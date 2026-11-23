"""Live-shaped runs on a key WITHOUT the native Decisions API, offline against ``tests/no_decisions_key.py``.

What is pinned here:
* ``agy3d prop --preview --token-ceiling N --max-images M`` builds a package labelled unverified (manifest, run report,
  last lines), and its ledger never passes either limit, also in runs built to hit each one;
* every refusal comes at start, before any generation call, with the numbers and what to run instead;
* the same run with ``--require-verified`` (or without ``--preview``) refuses exactly as before, and nothing about the
  verified path loosens when a preview exists (gates, QA records, the plugin hook, studio policy files);
* a native session: opened as a preview it exports a labelled package, switched in place it does too, and a session that
  is not a preview says at open what will be refused and cannot export;
* the money path writes exactly the keys it wrote before token ledgers existed.

No test here reads or sets a price variable, and no test calls a live API.
"""

from __future__ import annotations

import asyncio
import glob
import json
import re
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from antigravity3d import cli
from antigravity3d.tactics.governor import BudgetExceeded, Governor, plan_for

sys.path.insert(0, str(Path(__file__).parent))
from no_decisions_key import NoDecisionsKey  # noqa: E402

FAST = ["--art-direction", "retro-lowres"]  # 128 px maps: the same pipeline, faster texture derivation


@pytest.fixture
def key(monkeypatch):
    """The stand-in, with the SDK pointed at it and a dummy key (it never leaves this machine)."""
    def start(**kw):
        api = NoDecisionsKey(**kw).__enter__()
        monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", api.url)
        monkeypatch.setenv("GEMINI_API_KEY", "dummy-not-a-key")
        monkeypatch.delenv("AGY3D_THREE_JS", raising=False)  # the browser check is not what is tested here
        started.append(api)
        return api

    started: list[NoDecisionsKey] = []
    yield start
    for api in started:
        api.__exit__(None, None, None)


def prop(tmp_path, *args):
    return cli.main(["prop", "wooden crate with iron brackets", "--out", str(tmp_path / "out"), *args])


def report(tmp_path) -> dict:
    data = json.loads((tmp_path / "out" / "report.json").read_text())
    return data.get("extras", data)


def manifest(tmp_path) -> dict:
    return json.loads(Path(glob.glob(str(tmp_path / "out" / "runs" / "*" / "package" / "manifest.json"))[0]).read_text())


def usd_numbers(o, path="") -> list[str]:
    """Every *_usd key holding a number (a token ledger must have none)."""
    out = []
    if isinstance(o, dict):
        for k, v in o.items():
            if k.endswith("_usd") and isinstance(v, (int, float)) and not isinstance(v, bool):
                out.append(f"{path}/{k}={v}")
            out += usd_numbers(v, f"{path}/{k}")
    elif isinstance(o, list):
        for i, v in enumerate(o):
            out += usd_numbers(v, f"{path}[{i}]")
    return out


# ---------------------------------------------------------------------------------------------- prop: preview runs


def test_prop_preview_builds_a_labelled_unverified_package_within_both_limits(key, tmp_path, capsys):
    api = key()
    rc = prop(tmp_path, "--preview", "--token-ceiling", "150000", "--max-images", "4", "--single-candidate")
    out = capsys.readouterr().out
    assert rc == 0, out
    lines = out.strip().splitlines()
    # the line above the last says what the exit code means under an uncalibrated judge (antigravity3d.delivery): one more
    # line than before round five's exit rule, so the others moved up by one
    assert lines[-5].startswith("publication: UNVERIFIED PREVIEW (verified=false; judged by generative fallback, uncalibrated)")
    assert lines[-3].startswith("review: required") and lines[-1] == "preview gates: PASS"
    assert lines[-2].startswith("delivered: yes, every deterministic check passed (exit 0")
    assert "spend (tokens, no price applied):" in out and "$" not in out.split("spend (tokens")[1].splitlines()[0]
    m = manifest(tmp_path)
    assert m["verified"] is False and m["preview"] is True and m["needs_review"] is True
    assert m["judge"]["label"] == "generative fallback, uncalibrated" and m["judge"]["calibrated"] is False
    assert m["spend"]["unit"] == "tokens" and "no price applied" in m["spend"]["summary"]
    x = report(tmp_path)
    assert x["preview"]["verified"] is False and x["needs_review"] is True and x["verified"] is False and not x["published"]
    c = x["cost"]
    assert c["spend_unit"] == "tokens" and c["price_applied"] is False and "governor_usd" not in c
    assert 0 < c["governor_tokens"] <= 150_000 and c["images"] <= 4
    assert usd_numbers(x) == []
    for r in c["receipts"]:
        assert r["unit"] == "tokens" and r["price_applied"] is False and not [k for k in r if k.endswith("_usd")]
        assert r["booked_tokens"] <= r["reserved_tokens"] or r["source"] == "provider_usage"
    # the judge ran on the fallback with its output capped, through the ledger: route, ranking, QA, LOD pop
    cats = {r["category"] for r in c["receipts"]}
    assert {"route", "visual_qa", "author.propose", "lod_pop"} <= cats
    assert all(r["max_output_tokens"] == 8192 for r in api.requests if r["kind"] == "judge")


