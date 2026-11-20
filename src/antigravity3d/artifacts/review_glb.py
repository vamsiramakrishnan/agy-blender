"""The "review GLB": what the 3D inspector shows, as one glTF binary built by the shipping GLB writer.

The inspector used to rebuild geometry and materials in the page (welded vertices, normals averaged with
``computeVertexNormals``, hand-made materials), so a reviewer could approve shading and materials that do
not ship. It now loads this GLB with three.js' GLTFLoader instead:

- geometry, crease normals (vertex-split at hard edges), UVs and materials go through the same conversion
  ``kernel.fast_ops.write_gltf`` applies to the package GLBs (:func:`gltf_mesh`, kept byte-identical to it by
  ``tests/test_inspector.py::test_review_mesh_conversion_matches_the_package_writer``) and the same writer
  (``kernel.gltf_writer.build_glb``);
- one GLB per revision holds LOD0 (one node per scene node), every further LOD level (one node per scene node
  when per-node chains are given, as in the package's ``*_LODn.glb``), and the collider hulls, as separate root
  nodes. The writer de-duplicates images by content, so a texture used by every LOD level is embedded once;
- which glTF node is which scene node / LOD level / hull, plus budget, QA and the node table, stays JSON
  (:func:`build_review` returns both). Nodes are addressed by glTF node index, not by name, because
  GLTFLoader rewrites names (``PropertyBinding.sanitizeNodeName``).

Materials come in as ``kernel.gltf_writer.GltfMaterial`` objects keyed by node id. In a package they are the
ones the package GLBs use; in an asset session :func:`session_materials` reads them from the composed stage.
"""

from __future__ import annotations

import hashlib
import io
import weakref
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import numpy as np

from antigravity3d.kernel.gltf_writer import (
    EncodedImage,
    GltfMaterial,
    GltfMesh,
    TextureRef,
    build_glb,
    flip_v,
    split_by_corner_normals,
    z_up_to_y_up,
)

# fast_ops.write_gltf's default for a UV-mapped item that has no material
DEFAULT_MATERIAL = GltfMaterial(name="Default", base_color_factor=(0.5, 0.5, 0.5, 1.0), metallic_factor=0.0,
                                roughness_factor=0.5)


@dataclass(frozen=True)
class ReviewPart:
    """One mesh to show: world-space Z-up points, triangles, optional per-vertex UVs (bottom-left origin)."""

    node_id: str | None  # None for a merged LOD level that no longer has per-node parts
    points: np.ndarray
    triangles: np.ndarray
    uvs: np.ndarray | None = None
    material: GltfMaterial | None = None


def gltf_mesh(name: str, points: np.ndarray, triangles: np.ndarray, uvs: np.ndarray | None,
              material: GltfMaterial | None, *, shade: bool) -> GltfMesh:
    """One kernel mesh -> ``GltfMesh``, exactly as ``fast_ops.write_gltf`` converts a package item.

    ``shade``: write the crease normals the package USD authors (``compile_ir.crease_normals``), split per
    vertex at creases. ``write_gltf`` does this for UV-mapped items; the inspector also does it for render
    meshes that have no UVs yet (a session before ``generate_lods_and_colliders``), never for collider hulls.
    ``fast_ops.write_gltf`` could call this function (see the report for the suggested refactor).
    """
    from antigravity3d.kernel.compile_ir import crease_normals

    p = np.asarray(points, dtype=np.float64)
    tris = np.asarray(triangles)
    normals = None
    if shade:
        p, tris, uvs, normals = split_by_corner_normals(p, tris, uvs, crease_normals(p, tris))
        normals = z_up_to_y_up(normals)
    p = z_up_to_y_up(p)
    if uvs is not None:
        material = material if material is not None else DEFAULT_MATERIAL
        uvs = flip_v(uvs)
    elif material is not None:
        material = _untextured(material)
    return GltfMesh(name, p, tris, uvs, material, normals)


_untextured_cache: weakref.WeakKeyDictionary[GltfMaterial, GltfMaterial] = weakref.WeakKeyDictionary()


