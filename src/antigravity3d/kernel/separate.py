"""Deterministic separation of coplanar same-facing faces of different parts (no model call).

Two parts with same-facing faces in one plane whose areas overlap are drawn at one depth: the depth buffer picks
either by rounding noise, so the surface flickers (Tier-1's ``z_fighting``, ``fast_ops._check_zfight``; the
independent ``qa_check`` in Blender fails the asset on it). Detailed kit-bashed props produce them on most runs:
a handle grip whose cap is flush with its bracket, a latch plate as thick as the panel it sits in, corner-guard
plates whose ends meet in one plane, and parts sunk into the body whose hidden inner faces share a plane there.

This module moves one face of one part of each such pair so the two faces end ``margin`` apart, by changing the
part's size along the shared normal with a matching shift of its centre (an axis-aligned box: ``size`` and
``translate``; an axis-aligned cylinder's cap: ``height`` and ``translate``). The face a part is mounted by is never
moved: a face in contact with an opposite-facing face of another part stays where it is, so nothing floats and no
gap opens. Per pair, the part whose face in the plane is smaller moves:

* visible faces (the point just in front of the overlap is outside every closed part): the face stands proud
  (moves outward) when more than half of it lies on the other face, so a plate mounted flush keeps showing; else it
  sits back (moves inward), which adds no volume, so the outline cannot grow;
* enclosed faces (the point just in front of the overlap is inside a third closed part, and so is the point just
  behind it: the faces lie strictly inside that part, as with parts sunk into a body): the face moves deeper into
  that part; it must stay inside it, so nothing that could be seen changes;
* contact faces (both faces rest on a third part's face): both are mount faces, so neither moves; they are hidden
  while that part is opaque, and the exporter already leaves them out of the render meshes on engines that accept
  open meshes (``fast_ops.hidden_contact_faces``). Reported as unresolved with that reason.

A candidate move is kept only if it resolves its pair, creates no new same-facing pair for the moved part, keeps
every contact the part had (surfaces within ``CONTACT_TOL``), keeps an inward-moved part at least ``margin`` and
half its former extent thick, and keeps an enclosed face enclosed. Otherwise the other direction, then the other
part, is tried. Pairs between faces that are not axis-aligned, or of kinds whose faces are not parameters (cones,
spheres, imported meshes), stay unresolved and go to the author's repair as before.

The margin: both checks flag faces within 1 mm of one plane, inclusive (``fast_ops.ZFIGHT_EPS``; ``qa_check``'s
``eps`` 0.001 as a BVH nearest distance), and authored gaps of exactly 1.0 mm already fail ``qa_check`` after the
float32 export. ``MARGIN`` = 2 mm leaves 1 mm of headroom over both, is upstream's own contact tolerance (its
``tol`` and ``fast_ops.TOUCH_TOL``), and is a 2 mm step: a panel line, not a visible gap. Pairs are looked for with
Tier-1's own test (1 mm) and, for parallel faces less than the margin apart, with the margin as the window; a pair
already ``margin`` apart is not touched again, so the step is idempotent.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from antigravity3d.ir import NodeIR, SceneIR, SetParam, SetTransform, Transform
from antigravity3d.kernel import coplanar
from antigravity3d.kernel.fast_ops import ZFIGHT_EPS, _solid, _topology, _winding_inside
from antigravity3d.kernel.types import MeshData

MARGIN = 2 * ZFIGHT_EPS
HIDDEN_CONTACT = "hidden contact faces, not shipped (left out of the render meshes, see the package's contact_faces)"
# Two parts are in contact when their surfaces come this close (authored contact is exact up to float32 rounding,
# about 1e-7 m at a metre): a move may not take a contact away.
CONTACT_TOL = 1e-5
_ALIGNED = 1e-9    # |component| below this (relative) off the axis: a world-axis-aligned direction
_ON_PLANE = 1e-6   # a vertex this near a face's plane belongs to that face (float32 points at a few metres)
GROUND_Z = 0.0     # the ground plane (kernel/settle.py, tier1_qa)
_PROBE = 1e-4      # in-front / behind probe distance for the visible / enclosed test


@dataclass(frozen=True)
class FaceMove:
    node_id: str
    axis: int        # world axis 0/1/2
    sign: int        # +1 / -1: the face's outward normal
    delta: float     # metres along the outward normal (+: the part grows, the face stands proud; -: it sits back)
    partner: str
    kind: str        # "visible" | "enclosed" | "contact" | "ground"

    def describe(self) -> str:
        face = ("+" if self.sign > 0 else "-") + "xyz"[self.axis]
        if self.kind == "ground":
            return f"{self.node_id} bottom lifted {-self.delta * 1e3:.2f} mm off the ground plane (was coplanar with {self.partner})"
        if self.kind == "contact":
            return (f"{self.node_id} {face} face sunk {self.delta * 1e3:.2f} mm into the part it rests on (a contact face "
                    f"that ships, coplanar with {self.partner})")
        how = ("proud" if self.delta > 0 else "back") + (", inside the part enclosing both" if self.kind == "enclosed" else "")
        return f"{self.node_id} {face} face {self.delta * 1e3:+.2f} mm ({how}; was coplanar with {self.partner})"


@dataclass(frozen=True)
class Unresolved:
    a: str
    b: str
    why: str

    def describe(self) -> str:
        return f"{self.a}/{self.b}: {self.why}"


@dataclass
class SeparationResult:
    moves: list[FaceMove] = field(default_factory=list)
    unresolved: list[Unresolved] = field(default_factory=list)
    pairs_found: int = 0
    ms: float = 0.0
    ops: list = field(default_factory=list)  # SetParam / SetTransform ops for the moved nodes

    @property
    def hidden_contact(self) -> list[Unresolved]:
        """Pairs of contact faces that the exporter leaves out of the render meshes: they do not ship."""
        return [u for u in self.unresolved if HIDDEN_CONTACT in u.why]

    @property
    def left(self) -> list[Unresolved]:
        """Pairs that ship and could not be separated: they stay findings, for the author's repair."""
        return [u for u in self.unresolved if HIDDEN_CONTACT not in u.why]

    def summary(self) -> str:
        """One short line (a tool result): counts only."""
        if not self.pairs_found:
            return "no coplanar same-facing faces"
        parts = [f"moved {len(self.moves)} face(s) of {len({m.node_id for m in self.moves})} part(s) {MARGIN * 1e3:g} mm apart"]
        if lifted := {m.node_id for m in self.moves if m.kind == "ground"}:
            parts.append(f"lifted {len(lifted)} part(s) {MARGIN * 1e3:g} mm off the ground plane")
        if self.hidden_contact:
            parts.append(f"{len(self.hidden_contact)} hidden contact pair(s) not shipped")
        if self.left:
            parts.append(f"{len(self.left)} left (" + ", ".join(f"{u.a}/{u.b}" for u in self.left[:2]) + ")")
        return "; ".join(parts)

    def describe(self, limit: int = 12) -> str:
        if not self.pairs_found:
            return "no coplanar same-facing faces between parts"

        def some(items, fmt):
            return "; ".join(fmt(x) for x in items[:limit]) + (f"; +{len(items) - limit} more" if len(items) > limit else "")

        parts = [f"{self.pairs_found} coplanar face pair(s); moved {len(self.moves)} face(s) of "
                 f"{len({m.node_id for m in self.moves})} part(s) to {MARGIN * 1e3:g} mm apart"]
        if self.moves:
            parts.append("moved: " + some(self.moves, FaceMove.describe))
        if lifted := sorted({m.node_id for m in self.moves if m.kind == "ground"}):
            parts.append(f"lifted {len(lifted)} part(s) {MARGIN * 1e3:g} mm off the ground plane: " + ", ".join(lifted[:limit]))
        if hidden := self.hidden_contact:
            parts.append(f"{len(hidden)} pair(s) of hidden contact faces, not shipped (left out of the render meshes, "
                         f"see the package's contact_faces): " + some(hidden, lambda u: f"{u.a}/{u.b}"))
        if left := self.left:
            parts.append(f"unresolved {len(left)} (left to the author): " + some(left, Unresolved.describe))
        return " | ".join(parts)


