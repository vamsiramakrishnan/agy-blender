"""Content-addressed Merkle build cache.

Every pipeline node is keyed by a deterministic hash of its op, params, the keys
of its upstream nodes and the backend version, so an unchanged node is a
zero-cost hit (shareable across a team via a remote blob store) and any
upstream change invalidates exactly the downstream nodes.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import io
import json
import os
import re
import struct
import tempfile
import threading
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import numpy as np
from pydantic import BaseModel

from antigravity3d.ir import SceneIR

# --- stable hashing -----------------------------------------------------------


def _frame(h: "hashlib._Hash", tag: bytes, payload: bytes) -> None:
    h.update(tag)
    h.update(struct.pack("<Q", len(payload)))
    h.update(payload)


def _enc(obj: Any, h: "hashlib._Hash") -> None:
    # bool must precede int (bool is an int subclass).
    if obj is None:
        _frame(h, b"N", b"")
    elif isinstance(obj, bool):
        _frame(h, b"B", b"1" if obj else b"0")
    elif isinstance(obj, int):
        _frame(h, b"I", str(int(obj)).encode())
    elif isinstance(obj, float):
        # repr is the shortest round-trip form: bit-exact, so 0.0 != -0.0 and
        # nan hashes as 'nan'. int 1 and float 1.0 are distinct types and differ.
        _frame(h, b"F", repr(float(obj)).encode())
    elif isinstance(obj, str):
        _frame(h, b"S", obj.encode("utf-8"))
    elif isinstance(obj, (bytes, bytearray, memoryview)):
        _frame(h, b"Y", bytes(obj))
    elif isinstance(obj, np.ndarray):
        if obj.dtype.hasobject:
            raise TypeError("stable_hash: object-dtype numpy arrays are not supported")
        a = np.ascontiguousarray(obj)
        _frame(h, b"A", f"{a.dtype.str}|{a.shape}".encode() + b"|" + a.tobytes())
    elif isinstance(obj, np.generic):
        _enc(obj.item(), h)
    elif isinstance(obj, BaseModel):
        _frame(h, b"M", type(obj).__qualname__.encode())
        _enc(obj.model_dump(mode="json"), h)
    elif dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        _frame(h, b"D", type(obj).__qualname__.encode())
        _enc({f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)}, h)
    elif isinstance(obj, dict):
        if not all(isinstance(k, str) for k in obj):
            raise TypeError("stable_hash: dict keys must be str")
        _frame(h, b"{", struct.pack("<Q", len(obj)))
        for k in sorted(obj):
            _enc(k, h)
            _enc(obj[k], h)
    elif isinstance(obj, (list, tuple)):
        # list and tuple are deliberately equivalent (JSON round-trips lose the distinction).
        _frame(h, b"[", struct.pack("<Q", len(obj)))
        for v in obj:
            _enc(v, h)
    else:
        raise TypeError(f"stable_hash: unsupported type {type(obj).__qualname__}")


def stable_hash(obj: Any) -> str:
    """sha256 hex of a canonical encoding of ``obj``.

    Supported: None, bool, int, float, str, bytes, list/tuple (equivalent),
    dict with str keys (order-insensitive), pydantic models, dataclasses, numpy
    arrays (dtype+shape+bytes) and numpy scalars (as Python scalars). Types are
    part of the encoding: ``1`` != ``1.0`` != ``True`` and ``0.0`` != ``-0.0``.
    Anything else (sets, Path, arbitrary objects) raises ``TypeError``.
    """
    h = hashlib.sha256()
    _enc(obj, h)
    return h.hexdigest()


def node_key(
    op: str, *, inputs: dict, upstream: Sequence[str] = (), backend_version: str
) -> str:
    """Merkle step: covers op, own params, upstream keys (ordered) and backend version."""
    return stable_hash(
        {"op": op, "inputs": inputs, "upstream": list(upstream), "backend": backend_version}
    )


def ir_subtree_key(scene: SceneIR, node_id: str) -> str:
    """Hash of a node, its descendants and the materials they reference.

    The root's own ``parent`` is excluded (the key describes local subtree content;
    ancestors' transforms belong to the caller's upstream keys). Siblings and
    unreferenced materials do not affect the key.
    """
    by_id = {n.id: n for n in scene.nodes}
    if node_id not in by_id:
        raise KeyError(node_id)
    children: dict[str, list[str]] = {}
    for n in scene.nodes:
        if n.parent is not None:
            children.setdefault(n.parent, []).append(n.id)
    ids, stack = [], [node_id]
    while stack:
        cur = stack.pop()
        ids.append(cur)
        stack.extend(children.get(cur, ()))
    ids.sort()
    nodes = []
    for i in ids:
        d = by_id[i].model_dump(mode="json")
        if i == node_id:
            d["parent"] = None
        nodes.append(d)
    mat_ids = {by_id[i].material_id for i in ids} - {None}
    mats = [
        m.model_dump(mode="json", exclude={"surface"})  # texture-prompt words: no geometry or export depends on them
        for m in sorted(scene.materials, key=lambda m: m.id)
        if m.id in mat_ids
    ]
    return stable_hash({"root": node_id, "nodes": nodes, "materials": mats})


# --- blob stores --------------------------------------------------------------

_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{3,200}$")


@runtime_checkable
class BlobStore(Protocol):
    def get(self, key: str) -> bytes | None: ...
    def put(self, key: str, data: bytes) -> None: ...
    def has(self, key: str) -> bool: ...


class LocalBlobStore:
    """Sharded directory store. File layout: sha256(data) (32 bytes) + data.

    Writes go to a temp file in the target directory then ``os.replace`` (atomic);
    reads verify the digest and treat a corrupt/truncated blob as a miss.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        if not _KEY_RE.match(key):
            raise ValueError(f"invalid cache key {key!r}")
        return self.root / key[:2] / key[2:4] / key

    @contextlib.asynccontextmanager
    async def writing(self, key: str):
        """Coordinate paid cache fills across workers sharing a POSIX cache volume."""
        if os.name != "posix":
            yield
            return
        import fcntl

        path = self._path(key).with_suffix(".lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Retain the lock inode. Unlinking while a waiter is opening it breaks mutual exclusion.
        with path.open("a+b") as file:
            while True:
                try:
                    fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.02)
            yield

    def get(self, key: str) -> bytes | None:
        try:
            raw = self._path(key).read_bytes()
        except FileNotFoundError:
            return None
        digest, data = raw[:32], raw[32:]
        if len(digest) != 32 or hashlib.sha256(data).digest() != digest:
            return None
        return data

    def has(self, key: str) -> bool:
        return self.get(key) is not None

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(hashlib.sha256(data).digest())
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise


class GCSBlobStore:
    """Google Cloud Storage store; requires the ``gcs`` extra (google-cloud-storage)."""

    def __init__(self, bucket: str, prefix: str = "") -> None:
        try:
            from google.cloud import storage  # type: ignore[import-not-found]
        except ImportError as e:
            raise ImportError(
                "GCSBlobStore requires the 'gcs' extra: pip install 'antigravity3d[gcs]' "
                "(google-cloud-storage)"
            ) from e
        self._bucket = storage.Client().bucket(bucket)
        self._prefix = prefix.strip("/")

    def _blob(self, key: str):
        return self._bucket.blob(f"{self._prefix}/{key}" if self._prefix else key)

    def get(self, key: str) -> bytes | None:
        blob = self._blob(key)
        if not blob.exists():
            return None
        raw = blob.download_as_bytes()
        digest, data = raw[:32], raw[32:]
        return data if hashlib.sha256(data).digest() == digest else None

    def put(self, key: str, data: bytes) -> None:
        self._blob(key).upload_from_string(hashlib.sha256(data).digest() + data)

    def has(self, key: str) -> bool:
        return self._blob(key).exists()


class TieredStore:
    """Read-through with local back-fill; write-through to both tiers."""

    def __init__(self, local: BlobStore, remote: BlobStore) -> None:
        self.local, self.remote = local, remote

    def get(self, key: str) -> bytes | None:
        data = self.local.get(key)
        if data is not None:
            return data
        data = self.remote.get(key)
        if data is not None:
            self.local.put(key, data)
        return data

    def put(self, key: str, data: bytes) -> None:
        self.local.put(key, data)
        self.remote.put(key, data)

    def has(self, key: str) -> bool:
        return self.local.has(key) or self.remote.has(key)


# --- build cache --------------------------------------------------------------


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    deduped: int = 0  # async followers that shared an in-flight computation (also counted as hits)
    bytes_saved: int = 0
    saved_cost_usd: float = 0.0
    saved_ms: float = 0.0


