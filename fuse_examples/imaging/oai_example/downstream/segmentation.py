from fuse.utils.utils_logger import fuse_logger_start
import os
from glob import glob
import pandas as pd
from fuse.dl.models import ModelMultiHead
from fuse.utils import NDict

from fuse.dl.models.heads.head_dense_segmentation import HeadDenseSegmentation

from monai.losses import GeneralizedDiceLoss
import torch.nn as nn
import torch
from torch.utils.data.dataloader import DataLoader
from fuse.data.utils.collates import CollateDefault

from fuse.eval.metrics.segmentation.metrics_segmentation_common import MetricDice
import torch.optim as optim
from fuse.utils.rand.seed import Seed
import logging
from fuse.dl.losses.loss_default import LossDefault
from fuse.dl.lightning.pl_module import LightningModuleDefault
import pytorch_lightning as pl
from pytorch_lightning.callbacks import LearningRateMonitor
import hydra
from omegaconf import DictConfig
from clearml import Task
from fuse_examples.imaging.oai_example.data.seg_ds import SegOAI
from fuse.dl.models.backbones.backbone_unet3d import UNet3D


torch.set_float32_matmul_precision("medium")


@hydra.main(version_base="1.2", config_path=".", config_name="segmentation_config")
def main(cfg: DictConfig) -> None:
    cfg = hydra.utils.instantiate(cfg)
    results_path = cfg.results_dir
    os.environ["CUDA_VISIBLE_DEVICES"] = ",".join([str(x) for x in cfg.cuda_devices])

    assert (
        sum(
            cfg[weights] is not None
            for weights in [
                "suprem_weights",
                "dino_weights",
                "resume_training_from",
                "test_ckpt",
            ]
        )
        <= 1
    ), "only one weights/ckpt path can be used at a time"

    model_dir = os.path.join(results_path, cfg.experiment)
    if not os.path.isdir(model_dir):
        os.makedirs(model_dir)

    tags = cfg.tags + [cfg.backbone]
    print(f"EXPERIMENT : {cfg.experiment}")
    if cfg.clearml:
        if len(glob(model_dir + "/*.cmlid")) == 0:
            task = Task.create(
                project_name=cfg.clearml_project_name, task_name=cfg.experiment
            )
            with open(os.path.join(model_dir, f"{task.id}.cmlid"), "w"):
                pass

        clearml_task_id = glob(model_dir + "/*.cmlid")[0].split("/")[-1].split(".")[0]
        task = Task.init(
            project_name=cfg.clearml_project_name,
            task_name=cfg.experiment,
            reuse_last_task_id=clearml_task_id,
            continue_last_task=0,
            tags=tags,
        )

        task.connect(cfg)
        task.add_tags(cfg.tags)
    # Model definition:
    ##############################################################################
    if cfg.backbone == "unet3d":
        backbone = UNet3D(for_cls=False)

        if cfg.dino_weights is not None:
            print(f"LOADING ckpt from {cfg.dino_weights}")
            state_dict = torch.load(
                open(cfg.dino_weights, "rb"), map_location=torch.device("cpu")
            )
            state_dict = {
                k.replace("teacher_backbone.", ""): v
                for k, v in state_dict["state_dict"].items()
                if "teacher_backbone." in k
            }
        elif cfg.suprem_weights is not None:
            state_dict = torch.load(
                cfg.suprem_weights, map_location=torch.device("cpu")
            )
            state_dict = {
                k.replace("module.backbone.", ""): v
                for k, v in state_dict["net"].items()
            }
        else:
            state_dict = backbone.state_dict()

        backbone.load_state_dict(state_dict, strict=False)
        conv_inputs = [("model.backbone_features", 512)]

    heads = [
        HeadDenseSegmentation(
            head_name="head_seg",
            conv_inputs=conv_inputs,
            shared_classifier_head=nn.Conv3d(64, cfg.num_classes, kernel_size=1),
        )
    ]

    model = ModelMultiHead(conv_inputs=(("img", 1),), backbone=backbone, heads=heads)

    # read environment variables for data, cache and results locations

    ## Basic settings:
    ##############################################################################
    # create model results dir:
    # we use a time stamp in model directory name, to prevent re-writing

    # start logger
    fuse_logger_start(output_path=model_dir, console_verbose_level=logging.INFO)
    print("Done")

    # set constant seed for reproducibility.
    os.environ[
        "CUBLAS_WORKSPACE_CONFIG"
    ] = ":4096:8"  # required for pytorch deterministic mode
    rand_gen = Seed.set_seed(1234, deterministic_mode=True)

    ## FuseMedML dataset preparation
    ##############################################################################
    df_all = pd.read_csv(cfg.csv_path)
    dfs = {}
    for _set in ["train", "val", "test"]:
        dfs[_set] = df_all[df_all.fold.isin(cfg[f"{_set}_folds"])]

    train_ds = SegOAI.dataset(
        dfs["train"], validation=(not cfg.aug), resize_to=cfg.resize_to
    )
    val_ds = SegOAI.dataset(dfs["val"], validation=True, resize_to=cfg.resize_to)
    test_ds = SegOAI.dataset(dfs["test"], validation=True, resize_to=cfg.resize_to)
    ## Create dataloader

    train_dl = DataLoader(
        dataset=train_ds,
        shuffle=True,
        drop_last=False,
        # batch_sampler=sampler,
        batch_size=cfg.batch_size,
        collate_fn=CollateDefault(),
        num_workers=cfg.n_workers,
    )

    val_dl = DataLoader(
        dataset=val_ds,
        shuffle=False,
        drop_last=False,
        batch_sampler=None,
        batch_size=1,
        num_workers=cfg.n_workers,
        collate_fn=CollateDefault(),
        generator=rand_gen,
    )
    test_dl = DataLoader(
        dataset=test_ds,
        shuffle=False,
        drop_last=False,
        batch_sampler=None,
        batch_size=1,
        num_workers=cfg.n_workers,
        collate_fn=CollateDefault(),
        generator=rand_gen,
    )

    # Loss definition:
    ##############################################################################
    callable_loss = GeneralizedDiceLoss(
        include_background=cfg.include_background,
        sigmoid=cfg.sigmoid,
        softmax=cfg.softmax,
    )
    losses = {
        "generalized_dice_loss": LossDefault(
            pred="model.logits.head_seg",
            target="seg",
            callable=callable_loss,
            weight=1.0,
        )
    }

    # Metrics definition:
    ##############################################################################
    def pre_process_for_dice(sample: NDict) -> NDict:
        sample["model.logits.head_seg"] = sample["model.logits.head_seg"].argmax(axis=0)
        sample["seg"] = sample["seg"].argmax(axis=0)
        return sample

    train_metrics = {}
    val_metrics = {
        "dice": MetricDice(
            pred="model.logits.head_seg",
            target="seg",
            pre_collect_process_func=pre_process_for_dice,
        )
    }

    # best_epoch_source = dict(
    #     monitor="validation.metrics.auc.macro_avg",  # can be any key from losses or metrics dictionaries
    #     mode="max",  # can be either min/max
    # )

    # Optimizer definition:
    ##############################################################################

    optimizer = optim.Adam(
        model.parameters(), lr=cfg.learning_rate, weight_decay=cfg.weight_decay
    )

    # Scheduler definition:
    ##############################################################################
    # lr_scheduler = cosine_annealing_with_warmup_lr_scheduler(optimizer, num_warmup_steps=cfg.num_warmup_steps, T_max=cfg.n_epochs, eta_min=0.00001)
    lr_scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=cfg.n_epochs)

    lr_sch_config = dict(scheduler=lr_scheduler)
    optimizers_and_lr_schs = dict(optimizer=optimizer)
    optimizers_and_lr_schs["lr_scheduler"] = lr_sch_config

    # CallBacks

    callbacks = [
        # BackboneFinetuning(unfreeze_backbone_at_epoch=8,),
        LearningRateMonitor(logging_interval="epoch"),
        # ModelCheckpoint(dirpath=model_dir, save_last=True)
    ]

    ## Training
    ##############################################################################
    ckpt_path = None
    if cfg.resume_training_from is not None:
        ckpt_path = cfg.ckpt_path

    # create instance of PL module - FuseMedML generic version
    pl_module = LightningModuleDefault(
        model_dir=model_dir,
        model=model,
        losses=losses,
        train_metrics=train_metrics,
        validation_metrics=val_metrics,
        test_metrics=val_metrics,
        # best_epoch_source=best_epoch_source,
        optimizers_and_lr_schs=optimizers_and_lr_schs,
        callbacks=callbacks,
        save_model=False,
        log_unit="epoch",
    )
    # create lightining trainer.
    devices = torch.cuda.device_count()
    pl_trainer = pl.Trainer(
        default_root_dir=model_dir,
        max_epochs=cfg.n_epochs,
        accelerator="gpu",
        devices=devices,
        strategy="ddp" if devices > 1 else "auto",
        num_sanity_val_steps=0,
        gradient_clip_val=cfg.grad_clip,
        deterministic=False,
        accumulate_grad_batches=cfg.accumulate_grad_batches,
        precision=cfg.precision,
    )
    if cfg.test_ckpt is not None:
        print(f"Test using ckpt: {cfg.test_ckpt}")
        pl_trainer.validate(pl_module, test_dl, ckpt_path=cfg.test_ckpt)
    else:
        # print(f"Training using ckpt: {ckpt_path}")
        pl_trainer.fit(pl_module, train_dl, val_dl, ckpt_path=ckpt_path)


if __name__ == "__main__":
    main()
