"""The neutral evaluation of ``compare_agents.py``: the same checks, renders and judgement for every output.

Order, per output: a usable GLB exists -> stock Godot imports it (``GodotValidator``) -> the three.js check on the bare
GLB (no manifest, no USD: the same subset for every arm) -> facts read from the file (``glb_facts``) -> the upstream
``qa_check`` / ``mesh_stats`` / ``texel_density`` scripts through this project's Blender worker -> LODs and collision
-> renders with one camera and light rig -> a contact sheet. Then a blind pairwise judgement on the contact sheets,
each pair in both orders, and a static HTML gallery.
"""
from __future__ import annotations

import base64
import hashlib
import html
import io
import json
import math
import random
import shutil
from pathlib import Path
from typing import Any

import numpy as np

COMPONENT = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}
COL_SUFFIX = ("-col", "-colonly", "-convcol", "_col", "_collision", "_collider", "_colliders")
RENDER = {"width": 512, "height": 512, "samples": 32, "engine": "cycles"}
# (name, azimuth deg from the front (-Y in Blender after glTF import), elevation deg); "scale" is the ortho scale view.
VIEWS = (("scale", 0, 4), ("three_quarter", -35, 20), ("front", 0, 5), ("side", 90, 5), ("top", 0, 70),
         ("turn_045", 45, 15), ("turn_135", 135, 15), ("turn_225", 225, 15), ("turn_315", 315, 15))
CRITERIA = ("reads_as_brief", "proportion_scale", "surface_material", "defects")


# ----------------------------------------------------------------------------------------------- glTF reading


def _load(path: Path) -> tuple[Any, list[bytes]]:
    import pygltflib

    g = pygltflib.GLTF2().load(str(path))
    bufs = []
    for i, b in enumerate(g.buffers):
        if b.uri is None:
            bufs.append(g.binary_blob() or b"")
        elif b.uri.startswith("data:"):
            bufs.append(base64.b64decode(b.uri.split(",", 1)[1]))
        else:
            bufs.append((path.parent / b.uri).read_bytes())
    return g, bufs


def _view_bytes(g: Any, bufs: list[bytes], bv_index: int) -> bytes:
    bv = g.bufferViews[bv_index]
    return bufs[bv.buffer][(bv.byteOffset or 0):(bv.byteOffset or 0) + bv.byteLength]


def accessor(g: Any, bufs: list[bytes], idx: int) -> np.ndarray:
    a = g.accessors[idx]
    n, dt = NCOMP[a.type], np.dtype(COMPONENT[a.componentType])
    if a.bufferView is None:
        return np.zeros((a.count, n), dt)
    bv = g.bufferViews[a.bufferView]
    data = bufs[bv.buffer]
    start = (bv.byteOffset or 0) + (a.byteOffset or 0)
    stride = bv.byteStride or n * dt.itemsize
    if stride == n * dt.itemsize:
        arr = np.frombuffer(data, dt, a.count * n, start).reshape(a.count, n)
    else:
        raw = np.frombuffer(data, np.uint8, (a.count - 1) * stride + n * dt.itemsize, start)
        arr = np.stack([raw[i * stride:i * stride + n * dt.itemsize].view(dt) for i in range(a.count)])
    if a.normalized and dt.kind in "iu":
        arr = arr.astype(np.float64) / np.iinfo(dt).max
    return arr


