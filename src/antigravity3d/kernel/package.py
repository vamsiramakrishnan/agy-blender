"""Engine package writer: one implementation shared by the harness tool and the pipeline DAGs.

Layout of ``out_dir`` (everything referenced relative to the USD layer, so the directory can be moved)::

    <asset>.usd            default prim <asset>; LOD variant set; Colliders/UCX_*; relative texture paths
    <asset>.usdz           same content with textures inside (omitted, with a recorded reason, on failure)
    <asset>.glb            LOD0, embedded PBR images and material bindings (Y-up, glTF convention): self-contained
    <asset>_LOD<n>.glb     n >= 1, same materials; images referenced by relative URI ``textures/<file>``, not embedded
    <asset>_colliders.glb  convex hulls
    textures/              copies of the material maps (normal maps converted to the engine convention)
    inspector.html         offline Three.js PBR, channels, selectable nodes, LODs and budget gauges
    manifest.json          schema_version, units, tri counts, textures, materials, glb_images, collider_sources, hashes,
                           validation, versions; ``art_direction`` when one was applied (schema_version 3)

Images: ``<asset>.glb`` is the single-file deliverable and embeds every image; each image is byte-for-byte the
``textures/`` file the USD references. A far LOD file is never shipped without its package, so it references those
same files instead of carrying another copy of every map (glTF 2.0 allows URIs in a GLB), and an engine that imports
the LOD files gets one texture asset per map rather than one per file. The USDZ keeps its own copy (the format is a
self-contained archive) and ``inspector.html`` its embedded review GLB (it opens offline from one file).
``manifest.glb_images`` states, per GLB file and per glTF image, whether the image is embedded or referenced, its
content hash and the ``textures/`` file holding the same bytes; ``validate_package`` checks all of it.

Per-engine decisions (see ``ENGINE_RULES`` for the rule table they come from):

* up axis: the package USD is written in the engine's *native* up axis. Working stage is Z-up metres;
  for Y-up engines (unity_urp, godot_4, roblox_r15) the asset root gets ``xformOp:rotateX = -90`` and
  ``upAxis = Y`` together (so (x, y, z) -> (x, z, -y), the same mapping the GLB uses). Unreal stays Z-up.
* units: always metres (``metersPerUnit = 1.0``) with no scale baked in; every engine's rules accept
  1.0 and importers honour the metadata. Not converting to centimetres for Unreal is deliberate:
  a baked x100 root scale would affect collider/LOD authoring and buys nothing the importer does not do.
* normal maps: unreal_5 consumes DirectX (green down); everything else OpenGL (green up). The package
  records the native-engine map as ``textures[].path``. USD Preview Surface and glTF consume +Y;
  ``textures[].usd_path`` and embedded GLB normals always use that convention.
"""

from __future__ import annotations

import hashlib
import io
import importlib.metadata as md
import json
import platform
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import quote, unquote

import numpy as np
from PIL import Image
from pxr import Sdf, Usd, UsdGeom, UsdPhysics, UsdShade, Vt

from antigravity3d.artifacts.inspector import InspectorQA, build_inspector, bundled_three_js
from antigravity3d.kernel import compile_ir
from antigravity3d.kernel.compile_ir import (
    CREASE_DEG, IR_KEY, MapSource, MaterialParams, author_mesh_data, read_ir, read_material, set_normal_encoding,
)
from antigravity3d.kernel.fast_ops import (
    ExportBundle, Hull, Issue, LOD, UVResult, gltf_default_material, shipped, texel_density_report, write_gltf,
)
from antigravity3d.kernel.gltf_writer import EncodedImage, GltfMaterial, Sampler, TextureRef
from antigravity3d.kernel.types import MeshData, gltf_world_bounds
from antigravity3d.kernel.usdz import create_usdz
from antigravity3d.kernel.validator import (
    ENGINE_RULES,
    FBX_PRESET,
    PACKAGE_TEXTURE_DIR,
    check_engine_compliance,
    glb_images,
    texel_density_issues,
    validate_package,
    validate_usd,
)
from antigravity3d.state import EngineBudget

# 2: far LOD GLBs reference textures/ by URI instead of embedding them; manifest ``glb_images`` records which is which
# 3: the render meshes that ship may leave out faces two parts press together (manifest ``contact_faces`` says what was
#    removed, per part and per level, and whether the engine requires closed meshes); a collision hull over its
#    engine's limit is replaced and says so in ``collider_sources``; and the OPTIONAL ``art_direction`` record names a
#    style and the values applied (absent means no style; ``validate_package`` checks it against the files when
#    present), with the shipped texel density beside the atlas-space one in ``texel_density``.
# Packages written under an older version are validated by the layout they declare.
SCHEMA_VERSION = 3
ART_SCHEMA_VERSION = SCHEMA_VERSION  # the version whose contract has the ``art_direction`` record
TEXTURE_DIR = PACKAGE_TEXTURE_DIR
COLLIDERS_SCOPE = "Colliders"
LOD_SET = "LOD"
# Convention the engine's importer expects for tangent-space normal maps.
NORMAL_CONVENTION = {"unreal_5": "directx", "unity_urp": "opengl", "godot_4": "opengl", "roblox_r15": "opengl"}


@dataclass
class PackageManifest:
    out_dir: Path
    data: dict[str, Any]
    issues: list[Issue] = field(default_factory=list)  # USD, engine and package checks

    @property
    def files(self) -> list[str]:
        return list(self.data["files"])

    @property
    def ok(self) -> bool:
        return not any(i.severity == "critical" for i in self.issues)

    @property
    def n_lod_files(self) -> int:
        return sum(1 for f in self.data["files"] if re.search(r"_LOD\d+\.glb$", f))


# --------------------------------------------------------------------------- small helpers


