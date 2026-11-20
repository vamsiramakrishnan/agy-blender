from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

import numpy as np
from PIL import Image

from antigravity3d.adapters.blender_5_3.pool import BlenderPool
from antigravity3d.decisions.engine import DecisionControlPlane
from antigravity3d.decisions.fakes import FakeDecisionsClient
from antigravity3d.decisions.questions import MARKED_DEFECT_PHRASES
from antigravity3d.genmedia.nano_banana import FakeImageBackend, ImageForge
from antigravity3d.genmedia.pbr_deriver import derive_pbr
from antigravity3d.ir import IRPatch, MaterialIR, NodeIR, SceneIR, SetTransform, Transform
from antigravity3d.kernel.fast_ops import tier1_qa
from antigravity3d.kernel.usd_stage import StageManager
from antigravity3d.state import EngineBudget
from antigravity3d.tactics.authoring import CallableAuthor
from antigravity3d.tactics.distill import FixLog
from antigravity3d.tactics.governor import Governor, plan_for
from antigravity3d.tactics.pyramid import refine

BUDGET = EngineBudget("unreal_5")


def floating_scene() -> SceneIR:
    return SceneIR(asset_name="Prop", materials=[MaterialIR(id="wood")], nodes=[
        NodeIR(id="body", kind="box", params={"size": (1, 1, 1)}, transform=Transform(translate=(0, 0, 0.5)), material_id="wood"),
        NodeIR(id="lid", kind="box", params={"size": (1.04, 1.04, 0.05)}, transform=Transform(translate=(0, 0, 1.325)), material_id="wood")])


LID_DOWN = IRPatch(ops=[SetTransform(node_id="lid", transform=Transform(translate=(0, 0, 1.025)))])


class Script:
    """Programmable FakeDecisionsClient responder keyed by call kind (blockout QA / lookdev QA / locate)."""

    def __init__(self, blockout: Callable[[int], dict] = lambda n: {}, lookdev: Callable[[int], dict] = lambda n: {}, locate: float = 0.9) -> None:
        self.blockout, self.lookdev, self.locate, self.n = blockout, lookdev, locate, {"blockout": 0, "lookdev": 0}

    def __call__(self, req: dict) -> dict:
        qs = req["questions"]
        if any("__" in q for q in qs):  # set-of-mark localisation (LOD-pop items share the "__" syntax but are not marked defects)
            return {q: self.locate for q in qs if q.split("__")[0] in MARKED_DEFECT_PHRASES}
        if "score_silhouette" not in qs:
            return {}
        kind = "lookdev" if "defect_cg_plastic" in qs else "blockout"
        self.n[kind] += 1
        return (self.blockout if kind == "blockout" else self.lookdev)(self.n[kind])


def setup(tmp_path: Path, script: Script, scene: SceneIR | None = None) -> tuple[StageManager, DecisionControlPlane]:
    sm = StageManager.create(tmp_path / "p.usda", "Prop")
    sm.apply_ir(scene or floating_scene())
    return sm, DecisionControlPlane(FakeDecisionsClient(responder=script))


def maps() -> Any:
    rng = np.random.default_rng(3)
    return derive_pbr(Image.fromarray(rng.integers(60, 200, (64, 64, 3), dtype=np.uint8)))


async def up(data: bytes) -> str:
    return "fake://sheet"


async def run_a(sm: StageManager, plane: Any, author: CallableAuthor, tmp_path: Path, **kw: Any):
    kw = {"settle_first": False, "run_pass_b": False, "run_pass_c": False, **kw}
    return await refine(sm, plane=plane, author=author, governor=kw.pop("governor", Governor(plan_for(2))), budget=BUDGET, uploader=up,
                        workdir=tmp_path / "wd", **kw)


FLAGGED = {"defect_floating": 0.9, "defect_clipping": 0.9}  # two high defects: entangled, so agent_repair (not a deterministic fixer)


async def test_converges_when_the_repair_fixes_the_flagged_floating(tmp_path: Path) -> None:
    state: dict[str, Any] = {}

    def repair(scene: SceneIR, *, action: Any, located: dict, manifest: str) -> IRPatch:
        state.update(located=located, manifest=manifest, action=action)
        state["fixed"] = True
        return LID_DOWN

    script = Script(blockout=lambda n: {} if state.get("fixed") else dict(FLAGGED))
    sm, plane = setup(tmp_path, script)
    author = CallableAuthor(lambda *a, **k: [], repair)
    log = FixLog(tmp_path / "fix.jsonl")
    res = await run_a(sm, plane, author, tmp_path, fixlog=log)

    assert [r.action for r in res.rounds] == ["agent_repair", "approve"]
    assert res.converged["A"] and "converged in 2" in res.stopped["A"]
    assert state["action"].kind == "agent_repair" and "lid" in state["located"]["defect_floating"] and "lid" in state["located"]["tier1:floating_part"]
    assert "lid" in state["manifest"]
    assert not [i for i in tier1_qa(sm.extract_meshes(), BUDGET).issues if i.code == "floating_part"]
    assert sm.base_ir().nodes[1].transform.translate == (0, 0, 1.025)
    (rec,) = res.fix_records  # the improving repair is logged
    assert rec.score_after > rec.score_before and rec.ops == ["set_transform"] and rec.params_changed == ["lid.translate"]
    assert len(log.read()) == 1
    assert len(res.audit) == 2 and res.audit[0].startswith("A1 QA[blockout] agent_repair")
    assert res.tier15 > 3.5 and res.tier1_ok and res.author_calls == 1


