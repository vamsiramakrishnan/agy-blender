"""Import triangle assets into the compiler's right-handed, Z-up metre space."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import numpy as np


def triangle_parts(path: Path, data: bytes | None = None) -> Iterator[tuple[str, np.ndarray, np.ndarray, np.ndarray | None]]:
    """Yield each transformed instance, preserving winding and per-vertex UVs.

    ``data``: the file's bytes, already read (``path`` then only names the format): the loader gets a buffer and no
    resolver, so it cannot open another file, and what is read is exactly what the caller checked and will keep."""
    import io

    import trimesh

    ext = path.suffix.lower()
    if ext not in {".glb", ".gltf", ".obj", ".ply", ".stl"}:
        raise ValueError(f"{path}: unsupported triangle asset format")
    if data is None:
        if not path.is_file():
            raise FileNotFoundError(path)
        scene = trimesh.load_scene(path, process=False, allow_remote=False)
    else:
        scene = trimesh.load_scene(io.BytesIO(data), file_type=ext.lstrip("."), process=False, allow_remote=False)
    found = False
    for node in sorted(scene.graph.nodes_geometry):
        transform, geom_name = scene.graph[node]
        mesh = scene.geometry[geom_name]
        if not isinstance(mesh, trimesh.Trimesh) or not len(mesh.faces):
            continue
        points = trimesh.transform_points(mesh.vertices, transform)
        if ext in {".glb", ".gltf"}:
            points = np.column_stack((points[:, 0], -points[:, 2], points[:, 1]))
        faces = np.asarray(mesh.faces, np.int32)
        if np.linalg.det(transform[:3, :3]) < 0:
            faces = faces[:, ::-1]
        uv = getattr(mesh.visual, "uv", None)
        found = True
        yield str(node), points, faces, uv
    if not found:
        raise ValueError(f"{path}: no triangle meshes found")
