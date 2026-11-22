"""One texture set and one draw call per opaque prop: the shared atlas (fast_ops.unwrap_scene), its size from need
(squares and two-to-one rectangles), the bake (package._compose_atlas), the merged glTF material, what forces a
split, and the working stage sampling the maps as the package ships them (usd_stage.repeat_maps).

The last test renders the working stage and the shipped GLB with a real Blender (the lookdev renders go through
Blender's USD importer) and is in the engine qualification's required set."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from pxr import Usd, UsdShade

from antigravity3d.ir import MaterialIR, NodeIR, SceneIR, Transform
from antigravity3d.kernel import fast_ops
from antigravity3d.kernel.coplanar import opaque_materials
from antigravity3d.kernel.fast_ops import ATLAS_SIZES, SWATCH_SPAN_PX, run_export_pipeline, unwrap_scene
from antigravity3d.kernel.package import _flat_texel, _owner_labels, merge_materials, write_engine_package
from antigravity3d.kernel.usd_stage import MAP_REPEAT, StageManager
from antigravity3d.state import EngineBudget

ASSET = "Crate"


def checker(path: Path, size: int, cells: int, a: tuple[int, int, int], b: tuple[int, int, int]) -> str:
    """A tileable checker: ``cells`` squares across, so its scale on a surface is plain to see and to measure."""
    k = size // cells
    idx = (np.arange(size)[:, None] // k + np.arange(size)[None, :] // k) % 2
    Image.fromarray(np.where(idx[..., None] == 0, np.array(a, np.uint8), np.array(b, np.uint8))).save(path)
    return str(path)


def flat(path: Path, size: int, rgb: tuple[int, int, int]) -> str:
    Image.new("RGB", (size, size), rgb).save(path)
    return str(path)


def two_material_scene(d: Path, *, untextured_iron: bool = False, iron: dict | None = None) -> SceneIR:
    """A 0.4 m crate body (wood) with a band round it (iron): two opaque materials, both with tileable maps unless
    asked. About 1 m2 of surface: at 1024 px/m its shared atlas is not 2048 square (so the map repeat is not 1)."""
    wood = MaterialIR(id="wood", albedo_uri=checker(d / "wood_albedo.png", 1024, 32, (200, 140, 60), (90, 50, 20)),
                      orm_uri=flat(d / "wood_orm.png", 1024, (255, 200, 0)), normal_uri=flat(d / "wood_normal.png", 1024, (128, 128, 255)))
    if untextured_iron:
        metal = MaterialIR(id="iron", base_color=(0.2, 0.3, 0.6), metallic=1.0, roughness=0.35, **(iron or {}))
    else:
        metal = MaterialIR(id="iron", albedo_uri=checker(d / "iron_albedo.png", 1024, 16, (40, 60, 160), (220, 220, 230)),
                           orm_uri=flat(d / "iron_orm.png", 1024, (255, 90, 255)), **(iron or {}))
    return SceneIR(asset_name=ASSET, materials=[wood, metal], nodes=[
        NodeIR(id="body", kind="box", params={"size": (0.4, 0.3, 0.3)}, transform=Transform(translate=(0, 0, 0.15)), material_id="wood"),
        NodeIR(id="band", kind="box", params={"size": (0.42, 0.32, 0.05)}, transform=Transform(translate=(0, 0, 0.2)), material_id="iron"),
    ])


def build(d: Path, scene: SceneIR, budget: EngineBudget):
    sm = StageManager.create(d / "work.usda", scene.asset_name)
    sm.apply_ir(scene)
    bundle = run_export_pipeline(sm.extract_meshes(), budget, scene.asset_name, opaque_materials=opaque_materials(scene))
    pm = write_engine_package(sm, bundle, budget, d / "pkg", asset_name=scene.asset_name)
    return sm, bundle, pm


# --------------------------------------------------------------------------- layout


def _boxes(n: int, size: float):
    from antigravity3d.kernel.compile_ir import primitive_charts, primitive_mesh
    from antigravity3d.kernel.types import MeshData

    out = []
    for i in range(n):
        node = NodeIR(id=f"b{i}", kind="box", params={"size": (size, size, size)})
        p, t = primitive_mesh(node)
        out.append(MeshData(f"b{i}", f"/A/b{i}", p + [2.0 * i, 0, 0], t, f"m{i % 3}", uv_charts=primitive_charts(node)))
    return out


def test_opaque_materials_share_one_atlas_sized_from_need():
    meshes = _boxes(6, 0.3)
    b = EngineBudget("godot_4")
    shared = unwrap_scene(meshes, b, shared={"m0", "m1", "m2"})
    alone = unwrap_scene(meshes, b)
    assert {u.atlas_id for u in shared} == {"m0+m1+m2"} and all(u.shared_atlas for u in shared)
    assert {u.atlas_id for u in alone} == {"m0", "m1", "m2"} and {u.size for u in alone} == {(2048, 2048)}
    # six 0.3 m cubes: 3.24 m2 at 1024 px/m is 3.4 Mpx of charts: 2048 x 2048 holds them, 2048 x 1024 does not
    assert shared[0].size in ATLAS_SIZES and all(u.fitted for u in shared)
    assert min(u.texel_density for u in shared) == pytest.approx(1024.0)
    w, h = shared[0].size
    tried = [(tw, th) for tw, th, _ in shared[0].size_trials]
    assert tried[-1] == (w, h) and all(tw * th <= w * h for tw, th in tried)  # smallest by area that meets the target
    assert all(d < 1024.0 for _, _, d in shared[0].size_trials[:-1])
    assert shared[0].repeat_px == SWATCH_SPAN_PX  # the target is met: one map repeat is 2048 atlas px
    # every chart inside the atlas, none overlapping (raster check of the shipped triangles)
    assert fast_ops.uv_overlap_fraction(shared) == 0.0
    assert all(((u.uvs >= 0) & (u.uvs <= 1)).all() for u in shared)


def test_a_two_to_one_atlas_keeps_square_texels():
    """u is divided by the width and v by the height: a chart keeps one density along both axes."""
    meshes = _boxes(3, 0.5)
    u = unwrap_scene(meshes, EngineBudget("godot_4"), shared={"m0", "m1", "m2"}, shared_resolution=(2048, 1024))[0]
    assert u.size == (2048, 1024)
    pts, tri, uv = u.points, u.triangles, u.uvs.astype(np.float64) * [2048, 1024]
    for t in tri[:12]:  # each face of a cube is a square in 3D: its chart must be a square in texels
        e3 = np.linalg.norm(pts[t[1]] - pts[t[0]]), np.linalg.norm(pts[t[2]] - pts[t[1]])
        e2 = np.linalg.norm(uv[t[1]] - uv[t[0]]), np.linalg.norm(uv[t[2]] - uv[t[1]])
        assert e2[0] / e3[0] == pytest.approx(e2[1] / e3[1], rel=1e-3)


def test_the_engine_and_the_art_direction_cap_the_atlas():
    meshes = _boxes(6, 0.3)
    rob = unwrap_scene(meshes, EngineBudget("roblox_r15"), shared={"m0", "m1", "m2"})
    assert max(rob[0].size) <= 1024 and not rob[0].fitted  # Roblox: 1024; the density that fits, reported as missed
    art = unwrap_scene(meshes, EngineBudget("godot_4"), shared={"m0", "m1", "m2"}, max_px=512)
    assert max(art[0].size) <= 512
    from antigravity3d.art_direction import ArtDirection

    assert ArtDirection("t", "t", texture_px_prop=512).budget(EngineBudget("godot_4")).max_texture_px == 512


def test_without_shared_atlas_the_layout_is_one_2048_atlas_per_material(tmp_path):
    scene = two_material_scene(tmp_path)
    _, bundle, pm = build(tmp_path, scene, EngineBudget("godot_4", shared_atlas=False))
    assert {u.atlas_id for u in bundle.uvs} == {"wood", "iron"} and {u.size for u in bundle.uvs} == {(2048, 2048)}
    assert "texture_atlas" not in pm.data and pm.data["draw_calls"] == 2


# --------------------------------------------------------------------------- the bake


def test_two_textured_materials_ship_one_material_one_draw_call_three_maps(tmp_path):
    scene = two_material_scene(tmp_path)
    _, bundle, pm = build(tmp_path, scene, EngineBudget("godot_4"))
    assert pm.ok, [i for i in pm.issues if i.severity == "critical"]
    d = pm.data
    assert d["draw_calls"] == 1 and len(d["glb_batches"]) == 1
    assert [m["name"] for m in d["materials"]] == [f"{ASSET}_atlas"]
    ta = d["texture_atlas"]
    assert ta["status"] == "baked" and ta["gltf_materials"] == [{"material": f"{ASSET}_atlas", "from": ["iron", "wood"]}]
    assert sorted(ta["roles"]) == ["albedo", "normal", "orm"] and ta["splits"] == [] and ta["draw_calls"] == 1
    w, h = bundle.uvs[0].size
    files = sorted({t["usd_path"] for t in d["textures"]})
    assert files == sorted(ta["files"].values())  # the baked set is the package's only texture set
    for f in files:
        with Image.open(tmp_path / "pkg" / f) as im:
            assert im.size == (w, h)
    import pygltflib

    g = pygltflib.GLTF2().load(str(tmp_path / "pkg" / f"{ASSET}.glb"))
    assert len(g.materials) == 1 and len(g.images) == 3 and sum(len(m.primitives) for m in g.meshes) == 1
    # the USD keeps one material per surface, each reading the baked files; its st goes straight to the reader
    stage = Usd.Stage.Open(str(tmp_path / "pkg" / f"{ASSET}.usd"))
    for name in ("wood", "iron"):
        mat = stage.GetPrimAtPath(f"/{ASSET}/Looks/{name}")
        assert not mat.GetChild(MAP_REPEAT)
        tex = {c.GetName(): UsdShade.Shader(c).GetInput("file").Get().path for c in mat.GetChildren() if c.GetName().endswith("Tex")}
        assert sorted(Path(p).name for p in tex.values()) == sorted(Path(f).name for f in files)


def test_baked_texels_are_each_materials_map_at_the_same_uv(tmp_path):
    """Inside every chart the baked map is the owning material's map, repeated every repeat_px atlas px from uv (0, 0)."""
    scene = two_material_scene(tmp_path)
    _, bundle, pm = build(tmp_path, scene, EngineBudget("godot_4"))
    w, h = bundle.uvs[0].size
    rep = bundle.uvs[0].repeat_px
    baked = np.asarray(Image.open(tmp_path / "pkg" / pm.data["texture_atlas"]["files"]["albedo"]).convert("RGB"))
    by_mat = {"wood": tmp_path / "wood_albedo.png", "iron": tmp_path / "iron_albedo.png"}
    node_mat = {"body": "wood", "band": "iron"}
    for u in bundle.uvs:
        src = np.asarray(Image.open(by_mat[node_mat[u.node_id]]).convert("RGB").resize((rep, rep), Image.Resampling.LANCZOS))
        _, drawn = _owner_labels((w, h), [(1, u.uvs, fast_ops.shipped(u).triangles)])
        rows, cols = np.nonzero(drawn)
        # texel (row, col) is at st = ((col + .5) / w, 1 - (row + .5) / h): repeat px from the bottom left
        sy = (h - 1 - rows) % rep
        want = src[rep - 1 - sy, cols % rep]
        assert (np.abs(baked[rows, cols].astype(int) - want.astype(int)).max(axis=1) <= 1).mean() > 0.999


