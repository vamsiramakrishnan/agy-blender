"""Compiles a :class:`SceneIR` into USD opinions on the current edit target.

Authoring is a *delta* against the scene IR already composed on the root prim
(``customData["agy:sceneIR"]``): only new or changed nodes/materials are
written, and composed prims that the new IR no longer contains are
deactivated in the edit target. Rolling back is therefore just dropping the
layer that holds those opinions.
"""

from __future__ import annotations

import math
from itertools import pairwise
from pathlib import Path

import numpy as np
from pxr import Gf, Kind, Sdf, Usd, UsdGeom, UsdShade, Vt

from antigravity3d.ir import MaterialIR, NodeIR, PlaceRelative, SceneIR, Transform
from antigravity3d.kernel.mesh_io import triangle_parts

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
    if isinstance(v, (str, tuple, list, bool)):
        raise ValueError(f"{node.id}: param '{name}' must be a number, got {v!r}")  # noqa: TRY004
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
    for a, b in pairwise(rows):
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
    # fmt: off
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
    # fmt: on
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


# --- mesh data (UVs / normals) --------------------------------------------------

UV_WELD_KEY = "agy:uvWeldMap"  # int[] per authored vertex: id of the pre-unwrap vertex it was split from
CREASE_DEG = 40.0


def crease_normals(points: np.ndarray, tris: np.ndarray, crease_deg: float = CREASE_DEG) -> np.ndarray:
    """Per-corner (faceVarying, 3 per triangle) normals, smoothed across faces within ``crease_deg``.

    Smoothing is done over position-welded vertices, so seam-split vertices (UV seams) shade
    continuously while real hard edges (box corners) stay crisp.
    """
    from antigravity3d.kernel.fast_ops import _canon

    p = np.asarray(points, dtype=np.float64)
    t = np.asarray(tris, dtype=np.int64)
    fn = np.cross(p[t[:, 1]] - p[t[:, 0]], p[t[:, 2]] - p[t[:, 0]])  # area-weighted
    ln = np.linalg.norm(fn, axis=1, keepdims=True)
    unit = np.divide(fn, ln, out=np.zeros_like(fn), where=ln > 1e-20)
    wid = _canon(p)[t].ravel()
    order = np.argsort(wid, kind="stable")
    cuts = np.flatnonzero(np.diff(wid[order])) + 1
    cos_t = math.cos(math.radians(crease_deg))
    out = np.zeros((len(t) * 3, 3))
    for grp in np.split(order, cuts):
        faces = grp // 3
        same = (unit[faces] @ unit[faces].T) >= cos_t - 1e-9
        out[grp] = same.astype(np.float64) @ fn[faces]
    n = np.linalg.norm(out, axis=1, keepdims=True)
    fallback = np.repeat(unit, 3, axis=0)
    out = np.where(n > 1e-20, out / np.maximum(n, 1e-20), fallback)
    return out.astype(np.float32)


def author_mesh_data(
    prim: Usd.Prim,
    points: np.ndarray,
    tris: np.ndarray,
    uvs: np.ndarray | None,
    *,
    weld_map: np.ndarray | None = None,
    normals: bool = True,
) -> None:
    """Writes triangle geometry, ``primvars:st`` and normals on a Mesh prim in the edit target.

    Conventions (one place, used by the working stage, the package and its LOD variants):
      * ``primvars:st``  texCoord2f[], *vertex* interpolation: one UV per authored point, i.e. the
        seam-split topology of the unwrap, identical to the GLB. ``uvs`` None blocks any weaker st.
      * ``normals``      normal3f[], *faceVarying* (3 per triangle), crease-smoothed (see
        :func:`crease_normals`); not relying on USD's default because that smooths hard edges of
        shared-vertex meshes when subdivisionScheme=none.
      * the mesh is triangulated and ``subdivisionScheme=none``.
    """
    mesh = UsdGeom.Mesh(prim)
    pts = np.ascontiguousarray(points, dtype=np.float32)
    tri = np.ascontiguousarray(tris, dtype=np.int32).reshape(-1, 3)
    mesh.CreatePointsAttr().Set(Vt.Vec3fArray.FromNumpy(pts))
    mesh.CreateFaceVertexCountsAttr().Set(Vt.IntArray.FromNumpy(np.full(len(tri), 3, np.int32)))
    mesh.CreateFaceVertexIndicesAttr().Set(Vt.IntArray.FromNumpy(tri.ravel()))
    mesh.CreateExtentAttr().Set(Vt.Vec3fArray.FromNumpy(np.stack([pts.min(0), pts.max(0)])))
    mesh.CreateSubdivisionSchemeAttr().Set(UsdGeom.Tokens.none)
    if uvs is not None:
        uv = np.ascontiguousarray(uvs, dtype=np.float32).reshape(-1, 2)
        if len(uv) != len(pts):
            raise ValueError(f"{prim.GetPath()}: {len(uv)} uvs for {len(pts)} points")
        pv = UsdGeom.PrimvarsAPI(prim).CreatePrimvar("st", Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.vertex)
        pv.Set(Vt.Vec2fArray.FromNumpy(uv))
        pv.SetInterpolation(UsdGeom.Tokens.vertex)
        _block_authored(prim, ["primvars:st:indices"])
    else:
        _block_authored(prim, ["primvars:st", "primvars:st:indices"])
    if weld_map is not None:
        prim.CreateAttribute(UV_WELD_KEY, Sdf.ValueTypeNames.IntArray, custom=True).Set(
            Vt.IntArray.FromNumpy(np.ascontiguousarray(weld_map, dtype=np.int32))
        )
    else:
        _block_authored(prim, [UV_WELD_KEY])
    _block_authored(prim, ["primvars:normals", "primvars:normals:indices"])
    if normals:
        mesh.CreateNormalsAttr().Set(Vt.Vec3fArray.FromNumpy(crease_normals(pts, tri)))
        mesh.SetNormalsInterpolation(UsdGeom.Tokens.faceVarying)
    else:
        _block_authored(prim, ["normals"])


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

    for node in scene.nodes:
        nid = node.id
        walked = []
        while nid is not None and nid not in out:
            walked.append(nid)
            if len(walked) > len(by_id):
                raise ValueError(f"{node.id}: parent cycle")
            nid = by_id[nid].parent
        path = root if nid is None else out[nid]
        for child in reversed(walked):
            path = path.AppendChild(child)
            out[child] = path
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


