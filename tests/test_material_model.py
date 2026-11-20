"""The glTF PBR material model end to end: IR rules, USD homes, the one reader, the glTF derivation, the manifest."""

# ruff: noqa: C408
from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from pxr import Usd, UsdShade
from pydantic import ValidationError

from antigravity3d.ir import MaterialIR, NodeIR, SceneIR, Transform
from antigravity3d.kernel import package as P
from antigravity3d.kernel.compile_ir import author_scene, read_ir, read_material
from antigravity3d.kernel.fast_ops import run_export_pipeline
from antigravity3d.kernel.usd_stage import StageManager
from antigravity3d.kernel.validator import validate_package
from antigravity3d.state import EngineBudget

FULL = dict(
    base_color=(0.1, 0.2, 0.3), metallic=0.25, roughness=0.6, alpha_mode="BLEND", opacity=0.4, double_sided=True,
    emissive_color=(1.0, 0.5, 0.25), emissive_strength=4.0, clearcoat=0.8, clearcoat_roughness=0.1, transmission=0.7,
    ior=1.45, sheen_color=(0.2, 0.3, 0.4), sheen_roughness=0.5, specular=0.5, specular_color=(0.9, 0.8, 0.7))


def _png(path: Path, alpha: bool, seed: int = 1) -> str:
    rng = np.random.default_rng(seed)
    a = rng.integers(40, 220, (32, 32, 4 if alpha else 3), dtype=np.uint8)
    if alpha:
        a[..., 3] = np.where((np.arange(32)[:, None] + np.arange(32)[None]) % 2 == 0, 255, 0)
    Image.fromarray(a).save(path)
    return str(path)


def _scene(*mats: MaterialIR) -> SceneIR:
    nodes = [NodeIR(id=f"n{i}", kind="box", params={"size": [0.4, 0.4, 0.4]}, transform=Transform(translate=(0.5 * i, 0, 0.2)),
                    material_id=m.id) for i, m in enumerate(mats)]
    return SceneIR(asset_name="Mix", nodes=nodes, materials=list(mats))


def _factors(m: MaterialIR) -> dict:
    keys = {"base_color", "metallic", "roughness", "alpha_mode", "alpha_cutoff", "opacity", "double_sided", "emissive_color",
            "emissive_strength", "clearcoat", "clearcoat_roughness", "transmission", "ior", "sheen_color", "sheen_roughness",
            "specular", "specular_color"}
    return {k: v for k, v in m.model_dump().items() if k in keys}


def _assert_same(read: dict, want: dict) -> None:
    assert read.keys() == want.keys()
    for k, w in want.items():
        if isinstance(w, (tuple, list, float)):
            assert read[k] == pytest.approx(w, abs=1e-6), k
        else:
            assert read[k] == w, k


# --------------------------------------------------------------------------- IR


def test_defaults_are_the_gltf_defaults_and_old_documents_validate() -> None:
    m = MaterialIR(id="a")
    assert (m.alpha_mode, m.opacity, m.emissive_color, m.emissive_strength, m.ior, m.specular, m.double_sided) == (
        "OPAQUE", 1.0, (0.0, 0.0, 0.0), 1.0, 1.5, 1.0, False)
    old = {"id": "a", "base_color": [0.5, 0.5, 0.5], "metallic": 1.0, "roughness": 0.3, "albedo_uri": None, "normal_convention": "opengl"}
    assert MaterialIR.model_validate(old) == MaterialIR(id="a", base_color=(0.5, 0.5, 0.5), metallic=1.0, roughness=0.3)
    assert MaterialIR.model_validate_json(MaterialIR(**FULL, id="a").model_dump_json()) == MaterialIR(**FULL, id="a")


