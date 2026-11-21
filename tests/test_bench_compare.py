"""The before/after table tool: what it calls a change, and what it refuses to compare."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parent.parent / "benchmarks" / "compare.py"


@pytest.fixture(scope="module")
def compare():
    spec = importlib.util.spec_from_file_location("bench_compare_under_test", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(cases: dict[str, dict[str, float]], **machine) -> dict:
    """A result file: {benchmark: {case label: median_ms}}."""
    return {"machine": {"git_commit": "0" * 40, **machine}, "total_wall_s": 1.0,
            "benchmarks": {name: {"status": "ok", "cases": [{"params": {"case": k}, "median_ms": v} for k, v in by.items()]}
                           for name, by in cases.items()}}


def table(compare, before, after, **kw):
    b = compare.merged(before, kw.pop("rest", []))
    bands = compare.noise_bands(b, [r["benchmarks"] for r in kw.pop("repeats", [])], kw.get("noise", 0.10))
    rows, notes = compare.rows(b, after["benchmarks"], kw.get("noise", 0.10), bands)
    return {(r[0], r[1]): r[4] for r in rows}, notes


def test_a_difference_under_the_floor_is_noise_and_a_larger_one_is_named(compare):
    got, notes = table(compare, run({"k": {"a": 100.0, "b": 100.0, "c": 100.0}}), run({"k": {"a": 108.0, "b": 50.0, "c": 130.0}}))
    assert got == {("k", "case=a"): "within noise", ("k", "case=b"): "2.0x faster", ("k", "case=c"): "1.3x slower"}
    assert notes == []


def test_a_repeat_of_the_same_code_widens_the_band_for_that_case_only(compare):
    """Two runs of the pre-fix code 25% apart: a 20% difference in the after run is not a result for that case.
    The band is that spread times the floor: one repeat is a lower bound on the noise."""
    before, repeat = run({"k": {"jittery": 100.0, "steady": 100.0}}), run({"k": {"jittery": 125.0, "steady": 101.0}})
    after = run({"k": {"jittery": 120.0, "steady": 120.0}})
    assert table(compare, before, after)[0] == {("k", "case=jittery"): "1.2x slower", ("k", "case=steady"): "1.2x slower"}
    got, _ = table(compare, before, after, repeats=[repeat])
    assert got == {("k", "case=jittery"): "within noise", ("k", "case=steady"): "1.2x slower"}
    # the band is symmetric: a repeat that was faster by the same factor hides the same change
    got, _ = table(compare, before, after, repeats=[run({"k": {"jittery": 80.0, "steady": 100.0}})])
    assert got[("k", "case=jittery")] == "within noise"
    # and a change beyond the band is still reported
    got, _ = table(compare, before, run({"k": {"jittery": 135.0, "steady": 100.0}}), repeats=[repeat])
    assert got[("k", "case=jittery")] == "within noise"  # 1.35 is inside 1.25 x 1.10
    got, _ = table(compare, before, run({"k": {"jittery": 160.0, "steady": 100.0}}), repeats=[repeat])
    assert got[("k", "case=jittery")] == "1.6x slower"


def test_a_split_first_side_is_read_from_all_its_files_without_overriding_the_first(compare):
    before, rest = run({"k": {"a": 100.0}}), run({"k": {"a": 999.0}, "late": {"a": 10.0}})
    got, notes = table(compare, before, run({"k": {"a": 50.0}, "late": {"a": 20.0}}), rest=[rest])
    assert got == {("k", "case=a"): "2.0x faster", ("late", "case=a"): "2.0x slower"} and notes == []


def test_what_exists_on_one_side_only_is_listed_not_compared(compare):
    got, notes = table(compare, run({"k": {"a": 1.0, "gone": 1.0}, "old": {"a": 1.0}}), run({"k": {"a": 1.0, "new": 1.0}, "added": {"a": 1.0}}))
    assert set(got) == {("k", "case=a")}
    assert any("`old`: only in the first file" in n for n in notes) and any("`added`: only in the second file" in n for n in notes)
    assert any("case=gone" in n and "first" in n for n in notes) and any("case=new" in n and "second" in n for n in notes)


def test_command_line_prints_a_table_with_the_noise_band_when_repeats_are_given(compare, tmp_path, capsys):
    files = {}
    for name, data in (("before", run({"k": {"a": 100.0}}, timestamp_utc="t0")), ("after", run({"k": {"a": 40.0}})),
                       ("repeat", run({"k": {"a": 130.0}}))):
        files[name] = tmp_path / f"{name}.json"
        files[name].write_text(json.dumps(data))
    assert compare.main([str(files["before"]), str(files["after"]), "--repeat", str(files["repeat"])]) == 0
    out = capsys.readouterr().out
    assert "| Benchmark | Case | Before (median) | After (median) | Change | Noise band |" in out
    assert "| `k` | case=a | 100 ms | 40.0 ms | 2.5x faster | 43% |" in out  # 1.30 between the runs, times the floor
    assert compare.main([str(files["before"]), str(files["after"]), "--changed", "--noise", "2.0"]) == 0
    out = capsys.readouterr().out
    assert "`k`" not in out and "Noise band" not in out


def test_a_package_size_recorded_among_the_params_does_not_split_a_case(compare):
    """Older result files carry a package's byte size in ``params``. It is an outcome, so two trees that write
    packages of different sizes are still measuring the same case."""
    def doc(size: int, ms: float) -> dict:
        return {"machine": {}, "benchmarks": {"kernel.package.validate_cold": {"status": "ok", "cases": [
            {"params": {"subject": "crate", "tris": 144, "package_bytes": size}, "median_ms": ms}]}}}
    changes, notes = table(compare, doc(69_679_768, 100.0), doc(49_534_385, 50.0))
    assert list(changes.values()) == ["2.0x faster"] and not notes
