# Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import gc
import sys
import datetime
import six
import copy
import json

import paddle
import paddle.distributed as dist

from ppdet.utils.checkpoint import save_model, save_semi_model, save_model_info, update_train_results
from ppdet.metrics import get_infer_results

from ppdet.utils.logger import setup_logger
logger = setup_logger('ppdet.engine')

__all__ = [
    'Callback', 'ComposeCallback', 'LogPrinter', 'Checkpointer',
    'VisualDLWriter', 'SniperProposalsGenerator'
]


class Callback(object):
    def __init__(self, model):
        self.model = model
        log_ranks = self.model.cfg.get("log_ranks", '0')
        if isinstance(log_ranks, str):
            self.log_ranks = [int(i) for i in log_ranks.split(',')]
        elif isinstance(log_ranks, int):
            self.log_ranks = [log_ranks]
        self.logger = setup_logger('ppdet.engine.callbacks',log_ranks=self.log_ranks)

    def on_step_begin(self, status):
        pass

    def on_step_end(self, status):
        pass

    def on_epoch_begin(self, status):
        pass

    def on_epoch_end(self, status):
        pass

    def on_train_begin(self, status):
        pass

    def on_train_end(self, status):
        pass


class ComposeCallback(object):
    def __init__(self, callbacks):
        callbacks = [c for c in list(callbacks) if c is not None]
        for c in callbacks:
            assert isinstance(
                c, Callback), "callback should be subclass of Callback"
        self._callbacks = callbacks

    def on_step_begin(self, status):
        for c in self._callbacks:
            c.on_step_begin(status)

    def on_step_end(self, status):
        for c in self._callbacks:
            c.on_step_end(status)

    def on_epoch_begin(self, status):
        for c in self._callbacks:
            c.on_epoch_begin(status)

    def on_epoch_end(self, status):
        for c in self._callbacks:
            c.on_epoch_end(status)

    def on_train_begin(self, status):
        for c in self._callbacks:
            c.on_train_begin(status)

    def on_train_end(self, status):
        for c in self._callbacks:
            c.on_train_end(status)


class LogPrinter(Callback):
    def __init__(self, model):
        super(LogPrinter, self).__init__(model)

    def on_step_end(self, status):
        
        if dist.get_world_size() < 2 or dist.get_rank() in self.log_ranks:
            mode = status['mode']
            if mode == 'train':
                epoch_id = status['epoch_id']
                step_id = status['step_id']
                steps_per_epoch = status['steps_per_epoch']
                training_staus = status['training_staus']
                batch_time = status['batch_time']
                data_time = status['data_time']

                epoches = self.model.cfg.epoch
                batch_size = self.model.cfg['{}Reader'.format(mode.capitalize(
                ))]['batch_size']

                logs = training_staus.log()
                space_fmt = ':' + str(len(str(steps_per_epoch))) + 'd'
                if step_id % self.model.cfg.log_iter == 0:
                    eta_steps = (epoches - epoch_id) * steps_per_epoch - step_id
                    eta_sec = eta_steps * batch_time.global_avg
                    eta_str = str(datetime.timedelta(seconds=int(eta_sec)))
                    ips = float(batch_size) / batch_time.avg
                    max_mem_reserved_str = ""
                    max_mem_allocated_str = ""
                    print_mem_info = self.model.cfg.get("print_mem_info", True)
                    if paddle.device.is_compiled_with_cuda() and print_mem_info:
                        max_mem_reserved_str = f", max_mem_reserved: {paddle.device.cuda.max_memory_reserved() // (1024 ** 2)} MB"
                        max_mem_allocated_str = f", max_mem_allocated: {paddle.device.cuda.max_memory_allocated() // (1024 ** 2)} MB"
                    fmt = ' '.join([
                        'Epoch: [{}]',
                        '[{' + space_fmt + '}/{}]',
                        'learning_rate: {lr:.6f}',
                        '{meters}',
                        'eta: {eta}',
                        'batch_cost: {btime}',
                        'data_cost: {dtime}',
                        'ips: {ips:.4f} images/s'
                        '{max_mem_reserved_str}'
                        '{max_mem_allocated_str}'
                    ])
                    fmt = fmt.format(
                        epoch_id,
                        step_id,
                        steps_per_epoch,
                        lr=status['learning_rate'],
                        meters=logs,
                        eta=eta_str,
                        btime=str(batch_time),
                        dtime=str(data_time),
                        ips=ips,
                        max_mem_reserved_str=max_mem_reserved_str,
                        max_mem_allocated_str=max_mem_allocated_str)
                    self.logger.info(fmt)
            if mode == 'eval':
                step_id = status['step_id']
                if step_id % 100 == 0:
                    self.logger.info("Eval iter: {}".format(step_id))

    def on_epoch_end(self, status):
        if dist.get_world_size() < 2 or dist.get_rank() == 0:
            mode = status['mode']
            if mode == 'eval':
                sample_num = status['sample_num']
                cost_time = status['cost_time']
                self.logger.info('Total sample number: {}, average FPS: {}'.format(
                    sample_num, sample_num / cost_time))


