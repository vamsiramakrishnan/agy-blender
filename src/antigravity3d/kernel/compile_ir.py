"""Compiles a :class:`SceneIR` into USD opinions on the current edit target.

Authoring is a *delta* against the scene IR already composed on the root prim
(``customData["agy:sceneIR"]``): only new or changed nodes/materials are
written, and composed prims that the new IR no longer contains are
deactivated in the edit target. Rolling back is therefore just dropping the
layer that holds those opinions.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from pxr import Gf, Kind, Sdf, Usd, UsdGeom, UsdShade, Vt

from antigravity3d.ir import MaterialIR, NodeIR, SceneIR

IR_KEY = "agy:sceneIR"  # customData key path (USD stores it as dict agy -> sceneIR)
LOOKS = "Looks"
REF_CHILD = "ref"  # child prim of a mesh_ref node that carries the USD reference
PRIMITIVES = frozenset({"box", "cylinder", "cone", "sphere", "capsule"})
USD_EXTS = frozenset({".usd", ".usda", ".usdc", ".usdz"})
_MESH_ATTRS = ("points", "faceVertexCounts", "faceVertexIndices", "extent")

# --- primitive meshes ---------------------------------------------------------


def _num(node: NodeIR, name: str, default: float | None = None, *, allow_zero: bool = False) -> float:
    v = node.params.get(name, default)
    if v is None:
        raise ValueError(f"{node.id}: {node.kind} requires param '{name}'")
    if isinstance(v, (str, tuple, list)) or isinstance(v, bool):
        raise ValueError(f"{node.id}: param '{name}' must be a number, got {v!r}")
    v = float(v)
    if not math.isfinite(v) or v < 0 or (v == 0 and not allow_zero):
        raise ValueError(f"{node.id}: param '{name}' must be {'>= 0' if allow_zero else '> 0'}, got {v}")
    return v


def _int(node: NodeIR, name: str, default: int, minimum: int) -> int:
    v = node.params.get(name, default)
    if isinstance(v, (str, tuple, list, bool)) or float(v) != int(v) or int(v) < minimum:
        raise ValueError(f"{node.id}: param '{name}' must be an integer >= {minimum}, got {v!r}")
    return int(v)


def _lathe(profile: list[tuple[float, float]], n: int) -> tuple[np.ndarray, np.ndarray]:
    """Surface of revolution about Z from a top-to-bottom (r, z) profile.

    Rows with r == 0 collapse to a single pole vertex, so the result is a
    closed manifold with shared vertices (no seam duplicates).
    """
    ang = np.arange(n) * (2.0 * np.pi / n)
    cos, sin = np.cos(ang), np.sin(ang)
    pts: list[np.ndarray] = []
    rows: list[np.ndarray] = []  # vertex indices per row; length 1 for a pole
    count = 0
    for r, z in profile:
        if r == 0.0:
            pts.append(np.array([[0.0, 0.0, z]]))
            rows.append(np.array([count]))
            count += 1
        else:
            pts.append(np.column_stack([r * cos, r * sin, np.full(n, z)]))
            rows.append(np.arange(count, count + n))
            count += n
    tris: list[np.ndarray] = []
    for a, b in zip(rows[:-1], rows[1:]):
        a1, b1 = np.roll(a, -1), np.roll(b, -1)
        if len(a) == 1:  # pole above ring: fan
            tris.append(np.column_stack([np.full(n, a[0]), b, b1]))
        elif len(b) == 1:  # ring above pole: fan
            tris.append(np.column_stack([a, np.full(n, b[0]), a1]))
        else:  # ring above ring: quads, CCW seen from outside
            tris.append(np.column_stack([a, b, b1]))
            tris.append(np.column_stack([a, b1, a1]))
    return np.vstack(pts).astype(np.float32), np.vstack(tris).astype(np.int32)


def _box(sx: float, sy: float, sz: float) -> tuple[np.ndarray, np.ndarray]:
    x, y, z = sx / 2, sy / 2, sz / 2
    pts = np.array(
        [[-x, -y, -z], [x, -y, -z], [x, y, -z], [-x, y, -z],
         [-x, -y, z], [x, -y, z], [x, y, z], [-x, y, z]],
        dtype=np.float32,
    )
    tris = np.array(
        [[0, 2, 1], [0, 3, 2],  # -Z
         [4, 5, 6], [4, 6, 7],  # +Z
         [0, 1, 5], [0, 5, 4],  # -Y
         [2, 3, 7], [2, 7, 6],  # +Y
         [1, 2, 6], [1, 6, 5],  # +X
         [3, 0, 4], [3, 4, 7]],  # -X
        dtype=np.int32,
    )
    return pts, tris


def primitive_mesh(node: NodeIR) -> tuple[np.ndarray, np.ndarray]:
    """(points float32 Nx3, triangles int32 Mx3) for a primitive node, local space."""
    k = node.kind
    if k == "box":
        size = node.params.get("size")
        if not isinstance(size, (tuple, list)) or len(size) != 3:
            raise ValueError(f"{node.id}: box requires param 'size' as (x, y, z), got {size!r}")
        if not all(isinstance(s, (int, float)) and math.isfinite(s) and s > 0 for s in size):
            raise ValueError(f"{node.id}: box size components must be > 0, got {tuple(size)}")
        return _box(*map(float, size))
    if k not in PRIMITIVES:
        raise ValueError(f"{node.id}: kind '{k}' is not a primitive")
    r = _num(node, "radius")
    if k == "sphere":
        n, rings = _int(node, "segments", 24, 3), _int(node, "rings", 12, 2)
        phis = np.linspace(0.0, np.pi, rings + 1)
        return _lathe([(0.0 if i in (0, rings) else r * math.sin(p), r * math.cos(p)) for i, p in enumerate(phis)], n)
    if k == "capsule":
        h = _num(node, "height", allow_zero=True)
        n = _int(node, "segments", 16, 3)
        cap = max(2, n // 4)  # latitude bands per hemisphere
        top = [(r * math.sin(p), h / 2 + r * math.cos(p)) for p in np.linspace(0.0, np.pi / 2, cap + 1)]
        bot = [(r * math.sin(p), -h / 2 + r * math.cos(p)) for p in np.linspace(np.pi / 2, np.pi, cap + 1)]
        top[0], bot[-1] = (0.0, top[0][1]), (0.0, bot[-1][1])
        return _lathe(top + (bot[1:] if h == 0 else bot), n)
    h = _num(node, "height")
    n = _int(node, "segments", 24, 3)
    if k == "cylinder":
        return _lathe([(0.0, h / 2), (r, h / 2), (r, -h / 2), (0.0, -h / 2)], n)
    return _lathe([(0.0, h / 2), (r, -h / 2), (0.0, -h / 2)], n)  # cone: apex up


# --- scene authoring ----------------------------------------------------------


def read_ir(prim: Usd.Prim) -> SceneIR | None:
    """The scene IR stored in a prim's composed customData, if any."""
    if not prim or not prim.IsValid():
        return None
    raw = prim.GetCustomDataByKey(IR_KEY)
    return SceneIR.model_validate_json(raw) if raw else None


