"""web_compress.mjs encodes a package's texture maps in worker threads. The bytes must not depend on how many workers
there are (``AGY3D_KTX2_WORKERS=1`` is the old in-process path), a warm cache must encode nothing, and a bad setting or
a worker that fails must end the script with a message. These run the script itself on a small real job, so they need
Node and a ``node_modules`` holding the six compression packages: point ``AGY3D_TEST_NODE_MODULES`` at one (no other
test of the suite needs these packages), or the tests are skipped."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest

from antigravity3d.deliver import glb as G
from antigravity3d.deliver import web

NODE_MODULES = Path(os.environ["AGY3D_TEST_NODE_MODULES"]) if os.environ.get("AGY3D_TEST_NODE_MODULES") else None
SCRIPT = Path(web.__file__).parent / "web_compress.mjs"
ROLES = {"albedo": "baseColorTexture", "normal": "normalTexture", "orm": "metallicRoughnessTexture"}


@pytest.fixture(scope="module")
def node() -> str:
    exe = web.node_executable()
    if exe is None:
        pytest.skip("node is not available")
    if NODE_MODULES is None or not all(web.installed_version(web.find_package(NODE_MODULES.parent, n)) for n in web.TOOL_PACKAGES):
        pytest.skip("AGY3D_TEST_NODE_MODULES does not name a node_modules with the compression packages")
    return exe


def _png(seed: int, size: int) -> bytes:
    from PIL import Image

    rng = np.random.default_rng(seed)
    base = np.linspace(0, 255, size, dtype=np.uint8)[None, :, None] * np.ones((size, 1, 3), dtype=np.uint8)
    noise = rng.integers(0, 40, (size, size, 3), dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray((base // 2 + noise).astype(np.uint8)).save(buf, "PNG")
    return buf.getvalue()


def _glb(images: dict[str, bytes]) -> bytes:
    """One triangle with a map for each role, the images embedded as buffer views."""
    pos = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], "<f4").tobytes()
    nrm = np.array([[0, 0, 1]] * 3, "<f4").tobytes()
    uv = np.array([[0, 0], [1, 0], [0, 1]], "<f4").tobytes()
    idx = np.array([0, 1, 2, 0], "<u2").tobytes()
    blob = pos + nrm + uv + idx
    views = [{"buffer": 0, "byteOffset": 0, "byteLength": len(pos)}, {"buffer": 0, "byteOffset": len(pos), "byteLength": len(nrm)},
             {"buffer": 0, "byteOffset": len(pos) + len(nrm), "byteLength": len(uv)},
             {"buffer": 0, "byteOffset": len(pos) + len(nrm) + len(uv), "byteLength": 6}]
    pad = lambda b: b + b"\0" * (-len(b) % 4)
    mat: dict = {"pbrMetallicRoughness": {}}
    doc: dict = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}], "images": [], "textures": []}
    for n, (role, data) in enumerate(images.items()):
        views.append({"buffer": 0, "byteOffset": len(blob), "byteLength": len(data)})
        blob = pad(blob + data)
        doc["images"].append({"bufferView": len(views) - 1, "mimeType": "image/png"})
        doc["textures"].append({"source": n})
        slot = {"index": n}
        if role == "normal":
            mat["normalTexture"] = slot
        elif role == "orm":
            mat["pbrMetallicRoughness"]["metallicRoughnessTexture"] = slot
        else:
            mat["pbrMetallicRoughness"]["baseColorTexture"] = slot
    doc.update({"materials": [mat], "buffers": [{"byteLength": len(blob)}], "bufferViews": views,
                "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "NORMAL": 1, "TEXCOORD_0": 2}, "indices": 3, "material": 0}]}],
                "accessors": [{"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3", "min": [0, 0, 0], "max": [1, 1, 0]},
                              {"bufferView": 1, "componentType": 5126, "count": 3, "type": "VEC3"},
                              {"bufferView": 2, "componentType": 5126, "count": 3, "type": "VEC2"},
                              {"bufferView": 3, "componentType": 5123, "count": 3, "type": "SCALAR"}]})
    return G._join(doc, blob)


def _run(node: str, tmp: Path, name: str, glb: bytes, workers: str | None, cache: Path | None = None):
    """Run the script as ``web.compress`` does (the packages linked beside a copy of it); returns (result, report, level bytes, seconds)."""
    work = tmp / name
    work.mkdir()
    (work / "node_modules").symlink_to(NODE_MODULES, target_is_directory=True)
    shutil.copy(SCRIPT, work / "web_compress.mjs")
    cache = cache or work / "cache"
    cache.mkdir(exist_ok=True)
    (work / "in.glb").write_bytes(glb)
    job = {"levels": [{"in": str(work / "in.glb"), "out": str(work / "out.glb")}], "textures": web.TEXTURE_TIERS[0][1],
           "meshopt": web.MESHOPT_SETTINGS, "report": str(work / "report.json"), "cache": str(cache)}
    (work / "job.json").write_text(json.dumps(job))
    env = {"PATH": os.environ["PATH"], "HOME": str(work)}
    if workers is not None:
        env["AGY3D_KTX2_WORKERS"] = workers
    t0 = time.perf_counter()
    r = subprocess.run([node, "web_compress.mjs", "job.json"], cwd=work, env=env, capture_output=True, text=True, timeout=300)
    dt = time.perf_counter() - t0
    if r.returncode != 0:
        return r, None, None, dt
    return r, json.loads((work / "report.json").read_text()), (work / "out.glb").read_bytes(), dt


def _same_but_seconds(report: dict) -> list:
    return [{k: v for k, v in im.items() if k != "seconds"} for im in report["images"]]


@pytest.fixture(scope="module")
def three_maps() -> bytes:
    return _glb({"albedo": _png(1, 256), "normal": _png(2, 256), "orm": _png(3, 256)})


def test_the_workers_give_the_same_bytes_as_the_single_thread_path(node, three_maps, tmp_path):
    r1, rep1, lvl1, _ = _run(node, tmp_path, "one", three_maps, "1")
    r2, rep2, lvl2, _ = _run(node, tmp_path, "many", three_maps, "3")
    r3, rep3, lvl3, _ = _run(node, tmp_path, "default", three_maps, None)
    assert r1.returncode == r2.returncode == r3.returncode == 0, (r1.stderr, r2.stderr, r3.stderr)
    assert sorted(i["role"] for i in rep1["images"]) == ["albedo", "normal", "orm"]
    assert len(rep1["images"]) == 3 and len({i["ktx2_sha256"] for i in rep1["images"]}) == 3
    assert _same_but_seconds(rep1) == _same_but_seconds(rep2) == _same_but_seconds(rep3)
    assert hashlib.sha256(lvl1).digest() == hashlib.sha256(lvl2).digest() == hashlib.sha256(lvl3).digest()
    assert all(i["seconds"] > 0 for i in rep2["images"])  # each map's own encode time


def test_a_warm_cache_encodes_nothing_new(node, three_maps, tmp_path):
    cache = tmp_path / "shared"
    _, cold, lvl, cold_s = _run(node, tmp_path, "cold", three_maps, None, cache)
    before = {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in cache.iterdir()}
    assert len(before) == 3
    _, warm, lvl2, warm_s = _run(node, tmp_path, "warm", three_maps, None, cache)
    assert {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in cache.iterdir()} == before
    assert _same_but_seconds(warm) == _same_but_seconds(cold) and lvl2 == lvl
    assert max(i["seconds"] for i in warm["images"]) < 0.5 and min(i["seconds"] for i in cold["images"]) > max(i["seconds"] for i in warm["images"])
    assert warm_s < cold_s


@pytest.mark.parametrize("value", ["0", "-1", "two", "1.5", ""])
def test_a_bad_worker_count_ends_with_a_message_and_no_output(node, three_maps, tmp_path, value):
    r, report, _, _ = _run(node, tmp_path, "bad", three_maps, value)
    assert r.returncode != 0 and report is None
    assert "AGY3D_KTX2_WORKERS must be an integer >= 1" in r.stderr


@pytest.mark.parametrize("workers", ["1", "2"])
def test_a_map_that_cannot_be_encoded_ends_with_a_message_not_a_silent_fallback(node, tmp_path, workers):
    glb = _glb({"albedo": _png(1, 256), "normal": b"this is not a picture", "orm": _png(3, 256)})
    r, report, _, _ = _run(node, tmp_path, "broken", glb, workers)
    assert r.returncode != 0 and report is None
    assert "web_compress failed:" in r.stderr and len(r.stderr) < 600 * 3
    if workers != "1":
        assert "texture worker failed" in r.stderr