# --------------------------------------------------------------------------- the parts' editable faces


def _linear(t: Transform) -> np.ndarray:
    from antigravity3d.kernel.settle import _linear as lin

    return lin(t)


@dataclass
class _Node:
    node: NodeIR
    world: np.ndarray       # 3x3 linear part of the full chain (parent chain . own R S)
    parent: np.ndarray      # 3x3 linear part of the parent chain
    # local axis -> (world axis, |world scale of that local axis|) when the local axis maps onto a world axis
    axes: dict[int, tuple[int, float]]


def _nodes(scene: SceneIR) -> dict[str, _Node]:
    by_id = {n.id: n for n in scene.nodes}
    out: dict[str, _Node] = {}
    for n in scene.nodes:
        chain, p = [], n.parent
        while p is not None:
            chain.append(by_id[p])
            p = by_id[p].parent
        a = np.eye(3)
        for anc in reversed(chain):
            a = a @ _linear(anc.transform)
        w = a @ _linear(n.transform)
        axes = {}
        for k in range(3):
            col = w[:, k]
            m = int(np.argmax(np.abs(col)))
            if abs(col[m]) > 0 and all(abs(col[x]) <= _ALIGNED * abs(col[m]) for x in range(3) if x != m):
                axes[k] = (m, float(abs(col[m])))
        out[n.id] = _Node(n, w, a, axes)
    return out


