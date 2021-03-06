# -*- coding: utf-8 -*-
# @Author:FelixFu
# @Date: 2021.12.17
# @GitHub:https://github.com/felixfu520
# @Copy From:

import os   # 导入系统相关库
import shutil
import time
import json
import datetime
import numpy as np
from PIL import Image
from loguru import logger
import cv2
import random

import torch    # 深度学习相关库
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.utils.tensorboard import SummaryWriter
from torchsummary import summary

from dao.register import Registers
from dao.dataloaders.augments import get_transformer, get_transformerYOLO
from dao.utils import (       # 导入Train util库
    setup_logger,       # 日志设置
    load_ckpt,          # 加载ckpt
    save_checkpoint,    # 存储ckpt
    occupy_mem,         # 占据显存
    gpu_mem_usage,      # 显存使用情况
    EMA,                # 指数移动平均
    is_parallel,        # 是否时多卡模型
    MeterSegTrain,      # 训练评价指标
    MeterDetEval, MeterDetTrain,
    denormalization,    # 反归一化
    get_palette,        # 获得画板颜色,颜色版共num_classes
    colorize_mask,      # 为mask图，填充颜色
    synchronize,        # 同步所有进程(GPU)
    DataPrefetcherDet,  # 数据预加载
    all_reduce_norm,    # BN 参数进行多卡同步
    get_rank, get_local_rank, get_world_size,  # 导入分布式库
    multi_gt_creator
)


