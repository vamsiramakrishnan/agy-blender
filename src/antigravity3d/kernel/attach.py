"""Attach parts that touch nothing: the deterministic remedy for a part (or a group of parts touching only each other)
beside another part, which settling cannot reach because it only drops a part straight down.

Each detached component (Tier-1's ``floating_part`` / ``floating_group``) is moved along the shortest line from one of its
own vertices to the nearest other part's surface until that vertex lies on it (or just short of it, or a millimetre into
it): the Blender check, which is the gate, counts a part as touching when one of its own vertices is within 2 mm of another
part (kernel/fast_ops.py, TOUCH_TOL). Nearest component first, and the asset's connectivity is read again after every move,
so a chain closes up from its anchored end. A move is made only within ``reach`` and only when it detaches nothing,
raises no Tier-1 critical issue and adds no interpenetration warning Tier-1 did not already report; anything else is left
to the author, whose request then names the part, the nearest part and the move in numbers (``nearest``).

Measured live 2026-11-22 (r5/live_scorecard/runs/r5_hydrant_C): five worker repair rounds could not connect five chain
links 15-27 mm beside the body; settle reported "floating part beside another part, not above anything: left to the
author" and the run was refused by the Blender check at its last step, after 16 minutes and 558,000 tokens.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np

from antigravity3d.ir import SceneIR, SetTransform, Transform

GAP_SHORT = 0.0005  # stop this far short of the surface (inside the Blender check's 2 mm)
INTO = 0.001  # or this far into it: "a millimetre of overlap"
# A move may make the moved parts cross the part they now rest on, or a neighbour, by at most this depth: what a contact
# is (a link's edge across its anchor's, a millimetre of overlap). Deeper is burying a part, and the move is not made.
# Tier-1 reports any crossing at all as an interpenetration warning (one probe inside is enough): measured on the hydrant,
# every move that brings chain_link_1 within 2 mm of anything crosses the anchor's edge by under 0.1 mm, with no vertex
# inside, and reads as a new warning; so new warnings are allowed up to this depth and no deeper.
BURY_MAX = 0.0015
# A vertex this near another part already lies on it (a flush contact): the line between them is a rounding error, so
# it has no direction to follow and "onto the surface" is where the part already is. Measured 2026-11-22 on the offline
# lamp post (a pole flush on its base, the pole's bottom face left out of the file): the nearest vertex was 4 nm away,
# the move "onto the surface" was 4 nm, the kernel then passed by rounding and Blender's qa_check still read the base
# as "not touching".
FLUSH = 1e-6


def facing_step(lo: np.ndarray, hi: np.ndarray, target_points: Any, into: float = INTO) -> np.ndarray:
    """The move that takes the box [lo, hi] ``into`` the target's box along the axis the two boxes face each other on
    (the axis they are farthest apart on, or overlap least on), past whatever gap they have on it: what makes a
    contact of a part lying flush on another."""
    tp = np.asarray(target_points, np.float64)
    sep = np.maximum(tp.min(0) - hi, lo - tp.max(0))
    axis = int(np.argmax(sep))
    sign = 1.0 if tp.min(0)[axis] + tp.max(0)[axis] >= lo[axis] + hi[axis] else -1.0
    step = np.zeros(3)
    step[axis] = sign * (max(float(sep[axis]), 0.0) + into)
    return step


def closest_on_triangles(p: np.ndarray, a: np.ndarray, b: np.ndarray, c: np.ndarray) -> np.ndarray:
    """Row-wise closest point on triangle (a, b, c) to p (Ericson, Real-Time Collision Detection 5.1.5)."""
    def dot(x: np.ndarray, y: np.ndarray) -> np.ndarray:
        return np.einsum("ij,ij->i", x, y)

    ab, ac, ap = b - a, c - a, p - a
    d1, d2 = dot(ab, ap), dot(ac, ap)
    bp = p - b
    d3, d4 = dot(ab, bp), dot(ac, bp)
    cp = p - c
    d5, d6 = dot(ab, cp), dot(ac, cp)
    va, vb, vc = d3 * d6 - d5 * d4, d5 * d2 - d1 * d6, d1 * d4 - d3 * d2
    out = np.empty_like(p)
    done = np.zeros(len(p), bool)

    def put(mask: np.ndarray, value: np.ndarray) -> None:
        m = mask & ~done
        out[m] = value[m]
        done[m] = True

    put((d1 <= 0) & (d2 <= 0), a)
    put((d3 >= 0) & (d4 <= d3), b)
    put((d6 >= 0) & (d5 <= d6), c)
    v = d1 / np.where(np.abs(d1 - d3) > 1e-300, d1 - d3, 1e-300)
    put((vc <= 0) & (d1 >= 0) & (d3 <= 0), a + ab * v[:, None])
    w = d2 / np.where(np.abs(d2 - d6) > 1e-300, d2 - d6, 1e-300)
    put((vb <= 0) & (d2 >= 0) & (d6 <= 0), a + ac * w[:, None])
    num, den = d4 - d3, (d4 - d3) + (d5 - d6)
    w2 = num / np.where(np.abs(den) > 1e-300, den, 1e-300)
    put((va <= 0) & (num >= 0) & ((d5 - d6) >= 0), b + (c - b) * w2[:, None])
    s = va + vb + vc
    s = np.where(np.abs(s) > 1e-300, s, 1e-300)
    put(np.ones(len(p), bool), a + ab * (vb / s)[:, None] + ac * (vc / s)[:, None])
    return out


@dataclass
class Gap:
    """The shortest line from a vertex of the moving parts to another part's surface: ``vector`` (metres, world) moves the
    vertex onto that surface."""

    length: float
    vector: np.ndarray
    target: str
    vertex_of: str


def nearest(moving: list[Any], others: list[Any], reach: float) -> Gap | None:
    """The shortest line from a vertex of ``moving`` (meshes) to the surface of one of ``others`` (meshes) within
    ``reach``; None when nothing is that near."""
    found = gaps(moving, others, reach)
    return found[0] if found else None


def gaps(moving: list[Any], others: list[Any], reach: float) -> list[Gap]:
    """Per part of ``others`` within ``reach``, the shortest line from a vertex of ``moving`` to its surface, nearest
    first."""
    out: list[Gap] = []
    lo = np.min([np.asarray(m.points).min(0) for m in moving], axis=0)
    hi = np.max([np.asarray(m.points).max(0) for m in moving], axis=0)
    for o in others:
        op = np.asarray(o.points, np.float64)
        if (np.maximum(np.maximum(op.min(0) - hi, lo - op.max(0)), 0.0) ** 2).sum() > reach * reach:
            continue
        tri = op[np.asarray(o.triangles)]
        tlo, thi = tri.min(axis=1), tri.max(axis=1)
        best: Gap | None = None
        for m in moving:
            pts = np.asarray(m.points, np.float64)
            plo, phi = pts.min(0), pts.max(0)
            ti = np.flatnonzero(np.all((tlo <= phi + reach) & (thi >= plo - reach), axis=1))
            if not len(ti):
                continue
            step = max(1, 200_000 // len(ti))
            for s0 in range(0, len(pts), step):
                p = np.repeat(pts[s0:s0 + step], len(ti), axis=0)
                t = tri[np.tile(ti, len(pts[s0:s0 + step]))]
                q = closest_on_triangles(p, t[:, 0], t[:, 1], t[:, 2])
                d = np.linalg.norm(q - p, axis=1)
                k = int(np.argmin(d))
                if d[k] <= reach and (best is None or d[k] < best.length):
                    best = Gap(float(d[k]), q[k] - p[k], o.node_id, m.node_id)
        if best is not None:
            out.append(best)
    return sorted(out, key=lambda g: (g.length, g.target))


def translation_ops(scene: SceneIR, moves: dict[str, np.ndarray]) -> list[SetTransform]:
    """``SetTransform`` ops applying world-space translations to nodes' local transforms (the parent chain's linear part
    inverted, as kernel/settle.py does for a drop)."""
    from antigravity3d.kernel.settle import _linear

    by_id = {n.id: n for n in scene.nodes}
    ops = []
    for nid in sorted(moves):
        node = by_id[nid]
        chain, p = [], node.parent
        while p is not None:
            if p in moves:
                raise ValueError(f"attach: {nid!r} and its ancestor {p!r} are both moved")
            chain.append(by_id[p])
            p = by_id[p].parent
        a = np.eye(3)
        for anc in reversed(chain):
            a = a @ _linear(anc.transform)
        d = np.linalg.solve(a, np.asarray(moves[nid], np.float64))
        t = node.transform
        ops.append(SetTransform(node_id=nid, transform=Transform(
            translate=tuple(float(x) for x in np.array(t.translate) + d), rotate_euler_deg=t.rotate_euler_deg, scale=t.scale)))
    return ops


def detached(t1: Any) -> list[list[str]]:
    """Tier-1's detached components: each floating part alone, each floating group with all its members."""
    out = []
    for i in t1.issues:
        if i.code == "floating_part" and i.node_id:
            out.append([i.node_id])
        elif i.code == "floating_group":
            out.append(sorted(n.strip() for n in i.detail.split(" touch each other")[0].split(",") if n.strip()))
    return out


