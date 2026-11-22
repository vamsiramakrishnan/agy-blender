"""The look round (tactics.look_round): the finished asset rendered in colour as it ships, judged on its look, and the
author's material changes kept or undone on that judgement.

Offline: the judge and the author are ``tests/no_decisions_key.py`` (the crate is the recorded worker answer of
2026-11-22, ``live_designs``), the renders are the real three.js renders of the real package (skipped without the
browser library, as the web runtime check is)."""

from __future__ import annotations

import asyncio
import glob
import hashlib
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from antigravity3d import ir_schema as S
from antigravity3d.decisions import questions as Q
from antigravity3d.decisions.engine import DecisionControlPlane
from antigravity3d.decisions.fakes import FakeDecisionsClient
from antigravity3d.ir import MaterialIR, NodeIR, SceneIR, Transform
from antigravity3d.tactics import look_round as L

sys.path.insert(0, str(Path(__file__).parent))
from no_decisions_key import NoDecisionsKey  # noqa: E402

CRATE = "a rugged military supply crate kit-bashed from panels, corner guards, latches and two side handles"
SCENE = SceneIR(asset_name="Crate", nodes=[
    NodeIR(id="body", kind="box", params={"size": [1, 0.6, 0.5]}, transform=Transform(translate=(0, 0, 0.25)), material_id="paint"),
    NodeIR(id="latch", kind="box", params={"size": [0.05, 0.01, 0.08]}, transform=Transform(translate=(0.3, -0.305, 0.4)), material_id="steel"),
], materials=[MaterialIR(id="paint", base_color=(0.18, 0.22, 0.14), roughness=0.6, surface="olive drab paint"),
              MaterialIR(id="steel", base_color=(0.5, 0.5, 0.5), metallic=1.0, roughness=0.4),
              MaterialIR(id="decal", base_color=(0.8, 0.8, 0.8), albedo_uri="/tmp/decal.png")])


def _h(call: dict) -> str:
    return hashlib.sha256(json.dumps({"input": call["input"], "questions": call["questions"]}, sort_keys=True).encode()).hexdigest()


# --------------------------------------------------------------------------- the judge's look stage


async def test_existing_stages_ask_exactly_what_they_asked_and_the_look_stage_asks_about_surfaces_and_the_request():
    """Hashes of the same requests built by the base commit (73ad23a, before the look stage existed): blockout, lookdev and
    final, as separate views and as a sheet with blueprint and style, each with the brief."""
    c = FakeDecisionsClient()
    p = DecisionControlPlane(c)
    views = (("front view", "https://x/v1"), ("right view", "https://x/v2"))
    for stage in ("blockout", "lookdev", "final"):
        await p.evaluate_visual_qa(views, stage=stage, brief=CRATE)
        await p.evaluate_visual_qa("https://x/sheet", "https://x/o", "https://x/s", stage=stage, brief=CRATE)
    assert [_h(call) for call in c.calls] == [
        "ed37e8965e89ea151ac4cfb172936dac1c840a8a78ae8131321a40a763d82091",
        "6ca1a322cc1873292cb84f512576e1b9cb2e85a15e3b2708f24d7f1d828f0957",
        "0ad7558401de7959583a7e9be2f3a5a888dae9f901c19500de5f88b3bef67941",
        "872adf5c36cd9c3b9e4dfa969f97405109570ee999691f02a06735de25e89102",
        "ac4267b20223ed40b58b31e2a45aace139197703466e716be86d9e0dda70162a",
        "3638b51709d64833f1442fbbee4ac7ec7fb0d9cfe1f482cc6d4e5cee11db3841",
    ]
    r = await p.evaluate_visual_qa((("front right view", "https://x/v1"), ("back left view", "https://x/v2")), stage="look", brief=CRATE)
    call = c.calls[-1]
    assert _h(call) == "5f73c42ec8bffd6d45bfa9f4c5b2a5954b0497e4acfdb7c7a4348537493ad402"  # the look stage's request, pinned
    asked = set(call["questions"])
    assert "score_brief_look" in asked and {"score_pbr_realism", "score_material_separation", "defect_wrong_material"} <= asked
    geometry = {q for q, s in Q.VISUAL_QA_QUESTIONS.items() if "blockout" in s.stages}
    assert not asked & geometry  # a colour change cannot move a shape question; its noise must not decide one
    assert "Ignore shape and judge colour and material only" in call["questions"]["score_brief_look"]["instructions"]
    assert set(r.scores) == {"score_pbr_realism", "score_material_separation", "score_brief_look"}
    without, _ = Q.select(Q.VISUAL_QA_QUESTIONS, stage="look")
    assert "score_brief_look" not in without  # it needs the request, as the brief's other questions do


