"""BatchSanitizeDAG: many input meshes -> safe deterministic repairs -> UVs/LODs/colliders -> validated export.

No model calls unless a ``plane`` is passed for an optional visual check. Repairs are limited to ones that cannot
change the shape: weld duplicate vertices, drop degenerate and duplicate faces, fix winding / inverted normals,
and reground the whole asset. Anything else the Tier-1 checks find is reported as *remaining*, not guessed at.

Parallelism. The per-mesh work (xatlas UV unwrap, meshoptimizer LODs, trimesh repair, Python BFS winding, USD authoring)
is CPU-bound and mostly holds the interpreter lock, so threads in one process do not scale (measured: 0.9x to 1.0x for
the UV and LOD stages on 4 threads). ``--jobs N`` therefore means N *worker processes*: each is a fresh interpreter
(``python -c``, no ``fork``, no re-import of the caller's ``__main__``) that processes one file at a time and sends back
that file's report row. A worker that crashes (a CoACD segfault) or exceeds the per-file timeout is killed and replaced and
only that file's row is ``failed``; the rest of the batch completes. Workers are always killed on the way out, including
on Ctrl-C, and also die with the parent (``PR_SET_PDEATHSIG`` plus end-of-file on their pipe).
"""

from __future__ import annotations

import asyncio
import atexit
import faulthandler
import importlib
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
from multiprocessing.connection import Connection, wait
from pathlib import Path
from typing import Any

import numpy as np
import trimesh
from pxr import Usd, UsdGeom

from antigravity3d.tactics.governor import Governor, plan_for
from antigravity3d.dags.runner import Dag, DagReport, NodeCtx
from antigravity3d.kernel.compile_ir import USD_EXTS
from antigravity3d.kernel.fast_ops import run_export_pipeline, tier1_qa
from antigravity3d.kernel.mesh_io import triangle_parts
from antigravity3d.kernel.raster import contact_sheet
from antigravity3d.kernel.types import MeshData
from antigravity3d.kernel.usd_stage import StageManager, _triangulate
from antigravity3d.kernel.validator import check_engine_compliance, validate_usd
from antigravity3d.state import EngineBudget
from antigravity3d.tactics.common import Uploader, data_uri_uploader, png_bytes
from antigravity3d.tactics.package import write_glb_package, write_package_usd

MESH_EXTS = {".glb", ".gltf", ".obj", ".ply", ".stl", *USD_EXTS}
GROUND_TOL = 0.01
DEFAULT_TIMEOUT_S = 900.0  # per file, measured from dispatch; the slowest benchmark mesh (28k tris) needs about 3 s
START_TIMEOUT_S = 120.0  # a worker must import the stack and say hello within this
_THREAD_VARS = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")


def _ident(name: str, used: set[str], default: str = "mesh") -> str:
    base = re.sub(r"[^A-Za-z0-9_]", "_", name).strip("_") or default
    base = base if base[0].isalpha() else f"{default}_{base}"
    out, k = base, 1
    while out in used:
        k += 1
        out = f"{base}_{k}"
    used.add(out)
    return out


def _load_usd(path: Path) -> list[MeshData]:
    try:
        return StageManager.open(path).extract_meshes()
    except ValueError:  # a plain USD file without our IR: read its meshes directly
        stage = Usd.Stage.Open(str(path))
        xc, out, used = UsdGeom.XformCache(), [], set()
        for prim in stage.Traverse():
            if not prim.IsA(UsdGeom.Mesh):
                continue
            m = UsdGeom.Mesh(prim)
            pts, idx = m.GetPointsAttr().Get(), m.GetFaceVertexIndicesAttr().Get()
            if not pts or not idx:
                continue
            tris = _triangulate(np.asarray(m.GetFaceVertexCountsAttr().Get(), np.int64), np.asarray(idx, np.int64))
            mat = np.array(xc.GetLocalToWorldTransform(prim))
            world = np.asarray(pts, np.float64) @ mat[:3, :3] + mat[3, :3]
            if UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.y:
                world = np.stack([world[:, 0], -world[:, 2], world[:, 1]], axis=1)
            out.append(MeshData(_ident(prim.GetName(), used), str(prim.GetPath()), world, tris))
        return out


