"""Godot 4: the files an install writes into a project, and the check that loads them in the developer's engine.

Every choice below was checked with stock Godot 4.7.2 (``Godot_v4.7.2-stable_linux.x86_64``) on the offline crate
(``agy3d prop "wooden crate with iron brackets" --offline --engine godot_4``) imported into a fresh project:

* The package's files dropped into a project as they are, with Godot's default import settings, give: LOD0's
  embedded images extracted to six new PNGs beside the GLB (``gltf/embedded_image_handling=1``) while LOD1 binds
  ``textures/``, i.e. 12 texture assets for 6 maps; every texture imported lossless (``compress/mode=0``), the
  ``textures/`` ones without mipmaps (``detect_3d`` switches a texture to VRAM compression only when the editor GUI
  sees it used in 3D, which a headless or CI import never does); ``meshes/generate_lods=true`` adding Godot's own
  LOD to a LOD0 surface on top of the package's levels; and one StaticBody3D per ``-convcolonly`` hull (8 bodies).
* Hence: LOD0 is installed referencing ``textures/`` (:func:`antigravity3d.deliver.glb.reference_images`); each
  texture gets a written ``.import`` (VRAM compressed, mipmaps, normal maps as normal maps; measured: normal maps
  import as RGTC_RG, the others as DXT1, all with mipmaps); each GLB's ``.import`` turns Godot's LOD generation off.
  A hand-written ``.import`` with only ``[remap] importer`` and ``[params]`` is honoured: Godot adds ``uid``,
  ``path`` and ``[deps]`` on import and keeps the parameters.
* Visibility ranges measure the camera's distance to the instance's world AABB centre, not its origin (measured: a
  triangle whose origin is 10.4 m from the camera and whose AABB centre is 3 m away stays visible with
  ``visibility_range_end = 5``; with ``custom_aabb`` centred on the origin it is hidden; the documentation does not
  say). Each batch of a level has its own AABB (the crate's LOD1 iron batch is 0.78 m deep, LOD0's 1.0 m), so
  without help the batches of one level would switch at different camera positions. Every level instance gets
  ``custom_aabb`` = the asset's bounds: all of them measure from one centre, the bounding-sphere centre the levels
  were generated for (``kernel/lod_diff.py``), and culling stays correct (the box contains every level).
* Hysteresis: with threshold T and margin m Godot switches at T + m going out and at T - m coming in, one level
  drawn at every step (measured on a 0.1 m sweep each way). A far level must not be shown closer than its distance
  d (its error budget holds only from there), so T = d + m: the far level appears at d + 2m going out and goes at d
  coming in.
* Collision: the hull points are written as ConvexPolygonShape3D under ONE StaticBody3D (one body to change into a
  RigidBody3D, one broadphase entry), not by importing the colliders GLB, whose ``-convcolonly`` nodes Godot makes
  into one StaticBody3D each.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from antigravity3d.deliver import glb as glbtool
from antigravity3d.kernel.lod_diff import DEFAULT_FOV_DEG, transition_distance

TEXTURE_DIR = "textures"
# Share of a switch distance used as the hysteresis margin: a camera that sits at d (a player standing still, head
# bob of a few centimetres) does not flicker. 2.5 % is 0.7 m at the crate's 29 m switch.
HYSTERESIS = 0.025
# Jolt, the physics engine of new Godot 4 projects, keeps at most 256 points per convex hull
# (kernel/validator.py ENGINE_RULES["godot_4"].hull_limit says where that is from and what was measured).
HULL_POINT_LIMIT = 256

# Godot 4.7.2's [params] for the scene importer, as it writes them for a .glb, with ours where they differ:
# meshes/generate_lods=false (the package brings its levels) and nodes/use_name_suffixes=false. With name suffixes on
# (Godot's default) a node whose name ends in -col, _col, _rigid, _wheel, _noimp ... (trailing digits stripped first)
# is turned into something else; measured with stock 4.7.2: an authored part named "rear_wheel2" imported as a
# VehicleWheel3D named "rear2" with its mesh moved under it. Part ids are the author's words, and the render levels
# carry no import instructions (collision comes from the scene), so suffixes are off; a name like "handle__mx__grip"
# (expanded mirror and copy ids) imports unchanged either way. The edit check compares exactly these keys.
SCENE_PARAMS: dict[str, str] = {
    "nodes/root_type": '""', "nodes/root_name": '""', "nodes/apply_root_scale": "true", "nodes/root_scale": "1.0",
    "nodes/import_as_skeleton_bones": "false", "nodes/use_name_suffixes": "false", "nodes/use_node_type_suffixes": "true",
    "meshes/ensure_tangents": "true", "meshes/generate_lods": "false", "meshes/create_shadow_meshes": "true",
    "meshes/light_baking": "1", "meshes/lightmap_texel_size": "0.2", "meshes/force_disable_compression": "false",
    "animation/import": "false", "import_script/path": '""', "_subresources": "{}",
    "gltf/naming_version": "2", "gltf/embedded_image_handling": "1",
}


MATERIAL_DIR = "materials"


def scene_params(res_dest: str) -> dict[str, str]:
    """SCENE_PARAMS plus material extraction into ``DEST/materials``. Each level GLB carries its own copy of each
    glTF material, so a developer would have to make a material edit once per level. With ``materials/extract=1`` Godot
    writes each material once, as ``<material name>.tres`` mapped by its own glTF importer, and binds it from every
    level that names it (measured, 4.7.2: LOD0 and LOD1 of the atlas crate bind one StandardMaterial3D resource). A
    file already there is kept, not overwritten: an edit to it survives a reimport with the import cache deleted and
    shows on every level (measured). Godot does not create the folder ("Cannot save file" otherwise), so the install
    makes it. The .tres files are Godot's, not the install's: an update keeps them; uninstall removes the ones named
    after the installed GLBs' materials, which would otherwise point at the textures it removes."""
    return {**SCENE_PARAMS, "materials/extract": "1", "materials/extract_format": "0",
            "materials/extract_path": f'"{res_dest}/{MATERIAL_DIR}"'}