def set_normal_encoding(shader: UsdShade.Shader, convention: str) -> None:
    """Decode packed normals to the +Y tangent frame consumed by UsdPreviewSurface."""
    y = -2 if convention == "directx" else 2
    shader.CreateInput("scale", Sdf.ValueTypeNames.Float4).Set(Gf.Vec4f(2, y, 2, 1))
    shader.CreateInput("bias", Sdf.ValueTypeNames.Float4).Set(Gf.Vec4f(-1, -y / 2, -1, 0))


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
    for name in ("occlusion", "normal"):  # texture-only inputs: unwire stale connections
        inp = surf.GetInput(name)
        if inp and inp.GetAttr().HasAuthoredConnections():
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
        occl = surf.CreateInput("occlusion", Sdf.ValueTypeNames.Float)
        occl.ConnectToSource(orm.CreateOutput("r", Sdf.ValueTypeNames.Float))
        rough.ConnectToSource(orm.CreateOutput("g", Sdf.ValueTypeNames.Float))
        metal.ConnectToSource(orm.CreateOutput("b", Sdf.ValueTypeNames.Float))
    if m.normal_uri:
        nt = tex("normalTex", m.normal_uri, "raw")
        set_normal_encoding(nt, m.normal_convention)
        normal = surf.CreateInput("normal", Sdf.ValueTypeNames.Normal3f)
        normal.ConnectToSource(nt.CreateOutput("rgb", Sdf.ValueTypeNames.Float3))


def _block_authored(prim: Usd.Prim, names) -> None:
    for name in names:
        attr = prim.GetAttribute(name)
        if attr and attr.HasAuthoredValue():
            attr.Block()


def _author_transform(prim: Usd.Prim, t: Transform, prev: Transform | None = None) -> None:
    # Sparse overs avoid duplicating type, geometry and bindings on transform-only edits.
    for name, value_type, vec_type, value, previous in (
        ("translate", Sdf.ValueTypeNames.Double3, Gf.Vec3d, t.translate, prev and prev.translate),
        ("rotateXYZ", Sdf.ValueTypeNames.Float3, Gf.Vec3f, t.rotate_euler_deg, prev and prev.rotate_euler_deg),
        ("scale", Sdf.ValueTypeNames.Float3, Gf.Vec3f, t.scale, prev and prev.scale),
    ):
        if value != previous:
            prim.CreateAttribute(f"xformOp:{name}", value_type, custom=False).Set(vec_type(*value))
    if prev is None:
        UsdGeom.Xformable(prim).CreateXformOpOrderAttr().Set(
            Vt.TokenArray(["xformOp:translate", "xformOp:rotateXYZ", "xformOp:scale"])
        )


