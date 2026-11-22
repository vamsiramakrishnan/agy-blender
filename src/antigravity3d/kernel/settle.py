"""Deterministic vertical contact settling (no physics engine).

Each movable mesh is dropped along -Z until it rests on the ground plane or on
the meshes below it, or lifted if it penetrates. The vertical gap between a
movable ``M`` and the supporters is the minimum of

* downward rays from samples of M's down-facing surface (vertices, edge
  midpoints, centroids) against the supporters' up-facing triangles, and
* upward rays from samples of the supporters' up-facing surface that lie under
  M's footprint against M's down-facing triangles,

so vertex-on-face and edge/face contacts are both seen. Remaining blind spot:
a contact located at an edge-edge crossing that is not sampled is approximated
to within the sampling spacing; ``contact_report`` can re-check a result.

Supporter surfaces more than half of M's height above M's lower surface at the
same XY are ignored, so ceilings above the object are not mistaken for support
and an object may be lifted out of a penetration up to half its own height.

Tilting ("allow_tilt") is intentionally NOT implemented: a robust 3-point rest
needs convex-contact manifolds this cheap pass does not compute.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace

import numpy as np

from antigravity3d.ir import SceneIR, SetTransform, Transform
from antigravity3d.kernel.spatial import vertical_hits
from antigravity3d.kernel.types import MeshData

GROUND_ID = "__ground__"
_CONVERGED = 1e-9
_NZ = 1e-6
_CHUNK = 400_000  # max rays x triangles per vectorised block


def _unique_edge_mids(tri_idx: np.ndarray, pts: np.ndarray) -> np.ndarray:
    e = np.concatenate([tri_idx[:, [0, 1]], tri_idx[:, [1, 2]], tri_idx[:, [2, 0]]])
    e = np.sort(e, axis=1)
    e = np.unique(e[:, 0].astype(np.int64) * len(pts) + e[:, 1])
    return (pts[e // len(pts)] + pts[e % len(pts)]) / 2


class _Geo:
    """Triangle soup split into down-/up-facing parts with contact sample points."""

    __slots__ = ("lo", "hi", "low_tri", "up_tri", "low_s", "up_s")

    def __init__(self, lo, hi, low_tri, up_tri, low_s, up_s):
        self.lo, self.hi, self.low_tri, self.up_tri, self.low_s, self.up_s = lo, hi, low_tri, up_tri, low_s, up_s

    @classmethod
    def from_mesh(cls, m: MeshData) -> "_Geo":
        pts, tris = np.asarray(m.points, float), np.asarray(m.triangles)
        if not len(tris):
            z = np.empty((0, 3))
            return cls(pts.min(0) if len(pts) else np.zeros(3), pts.max(0) if len(pts) else np.zeros(3),
                       np.empty((0, 3, 3)), np.empty((0, 3, 3)), z, z)
        tri = pts[tris]
        n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        nz = n[:, 2] / np.maximum(np.linalg.norm(n, axis=1), 1e-300)
        low, up = nz < -_NZ, nz > _NZ

        def samples(mask: np.ndarray, centroids: bool) -> np.ndarray:
            if not mask.any():
                return np.empty((0, 3))
            ti = tris[mask]
            parts = [pts[np.unique(ti)], _unique_edge_mids(ti, pts)]
            if centroids:
                parts.append(tri[mask].mean(1))
            return np.concatenate(parts)

        return cls(pts.min(0), pts.max(0), tri[low], tri[up], samples(low, True), samples(up, False))

    def shifted(self, dz: float) -> "_Geo":
        d = np.array([0.0, 0.0, dz])
        return _Geo(self.lo + d, self.hi + d, self.low_tri + d, self.up_tri + d, self.low_s + d, self.up_s + d)


def _xy_overlap(a: _Geo, b: _Geo, pad: float = 1e-9) -> bool:
    return bool(np.all(a.lo[:2] <= b.hi[:2] + pad) and np.all(b.lo[:2] <= a.hi[:2] + pad))


def _in_xy(pts: np.ndarray, lo: np.ndarray, hi: np.ndarray, pad: float = 1e-9) -> np.ndarray:
    return np.all((pts[:, :2] >= lo[:2] - pad) & (pts[:, :2] <= hi[:2] + pad), axis=1)


def _tri_in_xy(tri: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
    return np.all((tri[..., :2].max(1) >= lo[:2] - 1e-9) & (tri[..., :2].min(1) <= hi[:2] + 1e-9), axis=1)


def _hits(tri: np.ndarray, rays: np.ndarray):
    """Yields (row_slice, (rows, T) hit heights) in memory-bounded chunks."""
    step = max(1, _CHUNK // max(1, len(tri)))
    for i in range(0, len(rays), step):
        yield i, vertical_hits(tri, rays[i : i + step, :2])


def _pair_gap(m: _Geo, s: _Geo, allow: float) -> float:
    """Minimum vertical gap of ``m`` above supporter ``s`` (+inf if no vertical overlap)."""
    best = np.inf
    if not _xy_overlap(m, s) or not len(s.up_tri) or not len(m.low_tri):
        return best
    # downward: m's lower samples -> s's up-facing triangles
    sel = _in_xy(m.low_s, s.lo, s.hi)
    tsel = s.up_tri[_tri_in_xy(s.up_tri, m.lo, m.hi)]
    if sel.any() and len(tsel):
        src = m.low_s[sel]
        for i, z in _hits(tsel, src):
            l = src[i : i + len(z), 2:3]
            z = np.where(~np.isnan(z) & (z <= l + allow), z, -np.inf).max(1)
            ok = np.isfinite(z)
            if ok.any():
                best = min(best, float((l[:, 0] - z)[ok].min()))
    # upward: s's up-facing samples under m's footprint -> m's down-facing triangles
    sel = _in_xy(s.up_s, m.lo, m.hi)
    tsel = m.low_tri[_tri_in_xy(m.low_tri, s.lo, s.hi)]
    if sel.any() and len(tsel):
        src = s.up_s[sel]
        for i, z in _hits(tsel, src):
            sz = src[i : i + len(z), 2:3]
            z = np.where(~np.isnan(z) & (z >= sz - allow), z, np.inf).min(1)
            ok = np.isfinite(z)
            if ok.any():
                best = min(best, float((z - sz[:, 0])[ok].min()))
    return best


def _min_gap(m: _Geo, supporters: list[_Geo], ground_z: float | None) -> float:
    zmin, zmax = float(m.lo[2]), float(m.hi[2])
    g = np.inf if ground_z is None else zmin - ground_z
    allow = (zmax - zmin) / 2
    for s in supporters:
        g = min(g, _pair_gap(m, s, allow))
    return g


@dataclass(frozen=True)
class NodeSettle:
    node_id: str
    dz: float  # world-space rigid translation delta along +Z (metres)
    min_gap: float  # signed vertical gap to supporters/ground after settling (<0 = penetration)
    max_penetration: float
    iterations: int
    rotation: tuple[float, float, float] | None = None  # always None: tilt is not implemented


@dataclass
class SettleResult:
    nodes: dict[str, NodeSettle]
    iterations: int
    ms: float
    converged: bool = True

    def apply(self, meshes: list[MeshData]) -> list[MeshData]:
        """Meshes with the settling deltas applied (non-moved meshes returned as-is)."""
        out = []
        for m in meshes:
            ns = self.nodes.get(m.node_id)
            if ns is None or ns.dz == 0.0:
                out.append(m)
            else:
                dz = np.array([0.0, 0.0, ns.dz])
                out.append(replace(m, points=m.points + dz,
                                   collider_points=None if m.collider_points is None else m.collider_points + dz))
        return out

    def to_ir_ops(self, scene: SceneIR) -> list[SetTransform]:
        """``SetTransform`` ops applying the world-space dz to each moved node's local transform.

        With world = P . T(t) R S and a world translation delta d, the new local
        translation is ``t + inv(A_P) d`` where ``A_P`` is the 3x3 linear part of the
        parent chain (rotation, non-uniform scale and any resulting shear are all
        handled by the inverse); rotation and scale of the node are unchanged.
        Raises ``KeyError`` for unknown nodes and ``ValueError`` for a singular
        parent chain or when a moved node has a moved ancestor (not supported).
        """
        by_id = {n.id: n for n in scene.nodes}
        moved = {k for k, v in self.nodes.items() if v.dz != 0.0}
        ops: list[SetTransform] = []
        for nid in sorted(moved):
            if nid not in by_id:
                raise KeyError(f"settle: node {nid!r} not in scene")
            chain, p = [], by_id[nid].parent
            while p is not None:
                if p in moved:
                    raise ValueError(f"settle: {nid!r} and its ancestor {p!r} are both moved; unsupported")
                chain.append(by_id[p])
                p = by_id[p].parent
            a = np.eye(3)
            for anc in reversed(chain):  # root first
                a = a @ _linear(anc.transform)
            if abs(np.linalg.det(a)) < 1e-12 or np.linalg.cond(a) > 1e8:
                raise ValueError(f"settle: parent chain of {nid!r} is singular")
            d = np.linalg.solve(a, np.array([0.0, 0.0, self.nodes[nid].dz]))
            t = by_id[nid].transform
            ops.append(SetTransform(
                node_id=nid,
                transform=Transform(
                    translate=tuple(float(x) for x in np.array(t.translate) + d),
                    rotate_euler_deg=t.rotate_euler_deg,
                    scale=t.scale,
                ),
            ))
        return ops


def _linear(t: Transform) -> np.ndarray:
    """3x3 linear part R.S of a node transform (USD rotateXYZ: X applied first, R = Rz Ry Rx)."""
    rx, ry, rz = np.radians(t.rotate_euler_deg)
    cx, sx, cy, sy, cz, sz = np.cos(rx), np.sin(rx), np.cos(ry), np.sin(ry), np.cos(rz), np.sin(rz)
    r = (
        np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1.0]])
        @ np.array([[cy, 0, sy], [0, 1.0, 0], [-sy, 0, cy]])
        @ np.array([[1.0, 0, 0], [0, cx, -sx], [0, sx, cx]])
    )
    return r @ np.diag(t.scale)


def settle(
    meshes: list[MeshData],
    movable: set[str],
    *,
    ground_z: float = 0.0,
    max_iters: int = 40,
    tol: float = 1e-3,
) -> SettleResult:
    """Drop/lift each movable mesh vertically onto the ground and the meshes below it.

    Movables are processed bottom-up by (min Z, node_id); each becomes a supporter
    for the next. Static meshes never move. Deterministic for identical input.
    """
    t0 = time.perf_counter()
    ids = {m.node_id for m in meshes}
    if missing := movable - ids:
        raise KeyError(f"settle: unknown movable node ids {sorted(missing)}")
    supporters = [_Geo.from_mesh(m) for m in meshes if m.node_id not in movable]
    todo = sorted((m for m in meshes if m.node_id in movable), key=lambda m: (float(m.points[:, 2].min()), m.node_id))
    nodes: dict[str, NodeSettle] = {}
    total, all_ok = 0, True
    for m in todo:
        geo, dz, it = _Geo.from_mesh(m), 0.0, 0
        g = _min_gap(geo, supporters, ground_z)
        for it in range(1, max_iters + 1):
            if abs(g) <= _CONVERGED:
                break
            geo, dz = geo.shifted(-g), dz - g
            g = _min_gap(geo, supporters, ground_z)
        ok = abs(g) < tol
        all_ok &= ok
        total += it
        nodes[m.node_id] = NodeSettle(m.node_id, float(dz), float(g), float(max(0.0, -g)), it)
        supporters.append(geo)
    return SettleResult(nodes, total, (time.perf_counter() - t0) * 1e3, all_ok)


@dataclass(frozen=True)
class Contact:
    a: str  # upper mesh (or the mesh floating above ground)
    b: str  # supporter below, or GROUND_ID
    kind: str  # "floating" | "penetration"
    gap: float  # signed vertical gap in metres (negative = penetration depth)


def contact_report(
    meshes: list[MeshData],
    *,
    tol: float = 1e-3,
    max_gap: float = 0.05,
    ground_z: float | None = None,
) -> list[Contact]:
    """Floating gaps (tol < gap <= max_gap) and vertical penetrations (< -tol) between
    vertically stacked pairs (the upper mesh is the one with the higher min Z), and,
    if ``ground_z`` is given, against the ground plane. Sorted by (a, b)."""
    geos = [(m.node_id, _Geo.from_mesh(m)) for m in meshes]
    out: list[Contact] = []

    def add(a: str, b: str, gap: float) -> None:
        if not np.isfinite(gap):
            return
        if gap < -tol:
            out.append(Contact(a, b, "penetration", float(gap)))
        elif tol < gap <= max_gap:
            out.append(Contact(a, b, "floating", float(gap)))

    for i, (ida, ga) in enumerate(geos):
        if ground_z is not None:
            add(ida, GROUND_ID, float(ga.lo[2]) - ground_z)
        for idb, gb in geos[i + 1 :]:
            (ua, u), (lb, l) = ((ida, ga), (idb, gb)) if (ga.lo[2], ida) >= (gb.lo[2], idb) else ((idb, gb), (ida, ga))
            add(ua, lb, _pair_gap(u, l, (u.hi[2] - u.lo[2]) / 2))
    return sorted(out, key=lambda c: (c.a, c.b))


#: A part whose box comes this near another part's box is "beside" it, not hovering (the window ``tier1_qa`` sweeps
#: candidate pairs with, ``touch_eps``: what used to make two parts count as touching).
REACH = 0.005


def mesh_bounds(meshes: list[MeshData]) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    return {m.node_id: (np.asarray(m.points, float).min(axis=0), np.asarray(m.points, float).max(axis=0))
            for m in meshes if len(m.points)}


def beside_a_part(bounds: dict[str, tuple[np.ndarray, np.ndarray]], node_id: str, reach: float = REACH) -> bool:
    """Should the repair of a floating part leave it where it is? True when nothing is under it (no other part whose top
    is at or below its bottom over its footprint, where a vertical drop would seat it) but another part's box is within
    ``reach``: the part is beside a part it belongs to (a gap measured sideways or at an edge), and dropping it to the
    ground would carry it away from that part. False when something is under it (seat it there) and when nothing is near
    (a part hovering over empty ground: the ground is its support, as before). One decision, used by the settle pass
    (tactics/pyramid.py) and by the template author's regrounding (tactics/authoring.py)."""
    lo, hi = bounds[node_id]
    others = [(l, h) for o, (l, h) in bounds.items() if o != node_id]
    if any(h[2] <= lo[2] + 1e-6 and (np.minimum(h[:2], hi[:2]) - np.maximum(l[:2], lo[:2]) > 1e-6).all() for l, h in others):
        return False
    return any((np.maximum(l - hi, lo - h) <= reach).all() for l, h in others)


__all__ = ["Contact", "GROUND_ID", "NodeSettle", "SettleResult", "REACH", "beside_a_part", "contact_report", "mesh_bounds", "settle"]
