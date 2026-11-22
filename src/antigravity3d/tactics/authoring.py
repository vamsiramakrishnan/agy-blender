"""Scene-IR authoring interfaces: the non-deterministic seam of Layer 4.

``IRAuthor`` is the only place a model is allowed to decide *what* to build. Everything downstream
(tournaments, pyramid, DAGs) treats it as an injected dependency, so the whole pipeline runs offline with
``TemplateAuthor`` and with a real model-driven agent through ``CallableAuthor`` / ``AgentAuthor``.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import math
import random
import re
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

import numpy as np

from antigravity3d.ir import (
    NODE_PARAMS, IRPatch, IROp, MaterialIR, NodeIR, SceneIR, SetParam, SetTransform, Transform, UpsertMaterial, UpsertNode, apply_patch,
)
from antigravity3d import service_answer
from antigravity3d.ir_shortcuts import local_bounds
from antigravity3d.kernel.compile_ir import triangle_count
from antigravity3d.kernel.settle import beside_a_part
from antigravity3d.state import EngineBudget

GROUND_TOL = 0.01
CURVED = ("cylinder", "cone", "sphere", "capsule", "lathe")  # kinds with a segments count (_shrink lowers it)
STAGE_TIMEOUT_S = 300.0  # harness stage-agent session (a different path from the governed one-shot calls)


@runtime_checkable
class IRAuthor(Protocol):
    async def propose(self, prompt: str, *, budget: EngineBudget, n: int, context: dict[str, Any]) -> list[SceneIR]:
        """``n`` competing blockouts for ``prompt`` (context carries asset_name, ortho_uri, seed, ...)."""

    async def repair(self, scene: SceneIR, *, action: Any, located: dict[str, list[str]], manifest: str) -> IRPatch | None:
        """A patch addressing ``located`` (defect id or ``tier1:<code>`` -> node ids), or None to decline."""


# --------------------------------------------------------------------------- geometry helpers (IR space, no USD)


def _rot(deg: tuple[float, float, float]) -> np.ndarray:
    rx, ry, rz = np.radians(deg)
    cx, sx, cy, sy, cz, sz = np.cos(rx), np.sin(rx), np.cos(ry), np.sin(ry), np.cos(rz), np.sin(rz)
    return (np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1.0]]) @ np.array([[cy, 0, sy], [0, 1.0, 0], [-sy, 0, cy]])
            @ np.array([[1.0, 0, 0], [0, cx, -sx], [0, sx, cx]]))


def _local(t: Transform) -> np.ndarray:
    m = np.eye(4)
    m[:3, :3] = _rot(t.rotate_euler_deg) @ np.diag(t.scale)
    m[:3, 3] = t.translate
    return m


def _local_box(n: NodeIR) -> tuple[np.ndarray, np.ndarray] | None:
    """(lo, hi) of a geometric node in its own frame; None for a group or a mesh_ref."""
    return local_bounds(n.kind, n.params)


def world_bounds(scene: SceneIR) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """World AABB per geometric node (corner-transformed, conservative for rotated nodes)."""
    by = {n.id: n for n in scene.nodes}
    cache: dict[str, np.ndarray] = {}

    def mat(nid: str) -> np.ndarray:
        if nid not in cache:
            n = by[nid]
            cache[nid] = (mat(n.parent) if n.parent else np.eye(4)) @ _local(n.transform)
        return cache[nid]

    out: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for n in scene.nodes:
        box = _local_box(n)
        if box is None:
            continue
        corners = np.array([[x, y, z] for x in (box[0][0], box[1][0]) for y in (box[0][1], box[1][1]) for z in (box[0][2], box[1][2])])
        w = (mat(n.id) @ np.c_[corners, np.ones(8)].T).T[:, :3]
        out[n.id] = (w.min(0), w.max(0))
    return out


def _world_to_local_delta(scene: SceneIR, node_id: str, delta: np.ndarray) -> np.ndarray:
    by = {n.id: n for n in scene.nodes}
    chain, p = [], by[node_id].parent
    while p:
        chain.append(by[p])
        p = by[p].parent
    a = np.eye(3)
    for anc in reversed(chain):
        a = a @ (_rot(anc.transform.rotate_euler_deg) @ np.diag(anc.transform.scale))
    return np.linalg.solve(a, delta)


def _move(scene: SceneIR, node_id: str, delta: np.ndarray) -> SetTransform:
    t = next(n for n in scene.nodes if n.id == node_id).transform
    d = _world_to_local_delta(scene, node_id, delta)
    return SetTransform(node_id=node_id, transform=Transform(
        translate=tuple(float(x) for x in np.array(t.translate) + d), rotate_euler_deg=t.rotate_euler_deg, scale=t.scale))


def count_tris(scene: SceneIR) -> int:
    return sum(triangle_count(n) for n in scene.nodes)  # the kernel's cost function, the one source


# --------------------------------------------------------------------------- mechanical repairs


def _reground(scene: SceneIR, flagged: set[str]) -> list[IROp]:
    """Drop each flagged node onto the nearest support below it (or the ground); lift sunk nodes. A flagged node with
    nothing under it but another part within reach is left where it is (kernel.settle.beside_a_part)."""
    ops: list[IROp] = []
    cur = scene
    for _ in range(len(scene.nodes) + 1):
        bounds = world_bounds(cur)
        todo = sorted((nid for nid in flagged if nid in bounds), key=lambda i: bounds[i][0][2])
        step = None
        for nid in todo:
            lo, hi = bounds[nid]
            below = [h[2] for o, (l, h) in bounds.items() if o != nid and h[2] <= lo[2] + 1e-6
                     and (np.minimum(h[:2], hi[:2]) - np.maximum(l[:2], lo[:2]) > 1e-6).all()]
            if not below and beside_a_part(bounds, nid):
                continue  # beside the part it belongs to, not hovering: a drop to the ground would carry it away
            dz = (max(below) if below else 0.0) - lo[2]
            if abs(dz) > 1e-6 and (below or abs(lo[2]) > GROUND_TOL):
                step = _move(cur, nid, np.array([0.0, 0.0, dz]))
                break
        if step is None:
            break
        ops.append(step)
        cur = apply_patch(cur, IRPatch(ops=[step]))
        flagged = flagged - {step.node_id}
    return ops


def _lift_all(scene: SceneIR) -> list[IROp]:
    """Whole asset off the ground (or sunk): translate every root node by -min z."""
    b = world_bounds(scene)
    if not b:
        return []
    dz = -min(lo[2] for lo, _ in b.values())
    return [_move(scene, n.id, np.array([0.0, 0.0, dz])) for n in scene.nodes if n.parent is None] if abs(dz) > 1e-6 else []


def _shrink(scene: SceneIR, max_tris: int) -> list[IROp]:
    """Reduce segments/rings of curved nodes until the analytic tri count fits 90% of the budget."""
    target, cur = int(max_tris * 0.9), scene
    ops: list[IROp] = []
    for _ in range(6):
        tris = count_tris(cur)
        if tris <= target:
            break
        k = max(0.25, math.sqrt(target / tris))
        # rounded bevels go to a chamfer first: they cost the most per part and change no silhouette
        step: list[IROp] = [SetParam(node_id=n.id, name="bevel_segments", value=1) for n in cur.nodes
                            if n.params.get("bevel") and int(n.params.get("bevel_segments", 1)) > 1]
        for n in cur.nodes if not step else ():
            if n.kind not in CURVED:
                continue
            fields = NODE_PARAMS[n.kind].model_fields
            for name, floor in (("segments", 8), ("rings", 6)):
                if name not in fields:
                    continue
                default = fields[name].default
                v = int(n.params.get(name, default))
                nv = max(floor, int(v * k))
                if nv < v:
                    step.append(SetParam(node_id=n.id, name=name, value=nv))
        if not step:
            break
        ops += step
        cur = apply_patch(cur, IRPatch(ops=step))
    return ops


def _separate(scene: SceneIR, nodes: set[str]) -> list[IROp]:
    """Push interpenetrating flagged nodes apart along the smallest overlap axis (smaller node moves)."""
    ops: list[IROp] = []
    cur = scene
    for _ in range(len(nodes) * 2):
        b = world_bounds(cur)
        ids = sorted(i for i in nodes if i in b)
        best = None
        for i, a in enumerate(ids):
            for c in ids[i + 1:]:
                (la, ha), (lc, hc) = b[a], b[c]
                ov = np.minimum(ha, hc) - np.maximum(la, lc)
                if (ov > 1e-4).all() and (best is None or ov.min() < best[0]):
                    vol = lambda lo, hi: float(np.prod(hi - lo))  # noqa: E731
                    mover, other = (a, c) if vol(la, ha) <= vol(lc, hc) else (c, a)
                    best = (float(ov.min()), int(ov.argmin()), mover, other)
        if best is None:
            break
        amt, axis, mover, other = best
        cm, co = (b[mover][0] + b[mover][1]) / 2, (b[other][0] + b[other][1]) / 2
        d = np.zeros(3)
        d[axis] = (amt + 1e-3) * (1.0 if cm[axis] >= co[axis] else -1.0)
        step = _move(cur, mover, d)
        ops.append(step)
        cur = apply_patch(cur, IRPatch(ops=[step]))
    return ops


# --------------------------------------------------------------------------- templates


_STOP = {"a", "an", "the", "with", "and", "of", "for", "in", "on", "to", "my", "some"}


def _slug(text: str) -> str:
    words = [w for w in re.findall(r"[A-Za-z0-9]+", text) if w.lower() not in _STOP][:3]
    name = "".join(w.capitalize() for w in words) or "Asset"
    return name if name[0].isalpha() else f"A{name}"


GRID = 4  # template coordinates are metres to 0.1 mm


def _snap(centre: float, size: float) -> tuple[float, float]:
    """(centre, size) of the extent centre +/- size/2 with both ends on the 0.1 mm grid. Rounding the size and the
    centre each (as the helpers did) puts an end up to 0.1 mm off its grid value, so two parts whose ends are the
    same number (a leg's top and the table top's underside) missed each other by up to 100 micrometres. Rounded at
    the ends, equal ends stay equal, and the size and centre are exact for them (a centre between two grid values needs
    one more decimal)."""
    lo, hi = round(centre - size / 2, GRID), round(centre + size / 2, GRID)
    return round((lo + hi) / 2, GRID + 1), round(hi - lo, GRID)


def _box(i: str, size: tuple[float, float, float], at: tuple[float, float, float], mat: str, parent: str | None = None, **kw: Any) -> NodeIR:
    snapped = [_snap(a, s) for a, s in zip(at, size)]
    return NodeIR(id=i, kind="box", params={"size": tuple(s for _, s in snapped)},
                  transform=Transform(translate=tuple(c for c, _ in snapped)), material_id=mat, parent=parent, **kw)


def _cyl(i: str, r: float, h: float, at: tuple[float, float, float], mat: str, seg: int, kind: str = "cylinder") -> NodeIR:
    z, height = _snap(at[2], h)
    return NodeIR(id=i, kind=kind, params={"radius": round(r, GRID), "height": height, "segments": seg},
                  transform=Transform(translate=(round(at[0], GRID), round(at[1], GRID), z)), material_id=mat)


# (id, base colour, metallic, roughness, surface words: the material and its finish, never a part; see MaterialIR.surface)
_PRIMARY = (("wood", (0.52, 0.36, 0.2), 0.0, 0.8, "weathered oak wood with open grain, matte"),
            ("metal", (0.55, 0.56, 0.6), 1.0, 0.4, "brushed steel with faint scratches"),
            ("stone", (0.5, 0.5, 0.48), 0.0, 0.9, "rough grey granite, weathered"),
            ("plastic", (0.2, 0.45, 0.75), 0.0, 0.5, "blue moulded plastic, satin finish"))
_IRON = MaterialIR(id="iron", base_color=(0.24, 0.24, 0.27), metallic=1.0, roughness=0.45,
                   surface="dark wrought iron, lightly rusted")


def _primary(prompt: str) -> MaterialIR:
    low = prompt.lower()
    key = next((k for k, *_ in _PRIMARY if k in low or (k == "metal" and re.search(r"steel|alumin|brass", low))), "wood")
    _, c, m, r, surface = next(p for p in _PRIMARY if p[0] == key)
    return MaterialIR(id=key, base_color=c, metallic=m, roughness=r, surface=surface)


def _crate(rng: random.Random, name: str, prompt: str) -> SceneIR:
    pm = _primary(prompt)
    w = rng.uniform(0.7, 1.1)
    d = w * rng.uniform(0.85, 1.0)
    h = rng.uniform(0.6, 0.95)
    lid_t, bt = 0.05, rng.uniform(0.09, 0.14)
    nodes = [_box("body", (w, d, h), (0, 0, h / 2), pm.id),
             _box("lid", (w + 0.04, d + 0.04, lid_t), (0, 0, h + lid_t / 2), pm.id)]
    for k, (sx, sy) in enumerate([(1, 1), (-1, 1), (1, -1), (-1, -1)]):
        nodes.append(_box(f"bracket_{k}", (bt, bt, 0.03), (sx * (w / 2 + 0.02 - bt / 2), sy * (d / 2 + 0.02 - bt / 2), h + lid_t + 0.015), "iron"))
    straps = rng.choice([2, 2, 3])
    ys = np.linspace(-d / 2 + 0.12, d / 2 - 0.12, straps)
    for side, sx in (("r", 1), ("l", -1)):
        for j, y in enumerate(ys):
            nodes.append(_box(f"strap_{side}{j}", (0.04, 0.06, h), (sx * (w / 2 + 0.02), float(y), h / 2), "iron"))
    return SceneIR(asset_name=name, nodes=nodes, materials=[pm, _IRON])


def _barrel(rng: random.Random, name: str, prompt: str) -> SceneIR:
    pm = _primary(prompt)
    r, h, seg = rng.uniform(0.3, 0.45), rng.uniform(0.8, 1.05), rng.choice([16, 20, 24])
    nodes = [_cyl("body", r, h, (0, 0, h / 2), pm.id, seg)]
    hoops = rng.choice([2, 3])
    for k, z in enumerate(np.linspace(0.18 * h, 0.82 * h, hoops)):
        nodes.append(_cyl(f"hoop_{k}", r * 1.04, 0.04, (0, 0, float(z)), "iron", seg))
    nodes.append(_cyl("top_ring", r * 0.96, 0.03, (0, 0, h + 0.015), "iron", seg))
    return SceneIR(asset_name=name, nodes=nodes, materials=[pm, _IRON])


def _table(rng: random.Random, name: str, prompt: str) -> SceneIR:
    pm = _primary(prompt)
    w, d, h = rng.uniform(1.0, 1.6), rng.uniform(0.6, 0.9), rng.uniform(0.7, 0.8)
    t, leg = 0.06, rng.uniform(0.05, 0.09)
    nodes = [_box("top", (w, d, t), (0, 0, h - t / 2), pm.id)]
    for k, (sx, sy) in enumerate([(1, 1), (-1, 1), (1, -1), (-1, -1)]):
        nodes.append(_box(f"leg_{k}", (leg, leg, h - t), (sx * (w / 2 - leg / 2 - 0.03), sy * (d / 2 - leg / 2 - 0.03), (h - t) / 2), pm.id))
    return SceneIR(asset_name=name, nodes=nodes, materials=[pm])


def _lamp(rng: random.Random, name: str, prompt: str) -> SceneIR:
    h, seg = rng.uniform(2.6, 3.6), rng.choice([12, 16])
    br = rng.uniform(0.12, 0.2)
    lamp = MaterialIR(id="glass", base_color=(1.0, 0.92, 0.7), roughness=0.2, surface="warm frosted glass, smooth")
    top = round(0.12 + h, GRID)  # the pole's top end, which the head's lowest point rests on
    radius = round(br, GRID)
    nodes = [_cyl("base", 0.16, 0.12, (0, 0, 0.06), "iron", seg), _cyl("pole", 0.04, h, (0, 0, 0.12 + h / 2), "iron", seg),
             NodeIR(id="head", kind="sphere", params={"radius": radius, "segments": seg, "rings": 8},
                    transform=Transform(translate=(0, 0, round(top + radius, GRID))), material_id="glass")]
    return SceneIR(asset_name=name, nodes=nodes, materials=[_IRON, lamp])


def _shelf(rng: random.Random, name: str, prompt: str) -> SceneIR:
    pm = _primary(prompt)
    w, d, H, n = rng.uniform(0.8, 1.2), rng.uniform(0.25, 0.4), rng.uniform(1.4, 2.0), rng.choice([3, 4, 5])
    side = 0.04
    nodes = [_box(f"side_{s}", (side, d, H), (sx * (w / 2 - side / 2), 0, H / 2), pm.id) for s, sx in (("l", -1), ("r", 1))]
    for k, z in enumerate(np.linspace(0.015, H - 0.015, n)):
        nodes.append(_box(f"board_{k}", (w - 2 * side, d, 0.03), (0, 0, float(z)), pm.id))
    return SceneIR(asset_name=name, nodes=nodes, materials=[pm])


# Modular kit pieces. Every size comes from the kit's metric (kernel/kit_metric.py), decided once per kit and passed
# in ``context["kit_metric"]``; a piece never draws its own dimensions, or the pieces would not snap (audit K1-K3).
# Pieces are built in the corner frame (module volume [0, dims]) and shifted to the metric's pivot convention. Trim
# bands run the full depth (so the mating faces, both ends and -Y, stay flat and full) and stand proud of the +Y face by
# the metric's relief, the one face no neighbour ever butts against (kernel/kit_metric.py, "Module volume"). Large
# surfaces are stacks of parts whose faces fit the shared trim sheet's strip (``max_face_m``), so the trim mapper never
# shrinks them and every piece keeps the sheet's texel density (audit K6). Trim bands have one height kit-wide so they
# line up across joints, and so do the course lines (one coursing per kit); what varies between variants of a piece type
# is detail that no joint depends on: a string course on a wall's +Y face, a floor's inlay, a pillar's drums.
KIT_BAND_BASE_M = 0.2
KIT_BAND_TOP_M = 0.15
_CONCRETE = MaterialIR(id="concrete", base_color=(0.5, 0.5, 0.48), roughness=0.9, surface="cast grey concrete with fine pores")


def _cuts(a: float, b: float, n: int) -> list[float]:
    return [round(a + (b - a) * k / n, 6) for k in range(n + 1)]


def _blocks(prefix: str, lo: tuple[float, float, float], hi: tuple[float, float, float], mat: str, max_face: float,
            keep: int, at: np.ndarray, extra: int = 0, zcuts: list[float] | None = None) -> list[NodeIR]:
    """Boxes filling [lo, hi]: the two axes other than ``keep`` are cut into parts of at most ``max_face`` (``extra``
    more cuts on the larger of them), so every face of every part fits the trim strip on its shorter side. ``zcuts``
    gives the z edges explicitly (kit-wide coursing, so course lines continue across the joints of a run)."""
    counts = [1, 1, 1]
    split = [i for i in range(3) if i != keep]
    for i in split:
        counts[i] = max(1, math.ceil((hi[i] - lo[i]) / max_face - 1e-9))
    counts[max(split, key=lambda i: hi[i] - lo[i])] += extra
    edges = [_cuts(lo[i], hi[i], counts[i]) for i in range(3)]
    if zcuts is not None:
        edges[2] = [round(z, 6) for z in zcuts]
        counts[2] = len(edges[2]) - 1
    nodes = []
    for i in range(counts[0]):
        for j in range(counts[1]):
            for k in range(counts[2]):
                a = np.array([edges[0][i], edges[1][j], edges[2][k]])
                b = np.array([edges[0][i + 1], edges[1][j + 1], edges[2][k + 1]])
                suffix = "_".join(str(v) for v, c in zip((i, j, k), counts) if c > 1)
                nodes.append(NodeIR(id=f"{prefix}_{suffix}" if suffix else prefix, kind="box", params={"size": tuple(float(v) for v in b - a)},
                                    transform=Transform(translate=tuple(float(v) for v in (a + b) / 2 + at)), material_id=mat))
    return nodes


def _metric(metric: Any) -> Any:
    from antigravity3d.kernel.kit_metric import KitMetric  # kernel module; imported here to keep this module's import light

    return metric if isinstance(metric, KitMetric) else KitMetric.interior()


def _bands(metric: Any) -> tuple[float, float]:
    H = metric.wall_height_m
    return min(KIT_BAND_BASE_M, metric.max_face_m, H / 4), min(KIT_BAND_TOP_M, metric.max_face_m, H / 4)


def _coursing(m: Any) -> list[float]:
    """The kit's course lines: equal courses between the base band and the cornice, each fitting the trim strip. One
    set for every wall-type piece, so a course line runs on unbroken from a wall through a doorway's jambs."""
    bt, ct = _bands(m)
    top = m.wall_height_m - ct
    return _cuts(bt, top, max(1, math.ceil((top - bt) / m.max_face_m - 1e-9)))


