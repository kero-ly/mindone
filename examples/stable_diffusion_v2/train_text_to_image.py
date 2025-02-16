"""
Stable diffusion model training/finetuning
"""
import argparse
import importlib
import logging
import os
import shutil

from ldm.data.dataset import build_dataset
from ldm.data.dataset_dist import calculate_split, download_tar
from ldm.modules.logger import set_logger
from ldm.modules.lora import inject_trainable_lora
from ldm.modules.train.callback import EvalSaveCallback, OverflowMonitor
from ldm.modules.train.checkpoint import resume_train_network
from ldm.modules.train.ema import EMA
from ldm.modules.train.learningrate import LearningRate
from ldm.modules.train.optim import build_optimizer
from ldm.modules.train.parallel_config import ParallelConfig
from ldm.modules.train.tools import parse_with_config, set_random_seed
from ldm.modules.train.trainer import TrainOneStepWrapper
from ldm.util import count_params, is_old_ms_version, str2bool
from omegaconf import OmegaConf

import mindspore as ms
from mindspore import Model, context, load_checkpoint, load_param_into_net
from mindspore.communication.management import get_group_size, get_rank, init
from mindspore.nn.wrap.loss_scale import DynamicLossScaleUpdateCell
from mindspore.train.callback import LossMonitor, TimeMonitor

os.environ["HCCL_CONNECT_TIMEOUT"] = "6000"
SD_VERSION = os.getenv("SD_VERSION", default="2.0")

logger = logging.getLogger(__name__)


def init_env(args):
    set_random_seed(args.seed)

    ms.set_context(mode=context.GRAPH_MODE)  # needed for MS2.0
    if args.use_parallel:
        init()
        device_id = int(os.getenv("DEVICE_ID"))
        device_num = get_group_size()
        ParallelConfig.dp = device_num
        rank_id = get_rank()
        args.rank = rank_id
        logger.debug("Device_id: {}, rank_id: {}, device_num: {}".format(device_id, rank_id, device_num))
        context.reset_auto_parallel_context()
        context.set_auto_parallel_context(
            parallel_mode=context.ParallelMode.DATA_PARALLEL,
            # parallel_mode=context.ParallelMode.AUTO_PARALLEL,
            gradients_mean=True,
            device_num=device_num,
        )
        # download dataset
        print("device_num", device_num, "rank_id", rank_id, flush=True)
        start_part_idx, start_tar_idx, start_sample_idx, end_part_idx, end_tar_idx, end_sample_idx, tars_to_sync = calculate_split(device_num / 8, rank_id / 8)
        #start_part_idx, start_tar_idx, start_sample_idx, end_part_idx, end_tar_idx, end_sample_idx, tars_to_sync = calculate_split(1024, 0) # 79
        download_tar(tars_to_sync, start_sample_idx, end_sample_idx)
        print("device_num", device_num, "rank_id", rank_id,
                "start_part_idx", start_part_idx, "start_tar_idx", start_tar_idx, "start_sample_idx", start_sample_idx,
                "end_part_idx", end_part_idx, "end_tar_idx", end_tar_idx, "end_sample_idx", end_sample_idx,
                "tars_to_sync", tars_to_sync, flush=True)
        
    else:
        device_num = 1
        device_id = int(os.getenv("DEVICE_ID", 0))
        rank_id = 0
        args.rank = rank_id

    context.set_context(
        mode=context.GRAPH_MODE,
        device_target="Ascend",
        device_id=device_id,
        max_device_memory="30GB",  # TODO: why limit?
    )
    ms.set_context(ascend_config={"precision_mode": "allow_fp32_to_fp16"})  # Only effective on Ascend 901B

    return rank_id, device_id, device_num


def build_model_from_config(config):
    config = OmegaConf.load(config).model
    if "target" not in config:
        if config == "__is_first_stage__":
            return None
        elif config == "__is_unconditional__":
            return None
        raise KeyError("Expected key `target` to instantiate.")
    config_params = config.get("params", dict())
    # config_params['cond_stage_trainable'] = cond_stage_trainable # TODO: easy config
    return get_obj_from_str(config["target"])(**config_params)


