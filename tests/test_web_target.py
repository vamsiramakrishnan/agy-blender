"""web_three as a package target (S/r7/CONTRACT_WEB.md section 2): its rules and where each value comes from, every place a
target is listed, the delivery of a script asset for it offline, and the three.js check as its engine check (a failing
check blocks delivery, a check that could not run says so and is never a pass, the page is loaded once per package).
Nothing here needs a browser except the tests marked ``browser``."""

from __future__ import annotations

import importlib.util
import io
import json
import re
from pathlib import Path
from typing import get_args

import pytest
from PIL import Image
from test_run_build import build, crate, write, ws  # noqa: F401 - ws is a fixture

from antigravity3d import cli
from antigravity3d.adapters.engines import validators, web_three
from antigravity3d.adapters.engines.web_three import NOT_RUN, WebThreeReport, WebThreeValidator
from antigravity3d.build import asset as asset_mod
from antigravity3d.deliver import detect
from antigravity3d.harness import tools as T
from antigravity3d.harness.session import session
from antigravity3d.kernel import fast_ops, package, validator
from antigravity3d.runtime import mcp_server as M
from antigravity3d.state import TargetEngine

ROOT = Path(__file__).resolve().parents[1]
RULES = validator.ENGINE_RULES["web_three"]


# --------------------------------------------------------------------------- the rules


def test_the_rules_are_the_glb_and_the_browsers_not_another_engines_row():
    assert (RULES.native_up_axis, RULES.meters_per_unit) == ("Y", (1.0,))
    assert RULES.max_texture_px == 4096 and not RULES.require_pow2_textures
    # no number is invented where no source was found
    assert RULES.max_tris_per_mesh is None and RULES.max_textures_per_material is None and RULES.hull_limit is None
    assert not RULES.closed_render_meshes and not RULES.one_material_per_part
    assert package.NORMAL_CONVENTION["web_three"] == "opengl"
    assert "web_three" not in validator.FBX_PRESET  # no FBX, as for Godot


@pytest.mark.parametrize("name", ["Batch_0", "crate_body", "Lid2", "ünï_part", "a-b", "x+y"])
def test_a_node_name_three_keeps_is_accepted(name):
    assert re.match(RULES.name_regex, name) and _sanitize_node_name(name) == name


@pytest.mark.parametrize("name", ["a.b", "a b", "a:b", "a/b", "a[0]", "a]", "a\tb"])
def test_a_node_name_three_would_rewrite_is_refused(name):
    assert not re.match(RULES.name_regex, name) and _sanitize_node_name(name) != name


def _sanitize_node_name(name: str) -> str:
    """three.js PropertyBinding.sanitizeNodeName (src/animation/PropertyBinding.js), which GLTFLoader applies to node names."""
    return re.sub(r"[\[\].:/]", "", re.sub(r"\s", "_", name))


def test_the_texture_cap_reaches_the_generated_maps_and_the_atlas():
    assert validator.engine_texture_px("web_three", 8192) == 4096 and validator.engine_texture_px("web_three", 1024) == 1024
    # round eight: with no size named a web atlas stops at the web default (EngineRules.default_texture_px); 4096 is asked for
    assert max(max(wh) for wh in fast_ops.atlas_sizes("web_three")) == 2048
    assert max(max(wh) for wh in fast_ops.atlas_sizes("web_three", 4096)) == 4096


def test_a_part_with_a_dotted_name_is_a_critical_compliance_issue():
    from antigravity3d.kernel.types import MeshData
    import numpy as np

    from antigravity3d.state import EngineBudget

    tri = np.array([[0, 1, 2]])
    pts = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], float)
    meshes = [MeshData("lid.001", "/a/lid.001", pts, tri, "m"), MeshData("lid", "/a/lid", pts, tri, "m")]
    issues = validator.check_engine_compliance(None, meshes, EngineBudget("web_three"))
    assert [(i.code, i.node_id) for i in issues if i.severity == "critical"] == [("web_three:bad_name", "lid.001")]


# --------------------------------------------------------------------------- every place a target is listed


