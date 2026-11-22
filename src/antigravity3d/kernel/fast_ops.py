"""Deterministic in-process mesh kernels: Tier-1 QA, UVs, LODs, colliders, export.

No LLM tokens, no Blender. Native backends actually used:
  * QA          : numpy (edge-table topology, winding-number interpenetration probe)
  * UVs         : exact developable charts for IR primitives (unfolded here); xatlas computes the charts
                  of every other mesh; all charts are placed by the same skyline rectangle packer
  * LODs        : meshoptimizer (simplify / simplify_with_attributes in absolute-error mode, vertex
                  cache, overdraw, vertex fetch); levels from lod_diff's screen-space error budget,
                  each verified by an exact sampled surface distance and a silhouette check
  * colliders   : CoACD + scipy ConvexHull (fast path)
  * glTF        : kernel/gltf_writer.py (pygltflib structures, written here)
  * KTX2        : external ``ktx`` / ``toktx`` CLI only (never faked)

FALLBACKS (see ``FALLBACKS``): trimesh's quadric simplifier needs the optional
``fast_simplification`` package, which is not installed here, so when
``meshopt.simplify`` raises we fall back to ``meshopt.simplify_sloppy`` (same native
library) rather than trimesh. Nothing else deviates from the intended library.
"""

from __future__ import annotations

import logging
import math
import shutil
import subprocess
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from collections.abc import Collection, Iterable, Mapping
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
    if not len(points):  # a part absent at a LOD level has no vertices
        return np.zeros(0, np.int64)
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
    euler: int = 2      # vertices - edges + faces of the welded mesh: 2 per sphere-like body, so more than 2 means several bodies

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
    euler = int(np.count_nonzero(np.bincount(t.ravel(), minlength=n))) - len(ukey) + len(t)
    return _Topo(boundary, nonmanifold, inconsistent, degenerate, duplicates, vol, euler)


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


# Same-facing faces of two parts closer than this to one plane, overlapping in area, fight in the depth buffer
# (EL4CTEO/blender-skills qa_check.py uses 1 mm too). See kernel/coplanar.py for the exact overlap test.
ZFIGHT_EPS = 0.001


# Two parts touch when their surfaces come within this distance (EL4CTEO/blender-skills qa_check.py: 2 mm). Measured on
# the saved authored stages (88 files, 15 distinct assets): every part that rests on another is within 0.1 micrometre
# of it, except the 50 micrometre gaps of the prop templates' rounded sizes (fixed in tactics/authoring.py); no part
# there has a surface gap between 0.1 mm and 5 cm. A gap of a couple of millimetres is visible at close range and
# is where light leaks between parts, so it is the smallest value that is not noise. Must not exceed ``touch_eps``,
# the box window the candidate pairs are swept with. Not handled: two curved parts that touch in their ideal shapes
# and miss only by their facets (two 16-segment spheres of radius 0.1 m can be 3.8 mm apart at a contact off the
# axes); none of the saved stages has one, so the tolerance does not add the parts' facet sag.
TOUCH_TOL = 0.002


def closed_meshes_required(budget: EngineBudget) -> bool:
    """Whether an open (not watertight) authored mesh fails the asset on the target engine: the engine rule table's
    ``closed_render_meshes`` (validator.ENGINE_RULES) unless the budget says otherwise (``require_manifold`` set).
    An engine the table does not know is held to the stricter reading."""
    if budget.require_manifold is not None:
        return budget.require_manifold
    from antigravity3d.kernel.validator import ENGINE_RULES

    rules = ENGINE_RULES.get(budget.target_engine)
    return True if rules is None else rules.closed_render_meshes


def tier1_qa(
    meshes: list[MeshData], budget: EngineBudget, ground_tol: float = 0.01, touch_eps: float = 0.005,
    zfight_eps: float = ZFIGHT_EPS, touch_tol: float = TOUCH_TOL,
) -> Tier1Report:
    t0 = time.perf_counter()
    issues: list[Issue] = []

    def add(code: str, sev: Severity, node: str | None, detail: str) -> None:
        issues.append(Issue(code, sev, node, detail))

    open_sev: Severity = "critical" if closed_meshes_required(budget) else "warning"  # an open mesh: per engine
    manifold_sev: Severity = "warning" if budget.require_manifold is False else "critical"  # an edge of 3+ faces
    valid: list[tuple[MeshData, bool, np.ndarray, np.ndarray]] = []  # mesh, closed, lo, hi
    volumes: list[float] = []
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
            add("not_watertight", open_sev, m.node_id, f"{topo.boundary} boundary edges: an open mesh shows its hole from behind (faces seen from "
                f"behind are culled) unless the material is double-sided; Roblox requires watertight meshes")
        if topo.inconsistent:
            add("inconsistent_winding", "critical", m.node_id, f"{topo.inconsistent} edges with flipped winding")
        elif topo.closed and topo.volume < -1e-12:
            add("inverted_normals", "critical", m.node_id, f"signed volume {topo.volume:.4g} < 0")
        elif not topo.nonmanifold and (topo.euler != 2 or not topo.closed):
            # one volume for several bodies hides an inside-out one among right-way-round ones; a sphere-like single
            # body has Euler characteristic 2 (most parts: skipped here, no component search)
            inside_out = _inverted_bodies(pts, tri, topo.closed)
            if inside_out:
                k, n_bodies, vol = inside_out[0]
                add("inverted_normals", "critical", m.node_id,
                    f"body {k + 1} of {n_bodies} has signed volume {vol:.4g} < 0 ({len(inside_out)} inside out)")
        valid.append((m, topo.closed and topo.volume > 0, pts.min(axis=0), pts.max(axis=0)))
        volumes.append(topo.volume)

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
        bounds_lo, bounds_hi = np.asarray([v[2] for v in valid]), np.asarray([v[3] for v in valid])
        # Union-find over the parts and the ground (index n): a part is supported when it is joined to the ground, by
        # resting on it or through a chain of touching parts. A pair whose parts are already in one group is not tested.
        n = len(valid)
        parent = list(range(n + 1))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for i in np.flatnonzero(bounds_lo[:, 2] <= ground_tol):  # resting on the ground (or sunk)
            parent[find(int(i))] = n
        gap = _PartGap(valid, volumes, touch_tol)
        near = []
        for i, j in _candidate_pairs(bounds_lo, bounds_hi, touch_eps):
            n_candidates += 1
            near.append((i, j))
            ri, rj = find(i), find(j)
            if ri != rj and gap.touch(i, j):
                parent[ri if ri != n else rj] = rj if ri != n else ri
            _check_pair(valid[i], valid[j], add)
        if n > 1:
            groups: dict[int, list[int]] = {}
            for i in range(n):
                groups.setdefault(find(i), []).append(i)
            loose = [g for root, g in groups.items() if root != find(n)]
            if find(n) == n and not any(find(i) == n for i in range(n)) and loose:
                # nothing rests on the ground (a hanging asset): the group with the most surface is its body
                loose.remove(max(loose, key=lambda g: sum(_surface_area(valid[i][0]) for i in g)))
            for g in sorted(loose, key=lambda g: min(valid[i][0].node_id for i in g)):
                names = sorted(valid[i][0].node_id for i in g)
                if len(g) == 1:
                    i = g[0]
                    inside = gap.enclosed_by(i, bounds_lo, bounds_hi)
                    if inside is not None:  # inside another part, touching none: a hidden part (eyes, internals), not a hovering one
                        other = valid[inside][0].node_id
                        add("enclosed_part", "warning", names[0],
                            f"inside {other}, surfaces more than {touch_tol * 1e3:g} mm from every part: its triangles are "
                            f"never seen while {other} is opaque, and it floats if {other} is see-through")
                        issues[:] = [x for x in issues if not (x.code == "interpenetration"
                                                               and x.detail.split(":")[0] in (f"{names[0]}/{other}", f"{other}/{names[0]}"))]
                    else:
                        add("floating_part", "warning", names[0], gap.describe(g, bounds_lo, bounds_hi))
                else:
                    add("floating_group", "warning", names[0], f"{', '.join(names)} touch each other and nothing else; "
                        + gap.describe(g, bounds_lo, bounds_hi))
            _check_zfight([v[0] for v in valid], zfight_eps, add, (bounds_lo, bounds_hi), near if zfight_eps <= touch_eps else None,
                          [v[1] for v in valid], budget)

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


def _inverted_bodies(pts: np.ndarray, tri: np.ndarray, closed: bool) -> list[tuple[int, int, float]]:
    """(index, number of bodies, signed volume) of every closed body of a mesh that holds several and is wound inside
    out (negative volume); empty for a single body (its own volume is the test) or when none is."""
    bodies = _closed_bodies(pts, tri)
    if bodies is None:
        return []
    out = []
    for k, (bp, bt) in enumerate(bodies):
        if closed:  # a closed mesh has only closed bodies
            c = bp[bt]
            vol = float(np.einsum("ij,ij->i", c[:, 0], np.cross(c[:, 1], c[:, 2])).sum() / 6.0)
        else:
            topo = _topology(bp, bt)
            if not topo.closed:
                continue
            vol = topo.volume
        if vol < -1e-12:
            out.append((k, len(bodies), vol))
    return out


class _PartGap:
    """Do two parts of one asset touch (their surfaces within ``tol``)? Answered by the cheapest exact means: the
    distance of the boxes when it is over ``tol``; a closed form when both parts are axis-aligned boxes (most
    parts); else the triangle tests of kernel/surface_gap on the triangles near the other part. Per-part data is
    built on first use, so a part that touches nothing near it costs nothing."""

    def __init__(self, valid, volumes: list[float], tol: float) -> None:
        self.valid, self.volumes, self.tol = valid, volumes, tol
        self.diag = float(np.linalg.norm(np.max([v[3] for v in valid], axis=0) - np.min([v[2] for v in valid], axis=0)))
        self._box: dict[int, bool] = {}
        self._tris: dict[int, object] = {}

    def is_box(self, i: int) -> bool:
        if i not in self._box:
            from antigravity3d.kernel import surface_gap

            m, closed, lo, hi = self.valid[i]
            self._box[i] = bool(closed and surface_gap.is_axis_box(np.asarray(m.points, np.float64), lo, hi, self.volumes[i]))
        return self._box[i]

    def tris(self, i: int):
        if i not in self._tris:
            from antigravity3d.kernel import surface_gap

            m = self.valid[i][0]
            self._tris[i] = surface_gap.Tris(np.asarray(m.points, np.float64), np.asarray(m.triangles))
        return self._tris[i]

    def touch(self, i: int, j: int) -> bool:
        from antigravity3d.kernel import surface_gap

        (mi, _, li, hi_), (mj, _, lj, hj) = self.valid[i], self.valid[j]
        if (np.maximum(np.maximum(li - hj, lj - hi_), 0.0) ** 2).sum() > self.tol * self.tol:
            return False  # the boxes are farther apart than that
        bi, bj = self.is_box(i), self.is_box(j)
        if bi and bj:
            return surface_gap.box_distance(li, hi_, lj, hj) <= self.tol
        # a corner of one part on or at the surface of the other: parts that rest on each other (nearly all of them)
        for a, b, box in ((i, j, bj), (j, i, bi)):
            pts = np.asarray(self.valid[a][0].points, np.float64)
            if box:
                if (surface_gap.point_box_distance(pts, self.valid[b][2], self.valid[b][3]) <= self.tol).any():
                    return True
            elif surface_gap.corner_touches(pts, self.tris(b), self.valid[b][2], self.valid[b][3], self.tol):
                return True
        return surface_gap.within(self.tris(i), li, hi_, self.tris(j), lj, hj, self.tol)

    def enclosed_by(self, i: int, lo: np.ndarray, hi: np.ndarray) -> int | None:
        """A closed part whose volume holds all of part i (given that i touches nothing: it is then inside or outside)."""
        inner = np.asarray(self.valid[i][0].points, np.float64)
        pool = np.concatenate([inner, inner[np.asarray(self.valid[i][0].triangles)].mean(axis=1)])
        if len(pool) > 96:
            pool = pool[np.linspace(0, len(pool) - 1, 96).astype(int)]
        for j in np.flatnonzero(np.all(lo <= lo[i], axis=1) & np.all(hi >= hi[i], axis=1)):
            if j == i or not self.valid[j][1]:
                continue
            m = self.valid[j][0]
            if _winding_inside(pool, np.asarray(m.points, np.float64)[np.asarray(m.triangles)]).all():
                return int(j)
        return None

    def describe(self, group: list[int], lo: np.ndarray, hi: np.ndarray) -> str:
        """Why a part or group floats, for whoever repairs it: the nearest part outside it, the surface distance to it
        (from the vertices of each to the other's surface: exact for those points), where it lies and the lowest z."""
        inside = set(group)
        others = [j for j in range(len(self.valid)) if j not in inside]
        gaps = np.array([[float(np.linalg.norm(np.maximum(np.maximum(lo[i] - hi[j], lo[j] - hi[i]), 0.0))) for j in others]
                         for i in group])
        a, k = np.unravel_index(int(np.argmin(gaps)), gaps.shape)
        i, j = group[a], others[k]
        d = np.inf
        for x, y in ((i, j), (j, i)):
            mx, my = self.valid[x][0], self.valid[y][0]
            d = min(d, float(_SurfaceIndex(my.points, my.triangles).distances(np.asarray(mx.points, np.float64), 0.0).min()))
        sep = np.maximum(lo[i] - hi[j], lo[j] - hi[i])  # per axis: positive when the boxes are apart on it
        axis = int(np.argmax(sep)) if sep.max() > 0 else int(np.argmax(np.abs((lo[j] + hi[j]) - (lo[i] + hi[i]))))
        sign = "+" if (lo[j][axis] + hi[j][axis]) >= (lo[i][axis] + hi[i][axis]) else "-"
        where = {"+z": "above it", "-z": "below it"}.get(sign + "xyz"[axis], f"beside it ({sign}{'xyz'[axis]})")
        return (f"surface {d * 1e3:.1f} mm from {self.valid[j][0].node_id}, the nearest part, which lies {where} "
                f"(touch tolerance {self.tol * 1e3:g} mm); min z {min(lo[m][2] for m in group):.3f}")


def _surface_area(m: MeshData) -> float:
    c = np.asarray(m.points, np.float64)[np.asarray(m.triangles)]
    u, v = c[:, 1] - c[:, 0], c[:, 2] - c[:, 0]
    n = np.stack([u[:, 1] * v[:, 2] - u[:, 2] * v[:, 1], u[:, 2] * v[:, 0] - u[:, 0] * v[:, 2], u[:, 0] * v[:, 1] - u[:, 1] * v[:, 0]], axis=1)
    return float(np.sqrt(np.einsum("ij,ij->i", n, n)).sum() / 2)


def shipped_masks(meshes: list[MeshData], budget: EngineBudget, only: set[int] | None = None) -> list[np.ndarray] | None:
    """Per mesh, the triangles the exporter leaves out of the render meshes for this budget (hidden contact faces,
    ``hidden_contact_faces`` under ``contact_faces_removable``: the export's own rule), or None when it leaves out
    none or when opacity is not known (meshes not read from a managed stage: ``MeshData.opaque`` is None).

    ``only``: the masks are needed for these meshes only. A part's hidden triangles depend on the parts whose boxes
    touch it (nothing farther can cover it), so it is decided on those parts alone, with the same result; the
    other meshes get empty masks."""
    if not meshes or any(m.opaque is None for m in meshes):
        return None
    opaque = frozenset(m.material_id for m in meshes if m.opaque)
    if not contact_faces_removable(budget, opaque):
        return None
    keep = list(range(len(meshes)))
    if only is not None:
        lo = np.array([np.asarray(m.points).min(axis=0) if len(m.points) else np.full(3, np.inf) for m in meshes])
        hi = np.array([np.asarray(m.points).max(axis=0) if len(m.points) else np.full(3, -np.inf) for m in meshes])
        pad = 1e-4 * max(1.0, float(np.linalg.norm(np.nanmax(np.where(np.isfinite(hi), hi, np.nan), axis=0)
                                                   - np.nanmin(np.where(np.isfinite(lo), lo, np.nan), axis=0))))
        sel = sorted(only)
        near = np.zeros(len(meshes), bool)
        near[sel] = True
        for k in sel:
            near |= np.all((lo <= hi[k] + pad) & (hi >= lo[k] - pad), axis=1)
        keep = np.flatnonzero(near).tolist()
    sub = hidden_contact_faces([(meshes[k].node_id, meshes[k].points, meshes[k].triangles,
                                 meshes[k].material_id if meshes[k].opaque else None) for k in keep], opaque)
    out = [np.zeros(len(m.triangles), bool) for m in meshes]
    for k, mask in zip(keep, sub):
        out[k] = mask
    return out


def _check_zfight(meshes: list[MeshData], eps: float, add, bounds=None, pairs=None, solid=None, budget=None) -> None:
    """Same-facing faces of two parts within ``eps`` of one plane whose areas overlap (more than a sliver
    ``eps`` wide; faces that only share an edge do not count): two surfaces at one depth, drawn in whichever
    order the depth buffer's rounding picks, so they flicker in every engine. A pair of faces that both do not ship
    (contact faces the exporter leaves out for this budget, ``shipped_masks``) is not drawn, so it is not counted;
    where they ship (closed-mesh engines, contact faces kept by request) it is."""
    from antigravity3d.kernel import coplanar

    parts = [coplanar.Part(m.node_id, np.asarray(m.points, np.float64), np.asarray(m.triangles)) for m in meshes]
    found = coplanar.same_facing_overlaps(parts, eps, bounds=bounds, pairs=pairs, solid=solid)
    hidden = shipped_masks(meshes, budget, only={x for i, j, _ in found for x in (i, j)}) if found and budget is not None else None
    if hidden is not None:  # the same pairs, counting only triangle pairs of which at least one ships
        found = coplanar.same_facing_overlaps(parts, eps, bounds=bounds, pairs=[(i, j) for i, j, _ in found], solid=solid, hidden=hidden)
    for i, j, area in found:
        add("z_fighting", "warning", meshes[i].node_id,
            f"{meshes[i].node_id}/{meshes[j].node_id}: {area * 1e4:.3g} cm^2 of same-facing faces within {eps * 1e3:g} mm of one plane")


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
        # Probes step a micrometre-scale distance into their own part, so a probe lying exactly on the other part's
        # surface (two parts in flush contact) reads as outside it: on a surface the winding number is 0.5 on a face but
        # undefined on an edge or vertex, where it can exceed the 0.9 threshold. Measured: an L-section corner guard
        # whose inner faces sit flush on a box read "~19% of probe points inside" for two of four identical mirrored
        # guards, and 0% with the step. A real overlap deeper than the step is still found.
        fn = np.cross(p[t[:, 1]] - p[t[:, 0]], p[t[:, 2]] - p[t[:, 0]])
        vn = np.zeros_like(p)
        np.add.at(vn, t.reshape(-1), np.repeat(fn, 3, axis=0))
        unit = lambda v: v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-30)  # noqa: E731
        step = 1e-6 * max(float(np.linalg.norm(np.ptp(p, axis=0))), 1e-9)
        pool = np.concatenate([p - step * unit(vn), p[t].mean(axis=1) - step * unit(fn)])
        if len(pool) > 96:
            pool = pool[np.linspace(0, len(pool) - 1, 96).astype(int)]
        inside = _winding_inside(pool, np.asarray(dst.points, dtype=np.float64)[np.asarray(dst.triangles)])
        hit = max(hit, float(inside.mean()))
    if hit > 0:
        add("interpenetration", "warning", ma.node_id, f"{ma.node_id}/{mb.node_id}: ~{hit:.0%} of probe points inside")


# --------------------------------------------------------------------------- UVs