def _face_param(nd: _Node, axis: int) -> tuple[str, int, float] | None:
    """(param name, local axis, world length of one unit of the param) of the face of ``nd`` normal to world
    ``axis``, or None when that face is not a parameter (rotated part, round side, cone, sphere, mesh)."""
    kind = nd.node.kind
    for k, (m, s) in nd.axes.items():
        if m != axis:
            continue
        if kind == "box":
            return "size", k, s
        if kind == "cylinder" and k == 2:
            return "height", k, s
    return None


# --------------------------------------------------------------------------- pairs


@dataclass
class _Pair:
    i: int
    j: int
    axis: int | None        # None: the faces are not axis-aligned
    sign: int
    plane_i: float
    plane_j: float
    area: float = 0.0       # overlapping area
    corners: list = field(default_factory=list)  # corners of the overlap pieces (3D)
    cen: np.ndarray = field(default_factory=lambda: np.zeros(3))  # area-weighted centroid accumulator


def _poly_centroid(poly) -> tuple[float, float, float]:
    a = cx = cy = 0.0
    n = len(poly)
    for k in range(n):
        x0, y0 = poly[k]
        x1, y1 = poly[(k + 1) % n]
        c = x0 * y1 - x1 * y0
        a += c
        cx += (x0 + x1) * c
        cy += (y0 + y1) * c
    if abs(a) < 1e-300:
        return 0.0, float(np.mean([p[0] for p in poly])), float(np.mean([p[1] for p in poly]))
    return abs(a) / 2, cx / (3 * a), cy / (3 * a)


def _find_pairs(parts: list[coplanar.Part], solid: list[bool], window: float,
                eligible: list[bool] | None = None, pairs=None, hidden=None, skipped: set | None = None) -> dict[tuple, _Pair]:
    """Same-facing parallel faces of two parts less than ``window`` apart whose overlap is wider than Tier-1's sliver
    (ZFIGHT_EPS): every pair Tier-1 reports, and the pairs a float32 export can bring within its 1 mm. A triangle pair
    both of whose triangles do not ship (``hidden``, per part) is left out; its pair key goes to ``skipped``."""
    out: dict[tuple, _Pair] = {}
    if pairs is not None and eligible is None:  # only these pairs: only their parts take part
        used = {x for pr in pairs for x in pr}
        eligible = [q in used for q in range(len(parts))]
    c = coplanar.coplanar_contacts(parts, window, facing="same", solid=solid, eligible=eligible, pairs=pairs,
                                   width=min(ZFIGHT_EPS, window))
    for i in range(len(parts)):
        for a, lst in c.same[i].items():
            for j, b in lst:
                if j <= i:
                    continue
                ca, na = c.frames[i][0][a], c.frames[i][1][a]
                cb = c.frames[j][0][b]
                ax = int(np.argmax(np.abs(na)))
                aligned = all(abs(na[x]) <= 1e-6 for x in range(3) if x != ax)
                sign = 1 if na[ax] > 0 else -1
                key = (i, j, ax, sign) if aligned else (i, j, None, tuple(np.round(na, 3)))
                if hidden is not None and hidden[i] is not None and hidden[j] is not None and hidden[i][a] and hidden[j][b]:
                    if skipped is not None:
                        skipped.add(key)
                    continue
                pr = out.get(key)
                if pr is None:
                    pr = out[key] = _Pair(i, j, ax if aligned else None, sign,
                                          float(ca[:, ax].mean()), float(cb[:, ax].mean()))
                u, v = coplanar._plane_basis(na)
                poly = coplanar._overlap(coplanar._in_plane(ca, u, v), coplanar._in_plane(cb, u, v))
                if len(poly) < 3:
                    continue
                ar, x, y = _poly_centroid(poly)
                off = float(ca.mean(axis=0) @ na)
                pr.area += ar
                pr.cen = pr.cen + ar * (x * u + y * v + off * na)
                pr.corners.extend(px * u + py * v + off * na for px, py in poly)
    return out