@pytest.mark.parametrize("fields,needle", [
    (dict(alpha_mode="MASK", albedo_uri="a.png"), "alpha_cutoff"),
    (dict(alpha_mode="MASK", alpha_cutoff=0.5), "albedo_uri"),
    (dict(alpha_cutoff=0.5), "only to alpha_mode MASK"),
    (dict(alpha_mode="MASK", albedo_uri="a.png", alpha_cutoff=0.0), "greater than 0"),
    (dict(opacity=0.5), "set alpha_mode BLEND"),
    (dict(alpha_mode="BLEND"), "blends nothing"),
    (dict(alpha_mode="BLEND", albedo_uri="a.png", opacity=0.5), "bake the fade"),
    (dict(clearcoat_roughness=0.2), "clearcoat is 0"),
    (dict(transmission=0.5, metallic=1.0), "fully metallic"),
    (dict(sheen_roughness=0.2), "sheen_color is black"),
    (dict(emissive_strength=3.0), "emissive_color is black"),
    (dict(emissive_uri="e.png"), "non-black emissive_color"),
    (dict(specular=0.0, specular_color=(0.5, 0.5, 0.5)), "specular is 0"),
    (dict(ior=0.9), "greater than or equal to 1"),
    (dict(clearcoat=1.5), "less than or equal to 1"),
    (dict(emissive_color=(2.0, 0.0, 0.0)), "within"),
    (dict(alpha_mode="MULTIPLY"), "OPAQUE"),
])
def test_incoherent_or_out_of_range_material_is_rejected_with_an_actionable_message(fields, needle) -> None:
    with pytest.raises(ValidationError, match=needle):
        MaterialIR(id="x", **fields)


def test_transmission_on_a_metallic_map_is_not_judged_without_the_map() -> None:
    assert MaterialIR(id="x", transmission=0.5, metallic=1.0, orm_uri="orm.png").transmission == 0.5  # the map decides


# --------------------------------------------------------------------------- USD: authored once, read once


def test_round_trip_every_field_then_clear_back_to_defaults_across_layers(tmp_path: Path) -> None:
    sm = StageManager.create(tmp_path / "s.usda", "Mix")
    full = MaterialIR(id="g", **FULL)
    sm.apply_ir(_scene(full))
    mat = UsdShade.Material(sm.stage.GetPrimAtPath("/Mix/Looks/g"))
    _assert_same(read_material(mat).factors(), _factors(full))
    assert sm.stage.GetPrimAtPath("/Mix/n0").GetAttribute("doubleSided").Get() is True

    plain = MaterialIR(id="g")  # an edit (a later layer) that clears every new field back to its default
    sm.apply_ir(_scene(plain))
    mat = UsdShade.Material(sm.stage.GetPrimAtPath("/Mix/Looks/g"))
    _assert_same(read_material(mat).factors(), _factors(plain))
    assert not sm.stage.GetPrimAtPath("/Mix/n0").GetAttribute("doubleSided").Get()
    assert read_ir(sm.stage.GetPrimAtPath("/Mix")).materials == [plain]


def test_double_sidedness_follows_assignment_and_material_edits(tmp_path: Path) -> None:
    stage = Usd.Stage.CreateInMemory()
    two, one = MaterialIR(id="two", double_sided=True), MaterialIR(id="one")
    base = _scene(two, one)
    author_scene(stage, base)
    sided = lambda n: bool(stage.GetPrimAtPath(f"/Mix/{n}").GetAttribute("doubleSided").Get())
    assert (sided("n0"), sided("n1")) == (True, False)
    swapped = base.model_copy(update={"nodes": [base.nodes[0].model_copy(update={"material_id": "one"}),
                                                  base.nodes[1].model_copy(update={"material_id": "two"})]})
    author_scene(stage, swapped)
    assert (sided("n0"), sided("n1")) == (False, True)
    author_scene(stage, swapped.model_copy(update={"materials": [two.model_copy(update={"double_sided": False}), one]}))
    assert (sided("n0"), sided("n1")) == (False, False)


def test_textures_alpha_from_albedo_and_emissive_map_are_wired_and_unwired(tmp_path: Path) -> None:
    a, e = _png(tmp_path / "a.png", True), _png(tmp_path / "e.png", False, 2)
    stage = Usd.Stage.CreateInMemory()
    leaf = MaterialIR(id="m", albedo_uri=a, alpha_mode="MASK", alpha_cutoff=0.4, emissive_color=(1, 1, 1), emissive_uri=e)
    author_scene(stage, _scene(leaf))
    p = read_material(UsdShade.Material(stage.GetPrimAtPath("/Mix/Looks/m")))
    assert set(p.maps) == {"albedo", "emissive"} and p.alpha_from_albedo and p.alpha_mode == "MASK"
    assert p.alpha_cutoff == pytest.approx(0.4) and p.maps["emissive"].color_space == "sRGB"
    author_scene(stage, _scene(MaterialIR(id="m")))
    p = read_material(UsdShade.Material(stage.GetPrimAtPath("/Mix/Looks/m")))
    assert not p.maps and not p.alpha_from_albedo and p.alpha_mode == "OPAQUE" and p.alpha_cutoff is None
    surf = UsdShade.Shader(stage.GetPrimAtPath("/Mix/Looks/m/Surface"))
    assert not any(surf.GetInput(n).HasConnectedSource() for n in ("diffuseColor", "emissiveColor", "opacity"))


