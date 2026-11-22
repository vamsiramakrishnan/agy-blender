"""The neutral evaluation of ``compare_agents.py``: the same checks, renders and judgement for every output.

Order, per output: a usable GLB exists -> stock Godot imports it (``GodotValidator``) -> the three.js check on the bare
GLB (no manifest, no USD: the same subset for every arm) -> facts read from the file (``glb_facts``) -> the upstream
``qa_check`` / ``mesh_stats`` / ``texel_density`` scripts through this project's Blender worker -> LODs and collision
-> renders with one camera and light rig -> a contact sheet, and a small one at game size. Then a blind pairwise
judgement on the contact sheets (full size and game size), each pair in both orders, the scorecard (``scorecard``:
every row's rule is in ``SCORE_ROWS``) and a static HTML gallery. ``package_diff`` compares two deliveries of one cell
(a change request) part by part.
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
        if _is_collision_name(name):
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
    facts["collision_meshes"] = collision_meshes(g, bufs, only_named=True)
    facts["texture_memory"] = texture_memory(imgs)
    return facts


# The convex-hull vertex limit of PhysX (Unity's and Unreal's physics); Godot accepts more and simplifies on import.
# A collision mesh is "simple" when it is convex and within this limit, so it works in every engine as shipped.
HULL_VERTEX_LIMIT = 255


def _is_collision_name(name: str) -> bool:
    low = name.lower()
    return low.endswith(COL_SUFFIX) or low.endswith(("-convcolonly", "-convcol")) or low.startswith("ucx_") \
        or "collision" in low or "collider" in low


def collision_meshes(g: Any, bufs: list[bytes], *, only_named: bool) -> list[dict[str, Any]]:
    """Per collision node: its unique vertices, whether it is convex (its volume within 2 % of its convex hull's),
    and the Godot import suffix its name carries (what Godot makes of it on import, without hand work)."""
    out = []
    for mesh_i, m, name in _instances(g):
        if only_named and not _is_collision_name(name):
            continue
        pts, faces = [], []
        for p in g.meshes[mesh_i].primitives:
            pos = accessor(g, bufs, p.attributes.POSITION).astype(np.float64)
            w = (m @ np.c_[pos, np.ones(len(pos))].T).T[:, :3]
            idx = accessor(g, bufs, p.indices).reshape(-1).astype(np.int64) if p.indices is not None else np.arange(len(pos))
            faces.append(idx[: len(idx) // 3 * 3].reshape(-1, 3) + sum(len(x) for x in pts))
            pts.append(w)
        P = np.concatenate(pts) if pts else np.zeros((0, 3))
        F = np.concatenate(faces) if faces else np.zeros((0, 3), int)
        uniq = np.unique(P.round(5), axis=0)
        rec: dict[str, Any] = {"node": name, "vertices": int(len(uniq)), "triangles": int(len(F)), "convex": None}
        low = name.lower()
        rec["godot_suffix"] = next((x for x in ("-convcolonly", "-colonly", "-convcol", "-col") if low.endswith(x)), None)
        if len(uniq) >= 4 and len(F):
            try:
                from scipy.spatial import ConvexHull

                hull = ConvexHull(uniq)
                a, b, c = P[F[:, 0]], P[F[:, 1]], P[F[:, 2]]
                vol = abs(float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum()) / 6)
                rec["convex"] = bool(hull.volume > 0 and abs(vol - hull.volume) <= 0.02 * hull.volume)
                rec["hull_volume_m3"] = round(float(hull.volume), 6)
            except Exception as e:  # noqa: BLE001 - flat or degenerate: not a usable hull
                rec["convex"], rec["error"] = False, f"{type(e).__name__}"[:80]
        rec["simple"] = bool(rec["convex"]) and rec["vertices"] <= HULL_VERTEX_LIMIT
        out.append(rec)
    return out


def texture_memory(imgs: list[dict[str, Any]]) -> dict[str, Any]:
    """Texture bytes in GPU memory, from each image's size, channels and role, with a full mip chain (x 4/3):
    - ``rgba8_mips_bytes``: uncompressed, 4 bytes per texel (what a loader that does not compress uploads; three.js
      with a plain GLTFLoader, for one);
    - ``vram_bytes_godot_default``: Godot 4's default import of a texture used in 3D, VRAM compressed with S3TC/RGTC on
      desktop: 0.5 byte per texel without alpha (BC1), 1 byte with alpha (BC3) or for a normal map (BC5).
    An estimate from the files, not a reading of an engine's memory."""
    raw = vram = 0.0
    for i in imgs:
        w, h = i.get("width") or 0, i.get("height") or 0
        if not w or not h:
            continue
        raw += w * h * 4 * 4 / 3
        alpha = str(i.get("mode") or "") in ("RGBA", "LA", "PA")
        bpp = 1.0 if alpha or "normal" in (i.get("roles") or []) else 0.5
        vram += w * h * bpp * 4 / 3
    return {"rgba8_mips_bytes": int(raw), "vram_bytes_godot_default": int(vram), "images": len(imgs)}


def visible_triangles(path: Path) -> int:
    """Triangles of every mesh instance that is not a collider (a LOD file's nodes often carry "LOD" in their names,
    which glb_facts sets aside as LOD nodes of a main file)."""
    g, bufs = _load(path)
    n = 0
    for mesh_i, _, name in _instances(g):
        if _is_collision_name(name):
            continue
        for p in g.meshes[mesh_i].primitives:
            cnt = g.accessors[p.indices].count if p.indices is not None else g.accessors[p.attributes.POSITION].count
            mode = 4 if p.mode is None else p.mode
            n += cnt // 3 if mode == 4 else max(cnt - 2, 0) if mode in (5, 6) else 0
    return n


def lod_facts(glb: Path, files: list[str]) -> list[dict[str, Any]]:
    """Each delivered LOD file: readable, its triangles, and whether it has fewer than the level before it."""
    out, prev = [], None
    try:
        prev = glb_facts(glb)["triangles"]
    except Exception:  # noqa: BLE001
        prev = None
    for f in files:
        try:
            t = visible_triangles(glb.parent / f)
            out.append({"file": f, "triangles": t, "valid": t > 0 and (prev is None or t < prev)})
            prev = t
        except Exception as e:  # noqa: BLE001
            out.append({"file": f, "triangles": None, "valid": False, "error": f"{type(e).__name__}: {e}"[:200]})
    return out


def collision_files_facts(glb: Path, files: list[str]) -> list[dict[str, Any]]:
    """Collision meshes in files delivered beside the main GLB (every mesh in such a file is a collider)."""
    out = []
    for f in files:
        try:
            g, bufs = _load(glb.parent / f)
            out += [{**c, "file": f} for c in collision_meshes(g, bufs, only_named=False)]
        except Exception as e:  # noqa: BLE001
            out.append({"file": f, "error": f"{type(e).__name__}: {e}"[:200], "simple": False})
    return out


def package_files(glb: Path) -> dict[str, Any]:
    """The delivered folder: every file under the main GLB's folder, hidden ones (caches, the harness's) excluded."""
    root = glb.parent
    files = [p for p in root.rglob("*") if p.is_file() and not any(x.startswith(".") for x in p.relative_to(root).parts)]
    return {"folder": str(root), "files": len(files), "bytes": sum(p.stat().st_size for p in files)}


def sidecar_files(glb: Path) -> dict[str, list[str]]:
    """LOD and collision files delivered beside the main GLB."""
    d = glb.parent
    lods = sorted(p.name for p in d.glob(f"{glb.stem}_LOD*.glb")) + sorted(p.name for p in d.glob("*_LOD[0-9].glb") if not p.name.startswith(glb.stem))
    cols = sorted(p.name for p in d.glob("*.glb") if p != glb and any(s in p.stem.lower() for s in ("collision", "collider", "-col", "_col")))
    return {"lod_files": sorted(set(lods)), "collision_files": cols}


# ----------------------------------------------------------------------------------------------- two deliveries


def parts(glb: Path) -> list[dict[str, Any]]:
    """The visible model as parts: connected pieces of each primitive's triangles after welding the primitive's vertices
    that share a position (a GLB splits vertices along UV seams, so welding by position joins them back). Welding stays
    inside a primitive: two separate parts that touch (a lid on a body of the same footprint) share corner positions,
    and welding across them would read as one part. Inside one primitive that holds several parts (this project's
    batches), parts touching at exactly coincident vertices still read as one; the diff is then coarser, not wrong.
    Collision and LOD nodes are left out, as in glb_facts. Arm-neutral: it needs no part names."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    g, bufs = _load(glb)
    pts, faces, welds = [], [], []
    n = nw = 0
    for mesh_i, m, name in _instances(g):
        low = name.lower()
        if _is_collision_name(name) or ("lod" in low and any(f"lod{k}" in low.replace("_", "") for k in range(1, 8))):
            continue
        for p in g.meshes[mesh_i].primitives:
            if (4 if p.mode is None else p.mode) != 4:
                continue
            pos = accessor(g, bufs, p.attributes.POSITION).astype(np.float64)
            w = (m @ np.c_[pos, np.ones(len(pos))].T).T[:, :3]
            idx = accessor(g, bufs, p.indices).reshape(-1).astype(np.int64) if p.indices is not None else np.arange(len(pos))
            faces.append(idx[: len(idx) // 3 * 3].reshape(-1, 3) + n)
            _, wi = np.unique(w.round(5), axis=0, return_inverse=True)  # 0.01 mm
            welds.append(wi.reshape(-1) + nw)
            nw += int(wi.max()) + 1 if len(wi) else 0
            pts.append(w)
            n += len(w)
    if not pts:
        return []
    P, F, weld = np.concatenate(pts), np.concatenate(faces), np.concatenate(welds)
    Fw = weld[F]
    k = int(weld.max()) + 1
    rows = np.concatenate([Fw[:, 0], Fw[:, 1], Fw[:, 2]])
    cols = np.concatenate([Fw[:, 1], Fw[:, 2], Fw[:, 0]])
    ncomp, label = connected_components(coo_matrix((np.ones(len(rows)), (rows, cols)), shape=(k, k)), directed=False)
    tri_label = label[Fw[:, 0]]
    out = []
    for c in np.unique(tri_label):
        tris = F[tri_label == c]
        v = P[np.unique(tris)]
        lo, hi = v.min(0), v.max(0)
        out.append({"lo": lo.round(5).tolist(), "hi": hi.round(5).tolist(), "triangles": int(len(tris))})
    return out


def package_diff(before: Path, after: Path, *, target_ratio: float | None = None, tol_m: float = 0.001,
                 match_frac: float = 0.25) -> dict[str, Any]:
    """What changed between two deliveries of one asset, part by part (``parts``).

    Parts are paired by the distance between their bounding boxes' corners (an optimal assignment); a pair further
    apart than ``match_frac`` of the asset's largest dimension is not a pair (the part was removed and another added).
    A pair is unchanged when both corners moved at most ``tol_m``. For a changed pair: its size ratio per axis
    (after / before) and how far its centre moved. With ``target_ratio`` (what the request asked for, e.g. 1.3), a
    changed part is "on target" when its largest per-axis ratio is within 0.1 of it; every other change, and every
    part added or removed, is collateral: something the request did not ask for."""
    from scipy.optimize import linear_sum_assignment

    A, B = parts(before), parts(after)
    fa, fb = glb_facts(before), glb_facts(after)
    out: dict[str, Any] = {"parts": [len(A), len(B)], "triangles": [fa["triangles"], fb["triangles"]],
                           "bounds_m": [fa.get("bounds_m"), fb.get("bounds_m")], "size_m_xyz": [fa.get("size_m_xyz"), fb.get("size_m_xyz")],
                           "tolerance_m": tol_m, "target_ratio": target_ratio}
    if not A or not B:
        out.update(unchanged=0, changed=[], added=len(B), removed=len(A))
        return out
    scale = max(fa.get("largest_dimension_m") or 1.0, fb.get("largest_dimension_m") or 1.0)
    ca = np.array([a["lo"] + a["hi"] for a in A])
    cb = np.array([b["lo"] + b["hi"] for b in B])
    cost = np.abs(ca[:, None, :] - cb[None, :, :]).max(axis=2)
    ia, ib = linear_sum_assignment(cost)
    paired = [(i, j) for i, j in zip(ia, ib) if cost[i, j] <= match_frac * scale]
    unchanged, changed = 0, []
    for i, j in paired:
        if cost[i, j] <= tol_m:
            unchanged += 1
            continue
        sa = np.array(A[i]["hi"]) - np.array(A[i]["lo"])
        sb = np.array(B[j]["hi"]) - np.array(B[j]["lo"])
        ratio = np.where(sa > 1e-6, sb / np.maximum(sa, 1e-9), 1.0)
        moved = float(np.linalg.norm((np.array(B[j]["lo"]) + B[j]["hi"]) / 2 - (np.array(A[i]["lo"]) + A[i]["hi"]) / 2))
        rec = {"size_ratio_xyz": ratio.round(3).tolist(), "centre_moved_m": round(moved, 4),
               "size_before_m": sa.round(4).tolist(), "triangles": [A[i]["triangles"], B[j]["triangles"]]}
        if target_ratio is not None:
            rec["on_target"] = bool(abs(float(ratio.max()) - target_ratio) <= 0.1)
        changed.append(rec)
    out.update(unchanged=unchanged, changed=changed, added=len(B) - len(paired), removed=len(A) - len(paired))
    if target_ratio is not None:
        on = sum(1 for c in changed if c.get("on_target"))
        out["on_target"] = on
        out["collateral"] = len(changed) - on + out["added"] + out["removed"]
    return out


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


def _texel_res(facts: dict[str, Any] | None) -> int:
    """The one resolution the upstream texel_density script takes, for this file's largest map: the side of the square
    with the same texel count. The script assumes a square map; passing 2048 for a 4096 x 2048 atlas read every
    island's density 1.41 times too low (the spread across islands, which the scorecard uses, does not depend on it)."""
    sizes = [(i["width"], i["height"]) for i in (facts or {}).get("images") or [] if i.get("width") and i.get("height")]
    return round(math.sqrt(max(w * h for w, h in sizes))) if sizes else 2048


async def upstream_checks(pool: Any, glb: Path, *, res: int = 2048) -> dict[str, Any]:
    """The upstream qa_check, mesh_stats and texel_density scripts on the GLB, through the project's worker. ``res``:
    the map resolution texel_density is told (``_texel_res``)."""
    v = await pool.call("version")
    if "primitives_source" not in v:
        return {"available": False, "reason": "this tree's Blender worker has no glTF import for inspect_usd and no "
                                              "texel_density (another engineer's change, not merged here)", "worker_ops": v.get("ops")}
    out: dict[str, Any] = {"available": True, "source": v.get("primitives_source")}
    # qa_check on welded copies: a GLB splits vertices along every UV seam, which would read as open edges in every
    # arm's file alike; mesh_stats and texel_density on the file as shipped.
    for name, kwargs, weld in (("qa_check", {}, True), ("mesh_stats", {}, False), ("texel_density", {"res": res}, False)):
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


async def render_views(pool: Any, glb: Path, out_dir: Path, views: tuple = VIEWS) -> dict[str, Any]:
    from antigravity3d.adapters.engines.validators import blender_frame_png

    out_dir.mkdir(parents=True, exist_ok=True)
    await pool.call("reset_scene")
    info = await pool.call("run_script", source=IMPORT_SRC.replace("PATH", repr(str(glb))), _timeout=300)
    meta = info.get("result") or {}
    pngs = []
    for name, az, el in views:
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


# The game-size read: what a player sees of a prop a few metres away. 128 px is about the height of a 1 m prop 10 m
# from a 1080p camera with a 60-degree vertical field of view (1080 / (2 * 10 * tan 30deg) = 94 px per metre), the
# distance at which a level designer places props around a play space. Downsampled from the full renders (no new
# render); a second row shows the lowest delivered level of detail from the same views, which an engine shows at a
# distance when the levels are wired.
SMALL_PX = 128
SMALL_VIEWS = ("three_quarter", "front", "side", "turn_135")
EVAL_VERSION = 2  # an evaluation written by an older version of this file is redone (round 5 added rows)


def small_sheet(lod0: dict[str, str], lod_last: dict[str, str] | None, out: Path) -> Path:
    from PIL import Image

    rows = [lod0] + ([lod_last] if lod_last else [])
    sheet = Image.new("RGB", (SMALL_PX * len(SMALL_VIEWS), SMALL_PX * len(rows)), (40, 40, 44))
    for r, views in enumerate(rows):
        for c, name in enumerate(SMALL_VIEWS):
            if views.get(name):
                im = Image.open(views[name]).convert("RGB").resize((SMALL_PX, SMALL_PX), Image.LANCZOS)
                sheet.paste(im, (c * SMALL_PX, r * SMALL_PX))
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    return out


def find_manifest(rec: Any, glb: Path) -> Path | None:
    """The package manifest this project writes, if the delivery has one: beside the GLB (the command line), or the
    one the plugin's own export wrote (a native session copies the GLB out of its package)."""
    if (glb.parent / "manifest.json").is_file():
        return glb.parent / "manifest.json"
    for m in (((getattr(rec, "pipeline", None) or {}).get("self_report") or {}).get("manifests") or []):
        if m.get("path") and Path(m["path"]).is_file():
            return Path(m["path"])
    return None


def provenance_facts(manifest: Path | None) -> dict[str, Any]:
    """Does the package say where each shipped file came from (``manifest.json`` ``provenance``)?"""
    if manifest is None:
        return {"present": False, "reason": "no package manifest"}
    d = json.loads(manifest.read_text())
    prov = d.get("provenance")
    if not isinstance(prov, dict):
        return {"present": False, "reason": "the manifest has no provenance record (not in this tree)"}
    shipped = set((d.get("file_hashes") or {}).keys())
    covered = set((prov.get("files") or {}).keys())
    return {"present": True, "generated_content": prov.get("generated_content"), "files": len(shipped),
            "files_with_origin": len(shipped & covered), "complete": bool(shipped) and shipped <= covered}


GODOT_PROJECT = """config_version=5

[application]

config/name="pilot install check"
config/features=PackedStringArray("4.7")
"""


async def install_check(manifest: Path | None, project: Path) -> dict[str, Any]:
    """This project's installer (``antigravity3d.deliver.install_package``) on the package, into a fresh Godot project,
    with its in-engine checks. Only this project's packages can be installed by it."""
    try:
        from antigravity3d.deliver import install_package
    except ImportError:
        return {"available": False, "reason": "not available in this tree (antigravity3d.deliver.install_package)"}
    if manifest is None:
        return {"available": True, "offered": False, "reason": "no package manifest: the installer takes this project's packages"}
    import inspect

    if project.exists():
        shutil.rmtree(project)
    project.mkdir(parents=True)
    (project / "project.godot").write_text(GODOT_PROJECT)
    try:
        res = install_package(manifest.parent, project, check=True)
        if inspect.isawaitable(res):
            res = await res
        d = res.to_dict() if hasattr(res, "to_dict") else dict(res)
    except Exception as e:  # noqa: BLE001 - a refusal or failure is the row's result
        return {"available": True, "offered": True, "ok": False, "error": f"{type(e).__name__}: {e}"[:500]}
    checks = d.get("checks") or []
    # InstallResult reports each check as "passed", "failed" or "skipped" (antigravity3d.deliver.install.Check); the
    # short forms are accepted too. A skipped check is not a pass: the row passes only when every check passed.
    norm = {"pass": "passed", "fail": "failed"}
    statuses = [norm.get(str(c.get("status")), str(c.get("status"))) for c in checks] if isinstance(checks, list) \
        else [norm.get(str(v), str(v)) for v in checks.values()]
    return {"available": True, "offered": True,
            "ok": d.get("status") in ("installed", "updated", "unchanged") and bool(statuses) and all(x == "passed" for x in statuses),
            "checks_failed": sum(1 for x in statuses if x == "failed"), "checks_skipped": sum(1 for x in statuses if x == "skipped"),
            "skipped_reasons": [f"{c.get('name')}: {c.get('detail')}" for c in checks if isinstance(c, dict) and c.get("status") == "skipped"],
            "status": d.get("status"), "result": d}


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
                if prev.get("glb_sha256") == digest and prev.get("v") == EVAL_VERSION and \
                        (prev.get("renders") or pool is None or not digest):
                    results[key] = prev
                    continue
            if d.exists():
                shutil.rmtree(d)
            d.mkdir(parents=True)
            r: dict[str, Any] = {"v": EVAL_VERSION, "glb_exists": bool(digest), "glb_sha256": digest, "source_glb": rec.glb}
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
            r["lods"] = lod_facts(src, r["sidecars"]["lod_files"])
            r["collision_files"] = collision_files_facts(src, r["sidecars"]["collision_files"])
            r["package"] = package_files(src)
            manifest = find_manifest(rec, src)
            r["manifest"] = str(manifest) if manifest else None
            r["provenance"] = provenance_facts(manifest)
            r["install"] = await install_check(manifest, d / "godot_project")
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
                    r["upstream"] = await upstream_checks(pool, bare, res=_texel_res(r.get("facts")))
                except Exception as e:  # noqa: BLE001
                    r["upstream"] = {"available": False, "reason": f"{type(e).__name__}: {e}"[:300]}
                try:
                    r["renders"] = await render_views(pool, bare, d / "renders")
                    r["sheet"] = str(contact_sheet(r["renders"]["views"], d / "sheet.png"))
                    lod0 = {Path(v).stem: v for v in r["renders"]["views"]}
                    last = None
                    if r["sidecars"]["lod_files"]:
                        lf = src.parent / r["sidecars"]["lod_files"][-1]
                        lr = await render_views(pool, lf, d / "renders_last_lod", tuple(v for v in VIEWS if v[0] in SMALL_VIEWS))
                        last = {Path(v).stem: v for v in lr["views"]}
                        r["renders_last_lod"] = {"file": lf.name, **lr}
                    r["sheet_small"] = str(small_sheet(lod0, last, d / "sheet_small.png"))
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

RUBRIC_SMALL = """You are reviewing two 3D props made for the same game-asset brief, as a player would see them in a game a few metres away. Each image shows one prop at that size: its top row is four views of the model, each tile 128 pixels; a second row, where present, is the same views of its lowest level of detail, which a game shows at a greater distance. Cameras, lights and ground are identical for both.

Brief: {brief}

Judge only what is visible at this size. For each criterion answer "1" (first image better), "2" (second image better) or "tie", with one short sentence:
- reads_as_brief: which reads more clearly as the brief at this size?
- proportion_scale: which has the more believable silhouette and proportions?
- surface_material: which surface and colour read better at this size (not noisy, not flat, materials distinguishable)?
- defects: which has fewer visible defects at this size (holes, popping or missing parts in the lower level, shading artefacts)?
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
              thinking: str, max_output: int, round_: str = "", small: bool = False) -> dict[str, Any]:
    """Judge every pair of arms that exist for a brief. Each pair has its own ledger account (two calls' worth), and
    each finished call is kept on disk, so adding an arm later judges only the new pairs. ``small``: the game-size
    read, the same judge and schema on the small sheets (RUBRIC_SMALL), in ``judge_small/``."""
    sheet_key, kind, rubric = ("sheet_small", "judge_small", RUBRIC_SMALL) if small else ("sheet", "judge", RUBRIC)
    from google import genai
    from google.genai import types

    ev = json.loads((root / (f"evaluation_{round_}.json" if round_ else "evaluation.json")).read_text())
    jdir = root / kind / round_ if round_ else root / kind
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
        if not all(e.get("usable") and e.get("renders") and e.get(sheet_key) for e in (e1, e2)):
            lost = [a for a, e in ((c["first"], e1), (c["second"], e2)) if not (e.get("usable") and e.get("renders") and e.get(sheet_key))]
            rec["walkover"] = f"no rendered output from {', '.join(lost)}: not sent to the judge"
        else:
            pair = "-".join(sorted((c["first"], c["second"])))
            acct = f"{round_ + ':' if round_ else ''}{kind}:{c['brief']}:{pair}"
            if acct not in clients:
                acc = relay.open(acct, 2 * per_call_tokens, 0, jdir / acct.replace(":", "_"), sandboxed=False)
                clients[acct] = genai.Client(api_key="via-relay", http_options=types.HttpOptions(
                    base_url=acc.endpoint, retry_options=types.HttpRetryOptions(attempts=1), timeout=600_000))
            rec["sheets"] = [e1.get("glb_sha256"), e2.get("glb_sha256")]  # a verdict is reused only for the same two files
            imgs = [_sheet_for_judge(Path(e1[sheet_key])), _sheet_for_judge(Path(e2[sheet_key]))]
            contents = [types.Part.from_text(text=rubric.format(brief=briefs[c["brief"]].prompt)), types.Part.from_text(text="Image 1:"),
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
    accounts = {k: v for k, v in relay.ledger.data["accounts"].items() if f"{kind}:" in k}
    res = {"model": model, "thinking": thinking, "kind": kind, "calls": done, "aggregate": aggregate(done), "accounts": accounts}
    (jdir / "judge.json").write_text(json.dumps(res, indent=1))
    return res


def _sheet_for_judge(p: Path, size: int = 1024) -> bytes:
    from PIL import Image

    im = Image.open(p).convert("RGB")
    im.thumbnail((size, size))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


# ----------------------------------------------------------------------------------------------- scorecard

# Every row of the scorecard: (id, group, label, rule, kind). kind: "check" (pass/fail; the game-ready ones make the
# game-ready rate), "value" (a measurement, no pass), "project" (a row only this project can pass by construction:
# reported beside the others, never in the shared rate). The rule is the definition a reader can check against the
# files; scorecard() applies exactly that. Engine-specific rows are for Godot 4, the briefs' target engine.
SCORE_ROWS: tuple[tuple[str, str, str, str, str], ...] = (
    ("delivered", "Finishing", "Delivered a package unassisted",
     "The cell's first session ended 'finished' with a main GLB, without a continuation session.", "check"),
    ("wall_s", "Finishing", "Time to a delivered package (s)", "Wall seconds of every session of the cell, summed.", "value"),
    ("spend", "Finishing", "Model calls / tokens / images",
     "The relay's ledger for the cell's accounts: model calls, tokens (input + output + thinking), generated images.", "value"),
    ("godot_import", "Game-ready", "Loads in Godot 4",
     "Stock Godot 4.7 imports the bare GLB (GLB and its referenced files only) without error (GodotValidator).", "check"),
    ("three_js", "Game-ready", "Loads in three.js", "The three.js check on the bare GLB reports no failed check.", "check"),
    ("upstream_qa", "Game-ready", "Independent Blender check passes", "The upstream qa_check script reports pass.", "check"),
    ("scale", "Game-ready", "Scale and orientation right",
     "Largest dimension inside the brief's plausible range, and for an upright brief, tallest along +Y.", "check"),
    ("pivot", "Game-ready", "Grounded and centred",
     "Lowest point within 2 cm of y = 0; bounding-box centre within 10 % of the largest dimension of the origin in x and z.", "check"),
    ("tri_budget", "Game-ready", "Triangle budget met", "Visible triangles of the main GLB at most the brief's budget.", "check"),
    ("uv", "Game-ready", "UVs present, inside 0-1, not overlapping",
     "Every primitive has TEXCOORD_0; no UV vertex outside [0, 1] (1e-4 slack); per material, at most 1 % of the covered "
     "texels are covered twice (a 256 px raster of the triangles).", "check"),
    ("texel_even", "Game-ready", "Texel density even",
     "For every visible object, the upstream texel_density script's spread (largest island density / smallest) is at most 2.", "check"),
    ("maps", "Game-ready", "Base colour, normal and metal-roughness maps present and not blank",
     "Every material binds the three maps (pbr_complete); no base-colour or normal map is flat (all channels' standard "
     "deviation under 1 %); no base colour is black (95 % of texels under 30 of 255).", "check"),
    ("lods", "Game-ready", "LOD files present and valid",
     "At least one <name>_LOD<n>.glb beside the main GLB; each readable and with fewer triangles than the level before.", "check"),
    ("collision", "Game-ready", "Collision present and simple",
     "A collision mesh (a node named as one in the main GLB, or a collision file beside it); every one convex (its volume "
     "within 2 % of its hull's) with at most 255 vertices (PhysX's hull limit; Godot simplifies larger ones itself).", "check"),
    ("game_ready_rate", "Game-ready", "Game-ready rate", "Share of the game-ready checks above that pass; 'all' when every one does.", "value"),
    ("draw_calls", "Runtime cost", "Draw calls / materials", "Primitives drawn (one draw call each) and materials, main GLB.", "value"),
    ("tris_per_level", "Runtime cost", "Triangles per level", "Visible triangles of the main GLB, then of each LOD file.", "value"),
    ("texture_memory", "Runtime cost", "Texture memory",
     "From each image's size and channels with a full mip chain: uncompressed RGBA8, and Godot 4's default VRAM "
     "compression on desktop (0.5 byte per texel, 1 with alpha or for a normal map). An estimate from the files.", "value"),
    ("package_bytes", "Runtime cost", "Package bytes", "Every file under the main GLB's folder (hidden caches excluded).", "value"),
    ("judge", "Looks", "Blind pairwise judge", "Per other arm: the winner both orders agree on, 'tie', or 'split'.", "value"),
    ("judge_small", "Looks", "Reads at game size",
     "The same judge on 128 px views (and the lowest LOD's, where delivered), both orders.", "value"),
    ("change", "Changing it", "One change request to a new delivered package",
     "A new session over a copy of the cell's files asked the brief's change (CHANGES): delivered or not, wall seconds, "
     "model calls, tokens.", "value"),
    ("change_scope", "Changing it", "The change left everything else alone",
     "package_diff of the two GLBs: parts resized by the asked ratio (within 0.1) against everything else that changed, "
     "was added or removed; triangles and bounds before and after. Pass: at least one part on target and no collateral.",
     "check_change"),
    ("hand_steps", "Using it", "Hand steps left before it is in a level (Godot 4)",
     "1 step to bring it in (copy the files, or run an install command), plus one per item that fails: import errors; "
     "scale or orientation; pivot; materials (maps missing or blank); collision not created on import (needs a node in "
     "the main GLB with a -colonly or -convcolonly suffix; -col and -convcol also keep the collision mesh visible, which "
     "is one step to hide it); a collision file beside the GLB is one step to attach. LOD files are not a step: Godot "
     "generates levels on import, and delivered ones are used only when wired (shown, not counted). An install whose "
     "in-engine checks all pass covers collision and levels.", "value"),
    ("installed", "Using it", "Installed and checked inside a Godot project",
     "antigravity3d.deliver.install_package on the package into a fresh project: every in-engine check passes. Only "
     "this project's packages can be installed by it.", "project"),
    ("same_bytes", "Trust", "Same request, same bytes",
     "The same command again into a copy of the first run's folder (its design record and image cache): every shipped "
     "file's sha256 equal. Only this project's command can be asked; a stock agent has no replay.", "project"),
    ("provenance", "Trust", "Says where each file came from",
     "manifest.json provenance names an origin for every shipped file. Only this project writes a manifest.", "project"),
)
GAME_READY = ("godot_import", "three_js", "upstream_qa", "scale", "pivot", "tri_budget", "uv", "texel_even", "maps",
              "lods", "collision")


def _cell_checks(e: dict[str, Any], b: Any) -> dict[str, bool | None]:
    f = e.get("facts") or {}
    up = e.get("upstream") or {}
    uv = f.get("uv") or {}
    worst = max((v["overlap_frac"] for v in (uv.get("by_material") or {}).values()), default=None)
    tex = f.get("images") or []
    blank = any(t.get("flat") for t in tex if {"baseColor", "normal"} & set(t.get("roles") or [])) or \
        any((t.get("frac_below_30_srgb") or 0) >= 0.95 for t in tex if "baseColor" in (t.get("roles") or []))
    dens = ((up.get("texel_density") or {}).get("objects") or {}) if up.get("available") else {}
    spreads = [o.get("spread") for n, o in dens.items() if not _is_collision_name(n) and o.get("spread") is not None]
    cols = (f.get("collision_meshes") or []) + [c for c in e.get("collision_files") or [] if "error" not in c]
    lods = e.get("lods") or []
    gd, th = e.get("godot") or {}, e.get("three") or {}
    return {
        "godot_import": (bool(gd.get("ok")) if not gd.get("skipped") else None) if gd else None,
        "three_js": (bool(th.get("ok")) if not th.get("skipped") else None) if th else None,
        "upstream_qa": bool((up.get("qa_check") or {}).get("pass")) if up.get("available") else None,
        "scale": bool(f.get("scale_plausible")) and (f.get("upright") is not False) if f else None,
        "pivot": bool(f.get("grounded") and f.get("centred_xz")) if f else None,
        "tri_budget": f.get("within_tri_budget") if f else None,
        "uv": (uv.get("primitives_with_uv") == uv.get("primitives") and (uv.get("verts_outside_0_1_frac") or 0) == 0
               and (worst is None or worst <= 0.01)) if f else None,
        "texel_even": (bool(spreads) and max(spreads) <= 2.0) if up.get("available") else None,
        "maps": (bool(f.get("pbr_complete")) and not blank) if f else None,
        "lods": bool(lods) and all(x.get("valid") for x in lods) if f else None,
        "collision": bool(cols) and all(c.get("simple") for c in cols) if f else None,
    }


def hand_steps(e: dict[str, Any], checks: dict[str, bool | None]) -> dict[str, Any]:
    """The Godot 4 checklist of SCORE_ROWS 'hand_steps', from the files."""
    f = e.get("facts") or {}
    inst = e.get("install") or {}
    installed = bool(inst.get("ok"))
    steps = [("bring it into the project" + (" (the install command)" if installed else " (copy the files)"))]
    if checks.get("godot_import") is False:
        steps.append("fix the import errors")
    if checks.get("scale") is False:
        steps.append("fix scale or orientation")
    if checks.get("pivot") is False:
        steps.append("move the pivot to the base centre")
    if checks.get("maps") is False:
        steps.append("wire or fix the materials")
    suffixes = [c.get("godot_suffix") for c in f.get("collision_meshes") or []]
    if not installed:
        if any(x in ("-colonly", "-convcolonly") for x in suffixes):
            pass
        elif any(x in ("-col", "-convcol") for x in suffixes):
            steps.append("hide the collision mesh Godot keeps visible (-col / -convcol)")
        elif e.get("collision_files"):
            steps.append("attach the delivered collision file to the scene")
        else:
            steps.append("make collision")
    lods = e.get("lods") or []
    return {"count": len(steps), "steps": steps,
            "authored_lods_in_use": (installed or not lods) if lods else None,
            "note": "Godot generates levels of detail on import; delivered LOD files are used only when wired"}


def scorecard(root: Path, results: dict[str, Any], briefs: dict[str, Any], round_: str = "") -> dict[str, Any]:
    """Fill every row of SCORE_ROWS for every cell from the evaluation, records, judgements and change requests, and
    write ``scorecard_<round>.json``."""
    ev = results.get("evaluation") or {}
    recs = {f"{r['brief']}:{r['arm']}": r for r in results.get("records", [])}
    changes = {f"{r['brief']}:{r['arm']}": r for r in results.get("changes", [])}
    repeats = {f"{r['brief']}:{r['arm']}": r for r in results.get("repeats", [])}
    cells: dict[str, Any] = {}
    for key, rec in recs.items():
        e = ev.get(key, {})
        b = briefs[rec["brief"]]
        f = e.get("facts") or {}
        checks = _cell_checks(e, b) if e.get("usable") else {k: False for k in GAME_READY}
        sessions = (rec.get("pipeline") or {}).get("sessions") or []
        row: dict[str, Any] = {
            "delivered": {"pass": rec.get("status") == "finished" and bool(rec.get("glb")) and len(sessions) <= 1,
                          "value": rec.get("status") + (f" after {len(sessions)} sessions" if len(sessions) > 1 else "")},
            "wall_s": {"value": rec.get("wall_s")},
            "spend": {"value": {"model_calls": rec.get("model_calls"), "tokens": (rec.get("tokens") or {}).get("total"),
                                "images": rec.get("images")}},
        }
        for k in GAME_READY:
            row[k] = {"pass": checks.get(k)}
        known = [v for k, v in checks.items() if v is not None]
        row["game_ready_rate"] = {"value": f"{sum(known)}/{len(known)}" if known else None,
                                  "pass": bool(known) and all(known) and len(known) == len(GAME_READY)}
        row["draw_calls"] = {"value": {"draw_calls": f.get("draw_calls"), "materials": f.get("materials")}}
        row["tris_per_level"] = {"value": [f.get("triangles")] + [x.get("triangles") for x in e.get("lods") or []]}
        row["texture_memory"] = {"value": f.get("texture_memory")}
        row["package_bytes"] = {"value": (e.get("package") or {}).get("bytes")}
        for jk in ("judge", "judge_small"):
            agg = (results.get(jk) or {}).get("aggregate") or {}
            vs = {}
            for pk, v in agg.items():
                br, pair = pk.split(":", 1)
                if br == rec["brief"] and rec["arm"] in pair.split("-"):
                    other = [a for a in pair.split("-") if a != rec["arm"]][0]
                    vs[other] = v["result"].get("overall")
            row[jk] = {"value": vs or None}
        ch = changes.get(key)
        if ch:
            d = (ch.get("pipeline") or {}).get("diff") or {}
            row["change"] = {"value": {"status": ch.get("status"), "wall_s": ch.get("wall_s"), "model_calls": ch.get("model_calls"),
                                       "tokens": (ch.get("tokens") or {}).get("total"), "images": ch.get("images"),
                                       "mode": (ch.get("pipeline") or {}).get("change_mode")}}
            row["change_scope"] = {"pass": (bool(d.get("on_target")) and d.get("collateral") == 0) if "changed" in d else False,
                                   "value": {k: d.get(k) for k in ("parts", "unchanged", "on_target", "collateral", "added",
                                                                   "removed", "triangles", "size_m_xyz")} if d else None}
        else:
            row["change"] = row["change_scope"] = {"value": "not run"}
        row["hand_steps"] = {"value": hand_steps(e, checks) if e.get("usable") else None}
        inst = e.get("install") or {}
        row["installed"] = {"pass": inst.get("ok") if inst.get("offered") else None,
                            "value": inst.get("reason") or ("checks: " + ", ".join(f"{k} {inst.get(k)}" for k in ("checks_failed", "checks_skipped")))}
        rp = repeats.get(key)
        row["same_bytes"] = ({"pass": (rp.get("pipeline") or {}).get("same_bytes"), "value": (rp.get("pipeline") or {}).get("bytes_compare")}
                             if rp else {"pass": None, "value": "not offered" if rec["arm"] != "C" else "not run"})
        prov = e.get("provenance") or {}
        row["provenance"] = {"pass": prov.get("complete") if prov.get("present") else None, "value": prov}
        cells[key] = row
    summary = {k: {"game_ready_rate": v["game_ready_rate"]["value"], "hand_steps": (v["hand_steps"]["value"] or {}).get("count"),
                   "delivered": v["delivered"]["pass"], "change_scope": v["change_scope"].get("pass")} for k, v in cells.items()}
    out = {"rows": [{"id": i, "group": g, "label": lab, "rule": rule, "kind": kind} for i, g, lab, rule, kind in SCORE_ROWS],
           "cells": cells, "summary": summary}
    (root / (f"scorecard_{round_}.json" if round_ else "scorecard.json")).write_text(json.dumps(out, indent=1, default=str))
    return out


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
    parts = ["<!doctype html><html><head><meta charset='utf-8'><title>Pilot: several ways to a prop</title><style>",
             "body{font:14px/1.45 system-ui,sans-serif;margin:24px;background:#16171a;color:#e6e6e6}",
             "h1,h2{font-weight:600}.warn{background:#3a2d12;border-left:4px solid #e0a020;padding:10px 14px;margin:12px 0}",
             ".grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px}.card{background:#202227;padding:10px;border-radius:6px}",
             "img{width:100%;border-radius:4px}table{border-collapse:collapse;width:100%;font-size:12.5px}",
             "td{border-top:1px solid #33363d;padding:3px 4px;vertical-align:top}td:first-child{color:#a8acb5;width:44%}",
             "code{background:#2a2d33;padding:1px 4px;border-radius:3px}</style></head><body>",
             "<h1>Pilot: the same prop brief, several ways</h1>",
             f"<div class='warn'><b>Pilot, {results.get('mode')}.</b> One run per arm per brief: these are {len(results.get('records') or [])} observations. "
             "Nothing here is a rate or a ranking that would hold up; a second run of the same cell could come out differently.</div>",
             f"<p>Tokens used: <b>{_fmt(t.get('tokens'))}</b> of {_fmt(t.get('ceiling_tokens'))} (input {_fmt(t.get('input_tokens'))}, "
             f"output {_fmt(t.get('output_tokens'))}, thinking {_fmt(t.get('thinking_tokens'))}, unknown-usage reservations "
             f"{_fmt(t.get('unknown_reserved_tokens'))}); images generated: <b>{_fmt(t.get('images'))}</b> of {_fmt(t.get('ceiling_images'))}.</p>"]
    if results.get("plugin_discovery"):
        pdisc = results["plugin_discovery"]
        parts.append("<div class='warn'><b>How the plugin arm's plugin was attached.</b> " + html.escape(
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
        shown = [a for a in labels if f"{bid}:{a}" in recs or f"arm {a}" in (results.get("not_run") or {})]
        for arm in shown:
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
        for title, agg_ in (("Blind pairwise judge", agg),
                            ("At game size (128 px)", ((results.get("judge_small") or {}).get("aggregate") or {}))):
            rows = [(pk.split(":", 1)[1], v["result"]) for pk, v in agg_.items() if pk.startswith(bid + ":")]
            if rows:
                parts.append(f"<h3>{title} (both orders; 'split' = the two orders disagreed)</h3><table><tr><td>pair</td>" +
                             "".join(f"<td>{c}</td>" for c in (*CRITERIA, "overall")) + "</tr>")
                for pair, res in rows:
                    parts.append(f"<tr><td>{pair}</td>" + "".join(f"<td>{html.escape(str(res.get(c)))}</td>" for c in (*CRITERIA, "overall")) + "</tr>")
                parts.append("</table>")
        sc = (results.get("scorecard") or {}).get("cells") or {}
        cols = [a for a in shown if f"{bid}:{a}" in sc]
        if cols:
            parts.append("<h3>Scorecard</h3><table><tr><td>row</td>" + "".join(f"<td>{a}</td>" for a in cols) + "<td>rule</td></tr>")
            for rid, group, label, rule, kind in SCORE_ROWS:
                cells_ = []
                for a in cols:
                    c = sc[f"{bid}:{a}"].get(rid) or {}
                    v = c.get("value")
                    txt = ("pass" if c["pass"] else "fail") if c.get("pass") is not None else ""
                    if v is not None and not (isinstance(v, dict) and rid == "hand_steps"):
                        txt = (txt + " " if txt else "") + _fmt(v if not isinstance(v, dict) else
                                                                "; ".join(f"{x}: {_fmt(y)}" for x, y in v.items() if not isinstance(y, dict)))
                    if rid == "hand_steps" and isinstance(v, dict):
                        txt = f"{v['count']}: " + "; ".join(v["steps"])
                    cells_.append(f"<td>{html.escape(txt or '–')}</td>")
                mark = " (only this project can pass)" if kind == "project" else ""
                parts.append(f"<tr><td>{html.escape(group)}: {html.escape(label)}{mark}</td>" + "".join(cells_) +
                             f"<td><small>{html.escape(rule)}</small></td></tr>")
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
