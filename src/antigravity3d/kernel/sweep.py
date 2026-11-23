"""A capped round rod along explicit local-space centreline samples.

The path is sampled by the author; no spline is fitted and no point is moved. Circular rings use the angle
bisector at each interior point and a rotation-minimising (parallel transported) frame. This avoids the flips a
Frenet frame has at straight sections and inflections. End rings are perpendicular to the end segments.

Each radial side is an open triangle ribbon with all vertices on its boundary. It can therefore be unfolded
isometrically by the existing analytic UV machinery, including under a non-uniform node transform. The two caps
have their own charts. Vertices are welded; UV seams and shading creases are introduced by the existing compiler.

Validation rejects coincident samples, reversals, locally collapsed inner bends, centreline crossings, and obvious
nonlocal tube collisions. It is not a general triangle self-intersection solver; existing asset gates still run.
"""

from __future__ import annotations

import math

import numpy as np

MAX_POINTS = 128
MAX_SEGMENTS = 64


def _closest_segments(p: np.ndarray, u: np.ndarray, q: np.ndarray, v: np.ndarray) -> tuple[float, float, float]:
    """Distance and parameters of the closest points of p+s*u and q+t*v, 0<=s,t<=1."""
    w = p - q
    a, b, c, d, e = float(u @ u), float(u @ v), float(v @ v), float(u @ w), float(v @ w)
    candidates = [(0.0, float(np.clip(e / c, 0, 1))), (1.0, float(np.clip((e + b) / c, 0, 1))),
                  (float(np.clip(-d / a, 0, 1)), 0.0), (float(np.clip((b - d) / a, 0, 1)), 1.0)]
    den = a * c - b * b
    if den > 1e-12 * a * c:
        s, t = (b * e - c * d) / den, (a * e - b * d) / den
        if 0 <= s <= 1 and 0 <= t <= 1:
            candidates.append((s, t))
    return min((float(np.linalg.norm(w + s * u - t * v)), s, t) for s, t in candidates)


def validate(path, radius: float, segments: int = 12, nid: str = "sweep") -> np.ndarray:
    """Return validated float64 centreline points, or a repairable, node-specific ValueError.

    At most 128 points and 64 radial sides bound both the quadratic centreline check and the generated mesh.
    Nearby intervals along the same rod are not treated as colliding capsules: that would reject densely sampled
    valid curves. The nonlocal clearance check applies only when the closest points are more than pi*radius apart
    along the path. Actual centreline crossings are refused irrespective of that separation.
    """
    if (not isinstance(radius, (int, float)) or isinstance(radius, bool)
            or not math.isfinite(radius) or radius <= 0):
        raise ValueError(f"{nid}: param 'radius' must be a finite number > 0, got {radius!r}")
    if (not isinstance(segments, (int, float)) or isinstance(segments, bool) or not math.isfinite(segments)
            or int(segments) != segments or not 3 <= segments <= MAX_SEGMENTS):
        raise ValueError(f"{nid}: param 'segments' must be an integer from 3 to {MAX_SEGMENTS}, got {segments!r}")
    if not isinstance(path, (list, tuple)) or not 2 <= len(path) <= MAX_POINTS:
        raise ValueError(f"{nid}: param 'path' needs 2 to {MAX_POINTS} explicit (x, y, z) points")
    for i, p in enumerate(path):
        if not isinstance(p, (list, tuple)) or len(p) != 3:
            raise ValueError(f"{nid}: path point {i} must be an (x, y, z) triple")
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in p):
            raise ValueError(f"{nid}: path point {i} must hold finite numbers")
    points = np.asarray(path, dtype=np.float64)
    delta = np.diff(points, axis=0)
    lengths = np.linalg.norm(delta, axis=1)
    eps = 1e-9 * max(float(np.ptp(points, axis=0).max()), radius, 1e-12)
    if (short := np.flatnonzero(lengths <= eps)).size:
        i = int(short[0])
        raise ValueError(f"{nid}: path points {i} and {i + 1} coincide (a zero-length segment); remove one")
    directions = delta / lengths[:, None]
    half_tan = np.zeros(len(points))
    if len(points) > 2:
        dots = np.clip(np.einsum("ij,ij->i", directions[:-1], directions[1:]), -1, 1)
        if (reverse := np.flatnonzero(dots <= -1 + 1e-8)).size:
            raise ValueError(f"{nid}: path reverses direction at point {int(reverse[0]) + 1}; use a sampled bend")
        half_tan[1:-1] = np.sqrt((1 - dots) / (1 + dots))
        # The inside offsets of two round sections meet radius*tan(half the turn) from their joint. Those reaches
        # must fit within each segment; otherwise tight bends overlap (including almost-reversals with long
        # segments). For dense circular samples this tends to r < the centreline's bend radius.
        if (tight := np.flatnonzero(lengths <= radius * (half_tan[:-1] + half_tan[1:]) + eps)).size:
            i = int(tight[0])
            raise ValueError(f"{nid}: radius is too large for the bend at path segment {i}-{i + 1}; "
                             "reduce radius or widen the bend")
    along = np.concatenate(([0.0], np.cumsum(lengths)))
    for i in range(len(delta)):
        for j in range(i + 2, len(delta)):
            # AABB rejection keeps ordinary open rails cheap; inflation is conservative for the round section.
            if np.any(np.maximum(points[i], points[i + 1]) + 2 * radius < np.minimum(points[j], points[j + 1])):
                continue
            if np.any(np.maximum(points[j], points[j + 1]) + 2 * radius < np.minimum(points[i], points[i + 1])):
                continue
            distance, s, t = _closest_segments(points[i], delta[i], points[j], delta[j])
            separation = along[j] + t * lengths[j] - along[i] - s * lengths[i]
            if distance <= eps:
                raise ValueError(f"{nid}: path crosses or touches itself (segment {i}-{i + 1} meets {j}-{j + 1})")
            if separation >= math.pi * radius and distance <= 2 * radius + eps:
                raise ValueError(f"{nid}: nonlocal path segments {i}-{i + 1} and {j}-{j + 1} are too close "
                                 "for this radius; separate them or reduce radius")
    return points