def test_every_switch_site_lists_the_target():
    assert "web_three" in get_args(TargetEngine) and "web_three" in cli.ENGINES
    assert asset_mod.ENGINES == get_args(TargetEngine) and M.ENGINES == get_args(TargetEngine)
    assert detect.TARGET_OF["threejs"] == "web_three"
    spec = importlib.util.spec_from_file_location("session_start_hook", ROOT / "plugin/antigravity-3d/hooks/scripts/session_start.py")
    hook = importlib.util.module_from_spec(spec)
    import sys

    sys.path.insert(0, str(ROOT / "plugin/antigravity-3d/hooks/scripts"))
    try:
        spec.loader.exec_module(hook)
    finally:
        sys.path.remove(str(ROOT / "plugin/antigravity-3d/hooks/scripts"))
    assert set(hook.ENGINE_NOTES) == set(get_args(TargetEngine))
    skill = (ROOT / "src/antigravity3d/build/plugin_script_mode/SKILL.md").read_text()
    assert all(f"`{e}`" in skill for e in get_args(TargetEngine))


@pytest.mark.parametrize("path", ["plugin/antigravity-3d/rules/engine-budgets.md", "src/antigravity3d/build/plugin_script_mode/engine-budgets.md"])
def test_the_budget_table_has_a_web_column_in_every_row(path):
    rows = [ln for ln in (ROOT / path).read_text().splitlines() if ln.startswith("|")]
    assert rows[0].split("|")[-2].strip() == "three.js (web)"
    assert {ln.count("|") for ln in rows} == {rows[0].count("|")}, "every row of the table has a cell for every engine"


def test_the_engine_check_of_the_target_is_the_three_js_check():
    assert isinstance(validators.validator_for("web_three"), WebThreeValidator)
    assert not isinstance(validators.validator_for("godot_4"), WebThreeValidator)


# --------------------------------------------------------------------------- one browser run per package state


def _glb(texture: str | None = "textures/a.png", pad: bytes = b"") -> bytes:
    """A GLB with only a JSON chunk: enough for the page's staging to see which texture a level file references."""
    doc = {"asset": {"version": "2.0"}, "images": [{"uri": texture}] if texture else []}
    body = json.dumps(doc).encode() + pad
    body += b" " * (-len(body) % 4)
    return b"glTF" + (2).to_bytes(4, "little") + (20 + len(body)).to_bytes(4, "little") + len(body).to_bytes(4, "little") + b"JSON" + body


def _package_dir(tmp_path: Path) -> Path:
    d = tmp_path / "pkg"
    (d / "textures").mkdir(parents=True, exist_ok=True)
    (d / "Thing.glb").write_bytes(_glb(pad=b" "))
    (d / "Thing_LOD1.glb").write_bytes(_glb(None, pad=b" "))
    (d / "Thing.usd").write_bytes(b"usd-one")
    (d / "textures" / "a.png").write_bytes(b"png-one")
    (d / "manifest.json").write_text(json.dumps({"asset": "Thing", "tris": 12}))
    return d


def _frame() -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (8, 8), (10, 20, 30, 255)).save(buf, "PNG")
    return buf.getvalue()


def _canned(monkeypatch, calls: list, report=None):
    async def _validate(self, package_path, timeout=0, **kw):
        calls.append(kw)
        if report is not None:
            return report
        _, man_path, _ = web_three._find_package(Path(package_path))  # what the browser would have read back from the package
        man = json.loads(man_path.read_text())
        return WebThreeReport(True, "web_three", "", metrics={"triangles": man["tris"]}, bounds_m=man.get("glb_bounds_m"),
                              frame_png=_frame(), checks=(web_three.Check("c", "pass", ""),), silhouette_iou=0.9991234,
                              screenshot="/gone/web_three.png", page="/gone/page.html")

    monkeypatch.setattr(WebThreeValidator, "_validate", _validate)


async def test_validate_then_capture_of_the_same_files_loads_the_page_once(tmp_path, monkeypatch):
    calls: list = []
    _canned(monkeypatch, calls)
    pkg, v = _package_dir(tmp_path), WebThreeValidator()
    first = await v.validate(pkg / "Thing.glb")
    shot = tmp_path / "engine.png"
    second = await v.capture(pkg / "Thing.glb", shot)
    third = await v.validate(pkg)  # the same bytes by the directory name
    assert len(calls) == 1 and first.ok and second.ok and third.ok and shot.read_bytes() == _frame()
    # a hit is a copy that carries no path into a directory that may be gone
    assert second is not first and third is not second and third.screenshot is None and third.page is None


