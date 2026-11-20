"""LOD pop diffing: render adjacent LODs from the same camera at their switch distance and score the jump.

screen_fraction: the object's bounding sphere (radius = half bbox diagonal of the reference LOD)
spans this fraction of the frame height, so the object appears as small as it will in game.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

from antigravity3d.kernel.raster import BG, Camera, View, _basis, render_buffers
from antigravity3d.kernel.types import MeshData

# screen-size thresholds at which LOD k stops being used: LOD0 down to 50%, LOD1 down to 25%, ...
# These are also the *candidate* switch points of the generator (fast_ops.generate_asset_lods): a level is
# shown from its screen fraction down to the next level's, so it must be acceptable at its own fraction.
LOD_SCREEN_FRACTIONS = (1.0, 0.5, 0.25, 0.1)
DEFAULT_FOV_DEG = 60.0
# Screen-space error budget, one definition for the generator and the checker: a level may deviate from LOD0
# by at most LOD_MAX_ERROR_PX pixels of a LOD_REFERENCE_SCREEN_PX-tall frame at the largest size it is shown.
LOD_REFERENCE_SCREEN_PX = 1080
LOD_MAX_ERROR_PX = 1.0
# Smallest switch fraction the generator considers (a 27 px tall object on a 1080 px frame); candidates continue
# LOD_SCREEN_FRACTIONS by halving (default_transitions), so far levels exist where the near ones save nothing.
LOD_MIN_SCREEN_FRACTION = 0.025
# A level is emitted only when it removes at least this share of the previous level's triangles; a smaller
# saving does not pay for another mesh in memory, another file and another visible switch.
LOD_MIN_REDUCTION = 0.25
LABEL_H = 16
CROP_MARGIN = 0.15      # of the union bbox, each side


def transition_distance(bounds_radius_m: float, screen_fraction: float, fov_deg: float = DEFAULT_FOV_DEG) -> float:
    """Camera distance at which a bounding sphere of this radius spans `screen_fraction` of the
    frame height: 2R = f * 2 d tan(fov/2)  =>  d = R / (f tan(fov/2))."""
    return bounds_radius_m / (screen_fraction * math.tan(math.radians(fov_deg) / 2))


def lod_error_budget_m(bounds_radius_m: float, screen_fraction: float, *, error_px: float = LOD_MAX_ERROR_PX,
                       screen_px: float = LOD_REFERENCE_SCREEN_PX) -> float:
    """Geometric error (metres) that projects to ``error_px`` pixels when the object's bounding sphere spans
    ``screen_fraction`` of a ``screen_px``-tall frame. At :func:`transition_distance` the frame shows
    f * screen_px pixels across the sphere's diameter 2R, so e_px = e_m * f * screen_px / (2R) for any fov."""
    return error_px * 2.0 * bounds_radius_m / (screen_fraction * screen_px)


def lod_transitions(lods: Sequence[Any]) -> tuple[float, ...]:
    """Switch fractions of a chain: each level's own ``screen_fraction`` when it carries one (levels made by
    the error-budget generator skip fractions whose level would not have paid off), else the default table."""
    own = [getattr(lod, "screen_fraction", None) for lod in lods[1:]]
    if own and all(isinstance(f, (int, float)) and 0 < f <= 1 for f in own):
        return tuple(float(f) for f in own)  # type: ignore[arg-type]
    return default_transitions(len(lods))


def lod_candidate_fractions() -> tuple[float, ...]:
    """Screen fractions at which the generator tries a level: 1.0 (LOD0), then default_transitions() down to
    LOD_MIN_SCREEN_FRACTION."""
    n = 2
    while default_transitions(n + 1)[-1] >= LOD_MIN_SCREEN_FRACTION - 1e-12:
        n += 1
    return (1.0, *default_transitions(n))


def default_transitions(n_lods: int) -> tuple[float, ...]:
    """Screen fractions of the n_lods-1 switches (LOD k -> k+1); halves past the named table."""
    f = list(LOD_SCREEN_FRACTIONS)
    while len(f) < n_lods:
        f.append(f[-1] / 2)
    return tuple(f[1:n_lods])


@dataclass(frozen=True)
class PopThresholds:
    min_iou: float = 0.90
    max_shading_diff: float = 0.06
    max_boundary_px: float = 2.5