@Registers.trainers.register
class DetTrainer:
    def __init__(self, exp, parser):
        self.exp = exp  # DotMap 格式 的配置文件
        self.parser = parser  # 命令行配置文件

        self.start_time = datetime.datetime.now().strftime('%m-%d_%H-%M')  # 此次trainer的开始时间
        self.data_type = torch.float16 if self.parser.fp16 else torch.float32  # 使用的数据类型
        assert self.data_type == torch.float32, \
            logger.error("ObjectDetection dataType must be float32, because fp16 don't mplementation")
        self.scaler = torch.cuda.amp.GradScaler(enabled=self.parser.amp)  # 在训练开始之前实例化一个Grad Scaler对象

    def run(self):
        self._before_train()
        # epochs
        for self.epoch in range(self.start_epoch, self.max_epoch):
            self._before_epoch()
            # iters
            for self.iter in range(self.max_iter):
                self._before_iter()
                self._train_one_iter()
                self._after_iter()
            self._after_epoch()
        self._after_train()

    def _before_train(self):
        """
        1.Logger Setting
        2.Model Setting;
        3.Optimizer Setting;
        4.Resume setting;
        5.DataLoader Setting;
        6.Loss Setting;
        7.Scheduler Setting;
        8.Evaluator Setting;
        """
        if self.parser.record:
            self.output_dir = os.path.join(self.exp.trainer.log_dir, self.exp.name, self.start_time)    # 日志目录
        else:
            self.output_dir = os.path.join(self.exp.trainer.log_dir, self.exp.name)  # 日志目录
            if get_rank() == 0:
                if os.path.exists(self.output_dir):  # 如果存在self.output_dir删除
                    try:
                        shutil.rmtree(self.output_dir)
                    except Exception as e:
                        logger.info("global rank {} can't remove tree {}".format(get_rank(), self.output_dir))
        setup_logger(self.output_dir, distributed_rank=get_rank(), filename=f"train_log.txt", mode="a")  # 设置只有rank=0输出日志，并重定向
        logger.info("....... Train Before, Setting something ...... ")
        logger.info("1. Logging Setting ...")
        logger.info(f"create log file {self.output_dir}/train_log.txt")  # log txt
        self.exp.pprint(pformat='json') if self.parser.detail else None  # 根据parser.detail来决定日志输出的详细
        with open(os.path.join(self.output_dir, 'config.json'), 'w') as f:    # 将配置文件写到self.output_dir
            json.dump(dict(self.exp), f)
        logger.info(f"create Tensorboard log {self.output_dir}")
        self.tblogger = SummaryWriter(self.output_dir) if get_rank() == 0 else None  # log tensorboard

        logger.info("2. Model Setting ...")
        torch.cuda.set_device(get_local_rank())
        model = Registers.det_models.get(self.exp.model.type)(**self.exp.model.kwargs)
        summary(model, input_size=(3, 416, 416), device="cpu") if self.parser.detail else None  # log torchsummary model
        logger.info("\n{}".format(model)) if self.parser.detail else None  # log model structure
        model.to("cuda:{}".format(get_local_rank()))    # model to self.device

        logger.info("3. Optimizer Setting")
        self.optimizer = Registers.optims.get(self.exp.optimizer.type)(model=model, **self.exp.optimizer.kwargs)

        logger.info("4. Resume/FineTuning Setting ...")
        model = self._resume_train(model)

        logger.info("5. Dataloader Setting ... ")
        self.max_epoch = self.exp.trainer.max_epochs
        self.no_aug = self.start_epoch >= self.max_epoch - self.exp.trainer.no_aug_epochs
        train_loader = Registers.dataloaders.get(self.exp.dataloader.type)(
            is_distributed=get_world_size() > 1,
            dataset=self.exp.dataloader.dataset,
            seed=self.parser.seed,
            **self.exp.dataloader.kwargs
        )
        self.max_iter = len(train_loader)
        logger.info("init prefetcher, this might take one minute or less...")
        # to solve https://github.com/pytorch/pytorch/issues/11201
        torch.multiprocessing.set_sharing_strategy('file_system')
        self.train_loader = DataPrefetcherDet(train_loader, device="cuda:{}".format(get_local_rank()))
        # self.train_loader = DataPrefetcherDet(train_loader)

        logger.info("6. Loss Setting ... ")
        logger.info("Yolo loss in Model!!!!")

        logger.info("7. Scheduler Setting ... ")
        self.lr_scheduler = Registers.schedulers.get(self.exp.lr_scheduler.type)(
            lr=self.exp.optimizer.kwargs.lr,
            iters_per_epoch=self.max_iter,
            total_epochs=self.exp.trainer.max_epochs,
            **self.exp.lr_scheduler.kwargs
        )

        logger.info("8. Other Setting ... ")
        logger.info("occupy mem")
        if self.parser.occupy:
            occupy_mem(get_local_rank())

        logger.info("Model DDP Setting")
        if get_world_size() > 1:
            model = DDP(model, device_ids=[get_local_rank()], broadcast_buffers=False, output_device=[get_local_rank()])

        logger.info("Model EMA Setting")
        # Exponential moving average
        # 用EMA方法对模型的参数做平均，以提高测试指标并增加模型鲁棒性（减少模型权重抖动）
        self.use_model_ema = self.parser.ema
        if self.use_model_ema:
            # self.ema_model = ModelEMA(model, 0.9998)
            # self.ema_model.updates = self.max_iter * self.start_epoch
            self.ema_model = EMA(model, 0.9998)
            self.ema_model.register()

        self.model = model
        self.model.train()

        logger.info("9. Evaluator Setting ... ")
        self.evaluator = Registers.evaluators.get(self.exp.evaluator.type)(
            is_distributed=get_world_size() > 1,
            dataloader=self.exp.evaluator.dataloader,
            num_classes=self.exp.model.kwargs.num_classes,
        )
        self.train_metrics = MeterDetTrain()
        self.best_acc = 0
        logger.info("Setting finished, training start ......")

    def _before_epoch(self):
        """
        Function: 每次epoch前的操作
            例如multi-scale， 马赛克增强，等取消与否。
        :return:
        """
        logger.info("---> start train epoch{}".format(self.epoch + 1))

    def _before_iter(self):
        pass

    def _train_one_iter(self):
        iter_start_time = time.time()

        images, labels, paths = self.train_loader.next()
        # # show img and mask
        # cv_image = denormalization(images[0].cpu().numpy(),[0.45289162, 0.43158466, 0.3984241], [0.2709828, 0.2679657, 0.28093508])    # 注意mean和std要和config.json中的一致
        # height, width, _ = cv_image.shape
        # label = labels[0].cpu().numpy()
        # cv_image = cv2.cvtColor(cv_image, cv2.COLOR_RGB2BGR)
        # for bbox in label:
        #     xmin = round(bbox[0] * width)
        #     ymin = round(bbox[1] * height)
        #     xmax = round(bbox[2] * width)
        #     ymax = round(bbox[3] * height)
        #     class_id = int(bbox[4])
        #     if xmax <= xmin or ymax <= ymin:
        #         logger.error("No bbox")
        #         continue
        #     cv2.rectangle(cv_image, (xmin, ymin), (xmax, ymax), (0, 255, 0), 4)
        #
        #     font = cv2.FONT_HERSHEY_SIMPLEX
        #     cv2.putText(cv_image,  self.train_loader.dataset.labels_id_name[str(class_id)], (xmin, ymin), font, 1, (0, 0, 255), 1)
        # cv2.imwrite("/ai/data/{}".format(paths[0].split('/')[-1]), cv_image)

        # 读取images和bboxes_labels
        inps = images.to(self.data_type)
        targets = labels.to(self.data_type)

        # multi-scale trick
        if (self.iter+1) % self.exp.trainer.log_per_iter == 0 and self.exp.trainer.multi_scale:
            # randomly choose a new size
            self.train_size = random.randint(self.exp.trainer.multiscale_range[0], self.exp.trainer.multiscale_range[1]) * 32
            # interpolate
            inps = torch.nn.functional.interpolate(inps, size=self.train_size, mode='bilinear', align_corners=False)
        else:
            self.train_size = inps[0].shape[1]
        data_end_time = time.time()

        with torch.cuda.amp.autocast(enabled=self.parser.amp):    # 开启auto cast的context manager语义（model+loss）
            loss, outputs = self.model(inps, targets)

        self.optimizer.zero_grad()   # 梯度清零
        self.scaler.scale(loss).backward()   # 反向传播；Scales loss. 为了梯度放大
        # scaler.step() 首先把梯度的值unscale回来.
        # 如果梯度的值不是infs或者NaNs, 那么调用optimizer.step()来更新权重,
        # 否则，忽略step调用，从而保证权重不更新（不被破坏）
        self.scaler.step(self.optimizer)    # optimizer.step 进行参数更新
        self.scaler.update()    # 准备着，看是否要增大scaler

        if self.use_model_ema:
            # self.ema_model.update(self.model)
            self.ema_model.update()
        lr = self.lr_scheduler.update_lr(self.progress_in_iter + 1)
        for param_group in self.optimizer.param_groups:
            param_group["lr"] = lr

        iter_end_time = time.time()
        self.train_metrics.update_metrics(
            data_time=data_end_time - iter_start_time,
            batch_time=iter_end_time - iter_start_time,
            lr=lr,
            total_loss=loss.item()
        )

    def _after_iter(self):
        """
        Function: iter后日志输出
        `after_iter` contains two parts of logic:
            * log information
            * reset setting of resize
        """
        if (self.iter + 1) % self.exp.trainer.log_per_iter == 0 and get_rank() == 0:
            # 剩余时间（不包括evaluator过程），并获得输出str
            left_iters = self.max_iter * self.max_epoch - (self.progress_in_iter + 1)
            eta_seconds = (self.train_metrics.batch_time.avg + self.train_metrics.data_time.avg) * left_iters
            eta_str = "ETA: {}".format(datetime.timedelta(seconds=int(eta_seconds)))

            # 单次iter时间
            time_str = "iter time:{:2f}, data time:{:2f}".format(self.train_metrics.batch_time.avg, self.train_metrics.data_time.avg)

            # 损失str
            loss_str = "total_loss:{:2f}".format(self.train_metrics.total_loss.avg)

            # 迭代次数str
            progress_str = f"epoch: {self.epoch + 1}/{self.max_epoch}, iter: {self.iter + 1}/{self.max_iter} "

            # 输出日志
            logger.info(
                "{}, {}, mem: {:.0f}Mb, {}, {}, lr: {:.3e}, {}".format(
                    progress_str,
                    "Size:{}".format(self.train_size),
                    gpu_mem_usage(),
                    time_str,
                    loss_str,
                    self.train_metrics.lr,
                    eta_str
                )
            )
            self.tblogger.add_scalar('train/total_loss', self.train_metrics.total_loss.avg, self.progress_in_iter)
            self.tblogger.add_scalar('train/lr', self.train_metrics.lr, self.progress_in_iter)
            for i, layer_i in enumerate(self.model.metrics):
                for k, v in layer_i.items():
                    self.tblogger.add_scalar("train/loss_layer_{}/{}".format(i, k), round(v, 4))
            self.train_metrics.reset_metrics()

    def _after_epoch(self):
        self._save_ckpt(ckpt_name="latest")

        if (self.epoch + 1) % self.exp.trainer.eval_interval == 0:
            all_reduce_norm(self.model)
            self._evaluate_and_save_model()

    def _after_train(self):
        logger.info("Training of experiment is done and the best Acc is {:.2f}".format(self.best_acc))
        logger.info("DONE")

    def _evaluate_and_save_model(self):
        if self.use_model_ema:
            # evalmodel = self.ema_model.ema
            evalmodel = self.ema_model.model
        else:
            evalmodel = self.model
            if is_parallel(evalmodel):
                evalmodel = evalmodel.module
        # set eval mode
        evalmodel.trainable = False
        evalmodel.eval()
        mAP, aps = self.evaluator.evaluate(evalmodel, get_world_size() > 1, device="cuda:{}".format(get_local_rank()),
                                           output_dir=self.output_dir)

        self.model.train()

        logger.info("mAP:{}, APs:{}".format(mAP, aps))

        # if get_rank() == 0:
        #     self.tblogger.add_scalar("val/mAP", mAP, self.epoch + 1)
        #     for k, v in aps.items():
        #         self.tblogger.add_scalar("val_detail/{} AP".format(k), v, self.epoch + 1)

        synchronize()
        self._save_ckpt("last_epoch", mAP > self.best_acc)
        self.best_acc = max(self.best_acc, mAP)

    def _save_ckpt(self, ckpt_name, update_best_ckpt=False):
        if get_rank() == 0:
            # save_model = self.ema_model.ema if self.use_model_ema else self.model
            save_model = self.ema_model.model if self.use_model_ema else self.model
            logger.info("Save weights to {} - update_best_ckpt:{}".format(self.output_dir, update_best_ckpt))
            ckpt_state = {
                "start_epoch": self.epoch + 1,
                "model": save_model.state_dict(),
                "optimizer": self.optimizer.state_dict(),
            }
            save_checkpoint(
                ckpt_state,
                update_best_ckpt,
                self.output_dir,
                ckpt_name,
            )

    def _resume_train(self, model):
        """
        如果args.resume为true，将args.ckpt权重resume；
        如果args.resume为false，将args.ckpt权重fine turning；
        :param model:
        :return:
        """
        if self.exp.trainer.resume:
            logger.info("resume training")
            # 获取ckpt路径
            assert self.exp.trainer.ckpt is not None
            ckpt_file = self.exp.trainer.ckpt
            # 加载ckpt
            ckpt = torch.load(ckpt_file, map_location="cuda:{}".format(get_local_rank()))
            # resume the model/optimizer state dict
            model.load_state_dict(ckpt["model"])
            self.optimizer.load_state_dict(ckpt["optimizer"])
            # resume the training states variables
            self.start_epoch = ckpt["start_epoch"]
            logger.info(
                "loaded checkpoint '{}' (epoch {})".format(self.exp.trainer.ckpt, self.start_epoch)
            )  # noqa
        else:
            if self.exp.trainer.ckpt is not None:
                logger.info("loading checkpoint for fine tuning")
                ckpt_file = self.exp.trainer.ckpt
                ckpt = torch.load(ckpt_file, map_location="cuda:{}".format(get_local_rank()))["model"]
                model = load_ckpt(model, ckpt)
            self.start_epoch = 0

        return model

    @property
    def progress_in_iter(self):
        return self.epoch * self.max_iter + self.iter


