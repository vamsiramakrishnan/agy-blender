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
               convention: Convention = "opengl", tileable: bool = True, roughness_contrast: float = 1.0,
               prepare: bool = False) -> PBRMaps:
    """`prepare=True` first runs the deterministic albedo pre-grading (`prepare_albedo`: de-light, range, tileability)."""
    if convention not in ("opengl", "directx"):
        raise ValueError(f"unknown normal convention {convention!r}")
    if prepare:
        albedo, _ = prepare_albedo(albedo, metallic=metallic_hint >= 0.5, tileable=tileable)
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
    for div, amp in ((128, 0.25), (32, 0.5), (8, 1.0)):  # fine -> coarse octaves, relative to image size
        n = ndi.gaussian_filter(rng.standard_normal(noise.shape).astype(np.float32), max(a.shape[:2]) / div, mode=_mode(tileable))
        noise += amp * (n - n.mean()) / (n.std() + 1e-6)
    noise = (noise - noise.mean()) / (noise.std() + 1e-6)  # zero-mean, unit-std: strength sets the roughness std directly
    a[..., 1] = np.clip(a[..., 1] + 0.12 * strength * noise, 0.04, 1.0)
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


def _plane_basis(h: int, w: int) -> np.ndarray:
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    return np.stack([np.ones_like(x).ravel(), (2 * x / max(w - 1, 1) - 1).ravel(), (2 * y / max(h - 1, 1) - 1).ravel()], -1)


def _suppress_specular(rgb: np.ndarray, tileable: bool, *, max_area_frac: float = 0.004, min_contrast: float = 0.12) -> np.ndarray:
    """Inpaint small, saturated, near-white blobs that stand out from their surroundings (baked highlights)."""
    lum = _lum(rgb)
    cand = (rgb.min(-1) > 0.86) & ((rgb.max(-1) - rgb.min(-1)) < 0.14)
    if not cand.any():
        return rgb
    lab, n = ndi.label(cand)
    area = ndi.sum(cand, lab, index=np.arange(1, n + 1))
    keep = area <= max(4.0, max_area_frac * lum.size)
    free = (~cand).astype(np.float32)
    bg = _blur(lum * free, 6.0, tileable) / np.maximum(_blur(free, 6.0, tileable), 1e-3)
    contrast = np.asarray(ndi.mean(lum - bg, lab, index=np.arange(1, n + 1)))
    keep &= contrast > min_contrast
    if not keep.any():
        return rgb
    mask = np.isin(lab, np.flatnonzero(keep) + 1)
    mask = ndi.binary_dilation(mask, iterations=2)  # soft halo around the blob belongs to the highlight too
    out, valid = rgb.copy(), (~mask).astype(np.float32)
    todo = mask.copy()
    for sigma in (2.0, 5.0, 12.0, 30.0):  # normalised convolution, widening until every masked pixel has support
        num = np.stack([_blur(rgb[..., c] * valid, sigma, tileable) for c in range(3)], -1)
        den = _blur(valid, sigma, tileable)[..., None]
        ok = todo & (den[..., 0] > 1e-3)
        out[ok] = (num / np.maximum(den, 1e-6))[ok]
        todo &= ~ok
        if not todo.any():
            break
    return out


def delight(albedo: Image.Image, *, strength: float = 1.0, tileable: bool = True, sigma_frac: float = 1 / 6,
            fit_plane: bool | None = None, max_gain: float = 4.0, suppress_specular: bool = False) -> Image.Image:
    """Flatten baked low-frequency lighting.

    Illumination is estimated in log-luminance space: (optionally) a least-squares plane, which removes a directional
    gradient without the border bias of any blur, then a large-radius Gaussian (sigma = sigma_frac * image size) that is
    toroidal when `tileable` (reflective otherwise) for the remaining smooth variation. It is divided out of RGB (hue and
    saturation untouched) and the result is re-centred so mean luminance equals the original. `strength` scales the
    correction in log space (0 = identity). `fit_plane` defaults to `not tileable` (a plane is not periodic).
    `suppress_specular` first inpaints small saturated near-white highlight blobs from their surroundings.
    """
    rgb = _rgb(albedo)
    if suppress_specular:
        rgb = _suppress_specular(rgb, tileable)
    lum = _lum(rgb)
    logl = np.log(lum + 0.02)
    est = np.zeros_like(logl)
    h, w = logl.shape
    if (not tileable) if fit_plane is None else fit_plane:
        basis = _plane_basis(h, w)
        coef, *_ = np.linalg.lstsq(basis, logl.ravel(), rcond=None)
        est = (basis[:, 1:] @ coef[1:]).reshape(h, w)  # gradient part only; the mean stays
    resid = logl - est
    low = _blur(resid, max(h, w) * sigma_frac, tileable)
    est = est + low - low.mean()
    gain = np.clip(np.exp(-strength * est), 1.0 / max_gain, max_gain)
    out = rgb * gain[..., None]
    m_out = _lum(out).mean()
    if m_out > 1e-6:
        out = out * (lum.mean() / m_out)  # re-centre: overall brightness and colour balance are preserved
    return _img(np.clip(out, 0.0, 1.0))


