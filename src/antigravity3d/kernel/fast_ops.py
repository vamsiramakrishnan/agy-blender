"""Deterministic in-process mesh kernels: Tier-1 QA, UVs, LODs, colliders, export.

No LLM tokens, no Blender. Native backends actually used:
  * QA          : numpy (edge-table topology, winding-number interpenetration probe)
  * UVs         : xatlas
  * LODs        : meshoptimizer (simplify / simplify_with_attributes, vertex cache,
                  overdraw, vertex fetch)
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
    charts: str = "computed"    # "analytic": exact developable charts from the IR; "computed": xatlas charts
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


def _unfold_charts(points: np.ndarray, tris: np.ndarray, charts: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Isometric unfolding of known developable charts. Returns (vmapping, triangles, uv in metres).

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
    placed = np.zeros(m, bool)
    next_chart = int(chart_of.max()) + 1 if m else 0
    seen_root: set[int] = set()

    def lay_first(fi: int) -> None:
        a, b, c = p[t[fi]]
        ab = np.linalg.norm(b - a)
        x = np.dot(c - a, b - a) / max(ab, 1e-30)
        y = np.linalg.norm(np.cross(b - a, c - a)) / max(ab, 1e-30)
        corner[fi] = [[0.0, 0.0], [ab, 0.0], [x, y]]

    for root in range(m):
        if placed[root]:
            continue
        if int(chart_of[root]) in seen_root:  # a second edge-connected component of one chart: its own chart
            comp_chart = next_chart
            next_chart += 1
        else:
            comp_chart = int(chart_of[root])
            seen_root.add(comp_chart)
        lay_first(root)
        placed[root] = True
        chart_of[root] = comp_chart
        queue = [root]
        qi = 0
        while qi < len(queue):
            fi = queue[qi]
            qi += 1
            for gi, s in nbr[fi]:
                if placed[gi]:
                    continue
                va, vb = t[fi, s], t[fi, (s + 1) % 3]
                ga = int(np.flatnonzero(t[gi] == va)[0])
                gb = int(np.flatnonzero(t[gi] == vb)[0])
                gc = 3 - ga - gb
                A, B = corner[fi, s], corner[fi, (s + 1) % 3]
                d = B - A
                dab = float(np.hypot(*d))
                la = float(np.linalg.norm(p[t[gi, gc]] - p[va]))
                lb = float(np.linalg.norm(p[t[gi, gc]] - p[vb]))
                x = (la * la - lb * lb + dab * dab) / (2 * max(dab, 1e-30))
                y = np.sqrt(max(la * la - x * x, 0.0))
                ex = d / max(dab, 1e-30)
                ey = np.array([-ex[1], ex[0]])
                cand = A + x * ex + y * ey
                tri_uv = np.zeros((3, 2))
                tri_uv[ga], tri_uv[gb], tri_uv[gc] = A, B, cand
                u0, u1, u2 = tri_uv
                if (u1[0] - u0[0]) * (u2[1] - u0[1]) - (u1[1] - u0[1]) * (u2[0] - u0[0]) < 0:
                    tri_uv[gc] = A + x * ex - y * ey
                corner[gi] = tri_uv
                placed[gi] = True
                chart_of[gi] = comp_chart
                queue.append(gi)
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
    return np.asarray(out_v, np.int64), ids.reshape(-1, 3).astype(np.int32), np.asarray(out_uv, np.float64).reshape(-1, 2)


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
    """Exact layout of a mesh with known charts; charts longer than ``max_extent_m`` are cut across their long
    axis along existing edges when the triangulation allows it (a long cylinder side becomes two rectangles)."""
    if m.uv_charts is None or len(m.uv_charts) != len(m.triangles) or len(m.triangles) == 0:
        return None
    charts = np.asarray(m.uv_charts, np.int64).copy()
    tris = np.asarray(m.triangles, np.int64)
    for _ in range(4):
        vmap, t2, uv = _unfold_charts(m.points, tris, charts)
        if max_extent_m is None:
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
            if wh[ax] <= max_extent_m:
                continue
            k = int(np.ceil(wh[ax] / max_extent_m))
            coord = (cent[sel] @ R.T)[:, ax] - lo[ax]
            slab = np.minimum((coord / wh[ax] * k).astype(np.int64), k - 1)
            if len(np.unique(slab)) < 2:
                continue  # one triangle spans the whole length (e.g. a tall pole): cannot cut, density will fit
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
        new.sort()
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


def _xatlas_options(resolution: int, tpu: float, *, input_uvs: bool):
    import xatlas

    co = xatlas.ChartOptions()
    co.use_input_mesh_uvs = input_uvs
    po = xatlas.PackOptions()
    po.resolution = resolution
    po.texels_per_unit = float(tpu)
    po.padding = UV_PADDING_PX
    po.bruteForce = False
    po.rotate_charts = True
    return co, po


def _xatlas_fit(add, density: float, resolution: int, *, input_uvs: bool):
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
    co, po = _xatlas_options(0, density, input_uvs=input_uvs)
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
    def add(atlas, prev=None, scale=None):
        for i, (m, c) in enumerate(zip(group, known)):
            if prev is not None:  # pack-only pass: the previous pass' layout as input UVs (charts kept)
                vm, idx, uv = prev[i]
                base = c.vmap if c is not None else np.arange(len(m.points))
                atlas.add_mesh(np.ascontiguousarray(np.asarray(m.points)[base[vm.astype(np.int64)]], dtype=np.float32),
                               np.ascontiguousarray(idx, dtype=np.uint32), uvs=np.ascontiguousarray(uv * scale, dtype=np.float32))
            elif c is not None:
                atlas.add_mesh(np.ascontiguousarray(np.asarray(m.points)[c.vmap], dtype=np.float32),
                               np.ascontiguousarray(c.tris, dtype=np.uint32), uvs=np.ascontiguousarray(c.uv_m, dtype=np.float32))
            else:
                atlas.add_mesh(np.ascontiguousarray(m.points, dtype=np.float32), np.ascontiguousarray(m.triangles, dtype=np.uint32))

    if any(c is not None for c in known):  # mixed group: compute the missing charts first, then pack all as input UVs
        todo = [m for m, c in zip(group, known) if c is None]
        import xatlas

        ca = xatlas.Atlas()
        for m in todo:
            ca.add_mesh(np.ascontiguousarray(m.points, dtype=np.float32), np.ascontiguousarray(m.triangles, dtype=np.uint32))
        co, po = _xatlas_options(0, density, input_uvs=False)
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
    atlas, tpu = _xatlas_fit(add, density, resolution, input_uvs=all(c is not None for c in known))
    fitted = tpu >= density * (1 - 1e-9)
    out = []
    for i, (m, c) in enumerate(zip(group, known)):
        vm, idx, uv = atlas.get_mesh(i)
        vm = vm.astype(np.int64)
        if c is not None:
            vm = c.vmap[vm]
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
    ratio: float
    points: np.ndarray           # (V, 3) float64 (float32 precision)
    triangles: np.ndarray        # (M, 3) int32
    uvs: np.ndarray | None
    error: float                 # meshoptimizer result_error (relative to mesh scale)
    error_m: float               # same, in metres

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


def generate_lods(
    points: np.ndarray,
    triangles: np.ndarray,
    ratios: Sequence[float] = (1.0, 0.5, 0.25, 0.1),
    uvs: np.ndarray | None = None,
) -> list[LOD]:
    import meshoptimizer as mo

    _patch_simplify_with_attributes()
    pts = np.ascontiguousarray(points, dtype=np.float32)
    tri = np.asarray(triangles)
    if uvs is None:  # weld so duplicated hard-edge verts do not freeze the mesh
        cid = _canon(np.asarray(points, dtype=np.float64))
        first = np.unique(cid, return_index=True)[1]
        pts = np.ascontiguousarray(pts[first])
        tri = cid[tri]
        uv32 = None
    else:
        uv32 = np.ascontiguousarray(uvs, dtype=np.float32)
    idx0 = np.ascontiguousarray(tri, dtype=np.uint32).ravel()
    verts = pts if uv32 is None else np.ascontiguousarray(np.hstack([pts, uv32]))
    scale = float(mo.simplify_scale(pts)) or 1.0
    lods: list[LOD] = []
    for r in ratios:
        err = np.zeros(1, np.float32)
        if r >= 1.0:
            idx = idx0.copy()
        else:
            target = max(12, int(len(idx0) * r) // 3 * 3)  # >= 4 tris
            dst = np.zeros_like(idx0)
            opts = mo.SIMPLIFY_LOCK_BORDER
            try:
                if uv32 is None:
                    n = mo.simplify(dst, idx0, pts, target_index_count=target, target_error=1.0, options=opts, result_error=err)
                else:
                    n = mo.simplify_with_attributes(
                        dst, idx0, pts, uv32, np.array([1.0, 1.0], np.float32),
                        target_index_count=target, target_error=1.0, options=opts, result_error=err,
                    )
            except Exception as e:  # noqa: BLE001 - documented fallback
                log.warning("meshopt simplify failed (%s); using simplify_sloppy", e)
                n = mo.simplify_sloppy(dst, idx0, pts, target_index_count=target, target_error=1.0, result_error=err)
            idx = dst[:n].copy()
        if len(idx) == 0:
            continue
        idx, v = _optimise(idx, verts, len(verts))
        lods.append(
            LOD(
                float(r),
                v[:, :3].astype(np.float64),
                idx.reshape(-1, 3).astype(np.int32),
                None if uv32 is None else v[:, 3:5].copy(),
                float(err[0]),
                float(err[0]) * scale,
            )
        )
    return lods


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
    lods = {u.node_id: generate_lods(u.points, u.triangles, uvs=u.uvs) for u in uvs}
    t2 = time.perf_counter()
    hulls = generate_colliders(meshes, budget.max_collision_hulls, asset_name=asset_name)
    t3 = time.perf_counter()
    t = {"uv": (t1 - t0) * 1e3, "lod": (t2 - t1) * 1e3, "colliders": (t3 - t2) * 1e3, "total": (t3 - t0) * 1e3}
    return ExportBundle(asset_name, uvs, lods, hulls, {k: round(v, 2) for k, v in t.items()})
