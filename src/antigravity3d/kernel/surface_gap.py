"""Surface distance between two parts of one asset: are they within ``limit`` of each other?

Two triangle meshes are within ``limit`` when some triangle of one is within ``limit`` of some triangle of the other.
The distance of two triangles is zero when they intersect and otherwise the smallest of the distances of each corner
to the other triangle (6) and of each edge to each edge (9): a pair of triangles that cross has an edge through the
other's face, which no corner or edge-edge distance sees (a barrel's hoop, wider than the body it is slid over, crosses
the body's surface this way and has no vertex within centimetres of it), so the edge-through-face test is part of it.

Narrowing, each step cheap before the next: boxes (``within`` is a closed form for two axis-aligned boxes, the common
case), then only the triangles whose boxes come near the other part's box, then only the pairs of those whose boxes come
within ``limit``, then the corner-to-face tests (a face resting on a face has corners on it), then the rest.
"""

from __future__ import annotations

import numpy as np

CHUNK_PAIRS = 200_000  # triangle pairs tested at once


def is_axis_box(points: np.ndarray, lo: np.ndarray, hi: np.ndarray, volume: float, rel: float = 1e-9) -> bool:
    """A closed mesh whose every vertex sits at a corner of its own bounding box and whose volume is the box's is that
    box (a solid inside a box that fills it is the box)."""
    size = hi - lo
    full = float(np.prod(size))
    if full <= 0 or abs(volume - full) > rel * full:
        return False
    tol = rel * max(float(size.max()), 1e-12)
    return bool(np.all((np.abs(points - lo) <= tol) | (np.abs(points - hi) <= tol)))


def box_distance(lo_a: np.ndarray, hi_a: np.ndarray, lo_b: np.ndarray, hi_b: np.ndarray) -> float:
    """Distance between the surfaces of two axis-aligned boxes: the gap of their extents when apart, 0 when they
    overlap or touch (their surfaces cross) unless one lies inside the other, then the smallest margin."""
    gap = np.maximum(np.maximum(lo_a - hi_b, lo_b - hi_a), 0.0)
    if gap.any():
        return float(np.linalg.norm(gap))
    for inner_lo, inner_hi, outer_lo, outer_hi in ((lo_a, hi_a, lo_b, hi_b), (lo_b, hi_b, lo_a, hi_a)):
        if (inner_lo >= outer_lo).all() and (inner_hi <= outer_hi).all():
            return float(np.minimum(inner_lo - outer_lo, outer_hi - inner_hi).min())
    return 0.0