def material_names(glb_bytes: bytes) -> list[str]:
    doc, _ = glbtool._split(glb_bytes)
    return [m.get("name", f"material_{i}") for i, m in enumerate(doc.get("materials") or [])]


def texture_params(role: str, normal_convention: str) -> dict[str, str]:
    """[params] for one map. VRAM compression with mipmaps for every map (a game samples them minified); a
    normal map as a normal map (RGTC on desktop: two channels, Z rebuilt in the shader); detect_3d off because the
    decision is made here. Colour space is not an import setting in Godot 4 (the material's sampler decides)."""
    return {
        "compress/mode": "2", "compress/high_quality": "false", "compress/lossy_quality": "0.7",
        "compress/normal_map": "1" if role == "normal" else "2",  # 2 = disabled, 0 would be detect
        "mipmaps/generate": "true", "mipmaps/limit": "-1",
        "process/normal_map_invert_y": "true" if role == "normal" and normal_convention == "directx" else "false",
        "process/fix_alpha_border": "true", "process/size_limit": "0", "detect_3d/compress_to": "0",
    }


def import_file(importer: str, kind: str, params: dict[str, str]) -> str:
    head = f'[remap]\n\nimporter="{importer}"\n' + ('importer_version=1\n' if importer == "scene" else "") + f'type="{kind}"\n'
    return head + "\n[params]\n\n" + "".join(f"{k}={v}\n" for k, v in params.items())


def read_import_params(text: str) -> dict[str, str]:
    """The [params] section of a .import file as written (values as text)."""
    out: dict[str, str] = {}
    section = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            section = s[1:-1]
        elif section == "params" and "=" in s:
            k, v = s.split("=", 1)
            out[k.strip()] = v.strip()
    return out


@dataclass
class LevelPlan:
    index: int
    file: str                       # GLB in DEST
    nodes: list[str]                # mesh node paths inside the GLB's scene (as Godot names them)
    begin: float                    # visibility_range_begin (m); 0 = from the camera
    begin_margin: float
    end: float                      # visibility_range_end (m); 0 = no end
    end_margin: float
    switch_in_m: float | None       # distance this level is first shown at coming closer (None for the last)
    tris: int