def _node_matrix(node: Any) -> np.ndarray:
    if node.matrix and len(node.matrix) == 16:
        return np.array(node.matrix, float).reshape(4, 4).T
    t = np.array(node.translation or [0, 0, 0], float)
    x, y, z, w = node.rotation or [0, 0, 0, 1]
    r = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                  [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                  [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
    m = np.eye(4)
    m[:3, :3] = r @ np.diag(node.scale or [1, 1, 1])
    m[:3, 3] = t
    return m


def _instances(g: Any) -> list[tuple[int, np.ndarray, str]]:
    """(mesh index, world matrix, node name) for every mesh instance of the default scene."""
    out: list[tuple[int, np.ndarray, str]] = []
    scene = g.scenes[g.scene or 0] if g.scenes else None
    roots = scene.nodes if scene else list(range(len(g.nodes)))

    def walk(i: int, parent: np.ndarray, depth: int = 0) -> None:
        if depth > 64:
            return
        n = g.nodes[i]
        m = parent @ _node_matrix(n)
        if n.mesh is not None:
            out.append((n.mesh, m, n.name or f"node{i}"))
        for c in n.children or []:
            walk(c, m, depth + 1)

    for r in roots:
        walk(r, np.eye(4))
    return out


def _image_bytes(g: Any, bufs: list[bytes], path: Path, img_index: int) -> bytes | None:
    im = g.images[img_index]
    try:
        if im.bufferView is not None:
            return _view_bytes(g, bufs, im.bufferView)
        if im.uri and im.uri.startswith("data:"):
            return base64.b64decode(im.uri.split(",", 1)[1])
        if im.uri:
            return (path.parent / im.uri).read_bytes()
    except (OSError, ValueError):
        return None
    return None


def _uv_overlap(tris_uv: np.ndarray, res: int = 256, cap: int = 60_000) -> tuple[float, float]:
    """(fraction of covered texels covered more than once, fraction of the 0-1 square covered)."""
    if len(tris_uv) == 0:
        return 0.0, 0.0
    if len(tris_uv) > cap:
        tris_uv = tris_uv[np.random.default_rng(0).choice(len(tris_uv), cap, replace=False)]
    count = np.zeros((res, res), np.int32)
    p = np.mod(tris_uv, 1.0) * res if False else np.clip(tris_uv, 0, 1) * res
    for t in p:
        lo = np.floor(t.min(0)).astype(int)
        hi = np.ceil(t.max(0)).astype(int)
        lo, hi = np.clip(lo, 0, res - 1), np.clip(hi, 0, res)
        if (hi <= lo).any():
            continue
        xs, ys = np.meshgrid(np.arange(lo[0], hi[0]) + 0.5, np.arange(lo[1], hi[1]) + 0.5)
        (x0, y0), (x1, y1), (x2, y2) = t
        d = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
        if abs(d) < 1e-12:
            continue
        a = ((y1 - y2) * (xs - x2) + (x2 - x1) * (ys - y2)) / d
        b = ((y2 - y0) * (xs - x2) + (x0 - x2) * (ys - y2)) / d
        inside = (a >= 0) & (b >= 0) & (a + b <= 1)
        count[lo[1]:hi[1], lo[0]:hi[0]] += inside
    covered = (count > 0).sum()
    return (float((count > 1).sum() / covered) if covered else 0.0), float(covered / res / res)


def _texture_stats(data: bytes, role: str) -> dict[str, Any]:
    from PIL import Image

    try:
        im = Image.open(io.BytesIO(data))
        w, h = im.size
        mode = im.mode
        im = im.convert("RGBA")
        im.thumbnail((512, 512))
        a = np.asarray(im).astype(np.float64) / 255.0
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"[:200]}
    pot = lambda v: v > 0 and (v & (v - 1)) == 0  # noqa: E731
    out: dict[str, Any] = {"width": w, "height": h, "mode": mode, "power_of_two": pot(w) and pot(h), "bytes": len(data)}
    rgb = a[..., :3]
    if role == "baseColor":
        lum = (rgb @ np.array([0.2126, 0.7152, 0.0722])) * 255
        out["srgb_luminance_p2_p50_p98"] = [round(float(np.percentile(lum, q)), 1) for q in (2, 50, 98)]
        out["frac_below_30_srgb"] = round(float((lum < 30).mean()), 3)
        out["frac_above_240_srgb"] = round(float((lum > 240).mean()), 3)
        out["flat"] = bool(rgb.std(axis=(0, 1)).max() < 0.01)
    elif role == "metallicRoughness":
        g_, b_ = rgb[..., 1], rgb[..., 2]
        out["roughness_p2_p50_p98"] = [round(float(np.percentile(g_, q)), 3) for q in (2, 50, 98)]
        out["metal_frac_in_between"] = round(float(((b_ > 0.1) & (b_ < 0.9)).mean()), 3)
        out["metal_mean"] = round(float(b_.mean()), 3)
    elif role == "normal":
        out["blue_mean"] = round(float(rgb[..., 2].mean()), 3)
        out["looks_tangent_space"] = bool(rgb[..., 2].mean() > 0.6)
        out["flat"] = bool(rgb.std(axis=(0, 1)).max() < 0.01)
    elif role == "occlusion":
        out["ao_p2_p50_p98"] = [round(float(np.percentile(rgb[..., 0], q)), 3) for q in (2, 50, 98)]
    return out


def glb_facts(path: Path, *, size_m: tuple[float, float] | None = None, upright: bool | None = None,
              max_tris: int | None = None) -> dict[str, Any]:
    """Geometry, UV, material and texture facts read from a GLB (glTF: +Y up, metres)."""
    g, bufs = _load(path)
    tris = 0
    draw_calls = 0
    pts: list[np.ndarray] = []
    prims_total = prims_uv = prims_norm = prims_tan = 0
    uv_out = uv_n = 0
    degenerate = 0
    tri_uv_by_mat: dict[int, list[np.ndarray]] = {}
    col_nodes, lod_nodes = [], []
    for mesh_i, m, name in _instances(g):
        low = name.lower()
        is_col = low.endswith(COL_SUFFIX) or low.startswith("ucx_") or "collision" in low or "collider" in low
        if is_col:
            col_nodes.append(name)
            continue
        if "lod" in low and any(f"lod{k}" in low.replace("_", "") for k in range(1, 8)):
            lod_nodes.append(name)
            continue
        for p in g.meshes[mesh_i].primitives:
            mode = 4 if p.mode is None else p.mode
            pos = accessor(g, bufs, p.attributes.POSITION).astype(np.float64)
            idx = accessor(g, bufs, p.indices).reshape(-1).astype(np.int64) if p.indices is not None else np.arange(len(pos))
            if mode == 4:
                faces = idx[: len(idx) // 3 * 3].reshape(-1, 3)
            elif mode == 5:
                faces = np.array([[idx[i], idx[i + 1], idx[i + 2]] for i in range(len(idx) - 2)]).reshape(-1, 3)
            elif mode == 6:
                faces = np.array([[idx[0], idx[i], idx[i + 1]] for i in range(1, len(idx) - 1)]).reshape(-1, 3)
            else:
                continue
            draw_calls += 1
            prims_total += 1
            tris += len(faces)
            w = (m @ np.c_[pos, np.ones(len(pos))].T).T[:, :3]
            pts.append(w)
            if len(faces):
                e1, e2 = w[faces[:, 1]] - w[faces[:, 0]], w[faces[:, 2]] - w[faces[:, 0]]
                degenerate += int((np.linalg.norm(np.cross(e1, e2), axis=1) < 1e-12).sum())
            if p.attributes.NORMAL is not None:
                prims_norm += 1
            if p.attributes.TANGENT is not None:
                prims_tan += 1
            if p.attributes.TEXCOORD_0 is not None:
                prims_uv += 1
                uv = accessor(g, bufs, p.attributes.TEXCOORD_0).astype(np.float64)
                uv_n += len(uv)
                uv_out += int(((uv < -1e-4) | (uv > 1 + 1e-4)).any(axis=1).sum())
                if len(faces):
                    tri_uv_by_mat.setdefault(p.material if p.material is not None else -1, []).append(uv[faces])
    P = np.concatenate(pts) if pts else np.zeros((0, 3))
    facts: dict[str, Any] = {"file_bytes": path.stat().st_size, "triangles": int(tris), "draw_calls": draw_calls,
                             "mesh_instances": len(_instances(g)), "materials": len(g.materials),
                             "extensions_used": list(g.extensionsUsed or []), "extensions_required": list(g.extensionsRequired or []),
                             "degenerate_triangles": degenerate}
    if len(P):
        lo, hi = P.min(0), P.max(0)
        size = hi - lo
        facts["bounds_m"] = [lo.round(4).tolist(), hi.round(4).tolist()]
        facts["size_m_xyz"] = size.round(4).tolist()
        facts["largest_dimension_m"] = round(float(size.max()), 4)
        facts["grounded"] = bool(abs(lo[1]) <= 0.02)
        facts["min_y_m"] = round(float(lo[1]), 4)
        centre = (lo + hi) / 2
        facts["centred_xz"] = bool(abs(centre[0]) <= 0.1 * size.max() + 1e-6 and abs(centre[2]) <= 0.1 * size.max() + 1e-6)
        if size_m:
            facts["scale_plausible"] = bool(size_m[0] <= size.max() <= size_m[1])
        if upright is not None:
            facts["upright"] = bool(size[1] >= 0.8 * max(size[0], size[2])) if upright else None
    if max_tris:
        facts["within_tri_budget"] = tris <= max_tris
    facts["uv"] = {"primitives_with_uv": prims_uv, "primitives": prims_total,
                   "verts_outside_0_1_frac": round(uv_out / uv_n, 4) if uv_n else None}
    overlaps = {}
    for mat, lst in tri_uv_by_mat.items():
        ov, cov = _uv_overlap(np.concatenate(lst))
        overlaps[str(mat)] = {"overlap_frac": round(ov, 4), "coverage_frac": round(cov, 4)}
    facts["uv"]["by_material"] = overlaps
    facts["normals"] = {"primitives_with_normals": prims_norm, "primitives_with_tangents": prims_tan, "primitives": prims_total}
    mats = []
    roles_of_image: dict[int, set[str]] = {}
    for mi, mat in enumerate(g.materials):
        pbr = mat.pbrMetallicRoughness
        rec = {"name": mat.name, "alpha_mode": mat.alphaMode, "double_sided": bool(mat.doubleSided),
               "base_color_factor": list(pbr.baseColorFactor) if pbr and pbr.baseColorFactor else None,
               "metallic_factor": pbr.metallicFactor if pbr else None, "roughness_factor": pbr.roughnessFactor if pbr else None,
               "textures": {}}
        slots = {"baseColor": pbr.baseColorTexture if pbr else None, "metallicRoughness": pbr.metallicRoughnessTexture if pbr else None,
                 "normal": mat.normalTexture, "occlusion": mat.occlusionTexture, "emissive": mat.emissiveTexture}
        for role, ti in slots.items():
            if ti is None or ti.index is None or ti.index >= len(g.textures):
                continue
            src = g.textures[ti.index].source
            rec["textures"][role] = src
            if src is not None:
                roles_of_image.setdefault(src, set()).add(role)
        mats.append(rec)
    facts["material_list"] = mats
    facts["pbr_complete"] = bool(mats) and all({"baseColor", "normal"} <= set(m["textures"]) and
                                               ("metallicRoughness" in m["textures"]) for m in mats)
    imgs = []
    for ii in range(len(g.images)):
        data = _image_bytes(g, bufs, path, ii)
        role = sorted(roles_of_image.get(ii, {"unused"}))[0]
        stats = _texture_stats(data, role) if data else {"error": "image not readable"}
        imgs.append({"index": ii, "roles": sorted(roles_of_image.get(ii, set())), "mime": g.images[ii].mimeType,
                     "external": bool(g.images[ii].uri and not g.images[ii].uri.startswith("data:")), **stats})
    facts["images"] = imgs
    facts["texture_count"] = len(imgs)
    facts["max_texture_px"] = max((i.get("width", 0) for i in imgs), default=0)
    facts["collision_nodes_in_main"] = col_nodes
    facts["lod_nodes_in_main"] = lod_nodes
    return facts


def sidecar_files(glb: Path) -> dict[str, list[str]]:
    """LOD and collision files delivered beside the main GLB."""
    d = glb.parent
    lods = sorted(p.name for p in d.glob(f"{glb.stem}_LOD*.glb")) + sorted(p.name for p in d.glob("*_LOD[0-9].glb") if not p.name.startswith(glb.stem))
    cols = sorted(p.name for p in d.glob("*.glb") if p != glb and any(s in p.stem.lower() for s in ("collision", "collider", "-col", "_col")))
    return {"lod_files": sorted(set(lods)), "collision_files": cols}


def bare_copy(glb: Path, dest: Path, name: str) -> Path:
    """The GLB and only the files its glTF references, nothing else (no manifest, no USD)."""
    g, _ = _load(glb)
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / f"{name}.glb"
    shutil.copyfile(glb, out)
    for uri in [b.uri for b in g.buffers] + [i.uri for i in g.images]:
        if uri and not uri.startswith("data:") and "://" not in uri:
            src = glb.parent / uri
            if src.is_file():
                (dest / uri).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dest / uri)
    return out


# ----------------------------------------------------------------------------------------------- engine checks


async def godot_check(glb: Path) -> dict[str, Any]:
    from antigravity3d.adapters.engines.validators import GodotValidator

    v = GodotValidator()
    r = await v.validate(glb, timeout=180)
    return {"ok": r.ok, "skipped": r.skipped_reason, "metrics": r.metrics, "bounds_m": r.bounds_m,
            "log_tail": (r.log_tail or "")[-1500:]}


async def three_check(glb: Path) -> dict[str, Any]:
    from antigravity3d.adapters.engines.web_three import WebThreeValidator

    v = WebThreeValidator()
    try:
        r = await v.validate(glb, timeout=180, out_dir=glb.parent / "three")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:500]}
    checks = [{"name": c.name, "status": c.status, "detail": (c.detail or "")[:200]} for c in getattr(r, "checks", ())]
    return {"ok": r.ok, "skipped": r.skipped_reason, "checks": checks,
            "failed": [c["name"] for c in checks if c["status"] == "fail"],
            "not_applicable": [c["name"] for c in checks if c["status"] == "skipped"], "log_tail": (r.log_tail or "")[-800:]}