# --------------------------------------------------------------------------- the step


class _State:
    def __init__(self, scene: SceneIR, meshes: list[MeshData]) -> None:
        self.ids = [m.node_id for m in meshes]
        self.index = {n: k for k, n in enumerate(self.ids)}
        self.points = [np.array(m.points, np.float64) for m in meshes]
        self.tris = [np.asarray(m.triangles, np.int64) for m in meshes]
        self.solid = [_solid(p, t) for p, t in zip(self.points, self.tris)]
        self.nodes = _nodes(scene)
        self.parents = {n.parent for n in scene.nodes if n.parent is not None}  # nodes that carry child parts
        self.lo = np.array([p.min(axis=0) if len(p) else np.full(3, np.inf) for p in self.points])
        self.hi = np.array([p.max(axis=0) if len(p) else np.full(3, -np.inf) for p in self.points])
        self.live = np.array([bool(len(t)) for t in self.tris])
        self._vol: dict[int, float] = {}
        self.hidden: list[np.ndarray] | None = None  # triangles that do not ship (see separate_coplanar)
        self.version = [0] * len(self.ids)  # bumped when a part moves (keys the pair cache below)
        self._pairs_of: dict[tuple, dict] = {}

    def set_points(self, k: int, p: np.ndarray) -> None:
        self.points[k] = p
        self.version[k] += 1
        self.lo[k], self.hi[k] = p.min(axis=0), p.max(axis=0)
        self._vol.pop(k, None)

    def volume(self, k: int) -> float:
        if k not in self._vol:
            self._vol[k] = float(_topology(self.points[k], self.tris[k]).volume) if self.solid[k] else 0.0
        return self._vol[k]

    def parts(self) -> list[coplanar.Part]:
        return [coplanar.Part(n, p, t) for n, p, t in zip(self.ids, self.points, self.tris)]

    def bounds(self, k: int) -> tuple[np.ndarray, np.ndarray]:
        return self.lo[k], self.hi[k]

    def extreme(self, k: int, axis: int, sign: int) -> float:
        c = self.points[k][:, axis]
        return float(c.max() if sign > 0 else c.min())

    def face_area(self, k: int, axis: int, sign: int) -> float:
        p, t = self.points[k], self.tris[k]
        c = p[t]
        n = np.cross(c[:, 1] - c[:, 0], c[:, 2] - c[:, 0])
        ln = np.linalg.norm(n, axis=1)
        on = (n[:, axis] * sign > 0.999999 * ln) & (np.abs(c[:, :, axis] - self.extreme(k, axis, sign)).max(axis=1) <= _ON_PLANE)
        return float(ln[on].sum() / 2)

    def moved(self, k: int, axis: int, sign: int, delta: float) -> np.ndarray:
        p = self.points[k].copy()
        on = np.abs(p[:, axis] - self.extreme(k, axis, sign)) <= _ON_PLANE
        p[on, axis] += sign * delta
        return p

    def inside(self, pt: np.ndarray, skip: set[int]) -> list[int]:
        out = []
        cand = np.flatnonzero(np.all((self.lo <= pt) & (self.hi >= pt), axis=1))
        for k in cand.tolist():
            if k in skip or not self.solid[k]:
                continue
            if _winding_inside(pt[None], self.points[k][self.tris[k]])[0]:
                out.append(k)
        return out

    def near(self, k: int, reach: float, points: np.ndarray | None = None) -> list[int]:
        p = self.points[k] if points is None else points
        lo, hi = p.min(axis=0) - reach, p.max(axis=0) + reach
        hit = np.all((self.lo <= hi) & (self.hi >= lo), axis=1) & self.live
        hit[k] = False
        return np.flatnonzero(hit).tolist()


def _touching(state: _State, k: int, pts: np.ndarray, others: list[int]) -> set[int]:
    """The parts among ``others`` whose surfaces come within CONTACT_TOL of part k (with points ``pts``)."""
    from antigravity3d.kernel.fast_ops import _PartGap

    idx = [k] + others
    valid, vols = [], []
    for q in idx:
        p = pts if q == k else state.points[q]
        t = state.tris[q]
        valid.append((MeshData(state.ids[q], "", p, t), state.solid[q], p.min(axis=0), p.max(axis=0)))
        # a face moved along its normal changes the volume, not whether the part is an axis box (all that is used)
        vols.append(state.volume(q) if q != k else float(_topology(p, t).volume) if state.solid[q] else 0.0)
    gap = _PartGap(valid, vols, CONTACT_TOL)
    return {idx[x] for x in range(1, len(idx)) if gap.touch(0, x)}


