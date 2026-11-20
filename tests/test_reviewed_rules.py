"""Authored fixtures and simulated history. No studio or provider outcomes are assumed."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from antigravity3d.runtime import distill_job as job
from antigravity3d.tactics import reviewed_rules as rules
from antigravity3d.tactics.distill import FixLog, FixRecord

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "distill-seating-holdout.json"


def synthetic_history():
    return [FixRecord(f"train{i}", {"defect_floating": .9}, ["set_transform"], ["cap.translate"],
                      2.8, 3.6, rule=rules.RULE, scene_hash=f"{i:064x}", origin="decisions/author")
            for i in range(3)]


def corpus():
    return rules.Corpus.model_validate_json(EXAMPLE.read_text())


def test_measures_geometric_improvement_and_checks_controls():
    result = job.evaluate(synthetic_history(), corpus())
    assert result["eligible"] and result["training_shapes"] == 3
    assert result["mae_reduction_m"] == pytest.approx(.06)
    assert [r["changed"] for r in result["replay"]] == [True, False]
    assert all(not r["new_tier1_issues"] for r in result["replay"])
    labels = corpus()
    # Art direction intentionally suspends this lid. The physical correction now
    # moves it away from the authored target and must veto promotion.
    raised = labels.fixtures[0]
    raised.expected_z["lid"] = raised.scene.nodes[1].transform.translate[2]
    failed = job.evaluate(synthetic_history(), labels)
    assert failed["replay"][0]["regressed"] and not failed["eligible"]


def test_rejects_training_leakage_uncalibrated_and_repeated_shapes():
    records = synthetic_history()
    fixture = corpus().fixtures[0]
    renamed = fixture.scene.model_copy(deep=True)
    renamed.asset_name = "Other"
    renamed.nodes[0].id = "renamed_support"
    assert rules.scene_hash(renamed) == rules.scene_hash(fixture.scene)
    expired = replace(records[0], asset_id="Other", scene_hash=rules.scene_hash(renamed))
    assert not job.evaluate(records, corpus(), history=[*records, expired])["eligible"]
    assert not job.evaluate([replace(r, origin="fake/author") for r in records], corpus())["eligible"]
    assert not job.evaluate([replace(r, scene_hash=records[0].scene_hash) for r in records], corpus())["eligible"]
    assert not job.evaluate(records, None)["eligible"]


def test_cli_review_discovers_prop_outputs_and_binds_promotion_to_exact_replay(tmp_path, monkeypatch):
    project, external = tmp_path / "project", tmp_path / "other-volume" / "asset"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setenv("AGY3D_STATE_DIR", str(project / ".agy3d"))
    log = FixLog.for_prop(external)
    for r in synthetic_history():
        log.append(r)
    assert job.discover_logs(project) == [log.path.resolve()]
    assert not rules.is_enabled()
    result = job.evaluate(synthetic_history(), corpus())
    args = ["--workspace", str(project), "--holdout", str(EXAMPLE)]
    assert job.main(args) == 0
    assert not rules.is_enabled()
    review = ["--promote", rules.RULE, "--reviewed-by", "studio reviewer", "--evidence"]
    with pytest.raises(SystemExit):
        job.main([*args, *review, "wrong"])
    assert not rules.is_enabled()
    assert job.main([*args, *review, result["digest"]]) == 0
    assert rules.is_enabled()
    approval = json.loads(rules.registry_path().read_text())
    assert approval["evidence"] == result["digest"] and approval["reviewer"] == "studio reviewer"
    assert approval["implementation"] == rules.implementation_hash()
    archived = json.loads((project / ".agy3d/distill/evidence" / f"{result['digest']}.json").read_text())
    assert archived == result["evidence"]


@pytest.mark.asyncio
async def test_promoted_rule_runs_before_general_settling(tmp_path, monkeypatch):
    from antigravity3d.cli import offline_plane
    from antigravity3d.kernel.usd_stage import StageManager
    from antigravity3d.state import EngineBudget
    from antigravity3d.tactics.authoring import TemplateAuthor
    from antigravity3d.tactics.governor import Governor, plan_for
    from antigravity3d.tactics.pyramid import refine

    monkeypatch.setenv("AGY3D_STATE_DIR", str(tmp_path / "state"))
    log = FixLog.for_prop(tmp_path / "output")
    for r in synthetic_history():
        log.append(r)
    data = corpus()
    digest = job.evaluate(synthetic_history(), data)["digest"]
    job.run(tmp_path, None, None, holdout=EXAMPLE, promote=rules.RULE, reviewer="test", evidence=digest)
    sm = StageManager.create(tmp_path / "test.usda", data.fixtures[0].scene.asset_name)
    sm.apply_ir(data.fixtures[0].scene)
    result = await refine(sm, plane=offline_plane(), author=TemplateAuthor(), governor=Governor(plan_for(0)),
                          budget=EngineBudget("unreal_5"), workdir=tmp_path / "out", max_rounds=1,
                          run_pass_b=False, run_pass_c=False)
    assert next(step.detail for step in result.steps if step.name == "settle_pre") == rules.RULE
    assert sm.base_ir().nodes[1].transform.translate[2] == pytest.approx(data.fixtures[0].expected_z["lid"])
