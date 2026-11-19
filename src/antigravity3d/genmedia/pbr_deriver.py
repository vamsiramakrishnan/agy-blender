"""Deterministic PBR map synthesis and auto-fixers. numpy/scipy/PIL only; no model calls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
from PIL import Image
from scipy import ndimage as ndi

Convention = Literal["opengl", "directx"]


@dataclass(frozen=True)
class PBRMaps:
    albedo: Image.Image
    orm: Image.Image  # R=AO, G=roughness, B=metallic
    normal: Image.Image


def _mode(tileable: bool) -> str:
    return "wrap" if tileable else "reflect"


def _rgb(img: Image.Image) -> np.ndarray:
    return np.asarray(img.convert("RGB"), np.float32) / 255.0


def _img(a: np.ndarray) -> Image.Image:
    return Image.fromarray(np.clip(np.rint(a * 255.0), 0, 255).astype(np.uint8))


def _lum(rgb: np.ndarray) -> np.ndarray:
    return rgb @ np.array([0.2126, 0.7152, 0.0722], np.float32)


def _blur(a: np.ndarray, sigma: float, tileable: bool) -> np.ndarray:
    return ndi.gaussian_filter(a, sigma, mode=_mode(tileable))


def height_from_albedo(albedo: Image.Image, tileable: bool = True) -> np.ndarray:
    """Luminance high-passed to drop lighting gradients, scaled into [0, 1]."""
    lum = _lum(_rgb(albedo))
    hp = lum - _blur(lum, max(lum.shape) / 8.0, tileable)
    return 0.5 + 0.5 * hp / (np.abs(hp).max() + 1e-6)


def normal_from_height(h: np.ndarray, strength: float, convention: Convention, tileable: bool) -> Image.Image:
    m = _mode(tileable)
    dx = ndi.sobel(h, axis=1, mode=m) / 8.0
    dy = ndi.sobel(h, axis=0, mode=m) / 8.0  # per image row (downwards)
    # OpenGL: +Y up, so green = +dh/drow; DirectX flips green.
    gy = dy if convention == "opengl" else -dy
    n = np.stack([-dx * strength * 8, gy * strength * 8, np.ones_like(h)], -1)
    n /= np.linalg.norm(n, axis=-1, keepdims=True)
    return _img(n * 0.5 + 0.5)


def derive_pbr(albedo: Image.Image, *, metallic_hint: float = 0.0, normal_strength: float = 2.0,
               convention: Convention = "opengl", tileable: bool = True, roughness_contrast: float = 1.0) -> PBRMaps:
    if convention not in ("opengl", "directx"):
        raise ValueError(f"unknown normal convention {convention!r}")
    rgb = _rgb(albedo)
    h = height_from_albedo(albedo, tileable)
    normal = normal_from_height(h, normal_strength, convention, tileable)

    cavity = _blur(h, 3.0, tileable) - h  # positive in pits
    ao = 1.0 - np.clip(cavity * 6.0, 0.0, 0.8)

    lum_local = _blur(_lum(rgb), 2.0, tileable)
    sat = rgb.max(-1) - rgb.min(-1)
    rough = (1.0 - lum_local) - 0.25 * sat  # darker / more saturated reads rougher
    rough = 0.6 + roughness_contrast * (rough - rough.mean())
    rough = np.clip(rough * (1.0 - 0.5 * metallic_hint), 0.04, 1.0)

    orm = np.stack([ao, rough, np.full_like(ao, float(np.clip(metallic_hint, 0, 1)))], -1)
    return PBRMaps(albedo.convert("RGB").copy(), _img(orm), normal)


def roughness_breakup(orm: Image.Image, strength: float = 0.5, *, seed: int = 0, tileable: bool = True) -> Image.Image:
    """CG-plastic fix: add deterministic multi-octave roughness variation to the G channel."""
    a = np.asarray(orm.convert("RGB"), np.float32) / 255.0
    rng = np.random.default_rng(seed)
    noise = np.zeros(a.shape[:2], np.float32)
    for sigma, amp in ((2.0, 0.25), (8.0, 0.5), (32.0, 1.0)):
        n = ndi.gaussian_filter(rng.standard_normal(noise.shape).astype(np.float32), sigma, mode=_mode(tileable))
        noise += amp * n / (n.std() + 1e-6)
    noise /= np.abs(noise).max() + 1e-6
    a[..., 1] = np.clip(a[..., 1] + 0.35 * strength * noise, 0.04, 1.0)
    return _img(a)


def edge_wear(albedo: Image.Image, normal: Image.Image, strength: float = 0.5, *, tileable: bool = True) -> Image.Image:
    """Lighten and desaturate albedo along convex-ish edges found from normal-map variation."""
    n = np.asarray(normal.convert("RGB"), np.float32) / 255.0 * 2 - 1
    m = _mode(tileable)
    curv = sum(np.abs(ndi.laplace(n[..., c], mode=m)) for c in (0, 1))
    curv = _blur(curv, 1.0, tileable)
    mask = np.clip(curv / (np.percentile(curv, 98) + 1e-6), 0, 1)[..., None]
    rgb = _rgb(albedo)
    worn = 0.5 * rgb + 0.5 * (0.35 * rgb + 0.65 * _lum(rgb)[..., None]) + 0.12
    return _img(rgb * (1 - mask * strength) + worn * mask * strength)


def delight(albedo: Image.Image, *, strength: float = 1.0, tileable: bool = True) -> Image.Image:
    """Flatten baked low-frequency lighting by dividing out a heavily blurred luminance."""
    rgb = _rgb(albedo)
    low = _blur(_lum(rgb), max(rgb.shape[:2]) / 6.0, tileable)
    gain = (low.mean() / np.maximum(low, 1e-3)) ** strength
    return _img(rgb * np.clip(gain, 0.25, 4.0)[..., None])


def seam_error(img: Image.Image) -> float:
    """Wrap-around edge discontinuity relative to ordinary neighbour differences.

    ~1.0 for a seamless tile (the wrapped edge looks like any other pixel pair); grows with the
    jump across the seam. Mean over the horizontal and vertical seams.
    """
    a = np.asarray(img.convert("RGB"), np.float32) / 255.0
    ratios = []
    for axis in (0, 1):
        b = np.moveaxis(a, axis, 0)
        edge = np.abs(b[0] - b[-1]).mean()
        inner = np.abs(np.diff(b, axis=0)).mean()
        ratios.append(edge / (inner + 1e-6))
    return float(np.mean(ratios))
