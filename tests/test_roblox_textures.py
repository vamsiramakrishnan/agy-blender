"""Roblox packages carry the texture set Roblox documents (separate metalness/roughness maps, 1024 px maps), and no
other engine's package changes. Real kernel, tiny generated images, no image backend, no Roblox Studio."""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from antigravity3d.kernel.types import MeshData
from antigravity3d.kernel.validator import ENGINE_RULES, check_engine_compliance, engine_texture_px
from antigravity3d.state import EngineBudget

from test_package import ASSET, _export, _maps


def _sized_maps(d: Path, side: int) -> Path:
    d.mkdir(exist_ok=True)
    _maps(d)
    for k in ("albedo", "orm", "normal"):
        Image.open(d / f"{k}.png").resize((side, side)).save(d / f"{k}.png")
    return d


def _glb_json(path: Path) -> dict:
    raw = path.read_bytes()
    n = struct.unpack("<I", raw[12:16])[0]
    return json.loads(raw[20:20 + n])


@pytest.fixture(scope="module")
def maps(tmp_path_factory) -> Path:
    return _sized_maps(tmp_path_factory.mktemp("rb_maps"), 64)


@pytest.fixture(scope="module")
def roblox(tmp_path_factory, maps):
    return _export(tmp_path_factory.mktemp("rb"), maps, "roblox_r15")


def test_roblox_package_ships_separate_metalness_and_roughness_beside_the_packed_map(roblox, maps):
    _, _, pm = roblox
    man = pm.data
    rec = man["roblox_surface_appearance"]
    entry = rec["materials"]["paint"]
    assert entry["packed_source"] == "textures/orm.png"  # the existing file name is kept
    assert {"textures/orm.png", "textures/albedo.png", "textures/normal.png"} <= set(man["files"])
    orm = np.asarray(Image.open(maps / "orm.png").convert("RGB"))
    root = pm.out_dir
    for key, channel in (("MetalnessMap", 2), ("RoughnessMap", 1)):
        rel = entry[key]
        assert rel in man["files"] and rel in man["file_hashes"]
        with Image.open(root / rel) as im:
            assert im.mode == "L" and im.size == orm.shape[:2]
            assert np.array_equal(np.asarray(im), orm[..., channel])
        assert hashlib.sha256((root / rel).read_bytes()).hexdigest() == man["file_hashes"][rel]["sha256"]
    assert entry["ColorMap"] == "textures/albedo.png" and entry["NormalMap"] == "textures/normal.png"
    note = next(n for n in man["notes"] if "Scale Unit" in n)
    assert "By default, this is set to Studs" in note and "metres" in note and "Meters" not in note  # no option the page does not list
    assert any("Studio" in u for u in rec["not_done"])
    assert rec["occlusion"].startswith("Roblox lists no occlusion map")
    assert rec["materials"]["paint"]["ColorMap_has_alpha"] is False
    quotes = " ".join(d["statement"] for d in rec["documented"])
    assert "1024\u00d71024 pixel spaces" in quotes and "4096\u00d74096" in quotes and "one MeshPart for each primitive" in quotes
    assert all(d["source"].startswith("https://create.roblox.com/docs/") for d in rec["documented"])
    assert rec["meshparts_expected"] == 3 and rec["texture_px_written"] == 64
    g = rec["size_guidance"]  # the asset's largest dimension in studs (28 cm each) picks the guidance row
    assert g["guidance_px"] == 256 if g["largest_dimension_studs"] <= 5 else g["guidance_px"] in (512, 1024)
    assert g["largest_dimension_m"] > 0 and "28 cm" in g["stud_definition"]
    assert "works in Roblox" not in json.dumps(rec)


def test_glb_keeps_the_packed_metallic_roughness_texture(roblox):
    _, _, pm = roblox
    doc = _glb_json(pm.out_dir / f"{ASSET}.glb")
    pbr = doc["materials"][0]["pbrMetallicRoughness"]
    assert "metallicRoughnessTexture" in pbr and "baseColorTexture" in pbr


def test_roblox_package_has_no_standing_size_warning_with_1024_maps_and_the_rule_is_not_relaxed(roblox, tmp_path):
    _, _, pm = roblox
    assert ENGINE_RULES["roblox_r15"].max_texture_px == 1024
    assert not [i for i in pm.issues if i.code.endswith("texture_size")]
    big = _sized_maps(tmp_path / "big", 2048)
    _, _, pm2 = _export(tmp_path / "e", big, "roblox_r15")
    assert [i for i in pm2.issues if i.code == "roblox_r15:texture_size"]  # a too-large map still warns