async def upstream_checks(pool: Any, glb: Path) -> dict[str, Any]:
    """The upstream qa_check, mesh_stats and texel_density scripts on the GLB, through the project's worker."""
    v = await pool.call("version")
    if "primitives_source" not in v:
        return {"available": False, "reason": "this tree's Blender worker has no glTF import for inspect_usd and no "
                                              "texel_density (another engineer's change, not merged here)", "worker_ops": v.get("ops")}
    out: dict[str, Any] = {"available": True, "source": v.get("primitives_source")}
    # qa_check on welded copies: a GLB splits vertices along every UV seam, which would read as open edges in every
    # arm's file alike; mesh_stats and texel_density on the file as shipped.
    for name, kwargs, weld in (("qa_check", {}, True), ("mesh_stats", {}, False), ("texel_density", {"res": 2048}, False)):
        if name not in (v.get("primitives") or []):
            out[name] = {"error": "primitive not loaded", "loaded": v.get("primitives"), "load_error": v.get("primitives_error")}
            continue
        try:
            out[name] = await pool.call("inspect_usd", path=str(glb), primitive=name, kwargs=kwargs, weld=weld, _timeout=300)
        except Exception as e:  # noqa: BLE001
            out[name] = {"error": f"{type(e).__name__}: {e}"[:400]}
    return out


HARNESS_EXPORT_SRC = '''
import bpy
before = set(bpy.data.objects)
with bpy.data.libraries.load(SOURCE, link=False) as (data_from, data_to):
    data_to.objects = list(data_from.objects)
for ob in data_to.objects:
    if ob is not None and ob.type == "MESH":
        bpy.context.scene.collection.objects.link(ob)
meshes = [o.name for o in bpy.data.objects if o not in before and o.type == "MESH"]
bpy.ops.export_scene.gltf(filepath=TARGET, export_format="GLB", use_selection=False)
result = {"meshes": meshes}
'''


async def harness_export_blend(pool: Any, blend: Path, out: Path) -> dict[str, Any]:
    """The mesh objects of an agent's .blend, appended into an empty scene in this project's worker and exported as
    GLB, so an undelivered cell can still be measured for geometry. Labelled wherever it is shown."""
    out.parent.mkdir(parents=True, exist_ok=True)
    await pool.call("reset_scene")
    src = HARNESS_EXPORT_SRC.replace("SOURCE", repr(str(blend))).replace("TARGET", repr(str(out)))
    res = await pool.call("run_script", source=src, _timeout=300)
    if not out.is_file():
        raise RuntimeError(f"no GLB written from {blend}: {res}")
    return {"source": blend, "glb": out, "result": res}


# ----------------------------------------------------------------------------------------------- renders