def _mount_faces(state: _State) -> set[tuple[int, int, int]]:
    """(part, world axis, sign) of every axis-aligned face in contact with an opposite-facing face of another part,
    and every bottom face on the ground (z = 0, where settle and Tier-1 put it)."""
    c = coplanar.coplanar_contacts(state.parts(), None, facing="opposite")
    out = {(k, 2, -1) for k in range(len(state.ids)) if state.live[k] and abs(state.lo[k][2] - GROUND_Z) <= CONTACT_TOL}
    for i in range(len(state.ids)):
        if c.frames[i] is None:
            continue
        for a in c.opposite[i]:
            n = c.frames[i][1][a]
            ax = int(np.argmax(np.abs(n)))
            out.add((i, ax, 1 if n[ax] > 0 else -1))
    return out


def separate_coplanar(scene: SceneIR, meshes: list[MeshData], margin: float = MARGIN, *,
                      hidden: list[np.ndarray] | None = None, max_rounds: int = 8) -> SeparationResult:
    """Moves of single faces that leave no same-facing coplanar overlap between parts (see the module docstring),
    and the IR ops that make them. Deterministic; the input is not changed.

    ``hidden``: per mesh, the triangles the exporter leaves out of the render meshes (``fast_ops.shipped_masks``, the
    export's own rule; None: all ship). A pair of faces that both do not ship is not drawn: it is reported as hidden
    contact faces and left alone. Two contact faces resting on one part that do ship (closed-mesh engines, contact
    faces kept by request) fight: the smaller part is sunk ``margin`` into the part it rests on (its mount face moves
    into that part, still in contact with it)."""
    t0 = time.perf_counter()
    res = SeparationResult()
    if len(meshes) < 2:
        res.ms = (time.perf_counter() - t0) * 1e3
        return res
    st = _State(scene, meshes)
    st.hidden = hidden
    window = margin - CONTACT_TOL
    skipped: set = set()
    mounts: set = set()  # computed once there is a pair to resolve
    done: set[tuple[int, int, int]] = set()   # faces moved already (each face moves once: the step terminates)
    last: dict[tuple, str] = {}
    first = None
    for _ in range(max_rounds):
        pairs = _find_pairs(st.parts(), st.solid, window, hidden=hidden, skipped=None if first is not None else skipped)
        if first is None:
            first = len(pairs)
            not_shipped = sorted(k for k in skipped if k not in pairs)
        if not pairs:
            break
        if not mounts:
            mounts = _mount_faces(st) or {(-1, -1, 0)}
        touched: set[int] = set()
        progress = False
        for key in sorted(pairs, key=lambda kv: (st.ids[kv[0]], st.ids[kv[1]], str(kv[2:]))):
            pr = pairs[key]
            if pr.i in touched or pr.j in touched:  # a part of it moved: the pair as it is now, if it is still there
                pr = _find_pairs(st.parts(), st.solid, window, pairs=[(pr.i, pr.j)], hidden=hidden).get(key)
                if pr is None:
                    continue
            why = _resolve(st, pr, margin, window, mounts, done, res)
            if why is None:
                touched.update((pr.i, pr.j))
                progress = True
                last.pop(_key(st, pr), None)
            else:
                last[_key(st, pr)] = why
        if not progress:
            break
    left = _find_pairs(st.parts(), st.solid, window, hidden=hidden) if res.moves else (pairs if first else {})
    res.unresolved = [Unresolved(a, b, f"{_face(*f)} faces: " + last.get((a, b, f), "not resolved within the step's rounds"))
                      for a, b, f in sorted(_key(st, p) for p in left.values())]
    res.unresolved += [Unresolved(st.ids[i], st.ids[j], f"{_face(ax, sg)} faces: {HIDDEN_CONTACT}")
                       for i, j, ax, sg in (not_shipped if first is not None else [])]
    res.pairs_found = (first or 0) + (len(not_shipped) if first is not None else 0)
    res.ops = _ops(st, res.moves)
    res.ms = (time.perf_counter() - t0) * 1e3
    return res


def _face(axis: int | None, sign) -> str:
    return "tilted" if axis is None else ("+" if sign > 0 else "-") + "xyz"[axis]