@dataclass
class GodotPlan:
    files: dict[str, bytes]
    imports: dict[str, dict[str, str]]    # .import file -> the params we wrote
    levels: list[LevelPlan]
    hulls: dict[str, int]                 # shape node -> point count
    centre: list[float]
    radius_m: float
    fov_deg: float
    notes: list[str] = field(default_factory=list)
    image_rewrites: dict[str, list[dict]] = field(default_factory=dict)


def _godot_float(x: float) -> str:
    return repr(float(round(x, 6)))


def _gltf_mesh_node_paths(glb: Path) -> list[str]:
    """Paths (relative to the GLB scene root) of the mesh nodes, the names Godot's importer gives them."""
    doc, _ = glbtool._split(glb.read_bytes())
    nodes = doc.get("nodes") or []
    parent: dict[int, int] = {c: i for i, n in enumerate(nodes) for c in n.get("children", [])}

    def path(i: int) -> str:
        chain = [nodes[i].get("name", f"node_{i}")]
        while i in parent:
            i = parent[i]
            chain.append(nodes[i].get("name", f"node_{i}"))
        return "/".join(reversed(chain))

    return [path(i) for i, n in enumerate(nodes) if "mesh" in n]


def level_files(manifest: dict[str, Any]) -> list[str]:
    asset = manifest["asset"]
    files = set(manifest.get("files") or [])
    far = sorted((f for f in files if re.fullmatch(re.escape(asset) + r"_LOD\d+\.glb", f)),
                 key=lambda f: int(re.search(r"_LOD(\d+)\.glb$", f).group(1)))  # type: ignore[union-attr]
    return [f"{asset}.glb", *far]


