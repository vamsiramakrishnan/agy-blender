"""FBX export of a shipped package, and the proof that the FBX is the same asset.

Runs inside the Blender worker (``daemon.py`` loads it by path, like ``skills.py``); the standard library only at
module level, ``bpy`` / ``numpy`` / the FBX add-on's parser are imported inside the functions that need them, so the
pure parts (the engine plan, the header and extent checks) are importable and tested without Blender.

What it writes is exactly one file, ``<out_dir>/<asset_name>.fbx``, produced by the reviewed upstream script
(``export_asset.py``, presets ``unity`` / ``unreal`` / ``roblox``) after the request passed
``skills.check_export_request``. The scene it exports is built here, from the package's GLB files, one render mesh per
LOD level, named by the target engine's convention (see :func:`plan`). The FBX is written under a temporary name,
re-imported and compared with the sources; only a file that passed is renamed to its final name.

Nothing in this module can say how Unity, Unreal Engine or Roblox Studio import the file: none of them runs here.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Callable

ROBLOX_MAX_TRIS = 20000  # Roblox "Mesh specifications": "Individual meshes can not exceed 20,000 triangles."
UNITY_MAX_LODS = 8  # Unity "Import LOD levels": "The LOD Group component accepts a maximum of eight LOD levels."
EXTENT_TOL_M = 1e-4  # metres; FBX vertices are float32 and the assets are a few metres across
SCALE_TOL = 1e-6
SLOTS = ("base_color", "normal", "roughness", "metallic", "emission", "alpha", "occlusion")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".tga", ".bmp", ".webp")

# What the upstream presets promise (their own comments), as numbers in the written file. The Blender FBX exporter
# writes the unit as UnitScaleFactor (centimetres per file unit): 100 means one file unit is a metre.
#   unity:  FBX All + Apply Transform, -Z forward, Y up -> "scale 1, no -90 X rotation on import": the axis
#           conversion is baked into the vertices, so every model has rotation 0 and scale 1
#   unreal: FBX Units Scale, -Z forward, Y up -> "the unit goes into the FBX header (no 100x root scale)"; the axis
#           conversion stays a -90 degree X rotation on each model, which is how Blender writes it without baking
#   roblox: FBX Unit Scale (Creator Hub), same transform handling as unreal
EXPECTED_HEADER: dict[str, dict[str, Any]] = {
    "unity": {"unit_scale_factor": 100.0, "up": (1, 1), "front": (2, 1), "coord": (0, 1), "model_rotation_deg": (0.0, 0.0, 0.0)},
    "unreal": {"unit_scale_factor": 100.0, "up": (1, 1), "front": (2, 1), "coord": (0, 1), "model_rotation_deg": (-90.0, 0.0, 0.0)},
    "roblox": {"unit_scale_factor": 100.0, "up": (1, 1), "front": (2, 1), "coord": (0, 1), "model_rotation_deg": (-90.0, 0.0, 0.0)},
}


class FbxMismatch(ValueError):
    """The written FBX is not the same asset as its sources; the message carries the failed checks and their numbers."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | os.PathLike[str]) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------------------------------- the plan


def plan(preset: str, asset_name: str, n_levels: int, n_hulls: int) -> dict[str, Any]:
    """The names and contents of the FBX, per engine (the documentation behind each rule is in the report).

    * unity: render meshes ``<asset>_LOD<n>`` (Unity: "Add the suffix _LODX to the name of each mesh ... Unity
      automatically creates an LOD Group"; at most eight levels); a single level keeps the plain name. No collision
      hulls: the documentation describes no FBX naming for them (Generate Colliders is an importer option).
    * unreal: render mesh ``<asset>`` plus convex hulls ``UCX_<asset>_<nn>`` ("UCX_[RenderMeshName]_##",
      "RenderMeshName must be identical to the name of the render mesh"). LOD levels are not written: Epic's FBX page
      documents the "Import LODs" option but no naming for LODs inside one FBX, so the levels stay in the GLB/USD.
    * roblox: render mesh ``<asset>`` only (Roblox generates its own LODs; its pages describe no FBX collision or LOD
      naming).
    """
    omitted: list[dict[str, str]] = []
    if preset == "unity":
        if n_levels > UNITY_MAX_LODS:
            raise ValueError(f"unity: {n_levels} LOD levels, a Unity LOD Group takes at most {UNITY_MAX_LODS}")
        render = [f"{asset_name}_LOD{i}" for i in range(n_levels)] if n_levels > 1 else [asset_name]
        hulls: list[str] = []
        if n_hulls:
            omitted.append({"what": "collision hulls", "why": "Unity documents no FBX naming for collision meshes (its Generate Colliders import option needs none)"})
    elif preset == "unreal":
        render = [asset_name]
        hulls = [f"UCX_{asset_name}_{i:02d}" for i in range(n_hulls)]
        if n_levels > 1:
            omitted.append({"what": f"LOD levels 1..{n_levels - 1}", "why": "Epic documents the Import LODs option but no LOD naming inside one FBX; the levels are in the package's GLB and USD files"})
    elif preset == "roblox":
        render = [asset_name]
        hulls = []
        if n_levels > 1:
            omitted.append({"what": f"LOD levels 1..{n_levels - 1}", "why": "Roblox creates LODs itself (RenderFidelity Automatic); its importer pages describe no LOD naming"})
        if n_hulls:
            omitted.append({"what": "collision hulls", "why": "Roblox pages describe no FBX collision naming (CollisionFidelity is set in Studio)"})
    else:
        raise ValueError(f"no plan for preset {preset!r}")
    return {"render": render, "hulls": hulls, "omitted": omitted, "levels_written": len(render)}


def expected_extent_in_file_frame(lo: list[float], hi: list[float]) -> tuple[list[float], list[float]]:
    """A Blender-world (Z up) box in the frame of the written file (Y up, -Z forward): (x, y, z) -> (x, z, -y)."""
    return [lo[0], lo[2], -hi[1]], [hi[0], hi[2], -lo[1]]


