"""Export-fidelity tests: UVs in the USD, relocatable textures, normal convention, LOD/collider structure.

Real kernel (xatlas, meshoptimizer, CoACD, pxr); images are tiny generated PNGs (no image backend).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pytest
import trimesh
from PIL import Image
from pxr import Sdf, Usd, UsdGeom, UsdPhysics, UsdShade, Vt

from antigravity3d.ir import MaterialIR, NodeIR, SceneIR, Transform
from antigravity3d.kernel.fast_ops import run_export_pipeline
from antigravity3d.kernel.package import collider_name, flip_normal_green, write_engine_package
from antigravity3d.kernel.usd_stage import StageManager
from antigravity3d.kernel.validator import check_engine_compliance, validate_package
from antigravity3d.state import EngineBudget

ASSET = "Prop"


def _maps(d: Path) -> dict[str, str]:
    rng = np.random.default_rng(7)
    out = {}
    for k in ("albedo", "orm", "normal"):
        a = rng.integers(0, 255, (32, 32, 3), dtype=np.uint8)
        Image.fromarray(a).save(d / f"{k}.png")
        out[f"{k}_uri"] = str(d / f"{k}.png")
    return out


def _scene(maps: dict[str, str], convention: str = "opengl") -> SceneIR:
    return SceneIR(
        asset_name=ASSET,
        materials=[MaterialIR(id="paint", normal_convention=convention, **maps)],
        nodes=[
            NodeIR(id="root", kind="group", transform=Transform(translate=(2, 1, 0), rotate_euler_deg=(0, 0, 90))),
            NodeIR(id="base", kind="cylinder", params={"radius": 0.5, "height": 0.4}, parent="root",
                   transform=Transform(translate=(0, 0, 0.2), scale=(1.5, 1, 1)), material_id="paint"),
            NodeIR(id="ball", kind="sphere", params={"radius": 0.3}, parent="root",
                   transform=Transform(translate=(0, 0, 0.7)), material_id="paint"),
            NodeIR(id="slab", kind="box", params={"size": (1, 1, 0.1)}, parent="root",
                   transform=Transform(translate=(0, 0, 0.05)), material_id="paint"),
        ],
    )


@pytest.fixture(scope="module")
def src(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("maps")
    _maps(d)
    return d


def _stage(tmp: Path, src: Path, convention: str = "opengl") -> StageManager:
    sm = StageManager.create(tmp / "work.usda", ASSET)
    sm.apply_ir(_scene({f"{k}_uri": str(src / f"{k}.png") for k in ("albedo", "orm", "normal")}, convention))
    return sm


def _export(tmp: Path, src: Path, engine: str, convention: str = "opengl"):
    sm = _stage(tmp, src, convention)
    budget = EngineBudget(engine)
    bundle = run_export_pipeline(sm.extract_meshes(), budget, ASSET)
    pm = write_engine_package(sm, bundle, budget, tmp / engine, asset_name=ASSET, qa={"tier1_ok": True, "tier15": 3.9})
    return sm, bundle, pm


@pytest.fixture(scope="module")
def unreal(tmp_path_factory, src):
    return _export(tmp_path_factory.mktemp("ue"), src, "unreal_5")


@pytest.fixture(scope="module")
def godot(tmp_path_factory, src):
    return _export(tmp_path_factory.mktemp("godot"), src, "godot_4")


def _meshes(stage: Usd.Stage) -> list[Usd.Prim]:
    return [p for p in Usd.PrimRange(stage.GetDefaultPrim()) if p.IsA(UsdGeom.Mesh) and not p.HasAPI(UsdPhysics.CollisionAPI)]


def _texture_inputs(stage: Usd.Stage) -> list[Sdf.AssetPath]:
    out = []
    for p in stage.Traverse():
        if p.IsA(UsdShade.Shader) and UsdShade.Shader(p).GetIdAttr().Get() == "UsdUVTexture":
            out.append(UsdShade.Shader(p).GetInput("file").Get())
    return out


# --------------------------------------------------------------------------- defect 1: UVs


@pytest.mark.parametrize("fx", ["unreal", "godot"])
def test_usd_meshes_carry_st_matching_glb_topology(fx, request):
    sm, bundle, pm = request.getfixturevalue(fx)
    stage = Usd.Stage.Open(str(pm.out_dir / f"{ASSET}.usd"))
    meshes = {p.GetName(): p for p in _meshes(stage)}
    assert set(meshes) == {"base", "ball", "slab"}
    for u in bundle.uvs:
        prim = meshes[u.node_id]
        pv = UsdGeom.PrimvarsAPI(prim).GetPrimvar("st")
        assert pv and pv.GetTypeName() == Sdf.ValueTypeNames.TexCoord2fArray
        assert pv.GetInterpolation() == UsdGeom.Tokens.vertex
        st = np.asarray(pv.Get())
        pts = np.asarray(UsdGeom.Mesh(prim).GetPointsAttr().Get())
        assert len(st) == len(pts) == len(u.points)  # seam-split topology, not the pre-unwrap points
        assert len(pts) > 0 and st.min() >= 0.0 and st.max() <= 1.0 and np.isfinite(st).all()
        np.testing.assert_allclose(st, u.uvs, atol=1e-6)
        assert len(UsdGeom.Mesh(prim).GetFaceVertexCountsAttr().Get()) == len(u.triangles)
        normals = UsdGeom.Mesh(prim).GetNormalsAttr().Get()
        assert UsdGeom.Mesh(prim).GetNormalsInterpolation() == UsdGeom.Tokens.faceVarying
        assert len(normals) == 3 * len(u.triangles)
    # GLB LOD0 has exactly the USD LOD0 topology
    glb = trimesh.load(pm.out_dir / f"{ASSET}.glb")
    assert sum(len(g.faces) for g in glb.geometry.values()) == sum(len(u.triangles) for u in bundle.uvs)
    assert sum(len(g.vertices) for g in glb.geometry.values()) == sum(len(u.points) for u in bundle.uvs)


def test_box_normals_stay_hard_and_sphere_smooth(godot):
    _, _, pm = godot
    stage = Usd.Stage.Open(str(pm.out_dir / f"{ASSET}.usd"))
    box = UsdGeom.Mesh(stage.GetPrimAtPath(f"/{ASSET}/root/slab"))
    n = np.asarray(box.GetNormalsAttr().Get())
    assert len(np.unique(np.round(n, 3), axis=0)) == 6  # six face normals, no averaging across 90 degree edges
    ball = UsdGeom.Mesh(stage.GetPrimAtPath(f"/{ASSET}/root/ball"))
    assert len(np.unique(np.round(np.asarray(ball.GetNormalsAttr().Get()), 2), axis=0)) > 50
    np.testing.assert_allclose(np.linalg.norm(n, axis=1), 1.0, atol=1e-4)


def test_working_stage_validator_and_extract_agree(src, tmp_path):
    sm = _stage(tmp_path, src)
    before = sm.extract_meshes()
    budget = EngineBudget("unreal_5")
    # no UVs yet: textured materials without st is critical (not just a warning)
    issues = check_engine_compliance(Usd.Stage.Open(sm.export_layer()), before, budget)
    assert {i.node_id for i in issues if i.code == "unreal_5:uv_missing" and i.severity == "critical"} == {"base", "ball", "slab"}
    uvs = run_export_pipeline(before, budget, ASSET).uvs
    assert not sm.has_uvs()
    tx_before = list(sm.tx_stack)
    sm.author_uvs(uvs)
    assert sm.has_uvs() and sm.tx_stack == tx_before  # overlay: no undo entry
    after = sm.extract_meshes()
    for a, b in zip(before, after):  # extract_meshes is the pre-unwrap geometry before and after
        assert a.node_id == b.node_id and len(a.points) == len(b.points)
        np.testing.assert_allclose(a.points, b.points, atol=1e-5)
        np.testing.assert_array_equal(a.triangles, b.triangles)
    flat = Usd.Stage.Open(sm.export_layer())
    assert not [i for i in check_engine_compliance(flat, after, budget) if i.code.endswith("uv_missing")]
    # unwrapping the folded geometry again is idempotent
    uvs2 = run_export_pipeline(after, budget, ASSET).uvs
    assert [len(u.points) for u in uvs2] == [len(u.points) for u in uvs]
    sm.author_uvs(uvs2)
    assert sm.has_uvs()


def test_uv_overlay_is_dropped_by_edits_and_works_with_variants(src, tmp_path):
    sm = _stage(tmp_path, src)
    budget = EngineBudget("godot_4")
    sm.author_uvs(run_export_pipeline(sm.extract_meshes(), budget, ASSET).uvs)
    assert sm.has_uvs()
    bigger = sm.base_ir().model_copy(deep=True)
    bigger.nodes[3] = bigger.nodes[3].model_copy(update={"params": {"size": (2, 2, 0.2)}})
    sm.add_variant("size", "big", bigger)
    assert not sm.has_uvs()  # variant edit invalidates derived UVs
    sm.select_variant("size", "big")
    uvs = run_export_pipeline(sm.extract_meshes(), budget, ASSET).uvs
    sm.author_uvs(uvs)  # authored at composed paths: overrides the selected variant's geometry correctly
    assert sm.has_uvs()
    slab = next(m for m in sm.extract_meshes() if m.node_id == "slab")
    assert np.ptp(slab.points[:, 0]) == pytest.approx(2.0, abs=1e-4)
    with pytest.raises(RuntimeError, match="variant selected"):
        sm.apply_ir(sm.base_ir())
    sm.rollback()
    assert not sm.has_uvs()
    sm.commit_all()
    assert not sm.has_uvs()


def test_base_edit_after_uvs_is_not_masked(src, tmp_path):
    from antigravity3d.ir import IRPatch, SetParam

    sm = _stage(tmp_path, src)
    budget = EngineBudget("godot_4")
    sm.author_uvs(run_export_pipeline(sm.extract_meshes(), budget, ASSET).uvs)
    sm.apply_patch(IRPatch(ops=[SetParam(node_id="slab", name="size", value=(3, 1, 0.1))]))
    slab = next(m for m in sm.extract_meshes() if m.node_id == "slab")
    assert np.ptp(slab.points[:, 1]) == pytest.approx(3.0, abs=1e-4) or np.ptp(slab.points[:, 0]) == pytest.approx(3.0, abs=1e-4)


# --------------------------------------------------------------------------- defect 2: relocatable


@pytest.mark.parametrize("fx", ["unreal", "godot"])
def test_package_relocates_and_resolves_every_texture(fx, request, tmp_path):
    _, _, pm = request.getfixturevalue(fx)
    first = tmp_path / "first"
    moved = tmp_path / "elsewhere" / "deep" / "pkg"
    shutil.copytree(pm.out_dir, first)
    moved.parent.mkdir(parents=True)
    shutil.move(first, moved)  # a different directory; nothing may point back to the original
    stage = Usd.Stage.Open(str(moved / f"{ASSET}.usd"))
    tex = _texture_inputs(stage)
    assert len(tex) == 3
    layer = stage.GetRootLayer()
    for a in tex:
        assert not Path(a.path).is_absolute() and a.path.startswith("./textures/")
        assert a.resolvedPath and Path(a.resolvedPath).is_file()
        assert Path(a.resolvedPath).resolve().is_relative_to(moved.resolve())
        assert Path(layer.ComputeAbsolutePath(a.path)).is_file()
    assert validate_package(moved) == []


def test_usdz_contains_textures_and_opens(unreal):
    _, _, pm = unreal
    assert pm.data["usdz"] == f"{ASSET}.usdz" and pm.data["usdz_skipped"] is None
    z = Usd.Stage.Open(str(pm.out_dir / pm.data["usdz"]))
    assert z.GetDefaultPrim().GetName() == ASSET
    tex = _texture_inputs(z)
    assert len(tex) == 3 and all(t.resolvedPath for t in tex)
    names = [n for n in Sdf.ZipFile.Open(str(pm.out_dir / pm.data["usdz"])).GetFileNames()]
    assert sum(n.startswith("textures/") for n in names) == 3


# --------------------------------------------------------------------------- defect 3: normals


def _green(p: Path) -> np.ndarray:
    return np.asarray(Image.open(p).convert("RGB"))[..., 1].astype(int)


def test_unreal_normal_map_is_green_flipped_and_godot_is_not(unreal, godot, src):
    _, _, ue = unreal
    _, _, gd = godot
    n_ue = next(t for t in ue.data["textures"] if t["role"] == "normal")
    n_gd = next(t for t in gd.data["textures"] if t["role"] == "normal")
    assert (n_ue["convention"], n_ue["converted"]) == ("directx", True)
    assert (n_gd["convention"], n_gd["converted"]) == ("opengl", False)
    assert ue.data["normal_convention"] == "directx" and gd.data["normal_convention"] == "opengl"
    g_ue, g_gd, g_src = (_green(ue.out_dir / n_ue["path"]), _green(gd.out_dir / n_gd["path"]), _green(src / "normal.png"))
    np.testing.assert_array_equal(g_gd, g_src)
    np.testing.assert_array_equal(g_ue, 255 - g_gd)
    a_ue = np.asarray(Image.open(ue.out_dir / n_ue["path"]).convert("RGB"))
    a_src = np.asarray(Image.open(src / "normal.png").convert("RGB"))
    np.testing.assert_array_equal(a_ue[..., [0, 2]], a_src[..., [0, 2]])  # only green changes
    # albedo/orm are copied untouched
    for t in ue.data["textures"]:
        if t["role"] != "normal":
            assert (ue.out_dir / t["path"]).read_bytes() == (src / f"{t['role']}.png").read_bytes()
    stage = Usd.Stage.Open(str(ue.out_dir / f"{ASSET}.usd"))
    assert stage.GetPrimAtPath(f"/{ASSET}/Looks/paint").GetCustomDataByKey("agy:normalConvention") == "opengl"
    nt = UsdShade.Shader(stage.GetPrimAtPath(f"/{ASSET}/Looks/paint/normalTex"))
    assert nt.GetInput("scale").Get()[1] == 2 and nt.GetInput("bias").Get()[1] == -1
    np.testing.assert_array_equal(_green(ue.out_dir / n_ue["usd_path"]), g_src)


def test_directx_marked_material_is_not_double_flipped(src, tmp_path_factory):
    _, _, ue = _export(tmp_path_factory.mktemp("ue_dx"), src, "unreal_5", "directx")
    _, _, gd = _export(tmp_path_factory.mktemp("gd_dx"), src, "godot_4", "directx")
    n_ue = next(t for t in ue.data["textures"] if t["role"] == "normal")
    n_gd = next(t for t in gd.data["textures"] if t["role"] == "normal")
    assert (n_ue["converted"], n_ue["convention"], n_ue["authored_convention"]) == (False, "directx", "directx")
    assert (n_gd["converted"], n_gd["convention"], n_gd["authored_convention"]) == (True, "opengl", "directx")
    np.testing.assert_array_equal(_green(ue.out_dir / n_ue["path"]), _green(src / "normal.png"))
    np.testing.assert_array_equal(_green(gd.out_dir / n_gd["path"]), 255 - _green(src / "normal.png"))


def test_flip_is_an_involution(src, tmp_path):
    once = flip_normal_green(src / "normal.png", tmp_path / "a.png")
    twice = flip_normal_green(once, tmp_path / "b.png")
    np.testing.assert_array_equal(np.asarray(Image.open(twice)), np.asarray(Image.open(src / "normal.png").convert("RGB")))


# --------------------------------------------------------------------------- defect 4: LODs, colliders, units


def test_lod_variant_set_matches_glb_lods(unreal):
    sm, bundle, pm = unreal
    stage = Usd.Stage.Open(str(pm.out_dir / f"{ASSET}.usd"))
    root = stage.GetDefaultPrim()
    vset = root.GetVariantSets().GetVariantSet("LOD")
    names = vset.GetVariantNames()
    levels = min(len(v) for v in bundle.lods.values())
    # levels come from the screen-space error budget (cylinder + sphere + box here: LOD0 and two far levels)
    assert names == [f"LOD{i}" for i in range(levels)] and levels >= 2
    assert vset.GetVariantSelection() == "LOD0"
    tris = []
    stage.SetEditTarget(stage.GetSessionLayer())
    for i, n in enumerate(names):
        vset.SetVariantSelection(n)
        tris.append(sum(len(UsdGeom.Mesh(p).GetFaceVertexCountsAttr().Get()) for p in _meshes(stage)))
        for p in _meshes(stage):
            st = UsdGeom.PrimvarsAPI(p).GetPrimvar("st").Get()
            assert len(st) == len(UsdGeom.Mesh(p).GetPointsAttr().Get())
        if i:
            glb = trimesh.load(pm.out_dir / f"{ASSET}_LOD{i}.glb")
            assert sum(len(g.faces) for g in glb.geometry.values()) == tris[-1]
    assert tris == pm.data["lod_tris"] == [sum(v[i].tri_count for v in bundle.lods.values()) for i in range(levels)]
    assert all(b < a for a, b in zip(tris, tris[1:]))
    assert tris[0] == sum(len(u.triangles) for u in bundle.uvs)
    assert pm.data["lod_screen_fractions"][0] == 1.0 and len(pm.data["lod_screen_fractions"]) == levels
    assert all(b < a for a, b in zip(pm.data["lod_screen_fractions"], pm.data["lod_screen_fractions"][1:]))
    # the coarsest level's geometry is where LOD0's is (local->world mapping through the node transforms is right)
    vset.SetVariantSelection("LOD0")
    cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])
    r0 = cache.ComputeWorldBound(root).ComputeAlignedRange()
    cache.Clear()
    vset.SetVariantSelection(names[-1])
    r3 = cache.ComputeWorldBound(root).ComputeAlignedRange()
    for a, b in zip(r0.GetMin(), r3.GetMin()):
        assert abs(a - b) < 0.1
    for a, b in zip(r0.GetMax(), r3.GetMax()):
        assert abs(a - b) < 0.1


def test_colliders_are_convex_guide_physics_meshes(unreal, godot):
    for sm, bundle, pm in (unreal, godot):
        stage = Usd.Stage.Open(str(pm.out_dir / f"{ASSET}.usd"))
        hulls = [p for p in stage.Traverse() if p.IsA(UsdGeom.Mesh) and p.HasAPI(UsdPhysics.CollisionAPI)]
        assert [h.GetName() for h in hulls] == pm.data["colliders"] == [f"UCX_{ASSET}_{i:02d}" for i in range(len(bundle.colliders))]
        assert 1 <= len(hulls) <= EngineBudget("unreal_5").max_collision_hulls
        for h in hulls:
            assert h.GetParent().GetName() == "Colliders" and h.HasAPI(UsdPhysics.MeshCollisionAPI)
            assert UsdPhysics.MeshCollisionAPI(h).GetApproximationAttr().Get() == "convexHull"
            assert UsdGeom.Imageable(h).GetPurposeAttr().Get() == UsdGeom.Tokens.guide
        assert not UsdGeom.PrimvarsAPI(hulls[0]).GetPrimvar("st")
    assert collider_name("godot_4", "A", 3, usd=False) == "A_03-convcolonly"
    assert collider_name("godot_4", "A", 3) == "UCX_A_03"
    gd = trimesh.load(godot[2].out_dir / f"{ASSET}_colliders.glb")
    assert all(n.endswith("-convcolonly") for n in gd.geometry)
    ue = trimesh.load(unreal[2].out_dir / f"{ASSET}_colliders.glb")
    assert all(n.startswith(f"UCX_{ASSET}_") for n in ue.geometry)


def test_units_and_axis_are_converted_consistently(unreal, godot):
    sm, bundle, ue = unreal
    _, _, gd = godot
    s_ue = Usd.Stage.Open(str(ue.out_dir / f"{ASSET}.usd"))
    s_gd = Usd.Stage.Open(str(gd.out_dir / f"{ASSET}.usd"))
    assert (UsdGeom.GetStageUpAxis(s_ue), UsdGeom.GetStageMetersPerUnit(s_ue)) == ("Z", 1.0)
    assert (UsdGeom.GetStageUpAxis(s_gd), UsdGeom.GetStageMetersPerUnit(s_gd)) == ("Y", 1.0)
    assert ue.data["units"]["root_transform"] is None and gd.data["units"]["up_axis"] == "Y"

    def bound(stage):
        cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])
        r = cache.ComputeWorldBound(stage.GetDefaultPrim()).ComputeAlignedRange()
        return np.array(r.GetMin()), np.array(r.GetMax())

    lo_z, hi_z = bound(s_ue)
    lo_y, hi_y = bound(s_gd)
    work = np.vstack([m.points for m in sm.extract_meshes()])
    np.testing.assert_allclose(lo_z, work.min(0), atol=1e-4)
    np.testing.assert_allclose(hi_z, work.max(0), atol=1e-4)
    # (x, y, z) -> (x, z, -y)
    np.testing.assert_allclose(lo_y, [work[:, 0].min(), work[:, 2].min(), -work[:, 1].max()], atol=1e-4)
    np.testing.assert_allclose(hi_y, [work[:, 0].max(), work[:, 2].max(), -work[:, 1].min()], atol=1e-4)
    # and the GLB (Y-up too) agrees with the Y-up USD
    glb = trimesh.load(gd.out_dir / f"{ASSET}.glb")
    gb = glb.bounds
    np.testing.assert_allclose(gb[0], lo_y, atol=1e-3)
    np.testing.assert_allclose(gb[1], hi_y, atol=1e-3)


# --------------------------------------------------------------------------- manifest + validate_package


def test_manifest_contents_and_clean_validation(unreal, godot):
    for _, bundle, pm in (unreal, godot):
        man = json.loads((pm.out_dir / "manifest.json").read_text())
        assert man["schema_version"] == 4 and man["asset"] == ASSET and man["usd"] == f"{ASSET}.usd"  # schema 4: the provenance record (kernel/provenance.py)
        assert man["tris"] == man["lod_tris"][0] and man["draw_calls"] == 3
        assert man["qa"] == {"tier1_ok": True, "tier15": 3.9}
        assert {"target_px_per_m", "mean_px_per_m"} <= set(man["texel_density"])
        assert {"usd", "trimesh", "xatlas", "meshoptimizer", "python"} <= set(man["versions"])
        assert all(t["width"] == 32 and t["height"] == 32 for t in man["textures"])
        assert set(man["file_hashes"]) == set(man["files"]) and f"{ASSET}.usdz" in man["files"]
        assert all(len(v["sha256"]) == 64 for v in man["file_hashes"].values())
        assert man["validation"]["package_ok"] and man["validation"]["package"] == []
        assert not [i for i in man["validation"]["usd"] + man["validation"]["engine"] if i["severity"] == "critical"]
        # the fixture's 1.5 m parts do not fit a 2048 px atlas at 1024 px/m: that is the one (new) warning it may carry
        assert pm.ok and all(i.code.endswith(":texel_density_target") and i.severity == "warning" for i in pm.issues)
        assert validate_package(pm.out_dir) == []
        # where every GLB keeps its images: LOD0 embeds them all, far LODs reference textures/, colliders have none
        glbs = [f for f in man["files"] if f.endswith(".glb")]
        assert sorted(man["glb_images"]) == sorted(glbs) and man["lod_levels"] >= 2
        texture_files = {t["usd_path"] for t in man["textures"]}
        for name, records in man["glb_images"].items():
            if name == f"{ASSET}_colliders.glb":
                assert records == []
                continue
            assert len(records) == 3 and {r["texture"] for r in records} == texture_files
            for r in records:
                assert r["sha256"] == man["file_hashes"][r["texture"]]["sha256"]
                if name == f"{ASSET}.glb":
                    assert r["storage"] == "embedded" and "uri" not in r
                else:
                    assert r["storage"] == "uri" and r["uri"] == r["texture"] and r["uri"].startswith("textures/")


def _codes(issues) -> set[str]:
    return {i.code.removeprefix("pkg:") for i in issues}


def test_validate_package_catches_corruption(src, tmp_path_factory):
    _, _, pm = _export(tmp_path_factory.mktemp("bad"), src, "unreal_5")
    out = pm.out_dir
    assert validate_package(out) == []
    # edit a hash
    man = json.loads((out / "manifest.json").read_text())
    good = dict(man["file_hashes"][f"{ASSET}.glb"])
    man["file_hashes"][f"{ASSET}.glb"]["sha256"] = "0" * 64
    (out / "manifest.json").write_text(json.dumps(man))
    assert "hash_mismatch" in _codes(validate_package(out))
    man["file_hashes"][f"{ASSET}.glb"] = good
    (out / "manifest.json").write_text(json.dumps(man))
    assert validate_package(out) == []
    # delete a texture
    tex = out / next(t["path"] for t in man["textures"] if t["role"] == "orm")
    saved = tex.read_bytes()
    tex.unlink()
    codes = _codes(validate_package(out))
    assert {"file_missing", "texture_missing"} <= codes
    tex.write_bytes(saved)
    assert validate_package(out) == []
    # an absolute (non-relocatable) texture path
    stage = Usd.Stage.Open(str(out / f"{ASSET}.usd"))
    sh = next(p for p in stage.Traverse() if p.GetName() == "albedoTex")
    UsdShade.Shader(sh).GetInput("file").Set(Sdf.AssetPath(str((out / "textures" / "albedo.png").resolve())))
    stage.GetRootLayer().Save()
    del stage
    codes = _codes(validate_package(out))
    assert "texture_not_relative" in codes and "hash_mismatch" in codes
    # missing manifest / unopenable usd
    (out / "manifest.json").unlink()
    assert _codes(validate_package(out)) == {"manifest_missing"}


def test_validate_package_catches_structural_defects(src, tmp_path_factory):
    _, _, pm = _export(tmp_path_factory.mktemp("bad2"), src, "unreal_5")
    out = pm.out_dir

    def rewrite(edit) -> set[str]:
        stage = Usd.Stage.Open(str(out / f"{ASSET}.usd"))
        edit(stage)
        stage.GetRootLayer().Save()
        man = json.loads((out / "manifest.json").read_text())
        from antigravity3d.kernel.package import _sha256

        man["file_hashes"][f"{ASSET}.usd"]["sha256"] = _sha256(out / f"{ASSET}.usd")  # keep hashes honest
        (out / "manifest.json").write_text(json.dumps(man))
        return _codes(validate_package(out))

    def drop_st(stage):
        stage.SetEditTarget(stage.GetRootLayer())
        vs = stage.GetDefaultPrim().GetVariantSets().GetVariantSet("LOD")
        vs.SetVariantSelection("LOD0")
        with vs.GetVariantEditContext():
            stage.GetPrimAtPath(f"/{ASSET}/root/ball").GetAttribute("primvars:st").Block()

    assert "uv_missing" in rewrite(drop_st)

    def bad_hull(stage):
        h = stage.GetPrimAtPath(f"/{ASSET}/Colliders/UCX_{ASSET}_00")
        UsdGeom.Imageable(h).GetPurposeAttr().Set(UsdGeom.Tokens.default_)
        UsdPhysics.MeshCollisionAPI(h).GetApproximationAttr().Set("none")
        # a tetrahedron with one face pushed inwards: closed, but concave
        UsdGeom.Mesh(h).GetPointsAttr().Set(Vt.Vec3fArray([(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1), (0.1, 0.1, 0.1)]))
        UsdGeom.Mesh(h).GetFaceVertexIndicesAttr().Set(Vt.IntArray([0, 1, 2, 0, 2, 3, 0, 3, 1, 1, 2, 4, 2, 3, 4, 3, 1, 4]))
        UsdGeom.Mesh(h).GetFaceVertexCountsAttr().Set(Vt.IntArray([3] * 6))

    codes = rewrite(bad_hull)
    assert {"collider_purpose", "collider_approx", "collider_convex"} <= codes


def test_reexport_into_same_directory_replaces_old_files(src, tmp_path):
    sm = _stage(tmp_path, src)
    budget = EngineBudget("unreal_5")
    bundle = run_export_pipeline(sm.extract_meshes(), budget, ASSET)
    out = tmp_path / "pkg"
    write_engine_package(sm, bundle, budget, out, asset_name=ASSET)
    (out / "textures" / "stale.png").write_bytes(b"x")
    pm = write_engine_package(sm, {"uvs": bundle.uvs, "lods": bundle.lods, "colliders": bundle.colliders}, budget, out, asset_name=ASSET)
    assert pm.ok and (out / "textures" / "stale.png").exists()  # unknown files are never deleted
    assert validate_package(out) == []


@pytest.mark.parametrize("fx", ["unreal", "godot"])
def test_relocated_glbs_carry_correct_pbr_pixels_at_every_lod(fx, request, tmp_path, src):
    """LOD0 alone, copied anywhere, carries its maps; a far LOD file finds them in the relocated package's textures/."""
    import io
    import struct
    from urllib.parse import unquote

    _, _, pm = request.getfixturevalue(fx)
    alone = tmp_path / "alone"
    alone.mkdir()
    moved = tmp_path / "elsewhere" / "pkg"
    shutil.copytree(pm.out_dir, moved)
    levels = 0
    for glb in sorted(moved.glob("*.glb")):
        if "colliders" in glb.stem:
            continue
        lod0 = glb.stem == ASSET
        if lod0:
            glb = Path(shutil.copy(glb, alone))  # the single-file deliverable needs nothing beside it
        levels += 1
        blob = glb.read_bytes()
        length = struct.unpack_from("<I", blob, 12)[0]
        doc = json.loads(blob[20:20 + length])
        binary = blob[28 + length:]
        for mesh in doc["meshes"]:
            for primitive in mesh["primitives"]:
                assert "TEXCOORD_0" in primitive["attributes"]
                mat = doc["materials"][primitive["material"]]
                assert mat["name"] == "paint"
                pbr = mat["pbrMetallicRoughness"]
                for role, slot in (("albedo", pbr["baseColorTexture"]), ("orm", pbr["metallicRoughnessTexture"]),
                                   ("orm", mat["occlusionTexture"]), ("normal", mat["normalTexture"])):
                    image = doc["images"][doc["textures"][slot["index"]]["source"]]
                    if lod0:
                        assert "uri" not in image
                        view = doc["bufferViews"][image["bufferView"]]
                        offset = view.get("byteOffset", 0)
                        data = binary[offset:offset + view["byteLength"]]
                    else:
                        assert "bufferView" not in image and image["uri"].startswith("textures/")
                        data = (glb.parent / unquote(image["uri"])).read_bytes()
                    with Image.open(io.BytesIO(data)) as actual:
                        with Image.open(src / f"{role}.png") as expected:
                            np.testing.assert_array_equal(actual, expected)
    assert levels == pm.data["lod_levels"] >= 2


