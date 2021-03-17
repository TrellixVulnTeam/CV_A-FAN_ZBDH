import argparse
import os
import time
import uuid
from collections import deque
from typing import Optional

import torch
import torch.nn as nn
from tensorboardX import SummaryWriter
from torch import optim
from torch.utils.data import DataLoader

from backbone.base import Base as BackboneBase
from config.train_config import TrainConfig as Config
from dataset.base import Base as DatasetBase
from extension.lr_scheduler import WarmUpMultiStepLR
from logger import Logger as Log
from model import Model
# from model_advlayer4 import Model
from roi.pooler import Pooler
import attack_algo
import pdb

def _train(dataset_name: str, backbone_name: str, path_to_data_dir: str, path_to_checkpoints_dir: str, path_to_resuming_checkpoint: Optional[str], args):
    print("DATASET:[{}] DIR:[{}]".format(dataset_name, path_to_data_dir))
    dataset = DatasetBase.from_name(dataset_name)(path_to_data_dir, DatasetBase.Mode.TRAIN, Config.IMAGE_MIN_SIDE, Config.IMAGE_MAX_SIDE)
    dataloader = DataLoader(dataset, batch_size=Config.BATCH_SIZE,
                            sampler=DatasetBase.NearestRatioRandomSampler(dataset.image_ratios, num_neighbors=Config.BATCH_SIZE),
                            num_workers=8, collate_fn=DatasetBase.padding_collate_fn, pin_memory=True)

    Log.i('Found {:d} samples'.format(len(dataset)))

    backbone = BackboneBase.from_name(backbone_name)(pretrained=True)
    model = nn.DataParallel(
        Model(
            backbone, dataset.num_classes(), pooler_mode=Config.POOLER_MODE,
            anchor_ratios=Config.ANCHOR_RATIOS, anchor_sizes=Config.ANCHOR_SIZES,
            rpn_pre_nms_top_n=Config.RPN_PRE_NMS_TOP_N, rpn_post_nms_top_n=Config.RPN_POST_NMS_TOP_N,
            anchor_smooth_l1_loss_beta=Config.ANCHOR_SMOOTH_L1_LOSS_BETA, proposal_smooth_l1_loss_beta=Config.PROPOSAL_SMOOTH_L1_LOSS_BETA
        ).cuda()
    )
    
    optimizer = optim.SGD(model.parameters(), lr=Config.LEARNING_RATE,
                          momentum=Config.MOMENTUM, weight_decay=Config.WEIGHT_DECAY)
    scheduler = WarmUpMultiStepLR(optimizer, milestones=Config.STEP_LR_SIZES, gamma=Config.STEP_LR_GAMMA,
                                  factor=Config.WARM_UP_FACTOR, num_iters=Config.WARM_UP_NUM_ITERS)
    step = 0
    time_checkpoint = time.time()
    losses = deque(maxlen=100)
    summary_writer = SummaryWriter(os.path.join(path_to_checkpoints_dir, 'summaries'))
    should_stop = False

    num_steps_to_display = Config.NUM_STEPS_TO_DISPLAY
    num_steps_to_snapshot = Config.NUM_STEPS_TO_SNAPSHOT
    num_steps_to_finish = Config.NUM_STEPS_TO_FINISH

    if path_to_resuming_checkpoint is not None:
        step = model.module.load(path_to_resuming_checkpoint, optimizer, scheduler)
        Log.i(f'Model has been restored from file: {path_to_resuming_checkpoint}')

    device_count = torch.cuda.device_count()
    assert Config.BATCH_SIZE % device_count == 0, 'The batch size is not divisible by the device count'
    Log.i('Start training with {:d} GPUs ({:d} batches per GPU)'.format(torch.cuda.device_count(),
                                                                        Config.BATCH_SIZE // torch.cuda.device_count()))

    while not should_stop:
        for _, (_, image_batch, _, bboxes_batch, labels_batch) in enumerate(dataloader):

            batch_size = image_batch.shape[0]
            image_batch = image_batch.cuda()
            bboxes_batch = bboxes_batch.cuda()
            labels_batch = labels_batch.cuda()

            inputs_all_se = {"x": image_batch, "adv": None, "out_idx": args.pertub_idx_se, "flag": 'head'}
            inputs_all_sd = {"x": image_batch, "adv": None, "out_idx": args.pertub_idx_sd + '_head', "flag": 'clean'}

            feature_map_se = model.train().forward(inputs_all_se, bboxes_batch, labels_batch)
            feature_map_se = feature_map_se.detach()
            rpn_roi_output_dict = model.train().forward(inputs_all_sd, bboxes_batch, labels_batch)

            feature_adv_se = attack_algo.PGD(feature_map_se, image_batch, 
                y = {'bb':bboxes_batch, 'lb': labels_batch},
                model= model,
                steps = 1,
                eps = (2.0 / 255), 
                gamma = (args.gamma / 255),
                idx = args.pertub_idx_se,
                randinit = args.randinit,
                clip = args.clip)

            adv_rpn_roi_output_dict = attack_algo.rpn_roi_PGD(
                layer=args.pertub_idx_sd,
                rpn_roi_output_dict = rpn_roi_output_dict, 
                y = {'bb':bboxes_batch, 'lb': labels_batch},
                model= model,
                steps = 1,
                eps = (2.0 / 255), 
                gamma = (0.3 / 255),
                randinit = args.randinit,
                clip = args.clip,
                only_roi_loss=False)
            
            adv_input_dict_sd = {'adv': adv_rpn_roi_output_dict, 'out_idx': args.pertub_idx_sd + '_tail', 'flag':'clean'}
            adv_input_dict_se = {'x': image_batch, 'adv': feature_adv_se, 'out_idx': args.pertub_idx_se, 'flag':'tail'}
            clean_input_dict = {'x': image_batch, "adv": None, 'out_idx': 0, 'flag':'clean'}

            anchor_objectness_losses1, anchor_transformer_losses1, proposal_class_losses1, proposal_transformer_losses1 = \
                model.train().forward(adv_input_dict_sd, bboxes_batch, labels_batch)
            anchor_objectness_losses2, anchor_transformer_losses2, proposal_class_losses2, proposal_transformer_losses2 = \
                model.train().forward(adv_input_dict_se, bboxes_batch, labels_batch)    
            anchor_objectness_losses3, anchor_transformer_losses3, proposal_class_losses3, proposal_transformer_losses3 = \
                model.train().forward(clean_input_dict, bboxes_batch, labels_batch) 

            # adv loss
            anchor_objectness_loss1 = anchor_objectness_losses1.mean()
            anchor_transformer_loss1 = anchor_transformer_losses1.mean()
            proposal_class_loss1 = proposal_class_losses1.mean()
            proposal_transformer_loss1 = proposal_transformer_losses1.mean()
            loss1 = anchor_objectness_loss1 + anchor_transformer_loss1 + proposal_class_loss1 + proposal_transformer_loss1
            # adv loss
            anchor_objectness_loss2 = anchor_objectness_losses2.mean()
            anchor_transformer_loss2 = anchor_transformer_losses2.mean()
            proposal_class_loss2 = proposal_class_losses2.mean()
            proposal_transformer_loss2 = proposal_transformer_losses2.mean()
            loss2 = anchor_objectness_loss2 + anchor_transformer_loss2 + proposal_class_loss2 + proposal_transformer_loss2
            # clean loss
            anchor_objectness_loss3 = anchor_objectness_losses3.mean()
            anchor_transformer_loss3 = anchor_transformer_losses3.mean()
            proposal_class_loss3 = proposal_class_losses3.mean()
            proposal_transformer_loss3 = proposal_transformer_losses3.mean()
            loss3 = anchor_objectness_loss3 + anchor_transformer_loss3 + proposal_class_loss3 + proposal_transformer_loss3
            # adv + clean
            loss = (loss1 + loss2 + loss3) * 0.3333
            
            anchor_objectness_loss = anchor_objectness_loss1 + anchor_objectness_loss2 + anchor_objectness_loss3
            anchor_transformer_loss = anchor_transformer_loss1 + anchor_transformer_loss2 + anchor_transformer_loss3
            proposal_class_loss = proposal_class_loss1 + proposal_class_loss2 + proposal_class_loss3
            proposal_transformer_loss = proposal_transformer_loss1 + proposal_transformer_loss2 + proposal_transformer_loss3
            
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            scheduler.step()

            losses.append(loss.item())
            summary_writer.add_scalar('train/anchor_objectness_loss', anchor_objectness_loss.item(), step)
            summary_writer.add_scalar('train/anchor_transformer_loss', anchor_transformer_loss.item(), step)
            summary_writer.add_scalar('train/proposal_class_loss', proposal_class_loss.item(), step)
            summary_writer.add_scalar('train/proposal_transformer_loss', proposal_transformer_loss.item(), step)
            summary_writer.add_scalar('train/loss', loss.item(), step)
            step += 1

            if step == num_steps_to_finish:
                should_stop = True

            if step % num_steps_to_display == 0:
                elapsed_time = time.time() - time_checkpoint
                time_checkpoint = time.time()
                steps_per_sec = num_steps_to_display / elapsed_time
                samples_per_sec = batch_size * steps_per_sec
                eta = (num_steps_to_finish - step) / steps_per_sec / 3600
                avg_loss = sum(losses) / len(losses)
                lr = scheduler.get_lr()[0]
                Log.i(f'[Step {step}] Avg. Loss = {avg_loss:.6f}, LR = {lr:.8f} ({samples_per_sec:.2f} samples/sec; ETA {eta:.1f} hrs)')

            if step % num_steps_to_snapshot == 0 or should_stop:
                path_to_checkpoint = model.module.save(path_to_checkpoints_dir, step, optimizer, scheduler)
                Log.i(f'Model has been saved to {path_to_checkpoint}')

            if should_stop:
                break

    Log.i('Done')
    print("=" * 100)
    print("FINISL!")
    print("=" * 100)


if __name__ == '__main__':
    def main():
        parser = argparse.ArgumentParser()
        #PGD Setting 
        parser.add_argument('--steps', default=1, type=int, help='PGD-steps')
        parser.add_argument('--pertub_idx_sd', help='index of perturb layers', default=None, type=str)
        parser.add_argument('--pertub_idx_se', help='index of perturb layers', default=None, type=int)
        parser.add_argument('--gamma', help='index of PGD gamma', default=0.5, type=float)
        parser.add_argument('--eps', default=2, type=float)
        parser.add_argument('--randinit', action="store_true", help="whether using apex")
        parser.add_argument('--clip', action="store_true", help="whether using apex")

        parser.add_argument('-s', '--dataset', type=str, choices=DatasetBase.OPTIONS, required=True, help='name of dataset')
        parser.add_argument('-b', '--backbone', type=str, choices=BackboneBase.OPTIONS, required=True, help='name of backbone model')
        parser.add_argument('-d', '--data_dir', type=str, default='./data', help='path to data directory')
        parser.add_argument('-o', '--outputs_dir', type=str, default='./outputs', help='path to outputs directory')
        parser.add_argument('-r', '--resume_checkpoint', type=str, help='path to resuming checkpoint')
        parser.add_argument('--image_min_side', type=float, help='default: {:g}'.format(Config.IMAGE_MIN_SIDE))
        parser.add_argument('--image_max_side', type=float, help='default: {:g}'.format(Config.IMAGE_MAX_SIDE))
        parser.add_argument('--anchor_ratios', type=str, help='default: "{!s}"'.format(Config.ANCHOR_RATIOS))
        parser.add_argument('--anchor_sizes', type=str, help='default: "{!s}"'.format(Config.ANCHOR_SIZES))
        parser.add_argument('--pooler_mode', type=str, choices=Pooler.OPTIONS, help='default: {.value:s}'.format(Config.POOLER_MODE))
        parser.add_argument('--rpn_pre_nms_top_n', type=int, help='default: {:d}'.format(Config.RPN_PRE_NMS_TOP_N))
        parser.add_argument('--rpn_post_nms_top_n', type=int, help='default: {:d}'.format(Config.RPN_POST_NMS_TOP_N))
        parser.add_argument('--anchor_smooth_l1_loss_beta', type=float, help='default: {:g}'.format(Config.ANCHOR_SMOOTH_L1_LOSS_BETA))
        parser.add_argument('--proposal_smooth_l1_loss_beta', type=float, help='default: {:g}'.format(Config.PROPOSAL_SMOOTH_L1_LOSS_BETA))
        parser.add_argument('--batch_size', type=int, help='default: {:g}'.format(Config.BATCH_SIZE))
        parser.add_argument('--learning_rate', type=float, help='default: {:g}'.format(Config.LEARNING_RATE))
        parser.add_argument('--momentum', type=float, help='default: {:g}'.format(Config.MOMENTUM))
        parser.add_argument('--weight_decay', type=float, help='default: {:g}'.format(Config.WEIGHT_DECAY))
        parser.add_argument('--step_lr_sizes', type=str, help='default: {!s}'.format(Config.STEP_LR_SIZES))
        parser.add_argument('--step_lr_gamma', type=float, help='default: {:g}'.format(Config.STEP_LR_GAMMA))
        parser.add_argument('--warm_up_factor', type=float, help='default: {:g}'.format(Config.WARM_UP_FACTOR))
        parser.add_argument('--warm_up_num_iters', type=int, help='default: {:d}'.format(Config.WARM_UP_NUM_ITERS))
        parser.add_argument('--num_steps_to_display', type=int, help='default: {:d}'.format(Config.NUM_STEPS_TO_DISPLAY))
        parser.add_argument('--num_steps_to_snapshot', type=int, help='default: {:d}'.format(Config.NUM_STEPS_TO_SNAPSHOT))
        parser.add_argument('--num_steps_to_finish', type=int, help='default: {:d}'.format(Config.NUM_STEPS_TO_FINISH))
        args = parser.parse_args()
        attack_algo.print_args(args, 100)
        
        dataset_name = args.dataset
        backbone_name = args.backbone

        exp_name = "s" + str(args.steps) + "se_" + str(args.pertub_idx_se) + "_sd_" + str(args.pertub_idx_sd) + "_g" + str(args.gamma) + "e" + str(args.eps)
        if args.randinit: exp_name += "_rand"
        if args.clip: exp_name += "_clip"
        
        path_to_data_dir = args.data_dir
        path_to_outputs_dir = args.outputs_dir
        path_to_resuming_checkpoint = args.resume_checkpoint

        path_to_checkpoints_dir = os.path.join(path_to_outputs_dir, 'ckpt-{:s}-{:s}-{:s}'
            .format(exp_name, dataset_name, backbone_name))
        if not os.path.exists(path_to_checkpoints_dir):
            print("create dir:[{}]".format(path_to_checkpoints_dir))
            os.makedirs(path_to_checkpoints_dir)

        Config.setup(image_min_side=args.image_min_side, image_max_side=args.image_max_side,
                     anchor_ratios=args.anchor_ratios, anchor_sizes=args.anchor_sizes, pooler_mode=args.pooler_mode,
                     rpn_pre_nms_top_n=args.rpn_pre_nms_top_n, rpn_post_nms_top_n=args.rpn_post_nms_top_n,
                     anchor_smooth_l1_loss_beta=args.anchor_smooth_l1_loss_beta, proposal_smooth_l1_loss_beta=args.proposal_smooth_l1_loss_beta,
                     batch_size=args.batch_size, learning_rate=args.learning_rate, momentum=args.momentum, weight_decay=args.weight_decay,
                     step_lr_sizes=args.step_lr_sizes, step_lr_gamma=args.step_lr_gamma,
                     warm_up_factor=args.warm_up_factor, warm_up_num_iters=args.warm_up_num_iters,
                     num_steps_to_display=args.num_steps_to_display, num_steps_to_snapshot=args.num_steps_to_snapshot, num_steps_to_finish=args.num_steps_to_finish)

        Log.initialize(os.path.join(path_to_checkpoints_dir, 'train.log'))
        Log.i('Arguments:')
        for k, v in vars(args).items():
            Log.i(f'\t{k} = {v}')
        Log.i(Config.describe())
        print("-" * 100)
        print("INFO: PID   : [{}]".format(os.getpid()))
        print("EXP: Dataset:[{}] ADV Step:[{}] SE Layer:[{}] SD Layer:[{}] Gamma:[{}] Eps:[{}] Randinit:[{}] Clip:[{}]"
            .format(args.dataset, args.steps, args.pertub_idx_se, args.pertub_idx_sd, args.gamma, args.eps, args.randinit, args.clip))
        print("-" * 100)
        _train(dataset_name, backbone_name, path_to_data_dir, path_to_checkpoints_dir, path_to_resuming_checkpoint, args)

    main()