def test_albedo_prompts_are_built_from_surface_words_never_from_the_brief(key, tmp_path, capsys):
    """The scene comes back from the authoring call (the stand-in answers with the template scene, surface words
    included); every albedo prompt the API then receives names that surface, never the crate or its brackets."""
    api = key()
    rc = prop(tmp_path, "--preview", "--token-ceiling", "150000", "--max-images", "4", "--single-candidate", *FAST)
    assert rc == 0, capsys.readouterr().out
    albedo = [p for p in api.image_prompts() if p.startswith("Seamless tileable albedo")]
    subjects = sorted(p.split("texture of: ", 1)[1].split(". ", 1)[0] for p in albedo)
    assert subjects == ["dark wrought iron, lightly rusted", "weathered oak wood with open grain, matte"], albedo
    assert not [p for p in albedo if re.search(r"crate|bracket", p, re.I)], albedo
    assert all("low-resolution retro game texture" in p for p in albedo)  # the art direction's texture brief stays


def test_prop_without_preview_refuses_at_start_and_generates_nothing(key, tmp_path, capsys, any_target_can_be_verified):
    # about the key (no calibrated judge); the default engine's own refusal comes first otherwise (tests/test_verifiable_targets.py)
    api = key()
    for extra in ([], ["--require-verified", "--token-ceiling", "150000", "--max-images", "4"]):
        assert prop(tmp_path, *extra) == 2
        err = capsys.readouterr().err
        assert "native Decisions API is not available on this key" in err and "a verified run needs it" in err
        assert "--preview --token-ceiling N --max-images M" in err
    assert api.generated() == 0 and set(api.kinds()) <= {"404"}


@pytest.mark.parametrize("args, says", [
    (["--preview"], "pass your own --token-ceiling N and --max-images M"),
    (["--preview", "--token-ceiling", "150000"], "--token-ceiling needs --max-images"),
    (["--preview", "--token-ceiling", "20000", "--max-images", "4"], "the smallest ceiling that can admit them is"),
])
def test_prop_preview_refusals_come_before_any_generation(key, tmp_path, capsys, args, says):
    api = key()
    assert prop(tmp_path, *args) == 2
    err = capsys.readouterr().err
    assert says in err and "nothing was generated" in err or "Pass --max-images M" in err
    assert api.generated() == 0


def test_a_run_built_to_hit_the_token_ceiling_is_refused_before_the_call_that_would_pass_it(key, tmp_path, capsys):
    api = key()
    from antigravity3d.decisions.engine import DecisionControlPlane
    from antigravity3d.decisions.fallback import GenerativeDecisionsBackend
    from antigravity3d.tactics.token_plan import judge_worst_tokens

    # the start-up check's smallest admissible ceiling (the largest judge call bounds it on this key)
    minimum = judge_worst_tokens(DecisionControlPlane(GenerativeDecisionsBackend(object()), backend_kind="generative_fallback"))
    # Since the start-up bound covers the geometry ranking too (61,800 with a brief, from 49,186), a single-candidate run
    # fits under minimum + 5,000; a run with the plan's candidates is the one that reaches it.
    rc = prop(tmp_path, "--preview", "--token-ceiling", str(minimum + 5_000), "--max-images", "8", *FAST)
    out = capsys.readouterr().out
    x = report(tmp_path)
    assert x["cost"]["governor_tokens"] <= minimum + 5_000
    # the call that would pass the ceiling was refused before dispatch, with its numbers, and the run failed closed
    assert rc == 1 and "BudgetExceeded: " in out and " tokens available" in out, out
    assert all(r["booked_tokens"] is not None for r in x["cost"]["receipts"]) and api.generated() > 0


def test_a_run_built_to_hit_the_image_limit_refuses_before_any_image(key, tmp_path, capsys):
    api = key()
    rc = prop(tmp_path, "--preview", "--token-ceiling", "150000", "--max-images", "1", *FAST)
    err = capsys.readouterr().err
    assert rc == 2
    assert "needs at least 4 image(s)" in err and "--single-candidate" in err and "only the routing call was made" in err
    assert not [k for k in api.kinds() if k == "image:sent"]


def test_a_run_allowed_no_image_delivers_a_package_and_generates_none(key, tmp_path, capsys):
    """--max-images 0 is a request, not a shortfall. Until 2026-11-22 it was refused like any other shortfall, so no
    run could be made at all once an image allowance was spent, while a stock agent with Blender delivered the same
    brief without one. The blueprint is skipped, no material is textured from an image, everything else runs."""
    api = key()
    rc = prop(tmp_path, "--preview", "--token-ceiling", "150000", "--max-images", "0", *FAST)
    io = capsys.readouterr()
    assert rc == 0, io.out + io.err
    assert not [k for k in api.kinds() if k.startswith("image")], api.kinds()
    x = report(tmp_path)
    assert x["cost"]["images"] == 0 and not any(r.get("images") for r in x["cost"]["receipts"])
    m = manifest(tmp_path)
    assert m["validation"]["package_ok"] and m["preview"] is True
    assert "none allowed (--max-images 0)" in io.out + io.err  # the developer is told why the asset has no image textures


