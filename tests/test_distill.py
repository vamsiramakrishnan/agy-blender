from __future__ import annotations

import json

import pytest

from antigravity3d.tactics.distill import (
    FixCluster,
    FixLog,
    FixRecord,
    cluster,
    propose_rules,
    signature,
)


def make(
    asset_id: str = "a",
    defects: dict[str, float] | None = None,
    ops: list[str] | None = None,
    params: list[str] | None = None,
    before: float = 0.4,
    after: float = 0.8,
    note: str = "",
) -> FixRecord:
    return FixRecord(
        asset_id=asset_id,
        defect_vector=defects if defects is not None else {"lid.gap": 0.9},
        ops=ops if ops is not None else ["set_param"],
        params_changed=params if params is not None else ["lid.size"],
        score_before=before,
        score_after=after,
        note=note,
    )


# --- FixLog -----------------------------------------------------------------

def test_append_read_round_trip(tmp_path):
    log = FixLog(tmp_path / "nested" / "dir" / "fixes.jsonl")
    original = make("crate_01", {"lid.gap": 0.9, "hinge.loose": 0.2}, ["set_param", "set_transform"],
                    ["lid.size", "hinge.translate"], 0.31, 0.77, note="widened lid")
    log.append(original)
    log.append(make("crate_02"))

    records = log.read()
    assert log.skipped == 0
    assert len(records) == 2
    assert records[0] == original
    assert records[1].asset_id == "crate_02"


def test_append_writes_one_json_object_per_line_with_ts(tmp_path):
    path = tmp_path / "fixes.jsonl"
    FixLog(path).append(make("x"))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["asset_id"] == "x"
    assert obj["ops"] == ["set_param"]
    assert isinstance(obj["ts"], str) and obj["ts"].endswith("+00:00")


def test_append_is_append_only(tmp_path):
    path = tmp_path / "fixes.jsonl"
    log = FixLog(path)
    log.append(make("first"))
    log.append(make("second"))
    assert [r.asset_id for r in FixLog(path).read()] == ["first", "second"]


def test_read_missing_file_returns_empty(tmp_path):
    log = FixLog(tmp_path / "absent.jsonl")
    assert log.read() == []
    assert log.skipped == 0


def test_read_skips_blank_lines_without_counting(tmp_path):
    path = tmp_path / "fixes.jsonl"
    FixLog(path).append(make("a"))
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n   \n\t\n")
    FixLog(path).append(make("b"))
    log = FixLog(path)
    assert [r.asset_id for r in log.read()] == ["a", "b"]
    assert log.skipped == 0