IMPORT_SRC = '''
import bpy
from mathutils import Vector
before = set(bpy.data.objects)
bpy.ops.import_scene.gltf(filepath=PATH)
new = [o for o in bpy.data.objects if o not in before]
for o in list(new):
    if o.type in {"CAMERA", "LIGHT"}:
        bpy.data.objects.remove(o, do_unlink=True)
col_suffix = ("-col", "-colonly", "-convcol", "_col", "_collision", "_collider", "_colliders")
hidden = []
for o in bpy.data.objects:
    low = o.name.lower()
    if low.endswith(col_suffix) or low.startswith("ucx_") or "collision" in low or "collider" in low:
        o.hide_render = True
        hidden.append(o.name)
bpy.context.view_layer.update()
pts = [o.matrix_world @ Vector(c) for o in bpy.data.objects if o.type == "MESH" and not o.hide_render for c in o.bound_box]
if not pts:
    pts = [Vector((-0.5, -0.5, 0)), Vector((0.5, 0.5, 1))]
lo = [min(p[i] for p in pts) for i in range(3)]
hi = [max(p[i] for p in pts) for i in range(3)]
scene = bpy.context.scene
world = bpy.data.worlds.new("pilot_world")
scene.world = world
try:
    world.use_nodes = True
except Exception:
    pass
bg = world.node_tree.nodes.get("Background") if world.node_tree else None
if bg is not None:
    bg.inputs[0].default_value = (0.22, 0.22, 0.24, 1.0)
    bg.inputs[1].default_value = 1.0
for name, rot, energy in (("pilot_key", (0.9, 0.0, -0.6), 3.0), ("pilot_fill", (1.1, 0.0, 2.2), 1.0), ("pilot_rim", (0.5, 0.0, 3.4), 2.0)):
    ld = bpy.data.lights.new(name, "SUN")
    ld.energy = energy
    ld.angle = 0.05
    lo_ = bpy.data.objects.new(name, ld)
    scene.collection.objects.link(lo_)
    lo_.rotation_euler = rot
bpy.ops.mesh.primitive_plane_add(size=200.0, location=(0, 0, 0))
ground = bpy.context.active_object
ground.name = "pilot_ground"
gm = bpy.data.materials.new("pilot_ground_mat")
gm.diffuse_color = (0.45, 0.45, 0.45, 1.0)
try:
    gm.use_nodes = True
    gm.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.45, 0.45, 0.45, 1.0)
    gm.node_tree.nodes["Principled BSDF"].inputs["Roughness"].default_value = 0.9
except Exception:
    pass
ground.data.materials.append(gm)
bpy.ops.mesh.primitive_cube_add(size=1.0)
ref = bpy.context.active_object
ref.name = "pilot_reference_1m80"
ref.scale = (0.3, 0.3, 1.8)
ref.location = (hi[0] + 0.45, (lo[1] + hi[1]) / 2, 0.9)
rm = bpy.data.materials.new("pilot_ref_mat")
try:
    rm.use_nodes = True
    rm.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.1, 0.35, 0.8, 1.0)
except Exception:
    pass
ref.data.materials.append(rm)
ref.hide_render = True
cd = bpy.data.cameras.new("pilot_cam")
cam = bpy.data.objects.new("pilot_cam", cd)
scene.collection.objects.link(cam)
scene.camera = cam
try:
    scene.view_settings.view_transform = "AgX"
except Exception:
    pass
result = {"lo": lo, "hi": hi, "hidden_collision": hidden, "objects": len(new)}
'''

VIEW_SRC = '''
import bpy, math
from mathutils import Vector
scene = bpy.context.scene
cam = bpy.data.objects["pilot_cam"]
ref = bpy.data.objects["pilot_reference_1m80"]
lo, hi = Vector(LO), Vector(HI)
az, el = math.radians(AZ), math.radians(EL)
d = Vector((math.sin(az) * math.cos(el), -math.cos(az) * math.cos(el), math.sin(el)))
if SCALE_VIEW:
    ref.hide_render = False
    left = min(lo.x, 0.0) - 0.3
    right = max(hi.x, 0.0) + 0.45 + 0.15 + 0.3
    top = max(hi.z, 1.8) + 0.2
    cam.data.type = "ORTHO"
    cam.data.ortho_scale = max(right - left, top + 0.2)
    target = Vector(((left + right) / 2, (lo.y + hi.y) / 2, top / 2))
    dist = 20.0
else:
    ref.hide_render = True
    cam.data.type = "PERSP"
    cam.data.lens = 50.0
    target = (lo + hi) / 2
    radius = max((hi - lo).length / 2, 0.01)
    fov = 2 * math.atan(cam.data.sensor_width / (2 * cam.data.lens))
    dist = radius / math.sin(fov / 2) * 1.08
cam.location = target + d * dist
cam.rotation_euler = (-d).to_track_quat("-Z", "Y").to_euler()
cam.data.clip_start = 0.01
cam.data.clip_end = dist * 4 + 50
result = {"view": NAME}
'''


async def render_views(pool: Any, glb: Path, out_dir: Path) -> dict[str, Any]:
    from antigravity3d.adapters.engines.validators import blender_frame_png

    out_dir.mkdir(parents=True, exist_ok=True)
    await pool.call("reset_scene")
    info = await pool.call("run_script", source=IMPORT_SRC.replace("PATH", repr(str(glb))), _timeout=300)
    meta = info.get("result") or {}
    pngs = []
    for name, az, el in VIEWS:
        src = (VIEW_SRC.replace("LO", repr(meta.get("lo", [-0.5, -0.5, 0]))).replace("HI", repr(meta.get("hi", [0.5, 0.5, 1])))
               .replace("AZ", str(az)).replace("EL", str(el)).replace("SCALE_VIEW", str(name == "scale")).replace("NAME", repr(name)))
        await pool.call("run_script", source=src, _timeout=120)
        r = await pool.call("render", **RENDER, _timeout=900)
        p = out_dir / f"{name}.png"
        p.write_bytes(blender_frame_png(r["image"]))
        pngs.append(str(p))
    return {"views": pngs, "import": meta, "settings": RENDER}


