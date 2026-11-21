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
    assert bp["cold_start_to_first_ping"]["status"] == "ok" and bp["cold_start_to_first_ping"]["n"] == 3 and bp["cold_start_to_first_ping"]["includes"]
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
            assert slot["blender"]["status"] in ("ok", "no_levels")
            if slot["blender"]["status"] == "ok":
                assert slot["blender"]["fake"] is True  # linted, not executed
        for op in ("uv_unwrap", "lod", "convex_hull", "sheet_4_views"):
            k = c["ops"][op]["kernel"]
            assert k["status"] == "ok" and k["median_ms"] > 0 and k["n"] >= 2
    uv = bp["comparisons"][0]["ops"]["uv_unwrap"]["kernel"]
    pipe = bp["comparisons"][0]["ops"]["uv_unwrap"]["kernel_pipeline"]
    assert pipe["status"] == "ok" and pipe["chart_source"] == ["analytic"]  # primitives are unwrapped from exact charts in the pipeline
    assert bp["comparisons"][0]["ops"]["lod"]["blender"]["status"] == "no_levels"  # twelve 12-triangle boxes: no level below LOD0
    assert uv["islands"] >= 1 and uv["tris"] == bp["comparisons"][0]["tris"] and uv["chart_source"] == ["computed"]
    assert bp["comparisons"][-1]["ops"]["convex_hull"]["kernel"]["hull_vertices"] > 0
    assert "godot" not in doc  # the fake run never starts Godot
    assert set(doc["setup"]["packages"]) == {"crate", "barrel", "sphere_ir"} and all(p["ok"] for p in doc["setup"]["packages"].values())


def test_fake_run_json_on_stdout_equals_the_file_and_markdown_is_written(fake_run):
    p, doc, md = fake_run
    assert stdout_json(p) == json.loads(json.dumps(doc))
    assert "FAKE daemon" in md and "Blender vs kernel" in md and "| crate (144) | lod |" in md
    assert "no levels" in md and "kernel as the pipeline runs it" in md and "What differs between the two sides" in md and "NOT the same algorithm" in md
    assert "Blender call" in md and "first call" in md and "no GPU" in md
    for key in ("schema", "mode", "ci", "machine", "versions", "tools", "missing", "errors", "total_wall_s", "fatal", "equivalence_notes", "upstream_checks"):
        assert key in doc
    assert doc["versions"]["antigravity3d_from"] and doc["schema"] == 2
    assert "Upstream checks survey" in md and "FAKE worker" in md and "Planted defects" in md and "Kit pieces" in md


