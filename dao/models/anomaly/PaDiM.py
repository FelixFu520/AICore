# -*- coding: utf-8 -*-
# @Author:FelixFu
# @Date: 2021.12.17
# @GitHub:https://github.com/felixfu520
# @Copy From: https://github.com/xiahaifeng1995/PaDiM-Anomaly-Detection-Localization-master/blob/main/main.py

import os
import pickle
import random
from random import sample
from tqdm import tqdm
from collections import OrderedDict

import numpy as np
from loguru import logger
from random import sample
from sklearn.metrics import roc_auc_score
from sklearn.metrics import roc_curve
from sklearn.metrics import precision_recall_curve
from scipy.spatial.distance import mahalanobis
from scipy.ndimage import gaussian_filter
import matplotlib.pyplot as plt
import matplotlib
from skimage import morphology
from skimage.segmentation import mark_boundaries

import onnx
import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader
from torchvision.models import wide_resnet50_2, resnet18
import timm
import torch.nn.functional as F

from dao.register import Registers
from .MyAdaptiveAvgPool2d import MyAdaptiveAvgPool2d


@Registers.anomaly_models.register
class PaDiM:
    def __init__(self, backbone, device=None, d_reduced: int = 100, total_dim=None, image_size=224, beta=1):
        # backbone load model
        if backbone.type == 'resnet18':
            self.model = resnet18(pretrained=True, progress=True)
        elif backbone.type == 'wide_resnet50_2':
            self.model = wide_resnet50_2(pretrained=True, progress=True)
        self.model.to(device)
        self.model.eval()
        # set model's intermediate outputs
        self.outputs = []
        def hook(module, input, output):
            self.outputs.append(output)
        self.model.layer1[-1].register_forward_hook(hook)
        self.model.layer2[-1].register_forward_hook(hook)
        self.model.layer3[-1].register_forward_hook(hook)

        self.image_size = image_size
        self.d_reduced = d_reduced  # your RAM will thank you   选取维度
        self.t_d = total_dim    # 总维度特征
        self.beta = beta
        self.device = device

        self.resize = torch.nn.AdaptiveAvgPool2d(int(image_size/4))     # 方式2所需要

    def fit(self, train_dataloader, output_dir=None):
        # extract train set features 提取特征
        train_feature_filepath = os.path.join(output_dir, 'features.pkl')  # 特征存放路径
        train_outputs = OrderedDict([('layer1', []), ('layer2', []), ('layer3', [])])
        if not os.path.exists(train_feature_filepath):  # 如果特征不存在
            # 提取特征
            logger.info("1.1 extract train set features")
            for i, (image, mask, label, image_path) in enumerate(train_dataloader):
                logger.info("extract feature iter {}/{}".format(i, len(train_dataloader)))
                # model prediction
                with torch.no_grad():
                    _ = self.model(image.to(self.device))
                # get intermediate layer outputs
                for k, v in zip(train_outputs.keys(), self.outputs):
                    train_outputs[k].append(v.cpu().detach())
                # initialize hook outputs
                self.outputs = []
            # 合并特征
            for k, v in train_outputs.items():
                train_outputs[k] = torch.cat(v, 0)

            # 将feature_maps 转为 embedding_vectors: torch.Size([200, 1792, 56, 56])
            logger.info("1.2 covert feature_maps to embedding_vectors ......")
            # 方式1（导出onnx失败）
            # embedding_vectors = train_outputs['layer1']
            # for layer_name in ['layer2', 'layer3']:
            #     embedding_vectors = embedding_concat(embedding_vectors, train_outputs[layer_name])
            # logger.info("merge embedding_vectors, and final size {}".format(embedding_vectors.shape))
            # 方式2（导出onnx失败）
            # embedding_vectors1 = train_outputs['layer1']
            # embedding_vectors2 = train_outputs['layer2']
            # embedding_vectors3 = train_outputs['layer3']
            # embedding_vectors1 = self.resize(embedding_vectors1)
            # embedding_vectors2 = self.resize(embedding_vectors2)
            # embedding_vectors3 = self.resize(embedding_vectors3)
            # embedding_vectors = torch.cat([embedding_vectors1, embedding_vectors2, embedding_vectors3], 1)
            # 方式3
            embedding_vectors1 = train_outputs['layer1']
            embedding_vectors2 = train_outputs['layer2']
            embedding_vectors3 = train_outputs['layer3']
            embedding_vectors2 = torch.nn.Upsample(scale_factor=2, mode='nearest')(embedding_vectors2)
            embedding_vectors3 = torch.nn.Upsample(scale_factor=4, mode='nearest')(embedding_vectors3)
            embedding_vectors = torch.cat([embedding_vectors1, embedding_vectors2, embedding_vectors3], 1)

            # randomly select d dimension
            logger.info("1.3 randomly select {} dimension".format(self.d_reduced))
            idx = torch.tensor(sample(range(0, embedding_vectors.shape[1]), self.d_reduced))
            embedding_vectors = torch.index_select(embedding_vectors, 1, idx)
            logger.info("embedding_vectors:{}".format(embedding_vectors.shape))

            # calculate multivariate Gaussian distribution
            logger.info("1.4 calculate multivariate Gaussian distribution")
            B, C, H, W = embedding_vectors.size()
            embedding_vectors = embedding_vectors.view(B, C, H * W)
            logger.info("embedding_vectors view:{}".format(embedding_vectors.shape))
            mean = torch.mean(embedding_vectors, dim=0).numpy()
            logger.info("cal mean:{}".format(mean.shape))
            cov = torch.zeros(C, C, H * W).numpy()
            I = np.identity(C)
            logger.info("cal cov .......")
            for i in range(H * W):
                cov[:, :, i] = np.cov(embedding_vectors[:, :, i].numpy(), rowvar=False) + 0.01 * I
            logger.info("cov:{}".format(cov.shape))

            # save learned distribution
            logger.info("1.5 save learned distribution")
            # 方式1，存储成python格式，C++读起来费劲
            # train_outputs = [mean, cov, np.asarray(idx)]
            # mean (550,3136)->(3136,550)一共56*56=3136个点，每个点有550个特征表示
            # std (550,550,3136)->(3136,550,550),一共56*56=3136个点，（550，550）表示协方差矩阵
            train_outputs = [mean.transpose(), cov.transpose(2,0,1), np.asarray(idx)]
            with open(train_feature_filepath, 'wb') as f:
                pickle.dump(train_outputs, f)

            # 方式2， 存储成txt格式，C++读起来方便
            trainMean_feature_filepath = os.path.join(output_dir, 'features_mean.txt')  # 特征存放路径
            with open(trainMean_feature_filepath, 'w') as f:
                for mean_one in mean.transpose().flatten():
                    f.write(str(mean_one)+"\n")
            trainStd_feature_filepath = os.path.join(output_dir, 'features_std.txt')  # 特征存放路径
            with open(trainStd_feature_filepath, 'w') as f:
                for std_one in cov.transpose(2,0,1).flatten():
                    f.write(str(std_one)+"\n")

        else:
            logger.info('load train set feature from: %s' % train_feature_filepath)
            with open(train_feature_filepath, 'rb') as f:
                train_outputs = pickle.load(f)

        self.train_output = train_outputs

    def evaluate(self, test_dataloader, output_dir=None):
        gt_list = []
        gt_mask_list = []
        test_imgs = []
        test_imgs_path = []
        self.test_outputs = []
        test_outputs = OrderedDict([('layer1', []), ('layer2', []), ('layer3', [])])

        # extract test set features
        logger.info("2.1 extract test set features")
        for i, (image, y, mask, image_path) in enumerate(test_dataloader):
            test_imgs.extend(image.cpu().detach().numpy())  # 所有图片
            gt_list.extend(y.cpu().detach().numpy())    # 是否是good
            gt_mask_list.extend(mask.cpu().detach().numpy())    # 所有mask
            test_imgs_path.extend(image_path)   # 图片路径
            # model prediction
            with torch.no_grad():
                _ = self.model(image.to(self.device))
            # get intermediate layer outputs
            for k, v in zip(test_outputs.keys(), self.outputs):
                test_outputs[k].append(v.cpu().detach())
            # initialize hook outputs
            self.outputs = []
        # 合并特征
        for k, v in test_outputs.items():
            test_outputs[k] = torch.cat(v, 0)

        # 将feature_maps 转为 embedding_vectors: torch.Size([200, 1792, 56, 56])
        logger.info("2.2 covert feature_maps to embedding_vectors ......")
        # 方式1（导出onnx失败）
        # embedding_vectors = test_outputs['layer1']
        # for layer_name in ['layer2', 'layer3']:
        #     embedding_vectors = embedding_concat(embedding_vectors, test_outputs[layer_name])
        # 方式2（导出onnx失败）
        # embedding_vectors1 = test_outputs['layer1']
        # embedding_vectors2 = test_outputs['layer2']
        # embedding_vectors3 = test_outputs['layer3']
        # embedding_vectors1 = self.resize(embedding_vectors1)
        # embedding_vectors2 = self.resize(embedding_vectors2)
        # embedding_vectors3 = self.resize(embedding_vectors3)
        # embedding_vectors = torch.cat([embedding_vectors1, embedding_vectors2, embedding_vectors3], 1)
        # 方式3
        embedding_vectors1 = test_outputs['layer1']
        embedding_vectors2 = test_outputs['layer2']
        embedding_vectors3 = test_outputs['layer3']
        embedding_vectors2 = torch.nn.Upsample(scale_factor=2, mode='nearest')(embedding_vectors2)
        embedding_vectors3 = torch.nn.Upsample(scale_factor=4, mode='nearest')(embedding_vectors3)
        embedding_vectors = torch.cat([embedding_vectors1, embedding_vectors2, embedding_vectors3], 1)

        logger.info("merge embedding_vectors, and final size {}".format(embedding_vectors.shape))

        # randomly select d dimension
        logger.info("2.3 randomly select {} dimension".format(self.d_reduced))
        embedding_vectors = torch.index_select(embedding_vectors, 1, torch.from_numpy(self.train_output[2]))

        # calculate distance matrix
        logger.info("2.4 calculate multivariate Gaussian distribution, this will take a minute ......")
        logger.info("this operate will use cpu, please Reserve sufficient resources ......")
        B, C, H, W = embedding_vectors.size()
        embedding_vectors = embedding_vectors.view(B, C, H * W).numpy()
        embedding_vectors = embedding_vectors.transpose(0, 2, 1)    # shape 改变
        dist_list = []
        for i in range(H * W):
            mean = self.train_output[0][i, :]
            conv_inv = np.linalg.inv(self.train_output[1][i, :, :])
            dist = [mahalanobis(sample[i, :], mean, conv_inv) for sample in embedding_vectors]
            dist_list.append(dist)

        dist_list = np.array(dist_list).transpose(1, 0).reshape(B, H, W)

        # upsample
        logger.info("2.5 upsample")
        dist_list = torch.tensor(dist_list)  # torch.Size([B, 56, 56])
        score_map = F.interpolate(dist_list.unsqueeze(1), size=self.image_size, mode='bilinear',
                                  align_corners=False).squeeze().numpy()    # (B, 224, 224)

        # apply gaussian smoothing on the score map
        logger.info("2.6 apply gaussian smoothing on the score map")
        for i in range(score_map.shape[0]):
            score_map[i] = gaussian_filter(score_map[i], sigma=4)

        # Normalization
        logger.info("2.6 Normalization")
        max_score = score_map.max()
        min_score = score_map.min()
        scores = (score_map - min_score) / (max_score - min_score)  # (B, 224, 224) scores是均值化后的结果

        # 以下是显示结果内容
        logger.info("2.7 result .......")
        fig, ax = plt.subplots(1, 2, figsize=(20, 10))  # 绘制ROC曲线
        fig_img_rocauc = ax[0]
        fig_pixel_rocauc = ax[1]

        # calculate image-level ROC AUC score
        logger.info("calculate image-level ROC AUC score")
        img_scores = scores.reshape(scores.shape[0], -1).max(axis=1)    # shape B
        gt_list = np.asarray(gt_list)   # shape B
        fpr, tpr, _ = roc_curve(gt_list, img_scores)
        img_roc_auc = roc_auc_score(gt_list, img_scores)
        logger.info('image ROCAUC: %.3f' % (img_roc_auc))
        fig_img_rocauc.plot(fpr, tpr, label='img_ROCAUC: %.3f' % (img_roc_auc))

        # calculate per-pixel level ROCAUC
        logger.info("calculate per-pixel level ROCAUC")
        gt_mask = np.where(np.asarray(gt_mask_list) != 0, 1, 0)  # (49, 1, 224, 224)
        fpr, tpr, _ = roc_curve(gt_mask.flatten(), scores.flatten())
        per_pixel_rocauc = roc_auc_score(gt_mask.flatten(), scores.flatten())
        logger.info('pixel ROCAUC: %.3f' % (per_pixel_rocauc))
        fig_pixel_rocauc.plot(fpr, tpr, label='ROCAUC: %.3f' % (per_pixel_rocauc))

        # 绘制ROC曲线，image-level&pixel-level
        save_dir = os.path.join(output_dir, "pictures")
        os.makedirs(save_dir, exist_ok=True)
        fig.tight_layout()
        fig.savefig(os.path.join(save_dir, 'roc_curve.png'), dpi=100)

        # get optimal threshold
        precision, recall, thresholds = precision_recall_curve(gt_mask.flatten(), scores.flatten())
        a = (1 + self.beta ** 2) * precision * recall
        b = self.beta ** 2 * precision + recall
        f1 = np.divide(a, b, out=np.zeros_like(a), where=b != 0)
        threshold = thresholds[np.argmax(f1)]

        # 绘制每张test图片预测信息
        mean = test_dataloader.dataset.mean
        std = test_dataloader.dataset.std
        plot_fig(test_imgs, scores, gt_mask_list, threshold, save_dir, test_imgs_path, mean, std)

        threshold_txt = os.path.join(output_dir, 'threshold.txt')
        with open(threshold_txt, 'w') as f:
            f.write(str(threshold) + "\n")
            f.write(str(max_score) + "\n")
            f.write(str(min_score) + "\n")


