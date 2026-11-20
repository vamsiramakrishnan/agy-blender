from __future__ import annotations

import numpy as np
import trimesh
from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade, Vt

from antigravity3d.kernel.types import MeshData
from antigravity3d.kernel.validator import ENGINE_RULES, check_engine_compliance, validate_usd
from antigravity3d.state import EngineBudget


def mk_stage(up="Z", mpu=1.0, meta=True, names=("Crate",), uv=True, hulls=()) -> Usd.Stage:
    st = Usd.Stage.CreateInMemory()
    if meta:
        UsdGeom.SetStageUpAxis(st, up)
        UsdGeom.SetStageMetersPerUnit(st, mpu)
    root = UsdGeom.Xform.Define(st, "/Asset")
    st.SetDefaultPrim(root.GetPrim())
    for n in (*names, *hulls):
        m = UsdGeom.Mesh.Define(st, f"/Asset/{n}")
        m.CreatePointsAttr(Vt.Vec3fArray([Gf.Vec3f(0, 0, 0), Gf.Vec3f(1, 0, 0), Gf.Vec3f(0, 1, 0)]))
        m.CreateFaceVertexCountsAttr([3])
        m.CreateFaceVertexIndicesAttr([0, 1, 2])
        if uv and n not in hulls:
            pv = UsdGeom.PrimvarsAPI(m).CreatePrimvar("st", Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.vertex)
            pv.Set(Vt.Vec2fArray([Gf.Vec2f(0, 0), Gf.Vec2f(1, 0), Gf.Vec2f(0, 1)]))
    return st


def md(nid: str, tris: int = 12) -> MeshData:
    tm = trimesh.creation.icosphere(1, 0.5) if tris > 12 else trimesh.creation.box()
    return MeshData(nid, f"/Asset/{nid}", np.array(tm.vertices, float), np.array(tm.faces, np.int32), "m")


def codes(issues, sev=None):
    return {i.code.split(":", 1)[-1] for i in issues if sev is None or i.severity == sev}


def test_validate_usd_clean_and_broken(tmp_path):
    assert [i for i in validate_usd(mk_stage()) if i.severity == "critical"] == []
    bad = validate_usd(mk_stage(meta=False))
    ids = " ".join(i.code for i in bad)
    assert "MissingUpAxisMetadata" in ids and "MissingMetersPerUnitMetadata" in ids
    assert all(i.severity == "critical" for i in bad)
    p = tmp_path / "a.usda"
    mk_stage(meta=False).Export(str(p))
    assert any("MissingUpAxis" in i.code for i in validate_usd(p))
    assert validate_usd(tmp_path / "nope.usda")[0].code == "usd_open_failed"


def test_rule_table_covers_all_engines():
    assert set(ENGINE_RULES) == {"unreal_5", "unity_urp", "godot_4", "roblox_r15"}


def test_unreal_rules():
    b = EngineBudget("unreal_5", max_collision_hulls=2)
    ok = check_engine_compliance(mk_stage(hulls=("UCX_Crate_00", "UCX_Crate_01")), [md("Crate")], b)
    assert not codes(ok, "critical"), ok
    bad = check_engine_compliance(mk_stage(hulls=("UCX_Crate_0", "UCX_Crate_01", "UCX_Crate_02")), [md("Crate")], b)
    assert {"hull_name", "hull_count"} <= codes(bad, "critical")
    assert "up_axis_reorient" in codes(check_engine_compliance(mk_stage(up="Y"), [md("Crate")], b), "warning")
    assert "meters_per_unit" in codes(check_engine_compliance(mk_stage(mpu=0.5), [md("Crate")], b), "warning")
    assert "up_axis_missing" in codes(check_engine_compliance(mk_stage(meta=False), [md("Crate")], b), "critical")


def test_names_and_uvs():
    for eng, nm in [("unreal_5", "my crate"), ("godot_4", "a.b"), ("roblox_r15", "crate-1")]:
        iss = check_engine_compliance(mk_stage(names=(nm.replace(" ", "_") if False else "ok",)), [md(nm)], EngineBudget(eng))
        assert "bad_name" in codes(iss, "critical"), eng
    iss = check_engine_compliance(mk_stage(uv=False), [md("Crate")], EngineBudget("unity_urp"))
    assert "uv_missing" in codes(iss, "warning")  # no textures authored -> warning only


