import sys
import os
import time
import datetime
import wandb

import torch
from torch import nn
import torch.optim as optim
import torchvision
from torchvision import transforms

import matplotlib.pyplot as plt
import seaborn as sns
sns.set()

sys.path.append('.')
sys.path.append('..')
from scripts.VAE import VAE, VAELoss
from scripts.image_dataset import ImageDataset
from scripts.plot_result import *
from scripts.print_progress_bar import print_progress_bar


def to_one_hot(n_class=10):
    return lambda labels: torch.eye(n_class)[labels]


def train_VAE(n_epochs, train_loader, valid_loader, model, loss_fn,
              out_dir='', lr=0.001, optimizer_cls=optim.Adam, wandb_flag=False,
              gpu_num=0, conditional=False, label_transform=lambda x: x):
    train_losses, valid_losses = [], []
    train_losses_mse, valid_losses_mse = [], []
    train_losses_ssim, valid_losses_ssim = [], []
    train_losses_kl, valid_losses_kl = [], []
    total_elapsed_time = 0
    best_test = 1e10
    optimizer = optimizer_cls(model.parameters(), lr=lr)

    # device setting
    cuda_flag = torch.cuda.is_available()
    device = torch.device(f'cuda:{gpu_num[0]}' if cuda_flag else 'cpu')
    print('device:', device)
    print("Let's use", torch.cuda.device_count(), "GPUs!")
    model = nn.DataParallel(model, device_ids=gpu_num)
    model.to(device)

    # acceleration
    scaler = torch.cuda.amp.GradScaler()
    torch.backends.cudnn.benchmark = True

    # figure
    fig_reconstructed_image = plt.figure(figsize=(20, 10))
    fig_latent_space = plt.figure(figsize=(10, 10))
    fig_2D_Manifold = plt.figure(figsize=(10, 10))
    fig_latent_traversal = plt.figure(figsize=(10, 10))
    fig_loss = plt.figure(figsize=(10, 10))

    for epoch in range(n_epochs + 1):
        start = time.time()

        running_loss = 0.0
        running_loss_mse = 0.0
        running_loss_ssim = 0.0
        running_loss_kl = 0.0
        model.train()
        for i, (image, label) in enumerate(train_loader):
            image = image.to(device)

            with torch.cuda.amp.autocast():
                if conditional:
                    label_one_hot = label_transform(label)
                    label_one_hot = label_one_hot.to(device)
                    y, mean, std = model(image, label_one_hot)
                else:
                    y, mean, std = model(image)
                loss_mse, loss_ssim, loss_kl = loss_fn(image, y, mean, std)

            loss = loss_mse + loss_ssim + loss_kl

            optimizer.zero_grad()
            scaler.scale(loss).backward()
            scaler.step(optimizer)
            scaler.update()

            running_loss += loss.item()
            running_loss_mse += loss_mse.item()
            running_loss_ssim += loss_ssim.item()
            running_loss_kl += loss_kl.item()

            header = f'epoch: {epoch}'
            print_progress_bar(i, len(train_loader), end='', header=header)

        train_loss = running_loss / len(train_loader)
        train_loss_mse = running_loss_mse / len(train_loader)
        train_loss_ssim = running_loss_ssim / len(train_loader)
        train_loss_kl = running_loss_kl / len(train_loader)
        train_losses.append(train_loss)
        train_losses_mse.append(train_loss_mse)
        train_losses_ssim.append(train_loss_ssim)
        train_losses_kl.append(train_loss_kl)

        running_loss = 0.0
        running_loss_mse = 0.0
        running_loss_ssim = 0.0
        running_loss_kl = 0.0
        valid_mean = []
        valid_label = []
        model.eval()
        for image, label in valid_loader:
            image = image.to(device)

            with torch.cuda.amp.autocast():
                if conditional:
                    label_one_hot = label_transform(label)
                    label_one_hot = label_one_hot.to(device)
                    y, mean, std = model(image, label_one_hot)
                else:
                    y, mean, std = model(image)
                loss_mse, loss_ssim, loss_kl = loss_fn(image, y, mean, std)

            loss = loss_mse + loss_ssim + loss_kl

            running_loss += loss.item()
            running_loss_mse += loss_mse.item()
            running_loss_ssim += loss_ssim.item()
            running_loss_kl += loss_kl.item()
            if len(valid_mean) * mean.shape[0] < 1000:
                valid_mean.append(mean)
                valid_label.append(label)

        valid_loss = running_loss / len(valid_loader)
        valid_loss_mse = running_loss_mse / len(valid_loader)
        valid_loss_ssim = running_loss_ssim / len(valid_loader)
        valid_loss_kl = running_loss_kl / len(valid_loader)
        valid_losses.append(valid_loss)
        valid_losses_mse.append(valid_loss_mse)
        valid_losses_ssim.append(valid_loss_ssim)
        valid_losses_kl.append(valid_loss_kl)
        valid_mean = torch.cat(valid_mean, dim=0)
        valid_label = torch.cat(valid_label, dim=0)

        end = time.time()
        elapsed_time = end - start
        total_elapsed_time += elapsed_time

        log = '\r\033[K' + f'epoch: {epoch}'
        log += '  train loss: {:.6f} ({:.6f}, {:.6f}, {:.6f})'.format(
            train_loss, train_loss_mse, train_loss_ssim, train_loss_kl)
        log += '  valid loss: {:.6f} ({:.6f}, {:.6f}, {:.6f})'.format(
            valid_loss, valid_loss_mse, valid_loss_ssim, valid_loss_kl)
        log += '  elapsed time: {:.3f}'.format(elapsed_time)
        print(log)

        # save model
        if valid_loss < best_test:
            best_test = valid_loss
            path_model_param_best = os.path.join(
                out_dir, 'model_param_best.pt')
            torch.save(model.state_dict(), path_model_param_best)
            if wandb_flag:
                wandb.save(path_model_param_best)

        if epoch % 100 == 0:
            model_param_dir = os.path.join(out_dir, 'model_param')
            if not os.path.exists(model_param_dir):
                os.mkdir(model_param_dir)
            path_model_param = os.path.join(
                model_param_dir,
                'model_param_{:06d}.pt'.format(epoch))
            torch.save(model.state_dict(), path_model_param)
            if wandb_flag:
                wandb.save(path_model_param)

        # save checkpoint
        path_checkpoint = os.path.join(out_dir, 'checkpoint.pt')
        torch.save({
            'epoch': epoch,
            'model_state_dict': model.state_dict(),
            'optimizer_state_dict': optimizer.state_dict(),
            'loss': loss,
        }, path_checkpoint)

        # plot loss
        fig_loss.clf()
        plot_losses(fig_loss, train_losses, valid_losses,
                    train_losses_mse, valid_losses_mse,
                    train_losses_ssim, valid_losses_ssim,
                    train_losses_kl, valid_losses_kl)
        fig_loss.savefig(os.path.join(out_dir, 'loss.png'))

        # show output
        if epoch % 10 == 0:
            image_ans = formatImages(image)
            image_hat = formatImages(y)
            fig_reconstructed_image.clf()
            plot_reconstructed_image(fig_reconstructed_image, image_ans,
                                     image_hat, col=10, epoch=epoch)
            fig_reconstructed_image.savefig(
                os.path.join(out_dir, 'reconstructed_image.png'))

            fig_latent_space.clf()
            plot_latent_space(fig_latent_space, valid_mean,
                              valid_label, epoch=epoch)
            fig_latent_space.savefig(
                os.path.join(out_dir, 'latent_space.png'))

            fig_2D_Manifold.clf()
            if conditional:
                label = label[0]
            else:
                label = None
            plot_2D_Manifold(fig_2D_Manifold, model.module, device,
                             z_sumple=valid_mean, col=20, epoch=epoch,
                             label=label, label_transform=label_transform)
            fig_2D_Manifold.savefig(
                os.path.join(out_dir, '2D_Manifold.png'))

            fig_latent_traversal.clf()
            plot_latent_traversal(fig_latent_traversal, model.module, device,
                                  row=valid_mean.shape[1], col=10, epoch=epoch,
                                  label=label, label_transform=label_transform)
            fig_latent_traversal.savefig(
                os.path.join(out_dir, 'latent_traversal.png'))

            if wandb_flag:
                wandb.log({
                    'epoch': epoch,
                    'reconstructed_image': wandb.Image(fig_reconstructed_image),
                    'latent_space': wandb.Image(fig_latent_space),
                    '2D_Manifold': wandb.Image(fig_2D_Manifold),
                    'latent_traversal': wandb.Image(fig_latent_traversal),
                })

        # wandb
        if wandb_flag:
            wandb.log({
                'epoch': epoch,
                'iteration': len(train_loader) * epoch,
                'train_loss': train_loss,
                'train_loss_mse': train_loss_mse,
                'train_loss_ssim': train_loss_ssim,
                'train_loss_kl': train_loss_kl,
                'valid_loss': valid_loss,
                'valid_loss_mse': valid_loss_mse,
                'valid_loss_ssim': valid_loss_ssim,
                'valid_loss_kl': valid_loss_kl,
            })
            wandb.save(path_checkpoint)

    print('total elapsed time: {} [s]'.format(total_elapsed_time))


