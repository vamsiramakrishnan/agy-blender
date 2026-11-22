"""Set-of-mark attribution: numbered badges on renders so defects are reported by number.

Each visible part gets a badge anchored on a pixel that provably belongs to it (the visible
pixel farthest from the region boundary). `legend` maps badge number -> node_id.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy import ndimage

from antigravity3d.kernel.fast_ops import UVResult, _tri_areas
from antigravity3d.kernel.raster import BG, RenderBuffers, View, render_buffers
from antigravity3d.kernel.types import MeshData

Box = tuple[int, int, int, int]  # x0, y0, x1, y1 inclusive

BADGE_FILL = (18, 18, 22)
BADGE_TEXT = (255, 255, 255)
BADGE_OUTLINE = (245, 245, 245)
LEADER = (255, 214, 64)
MARGIN = 2          # min gap between badge boxes
_FONT = ImageFont.load_default(size=11)


@dataclass(frozen=True)
class Badge:
    num: int
    node_id: str
    anchor: tuple[int, int]   # (x, y) pixel on the part's visible region
    box: Box
    leader: bool              # box sits outside the part, joined to the anchor by a line


@dataclass(frozen=True)
class MarkedView:
    image: Image.Image
    badges: list[Badge] = field(default_factory=list)
    unmarked: list[str] = field(default_factory=list)   # visible but < min_pixels


@dataclass(frozen=True)
class MarkedSheet:
    image: Image.Image
    legend: dict[int, str]
    visible: dict[str, list[int]]       # view -> badge numbers drawn in it
    unmarked: list[str]                 # node ids never marked in any view
    views: dict[str, MarkedView] = field(default_factory=dict)

    def legend_text(self) -> str:
        views_of: dict[int, list[str]] = {n: [v for v, ns in self.visible.items() if n in ns] for n in self.legend}
        lines = [f"[{n:02d}] {nid}  views: {','.join(views_of[n]) or '-'}" for n, nid in sorted(self.legend.items())]
        if self.unmarked:
            lines.append("unmarked (too small or hidden): " + ", ".join(self.unmarked))
        return "\n".join(lines)


def assign_badges(meshes: Sequence[MeshData]) -> dict[int, str]:
    """1-based numbering by sorted node_id: independent of the order of `meshes`."""
    return {i + 1: nid for i, nid in enumerate(sorted({m.node_id for m in meshes}))}


def locate_pixel(buffers: RenderBuffers, xy: tuple[float, float]) -> str | None:
    x, y = int(xy[0]), int(xy[1])
    if not (0 <= x < buffers.size and 0 <= y < buffers.size):
        return None
    p = int(buffers.prim_id[y, x])
    return buffers.node_ids[p] if p >= 0 else None


def prim_bbox_px(buffers: RenderBuffers, node_id: str) -> Box | None:
    idx = [i for i, n in enumerate(buffers.node_ids) if n == node_id]
    if not idx:
        return None
    ys, xs = np.nonzero(np.isin(buffers.prim_id, idx))
    if not len(xs):
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def _overlaps(a: Box, b: Box, gap: int = MARGIN) -> bool:
    return not (a[2] + gap < b[0] or b[2] + gap < a[0] or a[3] + gap < b[1] or b[3] + gap < a[1])


def _box_around(cx: int, cy: int, w: int, h: int, size: int) -> Box:
    x0 = min(max(cx - w // 2, 0), size - w)
    y0 = min(max(cy - h // 2, 0), size - h)
    return x0, y0, x0 + w - 1, y0 + h - 1


def _candidates(dt: np.ndarray, n: int = 16, sep: int = 4) -> list[tuple[int, int]]:
    """Region pixels by descending distance-to-boundary, greedily thinned to `sep` px apart."""
    flat = dt.ravel()
    k = min(len(flat), 600)
    top = np.argpartition(-flat, k - 1)[:k]
    top = top[np.argsort(-flat[top], kind="stable")]
    out: list[tuple[int, int]] = []
    for f in top:
        if flat[f] <= 0:
            break
        y, x = divmod(int(f), dt.shape[1])
        if all(abs(x - a) > sep or abs(y - b) > sep for a, b in out):
            out.append((x, y))
            if len(out) >= n:
                break
    return out


def mark_view(
    buffers: RenderBuffers,
    legend: dict[int, str],
    *,
    min_pixels: int = 24,
    reserved: Sequence[Box] = (),
) -> MarkedView:
    """Draw numbered badges on buffers.color. Returns the image, badge placements, tiny parts."""
    size = buffers.size
    img = Image.fromarray(buffers.color.copy())
    draw = ImageDraw.Draw(img)
    num_of = {nid: n for n, nid in legend.items()}
    counts = np.bincount(buffers.prim_id[buffers.prim_id >= 0], minlength=len(buffers.node_ids))
    occupied = buffers.prim_id >= 0
    integral = np.pad(occupied.astype(np.int32).cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    unmarked = sorted(buffers.node_ids[i] for i in np.nonzero((counts > 0) & (counts < min_pixels))[0] if buffers.node_ids[i] in num_of)
    todo = [i for i in np.nonzero(counts >= min_pixels)[0] if buffers.node_ids[i] in num_of]
    todo.sort(key=lambda i: (counts[i], num_of[buffers.node_ids[i]]))  # tight spots first
    # Each part's distance transform is taken over its bounding box padded by one background pixel, not the frame:
    # the same distances (every pixel outside the box is outside the region, and the padding is nearer than any of
    # them), at a fraction of the cost. Over the whole frame it was 3.7 of the 6.2 s that marking four 512 px views of
    # an 86-part crate took (r5 profile, 2026-11-22). The distances are then placed in a frame-sized array, so that the
    # candidate search (whose partial sort settles ties by array position) sees exactly what it always saw.
    boxes = ndimage.find_objects(buffers.prim_id + 1, max_label=len(buffers.node_ids))
    dt = np.zeros(buffers.prim_id.shape)
    placed: list[Box] = list(reserved)
    badges: list[Badge] = []

    for i in todo:
        nid = buffers.node_ids[i]
        n = num_of[nid]
        text = f"{n:02d}"
        l, t, r, b = draw.textbbox((0, 0), text, font=_FONT)
        w, h = r - l + 8, b - t + 6
        ys, xs = boxes[i]
        dt[ys, xs] = ndimage.distance_transform_edt(np.pad(buffers.prim_id[ys, xs] == i, 1))[1:-1, 1:-1]
        cands = _candidates(dt)
        ax, ay = cands[0]
        small = dt[ay, ax] < 0.5 * min(w, h)   # badge would swallow the part -> prefer a leader
        dt[ys, xs] = 0.0  # (read above only) the next part's search sees zeros outside its own region
        chosen: Box | None = None
        leader = False
        if not small:
            for cx, cy in cands:
                bx = _box_around(cx, cy, w, h, size)
                if not any(_overlaps(bx, p) for p in placed):
                    chosen, (ax, ay) = bx, (cx, cy)
                    break
        if chosen is None:
            chosen, leader = _free_box(integral, placed, (ax, ay), w, h, size), True
            if chosen is None:
                unmarked.append(nid)
                continue
        placed.append(chosen)
        badges.append(Badge(n, nid, (ax, ay), chosen, leader))

    for bd in badges:  # draw after placement so leaders sit under every badge
        if bd.leader:
            x0, y0, x1, y1 = bd.box
            ex, ey = min(max(bd.anchor[0], x0), x1), min(max(bd.anchor[1], y0), y1)
            draw.line([(ex, ey), bd.anchor], fill=(0, 0, 0), width=3)
            draw.line([(ex, ey), bd.anchor], fill=LEADER, width=1)
            draw.ellipse([bd.anchor[0] - 2, bd.anchor[1] - 2, bd.anchor[0] + 2, bd.anchor[1] + 2], fill=LEADER, outline=(0, 0, 0))
    for bd in badges:
        x0, y0, x1, y1 = bd.box
        draw.rounded_rectangle(bd.box, radius=3, fill=BADGE_FILL, outline=BADGE_OUTLINE, width=1)
        text = f"{bd.num:02d}"
        l, t, r, b = draw.textbbox((0, 0), text, font=_FONT)
        draw.text(((x0 + x1 + 1 - (r - l)) / 2 - l, (y0 + y1 + 1 - (b - t)) / 2 - t), text, font=_FONT, fill=BADGE_TEXT)
    return MarkedView(img, sorted(badges, key=lambda b: b.num), sorted(unmarked))


def _free_box(integral: np.ndarray, placed: Sequence[Box], anchor: tuple[int, int], w: int, h: int, size: int) -> Box | None:
    """Box near `anchor`, not overlapping `placed`, preferring background pixels (min occlusion)."""
    xs = np.arange(0, size - w + 1, 2)
    ys = np.arange(0, size - h + 1, 2)
    if not len(xs) or not len(ys):
        return None
    X, Y = np.meshgrid(xs, ys)
    occ = (integral[Y + h, X + w] - integral[Y, X + w] - integral[Y + h, X] + integral[Y, X]) / (w * h)
    d = np.hypot(X + w / 2 - anchor[0], Y + h / 2 - anchor[1])
    cost = occ * 3 * size + d
    # A box at (x, y) overlaps placed box p iff p0 - w + 1 - MARGIN <= x <= p2 + MARGIN and the same in y: one
    # rectangle of grid positions per placed box, painted by slicing (positions run in steps of 2 from 0). The same
    # mask as testing every position against every box, which was 1.8 s of marking four 512 px views of 86 parts.
    for p in placed:
        jx0, jx1 = max(0, -((w - 1 + MARGIN - p[0]) // 2)), min(len(xs) - 1, (p[2] + MARGIN) // 2)
        jy0, jy1 = max(0, -((h - 1 + MARGIN - p[1]) // 2)), min(len(ys) - 1, (p[3] + MARGIN) // 2)
        if jx0 <= jx1 and jy0 <= jy1:
            cost[jy0:jy1 + 1, jx0:jx1 + 1] = np.inf
    j = int(np.argmin(cost))
    if not np.isfinite(cost.flat[j]):
        return None
    x, y = int(X.flat[j]), int(Y.flat[j])
    return x, y, x + w - 1, y + h - 1


def marked_contact_sheet(
    meshes: list[MeshData],
    views: Sequence[View] = ("front", "right", "top", "persp"),
    size: int = 256,
    *,
    min_pixels: int = 24,
) -> MarkedSheet:
    legend = assign_badges(meshes)
    cols, rows = 2, (len(views) + 1) // 2
    sheet = Image.new("RGB", (cols * size, rows * size), tuple(int(x) for x in BG))
    visible: dict[str, list[int]] = {}
    mviews: dict[str, MarkedView] = {}
    seen: set[str] = set()
    for i, v in enumerate(views):
        label: Box = (0, 0, 8 * len(v) + 10, 14)
        mv = mark_view(render_buffers(meshes, v, size), legend, min_pixels=min_pixels, reserved=[label])
        d = ImageDraw.Draw(mv.image)
        d.rectangle(list(label), fill=(0, 0, 0))
        d.text((5, 2), v, fill=(255, 255, 255))
        d.rectangle([0, 0, size - 1, size - 1], outline=(70, 74, 84))
        sheet.paste(mv.image, ((i % cols) * size, (i // cols) * size))
        visible[v] = [b.num for b in mv.badges]
        mviews[v] = mv
        seen.update(b.node_id for b in mv.badges)
    unmarked = sorted(set(legend.values()) - seen)
    return MarkedSheet(sheet, legend, visible, unmarked, mviews)


# --------------------------------------------------------------------------- UV distortion

LOG_RANGE = 2.0  # colour saturates at 2**±2 = 4x denser / sparser than the mesh median
_NEUTRAL, _SPARSE, _DENSE = np.array([232, 232, 232.0]), np.array([40, 90, 205.0]), np.array([215, 45, 35.0])


def _diverging(t: np.ndarray) -> np.ndarray:
    """t in [-1,1] -> RGB float; negative = fewer texels per m^2 (blue), positive = more (red)."""
    t = np.clip(t, -1, 1)[..., None]
    return np.where(t < 0, _NEUTRAL + (_SPARSE - _NEUTRAL) * -t, _NEUTRAL + (_DENSE - _NEUTRAL) * t)


def triangle_density_ratio(r: UVResult) -> np.ndarray:
    """Per-triangle (uv area / 3D area) divided by the mesh median; NaN for degenerate triangles."""
    a3 = _tri_areas(r.points, r.triangles, 3)
    a2 = _tri_areas(r.uvs.astype(np.float64), r.triangles, 2)
    ratio = np.full(len(a3), np.nan)
    ok = a3 > 1e-14
    ratio[ok] = a2[ok] / a3[ok]
    med = np.nanmedian(ratio) if ok.any() else 0.0
    return ratio / med if med > 0 else np.full(len(a3), np.nan)


def uv_distortion_overlay(meshes: list[MeshData], uv_results: Sequence[UVResult], view: View, size: int = 256) -> Image.Image:
    """Texel-density heatmap (log2 ratio to mesh median, diverging around 1.0) over the clay render."""
    base = render_buffers(meshes, view, size)
    out = base.color.astype(np.float32)
    if base.camera is not None and uv_results:
        uvm = [MeshData(r.node_id, "", r.points, r.triangles) for r in uv_results]
        hb = render_buffers(uvm, view, size, camera=base.camera)
        ok = (hb.prim_id >= 0) & (hb.depth <= base.depth * (1 + 1e-4) + 1e-6)
        heat = np.zeros((size, size), np.float32)
        for i, r in enumerate(uv_results):
            m = ok & (hb.prim_id == i)
            if m.any():
                with np.errstate(divide="ignore", invalid="ignore"):
                    lg = np.log2(triangle_density_ratio(r))
                v = lg[hb.tri_id[m]]
                heat[m] = np.nan_to_num(v, nan=0.0, posinf=LOG_RANGE, neginf=-LOG_RANGE)
        rgb = _diverging(heat / LOG_RANGE)
        out[ok] = out[ok] * 0.3 + rgb[ok] * 0.7
    img = Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))
    d = ImageDraw.Draw(img)
    bw, bh, x0, y0 = min(size - 16, 96), 8, 8, size - 22
    d.rectangle([x0 - 4, y0 - 14, x0 + bw + 22, y0 + bh + 3], fill=(0, 0, 0))
    ramp = (_diverging(np.linspace(-1, 1, bw))).astype(np.uint8)
    img.paste(Image.fromarray(np.repeat(ramp[None], bh, axis=0)), (x0, y0))
    d.rectangle([x0 - 1, y0 - 1, x0 + bw, y0 + bh], outline=(200, 200, 200))
    d.line([(x0 + bw // 2, y0 - 2), (x0 + bw // 2, y0 + bh + 1)], fill=(0, 0, 0))
    d.text((x0 - 2, y0 - 13), f"{2**-LOG_RANGE:g}x", font=_FONT, fill=(255, 255, 255))
    d.text((x0 + bw // 2 - 3, y0 - 13), "1x", font=_FONT, fill=(255, 255, 255))
    d.text((x0 + bw - 12, y0 - 13), f"{2**LOG_RANGE:g}x", font=_FONT, fill=(255, 255, 255))
    return img