def test_the_image_limit_caps_candidates_and_is_never_passed(key, tmp_path, capsys):
    api = key()
    prop(tmp_path, "--preview", "--token-ceiling", "150000", "--max-images", "2", "--single-candidate", *FAST)
    capsys.readouterr()
    x = report(tmp_path)
    assert x["cost"]["images"] <= 2 and len([k for k in api.kinds() if k == "image:sent"]) <= 2
    assert sum(r.get("images", 0) for r in x["cost"]["receipts"]) <= 2


@pytest.mark.parametrize("kw, why", [
    ({"policy": 0.9}, "hard policy flag"),
    ({"overrides": {"defect_ip_or_text": 0.95}}, "escalation to a person"),
])
def test_a_hard_policy_flag_or_an_escalation_to_a_person_fails_a_preview_even_from_an_uncalibrated_judge(key, tmp_path, capsys, kw, why):
    """Content risk, not a quality score: these keep exit 1 under the delivery rule (antigravity3d.delivery). Loosening
    them is the owner's decision."""
    key(**kw)
    rc = prop(tmp_path, "--preview", "--token-ceiling", "150000", "--max-images", "4", "--single-candidate", *FAST)
    out = capsys.readouterr().out
    assert rc == 1, why
    assert "preview gates: FAIL" in out and "blocked:" in out


def test_a_score_gate_an_uncalibrated_judge_fails_is_a_finding_and_exits_0_or_3_with_strict(key, tmp_path, capsys):
    """Until round five a score below target from the uncalibrated judge failed the run (exit 1): live on 2026-11-22 a
    crate that validated clean and imported into Godot exited 1 on silhouette 2.74 < 3.3, and no first design of 27
    reached 3.3. Now the delivered package exits 0, the last lines name each target missed and by how much, the gates
    stay as computed (preview gates: FAIL, never read as approval), and --strict exits 3."""
    key(score_level="1")
    args = ("--preview", "--token-ceiling", "150000", "--max-images", "4", "--single-candidate", *FAST)
    assert prop(tmp_path, *args) == 0
    cap = capsys.readouterr()
    lines = cap.out.strip().splitlines()
    # progress goes to stderr only: the author's streamed call and the judge's calls; stdout stays the report
    assert "[author worker propose #1]" in cap.err and "[judge visual_qa:blockout]" in cap.err
    assert "[author " not in cap.out and "[judge " not in cap.out
    assert lines[-1] == "preview gates: FAIL" and lines[-2].startswith("delivered: yes")
    assert any(ln.startswith("finding: ") and "below the target" in ln and "look at it before you ship" in ln for ln in lines)
    x = report(tmp_path)
    assert x["preview_gates_ok"] is False and x["gates_ok"] is False  # unchanged: nothing may read the exit code as approval
    assert x["delivered"]["ok"] is True and x["exit"] == {"code": 0, "rule": x["exit"]["rule"], "strict": False}
    assert x["review"]["status"] == "below_target" and x["review"]["calibrated"] is False
    assert any(not t["met"] and t["short_by"] > 0 for t in x["review"]["targets"])
    other = tmp_path / "strict"
    other.mkdir()
    assert prop(other, *args, "--strict") == 3
    assert "--strict makes that exit 3" in capsys.readouterr().out


# ---------------------------------------------------------------------------------------------- kit


def kit(tmp_path, *args):
    return cli.main(["kit", "ruined stone temple", "--pieces", "2", "--out", str(tmp_path / "kit"), *args])


def test_kit_draws_every_piece_and_the_sheet_from_one_token_pool(key, tmp_path, capsys):
    api = key()
    assert kit(tmp_path, "--token-ceiling", "150000", "--max-images", "1") == 0
    out = capsys.readouterr().out
    assert "spend (tokens, no price applied):" in out and "of 150,000 tokens" in out and "1 of 1 images" in out
    data = json.loads((tmp_path / "kit" / "report.json").read_text())
    x = data.get("extras", data)
    sp = x["spend"]
    assert sp["unit"] == "tokens" and sp["ceiling_tokens"] == 150_000 and sp["spent_tokens"] <= 150_000 and sp["images"] <= 1
    assert usd_numbers(x) == [] and len([k for k in api.kinds() if k == "image:sent"]) == 1


@pytest.mark.parametrize("args, says", [
    ([], "AGY3D_TEXT_RATES_JSON"),
    (["--token-ceiling", "150000"], "pass --max-images"),
    (["--token-ceiling", "20000", "--max-images", "1"], "that is the smallest ceiling that can start this kit"),
])
def test_kit_refusals_come_before_any_generation(key, tmp_path, capsys, monkeypatch, args, says):
    monkeypatch.delenv("AGY3D_TEXT_RATES_JSON", raising=False)  # asserting the money path's refusal
    api = key()
    assert kit(tmp_path, *args) == 2
    assert says in capsys.readouterr().err
    assert api.generated() == 0