def build_plan(pkg: Path, manifest: dict[str, Any], rel_dest: str, *, fov_deg: float = DEFAULT_FOV_DEG) -> GodotPlan:
    """Every file the install writes, as bytes, relative to DEST (nothing written here). Every package file is read
    through deliver/paths (a plain relative name, no symbolic link, inside the package): an UnsafePath, a ValueError,
    names the entry."""
    from antigravity3d.deliver.paths import asset_name, inside, safe_name

    def src(rel: str) -> Path:
        return inside(pkg, safe_name(rel, "manifest file"), "manifest file", write=False)

    asset = asset_name(manifest["asset"])
    res_dest = "res://" + rel_dest.strip("/")
    files: dict[str, bytes] = {}
    imports: dict[str, dict[str, str]] = {}
    notes: list[str] = []
    convention = manifest.get("normal_convention") or "opengl"

    # textures: every package file under textures/, with its role from the manifest's records
    roles = {t["path"]: t["role"] for t in manifest.get("textures") or []}
    roles.update({t["usd_path"]: t["role"] for t in manifest.get("textures") or [] if t.get("usd_path")})
    tex = [f for f in manifest.get("files") or [] if f.startswith(TEXTURE_DIR + "/")]
    uri_of: dict[str, str] = {}
    for rel in tex:
        data = src(rel).read_bytes()
        files[rel] = data
        uri_of.setdefault(hashlib.sha256(data).hexdigest(), rel)
        role = roles.get(rel, "albedo")
        imports[rel + ".import"] = texture_params(role, convention)
        files[rel + ".import"] = import_file("texture", "CompressedTexture2D", imports[rel + ".import"]).encode()

    lv_files = level_files(manifest)
    missing = [f for f in lv_files if not src(f).is_file()]
    if missing:
        raise ValueError(f"package is missing level file(s) {missing}")
    rewrites: dict[str, list[dict]] = {}
    for f in lv_files:
        data, report = glbtool.reference_images(src(f), {k: v for k, v in uri_of.items()})
        left = [r for r in report if r["storage"] == "embedded"]
        if left:
            # Godot would extract them into DEST as files this install did not write; refuse instead of shipping that
            raise ValueError(f"{f}: image(s) {[r['image'] for r in left]} have no file in the package's textures/ "
                             f"with the same bytes (manifest glb_images)")
        from urllib.parse import unquote

        unresolved = [r["uri"] for r in report if unquote(r["uri"] or "") not in files]
        if unresolved:  # every level must bind the one copy of each map that is installed
            raise ValueError(f"{f}: image URI(s) {unresolved} are not files of the package's textures/")
        rewrites[f] = report
        files[f] = data
        params = scene_params(res_dest)
        imports[f + ".import"] = params
        files[f + ".import"] = import_file("scene", "PackedScene", params).encode()

    bounds = np.asarray(manifest["glb_bounds_m"], np.float64)
    centre = (bounds[0] + bounds[1]) / 2
    radius = float(np.linalg.norm(bounds[1] - bounds[0]) / 2)  # lod_diff: half the bbox diagonal of the reference LOD
    fractions = list(manifest.get("lod_screen_fractions") or [1.0])
    lod_tris = list(manifest.get("lod_tris") or [manifest["tris"]])
    if len(fractions) < len(lv_files):
        raise ValueError(f"manifest lod_screen_fractions has {len(fractions)} entries for {len(lv_files)} level files")
    dist = [0.0] + [transition_distance(radius, fractions[i], fov_deg) for i in range(1, len(lv_files))]
    margin = [d * HYSTERESIS for d in dist]
    levels: list[LevelPlan] = []
    for i, f in enumerate(lv_files):
        last = i == len(lv_files) - 1
        # T = d + m with margin m (see the module docstring): never shown closer than d
        begin, bm = (dist[i] + margin[i], margin[i]) if i else (0.0, 0.0)
        end, em = (0.0, 0.0) if last else (dist[i + 1] + margin[i + 1], margin[i + 1])
        nodes = _gltf_mesh_node_paths(src(f))
        if not nodes:
            raise ValueError(f"{f} has no mesh nodes")
        levels.append(LevelPlan(i, f, nodes, begin, bm, end, em, None if last else dist[i + 1],
                                int(lod_tris[i]) if i < len(lod_tris) else -1))

    # collision
    hull_names = list(manifest.get("collider_glb_nodes") or [])
    colliders_file = f"{asset}_colliders.glb"
    hulls: dict[str, np.ndarray] = {}
    if hull_names:
        if not src(colliders_file).is_file():
            raise ValueError(f"manifest names {len(hull_names)} collision hull(s) but {colliders_file} is missing")
        hulls = glbtool.hull_points(src(colliders_file), hull_names)
    else:
        notes.append("the package has no collision hulls (manifest collider_glb_nodes is empty): the scene has no StaticBody3D")

    aabb = f"AABB({', '.join(_godot_float(v) for v in [*bounds[0], *(bounds[1] - bounds[0])])})"
    ext = [(f"lod{lv.index}", lv.file) for lv in levels]
    sub = []
    shape_nodes: dict[str, int] = {}
    for k, name in enumerate(hull_names):
        pts = hulls[name]
        flat = ", ".join(_godot_float(v) for v in pts.reshape(-1))
        sub.append(f'[sub_resource type="ConvexPolygonShape3D" id="hull_{k:02d}"]\npoints = PackedVector3Array({flat})\n')
        shape_nodes[re.sub(r"-(conv)?col(only)?$", "", name)] = len(pts)
    out = [f"[gd_scene load_steps={1 + len(ext) + len(sub)} format=3]\n"]
    out += [f'[ext_resource type="PackedScene" path="{res_dest}/{f}" id="{i}"]\n' for i, f in ext]
    out += sub
    meta = json.dumps({"tool": "antigravity3d", "asset": asset, "fov_deg": fov_deg,
                       "switch_m": [lv.switch_in_m for lv in levels[:-1]]}).replace('"', '\\"')
    out.append(f'[node name="{asset}" type="Node3D"]\nmetadata/agy3d = "{meta}"\n')
    for lv in levels:
        out.append(f'[node name="LOD{lv.index}" parent="." instance=ExtResource("lod{lv.index}")]\n')
        for node in lv.nodes:
            props = [f"custom_aabb = {aabb}"]
            if lv.begin:
                props += [f"visibility_range_begin = {_godot_float(lv.begin)}", f"visibility_range_begin_margin = {_godot_float(lv.begin_margin)}"]
            if lv.end:
                props += [f"visibility_range_end = {_godot_float(lv.end)}", f"visibility_range_end_margin = {_godot_float(lv.end_margin)}"]
            parent, _, name = f"LOD{lv.index}/{node}".rpartition("/")
            out.append(f'[node name="{name}" parent="{parent}"]\n' + "\n".join(props) + "\n")
    if hull_names:
        out.append('[node name="Collision" type="StaticBody3D" parent="."]\n')
        for k, name in enumerate(shape_nodes):
            out.append(f'[node name="{name}" type="CollisionShape3D" parent="Collision"]\nshape = SubResource("hull_{k:02d}")\n')
    files[f"{asset}.tscn"] = "\n".join(out).encode()
    if abs(fov_deg - DEFAULT_FOV_DEG) > 1e-9:
        notes.append(f"switch distances use a {fov_deg:g} degree vertical field of view (the package's levels were "
                     f"generated for {DEFAULT_FOV_DEG:g})")
    return GodotPlan(files, imports, levels, shape_nodes, centre.tolist(), radius, fov_deg, notes, rewrites)