# --------------------------------------------------------------------------- what the author may answer


def test_a_look_answer_changes_only_the_fields_it_writes_and_only_materials():
    patch = S.apply_look(SCENE, json.dumps({"ops": [
        {"op": "upsert_material", "material": {"id": "paint", "base_color": [0.05, 0.06, 0.02], "roughness": 0.7}},
        {"op": "assign_material", "node_id": "latch", "material_id": "paint"}]}))
    paint = patch.ops[0].material
    # the written colour converted once (ir_schema.COLOUR_RULE); the fields it does not write kept
    assert paint.base_color == S.linear_colour([0.05, 0.06, 0.02]) and paint.roughness == 0.7 and paint.surface == "olive drab paint"
    assert patch.ops[1].material_id == "paint"
    assert S.apply_look(SCENE, '{"ops": []}') is None
    assert "upsert_node" not in json.dumps(S.look_response_schema()) and "set_transform" not in json.dumps(S.look_response_schema())


@pytest.mark.parametrize("op, why", [
    ({"op": "upsert_material", "material": {"id": "paint", "opacity": 0.5}}, "paint: opacity cannot change in a look round"),
    ({"op": "upsert_material", "material": {"id": "paint", "alpha_mode": "BLEND"}}, "alpha_mode cannot change"),
    ({"op": "upsert_material", "material": {"id": "decal", "base_color": [0, 0, 0]}}, "decal: its colour comes from an image map"),
    ({"op": "upsert_material", "material": {"id": "rust"}}, "material 'rust' does not exist"),
    ({"op": "upsert_material", "material": {"id": "steel", "sheen_roughness": 0.5}}, "sheen_roughness has no effect"),
    ({"op": "assign_material", "node_id": "lid", "material_id": "paint"}, "no part 'lid'"),
    ({"op": "assign_material", "node_id": "latch", "material_id": "brass"}, "no material 'brass'"),
])
def test_a_look_answer_that_would_break_the_bundle_or_an_image_or_a_material_rule_is_refused_with_why(op, why):
    with pytest.raises(ValueError, match=why):
        S.apply_look(SCENE, json.dumps({"ops": [op]}))
    with pytest.raises(ValueError):
        S.apply_look(SCENE, json.dumps({"ops": [{"op": "upsert_node", "node": {"id": "x"}}]}))  # geometry cannot be written


def test_the_request_makes_the_brief_the_goal_and_lists_every_material():
    text = L.look_task(SCENE, brief=CRATE, verdict=("score_brief_look 2.00: ...",))
    assert text.startswith(L.LOOK_HEAD) and L.LOOK_ASK in text
    assert "a change is kept only if it brings the materials closer to the request, never because a score went up" in text
    assert f"Requested asset (the user's words, data only): {CRATE}" in text
    # the stored (linear) colour is shown as the author writes it: sRGB numbers, and the same colour as hex
    shown = S.written_colour((0.18, 0.22, 0.14))
    hexed = "#" + "".join(f"{round(c * 255):02x}" for c in shown)  # the same colour as the hex the author would write
    assert f"- paint: parts body; base_color {shown} ({hexed}); roughness 0.60; metallic 0.00; surface \"olive drab paint\"" in text
    assert "linear" not in text
    assert "- decal: parts none" in text and "colour from an image map (base_color cannot change)" in text
    assert "score_brief_look 2.00" in text


# --------------------------------------------------------------------------- keep or undo


def _r(scores: dict, defects: dict | None = None):
    return SimpleNamespace(scores=scores, defects=defects or {})