# ---------------------------------------------------------------------------------------------- native session


async def native_session(api, tmp_path, mode: str) -> tuple[list[str], str]:
    from antigravity3d.harness import tools as T
    from antigravity3d.runtime.mcp_server import StudioHost
    from antigravity3d.state import EngineBudget
    from antigravity3d.tactics.authoring import TemplateAuthor

    h = StudioHost()
    kw = dict(preview=True, token_ceiling=150_000, max_images=4) if mode == "open" else {}
    lines = [await h.open_asset_session("crate", "godot_4", workdir=str(tmp_path / "s"), **kw)]
    sid = h.active
    scene = (await TemplateAuthor().propose("wooden crate", budget=EngineBudget("godot_4"), n=1, context={"asset_name": "crate"}))[0]
    ops = [{"op": "upsert_material", "material": m.model_dump(mode="json", exclude_defaults=True)} for m in scene.materials]
    ops += [{"op": "upsert_node", "node": n.model_dump(mode="json", exclude_defaults=True)} for n in scene.nodes]

    async def call(name, **a):
        r = await h.call_tool(name, T.ALL_TOOLS[name], sid, a)
        lines.append(f"{name}: {r}")
        return r

    await call("apply_scene_ir", ops=ops)
    await call("render_preview")
    await call("run_visual_qa")
    if mode == "switch":
        lines.append(await h.start_preview(150_000, 4))
    h._set_stage(sid, "LOOKDEV")
    await call("forge_pbr_material", material_id=scene.materials[0].id, prompt="weathered oak planks")
    await call("render_lookdev")
    await call("run_visual_qa")
    h._set_stage(sid, "EXPORT")
    await call("generate_lods_and_colliders")
    await call("run_usd_validator")
    await call("export_engine_package", out_name="crate")
    return lines, str(tmp_path / "s")


@pytest.mark.parametrize("mode", ["open", "switch"])
def test_native_preview_session_exports_a_labelled_package(key, tmp_path, mode):
    api = key()
    lines, wd = asyncio.run(native_session(api, tmp_path, mode))
    export = lines[-1]
    assert export.startswith("export_engine_package: EXPORTED PREVIEW crate (UNVERIFIED; judged by generative fallback, uncalibrated)")
    assert "of 150,000 tokens (no price applied)" in export and "of 4 images" in export
    # delivered and review said apart: the package's checks, then what the uncalibrated judge said
    assert "| delivered: yes, package validation passed" in export
    assert "| review: approve by generative fallback, uncalibrated at tier15 " in export and "a person must look at it" in export
    m = json.loads(Path(glob.glob(wd + "/export/crate/manifest.json")[0]).read_text())
    assert m["verified"] is False and m["preview"] is True and m["judge"]["calibrated"] is False
    assert m["validation"]["engine_import"]["ok"] in (None, True)  # skipped without Godot, passed with it; never parity
    assert m["validation"]["blender_asset_qa"]["status"] == "skipped"  # the Blender check is asked for in a preview too (no worker here)
    if mode == "switch":
        assert "WARNING: the native Decisions API is not available" in lines[0] and "start_preview" in lines[0]
        assert "visual QA refused" in next(l for l in lines if l.startswith("run_visual_qa"))
        assert "earlier verdicts discarded" in next(l for l in lines if l.startswith("SESSION") and l is not lines[0])


def test_native_session_not_opened_for_preview_cannot_export(key, tmp_path):
    from antigravity3d.harness import tools as T
    from antigravity3d.runtime.mcp_server import StudioHost

    key()

    async def go():
        h = StudioHost()
        line = await h.open_asset_session("crate", "godot_4", workdir=str(tmp_path / "s"))
        sid = h.active
        h._set_stage(sid, "EXPORT")
        r = await h.call_tool("export_engine_package", T.ALL_TOOLS["export_engine_package"], sid, {"out_name": "x"})
        return line, r

    line, refused = asyncio.run(go())
    assert "WARNING" in line and refused.startswith("REFUSED export blocked")
    assert not glob.glob(str(tmp_path / "s" / "export" / "x"))


