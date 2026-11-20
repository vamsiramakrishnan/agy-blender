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
    IRPatch, IROp, MaterialIR, NodeIR, SceneIR, SetParam, SetTransform, Transform, UpsertMaterial, UpsertNode, apply_patch,
)
from antigravity3d.kernel.compile_ir import primitive_mesh
from antigravity3d.state import EngineBudget

GROUND_TOL = 0.01
CURVED = ("cylinder", "cone", "sphere", "capsule")
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


def _half(n: NodeIR) -> np.ndarray | None:
    p = n.params
    if n.kind == "box":
        return np.asarray(p["size"], float) / 2
    if n.kind in ("cylinder", "cone"):
        return np.array([p["radius"], p["radius"], float(p["height"]) / 2])
    if n.kind == "sphere":
        return np.full(3, float(p["radius"]))
    if n.kind == "capsule":
        return np.array([p["radius"], p["radius"], float(p["height"]) / 2 + float(p["radius"])])
    return None


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
        h = _half(n)
        if h is None:
            continue
        corners = np.array([[sx, sy, sz] for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]) * h
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
    return sum(len(primitive_mesh(n)[1]) for n in scene.nodes if n.kind in ("box", *CURVED))


# --------------------------------------------------------------------------- mechanical repairs


def _reground(scene: SceneIR, flagged: set[str]) -> list[IROp]:
    """Drop each flagged node onto the nearest support below it (or the ground); lift sunk nodes."""
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
        step: list[IROp] = []
        for n in cur.nodes:
            if n.kind not in CURVED:
                continue
            for name, floor, default in (("segments", 8, 24 if n.kind != "capsule" else 16), ("rings", 6, 12)):
                if name == "rings" and n.kind != "sphere":
                    continue
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


def _box(i: str, size: tuple[float, float, float], at: tuple[float, float, float], mat: str, parent: str | None = None, **kw: Any) -> NodeIR:
    return NodeIR(id=i, kind="box", params={"size": tuple(round(s, 4) for s in size)},
                  transform=Transform(translate=tuple(round(a, 4) for a in at)), material_id=mat, parent=parent, **kw)


def _cyl(i: str, r: float, h: float, at: tuple[float, float, float], mat: str, seg: int, kind: str = "cylinder") -> NodeIR:
    return NodeIR(id=i, kind=kind, params={"radius": round(r, 4), "height": round(h, 4), "segments": seg},
                  transform=Transform(translate=tuple(round(a, 4) for a in at)), material_id=mat)


_PRIMARY = (("wood", (0.52, 0.36, 0.2), 0.0, 0.8), ("metal", (0.55, 0.56, 0.6), 1.0, 0.4),
            ("stone", (0.5, 0.5, 0.48), 0.0, 0.9), ("plastic", (0.2, 0.45, 0.75), 0.0, 0.5))
_IRON = MaterialIR(id="iron", base_color=(0.24, 0.24, 0.27), metallic=1.0, roughness=0.45)


def _primary(prompt: str) -> MaterialIR:
    low = prompt.lower()
    key = next((k for k, *_ in _PRIMARY if k in low or (k == "metal" and re.search(r"steel|alumin|brass", low))), "wood")
    _, c, m, r = next(p for p in _PRIMARY if p[0] == key)
    return MaterialIR(id=key, base_color=c, metallic=m, roughness=r)


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
    lamp = MaterialIR(id="glass", base_color=(1.0, 0.92, 0.7), roughness=0.2)
    nodes = [_cyl("base", 0.16, 0.12, (0, 0, 0.06), "iron", seg), _cyl("pole", 0.04, h, (0, 0, 0.12 + h / 2), "iron", seg),
             NodeIR(id="head", kind="sphere", params={"radius": round(br, 3), "segments": seg, "rings": 8},
                    transform=Transform(translate=(0, 0, round(0.12 + h + br, 4))), material_id="glass")]
    return SceneIR(asset_name=name, nodes=nodes, materials=[_IRON, lamp])


def _shelf(rng: random.Random, name: str, prompt: str) -> SceneIR:
    pm = _primary(prompt)
    w, d, H, n = rng.uniform(0.8, 1.2), rng.uniform(0.25, 0.4), rng.uniform(1.4, 2.0), rng.choice([3, 4, 5])
    side = 0.04
    nodes = [_box(f"side_{s}", (side, d, H), (sx * (w / 2 - side / 2), 0, H / 2), pm.id) for s, sx in (("l", -1), ("r", 1))]
    for k, z in enumerate(np.linspace(0.015, H - 0.015, n)):
        nodes.append(_box(f"board_{k}", (w - 2 * side, d, 0.03), (0, 0, float(z)), pm.id))
    return SceneIR(asset_name=name, nodes=nodes, materials=[pm])


def _wall(rng: random.Random, name: str, prompt: str) -> SceneIR:
    pm = MaterialIR(id="concrete", base_color=(0.5, 0.5, 0.48), roughness=0.9)
    w, h, t = 2.0, rng.uniform(2.8, 3.2), 0.2
    return SceneIR(asset_name=name, materials=[pm, _IRON], nodes=[
        _box("slab", (w, t, h), (0, 0, h / 2), pm.id), _box("base_trim", (w, 0.04, 0.2), (0, -t / 2 - 0.02, 0.1), "iron"),
        _box("cornice", (w, 0.04, 0.15), (0, -t / 2 - 0.02, h - 0.075), "iron")])