def test_a_self_contained_far_lod_is_judged_by_the_manifest_version(unreal, tmp_path):
    """Far LODs that embed their images were the version 1 contract: valid under a version 1 manifest, and
    ``lod_embeds_image`` under version 2. The same files either way; only what the manifest declares differs."""
    from antigravity3d.kernel.package import _sha256, _write_glb_level, gltf_materials

    sm, bundle, pm = unreal
    out = tmp_path / "pkg"
    shutil.copytree(pm.out_dir, out)
    materials = gltf_materials(Usd.Stage.Open(str(out / f"{ASSET}.usd")), sm.extract_meshes(), package_dir=out)
    man = json.loads((out / "manifest.json").read_text())
    for level in range(1, man["lod_levels"]):  # every far LOD written as version 1 wrote it: images embedded
        lod = out / f"{ASSET}_LOD{level}.glb"
        _write_glb_level(lod, {n: chain[level] for n, chain in bundle.lods.items()}, man["glb_batches"], materials, f"_LOD{level}")
        man["file_hashes"][lod.name] = {"sha256": _sha256(lod), "bytes": lod.stat().st_size}
    del man["glb_images"]  # version 1 manifests did not have it
    (out / "manifest.json").write_text(json.dumps({**man, "schema_version": 1}))
    assert validate_package(out) == []
    (out / "manifest.json").write_text(json.dumps({**man, "schema_version": 2}))
    issues = validate_package(out)
    assert {"lod_embeds_image", "glb_images_manifest"} <= _codes(issues) and "lod_texture_differs" not in _codes(issues)
    # a version 1 package is still held to the rest: a far level binding other image content than LOD0 fails
    from dataclasses import replace

    lod1 = out / f"{ASSET}_LOD1.glb"
    paint = next(iter(materials.values()))  # one material on every node
    swapped = replace(paint, base_color_texture=paint.normal_texture)  # albedo slot bound to the normal map
    _write_glb_level(lod1, {n: chain[1] for n, chain in bundle.lods.items()}, man["glb_batches"], dict.fromkeys(materials, swapped), "_LOD1")
    man["file_hashes"][lod1.name] = {"sha256": _sha256(lod1), "bytes": lod1.stat().st_size}
    (out / "manifest.json").write_text(json.dumps({**man, "schema_version": 1}))
    assert "lod_texture_differs" in _codes(validate_package(out))


