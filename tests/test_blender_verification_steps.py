"""The package and the kit are checked by EL4CTEO/blender-skills' scripts, run by the Blender worker, as pipeline steps.

``blender_asset_qa`` (pass C of the prop pipeline, and the native export) runs ``qa_check`` on the GLB the engines import,
vertices merged, with the target engine's profile; ``blender_kit_check`` (the kit DAG) runs ``kit_check`` on each piece
file with the kit's metric. Without a real worker both are skipped with the reason, never passed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from antigravity3d.adapters.blender_5_3 import upstream_checks
from antigravity3d.adapters.blender_5_3.pool import BlenderOpError, BlenderPool, WorkerError, find_blender
from antigravity3d.decisions.engine import DecisionControlPlane
from antigravity3d.decisions.fakes import FakeDecisionsClient
from antigravity3d.genmedia.nano_banana import FakeImageBackend, ImageForge
from antigravity3d.ir import MaterialIR, NodeIR, SceneIR, Transform
from antigravity3d.kernel.usd_stage import StageManager
from antigravity3d.state import EngineBudget
from antigravity3d.tactics.authoring import CallableAuthor
from antigravity3d.tactics.governor import Governor, plan_for
from antigravity3d.tactics.pyramid import PyramidResult, StepResult, refine

REQUIRED = ("usd_validate", "engine_import", "engine_capture", "engine_parity")


async def up(data: bytes) -> str:
    return "fake://sheet"


class Stub:
    """A pool whose answer is scripted; ``fake`` is left off like a PoolClient's (which has no such attribute)."""

    def __init__(self, result: Any = None, error: BaseException | None = None) -> None:
        self.result, self.error, self.calls = result, error, []

    async def call(self, op: str, /, **args: Any) -> Any:
        self.calls.append((op, args))
        if self.error:
            raise self.error
        return self.result


def box(nid: str, size: tuple[float, float, float], at: tuple[float, float, float]) -> NodeIR:
    return NodeIR(id=nid, kind="box", params={"size": size}, material_id="m0", transform=Transform(translate=at))


async def run_pyramid(tmp_path: Path, scene: SceneIR, pool: Any, engine: str = "unreal_5"):
    sm = StageManager.create(tmp_path / "p.usda", scene.asset_name)
    sm.apply_ir(scene)
    plane = DecisionControlPlane(FakeDecisionsClient(responder=lambda req: {}))
    return await refine(sm, plane=plane, author=CallableAuthor(lambda *a, **k: []), governor=Governor(plan_for(2)), budget=EngineBudget(engine),
                        uploader=up, workdir=tmp_path / "wd", forge=ImageForge(FakeImageBackend()), settle_first=False, pool=pool)


# ------------------------------------------------------------------------------------------------ offline


def test_a_skipped_check_never_verifies_and_a_failed_one_blocks_it():
    base = [StepResult(n, "passed") for n in REQUIRED]
    kw = dict(asset="x", tier1_ok=True, tier15=5.0, live_lookdev=True, live_parity=True, converged={"A": True, "B": True})
    assert PyramidResult(steps=list(base), **kw).verified
    assert PyramidResult(steps=[*base, StepResult("blender_asset_qa", "skipped", "no Blender pool attached")], **kw).verified  # unaffected
    failed = PyramidResult(steps=[*base, StepResult("blender_asset_qa", "failed", "x: floating part")], **kw)
    assert not failed.ok and not failed.verified and dict(failed.failed) == {"blender_asset_qa": "x: floating part"}


@pytest.mark.parametrize("pool", [None, BlenderPool(1, fake=True)])
async def test_prop_pipeline_skips_the_check_without_a_real_worker(tmp_path, pool):
    scene = SceneIR(asset_name="Crate", materials=[MaterialIR(id="m0")], nodes=[box("body", (1, 1, 1), (0, 0, 0.5))])
    res = await run_pyramid(tmp_path, scene, pool)  # a fake pool is never started and never called
    step = next(s for s in res.steps if s.name == "blender_asset_qa")
    assert step.status == "skipped" and step.detail and res.ok and not res.verified
    assert ("no Blender pool" in step.detail) if pool is None else ("fake" in step.detail)


