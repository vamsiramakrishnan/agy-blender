"""Managed warm Blender pool, shared by every agent process through one socket.

Run as the ``blender_pool`` plugin sidecar::

    python -m antigravity3d.runtime.worker_daemon --workers 4 [--fake]

It starts :class:`~antigravity3d.adapters.blender_5_3.pool.BlenderPool` and
listens on a single front Unix socket (``--socket`` / ``$AGY3D_POOL_SOCKET``,
default ``/tmp/agy3d/pool.sock``). Tool code in any agent process talks to it
with :class:`PoolClient`, so all tools share one warm pool instead of each
spawning Blender. The Antigravity sidecar manager owns the process lifetime
(``restart_policy: always``); this module only has to start, serve, and exit
cleanly on SIGTERM / SIGINT.

Wire format: exactly the framing of ``adapters/blender_5_3/protocol.py``
(4-byte big-endian length + msgpack map). Requests ``{id, op, args}``;
responses ``{id, ok, result | error}``. Requests on one connection may be
pipelined; responses carry the request id and can arrive out of order.

Control ops (handled here, never forwarded): ``__pool_info__`` returns
``{workers, pids, fake, socket}``; ``__release__`` (no response) releases
shared-memory segments the client has finished copying.

Bulk buffers (decision): **every numpy array in a result is forwarded through a
fresh** :class:`~antigravity3d.adapters.blender_5_3.protocol.ShmArray` owned by
this daemon, never inline. That keeps the protocol's rule that bulk data never
crosses the socket and keeps frames far below ``MAX_FRAME`` regardless of
render size. The pool has already copied the worker's segment out (so a worker
can be recycled independently); the daemon republishes it under its own prefix,
the client copies it out (``read_shm_copy``) and sends ``__release__`` with the
names. Segments are also unlinked when the client disconnects and at shutdown,
and ``/dev/shm/<prefix>*`` is swept on exit, so a crashed client cannot leak.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import glob
import itertools
import os
import signal
import socket as _socket
import sys
import uuid
from pathlib import Path
from typing import Any

import numpy as np

from antigravity3d.adapters.blender_5_3 import protocol as proto
from antigravity3d.adapters.blender_5_3.pool import BlenderOpError, BlenderPool, WorkerError

DEFAULT_SOCKET = "/tmp/agy3d/pool.sock"
POOL_INFO_OP = "__pool_info__"
RELEASE_OP = "__release__"


def default_socket_path() -> str:
    return os.environ.get("AGY3D_POOL_SOCKET") or DEFAULT_SOCKET


# --------------------------------------------------------------------------- server


class PoolServer:
    """Serves ``pool.call`` over one Unix socket. ``serve()`` until ``stop()``."""

    def __init__(self, pool: BlenderPool, socket_path: str):
        if len(socket_path.encode()) >= 104:
            raise ValueError(f"socket path too long for AF_UNIX: {socket_path}")
        self.pool = pool
        self.socket_path = socket_path
        self.prefix = f"agy3dfront_{os.getpid()}_{uuid.uuid4().hex[:6]}_"
        self._names = itertools.count(1)
        self._server: asyncio.base_events.Server | None = None
        self._conn_tasks: set[asyncio.Task[None]] = set()
        self._owned: dict[str, proto.ShmArray] = {}
        self._stopping = asyncio.Event()

    # ---- lifecycle
    async def start(self) -> None:
        path = Path(self.socket_path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.exists() or path.is_socket():
            if _socket_alive(self.socket_path):
                raise RuntimeError(f"another pool daemon is already serving {self.socket_path}")
            path.unlink()  # stale socket from a crashed run
        await self.pool.start()
        try:
            self._server = await asyncio.start_unix_server(self._on_conn, path=self.socket_path)
            os.chmod(self.socket_path, 0o600)
        except BaseException:
            await self.pool.stop()
            raise

    async def stop(self) -> None:
        self._stopping.set()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
        for t in list(self._conn_tasks):
            t.cancel()
        if self._conn_tasks:
            await asyncio.gather(*self._conn_tasks, return_exceptions=True)
        await self.pool.stop()
        for name in list(self._owned):
            self._release(name)
        for p in glob.glob(f"/dev/shm/{self.prefix}*"):
            with contextlib.suppress(OSError):
                os.unlink(p)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.socket_path)

    async def wait_stopped(self) -> None:
        await self._stopping.wait()

    # ---- shm forwarding
    def _publish(self, arr: np.ndarray) -> dict[str, Any]:
        sa = proto.ShmArray.publish(arr, name=f"{self.prefix}{next(self._names)}")
        self._owned[sa.name] = sa
        return sa.descriptor()

    def _release(self, name: str) -> None:
        sa = self._owned.pop(name, None)
        if sa is not None:
            sa.close()
            sa.unlink()

    def _encode(self, obj: Any, published: list[str]) -> Any:
        if isinstance(obj, np.ndarray):
            desc = self._publish(obj)
            published.append(desc["shm"])
            return desc
        if isinstance(obj, np.generic):
            return obj.item()
        if isinstance(obj, dict):
            return {k: self._encode(v, published) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self._encode(v, published) for v in obj]
        return obj

    # ---- connection handling
    async def _on_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._conn_tasks.add(task)
        wlock = asyncio.Lock()
        inflight: set[asyncio.Task[None]] = set()
        conn_owned: set[str] = set()
        try:
            while True:
                msg = await proto.arecv_msg(reader)
                if msg is None:
                    break
                if msg.get("op") == RELEASE_OP:
                    for name in (msg.get("args") or {}).get("shm", []):
                        if name in conn_owned:
                            conn_owned.discard(name)
                            self._release(name)
                    continue
                t = asyncio.create_task(self._handle(msg, writer, wlock, conn_owned))
                inflight.add(t)
                t.add_done_callback(inflight.discard)
        except (proto.ProtocolError, ConnectionError, OSError):
            pass
        except asyncio.CancelledError:
            pass
        finally:
            for t in list(inflight):
                t.cancel()
            if inflight:
                await asyncio.gather(*inflight, return_exceptions=True)
            for name in conn_owned:
                self._release(name)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            self._conn_tasks.discard(task)

    async def _handle(self, msg: dict[str, Any], writer: asyncio.StreamWriter, wlock: asyncio.Lock,
                      conn_owned: set[str]) -> None:
        mid = msg.get("id", 0)
        op = msg.get("op")
        args = msg.get("args") or {}
        published: list[str] = []
        try:
            if not isinstance(op, str) or not isinstance(args, dict):
                raise proto.ProtocolError("request needs a string op and a map of args")
            if op == POOL_INFO_OP:
                result: Any = {"workers": self.pool.size, "pids": self.pool.worker_pids,
                               "fake": self.pool.fake, "socket": self.socket_path}
            else:
                timeout = args.pop("_timeout", None)
                result = await self.pool.call(op, _timeout=timeout, **args)
            resp = proto.ok_response(mid, self._encode(result, published))
        except asyncio.CancelledError:
            for n in published:
                self._release(n)
            raise
        except BlenderOpError as e:
            resp = {"id": mid, "ok": False, "error": {"cls": e.cls, "msg": e.msg, "trace_tail": e.trace_tail}}
        except WorkerError as e:
            resp = {"id": mid, "ok": False, "error": {"cls": "WorkerError", "kind": e.kind, "msg": str(e),
                                                       "trace_tail": ""}}
        except Exception as e:  # noqa: BLE001 - every failure must become a response
            resp = proto.error_response(mid, e)
        conn_owned.update(published)
        try:
            async with wlock:
                await proto.asend_msg(writer, resp)
        except (ConnectionError, OSError):
            for n in published:
                conn_owned.discard(n)
                self._release(n)


def _socket_alive(path: str) -> bool:
    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(path)
        return True
    except OSError:
        return False
    finally:
        s.close()


# --------------------------------------------------------------------------- client


class PoolClient:
    """Async client for the front socket. One connection, pipelined, safe to share across tasks.

    ``call(op, **args)`` mirrors :meth:`BlenderPool.call`: it returns the result with
    every forwarded array copied into an ordinary numpy array the caller owns, and
    raises :class:`BlenderOpError` / :class:`WorkerError` like the in-process pool.
    """

    def __init__(self, socket_path: str | None = None, *, connect_timeout: float = 10.0):
        self.socket_path = socket_path or default_socket_path()
        self.connect_timeout = connect_timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._wlock = asyncio.Lock()
        self._rx: asyncio.Task[None] | None = None

    async def connect(self) -> PoolClient:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.connect_timeout
        while True:
            try:
                self._reader, self._writer = await asyncio.open_unix_connection(self.socket_path)
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if loop.time() >= deadline:
                    raise ConnectionError(f"no pool daemon at {self.socket_path}") from None
                await asyncio.sleep(0.05)
        self._rx = asyncio.create_task(self._read_loop())
        return self

    async def _read_loop(self) -> None:
        assert self._reader
        err: BaseException = ConnectionError("pool daemon closed the connection")
        try:
            while True:
                msg = await proto.arecv_msg(self._reader)
                if msg is None:
                    break
                fut = self._pending.pop(msg.get("id"), None)
                if fut is not None and not fut.done():
                    fut.set_result(msg)
        except asyncio.CancelledError:
            err = ConnectionError("client closed")
        except Exception as e:  # noqa: BLE001
            err = e
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(err)
        self._pending.clear()

    async def _send(self, msg: dict[str, Any]) -> None:
        if self._writer is None:
            await self.connect()
        assert self._writer
        async with self._wlock:
            await proto.asend_msg(self._writer, msg)

    async def _request(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        mid = next(self._ids)
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        try:
            await self._send(proto.request(mid, op, args))
        except BaseException:
            self._pending.pop(mid, None)
            raise
        return await fut

    async def call(self, op: str, /, *, _timeout: float | None = None, **args: Any) -> Any:
        if _timeout is not None:
            args["_timeout"] = _timeout
        resp = await self._request(op, args)
        if not resp.get("ok"):
            err = resp.get("error") or {}
            if err.get("cls") == "WorkerError":
                raise WorkerError(err.get("kind", "died"), err.get("msg", ""))
            raise BlenderOpError(err.get("cls", "Error"), err.get("msg", ""), err.get("trace_tail", ""))
        names: list[str] = []
        try:
            return self._materialize(resp.get("result"), names)
        finally:
            if names:
                with contextlib.suppress(Exception):
                    await self._send({"id": 0, "op": RELEASE_OP, "args": {"shm": names}})

    def _materialize(self, obj: Any, names: list[str]) -> Any:
        if proto.is_shm_desc(obj):
            names.append(obj["shm"])
            return proto.read_shm_copy(obj)
        if isinstance(obj, dict):
            return {k: self._materialize(v, names) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._materialize(v, names) for v in obj]
        return obj

    async def map(self, op: str, args_list: list[dict[str, Any]], *, return_exceptions: bool = False) -> list[Any]:
        return await asyncio.gather(*(self.call(op, **a) for a in args_list), return_exceptions=return_exceptions)

    async def pool_info(self) -> dict[str, Any]:
        return await self.call(POOL_INFO_OP)

    async def close(self) -> None:
        w, self._writer = self._writer, None
        if w is not None:
            w.close()
            with contextlib.suppress(Exception):
                await w.wait_closed()
        if self._rx is not None:
            self._rx.cancel()
            with contextlib.suppress(BaseException):
                await self._rx
            self._rx = None

    async def __aenter__(self) -> PoolClient:
        return await self.connect()

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


# --------------------------------------------------------------------------- main


async def _amain(ns: argparse.Namespace) -> int:
    pool = BlenderPool(ns.workers, blender_bin=ns.blender_bin, fake=ns.fake, timeout=ns.timeout)
    server = PoolServer(pool, ns.socket)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        await server.start()
    except Exception as e:  # noqa: BLE001
        print(f"[blender_pool] failed to start: {e}", file=sys.stderr, flush=True)
        return 1
    print(f"[blender_pool] READY socket={ns.socket} workers={ns.workers} fake={ns.fake} "
          f"pids={pool.worker_pids}", flush=True)
    try:
        await stop.wait()
    finally:
        print("[blender_pool] shutting down", flush=True)
        await server.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="antigravity3d.runtime.worker_daemon", description=__doc__.splitlines()[0])
    ap.add_argument("--workers", type=int, default=int(os.environ.get("AGY3D_POOL_WORKERS", "4")))
    ap.add_argument("--socket", default=default_socket_path())
    ap.add_argument("--blender-bin", default=None, help="defaults to $BLENDER_BIN, then `blender` on PATH")
    ap.add_argument("--timeout", type=float, default=120.0, help="per-call timeout in seconds")
    ap.add_argument("--fake", action="store_true", help="plain-CPython fake daemons (tests, no Blender)")
    ns = ap.parse_args(argv)
    if ns.workers < 1:
        ap.error("--workers must be >= 1")
    return asyncio.run(_amain(ns))


if __name__ == "__main__":
    sys.exit(main())