class Checkpointer(Callback):
    def __init__(self, model):
        super(Checkpointer, self).__init__(model)
        self.best_ap = -1000.
        self.save_dir = self.model.cfg.save_dir
        self.uniform_output_enabled = self.model.cfg.get("uniform_output_enabled", False)
        if hasattr(self.model.model, 'student_model'):
            self.weight = self.model.model.student_model
        else:
            self.weight = self.model.model
        
    def on_epoch_end(self, status):
        # Checkpointer only performed during training
        mode = status['mode']
        epoch_id = status['epoch_id']
        weight = None
        save_name = None
        if dist.get_world_size() < 2 or dist.get_rank() == 0:
            end_epoch = self.model.cfg.epoch
            save_name = str(epoch_id) if epoch_id != end_epoch - 1 else "model_final"
            if mode == 'train':
                end_epoch = self.model.cfg.epoch
                if (
                        epoch_id + 1
                ) % self.model.cfg.snapshot_epoch == 0 or epoch_id == end_epoch - 1:
                    save_name = str(
                        epoch_id) if epoch_id != end_epoch - 1 else "model_final"
                    weight = self.weight.state_dict()
            elif mode == 'eval':
                for metric in self.model._metrics:
                    map_res = metric.get_results()
                    if "MOTA" in map_res:
                        key = "mot"
                        eval_func = "mota"
                        epoch_ap = float(map_res.split(' ')[-9].rstrip("%")) / 100
                    else:
                        eval_func = "ap"
                        if 'pose3d' in map_res:
                            key = 'pose3d'
                            eval_func = "mpjpe"
                        elif 'bbox' in map_res:
                            key = 'bbox'
                        elif 'keypoint' in map_res:
                            key = 'keypoint'
                        else:
                            key = 'mask'

                        key = self.model.cfg.get('target_metrics', key)

                        if key not in map_res:
                            logger.warning("Evaluation results empty, this may be due to " \
                                        "training iterations being too few or not " \
                                        "loading the correct weights.")
                            key = '' # To adapt to PaddleX: Save model weights even when evaluation results are empty.
                            epoch_ap = 0.0
                        else:
                            epoch_ap = map_res[key][0]
                    epoch_metric = {
                        'metric': abs(epoch_ap),
                        'epoch': epoch_id + 1
                    }
                    save_path = os.path.join(os.path.join(self.save_dir, save_name) if self.uniform_output_enabled else self.save_dir, f"{save_name}.pdstates")
                    paddle.save(epoch_metric, save_path)
                    if self.uniform_output_enabled:
                        save_model_info(epoch_metric, self.save_dir, save_name)
                        update_train_results(self.model.cfg, save_name, epoch_metric, done_flag=epoch_id + 1 == self.model.cfg.epoch, ema=self.model.use_ema)
                    if 'save_best_model' in status and status['save_best_model']:
                        if epoch_ap >= self.best_ap:
                            self.best_ap = epoch_ap
                            save_name = 'best_model'
                            weight = self.weight.state_dict()
                            best_metric = {
                                'metric': abs(self.best_ap),
                                'epoch': epoch_id + 1
                            }
                            save_path = os.path.join(os.path.join(self.save_dir, save_name) if self.uniform_output_enabled else self.save_dir, "best_model.pdstates")
                            paddle.save(best_metric, save_path)
                            if self.uniform_output_enabled:
                                save_model_info(best_metric, self.save_dir, save_name)
                                update_train_results(self.model.cfg, save_name, best_metric, done_flag=epoch_id + 1 == self.model.cfg.epoch, ema=self.model.use_ema)
                        logger.info("Best test {} {} is {:0.3f}.".format(
                            key, eval_func, abs(self.best_ap)))
            if weight:
                if self.model.use_ema:
                    exchange_save_model = status.get('exchange_save_model',
                                                     False)
                    if not exchange_save_model:
                        # save model and ema_model
                        save_model(
                            status['weight'],
                            self.model.optimizer,
                            os.path.join(self.save_dir, save_name) if self.uniform_output_enabled else self.save_dir,
                            save_name,
                            epoch_id + 1,
                            ema_model=weight)
                        if self.uniform_output_enabled:
                            self.model.export(output_dir=os.path.join(self.save_dir, save_name, "inference"), for_fd=True)
                            gc.collect()
                    else:
                        # save model(student model) and ema_model(teacher model)
                        # in DenseTeacher SSOD, the teacher model will be higher,
                        # so exchange when saving pdparams
                        student_model = status['weight']  # model
                        teacher_model = weight  # ema_model
                        save_model(
                            teacher_model,
                            self.model.optimizer,
                            self.save_dir,
                            save_name,
                            epoch_id + 1,
                            ema_model=student_model)
                        del teacher_model
                        del student_model
                else:
                    save_model(weight, self.model.optimizer, os.path.join(self.save_dir, save_name) if self.uniform_output_enabled else self.save_dir,
                               save_name, epoch_id + 1)
                    if self.uniform_output_enabled:
                        self.model.export(output_dir=os.path.join(self.save_dir, save_name, "inference"), for_fd=True)
                        gc.collect()