def plot_fig(test_img, scores, gts, threshold, save_dir, test_imgs_path, mean, std):
    """
    将test_img,scores,gts根据threshold绘制成图像，并保存到save_dir中
    :param test_img: test_imgs:[(3, 224, 224), ..., batchsize]
    :param scores:  scores: (batchsize, 224, 224)
    :param gts: gt_mask_list: [(1, 224, 224), ..., batchsize]
    :param threshold: float
    :param save_dir: str
    :param class_name: [img_path, ..., batchsize]
    :return:
    """
    num = len(scores)
    logger.info("number:{}".format(num))
    vmax = scores.max() * 255.
    vmin = scores.min() * 255.
    for i in range(num):
        img = test_img[i]
        img = denormalization(img, mean=mean, std=std)
        gt = gts[i].transpose(1, 2, 0).squeeze()  # .transpose(1, 2, 0)
        heat_map = scores[i] * 255
        mask = scores[i]
        mask[mask > threshold] = 1
        mask[mask <= threshold] = 0
        kernel = morphology.disk(4)
        mask = morphology.opening(mask, kernel)
        mask *= 255
        vis_img = mark_boundaries(img, mask, color=(1, 0, 0), mode='thick')
        fig_img, ax_img = plt.subplots(1, 5, figsize=(12, 3))
        fig_img.subplots_adjust(right=0.9)
        norm = matplotlib.colors.Normalize(vmin=vmin, vmax=vmax)
        for ax_i in ax_img:
            ax_i.axes.xaxis.set_visible(False)
            ax_i.axes.yaxis.set_visible(False)
        ax_img[0].imshow(img)
        ax_img[0].title.set_text('Image')
        ax_img[1].imshow(gt, cmap='gray')
        ax_img[1].title.set_text('GroundTruth')
        ax = ax_img[2].imshow(heat_map, cmap='jet', norm=norm)
        ax_img[2].imshow(img, cmap='gray', interpolation='none')
        ax_img[2].imshow(heat_map, cmap='jet', alpha=0.5, interpolation='none')
        ax_img[2].title.set_text('Predicted heat map')
        ax_img[3].imshow(mask, cmap='gray')
        ax_img[3].title.set_text('Predicted mask')
        ax_img[4].imshow(vis_img)
        ax_img[4].title.set_text('Segmentation result')
        left = 0.92
        bottom = 0.15
        width = 0.015
        height = 1 - 2 * bottom
        rect = [left, bottom, width, height]
        cbar_ax = fig_img.add_axes(rect)
        cb = plt.colorbar(ax, shrink=0.6, cax=cbar_ax, fraction=0.046)
        cb.ax.tick_params(labelsize=8)
        font = {
            'family': 'serif',
            'color': 'black',
            'weight': 'normal',
            'size': 8,
        }
        cb.set_label('Anomaly Score', fontdict=font)

        img_name = test_imgs_path[i].split("/")[-1][:-4]+".png"
        ngtype = test_imgs_path[i].split("/")[-2]
        fig_img.savefig(os.path.join(save_dir, "{}_".format(ngtype)+img_name), dpi=100)
        plt.close()


