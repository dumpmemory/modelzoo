# Copyright 2022 Cerebras Systems.
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

import torch
import torch.nn as nn

from cerebras.modelzoo.common.registry import registry
from cerebras.modelzoo.losses.GPTLMHeadModelLoss import GPTLMHeadModelLoss
from cerebras.modelzoo.losses.LoadBalancingLoss import LoadBalancingLoss
from cerebras.modelzoo.models.multimodal.llava.modeling_llava import Llava
from cerebras.modelzoo.models.multimodal.multimodal_base_model import (
    ModalityType,
    MultimodalBaseModelWrapper,
)
from cerebras.modelzoo.trainer import summarize_scalar


@registry.register_model(
    "llava", datasetprocessor=["LlavaHDF5MapDataProcessor"]
)
class LlavaModel(MultimodalBaseModelWrapper, nn.Module):
    def __init__(self, params):
        super(LlavaModel, self).__init__()
        self._modalities = [ModalityType.IMAGE, ModalityType.TEXT]
        self._model_params = params.model
        model_params = self._model_params
        self.freeze = model_params.freeze
        self.image_feature_select_layer_idx = (
            model_params.image_feature_select_layer_idx
        )
        self.image_start_idx = model_params.image_start_idx
        self.image_feature_select_mode = model_params.image_feature_select_mode

        text_model_params = model_params.text_model
        if hasattr(text_model_params, 'moe'):
            self.moe_enabled = False
            text_model_params.moe = dict(num_experts=1)
        elif "num_experts" not in text_model_params.moe:
            self.moe_enabled = False
            text_model_params.moe = dict(num_experts=1)
        else:
            self.moe_enabled = text_model_params.moe["num_experts"] > 1

        if self.moe_enabled:
            self.load_balancing_loss_coef = text_model_params.moe.pop(
                "load_balancing_loss_coef", 0.01
            )
            self.load_balancing_loss_fn = LoadBalancingLoss(
                text_model_params.moe["num_experts"],
                text_model_params.moe["top_k"],
            )
        text_model_params.moe_params = text_model_params.moe

        self.model = self.build_model(model_params)
        vocab_size = getattr(model_params, ModalityType.TEXT.value).vocab_size
        self.loss_fn = GPTLMHeadModelLoss(
            vocab_size,
            model_params.loss_scaling,
            model_params.loss_weight,
        )
        del (
            model_params.loss_scaling,
            model_params.loss_weight,
        )

    @property
    def modalities(self):
        return self._modalities

    def _post_device_transfer(self):
        self.model.tie_weights()

    def build_model(self, model_params):
        # Build individual model towers for each modalities
        modality_models = self.build_modality_models(model_params)

        # Build projectors for each of the modalities
        projectors = self.build_projectors(model_params)

        # Initialize LLaVA model
        model = Llava(
            **modality_models,
            **projectors,
            freeze=self.freeze,
            image_feature_select_layer_idx=self.image_feature_select_layer_idx,
            image_start_idx=self.image_start_idx,
            image_feature_select_mode=self.image_feature_select_mode,
        )

        return model

    def forward(
        self,
        data,
        reduce_batch=True,
        output_logits=False,
    ):
        """The forward pass on the input data. This method
        returns the loss tensor if `output_logits` is False.
        If `output_logits` is True, the model call will also
        return the output logits tensor in addition to the
        loss as a (loss, lm_logits) tuple.

        This may be useful for performing post processing on
        the model's output logits.
        """

        # Note: attention_mask is a misnomer in this model and actually acts as
        # a loss mask. In the model computation its contents are ignored and
        # only its shape is used.
        _msg = (
            f"LLaVA model expects these data fields: \n"
            f"1. Either `image_data` (or) `image_embeddings` \n"
            f"2. Either `text_input_ids` (or) `text_embeddings` \n"
            f"3. `labels` \n"
            f"4. `loss_mask` \n"
            f"5. `key_padding_mask` \n"
        )
        assert (
            ("image_data" in data or "image_embeddings" in data)
            and ("text_input_ids" in data or "text_embeddings" in data)
            and "loss_mask" in data
            and "labels" in data
            and "key_padding_mask" in data
        ), _msg

        _msg = (
            f"The dtype for `text_input_ids`, "
            f"`loss_mask`, `labels`, `key_padding_mask "
            f"should be torch.int32"
        )
        assert (
            data["text_input_ids"].dtype == torch.int32
            and data["loss_mask"].dtype == torch.int32
            and data["labels"].dtype == torch.int32
            and data["key_padding_mask"].dtype == torch.int32
        ), _msg

        tgt_key_padding_mask = data["key_padding_mask"]
        if tgt_key_padding_mask is not None:
            tgt_key_padding_mask = tgt_key_padding_mask.to(
                data["image_data"].dtype
            )
            tgt_key_padding_mask = (
                tgt_key_padding_mask
                * torch.finfo(tgt_key_padding_mask.dtype).min
            )

        model_outputs = self.model(
            image_data=data.get("image_data"),
            text_input_ids=data.get("text_input_ids"),
            image_embeddings=data.get("image_embeddings"),
            text_embeddings=data.get("text_embeddings"),
            tgt_key_padding_mask=tgt_key_padding_mask,
            attention_span=data.get("attention_span"),
            position_ids=data.get("position_ids"),
            img_start_idx=data.get("img_start_idx"),
        )

        if self.moe_enabled:
            logits, routing_weights, expert_mask = model_outputs
        else:
            logits = model_outputs

        loss = self.loss_fn(
            logits,
            labels=data["labels"],
            attention_mask=data["loss_mask"],  # acts as a loss mask
            reduce_batch=reduce_batch,
        )

        if (
            self.moe_enabled
            and self.load_balancing_loss_coef > 0.0
            and self.training
        ):
            load_balance_loss = (
                self.load_balancing_loss_coef
                * self.load_balancing_loss_fn(
                    routing_weights,
                    expert_mask,
                    attention_mask=data["loss_mask"],  # acts as a loss mask
                )
            )
            summarize_scalar("load_balance_loss", load_balance_loss)
            loss = loss + load_balance_loss

        if output_logits:
            return loss, logits
        else:
            return loss
