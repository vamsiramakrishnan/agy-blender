"""BatchSanitizeDAG: many input meshes -> safe deterministic repairs -> UVs/LODs/colliders -> validated export.

No model calls unless a ``plane`` is passed for an optional visual check. Repairs are limited to ones that cannot
change the shape: weld duplicate vertices, drop degenerate and duplicate faces, fix winding / inverted normals,
and reground the whole asset. Anything else the Tier-1 checks find is reported as *remaining*, not guessed at.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any

import numpy as np
import trimesh
from pxr import Usd, UsdGeom

from antigravity3d.dags.runner import Dag, DagReport, NodeCtx
from antigravity3d.kernel.compile_ir import USD_EXTS
from antigravity3d.kernel.fast_ops import run_export_pipeline, tier1_qa
from antigravity3d.kernel.raster import contact_sheet
from antigravity3d.kernel.types import MeshData
from antigravity3d.kernel.usd_stage import StageManager, _triangulate
from antigravity3d.kernel.validator import check_engine_compliance, validate_usd
from antigravity3d.state import EngineBudget
from antigravity3d.tactics.common import Uploader, data_uri_uploader, png_bytes
from antigravity3d.tactics.package import write_glb_package, write_package_usd

MESH_EXTS = {".glb", ".gltf", ".obj", ".ply", ".stl", *USD_EXTS}
GROUND_TOL = 0.01


def _ident(name: str, used: set[str], default: str = "mesh") -> str:
    base = re.sub(r"[^A-Za-z0-9_]", "_", name).strip("_") or default
    base = base if base[0].isalpha() else f"{default}_{base}"
    out, k = base, 1
    while out in used:
        k += 1
        out = f"{base}_{k}"
    used.add(out)
    return out


def _load_usd(path: Path) -> list[MeshData]:
    try:
        return StageManager.open(path).extract_meshes()
    except ValueError:  # a plain USD file without our IR: read its meshes directly
        stage = Usd.Stage.Open(str(path))
        xc, out, used = UsdGeom.XformCache(), [], set()
        for prim in stage.Traverse():
            if not prim.IsA(UsdGeom.Mesh):
                continue
            m = UsdGeom.Mesh(prim)
            pts, idx = m.GetPointsAttr().Get(), m.GetFaceVertexIndicesAttr().Get()
            if not pts or not idx:
                continue
            tris = _triangulate(np.asarray(m.GetFaceVertexCountsAttr().Get(), np.int64), np.asarray(idx, np.int64))
            mat = np.array(xc.GetLocalToWorldTransform(prim))
            world = np.asarray(pts, np.float64) @ mat[:3, :3] + mat[3, :3]
            if UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.y:
                world = np.stack([world[:, 0], -world[:, 2], world[:, 1]], axis=1)
            out.append(MeshData(_ident(prim.GetName(), used), str(prim.GetPath()), world, tris))
        return out


def load_meshes(path: Path | str) -> list[MeshData]:
    """World-space, Z-up MeshData from GLB/glTF/OBJ/PLY/STL (trimesh) or USD (stage manager)."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext in USD_EXTS:
        meshes = _load_usd(path)
    else:
        scene = trimesh.load(str(path), force="scene")
        used: set[str] = set()
        meshes = []
        for node in scene.graph.nodes_geometry:
            tf, gname = scene.graph[node]
            g = scene.geometry[gname]
            if not isinstance(g, trimesh.Trimesh) or len(g.faces) == 0:
                continue
            p = trimesh.transform_points(np.asarray(g.vertices, np.float64), tf)
            if ext in (".glb", ".gltf"):  # glTF is Y-up; the pipeline is Z-up
                p = np.stack([p[:, 0], -p[:, 2], p[:, 1]], axis=1)
            meshes.append(MeshData(_ident(str(node), used), "", p, np.asarray(g.faces, np.int32)))
    if not meshes:
        raise ValueError(f"{path.name}: no triangle meshes found")
    return meshes


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
    tm = trimesh.Trimesh(np.asarray(m.points, np.float64), np.asarray(m.triangles), process=False)
    v0, f0, wound = len(tm.vertices), len(tm.faces), bool(tm.is_winding_consistent)
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


def reground(meshes: list[MeshData]) -> tuple[list[MeshData], float]:
    lo = min(float(m.points[:, 2].min()) for m in meshes if len(m.points))
    if abs(lo) <= GROUND_TOL:
        return meshes, 0.0
    return [MeshData(m.node_id, m.prim_path, m.points - np.array([0.0, 0.0, lo]), m.triangles, m.material_id, m.collider) for m in meshes], -lo


def _tokens(report: Any) -> set[str]:
    return {f"{i.code}@{i.node_id}" if i.node_id else i.code for i in report.issues}