def _author_node(stage: Usd.Stage, path: Sdf.Path, node: NodeIR, root: Sdf.Path) -> None:
    is_prim = node.kind in PRIMITIVES
    ref_path = None
    if node.kind == "mesh_ref":
        uri = node.params.get("path")
        if not isinstance(uri, str) or not uri:
            raise ValueError(f"{node.id}: mesh_ref requires string param 'path'")
        root_file = stage.GetRootLayer().realPath
        ref_path = ((Path(root_file).parent if root_file else Path.cwd()) / uri).resolve()
    imported = ref_path is not None and ref_path.suffix.lower() not in USD_EXTS
    prim = stage.DefinePrim(path, "Mesh" if is_prim or imported else "Xform")
    if not prim.IsActive():
        prim.SetActive(True)
    prim.CreateAttribute("agy:nodeId", Sdf.ValueTypeNames.String, custom=True).Set(node.id)
    prim.CreateAttribute("agy:collider", Sdf.ValueTypeNames.Bool, custom=True).Set(node.collider)

    _author_transform(prim, node.transform)

    if is_prim:
        pts, tris = primitive_mesh(node)
        author_mesh_data(prim, pts, tris, None, normals=False)
    elif imported:
        # One IR node owns one material and one mesh, including transformed instances.
        points, faces, uvs = [], [], []
        offset = 0
        for _, p, f, uv in triangle_parts(ref_path):
            points.append(p)
            faces.append(f + offset)
            uvs.append(uv)
            offset += len(p)
        st = np.concatenate(uvs) if all(uv is not None for uv in uvs) else None
        author_mesh_data(prim, np.concatenate(points), np.concatenate(faces), st, normals=False)
    else:
        _block_authored(prim, _MESH_ATTRS)

    if imported:
        prim.CreateAttribute("agy:meshRef", Sdf.ValueTypeNames.String, custom=True).Set(node.params["path"])
    else:
        _block_authored(prim, ["agy:meshRef"])
    if ref_path is not None and not imported:
        if not ref_path.is_file():
            raise FileNotFoundError(ref_path)
        # Anonymous tx layers have no directory against which to resolve a reference.
        ref = stage.DefinePrim(path.AppendChild(REF_CHILD))
        if not ref.IsActive():
            ref.SetActive(True)
        ref.GetReferences().SetReferences([Sdf.Reference(str(ref_path))])

    _bind_material(stage, prim, root, node.material_id)


def _bind_material(stage: Usd.Stage, prim: Usd.Prim, root: Sdf.Path, material_id: str | None) -> None:
    if material_id is not None:
        api = UsdShade.MaterialBindingAPI.Apply(prim)
        api.Bind(UsdShade.Material(stage.GetPrimAtPath(root.AppendPath(f"{LOOKS}/{material_id}"))))
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
        needs_import = False
        if node.kind == "mesh_ref":
            if Path(str(node.params.get("path", ""))).suffix.lower() in USD_EXTS:
                expected.add(path.AppendChild(REF_CHILD))
                opaque.add(path.AppendChild(REF_CHILD))
            else:
                prim = stage.GetPrimAtPath(path)
                needs_import = not prim or not prim.IsA(UsdGeom.Mesh)  # older stages held only a URI

        old = prev_nodes.get(node.id)
        if old is not None and prev_paths[node.id] == path and not needs_import and not stale(path):
            if old == node:
                continue
            if old.kind == node.kind and old.params == node.params:
                prim = stage.GetPrimAtPath(path)
                if old.transform != node.transform:
                    _author_transform(prim, node.transform, old.transform)
                if old.collider != node.collider:
                    prim.GetAttribute("agy:collider").Set(node.collider)
                if old.material_id != node.material_id:
                    _bind_material(stage, prim, root, node.material_id)
                continue
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


def resolve_anchor(scene: SceneIR, op: PlaceRelative, *, stage: Usd.Stage | None = None) -> Transform:
    """Resolve a one-shot world-space contact against the current composed geometry.

    on_top_of centres X/Y and contacts the upper Z face. flush_back aligns the +Y
    faces (positive gap insets the moving face) and preserves world X/Z.
    """
    if stage is None:
        stage = Usd.Stage.CreateInMemory()
        author_scene(stage, scene)
    paths = node_paths(scene, stage.GetDefaultPrim().GetPath())
    if op.target_id not in paths:
        raise KeyError(f"{op.op}: unknown target {op.target_id}")
    moving, target = paths[op.node_id], paths[op.target_id]
    if moving.HasPrefix(target) or target.HasPrefix(moving):
        raise ValueError("anchor nodes must belong to disjoint subtrees")
    cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_, UsdGeom.Tokens.render])

    def bounds(path):
        box = cache.ComputeWorldBound(stage.GetPrimAtPath(path)).ComputeAlignedRange()
        if box.IsEmpty():
            raise ValueError(f"{path}: anchor requires a nonempty bound")
        return np.array(box.GetMin()), np.array(box.GetMax())

    lo, hi = bounds(moving)
    other_lo, other_hi = bounds(target)
    delta = np.zeros(3)
    if op.op == "on_top_of":
        delta[:2] = (other_lo[:2] + other_hi[:2] - lo[:2] - hi[:2]) * 0.5
        delta[2] = other_hi[2] + op.gap_m - lo[2]
    else:
        delta[1] = other_hi[1] - op.gap_m - hi[1]
    prim = stage.GetPrimAtPath(moving)
    parent_to_world = np.array(UsdGeom.XformCache().GetParentToWorldTransform(prim))
    try:
        local_delta = np.linalg.solve(parent_to_world[:3, :3].T, delta)
    except np.linalg.LinAlgError as exc:
        raise ValueError(f"{moving}: anchor has singular parent transform") from exc
    transform = next(n.transform for n in scene.nodes if n.id == op.node_id)
    return transform.model_copy(update={"translate": tuple(transform.translate + local_delta)})
