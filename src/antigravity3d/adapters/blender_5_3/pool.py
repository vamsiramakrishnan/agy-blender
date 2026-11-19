"""Warm pool of Blender daemons.

``BlenderPool(size, blender_bin, fake=False)``: N daemons, each with its own
Unix socket in a private temp dir. ``call`` takes an idle worker from an asyncio
queue, so concurrency is bounded by ``size``.

Buffer ownership: results carrying ``{shm, shape, dtype}`` are *copied out* into
ordinary numpy arrays before the worker is released; the caller owns the copies.
The daemon (creator) unlinks its segments on the next request or at exit; the
pool additionally sweeps ``/dev/shm/<prefix>*`` when it replaces a worker or stops.

A worker that times out, drops the connection or dies is killed and replaced
(lazily restarted, with a background warm-up) and the call raises ``WorkerError``.

Verified here with ``fake=True`` (plain CPython daemons). Real Blender spawning
(``--background --factory-startup --python daemon.py -- --socket ...``) is written
to spec only. Blender's Python has no msgpack, so the child gets PYTHONPATH
including the directory that holds the host's ``msgpack`` package; whether the
host's compiled msgpack loads there depends on matching Python ABIs, otherwise
msgpack's pure-Python fallback is used.
"""

from __future__ import annotations

import asyncio
import glob
import itertools
import os
import shutil
import signal
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from . import protocol as proto

DAEMON = Path(__file__).with_name("daemon.py")


class WorkerError(RuntimeError):
    """The worker timed out, died, or could not start. ``kind`` in {timeout, died, startup}."""

    def __init__(self, kind: str, msg: str):
        super().__init__(f"{kind}: {msg}")
        self.kind = kind


class BlenderOpError(Exception):
    """The op raised inside the daemon."""

    def __init__(self, cls: str, msg: str, trace_tail: str = ""):
        super().__init__(f"{cls}: {msg}")
        self.cls, self.msg, self.trace_tail = cls, msg, trace_tail


def find_blender(explicit: str | None = None) -> str | None:
    cand = explicit or os.environ.get("BLENDER_BIN")
    if cand:
        return cand if os.path.exists(cand) else shutil.which(cand)
    return shutil.which("blender")


def _short_tmpdir() -> str:
    base = tempfile.gettempdir()
    if len(base) > 60:  # AF_UNIX paths are limited to ~104 bytes
        base = "/tmp"
    return tempfile.mkdtemp(prefix="agy3d-pool-", dir=base)


