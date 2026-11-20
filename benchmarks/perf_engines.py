#!/usr/bin/env python3
"""Timing of the Blender worker pool, Godot's headless import, and Blender-versus-kernel geometry operations.

    xvfb-run -a python benchmarks/perf_engines.py --out /tmp/engine-bench.json [--markdown FILE] [--quick] [--fake]

This is the CI half of the benchmark suite: it needs real binaries ($BLENDER_BIN, $GODOT_BIN) and measures
them. Without them it says what is missing, writes a JSON that records that, and exits 0. ``--fake`` runs
every code path against the worker pool's fake daemon (no bpy), so the plumbing can be tested anywhere; its
numbers are not Blender numbers and the JSON says so (``mode: "fake"``).

What is measured, each in its own try/except (an error is recorded as text, the run goes on):
  * the worker pool: cold start to the first answered ping, warm ping, ``import_usd`` of written packages,
    ``render_usd`` with the engines and sizes the pipeline requests (512 px, 16 samples, transparent);
  * the same geometry operations done by Blender and by this project's kernels on the same meshes in the same
    process on the same machine: UV unwrap, LOD decimation to the kernel's triangle counts, convex hull, and a
    4-view clay sheet. Both sides record what they produced; ``equivalence`` says where the work differs;
  * Godot's headless import of three written packages, the way the engine validator does it.

Blender scripts run through the pool's ``run_script`` (they pass the linter, which is also checked in
``--fake`` mode). Blender-side times are taken inside Blender around the operator only (mesh construction and
the socket round trip are recorded separately), which is the favourable side for Blender.

Output: the ``--out`` JSON, and one stdout line ``ENGINE_BENCH_JSON <compact json>`` for reading from a job log.
Exit status: 0 whenever the benchmark ran to the end, including when binaries or single measurements are
unavailable; 1 only if the benchmark itself crashed (the JSON written so far, with ``fatal``, is still saved).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

SCHEMA = 1
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:  # quality_facts lives next to this file; antigravity3d comes from the installed wheel
    sys.path.insert(0, str(HERE))

PROMPTS = {"crate": "wooden crate with iron brackets", "barrel": "rusty metal barrel"}
VIEWS = ("front", "right", "top", "persp")
SHEET_PX = 256
RENDER_REQUESTS = (("workbench", 3), ("eevee", 3), ("cycles", 2))  # what render_usd callers ask for: 512 px, 16 samples, transparent

EQUIVALENCE = {
    "uv_unwrap": "NOT the same algorithm. Kernel: xatlas chart computation + packing at a requested texel density + stretch statistics "
                 "(analytic charts disabled here so both sides compute charts). Blender smart_project: angle-based projection charts + island packing, no density fit. "
                 "Islands and total UV area are recorded for both; compare them before reading the ratio.",
    "uv_unwrap_angle_based": "NOT the same algorithm. bpy.ops.uv.unwrap(ANGLE_BASED) needs seams; seams are marked on edges sharper than 40 degrees, so a smooth sphere "
                             "gets none and unwraps as one chart (check islands). Cheap to run, weak as a comparison.",
    "lod": "Different algorithms, same targets. Kernel: meshoptimizer simplify per level + cache/overdraw/fetch optimisation + measured surface deviation per level. "
           "Blender: Decimate (collapse) from the original mesh to each level's triangle count, applied, no deviation measurement. Triangle counts reached are recorded for both.",
    "convex_hull": "Roughly the same work. Kernel: generate_colliders per part (topology check, scipy ConvexHull, fast path; CoACD only for concave parts). "
                   "Blender: bmesh.ops.convex_hull on the part's vertices. Hull vertex counts recorded; the kernel also computes volume and a collider mesh.",
    "sheet_4_views": "Different renderers. Kernel: the software rasteriser's clay sheet (preview_sheet, 4 views, 256 px tiles, labelled). "
                     "Blender: Workbench engine (studio lighting, flat colour), 4 orthographic 256 px renders written to PNG. Foreground fraction recorded per view.",
}

# ------------------------------------------------------------------------------------------------ small helpers


def _r(x: float, n: int = 2) -> float:
    return round(float(x), n)


def stats(samples_ms: list[float]) -> dict[str, Any]:
    s = [float(x) for x in samples_ms]
    if not s:
        return {"n": 0, "median_ms": None}
    return {"n": len(s), "median_ms": _r(statistics.median(s)), "min_ms": _r(min(s)), "max_ms": _r(max(s)), "samples_ms": [_r(x, 1) for x in s]}


class Run:
    """Holds the result document, the time budget and the error list."""

    def __init__(self, budget_s: float, fake: bool, quick: bool):
        self.t0 = time.monotonic()
        self.budget_s, self.fake, self.quick = budget_s, fake, quick
        self.errors: list[str] = []
        self.doc: dict[str, Any] = {}

    def elapsed(self) -> float:
        return time.monotonic() - self.t0

    def over(self, share: float) -> bool:
        return self.elapsed() > self.budget_s * share

    def record_error(self, path: str, e: BaseException) -> str:
        msg = f"{type(e).__name__}: {e}".replace("\n", " ")[:600]
        self.errors.append(f"{path}: {msg}")
        if os.environ.get("PERF_TRACE"):
            traceback.print_exc()
        return msg

    def attempt(self, store: dict, key: str, fn: Callable[[], dict], *, share: float = 1.0) -> Any:
        """Runs one measurement; an exception becomes ``{status: error, error: text}`` and never propagates."""
        if self.over(share):
            store[key] = {"status": "skipped", "reason": f"time budget ({self.budget_s:.0f}s) used up"}
            return None
        t0 = time.perf_counter()
        try:
            out = fn()
            out.setdefault("status", "ok")
        except Exception as e:  # noqa: BLE001 - a failed measurement must not abort the rest
            out = {"status": "error", "error": self.record_error(key, e)}
        out["wall_s"] = _r(time.perf_counter() - t0, 2)
        store[key] = out
        return out

    async def aattempt(self, store: dict, key: str, fn: Callable[[], Any], *, share: float = 1.0) -> Any:
        if self.over(share):
            store[key] = {"status": "skipped", "reason": f"time budget ({self.budget_s:.0f}s) used up"}
            return None
        t0 = time.perf_counter()
        try:
            out = await fn()
            out.setdefault("status", "ok")
        except Exception as e:  # noqa: BLE001
            out = {"status": "error", "error": self.record_error(key, e)}
        out["wall_s"] = _r(time.perf_counter() - t0, 2)
        store[key] = out
        return out


def cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def machine_info() -> dict[str, Any]:
    try:
        usable = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        usable = None
    return {"cpu_model": cpu_model(), "cpu_count": os.cpu_count(), "cpus_usable": usable, "platform": platform.platform(),
            "python": platform.python_version(), "loadavg_at_start": [round(x, 2) for x in os.getloadavg()] if hasattr(os, "getloadavg") else None,
            "omp_num_threads": os.environ.get("OMP_NUM_THREADS"), "libgl_software": os.environ.get("LIBGL_ALWAYS_SOFTWARE"),
            "display": os.environ.get("DISPLAY")}


def ci_info() -> dict[str, Any]:
    keys = {"sha": "GITHUB_SHA", "run_id": "GITHUB_RUN_ID", "ref": "GITHUB_REF", "runner": "RUNNER_NAME", "image": "ImageOS",
            "tests_outcome": "AGY_BENCH_TESTS_OUTCOME"}
    return {k: os.environ[v] for k, v in keys.items() if os.environ.get(v)}


def tool_status(env_var: str) -> dict[str, Any]:
    value = os.environ.get(env_var)
    if not value:
        return {"available": False, "missing": f"${env_var} is not set"}
    if not (os.path.isfile(value) and os.access(value, os.X_OK)):
        return {"available": False, "missing": f"${env_var}={value} is not an executable file", "path": value}
    return {"available": True, "path": value}


def godot_version(exe: str) -> str | None:
    try:
        p = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        return (p.stdout or p.stderr).strip().splitlines()[-1] if (p.stdout or p.stderr).strip() else None
    except Exception as e:  # noqa: BLE001
        return f"unknown ({type(e).__name__}: {e})"[:200]


# ------------------------------------------------------------------------------------------------ subjects

def island_count(keys_per_face: list[list[tuple]]) -> int:
    """UV islands: faces that share a (vertex, u, v) corner are one island (same rule as the Blender-side script)."""
    parent = list(range(len(keys_per_face)))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    seen: dict[tuple, int] = {}
    for f, keys in enumerate(keys_per_face):
        for k in keys:
            if k in seen:
                ra, rb = find(seen[k]), find(f)
                if ra != rb:
                    parent[ra] = rb
            else:
                seen[k] = f
    return len({find(i) for i in range(len(keys_per_face))})


def uv_stats_kernel(u: Any) -> dict[str, Any]:
    import numpy as np

    uv = np.asarray(u.uvs, np.float64)
    tri = np.asarray(u.triangles)
    orig = np.asarray(u.vmapping)
    keys = [[(int(orig[v]), round(float(uv[v, 0]), 5), round(float(uv[v, 1]), 5)) for v in t] for t in tri]
    a, b, c = uv[tri[:, 0]], uv[tri[:, 1]], uv[tri[:, 2]]
    area = float(np.abs((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (c[:, 0] - a[:, 0]) * (b[:, 1] - a[:, 1])).sum() / 2)
    return {"islands": island_count(keys), "uv_area": area, "tris": int(len(tri)), "chart_source": u.chart_source}


def build_subjects(workdir: Path, quick: bool) -> dict[str, Any]:
    """Meshes (stage-extracted parts, in world space) and written packages for the three package subjects."""
    import numpy as np  # noqa: F401
    import quality_facts

    from antigravity3d.ir import MaterialIR, NodeIR, SceneIR, Transform
    from antigravity3d.kernel import fast_ops
    from antigravity3d.kernel.package import write_engine_package
    from antigravity3d.kernel.usd_stage import StageManager
    from antigravity3d.state import EngineBudget
    from antigravity3d.tactics.authoring import TemplateAuthor

    budget = EngineBudget("unreal_5")
    qa = {"tier1_ok": True, "tier15": 3.6, "defects": {}, "audit": []}
    scenes: dict[str, tuple[Any, str]] = {}
    for name, prompt in PROMPTS.items():
        scenes[name] = (asyncio.run(TemplateAuthor().propose(prompt, budget=budget, n=1, context={"asset_name": name.capitalize()}))[0], name.capitalize())
    ball = SceneIR(asset_name="Ball", materials=[MaterialIR(id="m0")],
                   nodes=[NodeIR(id="ball", kind="sphere", params={"radius": 0.5, "segments": 72, "rings": 36}, material_id="m0",
                                 transform=Transform(translate=(0.0, 0.0, 0.5)))])
    scenes["sphere_ir"] = (ball, "Ball")
    subjects: dict[str, Any] = {}
    for name, (scene, asset) in scenes.items():
        d = workdir / name
        d.mkdir(parents=True)
        sm = StageManager.create(d / f"{asset}.usda", asset)
        sm.apply_ir(scene)
        sm.commit_all()
        meshes = sm.extract_meshes()
        bundle = fast_ops.run_export_pipeline(meshes, budget, asset)
        man = write_engine_package(sm, bundle, budget, d / "package", asset_name=asset, qa=qa)
        subjects[name] = {"asset": asset, "meshes": meshes, "package_dir": d / "package", "usd": d / "package" / f"{asset}.usd",
                          "glb": d / "package" / f"{asset}.glb", "files": man.files, "tris": int(sum(len(m.triangles) for m in meshes)), "package_ok": man.ok}
    comparisons = {n: subjects[n]["meshes"] for n in ("crate", "barrel")}
    for target in ((2000,) if quick else (2000, 10000)):
        m = quality_facts.sphere(target)
        comparisons[f"sphere_{len(m.triangles)}"] = [m]
    return {"packages": subjects, "comparison_meshes": comparisons}


def parts_payload(meshes: list[Any]) -> list[dict[str, Any]]:
    import numpy as np

    return [{"v": np.round(np.asarray(m.points, np.float64), 6).tolist(), "f": np.asarray(m.triangles, np.int64).tolist()} for m in meshes]


# ------------------------------------------------------------------------------------------------ Blender scripts

PRELUDE = '''
import bpy, bmesh, math, time
PARTS = @@PARTS@@
REPS = @@REPS@@

def make(part, name="agy_part"):
    me = bpy.data.meshes.new(name)
    me.from_pydata(part["v"], [], part["f"])
    me.update()
    ob = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(ob)
    return ob

def drop(ob):
    me = ob.data
    bpy.data.objects.remove(ob, do_unlink=True)
    bpy.data.meshes.remove(me)

def activate(ob):
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)

def tri_count(ob):
    return sum(len(p.vertices) - 2 for p in ob.data.polygons)
'''

UV_SCRIPT = '''
OP = "@@OP@@"

def uv_stats(ob):
    me = ob.data
    layer = me.uv_layers.active
    if layer is None:
        return 0, 0.0
    parent = list(range(len(me.polygons)))
    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    seen = {}
    area = 0.0
    for p in me.polygons:
        pts = []
        for li in p.loop_indices:
            uv = layer.data[li].uv
            pts.append((uv[0], uv[1]))
            key = (me.loops[li].vertex_index, round(uv[0], 5), round(uv[1], 5))
            if key in seen:
                ra, rb = find(seen[key]), find(p.index)
                if ra != rb:
                    parent[ra] = rb
            else:
                seen[key] = p.index
        s = 0.0
        for i in range(len(pts)):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % len(pts)]
            s += x1 * y2 - x2 * y1
        area += abs(s) / 2
    return len({find(i) for i in range(len(me.polygons))}), area

times = []
build_ms = []
islands = 0
uv_area = 0.0
tris = 0
for rep in range(REPS):
    total = 0.0
    for part in PARTS:
        t_build = time.perf_counter()
        ob = make(part)
        activate(ob)
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_mode(type="EDGE")
        bpy.ops.mesh.select_all(action="DESELECT")
        if OP == "angle":
            bpy.ops.mesh.edges_select_sharp(sharpness=math.radians(40.0))
            bpy.ops.mesh.mark_seam(clear=False)
        bpy.ops.mesh.select_all(action="SELECT")
        build_ms.append((time.perf_counter() - t_build) * 1000)
        t0 = time.perf_counter()
        if OP == "angle":
            bpy.ops.uv.unwrap(method="ANGLE_BASED", margin=0.0)
        else:
            bpy.ops.uv.smart_project(angle_limit=math.radians(66.0), island_margin=0.0)
        total += time.perf_counter() - t0
        bpy.ops.object.mode_set(mode="OBJECT")
        if rep == 0:
            n, a = uv_stats(ob)
            islands += n
            uv_area += a
            tris += tri_count(ob)
        drop(ob)
    times.append(total * 1000)
result = {"times_ms": times, "setup_ms_per_part_median": sorted(build_ms)[len(build_ms) // 2], "islands": islands, "uv_area": uv_area, "tris": tris, "parts": len(PARTS)}
'''

DECIMATE_SCRIPT = '''
TARGETS = @@TARGETS@@
times = []
by_level = {}
for rep in range(REPS):
    total = 0.0
    for part, targets in zip(PARTS, TARGETS):
        for level, target in enumerate(targets):
            ob = make(part)
            activate(ob)
            n0 = tri_count(ob)
            mod = ob.modifiers.new("agy_decimate", "DECIMATE")
            mod.decimate_type = "COLLAPSE"
            mod.ratio = min(1.0, max(0.0, target / max(n0, 1)))
            t0 = time.perf_counter()
            bpy.ops.object.modifier_apply(modifier=mod.name)
            total += time.perf_counter() - t0
            if rep == 0:
                by_level[level + 1] = by_level.get(level + 1, 0) + tri_count(ob)
            drop(ob)
    times.append(total * 1000)
result = {"times_ms": times, "tris_by_level": [by_level[k] for k in sorted(by_level)], "parts": len(PARTS)}
'''

HULL_SCRIPT = '''
times = []
hull_verts = 0
hull_faces = 0
for rep in range(REPS):
    total = 0.0
    for part in PARTS:
        ob = make(part)
        bm = bmesh.new()
        bm.from_mesh(ob.data)
        t0 = time.perf_counter()
        res = bmesh.ops.convex_hull(bm, input=bm.verts, use_existing_faces=False)
        total += time.perf_counter() - t0
        if rep == 0:
            hull_verts += sum(1 for g in res["geom"] if isinstance(g, bmesh.types.BMVert))
            hull_faces += sum(1 for g in res["geom"] if isinstance(g, bmesh.types.BMFace))
        bm.free()
        drop(ob)
    times.append(total * 1000)
result = {"times_ms": times, "hull_vertices": hull_verts, "hull_faces": hull_faces, "parts": len(PARTS)}
'''

SHEET_SCRIPT = '''
from mathutils import Vector
OUT_PREFIX = "@@OUT@@"
SIZE = @@SIZE@@
objs = [make(p, "agy_part_%d" % i) for i, p in enumerate(PARTS)]
sc = bpy.context.scene
sc.render.engine = "BLENDER_WORKBENCH"
sc.render.resolution_x = SIZE
sc.render.resolution_y = SIZE
sc.render.resolution_percentage = 100
sc.render.film_transparent = False
sc.render.image_settings.file_format = "PNG"
lo = [min(v[i] for p in PARTS for v in p["v"]) for i in range(3)]
hi = [max(v[i] for p in PARTS for v in p["v"]) for i in range(3)]
center = Vector(tuple((lo[i] + hi[i]) / 2 for i in range(3)))
diameter = max(math.sqrt(sum((hi[i] - lo[i]) ** 2 for i in range(3))), 0.001)
cam_data = bpy.data.cameras.new("agy_cam")
cam_data.type = "ORTHO"
cam_data.ortho_scale = diameter * 1.1
cam_data.clip_start = diameter * 0.01
cam_data.clip_end = diameter * 5
cam = bpy.data.objects.new("agy_cam", cam_data)
bpy.context.scene.collection.objects.link(cam)
sc.camera = cam
views = [("front", (0.0, -1.0, 0.0)), ("right", (1.0, 0.0, 0.0)), ("top", (0.0, 0.0, 1.0)), ("persp", (1.4, -2.0, 1.2))]
times = []
for rep in range(REPS):
    total = 0.0
    for name, d in views:
        v = Vector(d).normalized()
        cam.location = center + v * diameter * 2
        cam.rotation_euler = (-v).to_track_quat("-Z", "Y").to_euler()
        sc.render.filepath = OUT_PREFIX + name + ".png"
        t0 = time.perf_counter()
        bpy.ops.render.render(write_still=True)
        total += time.perf_counter() - t0
    times.append(total * 1000)
result = {"times_ms": times, "views": [n for n, _ in views], "size": SIZE, "parts": len(PARTS)}
'''


def script(body: str, parts: list[dict], reps: int, **subs: Any) -> str:
    src = PRELUDE + body
    src = src.replace("@@PARTS@@", json.dumps(parts, separators=(",", ":"))).replace("@@REPS@@", str(int(reps)))
    for k, v in subs.items():
        src = src.replace(f"@@{k}@@", v if isinstance(v, str) else json.dumps(v))
    return src


def blender_stats(res: dict[str, Any], roundtrip_ms: float, fake: bool) -> dict[str, Any]:
    """Result of a ``run_script`` call -> a measurement. In fake mode the script is only linted, not executed."""
    if not res.get("executed"):
        return {"status": "ok", "fake": True, "note": "script passed the linter; the fake daemon does not execute it", "roundtrip_ms": _r(roundtrip_ms, 1)}
    r = dict(res["result"])
    times = r.pop("times_ms")
    return {**stats(times), **r, "roundtrip_ms": _r(roundtrip_ms, 1), "timed": "inside Blender, around the operator only"}


# ------------------------------------------------------------------------------------------------ kernel side

def reps_for(tris: int, quick: bool) -> int:
    return 2 if quick else (5 if tris < 5000 else 3)


def time_calls(fn: Callable[[], Any], reps: int) -> tuple[list[float], Any]:
    last = fn()  # warm-up (imports, numba/scipy first use) is not timed
    samples = []
    for _ in range(reps):
        t = time.perf_counter()
        last = fn()
        samples.append((time.perf_counter() - t) * 1000)
    return samples, last


def kernel_uv(meshes: list[Any], reps: int) -> dict[str, Any]:
    from antigravity3d.kernel import fast_ops
    from antigravity3d.state import EngineBudget

    density = EngineBudget("unreal_5").texel_density_px_per_m
    plain = [replace(m, uv_charts=None) for m in meshes]
    samples, last = time_calls(lambda: [fast_ops.unwrap_uvs(m, density) for m in plain], reps)
    per = [uv_stats_kernel(u) for u in last]
    return {**stats(samples), "islands": sum(p["islands"] for p in per), "uv_area": sum(p["uv_area"] for p in per), "tris": sum(p["tris"] for p in per),
            "parts": len(plain), "chart_source": sorted({p["chart_source"] for p in per}), "texel_density_px_per_m": density}


def kernel_lods(meshes: list[Any], reps: int) -> tuple[dict[str, Any], list[list[int]]]:
    from antigravity3d.kernel import fast_ops

    samples, last = time_calls(lambda: [fast_ops.generate_lods(m.points, m.triangles) for m in meshes], reps)
    levels = [[lod.tri_count for lod in chain] for chain in last]
    depth = max(len(c) for c in levels)
    by_level = [sum(c[i] for c in levels if i < len(c)) for i in range(depth)]
    targets = [[t for t in c[1:] if t > 0] for c in levels]
    return {**stats(samples), "tris_by_level": by_level, "parts": len(meshes)}, targets


def kernel_hulls(meshes: list[Any], reps: int) -> dict[str, Any]:
    from antigravity3d.kernel import fast_ops

    samples, last = time_calls(lambda: [fast_ops.generate_colliders([m], 8) for m in meshes], reps)
    hulls = [h for per in last for h in per]
    return {**stats(samples), "hulls": len(hulls), "hull_vertices": int(sum(len(h.points) for h in hulls)), "hull_faces": int(sum(len(h.triangles) for h in hulls)),
            "sources": sorted({h.source for h in hulls}), "parts": len(meshes)}


def fg_fraction(arr: Any) -> float:
    """Fraction of pixels that differ from the top-left pixel (the background); for the uniform Workbench background."""
    import numpy as np

    a = np.asarray(arr)[..., :3].astype(np.int16)
    return float((np.abs(a - a[0, 0]).sum(axis=-1) > 6).mean())


def kernel_sheet(meshes: list[Any], reps: int) -> dict[str, Any]:
    from antigravity3d.kernel.raster import render_buffers
    from antigravity3d.tactics.pyramid import preview_sheet

    samples, _ = time_calls(lambda: preview_sheet(meshes, {}, SHEET_PX), reps)
    covered = [float((render_buffers(meshes, v, SHEET_PX).prim_id >= 0).mean()) for v in VIEWS]
    return {**stats(samples), "views": list(VIEWS), "size": SHEET_PX, "foreground_fraction": [_r(c, 4) for c in covered]}


# ------------------------------------------------------------------------------------------------ Blender section

async def run_script(pool: Any, source: str, timeout: float = 240.0) -> tuple[dict[str, Any], float]:
    t = time.perf_counter()
    res = await pool.call("run_script", source=source, _timeout=timeout)
    return res, (time.perf_counter() - t) * 1000


def ratio(b: dict[str, Any], k: dict[str, Any]) -> float | None:
    bm, km = b.get("median_ms"), k.get("median_ms")
    return _r(bm / km, 1) if bm and km else None


async def blender_section(run: Run, subjects: dict[str, Any], workdir: Path) -> None:
    from antigravity3d.adapters.blender_5_3.pool import BlenderPool

    fake = run.fake
    doc: dict[str, Any] = {}
    run.doc["blender_pool"] = doc
    pool_kw: dict[str, Any] = dict(timeout=300.0, startup_timeout=120.0)

    def new_pool() -> Any:
        return BlenderPool(1, fake=True, **pool_kw) if fake else BlenderPool(1, os.environ["BLENDER_BIN"], **pool_kw)

    # cold start: a fresh pool every time; the last one is kept for the rest of the run
    cold, ping_first = [], []
    pool = None
    for i in range(2):
        candidate = new_pool()
        t = time.perf_counter()
        try:
            await candidate.start()
            await candidate.call("ping")
        except Exception as e:  # noqa: BLE001
            doc["cold_start_to_first_ping"] = {"status": "error", "error": run.record_error("blender_pool.cold_start_to_first_ping", e)}
            await candidate.stop()
            return
        cold.append((time.perf_counter() - t) * 1000)
        if i == 0:
            await candidate.stop()
        else:
            pool = candidate
    doc["cold_start_to_first_ping"] = {"status": "ok", **stats(cold), "includes": "process spawn, Blender start, daemon load (incl. reviewed skills if $AGY3D_BLENDER_SKILLS is set), version handshake, first ping"}
    assert pool is not None
    try:
        await run.aattempt(doc, "version", lambda: _version(pool, run))

        async def warm_ping() -> dict[str, Any]:
            samples = []
            for _ in range(30):
                t = time.perf_counter()
                await pool.call("ping")
                samples.append((time.perf_counter() - t) * 1000)
            return stats(samples)
        await run.aattempt(doc, "warm_ping", warm_ping, share=0.7)

        imports: dict[str, Any] = {}
        doc["import_usd"] = imports
        for name, sub in subjects["packages"].items():
            async def one_import(sub: dict[str, Any] = sub) -> dict[str, Any]:
                samples, info = [], {}
                for _ in range(3):
                    await pool.call("reset_scene")
                    t = time.perf_counter()
                    info = await pool.call("import_usd", path=str(sub["usd"]))
                    samples.append((time.perf_counter() - t) * 1000)
                return {**stats(samples), "package_tris": sub["tris"], "package_bytes": sub["usd"].stat().st_size, "objects": len(info.get("objects", [])),
                        "fake": bool(info.get("fake")), "format": "package .usd (reset_scene between calls, untimed)"}
            await run.aattempt(imports, name, one_import, share=0.7)

        renders: dict[str, Any] = {}
        doc["render_usd"] = renders
        sub = subjects["packages"]["crate"]
        for engine, reps in RENDER_REQUESTS:
            async def one_render(engine: str = engine, reps: int = reps) -> dict[str, Any]:
                samples, shape = [], None
                for _ in range(2 if run.quick else reps):
                    t = time.perf_counter()
                    r = await pool.call("render_usd", path=str(sub["usd"]), engine=engine, width=512, height=512, samples=16, transparent=True)
                    samples.append((time.perf_counter() - t) * 1000)
                    shape = list(getattr(r.get("image"), "shape", [])) or None
                return {**stats(samples), "first_call_ms": _r(samples[0], 1), "engine": engine, "width": 512, "height": 512, "samples": 16, "transparent": True,
                        "image_shape": shape, "scene": "crate package", "note": "includes import_usd + frame set-up + render + EXR round trip, as render_usd does in the pipeline"}
            await run.aattempt(renders, engine, one_render, share=0.7)

        comps: list[dict[str, Any]] = []
        doc["comparisons"] = comps
        for label, meshes in subjects["comparison_meshes"].items():
            entry = await compare_mesh(run, pool, label, meshes, workdir)
            comps.append(entry)
    finally:
        await pool.stop()


async def _version(pool: Any, run: Run) -> dict[str, Any]:
    v = await pool.call("version")
    run.doc.setdefault("versions", {})["blender"] = v.get("blender")
    run.doc["versions"]["blender_python"] = v.get("python")
    return {"daemon": v.get("daemon"), "blender": v.get("blender"), "blender_version": v.get("blender_version"), "python": v.get("python"), "fake": v.get("fake")}


async def compare_mesh(run: Run, pool: Any, label: str, meshes: list[Any], workdir: Path) -> dict[str, Any]:
    tris = int(sum(len(m.triangles) for m in meshes))
    reps = reps_for(tris, run.quick)
    entry: dict[str, Any] = {"mesh": label, "parts": len(meshes), "tris": tris, "verts": int(sum(len(m.points) for m in meshes)), "reps": reps, "ops": {}}
    ops = entry["ops"]
    parts = parts_payload(meshes)

    async def pair(key: str, kernel: Callable[[], dict], blender_source: str, timeout: float = 240.0) -> None:
        slot: dict[str, Any] = {"equivalence": key}
        ops[key] = slot
        run.attempt(slot, "kernel", kernel, share=0.7)

        async def blender() -> dict[str, Any]:
            await pool.call("reset_scene")
            res, rt = await run_script(pool, blender_source, timeout)
            return blender_stats(res, rt, run.fake)
        await run.aattempt(slot, "blender", blender, share=0.7)
        if slot.get("kernel", {}).get("status") == "ok" and slot.get("blender", {}).get("status") == "ok":
            slot["blender_over_kernel"] = ratio(slot["blender"], slot["kernel"])

    await pair("uv_unwrap", lambda: kernel_uv(meshes, reps), script(UV_SCRIPT, parts, reps, OP="smart"))
    # the angle-based operator has the same kernel counterpart; only Blender is run again
    slot = {"equivalence": "uv_unwrap_angle_based", "kernel": {"status": "see uv_unwrap.kernel"}}
    ops["uv_unwrap_angle_based"] = slot

    async def angle() -> dict[str, Any]:
        await pool.call("reset_scene")
        res, rt = await run_script(pool, script(UV_SCRIPT, parts, reps, OP="angle"))
        return blender_stats(res, rt, run.fake)
    await run.aattempt(slot, "blender", angle, share=0.7)
    if slot["blender"].get("median_ms") and ops["uv_unwrap"].get("kernel", {}).get("median_ms"):
        slot["blender_over_kernel"] = ratio(slot["blender"], ops["uv_unwrap"]["kernel"])

    targets: list[list[int]] = []

    def lod_kernel() -> dict[str, Any]:
        out, tg = kernel_lods(meshes, reps)
        targets.extend(tg)
        return out
    lod_slot: dict[str, Any] = {"equivalence": "lod"}
    ops["lod"] = lod_slot
    run.attempt(lod_slot, "kernel", lod_kernel, share=0.7)
    if targets:
        async def decimate() -> dict[str, Any]:
            await pool.call("reset_scene")
            res, rt = await run_script(pool, script(DECIMATE_SCRIPT, parts, reps, TARGETS=targets))
            return {**blender_stats(res, rt, run.fake), "targets_tris_by_level": [sum(t[i] for t in targets if i < len(t)) for i in range(max(len(t) for t in targets))]}
        await run.aattempt(lod_slot, "blender", decimate, share=0.7)
        if lod_slot.get("kernel", {}).get("status") == "ok" and lod_slot.get("blender", {}).get("status") == "ok":
            lod_slot["blender_over_kernel"] = ratio(lod_slot["blender"], lod_slot["kernel"])
    else:
        lod_slot["blender"] = {"status": "skipped", "reason": "the kernel produced no LOD target (kernel failed or every part is already minimal)"}

    await pair("convex_hull", lambda: kernel_hulls(meshes, reps), script(HULL_SCRIPT, parts, reps))

    out_dir = workdir / f"sheet_{label}"
    out_dir.mkdir(parents=True, exist_ok=True)
    sheet_slot: dict[str, Any] = {"equivalence": "sheet_4_views"}
    ops["sheet_4_views"] = sheet_slot
    run.attempt(sheet_slot, "kernel", lambda: kernel_sheet(meshes, reps), share=0.7)

    async def sheet() -> dict[str, Any]:
        await pool.call("reset_scene")
        res, rt = await run_script(pool, script(SHEET_SCRIPT, parts, min(reps, 3), OUT=f"{out_dir}/", SIZE=SHEET_PX))
        out = blender_stats(res, rt, run.fake)
        if not run.fake:
            from PIL import Image
            import numpy as np
            out["foreground_fraction"] = [_r(fg_fraction(np.asarray(Image.open(out_dir / f"{v}.png").convert("RGB"))), 4) for v in VIEWS]
        return out
    await run.aattempt(sheet_slot, "blender", sheet, share=0.7)
    if sheet_slot.get("kernel", {}).get("status") == "ok" and sheet_slot.get("blender", {}).get("status") == "ok":
        sheet_slot["blender_over_kernel"] = ratio(sheet_slot["blender"], sheet_slot["kernel"])
    return entry


# ------------------------------------------------------------------------------------------------ Godot section

def godot_section(run: Run, subjects: dict[str, Any], godot: dict[str, Any]) -> None:
    from antigravity3d.adapters.engines.validators import GodotValidator

    doc: dict[str, Any] = {}
    run.doc["godot"] = doc
    exe = godot["path"]
    run.doc.setdefault("versions", {})["godot"] = godot_version(exe)

    def baseline() -> dict[str, Any]:
        samples = []
        with tempfile.TemporaryDirectory(prefix="agy3d-godot-bench-") as td:
            Path(td, "project.godot").write_text('config_version=5\n[application]\nconfig/name="agy3d_bench"\n')
            for _ in range(3):
                t = time.perf_counter()
                p = subprocess.run([exe, "--headless", "--path", td, "--quit"], capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
                samples.append((time.perf_counter() - t) * 1000)
                if p.returncode != 0:
                    raise RuntimeError(f"godot --headless --quit exited {p.returncode}: {(p.stdout + p.stderr)[-300:]}")
        return {**stats(samples), "what": "engine start and exit on an empty project, no import: the fixed cost inside every import time below"}
    run.attempt(doc, "empty_project_start", baseline, share=1.0)

    imports: dict[str, Any] = {}
    doc["import"] = imports
    validator = GodotValidator()
    for name, sub in subjects["packages"].items():
        def one(sub: dict[str, Any] = sub) -> dict[str, Any]:
            samples, report = [], None
            for _ in range(3):
                t = time.perf_counter()
                report = asyncio.run(validator.validate(sub["glb"], timeout=120))
                samples.append((time.perf_counter() - t) * 1000)
            if not report.ok:
                raise RuntimeError(f"Godot rejected {sub['glb'].name}: {report.skipped_reason or report.log_tail[-300:]}")
            return {**stats(samples), "package_tris": sub["tris"], "godot_triangles": (report.metrics or {}).get("triangles"), "glb_bytes": sub["glb"].stat().st_size,
                    "what": "GodotValidator.validate: process start + glTF load + scene generation + mesh walk (the package check CI and the pipeline run)"}
        run.attempt(imports, name, one, share=1.0)

    def capture() -> dict[str, Any]:
        sub, samples = subjects["packages"]["crate"], []
        with tempfile.TemporaryDirectory(prefix="agy3d-godot-cap-") as td:
            for _ in range(2):
                png = Path(td) / "frame.png"
                t = time.perf_counter()
                report = asyncio.run(validator.capture(sub["glb"], png, timeout=120))
                samples.append((time.perf_counter() - t) * 1000)
                if not report.ok or not png.is_file():
                    raise RuntimeError(f"Godot capture failed: {report.skipped_reason or report.log_tail[-300:]}")
        return {**stats(samples), "what": "GodotValidator.capture (gl_compatibility, software GL, 512 px), crate package"}
    run.attempt(doc, "capture_crate", capture, share=1.0)


# ------------------------------------------------------------------------------------------------ report

def markdown(doc: dict[str, Any]) -> str:
    def ms(x: Any) -> str:
        return "n/a" if x is None else f"{x:,.1f}"

    def cell(d: Any) -> str:
        if not isinstance(d, dict):
            return "n/a"
        if d.get("status") == "ok":
            return ms(d.get("median_ms")) + ("" if d.get("median_ms") is not None else " (fake)")
        return f"{d.get('status')}"
    lines = ["## Engine benchmark", ""]
    mode = doc.get("mode")
    m = doc.get("machine", {})
    v = doc.get("versions", {})
    lines.append(f"Mode `{mode}`. Blender `{v.get('blender')}`, Godot `{v.get('godot')}`. {m.get('cpu_model')} ({m.get('cpus_usable') or m.get('cpu_count')} usable CPUs).")
    if doc.get("fatal"):
        lines += ["", f"**The benchmark crashed:** `{doc['fatal']}`. Results below are partial."]
    if mode == "unavailable":
        lines += ["", "Nothing measured: " + "; ".join(doc.get("missing", []))]
        return "\n".join(lines) + "\n"
    if mode == "fake":
        lines += ["", "FAKE daemon: plumbing only, these are not Blender or Godot numbers."]
    bp = doc.get("blender_pool", {})
    if bp:
        lines += ["", "### Blender pool (median ms)", "", "| measurement | median ms | n | note |", "|---|---:|---:|---|"]
        rows = [("cold start to first ping", bp.get("cold_start_to_first_ping")), ("warm ping", bp.get("warm_ping"))]
        rows += [(f"import_usd {k}", x) for k, x in (bp.get("import_usd") or {}).items()]
        rows += [(f"render_usd {k} 512px 16spp", x) for k, x in (bp.get("render_usd") or {}).items()]
        for name, d in rows:
            lines.append(f"| {name} | {cell(d)} | {(d or {}).get('n', '')} | {(d or {}).get('error', (d or {}).get('reason', ''))[:100]} |")
        lines += ["", "### Blender vs kernel, same meshes (median ms; ratio = Blender / kernel)", "",
                  "| mesh (tris) | operation | kernel ms | Blender ms | ratio | kernel made | Blender made |", "|---|---|---:|---:|---:|---|---|"]
        for c in bp.get("comparisons", []):
            for op, s in c["ops"].items():
                k, b = s.get("kernel", {}), s.get("blender", {})

                def made(d: dict[str, Any]) -> str:
                    keys = ("islands", "tris_by_level", "hull_vertices", "foreground_fraction")
                    return ", ".join(f"{x}={d[x]}" for x in keys if x in d)
                lines.append(f"| {c['mesh']} ({c['tris']}) | {op} | {cell(k)} | {cell(b)} | {s.get('blender_over_kernel') or 'n/a'} | {made(k)} | {made(b)} |")
    g = doc.get("godot", {})
    if g:
        lines += ["", "### Godot headless (median ms)", "", "| measurement | median ms | note |", "|---|---:|---|"]
        rows = [("empty project start/exit", g.get("empty_project_start")), ("capture crate", g.get("capture_crate"))]
        rows += [(f"import {k}", x) for k, x in (g.get("import") or {}).items()]
        for name, d in rows:
            lines.append(f"| {name} | {cell(d)} | {(d or {}).get('error', (d or {}).get('reason', ''))[:100]} |")
    if doc.get("errors"):
        lines += ["", f"### {len(doc['errors'])} measurement(s) failed or skipped", ""] + [f"- `{e[:220]}`" for e in doc["errors"][:30]]
    lines += ["", f"Wall time {doc.get('total_wall_s')} s.", ""]
    return "\n".join(lines)


def write_outputs(run: Run, out: Path, md: Path | None) -> None:
    doc = run.doc
    doc["errors"] = run.errors
    doc["total_wall_s"] = _r(run.elapsed(), 1)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=1, default=str))
    if md is not None:
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text(markdown(doc))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--markdown", type=Path, help="also write a short table (for $GITHUB_STEP_SUMMARY)")
    ap.add_argument("--fake", action="store_true", help="use the fake worker daemon: exercises the plumbing, measures nothing real, skips Godot")
    ap.add_argument("--quick", action="store_true", help="fewer repetitions and meshes (tests)")
    ap.add_argument("--budget-s", type=float, default=240.0, help="soft time budget: later measurements are skipped once it is used up")
    ns = ap.parse_args(argv)

    run = Run(ns.budget_s, ns.fake, ns.quick)
    doc = run.doc
    doc.update(schema=SCHEMA, mode="fake" if ns.fake else "real", ci=ci_info(), machine=machine_info(), versions={}, fatal=None, equivalence_notes=EQUIVALENCE)
    blender, godot = ({"available": True, "path": "fake daemon"}, {"available": False, "missing": "--fake does not run Godot"}) if ns.fake \
        else (tool_status("BLENDER_BIN"), tool_status("GODOT_BIN"))
    doc["tools"] = {"blender": blender, "godot": godot}
    missing = [f"{n}: {t['missing']}" for n, t in doc["tools"].items() if not t["available"] and not (ns.fake and n == "godot")]
    doc["missing"] = missing
    crashed = False
    try:
        if not blender["available"] and not godot["available"]:
            doc["mode"] = "unavailable"
            print("engine benchmark: nothing to measure. " + "; ".join(missing), file=sys.stderr)
        else:
            try:
                import antigravity3d
                doc["versions"]["antigravity3d"] = getattr(antigravity3d, "__version__", None)
                doc["versions"]["antigravity3d_from"] = str(Path(antigravity3d.__file__).resolve().parent)
            except ImportError:
                pass
            with tempfile.TemporaryDirectory(prefix="agy3d-engine-bench-") as td:
                workdir = Path(td)
                t = time.perf_counter()
                subjects = build_subjects(workdir, ns.quick)
                doc["setup"] = {"wall_s": _r(time.perf_counter() - t, 1), "packages": {n: {"tris": s["tris"], "parts": len(s["meshes"]), "files": s["files"], "ok": s["package_ok"]}
                                                                          for n, s in subjects["packages"].items()},
                                "comparison_meshes": {n: {"parts": len(m), "tris": int(sum(len(x.triangles) for x in m))} for n, m in subjects["comparison_meshes"].items()}}
                if blender["available"]:
                    try:
                        asyncio.run(blender_section(run, subjects, workdir))
                    except Exception as e:  # noqa: BLE001 - recorded, Godot still runs
                        run.record_error("blender_section", e)
                        doc.setdefault("blender_pool", {})["fatal"] = f"{type(e).__name__}: {e}"[:600]
                if godot["available"]:
                    godot_section(run, subjects, godot)
    except BaseException as e:  # noqa: BLE001 - benchmark crash: keep what was measured, say so, exit non-zero
        crashed = True
        doc["fatal"] = f"{type(e).__name__}: {e}"[:800]
        traceback.print_exc()
    finally:
        write_outputs(run, ns.out, ns.markdown)
        print("ENGINE_BENCH_JSON " + json.dumps(doc, separators=(",", ":"), default=str), flush=True)
    return 1 if crashed else 0


if __name__ == "__main__":
    sys.exit(main())
