"""Pure-numpy software clay rasteriser: the cheap preview tier (no GL, no Hydra).

Triangles are binned by screen-bbox size into power-of-two buckets and rasterised as
(T, B, B) barycentric tensors; fragments from all buckets are then depth-resolved with
one argsort. Flat Lambert shading, fixed world key light plus a camera fill.
"""

from __future__ import annotations

from typing import Literal, Sequence

import numpy as np
from PIL import Image, ImageDraw

from antigravity3d.kernel.types import MeshData

View = Literal["front", "back", "left", "right", "top", "persp"]

BG = np.array([34, 36, 42], np.uint8)
CLAY = np.array([205, 198, 186], np.float32)
KEY_LIGHT = np.array([-0.45, -0.55, 0.70]) / np.linalg.norm([-0.45, -0.55, 0.70])

# camera (right, up, toward-scene) basis vectors in world space; Z-up, right-handed
_AXIS_VIEWS: dict[str, tuple[tuple[float, ...], tuple[float, ...], tuple[float, ...]]] = {
    "front": ((1, 0, 0), (0, 0, 1), (0, 1, 0)),    # camera at -Y looking +Y
    "back": ((-1, 0, 0), (0, 0, 1), (0, -1, 0)),
    "right": ((0, 1, 0), (0, 0, 1), (-1, 0, 0)),   # camera at +X looking -X
    "left": ((0, -1, 0), (0, 0, 1), (1, 0, 0)),
    "top": ((1, 0, 0), (0, 1, 0), (0, 0, -1)),
}


def _basis(view: str) -> np.ndarray:
    if view == "persp":
        f = np.array([0.55, 0.70, -0.45])  # looking from (-x,-y,+z) quadrant toward scene
        f /= np.linalg.norm(f)
        r = np.cross(f, [0, 0, 1.0])
        r /= np.linalg.norm(r)
        return np.stack([r, np.cross(r, f), f])
    return np.array(_AXIS_VIEWS[view], dtype=np.float64)


def _gather(meshes: Sequence[MeshData]) -> tuple[np.ndarray, np.ndarray]:
    pts, tris, off = [], [], 0
    for m in meshes:
        if len(m.triangles) == 0:
            continue
        pts.append(np.asarray(m.points, dtype=np.float64))
        tris.append(np.asarray(m.triangles, dtype=np.int64) + off)
        off += len(m.points)
    if not pts:
        return np.zeros((0, 3)), np.zeros((0, 3), np.int64)
    return np.vstack(pts), np.vstack(tris)


def _project(pts: np.ndarray, view: str, size: int):
    """Returns screen xy (N,2), cam depth z (N,), basis."""
    B = _basis(view)
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    c = (lo + hi) / 2
    rad = max(np.linalg.norm(hi - lo) / 2, 1e-6)
    cam = (pts - c) @ B.T
    persp = view == "persp"
    if persp:
        dist = rad / np.sin(np.radians(30) / 2) * 1.05
        z = cam[:, 2] + dist
        xy = cam[:, :2] / np.maximum(z, 1e-6)[:, None]
    else:
        z = cam[:, 2]
        xy = cam[:, :2]
    lo2, hi2 = xy.min(axis=0), xy.max(axis=0)
    scale = size * 0.88 / max((hi2 - lo2).max(), 1e-9)
    mid = (lo2 + hi2) / 2
    sx = (xy[:, 0] - mid[0]) * scale + size / 2
    sy = size / 2 - (xy[:, 1] - mid[1]) * scale
    return np.stack([sx, sy], axis=1), z, B, persp