def test_fake_run_surveys_every_subject_file_and_kit_piece(fake_run):
    _, doc, _ = fake_run
    u = doc["upstream_checks"]
    assert u["status"] == "ok" and u["upstream"]["commit"] == "9e7e04a603c125a0d17447ab97112fa70a865067"
    assert set(u["upstream"]["scripts"]) == {"mesh_stats", "qa_check", "kit_check", "texel_density"}
    assert all(len(v["sha256"]) == 64 for v in u["upstream"]["scripts"].values()) and u["upstream"]["source"]["kind"] == "fake"
    assert u["engines_run"] == ["unreal_5", "roblox_r15"] and u["engines_not_run"] == {"unity_urp": "--quick", "godot_4": "--quick"}
    assert set(u["mesh_stats_budget_for_engine"]) == {"unreal_5", "unity_urp", "godot_4", "roblox_r15"} and u["qa_profile_for_engine"]["roblox_r15"] == "roblox"
    assert set(u["props"]) == {"crate", "barrel", "sphere_ir"}
    for name, sub in u["props"].items():
        files = sub["files"]
        assert {"usd:unreal_5", "usd:roblox_r15"} <= set(files) and any(f.startswith("glb:") for f in files), name
        for f, fe in files.items():
            assert {"qa_check", "qa_check_welded", "texel_density", "texel_density_welded", "mesh_stats"} == set(fe["calls"]), (name, f)
            assert all(c["status"] == "ok" and c["result"]["fake"] and c["wall_s"] >= 0 and c["tris"] > 0 for c in fe["calls"].values())
            assert fe["calls"]["qa_check_welded"]["weld"] is True and fe["calls"]["qa_check"]["weld"] is False
            assert fe["project"]["tier1_qa"]["ok"] is True and fe["side_by_side"] and "kinds" in fe
        assert files["usd:roblox_r15"]["calls"]["qa_check"]["kwargs"] == {"profile": "roblox"}
        assert files["usd:roblox_r15"]["topology"]["up_axis"] == "Y" and files["usd:unreal_5"]["topology"]["up_axis"] == "Z"
    crate_body = u["props"]["crate"]["files"]["usd:unreal_5"]["topology"]["meshes"]["body"]
    assert (crate_body["points"], crate_body["components"]) == (24, 6)  # seam-split as written: one component per box face
    assert set(u["defects"]) == {"zfight", "back_to_back", "floating", "inverted", "no_uv"}
    assert u["defects"]["inverted"]["project_verdict"] == "found" and u["defects"]["floating"]["project_verdict"] == "found"
    assert set(u["defects"]["no_uv"]["files"]) == {"stage_usd"} and set(u["defects"]["zfight"]["files"]) == {"usd", "glb"}
    for d in u["defects"].values():
        assert d["expected"]["upstream_should"] and all(v is not None for fe in d["files"].values() for v in fe["verdict"].values())
    kits = u["kit"]["kits"]
    assert set(kits) == {"unreal_5", "godot_4"} and u["kit"]["convention"]["stated"] is None
    for k in kits.values():
        assert k["status"] == "ok" and len(k["pieces"]) == 4
        for pe in k["pieces"].values():
            assert set(pe["calls"]) == {"kit_check_upstream_defaults", "kit_check_upstream_defaults_welded", "kit_check_template_origin",
                                        "kit_check_template_origin_welded"}
            assert pe["calls"]["kit_check_template_origin"]["kwargs"] == {"grid": 0.5, "pivot": "bottom-center", "texture": 1024}
            assert pe["project"]["parts"] and pe["calls"]["kit_check_upstream_defaults"]["tris"] > 0
    assert u["cost"]["calls"] == len(u["cost"]["per_call"]) > 50


class _Wrapped:
    """The fake pool with one primitive failing, or with no primitives at all."""

    def __init__(self, pool, fail=None, no_primitives=False):
        self.pool, self.fail, self.no_primitives = pool, fail, no_primitives

    async def call(self, op, **kw):
        if op == "version" and self.no_primitives:
            return {**(await self.pool.call("version")), "primitives": [], "primitives_error": "ValueError: unexpected blender-skills source digest"}
        if op == "inspect_usd" and kw.get("primitive") == self.fail:
            raise RuntimeError(f"{self.fail} exploded")
        return await self.pool.call(op, **kw)


def _survey(tmp_path, *, budget_s=600.0, **wrap):
    import asyncio

    from antigravity3d.adapters.blender_5_3.pool import BlenderPool

    mod = load_module()
    import upstream_survey  # benchmarks/ is on sys.path once perf_engines is loaded

    run = mod.Run(budget_s, True, True)
    scenes = {k: v for k, v in mod.prop_scenes().items() if k == "crate"}

    async def go():
        async with BlenderPool(1, fake=True) as pool:
            await upstream_survey.survey_section(run, _Wrapped(pool, **wrap), scenes, tmp_path)
    asyncio.run(go())
    return run, upstream_survey


def test_survey_records_a_failing_call_and_goes_on(tmp_path):
    run, survey = _survey(tmp_path, fail="texel_density")
    u = run.doc["upstream_checks"]
    assert u["status"] == "ok"
    calls = u["props"]["crate"]["files"]["usd:unreal_5"]["calls"]
    assert calls["texel_density"]["status"] == "error" and "texel_density exploded" in calls["texel_density"]["error"]
    assert calls["qa_check"]["status"] == "ok" and calls["mesh_stats"]["status"] == "ok"
    assert any(e.startswith("upstream_checks.props.crate.unreal_5.usd.texel_density:") for e in run.errors)
    assert all(pe["calls"]["kit_check_upstream_defaults"]["status"] == "ok" for pe in u["kit"]["kits"]["unreal_5"]["pieces"].values())
    md = "\n".join(survey.markdown(run.doc))
    assert "| crate | usd:unreal_5 |" in md and "outcomes:" in md and "'error'" in md


