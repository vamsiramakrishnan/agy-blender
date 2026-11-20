"""Hierarchical spatial index (octree over prim AABBs) for bounded-context navigation.

Prims are placed by AABB centroid into an adaptive octree (each cell splits at the
midpoint of its centroids' extent). Every cell stores the *tight* union of its
prims' AABBs, so box queries prune exactly; cells at a given depth are the
drill-down summaries returned to the agent.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import numpy as np

from antigravity3d.kernel.types import MeshData

_EPS = 1e-9


def _fmt(v: np.ndarray | tuple[float, ...]) -> str:
    return "(" + ",".join(f"{float(x):.2f}".replace("-0.00", "0.00") for x in v) + ")"


def vertical_hits(tri: np.ndarray, xy: np.ndarray, eps: float = 1e-9) -> np.ndarray:
    """Heights of vertical rays through ``xy`` (K,2) against triangles ``tri`` (T,3,3).

    Returns (K,T) float with nan where the ray misses. Edge hits are inclusive
    (barycentric tolerance ``eps``) so rays on shared edges never fall through.
    """
    a, b, c = tri[:, 0], tri[:, 1], tri[:, 2]
    e1, e2 = b[:, :2] - a[:, :2], c[:, :2] - a[:, :2]
    det = e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]
    ok = np.abs(det) > 1e-14
    inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
    d = xy[:, None, :] - a[None, :, :2]
    u = (d[..., 0] * e2[:, 1] - d[..., 1] * e2[:, 0]) * inv
    v = (e1[:, 0] * d[..., 1] - e1[:, 1] * d[..., 0]) * inv
    hit = ok & (u >= -eps) & (v >= -eps) & (u + v <= 1 + eps)
    z = a[:, 2] + u * (b[:, 2] - a[:, 2]) + v * (c[:, 2] - a[:, 2])
    return np.where(hit, z, np.nan)


@dataclass(frozen=True)
class PrimInfo:
    node_id: str
    bounds_min: tuple[float, float, float]
    bounds_max: tuple[float, float, float]
    tris: int
    material_id: str | None
    tag: str | None


@dataclass(frozen=True)
class CellSummary:
    path: str  # octant path from the root, e.g. "0.3.5"; "" is the root
    depth: int
    bounds_min: tuple[float, float, float]
    bounds_max: tuple[float, float, float]
    n_prims: int
    tris: int
    top_tags: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class Neighbor:
    node_id: str
    distance_m: float


@dataclass
class RegionResult:
    kind: str  # "prims" | "cells"
    total_prims: int  # prims intersecting the region
    prims: list[PrimInfo] = field(default_factory=list)
    cells: list[CellSummary] = field(default_factory=list)

    def to_text(self, max_chars: int = 1200) -> str:
        """Terse deterministic rendering; ``len(result) <= max_chars`` always."""
        if max_chars <= 0:
            return ""
        if self.kind == "prims":
            head = f"{self.total_prims} prims in region"
            rows = (
                f"{p.node_id} {_fmt(p.bounds_min)}-{_fmt(p.bounds_max)} {p.tris}t"
                f"{' m=' + p.material_id if p.material_id else ''}{' [' + p.tag + ']' if p.tag else ''}"
                for p in self.prims
            )
            total = len(self.prims)
        else:
            head = f"{self.total_prims} prims in region, {len(self.cells)} cells (drill down with depth/bounds)"
            rows = (
                f"cell {c.path or 'root'} d{c.depth} {_fmt(c.bounds_min)}-{_fmt(c.bounds_max)} "
                f"{c.n_prims}p {c.tris}t" + "".join(f" {tag}x{count}" for tag, count in c.top_tags)
                for c in self.cells
            )
            total = len(self.cells)
        out = [head]
        used = len(head)
        for i, ln in enumerate(rows):
            tail = f"\n... +{total - i} more"
            if used + 1 + len(ln) + len(tail) > max_chars:
                out.append(tail[1:])
                break
            out.append(ln)
            used += 1 + len(ln)
        text = "\n".join(out)
        return text if len(text) <= max_chars else text[:max_chars - 1] + "~"


class _Node:
    __slots__ = ("lo", "hi", "start", "end", "children", "depth", "path", "tris")

    def __init__(self, lo, hi, start, end, depth, path, tris):
        self.lo, self.hi, self.start, self.end = lo, hi, start, end
        self.children: list[_Node] = []
        self.depth, self.path, self.tris = depth, path, tris


class SpatialIndex:
    def __init__(
        self,
        meshes: list[MeshData],
        semantic: dict[str, str] | None = None,
        *,
        max_depth: int = 8,
        leaf_capacity: int = 8,
    ) -> None:
        self.meshes = list(meshes)
        self.n = len(self.meshes)
        self.max_depth, self.leaf_capacity = max_depth, leaf_capacity
        self.semantic = dict(semantic or {})
        self.ids = [m.node_id for m in self.meshes]
        self._index = {nid: i for i, nid in enumerate(self.ids)}
        if len(self._index) != self.n:
            raise ValueError("duplicate node_id in meshes")
        self.lo = np.empty((self.n, 3))
        self.hi = np.empty((self.n, 3))
        self.tris = np.empty(self.n, dtype=np.int64)
        for i, m in enumerate(self.meshes):
            if len(m.points):
                self.lo[i], self.hi[i] = m.points.min(0), m.points.max(0)
            else:
                self.lo[i] = self.hi[i] = 0.0
            self.tris[i] = len(m.triangles)
        self.materials = [m.material_id for m in self.meshes]
        self._build_tree()

    @classmethod
    def from_bounds(cls, prims: list[PrimInfo]) -> SpatialIndex:
        """Build a navigation-only index without retaining any vertex or triangle buffers."""
        idx = cls.__new__(cls)
        idx.max_depth, idx.leaf_capacity = 8, 8
        idx.meshes = None
        idx.n = len(prims)
        idx.ids = [p.node_id for p in prims]
        idx.materials = [p.material_id for p in prims]
        idx.semantic = {p.node_id: p.tag for p in prims if p.tag}
        idx._index = {nid: i for i, nid in enumerate(idx.ids)}
        if len(idx._index) != idx.n:
            raise ValueError("duplicate node_id in bounds")
        idx.lo = np.array([p.bounds_min for p in prims], dtype=float).reshape(-1, 3)
        idx.hi = np.array([p.bounds_max for p in prims], dtype=float).reshape(-1, 3)
        idx.tris = np.array([p.tris for p in prims], dtype=np.int64)
        idx._build_tree()
        return idx

    def _build_tree(self) -> None:
        self.centroid = (self.lo + self.hi) / 2
        self.tags = [self.semantic.get(nid) for nid in self.ids]
        self.order = np.arange(self.n)
        self.root: _Node | None = None
        if self.n:
            self.root = self._build(0, self.n, 0, "")

    # -- build -------------------------------------------------------------------
    def _build(self, start: int, end: int, depth: int, path: str) -> _Node:
        idx = self.order[start:end]
        node = _Node(self.lo[idx].min(0), self.hi[idx].max(0), start, end, depth, path, int(self.tris[idx].sum()))
        if end - start <= self.leaf_capacity or depth >= self.max_depth:
            return node
        c = self.centroid[idx]
        cmin, cmax = c.min(0), c.max(0)
        if np.all(cmax - cmin < _EPS):
            return node
        code = ((c >= (cmin + cmax) / 2) * np.array([1, 2, 4])).sum(1)
        perm = np.argsort(code, kind="stable")
        self.order[start:end] = idx[perm]
        code = code[perm]
        bounds = np.searchsorted(code, np.arange(9))
        for o in range(8):
            s, e = start + bounds[o], start + bounds[o + 1]
            if e > s:
                node.children.append(self._build(s, e, depth + 1, f"{path}.{o}" if path else str(o)))
        return node

    # -- helpers -----------------------------------------------------------------
    def _info(self, i: int) -> PrimInfo:
        return PrimInfo(
            self.ids[i], tuple(map(float, self.lo[i])), tuple(map(float, self.hi[i])),
            int(self.tris[i]), self.materials[i], self.tags[i],
        )

    def _cell(self, node: _Node, idx: np.ndarray) -> CellSummary:
        tags = Counter(t for i in idx if (t := self.tags[i]) is not None)
        top = sorted(tags.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        return CellSummary(
            node.path, node.depth, tuple(map(float, node.lo)), tuple(map(float, node.hi)),
            len(idx), int(self.tris[idx].sum()), tuple(top),
        )

    @staticmethod
    def _overlaps(lo, hi, bmin, bmax) -> bool:
        return bool(np.all(lo <= bmax) and np.all(hi >= bmin))

    def _prims_in(self, bmin: np.ndarray, bmax: np.ndarray) -> np.ndarray:
        found: list[np.ndarray] = []
        stack = [self.root] if self.root else []
        while stack:
            nd = stack.pop()
            if not self._overlaps(nd.lo, nd.hi, bmin, bmax):
                continue
            if np.all(bmin <= nd.lo) and np.all(bmax >= nd.hi):
                found.append(self.order[nd.start:nd.end])
            elif nd.children:
                stack.extend(nd.children)
            else:
                idx = self.order[nd.start:nd.end]
                keep = np.all((self.lo[idx] <= bmax) & (self.hi[idx] >= bmin), axis=1)
                found.append(idx[keep])
        return np.sort(np.concatenate(found)) if found else np.empty(0, dtype=np.int64)

    def _cells_at(self, depth: int, bmin, bmax) -> list[CellSummary]:
        out: list[CellSummary] = []

        def walk(nd: _Node) -> None:
            if not self._overlaps(nd.lo, nd.hi, bmin, bmax):
                return
            if nd.depth >= depth or not nd.children:
                idx = self.order[nd.start:nd.end]
                keep = np.all((self.lo[idx] <= bmax) & (self.hi[idx] >= bmin), axis=1)
                if keep.any():
                    out.append(self._cell(nd, idx[keep]))
                return
            for ch in nd.children:
                walk(ch)

        if self.root:
            walk(self.root)
        return out

    # -- queries -----------------------------------------------------------------
    @property
    def world_bounds(self) -> tuple[np.ndarray, np.ndarray]:
        if not self.root:
            z = np.zeros(3)
            return z, z.copy()
        return self.root.lo, self.root.hi

    def query_region(
        self, bounds_min, bounds_max, depth: int | None = None, limit: int = 50
    ) -> RegionResult:
        """Prims intersecting the (closed) region, or octree-cell summaries.

        Cells are returned when ``depth`` is given or the hit count exceeds
        ``limit``; with no explicit depth the deepest level whose cell count still
        fits within ``limit`` is used.
        """
        bmin, bmax = np.asarray(bounds_min, float), np.asarray(bounds_max, float)
        if bmin.shape != (3,) or bmax.shape != (3,) or not np.all(bmin <= bmax):
            raise ValueError("region bounds must be ordered 3-vectors")
        if limit < 1 or (depth is not None and not 0 <= depth <= self.max_depth):
            raise ValueError("invalid region result limit or octree depth")
        idx = self._prims_in(bmin, bmax)
        if depth is None and len(idx) <= limit:
            return RegionResult("prims", len(idx), prims=[self._info(i) for i in sorted(idx, key=lambda i: self.ids[i])])
        if depth is None:
            depth = 0
            while depth < self.max_depth:
                if len(self._cells_at(depth + 1, bmin, bmax)) > limit:
                    break
                depth += 1
        cells = self._cells_at(depth, bmin, bmax)
        return RegionResult("cells", len(idx), cells=cells)

    def query_neighbors(self, node_id: str, radius_m: float, limit: int = 20) -> list[Neighbor]:
        """Prims whose AABB is within ``radius_m`` (box-to-box distance) of ``node_id``'s AABB."""
        if node_id not in self._index:
            raise KeyError(node_id)
        me = self._index[node_id]
        lo, hi = self.lo[me], self.hi[me]
        cands: list[np.ndarray] = []
        stack = [self.root] if self.root else []
        while stack:
            nd = stack.pop()
            if self._box_dist(lo, hi, nd.lo, nd.hi) > radius_m:
                continue
            if nd.children:
                stack.extend(nd.children)
            else:
                cands.append(self.order[nd.start:nd.end])
        if not cands:
            return []
        idx = np.concatenate(cands)
        idx = idx[idx != me]
        gap = np.maximum(0.0, np.maximum(self.lo[idx] - hi, lo - self.hi[idx]))
        dist = np.sqrt((gap**2).sum(1))
        keep = dist <= radius_m
        res = sorted(zip(dist[keep].tolist(), (self.ids[i] for i in idx[keep])))
        return [Neighbor(nid, d) for d, nid in res[:limit]]

    @staticmethod
    def _box_dist(lo1, hi1, lo2, hi2) -> float:
        gap = np.maximum(0.0, np.maximum(lo2 - hi1, lo1 - hi2))
        return float(np.sqrt((gap**2).sum()))

    def raycast_down(self, xy, z_start: float | None = None) -> float | None:
        """Height of the highest surface at/below ``z_start`` under ``xy`` (None if none).

        Only prims whose AABB contains the point in XY (and reaches below z_start)
        are tested, using a vectorised vertical ray/triangle kernel (edge-inclusive).
        """
        if self.meshes is None:
            raise ValueError("raycast requires triangle geometry")
        x, y = float(xy[0]), float(xy[1])
        zs = np.inf if z_start is None else float(z_start)
        bmin = np.array([x, y, -np.inf])
        bmax = np.array([x, y, zs])
        best: float | None = None
        for i in self._prims_in(bmin, bmax):
            m = self.meshes[i]
            if not len(m.triangles):
                continue
            z = vertical_hits(m.points[m.triangles], np.array([[x, y]]))[0]
            z = z[~np.isnan(z) & (z <= zs + _EPS)]
            if z.size and (best is None or z.max() > best):
                best = float(z.max())
        return best

    def overview(self, depth: int = 1, max_chars: int = 1200) -> str:
        """Top-level navigation text: world bounds, totals, per-cell summaries."""
        if not self.root:
            return "empty scene"
        lo, hi = self.world_bounds
        head = (
            f"scene {_fmt(lo)}-{_fmt(hi)} size {_fmt(hi - lo)} "
            f"{self.n} prims {int(self.tris.sum())}t depth<={self.max_depth}"
        )
        res = RegionResult("cells", self.n, cells=self._cells_at(depth, lo, hi))
        return (head + "\n" + res.to_text(max_chars=max(0, max_chars - len(head) - 1)))[:max_chars]