@dataclass(frozen=True)
class LodPair:
    screen_fraction: float
    distance_m: float
    color_a: np.ndarray
    color_b: np.ndarray
    mask_a: np.ndarray
    mask_b: np.ndarray
    crop_box: tuple[int, int, int, int]       # x0, y0, x1, y1 (exclusive) in render pixels
    crop_a: Image.Image                       # upscaled to size x size
    crop_b: Image.Image
    crop_diff: Image.Image
    iou: float
    shading_diff: float                       # mean |dRGB|/255 inside the union mask
    boundary_px: float                        # max symmetric silhouette-boundary displacement, pixels
    object_px: float                          # approx object size in pixels (screen_fraction * size)

    @property
    def score(self) -> float:
        """Larger = worse pop. Unitless sum; boundary error is relative to the object's pixel size."""
        return (1.0 - self.iou) + self.shading_diff + self.boundary_px / max(self.object_px, 1.0)


@dataclass(frozen=True)
class PairMetrics:
    index: int
    lod_from: int
    lod_to: int
    screen_fraction: float
    distance_m: float
    iou: float
    shading_diff: float
    boundary_px: float
    score: float
    tris_from: int
    tris_to: int


@dataclass(frozen=True)
class LodPopReport:
    transitions: list[PairMetrics]
    pairs: list[LodPair] = field(repr=False)
    size: int = 256

    @property
    def worst(self) -> PairMetrics | None:
        return max(self.transitions, key=lambda t: t.score, default=None)

    def failing(self, thresholds: PopThresholds = PopThresholds()) -> list[PairMetrics]:
        return [t for t in self.transitions if t.iou < thresholds.min_iou or t.shading_diff > thresholds.max_shading_diff or t.boundary_px > thresholds.max_boundary_px]

    def ok(self, thresholds: PopThresholds = PopThresholds()) -> bool:
        return not self.failing(thresholds)

    def one_line(self, thresholds: PopThresholds = PopThresholds()) -> str:
        w = self.worst
        if w is None:
            return "LOD pop: no transitions"
        bad = len(self.failing(thresholds))
        return (
            f"LOD pop: {len(self.transitions)} transitions, {'OK' if not bad else f'{bad} FAIL'}; worst pair {w.index} "
            f"(LOD{w.lod_from}->{w.lod_to} @{w.screen_fraction:.0%}, {w.distance_m:.1f} m): IoU {w.iou:.3f}, "
            f"shade {w.shading_diff:.3f}, edge {w.boundary_px:.1f}px, tris {w.tris_from}->{w.tris_to}"
        )

    def pair_sheet(self) -> Image.Image:
        """Rows = transitions; columns = LOD k | LOD k+1 | difference heatmap (shared crop window)."""
        s = self.size
        sheet = Image.new("RGB", (3 * s, max(len(self.pairs), 1) * (s + LABEL_H)), tuple(int(x) for x in BG))
        d = ImageDraw.Draw(sheet)
        for m, p in zip(self.transitions, self.pairs):
            y = m.index * (s + LABEL_H)
            d.rectangle([0, y, 3 * s - 1, y + LABEL_H - 1], fill=(0, 0, 0))
            d.text((4, y + 2), f"pair {m.index}: LOD{m.lod_from} -> LOD{m.lod_to} @ {m.screen_fraction:.0%} screen  "
                   f"IoU {m.iou:.3f}  shade {m.shading_diff:.3f}  edge {m.boundary_px:.1f}px", fill=(255, 255, 255))
            for c, im in enumerate((p.crop_a, p.crop_b, p.crop_diff)):
                sheet.paste(im, (c * s, y + LABEL_H))
            d.rectangle([0, y + LABEL_H, 3 * s - 1, y + LABEL_H + s - 1], outline=(70, 74, 84))
        return sheet


def lod_camera(ref_points: np.ndarray, view: View, size: int, screen_fraction: float, fov_deg: float = DEFAULT_FOV_DEG) -> tuple[Camera, float]:
    """True pinhole (persp) or orthographic camera at the switch distance; returns (camera, distance_m)."""
    pts = np.asarray(ref_points, np.float64)
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    rad = max(float(np.linalg.norm(hi - lo)) / 2, 1e-6)
    dist = transition_distance(rad, screen_fraction, fov_deg)
    persp = view == "persp"
    scale = (size / 2) / math.tan(math.radians(fov_deg) / 2) if persp else size * screen_fraction / (2 * rad)
    return Camera(view, _basis(view), (lo + hi) / 2, persp, dist if persp else 0.0, scale, np.zeros(2), size), dist


