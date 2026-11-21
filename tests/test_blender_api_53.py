"""Blender 5.3 behaviour of the worker's operations, the shim and the linter, against a real Blender.

Every test here starts real Blender workers (or Blender itself) and is skipped when no binary is configured;
CI lists the names in .github/workflows/qualification.yml as required, so a silent skip fails there. The cases
come from experiments on Blender 5.3.0 (see the comments): each one failed on the code that preceded it.
"""

import gzip
import json
import math
import os
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import numpy as np
import pytest

from antigravity3d.adapters.blender_5_3 import linter
from antigravity3d.adapters.blender_5_3.pool import BlenderOpError, BlenderPool, WorkerError, find_blender

pytestmark = pytest.mark.blender
WALKER = Path(__file__).with_name("blender_walker.py")


@pytest.fixture(autouse=True)
def _need_blender():
    if not find_blender():
        pytest.skip("no Blender binary ($BLENDER_BIN or PATH)")


async def script(pool, source, **kw):
    out = await pool.call("run_script", source=source, **kw)
    assert out["executed"]
    return out["result"]


TRIANGLE = """
import bpy
me = bpy.data.meshes.new('Tri')
me.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
me.update()
ob = bpy.data.objects.new('Tri', me)
bpy.context.scene.collection.objects.link(ob)
"""
QUAD_WITH_UV = """
import bpy
me = bpy.data.meshes.new('Quad')
me.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(0, 1, 2, 3)])
layer = me.uv_layers.new(name='UV')
for loop, uv in zip(layer.data, [(0, 0), (1, 0), (1, 1), (0, 1)]):
    loop.uv = uv
me.update()
ob = bpy.data.objects.new('Quad', me)
bpy.context.scene.collection.objects.link(ob)
"""


