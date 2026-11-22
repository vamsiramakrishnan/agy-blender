"""Paired comparison contracts and command dispatch; no live model endpoints are used."""
from __future__ import annotations

import copy
import importlib
import json
import os
import sys
from pathlib import Path

import pytest


@pytest.fixture
def ca(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "benchmarks"))
    module = importlib.import_module("compare_agents")
    monkeypatch.setattr(module, "BUDGET", copy.deepcopy(module.BUDGET))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    blender = tmp_path / "tools" / "blender"
    blender.parent.mkdir()
    blender.write_text("#!/bin/sh\necho 'Blender 5.3.0'\n")
    blender.chmod(0o755)
    monkeypatch.setenv("BLENDER_BIN", str(blender))
    return module


def flags(mode="single"):
    return ["--paired", "--round", "prop-contracts", "--c-candidates", mode,
            "--run-tokens", "A=700000", "C=700000", "--run-images", "A=12", "C=12",
            "--ceiling-tokens", "1425000", "--ceiling-images", "24"]


def paired(ca, capsys, mode="single"):
    assert ca.main(["plan", "--json", *flags(mode)]) == 0
    return json.loads(capsys.readouterr().out)


def test_paired_plan_shares_requirements_caps_and_judges_both_orders(ca, capsys, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("planning must not create a relay or run an agent")

    monkeypatch.setattr(ca, "make_relay", forbidden)
    monkeypatch.setattr(ca, "run_stock", forbidden)
    plan = paired(ca, capsys)
    assert plan["planned_totals"] == {"tokens": 1_425_000, "images": 24}
    assert plan["budget"]["run_tokens"] == {"A": 700_000, "C": 700_000}
    assert plan["budget"]["run_images"] == {"A": 12, "C": 12}
    assert plan["budget"]["continuations"] == {} and plan["budget"]["c_candidates"] == "single"
    assert plan["budget"]["changes"] is False and plan["budget"]["small_judge_tokens_per_call"] == 0
    assert not any(".change" in name or ".repeat" in name or "judge_small" in name for name in ca.planned_accounts())
    assert plan["order"] == [["crate", "A"], ["crate", "C"]]
    prompt = plan["prompts"]["crate"]
    assert prompt["content"] in prompt["A"] and prompt["C"] == prompt["content"]
    assert "at most 3 PBR" in prompt["C"] and "ceiling, not a quality score" in prompt["C"]
    assert plan["author_contracts"]["native_AgentAuthor_tested"] is False
    assert plan["approval_required_for_live"] is True
    ce = importlib.import_module("compare_eval")
    judges = ce.judge_plan(["crate"], ["A", "C"], seed=plan["judge"]["order_seed"])
    assert {(j["first"], j["second"]) for j in judges} == {("A", "C"), ("C", "A")}
    assert len(judges) == plan["judge"]["calls"] == 2
    assert plan["judge"]["view_size"] == "full" and plan["judge"]["game_size_calls"] == 0


@pytest.mark.parametrize("change,reason", [
    (("C=700000", "C=600000"), "equal explicit"),
    (("C=12", "C=11"), "equal explicit"),
    (("prop-contracts", "r2"), "fresh --round"),
    (("1425000", "1400000"), "more than the ceiling"),
])
def test_paired_plan_refuses_asymmetry_legacy_round_and_underfunding(ca, capsys, change, reason):
    args = flags()
    args[args.index(change[0])] = change[1]
    assert ca.main(["plan", *args]) == 2
    assert reason in capsys.readouterr().err


@pytest.mark.parametrize("flag,reason", [
    ("--c-candidates", "explicit --c-candidates"),
    ("--ceiling-images", "explicit --ceiling"),
    ("--round", "fresh --round"),
])
def test_paired_plan_requires_explicit_reviewable_choices(ca, capsys, flag, reason):
    args = flags()
    i = args.index(flag)
    del args[i:i + 2]
    assert ca.main(["plan", *args]) == 2
    assert reason in capsys.readouterr().err


@pytest.mark.parametrize("extra", [["--continue", "crate:A"], ["--c-native"], ["--only", "unknown:A"],
                                   ["--change", "crate:C"], ["--repeat", "crate:C"], ["--change-images", "C=1"]])
def test_paired_dry_run_refuses_unplanned_treatment_before_start(ca, capsys, monkeypatch, tmp_path, extra):
    monkeypatch.setattr(ca, "make_relay", lambda *a, **kw: pytest.fail("invalid plan started a relay"))
    assert ca.main(["dry-run", "--root", str(tmp_path), *flags(), *extra]) == 2
    assert "refusing:" in capsys.readouterr().err
    assert not (tmp_path / "ledger.json").exists()


@pytest.mark.parametrize("mode,has_flag", [("single", True), ("default", False)])
def test_paired_c_command_uses_fixed_candidate_mode_and_shared_brief(ca, capsys, monkeypatch, tmp_path, mode, has_flag):
    plan = paired(ca, capsys, mode)
    seen = {}

    class CapturedProcess:
        def __init__(self, cmd, **kwargs):
            seen.update(command=cmd, env=kwargs["env"])
            self.pid = os.getpid()

        def wait(self, timeout):
            seen["timeout"] = timeout
            return 2

    # The legacy planner advises a downgrade. A paired experiment must retain its declared treatment instead.
    monkeypatch.setattr(ca, "c_mode", lambda *a: (True, ["hero: up to 20 image(s)"], ["hero: up to 3 image(s)"]))
    monkeypatch.setattr(ca.subprocess, "Popen", CapturedProcess)
    record = ca.run_c(None, tmp_path, ca.BRIEF_BY_ID["crate"], offline=True)
    assert ("--single-candidate" in seen["command"]) is has_flag
    assert seen["command"][seen["command"].index("prop") + 1] == plan["prompts"]["crate"]["content"]
    assert "--offline" in seen["command"] and "GEMINI_API_KEY" not in seen["env"]
    assert record.pipeline["mode"] == ("single-candidate" if has_flag else "default")
    assert "not native AgentAuthor" in record.pipeline["author_contract"]
    assert seen["timeout"] == plan["wall_limit_s_per_arm"] == 75 * 60


def test_paired_pipeline_diagnostic_glb_does_not_count_as_completed_delivery(ca, capsys, monkeypatch, tmp_path):
    paired(ca, capsys)

    class FailedGateProcess:
        def __init__(self, cmd, **kwargs):
            output = Path(cmd[cmd.index("--out") + 1])
            output.mkdir()
            (output / "diagnostic.glb").write_bytes(b"diagnostic output retained after a failed gate")
            self.pid = os.getpid()

        def wait(self, timeout):
            return 1

    monkeypatch.setattr(ca, "c_mode", lambda *a: (False, [], []))
    monkeypatch.setattr(ca.subprocess, "Popen", FailedGateProcess)
    record = ca.run_c(None, tmp_path, ca.BRIEF_BY_ID["crate"], offline=True)
    assert record.glb is not None and Path(record.glb).exists()
    assert record.status == "failed gates" and record.pipeline["exit_code"] == 1


def test_paired_contract_prevents_relabeling_fake_results_or_reusing_changed_source(ca, capsys, monkeypatch, tmp_path):
    paired(ca, capsys)
    src = tmp_path / "source" / "src"
    src.mkdir(parents=True)
    source = src / "author.py"
    source.write_text("CONTRACT = 'production_prop'\n")
    monkeypatch.setattr(ca, "PROJECT_SRC", src)
    root = tmp_path / "result"
    contract = ca.bind_comparison_contract(root, live=False, c_against_fake=True)
    assert ca.bind_comparison_contract(root, live=False, c_against_fake=True) == contract
    before = (root / "comparison-contract.json").read_bytes()
    with pytest.raises(ValueError, match="execution mode changed"):
        ca.bind_comparison_contract(root, live=True, c_against_fake=True)
    source.write_text("CONTRACT = 'blockout'\n")
    with pytest.raises(ValueError, match="source, plan"):
        ca.bind_comparison_contract(root, live=False, c_against_fake=True)
    assert (root / "comparison-contract.json").read_bytes() == before


def test_paired_contract_will_not_adopt_historical_unbound_records(ca, capsys, tmp_path):
    paired(ca, capsys)
    ledger = tmp_path / "ledger.json"
    ledger.write_text('{"historical": true}\n')
    with pytest.raises(ValueError, match="fresh --root"):
        ca.bind_comparison_contract(tmp_path, live=False)
    assert not (tmp_path / "comparison-contract.json").exists()
    assert ledger.read_text() == '{"historical": true}\n'


def test_common_constraints_fail_four_materials_and_hand_completed_output(ca):
    stock = ca.RunRecord("crate", "A", status="finished")
    pipeline = ca.RunRecord("crate", "C", status="finished", pipeline={"exit_code": 0})
    evaluated = {"crate:A": {"glb_exists": True, "facts": {"triangles": 1476, "materials": 3}},
                 "crate:C": {"glb_exists": True, "facts": {"triangles": 752, "materials": 4}}}
    result = ca.paired_constraints([stock, pipeline], evaluated)
    assert result["crate:A"]["status"] == "passed"
    assert result["crate:C"]["status"] == "failed" and result["crate:C"]["checks"]["material_limit"] is False
    pipeline.pipeline["exit_code"] = 1
    evaluated["crate:C"]["facts"]["materials"] = 3
    assert ca.paired_constraints([pipeline], evaluated)["crate:C"]["checks"]["delivered_unassisted"] is False
    assert ca.paired_constraints([stock], {})["crate:A"]["status"] == "not evaluated"


def test_paired_live_still_requires_go_before_any_model_or_files(ca, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(ca, "make_relay", lambda *a, **kw: pytest.fail("unapproved live run started"))
    assert ca.main(["live", "--allow-c", "--root", str(tmp_path), *flags()]) == 2
    assert "real calls need --go" in capsys.readouterr().err
    assert not (tmp_path / "comparison-contract.json").exists()


def test_mixed_fake_and_live_root_is_rejected_before_creating_relay(ca, tmp_path, capsys, monkeypatch):
    paired(ca, capsys)
    ca.bind_comparison_contract(tmp_path, live=False)
    monkeypatch.setenv("GEMINI_API_KEY", "not-a-key-and-never-sent")
    monkeypatch.setattr(ca, "make_relay", lambda *a, **kw: pytest.fail("mixed provenance started a relay"))
    assert ca.main(["live", "--go", "--allow-c", "--root", str(tmp_path), *flags()]) == 2
    assert "execution mode changed" in capsys.readouterr().err
    assert not (tmp_path / "ledger.json").exists()


def test_paired_c_only_cannot_silently_omit_blender(ca, tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("BLENDER_BIN")
    monkeypatch.setattr(ca, "make_relay", lambda *a, **kw: pytest.fail("missing shared Blender started a relay"))
    # Planning remains usable on a host without engines. Execution must satisfy its declared tool parity.
    assert ca.main(["plan", "--json", *flags()]) == 0
    assert json.loads(capsys.readouterr().out)["toolchain"]["BLENDER_BIN"] is None
    assert ca.main(["dry-run", "--root", str(tmp_path), "--only", "crate:C", *flags()]) == 2
    assert "BLENDER_BIN" in capsys.readouterr().err
    assert not (tmp_path / "comparison-contract.json").exists()
    assert not (tmp_path / "ledger.json").exists()


@pytest.mark.parametrize("tool", ["BLENDER_BIN", "GODOT_BIN", "CHROMIUM_BIN", "AGY3D_THREE_JS"])
def test_paired_contract_refuses_changed_toolchain_between_cells(ca, tmp_path, capsys, monkeypatch, tool):
    paired(ca, capsys)
    root = tmp_path / "result"
    ca.bind_comparison_contract(root, live=False)
    before = (root / "comparison-contract.json").read_bytes()
    monkeypatch.setenv(tool, str(tmp_path / "different" / "tool"))
    with pytest.raises(ValueError, match="source, plan or execution mode changed"):
        ca.bind_comparison_contract(root, live=False)
    assert (root / "comparison-contract.json").read_bytes() == before


@pytest.mark.parametrize("override", [{"force_single": "remaining image headroom"}, {"prompt": "simpler box"},
                                      {"session": 2}, {"change": "larger handle"}, {"out_seed": Path("old-output")}])
def test_paired_c_rejects_unrecorded_treatments_before_creating_run(ca, tmp_path, capsys, override):
    paired(ca, capsys, "default")
    with pytest.raises(ValueError, match="declared brief and candidate policy"):
        ca.run_c(None, tmp_path, ca.BRIEF_BY_ID["crate"], offline=True, **override)
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize("version", ["Blender 4.3.2", "Blender 5.2.0", "not Blender"])
def test_paired_old_or_unrecognised_blender_is_refused_before_any_spend(ca, tmp_path, capsys, monkeypatch, version):
    blender = Path(os.environ["BLENDER_BIN"])
    blender.write_text(f"#!/bin/sh\necho '{version}'\n")
    monkeypatch.setattr(ca, "make_relay", lambda *a, **kw: pytest.fail("incompatible Blender started a relay"))
    assert ca.main(["dry-run", "--root", str(tmp_path), *flags()]) == 2
    assert "Blender 5.3 or newer" in capsys.readouterr().err
    assert not (tmp_path / "comparison-contract.json").exists()
    assert not (tmp_path / "ledger.json").exists()


def test_paired_contract_binds_tools_resolved_from_path(ca, tmp_path, capsys, monkeypatch):
    paired(ca, capsys)
    monkeypatch.delenv("GODOT_BIN", raising=False)
    original_path = os.environ["PATH"]
    for dirname in ("first", "second"):
        tool = tmp_path / dirname / "godot"
        tool.parent.mkdir()
        tool.write_text("#!/bin/sh\nexit 0\n")
        tool.chmod(0o755)
    root = tmp_path / "result"
    monkeypatch.setenv("PATH", f"{tmp_path / 'first'}:{original_path}")
    contract = ca.bind_comparison_contract(root, live=False)
    assert contract["toolchain"]["GODOT_BIN"] is None
    assert contract["toolchain"]["resolved_godot"]["path"] == str(tmp_path / "first" / "godot")
    monkeypatch.setenv("PATH", f"{tmp_path / 'second'}:{original_path}")
    with pytest.raises(ValueError, match="source, plan or execution mode changed"):
        ca.bind_comparison_contract(root, live=False)


def test_paired_stock_receives_same_wall_allowance_as_pipeline(ca, tmp_path, capsys, monkeypatch):
    from google.antigravity import types
    from google.antigravity.models import DEFAULT_IMAGE_GENERATION_MODEL, DEFAULT_MODEL, GeminiModelOptions

    plan = paired(ca, capsys)
    monkeypatch.setattr(ca, "_chown_tree", lambda *a: None)
    monkeypatch.setattr(ca, "socket_dir", lambda *a: tmp_path / "sockets")
    spec = ca.stock_spec(tmp_path / "stock", "A", ca.BRIEF_BY_ID["crate"],
                         share_tokens=700_000, share_images=12, calls=ca.calls_allowed("A"),
                         timeout_s=ca.C_TIMEOUT_S - 300)
    config = json.loads(Path(spec["host_config"]).read_text())
    assert config["timeout_s"] == plan["wall_limit_s_per_arm"] == 75 * 60
    assert config["task"] == plan["prompts"]["crate"]["A"]
    stock = plan["stock_execution"]
    assert stock["text_model"] == DEFAULT_MODEL and stock["image_model"] == DEFAULT_IMAGE_GENERATION_MODEL
    assert stock["max_output_tokens"] == config["max_output_tokens"] == ca.STOCK_MAX_OUTPUT
    assert stock["thinking_level"] == GeminiModelOptions(max_output_tokens=config["max_output_tokens"]).thinking_level
    assert stock["disabled_tools"] == [types.BuiltinTools.SEARCH_WEB.value, types.BuiltinTools.READ_URL_CONTENT.value]
    assert stock["hard_limits"] == config["budget"] == {"max_model_calls": 40, "max_tool_calls": 120}
    assert stock["advisory_model_calls"] == ca.calls_allowed("A", 700_000) == 14
    assert stock["advisory_is_enforced"] is False
    assert stock["sdk_timeout_s"] == config["timeout_s"] == stock["outer_timeout_s"]


def test_paired_notes_and_plan_expose_overrides_without_r5_treatments(ca, tmp_path, capsys):
    from antigravity3d.tactics.metered_author import AUTHOR_OUTPUT_BUDGETS

    plan = paired(ca, capsys)
    assert plan["author_contracts"]["A"] == "stock SDK agent with recorded benchmark overrides"
    assert plan["notes"] == ca.plan_notes()
    assert "12 images allowed" in plan["notes"]["stock model"]
    assert "0 images this round" not in json.dumps(plan)
    assert "new session over a copy" not in json.dumps(plan["notes"])
    assert "No continuations, change requests or repeats" in plan["notes"]["additional sessions"]
    limitations = " ".join(plan["limitations"])
    assert f"{ca.STOCK_MAX_OUTPUT:,}-token output cap" in limitations
    assert f"{AUTHOR_OUTPUT_BUDGETS['worker'].max_output_tokens:,}-token output cap" in limitations
    assert ca.assemble(tmp_path, None, live=False)["plan"]["notes"] == plan["notes"]


def test_paired_live_flags_reach_dispatch_without_actual_model_calls(ca, tmp_path, capsys, monkeypatch):
    seen = {}

    def capture(root, **kwargs):
        seen.update(root=root, **kwargs)
        return {"tally": {"tokens": 0, "images": 0}}

    monkeypatch.setenv("GEMINI_API_KEY", "not-a-key-and-never-sent")
    monkeypatch.setattr(ca, "run_all", capture)
    monkeypatch.setattr(ca, "make_relay", lambda *a, **kw: pytest.fail("dispatch probe made a relay"))
    assert ca.main(["live", "--go", "--allow-c", "--root", str(tmp_path), *flags()]) == 0
    assert seen["live"] is True and seen["allow_c"] is True and seen["c_native"] is False
    assert seen["change_cells"] is None and seen["repeat_cells"] is None
    assert not (tmp_path / "ledger.json").exists()


def test_score_uses_saved_paired_round_and_never_rewrites_ledger(ca, tmp_path, capsys, monkeypatch):
    paired(ca, capsys)
    contract = ca.bind_comparison_contract(tmp_path, live=False)
    ledger_path = tmp_path / "ledger.json"
    ledger = ca.Ledger(ledger_path)
    account = ca.cell_name("crate", "C")
    ledger.open_account(account, 700_000, 12)
    ledger.reserve(account, 100, 0)
    before = ledger_path.read_bytes()
    rec = ca.RunRecord("crate", "C", status="finished", account=account, pipeline={"exit_code": 0})
    run = tmp_path / "runs" / account.replace(":", "_")
    run.mkdir(parents=True)
    (run / "record.json").write_text(json.dumps(ca.asdict(rec)))
    (tmp_path / "evaluation_prop-contracts.json").write_text(json.dumps(
        {"crate:C": {"glb_exists": True, "facts": {"triangles": 900, "materials": 3}}}))
    ce = importlib.import_module("compare_eval")
    seen = {}

    def score(root, results, briefs, round_):
        seen.update(results=results, round=round_)
        return {"summary": {"cells": len(results["records"])}}

    monkeypatch.setattr(ce, "scorecard", score)
    monkeypatch.setattr(ce, "write_gallery", lambda *a, **kw: None)
    monkeypatch.setattr(ca, "BUDGET", copy.deepcopy(ca.ROUND2_BUDGET))
    assert ca.main(["score", "--root", str(tmp_path)]) == 0
    assert seen["round"] == "prop-contracts"
    assert len(seen["results"]["records"]) == 1
    assert seen["results"]["comparison_contract"] == json.loads(json.dumps(contract))
    assert seen["results"]["tally"]["ceiling_tokens"] == 1_425_000
    assert seen["results"]["tally"]["inflight_tokens"] == 100
    assert seen["results"]["shared_constraints"]["crate:C"]["status"] == "passed"
    assert ledger_path.read_bytes() == before
    assert ca.BUDGET["round"] == "r2"