def get_obj_from_str(string, reload=False):
    module, cls = string.rsplit(".", 1)
    if reload:
        module_imp = importlib.import_module(module)
        importlib.reload(module_imp)
    return getattr(importlib.import_module(module, package=None), cls)


def load_pretrained_model(pretrained_ckpt, net):
    logger.info(f"Loading pretrained model from {pretrained_ckpt}")
    if os.path.exists(pretrained_ckpt):
        param_dict = load_checkpoint(pretrained_ckpt)
        if is_old_ms_version():
            param_not_load = load_param_into_net(net, param_dict)
        else:
            param_not_load, ckpt_not_load = load_param_into_net(net, param_dict)
        logger.info("Params not load: {}".format(param_not_load))
    else:
        logger.warning("Checkpoint file {pretrained_ckpt} dose not exist!!!")


def load_pretrained_model_clip_and_vae(pretrained_ckpt, net):
    new_param_dict = {}
    logger.info(f"Loading pretrained model from {pretrained_ckpt}")
    if os.path.exists(pretrained_ckpt):
        param_dict = load_checkpoint(pretrained_ckpt)
        for key in param_dict:
            if key.startswith("first") or key.startswith("cond"):
                new_param_dict[key] = param_dict[key]
        param_not_load = load_param_into_net(net, new_param_dict)
        logger.info("Params not load: {}".format(param_not_load))
    else:
        logger.warning("Checkpoint file {pretrained_ckpt} dose not exist!!!")


def load_pretrained_model_vae_unet_cnclip(pretrained_ckpt, cnclip_ckpt, net):
    new_param_dict = {}
    logger.info(f"Loading pretrained model from {pretrained_ckpt}, {cnclip_ckpt}")
    if os.path.exists(pretrained_ckpt) and os.path.exists(cnclip_ckpt):
        param_dict = load_checkpoint(pretrained_ckpt)
        cnclip_param_dict = load_checkpoint(pretrained_ckpt)
        for key in param_dict:
            if key.startswith("first") or key.startswith("model"):
                new_param_dict[key] = param_dict[key]
        for key in cnclip_param_dict:
            new_param_dict[key] = cnclip_param_dict[key]
        param_not_load = load_param_into_net(net, new_param_dict)
        logger.info("Params not load: {}".format(param_not_load))
    else:
        logger.warning("Checkpoint file {pretrained_ckpt}, {cnclip_ckpt} dose not exist!!!")