def test_a_stage_with_no_new_opinions_stays_free_of_them() -> None:
    """Existing assets: not one new attribute on the material or its surface, so their USD (and GLB) are unchanged."""
    stage = Usd.Stage.CreateInMemory()
    author_scene(stage, _scene(MaterialIR(id="m", metallic=1.0)))
    surf = UsdShade.Shader(stage.GetPrimAtPath("/Mix/Looks/m/Surface"))
    assert sorted(i.GetBaseName() for i in surf.GetInputs()) == ["diffuseColor", "metallic", "roughness"]
    assert not UsdShade.Material(stage.GetPrimAtPath("/Mix/Looks/m")).GetInputs()
    assert not stage.GetPrimAtPath("/Mix/n0").GetAttribute("doubleSided").HasAuthoredValue()


def test_reader_understands_usd_preview_surface_semantics_without_alpha_mode() -> None:
    stage = Usd.Stage.CreateInMemory()
    author_scene(stage, _scene(MaterialIR(id="m")))
    surf = UsdShade.Shader(stage.GetPrimAtPath("/Mix/Looks/m/Surface"))
    mat = UsdShade.Material(stage.GetPrimAtPath("/Mix/Looks/m"))
    from pxr import Sdf
    surf.CreateInput("opacityThreshold", Sdf.ValueTypeNames.Float).Set(0.3)
    assert read_material(mat).alpha_mode == "MASK" and read_material(mat).alpha_cutoff == pytest.approx(0.3)
    surf.GetInput("opacityThreshold").Set(0.0)
    surf.CreateInput("opacity", Sdf.ValueTypeNames.Float).Set(0.5)
    assert read_material(mat).alpha_mode == "BLEND"


# --------------------------------------------------------------------------- glTF derivation


def _gltf(stage, scene):
    meshes = [P.MeshData(n.id, f"/Mix/{n.id}", np.zeros((0, 3)), np.zeros((0, 3), np.int32), n.material_id) for n in scene.nodes]
    return P.gltf_materials(stage, meshes)


def test_gltf_material_writes_core_fields_and_only_the_extensions_that_differ_from_defaults(tmp_path: Path) -> None:
    leaf = MaterialIR(id="leaf", albedo_uri=_png(tmp_path / "a.png", True), alpha_mode="MASK", alpha_cutoff=0.4, double_sided=True)
    mats = [MaterialIR(id="plain"), leaf, MaterialIR(id="lamp", emissive_color=(1, 0.5, 0), emissive_strength=3.0),
            MaterialIR(id="all", **{**FULL, "alpha_mode": "OPAQUE", "opacity": 1.0})]
    scene = _scene(*mats)
    stage = Usd.Stage.CreateInMemory()
    author_scene(stage, scene)
    got = _gltf(stage, scene)
    plain = got["n0"]
    assert (plain.extensions, plain.emissive_factor, plain.alpha_mode, plain.alpha_cutoff, plain.double_sided) == ({}, None, None, None, False)
    assert plain.base_color_factor[3] == 1.0
    assert (got["n1"].alpha_mode, got["n1"].alpha_cutoff, got["n1"].double_sided) == ("MASK", pytest.approx(0.4), True)
    assert got["n1"].base_color_texture is not None and list(got["n1"].base_color_factor) == [1.0, 1.0, 1.0, 1.0]
    assert got["n2"].emissive_factor == pytest.approx([1, 0.5, 0]) and got["n2"].extensions == {
        "KHR_materials_emissive_strength": {"emissiveStrength": 3.0}}
    assert list(got["n3"].extensions) == [
        "KHR_materials_emissive_strength", "KHR_materials_clearcoat", "KHR_materials_transmission", "KHR_materials_ior",
        "KHR_materials_sheen", "KHR_materials_specular"]
    assert got["n3"].extensions["KHR_materials_ior"]["ior"] == pytest.approx(1.45)