def _between(cuts: list[float], a: float, b: float) -> list[float]:
    return [a, *[z for z in cuts if a + 1e-6 < z < b - 1e-6], b]


def _wall(rng: random.Random, name: str, prompt: str, metric: Any = None) -> SceneIR:
    m = _metric(metric)
    L, D, H = m.dims("wall_segment")
    at, f = m.origin((L, D, H)), m.max_face_m
    bt, ct = _bands(m)
    cuts = _coursing(m)
    nodes = [*_blocks("base", (0, 0, 0), (L, D, bt), "iron", f, 0, at),
             *_blocks("course", (0, 0, bt), (L, D, H - ct), "concrete", f, 0, at, zcuts=cuts),
             *_blocks("cornice", (0, 0, H - ct), (L, D, H), "iron", f, 0, at), *_relief(m, [(0, L)], bt, ct, at)]
    if m.relief_m > 0 and len(cuts) > 2 and rng.random() < 0.5:  # variant: a string course proud of +Y on a course line
        z = cuts[len(cuts) // 2]
        h = min(0.1, f)
        nodes += _blocks("string_course", (0, D, z - h / 2), (L, D + m.relief_m, z + h / 2), "iron", f, 0, at)
    return SceneIR(asset_name=name, materials=[_CONCRETE, _IRON], nodes=nodes)


def _relief(m: Any, spans: list[tuple[float, float]], bt: float, ct: float, at: np.ndarray) -> list[NodeIR]:
    """The bands' relief, proud of the +Y face by ``m.relief_m``: the base band over each x span, the cornice over the
    whole length. Same heights and depth on every piece type, so the relief continues across the joints of a run."""
    r, D, H, L, f = m.relief_m, m.wall_thickness_m, m.wall_height_m, m.module_m, m.max_face_m
    if r <= 0:
        return []
    out = []
    for k, (x0, x1) in enumerate(spans):
        out += _blocks(f"base_relief_{k}" if len(spans) > 1 else "base_relief", (x0, D, 0), (x1, D + r, bt), "iron", f, 0, at)
    return out + _blocks("cornice_relief", (0, D, H - ct), (L, D + r, H), "iron", f, 0, at)


def _floor(rng: random.Random, name: str, prompt: str, metric: Any = None) -> SceneIR:
    m = _metric(metric)
    L, _, T = m.dims("floor_tile")
    at, f = m.origin((L, L, T)), m.max_face_m
    if rng.random() < 0.5:  # plain boards
        nodes = _blocks("board", (0, 0, 0), (L, L, T), "concrete", f, 0, at, extra=rng.choice([0, 1]))
    else:  # boards either side of an iron inlay along the tile's centre line (it continues across tile joints)
        c, w = L / 2, min(0.1, f)
        nodes = [*_blocks("board_a", (0, 0, 0), (L, c - w / 2, T), "concrete", f, 0, at),
                 *_blocks("inlay", (0, c - w / 2, 0), (L, c + w / 2, T), "iron", f, 0, at),
                 *_blocks("board_b", (0, c + w / 2, 0), (L, L, T), "concrete", f, 0, at)]
    return SceneIR(asset_name=name, materials=[_CONCRETE, _IRON], nodes=nodes)


def _pillar(rng: random.Random, name: str, prompt: str, metric: Any = None) -> SceneIR:
    m = _metric(metric)
    p, _, H = m.dims("pillar")
    at, f = m.origin((p, p, H)), m.max_face_m
    pb, pc = _bands(m)
    r, seg = round(p * rng.uniform(0.3, 0.4), 4), 16
    n = max(1, math.ceil((H - pb - pc) / f - 1e-9)) + rng.choice([0, 1])
    zs = _cuts(pb, H - pc, n)
    drums = [NodeIR(id=f"drum_{k}", kind="cylinder", params={"radius": r, "height": float(zs[k + 1] - zs[k]), "segments": seg},
                    transform=Transform(translate=tuple(float(v) for v in np.array([p / 2, p / 2, (zs[k] + zs[k + 1]) / 2]) + at)),
                    material_id="concrete") for k in range(n)]
    return SceneIR(asset_name=name, materials=[_CONCRETE, _IRON], nodes=[
        *_blocks("plinth", (0, 0, 0), (p, p, pb), "concrete", f, 0, at), *drums,
        *_blocks("capital", (0, 0, H - pc), (p, p, H), "iron", f, 0, at)])


def _doorway(rng: random.Random, name: str, prompt: str, metric: Any = None) -> SceneIR:
    m = _metric(metric)
    L, D, H = m.dims("doorway")
    at, f = m.origin((L, D, H)), m.max_face_m
    bt, ct = _bands(m)
    j, dh = (L - m.door_width_m) / 2, m.door_height_m
    ct = min(ct, H - dh)
    cuts = _coursing(m)
    nodes = []
    for side, (x0, x1) in (("l", (0, j)), ("r", (L - j, L))):
        nodes += _blocks(f"base_{side}", (x0, 0, 0), (x1, D, bt), "iron", f, 0, at)
        nodes += _blocks(f"jamb_{side}", (x0, 0, bt), (x1, D, dh), "concrete", f, 0, at, zcuts=_between(cuts, bt, dh))
    if H - ct - dh > 1e-6:
        nodes += _blocks("lintel", (0, 0, dh), (L, D, H - ct), "concrete", f, 0, at, zcuts=_between(cuts, dh, H - ct))
    nodes += _blocks("cornice", (0, 0, H - ct), (L, D, H), "iron", f, 0, at)
    nodes += _relief(m, [(0, j), (L - j, L)], bt, ct, at)
    return SceneIR(asset_name=name, materials=[_CONCRETE, _IRON], nodes=nodes)


KIT_TEMPLATE_FNS = {"wall_segment": _wall, "floor_tile": _floor, "pillar": _pillar, "doorway": _doorway}
TEMPLATES: dict[str, Callable[..., SceneIR]] = {
    "crate": _crate, "barrel": _barrel, "table": _table, "lamp_post": _lamp, "shelf": _shelf,
    "wall_segment": _wall, "floor_tile": _floor, "pillar": _pillar, "doorway": _doorway}
KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("barrel", ("barrel", "keg", "drum", "cask")), ("table", ("table", "desk", "bench")),
    ("lamp_post", ("lamp", "lantern", "street light", "streetlight")), ("shelf", ("shelf", "shelves", "bookcase", "rack")),
    ("wall_segment", ("wall",)), ("floor_tile", ("floor", "tile")), ("pillar", ("pillar", "column")),
    ("doorway", ("door", "gate")), ("crate", ("crate", "box", "chest", "container")))