def header_checks(header: dict[str, Any], preset: str, render_extents: dict[str, tuple[list[float], list[float]]],
                  hull_extent: tuple[list[float], list[float]] | None, hull_names: list[str]) -> list[dict[str, Any]]:
    """Checks on the facts parsed from the written file (``read_header``) against the preset's promises and the sources.

    ``render_extents``: model name -> expected (min, max) in metres in the file frame; ``hull_extent`` the union
    over the collision hulls.
    """
    want = EXPECTED_HEADER[preset]
    g = header["global"]
    checks: list[dict[str, Any]] = []

    def add(name: str, expected: Any, actual: Any, ok: bool) -> None:
        checks.append({"name": name, "expected": expected, "actual": actual, "ok": bool(ok)})

    add("header.unit_scale_factor", want["unit_scale_factor"], g["UnitScaleFactor"], abs(g["UnitScaleFactor"] - want["unit_scale_factor"]) < SCALE_TOL)
    add("header.original_unit_scale_factor", want["unit_scale_factor"], g["OriginalUnitScaleFactor"], abs(g["OriginalUnitScaleFactor"] - want["unit_scale_factor"]) < SCALE_TOL)
    for key, axis in (("up", "UpAxis"), ("front", "FrontAxis"), ("coord", "CoordAxis")):
        add(f"header.{key}_axis", list(want[key]), [g[axis], g[axis + "Sign"]], (g[axis], g[axis + "Sign"]) == want[key])
    models = header["models"]
    expected_names = [*render_extents, *hull_names]
    for name in expected_names:
        if name not in models:
            add(f"model.{name}.present", True, False, False)
            continue
        m = models[name]
        add(f"model.{name}.scaling", [1.0, 1.0, 1.0], m["scaling"], all(abs(s - 1.0) < SCALE_TOL for s in m["scaling"]))
        rot = [a + b for a, b in zip(m["rotation_deg"], m["pre_rotation_deg"])]
        add(f"model.{name}.rotation_deg", list(want["model_rotation_deg"]), rot, all(_angle_close(a, b) for a, b in zip(rot, want["model_rotation_deg"])))
        add(f"model.{name}.unsupported_transform_props", [], m["unsupported"], not m["unsupported"])
        add(f"model.{name}.parent", None, m["parent"], m["parent"] is None)
    extra = sorted(set(models) - set(expected_names))
    add("models.no_others", [], extra, not extra)
    for name, (lo, hi) in render_extents.items():
        m = models.get(name)
        if m is None or m["extent_min_m"] is None:
            continue
        err = max(max(abs(a - b) for a, b in zip(m["extent_min_m"], lo)), max(abs(a - b) for a, b in zip(m["extent_max_m"], hi)))
        add(f"extent_m.{name}", {"min": _r(lo), "max": _r(hi)}, {"min": _r(m["extent_min_m"]), "max": _r(m["extent_max_m"])}, err <= EXTENT_TOL_M)
    if hull_extent is not None:
        have = [models[n] for n in hull_names if n in models and models[n]["extent_min_m"] is not None]
        if have:
            lo = [min(m["extent_min_m"][i] for m in have) for i in range(3)]
            hi = [max(m["extent_max_m"][i] for m in have) for i in range(3)]
            err = max(max(abs(a - b) for a, b in zip(lo, hull_extent[0])), max(abs(a - b) for a, b in zip(hi, hull_extent[1])))
            add("extent_m.collision_hulls", {"min": _r(hull_extent[0]), "max": _r(hull_extent[1])}, {"min": _r(lo), "max": _r(hi)}, err <= EXTENT_TOL_M)
    return checks


def _angle_close(a: float, b: float, tol: float = 1e-3) -> bool:
    return abs(((a - b) + 180.0) % 360.0 - 180.0) < tol


def _r(v: list[float], n: int = 5) -> list[float]:
    return [round(float(x), n) for x in v]


# ---------------------------------------------------------------------------------------------------- the FBX file