async def test_a_result_marked_fake_is_a_skip_even_from_a_pool_that_does_not_say_so():
    stub = Stub({"ok": True, "pass": True, "errors": [], "warnings": [], "objects": {"FakeMesh": {"tris": 0}}, "fake": True})
    status, detail, data = await upstream_checks.asset_qa(stub, "/x/a.glb", "unreal_5")
    assert status == "skipped" and "fake" in detail and data == {}
    assert stub.calls == [("inspect_usd", {"path": "/x/a.glb", "primitive": "qa_check", "kwargs": {"profile": "game", "tol": 0.002}, "weld": True})]


async def test_a_worker_without_the_script_is_a_skip_and_a_worker_that_dies_is_an_error():
    missing = Stub(error=BlenderOpError("KeyError", "primitive 'qa_check' is not loaded in this worker (loaded: []; load error: x)"))
    status, detail, _ = await upstream_checks.asset_qa(missing, "a.glb", "unreal_5")
    assert status == "skipped" and "not loaded" in detail
    with pytest.raises(WorkerError):
        await upstream_checks.asset_qa(Stub(error=WorkerError("died", "connection lost")), "a.glb", "unreal_5")


async def test_errors_fail_the_step_with_upstreams_text_and_warnings_stay_as_evidence():
    result = {"errors": ["Batch_0: floating/detached part"], "warnings": ["hidden back-to-back faces: a<->b (2 pairs)"], "objects": {"Batch_0": {"tris": 12}}}
    status, detail = upstream_checks.qa_verdict(result, "game")
    assert status == "failed" and "Batch_0: floating/detached part" in detail and "EL4CTEO/blender-skills @9e7e04a" in detail
    status, detail = upstream_checks.qa_verdict({**result, "errors": []}, "roblox")
    assert status == "passed" and "no errors, 1 warning(s): hidden back-to-back faces" in detail and "roblox profile" in detail
    assert upstream_checks.qa_profile("roblox_r15") == "roblox" and upstream_checks.qa_profile("godot_4") == "game"


# ------------------------------------------------------------------------------------------------ real Blender

needs_blender = pytest.mark.skipif(not find_blender(), reason="no Blender binary ($BLENDER_BIN or PATH)")


@pytest.fixture
async def blender_pool():
    async with BlenderPool(1, startup_timeout=120) as pool:
        yield pool


_CRATE: dict[str, Any] = {}


async def crate_run(tmp_path_factory) -> tuple[Any, dict[str, Any]]:
    """The offline crate through the prop DAG with the real worker attached, as `agy3d prop --offline --blender` runs it (once)."""
    if not _CRATE:
        from antigravity3d.cli import make_collaborators
        from antigravity3d.dags.prop_to_engine import PropToEngineDAG
        from antigravity3d.tactics.distill import FixLog

        out = tmp_path_factory.mktemp("crate")
        plane, forge, author, _ = await make_collaborators(True, workdir=out / "agent")
        async with BlenderPool(1, startup_timeout=120) as pool:
            rep = await PropToEngineDAG(plane=plane, forge=forge, author=author, workdir=out, budget=EngineBudget("unreal_5"), pool=pool,
                                        fixlog=FixLog.for_prop(out), max_rounds=4, require_verified=False).run("wooden crate")
        manifests = sorted(out.glob("runs/*/package/manifest.json"))
        assert rep.ok and manifests, rep.summary()
        _CRATE["run"] = (rep, json.loads(manifests[0].read_text()))
    return _CRATE["run"]


@pytest.mark.blender
@needs_blender
async def test_a_clean_crate_passes_the_blender_asset_qa_step(tmp_path_factory):
    rep, manifest = await crate_run(tmp_path_factory)
    steps = {s["name"]: s for s in rep.extras["pyramid"]["steps"]}
    assert steps["blender_asset_qa"]["status"] == "passed", steps["blender_asset_qa"]
    assert "no errors" in steps["blender_asset_qa"]["detail"] and "game profile, vertices merged" in steps["blender_asset_qa"]["detail"]


@pytest.mark.blender
@needs_blender
async def test_the_blender_asset_qa_result_reaches_the_package_manifest(tmp_path_factory):
    _, manifest = await crate_run(tmp_path_factory)
    (step,) = [s for s in manifest["evidence"]["steps"] if s["name"] == "blender_asset_qa"]
    assert step["status"] == "passed" and "EL4CTEO/blender-skills @9e7e04a" in step["detail"]
    v = manifest["validation"]["blender_asset_qa"]
    assert v["script"] == "qa_check" and v["profile"] == "game" and v["merged"] is True and v["errors"] == [] and v["commit"].startswith("9e7e04a")