def load_meshes(path: Path | str) -> list[MeshData]:
    """World-space, Z-up MeshData from GLB/glTF/OBJ/PLY/STL (trimesh) or USD (stage manager)."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext in USD_EXTS:
        meshes = _load_usd(path)
    else:
        used: set[str] = set()
        meshes = [
            MeshData(_ident(name, used), "", points, faces)
            for name, points, faces, _ in triangle_parts(path)
        ]
    if not meshes:
        raise ValueError(f"{path.name}: no triangle meshes found")
    return meshes


def orient_faces(points: np.ndarray, faces: np.ndarray) -> tuple[np.ndarray, int]:
    """Consistent winding per connected component (BFS over shared edges), then outward for closed bodies.

    Own implementation: trimesh's ``fix_winding`` needs ``networkx``, which is not a declared dependency.
    """
    from collections import deque

    f = faces.copy()
    n = len(f)
    he: dict[tuple[int, int], list[int]] = {}
    for i, (a, b, c) in enumerate(f.tolist()):
        for u, v in ((a, b), (b, c), (c, a)):
            he.setdefault((min(u, v), max(u, v)), []).append(i)
    nbrs: list[list[int]] = [[] for _ in range(n)]
    for fs in he.values():
        if len(fs) == 2:
            nbrs[fs[0]].append(fs[1])
            nbrs[fs[1]].append(fs[0])

    def directed(i: int) -> set[tuple[int, int]]:
        a, b, c = f[i].tolist()
        return {(a, b), (b, c), (c, a)}

    seen = np.zeros(n, bool)
    flipped = 0
    for s in range(n):
        if seen[s]:
            continue
        comp, q = [s], deque([s])
        seen[s] = True
        while q:
            i = q.popleft()
            di = directed(i)
            for j in nbrs[i]:
                if seen[j]:
                    continue
                if di & directed(j):  # shares a directed edge with i: j runs the same way, flip it
                    f[j] = f[j][::-1]
                    flipped += 1
                seen[j] = True
                comp.append(j)
                q.append(j)
        cf = f[comp]
        if all(len(he[(min(u, v), max(u, v))]) == 2 for tri in cf.tolist() for u, v in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0]))):
            p = points[cf]
            vol = float(np.einsum("ij,ij->i", p[:, 0], np.cross(p[:, 1], p[:, 2])).sum() / 6.0)
            if vol < 0:
                f[comp] = cf[:, ::-1]
                flipped += len(comp)
    return f, flipped


def repair_mesh(m: MeshData) -> tuple[MeshData, dict[str, int]]:
    """Shape-preserving repairs; returns the repaired mesh and counts of what was changed."""
    tm = trimesh.Trimesh(np.asarray(m.points, np.float64), np.asarray(m.triangles), process=False)
    v0, f0, wound = len(tm.vertices), len(tm.faces), bool(tm.is_winding_consistent)
    tm.merge_vertices()
    f1 = len(tm.faces)
    tm.update_faces(tm.nondegenerate_faces())
    dropped = f1 - len(tm.faces)
    f2 = len(tm.faces)
    tm.update_faces(tm.unique_faces())
    dup = f2 - len(tm.faces)
    tm.remove_unreferenced_vertices()
    flipped = 0
    if len(tm.faces):
        faces, flipped = orient_faces(np.asarray(tm.vertices), np.asarray(tm.faces))
        tm = trimesh.Trimesh(np.asarray(tm.vertices), faces, process=False)
    counts = {"merged_vertices": v0 - len(tm.vertices), "degenerate_faces": dropped, "duplicate_faces": dup, "reoriented_faces": flipped,
              "winding_was_consistent": int(wound)}
    return MeshData(m.node_id, m.prim_path, np.asarray(tm.vertices, np.float64), np.asarray(tm.faces, np.int32), m.material_id, m.collider), counts


def reground(meshes: list[MeshData]) -> tuple[list[MeshData], float]:
    lo = min(float(m.points[:, 2].min()) for m in meshes if len(m.points))
    if abs(lo) <= GROUND_TOL:
        return meshes, 0.0
    return [MeshData(m.node_id, m.prim_path, m.points - np.array([0.0, 0.0, lo]), m.triangles, m.material_id, m.collider) for m in meshes], -lo


def _tokens(report: Any) -> set[str]:
    return {f"{i.code}@{i.node_id}" if i.node_id else i.code for i in report.issues}


def sanitize_file(path: Path, out_dir: Path, budget: EngineBudget, want_sheet: bool = False,
                  hook: str | None = None) -> tuple[dict[str, Any], bytes | None, float]:
    """Everything for one file, synchronously: the unit of work a worker process runs.

    Returns ``(row, contact_sheet_png_or_None, sheet_ms)``. Raises for an unreadable or empty input; the caller turns that
    into a ``failed`` row. ``hook`` ("module:function", called with ``path``) exists so tests can make one input crash or
    hang its worker; it does nothing in normal use.
    """
    if hook:
        mod, _, fn = hook.partition(":")
        getattr(importlib.import_module(mod), fn)(path)
    t: dict[str, float] = {}
    clock = time.perf_counter
    row: dict[str, Any] = {"file": str(path), "status": "ok", "ms": t}

    t0 = clock()
    meshes = load_meshes(path)
    t["load"] = (clock() - t0) * 1e3
    asset = _ident(path.stem, set(), "Asset")
    before = tier1_qa(meshes, budget)
    row.update(asset=asset, tris_before=before.stats["tris"], issues_before=sorted(_tokens(before)))

    t0 = clock()
    fixed_meshes, counts = [], {}
    for m in meshes:
        r, c = repair_mesh(m)
        fixed_meshes.append(r)
        for k, v in c.items():
            counts[k] = counts.get(k, 0) + v
    fixed_meshes, dz = reground(fixed_meshes)
    counts["grounded_dz"] = round(dz, 6)
    row["repairs"] = counts
    t["repair"] = (clock() - t0) * 1e3
    after = tier1_qa(fixed_meshes, budget)
    row.update(tris_after=after.stats["tris"], issues_after=sorted(_tokens(after)),
               issues_fixed=sorted(_tokens(before) - _tokens(after)), issues_remaining=sorted(_tokens(after)))
    crit_left = [i for i in after.issues if i.severity == "critical"]

    t0 = clock()
    bundle = run_export_pipeline(fixed_meshes, budget, asset)
    t["uv_lod_collider"] = (clock() - t0) * 1e3
    levels = [sum(len(v[k].triangles) for v in bundle.lods.values()) for k in range(min((len(v) for v in bundle.lods.values()), default=0))]
    row.update(lod_tris=levels, hulls=len(bundle.colliders), uv_stretch_max=max((u.stretch_max for u in bundle.uvs), default=0.0))

    t0 = clock()
    out_dir.mkdir(parents=True, exist_ok=True)
    usd = write_package_usd(out_dir / f"{asset}.usda", asset, bundle.uvs, {}, [], bundle.colliders, budget.target_engine)
    glb = write_glb_package(out_dir / f"{asset}.glb", bundle)
    stage = Usd.Stage.Open(str(usd))
    val = [i for i in validate_usd(stage) if i.severity == "critical"]
    comp = [i for i in check_engine_compliance(stage, fixed_meshes, budget) if i.severity == "critical"]
    t["validate_export"] = (clock() - t0) * 1e3
    row.update(validate_critical=[f"{i.code}@{i.node_id}" for i in val], compliance_critical=[f"{i.code}@{i.node_id}" for i in comp],
               outputs={"usd": str(usd), "glb": str(glb)})
    if crit_left or val or comp:
        row["status"] = "needs_attention"

    png, sheet_ms = None, 0.0
    if want_sheet:  # the raster is CPU work too: do it here, send back only the PNG
        t0 = clock()
        png = png_bytes(contact_sheet(fixed_meshes))
        sheet_ms = (clock() - t0) * 1e3
    for k in t:
        t[k] = round(t[k], 1)
    return row, png, sheet_ms


# --------------------------------------------------------------------------- worker processes


class WorkerFailure(Exception):
    """A worker died or was killed while holding a file. ``kind`` is ``crash`` or ``timeout``."""

    kind = "crash"

    def __init__(self, message: str, pid: int | None = None) -> None:
        super().__init__(message)
        self.pid = pid


class WorkerCrashed(WorkerFailure):
    kind = "crash"


class WorkerTimeout(WorkerFailure):
    kind = "timeout"


def _raise_in_parent(type_name: str, message: str) -> None:
    """Re-raise a worker's exception under its original class name, so rows read ``ValueError: ...`` as they always did."""
    raise type(type_name, (Exception,), {"__module__": "builtins"})(message)


