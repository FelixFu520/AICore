# -*- coding: utf-8 -*-
# @Author:FelixFu
# @Date: 2021.4.14
# @GitHub:https://github.com/felixfu520
# @Copy From:https://blog.csdn.net/u014687517/article/details/90216095

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.modules.loss import _Loss

from dao.register import Registers


# Dice系数
def diceCoeff(pred, gt, epsilon=1, activation='sigmoid'):
    r""" computational formula：
        dice = (2 * (pred ∩ gt)) / (pred ∪ gt)
    """
    if activation is None or activation == "none":
        activation_fn = lambda x: x
    elif activation == "sigmoid":
        activation_fn = nn.Sigmoid()
    elif activation == "softmax2d":
        activation_fn = nn.Softmax2d()
    else:
        raise NotImplementedError("Activation implemented for sigmoid and softmax2d 激活函数的操作")

    pred = activation_fn(pred)

    N = gt.size(0)
    pred_flat = pred.view(N, -1)
    gt_flat = gt.view(N, -1)

    intersection = (pred_flat * gt_flat).sum(1)
    unionset = pred_flat.sum(1) + gt_flat.sum(1)
    loss = (2 *intersection + epsilon) / (unionset + epsilon)

    return loss.sum() / N


@Registers.losses.register
class TverskyLoss(_Loss):
    __name__ = 'tversky_loss'

    def __init__(self, alpha=0.4, beta=0.6, activation='sigmoid'):
        super(TverskyLoss, self).__init__()
        self.alpha = alpha
        self.beta = beta
        self.activation = activation

    def forward(self, y_pred, y_true):
        assert y_pred.size() == y_true.size(), "the size of predict and target must be equal."
        batch_size = y_pred.size(0)
        loss = 0

        for i in range(batch_size):
            prob = y_pred[i]
            ref = y_true[i]

            tp = (ref * prob).sum()
            fp = ((1 - ref) * prob).sum()
            fn = (ref * (1 - prob)).sum()
            tversky = tp / (tp + self.alpha * fp + self.beta * fn)
            loss = loss + (1 - tversky)
        return loss / batch_size


if __name__ == "__main__":
    N, C, H, W = 2, 3, 4, 4
    loss = TverskyLoss()
    # 1. 测试分割
    input = torch.randn((N, C, H, W), requires_grad=True)
    target = torch.empty((N, C, H, W)).random_(C)
    output = loss(input, target)
    # print("input:{}".format(input))
    # print("target:{}".format(target))
    print("output:{}".format(output))
    torch.mean(output).backward()

    # 3. 测试分割(自定义数据)
    input = [[
        [[0, 1, 1, 0],
         [1, 0, 0, 1],
         [1, 0, 0, 1],
         [0, 1, 1, 0]],
        [[0, 0, 0, 0],
         [0, 0, 0, 0],
         [0, 0, 0, 0],
         [0, 0, 0, 0]],
        [[1, 0, 0, 1],
         [0, 1, 1, 0],
         [0, 1, 1, 0],
         [1, 0, 0, 1]]]]
    target = [[
        [[0, 1, 1, 0],
         [1, 0, 0, 1],
         [1, 0, 0, 1],
         [0, 1, 1, 0]],
        [[0, 0, 0, 0],
         [0, 0, 0, 0],
         [0, 0, 0, 0],
         [0, 0, 0, 0]],
        [[1, 0, 0, 1],
         [0, 1, 1, 0],
         [0, 1, 1, 0],
         [1, 0, 0, 1]]]]
    input = torch.tensor(input, dtype=torch.float32, requires_grad=True)
    target = torch.tensor(target, dtype=torch.float32)
    output = loss(input, target)
    # print("input:{}".format(input))
    # print("target:{}".format(target))
    print("output:{}".format(output))
    torch.mean(output).backward()

