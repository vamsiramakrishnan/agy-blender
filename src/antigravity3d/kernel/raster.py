"""Pure-numpy software clay rasteriser: the cheap preview tier (no GL, no Hydra).

Triangles are binned by screen-bbox size into tile sizes and rasterised as (T, B, B)
edge-function tensors; coverage of pixel centres is exact (see _rasterise), so adjacent
triangles leave no holes. Fragments from all buckets are then depth-resolved per pixel.
Flat Lambert shading, fixed world key light plus a camera fill.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

import numpy as np
from PIL import Image, ImageDraw

from antigravity3d.kernel.types import MeshData

View = Literal["front", "back", "left", "right", "top", "persp", "persp_rear"]
PERSP_VIEWS = frozenset({"persp", "persp_rear"})
# What the vision judge is shown of one design's shape, one 512 px image each (tactics.judging): the three orthographic
# views and two perspectives from opposite corners, so the back and left sides are seen at all.
JUDGE_VIEWS: tuple[View, ...] = ("front", "right", "top", "persp", "persp_rear")

BG = np.array([34, 36, 42], np.uint8)
CLAY = np.array([205, 198, 186], np.float32)
KEY_LIGHT = np.array([-0.45, -0.55, 0.70]) / np.linalg.norm([-0.45, -0.55, 0.70])
# A half turn about the vertical axis: "persp_rear" is "persp" seen from the opposite corner, camera in the
# (+x, +y, +z) quadrant. Its key light is turned with it. The world key above lights the faces that "persp" sees and
# leaves those facing +x and +y with fill light only (0.22 + 0.28 cos, every side about equally dark), so a fixed key
# would show the back as a flat silhouette. With camera and key both turned, a rear render is exactly the "persp"
# render of the design turned half a turn, which is the condition measured live on 2026-11-22 (r5/live/judge_truth.py,
# condition E).
_HALF_TURN = np.array([-1.0, -1.0, 1.0])


def key_light(view: str) -> np.ndarray:
    """Direction toward the key light for a render from ``view`` (world space)."""
    return KEY_LIGHT * _HALF_TURN if view == "persp_rear" else KEY_LIGHT

# camera (right, up, toward-scene) basis vectors in world space; Z-up, right-handed
_AXIS_VIEWS: dict[str, tuple[tuple[float, ...], tuple[float, ...], tuple[float, ...]]] = {
    "front": ((1, 0, 0), (0, 0, 1), (0, 1, 0)),    # camera at -Y looking +Y
    "back": ((-1, 0, 0), (0, 0, 1), (0, -1, 0)),
    "right": ((0, 1, 0), (0, 0, 1), (-1, 0, 0)),   # camera at +X looking -X
    "left": ((0, -1, 0), (0, 0, 1), (1, 0, 0)),
    "top": ((1, 0, 0), (0, 1, 0), (0, 0, -1)),
}


def _basis(view: str) -> np.ndarray:
    if view in PERSP_VIEWS:
        f = np.array([0.55, 0.70, -0.45])  # looking from (-x,-y,+z) quadrant toward scene
        if view == "persp_rear":
            f = f * _HALF_TURN  # from the (+x,+y,+z) quadrant (exact: a sign change)
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
    persp = view in PERSP_VIEWS
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


_TILE_BATCH_ELEMENTS = 1 << 24  # triangles x tile pixels evaluated at once (a few hundred MB of float32 at most)


def _tile_sizes(size: int) -> np.ndarray:
    """Tile edge lengths (pixels) that triangles are binned by, covering any triangle a ``size`` frame can hold.

    A triangle is evaluated on the smallest tile that contains its clipped bounding box. The table runs
    1, 2, 3, 4, 6, 8, ... (alternating x1.5 and x1.33) up to at least the frame size, so a triangle spanning
    the whole frame has a tile at every resolution, not only up to 256 px.
    """
    s = [1, 2, 3, 4]
    while s[-1] < size:
        s.append(s[-2] * 2)
    return np.array(s, np.int32)


_U32 = 2.0 ** -24   # unit roundoff of float32
_U64 = 2.0 ** -53   # unit roundoff of float64


def _orient_exact(ux, uy, vx, vy, px, py) -> np.ndarray:
    """Exact sign (-1, 0, 1) of (vx-ux)(py-uy) - (vy-uy)(px-ux) for float64 arrays, by integer arithmetic.

    Each float is mantissa * 2**exponent; scaling all six values of one evaluation by the same power of two makes
    them integers, and the determinant of integers is evaluated with Python's unbounded ints. Only called for the
    few evaluations whose floating-point value is too close to zero to trust its sign.
    """
    v = np.stack([np.asarray(a, np.float64) for a in (ux, uy, vx, vy, px, py)])
    if not v.shape[1]:
        return np.zeros(0, np.int8)
    m, e = np.frexp(v)
    mant = np.ldexp(m, 53).astype(np.int64)                   # |mant| < 2**53, v == mant * 2**(e - 53) exactly
    shift = (e - e.min(axis=0)).astype(object)
    q = mant.astype(object) << shift                          # common scale per column: exact integers
    det = (q[2] - q[0]) * (q[5] - q[1]) - (q[3] - q[1]) * (q[4] - q[0])
    return ((det > 0).astype(np.int8) - (det < 0).astype(np.int8))


def _orient_sign(ux, uy, vx, vy, px, py) -> np.ndarray:
    """Exact sign of the orientation determinant (see _orient_exact): float64 with a forward error bound, falling
    back to the exact evaluation only where the float64 value is within that bound of zero."""
    dx, dy, ex, ey = vx - ux, vy - uy, px - ux, py - uy
    l, r = dx * ey, dy * ex
    det = l - r
    # three roundings per product term (two subtractions, one product) and one for the difference
    err = 8 * _U64 * (np.abs(l) + np.abs(r))
    s = np.sign(det).astype(np.int8)
    # undecided: within the bound of zero (or NaN), unless both products are exactly zero because a factor is
    # (a difference of two floats is zero only when they are equal, so the exact determinant is zero too)
    amb = ~(np.abs(det) > err) & ~(((dx == 0) | (ey == 0)) & ((dy == 0) | (ex == 0)))
    if amb.any():
        s[amb] = _orient_exact(ux[amb], uy[amb], vx[amb], vy[amb], px[amb], py[amb])
    return s


def _owned(dx: np.ndarray, dy: np.ndarray) -> np.ndarray:
    """Whether a pixel centre lying exactly on an edge of direction (dx, dy) (counter-clockwise triangle, y down)
    belongs to the triangle. The rule is that of a point displaced by (eps**2, eps) for an infinitesimal eps > 0:
    the displaced point lies on no edge line, so of any set of triangles that tile a neighbourhood of the pixel
    centre exactly one contains it, whether the centre lies on a shared edge or on a vertex where a fan meets."""
    return (dx > 0) | ((dx == 0) & (dy < 0))


def _sliver_keys(tf, gx, gy, coef, D, S, k, abc, ox, oy) -> np.ndarray:
    """Depth keys of fragments of thin triangles (tf: triangle positions, (gx, gy): tile-local pixel centres).

    Barycentric weights from float64 edge functions, clamped to the triangle. When D is too small for even that
    (a triangle that is a segment to float64 precision) the key is interpolated along its longest edge instead.
    """
    with np.errstate(divide="ignore", invalid="ignore"):
        w0, w1 = ((cf[0][tf] * gx + cf[1][tf] * gy + cf[2][tf]) / D[tf] for cf in coef)
    w0 = np.clip(w0, 0, 1)
    w1 = np.minimum(np.clip(w1, 0, 1), 1 - w0)
    kt = k[tf]
    z = kt[:, 2] + w0 * (kt[:, 0] - kt[:, 2]) + w1 * (kt[:, 1] - kt[:, 2])
    seg = ~(D[tf] > 2.0 ** -30 * (S[0][tf] + S[1][tf])) | ~np.isfinite(z)
    if seg.any():
        q = np.stack([v[tf[seg]] for v in abc], 1)                      # (m, 3, 2) screen coordinates
        P = np.stack([ox[tf[seg]] + gx[seg], oy[tf[seg]] + gy[seg]], 1)
        e = np.argmax([np.sum((q[:, (n + 1) % 3] - q[:, n]) ** 2, axis=1) for n in range(3)], axis=0)
        m = np.arange(len(e))
        u, v = q[m, e], q[m, (e + 1) % 3]
        d = v - u
        s = np.clip(np.sum((P - u) * d, axis=1) / np.maximum(np.sum(d * d, axis=1), 1e-300), 0, 1)
        ks = kt[seg]
        z[seg] = ks[m, e] + s * (ks[m, (e + 1) % 3] - ks[m, e])
    return z.astype(np.float32)


def _rasterise(sxy: np.ndarray, key: np.ndarray, tris: np.ndarray, size: int):
    """Returns (tri_id image int32 [-1 = empty], depth-key image float32).

    Coverage rule: a pixel centre P is covered by a triangle iff P is strictly inside it, or lies on its boundary
    and the tie rule of :func:`_owned` assigns it to the triangle. Every edge sign used for that decision is the
    EXACT sign of the edge function of the triangle's float64 screen coordinates. Two triangles that share an edge
    (the same endpoint coordinates) therefore evaluate the same exact value with opposite signs, and the tie rule
    gives a centre on the edge to exactly one of them: their union is covered without holes and without double
    cover, whatever their size or position in the frame. A centre at a shared vertex goes to exactly one triangle
    of the fan around it, by the same argument (the tie rule is a consistent infinitesimal displacement).

    The exact signs cost almost nothing: edge functions are evaluated in float32 from tile-local coordinates
    (coefficients formed in float64 relative to the tile origin, so their magnitude is that of the tile, not of
    the frame), together with a rigorous bound on their rounding error. Only a pixel whose value lies within
    that bound of zero (in practice a centre within about 1e-6 px of an edge) is decided by exact integer
    arithmetic (:func:`_orient_exact`).
    """
    sxy = np.asarray(sxy, np.float64)
    tris = np.asarray(tris)
    P = sxy[tris]                                                  # (T, 3, 2)
    fin = np.isfinite(P).all(axis=(1, 2)) if not np.isfinite(sxy).all() else np.ones(len(tris), bool)
    # Orientation (sign of the doubled area D) in float64 with its error bound, as in _orient_sign. Each
    # triangle is rasterised counter-clockwise (D > 0, y down). A triangle whose sign the bound leaves open (a
    # sliver, nearly edge-on) is rasterised in both vertex orders instead: under the wrong order its three exact
    # edge values are the negated true ones, which cannot all be positive (they sum to the true D > 0), so that
    # copy covers nothing and the other covers exactly its pixels; if D is exactly 0 neither covers anything.
    dx, dy = P[:, 1, 0] - P[:, 0, 0], P[:, 1, 1] - P[:, 0, 1]
    ex, ey = P[:, 2, 0] - P[:, 0, 0], P[:, 2, 1] - P[:, 0, 1]
    l, r = dx * ey, dy * ex
    det, err = l - r, 8 * _U64 * (np.abs(l) + np.abs(r))
    pos_, neg_ = fin & (det > err), fin & (det < -err)
    open_ = fin & ~pos_ & ~neg_ & ~(((dx == 0) | (ey == 0)) & ((dy == 0) | (ex == 0)))  # exactly 0 otherwise
    if open_.any():
        src = np.repeat(np.arange(len(tris)), (pos_ | neg_).astype(np.int64) + 2 * open_)  # input triangle of each
        first = np.ones(len(src), bool)
        first[1:] = src[1:] != src[:-1]
        swap = np.where(open_[src], ~first, neg_[src])
    else:
        src = np.nonzero(pos_ | neg_)[0]
        swap = neg_[src]
    if len(src) < len(tris):
        vid, P = tris[src], P[src]
    else:
        vid = tris.copy()
    if swap.any():
        vid[swap, 1], vid[swap, 2] = vid[swap, 2], vid[swap, 1].copy()
        P[swap, 1], P[swap, 2] = P[swap, 2], P[swap, 1].copy()
    p0, p1, p2 = P[:, 0], P[:, 1], P[:, 2]
    lo = np.minimum(np.minimum(p0, p1), p2)
    hi = np.maximum(np.maximum(p0, p1), p2)
    with np.errstate(invalid="ignore"):
        # pixel i has its centre at i + 0.5: these are the first and last centres inside [lo, hi]
        x0 = np.maximum(np.ceil(lo[:, 0] - 0.5), 0)
        y0 = np.maximum(np.ceil(lo[:, 1] - 0.5), 0)
        x1 = np.minimum(np.floor(hi[:, 0] - 0.5), size - 1)
        y1 = np.minimum(np.floor(hi[:, 1] - 0.5), size - 1)
    sel = np.flatnonzero((x1 >= x0) & (y1 >= y0))
    x0, y0 = x0[sel].astype(np.int32), y0[sel].astype(np.int32)
    sizes = _tile_sizes(size)
    bs = sizes[np.searchsorted(sizes, np.maximum(x1[sel] - x0, y1[sel] - y0) + 1)]
    # a tile that fits in the frame is moved inside it (it still holds the bounding box), so no fragment of it
    # falls outside the frame and needs discarding
    fits = bs <= size
    x0 = np.where(fits, np.minimum(x0, size - bs), x0)
    y0 = np.where(fits, np.minimum(y0, size - bs), y0)
    order = np.argsort(bs, kind="stable")  # by tile size, input order within one: groups are slices
    sel, bs, x0, y0 = sel[order], bs[order], x0[order], y0[order]
    tri_img = np.full(size * size, -1, np.int32)
    if not len(sel):
        return tri_img.reshape(size, size), np.full((size, size), np.inf, np.float32)
    # Per-triangle set-up, for all triangles at once. Edge function of edge (u, v) at P = O + (gx, gy), O the
    # tile origin:  E = (vx-ux)(Py-uy) - (vy-uy)(Px-ux) = -dy*gx + dx*gy + (dy*lx - dx*ly),  d = v - u, l = u - O.
    # E0 (edge b->c) and E1 (edge c->a) are evaluated per pixel; E2 (edge a->b) = D - E0 - E1, D the doubled area.
    # Error bound: with S = (|dx| + |dy|)*B + |dy*lx| + |dx*ly| (the sum of the magnitudes of the terms), the
    # float64 coefficients carry at most ~4 u64 * S of error, their float32 rounding and the float32 evaluation
    # (one product, two sums) at most ~4 u32 * S; 8 u32 * S bounds both with a wide margin. The tiny absolute
    # term covers float32 underflow.
    Bt = bs.astype(np.float64)
    a, b, c = p0[sel], p1[sel], p2[sel]
    ox, oy = x0.astype(np.float64), y0.astype(np.float64)
    coef, bnd, S = [], [], []
    for u, v in ((b, c), (c, a)):
        dx, dy = v[:, 0] - u[:, 0], v[:, 1] - u[:, 1]
        cx, cy = dy * (u[:, 0] - ox), dx * (u[:, 1] - oy)
        S.append((np.abs(dx) + np.abs(dy)) * Bt + np.abs(cx) + np.abs(cy))
        bnd.append(8 * _U32 * S[-1] + 1e-30)
        coef.append((-dy, dx, cx - cy))
    # D and its bound as computed for the orientation (swapping two vertices negates the float value exactly)
    D, errD = np.where(swap, -det[src], det[src])[sel], err[src][sel]
    # E2 > 0  <=>  E0 + E1 < D; the float32 sum adds u32 * (S0 + S1), D carries errD
    bnd2 = 1.01 * (bnd[0] + bnd[1] + 2 * _U32 * (S[0] + S[1]) + errD + 4 * _U64 * np.abs(D))
    k = np.asarray(key, np.float32)[vid[sel]].astype(np.float64)  # (n, 3) in counter-clockwise vertex order
    # where the float32 weights' error bound (bnd / D) exceeds 2**-12, about the resolution of a float32 key
    # across a triangle, i.e. for slivers far thinner than their tile, the key is computed from float64 edge
    # functions instead (see _sliver_keys), so it is not extrapolated past the triangle
    thin = (bnd[0] + bnd[1]) > 2.0 ** -12 * D
    Dk = np.where(thin, 1.0, D)
    up, down = np.float32(np.inf), np.float32(-np.inf)
    F = np.empty((15, len(sel)), np.float32)
    F[0], F[1], F[2] = coef[0]
    F[3], F[4], F[5] = coef[1]
    # candidate thresholds: not certainly outside on any edge (rounded outward)
    F[6], F[7], F[8] = -bnd[0], -bnd[1], D + bnd2
    np.nextafter(F[6:8], down, out=F[6:8])
    np.nextafter(F[8], up, out=F[8])
    # certainly inside on every edge (rounded inward); a candidate failing one of these is decided exactly
    F[9], F[10], F[11] = bnd[0], bnd[1], D - bnd2
    np.nextafter(F[9:11], up, out=F[9:11])
    np.nextafter(F[11], down, out=F[11])
    # depth key = k2 + w0 (k0 - k2) + w1 (k1 - k2), w = E / D
    F[12], F[13], F[14] = k[:, 2], (k[:, 0] - k[:, 2]) / Dk, (k[:, 1] - k[:, 2]) / Dk
    ox_i, oy_i, tid = x0, y0, src[sel]
    groups = []
    cuts = [0, *(np.flatnonzero(bs[1:] != bs[:-1]) + 1).tolist(), len(bs)]
    for lo_, hi_ in zip(cuts[:-1], cuts[1:]):
        B = int(bs[lo_])
        step = max(1, _TILE_BATCH_ELEMENTS // (B * B))  # bounds the (T, B, B) work arrays
        groups.extend((B, q, min(q + step, hi_)) for q in range(lo_, hi_, step))
    pix_l, key_l, q_l, amb_l, thin_l = [], [], [], [], []
    n = 0                                                      # fragments so far
    for B, q0, q1 in groups:
        g = np.arange(B, dtype=np.float32) + 0.5               # tile-local pixel-centre offsets (exact)
        A0, B0, C0, A1, B1, C1, t0, t1, t2 = F[:9, q0:q1]
        E0 = (A0[:, None] * g + C0[:, None])[:, None, :] + (B0[:, None] * g)[:, :, None]   # (T, B, B): [t, j, i]
        E1 = (A1[:, None] * g + C1[:, None])[:, None, :] + (B1[:, None] * g)[:, :, None]
        cand = (E0 >= t0[:, None, None]) & (E1 >= t1[:, None, None]) & ((E0 + E1) <= t2[:, None, None])
        t, j, i = np.nonzero(cand)
        if not len(t):
            continue
        fx = ox_i[q0:q1].take(t) + i
        fy = oy_i[q0:q1].take(t) + j
        if B > size:  # only a tile wider than the frame can reach past it
            v = (fx < size) & (fy < size)
            t, j, i, fx, fy = t[v], j[v], i[v], fx[v], fy[v]
        e0, e1 = E0[t, j, i], E1[t, j, i]
        s0, s1, s2, kc, ka, kb = np.take(F[9:, q0:q1], t, axis=1)
        amb_l.append(n + np.flatnonzero((e0 <= s0) | (e1 <= s1) | ((e0 + e1) >= s2)))
        if thin[q0:q1].any():
            thin_l.append(n + np.flatnonzero(thin[q0:q1].take(t)))
        pix_l.append(fy * size + fx)
        key_l.append(kc + ka * e0 + kb * e1)
        q_l.append(q0 + t)
        n += len(t)
    zbuf = np.full(size * size, np.inf, np.float32)
    if not n:
        return tri_img.reshape(size, size), zbuf.reshape(size, size)
    pix, z, q = np.concatenate(pix_l), np.concatenate(key_l), np.concatenate(q_l)
    amb = np.concatenate(amb_l)
    f = np.concatenate(thin_l) if thin_l else amb[:0]
    if len(amb):
        # exact decision for the fragments that are not certainly inside
        ta, pa = q[amb], pix[amb]
        pu = np.concatenate([b[ta], c[ta], a[ta]])         # the three edges b->c, c->a, a->b, stacked
        pw = np.concatenate([c[ta], a[ta], b[ta]])
        px, py = np.tile(pa % size + 0.5, 3), np.tile(pa // size + 0.5, 3)
        sg = _orient_sign(pu[:, 0], pu[:, 1], pw[:, 0], pw[:, 1], px, py)
        good = ((sg > 0) | ((sg == 0) & _owned(pw[:, 0] - pu[:, 0], pw[:, 1] - pu[:, 1]))).reshape(3, -1).all(axis=0)
        if not good.all():
            keep = np.ones(n, bool)
            keep[amb[~good]] = False
            if len(f):
                f = (np.cumsum(keep) - 1)[f[keep[f]]]
            pix, z, q = pix[keep], z[keep], q[keep]
    if len(f):
        tf, pf = q[f], pix[f]
        z[f] = _sliver_keys(tf, pf % size - ox_i[tf] + 0.5, pf // size - oy_i[tf] + 0.5, coef, D, S, k, (a, b, c), ox_i, oy_i)
    tid = tid.take(q)
    # nearest key per pixel; of fragments at that key, the last in fragment order (tile groups in ascending size,
    # triangles in input order within a group) wins, so equal depths resolve deterministically
    np.minimum.at(zbuf, pix, z)
    at = np.flatnonzero(z == zbuf[pix])
    pa = pix[at]
    if len(at) * 16 < size * size:  # few pixels: sort them rather than touch a frame-sized array
        o = np.argsort(pa, kind="stable")
        ps = pa[o]
        last = np.ones(len(o), bool)
        last[:-1] = ps[1:] != ps[:-1]
        w = at[o[last]]
        tri_img[pix[w]] = tid[w]
    else:
        win = np.full(size * size, -1, np.int32 if len(pix) < 2 ** 31 else np.int64)
        np.maximum.at(win, pa, at.astype(win.dtype))
        tri_img[pa] = tid[win[pa]]
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
    node_ids: tuple[str, ...] = ()   # node_id per `meshes` index


def _uv_checker(img, hit, tri_img, tris, sxy, tri_mesh, meshes, uvs, size) -> None:
    """Overwrite shaded pixels of meshes with UVs by shading * checker(uv): screen-affine barycentrics."""
    ys, xs = np.nonzero(hit)
    t = tri_img[ys, xs]
    sel = np.array([meshes[i].node_id in uvs for i in range(len(meshes))])[tri_mesh[t]]
    ys, xs, t = ys[sel], xs[sel], t[sel]
    if not len(t):
        return
    w0, w1, w2 = _corner_weights(tri_img, tris, sxy, ys, xs)
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


def _corner_weights(tri_img, tris, sxy, ys, xs) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Screen-affine barycentric weights of the pixel centres (ys, xs) in the triangle each one shows. Built from
    differences between the centre and the triangle's own vertices, in float64, so they do not cancel."""
    t = tri_img[ys, xs]
    p0, p1, p2 = (sxy[tris[t, i]] for i in range(3))
    d = (p1[:, 0] - p0[:, 0]) * (p2[:, 1] - p0[:, 1]) - (p1[:, 1] - p0[:, 1]) * (p2[:, 0] - p0[:, 0])
    px, py = xs + 0.5, ys + 0.5
    w1 = ((px - p0[:, 0]) * (p2[:, 1] - p0[:, 1]) - (py - p0[:, 1]) * (p2[:, 0] - p0[:, 0])) / d
    w2 = ((p1[:, 0] - p0[:, 0]) * (py - p0[:, 1]) - (p1[:, 1] - p0[:, 1]) * (px - p0[:, 0])) / d
    return 1 - w1 - w2, w1, w2