async def test_regressing_repair_is_rolled_back_and_iteration_stops(tmp_path: Path) -> None:
    worse = {"defect_floating": 0.95, "defect_clipping": 0.95, "defect_inverted_faces": 0.95, "score_silhouette": 1.0}
    script = Script(blockout=lambda n: dict(FLAGGED) if n == 1 else dict(worse))
    sm, plane = setup(tmp_path, script)
    before, txs = sm.base_ir(), list(sm.tx_ids)
    bad = IRPatch(ops=[SetTransform(node_id="lid", transform=Transform(translate=(5, 0, 5)))])
    author = CallableAuthor(lambda *a, **k: [], lambda *a, **k: bad)
    res = await run_a(sm, plane, author, tmp_path, max_rounds=4)

    assert res.rounds[-1].rolled_back and "rolled back" in res.stopped["A"] and res.converged["A"] is False
    assert sm.base_ir() == before and sm.tx_ids == txs  # the transaction is gone, not just undone by a second patch
    assert author.calls["repair"] == 1 and len(res.rounds) == 2  # stopped iterating on this path
    assert res.fix_records == []
    assert res.tier15 == res.rounds[0].tier15  # reported state is the pre-repair state


async def test_auto_fix_paths_make_zero_author_calls(tmp_path: Path) -> None:
    script = Script(blockout=lambda n: {"defect_floating": 0.9} if n == 1 else {}, lookdev=lambda n: {"defect_cg_plastic": 0.9} if n == 1 else {})
    sm, plane = setup(tmp_path, script)
    m = maps()
    orm_before = m.orm.tobytes()
    author = CallableAuthor(lambda *a, **k: [], lambda *a, **k: LID_DOWN)
    res = await run_a(sm, plane, author, tmp_path, run_pass_b=True, textures={"wood": m})

    assert author.calls == {"propose": 0, "repair": 0} and res.author_calls == 0
    assert [(r.stage, r.action, r.fixer) for r in res.rounds] == [("A", "auto_fix", "reground"), ("A", "approve", None),
                                                                   ("B", "auto_fix", "roughness_breakup"), ("B", "approve", None)]
    assert not [i for i in tier1_qa(sm.extract_meshes(), BUDGET).issues if i.code == "floating_part"]  # settled deterministically
    assert res.objects["maps"]["wood"].orm.tobytes() != orm_before  # roughness breakup was applied to the real map
    assert [r.ops for r in res.fix_records] == [["set_transform"], ["roughness_breakup"]]
    assert (tmp_path / "wd" / "textures" / "wood_orm.png").exists()


async def test_budget_exhaustion_stops_before_a_repair(tmp_path: Path) -> None:
    sm, plane = setup(tmp_path, Script(blockout=lambda n: dict(FLAGGED)))
    gov = Governor(plan_for(2))
    gov.charge("spent", 0.849)
    author = CallableAuthor(lambda *a, **k: [], lambda *a, **k: LID_DOWN)
    res = await run_a(sm, plane, author, tmp_path, governor=gov)
    assert res.stopped["A"] == "budget exhausted" and author.calls["repair"] == 0


class NoLocate:
    """A plane without locate_defects (an older control plane)."""

    def __init__(self, plane: Any) -> None:
        self._p = plane

    def __getattr__(self, name: str) -> Any:
        if name == "locate_defects":
            raise AttributeError(name)
        return getattr(self._p, name)