class WiferFaceEval(Callback):
    def __init__(self, model):
        super(WiferFaceEval, self).__init__(model)

    def on_epoch_begin(self, status):
        assert self.model.mode == 'eval', \
            "WiferFaceEval can only be set during evaluation"
        for metric in self.model._metrics:
            metric.update(self.model.model)
        sys.exit()


class VisualDLWriter(Callback):
    """
    Use VisualDL to log data or image
    """

    def __init__(self, model):
        super(VisualDLWriter, self).__init__(model)

        assert six.PY3, "VisualDL requires Python >= 3.5"
        try:
            from visualdl import LogWriter
        except Exception as e:
            logger.error('visualdl not found, plaese install visualdl. '
                         'for example: `pip install visualdl`.')
            raise e
        self.vdl_writer = LogWriter(
            model.cfg.get('vdl_log_dir', 'vdl_log_dir/scalar'))
        self.vdl_loss_step = 0
        self.vdl_mAP_step = 0
        self.vdl_image_step = 0
        self.vdl_image_frame = 0

    def on_step_end(self, status):
        mode = status['mode']
        if dist.get_world_size() < 2 or dist.get_rank() == 0:
            if mode == 'train':
                training_staus = status['training_staus']
                for loss_name, loss_value in training_staus.get().items():
                    self.vdl_writer.add_scalar(loss_name, loss_value,
                                               self.vdl_loss_step)
                self.vdl_loss_step += 1
            elif mode == 'test':
                ori_image = status['original_image']
                result_image = status['result_image']
                self.vdl_writer.add_image(
                    "original/frame_{}".format(self.vdl_image_frame), ori_image,
                    self.vdl_image_step)
                self.vdl_writer.add_image(
                    "result/frame_{}".format(self.vdl_image_frame),
                    result_image, self.vdl_image_step)
                self.vdl_image_step += 1
                # each frame can display ten pictures at most.
                if self.vdl_image_step % 10 == 0:
                    self.vdl_image_step = 0
                    self.vdl_image_frame += 1

    def on_epoch_end(self, status):
        mode = status['mode']
        if dist.get_world_size() < 2 or dist.get_rank() == 0:
            if mode == 'eval':
                for metric in self.model._metrics:
                    res = metric.get_results()
                    if "MOTA" in res:
                        mota = float(res.split(' ')[-9].rstrip("%")) / 100
                        self.vdl_writer.add_scalar("mot-mota",
                                                    mota,
                                                    self.vdl_mAP_step)
                    else:
                        for key, map_value in metric.get_results().items():
                            self.vdl_writer.add_scalar("{}-mAP".format(key),
                                                    map_value[0],
                                                    self.vdl_mAP_step)
                self.vdl_mAP_step += 1