def sanitize(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


def collider_name(engine: str, asset: str, i: int, *, usd: bool = True) -> str:
    """Collision hull name. USD prim names are identifiers (no '-'), so the USD prims always use the
    ``UCX_<asset>_<nn>`` convention (Unreal's, and a generic marker; the physics API + purpose=guide are
    the machine-readable markers). GLB node names follow the engine: Godot's importer wants the
    ``-convcolonly`` suffix, which only glTF node names can carry."""
    a = sanitize(asset)
    if engine == "godot_4" and not usd:
        return f"{a}_{i:02d}-convcolonly"
    return f"UCX_{a}_{i:02d}"


def flip_normal_green(src: Path, dst: Path) -> Path:
    """Writes ``src`` with the green channel inverted (OpenGL <-> DirectX normal map)."""
    with Image.open(src) as im:
        im.load()
        has_alpha = im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info
        rgb = im.convert("RGBA" if has_alpha else "RGB")
    a = np.asarray(rgb).copy()
    a[..., 1] = 255 - a[..., 1]
    Image.fromarray(a).save(dst, format="PNG")
    return dst


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def split_batch(members: Sequence[str], tris: Mapping[str, int], cap: int | None) -> list[list[str]]:
    """One material's parts as meshes of at most ``cap`` triangles (None: no limit, one mesh), parts kept whole and in order.

    Greedy: a part joins the current mesh while the total stays within the cap, else it starts the next. A part that
    alone exceeds the cap gets a mesh of its own (no split of a part can be made here: the authored-part rule,
    ``check_engine_compliance``, rejects it)."""
    if not cap:
        return [list(members)]
    chunks: list[list[str]] = []
    total = 0
    for name in members:
        n = int(tris[name])
        if chunks and total + n <= cap:
            chunks[-1].append(name)
            total += n
        else:
            chunks.append([name])
            total = n
    return chunks


def batch_tri_cap_issues(engine: str, batch_tris: Mapping[str, int]) -> list[Issue]:
    """The engine's per-mesh triangle cap applied to what ships: one GLB mesh per batch.

    ``check_engine_compliance`` counts authored parts, but once a package has more nodes than draw calls its GLB submits
    the parts of a material as a world-space batch, and an importer makes one mesh of it (Roblox: "Individual meshes can
    not exceed 20,000 triangles"). The writer splits a material's batch at that limit (``split_batch``), so this is the
    check that it did: forty 880-triangle spheres pass the per-part rule and ship as two meshes, not one of 35,200. The
    per-part rule stays for what no split can fix, a single part over the limit; it names the part and runs before packaging."""
    cap = ENGINE_RULES[engine].max_tris_per_mesh
    if not cap:
        return []
    return [Issue(f"{engine}:batch_tri_cap", "critical", name,
                  f"{tris} tris in one shipped GLB mesh > {cap} per mesh (the GLB merges the parts of a material once there are more nodes than "
                  f"draw calls; the importer makes one mesh of the batch)")
            for name, tris in batch_tris.items() if tris > cap]


# --------------------------------------------------------------------------- FBX (written after the manifest's first version)

def fbx_inputs(data: Mapping[str, Any], out: Path) -> dict[str, Any] | None:
    """The explicit files the FBX export needs, from a package's manifest (None: this engine gets no FBX).

    The layout of a package is known here and nowhere else: the worker is handed paths, never asked to guess them.
    ``texture_map`` makes the FBX reference the map the engine reads where the GLB embeds another (Unreal: the package's
    ``normal_directx.png``, the GLB has the +Y ``normal.png``)."""
    preset = FBX_PRESET.get(str(data.get("target_engine")))
    if preset is None:
        return None
    asset = str(data["asset"])
    files = list(data.get("files") or [])
    lods = sorted((f for f in files if re.fullmatch(re.escape(asset) + r"_LOD\d+\.glb", f)), key=lambda f: int(re.search(r"_LOD(\d+)\.glb$", f).group(1)))  # type: ignore[union-attr]
    colliders = f"{asset}_colliders.glb"
    tmap = {Path(t["usd_path"]).name: Path(t["path"]).name for t in data.get("textures") or []
            if t.get("path") != t.get("usd_path") and Path(t["path"]).parent.as_posix() == TEXTURE_DIR == Path(t["usd_path"]).parent.as_posix()}
    return {"source": str(out / f"{asset}.glb"), "lods": [str(out / f) for f in lods],
            "colliders": str(out / colliders) if colliders in files else None,
            "textures_dir": str(out / TEXTURE_DIR) if (out / TEXTURE_DIR).is_dir() else None,  # a package without maps has none
            "texture_map": tmap, "asset_name": asset, "preset": preset, "out_dir": str(out)}


def fbx_contradictions(data: Mapping[str, Any], out: Path, result: Mapping[str, Any]) -> list[str]:
    """Where the FBX and the manifest's own records disagree (empty: they do not).

    Roblox: the FBX embeds image bytes; ``roblox_surface_appearance`` names the files a person assigns in Studio. The
    embedded base colour and normal must be those very files (byte for byte). Metalness and roughness are separate files
    there and are not carried by an FBX: that is said in the ``fbx`` record, not a disagreement."""
    out_list: list[str] = []
    record = data.get("roblox_surface_appearance")
    if result.get("preset") != "roblox" or not record:
        return out_list
    refs = {r["slot"]: r.get("sha256") for r in (result.get("textures") or {}).get("references", [])}
    wanted = {"DiffuseColor": "ColorMap", "NormalMap": "NormalMap"}
    for slot, key in wanted.items():
        files = {m[key] for m in record["materials"].values() if key in m}
        digests = {_sha256(out / f) for f in files if (out / f).is_file()}
        if slot in refs and refs[slot] not in digests:
            out_list.append(f"the FBX embeds a {slot} image that is not one of the package's {key} files {sorted(files)} (roblox_surface_appearance)")
        if slot not in refs and files:
            out_list.append(f"the package ships a {key} ({sorted(files)}) but the FBX carries no {slot}")
    return out_list


def fbx_record(result: Mapping[str, Any]) -> dict[str, Any]:
    """The manifest's ``fbx`` entry from the worker's result: facts about the file and its comparison, no timings or
    machine paths (two runs of one scene must describe their files the same way)."""
    header = result["header"]
    models = {n: {k: m[k] for k in ("type", "rotation_deg", "pre_rotation_deg", "scaling", "extent_min_m", "extent_max_m", "triangles", "normals_written")}
              for n, m in header["models"].items()}
    textures = result["textures"]
    checks = result["comparison"]["checks"]
    return {
        "status": "written", "file": Path(result["fbx"]["path"]).name, "preset": result["preset"], "blender": result["blender"],
        "script": {k: v for k, v in result["script"].items() if k != "source"},  # where it was loaded from is the run's, not the file's
        "comparison": {"ok": bool(result["comparison"]["ok"]), "checks": len(checks), "failed": [c["name"] for c in checks if not c["ok"]],
                       "checked": sorted({c["name"].split(".")[0] for c in checks})},
        "sources": {"levels": [{**lv, "file": Path(lv["file"]).name} for lv in result["sources"]["levels"]],
                    "collision_hulls": None if result["sources"]["collision_hulls"] is None
                    else {**result["sources"]["collision_hulls"], "file": Path(result["sources"]["collision_hulls"]["file"]).name}},
        "header": {**{k: header[k] for k in ("version", "UpAxis", "UpAxisSign", "FrontAxis", "FrontAxisSign", "CoordAxis", "CoordAxisSign",
                                             "UnitScaleFactor", "OriginalUnitScaleFactor")}, "models": models},
        "contents": result["contents"], "join": [{k: v for k, v in j.items() if k != "material_slots_after"} | {"material_slots": j["material_slots_after"]}
                                                  for j in result["join"]],
        "textures": {"mode": textures["mode"], "embedded_bytes": textures["embedded_bytes"],
                     "references": [{k: r[k] for k in ("slot", "relative", "sha256")} for r in textures["references"]],
                     "substituted": textures["substituted"]},
        "maps": {"carried": sorted({f"{m}.{slot}" for m, per in result["maps"]["fbx"].items() for slot, v in per.items() if v}),
                 "lost": list(result["maps"]["lost"]),
                 "note": "an FBX carries the texture links Blender's exporter knows (base colour, normal); the GLB and USD stay the full-fidelity files"},
        "limits": result.get("limits"), "reproducibility": result["reproducibility"],
        "verified": result["verified"], "not_verified": result["not_verified"],
    }


def fbx_failed_record(data: Mapping[str, Any], error_class: str, message: str) -> dict[str, Any]:
    """The manifest's ``fbx`` entry when the FBX could not be made: no file ships, the package's USD and GLB stand, and
    the entry says why (with the failed comparison checks when the worker sent them)."""
    failed: list[dict[str, Any]] = []
    if "failed checks: " in message:
        try:
            failed = json.loads(message.split("failed checks: ", 1)[1])
        except ValueError:
            failed = []
    return {"status": "failed", "preset": FBX_PRESET.get(str(data.get("target_engine"))), "error": error_class,
            "reason": message[:600], "failed_checks": failed[:20],
            "statement": "No FBX was written. The package's USD and GLB files are not affected by this."}


def drop_fbx(data: dict[str, Any], out: Path, error_class: str, message: str) -> None:
    """Take a written FBX out of the package again (file, ``files``, ``file_hashes``) and record why, as for a failed export."""
    name = (data.get("fbx") or {}).get("file")
    if name:
        (out / name).unlink(missing_ok=True)
        data["files"] = [f for f in data["files"] if f != name]
        data["file_hashes"].pop(name, None)
    data["fbx"] = fbx_failed_record(data, error_class, message)


def record_fbx(data: dict[str, Any], out: Path, result: Mapping[str, Any]) -> None:
    """Put a written FBX into the manifest: ``files``, ``file_hashes`` (read from disk) and the ``fbx`` record.

    The manifest is written by ``write_engine_package`` before the FBX exists, so whoever writes the shipped manifest
    calls this first (pass C's package node; the native export before it writes ``manifest.json``)."""
    rec = fbx_record(result)
    name = rec["file"]
    if name not in data["files"]:
        data["files"].append(name)
    path = out / name
    data["file_hashes"][name] = {"sha256": _sha256(path), "bytes": path.stat().st_size}
    data["fbx"] = rec


def _local_file(uri: str) -> Path | None:
    p = Path(uri[7:] if uri.startswith("file://") else uri)
    return p if p.is_file() else None


def _version(dist: str) -> str | None:
    try:
        return md.version(dist)
    except md.PackageNotFoundError:
        return None


def _versions() -> dict[str, Any]:
    from pxr import Usd as _Usd

    return {
        "antigravity3d": _version("antigravity3d"),
        "python": platform.python_version(),
        "usd": ".".join(map(str, _Usd.GetVersion())),
        "usd-core": _version("usd-core"),
        "trimesh": _version("trimesh"),
        "xatlas": _version("xatlas"),
        "meshoptimizer": _version("meshoptimizer"),
        "coacd": _version("coacd"),
        "numpy": np.__version__,
        "platform": sys.platform,
    }


def _issue_dict(i: Issue) -> dict[str, Any]:
    return {"code": i.code, "severity": i.severity, "node": i.node_id, "detail": i.detail}


def _parts(b: ExportBundle | Mapping[str, Any]) -> tuple[list[UVResult], dict[str, list], list[Hull]]:
    if isinstance(b, ExportBundle):
        return b.uvs, b.lods, b.colliders
    return list(b["uvs"]), dict(b.get("lods") or {}), list(b.get("colliders") or [])


def _clean_previous(out: Path) -> None:
    """Removes what an earlier export into this directory wrote (listed in its manifest)."""
    old = out / "manifest.json"
    if not old.is_file():
        return
    try:
        names = json.loads(old.read_text()).get("file_hashes", {}).keys()
    except ValueError:
        names = []
    for rel in [*names, "manifest.json"]:
        p = (out / rel).resolve()
        if p.is_relative_to(out.resolve()) and p.is_file():
            p.unlink()
    tex = out / TEXTURE_DIR
    if tex.is_dir() and not any(tex.iterdir()):
        tex.rmdir()


# --------------------------------------------------------------------------- USD authoring


def _package_textures(stage: Usd.Stage, out: Path, engine: str, art: Any = None) -> tuple[list[dict[str, Any]], list[str]]:
    """Copies map files into ``textures/``, converts normal maps, rewrites asset paths relative.

    With an art direction every map is written as it ships under that style (``ArtDirection.finish_map``: its map size,
    and its palette for albedo maps), whoever produced the source file; a map already within both limits is copied
    byte for byte, as without a style."""
    px = art.texture_px(engine, "prop", 1 << 30) if art is not None and art.texture_px_prop is not None else None
    target = NORMAL_CONVENTION.get(engine, "opengl")
    records: list[dict[str, Any]] = []
    unresolved: list[str] = []
    done: dict[tuple[str, bool], str] = {}  # (source, flipped) -> relative path
    owners: dict[str, str] = {}  # relative path -> source it was written for (collision guard)
    def copy_map(src: Path, flip: bool, convention: str, role: str) -> str:
        source = str(src.resolve())
        key = (source, flip)
        if key in done:
            return done[key]
        name = f"{src.stem}_{convention}.png" if flip else src.name
        rel = f"{TEXTURE_DIR}/{name}"
        if rel in owners and owners[rel] != source:
            tag = _sha256(src)[:12]  # the file's content, never where it lives: names must not depend on the author's directory
            rel = f"{TEXTURE_DIR}/{Path(name).stem}_{tag}{Path(name).suffix}"
        (out / TEXTURE_DIR).mkdir(exist_ok=True)
        if flip:
            flip_normal_green(src, out / rel)
        else:
            shutil.copy2(src, out / rel)
        if art is not None:
            with Image.open(out / rel) as im:
                im.load()
                finished = art.finish_map(im, role, px)
            if finished is not im:
                finished.save(out / rel, format="PNG")
        owners[rel] = source
        done[key] = rel
        return rel

    for prim in stage.Traverse():
        if not prim.IsA(UsdShade.Shader) or UsdShade.Shader(prim).GetIdAttr().Get() != "UsdUVTexture":
            continue
        shader = UsdShade.Shader(prim)
        inp = shader.GetInput("file")
        v = inp.Get() if inp else None
        if not isinstance(v, Sdf.AssetPath) or not v.path:
            continue
        mat = prim.GetParent()
        mat_id = mat.GetName()
        lowered = prim.GetName().lower()
        is_normal = "normal" in lowered
        role = "normal" if is_normal else next((r for r in ("orm", "emissive") if r in lowered), "albedo")
        src = _local_file(v.resolvedPath or v.path)
        if src is None:
            unresolved.append(v.path)
            continue
        src_conv = str(mat.GetCustomDataByKey("agy:normalConvention") or "opengl")
        flip = is_normal and src_conv != target
        rel = copy_map(src, flip, target, role)
        # USD Preview Surface and glTF both use +Y. Preserve a separate DirectX map
        # for native engine import when needed; use canonical USD scale/bias in the archive.
        usd_rel = copy_map(src, src_conv != "opengl", "opengl", role) if is_normal else rel
        with Image.open(out / rel) as im:
            width, height = im.size
        cs = shader.GetInput("sourceColorSpace")
        records.append({
            "path": rel,
            "usd_path": usd_rel,
            "role": role,
            "material": mat_id,
            "width": width,
            "height": height,
            "convention": (target if is_normal else None),
            "authored_convention": (src_conv if is_normal else None),
            "converted": flip,
            "color_space": str(cs.Get()) if cs and cs.Get() else None,
            "source": src.name,
        })
        inp.Set(Sdf.AssetPath(f"./{usd_rel}"))  # relative to the layer: the package is relocatable
        if is_normal:
            set_normal_encoding(shader, "opengl")
            mat.SetCustomDataByKey("agy:normalConvention", "opengl")
    _relativise_embedded_ir(stage, records)
    return records, unresolved


_ROBLOX_TEXTURES = "https://create.roblox.com/docs/art/modeling/texture-specifications"
_ROBLOX_IMPORTER = "https://create.roblox.com/docs/art/modeling/3d-importer"
_ROBLOX_UNITS = "https://create.roblox.com/docs/physics/units"
_STUD_M = 0.28  # "Length | 1 stud | 28 cm" (Roblox units page)


def _roblox_texture_guidance(largest_m: float) -> dict[str, Any]:
    """Roblox's size guidance for an object of this largest dimension: 5x5 studs 256, 10x10 studs 512, 20x20 studs 1024
    (texture specifications). The thresholds between those three rows are this function's reading of them."""
    studs = largest_m / _STUD_M
    return {"largest_dimension_m": round(largest_m, 3), "largest_dimension_studs": round(studs, 2),
            "stud_definition": f'"Length | 1 stud | 28 cm" ({_ROBLOX_UNITS})',
            "guidance_px": 256 if studs <= 5 else 512 if studs <= 10 else 1024,
            "reading": "up to 5 studs: 256; up to 10: 512; larger: 1024 (this package's reading of the three guidance rows)"}


def _roblox_surface_maps(out: Path, textures: list[dict[str, Any]], largest_m: float, meshparts: int) -> tuple[dict[str, Any], list[str]]:
    """Roblox only: the metalness and roughness maps as separate single-channel files, beside the packed ``*_orm.png``,
    and the manifest's ``roblox_surface_appearance`` record (what the Creator Hub documents, what was not checked).

    Roughness and Metalness are separate "Single Channel Grayscale (8-bit)" rows in the texture specifications; no
    occlusion map is listed and no packed map is mentioned. The packed file stays (the GLB uses it: glTF's
    metallicRoughnessTexture is G = roughness, B = metalness). Returns the record and the new file names."""
    per_material: dict[str, dict[str, Any]] = {}
    new_files: list[str] = []
    for t in textures:
        if t["role"] != "orm":
            continue
        rel = t["path"]
        stem = Path(rel).stem
        base = stem[:-4] if stem.endswith("_orm") else stem
        with Image.open(out / rel) as im:
            rgb = np.asarray(im.convert("RGB"))
        entry = per_material.setdefault(t["material"], {})
        entry["packed_source"] = rel
        for key, channel, name in (("MetalnessMap", 2, "metalness"), ("RoughnessMap", 1, "roughness")):
            dest = f"{TEXTURE_DIR}/{base}_{name}.png"
            Image.fromarray(np.ascontiguousarray(rgb[..., channel]), "L").save(out / dest, format="PNG")
            entry[key] = dest
            if dest not in new_files:
                new_files.append(dest)
    emissive = sorted({t["material"] for t in textures if t["role"] == "emissive"})
    for t in textures:
        entry = per_material.setdefault(t["material"], {}) if t["role"] in ("albedo", "normal") else per_material.get(t["material"])
        if entry is None or t["role"] not in ("albedo", "normal"):
            continue
        entry["ColorMap" if t["role"] == "albedo" else "NormalMap"] = t["path"]
        if t["role"] == "albedo":
            with Image.open(out / t["path"]) as im:
                entry["ColorMap_has_alpha"] = "A" in im.getbands()  # recorded; the file is not changed
    sizes = [max(t["width"], t["height"]) for t in textures]
    record = {
        "materials": dict(sorted(per_material.items())),
        "occlusion": "Roblox lists no occlusion map: the red (occlusion) channel of the packed map has no Roblox input and is not carried to the separate maps",
        "texture_px_written": max(sizes) if sizes else None,
        "size_guidance": _roblox_texture_guidance(largest_m),
        "meshparts_expected": meshparts,
        "documented": [
            {"statement": "Roughness, Metalness and Emissive Mask: \"Single Channel Grayscale (8-bit)\"; Normal: \"Roblox only supports OpenGL format - Tangent Space normal maps\"; Albedo: \"RGB (24-bit)\"", "source": _ROBLOX_TEXTURES},
            {"statement": "\"Roblox supports up to 4096\u00d74096 pixel texture resolutions (4K).\" and, under UV mapping, \"Roblox supports up to 1024\u00d71024 pixel spaces for texture maps.\" The page does not reconcile the two; 1024 satisfies both", "source": _ROBLOX_TEXTURES},
            {"statement": "\"For smaller 5\u00d75 stud objects, use 256\u00d7256 texture resolutions.\" \"For medium 10\u00d710 stud objects, use 512\u00d7512 texture resolutions.\" \"For larger 20\u00d720 stud objects, use 1024\u00d71024 texture resolutions.\"", "source": _ROBLOX_TEXTURES},
            {"statement": "\"Mesh objects can only have one material assigned.\"", "source": _ROBLOX_TEXTURES},
            {"statement": "glTF: \"A Model inside the top-level Model, containing one MeshPart for each primitive in the glTF mesh.\" This package's GLB has one mesh with one primitive per node or per material batch, so "
                          f"{meshparts} MeshPart(s) are expected", "source": _ROBLOX_IMPORTER},
            {"statement": "Scale Unit \"Sets what units the file was modeled in so that it's sized appropriately when imported. By default, this is set to Studs.\" The page lists no other option. This package is in metres", "source": _ROBLOX_IMPORTER},
        ],
        "not_done": [
            *([f"emission on {emissive}: Roblox takes an Emissive Mask (single-channel grayscale); no Creator Hub page found says how a glTF emissive texture and colour map to it, so no mask is written"] if emissive else []),
            "Not run in Roblox Studio. How SurfaceAppearance binds to each MeshPart, and whether the importer builds one from the GLB's packed metallic-roughness texture, are not documented and were not observed; the separate maps are for assigning MetalnessMap and RoughnessMap",
        ],
    }
    return record, new_files


_IR_URI_FIELD = {"albedo": "albedo_uri", "orm": "orm_uri", "normal": "normal_uri", "emissive": "emissive_uri"}


def _relativise_embedded_ir(stage: Usd.Stage, records: list[dict[str, Any]]) -> None:
    """Makes the scene IR embedded in the exported layer say what the rewritten shaders say.

    The IR keeps the author's source URIs (absolute local paths); the shaders were just pointed at
    ``./textures/<file>``. Left alone, the IR would leak the path into every shipped file, make two exports of one
    scene differ, and point a re-opened, relocated package at files that are not there. Each recorded map becomes
    the packaged file relative to the layer; a normal map was converted to the USD (OpenGL) convention when copied,
    so the IR's ``normal_convention`` follows. Maps that were not found on disk stay as authored.
    """
    asset = stage.GetDefaultPrim()
    scene = read_ir(asset)
    if scene is None:
        return
    by_material: dict[str, dict[str, str]] = {}
    for record in records:
        by_material.setdefault(record["material"], {})[_IR_URI_FIELD[record["role"]]] = f"./{record['usd_path']}"
    materials = []
    for m in scene.materials:
        update = dict(by_material.get(m.id, {}))
        if "normal_uri" in update:
            update["normal_convention"] = "opengl"
        materials.append(m.model_copy(update=update) if update else m)
    asset.SetCustomDataByKey(IR_KEY, scene.model_copy(update={"materials": materials}).model_dump_json())


def _write_glb_level(path: Path, parts: dict[str, UVResult | LOD], batches: dict[str, list[str]],
                     materials: dict[str, GltfMaterial], suffix: str, image_uris: Mapping[str, str] | None = None,
                     crease_deg: float | None = None) -> None:
    items = []
    bound = []
    names = []
    for name, nodes in batches.items():
        group = [parts[node] for node in nodes if len(parts[node].triangles)]  # a part absent at this LOD is omitted
        if not group:  # every member absent: no node, one draw call fewer
            continue
        names.append(name)
        nodes = [node for node in nodes if len(parts[node].triangles)]
        if len(group) == 1:
            items.append(group[0])
        else:
            offsets = np.cumsum([0] + [len(p.points) for p in group[:-1]])
            indices = np.vstack([p.triangles + offset for p, offset in zip(group, offsets)])
            items.append(LOD(1., np.vstack([p.points for p in group]), indices,
                             np.vstack([p.uvs for p in group]), 0., 0.))
        bound.append(materials.get(nodes[0]))
    write_gltf(path, items, [name + suffix for name in names], materials=bound, image_uris=image_uris, crease_deg=crease_deg)


def _glb_image_records(path: Path, texture_of: Mapping[str, str], hashes: Mapping[str, str]) -> list[dict[str, Any]]:
    """Manifest ``glb_images`` entry of one written GLB: per glTF image, embedded or referenced, its content hash
    and the ``textures/`` file with the same bytes (None for an image no package file holds, i.e. a transcoded
    map). Read back from the file, so the record says what was written."""
    out = []
    for rec in glb_images(path):
        if rec["storage"] == "uri":
            rel = unquote(rec["uri"])
            rec = {**rec, "sha256": hashes[rel], "texture": rel}
        else:
            rec = {**rec, "texture": texture_of.get(rec["sha256"])}
        out.append(rec)
    return out


def _flip_green_png(image: EncodedImage) -> EncodedImage:
    """OpenGL <-> DirectX normal map in memory (what ``flip_normal_green`` does to a file); alpha is kept."""
    with Image.open(io.BytesIO(image.data)) as im:
        im.load()
        has_alpha = im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info
        a = np.asarray(im.convert("RGBA" if has_alpha else "RGB")).copy()
    a[..., 1] = 255 - a[..., 1]
    buf = io.BytesIO()
    Image.fromarray(a).save(buf, format="PNG")
    return EncodedImage(buf.getvalue(), "image/png", hashlib.sha256(buf.getvalue()).hexdigest(), image.name, True)


def gltf_material(params: MaterialParams, image_of, *, double_sided: bool = False, sampler: Sampler | None = None) -> GltfMaterial:
    """The glTF 2.0 material for one :class:`MaterialParams`. Pure mapping, no I/O.

    ``image_of(role)`` returns the ``EncodedImage`` for a map role (``albedo``, ``orm``, ``normal``, ``emissive``) or
    None. Core fields are always written; each ``KHR_materials_*`` extension only when one of its parameters
    differs from the glTF default, so a material that uses none of them serialises exactly as before they existed.
    A map replaces the factor of its channel (white base colour under ``albedo``, 1.0 metallic/roughness under
    ``orm``); the emissive map is multiplied by ``emissive_color``, and ``emissive_strength`` is the extension.
    ``sampler`` (an art direction's texture filtering) goes on every texture slot; None writes no sampler.
    """
    tex = {role: TextureRef(img, sampler) for role in ("albedo", "orm", "normal", "emissive") if (img := image_of(role)) is not None}
    color = [1.0] * 3 if "albedo" in tex else list(params.base_color)
    ext: dict[str, dict[str, Any]] = {}
    if params.emissive_strength != 1.0:
        ext["KHR_materials_emissive_strength"] = {"emissiveStrength": params.emissive_strength}
    if params.clearcoat > 0.0:
        ext["KHR_materials_clearcoat"] = {"clearcoatFactor": params.clearcoat,
                                          "clearcoatRoughnessFactor": params.clearcoat_roughness}
    if params.transmission > 0.0:
        ext["KHR_materials_transmission"] = {"transmissionFactor": params.transmission}
    if params.ior != 1.5:
        ext["KHR_materials_ior"] = {"ior": params.ior}
    if any(params.sheen_color):
        ext["KHR_materials_sheen"] = {"sheenColorFactor": list(params.sheen_color),
                                      "sheenRoughnessFactor": params.sheen_roughness}
    if params.specular != 1.0 or tuple(params.specular_color) != (1.0, 1.0, 1.0):
        ext["KHR_materials_specular"] = {"specularFactor": params.specular,
                                         "specularColorFactor": list(params.specular_color)}
    blended = params.alpha_mode != "OPAQUE"
    return GltfMaterial(
        name=params.name, base_color_factor=(*color, params.opacity if blended else 1.0),
        base_color_texture=tex.get("albedo"), normal_texture=tex.get("normal"), occlusion_texture=tex.get("orm"),
        metallic_roughness_texture=tex.get("orm"),
        metallic_factor=1.0 if "orm" in tex else params.metallic,
        roughness_factor=1.0 if "orm" in tex else params.roughness,
        emissive_texture=tex.get("emissive"), emissive_factor=list(params.emissive_color) if any(params.emissive_color) else None,
        alpha_mode=params.alpha_mode if blended else None, alpha_cutoff=params.alpha_cutoff if params.alpha_mode == "MASK" else None,
        double_sided=double_sided, extensions=ext,
    )


def gltf_materials(stage: Usd.Stage, meshes: Sequence[MeshData], *, package_dir: str | Path | None = None,
                   sampler: Sampler | None = None) -> dict[str, GltfMaterial]:
    """``{node_id: GltfMaterial}`` for the meshes of a composed stage: the one derivation of glTF materials.

    The package export, the inspector's GLB and any checker call this; values come from
    :func:`antigravity3d.kernel.compile_ir.read_material` and the mapping is :func:`gltf_material`. Meshes bound to
    the same material and the same double-sidedness share one ``GltfMaterial`` object (one glTF material); a mesh
    without a bound material is absent from the result.

    ``package_dir``: the package directory whose ``textures/`` the stage's relative texture paths point into (the
    stage written by ``write_engine_package``). With None the stage is a live session: textures are read from where
    ``UsdUVTexture.file`` resolves (the source files) and a DirectX normal map (``agy:normalConvention``) is
    converted to glTF's OpenGL convention. Each distinct file is read once and embedded as the bytes it holds, so a
    package GLB image is bit-identical to the texture file the USD references. A map whose file is missing is left
    out and its factor is used. ``sampler``: see :func:`gltf_material`.
    """
    base = Path(package_dir) if package_dir is not None else None
    files: dict[tuple[str, bool], EncodedImage] = {}

    def load(ref: MapSource, flip: bool) -> EncodedImage | None:
        if base is not None:
            path = base / ref.authored[2:] if ref.authored.startswith("./") else base / ref.authored
        else:
            path = _local_file(ref.resolved or ref.authored)
        if path is None or not path.is_file():
            return None
        key = (str(path.resolve()), flip)
        if key not in files:
            image = EncodedImage.from_file(path)
            files[key] = _flip_green_png(image) if flip else image
        return files[key]

    by_key: dict[tuple[Sdf.Path, bool], GltfMaterial] = {}
    by_node: dict[str, GltfMaterial] = {}
    for mesh in meshes:
        prim = stage.GetPrimAtPath(mesh.prim_path)
        mat, _ = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()
        if not mat:
            continue
        sided = bool(prim.IsA(UsdGeom.Gprim) and UsdGeom.Gprim(prim).GetDoubleSidedAttr().Get())
        key = (mat.GetPath(), sided)
        if key not in by_key:
            params = read_material(mat, double_sided=sided)
            flip = base is None and params.normal_convention == "directx"
            by_key[key] = gltf_material(
                params, lambda role, params=params, flip=flip: (ref := params.maps.get(role)) and load(ref, flip and role == "normal"),
                double_sided=sided, sampler=sampler)
        by_node[mesh.node_id] = by_key[key]
    return by_node


def material_inventory(materials: Mapping[str, GltfMaterial]) -> list[dict[str, Any]]:
    """The glTF materials a package exports, one record per distinct material, in first-use order.

    Each record states the parameters that were written, in glTF terms (``baseColorFactor`` ... plus the
    ``extensions`` payloads and the map slots that carry a texture), so a package says what it contains and a
    runtime check can compare what it loaded against this list. Derived from the objects handed to the GLB writer,
    not from the stage, so it cannot drift from the file.
    """
    out, seen = [], set()
    for m in materials.values():
        if id(m) in seen:
            continue
        seen.add(id(m))
        ext = {k: {f: (list(v) if isinstance(v, (list, tuple)) else v) for f, v in body.items()} for k, body in m.extensions.items()}
        out.append({
            "name": m.name,
            "base_color_factor": [float(v) for v in m.base_color_factor],
            "metallic_factor": float(m.metallic_factor), "roughness_factor": float(m.roughness_factor),
            "emissive_factor": [float(v) for v in m.emissive_factor] if m.emissive_factor is not None else [0.0, 0.0, 0.0],
            "alpha_mode": m.alpha_mode or "OPAQUE",
            "alpha_cutoff": float(m.alpha_cutoff) if m.alpha_cutoff is not None else None,
            "double_sided": bool(m.double_sided),
            "extensions": ext,
            "texture_slots": [slot for slot, ref in (
                ("baseColorTexture", m.base_color_texture), ("metallicRoughnessTexture", m.metallic_roughness_texture),
                ("normalTexture", m.normal_texture), ("occlusionTexture", m.occlusion_texture),
                ("emissiveTexture", m.emissive_texture)) if ref is not None],
        })
    return out


_GEOMETRY_PROPS = frozenset({
    "points", "faceVertexCounts", "faceVertexIndices", "extent", "normals", "primvars:st", "primvars:st:indices",
    "primvars:normals", "agy:uvWeldMap", "agy:uvChart",  # per-vertex / per-face data of LOD0 only
})


def _author_lod_variants(
    stage: Usd.Stage, asset: Usd.Prim, targets: list[tuple[str, np.ndarray, np.ndarray, list]], crease_deg: float = CREASE_DEG,
) -> list[int]:
    """LOD structure: variant set ``LOD`` on the asset prim, variants ``LOD0..LODn``.

    A part absent at a level (a LOD with no triangles: its removal is within the level's error budget) is
    deactivated inside that variant (``active = false``) rather than given an empty mesh: an inactive prim is
    pruned from composition, so every importer and traversal skips it and validators never see an empty
    ``points`` array, while visibility=invisible would still be imported by DCCs/engines as a mesh.
    Chosen over sibling prims (``<asset>_LOD1``...) because one switch retargets every mesh of the asset
    consistently, materials/transforms/colliders are shared instead of duplicated, and viewers that
    understand variants (usdview, Omniverse, UE's LOD variant import) expose it directly. Every variant,
    LOD0 included, holds the points/indices/st/normals of each mesh prim (see the LIVRPS note below). Default selection: ``LOD0``. Each ``targets`` entry is
    (prim path, M33, translate, [LOD0, LOD1...]); LOD points are world(asset)-space and are mapped back
    to the prim's local space with the inverse of its local-to-asset transform.
    Returns total triangles per level.
    """
    levels = min(len(t[3]) for t in targets)
    vset = asset.GetVariantSets().AddVariantSet(LOD_SET)
    for i in range(levels):
        vset.AddVariant(f"LOD{i}")
    # LIVRPS: local opinions beat variants, so the base geometry cannot stay on the prims. Move it into
    # variant LOD0 (a copy of the property specs) and strip it from the local spec; the prim keeps its
    # transform, material binding and schema type. (Hence "LOD0 is a variant", not an empty one.)
    layer = stage.GetRootLayer()
    apath = asset.GetPath()
    for path, *_ in targets:
        src = layer.GetPrimAtPath(path)
        vprim = Sdf.CreatePrimInLayer(layer, apath.AppendVariantSelection(LOD_SET, "LOD0").AppendPath(Sdf.Path(path).MakeRelativePath(apath)))
        vprim.specifier = Sdf.SpecifierOver
        for prop in [p for p in src.properties if p.name in _GEOMETRY_PROPS]:
            Sdf.CopySpec(layer, prop.path, layer, vprim.path.AppendProperty(prop.name))
            src.RemoveProperty(prop)
    for i in range(1, levels):
        vset.SetVariantSelection(f"LOD{i}")
        with vset.GetVariantEditContext():
            for path, inv33, trans, lods in targets:
                lod = shipped(lods[i])  # without the triangles hidden at this level (fast_ops.UVResult.hidden)
                if not len(lod.triangles):
                    stage.GetPrimAtPath(path).SetActive(False)
                    continue
                local = (np.asarray(lod.points, dtype=np.float64) - trans) @ inv33
                author_mesh_data(stage.GetPrimAtPath(path), local, lod.triangles, lod.uvs, weld_map=None, crease_deg=crease_deg)
    vset.SetVariantSelection("LOD0")
    return [sum(shipped(t[3][i]).tri_count for t in targets) for i in range(levels)]


def _author_colliders(stage: Usd.Stage, asset: Usd.Prim, engine: str, asset_name: str, hulls: list[Hull]) -> list[str]:
    """Convex collision hulls ``<asset>/Colliders/UCX_<asset>_<nn>`` (engine naming), purpose=guide."""
    if not hulls:
        return []
    scope = UsdGeom.Scope.Define(stage, asset.GetPath().AppendChild(COLLIDERS_SCOPE))
    names: list[str] = []
    for i, h in enumerate(hulls):
        name = collider_name(engine, asset_name, i)
        prim = UsdGeom.Mesh.Define(stage, scope.GetPath().AppendChild(name)).GetPrim()
        author_mesh_data(prim, h.points, h.triangles, None, normals=False)
        UsdGeom.Imageable(prim).CreatePurposeAttr().Set(UsdGeom.Tokens.guide)
        UsdPhysics.CollisionAPI.Apply(prim)
        UsdPhysics.MeshCollisionAPI.Apply(prim).CreateApproximationAttr().Set("convexHull")
        names.append(name)
    return names


def _apply_units(stage: Usd.Stage, asset: Usd.Prim, engine: str) -> dict[str, Any]:
    native = ENGINE_RULES[engine].native_up_axis
    units: dict[str, Any] = {
        "up_axis": native, "meters_per_unit": 1.0, "source_up_axis": "Z", "source_meters_per_unit": 1.0,
        "root_transform": None,
    }
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y if native == "Y" else UsdGeom.Tokens.z)
    if native == "Y":
        xf = UsdGeom.Xformable(asset)
        old = xf.GetOrderedXformOps()
        rot = xf.AddRotateXOp(opSuffix="zUpToYUp")
        rot.Set(-90.0)
        xf.SetXformOpOrder([rot, *old])  # outermost: applies after the asset's own ops
        units["root_transform"] = "rotateX(-90deg): (x,y,z) -> (x,z,-y)"
    return units