def _floor(rng: random.Random, name: str, prompt: str) -> SceneIR:
    pm = MaterialIR(id="concrete", base_color=(0.5, 0.5, 0.48), roughness=0.9)
    return SceneIR(asset_name=name, materials=[pm, _IRON], nodes=[
        _box("tile", (2.0, 2.0, 0.1), (0, 0, 0.05), pm.id), _box("inlay", (1.6, 0.06, 0.01), (0, 0, 0.105), "iron")])


def _pillar(rng: random.Random, name: str, prompt: str) -> SceneIR:
    pm = MaterialIR(id="concrete", base_color=(0.5, 0.5, 0.48), roughness=0.9)
    h, seg = rng.uniform(2.8, 3.2), 16
    return SceneIR(asset_name=name, materials=[pm, _IRON], nodes=[
        _cyl("plinth", 0.35, 0.2, (0, 0, 0.1), pm.id, seg), _cyl("shaft", 0.25, h - 0.4, (0, 0, h / 2), pm.id, seg),
        _cyl("capital", 0.35, 0.2, (0, 0, h - 0.1), "iron", seg)])


def _doorway(rng: random.Random, name: str, prompt: str) -> SceneIR:
    pm = MaterialIR(id="concrete", base_color=(0.5, 0.5, 0.48), roughness=0.9)
    w, h = 2.0, rng.uniform(2.6, 3.0)
    return SceneIR(asset_name=name, materials=[pm, _IRON], nodes=[
        _box("jamb_l", (0.4, 0.2, h), (-w / 2 + 0.2, 0, h / 2), pm.id), _box("jamb_r", (0.4, 0.2, h), (w / 2 - 0.2, 0, h / 2), pm.id),
        _box("lintel", (w, 0.2, 0.4), (0, 0, h + 0.2), pm.id), _box("sill", (w - 0.8, 0.24, 0.03), (0, 0, 0.015), "iron")])


TEMPLATES: dict[str, Callable[[random.Random, str, str], SceneIR]] = {
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
        tpl = TEMPLATES[pick_template(prompt, context.get("template"))]
        base = int.from_bytes(hashlib.sha256(f"{prompt}|{name}|{self.seed}|{context.get('seed', 0)}".encode()).digest()[:4], "big")
        return [tpl(random.Random(base + 7919 * i), name, prompt) for i in range(n)]

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


class AgentAuthor:
    """Model-driven author on the harness: one BLOCKOUT stage-agent session per candidate / repair.

    Lazily imports ``antigravity3d.harness`` (heavy, needs the SDK and GEMINI_API_KEY); only used when not offline.
    ``propose`` opens ``n`` isolated sessions concurrently and reads each resulting IR back from its stage manager;
    a session that fails, times out or builds nothing is a recorded outcome (``propose_with_outcomes``), and when no
    session produced a scene the first failure is raised.
    ``repair`` opens a session on the current scene, hands the agent the located defects and returns the whole
    corrected scene as upserts of every changed node (an IRPatch), or None when nothing changed.
    """

    def __init__(self, *, plane: Any = None, forge: Any = None, workdir: Any = None, client: Any = None, role: str = "worker", timeout_s: float | None = None) -> None:
        """``timeout_s=None``: governed model calls use the per-tier timeouts beside the output-budget table
        (``metered_author.AUTHOR_TIMEOUT_S``) and harness stage sessions ``STAGE_TIMEOUT_S``; a number overrides both."""
        self.plane, self.forge, self.role, self.timeout_s = plane, forge, role, timeout_s
        self.client = client
        self._budget: EngineBudget | None = None
        self.workdir = workdir
        self.calls = {"propose": 0, "repair": 0}
        self.usage: list[Any] = []

    def bind(self, governor: Any, budget: EngineBudget, context: dict[str, Any]) -> IRAuthor:
        from google import genai
        from antigravity3d.tactics.metered_author import MeteredAuthor

        return MeteredAuthor(self.client or genai.Client(), governor, budget, context, timeout_s=self.timeout_s)

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
            S.open_session(None, sid, Path(directory), asset, budget, plane=self.plane, forge=self.forge)
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
        asset = context.get("asset_name") or _slug(prompt)
        emph = ("overall proportions", "construction detail", "symmetry", "clean minimal forms")
        task = (f"Build a blockout of: {prompt}. The asset name is {asset}; budget {budget.max_tris} tris, at most {budget.max_draw_calls} materials. "
                "Z-up metres, standing on the ground (min z = 0), parts touching, no interpenetration. Use apply_scene_ir (materials included) then stop. Emphasis: ")
        timeout = STAGE_TIMEOUT_S if self.timeout_s is None else self.timeout_s

        async def candidate(i: int) -> tuple[SceneIR | None, Any, Exception | None]:
            try:
                scene = await self._session(f"cand{i}", asset, budget, task + emph[i % len(emph)])
            except Exception as exc:  # noqa: BLE001 - recorded per candidate; raised below if nothing was produced
                timed_out = isinstance(exc, TimeoutError)
                if timed_out and not str(exc):  # asyncio's TimeoutError carries no text
                    exc.args = (f"stage session for candidate #{i} gave no result within {timeout:.0f} s",)
                detail = " ".join(str(exc).split())
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
                f"Located problems (defect -> node ids): {located}. Current manifest:\n{manifest}\nFix only what is flagged with apply_scene_ir.")
        if self._budget is None:
            raise ValueError("repair requires the target budget of a prior proposal")
        new = await self._session("repair", scene.asset_name, self._budget, task, seed_scene=scene)
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