# --------------------------------------------------------------------------- the in-project check

_CHECK_GD = r'''extends SceneTree
# antigravity3d install check: loads the installed scene and prints what a developer would look at.
func _init():
    call_deferred("run")

func tri_count(mi: MeshInstance3D) -> int:
    var n := 0
    var m: Mesh = mi.mesh
    if m == null:
        return 0
    for s in m.get_surface_count():
        var a := m.surface_get_arrays(s)
        var idx = a[Mesh.ARRAY_INDEX]
        n += (idx.size() if idx != null else a[Mesh.ARRAY_VERTEX].size()) / 3
    return n

func tex_info(t) -> Variant:
    if t == null:
        return null
    var im: Image = t.get_image()
    return {"path": t.resource_path, "format": (im.get_format() if im else -1), "compressed": (im.is_compressed() if im else false), "mipmaps": (im.has_mipmaps() if im else false)}

func run():
    var out := {"engine_version": Engine.get_version_info().string, "physics_engine": str(ProjectSettings.get_setting("physics/3d/physics_engine"))}
    var packed = load(OS.get_environment("AGY_SCENE"))
    if packed == null:
        out["load_error"] = "scene did not load"
        print("AGY_DELIVER ", JSON.stringify(out)); quit(1); return
    var scene: Node = packed.instantiate()
    root.add_child(scene)
    var levels := []
    for child in scene.get_children():
        if not child.name.begins_with("LOD"):
            continue
        var meshes := []
        var lo := Vector3.ZERO
        var hi := Vector3.ZERO
        var first := true
        for mi in child.find_children("*", "MeshInstance3D", true, false):
            var surfaces := []
            for s in mi.mesh.get_surface_count():
                var mat = mi.get_active_material(s)
                var rec := {"material": (mat.resource_name if mat else null), "resource": (mat.resource_path if mat else null), "class": (mat.get_class() if mat else null), "lods": RenderingServer.mesh_get_surface(mi.mesh.get_rid(), s).get("lods", []).size()}
                if mat is BaseMaterial3D:
                    rec["albedo"] = tex_info(mat.albedo_texture)
                    rec["normal"] = tex_info(mat.normal_texture) if mat.normal_enabled else null
                    rec["roughness"] = tex_info(mat.roughness_texture)
                    rec["metallic"] = tex_info(mat.metallic_texture)
                    rec["ao"] = tex_info(mat.ao_texture) if mat.ao_enabled else null
                    rec["emission"] = tex_info(mat.emission_texture) if mat.emission_enabled else null
                surfaces.append(rec)
                var arr: Array = mi.mesh.surface_get_arrays(s)
                for v in mi.global_transform * arr[Mesh.ARRAY_VERTEX]:
                    if first:
                        lo = v; hi = v; first = false
                    else:
                        lo = lo.min(v); hi = hi.max(v)
            var ca: AABB = mi.custom_aabb
            meshes.append({"node": str(child.get_path_to(mi)), "tris": tri_count(mi), "surfaces": surfaces,
                "begin": mi.visibility_range_begin, "begin_margin": mi.visibility_range_begin_margin,
                "end": mi.visibility_range_end, "end_margin": mi.visibility_range_end_margin,
                "custom_aabb": [[ca.position.x, ca.position.y, ca.position.z], [ca.end.x, ca.end.y, ca.end.z]]})
        levels.append({"name": str(child.name), "meshes": meshes, "bounds_m": [[lo.x, lo.y, lo.z], [hi.x, hi.y, hi.z]]})
    out["levels"] = levels
    var bodies := []
    for body in scene.find_children("*", "CollisionObject3D", true, false):
        var shapes := []
        for cs in body.find_children("*", "CollisionShape3D", true, false):
            var sh = cs.shape
            shapes.append({"node": str(cs.name), "class": (sh.get_class() if sh else null), "points": (sh.points.size() if sh is ConvexPolygonShape3D else -1)})
        bodies.append({"node": str(body.name), "class": body.get_class(), "shapes": shapes})
    out["bodies"] = bodies
    print("AGY_DELIVER ", JSON.stringify(out))
    var shot := OS.get_environment("AGY_SHOT")
    if shot.is_empty():
        quit(0); return
    await render(scene, out, shot)
    quit(0)

func flat(color: Color) -> StandardMaterial3D:
    var m := StandardMaterial3D.new()
    m.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
    m.albedo_color = color
    return m

func render(scene: Node, out: Dictionary, shot: String):
    var vp := SubViewport.new()
    vp.size = Vector2i(512, 512)
    vp.own_world_3d = true
    vp.render_target_update_mode = SubViewport.UPDATE_ALWAYS
    root.add_child(vp)
    scene.reparent(vp, false)
    var cam := Camera3D.new()
    vp.add_child(cam)
    var centre := Vector3(float(OS.get_environment("AGY_CX")), float(OS.get_environment("AGY_CY")), float(OS.get_environment("AGY_CZ")))
    var radius := float(OS.get_environment("AGY_R"))
    var dir := Vector3(1.4, 1.0, 2.0).normalized()
    for spec in [[Vector3(2,4,3),2.5], [Vector3(-2,2,-1),0.8]]:
        var lamp := DirectionalLight3D.new()
        vp.add_child(lamp)
        lamp.light_energy = spec[1]
        lamp.look_at_from_position(spec[0], Vector3.ZERO)
    var env := WorldEnvironment.new()
    env.environment = Environment.new()
    env.environment.ambient_light_source = Environment.AMBIENT_SOURCE_COLOR
    env.environment.ambient_light_color = Color(0.35, 0.35, 0.4)
    vp.add_child(env)
    # 1. the developer's view: the scene as installed, close up (LOD0), materials as imported
    cam.fov = 40
    cam.position = centre + dir * radius * 3.2
    cam.look_at(centre)
    cam.make_current()
    for i in 4:
        await RenderingServer.frame_post_draw
    var img := vp.get_texture().get_image()
    if img != null and not img.is_empty():
        img.save_png(shot)
    # 2. switching: each level drawn in its own flat colour; the camera moves out past each switch and back in.
    # A narrow lens fills the frame at any distance: visibility ranges depend on distance only, not on the lens.
    var colours := [Color(1,0,0), Color(0,1,0), Color(0,0,1), Color(1,1,0), Color(1,0,1), Color(0,1,1)]
    var lv := 0
    for child in scene.get_children():
        if child.name.begins_with("LOD"):
            for mi in child.find_children("*", "MeshInstance3D", true, false):
                mi.material_override = flat(colours[lv % colours.size()])
            lv += 1
    var probes := []
    for d in JSON.parse_string(OS.get_environment("AGY_PROBES")):
        cam.position = centre + dir * float(d)
        cam.fov = clampf(rad_to_deg(2.0 * atan(radius * 1.2 / float(d))), 0.5, 120.0)
        cam.near = maxf(float(d) - radius * 2.0, 0.05)
        cam.far = float(d) + radius * 2.0
        cam.look_at(centre)
        for i in 3:
            await RenderingServer.frame_post_draw
        var frame := vp.get_texture().get_image()
        var counts := []
        for c in colours:
            counts.append(0)
        var other := 0
        for y in range(0, 512, 4):
            for x in range(0, 512, 4):
                var p := frame.get_pixel(x, y)
                var hit := -1
                for k in colours.size():
                    var c: Color = colours[k]
                    if absf(p.r - c.r) < 0.2 and absf(p.g - c.g) < 0.2 and absf(p.b - c.b) < 0.2:
                        hit = k
                        break
                if hit >= 0:
                    counts[hit] += 1
                elif p.a > 0.5 and (p.r + p.g + p.b) > 0.05:
                    other += 1
        probes.append({"distance_m": float(d), "level_pixels": counts.slice(0, lv), "other": other})
    print("AGY_DELIVER_RENDER ", JSON.stringify({"probes": probes, "screenshot": shot}))
'''