def test_read_skips_malformed_lines_and_counts_them(tmp_path):
    path = tmp_path / "fixes.jsonl"
    good = json.dumps({
        "asset_id": "ok",
        "defect_vector": {"d": 1.0},
        "ops": ["set_param"],
        "params_changed": ["p.q"],
        "score_before": 0.1,
        "score_after": 0.9,
    })
    lines = [
        good,
        "{not json",
        "[1, 2, 3]",  # not an object
        json.dumps({"asset_id": "missing-fields"}),
        json.dumps({**json.loads(good), "ops": "set_param"}),  # wrong type
        json.dumps({**json.loads(good), "score_after": "high"}),  # wrong type
        json.dumps({**json.loads(good), "defect_vector": {"d": "x"}}),  # wrong type
        json.dumps({**json.loads(good), "asset_id": 7}),  # wrong type
        good.replace('"ok"', '"ok2"'),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    log = FixLog(path)
    records = log.read()
    assert [r.asset_id for r in records] == ["ok", "ok2"]
    assert log.skipped == 7


def test_read_resets_skipped_counter_each_call(tmp_path):
    path = tmp_path / "fixes.jsonl"
    path.write_text("garbage\n", encoding="utf-8")
    log = FixLog(path)
    log.read()
    assert log.skipped == 1
    path.write_text("", encoding="utf-8")
    log.read()
    assert log.skipped == 0


def test_note_defaults_to_empty_when_absent(tmp_path):
    path = tmp_path / "fixes.jsonl"
    path.write_text(json.dumps({
        "asset_id": "n",
        "defect_vector": {},
        "ops": [],
        "params_changed": [],
        "score_before": 0.0,
        "score_after": 0.5,
    }) + "\n", encoding="utf-8")
    records = FixLog(path).read()
    assert len(records) == 1
    assert records[0].note == ""


# --- signature --------------------------------------------------------------

def test_signature_thresholds_defects_inclusively():
    r = make(defects={"a": 0.5, "b": 0.49, "c": 0.9, "d": 0.0})
    defects, _ = signature(r)
    assert defects == ("a", "c")


def test_signature_custom_threshold():
    r = make(defects={"a": 0.5, "b": 0.49, "c": 0.9})
    assert signature(r, threshold=0.8)[0] == ("c",)
    assert signature(r, threshold=0.0)[0] == ("a", "b", "c")


def test_signature_ops_sorted_and_unique():
    r = make(ops=["set_transform", "set_param", "set_param"])
    assert signature(r)[1] == ("set_param", "set_transform")


def test_signature_ignores_params_and_scores_and_note():
    a = make(params=["x.y"], before=0.1, after=0.2, note="one")
    b = make(params=["z.w"], before=0.5, after=0.9, note="two")
    assert signature(a) == signature(b)


def test_signature_empty_defects():
    assert signature(make(defects={"a": 0.1}))[0] == ()


# --- cluster ----------------------------------------------------------------

def test_cluster_requires_min_count():
    recs = [make(f"a{i}", after=0.9) for i in range(2)]
    assert cluster(recs) == []
    recs.append(make("a2", after=0.9))
    assert len(cluster(recs)) == 1


def test_cluster_min_count_is_configurable():
    recs = [make("a0", after=0.9), make("a1", after=0.9)]
    assert len(cluster(recs, min_count=2)) == 1


def test_cluster_mean_gain_threshold_inclusive():
    # mean gain exactly 0.3 passes min_gain=0.3
    recs = [make(f"a{i}", before=0.2, after=0.5) for i in range(3)]
    result = cluster(recs)
    assert len(result) == 1
    assert result[0].mean_gain == pytest.approx(0.3)


def test_cluster_drops_low_gain_groups():
    recs = [make(f"a{i}", before=0.5, after=0.6) for i in range(3)]  # gain 0.1
    assert cluster(recs) == []
    assert len(cluster(recs, min_gain=0.05)) == 1


def test_cluster_mean_not_per_record_gain():
    # one big win and two no-ops: mean 0.3 -> kept at min_gain 0.3
    recs = [
        make("big", before=0.0, after=0.9),
        make("n1", before=0.5, after=0.5),
        make("n2", before=0.5, after=0.5),
    ]
    result = cluster(recs)
    assert len(result) == 1
    assert result[0].mean_gain == pytest.approx(0.3)


def test_cluster_groups_by_signature():
    recs = [
        make("a1", defects={"lid.gap": 0.9}, ops=["set_param"], after=0.9),
        make("a2", defects={"lid.gap": 0.7}, ops=["set_param"], after=0.9),
        make("a3", defects={"lid.gap": 0.8}, ops=["set_param"], after=0.9),
        make("b1", defects={"lid.gap": 0.9}, ops=["set_transform"], after=0.9),
        make("b2", defects={"lid.gap": 0.9}, ops=["set_transform"], after=0.9),
        make("b3", defects={"lid.gap": 0.9}, ops=["set_transform"], after=0.9),
    ]
    result = cluster(recs)
    assert {c.ops for c in result} == {("set_param",), ("set_transform",)}
    assert all(c.count == 3 for c in result)


def test_cluster_sort_order_by_count_times_gain_then_defects_then_ops():
    # A: count 3, gain 0.5 -> 1.5 ; B: count 4, gain 0.4 -> 1.6 ; C: count 3, gain 0.5 -> 1.5
    recs: list[FixRecord] = []
    recs += [make(f"a{i}", defects={"A": 0.9}, ops=["op1"], before=0.0, after=0.5) for i in range(3)]
    recs += [make(f"b{i}", defects={"B": 0.9}, ops=["op1"], before=0.0, after=0.4) for i in range(4)]
    recs += [make(f"c{i}", defects={"C": 0.9}, ops=["op1"], before=0.0, after=0.5) for i in range(3)]
    result = cluster(recs)
    assert [c.defects for c in result] == [("B",), ("A",), ("C",)]


def test_cluster_tie_broken_by_defects_then_ops():
    recs: list[FixRecord] = []
    recs += [make(f"z{i}", defects={"zeta": 0.9}, ops=["x"], before=0.0, after=0.5) for i in range(3)]
    recs += [make(f"y{i}", defects={"alpha": 0.9}, ops=["x"], before=0.0, after=0.5) for i in range(3)]
    result = cluster(recs)
    assert [c.defects for c in result] == [("alpha",), ("zeta",)]


def test_cluster_fields_and_example_ids_first_three_in_input_order():
    recs = [
        make("first", defects={"d1": 0.9, "d2": 0.1}, ops=["b", "a", "a"], params=["lid.size"], before=0.1, after=0.9),
        make("second", defects={"d1": 0.9}, ops=["a", "b"], params=["hinge.size"], before=0.1, after=0.9),
        make("third", defects={"d1": 0.6}, ops=["b", "a"], params=["x.size"], before=0.1, after=0.9),
        make("fourth", defects={"d1": 0.95}, ops=["a", "b", "a"], params=["y.size"], before=0.1, after=0.9),
    ]
    result = cluster(recs)
    assert len(result) == 1
    c = result[0]
    assert isinstance(c, FixCluster)
    assert c.defects == ("d1",)
    assert c.ops == ("a", "b")
    assert c.count == 4
    assert c.mean_gain == pytest.approx(0.8)
    assert c.example_asset_ids == ("first", "second", "third")


def test_cluster_empty_input():
    assert cluster([]) == []


def test_cluster_threshold_argument_changes_grouping():
    recs = [make(f"a{i}", defects={"d": 0.45}, before=0.0, after=0.9) for i in range(3)]
    result = cluster(recs, threshold=0.5)  # 0.45 not flagged -> empty defect signature
    assert len(result) == 1 and result[0].defects == ()
    result_low = cluster(recs, threshold=0.4)
    assert len(result_low) == 1 and result_low[0].defects == ("d",)


# --- common_params ----------------------------------------------------------

def test_common_params_uses_part_after_last_dot():
    recs = [
        make("a", params=["lid.size", "hinge.translate"], before=0.0, after=0.9),
        make("b", params=["handle.size"], before=0.0, after=0.9),
        make("c", params=["lid.size"], before=0.0, after=0.9),
    ]
    result = cluster(recs)
    # "size" in 3/3 records; "translate" in 1/3 (below half)
    assert result[0].common_params == ("size",)


def test_common_params_at_least_half_inclusive():
    recs = [
        make("a", params=["p.alpha", "q.beta"], before=0.0, after=0.9),
        make("b", params=["alpha"], before=0.0, after=0.9),
        make("c", params=["r.gamma"], before=0.0, after=0.9),
        make("d", params=["beta"], before=0.0, after=0.9),
    ]
    # alpha: 2/4 = half -> kept; beta: 2/4 -> kept; gamma: 1/4 -> dropped
    assert cluster(recs, min_count=4)[0].common_params == ("alpha", "beta")


def test_common_params_counts_each_record_once_and_sorted():
    recs = [
        make("a", params=["x.size", "y.size", "z.width"], before=0.0, after=0.9),
        make("b", params=["size"], before=0.0, after=0.9),
        make("c", params=["w.width", "size"], before=0.0, after=0.9),
    ]
    result = cluster(recs)
    assert result[0].common_params == ("size", "width")


def test_common_params_empty_when_no_param_is_common():
    recs = [
        make("a", params=["p.one"], before=0.0, after=0.9),
        make("b", params=["q.two"], before=0.0, after=0.9),
        make("c", params=["r.three"], before=0.0, after=0.9),
    ]
    assert cluster(recs)[0].common_params == ()


# --- propose_rules ----------------------------------------------------------

def test_propose_rules_empty():
    assert propose_rules([]) == "## Proposed guardrails\n\n_No recurring fixes yet._\n"


def test_propose_rules_exact_output_with_params():
    clusters = [
        FixCluster(
            defects=("hinge.loose", "lid.gap"),
            ops=("set_param", "set_transform"),
            count=3,
            mean_gain=0.4166,
            common_params=("size", "translate"),
            example_asset_ids=("crate_01", "crate_07", "chest_02"),
        ),
        FixCluster(
            defects=("handle.offset",),
            ops=("set_param",),
            count=5,
            mean_gain=0.31,
            common_params=(),
            example_asset_ids=("mug_1",),
        ),
    ]
    expected = (
        "## Proposed guardrails\n\n"
        "- When **hinge.loose, lid.gap** is flagged, the fix that worked 3 times (mean gain +0.42) "
        "was: set_param + set_transform on size, translate. "
        "Examples: crate_01, crate_07, chest_02.\n"
        "- When **handle.offset** is flagged, the fix that worked 5 times (mean gain +0.31) "
        "was: set_param on various parameters. "
        "Examples: mug_1.\n"
    )
    assert propose_rules(clusters) == expected


def test_propose_rules_single_cluster_exact_output():
    clusters = [
        FixCluster(
            defects=("d",),
            ops=("op",),
            count=3,
            mean_gain=0.5,
            common_params=("size",),
            example_asset_ids=("a",),
        ),
    ]
    assert propose_rules(clusters) == (
        "## Proposed guardrails\n\n"
        "- When **d** is flagged, the fix that worked 3 times (mean gain +0.50) "
        "was: op on size. Examples: a.\n"
    )


def test_propose_rules_from_real_clustering():
    recs = [make(f"k{i}", defects={"lid.gap": 0.9}, ops=["set_param"], params=["lid.size"], before=0.2, after=0.8)
            for i in range(3)]
    text = propose_rules(cluster(recs))
    assert text == (
        "## Proposed guardrails\n\n"
        "- When **lid.gap** is flagged, the fix that worked 3 times (mean gain +0.60) "
        "was: set_param on size. Examples: k0, k1, k2.\n"
    )