def _art_record(art: Any, budget: EngineBudget, textures: list[dict[str, Any]], out: Path, dens: list[float],
                uvs: Sequence[UVResult]) -> dict[str, Any]:
    """The manifest's ``art_direction`` record: the style, the values applied and what the written maps measure.
    ``validate_package`` checks the files against it (``validator._check_art_direction``).

    ``measured.texel_density_px_per_m`` is what the shipped maps really give: the UV layout's density (laid out for a
    ``UVResult.resolution`` atlas) scaled by the size of the largest map that is actually bound."""
    from antigravity3d.art_direction import count_colors

    target = art.texture_px(budget.target_engine, "prop", 0) if art.texture_px_prop is not None else None
    record = art.manifest_record(budget=budget, asset_class="prop", texture_px=target)
    sizes = [max(t["width"], t["height"]) for t in textures]
    colors = {}
    for t in textures:
        if t["role"] == "albedo" and t["path"] not in colors:
            with Image.open(out / t["path"]) as im:
                colors[t["path"]] = count_colors(im)
    atlas = max((u.resolution for u in uvs), default=0)
    record["measured"] = {
        "texture_px_max": max(sizes) if sizes else None,
        "albedo_colors": colors,
        "texel_density_px_per_m": round(float(np.mean(dens)) * max(sizes) / atlas, 2) if dens and sizes and atlas else None,
    }
    return record