def _untextured(m: GltfMaterial) -> GltfMaterial:
    """Factor-only copy of a material for a mesh without UVs (a texture without TEXCOORD_0 is invalid glTF).
    One copy per material object, so meshes sharing a material still share one glTF material."""
    if m not in _untextured_cache:
        has_albedo = m.base_color_texture is not None
        _untextured_cache[m] = replace(
            m, base_color_texture=None, metallic_roughness_texture=None, normal_texture=None, occlusion_texture=None,
            emissive_texture=None, extensions={},
            # an albedo-textured material carries a white factor: show the untextured mesh in neutral grey
            base_color_factor=(0.72, 0.74, 0.76, 1.0) if has_albedo else tuple(m.base_color_factor))
    return _untextured_cache[m]


def _maps(m: GltfMaterial | None, uv: bool) -> list[str]:
    if m is None or not uv:
        return []
    out = []
    if m.base_color_texture is not None:
        out.append("albedo")
    if m.metallic_roughness_texture is not None or m.occlusion_texture is not None:
        out.append("orm")
    if m.normal_texture is not None:
        out.append("normal")
    return out


def build_review(
    lod0: Sequence[ReviewPart],
    *,
    levels: Sequence[tuple[str, Sequence[ReviewPart]]] = (),
    hulls: Sequence[tuple[str, np.ndarray, np.ndarray]] = (),
) -> tuple[dict[str, Any], bytes]:
    """Returns ``(layout, glb)``: the GLB (LOD0, further levels, hulls as root nodes) and where each part is.

    ``layout = {"levels": [{"label", "tris", "parts": [{"node", "glb", "tris", "uv", "maps"}]}],
    "colliders": [{"name", "glb"}], "glb": {"bytes", "sha256"}}``; ``glb`` values are glTF node indices.
    """
    meshes: list[GltfMesh] = []
    out_levels = []
    for label, parts in [("LOD0", lod0), *levels]:
        entries = []
        for part in parts:
            name = f"{label}:{part.node_id or 'merged'}"
            entries.append({"node": part.node_id, "glb": len(meshes), "tris": int(len(part.triangles)),
                            "uv": part.uvs is not None, "maps": _maps(part.material, part.uvs is not None)})
            meshes.append(gltf_mesh(name, part.points, part.triangles, part.uvs, part.material, shade=True))
        out_levels.append({"label": label, "tris": sum(e["tris"] for e in entries), "parts": entries})
    colliders = []
    for name, points, triangles in hulls:
        colliders.append({"name": name, "glb": len(meshes)})
        meshes.append(gltf_mesh(f"hull:{name}", points, triangles, None, None, shade=False))
    glb = build_glb(meshes) if meshes else b""
    return ({"levels": out_levels, "colliders": colliders,
             "glb": {"bytes": len(glb), "sha256": hashlib.sha256(glb).hexdigest()}}, glb)


# --------------------------------------------------------------------------- materials of a session


def _asset_file(value: Any) -> Path | None:
    """Local file behind a USD asset value (resolved path, plain path or file:// URI)."""
    if value is None:
        return None
    raw = getattr(value, "resolvedPath", "") or getattr(value, "path", "") or str(value)
    u = urlparse(raw)
    p = Path(unquote(u.path)) if u.scheme == "file" else Path(raw)
    return p if raw and p.is_file() else None


def _flip_green(image: EncodedImage) -> EncodedImage:
    """DirectX (Y-) -> OpenGL (Y+) normal map, which glTF requires. Decodes and PNG-encodes once."""
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(image.data)) as im:
        rgb = im.convert("RGB")
        r, g, b = rgb.split()
        buf = io.BytesIO()
        Image.merge("RGB", (r, ImageOps.invert(g), b)).save(buf, "PNG")
    return EncodedImage(buf.getvalue(), "image/png", hashlib.sha256(buf.getvalue()).hexdigest(), image.name, True)


