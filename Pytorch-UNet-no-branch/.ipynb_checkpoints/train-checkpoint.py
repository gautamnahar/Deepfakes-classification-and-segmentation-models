import argparse
import logging
import sys
from pathlib import Path

import torch
import torch.nn as nn
import torch.nn.functional as F
import wandb
from torch import optim
from torch.utils.data import DataLoader, random_split
from tqdm import tqdm

from utils.data_loading import BasicDataset, CarvanaDataset
from utils.dice_score import dice_loss
from evaluate import evaluate
from unet import UNet

# +
dir_img_training = Path('../dfdc_deepfake_challenge/dataset_new_1/training/crops/')
dir_mask_training = Path('../dfdc_deepfake_challenge/dataset_new_1/training/masks/')

dir_img_testing = Path('../dfdc_deepfake_challenge/dataset_new_1/testing/crops/crops_testing/')
dir_mask_testing = Path('../dfdc_deepfake_challenge/dataset_new_1/testing/masks/masks_testing/')


dir_checkpoint = Path('./checkpoints/')


# +
def train_net(net,
              device,
              epochs: int = 20,
              batch_size: int = 32,
              learning_rate: float = 0.001,
              val_percent: float = 0.1,
              save_checkpoint: bool = True,
              img_scale: float = 1.0,
              amp: bool = False):
    # 1. Create dataset
    try:
        dataset_training = CarvanaDataset(dir_img_training, dir_mask_training, img_scale)
    except (AssertionError, RuntimeError):
        dataset_training = BasicDataset(dir_img_training, dir_mask_training, img_scale)
        
    try:
        dataset_testing = CarvanaDataset(dir_img_testing, dir_mask_testing, img_scale)
    except (AssertionError, RuntimeError):
        dataset_testing = BasicDataset(dir_img_testing, dir_mask_testing, img_scale)

    # 2. Split into train / validation partitions
    n_val = len(dataset_testing)
    n_train = len(dataset_training)
    train_set, _ = random_split(dataset_training, [n_train, 0], generator=torch.Generator().manual_seed(0))
    _, val_set = random_split(dataset_testing, [0, n_val], generator=torch.Generator().manual_seed(0))

    # 3. Create data loaders
    loader_args = dict(batch_size=batch_size, num_workers=4, pin_memory=True)
    train_loader = DataLoader(train_set, shuffle=False,**loader_args)
    val_loader = DataLoader(val_set, shuffle=False, drop_last=True,**loader_args)

    # (Initialize logging)
    experiment = wandb.init(project='U-Net', reinit=True, anonymous='must', entity="sravanchittupalli")
    experiment.config.update(dict(epochs=epochs, batch_size=batch_size, learning_rate=learning_rate,
                                  val_percent=val_percent, save_checkpoint=save_checkpoint, img_scale=img_scale,
                                  amp=amp))

    logging.info(f'''Starting training:
        Epochs:          {epochs}
        Batch size:      {batch_size}
        Learning rate:   {learning_rate}
        Training size:   {n_train}
        Validation size: {n_val}
        Checkpoints:     {save_checkpoint}
        Device:          {device.type}
        Images scaling:  {img_scale}
        Mixed Precision: {amp}
    ''')

    # 4. Set up the optimizer, the loss, the learning rate scheduler and the loss scaling for AMP
    optimizer = optim.Adam(net.parameters(), lr=learning_rate, weight_decay=1e-8)#, momentum=0.9)
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, 'max', patience=2)  # goal: maximize Dice score
    grad_scaler = torch.cuda.amp.GradScaler(enabled=amp)
    criterion = nn.BCELoss()
    global_step = 0

    # 5. Begin training
    for epoch in range(epochs):
        net.train()
        epoch_loss = 0
        with tqdm(total=n_train, desc=f'Epoch {epoch + 1}/{epochs}', unit='img') as pbar:
            for batch in train_loader:
                images = batch['image']
                true_out = batch['output']
                
                assert images.shape[1] == net.n_channels, \
                    f'Network has been defined with {net.n_channels} input channels, ' \
                    f'but loaded images have {images.shape[1]} channels. Please check that ' \
                    'the images are loaded correctly.'

                images = images.to(device=device, dtype=torch.float32)
                true_out = true_out.to(device=device, dtype=torch.float32)

                with torch.cuda.amp.autocast(enabled=amp):
                    pred = net(images)