def _touch_stamp_kept(path: Path, data: bytes) -> None:
    """What ``cp -p`` and ``rsync -a`` leave: new bytes under the old modification time."""
    st = path.stat()
    path.write_bytes(data)
    import os

    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


@pytest.mark.parametrize("what", ["glb", "level_glb", "texture", "unreferenced_texture", "manifest", "usd"])
async def test_a_changed_input_of_the_check_is_a_new_run_even_with_the_old_stamp(what, tmp_path, monkeypatch):
    """Every file a run reads is in the cache key, by content: the same size and the same mtime do not make an old result current."""
    calls: list = []
    _canned(monkeypatch, calls)
    pkg, v = _package_dir(tmp_path), WebThreeValidator()
    await v.validate(pkg)
    (pkg / "textures" / "b.png").write_bytes(b"other-one")
    await v.validate(pkg)  # a new file in textures/ is a change too
    assert len(calls) == 2
    target = {"glb": pkg / "Thing.glb", "level_glb": pkg / "Thing_LOD1.glb", "texture": pkg / "textures" / "a.png",
              "unreferenced_texture": pkg / "textures" / "b.png", "manifest": pkg / "manifest.json", "usd": pkg / "Thing.usd"}[what]
    old = target.read_bytes()
    # new bytes of the same length: a GLB's trailing JSON padding becomes another whitespace, the others change a byte
    new = {"manifest": old.replace(b'"tris": 12', b'"tris": 13')}.get(what, old[:-1] + (b"\n" if what.endswith("glb") else b"X"))
    _touch_stamp_kept(target, new)
    assert target.read_bytes() != old and target.stat().st_size == len(old)
    await v.validate(pkg)
    assert len(calls) == 3, f"a changed {what} was answered from an earlier run"
    await v.validate(pkg)
    assert len(calls) == 3  # and the new state is then kept


async def test_only_the_default_rule_and_reference_without_an_out_dir_use_the_cache(tmp_path, monkeypatch):
    calls: list = []
    _canned(monkeypatch, calls)
    pkg, v = _package_dir(tmp_path), WebThreeValidator()
    await v.validate(pkg)
    await v.validate(pkg, reference=_frame())  # an explicit reference is another question
    await v.validate(pkg, rule=web_three.PassRule(min_silhouette_iou=0.5))  # so is another pass rule
    await v.validate(pkg, out_dir=tmp_path / "out")  # and a run that writes files is a real one
    assert len(calls) == 4
    await v.validate(pkg)  # the default-rule run of the first call is still what a plain call gets
    assert len(calls) == 4


async def test_a_run_that_did_not_run_is_not_kept(tmp_path, monkeypatch):
    calls: list = []
    skipped = WebThreeValidator()._skipped_report("no browser")
    _canned(monkeypatch, calls, skipped)
    pkg, v = _package_dir(tmp_path), WebThreeValidator()
    await v.validate(pkg)
    await v.validate(pkg)
    assert len(calls) == 2 and skipped.skipped_reason == "no browser"


def test_the_skip_text_says_not_run_only_for_the_targets_own_check():
    assert WebThreeValidator(engine_check=True)._skipped_report("no browser").skipped_reason == f"{NOT_RUN}: no browser"
    assert WebThreeValidator()._skipped_report("no browser").skipped_reason == "no browser"
    once = WebThreeValidator(engine_check=True)._skipped_report(f"{NOT_RUN}: no browser").skipped_reason
    assert once == f"{NOT_RUN}: no browser"  # the words are not stacked
    assert validators.validator_for("web_three").engine_check


def test_the_parity_threshold_is_the_stricter_of_the_generic_one_and_the_checks_own(monkeypatch):
    from dataclasses import replace

    v = validators.validator_for("web_three")
    assert v.min_silhouette_iou == max(validators.MIN_SILHOUETTE_IOU, web_three.PASS_RULE.min_silhouette_iou) == 0.98
    monkeypatch.setattr(web_three, "PASS_RULE", replace(web_three.PASS_RULE, min_silhouette_iou=0.99))
    assert v.min_silhouette_iou == 0.99
    assert validators.validator_for("godot_4").min_silhouette_iou == validators.MIN_SILHOUETTE_IOU