def test_gutters_take_the_nearest_charts_material():
    lab, drawn = _owner_labels((64, 32), [(1, np.array([[0.05, 0.1], [0.3, 0.1], [0.05, 0.9]]), np.array([[0, 1, 2]])),
                                          (2, np.array([[0.7, 0.1], [0.95, 0.1], [0.95, 0.9]]), np.array([[0, 1, 2]]))])
    assert lab.shape == (32, 64) and set(np.unique(drawn)) == {0, 1, 2} and (lab > 0).all()
    assert (lab[:, :20] == 1).all() and (lab[:, 44:] == 2).all()  # each side of the gap mips from its own material


def test_an_untextured_material_is_a_flat_patch_of_its_factors(tmp_path):
    scene = two_material_scene(tmp_path, untextured_iron=True)
    _, bundle, pm = build(tmp_path, scene, EngineBudget("godot_4"))
    ta = pm.data["texture_atlas"]
    from antigravity3d.kernel.compile_ir import MaterialParams

    assert pm.data["draw_calls"] == 1
    assert ta["members"]["iron"]["flat"]["albedo"] == list(_flat_texel(MaterialParams("iron", base_color=(0.2, 0.3, 0.6)), "albedo"))
    assert ta["members"]["iron"]["flat"]["orm"] == [255, round(0.35 * 255), 255]
    w, h = bundle.uvs[0].size
    orm = np.asarray(Image.open(tmp_path / "pkg" / ta["files"]["orm"]).convert("RGB"))
    band = next(u for u in bundle.uvs if u.node_id == "band")
    _, drawn = _owner_labels((w, h), [(1, band.uvs, fast_ops.shipped(band).triangles)])
    assert (orm[drawn > 0] == [255, 89, 255]).all()