def switch_probes(switch_m: list[float]) -> list[tuple[float, int]]:
    """Camera distances, in the order visited, and the level that must be the one drawn at each: out past every
    switch and back in. At each switch d (margin m) the camera stops in the middle of the hysteresis band, d + m,
    once from each side: coming from nearer the near level must still be drawn, coming from farther the far one."""
    out: list[tuple[float, int]] = []
    for k, d in enumerate(switch_m):
        m = d * HYSTERESIS
        out += [(d * 0.97, k), (d + m, k), ((d + 2 * m) * 1.03, k + 1)]
    for k in reversed(range(len(switch_m))):
        d = switch_m[k]
        m = d * HYSTERESIS
        out += [(d + m, k + 1), (d * 0.97, k)]
    return out


def find_godot(explicit: str | None = None) -> str | None:
    """The developer's Godot: ``explicit``, else ``$GODOT_BIN``, else godot/godot4 on PATH."""
    for cand in (explicit, os.environ.get("GODOT_BIN")):
        if cand:
            return cand if os.access(cand, os.X_OK) else shutil.which(cand)
    for n in ("godot", "godot4", "Godot"):
        if p := shutil.which(n):
            return p
    return None


def godot_version(exe: str) -> str | None:
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=60).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.splitlines()[-1] if out else None