@dataclass
class AttachResult:
    moves: dict[str, np.ndarray] = field(default_factory=dict)  # node -> world translation
    attached: list[str] = field(default_factory=list)  # one line per move made
    left: list[str] = field(default_factory=list)  # one line per component left to the author, with its numbers

    def ops(self, scene: SceneIR) -> list[SetTransform]:
        return translation_ops(scene, {k: v for k, v in self.moves.items() if np.any(v)})


def _key(t1: Any) -> tuple[set, int]:
    pen = {i.detail.split(":")[0] for i in t1.issues if i.code == "interpenetration"}
    return pen, sum(i.severity == "critical" for i in t1.issues)


def _zf(t1: Any) -> set:
    return {i.detail.split(":")[0] for i in t1.issues if i.code == "z_fighting"}


def crossing_depth(a: Any, b: Any) -> float:
    """How deep two parts cross: the largest distance to the other's surface of a vertex or face centre of either that
    lies inside the other (closed meshes; generalised winding number, as Tier-1's interpenetration probe)."""
    from antigravity3d.kernel.fast_ops import _winding_inside

    deepest = 0.0
    for x, y in ((a, b), (b, a)):
        px, py = np.asarray(x.points, np.float64), np.asarray(y.points, np.float64)
        ty = py[np.asarray(y.triangles)]
        probes = np.concatenate([px, px[np.asarray(x.triangles)].mean(axis=1)])
        probes = probes[np.all((probes >= py.min(0)) & (probes <= py.max(0)), axis=1)]
        if not len(probes):
            continue
        inside = probes[_winding_inside(probes, ty)]
        for p in inside:
            q = closest_on_triangles(np.repeat(p[None], len(ty), 0), ty[:, 0], ty[:, 1], ty[:, 2])
            deepest = max(deepest, float(np.linalg.norm(q - p, axis=1).min()))
    return deepest