@pytest.mark.parametrize("change, field", [({"clearcoat": 0.5}, "extensions"), ({"emissive_color": (1.0, 0.5, 0.0), "emissive_strength": 4.0}, "extensions")])
def test_a_per_material_property_keeps_its_own_material_but_shares_the_images(tmp_path, change, field):
    scene = two_material_scene(tmp_path, iron=change)
    _, _, pm = build(tmp_path, scene, EngineBudget("godot_4"))
    assert pm.ok, [i for i in pm.issues if i.severity == "critical"]
    ta = pm.data["texture_atlas"]
    assert pm.data["draw_calls"] == 2 and len(pm.data["materials"]) == 2
    assert ta["splits"] and field in ta["splits"][0]["differ_in"]
    import pygltflib

    g = pygltflib.GLTF2().load(str(tmp_path / "pkg" / f"{ASSET}.glb"))
    roles = 4 if "emissive_color" in change else 3
    assert len(g.images) == roles  # one texture set, two draw calls


def test_a_double_sided_part_keeps_its_own_material(tmp_path):
    scene = two_material_scene(tmp_path, iron={"double_sided": True})
    _, _, pm = build(tmp_path, scene, EngineBudget("godot_4"))
    assert pm.ok and pm.data["draw_calls"] == 2
    assert "double_sided" in pm.data["texture_atlas"]["splits"][0]["differ_in"]


