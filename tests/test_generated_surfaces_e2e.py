"""The default no-image path end to end: `agy3d prop --offline --max-images 0` with generated surfaces (the default for a
real run; the suite's conftest turns the default off, so this file asks for it with --surfaces generated).

What a real package goes through: the package validator, the comparison scorecard's map, UV and maps rules on the GLB,
a rerun in the same folder giving the same bytes, an edit by patch that re-bakes the atlas, and (with the engines) the
three.js check, the Godot import and `agy3d install` into a fresh Godot project with every check.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

import pytest

from antigravity3d import cli

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))

CRATE = "a rugged military supply crate kit-bashed from panels, corner guards, latches and two side handles"
ARGS = ["--offline", "--engine", "godot_4", "--max-images", "0", "--token-ceiling", "2000000", "--surfaces", "generated"]
BIG_BRACKETS = {"ops": [{"op": "set_param", "node_id": f"bracket_{i}", "name": "size", "value": [0.1396, 0.1396, 0.03]}
                        for i in (0, 1)]}


def _package(out: Path) -> Path:
    from antigravity3d.dags import edit_asset as E

    pkg, _, _ = E.find_package(out)
    return pkg


def _files(pkg: Path) -> dict[str, bytes]:
    return {str(p.relative_to(pkg)): p.read_bytes() for p in sorted(pkg.rglob("*")) if p.is_file()}


@pytest.fixture(scope="module")
def run(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("generated") / "out"
    assert cli.main(["prop", CRATE, *ARGS, "--out", str(out)]) == 0
    return out


@pytest.fixture(scope="module")
def manifest(run) -> dict:
    return json.loads((_package(run) / "manifest.json").read_text())


def test_every_image_less_material_ships_a_generated_surface(manifest) -> None:
    assert manifest["budget"]["generated_surfaces"] is True
    ta = manifest["texture_atlas"]
    assert ta["status"] == "baked" and {"albedo", "orm", "normal"} <= set(ta["files"])
    for name, m in ta["members"].items():
        assert m["sources"] == {"albedo": "smart", "orm": "smart", "normal": "smart"}, (name, m)
        g = m["generated"]
        assert g["class"] in ("painted_metal", "painted_wood", "bare_metal", "cast_metal", "brass", "rubber", "polymer",
                              "wood", "fabric", "glass", "plain"), g
        assert g["reason"] and "timings_ms" not in g      # what was decided, not how long it took
        for d in g["markings"]:
            assert {"kind", "material", "placed"} <= set(d)


def test_the_package_passes_the_validator_and_the_scorecard_rules(run) -> None:
    import compare_eval as ev

    from antigravity3d.kernel.validator import validate_package

    pkg = _package(run)
    assert not [i for i in validate_package(pkg) if i.severity == "critical"]
    m = json.loads((pkg / "manifest.json").read_text())
    facts = ev.glb_facts(pkg / f"{m['asset']}.glb", max_tris=m["budget"]["max_tris"])
    checks = ev._cell_checks({"facts": facts}, None)
    assert checks["maps"] is True, [(t["roles"], t.get("flat"), t.get("frac_below_30_srgb")) for t in facts["images"]]
    assert checks["uv"] is True and checks["tri_budget"] is True


def test_a_rerun_in_the_same_folder_gives_the_same_bytes(run, tmp_path) -> None:
    again = tmp_path / "out"
    shutil.copytree(run, again)
    assert cli.main(["prop", CRATE, *ARGS, "--out", str(again)]) == 0
    a, b = _files(_package(run)), _files(_package(again))
    assert set(a) == set(b)
    assert sorted(k for k in a if a[k] != b[k]) == []


def test_an_edit_re_bakes_the_generated_atlas(run, tmp_path) -> None:
    from antigravity3d.kernel.validator import validate_package

    out = tmp_path / "out"
    shutil.copytree(run, out)
    before = _package(out)
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps(BIG_BRACKETS))
    assert cli.main(["edit", str(out), "make the two rear corner brackets 30 % larger", "--patch", str(ops), "--offline"]) == 0
    after = _package(out)
    assert after != before
    m = json.loads((after / "manifest.json").read_text())
    assert all(v["sources"].get("albedo") == "smart" for v in m["texture_atlas"]["members"].values())
    assert not [i for i in validate_package(after) if i.severity == "critical"]


def test_flat_is_one_setting_away(tmp_path) -> None:
    out = tmp_path / "out"
    assert cli.main(["prop", CRATE, *[a if a != "generated" else "flat" for a in ARGS], "--out", str(out)]) == 0
    m = json.loads((_package(out) / "manifest.json").read_text())
    assert m["budget"]["generated_surfaces"] is False
    assert not any(v.get("sources") for v in (m.get("texture_atlas") or {}).get("members", {}).values())


# --------------------------------------------------------------------------- engines


@pytest.mark.browser
def test_three_js_loads_the_generated_package(run) -> None:
    from antigravity3d.adapters.engines import web_three as W

    rep = asyncio.run(W.WebThreeValidator().validate(_package(run), 120))
    if rep.skipped:
        pytest.skip(rep.skipped)
    assert rep.ok, [c for c in rep.checks if c.status == "fail"]


def test_godot_imports_and_installs_the_generated_package(run, tmp_path) -> None:
    from antigravity3d.adapters.engines.validators import GodotValidator
    from antigravity3d.deliver import godot as G
    from antigravity3d.deliver.install import install_package

    if G.find_godot() is None:
        pytest.skip("set GODOT_BIN to run stock Godot")
    pkg = _package(run)
    asset = json.loads((pkg / "manifest.json").read_text())["asset"]
    rep = asyncio.run(GodotValidator().validate(pkg / f"{asset}.glb"))
    assert rep.ok, rep.log_tail
    project = tmp_path / "game"
    project.mkdir()
    (project / "project.godot").write_text('config_version=5\n\n[application]\n\nconfig/name="t"\n'
                                           'config/features=PackedStringArray("4.7", "Forward Plus")\n')
    (project / "main.tscn").write_text('[gd_scene format=3]\n[node name="Main" type="Node3D"]\n')
    r = install_package(pkg, project)
    checks = {c.name: (c.status, c.detail) for c in r.checks}
    assert r.status == "installed" and r.ok, checks
    assert checks["materials"][0] == "passed", checks