def contact_sheet(pngs: list[str], out: Path, tile: int = 512, cols: int = 3, missing_label: str | None = None) -> Path:
    from PIL import Image, ImageDraw

    rows = math.ceil(len(VIEWS) / cols)
    sheet = Image.new("RGB", (tile * cols, tile * rows), (40, 40, 44))
    draw = ImageDraw.Draw(sheet)
    if missing_label:
        draw.text((40, tile * rows // 2), missing_label, fill=(230, 230, 230))
    for i, p in enumerate(pngs):
        im = Image.open(p).convert("RGB").resize((tile, tile))
        x, y = (i % cols) * tile, (i // cols) * tile
        sheet.paste(im, (x, y))
        draw.text((x + 8, y + 6), Path(p).stem.replace("_", " "), fill=(255, 255, 255))
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    return out


# ----------------------------------------------------------------------------------------------- evaluate all


async def evaluate_all(root: Path, records: list[Any], briefs: dict[str, Any], round_: str = "") -> dict[str, Any]:
    from antigravity3d.adapters.blender_5_3.pool import BlenderPool

    results: dict[str, Any] = {}
    pool = None
    import os

    if os.environ.get("BLENDER_BIN"):
        pool = BlenderPool(1, os.environ["BLENDER_BIN"], timeout=900)
        await pool.start()
    try:
        for rec in records:
            key = f"{rec.brief}:{rec.arm}"
            b = briefs[rec.brief]
            d = root / "eval" / round_ / rec.brief / rec.arm if round_ else root / "eval" / rec.brief / rec.arm
            digest = hashlib.sha256(Path(rec.glb).read_bytes()).hexdigest() if rec.glb and Path(rec.glb).is_file() else None
            cached = d / "evaluation.json"
            if cached.exists():  # an arm added later does not re-evaluate the others
                prev = json.loads(cached.read_text())
                if prev.get("glb_sha256") == digest and (prev.get("renders") or pool is None or not digest):
                    results[key] = prev
                    continue
            if d.exists():
                shutil.rmtree(d)
            d.mkdir(parents=True)
            r: dict[str, Any] = {"glb_exists": bool(digest), "glb_sha256": digest, "source_glb": rec.glb}
            results[key] = r
            if not r["glb_exists"]:
                r["sheet"] = str(contact_sheet([], d / "sheet.png", missing_label="no GLB delivered"))
                ws = Path((getattr(rec, "pipeline", None) or {}).get("workspace") or "")
                blends = sorted(p for p in ws.rglob("*.blend") if ".blend" == p.suffix) if ws.is_dir() else []
                if blends and pool is not None:  # what exists: the agent's own scene, geometry only, exported by us
                    try:
                        out = await harness_export_blend(pool, max(blends, key=lambda p: p.stat().st_mtime), d / "harness_export" / "asset.glb")
                        r["harness_export"] = {"source": str(out["source"]), "glb": str(out["glb"]),
                                               "label": "exported by the harness from the agent's .blend, not delivered by the agent; geometry only",
                                               "facts": glb_facts(out["glb"], size_m=b.size_m, upright=b.upright, max_tris=b.max_tris)}
                    except Exception as e:  # noqa: BLE001
                        r["harness_export"] = {"error": f"{type(e).__name__}: {e}"[:400]}
                cached.write_text(json.dumps(r, indent=1, default=str))
                continue
            src = Path(rec.glb)
            r["sidecars"] = sidecar_files(src)
            try:
                bare = bare_copy(src, d / "bare", "asset")
                r["facts"] = glb_facts(bare, size_m=b.size_m, upright=b.upright, max_tris=b.max_tris)
                r["usable"] = r["facts"]["triangles"] > 0
                # The three.js page needs bounds to place its camera; without a USD it takes them from a manifest. This
                # one holds only the bounds read from the GLB itself, so every arm gets the same manifest-free subset.
                if r["facts"].get("bounds_m"):
                    (bare.parent / "manifest.json").write_text(json.dumps({"glb_bounds_m": r["facts"]["bounds_m"]}))
            except Exception as e:  # noqa: BLE001
                r["usable"] = False
                r["facts_error"] = f"{type(e).__name__}: {e}"[:500]
                r["sheet"] = str(contact_sheet([], d / "sheet.png", missing_label="GLB not readable"))
                continue
            r["godot"] = await godot_check(bare)
            r["three"] = await three_check(bare)
            if pool is not None:
                try:
                    r["upstream"] = await upstream_checks(pool, bare)
                except Exception as e:  # noqa: BLE001
                    r["upstream"] = {"available": False, "reason": f"{type(e).__name__}: {e}"[:300]}
                try:
                    r["renders"] = await render_views(pool, bare, d / "renders")
                    r["sheet"] = str(contact_sheet(r["renders"]["views"], d / "sheet.png"))
                except Exception as e:  # noqa: BLE001
                    r["render_error"] = f"{type(e).__name__}: {e}"[:600]
                    r["sheet"] = str(contact_sheet([], d / "sheet.png", missing_label="render failed"))
            else:
                r["upstream"] = {"available": False, "reason": "no BLENDER_BIN"}
                r["sheet"] = str(contact_sheet([], d / "sheet.png", missing_label="no Blender: not rendered"))
            (d / "evaluation.json").write_text(json.dumps(r, indent=1, default=str))
    finally:
        if pool is not None:
            await pool.stop()
    (root / (f"evaluation_{round_}.json" if round_ else "evaluation.json")).write_text(json.dumps(results, indent=1, default=str))
    return results


# ----------------------------------------------------------------------------------------------- judge


RUBRIC = """You are reviewing two 3D props made for the same game-asset brief. Each image is a contact sheet of one prop, rendered with identical cameras, lights and ground (top-left tile: an orthographic scale view next to a blue 1.8 m tall reference block standing on the ground; other tiles: perspective views framed on the prop).

Brief: {brief}

Judge only what is visible. For each criterion answer "1" (first image better), "2" (second image better) or "tie", with one short sentence:
- reads_as_brief: which reads more clearly as the brief?
- proportion_scale: which has more believable proportions and real-world scale (use the 1.8 m block)?
- surface_material: which has better surface and material quality for a game asset (texture detail, material response, colour)?
- defects: which has fewer visible defects (holes, floating or sunken parts, broken shading, stretched or missing textures, intersections)?
Then give an overall winner ("1" or "2") with one sentence."""

JUDGE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "criteria": {"type": "OBJECT", "properties": {c: {"type": "OBJECT", "properties": {
            "winner": {"type": "STRING", "enum": ["1", "2", "tie"]}, "reason": {"type": "STRING"}},
            "required": ["winner", "reason"]} for c in CRITERIA}, "required": list(CRITERIA)},
        "overall": {"type": "OBJECT", "properties": {"winner": {"type": "STRING", "enum": ["1", "2"]},
                                                     "reason": {"type": "STRING"}}, "required": ["winner", "reason"]},
    },
    "required": ["criteria", "overall"],
}


def judge_plan(briefs: list[str], arms: list[str] | None = None, seed: int = 20261121) -> list[dict[str, Any]]:
    """Every pair of arms, per brief, in both orders; call order shuffled with a recorded seed."""
    arms = sorted(arms or ["A", "B", "C"])
    pairs = [(x, y) for i, x in enumerate(arms) for y in arms[i + 1:]]
    calls = [{"brief": b, "first": x, "second": y} for b in briefs for p in pairs for x, y in (p, p[::-1])]
    random.Random(seed).shuffle(calls)
    return calls


def aggregate(calls: list[dict[str, Any]]) -> dict[str, Any]:
    """Per brief, pair and criterion: the arm both orders agree on, 'tie', or 'split' (the orders disagree)."""
    out: dict[str, Any] = {}
    for c in calls:
        if not c.get("verdict"):
            continue
        key = (c["brief"], "-".join(sorted((c["first"], c["second"]))))
        slot = out.setdefault(f"{key[0]}:{key[1]}", {"votes": {k: [] for k in (*CRITERIA, "overall")}})
        v = c["verdict"]
        for k in (*CRITERIA, "overall"):
            w = (v["criteria"][k] if k != "overall" else v["overall"])["winner"]
            slot["votes"][k].append(c["first"] if w == "1" else c["second"] if w == "2" else "tie")
    for slot in out.values():
        slot["result"] = {}
        for k, votes in slot["votes"].items():
            if len(votes) < 2:
                slot["result"][k] = f"one order only: {votes[0]}" if votes else "not judged"
            elif votes[0] == votes[1]:
                slot["result"][k] = votes[0]
            else:
                slot["result"][k] = "split"
    return out


def judge_all(root: Path, records: list[Any], briefs: dict[str, Any], relay: Any, per_call_tokens: int, model: str,
              thinking: str, max_output: int, round_: str = "") -> dict[str, Any]:
    """Judge every pair of arms that exist for a brief. Each pair has its own ledger account (two calls' worth), and
    each finished call is kept on disk, so adding an arm later judges only the new pairs."""
    from google import genai
    from google.genai import types

    ev = json.loads((root / (f"evaluation_{round_}.json" if round_ else "evaluation.json")).read_text())
    jdir = root / "judge" / round_ if round_ else root / "judge"
    (jdir / "calls").mkdir(parents=True, exist_ok=True)
    by_brief: dict[str, list[str]] = {}
    for r in records:
        by_brief.setdefault(r.brief, []).append(r.arm)
    every_arm = sorted({a for arms in by_brief.values() for a in arms})
    plan = [c for c in judge_plan(sorted(by_brief), every_arm)  # one seeded order over everything that exists
            if c["first"] in by_brief[c["brief"]] and c["second"] in by_brief[c["brief"]]]
    clients: dict[str, Any] = {}
    done: list[dict[str, Any]] = []
    for c in plan:
        f = jdir / "calls" / f"{c['brief']}_{c['first']}_{c['second']}.json"
        if f.exists():
            prev = json.loads(f.read_text())
            # A call written by the frozen A/B harness has no "sheets": its two files are A and B runs, which never re-run.
            if prev.get("verdict") and "sheets" not in prev or prev.get("verdict") and prev.get("sheets") == [ev.get(f"{c['brief']}:{c['first']}", {}).get("glb_sha256"),
                                                             ev.get(f"{c['brief']}:{c['second']}", {}).get("glb_sha256")]:
                done.append(prev)
                continue
        e1, e2 = ev.get(f"{c['brief']}:{c['first']}", {}), ev.get(f"{c['brief']}:{c['second']}", {})
        rec = dict(c)
        if not e1.get("usable") or not e2.get("usable") or not e1.get("renders") or not e2.get("renders"):
            lost = [a for a, e in ((c["first"], e1), (c["second"], e2)) if not (e.get("usable") and e.get("renders"))]
            rec["walkover"] = f"no rendered output from {', '.join(lost)}: not sent to the judge"
        else:
            pair = "-".join(sorted((c["first"], c["second"])))
            acct = f"{round_ + ':' if round_ else ''}judge:{c['brief']}:{pair}"
            if acct not in clients:
                acc = relay.open(acct, 2 * per_call_tokens, 0, jdir / acct.replace(":", "_"), sandboxed=False)
                clients[acct] = genai.Client(api_key="via-relay", http_options=types.HttpOptions(
                    base_url=acc.endpoint, retry_options=types.HttpRetryOptions(attempts=1), timeout=600_000))
            rec["sheets"] = [e1.get("glb_sha256"), e2.get("glb_sha256")]  # a verdict is reused only for the same two files
            imgs = [_sheet_for_judge(Path(e1["sheet"])), _sheet_for_judge(Path(e2["sheet"]))]
            contents = [types.Part.from_text(text=RUBRIC.format(brief=briefs[c["brief"]].prompt)), types.Part.from_text(text="Image 1:"),
                        types.Part.from_bytes(data=imgs[0], mime_type="image/png"), types.Part.from_text(text="Image 2:"),
                        types.Part.from_bytes(data=imgs[1], mime_type="image/png")]
            cfg = types.GenerateContentConfig(response_mime_type="application/json", response_schema=JUDGE_SCHEMA,
                                              max_output_tokens=max_output,
                                              thinking_config=types.ThinkingConfig(thinking_level=thinking.upper()))
            try:
                resp = clients[acct].models.generate_content(model=model, contents=contents, config=cfg)
                rec["verdict"] = json.loads(resp.text)
            except Exception as e:  # noqa: BLE001 - a refused or failed call is a recorded outcome (and retried on a rerun)
                rec["error"] = f"{type(e).__name__}: {e}"[:400]
        f.write_text(json.dumps(rec, indent=1))
        done.append(rec)
    for acct in clients:
        relay.close(acct)
    accounts = {k: v for k, v in relay.ledger.data["accounts"].items() if "judge:" in k}
    res = {"model": model, "thinking": thinking, "calls": done, "aggregate": aggregate(done), "accounts": accounts}
    (jdir / "judge.json").write_text(json.dumps(res, indent=1))
    return res


def _sheet_for_judge(p: Path, size: int = 1024) -> bytes:
    from PIL import Image

    im = Image.open(p).convert("RGB")
    im.thumbnail((size, size))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


# ----------------------------------------------------------------------------------------------- gallery


def _fmt(v: Any) -> str:
    if v is None:
        return "–"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:.3g}"
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, list):
        return ", ".join(_fmt(x) for x in v)
    return str(v)