def test_a_blended_material_keeps_its_own_atlas_maps_and_draw_call(tmp_path):
    scene = two_material_scene(tmp_path, untextured_iron=True, iron={"alpha_mode": "BLEND", "opacity": 0.5})
    _, bundle, pm = build(tmp_path, scene, EngineBudget("godot_4"))
    assert pm.ok
    by = {u.node_id: u for u in bundle.uvs}
    assert by["band"].atlas_id == "iron" and not by["band"].shared_atlas and by["band"].size == (2048, 2048)
    assert pm.data["draw_calls"] == 2


def test_merge_materials_merges_only_what_is_equal_but_the_name():
    from antigravity3d.kernel.gltf_writer import GltfMaterial

    a, b, c = GltfMaterial("a", roughness_factor=0.5), GltfMaterial("b", roughness_factor=0.5), GltfMaterial("c", roughness_factor=0.6)
    out, merged = merge_materials({"n1": a, "n2": b, "n3": c, "n4": a}, "X_atlas")
    assert out["n1"] is out["n2"] is out["n4"] and out["n1"].name == "X_atlas" and out["n3"] is c
    assert merged == [{"material": "X_atlas", "from": ["a", "b"]}]


def test_a_missing_source_map_is_not_baked_into_a_flat_patch(tmp_path):
    scene = two_material_scene(tmp_path)
    sm = StageManager.create(tmp_path / "work.usda", ASSET)
    sm.apply_ir(scene)
    b = EngineBudget("godot_4")
    bundle = run_export_pipeline(sm.extract_meshes(), b, ASSET, opaque_materials=opaque_materials(scene))
    os.unlink(tmp_path / "iron_albedo.png")
    pm = write_engine_package(sm, bundle, b, tmp_path / "pkg", asset_name=ASSET)
    assert pm.data["texture_atlas"]["status"] == "not_baked" and "iron.albedo" in pm.data["texture_atlas"]["reason"]
    assert any("not found" in n for n in pm.data["notes"])