class BatchSanitizeDAG:
    def __init__(self, *, workdir: Path | str, budget: EngineBudget, plane: Any = None, uploader: Uploader | None = None, forge: Any = None,
                 pool: Any = None, cache: Any = None, max_parallel: int = 4) -> None:
        self.out, self.budget, self.plane, self.cache = Path(workdir), budget, plane, cache
        self.up = uploader or (forge.upload if forge is not None else data_uri_uploader)
        self.max_parallel = max_parallel

    async def sanitize_one(self, path: Path, out_dir: Path) -> dict[str, Any]:
        t: dict[str, float] = {}
        clock = time.perf_counter
        row: dict[str, Any] = {"file": str(path), "status": "ok", "ms": t}

        t0 = clock()
        meshes = await asyncio.to_thread(load_meshes, path)
        t["load"] = (clock() - t0) * 1e3
        asset = _ident(path.stem, set(), "Asset")
        before = await asyncio.to_thread(tier1_qa, meshes, self.budget)
        row.update(asset=asset, tris_before=before.stats["tris"], issues_before=sorted(_tokens(before)))

        t0 = clock()
        fixed_meshes, counts = [], {}
        for m in meshes:
            r, c = await asyncio.to_thread(repair_mesh, m)
            fixed_meshes.append(r)
            for k, v in c.items():
                counts[k] = counts.get(k, 0) + v
        fixed_meshes, dz = await asyncio.to_thread(reground, fixed_meshes)
        counts["grounded_dz"] = round(dz, 6)
        row["repairs"] = counts
        t["repair"] = (clock() - t0) * 1e3
        after = await asyncio.to_thread(tier1_qa, fixed_meshes, self.budget)
        row.update(tris_after=after.stats["tris"], issues_after=sorted(_tokens(after)),
                   issues_fixed=sorted(_tokens(before) - _tokens(after)), issues_remaining=sorted(_tokens(after)))
        crit_left = [i for i in after.issues if i.severity == "critical"]

        t0 = clock()
        bundle = await asyncio.to_thread(run_export_pipeline, fixed_meshes, self.budget, asset)
        t["uv_lod_collider"] = (clock() - t0) * 1e3
        levels = [sum(len(v[k].triangles) for v in bundle.lods.values()) for k in range(min((len(v) for v in bundle.lods.values()), default=0))]
        row.update(lod_tris=levels, hulls=len(bundle.colliders), uv_stretch_max=max((u.stretch_max for u in bundle.uvs), default=0.0))

        t0 = clock()
        out_dir.mkdir(parents=True, exist_ok=True)
        usd = write_package_usd(out_dir / f"{asset}.usda", asset, bundle.uvs, {}, [], bundle.colliders, self.budget.target_engine)
        glb = await asyncio.to_thread(write_glb_package, out_dir / f"{asset}.glb", bundle)
        stage = Usd.Stage.Open(str(usd))
        val = [i for i in validate_usd(stage) if i.severity == "critical"]
        comp = [i for i in check_engine_compliance(stage, fixed_meshes, self.budget) if i.severity == "critical"]
        t["validate_export"] = (clock() - t0) * 1e3
        row.update(validate_critical=[f"{i.code}@{i.node_id}" for i in val], compliance_critical=[f"{i.code}@{i.node_id}" for i in comp],
                   outputs={"usd": str(usd), "glb": str(glb)})

        if self.plane is not None:
            t0 = clock()
            sheet = await asyncio.to_thread(contact_sheet, fixed_meshes)
            from antigravity3d.decisions.compose import decide_action, tier15_score

            q = await self.plane.evaluate_visual_qa(await self.up(png_bytes(sheet)), stage="blockout")
            row["visual"] = {"tier15": tier15_score(q), "action": decide_action(q, None, "blockout").kind}
            t["visual_check"] = (clock() - t0) * 1e3
        if crit_left or val or comp:
            row["status"] = "needs_attention"
        for k in t:
            t[k] = round(t[k], 1)
        return row

    def build(self, files: list[Path | str]) -> Dag:
        dag = Dag("batch_sanitize")
        seen: set[str] = set()
        for f in files:
            p = Path(f)
            node = f"file:{_ident(p.name, seen, 'file')}"

            async def run(ctx: NodeCtx, p: Path = p, node: str = node) -> dict[str, Any]:
                row = await self.sanitize_one(p, self.out / node.split(":", 1)[1])
                ctx.data(status=row["status"], tris_before=row["tris_before"], tris_after=row["tris_after"])
                return row

            dag.node(node, run)
        return dag

    async def run(self, files: list[Path | str]) -> DagReport:
        rep = await self.build(files).run(max_parallel=self.max_parallel)
        rows = []
        paths = {n: str(f) for n, f in zip(rep.nodes, files)}  # nodes are created in input order
        for n in rep.nodes.values():
            r = rep.results.get(n.name)
            rows.append(r if r else {"file": paths[n.name], "status": "failed", "error": n.error or n.reason})
        rep.extras = {"rows": rows, "engine": self.budget.target_engine, "out": str(self.out),
                      "gates_ok": bool(rows) and all(r["status"] == "ok" for r in rows),
                      "skipped": [{"step": "visual_check", "reason": "no plane supplied: sanitize makes no model calls"}] if self.plane is None else []}
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "report.json").write_text(rep.to_json())
        return rep


def table(rows: list[dict[str, Any]]) -> str:
    lines = [f"{'file':<28}{'status':<16}{'tris':>14}  {'fixed':>5} {'left':>4}  LODs"]
    for r in rows:
        name = Path(r["file"]).name[:27]
        if r["status"] == "failed":
            lines.append(f"{name:<28}{'FAILED':<16}  {r.get('error', '')}")
            continue
        lines.append(f"{name:<28}{r['status']:<16}{r['tris_before']:>6}->{r['tris_after']:<6}  {len(r['issues_fixed']):>5} {len(r['issues_remaining']):>4}  {r['lod_tris']}")
    return "\n".join(lines)
