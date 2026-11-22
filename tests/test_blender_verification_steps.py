"""The package and the kit are checked by EL4CTEO/blender-skills' scripts, run by the Blender worker, as pipeline steps.

``blender_asset_qa`` (pass C of the prop pipeline, and the native export) runs ``qa_check`` on the GLB the engines import,
vertices merged, with the target engine's profile; ``blender_kit_check`` (the kit DAG) runs ``kit_check`` on each piece
file with the kit's metric. Without a real worker both are skipped with the reason, never passed.
"""

from __future__ import annotations

import json
import struct
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


def test_duplicate_vertices_that_are_parts_in_contact_are_read_as_that_and_others_are_not():
    """After the merge within parts, the positions two parts share are what upstream counts as duplicate vertices. The
    worker says how many vertices those are; the warning is read as parts in contact only when they account for all of it."""
    dup = "Batch_0: duplicate verts=16 loose verts=0"
    weld = lambda apart: {"agy_inspect": {"weld": {"objects": {"Batch_0": {"parts": 36, "coincident_between_parts": apart}}}}}  # noqa: E731
    result = {"errors": [], "warnings": [dup, "hidden back-to-back faces: a<->b (2 pairs)"], "objects": {"Batch_0": {"tris": 544}}}
    assert upstream_checks.read_duplicates({**result, **weld(32)}) == {dup: 32}
    status, detail = upstream_checks.qa_verdict({**result, **weld(32)}, "roblox")
    assert status == "passed" and "vertices merged part by part" in detail
    assert dup + " (all 32 are vertices of separate parts in contact, not unmerged duplicates)" in detail  # upstream's text, then the reading
    for other in (weld(0), weld(8), {}):  # some of the duplicates are not parts in contact, or nothing is known: left as written
        assert upstream_checks.read_duplicates({**result, **other}) == {}
        assert "parts in contact" not in upstream_checks.qa_verdict({**result, **other}, "roblox")[1]
    loose = {**result, "warnings": ["Batch_0: duplicate verts=16 loose verts=3"], **weld(32)}  # loose vertices are a defect of their own
    assert upstream_checks.read_duplicates(loose) == {}


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


def flush_scene() -> SceneIR:
    """Kit-bashed details as a model writes them: a rib crossing a panel of the same thickness on a base (their tops
    share a plane), and a handle grip whose caps are flush with its brackets' outer faces."""
    grip = NodeIR(id="grip", kind="cylinder", params={"radius": 0.012, "height": 0.16, "segments": 12}, material_id="m0",
                  transform=Transform(translate=(-0.525, 0.0, 0.1), rotate_euler_deg=(90.0, 0.0, 0.0)))
    return SceneIR(asset_name="Flush", materials=[MaterialIR(id="m0")], nodes=[
        box("base", (1.0, 1.0, 0.2), (0.0, 0.0, 0.1)), box("panel", (0.4, 0.4, 0.01), (0.0, 0.0, 0.205)),
        box("rib", (0.04, 0.6, 0.01), (0.0, 0.0, 0.205)), grip,
        box("br_f", (0.04, 0.02, 0.03), (-0.52, -0.07, 0.1)), box("br_b", (0.04, 0.02, 0.03), (-0.52, 0.07, 0.1))])


@pytest.mark.blender
@needs_blender
@pytest.mark.parametrize("engine", ["unreal_5", "roblox_r15"])
async def test_flush_details_are_separated_before_qa_and_pass_the_blender_asset_qa_step(tmp_path, blender_pool, engine, monkeypatch):
    """The deterministic separation (kernel/separate.py, step ``separate_coplanar`` of pass A) moves the flush faces 2 mm
    apart; Blender's qa_check on the exported GLB then finds no z-fighting. Negative control: with the step switched off
    (a separation that finds nothing) the same scene fails that check, as it did before the step existed."""
    from antigravity3d.kernel.separate import SeparationResult
    from antigravity3d.tactics import pyramid

    sm = StageManager.create(tmp_path / "on.usda", "Flush")
    sm.apply_ir(flush_scene())
    plane = DecisionControlPlane(FakeDecisionsClient(responder=lambda req: {}))
    kw = dict(plane=plane, author=CallableAuthor(lambda *a, **k: []), governor=Governor(plan_for(2)), budget=EngineBudget(engine),
              uploader=up, forge=ImageForge(FakeImageBackend()), settle_first=True, pool=blender_pool)
    res = await refine(sm, workdir=tmp_path / "on", **kw)
    steps = {s.name: s for s in res.steps}
    assert steps["separate_coplanar"].status == "passed", steps["separate_coplanar"]
    assert steps["blender_asset_qa"].status == "passed", steps["blender_asset_qa"]
    assert "z_fighting" not in steps or steps["z_fighting"].status != "warned" or engine != "roblox_r15"
    monkeypatch.setattr(pyramid, "separate_coplanar", lambda *a, **k: SeparationResult())
    sm = StageManager.create(tmp_path / "off.usda", "Flush")
    sm.apply_ir(flush_scene())
    res = await refine(sm, workdir=tmp_path / "off", **kw)
    failed = dict(res.failed)
    assert "z-fighting" in failed.get("blender_asset_qa", ""), failed


