#!/usr/bin/env python
"""Test Fastai integration
---
id: 0.0.4
check-ext-wandb: {}
assert:
  - :wandb:runs_len: 1
  - :wandb:runs[0][project]: integrations_testing
  - :wandb:runs[0][config][lr_0]: 0.01
  - :wandb:runs[0][summary][epoch]: 2
  - :wandb:runs[0][exitcode]: 0
"""

from fastai.vision.all import *
from fastai.callback.wandb import *
import wandb

wandb.init(project='integrations_testing')
path = untar_data(URLs.MNIST_TINY)
mnist = DataBlock(blocks = (ImageBlock(cls=PILImageBW),CategoryBlock),
                  get_items = get_image_files,
                  splitter = GrandparentSplitter(),
                  get_y = parent_label)

dls = mnist.dataloaders(path/"train", bs=32)
learn = cnn_learner(dls, resnet18, metrics=error_rate)
learn.fit(2, 1e-2, cbs=WandbCallback(log_preds=False, log_model=False))
wandb.finish()