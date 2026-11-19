from __future__ import annotations

import numpy as np
import pytest
from PIL import Image
from scipy import ndimage as ndi

from antigravity3d.genmedia.pbr_deriver import PBRMaps, delight, derive_pbr, edge_wear, roughness_breakup, seam_error


def tile(n=64, seed=1) -> Image.Image:
    """Periodic texture: noise blurred with wrap-around, so it tiles perfectly."""
    rng = np.random.default_rng(seed)
    a = np.stack([ndi.gaussian_filter(rng.random((n, n)), 2.0, mode="wrap") for _ in range(3)], -1)
    a = (a - a.min()) / np.ptp(a)
    return Image.fromarray((a * 255).astype(np.uint8))


def vecs(img: Image.Image) -> np.ndarray:
    return np.asarray(img, np.float32) / 255 * 2 - 1


def test_outputs_and_channel_ranges():
    m = derive_pbr(tile(), metallic_hint=0.3)
    assert isinstance(m, PBRMaps)
    for im in (m.albedo, m.orm, m.normal):
        assert im.size == (64, 64) and im.mode == "RGB"
    orm = np.asarray(m.orm, np.float32) / 255
    assert orm.min() >= 0 and orm.max() <= 1
    assert np.allclose(orm[..., 2], 0.3, atol=1 / 255)
    assert orm[..., 0].min() < 1.0 and orm[..., 1].std() > 0.005  # AO and roughness carry detail
    assert orm[..., 1].min() >= 0.04 - 1 / 255


def test_normal_unit_length_and_up_facing():
    n = vecs(derive_pbr(tile()).normal)
    ln = np.linalg.norm(n, axis=-1)
    assert np.abs(ln - 1).max() < 0.03  # 8-bit quantisation only
    assert (n[..., 2] > 0).all()
    flat = vecs(derive_pbr(Image.new("RGB", (16, 16), (128, 128, 128))).normal)
    assert np.allclose(flat, [0, 0, 1], atol=0.02)


def test_tileable_normal_is_seamless_roll_equivariant():
    img = tile()
    a = np.asarray(derive_pbr(img, tileable=True).normal, np.int16)
    rolled = Image.fromarray(np.roll(np.asarray(img), (13, 21), (0, 1)))
    b = np.asarray(derive_pbr(rolled, tileable=True).normal, np.int16)
    assert np.abs(np.roll(a, (13, 21), (0, 1)) - b).max() <= 2  # seam moves like content: no border artefacts
    nt = np.asarray(derive_pbr(rolled, tileable=False).normal, np.int16)
    assert np.abs(np.roll(a, (13, 21), (0, 1)) - nt).max() > 2  # non-tileable reflects at borders
    assert seam_error(derive_pbr(img).normal) < 1.5


def test_directx_flips_green_only():
    img = tile()
    gl, dx = (np.asarray(derive_pbr(img, convention=c).normal, np.int16) for c in ("opengl", "directx"))
    assert np.abs(gl[..., 0] - dx[..., 0]).max() <= 1 and np.abs(gl[..., 2] - dx[..., 2]).max() <= 1
    assert np.abs((255 - gl[..., 1]) - dx[..., 1]).max() <= 1
    with pytest.raises(ValueError):
        derive_pbr(img, convention="vulkan")  # type: ignore[arg-type]


def test_normal_strength_scales_tilt():
    img = tile()
    weak, strong = (vecs(derive_pbr(img, normal_strength=s).normal)[..., 2].mean() for s in (0.5, 4.0))
    assert strong < weak < 1.0


def test_roughness_breakup_adds_variation_deterministically():
    flat = Image.fromarray(np.dstack([np.full((64, 64), v, np.uint8) for v in (255, 128, 0)]))
    out = roughness_breakup(flat, 1.0, seed=3)
    g = np.asarray(out)[..., 1]
    assert g.std() > 5 and np.array_equal(np.asarray(out), np.asarray(roughness_breakup(flat, 1.0, seed=3)))
    assert np.array_equal(np.asarray(out)[..., [0, 2]], np.asarray(flat)[..., [0, 2]])
    assert np.array_equal(np.asarray(roughness_breakup(flat, 0.0)), np.asarray(flat))


def test_edge_wear_lightens_edges_only_with_strength():
    m = derive_pbr(tile())
    worn = np.asarray(edge_wear(m.albedo, m.normal, 1.0), np.float32)
    base = np.asarray(m.albedo, np.float32)
    assert worn.mean() > base.mean() and np.array_equal(np.asarray(edge_wear(m.albedo, m.normal, 0.0)), np.asarray(m.albedo))


def test_delight_flattens_baked_gradient():
    base = np.asarray(tile(), np.float32)
    ramp = np.linspace(0.4, 1.0, 64, dtype=np.float32)[None, :, None]
    lit = Image.fromarray((base * ramp).astype(np.uint8))

    def col_spread(im):
        return np.asarray(im, np.float32).mean(axis=(0, 2)).std()

    assert col_spread(delight(lit, tileable=False)) < 0.5 * col_spread(lit)


def test_seam_error_detects_discontinuity():
    ramp = Image.fromarray(np.tile(np.linspace(0, 255, 64, dtype=np.uint8), (64, 1)))
    assert seam_error(ramp) > 10
    assert seam_error(tile()) < 1.5
    assert seam_error(Image.new("RGB", (8, 8), (5, 5, 5))) == 0.0