def test_a_session_only_the_user_can_unblock_refuses_delivery_at_once_and_says_what_to_ask(key, tmp_path):
    """Live 2026-11-22 (r5/live_rerun_N, a change request on a reopened asset, not a preview): deliver_package ran its
    checks and a lookdev render for 58 s before "export blocked: needs tier15>=3.4 ... Fix what it names with
    apply_scene_ir". Nothing it checks and nothing the agent changes can end in a delivery there: without the
    calibrated judge only a preview delivers, and a preview is the user's decision. It now says so before any work, the
    QA record carries it for the hooks, and the switch to a preview clears it."""
    import time

    from antigravity3d.harness import tools as T
    from antigravity3d.harness.session import NO_JUDGE, session
    from antigravity3d.runtime import qa_state
    from antigravity3d.runtime.mcp_server import StudioHost
    from antigravity3d.state import EngineBudget
    from antigravity3d.tactics.authoring import TemplateAuthor

    api = key()

    async def go():
        h = StudioHost()
        await h.open_asset_session("crate", "godot_4", workdir=str(tmp_path / "s"), workspace=str(tmp_path), conversation_id="conv-1")
        sid = h.active
        scene = (await TemplateAuthor().propose("wooden crate", budget=EngineBudget("godot_4"), n=1, context={"asset_name": "crate"}))[0]
        ops = [{"op": "upsert_material", "material": m.model_dump(mode="json", exclude_defaults=True)} for m in scene.materials]
        ops += [{"op": "upsert_node", "node": n.model_dump(mode="json", exclude_defaults=True)} for n in scene.nodes]
        await h.call_tool("apply_scene_ir", T.ALL_TOOLS["apply_scene_ir"], sid, {"ops": ops})
        asked = api.generated()
        t0 = time.monotonic()
        reply = await h.call_tool("deliver_package", T.ALL_TOOLS["deliver_package"], sid, {"dest": "output"})
        took = time.monotonic() - t0
        waits = qa_state.read_qa_state(sid, tmp_path).waits_for_user
        await h.start_preview(150_000, 0)
        return str(reply), took, api.generated() - asked, waits, qa_state.read_qa_state(sid, tmp_path).waits_for_user, \
            session(h.registry, sid).waits_for_user()

    reply, took, calls, waits, after, live_after = asyncio.run(go())
    assert reply.startswith(f"NOT DELIVERED: a person must decide ({NO_JUDGE}). Reply to the user with this and stop: ask the user "
                            "whether they want an unverified preview and for their two limits")
    assert "start_preview" in reply and "\n" not in reply  # one sentence: no check ran, nothing was rendered
    assert took < 2.0 and calls == 0 and not glob.glob(str(tmp_path / "output" / "*"))
    assert waits == NO_JUDGE and after == "" and live_after == ""


def test_preview_needs_both_user_limits():
    from antigravity3d.harness.session import check_preview_limits

    for tokens, images in ((None, 4), (150_000, None), (None, None), (0, 4), (150_000, -1)):
        with pytest.raises(ValueError):
            check_preview_limits(tokens, images)


def test_plugin_hook_asks_the_user_with_both_numbers_before_a_preview_opens():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin" / "antigravity-3d" / "hooks" / "scripts"))
    try:
        import pre_tool_gate as g
    finally:
        sys.path.pop(0)
    ask = g.preview_decision({"token_ceiling": 150000, "max_images": 4})
    assert ask["decision"] == "ask" and "150,000 tokens" in ask["reason"] and "4 generated image" in ask["reason"]
    assert "UNVERIFIED" in ask["reason"]
    for bad in ({}, {"token_ceiling": 150000}, {"max_images": 3}, {"token_ceiling": "lots", "max_images": 2}):
        assert g.preview_decision(bad)["decision"] == "deny"


# ---------------------------------------------------------------------------------------------- verified path unchanged


def test_a_preview_plane_cannot_enter_a_verified_run():
    from antigravity3d.dags.prop_to_engine import PropToEngineDAG
    from antigravity3d.state import EngineBudget
    from antigravity3d.tactics.authoring import TemplateAuthor

    plane = cli.offline_plane().preview_copy()
    with pytest.raises(ValueError, match="verified run cannot use a preview"):
        PropToEngineDAG(plane=plane, forge=None, author=TemplateAuthor(), workdir="/nonexistent", budget=EngineBudget("unreal_5"),
                        require_verified=True)


def test_uncalibrated_approval_never_opens_the_verified_export_gate():
    from antigravity3d.runtime.qa_state import QAState
    from antigravity3d.state import AssetSessionState, EngineBudget, VisualVerdict

    st = AssetSessionState("s", Path("x.usda"), EngineBudget("godot_4"), last_tier1_ok=True, last_tier15_score=3.8)
    for backend, renderer in (("uncalibrated", "eevee"), ("generative_fallback", "eevee"), ("decisions", "clay")):
        st.verdict = VisualVerdict(st.revision, "godot_4", "lookdev", renderer, backend, "approve")
        assert not st.export_allowed
        assert not QAState(True, 3.8, revision=st.revision, target="godot_4", verdict=st.verdict).export_allowed
        st.preview = True  # the same verdict opens only the preview gate
        assert st.export_allowed
        st.preview = False
    st.verdict = VisualVerdict(st.revision, "godot_4", "lookdev", "eevee", "decisions", "approve")
    assert st.export_allowed  # the verified gate itself is unchanged