def _mesh(lod: Any, nid: str) -> MeshData:
    return MeshData(nid, "", np.asarray(lod.points, np.float64), np.asarray(lod.triangles, np.int32))


def _boundary(mask: np.ndarray) -> np.ndarray:
    return mask & ~ndimage.binary_erosion(mask, border_value=0)


def _heat(t: np.ndarray) -> np.ndarray:
    t = np.clip(t, 0, 1)[..., None]
    return np.where(t < 0.5, np.array([0, 0, 0]) + t * 2 * np.array([255, 0, 0]), np.array([255, 0, 0]) + (t - 0.5) * 2 * np.array([0, 255, 0])).astype(np.uint8)


def render_lod_pair(
    lod_a: Any,
    lod_b: Any,
    *,
    screen_fraction: float,
    size: int = 256,
    view: View = "persp",
    fov_deg: float = DEFAULT_FOV_DEG,
    reference: Any = None,
) -> LodPair:
    """lod_a/lod_b: anything with .points/.triangles (LOD, MeshData). Camera is framed on `reference`
    (default lod_a) so both render from identical camera and distance."""
    cam, dist = lod_camera((lod_a if reference is None else reference).points, view, size, screen_fraction, fov_deg)
    ra = render_buffers([_mesh(lod_a, "a")], view, size, camera=cam)
    rb = render_buffers([_mesh(lod_b, "b")], view, size, camera=cam)
    ma, mb = ra.prim_id >= 0, rb.prim_id >= 0
    union, inter = ma | mb, ma & mb
    iou = float(inter.sum() / union.sum()) if union.any() else 1.0
    cd = np.abs(ra.color.astype(np.float32) - rb.color.astype(np.float32)).mean(axis=2) / 255.0
    shade = float(cd[union].mean()) if union.any() else 0.0
    ba, bb = _boundary(ma), _boundary(mb)
    if not ma.any() and not mb.any():
        haus = 0.0
    elif not ma.any() or not mb.any():
        haus = float(size * math.sqrt(2))
    else:
        da, db = ndimage.distance_transform_edt(~ba), ndimage.distance_transform_edt(~bb)
        haus = float(max(db[ba].max(), da[bb].max()))
    # shared square crop around the union of silhouettes, upscaled with nearest-neighbour
    if union.any():
        ys, xs = np.nonzero(union)
        x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    else:
        x0, y0, x1, y1 = 0, 0, size, size
    side = max(x1 - x0, y1 - y0) * (1 + 2 * CROP_MARGIN)
    side = int(min(max(math.ceil(side), 8), size))
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    bx = int(min(max(cx - side // 2, 0), size - side))
    by = int(min(max(cy - side // 2, 0), size - side))
    box = (bx, by, bx + side, by + side)
    heat = _heat(np.where(ma ^ mb, 1.0, np.minimum(cd * 4, 0.9) * union))

    def up(a: np.ndarray) -> Image.Image:
        return Image.fromarray(a[by:by + side, bx:bx + side]).resize((size, size), Image.NEAREST)

    return LodPair(screen_fraction, float(dist), ra.color, rb.color, ma, mb, box, up(ra.color), up(rb.color), up(heat),
                   iou, shade, haus, screen_fraction * size)


def lod_pop_report(
    lods: Sequence[Any],
    *,
    size: int = 256,
    view: View = "persp",
    transitions: Sequence[float] | None = None,
    fov_deg: float = DEFAULT_FOV_DEG,
) -> LodPopReport:
    """Pop metrics for every adjacent pair, camera framed on lods[0] at each switch's screen fraction
    (the levels' own ``screen_fraction`` when they carry one, see :func:`lod_transitions`)."""
    fr = tuple(transitions) if transitions is not None else lod_transitions(lods)
    pairs, rows = [], []
    for i, f in enumerate(fr):
        p = render_lod_pair(lods[i], lods[i + 1], screen_fraction=f, size=size, view=view, fov_deg=fov_deg, reference=lods[0])
        pairs.append(p)
        rows.append(PairMetrics(i, i, i + 1, f, p.distance_m, p.iou, p.shading_diff, p.boundary_px, p.score,
                                len(lods[i].triangles), len(lods[i + 1].triangles)))
    return LodPopReport(rows, pairs, size)