@pytest.mark.blender
@needs_blender
async def test_planted_z_fighting_fails_the_blender_asset_qa_step_with_upstreams_message(tmp_path, blender_pool):
    # two boxes whose top faces are coplanar (z = 0.5), face +Z and overlap in area (benchmarks/upstream_survey.py, build_defects)
    scene = SceneIR(asset_name="ZFight", materials=[MaterialIR(id="m0")],
                    nodes=[box("base", (1.0, 1.0, 0.5), (0.0, 0.0, 0.25)), box("overlap", (0.6, 0.6, 0.5), (0.5, 0.0, 0.25))])
    res = await run_pyramid(tmp_path, scene, blender_pool)
    failed = dict(res.failed)
    assert "blender_asset_qa" in failed and "z-fighting" in failed["blender_asset_qa"], (failed, [(s.name, s.status) for s in res.steps])
    assert not res.ok and not res.verified


@pytest.mark.blender
@needs_blender
async def test_a_roblox_batch_over_the_triangle_limit_fails_where_the_per_part_rule_passes(tmp_path, blender_pool):
    """The GLB submits parts per material in one batch once there are more nodes than the draw-call budget; Roblox
    imports a batch as one mesh, so upstream's per-mesh limit (20000, also ENGINE_RULES) counts the batch."""
    from antigravity3d.kernel import fast_ops
    from antigravity3d.kernel.package import write_engine_package
    from antigravity3d.kernel.validator import check_engine_compliance

    nodes = [NodeIR(id=f"ball{i}", kind="sphere", params={"radius": 0.5, "segments": 40}, material_id="m0", transform=Transform(translate=(0.6 * i, 0, 0.5)))
             for i in range(40)]
    sm = StageManager.create(tmp_path / "b.usda", "Balls")
    sm.apply_ir(SceneIR(asset_name="Balls", materials=[MaterialIR(id="m0")], nodes=nodes))
    sm.commit_all()
    meshes = sm.extract_meshes()
    budget = EngineBudget("roblox_r15", max_tris=60000)
    assert not [i for i in check_engine_compliance(None, meshes, budget) if i.code.endswith("mesh_tri_cap")]  # every part has 880 tris
    pkg = write_engine_package(sm, fast_ops.run_export_pipeline(meshes, budget, "Balls"), budget, tmp_path / "pkg", asset_name="Balls", qa={"tier1_ok": True})
    assert list(pkg.data["glb_batches"]) == ["Batch_0"]
    status, detail, _ = await upstream_checks.asset_qa(blender_pool, str(pkg.out_dir / "Balls.glb"), "roblox_r15")
    assert status == "failed" and "Batch_0: 35200 tris > roblox limit 20000" in detail


def package_glb(tmp_path: Path, nodes: list[NodeIR], engine: str = "unreal_5") -> str:
    from antigravity3d.kernel import fast_ops
    from antigravity3d.kernel.package import write_engine_package

    sm = StageManager.create(tmp_path / "s.usda", "Pieces")
    sm.apply_ir(SceneIR(asset_name="Pieces", materials=[MaterialIR(id="m0")], nodes=nodes))
    sm.commit_all()
    budget = EngineBudget(engine)
    pkg = write_engine_package(sm, fast_ops.run_export_pipeline(sm.extract_meshes(), budget, "Pieces"), budget, tmp_path / "pkg", asset_name="Pieces",
                               qa={"tier1_ok": True})
    return str(pkg.out_dir / "Pieces.glb")


