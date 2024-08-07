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

"""
Config classes of T5 data Configs

"""

from dataclasses import dataclass

from cerebras.modelzoo.common.registry import registry
from cerebras.modelzoo.data.nlp.t5.config import T5DynamicDataProcessorConfig


@registry.register_data_config("TransformerDynamicDataProcessor")
@dataclass
class TransformerDynamicDataProcessorConfig(T5DynamicDataProcessorConfig):
    pass
