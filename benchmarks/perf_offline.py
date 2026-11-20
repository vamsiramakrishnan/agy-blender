#!/usr/bin/env python3
"""Offline performance benchmarks for antigravity-3d (deterministic, local parts only).

    python benchmarks/perf_offline.py --out benchmarks/results/offline.json [--quick] [--only NAME ...] [--list]

No network and no model/API calls: provider keys are removed from the environment, and every decision,
image and authoring call goes to the package's own fakes (FakeDecisionsClient, FakeImageBackend,
TemplateAuthor). Blender and Godot are never started; the Blender pool is only used in its built-in fake
daemon mode, and only to time IPC. See benchmarks/README.md for what each benchmark includes and excludes.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------------------------------------
# Environment: this block must run before numpy (or anything that imports it) is imported.
# ---------------------------------------------------------------------------------------------------------
import os

THREAD_ENV = {"OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}
for _k, _v in THREAD_ENV.items():
    os.environ[_k] = _v
for _k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_APPLICATION_CREDENTIALS",
           "AGY3D_POOL_SOCKET", "BLENDER_BIN"):
    os.environ.pop(_k, None)
os.environ["AGY3D_OFFLINE"] = "1"

import argparse
import asyncio
import contextlib
import gc
import hashlib
import json
import logging
import math
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[1]
SIZES_FULL = [5, 20, 50, 200]
SIZES_QUICK = [5, 50]
PROMPTS = {  # the five TemplateAuthor prop prompts used by the pipeline benchmarks
    "crate": "wooden crate with iron brackets",
    "barrel": "rusty metal barrel",
    "table": "oak dining table",
    "lamp_post": "iron street lamp post",
    "shelf": "wooden storage shelf",
}


# ---------------------------------------------------------------------------------------------------------
# Lazy heavy imports (so --list is instant)
# ---------------------------------------------------------------------------------------------------------
def _load() -> None:
    """Import numpy and the package into module globals. Idempotent."""
    g = globals()
    if "np" in g:
        return
    import numpy as np_

    g["np"] = np_
    from antigravity3d import ir as ir_
    from antigravity3d.ir import (IRPatch, MaterialIR, NodeIR, SceneIR, SetTransform, Transform)
    from antigravity3d.kernel import cache as cache_, compile_ir, fast_ops, package as package_, raster, settle as settle_
    from antigravity3d.kernel import som, spatial, usd_stage, validator
    from antigravity3d.kernel.types import MeshData
    from antigravity3d.state import EngineBudget

    g.update(ir=ir_, IRPatch=IRPatch, MaterialIR=MaterialIR, NodeIR=NodeIR, SceneIR=SceneIR, SetTransform=SetTransform,
             Transform=Transform, kcache=cache_, compile_ir=compile_ir, fast_ops=fast_ops, kpackage=package_,
             raster=raster, ksettle=settle_, som=som, spatial=spatial, usd_stage=usd_stage, validator=validator,
             MeshData=MeshData, EngineBudget=EngineBudget)


# ---------------------------------------------------------------------------------------------------------
# Measurement core
# ---------------------------------------------------------------------------------------------------------
def _r(x: float) -> float:
    return round(float(x), 4)


def summarize(samples: list[float]) -> dict[str, Any]:
    """Stats over per-iteration wall times in ms. p95 is null below 20 samples (linear interpolation otherwise)."""
    n = len(samples)
    p95 = float(np.percentile(samples, 95)) if n >= 20 else None
    return {"n": n, "median_ms": _r(statistics.median(samples)), "p95_ms": None if p95 is None else _r(p95),
            "min_ms": _r(min(samples)), "max_ms": _r(max(samples)), "mean_ms": _r(statistics.fmean(samples))}


@dataclass
class Case:
    params: dict[str, Any]
    samples_ms: list[float]
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"params": self.params, **summarize(self.samples_ms), "extra": self.extra}


class Ctx:
    def __init__(self, quick: bool, tmp: Path) -> None:
        self.quick, self.tmp = quick, tmp
        self.cap_s = 1.0 if quick else 3.0           # per-case timed-loop cap (checked once min samples are in)
        self.min_slow = 3 if quick else 5            # min samples for operations >= 100 ms
        self.sizes = SIZES_QUICK if quick else SIZES_FULL
        self._memo: dict[str, Any] = {}
        self._n = 0

    def dir(self, name: str = "d") -> Path:
        self._n += 1
        p = self.tmp / f"{name}_{self._n}"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def once(self, key: str, fn: Callable[[], Any]) -> Any:
        if key not in self._memo:
            self._memo[key] = fn()
        return self._memo[key]


def _classify_and_plan(warm: list[float], ctx: Ctx) -> tuple[int, bool]:
    slow = min(warm) >= 100.0
    return (ctx.min_slow if slow else 20), slow


def timeit(fn: Callable[..., Any], ctx: Ctx, *, params: dict[str, Any], setup: Callable[[], Any] | None = None,
           teardown: Callable[[Any, Any], None] | None = None, collect: Callable[[Any], dict[str, float]] | None = None,
           extra: dict[str, Any] | None = None, warmup: int = 2, max_n: int = 2000) -> Case:
    """Time ``fn`` (called as fn(state) if ``setup`` is given, else fn()).

    Only the call to ``fn`` is inside the timed region (perf_counter_ns). ``setup`` runs untimed before every
    iteration to build fresh state, ``teardown(state, result)`` runs untimed after it (use it to undo state and to
    assert the result was the one intended). ``collect(result)`` may return named sub-timings in ms.
    Warm-up: ``warmup`` untimed iterations, then one gc.collect(), then GC stays enabled during timing.
    Sample count: >= 20 when the fastest warm-up iteration is < 100 ms, else >= 5 (3 in quick mode); the loop
    stops once the minimum is reached and the per-case cap has elapsed (setup/teardown time counts toward the cap).
    """
    def one() -> tuple[float, Any]:
        state = setup() if setup else None
        t0 = time.perf_counter_ns()
        res = fn(state) if setup else fn()
        t1 = time.perf_counter_ns()
        if teardown:
            teardown(state, res)
        return (t1 - t0) / 1e6, res

    warm = [one()[0] for _ in range(max(2, warmup))]
    min_n, slow = _classify_and_plan(warm, ctx)
    gc.collect()
    samples: list[float] = []
    parts: dict[str, list[float]] = {}
    start, cpu0 = time.perf_counter(), time.process_time()
    while True:
        d, res = one()
        samples.append(d)
        if collect:
            for k, v in collect(res).items():
                parts.setdefault(k, []).append(v)
        n = len(samples)
        if n >= max_n or (n >= min_n and time.perf_counter() - start >= ctx.cap_s):
            break
    wall, cpu = time.perf_counter() - start, time.process_time() - cpu0
    ex = dict(extra or {})
    ex["warmup_iters"] = len(warm)
    ex["process_cpu_over_wall"] = round(cpu / wall, 2) if wall > 0 else None  # >1 means threads outside our control
    if parts:
        ex["parts_median_ms"] = {k: _r(statistics.median(v)) for k, v in parts.items()}
    return Case(params, samples, ex)


async def atimeit(fn: Callable[..., Any], ctx: Ctx, *, params: dict[str, Any], setup: Callable[[], Any] | None = None,
                  teardown: Callable[[Any, Any], Any] | None = None, extra: dict[str, Any] | None = None,
                  warmup: int = 2, max_n: int = 2000) -> Case:
    """Same contract as :func:`timeit` for coroutine functions (all three callables may be async)."""
    async def call(f: Callable[..., Any], *a: Any) -> Any:
        r = f(*a)
        return await r if asyncio.iscoroutine(r) else r

    async def one() -> tuple[float, Any]:
        state = (await call(setup)) if setup else None
        t0 = time.perf_counter_ns()
        res = await call(fn, state) if setup else await call(fn)
        t1 = time.perf_counter_ns()
        if teardown:
            await call(teardown, state, res)
        return (t1 - t0) / 1e6, res

    warm = [(await one())[0] for _ in range(max(2, warmup))]
    min_n, _ = _classify_and_plan(warm, ctx)
    gc.collect()
    samples: list[float] = []
    start = time.perf_counter()
    while True:
        samples.append((await one())[0])
        n = len(samples)
        if n >= max_n or (n >= min_n and time.perf_counter() - start >= ctx.cap_s):
            break
    ex = dict(extra or {})
    ex["warmup_iters"] = len(warm)
    return Case(params, samples, ex)


# ---------------------------------------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------------------------------------
@dataclass
class Bench:
    name: str
    includes: str
    excludes: str
    fn: Callable[[Ctx], list[Case]]


REGISTRY: dict[str, Bench] = {}


def bench(name: str, includes: str, excludes: str):
    def deco(fn: Callable[[Ctx], list[Case]]):
        if name in REGISTRY:
            raise ValueError(f"duplicate benchmark {name}")
        REGISTRY[name] = Bench(name, includes, excludes, fn)
        return fn
    return deco


# ---------------------------------------------------------------------------------------------------------
# A. Scene and mesh generators
# ---------------------------------------------------------------------------------------------------------
BENCH_BUDGET_TRIS = 1_000_000  # scene-size benchmarks must not trip the 15k default tri budget (see README)


def scene_budget():
    return EngineBudget("unreal_5", max_tris=BENCH_BUDGET_TRIS, max_draw_calls=4)


def make_scene(n: int, name: str = "Bench"):
    """n primitives (box/cylinder/sphere in rotation, 3 materials) on a 2 m grid, each resting on z = 0.

    Footprints are <= 0.8 m and cells are 2 m apart, so nothing touches or interpenetrates and Tier-1 passes.
    """
    mats = [MaterialIR(id=f"m{i}", base_color=c, roughness=r) for i, (c, r) in
            enumerate([((0.6, 0.4, 0.2), 0.7), ((0.5, 0.5, 0.55), 0.4), ((0.2, 0.5, 0.3), 0.9)])]
    cols = max(1, math.ceil(math.sqrt(n)))
    nodes = []
    for i in range(n):
        r, c = divmod(i, cols)
        x, y = (c - (cols - 1) / 2) * 2.0, (r - (cols - 1) / 2) * 2.0
        kind = ("box", "cylinder", "sphere")[i % 3]
        if kind == "box":
            sx, sy, sz = 0.4 + 0.08 * (i % 5), 0.3 + 0.07 * (i % 4), 0.4 + 0.1 * (i % 6)
            params, z = {"size": (sx, sy, sz)}, sz / 2
        elif kind == "cylinder":
            rad, h = 0.2 + 0.04 * (i % 4), 0.5 + 0.1 * (i % 5)
            params, z = {"radius": rad, "height": h, "segments": 16}, h / 2
        else:
            rad = 0.25 + 0.03 * (i % 5)
            params, z = {"radius": rad, "segments": 16, "rings": 8}, rad
        nodes.append(NodeIR(id=f"n{i:03d}", kind=kind, params=params, material_id=f"m{i % 3}",
                            transform=Transform(translate=(x, y, z))))
    return SceneIR(asset_name=name, nodes=nodes, materials=mats)


def scene_tris(scene) -> int:
    return int(sum(len(compile_ir.primitive_mesh(n)[1]) for n in scene.nodes))


def edit_op(scene) -> dict[str, Any]:
    """One-node SetTransform: shift the middle node 0.1 m along x (stays inside its 2 m cell)."""
    node = scene.nodes[len(scene.nodes) // 2]
    t = node.transform
    new = {"translate": [t.translate[0] + 0.1, t.translate[1], t.translate[2]],
           "rotate_euler_deg": list(t.rotate_euler_deg), "scale": list(t.scale)}
    return {"op": "set_transform", "node_id": node.id, "transform": new}


def edit_json(scene) -> str:
    return json.dumps({"ops": [edit_op(scene)]})


def edit_patch(scene):
    return IRPatch.model_validate({"ops": [edit_op(scene)]})


def build_stage(path: Path, scene, *, commit: bool = True):
    sm = usd_stage.StageManager.create(path, scene.asset_name)
    sm.apply_ir(scene)
    if commit:
        sm.commit_all()  # base scene baked into the root layer; edits then stack on a clean base
    return sm


def as_mesh(node_id: str, pts, tris, mat: str | None = "m0", collider: bool = True):
    return MeshData(node_id, f"/{node_id}", np.asarray(pts, np.float64), np.asarray(tris, np.int32), mat, collider)


def sphere_set(target_tris: int, k: int = 12) -> list:
    """k spheres on a grid; segment count picked so the total triangle count is closest to the target."""
    def build(s: int):
        out = []
        for i in range(k):
            node = NodeIR(id=f"p{i}", kind="sphere", params={"radius": 0.4, "segments": s, "rings": max(3, s // 2)})
            pts, tri = compile_ir.primitive_mesh(node)
            off = np.array([(i % 4) * 1.0, (i // 4) * 1.0, 0.4])
            out.append(as_mesh(f"p{i}", np.asarray(pts, np.float64) + off, tri, f"m{i % 3}"))
        return out
    best = min(range(4, 120, 2), key=lambda s: abs(sum(len(m.triangles) for m in build(s)) - target_tris))
    return build(best)


def uv_sphere_mesh(target_tris: int, name: str = "ball"):
    """UV sphere (radius 0.5 m, resting on z=0) with the triangle count closest to the target."""
    import trimesh

    def mk(b: int):
        return trimesh.creation.uv_sphere(radius=0.5, count=[2 * b, b])
    b = min(range(3, 120), key=lambda b: abs(len(mk(b).faces) - target_tris))
    m = mk(b)
    return as_mesh(name, m.vertices + np.array([0, 0, 0.5]), m.faces, "m0")


def u_shape_mesh(target_tris: int, name: str = "ushape"):
    """Concave U-shaped prism built from a voxel grid (welded, closed, outward winding)."""
    faces_dir = {  # outward-facing quads, CCW seen from outside; (dx, dy, dz) -> corner offsets
        (1, 0, 0): [(1, 0, 0), (1, 1, 0), (1, 1, 1), (1, 0, 1)], (-1, 0, 0): [(0, 0, 0), (0, 0, 1), (0, 1, 1), (0, 1, 0)],
        (0, 1, 0): [(0, 1, 0), (0, 1, 1), (1, 1, 1), (1, 1, 0)], (0, -1, 0): [(0, 0, 0), (1, 0, 0), (1, 0, 1), (0, 0, 1)],
        (0, 0, 1): [(0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)], (0, 0, -1): [(0, 0, 0), (0, 1, 0), (1, 1, 0), (1, 0, 0)]}

    def mk(nz: int, nx: int = 12, ny: int = 8):
        filled = {(i, j, k) for i in range(nx) for j in range(ny) for k in range(nz) if j < 3 or i < 3 or i >= nx - 3}
        vid: dict[tuple[int, int, int], int] = {}
        pts: list[tuple[int, int, int]] = []
        tris: list[tuple[int, int, int]] = []
        for (i, j, k) in sorted(filled):
            for d, corners in faces_dir.items():
                if (i + d[0], j + d[1], k + d[2]) in filled:
                    continue
                q = []
                for (a, b, c) in corners:
                    p = (i + a, j + b, k + c)
                    if p not in vid:
                        vid[p] = len(pts)
                        pts.append(p)
                    q.append(vid[p])
                tris += [(q[0], q[1], q[2]), (q[0], q[2], q[3])]
        return np.asarray(pts, float) * 0.05, np.asarray(tris, np.int32)
    nz = min(range(1, 40), key=lambda z: abs(len(mk(z)[1]) - target_tris))
    pts, tris = mk(nz)
    return as_mesh(name, pts, tris, "m0")


# ---------------------------------------------------------------------------------------------------------
# Harness session (model-facing tools driven with a stub ToolContext, as tests/test_harness_tools.py does)
# ---------------------------------------------------------------------------------------------------------
_sess_counter = 0


def open_harness(ctx: Ctx, runner: asyncio.Runner, scene, budget, *, commit_base: bool = True, client=None):
    """Session with the scene applied through the real apply_scene_ir tool. Returns a namespace; call .close()."""
    global _sess_counter
    from google.antigravity.tools.tool_context import ToolContext

    from antigravity3d.decisions.engine import DecisionControlPlane
    from antigravity3d.decisions.fakes import FakeDecisionsClient
    from antigravity3d.genmedia.nano_banana import FakeImageBackend, ImageForge
    from antigravity3d.harness import session as S
    from antigravity3d.harness import tools as T
    from antigravity3d.state import SceneSessionRegistry

    _sess_counter += 1
    sid = f"bench{_sess_counter}"
    reg = SceneSessionRegistry()
    client = client or FakeDecisionsClient()
    S.open_session(reg, sid, ctx.dir("sess") / sid, scene.asset_name, budget,
                   plane=DecisionControlPlane(client), forge=ImageForge(FakeImageBackend()), pool=None)
    conv = f"conv-{sid}-" + "0" * 32
    S.bind_conversation(conv, sid, reg)
    S.set_toolset(reg, sid, "BLOCKOUT")
    reg.get(sid).active_stage = T.TOOLSET_STAGE["BLOCKOUT"]
    tctx = ToolContext(SimpleNamespace(conversation_id=conv))
    env = SimpleNamespace(reg=reg, sid=sid, ctx=tctx, client=client, T=T, S=S, runner=runner,
                          sess=lambda: S.session(reg, sid))

    def close() -> None:
        S.unbind_conversation(conv)
        reg.drop(sid)
    env.close = close
    r = runner.run(T.apply_scene_ir(scene.model_dump_json(), tctx))
    if not r.startswith("OK"):
        raise RuntimeError(f"initial scene rejected: {r[:160]}")
    if commit_base:
        s = env.sess()
        s.stage.commit_all()
        s.sync_tx()
    return env


def _expect(result: str, prefix: str, what: str) -> None:
    if not result.startswith(prefix):
        raise RuntimeError(f"{what}: expected {prefix!r}, got {result[:140]!r}")


# ---------------------------------------------------------------------------------------------------------
# B. Edit loop
# ---------------------------------------------------------------------------------------------------------
def _scene_params(scene) -> dict[str, Any]:
    return {"nodes": len(scene.nodes), "tris": scene_tris(scene), "materials": len(scene.materials)}


@bench("kernel.edit.patch",
       "StageManager.apply_patch of a one-node SetTransform: base_ir read, patch apply + validation, USD authoring into a new transaction layer.",
       "Building the stage, JSON parsing, mesh extraction, Tier-1 QA, and the rollback that restores the base (untimed teardown).")
def b_edit_patch(ctx: Ctx) -> list[Case]:
    cases = []
    for n in ctx.sizes:
        scene = make_scene(n)
        sm = build_stage(ctx.dir("st") / "s.usda", scene)
        patch = edit_patch(scene)
        cases.append(timeit(lambda: sm.apply_patch(patch), ctx, params=_scene_params(scene),
                            teardown=lambda _s, _r: sm.rollback(discard=True)))
    return cases


@bench("kernel.edit.patch_qa",
       "apply_patch (one-node SetTransform) + StageManager.extract_meshes + tier1_qa, back to back.",
       "JSON parsing, harness state/QA persistence, rollback of the edit (untimed).")
def b_edit_patch_qa(ctx: Ctx) -> list[Case]:
    cases = []
    budget = scene_budget()
    for n in ctx.sizes:
        scene = make_scene(n)
        sm = build_stage(ctx.dir("st") / "s.usda", scene)
        patch = edit_patch(scene)

        def fn() -> dict[str, Any]:
            t0 = time.perf_counter_ns()
            sm.apply_patch(patch)
            t1 = time.perf_counter_ns()
            meshes = sm.extract_meshes()
            t2 = time.perf_counter_ns()
            rep = fast_ops.tier1_qa(meshes, budget)
            t3 = time.perf_counter_ns()
            return {"ok": rep.ok, "patch": (t1 - t0) / 1e6, "extract_meshes": (t2 - t1) / 1e6, "tier1_qa": (t3 - t2) / 1e6}

        def td(_s: Any, r: dict[str, Any]) -> None:
            sm.rollback(discard=True)
            if not r["ok"]:
                raise RuntimeError("Tier-1 rejected the benchmark edit; the timing would not be an accepted edit")
        cases.append(timeit(fn, ctx, params=_scene_params(scene), teardown=td,
                            collect=lambda r: {k: v for k, v in r.items() if k != "ok"}))
    return cases


@bench("harness.edit.apply_scene_ir",
       "T.apply_scene_ir end to end for an accepted one-node SetTransform: JSON parse, transaction, extract_meshes + Tier-1 (in a worker thread), state/QA persistence to disk, session lock, event-loop hop.",
       "Session open, initial scene load, the undo that restores the base (untimed), MCP framing, any model call.")
def b_harness_apply(ctx: Ctx) -> list[Case]:
    cases = []
    with asyncio.Runner() as runner:
        for n in ctx.sizes:
            scene = make_scene(n)
            env = open_harness(ctx, runner, scene, scene_budget())
            ej = edit_json(scene)
            T = env.T

            def td(_s: Any, r: str) -> None:
                _expect(r, "OK", "apply_scene_ir")
                _expect(runner.run(T.undo_last_edit(env.ctx)), "UNDONE", "undo")
            cases.append(timeit(lambda: runner.run(T.apply_scene_ir(ej, env.ctx)), ctx, params=_scene_params(scene), teardown=td))
            env.close()
    return cases


MESH_REF_QUAD = """#usda 1.0
(
    defaultPrim = "Quad"
    upAxis = "Z"
)
def Mesh "Quad"
{
    int[] faceVertexCounts = [4]
    int[] faceVertexIndices = [0, 1, 2, 3]
    point3f[] points = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)]
}
"""


@bench("harness.edit.rejected_rollback",
       "T.apply_scene_ir given an edit that fails Tier-1 critically (adds an open, non-watertight quad via mesh_ref): transaction, extract_meshes + Tier-1, automatic rollback, state sync. Returns 'ROLLED BACK ...'.",
       "Session open and scene load. Includes the cost of the Tier-1 pass that detects the failure, so it is the whole rejected-edit turn, not the rollback alone (see kernel.rollback.raw).")
def b_harness_rejected(ctx: Ctx) -> list[Case]:
    cases = []
    quad = ctx.dir("quad") / "quad.usda"
    quad.write_text(MESH_REF_QUAD)
    bad = json.dumps({"ops": [{"op": "upsert_node", "node": {"id": "sheet", "kind": "mesh_ref", "params": {"path": str(quad)}}}]})
    with asyncio.Runner() as runner:
        for n in ctx.sizes:
            scene = make_scene(n)
            env = open_harness(ctx, runner, scene, scene_budget())
            T = env.T
            cases.append(timeit(lambda: runner.run(T.apply_scene_ir(bad, env.ctx)), ctx, params=_scene_params(scene),
                                teardown=lambda _s, r: _expect(r, "ROLLED BACK", "rejected edit")))
            env.close()
    return cases


@bench("kernel.rollback.raw",
       "StageManager.rollback() of one pending one-node edit. Case discard=true is the path used for rejected edits (empties the layer in the same change block); discard=false is the path undo_last_edit uses. With include_next_read=true the timed region also contains the first extract_meshes() after the rollback.",
       "The edit that creates the pending transaction (untimed setup); Tier-1 QA.")
def b_rollback_raw(ctx: Ctx) -> list[Case]:
    cases = []
    for n in ctx.sizes:
        scene = make_scene(n)
        sm = build_stage(ctx.dir("st") / "s.usda", scene)
        patch = edit_patch(scene)
        for discard, nxt in ((True, False), (False, False), (True, True)):
            def fn(tid: str) -> None:
                sm.rollback(tid, discard=discard)
                if nxt:
                    sm.extract_meshes()
            cases.append(timeit(fn, ctx, params={**_scene_params(scene), "discard": discard, "include_next_read": nxt},
                                setup=lambda: sm.apply_patch(patch)))
    return cases


@bench("harness.edit.undo",
       "T.undo_last_edit: lock, rollback of the top transaction, state invalidation, extract_meshes + Tier-1 re-run, QA persistence.",
       "The accepted edit that creates the transaction to undo (untimed setup).")
def b_harness_undo(ctx: Ctx) -> list[Case]:
    cases = []
    with asyncio.Runner() as runner:
        for n in ctx.sizes:
            scene = make_scene(n)
            env = open_harness(ctx, runner, scene, scene_budget())
            ej, T = edit_json(scene), env.T
            cases.append(timeit(lambda _s: runner.run(T.undo_last_edit(env.ctx)), ctx, params=_scene_params(scene),
                                setup=lambda: _expect(runner.run(T.apply_scene_ir(ej, env.ctx)), "OK", "edit"),
                                teardown=lambda _s, r: _expect(r, "UNDONE", "undo")))
            env.close()
    return cases


@bench("harness.edit.to_pixels",
       "One agent turn of the edit loop: T.apply_scene_ir (accepted one-node edit) followed by T.render_preview (4-view clay contact sheet, 256 px per view, wireframe on = the tool defaults, PNG save, fake upload, preview bookkeeping).",
       "The undo that restores the base (untimed), visual QA, any model call.")
def b_harness_pixels(ctx: Ctx) -> list[Case]:
    cases = []
    with asyncio.Runner() as runner:
        for n in ctx.sizes:
            scene = make_scene(n)
            env = open_harness(ctx, runner, scene, scene_budget())
            ej, T = edit_json(scene), env.T

            def fn() -> dict[str, Any]:
                t0 = time.perf_counter_ns()
                a = runner.run(T.apply_scene_ir(ej, env.ctx))
                t1 = time.perf_counter_ns()
                p = runner.run(T.render_preview(env.ctx))
                t2 = time.perf_counter_ns()
                return {"a": a, "p": p, "edit": (t1 - t0) / 1e6, "render_preview": (t2 - t1) / 1e6}

            def td(_s: Any, r: dict[str, Any]) -> None:
                _expect(r["a"], "OK", "edit")
                _expect(r["p"], "PREVIEW", "render_preview")
                runner.run(T.undo_last_edit(env.ctx))
            cases.append(timeit(fn, ctx, params={**_scene_params(scene), "preview_px_per_view": T.PREVIEW_PX, "wireframe": True},
                                teardown=td, collect=lambda r: {"edit": r["edit"], "render_preview": r["render_preview"]}))
            env.close()
    return cases


def _timed_fake_client():
    from antigravity3d.decisions.fakes import FakeDecisionsClient

    class TimedFake(FakeDecisionsClient):
        """FakeDecisionsClient that accumulates the time spent inside the fake itself (wire validation + answer build)."""
        spent_ms = 0.0

        async def _create(self, model, input, questions):  # noqa: A002
            t0 = time.perf_counter_ns()
            try:
                return await super()._create(model, input, questions)
            finally:
                self.spent_ms += (time.perf_counter_ns() - t0) / 1e6
    return TimedFake()


@bench("harness.qa.visual_overhead",
       "T.run_visual_qa against FakeDecisionsClient with a current preview: approval revocation + QA persistence, request construction, the fake client call (validation + canned answers), score composition, audit-delta, state write. Tool-side overhead only.",
       "Model/API latency (none is incurred), rendering the preview (rendered once, untimed, before the loop). The fake client's own share is reported in extra.fake_client_ms_median.")
def b_visual_qa(ctx: Ctx) -> list[Case]:
    cases = []
    with asyncio.Runner() as runner:
        for n in ctx.sizes:
            scene = make_scene(n)
            client = _timed_fake_client()
            env = open_harness(ctx, runner, scene, scene_budget(), client=client)
            T = env.T
            _expect(runner.run(T.render_preview(env.ctx)), "PREVIEW", "render_preview")
            fake_ms: list[float] = []

            def setup() -> None:
                client.spent_ms = 0.0

            def td(_s: Any, r: str) -> None:
                if r.startswith(("STALE", "ERROR")):
                    raise RuntimeError(f"run_visual_qa: {r[:140]}")
                fake_ms.append(client.spent_ms)
            case = timeit(lambda _s: runner.run(T.run_visual_qa(env.ctx)), ctx, params=_scene_params(scene), setup=setup, teardown=td)
            case.extra["fake_client_ms_median"] = _r(statistics.median(fake_ms))
            case.extra["fake_client_calls_total"] = len(client.calls)
            cases.append(case)
            env.close()
    return cases


# ---------------------------------------------------------------------------------------------------------
# C. Preview rendering
# ---------------------------------------------------------------------------------------------------------
@bench("kernel.raster.contact_sheet",
       "kernel.raster.contact_sheet: 4 views (front, right, top, persp), wireframe overlay on, label + border drawing, sheet assembly (PIL image returned).",
       "Mesh extraction from USD, PNG encoding/saving, upload. Scenes are 12 spheres on a 4x3 grid (triangle count is the closest to the target the sphere segment count allows).")
def c_contact_sheet(ctx: Ctx) -> list[Case]:
    cases = []
    for target in (1000, 5000, 20000):
        meshes = sphere_set(target)
        tris = sum(len(m.triangles) for m in meshes)
        for px in ((256,) if ctx.quick else (256, 512)):
            cases.append(timeit(lambda: raster.contact_sheet(meshes, size=px), ctx,
                                params={"tris": tris, "meshes": len(meshes), "px_per_view": px, "views": 4, "wireframe": True}))
    return cases


@bench("kernel.som.marked_sheet",
       "kernel.som.marked_contact_sheet: 4 views rendered with render_buffers, set-of-mark badge placement per view, labels, sheet assembly.",
       "Mesh extraction, PNG encoding, any model call that would consume the marked sheet.")
def c_marked_sheet(ctx: Ctx) -> list[Case]:
    cases = []
    for target in (1000, 5000, 20000):
        meshes = sphere_set(target)
        tris = sum(len(m.triangles) for m in meshes)
        cases.append(timeit(lambda: som.marked_contact_sheet(meshes, size=256), ctx,
                            params={"tris": tris, "meshes": len(meshes), "px_per_view": 256, "views": 4}))
    return cases


# ---------------------------------------------------------------------------------------------------------
# D. Export kernels
# ---------------------------------------------------------------------------------------------------------
def single_meshes(ctx: Ctx) -> list[tuple[str, Any]]:
    def build():
        out = [(f"sphere_{t}", uv_sphere_mesh(t)) for t in (500, 2000, 10000, 50000)]
        out.append(("u_shape_concave", u_shape_mesh(2000)))
        return out
    return ctx.once("single_meshes", build)


def _mesh_params(label: str, m) -> dict[str, Any]:
    return {"mesh": label, "tris": int(len(m.triangles)), "verts": int(len(m.points))}


@bench("kernel.uv.unwrap",
       "fast_ops.unwrap_uvs (xatlas unwrap + pack + stretch statistics) at 1024 px/m and the default 2048 atlas resolution.",
       "Writing UVs to USD, LOD generation. Convex spheres and one concave U prism.")
def d_uv(ctx: Ctx) -> list[Case]:
    budget = EngineBudget("unreal_5")
    return [timeit(lambda m=m: fast_ops.unwrap_uvs(m, budget.texel_density_px_per_m), ctx,
                   params={**_mesh_params(label, m), "texel_density_px_per_m": budget.texel_density_px_per_m, "resolution": 2048})
            for label, m in single_meshes(ctx)]


@bench("kernel.lod.generate",
       "fast_ops.generate_lods with default ratios (1.0, 0.5, 0.25, 0.1): meshoptimizer simplify + cache/overdraw/fetch optimisation per level. with_uvs=true is the pipeline's call (seam-split points + UVs from unwrap, attribute-aware simplify); with_uvs=false is the weld-first path.",
       "UV unwrap (done once, untimed, for the with_uvs cases), writing LODs anywhere.")
def d_lod(ctx: Ctx) -> list[Case]:
    budget = EngineBudget("unreal_5")
    cases = []
    for label, m in single_meshes(ctx):
        u = fast_ops.unwrap_uvs(m, budget.texel_density_px_per_m)
        for with_uvs in (True, False):
            if with_uvs:
                fn = lambda u=u: fast_ops.generate_lods(u.points, u.triangles, uvs=u.uvs)  # noqa: E731
                lod_tris = [x.tri_count for x in fn()]
            else:
                fn = lambda m=m: fast_ops.generate_lods(m.points, m.triangles)  # noqa: E731
                lod_tris = [x.tri_count for x in fn()]
            cases.append(timeit(fn, ctx, params={**_mesh_params(label, m), "with_uvs": with_uvs}, extra={"lod_tris": lod_tris}))
    return cases


@bench("kernel.collider.convex",
       "fast_ops.generate_colliders (max_hulls=8) on convex closed meshes: topology check, scipy ConvexHull, the 'fast_path' (CoACD is not invoked).",
       "Hull authoring into USD/GLB. Only the convex spheres are run here; the concave mesh is kernel.collider.concave.")
def d_collider_convex(ctx: Ctx) -> list[Case]:
    cases = []
    for label, m in single_meshes(ctx):
        if label.startswith("u_shape"):
            continue
        hulls = fast_ops.generate_colliders([m], 8)
        cases.append(timeit(lambda m=m: fast_ops.generate_colliders([m], 8), ctx, params=_mesh_params(label, m),
                            extra={"hulls": len(hulls), "sources": sorted({h.source for h in hulls})}))
    return cases


@bench("kernel.collider.concave",
       "fast_ops.generate_colliders (max_hulls=8) on a concave U-shaped prism: topology check, CoACD decomposition (threshold 0.05, the package's settings), per-part hull.",
       "Hull authoring. CoACD may use threads that OMP_NUM_THREADS does not cap; extra.process_cpu_over_wall shows whether it did.")
def d_collider_concave(ctx: Ctx) -> list[Case]:
    label, m = [x for x in single_meshes(ctx) if x[0].startswith("u_shape")][0]
    hulls = fast_ops.generate_colliders([m], 8)
    return [timeit(lambda: fast_ops.generate_colliders([m], 8), ctx, params=_mesh_params(label, m),
                   extra={"hulls": len(hulls), "sources": sorted({h.source for h in hulls})})]


def prop_assets(ctx: Ctx, kind: str, textured: bool) -> SimpleNamespace:
    """TemplateAuthor prop (crate/barrel) with its stage-extracted meshes and export bundle. Memoised per run."""
    def build() -> SimpleNamespace:
        from antigravity3d.tactics.authoring import TemplateAuthor

        budget = EngineBudget("unreal_5")
        asset = kind.capitalize()
        scene = asyncio.run(TemplateAuthor().propose(PROMPTS[kind], budget=budget, n=1, context={"asset_name": asset}))[0]
        tex_px = 0
        if textured:
            scene, tex_px = _add_textures(ctx, scene)
        sm = build_stage(ctx.dir(f"{kind}_stage") / f"{asset}.usda", scene)
        meshes = sm.extract_meshes()
        bundle = fast_ops.run_export_pipeline(meshes, budget, asset)
        return SimpleNamespace(scene=scene, meshes=meshes, bundle=bundle, budget=budget, asset=asset, textured=textured,
                               tex_px=tex_px, tris=sum(len(m.triangles) for m in meshes), kind=kind)
    return ctx.once(f"prop:{kind}:{textured}", build)


def _add_textures(ctx: Ctx, scene):
    """Give each material a procedural albedo (FakeImageBackend) + derive_pbr ORM and normal, written as PNG files."""
    from antigravity3d.genmedia.nano_banana import FakeImageBackend, ImageForge
    from antigravity3d.genmedia.pbr_deriver import derive_pbr
    from antigravity3d.tactics.common import load_png

    px = 1024 if ctx.quick else 2048
    forge = ImageForge(FakeImageBackend())
    tdir = ctx.dir("textures")
    mats = []
    for m in scene.materials:
        png = asyncio.run(forge.seamless_albedo(f"{m.id} surface", size=px)).png_bytes
        maps = derive_pbr(load_png(png), metallic_hint=float(m.metallic), convention="opengl")
        paths = {}
        for role, img in (("albedo", maps.albedo), ("orm", maps.orm), ("normal", maps.normal)):
            p = tdir / f"{m.id}_{role}.png"
            img.save(p)
            paths[f"{role}_uri"] = str(p)
        mats.append(m.model_copy(update=paths))
    return scene.model_copy(update={"materials": mats}), px


@bench("kernel.export.pipeline",
       "fast_ops.run_export_pipeline: unwrap_scene (xatlas, one atlas per material) + generate_lods per mesh + generate_colliders. This is the 'UV + LOD + collider outside Blender' figure.",
       "Package writing/validation, USD authoring, mesh extraction (meshes are prepared untimed). Per-stage medians from ExportBundle.timings_ms are in extra.parts_median_ms.")
def d_pipeline(ctx: Ctx) -> list[Case]:
    budget = EngineBudget("unreal_5")
    cases = []
    for label, m in single_meshes(ctx):
        cases.append(timeit(lambda m=m: fast_ops.run_export_pipeline([m], budget, "Single"), ctx,
                            params={"subject": label, **_mesh_params(label, m), "parts": 1},
                            collect=lambda b: dict(b.timings_ms)))
    for kind in ("crate", "barrel"):
        p = prop_assets(ctx, kind, False)
        cases.append(timeit(lambda p=p: fast_ops.run_export_pipeline(p.meshes, budget, p.asset), ctx,
                            params={"subject": f"{kind} (TemplateAuthor)", "tris": p.tris, "parts": len(p.meshes),
                                    "materials": len(p.scene.materials)},
                            collect=lambda b: dict(b.timings_ms),
                            extra={"design_doc_target_ms": 250, "hulls": len(p.bundle.colliders),
                                   "hull_sources": sorted({h.source for h in p.bundle.colliders})}))
    return cases


# ---------------------------------------------------------------------------------------------------------
# E. Validation and packaging
# ---------------------------------------------------------------------------------------------------------
@bench("kernel.qa.tier1",
       "fast_ops.tier1_qa on the crate and barrel meshes (topology, budgets, grounding, pairwise AABB/contact checks).",
       "Mesh extraction from USD (meshes prepared untimed).")
def e_tier1(ctx: Ctx) -> list[Case]:
    cases = []
    for kind in ("crate", "barrel"):
        p = prop_assets(ctx, kind, False)
        rep = fast_ops.tier1_qa(p.meshes, p.budget)
        cases.append(timeit(lambda p=p: fast_ops.tier1_qa(p.meshes, p.budget), ctx,
                            params={"subject": kind, "tris": p.tris, "parts": len(p.meshes)},
                            extra={"tier1_ok": rep.ok, "issues": sorted({i.code for i in rep.issues})}))
    return cases


def _written_package(ctx: Ctx, kind: str, textured: bool) -> SimpleNamespace:
    """Package written once (untimed) for the validate benchmarks."""
    def build() -> SimpleNamespace:
        p = prop_assets(ctx, kind, textured)
        sm = build_stage(ctx.dir("pkgstage") / f"{p.asset}.usda", p.scene)
        out = ctx.dir("pkg")
        man = kpackage.write_engine_package(sm, p.bundle, p.budget, out, asset_name=p.asset, qa=_QA)
        return SimpleNamespace(p=p, out=out, man=man)
    return ctx.once(f"pkgw:{kind}:{textured}", build)


_QA = {"tier1_ok": True, "tier15": 3.6, "defects": {}, "audit": []}


@bench("kernel.validate.usd",
       "Three separate checks on the written package USD of the crate/barrel: validate_usd(stage) (all registered UsdValidation validators), check_engine_compliance(stage, meshes, budget) (unreal_5 rules, texture size/convention, hulls), and validate_usd(path) which also opens the file.",
       "Writing the package. The stage is the package's own USD (LOD variants, colliders, textured materials).")
def e_validate_usd(ctx: Ctx) -> list[Case]:
    from pxr import Usd

    cases = []
    for kind in ("crate", "barrel"):
        w = _written_package(ctx, kind, True)
        usd = w.out / w.man.data["usd"]
        stage = Usd.Stage.Open(str(usd))
        mesh_data = [MeshData(u.node_id, f"/{w.p.asset}/{u.node_id}", u.points, u.triangles, u.atlas_id) for u in w.p.bundle.uvs]
        base = {"subject": kind, "tris": w.p.tris}
        cases.append(timeit(lambda: validator.validate_usd(stage), ctx, params={**base, "check": "validate_usd(stage)"}))
        cases.append(timeit(lambda: validator.check_engine_compliance(stage, mesh_data, w.p.budget), ctx,
                            params={**base, "check": "check_engine_compliance(stage)"}))
        cases.append(timeit(lambda: validator.validate_usd(usd), ctx, params={**base, "check": "validate_usd(path) incl. open"}))
    return cases


@bench("kernel.package.write",
       "kernel.package.write_engine_package on a fresh stage: UV authoring, textures copied/converted (unreal_5 DirectX normal flip), USD + USDZ + GLB LODs + collider GLB, inspector HTML, hashing, and the function's built-in validation (UsdValidation, engine compliance, cold package validation).",
       "Stage creation (untimed setup), the UV/LOD/collider bundle (computed once, untimed), deleting the package (untimed teardown). Texture inputs: textured=true gives every material 3 maps (albedo, ORM, normal) from FakeImageBackend + derive_pbr at texture_px; textured=false has no texture inputs, which skips texture copy, hashing and the inspector's texture embedding.")
def e_package_write(ctx: Ctx) -> list[Case]:
    cases = []
    for kind in ("crate", "barrel"):
        for textured in (False, True):
            p = prop_assets(ctx, kind, textured)

            def setup(p=p) -> Any:
                sm = build_stage(ctx.dir("pw") / f"{p.asset}.usda", p.scene)
                return sm, ctx.dir("pwout")

            def fn(st, p=p) -> Any:
                return kpackage.write_engine_package(st[0], p.bundle, p.budget, st[1], asset_name=p.asset, qa=_QA)

            def td(st, man) -> None:
                if not man.ok:
                    raise RuntimeError("package validation reported critical issues: " + "; ".join(
                        f"{i.code}" for i in man.issues if i.severity == "critical")[:160])
                shutil.rmtree(st[1], ignore_errors=True)
                shutil.rmtree(st[0].stage.GetRootLayer().realPath.rsplit("/", 1)[0], ignore_errors=True)
            c = timeit(fn, ctx, params={"subject": kind, "tris": p.tris, "textured": textured,
                                        "texture_px": p.tex_px if textured else None}, setup=setup, teardown=td)
            c.extra["package_files"] = None
            cases.append(c)
    return cases


@bench("kernel.package.validate_cold",
       "validators.validate_package on an already written package directory: manifest + hash check of every file, USD re-open, UV/texture/LOD/collider consistency checks, GLB material checks.",
       "Writing the package (once, untimed). Same textured packages as kernel.package.write.")
def e_validate_cold(ctx: Ctx) -> list[Case]:
    cases = []
    for kind in ("crate", "barrel"):
        w = _written_package(ctx, kind, True)
        size = sum(f.stat().st_size for f in w.out.rglob("*") if f.is_file())
        issues = validator.validate_package(w.out)
        cases.append(timeit(lambda: validator.validate_package(w.out), ctx,
                            params={"subject": kind, "tris": w.p.tris, "files": len(w.man.files), "package_bytes": size},
                            extra={"critical_issues": sum(i.severity == "critical" for i in issues)}))
    return cases


# ---------------------------------------------------------------------------------------------------------
# F. Other kernels
# ---------------------------------------------------------------------------------------------------------
@bench("kernel.settle",
       "kernel.settle.settle: 10 movable props (boxes, cylinders, spheres; 3 stacked above others) dropped onto a static floor slab, iterating to convergence.",
       "Applying the resulting offsets as IR ops, mesh extraction. Meshes are built directly from compile_ir primitives.")
def f_settle(ctx: Ctx) -> list[Case]:
    rng = np.random.default_rng(1234)
    meshes = [as_mesh("floor", compile_ir.primitive_mesh(NodeIR(id="f", kind="box", params={"size": (20.0, 20.0, 0.2)}))[0] + np.array([0, 0, -0.1]),
                      compile_ir.primitive_mesh(NodeIR(id="f", kind="box", params={"size": (20.0, 20.0, 0.2)}))[1], "m0")]
    ids = set()
    for i in range(10):
        kind = ("box", "cylinder", "sphere")[i % 3]
        params = {"box": {"size": (0.6, 0.5, 0.4)}, "cylinder": {"radius": 0.25, "height": 0.5, "segments": 16},
                  "sphere": {"radius": 0.3, "segments": 16, "rings": 8}}[kind]
        pts, tri = compile_ir.primitive_mesh(NodeIR(id=f"p{i}", kind=kind, params=params))
        stacked = i >= 7  # these three share x/y with props 0..2 and start above them
        xy = np.array([(i - 7) * 2.0, 0.0]) if stacked else np.array([(i % 7) * 2.0 - 6.0, 0.0])
        z0 = float(rng.uniform(1.0, 2.5)) + (1.0 if stacked else 0.0)
        meshes.append(as_mesh(f"p{i}", np.asarray(pts, np.float64) + np.array([xy[0], xy[1], z0]), tri, "m1"))
        ids.add(f"p{i}")
    res = ksettle.settle(meshes, ids, ground_z=-0.2)
    return [timeit(lambda: ksettle.settle(meshes, ids, ground_z=-0.2), ctx,
                   params={"movable_props": 10, "static_meshes": 1, "tris": int(sum(len(m.triangles) for m in meshes))},
                   extra={"converged_all": bool(res.ok), "total_iters": int(res.iterations)})]


def _spatial_inputs(n: int) -> tuple[list, list]:
    side = math.ceil(math.sqrt(n))
    rng = np.random.default_rng(7)
    cube_p, cube_t = compile_ir.primitive_mesh(NodeIR(id="b", kind="box", params={"size": (1.0, 1.0, 1.0)}))
    cube_p = np.asarray(cube_p, np.float64)
    meshes, prims = [], []
    for i in range(n):
        off = np.array([(i % side) * 1.5, (i // side) * 1.5, float(rng.uniform(0.5, 3.0))])
        pts = cube_p + off
        m = as_mesh(f"b{i}", pts, cube_t, f"m{i % 3}")
        meshes.append(m)
        prims.append(spatial.PrimInfo(m.node_id, tuple(pts.min(0)), tuple(pts.max(0)), 12, m.material_id, None))
    return meshes, prims


@bench("kernel.spatial.build",
       "Octree construction. constructor=SpatialIndex(meshes) takes world-space MeshData (per-mesh bounds + tree); constructor=from_bounds takes precomputed PrimInfo (what StageManager uses). constructor=stage_first_query is StageManager.query_region right after an edit, i.e. USD traversal + from_bounds + one query.",
       "Building the input mesh list / PrimInfo list (untimed). Boxes are 1 m cubes on a 1.5 m grid.")
def f_spatial_build(ctx: Ctx) -> list[Case]:
    n = 2000 if ctx.quick else 10000
    meshes, prims = _spatial_inputs(n)
    cases = [timeit(lambda: spatial.SpatialIndex(meshes), ctx, params={"boxes": n, "constructor": "SpatialIndex(meshes)"}),
             timeit(lambda: spatial.SpatialIndex.from_bounds(prims), ctx, params={"boxes": n, "constructor": "from_bounds"})]
    scene = make_scene(200)
    sm = build_stage(ctx.dir("st") / "s.usda", scene)
    patch = edit_patch(scene)

    def invalidate() -> None:  # a real edit fires the stage's ObjectsChanged notice, which drops the cached index
        sm.apply_patch(patch)
        sm.rollback(discard=True)
    cases.append(timeit(lambda _s: sm.query_region((-3, -3, 0), (3, 3, 2)), ctx, setup=invalidate,
                        params={"boxes": 200, "constructor": "stage_first_query (StageManager.query_region after an edit)"}))
    return cases


@bench("kernel.spatial.query_region",
       "SpatialIndex.query_region(...) + RegionResult.to_text(1200) on a prebuilt index, for a small, a medium and a whole-scene region; plus StageManager.query_region on a 200-node stage with the index already cached.",
       "Index construction.")
def f_spatial_query(ctx: Ctx) -> list[Case]:
    n = 2000 if ctx.quick else 10000
    meshes, _ = _spatial_inputs(n)
    idx = spatial.SpatialIndex(meshes)
    side = math.ceil(math.sqrt(n)) * 1.5
    cases = []
    for label, half in (("small", 3.0), ("medium", side * 0.15), ("whole", side)):
        lo, hi = (side / 2 - half, side / 2 - half, 0.0), (side / 2 + half, side / 2 + half, 4.0)
        res = idx.query_region(lo, hi)
        cases.append(timeit(lambda lo=lo, hi=hi: idx.query_region(lo, hi).to_text(1200), ctx,
                            params={"boxes": n, "region": label, "region_half_width_m": round(half, 2)},
                            extra={"result_kind": res.kind, "prims_in_region": res.total_prims}))
    scene = make_scene(200)
    sm = build_stage(ctx.dir("st") / "s.usda", scene)
    sm.query_region((-3, -3, 0), (3, 3, 2))  # build the cached index
    cases.append(timeit(lambda: sm.query_region((-3, -3, 0), (3, 3, 2)), ctx,
                        params={"boxes": 200, "region": "6 m square, StageManager.query_region, index cached"}))
    return cases


def _cache_cases(ctx: Ctx) -> dict[str, list[Case]]:
    from antigravity3d.tactics.cached import cached_export_pipeline

    def build() -> dict[str, list[Case]]:
        hit_cases, miss_cases = [], []
        for kind in ("crate", "barrel"):
            p = prop_assets(ctx, kind, False)
            base = {"subject": kind, "tris": p.tris, "parts": len(p.meshes)}

            def setup():
                return kcache.BuildCache(kcache.LocalBlobStore(ctx.dir("cmiss")))

            def fn_miss(cache, p=p):
                return cached_export_pipeline(cache, p.scene, p.meshes, p.budget)

            def td_miss(_c, r):
                if r[1]:
                    raise RuntimeError("expected a cache miss")
            miss = timeit(fn_miss, ctx, params=base, setup=setup, teardown=td_miss)
            warm = kcache.BuildCache(kcache.LocalBlobStore(ctx.dir("chit")))
            cached_export_pipeline(warm, p.scene, p.meshes, p.budget)  # populate (untimed)

            def fn_hit(p=p):
                return cached_export_pipeline(warm, p.scene, p.meshes, p.budget)
            hit = timeit(fn_hit, ctx, params=base, teardown=lambda _s, r: (_ for _ in ()).throw(RuntimeError("expected a hit")) if not r[1] else None)
            ratio = round(statistics.median(miss.samples_ms) / statistics.median(hit.samples_ms), 1)
            miss.extra["miss_over_hit_median_ratio"] = ratio
            hit.extra["miss_over_hit_median_ratio"] = ratio
            hit.extra["miss_median_ms"] = _r(statistics.median(miss.samples_ms))
            miss.extra["hit_median_ms"] = _r(statistics.median(hit.samples_ms))
            hit_cases.append(hit)
            miss_cases.append(miss)
        return {"hit": hit_cases, "miss": miss_cases}
    return ctx.once("cache_cases", build)


@bench("kernel.cache.hit",
       "tactics.cached.cached_export_pipeline on a populated LocalBlobStore: ir_subtree_key/node_key hashing, blob read, npz decode into an ExportBundle, hit accounting.",
       "The original compute. Local disk store only (no GCS tier). extra.miss_over_hit_median_ratio compares with kernel.cache.miss.")
def f_cache_hit(ctx: Ctx) -> list[Case]:
    return _cache_cases(ctx)["hit"]


@bench("kernel.cache.miss",
       "cached_export_pipeline on an empty store (fresh directory per iteration): key hashing, run_export_pipeline, bundle npz encode, blob write, then decode of the returned bundle.",
       "Creating the empty store (untimed).")
def f_cache_miss(ctx: Ctx) -> list[Case]:
    return _cache_cases(ctx)["miss"]


# ---------------------------------------------------------------------------------------------------------
# G. IPC overhead (stand-in daemon only)
# ---------------------------------------------------------------------------------------------------------
@bench("ipc.blender_pool.fake",
       "STAND-IN ONLY, IPC OVERHEAD ONLY: BlenderPool(1, fake=True) (a CPython daemon, no bpy, no Blender). Case op=ping is one request/response over the socketpair; op=render_1024x1024x4_f32 requests a fake render and reads the 16.8 MB float32 image back through shared memory (the daemon synthesises the gradient with numpy: that cost is included).",
       "Everything about real Blender: start-up, scene import, rendering. These numbers say nothing about real render time. Pool start-up is excluded.")
def g_pool(ctx: Ctx) -> list[Case]:
    from antigravity3d.adapters.blender_5_3.pool import BlenderPool

    async def go() -> list[Case]:
        t0 = time.perf_counter()
        async with BlenderPool(1, fake=True) as pool:
            startup_ms = (time.perf_counter() - t0) * 1e3
            cases = [await atimeit(lambda: pool.call("ping"), ctx, params={"op": "ping", "workers": 1, "fake": True},
                                   teardown=lambda _s, r: None if r.get("pong") else (_ for _ in ()).throw(RuntimeError("no pong")),
                                   extra={"pool_start_ms_not_timed": _r(startup_ms)})]

            async def render():
                return await pool.call("render", engine="cycles", width=1024, height=1024)

            def td(_s, r):
                img = r["image"]
                if img.shape != (1024, 1024, 4) or img.dtype != np.float32:
                    raise RuntimeError(f"unexpected image {img.shape} {img.dtype}")
            cases.append(await atimeit(render, ctx, params={"op": "render_1024x1024x4_f32", "payload_bytes": 1024 * 1024 * 4 * 4,
                                                            "workers": 1, "fake": True}, teardown=td))
        return cases
    with asyncio.Runner() as runner:
        return runner.run(go())


@bench("mcp.roundtrip.apply_scene_ir",
       "tools/call apply_scene_ir (accepted one-node SetTransform) through the real MCP stdio server (python -m antigravity3d.runtime.mcp_server, offline) driven by the official mcp client: client framing, pipe I/O, server dispatch + session gate + the tool itself. extra.direct_median_ms is the same tool called in-process on an identical session (base scene applied as one open transaction, as over MCP); extra.mcp_overhead_ms = mcp median - direct median.",
       "Server start-up/handshake/open_asset_session (reported in extra, not in samples), the undo between iterations (untimed). The server is a separate process, so its CPU competes with the client on this 4-vCPU box.")
def g_mcp(ctx: Ctx) -> list[Case]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    sizes = [5] if ctx.quick else [5, 50]
    state, work = ctx.dir("mcpstate"), ctx.dir("mcpwork")
    env = {k: v for k, v in os.environ.items() if k not in {"GEMINI_API_KEY", "GOOGLE_API_KEY", "AGY3D_POOL_SOCKET"}}
    env.update({"AGY3D_OFFLINE": "1", "AGY3D_STATE_DIR": str(state), **THREAD_ENV})

    async def call(s: ClientSession, name: str, **args: Any) -> str:
        res = await s.call_tool(name, args)
        return "".join(c.text for c in res.content if c.type == "text")

    async def go(n: int, direct_ms: float) -> Case:
        scene = make_scene(n)
        ej = edit_json(scene)
        params = StdioServerParameters(command=sys.executable, args=["-m", "antigravity3d.runtime.mcp_server"], env=env, cwd=str(work))
        t0 = time.perf_counter()
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as s:
                await s.initialize()
                t_init = (time.perf_counter() - t0) * 1e3
                t1 = time.perf_counter()
                line = await call(s, "open_asset_session", asset_name="Bench", target_engine="unreal_5",
                                  max_tris=BENCH_BUDGET_TRIS, workdir=str(ctx.dir("mcpsess") / "w"))
                if not line.startswith("SESSION"):
                    raise RuntimeError(f"open_asset_session: {line[:140]}")
                r = await call(s, "apply_scene_ir", ops_json=scene.model_dump_json())
                _expect(r, "OK", "initial scene over MCP")
                t_open = (time.perf_counter() - t1) * 1e3

                async def fn():
                    return await call(s, "apply_scene_ir", ops_json=ej)

                async def td(_s, r):
                    _expect(r, "OK", "mcp edit")
                    _expect(await call(s, "undo_last_edit"), "UNDONE", "mcp undo")
                case = await atimeit(fn, ctx, params={**_scene_params(scene), "transport": "mcp stdio, offline server"}, teardown=td,
                                     extra={"initialize_ms_not_timed": _r(t_init), "open_session_and_scene_ms_not_timed": _r(t_open)})
        case.extra["direct_median_ms"] = _r(direct_ms)
        case.extra["mcp_overhead_ms"] = _r(statistics.median(case.samples_ms) - direct_ms)
        return case

    cases = []
    with asyncio.Runner() as runner:
        for n in sizes:
            scene = make_scene(n)
            env_d = open_harness(ctx, runner, scene, scene_budget(), commit_base=False)  # same shape as the MCP session
            ej = edit_json(scene)
            T = env_d.T
            dcase = timeit(lambda: runner.run(T.apply_scene_ir(ej, env_d.ctx)), ctx, params={"nodes": n},
                           teardown=lambda _s, r: (_expect(r, "OK", "direct edit"), runner.run(T.undo_last_edit(env_d.ctx))))
            env_d.close()
            cases.append(runner.run(go(n, statistics.median(dcase.samples_ms))))
    return cases


# ---------------------------------------------------------------------------------------------------------
# H. Pipeline (in-process, offline fixtures)
# ---------------------------------------------------------------------------------------------------------
def _merge(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _overlap_ms(spans: list[tuple[float, float]], lo: float, hi: float) -> float:
    return sum(max(0.0, min(b, hi) - max(a, lo)) for a, b in spans) * 1e3


def _make_fixtures(memo: dict, replay: bool):
    """(backend, client) wrappers that record the wall-clock spans they spend inside the fakes."""
    from antigravity3d.cli import OFFLINE_ANSWERS
    from antigravity3d.decisions.fakes import FakeDecisionsClient
    from antigravity3d.genmedia.nano_banana import FakeImageBackend

    class TimedBackend(FakeImageBackend):
        def __init__(self) -> None:
            super().__init__()
            self.spans: list[tuple[float, float]] = []

        async def generate(self, *, model, prompt, images=(), size=None, aspect_ratio="1:1"):
            key = (prompt, tuple(hashlib.sha1(i).hexdigest() for i in images), size)
            t0 = time.perf_counter()
            if replay and key in memo:
                self.calls.append({"model": model, "prompt": prompt, "n_images": len(images), "size": size, "replayed": True})
                data = memo[key]
            else:
                data = await super().generate(model=model, prompt=prompt, images=images, size=size, aspect_ratio=aspect_ratio)
                memo.setdefault(key, data)
            self.spans.append((t0, time.perf_counter()))
            return data

    class TimedClient(FakeDecisionsClient):
        def __init__(self, **kw: Any) -> None:
            super().__init__(**kw)
            self.spans: list[tuple[float, float]] = []

        async def _create(self, model, input, questions):  # noqa: A002
            t0 = time.perf_counter()
            try:
                return await super()._create(model, input, questions)
            finally:
                self.spans.append((t0, time.perf_counter()))
    return TimedBackend(), TimedClient(answers=dict(OFFLINE_ANSWERS))


def run_prop_once(prompt: str, out: Path, *, single: bool, memo: dict, replay: bool = False, seed: int = 0,
                  engine: str = "unreal_5", record_spans: bool = True) -> dict[str, Any]:
    """One in-process `agy3d prop PROMPT --offline` equivalent (same wiring as cli.cmd_prop with --offline, no --blender)."""
    from antigravity3d.dags import runner as dag_runner
    from antigravity3d.dags.prop_to_engine import PropToEngineDAG
    from antigravity3d.decisions.engine import DecisionControlPlane
    from antigravity3d.genmedia.nano_banana import ImageForge
    from antigravity3d.tactics.authoring import TemplateAuthor
    from antigravity3d.tactics.distill import FixLog

    backend, client = _make_fixtures(memo, replay)
    out.mkdir(parents=True, exist_ok=True)
    cache = kcache.BuildCache(kcache.LocalBlobStore(out / ".agy3d-cache"))
    dag = PropToEngineDAG(plane=DecisionControlPlane(client), forge=ImageForge(backend), author=TemplateAuthor(seed=seed),
                          workdir=out, budget=EngineBudget(engine), pool=None, cache=cache, style_anchor=None,
                          fixlog=FixLog.for_prop(out), max_rounds=4, require_verified=False, single_candidate=single)
    origin: list[float] = []
    orig_run = dag_runner.Dag.run

    async def timed_run(self, *a: Any, **kw: Any):
        origin.append(time.perf_counter())  # DagReport node times are relative to ~this instant (Dag.run's own t0)
        return await orig_run(self, *a, **kw)
    dag_runner.Dag.run = timed_run
    try:
        async def go():
            t0 = time.perf_counter()
            rep = await dag.run(prompt)
            return rep, (time.perf_counter() - t0) * 1e3
        rep, wall_ms = asyncio.run(go())
    finally:
        dag_runner.Dag.run = orig_run
    t_origin = origin[0]
    img, judge = _merge(backend.spans), _merge(client.spans)
    both = _merge(backend.spans + client.spans)
    nodes = {}
    for name, n in rep.nodes.items():
        lo, hi = t_origin + n.start_ms / 1e3, t_origin + n.end_ms / 1e3
        dur = n.duration_ms
        fx = _overlap_ms(both, lo, hi) if n.status in ("ok", "failed") else 0.0
        nodes[name] = {"status": n.status, "duration_ms": _r(dur), "fixture_ms": _r(fx),
                       "fixture_frac": _r(fx / dur) if dur > 0.5 else 0.0}
    fix_union = _overlap_ms(both, 0, float("inf"))
    x = rep.extras
    cache_stats = x["cost"]["cache"] or {}
    return {"wall_ms": wall_ms, "dag_wall_ms": rep.wall_ms, "nodes": nodes,
            "fixture_union_ms": fix_union, "fixture_image_ms": _overlap_ms(img, 0, float("inf")),
            "fixture_judge_ms": _overlap_ms(judge, 0, float("inf")),
            "fixture_image_sum_ms": sum(b - a for a, b in backend.spans) * 1e3,
            "fixture_judge_sum_ms": sum(b - a for a, b in client.spans) * 1e3,
            "local_compute_ms": wall_ms - fix_union,
            "decision_calls": len(client.calls), "image_generations": len(backend.calls),
            "image_calls_replayed": sum(1 for c in backend.calls if c.get("replayed")),
            "cache_hits": cache_stats.get("hits"), "cache_misses": cache_stats.get("misses"),
            "dag_ok": bool(rep.ok), "preview_gates_ok": bool(x["preview_gates_ok"]), "gates_ok": bool(x["gates_ok"]),
            "route_reused": bool(rep.nodes["route"].data.get("reused")),
            "skipped_nodes": sorted(rep.by_status("skipped")), "failed_nodes": sorted(rep.by_status("failed")),
            "tier15": x["scores"].get("tier15") if x.get("scores") else None}


def _pipeline_runs(ctx: Ctx) -> list[dict[str, Any]]:
    def build() -> list[dict[str, Any]]:
        repeats = 1 if ctx.quick else 3
        prompts = dict(list(PROMPTS.items())[:2]) if ctx.quick else PROMPTS
        rows = []
        for kind, prompt in prompts.items():
            for mode in ("governed", "single_candidate"):
                single = mode == "single_candidate"
                for rep_i in range(repeats):
                    base = ctx.dir("prop")
                    memo: dict = {}
                    cold = run_prop_once(prompt, base, single=single, memo=memo)           # fresh dir + cache
                    warm = run_prop_once(prompt, base, single=single, memo={})             # same dir, same cache
                    replay = run_prop_once(prompt, ctx.dir("prop"), single=single, memo=memo, replay=True)  # fresh cache, fixture images served from memo
                    for cache_mode, r in (("cold", cold), ("warm", warm), ("cold_image_replay", replay)):
                        rows.append({"prompt_key": kind, "prompt": prompt, "mode": mode, "cache": cache_mode, "repeat": rep_i, **r})
                    shutil.rmtree(base, ignore_errors=True)
        return rows
    return ctx.once("pipeline_runs", build)


def _med(rows: list[dict], key: str) -> float:
    return _r(statistics.median(r[key] for r in rows))


@bench("pipeline.prop_to_engine.offline",
       "PropToEngineDAG.run wall time (in-process, same wiring as `agy3d prop --offline`, bare event-loop start included, unreal_5, max_rounds=4) per prompt x mode x cache state. cache=cold: fresh output dir; cache=warm: second run on the same dir/cache (route decision and image/PBR/export results reused); cache=cold_image_replay: fresh cache but the fixture image backend returns pre-generated bytes instantly, which measures the run without the fixture's procedural texture synthesis. Per-node durations and fixture shares are in extra.",
       "Interpreter start (see cli.*), Blender, any real network wait. Fixture time (fake image synthesis in a worker thread, fake judge validation) IS inside the wall time; extra.local_compute_ms = wall - union of fixture spans, and the cold_image_replay case gives a direct measurement of the same quantity.")
def h_pipeline(ctx: Ctx) -> list[Case]:
    rows = _pipeline_runs(ctx)
    cases = []
    for key in dict.fromkeys((r["prompt_key"], r["mode"], r["cache"]) for r in rows):
        sel = [r for r in rows if (r["prompt_key"], r["mode"], r["cache"]) == key]
        nodes = {}
        for name in sel[0]["nodes"]:
            nodes[name] = {"median_duration_ms": _r(statistics.median(r["nodes"][name]["duration_ms"] for r in sel)),
                           "median_fixture_frac": _r(statistics.median(r["nodes"][name]["fixture_frac"] for r in sel)),
                           "status": sorted({r["nodes"][name]["status"] for r in sel})}
        cases.append(Case(
            {"prompt": sel[0]["prompt"], "mode": key[1], "cache": key[2], "engine": "unreal_5", "repeats": len(sel)},
            [r["wall_ms"] for r in sel],
            {"node_ms": nodes, "local_compute_ms_median": _med(sel, "local_compute_ms"),
             "fixture_union_ms_median": _med(sel, "fixture_union_ms"),
             "fixture_image_ms_median": _med(sel, "fixture_image_ms"), "fixture_judge_ms_median": _med(sel, "fixture_judge_ms"),
             "fixture_image_sum_ms_median": _med(sel, "fixture_image_sum_ms"),
             "decision_calls": sorted({r["decision_calls"] for r in sel}), "image_generations": sorted({r["image_generations"] for r in sel}),
             "image_calls_replayed": sorted({r["image_calls_replayed"] for r in sel}),
             "cache_hits_misses": sorted({(r["cache_hits"], r["cache_misses"]) for r in sel}),
             "route_reused": sorted({r["route_reused"] for r in sel}),
             "dag_ok": all(r["dag_ok"] for r in sel), "preview_gates_ok": all(r["preview_gates_ok"] for r in sel),
             "gates_ok_verified_publication": all(r["gates_ok"] for r in sel),
             "skipped_nodes": sorted({n for r in sel for n in r["skipped_nodes"]}), "failed_nodes": sorted({n for r in sel for n in r["failed_nodes"]}),
             "tier15": sorted({r["tier15"] for r in sel if r["tier15"] is not None})}))
    return cases


@bench("pipeline.prop_to_engine.node_classification",
       "Derived from the same runs as pipeline.prop_to_engine.offline (cold cache only): for each DAG node, the median share of its own duration during which a fixture call (fake image synthesis or fake judge) was in progress. A node is 'fixture-dominated' when that share is >= 0.5, otherwise 'local deterministic work'. The 'samples' are the per-run cold wall times.",
       "Warm runs. A node's wall duration overlaps other nodes (pbr runs beside geometry), so node durations do not sum to wall time. Shares assume node windows from DagReport and fixture spans use the same clock (they do, to within the event-loop start-up of Dag.run).")
def h_classification(ctx: Ctx) -> list[Case]:
    rows = [r for r in _pipeline_runs(ctx) if r["cache"] == "cold"]
    cases = []
    for mode in ("governed", "single_candidate"):
        sel = [r for r in rows if r["mode"] == mode]
        table = {}
        for name in sel[0]["nodes"]:
            dur = statistics.median(r["nodes"][name]["duration_ms"] for r in sel)
            frac = statistics.median(r["nodes"][name]["fixture_frac"] for r in sel)
            fx = statistics.median(r["nodes"][name]["fixture_ms"] for r in sel)
            table[name] = {"median_duration_ms": _r(dur), "median_fixture_ms": _r(fx), "median_fixture_frac": _r(frac),
                           "class": "fixture-dominated" if frac >= 0.5 else "local deterministic work"}
        cases.append(Case({"mode": mode, "cache": "cold", "runs": len(sel)}, [r["wall_ms"] for r in sel],
                          {"nodes": table, "local_compute_ms_median": _med(sel, "local_compute_ms"),
                           "fixture_union_ms_median": _med(sel, "fixture_union_ms"),
                           "wall_ms_median": _med(sel, "wall_ms")}))
    return cases


# ---------------------------------------------------------------------------------------------------------
# I. CLI end to end
# ---------------------------------------------------------------------------------------------------------
def _clean_env(state: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in {"GEMINI_API_KEY", "GOOGLE_API_KEY", "AGY3D_POOL_SOCKET", "BLENDER_BIN"}}
    env.update({"AGY3D_OFFLINE": "1", "AGY3D_STATE_DIR": str(state), **THREAD_ENV})
    return env


def _agy3d_cmd() -> list[str]:
    exe = Path(sys.executable).parent / "agy3d"
    return [str(exe)] if exe.exists() else [sys.executable, "-m", "antigravity3d.cli"]


def _run_cli(args: list[str], env: dict[str, str], cwd: Path) -> tuple[float, subprocess.CompletedProcess]:
    t0 = time.perf_counter_ns()
    p = subprocess.run(_agy3d_cmd() + args, env=env, cwd=cwd, capture_output=True, text=True, timeout=600)
    return (time.perf_counter_ns() - t0) / 1e6, p


@bench("cli.prop.offline",
       "Subprocess wall time of `agy3d prop \"wooden crate with iron brackets\" --offline --out DIR` (fresh DIR each run): console-script start, imports, the whole offline pipeline, report printing, process exit.",
       "Nothing in-process is subtracted: this is what a shell user waits for. Exit status 0 is required (preview gates passed).")
def i_cli_prop(ctx: Ctx) -> list[Case]:
    runs = 2 if ctx.quick else 3
    env = _clean_env(ctx.dir("clistate"))
    samples = []
    for _ in range(runs):
        out = ctx.dir("cliprop") / "o"
        ms, p = _run_cli(["prop", PROMPTS["crate"], "--offline", "--out", str(out)], env, ctx.tmp)
        if p.returncode != 0:
            raise RuntimeError(f"agy3d prop exited {p.returncode}: {(p.stderr or p.stdout)[-160:]!r}")
        samples.append(ms)
    return [Case({"prompt": PROMPTS["crate"], "engine": "unreal_5", "runs": runs}, samples, {"first_run_ms": _r(samples[0])})]


@bench("cli.models",
       "Subprocess wall time of `agy3d models` (prints a static table): console-script start + CLI module imports + exit. A proxy for CLI cold start.",
       "Pipeline work. Not a warm number: each run is a new interpreter (OS file cache is warm after the first run, which is kept).")
def i_cli_models(ctx: Ctx) -> list[Case]:
    env = _clean_env(ctx.dir("clistate"))
    samples = []
    for _ in range(3 if ctx.quick else 5):
        ms, p = _run_cli(["models"], env, ctx.tmp)
        if p.returncode != 0:
            raise RuntimeError(f"agy3d models exited {p.returncode}")
        samples.append(ms)
    return [Case({"runs": len(samples)}, samples, {})]


@bench("import.usd_stage",
       "In a fresh interpreter: perf_counter around `import antigravity3d.kernel.usd_stage` only (pxr + numpy + package imports it pulls in), measured inside the child. extra.bare_interpreter_wall_ms is `python -c pass` subprocess wall time for reference.",
       "Interpreter start-up (reported separately). First-call lazy costs (UsdValidation registry load, etc.) are not included.")
def i_import(ctx: Ctx) -> list[Case]:
    env = _clean_env(ctx.dir("clistate"))
    code = "import time;t=time.perf_counter();import antigravity3d.kernel.usd_stage;print((time.perf_counter()-t)*1e3)"
    samples, bare = [], []
    for _ in range(3 if ctx.quick else 5):
        p = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120, cwd=ctx.tmp)
        if p.returncode != 0:
            raise RuntimeError(f"import failed: {p.stderr[-160:]!r}")
        samples.append(float(p.stdout.strip().splitlines()[-1]))
        t0 = time.perf_counter()
        subprocess.run([sys.executable, "-c", "pass"], env=env, capture_output=True, timeout=60, cwd=ctx.tmp)
        bare.append((time.perf_counter() - t0) * 1e3)
    return [Case({"module": "antigravity3d.kernel.usd_stage", "runs": len(samples)}, samples,
                 {"bare_interpreter_wall_ms": _r(statistics.median(bare))})]


# ---------------------------------------------------------------------------------------------------------
# J. Throughput
# ---------------------------------------------------------------------------------------------------------
def _worker_init() -> None:
    _load()
    import antigravity3d.dags.prop_to_engine  # noqa: F401  (import cost paid before the clock starts)
    import antigravity3d.tactics.authoring  # noqa: F401


def _worker_noop(_i: int) -> int:
    time.sleep(0.3)
    return os.getpid()


def _worker_prop(args: tuple[str, int, str]) -> dict[str, Any]:
    prompt, seed, out = args
    t0 = time.perf_counter()
    r = run_prop_once(prompt, Path(out), single=False, memo={}, seed=seed)
    return {"ms": (time.perf_counter() - t0) * 1e3, "preview_gates_ok": r["preview_gates_ok"], "dag_ok": r["dag_ok"],
            "pid": os.getpid()}


@bench("throughput.prop_offline",
       "K distinct offline prop runs (PropToEngineDAG, governed mode, unreal_5; prompt/author seed vary per job, each job has its own empty cache dir) executed by P spawned worker processes (ProcessPoolExecutor, spawn). One timed sample per P: wall time from submitting the K jobs to the last result.",
       "Worker start-up and module imports (workers are started and warmed first). This is the deterministic local portion with fixture images and fixture judges, not a live throughput figure: a live run waits on APIs and would overlap differently. 4 vCPUs only, so P=4 contends with the parent and anything else on the box.")
def j_throughput(ctx: Ctx) -> list[Case]:
    import concurrent.futures as cf
    import multiprocessing as mp

    k = 4 if ctx.quick else 12
    ps = [1, 2] if ctx.quick else [1, 2, 4]
    adjectives = ["old", "weathered", "small", "large", "painted", "plain"]
    names = list(PROMPTS.values())
    jobs = [(f"{adjectives[i % len(adjectives)]} {names[i % len(names)]}", 100 + i) for i in range(k)]
    cases, t1 = [], None
    for p in ps:
        base = ctx.dir("tp")
        with cf.ProcessPoolExecutor(max_workers=p, mp_context=mp.get_context("spawn"), initializer=_worker_init) as ex:
            list(ex.map(_worker_noop, range(p)))  # force all p workers up and initialised
            t0 = time.perf_counter()
            res = list(ex.map(_worker_prop, [(pr, seed, str(base / f"job{i}")) for i, (pr, seed) in enumerate(jobs)]))
            wall = (time.perf_counter() - t0) * 1e3
        shutil.rmtree(base, ignore_errors=True)
        t1 = t1 or wall
        speed = t1 / wall
        cases.append(Case({"jobs_K": k, "worker_processes_P": p, "start_method": "spawn", "threads_per_process": 1},
                          [wall], {"assets_per_hour": _r(k / (wall / 1e3) * 3600), "speedup_vs_P1": _r(speed),
                                   "scaling_efficiency": _r(speed / p), "per_job_ms_median": _r(statistics.median(r["ms"] for r in res)),
                                   "distinct_worker_pids": len({r["pid"] for r in res}),
                                   "all_preview_gates_ok": all(r["preview_gates_ok"] for r in res),
                                   "parent_cpu_count": os.cpu_count(), "design_doc_claim": "120+ assets/hour on 8 workers (not comparable: 4 vCPUs, fixtures)"}))
    return cases


# ---------------------------------------------------------------------------------------------------------
# K. Batch sanitize
# ---------------------------------------------------------------------------------------------------------
def make_sanitize_inputs(out: Path, count: int) -> list[Path]:
    """`count` meshes (~200 to ~20k tris) in .ply/.obj; some deliberately defective. Returns paths in order."""
    import trimesh

    out.mkdir(parents=True, exist_ok=True)
    targets = [int(x) for x in np.geomspace(200, 20000, count)]
    defects = {2: "dup_verts", 6: "flipped", 9: "floating", 12: "dup_verts", 15: "flipped_half", 18: "floating", 21: "dup_verts"}
    files = []
    for i, tgt in enumerate(targets):
        shape = ("sphere", "box", "cylinder", "capsule", "torus", "ushape")[i % 6]
        if shape == "box":
            m = trimesh.creation.box(extents=(0.8, 0.6, 0.5))
            while len(m.faces) * 4 <= tgt:
                m = m.subdivide()
        elif shape == "cylinder":
            s = max(4, tgt // 4)
            m = trimesh.creation.cylinder(radius=0.3, height=0.9, sections=s)
        elif shape == "capsule":
            b = max(3, int(math.sqrt(tgt / 2)))
            m = trimesh.creation.capsule(height=0.5, radius=0.25, count=[b, b * 2])
        elif shape == "torus":
            a = max(3, int(math.sqrt(tgt / 2)))
            m = trimesh.creation.torus(major_radius=0.5, minor_radius=0.15, major_sections=a, minor_sections=a)
        elif shape == "ushape":
            u = u_shape_mesh(tgt)
            m = trimesh.Trimesh(u.points, u.triangles, process=False)
        else:
            b = max(3, int(math.sqrt(tgt / 2)))
            m = trimesh.creation.uv_sphere(radius=0.5, count=[2 * b, b])
        m = trimesh.Trimesh(np.asarray(m.vertices), np.asarray(m.faces), process=False)
        m.apply_translation([0, 0, -m.bounds[0][2]])  # rests on z = 0
        kind = defects.get(i)
        if kind == "dup_verts":
            m = trimesh.Trimesh(m.vertices[m.faces.reshape(-1)], np.arange(len(m.faces) * 3).reshape(-1, 3), process=False)
        elif kind == "flipped":
            m = trimesh.Trimesh(m.vertices, m.faces[:, ::-1], process=False)
        elif kind == "flipped_half":
            f = m.faces.copy()
            f[::2] = f[::2, ::-1]
            m = trimesh.Trimesh(m.vertices, f, process=False)
        elif kind == "floating":
            m.apply_translation([0, 0, 0.5])
        ext = "ply" if i % 2 == 0 else "obj"
        p = out / f"mesh_{i:02d}_{shape}{'_' + kind if kind else ''}.{ext}"
        m.export(p)
        files.append(p)
    return files


@bench("dag.batch_sanitize",
       "BatchSanitizeDAG.run over generated meshes (load, Tier-1 before, shape-preserving repair, Tier-1 after, UV+LOD+collider export pipeline, USD+GLB write, UsdValidation + engine compliance) with max_parallel=jobs, unreal_5 default budget. One timed sample per jobs value: wall time of the whole batch.",
       "Mesh generation (untimed). The DAG parallelises with asyncio.to_thread, i.e. threads in one process, not processes. Meshes above 15,000 triangles exceed the default unreal_5 tri budget and are reported 'needs_attention'; that is a property of the inputs, listed in extra.status_counts / extra.attention_reasons.")
def k_sanitize(ctx: Ctx) -> list[Case]:
    from antigravity3d.dags.batch_sanitize import BatchSanitizeDAG

    count = 8 if ctx.quick else 24
    files = make_sanitize_inputs(ctx.dir("sanin"), count)
    tris_in = {}
    cases = []
    for jobs in (1, 2, 4):
        out = ctx.dir("sanout")

        async def go():
            t0 = time.perf_counter()
            rep = await BatchSanitizeDAG(workdir=out, budget=EngineBudget("unreal_5"), max_parallel=jobs).run(files)
            return rep, (time.perf_counter() - t0) * 1e3
        rep, wall = asyncio.run(go())
        rows = rep.extras["rows"]
        per_mesh = [sum(r["ms"].values()) for r in rows if "ms" in r]
        reasons: dict[str, int] = {}
        for r in rows:
            for tok in (r.get("issues_remaining") or []) + (r.get("validate_critical") or []) + (r.get("compliance_critical") or []):
                reasons[tok.split("@")[0]] = reasons.get(tok.split("@")[0], 0) + 1
        for r in rows:
            tris_in[Path(r["file"]).name] = r.get("tris_before")
        status: dict[str, int] = {}
        for r in rows:
            status[r["status"]] = status.get(r["status"], 0) + 1
        cases.append(Case({"meshes": count, "jobs": jobs, "engine": "unreal_5", "max_tris": EngineBudget("unreal_5").max_tris}, [wall], {
            "meshes_per_minute": _r(count / (wall / 1e3) * 60),
            "per_mesh_median_ms": _r(statistics.median(per_mesh)) if per_mesh else None,
            "per_mesh_sum_of_stage_ms_note": "each mesh's own load+repair+export+validate stage times; overlap when jobs>1",
            "gates_passed_ok_rows": status.get("ok", 0), "status_counts": status, "all_gates_ok": bool(rep.extras["gates_ok"]),
            "attention_reasons": reasons, "input_tris_min_max": [min(v for v in tris_in.values() if v), max(v for v in tris_in.values() if v)]}))
        shutil.rmtree(out, ignore_errors=True)
    return cases


# ---------------------------------------------------------------------------------------------------------
# Machine block, runner, CLI
# ---------------------------------------------------------------------------------------------------------
def _sh(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def machine_block(quick: bool) -> dict[str, Any]:
    import importlib.metadata as md

    cpu = next((ln.split(":", 1)[1].strip() for ln in Path("/proc/cpuinfo").read_text().splitlines() if ln.startswith("model name")), platform.processor())
    mem_kb = next((int(ln.split()[1]) for ln in Path("/proc/meminfo").read_text().splitlines() if ln.startswith("MemTotal")), 0)

    def ver(d: str) -> str | None:
        try:
            return md.version(d)
        except md.PackageNotFoundError:
            return None
    return {
        "cpu_model": cpu, "logical_cpus": os.cpu_count(), "ram_gb": round(mem_kb / 1048576, 1),
        "os": f"{platform.system()} {platform.release()}", "python": sys.version.split()[0],
        "git_commit": _sh(["git", "rev-parse", "HEAD"]), "git_dirty_tracked_files": len(_sh(["git", "status", "--porcelain", "--untracked-files=no"]).splitlines()),
        "versions": {d: ver(d) for d in ("antigravity-3d", "usd-core", "trimesh", "xatlas", "meshoptimizer", "coacd", "numpy", "scipy", "pillow")},
        "thread_env": {k: os.environ.get(k) for k in THREAD_ENV},
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "quick": quick,
        "loadavg_at_start": [round(x, 2) for x in os.getloadavg()],
        "offline": {"GEMINI_API_KEY_set": "GEMINI_API_KEY" in os.environ, "AGY3D_OFFLINE": os.environ.get("AGY3D_OFFLINE")},
    }


def _select(only: list[str] | None) -> list[str]:
    if not only:
        return list(REGISTRY)
    sel = []
    for pat in only:
        hits = [n for n in REGISTRY if n == pat or n.startswith(pat.rstrip(".") + ".")]
        if not hits:
            raise SystemExit(f"--only {pat!r}: no such benchmark (use --list)")
        sel += [h for h in hits if h not in sel]
    return sel


def _write(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=1))
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(REPO / "benchmarks/results/offline.json"))
    ap.add_argument("--quick", action="store_true", help="fewer iterations / smaller sizes (< ~2 min)")
    ap.add_argument("--only", nargs="+", metavar="NAME", help="run only these benchmarks (exact name or dotted prefix)")
    ap.add_argument("--list", action="store_true", help="list benchmark names and exit")
    a = ap.parse_args(argv)
    if a.list:
        for n, b in REGISTRY.items():
            print(f"{n:<48} {b.includes[:90]}")
        return 0
    names = _select(a.only)
    _load()
    logging.getLogger().setLevel(logging.ERROR)
    out = Path(a.out)
    t_start = time.perf_counter()
    doc: dict[str, Any] = {"machine": machine_block(a.quick), "quick": a.quick, "benchmarks": {}}
    with tempfile.TemporaryDirectory(prefix="agy3d-perf-") as td:
        tmp = Path(td)
        os.environ["AGY3D_STATE_DIR"] = str(tmp / "state")  # QA/fixlog state must not land in the repo or cwd
        ctx = Ctx(a.quick, tmp)
        for name in names:
            b = REGISTRY[name]
            t0 = time.perf_counter()
            entry: dict[str, Any] = {"includes": b.includes, "excludes": b.excludes}
            try:
                cases = b.fn(ctx)
                entry.update(status="ok", cases=[c.to_json() for c in cases])
            except Exception as e:  # noqa: BLE001 - a failing benchmark must not abort the run
                msg = f"{type(e).__name__}: {e}".replace("\n", " ")[:300]
                entry.update(status="error", error=msg, cases=[])
                print(f"[{name}] ERROR {msg}", file=sys.stderr)
                if os.environ.get("PERF_TRACE"):
                    traceback.print_exc()
            entry["bench_wall_s"] = round(time.perf_counter() - t0, 2)
            doc["benchmarks"][name] = entry
            ok = entry["status"] == "ok"
            med = entry["cases"][0]["median_ms"] if ok and entry["cases"] else None
            print(f"[{name}] {entry['status']} {entry['bench_wall_s']}s" + (f" first-case median {med} ms" if med is not None else ""), flush=True)
            _write(out, doc)  # partial results survive a later native crash
            gc.collect()
    doc["machine"]["loadavg_at_end"] = [round(x, 2) for x in os.getloadavg()]
    doc["total_wall_s"] = round(time.perf_counter() - t_start, 1)
    _write(out, doc)
    bad = [n for n, e in doc["benchmarks"].items() if e["status"] != "ok"]
    print(f"done in {doc['total_wall_s']} s; {len(names) - len(bad)}/{len(names)} ok" + (f"; failed: {', '.join(bad)}" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