def _key(st: _State, p: _Pair) -> tuple:
    return (st.ids[p.i], st.ids[p.j], (p.axis, p.sign))


def _classify(st: _State, pr: _Pair) -> tuple[str, list[int], list[int]]:
    """("visible" | "enclosed" | "covered", enclosing parts, parts in front): probes just in front of and behind
    the overlap (its centre and its corners pulled 5 % toward it). Enclosed: one third part holds every probe on both
    sides. Covered: every front probe is inside some third part, not all behind it (contact faces)."""
    ax, s = pr.axis, pr.sign
    cen = pr.cen / pr.area
    probes = [cen] + [q + (cen - q) * 0.05 for q in pr.corners]
    step = s * _PROBE * np.eye(3)[ax]
    common: set[int] | None = None
    fronts: set[int] = set()
    all_front = True
    for q in probes:
        f = st.inside(q + step, {pr.i, pr.j})
        if not f:
            all_front = False
            common = set()
            continue
        fronts.update(f)
        both = set(f) & set(st.inside(q - step, {pr.i, pr.j}))
        common = both if common is None else common & both
    if common:
        return "enclosed", sorted(common), sorted(fronts)
    return ("covered" if all_front else "visible"), [], sorted(fronts)


def _resolve(st: _State, pr: _Pair, margin: float, window: float, mounts, done, res: SeparationResult,
             ) -> str | None:
    """Apply the first valid move for this pair (in st and res); None on success, else why none is valid."""
    i, j, ax, s = pr.i, pr.j, pr.axis, pr.sign
    if ax is None:
        return "not axis-aligned (a rotated part): left to the author"
    if pr.area <= 0:
        return "no overlap measured"
    kind, enclosers, fronts = _classify(st, pr)
    if (i, ax, s) in mounts and (j, ax, s) in mounts:
        if (ax, s) == (2, -1) and _on_ground(st, i) and _on_ground(st, j) and kind == "visible":
            return _lift(st, pr, margin, window, mounts, done, res)
        if kind != "covered":
            return "both parts are mounted by these faces (each on another part)"
        names = ", ".join(st.ids[k] for k in fronts)
        why = _sink(st, pr, margin, window, mounts, done, res, fronts)
        return None if why is None else f"both rest on {names}; the smaller cannot be sunk into it: {why}"
    if kind == "covered":
        kind = "visible"  # partly covered, one face free: separated like a visible pair
    ai, aj = st.face_area(i, ax, s), st.face_area(j, ax, s)
    vi = float(np.prod(np.ptp(st.points[i], axis=0)))
    vj = float(np.prod(np.ptp(st.points[j], axis=0)))
    order = [(i, j, ai), (j, i, aj)] if (ai, vi, st.ids[i]) <= (aj, vj, st.ids[j]) else [(j, i, aj), (i, j, ai)]
    reasons = []
    cen = pr.cen / pr.area
    for k, other, area in order:
        x_k, x_o = (s * pr.plane_i, s * pr.plane_j) if k == i else (s * pr.plane_j, s * pr.plane_i)
        out_d, in_d = x_o + margin - x_k, x_o - margin - x_k
        if kind == "enclosed":
            dirs = [out_d, in_d]
        else:
            # stand proud when this is one of the part's broad faces (the part is thinnest along the normal: a plate,
            # a rib, a latch) and more than half of it lies on the other face: sitting back would hide the plate
            # itself behind the other face. Otherwise (an end face, or a small overlap) sit back: no volume is added.
            f = pr.area / area if area > 0 else 0.0
            ext = np.ptp(st.points[k], axis=0)
            broad = ext[ax] <= ext.min() * (1 + 1e-6)
            dirs = [out_d, in_d] if broad and f > 0.5 else [in_d, out_d]
        for d in dirs:
            why = _try(st, k, other, ax, s, d, margin, window, mounts, done, enclosers, kind, res, cen)
            if why is None:
                return None
            reasons.append(f"{st.ids[k]} {'proud' if d > 0 else 'back'}: {why}")
    return "no valid move (" + "; ".join(dict.fromkeys(reasons)) + ")"


def _on_ground(st: _State, k: int) -> bool:
    return bool(abs(st.lo[k][2] - GROUND_Z) <= CONTACT_TOL)