# --------------------------------------------------------------------------- the stage samples the maps as they ship


def test_the_stage_repeats_each_map_as_the_bake_does(tmp_path):
    scene = two_material_scene(tmp_path)
    sm, bundle, _ = build(tmp_path, scene, EngineBudget("godot_4"))
    w, h = bundle.uvs[0].size
    assert (w, h) != (SWATCH_SPAN_PX, SWATCH_SPAN_PX)
    for name in ("wood", "iron"):
        mat = sm.stage.GetPrimAtPath(f"/{ASSET}/Looks/{name}")
        t2d = UsdShade.Shader(mat.GetChild(MAP_REPEAT))
        assert t2d.GetIdAttr().Get() == "UsdTransform2d"
        assert tuple(t2d.GetInput("scale").Get()) == pytest.approx((w / bundle.uvs[0].repeat_px, h / bundle.uvs[0].repeat_px))
        tex = UsdShade.Shader(mat.GetChild("albedoTex"))
        assert tex.GetInput("st").GetConnectedSource()[0].GetPrim().GetName() == MAP_REPEAT
        assert tex.GetInput("wrapS").Get() == "repeat"


BLENDER_RENDER = r'''
import bpy, sys, math
src, out, kind = sys.argv[-3:]
bpy.ops.wm.read_factory_settings(use_empty=True)
if kind == "usd":
    bpy.ops.wm.usd_import(filepath=src)
else:
    bpy.ops.import_scene.gltf(filepath=src)
for o in list(bpy.data.objects):
    if o.type != "MESH" or "LOD" in o.name or "col" in o.name.lower():
        if o.type == "MESH":
            bpy.data.objects.remove(o)
sc = bpy.context.scene
cam = bpy.data.cameras.new("c"); cam.type = "ORTHO"; cam.ortho_scale = 0.55
co = bpy.data.objects.new("c", cam); sc.collection.objects.link(co); sc.camera = co
co.location = (0.0, -2.0, 0.15); co.rotation_euler = (math.radians(90), 0, 0)
world = bpy.data.worlds.new("w"); sc.world = world; world.use_nodes = True
world.node_tree.nodes["Background"].inputs["Color"].default_value = (1, 1, 1, 1)
sc.render.engine = "CYCLES"; sc.cycles.samples = 4; sc.cycles.use_denoising = False; sc.cycles.seed = 1
sc.view_settings.view_transform = "Standard"
sc.render.resolution_x = sc.render.resolution_y = 256
sc.render.filepath = out
bpy.ops.render.render(write_still=True)
'''


def _blender() -> str | None:
    return os.environ.get("BLENDER_BIN") or shutil.which("blender")