def test_a_texture_missing_on_disk_falls_back_to_its_factor(tmp_path: Path) -> None:
    scene = _scene(MaterialIR(id="m", base_color=(0.1, 0.2, 0.3), albedo_uri=str(tmp_path / "gone.png")))
    stage = Usd.Stage.CreateInMemory()
    author_scene(stage, scene)
    m = _gltf(stage, scene)["n0"]
    assert m.base_color_texture is None and list(m.base_color_factor[:3]) == pytest.approx([0.1, 0.2, 0.3])


# --------------------------------------------------------------------------- package


def _package(root: Path, *, mats=None) -> tuple[Path, dict]:
    root.mkdir(parents=True, exist_ok=True)
    leaf, em = _png(root / "leaf.png", True), _png(root / "em.png", False, 2)
    mats = mats or [
        MaterialIR(id="iron", base_color=(0.5, 0.5, 0.55), metallic=1.0),
        MaterialIR(id="glass", base_color=(0.9, 0.95, 1.0), roughness=0.05, transmission=0.9, ior=1.45, double_sided=True),
        MaterialIR(id="leaf", albedo_uri=leaf, alpha_mode="MASK", alpha_cutoff=0.5, double_sided=True),
        MaterialIR(id="lamp", emissive_color=(1, 0.8, 0.4), emissive_strength=5.0, emissive_uri=em),
        MaterialIR(id="paint", base_color=(0.6, 0.05, 0.05), clearcoat=1.0, clearcoat_roughness=0.05),
        MaterialIR(id="cloth", base_color=(0.3, 0.2, 0.5), sheen_color=(0.8, 0.7, 0.9), sheen_roughness=0.4, specular=0.5),
        MaterialIR(id="film", base_color=(0.2, 0.6, 0.9), alpha_mode="BLEND", opacity=0.35)]
    sm = StageManager.create(root / "s.usda", "Mix")
    sm.apply_ir(_scene(*mats))
    budget = EngineBudget("godot_4", max_draw_calls=16)
    manifest = P.write_engine_package(sm, run_export_pipeline(sm.extract_meshes(), budget, "Mix"), budget, root / "pkg", asset_name="Mix")
    return root / "pkg", manifest.data


def _glb_doc(path: Path) -> dict:
    raw = path.read_bytes()
    n = int.from_bytes(raw[12:16], "little")
    return json.loads(raw[20:20 + n])


@pytest.fixture(scope="module")
def pkg(tmp_path_factory):
    return _package(tmp_path_factory.mktemp("mix"))


def test_package_manifest_states_the_exported_materials_and_the_glb_agrees(pkg) -> None:
    d, man = pkg
    assert man["validation"]["package_ok"], man["validation"]
    inv = {m["name"]: m for m in man["materials"]}
    assert set(inv) == {"iron", "glass", "leaf", "lamp", "paint", "cloth", "film"}
    assert inv["glass"]["extensions"]["KHR_materials_transmission"]["transmissionFactor"] == pytest.approx(0.9) and inv["glass"]["double_sided"]
    assert inv["leaf"]["alpha_mode"] == "MASK" and inv["leaf"]["alpha_cutoff"] == pytest.approx(0.5) and inv["leaf"]["texture_slots"] == ["baseColorTexture"]
    assert inv["lamp"]["emissive_factor"] == pytest.approx([1, 0.8, 0.4]) and inv["lamp"]["texture_slots"] == ["emissiveTexture"]
    assert inv["film"]["alpha_mode"] == "BLEND" and inv["film"]["base_color_factor"][3] == pytest.approx(0.35)
    assert inv["iron"]["extensions"] == {} and inv["iron"]["alpha_mode"] == "OPAQUE"
    assert {t["role"] for t in man["textures"]} == {"albedo", "emissive"}
    doc = _glb_doc(d / "Mix.glb")
    assert "extensionsRequired" not in doc and set(doc["extensionsUsed"]) == {
        "KHR_materials_transmission", "KHR_materials_ior", "KHR_materials_emissive_strength", "KHR_materials_clearcoat",
        "KHR_materials_sheen", "KHR_materials_specular"}
    by_name = {m["name"]: m for m in doc["materials"]}
    assert by_name["iron"].keys() <= {"name", "pbrMetallicRoughness", "doubleSided", "alphaMode"}  # nothing new leaks in
    assert by_name["iron"].get("alphaMode", "OPAQUE") == "OPAQUE"
    assert by_name["leaf"]["alphaMode"] == "MASK" and by_name["leaf"]["doubleSided"] is True