class BuildCache:
    def __init__(self, store: BlobStore) -> None:
        self.store = store
        self.stats = CacheStats()
        self._lock = threading.Lock()
        self._inflight: dict[str, asyncio.Future[bytes]] = {}
        # Files-API URI -> sha256 of the bytes uploaded under it, for the uploads made through this cache's forge. A URI
        # changes with every upload (they expire), so a key over a request that references a file uses its content.
        self._uploads: dict[str, str] = {}

    def note_upload(self, uri: str, data: bytes) -> None:
        with self._lock:
            self._uploads[uri] = hashlib.sha256(data).hexdigest()

    def upload_digest(self, uri: str) -> str | None:
        """sha256 of what was uploaded as ``uri`` through this cache, or None when it was not."""
        with self._lock:
            return self._uploads.get(uri)

    def _hit(self, data: bytes, cost: float, ms: float, deduped: bool = False) -> None:
        with self._lock:
            s = self.stats
            s.hits += 1
            s.deduped += deduped
            s.bytes_saved += len(data)
            s.saved_cost_usd += cost
            s.saved_ms += ms

    def credit_savings(self, *, cost_usd: float, ms: float) -> None:
        """Credit metered savings known only after a shared in-flight build finishes."""
        with self._lock:
            self.stats.saved_cost_usd += cost_usd
            self.stats.saved_ms += ms

    def _miss(self) -> None:
        with self._lock:
            self.stats.misses += 1

    def get_or_compute(
        self,
        key: str,
        compute: Callable[[], bytes],
        *,
        saved_cost_usd: float = 0.0,
        saved_ms: float = 0.0,
    ) -> tuple[bytes, bool]:
        """Returns (data, hit). ``saved_*`` are credited to stats only on a hit."""
        data = self.store.get(key)
        if data is not None:
            self._hit(data, saved_cost_usd, saved_ms)
            return data, True
        self._miss()
        data = compute()
        self.store.put(key, data)
        return data, False

    async def aget_or_compute(
        self,
        key: str,
        compute: Callable[[], Awaitable[bytes]],
        *,
        saved_cost_usd: float = 0.0,
        saved_ms: float = 0.0,
    ) -> tuple[bytes, bool]:
        """Single-flight: concurrent callers of one key share one ``compute()`` call.

        Must be used from a single event loop. Followers return hit=True.
        """
        fut = self._inflight.get(key)
        if fut is not None:
            data = await asyncio.shield(fut)
            self._hit(data, saved_cost_usd, saved_ms, deduped=True)
            return data, True
        fut = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            data = await asyncio.to_thread(self.store.get, key)
            if data is None:
                local = self.store.local if isinstance(self.store, TieredStore) else self.store
                guard = local.writing(key) if isinstance(local, LocalBlobStore) else contextlib.nullcontext()
                async with guard:
                    if isinstance(local, LocalBlobStore):
                        data = await asyncio.to_thread(self.store.get, key)
                    if data is None:
                        self._miss()
                        data = await compute()
                        # A write in a thread cannot be interrupted: on cancellation (a stopped step) wait for it, so
                        # nothing is still writing into the cache once the step reports stopped.
                        put = asyncio.ensure_future(asyncio.to_thread(self.store.put, key, data))
                        try:
                            await asyncio.shield(put)
                        except asyncio.CancelledError:
                            await put
                            raise
                        fut.set_result(data)
                        return data, False
            self._hit(data, saved_cost_usd, saved_ms)
            fut.set_result(data)
            return data, True
        except BaseException as e:
            if not fut.done():
                fut.set_exception(e)
                fut.exception()  # mark retrieved so a follower-less failure does not warn
            raise
        finally:
            self._inflight.pop(key, None)

    def get_or_compute_json(
        self, key: str, compute: Callable[[], Any], **kw: float
    ) -> tuple[Any, bool]:
        data, hit = self.get_or_compute(
            key, lambda: json.dumps(compute(), sort_keys=True, separators=(",", ":")).encode(), **kw
        )
        return json.loads(data), hit

    def get_or_compute_npz(
        self, key: str, compute: Callable[[], dict[str, np.ndarray]], **kw: float
    ) -> tuple[dict[str, np.ndarray], bool]:
        def build() -> bytes:
            buf = io.BytesIO()
            np.savez(buf, **compute())
            return buf.getvalue()

        data, hit = self.get_or_compute(key, build, **kw)
        with np.load(io.BytesIO(data), allow_pickle=False) as z:
            return {k: z[k] for k in z.files}, hit


__all__ = [
    "BlobStore", "BuildCache", "CacheStats", "GCSBlobStore", "LocalBlobStore", "TieredStore",
    "ir_subtree_key", "node_key", "stable_hash",
]