def denormalization(x, mean=[0.335782, 0.335782, 0.335782], std=[0.256730, 0.256730, 0.256730]):
    mean = np.array(mean)
    std = np.array(std)
    x = (((x.transpose(1, 2, 0) * std) + mean) * 255.).astype(np.uint8)

    return x


def embedding_concat(x, y):
    """
    将x,y嵌入连接，要求x,y同batchsize大小，通道数、长宽各不相同； 拼接成x形状即[B_x, C_(x+y), H_x, W_x)

    :param x:
    :param y:
    :return:
    """
    B, C1, H1, W1 = x.size()  # torch.Size([32, 256, 56, 56])
    _, C2, H2, W2 = y.size()  # torch.Size([32, 512, 28, 28])
    s = int(H1 / H2)    # x对于y的缩小步长
    x = F.unfold(x, kernel_size=s, dilation=1, stride=s)  # https://blog.csdn.net/qq_34914551/article/details/102940368 torch.Size([32, 256, 56, 56])-》torch.Size([32, 1024, 784])
    x = x.view(B, C1, -1, H2, W2)  # torch.Size([32, 256, 4, 28, 28])
    z = torch.zeros(B, C1 + C2, x.size(2), H2, W2)  # torch.Size([32, 768, 4, 28, 28])
    for i in range(x.size(2)):
        z[:, :, i, :, :] = torch.cat((x[:, :, i, :, :], y), 1)  # torch.cat((x[:, :, i, :, :], y), 1)=torch.Size([32, 768, 28, 28]), 其中x[:, :, i, :, :]torch.Size([32, 256, 28, 28])， ytorch.Size([32, 512, 28, 28])
    z = z.view(B, -1, H2 * W2)  # torch.Size([32, 3072, 784])
    z = F.fold(z, kernel_size=s, output_size=(H1, W1), stride=s)  # torch.Size([32, 768, 56, 56])

    return z


