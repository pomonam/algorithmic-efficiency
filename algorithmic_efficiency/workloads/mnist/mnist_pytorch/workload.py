"""MNIST workload implemented in PyTorch."""

from collections import OrderedDict
import contextlib
from typing import Dict, Iterator, Optional, Tuple

import torch
from torch import nn
import torch.distributed as dist
import torch.nn.functional as F
from torch.nn.parallel import DistributedDataParallel as DDP

from algorithmic_efficiency import init_utils
from algorithmic_efficiency import param_utils
from algorithmic_efficiency import spec
from algorithmic_efficiency.pytorch_utils import pytorch_setup
from algorithmic_efficiency.workloads.mnist.workload import BaseMnistWorkload

USE_PYTORCH_DDP, RANK, DEVICE, N_GPUS = pytorch_setup()


class _Model(nn.Module):

  def __init__(self) -> None:
    super().__init__()
    input_size = 28 * 28
    num_hidden = 128
    num_classes = 10
    self.net = nn.Sequential(
        OrderedDict([('layer1',
                      torch.nn.Linear(input_size, num_hidden, bias=True)),
                     ('layer1_sig', torch.nn.Sigmoid()),
                     ('layer2',
                      torch.nn.Linear(num_hidden, num_classes, bias=True))]))

  def reset_parameters(self) -> None:
    for m in self.net.modules():
      if isinstance(m, nn.Linear):
        init_utils.pytorch_default_init(m)

  def forward(self, x: spec.Tensor) -> spec.Tensor:
    x = x.view(x.size()[0], -1)
    return self.net(x)


class MnistWorkload(BaseMnistWorkload):

  def _build_input_queue(
      self,
      data_rng: spec.RandomState,
      split: str,
      data_dir: str,
      global_batch_size: int,
      cache: Optional[bool] = None,
      repeat_final_dataset: Optional[bool] = None,
      num_batches: Optional[int] = None) -> Iterator[Dict[str, spec.Tensor]]:
    per_device_batch_size = int(global_batch_size / N_GPUS)

    # Only create and iterate over tf input pipeline in one Python process to
    # avoid creating too many threads.
    if RANK == 0:
      np_iter = super()._build_input_queue(data_rng,
                                           split,
                                           data_dir,
                                           global_batch_size,
                                           num_batches,
                                           repeat_final_dataset)
    while True:
      if RANK == 0:
        batch = next(np_iter)  # pylint: disable=stop-iteration-return
        inputs = torch.as_tensor(
            batch['inputs'], dtype=torch.float32, device=DEVICE)
        targets = torch.as_tensor(
            batch['targets'], dtype=torch.long, device=DEVICE)
        weights = torch.as_tensor(
            batch['weights'], dtype=torch.bool, device=DEVICE)
        # Send batch to other devices when using DDP.
        if USE_PYTORCH_DDP:
          dist.broadcast(inputs, src=0)
          inputs = inputs[0]
          dist.broadcast(targets, src=0)
          targets = targets[0]
          dist.broadcast(weights, src=0)
          weights = weights[0]
        else:
          inputs = inputs.view(-1, *inputs.shape[2:])
          targets = targets.view(-1, *targets.shape[2:])
          weights = weights.view(-1, *weights.shape[2:])
      else:
        inputs = torch.empty((N_GPUS, per_device_batch_size, 28, 28, 1),
                             dtype=torch.float32,
                             device=DEVICE)
        dist.broadcast(inputs, src=0)
        inputs = inputs[RANK]
        targets = torch.empty((N_GPUS, per_device_batch_size),
                              dtype=torch.long,
                              device=DEVICE)
        dist.broadcast(targets, src=0)
        targets = targets[RANK]
        weights = torch.empty((N_GPUS, per_device_batch_size),
                              dtype=torch.bool,
                              device=DEVICE)
        dist.broadcast(weights, src=0)
        weights = weights[RANK]

      batch = {
          'inputs': inputs.permute(0, 3, 1, 2),
          'targets': targets,
          'weights': weights
      }
      yield batch

  def init_model_fn(
      self,
      rng: spec.RandomState,
      dropout_rate: Optional[float] = None,
      aux_dropout_rate: Optional[float] = None) -> spec.ModelInitState:
    """Dropout is unused."""
    del dropout_rate
    del aux_dropout_rate
    torch.random.manual_seed(rng[0])
    model = _Model()
    self._param_shapes = param_utils.pytorch_param_shapes(model)
    self._param_types = param_utils.pytorch_param_types(self._param_shapes)
    model.to(DEVICE)
    if N_GPUS > 1:
      if USE_PYTORCH_DDP:
        model = DDP(model, device_ids=[RANK], output_device=RANK)
      else:
        model = torch.nn.DataParallel(model)
    return model, None

  def is_output_params(self, param_key: spec.ParameterKey) -> bool:
    return param_key in ['net.layer2.weight', 'net_layer2.bias']

  def model_fn(
      self,
      params: spec.ParameterContainer,
      augmented_and_preprocessed_input_batch: Dict[str, spec.Tensor],
      model_state: spec.ModelAuxiliaryState,
      mode: spec.ForwardPassMode,
      rng: spec.RandomState,
      update_batch_norm: bool) -> Tuple[spec.Tensor, spec.ModelAuxiliaryState]:
    del model_state
    del rng
    del update_batch_norm
    model = params
    if mode == spec.ForwardPassMode.EVAL:
      model.eval()
    contexts = {
        spec.ForwardPassMode.EVAL: torch.no_grad,
        spec.ForwardPassMode.TRAIN: contextlib.nullcontext
    }
    with contexts[mode]():
      logits_batch = model(augmented_and_preprocessed_input_batch['inputs'])
    return logits_batch, None

  # Does NOT apply regularization, which is left to the submitter to do in
  # `update_params`.
  def loss_fn(self,
              label_batch: spec.Tensor,
              logits_batch: spec.Tensor,
              mask_batch: Optional[spec.Tensor] = None,
              label_smoothing: float = 0.0) -> Tuple[spec.Tensor, spec.Tensor]:
    """Return (correct scalar average loss, 1-d array of per-example losses)."""
    per_example_losses = F.cross_entropy(
        logits_batch,
        label_batch,
        reduction='none',
        label_smoothing=label_smoothing)
    # `mask_batch` is assumed to be shape [batch].
    if mask_batch is not None:
      per_example_losses *= mask_batch
      n_valid_examples = mask_batch.sum()
    else:
      n_valid_examples = len(per_example_losses)
    summed_loss = per_example_losses.sum()
    return summed_loss / n_valid_examples, per_example_losses

  def _eval_model(
      self,
      params: spec.ParameterContainer,
      batch: Dict[str, spec.Tensor],
      model_state: spec.ModelAuxiliaryState,
      rng: spec.RandomState) -> Dict[spec.Tensor, spec.ModelAuxiliaryState]:
    """Return the mean accuracy and loss as a dict."""
    logits, _ = self.model_fn(
        params,
        batch,
        model_state,
        spec.ForwardPassMode.EVAL,
        rng,
        update_batch_norm=False)
    weights = batch.get('weights')
    if weights is None:
      weights = torch.ones(len(logits)).to(DEVICE)
    _, predicted = torch.max(logits.data, 1)
    # Number of correct predictions.
    accuracy = ((predicted == batch['targets']) * weights).sum()
    _, per_example_losses = self.loss_fn(batch['targets'], logits, weights)
    loss = per_example_losses.sum()
    return {'accuracy': accuracy, 'loss': loss}
