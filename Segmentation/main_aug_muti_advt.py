import network
import utils
import os
import random
import args
import numpy as np
import time
from torch.utils import data
from metrics import StreamSegMetrics

import torch
import torch.nn as nn
from utils.visualizer import Visualizer
from torch.utils.tensorboard import SummaryWriter
from PIL import Image
import matplotlib
import matplotlib.pyplot as plt
import attack_algo
import pdb

def main():

    opts = args.get_argparser().parse_args()
    args.print_args(opts)
    
    if opts.dataset.lower() == 'voc':
        opts.num_classes = 21
    elif opts.dataset.lower() == 'cityscapes':
        opts.num_classes = 19

    # Setup visualization
    vis = Visualizer(port=opts.vis_port,
                     env=opts.vis_env) if opts.enable_vis else None
    if vis is not None:  # display options
        vis.vis_table("Options", vars(opts))

    os.environ['CUDA_VISIBLE_DEVICES'] = opts.gpu_id
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print("Device: %s" % device)

    # Setup random seed
    torch.manual_seed(opts.random_seed)
    np.random.seed(opts.random_seed)
    random.seed(opts.random_seed)

    # Setup dataloader
    if opts.dataset=='voc' and not opts.crop_val:
        opts.val_batch_size = 1
    
    train_dst, val_dst = args.get_dataset(opts)
    
    train_loader = data.DataLoader(
        train_dst, batch_size=opts.batch_size, shuffle=True, num_workers=2, drop_last=True)
    val_loader = data.DataLoader(
        val_dst, batch_size=opts.val_batch_size, shuffle=True, num_workers=2)
    print("Dataset: %s, Train set: %d, Val set: %d" %
          (opts.dataset, len(train_dst), len(val_dst)))

    # Set up model
    model_map = {
        'deeplabv3_resnet50': network.deeplabv3_resnet50,
        'deeplabv3plus_resnet50': network.deeplabv3plus_resnet50,
        'deeplabv3_resnet101': network.deeplabv3_resnet101,
        'deeplabv3plus_resnet101': network.deeplabv3plus_resnet101,
        'deeplabv3_mobilenet': network.deeplabv3_mobilenet,
        'deeplabv3plus_mobilenet': network.deeplabv3plus_mobilenet
    }

    model = model_map[opts.model](num_classes=opts.num_classes, output_stride=opts.output_stride)
    if opts.separable_conv and 'plus' in opts.model:
        network.convert_to_separable_conv(model.classifier)
    utils.set_bn_momentum(model.backbone, momentum=0.01)
    # Set up metrics
    metrics = StreamSegMetrics(opts.num_classes)
    # Set up optimizer
    optimizer = torch.optim.SGD(params=[
        {'params': model.backbone.parameters(), 'lr': 0.1*opts.lr},
        {'params': model.classifier.parameters(), 'lr': opts.lr},
    ], lr=opts.lr, momentum=0.9, weight_decay=opts.weight_decay)

    if opts.lr_policy=='poly':
        scheduler = utils.PolyLR(optimizer, opts.total_itrs, power=0.9)
    elif opts.lr_policy=='step':
        scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=opts.step_size, gamma=0.1)

    # Set up criterion
    if opts.loss_type == 'focal_loss':
        criterion = utils.FocalLoss(ignore_index=255, size_average=True)
    elif opts.loss_type == 'cross_entropy':
        criterion = nn.CrossEntropyLoss(ignore_index=255, reduction='mean')
        
    def save_ckpt(path):
        """ save current model
        """
        torch.save({
            "cur_itrs": cur_itrs,
            "model_state": model.module.state_dict(),
            "optimizer_state": optimizer.state_dict(),
            "scheduler_state": scheduler.state_dict(),
            "best_score": best_score,
        }, path)
        print("Model saved as %s" % path)
    opts.exp = opts.dataset.lower() + "_" + opts.model + "_" + opts.exp + "_muti_advt" 

    print("INFO: Save dir:[{}]".format(opts.exp))
    writer = SummaryWriter(log_dir='runs/' + opts.exp)
    utils.mkdir('checkpoints/' + opts.exp)
    # Restore
    best_score = 0.0
    cur_itrs = 0
    cur_epochs = 0
    if opts.ckpt is not None and os.path.isfile(opts.ckpt):

        checkpoint = torch.load(opts.ckpt, map_location=torch.device('cpu'))
        model.load_state_dict(checkpoint["model_state"])
        model = nn.DataParallel(model)
        model.to(device)
        if opts.continue_training:
            optimizer.load_state_dict(checkpoint["optimizer_state"])
            scheduler.load_state_dict(checkpoint["scheduler_state"])
            cur_itrs = checkpoint["cur_itrs"]
            best_score = checkpoint['best_score']
            print("Training state restored from %s" % opts.ckpt)
        print("Model restored from %s" % opts.ckpt)
        del checkpoint  # free memory
    else:
        print("[!] Retrain")
        model = nn.DataParallel(model)
        model.to(device)

    #==========   Train Loop   ==========#
    vis_sample_id = np.random.randint(0, len(val_loader), opts.vis_num_samples,
                                      np.int32) if opts.enable_vis else None  # sample idxs for visualization
    denorm = utils.Denormalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])  # denormalization for ori images

    if opts.test_only:
        model.eval()
        val_score, ret_samples = args.validate(
            opts=opts, model=model, loader=val_loader, device=device, metrics=metrics, ret_samples_ids=vis_sample_id)
        print(metrics.to_str(val_score))
        return

    interval_loss = 0
    total_time = 0
    while True: #cur_itrs < opts.total_itrs:
        # =====  Train  =====
        model.train()
        cur_epochs += 1
        for (images, labels) in train_loader:

            t0 = time.time()
            cur_itrs += 1
            
            images = images.to(device, dtype=torch.float32)
            labels = labels.to(device, dtype=torch.long)

            if images.shape[0] != 4: continue
            adv_images = attack_algo.adv_input(x=images, criterion=criterion, y=labels, model=model, steps=5, eps=(4.0 / 255), gamma=(1.0 / 255), randinit=True, clip=True)

            inputs_all1    = {"x": images,     "adv": None, "out_idx": 1,             "flag": 'head' }
            inputs_all2    = {"x": images,     "adv": None, "out_idx": 2,             "flag": 'head' }
            inputs_all3    = {"x": images,     "adv": None, "out_idx": 3,             "flag": 'head' }
            inputs_all4    = {"x": images,     "adv": None, "out_idx": 4,             "flag": 'head' }
            inputs_allsd   = {"x": images,     "adv": None, "out_idx": "concat_head", "flag": 'clean'}
        
            optimizer.zero_grad()

            output_dict1 = model(inputs_all1)
            output_dict2 = model(inputs_all2)
            output_dict3 = model(inputs_all3)
            output_dict4 = model(inputs_all4)

            decoder_feature_map_dict = model(inputs_allsd)
            feature_map_sd = decoder_feature_map_dict['adv'].detach()

            low_level_feat = output_dict1["low_level"]
            feature_map1 = output_dict1['out'].detach()
            feature_map2 = output_dict2['out'].detach()
            feature_map3 = output_dict3['out'].detach()
            feature_map4 = output_dict4['out'].detach()
            
            feature_adv1 = attack_algo.PGD(x=feature_map1, image_batch=images, low_level_feat=low_level_feat, criterion=criterion, y=labels, model=model,
                steps=1, eps=(2.0 / 255),  gamma=(0.001 / 255), idx=1)
            
            feature_adv2 = attack_algo.PGD(x=feature_map2, image_batch=images, low_level_feat=low_level_feat, criterion=criterion, y=labels, model=model,
                steps=1, eps=(2.0 / 255),  gamma=(0.001 / 255), idx=2)

            feature_adv3 = attack_algo.PGD(x=feature_map3, image_batch=images, low_level_feat=low_level_feat, criterion=criterion, y=labels, model=model,
                steps=1, eps=(2.0 / 255),  gamma=(0.1 / 255), idx=3)
            
            feature_adv4 = attack_algo.PGD(x=feature_map4, image_batch=images, low_level_feat=low_level_feat, criterion=criterion, y=labels, model=model,
                steps=1, eps=(2.0 / 255),  gamma=(0.001 / 255), idx=4)

            feature_adv_sd_dict = attack_algo.decoder_PGD(input_dict=decoder_feature_map_dict, image_batch=images, criterion=criterion, y = labels, model= model,
                steps=1, eps=(2.0 / 255),  gamma = (0.4 / 255), idx='concat')

            adv_feature_map_sd = feature_adv_sd_dict['adv'].detach()

            if opts.mix_all:
                feature_adv1 = attack_algo.mix_feature(feature_map1, feature_adv1)
                feature_adv2 = attack_algo.mix_feature(feature_map2, feature_adv2)
                feature_adv3 = attack_algo.mix_feature(feature_map3, feature_adv3)
                feature_adv4 = attack_algo.mix_feature(feature_map4, feature_adv4)
                adv_feature_map_sd = attack_algo.mix_feature(feature_map_sd, adv_feature_map_sd)
            feature_adv_sd_dict['adv'] = adv_feature_map_sd

            adv_input_dict     = {'x': adv_images, 'adv': None,            'out_idx': 0, 'flag':'clean'}
            adv_input_se_dict1 = {'x': images, 'adv': feature_adv1,        'out_idx': 1, 'flag':'tail', 'low_level_feat': low_level_feat}
            adv_input_se_dict2 = {'x': images, 'adv': feature_adv2,        'out_idx': 2, 'flag':'tail', 'low_level_feat': low_level_feat}
            adv_input_se_dict3 = {'x': images, 'adv': feature_adv3,        'out_idx': 3, 'flag':'tail', 'low_level_feat': low_level_feat}
            adv_input_se_dict4 = {'x': images, 'adv': feature_adv4,        'out_idx': 4, 'flag':'tail', 'low_level_feat': low_level_feat}
            adv_input_sd_dict  = {'x': images, 'adv': feature_adv_sd_dict, 'out_idx': "concat_tail",      'flag':'clean'}

            output0 = model(adv_input_dict)
            output1 = model(adv_input_se_dict1)
            output2 = model(adv_input_se_dict2)
            output3 = model(adv_input_se_dict3)
            output4 = model(adv_input_se_dict4)
            output5 = model(adv_input_sd_dict)
            # 1108
            loss0 = criterion(output0, labels) # advt
            loss1 = criterion(output1, labels) # muti1
            loss2 = criterion(output2, labels) # muti2
            loss3 = criterion(output3, labels) # muti3 best
            loss4 = criterion(output4, labels) # muti4
            loss5 = criterion(output5, labels) # sd

            if opts.loss_settings == 1:
                # setting1: 0.8 advt + 0.2 others
                loss = 0.8 * loss0 + 0.04 * (loss1 + loss2 + loss3 + loss4 + loss5)
            elif opts.loss_settings == 2:
                # setting2: 0.6 advt + 0.4 others
                loss = 0.6 * loss0 + 0.08 * (loss1 + loss2 + loss3 + loss4 + loss5)
            elif opts.loss_settings == 3:
                # setting3: 0.6 advt + 0.3 best + 0.1 others
                loss = 0.6 * loss0 + 0.3 * loss3 + 0.025 * (loss1 + loss2 + loss4 + loss5)
            elif opts.loss_settings == 4:
                # setting4: 0.6 advt + 0.2 best + 0.2 others
                loss = 0.6 * loss0 + 0.2 * loss3 + 0.05 * (loss1 + loss2 + loss4 + loss5)
            else: assert False


            # if opts.loss_settings == 1:
            #     # setting1: average all loss
            #     loss = 0.5 * ((loss0 + loss1 + loss2 + loss3 + loss4 + loss5) / 6.0) + 0.5 * loss6
            # elif opts.loss_settings == 2:
            #     # setting2: 0.5 clean + 0.5 others
            #     loss = 0.5 * loss0 + 0.1 * (loss1 + loss2 + loss3 + loss4 + loss5)
            # elif opts.loss_settings == 3:
            #     # setting3: 0.4 (clean + layer3) + 0.2 others
            #     loss = 0.4 * (loss0 + loss3) +  0.05 * (loss1 + loss2 + loss4 + loss5)
            # elif opts.loss_settings == 3:
            #     # setting4: 0.3 (clean + layer3 + layer4) + 0.1 others
            #     loss = 0.3 * (loss0 + loss3 + loss4) +  0.1 * (loss1 + loss2 + loss5) / 3.0
            # else: assert False
            
            loss.backward()
            optimizer.step()

            np_loss = loss.detach().cpu().numpy()
            interval_loss += np_loss
            if vis is not None:
                vis.vis_scalar('Loss', cur_itrs, np_loss)

            if (cur_itrs) % 10 == 0:
                interval_loss = interval_loss / 10
                print(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()) + ' | ' + 
                      "Epoch:[{}], Itrs:[{}/{}], Loss:[{:.4f}], Time:[{:.4f} min], Best IOU:[{:.4f}]"
                      .format(cur_epochs, cur_itrs, int(opts.total_itrs), interval_loss, total_time / 60, best_score))
                writer.add_scalar('Loss/train', interval_loss, cur_itrs)
                interval_loss = 0.0
                total_time = 0.0

            if (cur_itrs) % opts.val_interval == 0 and cur_itrs >= opts.total_itrs / 2:
            # if (cur_itrs) % opts.val_interval == 0:
                save_ckpt('checkpoints/' + opts.exp + '/latest_%s_%s_os%d.pth' %
                          (opts.model, opts.dataset, opts.output_stride))
                print("validation...")
                model.eval()
                val_score, ret_samples = args.validate(
                    opts=opts, model=model, loader=val_loader, device=device, metrics=metrics, ret_samples_ids=vis_sample_id)
                print(metrics.to_str(val_score))

                writer.add_scalar('mIOU/test', val_score['Mean IoU'], cur_itrs)

                if val_score['Mean IoU'] > best_score:  # save best model
                    best_score = val_score['Mean IoU']
                    save_ckpt('checkpoints/' + opts.exp +'/best_%s_%s_os%d.pth' %
                              (opts.model, opts.dataset, opts.output_stride))

                if vis is not None:  # visualize validation score and samples
                    vis.vis_scalar("[Val] Overall Acc", cur_itrs, val_score['Overall Acc'])
                    vis.vis_scalar("[Val] Mean IoU", cur_itrs, val_score['Mean IoU'])
                    vis.vis_table("[Val] Class IoU", val_score['Class IoU'])

                    for k, (img, target, lbl) in enumerate(ret_samples):
                        img = (denorm(img) * 255).astype(np.uint8)
                        target = train_dst.decode_target(target).transpose(2, 0, 1).astype(np.uint8)
                        lbl = train_dst.decode_target(lbl).transpose(2, 0, 1).astype(np.uint8)
                        concat_img = np.concatenate((img, target, lbl), axis=2)  # concat along width
                        vis.vis_image('Sample %d' % k, concat_img)
                model.train()

            scheduler.step() 
            t1 = time.time() 
            total_time += t1 - t0

            if cur_itrs >=  opts.total_itrs:
                print("syd: --------------------[SD]--------------------")
                print("syd: Model dir:[{}]".format(opts.exp))
                print("syd: Setting: Layer:[{}] Gamma:[{}] Best IOU:[{}]"
                    .format(opts.pertub_idx_sd, opts.gamma_sd, best_score))
                print("syd: --------------------[SD]--------------------")
                writer.close()
                return

        
if __name__ == '__main__':
    main()