def test_generation_size_is_capped_to_the_engine_and_other_engines_keep_2048():
    assert engine_texture_px("roblox_r15", 2048) == 1024
    assert engine_texture_px("roblox_r15", 256) == 256
    for e in ("unreal_5", "unity_urp", "godot_4"):
        assert engine_texture_px(e, 2048) == 2048


def _mesh(node: str, material: str) -> MeshData:
    pts = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], float)
    return MeshData(node, f"/Prop/{node}", pts, np.array([[0, 1, 2]]), material)


def test_several_materials_on_roblox_warn_once_and_never_fail():
    two = check_engine_compliance(None, [_mesh("a", "wood"), _mesh("b", "iron")], EngineBudget("roblox_r15"))
    hit = [i for i in two if i.code == "roblox_r15:material_per_part"]
    assert len(hit) == 1 and hit[0].severity == "warning" and "MeshPart" in hit[0].detail and "one material assigned" in hit[0].detail
    one = check_engine_compliance(None, [_mesh("a", "wood"), _mesh("b", "wood")], EngineBudget("roblox_r15"))
    assert not [i for i in one if i.code.endswith("material_per_part")]
    other = check_engine_compliance(None, [_mesh("a", "wood"), _mesh("b", "iron")], EngineBudget("godot_4"))
    assert not [i for i in other if i.code.endswith("material_per_part")]


@pytest.mark.parametrize("engine", ["godot_4", "unreal_5", "unity_urp"])
def test_other_engines_packages_are_unchanged(engine, maps, tmp_path):
    _, _, pm = _export(tmp_path, maps, engine)
    man, root = pm.data, pm.out_dir
    assert "roblox_surface_appearance" not in man
    assert not [f for f in man["files"] if f.endswith(("_metalness.png", "_roughness.png"))]
    assert not [n for n in man["notes"] if "Roblox" in n]
    assert not [i for i in pm.issues if "material_per_part" in i.code]
    for f, rec in man["file_hashes"].items():  # copy-through textures are byte for byte the sources
        if f.startswith("textures/") and engine != "unreal_5":
            assert rec["sha256"] == hashlib.sha256((maps / Path(f).name).read_bytes()).hexdigest()
    assert {f"{ASSET}.usd", f"{ASSET}.glb", "textures/orm.png"} <= set(man["files"]) and len(man["files"]) <= 11
    # the writer is deterministic: a second write of the same scene is byte-identical
    _, _, again = _export(tmp_path / "again", maps, engine)
    assert {f: r["sha256"] for f, r in again.data["file_hashes"].items()} == {f: r["sha256"] for f, r in man["file_hashes"].items()}


# --------------------------------------------------------------------------- texel density across materials


def _prim(nid, kind, mat, **params):
    from antigravity3d.ir import NodeIR
    from antigravity3d.kernel.compile_ir import primitive_charts, primitive_mesh

    node = NodeIR(id=nid, kind=kind, params=params)
    p, t = primitive_mesh(node)
    return MeshData(nid, "", np.asarray(p, float), np.asarray(t), mat, True, primitive_charts(node))


def _wall_and_knob():
    from antigravity3d.kernel.fast_ops import unwrap_scene

    return unwrap_scene([_prim("wall", "box", "stone", size=(4.0, 0.3, 3.0)), _prim("knob", "box", "metal", size=(0.1, 0.1, 0.1))],
                        EngineBudget("unreal_5"))


def test_texel_density_report_for_the_wall_and_knob_case():
    from antigravity3d.kernel.fast_ops import describe_texel_density, texel_density_report
    from antigravity3d.kernel.validator import texel_density_issues

    uvs = _wall_and_knob()
    rep = texel_density_report(uvs, 1024.0)
    stone, metal = rep["materials"]["stone"], rep["materials"]["metal"]
    assert stone["target_met"] is False and metal["target_met"] is True
    assert stone["px_per_m"] == pytest.approx(323.0, abs=0.1) and metal["px_per_m"] == pytest.approx(1024.0)
    assert rep["spread"] == pytest.approx(3.17, abs=0.01) and (rep["highest"], rep["lowest"]) == ("metal", "stone")
    assert stone["atlas_px"] == 2048 and stone["needed_atlas_px"] == 6493 and stone["needed_atlas_px_pow2"] == 8192
    assert metal["needed_atlas_px"] == 2048
    assert "stone 323" in describe_texel_density(rep) and "spread 3.17x" in describe_texel_density(rep)
    issues = texel_density_issues(rep, "unreal_5")
    assert {i.code for i in issues} == {"unreal_5:texel_density_target", "unreal_5:texel_density_spread"}
    assert all(i.severity == "warning" for i in issues)
    target = next(i for i in issues if i.code.endswith("target"))
    assert "stone" in target.detail and "323" in target.detail and "8192" in target.detail
    spread = next(i for i in issues if i.code.endswith("spread"))
    assert "'metal'" in spread.detail and "'stone'" in spread.detail and "3.17x" in spread.detail