def _lift(st: _State, pr: _Pair, margin: float, window: float, mounts, done, res) -> str | None:
    """Two bottoms on the ground plane overlap: lift the smaller part's bottom ``margin`` (its top stays), only if it
    stays in contact with a part that remains on the ground. The larger part keeps the ground contact its collider
    and the asset's bounds rest on; the underside is where a 2 mm step is not looked at."""
    i, j, ax, s = pr.i, pr.j, pr.axis, pr.sign
    k, other = (i, j) if (st.face_area(i, ax, s), st.ids[i]) <= (st.face_area(j, ax, s), st.ids[j]) else (j, i)
    why = _try(st, k, other, ax, s, -margin, margin, window, mounts, done, [], "ground", res, pr.cen / pr.area)
    return None if why is None else f"both rest on the ground; {st.ids[k]} cannot be lifted: {why}"


def _sink(st: _State, pr: _Pair, margin: float, window: float, mounts, done, res, fronts: list[int]) -> str | None:
    """Sink the smaller of two parts resting on the same face ``margin`` into the part(s) under them (only the
    smaller: the larger is the body things are mounted on, and is left as authored)."""
    i, j, ax, s = pr.i, pr.j, pr.axis, pr.sign
    ai, aj = st.face_area(i, ax, s), st.face_area(j, ax, s)
    order = [(i, j)] if (ai, st.ids[i]) <= (aj, st.ids[j]) else [(j, i)]
    cen = pr.cen / pr.area
    reasons = []
    for k, other in order:
        x_k, x_o = (s * pr.plane_i, s * pr.plane_j) if k == i else (s * pr.plane_j, s * pr.plane_i)
        why = _try(st, k, other, ax, s, x_o + margin - x_k, margin, window, mounts, done, fronts, "contact", res, cen)
        if why is None:
            return None
        reasons.append(f"{st.ids[k]}: {why}")
    return "; ".join(reasons)


def _try(st: _State, k: int, other: int, ax: int, s: int, d: float, margin: float, window: float, mounts, done,
         enclosers: list[int], kind: str, res: SeparationResult, centre: np.ndarray) -> str | None:
    """Validate moving part k's (ax, s) face by d (pushed further, up to twice the margin, past other same-facing
    faces it would land on); apply it when valid. None on success, else why not."""
    nd = st.nodes.get(st.ids[k])
    if nd is None or _face_param(nd, ax) is None:
        return "its face is not a size parameter (rotated, round side, or a kind without flat faces)"
    if st.ids[k] in st.parents:  # its translate changes: its children would move with it, unchecked
        return "it has child parts, which would move with it"
    if (k, ax, s) in mounts and kind not in ("contact", "ground"):
        return "it is mounted by that face"
    if (k, ax, s) in done:
        return "that face was moved already"
    ext = float(np.ptp(st.points[k][:, ax]))
    x_k = s * st.extreme(k, ax, s)
    reach = 2 * margin + CONTACT_TOL
    near = sorted(set(st.near(k, reach)))
    parts = st.parts()

    def mine(pairs):
        return {(st.ids[p.i], st.ids[p.j], p.axis, p.sign if p.axis is not None else 0): p
                for p in pairs.values() if k in (p.i, p.j)}

    ck = (k, tuple((q, st.version[q]) for q in [k, *near]))
    if ck not in st._pairs_of:  # the part's pairs as the scene is now (unchanged until it or a neighbour moves)
        st._pairs_of[ck] = mine(_find_pairs(parts, st.solid, window, pairs=[(min(k, q), max(k, q)) for q in near], hidden=st.hidden))
    was = st._pairs_of[ck]
    target = (st.ids[k], st.ids[other], ax, s) if k < other else (st.ids[other], st.ids[k], ax, s)
    why = "no move"
    for _ in range(6):
        if d > 2 * margin + CONTACT_TOL and kind == "visible":  # standing proud grows the outline: at most 2 margins
            return f"{why}; a clear position needs more than {2 * margin * 1e3:g} mm proud"
        if d < 0 and ext + d < max(margin, 0.5 * ext):
            return f"too thin to sit back ({ext * 1e3:.1f} mm)"
        new = st.moved(k, ax, s, d)
        if kind == "enclosed" and not _still_enclosed(st, k, ax, s, d, enclosers[0], centre):
            return f"its face would leave {st.ids[enclosers[0]]}"
        if kind == "contact" and not _sunk_inside(st, k, ax, s, d, enclosers):
            return "its face would not lie inside " + ", ".join(st.ids[e] for e in enclosers)
        near_after = sorted(set(near) | set(st.near(k, reach, new)))
        now_touching = _touching(st, k, new, near_after)
        if lost := _touching(st, k, st.points[k], near) - now_touching:
            return "it would lose contact with " + ", ".join(sorted(st.ids[q] for q in lost))
        if kind == "ground" and not any(_on_ground(st, q) for q in now_touching):
            return "it would touch no part that stays on the ground"
        trial = list(parts)
        trial[k] = coplanar.Part(st.ids[k], new, st.tris[k])
        now = mine(_find_pairs(trial, st.solid, window, pairs=[(min(k, q), max(k, q)) for q in near_after], hidden=st.hidden))
        # a pair it did not have, the pair being separated, or a pair it had whose overlap grows
        fresh = {key: p for key, p in now.items()
                 if key not in was or key == target or p.area > was[key].area * (1 + 1e-6) + 1e-12}
        if not fresh:
            st.set_points(k, new)
            done.add((k, ax, s))
            res.moves.append(FaceMove(st.ids[k], ax, s, float(d), st.ids[other], kind))
            return None
        names = ", ".join(sorted({a if b == st.ids[k] else b for a, b, *_ in fresh}))
        why = f"it would be coplanar with {names}"
        same_face = [p for p in fresh.values() if p.axis == ax and p.sign == s]
        if len(same_face) < len(fresh):
            return why  # a side face would meet another part: moving further does not help
        # land the face margin beyond the furthest face it met, in the same direction
        planes = [s * (p.plane_j if p.i == k else p.plane_i) for p in same_face]
        d = (max(planes) + margin - x_k) if d > 0 else (min(planes) - margin - x_k)
    return why


