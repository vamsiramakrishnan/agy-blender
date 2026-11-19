from __future__ import annotations

import asyncio
import dataclasses
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from antigravity3d.ir import MaterialIR, NodeIR, SceneIR, Transform
from antigravity3d.kernel.cache import (
    BuildCache,
    GCSBlobStore,
    LocalBlobStore,
    TieredStore,
    ir_subtree_key,
    node_key,
    stable_hash,
)


# --- stable_hash ---------------------------------------------------------------


def test_dict_order_irrelevant():
    assert stable_hash({"a": 1, "b": [1, 2]}) == stable_hash({"b": [1, 2], "a": 1})


def test_type_sensitivity():
    hs = {stable_hash(v) for v in (1, 1.0, True, "1", b"1", None, [1], 0.0, -0.0, [], {}, "")}
    assert len(hs) == 12
    assert stable_hash([1, 2]) == stable_hash((1, 2))  # documented equivalence
    assert stable_hash([[1], 2]) != stable_hash([1, [2]])
    assert stable_hash(["ab", "c"]) != stable_hash(["a", "bc"])


def test_numpy_dtype_shape_bytes():
    a = np.arange(6, dtype=np.float64)
    assert stable_hash(a) == stable_hash(a.copy())
    assert stable_hash(a) != stable_hash(a.astype(np.float32))
    assert stable_hash(a) != stable_hash(a.reshape(2, 3))
    assert stable_hash(a) == stable_hash(np.asfortranarray(a))
    b = a.copy()
    b[3] += 1e-12
    assert stable_hash(a) != stable_hash(b)


def test_models_and_dataclasses():
    t1, t2 = Transform(translate=(1, 2, 3)), Transform(translate=(1, 2, 3.5))
    assert stable_hash(t1) == stable_hash(Transform(translate=(1, 2, 3)))
    assert stable_hash(t1) != stable_hash(t2)

    @dataclasses.dataclass
    class D:
        x: int
        y: str

    assert stable_hash(D(1, "a")) == stable_hash(D(1, "a"))
    assert stable_hash(D(1, "a")) != stable_hash(D(2, "a"))


@pytest.mark.parametrize("bad", [{1, 2}, Path("x"), object(), {1: "a"}, lambda: 0])
def test_unsupported_types(bad):
    with pytest.raises(TypeError):
        stable_hash(bad)


def test_hash_stable_across_processes():
    code = (
        "from antigravity3d.kernel.cache import stable_hash;import numpy as np;"
        "print(stable_hash({'k':{'z','y'} and ['z','y'],'f':0.1+0.2,'a':np.arange(4.0),'n':None}))"
    )
    outs = {
        subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, check=True, env={"PYTHONHASHSEED": seed, "PATH": ""},
        ).stdout
        for seed in ("1", "2", "random")
    }
    assert len(outs) == 1
    here = stable_hash({"k": ["z", "y"], "f": 0.1 + 0.2, "a": np.arange(4.0), "n": None})
    assert outs.pop().strip() == here


# --- Merkle keys ---------------------------------------------------------------


def test_node_key_sensitivity():
    base = dict(inputs={"w": 3}, upstream=["u1"], backend_version="v1")
    k = node_key("bevel", **base)
    assert k == node_key("bevel", **base)
    assert k != node_key("bevel2", **base)
    assert k != node_key("bevel", **{**base, "inputs": {"w": 4}})
    assert k != node_key("bevel", **{**base, "upstream": ["u2"]})
    assert k != node_key("bevel", **{**base, "upstream": ["u1", "u2"]})
    assert k != node_key("bevel", **{**base, "backend_version": "v2"})


def test_merkle_invalidation_downstream_only():
    def chain(bevel_w: float):
        gen = node_key("gen", inputs={"prompt": "chair"}, backend_version="1")
        uv = node_key("uv", inputs={}, upstream=[gen], backend_version="1")
        bevel = node_key("bevel", inputs={"w": bevel_w}, upstream=[gen], backend_version="1")
        bake = node_key("bake", inputs={}, upstream=[bevel, uv], backend_version="1")
        return gen, uv, bevel, bake

    a, b = chain(0.01), chain(0.02)
    assert a[0] == b[0] and a[1] == b[1]  # upstream gen + sibling uv untouched
    assert a[2] != b[2] and a[3] != b[3]  # bevel and its downstream bake change