@pytest.mark.skipif(_blender() is None, reason="no Blender binary ($BLENDER_BIN or PATH)")
def test_real_blender_renders_the_stage_and_the_shipped_glb_at_one_texture_scale(tmp_path):
    """The working stage (as Blender's USD importer reads it for lookdev) and the shipped GLB, same camera: the checker
    must line up texel for texel on an atlas that is not 2048 square. The control (the stage without its map repeat,
    each map spread over the atlas as before) must not, or the comparison could not see a scale change."""
    scene = two_material_scene(tmp_path)
    sm, bundle, pm = build(tmp_path, scene, EngineBudget("godot_4"))
    assert bundle.uvs[0].size != (SWATCH_SPAN_PX, SWATCH_SPAN_PX)
    sm.export(tmp_path / "stage.usda")
    control = tmp_path / "control.usda"
    control.write_text((tmp_path / "stage.usda").read_text().replace(f"/{MAP_REPEAT}.outputs:result", "/stReader.outputs:result"))
    script = tmp_path / "render.py"
    script.write_text(BLENDER_RENDER)
    shots = {}
    for kind, src in (("usd", tmp_path / "stage.usda"), ("glb", tmp_path / "pkg" / f"{ASSET}.glb"), ("usd", control)):
        out = tmp_path / f"{Path(src).stem}.png"
        done = subprocess.run([_blender(), "-b", "--factory-startup", "--python-exit-code", "1", "--python", str(script), "--",
                               str(src), str(out), kind], capture_output=True, text=True, timeout=600)
        assert done.returncode == 0, done.stderr[-2000:]
        shots[Path(src).stem] = np.asarray(Image.open(out).convert("RGB"), np.float64)
    obj = (np.abs(shots["stage"] - 255).max(axis=2) > 8) | (np.abs(shots[f"{ASSET}"] - 255).max(axis=2) > 8)
    assert obj.mean() > 0.2
    same = np.abs(shots["stage"] - shots[ASSET]).max(axis=2)[obj]
    ctrl = np.abs(shots["control"] - shots[ASSET]).max(axis=2)[obj]
    report = {"pixels": int(obj.sum()), "stage_vs_glb_mean": round(float(same.mean()), 2),
              "stage_vs_glb_over_48": round(float((same > 48).mean()), 4), "control_vs_glb_mean": round(float(ctrl.mean()), 2),
              "control_over_48": round(float((ctrl > 48).mean()), 4)}
    (tmp_path / "report.json").write_text(json.dumps(report))
    assert report["stage_vs_glb_over_48"] < 0.02, report  # checker edges and render noise only
    assert report["control_over_48"] > 0.15, report  # a scale change shows on a quarter of the surface or more


def test_small_maps_lay_the_atlas_out_at_the_density_they_hold(tmp_path):
    """256 px maps span 2048 atlas px at the target (SWATCH_SPAN_PX): they hold an eighth of 1024 px/m. The atlas is laid
    out at 128 px/m and their texels are copied 1:1, instead of enlarged 8x into an atlas sixty-four times larger."""
    from antigravity3d.ir import MaterialIR

    scene = two_material_scene(tmp_path)
    small = [MaterialIR(id=m.id, albedo_uri=checker(tmp_path / f"{m.id}_s.png", 256, 8, (200, 140, 60), (90, 50, 20))) for m in scene.materials]
    _, bundle, pm = build(tmp_path, scene.model_copy(update={"materials": small}), EngineBudget("godot_4"))
    u = bundle.uvs[0]
    assert u.shared_atlas and u.repeat_px == 256 and min(x.texel_density for x in bundle.uvs) == pytest.approx(128.0)
    assert max(u.size) <= 256
    ta = pm.data["texture_atlas"]
    assert {r["resample_ratio"] for m in ta["members"].values() for r in m["maps"].values()} == {1.0}
    assert pm.data["uv"]["texel_density"]["materials"][u.atlas_id]["target_met"]  # the target the maps can carry


