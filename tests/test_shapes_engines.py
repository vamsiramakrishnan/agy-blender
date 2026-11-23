"""A design using every shaped primitive (bevelled and inset boxes, bevelled cylinders, open and closed lathes,
concave extrusions) through the whole offline prop pipeline with a real Blender worker: the package validates, three.js
renders it, Godot imports it (godot_4), the upstream Blender asset check passes, and for Roblox the FBX is written and
re-imported. Real Blender (marked ``blender``; CI's ``stock-engines`` job, in the qualification workflow's required set).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from antigravity3d.adapters.blender_5_3.pool import BlenderPool, find_blender

sys.path.insert(0, str(Path(__file__).parent))
from test_shapes import _crate  # noqa: E402
from test_sweep_pipeline import rail_scene  # noqa: E402

needs_blender = pytest.mark.skipif(not find_blender(), reason="no Blender binary ($BLENDER_BIN or PATH)")


@pytest.fixture
async def blender_pool():
    async with BlenderPool(1, startup_timeout=120) as pool:
        yield pool


def _step(rep, name: str) -> dict:
    return next(s for s in rep.extras["pyramid"]["steps"] if s["name"] == name)


@pytest.mark.blender
@needs_blender
@pytest.mark.parametrize("engine", ["godot_4", "roblox_r15"])
async def test_shaped_crate_goes_through_the_pipeline_and_the_engines(tmp_path, blender_pool, engine):
    from antigravity3d.cli import offline_plane
    from antigravity3d.dags.prop_to_engine import PropToEngineDAG
    from antigravity3d.genmedia.nano_banana import FakeImageBackend, ImageForge
    from antigravity3d.kernel.fast_ops import PROXY_REASON
    from antigravity3d.state import EngineBudget
    from antigravity3d.tactics.authoring import CallableAuthor

    scene = _crate()

    async def propose(prompt, *, budget, n, context):
        return [scene.model_copy(update={"asset_name": context.get("asset_name", scene.asset_name)})]

    dag = PropToEngineDAG(plane=offline_plane(), forge=ImageForge(FakeImageBackend()), author=CallableAuthor(propose, lambda *a, **k: None),
                          workdir=tmp_path, budget=EngineBudget(engine, max_tris=6000), pool=blender_pool, single_candidate=True)
    rep = await dag.run("a wooden supply crate with rounded edges, recessed side panels, a lid with a recessed top, metal "
                        "feet, corner guards, a turned knob, a lifting ring and two bracket handles")
    assert rep.ok, [(s["name"], s["status"], str(s["detail"])[:300]) for s in rep.extras.get("pyramid", {}).get("steps", [])]
    for name in ("uv_lod_colliders", "usd_validate", "lod_pop", "export_glb", "blender_asset_qa"):
        assert _step(rep, name)["status"] == "passed", (name, _step(rep, name)["detail"])
    assert "missed" not in _step(rep, "uv_lod_colliders")["detail"]
    assert "no errors" in _step(rep, "blender_asset_qa")["detail"]
    if os.environ.get("AGY3D_THREE_JS") and os.environ.get("CHROMIUM_BIN"):
        web = _step(rep, "web_runtime")
        assert web["status"] == "passed", web["detail"]
    if engine == "godot_4":
        imp = _step(rep, "engine_import")
        assert imp["status"] == "passed" or not os.environ.get("GODOT_BIN"), imp["detail"]
    else:
        fbx = _step(rep, "export_fbx")
        assert fbx["status"] == "passed" and "re-imported in Blender" in fbx["detail"], fbx["detail"]
    (manifest_path,) = sorted(tmp_path.glob("runs/*/package/manifest.json"))
    manifest = json.loads(manifest_path.read_text())
    # the manifest says that the bevelled and inset parts collide as their plain shapes
    assert any(PROXY_REASON in c["reason"] for c in manifest["collider_sources"])


@pytest.mark.blender
@needs_blender
@pytest.mark.parametrize("engine", ["godot_4", "roblox_r15"])
async def test_swept_rail_goes_through_the_pipeline_and_the_engines(tmp_path, blender_pool, engine):
    """A fixed sweep fixture through the same delivery pipeline, including independent Blender QA and FBX re-import."""
    from antigravity3d.cli import offline_plane
    from antigravity3d.dags.prop_to_engine import PropToEngineDAG
    from antigravity3d.genmedia.nano_banana import FakeImageBackend, ImageForge
    from antigravity3d.kernel.validator import validate_package
    from antigravity3d.state import EngineBudget
    from antigravity3d.tactics.authoring import CallableAuthor

    scene = rail_scene()

    async def propose(prompt, *, budget, n, context):
        return [scene.model_copy(update={"asset_name": context.get("asset_name", scene.asset_name)})]

    dag = PropToEngineDAG(plane=offline_plane(), forge=ImageForge(FakeImageBackend()),
                          author=CallableAuthor(propose, lambda *a, **k: None), workdir=tmp_path,
                          budget=EngineBudget(engine, max_tris=5000), pool=blender_pool, single_candidate=True)
    rep = await dag.run("a continuous wooden bent rail with circular cross-section and flat closed ends")
    assert rep.ok, [(s["name"], s["status"], str(s["detail"])[:300]) for s in rep.extras.get("pyramid", {}).get("steps", [])]
    for name in ("uv_lod_colliders", "usd_validate", "lod_pop", "export_glb", "blender_asset_qa"):
        assert _step(rep, name)["status"] == "passed", (name, _step(rep, name)["detail"])
    assert "missed" not in _step(rep, "uv_lod_colliders")["detail"]
    assert "no errors" in _step(rep, "blender_asset_qa")["detail"]
    if os.environ.get("AGY3D_THREE_JS") and os.environ.get("CHROMIUM_BIN"):
        assert _step(rep, "web_runtime")["status"] == "passed", _step(rep, "web_runtime")["detail"]
    if engine == "godot_4":
        assert _step(rep, "engine_import")["status"] == "passed" or not os.environ.get("GODOT_BIN"), _step(rep, "engine_import")["detail"]
    else:
        assert _step(rep, "export_fbx")["status"] == "passed", _step(rep, "export_fbx")["detail"]
        assert "re-imported in Blender" in _step(rep, "export_fbx")["detail"]
    (manifest_path,) = sorted(tmp_path.glob("runs/*/package/manifest.json"))
    assert not validate_package(manifest_path.parent)
