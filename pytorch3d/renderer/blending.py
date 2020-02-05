#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates. All rights reserved.


import numpy as np
from typing import NamedTuple
import torch

# Example functions for blending the top K colors per pixel using the outputs
# from rasterization.
# NOTE: All blending function should return an RGBA image per batch element


# Data class to store blending params with defaults
class BlendParams(NamedTuple):
    sigma: float = 1e-4
    gamma: float = 1e-4
    background_color = (1.0, 1.0, 1.0)


def hard_rgb_blend(colors, fragments) -> torch.Tensor:
    """
    Naive blending of top K faces to return an RGBA image
      - **RGB** - choose color of the closest point i.e. K=0
      - **A** - 1.0

    Args:
        colors: (N, H, W, K, 3) RGB color for each of the top K faces per pixel.
        fragments: the outputs of rasterization. From this we use
            - pix_to_face: LongTensor of shape (N, H, W, K) specifying the indices
              of the faces (in the packed representation) which
              overlap each pixel in the image. This is used to
              determine the output shape.
    Returns:
        RGBA pixel_colors: (N, H, W, 4)
    """
    N, H, W, K = fragments.pix_to_face.shape
    device = fragments.pix_to_face.device
    pixel_colors = torch.ones((N, H, W, 4), dtype=colors.dtype, device=device)
    pixel_colors[..., :3] = colors[..., 0, :]
    return torch.flip(pixel_colors, [1])


def sigmoid_alpha_blend(colors, fragments, blend_params) -> torch.Tensor:
    """
    Silhouette blending to return an RGBA image
      - **RGB** - choose color of the closest point.
      - **A** - blend based on the 2D distance based probability map [0].

    Args:
        colors: (N, H, W, K, 3) RGB color for each of the top K faces per pixel.
        fragments: the outputs of rasterization. From this we use
            - pix_to_face: LongTensor of shape (N, H, W, K) specifying the indices
              of the faces (in the packed representation) which
              overlap each pixel in the image.
            - dists: FloatTensor of shape (N, H, W, K) specifying
              the 2D euclidean distance from the center of each pixel
              to each of the top K overlapping faces.

    Returns:
        RGBA pixel_colors: (N, H, W, 4)

    [0] Liu et al, 'Soft Rasterizer: A Differentiable Renderer for Image-based
        3D Reasoning', ICCV 2019
    """
    N, H, W, K = fragments.pix_to_face.shape
    pixel_colors = torch.ones(
        (N, H, W, 4), dtype=colors.dtype, device=colors.device
    )
    mask = fragments.pix_to_face >= 0

    # The distance is negative if a pixel is inside a face and positive outside
    # the face. Therefore use -1.0 *  fragments.dists to get the correct sign.
    prob = torch.sigmoid(-fragments.dists / blend_params.sigma) * mask

    # The cumulative product ensures that alpha will be 1 if at least 1 face
    # fully covers the pixel as for that face prob will be 1.0
    # TODO: investigate why torch.cumprod backwards is very slow for large
    # values of K.
    # Temporarily replace this with exp(sum(log))) using the fact that
    # a*b = exp(log(a*b)) = exp(log(a) + log(b))
    # alpha = 1.0 - torch.cumprod((1.0 - prob), dim=-1)[..., -1]

    alpha = 1.0 - torch.exp(torch.log((1.0 - prob)).sum(dim=-1))

    pixel_colors[..., :3] = colors[..., 0, :]  # Hard assign for RGB
    pixel_colors[..., 3] = alpha

    pixel_colors = torch.clamp(pixel_colors, min=0, max=1.0)
    return torch.flip(pixel_colors, [1])