def test_tampering_with_the_inventory_is_a_critical_package_issue(pkg, tmp_path) -> None:
    d, _ = pkg
    copy = tmp_path / "p"
    shutil.copytree(d, copy)
    assert not [i for i in validate_package(copy) if i.severity == "critical"]
    man = json.loads((copy / "manifest.json").read_text())
    next(m for m in man["materials"] if m["name"] == "glass")["extensions"]["KHR_materials_transmission"]["transmissionFactor"] = 0.1
    (copy / "manifest.json").write_text(json.dumps(man))
    assert any(i.code == "pkg:material_inventory" and i.severity == "critical" for i in validate_package(copy))


def test_package_without_new_parameters_has_no_trace_of_them_in_its_glb(tmp_path) -> None:
    _, man = _package(tmp_path / "plain", mats=[MaterialIR(id="a", metallic=1.0), MaterialIR(id="b", albedo_uri=_png(tmp_path / "t.png", False))])
    doc = _glb_doc(tmp_path / "plain" / "pkg" / "Mix.glb")
    assert "extensionsUsed" not in doc
    assert all(not (m.keys() - {"name", "pbrMetallicRoughness", "doubleSided", "alphaMode", "normalTexture", "occlusionTexture"})
               and m.get("alphaMode", "OPAQUE") == "OPAQUE" for m in doc["materials"])
    assert all(m["extensions"] == {} and m["alpha_mode"] == "OPAQUE" for m in man["materials"])


def test_shipped_ir_names_the_packaged_textures_and_a_relocated_package_reopens(tmp_path) -> None:
    d, _ = _package(tmp_path / "a")
    shipped = Usd.Stage.Open(str(d / "Mix.usd"))
    ir = read_ir(shipped.GetDefaultPrim())
    uris = {m.id: (m.albedo_uri, m.emissive_uri) for m in ir.materials}
    assert uris["leaf"][0] == "./textures/leaf.png" and uris["lamp"][1] == "./textures/em.png"
    assert str(tmp_path) not in (d / "Mix.usd").read_text(errors="ignore")
    moved = tmp_path / "elsewhere" / "pkg"
    shutil.copytree(d, moved)
    shutil.rmtree(d)
    sm = StageManager.open(moved / "Mix.usd")
    sm.apply_ir(sm.base_ir().model_copy(update={"materials": [m.model_copy(update={"roughness": 0.9}) for m in sm.base_ir().materials]}))
    p = read_material(UsdShade.Material(sm.stage.GetPrimAtPath("/Mix/Looks/leaf")))
    assert Path(p.maps["albedo"].resolved) == (moved / "textures" / "leaf.png").resolve()


def test_alpha_mode_reading_an_albedo_without_alpha_warns(tmp_path) -> None:
    mats = [MaterialIR(id="leaf", albedo_uri=_png(tmp_path / "rgb.png", False), alpha_mode="MASK", alpha_cutoff=0.5)]
    _, man = _package(tmp_path / "w", mats=mats)
    issues = man["validation"]["engine"]
    assert any(i["code"].endswith("material_alpha") and i["severity"] == "warning" for i in issues), issues


def test_live_session_derivation_flips_a_directx_normal_map_in_memory(tmp_path) -> None:
    n = _png(tmp_path / "n.png", False, 5)
    scene = _scene(MaterialIR(id="m", normal_uri=n, normal_convention="directx"))
    stage = Usd.Stage.CreateInMemory()
    author_scene(stage, scene)
    ref = np.asarray(Image.open(n).convert("RGB"))
    out = _gltf(stage, scene)["n0"].normal_texture.image
    flipped = np.asarray(Image.open(io.BytesIO(out.data)).convert("RGB"))
    assert (flipped[..., 1] == 255 - ref[..., 1]).all() and (flipped[..., 0] == ref[..., 0]).all()