class WandbCallback(Callback):
    def __init__(self, model):
        super(WandbCallback, self).__init__(model)

        try:
            import wandb
            self.wandb = wandb
        except Exception as e:
            logger.error('wandb not found, please install wandb. '
                         'Use: `pip install wandb`.')
            raise e

        self.wandb_params = model.cfg.get('wandb', None)
        self.save_dir = self.model.cfg.save_dir
        if self.wandb_params is None:
            self.wandb_params = {}
        for k, v in model.cfg.items():
            if k.startswith("wandb_"):
                self.wandb_params.update({k.lstrip("wandb_"): v})

        self._run = None
        if dist.get_world_size() < 2 or dist.get_rank() == 0:
            _ = self.run
            self.run.config.update(self.model.cfg)
            self.run.define_metric("epoch")
            self.run.define_metric("eval/*", step_metric="epoch")

        self.best_ap = -1000.
        self.fps = []

    @property
    def run(self):
        if self._run is None:
            if self.wandb.run is not None:
                logger.info(
                    "There is an ongoing wandb run which will be used"
                    "for logging. Please use `wandb.finish()` to end that"
                    "if the behaviour is not intended")
                self._run = self.wandb.run
            else:
                self._run = self.wandb.init(**self.wandb_params)
        return self._run

    def save_model(self,
                   optimizer,
                   save_dir,
                   save_name,
                   last_epoch,
                   ema_model=None,
                   ap=None,
                   fps=None,
                   tags=None):
        if dist.get_world_size() < 2 or dist.get_rank() == 0:
            model_path = os.path.join(save_dir, save_name)
            metadata = {}
            metadata["last_epoch"] = last_epoch
            if ap:
                metadata["ap"] = ap

            if fps:
                metadata["fps"] = fps

            if ema_model is None:
                ema_artifact = self.wandb.Artifact(
                    name="ema_model-{}".format(self.run.id),
                    type="model",
                    metadata=metadata)
                model_artifact = self.wandb.Artifact(
                    name="model-{}".format(self.run.id),
                    type="model",
                    metadata=metadata)

                ema_artifact.add_file(model_path + ".pdema", name="model_ema")
                model_artifact.add_file(model_path + ".pdparams", name="model")

                self.run.log_artifact(ema_artifact, aliases=tags)
                self.run.log_artfact(model_artifact, aliases=tags)
            else:
                model_artifact = self.wandb.Artifact(
                    name="model-{}".format(self.run.id),
                    type="model",
                    metadata=metadata)
                model_artifact.add_file(model_path + ".pdparams", name="model")
                self.run.log_artifact(model_artifact, aliases=tags)

    def on_step_end(self, status):

        mode = status['mode']
        if dist.get_world_size() < 2 or dist.get_rank() == 0:
            if mode == 'train':
                training_status = status['training_staus'].get()
                for k, v in training_status.items():
                    training_status[k] = float(v)

                # calculate ips, data_cost, batch_cost
                batch_time = status['batch_time']
                data_time = status['data_time']
                batch_size = self.model.cfg['{}Reader'.format(mode.capitalize(
                ))]['batch_size']

                ips = float(batch_size) / float(batch_time.avg)
                data_cost = float(data_time.avg)
                batch_cost = float(batch_time.avg)

                metrics = {"train/" + k: v for k, v in training_status.items()}

                metrics["train/ips"] = ips
                metrics["train/data_cost"] = data_cost
                metrics["train/batch_cost"] = batch_cost

                self.fps.append(ips)
                self.run.log(metrics)

    def on_epoch_end(self, status):
        mode = status['mode']
        epoch_id = status['epoch_id']
        save_name = None
        if dist.get_world_size() < 2 or dist.get_rank() == 0:
            if mode == 'train':
                fps = sum(self.fps) / len(self.fps)
                self.fps = []

                end_epoch = self.model.cfg.epoch
                if (
                        epoch_id + 1
                ) % self.model.cfg.snapshot_epoch == 0 or epoch_id == end_epoch - 1:
                    save_name = str(
                        epoch_id) if epoch_id != end_epoch - 1 else "model_final"
                    tags = ["latest", "epoch_{}".format(epoch_id)]
                    self.save_model(
                        self.model.optimizer,
                        self.save_dir,
                        save_name,
                        epoch_id + 1,
                        self.model.use_ema,
                        fps=fps,
                        tags=tags)
            if mode == 'eval':
                sample_num = status['sample_num']
                cost_time = status['cost_time']

                fps = sample_num / cost_time

                merged_dict = {}
                for metric in self.model._metrics:
                    for key, map_value in metric.get_results().items():
                        merged_dict["eval/{}-mAP".format(key)] = map_value[0]
                merged_dict["epoch"] = status["epoch_id"]
                merged_dict["eval/fps"] = sample_num / cost_time

                self.run.log(merged_dict)

                if 'save_best_model' in status and status['save_best_model']:
                    for metric in self.model._metrics:
                        map_res = metric.get_results()
                        if 'pose3d' in map_res:
                            key = 'pose3d'
                        elif 'bbox' in map_res:
                            key = 'bbox'
                        elif 'keypoint' in map_res:
                            key = 'keypoint'
                        else:
                            key = 'mask'
                        if key not in map_res:
                            logger.warning("Evaluation results empty, this may be due to " \
                                        "training iterations being too few or not " \
                                        "loading the correct weights.")
                            return
                        if map_res[key][0] >= self.best_ap:
                            self.best_ap = map_res[key][0]
                            save_name = 'best_model'
                            tags = ["best", "epoch_{}".format(epoch_id)]

                            self.save_model(
                                self.model.optimizer,
                                self.save_dir,
                                save_name,
                                last_epoch=epoch_id + 1,
                                ema_model=self.model.use_ema,
                                ap=abs(self.best_ap),
                                fps=fps,
                                tags=tags)

    def on_train_end(self, status):
        self.run.finish()