# --------------------------------------------------------------------------- the writer


def _ship_lod0(stage: Usd.Stage, meshes: Sequence[MeshData], uvs: Sequence[UVResult], crease_deg: float = CREASE_DEG) -> dict[str, int]:
    """Drops each part's hidden triangles (``UVResult.hidden``) from the LOD0 geometry of the package stage (a
    copy of the working stage, whose meshes stay closed). Points, uvs, weld map and chart ids are the working
    stage's own values, indexed: the kept vertices are bit-identical to the closed mesh's. The normals are rewritten
    for the kept triangles at the shipped ``crease_deg`` (the art direction's, as for every other normal written: the
    default rewrote them at 40 degrees on a part that lost faces, which the art_crease check rejects). Returns removed
    triangles per part."""
    removed: dict[str, int] = {}
    for m, u in zip(meshes, uvs):
        if u.hidden is None or not u.hidden.any():
            continue
        prim = stage.GetPrimAtPath(m.prim_path)
        mesh = UsdGeom.Mesh(prim)
        pts = np.asarray(mesh.GetPointsAttr().Get(), np.float64)
        if len(pts) != len(u.points):
            raise ValueError(f"{m.prim_path}: package mesh has {len(pts)} points, its unwrap {len(u.points)}")
        s = shipped(u)
        weld = prim.GetAttribute(compile_ir.UV_WELD_KEY).Get() if prim.HasAttribute(compile_ir.UV_WELD_KEY) else None
        author_mesh_data(prim, pts[s.vertices], s.triangles, s.uvs,
                         weld_map=None if weld is None else np.asarray(weld, np.int32)[s.vertices], crease_deg=crease_deg)
        if prim.HasAttribute(compile_ir.UV_CHART_KEY):
            attr = prim.GetAttribute(compile_ir.UV_CHART_KEY)
            chart = attr.Get()
            if chart is not None and len(chart) == len(u.triangles):
                attr.Set(Vt.IntArray.FromNumpy(np.asarray(chart, np.int32)[~u.hidden]))
        removed[u.node_id] = int(u.hidden.sum())
    return removed


