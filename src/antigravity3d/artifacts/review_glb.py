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
import weakref
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

import numpy as np

from antigravity3d.kernel.gltf_writer import (
    GltfMaterial,
    GltfMesh,
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


def session_materials(stage: Any, meshes: Sequence[Any]) -> dict[str, GltfMaterial]:
    """``{node_id: GltfMaterial}`` for the meshes of a composed asset-session stage.

    This is the package's own derivation (:func:`antigravity3d.kernel.package.gltf_materials`) applied to a live
    stage: textures are read from where the ``UsdUVTexture`` shaders resolve, and a DirectX normal map is
    converted to glTF's OpenGL convention. The inspector therefore shows a session with exactly the materials
    an export of it would ship. Imported lazily because ``kernel.package`` imports this package.
    """
    from antigravity3d.kernel.package import gltf_materials

    return gltf_materials(stage, meshes)
