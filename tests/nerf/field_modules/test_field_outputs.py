"""
Field output tests
"""
import torch
from torch import nn

from mattport.nerf.field_modules.field_heads import DensityFieldHead, FieldHead, FieldHeadNames, RGBFieldHead


def test_field_output():
    """Test render output"""
    in_dim = 6
    out_dim = 4
    field_head_name = FieldHeadNames.DENSITY
    activation = nn.ReLU()
    render_head = FieldHead(in_dim=in_dim, out_dim=out_dim, field_head_name=field_head_name, activation=activation)
    assert render_head.get_out_dim() == out_dim

    x = torch.ones((9, in_dim))
    render_head(x)


def test_density_output():
    """Test rgb output"""
    in_dim = 6
    density_head = DensityFieldHead(in_dim)
    assert density_head.get_out_dim() == 1

    x = torch.ones((9, in_dim))
    density_head(x)


def test_rgb_output():
    """Test rgb output"""
    in_dim = 6
    rgb_head = RGBFieldHead(in_dim)
    assert rgb_head.get_out_dim() == 3

    x = torch.ones((9, in_dim))
    rgb_head(x)


if __name__ == "__main__":
    test_field_output()
    test_density_output()
    test_rgb_output()
