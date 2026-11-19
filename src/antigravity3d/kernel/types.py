from __future__ import annotations
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