@Registers.anomaly_models.register
class PaDiM_demo(torch.nn.Module):
    def __init__(self, backbone, device=None, d_reduced: int = 100, total_dim=None, image_size=224, beta=1,select_index=None):
        super(PaDiM_demo, self).__init__()
        # backbone load model
        if backbone.type == 'resnet18':
            self.model = resnet18(pretrained=True, progress=True)
        elif backbone.type == 'wide_resnet50_2':
            self.model = wide_resnet50_2(pretrained=True, progress=True)
        self.model.to(device)
        self.model.eval()
        # set model's intermediate outputs
        self.outputs = []
        def hook(module, input, output):
            self.outputs.append(output)
        self.model.layer1[-1].register_forward_hook(hook)
        self.model.layer2[-1].register_forward_hook(hook)
        self.model.layer3[-1].register_forward_hook(hook)

        self.image_size = image_size
        self.d_reduced = d_reduced  # your RAM will thank you   选取维度
        self.t_d = total_dim    # 总维度特征
        self.beta = beta
        self.device = device
        self.select_index = select_index

        self.resize = torch.nn.AdaptiveAvgPool2d(int(image_size/4))

    def forward(self, x):
        # logger.info("2.1 extract test set features")
        test_outputs = OrderedDict([('layer1', []), ('layer2', []), ('layer3', [])])    # 存储结果输出
        for img in x:
            image_demo = torch.tensor(img[0]).unsqueeze(0)
            with torch.no_grad():
                _ = self.model(image_demo.to(self.device))
            # get intermediate layer outputs
            for k, v in zip(test_outputs.keys(), self.outputs):
                test_outputs[k].append(v.cpu().detach())
            # initialize hook outputs
            self.outputs = []

        # 合并
        for k, v in test_outputs.items():
            test_outputs[k] = torch.cat(v, 0)

        # logger.info("2.2 covert feature_maps to embedding_vectors ......")
        # 方式1（导出onnx失败）
        # embedding_vectors = test_outputs['layer1']
        # for layer_name in ['layer2', 'layer3']:
        #     embedding_vectors = embedding_concat(embedding_vectors, test_outputs[layer_name])
        # 方式2（导出onnx失败）
        # embedding_vectors1 = test_outputs['layer1']
        # embedding_vectors2 = test_outputs['layer2']
        # embedding_vectors3 = test_outputs['layer3']
        # embedding_vectors1 = self.resize(embedding_vectors1)
        # embedding_vectors2 = self.resize(embedding_vectors2)
        # embedding_vectors3 = self.resize(embedding_vectors3)
        # embedding_vectors = torch.cat([embedding_vectors1, embedding_vectors2, embedding_vectors3], 1)
        # 方式3
        embedding_vectors1 = test_outputs['layer1']
        embedding_vectors2 = test_outputs['layer2']
        embedding_vectors3 = test_outputs['layer3']
        embedding_vectors2 = torch.nn.Upsample(scale_factor=2, mode='nearest')(embedding_vectors2)
        embedding_vectors3 = torch.nn.Upsample(scale_factor=4, mode='nearest')(embedding_vectors3)
        embedding_vectors = torch.cat([embedding_vectors1, embedding_vectors2, embedding_vectors3], 1)

        # logger.info("merge embedding_vectors, and final size {}".format(embedding_vectors.shape))

        # randomly select d dimension
        logger.info("2.3 randomly select {} dimension".format(self.d_reduced))
        embedding_vectors = torch.index_select(embedding_vectors, 1, torch.from_numpy(self.select_index))

        return embedding_vectors


