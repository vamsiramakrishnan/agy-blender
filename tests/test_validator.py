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
    # web_three (three.js projects) is the fifth target: its row and where each value comes from are in tests/test_web_target.py
    assert set(ENGINE_RULES) == {"unreal_5", "unity_urp", "godot_4", "roblox_r15", "web_three"}


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


# --- engine hull limits, Roblox per-mesh cap, power-of-two textures, scale plausibility ---------------------------


def _ball_package(tmp_path, engine: str, capped: bool):
    from dataclasses import replace as dc_replace

    from antigravity3d.ir import NodeIR, SceneIR
    from antigravity3d.kernel.fast_ops import generate_colliders, run_export_pipeline
    from antigravity3d.kernel.package import write_engine_package
    from antigravity3d.kernel.usd_stage import StageManager

    sm = StageManager.create(tmp_path / f"{engine}{capped}.usda", "Ball")
    sm.apply_ir(SceneIR(asset_name="Ball", nodes=[NodeIR(id="ball", kind="sphere", params={"radius": 0.5, "segments": 64, "rings": 32})]))
    budget = EngineBudget(engine)
    bundle = run_export_pipeline(sm.extract_meshes(), budget, "Ball")
    if not capped:  # what a caller that passes no limit would ship
        bundle = dc_replace(bundle, colliders=generate_colliders(sm.extract_meshes(), budget.max_collision_hulls, asset_name="Ball"))
    return write_engine_package(sm, bundle, budget, tmp_path / f"pkg_{engine}_{capped}", asset_name="Ball")


def test_package_validator_fails_a_hull_over_the_engine_limit_in_usd_and_glb(tmp_path):
    from antigravity3d.kernel.validator import validate_package

    for engine in ("godot_4", "unity_urp"):
        bad = _ball_package(tmp_path, engine, capped=False)
        issues = [i for i in validate_package(bad.out_dir) if i.code == "pkg:collider_limit"]
        assert {i.severity for i in issues} == {"critical"} and len(issues) == 2, issues  # the USD prim and the GLB node
        assert any("Ball_colliders.glb" in i.detail for i in issues)
        assf = [i for i in bad.issues if i.code == f"{engine}:hull_limit"]
        assert assf and assf[0].severity == "critical" and not bad.ok
        good = _ball_package(tmp_path, engine, capped=True)
        assert good.ok and validate_package(good.out_dir) == []
        assert "capped to the engine's hull limit" in good.data["collider_sources"][0]["reason"]
    # Roblox reads no collision hulls: an uncapped hull is not a failure there
    rb = _ball_package(tmp_path, "roblox_r15", capped=False)
    assert not [i for i in validate_package(rb.out_dir) if i.code == "pkg:collider_limit"]


def test_hull_limit_counts_what_each_engine_counts():
    from antigravity3d.kernel.validator import hull_limit_issue

    from antigravity3d.kernel.fast_ops import _hull

    a = np.arange(128) * 2 * np.pi / 128
    ring = np.c_[0.3 * np.cos(a), 0.3 * np.sin(a)]
    p, t, _ = _hull(np.vstack([np.c_[ring, np.zeros(128)], np.c_[ring, np.ones(128)]]))  # 256 vertices, 508 triangles, 130 faces
    assert "508 triangles > 255" in hull_limit_issue("unity_urp", p, t)
    assert "256 vertices > 255" in hull_limit_issue("unreal_5", p, t) and "planar faces" not in hull_limit_issue("unreal_5", p, t)
    assert hull_limit_issue("godot_4", p, t) == ""              # 256 points: Jolt's maximum
    assert hull_limit_issue("roblox_r15", p, t) == ""


def test_roblox_mesh_cap_is_the_documented_20000():
    tm = trimesh.creation.icosphere(4, 1.0)  # 5120 tris
    big = trimesh.util.concatenate([tm.copy().apply_translation([3 * i, 0, 0]) for i in range(3)])  # 15,360 triangles
    m = MeshData("Big", "/Asset/Big", np.array(big.vertices), np.array(big.faces, np.int32), "m")
    assert ENGINE_RULES["roblox_r15"].max_tris_per_mesh == 20_000
    iss = check_engine_compliance(mk_stage(up="Y", names=("Big",)), [m], EngineBudget("roblox_r15", max_tris=50_000))
    assert "mesh_tri_cap" not in codes(iss)


def test_power_of_two_texture_warnings_follow_each_engine(tmp_path):
    from PIL import Image

    img = tmp_path / "t.png"
    Image.new("RGB", (1000, 512)).save(img)
    st = mk_stage(up="Y")
    UsdShade.Material.Define(st, "/Asset/Mat")
    sh = UsdShade.Shader.Define(st, "/Asset/Mat/Tex")
    sh.CreateIdAttr("UsdUVTexture")
    sh.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(Sdf.AssetPath(str(img)))
    for engine, warned in (("unreal_5", True), ("unity_urp", True), ("godot_4", False), ("roblox_r15", False)):
        iss = check_engine_compliance(st, [md("Crate")], EngineBudget(engine))
        assert ("texture_npot" in codes(iss, "warning")) is warned, engine
        assert "texture_npot" not in codes(iss, "critical")


def test_implausible_scale_is_a_warning_with_the_unit_readings():
    from antigravity3d.kernel.validator import scale_issue

    crate = trimesh.creation.box((80, 80, 70))  # an 80 cm crate written in centimetres, read as metres
    m = MeshData("crate", "/Asset/crate", np.array(crate.vertices), np.array(crate.faces, np.int32), "m")
    why = scale_issue([m])
    assert "80 m" in why and "centimetres read as metres: 0.8 m" in why and "Not rescaled" in why
    iss = [i for i in check_engine_compliance(None, [m], EngineBudget("unreal_5")) if i.code.endswith("implausible_scale")]
    assert len(iss) == 1 and iss[0].severity == "warning"
    tiny = MeshData("t", "/t", np.array(crate.vertices) * 1e-4, np.array(crate.faces, np.int32), "m")
    assert "below 0.01 m" in scale_issue([tiny]) and "x100" in scale_issue([tiny])
    for size in ((0.8, 0.7, 0.9), (2.0, 0.24, 3.4), (12.0, 12.0, 6.0)):  # crate, doorway, a large environment piece
        box = trimesh.creation.box(size)
        assert scale_issue([MeshData("b", "/b", np.array(box.vertices), np.array(box.faces, np.int32), "m")]) == ""