def _worker_main(fd: int) -> None:
    """Entry point of a worker process (``python -c``): serve one file at a time over the socket ``fd`` until EOF."""
    faulthandler.enable()  # a segfault leaves a Python traceback in the captured stderr
    try:  # die with the parent even in the middle of a file (the idle case is end-of-file on the socket)
        import ctypes

        ctypes.CDLL(None).prctl(1, int(signal.SIGKILL), 0, 0, 0)  # PR_SET_PDEATHSIG
    except Exception:  # noqa: BLE001 - not Linux, or no libc handle: the EOF path still covers the idle case
        pass
    conn = Connection(fd)
    for mod in ("coacd", "xatlas", "meshoptimizer"):  # warm-up: pay the import cost before the first timed file
        try:
            importlib.import_module(mod)
        except ImportError:
            pass
    conn.send(("ready", os.getpid()))
    while True:
        try:
            task = conn.recv()
        except (EOFError, OSError):
            return
        if task is None:
            return
        try:
            result: tuple = ("ok", sanitize_file(*task))
        except Exception as e:  # noqa: BLE001 - one bad file is that file's failed row
            result = ("err", type(e).__name__, str(e))
        conn.send(result)


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL the worker's whole process group and reap it. A no-op once reaped, so a recycled pid is never signalled."""
    if proc.poll() is None:  # unreaped: the pid cannot have been reused
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # the worker is its own session leader
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - SIGKILL does not time out
        pass