def read_header(path: str) -> dict[str, Any]:
    """Facts an engine importer reads, parsed straight from the binary FBX with Blender's own parser (not an import).

    ``global``: the axis and unit settings; ``models``: every Model node with its local translation / rotation /
    pre-rotation / scaling, its parent and (when it carries a mesh) the extent of its vertices in metres in the file's
    frame; ``textures``: each texture's slot, relative and absolute file name and whether it carries embedded bytes.
    """
    import numpy as np
    from io_scene_fbx import parse_fbx

    root, version = parse_fbx.parse(path)
    top = {e.id: e for e in root.elems}

    def nm(raw: bytes) -> str:
        return raw.split(b"\x00\x01")[0].decode("utf-8", "replace")

    glob: dict[str, Any] = {}
    for e in top[b"GlobalSettings"].elems:
        if e.id == b"Properties70":
            for q in e.elems:
                glob[q.props[0].decode()] = q.props[4]
    need = ("UpAxis", "UpAxisSign", "FrontAxis", "FrontAxisSign", "CoordAxis", "CoordAxisSign", "UnitScaleFactor", "OriginalUnitScaleFactor")
    glob = {k: glob[k] for k in need}
    unit = float(glob["UnitScaleFactor"]) / 100.0  # file unit -> metres

    models: dict[int, dict[str, Any]] = {}
    geometry: dict[int, Any] = {}
    videos: dict[str, dict[str, Any]] = {}
    textures: dict[int, dict[str, Any]] = {}
    for o in top[b"Objects"].elems:
        if o.id == b"Model":
            props: dict[str, Any] = {}
            for e in o.elems:
                if e.id == b"Properties70":
                    for q in e.elems:
                        props[q.props[0].decode()] = list(q.props[4:])
            handled = {"Lcl Translation", "Lcl Rotation", "Lcl Scaling", "PreRotation", "DefaultAttributeIndex", "InheritType",
                       "RotationOrder", "Visibility", "Visibility Inheritance", "MaxHandle", "UDP3DSMAX", "currentUVSet"}
            unsupported = sorted(k for k, v in props.items() if k not in handled and k in
                                 {"RotationOffset", "RotationPivot", "ScalingOffset", "ScalingPivot", "GeometricTranslation", "GeometricRotation",
                                  "GeometricScaling", "PostRotation", "RotationActive", "TranslationActive"} and any(abs(float(x)) > 1e-9 for x in v if isinstance(x, (int, float))))
            if props.get("RotationOrder", [0])[0] not in (0, None):
                unsupported.append("RotationOrder")
            models[o.props[0]] = {"name": nm(o.props[1]), "type": o.props[2].decode(), "parent": None,
                                  "translation": list(props.get("Lcl Translation", [0.0, 0.0, 0.0])),
                                  "rotation_deg": list(props.get("Lcl Rotation", [0.0, 0.0, 0.0])),
                                  "pre_rotation_deg": list(props.get("PreRotation", [0.0, 0.0, 0.0])),
                                  "scaling": list(props.get("Lcl Scaling", [1.0, 1.0, 1.0])), "unsupported": unsupported,
                                  "geometry": None, "normals_written": False, "extent_min_m": None, "extent_max_m": None, "triangles": None}
        elif o.id == b"Geometry":
            verts = next((e.props[0] for e in o.elems if e.id == b"Vertices"), None)
            idx = next((e.props[0] for e in o.elems if e.id == b"PolygonVertexIndex"), None)
            geometry[o.props[0]] = (verts, idx, any(e.id == b"LayerElementNormal" for e in o.elems))
        elif o.id == b"Video":
            content = next((e for e in o.elems if e.id == b"Content"), None)
            videos[nm(o.props[1])] = {"relative": next((e.props[0].decode("utf-8", "replace") for e in o.elems if e.id == b"RelativeFilename"), None),
                                      "filename": next((e.props[0].decode("utf-8", "replace") for e in o.elems if e.id == b"Filename"), None),
                                      "embedded_bytes": len(content.props[0]) if content is not None else 0,
                                      "embedded_sha256": sha256_bytes(bytes(content.props[0])) if content is not None and content.props[0] else None}
        elif o.id == b"Texture":
            textures[o.props[0]] = {"name": nm(o.props[1]), "media": next((nm(e.props[0]) for e in o.elems if e.id == b"Media"), None), "slot": None}
    for c in top[b"Connections"].elems:
        if c.id != b"C":
            continue
        kind, child, parent = c.props[0], c.props[1], c.props[2]
        if kind == b"OO" and child in models and parent in models:
            models[child]["parent"] = parent
        elif kind == b"OO" and child in geometry and parent in models:
            models[parent]["geometry"] = child
        elif kind == b"OP" and child in textures:
            textures[child]["slot"] = c.props[3].decode()

    def rot(deg: list[float]) -> Any:
        rx, ry, rz = (math.radians(a) for a in deg)
        cx, sx, cy, sy, cz, sz = math.cos(rx), math.sin(rx), math.cos(ry), math.sin(ry), math.cos(rz), math.sin(rz)
        mx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
        my = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
        mz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
        return mz @ my @ mx  # FBX EulerXYZ: X is applied first

    def local(m: dict[str, Any]) -> Any:
        mat = np.eye(4)
        mat[:3, :3] = rot(m["pre_rotation_deg"]) @ rot(m["rotation_deg"]) @ np.diag(m["scaling"])
        mat[:3, 3] = m["translation"]
        return mat

    def world(mid: int) -> Any:
        m = models[mid]
        return local(m) if m["parent"] is None else world(m["parent"]) @ local(m)

    for mid, m in models.items():
        if m["geometry"] is None:
            continue
        verts, idx, has_normals = geometry[m["geometry"]]
        pts = np.asarray(verts, dtype=np.float64).reshape(-1, 3)
        w = world(mid)
        pts = pts @ w[:3, :3].T + w[:3, 3]
        m["extent_min_m"] = (pts.min(0) * unit).tolist()
        m["extent_max_m"] = (pts.max(0) * unit).tolist()
        m["normals_written"] = bool(has_normals)
        if idx is not None:  # a polygon ends at its negative index; triangles = sum(size - 2)
            ends = int((np.asarray(idx) < 0).sum())
            m["triangles"] = int(len(idx) - 2 * ends)
    named = {mid: {**m, "parent": models[m["parent"]]["name"] if m["parent"] in models else None} for mid, m in models.items()}
    out_models = {m["name"]: {k: v for k, v in m.items() if k not in ("name", "geometry")} for m in named.values()}
    tex_out = [{"slot": t["slot"], "name": t["name"], **{k: v for k, v in videos.get(t["media"] or "", {}).items()}} for t in textures.values()]
    return {"version": version, "global": glob, "models": out_models, "textures": tex_out}


# ---------------------------------------------------------------------------------------------------- Blender side


def _enable(addon: str, what: str) -> None:
    import addon_utils

    if not addon_utils.check(addon)[1]:
        addon_utils.enable(addon, default_set=True)
    if not addon_utils.check(addon)[1]:
        raise RuntimeError(f"the {what} (add-on {addon}) is not available in this Blender")


def _base(name: str) -> str:
    return re.sub(r"\.\d{3}$", "", name)  # Blender's collision suffix for a datablock imported a second time


def _tris(mesh: Any) -> int:
    mesh.calc_loop_triangles()
    return len(mesh.loop_triangles)


def _image_bytes(img: Any) -> bytes | None:
    if img.packed_file is not None:
        return bytes(img.packed_file.data)
    import bpy

    path = bpy.path.abspath(img.filepath)
    if path and os.path.isfile(path):
        with open(path, "rb") as f:
            return f.read()
    return None


