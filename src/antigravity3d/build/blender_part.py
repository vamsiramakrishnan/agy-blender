"""``a.blender(id, code)``: the mesh a Blender Python snippet builds, run by a headless Blender.

The snippet is linted first with the project's own gate (``adapters.blender_5_3.linter.assert_safe``: a guard against
accidents, not a sandbox), then run by ``$BLENDER_BIN`` (else ``blender`` on PATH) with ``--factory-startup`` in an
emptied scene, inside ``WRAPPER``. Afterwards the wrapper takes every mesh-like object left in the scene (mesh, curve,
surface, text, metaball) that is visible, evaluated with its modifiers, in world space, triangulated, and hands the joined
arrays back. Hidden objects (hidden in the viewport or for render, or drawn as wire or bounds) are left out: the usual
boolean idiom hides its cutter, and joining it made a part with 544 non-manifold edges (review 2026-11-22, r7).
Nothing is repaired or welded: what Blender shared stays shared, the session's checks report the rest.

The result is cached under ``$AGY3D_BUILD_CACHE`` (default ``~/.cache/agy3d/build``) by the hash of snippet, wrapper and
Blender binary path, so an unchanged snippet costs nothing on the next run and the same script makes the same part.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from antigravity3d.build.shapes import BuildError

TIMEOUT_S = 120.0
CACHE_ENV = "AGY3D_BUILD_CACHE"

WRAPPER = r'''
import json, sys, traceback
import bpy
import numpy as np

snippet_path, out_path, err_path = sys.argv[sys.argv.index("--") + 1:]
bpy.ops.wm.read_factory_settings(use_empty=True)
with open(snippet_path, encoding="utf-8") as fh:
    source = fh.read()
try:
    exec(compile(source, "<snippet>", "exec"), {"__name__": "__snippet__"})
except BaseException as e:
    line = (e.lineno or 0) if isinstance(e, SyntaxError) and e.filename == "<snippet>" else 0
    for frame in traceback.extract_tb(e.__traceback__):
        if frame.filename == "<snippet>":
            line = frame.lineno
    with open(err_path, "w", encoding="utf-8") as fh:
        json.dump({"line": line, "error": f"{type(e).__name__}: {e}"}, fh)
    sys.exit(1)
depsgraph = bpy.context.evaluated_depsgraph_get()
points, tris, objects, offset = [], [], 0, 0
skipped = {}
for ob in sorted(bpy.context.scene.objects, key=lambda o: o.name):
    if ob.type not in {"MESH", "CURVE", "SURFACE", "FONT", "META"}:
        continue
    why = ("hidden" if ob.hide_get() or ob.hide_viewport else "hidden for render" if ob.hide_render
           else f"shown as {ob.display_type.lower()}" if ob.display_type in {"WIRE", "BOUNDS"} else "")
    if why:
        skipped[why] = skipped.get(why, 0) + 1
        continue
    ev = ob.evaluated_get(depsgraph)
    me = ev.to_mesh()
    if me is None:
        continue
    me.calc_loop_triangles()
    nv, nt = len(me.vertices), len(me.loop_triangles)
    if nv and nt:
        co = np.empty(nv * 3, np.float32)
        me.vertices.foreach_get("co", co)
        ix = np.empty(nt * 3, np.int32)
        me.loop_triangles.foreach_get("vertices", ix)
        m = np.array(ob.matrix_world, np.float64)
        pts = co.reshape(-1, 3).astype(np.float64) @ m[:3, :3].T + m[:3, 3]
        tri = ix.reshape(-1, 3)
        if np.linalg.det(m[:3, :3]) < 0:
            tri = tri[:, ::-1]
        points.append(pts)
        tris.append(tri + offset)
        offset += nv
        objects += 1
    ev.to_mesh_clear()
np.savez(out_path, points=np.concatenate(points) if points else np.zeros((0, 3)),
         tris=np.concatenate(tris) if tris else np.zeros((0, 3), np.int32), objects=np.array(objects),
         skipped=np.array(json.dumps(skipped, sort_keys=True)))
'''


def _find_blender() -> str | None:
    from antigravity3d.adapters.blender_5_3.pool import (
        find_blender,  # the project's one rule: $BLENDER_BIN, else PATH
    )

    return find_blender()


def cache_dir() -> Path:
    return Path(os.environ.get(CACHE_ENV) or Path.home() / ".cache" / "agy3d" / "build")


def run(pid: str, code: str) -> tuple[np.ndarray, np.ndarray, int, dict[str, int], bool]:
    """(points, triangles, objects joined, {why: count} of objects left out, served from the cache) of the snippet
    ``code``; raises BuildError."""
    where = f"blender {pid}"
    if not isinstance(code, str) or not code.strip():
        raise BuildError(f"{where}: code must be the snippet's Python text")
    from antigravity3d.adapters.blender_5_3.linter import UnsafeScriptError, assert_safe

    try:
        assert_safe(code)
    except UnsafeScriptError as e:
        first = "; ".join(f"line {i.line}: {i.message}" for i in e.issues[:3])
        raise BuildError(f"{where}: the snippet is refused: {first}") from None
    blender = _find_blender()
    if not blender:
        raise BuildError(f"{where}: no Blender here ($BLENDER_BIN is not set and `blender` is not on PATH); a.mesh, "
                         "a.tube and a.loft build without it")
    key = hashlib.sha256(f"{code}\0{WRAPPER}\0{blender}".encode()).hexdigest()
    cached = cache_dir() / f"{key}.npz"
    if cached.is_file():
        try:
            pts, tris, n, skipped = _read(cached)
            return pts, tris, n, skipped, True
        except (OSError, ValueError, KeyError):
            pass  # a damaged cache entry is rebuilt below
    with tempfile.TemporaryDirectory(prefix="agy3d_blender_") as tmp:
        t = Path(tmp)
        (t / "snippet.py").write_text(code, encoding="utf-8")
        (t / "wrapper.py").write_text(WRAPPER, encoding="utf-8")
        out, err = t / "out.npz", t / "err.json"
        cmd = [blender, "--background", "--factory-startup", "--python-exit-code", "1", "--python", str(t / "wrapper.py"),
               "--", str(t / "snippet.py"), str(out), str(err)]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=True, cwd=tmp)
        try:
            log, _ = proc.communicate(timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)  # Blender and anything it started
            proc.communicate()
            raise BuildError(f"{where}: the snippet did not finish within {TIMEOUT_S:.0f} s") from None
        if err.is_file():
            info = json.loads(err.read_text(encoding="utf-8"))
            line = int(info.get("line") or 0)
            lines = code.splitlines()
            text = f": `{lines[line - 1].strip()}`" if 0 < line <= len(lines) else ""
            raise BuildError(f"{where}: line {line} of the snippet{text}: {info.get('error')}")
        if proc.returncode != 0 or not out.is_file():
            tail = " | ".join(log.decode("utf-8", "replace").strip().splitlines()[-3:])
            raise BuildError(f"{where}: Blender stopped with exit code {proc.returncode}: {tail}")
        pts, tris, n, skipped = _read(out)
        cached.parent.mkdir(parents=True, exist_ok=True)
        part = cached.with_suffix(f".{os.getpid()}.tmp")
        shutil.copyfile(out, part)
        os.replace(part, cached)  # atomic: a parallel run never reads half a file
    return pts, tris, n, skipped, False


def _read(path: Path) -> tuple[np.ndarray, np.ndarray, int, dict[str, int]]:
    with np.load(path) as z:
        return (np.asarray(z["points"], np.float64), np.asarray(z["tris"], np.int32), int(z["objects"]),
                json.loads(str(z["skipped"])))
