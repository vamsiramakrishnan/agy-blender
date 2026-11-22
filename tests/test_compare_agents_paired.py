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
def ca(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "benchmarks"))
    module = importlib.import_module("compare_agents")
    monkeypatch.setattr(module, "BUDGET", copy.deepcopy(module.BUDGET))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
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


@pytest.mark.parametrize("extra", [["--continue", "crate:A"], ["--c-native"], ["--only", "unknown:A"]])
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