def _lod1_edit(out: Path, edit) -> set[str]:
    """Rewrite the JSON of ``<asset>_LOD1.glb`` with ``edit(doc)``, keep the manifest's hash of the file honest, validate."""
    import struct

    from antigravity3d.kernel.package import _sha256

    glb = out / f"{ASSET}_LOD1.glb"
    blob = glb.read_bytes()
    length = struct.unpack_from("<I", blob, 12)[0]
    doc = json.loads(blob[20:20 + length])
    edit(doc)
    js = json.dumps(doc, separators=(",", ":")).encode()
    js += b" " * (-len(js) % 4)
    rest = blob[20 + length:]
    glb.write_bytes(struct.pack("<4sII", b"glTF", 2, 20 + len(js) + len(rest)) + struct.pack("<I4s", len(js), b"JSON") + js + rest)
    man = json.loads((out / "manifest.json").read_text())
    man["file_hashes"][glb.name] = {"sha256": _sha256(glb), "bytes": glb.stat().st_size}
    (out / "manifest.json").write_text(json.dumps(man))
    return _codes(validate_package(out))


def test_validate_package_checks_the_textures_far_lods_reference(unreal, tmp_path):
    from antigravity3d.kernel.package import _sha256

    _, _, pm = unreal
    out = tmp_path / "pkg"
    shutil.copytree(pm.out_dir, out)
    man = json.loads((out / "manifest.json").read_text())
    assert validate_package(out) == [] and f"{ASSET}_LOD1.glb" in man["files"]
    albedo = out / next(t["usd_path"] for t in man["textures"] if t["role"] == "albedo")
    saved = albedo.read_bytes()

    # a referenced texture is missing
    albedo.unlink()
    assert "glb_image_missing" in _codes(validate_package(out))
    # a referenced texture has other content than the manifest records, even with its own hash entry updated
    Image.fromarray(np.zeros((32, 32, 3), np.uint8)).save(albedo)
    assert {"hash_mismatch", "glb_image_hash"} <= _codes(validate_package(out))
    edited = json.loads((out / "manifest.json").read_text())
    edited["file_hashes"][albedo.relative_to(out).as_posix()]["sha256"] = _sha256(albedo)
    (out / "manifest.json").write_text(json.dumps(edited))
    codes = _codes(validate_package(out))
    assert "hash_mismatch" not in codes and {"glb_images_manifest", "lod_texture_differs"} <= codes
    (out / "manifest.json").write_text(json.dumps(man))
    albedo.write_bytes(saved)
    assert validate_package(out) == []

    # a reference outside textures/ (still inside the package, and to a file that exists), and one leaving the package
    (out / "albedo_copy.png").write_bytes(saved)
    assert "glb_image_outside" in _lod1_edit(out, lambda doc: doc["images"][0].update(uri="albedo_copy.png"))
    assert "glb_image_outside" in _lod1_edit(out, lambda doc: doc["images"][0].update(uri="../" + doc["images"][0]["uri"]))
    assert "glb_image_outside" in _lod1_edit(out, lambda doc: doc["images"][0].update(uri=albedo.resolve().as_uri()))


