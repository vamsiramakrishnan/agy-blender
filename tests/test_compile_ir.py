from __future__ import annotations

import math

import numpy as np
import pytest
import trimesh
from pxr import Sdf, Usd, UsdGeom, UsdShade

from antigravity3d.ir import MaterialIR, NodeIR, SceneIR, Transform
from antigravity3d.kernel.compile_ir import author_scene, primitive_mesh, read_ir

PRIMS = [
    NodeIR(id="b", kind="box", params={"size": (1.0, 2.0, 3.0)}),
    NodeIR(id="cy", kind="cylinder", params={"radius": 0.5, "height": 2.0}),
    NodeIR(id="co", kind="cone", params={"radius": 0.5, "height": 2.0, "segments": 7}),
    NodeIR(id="s", kind="sphere", params={"radius": 1.0}),
    NodeIR(id="s3", kind="sphere", params={"radius": 1.0, "segments": 3, "rings": 2}),
    NodeIR(id="ca", kind="capsule", params={"radius": 0.25, "height": 1.0}),
    NodeIR(id="ca0", kind="capsule", params={"radius": 0.25, "height": 0}),
]


def _polygon_area(r: float, n: int) -> float:
    return 0.5 * n * r * r * math.sin(2 * math.pi / n)


@pytest.mark.parametrize("node", PRIMS, ids=lambda n: n.id)
def test_primitive_is_closed_outward_manifold(node: NodeIR) -> None:
    pts, tris = primitive_mesh(node)
    assert pts.dtype == np.float32 and pts.shape[1] == 3
    assert tris.dtype == np.int32 and tris.shape[1] == 3
    m = trimesh.Trimesh(pts, tris, process=False)
    assert m.is_watertight and m.is_winding_consistent
    assert m.volume > 0  # outward winding
    assert len(np.unique(pts.round(6), axis=0)) == len(pts)  # no duplicated seam vertices
    assert m.bounds[0][2] == pytest.approx(-m.bounds[1][2], abs=1e-6)  # centred on the local origin
    assert np.all(m.bounds[0] < 0) and np.all(m.bounds[1] > 0)  # (odd segment counts aren't x-symmetric)


def test_primitive_volumes() -> None:
    vol = lambda n: trimesh.Trimesh(*primitive_mesh(n), process=False).volume  # noqa: E731
    box, cyl, cone, sph, _, cap, _ = PRIMS
    assert vol(box) == pytest.approx(6.0, rel=1e-6)
    assert vol(cyl) == pytest.approx(_polygon_area(0.5, 24) * 2.0, rel=1e-5)
    assert vol(cone) == pytest.approx(_polygon_area(0.5, 7) * 2.0 / 3, rel=1e-5)
    assert vol(sph) == pytest.approx(4 / 3 * math.pi, rel=0.05)
    assert vol(cap) == pytest.approx(math.pi * 0.25**2 * (1.0 + 4 / 3 * 0.25), rel=0.1)
    cone_pts = primitive_mesh(cone)[0]
    assert cone_pts[:, 2].max() == pytest.approx(1.0) and cone_pts[:, 2].min() == pytest.approx(-1.0)


@pytest.mark.parametrize(
    "params,kind",
    [
        ({}, "cylinder"),
        ({"radius": -1.0, "height": 1.0}, "cylinder"),
        ({"radius": 1.0, "height": 0.0}, "cone"),
        ({"radius": 1.0, "segments": 2}, "sphere"),
        ({"radius": 1.0, "rings": 1.5}, "sphere"),
        ({"radius": "big"}, "sphere"),
        ({"size": (1.0, 0.0, 1.0)}, "box"),
        ({"size": 1.0}, "box"),
        ({"radius": 1.0}, "capsule"),
    ],
)
def test_invalid_params_raise(params: dict, kind: str) -> None:
    with pytest.raises(ValueError, match="^bad_node: "):
        primitive_mesh(NodeIR(id="bad_node", kind=kind, params=params))


def _scene(tmp_path) -> SceneIR:
    usd_ref = tmp_path / "ref.usda"
    ref_stage = Usd.Stage.CreateNew(str(usd_ref))
    ref_stage.SetDefaultPrim(UsdGeom.Xform.Define(ref_stage, "/R").GetPrim())
    UsdGeom.Cube.Define(ref_stage, "/R/cube")
    ref_stage.Save()
    return SceneIR(
        asset_name="Prop",
        materials=[
            MaterialIR(id="plain", base_color=(1, 0, 0), metallic=1.0, roughness=0.2),
            MaterialIR(id="tex", albedo_uri="a.png", orm_uri="orm.png", normal_uri="n.png", normal_convention="directx"),
        ],
        nodes=[
            NodeIR(id="grp", kind="group", transform=Transform(translate=(0, 0, 1))),
            NodeIR(id="body", kind="box", params={"size": (1, 1, 1)}, parent="grp", material_id="tex"),
            NodeIR(id="knob", kind="sphere", params={"radius": 0.1}, material_id="plain", collider=False),
            NodeIR(id="scan", kind="mesh_ref", params={"path": "scan.glb"}),
            NodeIR(id="lib", kind="mesh_ref", params={"path": str(usd_ref)}),
        ],
    )