def pick_template(prompt: str, hint: str | None = None) -> str:
    if hint in TEMPLATES:
        return hint  # type: ignore[return-value]
    low = prompt.lower()
    return next((t for t, kws in KEYWORDS if any(re.search(rf"\b{k}", low) for k in kws)), "crate")


class TemplateAuthor:
    """Deterministic offline author: parametric prop templates with seeded variation, mechanical repairs."""

    def __init__(self, seed: int = 0) -> None:
        self.seed = seed
        self.budget: EngineBudget | None = None
        self.calls = {"propose": 0, "repair": 0}

    async def propose(self, prompt: str, *, budget: EngineBudget, n: int, context: dict[str, Any]) -> list[SceneIR]:
        self.calls["propose"] += 1
        self.budget = budget
        name = context.get("asset_name") or _slug(prompt)
        key = pick_template(prompt, context.get("template"))
        tpl = TEMPLATES[key]
        kw = {"metric": context.get("kit_metric")} if key in KIT_TEMPLATE_FNS else {}  # kit pieces build to the kit's metric
        base = int.from_bytes(hashlib.sha256(f"{prompt}|{name}|{self.seed}|{context.get('seed', 0)}".encode()).digest()[:4], "big")
        return [tpl(random.Random(base + 7919 * i), name, prompt, **kw) for i in range(n)]

    async def repair(self, scene: SceneIR, *, action: Any, located: dict[str, list[str]], manifest: str) -> IRPatch | None:
        self.calls["repair"] += 1
        flagged = lambda *keys: {n for k in keys for n in located.get(k, [])}  # noqa: E731
        ops: list[IROp] = []
        floating = flagged("defect_floating", "tier1:floating_part")
        if floating:
            ops += _reground(scene, floating)
        elif "tier1:ungrounded" in located:
            ops += _lift_all(scene)
        if "tier1:tri_budget" in located and self.budget is not None:
            ops += _shrink(scene, self.budget.max_tris)
        clip = flagged("defect_clipping", "tier1:interpenetration")
        if len(clip) >= 2:
            ops += _separate(apply_patch(scene, IRPatch(ops=ops)) if ops else scene, clip)
        return IRPatch(ops=ops) if ops else None