def _contact_record(budget: EngineBudget, uvs: Sequence[UVResult], lods: Mapping[str, list], removed: Mapping[str, int],
                    lod_tris: Sequence[int]) -> dict[str, Any]:
    """Manifest entry: which triangles of the authored solids are not shipped, so a reader can tell the render
    meshes are not the closed parts (colliders are made from the closed parts)."""
    rule = ENGINE_RULES.get(budget.target_engine)
    per_level = {n: [int(lv.hidden.sum()) if getattr(lv, "hidden", None) is not None else 0 for lv in chain]
                 for n, chain in lods.items()}
    return {
        "rule": "triangles of a closed opaque part covered completely by opposite-facing coplanar faces of other "
                "closed opaque parts of this asset are left out of the render meshes (fast_ops.run_export_pipeline)",
        "engine_requires_closed_meshes": bool(rule and rule.closed_render_meshes),
        "kept_by_request": bool(budget.keep_contact_faces),  # EngineBudget.keep_contact_faces (--keep-contact-faces)
        "caveat": "each part keeps its own prim in the USD (and its own GLB node unless parts are batched per material): "
                  "a part moved away from where it was authored shows an opening where it touched another part. "
                  "Export with keep_contact_faces to ship the closed parts.",
        "authored_tris": sum(len(u.triangles) for u in uvs),
        "removed_tris": sum(removed.values()),
        "removed_per_part": dict(sorted(removed.items())),
        "removed_per_level": [sum(c[i] for c in per_level.values() if i < len(c)) for i in range(len(lod_tris))]
                             if per_level else [sum(removed.values())],
        "render_meshes_closed": not removed and not any(any(c) for c in per_level.values()),
    }