def main(args):
    # init
    rank_id, device_id, device_num = init_env(args)
    set_logger(name="", output_dir=args.output_path, rank=rank_id, log_level=eval(args.log_level))

    # build model
    latent_diffusion_with_loss = build_model_from_config(args.model_config)
    pretrained_ckpt = os.path.join(args.pretrained_model_path, args.pretrained_model_file)
    if args.custom_text_encoder is not None and os.path.exists(args.custom_text_encoder):
        load_pretrained_model_vae_unet_cnclip(pretrained_ckpt, args.custom_text_encoder, latent_diffusion_with_loss)
    else:
        load_pretrained_model(pretrained_ckpt, latent_diffusion_with_loss)

    # build dataset
    tokenizer = latent_diffusion_with_loss.cond_stage_model.tokenizer
    args.data_path = "/cache/merged_imgp_text.csv"
    dataset = build_dataset(args, rank_id, device_num, tokenizer)

    # lora injection
    if args.use_lora:
        # freeze network
        for param in latent_diffusion_with_loss.get_parameters():
            param.requires_grad = False

        # inject lora params
        injected_attns, injected_trainable_params = inject_trainable_lora(
            latent_diffusion_with_loss,
            rank=args.lora_rank,
            use_fp16=args.lora_fp16,
        )

        # TODO: support lora inject to text encoder (remove .model)
        assert len(latent_diffusion_with_loss.trainable_params()) == len(
            injected_trainable_params
        ), "Only lora params should be trainable. but got {} trainable params".format(
            len(latent_diffusion_with_loss.trainable_params())
        )
        # print('Trainable params: ', latent_diffusion_with_loss.model.trainable_params())

    if not args.decay_steps:
        dataset_size = dataset.get_dataset_size()
        args.decay_steps = args.epochs * dataset_size - args.warmup_steps  # fix lr scheduling
        if args.decay_steps <= 0:
            logger.warning(
                f"decay_steps is {args.decay_steps}, please check epochs, dataset_size and warmup_steps. "
                f"Will force decay_steps to be set to 1."
            )
            args.decay_steps = 1
    lr = LearningRate(args.start_learning_rate, args.end_learning_rate, args.warmup_steps, args.decay_steps)
    optimizer = build_optimizer(latent_diffusion_with_loss, args, lr)

    loss_scaler = DynamicLossScaleUpdateCell(
        loss_scale_value=args.init_loss_scale, scale_factor=args.loss_scale_factor, scale_window=args.scale_window
    )

    # resume ckpt
    if rank_id == 0:
        ckpt_dir = os.path.join(args.output_path, "ckpt")
        if not os.path.exists(ckpt_dir):
            os.makedirs(ckpt_dir)
    start_epoch = 0
    if args.resume:
        resume_ckpt = os.path.join(ckpt_dir, "train_resume.ckpt") if isinstance(args.resume, bool) else args.resume

        start_epoch, loss_scale, cur_iter, last_overflow_iter = resume_train_network(
            latent_diffusion_with_loss, optimizer, resume_ckpt
        )
        loss_scaler.loss_scale_value = loss_scale
        loss_scaler.cur_iter = cur_iter
        loss_scaler.last_overflow_iter = last_overflow_iter

    # trainer (standalone and distributed)
    ema = (
        EMA(
            latent_diffusion_with_loss,  # .model, #TODO: remove .model if not only train UNet
            ema_decay=0.9999,
        )
        if args.use_ema
        else None
    )

    net_with_grads = TrainOneStepWrapper(
        latent_diffusion_with_loss,
        optimizer=optimizer,
        scale_sense=loss_scaler,
        drop_overflow_update=True,  # TODO: allow config
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        clip_grad=args.clip_grad,
        clip_norm=args.max_grad_norm,
        ema=ema,
    )

    model = Model(net_with_grads)

    # callbacks
    callback = [TimeMonitor(args.callback_size), LossMonitor(args.callback_size)]
    ofm_cb = OverflowMonitor()
    callback.append(ofm_cb)

    if rank_id == 0:
        save_cb = EvalSaveCallback(
            network=latent_diffusion_with_loss,  # TODO: save unet/vae seperately
            use_lora=args.use_lora,
            rank_id=rank_id,
            ckpt_save_dir=ckpt_dir,
            ema=ema,
            ckpt_save_policy="latest_k",
            ckpt_max_keep=10,
            ckpt_save_interval=args.ckpt_save_interval,
            lora_rank=args.lora_rank,
        )

        callback.append(save_cb)

    # log
    if rank_id == 0:
        num_params_unet, _ = count_params(latent_diffusion_with_loss.model.diffusion_model)
        num_params_text_encoder, _ = count_params(latent_diffusion_with_loss.cond_stage_model)
        num_params_vae, _ = count_params(latent_diffusion_with_loss.first_stage_model)
        num_params, num_trainable_params = count_params(latent_diffusion_with_loss)

        key_info = "Key Settings:\n" + "=" * 50 + "\n"
        key_info += "\n".join(
            [
                "MindSpore mode[GRAPH(0)/PYNATIVE(1)]: 0",
                f"Distributed mode: {args.use_parallel}",
                f"Data path: {args.data_path}",
                f"Model: StableDiffusion v{SD_VERSION}",
                f"Num params: {num_params:,} (unet: {num_params_unet:,}, text encoder: {num_params_text_encoder:,}, vae: {num_params_vae:,})",
                f"Num trainable params: {num_trainable_params:,}",
                f"Precision: {latent_diffusion_with_loss.model.diffusion_model.dtype}",
                f"Use LoRA: {args.use_lora}",
                f"LoRA rank: {args.lora_rank}",
                f"Learning rate: {args.start_learning_rate}",
                f"Batch size: {args.train_batch_size}",
                f"Grad accumulation steps: {args.gradient_accumulation_steps}",
                f"Num epochs: {args.epochs}",
                f"Grad clipping: {args.clip_grad}",
                f"Max grad norm: {args.max_grad_norm}",
                f"EMA: {args.use_ema}",
            ]
        )
        key_info += "\n" + "=" * 50
        logger.info(key_info)

        logger.info("Start training...")
        # backup config files
        shutil.copyfile(args.model_config, os.path.join(args.output_path, "model_config.yaml"))
        shutil.copyfile(args.train_config, os.path.join(args.output_path, "train_config.yaml"))

    # train
    model.train(args.epochs, dataset, callbacks=callback, dataset_sink_mode=False, initial_epoch=start_epoch)


