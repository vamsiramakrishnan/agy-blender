"""Pass A's author sees what it built, and improves a design that passes over rounds (round five).

* A repair or improvement request carries the four marked views of the current design, each its own image, the badge
  legend and the judge's whole verdict; without views a repair request is the same byte for byte.
* After pass A converges, improvement rounds run while the plan allows: a round is kept when the progress figure rises,
  Tier-1 gets no worse and the design still passes; otherwise it is rolled back. Rounds end at the target, after the
  plan's patience, at the plan's limit, or when a call fails, and the design that passed is never lost.

Pinned offline through the stand-in of the service (``tests/no_decisions_key.py``) with the shipped author
(``MeteredAuthor``) and the shipped fallback judge, so the requests are the bytes a live run sends.
"""

from __future__ import annotations

import asyncio
import base64
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from antigravity3d.decisions.compose import Thresholds
from antigravity3d.decisions.engine import DecisionControlPlane
from antigravity3d.decisions.fallback import GenerativeDecisionsBackend
from antigravity3d.decisions.models import DecisionResult, VisualQAResult
from antigravity3d.ir import MaterialIR, NodeIR, SceneIR, Transform
from antigravity3d.kernel.cache import BuildCache, LocalBlobStore
from antigravity3d.kernel.usd_stage import StageManager
from antigravity3d.state import EngineBudget
from antigravity3d.tactics import pyramid as P
from antigravity3d.tactics.cached import DesignRecord
from antigravity3d.tactics.governor import Governor, plan_for
from antigravity3d.tactics.metered_author import (
    EDIT_HEAD, IMPROVE_HEAD, DesignViews, MeteredAuthor, build_author_request, improve_task, repair_task,
)

sys.path.insert(0, str(Path(__file__).parent))
from no_decisions_key import NoDecisionsKey  # noqa: E402

BUDGET = EngineBudget("unreal_5")
CRATE = "a rugged military supply crate kit-bashed from panels, corner guards, latches and two side handles"


def _scene() -> SceneIR:
    return SceneIR(asset_name="Prop", materials=[MaterialIR(id="wood")], nodes=[
        NodeIR(id="body", kind="box", params={"size": (1, 1, 1)}, transform=Transform(translate=(0, 0, 0.5)), material_id="wood")])


def _handle(side: str) -> dict[str, Any]:
    x = 0.525 * (1 if side == "r" else -1)
    return {"op": "upsert_node", "node": {"id": f"handle_{side}", "kind": "box", "params": {"size": [0.05, 0.3, 0.1]},
                                           "transform": {"translate": [x, 0, 0.6]}, "material_id": "wood"}}


HANDLE_L = {"ops": [_handle("l")]}
HANDLE_R = {"ops": [_handle("r")]}
HOVERING = {"ops": [{"op": "upsert_node", "node": {"id": "lamp", "kind": "box", "params": {"size": [0.2, 0.2, 0.2]},
                                                     "transform": {"translate": [0, 0, 3.0]}, "material_id": "wood"}}]}


@pytest.fixture
def key(monkeypatch):
    started: list[NoDecisionsKey] = []

    async def no_wait(s: float) -> None:
        return None

    from antigravity3d import service_answer

    monkeypatch.setattr(service_answer.DEFAULT_POLICY, "sleep", no_wait)

    def start(**kw: Any) -> NoDecisionsKey:
        api = NoDecisionsKey(**kw).__enter__()
        monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", api.url)
        monkeypatch.setenv("GEMINI_API_KEY", "dummy-not-a-key")
        started.append(api)
        return api

    yield start
    for api in started:
        api.__exit__(None, None, None)