def session_materials(stage: Any, meshes: Sequence[Any]) -> dict[str, GltfMaterial]:
    """``{node_id: GltfMaterial}`` for the meshes of a composed asset-session stage.

    The one place the inspector derives materials in a session (the package passes its own GLB materials).
    Same mapping as the package GLB (``kernel/package.py``, ``_glb_materials``): the bound material's
    ``UsdPreviewSurface``; ``diffuseColor`` -> base colour (white when an albedo map is bound), the albedo,
    packed ORM (occlusion + metallic-roughness, one texture) and normal maps; metallic/roughness factors 1.0
    when the ORM map drives them. Images are the files the ``UsdUVTexture`` shaders reference, embedded as
    they are; a DirectX normal map (``agy:normalConvention``) is converted to glTF's OpenGL convention.
    Point this at the single material reader once it exists (see the module docstring).
    """
    from pxr import Sdf, UsdShade

    files: dict[Path, EncodedImage] = {}
    by_path: dict[Any, GltfMaterial] = {}
    out: dict[str, GltfMaterial] = {}

    def image(path: Path, flip: bool) -> EncodedImage:
        key = (path, flip)
        if key not in files:
            img = EncodedImage.from_file(path)
            files[key] = _flip_green(img) if flip else img
        return files[key]

    def texture_file(inp: Any) -> Path | None:
        if not inp or not inp.HasConnectedSource():
            return None
        src = inp.GetConnectedSource()
        shader = UsdShade.Shader(src[0].GetPrim()) if src else None
        if not shader or shader.GetIdAttr().Get() != "UsdUVTexture":
            return None
        f = shader.GetInput("file")
        return _asset_file(f.Get()) if f else None

    for mesh in meshes:
        prim = stage.GetPrimAtPath(Sdf.Path(mesh.prim_path))
        if not prim:
            continue
        mat, _ = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()
        if not mat:
            continue
        path = mat.GetPath()
        if path not in by_path:
            shader, _, _ = mat.ComputeSurfaceSource()
            if not shader:
                continue
            albedo = texture_file(shader.GetInput("diffuseColor"))
            orm = texture_file(shader.GetInput("roughness")) or texture_file(shader.GetInput("metallic"))
            normal = texture_file(shader.GetInput("normal"))
            directx = mat.GetPrim().GetCustomDataByKey("agy:normalConvention") == "directx"
            tex = {k: TextureRef(image(p, k == "normal" and directx)) for k, p in
                   (("albedo", albedo), ("orm", orm), ("normal", normal)) if p is not None}

            def value(name: str, default: Any) -> Any:
                inp = shader.GetInput(name)
                v = inp.Get() if inp else None
                return default if v is None else v

            color = [1.0] * 3 if "albedo" in tex else [float(c) for c in value("diffuseColor", (0.8, 0.8, 0.8))]
            by_path[path] = GltfMaterial(
                name=path.name, base_color_factor=(*color, 1.0), base_color_texture=tex.get("albedo"),
                normal_texture=tex.get("normal"), occlusion_texture=tex.get("orm"),
                metallic_roughness_texture=tex.get("orm"),
                metallic_factor=1.0 if "orm" in tex else float(value("metallic", 0.0)),
                roughness_factor=1.0 if "orm" in tex else float(value("roughness", 0.5)),
            )
        out[mesh.node_id] = by_path[path]
    return out


def materials_from_png_maps(textures: Mapping[str, Any], material_ids: Mapping[str, str | None]) -> dict[str, GltfMaterial]:
    """Transitional: ``{material_id: InspectorTextures}`` (PNG bytes per map) -> ``{node_id: GltfMaterial}``.

    Only for callers of :func:`antigravity3d.artifacts.inspector.build_inspector` that still pass
    ``textures=`` (``kernel/package.py`` until it passes its GLB materials as ``materials=``). Same mapping as
    the package GLB: white base colour under an albedo map, one ORM texture for occlusion and
    metallic-roughness, factors 1.0.
    """
    made: dict[str, GltfMaterial] = {}
    for mid, t in textures.items():
        slots = {k: TextureRef(EncodedImage.from_bytes(v, f"{mid}_{k}")) for k, v in
                 (("albedo", t.albedo_png), ("orm", t.orm_png), ("normal", t.normal_png)) if v}
        if slots:
            made[mid] = GltfMaterial(
                name=mid, base_color_factor=(1.0, 1.0, 1.0, 1.0) if "albedo" in slots else (0.8, 0.8, 0.8, 1.0),
                base_color_texture=slots.get("albedo"), metallic_roughness_texture=slots.get("orm"),
                occlusion_texture=slots.get("orm"), normal_texture=slots.get("normal"),
                metallic_factor=1.0 if "orm" in slots else 0.0, roughness_factor=1.0 if "orm" in slots else 0.5)
    return {node: made[mid] for node, mid in material_ids.items() if mid in made}