UV_PADDING_PX = 2           # gutter on each side of a chart, in atlas texels (charts are 2x this apart)
# Computed (xatlas) charts may each cover at most this much *smooth* Gaussian curvature (steradians). xatlas's
# default charts are unbounded, and its chart growing costs superlinearly in chart size: 0.30 / 0.83 / 8.2 s on
# UV spheres of 1.9k / 10k / 50k triangles, against 0.27 / 0.54 / 2.4 s with charts capped at 1/32 of a sphere
# (4 pi / 32 sr each). The cap was set for xatlas's orthographic flattening (area distortion 0.015 at the cap,
# 0.06-0.09 unbounded); charts are now re-flattened (_flatten), and the cap was re-measured with that, through
# unwrap_uvs at 1024 px/m into 2048, on spheres of 448 to 49,920 triangles, a torus, a blob, a sheet and shells:
#   - area distortion grows with the cap: 0.0006-0.0027 at 0.5x, 0.0023-0.0059 at 1x, 0.0027-0.0082 at 1.5x,
#     0.0037-0.0116 at 2x, 0.015-0.043 unbounded;
#   - density does not follow it (rectangle packing noise, -6 to +10 % against 1x with no trend; no setting is at
#     least as dense as 1x on every subject);
#   - 1.5x / 2x save 4 to 20 % of seam vertices, 0.5x costs 5 to 39 % more;
#   - the 49,920-triangle sphere takes 2 to 2.5x as long at 1.5x-2x and 5.6x unbounded (xatlas chart growing).
# So the cap stays at 1/32 of a sphere. Curvature concentrated on sharp edges
# (box corners, cylinder rims: dihedral above CREASE) is not counted: those edges are chart seams anyway, and
# capping such meshes only fragments flat charts (measured: a 32-sided cylinder split into one chart per side
# quad, every rim vertex in three charts, which then locked the LOD simplifier).
UV_CHART_CURVATURE_SR = 4.0 * np.pi / 32
UV_SMOOTH_CREASE_DEG = 40.0
UV_MIN_RECT_FILL = 0.6      # charts filling less of their bounding rectangle are cut (see _charted, _slabs)
UV_CHART_PASS_PX = 64.0     # xatlas computes charts in an atlas about this many texels across (see _computed_charts)


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
    # (M,) bool: triangles that are not shipped (hidden contact faces, see kernel/coplanar.py), None = all shipped.
    # They stay here so the working stage keeps closed solids; charts made only of them are not packed into the
    # atlas (their uvs lie right of it, u > 1), and density and stretch above describe the shipped triangles.
    hidden: np.ndarray | None = None
    # True: this atlas is the one several (or all) opaque materials share, sized from need; the package bakes the maps of
    # its materials into one texture set at its side (package._compose_atlas). False: one material's own 2048 px atlas.
    shared_atlas: bool = False
    # Atlas height in px when it differs from ``resolution`` (the width): a two-to-one atlas (ATLAS_SIZES). None: square.
    height: int | None = None
    # Shared atlas only: atlas px one repeat of a tileable material map spans (SWATCH_SPAN_PX at the target density).
    # The stage samples each material's own map with this repeat (StageManager.author_uvs) and the package bakes the
    # maps with it (package._compose_atlas), so the stage looks as the package ships. None: the map spans the atlas.
    repeat_px: int | None = None
    # Shared atlas sized from need: the sizes tried, smallest first, as (width, height, px/m the packing reached).
    size_trials: tuple = ()
    # Shared atlas of an export given the shipped layout (unwrap_scene ``keep``): "kept: N of M charts where they shipped"
    # or "repacked: <why>". None: no shipped layout was given.
    layout: str | None = None

    @property
    def size(self) -> tuple[int, int]:
        """(width, height) of the atlas in px."""
        return int(self.resolution), int(self.height or self.resolution)

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
    chart: np.ndarray | None = None  # (M,) chart id per triangle
    frames: list | None = None    # per chart: minimum-area rectangle (filled by _chart_frames)


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
    The skyline is kept as x-sorted parallel lists so the segments under a candidate position are found by
    bisection and their highest top by a slice maximum (this is called a dozen or more times per atlas by the
    density search, with hundreds of charts on computed meshes).
    """
    from bisect import bisect_left

    sizes = [(float(a), float(b)) for a, b in np.asarray(sizes, np.float64).reshape(-1, 2)]
    order = sorted(range(len(sizes)), key=lambda i: (-max(sizes[i]), -sizes[i][0] * sizes[i][1], i))
    sx, sy, sw = [0.0], [0.0], [float(width)]  # (x, y, w) segments covering [0, width)
    out = np.zeros((len(sizes), 3))
    wlim, hlim = width + 1e-9, height + 1e-9
    for i in order:
        best = None
        for turned in (0, 1):
            w, h = (sizes[i][1], sizes[i][0]) if turned else sizes[i]
            if w > width or h > height:
                continue
            for j in range(len(sx)):
                x = sx[j]
                if x + w > wlim:
                    break
                e = bisect_left(sx, x + w - 1e-9, j)  # segments j .. e-1 lie under [x, x + w)
                y = max(max(sy[j:e]), 0.0) if e > j else 0.0
                top = y + h
                if top <= hlim and (best is None or top < best[0] or (top == best[0] and x < best[1])):
                    best = (top, x, y, w, h, turned)
        if best is None:
            return None
        _, x, y, w, h, turned = best
        out[i] = (x, y, turned)
        right = x + w
        new = []
        for a, b, c in zip(sx, sy, sw):
            if a + c <= x + 1e-9 or a >= right - 1e-9:
                new.append((a, b, c))
                continue
            if a < x:
                new.append((a, b, x - a))
            if a + c > right:
                new.append((right, b, a + c - right))
        new.append((x, y + h, w))
        new = sorted(seg for seg in new if seg[2] > 1e-9)  # zero-width slivers would break the x order
        merged = [new[0]]
        for seg in new[1:]:
            px, py, pw = merged[-1]
            if abs(py - seg[1]) < 1e-9 and abs(px + pw - seg[0]) < 1e-9:
                merged[-1] = (px, py, pw + seg[2])
            else:
                merged.append(seg)
        sx = [s[0] for s in merged]
        sy = [s[1] for s in merged]
        sw = [s[2] for s in merged]
    return out


def _strip_search(ext: np.ndarray, pad: float, side: float, hi: float, tol: float = 1e-4,
                  height: float | None = None) -> tuple[float, np.ndarray]:
    """Largest density (to ``tol``, below ``hi``) at which the rectangles ``ext * d + pad`` fit a ``side`` square, or
    a ``side`` wide, ``height`` high rectangle.

    The skyline packer never places a rectangle higher than it must, so packing into an unbounded strip of the
    same width gives the same placements as packing into the square, up to the first one that does not fit: the
    layout fits exactly when the strip height it needs is at most ``side``. That height (roughly proportional to
    d squared) steers the next density, instead of only a fits / does not fit bit: a few steps bracket the
    answer to about 1 %, and bisection finishes it. The packer is not monotone in d, so the largest density that
    fitted is kept. Measured on computed charts that overflow (39 to 530 rectangles): 8 to 11 packings, against
    16 or 17 for the bisection from hi / 8."""
    best_d, best_p = 0.0, None
    fail = hi
    d = hi
    limit = side if height is None else height
    for it in range(64):
        s = ext * d + pad
        p = _skyline_pack(s, side, np.inf)
        need = float((p[:, 1] + np.where(p[:, 2] > 0, s[:, 0], s[:, 1])).max()) if p is not None and len(p) else np.inf
        if need <= limit + 1e-9:
            if d > best_d:
                best_d, best_p = d, p
        else:
            fail = min(fail, d)
        if best_p is not None and fail - best_d <= tol * fail:
            break
        guess = d * np.sqrt(limit / need) if np.isfinite(need) and need > 0 else 0.5 * d
        if best_p is None:  # nothing fitted yet: step down, at least a little
            d = min(guess, d * (1 - 1e-3))
        elif it < 4 and best_d < guess < fail:  # stay inside the bracket, away from its ends
            span = fail - best_d
            d = min(max(guess, best_d + 0.05 * span), fail - 0.05 * span)
        else:
            d = 0.5 * (best_d + fail)
    return float(best_d), best_p  # type: ignore[return-value]


def _chart_frames(c: _Charted) -> list[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]]:
    """(vertex ids, rotation, min corner, (w, h)) of each chart's minimum-area rectangle, in chart id order."""
    if c.frames is None:
        cid = _vertex_charts(c.tris, c.chart, len(c.uv_m))
        order = np.argsort(cid, kind="stable")
        ks, starts = np.unique(cid[order], return_index=True)
        used = np.zeros(len(c.uv_m), bool)
        used[np.asarray(c.tris).reshape(-1)] = True
        c.frames = []
        for k, v in zip(ks, np.split(order, starts[1:])):
            v = v[used[v]]
            if len(v):
                c.frames.append((v, *_min_area_frame(c.uv_m[v])))
    return c.frames


def _wh(resolution: int | tuple[int, int]) -> tuple[int, int]:
    """(width, height) of an atlas given as one side (square) or as (width, height)."""
    return (int(resolution), int(resolution)) if np.isscalar(resolution) else (int(resolution[0]), int(resolution[1]))


def _pack_analytic(charted: list[_Charted], density: float, resolution: int | tuple[int, int],
                   hidden: Sequence[np.ndarray | None] | None = None) -> tuple[list[np.ndarray], float]:
    """Packs charts (exact ones, and computed ones in metres) as their minimum-area rectangles into one atlas,
    ``resolution`` px square or (width, height) px. Texels are square either way: u is divided by the width and v by
    the height, so a chart keeps one density along both axes.

    Each chart keeps UV_PADDING_PX texels of private gutter on every side, so charts are at least
    2 * UV_PADDING_PX texels apart. The requested density is used when everything fits; otherwise the largest
    density that fits is searched (the packer is cheap, so this costs milliseconds, unlike re-running a raster
    packer). Returns per-mesh UVs in [0, 1] and the density used.

    Groups of exact charts only keep the plain bisection they always had (same layout for the same input).
    Groups with computed charts have hundreds of charts, where each packing costs tens of milliseconds, so
    there the packer is run without a height limit and the height it needs steers the next density (see
    ``_strip_search``). Both searches skip packings that cannot succeed: rectangles whose total area exceeds
    the atlas, or one rectangle wider than it.

    ``hidden`` (per mesh, a triangle mask or None): a chart all of whose triangles are hidden takes no atlas space;
    its vertices are laid out isometrically at the same density right of the atlas (u > 1), so the mesh keeps a
    complete, undistorted layout for the stages that still see those triangles (the working stage, the LOD
    simplifier) while nothing that ships samples it.
    """
    frames, phantom = [], []
    for mi, c in enumerate(charted):
        h = None if hidden is None else hidden[mi]
        seen = None
        if h is not None and h.any():
            seen = np.zeros(len(c.uv_m), bool)
            seen[np.asarray(c.tris, np.int64)[~h].reshape(-1)] = True
        for f in _chart_frames(c):
            (frames if seen is None or seen[f[0]].any() else phantom).append((mi, *f))
    if not frames:  # every chart hidden: nothing to pack (the caller ships nothing of these meshes)
        frames, phantom = phantom, []
    ext = np.array([f[4] for f in frames], np.float64).reshape(-1, 2)
    pad = 2.0 * UV_PADDING_PX
    edge = 1.0  # keep half a texel clear of the atlas border, plus rounding
    W, H = _wh(resolution)
    sw, sh = W - edge, H - edge
    area = float(ext.prod(axis=1).sum())
    # above hi the rectangles cannot fit (each may be turned: its longer side within the longer atlas side, its shorter
    # within the shorter), so no packing is needed to know it
    hi = min(float(density), max(sw, sh) / max(float(ext.max()), 1e-12), min(sw, sh) / max(float(ext.min(axis=1).max()), 1e-12),
             np.sqrt(sw * sh / max(area, 1e-12)))

    def fits(d: float) -> np.ndarray | None:
        return _skyline_pack(ext * d + pad, sw, sh)

    d = float(density)
    place = fits(d) if d <= hi else None
    if place is None and any(c.kind == "computed" for c in charted):
        d, place = _strip_search(ext, pad, sw, hi, height=sh)
    elif place is None:
        lo = hi / 8
        place = fits(lo)
        while place is None and lo > 1e-6:
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
    place = list(place) + [(W + pad, 0.0, False)] * len(phantom)
    for (mi, v, R, lo_, wh), (x, y, turned) in zip(frames + phantom, place):
        q = (charted[mi].uv_m[v] @ R.T - lo_) * d  # chart in px, min corner at the origin
        if turned:  # 90 degrees counter-clockwise keeps the winding: (u, v) -> (h - v, u)
            q = np.stack([wh[1] * d - q[:, 1], q[:, 0]], axis=1)
        out[mi][v] = (q + [x + pad / 2 + edge / 2, y + pad / 2 + edge / 2]) / [W, H]
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


def _xatlas_options(tpu: float, *, surface_area: float = 0.0, smooth_curvature: float = 0.0):
    import xatlas

    co = xatlas.ChartOptions()
    if surface_area > 0 and smooth_curvature > UV_CHART_CURVATURE_SR:
        co.max_chart_area = float(surface_area * UV_CHART_CURVATURE_SR / smooth_curvature)
    po = xatlas.PackOptions()
    po.resolution = 0
    po.texels_per_unit = float(tpu)
    po.padding = 0          # the layout is thrown away: only the charts and their flattening are kept
    po.bilinear = False
    po.bruteForce = False
    return co, po


def _vertex_charts(tris: np.ndarray, chart: np.ndarray, nverts: int) -> np.ndarray:
    vc = np.zeros(nverts, np.int64)
    vc[np.asarray(tris, np.int64).reshape(-1)] = np.repeat(chart, 3)
    return vc


def _split_shared(vmap: np.ndarray, tris: np.ndarray, uv: np.ndarray, chart: np.ndarray):
    """Gives every chart its own copy of a vertex it shares with another chart (same uv)."""
    t = np.asarray(tris, np.int64)
    corner = np.repeat(np.asarray(chart, np.int64), 3)
    if (_vertex_charts(t, chart, len(uv))[t.reshape(-1)] == corner).all():
        return vmap, t, uv
    nc = int(corner.max()) + 1
    u, inv = np.unique(t.reshape(-1) * nc + corner, return_inverse=True)
    return vmap[u // nc], inv.reshape(-1, 3), uv[u // nc]


def _metric_charts(p3: np.ndarray, tris: np.ndarray, uv: np.ndarray, chart: np.ndarray) -> np.ndarray:
    """xatlas chart layouts back to their own flattening, in metres.

    xatlas rescales each chart along the atlas axes so that its extents are whole texels (measured: the charts
    of a sphere packed at 64 px/m all come out 13 x 27, 20 x 13 ... texels to within 1e-5); at a low texel
    density that per-axis rescale is several percent and shows up as area and angle distortion that the
    flattening does not have. The per-chart u scale that minimises the conformal energy
    sum_t area_t |s rot90(grad u) - grad v|^2  (closed form) undoes it: exactly for planar and LSCM charts, and
    for xatlas' orthographic-projection charts it picks the least-sheared aspect. Each chart is then scaled to
    its 3D area, so every chart has the same texel density. Measured on UV spheres, a torus, a blob, a sheet,
    shells and a concave prism: the result does not depend on the density xatlas packed at (32 to 1024 px/m),
    area distortion is at or below what xatlas returns at 1024 px/m, and angle distortion lower or within 0.0005."""
    t = np.asarray(tris, np.int64)
    p, q = p3[t], uv[t]
    e1, e2 = p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]
    n = np.cross(e1, e2)
    a3 = 0.5 * np.linalg.norm(n, axis=1)
    l1 = np.linalg.norm(e1, axis=1)
    ok = (a3 > 1e-14) & (l1 > 0)
    x = e1 / np.maximum(l1, 1e-300)[:, None]
    y = np.cross(n, x)
    y /= np.maximum(np.linalg.norm(y, axis=1), 1e-300)[:, None]
    P = np.zeros((len(t), 2, 2))
    P[:, 0, 0] = l1
    P[:, 1, 0], P[:, 1, 1] = (e2 * x).sum(1), (e2 * y).sum(1)
    P[~ok] = np.eye(2)
    J = np.linalg.solve(P, np.stack([q[:, 1] - q[:, 0], q[:, 2] - q[:, 0]], 1))  # J[:, :, k]: gradient of uv[k]
    gu, gv = J[:, :, 0], J[:, :, 1]
    rgu = np.stack([-gu[:, 1], gu[:, 0]], 1)
    w = np.where(ok, a3, 0.0)
    nc = int(chart.max()) + 1 if len(chart) else 0
    num = np.bincount(chart, w * (rgu * gv).sum(1), nc)
    den = np.bincount(chart, w * (rgu * rgu).sum(1), nc)
    s = np.abs(num / np.where(den > 0, den, 1.0))
    s = np.where((den > 0) & (s > 0) & np.isfinite(s), s, 1.0)
    vc = _vertex_charts(t, chart, len(uv))
    return _to_surface_area(p3, t, uv * np.stack([s[vc], np.ones(len(uv))], 1), chart)


def _to_surface_area(p3: np.ndarray, tris: np.ndarray, uv: np.ndarray, chart: np.ndarray) -> np.ndarray:
    """Scales each chart about the origin so its uv area equals its 3D area (every chart at the same density)."""
    t = np.asarray(tris, np.int64)
    nc = int(chart.max()) + 1 if len(chart) else 0
    s3 = np.bincount(chart, _tri_areas(p3, t, 3), nc)
    s2 = np.bincount(chart, _tri_areas(uv, t, 2), nc)
    k = np.where((s2 > 0) & (s3 > 0), np.sqrt(s3 / np.where(s2 > 0, s2, 1.0)), 1.0)
    return uv * k[_vertex_charts(t, chart, len(uv))][:, None]


def _tri_frames(p3: np.ndarray, t: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Each triangle's corners in its own orthonormal 2D frame (counter-clockwise about its normal), and its area."""
    p = p3[t]
    e1, e2 = p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]
    n = np.cross(e1, e2)
    a = 0.5 * np.linalg.norm(n, axis=1)
    l1 = np.linalg.norm(e1, axis=1)
    x = e1 / np.maximum(l1, 1e-300)[:, None]
    y = np.cross(n, x)
    y /= np.maximum(np.linalg.norm(y, axis=1), 1e-300)[:, None]
    q = np.zeros((len(t), 3, 2))
    q[:, 1, 0] = l1
    q[:, 2, 0], q[:, 2, 1] = (e2 * x).sum(1), (e2 * y).sum(1)
    return q, a


def _arap(p3: np.ndarray, t: np.ndarray, uv: np.ndarray, chart: np.ndarray, solve: np.ndarray, iters: int = 3) -> np.ndarray:
    """As-rigid-as-possible flattening (local best rotations, global cotangent-Laplacian solve, one vertex pinned per
    chart) of the charts flagged in ``solve``, starting from ``uv`` at surface area; at most ``iters`` global
    solves, fewer when one lowers the energy by less than 1 %. The Laplacian is factorised once (SuperLU, fixed
    column ordering: the same bytes on every run). See _flatten for the iteration count."""
    import scipy.sparse as sp
    from scipy.sparse.linalg import splu

    nv = len(uv)
    q, a = _tri_frames(p3, t)
    on = solve[chart] & (a > 0)
    cot = np.zeros((len(t), 3))
    for k in range(3):
        u1, u2 = q[:, (k + 1) % 3] - q[:, k], q[:, (k + 2) % 3] - q[:, k]
        cot[:, k] = (u1 * u2).sum(1) / np.maximum(np.abs(u1[:, 0] * u2[:, 1] - u1[:, 1] * u2[:, 0]), 1e-300)
    cot[~on] = 0.0
    I, J, W = [], [], []
    for k in range(3):
        i, j, w = t[:, (k + 1) % 3], t[:, (k + 2) % 3], 0.5 * cot[:, k]
        I += [i, j, i, j]
        J += [j, i, i, j]
        W += [-w, -w, w, w]
    L = sp.csr_matrix((np.concatenate(W), (np.concatenate(I), np.concatenate(J))), shape=(nv, nv))
    vc = np.full(nv, -1, np.int64)
    vc[t.reshape(-1)] = np.repeat(chart, 3)
    pin = ~((vc >= 0) & solve[np.maximum(vc, 0)])
    order = np.argsort(vc, kind="stable")
    ks, starts = np.unique(vc[order], return_index=True)
    pin[order[starts][(ks >= 0)]] = True  # first vertex of every chart
    free, held = np.flatnonzero(~pin), np.flatnonzero(pin)
    if not len(free):
        return uv
    lu = splu(L[free][:, free].tocsc(), permc_spec="COLAMD")
    Lp = L[free][:, held]
    x = uv.copy()
    last = np.inf
    for _ in range(iters):
        Q = x[t]
        S = np.zeros((len(t), 2, 2))
        for k in range(3):
            de, du = q[:, (k + 1) % 3] - q[:, (k + 2) % 3], Q[:, (k + 1) % 3] - Q[:, (k + 2) % 3]
            S += cot[:, k, None, None] * de[:, :, None] * du[:, None, :]
        # the rotation [[c, s], [-s, c]] maximising trace(R^T S) (closed form in 2D; never a reflection)
        c, sn = S[:, 0, 0] + S[:, 1, 1], S[:, 0, 1] - S[:, 1, 0]
        r = np.hypot(c, sn)
        c, sn = np.where(r > 0, c / np.where(r > 0, r, 1.0), 1.0), np.where(r > 0, sn / np.where(r > 0, r, 1.0), 0.0)
        R = np.stack([np.stack([c, sn], 1), np.stack([-sn, c], 1)], 1)
        energy, rhs = 0.0, np.zeros((nv, 2))
        for k in range(3):
            i, j = t[:, (k + 1) % 3], t[:, (k + 2) % 3]
            rot = np.einsum("ni,nij->nj", q[:, (k + 1) % 3] - q[:, (k + 2) % 3], R)
            energy += float((cot[:, k] * ((Q[:, (k + 1) % 3] - Q[:, (k + 2) % 3] - rot) ** 2).sum(1)).sum())
            v = 0.5 * cot[:, k, None] * rot
            for d in range(2):
                rhs[:, d] += np.bincount(i, v[:, d], nv) - np.bincount(j, v[:, d], nv)
        if energy > last * 0.99:
            break
        last = energy
        b = rhs[free] - Lp @ x[held]
        x[free] = np.stack([lu.solve(np.ascontiguousarray(b[:, 0])), lu.solve(np.ascontiguousarray(b[:, 1]))], 1)
    return x


def _valid_charts(uv: np.ndarray, t: np.ndarray, chart: np.ndarray) -> np.ndarray:
    """Per chart: no triangle flipped or collapsed, and no two border edges touching or crossing (other than at a
    shared vertex). Together these make a flattening one-to-one: a map without flips whose border does not meet
    itself does not overlap itself (each uv point is covered as often as the border winds around it)."""
    nc = int(chart.max()) + 1 if len(chart) else 0
    q = uv[t]
    signed = (q[:, 1, 0] - q[:, 0, 0]) * (q[:, 2, 1] - q[:, 0, 1]) - (q[:, 1, 1] - q[:, 0, 1]) * (q[:, 2, 0] - q[:, 0, 0])
    ok = np.bincount(chart, signed <= 0, nc) == 0
    e = np.concatenate([t[:, [0, 1]], t[:, [1, 2]], t[:, [2, 0]]])
    ec = np.tile(chart, 3)
    key = np.sort(e, axis=1)
    u, first, n = np.unique(key, axis=0, return_index=True, return_counts=True)
    border, bc = u[n == 1], ec[first[n == 1]]
    order = np.argsort(bc, kind="stable")
    ks, starts = np.unique(bc[order], return_index=True)

    def orient(p, a, b):
        return np.sign((a[:, 0] - p[:, 0]) * (b[:, 1] - p[:, 1]) - (a[:, 1] - p[:, 1]) * (b[:, 0] - p[:, 0]))

    for k, sel in zip(ks.tolist(), np.split(order, starts[1:])):
        if not ok[k] or len(sel) < 4:
            continue
        b = border[sel]
        A, B = uv[b[:, 0]], uv[b[:, 1]]
        lo, hi = np.minimum(A, B), np.maximum(A, B)
        xs = np.argsort(lo[:, 0], kind="stable")  # sweep along u: pairs whose u ranges overlap
        end = np.searchsorted(lo[xs, 0], hi[xs, 0], side="right")  # sorted j in (i, end) start before i ends
        n = np.maximum(end - np.arange(len(xs)) - 1, 0)
        if not n.sum():
            continue
        i = np.repeat(np.arange(len(xs)), n)
        j = i + 1 + (np.arange(int(n.sum())) - np.repeat(np.cumsum(n) - n, n))
        i, j = xs[i], xs[j]
        m = (hi[i, 1] >= lo[j, 1]) & (lo[i, 1] <= hi[j, 1])
        i, j = i[m], j[m]
        share = (b[i, 0] == b[j, 0]) | (b[i, 0] == b[j, 1]) | (b[i, 1] == b[j, 0]) | (b[i, 1] == b[j, 1])
        i, j = i[~share], j[~share]
        o1, o2 = orient(A[i], B[i], A[j]), orient(A[i], B[i], B[j])
        o3, o4 = orient(A[j], B[j], A[i]), orient(A[j], B[j], B[i])
        if ((o1 != o2) & (o3 != o4)).any() or ((o1 == 0) & (o2 == 0)).any():
            ok[k] = False
    return ok


def _flatten(p3: np.ndarray, tris: np.ndarray, uv: np.ndarray, chart: np.ndarray, planar: np.ndarray) -> tuple[np.ndarray, dict]:
    """Re-flattens xatlas' charts (``uv``: its layout at surface area) without changing their borders.

    xatlas flattens most charts by orthographic projection, whose area distortion is the cosine of the tilt: a
    quarter of a cylinder comes out at 0.08 (over the 0.05 budget), a 1/32 sphere cap at 0.015. As-rigid-as-
    possible iterations from that projection remove it: a developable chart (cylinder, cone) converges to its
    isometric unfolding (area and angle distortion 0.0000 after one or two iterations), a doubly curved one to
    near the least distortion it can have. Measured (area / angle distortion, cost at 49,920 triangles), on UV
    spheres of 448 to 49,920 triangles, a torus, a blob, a sheet, shells and tubes:
      - projection: 0.014-0.080 / 0.017-0.11;
      - least-squares conformal map: 0.003-0.012 / 0.002-0.008, 330 ms (its sparse solve has 2x the unknowns
        and 4x the fill of ARAP's);
      - LSCM then ARAP: 0.0018-0.0058 / 0.003-0.009, 880 ms;
      - ARAP from the projection, 3 iterations: 0.0023-0.0059 / 0.0033-0.0089, 213 ms (1 iteration: 0.0027-0.0060
        area, 144 ms; 10: 0.0020-0.0050, 524 ms). Against LSCM + ARAP the largest gap is the torus, 0.0041 / 0.0028.
    So ARAP from the projection, 3 iterations: it is close to the best measured at a quarter of its cost. A chart
    keeps the projection where the result is flipped or overlaps itself (``_valid_charts``; none did on any
    subject measured), and charts that are not one connected piece of non-degenerate triangles keep it too.
    Charts xatlas returned mirrored (about half) are mirrored back first, so every chart is counter-clockwise
    (one tangent handedness). Returns the uvs and how many charts ended which way."""
    t = np.asarray(tris, np.int64)
    nc = int(chart.max()) + 1 if len(chart) else 0
    _, a = _tri_frames(p3, t)
    good = a > 0
    solve = (np.bincount(chart, ~good, nc) == 0) & ~planar  # a planar chart's projection is already exact
    if good.any():  # one connected piece per chart: the solve pins one vertex per chart, a second piece would float
        piece, at = np.unique(_pieces(t[good], chart[good]), return_index=True)
        solve &= np.bincount(chart[good][at], minlength=nc) == 1
    else:
        solve[:] = False
    stats = {"charts": nc, "planar": int(planar.sum()), "projection_kept_degenerate": int(nc - solve.sum() - planar.sum())}
    vc = _vertex_charts(t, chart, len(uv))
    q = uv[t]
    signed = (q[:, 1, 0] - q[:, 0, 0]) * (q[:, 2, 1] - q[:, 0, 1]) - (q[:, 1, 1] - q[:, 0, 1]) * (q[:, 2, 0] - q[:, 0, 0])
    mirrored = np.bincount(chart, signed, nc) < 0
    uv = uv.copy()
    uv[mirrored[vc], 0] *= -1.0
    stats["mirrored_back"] = int(mirrored.sum())
    if not solve.any():
        return uv, stats
    flat = _to_surface_area(p3, t, _arap(p3, t, uv, chart, solve), chart)
    ok = solve & _valid_charts(flat, t, chart)
    stats["projection_kept_invalid"] = int((solve & ~ok).sum())
    return np.where(ok[vc][:, None], flat, uv), stats