# --------------------------------------------------------------------------- delivery of a script asset, offline


async def _deliver(ws, engine: str):
    host = M.StudioHost(script=True)
    await build(host, ws, write(ws, crate(engine=engine)), preview=True, token_ceiling=100_000, max_images=0)
    reply = await host.call_tool("deliver_package", T.deliver_package, "", {"dest": "output"})
    pkg = session(host.registry, host.active).last_export
    return reply, pkg, (json.loads((pkg / "manifest.json").read_text()) if pkg else None)


async def test_a_script_asset_is_delivered_for_the_web_and_says_what_a_page_downloads(ws, monkeypatch):
    calls: list = []
    _canned(monkeypatch, calls)
    monkeypatch.setattr(WebThreeValidator, "_unavailable", lambda self: None)
    reply, pkg, man = await _deliver(ws, "web_three")
    assert reply.startswith("DELIVERED"), reply
    assert man["target_engine"] == "web_three" and "fbx" not in man and not list(pkg.glob("*.fbx"))
    dl = man["web_download"]
    glb = pkg / f"{man['asset']}.glb"
    assert dl["level"] == "LOD0" and dl["files"] == {glb.name: glb.stat().st_size} and dl["bytes"] == glb.stat().st_size
    # LOD0 embeds its images: the PNGs beside it are not part of what a page fetches for it
    assert [r["storage"] for r in man["glb_images"][glb.name]] == ["embedded"] * len(man["glb_images"][glb.name])
    rec = man["validation"]["engine_import"]
    assert rec["ok"] and rec["engine"] == "web_three" and rec["checks_passed"] == 1 and rec["silhouette_iou"] == 0.9991
    # round eight: the download is stated with what chooses a size (size_lines), and the answer ends with the install call
    assert "three.js check passed: 1 of 1 checks" in reply and f"a page downloads {dl['bytes']:,} bytes for the full-detail model" in reply
    assert "stay sharp up to about" in reply
    assert reply.splitlines()[-1].startswith(f'NEXT: to use it in a three.js project, install_into_project(package_dir="{pkg}", '
                                             'dry_run=false) gives a loader module'), reply
    assert len(calls) == 1, "the preflight's run is the delivery's run"


async def test_a_failing_three_js_check_blocks_delivery_for_this_target(ws, monkeypatch):
    monkeypatch.setattr(WebThreeValidator, "_unavailable", lambda self: None)
    bad = WebThreeReport(False, "web_three", "FAIL\n[FAIL   ] no loader/console/page errors: boom",
                         checks=(web_three.Check("no loader/console/page errors", "fail", "boom"),))
    _canned(monkeypatch, [], bad)
    reply, pkg, _ = await _deliver(ws, "web_three")
    assert reply.startswith("NOT DELIVERED") and "web_three import" in reply and "boom" in reply and pkg is None


async def test_a_three_js_check_that_could_not_run_ships_the_package_and_says_so(ws, monkeypatch):
    monkeypatch.setattr(WebThreeValidator, "_unavailable", lambda self: "web_three browser not found (set $CHROMIUM_BIN)")
    reply, pkg, man = await _deliver(ws, "web_three")
    assert reply.startswith("DELIVERED"), reply
    rec = man["validation"]["engine_import"]
    assert rec["ok"] is None and rec["skipped"] == f"{NOT_RUN}: web_three browser not found (set $CHROMIUM_BIN)"
    assert f"{NOT_RUN}: web_three browser not found" in reply and "three.js check passed" not in reply
    assert "was validated and checked (the engine import was not run: three.js check NOT run" in reply and "imported" not in reply.split("FINISHED")[1]
    assert not man.get("verified") and man["web_download"]["bytes"] > 0  # a number the page needs, with or without the browser


async def test_other_targets_are_delivered_as_before_with_no_web_facts(ws):
    reply, _pkg, man = await _deliver(ws, "godot_4")
    assert reply.startswith("DELIVERED") and "three.js" not in reply and "web_download" not in man
    assert "checks_passed" not in man["validation"].get("engine_import", {})


