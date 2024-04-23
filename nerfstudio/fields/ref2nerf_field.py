# Copyright 2022 the Regents of the University of California, Nerfstudio Team and contributors. All rights reserved.
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
Field for compound nerf model, adds scene contraction and image embeddings to instant ngp
"""


from typing import Dict, Literal, Optional, Tuple

import torch
from torch import Tensor, nn

from nerfstudio.cameras.rays import RaySamples
from nerfstudio.data.scene_box import SceneBox
from nerfstudio.field_components.activations import trunc_exp
from nerfstudio.field_components.embedding import Embedding
from nerfstudio.field_components.encodings import NeRFEncoding, SHEncoding
from nerfstudio.field_components.field_heads import (
    FieldHeadNames,
    PredNormalsFieldHead,
    SemanticFieldHead,
    TransientDensityFieldHead,
    TransientRGBFieldHead,
    UncertaintyFieldHead,
)
from nerfstudio.field_components.mlp import MLP, MLPWithHashEncoding
from nerfstudio.field_components.spatial_distortions import SpatialDistortion
from nerfstudio.fields.base_field import Field, get_normalized_directions
from nerfstudio.model_components.renderers import RGBRenderer

class Ref2NerfField(Field):
    """Compound Field

    Args:
        aabb: parameters of scene aabb bounds
        num_images: number of images in the dataset
        num_layers: number of hidden layers
        hidden_dim: dimension of hidden layers
        geo_feat_dim: output geo feat dimensions
        num_levels: number of levels of the hashmap for the base mlp
        base_res: base resolution of the hashmap for the base mlp
        max_res: maximum resolution of the hashmap for the base mlp
        log2_hashmap_size: size of the hashmap for the base mlp
        num_layers_color: number of hidden layers for color network
        num_layers_transient: number of hidden layers for transient network
        features_per_level: number of features per level for the hashgrid
        hidden_dim_color: dimension of hidden layers for color network
        hidden_dim_transient: dimension of hidden layers for transient network
        appearance_embedding_dim: dimension of appearance embedding
        transient_embedding_dim: dimension of transient embedding
        use_transient_embedding: whether to use transient embedding
        use_semantics: whether to use semantic segmentation
        num_semantic_classes: number of semantic classes
        use_pred_normals: whether to use predicted normals
        use_average_appearance_embedding: whether to use average appearance embedding or zeros for inference
        spatial_distortion: spatial distortion to apply to the scene
    """

    aabb: Tensor

    def __init__(
        self,
        aabb: Tensor,
        num_images: int,
        num_layers: int = 3,
        hidden_dim: int = 64,
        geo_feat_dim: int = 15,
        num_levels: int = 16,
        base_res: int = 16,
        max_res: int = 2048,
        log2_hashmap_size: int = 19,
        num_layers_color: int = 3,
        num_layers_transient: int = 2,
        features_per_level: int = 2,
        hidden_dim_color: int = 64,
        hidden_dim_transient: int = 64,
        appearance_embedding_dim: int = 32,
        transient_embedding_dim: int = 16,
        use_transient_embedding: bool = False,
        use_semantics: bool = False,
        num_semantic_classes: int = 100,
        pass_semantic_gradients: bool = False,
        use_pred_normals: bool = False,
        use_average_appearance_embedding: bool = False,
        spatial_distortion: Optional[SpatialDistortion] = None,
        average_init_density: float = 1.0,
        implementation: Literal["tcnn", "torch"] = "torch",
    ) -> None:
        super().__init__()

        self.register_buffer("aabb", aabb)
        self.geo_feat_dim = 64

        self.register_buffer("max_res", torch.tensor(max_res))
        self.register_buffer("num_levels", torch.tensor(num_levels))
        self.register_buffer("log2_hashmap_size", torch.tensor(log2_hashmap_size))

        self.spatial_distortion = spatial_distortion
        self.num_images = num_images
        self.appearance_embedding_dim = appearance_embedding_dim
        if self.appearance_embedding_dim > 0:
            self.embedding_appearance = Embedding(self.num_images, self.appearance_embedding_dim)
        else:
            self.embedding_appearance = None
        self.use_average_appearance_embedding = use_average_appearance_embedding
        self.use_transient_embedding = use_transient_embedding
        self.use_semantics = use_semantics
        self.use_pred_normals = use_pred_normals
        self.pass_semantic_gradients = pass_semantic_gradients
        self.base_res = base_res
        self.average_init_density = average_init_density
        self.step = 0

        self.direction_encoding = SHEncoding(
            levels=4,
            implementation=implementation,
        )

        self.position_encoding = NeRFEncoding(
            in_dim=3, num_frequencies=10, min_freq_exp=0, max_freq_exp=9, 
            include_input=True, implementation=implementation)

        self.num_layers = 4
        self.hidden_dim = 128
        self.num_layers_color = 2
        self.mlp_base = MLP(
            in_dim= self.position_encoding.get_out_dim(),
            num_layers=self.num_layers,
            layer_width=self.hidden_dim,
            out_dim=1 + self.geo_feat_dim,
            activation=nn.ReLU(),
            out_activation=None,
            implementation=implementation,
        )

        # self.mlp_base = MLPWithHashEncoding(
        #     num_levels=num_levels,
        #     min_res=base_res,
        #     max_res=max_res,
        #     log2_hashmap_size=log2_hashmap_size,
        #     features_per_level=features_per_level,
        #     num_layers=num_layers,
        #     layer_width=hidden_dim,
        #     out_dim=1 + self.geo_feat_dim,
        #     activation=nn.ReLU(),
        #     out_activation=None,
        #     implementation=implementation,
        # )

        self.mlp_base_independent = MLP(
            in_dim= self.position_encoding.get_out_dim(),
            num_layers=self.num_layers + 2,
            layer_width=self.hidden_dim,
            out_dim=1 + 3 + self.geo_feat_dim,
            activation=nn.ReLU(),
            out_activation=None,
            implementation=implementation,

        )

        # self.mlp_base_independent = MLPWithHashEncoding(
        #     num_levels=num_levels,
        #     min_res=base_res,
        #     max_res=max_res,
        #     log2_hashmap_size=log2_hashmap_size,
        #     features_per_level=features_per_level,
        #     num_layers=num_layers,
        #     layer_width=hidden_dim,
        #     out_dim=1 + 3 + self.geo_feat_dim,
        #     activation=nn.ReLU(),
        #     out_activation=None,
        #     implementation=implementation,
        # )

        self.feature_dim = 64
        self.mlp_head = MLP(
            in_dim=self.direction_encoding.get_out_dim() + self.geo_feat_dim + self.appearance_embedding_dim,
            num_layers=self.num_layers_color,
            layer_width=self.hidden_dim,
            out_dim=1 + self.feature_dim,
            activation=nn.ReLU(),
            out_activation=None,
            implementation=implementation,
        )
        self.mlp_offset = MLP(
            in_dim=self.direction_encoding.get_out_dim() + self.geo_feat_dim + self.appearance_embedding_dim,
            num_layers=self.num_layers_color,
            layer_width=self.hidden_dim,
            out_dim=3,
            activation=nn.ReLU(),
            out_activation=None,
            implementation=implementation,
        )

        self.mlp_decoder = MLP(
            in_dim = self.feature_dim,
            num_layers=2,
            layer_width=24,
            out_dim=3,
            activation=nn.ReLU(),
            out_activation=nn.Sigmoid(),
            implementation=implementation,
        )

        self.mlp_gate = MLP(
            in_dim = self.feature_dim,
            num_layers=2,
            layer_width=24,
            out_dim=1,
            activation=nn.ReLU(),
            out_activation=None,
            implementation=implementation,
        )


        self.renderer_offset = RGBRenderer(background_color='black')

    def get_density(self, ray_samples: RaySamples) -> Tuple[Tensor, Tensor]:
        """Computes and returns the densities."""
        if self.spatial_distortion is not None:
            positions = ray_samples.frustums.get_positions()
            positions = self.spatial_distortion(positions)
            positions = (positions + 2.0) / 4.0
        else:
            positions = SceneBox.get_normalized_positions(ray_samples.frustums.get_positions(), self.aabb)
        # Make sure the tcnn gets inputs between 0 and 1.
     #   selector = ((positions > 0.0) & (positions < 1.0)).all(dim=-1)
     #   positions = positions * selector[..., None]
        self._sample_locations = positions
        if not self._sample_locations.requires_grad:
            self._sample_locations.requires_grad = True
        encode_xyz = self.position_encoding(positions.view(-1, 3))
        #positions_flat = positions.view(-1, 3)
        #h = self.mlp_base(positions_flat).view(*ray_samples.frustums.shape, -1)
        encode_xyz = encode_xyz.view(-1, encode_xyz.shape[-1])
        h = self.mlp_base(encode_xyz).view(*ray_samples.frustums.shape, -1)
        density_before_activation, base_mlp_out = torch.split(h, [1, self.geo_feat_dim], dim=-1)
        self._density_before_activation = density_before_activation

        # Rectifying the density with an exponential is much more stable than a ReLU or
        # softplus, because it enables high post-activation (float32) density outputs
        # from smaller internal (float16) parameters.
        density = self.average_init_density * trunc_exp(density_before_activation.to(positions))
     #   density = density * selector[..., None]
        return density, base_mlp_out

    def get_outputs(
        self, ray_samples: RaySamples, density_embedding: Optional[Tensor] = None
    ) -> Dict[FieldHeadNames, Tensor]:
        assert density_embedding is not None
        outputs = {}
        if ray_samples.camera_indices is None:
            raise AttributeError("Camera indices are not provided.")
        camera_indices = ray_samples.camera_indices.squeeze()
        directions = get_normalized_directions(ray_samples.frustums.directions)
        directions_flat = directions.view(-1, 3)
        d = self.direction_encoding(directions_flat)

        outputs_shape = ray_samples.frustums.directions.shape[:-1]


        h = torch.cat(
            [
                d,
                density_embedding.view(-1, self.geo_feat_dim),
            ],
            dim=-1,
        )
        rgb = self.mlp_head(h).view(*outputs_shape, -1).to(directions)
        outputs.update({FieldHeadNames.RGB: rgb})

        return outputs


    def forward(self, ray_samples: RaySamples, compute_normals: bool = False) -> Dict[FieldHeadNames, Tensor]:
        """Evaluates the field at points along the ray.

        Args:
            ray_samples: Samples to evaluate field on.
        """
        density, density_embedding = self.get_density(ray_samples)
        weight = ray_samples.get_weights(density)

        directions = get_normalized_directions(ray_samples.frustums.directions)
        directions_flat = directions.view(-1, 3)
        d = self.direction_encoding(directions_flat)

        outputs_shape = ray_samples.frustums.directions.shape[:-1]

        # Determine the offset delta_x
        h_offset = torch.cat(
            [
                d,
                density_embedding.view(-1, self.geo_feat_dim),
            ],
            dim=-1,
        )

        offset = self.mlp_offset(h_offset).view(*outputs_shape, -1)
        delta_x = torch.cumsum(weight * offset, dim=-2)

        ray_samples.frustums.set_offsets(delta_x)
        positions = SceneBox.get_normalized_positions(ray_samples.frustums.get_positions(), self.aabb)
        directions = get_normalized_directions(ray_samples.frustums.directions)
        directions_flat = directions.view(-1, 3)
        d = self.direction_encoding(directions_flat)
       
        #Calculate the view-dependent and view-independent outputs
        #Make sure the tcnn gets inputs between 0 and 1.

       # selector = ((positions > 0.0) & (positions < 1.0)).all(dim=-1)
       # positions = positions * selector[..., None]
       # self._sample_locations = positions
        if not self._sample_locations.requires_grad:
            self._sample_locations.requires_grad = True
        encode_xyz = self.position_encoding(positions.view(-1, 3))
        encode_xyz = encode_xyz.view(-1, encode_xyz.shape[-1])
        h = self.mlp_base_independent(encode_xyz).view(*ray_samples.frustums.shape, -1)
        #positions_flat = positions.view(-1, 3)
        #h = self.mlp_base_independent(positions_flat).view(*ray_samples.frustums.shape, -1)
        density_vi, color_vi, base_mlp_out = torch.split(h, [1, 3, self.geo_feat_dim], dim=-1)
        density_vi = self.average_init_density * trunc_exp(density_vi.to(positions))
       # density_vi = density_vi * selector[..., None]        

        h_vd = torch.cat(
            [
                d,
                base_mlp_out.view(-1, self.geo_feat_dim),
            ],
            dim=-1,
        )

        density_vd, color_vd = torch.split(self.mlp_head(h_vd).view(*outputs_shape, -1).to(directions), [1, self.feature_dim], dim=-1)
        density_vd = self.average_init_density * trunc_exp(density_vd.to(positions))
    #    density_vd = density_vd * selector[..., None]


        # Render view-independent color
        weight_vi = ray_samples.get_weights(density_vi)
        color_vi = torch.sigmoid(color_vi)
        rgb_vi = torch.sum(weight_vi*color_vi, dim=-2)
        #rgb_vi = self.renderer_offset(weight_vi, color_vi, ray_samples.frustums.directions)

        # Render view-dependent color
        weight_vd = ray_samples.get_weights(density_vd)
        #weight_vd = ray_samples.get_weights_and_transmittance(density, density_vd)
        feature_map = torch.sum(weight_vd*color_vd, dim=-2)

        rgb_vd = self.mlp_decoder(feature_map)


        # Blender vi and vd
        alpha = self.mlp_gate(feature_map).view(-1, 1)
        rgb = alpha * rgb_vd + rgb_vi
        #rgb = torch.nan_to_num(rgb)
        #rgb = torch.clamp(rgb, 0.0, 1.0)

        field_outputs = {}
       # field_outputs = self.get_outputs(ray_samples, density_embedding=density_embedding)
        field_outputs[FieldHeadNames.SH] = delta_x # type: ignore
        field_outputs[FieldHeadNames.DENSITY] = density_vi
        field_outputs[FieldHeadNames.RGB] = rgb
        field_outputs[FieldHeadNames.VI_RGB] = rgb_vi
        field_outputs[FieldHeadNames.VD_RGB] = rgb_vd


        return field_outputs