def test_survey_is_skipped_with_the_reason_when_the_worker_has_no_primitives(tmp_path):
    run, survey = _survey(tmp_path, no_primitives=True)
    u = run.doc["upstream_checks"]
    assert u["status"] == "skipped" and "unexpected blender-skills source digest" in u["reason"] and "props" not in u
    assert "Survey skipped" in "\n".join(survey.markdown(run.doc))


def test_survey_respects_the_time_budget(tmp_path):
    run, _ = _survey(tmp_path, budget_s=-1.0)
    u = run.doc["upstream_checks"]
    assert u["engines_run"] == [] and set(u["engines_not_run"]) == {"unreal_5", "unity_urp", "godot_4", "roblox_r15"}
    assert all(c["status"] == "skipped" for fe in u["defects"]["zfight"]["files"].values() for c in fe["calls"].values())
    assert all(k["status"] == "skipped" for k in u["kit"]["kits"].values()) and u["cost"]["calls"] == 0


def test_upstream_messages_are_classified_by_kind():
    load_module()
    import upstream_survey as s

    samples = {  # wording of qa_check.py / kit_check.py at the pinned commit
        "z-fighting: A<->B (3 overlapping coplanar tri pairs)": "zfighting",
        "hidden back-to-back faces: A<->B (2 pairs) - delete to save tris": "back_to_back",
        "floating/detached part in A (8 verts at [0, 0, 1])": "floating",
        "A: 1 closed parts with normals pointing inward": "inverted",
        "A: 4 edges with inconsistent face winding": "inconsistent_winding",
        "A: duplicate verts=12 loose verts=0": "duplicate_verts", "A: unapplied scale [1.0, 2.0, 1.0]": "unapplied_scale",
        "A: no UV map": "no_uv", "no UVs": "no_uv", "A: 24 open boundary edges (not watertight)": "open_boundary",
        "400000 tris > max_tris 300000: z-fighting/floating checks skipped": "checks_skipped",
        "asset lowest point z=0.2 (not resting on ground plane z=0)": "not_grounded",
        "unapplied rotation 90.0 deg": "unapplied_rotation", "pivot is not at the corner: offset [-1, -1, 0] m (--fix)": "kit_pivot",
        "12 open-edge verts inside the piece (holes; seams must lie on its outer faces)": "kit_inner_open_edges",
        "name lacks 'SM_' prefix": "kit_name_prefix", "A: 25000 tris > roblox limit 20000": "tri_limit",
    }
    for msg, kind in samples.items():
        assert s.classify(msg) == kind, msg
    assert s.outcome("qa_check", {"warnings": ["400000 tris > max_tris 300000: z-fighting/floating checks skipped"], "pass": True}) == "checks_skipped"


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
    assert any(e.startswith("blender_pool.cold_start_to_first_ping:") for e in doc["errors"])


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

    class OpError(Exception):
        trace_tail = "line 1\nline 2"
    run.attempt(store, "op", lambda: (_ for _ in ()).throw(OpError("boom")), path="comparisons.barrel.lod.blender")
    assert store["op"]["trace_tail"] == "line 1\nline 2"
    assert run.errors[-1] == "comparisons.barrel.lod.blender: OpError: boom"
    run.errors.pop()
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


def test_blender_scripts_never_read_the_layer_object_list_or_remove_objects():
    """A removed object leaves a stale entry in the view layer until Blender syncs it; scripts must not depend on that."""
    mod = load_module()
    for body in (mod.UV_SCRIPT, mod.DECIMATE_SCRIPT, mod.HULL_SCRIPT, mod.SHEET_SCRIPT, mod.PRELUDE):
        assert "view_layer.objects" not in body.replace("view_layer.objects.active = ob", "")
        assert "objects.remove" not in body and "select_set" not in body


def test_godot_baseline_launch_mirrors_the_validator():
    import inspect

    mod = load_module()
    src = inspect.getsource(mod.godot_section)
    assert '"--headless", "--path", td, "--script", "check.gd"' in src and "--quit" not in src


def test_sheet_script_copies_the_kernel_fit_rule():
    mod = load_module()
    assert "FILL = 0.88" in mod.SHEET_SCRIPT and "0.55, 0.70, -0.45" in mod.SHEET_SCRIPT and "math.radians(15.0)" in mod.SHEET_SCRIPT