def baked_light_score(albedo: Image.Image) -> float:
    """Deterministic estimate in [0, 1] of how much directional / low-frequency lighting remains in an albedo.

    On a <=128px copy, log-luminance is fitted with a quadratic polynomial surface. Let I be its non-constant part:
      r_dir  = variance share of the linear (x, y) terms: a directional light gradient is odd-symmetric
      r_low  = variance share of the whole fitted surface (adds vignette / AO-like quadratic terms)
      amp    = min(1, std(I) / 0.12): lighting must be strong enough in absolute terms (log-luminance) to matter
    score = clip(amp * (2.5 * r_dir + 1.5 * (r_low - r_dir)), 0, 1)
    Flat or noisy-but-evenly-lit textures score ~0; a texture lit by a strong ramp or vignette scores high. It is a
    cheap screen (skip the model when the answer is obvious), not a replacement for judgement on borderline values.
    """
    a = _rgb(albedo.copy().convert("RGB"))
    h, w = a.shape[:2]
    if max(h, w) > 128:
        scale = 128 / max(h, w)
        a = _rgb(albedo.convert("RGB").resize((max(8, round(w * scale)), max(8, round(h * scale))), Image.BOX))
        h, w = a.shape[:2]
    logl = np.log(_lum(a) + 0.02)
    total = float(logl.var())
    if total < 1e-8:
        return 0.0
    y, x = np.mgrid[0:h, 0:w].astype(np.float64)
    xn, yn = 2 * x / max(w - 1, 1) - 1, 2 * y / max(h - 1, 1) - 1
    full = np.stack([np.ones(h * w), xn.ravel(), yn.ravel(), (xn * xn).ravel(), (xn * yn).ravel(), (yn * yn).ravel()], -1)
    coef, *_ = np.linalg.lstsq(full, logl.ravel().astype(np.float64), rcond=None)
    lin = (full[:, 1:3] @ coef[1:3]).reshape(h, w)
    surf = (full[:, 1:] @ coef[1:]).reshape(h, w)
    r_dir, r_low = float(lin.var()) / total, float(surf.var()) / total
    amp = min(1.0, float(surf.std()) / 0.12)
    return float(np.clip(amp * (2.5 * r_dir + 1.5 * max(r_low - r_dir, 0.0)), 0.0, 1.0))


# --- range normalisation -----------------------------------------------------------------------------------
DIELECTRIC_ALBEDO_RANGE = (30, 240)  # sRGB 8-bit luminance of physically plausible non-metal albedo
METAL_ALBEDO_RANGE = (180, 255)  # metals reflect most of the light: a high floor
RANGE_TOL = 0.5  # half an 8-bit level: quantisation slack when judging "out of range"
KNEE_FRAC = 0.15  # soft knee width as a fraction of the target span


@dataclass(frozen=True)
class RangeReport:
    lo: float
    hi: float
    metallic: bool
    frac_out_before: float  # fraction of pixels with luminance outside [lo, hi]
    frac_out_after: float
    frac_below_before: float
    frac_above_before: float
    corrected: bool
    lum_min_before: float
    lum_max_before: float
    lum_min_after: float
    lum_max_after: float

    def one_line(self) -> str:
        if not self.corrected:
            return f"range[{self.lo:g}-{self.hi:g}] ok ({self.frac_out_before:.1%} out)"
        return (f"range[{self.lo:g}-{self.hi:g}] fixed: out {self.frac_out_before:.1%}->{self.frac_out_after:.1%} "
                f"(below {self.frac_below_before:.1%}, above {self.frac_above_before:.1%})")


