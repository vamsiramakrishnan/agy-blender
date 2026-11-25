"""Round eight, section B: a web package is web-sized by default, and its answer names the next step.

The texture size is a caller's parameter (open_asset_session, Asset(...), --max-texture-px) and a size the caller chose is
reported as the density it gives, not as a defect; the answer for a web package says what a size costs (atlas, download,
texture memory, how large on screen it stays sharp); the GLBs carry WebP maps when the frames show no difference from
PNG (adapters/engines/web_textures.py), the PNG maps stay for the USD and the KTX2 encoder; the delivery's answer ends
with the install call; the installer's description separates a project the user had from one built in this session.

Tests marked ``browser`` render in headless Chromium; ``blender`` imports a WebP GLB in Blender."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from pathlib import Path

import pytest
from test_run_build import build, crate, write, ws  # noqa: F401 - ws is a fixture

from antigravity3d.adapters.engines import web_textures as WT
from antigravity3d.adapters.engines import web_three
from antigravity3d.adapters.engines.web_three import WebThreeValidator
from antigravity3d.build import handoff
from antigravity3d.build.asset import Asset
from antigravity3d.build.shapes import BuildError
from antigravity3d.deliver import glb as G
from antigravity3d.deliver import install_package, web
from antigravity3d.harness import tools as T
from antigravity3d.harness.session import session
from antigravity3d.kernel import fast_ops
from antigravity3d.kernel.validator import validate_package
from antigravity3d.runtime import mcp_server as M
from antigravity3d.state import TEXTURE_PX_CHOICES, texture_px_choice


@pytest.fixture(autouse=True)
def _fresh_decisions():
    WT._DECIDED.clear()
    yield
    WT._DECIDED.clear()


def _tree(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


# --------------------------------------------------------------------------- 1. max_texture_px is the caller's


def test_the_texture_size_choices_are_the_atlas_sides_and_anything_else_is_refused_in_one_sentence():
    assert TEXTURE_PX_CHOICES == fast_ops.ATLAS_SIDES
    assert [texture_px_choice(v) for v in (None, 0, "", 1024)] == [None, None, None, 1024]
    for bad in (1000, 8192, True, "1024", 1024.0):
        with pytest.raises(ValueError, match=r"max_texture_px must be one of 256, 512, 1024, 2048, 4096"):
            texture_px_choice(bad)


def test_asset_takes_max_texture_px_into_its_design_and_an_older_design_without_it_still_reads(tmp_path):
    def one_box(**kw):
        a = Asset("crate", engine="web_three", **kw)
        a.box("body", size=(1.0, 1.0, 1.0), material=a.material("red", "#c02020"))
        return a.design()["asset"]

    assert one_box(max_texture_px=1024)["max_texture_px"] == 1024
    assert "max_texture_px" not in one_box()  # a design that chose none reads as before
    with pytest.raises(BuildError, match="Asset crate: max_texture_px must be one of"):
        Asset("crate", engine="web_three", max_texture_px=3000)
    doc = {"version": 1, "asset": {"name": "c", "engine": "web_three", "max_tris": 100, "brief": "", "art_direction": ""},
           "materials": [], "nodes": [], "notes": [], "stats": {}}
    (tmp_path / "design.json").write_text(json.dumps(doc))
    assert handoff.read(tmp_path).max_texture_px is None
    doc["asset"]["max_texture_px"] = 512
    (tmp_path / "design.json").write_text(json.dumps(doc))
    assert handoff.read(tmp_path).max_texture_px == 512
    doc["asset"]["max_texture_px"] = 500
    (tmp_path / "design.json").write_text(json.dumps(doc))
    with pytest.raises(BuildError, match="asset.max_texture_px must be one of"):
        handoff.read(tmp_path)


def test_the_command_line_takes_max_texture_px_into_the_budget():
    from antigravity3d import cli

    a = cli.build_parser().parse_args(["prop", "crate", "--engine", "web_three", "--max-texture-px", "512", "--out", "x"])
    assert cli._budget(a).max_texture_px == 512
    assert cli._budget(cli.build_parser().parse_args(["prop", "crate", "--out", "x"])).max_texture_px is None
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["prop", "crate", "--max-texture-px", "3000", "--out", "x"])


async def test_open_asset_session_takes_max_texture_px_and_its_line_says_so(ws):
    host = M.StudioHost(script=False)
    line = await host.open_asset_session("crate", "web_three", workspace=str(ws), max_texture_px=1024)
    s = session(host.registry, host.active)
    assert s.state.budget.max_texture_px == 1024 and "textures<=1024px" in line
    with pytest.raises(ValueError, match="max_texture_px must be one of"):
        await host.open_asset_session("crate", "web_three", workspace=str(ws), max_texture_px=999)
    tools = M._management(host)
    assert "max_texture_px" in tools["open_asset_session"][0].__annotations__


async def test_run_build_takes_the_scripts_max_texture_px_and_a_later_build_changes_it_in_place(ws):
    host = M.StudioHost(script=True)
    text = crate(engine="web_three").replace("max_tris=4000,", "max_tris=4000, max_texture_px=512,")
    await build(host, ws, write(ws, text))
    s = session(host.registry, host.active)
    assert s.state.budget.max_texture_px == 512
    r = await build(host, ws, write(ws, text.replace("max_texture_px=512", "max_texture_px=256").replace("#2040d0", "#2050d0")))
    assert session(host.registry, host.active).state.budget.max_texture_px == 256
    assert "texture atlas now at most 256 px (was 512)" in r


async def _deliver(ws, engine: str, script: str):
    host = M.StudioHost(script=True)
    await build(host, ws, write(ws, script), preview=True, token_ceiling=100_000, max_images=0)
    reply = await host.call_tool("deliver_package", T.deliver_package, "", {})
    pkg = session(host.registry, host.active).last_export
    return reply, pkg, (json.loads((pkg / "manifest.json").read_text()) if pkg else None)


async def test_a_chosen_atlas_size_is_reported_as_the_density_it_gives_not_as_a_defect(ws, monkeypatch):
    """The crate's flat colours want a 512 x 256 atlas; capped at 256 the density is lower, and that is the choice."""
    monkeypatch.setattr(WebThreeValidator, "_unavailable", lambda self: "no browser in this test")
    text = crate(engine="web_three").replace("max_tris=4000,", "max_tris=4000, max_texture_px=256,")
    reply, _pkg, man = await _deliver(ws, "web_three", text)
    assert reply.startswith("DELIVERED"), reply
    assert man["budget"]["max_texture_px"] == 256 and max(man["texture_atlas"]["atlas_wh"]) <= 256
    recs = man["uv"]["texel_density"]["materials"].values()
    assert recs and all(r["chosen_max_texture_px"] == 256 for r in recs)
    assert any(r["needed_atlas_px"] > 256 for r in recs), "the cap must be what lowered the density for this test to mean anything"
    assert not [i for i in man["validation"]["engine"] if i["code"].endswith("texel_density_target")]
    assert "every map is at most 256 px because max_texture_px=256 was asked for" in reply
    # no browser: no frames to compare, so the package keeps PNG and the answer and the manifest say why
    assert man["web_textures"]["format"] == "png" and "could not be compared" in man["web_textures"]["reason"]
    assert "in PNG (not WebP: the frames could not be compared" in reply