@Registers.trainers.register
class DetEval:
    def __init__(self, exp, parser):
        self.exp = exp  # DotMap 格式 的配置文件
        self.parser = parser  # 命令行配置文件

        self.start_time = datetime.datetime.now().strftime('%m-%d_%H-%M')   # 此次trainer的开始时间

    def run(self):
        self._before_eval()
        pixAcc, mIoU, Class_IoU_dict = self.evaluator.evaluate(self.model,
                                                               get_world_size() > 1,
                                                               device="cuda:{}".format(get_local_rank()),
                                                               output_dir=self.output_dir,
                                                               save_pic=True
                                                               )
        logger.info("pixACC:{}\nmIoU:{}\nClass_IoU_dict:{}".format(pixAcc, mIoU, Class_IoU_dict))
        with open(os.path.join(self.output_dir, "result.txt"), 'w', encoding='utf-8') as result_file:
            result_file.write("pixACC:{}\nmIoU:{}\nClass_IoU_dict:{}".format(pixAcc, mIoU, Class_IoU_dict))
        logger.info("DONE")

    def _before_eval(self):
        """
        1.Logger Setting
        2.Model Setting;
        3.Evaluator Setting;
        """
        if self.parser.record:
            self.output_dir = os.path.join(self.exp.trainer.log_dir, self.exp.name, self.start_time)  # 日志目录
        else:
            self.output_dir = os.path.join(self.exp.trainer.log_dir, self.exp.name)    # 日志目录
            if os.path.exists(self.output_dir):  # 如果存在self.output_dir删除
                shutil.rmtree(self.output_dir)
        setup_logger(self.output_dir, distributed_rank=0, filename=f"val_log.txt",
                     mode="a")  # 设置只有rank=0输出日志，并重定向，单卡模式
        logger.info("....... Eval Before, Setting something ...... ")
        logger.info("1. Logging Setting ...")
        logger.info(f"create log file {self.output_dir}/eval_log.txt")  # log txt
        self.exp.pprint(pformat='json') if self.parser.detail else None  # 根据parser.detail来决定日志输出的详细
        with open(os.path.join(self.output_dir, 'config.json'), 'w') as f:  # 将配置文件写到self.output_dir
            json.dump(dict(self.exp), f)

        logger.info("2. Model Setting ...")
        torch.cuda.set_device(self.parser.gpu)
        model = Registers.seg_models.get(self.exp.model.type)(self.exp.model.backbone, **self.exp.model.kwargs)
        logger.info("\n{}".format(model)) if self.parser.detail else None  # log model structure
        summary(model, input_size=(3, 224, 224), device="cpu") if self.parser.detail else None  # log torchsummary model
        model.to("cuda:{}".format(self.parser.gpu))  # model to self.device

        ckpt_file = self.exp.trainer.ckpt
        ckpt = torch.load(ckpt_file, map_location="cuda:{}".format(get_local_rank()))["model"]
        model = load_ckpt(model, ckpt)

        logger.info("Model DDP Setting")
        if get_world_size() > 1:
            model = DDP(model, device_ids=[get_local_rank()], broadcast_buffers=False, output_device=[get_local_rank()])

        self.model = model
        self.model.eval()

        logger.info("9. Evaluator Setting ... ")
        self.evaluator = Registers.evaluators.get(self.exp.evaluator.type)(
            is_distributed=get_world_size() > 1,
            dataloader=self.exp.evaluator.dataloader,
            num_classes=self.exp.model.kwargs.num_classes
        )
        logger.info("Setting finished, eval start ......")