def node_paths(scene: SceneIR, root: Sdf.Path) -> dict[str, Sdf.Path]:
    by_id = {n.id: n for n in scene.nodes}
    out: dict[str, Sdf.Path] = {}

    def path(nid: str, depth: int = 0) -> Sdf.Path:
        if nid not in out:
            if depth > len(by_id):
                raise ValueError(f"{nid}: parent cycle")
            parent = by_id[nid].parent
            out[nid] = (root if parent is None else path(parent, depth + 1)).AppendChild(nid)
        return out[nid]

    for n in scene.nodes:
        path(n.id)
    return out


def _shader_names(m: MaterialIR) -> list[str]:
    names = ["Surface"]
    if m.albedo_uri or m.orm_uri or m.normal_uri:
        names.append("stReader")
    names += [t for t, uri in (("albedoTex", m.albedo_uri), ("ormTex", m.orm_uri), ("normalTex", m.normal_uri)) if uri]
    return names


def _set_input(shader: UsdShade.Shader, name: str, vtype: Sdf.ValueTypeName, value) -> UsdShade.Input:
    inp = shader.CreateInput(name, vtype)
    inp.Set(value)
    if inp.GetAttr().HasAuthoredConnections():  # a weaker layer wired a texture here
        inp.GetAttr().SetConnections([])  # explicit empty list overrides it
    return inp