def test_validate_package_fails_a_far_lod_that_embeds_images(unreal, tmp_path):
    from antigravity3d.kernel.package import _sha256, _write_glb_level, gltf_materials

    sm, bundle, pm = unreal
    out = tmp_path / "pkg"
    shutil.copytree(pm.out_dir, out)
    materials = gltf_materials(Usd.Stage.Open(str(out / f"{ASSET}.usd")), sm.extract_meshes(), package_dir=out)
    lod1 = out / f"{ASSET}_LOD1.glb"
    # the same level written in the self-contained form (every image embedded again)
    _write_glb_level(lod1, {n: chain[1] for n, chain in bundle.lods.items()}, pm.data["glb_batches"], materials, "_LOD1")
    man = json.loads((out / "manifest.json").read_text())
    man["file_hashes"][lod1.name] = {"sha256": _sha256(lod1), "bytes": lod1.stat().st_size}
    (out / "manifest.json").write_text(json.dumps(man))
    codes = _codes(validate_package(out))
    assert "lod_embeds_image" in codes and "glb_images_manifest" in codes and "lod_texture_differs" not in codes
    # LOD0 is the single-file deliverable: a reference in it fails, although the file it names is fine
    glb = out / f"{ASSET}.glb"
    _write_glb_level(glb, {u.node_id: u for u in bundle.uvs}, pm.data["glb_batches"], materials, "",
                     image_uris={r["sha256"]: r["uri"] for r in pm.data["glb_images"][f"{ASSET}_LOD1.glb"]})
    man["file_hashes"][glb.name] = {"sha256": _sha256(glb), "bytes": glb.stat().st_size}
    (out / "manifest.json").write_text(json.dumps(man))
    assert "glb_not_self_contained" in _codes(validate_package(out))