def _row_facts(e: dict[str, Any], rec: dict[str, Any]) -> list[tuple[str, str]]:
    f = e.get("facts") or {}
    gd, th, up = e.get("godot") or {}, e.get("three") or {}, e.get("upstream") or {}
    uvs = (f.get("uv") or {}).get("by_material") or {}
    worst_overlap = max((v["overlap_frac"] for v in uvs.values()), default=None)
    tex = f.get("images") or []
    rows = [
        ("Outcome", rec.get("status")), ("GLB delivered", e.get("glb_exists")), ("Godot 4.7 import", gd.get("ok") if gd else None),
        ("three.js check", ("pass" if th.get("ok") else f"fail: {', '.join(th.get('failed') or [])[:160]}") if th else None),
        ("Triangles", f.get("triangles")), ("Within triangle budget", f.get("within_tri_budget")), ("Draw calls", f.get("draw_calls")),
        ("Size x·y·z (m, Y up)", f.get("size_m_xyz")), ("Scale plausible", f.get("scale_plausible")), ("On the ground", f.get("grounded")),
        ("Upright", f.get("upright")), ("Primitives with UVs", f"{(f.get('uv') or {}).get('primitives_with_uv')}/{(f.get('uv') or {}).get('primitives')}" if f else None),
        ("Worst UV overlap", worst_overlap), ("UV verts outside 0-1", (f.get("uv") or {}).get("verts_outside_0_1_frac")),
        ("Normals", f"{(f.get('normals') or {}).get('primitives_with_normals')}/{(f.get('normals') or {}).get('primitives')}" if f else None),
        ("Materials", f.get("materials")), ("PBR set complete (base, normal, MR)", f.get("pbr_complete")),
        ("Textures", f"{len(tex)}: " + ", ".join(f"{t.get('width')}x{t.get('height')} {'/'.join(t.get('roles') or ['unused'])}" for t in tex) if tex else "0"),
        ("Base colour texture (sRGB luminance p2/p50/p98; flat?)", "; ".join(
            f"{t.get('srgb_luminance_p2_p50_p98')}{' flat' if t.get('flat') else ''}" for t in tex if "baseColor" in (t.get("roles") or [])) or None),
        ("LOD files", _fmt((e.get("sidecars") or {}).get("lod_files"))), ("Collision", _fmt(((e.get("sidecars") or {}).get("collision_files") or []) + (f.get("collision_nodes_in_main") or []))),
        ("Upstream qa_check", ("pass" if (up.get("qa_check") or {}).get("pass") else "fail") if up.get("available") else f"not run: {up.get('reason', '')[:80]}"),
        ("Tokens (in / out / thinking)", f"{_fmt((rec.get('tokens') or {}).get('input_tokens'))} / {_fmt((rec.get('tokens') or {}).get('output_tokens'))} / {_fmt((rec.get('tokens') or {}).get('thinking_tokens'))}"),
        ("Input tokens served from cache", (rec.get("tokens") or {}).get("cached_tokens")),
        ("Images generated", rec.get("images")), ("Model calls", rec.get("model_calls")), ("Tool calls", rec.get("tool_calls")),
        ("Blender invocations", rec.get("blender_invocations")), ("Wall time (s)", rec.get("wall_s")),
    ]
    if rec.get("arm") == "C":
        p = rec.get("pipeline") or {}
        rows.append(("Pipeline says", f"publication: {p.get('publication')}; preview gates: {p.get('preview_gates')}; judge backend: {p.get('decision_backend')}"))
        t_ = rec.get("tokens") or {}
        rows.append(("Spend, the pipeline's own line", p.get("own_spend") or "–"))
        rows.append(("Spend, the relay's count", f"{_fmt(t_.get('total'))} tokens ({_fmt(t_.get('input_tokens'))} in / "
                     f"{_fmt(t_.get('output_tokens'))} out / {_fmt(t_.get('thinking_tokens'))} thinking); {_fmt(rec.get('images'))} images; "
                     f"{_fmt(rec.get('model_calls'))} model calls"))
    return [(k, _fmt(v)) for k, v in rows]