def test_gutters_continue_the_map_8_px_then_hold_the_materials_mean(tmp_path):
    from antigravity3d.kernel.package import GUTTER_COPY_PX

    scene = two_material_scene(tmp_path)
    _, bundle, pm = build(tmp_path, scene, EngineBudget("godot_4"))
    w, h = bundle.uvs[0].size
    rep = bundle.uvs[0].repeat_px
    node_mat = {"body": 1, "band": 2}
    owner, drawn, dist = _owner_labels((w, h), [(node_mat[u.node_id], u.uvs, fast_ops.shipped(u).triangles) for u in bundle.uvs],
                                       with_distance=True)
    baked = np.asarray(Image.open(tmp_path / "pkg" / pm.data["texture_atlas"]["files"]["albedo"]).convert("RGB")).astype(int)
    for k in (1, 2):
        far = (owner == k) & (dist > GUTTER_COPY_PX)
        mean = np.rint(baked[drawn == k].mean(axis=0))
        assert far.any() and (np.abs(baked[far] - mean).max() <= 1)
    # near the wood's charts the gutter is the wood map, repeated from uv (0, 0) as on the charts
    src = np.asarray(Image.open(tmp_path / "wood_albedo.png").convert("RGB").resize((rep, rep), Image.Resampling.LANCZOS)).astype(int)
    rows, cols = np.nonzero((owner == 1) & (dist > 0) & (dist <= GUTTER_COPY_PX))
    want = src[rep - 1 - (h - 1 - rows) % rep, cols % rep]
    assert len(rows) and (np.abs(baked[rows, cols] - want).max(axis=1) <= 1).mean() > 0.999


class GrainSource:
    """A stand-in for a geometry-driven surface (no image model): stripes along world x, painted in atlas space from the
    surface point under each texel (raster.triangle + barycentric of the texel's uv)."""
    name = "grain"

    def __init__(self, material: str, detail: float):
        self.material, self.detail, self.calls = material, detail, []

    def detail_px_per_m(self, material, target):
        return self.detail if material.name == self.material else None

    def roles(self, material):
        return {"albedo"}

    def texels(self, material, role, raster):
        if material.name != self.material or role != "albedo":
            return None
        self.calls.append(role)
        W, H = raster.size
        rows, cols = np.nonzero(raster.region(material.name) & (raster.triangle >= 0))
        out = np.zeros((H, W, 3), np.uint8)
        st = np.stack([(cols + 0.5) / W, 1.0 - (rows + 0.5) / H], 1)
        xs = np.empty(len(rows))
        for t in np.unique(raster.triangle[rows, cols]):
            part, k = raster.part_of(int(t))
            sel = raster.triangle[rows, cols] == t
            a, b, c = part.uvs[part.triangles[k]].astype(np.float64)
            T = np.array([b - a, c - a]).T
            l = np.linalg.lstsq(T, (st[sel] - a).T, rcond=None)[0].T  # barycentric (b, c)
            p = part.points[part.triangles[k]]
            xs[sel] = (p[0] + l[:, :1] * (p[1] - p[0]) + l[:, 1:] * (p[2] - p[0]))[:, 0]
        stripe = (np.floor(xs / 0.05) % 2).astype(bool)
        out[rows, cols] = np.where(stripe[:, None], [200, 60, 60], [60, 60, 200])
        return out