def _rasterise(sxy: np.ndarray, key: np.ndarray, tris: np.ndarray, size: int):
    """Returns (tri_id image int32 [-1 = empty], depth-key image float32)."""
    p0, p1, p2 = (sxy[tris[:, i]] for i in range(3))
    area = (p1[:, 0] - p0[:, 0]) * (p2[:, 1] - p0[:, 1]) - (p1[:, 1] - p0[:, 1]) * (p2[:, 0] - p0[:, 0])
    lo = np.minimum(np.minimum(p0, p1), p2)
    hi = np.maximum(np.maximum(p0, p1), p2)
    x0 = np.maximum(np.floor(lo[:, 0] - 0.5), 0).astype(np.int32)
    y0 = np.maximum(np.floor(lo[:, 1] - 0.5), 0).astype(np.int32)
    x1 = np.minimum(np.ceil(hi[:, 0] - 0.5), size - 1).astype(np.int32)
    y1 = np.minimum(np.ceil(hi[:, 1] - 0.5), size - 1).astype(np.int32)
    ext = np.maximum(x1 - x0, y1 - y0) + 1
    ok = (np.abs(area) > 1e-9) & (x1 >= x0) & (y1 >= y0)
    bucket = np.ones(len(tris), np.int32)
    bucket[ok] = 1 << np.ceil(np.log2(np.maximum(ext[ok], 1))).astype(np.int32)
    pix_l, key_l, tri_l = [], [], []
    for B in np.unique(bucket[ok]):
        ids = np.nonzero(ok & (bucket == B))[0]
        g = np.arange(B, dtype=np.float32) + 0.5
        px = x0[ids, None, None] + g[None, None, :]          # (T,1,B)
        py = y0[ids, None, None] + g[None, :, None]          # (T,B,1)
        a, b, c = p0[ids].astype(np.float32), p1[ids].astype(np.float32), p2[ids].astype(np.float32)
        inv = (1.0 / area[ids]).astype(np.float32)[:, None, None]
        w0 = ((b[:, 0, None, None] - px) * (c[:, 1, None, None] - py) - (b[:, 1, None, None] - py) * (c[:, 0, None, None] - px)) * inv
        w1 = ((c[:, 0, None, None] - px) * (a[:, 1, None, None] - py) - (c[:, 1, None, None] - py) * (a[:, 0, None, None] - px)) * inv
        w2 = 1.0 - w0 - w1
        # x0 + g can exceed x1 for the padded part of the bucket
        inside = (w0 >= -1e-5) & (w1 >= -1e-5) & (w2 >= -1e-5) & (px < (x1[ids] + 1)[:, None, None]) & (py < (y1[ids] + 1)[:, None, None])
        t, j, i = np.nonzero(inside)
        if not len(t):
            continue
        k = key[tris[ids]]  # (T,3)
        z = w0[t, j, i] * k[t, 0] + w1[t, j, i] * k[t, 1] + w2[t, j, i] * k[t, 2]
        pix_l.append((y0[ids][t] + j) * size + x0[ids][t] + i)
        key_l.append(z)
        tri_l.append(ids[t])
    tri_img = np.full(size * size, -1, np.int32)
    zbuf = np.full(size * size, np.inf, np.float32)
    if pix_l:
        pix, z, tid = np.concatenate(pix_l), np.concatenate(key_l), np.concatenate(tri_l)
        order = np.argsort(-z, kind="stable")  # far first; last write wins = nearest
        tri_img[pix[order]] = tid[order]
        zbuf[pix[order]] = z[order]
    return tri_img.reshape(size, size), zbuf.reshape(size, size)


def render_clay(
    meshes: list[MeshData],
    view: View = "persp",
    size: int = 256,
    wireframe: bool = False,
) -> np.ndarray:
    img = np.empty((size, size, 3), np.uint8)
    img[:] = BG
    pts, tris = _gather(meshes)
    if not len(tris):
        return img
    sxy, z, B, persp = _project(pts, view, size)
    keep = (z[tris] > 1e-6).all(axis=1)
    tris = tris[keep]
    # depth key: smaller = nearer. Persp interpolates 1/z linearly (screen-correct).
    key = (-1.0 / z if persp else z).astype(np.float32)
    tri_img, zbuf = _rasterise(sxy, key, tris, size)

    p = pts[tris]
    n = np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0])
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
    toward_cam = -B[2]
    n *= np.where(n @ toward_cam < 0, -1.0, 1.0)[:, None]  # double-sided
    shade = 0.22 + 0.60 * np.maximum(n @ KEY_LIGHT, 0) + 0.28 * np.maximum(n @ toward_cam, 0)
    col = np.clip(CLAY[None] * shade[:, None], 0, 255).astype(np.uint8)
    hit = tri_img >= 0
    img[hit] = col[tri_img[hit]]

    if wireframe:
        e = np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]])
        e = np.unique(np.sort(e, axis=1), axis=0)
        a, b = sxy[e[:, 0]], sxy[e[:, 1]]
        ns = np.clip(np.ceil(np.abs(b - a).max(axis=1)).astype(np.int64), 1, size * 2)
        eid = np.repeat(np.arange(len(e)), ns)
        t = (np.arange(len(eid)) - np.repeat(np.cumsum(ns) - ns, ns)) / np.repeat(ns, ns)
        xy = a[eid] + (b[eid] - a[eid]) * t[:, None]
        ix, iy = np.floor(xy[:, 0]).astype(int), np.floor(xy[:, 1]).astype(int)
        v = (ix >= 0) & (ix < size) & (iy >= 0) & (iy < size)
        ka, kb = key[e[:, 0]][eid], key[e[:, 1]][eid]
        if persp:  # interpolate in 1/z space
            za, zb = z[e[:, 0]][eid], z[e[:, 1]][eid]
            kk = -((1 - t) / za + t / zb)
        else:
            kk = ka + (kb - ka) * t
        rng = float(np.ptp(key)) or 1.0
        v &= kk[:] <= zbuf[np.clip(iy, 0, size - 1), np.clip(ix, 0, size - 1)] + 0.01 * rng
        img[iy[v], ix[v]] = (img[iy[v], ix[v]] * 0.35).astype(np.uint8)
    return img


def contact_sheet(
    meshes: list[MeshData],
    views: Sequence[View] = ("front", "right", "top", "persp"),
    size: int = 256,
    wireframe_overlay: bool = True,
) -> Image.Image:
    cols = 2
    rows = (len(views) + 1) // 2
    sheet = Image.new("RGB", (cols * size, rows * size), tuple(int(x) for x in BG))
    for i, v in enumerate(views):
        tile = Image.fromarray(render_clay(meshes, v, size, wireframe=wireframe_overlay))
        d = ImageDraw.Draw(tile)
        d.rectangle([0, 0, 8 * len(v) + 10, 14], fill=(0, 0, 0))
        d.text((5, 2), v, fill=(255, 255, 255))
        d.rectangle([0, 0, size - 1, size - 1], outline=(70, 74, 84))
        sheet.paste(tile, ((i % cols) * size, (i // cols) * size))
    return sheet