def test_roblox_per_mesh_cap_and_budgets():
    big = trimesh.creation.icosphere(5, 1.0)  # 20480 tris
    m = MeshData("Big", "/Asset/Big", np.array(big.vertices), np.array(big.faces, np.int32), "m")
    iss = check_engine_compliance(mk_stage(up="Y", names=("Big",)), [m], EngineBudget("roblox_r15", max_tris=50_000))
    assert "mesh_tri_cap" in codes(iss, "critical") and "up_axis" not in codes(iss)
    assert "up_axis" in codes(check_engine_compliance(mk_stage(up="Z"), [m], EngineBudget("roblox_r15", max_tris=50_000)), "critical")
    small = check_engine_compliance(None, [md("A"), md("B")], EngineBudget("unreal_5", max_tris=10, max_draw_calls=1))
    assert "tri_budget" in codes(small, "critical")
    two = [md("A"), MeshData("B", "/B", md("A").points, md("A").triangles, "other")]
    assert "draw_call_budget" in codes(check_engine_compliance(None, two, EngineBudget("unity_urp", max_draw_calls=1)), "critical")


def test_texture_hints(tmp_path):
    from PIL import Image

    img = tmp_path / "t.png"
    Image.new("RGB", (2048, 1000)).save(img)
    st = mk_stage(up="Y")
    mat = UsdShade.Material.Define(st, "/Asset/Mat")
    sh = UsdShade.Shader.Define(st, "/Asset/Mat/Tex")
    sh.CreateIdAttr("UsdUVTexture")
    sh.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(Sdf.AssetPath(str(img)))
    iss = check_engine_compliance(st, [md("Crate")], EngineBudget("roblox_r15"))
    assert "texture_size" in codes(iss, "warning")
    iss = check_engine_compliance(st, [md("Crate")], EngineBudget("unreal_5"))
    assert "texture_npot" in codes(iss, "warning")


# --- UV rule (textured material vs st) and collider exemption ------------------------------------


def _textured_stage(uv: bool, tex: bool = True) -> Usd.Stage:
    st = mk_stage(uv=uv)
    mat = UsdShade.Material.Define(st, "/Asset/Looks/m")
    sh = UsdShade.Shader.Define(st, "/Asset/Looks/m/albedoTex")
    sh.CreateIdAttr("UsdUVTexture")
    if tex:
        sh.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(Sdf.AssetPath("./textures/a.png"))
    UsdShade.MaterialBindingAPI.Apply(st.GetPrimAtPath("/Asset/Crate")).Bind(mat)
    return st


def test_uv_missing_is_critical_only_when_the_meshs_material_has_textures():
    b = EngineBudget("unreal_5")
    bad = check_engine_compliance(_textured_stage(uv=False), [md("Crate")], b)
    assert [i for i in bad if i.code.endswith("uv_missing")][0].severity == "critical"
    assert not [i for i in check_engine_compliance(_textured_stage(uv=True), [md("Crate")], b) if i.code.endswith("uv_missing")]
    plain = [i for i in check_engine_compliance(_textured_stage(uv=False, tex=False), [md("Crate")], b) if i.code.endswith("uv_missing")]
    assert plain and plain[0].severity == "warning"


def test_physics_tagged_collider_is_not_a_uv_offender_in_any_engine():
    from pxr import UsdPhysics

    for eng in ("unreal_5", "unity_urp", "godot_4", "roblox_r15"):
        st = _textured_stage(uv=True)
        h = UsdGeom.Mesh.Define(st, "/Asset/Colliders/UCX_Asset_00")
        h.CreatePointsAttr(Vt.Vec3fArray([Gf.Vec3f(0, 0, 0), Gf.Vec3f(1, 0, 0), Gf.Vec3f(0, 1, 0)]))
        h.CreateFaceVertexCountsAttr([3])
        h.CreateFaceVertexIndicesAttr([0, 1, 2])
        UsdPhysics.CollisionAPI.Apply(h.GetPrim())
        issues = check_engine_compliance(st, [md("Crate")], EngineBudget(eng, max_collision_hulls=4))
        assert not [i for i in issues if i.code.endswith("uv_missing")], eng


def test_validate_package_requires_a_manifest(tmp_path):
    from antigravity3d.kernel.validator import validate_package

    assert [i.code for i in validate_package(tmp_path)] == ["pkg:manifest_missing"]
    (tmp_path / "manifest.json").write_text("{nope")
    assert [i.code for i in validate_package(tmp_path)] == ["pkg:manifest_unreadable"]