def test_plugin_hook_gate_keeps_the_verified_rule_and_reads_preview_only_from_the_record(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin" / "antigravity-3d" / "hooks" / "scripts"))
    try:
        import _common as c
    finally:
        sys.path.pop(0)
    verdict = {"revision": "r", "target": "godot_4", "stage": "lookdev", "renderer": "clay", "backend": "uncalibrated", "action": "approve"}
    rec = {"schema": c.QA_SCHEMA, "conversation_id": "c", "session_id": "s", "revision": "r", "target": "godot_4",
           "tier1_ok": True, "tier15_score": 3.8, "verdict": verdict, "validator_ok": True, "ts": 1.0}
    (tmp_path / "qa").mkdir()
    for preview, ok in ((None, False), (False, False), ("yes", False), (True, True)):
        body = dict(rec) if preview is None else {**rec, "preview": preview}
        (tmp_path / "qa" / "s.json").write_text(json.dumps(body))
        assert c.export_gate(tmp_path, "c", "s")[0] is ok


async def test_trust_uncalibrated_in_a_policy_file_does_not_let_the_fallback_judge_a_governed_run(tmp_path):
    from antigravity3d.decisions import questions as Q
    from antigravity3d.decisions.compose import Thresholds
    from antigravity3d.decisions.engine import DecisionControlPlane
    from antigravity3d.decisions.fakes import FakeDecisionsClient
    from antigravity3d.decisions.models import DecisionBudgetError

    (tmp_path / "studio_policy.yaml").write_text("thresholds:\n  trust_uncalibrated: true\n")
    assert Thresholds.load(tmp_path / "studio_policy.yaml").trust_uncalibrated
    plane = DecisionControlPlane(FakeDecisionsClient(), backend_kind="generative_fallback")
    for gov in (Governor(plan_for(1)), Governor(plan_for(1), token_ceiling=10**6, max_images=1)):
        with pytest.raises(DecisionBudgetError, match="only a preview can be judged"):
            await plane.evaluate("x", {"a": Q.noul("q")}, tag="t", governor=gov)


# ---------------------------------------------------------------------------------------------- the money path


def keypaths(o, pre=""):
    out = set()
    if isinstance(o, dict):
        for k, v in o.items():
            out.add(f"{pre}/{k}")
            out |= keypaths(v, f"{pre}/{k}")
    elif isinstance(o, list):
        for v in o:
            out |= keypaths(v, pre + "[]")
    return out


async def test_the_money_path_writes_exactly_the_keys_it_wrote_before(tmp_path, monkeypatch):
    from antigravity3d.dags.prop_to_engine import PropToEngineDAG
    from antigravity3d.genmedia.nano_banana import FakeImageBackend, ImageForge
    from antigravity3d.state import EngineBudget
    from antigravity3d.tactics.authoring import TemplateAuthor

    monkeypatch.delenv("AGY3D_THREE_JS", raising=False)
    rep = await PropToEngineDAG(plane=cli.offline_plane(), forge=ImageForge(FakeImageBackend()), author=TemplateAuthor(),
                                workdir=tmp_path, budget=EngineBudget("unreal_5")).run("wooden crate")
    x = json.loads(json.dumps(rep.extras, default=str))
    keep = {k: v for k, v in x.items() if k not in ("outputs", "report_path", "workdir")}
    before = json.loads((Path(__file__).parent / "money_path_keys.json").read_text())
    machine = ("/pyramid/artifacts/", "/skipped[]", "/pyramid/skipped[]", "/scores/web_", "/pyramid/scores/web_")  # installed engines/browsers
    # /budget/max_texture_px, /budget/shared_atlas and /budget/base_color_jpeg_quality were added on purpose (EngineBudget: one shared atlas per prop, capped
    # by an art direction's texture size); the report writes every budget field
    # /budget/generated_surfaces likewise (surfaces generated from the geometry for a material without an image)
    # Added on purpose in round five (antigravity3d.delivery): what the exit code said, beside the unchanged gates. On the
    # money path (a calibrated judge) the gates decide, as before.
    delivery = ("/delivered", "/review", "/exit")
    assert sorted(k for k in keypaths(keep) if not k.startswith(machine + delivery)) == before["extras"]
    assert x["exit"] == {"code": 0 if x["preview_gates_ok"] else 1, "rule": "gates of a calibrated judge"}
    assert x["review"]["calibrated"] is True and x["delivered"]["ok"] in (True, False)
    assert {n: sorted(d.data) for n, d in rep.nodes.items()} == before["node_data"]
    assert x["cost"]["plan"].startswith("standard: $") and "tokens" not in x["cost"]["plan"]
    assert all("reserved_usd" in r and "unit" not in r for r in x["cost"]["receipts"])


# ---------------------------------------------------------------------------------------------- the ledger


def test_a_journal_keeps_its_unit(tmp_path):
    j = tmp_path / "spend.sqlite3"
    Governor(plan_for(1), journal=j).charge("x", 0.01)
    with pytest.raises(ValueError, match="kept in usd"):
        Governor(plan_for(1), journal=j, token_ceiling=1000)
    t = tmp_path / "tokens.sqlite3"
    Governor(plan_for(1), journal=t, token_ceiling=1000)
    with pytest.raises(ValueError, match="kept in tokens"):
        Governor(plan_for(1), journal=t)


def test_token_receipts_name_their_unit_and_images_are_a_hard_count():
    g = Governor(plan_for(1), token_ceiling=10_000, max_images=2)
    a = g.reserve("image.albedo", 3_000, receipt={"booked_usd": 0.0}, images=1)
    b = g.reserve("image.albedo", 3_000, images=1)
    with pytest.raises(BudgetExceeded, match="2 of 2 images"):
        g.reserve("image.albedo", 10, images=1)
    a.settle(2_867, receipt={"booked_usd": 0.0, "op": "albedo"})  # outcome known: one image
    b.settle(None, receipt={"op": "albedo"})  # outcome unknown: the image and the whole hold stay counted
    assert g.images_used == 2 and g.spent == 5_867
    r = g.receipts()
    assert all(x["unit"] == "tokens" and x["price_applied"] is False and not [k for k in x if k.endswith("_usd")] for x in r)
    assert g.summary().startswith("simple: 5,867 of 10,000 tokens (no price applied); 2 of 2 images")
    c = Governor(plan_for(1), token_ceiling=10_000, max_images=1)
    c.reserve("image.ortho", 100, images=1).settle(50, images=0)  # known: no image came back
    assert c.images_used == 0
    with pytest.raises(BudgetExceeded, match="can never fit: ceiling 10,000 tokens"):
        asyncio.run(c.reserve_wait("author.propose", 20_000))


def test_candidates_never_plan_more_images_than_remain():
    g = Governor(plan_for(3), token_ceiling=10**6, max_images=5)
    assert g.candidates("pbr", unit_cost=1, images_each=2) == 2 and g.candidates("ortho", unit_cost=1, images_each=1) == 4
    assert replace(plan_for(3)).pbr_candidates == 6


# ---------------------------------------------------------------------------------------------- the judge's output cap


async def test_judge_requests_carry_the_cap_and_the_hold_uses_it():
    from antigravity3d.decisions import fallback as F, questions as Q

    seen = {}

    class Models:
        async def count_tokens(self, *, model, contents):
            return type("C", (), {"total_tokens": 1000})()

        async def generate_content(self, *, model, contents, config):
            seen["max"] = config.max_output_tokens
            raise RuntimeError("stop here")

    client = type("Client", (), {"aio": type("Aio", (), {"models": Models()})()})()
    b = F.GenerativeDecisionsBackend(client, model="m", thinking_level="LOW", chunk_size=2)
    qs = {f"q{i}": Q.noul("is it?") for i in range(3)}  # two chunks
    worst = await b.worst_case_tokens("x", qs)
    unseen = [len(json.dumps(F.build_response_schema(c), separators=(",", ":")).encode()) + len(F.SYSTEM_INSTRUCTION.encode())
              for c in (dict(list(qs.items())[:2]), dict(list(qs.items())[2:]))]
    assert worst == sum((1000 + u + F.JUDGE_MAX_OUTPUT_TOKENS) * 2 for u in unseen)
    with pytest.raises(RuntimeError):
        await b.create(input="x", questions={"q0": Q.noul("is it?")})
    assert seen["max"] == F.JUDGE_MAX_OUTPUT_TOKENS == 8192


async def test_the_judges_count_sends_no_per_image_resolution_and_is_never_an_under_count():
    # The API refuses a per-image resolution on an image given by file reference when it counts (the stand-in above
    # answers the same 400), so the count is made without resolutions; an image asked for above the default
    # resolution gets the difference, and the generating call still carries the resolutions.
    from antigravity3d.decisions import fallback as F, questions as Q

    counted, generated = [], []

    class Models:
        async def count_tokens(self, *, model, contents):
            counted.append(contents[0].parts)
            return type("C", (), {"total_tokens": 5000})()

        async def generate_content(self, *, model, contents, config):
            generated.append(contents[0].parts)
            raise RuntimeError("stop here")

    client = type("Client", (), {"aio": type("Aio", (), {"models": Models()})()})()
    b = F.GenerativeDecisionsBackend(client, model="m", thinking_level="LOW")
    qs = {"q0": Q.noul("is it?")}
    low = [{"type": "image", "uri": "https://files/a", "resolution": "low"}, {"type": "image", "uri": "https://files/b", "resolution": "high"}]
    unseen = len(json.dumps(F.build_response_schema(qs), separators=(",", ":")).encode()) + len(F.SYSTEM_INSTRUCTION.encode())
    assert await b.worst_case_tokens(low, qs) == (5000 + unseen + F.JUDGE_MAX_OUTPUT_TOKENS) * 2
    assert all(getattr(p, "media_resolution", None) is None for p in counted[-1]) and counted[-1][0].file_data.file_uri == "https://files/a"
    ultra = [{"type": "image", "uri": "https://files/a", "resolution": "ultra_high"}, {"type": "image", "uri": "https://files/b", "resolution": "low"}]
    extra = F.IMAGE_TOKENS_AT["ultra_high"] - F.IMAGE_TOKENS_DEFAULT
    assert extra == 1120 and await b.worst_case_tokens(ultra, qs) == (5000 + extra + unseen + F.JUDGE_MAX_OUTPUT_TOKENS) * 2
    with pytest.raises(RuntimeError):
        await b.create(input=low, questions=qs)
    assert [p.media_resolution is not None for p in generated[-1][:2]] == [True, True], "the judging call keeps the resolutions"


# --------------------------------------------------------------- what the first live preview runs showed (2026-11-21)


def test_a_geometry_sheet_fails_the_text_question_unless_its_view_labels_are_set_aside(key, tmp_path, capsys, monkeypatch):
    """A geometry candidate is a contact sheet with a view label in each tile. Asked the question a generated image is
    asked, the live judge answered yes to legible text and the only candidate of the run was disqualified. The stand-in
    answers as the live judge did, so this run delivers a package only because the question names the labels."""
    from antigravity3d.decisions import questions as Q

    key()
    assert prop(tmp_path, "--preview", "--token-ceiling", "400000", "--max-images", "12", *FAST) == 0
    assert "preview gates: PASS" in capsys.readouterr().out
    # control: the plain question on the same sheet
    monkeypatch.setattr(Q, "SHEET_KINDS", frozenset())
    other = tmp_path / "plain"
    other.mkdir()
    assert prop(other, "--preview", "--token-ceiling", "400000", "--max-images", "12", *FAST) != 0
    out = capsys.readouterr().out
    assert "policy_legible_text" in out and "preview gates: FAIL" in out


async def test_a_judge_call_slower_than_its_deadline_is_answered_504_once_and_holds_one_reservation(key):
    """Live, a texture ranking that takes about three minutes was sent twice at a 60 s deadline: two reservations held,
    no answer. The service answers 504 when the deadline a request carries runs out; that attempt is not sent again."""
    from google import genai

    from antigravity3d.decisions import questions as Q
    from antigravity3d.decisions.engine import DecisionControlPlane
    from antigravity3d.decisions.fallback import DEFAULT_TIMEOUT_S, GenerativeDecisionsBackend
    from antigravity3d.decisions.models import TransientDecisionError

    assert DEFAULT_TIMEOUT_S >= 600  # measured: an answer came after 176 s, none within 240 s (see fallback.py)
    api = key(judge_delay_s=1.6)
    backend = GenerativeDecisionsBackend(genai.Client(api_key="dummy-not-a-key"), timeout_s=1.0)
    plane = DecisionControlPlane(backend, backend.model, backend_kind="generative_fallback").preview_copy()
    gov = Governor(plan_for(2), token_ceiling=200_000, max_images=0)
    with pytest.raises(TransientDecisionError, match="not sent again"):
        await plane.evaluate("a question about nothing", {"a": Q.noul("Is it?")}, tag="slow", governor=gov)
    assert api.kinds().count("judge:504") == 1 and "judge" not in api.kinds()
    held = [r for r in gov.receipts() if r.get("operation") == "slow"]
    assert [(r["status"], r["source"]) for r in held] == [("ServerError", "reservation_estimate")]
    assert gov.spent == held[0]["booked_tokens"] == held[0]["reserved_tokens"]  # one reservation held, the outcome unknown
    # a call that answers inside its deadline is not affected
    fast = key(judge_delay_s=0.0)
    quick = GenerativeDecisionsBackend(genai.Client(api_key="dummy-not-a-key"), timeout_s=5.0)
    res = await DecisionControlPlane(quick, quick.model, backend_kind="generative_fallback").preview_copy().evaluate(
        "a question about nothing", {"a": Q.noul("Is it?")}, tag="quick", governor=gov)
    assert res.attempts == 1 and fast.kinds().count("judge") == 1


def test_a_design_the_judge_reads_as_a_blockout_of_the_brief_is_sent_back_not_approved(key, tmp_path, capsys):
    """The stand-in answers the brief's depiction score low (level 1): pass A asks the author for a better design instead
    of converging, and the run's export decision fails on the brief. The same run with the default answers converges.
    Since round five a design short only of a score gate gets an improvement round (author:improve, with its renders),
    not a repair: the score names no fault a repair could fix."""
    api = key(score_overrides={"score_brief_depiction": "1"})
    rc = prop(tmp_path, "--preview", "--token-ceiling", "150000", "--max-images", "4", "--single-candidate", *FAST)
    out = capsys.readouterr().out
    assert "author:improve" in api.kinds() and "author:repair" not in api.kinds(), out
    x = report(tmp_path)
    blob = json.dumps(x)
    assert "brief depiction" in blob and "brief_unmet" in blob, blob[:2000]
    # The export decision still fails on the brief (preview gates: FAIL, never approval). The exit code is 0 since round
    # five's delivery rule (antigravity3d.delivery): an uncalibrated judge's score below target is a finding the last
    # lines state, not a failure; --strict makes it exit 3.
    assert not x["published"] and x["preview_gates_ok"] is False and x["review"]["status"] == "below_target"
    assert rc == 0 and "finding: depiction of the brief" in out