#                     print(pred, true_out)
                    loss = criterion(pred, true_out)

                optimizer.zero_grad(set_to_none=True)
                grad_scaler.scale(loss).backward()
                grad_scaler.step(optimizer)
                grad_scaler.update()
                epoch_loss += loss.item()

                pbar.update(images.shape[0])
            
            global_step += 1
            experiment.log({
                'train loss': epoch_loss/(n_train//batch_size),
                'step': global_step,
                'epoch': epoch
            })
            pbar.set_postfix(**{'loss (epoch)': epoch_loss/(n_train//batch_size)})

        # Evaluation round
        # print(global_step, n_train, batch_size)
#         if epoch % 2== 0:
#             histograms = {}
#             for tag, value in net.named_parameters():
#                 tag = tag.replace('/', '.')
#                 histograms['Weights/' + tag] = wandb.Histogram(value.data.cpu())
#                 histograms['Gradients/' + tag] = wandb.Histogram(value.grad.data.cpu())

#             val_score = evaluate(net, val_loader, device)
#             scheduler.step(val_score)

#             logging.info('Validation Dice score: {}'.format(val_score))
#             experiment.log({
#                 'learning rate': optimizer.param_groups[0]['lr'],
#                 'validation score': val_score,
#                 'step': global_step,
#                 'epoch': epoch,
#                 **histograms
#             })

        if (epoch%2 == 0):
            Path(dir_checkpoint).mkdir(parents=True, exist_ok=True)
            torch.save(net.state_dict(), str(dir_checkpoint / f'checkpoint_epoch_224x224_{epoch + 1}.pth'))
            logging.info(f'Checkpoint {epoch + 1} saved!')


# -

def get_args():
    parser = argparse.ArgumentParser(description='Train the UNet on images and target masks')
    parser.add_argument('--epochs', '-e', metavar='E', type=int, default=100, help='Number of epochs')
    parser.add_argument('--batch-size', '-b', dest='batch_size', metavar='B', type=int, default=64, help='Batch size')
    parser.add_argument('--learning-rate', '-l', metavar='LR', type=float, default=0.00001,
                        help='Learning rate', dest='lr')
    parser.add_argument('--load', '-f', type=str, default=False, help='Load model from a .pth file')
    parser.add_argument('--scale', '-s', type=float, default=1.0, help='Downscaling factor of the images')
    parser.add_argument('--validation', '-v', dest='val', type=float, default=10.0,
                        help='Percent of the data that is used as validation (0-100)')
    parser.add_argument('--amp', action='store_true', default=False, help='Use mixed precision')

    return parser.parse_args()


if __name__ == '__main__':
    args = get_args()

    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
    logging.info(f'Using device {device}')

    # Change here to adapt to your data
    # n_channels=3 for RGB images
    # n_classes is the number of probabilities you want to get per pixel
    net = UNet(n_channels=3, n_classes=2)

    logging.info(f'Network:\n'
                 f'\t{net.n_channels} input channels\n'
                 f'\t{net.n_classes} output channels (classes)')

    if args.load:
        net.load_state_dict(torch.load(args.load, map_location=device))
        logging.info(f'Model loaded from {args.load}')

    net.to(device=device)
    try:
        train_net(net=net,
                  epochs=args.epochs,
                  batch_size=args.batch_size,
                  learning_rate=args.lr,
                  device=device,
                  img_scale=args.scale,
                  val_percent=args.val / 100,
                  amp=args.amp)
    except KeyboardInterrupt:
        torch.save(net.state_dict(), 'INTERRUPTED.pth')
        logging.info('Saved interrupt')
        sys.exit(0)
