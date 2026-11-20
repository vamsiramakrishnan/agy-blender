"""Engine-ready export artefacts built from the kernel's ExportBundle (UV-split meshes, LODs, hulls).

The authoring stage keeps IR primitives without UVs; the *package* USD is baked from the unwrapped meshes so
it carries ``primvars:st``, material bindings and engine-named collision hulls, in the engine's native up axis.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Sequence

import numpy as np
from pxr import Kind, Sdf, Usd, UsdGeom, UsdPhysics, UsdShade, Vt

from antigravity3d.ir import MaterialIR
from antigravity3d.kernel.compile_ir import _author_material, sync_double_sided
from antigravity3d.kernel.fast_ops import ExportBundle, Hull, UVResult, write_gltf
from antigravity3d.kernel.validator import ENGINE_RULES


def hull_prim_name(engine: str, asset: str, index: int, hull_name: str) -> str | None:
    """USD collision-hull prim name: always ``UCX_<asset>_<nn>`` (USD identifiers cannot carry Godot's '-convcolonly')."""
    return hull_name if hull_name.startswith("UCX_") else f"UCX_{asset}_{index:02d}"


def _to_native(points: np.ndarray, engine: str) -> np.ndarray:
    p = np.asarray(points, np.float64)
    return p if ENGINE_RULES[engine].native_up_axis == "Z" else np.stack([p[:, 0], p[:, 2], -p[:, 1]], axis=1)  # type: ignore[index]


def _define_mesh(stage: Usd.Stage, path: str, pts: np.ndarray, tris: np.ndarray, uvs: np.ndarray | None) -> Usd.Prim:
    mesh = UsdGeom.Mesh.Define(stage, path)
    mesh.CreatePointsAttr().Set(Vt.Vec3fArray.FromNumpy(np.ascontiguousarray(pts, np.float32)))
    mesh.CreateFaceVertexCountsAttr().Set(Vt.IntArray.FromNumpy(np.full(len(tris), 3, np.int32)))
    mesh.CreateFaceVertexIndicesAttr().Set(Vt.IntArray.FromNumpy(np.ascontiguousarray(tris, np.int32).ravel()))
    mesh.CreateExtentAttr().Set(Vt.Vec3fArray.FromNumpy(np.stack([pts.min(0), pts.max(0)]).astype(np.float32)))
    mesh.CreateSubdivisionSchemeAttr().Set(UsdGeom.Tokens.none)
    if uvs is not None:
        pv = UsdGeom.PrimvarsAPI(mesh.GetPrim()).CreatePrimvar("st", Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.vertex)
        pv.Set(Vt.Vec2fArray.FromNumpy(np.ascontiguousarray(uvs, np.float32)))
    return mesh.GetPrim()


def write_package_usd(
    path: Path, asset_name: str, uvs: Sequence[UVResult], mat_of: dict[str, str | None], materials: Sequence[MaterialIR],
    hulls: Sequence[Hull], engine: str, *, textures_from: Path | None = None,
) -> Path:
    """Writes the baked package. ``textures_from``: directory whose ``textures/`` folder is copied next to the file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stage = Usd.Stage.CreateNew(str(path))
    root = UsdGeom.Xform.Define(stage, f"/{asset_name}")
    Usd.ModelAPI(root.GetPrim()).SetKind(Kind.Tokens.component)
    stage.SetDefaultPrim(root.GetPrim())
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z if ENGINE_RULES[engine].native_up_axis == "Z" else UsdGeom.Tokens.y)  # type: ignore[index]
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    used = {m for m in mat_of.values() if m}
    if used:
        UsdGeom.Scope.Define(stage, f"/{asset_name}/Looks")
    for m in materials:
        if m.id in used:
            _author_material(stage, Sdf.Path(f"/{asset_name}/Looks/{m.id}"), m)
    for u in uvs:
        prim = _define_mesh(stage, f"/{asset_name}/{u.node_id}", _to_native(u.points, engine), u.triangles, u.uvs)
        if mat_of.get(u.node_id):
            UsdShade.MaterialBindingAPI.Apply(prim).Bind(UsdShade.Material(stage.GetPrimAtPath(f"/{asset_name}/Looks/{mat_of[u.node_id]}")))
    if sided := {m.id for m in materials if m.id in used and m.double_sided}:  # a fresh stage: nothing to clear
        sync_double_sided(stage, root.GetPrim(), sided)
    for i, h in enumerate(hulls):
        name = hull_prim_name(engine, asset_name, i, h.name)
        if name:
            prim = _define_mesh(stage, f"/{asset_name}/{name}", _to_native(h.points, engine), h.triangles, None)
            UsdGeom.Imageable(prim).CreatePurposeAttr(UsdGeom.Tokens.guide)
            UsdPhysics.CollisionAPI.Apply(prim)
            UsdPhysics.MeshCollisionAPI.Apply(prim).CreateApproximationAttr().Set("convexHull")
    stage.GetRootLayer().Save()
    if textures_from is not None and (Path(textures_from) / "textures").is_dir():
        shutil.copytree(Path(textures_from) / "textures", path.parent / "textures", dirs_exist_ok=True)
    return path


def write_glb_package(path: Path, bundle: ExportBundle) -> Path:
    """Export sanitized geometry as a visible LOD0 plus separate LOD and collider sidecars."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    for i in range(1, min((len(chain) for chain in bundle.lods.values()), default=0)):
        write_gltf(path.with_name(f"{path.stem}_LOD{i}.glb"), [chain[i] for chain in bundle.lods.values()],
                   [f"{node}_LOD{i}" for node in bundle.lods])
    if bundle.colliders:
        write_gltf(path.with_name(f"{path.stem}_colliders.glb"), bundle.colliders, [h.name for h in bundle.colliders])
    return write_gltf(path, bundle.uvs)


def merged_lods(bundle: ExportBundle) -> list[SimpleNamespace]:
    """One merged mesh per LOD level across all nodes (levels shared by every node), carrying the level's
    ``screen_fraction`` (the largest screen size it is shown at; LOD0 1.0) so the pop check renders each switch
    where the generator placed it."""
    levels = min((len(v) for v in bundle.lods.values()), default=0)
    out = []
    for k in range(levels):
        pts, tris, base = [], [], 0
        for lods in bundle.lods.values():
            pts.append(lods[k].points)
            tris.append(lods[k].triangles + base)
            base += len(lods[k].points)
        fr = min(float(getattr(lods[k], "screen_fraction", 1.0)) for lods in bundle.lods.values())
        out.append(SimpleNamespace(points=np.vstack(pts), triangles=np.vstack(tris), screen_fraction=fr if k else 1.0))
    return out


__all__ = ["hull_prim_name", "merged_lods", "write_glb_package", "write_package_usd"]