def test_a_map_gltf_cannot_reference_stays_embedded_in_far_lods(tmp_path):
    """glTF allows only PNG and JPEG: a WebP source map is packaged as is and transcoded to PNG once for the GLBs.
    No package file holds those PNG bytes, so every level embeds that one image; the other maps are referenced."""
    rng = np.random.default_rng(1)
    maps = {}
    for k, ext in (("albedo", "webp"), ("orm", "png"), ("normal", "png")):
        Image.fromarray(rng.integers(0, 255, (32, 32, 3), dtype=np.uint8)).save(tmp_path / f"{k}.{ext}")
        maps[f"{k}_uri"] = str(tmp_path / f"{k}.{ext}")
    sm = StageManager.create(tmp_path / "w.usda", "Orb")
    sm.apply_ir(SceneIR(asset_name="Orb", materials=[MaterialIR(id="s", **maps)],
                        nodes=[NodeIR(id="b", kind="sphere", params={"radius": .6}, material_id="s")]))
    budget = EngineBudget("godot_4")
    pm = write_engine_package(sm, run_export_pipeline(sm.extract_meshes(), budget, "Orb"), budget, tmp_path / "out", asset_name="Orb")
    assert pm.ok and pm.n_lod_files >= 1 and validate_package(pm.out_dir) == []
    lod0 = {r["image"]: r for r in pm.data["glb_images"]["Orb.glb"]}
    assert [r["texture"] for r in lod0.values()] == [None, "textures/orm.png", "textures/normal.png"]
    for level in range(1, pm.data["lod_levels"]):
        recs = pm.data["glb_images"][f"Orb_LOD{level}.glb"]
        assert [r["storage"] for r in recs] == ["embedded", "uri", "uri"] and recs[0]["sha256"] == lod0[0]["sha256"]


