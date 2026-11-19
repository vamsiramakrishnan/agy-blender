import os
import socket
import threading

import numpy as np
import pytest

from antigravity3d.adapters.blender_5_3 import protocol as P


def _shm_names(prefix: str = "") -> set[str]:
    return {n for n in os.listdir("/dev/shm") if n.startswith(prefix or "agy_t")}


def test_roundtrip_and_partial_reads():
    a, b = socket.socketpair()
    msg = P.request(7, "render", {"w": 3, "blob": b"\x00" * 5000, "nested": {"x": [1, 2.5, None]}})
    raw = P.pack(msg)

    def dribble():
        for i in range(0, len(raw), 7):
            a.sendall(raw[i:i + 7])
        a.close()

    t = threading.Thread(target=dribble)
    t.start()
    assert P.recv_msg(b) == msg
    assert P.recv_msg(b) is None  # clean EOF at boundary
    t.join()


def test_truncated_frame_raises():
    a, b = socket.socketpair()
    a.sendall(P.pack({"id": 1})[:-2])
    a.close()
    with pytest.raises(P.ProtocolError):
        P.recv_msg(b)


def test_oversize_declared_length_rejected():
    a, b = socket.socketpair()
    a.sendall((P.MAX_FRAME + 1).to_bytes(4, "big"))
    with pytest.raises(P.ProtocolError):
        P.recv_msg(b)


async def test_async_roundtrip():
    import asyncio
    reader = asyncio.StreamReader()
    msg = P.ok_response(1, {"a": [1, 2]})
    raw = P.pack(msg)
    reader.feed_data(raw[:3])
    reader.feed_data(raw[3:])
    reader.feed_eof()
    assert await P.arecv_msg(reader) == msg
    assert await P.arecv_msg(reader) is None


def test_error_response_shape():
    r = P.error_response(3, ValueError("boom"), "tb")
    assert r == {"id": 3, "ok": False, "error": {"cls": "ValueError", "msg": "boom", "trace_tail": "tb"}}


def test_shm_roundtrip_1024_float32_no_leak():
    before = _shm_names("agy_t")
    arr = np.random.default_rng(0).random((1024, 1024, 4), dtype=np.float32)
    owner = P.ShmArray.publish(arr, name="agy_t_big")
    desc = owner.descriptor()
    assert desc["shape"] == [1024, 1024, 4] and desc["dtype"] == "<f4"
    assert P.unpack(P.pack({"r": desc})[4:])["r"] == desc  # descriptor survives the wire
    out = P.read_shm_copy(desc)
    np.testing.assert_array_equal(out, arr)
    peer = P.ShmArray.attach(desc)
    with pytest.raises(PermissionError):
        peer.unlink()
    peer.close()
    assert "agy_t_big" in _shm_names("agy_t")
    owner.close()
    owner.unlink()
    owner.unlink()  # idempotent
    assert _shm_names("agy_t") == before


def test_shm_context_manager_unlinks_on_error():
    with pytest.raises(RuntimeError):
        with P.ShmArray.publish(np.zeros(8, np.float32), name="agy_t_ctx"):
            assert "agy_t_ctx" in _shm_names("agy_t")
            raise RuntimeError
    assert "agy_t_ctx" not in _shm_names("agy_t")


def test_attach_missing_raises():
    with pytest.raises(FileNotFoundError):
        P.ShmArray.attach({"shm": "agy_t_nope", "shape": [1], "dtype": "<f4"})