def write_gallery(root: Path, results: dict[str, Any], briefs: dict[str, Any], labels: dict[str, str],
                  name: str = "gallery.html") -> Path:
    ev = results.get("evaluation") or {}
    recs = {f"{r['brief']}:{r['arm']}": r for r in results.get("records", [])}
    agg = (results.get("judge") or {}).get("aggregate") or {}
    t = results.get("tally") or {}
    parts = ["<!doctype html><html><head><meta charset='utf-8'><title>Pilot: three ways to a prop</title><style>",
             "body{font:14px/1.45 system-ui,sans-serif;margin:24px;background:#16171a;color:#e6e6e6}",
             "h1,h2{font-weight:600}.warn{background:#3a2d12;border-left:4px solid #e0a020;padding:10px 14px;margin:12px 0}",
             ".grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}.card{background:#202227;padding:10px;border-radius:6px}",
             "img{width:100%;border-radius:4px}table{border-collapse:collapse;width:100%;font-size:12.5px}",
             "td{border-top:1px solid #33363d;padding:3px 4px;vertical-align:top}td:first-child{color:#a8acb5;width:44%}",
             "code{background:#2a2d33;padding:1px 4px;border-radius:3px}</style></head><body>",
             "<h1>Pilot: the same prop brief, three ways</h1>",
             f"<div class='warn'><b>Pilot, {results.get('mode')}.</b> One run per arm per brief: these are {len(results.get('records') or [])} observations. "
             "Nothing here is a rate or a ranking that would hold up; a second run of the same cell could come out differently.</div>",
             f"<p>Tokens used: <b>{_fmt(t.get('tokens'))}</b> of {_fmt(t.get('ceiling_tokens'))} (input {_fmt(t.get('input_tokens'))}, "
             f"output {_fmt(t.get('output_tokens'))}, thinking {_fmt(t.get('thinking_tokens'))}, unknown-usage reservations "
             f"{_fmt(t.get('unknown_reserved_tokens'))}); images generated: <b>{_fmt(t.get('images'))}</b> of {_fmt(t.get('ceiling_images'))}.</p>"]
    if results.get("plugin_discovery"):
        pdisc = results["plugin_discovery"]
        parts.append("<div class='warn'><b>How arm C's plugin was attached.</b> " + html.escape(
            f"With {pdisc['sdk']}, the headless harness picked the plugin up from none of: {'; '.join(pdisc['tried'])}. "
            f"The request carried {pdisc['request_carried']}. So {pdisc['so']}: rules as system-instruction text, skills "
            "through skills_paths, the MCP server from its installed mcp_config.json, the hook scripts from hooks.json run "
            "with their documented JSON contract, agents/*.md as subagents, and the Blender pool sidecar started in the "
            "sandbox. Session limits (preview, token ceiling, image count) are set by the harness from the run's share; a "
            "person would be asked for them.") + "</div>")
    if results.get("unfair"):
        parts.append("<div class='warn'><b>Where the comparison is unfair to one arm</b><ul>" +
                     "".join(f"<li>{html.escape(u)}</li>" for u in results["unfair"]) + "</ul></div>")
    for bid, why in (results.get("not_run") or {}).items():
        if not bid.startswith("arm "):
            parts.append(f"<p><b>Brief {html.escape(bid)}</b>: {html.escape(why)}</p>")
    for bid, b in briefs.items():
        if not any(k.startswith(bid + ":") for k in recs):
            continue
        parts.append(f"<h2>{html.escape(bid)}: “{html.escape(b.prompt)}”</h2><p>Budget {b.max_tris:,} triangles; plausible size "
                     f"{b.size_m[0]}–{b.size_m[1]} m. Why this brief: {html.escape(b.why)}.</p><div class='grid'>")
        for arm in ("A", "B", "C"):
            k = f"{bid}:{arm}"
            e, rec = ev.get(k, {}), recs.get(k, {})
            if not rec:
                why = (results.get("not_run") or {}).get(f"arm {arm}", "not run yet")
                parts.append(f"<div class='card'><h3>{arm}: {html.escape(labels[arm])}</h3><p><i>Not run yet.</i> "
                             f"{html.escape(why)}</p></div>")
                continue
            sheet = e.get("sheet")
            rel = Path(sheet).relative_to(root).as_posix() if sheet and Path(sheet).is_relative_to(root) else None
            parts.append(f"<div class='card'><h3>{arm}: {html.escape(labels[arm])}</h3>")
            parts.append(f"<img src='{html.escape(rel)}' alt='{k} contact sheet'>" if rel else "<p>(no image)</p>")
            parts.append("<table>" + "".join(f"<tr><td>{html.escape(a)}</td><td>{html.escape(v)}</td></tr>" for a, v in _row_facts(e, rec)) + "</table>")
            if rec.get("final_text"):
                parts.append(f"<details><summary>What the agent said</summary><pre style='white-space:pre-wrap'>{html.escape(rec['final_text'][:3000])}</pre></details>")
            if rec.get("notes"):
                parts.append("<p>" + "<br>".join(html.escape(n) for n in rec["notes"]) + "</p>")
            sess = (rec.get("pipeline") or {}).get("sessions")
            if sess:
                rows_ = "".join(f"<tr><td>{'attempt' if rec.get('arm') == 'C' else 'session'} {x['session']}"
                                f"{(' (' + html.escape(str(x.get('mode'))) + ')') if x.get('mode') else ''}</td><td>{html.escape(str(x['status']))}</td><td>{_fmt(x['tokens'])} "
                                f"({_fmt(x.get('cached_tokens'))} of the input from cache)</td><td>{_fmt(x['model_calls'])} calls</td>"
                                f"<td>{_fmt(x['wall_s'])} s</td></tr>" for x in sess)
                parts.append(("<p><b>Attempts</b> (the second ran after the fix of the first's failure, in a fresh output folder)</p><table>"
                              if rec.get("arm") == "C" else
                              "<p><b>Sessions</b> (the second is a fresh conversation in the same workspace, told only that an "
                              "earlier session ran out of budget and which files exist)</p><table>") + rows_ +
                             f"<tr><td>sum</td><td></td><td>{_fmt((rec.get('tokens') or {}).get('total'))}</td>"
                             f"<td>{_fmt(rec.get('model_calls'))} calls</td><td>{_fmt(rec.get('wall_s'))} s</td></tr></table>")
            hx = e.get("harness_export")
            if hx:
                f_ = hx.get("facts") or {}
                parts.append("<div class='warn'><b>" + html.escape(hx.get("label") or "harness export") + "</b><br>" + html.escape(
                    f"from {Path(hx.get('source', '?')).name}: {f_.get('triangles')} triangles, size {f_.get('size_m_xyz')} m, "
                    f"UVs on {(f_.get('uv') or {}).get('primitives_with_uv')}/{(f_.get('uv') or {}).get('primitives')} primitives, "
                    f"on the ground: {f_.get('grounded')}" if f_ else hx.get("error", "")) + "</div>")
            sr = (rec.get("pipeline") or {}).get("self_report")
            if sr:
                lines = [f"manifest {Path(m['path']).name}: verified={m.get('verified')}, preview={m.get('preview')}, tris={m.get('tris')}"
                         for m in sr.get("manifests") or []]
                lines += [f"QA record {q.get('session_id')}: tier-1 {'ok' if q.get('tier1_ok') else 'not ok'}, tier-1.5 "
                          f"{q.get('tier15_score')}, verdict {((q.get('verdict') or {}).get('backend'), (q.get('verdict') or {}).get('action'))}"
                          for q in sr.get("qa_records") or []]
                parts.append("<details><summary>The plugin's self-report (shown as is; not used in any score)</summary><pre "
                             "style='white-space:pre-wrap'>" + html.escape("\n".join(lines) or "nothing recorded") + "</pre></details>")
            mt = (rec.get("pipeline") or {}).get("mcp_tools")
            if mt:
                parts.append("<p>Plugin tools called: " + html.escape(", ".join(f"{k} ×{v}" for k, v in mt.items())) + "</p>")
            parts.append("</div>")
        parts.append("</div>")
        rows = [(pk.split(":", 1)[1], v["result"]) for pk, v in agg.items() if pk.startswith(bid + ":")]
        if rows:
            parts.append("<h3>Blind pairwise judge (both orders; 'split' = the two orders disagreed)</h3><table><tr><td>pair</td>" +
                         "".join(f"<td>{c}</td>" for c in (*CRITERIA, "overall")) + "</tr>")
            for pair, res in rows:
                parts.append(f"<tr><td>{pair}</td>" + "".join(f"<td>{html.escape(str(res.get(c)))}</td>" for c in (*CRITERIA, "overall")) + "</tr>")
            parts.append("</table>")
    if results.get("native_sizing"):
        n = results["native_sizing"]
        parts.append("<h2>Finding: the stock agent driving the plugin tool by tool (sized, not run live)</h2>")
        parts.append("<p>" + html.escape(
            f"Method: {n['method']}. The run took {n['agent_calls']} agent model calls and {n['plugin_calls']} calls by the "
            f"plugin itself; each agent call's input grew from about {n['agent_input_estimate_first_last'][0]:,} to "
            f"{n['agent_input_estimate_first_last'][1]:,} estimated tokens, because every call re-sends the whole conversation. "
            "Share a complete run needs, by the measured actual/estimate ratio: " +
            ", ".join(f"{k}: {v:,}" for k, v in n["share_needed_by_ratio"].items()) +
            f" tokens; of that, the plugin's own model calls are about {n['plugin_own_tokens_approx']:,}. "
            f"Consequence: {n['consequence']}. An error-free script is the best case; a real model would need repairs.") + "</p>")
    if results.get("round1"):
        parts.append("<h2>Finding: round 1 (stopped)</h2><p>Four stock runs (A and B, crate and hydrant) at 195,000 tokens each, "
                     "without a budget sentence in the task. None began modelling. Their logs are kept as evidence.</p><table>"
                     "<tr><td>run</td><td>model calls</td><td>tokens</td><td>input per call</td><td>how it ended</td><td>what it did</td></tr>")
        for run_name, f in results["round1"].items():
            did = "; ".join(f"{d['tool']}: {d['what']}" for d in f.get("actions", [])[:12])
            parts.append(f"<tr><td>{html.escape(run_name)}</td><td>{f['model_calls']}</td><td>{f['tokens']:,}</td><td>"
                         f"{html.escape(', '.join(f'{x:,}' for x in f['input_per_call'] if x))}</td><td>{html.escape(str(f['record']))}</td>"
                         f"<td><details><summary>{len(f.get('actions', []))} actions</summary>{html.escape(did)}</details></td></tr>")
        parts.append("</table>")
    if results.get("c_unexpressed"):
        parts.append("<h2>What arm C's command line could not be told</h2><ul>" +
                     "".join(f"<li>{html.escape(x)}</li>" for x in results["c_unexpressed"]) + "</ul>")
    parts.append("</body></html>")
    out = root / name
    out.write_text("".join(parts))
    return out