def test_validator_rejects_uv_only_glb_even_with_matching_geometry_and_hashes(godot, tmp_path):
    from antigravity3d.kernel.fast_ops import write_gltf
    from antigravity3d.kernel.package import _sha256

    _, bundle, pm = godot
    out = tmp_path / "broken"
    shutil.copytree(pm.out_dir, out)
    glb = write_gltf(out / f"{ASSET}.glb", bundle.uvs)
    manifest = json.loads((out / "manifest.json").read_text())
    manifest["file_hashes"][glb.name]["sha256"] = _sha256(glb)
    (out / "manifest.json").write_text(json.dumps(manifest))
    assert "pkg:glb_material" in {issue.code for issue in validate_package(out)}


async def test_godot_loads_relocated_pbr_and_counts_real_surfaces(godot, tmp_path):
    from antigravity3d.adapters.engines.validators import GodotValidator

    validator = GodotValidator()
    if not validator.available():
        pytest.skip("set GODOT_BIN to run the stock Godot import")
    _, _, pm = godot
    glb = tmp_path / "relocated.glb"
    shutil.copyfile(pm.out_dir / f"{ASSET}.glb", glb)
    report = await validator.validate(glb, timeout=30)
    assert report.ok, report.log_tail
    assert report.metrics == {"triangles": pm.data["tris"], "surfaces": 3, "textured_surfaces": 3,
                              "normal_surfaces": 3, "orm_surfaces": 3, "images": 3, "images_loaded": 3}


async def test_godot_imports_far_lods_of_a_relocated_package_with_their_textures(godot, tmp_path):
    """The package moves as a directory; each far LOD file resolves textures/ next to it in stock Godot, binds every map
    on every surface, and an import whose referenced texture is gone fails."""
    from antigravity3d.adapters.engines.validators import GodotValidator

    validator = GodotValidator()
    if not validator.available():
        pytest.skip("set GODOT_BIN to run the stock Godot import")
    _, _, pm = godot
    moved = tmp_path / "elsewhere" / "pkg"
    shutil.copytree(pm.out_dir, moved)
    assert pm.n_lod_files >= 1 and validate_package(moved) == []
    for level in range(1, pm.data["lod_levels"]):
        lod = moved / f"{ASSET}_LOD{level}.glb"
        assert all(r["storage"] == "uri" for r in pm.data["glb_images"][lod.name])
        report = await validator.validate(lod, timeout=30)
        assert report.ok, (lod.name, report.log_tail)
        surfaces = len(pm.data["lod_glb_nodes"][level])
        assert report.metrics == {"triangles": pm.data["lod_tris"][level], "surfaces": surfaces, "textured_surfaces": surfaces,
                                  "normal_surfaces": surfaces, "orm_surfaces": surfaces, "images": 3, "images_loaded": 3}, lod.name
    (moved / next(t["usd_path"] for t in pm.data["textures"] if t["role"] == "albedo")).unlink()
    report = await validator.validate(moved / f"{ASSET}_LOD1.glb", timeout=30)
    assert not report.ok, report.log_tail


async def test_godot_renders_relocated_package(godot, tmp_path):
    from antigravity3d.adapters.engines.validators import GodotValidator

    _, _, pm = godot
    validator = GodotValidator()
    glb = tmp_path / "relocated.glb"
    shutil.copyfile(pm.out_dir / f"{ASSET}.glb", glb)
    png = tmp_path / "frame.png"
    report = await validator.capture(glb, png, timeout=45)
    if report.skipped:
        pytest.skip(report.skipped_reason)
    assert report.ok, report.log_tail
    with Image.open(png) as image:
        assert image.size == (512, 512) and len(image.convert("L").getcolors(256)) > 10