def torchvision_dataset(dataset, image_size):
    transform = transforms.Compose([
        transforms.ToTensor(),
        transforms.Resize((image_size, image_size)),
    ])

    label_dim = 10
    if dataset == 'mnist':
        train_dataset = torchvision.datasets.MNIST(
            root='../datasets/mnist', train=True,
            download=True, transform=transform)
        valid_dataset = torchvision.datasets.MNIST(
            root='../datasets/mnist', train=False,
            download=True, transform=transform)
    elif dataset == 'emnist':
        train_dataset = torchvision.datasets.EMNIST(
            root='../datasets/emnist', split='balanced', train=True,
            download=True, transform=transform)
        valid_dataset = torchvision.datasets.EMNIST(
            root='../datasets/emnist', split='balanced', train=False,
            download=True, transform=transform)
    elif dataset == 'fashion-mnist':
        train_dataset = torchvision.datasets.FashionMNIST(
            root='../datasets/fashion-mnist', train=True,
            download=True, transform=transform)
        valid_dataset = torchvision.datasets.FashionMNIST(
            root='../datasets/fashion-mnist', train=False,
            download=True, transform=transform)
    elif dataset == 'cifar10':
        train_dataset = torchvision.datasets.CIFAR10(
            root='../datasets/cifar10', train=True,
            download=True, transform=transform)
        valid_dataset = torchvision.datasets.CIFAR10(
            root='../datasets/cifar10', train=False,
            download=True, transform=transform)
    elif dataset == 'stl10':
        train_dataset = torchvision.datasets.STL10(
            root='../datasets/stl10', split='train',
            download=True, transform=transform)
        valid_dataset = torchvision.datasets.STL10(
            root='../datasets/stl10', split='test',
            download=True, transform=transform)
    elif dataset == 'celebA':
        train_dataset = torchvision.datasets.CelebA(
            root='../datasets/celebA', split='train', target_type='identity',
            download=True, transform=transform)
        valid_dataset = torchvision.datasets.CelebA(
            root='../datasets/celebA', split='valid', target_type='identity',
            download=True, transform=transform)
    else:
        train_dataset = ImageDataset(
            dataset, train=True, image_size=image_size)
        valid_dataset = ImageDataset(
            dataset, train=False, image_size=image_size)
        label_dim = train_dataset.label_dim

    return train_dataset, valid_dataset, label_dim


