from __future__ import annotations

import numpy as np
import pytest
from PIL import Image
from scipy import ndimage as ndi

from antigravity3d.genmedia.pbr_deriver import (
    BAKED_LIGHT_SKIP, METAL_ALBEDO_RANGE, PBRMaps, baked_light_score, delight, derive_pbr, edge_wear, make_tileable,
    normalize_albedo_range, prepare_albedo, roughness_breakup, seam_error, verify_tileable,
)


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


# ---- range normalisation, de-lighting, tileability, pre-grading ----------------------------------------------

def lum(img: Image.Image) -> np.ndarray:
    return np.asarray(img, np.float32) @ np.array([0.2126, 0.7152, 0.0722], np.float32)


def noise_albedo(n=128, lo=70, hi=190, seed=0, sigma=1.5) -> np.ndarray:
    """Periodic coloured noise scaled into [lo, hi] (float, 0-255)."""
    rng = np.random.default_rng(seed)
    a = np.stack([ndi.gaussian_filter(rng.random((n, n)), sigma, mode="wrap") for _ in range(3)], -1)
    return (a - a.min()) / np.ptp(a) * (hi - lo) + lo


def to_img(a: np.ndarray) -> Image.Image:
    return Image.fromarray(np.clip(np.rint(a), 0, 255).astype(np.uint8))


def lit_with(base: np.ndarray, kind: str) -> Image.Image:
    n = base.shape[0]
    if kind == "ramp":
        g = np.linspace(0.5, 1.5, n)[None, :, None]
    elif kind == "diagonal":
        g = (np.linspace(0.55, 1.2, n)[None, :] + np.linspace(0.0, 0.35, n)[:, None])[..., None]
    else:  # vignette
        yy, xx = np.mgrid[0:n, 0:n]
        g = (1.0 - 0.5 * (((yy - n / 2) ** 2 + (xx - n / 2) ** 2) / (2 * (n / 2) ** 2)))[..., None]
    return to_img(base * g)


def test_range_report_crushed_blacks_and_blown_whites():
    rng = np.random.default_rng(3)
    a = noise_albedo(96, 60, 200, seed=3)
    a[rng.random(a.shape[:2]) < 0.15] = 2.0  # crushed blacks
    a[rng.random(a.shape[:2]) < 0.10] = 253.0  # blown whites
    img = to_img(a)
    out, rep = normalize_albedo_range(img)
    assert rep.corrected and rep.frac_below_before > 0.1 and rep.frac_above_before > 0.07
    assert rep.frac_out_before == pytest.approx(rep.frac_below_before + rep.frac_above_before)
    assert rep.frac_out_after == 0.0
    y = lum(out)
    assert y.min() >= 30 - 0.5 and y.max() <= 240 + 0.5 and rep.lum_min_after >= 29.5 and rep.lum_max_after <= 240.5
    assert "fixed" in rep.one_line()
    # soft knee, not a clip: monotone in luminance, mid-range pixels untouched
    yi = lum(img)
    order = np.argsort(yi.ravel(), kind="stable")
    assert (np.diff(y.ravel()[order]) >= -1.0).all()
    mid = (yi > 90) & (yi < 180)
    assert np.abs(np.asarray(out, np.float32) - np.asarray(img, np.float32))[mid].max() <= 1.0
    again, rep2 = normalize_albedo_range(out)
    assert not rep2.corrected and again is out  # idempotent


def test_range_preserves_hue_and_saturation_of_corrected_pixels():
    img = Image.new("RGB", (8, 8), (10, 4, 2))  # dark brown, below the floor
    out, rep = normalize_albedo_range(img)
    r, g, b = (float(c) for c in out.getpixel((0, 0)))
    assert rep.corrected and 30 - 0.5 <= 0.2126 * r + 0.7152 * g + 0.0722 * b
    assert r > g > b and abs(g / r - 0.4) < 0.12 and abs(b / r - 0.2) < 0.12  # hue ratios kept
    black = normalize_albedo_range(Image.new("RGB", (4, 4), (0, 0, 0)))[0].getpixel((0, 0))
    assert black[0] == black[1] == black[2] >= 30