@pytest.mark.browser
def test_exported_inspector_opens_with_browser_offline(godot):
    import os

    if not os.environ.get("AGY3D_THREE_JS"):
        pytest.skip("run in the browser qualification job")
    from playwright.sync_api import sync_playwright

    path = godot[2].out_dir / "inspector.html"
    with sync_playwright() as pw:
        try:
            launched = pw.chromium.launch(executable_path=os.getenv("CHROMIUM_BIN"),
                                          args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        except Exception as e:  # same convention as the other browser tests: no browser here, not a failure
            if "Executable doesn't exist" in str(e):
                pytest.skip("no Chromium for this Playwright; set CHROMIUM_BIN")
            raise
        with launched as browser:
            ctx = browser.new_context(offline=True)
            page = ctx.new_page()
            page.goto(path.resolve().as_uri())
            page.wait_for_function("window.__inspectorReady || window.__inspectorError")
            assert page.evaluate("window.__inspectorState.glOk")
            for mode in ("albedo", "orm", "normal"):
                page.locator(f'button[data-mode="{mode}"]').click()
                assert page.locator("body").get_attribute("data-channel") == mode


def test_static_draw_batches_fit_limit_and_retain_prim_attribution(godot, tmp_path):
    sm, bundle, _ = godot
    budget = EngineBudget("godot_4", max_draw_calls=1)
    pm = write_engine_package(sm, bundle, budget, tmp_path, asset_name=ASSET)
    assert pm.ok and pm.data["draw_calls"] == 1
    assert sorted(pm.data["glb_batches"]["Batch_0"]) == ["ball", "base", "slab"]
    assert validate_package(tmp_path) == []
    glb = trimesh.load(tmp_path / f"{ASSET}.glb", force="scene")
    assert len(glb.geometry) == 1 and len(glb.geometry["Batch_0"].faces) == pm.data["tris"]


@pytest.fixture(scope="module")
def relocated_goldens(tmp_path_factory):
    root = tmp_path_factory.mktemp("portable_goldens")
    sources = root / "source_maps"
    sources.mkdir()
    maps = _maps(sources)
    goldens = []
    primitives = [
        ("Crate", "box", {"size": (1, .6, .9)}),
        ("Slab", "box", {"size": (2, .6, .15)}),
        ("RingBlank", "cylinder", {"radius": .7, "height": .2}),
        ("Pillar", "cylinder", {"radius": .2, "height": 1.5}),
        ("Cone", "cone", {"radius": .7, "height": .8}),
        ("Spire", "cone", {"radius": .4, "height": 1.6, "segments": 6}),
        ("Orb", "sphere", {"radius": .6}),
        ("FacetedOrb", "sphere", {"radius": .4, "segments": 8, "rings": 4}),
        ("Canister", "capsule", {"radius": .3, "height": .7}),
        ("Pin", "capsule", {"radius": .15, "height": 1.3}),
    ]
    for i, (name, kind, params) in enumerate(primitives):
        work = root / name
        work.mkdir()
        mat = MaterialIR(id="surface", normal_convention="opengl" if i % 2 else "directx", **maps)
        sm = StageManager.create(work / "working.usda", name)
        sm.apply_ir(SceneIR(asset_name=name, materials=[mat], nodes=[NodeIR(id="body", kind=kind, params=params,
                    transform=Transform(translate=(.1, -.2, .5)), material_id=mat.id)]))
        budget = EngineBudget("godot_4")
        bundle = run_export_pipeline(sm.extract_meshes(), budget, name)
        pm = write_engine_package(sm, bundle, budget, work / "out", asset_name=name)
        assert pm.ok, pm.issues
        destination = root / "relocated" / name
        destination.parent.mkdir(exist_ok=True)
        pm.out_dir.rename(destination)
        goldens.append((name, destination))
    shutil.rmtree(sources)
    return goldens


async def test_ten_relocated_golden_packages_load_in_stock_godot(relocated_goldens):
    from antigravity3d.adapters.engines.validators import GodotValidator

    validator = GodotValidator()
    if not validator.available():
        pytest.skip("Godot binary required for the ten-package import test")
    far_lods = 0
    for name, pkg in relocated_goldens:
        assert validate_package(pkg) == []
        manifest = json.loads((pkg / "manifest.json").read_text())
        report = await validator.validate(pkg / f"{name}.glb", timeout=30)
        assert report.ok, (name, report.log_tail)
        assert report.geometry_issue(manifest) is None, (name, report.bounds_m)
        assert report.metrics == {"triangles": manifest["tris"], "surfaces": 1, "textured_surfaces": 1,
                                  "normal_surfaces": 1, "orm_surfaces": 1, "images": 3, "images_loaded": 3}
        # far LOD files reference the relocated package's textures/: the import must find and bind all three maps
        for level in range(1, manifest["lod_levels"]):
            lod = pkg / f"{name}_LOD{level}.glb"
            report = await validator.validate(lod, timeout=30)
            assert report.ok, (lod.name, report.log_tail)
            assert report.metrics == {"triangles": manifest["lod_tris"][level], "surfaces": 1, "textured_surfaces": 1,
                                      "normal_surfaces": 1, "orm_surfaces": 1, "images": 3, "images_loaded": 3}, lod.name
            far_lods += 1
    assert far_lods >= 1


@pytest.mark.blender
async def test_ten_relocated_golden_dcc_engine_silhouettes(relocated_goldens, tmp_path):
    import os
    from antigravity3d.adapters.blender_5_3.pool import BlenderPool, find_blender
    from antigravity3d.adapters.engines.validators import GodotValidator, MIN_SILHOUETTE_IOU, blender_frame_png, silhouette_iou

    validator = GodotValidator()
    if not find_blender() or not validator.available():
        pytest.skip("Blender 5.3 and Godot required for measured engine parity")
    if not os.getenv("DISPLAY") and not os.getenv("WAYLAND_DISPLAY") and not shutil.which("xvfb-run"):
        pytest.skip("graphics renderer required for stock Godot capture")
    async with BlenderPool(1, startup_timeout=120) as pool:
        for name, pkg in relocated_goldens:
            dcc = await pool.call("render_usd", path=str(pkg / f"{name}.usd"), engine="cycles",
                                  width=512, height=512, samples=2, denoiser="none", transparent=True)
            png = tmp_path / f"{name}.png"
            capture = await validator.capture(pkg / f"{name}.glb", png, timeout=45)
            assert capture.ok, (name, capture.log_tail or capture.skipped_reason)
            assert silhouette_iou(blender_frame_png(dcc["image"]), png.read_bytes()) >= MIN_SILHOUETTE_IOU, name


def test_missing_source_map_prevents_package_approval(src, tmp_path):
    local = tmp_path / "materials"
    shutil.copytree(src, local)
    sm = _stage(tmp_path, local)
    budget = EngineBudget("godot_4")
    bundle = run_export_pipeline(sm.extract_meshes(), budget, ASSET)
    (local / "orm.png").unlink()
    manifest = write_engine_package(sm, bundle, budget, tmp_path / "draft", asset_name=ASSET)
    assert not manifest.ok and manifest.data["validation"]["package_ok"] is False
    assert {i.code for i in manifest.issues if i.severity == "critical"} >= {"pkg:texture_missing", "pkg:glb_material"}


async def test_godot_measures_nested_node_transforms_and_rejects_changed_bounds(godot, tmp_path):
    import hashlib
    import struct
    from antigravity3d.adapters.engines.validators import GodotValidator

    validator = GodotValidator()
    if not validator.available():
        pytest.skip("Godot required to measure transformed vertices")
    _, _, pm = godot
    pkg = tmp_path / "transformed"
    shutil.copytree(pm.out_dir, pkg)
    glb = pkg / f"{ASSET}.glb"
    original = glb.read_bytes()
    length = struct.unpack_from("<I", original, 12)[0]
    doc = json.loads(original[20:20 + length])
    scene = doc["scenes"][doc.get("scene", 0)]
    scaled = len(doc["nodes"])
    doc["nodes"].append({"name": "ScaledParent", "scale": [2, 3, 4], "children": scene["nodes"]})
    scene["nodes"] = [scaled + 1]
    # Reverse X and Z with a half turn about Y, then translate. All source triangles remain intact.
    doc["nodes"].append({"name": "Outer", "rotation": [0, 1, 0, 0], "translation": [5, 6, 7], "children": [scaled]})
    data = json.dumps(doc, separators=(",", ":")).encode()
    data += b" " * (-len(data) % 4)
    rest = original[20 + length:]
    glb.write_bytes(struct.pack("<4sIII4s", b"glTF", 2, 20 + len(data) + len(rest), len(data), b"JSON") + data + rest)
    manifest = json.loads((pkg / "manifest.json").read_text())
    manifest["file_hashes"][glb.name] = {"sha256": hashlib.sha256(glb.read_bytes()).hexdigest(), "bytes": glb.stat().st_size}
    (pkg / "manifest.json").write_text(json.dumps(manifest))

    imp = await validator.validate(glb, timeout=30)
    assert imp.ok and imp.metrics["triangles"] == manifest["tris"]
    transformed = np.array(manifest["glb_bounds_m"]) * [-2, 3, -4] + [5, 6, 7]
    np.testing.assert_allclose(imp.bounds_m, [transformed.min(0), transformed.max(0)], atol=1e-5)
    assert "bounds drift" in imp.geometry_issue(manifest)
    assert "pkg:glb_bounds" in {issue.code for issue in validate_package(pkg)}


# --------------------------------------------------------------------------- collision hulls in stock Godot

_GODOT_HULLS = """extends SceneTree

func _init():
    call_deferred("run")

func run():
    var out := {}
    for file in OS.get_environment("AGY_HULL_FILES").split(","):
        var packed = load("res://" + file)
        if packed == null:
            out[file] = {"error": "load failed"}; continue
        var scene: Node = packed.instantiate()
        root.add_child(scene)
        var shapes := []
        for s in scene.find_children("*", "CollisionShape3D", true, false):
            var n := -1
            if s.shape is ConvexPolygonShape3D:
                n = s.shape.points.size()
            shapes.append({"class": s.shape.get_class(), "points": n, "body": s.get_parent().get_class()})
        await physics_frame
        await physics_frame
        var space = root.world_3d.direct_space_state
        var hits := []
        var count := 400
        for i in count:
            var y := 1.0 - (i + 0.5) * 2.0 / count
            var r := sqrt(1.0 - y * y)
            var d := Vector3(cos(i * 2.399963229728653) * r, y, sin(i * 2.399963229728653) * r)
            var hit = space.intersect_ray(PhysicsRayQueryParameters3D.create(d * 10.0, Vector3.ZERO))
            hits.append(hit.position.length() if hit.has("position") else -1.0)
        out[file] = {"shapes": shapes, "hits": hits}
        scene.free()
        await physics_frame
    out["engine"] = str(ProjectSettings.get_setting("physics/3d/physics_engine"))
    print("AGY_HULLS ", JSON.stringify(out))
    quit(0)
"""


def _ray_radius(points: np.ndarray, dirs: np.ndarray) -> np.ndarray:
    """Distance from the origin along each direction to the surface of the convex hull of ``points``."""
    from scipy.spatial import ConvexHull

    eq = ConvexHull(points).equations
    nd = dirs @ eq[:, :3].T
    with np.errstate(divide="ignore"):
        return np.where(nd > 1e-12, -eq[:, 3] / nd, np.inf).min(axis=1)


def test_capped_hull_imports_in_stock_godot_as_its_collision_shape(tmp_path):
    """A godot_4 package of a dense sphere ships a hull capped to Jolt's 256 points. Stock Godot (Jolt, the default for
    new projects) imports ``<asset>_colliders.glb`` through its ``-convcolonly`` naming hint as one static
    ConvexPolygonShape3D with every shipped point, and ray casts meet it where the shipped hull is, never more than 1 mm
    inside the part's own hull. The uncapped 1,986-point hull of the same part, by contrast, comes back smaller than
    itself (Jolt stops building at 256 points): that is the defect the cap removes."""
    import os
    import subprocess

    from antigravity3d.kernel.fast_ops import generate_colliders, write_gltf

    exe = os.environ.get("GODOT_BIN") or shutil.which("godot")
    if not exe:
        pytest.skip("set GODOT_BIN to run the stock Godot import")
    sm = StageManager.create(tmp_path / "ball.usda", "Ball")
    sm.apply_ir(SceneIR(asset_name="Ball", nodes=[NodeIR(id="ball", kind="sphere", params={"radius": 0.5, "segments": 64, "rings": 32})]))
    budget = EngineBudget("godot_4")
    bundle = run_export_pipeline(sm.extract_meshes(), budget, "Ball")
    pm = write_engine_package(sm, bundle, budget, tmp_path / "pkg", asset_name="Ball")
    assert pm.ok and validate_package(pm.out_dir) == []
    (capped,) = bundle.colliders
    (raw,) = generate_colliders(sm.extract_meshes(), budget.max_collision_hulls, asset_name="Ball")
    assert len(capped.points) <= 256 < len(raw.points)
    proj = tmp_path / "godot"
    proj.mkdir()
    (proj / "project.godot").write_text('config_version=5\n[application]\nconfig/name="hulls"\n[physics]\n3d/physics_engine="Jolt Physics"\n')
    (proj / "check.gd").write_text(_GODOT_HULLS)
    shutil.copyfile(pm.out_dir / "Ball_colliders.glb", proj / "capped.glb")
    write_gltf(proj / "raw.glb", [raw], [collider_name("godot_4", "Ball", 0, usd=False)])
    env = {**os.environ, "AGY_HULL_FILES": "capped.glb,raw.glb"}
    subprocess.run([exe, "--headless", "--path", str(proj), "--import"], env=env, capture_output=True, timeout=180, check=True)
    run = subprocess.run([exe, "--headless", "--path", str(proj), "--script", "check.gd"], env=env, capture_output=True,
                         text=True, timeout=180)
    line = next((ln for ln in run.stdout.splitlines() if ln.startswith("AGY_HULLS ")), None)
    assert line, run.stdout[-2000:] + run.stderr[-2000:]
    out = json.loads(line.removeprefix("AGY_HULLS "))
    assert out["engine"] == "Jolt Physics"
    i = np.arange(400) + 0.5
    y = 1.0 - 2.0 * i / 400
    dirs = np.c_[np.cos((i - 0.5) * 2.399963229728653) * np.sqrt(1 - y * y), y, np.sin((i - 0.5) * 2.399963229728653) * np.sqrt(1 - y * y)]
    to_y_up = lambda p: np.c_[p[:, 0], p[:, 2], -p[:, 1]]  # noqa: E731 - the GLB's axes
    part = _ray_radius(to_y_up(raw.points), dirs)            # the part's own hull
    for file, hull in (("capped.glb", capped), ("raw.glb", raw)):
        rec = out[file]
        assert rec.get("shapes") == [{"class": "ConvexPolygonShape3D", "points": len(hull.points), "body": "StaticBody3D"}], rec
        assert min(rec["hits"]) > 0
    capped_hits = np.asarray(out["capped.glb"]["hits"])
    assert np.abs(capped_hits - _ray_radius(to_y_up(capped.points), dirs)).max() < 1e-3
    assert (capped_hits - part).min() > -1e-3
    assert (np.asarray(out["raw.glb"]["hits"]) - part).min() < -2e-3


async def test_godot_draws_a_package_without_hidden_contact_faces_like_the_closed_one(tmp_path):
    """Faces two parts press together are left out of the render meshes (fast_ops.run_export_pipeline): stock Godot
    imports the open meshes with the manifest's triangle counts, and its frame is the closed package's, pixel for
    pixel."""
    import os
    from PIL import Image
    from antigravity3d.adapters.engines.validators import GodotValidator
    from antigravity3d.kernel.coplanar import opaque_materials

    validator = GodotValidator()
    if not validator.available():
        pytest.skip("Godot binary required")
    if not os.getenv("DISPLAY") and not os.getenv("WAYLAND_DISPLAY") and not shutil.which("xvfb-run"):
        pytest.skip("graphics renderer required for stock Godot capture")

    def node(i, lo, hi, kind="box", **params):
        lo, hi = np.asarray(lo, float), np.asarray(hi, float)
        params = params or {"size": tuple((hi - lo).tolist())}
        return NodeIR(id=i, kind=kind, params=params, transform=Transform(translate=tuple(((lo + hi) / 2).tolist())),
                      material_id="wood" if i != "post" else "iron")

    scene = SceneIR(asset_name=ASSET, materials=[MaterialIR(id="wood", base_color=(.5, .35, .2)), MaterialIR(id="iron", base_color=(.3, .3, .3))],
                    nodes=[node("base", (-.5, -.5, 0), (.5, .5, .4)), node("lid", (-.5, -.5, .4), (.5, .5, .5)),
                           node("post", (-.1, -.1, .5), (.1, .1, 1.1), "cylinder", radius=.1, height=.6, segments=16)])
    frames, tris = {}, {}
    for closed in (True, False):
        sm = StageManager.create(tmp_path / f"w{closed}.usda", ASSET)
        sm.apply_ir(scene)
        budget = EngineBudget("godot_4", keep_contact_faces=closed)
        bundle = run_export_pipeline(sm.extract_meshes(), budget, ASSET, opaque_materials=opaque_materials(scene))
        pm = write_engine_package(sm, bundle, budget, tmp_path / f"p{closed}", asset_name=ASSET)
        assert pm.ok, pm.issues
        assert pm.data["contact_faces"]["kept_by_request"] is closed
        report = await validator.validate(pm.out_dir / f"{ASSET}.glb", timeout=45)
        assert report.ok and report.metrics["triangles"] == pm.data["tris"], report.log_tail
        assert report.geometry_issue(pm.data) is None
        png = tmp_path / f"{closed}.png"
        capture = await validator.capture(pm.out_dir / f"{ASSET}.glb", png, timeout=45)
        assert capture.ok, capture.log_tail or capture.skipped_reason
        frames[closed], tris[closed] = np.asarray(Image.open(png).convert("RGBA")), pm.data["tris"]
    assert tris[False] == tris[True] - 20  # base top and lid bottom, the post's bottom cap
    assert np.array_equal(frames[True], frames[False])