def test_reply_lines_for_each_state():
    base = {"target_engine": "web_three", "web_download": {"level": "LOD0", "bytes": 2_200_000, "files": {"a.glb": 2_200_000}}}
    dl = "page download for LOD0: 2,200,000 bytes (a.glb 2,200,000); the model has no texture maps"

    def step(status, detail):
        return {"evidence": {"steps": [{"name": "engine_import", "status": status, "detail": detail}]}}

    ran = {**base, "validation": {"engine_import": {"ok": True, "checks_passed": 23, "checks": 23, "silhouette_iou": 0.9986}}}
    assert web_three.reply_lines(ran) == ["three.js check passed: 23 of 23 checks, silhouette IoU 0.9986", dl]
    # a check that ran and failed is FAILED with its reason, never "NOT run" (the pipeline writes no record for it)
    assert web_three.reply_lines({**base, **step("failed", "no loader errors: boom")})[0] == "three.js check FAILED: no loader errors: boom"
    # "passed" needs ok to be True, whatever else the record holds
    contradiction = {**base, "validation": {"engine_import": {"ok": False, "checks_passed": 22, "checks": 23}}, **step("failed", "x: y")}
    assert web_three.reply_lines(contradiction)[0] == "three.js check FAILED: x: y"
    assert web_three.reply_lines({**base, "validation": {"engine_import": {"checks_passed": 3, "checks": 3}}})[0] == f"{NOT_RUN}: no result is recorded"
    # not run: the record's reason (tools), the step's (pipeline), or neither
    assert web_three.reply_lines({**base, "validation": {"engine_import": {"ok": None, "skipped": f"{NOT_RUN}: no browser"}}})[0] == f"{NOT_RUN}: no browser"
    assert web_three.reply_lines({**base, **step("skipped", f"{NOT_RUN}: no browser")})[0] == f"{NOT_RUN}: no browser"
    assert web_three.reply_lines({**base, "validation": {}})[0] == f"{NOT_RUN}: no result is recorded"
    assert web_three.reply_lines({"target_engine": "godot_4", **step("failed", "x")}) == []
    assert web_three.reply_lines({**base, **step("failed", "x")})[1] == dl


def test_a_failing_check_on_the_command_path_is_reported_as_failed_not_as_not_run(tmp_path, monkeypatch, capsys):
    from antigravity3d import cli

    monkeypatch.setenv("AGY3D_OFFLINE", "1")
    bad = WebThreeReport(False, "web_three", "FAIL", checks=(web_three.Check("no loader errors", "fail", "boom"),))
    _canned(monkeypatch, [], bad)
    monkeypatch.setattr(WebThreeValidator, "_unavailable", lambda self: None)
    # "available" makes the run's ``web_three.prestart()`` lease the shared browser, which imports Playwright (absent in CI's unit
    # job); the check itself is canned above, so the lease is stood in for and no browser is involved either way.
    monkeypatch.setattr(web_three.web_browser.SHARED, "lease", lambda *_a, **_k: web_three.web_browser.Lease(None))
    rc = cli.main(["prop", "wooden crate with iron brackets", "--offline", "--engine", "web_three", "--out", str(tmp_path / "out")])
    out = capsys.readouterr().out
    assert rc == 1 and "three.js check FAILED: no loader errors: boom" in out and "NOT run" not in out


async def test_the_finished_line_says_imported_only_for_an_import_that_passed(ws, monkeypatch):
    """Old text for every target: "was validated, imported and checked by this tool". Now "imported" needs the import to have
    passed; otherwise: "was validated and checked (the engine import was not run: <reason>)" (Roblox has no importer)."""
    reply, _pkg, _man = await _deliver(ws, "roblox_r15")
    assert reply.startswith("DELIVERED") and "validated, imported" not in reply
    assert "was validated and checked (the engine import was not run: roblox has no headless import validator) by this tool" in reply
    monkeypatch.setattr(WebThreeValidator, "_unavailable", lambda self: None)
    _canned(monkeypatch, [])
    reply, _pkg, _man = await _deliver(ws, "web_three")
    assert "was validated, imported and checked by this tool" in reply


# --------------------------------------------------------------------------- the real browser


