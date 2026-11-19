"""Pure-numpy software clay rasteriser: the cheap preview tier (no GL, no Hydra).

Triangles are binned by screen-bbox size into power-of-two buckets and rasterised as
(T, B, B) barycentric tensors; fragments from all buckets are then depth-resolved with
one argsort. Flat Lambert shading, fixed world key light plus a camera fill.
"""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True)
class Camera:
    """Projection used by a render: world -> pixels. Orthographic or pinhole (persp)."""

    view: str
    basis: np.ndarray       # (3,3) rows: right, up, toward-scene
    center: np.ndarray      # (3,) world point the camera looks at
    persp: bool
    dist: float             # camera distance from `center` (persp only)
    scale: float            # pixels per projected unit
    mid: np.ndarray         # (2,) projected-plane point mapped to the image centre
    size: int

    def project(self, pts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """World points (N,3) -> (pixel xy (N,2), camera depth (N,))."""
        cam = (pts - self.center) @ self.basis.T
        if self.persp:
            z = cam[:, 2] + self.dist
            xy = cam[:, :2] / np.maximum(z, 1e-6)[:, None]
        else:
            z = cam[:, 2]
            xy = cam[:, :2]
        sx = (xy[:, 0] - self.mid[0]) * self.scale + self.size / 2
        sy = self.size / 2 - (xy[:, 1] - self.mid[1]) * self.scale
        return np.stack([sx, sy], axis=1), z

    def metres_per_pixel(self, depth: float | np.ndarray) -> float | np.ndarray:
        """World size of one pixel at camera depth `depth` (depth as stored in RenderBuffers)."""
        return depth / self.scale if self.persp else 1.0 / self.scale


def frame_camera(pts: np.ndarray, view: str, size: int, fill: float = 0.88) -> Camera:
    """Auto-framing camera: the projected bbox of `pts` spans `fill` of the image's long side."""
    B = _basis(view)
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    c = (lo + hi) / 2
    rad = max(np.linalg.norm(hi - lo) / 2, 1e-6)
    cam = (pts - c) @ B.T
    persp = view == "persp"
    if persp:
        dist = rad / np.sin(np.radians(30) / 2) * 1.05
        xy = cam[:, :2] / np.maximum(cam[:, 2] + dist, 1e-6)[:, None]
    else:
        dist = 0.0
        xy = cam[:, :2]
    lo2, hi2 = xy.min(axis=0), xy.max(axis=0)
    scale = size * fill / max((hi2 - lo2).max(), 1e-9)
    return Camera(view, B, c, persp, float(dist), float(scale), (lo2 + hi2) / 2, size)


def _project(pts: np.ndarray, view: str, size: int):
    """Returns screen xy (N,2), cam depth z (N,), basis, persp."""
    cam = frame_camera(pts, view, size)
    sxy, z = cam.project(pts)
    return sxy, z, cam.basis, cam.persp


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
    sizes = np.array([1, 2, 3, 4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256], np.int32)
    bucket = np.zeros(len(tris), np.int32)
    bucket[ok] = sizes[np.searchsorted(sizes, ext[ok])]
    pix_l, key_l, tri_l = [], [], []
    for B in np.unique(bucket[ok]):
        ids = np.nonzero(ok & (bucket == B))[0]
        g = np.arange(B, dtype=np.float32) + 0.5
        a, b, c = p0[ids].astype(np.float32), p1[ids].astype(np.float32), p2[ids].astype(np.float32)
        inv = (1.0 / area[ids]).astype(np.float32)
        ox = x0[ids].astype(np.float32)[:, None] + g[None]    # (T,B) pixel-centre x
        oy = y0[ids].astype(np.float32)[:, None] + g[None]    # (T,B) pixel-centre y
        # w0 (weight of p0) = edge function of (p1,p2) at pixel, normalised; linear in x and y
        w = []
        for u, v in ((b, c), (c, a)):
            dy, dx = (v[:, 1] - u[:, 1]) * inv, (u[:, 0] - v[:, 0]) * inv
            # (u-P) x (v-P) normalised: ((ux-px)(vy-py)-(uy-py)(vx-px))/area -> affine in px,py
            c0 = (u[:, 0] * v[:, 1] - u[:, 1] * v[:, 0]) * inv
            tx = (-dy)[:, None] * ox + c0[:, None]
            ty = (-dx)[:, None] * oy
            w.append(tx[:, None, :] + ty[:, :, None])
        w0, w1 = w
        inside = (w0 >= -1e-5) & (w1 >= -1e-5) & ((w0 + w1) <= 1.00001)
        t, j, i = np.nonzero(inside)
        if not len(t):
            continue
        fx = x0[ids][t] + i
        fy = y0[ids][t] + j
        v = (fx < size) & (fy < size)
        t, j, i, fx, fy = t[v], j[v], i[v], fx[v], fy[v]
        k = key[tris[ids]]  # (T,3)
        wa, wb = w0[t, j, i], w1[t, j, i]
        pix_l.append(fy * size + fx)
        key_l.append(wa * k[t, 0] + wb * k[t, 1] + (1.0 - wa - wb) * k[t, 2])
        tri_l.append(ids[t])
    tri_img = np.full(size * size, -1, np.int32)
    zbuf = np.full(size * size, np.inf, np.float32)
    if pix_l:
        pix, z, tid = np.concatenate(pix_l), np.concatenate(key_l), np.concatenate(tri_l)
        order = np.argsort(-z, kind="stable")  # far first; last write wins = nearest
        tri_img[pix[order]] = tid[order]
        zbuf[pix[order]] = z[order]
    return tri_img.reshape(size, size), zbuf.reshape(size, size)


@dataclass(frozen=True)
class RenderBuffers:
    color: np.ndarray       # (H,W,3) uint8, identical to render_clay
    prim_id: np.ndarray     # (H,W) int32, index into the `meshes` list, -1 = background
    tri_id: np.ndarray      # (H,W) int32, triangle index within that mesh, -1 = background
    depth: np.ndarray       # (H,W) float32 camera depth (see Camera), inf = background
    camera: Camera | None   # None for an empty scene
    view: str
    size: int


def _uv_checker(img, hit, tri_img, tris, sxy, tri_mesh, meshes, uvs, size) -> None:
    """Overwrite shaded pixels of meshes with UVs by shading * checker(uv): screen-affine barycentrics."""
    ys, xs = np.nonzero(hit)
    t = tri_img[ys, xs]
    sel = np.array([meshes[i].node_id in uvs for i in range(len(meshes))])[tri_mesh[t]]
    ys, xs, t = ys[sel], xs[sel], t[sel]
    if not len(t):
        return
    p0, p1, p2 = (sxy[tris[t, i]] for i in range(3))
    d = (p1[:, 0] - p0[:, 0]) * (p2[:, 1] - p0[:, 1]) - (p1[:, 1] - p0[:, 1]) * (p2[:, 0] - p0[:, 0])
    px, py = xs + 0.5, ys + 0.5
    w1 = ((px - p0[:, 0]) * (p2[:, 1] - p0[:, 1]) - (py - p0[:, 1]) * (p2[:, 0] - p0[:, 0])) / d
    w2 = ((p1[:, 0] - p0[:, 0]) * (py - p0[:, 1]) - (p1[:, 1] - p0[:, 1]) * (px - p0[:, 0])) / d
    w0 = 1 - w1 - w2
    mi = tri_mesh[t]
    uv = np.zeros((len(t), 2))
    for i in np.unique(mi):
        k = mi == i
        local = tris[t[k]] - _vert_base(meshes)[i]
        u = np.asarray(uvs[meshes[i].node_id], np.float64)[local]
        uv[k] = w0[k, None] * u[:, 0] + w1[k, None] * u[:, 1] + w2[k, None] * u[:, 2]
    chk = ((np.floor(uv * 8).sum(axis=1) % 2) == 0)
    f = np.where(chk, 1.0, 0.55)[:, None]
    img[ys, xs] = np.clip(img[ys, xs] * f, 0, 255).astype(np.uint8)


def _vert_base(meshes) -> list[int]:
    out, off = [], 0
    for m in meshes:
        out.append(off)
        if len(m.triangles):
            off += len(m.points)
    return out


def render_buffers(
    meshes: list[MeshData],
    view: View = "persp",
    size: int = 256,
    *,
    wireframe: bool = False,
    uvs: dict[str, np.ndarray] | None = None,
    camera: Camera | None = None,
) -> RenderBuffers:
    """Single rasterisation path: clay colour plus ID/depth buffers.

    uvs: node_id -> (V,2) per-vertex UVs; when given, the colour is replaced by a UV
    checker pattern (tiling past [0,1] is visible) for those meshes.
    camera: explicit camera (e.g. shared between two LODs); default auto-frames the meshes.
    """
    nid = tuple(m.node_id for m in meshes)
    img = np.empty((size, size, 3), np.uint8)
    img[:] = BG
    prim = np.full((size, size), -1, np.int32)
    tri_loc = np.full((size, size), -1, np.int32)
    depth = np.full((size, size), np.inf, np.float32)
    pts, tris = _gather(meshes)
    if not len(tris):
        return RenderBuffers(img, prim, tri_loc, depth, None, view, size, nid)
    tri_mesh = np.concatenate([np.full(len(m.triangles), i, np.int32) for i, m in enumerate(meshes) if len(m.triangles)])
    tri_start = np.concatenate([np.arange(len(m.triangles)) for m in meshes if len(m.triangles)])
    cam = camera or frame_camera(pts, view, size)
    sxy, z = cam.project(pts)
    B, persp = cam.basis, cam.persp
    if persp:
        keep = (z[tris] > 1e-6).all(axis=1)
        tris, tri_mesh, tri_start = tris[keep], tri_mesh[keep], tri_start[keep]
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
    if uvs:
        _uv_checker(img, hit, tri_img, tris, sxy, tri_mesh, meshes, uvs, size)
    prim[hit] = tri_mesh[tri_img[hit]]
    tri_loc[hit] = tri_start[tri_img[hit]]
    depth[hit] = (-1.0 / zbuf[hit] if persp else zbuf[hit]).astype(np.float32)

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
        v &= hit[np.clip(iy, 0, size - 1), np.clip(ix, 0, size - 1)]
        v &= kk <= zbuf[np.clip(iy, 0, size - 1), np.clip(ix, 0, size - 1)] + 0.01 * rng
        img[iy[v], ix[v]] = (img[iy[v], ix[v]] * 0.6).astype(np.uint8)
    return RenderBuffers(img, prim, tri_loc, depth, cam, view, size, nid)


def render_clay(
    meshes: list[MeshData],
    view: View = "persp",
    size: int = 256,
    wireframe: bool = False,
) -> np.ndarray:
    return render_buffers(meshes, view, size, wireframe=wireframe).color


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