def _soft_knee(y: np.ndarray, lo: float, hi: float, k: float) -> np.ndarray:
    """Identity on [lo+k, hi-k]; exponential roll-off (C1 at the knee) approaching lo / hi asymptotically."""
    out = y.copy()
    top = y > hi - k
    out[top] = (hi - k) + k * (1.0 - np.exp(-(y[top] - (hi - k)) / k))
    bot = y < lo + k
    out[bot] = (lo + k) - k * (1.0 - np.exp(-((lo + k) - y[bot]) / k))
    return out


def normalize_albedo_range(albedo: Image.Image, *, lo: float = DIELECTRIC_ALBEDO_RANGE[0], hi: float = DIELECTRIC_ALBEDO_RANGE[1],
                           metallic: bool = False) -> tuple[Image.Image, RangeReport]:
    """Bring luminance into the plausible sRGB albedo range with a soft-knee remap that preserves hue and saturation.

    Dielectric default 30-240. `metallic=True` raises the floor/ceiling to at least METAL_ALBEDO_RANGE (180-255). Only
    luminance is remapped (RGB is scaled by the luminance ratio, so chromaticity and local contrast outside the knee
    zones are untouched; pixels whose scaled colour would leave gamut are desaturated toward their luminance, hue kept).
    The input is returned unchanged (same object) when no pixel is out of range.
    """
    if metallic:
        lo, hi = max(lo, METAL_ALBEDO_RANGE[0]), max(hi, METAL_ALBEDO_RANGE[1])
    if not 0 <= lo < hi <= 255:
        raise ValueError(f"bad albedo range {lo}-{hi}")
    rgb = _rgb(albedo)
    y = _lum(rgb) * 255.0
    below, above = float((y < lo - RANGE_TOL).mean()), float((y > hi + RANGE_TOL).mean())
    before = below + above
    if before == 0.0:
        rep = RangeReport(lo, hi, metallic, 0.0, 0.0, 0.0, 0.0, False, float(y.min()), float(y.max()), float(y.min()), float(y.max()))
        return albedo, rep
    y2 = _soft_knee(y, lo, hi, KNEE_FRAC * (hi - lo))
    scale = (y2 / np.maximum(y, 0.5))[..., None]
    out = rgb * scale
    yo = (y2 / 255.0)[..., None]
    dark = (y < 0.5)[..., None]
    out = np.where(dark, yo, out)  # black has no hue: becomes neutral at the new floor
    over = out.max(-1, keepdims=True)
    t = np.where(over > 1.0, (1.0 - yo) / np.maximum(over - yo, 1e-6), 1.0)  # gamut compression toward luminance
    out = yo + (out - yo) * np.clip(t, 0.0, 1.0)
    img = _img(out)
    y_after = _lum(np.asarray(img, np.float32) / 255.0) * 255.0
    after = float(((y_after < lo - RANGE_TOL) | (y_after > hi + RANGE_TOL)).mean())
    rep = RangeReport(lo, hi, metallic, before, after, below, above, True, float(y.min()), float(y.max()),
                      float(y_after.min()), float(y_after.max()))
    return img, rep


# --- tileability ---------------------------------------------------------------------------------------------
TILE_TOL = 1.5  # seam_error at/below this is visually seamless (a clean tile measures ~1.0)


@dataclass(frozen=True)
class TileReport:
    seam_x: float  # left/right wrap seam, ~1.0 when seamless
    seam_y: float  # top/bottom wrap seam
    tol: float
    ok: bool
    fixed: bool = False

    @property
    def seam_error(self) -> float:
        return (self.seam_x + self.seam_y) / 2

    def one_line(self) -> str:
        return f"tile seam x={self.seam_x:.2f} y={self.seam_y:.2f} (tol {self.tol:g}) " + ("ok" if self.ok else "SEAM") + (" [fixed]" if self.fixed else "")


def _seam_ratios(img: Image.Image) -> tuple[float, float]:
    """(x, y) seam ratios: wrapped-edge difference over the mean neighbour difference, per axis."""
    a = np.asarray(img.convert("RGB"), np.float32) / 255.0
    out = []
    for axis in (1, 0):  # x seam = across columns
        b = np.moveaxis(a, axis, 0)
        edge = np.abs(b[0] - b[-1]).mean()
        inner = np.abs(np.diff(b, axis=0)).mean()
        out.append(float(edge / (inner + 1e-6)))
    return out[0], out[1]


