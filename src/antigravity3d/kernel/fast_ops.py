"""Deterministic in-process mesh kernels: Tier-1 QA, UVs, LODs, colliders, export.

No LLM tokens, no Blender. Native backends actually used:
  * QA          : numpy (edge-table topology, winding-number interpenetration probe)
  * UVs         : exact developable charts for IR primitives (unfolded here, packed by a skyline
                  rectangle packer); xatlas charts + packing for imported meshes
  * LODs        : meshoptimizer (simplify / simplify_with_attributes in absolute-error mode, vertex
                  cache, overdraw, vertex fetch); levels from lod_diff's screen-space error budget,
                  each verified by an exact sampled surface distance and a silhouette check
  * colliders   : CoACD + scipy ConvexHull (fast path)
  * glTF        : trimesh GLB export
  * KTX2        : external ``ktx`` / ``toktx`` CLI only (never faked)

FALLBACKS (see ``FALLBACKS``): trimesh's quadric simplifier needs the optional
``fast_simplification`` package, which is not installed here, so when
``meshopt.simplify`` raises we fall back to ``meshopt.simplify_sloppy`` (same native
library) rather than trimesh. Nothing else deviates from the intended library.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Sequence

import numpy as np

from antigravity3d.kernel.types import MeshData
from antigravity3d.state import EngineBudget

log = logging.getLogger(__name__)

FALLBACKS = {
    "lod_simplify": "meshopt.simplify_sloppy on exception (trimesh needs fast_simplification, absent)",
    "lod_attr_binding": "meshoptimizer 0.2.30a0 leaves simplifyWithAttributes' ctypes argtypes unset (TypeError on call); we set them ourselves on the same native symbol",
    "uv_overflow": "when the requested density cannot fit one atlas, the same charts are re-packed with texels_per_unit=0 (auto-fit); charts are never recomputed",
}

Severity = Literal["critical", "warning"]


@dataclass(frozen=True)
class Issue:
    code: str
    severity: Severity
    node_id: str | None
    detail: str


# --------------------------------------------------------------------------- QA


def _canon(points: np.ndarray, q: float = 1e-6) -> np.ndarray:
    """Per-vertex canonical id after quantised position welding."""
    k = np.round(points / q).astype(np.int64)
    k -= k.min(axis=0)
    span = k.max(axis=0) + 1
    key = (k[:, 0] * span[1] + k[:, 1]) * span[2] + k[:, 2]
    return np.unique(key, return_inverse=True)[1].astype(np.int64)


@dataclass(frozen=True)
class _Topo:
    boundary: int
    nonmanifold: int
    inconsistent: int
    degenerate: int
    duplicates: int
    volume: float

    @property
    def closed(self) -> bool:
        return self.boundary == 0 and self.nonmanifold == 0 and self.inconsistent == 0


def _topology(points: np.ndarray, tris: np.ndarray) -> _Topo:
    cid = _canon(points)
    t = cid[tris]
    n = int(cid.max()) + 1
    p = points[tris]
    area2 = np.linalg.norm(np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]), axis=1)
    keep = (area2 > 1e-12) & (t[:, 0] != t[:, 1]) & (t[:, 1] != t[:, 2]) & (t[:, 0] != t[:, 2])
    degenerate = int((~keep).sum())
    t = t[keep]
    srt = np.sort(t, axis=1)
    _, fcount = np.unique((srt[:, 0] * n + srt[:, 1]) * n + srt[:, 2], return_counts=True)
    duplicates = int((fcount - 1).sum())
    a = np.concatenate([t[:, 0], t[:, 1], t[:, 2]])
    b = np.concatenate([t[:, 1], t[:, 2], t[:, 0]])
    dkey, dcount = np.unique(a * n + b, return_counts=True)
    ukey, ucount = np.unique(np.minimum(a, b) * n + np.maximum(a, b), return_counts=True)
    boundary = int((ucount == 1).sum())
    nonmanifold = int((ucount > 2).sum())
    inc = dkey[dcount >= 2]
    if len(inc):
        ik = np.minimum(inc // n, inc % n) * n + np.maximum(inc // n, inc % n)
        inconsistent = int((ucount[np.searchsorted(ukey, ik)] == 2).sum())
    else:
        inconsistent = 0
    vol = float(np.einsum("ij,ij->i", p[:, 0], np.cross(p[:, 1], p[:, 2])).sum() / 6.0)
    return _Topo(boundary, nonmanifold, inconsistent, degenerate, duplicates, vol)


def _single_component(points: np.ndarray, tris: np.ndarray) -> bool:
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    cid = _canon(points)
    t = cid[tris]
    n = int(cid.max()) + 1
    a = np.concatenate([t[:, 0], t[:, 1]])
    b = np.concatenate([t[:, 1], t[:, 2]])
    return connected_components(coo_matrix((np.ones(len(a)), (a, b)), shape=(n, n)), directed=False)[0] == 1


def _winding_inside(samples: np.ndarray, tri_pts: np.ndarray) -> np.ndarray:
    """Generalised winding number > 0.9 per sample point (Van Oosterom-Strackee)."""
    wn = np.zeros(len(samples))
    for s in range(0, len(tri_pts), 1024):
        t = tri_pts[s : s + 1024]
        a = t[None, :, 0] - samples[:, None]
        b = t[None, :, 1] - samples[:, None]
        c = t[None, :, 2] - samples[:, None]
        la, lb, lc = (np.linalg.norm(v, axis=2) for v in (a, b, c))
        num = np.einsum("pmk,pmk->pm", a, np.cross(b, c))
        den = (
            la * lb * lc
            + np.einsum("pmk,pmk->pm", a, b) * lc
            + np.einsum("pmk,pmk->pm", b, c) * la
            + np.einsum("pmk,pmk->pm", c, a) * lb
        )
        wn += (2.0 * np.arctan2(num, den)).sum(axis=1)
    return wn / (4 * np.pi) > 0.9


@dataclass(frozen=True)
class Tier1Report:
    ok: bool
    issues: list[Issue]
    stats: dict

    def one_line(self, limit: int = 200) -> str:
        def tok(i: Issue) -> str:
            if i.code == "tri_budget":
                return f"tris {self.stats['tris']}>{self.stats['max_tris']}"
            if i.code == "draw_call_budget":
                return f"draws {self.stats['draw_calls']}>{self.stats['max_draw_calls']}"
            return f"{i.code}@{i.node_id}" if i.node_id else i.code

        def uniq(xs: list[str]) -> list[str]:
            return list(dict.fromkeys(xs))

        crit = uniq([tok(i) for i in self.issues if i.severity == "critical"])
        warn = uniq([tok(i) for i in self.issues if i.severity == "warning"])
        s = "TIER1 FAIL: " + "; ".join(crit) if crit else "TIER1 OK"
        if warn:
            s += " | warn: " + "; ".join(warn)
        return s if len(s) <= limit else s[: limit - 3] + "..."


def _candidate_pairs(lo: np.ndarray, hi: np.ndarray, eps: float):
    """Sweep on the most dispersed axis, then reject separated boxes on the other two."""
    span = hi - lo
    axis = int(np.argmax(np.ptp(lo + span / 2, axis=0) / (span.mean(0) + 1e-12)))
    order = np.argsort(lo[:, axis], kind="stable")
    starts = lo[order, axis]
    ends = np.searchsorted(starts, hi[order, axis] + eps, side="right")
    for k, end in enumerate(ends):
        if end <= k + 1:
            continue
        i = order[k]
        possible = order[k + 1:end]
        keep = np.all((lo[possible] <= hi[i] + eps) & (hi[possible] >= lo[i] - eps), axis=1)
        for j in possible[keep]:
            yield (i, j) if i < j else (j, i)


def tier1_qa(
    meshes: list[MeshData], budget: EngineBudget, ground_tol: float = 0.01, touch_eps: float = 0.005
) -> Tier1Report:
    t0 = time.perf_counter()
    issues: list[Issue] = []

    def add(code: str, sev: Severity, node: str | None, detail: str) -> None:
        issues.append(Issue(code, sev, node, detail))

    manifold_sev: Severity = "critical" if budget.require_manifold else "warning"
    valid: list[tuple[MeshData, bool, np.ndarray, np.ndarray]] = []  # mesh, closed, lo, hi
    total_tris = total_verts = 0
    for m in meshes:
        pts = np.asarray(m.points, dtype=np.float64)
        tri = np.asarray(m.triangles)
        total_tris += len(tri)
        total_verts += len(pts)
        if len(pts) == 0 or len(tri) == 0:
            add("empty_mesh", "critical", m.node_id, "no vertices or triangles")
            continue
        if not np.isfinite(pts).all():
            add("non_finite", "critical", m.node_id, f"{int((~np.isfinite(pts)).any(axis=1).sum())} verts NaN/inf")
            continue
        if tri.min() < 0 or tri.max() >= len(pts):
            add("bad_indices", "critical", m.node_id, "triangle index out of range")
            continue
        topo = _topology(pts, tri)
        if topo.degenerate:
            add("degenerate_tris", "critical", m.node_id, f"{topo.degenerate} zero-area triangles")
        if topo.duplicates:
            add("duplicate_faces", "warning", m.node_id, f"{topo.duplicates} duplicate faces")
        if topo.nonmanifold:
            add("non_manifold", manifold_sev, m.node_id, f"{topo.nonmanifold} edges shared by >2 faces")
        if topo.boundary:
            add("not_watertight", manifold_sev, m.node_id, f"{topo.boundary} boundary edges")
        if topo.inconsistent:
            add("inconsistent_winding", "critical", m.node_id, f"{topo.inconsistent} edges with flipped winding")
        elif topo.closed and topo.volume < -1e-12:
            add("inverted_normals", "critical", m.node_id, f"signed volume {topo.volume:.4g} < 0")
        valid.append((m, topo.closed and topo.volume > 0, pts.min(axis=0), pts.max(axis=0)))

    draw_calls = len({m.material_id for m in meshes})
    if total_tris > budget.max_tris:
        add("tri_budget", "critical", None, f"{total_tris} tris > {budget.max_tris}")
    if draw_calls > budget.max_draw_calls:
        add("draw_call_budget", "critical", None, f"{draw_calls} draw calls > {budget.max_draw_calls}")

    bbox = None
    n_candidates = 0
    if valid:
        lo = np.min([v[2] for v in valid], axis=0)
        hi = np.max([v[3] for v in valid], axis=0)
        bbox = (lo.round(4).tolist(), hi.round(4).tolist())
        if abs(lo[2]) > ground_tol:
            kind = "floating" if lo[2] > 0 else "sunk below ground"
            add("ungrounded", "warning", None, f"min z {lo[2]:.4f} m ({kind}, tol {ground_tol})")
        touched = np.zeros(len(valid), dtype=bool)
        bounds_lo, bounds_hi = np.asarray([v[2] for v in valid]), np.asarray([v[3] for v in valid])
        for i, j in _candidate_pairs(bounds_lo, bounds_hi, touch_eps):
            touched[i] = touched[j] = True
            n_candidates += 1
            _check_pair(valid[i], valid[j], add)
        if len(valid) > 1:
            for i in np.flatnonzero(~touched & (bounds_lo[:, 2] > ground_tol)):
                add("floating_part", "warning", valid[i][0].node_id, f"AABB touches nothing; min z {bounds_lo[i, 2]:.3f}")

    ms = (time.perf_counter() - t0) * 1e3
    stats = {
        "tris": total_tris,
        "verts": total_verts,
        "draw_calls": draw_calls,
        "max_tris": budget.max_tris,
        "max_draw_calls": budget.max_draw_calls,
        "bbox": bbox,
        "candidate_pairs": n_candidates,
        "ms": round(ms, 2),
    }
    return Tier1Report(not any(i.severity == "critical" for i in issues), issues, stats)


def _check_pair(a, b, add) -> None:
    (ma, ca, la, ha), (mb, cb, lb, hb) = a, b
    if ((np.minimum(ha, hb) - np.maximum(la, lb)) <= 1e-4).any():
        return  # AABBs only touch or are apart
    hit = 0.0
    for src, dst, closed in ((ma, mb, cb), (mb, ma, ca)):
        if not closed:
            continue
        p = np.asarray(src.points, dtype=np.float64)
        t = np.asarray(src.triangles)
        pool = np.concatenate([p, p[t].mean(axis=1)])
        if len(pool) > 96:
            pool = pool[np.linspace(0, len(pool) - 1, 96).astype(int)]
        inside = _winding_inside(pool, np.asarray(dst.points, dtype=np.float64)[np.asarray(dst.triangles)])
        hit = max(hit, float(inside.mean()))
    if hit > 0:
        add("interpenetration", "warning", ma.node_id, f"{ma.node_id}/{mb.node_id}: ~{hit:.0%} of probe points inside")


# --------------------------------------------------------------------------- UVs

UV_PADDING_PX = 2           # gutter between charts, in atlas texels
# Computed (xatlas) charts may each flatten at most this much *smooth* Gaussian curvature (steradians). LSCM is
# conformal, so its area distortion grows with the curvature a chart flattens, and xatlas's default charts are
# unbounded: on UV spheres of 1.9k / 10k / 50k triangles at 1024 px/m they measure mean area distortion 0.088 /
# 0.064 / 0.068 (budget 0.05) in 0.30 / 0.83 / 8.2 s (chart growing + LSCM cost grows superlinearly with chart
# size). A sphere holds 4 pi sr; capping its charts at 1/32 of its area (4 pi / 32 sr each) gives 0.015 / 0.014 /
# 0.018 in 0.27 / 0.54 / 2.4 s at equal or higher achieved density. Curvature concentrated on sharp edges
# (box corners, cylinder rims: dihedral above CREASE) is not counted: those edges are chart seams anyway, and
# capping such meshes only fragments flat charts (measured: a 32-sided cylinder split into one chart per side
# quad, every rim vertex in three charts, which then locked the LOD simplifier).
UV_CHART_CURVATURE_SR = 4.0 * np.pi / 32
UV_SMOOTH_CREASE_DEG = 40.0
UV_MIN_RECT_FILL = 0.6      # exact charts filling less of their bounding rectangle are cut (see _charted)


@dataclass(frozen=True)
class UVResult:
    node_id: str
    points: np.ndarray          # (V', 3) float64, seam-split
    triangles: np.ndarray       # (M, 3) int32
    uvs: np.ndarray             # (V', 2) float32 in [0, 1]
    vmapping: np.ndarray        # (V',) index into the original vertices
    texel_density: float        # achieved px/m
    stretch_mean: float         # area-weighted mean |ratio-1|, ratio = uv-area-share / 3d-area-share
    stretch_max: float
    resolution: int
    atlas_id: str | None = None
    fitted: bool = True         # False: requested density overflowed, re-fit with lower density
    chart_source: str = "computed"  # "analytic": exact developable charts from the IR; "computed": xatlas charts
    angle_distortion: float = 0.0  # area-weighted mean (sigma_max / sigma_min - 1) of the UV map (0 = conformal)

    def within(self, budget: EngineBudget) -> bool:
        return self.stretch_mean <= budget.max_uv_stretch


def _tri_areas(p: np.ndarray, t: np.ndarray, dim: int) -> np.ndarray:
    a, b, c = p[t[:, 0]], p[t[:, 1]], p[t[:, 2]]
    if dim == 2:
        return 0.5 * np.abs((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0]))
    return 0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1)


def _stretch(points, tris, uvs) -> tuple[float, float]:
    a3, a2 = _tri_areas(points, tris, 3), _tri_areas(uvs.astype(np.float64), tris, 2)
    ok = a3 > 1e-14
    if not ok.any() or a2[ok].sum() <= 0:
        return 0.0, 0.0
    w = a3[ok] / a3[ok].sum()
    dev = np.abs(a2[ok] / a3[ok] / (a2[ok].sum() / a3[ok].sum()) - 1.0)
    return float((dev * w).sum()), float(dev.max())


def _angle_distortion(points, tris, uvs) -> float:
    """Area-weighted mean of (sigma1 / sigma2 - 1) of each triangle's 3D -> UV Jacobian (0 = no shear)."""
    p = np.asarray(points, np.float64)[tris]
    q = np.asarray(uvs, np.float64)[tris]
    e1, e2 = p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]
    l1 = np.linalg.norm(e1, axis=1)
    a3 = 0.5 * np.linalg.norm(np.cross(e1, e2), axis=1)
    ok = (a3 > 1e-14) & (l1 > 0)
    if not ok.any():
        return 0.0
    x = e1[ok] / l1[ok, None]
    y = np.cross(np.cross(e1[ok], e2[ok]), x)
    y /= np.linalg.norm(y, axis=1, keepdims=True)
    P = np.stack([np.stack([l1[ok], np.zeros(ok.sum())], 1), np.stack([(e2[ok] * x).sum(1), (e2[ok] * y).sum(1)], 1)], 1)  # rows: edges in 2D
    Q = np.stack([q[ok, 1] - q[ok, 0], q[ok, 2] - q[ok, 0]], 1)
    J = np.linalg.solve(P, Q)  # (2,2): local 3D frame -> uv
    s = np.linalg.svd(J, compute_uv=False)
    r = np.where(s[:, 1] > 0, s[:, 0] / np.maximum(s[:, 1], 1e-30) - 1.0, 0.0)
    w = a3[ok] / a3[ok].sum()
    return float((r * w).sum())