def test_author_scene_structure(tmp_path) -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _scene(tmp_path)
    author_scene(stage, scene)

    root = stage.GetDefaultPrim()
    assert root.GetPath() == Sdf.Path("/Prop") and root.IsA(UsdGeom.Xform)
    assert Usd.ModelAPI(root).GetKind() == "component"
    assert UsdGeom.GetStageUpAxis(stage) == "Z" and UsdGeom.GetStageMetersPerUnit(stage) == 1.0
    assert read_ir(root) == scene

    body = UsdGeom.Mesh(stage.GetPrimAtPath("/Prop/grp/body"))
    assert body and body.GetSubdivisionSchemeAttr().Get() == "none"
    assert len(body.GetPointsAttr().Get()) == 8 and len(body.GetExtentAttr().Get()) == 2
    assert set(body.GetFaceVertexCountsAttr().Get()) == {3}
    assert body.GetPrim().GetAttribute("agy:collider").Get() is True
    assert stage.GetPrimAtPath("/Prop/knob").GetAttribute("agy:collider").Get() is False
    assert UsdGeom.Xformable(stage.GetPrimAtPath("/Prop/grp")).GetLocalTransformation()[3][2] == 1.0

    bound, _ = UsdShade.MaterialBindingAPI(body.GetPrim()).ComputeBoundMaterial()
    assert bound.GetPath() == Sdf.Path("/Prop/Looks/tex")
    assert bound.GetPrim().GetCustomDataByKey("agy:normalConvention") == "directx"
    surf = UsdShade.Shader(stage.GetPrimAtPath("/Prop/Looks/tex/Surface"))
    wiring = {
        name: (src.GetPath().name, out)
        for name in ("roughness", "metallic", "occlusion", "diffuseColor", "normal")
        for src, out, _ in [surf.GetInput(name).GetConnectedSource()]
    }
    assert wiring == {
        "roughness": ("ormTex", "g"),
        "metallic": ("ormTex", "b"),
        "occlusion": ("ormTex", "r"),
        "diffuseColor": ("albedoTex", "rgb"),
        "normal": ("normalTex", "rgb"),
    }
    plain = UsdShade.Shader(stage.GetPrimAtPath("/Prop/Looks/plain/Surface"))
    assert plain.GetInput("metallic").Get() == 1.0
    assert not stage.GetPrimAtPath("/Prop/Looks/plain/stReader")

    assert stage.GetPrimAtPath("/Prop/scan").GetAttribute("agy:meshRef").Get() == "scan.glb"
    assert stage.GetPrimAtPath("/Prop/lib/ref/cube").IsA(UsdGeom.Cube)


def test_author_scene_is_a_delta(tmp_path) -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _scene(tmp_path)
    author_scene(stage, scene)

    layer = Sdf.Layer.CreateAnonymous("delta")
    stage.GetSessionLayer().subLayerPaths.insert(0, layer.identifier)
    stage.SetEditTarget(layer)
    nodes = [n for n in scene.nodes if n.id != "knob"]
    nodes[1] = nodes[1].model_copy(update={"material_id": None, "params": {"size": (2.0, 2.0, 2.0)}})
    edited = scene.model_copy(update={"nodes": nodes})
    author_scene(stage, edited)

    knob = stage.GetPrimAtPath("/Prop/knob")
    assert knob and not knob.IsActive()
    assert layer.GetPrimAtPath("/Prop/knob").active is False
    assert layer.GetPrimAtPath("/Prop/scan") is None  # unchanged nodes are not re-authored
    body = stage.GetPrimAtPath("/Prop/grp/body")
    assert not UsdShade.MaterialBindingAPI(body).ComputeBoundMaterial()[0]
    assert read_ir(stage.GetDefaultPrim()) == edited

    stage.GetSessionLayer().subLayerPaths.remove(layer.identifier)
    assert stage.GetPrimAtPath("/Prop/knob").IsActive()
    assert read_ir(stage.GetDefaultPrim()) == scene
