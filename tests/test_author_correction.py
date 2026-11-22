"""A design that does not read is corrected once, node by node, instead of being lost.

The fixture is a real failed answer (``data/live_answers/crate_flat_repeat_2026-11-22.json``, worker tier, the crate
brief): written against the first repeat design (three flat fields), it reads now as two nodes with fields the wire
does not have. Live, one answer in three was lost this way before repeat became a row-or-ring object. The correction
asks only for the failing nodes (``correct_task``) and applies the answer to the design as written.
Offline: ``tests/no_decisions_key.py`` answers the propose call with the fixture and the correction with ``corrections``.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from google import genai

from antigravity3d import ir_schema as S
from antigravity3d.state import EngineBudget
from antigravity3d.tactics.governor import Governor, plan_for
from antigravity3d.tactics.metered_author import AuthorInvalid, AuthorTruncated, MeteredAuthor, correct_task

sys.path.insert(0, str(Path(__file__).parent))
from no_decisions_key import NoDecisionsKey  # noqa: E402

DATA = Path(__file__).parent / "data" / "live_answers"
FAILED = (DATA / "crate_flat_repeat_2026-11-22.json").read_text()
ASSET = "RuggedMilitarySupply"
PROMPT = "a rugged military supply crate kit-bashed from panels, corner guards, latches and two side handles"


def fixed(node_id: str, **repeat) -> dict:
    node = next(n for n in json.loads(FAILED)["nodes"] if n["id"] == node_id)
    node = {k: v for k, v in node.items() if not k.startswith("repeat_")}
    return {**node, "repeat": repeat}


RIB = fixed("rib_front", count=2, offset=[0.36, 0, 0])
BOLT = fixed("bolt_lid", count=4, offset=[0.2, 0, 0])


def author(api: NoDecisionsKey, tmp_path: Path) -> MeteredAuthor:
    client = genai.Client(api_key="dummy-not-a-key", http_options={"base_url": api.url})
    gov = Governor(replace(plan_for(1), geometry_candidates=1), token_ceiling=500_000, journal=tmp_path / "spend.sqlite3")
    return MeteredAuthor(client, gov, EngineBudget("godot_4", max_tris=3000), {"asset_name": ASSET}, timeout_s=30)


async def propose(api: NoDecisionsKey, tmp_path: Path):
    a = author(api, tmp_path)
    return a, await a.propose_with_outcomes(PROMPT, budget=a.budget, n=1, context={"asset_name": ASSET})


def test_the_correction_request_carries_only_the_failing_nodes_and_the_exact_errors():
    try:
        S.parse_scene(FAILED)
    except ValueError as e:
        err = e
    failed = json.loads(FAILED)
    text = correct_task(PROMPT, index=0, n=1, failed=failed, error=err)
    assert "nodes.7.box.repeat_count: Extra inputs are not permitted (got 2)" in text
    assert '"id":"rib_front"' in text and '"id":"bolt_lid"' in text
    assert '"id":"crate_body"' not in text and '"crate_body"' in text  # named in the id list, not restated
    assert len(text) < len(FAILED) / 2


async def test_a_failed_answer_is_rescued_by_one_correction(tmp_path):
    with NoDecisionsKey(propose_answers=[FAILED], corrections=[{"nodes": [RIB, BOLT], "materials": [], "remove": []}]) as api:
        a, res = await propose(api, tmp_path)
    assert api.kinds().count("author:propose") == 1 and api.kinds().count("author:correct") == 1
    scene = res.scenes[0]
    ids = [n.id for n in scene.nodes]
    assert {"rib_front", "rib_front__r1", "bolt_lid__r3", "bolt_lid__r3__my", "crate_body"} <= set(ids)
    by_hand = json.loads(FAILED)
    by_hand["nodes"] = [RIB if n["id"] == "rib_front" else BOLT if n["id"] == "bolt_lid" else n for n in by_hand["nodes"]]
    assert scene == S.parse_scene(json.dumps(by_hand))  # every other node exactly as written
    out = res.outcomes[0]
    assert out.status == "produced" and out.correction.startswith("rescued: ")
    assert "Extra inputs are not permitted" in out.correction
    assert out.usage["output_tokens"] == (1140 + 1066) + (400 + 900)  # both calls are on the candidate


async def test_a_correction_that_still_fails_keeps_both_messages(tmp_path):
    bad = {**RIB, "repeat": {"count": 2, "offset": [0.36, 0, 0], "around": "z"}}  # the original mistake, nested
    with NoDecisionsKey(propose_answers=[FAILED], corrections=[{"nodes": [bad, BOLT], "materials": [], "remove": []}]) as api:
        with pytest.raises(AuthorInvalid) as info:
            await propose(api, tmp_path)
    msg = str(info.value)
    assert msg.count("does not validate") == 2 and "correction:" in msg
    assert api.kinds().count("author:correct") == 1  # one correction per candidate, never a second


async def test_an_answer_that_is_not_json_or_was_cut_off_is_not_corrected(tmp_path):
    with NoDecisionsKey(propose_answers=["{\"nodes\": [{\"id\""]) as api:
        with pytest.raises(AuthorInvalid):
            await propose(api, tmp_path)
    assert "author:correct" not in api.kinds()
    with NoDecisionsKey(fail=[], propose_answers=[FAILED]) as api:  # sanity: the same fixture is corrected
        with pytest.raises(AuthorInvalid):  # an empty correction changes nothing, so it fails again
            await propose(api, tmp_path / "b")
    assert api.kinds().count("author:correct") == 1
    assert issubclass(AuthorTruncated, Exception) and not issubclass(AuthorTruncated, AuthorInvalid)  # never corrected


def test_a_correction_can_remove_and_add_and_materials_are_replaced_by_id():
    failed = json.loads(FAILED)
    fix = {"nodes": [RIB, BOLT, {"id": "extra_bolt", "kind": "cylinder", "params": {"radius": 0.005, "height": 0.004},
                                 "transform": {"translate": [0, 0.26, 0.2]}}],
           "materials": [{"id": "mat_crate_body", "base_color": [0.2, 0.25, 0.1], "roughness": 0.7}],
           "remove": ["skid_front"]}
    scene = S.apply_correction(failed, json.dumps(fix))
    ids = {n.id for n in scene.nodes}
    assert "skid_front" not in ids and "skid_front__my" not in ids and "extra_bolt" in ids
    assert next(m for m in scene.materials if m.id == "mat_crate_body").base_color == (0.2, 0.25, 0.1)


async def test_a_kernel_refusal_is_corrected_like_a_parse_error(tmp_path, monkeypatch):
    """The kernel's per-node check runs inside the parse (``kernel_checked``), so a design it would refuse is corrected
    node by node. The check here stands in for ``compile_ir.check_node`` with the rule shapes states for a box: a bevel
    must stay below half the smallest extent."""
    from antigravity3d.kernel import compile_ir

    def check_node(node):
        if node.kind == "box" and node.params.get("bevel", 0) >= min(node.params["size"]) / 2:
            raise ValueError(f"{node.id}: bevel {node.params['bevel']} must be below half the smallest size")

    monkeypatch.setattr(compile_ir, "check_node", check_node, raising=False)
    design = json.loads((DATA / "crate_p1_2026-11-22.json").read_text())
    skid = next(n for n in design["nodes"] if n["id"] == "skid_r")
    skid["params"]["bevel"] = 0.02  # the skid is 2.5 cm tall
    fix = {**skid, "params": {**skid["params"], "bevel": 0.004}}
    with NoDecisionsKey(propose_answers=[json.dumps(design)], corrections=[{"nodes": [fix], "materials": [], "remove": []}]) as api:
        a, res = await propose(api, tmp_path)
    assert api.kinds().count("author:correct") == 1
    assert res.outcomes[0].correction.startswith("rescued: skid_r: bevel 0.02 must be below half the smallest size")
    assert all(n.params.get("bevel") == 0.004 for n in res.scenes[0].nodes if n.id.startswith("skid_r"))