def png_centre(path):
    """(bit depth, centre pixel values as stored) of an 8 or 16 bit RGB(A) PNG; no image library needed."""
    data = Path(path).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat = 8, b""
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        chunk = data[pos + 8:pos + 8 + n]
        pos += 12 + n
        if kind == b"IHDR":
            w, h, depth, ctype = struct.unpack(">IIBB", chunk[:10])
        elif kind == b"IDAT":
            idat += chunk
    channels = {2: 3, 6: 4}[ctype]
    bpp, stride = channels * depth // 8, w * channels * depth // 8
    raw, prev, rows, p = zlib.decompress(idat), bytearray(stride), [], 0
    for _ in range(h):
        ftype, line, p = raw[p], bytearray(raw[p + 1:p + 1 + stride]), p + 1 + stride
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if ftype == 1:
                line[i] = (line[i] + a) & 255
            elif ftype == 2:
                line[i] = (line[i] + b) & 255
            elif ftype == 3:
                line[i] = (line[i] + ((a + b) >> 1)) & 255
            elif ftype == 4:
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append(line)
        prev = line
    row, x = rows[h // 2], (w // 2) * bpp
    if depth == 8:
        return 8, list(row[x:x + channels])
    return 16, [struct.unpack(">H", row[x + 2 * k:x + 2 * k + 2])[0] for k in range(channels)]


# ------------------------------------------------------------------------------------------------ empty scene

async def test_worker_starts_from_an_empty_scene():
    # --factory-startup loads Cube, Light and Camera; everything the worker does (and every model-written script) used
    # to run next to them. The worker now resets once at start-up.
    async with BlenderPool(1, startup_timeout=120) as pool:
        seen = await script(pool, "import bpy\nsc = bpy.context.scene\n"
                                  "result = {'objects': sorted(o.name for o in bpy.data.objects), 'camera': sc.camera is None,\n"
                                  "          'lights': len(bpy.data.lights), 'cameras': len(bpy.data.cameras), 'meshes': len(bpy.data.meshes)}")
        assert seen == {"objects": [], "camera": True, "lights": 0, "cameras": 0, "meshes": 0}


async def test_render_of_a_created_object_needs_no_factory_camera_or_light():
    async with BlenderPool(1, startup_timeout=120) as pool:
        await script(pool, TRIANGLE)
        r = await pool.call("render", engine="workbench", width=24, height=24)
        assert r["image"].shape == (24, 24, 4) and np.std(r["image"][:, :, :3]) > 0.01
        names = await script(pool, "import bpy\nresult = sorted(o.name for o in bpy.data.objects)")
        assert "Cube" not in names and "Tri" in names  # the lookdev camera and lights are the worker's own


# ------------------------------------------------------------------------------------------------ geometry nodes

LIFT_TREE = {"name": "lift", "inputs": [{"name": "offset", "type": "NodeSocketFloat", "default": 0.0}],
             "nodes": [{"name": "Move", "type": "GeometryNodeTransform"}, {"name": "Vec", "type": "ShaderNodeCombineXYZ"}],
             "links": [["Group Input", "Geometry", "Move", "Geometry"], ["Group Input", "offset", "Vec", "Z"],
                       ["Vec", "Vector", "Move", "Translation"], ["Move", "Geometry", "Group Output", "Geometry"]]}
MAX_Z = ("import bpy\nob = bpy.data.objects['Tri']\nbpy.context.view_layer.update()\n"
         "ev = ob.evaluated_get(bpy.context.evaluated_depsgraph_get())\nme = ev.to_mesh()\n"
         "result = max(v.co.z for v in me.vertices)\nev.to_mesh_clear()")


async def test_real_geonodes_input_changes_the_evaluated_mesh():
    # 5.2+: mod[identifier] = v raises TypeError (it used to set an ID property); the value lives in mod.properties.inputs
    async with BlenderPool(1, startup_timeout=120) as pool:
        await script(pool, TRIANGLE)
        r = await pool.call("apply_geonodes", tree_json=json.dumps(LIFT_TREE), object_name="Tri", inputs={"offset": 2.0}, is_preview=False)
        assert r["evaluated_vertices"] == 3 and set(r["identifiers"]) == {"Geometry", "offset", "is_preview"}
        assert await script(pool, MAX_Z) == pytest.approx(2.0)
        again = await pool.call("apply_geonodes", tree_json=json.dumps(LIFT_TREE), object_name="Tri", inputs={"offset": 4.0})  # reuse path
        assert await script(pool, MAX_Z) == pytest.approx(4.0)
        read = await script(pool, "import bpy\nmod = bpy.data.objects['Tri'].modifiers['AGY_GeoNodes']\n"
                                  f"result = mod.properties.inputs.{again['identifiers']['is_preview']}.value")
        assert read is True
        with pytest.raises(BlenderOpError, match="no such input"):
            await pool.call("apply_geonodes", tree_json=json.dumps(LIFT_TREE), object_name="Tri", inputs={"offest": 1.0})


async def test_real_geonodes_geometry_input_must_be_first():
    # a modifier feeds the geometry into the first input: with another socket first the evaluated mesh was empty, silently
    tree = {**LIFT_TREE, "inputs": [{"name": "offset", "type": "NodeSocketFloat"}, {"name": "Geometry", "type": "NodeSocketGeometry"}]}
    async with BlenderPool(1, startup_timeout=120) as pool:
        await script(pool, TRIANGLE)
        with pytest.raises(BlenderOpError, match="geometry input must be first"):
            await pool.call("apply_geonodes", tree_json=json.dumps(tree), object_name="Tri")


async def test_real_geonodes_values_by_type_and_the_linter_allowed_getattr_form():
    # vector and compare nodes by socket name (5.2: one live socket per name), the input found by identifier in a script
    tree = {"name": "typed", "inputs": [{"name": "shift", "type": "NodeSocketVector", "default": [0, 0, 0]}, {"name": "flag", "type": "NodeSocketBool"}],
            "nodes": [{"name": "Move", "type": "GeometryNodeTransform"}, {"name": "Cmp", "type": "FunctionNodeCompare",
                                                                             "props": {"data_type": "VECTOR", "mode": "ELEMENT"}, "inputs": {"A": [1, 2, 3]}}],
            "links": [["Group Input", "Geometry", "Move", "Geometry"], ["Group Input", "shift", "Move", "Translation"],
                      ["Move", "Geometry", "Group Output", "Geometry"]]}
    async with BlenderPool(1, startup_timeout=120) as pool:
        await script(pool, TRIANGLE)
        await pool.call("apply_geonodes", tree_json=json.dumps(tree), object_name="Tri", inputs={"shift": [0, 0, 3]})
        assert await script(pool, MAX_Z) == pytest.approx(3.0)
        found = await script(pool, "import bpy\nmod = bpy.data.objects['Tri'].modifiers['AGY_GeoNodes']\nng = mod.node_group\nout = {}\n"
                                   "for item in ng.interface.items_tree:\n    if item.item_type == 'SOCKET' and item.in_out == 'INPUT' and item.name == 'shift':\n"
                                   "        getattr(mod.properties.inputs, item.identifier).value = (0, 0, 5)\n"
                                   "bpy.data.objects['Tri'].update_tag()\nbpy.context.view_layer.update()\n"
                                   "ev = bpy.data.objects['Tri'].evaluated_get(bpy.context.evaluated_depsgraph_get())\nme = ev.to_mesh()\n"
                                   "out['z'] = max(v.co.z for v in me.vertices)\nev.to_mesh_clear()\n"
                                   "out['cmp_a'] = list(ng.nodes['Cmp'].inputs['A'].default_value)\nresult = out")
        assert found["z"] == pytest.approx(5.0) and found["cmp_a"] == [1.0, 2.0, 3.0]


# ------------------------------------------------------------------------------------------------ bake maps

async def test_real_bake_maps_store_data_maps_as_numbers_and_colour_as_colour(tmp_path):
    # roughness 0.5 was stored as 188/255 and a flat normal as (0.735, 0.735, 1.0): the images were colour managed
    async with BlenderPool(1, startup_timeout=120) as pool:
        await script(pool, QUAD_WITH_UV)
        r = await pool.call("bake_maps", object_name="Quad", maps=["roughness", "normal", "ao", "diffuse"], resolution=8, samples=1, out_dir=str(tmp_path))
        by_map = {Path(p).stem.split("_", 1)[1]: png_centre(p) for p in r["paths"]}
        depth, rough = by_map["roughness"]
        assert depth == 8 and rough == pytest.approx([128, 128, 128], abs=1)
        depth, normal = by_map["normal"]
        assert depth == 16 and normal == pytest.approx([32768, 32768, 65535], abs=200)
        assert by_map["ao"][1] == [255, 255, 255]
        assert by_map["diffuse"][1] == pytest.approx([229, 229, 229], abs=6)  # base colour 0.8 (sRGB 231) stays encoded; stored raw it would be 204
        assert r["samples"] == 1


async def test_real_bake_maps_samples_default_is_the_scene_value_and_is_restored(tmp_path):
    async with BlenderPool(1, startup_timeout=120) as pool:
        await script(pool, QUAD_WITH_UV)
        scene_default = await script(pool, "import bpy\nresult = bpy.context.scene.cycles.samples")
        r = await pool.call("bake_maps", object_name="Quad", maps=["roughness"], resolution=4, out_dir=str(tmp_path))  # no samples given
        assert r["samples"] == scene_default
        await pool.call("bake_maps", object_name="Quad", maps=["roughness"], resolution=4, samples=3, out_dir=str(tmp_path))
        assert await script(pool, "import bpy\nresult = bpy.context.scene.cycles.samples") == scene_default
        with pytest.raises(BlenderOpError, match="samples must be at least 1"):
            await pool.call("bake_maps", object_name="Quad", maps=["roughness"], samples=0, out_dir=str(tmp_path))


async def test_real_bake_maps_failure_is_an_error_naming_the_cause(tmp_path):
    async with BlenderPool(1, startup_timeout=120) as pool:
        await script(pool, TRIANGLE)  # no UV map
        with pytest.raises(BlenderOpError, match="UV"):
            await pool.call("bake_maps", object_name="Tri", maps=["roughness"], resolution=4, samples=1, out_dir=str(tmp_path))
        assert await script(pool, "import bpy\nresult = len(bpy.data.images)") == 0  # nothing left behind


# ------------------------------------------------------------------------------------------------ splats

def write_ply(path, splat):
    names = ["x", "y", "z"] + (["f_dc_0", "f_dc_1", "f_dc_2", "opacity", "scale_0", "scale_1", "scale_2", "rot_0", "rot_1", "rot_2", "rot_3"] if splat else [])
    with open(path, "wb") as f:
        f.write(("ply\nformat binary_little_endian 1.0\nelement vertex 5\n" + "".join(f"property float {n}\n" for n in names) + "end_header\n").encode())
        for i in range(5):
            row = [i * 0.5, 0.1 * i, 0.2] + ([0.5, 0.2, 0.1, 1.0, math.log(0.05), math.log(0.05), math.log(0.05), 1, 0, 0, 0] if splat else [])
            f.write(struct.pack(f"<{len(row)}f", *row))


def write_spz(path, points=3, version=2):
    """A minimal valid SPZ: gzip of a 16-byte header, 24-bit fixed-point positions, alphas, colours, scales, rotations."""
    body = struct.pack("<IIIBBBB", 0x5053474E, version, points, 0, 12, 0, 0)
    for i in range(points):
        for c in (i * 0.5, 0.1, 0.2):
            body += struct.pack("<I", int(round(c * 4096)) & 0xFFFFFF)[:3]
    body += bytes([255]) * points + bytes([128, 100, 90]) * points + bytes([100, 100, 100]) * points
    body += (bytes([0, 0, 0, 128]) if version >= 3 else bytes([128, 128, 255])) * points
    with gzip.open(path, "wb") as f:
        f.write(body)


async def test_real_import_splat_reports_what_the_file_became(tmp_path):
    # 5.3: a splat PLY and an SPZ import as POINTCLOUD / GAUSSIAN_SPLAT; a PLY without splat attributes is a mesh
    write_ply(tmp_path / "splat.ply", True)
    write_ply(tmp_path / "plain.ply", False)
    write_spz(tmp_path / "v2.spz")
    write_spz(tmp_path / "v3.spz", version=3)
    (tmp_path / "junk.spz").write_bytes(b"not a splat")
    async with BlenderPool(1, startup_timeout=120) as pool:
        got = {}
        for name in ("splat.ply", "v2.spz", "v3.spz", "plain.ply"):
            await pool.call("reset_scene")
            got[name] = await pool.call("import_splat", path=str(tmp_path / name))
        assert got["splat.ply"]["mode"] == "gaussian_splat" and got["splat.ply"]["points"] == 5 and got["splat.ply"]["kinds"] == ["gaussian_splat"]
        assert got["v2.spz"]["mode"] == "gaussian_splat" and got["v2.spz"]["points"] == 3
        assert got["v3.spz"]["mode"] == "gaussian_splat" and got["v3.spz"]["points"] == 3
        assert got["plain.ply"]["mode"] == "mesh_points" and got["plain.ply"]["points"] == 5
        with pytest.raises(BlenderOpError, match="SPZ"):
            await pool.call("import_splat", path=str(tmp_path / "junk.spz"))
        with pytest.raises(BlenderOpError, match="unsupported splat format"):
            await pool.call("import_splat", path=str(tmp_path / "x.obj"))
        assert (await pool.call("ping"))["pong"]


# ------------------------------------------------------------------------------------------------ render, export

async def test_real_render_survives_a_scene_left_in_multilayer_output():
    # 5.0: with media_type MULTI_LAYER_IMAGE the format OPEN_EXR is not accepted; render raised TypeError
    async with BlenderPool(1, startup_timeout=120) as pool:
        await script(pool, TRIANGLE)
        await script(pool, "import bpy\ns = bpy.context.scene.render.image_settings\ns.media_type = 'MULTI_LAYER_IMAGE'\ns.file_format = 'OPEN_EXR_MULTILAYER'")
        r = await pool.call("render", engine="workbench", width=16, height=16)
        assert r["image"].shape == (16, 16, 4) and np.std(r["image"][:, :, :3]) > 0.01


async def test_real_export_usd_writes_the_scene_and_only_the_scene(tmp_path):
    from pxr import Usd, UsdGeom

    async with BlenderPool(1, startup_timeout=120) as pool:
        await script(pool, TRIANGLE)
        r = await pool.call("export_usd", path=str(tmp_path / "out.usd"))
    stage = Usd.Stage.Open(r["path"])
    meshes = [p for p in stage.Traverse() if p.IsA(UsdGeom.Mesh)]
    assert [p.GetName() for p in meshes] == ["Tri"]  # no factory cube
    assert len(UsdGeom.Mesh(meshes[0]).GetPointsAttr().Get()) == 3


# ------------------------------------------------------------------------------------------------ shim helpers

BINDCODE = "import bpy\nimg = bpy.data.images.new('t', 4, 4)\nresult = img.bindcode.width"


async def test_real_legacy_bindcode_rewrite_runs_and_the_worker_lives():
    # the rewrite called gpu.texture.from_image without gpu.init(): a segmentation fault of the whole worker
    async with BlenderPool(1, startup_timeout=120) as pool:
        pids = pool.worker_pids
        assert await script(pool, BINDCODE, legacy=True) == 4
        assert await script(pool, "import bpy\nimg = bpy.data.images.new('u', 8, 2)\nresult = bpy.agy_compat.texture_from_image(img).height") == 2
        assert (await pool.call("ping"))["pong"] and pool.worker_pids == pids


async def test_real_gpu_init_first_script_and_unguarded_script():
    async with BlenderPool(1, startup_timeout=120) as pool:
        ok = "import bpy\nimport gpu\nimport gpu.texture\ngpu.init()\ngpu.init()\nimg = bpy.data.images.new('t', 4, 4)\nresult = gpu.texture.from_image(img).width"
        assert await script(pool, ok) == 4
        with pytest.raises(BlenderOpError, match="AG023"):  # the linter stops the call that would crash the process
            await script(pool, "import bpy\nimport gpu.texture\nresult = gpu.texture.from_image(bpy.data.images.new('t', 4, 4)).width")


async def test_real_compat_helpers():
    old_engine = "'BLENDER_' + 'EEVEE_NEXT'"  # built at run time: the linter rejects the literal (AG011)
    old_solver = "'FA' + 'ST'"
    src = f"""
import bpy
sc = bpy.context.scene
bpy.agy_compat.set_engine(sc, {old_engine})
engine = sc.render.engine
ob = bpy.data.objects.new('B', bpy.data.meshes.new('B'))
sc.collection.objects.link(ob)
mod = ob.modifiers.new('bool', 'BOOLEAN')
bpy.agy_compat.set_boolean_solver(mod, {old_solver})
bpy.agy_compat.set_prop(sc, 'frame_end', 77)
bpy.agy_compat.set_prop(sc, 'my_custom_key', 3)
ob.data.materials.append(bpy.data.materials.new('M'))
slot = ob.material_slots[0]
result = {{'engine': engine, 'solver': mod.solver, 'frame_end': sc.frame_end, 'rna_get': bpy.agy_compat.get_prop(sc, 'frame_end'),
          'custom_get': bpy.agy_compat.get_prop(sc, 'my_custom_key'), 'is_idprop': 'my_custom_key' in sc.keys(), 'default': bpy.agy_compat.get_prop(sc, 'nope', 9),
          'rename_modifier': bpy.agy_compat.safe_set_name(mod, 'renamed'), 'modifier_name': mod.name,
          'rename_slot': bpy.agy_compat.safe_set_name(slot, 'x'),
          'asset_type': bpy.agy_compat.asset_representation_type() is bpy.types.AssetRepresentation}}
"""
    async with BlenderPool(1, startup_timeout=120) as pool:
        r = await script(pool, src)
    assert r == {"engine": "BLENDER_EEVEE", "solver": "FLOAT", "frame_end": 77, "rna_get": 77, "custom_get": 3, "is_idprop": True, "default": 9,
                 "rename_modifier": True, "modifier_name": "renamed", "rename_slot": False, "asset_type": True}


async def test_real_pass_name_map_names_sockets_that_exist():
    src = """
import bpy
sc = bpy.context.scene
sc.render.engine = 'CYCLES'
sc.compositing_node_group = bpy.data.node_groups.new('c', 'CompositorNodeTree')
rl = sc.compositing_node_group.nodes.new('CompositorNodeRLayers')
vl = sc.view_layers[0]
vl.use_pass_z = True
vl.use_pass_diffuse_color = vl.use_pass_diffuse_direct = vl.use_pass_diffuse_indirect = True
vl.use_pass_glossy_color = vl.use_pass_glossy_direct = vl.use_pass_glossy_indirect = True
vl.use_pass_transmission_color = vl.use_pass_transmission_direct = vl.use_pass_transmission_indirect = True
vl.use_pass_emit = vl.use_pass_environment = vl.use_pass_ambient_occlusion = True
vl.use_pass_object_index = vl.use_pass_material_index = True
bpy.context.view_layer.update()
result = {'mapped': {old: bpy.agy_compat.pass_name(old) for old in %r}, 'old_present': [o for o in %r if rl.outputs.get(o) is not None],
          'new_present': {old: rl.outputs.get(bpy.agy_compat.pass_name(old)) is not None for old in %r}, 'identity': bpy.agy_compat.pass_name('Diffuse Color')}
"""
    from antigravity3d.adapters.blender_5_3.shim_5_3 import RENDER_PASS_MAP

    olds = sorted(RENDER_PASS_MAP)
    async with BlenderPool(1, startup_timeout=120) as pool:
        r = await script(pool, src % (olds, olds, olds))
    assert r["old_present"] == [] and r["mapped"] == RENDER_PASS_MAP and r["identity"] == "Diffuse Color"
    assert all(r["new_present"].values()), r["new_present"]  # every right-hand name is a socket of a Cycles Render Layers node


# ------------------------------------------------------------------------------------------------ the linter against Blender

def run_walker(tmp_path, rules):
    table = tmp_path / "rules.json"
    table.write_text(json.dumps(rules))
    cmd = [find_blender(), "-b", "--factory-startup", "--python-exit-code", "1", "--python", str(WALKER), "--", str(table)]
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    lines = [ln for ln in done.stdout.splitlines() if ln.startswith("WALKER ")]
    assert lines, f"the cross-check did not report (rc {done.returncode}):\n{done.stdout[-1500:]}\n{done.stderr[-1500:]}"
    return json.loads(lines[-1][7:])


def test_linter_tables_match_blender(tmp_path):
    """Every name the linter calls removed is gone in the installed Blender and every replacement its hint names exists."""
    report = run_walker(tmp_path, linter.describe_rules())
    assert report["checked"] == len(linter.describe_rules()) and report["checked"] > 60
    assert report["failures"] == [], "\n".join(report["failures"])


def test_the_cross_check_catches_a_name_that_still_works(tmp_path):
    # the check is not vacuous: names that still work in 5.3 (and a replacement that does not exist) are reported
    rules = [r for r in linter.describe_rules() if r["old"] in ("use_auto_smooth", "Subsurface")]
    rules += [{"code": "AG015", "kind": "attr", "old": "location", "new": ["rna:Mesh.no_such_thing"], "hint": "", "owner": ""},
              {"code": "AG015", "kind": "attr", "old": "blend_method", "new": [], "hint": "", "owner": ""},
              {"code": "AG018", "kind": "rl_out", "old": "Depth", "new": ["socket:Depth"], "hint": "", "owner": "CompositorNodeRLayers"}]
    failures = run_walker(tmp_path, rules)["failures"]
    assert len(failures) == 4 and any("'location'" in f and "still exists" in f for f in failures)
    assert any("no_such_thing" in f for f in failures) and any("'blend_method'" in f for f in failures) and any("'Depth'" in f for f in failures)


def test_the_four_names_that_still_work_are_not_linted():
    still_working = "import bpy\nm = bpy.data.materials['m']\nm.blend_method = 'BLEND'\ns = bpy.context.scene.sequence_editor.sequences_all[0]\nn = s.frame_final_duration\n" \
                    "bpy.ops.pose.convert_rotation()\nbpy.ops.anim.convert_legacy_action()\n"
    assert linter.lint_script(still_working) == []


WITNESSES = {
    # linter code -> a snippet that must raise in Blender, and a corrected one that must not
    "AG016": ("import bpy\nob = bpy.data.objects['W']\nmod = ob.modifiers.new('g', 'NODES')\nmod['Socket_1'] = 1.0\n",
              "import bpy\nob = bpy.data.objects['W']\nmod = ob.modifiers.new('g', 'NODES')\nng = bpy.data.node_groups.new('g', 'GeometryNodeTree')\n"
              "ng.interface.new_socket(name='G', in_out='INPUT', socket_type='NodeSocketGeometry')\n"
              "ng.interface.new_socket(name='x', in_out='INPUT', socket_type='NodeSocketFloat')\nmod.node_group = ng\nmod.properties.inputs.Socket_1.value = 1.0\n"),
    "AG022": ("import bpy\ns = bpy.context.scene.render.image_settings\ns.file_format = 'OPEN_EXR_MULTILAYER'\n",
              "import bpy\ns = bpy.context.scene.render.image_settings\ns.media_type = 'MULTI_LAYER_IMAGE'\ns.file_format = 'OPEN_EXR_MULTILAYER'\n"),
    "AG022b": ("import bpy\ns = bpy.context.scene.render.image_settings\ns.file_format = 'FFMPEG'\n",
               "import bpy\ns = bpy.context.scene.render.image_settings\ns.media_type = 'VIDEO'\ns.file_format = 'FFMPEG'\n"),
}


@pytest.mark.parametrize("key", sorted(WITNESSES))
def test_structural_rules_match_what_blender_does(tmp_path, key):
    bad, good = WITNESSES[key]
    code = key[:5]
    assert code in {i.code for i in linter.lint_script(bad)} and not [i for i in linter.lint_script(good) if i.severity == "error"]
    prelude = "import bpy\nbpy.ops.wm.read_factory_settings(use_empty=True)\nbpy.context.scene.collection.objects.link(bpy.data.objects.new('W', bpy.data.meshes.new('W')))\n"
    results = {}
    for label, src in (("bad", bad), ("good", good)):
        path = tmp_path / f"{key}_{label}.py"
        path.write_text(prelude + "try:\n    exec(compile(%r, 'w', 'exec'), {})\n    print('WIT ok')\nexcept Exception as e:\n    print('WIT', type(e).__name__)\n" % src)
        done = subprocess.run([find_blender(), "-b", "--factory-startup", "--python-exit-code", "1", "--python", str(path)], capture_output=True, text=True, timeout=120)
        results[label] = [ln for ln in done.stdout.splitlines() if ln.startswith("WIT ")]
    assert results["bad"] and results["bad"][0] != "WIT ok", f"{key}: the flagged snippet worked: {results}"
    assert results["good"] == ["WIT ok"], f"{key}: the corrected snippet failed: {results}"


def test_the_specular_bsdf_node_still_has_a_specular_input(tmp_path):
    # why AG017 does not flag 'Specular' on a variable bound to another node type: in 5.3.0 this node has that input
    src = "import bpy\nnt = bpy.data.materials.new('m').node_tree\nspec_bsdf = nt.nodes.new('ShaderNodeEeveeSpecular')\nspec_bsdf.inputs['Specular'].default_value = (1, 1, 1, 1)\n"
    assert not [i for i in linter.lint_script(src) if i.severity == "error"]
    path = tmp_path / "specular.py"
    path.write_text(src + "print('SPEC ok', flush=True)\n")
    done = subprocess.run([find_blender(), "-b", "--factory-startup", "--python-exit-code", "1", "--python", str(path)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0 and "SPEC ok" in done.stdout, done.stdout[-500:] + done.stderr[-500:]


def test_gpu_calls_without_init_do_not_work_in_background(tmp_path):
    # AG023's evidence: without gpu.init() the call does not succeed (5.3.0: from_image kills the process)
    path = tmp_path / "gpu_noinit.py"
    path.write_text("import bpy, gpu, gpu.texture\nprint('GPU', gpu.texture.from_image(bpy.data.images.new('t', 4, 4)).width, flush=True)\n")
    done = subprocess.run([find_blender(), "-b", "--factory-startup", "--python-exit-code", "1", "--python", str(path)], capture_output=True, text=True, timeout=120)
    assert done.returncode != 0 and "GPU 4" not in done.stdout
    path.write_text("import bpy, gpu, gpu.texture\ngpu.init()\nprint('GPU', gpu.texture.from_image(bpy.data.images.new('t', 4, 4)).width, flush=True)\n")
    done = subprocess.run([find_blender(), "-b", "--factory-startup", "--python-exit-code", "1", "--python", str(path)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0 and "GPU 4" in done.stdout, done.stderr[-500:]


# ------------------------------------------------------------------------------------------------ kit with a splat

async def test_kit_pipeline_imports_the_splat_with_a_real_worker(tmp_path):
    # EnvironmentKitDAG(pool=...) with a real worker: the splat_import step runs the native importer and keeps what came back
    from antigravity3d.dags.environment_kit import EnvironmentKitDAG
    from antigravity3d.genmedia.nano_banana import FakeImageBackend, ImageForge
    from antigravity3d.state import EngineBudget
    from antigravity3d.tactics.authoring import TemplateAuthor

    splat = tmp_path / "backdrop.spz"
    write_spz(splat)
    async with BlenderPool(1, startup_timeout=120) as pool:
        dag = EnvironmentKitDAG(forge=ImageForge(FakeImageBackend()), author=TemplateAuthor(), workdir=tmp_path / "kit",
                                budget=EngineBudget("unreal_5"), sheet_resolution=256, pool=pool)
        rep = await dag.run("ruined stone temple", 2, splat)
    node = rep.nodes["splat_import"]
    assert rep.ok and node.status == "ok", (node.status, node.reason, rep.summary())
    result = rep.results["splat_import"]
    assert result["mode"] == "gaussian_splat" and result["points"] == 3 and result["kinds"] == ["gaussian_splat"]


def test_kit_cli_imports_the_splat_with_a_real_blender(tmp_path):
    splat = tmp_path / "backdrop.spz"
    write_spz(splat)
    done = subprocess.run([sys.executable, "-m", "antigravity3d.cli", "kit", "ruined stone temple", "--pieces", "2", "--offline",
                           "--out", str(tmp_path / "kit"), "--splat", str(splat), "--blender", os.environ["BLENDER_BIN"]],
                          capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    assert "splat import/render requires" not in done.stdout and "gates: PASS" in done.stdout


def test_kit_cli_without_a_worker_skips_the_splat_naming_what_is_missing(tmp_path):
    splat = tmp_path / "backdrop.ply"
    write_ply(splat, True)
    done = subprocess.run([sys.executable, "-m", "antigravity3d.cli", "kit", "ruined stone temple", "--pieces", "2", "--offline",
                           "--out", str(tmp_path / "kit"), "--splat", str(splat)], capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stdout[-1500:] + done.stderr[-1500:]
    assert "dag:splat_import: splat import/render requires a real Blender pool (none provided)" in done.stdout
    assert f"splat import: NOT DONE: {splat} needs a Blender worker; pass --blender PATH or --pool-socket PATH" in done.stdout