async def test_full_run_records_every_step_and_skips_carry_reasons(tmp_path: Path) -> None:
    state: dict[str, Any] = {}
    script = Script(blockout=lambda n: {} if state.get("fixed") else dict(FLAGGED))
    sm, plane = setup(tmp_path, script)
    author = CallableAuthor(lambda *a, **k: [], lambda *a, **k: (state.update(fixed=True), LID_DOWN)[1])
    res = await refine(sm, plane=NoLocate(plane), author=author, governor=Governor(plan_for(2)), budget=BUDGET, uploader=up, workdir=tmp_path / "wd",
                       forge=ImageForge(FakeImageBackend()), settle_first=False)
    status = {s.name: s.status for s in res.steps}
    skipped = dict(res.skipped)
    assert not res.failed, res.failed
    for name in ("textures", "uv_lod_colliders", "usd_validate", "engine_compliance", "export_glb"):
        assert status[name] == "passed", (name, status)
    # an all-box prop has nothing to simplify within the screen-space error budget: LOD0 only, nothing to pop
    assert {"locate_defects", "render_tier", "paintover", "engine_import", "engine_parity", "lod_pop", "lod_pop_vision"} <= set(skipped)
    assert "LOD0 only" in skipped["lod_pop"]
    assert all(reason.strip() for reason in skipped.values())
    assert "no locate_defects" in skipped["locate_defects"] or "no locate_defects" in skipped["locate_defects"].replace("plane has ", "")
    assert "not found" in skipped["engine_import"] and "paintover_loop disabled" in skipped["paintover"] and "no Blender pool" in skipped["render_tier"]
    assert res.render_tier == "preview" and res.ok
    assert Path(res.artifacts["usd"]).exists() and Path(res.artifacts["glb"]).exists()
    assert res.to_dict()["skipped"] and all(r["reason"] for r in res.to_dict()["skipped"])
    # the shipped package is loaded in three.js when a browser exists; otherwise the step says what is missing
    assert status["web_runtime"] in ("passed", "skipped"), status
    if status["web_runtime"] == "passed":
        assert Path(res.artifacts["web_render"]).exists() and res.scores["web_silhouette_iou"] >= 0.98
        assert not list((tmp_path / "wd").glob("web_three_*")), "the verifier's scratch directory must not be left behind"
    else:
        assert "playwright" in skipped["web_runtime"] or "browser" in skipped["web_runtime"]


async def test_web_runtime_step_is_skipped_with_the_reason_or_fails_the_run(tmp_path: Path, monkeypatch) -> None:
    from antigravity3d.adapters.engines import web_three
    from antigravity3d.tactics import pyramid

    async def run(sub: str):
        sm, plane = setup(tmp_path / sub, Script())
        return await refine(sm, plane=plane, author=CallableAuthor(lambda *a, **k: []), governor=Governor(plan_for(2)), budget=BUDGET,
                            uploader=up, workdir=tmp_path / sub / "wd", forge=ImageForge(FakeImageBackend()), settle_first=False)

    # no browser: skipped with the verifier's own reason, and the run is still a pass
    monkeypatch.setattr(web_three.WebThreeValidator, "_unavailable", lambda self: "web_three browser not found (set $CHROMIUM_BIN)")
    res = await run("absent")
    assert dict(res.skipped)["web_runtime"] == "web_three browser not found (set $CHROMIUM_BIN)" and res.ok
    assert "web_render" not in res.artifacts

    # the runtime rejects the package: the step fails with the failing checks named, and so does the run
    class Rejecting(web_three.WebThreeValidator):
        async def validate(self, package_path, timeout=300, **kw):
            checks = (web_three.Check("no loader/console/page errors", "fail", "THREE.GLTFLoader: Unexpected end of JSON input"),
                      web_three.Check("triangles == manifest tris", "pass", "loaded 24, manifest 24"))
            return web_three.WebThreeReport(False, web_three.ENGINE, "FAIL", checks=checks)

    monkeypatch.setattr(pyramid, "WebThreeValidator", Rejecting)
    res = await run("rejected")
    failed = dict(res.failed)
    assert "Unexpected end of JSON input" in failed["web_runtime"] and "triangles ==" not in failed["web_runtime"]
    assert not res.ok


async def test_hero_plan_with_a_fake_pool_and_no_engine_render(tmp_path: Path) -> None:
    sm, plane = setup(tmp_path, Script())
    pool = BlenderPool(1, fake=True)  # never started: a fake pool must not be used as a renderer
    forge = ImageForge(FakeImageBackend())
    res = await refine(sm, plane=plane, author=CallableAuthor(lambda *a, **k: []), governor=Governor(plan_for(3)), budget=BUDGET, uploader=up,
                       workdir=tmp_path / "wd", forge=forge, pool=pool, textures={"wood": maps()}, settle_first=False)
    skipped = dict(res.skipped)
    assert "pool is fake" in skipped["render_tier"] and res.render_tier == "preview"
    assert next(s for s in res.steps if s.name == "paintover").status == "passed" and "paintover_uri" in res.artifacts
    assert "no engine render available" in skipped["engine_parity"]  # plan wants engine truth, but no engine render exists
    assert asyncio.iscoroutinefunction(pool.call)