def test_inventory_lists_the_default_material_of_unbound_meshes(tmp_path) -> None:
    root = tmp_path / "u"
    root.mkdir()
    sm = StageManager.create(root / "s.usda", "Mix")
    sm.apply_ir(SceneIR(asset_name="Mix", nodes=[NodeIR(id="n0", kind="box", params={"size": [1, 1, 1]})]))
    budget = EngineBudget("godot_4")
    man = P.write_engine_package(sm, run_export_pipeline(sm.extract_meshes(), budget, "Mix"), budget, root / "pkg", asset_name="Mix").data
    assert [m["name"] for m in man["materials"]] == ["Default"] and man["validation"]["package_ok"]


def test_agent_facing_rules_name_the_new_fields_and_their_cross_rules() -> None:
    from antigravity3d import ir_schema

    text = ir_schema.ir_rules_text()
    for field in ("alpha_mode", "alpha_cutoff", "opacity", "double_sided", "emissive_color", "emissive_strength", "emissive_uri",
                  "clearcoat", "transmission", "ior", "sheen_color", "specular_color"):
        assert field in text
    assert "MASK needs alpha_cutoff" in text


def test_double_sided_follows_the_resolved_binding_through_a_group(tmp_path: Path) -> None:
    two, one = MaterialIR(id="two", double_sided=True), MaterialIR(id="one")
    nodes = [NodeIR(id="grp", kind="group", material_id="two"),
             NodeIR(id="a", kind="box", params={"size": [1, 1, 1]}, parent="grp"),
             NodeIR(id="b", kind="box", params={"size": [1, 1, 1]}, parent="grp", transform=Transform(translate=(2, 0, 0)))]
    scene = SceneIR(asset_name="Mix", nodes=nodes, materials=[two, one])
    stage = Usd.Stage.CreateInMemory()
    author_scene(stage, scene)
    sided = lambda n: bool(stage.GetPrimAtPath(f"/Mix/grp/{n}").GetAttribute("doubleSided").Get())
    assert (sided("a"), sided("b")) == (True, True)  # both inherit the group's binding
    assert read_material(UsdShade.Material(stage.GetPrimAtPath("/Mix/Looks/two"))).double_sided
    rebound = scene.model_copy(update={"nodes": [nodes[0], nodes[1], nodes[2].model_copy(update={"material_id": "one"})]})
    author_scene(stage, rebound)
    assert (sided("a"), sided("b")) == (True, False)  # b stopped resolving to a double-sided material: opinion gone
    author_scene(stage, rebound.model_copy(update={"materials": [two.model_copy(update={"double_sided": False}), one]}))
    assert (sided("a"), sided("b")) == (False, False)
    assert not read_material(UsdShade.Material(stage.GetPrimAtPath("/Mix/Looks/two"))).double_sided


def test_write_package_usd_keeps_the_whole_material_including_double_sided(tmp_path: Path) -> None:
    from antigravity3d.kernel.fast_ops import UVResult
    from antigravity3d.tactics.package import write_package_usd

    pts = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], float)
    uv = UVResult("leaf", pts, np.array([[0, 1, 2]], np.int32), pts[:, :2].astype(np.float32), np.arange(3), 0.0, 0.0, 0.0, 64)
    other = UVResult("rock", pts + 3, np.array([[0, 1, 2]], np.int32), pts[:, :2].astype(np.float32), np.arange(3), 0.0, 0.0, 0.0, 64)
    mats = [MaterialIR(id="leaf_m", **FULL), MaterialIR(id="rock_m")]
    path = write_package_usd(tmp_path / "Kit.usda", "Kit", [uv, other], {"leaf": "leaf_m", "rock": "rock_m"}, mats, [], "godot_4")
    stage = Usd.Stage.Open(str(path))
    got = read_material(UsdShade.Material(stage.GetPrimAtPath("/Kit/Looks/leaf_m")))
    _assert_same(got.factors(), _factors(mats[0]))
    assert not read_material(UsdShade.Material(stage.GetPrimAtPath("/Kit/Looks/rock_m"))).double_sided
    assert stage.GetPrimAtPath("/Kit/leaf").GetAttribute("doubleSided").Get() is True
    assert not stage.GetPrimAtPath("/Kit/rock").GetAttribute("doubleSided").Get()
