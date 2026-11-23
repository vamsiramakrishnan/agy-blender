"""Shape-preserving repairs of a triangle mesh: weld duplicate vertices, drop degenerate and duplicate faces, make the
winding consistent and outward. Moved here unchanged from ``dags/batch_sanitize.py`` (which still exports both names)
so that a build script's ``a.mesh`` can use them without importing the sanitize DAG (pxr, the tactics: about 1 s)."""

from __future__ import annotations

import numpy as np

from antigravity3d.kernel.types import MeshData


def orient_faces(points: np.ndarray, faces: np.ndarray) -> tuple[np.ndarray, int]:
    """Consistent winding per connected component (BFS over shared edges), then outward for closed bodies.

    Own implementation: trimesh's ``fix_winding`` needs ``networkx``, which is not a declared dependency.
    """
    from collections import deque

    f = faces.copy()
    n = len(f)
    he: dict[tuple[int, int], list[int]] = {}
    for i, (a, b, c) in enumerate(f.tolist()):
        for u, v in ((a, b), (b, c), (c, a)):
            he.setdefault((min(u, v), max(u, v)), []).append(i)
    nbrs: list[list[int]] = [[] for _ in range(n)]
    for fs in he.values():
        if len(fs) == 2:
            nbrs[fs[0]].append(fs[1])
            nbrs[fs[1]].append(fs[0])

    def directed(i: int) -> set[tuple[int, int]]:
        a, b, c = f[i].tolist()
        return {(a, b), (b, c), (c, a)}

    seen = np.zeros(n, bool)
    flipped = 0
    for s in range(n):
        if seen[s]:
            continue
        comp, q = [s], deque([s])
        seen[s] = True
        while q:
            i = q.popleft()
            di = directed(i)
            for j in nbrs[i]:
                if seen[j]:
                    continue
                if di & directed(j):  # shares a directed edge with i: j runs the same way, flip it
                    f[j] = f[j][::-1]
                    flipped += 1
                seen[j] = True
                comp.append(j)
                q.append(j)
        cf = f[comp]
        if all(len(he[(min(u, v), max(u, v))]) == 2 for tri in cf.tolist() for u, v in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0]))):
            p = points[cf]
            vol = float(np.einsum("ij,ij->i", p[:, 0], np.cross(p[:, 1], p[:, 2])).sum() / 6.0)
            if vol < 0:
                f[comp] = cf[:, ::-1]
                flipped += len(comp)
    return f, flipped


def repair_mesh(m: MeshData) -> tuple[MeshData, dict[str, int]]:
    """Shape-preserving repairs; returns the repaired mesh and counts of what was changed."""
    import trimesh

    tm = trimesh.Trimesh(np.asarray(m.points, np.float64), np.asarray(m.triangles), process=False)
    v0, wound = len(tm.vertices), bool(tm.is_winding_consistent)
    tm.merge_vertices()
    f1 = len(tm.faces)
    tm.update_faces(tm.nondegenerate_faces())
    dropped = f1 - len(tm.faces)
    f2 = len(tm.faces)
    tm.update_faces(tm.unique_faces())
    dup = f2 - len(tm.faces)
    tm.remove_unreferenced_vertices()
    flipped = 0
    if len(tm.faces):
        faces, flipped = orient_faces(np.asarray(tm.vertices), np.asarray(tm.faces))
        tm = trimesh.Trimesh(np.asarray(tm.vertices), faces, process=False)
    counts = {"merged_vertices": v0 - len(tm.vertices), "degenerate_faces": dropped, "duplicate_faces": dup, "reoriented_faces": flipped,
              "winding_was_consistent": int(wound)}
    return MeshData(m.node_id, m.prim_path, np.asarray(tm.vertices, np.float64), np.asarray(tm.faces, np.int32), m.material_id, m.collider), counts