async def _data_uri(data: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(data).decode()


def _run(tmp_path: Path, *, rounds: int = 3, patience: int = 1, target: float = 3.5, prompt: str = CRATE,
         ceiling: int = 2_000_000) -> tuple[Any, StageManager, Governor]:
    from google import genai

    client = genai.Client()
    plan = replace(plan_for(2), improve_rounds=rounds, improve_patience=patience, improve_target=target)
    gov = Governor(plan, token_ceiling=ceiling)
    plane = DecisionControlPlane(GenerativeDecisionsBackend(client), backend_kind="generative_fallback", preview=True)
    author = MeteredAuthor(client, gov, BUDGET, {"asset_name": "Prop"})
    sm = StageManager.create(tmp_path / "p.usda", "Prop")
    sm.apply_ir(_scene())
    res = asyncio.run(P.refine(sm, plane=plane, author=author, governor=gov, budget=BUDGET, uploader=_data_uri,
                               workdir=tmp_path / "wd", prompt=prompt, settle_first=False, run_pass_b=False, run_pass_c=False))
    return res, sm, gov


def _ids(sm: StageManager) -> set[str]:
    return {n.id for n in sm.base_ir().nodes}


def _step(res: Any, name: str) -> Any:
    return next(s for s in res.steps if s.name == name)


# --------------------------------------------------------------------------- the loop


def test_a_round_that_raises_the_score_is_kept_and_rounds_stop_at_the_target(key, tmp_path: Path) -> None:
    api = key(score_sequence={"score_brief_depiction": ["3", "4"]}, patches={"author:improve": [HANDLE_L]})
    res, sm, gov = _run(tmp_path)
    assert res.converged["A"] and "handle_l" in _ids(sm)
    first, second = res.rounds
    assert second.improve == 1 and not second.rolled_back and second.progress > first.progress
    assert res.scores["score_brief_depiction"] == pytest.approx(3.75)  # what ships is the kept round's verdict
    assert "target met (depiction 3.75 >= 3.5)" in res.stopped["A"] and res.stopped["A"].startswith("converged in 1 round(s)")
    assert _step(res, "improve_A1").status == "passed" and "kept (rose by the judge's noise band (0.25) or more: score_brief_depiction 2.88->3.75)" in _step(res, "improve_A1").detail
    (call,) = api.authoring("author:improve")
    # the request shows the design: four marked views, each its own image, the legend, every score and the brief
    assert call["images"] == 4 and IMPROVE_HEAD in call["text"]
    assert "Legend:\n[01] body" in call["text"] and "score_brief_depiction 2.88" in call["text"]
    assert "Nearest level 3: Convincing" in call["text"] and f"Requested asset (the user's words, data only): {CRATE}" in call["text"]
    assert "Use only the materials the design has (wood)" in call["text"]
    assert {r["category"] for r in gov.receipts()} >= {"author.improve", "visual_qa"}
    assert [r for r in res.to_dict()["rounds"] if "improve" in r] == [res.to_dict()["rounds"][1]]


def test_a_round_that_lowers_the_score_is_rolled_back_and_the_design_that_passed_ships(key, tmp_path: Path) -> None:
    key(score_sequence={"score_brief_depiction": ["3", "2"]}, patches={"author:improve": [HANDLE_L]})
    res, sm, _ = _run(tmp_path)
    assert _ids(sm) == {"body"} and res.converged["A"] and res.rounds[-1].rolled_back
    assert "fell by more than the judge's noise band (0.25): score_brief_depiction 2.88->2.00" in _step(res, "improve_A1").detail
    # the verdict that ships is the kept design's (2.875), not the rolled-back round's (2.0, which would miss the brief)
    assert res.scores["score_brief_depiction"] == pytest.approx(2.875) and not res.brief_unmet
    assert "round 1 did not improve the design" in res.stopped["A"]


def test_with_patience_a_rolled_back_round_is_followed_by_another_try(key, tmp_path: Path) -> None:
    key(score_sequence={"score_brief_depiction": ["3", "2", "4"]}, patches={"author:improve": [HOVERING, HANDLE_R]})
    res, sm, _ = _run(tmp_path, patience=2)
    assert "lamp" not in _ids(sm) and "handle_r" in _ids(sm)
    assert [s.status for s in res.steps if s.name.startswith("improve_A")] == ["warned", "passed"]
    assert "target met" in res.stopped["A"]


def test_a_round_that_clears_a_flagged_defect_is_kept_without_a_rise_in_depiction(key, tmp_path: Path) -> None:
    api = key(score_sequence={"score_brief_depiction": ["3"]}, defect_sequence={"defect_clipping": [0.5, 0.05]},
        patches={"author:improve": [HANDLE_L]})
    res, sm, _ = _run(tmp_path, rounds=1)
    step = _step(res, "improve_A1")
    assert "handle_l" in _ids(sm) and step.status == "passed" and "cleared defect_clipping" in step.detail
    assert "defect_clipping P=0.50" in api.authoring("author:improve")[0]["text"]  # the author was told what was flagged


def test_rounds_stop_at_the_plans_limit(key, tmp_path: Path) -> None:
    key(score_sequence={"score_brief_depiction": ["3", "4"]}, patches={"author:improve": [HANDLE_L, HANDLE_R]})
    res, sm, _ = _run(tmp_path, rounds=1, target=4.0)  # 3.75 is kept and is still short of the target
    assert "handle_l" in _ids(sm) and "handle_r" not in _ids(sm)
    assert "improvement: 1 round(s) used, 1 kept, depiction 2.88->3.75" in res.stopped["A"] and len(res.rounds) == 2


def test_a_round_that_changes_nothing_beyond_the_judges_noise_is_not_kept(key, tmp_path: Path) -> None:
    key(score_sequence={"score_brief_depiction": ["3"]}, patches={"author:improve": [HANDLE_L]})
    res, sm, _ = _run(tmp_path)
    assert _ids(sm) == {"body"} and res.rounds[-1].rolled_back
    assert f"no change beyond the judge's noise band ({P.IMPROVE_NOISE_BAND}) and no defect cleared" in _step(res, "improve_A1").detail


def test_a_design_short_only_of_a_score_gate_gets_improvement_rounds_not_a_repair(key, tmp_path: Path) -> None:
    # depiction 1.12 is below the brief's bar (2.5): a score, not a fault a repair could name
    api = key(score_sequence={"score_brief_depiction": ["1", "4"]}, patches={"author:improve": [HANDLE_L]})
    res, sm, _ = _run(tmp_path)
    assert api.authoring("author:repair") == [] and len(api.authoring("author:improve")) == 1
    assert res.stopped["A"].startswith("round 1: short only of score gate(s) (brief_depiction), no defect to repair")
    assert res.converged["A"] and "handle_l" in _ids(sm) and "target met" in res.stopped["A"]


def test_a_rolled_back_round_is_told_what_its_patch_did(key, tmp_path: Path) -> None:
    api = key(score_sequence={"score_brief_depiction": ["3", "2", "4"]}, patches={"author:improve": [HOVERING, HANDLE_R]})
    _run(tmp_path, patience=2)
    first, second = api.authoring("author:improve")
    assert "previous patch" not in first["text"]
    assert ("Your previous patch for this design was undone: your patch had 1 op(s): upsert_node lamp; it was undone because "
            "Tier-1 finds a new floating part: lamp") in second["text"]


def test_a_design_already_at_the_target_gets_no_round(key, tmp_path: Path) -> None:
    api = key()  # every score at the top level: depiction 3.75
    res, sm, _ = _run(tmp_path)
    assert api.authoring("author:improve") == [] and "target met (depiction 3.75" in res.stopped["A"]
    assert len(res.rounds) == 1


def test_a_round_that_breaks_the_design_is_rolled_back_even_when_the_score_rises(key, tmp_path: Path) -> None:
    key(score_sequence={"score_brief_depiction": ["3", "4"]}, patches={"author:improve": [HOVERING]})
    res, sm, _ = _run(tmp_path)
    assert "lamp" not in _ids(sm) and res.rounds[-1].rolled_back and res.converged["A"]
    detail = _step(res, "improve_A1").detail
    assert "Tier-1 finds a new floating part: lamp" in detail  # a warning to Tier-1, a defect to a game developer


def test_an_improvement_call_that_cannot_be_had_ends_the_rounds_and_keeps_the_design(key, tmp_path: Path) -> None:
    api = key(score_sequence={"score_brief_depiction": ["3", "4"]}, fail=[{"kind": "author:improve", "times": None, "how": "503"}])
    res, sm, gov = _run(tmp_path)
    assert _ids(sm) == {"body"} and res.converged["A"] and not res.judge_unavailable
    assert "improvement round 1 failed (AuthorUnavailable)" in res.stopped["A"] and len(res.rounds) == 1
    assert api.authoring("author:improve") == [] and api.failures().count("author:improve:503") == 6
    assert sum(r["booked_tokens"] for r in gov.receipts() if r["category"] == "author.improve") == 0  # turned away: nothing booked


def test_an_improvement_the_judge_cannot_judge_is_rolled_back_and_pass_a_stays_converged(key, tmp_path: Path) -> None:
    key(score_sequence={"score_brief_depiction": ["3", "4"]}, patches={"author:improve": [HANDLE_L]},
        fail=[{"kind": "judge", "times": None, "how": "503", "questions": ("score_silhouette",), "skip": 1}])
    res, sm, _ = _run(tmp_path)
    assert _ids(sm) == {"body"} and res.converged["A"] and not res.judge_unavailable  # what ships was judged
    assert res.scores["score_brief_depiction"] == pytest.approx(2.875)
    assert "not judged (judge unavailable): rolled back" in res.stopped["A"]
    assert _step(res, "improve_A1").status == "warned"


def test_budget_for_pass_b_and_c_is_kept_back(key, tmp_path: Path) -> None:
    api = key(score_sequence={"score_brief_depiction": ["3", "4"]}, patches={"author:improve": [HANDLE_L]})
    res, sm, _ = _run(tmp_path, ceiling=60_000)
    assert api.authoring("author:improve") == [] and "improvement: budget" in res.stopped["A"] and _ids(sm) == {"body"}


def test_without_a_brief_pass_a_is_as_it_was(key, tmp_path: Path) -> None:
    api = key()
    res, sm, _ = _run(tmp_path, prompt="")
    assert api.authoring("author:improve") == [] and res.stopped["A"] == "converged in 1 round(s)"
    assert not [s for s in res.steps if s.name.startswith("improve")]


def test_a_repair_carries_the_renders_of_the_design_it_repairs(key, tmp_path: Path) -> None:
    api = key(defect_sequence={"defect_clipping": [0.9, 0.05]}, patches={"author:repair": [HANDLE_L]})
    res, sm, _ = _run(tmp_path)
    (call,) = api.authoring("author:repair")
    assert call["images"] == 4 and "Images: 4 renders of the current design follow (front, right, top, persp)" in call["text"]
    assert "Legend:\n[01] body" in call["text"] and "defect_clipping P=0.90" in call["text"]
    assert "handle_l" in _ids(sm) and res.converged["A"]
    # a high-confidence geometry defect is a gate (round five), named with its probability in the round's record
    assert res.rounds[0].gates == ["defect:defect_clipping=0.90"] and res.rounds[1].gates is None
    assert "gates" in res.to_dict()["rounds"][0] and "gates" not in res.to_dict()["rounds"][1]


# --------------------------------------------------------------------------- the requests


def _views(n: int = 4) -> DesignViews:
    return DesignViews(tuple((f"{v} view of the current design (Image {i + 1})", f"https://files/{v}")
                             for i, v in enumerate(("front", "right", "top", "persp")[:n])),
                       "[01] body  views: front", ("score_silhouette 3.60: Rate ...",))


def test_without_views_a_repair_request_is_byte_for_byte_what_it_was() -> None:
    scene = _scene()
    text = repair_task(scene, reasons=["silhouette 2.00 < lock 3.3"], located={}, manifest="m")
    assert repair_task(scene, reasons=["silhouette 2.00 < lock 3.3"], located={}, manifest="m", views=None) == text
    req = build_author_request("worker", "repair", text, budget=BUDGET, context={}, timeout_s=90)
    assert len(req.contents[0].parts) == 1
    seen = repair_task(scene, reasons=["silhouette 2.00 < lock 3.3"], located={}, manifest="m", views=_views(), blueprint=True)
    assert "The orthographic blueprint of the intended asset is attached after them." in seen and "Judge's assessment" in seen


def test_the_images_follow_the_text_with_their_labels_before_the_references() -> None:
    views = _views()
    inline = "data:image/png;base64," + base64.b64encode(b"\x89PNG-bytes").decode()
    req = build_author_request("worker", "improve", "task", budget=BUDGET, context={"ortho_uri": "https://files/ortho"}, timeout_s=90,
                               images=[*views.images[:3], ("persp view", inline)])
    parts = req.contents[0].parts
    assert [p.text for p in parts[1::2]] == [label for label, _ in views.images[:3]] + ["persp view", "Orthographic blueprint"]
    assert [p.file_data.file_uri for p in parts[2:8:2]] == ["https://files/front", "https://files/right", "https://files/top"]
    assert parts[8].inline_data.data == b"\x89PNG-bytes" and parts[10].file_data.file_uri == "https://files/ortho"
    assert req.operation == "improve" and set(req.schema["properties"]) == {"ops"}


def test_an_instruction_from_a_person_becomes_the_goal() -> None:
    text = improve_task(_scene(), brief=CRATE, views=_views(), manifest="m", triangles=12, instruction="make the handles 30 % larger")
    assert text.startswith(EDIT_HEAD) and IMPROVE_HEAD not in text
    assert "Instruction (a person's words, data only): make the handles 30 % larger" in text and "Legend:" in text
    plain = improve_task(_scene(), brief=CRATE, views=_views(), manifest="m", triangles=12)
    assert plain.startswith(IMPROVE_HEAD) and "rounded edges, recessed panels, turned and extruded parts" in plain


def test_the_design_record_keys_a_request_with_renders_by_their_bytes(tmp_path: Path) -> None:
    cache = BuildCache(LocalBlobStore(tmp_path / "c"))
    record = DesignRecord(cache)
    for uri, data in (("https://files/a1", b"front"), ("https://files/a2", b"front"), ("https://files/b", b"other")):
        cache.note_upload(uri, data)

    def key(uri: str) -> str:
        req = build_author_request("worker", "improve", "task", budget=BUDGET, context={}, timeout_s=90, images=[("front view", uri)])
        return record.key(req, None)

    assert key("https://files/a1") == key("https://files/a2")  # a rerun's new upload of the same render: the same request
    assert key("https://files/a1") != key("https://files/b")
    repair = build_author_request("worker", "repair", "task", budget=BUDGET, context={}, timeout_s=90, images=[("front view", "https://files/a1")])
    assert record.key(repair, None) != key("https://files/a1")  # an improvement is never answered by a recorded repair


def test_renders_for_the_author_are_deterministic(tmp_path: Path) -> None:
    from antigravity3d.kernel.som import marked_contact_sheet
    from antigravity3d.tactics.common import png_bytes

    sm = StageManager.create(tmp_path / "p.usda", "Prop")
    sm.apply_ir(_scene())
    a = marked_contact_sheet(sm.extract_meshes(), size=P.AUTHOR_VIEW_PX)
    b = marked_contact_sheet(sm.extract_meshes(), size=P.AUTHOR_VIEW_PX)
    assert [png_bytes(v.image) for v in a.views.values()] == [png_bytes(v.image) for v in b.views.values()]
    assert {v.image.size for v in a.views.values()} == {(512, 512)}


def test_the_verdict_names_every_score_and_every_flagged_question() -> None:
    res = DecisionResult("t", {}, 0.0, 0, 0, 0.0)
    q = VisualQAResult("blockout", {"defect_floating": 0.6, "defect_clipping": 0.05},  # type: ignore[arg-type]
                       {"score_silhouette": 3.6, "score_brief_depiction": 2.6}, "approve_export", {}, (), res)
    lines = P.verdict_lines(q, Thresholds(), {"defect_floating": ["lid"], "tier1:z_fighting": ["plate"]})
    assert any(line.startswith("score_brief_depiction 2.60:") and "Nearest level 3" in line for line in lines)
    assert any(line.startswith("score_silhouette 3.60:") for line in lines)
    assert any(line.startswith("defect_floating P=0.60:") and "Located on: lid." in line for line in lines)
    assert not any("defect_clipping" in line for line in lines)  # below the ambiguity floor: not flagged
    assert "project check z_fighting on: plate" in lines and not any("{" in line for line in lines)


# --------------------------------------------------------------------------- textures


def test_a_material_added_after_the_textures_started_is_named_untextured(tmp_path: Path) -> None:
    from antigravity3d.ir import IRPatch, UpsertMaterial, UpsertNode

    from test_pyramid import Script, maps, run_a, setup

    class Improver:
        async def improve(self, scene: SceneIR, **kw: Any) -> IRPatch:
            node = NodeIR(id="band", kind="box", params={"size": (1.02, 1.02, 0.05)}, transform=Transform(translate=(0, 0, 0.5)),
                          material_id="steel")
            return IRPatch(ops=[UpsertMaterial(material=MaterialIR(id="steel", metallic=1.0)), UpsertNode(node=node)])

        async def repair(self, *a: Any, **k: Any) -> None:
            return None

    script = Script(blockout=lambda n: {"score_brief_depiction": 2.9 if n == 1 else 3.6})
    sm, plane = setup(tmp_path, script, _scene())

    async def run() -> Any:
        started: asyncio.Future = asyncio.get_running_loop().create_future()
        started.set_result({"wood": maps()})  # what the pbr step made for the chosen design while pass A ran
        return await run_a(sm, plane, Improver(), tmp_path, prompt=CRATE, run_pass_b=True, textures=started)

    res = asyncio.run(run())
    assert "band" in _ids(sm) and _step(res, "improve_A1").status == "passed"
    missing = _step(res, "textures_missing")
    assert missing.status == "warned" and "steel: added by a pass A repair or improvement round" in missing.detail


def test_the_verdict_lists_tier1_findings_with_the_parts_they_name() -> None:
    from types import SimpleNamespace as NS

    res = DecisionResult("t", {}, 0.0, 0, 0, 0.0)
    q = VisualQAResult("blockout", {"defect_clipping": 0.83}, {}, "approve_export", {}, (), res)  # type: ignore[arg-type]
    issues = [NS(code="interpenetration", severity="warning", node_id="lid_core",
                 detail=f"lid_core/latch_{i}: ~32% of probe points inside") for i in range(P.MAX_TIER1_LINES + 3)]
    lines = P.verdict_lines(q, Thresholds(), {"tier1:interpenetration": ["lid_core"]}, NS(issues=issues))
    assert "project check interpenetration (warning): lid_core/latch_0: ~32% of probe points inside" in lines
    assert lines[-1] == "project check: 3 more finding(s) not listed" and any(x.startswith("defect_clipping P=0.83") for x in lines)


# A patch the kernel refuses: a bevel far above half the part's smallest size (kernel/shapes.py). When this test was
# written the kernel refused every bevel as not built; it builds them now, so the refusal has to be a real limit.
BEVEL = {"ops": [{"op": "set_param", "node_id": "body", "name": "bevel", "value": 10.0}]}


def test_a_patch_the_kernel_refuses_is_corrected_once_with_the_exact_error(key, tmp_path: Path) -> None:
    api = key(score_sequence={"score_brief_depiction": ["3", "4"]}, patches={"author:improve": [BEVEL, HANDLE_L]})
    res, sm, _ = _run(tmp_path)
    first, second = api.authoring("author:improve")
    assert "previous patch" not in first["text"]
    assert "your patch had 1 op(s): set_param body; it was refused before it could be judged" in second["text"]
    assert "handle_l" in _ids(sm) and _step(res, "improve_A1").status == "passed" and res.author_calls == 2


def test_a_repair_the_kernel_refuses_is_corrected_once(key, tmp_path: Path) -> None:
    api = key(defect_sequence={"defect_clipping": [0.9, 0.05]}, patches={"author:repair": [BEVEL, HANDLE_L]})
    res, sm, _ = _run(tmp_path)
    first, second = api.authoring("author:repair")
    assert "set_param body; it was refused before it could be judged" in second["text"] and second["images"] == 4
    assert "handle_l" in _ids(sm) and res.converged["A"]