def _computed_charts(meshes: Sequence[MeshData]) -> list[_Charted]:
    """xatlas charts (one run over all the meshes, so the curvature bound sees their total), re-flattened by
    ``_flatten``, in metres.

    Only xatlas' chart borders (and its flattening, as the start and fallback) are used; its packing is not
    (``_pack_analytic`` packs these charts with the exact ones). So xatlas packs into a tiny atlas,
    UV_CHART_PASS_PX across: its packing cost grows with the
    texel area it packs (measured on a 448-triangle sphere: 3 ms at 16 px/m, 114 ms at 1024 px/m, for the same
    charts) and the result does not depend on that density once ``_metric_charts`` has undone xatlas' per-chart
    snapping to whole texels."""
    import xatlas

    area = _area(meshes)
    atlas = xatlas.Atlas()
    for m in meshes:
        atlas.add_mesh(np.ascontiguousarray(m.points, dtype=np.float32), np.ascontiguousarray(m.triangles, dtype=np.uint32))
    co, po = _xatlas_options(UV_CHART_PASS_PX / np.sqrt(max(area, 1e-12)), surface_area=area, smooth_curvature=_smooth_curvature(meshes))
    atlas.generate(co, po)
    wh = np.array([atlas.width, atlas.height], np.float64)
    out = []
    for i, m in enumerate(meshes):
        vm, idx, uv = atlas.get_mesh(i)
        tris = idx.reshape(-1, 3).astype(np.int64)
        chart = np.full(len(tris), -1, np.int64)
        planar = []
        for c in range(atlas.get_mesh_chart_count(i)):
            ch = atlas.get_mesh_chart(i, c)
            chart[np.asarray(ch.faces, np.int64)] = c
            planar.append(ch.type == xatlas.ChartType.Planar)
        if (chart < 0).any():  # faces xatlas leaves out (zero area): one chart, a single point
            chart[chart < 0] = len(planar)
            planar.append(False)
        vm = vm.astype(np.int64)
        vm, tris, uv = _split_shared(vm, tris, np.asarray(uv, np.float64) * wh, chart)
        p3 = np.asarray(m.points, np.float64)[vm]
        uv, _ = _flatten(p3, tris, _metric_charts(p3, tris, uv, chart), chart, np.array(planar, bool))
        out.append(_Charted(m, vm, tris.astype(np.int32), uv, "computed", chart))
    return out


def _pieces(tris: np.ndarray, label: np.ndarray) -> np.ndarray:
    """Per triangle: 0-based id of its connected piece (triangles sharing a vertex) among those with the same label."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    nl = int(label.max()) + 1
    _, sv = np.unique(tris.reshape(-1) * nl + np.repeat(label, 3), return_inverse=True)  # vertices shared within a label
    sv = sv.reshape(-1, 3)
    n = int(sv.max()) + 1
    g = coo_matrix((np.ones(2 * len(sv)), (np.r_[sv[:, 0], sv[:, 1]], np.r_[sv[:, 1], sv[:, 2]])), shape=(n, n))
    return np.unique(connected_components(g, directed=False)[1][sv[:, 0]], return_inverse=True)[1]


def _slabs(c: _Charted, max_extent_m: float) -> _Charted:
    """Cuts computed charts the way ``_charted`` cuts exact ones: a chart longer than ``max_extent_m`` or filling
    less than UV_MIN_RECT_FILL of its minimum-area rectangle is cut across its long axis into equal slabs (each
    connected piece of a slab is a chart), up to four times. The pieces keep their uvs, so distortion does
    not change; the cost is seam vertices. Rectangle packing of computed charts depends on this: a torus'
    charts are curved strips (they fill 0.32 of their rectangles), and uncut it packs at 629 px/m instead of
    917 (1024 requested). The rectangles found here are kept for the packer."""
    tris = np.asarray(c.tris, np.int64)
    uv = c.uv_m
    a2 = _tri_areas(uv, tris, 2)
    cent = uv[tris].mean(axis=1)
    label = np.unique(c.chart, return_inverse=True)[1]
    todo = set(range(int(label.max()) + 1 if len(label) else 0))
    rect: dict[int, tuple] = {}
    for depth in range(5):
        order = np.argsort(label, kind="stable")
        ks, starts = np.unique(label[order], return_index=True)
        cut = np.zeros(len(tris), bool)
        sub = label.copy()  # slab index within a cut chart (any value elsewhere)
        for k, sel in zip(ks.tolist(), np.split(order, starts[1:])):
            if k not in todo:
                continue
            R, lo, wh = rect[k] = _min_area_frame(uv[np.unique(tris[sel])])
            ax = int(np.argmax(wh))
            fill = float(a2[sel].sum() / max(wh[0] * wh[1], 1e-30))
            if depth == 4 or len(sel) < 2 or (wh[ax] <= max_extent_m and fill >= UV_MIN_RECT_FILL):
                continue
            n = max(2, int(np.ceil(wh[ax] / max_extent_m)))
            coord = (cent[sel] @ R.T)[:, ax] - lo[ax]
            slab = np.minimum((coord / max(wh[ax], 1e-30) * n).astype(np.int64), n - 1)
            along = (uv[tris[sel]] @ R.T)[..., ax]
            used = np.unique(slab)
            if len(used) < 2 or max(float(np.ptp(along[slab == j])) for j in used) > max_extent_m:
                continue  # triangles span the length: no cut helps
            cut[sel] = True
            sub[sel] = k * n + slab  # unique per (chart, slab) among the cut triangles of this depth
            del rect[k]
        if not cut.any():
            break
        idx = np.flatnonzero(cut)
        top = int(label.max()) + 1
        label[idx] = top + _pieces(tris[idx], np.unique(sub[idx], return_inverse=True)[1])
        todo = set(np.unique(label[idx]).tolist())
    ids, label = np.unique(label, return_inverse=True)
    vm, t, uv2 = _split_shared(c.vmap, tris, uv, label)  # a no-op when nothing was cut
    out = _Charted(c.mesh, vm, t.astype(np.int32), uv2, c.kind, label)
    cid = _vertex_charts(t, label, len(uv2))
    order = np.argsort(cid, kind="stable")
    ks, starts = np.unique(cid[order], return_index=True)
    out.frames = [(v, *rect[int(ids[k])]) for k, v in zip(ks, np.split(order, starts[1:]))]
    return out


def _result(m: MeshData, vm: np.ndarray, tris: np.ndarray, uv: np.ndarray, fitted: bool, resolution: int | tuple[int, int],
            atlas_id: str | None, kind: str, hidden: np.ndarray | None = None) -> UVResult:
    pts = np.asarray(m.points, dtype=np.float64)[vm]
    uv = uv.astype(np.float32)
    shown = tris if hidden is None else tris[~hidden]
    inside = np.zeros(len(uv), bool)
    inside[np.asarray(shown, np.int64).reshape(-1)] = True
    if hidden is None:
        inside[:] = True
    uv[inside] = np.clip(uv[inside], 0.0, 1.0)  # the atlas; charts of hidden triangles only lie right of it
    if not len(shown):
        shown = tris
    a3, a2 = _tri_areas(pts, shown, 3).sum(), _tri_areas(uv, shown, 2).sum()
    W, H = _wh(resolution)
    dens = float(np.sqrt(a2 * W * H / a3)) if a3 > 0 and a2 > 0 else 0.0  # a uv area of 1 is W x H texels
    # Distortion is measured on texels (u x width, v x height): on a two-to-one atlas normalised uv scales u and v
    # differently, so an isometric chart would read as sheared (angle distortion 1.0 on a 512 x 256 atlas).
    px = uv.astype(np.float64) * [W, H]
    sm, sx = _stretch(pts, shown, px)
    return UVResult(m.node_id, pts, tris, uv, vm, dens, sm, sx, W, atlas_id, fitted, kind,
                    _angle_distortion(pts, shown, px), hidden, height=None if H == W else H)


def _area(meshes: Sequence[MeshData]) -> float:
    return float(sum(_tri_areas(np.asarray(m.points, np.float64), np.asarray(m.triangles), 3).sum() for m in meshes))


def _scaled(c: _Charted, f: float) -> _Charted:
    """The same charts drawn ``f`` times as large (f < 1: a material that carries less detail than the atlas density
    gets fewer texels; each chart stays an isometric unfolding, only its scale in the atlas changes)."""
    frames = None if c.frames is None else [(v, R, lo * f, wh * f) for v, R, lo, wh in c.frames]
    return _Charted(c.mesh, c.vmap, c.tris, c.uv_m * f, c.kind, c.chart, frames)


def _unwrap_group(group: list[MeshData], density: float, resolution: int | tuple[int, int], atlas_id: str | None,
                  hidden: Sequence[np.ndarray | None] | None = None, closed_out: list | None = None,
                  cache: dict | None = None, raw: dict | None = None, scales: Sequence[float] | None = None,
                  keep: KeepLayout | None = None) -> list[UVResult]:
    """One atlas: exact charts for meshes that carry them, xatlas charts for the others, all packed together as
    rectangles by ``_pack_analytic`` (so a group whose meshes all carry exact charts is packed as before).
    ``cache`` (a dict, per call site) keeps the charts and their closed packing, so a second call for the same
    group with another ``hidden`` mask only packs again. ``raw`` (a dict) keeps xatlas' charts before they are cut
    to the atlas side, so trying another side (``_unwrap_sized``) does not run xatlas again. ``scales`` (per mesh, at
    most 1): each mesh's charts are packed that many times as large as ``density`` asks (its own density is then
    density x scale; see ``unwrap_scene``'s ``detail_px_per_m``)."""
    if cache is not None and atlas_id in cache:
        known, uvs, d, closed = cache[atlas_id]
        return _repacked(known, uvs, d, closed, density, resolution, atlas_id, hidden, closed_out, keep)
    # a chart longer than the atlas at the requested density is cut where the mesh allows it
    max_extent = (max(_wh(resolution)) - 1.0 - 2.0 * UV_PADDING_PX) / density
    known = [_charted(m, max_extent) for m in group]
    todo = [i for i, c in enumerate(known) if c is None]
    if todo:
        computed = raw.get(atlas_id) if raw is not None else None
        if computed is None:
            computed = _computed_charts([group[i] for i in todo])
            if raw is not None:
                raw[atlas_id] = computed
        for i, c in zip(todo, computed):
            known[i] = _slabs(c, max_extent)
    if scales is not None:
        known = [_scaled(c, f) if c is not None and f < 1.0 else c for c, f in zip(known, scales)]  # type: ignore[arg-type]
    uvs, d = _pack_analytic(known, density, resolution)  # type: ignore[arg-type]
    # A few large computed charts can miss the requested density although their rectangles (with gutters) take
    # less area than the atlas: then it is their size, not the area, that does not fit (measured: a 2 m^2
    # sheet in 5 charts of 680 to 940 px packs at 1018 of 1024 px/m; xatlas fits it). Halving the longest
    # charts (up to three times, kept only when the density rises) gives the packer smaller pieces.
    for _ in range(3):
        if d >= density * (1 - 1e-9) or not todo:
            break
        ext = np.array([f[3] for c in known for f in _chart_frames(c)], np.float64).reshape(-1, 2)  # type: ignore[arg-type]
        if float((ext * density + 2.0 * UV_PADDING_PX).prod(axis=1).sum()) > float(np.prod(np.subtract(_wh(resolution), 1.0))):
            break
        longest = max(float(f[3].max()) for i in todo for f in _chart_frames(known[i]))  # type: ignore[arg-type]
        trial = list(known)
        for i in todo:
            trial[i] = _slabs(known[i], 0.5 * longest)  # type: ignore[arg-type]
        uvs2, d2 = _pack_analytic(trial, density, resolution)  # type: ignore[arg-type]
        if d2 <= d:
            break
        known, uvs, d = trial, uvs2, d2
    fitted = bool(d >= density * (1 - 1e-9))
    closed = [_result(c.mesh, c.vmap, c.tris, uv, fitted, resolution, atlas_id, c.kind)  # type: ignore[union-attr]
              for c, uv in zip(known, uvs)]
    if cache is not None:
        cache[atlas_id] = (known, uvs, d, closed)
    return _repacked(known, uvs, d, closed, density, resolution, atlas_id, hidden, closed_out, keep)


@dataclass(frozen=True)
class KeepLayout:
    """The layout a package shipped, to be kept by the next export of the same asset (an edit): the atlas size and, per
    part, the shipped LOD0 points (Z-up asset space, metres), triangles and uvs. Read from the package alone
    (``package.layout_from_package``). Immutable: every export attempt of one edit reads the same one."""
    size: tuple[int, int]
    parts: Mapping[str, tuple[np.ndarray, np.ndarray, np.ndarray]]
    digest: str = ""


#: Positions of shipped vertices are compared at this resolution (m): float32 USD points read back and float64 stage
#: points of the same geometry agree to about 1e-7 m; a part that moved by a millimetre does not match.
KEEP_POS_DECIMALS = 5
#: Block size (atlas px) of the free-space search for the charts an edit adds or changes (``_pack_kept``).
KEEP_BLOCK_PX = 4
#: How far from 1 the scale of a shipped chart's placement may be and still be the same unfolding (``_pack_kept``).
KEEP_SCALE_TOL = 2e-3


def _pos_keys(p: np.ndarray) -> np.ndarray:
    return np.round(np.asarray(p, np.float64), KEEP_POS_DECIMALS)