@pytest.mark.browser
async def test_browser_a_script_asset_for_the_web_passes_the_three_js_check_once(ws):
    if not WebThreeValidator().available():
        pytest.skip("needs the Python 'playwright' package and CHROMIUM_BIN (or a Chrome/Chromium on PATH)")
    reply, pkg, man = await _deliver(ws, "web_three")
    assert reply.startswith("DELIVERED"), reply
    rec = man["validation"]["engine_import"]
    assert rec["ok"] and rec["checks_passed"] == rec["checks"] >= 20 and rec["silhouette_iou"] >= web_three.MIN_SILHOUETTE_IOU
    assert rec["engine_version"] == "0.160.0 (vendored three.js)"  # which three.js did the check
    shot = pkg / "engine.png"
    rep = await WebThreeValidator().capture(pkg / f"{man['asset']}.glb", shot)
    assert rep.ok and Image.open(shot).size == (512, 512) and "A" in Image.open(shot).getbands()


# --------------------------------------------------------------------------- every target delivers through a session

ALL_TARGETS = list(get_args(TargetEngine))


async def _default_session_delivery(root: Path, engine: str) -> str:
    """The default path: scene operations through apply_scene_ir, then deliver_package (as tests/test_preview_runs.py drives it)."""
    from antigravity3d.ir import MaterialIR, NodeIR, Transform
    from antigravity3d.tactics.authoring import TemplateAuthor
    from antigravity3d.state import EngineBudget

    host = M.StudioHost()
    await host.open_asset_session("crate", engine, workdir=str(root / "s"), workspace=str(root), conversation_id="conv-1",
                                  preview=True, token_ceiling=150_000, max_images=0)
    sid = host.active
    scene = (await TemplateAuthor().propose("wooden crate", budget=EngineBudget(engine), n=1, context={"asset_name": "crate"}))[0]
    ops = [{"op": "upsert_material", "material": m.model_dump(mode="json", exclude_defaults=True)} for m in scene.materials]
    ops += [{"op": "upsert_node", "node": n.model_dump(mode="json", exclude_defaults=True)} for n in scene.nodes]
    await host.call_tool("apply_scene_ir", T.ALL_TOOLS["apply_scene_ir"], sid, {"ops": ops})
    return await host.call_tool("deliver_package", T.ALL_TOOLS["deliver_package"], sid, {"dest": "output"})


@pytest.mark.parametrize("path", ["default", "script"])
@pytest.mark.parametrize("engine", ALL_TARGETS)
async def test_every_target_delivers_offline_through_a_default_session_and_through_a_script(engine, path, ws, monkeypatch):
    """The Z-up working stage is checked on both paths before packaging; the up-axis rows describe what ships, so no target
    is refused for the working stage's axis (Roblox was: its row lists Y only). web_three's browser check is stood in for
    by "could not run", which ships the package and says so; its real run is in the browser tests."""
    monkeypatch.setattr(WebThreeValidator, "_unavailable", lambda self: "no browser in this test")
    if path == "default":
        reply = await _default_session_delivery(ws, engine)
    else:
        reply, _pkg, _man = await _deliver(ws, engine)
    assert reply.startswith("DELIVERED"), reply.splitlines()[0]
    assert "up_axis" not in reply  # not even the working stage's "must be re-oriented" warning


def test_the_working_stage_is_not_judged_by_the_shipped_axis_but_the_shipped_stage_is():
    from antigravity3d.kernel.types import MeshData
    from antigravity3d.state import EngineBudget
    from pxr import Usd, UsdGeom
    import numpy as np

    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    UsdGeom.Xform.Define(stage, "/crate")
    meshes = [MeshData("m", "/crate/m", np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], float), np.array([[0, 1, 2]]), None)]
    for engine in ALL_TARGETS:
        budget = EngineBudget(engine)
        working = [i.code for i in validator.check_engine_compliance(stage, meshes, budget, working_stage=True) if "up_axis" in i.code]
        shipped = [i.code for i in validator.check_engine_compliance(stage, meshes, budget)]
        assert working == [], (engine, working)
        # a Z-up file as a shipped stage: native for Unreal, refused or re-oriented by the others as their rows say
        assert ("up_axis" in " ".join(shipped)) == (engine != "unreal_5"), (engine, shipped)