async def test_an_uncapped_package_reads_as_before_apart_from_the_web_facts(ws, monkeypatch):
    monkeypatch.setattr(WebThreeValidator, "_unavailable", lambda self: "no browser in this test")
    _reply, _pkg, man = await _deliver(ws, "web_three", crate(engine="web_three"))
    assert "max_texture_px" not in man["budget"]
    assert all("chosen_max_texture_px" not in r for r in man["uv"]["texel_density"]["materials"].values())


async def test_a_web_package_with_no_size_named_is_capped_at_the_web_default_and_says_so(ws, monkeypatch):
    """No size named: a web atlas stops at ``EngineRules.default_texture_px`` (2048), because a browser holds every map
    uncompressed (run 1 of the website task: 454 MB of textures for three props at the 4096 px the density target asks
    for). The default is lowered here to 256 so that the small crate (it wants 512 x 256) meets it."""
    import dataclasses

    from antigravity3d.kernel import fast_ops
    from antigravity3d.kernel.validator import ENGINE_RULES

    assert ENGINE_RULES["web_three"].default_texture_px == 2048
    assert max(max(wh) for wh in fast_ops.atlas_sizes("web_three")) == 2048
    assert max(max(wh) for wh in fast_ops.atlas_sizes("web_three", 4096)) == 4096, "4096 is one parameter away"
    assert max(max(wh) for wh in fast_ops.atlas_sizes("godot_4")) == 4096, "other engines are as before"
    monkeypatch.setitem(ENGINE_RULES, "web_three", dataclasses.replace(ENGINE_RULES["web_three"], default_texture_px=256))
    monkeypatch.setattr(WebThreeValidator, "_unavailable", lambda self: "no browser in this test")
    reply, _pkg, man = await _deliver(ws, "web_three", crate(engine="web_three"))
    assert reply.startswith("DELIVERED"), reply
    assert "max_texture_px" not in man["budget"] and max(man["texture_atlas"]["atlas_wh"]) <= 256
    recs = man["uv"]["texel_density"]["materials"].values()
    assert recs and all(r["default_max_texture_px"] == 256 and "chosen_max_texture_px" not in r for r in recs)
    assert any(r["needed_atlas_px"] > 256 for r in recs), "the default must be what capped the atlas for this test to mean anything"
    assert not [i for i in man["validation"]["engine"] if i["code"].endswith("texel_density_target")]
    assert "every map is at most 256 px, the default for a web page; max_texture_px=512 would make that up to about" in reply


# --------------------------------------------------------------------------- 2. what a size costs, in two lines


def _web_manifest(**over):
    m = {"target_engine": "web_three", "validation": {"engine_import": {"ok": True, "checks_passed": 25, "checks": 25, "silhouette_iou": 0.9997}},
         "web_download": {"level": "LOD0", "bytes": 2_450_008, "files": {"gate.glb": 2_450_008}},
         "textures": [{"path": f"textures/gate_atlas_{r}.png", "role": r, "width": 4096, "height": 2048} for r in ("albedo", "orm", "normal")],
         "texture_atlas": {"atlas_wh": [4096, 2048], "texel_density_px_per_m": 512.0},
         "glb_bounds_m": [[-0.94, 0.0, -0.66], [0.94, 1.262, 0.66]], "budget": {"max_tris": 8000},
         "web_textures": {"format": "webp", "glb_bytes": {"before": 9_593_560, "after": 2_450_008}}}
    m.update(over)
    return m


