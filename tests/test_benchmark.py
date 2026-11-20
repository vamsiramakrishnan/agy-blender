import json

import pytest

from antigravity3d.tactics.benchmark import compare


def cohort(tmp_path, pairs=1):
    rows = []
    for i in range(pairs):
        row = {"brief_id": str(i)}
        for mode in ("single", "governed"):
            first = mode == "governed"
            report = tmp_path / f"{i}-{mode}.json"
            report.write_text(json.dumps({"prompt": f"brief {i}", "engine": "godot_4", "budget": {"max_tris": 512},
                                          "wall_ms": 100, "candidate_mode": mode, "decision_backend": "decisions",
                                          "verified": first}))
            row[mode] = {"report": report.name, "input_tokens": 50, "output_tokens": 10, "billed_usd": .3,
                         "rework_minutes": 0 if first else 20, "first_pass": first, "accepted": first}
        rows.append(row)
    path = tmp_path / "cohort.json"
    path.write_text(json.dumps({"ceiling_usd": 1.0, "pairs": rows}))
    return path


def test_paired_release_gate_needs_sample_and_labels(tmp_path):
    assert not compare(cohort(tmp_path))["rollout_pass"]
    result = compare(cohort(tmp_path, 50))
    assert result["rollout_pass"] and result["first_pass_gain_pp"] == 100
    assert result["single"]["usd_per_accepted"] is None
    assert result["governed"]["usd_per_accepted"] == pytest.approx(.3)
    assert result["single"]["input_tokens"] == 2500
    assert result["rework_reduction_pct"] == 100
    assert result["governed"]["wall_p95_ms"] == 100


@pytest.mark.parametrize(("key", "wrong"), [("prompt", "wrong"), ("verified", False), ("budget", {}),
                                            ("decision_backend", "fake"), ("candidate_mode", "single")])
def test_rejects_invalid_pairs(tmp_path, key, wrong):
    path = cohort(tmp_path)
    report = tmp_path / "0-governed.json"
    data = json.loads(report.read_text())
    report.write_text(json.dumps({**data, key: wrong}))
    with pytest.raises(ValueError):
        compare(path)