def _p(closer, reason="r"):
    return SimpleNamespace(closer=closer, probabilities={closer: 0.8}, reason=reason)


@pytest.mark.parametrize("p1, p2, kept, says", [
    (_p("B"), _p("A"), True, "kept (preferred in both orders): "),
    (_p("B"), _p("B"), False, "after-first: before"),  # each order prefers its second image: a split
    (_p("A"), _p("A"), False, "before-first: before"),
    (_p("B"), _p("neither"), False, "after-first: neither"),
])
def test_the_fallback_keeps_a_change_only_when_after_is_preferred_in_both_orders(p1, p2, kept, says):
    ok, why = L.keep_by_pairs(p1, p2)
    assert ok is kept and says in why and '"r"' in why
    same = _r({"score_brief_look": 2.0})
    assert L.decide(same, same, (p1, p2), 0.35, same) == (ok, why)  # the separate scores do not decide on the fallback
    flagged = _r({"score_brief_look": 2.0}, {"defect_wrong_material": 0.9})
    # a new flag confirmed by the second after-review undoes even a preferred change ...
    assert L.decide(same, flagged, (p1, p2), 0.35, flagged) == (False, L.decide(same, flagged, (p1, p2), 0.35, flagged)[1])
    assert L.decide(same, flagged, (p1, p2), 0.35, flagged)[1].startswith("undone (new defect confirmed): defect_wrong_material=0.90/0.90")
    # ... unconfirmed (the other review does not answer yes) it decides nothing and is recorded
    kept2, why2 = L.decide(same, flagged, (p1, p2), 0.35, same)
    assert kept2 is ok and why2.startswith(why) and "unconfirmed new flag(s), decided nothing: defect_wrong_material=0.90/0.00" in why2
    # a flag the before state already had is not new
    assert L.decide(flagged, flagged, (p1, p2), 0.35, flagged) == (ok, why)


async def test_the_pairwise_request_goes_to_the_fallback_only_and_reads_its_reason(monkeypatch):
    from google import genai

    from antigravity3d.decisions.factory import make_control_plane

    views = (("front right view", "https://x/a1"), ("back left view", "https://x/a2"))
    other = (("front right view", "https://x/b1"), ("back left view", "https://x/b2"))
    with pytest.raises(NotImplementedError):
        await DecisionControlPlane(FakeDecisionsClient()).compare_states(views, other, aspect="look", brief=CRATE)
    with NoDecisionsKey(pair_prefers="neither") as api:
        client = genai.Client(api_key="dummy-not-a-key", http_options={"base_url": api.url})
        plane = (await make_control_plane(client)).preview_copy()
        v = await plane.compare_states(views, other, aspect="look", brief=CRATE)
    assert v.closer == "neither" and v.reason == "stand-in: they do not differ" and set(v.probabilities) == {"A", "B", "neither"}
    qs = Q.compare_questions("look", "Images 1 to 2", "Images 3 to 4")
    assert "Ignore shape and judge colour and material only" in qs["compare_closer"]["instructions"]
    assert qs["compare_reason"]["type"] == "text" and CRATE not in json.dumps(qs)  # fixed texts: the request is quoted input
    with pytest.raises(ValueError):
        await plane.compare_states(views, other[:1], aspect="look")


@pytest.mark.parametrize("before, after, kept, says", [
    (_r({"score_brief_look": 2.0}), _r({"score_brief_look": 3.0}), True, "rose by the band"),
    (_r({"score_brief_look": 2.0}), _r({"score_brief_look": 2.1}), False, "no change beyond the band"),
    (_r({"score_brief_look": 2.0, "score_pbr_realism": 3.5}), _r({"score_brief_look": 3.0, "score_pbr_realism": 3.0}), False, "fell by more"),
    (_r({"score_brief_look": 2.0}), _r({"score_brief_look": 3.0}, {"defect_wrong_material": 0.6}), False, "newly flagged"),
    (_r({"score_brief_look": 3.0}, {"defect_wrong_material": 0.7}), _r({"score_brief_look": 3.0}, {"defect_wrong_material": 0.1}), True, "cleared"),
])
def test_a_change_is_kept_only_on_evidence_beyond_the_band(before, after, kept, says):
    ok, why = L.keep_look_change(before, after, 0.35)
    assert ok is kept and says in why


