from __future__ import annotations
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
import numpy as np

@dataclass(frozen=True)
class MeshData:
    node_id: str
    prim_path: str
    points: np.ndarray      # (N, 3) float64, world space, metres, Z-up
    triangles: np.ndarray   # (M, 3) int32
    material_id: str | None = None
    collider: bool = True
    # (M,) int chart id per triangle when the exact developable UV charts are known (IR primitives, see
    # compile_ir.primitive_charts); None -> the unwrap stage computes charts (imported / sanitized meshes).
    uv_charts: np.ndarray | None = None
    # Whether the bound material is opaque (``coplanar.opaque_materials`` of the scene IR, as the exporter reads it):
    # set by the stage manager; None for meshes that do not come from a managed stage (nothing is assumed). Not part
    # of any cache key: it follows from the materials, which the keys hash.
    opaque: bool | None = None
    # (K, 3) world-space points whose convex hull is the part's collision shape, when the kernel knows a cheaper one
    # than the render mesh: the sharp box of a bevelled or inset box, the sharp cylinder of a bevelled one
    # (compile_ir.collider_proxy). None: collision is made from ``points``. Whoever moves ``points`` moves these too.
    collider_points: np.ndarray | None = None
    # The bound material's parameters (compile_ir.MaterialParams, set by the stage manager; None: no material, or not
    # from a managed stage). The layout asks the surface sources how much detail it carries (surface_detail.
    # detail_px_per_m): a shared atlas is laid out no denser than its most detailed material needs. Not part of any
    # cache key: it follows from the materials, which the keys hash.
    material: Any = None
    # The layout the asset shipped with (fast_ops.KeepLayout), when this design is an edit of a shipped package
    # (StageManager.keep_layout): the export keeps every chart that is still there unchanged where it was.
    keep_layout: Any = None
    # The line the part is round about (``Run``, world space): a script tube's path, a cylinder's, cone's, capsule's or
    # lathe's axis (compile_ir.node_run, read by the stage). None for every other part. Whoever moves ``points`` moves
    # this too.
    run: "Run | None" = None
    # The way the part's wood runs as its author said it (``grain=``), a unit vector in world space; None: the painter
    # chooses (surface_frame.part_grain). Whoever turns ``points`` turns this too.
    grain: np.ndarray | None = None


@dataclass(frozen=True)
class Run:
    """A part's run: the centre line its material follows and how thick the part is along it.

    ``points`` (K >= 2, world metres) in order along the part; ``radii`` (K,) the distance from the line to the
    surface at each point (a rod's radius, a board's half-thickness), 0 where unknown; ``round``: the part is round
    about the line by construction (a script's tube), so the facet lines along it are not edges, in the shading the
    package ships or in the wear the painter draws; ``closed``: the last point joins the first (a ring)."""

    points: np.ndarray
    radii: np.ndarray
    round: bool = False
    closed: bool = False

    def moved(self, f) -> "Run":
        """The run with ``f`` applied to its points (a translation or any affine map of the part's points)."""
        return Run(np.asarray(f(np.asarray(self.points, np.float64)), np.float64), self.radii, self.round, self.closed)

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