def test_the_answer_states_atlas_download_memory_and_sharpness_in_two_lines():
    lines = web_three.reply_lines(_web_manifest())
    assert lines[0] == "three.js check passed: 25 of 25 checks, silhouette IoU 0.9997" and len(lines) == 3
    size, sharp = lines[1:]
    assert size.startswith("web size: 4096 x 2048 px texture atlas in WebP (with PNG the GLB was 9,593,560 bytes); "
                           "a page downloads 2,450,008 bytes for the full-detail model (gate.glb 2,450,008); 3 map(s) take 128 MiB")
    # 512 texels per metre over the 1.88 m longest side; the next atlas down is 2048 x 2048, half the texels
    assert sharp.startswith("the textures stay sharp up to about 963 px across on screen")
    assert sharp.endswith("max_texture_px=2048 would make that about 681 px with 50% of the texture memory and of the maps' download")
    chosen = web_three.reply_lines(_web_manifest(budget={"max_texture_px": 4096}))[2]
    assert chosen.endswith("every map is at most 4096 px because max_texture_px=4096 was asked for")
    png = web_three.reply_lines(_web_manifest(web_textures={"format": "png", "reason": "outside the tolerance in close_up_1"}))[1]
    assert "texture atlas in PNG (not WebP: outside the tolerance in close_up_1)" in png
    assert web_three.reply_lines({**_web_manifest(), "target_engine": "godot_4"}) == []


# --------------------------------------------------------------------------- 4. the answer ends with the install call


def test_the_next_step_is_the_exact_install_call_for_three_js_and_godot_and_nothing_for_the_others(tmp_path):
    for engine, needle in (("web_three", ("a loader module to import (src/agy3d/hand_truck/index.js: loadHandTruck() returns "
                                          "a Group holding a THREE.LOD")),
                           ("godot_4", "a scene agy3d/hand_truck/hand_truck.tscn")):
        (tmp_path / "manifest.json").write_text(json.dumps({"target_engine": engine, "asset": "hand_truck"}))
        (line,) = T._install_step(tmp_path)
        assert line.startswith(f'NEXT: to use it in a {"three.js" if engine == "web_three" else "Godot"} project, '
                               f'install_into_project(package_dir="{tmp_path}", dry_run=false) gives ')
        assert needle in line and "dry_run=true first" in line and line.endswith("Do not copy the files by hand.")
    (tmp_path / "manifest.json").write_text(json.dumps({"target_engine": "unreal_5", "asset": "x"}))
    assert T._install_step(tmp_path) == [] and T._install_step(None) == []


# --------------------------------------------------------------------------- 5. the installer's two cases


def test_the_install_description_separates_a_project_the_user_had_from_one_built_in_this_session():
    for script in (False, True):
        fn, doc = M._management(M.StudioHost(script=script))["install_into_project"]
        flat = " ".join(doc.split())
        assert "A project you are building in this session because the user asked for it" in flat
        assert "install directly with dry_run=false" in flat and "never by copying its files by hand" in flat
        assert "A project the user already had: call with dry_run=true first, show the user the plan" in flat
        assert "Without the compression packages in the project the install still writes the web GLB" in flat
        import typing

        dry = typing.get_type_hints(fn, globalns=vars(M), include_extras=True)["dry_run"].__metadata__[0].description
        assert "already had" in dry and "pass false" in dry


# --------------------------------------------------------------------------- 3. WebP when the frames agree


@pytest.fixture(scope="module")
def png_package(tmp_path_factory) -> Path:
    """The offline web crate (``agy3d prop --offline --engine web_three``) with its maps left PNG: the comparison is
    stubbed to 'could not run', which keeps PNG."""
    from antigravity3d.cli import main

    out = tmp_path_factory.mktemp("crate")

    async def unmeasured(*a, **k):
        return {"passed": None, "why": "stubbed for the fixture", "tolerance": {}}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(WT, "measure", unmeasured)
        assert main(["prop", "wooden crate with iron brackets", "--offline", "--engine", "web_three", "--out", str(out)]) == 0
    (pkg,) = out.glob("runs/*/package")
    man = json.loads((pkg / "manifest.json").read_text())
    assert man["web_textures"]["format"] == "png" and man["lod_levels"] >= 2
    return pkg


def _copy(pkg: Path, dst: Path) -> tuple[Path, dict]:
    shutil.copytree(pkg, dst)
    return dst, json.loads((dst / "manifest.json").read_text())


def _passing(calls: list):
    async def measure(manifest, png_glb, webp_glb, timeout=0):
        calls.append(len(webp_glb))
        return {"passed": True, "frames": [{"view": "frame", "passed": True, "mean_abs": 0.002, "pixel_share": 0.0}],
                "tolerance": {"pixel_delta": 0.08}}
    return measure