class _Worker:
    """One worker process and its socket. ``call`` is used from one thread at a time."""

    def __init__(self, env: dict[str, str], pythonpath: str) -> None:
        mine, theirs = socket.socketpair()
        self.log = tempfile.TemporaryFile()  # stdout+stderr: native-library noise stays off the CLI, and crashes stay diagnosable
        code = f"from antigravity3d.dags.batch_sanitize import _worker_main; _worker_main({theirs.fileno()})"
        # -P: do not prepend the cwd to sys.path. The parent's sys.path is replicated through PYTHONPATH instead.
        self.proc = subprocess.Popen([sys.executable, "-P", "-c", code], pass_fds=[theirs.fileno()], stdin=subprocess.DEVNULL,
                                     stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True,
                                     env={**env, "PYTHONPATH": pythonpath})
        theirs.close()
        self.conn = Connection(mine.detach())
        self.pid = self.proc.pid
        self.ready = False
        self.dead = False

    def _stderr_tail(self, limit: int = 1500) -> str:
        try:
            self.log.seek(0, os.SEEK_END)
            n = self.log.tell()
            self.log.seek(max(0, n - limit))
            text = self.log.read().decode("utf-8", "replace").strip()
        except (OSError, ValueError):
            return ""
        lines = [ln for ln in text.splitlines() if not ln.startswith("#")]
        return "\n".join(lines[-12:])

    def _fail(self, exc_type: type[WorkerFailure], message: str) -> WorkerFailure:
        self.dead = True
        _kill_group(self.proc)
        tail = self._stderr_tail()
        self.dispose()
        return exc_type(message + (f"; worker output: {tail}" if tail else ""), self.pid)

    def _recv(self, timeout: float | None, what: str) -> Any:
        try:
            ready = wait([self.conn], timeout)
        except (OSError, ValueError):  # handle closed under us: the pool is shutting down
            ready = [self.conn]
        if not ready:
            raise self._fail(WorkerTimeout, f"worker pid {self.pid} {what} timed out after {timeout:g} s and was killed")
        try:
            return self.conn.recv()
        except (EOFError, OSError):
            try:
                rc = self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                rc = None
            how = f"signal {signal.Signals(-rc).name}" if rc is not None and rc < 0 else f"exit code {rc}"
            raise self._fail(WorkerCrashed, f"worker pid {self.pid} died ({how}) {what}") from None

    def call(self, task: tuple, timeout: float | None) -> tuple:
        if not self.ready:
            self._recv(START_TIMEOUT_S, "starting up")
            self.ready = True
        try:
            self.conn.send(task)
        except (BrokenPipeError, OSError):
            raise self._fail(WorkerCrashed, f"worker pid {self.pid} died before it could take {Path(task[0]).name}") from None
        return self._recv(timeout, f"while processing {Path(task[0]).name}")

    def dispose(self) -> None:
        for closer in (self.conn.close, self.log.close):
            try:
                closer()
            except OSError:
                pass