def _slot_images(mat: Any) -> dict[str, list[str]]:
    """Which images feed each slot of a material (sha256 of their bytes), found by walking the links upstream."""
    out: dict[str, list[str]] = {s: [] for s in SLOTS}
    if mat is None or not mat.use_nodes or mat.node_tree is None:
        return out
    tree = mat.node_tree
    bsdf = next((n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"), None)

    def images_upstream(sock: Any, depth: int = 0) -> list[Any]:
        found: list[Any] = []
        if depth > 8:
            return found
        for link in sock.links:
            node = link.from_node
            if node.type == "TEX_IMAGE" and node.image is not None:
                found.append(node.image)
            else:
                for inp in node.inputs:
                    found += images_upstream(inp, depth + 1)
        return found

    def sha(imgs: list[Any]) -> list[str]:
        digests = []
        for img in imgs:
            data = _image_bytes(img)
            digests.append(sha256_bytes(data) if data is not None else f"unreadable:{img.name}")
        return sorted(set(digests))

    if bsdf is not None:
        for slot, socket in (("base_color", "Base Color"), ("normal", "Normal"), ("roughness", "Roughness"), ("metallic", "Metallic"),
                             ("emission", "Emission Color"), ("alpha", "Alpha")):
            if socket in bsdf.inputs:
                out[slot] = sha(images_upstream(bsdf.inputs[socket]))
    for n in tree.nodes:  # the glTF importer keeps the occlusion map in this group; it has no Principled socket
        if n.type == "GROUP" and n.node_tree is not None and "glTF" in n.node_tree.name:
            out["occlusion"] = sha([img for inp in n.inputs for img in images_upstream(inp)])
    return out


def _measure(bpy: Any, objs: list[Any]) -> dict[str, Any]:
    """Counts and bounds of mesh objects in the current scene (world space, metres)."""
    from mathutils import Vector

    bpy.context.view_layer.update()
    meshes = [o for o in objs if o.type == "MESH"]
    pts = [o.matrix_world @ Vector(c) for o in meshes for c in o.bound_box]
    lo = [min(p[i] for p in pts) for i in range(3)] if pts else [0.0] * 3
    hi = [max(p[i] for p in pts) for i in range(3)] if pts else [0.0] * 3
    mats = sorted({_base(s.material.name) for o in meshes for s in o.material_slots if s.material is not None})
    return {"objects": len(meshes), "tris": sum(_tris(o.data) for o in meshes), "verts": sum(len(o.data.vertices) for o in meshes),
            "uv_layers": max((len(o.data.uv_layers) for o in meshes), default=0), "materials": mats,
            "custom_normals": any(o.data.has_custom_normals for o in meshes), "lo": lo, "hi": hi}


def _watertight(bpy: Any, mesh: Any) -> dict[str, int]:
    """Open edges of a mesh after welding coincident points (UV seams split vertices in a shipped GLB)."""
    import bmesh

    bm = bmesh.new()
    bm.from_mesh(mesh)
    bmesh.ops.remove_doubles(bm, verts=bm.verts[:], dist=1e-6)
    open_edges = sum(1 for e in bm.edges if len(e.link_faces) != 2)
    bm.free()
    return {"open_edges": open_edges}


def _join(bpy: Any, objs: list[Any]) -> Any:
    """One mesh object from ``objs`` (material slots merge by material); the first object keeps its transform."""
    active = objs[0]
    if len(objs) > 1:
        with bpy.context.temp_override(active_object=active, object=active, selected_objects=objs, selected_editable_objects=objs):
            bpy.ops.object.join()
    return active


def sha_map(req: dict[str, Any]) -> dict[str, str]:
    """``{sha256 of the GLB's image: sha256 of the file the FBX must reference instead}`` from the request's ``texture_map``
    (file names in ``textures_dir``: the GLB embeds ``normal.png`` (+Y); Unreal reads the package's ``normal_directx.png``)."""
    out: dict[str, str] = {}
    for was, now in (req.get("texture_map") or {}).items():
        out[sha256_file(os.path.join(req["textures_dir"], was))] = sha256_file(os.path.join(req["textures_dir"], now))
    return out


def _bind_images(bpy: Any, preset: str, textures_dir: str | None, out_dir: str | None, scratch_name: str,
                 texture_map: dict[str, str] | None = None) -> dict[str, Any]:
    """Give the scene's images the identity the FBX needs, without writing anything.

    unity / unreal: every packed image (a GLB's embedded image, or a far LOD's referenced one, which the importer packs
    too) is replaced by the package's own ``textures/`` file with the same bytes, so the exporter writes a relative
    reference to a file that already exists and nothing is copied. The files must lie under ``out_dir`` (the exporter
    writes an absolute path otherwise, which does not survive a move). roblox: the images stay packed (the preset
    embeds them); each gets the file name the package gave the same bytes, so the embedded copy has an extension.

    ``texture_map`` (unity / unreal): an image whose bytes are the file ``was`` is bound to the file ``now`` instead.
    """
    by_sha: dict[str, str] = {}
    swap: dict[str, tuple[str, str]] = {}  # sha256 of the GLB's bytes -> (their file name, the file to reference)
    for was, now in (texture_map or {}).items():
        swap[sha256_file(Path(textures_dir or "") / was)] = (was, str(Path(textures_dir or "") / now))
    if textures_dir:
        for p in sorted(Path(textures_dir).iterdir()):
            if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES:
                by_sha.setdefault(sha256_file(p), str(p))
    rebound: list[dict[str, Any]] = []
    unresolved: list[str] = []
    seen: dict[int, Any] = {}
    for mat in bpy.data.materials:
        if not mat.use_nodes or mat.node_tree is None:
            continue
        for node in mat.node_tree.nodes:
            if node.type != "TEX_IMAGE" or node.image is None:
                continue
            img = node.image
            data = _image_bytes(img)
            digest = sha256_bytes(data) if data is not None else None
            match = by_sha.get(digest) if digest else None
            replaced = swap.get(digest) if digest else None
            if preset == "roblox":
                if data is None:
                    unresolved.append(img.name)
                elif img.packed_file is not None:
                    if match is not None:
                        name = os.path.basename(match)
                    else:
                        name = (img.name if img.name.lower().endswith(IMAGE_SUFFIXES) else img.name + (".jpg" if data[:2] == b"\xff\xd8" else ".png"))
                    img.filepath = os.path.join(scratch_name, name)  # does not exist: the exporter embeds the packed bytes and copies nothing
                continue
            if replaced is not None:
                match = replaced[1]
            elif img.packed_file is None and img.filepath and os.path.isfile(bpy.path.abspath(img.filepath)):
                resolved = os.path.realpath(bpy.path.abspath(img.filepath))
                if out_dir and not _inside(resolved, out_dir):
                    unresolved.append(f"{img.name}: {resolved} is outside the output directory")
                continue
            if match is None:
                unresolved.append(img.name if data is not None else f"{img.name} (no data)")
                continue
            if not out_dir or not _inside(match, out_dir):
                unresolved.append(f"{img.name}: {match} is outside the output directory")
                continue
            new = seen.get(id(img))
            if new is None:
                new = bpy.data.images.load(match, check_existing=True)
                new.colorspace_settings.name = img.colorspace_settings.name
                new.alpha_mode = img.alpha_mode
                seen[id(img)] = new
                rebound.append({"image": img.name, "file": match, "sha256": digest, **({"instead_of": replaced[0]} if replaced else {})})
            node.image = new
    if unresolved:
        raise ValueError(f"{preset}: images with no file in textures_dir under the output directory (the FBX would reference paths that do not exist): {sorted(set(unresolved))}")
    return {"rebound": rebound}


def _inside(path: str, directory: str) -> bool:
    p, d = os.path.realpath(path), os.path.realpath(directory)
    return os.path.commonpath([p, d]) == d


def import_sources(bpy: Any, import_gltf: Callable[[dict[str, Any]], dict[str, Any]], reset: Callable[[dict[str, Any]], Any],
                   req: dict[str, Any]) -> dict[str, Any]:
    """Empty scene, then each source file; per level the objects it produced and their measurements (before any edit)."""
    reset({})
    levels: list[dict[str, Any]] = []
    for path in [req["source"], *req["lods"]]:
        names = import_gltf({"path": path})["objects"]
        objs = [bpy.data.objects[n] for n in names if bpy.data.objects[n].type == "MESH"]
        if not objs:
            raise ValueError(f"{path} imported no mesh object")
        m = _measure(bpy, objs)
        m["maps"] = {mat: _slot_images(bpy.data.materials.get(mat)) for mat in m["materials"]}
        levels.append({"path": path, "names": [o.name for o in objs], **m})
    hulls = None
    if req.get("colliders"):
        names = import_gltf({"path": req["colliders"]})["objects"]
        objs = sorted((bpy.data.objects[n] for n in names if bpy.data.objects[n].type == "MESH"), key=lambda o: o.name)
        if objs:
            hulls = {"path": req["colliders"], "names": [o.name for o in objs], **_measure(bpy, objs),
                     "per_hull_tris": [_tris(o.data) for o in objs]}
    # first import's materials are the asset's; later levels' copies are folded into them by name
    return {"levels": levels, "hulls": hulls, "sha_map": sha_map(req) if req.get("texture_map") else {}}


def build_scene(bpy: Any, req: dict[str, Any], src: dict[str, Any], names: dict[str, Any]) -> dict[str, Any]:
    """Join each level into one mesh, fold its materials into LOD0's, name everything for the engine."""
    lod0_mats = {m.name: m for m in bpy.data.materials}
    report: list[dict[str, Any]] = []
    render_objs: list[Any] = []
    for i, level in enumerate(src["levels"]):
        objs = [bpy.data.objects[n] for n in level["names"]]
        if i >= len(names["render"]):  # a level this engine's FBX does not carry
            for o in objs:
                bpy.data.objects.remove(o, do_unlink=True)
            continue
        if i > 0:
            for o in objs:
                for slot in o.material_slots:
                    mat = slot.material
                    if mat is not None and _base(mat.name) in lod0_mats and mat is not lod0_mats[_base(mat.name)]:
                        old = mat
                        slot.material = lod0_mats[_base(mat.name)]
                        if old.users == 0:
                            bpy.data.materials.remove(old)
        before = {"parts": len(objs), "tris": sum(_tris(o.data) for o in objs), "uv_layers": max(len(o.data.uv_layers) for o in objs),
                  "custom_normals": any(o.data.has_custom_normals for o in objs)}
        joined = _join(bpy, objs)
        joined.name = f"__agy_tmp_{i}"
        after = {"tris": _tris(joined.data), "uv_layers": len(joined.data.uv_layers), "custom_normals": joined.data.has_custom_normals,
                 "material_slots": [s.material.name for s in joined.material_slots if s.material is not None]}
        report.append({"level": i, **before, **{f"{k}_after": v for k, v in after.items()}})
        render_objs.append(joined)
    for obj, final in zip(render_objs, names["render"]):
        obj.name = final
        obj.data.name = final
    hull_objs: list[Any] = []
    if src["hulls"] is not None and names["hulls"]:
        hull_objs = [bpy.data.objects[n] for n in src["hulls"]["names"]]
        for k, obj in enumerate(hull_objs):
            obj.name = f"__agy_hull_{k}"
        for obj, final in zip(hull_objs, names["hulls"]):
            obj.name = final
            obj.data.name = final
    elif src["hulls"] is not None:  # not part of this engine's FBX
        for n in src["hulls"]["names"]:
            bpy.data.objects.remove(bpy.data.objects[n], do_unlink=True)
    return {"join": report, "render": render_objs, "hulls": hull_objs}


def _fbx_facts(bpy: Any, import_fbx_path: str, names: dict[str, Any]) -> dict[str, Any]:
    """Re-import the FBX into an empty scene and measure what an importer made of it."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    _enable("io_scene_fbx", "FBX importer")
    if "FINISHED" not in bpy.ops.import_scene.fbx(filepath=import_fbx_path):
        raise RuntimeError(f"FBX import failed for {import_fbx_path}")
    bpy.context.view_layer.update()
    objs = list(bpy.data.objects)
    out: dict[str, Any] = {"objects": sorted(o.name for o in objs), "meshes": {}}
    for o in objs:
        if o.type == "MESH":
            m = _measure(bpy, [o])
            m["maps"] = {mat: _slot_images(bpy.data.materials.get(mat)) for mat in m["materials"]}
            out["meshes"][o.name] = m
    render = [out["meshes"][n] for n in names["render"] if n in out["meshes"]]
    out["materials"] = sorted({mat for m in render for mat in m["materials"]})
    images: dict[str, dict[str, Any]] = {}
    for mat in bpy.data.materials:
        if mat.use_nodes and mat.node_tree:
            for n in mat.node_tree.nodes:
                if n.type == "TEX_IMAGE" and n.image is not None:
                    img = n.image
                    data = _image_bytes(img)
                    images[img.name] = {"sha256": sha256_bytes(data) if data is not None else None, "packed": img.packed_file is not None,
                                        "filepath": bpy.path.abspath(img.filepath) if img.filepath else "", "loaded": data is not None and tuple(img.size) != (0, 0)}
    out["images"] = images
    return out


def compare(src: dict[str, Any], facts: dict[str, Any], header: dict[str, Any], preset: str, names: dict[str, Any],
            fbx_dir: str, textures_dir: str | None) -> dict[str, Any]:
    """The round-trip comparison: every check with its expected and actual numbers."""
    checks: list[dict[str, Any]] = []

    def add(name: str, expected: Any, actual: Any, ok: bool) -> None:
        checks.append({"name": name, "expected": expected, "actual": actual, "ok": bool(ok)})

    meshes = facts["meshes"]
    n_levels_written = len(names["render"])
    levels = src["levels"][:n_levels_written]
    add("objects.render_meshes", names["render"], [n for n in names["render"] if n in meshes], all(n in meshes for n in names["render"]))
    add("objects.collision_hulls", names["hulls"], [n for n in names["hulls"] if n in meshes], all(n in meshes for n in names["hulls"]))
    add("objects.count", len(names["render"]) + len(names["hulls"]), len(meshes), len(meshes) == len(names["render"]) + len(names["hulls"]))
    src_materials = sorted({m for lv in src["levels"] for m in lv["materials"]})
    add("materials.count", len(src_materials), len(facts["materials"]), len(src_materials) == len(facts["materials"]))
    add("materials.names", src_materials, facts["materials"], [_base(m) for m in facts["materials"]] == src_materials or sorted(facts["materials"]) == src_materials)
    render_extents: dict[str, tuple[list[float], list[float]]] = {}
    for lv, name in zip(levels, names["render"]):
        got = meshes.get(name)
        if got is None:
            continue
        add(f"triangles.{name}", lv["tris"], got["tris"], lv["tris"] == got["tris"])
        add(f"uv_layers.{name}", lv["uv_layers"], got["uv_layers"], got["uv_layers"] == lv["uv_layers"])
        err = max(max(abs(a - b) for a, b in zip(lv["lo"], got["lo"])), max(abs(a - b) for a, b in zip(lv["hi"], got["hi"])))
        add(f"bbox_m.{name}", {"min": _r(lv["lo"]), "max": _r(lv["hi"])}, {"min": _r(got["lo"]), "max": _r(got["hi"])}, err <= EXTENT_TOL_M)
        render_extents[name] = expected_extent_in_file_frame(lv["lo"], lv["hi"])
        add(f"materials.{name}", lv["materials"], got["materials"], sorted(_base(m) for m in got["materials"]) == lv["materials"])
    hull_extent = None
    if names["hulls"] and src["hulls"] is not None:
        hs = src["hulls"]
        got_tris = [meshes[n]["tris"] for n in names["hulls"] if n in meshes]
        add("triangles.collision_hulls", hs["per_hull_tris"], got_tris, hs["per_hull_tris"] == got_tris)
        hull_extent = expected_extent_in_file_frame(hs["lo"], hs["hi"])
        lo = [min(meshes[n]["lo"][i] for n in names["hulls"] if n in meshes) for i in range(3)] if got_tris else [0.0] * 3
        hi = [max(meshes[n]["hi"][i] for n in names["hulls"] if n in meshes) for i in range(3)] if got_tris else [0.0] * 3
        err = max(max(abs(a - b) for a, b in zip(hs["lo"], lo)), max(abs(a - b) for a, b in zip(hs["hi"], hi)))
        add("bbox_m.collision_hulls", {"min": _r(hs["lo"]), "max": _r(hs["hi"])}, {"min": _r(lo), "max": _r(hi)}, err <= EXTENT_TOL_M)
    checks += header_checks(header, preset, render_extents, hull_extent, names["hulls"])

    # images: what the FBX refers to must exist (or be embedded) and be byte-identical to a source image
    src_shas = {s for lv in src["levels"] for per in lv["maps"].values() for shas in per.values() for s in shas}
    tex = header["textures"]
    fbx_shas: set[str] = set()
    problems: list[str] = []
    for t in tex:
        if preset == "roblox":
            digest = t.get("embedded_sha256")
            if digest is None:
                problems.append(f"{t['name']}: not embedded")
            else:
                fbx_shas.add(digest)
        else:
            rel = t.get("relative") or ""
            target = os.path.normpath(os.path.join(fbx_dir, rel)) if rel and not os.path.isabs(rel) else None
            if target is None:
                problems.append(f"{t['name']}: reference {rel!r} is not a path relative to the FBX")
            elif not os.path.isfile(target):
                problems.append(f"{t['name']}: {rel!r} does not resolve from {fbx_dir}")
            else:
                fbx_shas.add(sha256_file(target))
    add("images.resolvable", [], problems, not problems)
    swapped = src.get("sha_map") or {}
    foreign = sorted(fbx_shas - src_shas - set(swapped.values()))
    add("images.bytes_from_the_sources", [], foreign, not foreign)
    loaded = sorted(k for k, v in facts["images"].items() if not v["loaded"])
    add("images.loaded_by_the_importer", [], loaded, not loaded)

    # maps: base colour and normal are what every engine's importer connects; the rest is reported, not required
    source_maps: dict[str, dict[str, list[str]]] = {}
    for lv in src["levels"][:1]:
        source_maps = lv["maps"]
    fbx_maps: dict[str, dict[str, list[str]]] = {}
    if names["render"] and names["render"][0] in meshes:
        fbx_maps = {_base(k): v for k, v in meshes[names["render"][0]]["maps"].items()}
    lost: list[str] = []
    for mat, slots in source_maps.items():
        for slot, shas in slots.items():
            if shas and fbx_maps.get(mat, {}).get(slot) != sorted(swapped.get(x, x) for x in shas):
                lost.append(f"{mat}.{slot}")
    if swapped:  # the engine-native map is what the FBX's slot holds, not the GLB's bytes
        for was, now in swapped.items():
            hit = [f"{mat}.{slot}" for mat, slots in source_maps.items() for slot, shas in slots.items() if was in shas]
            held = [k for k in hit if now in fbx_maps.get(k.split(".")[0], {}).get(k.split(".")[1], [])]
            add("textures.engine_native_map_referenced", hit, held, hit == held)
    required_lost = [x for x in lost if x.split(".")[1] in ("base_color", "normal")]
    add("maps.base_color_and_normal_survive", [], required_lost, not required_lost)
    return {"ok": all(c["ok"] for c in checks), "checks": checks, "maps": {"source": source_maps, "fbx": fbx_maps, "lost": lost}}


def _tmp_names(out_dir: str, asset: str, preset: str) -> tuple[str, str | None]:
    """(temporary file, temporary directory to remove). Roblox embeds its images and writes no relative reference, so
    its temporary file can sit in a private subdirectory under the final basename (the embedded names then read
    ``<asset>.fbm/...``); the others reference ``textures/`` relative to their own directory, so they stay beside it."""
    token = f"{os.getpid():x}{int(time.time() * 1000) & 0xffffff:x}"
    if preset == "roblox":
        # Roblox embeds the absolute path of its temporary directory in the FBX: a name derived from the asset keeps two
        # exports of one asset byte-comparable (see REPRODUCIBILITY); a leftover or a concurrent export gets a unique one
        d = os.path.join(out_dir, f".agy-fbx-{asset}")
        if os.path.lexists(d):
            d = os.path.join(out_dir, f".agy-fbx-{asset}-{token}")
        return os.path.join(d, f"{asset}.fbx"), d
    return os.path.join(out_dir, f".{asset}.{token}.part.fbx"), None


def _listing(root: str) -> set[str]:
    return {os.path.join(d, f) for d, ds, fs in os.walk(root) for f in [*fs, *ds]}


def written_by_export(paths: set[str], tmp: str, tmp_dir: str | None) -> set[str]:
    """The paths among ``paths`` that this export created: its temporary file, anything named after it (Blender puts
    copied textures in ``<stem>.fbm`` beside an FBX), and its private directory with what lies in it.

    The output directory is usually the package itself, and other steps may write there while the export runs. A path
    that does not carry this export's temporary name is somebody else's file: it is neither counted against the
    export nor removed by it."""
    stem = os.path.splitext(tmp)[0] + "."
    return {p for p in paths
            if p == tmp or p.startswith(stem) or (tmp_dir is not None and (p == tmp_dir or p.startswith(tmp_dir + os.sep)))}


def _facts_summary(src: dict[str, Any]) -> dict[str, Any]:
    return {"levels": [{"file": lv["path"], "tris": lv["tris"], "verts": lv["verts"], "objects": lv["objects"], "materials": lv["materials"],
                        "uv_layers": lv["uv_layers"], "bbox_m": {"min": _r(lv["lo"]), "max": _r(lv["hi"])}} for lv in src["levels"]],
            "collision_hulls": None if src["hulls"] is None else {"file": src["hulls"]["path"], "count": len(src["hulls"]["names"]), "tris": src["hulls"]["tris"]}}


# Two exports of the same package are NOT byte-identical. Measured (three presets, exports a second or more apart, same
# directory): the only bytes that differ are the header's CreationTimeStamp (Second and Millisecond, and Minute and up
# further apart), which Blender's FBX exporter writes from the clock (``datetime.now()``) and offers no operator option or
# documented environment variable to fix. The file also holds the absolute path of the build directory in each texture's
# Filename (the relative path is what a reader should use). Neither is patched here: the hash in the manifest is the hash of
# this file, and the manifest says that it is not reproducible and why.
REPRODUCIBILITY = {
    "byte_identical_across_runs": False,
    "differs": ["FBXHeaderExtension/CreationTimeStamp: the time of the export, written by Blender's exporter from the clock (no option or documented environment variable sets it)"],
    "machine_specific": ["Video/Filename and Texture/FileName: the absolute path of the build directory (the RelativeFilename beside it is the one to read by)"],
    "note": "this file's hash identifies this file; re-exporting the same package gives a different hash with the same content",
}

NOT_VERIFIED = [
    "No Unity, Unreal Engine or Roblox Studio ran: how each engine's importer reads this file is documented, not observed.",
    "Only base colour and normal links are checked as required; roughness, metallic, occlusion and emission maps are reported (maps.lost).",
]


def _statement(preset: str, ok: bool) -> str:
    if not ok:
        return "FBX rejected: it did not match its sources; no file was kept."
    return (f"FBX written with upstream preset {preset!r}, re-imported in Blender and equal to the source GLB(s) in object, material and "
            "triangle counts, UV layers, bounding box in metres and image bytes; its header (unit scale, axes, per-model rotation and scale) "
            "was parsed from the file. It has NOT been opened in Unity, Unreal Engine or Roblox Studio.")


def export_fbx(bpy: Any, req: dict[str, Any], writer: Callable[..., dict[str, Any]], import_gltf: Callable[[dict[str, Any]], dict[str, Any]],
               reset: Callable[[dict[str, Any]], Any], script: dict[str, Any]) -> dict[str, Any]:
    """Write ``req['final']`` (validated by ``skills.check_export_request``) or nothing. See the module docstring."""
    t0 = time.perf_counter()
    preset, asset, out_dir, final = req["preset"], req["asset_name"], req["out_dir"], req["final"]
    src = import_sources(bpy, import_gltf, reset, req)
    names = plan(preset, asset, len(src["levels"]), len(src["hulls"]["names"]) if src["hulls"] else 0)
    t_import = time.perf_counter()
    tmp, tmp_dir = _tmp_names(out_dir, asset, preset)
    before = _listing(out_dir)
    try:
        if tmp_dir:
            os.makedirs(tmp_dir)
        textures = _bind_images(bpy, preset, req.get("textures_dir"), out_dir, "/agy-fbx-no-such-dir", req.get("texture_map"))
        built = build_scene(bpy, req, src, names)
        join_bad = [r for r in built["join"] if r["tris"] != r["tris_after"] or r["uv_layers"] != r["uv_layers_after"] or (r["custom_normals"] and not r["custom_normals_after"])]
        if join_bad:
            raise FbxMismatch(f"joining the parts of a level changed it: {json.dumps(join_bad)}")
        limits = None
        if preset == "roblox":
            m = built["render"][0]
            limits = {"roblox": {"max_triangles_per_mesh": ROBLOX_MAX_TRIS, "meshes": [{"name": m.name, "tris": _tris(m.data), **_watertight(bpy, m.data)}]}}
            limits["roblox"]["ok"] = all(x["tris"] <= ROBLOX_MAX_TRIS and x["open_edges"] == 0 for x in limits["roblox"]["meshes"])
        t_built = time.perf_counter()
        export_names = [o.name for o in (*built["render"], *built["hulls"])]
        res = writer(preset=preset, objects=export_names, out=tmp, no_anim=True)
        if not res.get("ok") or len(res.get("files", [])) != 1 or os.path.realpath(res["files"][0]["file"]) != os.path.realpath(tmp):
            raise RuntimeError(f"export_asset did not write exactly the requested file: {json.dumps(res)[:500]}")
        t_export = time.perf_counter()
        new = written_by_export(_listing(out_dir) - before, tmp, tmp_dir)
        allowed = {tmp} | ({tmp_dir} if tmp_dir else set())
        stray = sorted(new - allowed)
        if stray:
            raise RuntimeError(f"the exporter wrote files beyond the one requested: {stray}")
        header = read_header(tmp)
        facts = _fbx_facts(bpy, tmp, names)
        result = compare(src, facts, header, preset, names, os.path.dirname(tmp), req.get("textures_dir"))
        t_verify = time.perf_counter()
        if not result["ok"]:
            failed = [c for c in result["checks"] if not c["ok"]]
            raise FbxMismatch(f"{preset} FBX does not match its sources; failed checks: {json.dumps(failed, default=str)}")
        os.replace(tmp, final)
    finally:
        for p in sorted(written_by_export(_listing(out_dir) - before, tmp, tmp_dir), key=len, reverse=True):
            if p == final:
                continue
            try:
                shutil.rmtree(p) if os.path.isdir(p) else os.unlink(p)
            except OSError:
                pass
    embedded = sum(t.get("embedded_bytes", 0) for t in header["textures"])
    return {
        "ok": True, "fake": False, "preset": preset, "asset_name": asset,
        "fbx": {"path": final, "bytes": os.path.getsize(final), "sha256": sha256_file(final)},
        "blender": bpy.app.version_string, "script": script,
        "seconds": {"import": round(t_import - t0, 3), "build": round(t_built - t_import, 3), "export": round(t_export - t_built, 3),
                    "verify": round(t_verify - t_export, 3), "total": round(time.perf_counter() - t0, 3)},
        "sources": _facts_summary(src),
        "contents": {"render_meshes": [{"name": n, "tris": facts["meshes"][n]["tris"], "materials": facts["meshes"][n]["materials"],
                                       "uv_layers": facts["meshes"][n]["uv_layers"], "normals_written": header["models"][n]["normals_written"]}
                                      for n in names["render"]],
                     "collision_hulls": names["hulls"], "omitted": names["omitted"]},
        "join": built["join"],
        "textures": {"mode": "embedded" if preset == "roblox" else "relative_reference", "rebound": textures["rebound"],
                     "embedded_bytes": embedded,
                     "references": [{"slot": t["slot"], "relative": t.get("relative"), "embedded_bytes": t.get("embedded_bytes", 0),
                                     "sha256": t.get("embedded_sha256") if preset == "roblox" else sha256_file(os.path.join(out_dir, t["relative"]))}
                                    for t in header["textures"]],
                     "substituted": [dict(t) for t in dict.fromkeys(tuple(sorted({"file": os.path.basename(r["file"]), "instead_of": r["instead_of"]}.items()))
                                                    for r in textures["rebound"] if "instead_of" in r)]},
        "header": {"version": header["version"], **header["global"], "models": header["models"]},
        "comparison": {"ok": True, "checks": result["checks"]},
        "maps": result["maps"], "limits": limits, "reproducibility": REPRODUCIBILITY,
        "verified": _statement(preset, True), "not_verified": NOT_VERIFIED,
    }


def verify_fbx(bpy: Any, req: dict[str, Any], import_gltf: Callable[[dict[str, Any]], dict[str, Any]], reset: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
    """The same comparison for an FBX that already exists (read only): raises :class:`FbxMismatch` when it fails."""
    t0 = time.perf_counter()
    preset = req["preset"]
    src = import_sources(bpy, import_gltf, reset, req)
    names = plan(preset, req["asset_name"], len(src["levels"]), len(src["hulls"]["names"]) if src["hulls"] else 0)
    header = read_header(req["fbx"])
    facts = _fbx_facts(bpy, req["fbx"], names)
    result = compare(src, facts, header, preset, names, os.path.dirname(os.path.realpath(req["fbx"])), req.get("textures_dir"))
    if not result["ok"]:
        failed = [c for c in result["checks"] if not c["ok"]]
        raise FbxMismatch(f"{preset} FBX does not match its sources; failed checks: {json.dumps(failed, default=str)}")
    return {"ok": True, "fake": False, "preset": preset, "asset_name": req["asset_name"],
            "fbx": {"path": req["fbx"], "bytes": os.path.getsize(req["fbx"]), "sha256": sha256_file(req["fbx"])},
            "blender": bpy.app.version_string, "seconds": {"total": round(time.perf_counter() - t0, 3)},
            "header": {"version": header["version"], **header["global"], "models": header["models"]},
            "comparison": {"ok": True, "checks": result["checks"]}, "maps": result["maps"],
            "verified": _statement(preset, True), "not_verified": NOT_VERIFIED}