@pytest.mark.blender
@needs_blender
async def test_separate_boxes_resting_on_the_ground_pass_and_a_part_hovering_5_cm_fails(tmp_path, blender_pool):
    """qa_check calls every piece but the biggest that touches nothing "floating", with no notion of ground. The worker
    lists the pieces with their lowest z beside the result; two crates side by side are separate pieces, not floating."""
    side = [box("a", (1, 1, 1), (0, 0, 0.5)), box("b", (1, 1, 1), (2, 0, 0.5))]
    status, detail, data = await upstream_checks.asset_qa(blender_pool, package_glb(tmp_path / "side", side), "unreal_5")
    assert status == "passed" and "resting on the ground" in detail and "floating/detached part" in detail, detail  # both readings are in the detail
    assert len(data["errors"]) == 1 and data["errors"][0].startswith("floating/detached part") and data["resting_on_ground"] == data["errors"]  # upstream's text, untouched
    hover = [box("base", (1, 1, 1), (0, 0, 0.5)), box("hover", (0.3, 0.3, 0.3), (0, 0, 1.2))]  # its underside is 5 cm above the top
    status, detail, data = await upstream_checks.asset_qa(blender_pool, package_glb(tmp_path / "hover", hover), "unreal_5")
    assert status == "failed" and "floating/detached part" in detail and data["resting_on_ground"] == [], detail
    raised = [box("base", (1, 1, 1), (0, 0, 0.5)), box("raised", (0.3, 0.3, 0.3), (3, 0, 0.35))]  # separate, 20 cm off the ground
    status, _, _ = await upstream_checks.asset_qa(blender_pool, package_glb(tmp_path / "raised", raised), "unreal_5")
    assert status == "failed"


class OffPivot:
    """The real pool, except that the first piece file it is asked about is moved 0.3 m along X first (off its pivot)."""

    def __init__(self, pool: BlenderPool) -> None:
        self.pool, self.moved = pool, None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.pool, name)

    async def call(self, op: str, /, **args: Any) -> Any:
        if op == "inspect_usd" and self.moved is None:
            from pxr import Gf, Usd, UsdGeom

            stage = Usd.Stage.Open(args["path"])
            for prim in stage.Traverse():
                if prim.IsA(UsdGeom.Mesh):
                    mesh = UsdGeom.Mesh(prim)
                    mesh.GetPointsAttr().Set([p + Gf.Vec3f(0.3, 0, 0) for p in mesh.GetPointsAttr().Get()])
            stage.GetRootLayer().Save()
            self.moved = Path(args["path"]).stem
        return await self.pool.call(op, **args)


async def build_kit(tmp_path: Path, which: str, pool: Any):
    from antigravity3d.dags.environment_kit import EnvironmentKitDAG
    from antigravity3d.kernel.kit_metric import KitMetric
    from antigravity3d.tactics.authoring import TemplateAuthor

    dag = EnvironmentKitDAG(forge=ImageForge(FakeImageBackend()), author=TemplateAuthor(), workdir=tmp_path / "kit", budget=EngineBudget("unreal_5"),
                            sheet_resolution=1024, metric=KitMetric.parse(which), pool=pool)
    return await dag.run("ruined stone temple", 4)


async def test_kit_dag_skips_the_check_without_a_real_worker(tmp_path):
    for sub, pool in (("none", None), ("fake", BlenderPool(1, fake=True))):
        rep = await build_kit(tmp_path / sub, "interior", pool)
        node = rep.nodes["blender_kit_check"]
        assert node.status == "skipped" and node.reason and rep.ok and rep.extras["gates_ok"] and rep.extras["blender_kit_check"] is None
        assert {"step": "dag:blender_kit_check", "reason": node.reason} in rep.extras["skipped"]


@pytest.mark.blender
@needs_blender
@pytest.mark.parametrize("which", ["interior", "exterior"])
async def test_kit_passes_the_blender_kit_check_node(tmp_path, blender_pool, which):
    rep = await build_kit(tmp_path, which, blender_pool)
    check = rep.extras["blender_kit_check"]
    assert rep.nodes["blender_kit_check"].status == "ok" and check["pass"] and rep.ok and rep.extras["gates_ok"], rep.summary()
    assert check["merged"] and len(check["pieces"]) == 4 and all(v["pass"] for v in check["pieces"].values())
    assert "kit_check of EL4CTEO/blender-skills @9e7e04a" in check["source"]


@pytest.mark.blender
@needs_blender
async def test_a_kit_piece_moved_off_its_pivot_fails_the_blender_kit_check_node_and_the_gates(tmp_path, blender_pool):
    pool = OffPivot(blender_pool)
    rep = await build_kit(tmp_path, "interior", pool)
    node = rep.nodes["blender_kit_check"]
    assert pool.moved and node.status == "failed" and "kit_check of EL4CTEO/blender-skills" in node.error and pool.moved in node.error, node
    assert not rep.ok and not rep.extras["gates_ok"]


# ------------------------------------------------------------------------------------------------ native export (harness/tools.py)