Fn = Callable[..., Any]


async def _call(fn: Fn, *a: Any, **kw: Any) -> Any:
    r = fn(*a, **kw)
    return await r if inspect.isawaitable(r) else r


class CallableAuthor:
    """Wraps (async or sync) callables; the harness plugs a model-driven agent in here."""

    def __init__(self, propose: Callable[..., Awaitable[list[SceneIR]] | list[SceneIR]],
                 repair: Callable[..., Awaitable[IRPatch | None] | IRPatch | None] | None = None) -> None:
        self._propose, self._repair = propose, repair
        self.calls = {"propose": 0, "repair": 0}

    async def propose(self, prompt: str, *, budget: EngineBudget, n: int, context: dict[str, Any]) -> list[SceneIR]:
        self.calls["propose"] += 1
        return list(await _call(self._propose, prompt, budget=budget, n=n, context=context))

    async def repair(self, scene: SceneIR, *, action: Any, located: dict[str, list[str]], manifest: str) -> IRPatch | None:
        self.calls["repair"] += 1
        return None if self._repair is None else await _call(self._repair, scene, action=action, located=located, manifest=manifest)


def _service_failure(exc: BaseException) -> bool:
    """A failure on the service's side or of the connection (``service_answer``), not a bug in the request. The
    session's timing is not one request's, so it is never taken for a quick rejection."""
    return service_answer.classify(exc, float("inf")) != "final"