@Registers.trainers.register
class DetDemo:
    def __init__(self, exp, parser):
        self.exp = exp  # DotMap 格式 的配置文件
        self.parser = parser  # 命令行配置文件

        self.start_time = datetime.datetime.now().strftime('%m-%d_%H-%M')  # 此次trainer的开始时间

        # 因为SegDemo只支持单机单卡
        assert self.parser.devices == 1, "exp.envs.gpus.devices must 1, please set again "
        assert self.parser.num_machines == 1, "exp.envs.gpus.devices must 1, please set again "
        assert self.parser.machine_rank == 0, "exp.envs.gpus.devices must 0, please set again "

    def run(self):
        self._before_demo()
        results = []
        for image, shape, img_p in self.images:
            image = torch.tensor(image).unsqueeze(0)  # 1, c, h, w
            image = image.to(device="cuda:{}".format(self.parser.gpu))  # 1, c, h, w
            output = self.model(image)
            output = np.uint8(output.data.max(1)[1].cpu().numpy()[0])
            output = colorize_mask(output, get_palette(self.exp.model.kwargs.num_classes))
            output = output.resize((shape[1], shape[0]))
            results.append((output, img_p))
        os.makedirs(os.path.join(self.output_dir, "pictures"), exist_ok=True)
        for i, (image, img_p) in enumerate(results):
            shutil.copy(img_p, os.path.join(self.output_dir, "pictures", os.path.basename(img_p)[:-4]+".jpg"))
            image.save(os.path.join(self.output_dir, "pictures", os.path.basename(img_p)[:-4]+".png"))
        logger.info("DONE")

    def _before_demo(self):
        """
        1.Logger Setting
        2.Model Setting;
        """
        if self.parser.record:
            self.output_dir = os.path.join(self.exp.trainer.log_dir, self.exp.name, self.start_time)  # 日志目录
        else:
            self.output_dir = os.path.join(self.exp.trainer.log_dir, self.exp.name)  # 日志目录
            if os.path.exists(self.output_dir):  # 如果存在self.output_dir删除
                shutil.rmtree(self.output_dir)
        setup_logger(self.output_dir, distributed_rank=0, filename=f"demo_log.txt", mode="a")  # 输出日志重定向
        logger.info("....... Demo Before, Setting something ...... ")
        logger.info("1. Logging Setting ...")
        logger.info(f"create log file {self.output_dir}/demo_log.txt")  # log txt
        self.exp.pprint(pformat='json') if self.parser.detail else None  # 根据parser.detail来决定日志输出的详细
        with open(os.path.join(self.output_dir, 'config.json'), 'w') as f:  # 将配置文件写到self.output_dir
            json.dump(dict(self.exp), f)

        logger.info("2. Model Setting ...")
        torch.cuda.set_device(self.parser.gpu)
        model = Registers.seg_models.get(self.exp.model.type)(self.exp.model.backbone, **self.exp.model.kwargs)
        logger.info("\n{}".format(model)) if self.parser.detail else None  # log model structure
        summary(model, input_size=(3, 224, 224), device="cpu") if self.parser.detail else None  # log torchsummary model
        model.to("cuda:{}".format(self.parser.gpu))  # model to self.device

        ckpt_file = self.exp.trainer.ckpt
        ckpt = torch.load(ckpt_file, map_location="cuda:{}".format(self.parser.gpu))["model"]
        self.model = load_ckpt(model, ckpt)
        self.model.eval()

        self.images = self._get_images()  # ndarray

    def _img_ok(self, img_p):
        flag = False
        for m in self.exp.images.image_ext:
            if img_p.endswith(m):
                flag = True
        return flag

    def _get_images(self):
        results = []
        all_paths = []
        all_p = [p for p in os.listdir(self.exp.images.path) if self._img_ok(p)]
        for p in all_p:
            all_paths.append(os.path.join(self.exp.images.path, p))

        for img_p in all_paths:
            image = np.array(Image.open(img_p))  # h,w
            if len(image.shape) == 2:
                image = np.expand_dims(image, axis=2)  # h,w,1
            shape = image.shape
            transform = get_transformer(self.exp.images.transforms.kwargs)
            image = transform(image=image)['image']
            image = image.transpose(2, 0, 1)  # c, h, w
            results.append((image, shape, img_p))
        return results