class _Worker:
    def __init__(self, pool: BlenderPool, idx: int):
        self.pool, self.idx, self.gen = pool, idx, 0
        self.proc: asyncio.subprocess.Process | None = None
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.lock = asyncio.Lock()
        self.shm_prefix = f"{pool.prefix}w{idx}_"

    @property
    def ready(self) -> bool:
        return (self.proc is not None and self.proc.returncode is None and self.writer is not None
                and self.reader is not None and not self.reader.at_eof() and not self.writer.is_closing())

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    async def ensure_started(self) -> None:
        async with self.lock:
            if self.proc is not None and self.proc.returncode is not None:
                await self._teardown()
            if not self.ready:
                await self._spawn()

    async def _spawn(self) -> None:
        pool = self.pool
        self.gen += 1
        sock = os.path.join(pool.tmpdir, f"w{self.idx}-{self.gen}.sock")
        log = open(os.path.join(pool.tmpdir, f"w{self.idx}-{self.gen}.log"), "wb")
        env = {**os.environ, "AGY3D_SHM_PREFIX": self.shm_prefix, **pool.extra_env}
        if pool.fake:
            cmd = [sys.executable, str(DAEMON), "--fake", "--socket", sock, "--parent-pid", str(os.getpid())]
        else:
            import msgpack
            site = str(Path(msgpack.__file__).resolve().parent.parent)
            env["PYTHONPATH"] = os.pathsep.join(p for p in (site, env.get("PYTHONPATH", "")) if p)
            cmd = [pool.blender_bin or "", "--background", "--factory-startup", "--python", str(DAEMON),
                   "--", "--socket", sock, "--parent-pid", str(os.getpid())]
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=log, stderr=log, env=env, start_new_session=True)
        finally:
            log.close()
        try:
            await asyncio.wait_for(self._connect(sock), pool.startup_timeout)
            resp = await asyncio.wait_for(self._roundtrip_raw("ping", {}), pool.startup_timeout)
            if not resp.get("ok"):
                raise WorkerError("startup", f"ping failed: {resp}")
        except BaseException as e:
            tail = self._log_tail(os.path.join(pool.tmpdir, f"w{self.idx}-{self.gen}.log"))
            await self._teardown()
            if isinstance(e, asyncio.CancelledError):
                raise
            raise WorkerError("startup", f"worker {self.idx} did not come up ({type(e).__name__}); log: {tail}") from e

    async def _connect(self, sock: str) -> None:
        assert self.proc
        while True:
            if self.proc.returncode is not None:
                raise ConnectionError(f"daemon exited with {self.proc.returncode}")
            try:
                self.reader, self.writer = await asyncio.open_unix_connection(sock)
                return
            except (FileNotFoundError, ConnectionRefusedError):
                await asyncio.sleep(0.02)

    @staticmethod
    def _log_tail(path: str) -> str:
        try:
            return Path(path).read_text(errors="replace")[-500:]
        except OSError:
            return ""

    async def _roundtrip_raw(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        assert self.reader and self.writer
        mid = next(self.pool._ids)
        await proto.asend_msg(self.writer, proto.request(mid, op, args))
        resp = await proto.arecv_msg(self.reader)
        if resp is None:
            raise ConnectionError("daemon closed the connection")
        if resp.get("id") != mid:
            raise proto.ProtocolError(f"response id {resp.get('id')} != {mid}")
        return resp

    async def _teardown(self) -> None:
        w, self.writer, self.reader = self.writer, None, None
        if w is not None:
            w.close()
        p, self.proc = self.proc, None
        if p is not None and p.returncode is None:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                p.kill()
            except ProcessLookupError:
                pass
        if p is not None:
            await p.wait()
        self.pool._sweep_shm(self.shm_prefix)

    async def shutdown(self) -> None:
        if self.ready:
            try:
                await asyncio.wait_for(self._roundtrip_raw("shutdown", {}), 2)
                assert self.proc
                await asyncio.wait_for(self.proc.wait(), 3)
            except Exception:
                pass
        await self._teardown()


def _materialize(obj: Any) -> Any:
    """Replaces every ``{shm, shape, dtype}`` descriptor with a copied numpy array."""
    if proto.is_shm_desc(obj):
        return proto.read_shm_copy(obj)
    if isinstance(obj, dict):
        return {k: _materialize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_materialize(v) for v in obj]
    return obj


class BlenderPool:
    def __init__(self, size: int, blender_bin: str | None = None, fake: bool = False, *,
                 timeout: float = 120.0, startup_timeout: float = 60.0, extra_env: dict[str, str] | None = None):
        if size < 1:
            raise ValueError("size must be >= 1")
        self.size, self.fake, self.timeout, self.startup_timeout = size, fake, timeout, startup_timeout
        self.blender_bin = None if fake else find_blender(blender_bin)
        self.extra_env = dict(extra_env or {})
        self.prefix = f"agy3d_{os.getpid()}_{uuid.uuid4().hex[:6]}_"
        self.tmpdir = ""
        self._ids = itertools.count(1)
        self._workers: list[_Worker] = []
        self._idle: asyncio.Queue[_Worker] = asyncio.Queue()
        self._bg: set[asyncio.Task[None]] = set()
        self._started = False

    @property
    def worker_pids(self) -> list[int | None]:
        return [w.pid for w in self._workers]

    async def start(self) -> None:
        if self._started:
            return
        if not self.fake and not self.blender_bin:
            raise FileNotFoundError("no Blender binary: set $BLENDER_BIN or put `blender` on PATH")
        self.tmpdir = _short_tmpdir()
        self._started = True
        self._workers = [_Worker(self, i) for i in range(self.size)]
        try:
            await asyncio.gather(*(w.ensure_started() for w in self._workers))
        except BaseException:
            await self.stop()
            raise
        for w in self._workers:
            self._idle.put_nowait(w)

    def _sweep_shm(self, prefix: str) -> None:
        for p in glob.glob(f"/dev/shm/{prefix}*"):
            try:
                os.unlink(p)
            except OSError:
                pass

    async def _acquire(self) -> _Worker:
        if not self._started:
            raise RuntimeError("pool not started")
        w = await self._idle.get()
        try:
            await asyncio.sleep(0)  # let the child watcher notice an idle SIGKILL
            await w.ensure_started()
        except BaseException:
            self._idle.put_nowait(w)
            raise
        return w

    async def _replace(self, w: _Worker) -> None:
        await w._teardown()  # next acquirer restarts it; warm it in the background meanwhile
        if not self._started:
            return
        t = asyncio.create_task(self._warm(w))
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    @staticmethod
    async def _warm(w: _Worker) -> None:
        try:
            await w.ensure_started()
        except Exception:
            pass

    async def call(self, op: str, /, *, _timeout: float | None = None, **args: Any) -> Any:
        """Runs ``op`` on an idle worker. Raises BlenderOpError (op failed) or WorkerError."""
        w = await self._acquire()
        try:
            t = _timeout or self.timeout
            try:
                resp = await asyncio.wait_for(w._roundtrip_raw(op, args), t)
            except asyncio.TimeoutError as e:
                await self._replace(w)
                raise WorkerError("timeout", f"{op} exceeded {t}s; worker replaced") from e
            except (ConnectionError, proto.ProtocolError, OSError) as e:
                await self._replace(w)
                raise WorkerError("died", f"{op}: worker connection lost ({type(e).__name__})") from e
            except asyncio.CancelledError:
                await self._replace(w)  # an in-flight reply would desync the stream
                raise
            if not resp.get("ok"):
                err = resp.get("error", {})
                raise BlenderOpError(err.get("cls", "Error"), err.get("msg", ""), err.get("trace_tail", ""))
            return _materialize(resp.get("result"))
        finally:
            self._idle.put_nowait(w)

    async def map(self, op: str, args_list: list[dict[str, Any]], *, return_exceptions: bool = False) -> list[Any]:
        """Parallel fan-out (e.g. best-of-N seeds); results keep input order."""
        return await asyncio.gather(*(self.call(op, **a) for a in args_list), return_exceptions=return_exceptions)

    async def stop(self) -> None:
        self._started = False
        for t in list(self._bg):
            t.cancel()
        if self._bg:
            await asyncio.gather(*self._bg, return_exceptions=True)
        await asyncio.gather(*(w.shutdown() for w in self._workers), return_exceptions=True)
        for w in self._workers:
            self._sweep_shm(w.shm_prefix)
        self._workers = []
        self._idle = asyncio.Queue()
        if self.tmpdir:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
            self.tmpdir = ""

    async def __aenter__(self) -> BlenderPool:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