def roblox_scene(tmp_path: Path, name: str, nodes: list[NodeIR]):
    from antigravity3d.kernel import fast_ops
    from antigravity3d.kernel.package import write_engine_package

    sm = StageManager.create(tmp_path / f"{name}.usda", name)
    sm.apply_ir(SceneIR(asset_name=name, materials=[MaterialIR(id="m0")], nodes=nodes))
    sm.commit_all()
    meshes = sm.extract_meshes()
    budget = EngineBudget("roblox_r15", max_tris=60000)
    pkg = write_engine_package(sm, fast_ops.run_export_pipeline(meshes, budget, name), budget, tmp_path / "pkg", asset_name=name, qa={"tier1_ok": True})
    return meshes, budget, pkg


def balls(n: int) -> list[NodeIR]:
    return [NodeIR(id=f"ball{i}", kind="sphere", params={"radius": 0.5, "segments": 40}, material_id="m0", transform=Transform(translate=(0.6 * i, 0, 0.5)))
            for i in range(n)]


@pytest.mark.blender
@needs_blender
async def test_a_roblox_material_over_the_triangle_limit_ships_as_meshes_within_it_and_one_oversized_part_still_fails(tmp_path, blender_pool):
    """The GLB submits the parts of a material in one batch once there are more nodes than the draw-call budget, and Roblox
    imports a batch as one mesh (limit 20,000). The writer splits a material's batch at the limit, parts kept whole: forty
    880-triangle spheres ship as two meshes, and the project's rules and Blender's qa_check agree. A single part over the limit
    cannot be split by the writer: the authored-part rule rejects it, and so does Blender."""
    from antigravity3d.kernel.validator import check_engine_compliance, validate_package

    meshes, budget, pkg = roblox_scene(tmp_path / "balls", "Balls", balls(40))
    assert not [i for i in check_engine_compliance(None, meshes, budget) if i.code.endswith("mesh_tri_cap")]  # every part has 880 tris
    batches = pkg.data["glb_batches"]
    assert list(batches) == ["Batch_0", "Batch_1"] and [len(m) for m in batches.values()] == [22, 18], batches
    assert pkg.ok and not [i for i in validate_package(pkg.out_dir) if i.severity == "critical"], [i.code for i in pkg.issues if i.severity == "critical"]
    assert pkg.data["draw_calls"] == 2 and pkg.data["roblox_surface_appearance"]["meshparts_expected"] == 2
    assert any("2 GLB meshes" in n for n in pkg.data["notes"])  # the note counts the split meshes
    status, detail, _ = await upstream_checks.asset_qa(blender_pool, str(pkg.out_dir / "Balls.glb"), "roblox_r15")
    assert status == "passed" and "35200 tris" in detail, detail  # all of it ships, as two meshes
    big = [NodeIR(id="big", kind="sphere", params={"radius": 0.5, "segments": 950}, material_id="m0", transform=Transform(translate=(0, 0, 0.5)))]
    meshes, budget, pkg = roblox_scene(tmp_path / "big", "Big", big)
    assert len(meshes[0].triangles) > 20_000
    assert [i.code for i in check_engine_compliance(None, meshes, budget) if i.code.endswith("mesh_tri_cap")] == ["roblox_r15:mesh_tri_cap"]
    assert not pkg.ok
    status, detail, _ = await upstream_checks.asset_qa(blender_pool, str(pkg.out_dir / "Big.glb"), "roblox_r15")
    assert status == "failed" and "> roblox limit 20000" in detail


