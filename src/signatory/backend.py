# Copyright 2019 Patrick Kidger. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
#    http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# =========================================================================
"""Provides certain low-level helpers."""


import torch


def interpret_basepoint(basepoint, path):
    if basepoint is True:
        basepoint_value = torch.zeros((path.shape[0], path.shape[2]), dtype=path.dtype, device=path.device)
    elif isinstance(basepoint, torch.Tensor):
        basepoint_value = basepoint
        basepoint = True
    else:
        basepoint_value = torch.Tensor()
    return basepoint, basepoint_value


def interpret_initial(initial):
    if isinstance(initial, torch.Tensor):
        initial_value = initial
        initial = True
    else:
        initial_value = torch.Tensor()
        initial = False
    return initial, initial_value