@Registers.anomaly_models.register
class PaDiM_export(torch.nn.Module):
    def __init__(self, backbone, device=None, d_reduced: int = 100, total_dim=None, image_size=224, beta=1,select_index=None):
        super(PaDiM_export, self).__init__()
        # backbone load model
        if backbone.type == 'resnet18':
            self.model = resnet18(pretrained=True, progress=True)
        elif backbone.type == 'wide_resnet50_2':
            self.model = wide_resnet50_2(pretrained=True, progress=True)
        self.model.to(device)
        self.model.eval()
        # set model's intermediate outputs
        self.outputs = []
        self.test_outputs = OrderedDict([('layer1', []), ('layer2', []), ('layer3', [])])    # 存储结果输出
        def hook(module, input, output):
            self.outputs.append(output)
        self.model.layer1[-1].register_forward_hook(hook)
        self.model.layer2[-1].register_forward_hook(hook)
        self.model.layer3[-1].register_forward_hook(hook)

        self.image_size = image_size
        self.d_reduced = d_reduced  # your RAM will thank you   选取维度
        self.t_d = total_dim    # 总维度特征
        self.beta = beta
        self.device = device
        self.select_index = torch.from_numpy(select_index)

        # self.resize = torch.nn.AdaptiveAvgPool2d(int(image_size/4))
        self.resize = MyAdaptiveAvgPool2d(output_size=56)

    def forward(self, x):
        # logger.info("2.1 extract test set features")
        with torch.no_grad():
            _ = self.model(x.to(self.device))

        # 1、只导出resnet输出
        # return self.outputs[0], self.outputs[1], self.outputs[2]

        # 2、导出embedding vectors
        # 方式1（导出onnx失败）
        # 以下内容是返回embedding vectors，onnx不支持所以只导出resnet----这个是原来合并embedding vector方法时使用的
        # # get intermediate layer outputs
        # for k, v in zip(self.test_outputs.keys(), self.outputs):
        #     self.test_outputs[k].append(v.cpu().detach())
        #
        # # 合并
        # for k, v in self.test_outputs.items():
        #     self.test_outputs[k] = torch.cat(v, 0)
        #
        # # logger.info("2.2 covert feature_maps to embedding_vectors ......")
        # embedding_vectors = self.test_outputs['layer1']
        # for layer_name in ['layer2', 'layer3']:
        #     embedding_vectors = embedding_concat(embedding_vectors, self.test_outputs[layer_name])
        #
        # # logger.info("merge embedding_vectors, and final size {}".format(embedding_vectors.shape))
        #
        # # randomly select d dimension
        # embedding_vectors = torch.index_select(embedding_vectors, 1, self.select_index)
        #
        # return embedding_vectors

        # 使用方式2： 新的合并embedding vectors（导出onnx失败）
        # embedding_vectors1 = self.resize(self.outputs[0])
        # embedding_vectors2 = self.resize(self.outputs[1])
        # embedding_vectors3 = self.resize(self.outputs[2])
        # embedding_vectors = torch.cat([embedding_vectors1, embedding_vectors2, embedding_vectors3], 1)
        # return embedding_vectors

        # 方式3
        self.outputs[1] = torch.nn.Upsample(scale_factor=2, mode='nearest')(self.outputs[1])
        self.outputs[2] = torch.nn.Upsample(scale_factor=4, mode='nearest')(self.outputs[2])
        embedding_vectors = torch.cat(self.outputs, 1)

        # randomly select d dimension
        # logger.info("2.3 randomly select {} dimension".format(self.d_reduced))
        embedding_vectors = torch.index_select(embedding_vectors, 1, self.select_index)
        embedding_vectors = torch.transpose(
            embedding_vectors.reshape(
                embedding_vectors.shape[0],
                embedding_vectors.shape[1],
                embedding_vectors.shape[2] * embedding_vectors.shape[3]
            ), 2, 1)    # (B, 550,56,56)->(B, 3136,550)
        return embedding_vectors