class AgentAuthor:
    """Model-driven author on the harness: one BLOCKOUT stage-agent session per candidate / repair.

    Lazily imports ``antigravity3d.harness`` (heavy, needs the SDK and GEMINI_API_KEY); only used when not offline.
    ``propose`` opens ``n`` isolated sessions concurrently and reads each resulting IR back from its stage manager;
    a session that fails, times out or builds nothing is a recorded outcome (``propose_with_outcomes``), and when no
    session produced a scene the first failure is raised.
    ``repair`` opens a session on the current scene, hands the agent the located defects and returns the whole
    corrected scene as upserts of every changed node (an IRPatch), or None when nothing changed.
    """

    def __init__(self, *, plane: Any = None, forge: Any = None, workdir: Any = None, client: Any = None, role: str = "worker", timeout_s: float | None = None,
                 progress: Callable[[Any], None] | None = None) -> None:
        """``timeout_s=None``: governed model calls use the per-tier timeouts beside the output-budget table
        (``metered_author.AUTHOR_TIMEOUT_S``) and harness stage sessions ``STAGE_TIMEOUT_S``; a number overrides both."""
        self.plane, self.forge, self.role, self.timeout_s = plane, forge, role, timeout_s
        self.client = client
        # MeteredAuthor's progress sink (tactics.stream_call.ModelCallProgress per event); None: no events
        self.progress = progress
        self._budget: EngineBudget | None = None
        self.workdir = workdir
        self.calls = {"propose": 0, "repair": 0}
        self.usage: list[Any] = []

    def bind(self, governor: Any, budget: EngineBudget, context: dict[str, Any], *, record: Any = None) -> IRAuthor:
        """The governed one-shot author for one asset. ``record``: the run's ``tactics.cached.DesignRecord`` (answered
        requests of earlier runs in the same ``--out`` are reused without a call), or None.

        The stage-session path (:meth:`_session`) is not recorded: its result is a multi-turn tool trajectory of the
        harness, and nothing a key could hold (the harness's tools and prompts) would invalidate it when they change."""
        from google import genai
        from antigravity3d.tactics.metered_author import MeteredAuthor

        return MeteredAuthor(self.client or genai.Client(), governor, budget, context, timeout_s=self.timeout_s, record=record,
                             progress=self.progress)

    def _run_stage(self) -> Callable[..., Any]:
        from antigravity3d.harness.agents import run_stage  # lazy

        return run_stage

    async def _session(self, tag: str, asset: str, budget: EngineBudget, prompt: str, seed_scene: SceneIR | None = None) -> SceneIR:
        import tempfile
        import uuid
        from pathlib import Path

        from antigravity3d.harness import session as S

        run_stage = self._run_stage()
        sid = f"{tag}-{uuid.uuid4().hex[:8]}"
        if self.workdir is not None:
            Path(self.workdir).mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.workdir, prefix=f"{sid}-") as directory:
            S.open_session(None, sid, Path(directory), asset, budget, plane=self.plane, forge=self.forge, brief=getattr(self, "_brief", None))
            try:
                stage = S.DEFAULT_REGISTRY.resource(sid, S.R_STAGE)
                if seed_scene is not None:
                    stage.apply_ir(seed_scene)
                run = await run_stage("BLOCKOUT", sid, prompt, role=self.role,
                                       timeout_s=STAGE_TIMEOUT_S if self.timeout_s is None else self.timeout_s)
                self.usage.append(run.usage)
                return stage.base_ir()
            finally:
                S.DEFAULT_REGISTRY.drop(sid)

    async def propose_with_outcomes(self, prompt: str, *, budget: EngineBudget, n: int, context: dict[str, Any]) -> Any:
        """One BLOCKOUT stage session per requested candidate, each with its recorded outcome: the contract of
        ``MeteredAuthor.propose_with_outcomes``, so no requested candidate ends unnamed. Raises only when no session
        produced a scene: the first failure, naming what happened to every candidate when several were requested.
        (Failed sessions used to be dropped, so a caller asking for one candidate got an empty list and no cause.)"""
        from antigravity3d.tactics.metered_author import CandidateOutcome, ProposeResult  # lazy, like the harness

        self.calls["propose"] += 1
        self._budget = budget
        self._art = context.get("art_direction") or ""  # art direction author brief; repeated in repairs
        self._brief = prompt  # the user's request: the stage sessions' visual QA judge is given it (open_session(brief=))
        asset = context.get("asset_name") or _slug(prompt)
        emph = ("overall proportions", "construction detail", "symmetry", "clean minimal forms")
        from antigravity3d.kernel.kit_metric import context_brief

        brief = context_brief(context)
        task = (f"Build a blockout of: {prompt}. The asset name is {asset}; budget {budget.max_tris} tris, at most {budget.max_draw_calls} materials. "
                "Z-up metres, standing on the ground (min z = 0), parts touching, no interpenetration. "
                + (f"{brief} " if brief else "") + "Use apply_scene_ir (materials included) then stop. "
                + (f"{self._art.rstrip('.')}. " if self._art else "") + "Emphasis: ")
        timeout = STAGE_TIMEOUT_S if self.timeout_s is None else self.timeout_s

        async def candidate(i: int) -> tuple[SceneIR | None, Any, Exception | None]:
            try:
                scene = await self._session(f"cand{i}", asset, budget, task + emph[i % len(emph)])
            except Exception as exc:  # noqa: BLE001 - recorded per candidate; raised below if nothing was produced
                timed_out = isinstance(exc, TimeoutError)
                if timed_out and not str(exc):  # asyncio's TimeoutError carries no text
                    exc.args = (f"stage session for candidate #{i} gave no result within {timeout:.0f} s",)
                detail = " ".join(str(exc).split())
                if not timed_out and _service_failure(exc):  # the SDK's own API retries ran out: the service gave no answer
                    return None, CandidateOutcome(i, "unavailable", f"stage session: {service_answer.describe(exc)}"), exc
                return None, CandidateOutcome(i, "timed_out" if timed_out else "error", detail if timed_out else f"{type(exc).__name__}: {detail}"), exc
            if not scene.nodes:
                empty = ValueError(f"stage session for candidate #{i} ended with no nodes in the scene")
                return None, CandidateOutcome(i, "invalid", str(empty)), empty
            return scene, CandidateOutcome(i, "produced"), None

        # A cancellation of this task cancels every session (gather) and propagates, as does a session's own
        # non-Exception BaseException.
        results = await asyncio.gather(*(candidate(i) for i in range(n)), return_exceptions=True)
        for r in results:
            if isinstance(r, BaseException):
                raise r
        scenes = [scene for scene, _, _ in results if scene is not None]
        outcomes = [outcome for _, outcome, _ in results]
        if scenes or not results:
            return ProposeResult(scenes, outcomes)
        first = results[0][2]
        if len(outcomes) > 1:  # keep the first error's type; name what happened to every candidate
            first.args = (f"{first.args[0] if first.args else first}; no stage session of {len(outcomes)} produced a scene: "
                          f"{ProposeResult([], outcomes).summary()}",)
        raise first

    async def propose(self, prompt: str, *, budget: EngineBudget, n: int, context: dict[str, Any]) -> list[SceneIR]:
        return (await self.propose_with_outcomes(prompt, budget=budget, n=n, context=context)).scenes

    async def repair(self, scene: SceneIR, *, action: Any, located: dict[str, list[str]], manifest: str) -> IRPatch | None:
        self.calls["repair"] += 1
        task = (f"Repair this blockout. QA verdict: {getattr(action, 'kind', action)}; reasons: {getattr(action, 'reasons', [])}. "
                f"Located problems (defect -> node ids): {located}. Current manifest:\n{manifest}\nFix only what is flagged with apply_scene_ir."
                + (f"\n{self._art}" if getattr(self, "_art", "") else ""))
        if self._budget is None:
            raise ValueError("repair requires the target budget of a prior proposal")
        try:
            new = await self._session("repair", scene.asset_name, self._budget, task, seed_scene=scene)
        except Exception as exc:
            if isinstance(exc, TimeoutError) or not _service_failure(exc):
                raise
            from antigravity3d.tactics.metered_author import AuthorUnavailable  # lazy, like the harness

            code, status = service_answer.code_status(exc)
            # The stage session is not metered here (no reservation); the Antigravity SDK's own API retries ran first.
            raise AuthorUnavailable(f"{self.role} repair (stage session): no answer from the service after the SDK's "
                                    f"retries: {service_answer.describe(exc)}", attempts=[], nothing_booked=False, code=code,
                                    status=status, model=str(getattr(exc, "model", "") or "stage-session"), role=self.role,
                                    operation="repair", finish_reason=status or type(exc).__name__, usage=None,
                                    max_output_tokens=0) from exc
        old = {n.id: n for n in scene.nodes}
        ops: list[IROp] = [UpsertNode(node=n) for n in new.nodes if old.get(n.id) != n]
        ops += [UpsertMaterial(material=m) for m in new.materials if m not in scene.materials]
        return IRPatch(ops=ops) if ops else None


def best_author(*, plane: Any = None, forge: Any = None, workdir: Any = None, client: Any = None, live: bool = True) -> tuple[IRAuthor, str]:
    """AgentAuthor when the harness imports (and ``live``), else TemplateAuthor; with a note saying which and why."""
    if live:
        try:
            a = AgentAuthor(plane=plane, forge=forge, workdir=workdir, client=client)
            a._run_stage()
            return a, "author: Gemini JSON IR with per-route token admission"
        except Exception as e:  # noqa: BLE001 - harness optional
            return TemplateAuthor(), f"harness unavailable ({type(e).__name__}: {e}); using TemplateAuthor"
    return TemplateAuthor(), "TemplateAuthor"