def verify_tileable(img: Image.Image, *, tol: float = TILE_TOL) -> TileReport:
    """Toroidal seam check in both axes (same measure as `seam_error`, reported per axis)."""
    sx, sy = _seam_ratios(img)
    return TileReport(sx, sy, tol, max(sx, sy) <= tol)


def _crossfade_axis(a: np.ndarray, axis: int, blend: float) -> np.ndarray:
    n = a.shape[axis]
    b = max(2, int(round(blend * n)))
    t = np.clip((np.arange(n) + 0.5) / b, 0, 1)
    t = np.minimum(t, t[::-1])  # ramp 0 -> 1 across `blend` of the size at both borders
    w = (t * t * (3 - 2 * t)).astype(np.float32)
    shape = [1] * a.ndim
    shape[axis] = n
    w = w.reshape(shape)
    return a * w + np.roll(a, n // 2, axis=axis) * (1 - w)  # border pixels come from the half-shifted copy: adjacent in the source


def make_tileable(img: Image.Image, blend: float = 0.1, *, tol: float = TILE_TOL) -> Image.Image:
    """Cross-fade the borders against a half-shifted copy so the wrap-around seam becomes an ordinary pixel step.

    Only axes whose seam exceeds `tol` are touched; if neither does, `img` itself is returned (no-op). Content more
    than `blend` of the size away from a border is unchanged.
    """
    if not 0 < blend <= 0.5:
        raise ValueError("blend must be in (0, 0.5]")
    sx, sy = _seam_ratios(img)
    if max(sx, sy) <= tol:
        return img
    a = np.asarray(img.convert("RGB"), np.float32)
    if sx > tol:
        a = _crossfade_axis(a, 1, blend)
    if sy > tol:
        a = _crossfade_axis(a, 0, blend)
    return Image.fromarray(np.clip(np.rint(a), 0, 255).astype(np.uint8))


# --- the pre-grading stage -----------------------------------------------------------------------------------------
BAKED_LIGHT_SKIP = 0.12  # below this the albedo is treated as evenly lit and de-lighting is skipped


@dataclass(frozen=True)
class AlbedoReport:
    baked_light_before: float
    baked_light_after: float
    delit: bool
    range: RangeReport
    tile_before: TileReport | None
    tile_after: TileReport | None
    changed: bool

    def one_line(self) -> str:
        parts = [f"baked_light {self.baked_light_before:.2f}" + (f"->{self.baked_light_after:.2f} (delit)" if self.delit else " (skip)"),
                 self.range.one_line()]
        if self.tile_before is not None:
            parts.append(self.tile_before.one_line() if not self.tile_after or not self.tile_after.fixed
                         else f"tile seam {self.tile_before.seam_error:.2f}->{self.tile_after.seam_error:.2f} [fixed]")
        return "albedo prep: " + "; ".join(parts)


def prepare_albedo(albedo: Image.Image, *, metallic: bool = False, tileable: bool = True) -> tuple[Image.Image, AlbedoReport]:
    """Deterministic pre-grading before any model sees the albedo: de-light -> range normalise -> tileable verify/fix.

    Each step is a no-op when its check passes (baked_light_score below BAKED_LIGHT_SKIP, nothing out of range, seam within
    tolerance), so running it on an already-clean texture returns it unchanged and the stage is idempotent.
    """
    img = albedo.convert("RGB")
    bl0 = baked_light_score(img)
    delit = bl0 >= BAKED_LIGHT_SKIP
    if delit:
        img = delight(img, tileable=tileable)
    bl1 = baked_light_score(img) if delit else bl0
    img, rr = normalize_albedo_range(img, metallic=metallic)
    t0 = t1 = None
    if tileable:
        t0 = verify_tileable(img)
        if not t0.ok:
            img = make_tileable(img)
            v = verify_tileable(img)
            t1 = TileReport(v.seam_x, v.seam_y, v.tol, v.ok, fixed=True)
        else:
            t1 = t0
    changed = delit or rr.corrected or bool(t1 and t1.fixed)
    return (img if changed else albedo), AlbedoReport(bl0, bl1, delit, rr, t0, t1, changed)


def seam_error(img: Image.Image) -> float:
    """Wrap-around edge discontinuity relative to ordinary neighbour differences.

    ~1.0 for a seamless tile (the wrapped edge looks like any other pixel pair); grows with the
    jump across the seam. Mean over the horizontal and vertical seams.
    """
    return float(np.mean(_seam_ratios(img)))