def _pack_kept(known: list[_Charted], density: float, resolution: int | tuple[int, int], hidden, keep: KeepLayout
               ) -> tuple[list[np.ndarray] | None, int, int, str]:
    """The shipped packing with every chart of ``keep`` that is still there kept where it was.

    A chart is kept when every triangle of it that ships matches a triangle the package shipped (the same three
    corner positions, ``KEEP_POS_DECIMALS``), and the old placement is the same isometric unfolding at this chart's
    density (a similarity of its metre layout with scale = the density it is packed at: a material whose detail
    changed is not kept). A kept chart's vertices take the shipped uvs verbatim (bit for bit); vertices only hidden
    triangles use follow the same placement. Every other chart (a changed or new part, a face a neighbour no longer
    covers) is placed first-fit, bottom-left, into texels no kept chart or its gutter occupies, at the same density and
    padding as ``_pack_analytic``. Returns (per-mesh uvs, charts kept, charts packed, reason); uvs None when a chart
    does not fit (the caller repacks everything and says why)."""
    W, H = _wh(resolution)
    Wo, Ho = keep.size  # the shipped atlas: kept charts keep their texel positions in it, from the bottom-left corner
    same_size = (W, H) == (Wo, Ho)
    pad = 2.0 * UV_PADDING_PX
    edge = 1.0
    frames, phantom = [], []
    for mi, c in enumerate(known):
        h = None if hidden is None else hidden[mi]
        seen = None
        if h is not None and h.any():
            seen = np.zeros(len(c.uv_m), bool)
            seen[np.asarray(c.tris, np.int64)[~h].reshape(-1)] = True
        for f in _chart_frames(c):
            (frames if seen is None or seen[f[0]].any() else phantom).append((mi, *f))
    out = [np.zeros((len(c.uv_m), 2)) for c in known]
    occ = np.zeros((H, W), bool)  # rows are v (bottom up), columns u, in atlas px
    old_tri: dict[str, dict[tuple, np.ndarray]] = {}

    def old_index(node: str) -> dict[tuple, np.ndarray]:
        if node not in old_tri:
            pts, tri, uv = keep.parts[node]
            k = _pos_keys(pts)
            idx = {}
            for t in np.asarray(tri, np.int64):
                # corners in their order (the exporter writes a triangle's corners in the same order every time): a chart
                # that wraps round (a cylinder's side) has two vertices at one position with different uvs, so a corner
                # is found by its place in the triangle, never by its position alone
                idx[tuple(tuple(k[v]) for v in t)] = np.array([uv[v] for v in t])
            old_tri[node] = idx
        return old_tri[node]

    kept, todo = 0, []
    for fr in frames:
        mi, v, R, lo, wh = fr
        c = known[mi]
        node = c.mesh.node_id
        placed = None
        if node in keep.parts:
            tris = np.asarray(c.tris, np.int64)
            in_chart = np.zeros(len(c.uv_m), bool)
            in_chart[v] = True
            sel = in_chart[tris[:, 0]]
            h = None if hidden is None else hidden[mi]
            ship = sel & (~h if h is not None else True)
            idx = old_index(node)
            pts = _pos_keys(np.asarray(c.mesh.points, np.float64)[c.vmap])
            uv_of: dict[int, np.ndarray] = {}
            ok = bool(ship.any())
            for t in tris[ship]:
                ouv = idx.get(tuple(tuple(pts[x]) for x in t))
                if ouv is None:
                    ok = False
                    break
                for x, u in zip(t, ouv):
                    if x in uv_of and not np.array_equal(uv_of[x], u):
                        ok = False
                        break
                    uv_of[x] = u
                if not ok:
                    break
            if ok:
                vs = np.array(sorted(uv_of))
                src = c.uv_m[vs] * density  # the chart at this density, in px
                dst = np.asarray([uv_of[x] for x in vs], np.float64) * [Wo, Ho]
                A = np.c_[src, np.ones(len(src))]
                sol, *_ = np.linalg.lstsq(A, dst, rcond=None)
                M = sol[:2].T
                sv = np.linalg.svd(M, compute_uv=False)
                resid = float(np.abs(A @ sol - dst).max()) if len(src) else np.inf
                # the same unfolding at the same density: a similarity of scale 1 in texels. The tolerance is float32 uv
                # rounding over a chart a few texels wide (measured up to 1.6e-4 on one-triangle charts); a material whose
                # detail changed is drawn at another scale (a map half the size: 0.5)
                if len(vs) >= 3 and resid < 1e-2 and abs(sv[0] - 1) < KEEP_SCALE_TOL and abs(sv[1] - 1) < KEEP_SCALE_TOL and np.linalg.det(M) > 0:
                    full = (np.c_[c.uv_m[v] * density, np.ones(len(v))] @ sol) / [W, H]
                    for j, x in enumerate(v):
                        if x in uv_of:  # the shipped uv itself (bit for bit) in the same atlas; its texel position in a grown one
                            full[j] = uv_of[x] if same_size else np.asarray(uv_of[x], np.float64) * [Wo, Ho] / [W, H]
                    placed = full
        if placed is None:
            todo.append(fr)
            continue
        out[mi][v] = placed
        px = placed * [W, H]
        x0, y0 = np.floor(px.min(0) - pad / 2).astype(int)
        x1, y1 = np.ceil(px.max(0) + pad / 2).astype(int)
        occ[max(y0, 0):min(y1, H), max(x0, 0):min(x1, W)] = True
        kept += 1
    if kept == 0:
        return None, 0, len(frames), "no chart of the shipped layout is still there unchanged"
    # the rest, largest first, into the free texels (summed-area table of the occupied ones)
    todo.sort(key=lambda f: -float(np.prod(f[4] * density + pad)))
    # the search runs on blocks of KEEP_BLOCK_PX texels (a block is taken when any texel of it is): 16 times fewer cells
    # on a 4096 x 2048 atlas, at the cost of up to KEEP_BLOCK_PX - 1 texels of placement freedom
    bs = KEEP_BLOCK_PX
    for mi, v, R, lo, wh in todo:
        best = None
        hb, wb = -(-H // bs), -(-W // bs)
        blk = np.zeros((hb * bs, wb * bs), bool)
        blk[:H, :W] = occ
        blk[H:, :] = True
        blk[:, W:] = True
        blk = blk.reshape(hb, bs, wb, bs).any(axis=(1, 3))
        S = np.zeros((hb + 1, wb + 1), np.int32)
        S[1:, 1:] = blk.cumsum(0, dtype=np.int32).cumsum(1, dtype=np.int32)
        for turned in (False, True):
            w, h = (wh[::-1] if turned else wh) * density + pad
            w, h = int(np.ceil(w + edge)), int(np.ceil(h + edge))
            if w > W or h > H:
                continue
            cw, ch = -(-w // bs), -(-h // bs)
            if cw > wb or ch > hb:
                continue
            area = S[ch:, cw:] - S[:-ch, cw:] - S[ch:, :-cw] + S[:-ch, :-cw]
            free = np.argwhere(area == 0)
            if len(free):
                y, x = free[0] * bs  # lowest row, then leftmost: bottom-left, as the skyline packer
                if best is None or (y, x) < best[:2]:
                    best = (int(y), int(x), turned, w, h)
        if best is None:
            return None, kept, len(frames), "the changed or new charts do not fit in the texels the kept charts leave free"
        y, x, turned, w, h = best
        q = (known[mi].uv_m[v] @ R.T - lo) * density
        if turned:
            q = np.stack([wh[1] * density - q[:, 1], q[:, 0]], axis=1)
        out[mi][v] = (q + [x + pad / 2 + edge / 2, y + pad / 2 + edge / 2]) / [W, H]
        occ[y:y + h, x:x + w] = True
    for mi, v, R, lo, wh in phantom:  # charts of hidden triangles only: right of the atlas, as _pack_analytic
        q = (known[mi].uv_m[v] @ R.T - lo) * density
        out[mi][v] = (q + [W + pad, 0.0]) / [W, H]
    return out, kept, len(frames), ""


def _repacked(known, uvs, d, closed, density, resolution, atlas_id, hidden, closed_out, keep=None) -> list[UVResult]:
    if closed_out is not None:
        closed_out.extend(closed)
    if keep is not None:
        note = None
        (W, H), (Wo, Ho) = _wh(resolution), tuple(keep.size)
        if W < Wo or H < Ho:
            note = f"repacked: the atlas is now {W} x {H} px, smaller than the shipped {Wo} x {Ho} on a side"
        elif d < density * (1 - 1e-9):
            note = f"repacked: the {W} x {H} px atlas holds the charts only below the density ({d:.1f} of {density:.1f} px/m)"
        else:
            got, n_kept, n_all, why = _pack_kept(known, density, resolution, hidden, keep)
            if got is not None:
                hs = hidden if hidden is not None else [None] * len(known)
                grown = "" if (W, H) == (Wo, Ho) else (f"; the atlas grew from {Wo} x {Ho} to {W} x {H} px: their uvs are rescaled, "
                                                       "their texels are where they were")
                return [replace(_result(c.mesh, c.vmap, c.tris, uv, True, resolution, atlas_id, c.kind, hs[i]),
                                layout=f"kept: {n_kept} of {n_all} charts where they shipped{grown}")
                        for i, (c, uv) in enumerate(zip(known, got))]
            note = f"repacked: {why}"
        normal = _repacked(known, uvs, d, closed, density, resolution, atlas_id, hidden, None)
        return [replace(r, layout=note) for r in normal]
    if hidden is None or not any(h is not None and h.any() for h in hidden):
        return closed
    # The same charts (same seam-split vertices and triangles), packed again without the charts of hidden
    # triangles only. Kept only when it does not lower the density (rectangle packing is not monotone).
    uvs2, d2 = _pack_analytic(known, density, resolution, hidden)  # type: ignore[arg-type]
    if d2 < d:
        uvs2, d2 = uvs, d
    fitted = bool(d2 >= density * (1 - 1e-9))
    return [_result(c.mesh, c.vmap, c.tris, uv, fitted, resolution, atlas_id, c.kind, hidden[i])  # type: ignore[union-attr]
            for i, (c, uv) in enumerate(zip(known, uvs2))]


def unwrap_uvs(mesh: MeshData, texel_density_px_per_m: float, resolution: int = 2048) -> UVResult:
    return _unwrap_group([mesh], texel_density_px_per_m, resolution, mesh.material_id)[0]


#: Atlas sizes (width, height) an atlas sized from need (``unwrap_scene(shared_resolution=None)``) may take, by area. Squares
#: and two-to-one rectangles: with squares only, a layout just over one square needs the next, four times the texels
#: (up to three quarters empty); with two-to-one steps the next size is twice the texels (at most half empty). Only
#: landscape is listed: the packer may turn every chart, so a portrait atlas is the same layout transposed. Every
#: engine and block-compressed format takes non-square power-of-two maps. 4096 x 4096 is the largest: the texels of
#: four 2048 maps, what an asset of the default four materials (``EngineBudget.max_draw_calls``) shipped before
#: materials shared one atlas (a 4096 RGBA8 map with mips is already 85 MiB uncompressed). The engine's own
#: ``max_texture_px`` caps both sides further (Roblox: 1024).
ATLAS_SIZES = ((256, 256), (512, 256), (512, 512), (1024, 512), (1024, 1024), (2048, 1024), (2048, 2048), (4096, 2048),
               (4096, 4096))
ATLAS_SIDES = tuple(sorted({w for w, _ in ATLAS_SIZES}))
#: Atlas px one repeat of a tileable material map spans at the target texel density: 2048, so a map covers 2 m at the
#: default 1024 px/m. Until materials shared an atlas every material had its own 2048 px atlas and its map spanned all
#: of it; keeping that span keeps the surface detail whatever the shared atlas measures (a 2048 px map spread over a
#: 4096 atlas would show half its texels per metre; on a small atlas the map repeats instead of shrinking).
SWATCH_SPAN_PX = 2048
#: The detail (map px per SWATCH_SPAN_PX) a shared atlas of flat-coloured materials only is laid out for: the smallest
#: atlas side. A flat patch needs no texel density, only charts wide enough that their gutters (UV_PADDING_PX) keep
#: two colours apart; at the default target this is 128 px/m. Measured on a three-part, two-colour prop: at the full
#: 1024 px/m its albedo map was 4096 x 4096, and stock Godot, sampling it at a high mip level, bled the two colours
#: into each other on 22 pixels of the frame.
FLAT_DETAIL_PX = 256


def atlas_sizes(engine: str | None, max_px: int | None = None) -> tuple[tuple[int, int], ...]:
    """The (width, height) sizes ``unwrap_scene`` tries for an atlas sized from need for ``engine``: ``ATLAS_SIZES`` with
    both sides within its ``max_texture_px`` and within ``max_px`` (an art direction's texture size)."""
    from antigravity3d.kernel.validator import ENGINE_RULES

    rule = ENGINE_RULES.get(engine) if engine else None
    cap = rule.max_texture_px if rule is not None and rule.max_texture_px else ATLAS_SIZES[-1][0]
    if max_px:
        cap = min(cap, int(max_px))
    return tuple(wh for wh in ATLAS_SIZES if max(wh) <= cap) or (ATLAS_SIZES[0],)


def shared_atlas_id(materials: Iterable[str | None]) -> str | None:
    """The ``UVResult.atlas_id`` of an atlas several materials share: the material id itself for one material (so a
    one-material asset reads as before), else the ids joined with '+', which no IR id contains."""
    ids = sorted({str(m) for m in materials})
    return ids[0] if len(ids) == 1 else "+".join(ids)


def _unwrap_sized(group: list[MeshData], density: float, sides: Sequence[int | tuple[int, int]], atlas_id: str | None,
                  hidden, closed_out: list | None, cache: dict | None, scales: Sequence[float] | None = None,
                  keep: KeepLayout | None = None, pin: bool = True) -> list[UVResult]:
    """``_unwrap_group`` at the smallest of ``sides`` whose atlas holds every chart at ``density`` (the largest side
    when none does: the density is then lowered, ``UVResult.fitted`` False). The closed packing (every triangle
    shipped) decides, so the layout with hidden faces removed, packed into the same side, never needs more. xatlas runs
    once; each side tried costs one packing. With ``cache`` the chosen side's packing is kept (``_unwrap_group``)."""
    if cache is not None and atlas_id in cache:
        side = cache[atlas_id][3][0].size
        out = _unwrap_group(group, density, side, atlas_id, hidden, closed_out, cache, keep=keep if pin else None)
        tried = cache.get((atlas_id, "sizes tried"), ())
        return [replace(r, size_trials=tried) for r in out] if tried else out
    raw: dict = {}
    tried = []
    if keep is not None and tuple(keep.size) in {tuple(_wh(x)) for x in sides}:  # the shipped size first: it may keep the layout
        sides = [tuple(keep.size), *[x for x in sides if tuple(_wh(x)) != tuple(keep.size)]]
    fits = False
    for side in sides:
        local: dict = {}
        trial = _unwrap_group(group, density, side, atlas_id, None, None, local, raw, scales)
        tried.append((*_wh(side), round(max((r.texel_density for r in trial if r.texel_density > 0), default=0.0), 2)))
        if all(r.fitted for r in trial):
            fits = True
            break
    if not fits and len(sides) > 1:  # none holds the charts at the density: the largest, at the density that fits
        side = max(sides, key=lambda x: int(np.prod(_wh(x))))
        if tuple(_wh(side)) != tuple(_wh(sides[-1])):
            local = {}
            _unwrap_group(group, density, side, atlas_id, None, None, local, raw, scales)
    keep_cache = cache if cache is not None else {}
    keep_cache[atlas_id] = local[atlas_id]
    if len(sides) > 1:
        keep_cache[(atlas_id, "sizes tried")] = tuple(tried)
    out = _unwrap_group(group, density, side, atlas_id, hidden, closed_out, keep_cache, keep=keep if pin else None)
    if len(sides) > 1:
        out = [replace(r, size_trials=tuple(tried)) for r in out]
        if closed_out is not None:
            closed_out[-len(out):] = [replace(r, size_trials=tuple(tried)) for r in closed_out[-len(out):]]
    return out


def unwrap_scene(meshes: list[MeshData], budget: EngineBudget, resolution: int = 2048,
                 hidden: Sequence[np.ndarray | None] | None = None, closed_out: list | None = None,
                 cache: dict | None = None, shared: Collection[str] | None = None,
                 shared_resolution: int | tuple[int, int] | None = None, max_px: int | None = None,
                 detail_px_per_m: float | Mapping[str | None, float | None] | None = None,
                 keep: KeepLayout | None = None, pin: bool = True) -> list[UVResult]:
    """One atlas per material id, except that the materials in ``shared`` are laid out together in one atlas (at one
    texel density, so the package can bake their maps into one texture set: ``package._compose_atlas``); results are
    returned in input order.

    ``resolution``: the side of each material's own atlas. The shared atlas is ``shared_resolution`` px (a side or
    (width, height)), or with None sized from need: the smallest of ``atlas_sizes(budget.target_engine, max_px)`` that
    holds its charts at ``budget.texel_density_px_per_m``; so the budget's density target and an art direction's
    texture size (``max_px``) are what choose it. Its results carry ``repeat_px`` (``SWATCH_SPAN_PX`` at the target).
    ``detail_px_per_m``: the most detail any shared material carries (``surface_detail.detail_px_per_m``). The shared
    atlas is laid out at that density when it is below the target (a 256 px map spanning SWATCH_SPAN_PX at the target
    holds an eighth of it: its texels are then copied 1:1), rather than at a density only enlarged pixels would fill.
    Given per material (a mapping; None or absent: a flat colour, ``FLAT_DETAIL_PX``), the atlas takes the largest and
    each material's charts are scaled down to its own: a flat patch beside a textured surface costs few texels.
    ``keep``: the layout a package shipped (an edit's): the shared atlas keeps its size when it still holds the charts,
    and every chart that ships unchanged keeps its place (``_pack_kept``); the results' ``layout`` says what happened. Only the shared atlas is sized from need because only its maps are baked at
    the atlas side (package._compose_atlas); a material's own atlas ships the material's map as it is, spread over
    the whole atlas, so its side is that map's span (2048, package.SWATCH_SPAN_PX).

    Meshes that carry exact charts (``MeshData.uv_charts``, set for IR primitives) are unfolded
    isometrically and only packed; the others get xatlas charts, computed once. ``hidden`` (per mesh, a triangle
    mask or None) marks triangles that will not ship: see ``_pack_analytic`` and ``UVResult.hidden``. With it,
    ``closed_out`` (a list) receives the unwrap without that mask, in input order: the same charts, vertices and
    triangles, packed as if every triangle shipped.
    """
    together = {m.material_id for m in meshes if shared is not None and m.material_id in shared}
    joint = shared_atlas_id(together) if together else None
    groups: dict[str | None, list[int]] = {}
    for i, m in enumerate(meshes):
        groups.setdefault(joint if m.material_id in together else m.material_id, []).append(i)
    need = atlas_sizes(budget.target_engine, max_px) if shared_resolution is None else (shared_resolution,)
    res: list[UVResult | None] = [None] * len(meshes)
    closed: list[UVResult | None] = [None] * len(meshes)
    for mat, ids in groups.items():
        h = None if hidden is None else [None if hidden[i] is None or not hidden[i].any() else hidden[i] for i in ids]
        got: list[UVResult] = []
        is_shared = bool(together) and mat == joint
        sides = need if is_shared else (resolution,)
        density = budget.texel_density_px_per_m
        scales = None
        if is_shared and isinstance(detail_px_per_m, Mapping):
            flat = budget.texel_density_px_per_m * FLAT_DETAIL_PX / SWATCH_SPAN_PX
            own = [min(float(detail_px_per_m.get(meshes[i].material_id) or flat), density) for i in ids]
            density = max(own)
            scales = [d / density for d in own]
        elif is_shared and detail_px_per_m:
            density = min(density, float(detail_px_per_m))
        out = _unwrap_sized([meshes[i] for i in ids], density, sides, mat, h, got, cache, scales, keep if is_shared else None, pin)
        if is_shared:
            d = max((r.texel_density for r in out if r.texel_density > 0), default=budget.texel_density_px_per_m)  # what ships
            repeat = max(1, int(round(SWATCH_SPAN_PX * d / budget.texel_density_px_per_m)))
            out = [replace(r, shared_atlas=True, repeat_px=repeat) for r in out]
            got = [replace(r, shared_atlas=True, repeat_px=repeat) for r in got]
        for i, r in zip(ids, out):
            res[i] = r
        for i, r in zip(ids, got):
            closed[i] = r
    if closed_out is not None:
        closed_out.extend(closed)
    return res  # type: ignore[return-value]


#: EL4CTEO retopology-uv/SKILL.md and texel_density.py: the largest-to-smallest density ratio an asset should stay within
TEXEL_SPREAD_LIMIT = 1.3
#: a missed target is warned about from this fraction down (``target_met`` itself is exact): 7 of the benchmark's ten
#: single-primitive assets miss 1024 px/m in a 2048 px atlas, several by under 3 %, and a warning on each would be noise
TEXEL_TARGET_WARN_FRACTION = 0.9


def texel_density_report(results: Sequence[UVResult], target_px_per_m: float) -> dict:
    """Achieved texel density per material atlas and across the asset (reporting only: nothing is repacked).

    Each material has one atlas and one density for all its charts (``_pack_analytic``). ``target_met`` is the packer's
    ``fitted`` flag (False: the atlas could not hold its charts at ``target_px_per_m`` and the density was lowered).
    ``px_per_m`` is in atlas space, at ``atlas_px`` (the packing resolution: the width, its longer side;
    ``atlas_wh`` is (width, height), which differ for a two-to-one atlas); ``needed_atlas_px`` is the width at which
    the same layout would reach the target (the atlas scales with the density), ``needed_atlas_wh`` both sides,
    ``needed_atlas_px_pow2`` the next power of two of the width. ``spread`` is the highest over the lowest material density, with the two extremes named."""
    groups: dict[str, list[UVResult]] = {}
    for r in results:
        if r.texel_density > 0:
            groups.setdefault(str(r.atlas_id), []).append(r)
    mats: dict[str, dict] = {}
    for name, rs in sorted(groups.items()):
        # one density per atlas; in a shared atlas a material that carries less detail (a flat colour) is drawn smaller
        # (unwrap_scene's detail_px_per_m), so the atlas's density is its largest
        shared_atlas = any(r.shared_atlas for r in rs)
        d = max(r.texel_density for r in rs) if shared_atlas else min(r.texel_density for r in rs)
        px, py = max(r.size for r in rs)
        met = all(r.fitted for r in rs)
        need = px if met else int(np.ceil(px * target_px_per_m / d))
        need_y = py if met else int(np.ceil(py * target_px_per_m / d))
        # occupancy: the share of the atlas that charts cover (sum of the shipped triangles' UV areas, overlaps counted
        # twice, so an upper bound) and the share its UV bounding box covers (what a crop to the used region would keep)
        area, lo, hi = 0.0, np.full(2, np.inf), np.full(2, -np.inf)
        for r in rs:
            sh = shipped(r)
            if sh.uvs is None or not len(sh.triangles):
                continue
            q = np.asarray(sh.uvs, np.float64)[np.asarray(sh.triangles)]
            e1, e2 = q[:, 1] - q[:, 0], q[:, 2] - q[:, 0]
            area += float(np.abs(e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]).sum() / 2)
            lo, hi = np.minimum(lo, q.reshape(-1, 2).min(0)), np.maximum(hi, q.reshape(-1, 2).max(0))
        box = float(np.prod(np.clip(hi - lo, 0, 1))) if np.isfinite(lo).all() else 0.0
        mats[name] = {"px_per_m": round(float(d), 2), "target_met": met, "atlas_px": px, "atlas_wh": [px, py],
                      "needed_atlas_px": need, "needed_atlas_wh": [need, need_y],
                      "needed_atlas_px_pow2": 1 << max(0, need - 1).bit_length(),
                      "shared": any(r.shared_atlas for r in rs),
                      "chart_area_fraction": round(min(area, 1.0), 4), "uv_bbox_fraction": round(box, 4)}
    if not mats:
        return {"target_px_per_m": target_px_per_m, "materials": {}, "spread": None, "spread_limit": TEXEL_SPREAD_LIMIT,
                "highest": None, "lowest": None}
    hi = max(mats, key=lambda k: mats[k]["px_per_m"])
    lo = min(mats, key=lambda k: mats[k]["px_per_m"])
    return {"target_px_per_m": target_px_per_m, "materials": mats,
            "spread": round(mats[hi]["px_per_m"] / mats[lo]["px_per_m"], 3), "spread_limit": TEXEL_SPREAD_LIMIT,
            "highest": hi, "lowest": lo}


def describe_texel_density(report: dict) -> str:
    """One line for a step detail: ``stone 323/1024 px/m (target missed), metal 1024 (met); spread 3.17x``."""
    t = report["target_px_per_m"]
    parts = [f"{k} {v['px_per_m']:g}/{t:g} px/m (target missed)" if not v["target_met"] else f"{k} {v['px_per_m']:g} (met)"
             for k, v in report["materials"].items()]
    spread = report.get("spread")
    return ", ".join(parts) + (f"; spread {spread:.2f}x" if spread is not None else "")


def uv_overlap_fraction(results: Sequence[UVResult], res: int = 512) -> float:
    """Fraction of covered texels hit by more than one shipped triangle (conservative raster; hidden contact faces,
    whose charts may lie outside the atlas, are not shipped and not counted)."""
    cov = np.zeros((res, res), np.int32)
    for r in map(shipped, results):
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
    error: float                 # bound on the deviation from LOD0, relative to the mesh scale (meshopt simplify_scale)
    # The same in metres: no sample of the two-sided surface distance exceeds it, and it is within the level's
    # budget. It is a bound, not the largest sampled distance: a sample is only settled exactly when it could
    # exceed the budget (see _surface_deviation), so the true deviation is often well below this value.
    error_m: float
    screen_fraction: float = 1.0  # largest screen fraction (bounding-sphere diameter / frame height) it is shown at
    hidden: np.ndarray | None = None  # (M,) bool: triangles not shipped at this level (see UVResult.hidden)

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
    (first pass) and triangle bounding boxes (exact pruning bound). The exhaustive pass looks for every box
    closer than a bound through KD-trees over box centres, one per size class: a box can only be closer than
    ``r`` if its centre is within ``r`` plus its half-diagonal, and one tree with the largest half-diagonal of
    the whole mesh would make one table-top triangle turn every query into a scan of every triangle."""

    SIZE_CLASS_FACTOR = 4.0   # half-diagonals within a class differ by at most this factor ...
    SIZE_CLASSES = 8          # ... except the last class, which holds everything smaller
    BRUTE_PAIRS = 2_000_000   # sample x box pairs tested at once without a tree (and the chunk size)
    BRUTE_BOXES = 8192        # sample x box pairs below which the exhaustive pass tests every box
    DENSE_PAIRS = 4096        # below this many sample x triangle pairs, exact distances are computed outright

    def __init__(self, p: np.ndarray, t: np.ndarray) -> None:
        from scipy.spatial import cKDTree

        p = np.asarray(p, np.float64)
        t = np.asarray(t, np.int64)
        self.a, self.b, self.c = p[t[:, 0]], p[t[:, 1]], p[t[:, 2]]
        self.m = len(t)
        self.tree = cKDTree((self.a + self.b + self.c) / 3) if self.m else None
        self.lo = np.minimum(np.minimum(self.a, self.b), self.c)
        self.hi = np.maximum(np.maximum(self.a, self.b), self.c)
        self._classes: list | None = None
        self._t = t
        self._p = p
        self._partner: np.ndarray | None = None

    @property
    def partner(self) -> np.ndarray:
        """Per triangle, an edge-adjacent coplanar triangle whose union with it is a convex quad (a box face,
        a cylinder side panel), else itself. The distance to a convex set is a convex function, so a convex
        bound may use the quad instead of either half (see _convex_upper)."""
        if self._partner is None:
            t, p = self._t, self._p
            part = np.arange(self.m)
            if self.m:
                n = np.cross(self.b - self.a, self.c - self.a)
                ln = np.maximum(np.linalg.norm(n, axis=1), 1e-300)
                e = np.concatenate([t[:, [0, 1]], t[:, [1, 2]], t[:, [2, 0]]])
                f = np.tile(np.arange(self.m), 3)
                opp = np.concatenate([t[:, 2], t[:, 0], t[:, 1]])  # vertex opposite each edge
                key = np.sort(e, axis=1)
                order = np.lexsort((key[:, 1], key[:, 0]))
                ks = key[order]
                i = np.flatnonzero((ks[1:] == ks[:-1]).all(axis=1))
                f1, f2 = f[order[i]], f[order[i + 1]]
                ea, eb = p[ks[i, 0]], p[ks[i, 1]]
                c, d = p[opp[order[i]]], p[opp[order[i + 1]]]
                nn = n[f1]

                def side(x, y, z):
                    return np.einsum("ij,ij->i", np.cross(y - x, z - x), nn)

                ok = (np.einsum("ij,ij->i", n[f1], n[f2]) / (ln[f1] * ln[f2]) >= 1 - 1e-9) & (f1 != f2)
                ok &= (side(c, d, ea) * side(c, d, eb) < 0) & (side(ea, eb, c) * side(ea, eb, d) < 0)  # diagonals cross
                part[f1[ok]], part[f2[ok]] = f2[ok], f1[ok]  # any qualifying neighbour: each pair is a convex quad
            self._partner = part
        return self._partner

    def _size_classes(self) -> list:
        """[(triangle ids, largest half-diagonal, KD-tree over their box centres or None)], one entry per size
        class. Small classes are tested exhaustively (a tree would cost more than it prunes)."""
        from scipy.spatial import cKDTree

        if self._classes is None:
            half = np.linalg.norm((self.hi - self.lo) / 2, axis=1)
            top = float(half.max()) if self.m else 0.0
            if top > 0:
                k = np.floor(np.log(top / np.maximum(half, top * 1e-300)) / np.log(self.SIZE_CLASS_FACTOR))
                k = np.minimum(k, self.SIZE_CLASSES - 1).astype(np.int64)
            else:
                k = np.zeros(self.m, np.int64)
            centre = (self.lo + self.hi) / 2
            self._classes = []
            for cls in np.unique(k):
                ids = np.flatnonzero(k == cls)
                self._classes.append((ids, float(half[ids].max()), cKDTree(centre[ids]) if len(ids) > 64 else None))
        return self._classes

    def _pairs(self, sl: np.ndarray, bound: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Every (sample row, triangle) whose bounding box is closer to the sample than ``bound[row]``: the only
        triangles that can be closer than the bound. Exact: a box closer than r has its centre within r plus
        its half-diagonal, and the tree query uses the class's largest half-diagonal (with a rounding margin)."""
        import itertools

        rows, cols = [], []
        if len(sl) * self.m <= self.BRUTE_BOXES:  # few pairs: test every box, no class trees needed
            classes = [(np.arange(self.m), 0.0, None)]
        else:
            classes = self._size_classes()
        for ids, half, tree in classes:
            if tree is None:
                r2 = np.repeat(np.arange(len(sl)), len(ids))
                c2 = np.tile(ids, len(sl))
            else:
                hits = tree.query_ball_point(sl, (bound + half) * (1 + 1e-9) + 1e-12, return_sorted=False)
                cnt = np.fromiter(map(len, hits), np.int64, len(hits))
                if not cnt.sum():
                    continue
                r2 = np.repeat(np.arange(len(sl)), cnt)
                c2 = ids[np.fromiter(itertools.chain.from_iterable(hits), np.int64, int(cnt.sum()))]
            gap = np.maximum(np.maximum(self.lo[c2] - sl[r2], sl[r2] - self.hi[c2]), 0.0)
            keep = np.sqrt(np.einsum("ij,ij->i", gap, gap)) < bound[r2]
            rows.append(r2[keep])
            cols.append(c2[keep])
        if not rows:
            return np.zeros(0, np.int64), np.zeros(0, np.int64)
        return np.concatenate(rows), np.concatenate(cols)

    def _chunks(self, n: int):
        """Sample slices small enough that the exhaustively tested classes stay within BRUTE_PAIRS pairs."""
        if n * self.m <= self.BRUTE_BOXES:
            return iter((slice(0, n),))
        brute = sum(len(ids) for ids, _, tree in self._size_classes() if tree is None)
        step = max(1, self.BRUTE_PAIRS // max(brute, 64))
        return (slice(s0, s0 + step) for s0 in range(0, n, step))

    def _exact(self, ub: np.ndarray, ids: np.ndarray, samples: np.ndarray) -> None:
        """Lower ub[ids] to the exact distance of samples[ids]: the minimum over every triangle whose box is
        closer than the current bound (any closer triangle is one of them)."""
        for sl_ in self._chunks(len(ids)):
            part = ids[sl_]
            sl = samples[part]
            r2, c2 = self._pairs(sl, ub[part])
            self._refine(ub, part, sl, r2, c2)

    def _first_bound(self, samples: np.ndarray) -> np.ndarray:
        """Per-sample upper bound: the distance to the triangle with the nearest centroid."""
        nn = np.asarray(self.tree.query(samples, k=1)[1]).reshape(-1)
        return _point_triangle_distance(samples, self.a[nn], self.b[nn], self.c[nn])

    def _settle(self, ub: np.ndarray, ids: np.ndarray, samples: np.ndarray, limit: float) -> None:
        """Passes two and three of :meth:`distances` on samples[ids] (whose first bound exceeds ``limit``):
        tighten with the 8 nearest centroids, then make exact every value still above ``limit``. Each sample is
        settled independently, so settling a subset gives those samples the values the whole set would."""
        k = min(8, self.m)
        if k > 1:
            nn = np.asarray(self.tree.query(samples[ids], k=k)[1]).reshape(len(ids), k)
            rows = np.repeat(ids, k)
            f = nn.reshape(-1)
            d = _point_triangle_distance(samples[rows], self.a[f], self.b[f], self.c[f]).reshape(len(ids), k).min(axis=1)
            ub[ids] = np.minimum(ub[ids], d)
            ids = ids[ub[ids] > limit]
        if len(ids):
            self._exact(ub, ids, samples)

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
        ub = self._first_bound(samples)  # any triangle gives an upper bound
        far = np.flatnonzero(ub > limit)
        if len(far):
            self._settle(ub, far, samples, limit)
        return ub

    @staticmethod
    def _batches(ub: np.ndarray, limit: float):
        """The samples whose bound exceeds ``limit``, in order of decreasing bound, in batches of 64 doubling to
        4096 (the order in which :meth:`max_distance` makes them exact)."""
        far = np.flatnonzero(ub > limit)
        far = far[np.argsort(-ub[far], kind="stable")]
        s0, step = 0, 64
        while s0 < len(far):
            yield far[s0:s0 + step]
            s0, step = s0 + step, min(step * 2, 4096)

    HEAD = 256  # samples settled first, farthest bound first: enough to find a maximum above the limit

    def _head_tail(self, samples: np.ndarray, limit: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(first bounds, the HEAD samples with the largest bounds above ``limit``, the other samples above it)."""
        ub = self._first_bound(samples)
        far = np.flatnonzero(ub > limit)
        if len(far) > self.HEAD:
            far = far[np.argsort(-ub[far], kind="stable")]
        return ub, far[:self.HEAD], far[self.HEAD:]

    def max_over(self, samples: np.ndarray, limit: float) -> float:
        """``distances(samples, limit).max()``, the same value, without settling samples that cannot change it:
        the samples with the largest first bounds are settled first, and once a value above ``limit`` is found
        (it is exact) the samples whose first bound is not above it cannot hold the maximum. When no sample
        exceeds ``limit`` every candidate is settled, exactly as :meth:`distances` does."""
        if not len(samples):
            return 0.0
        if not self.m:
            return np.inf
        ub, head, tail = self._head_tail(samples, limit)
        best = -np.inf
        if len(head):
            self._settle(ub, head, samples, limit)
            best = float(ub[head].max())
        if len(tail):
            if best > limit:
                tail = tail[ub[tail] > best]
            if len(tail):
                self._settle(ub, tail, samples, limit)
                best = max(best, float(ub[tail].max()))
        return best if best > limit else float(ub.max())

    def exceeds(self, samples: np.ndarray, limit: float) -> bool:
        """True iff some sample is farther than ``limit`` from the surface (exact): the samples with the largest
        first bounds are settled first, the others only when none of those exceeds."""
        if not len(samples):
            return False
        if not self.m:
            return True
        ub, head, tail = self._head_tail(samples, limit)
        for ids in (head, tail):
            if len(ids):
                self._settle(ub, ids, samples, limit)
                if ub[ids].max() > limit:
                    return True
        return False

    def nearest(self, samples: np.ndarray) -> np.ndarray:
        """Index of the exactly nearest triangle of each sample (8 nearest centroids, then every triangle whose
        bounding box is closer than the distance found; on a tie the centroid search's pick stays, else the
        lowest index)."""
        n, k = len(samples), min(8, self.m)
        nn = np.asarray(self.tree.query(samples, k=k)[1]).reshape(n, k)
        f = nn.reshape(-1)
        d = _point_triangle_distance(np.repeat(samples, k, axis=0), self.a[f], self.b[f], self.c[f]).reshape(n, k)
        j = d.argmin(axis=1)
        best, arg = d[np.arange(n), j], nn[np.arange(n), j]
        for sl_ in self._chunks(n):
            sl = samples[sl_]
            s0 = sl_.start
            r2, c2 = self._pairs(sl, best[sl_])
            if len(r2):
                d2 = _point_triangle_distance(sl[r2], self.a[c2], self.b[c2], self.c[c2])
                order = np.lexsort((c2, d2, r2))  # per row, the smallest distance first, then the lowest index
                r2, c2, d2 = r2[order], c2[order], d2[order]
                first = np.r_[True, r2[1:] != r2[:-1]]
                rows = s0 + r2[first]
                better = d2[first] < best[rows]
                best[rows[better]], arg[rows[better]] = d2[first][better], c2[first][better]
        return arg

    def exact(self, samples: np.ndarray) -> np.ndarray:
        """Exact distance of every sample to the surface."""
        n = len(samples)
        if n * self.m <= self.DENSE_PAIRS:
            f = np.tile(np.arange(self.m), n)
            d = _point_triangle_distance(np.repeat(samples, self.m, axis=0), self.a[f], self.b[f], self.c[f])
            return d.reshape(n, self.m).min(axis=1)
        ub = self._first_bound(samples)
        self._settle(ub, np.arange(n), samples, -1.0)
        return ub

    def max_distance(self, samples: np.ndarray) -> float:
        """Exact max over samples of the distance to the surface. Small problems: every sample against every
        triangle at once. Otherwise per-sample upper bounds (nearest centroid's triangle) first, then samples in
        order of decreasing bound, each batch tightened with the 8 nearest centroids and made exact where it
        can still beat the best exact value, until no remaining bound can."""
        n = len(samples)
        if not n:
            return 0.0
        if not self.m:
            return np.inf
        if n * self.m <= self.DENSE_PAIRS:
            f = np.tile(np.arange(self.m), n)
            d = _point_triangle_distance(np.repeat(samples, self.m, axis=0), self.a[f], self.b[f], self.c[f])
            return float(d.reshape(n, self.m).min(axis=1).max())
        ub = self._first_bound(samples)
        best = 0.0
        for ids in self._batches(ub, -np.inf):
            if ub[ids[0]] <= best:  # no remaining upper bound can beat the exact maximum found
                break
            self._settle(ub, ids, samples, best)
            best = max(best, float(ub[ids].max()))
        return best

    def _refine(self, ub: np.ndarray, ids: np.ndarray, sl: np.ndarray, r2: np.ndarray, c2: np.ndarray) -> None:
        if len(r2):
            d = np.full(len(ids), np.inf)
            np.minimum.at(d, r2, _point_triangle_distance(sl[r2], self.a[c2], self.b[c2], self.c[c2]))
            ub[ids] = np.minimum(ub[ids], d)


def _one_sided(samples: np.ndarray, p: np.ndarray, t: np.ndarray, limit: float) -> float:
    """max over samples of the distance to surface (p, t), decided exactly against ``limit`` (exact whenever it
    exceeds ``limit``, otherwise an upper bound that is still <= ``limit``: the accept / reject decision is
    never wrong). See _SurfaceIndex.distances."""
    return _SurfaceIndex(p, t).max_over(np.asarray(samples, np.float64), limit)


def _coarse_samples(p1: np.ndarray, t1: np.ndarray) -> np.ndarray:
    """Centroids and edge midpoints of the triangles, each shared edge's midpoint once (both triangles of an
    edge give the same point, and a repeated sample cannot change a maximum)."""
    a, b, c = p1[t1[:, 0]], p1[t1[:, 1]], p1[t1[:, 2]]
    e = np.sort(np.concatenate([t1[:, [0, 1]], t1[:, [1, 2]], t1[:, [2, 0]]]), axis=1).astype(np.int64)
    key = np.unique(e[:, 0] * (int(e.max(initial=0)) + 1) + e[:, 1])
    i, j = np.divmod(key, int(e.max(initial=0)) + 1)
    return np.vstack([(a + b + c) / 3, (p1[i] + p1[j]) / 2])


def _distinct_points(p: np.ndarray) -> np.ndarray:
    """The rows of p without exact repeats (a seam-split mesh repeats every seam position once per chart)."""
    p = np.ascontiguousarray(p, np.float64)
    if not len(p):
        return p
    _, first = np.unique(p.view(np.dtype((np.void, p.dtype.itemsize * p.shape[1]))).ravel(), return_index=True)
    return p[np.sort(first)]


def _surface_deviation(p0: np.ndarray, t0: np.ndarray, p1: np.ndarray, t1: np.ndarray, limit: float,
                       ref_index: _SurfaceIndex | None = None, ref_vertices: np.ndarray | None = None) -> float:
    """Two-sided surface distance (a sampled Hausdorff distance) between a mesh (p0, t0) and its
    simplification (p1, t1), in the units of the points, decided exactly against ``limit`` (see
    _SurfaceIndex.max_over). Reference -> simplified: the reference's vertices (where an edge collapse moves
    the surface most). Simplified -> reference: centroids and edge midpoints of the coarse triangles (chords and
    bulges; its vertices are reference vertices, at distance 0). ``ref_index``, ``ref_vertices``: a prebuilt
    index of (p0, t0) and its distinct used vertices to reuse. A value above ``limit`` is exact for the side
    that exceeded it (the other side is then not measured)."""
    d1 = _SurfaceIndex(p1, t1).max_over(_distinct_points(p0[np.unique(t0)]) if ref_vertices is None else ref_vertices, limit)
    if d1 > limit:  # already rejected (and exact): skip the other side
        return d1
    ref = ref_index if ref_index is not None else _SurfaceIndex(p0, t0)
    return float(max(d1, ref.max_over(_coarse_samples(p1, t1), limit)))


# 13 view directions (axes, face and body diagonals; opposite directions project identically)
_VIEW_DIRS = np.array([(1, 0, 0), (0, 1, 0), (0, 0, 1), (1, 1, 0), (1, -1, 0), (1, 0, 1), (1, 0, -1), (0, 1, 1),
                       (0, 1, -1), (1, 1, 1), (1, 1, -1), (1, -1, 1), (-1, 1, 1)], np.float64)
_VIEW_DIRS /= np.linalg.norm(_VIEW_DIRS, axis=1, keepdims=True)


def _projected_areas(p: np.ndarray, t: np.ndarray) -> np.ndarray:
    """Silhouette area of a closed mesh seen along each of _VIEW_DIRS: (1/2) sum |n . d| a (Cauchy's
    projection formula; exact for convex parts, counts overlapping layers otherwise)."""
    n = np.cross(p[t[:, 1]] - p[t[:, 0]], p[t[:, 2]] - p[t[:, 0]]) / 2  # area-weighted normals
    # in row blocks: one (n, 3) x (3, 13) product hands a large n to the threaded BLAS path, measured ten times
    # slower than the same product in blocks, which gives a bit-identical result
    proj = np.vstack([n[i:i + 4096] @ _VIEW_DIRS.T for i in range(0, len(n), 4096)]) if len(n) else np.zeros((0, len(_VIEW_DIRS)))
    return 0.5 * np.abs(proj).sum(axis=0)


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
        cid = _canon(self.pts64)
        first = np.unique(cid, return_index=True)[1]
        if uvs is None:  # weld so duplicated hard-edge verts do not freeze the mesh
            pts = np.ascontiguousarray(pts[first])
            tri = cid[tri]
            self.uv = None
        else:
            # The copies of a vertex on a UV seam must be at exactly the same position: meshoptimizer joins them
            # by bitwise equality, and copies that differ by rounding noise (a seam closed with sin(2 pi), which is
            # -2.4e-16 and not 0) are collapsed separately, which opens the seam into hairline cracks.
            pts = np.ascontiguousarray(pts[first][cid])
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
        self._ref_vertices: np.ndarray | None = None  # the reference's used vertices (its side's samples), likewise

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
            if sil <= silhouette_tol and self._ref_vertices is None:
                self._ref_vertices = _distinct_points(self.ref[0][np.unique(self.ref[1])])
            dev = (_surface_deviation(self.ref[0], self.ref[1], self.ref[0], idx.reshape(-1, 3).astype(np.int64), limit,
                                      self.ref_index, self._ref_vertices) if sil <= silhouette_tol else np.inf)
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


LOD_DROP_MAX_PIECES = 200_000  # a part needing more bisected pieces than this to settle is kept (conservative)


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
    512 px); :meth:`iou` compares another version of the asset against them (smallest IoU over the views).
    The 13 views are laid side by side in one strip and rasterised in a single call."""

    def __init__(self, asset0: Sequence[tuple[np.ndarray, np.ndarray]], object_px: float) -> None:
        p0, t0 = _gather(asset0)
        self.centre = (p0.min(0) + p0.max(0)) / 2
        radius = max(float(np.linalg.norm(p0.max(0) - p0.min(0))) / 2, 1e-9)
        px = min(float(object_px), 512.0)
        self.size = int(max(16, np.ceil(px * 1.1)))
        self.scale = px / (2 * radius)
        frames = []
        for d in _VIEW_DIRS:
            r = np.cross(d, [0, 0, 1.0] if abs(d[2]) < 0.9 else [1.0, 0, 0])
            r /= np.linalg.norm(r)
            frames.append((d, r, np.cross(r, d)))
        self.frames = frames
        self.masks0 = self._masks(p0, t0)

    def _masks(self, p: np.ndarray, t: np.ndarray) -> np.ndarray:
        """(13, size, size) boolean silhouettes; each view is one tile of a 4 x 4 grid rasterised in one call."""
        from antigravity3d.kernel.raster import _rasterise

        v, sz, g = len(self.frames), self.size, 4
        if not len(t):
            return np.zeros((v, sz, sz), bool)
        q = p - self.centre
        xy, key, tris = [], [], []
        for i, (d, r, u) in enumerate(self.frames):
            ox, oy = (i % g) * sz, (i // g) * sz
            xy.append(np.stack([q @ r * self.scale + sz / 2 + ox, sz / 2 - q @ u * self.scale + oy], axis=1))
            key.append(q @ d)
            tris.append(t + i * len(q))
        hit = _rasterise(np.vstack(xy), np.concatenate(key).astype(np.float32), np.vstack(tris), g * sz)[0] >= 0
        return np.stack([hit[(i // g) * sz:(i // g + 1) * sz, (i % g) * sz:(i % g + 1) * sz] for i in range(v)])

    def iou(self, version: Sequence[tuple[np.ndarray, np.ndarray]]) -> float:
        m1 = self._masks(*_gather(version))
        union = (self.masks0 | m1).sum(axis=(1, 2))
        inter = (self.masks0 & m1).sum(axis=(1, 2))
        return float(np.min(np.where(union > 0, inter / np.maximum(union, 1), 1.0)))


def _bounds_radius(points: Sequence[np.ndarray]) -> float:
    p = [np.asarray(x, np.float64) for x in points if len(x)]
    lo = np.min([x.min(axis=0) for x in p], axis=0)
    hi = np.max([x.max(axis=0) for x in p], axis=0)
    return float(np.linalg.norm(hi - lo)) / 2


def _absent(s: "_Simplifier", screen_fraction: float) -> LOD:
    """A part that is not drawn at this level: no points, no triangles (``tri_count == 0``)."""
    return LOD(0.0, np.zeros((0, 3)), np.zeros((0, 3), np.int32), None if s.uv is None else np.zeros((0, 2), np.float32),
               0.0, 0.0, float(screen_fraction))


def _convex_bound(tri: np.ndarray, cand: tuple[np.ndarray, np.ndarray, np.ndarray],
                  partner: tuple[np.ndarray, np.ndarray, np.ndarray]) -> np.ndarray:
    """Per triangle S (corners (n, 3, 3)) the bound of :func:`_convex_upper` from 4 candidate triangles per S:
    ``cand`` and ``partner`` are their corners and those of their coplanar partners, (4 n, 3) each."""
    n = len(tri)
    pts = np.repeat(tri, 4, axis=0).reshape(-1, 3)  # per S: 4 candidates x 3 corners
    d = _point_triangle_distance(pts, *(np.repeat(x, 3, axis=0) for x in cand))
    # T joined with its coplanar partner is still convex: distance = min of the halves
    d = np.minimum(d, _point_triangle_distance(pts, *(np.repeat(x, 3, axis=0) for x in partner))).reshape(n, 4, 3)
    return d.max(axis=2).min(axis=1)


def _convex_upper(index: "_SurfaceIndex", tri: np.ndarray, *, return_candidates: bool = False):
    """Per triangle S (corners (n, 3, 3)) an upper bound of max over x in S of the distance from x to the
    indexed surface: for any one indexed triangle T, x -> dist(x, T) is convex, so its maximum over S is at a
    corner, and the distance to the surface is <= the distance to T. T ranges over the exactly nearest
    triangles of S's corners and centroid. An exact upper bound, with no sampling term."""
    n = len(tri)
    q = np.concatenate([tri, tri.mean(axis=1, keepdims=True)], axis=1).reshape(-1, 3)
    cand = index.nearest(q).reshape(n, 4)
    c, pt = cand.reshape(-1), index.partner[cand.reshape(-1)]
    up = _convex_bound(tri, (index.a[c], index.b[c], index.c[c]), (index.a[pt], index.b[pt], index.c[pt]))
    return (up, cand) if return_candidates else up


class _Removal:
    """Decides, per level, which parts may be absent: a part P may go when the largest distance D from its LOD0
    surface to what remains drawn is within the level's budget, and the asset's silhouette keeps the pop
    tolerance. Everything that does not depend on the level is computed once and reused by every level.

    D is bracketed exactly, without a sampling term: lo = the largest exact distance of P's vertices (D >= lo),
    hi = the largest convex upper bound of P's triangles (see _convex_upper; D <= hi). The bracket against the
    other parts at LOD0 minus the parts already gone is cached per (part, remaining set). Remaining parts may be
    simplified at a level, which moves their surface by at most their measured deviation d, so a level compares
    [lo - d, hi + d] with its budget. Only a budget inside that bracket triggers work that is not cached:
    bisecting just the triangles whose bound is above the budget, against the level's own meshes."""

    def __init__(self, simp: dict[str, "_Simplifier"], silhouette_iou: float) -> None:
        self.simp = simp
        self.min_iou = silhouette_iou
        self.area = {n: float(_tri_areas(s.ref[0], s.ref[1], 3).sum()) for n, s in simp.items()}
        self.tris = {n: len(s.idx0) // 3 for n, s in simp.items()}
        self._index: dict[frozenset, _SurfaceIndex] = {}
        # (part, remaining parts) -> [lo, hi or None, parts owning lo's nearest triangles, parts owning hi's]
        self._bracket: dict[tuple[str, frozenset], list] = {}
        self._asset0 = [(s.ref[0], s.ref[1]) for s in simp.values()]
        self._sil: dict[float, _Silhouettes] = {}
        self._probe: dict[str, np.ndarray] = {}
        self._query: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        self._part: dict[str, _SurfaceIndex] = {}
        self._near: dict[tuple[str, str, str], tuple[np.ndarray, np.ndarray]] = {}
        self._lo_search: dict[tuple[str, frozenset], dict] = {}
        used = {n: s.ref[0][np.unique(s.ref[1])] for n, s in simp.items()}
        self._lo3 = {n: p.min(axis=0) for n, p in used.items()}  # bounding boxes of the parts' LOD0 surfaces
        self._hi3 = {n: p.max(axis=0) for n, p in used.items()}

    def _rest_index(self, rest: frozenset) -> _SurfaceIndex:
        if rest not in self._index:
            names = sorted(rest)
            index = _SurfaceIndex(*_gather([(self.simp[n].ref[0], self.simp[n].ref[1]) for n in names]))
            index.owner = np.repeat(np.array(names, dtype=object), [len(self.simp[n].idx0) // 3 for n in names])
            self._index[rest] = index
        return self._index[rest]

    def _part_index(self, nid: str) -> _SurfaceIndex:
        if nid not in self._part:
            self._part[nid] = _SurfaceIndex(*self.simp[nid].ref)
        return self._part[nid]

    def _probes(self, nid: str) -> np.ndarray:
        """The part's vertices and triangle centroids (each distinct position once)."""
        if nid not in self._probe:
            p, t = self.simp[nid].ref
            a, b, c = p[t[:, 0]], p[t[:, 1]], p[t[:, 2]]
            self._probe[nid] = _distinct_points(np.vstack([p[np.unique(t)], (a + b + c) / 3]))
        return self._probe[nid]

    def _queries(self, nid: str) -> tuple[np.ndarray, np.ndarray]:
        """(the part's triangles as corners (n, 3, 3), their corners and centroids (4 n, 3)): _convex_upper's
        query points."""
        if nid not in self._query:
            s = self.simp[nid]
            tri = s.ref[0][s.ref[1]]
            self._query[nid] = (tri, np.concatenate([tri, tri.mean(axis=1, keepdims=True)], axis=1).reshape(-1, 3))
        return self._query[nid]

    def _nearest_in(self, kind: str, nid: str, q: str, pts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(exact distance, nearest triangle of part q) of each of nid's probe ("probe") or query ("query")
        points, cached: it does not depend on the level or on which other parts remain."""
        key = (kind, nid, q)
        if key not in self._near:
            index = self._part_index(q)
            n, m = len(pts), index.m
            if n * m <= index.DENSE_PAIRS:  # small: every point against every triangle at once
                f = np.tile(np.arange(m), n)
                d = _point_triangle_distance(np.repeat(pts, m, axis=0), index.a[f], index.b[f], index.c[f]).reshape(n, m)
                j = d.argmin(axis=1)
                self._near[key] = (d[np.arange(n), j], j)
            else:
                j = index.nearest(pts)
                self._near[key] = (_point_triangle_distance(pts, index.a[j], index.b[j], index.c[j]), j)
        return self._near[key]

    def _to_rest(self, kind: str, nid: str, pts: np.ndarray, rest: frozenset) -> tuple[np.ndarray, list, np.ndarray, np.ndarray]:
        """Per point: exact distance to the remaining parts' LOD0 surface, then the owning part (an index into
        the returned names) and triangle of a nearest triangle. Distances to the union are the minimum over the
        parts, from per-part results cached across remaining sets and levels; a part whose bounding box is
        farther from every point than the distance already found cannot lower it and is not measured."""
        names = sorted(rest)
        gap = np.stack([np.linalg.norm(np.maximum(np.maximum(self._lo3[q] - pts, pts - self._hi3[q]), 0.0), axis=1)
                        for q in names])
        cur = np.full(len(pts), np.inf)
        own = np.zeros(len(pts), np.int64)
        tri = np.zeros(len(pts), np.int64)
        for qi in sorted(range(len(names)), key=lambda i: (float(gap[i].min()), names[i])):
            if (gap[qi] > cur * (1 + 1e-9) + 1e-12).all():  # (with a rounding margin) farther than what was found
                continue
            d, j = self._nearest_in(kind, nid, names[qi], pts)
            better = d < cur
            cur[better], own[better], tri[better] = d[better], qi, j[better]
        return cur, names, own, tri

    def _entry(self, nid: str, rest: frozenset) -> list:
        """[lo, hi, parts owning the triangles hi was measured against] of (part, remaining set), all None at
        first. lo is an exact distance of some point of the part, so it stays a lower bound for any smaller
        remaining set (distances only grow when parts go); hi stays an upper bound for a smaller set as long as
        the triangles it used are all still there."""
        return self._bracket.setdefault((nid, rest), [None, None, None])

    def _lo_known(self, nid: str, rest: frozenset) -> float:
        return max((e[0] for (n, r), e in self._bracket.items() if n == nid and rest <= r and e[0] is not None), default=0.0)

    def _hi_known(self, nid: str, rest: frozenset) -> float:
        return min((e[1] for (n, r), e in self._bracket.items()
                    if n == nid and rest <= r and e[1] is not None and e[2] is not None and e[2] <= rest), default=np.inf)

    def _lo(self, nid: str, rest: frozenset, need: float = np.inf) -> float:
        """Exact largest distance to ``rest`` at LOD0 of the part's vertices and triangle centroids: a lower
        bound of D that already rejects a long pole or a wide board (their centres are far from everything
        else). The search stops once an exact distance above ``need`` is found (any exact distance of a point
        of the part is a lower bound of D, and that one already decides) and resumes if a later question needs
        more: the largest value found so far is returned."""
        e = self._entry(nid, rest)
        st = self._lo_search.get((nid, rest))
        if st is None:
            st = self._lo_search[(nid, rest)] = self._lo_start(nid, rest)
        if not st["done"] and max(e[0] or 0.0, st["best"]) <= need:
            self._lo_resume(nid, st, need)
        e[0] = max(e[0] or 0.0, st["best"])
        return e[0]

    def _lo_start(self, nid: str, rest: frozenset) -> dict:
        """State of the search for the largest probe distance to ``rest``. Per (part, other part), cached
        across remaining sets and levels: an upper bound per probe (nearest centroid's triangle) and exact
        distances computed only where needed. Probes are made exact in order of decreasing upper bound until no
        remaining bound can beat the largest exact value; another part only takes part where its bounding box
        is closer to the probe than the bound."""
        x = self._probes(nid)
        names = sorted(rest)
        gap = {q: self._box_gap(nid, q) for q in names}
        up = np.full(len(x), np.inf)
        for q in sorted(names, key=lambda q: (float(gap[q].min()), q)):
            if (gap[q] > up * (1 + 1e-9) + 1e-12).all():  # (with a rounding margin) farther than the bound
                continue
            up = np.minimum(up, self._pair(nid, q)[0])
        near = [q for q in names if not (gap[q] > up * (1 + 1e-9) + 1e-12).all()]
        return {"up": up, "gap": gap, "near": near, "order": np.argsort(-up, kind="stable"), "pos": 0, "step": 16,
                "best": 0.0, "done": False}

    def _lo_resume(self, nid: str, st: dict, need: float) -> None:
        up, gap, order = st["up"], st["gap"], st["order"]
        while st["pos"] < len(order):
            ids = order[st["pos"]:st["pos"] + st["step"]]
            if up[ids[0]] <= st["best"]:  # no remaining upper bound can beat the exact maximum found
                break
            d = up[ids].copy()
            for q in st["near"]:
                mask = ~(gap[q][ids] > d * (1 + 1e-9) + 1e-12)
                if mask.any():
                    d[mask] = np.minimum(d[mask], self._pair_exact(nid, q, ids[mask]))
            st["best"] = max(st["best"], float(d.max()))
            st["pos"], st["step"] = st["pos"] + st["step"], min(st["step"] * 2, 4096)
            if st["best"] > need:
                return
        st["done"] = True

    def _box_gap(self, nid: str, q: str) -> np.ndarray:
        """Per probe of nid, its distance to part q's bounding box (a lower bound of its distance to q)."""
        key = ("gap", nid, q)
        if key not in self._near:
            x = self._probes(nid)
            self._near[key] = np.linalg.norm(np.maximum(np.maximum(self._lo3[q] - x, x - self._hi3[q]), 0.0), axis=1)
        return self._near[key]

    def _pair(self, nid: str, q: str) -> list:
        """[upper bound, exact distance or NaN] per probe of nid against part q (see _lo_start)."""
        key = ("pair", nid, q)
        if key not in self._near:
            x, index = self._probes(nid), self._part_index(q)
            if len(x) * index.m <= index.DENSE_PAIRS:  # small: exact outright, cheaper than bounding first
                d = index.exact(x)
                self._near[key] = [d, d.copy()]
            else:
                self._near[key] = [index._first_bound(x), np.full(len(x), np.nan)]
        return self._near[key]

    def _pair_exact(self, nid: str, q: str, ids: np.ndarray) -> np.ndarray:
        _, ex = self._pair(nid, q)
        todo = ids[np.isnan(ex[ids])]
        if len(todo):
            ex[todo] = self._part_index(q).exact(self._probes(nid)[todo])
        return ex[ids]

    def _hi(self, nid: str, rest: frozenset) -> float:
        """Largest convex upper bound of the part's triangles against ``rest`` at LOD0 (D <= hi; see
        _convex_upper: T ranges over a nearest triangle of the remaining parts for each corner and centroid)."""
        e = self._entry(nid, rest)
        if e[1] is None:
            tri, q = self._queries(nid)
            _, names, own, t = self._to_rest("query", nid, q, rest)
            cand = [np.zeros((len(q), 3)) for _ in range(6)]
            for qi in np.unique(own):
                index, m = self._part_index(names[qi]), own == qi
                j, pj = t[m], index.partner[t[m]]
                for k, x in enumerate((index.a[j], index.b[j], index.c[j], index.a[pj], index.b[pj], index.c[pj])):
                    cand[k][m] = x
            up = _convex_bound(tri, tuple(cand[:3]), tuple(cand[3:]))
            e[1], e[2] = float(up.max()), frozenset(names[qi] for qi in np.unique(own))
        return e[1]

    @staticmethod
    def _settle(index: _SurfaceIndex, tri: np.ndarray, budget: float) -> tuple[bool, float]:
        """(D <= budget?, a new exact lower bound of D). Bisects (longest edge) only the triangles whose convex
        bound exceeds the budget; an edge midpoint farther than the budget proves "no"; too many pieces keeps
        the part (conservative)."""
        pieces, lo = 0, 0.0
        while len(tri):
            up = _convex_upper(index, tri)
            tri = tri[up > budget]
            if not len(tri):
                return True, lo
            e = np.stack([np.linalg.norm(tri[:, (i + 1) % 3] - tri[:, i], axis=1) for i in range(3)], axis=1)
            k = e.argmax(axis=1)
            rows = np.arange(len(tri))
            a, b, c = tri[rows, k], tri[rows, (k + 1) % 3], tri[rows, (k + 2) % 3]
            mid = (a + b) / 2
            if index.exceeds(mid, budget):
                return False, index.max_distance(mid)
            pieces += 2 * len(tri)
            if pieces > LOD_DROP_MAX_PIECES:
                return False, lo
            tri = np.concatenate([np.stack([a, mid, c], axis=1), np.stack([mid, b, c], axis=1)])
        return True, lo

    def _decide(self, nid: str, rest: list[str], picks: dict[str, tuple[np.ndarray, float]], budget: float) -> bool:
        r = frozenset(rest)
        slack = max(picks[n][1] for n in rest)
        if self._lo_known(nid, r) - slack > budget:  # bounds already known (here or for a larger set) settle it
            return False
        if self._hi_known(nid, r) + slack <= budget:
            return True
        if self._lo(nid, r, budget + slack) - slack > budget:
            return False
        hi = self._hi(nid, r)
        # hi used triangles of a few parts only: each moved by at most its own measured deviation
        if hi + max((picks[n][1] for n in self._entry(nid, r)[2] & r), default=0.0) <= budget:
            return True
        s = self.simp[nid]
        if slack == 0:  # what is learnt about D against the LOD0 rest is kept for every later level
            ok, found = self._settle(self._rest_index(r), s.ref[0][s.ref[1]], budget)
            e = self._entry(nid, r)
            e[0] = max(e[0], found)
            if ok:  # proved against this remaining set only
                e[1], e[2] = min(e[1], budget), r
            return ok
        index = _SurfaceIndex(*_gather([(self.simp[n].ref[0], picks[n][0].reshape(-1, 3)) for n in rest]))
        if index.exceeds(self._probes(nid), budget):  # the level's own meshes: one point farther settles "no"
            return False
        return self._settle(index, s.ref[0][s.ref[1]], budget)[0]

    def droppable(self, picks: dict[str, tuple[np.ndarray, float]], already: set[str], budget: float, object_px: float,
                  needed: int) -> set[str]:
        """Greedy set of parts absent at this level (smallest surface first, ties by name; parts absent at the
        previous level stay absent; never every part). ``needed``: triangles removal must save for the level to
        be worth emitting; when even removing every candidate cannot save that many, nothing is measured, and the
        search stops as soon as the parts not yet decided could no longer make up the difference (the level is
        not emitted then, whatever the rest of the search would find)."""
        gone = set(already)
        order = sorted((n for n in self.simp if n not in gone), key=lambda n: (self.area[n], n))
        if len(order) < 2 or sum(self.tris[n] for n in order) - min(self.tris[n] for n in order) < needed:
            return gone
        saves = [len(picks[n][0]) // 3 for n in order]  # what absence saves at this level
        saved, undecided = 0, sum(saves)
        new: list[str] = []
        for nid, save in zip(order, saves):
            if saved + undecided < needed:
                return gone
            undecided -= save
            rest = [n for n in self.simp if n not in gone and n != nid]
            if not rest:
                break
            if self._decide(nid, rest, picks, budget):
                gone.add(nid)
                new.append(nid)
                saved += save
        if not new or saved < needed:
            return gone
        if object_px not in self._sil:
            self._sil[object_px] = _Silhouettes(self._asset0, object_px)
        sil = self._sil[object_px]

        def view(absent: set[str]):
            return [(self.simp[n].ref[0], picks[n][0].reshape(-1, 3)) for n in self.simp if n not in absent]

        while new and sil.iou(view(gone)) < self.min_iou:  # test the set once; back off the latest part only if it fails
            gone.discard(new.pop())
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
    removal = _Removal(simp, 1.0 - silhouette_tol) if len(simp) > 1 else None
    for f in fr[1:]:
        budget = ld.lod_error_budget_m(radius, f, error_px=error_px, screen_px=screen_px)
        picks = {nid: s.within(budget, silhouette_tol) for nid, s in simp.items()}
        kept = sum(len(idx) // 3 for nid, (idx, _) in picks.items() if nid not in dropped)
        needed = kept - int((1.0 - min_reduction) * prev_tris)  # what removal must still save for this level to pay
        gone = removal.droppable(picks, dropped, budget, f * screen_px, needed) if removal is not None else set()
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
    source: Literal["fast_path", "coacd", "merged", "body_hull"]
    #: why a hull is not what the mesh asked for: the repair the input needed, or why decomposition gave way to the hull of a body.
    #: "body_hull" is the convex hull of a whole body, the fallback where no decomposition finished or none was possible.
    reason: str = ""


def _hull(points: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
    from scipy.spatial import ConvexHull

    # Hulls ship as float32 points, and the package check (validator._is_convex) runs Qhull again on those: a point
    # Qhull kept as a vertex of a flat facet in float64 can fall a rounding error inside it in float32 and be dropped,
    # and the hull is then reported "not convex". Measured: the merged hull of a turned knob and a ring (88 points,
    # flat caps of 16 and 20 coplanar points) lost 9 vertices that way. So the hull is taken of the float32 values
    # and again of its own vertices until Qhull keeps all of them (once or twice in practice).
    points = np.asarray(points, np.float64).astype(np.float32).astype(np.float64)
    for _ in range(4):
        h = ConvexHull(points)
        used = np.unique(h.simplices)
        if len(used) == len(points):
            break
        points = points[used]
    remap = np.zeros(len(points), np.int64)
    remap[used] = np.arange(len(used))
    tris = remap[h.simplices]
    pts = points[used]
    c = pts.mean(axis=0)
    p = pts[tris]
    flip = np.einsum("ij,ij->i", np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]), p[:, 0] - c) < 0
    tris[flip] = tris[flip][:, ::-1]
    return pts, tris.astype(np.int32), float(h.volume)


# CoACD's time grows with its input's triangle count, and a modelled solid is often a few dozen planar faces
# cut into thousands of triangles. Fewer triangles for the same surface is work removed, not quality traded.
def _ear_clip(poly, xy, area_tol):
    """Triangulate a simple polygon given as vertex ids, counter-clockwise in the ``xy`` map (ids may repeat where a hole is bridged in).

    A small polygon takes the ear whose smallest angle is largest, which keeps slivers out; a large one takes the first valid ear.
    Returns None when only zero-area triangles remain."""
    poly = list(poly)
    out = []
    while len(poly) > 3:
        k = len(poly)
        pts = [xy[v] for v in poly]
        conv = [(pts[i][0]-pts[i-1][0])*(pts[(i+1) % k][1]-pts[i-1][1]) - (pts[i][1]-pts[i-1][1])*(pts[(i+1) % k][0]-pts[i-1][0]) for i in range(k)]
        reflex = [i for i in range(k) if conv[i] <= area_tol]
        pick = -1
        best = 0.0
        for i in range(k):
            if conv[i] <= area_tol:
                continue
            ia, ic = i-1, (i+1) % k
            ax, ay = pts[ia]; bx, by = pts[i]; cx, cy = pts[ic]
            ids = (poly[ia], poly[i], poly[ic])
            ok = True
            for d in reflex:
                if poly[d] in ids:
                    continue
                dx, dy = pts[d]
                if ((bx-ax)*(dy-ay) - (by-ay)*(dx-ax) >= -area_tol and (cx-bx)*(dy-by) - (cy-by)*(dx-bx) >= -area_tol
                        and (ax-cx)*(dy-cy) - (ay-cy)*(dx-cx) >= -area_tol):
                    ok = False
                    break
            if not ok:
                continue
            if k > 8:
                pick = i
                break
            l1 = (bx-ax)**2 + (by-ay)**2; l2 = (cx-bx)**2 + (cy-by)**2; l3 = (ax-cx)**2 + (ay-cy)**2
            score = conv[i] / max(l1*l2, l2*l3, l3*l1)
            if pick < 0 or score > best + 1e-15:
                pick, best = i, score
        if pick < 0:
            return None
        out.append((poly[pick-1], poly[pick], poly[(pick+1) % k]))
        del poly[pick]
    a, b, c = poly
    (ax, ay), (bx, by), (cx, cy) = xy[a], xy[b], xy[c]
    if (bx-ax)*(cy-ay) - (by-ay)*(cx-ax) <= area_tol:
        return None
    out.append((a, b, c))
    return out


def _bridge(outer, holes, xy, eps, area_tol):
    """Join each hole (clockwise) to the outer loop (counter-clockwise) by the shortest bridge that crosses no edge and touches no
    other vertex, so that the result is one simple polygon. None when a hole cannot be reached."""
    outer = list(outer)
    pending = sorted(holes, key=lambda h: -max(xy[v][0] for v in h))
    for hi, hole in enumerate(pending):
        edges = []
        for loop in [outer] + pending[hi:]:
            for i in range(len(loop)):
                edges.append((loop[i], loop[(i+1) % len(loop)]))
        verts = set(v for e in edges for v in e)
        cands = sorted(((xy[o][0]-xy[h][0])**2 + (xy[o][1]-xy[h][1])**2, oi, hj) for oi, o in enumerate(outer) for hj, h in enumerate(hole))
        done = False
        for _, oi, hj in cands:
            p, q = outer[oi], hole[hj]
            (px, py), (qx, qy) = xy[p], xy[q]
            L = math.hypot(qx-px, qy-py)
            if L <= eps:
                continue
            ok = True
            for w in verts:
                if w == p or w == q:
                    continue
                wx, wy = xy[w]
                t = ((wx-px)*(qx-px) + (wy-py)*(qy-py)) / (L*L)
                t = min(1.0, max(0.0, t))
                if math.hypot(px + t*(qx-px) - wx, py + t*(qy-py) - wy) <= eps:
                    ok = False
                    break
            if ok:
                for r, s in edges:
                    if r in (p, q) or s in (p, q):
                        continue
                    (rx, ry), (sx, sy) = xy[r], xy[s]
                    o1 = (qx-px)*(ry-py) - (qy-py)*(rx-px); o2 = (qx-px)*(sy-py) - (qy-py)*(sx-px)
                    if (o1 > area_tol and o2 > area_tol) or (o1 < -area_tol and o2 < -area_tol):
                        continue
                    o3 = (sx-rx)*(py-ry) - (sy-ry)*(px-rx); o4 = (sx-rx)*(qy-ry) - (sy-ry)*(qx-rx)
                    if (o3 > area_tol and o4 > area_tol) or (o3 < -area_tol and o4 < -area_tol):
                        continue
                    ok = False
                    break
            if ok:
                outer = outer[:oi+1] + hole[hj:] + hole[:hj] + [hole[hj], p] + outer[oi+1:]
                done = True
                break
        if not done:
            return None
    return outer


def _merge_planar(points, tris, rel_tol=1e-8, min_gain=0.8):
    """Merge coplanar triangles of a closed, consistently wound, welded mesh into the fewest triangles that cover the same faces.

    Neighbouring triangles whose planes agree join into a region. A vertex strictly inside a region, or on a straight edge between
    exactly two regions, is dropped; each region is then re-triangulated from its boundary loops (holes bridged in). A region whose
    vertices stray more than ``2 * rel_tol * diagonal`` from its plane, or that cannot be re-triangulated, keeps its triangles and its
    boundary vertices. Dropped vertices lie within ``2 * rel_tol * diagonal`` of the new surface, so for planar input the surface is
    unchanged and the vertex set of the convex hull is too. Returns ``(points, tris)``, or None when the gain would be below
    ``1 - min_gain`` of the triangles (nothing worth a pass) or the mesh does not qualify."""
    P = np.asarray(points, np.float64)
    F = np.asarray(tris, np.int64)
    m, n = len(F), len(P)
    diag = float(np.linalg.norm(P.max(axis=0) - P.min(axis=0)))
    eps = rel_tol * diag
    area_tol = 1e-10 * diag * diag
    p0, p1, p2 = P[F[:, 0]], P[F[:, 1]], P[F[:, 2]]
    cr = np.cross(p1 - p0, p2 - p0)
    ar = np.linalg.norm(cr, axis=1)
    if (ar <= area_tol).any():
        return None
    N = cr / ar[:, None]
    # half-edge i = 3*f + s goes F[f,s] -> F[f,(s+1)%3]
    ha = F.reshape(-1)
    hb = np.roll(F, -1, axis=1).reshape(-1)
    key = ha * n + hb
    order = np.argsort(key)
    sk = key[order]
    rk = hb * n + ha
    pos = np.searchsorted(sk, rk)
    pos[pos >= len(sk)] = 0
    if not (sk[pos] == rk).all():
        return None
    twin = order[pos]
    f1 = np.arange(3*m) // 3
    f2 = twin // 3
    opp = F[f2, (twin % 3 + 2) % 3]           # third vertex of the neighbouring face
    flat = (np.linalg.norm(N[f1] - N[f2], axis=1) <= 1e-6) & (np.abs(np.einsum("ij,ij->i", N[f1], P[opp] - P[ha])) <= eps)
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    g = coo_matrix((np.ones(int(flat.sum())), (f1[flat], f2[flat])), shape=(m, m))
    nreg, reg = connected_components(g, directed=False)
    if nreg > min_gain * m:
        return None
    # Each vertex dropped from a closed mesh removes two triangles (Euler), and only a vertex touching one region, or two, can go.
    pair = np.unique(F.reshape(-1) * nreg + np.repeat(reg, 3))
    pv, pr = pair // nreg, pair % nreg
    touching = np.bincount(pv, minlength=n)
    if 2 * int(((touching == 1) | (touching == 2)).sum()) < (1.0 - min_gain) * m:
        return None
    # plane fit per region; a region whose vertices stray from its plane stays as it is
    w = ar / 2
    nr = np.zeros((nreg, 3)); np.add.at(nr, reg, N * w[:, None])
    cen = np.zeros((nreg, 3)); np.add.at(cen, reg, (p0 + p1 + p2) / 3 * w[:, None])
    wsum = np.bincount(reg, w, nreg)
    cen /= wsum[:, None]
    ln = np.linalg.norm(nr, axis=1)
    nr /= np.maximum(ln, 1e-300)[:, None]
    dev = np.zeros(nreg)
    for pk in (p0, p1, p2):
        np.maximum.at(dev, reg, np.abs(np.einsum("ij,ij->i", pk - cen[reg], nr[reg])))
    pinned = (dev > 2 * eps) | (np.bincount(reg, minlength=nreg) == 1)
    # boundary half-edges per region
    bnd = np.flatnonzero(reg[f1] != reg[f2])
    bnd_reg = reg[f1[bnd]]
    o = np.argsort(bnd_reg, kind="stable")
    bnd, bnd_reg = bnd[o], bnd_reg[o]
    starts = np.searchsorted(bnd_reg, np.arange(nreg + 1))
    # the regions touching each vertex are in vregs
    vregs: dict[int, list[int]] = {}
    for v, r in zip(pv.tolist(), pr.tolist()):
        vregs.setdefault(v, []).append(r)
    ploops: dict[int, list[list[int]]] = {}
    for r in np.flatnonzero(~pinned).tolist():
        nxt = {}
        bad = False
        for h in bnd[starts[r]:starts[r+1]].tolist():
            a, b = int(ha[h]), int(hb[h])
            if a in nxt:
                bad = True
                break
            nxt[a] = b
        loops = []
        if not bad:
            seen = set()
            for s in sorted(nxt):
                if s in seen:
                    continue
                loop, cur = [], s
                while cur not in seen:
                    seen.add(cur); loop.append(cur)
                    cur = nxt.get(cur, -1)
                    if cur < 0:
                        bad = True
                        break
                if bad or cur != s:
                    bad = True
                    break
                loops.append(loop)
        if bad:
            pinned[r] = True
        else:
            ploops[r] = loops
    # 2-D frames
    def frame(r):
        x, y, z = nr[r].tolist()
        k = min(range(3), key=lambda i: abs((x, y, z)[i]))
        ax = [0.0, 0.0, 0.0]; ax[k] = 1.0
        u = (y*ax[2] - z*ax[1], z*ax[0] - x*ax[2], x*ax[1] - y*ax[0])
        ul = math.sqrt(u[0]**2 + u[1]**2 + u[2]**2)
        u = (u[0]/ul, u[1]/ul, u[2]/ul)
        return np.asarray(u), np.asarray((y*u[2] - z*u[1], z*u[0] - x*u[2], x*u[1] - y*u[0]))
    result: dict[int, list[tuple[int, int, int]]] = {}
    for _ in range(6):
        # vertices that may be dropped: interior to one region, or on a straight edge shared by exactly two regions
        ent: dict[int, list[tuple[int, int, int]]] = {}
        for r, loops in ploops.items():
            if pinned[r]:
                continue
            for loop in loops:
                k = len(loop)
                for i in range(k):
                    ent.setdefault(loop[i], []).append((r, loop[i-1], loop[(i+1) % k]))
        drop = set()
        for v, rs in vregs.items():
            if any(pinned[r] for r in rs):
                continue
            if len(rs) == 1:
                drop.add(v)
            elif len(rs) == 2:
                e = ent.get(v)
                if e is None or len(e) != 2 or e[0][1] != e[1][2] or e[0][2] != e[1][1]:
                    continue
                a, b = P[e[0][1]], P[e[0][2]]
                ab = b - a
                t = float(np.dot(P[v] - a, ab) / max(float(np.dot(ab, ab)), 1e-300))
                if 0.0 < t < 1.0 and np.linalg.norm(a + t * ab - P[v]) <= eps:
                    drop.add(v)
        changed = False
        result.clear()
        for r, loops in ploops.items():
            if pinned[r]:
                continue
            u, vv = frame(r)
            keepl = [[x for x in lp if x not in drop] for lp in loops]
            ids = sorted(set(x for lp in keepl for x in lp))
            if any(len(lp) < 3 for lp in keepl):
                pinned[r] = changed = True
                continue
            xy = {x: (float(np.dot(P[x], u)), float(np.dot(P[x], vv))) for x in ids}
            areas = [0.5 * sum(xy[lp[i]][0]*xy[lp[(i+1) % len(lp)]][1] - xy[lp[(i+1) % len(lp)]][0]*xy[lp[i]][1] for i in range(len(lp))) for lp in keepl]
            outs = [i for i, a in enumerate(areas) if a > 0]
            if len(outs) != 1 or sum(len(lp) for lp in keepl) > 400:
                pinned[r] = changed = True
                continue
            outer = keepl[outs[0]]
            holes = [lp for i, lp in enumerate(keepl) if i != outs[0]]
            poly = _bridge(outer, holes, xy, eps, area_tol) if holes else outer
            tri = _ear_clip(poly, xy, area_tol) if poly is not None else None
            if tri is not None:
                tot = sum(0.5 * ((xy[b][0]-xy[a][0])*(xy[c][1]-xy[a][1]) - (xy[b][1]-xy[a][1])*(xy[c][0]-xy[a][0])) for a, b, c in tri)
                if abs(tot - sum(areas)) > 1e-9 * max(abs(sum(areas)), 1e-300) + area_tol:
                    tri = None
            if tri is None:
                pinned[r] = changed = True
                continue
            result[r] = tri
        if not changed:
            break
    else:
        return None
    out = []
    keep_reg = np.isin(reg, [r for r in range(nreg) if r not in result])
    out.extend(map(tuple, F[keep_reg].tolist()))
    for r in sorted(result):
        out.extend(result[r])
    if len(out) >= m:
        return None
    f = np.asarray(out, np.int64)
    used = np.unique(f)
    rm = np.full(n, -1, np.int64); rm[used] = np.arange(len(used))
    return P[used], rm[f].astype(np.int32)


def _decomposition_input(pts: np.ndarray, tri: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The mesh CoACD should decompose: the same closed solid with coplanar triangles merged, or the input itself.

    Only a closed mesh is touched. The merged mesh must still be closed and manifold, with the same volume to within 1e-9."""
    cid = _canon(pts)
    wp = np.zeros((int(cid.max()) + 1, 3))
    wp[cid] = pts
    wt = cid[tri]
    if (wt[:, 0] == wt[:, 1]).any() or (wt[:, 1] == wt[:, 2]).any() or (wt[:, 0] == wt[:, 2]).any():
        return pts, tri
    merged = _merge_planar(wp, wt)
    if merged is None:
        return pts, tri
    before, after = _topology(wp, wt), _topology(*merged)
    if not after.closed or after.degenerate or after.duplicates or abs(after.volume - before.volume) > 1e-9 * abs(before.volume):
        return pts, tri
    return merged


def _closed_bodies(pts: np.ndarray, tri: np.ndarray) -> list[tuple[np.ndarray, np.ndarray]] | None:
    """The separate bodies of a closed mesh that holds several (as vertices and triangles each), or None when it holds one.
    Bodies that only touch at a vertex are separate.

    CoACD's manifold pre-pass costs seconds and is only skipped for a single body, but a mesh of closed bodies needs no repair:
    each body can be decomposed alone."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    cid = _canon(pts)
    t = cid[tri]
    n = int(cid.max()) + 1
    # faces are one body when they share an edge: bodies that only touch at a vertex are separate
    key = np.concatenate([np.minimum(t[:, i], t[:, (i + 1) % 3]) * n + np.maximum(t[:, i], t[:, (i + 1) % 3]) for i in range(3)])
    face = np.tile(np.arange(len(t)), 3)
    order = np.argsort(key, kind="stable")
    same = np.flatnonzero(key[order][1:] == key[order][:-1])
    a, b = face[order[same]], face[order[same + 1]]
    count, label = connected_components(coo_matrix((np.ones(len(a)), (a, b)), shape=(len(t), len(t))), directed=False)
    if count == 1:
        return None
    bodies = []
    for body in range(count):
        bt = tri[label == body]
        used = np.unique(bt)
        remap = np.zeros(len(pts), np.int64)
        remap[used] = np.arange(len(used))
        bodies.append((pts[used], remap[bt].astype(np.int32)))
    return bodies


#: One budget for every decomposition that runs in a child process, in one place. The slowest legitimate decomposition measured
#: (a curved concave solid, CoACD with the repair pass off) takes 0.9 ms per triangle, and a thin shell made solid (the hardest
#: class that still finishes) 6 ms per triangle up to 2,000 triangles, 12 s at most. The budget is three times the per-triangle
#: figure with a floor of 30 s: a run that exceeds it is not a slow decomposition but one that is not coming back.
DECOMPOSITION_BUDGET_FLOOR_S = 30.0
DECOMPOSITION_BUDGET_PER_TRIANGLE_S = 3 * 0.9e-3
#: A clean closed body is decomposed in this process (no process start on the common path) up to this many triangles, which
#: take about 25 s; beyond it the run is long enough to be worth a deadline.
IN_PROCESS_MAX_TRIANGLES = 25_000
#: Thickness of the solid made from an open surface, as a fraction of the body's extent. Measured on a cup, a bowl, a wavy sheet and
#: a sphere with a large opening: at 0.005 the hulls reach no further from the surface than the ones CoACD's own repair pass gave
#: (0.25% against 0.4%) and cover it better; each doubling doubles the hull volume claimed beyond the surface.
SHELL_THICKNESS = 0.005


def decomposition_budget_s(triangles: int) -> float:
    return max(DECOMPOSITION_BUDGET_FLOOR_S, DECOMPOSITION_BUDGET_PER_TRIANGLE_S * triangles)


def _solid_from_surface(pts: np.ndarray, tri: np.ndarray, thickness: float) -> tuple[np.ndarray, np.ndarray] | None:
    """A closed solid of the given thickness around an open, consistently wound surface (each vertex moved half the thickness
    to both sides along its normal, the boundary walled), or None when the result is not a closed solid."""
    from antigravity3d.kernel.collider_repair import _closed_volume

    f = np.asarray(tri, np.int64)
    n = len(pts)
    vn = np.zeros_like(pts)
    fn = np.cross(pts[f[:, 1]] - pts[f[:, 0]], pts[f[:, 2]] - pts[f[:, 0]])
    for k in range(3):
        np.add.at(vn, f[:, k], fn)
    vn /= np.maximum(np.linalg.norm(vn, axis=1, keepdims=True), 1e-300)
    a = np.concatenate([f[:, 0], f[:, 1], f[:, 2]])
    b = np.concatenate([f[:, 1], f[:, 2], f[:, 0]])
    edge = set(zip(a.tolist(), b.tolist()))
    walls = [w for x, y in sorted(edge) if (y, x) not in edge for w in ((x, n + x, n + y), (x, n + y, y))]
    solid_pts = np.vstack([pts + vn * thickness / 2, pts - vn * thickness / 2])
    solid_tri = np.vstack([f, f[:, ::-1] + n, np.array(walls, np.int64).reshape(-1, 3)])
    closed, volume = _closed_volume(solid_pts, solid_tri)
    if not closed or volume == 0:
        return None
    return solid_pts, (solid_tri[:, ::-1] if volume < 0 else solid_tri).astype(np.int32)


def _safe_hull(points: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
    """``_hull``, and for points that lie in one plane the hull of the same points thickened by a thousandth of their extent."""
    from scipy.spatial import QhullError

    try:
        return _hull(points)
    except QhullError:
        e = max(float(np.ptp(points, axis=0).max()) * 1e-3, 1e-6)
        return _hull(np.vstack([points + d for d in (np.zeros(3), [0, 0, e], [0, e, 0], [e, 0, 0])]))


@dataclass(frozen=True)
class HullLimit:
    """What a target engine accepts in one convex collision mesh (see ``validator.ENGINE_RULES``), each quantity as the
    engine counts it on the mesh that ships; ``None`` = no limit on it.

    ``vertices``: distinct points (PhysX vertices, Jolt hull points). ``triangles``: triangles of the shipped
    triangulation (Unity). ``planar_faces``: polygons, coplanar triangles counted once (PhysX faces)."""
    vertices: int | None = None
    triangles: int | None = None
    planar_faces: int | None = None
    label: str = ""

    @property
    def max_vertices(self) -> int:
        """The most vertices a triangulated convex hull within this limit can have (V vertices make 2V - 4 triangles)."""
        v = self.vertices if self.vertices is not None else 10**9
        return min(v, (self.triangles + 4) // 2) if self.triangles is not None else v

    def excess(self, n_vertices: int, n_triangles: int, n_planes: int) -> str:
        """Empty when a hull of these counts is accepted, else what is over the limit ("300 vertices > 256")."""
        out = []
        for n, cap, what in ((n_vertices, self.vertices, "vertices"), (n_triangles, self.triangles, "triangles"),
                             (n_planes, self.planar_faces, "planar faces")):
            if cap is not None and n > cap:
                out.append(f"{n} {what} > {cap}")
        return ", ".join(out)


def hull_plane_count(points: np.ndarray, triangles: np.ndarray, rel_tol: float = 1e-5) -> int:
    """Number of planar faces of a closed convex triangle mesh: triangles that share an edge are one face when each one's
    far vertex lies within ``rel_tol`` of the diagonal from the other's plane. The tolerance is well above float32
    rounding (USD and glTF store points as float32, which tilts the plane of a small triangle) and well below the step
    between two real faces of a collision hull."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    p = np.asarray(points, np.float64)
    t = np.asarray(triangles, np.int64).reshape(-1, 3)
    m = len(t)
    if m == 0:
        return 0
    tol = rel_tol * max(float(np.linalg.norm(np.ptp(p[np.unique(t)], axis=0))), 1e-300)
    n = np.cross(p[t[:, 1]] - p[t[:, 0]], p[t[:, 2]] - p[t[:, 0]])
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-300)
    d = np.einsum("ij,ij->i", n, p[t[:, 0]])
    # half-edge (a, b) of face f, with its far vertex; the twin is (b, a)
    a, b, far = t.reshape(-1), np.roll(t, -1, axis=1).reshape(-1), np.roll(t, -2, axis=1).reshape(-1)
    face = np.repeat(np.arange(m), 3)
    key = a * (int(t.max()) + 1) + b
    twin_key = b * (int(t.max()) + 1) + a
    order = np.argsort(key)
    pos = np.clip(np.searchsorted(key[order], twin_key), 0, len(key) - 1)
    has = key[order][pos] == twin_key
    f1, f2 = face[has], face[order[pos[has]]]
    v1, v2 = far[has], far[order[pos[has]]]
    flat = (np.abs(np.einsum("ij,ij->i", n[f1], p[v2]) - d[f1]) <= tol) & (np.abs(np.einsum("ij,ij->i", n[f2], p[v1]) - d[f2]) <= tol)
    count, _ = connected_components(coo_matrix((np.ones(int(flat.sum())), (f1[flat], f2[flat])), shape=(m, m)), directed=False)
    return int(count)


#: Vertices of a capped hull that lie closer together than this fraction of the hull's diagonal are welded: one point
#: of a float32 mesh (as USD and glTF store it) cannot tell them apart. A welded vertex moves inward by at most this much,
#: which bounds how far the part can lie outside the capped hull (1 um per metre of diagonal).
HULL_WELD_REL = 1e-6


def _dot3(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """``a @ b.T`` for (n, 3) and (m, 3) arrays, as three products: BLAS threads gain nothing on an inner dimension of 3,
    and on a busy machine they made this 10 to 60 times slower."""
    return a[:, None, 0] * b[None, :, 0] + a[:, None, 1] * b[None, :, 1] + a[:, None, 2] * b[None, :, 2]


def _halfspace_vertices(normals: np.ndarray, offsets: np.ndarray, interior: np.ndarray, weld: float) -> np.ndarray:
    """The vertices of the polytope ``normals @ x <= offsets``, those within ``weld`` of each other welded."""
    from scipy.sparse.csgraph import connected_components
    from scipy.sparse import coo_matrix
    from scipy.spatial import HalfspaceIntersection, cKDTree

    hs = HalfspaceIntersection(np.c_[normals, -offsets], interior)
    v = hs.intersections
    pairs = cKDTree(v).query_pairs(weld, output_type="ndarray")
    if len(pairs):
        _, lab = connected_components(coo_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(len(v), len(v))),
                                      directed=False)
        _, first = np.unique(lab, return_index=True)
        v = v[np.sort(first)]
    return v


def _even_directions(n: int) -> np.ndarray:
    """``n`` unit vectors spread evenly over the sphere (Fibonacci lattice); deterministic."""
    i = np.arange(n) + 0.5
    z = 1.0 - 2.0 * i / n
    r = np.sqrt(np.maximum(0.0, 1.0 - z * z))
    phi = i * (math.pi * (3.0 - math.sqrt(5.0)))
    return np.c_[np.cos(phi) * r, np.sin(phi) * r, z]


def _plane_cap(p0: np.ndarray, t0: np.ndarray, limit: HullLimit, seed: int = 0):
    """Enclosing hull within ``limit`` as an intersection of supporting half-spaces of the hull ``(p0, t0)``.

    Starts from the six axis-aligned supporting planes and, while the result stays within the limit, adds the cuts
    that remove most: for the vertices lying furthest outside the original hull, the original face plane the vertex
    lies furthest beyond, or the supporting plane in its direction from the centre when that cuts deeper. Every plane
    supports the original hull, so the result contains it (up to the vertex weld, ``HULL_WELD_REL`` of the diagonal).
    A polytope of F planes has up to 2F - 4 vertices. Returns ``(points, triangles, volume, planes, reach)``, ``reach``
    being how far its vertices lie beyond the original face planes at most, or None when no plane set fits."""
    diag = float(np.linalg.norm(np.ptp(p0, axis=0)))
    weld = HULL_WELD_REL * diag
    fn = np.cross(p0[t0[:, 1]] - p0[t0[:, 0]], p0[t0[:, 2]] - p0[t0[:, 0]])
    ln = np.linalg.norm(fn, axis=1)
    ok = ln > 1e-12 * diag * diag
    N = fn[ok] / ln[ok, None]
    D = np.einsum("ij,ij->i", N, p0[t0[ok, 0]])
    # coplanar triangles give one candidate plane
    _, first = np.unique(np.round(np.c_[N, D / diag] * 1e9).astype(np.int64), axis=0, return_index=True)
    first.sort()
    N, D = N[first], D[first]
    centre = p0.mean(axis=0)
    axes = np.vstack([np.eye(3), -np.eye(3)]) if seed <= 6 else _even_directions(seed)
    planes_n = [a for a in axes]
    planes_d = [float(x) for x in _dot3(p0, axes).max(axis=0)]

    def build(nn, dd):
        # every vertex of a half-space intersection is extreme: its triangulated hull has 2V - 4 triangles
        hp = _halfspace_vertices(np.asarray(nn), np.asarray(dd), centre, weld)
        return hp, None, None, limit.excess(len(hp), 2 * len(hp) - 4, len(nn))

    best = build(planes_n, planes_d)
    if best[3]:  # a box (8 vertices, 12 triangles, 6 faces) is over the limit: no plane set can do better
        return None
    step = None
    known: dict[bytes, tuple[int, float]] = {}                # vertex -> (deepest original plane, how far beyond it)
    while True:
        hp = best[0]
        keys = [x.tobytes() for x in hp]
        fresh = [i for i, k in enumerate(keys) if k not in known]
        if fresh:                                             # a vertex kept from the last round keeps its answer
            viol = _dot3(hp[fresh], N) - D                    # how far each vertex lies beyond each original plane
            jf = viol.argmax(axis=1)
            for i, j, v in zip(fresh, jf.tolist(), viol[np.arange(len(fresh)), jf].tolist()):
                known[keys[i]] = (j, v)
        jbest = np.array([known[k][0] for k in keys], np.int64)
        vbest = np.array([known[k][1] for k in keys])
        if vbest.max() <= weld:
            break                                             # the original hull itself: nothing left to cut
        order = np.argsort(-vbest, kind="stable")
        order = order[vbest[order] > weld]
        room = limit.max_vertices - len(hp)
        if limit.planar_faces is not None:
            room = min(room, limit.planar_faces - len(planes_n))
        k = step if step is not None else max(1, room // 8)
        U = hp[order] - centre
        U /= np.maximum(np.linalg.norm(U, axis=1, keepdims=True), 1e-300)
        new_n, new_d, seen = [], [], set()
        chunk = max(32, 2 * k)
        for r, i in enumerate(order.tolist()):
            if r % chunk == 0:                                # supporting planes of the original hull, as far as needed
                part = slice(r, r + chunk)
                HU = _dot3(p0, U[part]).max(axis=0)
                deep = np.einsum("ij,ij->i", hp[order[part]], U[part]) - HU > vbest[order[part]]
            if deep[r % chunk]:
                new_n.append(U[r])
                new_d.append(float(HU[r % chunk]))
                if len(new_n) >= k:
                    break
                continue
            j = int(jbest[i])
            if j in seen:
                continue
            seen.add(j)
            new_n.append(N[j])
            new_d.append(float(D[j]))
            if len(new_n) >= k:
                break
        if not new_n:
            break
        trial = build(planes_n + new_n, planes_d + new_d)
        if trial[3]:
            if k == 1:
                break                                         # one more plane would exceed the limit
            step = max(1, k // 2)                             # near the limit: fewer planes at a time
            continue
        planes_n += new_n
        planes_d += new_d
        best = trial
    # every vertex of ``best`` was measured at the top of the last round
    reach = max(0.0, max(known[x.tobytes()][1] for x in best[0]))
    hp, ht, hv = _hull(best[0])
    return hp, ht, hv, len(planes_n), reach


#: how a hull's ``reason`` starts its note when ``_limit_hull`` replaced it
CAPPED_MARK = "capped to the engine's hull limit"


def _limit_hull(pts: np.ndarray, tris: np.ndarray, volume: float, limit: HullLimit):
    """A convex hull that an engine with ``limit`` accepts and that still encloses the hull ``(pts, tris)``.

    Returns ``(points, triangles, volume, note)``; the input itself (note "") when it is within the limit, else the
    supporting half-space construction of ``_plane_cap``. The same input gives the same output bit for bit.

    Measured against it (sphere 64x32, capsule, cylinder 256): a subset of the hull's vertices pushed out from the
    centre until it encloses (per-vertex factors from a linear program) claimed 1.3 to 2 times the excess volume on a
    sphere and far more on elongated shapes."""
    excess = limit.excess(len(pts), len(tris), hull_plane_count(pts, tris) if limit.planar_faces is not None else 0)
    if not excess:
        return pts, tris, volume, ""
    p0 = np.asarray(pts, np.float64)
    t0 = np.asarray(tris, np.int64)
    # Two starts: the six axis planes (best where the hull has few large faces: a cylinder keeps its own faces) and
    # supporting planes in evenly spread directions for most of the plane budget (best on curved hulls: a sphere's worst
    # corner comes down by about a fifth). The one whose corners reach least far beyond the original hull is kept.
    planes_budget = min(limit.max_vertices // 2 + 2, limit.planar_faces if limit.planar_faces is not None else 10**9)
    found = [r for r in (_plane_cap(p0, t0, limit), _plane_cap(p0, t0, limit, seed=int(0.9 * planes_budget)))
             if r is not None]
    if not found:
        raise ValueError(f"no enclosing hull within the engine's limit ({limit.label}): {excess}")
    hp, ht, hv, planes, reach = min(found, key=lambda r: (r[4], r[2]))
    note = (f"{CAPPED_MARK}{f' ({limit.label})' if limit.label else ''}: {excess}; now "
            f"{len(hp)} vertices, {len(ht)} triangles, {planes} planes, volume {hv / volume - 1:+.2%}, "
            f"corners up to {reach * 1e3:.1f} mm beyond the original hull's face planes")
    return hp, ht.astype(np.int32), hv, note


def _coacd_params(threshold: float, share: int, off: bool) -> dict:
    return dict(threshold=threshold, max_convex_hull=share, preprocess_mode="off" if off else "auto", resolution=500,
                mcts_iterations=40, mcts_nodes=10, max_ch_vertex=64, decimate=True, seed=0)


# The reason carried by a hull made from MeshData.collider_points, so the manifest's collider_sources says that the
# collision of a bevelled or inset part is its plain shape.
PROXY_REASON = "plain shape of a bevelled or inset part: rounded edges and recessed panels are not in the collision"


@dataclass
class _Piece:
    """One body to decompose: what CoACD is given, and what stands in for it if it cannot be."""
    pts: np.ndarray
    tri: np.ndarray
    hull_pts: np.ndarray           # the hull of this stands in when no decomposition comes back
    volume_hint: float             # orders pieces (largest first); the volume of the convex hull of the body
    bounded: bool                  # run in a child process with a deadline
    reason: str = ""               # what repair the input needed, carried to the hulls


def generate_colliders(
    meshes: list[MeshData], max_hulls: int, threshold: float = 0.05, asset_name: str = "Asset", *,
    limit: HullLimit | None = None,
) -> list[Hull]:
    """Convex collision hulls for the collider meshes, at most ``max_hulls`` of them.

    ``limit``: what the target engine accepts in one convex mesh (``hull_limit(engine)``); every hull over it, from
    any path (fast path, decomposition, merge, body hull), is replaced by an enclosing hull within it, and the
    hull's ``reason`` says so. ``None`` leaves hulls as computed."""
    import coacd

    from antigravity3d.kernel import collider_worker
    from antigravity3d.kernel.collider_repair import repair_collider_mesh

    coacd.set_log_level("error")
    raw: list[tuple[np.ndarray, np.ndarray, float, str, str]] = []
    concave: list[_Piece] = []

    def sort_out(pts: np.ndarray, tri: np.ndarray, topo: _Topo, reason: str = "", bounded: bool = False) -> bool:
        """A closed body goes to the hull fast path if convex, else to decomposition (under a deadline if ``bounded``)."""
        pp, tt, hv = _hull(pts)
        if topo.closed and topo.volume > 0 and hv <= topo.volume * 1.02:
            raw.append((pp, tt, hv, "fast_path", reason))
            return True
        concave.append(_Piece(pts, tri, pts, hv, bounded=bounded, reason=reason))
        return False

    for m in meshes:
        if not m.collider or len(m.triangles) == 0:
            continue
        if m.collider_points is not None:  # the part's plain shape (compile_ir.collider_proxy): convex by construction
            pp, tt, hv = _hull(np.asarray(m.collider_points, np.float64))
            raw.append((pp, tt, hv, "fast_path", PROXY_REASON))
            continue
        pts = np.asarray(m.points, dtype=np.float64)
        tri = np.asarray(m.triangles)
        topo = _topology(pts, tri)
        if topo.closed and topo.volume > 0:
            if not sort_out(pts, tri, topo):
                bodies = _closed_bodies(pts, tri)
                if bodies:
                    concave.pop()
                    for bp, bt in bodies:
                        sort_out(bp, bt, _topology(bp, bt))
            continue
        # Not one closed manifold body: CoACD's own repair pass would rebuild it as a dense shell (hundreds of thousands of
        # triangles, minutes to decompose). Repair a private copy instead and decompose what comes out.
        rep = repair_collider_mesh(pts, tri)
        why = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in rep.notes.items()
                        if v and k in ("welded", "degenerate", "duplicate", "flipped", "capped", "nonmanifold_edges", "split_vertices"))
        why = f"repaired input ({why})" if why else "repaired input"
        for body in rep.bodies:
            if body.kind == "closed":
                # closed by vertex index; where a test by position disagrees the body touches itself, and the repair-free
                # decomposition of such a body is not known to be bounded
                sort_out(body.points, body.triangles, _Topo(0, 0, 0, 0, 0, body.volume), why,
                         bounded=not _topology(body.points, body.triangles).closed)
            elif body.kind == "pinched":
                # closed surface with non-manifold vertices left (it touches itself): measured to take minutes even with the
                # repair pass off, so no decomposition is attempted
                pp, tt, hv = _safe_hull(body.points)
                raw.append((pp, tt, hv, "body_hull", f"{why}; surface touches itself at an edge or vertex, not decomposed"))
            else:
                thick = SHELL_THICKNESS * float(np.linalg.norm(np.ptp(body.points, axis=0)))
                solid = _solid_from_surface(body.points, body.triangles, thick)
                if solid is None:
                    pp, tt, hv = _safe_hull(body.points)
                    raw.append((pp, tt, hv, "body_hull", f"{why}; open surface that could not be made solid, not decomposed"))
                else:
                    hp, _, hv = _safe_hull(solid[0])
                    concave.append(_Piece(solid[0], solid[1], solid[0], hv, bounded=True, reason=f"{why}; open surface made solid"))
    if concave:
        budget_left = max(len(concave), max_hulls - len(raw))
        for k, piece in enumerate(sorted(concave, key=lambda x: -x.volume_hint)):
            share = max(1, budget_left // (len(concave) - k))
            budget_left -= share
            pts, tri = piece.pts, piece.tri
            # preprocess "off" segfaulted CoACD on overlapping multi-body input, so the manifold pre-pass is only skipped for
            # a single closed body; every such body is closed here, by position (as given) or by index (repaired)
            if not piece.bounded:                       # (position-closed, so the coplanar merge applies)
                pts, tri = _decomposition_input(pts, tri)
            bounded = piece.bounded or len(tri) > IN_PROCESS_MAX_TRIANGLES
            if bounded:
                out = collider_worker.run_in_child(pts, tri, _coacd_params(threshold, share, True), decomposition_budget_s(len(tri)))
                parts, fail = out.parts, out.reason
            else:
                parts, fail = coacd.run_coacd(coacd.Mesh(pts, np.asarray(tri, dtype=np.int32)), **_coacd_params(threshold, share, True)), ""
            if parts is None:
                pp, tt, hv2 = _safe_hull(piece.hull_pts)
                raw.append((pp, tt, hv2, "body_hull", "; ".join(x for x in (piece.reason, f"decomposition stopped: {fail}") if x)))
                continue
            for v, _f in parts:
                pp, tt, hv2 = _hull(np.asarray(v, dtype=np.float64))
                raw.append((pp, tt, hv2, "coacd", piece.reason))
    while len(raw) > max(1, max_hulls):
        raw.sort(key=lambda h: h[2])
        a, b = raw[0], raw[1]
        pp, tt, hv = _hull(np.vstack([a[0], b[0]]))
        raw = [(pp, tt, hv, "merged", "; ".join(dict.fromkeys(x for x in (a[4], b[4]) if x)))] + raw[2:]
    if limit is not None:
        capped = []
        for pp, tt, hv, src, why in raw:
            pp, tt, hv2, note = _limit_hull(pp, tt, hv, limit)
            capped.append((pp, tt, hv2, src, "; ".join(x for x in (why, note) if x)))
        raw = capped
    raw.sort(key=lambda h: -h[2])
    return [Hull(f"UCX_{asset_name}_{i:02d}", p, t, v, s, r) for i, (p, t, v, s, r) in enumerate(raw)]


def hull_limit(engine: str | None) -> HullLimit | None:
    """The convex-hull limit of a target engine, from ``validator.ENGINE_RULES``; None when it has none or is unknown."""
    if engine is None:
        return None
    from antigravity3d.kernel.validator import ENGINE_RULES

    rules = ENGINE_RULES.get(engine)  # type: ignore[call-overload]
    return rules.hull_limit if rules is not None else None


def describe_collider_sources(hulls: list[Hull]) -> str:
    """"coacd, body_hull (decomposition stopped: timeout after 30 s)": the sources of a set of hulls, with every reason a hull
    is not a plain decomposition or hull, so that a fallback is never silent."""
    sources = sorted({h.source for h in hulls})
    reasons = sorted({h.reason for h in hulls if (h.source == "body_hull" or PROXY_REASON in h.reason) and h.reason})
    capped = [h for h in hulls if CAPPED_MARK in h.reason]
    if capped:
        reasons.append(f"{len(capped)} capped to the engine's hull limit, volume "
                       + ", ".join(h.reason.split("volume ", 1)[-1].split(",", 1)[0] for h in capped))
    return ", ".join(sources) + (f" ({'; '.join(reasons)})" if reasons else "")


# --------------------------------------------------------------------------- export


_gltf_default_material: list = []   # built on first use: gltf_writer is imported lazily, like the other exporters
_gltf_untextured: list = []         # one weakly keyed {material: its factor-only copy}


def gltf_default_material():
    """The grey material a UV-mapped mesh gets when none is bound (one shared object: one glTF material)."""
    if not _gltf_default_material:
        from antigravity3d.kernel import gltf_writer as gw

        _gltf_default_material.append(gw.GltfMaterial(name="Default", base_color_factor=(0.5, 0.5, 0.5, 1.0),
                                                      metallic_factor=0.0, roughness_factor=0.5))
    return _gltf_default_material[0]


def _without_textures(value):
    """``value`` (an extension payload) with every texture slot removed, at any depth."""
    from antigravity3d.kernel import gltf_writer as gw

    if isinstance(value, Mapping):
        return {k: _without_textures(v) for k, v in value.items() if not isinstance(v, gw.TextureRef)}
    return value


def _untextured(material):
    """Factor-only copy of a material for a mesh without UVs (a texture without TEXCOORD_0 is invalid glTF).

    One copy per material object, so UV-less meshes of one material still share one glTF material. A material
    with an albedo map carries a white base colour factor, so its copy is shown in neutral grey; factor-only
    extension parameters (emission strength, clearcoat, transmission...) are kept, their texture slots are not.
    """
    import weakref
    from dataclasses import replace

    if not _gltf_untextured:
        _gltf_untextured.append(weakref.WeakKeyDictionary())
    cache = _gltf_untextured[0]
    if material not in cache:
        had_albedo = material.base_color_texture is not None
        cache[material] = replace(
            material, base_color_texture=None, metallic_roughness_texture=None, normal_texture=None,
            occlusion_texture=None, emissive_texture=None, extensions=_without_textures(material.extensions),
            base_color_factor=(0.72, 0.74, 0.76, 1.0) if had_albedo else tuple(material.base_color_factor))
    return cache[material]


def gltf_mesh(name: str, points: np.ndarray, triangles: np.ndarray, uvs: np.ndarray | None, material=None, *,
              shade: bool, y_up: bool = True, crease_deg: float | None = None):
    """One kernel mesh (world space, Z-up, UVs with a bottom-left origin) as a ``gltf_writer.GltfMesh``.

    The single conversion behind every GLB this package writes: the package GLBs (:func:`write_gltf`) and the
    inspector's review GLB (``artifacts/review_glb.py``), so what is reviewed is converted exactly as what ships.

    ``shade``: write shading normals, the crease normals the package USD authors
    (``compile_ir.crease_normals`` at ``crease_deg``; None is the project's 40 degrees, an art direction may set
    another) turned into one normal per vertex by splitting vertices at creases. ``y_up``: glTF is Y-up, so (x, y, z) -> (x, z, -y). A mesh with UVs and no material gets
    :func:`gltf_default_material`; a mesh without UVs keeps only the factors of its material.
    """
    from antigravity3d.kernel import gltf_writer as gw
    from antigravity3d.kernel.compile_ir import CREASE_DEG, crease_normals

    p = np.asarray(points, dtype=np.float64)
    tris = np.asarray(triangles)
    normals = None
    if shade:
        crease = CREASE_DEG if crease_deg is None else crease_deg
        p, tris, uvs, normals = gw.split_by_corner_normals(p, tris, uvs, crease_normals(p, tris, crease))
        if y_up:
            normals = gw.z_up_to_y_up(normals)
    if y_up:
        p = gw.z_up_to_y_up(p)
    if uvs is not None:
        material = material if material is not None else gltf_default_material()
        uvs = gw.flip_v(uvs)
    elif material is not None:
        material = _untextured(material)
    return gw.GltfMesh(name, p, tris, uvs, material, normals)


def write_gltf(
    path: str | Path,
    items: Sequence[LOD | MeshData | UVResult | Hull],
    names: Sequence[str] | None = None,
    y_up: bool = True,
    *,
    materials: Sequence | None = None,
    image_uris: Mapping[str, str] | None = None,
    crease_deg: float | None = None,
) -> Path:
    """Writes a GLB. Our world is Z-up; glTF is Y-up, so (x,y,z) -> (x,z,-y) unless y_up=False.

    ``materials`` holds one ``gltf_writer.GltfMaterial`` (or None) per item; images inside are embedded as the
    bytes they already hold (see kernel/gltf_writer.py), except those whose content digest ``image_uris`` maps to
    a relative URI: those are referenced, not embedded (``gltf_writer.build_glb``). Items with UVs and no material
    get a grey default. Items with UVs (the rendered ones; collider hulls have none) also get shading normals, so
    the GLB shades like the USD; items without UVs get neither normals nor a material. Each item goes through
    :func:`gltf_mesh` (``crease_deg``: the shading crease, see there).
    """
    from antigravity3d.kernel import gltf_writer as gw

    meshes = []
    for i, it in enumerate(items):
        uv = getattr(it, "uvs", None)
        name = names[i] if names else getattr(it, "name", None) or getattr(it, "node_id", None) or f"mesh_{i}"
        material = materials[i] if uv is not None and materials else None
        meshes.append(gltf_mesh(name, it.points, it.triangles, uv, material, shade=uv is not None, y_up=y_up, crease_deg=crease_deg))
    return gw.write_glb(path, meshes, image_uris=image_uris)


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


@dataclass(frozen=True)
class Shipped:
    """What a part ships at one level: its triangles minus the hidden ones, unreferenced vertices dropped.
    ``vertices`` indexes the part's (seam-split) vertices it keeps, in order."""
    points: np.ndarray
    triangles: np.ndarray
    uvs: np.ndarray | None
    vertices: np.ndarray

    @property
    def tri_count(self) -> int:
        return len(self.triangles)


def shipped(part: UVResult | LOD) -> Shipped:
    """The shipped form of a part (``UVResult`` = LOD0, or one ``LOD`` level): see ``UVResult.hidden``."""
    h = getattr(part, "hidden", None)
    pts, tri, uv = np.asarray(part.points), np.asarray(part.triangles), part.uvs
    if h is None or not h.any():
        return Shipped(pts, tri, uv, np.arange(len(pts)))
    t = tri[~h]
    keep = np.unique(t.reshape(-1))
    remap = np.full(len(pts), -1, np.int64)
    remap[keep] = np.arange(len(keep))
    return Shipped(pts[keep], remap[t].astype(tri.dtype), None if uv is None else np.asarray(uv)[keep], keep)


def _solid(points: np.ndarray, tris: np.ndarray) -> bool:
    if not len(tris):
        return False
    t = _topology(np.asarray(points, np.float64), np.asarray(tris))
    return t.closed and t.volume > 0


def _crease_safe(points: np.ndarray, tris: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """``mask`` without the triangles whose removal would change a shipped corner normal: crease normals smooth
    across faces within 40 degrees, so a hidden face next to a shallow visible one shades it. Box and cylinder
    contacts meet their neighbours at 90 degrees and lose nothing here."""
    from antigravity3d.kernel.compile_ir import crease_normals

    if not mask.any():
        return mask
    mask = mask.copy()
    full = crease_normals(points, tris).reshape(-1, 3, 3)
    wid = _canon(np.asarray(points, np.float64))[np.asarray(tris)]
    while mask.any():
        kept = np.flatnonzero(~mask)
        now = crease_normals(points, tris[kept]).reshape(-1, 3, 3)
        bad = np.abs(now - full[kept]).max(axis=2) > 1e-6  # (kept, 3) corners whose normal moved
        if not bad.any():
            break
        moved = np.unique(wid[kept][bad])
        back = mask & np.isin(wid, moved).any(axis=1)
        if not back.any():
            break
        mask &= ~back
    return mask


def contact_faces_removable(budget: EngineBudget, opaque_materials) -> bool:
    """Whether the shipped meshes may drop hidden contact faces: the caller knows which materials are opaque
    (``coplanar.opaque_materials``), the target engine accepts open render meshes (``EngineRules``) and the run did
    not ask to ship the closed solids (``EngineBudget.keep_contact_faces``)."""
    from antigravity3d.kernel.validator import ENGINE_RULES

    rules = ENGINE_RULES.get(budget.target_engine)
    return (opaque_materials is not None and rules is not None and not rules.closed_render_meshes
            and not budget.keep_contact_faces)


def hidden_contact_faces(parts: Sequence[tuple[str, np.ndarray, np.ndarray, str | None]], opaque_materials) -> list[np.ndarray]:
    """Per part (node id, points, triangles, material id) the triangles hidden by coplanar contact with other
    parts of the same asset (``coplanar.hidden_triangles``), restricted so no shipped corner normal changes."""
    from antigravity3d.kernel import coplanar

    items = [coplanar.Part(n, np.asarray(p, np.float64), np.asarray(t)) for n, p, t, _ in parts]
    solid = [_solid(it.points, it.triangles) for it in items]
    opaque = [mat in opaque_materials for *_, mat in parts]
    masks = coplanar.hidden_triangles(items, solid, opaque)
    return [_crease_safe(it.points, it.triangles, m) for it, m in zip(items, masks)]


def _ship_levels(closed: list[UVResult], lods: dict[str, list[LOD]], materials: dict[str, str | None],
                 opaque_materials, hidden0: dict[str, np.ndarray]) -> tuple[dict[str, list[LOD]], dict[str, np.ndarray]]:
    """LOD chains with each level's hidden triangles marked (decided on that level's own meshes: a part absent
    at a level covers nothing there, and a simplified part only hides what it still covers exactly). A level
    whose shipped triangle count no longer pays for itself against the previous kept level (LOD_MIN_REDUCTION)
    is left out, as generate_asset_lods leaves out a candidate that does not pay.

    Also returns, per part, the unwrap's vertices that a kept level ships although no shipped LOD0 triangle uses
    them (``hidden0``: LOD0's masks): their charts must be packed into the one atlas all levels share."""
    from antigravity3d.kernel import lod_diff as ld

    exposed: dict[str, np.ndarray] = {}
    if not lods:
        return lods, exposed
    by_id = {u.node_id: u for u in closed}
    names = [u.node_id for u in closed if u.node_id in lods and len(lods[u.node_id][0].triangles)]
    levels = min(len(lods[n]) for n in names)
    out: dict[str, list[LOD]] = {n: [] for n in names}
    prev = None
    for k in range(levels):
        level = [lods[n][k] for n in names]
        masks = hidden_contact_faces([(n, lv.points, lv.triangles, materials.get(n)) for n, lv in zip(names, level)],
                                     opaque_materials)
        marked = [replace(lv, hidden=m if m.any() else None) for lv, m in zip(level, masks)]
        tris = sum(shipped(lv).tri_count for lv in marked)
        if prev is not None and tris > (1.0 - ld.LOD_MIN_REDUCTION) * prev:
            continue
        for n, lv in zip(names, marked):
            out[n].append(lv)
            h0 = hidden0.get(n)
            if k == 0 or h0 is None or not h0.any() or lv.uvs is None:
                continue
            u = by_id[n]
            seen = np.zeros(len(u.points), bool)
            seen[u.triangles[~h0].reshape(-1)] = True
            at = _vertex_index(u, lv)
            used = at[np.asarray(shipped(lv).vertices)] if lv.hidden is not None else at
            extra = used[(used >= 0) & ~seen[np.maximum(used, 0)]]
            if len(extra):
                exposed[n] = np.union1d(exposed.get(n, np.zeros(0, np.int64)), extra)
        prev = tris
    count = max(len(c) for c in out.values())
    for n in lods:  # parts without geometry keep a chain as long as the others
        if n not in out:
            out[n] = lods[n][:count]
    return {n: out[n] for n in lods}, exposed


def _vertex_index(u: UVResult, lv: LOD) -> np.ndarray:
    """Per vertex of a LOD level, the unwrap vertex it is (-1 if none): meshoptimizer keeps vertices, and
    _Simplifier joins seam copies at the position of the first copy, so a level vertex is found by that position
    and its uv."""
    p32 = np.ascontiguousarray(u.points, np.float32)
    cid = _canon(np.asarray(u.points, np.float64))
    first = np.unique(cid, return_index=True)[1]
    ref = np.ascontiguousarray(np.hstack([p32[first][cid], np.asarray(u.uvs, np.float32)]))
    index = {row.tobytes(): i for i, row in enumerate(ref)}
    key = np.ascontiguousarray(np.hstack([np.asarray(lv.points, np.float32), np.asarray(lv.uvs, np.float32)]))
    return np.array([index.get(row.tobytes(), -1) for row in key], np.int64)


def _chart_of(tris: np.ndarray, seeds: np.ndarray) -> np.ndarray:
    """Triangles connected (through shared vertex indices: charts are seam-split) to any of ``seeds``."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    t = np.asarray(tris, np.int64)
    n = int(t.max()) + 1
    g = coo_matrix((np.ones(2 * len(t)), (np.r_[t[:, 0], t[:, 1]], np.r_[t[:, 1], t[:, 2]])), shape=(n, n))
    comp = connected_components(g, directed=False)[1]
    seeds = seeds[(seeds >= 0) & (seeds < n)]
    return np.isin(comp[t[:, 0]], comp[seeds])


def _relayout(lods: dict[str, list[LOD]], closed: list[UVResult], shipped_uvs: list[UVResult]) -> dict[str, list[LOD]] | None:
    """The LOD chains with every level's uvs moved from the closed atlas to the shipped one, vertex by vertex
    (``_vertex_index``); None if a level vertex is not found."""
    out: dict[str, list[LOD]] = {}
    by_id = {c.node_id: (c, s) for c, s in zip(closed, shipped_uvs)}
    for n, chain in lods.items():
        if n not in by_id or chain[0].uvs is None or np.array_equal(by_id[n][0].uvs, by_id[n][1].uvs):
            out[n] = chain
            continue
        c, s = by_id[n]
        new = []
        for lv in chain:
            if not len(lv.points):
                new.append(lv)
                continue
            at = _vertex_index(c, lv)
            if (at < 0).any():
                return None
            new.append(replace(lv, uvs=np.asarray(s.uvs, np.float32)[at]))
        out[n] = new
    return out


def run_export_pipeline(meshes: list[MeshData], budget: EngineBudget, asset_name: str, *,
                        opaque_materials: frozenset[str] | set[str] | None = None) -> ExportBundle:
    """UVs, LODs and colliders of one asset.

    ``opaque_materials`` (material ids rendered opaque; None = unknown) turns on hidden contact faces where the
    target engine allows open render meshes: triangles of a part that other parts of this asset cover exactly
    are marked ``hidden`` in the UVs and in every LOD level, take no atlas space, and are left out of what ships
    (``shipped``). The meshes themselves, the colliders and everything that reads the working stage keep the
    closed solids."""
    # Colliders depend only on the source meshes, but they are not run on a second thread beside the unwrap: in about
    # 860 such runs the output never differed from this serial order, yet one died with a segmentation fault inside
    # scipy's ConvexHull while the other thread was in xatlas (none in 400 serial runs). The gain was 0 to 15%.
    t = {}
    t0 = time.perf_counter()
    # Every opaque material is laid out in one atlas, sized from need (ATLAS_SIDES), so the package can ship one texture
    # set and one draw call (package._compose_atlas). Materials not known to be opaque keep 2048 px atlases of their own.
    shared = opaque_materials if opaque_materials is not None and budget.shared_atlas else None
    # how much detail the shared materials carry (their maps, or any other surface source): the layout needs no more;
    # flat colours only need FLAT_DETAIL_PX (surface_detail)
    from antigravity3d.kernel import surface_detail

    target = budget.texel_density_px_per_m
    first = {}
    for m in meshes:
        if shared is not None and m.material_id in shared:
            first.setdefault(m.material_id, m.material)
    from antigravity3d.kernel import smart_source  # the budget says whether image-less materials get a painted surface

    with smart_source.using(budget.generated_surfaces):
        detail = {mid: surface_detail.detail_px_per_m(p, target) for mid, p in first.items()}  # None: a flat colour
    # the layout the asset shipped with, when this export is an edit of it (StageManager.keep_layout): kept where it can be
    keep = next((m.keep_layout for m in meshes if getattr(m, "keep_layout", None) is not None), None) if shared is not None else None
    hidden = None
    if contact_faces_removable(budget, opaque_materials):
        hidden = hidden_contact_faces([(m.node_id, m.points, m.triangles, m.material_id) for m in meshes], opaque_materials)
        if not any(h.any() for h in hidden):
            hidden = None
    th = time.perf_counter()
    if hidden is None:
        uvs = unwrap_scene(meshes, budget, 2048, shared=shared, max_px=budget.max_texture_px, detail_px_per_m=detail, keep=keep)
        t1 = time.perf_counter()
        lods = generate_asset_lods(uvs)
    else:
        # One atlas for every level. LODs are made from the closed unwrap: the same chain, triangle for triangle,
        # as without the rule (the simplifier is sensitive to where charts sit in the atlas). Each level's hidden
        # triangles are decided on its own meshes; a chart is left out of the packing only if no kept level ships
        # any of it. The charts are then packed once more (same charts, same vertices) and every level's uvs are
        # carried over to that packing vertex by vertex.
        materials = {m.node_id: m.material_id for m in meshes}
        cache: dict = {}
        # the closed packing (LODs are simplified from it) takes the shipped size when it can, but pins nothing: what ships
        # is the packing below, and every level's uvs are carried over to it (_relayout)
        closed = unwrap_scene(meshes, budget, 2048, cache=cache, shared=shared, max_px=budget.max_texture_px, detail_px_per_m=detail,
                              keep=keep, pin=False)
        t1 = time.perf_counter()
        lods_closed = generate_asset_lods(closed)
        hidden0 = {u.node_id: h for u, h in zip(closed, hidden)}
        marked, exposed = _ship_levels(closed, lods_closed, materials, opaque_materials, hidden0)
        pack = [h & ~_chart_of(u.triangles, exposed[u.node_id]) if u.node_id in exposed else h for u, h in zip(closed, hidden)]
        uvs = unwrap_scene(meshes, budget, 2048, hidden=pack, cache=cache, shared=shared, max_px=budget.max_texture_px, detail_px_per_m=detail,
                           keep=keep)
        uvs = [replace(u, hidden=h if h.any() else None) for u, h in zip(uvs, hidden)]
        lods = _relayout(marked, closed, uvs)
        if lods is None:  # a level vertex not found in the unwrap: ship every triangle, as before the rule
            log.warning("hidden contact faces not removed for %s: LOD levels could not be carried to the shipped atlas", asset_name)
            uvs, lods = closed, lods_closed
    t2 = time.perf_counter()
    hulls = generate_colliders(meshes, budget.max_collision_hulls, asset_name=asset_name, limit=hull_limit(budget.target_engine))
    t3 = time.perf_counter()
    t = {"contact": (th - t0) * 1e3, "uv": (t1 - th) * 1e3, "lod": (t2 - t1) * 1e3, "colliders": (t3 - t2) * 1e3,
         "total": (t3 - t0) * 1e3}
    return ExportBundle(asset_name, uvs, lods, hulls, {k: round(v, 2) for k, v in t.items()})