def test_the_rule_on_what_ships_catches_a_writer_that_does_not_split(tmp_path, monkeypatch):
    """The per-mesh check on the shipped GLB is the proof that the writer did its job: with the split switched off, the
    project's own rules (the engine check at write time and validate_package on the files) reject forty 880-triangle spheres,
    which the authored-part rule alone lets through."""
    from antigravity3d.kernel import package
    from antigravity3d.kernel.validator import check_engine_compliance, validate_package

    monkeypatch.setattr(package, "split_batch", lambda members, tris, cap: [list(members)])
    meshes, budget, pkg = roblox_scene(tmp_path, "Balls", balls(40))
    assert not [i for i in check_engine_compliance(None, meshes, budget) if i.code.endswith("mesh_tri_cap")]
    assert list(pkg.data["glb_batches"]) == ["Batch_0"] and not pkg.ok
    codes = {i.code: i.detail for i in pkg.issues if i.severity == "critical"}
    assert "35200 tris in one shipped GLB mesh > 20000" in codes["roblox_r15:batch_tri_cap"] and "pkg:glb_batch_tri_cap" in codes
    assert {i.code for i in validate_package(pkg.out_dir) if i.severity == "critical"} == {"pkg:glb_batch_tri_cap"}  # re-read from the shipped GLB


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
async def test_separate_boxes_resting_on_the_ground_fail_as_the_script_says_and_a_part_hovering_5_cm_fails(tmp_path, blender_pool):
    """qa_check calls every piece but the biggest that touches nothing "floating", with no notion of ground, and the gate
    reads it as it is (as the scorecard's independent check does): two pieces side by side on the floor are one asset
    with a detached piece. The worker still lists the pieces with their lowest z, and the detail says which is on the
    ground (live 2026-11-22, r5_crate_C: a skid nothing touched passed the gate this way and failed the independent check)."""
    side = [box("a", (1, 1, 1), (0, 0, 0.5)), box("b", (1, 1, 1), (2, 0, 0.5))]
    status, detail, data = await upstream_checks.asset_qa(blender_pool, package_glb(tmp_path / "side", side), "unreal_5")
    assert status == "failed" and "on the ground, touching no other part" in detail and "floating/detached part" in detail, detail
    assert len(data["errors"]) == 1 and data["errors"][0].startswith("floating/detached part") and data["resting_on_ground"] == data["errors"]  # upstream's text, untouched
    hover = [box("base", (1, 1, 1), (0, 0, 0.5)), box("hover", (0.3, 0.3, 0.3), (0, 0, 1.2))]  # its underside is 5 cm above the top
    status, detail, data = await upstream_checks.asset_qa(blender_pool, package_glb(tmp_path / "hover", hover), "unreal_5")
    assert status == "failed" and "floating/detached part" in detail and data["resting_on_ground"] == [], detail
    raised = [box("base", (1, 1, 1), (0, 0, 0.5)), box("raised", (0.3, 0.3, 0.3), (3, 0, 0.35))]  # separate, 20 cm off the ground
    status, _, _ = await upstream_checks.asset_qa(blender_pool, package_glb(tmp_path / "raised", raised), "unreal_5")
    assert status == "failed"