def test_range_metal_uses_high_floor_and_validates():
    img = to_img(noise_albedo(64, 60, 200))
    out, rep = normalize_albedo_range(img, metallic=True)
    assert (rep.lo, rep.hi) == METAL_ALBEDO_RANGE and rep.corrected and lum(out).min() >= METAL_ALBEDO_RANGE[0] - 0.5
    ok, rep2 = normalize_albedo_range(img)
    assert not rep2.corrected and ok is img
    with pytest.raises(ValueError):
        normalize_albedo_range(img, lo=200, hi=100)


@pytest.mark.parametrize("kind", ["ramp", "diagonal", "vignette"])
def test_baked_light_score_and_delight_on_known_lighting(kind):
    base = noise_albedo(128)
    clean = to_img(base)
    lit = lit_with(base, kind)
    s_clean, s_lit = baked_light_score(clean), baked_light_score(lit)
    assert s_clean < 0.05 and s_lit > 0.6
    out = delight(lit, tileable=False)
    assert baked_light_score(out) < 0.35 * s_lit  # substantially lower
    if kind != "vignette":
        assert baked_light_score(out) < 0.1
    # colour preserved: mean RGB within 2 levels of the lit input, and channel ratios intact
    m_in, m_out = np.asarray(lit, np.float32).mean((0, 1)), np.asarray(out, np.float32).mean((0, 1))
    assert np.abs(m_in - m_out).max() < 2.0
    assert np.abs(m_in / m_in.sum() - m_out / m_out.sum()).max() < 0.005


def test_baked_light_score_is_deterministic_bounded_and_flat_safe():
    img = lit_with(noise_albedo(64), "ramp")
    assert baked_light_score(img) == baked_light_score(img.copy())
    assert baked_light_score(Image.new("RGB", (32, 32), (120, 100, 90))) == 0.0
    assert 0.0 <= baked_light_score(to_img(noise_albedo(64, 0, 255))) <= 1.0
    mild = lit_with(noise_albedo(128), "ramp")
    weak = to_img(noise_albedo(128) * np.linspace(0.97, 1.03, 128)[None, :, None])
    assert baked_light_score(weak) < baked_light_score(mild)
    assert baked_light_score(img.resize((300, 300))) > 0.5  # large inputs are scored on a downsample


def test_delight_strength_zero_is_near_identity_and_tileable_mode_wraps():
    lit = lit_with(noise_albedo(64), "ramp")
    assert np.abs(np.asarray(delight(lit, strength=0.0), np.int16) - np.asarray(lit, np.int16)).max() <= 1
    img = to_img(noise_albedo(64))
    a = np.asarray(delight(img, tileable=True), np.int16)
    b = np.asarray(delight(Image.fromarray(np.roll(np.asarray(img), (9, 17), (0, 1))), tileable=True), np.int16)
    assert np.abs(np.roll(a, (9, 17), (0, 1)) - b).max() <= 2  # wrap-aware: no border handling artefacts


def test_specular_suppression_inpaints_small_highlights_only():
    a = noise_albedo(128, 80, 160, sigma=3)
    a[40:44, 50:54] = 252.0
    img = to_img(a)
    kept = np.asarray(delight(img, suppress_specular=False))
    gone = np.asarray(delight(img, suppress_specular=True))
    assert kept[40:44, 50:54].mean() > 200 and abs(gone[40:44, 50:54].mean() - np.asarray(img)[30:36, 50:54].mean()) < 40
    assert gone.max() < 200
    white = Image.new("RGB", (64, 64), (250, 250, 250))  # a legitimately white surface is not a highlight
    assert np.asarray(delight(white, suppress_specular=True)).mean() > 245


def smooth_nonperiodic(n=64, seed=2) -> np.ndarray:
    rng = np.random.default_rng(seed)
    a = np.stack([ndi.gaussian_filter(rng.random((n, n)), 3.0, mode="reflect") for _ in range(3)], -1)
    a = (a - a.min()) / np.ptp(a) * 100 + 60
    return a + np.linspace(0, 70, n)[None, :, None] + np.linspace(0, 40, n)[:, None, None]  # ramps break both seams