def test_no_density_warning_for_a_consistent_asset_and_a_near_miss_is_recorded_not_warned():
    from antigravity3d.kernel.fast_ops import texel_density_report
    from antigravity3d.kernel.validator import texel_density_issues

    from antigravity3d.kernel.fast_ops import unwrap_scene

    uvs = unwrap_scene([_prim("a", "box", "x", size=(0.2, 0.2, 0.2)), _prim("b", "box", "y", size=(0.1, 0.1, 0.1))], EngineBudget("unreal_5"))
    rep = texel_density_report(uvs, 1024.0)
    assert rep["spread"] == pytest.approx(1.0) and texel_density_issues(rep, "godot_4") == []
    near = {"target_px_per_m": 1024.0, "spread_limit": 1.3, "spread": 1.0, "highest": "s", "lowest": "s",
            "materials": {"s": {"px_per_m": 1021.5, "target_met": False, "atlas_px": 2048, "needed_atlas_px": 2052, "needed_atlas_px_pow2": 4096}}}
    assert texel_density_issues(near, "godot_4") == []


def test_manifest_uv_record_carries_per_material_density(tmp_path, maps):
    _, bundle, pm = _export(tmp_path, maps, "godot_4")
    rec = pm.data["uv"]["texel_density"]
    assert {"primvar", "interpolation", "normals"} <= set(pm.data["uv"])  # the existing keys stay
    assert {"mean_px_per_m", "min_px_per_m", "target_px_per_m"} <= set(pm.data["texel_density"])
    paint = rec["materials"]["paint"]
    assert paint["px_per_m"] == pytest.approx(min(u.texel_density for u in bundle.uvs), abs=0.01)
    assert paint["target_met"] == all(u.fitted for u in bundle.uvs)
    assert paint["shipped_px_per_m"] == pytest.approx(paint["px_per_m"] * 64 / paint["atlas_px"], abs=0.01)
    assert rec["spread"] == pytest.approx(1.0)


def test_size_guidance_rows():
    from antigravity3d.kernel.package import _roblox_texture_guidance

    assert [_roblox_texture_guidance(m)["guidance_px"] for m in (0.5, 1.4, 1.5, 2.8, 2.9, 10.0)] == [256, 256, 512, 512, 1024, 1024]


def test_alpha_in_the_albedo_is_recorded_and_the_file_is_not_changed(tmp_path):
    d = _sized_maps(tmp_path / "m", 32)
    Image.new("RGBA", (32, 32), (10, 20, 30, 128)).save(d / "albedo.png")
    before = hashlib.sha256((d / "albedo.png").read_bytes()).hexdigest()
    _, _, pm = _export(tmp_path / "e", d, "roblox_r15")
    assert pm.data["roblox_surface_appearance"]["materials"]["paint"]["ColorMap_has_alpha"] is True
    assert pm.data["file_hashes"]["textures/albedo.png"]["sha256"] == before


@pytest.mark.parametrize("engine", ["godot_4", "roblox_r15"])
def test_density_keys_say_what_the_shipped_maps_reach(engine, maps, tmp_path):
    _, _, pm = _export(tmp_path, maps, engine)
    td = pm.data["texel_density"]
    assert td["shipped_texture_px"] == 64 and td["space"].startswith("atlas (2048 px")
    assert td["shipped_mean_px_per_m"] == pytest.approx(td["mean_px_per_m"] * 64 / 2048, abs=0.02)


def test_occupancy_of_the_wall_and_knob_atlases():
    from antigravity3d.kernel.fast_ops import texel_density_report

    rep = texel_density_report(_wall_and_knob(), 1024.0)
    knob, wall = rep["materials"]["metal"], rep["materials"]["stone"]
    assert knob["chart_area_fraction"] < 0.02 and knob["uv_bbox_fraction"] < 0.05  # a 10 cm cube in a 2048 px atlas
    assert wall["chart_area_fraction"] > 0.5