def _frames(points: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    directions = np.diff(points, axis=0)
    directions /= np.linalg.norm(directions, axis=1)[:, None]
    tangent = np.empty_like(points)
    tangent[0], tangent[-1] = directions[0], directions[-1]
    tangent[1:-1] = directions[:-1] + directions[1:]
    tangent /= np.linalg.norm(tangent, axis=1)[:, None]
    normal = np.empty_like(points)
    seed = np.eye(3)[int(np.argmin(np.abs(tangent[0])))]
    normal[0] = seed - tangent[0] * float(seed @ tangent[0])
    normal[0] /= np.linalg.norm(normal[0])
    for i in range(1, len(points)):
        v = np.cross(tangent[i - 1], tangent[i])
        c = float(tangent[i - 1] @ tangent[i])
        # Minimal rotation mapping the old tangent to the new one; valid paths have no antiparallel tangents.
        n = normal[i - 1] + np.cross(v, normal[i - 1]) + np.cross(v, np.cross(v, normal[i - 1])) / (1 + c)
        n -= tangent[i] * float(n @ tangent[i])
        normal[i] = n / np.linalg.norm(n)
    return tangent, normal, np.cross(tangent, normal)


def build(path, radius: float, segments: int = 12, nid: str = "sweep") -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return welded float32 points, outward int32 triangles, and one int32 chart ID per triangle."""
    points = validate(path, radius, segments, nid)
    n = int(segments)
    _, normal, binormal = _frames(points)
    angles = 2 * math.pi * np.arange(n) / n
    rings = points[:, None, :] + radius * (np.cos(angles)[None, :, None] * normal[:, None, :]
                                          + np.sin(angles)[None, :, None] * binormal[:, None, :])
    vertices = np.vstack((rings.reshape(-1, 3), points[[0, -1]])).astype(np.float32)
    triangles, charts = [], []
    for i in range(len(points) - 1):
        for j in range(n):
            k = (j + 1) % n
            a, b, c, d = i * n + j, i * n + k, (i + 1) * n + k, (i + 1) * n + j
            triangles.extend(((a, b, c), (a, c, d)))
            charts.extend((j, j))
    start, end = len(vertices) - 2, len(vertices) - 1
    last = (len(points) - 1) * n
    for j in range(n):
        k = (j + 1) % n
        triangles.append((start, k, j))
        charts.append(n)
        triangles.append((end, last + j, last + k))
        charts.append(n + 1)
    tris = np.asarray(triangles, dtype=np.int32)
    p = vertices[tris].astype(np.float64)
    area2 = np.linalg.norm(np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]), axis=1)
    if not np.isfinite(vertices).all() or not np.isfinite(area2).all() or np.any(area2 == 0):
        raise ValueError(f"{nid}: sweep collapses at mesh precision; move the local path nearer the origin or enlarge it")
    return vertices, tris, np.asarray(charts, dtype=np.int32)