def test_webp_ships_in_every_glb_the_pngs_stay_and_the_package_validates(png_package, tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(WT, "measure", _passing(calls))
    pkg, data = _copy(png_package, tmp_path / "pkg")
    before = (pkg / f"{data['asset']}.glb").stat().st_size
    rec = asyncio.run(WT.web_format(pkg, data))
    man = json.loads((pkg / "manifest.json").read_text())
    assert rec["format"] == "webp" and man["web_textures"] == {k: v for k, v in rec.items() if k != "timings"}
    assert "timings" not in man["web_textures"], "run telemetry is not package content"
    lod0 = f"{man['asset']}.glb"
    assert rec["glb_bytes"] == {"before": before, "after": (pkg / lod0).stat().st_size} and rec["glb_bytes"]["after"] < before
    assert man["web_download"]["bytes"] == rec["glb_bytes"]["after"]
    webps = [m["webp"] for m in rec["maps"] if m["webp"]]
    assert webps and all(w in man["files"] and (pkg / w).is_file() for w in webps)
    assert all(m["texture"] in man["files"] and (pkg / m["texture"]).is_file() for m in rec["maps"])  # the PNGs stay
    assert all(r["mime_type"] == "image/webp" for r in man["glb_images"][lod0] if r["texture"] in webps)
    for name, recs in man["glb_images"].items():  # far levels reference the WebP files: every level binds the same content
        if "_LOD" in name:
            assert {r["uri"] for r in recs if r["storage"] == "uri"} <= set(webps) | {m["texture"] for m in rec["maps"] if not m["webp"]}
    doc = G._split((pkg / lod0).read_bytes())[0]
    assert doc["extensionsRequired"] == ["EXT_texture_webp"] and all(
        "source" not in t for t in doc["textures"] if "EXT_texture_webp" in t.get("extensions", {}))
    assert [i for i in validate_package(pkg) if i.severity == "critical"] == []
    assert man["validation"]["package_ok"] and all("webp_encode" in man["provenance"]["files"][w]["steps"] for w in webps)


def test_the_same_bytes_are_decided_once_per_process_and_give_the_same_files(png_package, tmp_path, monkeypatch):
    """deliver_package's checked package and the export's are the same bytes: the export applies the decision."""
    calls: list = []
    monkeypatch.setattr(WT, "measure", _passing(calls))
    a, da = _copy(png_package, tmp_path / "a")
    b, db = _copy(png_package, tmp_path / "b")
    first = asyncio.run(WT.web_format(a, da))
    second = asyncio.run(WT.web_format(b, db))
    assert len(calls) == 1 and first["timings"]["reused"] is False and second["timings"]["reused"] is True
    assert _tree(a) == _tree(b), "the manifest and every file are the same bytes"


_OUTSIDE = {"passed": False, "frames": [{"view": "close_up_2", "passed": False, "mean_abs": 0.03, "pixel_share": 0.04}],
            "tolerance": {"pixel_delta": 0.08}}
_OUTSIDE_SAYS = "outside the tolerance in close_up_2 (mean 0.0300, 4.00% of the asset's pixels over 8%)"


def _lossless(glb: bytes) -> list[bool]:
    """Per WebP map a GLB embeds: whether it is stored lossless (a VP8L chunk; a lossy one is 'VP8 ')."""
    return [b[12:16] == b"VP8L" for b, mime in G.embedded_images(glb) if mime == "image/webp"]


def _measuring(calls: list, passes):
    """A comparison that is inside the tolerance when ``passes(which maps of the candidate are lossless)``."""
    async def measure(manifest, png_glb, webp_glb, timeout=0):
        calls.append(_lossless(webp_glb))
        if passes(calls[-1]):
            return {"passed": True, "frames": [{"view": "frame", "passed": True, "mean_abs": 0.002, "pixel_share": 0.0}],
                    "tolerance": {"pixel_delta": 0.08}}
        return json.loads(json.dumps(_OUTSIDE))
    return measure


@pytest.mark.parametrize("outcome, reason, measured", [
    (_OUTSIDE, f"lossy: {_OUTSIDE_SAYS}; roughness/metal map lossless: {_OUTSIDE_SAYS}; lossless: {_OUTSIDE_SAYS}", 3),
    ({"passed": None, "why": "no browser"}, "the frames could not be compared, so nothing lossy ships: no browser", 1),
])
def test_a_difference_or_no_measurement_keeps_png_and_says_why(png_package, tmp_path, monkeypatch, outcome, reason, measured):
    """Every encoding outside the tolerance: PNG, with each encoding's numbers. Frames that cannot be rendered: PNG at
    once, the browser is not asked again (a lossless map is not shipped unmeasured either)."""
    calls: list = []

    async def measure(*a, **k):
        calls.append(1)
        return json.loads(json.dumps(outcome))

    monkeypatch.setattr(WT, "measure", measure)
    pkg, data = _copy(png_package, tmp_path / "pkg")
    before = _tree(pkg)
    rec = asyncio.run(WT.web_format(pkg, data))
    assert rec["format"] == "png" and rec["reason"] == reason and len(calls) == measured
    assert "encoding" not in rec and all("lossless" not in m for m in rec["maps"]), "the maps listed are the lossy ones"
    after = _tree(pkg)
    assert {k: v for k, v in after.items() if k != "manifest.json"} == {k: v for k, v in before.items() if k != "manifest.json"}
    assert json.loads((pkg / "manifest.json").read_text())["web_textures"]["format"] == "png"


def test_a_lossy_roughness_metal_map_outside_the_tolerance_ships_lossless_and_the_other_maps_stay_lossy(
        png_package, tmp_path, monkeypatch):
    """Run 5 of the website task: two props kept PNG (1.65 and 1.48 MB of maps) because the lossy roughness/metal map
    put 1.07 % and 2.02 % of a close-up's pixels over 8 %. That one map lossless is inside the tolerance and the
    package is still far under PNG."""
    calls: list = []
    monkeypatch.setattr(WT, "measure", _measuring(calls, any))  # inside the tolerance once a map is lossless
    pkg, data = _copy(png_package, tmp_path / "pkg")
    png_bytes = (pkg / f"{data['asset']}.glb").stat().st_size
    rec = asyncio.run(WT.web_format(pkg, data))
    assert rec["format"] == "webp" and rec["encoding"] == "roughness/metal map lossless"
    assert calls == [[False, False, False], [False, True, False]], "lossy first, then the one map lossless"
    assert rec["tried"] == [{"encoding": "lossy", "reason": _OUTSIDE_SAYS}] and "reason" not in rec
    by_role = {m["role"]: m for m in rec["maps"]}
    assert by_role["orm"].get("lossless") is True and "quality" not in by_role["orm"]
    assert all("lossless" not in by_role[r] and by_role[r]["quality"] == WT.WEBP_QUALITY[r] for r in ("albedo", "normal"))
    assert all(m["webp"] and (pkg / m["webp"]).is_file() for m in rec["maps"])
    lod0 = (pkg / f"{data['asset']}.glb").read_bytes()
    assert _lossless(lod0) == [False, True, False] and len(lod0) == rec["glb_bytes"]["after"] < png_bytes
    # the lossless map is the PNG's pixels exactly
    from PIL import Image

    orm = by_role["orm"]
    assert Image.open(pkg / orm["webp"]).convert("RGB").tobytes() == Image.open(pkg / orm["texture"]).convert("RGB").tobytes()
    man = json.loads((pkg / "manifest.json").read_text())
    assert man["web_textures"] == {k: v for k, v in rec.items() if k != "timings"}
    assert [i for i in validate_package(pkg) if i.severity == "critical"] == []


def test_every_map_lossless_is_the_last_encoding_tried_and_a_map_that_is_no_smaller_stays_png(png_package, tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(WT, "measure", _measuring(calls, lambda maps: bool(maps) and all(maps)))
    pkg, data = _copy(png_package, tmp_path / "pkg")
    rec = asyncio.run(WT.web_format(pkg, data))
    assert rec["format"] == "webp" and rec["encoding"] == "lossless" and len(calls) == 3 and all(calls[-1])
    assert [t["encoding"] for t in rec["tried"]] == ["lossy", "roughness/metal map lossless"]
    kept = [m for m in rec["maps"] if not m["webp"]]
    assert all(m["webp_bytes"] >= m["png_bytes"] and m["kept_png"] == "the WebP encoding is not smaller" for m in kept)
    assert all(m.get("lossless") is True for m in rec["maps"])
    assert [i for i in validate_package(pkg) if i.severity == "critical"] == []


def test_an_encoding_that_gives_the_same_files_as_one_already_compared_is_not_compared_again(png_package, tmp_path, monkeypatch):
    """A package with no roughness/metal map (here: a step that names a role no map has) has nothing to try in
    between: the browser is not started for the same bytes twice."""
    calls: list = []

    async def measure(*a, **k):
        calls.append(1)
        return json.loads(json.dumps(_OUTSIDE))

    monkeypatch.setattr(WT, "measure", measure)
    monkeypatch.setattr(WT, "LADDER", (("lossy", frozenset()), ("nothing new", frozenset({"clearcoat"})), ("lossless", None)))
    pkg, data = _copy(png_package, tmp_path / "pkg")
    rec = asyncio.run(WT.web_format(pkg, data))
    assert len(calls) == 2 and [t["encoding"] for t in rec["tried"]] == ["lossy", "lossless"] and rec["format"] == "png"


def test_frames_that_cannot_be_compared_after_a_miss_end_the_search_and_are_asked_again_next_time(png_package, tmp_path, monkeypatch):
    answers = [_OUTSIDE, {"passed": None, "why": "the browser closed"}]
    calls: list = []

    async def measure(*a, **k):
        calls.append(1)
        return json.loads(json.dumps(answers[(len(calls) - 1) % 2]))  # for each package: a miss, then no comparison

    monkeypatch.setattr(WT, "measure", measure)
    pkg, data = _copy(png_package, tmp_path / "pkg")
    rec = asyncio.run(WT.web_format(pkg, data))
    assert rec["format"] == "png" and len(calls) == 2
    assert rec["reason"] == (f"lossy: {_OUTSIDE_SAYS}; roughness/metal map lossless: the frames could not be compared: "
                             "the browser closed")
    again, data2 = _copy(png_package, tmp_path / "again")
    asyncio.run(WT.web_format(again, data2))
    assert len(calls) == 4, "a decision that rests on a comparison that could not run is not kept"


def test_a_metal_mask_comes_back_wrong_from_lossy_webp_at_any_quality_and_exact_from_lossless():
    """Why the roughness/metal map is the one stored lossless: lossy WebP keeps colour at half the resolution of
    brightness (YUV 4:2:0) whatever the quality, and this map's channels are unrelated quantities, so a metal mask
    against the roughness beside it is 'colour'. Measured on run 5's two maps: 14.4 % and 10.2 % of texels off by more
    than 8 % at quality 90, 14.3 % and 10.2 % at quality 100. Here: a coating at 0.45 metal with chips of bare metal
    and specks of dirt one to two texels wide."""
    import io

    import numpy as np
    from PIL import Image

    rng = np.random.default_rng(5)
    side = 256
    metal = np.full((side, side), round(0.45 * 255), np.uint8)
    for value, count in ((255, 900), (0, 900)):
        for y, x in rng.integers(0, side - 2, (count, 2)):
            metal[y:y + rng.integers(1, 3), x:x + rng.integers(1, 3)] = value
    rough = (140 + 20 * np.sin(np.arange(side) / 9.0)[None, :] + rng.normal(0, 3, (side, side))).clip(0, 255).astype(np.uint8)
    orm = np.dstack([np.full((side, side), 255, np.uint8), rough, metal])
    buf = io.BytesIO()
    Image.fromarray(orm, "RGB").save(buf, "PNG")

    def back(quality):
        return np.asarray(Image.open(io.BytesIO(WT._encode_one(buf.getvalue(), quality))).convert("RGB"), dtype=np.int16)

    def off(quality):  # share of texels with a channel off by more than the tolerance's 8 % of full scale
        return float((np.abs(back(quality) - orm).max(axis=2) > 0.08 * 255).mean())

    at90, at100 = off(90), off(100)
    assert at90 > 0.05 and at100 > 0.05, (at90, at100)
    assert at100 > 0.8 * at90, "a higher quality does not bring the mask back"
    assert np.array_equal(back(None), orm), "lossless is the same texels"


def test_a_webp_package_that_does_not_validate_is_put_back_as_it_was(png_package, tmp_path, monkeypatch):
    from antigravity3d.kernel import validator
    from antigravity3d.kernel.fast_ops import Issue

    monkeypatch.setattr(WT, "measure", _passing([]))
    monkeypatch.setattr(validator, "validate_package", lambda out: [Issue("pkg:boom", "critical", None, "boom")])
    pkg, data = _copy(png_package, tmp_path / "pkg")
    before = _tree(pkg)
    rec = asyncio.run(WT.web_format(pkg, data))
    assert rec["format"] == "png" and "did not validate" in rec["reason"] and "boom" in rec["reason"]
    after = _tree(pkg)
    assert {k: v for k, v in after.items() if k != "manifest.json"} == {k: v for k, v in before.items() if k != "manifest.json"}
    assert not list(pkg.rglob("*.webp"))


def test_other_targets_are_not_touched(png_package, tmp_path):
    pkg, data = _copy(png_package, tmp_path / "pkg")
    data["target_engine"] = "godot_4"
    data.pop("web_textures")
    before = _tree(pkg)
    assert asyncio.run(WT.web_format(pkg, data)) is None and _tree(pkg) == before and "web_textures" not in data


def test_encoding_is_deterministic(png_package):
    man = json.loads((png_package / "manifest.json").read_text())
    one, maps, _ = WT.encode(png_package, man)
    two, _, _ = WT.encode(png_package, man)
    assert one is not None and one.glbs == two.glbs and one.files == two.files
    assert all(m["webp_bytes"] < m["png_bytes"] for m in maps if m["webp"])


def test_swap_images_writes_the_extension_only_while_used_and_strip_images_drops_it(png_package, tmp_path, monkeypatch):
    monkeypatch.setattr(WT, "measure", _passing([]))
    pkg, data = _copy(png_package, tmp_path / "pkg")
    asyncio.run(WT.web_format(pkg, data))
    glb = (pkg / f"{data['asset']}.glb").read_bytes()
    stripped = G._split(G.strip_images(glb))[0]
    assert "EXT_texture_webp" not in stripped.get("extensionsUsed", []) + stripped.get("extensionsRequired", [])
    # back to PNG: the PNG maps' bytes, plain sources, no extension
    textures = {f: (pkg / f).read_bytes() for f in data["files"] if f.startswith("textures/")}
    back = web.lossless_level0(glb, textures, data)
    doc = G._split(back)[0]
    pngs = {hashlib.sha256(b).hexdigest() for b, _m in G.embedded_images(back)}
    assert pngs == {hashlib.sha256(textures[m["texture"]]).hexdigest() for m in data["web_textures"]["maps"]}
    assert all(m == "image/png" for _b, m in G.embedded_images(back))
    assert "extensionsRequired" not in doc and all("source" in t and "extensions" not in t for t in doc["textures"])


def test_the_installer_serves_the_webp_maps_and_compresses_from_the_pngs(png_package, tmp_path, monkeypatch):
    from test_web_install import _project

    monkeypatch.setattr(WT, "measure", _passing([]))
    pkg, data = _copy(png_package, tmp_path / "pkg")
    asyncio.run(WT.web_format(pkg, data))
    man = json.loads((pkg / "manifest.json").read_text())
    project = _project(tmp_path / "game")
    r = install_package(pkg, project, check=False)
    assert r.status == "installed", r.message
    served = sorted(p.name for p in (project / "public" / "agy3d" / man["asset"] / "textures").iterdir())
    shown = {Path(m["webp"] or m["texture"]).name for m in man["web_textures"]["maps"]}
    assert served == sorted(shown), "only the maps LOD0 shows are served: the PNG copies stay in the package"
    parts = web.read_parts(pkg, man)
    assert {hashlib.sha256(b).hexdigest() for b, _m in G.embedded_images(parts.lossless_first)} == \
        {hashlib.sha256((pkg / m["texture"]).read_bytes()).hexdigest() for m in man["web_textures"]["maps"]}


@pytest.mark.browser
def test_browser_noise_maps_differ_as_lossy_webp_so_the_offline_crate_ships_them_lossless(tmp_path):
    """The offline crate's maps are the fake image backend's noise. Lossy WebP visibly changes them (measured: 7.6 % of
    the frame's asset pixels over 8 %), and with the roughness/metal map alone lossless they still differ (5.9 %: the
    colour map is noise too). Every map lossless is the PNG's picture, 0 pixels over in every frame, and smaller
    (443,988 -> 383,784 bytes of GLB), with the normal map left PNG because its lossless WebP is no smaller. The guard
    is the measurement, not the format."""
    if why := WebThreeValidator()._unavailable():
        pytest.skip(why)
    from antigravity3d.cli import main

    assert main(["prop", "wooden crate with iron brackets", "--offline", "--engine", "web_three", "--out", str(tmp_path)]) == 0
    (pkg,) = tmp_path.glob("runs/*/package")
    rec = json.loads((pkg / "manifest.json").read_text())["web_textures"]
    views = [f["view"] for f in rec["measured"]["frames"]]
    assert views[0] == "frame" and len(views) == 1 + WT.CLOSE_UPS
    assert [t["encoding"] for t in rec["tried"]] == ["lossy", "roughness/metal map lossless"]
    assert all(t["reason"].startswith("outside the tolerance in frame") for t in rec["tried"])
    assert rec["format"] == "webp" and rec["encoding"] == "lossless" and rec["measured"]["passed"] is True
    assert all(f["pixel_share"] == 0 and f["mean_abs"] == 0 for f in rec["measured"]["frames"]), "lossless is exact in the frame too"
    assert rec["glb_bytes"]["after"] < rec["glb_bytes"]["before"]
    kept = [m["role"] for m in rec["maps"] if not m["webp"]]
    assert kept == ["normal"] and len(list(pkg.rglob("*.webp"))) == 2, "a map that is no smaller as WebP stays PNG beside the others"
    assert [i for i in validate_package(pkg) if i.severity == "critical"] == []


@pytest.mark.browser
async def test_browser_a_painted_crate_ships_webp_and_the_three_js_check_passes_on_it(ws, monkeypatch):
    """The script crate's flat-colour maps as WebP are inside the tolerance in every frame; the package check of the
    WebP GLB (the vendored three.js) then passes, LOD content included."""
    if why := WebThreeValidator()._unavailable():
        pytest.skip(why)
    with monkeypatch.context() as m:  # the package written without a browser keeps PNG
        m.setattr(WebThreeValidator, "_unavailable", lambda self: "no browser while it is written")
        _reply, png_pkg, _man = await _deliver(ws, "web_three", crate(engine="web_three"))
    pkg, data = _copy(png_pkg, ws / "copy")
    rec = await WT.web_format(pkg, data)
    assert rec["format"] == "webp" and all(f["passed"] for f in rec["measured"]["frames"]), rec
    assert rec["glb_bytes"]["after"] < rec["glb_bytes"]["before"]
    rep = await WebThreeValidator().validate(pkg)
    assert rep.ok, [str(c) for c in rep.failures]


@pytest.mark.blender
async def test_blender_imports_a_webp_glb(png_package, tmp_path, monkeypatch):
    """The Blender check of what ships (upstream_checks.asset_qa) reads the GLB the web gets."""
    from antigravity3d.adapters.blender_5_3 import upstream_checks
    from antigravity3d.adapters.blender_5_3.pool import BlenderPool, find_blender

    if not find_blender():
        pytest.skip("no Blender binary ($BLENDER_BIN or PATH)")
    monkeypatch.setattr(WT, "measure", _passing([]))
    pkg, data = _copy(png_package, tmp_path / "pkg")
    await WT.web_format(pkg, data)
    async with BlenderPool(1, startup_timeout=180) as pool:
        status, detail, _data = await upstream_checks.asset_qa(pool, str(pkg / f"{data['asset']}.glb"), "web_three")
    assert status == "passed", detail


# --------------------------------------------------------------------------- a chosen size holds for every map


def _glass_and_paint(tmp: Path, budget):
    """Two materials with 1024 px maps: opaque paint (shared atlas) and blended glass (its own atlas, not shared)."""
    import numpy as np
    from PIL import Image

    from antigravity3d.ir import MaterialIR, NodeIR, SceneIR, Transform
    from antigravity3d.kernel.package import write_engine_package
    from antigravity3d.kernel.usd_stage import StageManager

    rng = np.random.default_rng(3)
    maps = {}
    for mat in ("paint", "glass"):
        for k in ("albedo", "orm", "normal"):
            Image.fromarray(rng.integers(0, 255, (1024, 1024, 3), dtype=np.uint8)).save(tmp / f"{mat}_{k}.png")
        maps[mat] = {f"{k}_uri": str(tmp / f"{mat}_{k}.png") for k in ("albedo", "orm", "normal")}
    scene = SceneIR(asset_name="Lamp", materials=[
        MaterialIR(id="paint", **maps["paint"]), MaterialIR(id="glass", alpha_mode="BLEND", **maps["glass"])],
        nodes=[NodeIR(id="base", kind="box", params={"size": (0.6, 0.6, 0.2)}, transform=Transform(translate=(0, 0, 0.1)), material_id="paint"),
               NodeIR(id="shade", kind="sphere", params={"radius": 0.25}, transform=Transform(translate=(0, 0, 0.45)), material_id="glass")])
    sm = StageManager.create(tmp / "work.usda", "Lamp")
    sm.apply_ir(scene)
    bundle = fast_ops.run_export_pipeline(sm.extract_meshes(), budget, "Lamp")
    return bundle, write_engine_package(sm, bundle, budget, tmp / "pkg", asset_name="Lamp", qa={"tier1_ok": True, "tier15": 3.9})


@pytest.mark.parametrize("engine", ["web_three", "godot_4"])
def test_a_chosen_size_holds_for_a_transparent_materials_own_maps_too(tmp_path, engine):
    """Found in review: materials outside the shared atlas (not known to be opaque) kept a hard-coded 2048 px atlas and
    shipped their maps as they were, while the answer said the atlas was at most the size asked for."""
    from antigravity3d.state import EngineBudget

    bundle, pm = _glass_and_paint(tmp_path, EngineBudget(engine, max_texture_px=512))
    assert pm.ok, [i for i in pm.issues if i.severity == "critical"]
    man = pm.data
    assert man["textures"] and all(max(t["width"], t["height"]) <= 512 for t in man["textures"])
    glass = [t for t in man["textures"] if t["material"] == "glass"]
    assert glass and all(t["downscaled_from"] == [1024, 1024] for t in glass), "the glass maps were shrunk, and say so"
    assert all(u.resolution <= 512 for u in bundle.uvs), "its own atlas is packed at the size it ships at"
    assert {"glass"} <= set(man["uv"]["texel_density"]["materials"]) and all(
        r["chosen_max_texture_px"] == 512 for r in man["uv"]["texel_density"]["materials"].values())
    if engine == "web_three":
        assert web_three.size_lines(man)[1].endswith("every map is at most 512 px because max_texture_px=512 was asked for")


def test_without_a_chosen_size_a_godot_packages_own_maps_ship_as_before(tmp_path):
    from antigravity3d.state import EngineBudget

    _bundle, pm = _glass_and_paint(tmp_path, EngineBudget("godot_4"))
    glass = [t for t in pm.data["textures"] if t["material"] == "glass"]
    assert glass and all((t["width"], t["height"]) == (1024, 1024) and "downscaled_from" not in t for t in glass)


def test_generated_maps_are_made_at_the_chosen_size():
    from antigravity3d.kernel.validator import budget_texture_px, chosen_texture_px
    from antigravity3d.state import EngineBudget

    assert budget_texture_px(EngineBudget("web_three", max_texture_px=512), 2048) == 512
    assert budget_texture_px(EngineBudget("web_three"), 4096) == 2048  # the web default
    assert budget_texture_px(EngineBudget("roblox_r15"), 2048) == 1024  # the engine's limit, as before
    assert budget_texture_px(EngineBudget("godot_4"), 2048) == 2048 and chosen_texture_px(EngineBudget("godot_4")) is None
    assert fast_ops.own_atlas_px(EngineBudget("godot_4")) == 2048 and fast_ops.own_atlas_px(EngineBudget("godot_4", max_texture_px=256)) == 256


async def test_a_cancel_during_the_webp_write_returns_only_once_the_package_is_whole(png_package, tmp_path, monkeypatch):
    """An edit stops a running deliver_package routinely: the cancel must not return while the thread still writes GLBs
    and the manifest into the folder (then about to be removed), leaving WebP GLBs under a PNG manifest."""
    import threading
    import time

    from antigravity3d.kernel import validator

    monkeypatch.setattr(WT, "measure", _passing([]))
    real, writing, done = validator.validate_package, threading.Event(), threading.Event()

    def slow(out):  # runs in _apply's thread after the files are written: the cancel arrives now
        writing.set()
        time.sleep(0.5)
        try:
            return real(out)
        finally:
            done.set()

    monkeypatch.setattr(validator, "validate_package", slow)
    pkg, data = _copy(png_package, tmp_path / "pkg")
    task = asyncio.ensure_future(WT.web_format(pkg, data))
    await asyncio.to_thread(writing.wait, 30)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert done.is_set(), "the cancel came back before the thread had finished"
    assert [i for i in real(pkg) if i.severity == "critical"] == [], "files and manifest agree"
    assert not list(pkg.rglob("*.agy3d-tmp"))