async def export_crate(env, monkeypatch) -> tuple[str, dict[str, Any] | None]:
    """The native session's path: export_engine_package -> _verify_engine (engine import and parity simulated, as in test_harness_tools)."""
    from test_harness_tools import CRATE, record_approval, simulate_engine_import

    from antigravity3d.harness import tools as T

    simulate_engine_import(monkeypatch)
    await T.apply_scene_ir(json.dumps(CRATE), env.ctx)
    await T.run_visual_qa(env.ctx)
    record_approval(env)
    await T.generate_lods_and_colliders(env.ctx)
    await T.run_usd_validator(env.ctx)
    reply = await T.export_engine_package(env.ctx, "crate_pkg")
    manifest = env.sess().export_dir / "crate_pkg" / "manifest.json"
    return reply, (json.loads(manifest.read_text()) if manifest.exists() else None)


async def test_native_export_records_a_skipped_check_without_a_real_worker(tmp_path, monkeypatch):
    from test_harness_tools import make_session

    reply, manifest = await export_crate(make_session(tmp_path), monkeypatch)
    assert reply.startswith("EXPORTED") and manifest["validation"]["blender_asset_qa"]["status"] == "skipped"
    assert "no Blender pool" in manifest["validation"]["blender_asset_qa"]["detail"]


async def test_native_export_is_refused_when_the_worker_reports_errors(tmp_path, monkeypatch):
    from test_harness_tools import make_session

    stub = Stub({"ok": True, "pass": False, "errors": ["body: floating/detached part in body"], "warnings": [], "objects": {"Batch_0": {"tris": 24}}})
    reply, manifest = await export_crate(make_session(tmp_path, pool=stub), monkeypatch)
    assert reply.startswith("REFUSED blender_asset_qa:") and "floating/detached part" in reply and manifest is None
    assert stub.calls and stub.calls[0][1]["weld"] is True and stub.calls[0][1]["path"].endswith("Crate.glb")


async def test_a_preview_export_runs_the_same_check_before_the_engine_import(tmp_path, monkeypatch):
    # An unverified preview is exported through its own engine step (no parity, any judge); the Blender check is about
    # the file, not the judge, so it runs there too: refused on errors, recorded as skipped without a real worker.
    from types import SimpleNamespace

    from antigravity3d.adapters.engines import validators
    from antigravity3d.harness import tools as T

    asked = []

    class NoImporter:
        async def validate(self, glb):
            asked.append(str(glb))
            return SimpleNamespace(skipped=True, skipped_reason="no importer here", engine="godot_4", ok=False)

    monkeypatch.setattr(validators, "validator_for", lambda engine: NoImporter())
    (tmp_path / "Crate.glb").write_bytes(b"glTF")
    state = SimpleNamespace(budget=SimpleNamespace(target_engine="godot_4"))

    stub = Stub({"ok": True, "pass": False, "errors": ["body: floating/detached part in body"], "warnings": [], "objects": {"Batch_0": {"tris": 24}}})
    pkg = SimpleNamespace(out_dir=tmp_path, data={"validation": {}})
    reason = await T._preview_engine_import(SimpleNamespace(pool=stub, state=state), pkg, "Crate")
    assert reason.startswith("blender_asset_qa:") and "floating/detached part" in reason
    assert not asked and "blender_asset_qa" not in pkg.data["validation"], "refused before the engine import, nothing recorded"

    pkg = SimpleNamespace(out_dir=tmp_path, data={"validation": {}})
    assert await T._preview_engine_import(SimpleNamespace(pool=None, state=state), pkg, "Crate") is None
    assert pkg.data["validation"]["blender_asset_qa"]["status"] == "skipped" and "no Blender pool" in pkg.data["validation"]["blender_asset_qa"]["detail"]
    assert pkg.data["validation"]["engine_import"]["skipped"] == "no importer here" and asked == [str(tmp_path / "Crate.glb")]


@pytest.mark.blender
@needs_blender
async def test_native_export_of_a_crate_passes_the_check_with_a_real_worker(tmp_path, monkeypatch, blender_pool):
    from test_harness_tools import make_session

    reply, manifest = await export_crate(make_session(tmp_path, pool=blender_pool), monkeypatch)
    qa = manifest["validation"]["blender_asset_qa"]
    assert reply.startswith("EXPORTED") and qa["status"] == "passed" and qa["errors"] == [] and qa["profile"] == "game", (reply, qa)