def test_make_tileable_fixes_hard_seam_in_both_axes():
    img = to_img(smooth_nonperiodic())
    rep0 = verify_tileable(img)
    assert not rep0.ok and rep0.seam_x > 5 and rep0.seam_y > 3 and seam_error(img) > 4
    fixed = make_tileable(img)
    rep1 = verify_tileable(fixed)
    assert rep1.ok and 0.3 < rep1.seam_x < 1.5 and 0.3 < rep1.seam_y < 1.5 and seam_error(fixed) < 1.5
    assert fixed.size == img.size
    c = np.asarray(img, np.int16)  # interior untouched: only the blend band changes
    assert np.array_equal(np.asarray(fixed, np.int16)[8:-8, 8:-8], c[8:-8, 8:-8])
    # a single bad axis only touches that axis
    xonly = to_img(noise_albedo(64, 60, 160) + np.linspace(0, 60, 64)[None, :, None])  # ramp across x only
    assert verify_tileable(xonly).seam_x > 1.5 > verify_tileable(xonly).seam_y
    fx = np.asarray(make_tileable(xonly), np.int16)
    assert np.array_equal(fx[:, 8:-8], np.asarray(xonly, np.int16)[:, 8:-8])  # nothing but the x borders changed
    assert verify_tileable(make_tileable(xonly)).ok
    with pytest.raises(ValueError):
        make_tileable(img, blend=0.0)


def test_make_tileable_is_noop_within_tolerance():
    img = to_img(noise_albedo(64))
    assert verify_tileable(img).ok and make_tileable(img) is img
    assert verify_tileable(img, tol=0.0001).ok is False  # tol is a real knob
    assert make_tileable(img, tol=0.0001) is not img


def test_prepare_albedo_is_idempotent_on_clean_texture():
    clean = to_img(noise_albedo(64, 70, 190))
    out, rep = prepare_albedo(clean)
    assert out is clean and not rep.changed and not rep.delit and not rep.range.corrected
    assert rep.baked_light_before < BAKED_LIGHT_SKIP and rep.tile_before.ok
    line = rep.one_line()
    assert "\n" not in line and line.startswith("albedo prep:") and "skip" in line
    out2, rep2 = prepare_albedo(out)
    assert np.array_equal(np.asarray(out2), np.asarray(clean)) and not rep2.changed


def test_prepare_albedo_chains_all_three_stages_and_converges():
    base = noise_albedo(128, 0, 255, seed=5)
    dirty = to_img(base * np.linspace(0.4, 1.5, 128)[None, :, None] + np.linspace(0, 40, 128)[:, None, None])
    out, rep = prepare_albedo(dirty, tileable=False)
    assert rep.delit and rep.baked_light_before > 0.6 and rep.baked_light_after < 0.15 and rep.tile_before is None
    assert baked_light_score(out) < 0.15 and lum(out).min() >= 29.5 and lum(out).max() <= 240.5
    assert "delit" in rep.one_line()
    again, rep2 = prepare_albedo(out, tileable=False)
    assert not rep2.changed and again is out
    # seam stage
    seamy = to_img(smooth_nonperiodic())
    fixed, rep3 = prepare_albedo(seamy, tileable=True)
    assert rep3.tile_before.ok is False and rep3.tile_after.fixed and rep3.tile_after.ok and "[fixed]" in rep3.one_line()
    assert prepare_albedo(Image.new("RGB", (32, 32), (20, 20, 20)), metallic=True)[1].range.lo == METAL_ALBEDO_RANGE[0]


def test_derive_pbr_prepare_is_opt_in():
    lit = lit_with(noise_albedo(64, 0, 255), "ramp")
    plain = derive_pbr(lit, tileable=False)
    assert np.array_equal(np.asarray(plain.albedo), np.asarray(lit))  # default behaviour untouched
    prepped = derive_pbr(lit, tileable=False, prepare=True)
    assert baked_light_score(prepped.albedo) < baked_light_score(lit) and lum(prepped.albedo).min() >= 29.5
    assert prepped.normal.size == (64, 64)


def test_large_tileable_lowpass_matches_wrapped_convolution():
    from antigravity3d.genmedia.pbr_deriver import _blur

    arr = np.random.default_rng(921).random((144, 257), dtype=np.float32)
    spatial = ndi.gaussian_filter(arr, sigma=32, mode="wrap")
    assert np.allclose(_blur(arr, 32, tileable=True), spatial, atol=2e-5)