def _unfold_charts(points: np.ndarray, tris: np.ndarray, charts: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Isometric unfolding of known developable charts. Returns (vmapping, triangles, uv in metres, chart per
    triangle); a chart that is not edge-connected gets a new id per component.

    Each chart is laid out triangle by triangle along a breadth-first spanning tree of its edge adjacency,
    every triangle keeping its exact 3D edge lengths (so distortion is zero for a developable chart). A
    vertex reached along two paths that disagree (the cut of a closed ring, e.g. a cylinder's side) is split,
    which places the seam deterministically. Triangle order is preserved; UV winding is counter-clockwise.
    """
    p = np.asarray(points, np.float64)
    t = np.asarray(tris, np.int64)
    m = len(t)
    corner = np.zeros((m, 3, 2))
    chart_of = np.asarray(charts, np.int64).copy()
    # edge -> faces within a chart (undirected, keyed by chart and sorted vertex pair)
    e = np.concatenate([t[:, [0, 1]], t[:, [1, 2]], t[:, [2, 0]]])
    f = np.tile(np.arange(m), 3)
    slot = np.repeat(np.arange(3), m)
    key = np.stack([chart_of[f], e.min(1), e.max(1)], 1)
    order = np.lexsort((key[:, 2], key[:, 1], key[:, 0]))
    ks = key[order]
    same = np.flatnonzero((ks[1:] == ks[:-1]).all(1))
    nbr: list[list[tuple[int, int]]] = [[] for _ in range(m)]  # (neighbour face, local edge slot of this face)
    for i in same:
        a, b = order[i], order[i + 1]
        nbr[f[a]].append((int(f[b]), int(slot[a])))
        nbr[f[b]].append((int(f[a]), int(slot[b])))
    placed = [False] * m
    next_chart = int(chart_of.max()) + 1 if m else 0
    seen_root: set[int] = set()
    P = p.tolist()
    T = t.tolist()
    C = corner.tolist()  # filled per face below, written back at the end
    import math

    def dist(i: int, j: int) -> float:
        a, b = P[i], P[j]
        return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)

    for root in range(m):
        if placed[root]:
            continue
        if int(chart_of[root]) in seen_root:  # a second edge-connected component of one chart: its own chart
            comp_chart = next_chart
            next_chart += 1
        else:
            comp_chart = int(chart_of[root])
            seen_root.add(comp_chart)
        a, b, c = (p[v] for v in T[root])
        ab = float(np.linalg.norm(b - a))
        x = float(np.dot(c - a, b - a)) / max(ab, 1e-30)
        y = float(np.linalg.norm(np.cross(b - a, c - a))) / max(ab, 1e-30)
        C[root] = [[0.0, 0.0], [ab, 0.0], [x, y]]
        placed[root] = True
        chart_of[root] = comp_chart
        queue = [root]
        qi = 0
        while qi < len(queue):
            fi = queue[qi]
            qi += 1
            tf = T[fi]
            for gi, sl in nbr[fi]:
                if placed[gi]:
                    continue
                va, vb = tf[sl], tf[(sl + 1) % 3]
                tg = T[gi]
                ga, gb = tg.index(va), tg.index(vb)
                gc = 3 - ga - gb
                A, B = C[fi][sl], C[fi][(sl + 1) % 3]
                dx, dy = B[0] - A[0], B[1] - A[1]
                dab = max(math.hypot(dx, dy), 1e-30)
                la, lb = dist(tg[gc], va), dist(tg[gc], vb)
                x = (la * la - lb * lb + dab * dab) / (2 * dab)
                y = math.sqrt(max(la * la - x * x, 0.0))
                ex, ey = dx / dab, dy / dab
                tri = [None, None, None]
                tri[ga], tri[gb] = A, B
                tri[gc] = [A[0] + x * ex - y * ey, A[1] + x * ey + y * ex]
                u0, u1, u2 = tri
                if (u1[0] - u0[0]) * (u2[1] - u0[1]) - (u1[1] - u0[1]) * (u2[0] - u0[0]) < 0:
                    tri[gc] = [A[0] + x * ex + y * ey, A[1] + x * ey - y * ex]
                C[gi] = tri
                placed[gi] = True
                chart_of[gi] = comp_chart
                queue.append(gi)
    corner = np.asarray(C, np.float64).reshape(m, 3, 2)
    # weld corners: same source vertex, same chart and (within tolerance) the same uv
    span = float(np.ptp(p, axis=0).max()) if len(p) else 1.0
    tol = 1e-7 * max(span, 1e-9)
    cv = t.reshape(-1)
    cc = np.repeat(chart_of, 3)
    cu = corner.reshape(-1, 2)
    grp = np.lexsort((cc, cv))
    ids = np.empty(len(cv), np.int64)
    out_v: list[int] = []
    out_uv: list[np.ndarray] = []
    start = 0
    gkey = np.stack([cv[grp], cc[grp]], 1)
    cuts = np.flatnonzero((gkey[1:] != gkey[:-1]).any(1)) + 1
    for g in np.split(grp, cuts):
        reps: list[int] = []
        for k in g:
            for r in reps:
                if np.abs(out_uv[r] - cu[k]).max() <= tol:
                    ids[k] = r
                    break
            else:
                ids[k] = len(out_v)
                reps.append(len(out_v))
                out_v.append(int(cv[k]))
                out_uv.append(cu[k])
        start += len(g)
    return (np.asarray(out_v, np.int64), ids.reshape(-1, 3).astype(np.int32), np.asarray(out_uv, np.float64).reshape(-1, 2),
            chart_of)


@dataclass
class _Charted:
    """A mesh with its chart layout: seam-split vertices (``vmap`` into ``mesh.points``), triangles, UVs in metres."""
    mesh: MeshData
    vmap: np.ndarray
    tris: np.ndarray
    uv_m: np.ndarray
    kind: str                     # "analytic" | "computed"
    chart: np.ndarray | None = None  # (M,) chart id per triangle (analytic only)


def _min_area_frame(p2: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Rotation R (rows = new axes), min corner and (w, h) of the minimum-area bounding rectangle of 2D points."""
    from scipy.spatial import ConvexHull, QhullError

    try:
        hull = p2[ConvexHull(p2).vertices]
        d = np.roll(hull, -1, axis=0) - hull
        ang = np.unique(np.round(np.arctan2(d[:, 1], d[:, 0]) % (np.pi / 2), 12))
    except (QhullError, ValueError):
        hull, ang = p2, np.array([0.0])
    c, s = np.cos(ang), np.sin(ang)
    R = np.stack([np.stack([c, s], -1), np.stack([-s, c], -1)], axis=1)
    q = np.einsum("aij,nj->ani", R, hull)
    lo, hi = q.min(axis=1), q.max(axis=1)
    k = int(np.argmin(np.prod(hi - lo, axis=1)))
    return R[k], lo[k], hi[k] - lo[k]


def _charted(m: MeshData, max_extent_m: float | None = None) -> _Charted | None:
    """Exact layout of a mesh with known charts, shaped for rectangle packing.

    A chart is cut across its long axis, along existing edges, into equal slabs when it is longer than
    ``max_extent_m`` (it could not be placed at the requested density: a long cylinder side becomes two
    rectangles), or when it fills less than UV_MIN_RECT_FILL of its bounding rectangle (a wide annular
    sector of a sphere band becomes two flatter ones). Cuts never change distortion (each piece is still an
    isometric unfolding); they cost seam vertices and buy texel density."""
    if m.uv_charts is None or len(m.uv_charts) != len(m.triangles) or len(m.triangles) == 0:
        return None
    charts = np.asarray(m.uv_charts, np.int64).copy()
    tris = np.asarray(m.triangles, np.int64)
    for attempt in range(5):
        vmap, t2, uv, charts = _unfold_charts(m.points, tris, charts)
        if max_extent_m is None or attempt == 4:
            break
        # per-chart extent in its min-area frame; cut along the long axis into equal slabs of triangle centroids
        cent = uv[t2].mean(axis=1)
        nxt = charts.copy()
        top = int(charts.max()) + 1
        changed = False
        for c in np.unique(charts):
            sel = np.flatnonzero(charts == c)
            R, lo, wh = _min_area_frame(uv[np.unique(t2[sel])])
            ax = int(np.argmax(wh))
            fill = float(_tri_areas(uv, t2[sel], 2).sum() / max(wh[0] * wh[1], 1e-30))
            if wh[ax] <= max_extent_m and fill >= UV_MIN_RECT_FILL:
                continue
            k = max(2, int(np.ceil(wh[ax] / max_extent_m)))
            coord = (cent[sel] @ R.T)[:, ax] - lo[ax]
            slab = np.minimum((coord / wh[ax] * k).astype(np.int64), k - 1)
            along = (uv[t2[sel]] @ R.T)[..., ax]  # (n, 3) corner coordinates on the long axis
            span = max(float(np.ptp(along[slab == j])) for j in np.unique(slab))
            if len(np.unique(slab)) < 2 or span > max_extent_m:
                continue  # triangles span the length (a box face, a tall pole): no cut helps, the density will fit
            nxt[sel] = np.where(slab == 0, c, top + slab - 1)
            top += k - 1
            changed = True
        if not changed:
            break
        charts = nxt
    return _Charted(m, vmap, t2.astype(np.int32), uv, "analytic", charts)


def _skyline_pack(sizes: np.ndarray, width: float, height: float) -> np.ndarray | None:
    """Bottom-left skyline packing of axis-aligned rectangles (w, h), each optionally turned 90 degrees.

    Returns (n, 3) [x, y, turned] or None when they do not all fit. Deterministic: rectangles go in order of
    decreasing longer side, then area, then index; ties pick the lowest top edge, then the leftmost x.
    """
    order = sorted(range(len(sizes)), key=lambda i: (-max(sizes[i]), -sizes[i][0] * sizes[i][1], i))
    sky = [(0.0, 0.0, float(width))]  # (x, y, w) segments covering [0, width)
    out = np.zeros((len(sizes), 3))
    for i in order:
        best = None
        for turned in (0, 1):
            w, h = (sizes[i][1], sizes[i][0]) if turned else (sizes[i][0], sizes[i][1])
            if w > width or h > height:
                continue
            for j, (x, _, _) in enumerate(sky):
                if x + w > width + 1e-9:
                    break
                y, k, right = 0.0, j, x + w
                while k < len(sky) and sky[k][0] < right - 1e-9:
                    y = max(y, sky[k][1])
                    k += 1
                if y + h <= height + 1e-9 and (best is None or (y + h, x) < (best[0], best[1])):
                    best = (y + h, x, y, w, h, turned)
        if best is None:
            return None
        _, x, y, w, h, turned = best
        out[i] = (x, y, turned)
        new, right = [], x + w
        for sx, sy, sw in sky:
            if sx + sw <= x + 1e-9 or sx >= right - 1e-9:
                new.append((sx, sy, sw))
                continue
            if sx < x:
                new.append((sx, sy, x - sx))
            if sx + sw > right:
                new.append((right, sy, sx + sw - right))
        new.append((x, y + h, w))
        new = sorted(seg for seg in new if seg[2] > 1e-9)  # zero-width slivers would break the x order
        merged = [new[0]]
        for seg in new[1:]:
            px, py, pw = merged[-1]
            if abs(py - seg[1]) < 1e-9 and abs(px + pw - seg[0]) < 1e-9:
                merged[-1] = (px, py, pw + seg[2])
            else:
                merged.append(seg)
        sky = merged
    return out


def _pack_analytic(charted: list[_Charted], density: float, resolution: int) -> tuple[list[np.ndarray], float]:
    """Packs exact charts as their minimum-area rectangles into one ``resolution`` square atlas.

    Each chart keeps UV_PADDING_PX texels of private gutter on every side. The requested density is used
    when everything fits; otherwise the largest density that fits is found by bisection (the packer is
    cheap, so this costs milliseconds, unlike re-running a raster packer). Returns per-mesh UVs in [0, 1]
    and the density used.
    """
    frames = []  # (mesh index, vertex ids, rotation, min corner, (w, h)) per chart
    for mi, c in enumerate(charted):
        cid = np.zeros(len(c.uv_m), np.int64)
        cid[c.tris.reshape(-1)] = np.repeat(c.chart, 3)
        for k in np.unique(c.chart):
            v = np.flatnonzero(cid == k)
            R, lo, wh = _min_area_frame(c.uv_m[v])
            frames.append((mi, v, R, lo, wh))
    ext = np.array([f[4] for f in frames], np.float64).reshape(-1, 2)
    pad = 2.0 * UV_PADDING_PX
    edge = 1.0  # keep half a texel clear of the atlas border, plus rounding

    def fits(d: float) -> np.ndarray | None:
        return _skyline_pack(ext * d + pad, resolution - edge, resolution - edge)

    d = float(density)
    place = fits(d)
    if place is None:
        area = float(ext.prod(axis=1).sum())
        hi = min(d, (resolution - edge) / max(float(ext.max()), 1e-12), (resolution - edge) / np.sqrt(max(area, 1e-12)))
        lo = hi / 8
        while fits(lo) is None and lo > 1e-6:
            lo /= 2
        place = fits(lo)
        for _ in range(24):
            mid = 0.5 * (lo + hi)
            p = fits(mid)
            if p is None:
                hi = mid
            else:
                lo, place = mid, p
            if hi - lo <= 1e-4 * hi:
                break
        d = lo
    out = [np.zeros((len(c.uv_m), 2)) for c in charted]
    for (mi, v, R, lo_, wh), (x, y, turned) in zip(frames, place):
        q = (charted[mi].uv_m[v] @ R.T - lo_) * d  # chart in px, min corner at the origin
        if turned:  # 90 degrees counter-clockwise keeps the winding: (u, v) -> (h - v, u)
            q = np.stack([wh[1] * d - q[:, 1], q[:, 0]], axis=1)
        out[mi][v] = (q + [x + pad / 2 + edge / 2, y + pad / 2 + edge / 2]) / resolution
    return out, d


def _smooth_curvature(meshes: Sequence[MeshData]) -> float:
    """Total |angle defect| (discrete Gaussian curvature, sr) at closed vertices whose incident edges all bend
    less than UV_SMOOTH_CREASE_DEG; vertices on sharp creases or open borders are left out."""
    total = 0.0
    for m in meshes:
        p = np.asarray(m.points, np.float64)
        cid = _canon(p)
        t = cid[np.asarray(m.triangles, np.int64)]
        q = p[np.unique(cid, return_index=True)[1]]
        e1, e2, e3 = q[t[:, 1]] - q[t[:, 0]], q[t[:, 2]] - q[t[:, 1]], q[t[:, 0]] - q[t[:, 2]]
        fn = np.cross(e1, -e3)
        ok = np.linalg.norm(fn, axis=1) > 1e-20
        if not ok.any():
            continue
        t, e1, e2, e3, fn = t[ok], e1[ok], e2[ok], e3[ok], fn[ok]
        fn /= np.linalg.norm(fn, axis=1, keepdims=True)

        def ang(u: np.ndarray, v: np.ndarray) -> np.ndarray:
            return np.arctan2(np.linalg.norm(np.cross(u, v), axis=1), (u * v).sum(1))

        n = len(q)
        defect = np.full(n, 2 * np.pi)
        np.subtract.at(defect, t[:, 0], ang(e1, -e3))
        np.subtract.at(defect, t[:, 1], ang(e2, -e1))
        np.subtract.at(defect, t[:, 2], ang(e3, -e2))
        a = np.concatenate([t[:, 0], t[:, 1], t[:, 2]])
        b = np.concatenate([t[:, 1], t[:, 2], t[:, 0]])
        f = np.tile(np.arange(len(t)), 3)
        key = np.minimum(a, b) * n + np.maximum(a, b)
        order = np.argsort(key, kind="stable")
        ks = key[order]
        start = np.flatnonzero(np.r_[True, ks[1:] != ks[:-1]])
        count = np.diff(np.r_[start, len(ks)])
        sharp = np.zeros(n, bool)
        two = start[count == 2]
        bend = np.degrees(np.arccos(np.clip((fn[f[order[two]]] * fn[f[order[two + 1]]]).sum(1), -1, 1)))
        hard = np.r_[start[count != 2], two[bend >= UV_SMOOTH_CREASE_DEG]]  # open / non-manifold / creased
        sharp[a[order[hard]]] = sharp[b[order[hard]]] = True
        total += float(np.abs(defect[~sharp & np.isin(np.arange(n), t)]).sum())
    return total


def _xatlas_options(resolution: int, tpu: float, *, input_uvs: bool, surface_area: float = 0.0,
                    smooth_curvature: float = 0.0):
    import xatlas

    co = xatlas.ChartOptions()
    co.use_input_mesh_uvs = input_uvs
    if not input_uvs and surface_area > 0 and smooth_curvature > UV_CHART_CURVATURE_SR:
        co.max_chart_area = float(surface_area * UV_CHART_CURVATURE_SR / smooth_curvature)
    po = xatlas.PackOptions()
    po.resolution = resolution
    po.texels_per_unit = float(tpu)
    po.padding = UV_PADDING_PX
    po.bruteForce = False
    po.rotate_charts = True
    return co, po


def _xatlas_fit(add, density: float, resolution: int, *, input_uvs: bool, surface_area: float = 0.0,
                smooth_curvature: float = 0.0):
    """xatlas generate into a *single* atlas no larger than ``resolution`` at the highest density reached.

    xatlas's own fit mode (texels_per_unit=0) estimates a density but then grows the atlas past
    ``resolution`` (measured: 3289 x 2808 for a 2048 request), so the earlier pipeline baked UVs normalised
    to a non-square, oversized atlas. Here the first pass (resolution 0 = one atlas of any size) also
    computes the charts when ``input_uvs`` is False; if it is larger than ``resolution`` only the *packing*
    is repeated, from the charts of the first pass, at the density scaled to fit.
    Returns (atlas, density used, uv scale factor to the resolution square).
    """
    import xatlas

    atlas = xatlas.Atlas()
    add(atlas, None)
    co, po = _xatlas_options(0, density, input_uvs=input_uvs, surface_area=surface_area, smooth_curvature=smooth_curvature)
    atlas.generate(co, po)
    tpu = float(density)
    for _ in range(6):
        w, h = atlas.width, atlas.height
        if max(w, h) <= resolution:
            return atlas, tpu
        first = atlas
        scale = np.array([w, h], np.float64) / tpu  # metres per normalised uv unit
        tpu *= 0.98 * resolution / max(w, h)
        atlas = xatlas.Atlas()
        add(atlas, [first.get_mesh(i) for i in range(first.mesh_count)], scale)
        co, po = _xatlas_options(0, tpu, input_uvs=True)
        atlas.generate(co, po)
    return atlas, tpu


def _atlas_uv(atlas, uv: np.ndarray, resolution: int) -> np.ndarray:
    """xatlas normalises UVs by the atlas' *cropped* width and height, which differ from each other and from
    ``resolution``; map to the ``resolution`` x ``resolution`` texture the atlas is baked into, so texels stay
    square and the reported density is the real one."""
    return np.asarray(uv, np.float64) * (np.array([atlas.width, atlas.height], np.float64) / float(resolution))


def _result(m: MeshData, vm: np.ndarray, tris: np.ndarray, uv: np.ndarray, fitted: bool, resolution: int,
            atlas_id: str | None, kind: str) -> UVResult:
    pts = np.asarray(m.points, dtype=np.float64)[vm]
    uv = np.clip(uv.astype(np.float32), 0.0, 1.0)
    a3, a2 = _tri_areas(pts, tris, 3).sum(), _tri_areas(uv, tris, 2).sum()
    dens = float(np.sqrt(a2 / a3) * resolution) if a3 > 0 and a2 > 0 else 0.0
    sm, sx = _stretch(pts, tris, uv)
    return UVResult(m.node_id, pts, tris, uv, vm, dens, sm, sx, resolution, atlas_id, fitted, kind,
                    _angle_distortion(pts, tris, uv))


def _area(meshes: Sequence[MeshData]) -> float:
    return float(sum(_tri_areas(np.asarray(m.points, np.float64), np.asarray(m.triangles), 3).sum() for m in meshes))


def _unwrap_group(group: list[MeshData], density: float, resolution: int, atlas_id: str | None) -> list[UVResult]:
    # an analytic chart longer than the atlas at the requested density is cut where the mesh allows it
    max_extent = (resolution - 1.0 - 2.0 * UV_PADDING_PX) / density
    known = [_charted(m, max_extent) for m in group]
    if all(c is not None for c in known):
        uvs, d = _pack_analytic(known, density, resolution)  # type: ignore[arg-type]
        fitted = d >= density * (1 - 1e-9)
        return [_result(c.mesh, c.vmap, c.tris, uv, fitted, resolution, atlas_id, "analytic")  # type: ignore[union-attr]
                for c, uv in zip(known, uvs)]

    # at least one mesh needs computed charts: xatlas computes those once and packs everything
    bases: list[np.ndarray] = []  # per mesh: original vertex id of each vertex fed to the current atlas

    def add(atlas, prev=None, scale=None):
        nonlocal bases
        fed = []
        for i, (m, c) in enumerate(zip(group, known)):
            if prev is not None:  # pack-only pass: the previous pass' layout as input UVs (charts kept)
                vm, idx, uv = prev[i]
                base = bases[i][vm.astype(np.int64)]
                atlas.add_mesh(np.ascontiguousarray(np.asarray(m.points)[base], dtype=np.float32),
                               np.ascontiguousarray(idx, dtype=np.uint32), uvs=np.ascontiguousarray(uv * scale, dtype=np.float32))
            elif c is not None:
                base = c.vmap
                atlas.add_mesh(np.ascontiguousarray(np.asarray(m.points)[base], dtype=np.float32),
                               np.ascontiguousarray(c.tris, dtype=np.uint32), uvs=np.ascontiguousarray(c.uv_m, dtype=np.float32))
            else:
                base = np.arange(len(m.points))
                atlas.add_mesh(np.ascontiguousarray(m.points, dtype=np.float32), np.ascontiguousarray(m.triangles, dtype=np.uint32))
            fed.append(base)
        bases = fed

    if any(c is not None for c in known):  # mixed group: compute the missing charts first, then pack all as input UVs
        todo = [m for m, c in zip(group, known) if c is None]
        import xatlas

        ca = xatlas.Atlas()
        for m in todo:
            ca.add_mesh(np.ascontiguousarray(m.points, dtype=np.float32), np.ascontiguousarray(m.triangles, dtype=np.uint32))
        co, po = _xatlas_options(0, density, input_uvs=False, surface_area=_area(todo), smooth_curvature=_smooth_curvature(todo))
        ca.generate(co, po)
        sc = np.array([ca.width, ca.height], np.float64) / float(ca.texels_per_unit or density)
        it = iter(range(len(todo)))
        filled = []
        for m, c in zip(group, known):
            if c is None:
                vm, idx, uv = ca.get_mesh(next(it))
                c = _Charted(m, vm.astype(np.int64), idx.reshape(-1, 3).astype(np.int32), uv.astype(np.float64) * sc, "computed")
            filled.append(c)
        known = filled
    computed = [m for m, c in zip(group, known) if c is None]
    atlas, tpu = _xatlas_fit(add, density, resolution, input_uvs=not computed, surface_area=_area(computed),
                             smooth_curvature=_smooth_curvature(computed))
    fitted = tpu >= density * (1 - 1e-9)
    out = []
    for i, (m, c) in enumerate(zip(group, known)):
        vm, idx, uv = atlas.get_mesh(i)
        vm = bases[i][vm.astype(np.int64)]
        out.append(_result(m, vm, idx.reshape(-1, 3).astype(np.int32), _atlas_uv(atlas, uv, resolution), fitted, resolution,
                           atlas_id, c.kind if c is not None else "computed"))
    return out


def unwrap_uvs(mesh: MeshData, texel_density_px_per_m: float, resolution: int = 2048) -> UVResult:
    return _unwrap_group([mesh], texel_density_px_per_m, resolution, mesh.material_id)[0]


def unwrap_scene(meshes: list[MeshData], budget: EngineBudget, resolution: int = 2048) -> list[UVResult]:
    """One atlas per material id; results are returned in input order.

    Meshes that carry exact charts (``MeshData.uv_charts``, set for IR primitives) are unfolded
    isometrically and only packed; the others get xatlas charts, computed once.
    """
    groups: dict[str | None, list[int]] = {}
    for i, m in enumerate(meshes):
        groups.setdefault(m.material_id, []).append(i)
    res: list[UVResult | None] = [None] * len(meshes)
    for mat, ids in groups.items():
        for i, r in zip(ids, _unwrap_group([meshes[i] for i in ids], budget.texel_density_px_per_m, resolution, mat)):
            res[i] = r
    return res  # type: ignore[return-value]


def uv_overlap_fraction(results: Sequence[UVResult], res: int = 512) -> float:
    """Fraction of covered texels hit by more than one triangle (conservative raster)."""
    cov = np.zeros((res, res), np.int32)
    for r in results:
        for tri in r.triangles:
            p = r.uvs[tri].astype(np.float64) * res
            x0, y0 = np.floor(p.min(axis=0)).astype(int)
            x1, y1 = np.ceil(p.max(axis=0)).astype(int)
            xs, ys = np.meshgrid(np.arange(max(x0, 0), min(x1, res - 1) + 1), np.arange(max(y0, 0), min(y1, res - 1) + 1))
            c = np.stack([xs.ravel() + 0.5, ys.ravel() + 0.5], axis=1)
            d = (p[1, 1] - p[2, 1]) * (p[0, 0] - p[2, 0]) + (p[2, 0] - p[1, 0]) * (p[0, 1] - p[2, 1])
            if abs(d) < 1e-12:
                continue
            l1 = ((p[1, 1] - p[2, 1]) * (c[:, 0] - p[2, 0]) + (p[2, 0] - p[1, 0]) * (c[:, 1] - p[2, 1])) / d
            l2 = ((p[2, 1] - p[0, 1]) * (c[:, 0] - p[2, 0]) + (p[0, 0] - p[2, 0]) * (c[:, 1] - p[2, 1])) / d
            m = (l1 > 0) & (l2 > 0) & (l1 + l2 < 1)  # strict interior: shared edges do not count
            cov[ys.ravel()[m], xs.ravel()[m]] += 1
    covered = (cov > 0).sum()
    return float((cov > 1).sum() / covered) if covered else 0.0


# --------------------------------------------------------------------------- LODs


@dataclass(frozen=True)
class LOD:
    ratio: float                 # triangles of this level / triangles of LOD0 (a result, not a target)
    points: np.ndarray           # (V, 3) float64 (float32 precision)
    triangles: np.ndarray        # (M, 3) int32
    uvs: np.ndarray | None
    error: float                 # measured deviation from LOD0 relative to the mesh scale (meshopt simplify_scale)
    error_m: float               # same, in metres (two-sided, sampled surface distance; see _surface_deviation)
    screen_fraction: float = 1.0  # largest screen fraction (bounding-sphere diameter / frame height) it is shown at

    @property
    def tri_count(self) -> int:
        return len(self.triangles)


def _optimise(idx: np.ndarray, verts: np.ndarray, nvert: int) -> tuple[np.ndarray, np.ndarray]:
    """Vertex cache -> overdraw -> vertex fetch. verts: (V, k) float32 with xyz first."""
    import meshoptimizer as mo

    pos = np.ascontiguousarray(verts[:, :3])
    cache = np.zeros_like(idx)
    mo.optimize_vertex_cache(cache, idx, vertex_count=nvert)
    over = np.zeros_like(idx)
    mo.optimize_overdraw(over, cache, pos, vertex_count=nvert)
    out = np.zeros_like(verts)
    n = mo.optimize_vertex_fetch(out, over, np.ascontiguousarray(verts), vertex_count=nvert)  # remaps `over` in place
    return over, out[:n]


def _patch_simplify_with_attributes() -> None:
    import ctypes as C

    import meshoptimizer.simplifier as sm

    f = sm.lib.meshopt_simplifyWithAttributes
    if f.argtypes is None:
        P = C.c_void_p
        f.argtypes = [P, P, C.c_size_t, P, C.c_size_t, C.c_size_t, P, C.c_size_t, P, C.c_size_t, P,
                      C.c_size_t, C.c_float, C.c_uint, P]
        f.restype = C.c_size_t


LOD_MAX_REFINE = 4  # simplify -> measure attempts per level before keeping the unsimplified mesh


def _point_triangle_distance(p: np.ndarray, a: np.ndarray, b: np.ndarray, c: np.ndarray) -> np.ndarray:
    """Row-wise exact distance from points p to triangles (a, b, c) (Ericson, Real-Time Collision Detection 5.1.5)."""
    def dot(x: np.ndarray, y: np.ndarray) -> np.ndarray:
        return np.einsum("ij,ij->i", x, y)

    ab, ac, ap, bp, cp = b - a, c - a, p - a, p - b, p - c
    d1, d2, d3, d4, d5, d6 = dot(ab, ap), dot(ac, ap), dot(ab, bp), dot(ac, bp), dot(ab, cp), dot(ac, cp)
    va, vb, vc = d3 * d6 - d5 * d4, d5 * d2 - d1 * d6, d1 * d4 - d3 * d2
    den = va + vb + vc
    den = np.where(np.abs(den) < 1e-300, 1e-300, den)
    q = a + ab * (vb / den)[:, None] + ac * (vc / den)[:, None] - p  # face region, relative to p

    def seg(x: np.ndarray, d: np.ndarray) -> np.ndarray:  # x: segment start relative to p, d: direction
        t = np.clip(-dot(x, d) / np.maximum(dot(d, d), 1e-300), 0.0, 1.0)
        r = x + d * t[:, None]
        return dot(r, r)

    edge = np.minimum(np.minimum(seg(-ap, ab), seg(-bp, c - b)), seg(-cp, a - c))
    inside = (va >= 0) & (vb >= 0) & (vc >= 0)
    return np.sqrt(np.where(inside, dot(q, q), edge))


class _SurfaceIndex:
    """Search structure over one triangle mesh, built once and queried many times: centroids in a KD-tree
    (first pass), triangle bounding boxes (exact pruning bound) and, for large meshes, a KD-tree over the
    box centres so the second pass only visits boxes that can be closer than the bound found."""

    def __init__(self, p: np.ndarray, t: np.ndarray) -> None:
        from scipy.spatial import cKDTree

        p = np.asarray(p, np.float64)
        t = np.asarray(t, np.int64)
        self.a, self.b, self.c = p[t[:, 0]], p[t[:, 1]], p[t[:, 2]]
        self.m = len(t)
        self.tree = cKDTree((self.a + self.b + self.c) / 3) if self.m else None
        self.lo = np.minimum(np.minimum(self.a, self.b), self.c)
        self.hi = np.maximum(np.maximum(self.a, self.b), self.c)
        self._box_tree = None
        self._half = 0.0

    def _boxes(self):
        from scipy.spatial import cKDTree

        if self._box_tree is None:
            self._box_tree = cKDTree((self.lo + self.hi) / 2)
            self._half = float(np.linalg.norm((self.hi - self.lo) / 2, axis=1).max())
        return self._box_tree, self._half

    def distances(self, samples: np.ndarray, limit: float) -> np.ndarray:
        """Per-sample distance to the surface, exact wherever it exceeds ``limit`` (else an upper bound
        that is still <= ``limit``): first against the triangle with the nearest centroid, then the 8 nearest,
        then, for the samples still above ``limit``, against every triangle whose bounding box is closer than
        that bound. Each pass only sees the samples the previous one could not settle."""
        n = len(samples)
        if not n:
            return np.zeros(0)
        if not self.m:
            return np.full(n, np.inf)
        nn = np.asarray(self.tree.query(samples, k=1)[1]).reshape(-1)  # any triangle gives an upper bound
        ub = _point_triangle_distance(samples, self.a[nn], self.b[nn], self.c[nn])
        far = np.flatnonzero(ub > limit)
        k = min(8, self.m)
        if len(far) and k > 1:  # tighten with the 8 nearest centroids before the exhaustive pass
            nn = np.asarray(self.tree.query(samples[far], k=k)[1]).reshape(len(far), k)
            rows = np.repeat(far, k)
            f = nn.reshape(-1)
            d = _point_triangle_distance(samples[rows], self.a[f], self.b[f], self.c[f]).reshape(len(far), k).min(axis=1)
            ub[far] = np.minimum(ub[far], d)
            far = far[ub[far] > limit]
        if not len(far):
            return ub
        if self.m <= 2048:  # small target: bounding-box bound against every triangle
            step = max(1, 2_000_000 // self.m)
            for s0 in range(0, len(far), step):
                ids = far[s0:s0 + step]
                sl = samples[ids]
                gap = np.maximum(np.maximum(self.lo[None] - sl[:, None], sl[:, None] - self.hi[None]), 0.0)
                r2, c2 = np.nonzero(np.sqrt(np.einsum("ijk,ijk->ij", gap, gap)) < ub[ids][:, None])
                self._refine(ub, ids, sl, r2, c2)
            return ub
        import itertools

        tree, half = self._boxes()
        sl = samples[far]
        hits = tree.query_ball_point(sl, ub[far] + half)  # every box that can hold a closer triangle
        cnt = np.fromiter(map(len, hits), np.int64, len(hits))
        r2 = np.repeat(np.arange(len(far)), cnt)
        c2 = np.fromiter(itertools.chain.from_iterable(hits), np.int64, int(cnt.sum()))
        gap = np.maximum(np.maximum(self.lo[c2] - sl[r2], sl[r2] - self.hi[c2]), 0.0)
        keep = np.sqrt(np.einsum("ij,ij->i", gap, gap)) < ub[far][r2]
        self._refine(ub, far, sl, r2[keep], c2[keep])
        return ub

    def exceeds(self, samples: np.ndarray, limit: float) -> bool:
        """True iff some sample is farther than ``limit`` from the surface (exact), stopping at the first proof:
        candidates are refined exactly in small batches, farthest upper bound first."""
        if not len(samples):
            return False
        if not self.m:
            return True
        nn = np.asarray(self.tree.query(samples, k=1)[1]).reshape(-1)
        ub = _point_triangle_distance(samples, self.a[nn], self.b[nn], self.c[nn])
        far = np.flatnonzero(ub > limit)
        far = far[np.argsort(-ub[far], kind="stable")]
        for s0, step in ((0, 64), (64, 4096)):
            chunk = far[s0:s0 + step] if step == 64 else far[s0:]
            for c0 in range(0, len(chunk), step):
                ids = chunk[c0:c0 + step]
                if self.distances(samples[ids], limit).max() > limit:
                    return True
        return False

    def _refine(self, ub: np.ndarray, ids: np.ndarray, sl: np.ndarray, r2: np.ndarray, c2: np.ndarray) -> None:
        if len(r2):
            d = np.full(len(ids), np.inf)
            np.minimum.at(d, r2, _point_triangle_distance(sl[r2], self.a[c2], self.b[c2], self.c[c2]))
            ub[ids] = np.minimum(ub[ids], d)


def _one_sided(samples: np.ndarray, p: np.ndarray, t: np.ndarray, limit: float) -> float:
    """max over samples of the distance to surface (p, t), decided exactly against ``limit`` (exact whenever it
    exceeds ``limit``, otherwise an upper bound that is still <= ``limit``: the accept / reject decision is
    never wrong). See _SurfaceIndex.distances."""
    d = _SurfaceIndex(p, t).distances(np.asarray(samples, np.float64), limit)
    return float(d.max()) if len(d) else 0.0


def _coarse_samples(p1: np.ndarray, t1: np.ndarray) -> np.ndarray:
    a, b, c = p1[t1[:, 0]], p1[t1[:, 1]], p1[t1[:, 2]]
    return np.vstack([(a + b + c) / 3, (a + b) / 2, (b + c) / 2, (c + a) / 2])


def _surface_deviation(p0: np.ndarray, t0: np.ndarray, p1: np.ndarray, t1: np.ndarray, limit: float,
                       ref_index: _SurfaceIndex | None = None) -> float:
    """Two-sided surface distance (a sampled Hausdorff distance) between a mesh (p0, t0) and its
    simplification (p1, t1), in the units of the points, decided exactly against ``limit`` (see _one_sided).
    Reference -> simplified: the reference's vertices (where an edge collapse moves the surface most).
    Simplified -> reference: centroids and edge midpoints of the coarse triangles (chords and bulges; its
    vertices are reference vertices, at distance 0). ``ref_index``: a prebuilt index of (p0, t0) to reuse.
    A value above ``limit`` is exact for the side that exceeded it (the other side is then not measured)."""
    ref = ref_index if ref_index is not None else _SurfaceIndex(p0, t0)
    d1 = _SurfaceIndex(p1, t1).distances(p0[np.unique(t0)], limit)
    if len(d1) and d1.max() > limit:  # already rejected (and exact): skip the other side
        return float(d1.max())
    d2 = ref.distances(_coarse_samples(p1, t1), limit)
    return float(max(d1.max() if len(d1) else 0.0, d2.max() if len(d2) else 0.0))


# 13 view directions (axes, face and body diagonals; opposite directions project identically)
_VIEW_DIRS = np.array([(1, 0, 0), (0, 1, 0), (0, 0, 1), (1, 1, 0), (1, -1, 0), (1, 0, 1), (1, 0, -1), (0, 1, 1),
                       (0, 1, -1), (1, 1, 1), (1, 1, -1), (1, -1, 1), (-1, 1, 1)], np.float64)
_VIEW_DIRS /= np.linalg.norm(_VIEW_DIRS, axis=1, keepdims=True)


def _projected_areas(p: np.ndarray, t: np.ndarray) -> np.ndarray:
    """Silhouette area of a closed mesh seen along each of _VIEW_DIRS: (1/2) sum |n . d| a (Cauchy's
    projection formula; exact for convex parts, counts overlapping layers otherwise)."""
    n = np.cross(p[t[:, 1]] - p[t[:, 0]], p[t[:, 2]] - p[t[:, 0]]) / 2  # area-weighted normals
    return 0.5 * np.abs(n @ _VIEW_DIRS.T).sum(axis=0)


class _Simplifier:
    """Error-bounded simplification of one mesh (seam-split with UVs, or welded without).

    meshoptimizer's ``result_error`` is a quadric estimate, not a bound: measured on a 20-segment cylinder it
    reported 0.0151 m where the surfaces were 0.0310 m apart. So each level is simplified with
    SIMPLIFY_ERROR_ABSOLUTE at the budget, its real deviation is measured (:func:`_surface_deviation`), and the
    target is tightened until the measured deviation is within the budget.

    Seams: on a closed seam-split mesh meshoptimizer re-welds by position, so seam edges between two charts
    can collapse *along* the seam; vertices where three or more charts meet (box corners) stay. LOCK_BORDER
    therefore did nothing on closed meshes (measured: identical output with and without) and, on open meshes,
    froze borders the error bound already protects, so it is not used. UV attributes are weighted in metres of
    surface (the inverse of the mesh's texel scale), so a texture shift costs as much as a geometric one.
    """

    def __init__(self, points: np.ndarray, triangles: np.ndarray, uvs: np.ndarray | None) -> None:
        import meshoptimizer as mo

        _patch_simplify_with_attributes()
        self.pts64 = np.asarray(points, np.float64)
        pts = np.ascontiguousarray(points, dtype=np.float32)
        tri = np.asarray(triangles)
        if uvs is None:  # weld so duplicated hard-edge verts do not freeze the mesh
            cid = _canon(self.pts64)
            first = np.unique(cid, return_index=True)[1]
            pts = np.ascontiguousarray(pts[first])
            tri = cid[tri]
            self.uv = None
        else:
            self.uv = np.ascontiguousarray(uvs, dtype=np.float32)
        self.pts = pts
        self.idx0 = np.ascontiguousarray(tri, dtype=np.uint32).ravel()
        self.verts = pts if self.uv is None else np.ascontiguousarray(np.hstack([pts, self.uv]))
        self.scale = float(mo.simplify_scale(pts)) or 1.0
        self.ref = (pts.astype(np.float64), self.idx0.reshape(-1, 3).astype(np.int64))
        self.uv_w = None
        if self.uv is not None:
            a3 = _tri_areas(self.ref[0], self.ref[1], 3).sum()
            a2 = _tri_areas(self.uv.astype(np.float64), self.ref[1], 2).sum()
            m_per_uv = float(np.sqrt(a3 / a2)) if a2 > 0 else 1.0
            self.uv_w = np.full(2, m_per_uv / self.scale, np.float32)  # relative units, like positions
        self._cache: dict[float, tuple[np.ndarray, float]] = {}
        self._area0 = _projected_areas(*self.ref)
        self._floor = 0.0  # largest budget known to remove nothing: smaller budgets cannot do better
        self._measured: dict[bytes, tuple[float, float]] = {}
        self._ref_index: _SurfaceIndex | None = None  # built on first measurement, reused by every level/attempt

    @property
    def ref_index(self) -> _SurfaceIndex:
        if self._ref_index is None:
            self._ref_index = _SurfaceIndex(*self.ref)
        return self._ref_index

    def _run(self, target_error_m: float) -> np.ndarray:
        import meshoptimizer as mo

        dst = np.zeros_like(self.idx0)
        err = np.zeros(1, np.float32)
        opts = mo.SIMPLIFY_ERROR_ABSOLUTE
        try:
            if self.uv is None:
                n = mo.simplify(dst, self.idx0, self.pts, target_index_count=0, target_error=target_error_m,
                                options=opts, result_error=err)
            else:
                n = mo.simplify_with_attributes(dst, self.idx0, self.pts, self.uv, self.uv_w, target_index_count=0,
                                                target_error=target_error_m, options=opts, result_error=err)
        except Exception as e:  # noqa: BLE001 - documented fallback
            log.warning("meshopt simplify failed (%s); using simplify_sloppy", e)
            n = mo.simplify_sloppy(dst, self.idx0, self.pts, target_index_count=0,
                                   target_error=target_error_m / self.scale, result_error=err)
        return dst[:n].copy()

    def silhouette_change(self, idx: np.ndarray) -> float:
        """Largest relative change of the part's silhouette area over _VIEW_DIRS."""
        a1 = _projected_areas(self.ref[0], idx.reshape(-1, 3).astype(np.int64))
        return float(np.max(np.abs(a1 - self._area0) / np.maximum(self._area0, 1e-30)))

    def _measure(self, idx: np.ndarray, silhouette_tol: float, limit: float) -> tuple[float, float]:
        """(silhouette change, surface deviation) of a candidate, memoised: nearby targets often give the same
        index buffer. The deviation is only measured when the cheap silhouette test passes (else inf); it is
        exact above ``limit`` and an upper bound below it, so a memoised value decides any later limit too."""
        key = idx.tobytes()
        if key not in self._measured:
            sil = self.silhouette_change(idx)
            dev = (_surface_deviation(self.ref[0], self.ref[1], self.ref[0], idx.reshape(-1, 3).astype(np.int64), limit,
                                      self.ref_index) if sil <= silhouette_tol else np.inf)
            self._measured[key] = (sil, dev)
        return self._measured[key]

    def within(self, budget_m: float, silhouette_tol: float = np.inf) -> tuple[np.ndarray, float]:
        """Cheapest index buffer found whose measured deviation from the input is <= budget_m and whose
        silhouette area changes by at most ``silhouette_tol`` in every view direction."""
        key = round(float(budget_m), 12)
        if key in self._cache:
            return self._cache[key]
        best = (self.idx0, 0.0)
        target = budget_m
        if budget_m > self._floor:
            for _ in range(LOD_MAX_REFINE):
                idx = self._run(target)
                if len(idx) == 0 or len(idx) >= len(self.idx0):
                    break
                sil, dev = self._measure(idx, silhouette_tol, budget_m)
                if dev <= budget_m and sil <= silhouette_tol:
                    best = (idx, dev)
                    break
                shrink = 0.8 if dev <= budget_m or not np.isfinite(dev) else 0.9 * budget_m / max(dev, 1e-30)
                if sil > silhouette_tol:
                    shrink = min(shrink, 0.9 * silhouette_tol / sil)
                target *= min(0.8, shrink)
            if best[0] is self.idx0:
                self._floor = max(self._floor, budget_m)
        self._cache[key] = best
        return best

    def level(self, idx: np.ndarray, dev: float, screen_fraction: float) -> LOD:
        idx, v = _optimise(np.ascontiguousarray(idx, np.uint32), self.verts, len(self.verts))
        return LOD(len(idx) / max(len(self.idx0), 1), v[:, :3].astype(np.float64), idx.reshape(-1, 3).astype(np.int32),
                   None if self.uv is None else v[:, 3:5].copy(), dev / self.scale, dev, float(screen_fraction))


LOD_DROP_MAX_SAMPLES = 200_000  # a part needing more surface samples than this is never dropped (conservative)


def _dense_samples(p: np.ndarray, t: np.ndarray, spacing: float) -> np.ndarray | None:
    """Barycentric grid on every triangle, sub-triangle edges <= ``spacing``: every surface point is then
    within spacing / sqrt(3) of a sample (circumradius bound), so a 1-Lipschitz distance measured on the
    samples is exact to that cover radius. None when that would take more than LOD_DROP_MAX_SAMPLES."""
    a, b, c = p[t[:, 0]], p[t[:, 1]], p[t[:, 2]]
    longest = np.max(np.stack([np.linalg.norm(b - a, axis=1), np.linalg.norm(c - b, axis=1), np.linalg.norm(a - c, axis=1)]), axis=0)
    k = np.maximum(np.ceil(longest / max(spacing, 1e-12)), 1).astype(np.int64)
    if int(((k + 1) * (k + 2) // 2).sum()) > LOD_DROP_MAX_SAMPLES:
        return None
    out = []
    for kk in np.unique(k):
        sel = k == kk
        i, j = np.meshgrid(np.arange(kk + 1), np.arange(kk + 1), indexing="ij")
        ok = (i + j) <= kk
        u, v = (i[ok] / kk)[None, :, None], (j[ok] / kk)[None, :, None]
        out.append((a[sel][:, None] * (1 - u - v) + b[sel][:, None] * u + c[sel][:, None] * v).reshape(-1, 3))
    return np.vstack(out)


def _gather(ms: Sequence[tuple[np.ndarray, np.ndarray]]) -> tuple[np.ndarray, np.ndarray]:
    pts, tris, off = [], [], 0
    for q, t in ms:
        if len(t):
            pts.append(np.asarray(q, np.float64))
            tris.append(np.asarray(t, np.int64).reshape(-1, 3) + off)
            off += len(q)
    return (np.vstack(pts), np.vstack(tris)) if pts else (np.zeros((0, 3)), np.zeros((0, 3), np.int64))


class _Silhouettes:
    """Orthographic silhouettes of an asset's LOD0 along the 13 _VIEW_DIRS, rendered once, with the asset's
    bounding sphere spanning ``object_px`` pixels (its on-screen size at the level being judged, capped at
    512 px); :meth:`iou` compares another version of the asset against them (smallest IoU over the views)."""

    def __init__(self, asset0: Sequence[tuple[np.ndarray, np.ndarray]], object_px: float) -> None:
        p0, t0 = _gather(asset0)
        self.centre = (p0.min(0) + p0.max(0)) / 2
        radius = max(float(np.linalg.norm(p0.max(0) - p0.min(0))) / 2, 1e-9)
        px = min(float(object_px), 512.0)
        self.size = int(max(16, np.ceil(px * 1.1)))
        self.scale = px / (2 * radius)
        self.frames = []
        for d in _VIEW_DIRS:
            r = np.cross(d, [0, 0, 1.0] if abs(d[2]) < 0.9 else [1.0, 0, 0])
            r /= np.linalg.norm(r)
            self.frames.append((d, r, np.cross(r, d)))
        self.masks0 = [self._mask(p0, t0, f) for f in self.frames]

    def _mask(self, p: np.ndarray, t: np.ndarray, frame) -> np.ndarray:
        from antigravity3d.kernel.raster import _rasterise

        if not len(t):
            return np.zeros((self.size, self.size), bool)
        d, r, u = frame
        q = p - self.centre
        sxy = np.stack([q @ r * self.scale + self.size / 2, self.size / 2 - q @ u * self.scale], axis=1)
        return _rasterise(sxy, (q @ d).astype(np.float32), t, self.size)[0] >= 0

    def iou(self, version: Sequence[tuple[np.ndarray, np.ndarray]]) -> float:
        p1, t1 = _gather(version)
        worst = 1.0
        for m0, f in zip(self.masks0, self.frames):
            m1 = self._mask(p1, t1, f)
            union = (m0 | m1).sum()
            worst = min(worst, float((m0 & m1).sum() / union) if union else 1.0)
        return worst


def _bounds_radius(points: Sequence[np.ndarray]) -> float:
    p = [np.asarray(x, np.float64) for x in points if len(x)]
    lo = np.min([x.min(axis=0) for x in p], axis=0)
    hi = np.max([x.max(axis=0) for x in p], axis=0)
    return float(np.linalg.norm(hi - lo)) / 2


def _absent(s: "_Simplifier", screen_fraction: float) -> LOD:
    """A part that is not drawn at this level: no points, no triangles (``tri_count == 0``)."""
    return LOD(0.0, np.zeros((0, 3)), np.zeros((0, 3), np.int32), None if s.uv is None else np.zeros((0, 2), np.float32),
               0.0, 0.0, float(screen_fraction))


def _droppable(simp: dict[str, "_Simplifier"], picks: dict[str, tuple[np.ndarray, float]], already: set[str], budget: float,
               asset0: list[tuple[np.ndarray, np.ndarray]], min_iou: float, object_px: float) -> set[str]:
    """Parts that may be absent at a level: removing them keeps the asset within the level's budget.

    A part P may go when every point of its LOD0 surface is within ``budget`` of what remains drawn at this
    level (the other parts' meshes at this level): the asset's two-sided surface distance to LOD0 is then still
    <= budget (the other direction is the remaining parts' own measured deviation). Measured on a sample grid
    with spacing budget / 4 whose cover radius (budget / 4 / sqrt 3) is subtracted from the limit, so the bound
    is exact, not sampled. And the asset's silhouette, seen from 13 directions at its on-screen size, must keep
    IoU >= ``min_iou`` against LOD0. Greedy, smallest surface first, ties by name; parts already absent at the
    previous level stay absent. Never every part."""
    gone = set(already)
    sil: _Silhouettes | None = None  # LOD0 silhouettes, rendered on first need and shared by every candidate
    spacing = budget / 4
    limit = budget - spacing / np.sqrt(3)
    order = sorted((nid for nid in simp if nid not in gone),
                   key=lambda n: (float(_tri_areas(simp[n].ref[0], simp[n].ref[1], 3).sum()), n))
    for nid in order:
        rest = [n for n in simp if n not in gone and n != nid]
        if not rest:
            break
        pts, tris, off = [], [], 0
        for n in rest:
            pts.append(simp[n].ref[0])
            tris.append(picks[n][0].reshape(-1, 3).astype(np.int64) + off)
            off += len(simp[n].ref[0])
        index = _SurfaceIndex(np.vstack(pts), np.vstack(tris))
        # the part's own vertices first: exact, cheap, and a lower bound of the maximum
        if index.exceeds(simp[nid].ref[0][np.unique(simp[nid].ref[1])], limit):
            continue
        samples = _dense_samples(simp[nid].ref[0], simp[nid].ref[1], spacing)
        if samples is None or index.exceeds(samples, limit):
            continue
        level = [(simp[n].ref[0], picks[n][0].reshape(-1, 3)) for n in simp if n not in gone and n != nid]
        if sil is None:
            sil = _Silhouettes(asset0, object_px)
        if sil.iou(level) < min_iou:
            continue
        gone.add(nid)
    return gone


def generate_asset_lods(
    parts: Sequence[UVResult | tuple[str, np.ndarray, np.ndarray, np.ndarray | None]],
    *,
    screen_fractions: Sequence[float] | None = None,
    error_px: float | None = None,
    screen_px: float | None = None,
    min_reduction: float | None = None,
) -> dict[str, list[LOD]]:
    """LOD chains of one asset, levels defined by what the viewer can see (one definition with lod_diff).

    Candidate level k is shown from screen fraction f_k (lod_diff.lod_candidate_fractions(): the checker's
    LOD_SCREEN_FRACTIONS continued by halving to LOD_MIN_SCREEN_FRACTION) down to the next level's. Its budget is the error that projects to LOD_MAX_ERROR_PX pixels of a LOD_REFERENCE_SCREEN_PX
    frame at f_k for the asset's bounding sphere (lod_diff.lod_error_budget_m); every part is simplified to
    the cheapest mesh whose *measured* deviation stays within that budget and whose silhouette area, seen
    from 13 directions, changes by at most half the LOD-pop IoU tolerance ((1 - min_iou) / 2). The second
    condition is what keeps thin parts: a 8 cm pole may move less than the asset-scale pixel budget and still
    lose a quarter of its outline by turning into a prism, which the IoU gate (and the eye) would see. A candidate is emitted only when the asset's triangle count drops by at
    least LOD_MIN_REDUCTION from the previous emitted level; otherwise the previous level stays in use down
    to the next candidate. Every part gets one entry per emitted level (a part that cannot get cheaper
    repeats its mesh), so ``len(chain)`` is the asset's level count; LOD0 is the input with
    screen_fraction 1.0. An asset nothing can be removed from gets LOD0 only.
    """
    from antigravity3d.kernel import lod_diff as ld

    fr = tuple(screen_fractions) if screen_fractions is not None else ld.lod_candidate_fractions()
    error_px = ld.LOD_MAX_ERROR_PX if error_px is None else error_px
    screen_px = ld.LOD_REFERENCE_SCREEN_PX if screen_px is None else screen_px
    min_reduction = ld.LOD_MIN_REDUCTION if min_reduction is None else min_reduction
    silhouette_tol = (1.0 - ld.PopThresholds().min_iou) / 2.0  # per part; leaves half the IoU tolerance for raster noise
    items = [(p.node_id, p.points, p.triangles, p.uvs) if isinstance(p, UVResult) else p for p in parts]
    if not items:
        return {}
    empty = {nid: [LOD(1.0, np.asarray(pts, np.float64), np.zeros((0, 3), np.int32), uv, 0.0, 0.0, 1.0)]
             for nid, pts, tri, uv in items if len(tri) == 0}
    items = [it for it in items if len(it[2])]
    if not items:
        return empty
    radius = _bounds_radius([it[1] for it in items])
    simp = {nid: _Simplifier(pts, tri, uv) for nid, pts, tri, uv in items}
    chains = {nid: [s.level(s.idx0, 0.0, 1.0)] for nid, s in simp.items()}
    prev_tris = sum(len(s.idx0) // 3 for s in simp.values())
    dropped: set[str] = set()  # parts absent at the last emitted level stay absent further out
    asset0 = [(s.ref[0], s.ref[1]) for s in simp.values()]
    for f in fr[1:]:
        budget = ld.lod_error_budget_m(radius, f, error_px=error_px, screen_px=screen_px)
        picks = {nid: s.within(budget, silhouette_tol) for nid, s in simp.items()}
        gone = _droppable(simp, picks, dropped, budget, asset0, 1.0 - silhouette_tol, f * screen_px) if len(simp) > 1 else set()
        tris = sum(len(idx) // 3 for nid, (idx, _) in picks.items() if nid not in gone)
        if tris >= prev_tris or tris > (1.0 - min_reduction) * prev_tris:  # never a duplicate level
            continue
        for nid, (idx, dev) in picks.items():
            chains[nid].append(_absent(simp[nid], f) if nid in gone else simp[nid].level(idx, dev, f))
        dropped, prev_tris = gone, tris
    for nid, chain in empty.items():  # keep every part's chain as long as the asset's
        chains[nid] = chain * max((len(c) for c in chains.values()), default=1)
    return chains


def generate_lods(
    points: np.ndarray,
    triangles: np.ndarray,
    ratios: Sequence[float] | None = None,
    uvs: np.ndarray | None = None,
    *,
    screen_fractions: Sequence[float] | None = None,
) -> list[LOD]:
    """LOD chain of a single mesh.

    Default (``ratios`` None): screen-space levels exactly as :func:`generate_asset_lods` defines them, with
    the mesh as the whole asset. ``ratios`` asks for explicit triangle-ratio targets instead (fixtures and
    experiments; the export pipeline never uses it): each level is simplified towards ``ratio`` of the input
    without an error bound and reports its measured deviation; a ratio that removes nothing is skipped.
    """
    if ratios is None:
        return generate_asset_lods([("mesh", points, triangles, uvs)], screen_fractions=screen_fractions)["mesh"]
    import meshoptimizer as mo

    s = _Simplifier(points, triangles, uvs)
    out: list[LOD] = []
    for r in ratios:
        if r >= 1.0:
            idx, dev = s.idx0, 0.0
        else:
            target = max(12, int(len(s.idx0) * r) // 3 * 3)  # >= 4 tris
            dst = np.zeros_like(s.idx0)
            if s.uv is None:
                n = mo.simplify(dst, s.idx0, s.pts, target_index_count=target, target_error=1.0)
            else:
                n = mo.simplify_with_attributes(dst, s.idx0, s.pts, s.uv, s.uv_w, target_index_count=target, target_error=1.0)
            idx = dst[:n].copy()
            if len(idx) == 0 or (out and len(idx) >= out[-1].tri_count * 3):
                continue
            dev = _surface_deviation(s.ref[0], s.ref[1], s.ref[0], idx.reshape(-1, 3).astype(np.int64), 0.0)  # exact
        out.append(s.level(idx, dev, 1.0))
    return out


# --------------------------------------------------------------------------- colliders


@dataclass(frozen=True)
class Hull:
    name: str
    points: np.ndarray
    triangles: np.ndarray
    volume: float
    source: Literal["fast_path", "coacd", "merged"]


def _hull(points: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
    from scipy.spatial import ConvexHull

    h = ConvexHull(points)
    used = np.unique(h.simplices)
    remap = np.zeros(len(points), np.int64)
    remap[used] = np.arange(len(used))
    tris = remap[h.simplices]
    pts = points[used]
    c = pts.mean(axis=0)
    p = pts[tris]
    flip = np.einsum("ij,ij->i", np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]), p[:, 0] - c) < 0
    tris[flip] = tris[flip][:, ::-1]
    return pts, tris.astype(np.int32), float(h.volume)


def generate_colliders(
    meshes: list[MeshData], max_hulls: int, threshold: float = 0.05, asset_name: str = "Asset"
) -> list[Hull]:
    import coacd

    coacd.set_log_level("error")
    raw: list[tuple[np.ndarray, np.ndarray, float, str]] = []
    concave: list[tuple[MeshData, float]] = []
    for m in meshes:
        if not m.collider or len(m.triangles) == 0:
            continue
        pts = np.asarray(m.points, dtype=np.float64)
        pp, tt, hv = _hull(pts)
        topo = _topology(pts, np.asarray(m.triangles))
        if topo.closed and topo.volume > 0 and hv <= topo.volume * 1.02:
            raw.append((pp, tt, hv, "fast_path"))
        else:
            concave.append((m, hv))
    if concave:
        budget_left = max(len(concave), max_hulls - len(raw))
        for k, (m, hv) in enumerate(sorted(concave, key=lambda x: -x[1])):
            share = max(1, budget_left // (len(concave) - k))
            budget_left -= share
            pts = np.asarray(m.points, dtype=np.float64)
            tri = np.asarray(m.triangles)
            # preprocess "off" segfaulted CoACD on overlapping multi-body input, so only skip the
            # manifold pre-pass for a single closed body.
            clean = _topology(pts, tri).closed and _single_component(pts, tri)
            parts = coacd.run_coacd(
                coacd.Mesh(pts, np.asarray(m.triangles, dtype=np.int32)),
                threshold=threshold,
                max_convex_hull=share,
                preprocess_mode="off" if clean else "auto",
                resolution=500,
                mcts_iterations=40,
                mcts_nodes=10,
                max_ch_vertex=64,
                decimate=True,
                seed=0,
            )
            for v, _f in parts:
                pp, tt, hv2 = _hull(np.asarray(v, dtype=np.float64))
                raw.append((pp, tt, hv2, "coacd"))
    while len(raw) > max(1, max_hulls):
        raw.sort(key=lambda h: h[2])
        a, b = raw[0], raw[1]
        pp, tt, hv = _hull(np.vstack([a[0], b[0]]))
        raw = [(pp, tt, hv, "merged")] + raw[2:]
    raw.sort(key=lambda h: -h[2])
    return [Hull(f"UCX_{asset_name}_{i:02d}", p, t, v, s) for i, (p, t, v, s) in enumerate(raw)]


# --------------------------------------------------------------------------- export


def write_gltf(
    path: str | Path,
    items: Sequence[LOD | MeshData | UVResult | Hull],
    names: Sequence[str] | None = None,
    y_up: bool = True,
    *,
    materials: Sequence | None = None,
) -> Path:
    """Writes a GLB. Our world is Z-up; glTF is Y-up, so (x,y,z) -> (x,z,-y) unless y_up=False.

    ``materials`` holds one ``gltf_writer.GltfMaterial`` (or None) per item; images inside are embedded as the
    bytes they already hold (see kernel/gltf_writer.py). Items with UVs and no material get a grey default.
    Items with UVs (the rendered ones; collider hulls have none) also get shading normals: the same crease
    normals the package USD authors (``compile_ir.crease_normals``, 40 degrees), converted to one per vertex by
    splitting vertices at creases, so the GLB shades like the USD. Computed from the points, triangles and UVs
    handed in, whatever their layout.
    """
    from antigravity3d.kernel import gltf_writer as gw
    from antigravity3d.kernel.compile_ir import crease_normals

    default_material = gw.GltfMaterial(name="Default", base_color_factor=(0.5, 0.5, 0.5, 1.0),
                                       metallic_factor=0.0, roughness_factor=0.5)
    meshes = []
    for i, it in enumerate(items):
        uv = getattr(it, "uvs", None)
        p = np.asarray(it.points, dtype=np.float64)
        tris = np.asarray(it.triangles)
        normals = None
        if uv is not None:
            p, tris, uv, normals = gw.split_by_corner_normals(p, tris, uv, crease_normals(p, tris))
            if y_up:
                normals = gw.z_up_to_y_up(normals)
        if y_up:
            p = gw.z_up_to_y_up(p)
        material = None
        if uv is not None:
            material = materials[i] if materials and materials[i] is not None else default_material
            uv = gw.flip_v(uv)
        name = names[i] if names else getattr(it, "name", None) or getattr(it, "node_id", None) or f"mesh_{i}"
        meshes.append(gw.GltfMesh(name, p, tris, uv, material, normals))
    return gw.write_glb(path, meshes)


def compress_texture_ktx2(png_path: str | Path, out_path: str | Path, mode: Literal["uastc", "etc1s"] = "uastc") -> Path | None:
    """Shells out to KTX-Software (`ktx create`, else `toktx`). None when neither is installed."""
    out_path = Path(out_path)
    if ktx := shutil.which("ktx"):
        cmd = [ktx, "create", "--format", "R8G8B8A8_SRGB", "--encode", "basis-lz" if mode == "etc1s" else "uastc",
               "--generate-mipmap", str(png_path), str(out_path)]
    elif toktx := shutil.which("toktx"):
        cmd = [toktx, "--t2", "--encode", "etc1s" if mode == "etc1s" else "uastc", "--genmipmap", str(out_path), str(png_path)]
    else:
        log.warning("ktx/toktx not on PATH; skipping KTX2 compression of %s", png_path)
        return None
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0 or not out_path.exists():
        log.warning("KTX2 compression failed: %s", r.stderr.strip()[:300])
        return None
    return out_path


# --------------------------------------------------------------------------- pipeline


@dataclass
class ExportBundle:
    asset_name: str
    uvs: list[UVResult]
    lods: dict[str, list[LOD]]
    colliders: list[Hull]
    timings_ms: dict[str, float] = field(default_factory=dict)


def run_export_pipeline(meshes: list[MeshData], budget: EngineBudget, asset_name: str) -> ExportBundle:
    t = {}
    t0 = time.perf_counter()
    uvs = unwrap_scene(meshes, budget)
    t1 = time.perf_counter()
    lods = generate_asset_lods(uvs)
    t2 = time.perf_counter()
    hulls = generate_colliders(meshes, budget.max_collision_hulls, asset_name=asset_name)
    t3 = time.perf_counter()
    t = {"uv": (t1 - t0) * 1e3, "lod": (t2 - t1) * 1e3, "colliders": (t3 - t2) * 1e3, "total": (t3 - t0) * 1e3}
    return ExportBundle(asset_name, uvs, lods, hulls, {k: round(v, 2) for k, v in t.items()})