def isolated_project(project: Path, rel_dest: str, work: Path) -> tuple[Path, list[str]]:
    """A copy of the developer's project that holds only its ``project.godot`` (so its import defaults, physics
    engine and rendering settings apply) and the installed DEST, at the same res:// path. The check runs there, so
    nothing outside DEST is written in the real project (an import writes Godot's ``.godot/`` cache) and none of the
    project's code runs: a ``--script`` run starts the project's autoloads (measured, Godot 4.7.2), so the
    ``[autoload]`` and ``[editor_plugins]`` sections are left out of the copy. Returns the copy and what was left out."""
    text = (project / "project.godot").read_text(errors="replace")
    kept, dropped, section = [], [], None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            section = s[1:-1]
            if section in ("autoload", "editor_plugins"):
                dropped.append(section)
        if section not in ("autoload", "editor_plugins"):
            kept.append(line)
    root = work / "project"
    root.mkdir(parents=True)
    (root / "project.godot").write_text("\n".join(kept) + "\n")
    shutil.copytree(project / rel_dest, root / rel_dest, ignore=shutil.ignore_patterns(".agy3d"))
    return root, dropped


def _run(cmd: list[str], env: dict[str, str], timeout: float) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env={**os.environ, **env},
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        return -1, f"timed out after {timeout}s\n{(e.stdout or b'')[-2000:]!r}"
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def asset_log_lines(log: str, rel_dest: str, names: list[str]) -> list[str]:
    """ERROR / WARNING lines of a Godot log that name the installed directory or one of its files (with the
    ``at:`` line that follows each)."""
    out = []
    lines = log.splitlines()
    keys = [rel_dest, *names]
    for i, line in enumerate(lines):
        if re.match(r"\s*(ERROR|WARNING|SCRIPT ERROR)", line):
            block = line + (" | " + lines[i + 1].strip() if i + 1 < len(lines) and lines[i + 1].strip().startswith("at:") else "")
            if any(k and k in block for k in keys):
                out.append(block[:400])
    return out


