"""Offline tests of the harness tools: real kernel + FakeDecisionsClient + FakeImageBackend."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from google.antigravity.tools.tool_context import ToolContext
from pxr import Usd, UsdGeom

from antigravity3d.decisions.engine import DecisionControlPlane
from antigravity3d.decisions.fakes import FakeDecisionsClient
from antigravity3d.genmedia.nano_banana import FakeImageBackend, ImageForge
from antigravity3d.harness import session as S
from antigravity3d.harness import tools as T
from antigravity3d.state import MAX_AUDIT_DELTAS, EngineBudget, SceneSessionRegistry, VisualVerdict

RETURN_CAP = 400
B64_RUN = re.compile(r"[A-Za-z0-9+/]{120,}={0,2}")


def box(nid: str, size, z: float, **kw) -> dict:
    return {"op": "upsert_node", "node": {"id": nid, "kind": "box", "params": {"size": list(size)},
                                          "transform": {"translate": [0, 0, z]}, **kw}}


CRATE = {"ops": [box("body", (1.0, 0.8, 0.6), 0.3), box("lid", (1.05, 0.85, 0.08), 0.64)]}


def make_session(tmp_path: Path, *, budget: EngineBudget | None = None, client: FakeDecisionsClient | None = None,
                 sid: str = "s1", pool=None, forge: bool = True, plane: bool = True, stage: str = "BLOCKOUT"):
    reg = SceneSessionRegistry()
    client = client or FakeDecisionsClient()
    backend = FakeImageBackend()
    S.open_session(reg, sid, tmp_path / sid, "Crate", budget or EngineBudget("unreal_5"),
                   plane=DecisionControlPlane(client) if plane else None,
                   forge=ImageForge(backend) if forge else None, pool=pool)
    conv = f"conv-{sid}-" + "0" * 32
    S.bind_conversation(conv, sid, reg)
    S.set_toolset(reg, sid, stage)
    reg.get(sid).active_stage = T.TOOLSET_STAGE[stage]
    ctx = ToolContext(SimpleNamespace(conversation_id=conv))
    return SimpleNamespace(reg=reg, sid=sid, ctx=ctx, client=client, backend=backend, state=reg.get(sid),
                           sess=lambda: S.session(reg, sid))


def masked_fixture(path):
    from PIL import Image
    im = Image.new("RGBA", (16, 16))
    im.paste((180, 145, 100, 255), (4, 4, 12, 12))
    im.save(path)
    return path


def record_approval(env):
    """A fixture for testing consumers of a passing receipt without paid model calls."""
    state = env.state
    s = env.sess()
    p = s.add_preview(masked_fixture(s.artifacts_dir / "fixture.png"), "https://fixture.test/dcc", kind="eevee")
    s.set_tier15(3.8, VisualVerdict(state.revision, state.budget.target_engine, "lookdev",
                                             "eevee", "decisions", "approve"), p)


def simulate_engine_import(monkeypatch, *, ok=True):
    from antigravity3d.adapters.engines import validators

    async def validate(path):
        assert path.is_file()
        return validators.EngineReport(ok, "test_engine", "fixture import result")
    async def capture(_glb, path):
        masked_fixture(path)
        return validators.EngineReport(True, "test_engine")
    async def parity(*_args, **_kwargs):
        return SimpleNamespace(result=SimpleNamespace(calibrated=True, source="decisions"),
                               scores={"score_engine_parity": 4.0}, defects={})
    monkeypatch.setattr(DecisionControlPlane, "evaluate_engine_parity", parity)
    monkeypatch.setattr(validators, "validator_for", lambda _target: SimpleNamespace(validate=validate, capture=capture))


def composed(env) -> str:
    return env.sess().stage.stage.Flatten().ExportToString()


def check_line(r: str, lines: int = 3) -> str:
    assert isinstance(r, str) and r
    assert len(r) <= RETURN_CAP, r
    assert r.count("\n") < lines, r
    assert not B64_RUN.search(r) and "iVBOR" not in r, r
    return r


@pytest.fixture
def env(tmp_path):
    return make_session(tmp_path)


async def test_good_patch_commits(env):
    r = check_line(await T.apply_scene_ir(json.dumps(CRATE), env.ctx), 1)
    assert r.startswith("OK tx=") and "nodes=2" in r and "tris=24" in r
    assert env.state.last_tier1_ok
    assert env.state.tx_stack == env.sess().stage.tx_stack and len(env.state.tx_stack) == 1


async def test_full_scene_json_and_bare_op_list(env):
    scene = {"nodes": [CRATE["ops"][0]["node"]], "materials": [{"id": "wood"}]}
    assert (await T.apply_scene_ir(json.dumps(scene), env.ctx)).startswith("OK")
    r = await T.apply_scene_ir(json.dumps([{"op": "assign_material", "node_id": "body", "material_id": "wood"}]), env.ctx)
    assert r.startswith("OK") and "ops=1" in r


def _open_quad_usda(path: Path) -> Path:
    path.write_text("""#usda 1.0