def softmax_rgb_blend(colors, fragments, blend_params) -> torch.Tensor:
    """
    RGB and alpha channel blending to return an RGBA image based on the method
    proposed in [0]
      - **RGB** - blend the colors based on the 2D distance based probability map and
        relative z distances.
      - **A** - blend based on the 2D distance based probability map.

    Args:
        colors: (N, H, W, K, 3) RGB color for each of the top K faces per pixel.
        fragments: namedtuple with outputs of rasterization. We use properties
            - pix_to_face: LongTensor of shape (N, H, W, K) specifying the indices
              of the faces (in the packed representation) which
              overlap each pixel in the image.
            - dists: FloatTensor of shape (N, H, W, K) specifying
              the 2D euclidean distance from the center of each pixel
              to each of the top K overlapping faces.
            - zbuf: FloatTensor of shape (N, H, W, K) specifying
              the interpolated depth from each pixel to to each of the
              top K overlapping faces.
        blend_params: instance of BlendParams dataclass containing properties
            - sigma: float, parameter which controls the width of the sigmoid
              function used to calculate the 2D distance based probability.
              Sigma controls the sharpness of the edges of the shape.
            - gamma: float, parameter which controls the scaling of the
              exponential function used to control the opacity of the color.
            - background_color: (3) element list/tuple/torch.Tensor specifying
              the RGB values for the background color.

    Returns:
        RGBA pixel_colors: (N, H, W, 4)

    [0] Shichen Liu et al, 'Soft Rasterizer: A Differentiable Renderer for
    Image-based 3D Reasoning'
    """
    N, H, W, K = fragments.pix_to_face.shape
    device = fragments.pix_to_face.device
    pix_colors = torch.ones(
        (N, H, W, 4), dtype=colors.dtype, device=colors.device
    )
    background = blend_params.background_color
    if not torch.is_tensor(background):
        background = torch.tensor(
            background, dtype=torch.float32, device=device
        )

    # Background color
    delta = np.exp(1e-10 / blend_params.gamma) * 1e-10
    delta = torch.tensor(delta, device=device)

    # Near and far clipping planes.
    # TODO: add zfar/znear as input params.
    zfar = 100.0
    znear = 1.0

    # Mask for padded pixels.
    mask = fragments.pix_to_face >= 0

    # Sigmoid probability map based on the distance of the pixel to the face.
    prob_map = torch.sigmoid(-fragments.dists / blend_params.sigma) * mask

    # The cumulative product ensures that alpha will be 1 if at least 1 face
    # fully covers the pixel as for that face prob will be 1.0
    # TODO: investigate why torch.cumprod backwards is very slow for large
    # values of K.
    # Temporarily replace this with exp(sum(log))) using the fact that
    # a*b = exp(log(a*b)) = exp(log(a) + log(b))
    # alpha = 1.0 - torch.cumprod((1.0 - prob), dim=-1)[..., -1]

    alpha = 1.0 - torch.exp(torch.log((1.0 - prob_map)).sum(dim=-1))

    # Weights for each face. Adjust the exponential by the max z to prevent
    # overflow. zbuf shape (N, H, W, K), find max over K.
    # TODO: there may still be some instability in the exponent calculation.
    z_inv = (zfar - fragments.zbuf) / (zfar - znear) * mask
    z_inv_max = torch.max(z_inv, dim=-1).values[..., None]
    weights_num = prob_map * torch.exp((z_inv - z_inv_max) / blend_params.gamma)

    # Normalize weights.
    # weights_num shape: (N, H, W, K). Sum over K and divide through by the sum.
    denom = weights_num.sum(dim=-1)[..., None] + delta
    weights = weights_num / denom

    # Sum: weights * textures + background color
    weighted_colors = (weights[..., None] * colors).sum(dim=-2)
    weighted_background = (delta / denom) * background
    pix_colors[..., :3] = weighted_colors + weighted_background
    pix_colors[..., 3] = alpha

    # Clamp colors to the range 0-1 and flip y axis.
    pix_colors = torch.clamp(pix_colors, min=0, max=1.0)
    return torch.flip(pix_colors, [1])