if __name__ == "__main__":
    logger.debug("process id:", os.getpid())
    parser = argparse.ArgumentParser()
    parser.add_argument("--use_parallel", default=True, type=str2bool, help="use parallel")
    parser.add_argument("--data_path", default="dataset", type=str, help="data path")
    parser.add_argument("--output_path", default="output/", type=str, help="output directory to save training results")
    parser.add_argument(
        "--resume",
        default=False,
        type=str,
        help="resume training, can set True or path to resume checkpoint.(default=False)",
    )
    parser.add_argument("--train_config", default="configs/train_config.json", type=str, help="train config path")
    parser.add_argument("--model_config", default="configs/v1-train-chinese.yaml", type=str, help="model config path")
    parser.add_argument("--custom_text_encoder", default="", type=str, help="use this to plug in custom clip model")
    parser.add_argument("--pretrained_model_path", default="", type=str, help="pretrained model directory")
    parser.add_argument("--pretrained_model_file", default="", type=str, help="pretrained model file name")
    parser.add_argument("--use_lora", default=False, type=str2bool, help="use lora finetuning")
    parser.add_argument(
        "--lora_rank",
        default=4,
        type=int,
        help="lora rank. The bigger, the larger the LoRA model will be, but usually gives better generation quality.",
    )
    parser.add_argument("--lora_fp16", default=True, type=str2bool, help="Whether use fp16 for LoRA params.")

    parser.add_argument("--optim", default="adamw", type=str, help="optimizer")
    parser.add_argument("--seed", default=3407, type=int, help="data path")
    parser.add_argument("--warmup_steps", default=1000, type=int, help="warmup steps")
    parser.add_argument("--train_batch_size", default=10, type=int, help="batch size")
    parser.add_argument("--callback_size", default=1, type=int, help="callback size.")
    parser.add_argument("--start_learning_rate", default=1e-5, type=float, help="The initial learning rate for Adam.")
    parser.add_argument("--end_learning_rate", default=1e-7, type=float, help="The end learning rate for Adam.")
    parser.add_argument("--decay_steps", default=0, type=int, help="lr decay steps.")
    parser.add_argument("--epochs", default=10, type=int, help="epochs")
    parser.add_argument("--init_loss_scale", default=65536, type=float, help="loss scale")
    parser.add_argument("--loss_scale_factor", default=2, type=float, help="loss scale factor")
    parser.add_argument("--scale_window", default=1000, type=float, help="scale window")
    parser.add_argument("--gradient_accumulation_steps", default=1, type=int, help="gradient accumulation steps")
    # parser.add_argument("--cond_stage_trainable", default=False, type=str2bool, help="whether text encoder is trainable")
    parser.add_argument("--use_ema", default=False, type=str2bool, help="whether use EMA")
    parser.add_argument("--clip_grad", default=False, type=str2bool, help="whether apply gradient clipping")
    parser.add_argument(
        "--max_grad_norm",
        default=1.0,
        type=float,
        help="max gradient norm for clipping, effective when `clip_grad` enabled.",
    )

    parser.add_argument("--ckpt_save_interval", default=4, type=int, help="save checkpoint every this epochs")
    parser.add_argument("--random_crop", default=False, type=str2bool, help="random crop")
    parser.add_argument("--filter_small_size", default=True, type=str2bool, help="filter small images")
    parser.add_argument("--image_size", default=512, type=int, help="images size")
    parser.add_argument("--image_filter_size", default=256, type=int, help="image filter size")

    parser.add_argument(
        "--log_level",
        type=str,
        default="logging.INFO",
        help="log level, options: logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR",
    )

    args = parser.parse_args()
    args = parse_with_config(args)
    abs_path = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ""))
    args.model_config = os.path.join(abs_path, args.model_config)

    logger.info(args)
    main(args)