(
    defaultPrim = "Quad"
    upAxis = "Z"
)
def Mesh "Quad"
{
    int[] faceVertexCounts = [4]
    int[] faceVertexIndices = [0, 1, 2, 3]
    point3f[] points = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)]
}
""")
    return path


async def test_non_manifold_patch_rolled_back_byte_for_byte(env, tmp_path):
    await T.apply_scene_ir(json.dumps(CRATE), env.ctx)
    before, stack = composed(env), list(env.state.tx_stack)
    quad = _open_quad_usda(tmp_path / "quad.usda")
    bad = {"ops": [{"op": "upsert_node", "node": {"id": "sheet", "kind": "mesh_ref", "params": {"path": str(quad)},
                                                  "transform": {"translate": [0, 0, 0]}}}]}
    r = check_line(await T.apply_scene_ir(json.dumps(bad), env.ctx), 1)
    assert r.startswith("ROLLED BACK TIER1 FAIL") and "not_watertight" in r
    assert composed(env) == before
    assert env.state.tx_stack == stack == env.sess().stage.tx_stack
    assert env.state.last_tier1_ok


async def test_over_budget_patch_rolled_back(tmp_path):
    env = make_session(tmp_path, budget=EngineBudget("unreal_5", max_tris=200))
    await T.apply_scene_ir(json.dumps(CRATE), env.ctx)
    before = composed(env)
    ball = {"op": "upsert_node", "node": {"id": "ball", "kind": "sphere", "params": {"radius": 0.2, "segments": 48, "rings": 24},
                                          "transform": {"translate": [0, 0, 0.88]}}}
    r = check_line(await T.apply_scene_ir(json.dumps({"ops": [ball]}), env.ctx), 1)
    assert r.startswith("ROLLED BACK") and "tris" in r and ">200" in r
    assert composed(env) == before


async def test_invalid_ir_is_one_error_line(env):
    r = check_line(await T.apply_scene_ir(json.dumps({"ops": [{"op": "set_param", "node_id": "ghost", "name": "radius", "value": 1}]}), env.ctx), 1)
    assert r.startswith("ERROR") and "ghost" in r
    r = check_line(await T.apply_scene_ir('{"ops": [{"op": "upsert_node", "node": {"id": "x", "kind": "box"}}]}', env.ctx), 1)
    assert r.startswith("ERROR") and "size" in r
    assert env.state.tx_stack == []


async def test_undo_last_edit(env):
    assert check_line(await T.undo_last_edit(env.ctx)) == "nothing to undo"
    await T.apply_scene_ir(json.dumps(CRATE), env.ctx)
    first = composed(env)
    await T.apply_scene_ir(json.dumps({"ops": [box("handle", (0.2, 0.05, 0.05), 0.4)]}), env.ctx)
    assert len(env.state.tx_stack) == 2
    r = check_line(await T.undo_last_edit(env.ctx), 1)
    assert r.startswith("UNDONE") and "open_tx=1" in r
    assert composed(env) == first and env.state.tx_stack == env.sess().stage.tx_stack


async def test_render_qa_stores_score_and_caps_audit(env):
    await T.apply_scene_ir(json.dumps(CRATE), env.ctx)
    r = check_line(await T.render_preview(env.ctx), 1)
    assert r.startswith("PREVIEW #1") and "uri=fake://" in r
    assert env.sess().previews[-1].path.is_file() and env.backend.uploads
    for _ in range(MAX_AUDIT_DELTAS + 3):
        r = check_line(await T.run_visual_qa(env.ctx))
        assert r.startswith("QA[blockout] approve")
    assert env.state.last_tier15_score == pytest.approx(3.6, abs=0.05)
    assert len(env.state.audit_deltas) == MAX_AUDIT_DELTAS
    assert len(env.sess().previews) == 1  # QA reused the fresh sheet instead of re-rendering
    sent = env.client.calls[-1]
    assert sent["n_images"] == 1 and sent["input"][1]["resolution"] == "low"  # blockout tier


async def test_geometry_edit_after_qa_resets_score(env):
    await T.apply_scene_ir(json.dumps(CRATE), env.ctx)
    await T.run_visual_qa(env.ctx)
    assert not env.state.export_allowed  # blockout clay and offline Decisions cannot authorize publication
    record_approval(env)
    assert env.state.export_allowed
    await T.apply_scene_ir(json.dumps({"ops": [box("lid", (1.05, 0.85, 0.1), 0.65)]}), env.ctx)
    assert env.state.last_tier15_score == 0.0 and not env.state.export_allowed
    await T.run_visual_qa(env.ctx)
    assert env.sess().previews[-1].geom_version == env.sess().geom_version  # stale sheet was not reused


async def test_reground_auto_fix(tmp_path):
    env = make_session(tmp_path, client=FakeDecisionsClient({"defect_floating": 0.95}))
    floating = {"ops": [box("body", (1, 1, 0.5), 0.75)]}  # bottom at z=0.5
    await T.apply_scene_ir(json.dumps(floating), env.ctx)
    r = check_line(await T.run_visual_qa(env.ctx))
    assert "auto_fix fixer=reground" in r and "fix: reground dz=-0.500" in r
    zmin = min(m.points[:, 2].min() for m in env.sess().stage.extract_meshes())
    assert abs(zmin) < 1e-6
    assert env.state.last_tier15_score == 0.0  # the fix changed geometry: re-QA required


async def test_texture_fixer_not_applicable_without_maps(tmp_path):
    env = make_session(tmp_path, client=FakeDecisionsClient({"defect_cg_plastic": 0.95}), stage="LOOKDEV")
    await T.apply_scene_ir(json.dumps(CRATE), env.ctx)
    r = await T.run_visual_qa(env.ctx)
    assert "roughness_breakup" in r and "not applicable" in r


async def test_export_refused_then_allowed_and_reloads(env, monkeypatch):
    simulate_engine_import(monkeypatch)
    await T.apply_scene_ir(json.dumps(CRATE), env.ctx)
    r = check_line(await T.export_engine_package(env.ctx, "crate_pkg"), 1)
    assert r.startswith("REFUSED export blocked") and "tier15" in r
    assert not (env.sess().export_dir / "crate_pkg").exists()
    await T.run_visual_qa(env.ctx)
    assert not env.state.export_allowed
    record_approval(env)
    r = check_line(await T.generate_lods_and_colliders(env.ctx), 1)
    assert r.startswith("LODS 2 nodes") and "colliders 2 hulls" in r
    check_line(await T.run_usd_validator(env.ctx), 1)
    r = check_line(await T.export_engine_package(env.ctx, "crate_pkg"), 1)
    assert r.startswith("EXPORTED crate_pkg")
    out = env.sess().export_dir / "crate_pkg"
    stage = Usd.Stage.Open(str(out / "Crate.usd"))
    assert stage.GetDefaultPrim().GetName() == "Crate"
    assert not stage.GetPrimAtPath("/__agy_base")
    assert {p.GetName() for p in stage.Traverse()} >= {"body", "lid"}
    import trimesh

    glb = trimesh.load(out / "Crate.glb")
    assert set(glb.geometry) == {"body", "lid"} and sum(len(g.faces) for g in glb.geometry.values()) == 24
    hulls = trimesh.load(out / "Crate_colliders.glb")
    assert all(n.startswith("UCX_Crate_") for n in hulls.geometry)
    man = json.loads((out / "manifest.json").read_text())
    # two boxes cannot get cheaper within any screen-space error budget: one level, no duplicate LOD files
    assert not list(out.glob("Crate_LOD*.glb")) and man["lod_tris"] == [24] and man["lod_screen_fractions"] == [1.0]
    assert man["budget"]["max_tris"] == 15000 and man["qa"]["tier1_ok"] and man["qa"]["tier15"] >= 3.4
    assert man["schema_version"] == 2 and man["validation"]["package_ok"] and "pkg ok" in r
    assert (out / "Crate.usdz").is_file() and man["usd"] == "Crate.usd"
    assert env.sess().stage.has_uvs()  # UVs are authored into the working stage by the export pipeline
    for m in (p for p in stage.Traverse() if p.GetName() in ("body", "lid")):
        assert len(UsdGeom.PrimvarsAPI(m).GetPrimvar("st").Get()) == len(UsdGeom.Mesh(m).GetPointsAttr().Get())
    assert env.sess().last_export == out


async def test_variants_branching(env):
    await T.apply_scene_ir(json.dumps(CRATE), env.ctx)
    v = [{"name": "flat", "ops": [box("lid", (1.05, 0.85, 0.08), 0.64)]}, {"name": "tall", "ops": [box("lid", (1.05, 0.85, 0.3), 0.75)]}]
    r = check_line(await T.propose_variants("lid", v, env.ctx), 1)
    assert r.startswith("VARIANTS lid: flat:ok") and "tall:ok" in r
    r = check_line(await T.select_variant("lid", "tall", env.ctx), 1)
    assert r.startswith("SELECTED lid=tall")
    r = await T.apply_scene_ir(json.dumps({"ops": [box("x", (0.1, 0.1, 0.1), 0.05)]}), env.ctx)
    assert r.startswith("ERROR") and "variant selected" in r
    assert (await T.select_variant("lid", "nope", env.ctx)).startswith("ERROR")
    r = check_line(await T.promote_variant("lid", "tall", env.ctx), 1)
    assert r.startswith("PROMOTED")
    assert next(n for n in env.sess().stage.base_ir().nodes if n.id == "lid").params["size"][2] == pytest.approx(0.3)
    assert (await T.apply_scene_ir(json.dumps({"ops": [box("x", (0.1, 0.1, 0.1), 0.05)]}), env.ctx)).startswith("OK")
    assert env.state.tx_stack == env.sess().stage.tx_stack


async def test_forge_pbr_material_ranks_and_binds(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "TEXTURE_PX", 64)
    env = make_session(tmp_path, stage="LOOKDEV")
    await T.apply_scene_ir(json.dumps(CRATE), env.ctx)
    r = check_line(await T.forge_pbr_material("wood", "weathered oak planks", env.ctx, candidates=3), 1)
    assert r.startswith("MATERIAL wood v1") and "3 passed policy" in r
    tags = [c for c in env.client.calls]
    assert len(tags) == 1
    assert sum("policy_legible_text" in q for q in tags[0]["questions"]) == 3
    assert sum("score_tileability" in q for q in tags[0]["questions"]) == 3
    mat = env.sess().stage.base_ir().materials[0]
    assert all(Path(getattr(mat, k)).is_file() for k in T.MAP_KEYS)
    r = check_line(await T.apply_shader_diff("wood", json.dumps({"roughness": 0.7, "assign_to": ["body", "lid"]}), env.ctx), 1)
    assert r.startswith("SHADER wood") and "assigned=2" in r
    assert {m.material_id for m in env.sess().stage.extract_meshes()} == {"wood"}
    assert env.state.last_tier15_score == 0.0


async def test_forge_discards_result_after_concurrent_scene_edit(tmp_path, monkeypatch):
    env = make_session(tmp_path, stage="LOOKDEV")
    monkeypatch.setattr(T, "TEXTURE_PX", 32)
    entered, resume = asyncio.Event(), asyncio.Event()
    original = env.sess().plane.rank_candidates

    async def wait_and_grade(*args, **kwargs):
        entered.set()
        await resume.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(env.sess().plane, "rank_candidates", wait_and_grade)
    task = asyncio.create_task(T.forge_pbr_material("wood", "oak", env.ctx))
    await asyncio.wait_for(entered.wait(), 5)
    await T.apply_scene_ir(json.dumps(CRATE), env.ctx)
    resume.set()
    assert (await task).startswith("STALE")
    assert not env.sess().stage.base_ir().materials
    assert not list((env.sess().artifacts_dir / "materials").iterdir())


async def test_forge_blocked_by_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "TEXTURE_PX", 32)
    env = make_session(tmp_path, client=FakeDecisionsClient({"policy_legible_text": 0.9}), stage="LOOKDEV")
    r = check_line(await T.forge_pbr_material("wood", "oak", env.ctx), 1)
    assert r.startswith("BLOCKED") and "policy_legible_text" in r
    assert env.sess().stage.base_ir().materials == []


async def test_shader_diff_rolls_back_over_draw_budget(tmp_path):
    env = make_session(tmp_path, budget=EngineBudget("unreal_5", max_draw_calls=1), stage="LOOKDEV")
    await T.apply_scene_ir(json.dumps(CRATE), env.ctx)
    r = await T.apply_shader_diff("red", json.dumps({"base_color": [1, 0, 0], "assign_to": ["lid"]}), env.ctx)
    assert r.startswith("ROLLED BACK") and "draws 2>1" in r


async def test_paintover_and_lookdev_fallback(env):
    await T.apply_scene_ir(json.dumps(CRATE), env.ctx)
    r = check_line(await T.run_paintover_loop("rusty iron bands", env.ctx), 1)
    assert r.startswith("PAINTOVER") and env.state.paintover_uri.startswith("fake://")
    r = check_line(await T.render_lookdev(env.ctx), 1)
    assert r.startswith("BLENDER TIER UNAVAILABLE") and "preview tier used" in r


async def test_lookdev_and_bpy_with_fake_pool(tmp_path):
    from antigravity3d.adapters.blender_5_3.pool import BlenderPool

    async with BlenderPool(1, fake=True) as pool:
        env = make_session(tmp_path, pool=pool, stage="EXPORT")
        await T.apply_scene_ir(json.dumps(CRATE), env.ctx)
        r = check_line(await T.render_lookdev(env.ctx, engine="eevee"), 1)
        assert r.startswith("LOOKDEV eevee render #1")
        r = check_line(await T.run_bpy_script("import bpy\nbpy.ops.mesh.primitive_cube_add()", env.ctx), 1)
        assert r.startswith("RAN")
    assert (await T.run_bpy_script("import os\nos.remove('x')", env.ctx)).startswith("REJECTED")


async def test_bpy_unavailable_without_pool(env):
    assert (await T.run_bpy_script("import bpy", env.ctx)) == "UNAVAILABLE: no Blender pool attached to this session"


async def test_manifest_capped(tmp_path):
    env = make_session(tmp_path, budget=EngineBudget("unreal_5", max_tris=100_000, max_draw_calls=99))
    ops = [box(f"slat_{i:02d}", (0.1, 0.1, 0.1), 0.05 + 0.1 * i) for i in range(60)]
    assert (await T.apply_scene_ir(json.dumps({"ops": ops}), env.ctx)).startswith("OK")
    r = await T.inspect_manifest(env.ctx)
    assert len(r) <= T.MANIFEST_CAP and r.endswith("nodes") and "... +" in r


async def test_session_resolution_fallback_and_unbound(env):
    ctx = ToolContext(SimpleNamespace(conversation_id="unbound-conv"))
    r = await T.inspect_manifest(ctx)
    assert r.startswith("ERROR") and "no asset session bound" in r
    ctx.set_state(S.CTX_SESSION_KEY, env.sid)
    ctx.set_state(S.CTX_REGISTRY_KEY, env.reg)
    assert (await T.inspect_manifest(ctx)).startswith("# Crate")


async def test_session_lock_serialises_concurrent_edits(env):
    import asyncio

    results = await asyncio.gather(*(T.apply_scene_ir(json.dumps({"ops": [box(f"b{i}", (0.2, 0.2, 0.2), 0.1)]}), env.ctx) for i in range(5)))
    assert all(r.startswith("OK") for r in results)
    assert len(env.state.tx_stack) == 5 == len(env.sess().stage.tx_stack)
    assert np.isclose(min(m.points[:, 2].min() for m in env.sess().stage.extract_meshes()), 0.0)