@Registers.trainers.register
class DetExport:
    def __init__(self, exp, parser):
        self.exp = exp  # DotMap 格式 的配置文件
        self.parser = parser  # 命令行配置文件

        self.start_time = datetime.datetime.now().strftime('%m-%d_%H-%M')  # 此次trainer的开始时间

        # 因为ClsExport只支持单机单卡
        assert self.parser.devices == 1, "devices must 1, please set again "
        assert self.parser.num_machines == 1, "num_machines must 1, please set again "
        assert self.parser.machine_rank == 0, "machine_rank must 0, please set again "

    def _before_export(self):
        """
        1.Logger Setting
        2.Model Setting;
        """
        if self.parser.record:
            self.output_dir = os.path.join(self.exp.trainer.log_dir, self.exp.name, self.start_time)  # 日志目录
        else:
            self.output_dir = os.path.join(self.exp.trainer.log_dir, self.exp.name)  # 日志目录
            if os.path.exists(self.output_dir):  # 如果存在self.output_dir删除
                shutil.rmtree(self.output_dir)
        setup_logger(self.output_dir, distributed_rank=0, filename=f"export_log.txt",
                     mode="a")  # 设置只有rank=0输出日志，并重定向
        logger.info("....... Export Before, Setting something ...... ")
        logger.info("1. Logging Setting ...")
        logger.info(f"create log file {self.output_dir}/export_log.txt")  # log txt
        self.exp.pprint(pformat='json') if self.parser.detail else None  # 根据parser.detail来决定日志输出的详细
        with open(os.path.join(self.output_dir, 'config.json'), 'w') as f:  # 将配置文件写到self.output_dir
            json.dump(dict(self.exp), f)

        logger.info("2. Model Setting ...")
        model = Registers.seg_models.get(self.exp.model.type)(self.exp.model.backbone, **self.exp.model.kwargs)
        logger.info("\n{}".format(model)) if self.parser.detail else None  # log model structure
        summary(model, input_size=(3, 224, 224), device="cpu") if self.parser.detail else None  # log torchsummary model
        model.to("cpu")  # model to self.device

        ckpt_file = self.exp.trainer.ckpt
        ckpt = torch.load(ckpt_file, map_location="cpu")["model"]
        self.model = load_ckpt(model, ckpt)
        self.model.eval()

        logger.info("Setting finished, export onnx start ......")

    @logger.catch
    def run(self):
        self._before_export()

        x = torch.randn(self.exp.onnx.x_size)
        onnx_path = os.path.join(self.output_dir, self.exp.name + ".onnx")
        torch.onnx.export(self.model,
                          x,
                          onnx_path,
                          **self.exp.onnx.kwargs)
        logger.info("DONE")