def _sunk_inside(st: _State, k: int, ax: int, s: int, d: float, under: list[int]) -> bool:
    """Every corner of k's face, moved by d (pulled slightly toward the face's centre), lies inside one of ``under``."""
    face = np.abs(st.points[k][:, ax] - st.extreme(k, ax, s)) <= _ON_PLANE
    probe = st.points[k][face]
    probe = probe + (probe.mean(axis=0) - probe) * 1e-3
    probe[:, ax] += s * d
    inside = np.zeros(len(probe), bool)
    for e in under:
        inside |= _winding_inside(probe, st.points[e][st.tris[e]])
    return bool(inside.all())


def _still_enclosed(st: _State, k: int, ax: int, s: int, d: float, e: int, centre: np.ndarray) -> bool:
    """Every corner of k's face that lay inside part e (pulled slightly toward the face's centre, so a corner on a
    surface does not count), and the overlap's centre, still lie inside e once the face is moved by d."""
    tri_e = st.points[e][st.tris[e]]
    face = np.abs(st.points[k][:, ax] - st.extreme(k, ax, s)) <= _ON_PLANE
    old = st.points[k][face]
    old = old + (old.mean(axis=0) - old) * 1e-3
    probe = np.concatenate([old[_winding_inside(old, tri_e)], centre[None]])
    probe[:, ax] += s * d
    return bool(_winding_inside(probe, tri_e).all())


def _ops(st: _State, moves: list[FaceMove]) -> list:
    """SetParam (size / height) and SetTransform (translate) per moved node, all of its moved faces combined."""
    per: dict[str, list[FaceMove]] = {}
    for m in moves:
        per.setdefault(m.node_id, []).append(m)
    ops: list = []
    for nid in sorted(per):
        nd = st.nodes[nid]
        node = nd.node
        shift = np.zeros(3)
        if node.kind == "box":
            size = [float(v) for v in node.params["size"]]
        else:
            height = float(node.params["height"])
        for m in per[nid]:
            name, k, unit = _face_param(nd, m.axis)
            if name == "size":
                size[k] += m.delta / unit
            else:
                height += m.delta / unit
            shift[m.axis] += m.sign * m.delta / 2
        if node.kind == "box":
            ops.append(SetParam(node_id=nid, name="size", value=tuple(size)))
        else:
            ops.append(SetParam(node_id=nid, name="height", value=height))
        local = np.linalg.solve(nd.parent, shift)
        t = node.transform
        ops.append(SetTransform(node_id=nid, transform=Transform(
            translate=tuple(float(x) for x in np.asarray(t.translate, np.float64) + local),
            rotate_euler_deg=t.rotate_euler_deg, scale=t.scale)))
    return ops


__all__ = ["CONTACT_TOL", "FaceMove", "MARGIN", "SeparationResult", "Unresolved", "separate_coplanar"]