# --- ir_subtree_key --------------------------------------------------------------


def _scene() -> SceneIR:
    return SceneIR(
        asset_name="s",
        materials=[MaterialIR(id="m1"), MaterialIR(id="m2", roughness=0.1)],
        nodes=[
            NodeIR(id="root", kind="group"),
            NodeIR(id="a", kind="box", params={"size": (1.0, 1.0, 1.0)}, parent="root", material_id="m1"),
            NodeIR(id="a_child", kind="sphere", params={"radius": 0.2}, parent="a"),
            NodeIR(id="b", kind="box", params={"size": (2.0, 1.0, 1.0)}, parent="root", material_id="m2"),
        ],
    )


def test_subtree_key_isolated_from_sibling_edits():
    s = _scene()
    ka, kb = ir_subtree_key(s, "a"), ir_subtree_key(s, "b")
    edited = s.model_copy(
        update={
            "nodes": [
                n.model_copy(update={"transform": Transform(translate=(5, 0, 0))}) if n.id == "b" else n
                for n in s.nodes
            ],
            "materials": [m.model_copy(update={"roughness": 0.9}) if m.id == "m2" else m for m in s.materials],
        }
    )
    assert ir_subtree_key(edited, "a") == ka
    assert ir_subtree_key(edited, "b") != kb
    assert ir_subtree_key(edited, "root") != ir_subtree_key(s, "root")


def test_subtree_key_covers_descendants_and_materials():
    s = _scene()
    ka = ir_subtree_key(s, "a")
    child_edit = s.model_copy(
        update={"nodes": [n.model_copy(update={"params": {"radius": 0.3}}) if n.id == "a_child" else n for n in s.nodes]}
    )
    assert ir_subtree_key(child_edit, "a") != ka
    mat_edit = s.model_copy(
        update={"materials": [m.model_copy(update={"roughness": 0.9}) if m.id == "m1" else m for m in s.materials]}
    )
    assert ir_subtree_key(mat_edit, "a") != ka
    with pytest.raises(KeyError):
        ir_subtree_key(s, "nope")


# --- stores ----------------------------------------------------------------------

K = "ab" * 32


def test_local_store_roundtrip_and_sharding(tmp_path):
    st = LocalBlobStore(tmp_path)
    assert st.get(K) is None and not st.has(K)
    st.put(K, b"hello")
    assert st.get(K) == b"hello" and st.has(K)
    assert (tmp_path / K[:2] / K[2:4] / K).is_file()
    with pytest.raises(ValueError):
        st.put("../evil", b"x")


def test_corrupt_blob_is_miss(tmp_path):
    st = LocalBlobStore(tmp_path)
    st.put(K, b"payload-bytes")
    p = tmp_path / K[:2] / K[2:4] / K
    raw = bytearray(p.read_bytes())
    raw[-1] ^= 0xFF
    p.write_bytes(bytes(raw))
    assert st.get(K) is None
    p.write_bytes(b"short")
    assert st.get(K) is None
    cache = BuildCache(st)
    data, hit = cache.get_or_compute(K, lambda: b"fresh")
    assert (data, hit) == (b"fresh", False)
    assert st.get(K) == b"fresh"


def test_write_is_atomic(tmp_path, monkeypatch):
    st = LocalBlobStore(tmp_path)
    st.put(K, b"old")
    import os as _os

    def boom(*a, **k):
        raise OSError("crash before rename")

    monkeypatch.setattr("antigravity3d.kernel.cache.os.replace", boom)
    with pytest.raises(OSError):
        st.put(K, b"new")
    monkeypatch.undo()
    assert st.get(K) == b"old"  # reader never sees a partial/new blob
    assert not [p for p in (tmp_path / K[:2] / K[2:4]).iterdir() if p.name.startswith(".tmp-")]
    _ = _os