def run_check(exe: str, project: Path, rel_dest: str, plan_record: dict[str, Any], work: Path, *,
              render: bool = True, timeout: float = 300.0) -> dict[str, Any]:
    """Import an isolated copy of the project with ``exe`` and load the installed scene. Returns the raw
    measurements (``import``, ``scene``, ``render``) for :func:`antigravity3d.deliver.install.godot_checks`."""
    root, dropped = isolated_project(project, rel_dest, work)
    asset = plan_record["asset"]
    names = [Path(f).name for f in plan_record["files"]]
    code, log = _run([exe, "--headless", "--path", str(root), "--import"], {}, timeout)
    result: dict[str, Any] = {"isolated_copy_left_out": dropped,
                              "import": {"exit": code, "asset_lines": asset_log_lines(log, rel_dest, names), "log_tail": log[-3000:]}}
    script = work / "agy3d_check.gd"
    script.write_text(_CHECK_GD)
    env = {"AGY_SCENE": f"res://{rel_dest}/{asset}.tscn", "AGY_SHOT": ""}
    code, log = _run([exe, "--headless", "--path", str(root), "--script", str(script)], env, timeout)
    scene = next((json.loads(line.split(" ", 1)[1]) for line in log.splitlines() if line.startswith("AGY_DELIVER {")), None)
    result["scene"] = {"exit": code, "data": scene, "asset_lines": asset_log_lines(log, rel_dest, names), "log_tail": log[-3000:]}
    display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    xvfb = shutil.which("xvfb-run")
    if not render:
        result["render"] = {"skipped": "render check not requested"}
    elif not display and not xvfb:
        result["render"] = {"skipped": "no display and no xvfb-run: Godot's renderer needs one"}
    else:
        probes = switch_probes(plan_record["switch_m"])
        shot = work / "godot.png"
        cx, cy, cz = plan_record["centre"]
        env = {**env, "AGY_SHOT": str(shot), "AGY_CX": str(cx), "AGY_CY": str(cy), "AGY_CZ": str(cz),
               "AGY_R": str(plan_record["radius_m"]), "AGY_PROBES": json.dumps([d for d, _ in probes])}
        cmd = [exe, "--rendering-method", "gl_compatibility", "--audio-driver", "Dummy", "--path", str(root), "--script", str(script)]
        if not display:
            cmd = [xvfb, "-a", *cmd]
        code, log = _run(cmd, env, timeout)
        data = next((json.loads(line.split(" ", 1)[1]) for line in log.splitlines() if line.startswith("AGY_DELIVER_RENDER {")), None)
        result["render"] = {"exit": code, "data": data, "screenshot": str(shot) if shot.is_file() else None,
                            "probes_m": probes, "log_tail": log[-2000:]}
    return result


def expected_maps(glb: Path) -> dict[str, list[str]]:
    """Per glTF material name, the map slots it binds, read from the installed GLB (never from a count)."""
    return expected_maps_bytes(glb.read_bytes())


def expected_maps_bytes(data: bytes) -> dict[str, list[str]]:
    doc, _ = glbtool._split(data)
    out: dict[str, list[str]] = {}
    for i, m in enumerate(doc.get("materials") or []):
        pbr = m.get("pbrMetallicRoughness") or {}
        slots = []
        if "baseColorTexture" in pbr:
            slots.append("albedo")
        if "metallicRoughnessTexture" in pbr:
            slots += ["roughness", "metallic"]
        if "normalTexture" in m:
            slots.append("normal")
        if "occlusionTexture" in m:
            slots.append("ao")
        if "emissiveTexture" in m:
            slots.append("emission")
        out[m.get("name", f"material_{i}")] = slots
    return out


def godot_camera_note() -> str:
    return (f"switch distances assume a {DEFAULT_FOV_DEG:g} degree vertical field of view, the one the package's levels "
            f"were generated for; Godot's Camera3D default is 75 degrees, where they switch later than needed "
            f"(about {math.tan(math.radians(37.5)) / math.tan(math.radians(30)):.2f}x the distance), never sooner")


__all__ = ["GodotPlan", "LevelPlan", "build_plan", "run_check", "find_godot", "godot_version", "expected_maps", "expected_maps_bytes",
           "read_import_params", "HULL_POINT_LIMIT", "HYSTERESIS"]