def test_a_surface_source_sets_the_layout_detail_and_paints_its_material(tmp_path):
    """The two seams a geometry-driven surface plugs into: the layout asks how much detail a material carries, the bake
    asks for its texels; the raster gives the triangle under every texel."""
    from antigravity3d.kernel import surface_detail

    scene = two_material_scene(tmp_path, untextured_iron=True)
    flat_only = scene.model_copy(update={"materials": [m.model_copy(update={"albedo_uri": None, "orm_uri": None, "normal_uri": None})
                                                       for m in scene.materials]})
    src = GrainSource("iron", 512.0)
    surface_detail.register_source(src)
    try:
        _, bundle, pm = build(tmp_path, flat_only, EngineBudget("godot_4"))
    finally:
        surface_detail.unregister_source(src)
    by = {u.node_id: u.texel_density for u in bundle.uvs}
    assert by["band"] == pytest.approx(512.0)  # the grain asked for 512 (not the flat 128)
    assert by["body"] == pytest.approx(128.0)  # the flat wood beside it is drawn at FLAT_DETAIL_PX, not at the grain's density
    ta = pm.data["texture_atlas"]
    assert ta["members"]["iron"]["sources"] == {"albedo": "grain"} and src.calls == ["albedo"]
    assert "normal" in ta["roles_omitted"]  # the grain paints base colour only
    w, h = bundle.uvs[0].size
    band = next(u for u in bundle.uvs if u.node_id == "band")
    _, drawn = _owner_labels((w, h), [(1, band.uvs, fast_ops.shipped(band).triangles)])
    baked = np.asarray(Image.open(tmp_path / "pkg" / ta["files"]["albedo"]).convert("RGB"))[drawn > 0]
    assert {tuple(c) for c in np.unique(baked, axis=0)} <= {(200, 60, 60), (60, 60, 200)}
    assert ((baked == [200, 60, 60]).all(1).mean() > 0.2) and ((baked == [60, 60, 200]).all(1).mean() > 0.2)


def test_a_flat_material_beside_a_textured_one_takes_few_texels(tmp_path):
    """One textured (1024 px maps: 512 px/m), one flat: the flat charts are drawn at FLAT_DETAIL_PX (128 px/m), so the
    atlas holds the textured surface at its density and the flat one at an eighth of the area per square metre."""
    scene = two_material_scene(tmp_path, untextured_iron=True)
    _, bundle, pm = build(tmp_path, scene, EngineBudget("godot_4"))
    by = {u.node_id: u for u in bundle.uvs}
    assert by["body"].texel_density == pytest.approx(512.0) and by["band"].texel_density == pytest.approx(128.0)
    assert by["body"].repeat_px == 1024  # the textured map is copied 1:1
    rep = pm.data["uv"]["texel_density"]["materials"][by["body"].atlas_id]
    assert rep["px_per_m"] == pytest.approx(512.0) and rep["target_met"]
    assert pm.ok and fast_ops.uv_overlap_fraction(bundle.uvs) == 0.0


def test_every_baked_map_states_its_lossless_and_jpeg_cost(tmp_path):
    scene = two_material_scene(tmp_path)
    _, _, pm = build(tmp_path, scene, EngineBudget("godot_4"))
    enc = pm.data["texture_atlas"]["encoding"]
    assert set(enc) == {"albedo", "orm", "normal"}
    for role, r in enc.items():
        assert r["shipped"] == "png" and r["bytes"] == (tmp_path / "pkg" / pm.data["texture_atlas"]["files"][role]).stat().st_size
        assert r["alternative"]["encoding"] == "jpeg_q95" and r["alternative"]["bytes"] > 0
    assert "angle_mean_deg" in enc["normal"]["alternative"]["error_on_charts"]


def test_the_base_colour_can_ship_as_jpeg_and_every_check_reads_it(tmp_path):
    scene = two_material_scene(tmp_path)
    _, _, pm = build(tmp_path, scene, EngineBudget("godot_4", base_color_jpeg_quality=95))
    assert pm.ok, [i for i in pm.issues if i.severity == "critical"]
    ta = pm.data["texture_atlas"]
    assert ta["files"]["albedo"].endswith(".jpg") and ta["files"]["orm"].endswith(".png")
    assert ta["encoding"]["albedo"]["shipped"] == "jpeg" and ta["encoding"]["albedo"]["alternative"]["encoding"] == "png"
    with Image.open(tmp_path / "pkg" / ta["files"]["albedo"]) as im:
        assert im.format == "JPEG"
    import pygltflib

    g = pygltflib.GLTF2().load(str(tmp_path / "pkg" / f"{ASSET}.glb"))
    assert sorted(i.mimeType for i in g.images) == ["image/jpeg", "image/png", "image/png"]
    assert not g.extensionsRequired