def test_gcs_missing_extra_error():
    try:
        import google.cloud.storage  # noqa: F401
        pytest.skip("google-cloud-storage installed")
    except ImportError:
        pass
    with pytest.raises(ImportError, match=r"gcs"):
        GCSBlobStore("bucket", "pfx")


class MemStore:
    def __init__(self):
        self.d: dict[str, bytes] = {}
        self.gets = 0

    def get(self, key):
        self.gets += 1
        return self.d.get(key)

    def put(self, key, data):
        self.d[key] = data

    def has(self, key):
        return key in self.d


def test_tiered_backfill_and_writethrough(tmp_path):
    local, remote = LocalBlobStore(tmp_path), MemStore()
    t = TieredStore(local, remote)
    remote.put(K, b"remote-data")
    assert not local.has(K)
    assert t.get(K) == b"remote-data"
    assert local.get(K) == b"remote-data"  # back-filled
    g = remote.gets
    assert t.get(K) == b"remote-data" and remote.gets == g  # now served locally
    k2 = "cd" * 32
    t.put(k2, b"x")
    assert local.get(k2) == b"x" and remote.d[k2] == b"x"
    assert t.get("ef" * 32) is None


# --- BuildCache ------------------------------------------------------------------


def test_stats_and_credit(tmp_path):
    c = BuildCache(LocalBlobStore(tmp_path))
    calls = []

    def work():
        calls.append(1)
        return b"12345"

    assert c.get_or_compute(K, work, saved_cost_usd=0.04, saved_ms=900) == (b"12345", False)
    assert c.stats.misses == 1 and c.stats.hits == 0 and c.stats.saved_cost_usd == 0
    for _ in range(2):
        assert c.get_or_compute(K, work, saved_cost_usd=0.04, saved_ms=900) == (b"12345", True)
    assert len(calls) == 1
    s = c.stats
    assert (s.hits, s.misses, s.bytes_saved) == (2, 1, 10)
    assert s.saved_cost_usd == pytest.approx(0.08) and s.saved_ms == pytest.approx(1800)


def test_json_and_npz(tmp_path):
    c = BuildCache(LocalBlobStore(tmp_path))
    v, hit = c.get_or_compute_json(K, lambda: {"b": [1, 2], "a": 1.5})
    assert v == {"a": 1.5, "b": [1, 2]} and not hit
    v, hit = c.get_or_compute_json(K, lambda: pytest.fail("recomputed"))
    assert hit and v["a"] == 1.5
    k2 = "cd" * 32
    arrs = {"p": np.random.default_rng(0).random((4, 3)), "t": np.arange(6, dtype=np.int32).reshape(2, 3)}
    out, hit = c.get_or_compute_npz(k2, lambda: arrs)
    assert not hit
    out, hit = c.get_or_compute_npz(k2, lambda: pytest.fail("recomputed"))
    assert hit and np.array_equal(out["p"], arrs["p"]) and out["t"].dtype == np.int32


def test_single_flight(tmp_path):
    c = BuildCache(LocalBlobStore(tmp_path))
    calls = 0

    async def compute():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return b"result"

    async def main():
        return await asyncio.gather(*(c.aget_or_compute(K, compute, saved_ms=10) for _ in range(20)))

    res = asyncio.run(main())
    assert calls == 1
    assert all(d == b"result" for d, _ in res)
    assert sorted(h for _, h in res).count(False) == 1
    assert c.stats.misses == 1 and c.stats.hits == 19 and c.stats.deduped == 19
    assert c.stats.saved_ms == pytest.approx(190)
    assert c._inflight == {}


def test_single_flight_failure_propagates_and_recovers(tmp_path):
    c = BuildCache(LocalBlobStore(tmp_path))
    n = 0

    async def flaky():
        nonlocal n
        n += 1
        await asyncio.sleep(0.01)
        if n == 1:
            raise RuntimeError("boom")
        return b"ok"

    async def main():
        r = await asyncio.gather(*(c.aget_or_compute(K, flaky) for _ in range(3)), return_exceptions=True)
        assert all(isinstance(x, RuntimeError) for x in r) and n == 1
        return await c.aget_or_compute(K, flaky)

    assert asyncio.run(main()) == (b"ok", False)