def _shade_with_normals(img, hit, tri_img, tris, sxy, tri_mesh, tri_start, meshes, normals, toward_cam, facing, key,
                        base=None) -> None:
    """Overwrite the flat shade of the meshes that come with shading normals by a per-pixel one: the corner
    normals interpolated across the triangle, lit by the same two lights. A triangle seen from behind is lit from
    the side that is seen, as in the flat shade. ``base``: (meshes, 3) colour per mesh (None: clay)."""
    ys, xs = np.nonzero(hit)
    t = tri_img[ys, xs]
    sel = np.array([m.node_id in normals for m in meshes])[tri_mesh[t]]
    ys, xs, t = ys[sel], xs[sel], t[sel]
    if not len(t):
        return
    w0, w1, w2 = _corner_weights(tri_img, tris, sxy, ys, xs)
    mi = tri_mesh[t]
    n = np.zeros((len(t), 3))
    for i in np.unique(mi):
        k = mi == i
        c = np.asarray(normals[meshes[i].node_id], np.float64).reshape(-1, 3, 3)[tri_start[t[k]]]
        n[k] = w0[k, None] * c[:, 0] + w1[k, None] * c[:, 1] + w2[k, None] * c[:, 2]
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
    n *= facing[t][:, None]
    shade = 0.22 + 0.60 * np.maximum(n @ key, 0) + 0.28 * np.maximum(n @ toward_cam, 0)
    img[ys, xs] = np.clip((CLAY[None] if base is None else base[mi]) * shade[:, None], 0, 255).astype(np.uint8)


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
    normals: dict[str, np.ndarray] | None = None,
    feature_edges: bool = False,
    crease_deg: float = 40.0,
    colours: dict[str, Sequence[float]] | None = None,
) -> RenderBuffers:
    """Single rasterisation path: clay colour plus ID/depth buffers.

    feature_edges: darken the lines a shaded render shows as edges instead of every triangle edge (``wireframe``):
    each part's outline and its boundary with another part (from the ID buffer), and the edges whose faces meet at
    more than ``crease_deg`` (the hard edges of the shipped normals, ``compile_ir.crease_normals``), open edges and
    edges of more than two faces. Not with ``wireframe``.

    uvs: node_id -> (V,2) per-vertex UVs; when given, the colour is replaced by a UV
    checker pattern (tiling past [0,1] is visible) for those meshes.
    normals: node_id -> (T,3,3) shading normals, one per triangle corner. Those meshes are lit per pixel with
    the interpolated normal, the way a renderer lights a mesh that ships with normals; every other mesh keeps
    one normal per triangle (the clay preview: it shows the triangles, which is what a preview is for).
    camera: explicit camera (e.g. shared between two LODs); default auto-frames the meshes.
    colours: node_id -> base colour as displayed (sRGB, 0-255): those meshes are shaded in their colour instead of
    clay, with the same lights (the script path's colour views, ``harness.tools._render_views(colour=True)``). None:
    clay for every mesh, exactly as before.
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
    facing = np.where(n @ toward_cam < 0, -1.0, 1.0)  # double-sided
    n *= facing[:, None]
    key_dir = key_light(cam.view)
    shade = 0.22 + 0.60 * np.maximum(n @ key_dir, 0) + 0.28 * np.maximum(n @ toward_cam, 0)
    base = None
    if colours:
        base = np.array([colours.get(m.node_id, CLAY) for m in meshes], np.float32).reshape(-1, 3)
    col = np.clip((CLAY[None] if base is None else base[tri_mesh]) * shade[:, None], 0, 255).astype(np.uint8)
    hit = tri_img >= 0
    img[hit] = col[tri_img[hit]]
    if normals:
        _shade_with_normals(img, hit, tri_img, tris, sxy, tri_mesh, tri_start, meshes, normals, toward_cam, facing, key_dir, base)
    if uvs:
        _uv_checker(img, hit, tri_img, tris, sxy, tri_mesh, meshes, uvs, size)
    prim[hit] = tri_mesh[tri_img[hit]]
    tri_loc[hit] = tri_start[tri_img[hit]]
    depth[hit] = (-1.0 / zbuf[hit] if persp else zbuf[hit]).astype(np.float32)

    if wireframe and feature_edges:
        raise ValueError("draw either every triangle edge (wireframe) or the feature edges, not both")
    if wireframe:
        e = np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]])
        e = np.unique(np.sort(e, axis=1), axis=0)
        iy, ix = _visible_edge_pixels(e, sxy, key, z, persp, hit, zbuf, size)
        img[iy, ix] = (img[iy, ix] * 0.6).astype(np.uint8)
    elif feature_edges:
        mask = _part_outlines(prim, depth)
        e = _feature_edge_list(meshes, pts, tris, crease_deg)
        if len(e):
            iy, ix = _visible_edge_pixels(e, sxy, key, z, persp, hit, zbuf, size)
            mask[iy, ix] = True
        img[mask] = (img[mask] * 0.6).astype(np.uint8)
    return RenderBuffers(img, prim, tri_loc, depth, cam, view, size, nid)


def _visible_edge_pixels(e, sxy, key, z, persp, hit, zbuf, size) -> tuple[np.ndarray, np.ndarray]:
    """Pixels of the mesh edges ``e`` (vertex index pairs) that lie on a covered pixel and are not behind the surface
    there (within 1% of the depth range): the lines a wireframe overlay draws."""
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
    return iy[v], ix[v]


def _feature_edge_list(meshes, pts, tris, crease_deg: float) -> np.ndarray:
    """Vertex index pairs of the edges a shaded render with the shipped normals shows as lines: within each part (vertices
    welded by position, as ``compile_ir.crease_normals`` welds them), an edge whose two faces meet at more than
    ``crease_deg`` (a hard edge in the shipped normals), an open edge, and an edge of more than two faces. The edges
    inside a smooth surface (the triangulation) are not drawn."""
    from antigravity3d.kernel.fast_ops import _canon

    wid = np.zeros(len(pts), np.int64)
    off = base = 0
    for m in meshes:
        if not len(m.triangles):
            continue
        k = _canon(np.asarray(m.points, np.float64))
        wid[off:off + len(m.points)] = k + base
        base += int(k.max()) + 1 if len(k) else 0
        off += len(m.points)
    E = np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]])
    face = np.tile(np.arange(len(tris)), 3)
    W = np.sort(wid[E], axis=1)
    keep = W[:, 0] != W[:, 1]  # an edge welded to a point draws nothing
    E, face, W = E[keep], face[keep], W[keep]
    if not len(W):
        return np.zeros((0, 2), np.int64)
    _, first, inv, count = np.unique(W, axis=0, return_index=True, return_inverse=True, return_counts=True)
    inv = inv.ravel()
    p = pts[tris]
    fn = np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0])
    ln = np.linalg.norm(fn, axis=1, keepdims=True)
    unit = np.divide(fn, ln, out=np.zeros_like(fn), where=ln > 1e-20)
    order = np.argsort(inv, kind="stable")
    starts = np.concatenate([[0], np.cumsum(count)[:-1]])
    two = count == 2
    fa, fb = face[order[starts[two]]], face[order[starts[two] + 1]]
    hard = np.ones(len(count), bool)
    dots = (unit[fa] * unit[fb]).sum(axis=1)
    hard[two] = dots < np.cos(np.radians(crease_deg)) - 1e-9
    if any(getattr(m, "run", None) is not None and m.run.round for m in meshes):
        # the facets along a round part ship smooth (compile_ir.round_soft, the shipped normals' own rule): no line there
        from antigravity3d.kernel.compile_ir import round_soft
        from antigravity3d.kernel.surface_frame import round_dirs

        d = np.concatenate([round_dirs(m.points, m.triangles, getattr(m, "run", None)) for m in meshes if len(m.triangles)])
        if len(d) == len(tris):
            soft = round_soft(unit[fa], unit[fb], d[fa], d[fb], crease_deg)
            hard[np.flatnonzero(two)[soft]] = False
    return E[first[hard]]


def _part_outlines(prim: np.ndarray, depth: np.ndarray) -> np.ndarray:
    """Pixels where one part (or the background) meets another, marked on the nearer side: every part's outline, and
    the boundary of a flush plate on a face of its own colour, which shading alone does not show."""
    mask = np.zeros(prim.shape, bool)
    for a, b, da, db, ma, mb in ((prim[:, :-1], prim[:, 1:], depth[:, :-1], depth[:, 1:], mask[:, :-1], mask[:, 1:]),
                                 (prim[:-1], prim[1:], depth[:-1], depth[1:], mask[:-1], mask[1:])):
        diff = a != b
        near_a = diff & (da <= db)
        ma |= near_a
        mb |= diff & ~near_a
    return mask & (prim >= 0)


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
