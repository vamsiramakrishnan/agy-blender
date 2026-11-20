"""benchmarks/perf_engines.py: plumbing, graceful absence of engines, and the shape of its JSON.

The real Blender and Godot measurements run only in CI (job ``stock-engines``). Here the Blender side is the
pool's fake daemon, which lints each Blender script but does not execute it.
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "benchmarks" / "perf_engines.py"
OPS = ["uv_unwrap", "uv_unwrap_angle_based", "lod", "convex_hull", "sheet_4_views"]


def load_module():
    spec = importlib.util.spec_from_file_location("perf_engines_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def run_script(tmp_path, *args, env_extra=None, drop=("BLENDER_BIN", "GODOT_BIN")):
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env.update(env_extra or {})
    out = tmp_path / "bench.json"
    md = tmp_path / "bench.md"
    p = subprocess.run([sys.executable, str(SCRIPT), "--out", str(out), "--markdown", str(md), *args], capture_output=True, text=True, env=env, timeout=300)
    return p, out, md


def stdout_json(p):
    lines = [ln for ln in p.stdout.splitlines() if ln.startswith("ENGINE_BENCH_JSON ")]
    assert len(lines) == 1, p.stdout[-500:]
    return json.loads(lines[0].removeprefix("ENGINE_BENCH_JSON "))


def test_without_binaries_says_what_is_missing_and_exits_zero(tmp_path):
    p, out, md = run_script(tmp_path)
    assert p.returncode == 0, p.stderr[-800:]
    doc = json.loads(out.read_text())
    assert doc["mode"] == "unavailable" and doc["fatal"] is None
    assert any("BLENDER_BIN" in m for m in doc["missing"]) and any("GODOT_BIN" in m for m in doc["missing"])
    assert doc["tools"]["blender"]["available"] is False and doc["tools"]["godot"]["available"] is False
    assert "blender_pool" not in doc and "godot" not in doc  # nothing was measured, nothing is claimed
    assert doc["machine"]["cpu_model"] and doc["machine"]["cpu_count"]
    assert stdout_json(p)["mode"] == "unavailable"
    assert "Nothing measured" in md.read_text()


def test_binary_path_that_does_not_exist_is_reported_not_crashed(tmp_path):
    p, out, _ = run_script(tmp_path, env_extra={"BLENDER_BIN": str(tmp_path / "nope"), "GODOT_BIN": str(tmp_path / "nope2")})
    assert p.returncode == 0
    doc = json.loads(out.read_text())
    assert doc["mode"] == "unavailable"
    assert "is not an executable file" in doc["tools"]["blender"]["missing"]


@pytest.fixture(scope="module")
def fake_run(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("fake")
    p, out, md = run_script(tmp, "--fake", "--quick")
    return p, json.loads(out.read_text()), md.read_text()


def test_fake_run_exercises_every_measurement(fake_run):
    p, doc, md = fake_run
    assert p.returncode == 0, p.stderr[-800:]
    assert doc["mode"] == "fake" and doc["fatal"] is None and doc["errors"] == []
    bp = doc["blender_pool"]
    assert bp["cold_start_to_first_ping"]["status"] == "ok" and bp["cold_start_to_first_ping"]["n"] == 2
    assert bp["warm_ping"]["n"] == 30 and bp["version"]["fake"] is True
    assert sorted(bp["import_usd"]) == ["barrel", "crate", "sphere_ir"] and all(v["status"] == "ok" for v in bp["import_usd"].values())
    assert sorted(bp["render_usd"]) == ["cycles", "eevee", "workbench"]
    assert all(v["status"] == "ok" and v["image_shape"] == [512, 512, 4] for v in bp["render_usd"].values())
    meshes = [c["mesh"] for c in bp["comparisons"]]
    assert meshes[:2] == ["crate", "barrel"] and any(m.startswith("sphere_") for m in meshes)
    for c in bp["comparisons"]:
        assert sorted(c["ops"]) == sorted(OPS)
        for op, slot in c["ops"].items():
            assert slot["equivalence"] in doc["equivalence_notes"]
            assert slot["blender"]["status"] == "ok" and slot["blender"]["fake"] is True  # linted, not executed
        for op in ("uv_unwrap", "lod", "convex_hull", "sheet_4_views"):
            k = c["ops"][op]["kernel"]
            assert k["status"] == "ok" and k["median_ms"] > 0 and k["n"] >= 2
    uv = bp["comparisons"][0]["ops"]["uv_unwrap"]["kernel"]
    assert uv["islands"] >= 1 and uv["tris"] == bp["comparisons"][0]["tris"] and uv["chart_source"] == ["computed"]
    assert bp["comparisons"][-1]["ops"]["convex_hull"]["kernel"]["hull_vertices"] > 0
    assert "godot" not in doc  # the fake run never starts Godot
    assert set(doc["setup"]["packages"]) == {"crate", "barrel", "sphere_ir"} and all(p["ok"] for p in doc["setup"]["packages"].values())


def test_fake_run_json_on_stdout_equals_the_file_and_markdown_is_written(fake_run):
    p, doc, md = fake_run
    assert stdout_json(p) == json.loads(json.dumps(doc))
    assert "FAKE daemon" in md and "Blender vs kernel" in md and "| crate (144) | lod |" in md
    for key in ("schema", "mode", "ci", "machine", "versions", "tools", "missing", "errors", "total_wall_s", "fatal", "equivalence_notes"):
        assert key in doc
    assert doc["versions"]["antigravity3d_from"]


def test_one_unusable_binary_is_recorded_and_the_run_still_exits_zero(tmp_path):
    bad = tmp_path / "blender"
    bad.write_text("#!/bin/sh\nexit 1\n")
    bad.chmod(0o755)
    p, out, _ = run_script(tmp_path, "--quick", env_extra={"BLENDER_BIN": str(bad)})
    assert p.returncode == 0, p.stderr[-800:]
    doc = json.loads(out.read_text())
    assert doc["mode"] == "real" and doc["fatal"] is None
    assert doc["blender_pool"]["cold_start_to_first_ping"]["status"] == "error"
    assert "startup" in doc["blender_pool"]["cold_start_to_first_ping"]["error"]
    assert doc["tools"]["godot"]["available"] is False and "godot" not in doc
    assert any("cold_start_to_first_ping" in e for e in doc["errors"])


def test_benchmark_crash_keeps_the_partial_json_and_exits_nonzero(tmp_path, monkeypatch, capsys):
    mod = load_module()
    monkeypatch.setenv("BLENDER_BIN", sys.executable)  # exists and is executable; never started because setup raises first
    monkeypatch.delenv("GODOT_BIN", raising=False)

    def boom(*a, **k):
        raise RuntimeError("setup exploded")
    monkeypatch.setattr(mod, "build_subjects", boom)
    out = tmp_path / "o.json"
    assert mod.main(["--out", str(out), "--markdown", str(tmp_path / "o.md")]) == 1
    doc = json.loads(out.read_text())
    assert "setup exploded" in doc["fatal"] and doc["mode"] == "real"
    assert "crashed" in (tmp_path / "o.md").read_text()
    assert capsys.readouterr().out.count("ENGINE_BENCH_JSON ") == 1


def test_measurement_error_is_recorded_per_measurement(tmp_path):
    mod = load_module()
    run = mod.Run(100.0, True, True)
    store = {}
    run.attempt(store, "good", lambda: {"median_ms": 1.0})
    run.attempt(store, "bad", lambda: (_ for _ in ()).throw(ValueError("no such operator")))
    assert store["good"]["status"] == "ok"
    assert store["bad"] == {"status": "error", "error": "ValueError: no such operator", "wall_s": store["bad"]["wall_s"]}
    run.budget_s = -1.0
    run.attempt(store, "late", lambda: {"median_ms": 1.0})
    assert store["late"]["status"] == "skipped" and run.errors == ["bad: ValueError: no such operator"]


def test_every_blender_script_passes_the_production_linter():
    """The scripts go through the daemon's linter in CI, which this benchmark must not need to weaken."""
    from antigravity3d.adapters.blender_5_3 import linter

    mod = load_module()
    parts = [{"v": [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], "f": [[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]]}]
    sources = [mod.script(mod.UV_SCRIPT, parts, 2, OP="smart"), mod.script(mod.UV_SCRIPT, parts, 2, OP="angle"),
               mod.script(mod.DECIMATE_SCRIPT, parts, 2, TARGETS=[[2, 1]]), mod.script(mod.HULL_SCRIPT, parts, 2),
               mod.script(mod.SHEET_SCRIPT, parts, 2, OUT="/tmp/x/", SIZE=64)]
    for src in sources:
        assert "@@" not in src
        errors = [i for i in linter.lint_script(src) if i.severity == "error"]
        assert not errors, errors


def test_island_count_joins_faces_that_share_a_uv_corner():
    mod = load_module()
    a, b, c, d = (0, 0.0, 0.0), (1, 1.0, 0.0), (2, 0.0, 1.0), (3, 1.0, 1.0)
    assert mod.island_count([[a, b, c], [b, c, d]]) == 1
    assert mod.island_count([[a, b, c], [(1, 5.0, 5.0), (2, 5.0, 6.0), (3, 6.0, 6.0)]]) == 2