def write_engine_package(
    stage_manager,
    bundle_or_parts: ExportBundle | Mapping[str, Any],
    budget: EngineBudget,
    out_dir: str | Path,
    *,
    asset_name: str,
    qa: dict | None = None,
    art: Any = None,
) -> PackageManifest:
    """Writes the package (see module docstring) and validates it cold. Idempotent per directory.

    ``stage_manager`` is the working :class:`StageManager`; the bundle's UVs are authored into it first
    (overlay, not a transaction: see usd_stage.py), so the stage, the exported USD and the GLB carry the
    same unwrapped topology. ``bundle_or_parts`` is an ``ExportBundle`` or a mapping with ``uvs``,
    ``lods`` and ``colliders``.

    The package is a function of the scene and the budget: two exports of the same scene are byte-identical,
    manifest included. How long the kernels took is run telemetry (``ExportBundle.timings_ms``, the pipeline's
    step durations), not package content, so it is not written here.

    ``art``: the :class:`antigravity3d.art_direction.ArtDirection` the asset was built with, or None. It sets the
    shading crease of every normal written (USD LOD0 and LOD variants, GLB levels, inspector) and the texture sampler
    of every GLB texture, and adds the ``art_direction`` record (schema_version 3) with the values applied and what
    was measured on the written maps. ``budget`` is then already the effective budget (``ArtDirection.budget``).
    With None nothing differs from a package written before art directions existed.
    """
    uvs, lods, hulls = _parts(bundle_or_parts)
    crease = art.crease_deg() if art is not None else CREASE_DEG
    if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", asset_name):
        raise ValueError("package asset_name must be a USD identifier")
    qa = dict(qa or {})
    engine = budget.target_engine
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    _clean_previous(out)
    notes: list[str] = []

    stage_manager.ensure_export_uvs(uvs, crease_deg=crease)
    meshes = stage_manager.extract_meshes()
    xc = UsdGeom.XformCache()
    src_stage = stage_manager.stage

    layer = stage_manager.export_layer()
    stage = Usd.Stage.Open(layer)
    asset = stage.GetDefaultPrim()
    removed = _ship_lod0(stage, meshes, uvs, crease)

    textures, unresolved = _package_textures(stage, out, engine, art)
    if unresolved:
        notes.append(f"{len(unresolved)} texture path(s) not found on disk, left unchanged: {unresolved[:3]}")

    # LODs (variant set) -- only when every mesh has a matching chain of >= 2 levels
    lod_targets = []
    for m, u in zip(meshes, uvs):
        chain = lods.get(u.node_id)
        if not chain or chain[0].tri_count != len(u.triangles):
            lod_targets = []
            break
        mat = np.array(xc.GetLocalToWorldTransform(src_stage.GetPrimAtPath(m.prim_path)))
        lod_targets.append((m.prim_path, np.linalg.inv(mat[:3, :3]), mat[3, :3], chain))
    ship = {u.node_id: shipped(u) for u in uvs}  # what each part ships at LOD0 (all of it unless hidden faces)
    ship_lods = {n: [shipped(lv) for lv in chain] for n, chain in lods.items()}
    levels = min((len(t[3]) for t in lod_targets), default=0)
    if levels > 1:
        lod_tris = _author_lod_variants(stage, asset, lod_targets, crease)
    else:
        lod_tris = [sum(len(s.triangles) for s in ship.values())]
        if lods and not lod_targets:
            notes.append("LOD chains do not match the unwrapped meshes; LODs omitted from the USD")
    names = _author_colliders(stage, asset, engine, asset_name, hulls)
    units = _apply_units(stage, asset, engine)

    files: list[str] = []
    usd_path = out / f"{asset_name}.usd"
    stage.GetRootLayer().Export(str(usd_path))
    files.append(usd_path.name)

    usdz_name: str | None = None
    usdz_skipped: str | None = None
    try:
        if create_usdz(usd_path, out / f"{asset_name}.usdz"):
            usdz_name = f"{asset_name}.usdz"
            files.append(usdz_name)
        else:
            usdz_skipped = "UsdUtils.CreateNewUsdzPackage returned False"
    except Exception as e:  # noqa: BLE001 - pxr raises Tf.ErrorException
        usdz_skipped = f"{type(e).__name__}: {e}"[:200]

    materials = gltf_materials(stage, meshes, package_dir=out, sampler=art.sampler() if art is not None else None)
    # Preserve selectable nodes where the draw budget allows it. Static props whose
    # node count exceeds the budget are submitted in one world-space batch per material, split where the engine
    # limits the triangles of one mesh (Roblox) so that no shipped mesh is over it (see ``split_batch``).
    batches = {u.node_id: [u.node_id] for u in uvs}
    if len(batches) > budget.max_draw_calls:
        groups: dict[str | None, list[str]] = {}
        for mesh in meshes:
            groups.setdefault(mesh.material_id, []).append(mesh.node_id)
        cap = ENGINE_RULES[engine].max_tris_per_mesh
        chunks = [chunk for members in groups.values() for chunk in split_batch(members, {n: len(ship[n].triangles) for n in members}, cap)]
        batches = {f"Batch_{i}": members for i, members in enumerate(chunks)}
    # Each texture file is hashed once: the hash is the manifest's, and (as the content digest the GLB writer
    # de-duplicates images by) it tells which file a material image is, so far LODs can reference that file.
    texture_files = list(dict.fromkeys(t[key] for t in textures for key in ("path", "usd_path")))
    texture_hashes = {rel: _sha256(out / rel) for rel in texture_files}
    texture_of: dict[str, str] = {}
    for rel, digest in texture_hashes.items():
        texture_of.setdefault(digest, rel)  # identical files: the first one, in manifest order
    _write_glb_level(out / f"{asset_name}.glb", ship, batches, materials, "", crease_deg=crease)
    files.append(f"{asset_name}.glb")
    glb_levels = levels
    # A material image whose bytes no package file holds (a map transcoded to PNG because glTF allows only PNG and
    # JPEG) has no file to reference and stays embedded; package maps are PNG or JPEG, so in practice none does.
    lod_uris = {digest: quote(rel) for digest, rel in texture_of.items()}
    for i in range(1, glb_levels):
        suffix = f"_LOD{i}"
        _write_glb_level(out / f"{asset_name}{suffix}.glb", {n: chain[i] for n, chain in ship_lods.items()},
                         batches, materials, suffix, image_uris=lod_uris, crease_deg=crease)
        files.append(f"{asset_name}{suffix}.glb")
    if hulls:
        write_gltf(out / f"{asset_name}_colliders.glb", hulls, [collider_name(engine, asset_name, i, usd=False) for i in range(len(hulls))])
        files.append(f"{asset_name}_colliders.glb")
    glb_files = list(files[files.index(f"{asset_name}.glb"):])
    files.extend(texture_files)
    roblox_record = None
    if engine == "roblox_r15":
        allp = np.vstack([np.asarray(u.points, float).reshape(-1, 3) for u in uvs if len(u.points)]) if uvs else np.zeros((1, 3))
        roblox_record, roblox_files = _roblox_surface_maps(out, textures, float(np.ptp(allp, axis=0).max()), len(batches))
        files.extend(f for f in roblox_files if f not in files)
        notes.append("Roblox 3D Importer: its Scale Unit setting \"Sets what units the file was modeled in\" and \"By default, this is set to "
                     "Studs\" (the page lists no other option); this package is in metres (1 stud is 28 cm per the Roblox units page). "
                     "Not checked in Studio.")
        if len(batches) > 1:
            notes.append(f"{len(batches)} GLB meshes: the importer page says a glTF mesh becomes \"one MeshPart for each primitive\", so "
                         f"expect {len(batches)} MeshParts; \"Mesh objects can only have one material assigned\" (texture specifications).")

    # The inspector renders a GLB built by the same writer from the same seam-split vertices and the same
    # GltfMaterial objects as the package GLB, so what is reviewed is what ships.
    inspector_meshes = [MeshData(m.node_id, m.prim_path, ship[u.node_id].points, ship[u.node_id].triangles, m.material_id)
                        for m, u in zip(meshes, uvs, strict=True)]
    viewer = build_inspector(inspector_meshes, asset_name=asset_name, budget=budget, materials=materials,
                             uvs={n: s.uvs for n, s in ship.items()},
                             lods=dict(ship_lods) if glb_levels > 1 else None,
                             colliders=[(name, h.points, h.triangles) for name, h in zip(names, hulls)],
                             qa=InspectorQA(bool(qa.get("tier1_ok")), tier15_score=qa.get("tier15"),
                                            defects=qa.get("defects", {}), audit_deltas=qa.get("audit", [])) if qa else None,
                             draw_calls=len(batches), three_js_source=bundled_three_js(), out_path=out / "inspector.html",
                             crease_deg=crease)
    files.append(viewer.name)

    # package-level validators (UsdValidation + engine rules) on the file we actually wrote
    mesh_data = [MeshData(u.node_id, f"/{asset.GetName()}/{u.node_id}", ship[u.node_id].points, ship[u.node_id].triangles, u.atlas_id)
                 for u in uvs]
    usd_issues = validate_usd(usd_path)
    eng_issues = check_engine_compliance(Usd.Stage.Open(str(usd_path)), mesh_data, budget)
    eng_issues += batch_tri_cap_issues(engine, {b: sum(len(ship[n].triangles) for n in members) for b, members in batches.items()})
    density = texel_density_report(uvs, budget.texel_density_px_per_m)
    cap = ENGINE_RULES[engine].max_texture_px
    for mat_id, rec in density["materials"].items():  # what the shipped maps reach: the atlas density scaled by map side / packing side
        side = max((max(t["width"], t["height"]) for t in textures if t["material"] == mat_id), default=None)
        rec["map_px"] = side
        rec["shipped_px_per_m"] = round(rec["px_per_m"] * side / rec["atlas_px"], 2) if side else None
        rec["shipped_target_met"] = bool(side) and rec["shipped_px_per_m"] >= budget.texel_density_px_per_m * (1 - 1e-9)
        rec["needed_map_px"] = rec["needed_atlas_px"] if side else None  # side that reaches the target with this layout (the layout scales with it)
        rec["engine_max_texture_px"] = cap
    eng_issues += texel_density_issues(density, engine)

    dens = [u.texel_density for u in uvs if u.texel_density > 0]
    data: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "asset": asset_name,
        "target_engine": engine,
        "usd": usd_path.name,
        "inspector": viewer.name,
        "usdz": usdz_name,
        "usdz_skipped": usdz_skipped,
        "units": units,
        "normal_convention": NORMAL_CONVENTION.get(engine, "opengl"),
        "budget": {k: getattr(budget, k) for k in ("max_tris", "max_draw_calls", "max_collision_hulls", "texel_density_px_per_m")},
        "tris": sum(len(s.triangles) for s in ship.values()),
        "lod_levels": max(glb_levels, len(lod_tris)),
        "lod_tris": lod_tris,
        # largest screen fraction (bounding-sphere diameter / frame height) each level is shown at (LOD0: 1.0);
        # levels are generated to lod_diff's screen-space error budget, so a level exists only where it pays off
        "lod_screen_fractions": [1.0] + [float(min(getattr(c[i], "screen_fraction", 1.0) for c in lods.values()))
                                         for i in range(1, len(lod_tris))],
        # GLB nodes (draw calls) per level: a level drops a node when every part in it is absent there
        "lod_glb_nodes": [list(batches)] + [[b for b, nodes in batches.items() if any(len(ship_lods[n][i].triangles) for n in nodes)]
                                            for i in range(1, glb_levels)],
        "lod_draw_calls": [len(batches)] + [sum(1 for nodes in batches.values() if any(len(ship_lods[n][i].triangles) for n in nodes))
                                            for i in range(1, glb_levels)],
        "lod_structure": f"variantSet '{LOD_SET}' on /{asset.GetName()}: LOD0 (base) .. LOD{len(lod_tris) - 1}" if len(lod_tris) > 1 else None,
        "draw_calls": len(batches),
        "glb_batches": batches,
        "contact_faces": _contact_record(budget, uvs, lods, removed, lod_tris),
        "glb_bounds_m": gltf_world_bounds((mesh.points for mesh in meshes), z_up=True),
        "texel_density": {
            "target_px_per_m": budget.texel_density_px_per_m,
            "mean_px_per_m": round(float(np.mean(dens)), 2) if dens else None,
            "min_px_per_m": round(float(np.min(dens)), 2) if dens else None,
            "uv_stretch_max": round(max((u.stretch_mean for u in uvs), default=0.0), 4),
        },
        "uv": {"primvar": "st", "interpolation": "vertex", "normals": f"faceVarying (crease {crease:g} deg)",
               "texel_density": density},
        "colliders": names,
        "collider_sources": [{"name": n, "source": h.source, "reason": h.reason} for n, h in zip(names, hulls)],
        "collider_glb_nodes": [collider_name(engine, asset_name, i, usd=False) for i in range(len(hulls))],
        "collider_structure": f"/{asset.GetName()}/{COLLIDERS_SCOPE}/<name>: purpose=guide, CollisionAPI+MeshCollisionAPI convexHull" if names else None,
        "textures": textures,
        "glb_images": {f: _glb_image_records(out / f, texture_of, texture_hashes) for f in glb_files},
        "materials": material_inventory({**{u.node_id: gltf_default_material() for u in uvs}, **materials}),
        "qa": {k: v for k, v in qa.items() if k != "audit"},
        "audit": qa.get("audit", []),
        "versions": _versions(),
        "notes": notes,
        "files": files,
        "validation": {},
    }
    if roblox_record is not None:
        data["roblox_surface_appearance"] = roblox_record
    # one truth about texel density: the UV layout's figure is atlas-space (packed for UVResult.resolution); say so, and
    # give the density the shipped maps really reach beside it (a map smaller than the atlas, as on Roblox, scales it)
    side = max((max(t["width"], t["height"]) for t in textures), default=0)
    atlas_px = max((u.resolution for u in uvs), default=0)
    data["texel_density"].update({
        "space": f"atlas ({atlas_px} px packing resolution)",
        "shipped_mean_px_per_m": round(float(np.mean(dens)) * side / atlas_px, 2) if dens and side and atlas_px else None,
        "shipped_texture_px": side or None})
    if art is not None:
        data["schema_version"] = ART_SCHEMA_VERSION
        data["art_direction"] = _art_record(art, budget, textures, out, dens, uvs)
    data["file_hashes"] = {f: {"sha256": texture_hashes.get(f) or _sha256(out / f), "bytes": (out / f).stat().st_size} for f in files}
    mpath = out / "manifest.json"
    mpath.write_text(json.dumps(data, indent=1))
    pkg_issues = validate_package(out)  # cold re-open of what was just written
    issues = usd_issues + eng_issues + pkg_issues
    data["validation"] = {
        "usd": [_issue_dict(i) for i in usd_issues],
        "engine": [_issue_dict(i) for i in eng_issues],
        "package": [_issue_dict(i) for i in pkg_issues],
        "package_ok": not any(i.severity == "critical" for i in issues),
    }
    mpath.write_text(json.dumps(data, indent=1))  # the manifest is not in file_hashes, so this is stable
    return PackageManifest(out, data, issues)