def _author_material(stage: Usd.Stage, path: Sdf.Path, m: MaterialIR) -> None:
    mat = UsdShade.Material.Define(stage, path)
    if not mat.GetPrim().IsActive():
        mat.GetPrim().SetActive(True)
    mat.GetPrim().SetCustomDataByKey("agy:normalConvention", m.normal_convention)
    surf = UsdShade.Shader.Define(stage, path.AppendChild("Surface"))
    surf.CreateIdAttr("UsdPreviewSurface")
    mat.CreateSurfaceOutput().ConnectToSource(surf.ConnectableAPI(), "surface")
    color = _set_input(surf, "diffuseColor", Sdf.ValueTypeNames.Color3f, Gf.Vec3f(*m.base_color))
    rough = _set_input(surf, "roughness", Sdf.ValueTypeNames.Float, m.roughness)
    metal = _set_input(surf, "metallic", Sdf.ValueTypeNames.Float, m.metallic)
    occl = surf.CreateInput("occlusion", Sdf.ValueTypeNames.Float)
    normal = surf.CreateInput("normal", Sdf.ValueTypeNames.Normal3f)
    for inp in (occl, normal):
        if inp.GetAttr().HasAuthoredConnections():
            inp.GetAttr().SetConnections([])
    if not (m.albedo_uri or m.orm_uri or m.normal_uri):
        return
    reader = UsdShade.Shader.Define(stage, path.AppendChild("stReader"))
    reader.CreateIdAttr("UsdPrimvarReader_float2")
    reader.CreateInput("varname", Sdf.ValueTypeNames.String).Set("st")
    st = reader.CreateOutput("result", Sdf.ValueTypeNames.Float2)

    def tex(name: str, uri: str, colorspace: str) -> UsdShade.Shader:
        t = UsdShade.Shader.Define(stage, path.AppendChild(name))
        t.CreateIdAttr("UsdUVTexture")
        t.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(Sdf.AssetPath(uri))
        t.CreateInput("st", Sdf.ValueTypeNames.Float2).ConnectToSource(st)
        t.CreateInput("sourceColorSpace", Sdf.ValueTypeNames.Token).Set(colorspace)
        return t

    if m.albedo_uri:
        color.ConnectToSource(tex("albedoTex", m.albedo_uri, "sRGB").CreateOutput("rgb", Sdf.ValueTypeNames.Float3))
    if m.orm_uri:
        orm = tex("ormTex", m.orm_uri, "raw")
        occl.ConnectToSource(orm.CreateOutput("r", Sdf.ValueTypeNames.Float))
        rough.ConnectToSource(orm.CreateOutput("g", Sdf.ValueTypeNames.Float))
        metal.ConnectToSource(orm.CreateOutput("b", Sdf.ValueTypeNames.Float))
    if m.normal_uri:
        nt = tex("normalTex", m.normal_uri, "raw")
        nt.CreateInput("scale", Sdf.ValueTypeNames.Float4).Set(Gf.Vec4f(2, 2, 2, 1))
        nt.CreateInput("bias", Sdf.ValueTypeNames.Float4).Set(Gf.Vec4f(-1, -1, -1, 0))
        normal.ConnectToSource(nt.CreateOutput("rgb", Sdf.ValueTypeNames.Float3))


def _block_authored(prim: Usd.Prim, names) -> None:
    for name in names:
        attr = prim.GetAttribute(name)
        if attr and attr.HasAuthoredValue():
            attr.Block()


def _author_node(stage: Usd.Stage, path: Sdf.Path, node: NodeIR, root: Sdf.Path) -> None:
    is_prim = node.kind in PRIMITIVES
    prim = stage.DefinePrim(path, "Mesh" if is_prim else "Xform")
    if not prim.IsActive():
        prim.SetActive(True)
    prim.CreateAttribute("agy:nodeId", Sdf.ValueTypeNames.String, custom=True).Set(node.id)
    prim.CreateAttribute("agy:collider", Sdf.ValueTypeNames.Bool, custom=True).Set(node.collider)

    t = node.transform
    prim.CreateAttribute("xformOp:translate", Sdf.ValueTypeNames.Double3).Set(Gf.Vec3d(*t.translate))
    prim.CreateAttribute("xformOp:rotateXYZ", Sdf.ValueTypeNames.Float3).Set(Gf.Vec3f(*t.rotate_euler_deg))
    prim.CreateAttribute("xformOp:scale", Sdf.ValueTypeNames.Float3).Set(Gf.Vec3f(*t.scale))
    UsdGeom.Xformable(prim).CreateXformOpOrderAttr().Set(
        Vt.TokenArray(["xformOp:translate", "xformOp:rotateXYZ", "xformOp:scale"])
    )

    if is_prim:
        pts, tris = primitive_mesh(node)
        mesh = UsdGeom.Mesh(prim)
        mesh.CreatePointsAttr().Set(Vt.Vec3fArray.FromNumpy(pts))
        mesh.CreateFaceVertexCountsAttr().Set(Vt.IntArray.FromNumpy(np.full(len(tris), 3, np.int32)))
        mesh.CreateFaceVertexIndicesAttr().Set(Vt.IntArray.FromNumpy(tris.ravel()))
        mesh.CreateExtentAttr().Set(Vt.Vec3fArray([Gf.Vec3f(*pts.min(0)), Gf.Vec3f(*pts.max(0))]))
        mesh.CreateSubdivisionSchemeAttr().Set(UsdGeom.Tokens.none)
    else:  # kind changed away from a primitive: hide stale mesh data from weaker layers
        _block_authored(prim, _MESH_ATTRS)

    if node.kind == "mesh_ref":
        ref_path = node.params.get("path")
        if not isinstance(ref_path, str) or not ref_path:
            raise ValueError(f"{node.id}: mesh_ref requires string param 'path'")
        p = Path(ref_path)
        if p.suffix.lower() in USD_EXTS:
            # Anonymous tx layers have no directory to anchor relative paths against.
            ref = stage.DefinePrim(path.AppendChild(REF_CHILD))
            if not ref.IsActive():
                ref.SetActive(True)
            ref.GetReferences().SetReferences([Sdf.Reference(str(p if p.is_absolute() else p.resolve()))])
            _block_authored(prim, ["agy:meshRef"])
        else:
            prim.CreateAttribute("agy:meshRef", Sdf.ValueTypeNames.String, custom=True).Set(ref_path)
    else:
        _block_authored(prim, ["agy:meshRef"])

    if node.material_id is not None:
        api = UsdShade.MaterialBindingAPI.Apply(prim)
        api.Bind(UsdShade.Material(stage.GetPrimAtPath(root.AppendPath(f"{LOOKS}/{node.material_id}"))))
    else:
        rel = prim.GetRelationship("material:binding")
        if rel and rel.HasAuthoredTargets():
            rel.SetTargets([])  # explicit empty list overrides a weaker binding


