"""Wire protocol between the pool and a Blender daemon.

Framing: 4-byte big-endian length + MessagePack map.
Request  ``{id, op, args}``; response ``{id, ok: True, result}`` or
``{id, ok: False, error: {cls, msg, trace_tail}}``.

Bulk buffers never cross the socket. The producer publishes them with
:meth:`ShmArray.publish` and the message carries ``{shm, shape, dtype}``.

Shared-memory ownership: the *creator* (normally the daemon) is the only party
that unlinks. Consumers ``attach`` (copying out or viewing) and only ``close``.
All segments are opened with ``track=False`` (Python >= 3.13) so no
``resource_tracker`` process is involved: attach-only processes therefore cannot
double-unlink or emit the "leaked shared_memory" warning, and the daemon never
spawns a tracker (inside Blender ``sys.executable`` is not a plain Python).
On older Pythons we fall back to unregistering the attached segment manually.

This module depends only on the stdlib, numpy and msgpack so the daemon can load
it by file path inside Blender's Python.
"""

from __future__ import annotations

import asyncio
import socket
import struct
import sys
from multiprocessing import shared_memory
from typing import Any

import msgpack
import numpy as np

MAX_FRAME = 256 * 1024 * 1024
_HDR = struct.Struct(">I")


class ProtocolError(Exception):
    """Malformed frame or stream closed mid-frame."""


def pack(msg: dict[str, Any]) -> bytes:
    body = msgpack.packb(msg, use_bin_type=True)
    if len(body) > MAX_FRAME:
        raise ProtocolError(f"frame too large: {len(body)} bytes (use shm)")
    return _HDR.pack(len(body)) + body


def unpack(body: bytes) -> dict[str, Any]:
    msg = msgpack.unpackb(body, raw=False)
    if not isinstance(msg, dict):
        raise ProtocolError("frame is not a map")
    return msg


def _check_len(n: int) -> int:
    if n > MAX_FRAME:
        raise ProtocolError(f"declared frame length {n} exceeds limit")
    return n


def _recv_exact(sock: socket.socket, n: int, *, eof_ok: bool = False) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            if eof_ok and not buf:
                return None
            raise ProtocolError(f"stream closed after {len(buf)}/{n} bytes")
        buf += chunk
    return bytes(buf)


def send_msg(sock: socket.socket, msg: dict[str, Any]) -> None:
    sock.sendall(pack(msg))


def recv_msg(sock: socket.socket) -> dict[str, Any] | None:
    """Returns the next message, or None on a clean EOF at a frame boundary."""
    hdr = _recv_exact(sock, _HDR.size, eof_ok=True)
    if hdr is None:
        return None
    body = _recv_exact(sock, _check_len(_HDR.unpack(hdr)[0]))
    return unpack(body or b"")


async def asend_msg(writer: asyncio.StreamWriter, msg: dict[str, Any]) -> None:
    writer.write(pack(msg))
    await writer.drain()


async def arecv_msg(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    try:
        hdr = await reader.readexactly(_HDR.size)
    except asyncio.IncompleteReadError as e:
        if not e.partial:
            return None
        raise ProtocolError("stream closed inside frame header") from e
    try:
        body = await reader.readexactly(_check_len(_HDR.unpack(hdr)[0]))
    except asyncio.IncompleteReadError as e:
        raise ProtocolError("stream closed inside frame body") from e
    return unpack(body)


def request(id_: int, op: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": id_, "op": op, "args": args or {}}


def ok_response(id_: int, result: Any) -> dict[str, Any]:
    return {"id": id_, "ok": True, "result": result}


def error_response(id_: int, exc: BaseException, trace_tail: str = "") -> dict[str, Any]:
    return {"id": id_, "ok": False,
            "error": {"cls": type(exc).__name__, "msg": str(exc), "trace_tail": trace_tail}}


def _open_shm(*, name: str | None = None, create: bool = False, size: int = 0) -> shared_memory.SharedMemory:
    if sys.version_info >= (3, 13):
        return shared_memory.SharedMemory(name=name, create=create, size=size, track=False)
    shm = shared_memory.SharedMemory(name=name, create=create, size=size)
    if not create:  # attach-only: stop the tracker from unlinking the creator's segment
        from multiprocessing import resource_tracker
        try:
            resource_tracker.unregister(shm._name, "shared_memory")  # type: ignore[attr-defined]
        except Exception:
            pass
    return shm


class ShmArray:
    """A numpy array backed by a named shared-memory segment.

    ``publish`` makes the creator (``owner=True``); only the owner may ``unlink``.
    ``attach`` yields a non-owning view whose ``unlink`` is a no-op error.
    Use as a context manager: it closes, and unlinks iff owner.
    """

    def __init__(self, shm: shared_memory.SharedMemory, shape: tuple[int, ...], dtype: np.dtype, owner: bool):
        self._shm = shm
        self._closed = False
        self.name = shm.name
        self.owner = owner
        self.array: np.ndarray = np.ndarray(shape, dtype=dtype, buffer=shm.buf)
        self._unlinked = False

    @classmethod
    def publish(cls, arr: np.ndarray, name: str | None = None) -> ShmArray:
        arr = np.ascontiguousarray(arr)
        shm = _open_shm(name=name, create=True, size=max(arr.nbytes, 1))
        try:
            out = cls(shm, arr.shape, arr.dtype, owner=True)
            out.array[...] = arr
        except BaseException:
            shm.close()
            shm.unlink()
            raise
        return out

    @classmethod
    def attach(cls, desc: dict[str, Any]) -> ShmArray:
        shm = _open_shm(name=desc["shm"])
        try:
            return cls(shm, tuple(desc["shape"]), np.dtype(desc["dtype"]), owner=False)
        except BaseException:
            shm.close()
            raise

    def descriptor(self) -> dict[str, Any]:
        return {"shm": self.name, "shape": list(self.array.shape), "dtype": self.array.dtype.str}

    def copy(self) -> np.ndarray:
        return self.array.copy()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.array = np.empty(0)  # drop the exported buffer or close() raises BufferError
            self._shm.close()

    def unlink(self) -> None:
        if not self.owner:
            raise PermissionError("only the creator of a shm segment may unlink it")
        if not self._unlinked:
            self._unlinked = True
            try:
                self._shm.unlink()
            except FileNotFoundError:
                pass

    def __enter__(self) -> ShmArray:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
        if self.owner:
            self.unlink()


def read_shm_copy(desc: dict[str, Any]) -> np.ndarray:
    """Attach, copy out, close (never unlinks). The caller owns the returned array."""
    sa = ShmArray.attach(desc)
    try:
        return sa.copy()
    finally:
        sa.close()


def is_shm_desc(obj: Any) -> bool:
    return isinstance(obj, dict) and "shm" in obj and "shape" in obj and "dtype" in obj
