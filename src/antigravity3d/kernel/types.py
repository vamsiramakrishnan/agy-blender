from __future__ import annotations
from collections.abc import Iterable
from dataclasses import dataclass
import numpy as np

@dataclass(frozen=True)
class MeshData:
    node_id: str
    prim_path: str
    points: np.ndarray      # (N, 3) float64, world space, metres, Z-up
    triangles: np.ndarray   # (M, 3) int32
    material_id: str | None = None
    collider: bool = True

def gltf_world_bounds(points: Iterable[np.ndarray], *, z_up: bool) -> list[list[float]]:
    """Tight world-space bounds of visual vertices in glTF's right-handed Y-up metres."""
    limits = [(p.min(axis=0), p.max(axis=0)) for p in points if len(p)]
    if not limits:
        raise ValueError("no renderable geometry for bounds")
    lo = np.min([b[0] for b in limits], axis=0)
    hi = np.max([b[1] for b in limits], axis=0)
    if z_up:
        return [[float(lo[0]), float(lo[2]), float(-hi[1])], [float(hi[0]), float(hi[2]), float(-lo[1])]]
    return [lo.tolist(), hi.tolist()]


def bounds_issue(expected, measured) -> str | None:
    """Compare each face of the AABB relative to that axis's span, with a 1 µm floor for flat geometry."""
    try:
        a, b = (np.asarray(v, dtype=np.float64) for v in (expected, measured))
    except (ValueError, TypeError):
        return "invalid world-space bounds"
    for box in (a, b):
        if box.shape != (2, 3) or not np.isfinite(box).all() or (box[1] < box[0]).any():
            return "missing or invalid world-space bounds"
    tolerance = np.maximum(0.005 * (a[1] - a[0]), 1e-6)
    drift = np.max(np.abs(a - b), axis=0)
    if (drift > tolerance).any():
        return f"bounds drift {drift.tolist()} metres exceeds 0.5% per-axis tolerance {tolerance.tolist()}"
    return None
