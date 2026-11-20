"""The "review GLB": what the 3D inspector shows, as one glTF binary built by the shipping GLB writer.

The inspector used to rebuild geometry and materials in the page (welded vertices, normals averaged with
``computeVertexNormals``, hand-made materials), so a reviewer could approve shading and materials that do
not ship. It now loads this GLB with three.js' GLTFLoader instead:

- geometry, crease normals (vertex-split at hard edges), UVs and materials go through the one conversion the
  package GLBs use (``kernel.fast_ops.gltf_mesh``, which ``fast_ops.write_gltf`` calls per item) and the same
  writer (``kernel.gltf_writer.build_glb``). The one deliberate difference in how it is called: render meshes
  that have no UVs yet (a session before ``generate_lods_and_colliders``) are shaded too;
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
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from antigravity3d.kernel.fast_ops import gltf_mesh
from antigravity3d.kernel.gltf_writer import GltfMaterial, GltfMesh, build_glb


@dataclass(frozen=True)
class ReviewPart:
    """One mesh to show: world-space Z-up points, triangles, optional per-vertex UVs (bottom-left origin)."""

    node_id: str | None  # None for a merged LOD level that no longer has per-node parts
    points: np.ndarray
    triangles: np.ndarray
    uvs: np.ndarray | None = None
    material: GltfMaterial | None = None


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

    A part with no triangles at a level beyond LOD0 is absent there (the LOD generator drops parts that cannot be
    seen at that distance): it gets no glTF node and no entry in that level's ``parts``, exactly as the package's
    ``*_LODn.glb`` leaves it out.
    """
    meshes: list[GltfMesh] = []
    out_levels = []
    for label, parts in [("LOD0", lod0), *levels]:
        entries = []
        for part in parts:
            if label != "LOD0" and len(part.triangles) == 0:
                continue
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