# --------------------------------------------------------------------------- a kept state is never written twice


def test_a_package_moved_from_where_it_was_written_is_the_package_written_in_place(tmp_path):
    from antigravity3d.kernel import coplanar
    from antigravity3d.kernel.fast_ops import run_export_pipeline
    from antigravity3d.kernel.package import write_engine_package
    from antigravity3d.kernel.usd_stage import StageManager
    from antigravity3d.state import EngineBudget

    sm = StageManager.create(tmp_path / "Crate_stage.usda", "Crate")
    sm.apply_ir(SCENE.model_copy(update={"materials": SCENE.materials[:2]}))
    budget = EngineBudget("godot_4")
    bundle = run_export_pipeline(sm.extract_meshes(), budget, "Crate", opaque_materials=coplanar.opaque_materials(sm.base_ir()))
    write_engine_package(sm, bundle, budget, tmp_path / "direct", asset_name="Crate")
    write_engine_package(sm, bundle, budget, tmp_path / "elsewhere", asset_name="Crate")
    (tmp_path / "final").mkdir()
    (tmp_path / "final" / "stale.txt").write_text("from an earlier write")
    L._replace_package(tmp_path / "elsewhere", tmp_path / "final")

    def files(d):
        return {str(p.relative_to(d)): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}

    assert files(tmp_path / "final") == files(tmp_path / "direct") and not (tmp_path / "elsewhere").exists()


# --------------------------------------------------------------------------- whole runs (offline, real renders)


needs_browser = pytest.mark.skipif(L.renderer_unavailable() is not None, reason="three.js web renderer unavailable")
PAINT = {"op": "upsert_material", "material": {"id": "mat_crate_body", "base_color": [0.05, 0.06, 0.02], "roughness": 0.7}}


def _shipped_paint(usda: str) -> tuple:
    import re

    scene = SceneIR.model_validate_json(re.search(r"string sceneIR = '(.*?)'\n", usda, re.S).group(1))
    return next(m.base_color for m in scene.materials if m.id == "mat_crate_body")


def _run(tmp_path, monkeypatch, **kw):
    from antigravity3d import cli

    with NoDecisionsKey(live_designs=True, **kw) as api:
        monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", api.url)
        monkeypatch.setenv("GEMINI_API_KEY", "dummy-not-a-key")
        rc = cli.main(["prop", CRATE, "--engine", "godot_4", "--max-tris", "3000", "--out", str(tmp_path / "out"), "--preview",
                       "--token-ceiling", "400000", "--max-images", "0"])
    run = Path(glob.glob(str(tmp_path / "out" / "runs" / "*"))[0])
    steps = {s["name"]: s for s in json.loads((run / "report.json").read_text())["pyramid"]["steps"]}
    usda = (run / f"RuggedMilitarySupply_authoring.usda").read_text()
    return rc, api, steps, run, usda


@needs_browser
def test_a_change_the_judge_reads_as_closer_to_the_request_ships_baked_once(tmp_path, monkeypatch):
    """The first answer changes an opacity (refused), its correction the paint; the judge prefers the after state in both
    orders (the separate look scores do not move, as live), so the change is kept, and the package of the kept state is
    the one judged: two bakes, none in pass C."""
    bad = {"ops": [{"op": "upsert_material", "material": {"id": "mat_crate_body", "opacity": 0.5}}]}
    rc, api, steps, run, usda = _run(tmp_path, monkeypatch, patches={"author:look": [bad], "author:look_correct": [{"ops": [PAINT]}]},
                                     pair_prefers="later")
    assert rc == 0
    look = steps["look_round"]
    assert look["status"] == "passed" and look["detail"].startswith("kept mat_crate_body base_color [0.05, 0.06, 0.02]"), look
    assert "kept (preferred in both orders): before-first: after (0.90) \"stand-in: B is closer\"; after-first: after (0.90)" in look["detail"]
    assert "2 bake(s)" in look["detail"]
    assert sum(k == "judge" for k in api.kinds()) >= 5  # look review before, twice after, the two orders
    assert api.kinds().count("author:look") == 1 and api.kinds().count("author:look_correct") == 1
    assert _shipped_paint(usda) == S.linear_colour([0.05, 0.06, 0.02])  # the scene that shipped has the kept paint, converted once
    assert steps["usd_validate"]["status"] == "passed"
    assert {p.name for p in (run / "look").iterdir()} == {f"round1_{s}_{v}.png" for s in ("before", "after") for v in ("front_right", "back_left")}
    assert steps["web_runtime"]["status"] == "passed"  # pass C verified the package the look round left