@pytest.mark.blender
@needs_blender
async def test_qa_check_reads_a_flush_part_as_the_kernel_does_and_as_a_contact_after_attach(tmp_path, blender_pool):
    """A pole standing flush on a wider base, as the file ships it (the pole's bottom face left out). Whether the base
    touches the pole is then decided by which way two float32 heights rounded, and qa_check and Tier-1 read it the same
    way: on the seed whose pole is 35 nm under the base's top both find a contact (an edge of the base passes through
    the pole's wall); on the seed whose pole is 4 nm over it Tier-1 reports the base and qa_check says "not touching"
    (it passes the file only because a ray from the base's centre leaves through the pole, so it calls the base
    enclosed). There attach sets the base a millimetre into the pole and both read a contact. Before 2026-11-22 attach
    moved it by the 4 nm instead, the kernel's trial passed, and qa_check still said "not touching". Measured with
    Blender 5.3; the seeds are those of tests/test_attach.py's _flush_lamp. (In 60 random flush contacts,
    2026-11-22, Tier-1 never passed a part qa_check read as not touching; it reported 24 that qa_check read as touching.)"""
    from test_attach import _flush_lamp

    from antigravity3d.ir import IRPatch
    from antigravity3d.kernel import attach, coplanar, fast_ops
    from antigravity3d.kernel.package import write_engine_package
    from antigravity3d.tactics import pyramid

    budget = EngineBudget("unreal_5")

    async def read(seed: int, fix: bool) -> tuple[list[str], str, list[str], list[str]]:
        d = tmp_path / f"{seed}{'_attached' if fix else ''}"
        d.mkdir()
        sm = StageManager.create(d / "s.usda", "Prop")
        sm.apply_ir(_flush_lamp(seed))
        meshes = sm.extract_meshes()
        if fix:
            sm.apply_patch(IRPatch(ops=attach.attach(meshes, budget, pyramid.reach_of(meshes)).ops(sm.base_ir())))
        sm.commit_all()
        meshes = sm.extract_meshes()
        reported = sorted(pyramid.floating_ids(fast_ops.tier1_qa(meshes, budget)))
        bundle = fast_ops.run_export_pipeline(meshes, budget, "Prop", opaque_materials=coplanar.opaque_materials(sm.base_ir()))
        pkg = write_engine_package(sm, bundle, budget, d / "pkg", asset_name="Prop", qa={"tier1_ok": True})
        status, _, data = await upstream_checks.asset_qa(blender_pool, str(pkg.out_dir / "Prop.glb"), "unreal_5")
        return reported, status, data["errors"], [w for w in data["warnings"] if "not touching" in w]

    assert await read(0, False) == ([], "passed", [], [])
    assert await read(2, False) == (["base"], "passed", [], ["1 parts fully enclosed, not touching (eyes/internals?): ['Batch_0']"])
    assert await read(2, True) == ([], "passed", [], [])


def flip_first_triangle(glb: Path) -> None:
    """Rewrite the GLB with the first triangle of its first mesh wound the other way (two indices swapped in place)."""
    raw = bytearray(glb.read_bytes())
    n = struct.unpack_from("<I", raw, 12)[0]
    doc = json.loads(raw[20:20 + n])
    acc = doc["accessors"][doc["meshes"][0]["primitives"][0]["indices"]]
    at = 20 + n + 8 + doc["bufferViews"][acc["bufferView"]].get("byteOffset", 0) + acc.get("byteOffset", 0)
    size = {5123: 2, 5125: 4}[acc["componentType"]]
    raw[at + size:at + 3 * size] = raw[at + 2 * size:at + 3 * size] + raw[at + size:at + 2 * size]
    glb.write_bytes(raw)


@pytest.mark.blender
@needs_blender
async def test_separate_parts_sharing_edges_in_one_mesh_pass_and_a_miswound_part_still_fails(tmp_path, blender_pool):
    """A lid on a body of the same footprint: every part is a closed, consistently wound box, and their rims share four
    edges. With more parts than the draw-call budget the GLB puts them in one mesh. Merged across the mesh they read as
    one surface with faces wound against each other, and qa_check failed the asset on "edges with inconsistent face
    winding" (the 55-part crate on roblox_r15, 2026-11-21); merged part by part they are what they are. Negative
    control: the same file with one triangle really wound the other way still fails, with upstream's message."""
    nodes = [box("body", (1.0, 0.6, 0.4), (0, 0, 0.2)), box("lid", (1.0, 0.6, 0.1), (0, 0, 0.45)), box("cap", (1.0, 0.6, 0.05), (0, 0, 0.525)),
             box("pad_a", (0.2, 0.2, 0.05), (-0.3, 0, 0.575)), box("pad_b", (0.2, 0.2, 0.05), (0.3, 0, 0.575))]
    _, _, pkg = roblox_scene(tmp_path, "Stack", nodes)
    assert list(pkg.data["glb_batches"]) == ["Batch_0"]  # five parts, one mesh
    glb = pkg.out_dir / "Stack.glb"
    status, detail, data = await upstream_checks.asset_qa(blender_pool, str(glb), "roblox_r15")
    assert status == "passed" and "inconsistent face winding" not in detail and "non-manifold" not in detail, detail
    assert data["pieces_after_merge"] == {"Batch_0": 5} and data["merged_within"] == "parts"  # five closed boxes, as authored
    # body/lid and lid/cap share their four rim corners: 8 positions, 16 vertices, read as parts in contact
    assert list(data["parts_in_contact_share_vertices"].values()) == [16], data
    assert "(all 16 are vertices of separate parts in contact, not unmerged duplicates)" in detail
    flip_first_triangle(glb)
    status, detail, _ = await upstream_checks.asset_qa(blender_pool, str(glb), "roblox_r15")
    assert status == "failed" and "edges with inconsistent face winding" in detail, detail


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