_LIVE_POOLS: set[_Pool] = set()


@atexit.register
def _kill_leftovers() -> None:  # a safety net for exits that skip the normal close
    for pool in list(_LIVE_POOLS):
        pool.close()


def _cpu_count() -> int:
    n = getattr(os, "process_cpu_count", lambda: None)() or len(getattr(os, "sched_getaffinity", lambda _: [])(0)) or os.cpu_count()
    return n or 1


class _Pool:
    """``size`` worker processes; ``run`` hands one task to a free worker and replaces a worker that died."""

    def __init__(self, size: int) -> None:
        self.size = size
        env = dict(os.environ)
        cap = max(1, _cpu_count() // size)  # n workers x all-core native thread pools would oversubscribe the machine
        if cap < _cpu_count():
            for v in _THREAD_VARS:
                env.setdefault(v, str(cap))
        self.env, self.pythonpath = env, os.pathsep.join(p for p in sys.path if p)
        self.free: asyncio.Queue[_Worker] = asyncio.Queue()
        self.live: list[_Worker] = []
        self.pids: list[int] = []
        self.closed = False
        _LIVE_POOLS.add(self)
        for _ in range(size):  # all start now, so their imports overlap
            self._spawn()

    def _spawn(self) -> None:  # on the event-loop thread: PDEATHSIG is tied to the thread that started the child
        w = _Worker(self.env, self.pythonpath)
        self.pids.append(w.pid)
        self.live.append(w)
        self.free.put_nowait(w)

    async def run(self, task: tuple, timeout: float | None) -> tuple[int, tuple]:
        w = await self.free.get()
        try:
            return w.pid, await asyncio.to_thread(w.call, task, timeout)
        except WorkerFailure:
            self.live.remove(w)
            if not self.closed:
                self._spawn()  # the dead worker is gone; keep the pool at full strength
            raise
        except BaseException:  # cancelled mid-file: the worker's state is unknown, never reuse it
            w.dead = True
            _kill_group(w.proc)
            self.live.remove(w)
            raise
        finally:
            if not w.dead:
                self.free.put_nowait(w)

    def close(self) -> None:
        self.closed = True
        _LIVE_POOLS.discard(self)
        for w in list(self.live):  # killing a busy worker is what unblocks the thread waiting on it; that thread disposes it
            _kill_group(w.proc)
            w.dispose()  # a thread still waiting on it sees a closed handle and reports the worker as killed
        self.live.clear()


class BatchSanitizeDAG:
    """``max_parallel`` is the number of worker processes (``--jobs``; None: one per usable CPU core, never more than files). ``timeout_s`` bounds one file (None: unbounded).

    ``isolate=False`` runs files in threads of this process instead (the pre-fix behaviour: no crash isolation and no
    CPU scaling); it is the fallback where worker processes are unavailable (non-POSIX).
    """

    def __init__(self, *, workdir: Path | str, budget: EngineBudget, plane: Any = None, uploader: Uploader | None = None, forge: Any = None,
                 pool: Any = None, cache: Any = None, max_parallel: int | None = None, timeout_s: float | None = DEFAULT_TIMEOUT_S,
                 isolate: bool | None = None, worker_hook: str | None = None) -> None:
        self.out, self.budget, self.plane, self.cache = Path(workdir), budget, plane, cache
        self.up = uploader or (forge.upload if forge is not None else data_uri_uploader)
        self.max_parallel = max(1, int(max_parallel or _cpu_count()))
        self.timeout_s = timeout_s if timeout_s and timeout_s > 0 else None
        self.isolate = (os.name == "posix") if isolate is None else isolate
        self.worker_hook = worker_hook
        self._pool: _Pool | None = None
        self._failures: dict[str, dict[str, Any]] = {}
        self._input_index: dict[str, int] = {}

    async def _process(self, path: Path, out_dir: Path) -> tuple[dict[str, Any], bytes | None, float]:
        task = (path, out_dir, self.budget, self.plane is not None, self.worker_hook)
        if self._pool is None:  # in-process fallback
            return await asyncio.to_thread(sanitize_file, *task)
        pid, reply = await self._pool.run(task, self.timeout_s)
        if reply[0] == "err":
            _raise_in_parent(reply[1], reply[2])
        row, png, sheet_ms = reply[1]
        row["worker_pid"] = pid
        return row, png, sheet_ms

    async def sanitize_one(self, path: Path, out_dir: Path) -> dict[str, Any]:
        row, png, sheet_ms = await self._process(path, out_dir)
        if self.plane is not None:
            t0 = time.perf_counter()
            from antigravity3d.decisions.compose import decide_action, tier15_score

            governor = Governor(plan_for(0), journal=out_dir / "spend.sqlite3")
            q = await self.plane.evaluate_visual_qa(await self.up(png), stage="blockout", governor=governor)
            row["decision_receipts"] = governor.receipts()
            row["visual"] = {"tier15": tier15_score(q), "action": decide_action(q, None, "blockout").kind}
            row["ms"]["visual_check"] = round(sheet_ms + (time.perf_counter() - t0) * 1e3, 1)
        return row

    def build(self, files: list[Path | str]) -> Dag:
        """One node per file. Nodes are added largest file first (ties keep input order): with N workers the batch ends when
        the last file does, so starting the big ones first shortens the tail. Only dispatch order changes: every file's
        output is the same, and ``run`` reports the rows in input order."""
        dag = Dag("batch_sanitize")
        seen: set[str] = set()
        named = [(f"file:{_ident(Path(f).name, seen, 'file')}", Path(f)) for f in files]  # names follow input order
        self._input_index = {node: i for i, (node, _) in enumerate(named)}

        def size(p: Path) -> int:
            try:
                return p.stat().st_size
            except OSError:
                return 0

        for node, p in sorted(named, key=lambda np_: -size(np_[1])):

            async def run(ctx: NodeCtx, p: Path = p, node: str = node) -> dict[str, Any]:
                try:
                    row = await self.sanitize_one(p, self.out / node.split(":", 1)[1])
                except WorkerFailure as e:
                    self._failures[node] = {"failure": e.kind, "worker_pid": e.pid, **({"timeout_s": self.timeout_s} if e.kind == "timeout" else {})}
                    raise
                ctx.data(status=row["status"], tris_before=row["tris_before"], tris_after=row["tris_after"])
                return row

            dag.node(node, run)
        return dag

    async def run(self, files: list[Path | str]) -> DagReport:
        jobs = min(self.max_parallel, max(1, len(files)))
        self._pool = _Pool(jobs) if self.isolate and files else None
        try:
            rep = await self.build(files).run(max_parallel=jobs)
        finally:
            if self._pool is not None:
                self._pool.close()
        rows = []
        paths = {node: str(f) for node, f in zip(self._input_index, files)}
        for n in sorted(rep.nodes.values(), key=lambda n: self._input_index[n.name]):  # input order, whatever the dispatch / finish order
            r = rep.results.get(n.name)
            rows.append(r if r else {"file": paths[n.name], "status": "failed", "error": n.error or n.reason, **self._failures.get(n.name, {})})
        used = sorted({r["worker_pid"] for r in rows if r.get("worker_pid")})
        rep.extras = {"rows": rows, "engine": self.budget.target_engine, "out": str(self.out),
                      "gates_ok": bool(rows) and all(r["status"] == "ok" for r in rows),
                      "workers": {"mode": "processes" if self._pool is not None else "threads", "jobs": jobs, "timeout_s": self.timeout_s,
                                  "distinct_pids": len(used), "pids_spawned": list(self._pool.pids) if self._pool else [],
                                  "restarted": max(0, len(self._pool.pids) - jobs) if self._pool else 0},
                      "skipped": [{"step": "visual_check", "reason": "no plane supplied: sanitize makes no model calls"}] if self.plane is None else []}
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "report.json").write_text(rep.to_json())
        return rep


def table(rows: list[dict[str, Any]]) -> str:
    lines = [f"{'file':<28}{'status':<16}{'tris':>14}  {'fixed':>5} {'left':>4}  LODs"]
    for r in rows:
        name = Path(r["file"]).name[:27]
        if r["status"] == "failed":
            lines.append(f"{name:<28}{'FAILED':<16}  {r.get('error', '')}")
            continue
        lines.append(f"{name:<28}{r['status']:<16}{r['tris_before']:>6}->{r['tris_after']:<6}  {len(r['issues_fixed']):>5} {len(r['issues_remaining']):>4}  {r['lod_tris']}")
    return "\n".join(lines)