def main(args):
    train_dataset, valid_dataset, label_dim = torchvision_dataset(
        args.data_path, image_size=args.image_size)
    train_loader = torch.utils.data.DataLoader(
        train_dataset,
        batch_size=args.batch_size,
        shuffle=True,
        num_workers=8,
        # pin_memory=True,
    )
    valid_loader = torch.utils.data.DataLoader(
        valid_dataset,
        batch_size=args.batch_size,
        shuffle=True,
        num_workers=8,
        # pin_memory=True,
        drop_last=True,
    )

    image, label = train_dataset[0]
    n_channel = image.shape[-3]
    label_transform = lambda x: x
    if args.conditional:
        label_transform = to_one_hot(label_dim)
    else:
        label_dim = 0
    model = VAE(
        z_dim=5,
        image_size=args.image_size,
        n_channel=n_channel,
        label_dim=label_dim
    )

    if not os.path.exists('results'):
        os.mkdir('results')
    out_dir = 'results/' + datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    os.mkdir(out_dir)

    if args.wandb:
        wandb.init(project='VAE')
        wandb.watch(model)

        config = wandb.config

        config.data_path = args.data_path
        config.epoch = args.epoch
        config.batch_size = args.batch_size
        config.learning_rate = args.learning_rate
        config.image_size = args.image_size
        config.gpu_num = args.gpu_num
        config.conditional = args.conditional

        config.train_data_num = len(train_dataset)
        config.valid_data_num = len(valid_dataset)

    train_VAE(
        n_epochs=args.epoch,
        train_loader=train_loader,
        valid_loader=valid_loader,
        model=model,
        loss_fn=VAELoss(),
        out_dir=out_dir,
        lr=args.learning_rate,
        wandb_flag=args.wandb,
        gpu_num=args.gpu_num,
        conditional=args.conditional,
        label_transform=label_transform,
    )


def argparse():
    from argparse import ArgumentParser
    parser = ArgumentParser()
    parser.add_argument('--data_path', type=str, default='mnist')
    parser.add_argument('--epoch', type=int, default=10000)
    parser.add_argument('--batch_size', type=int, default=100)
    parser.add_argument('--learning_rate', type=float, default=0.001)
    parser.add_argument('--image_size', type=int, default=32)
    parser.add_argument('--wandb', action='store_true')
    tp = lambda x:list(map(int, x.split(',')))
    parser.add_argument('--gpu_num', type=tp, default='0')
    parser.add_argument('--conditional', action='store_true')
    return parser.parse_args()


if __name__ == '__main__':
    args = argparse()
    main(args)
