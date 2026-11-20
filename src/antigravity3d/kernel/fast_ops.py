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
    "uv_overflow": "xatlas re-run with texels_per_unit=0 (auto-fit) when requested density overflows the atlas",
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
    if valid:
        lo = np.min([v[2] for v in valid], axis=0)
        hi = np.max([v[3] for v in valid], axis=0)
        bbox = (lo.round(4).tolist(), hi.round(4).tolist())
        if abs(lo[2]) > ground_tol:
            kind = "floating" if lo[2] > 0 else "sunk below ground"
            add("ungrounded", "warning", None, f"min z {lo[2]:.4f} m ({kind}, tol {ground_tol})")
        if len(valid) > 1:
            for i, (m, _, l, h) in enumerate(valid):
                if l[2] <= ground_tol:
                    continue
                if not any(
                    j != i and (l - touch_eps <= v[3]).all() and (h + touch_eps >= v[2]).all()
                    for j, v in enumerate(valid)
                ):
                    add("floating_part", "warning", m.node_id, f"AABB touches nothing; min z {l[2]:.3f}")
        for i in range(len(valid)):
            for j in range(i + 1, len(valid)):
                _check_pair(valid[i], valid[j], add)

    ms = (time.perf_counter() - t0) * 1e3
    stats = {
        "tris": total_tris,
        "verts": total_verts,
        "draw_calls": draw_calls,
        "max_tris": budget.max_tris,
        "max_draw_calls": budget.max_draw_calls,
        "bbox": bbox,
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


def _pack(group: list[MeshData], density: float, resolution: int):
    import xatlas

    for tpu in (density, 0.0):
        atlas = xatlas.Atlas()
        for m in group:
            atlas.add_mesh(np.ascontiguousarray(m.points, dtype=np.float32), np.ascontiguousarray(m.triangles, dtype=np.uint32))
        po = xatlas.PackOptions()
        po.resolution = resolution
        po.texels_per_unit = float(tpu)
        po.padding = 2
        po.bruteForce = False
        po.rotate_charts = True
        co = xatlas.ChartOptions()
        atlas.generate(co, po)
        if atlas.atlas_count == 1:
            return atlas, tpu == density
    return atlas, False


def _results(group: list[MeshData], atlas, fitted: bool, resolution: int, atlas_id: str | None) -> list[UVResult]:
    out = []
    for i, m in enumerate(group):
        vm, idx, uv = atlas.get_mesh(i)
        vm = vm.astype(np.int64)
        pts = np.asarray(m.points, dtype=np.float64)[vm]
        tris = idx.reshape(-1, 3).astype(np.int32)
        uv = np.clip(uv.astype(np.float32), 0.0, 1.0)
        a3, a2 = _tri_areas(pts, tris, 3).sum(), _tri_areas(uv, tris, 2).sum()
        dens = float(np.sqrt(a2 / a3) * resolution) if a3 > 0 and a2 > 0 else 0.0
        sm, sx = _stretch(pts, tris, uv)
        out.append(UVResult(m.node_id, pts, tris, uv, vm, dens, sm, sx, resolution, atlas_id, fitted))
    return out


def unwrap_uvs(mesh: MeshData, texel_density_px_per_m: float, resolution: int = 2048) -> UVResult:
    atlas, fitted = _pack([mesh], texel_density_px_per_m, resolution)
    return _results([mesh], atlas, fitted, resolution, mesh.material_id)[0]


def unwrap_scene(meshes: list[MeshData], budget: EngineBudget, resolution: int = 2048) -> list[UVResult]:
    """One atlas per material id; results are returned in input order."""
    groups: dict[str | None, list[int]] = {}
    for i, m in enumerate(meshes):
        groups.setdefault(m.material_id, []).append(i)
    res: list[UVResult | None] = [None] * len(meshes)
    for mat, ids in groups.items():
        group = [meshes[i] for i in ids]
        atlas, fitted = _pack(group, budget.texel_density_px_per_m, resolution)
        for i, r in zip(ids, _results(group, atlas, fitted, resolution, mat)):
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
) -> Path:
    """Writes a GLB. Our world is Z-up; glTF is Y-up, so (x,y,z) -> (x,z,-y) unless y_up=False."""
    import trimesh
    from trimesh.visual import TextureVisuals

    path = Path(path)
    scene = trimesh.Scene()
    for i, it in enumerate(items):
        uv = getattr(it, "uvs", None)
        p = np.asarray(it.points, dtype=np.float64)
        if y_up:
            p = np.stack([p[:, 0], p[:, 2], -p[:, 1]], axis=1)
        tm = trimesh.Trimesh(p, np.asarray(it.triangles), process=False)
        if uv is not None:
            tm.visual = TextureVisuals(uv=np.asarray(uv, dtype=np.float64))
        name = names[i] if names else getattr(it, "name", None) or getattr(it, "node_id", None) or f"mesh_{i}"
        scene.add_geometry(tm, node_name=name, geom_name=name)
    path.write_bytes(scene.export(file_type="glb"))
    return path


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