@needs_browser
@pytest.mark.parametrize("prefers, says", [
    ("neither", "undone (not shown to be better): before-first: neither"),
    ("A", "undone (not shown to be better): before-first: before"),  # the judge prefers whatever it sees first: a split
    ("earlier", "undone (not shown to be better): before-first: before"),
])
def test_a_change_not_preferred_in_both_orders_is_undone_and_the_first_package_ships(tmp_path, monkeypatch, prefers, says):
    rc, api, steps, run, usda = _run(tmp_path, monkeypatch, patches={"author:look": [{"ops": [PAINT]}]}, pair_prefers=prefers)
    look = steps["look_round"]
    assert rc == 0 and look["status"] == "warned" and look["detail"].startswith("undone mat_crate_body"), look
    assert says in look["detail"] and "2 bake(s)" in look["detail"]
    assert _shipped_paint(usda) != S.linear_colour([0.05, 0.06, 0.02])
    assert not (run / "look" / "round1_package").exists()


@needs_browser
def test_a_new_defect_confirmed_by_both_after_reviews_undoes_a_preferred_change(tmp_path, monkeypatch):
    rc, api, steps, run, usda = _run(tmp_path, monkeypatch, patches={"author:look": [{"ops": [PAINT]}]}, pair_prefers="later",
                                     look_sequence={"defect_wrong_material": [0.05, 0.8]})  # before 0.05, both after reviews 0.8
    look = steps["look_round"]
    assert rc == 0 and look["status"] == "warned", look
    assert look["detail"].startswith("undone mat_crate_body") and "undone (new defect confirmed): defect_wrong_material=0.80/0.80" in look["detail"]
    assert "pairwise: before-first: after" in look["detail"]  # the preference is recorded beside the defect that overrules it
    assert _shipped_paint(usda) != S.linear_colour([0.05, 0.06, 0.02])


@needs_browser
def test_a_new_flag_one_after_review_does_not_confirm_is_recorded_and_the_preferred_change_kept(tmp_path, monkeypatch):
    """Live on 2026-11-22: one review's defect_wrong_material 0.55 would have undone a change the judge preferred 6 of 6."""
    rc, api, steps, run, usda = _run(tmp_path, monkeypatch, patches={"author:look": [{"ops": [PAINT]}]}, pair_prefers="later",
                                     look_sequence={"defect_wrong_material": [0.05, 0.55, 0.1]})  # the two after reviews disagree
    look = steps["look_round"]
    assert rc == 0 and look["status"] == "passed" and "kept (preferred in both orders)" in look["detail"], look
    assert "unconfirmed new flag(s), decided nothing: defect_wrong_material=" in look["detail"]
    assert _shipped_paint(usda) == S.linear_colour([0.05, 0.06, 0.02])


@needs_browser
def test_no_change_is_one_bake_and_a_plan_without_a_look_round_makes_no_look_call(tmp_path, monkeypatch):
    rc, api, steps, _, _ = _run(tmp_path / "a", monkeypatch)
    assert rc == 0 and steps["look_round"]["detail"].startswith("the author kept every material") and "1 bake(s)" in steps["look_round"]["detail"]
    from antigravity3d.tactics import governor

    monkeypatch.setattr(governor, "PLANS", {k: replace(v, look_rounds=0) for k, v in governor.PLANS.items()})
    rc, api, steps, _, _ = _run(tmp_path / "b", monkeypatch)
    assert rc == 0 and "look_round" not in steps and not [k for k in api.kinds() if k.startswith("author:look")]