class SniperProposalsGenerator(Callback):
    def __init__(self, model):
        super(SniperProposalsGenerator, self).__init__(model)
        ori_dataset = self.model.dataset
        self.dataset = self._create_new_dataset(ori_dataset)
        self.loader = self.model.loader
        self.cfg = self.model.cfg
        self.infer_model = self.model.model

    def _create_new_dataset(self, ori_dataset):
        dataset = copy.deepcopy(ori_dataset)
        # init anno_cropper
        dataset.init_anno_cropper()
        # generate infer roidbs
        ori_roidbs = dataset.get_ori_roidbs()
        roidbs = dataset.anno_cropper.crop_infer_anno_records(ori_roidbs)
        # set new roidbs
        dataset.set_roidbs(roidbs)

        return dataset

    def _eval_with_loader(self, loader):
        results = []
        with paddle.no_grad():
            self.infer_model.eval()
            for step_id, data in enumerate(loader):
                outs = self.infer_model(data)
                for key in ['im_shape', 'scale_factor', 'im_id']:
                    outs[key] = data[key]
                for key, value in outs.items():
                    if hasattr(value, 'numpy'):
                        outs[key] = value.numpy()

                results.append(outs)

        return results

    def on_train_end(self, status):
        self.loader.dataset = self.dataset
        results = self._eval_with_loader(self.loader)
        results = self.dataset.anno_cropper.aggregate_chips_detections(results)
        # sniper
        proposals = []
        clsid2catid = {v: k for k, v in self.dataset.catid2clsid.items()}
        for outs in results:
            batch_res = get_infer_results(outs, clsid2catid)
            start = 0
            for i, im_id in enumerate(outs['im_id']):
                bbox_num = outs['bbox_num']
                end = start + bbox_num[i]
                bbox_res = batch_res['bbox'][start:end] \
                    if 'bbox' in batch_res else None
                if bbox_res:
                    proposals += bbox_res
        logger.info("save proposals in {}".format(self.cfg.proposals_path))
        with open(self.cfg.proposals_path, 'w') as f:
            json.dump(proposals, f)