def point_box_distance(points: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
    """Distance of each point to the surface of the axis-aligned box lo..hi (inside points: to the nearest face)."""
    outside = np.maximum(np.maximum(lo - points, points - hi), 0.0)
    inside = np.minimum(points - lo, hi - points).min(axis=1)
    return np.where((outside == 0.0).all(axis=1), inside, np.sqrt(np.einsum("ij,ij->i", outside, outside)))


def corner_touches(points: np.ndarray, other: "Tris", lo: np.ndarray, hi: np.ndarray, limit: float) -> bool:
    """Is some vertex of one part within ``limit`` of the surface of the other (given as ``other``, its box lo..hi)? Each such
    vertex is a point of the first surface, so a hit proves the parts touch; a miss proves nothing (the parts may cross
    without a vertex near: see ``within``). Only the vertices inside the other's box and the triangles near them are
    measured: parts that rest on each other have corners on the contact (a cap on a face), nearly always."""
    near = points[np.all((points >= lo - limit) & (points <= hi + limit), axis=1)]
    if not len(near):
        return False
    tri = other.near(near.min(axis=0), near.max(axis=0), limit)
    if not len(tri):
        return False
    from antigravity3d.kernel.fast_ops import _point_triangle_distance

    step = max(1, CHUNK_PAIRS // len(tri))
    for s0 in range(0, len(near), step):
        pts = near[s0:s0 + step]
        p, t = np.repeat(pts, len(tri), axis=0), np.tile(tri, len(pts))
        if (_point_triangle_distance(p, other.c[t][:, 0], other.c[t][:, 1], other.c[t][:, 2]) <= limit).any():
            return True
    return False


def _dot(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    return np.einsum("ij,ij->i", x, y)


def _segment_segment(p1: np.ndarray, q1: np.ndarray, p2: np.ndarray, q2: np.ndarray) -> np.ndarray:
    """Row-wise distance between segments p1q1 and p2q2 (Ericson, Real-Time Collision Detection 5.1.9)."""
    d1, d2, r = q1 - p1, q2 - p2, p1 - p2
    a, e, f, c, b = _dot(d1, d1), _dot(d2, d2), _dot(d2, r), _dot(d1, r), _dot(d1, d2)
    den = a * e - b * b
    s = np.where(den > 1e-18 * np.maximum(a * e, 1e-300), np.clip((b * f - c * e) / np.where(den > 0, den, 1.0), 0.0, 1.0), 0.0)
    t = (b * s + f) / np.maximum(e, 1e-300)
    low, high = t < 0.0, t > 1.0
    t = np.clip(t, 0.0, 1.0)
    s = np.where(low, np.clip(-c / np.maximum(a, 1e-300), 0.0, 1.0),
                 np.where(high, np.clip((b - c) / np.maximum(a, 1e-300), 0.0, 1.0), s))
    diff = (p1 + d1 * s[:, None]) - (p2 + d2 * t[:, None])
    return np.sqrt(_dot(diff, diff))


def _segment_through(p: np.ndarray, q: np.ndarray, a: np.ndarray, b: np.ndarray, c: np.ndarray) -> np.ndarray:
    """Row-wise: does segment pq cross the face of triangle abc (Moller-Trumbore; a segment in the plane is left to
    the distance tests)."""
    d, e1, e2 = q - p, b - a, c - a
    h = np.cross(d, e2)
    det = _dot(e1, h)
    ok = np.abs(det) > 1e-18
    inv = 1.0 / np.where(ok, det, 1.0)
    s = p - a
    u = inv * _dot(s, h)
    qv = np.cross(s, e1)
    v = inv * _dot(d, qv)
    t = inv * _dot(e2, qv)
    return ok & (u >= 0) & (v >= 0) & (u + v <= 1) & (t >= 0) & (t <= 1)


def _pairs_within(A: np.ndarray, B: np.ndarray, limit: float) -> bool:
    """Is some triangle pair (A: (k,3,3), B: (k,3,3), row-wise) within ``limit``?"""
    from antigravity3d.kernel.fast_ops import _point_triangle_distance

    for corner in range(3):
        if (_point_triangle_distance(A[:, corner], B[:, 0], B[:, 1], B[:, 2]) <= limit).any():
            return True
        if (_point_triangle_distance(B[:, corner], A[:, 0], A[:, 1], A[:, 2]) <= limit).any():
            return True
    for k in range(3):
        if _segment_through(A[:, k], A[:, (k + 1) % 3], B[:, 0], B[:, 1], B[:, 2]).any():
            return True
        if _segment_through(B[:, k], B[:, (k + 1) % 3], A[:, 0], A[:, 1], A[:, 2]).any():
            return True
    for i in range(3):
        for j in range(3):
            if (_segment_segment(A[:, i], A[:, (i + 1) % 3], B[:, j], B[:, (j + 1) % 3]) <= limit).any():
                return True
    return False


class Tris:
    """One part's triangles as (M, 3, 3) corners with their boxes, built once and shared by every pair it is in."""

    __slots__ = ("c", "lo", "hi")

    def __init__(self, points: np.ndarray, triangles: np.ndarray) -> None:
        self.c = np.asarray(points, np.float64)[np.asarray(triangles)]
        self.lo = self.c.min(axis=1)
        self.hi = self.c.max(axis=1)

    def near(self, lo: np.ndarray, hi: np.ndarray, limit: float) -> np.ndarray:
        """Indices of the triangles whose box comes within ``limit`` of the box lo..hi."""
        return np.flatnonzero(np.all((self.lo <= hi + limit) & (self.hi >= lo - limit), axis=1))


def within(a: Tris, lo_a: np.ndarray, hi_a: np.ndarray, b: Tris, lo_b: np.ndarray, hi_b: np.ndarray, limit: float) -> bool:
    """True iff the surfaces of two meshes (their part boxes given) come within ``limit`` of each other, intersection
    included."""
    ia, ib = a.near(lo_b, hi_b, limit), b.near(lo_a, hi_a, limit)
    if not len(ia) or not len(ib):
        return False
    step = max(1, CHUNK_PAIRS // len(ib))
    for s in range(0, len(ia), step):
        rows = ia[s:s + step]
        ok = np.all((a.lo[rows][:, None] <= b.hi[ib][None] + limit) & (a.hi[rows][:, None] >= b.lo[ib][None] - limit), axis=2)
        x, y = np.nonzero(ok)
        if len(x) and _pairs_within(a.c[rows[x]], b.c[ib[y]], limit):
            return True
    return False