def attach(meshes: list[Any], budget: Any, reach: float, *, only: set[str] | None = None) -> AttachResult:
    """Move detached components onto their nearest part (see the module text). ``only``: move nothing else (an author's
    patch: the parts it changed). Returns the world moves; the caller turns them into ops (``AttachResult.ops``)."""
    from antigravity3d.kernel.fast_ops import tier1_qa

    res = AttachResult()
    cur = list(meshes)
    t1 = tier1_qa(cur, budget)
    pen0, crit0 = _key(t1)

    def others_of(comp: list[str], meshes_: list[Any]) -> tuple[list[Any], list[Any]]:
        inside = set(comp)
        return [m for m in meshes_ if m.node_id in inside], [m for m in meshes_ if m.node_id not in inside]

    def moves_onto(comp: list[str], g: Gap) -> list[np.ndarray]:
        """Along the vertex's line: just short of the surface, on it, a millimetre into it; then along the axis the two
        parts' boxes face each other on, a millimetre past their gap (a part lying flush on another whose vertices all
        fall beyond the other's face, a skid under a bevelled body: crossing it by a millimetre is a contact the Blender
        check sees; lying flush is one it sees or not by rounding, r5_crate_C)."""
        unit = g.vector / max(g.length, 1e-12)
        # a vertex already on the target (FLUSH) has no line to follow: only the step along the facing axis is tried
        out = [] if g.length < FLUSH else [unit * s for s in (g.length - GAP_SHORT, g.length, g.length + INTO) if s > 0]
        mine = [m for m in cur if m.node_id in comp]
        tgt = next(m for m in cur if m.node_id == g.target)
        lo = np.min([np.asarray(m.points).min(0) for m in mine], axis=0)
        hi = np.max([np.asarray(m.points).max(0) for m in mine], axis=0)
        out.append(facing_step(lo, hi, tgt.points))
        return out

    def try_move(comp: list[str], g: Gap, t_now: Any, pen_now: set) -> tuple | None:
        """The first of ``moves_onto`` that attaches ``comp`` without detaching another part, raising a critical issue
        or burying a part."""
        before = sum(len(c) for c in detached(t_now))
        for delta in moves_onto(comp, g):
            shift = float(np.linalg.norm(delta))
            if shift > reach + INTO:
                continue
            trial = [replace(m, points=np.asarray(m.points, np.float64) + delta,
                             collider_points=None if m.collider_points is None else m.collider_points + delta,
                             run=None if m.run is None else m.run.moved(lambda p: p + delta))
                     if m.node_id in comp else m for m in cur]
            t2 = tier1_qa(trial, budget)
            pen2, crit2 = _key(t2)
            still = {n for c in detached(t2) for n in c}
            if still & set(comp) or crit2 > crit0 or len(still) > before - len(comp):
                continue
            if _zf(t2) - _zf(t_now):
                continue  # it would put a face of the moved part in another's plane: the Blender check refuses that too
            by = {m.node_id: m for m in trial}
            new = [pair.split("/") for pair in pen2 - pen_now]
            if any(len(ab) != 2 or ab[0] not in by or ab[1] not in by or crossing_depth(by[ab[0]], by[ab[1]]) > BURY_MAX
                   for ab in new):
                continue  # it would bury a part
            return trial, t2, pen2, delta, shift
        return None

    pen = pen0
    diag = {m.node_id: float(np.linalg.norm(np.ptp(np.asarray(m.points), axis=0))) for m in meshes}
    body = max(diag, key=diag.get)  # the asset's biggest part never moves (the Blender check's biggest island)
    for _ in range(4 * len(meshes) + 1):
        comps = [c for c in detached(t1) if (only is None or set(c) <= only) and body not in c]
        if not comps:
            break
        loose = {n for c in detached(t1) if body not in c for n in c}  # the body's own group is an anchor all the same
        # Every acceptable move of every detached component onto one of its three nearest attached parts, then the one
        # that leaves the most of the other detached components with an attached part within reach (a link moved onto
        # the body when its neighbour link is nearly as near would leave the next link stranded: the hydrant's chain),
        # the shortest of those. With nothing to tell them apart, the nearest move, as the rule says.
        options = []
        for comp in comps:
            mine, rest = others_of(comp, cur)
            for g in [g for g in gaps(mine, rest, reach) if g.target not in loose][:3]:
                made = try_move(comp, g, t1, pen)
                if made:
                    trial, t2 = made[0], made[1]
                    loose2 = {n for c in detached(t2) if body not in c for n in c}
                    reachable = sum(1 for c in detached(t2)
                                    if any(x.target not in loose2 for x in gaps(*others_of(c, trial), reach)))
                    options.append((-reachable, made[4], comp, g, made))
        if not options:
            break
        options.sort(key=lambda o: (o[0], o[1], o[2]))
        _r, shift, comp, g, (cur, t1, pen, delta, _s) = options[0]
        for n in comp:
            res.moves[n] = res.moves.get(n, np.zeros(3)) + delta
        res.attached.append(f"{', '.join(comp)} moved {shift * 1e3:.1f} mm onto {g.target} "
                            f"({', '.join(f'{x * 1e3:+.1f}' for x in delta)} mm)")
    for comp in detached(t1):
        if only is not None and not set(comp) <= only or body in comp:
            continue
        mine, rest = others_of(comp, cur)
        found = gaps(mine, rest, reach)
        if not found:
            res.left.append(f"{', '.join(comp)}: no part within {reach * 1e3:.0f} mm of it")
        else:
            g = found[0]
            res.left.append(f"{', '.join(comp)}: {g.length * 1e3:.1f} mm from {g.target} (a vertex of {g.vertex_of} moves onto "
                            f"it by {', '.join(f'{x * 1e3:+.1f}' for x in g.vector)} mm); no move within {reach * 1e3:.0f} mm "
                            "attaches it without burying a part or detaching another")
    return res


__all__ = ["FLUSH", "INTO", "AttachResult", "Gap", "attach", "closest_on_triangles", "detached", "facing_step", "gaps", "nearest",
           "translation_ops"]