class SemiLogPrinter(LogPrinter):
    def __init__(self, model):
        super(SemiLogPrinter, self).__init__(model)

    def on_step_end(self, status):
        if dist.get_world_size() < 2 or dist.get_rank() == 0:
            mode = status['mode']
            if mode == 'train':
                epoch_id = status['epoch_id']
                step_id = status['step_id']
                iter_id = status['iter_id']
                steps_per_epoch = status['steps_per_epoch']
                training_staus = status['training_staus']
                batch_time = status['batch_time']
                data_time = status['data_time']

                epoches = self.model.cfg.epoch
                batch_size = self.model.cfg['{}Reader'.format(mode.capitalize(
                ))]['batch_size']
                iters = epoches * steps_per_epoch
                logs = training_staus.log()
                iter_space_fmt = ':' + str(len(str(iters))) + 'd'
                space_fmt = ':' + str(len(str(iters))) + 'd'
                if step_id % self.model.cfg.log_iter == 0:
                    eta_steps = (epoches - epoch_id) * steps_per_epoch - step_id
                    eta_sec = eta_steps * batch_time.global_avg
                    eta_str = str(datetime.timedelta(seconds=int(eta_sec)))
                    ips = float(batch_size) / batch_time.avg
                    fmt = ' '.join([
                        '{' + iter_space_fmt + '}/{} iters',
                        'Epoch: [{}]',
                        '[{' + space_fmt + '}/{}]',
                        'learning_rate: {lr:.6f}',
                        '{meters}',
                        'eta: {eta}',
                        'batch_cost: {btime}',
                        'data_cost: {dtime}',
                        'ips: {ips:.4f} images/s',
                    ])
                    fmt = fmt.format(
                        iter_id,
                        iters,
                        epoch_id,
                        step_id,
                        steps_per_epoch,
                        lr=status['learning_rate'],
                        meters=logs,
                        eta=eta_str,
                        btime=str(batch_time),
                        dtime=str(data_time),
                        ips=ips)
                    logger.info(fmt)
            if mode == 'eval':
                step_id = status['step_id']
                if step_id % 100 == 0:
                    logger.info("Eval iter: {}".format(step_id))


class SemiCheckpointer(Checkpointer):
    def __init__(self, model):
        super(SemiCheckpointer, self).__init__(model)
        cfg = self.model.cfg
        self.best_ap = 0.
        self.save_dir = os.path.join(self.model.cfg.save_dir,
                                     self.model.cfg.filename)
        if hasattr(self.model.model, 'student') and hasattr(self.model.model,
                                                            'teacher'):
            self.weight = (self.model.model.teacher, self.model.model.student)
        elif hasattr(self.model.model, 'student') or hasattr(self.model.model,
                                                             'teacher'):
            raise AttributeError(
                "model has no attribute 'student' or 'teacher'")
        else:
            raise AttributeError(
                "model has no attribute 'student' and 'teacher'")

    def every_n_iters(self, iter_id, n):
        return (iter_id + 1) % n == 0 if n > 0 else False

    def on_step_end(self, status):
        # Checkpointer only performed during training
        mode = status['mode']
        eval_interval = status['eval_interval']
        save_interval = status['save_interval']
        iter_id = status['iter_id']
        epoch_id = status['epoch_id']
        t_weight = None
        s_weight = None
        save_name = None
        if dist.get_world_size() < 2 or dist.get_rank() == 0:
            if self.every_n_iters(iter_id, save_interval) and mode == 'train':
                save_name = "last_epoch"
                # save_name = str(iter_id + 1)
                t_weight = self.weight[0].state_dict()
                s_weight = self.weight[1].state_dict()
                save_semi_model(t_weight, s_weight, self.model.optimizer,
                                self.save_dir, save_name, epoch_id + 1,
                                iter_id + 1)

    def on_epoch_end(self, status):
        # Checkpointer only performed during training
        mode = status['mode']
        eval_interval = status['eval_interval']
        save_interval = status['save_interval']
        iter_id = status['iter_id']
        epoch_id = status['epoch_id']
        t_weight = None
        s_weight = None
        save_name = None
        if dist.get_world_size() < 2 or dist.get_rank() == 0:
            if self.every_n_iters(iter_id, eval_interval) and mode == 'eval':
                if 'save_best_model' in status and status['save_best_model']:
                    for metric in self.model._metrics:
                        map_res = metric.get_results()
                        if 'bbox' in map_res:
                            key = 'bbox'
                        elif 'keypoint' in map_res:
                            key = 'keypoint'
                        else:
                            key = 'mask'
                        if key not in map_res:
                            logger.warning("Evaluation results empty, this may be due to " \
                                        "training iterations being too few or not " \
                                        "loading the correct weights.")
                            return
                        if map_res[key][0] > self.best_ap:
                            self.best_ap = map_res[key][0]
                            save_name = 'best_model'
                            t_weight = self.weight[0].state_dict()
                            s_weight = self.weight[1].state_dict()
                        logger.info("Best teacher test {} ap is {:0.3f}.".
                                    format(key, self.best_ap))
                    if t_weight and s_weight:
                        save_semi_model(t_weight, s_weight,
                                        self.model.optimizer, self.save_dir,
                                        save_name, epoch_id + 1, iter_id + 1)