def _ensure_root(stage: Usd.Stage, root: Sdf.Path) -> Usd.Prim:
    prim = stage.GetPrimAtPath(root)
    if prim and prim.IsValid():
        return prim
    prim = UsdGeom.Xform.Define(stage, root).GetPrim()
    Usd.ModelAPI(prim).SetKind(Kind.Tokens.component)
    if root.IsRootPrimPath():
        stage.SetDefaultPrim(prim)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    return prim


def author_scene(
    stage: Usd.Stage,
    scene: SceneIR,
    *,
    root: Sdf.Path | str | None = None,
    prev: SceneIR | None = None,
) -> None:
    """Authors ``scene`` under ``root`` (default ``/<asset_name>``) in the edit target.

    ``prev`` defaults to the IR composed on ``root``; only nodes/materials that
    differ from it are written. ``root`` lets the StageManager author into a
    referenced base prim (see usd_stage.py) instead of the default prim.
    """
    root = Sdf.Path(root) if root is not None else Sdf.Path.absoluteRootPath.AppendChild(scene.asset_name)
    root_prim = _ensure_root(stage, root)
    if prev is None:
        prev = read_ir(root_prim)
    prev_nodes = {n.id: n for n in prev.nodes} if prev else {}
    prev_mats = {m.id: m for m in prev.materials} if prev else {}
    prev_paths = node_paths(prev, root) if prev else {}
    paths = node_paths(scene, root)
    expected: set[Sdf.Path] = {root, *paths.values()}
    opaque: set[Sdf.Path] = set()  # subtrees we don't own (referenced mesh files)

    def stale(path: Sdf.Path) -> bool:
        p = stage.GetPrimAtPath(path)
        return not (p and p.IsActive())

    looks = root.AppendChild(LOOKS)
    if scene.materials:
        expected.add(looks)
        if stale(looks):
            scope = UsdGeom.Scope.Define(stage, looks).GetPrim()
            if not scope.IsActive():
                scope.SetActive(True)
    for m in scene.materials:
        mpath = looks.AppendChild(m.id)
        expected.add(mpath)
        expected.update(mpath.AppendChild(s) for s in _shader_names(m))
        if prev_mats.get(m.id) != m or stale(mpath):
            _author_material(stage, mpath, m)

    for node in sorted(scene.nodes, key=lambda n: paths[n.id].pathElementCount):  # parents first
        path = paths[node.id]
        if node.kind == "mesh_ref" and Path(str(node.params.get("path", ""))).suffix.lower() in USD_EXTS:
            expected.add(path.AppendChild(REF_CHILD))
            opaque.add(path.AppendChild(REF_CHILD))
        if prev_nodes.get(node.id) != node or prev_paths.get(node.id) != path or stale(path):
            _author_node(stage, path, node, root)

    # Deactivate whatever weaker layers still compose but the new IR dropped.
    # Collect first: changing `active` recomposes and would invalidate the range.
    drop: list[Usd.Prim] = []
    it = iter(Usd.PrimRange(root_prim, Usd.PrimIsActive))
    for p in it:
        path = p.GetPath()
        if path not in expected:
            drop.append(p)
            it.PruneChildren()
        elif path in opaque:
            it.PruneChildren()
    for p in drop:
        p.SetActive(False)

    root_prim.SetCustomDataByKey(IR_KEY, scene.model_dump_json())
