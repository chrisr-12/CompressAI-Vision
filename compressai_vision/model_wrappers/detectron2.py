# Copyright (c) 2022-2023, InterDigital Communications, Inc
# All rights reserved.

# Redistribution and use in source and binary forms, with or without
# modification, are permitted (subject to the limitations in the disclaimer
# below) provided that the following conditions are met:

# * Redistributions of source code must retain the above copyright notice,
#   this list of conditions and the following disclaimer.
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
# * Neither the name of InterDigital Communications, Inc nor the names of its
#   contributors may be used to endorse or promote products derived from this
#   software without specific prior written permission.

# NO EXPRESS OR IMPLIED LICENSES TO ANY PARTY'S PATENT RIGHTS ARE GRANTED BY
# THIS LICENSE. THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND
# CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT
# NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A
# PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS;
# OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
# WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR
# OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF
# ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

# TODO (racapef) check/add detectron2 license header

import torch
from typing import Dict, List

from .base_wrapper import BaseWrapper

from detectron2.checkpoint import DetectionCheckpointer
from detectron2.config import get_cfg
from detectron2.modeling import build_model


__all__ = [
    "faster_rcnn_X_101_32x8d_FPN_3x",
    "mask_rcnn_X_101_32x8d_FPN_3x",
    "faster_rcnn_R_50_FPN_3x",
    "mask_rcnn_R_50_FPN_3x",
]

class Rcnn_R_50_X_101_FPN(BaseWrapper):
    def __init__(self, device='cpu', **kwargs):
        super().__init__()

        self.cfg = get_cfg()
        self.cfg.merge_from_file(kwargs['cfg'])
        self.model = build_model(self.cfg).to(device).eval()
        
        self.backbone = self.model.backbone
        self.proposal_generator = self.model.proposal_generator
        self.roi_heads = self.model.roi_heads
        self.postprocess = self.model._postprocess
        DetectionCheckpointer(self.model).load(kwargs['weight'])

        assert self.proposal_generator is not None

    @torch.no_grad()
    def input_to_features(self, x):
        """Computes deep features at the intermediate layer(s) all the way from the input"""
        imgs = self.model.preprocess_image(x)
        return self.backbone(imgs.tensor), imgs.image_sizes

    @torch.no_grad()
    def features_to_output(self, x, org_img_size: Dict, input_img_size: List):
        """

        Detectron2 source codes are referenced for this function, specifically the class "GeneralizedRCNN"
        Unnecessary parts for split inference are removed or modified properly. 

        Please find the license statement in the downloaded original Detectron2 source codes or at here:
        https://github.com/facebookresearch/detectron2/blob/main/LICENSE

        """

        """Complete the downstream task from the intermediate deep features"""
        
        class dummy:
            def __init__(self, img_size:list):
                self.image_sizes = img_size
        cdummy = dummy(input_img_size)

        proposals, _ = self.proposal_generator(cdummy, x, None)
        results, _ = self.roi_heads(cdummy, x, proposals, None)

        assert (
            not torch.jit.is_scripting()
        ), "Scripting is not supported for postprocess."
        return self.model._postprocess(results, [org_img_size,], input_img_size)

    @torch.no_grad()
    def forward(self, x):
        """Complete the downstream task with end-to-end manner all the way from the input"""
        return self.model(x)

    def channels2frame(self, x, num_channels_in_width, num_channels_in_height):
        """rehape tensor channels to a frame"""
        raise NotImplemented
    
    def frame2channels(self, x, tensor_shape):
        """reshape frames of channels into tensor(s)"""
        raise NotImplemented
    
    def get_cfg(self):
        return self.cfg

    #[TODO (choih): To be reused for some purpose]
    def preInputTensor(self, img, img_id):
        """

        :param img: numpy BGR image (h,w,3)

        """
        height, width = img.shape[:2]
        if self.aug is not None:
            image = self.aug.get_transform(img).apply_image(img)
        image = torch.as_tensor(image.astype("float32").transpose(2, 0, 1))

        inputs = {
            "image": image,
            "height": height,
            "width": width,
            "image_id": img_id,
        }
        return [
            inputs,
        ]

class faster_rcnn_X_101_32x8d_FPN_3x(Rcnn_R_50_X_101_FPN):
    def __init__(self, device='cpu', **kwargs):
        super().__init__(device,  **kwargs)


class mask_rcnn_X_101_32x8d_FPN_3x(Rcnn_R_50_X_101_FPN):
    def __init__(self, device='cpu', **kwargs):
        super().__init__(device,  **kwargs)


class faster_rcnn_R_50_FPN_3x(Rcnn_R_50_X_101_FPN):
    def __init__(self, device='cpu', **kwargs):
        super().__init__(device,  **kwargs)

class mask_rcnn_R_50_FPN_3x(Rcnn_R_50_X_101_FPN):
    def __init__(self, device='cpu', **kwargs):
        super().__init__(device,  **kwargs)

