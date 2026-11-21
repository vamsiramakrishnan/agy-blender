import pytest

from antigravity3d.adapters.blender_5_3.linter import UnsafeScriptError, assert_safe, lint_script

BAD = [
    ("import os", "AG001"),
    ("import os.path", "AG001"),
    ("import subprocess as sp\nsp.run(['ls'])", "AG001"),
    ("from os import system as s\ns('id')", "AG006"),
    ("from os import system as s", "AG001"),
    ("import shutil\nshutil.rmtree('/')", "AG006"),
    ("import socket", "AG001"),
    ("import ctypes", "AG001"),
    ("import sys", "AG001"),
    ("import pickle", "AG001"),
    ("import multiprocessing", "AG001"),
    ("import importlib", "AG001"),
    ("from . import x", "AG001"),
    ("__import__('os').system('id')", "AG002"),
    ("import importlib\nimportlib.import_module('os')", "AG002"),
    ("eval('1+1')", "AG003"),
    ("exec('x=1')", "AG003"),
    ("f = compile('1', 'x', 'eval')", "AG003"),
    ("e = eval\ne('1')", "AG003"),
    ("open('/tmp/x', 'w')", "AG004"),
    ("open('/tmp/x', mode='ab')", "AG004"),
    ("m = 'w'\nopen('/tmp/x', m)", "AG004"),
    ("import bpy\ngetattr(bpy.ops.wm, 'quit_' + 'blender')()", "AG005"),
    ("import bpy\ngetattr(bpy.ops.wm, 'quit_blender')()", "AG006"),
    ("import bpy\ngetattr(bpy, '__class__')", "AG005"),
    ("import bpy\nbpy.ops.wm.quit_blender()", "AG006"),
    ("import bpy\nbpy.ops.wm.read_factory_settings()", "AG006"),
    ("import bpy\nbpy.ops.wm.save_mainfile()", "AG006"),
    ("import bpy as b\nb.ops.wm.save_mainfile()", "AG006"),
    ("import bpy\nw = bpy.ops.wm\nw.quit_blender()", "AG006"),
    ("import bpy\nbpy.app.handlers.load_post.append(lambda *a: None)", "AG008"),
    ("from bpy.app import handlers", "AG008"),
    ("x = ().__class__.__bases__[0].__subclasses__()", "AG007"),
    ("def f(): pass\nf.__globals__", "AG007"),
    ("__builtins__", "AG007"),
    ("import bpy\nbpy.__dict__", "AG007"),
    ("import bgl", "AG010"),
    ("from bgl import glEnable", "AG010"),
    ("import bpy\nbpy.context.scene.render.engine = 'BLENDER_EEVEE_NEXT'", "AG011"),
    ("ENGINES = ['CYCLES', 'BLENDER_EEVEE_NEXT']", "AG011"),
    ("import bpy\nm = bpy.context.object.modifiers['b']\nm.solver = 'FAST'", "AG012"),
    ("setattr(m, 'solver', 'FAST')", "AG012"),
    ("import bpy\nbpy.types.AssetHandle", "AG013"),
    ("from bpy.types import AssetHandle", "AG013"),
    ("import bpy\ntex = bpy.data.images['x'].bindcode", "AG014"),
    ("def (:", "AG000"),
]


@pytest.mark.parametrize("src,code", BAD)
def test_bad_snippets(src, code):
    codes = {i.code for i in lint_script(src)}
    assert code in codes, f"{code} not in {codes} for {src!r}"
    with pytest.raises(UnsafeScriptError):
        assert_safe(src)


def test_table_size():
    assert len(BAD) >= 25


GOOD = [
    "import bpy\nbpy.ops.mesh.primitive_cube_add(size=2)\nbpy.context.object.name = 'Cube'",
    "import bpy, bmesh\nbm = bmesh.new()\nbmesh.ops.create_cube(bm, size=1.0)\nme = bpy.data.meshes.new('m')\nbm.to_mesh(me)\nbm.free()",
    "import bpy as b\nfrom mathutils import Vector, Matrix\nb.context.object.location = Vector((1, 2, 3))",
    "import math, random\nrandom.seed(3)\nr = [math.sin(i) * random.random() for i in range(10)]",
    "import bpy\nbpy.context.scene.render.engine = 'BLENDER_EEVEE'\nm = bpy.context.object.modifiers.new('b', 'BOOLEAN')\nm.solver = 'FLOAT'",
    "import numpy as np\nimport gpu\nfrom gpu_extras.batch import batch_for_shader\na = np.zeros((4, 4), dtype=np.float32)",
    "import bpy, json\nd = json.loads('{\"a\": 1}')\nbpy.context.scene['cfg'] = json.dumps(d)\nresult = d",
    "import bpy\nobj = bpy.data.objects.get('Cube')\nval = getattr(obj, 'location', None)\nhasattr(obj, 'name')",
    "import bpy\nwith open('/tmp/in.txt') as f:\n    data = f.read()\nwith open('/tmp/in.bin', 'rb') as g:\n    g.read()",
    "import bpy\nimg = bpy.data.images.new('t', 8, 8)\nimport gpu\nimport gpu.texture\ngpu.init()\ntex = gpu.texture.from_image(img)",
    "import bpy\nfor o in bpy.context.scene.objects:\n    if o.type == 'MESH':\n        o.name = o.name.upper()\nwhile True:\n    break",
    "import bpy\nops = bpy.ops.object\nops.select_all(action='DESELECT')\nbpy.ops.wm.usd_export(filepath='x.usd')",
]


@pytest.mark.parametrize("src", GOOD)
def test_good_snippets(src):
    assert lint_script(src) == []
    assert_safe(src)


def test_warning_does_not_raise():
    src = "while True:\n    pass"
    issues = lint_script(src)
    assert [i.code for i in issues] == ["AG030"] and issues[0].severity == "warning"
    assert_safe(src)


def test_issue_positions():
    (i,) = [i for i in lint_script("x = 1\nimport os") if i.code == "AG001"]
    assert (i.line, i.col) == (2, 0)


# ---- removed names (AG015-AG026): each family has flagged snippets and near misses that must stay clean.
# Every name here was checked against Blender 5.3.0 (tests/test_blender_api_53.py walks the tables in the installed Blender).

def codes(src: str) -> set[str]:
    return {i.code for i in lint_script(src) if i.severity == "error"}


FLAGGED = [
    # AG015 removed attribute / data / type names, named in the message with their replacement
    ("import bpy\nbpy.data.meshes['m'].use_auto_smooth = True", "AG015", "shade_smooth_by_angle"),
    ("import bpy\nx = bpy.data.meshes['m'].calc_normals_split()", "AG015", "corner_normals"),
    ("import bpy\nm = bpy.data.materials['m']\nm.shadow_method = 'NONE'", "AG015", "surface_render_method"),
    ("import bpy\nbpy.context.scene.eevee.use_gtao = True", "AG015", "ambient_occlusion_distance"),
    ("import bpy\nb.sculpt_tool = 'DRAW'", "AG015", "sculpt_brush_type"),
    ("import bpy\nn.file_slots.new('x')", "AG015", "file_output_items"),
    ("import bpy\nx = bpy.data.grease_pencils_v3", "AG015", "bpy.data.grease_pencils"),
    ("import bpy\nx = bpy.types.GreasePencilv3", "AG015", "bpy.types.GreasePencil"),
    ("import bpy\nx = bpy.context.tool_settings.unified_paint_settings", "AG015", "tool_settings.sculpt"),
    # AG016 Geometry Nodes modifier inputs by key
    ("import bpy\nmod = bpy.data.objects['o'].modifiers.new('g', 'NODES')\nmod['Socket_1'] = 2.0", "AG016", "properties.inputs"),
    ("import bpy\nfor m in bpy.data.objects['o'].modifiers:\n    m['Socket_2'] = 1", "AG016", "properties.inputs"),
    ("import bpy\nv = bpy.data.objects['o'].modifiers['g']['Socket_1']", "AG016", "properties.inputs"),
    # AG017 removed socket names: Principled BSDF only when the node is recognisable, Compare / Random Value identifiers always
    ("import bpy\nbsdf = nodes['Principled BSDF']\nbsdf.inputs['Specular'].default_value = 0.5", "AG017", "Specular IOR Level"),
    ("import bpy\np = nodes.new('ShaderNodeBsdfPrincipled')\np.inputs['Clearcoat Roughness'].default_value = 0.1", "AG017", "Coat Roughness"),
    ("import bpy\nnodes['Principled BSDF'].inputs.get('Emission')", "AG017", "Emission Color"),
    ("import bpy\nn.inputs['A_VEC3'].default_value = (1, 2, 3)", "AG017", "'A'"),
    ("import bpy\nr.inputs['Min_001'].default_value = 1.0", "AG017", "'Min'"),
    # AG018 old render pass names, only on a Render Layers node
    ("import bpy\nrl = nodes.new('CompositorNodeRLayers')\nx = rl.outputs['DiffCol']", "AG018", "Diffuse Color"),
    ("import bpy\nrender_layers = tree.nodes['Render Layers']\nx = render_layers.outputs.get('Z')", "AG018", "Depth"),
    ("import bpy\nx = tree.nodes['Render Layers'].outputs['AO']", "AG018", "Ambient Occlusion"),
    # AG019 removed enum values
    ("import bpy\nsky.sky_type = 'NISHITA'", "AG019", "MULTIPLE_SCATTERING"),
    ("import bpy\ns.file_format = 'AVI_JPEG'", "AG019", "FFMPEG"),
    # AG020 removed node types
    ("import bpy\ntree.nodes.new('CompositorNodeMixRGB')", "AG020", "ShaderNodeMix"),
    ("import bpy\ntree.nodes.new(type='CompositorNodeGamma')", "AG020", "ShaderNodeGamma"),
    ("import bpy\nmat.node_tree.nodes.new('ShaderNodeTexPointDensity')", "AG020", "Point Density"),
    # AG021 removed operator keywords
    ("import bpy\nbpy.ops.wm.usd_export(filepath='a.usd', export_textures=True)", "AG021", "export_textures_mode"),
    ("import bpy\nbpy.ops.wm.usd_export(filepath='a.usd', visible_objects_only=True)", "AG021", "selected_objects_only"),
    ("import bpy\nbpy.ops.export_scene.gltf(filepath='a.glb', export_image_add_webp=True)", "AG021", "export_compressed_images_type"),
    ("import bpy\nbpy.ops.export_scene.gltf(filepath='a.glb', at_collection_center=True)", "AG021", "collection_center_mode"),
    # AG022 file_format before media_type
    ("import bpy\ns = bpy.context.scene.render.image_settings\ns.file_format = 'OPEN_EXR_MULTILAYER'", "AG022", "MULTI_LAYER_IMAGE"),
    ("import bpy\ndef setup(s):\n    s.file_format = 'FFMPEG'", "AG022", "VIDEO"),
    ("import bpy\ndef a(s):\n    s.media_type = 'VIDEO'\ndef b(s):\n    s.file_format = 'FFMPEG'", "AG022", "VIDEO"),  # another function's media_type does not count
    ("import bpy\ns.file_format = 'FFMPEG'\ns.media_type = 'VIDEO'", "AG022", "VIDEO"),  # too late
    # AG023 gpu use without gpu.init()
    ("import gpu\nimport gpu.texture\nt = gpu.texture.from_image(img)", "AG023", "gpu.init()"),
    ("import gpu\nt = gpu.platform.backend_type_get()", "AG023", "gpu.init()"),
    ("import gpu\nfrom gpu.texture import from_image\nt = from_image(img)", "AG023", "gpu.init()"),
    ("import gpu\nt = gpu.state.blend_set('ALPHA')\ngpu.init()", "AG023", "gpu.init()"),  # init comes too late
    # AG024 node group sockets
    ("import bpy\nng = bpy.data.node_groups.new('g', 'GeometryNodeTree')\nng.inputs.new('NodeSocketFloat', 'x')", "AG024", "interface.new_socket"),
    ("import bpy\nbpy.data.node_groups['g'].outputs.new('NodeSocketGeometry', 'Geometry')", "AG024", "interface.new_socket"),
    # AG025 scene.node_tree
    ("import bpy\nt = bpy.context.scene.node_tree", "AG025", "compositing_node_group"),
    ("import bpy\nt = scene.node_tree.nodes", "AG025", "compositing_node_group"),
    # AG026 Action.fcurves / groups
    ("import bpy\nfc = obj.animation_data.action.fcurves", "AG026", "channelbag"),
    ("import bpy\nfor g in action.groups:\n    pass", "AG026", "channelbag"),
]


@pytest.mark.parametrize("src,code,names", FLAGGED)
def test_removed_names_are_flagged_and_the_message_names_the_replacement(src, code, names):
    issues = [i for i in lint_script(src) if i.code == code]
    assert issues, f"{code} not reported for {src!r}: {lint_script(src)}"
    assert any(names in i.message for i in issues), [i.message for i in issues]
    with pytest.raises(UnsafeScriptError):
        assert_safe(src)


NEAR_MISSES = [
    # the same words where they still work or belong to something else
    "import bpy\nme = bpy.data.meshes['m']\nx = me.corner_normals\nbpy.ops.object.shade_smooth_by_angle(angle=0.5)",   # the replacements
    "import bpy\nm = bpy.data.materials['m']\nm.blend_method = 'BLEND'\nm.surface_render_method = 'BLENDED'",                # blend_method still works (alias)
    "import bpy\nx = bpy.context.scene.sequence_editor.sequences_all[0].frame_final_duration",                              # still works
    "import bpy\nbpy.ops.pose.convert_rotation()\nbpy.ops.anim.convert_legacy_action()",                                  # still exist in 5.3
    "import bpy\nbpy.context.scene.use_nodes = True\nmat.use_nodes = True",                                                  # accepted, not an error
    "import bpy\nx = bpy.context.tool_settings.sculpt.unified_paint_settings",                                              # the replacement
    "import bpy\nok = hasattr(me, 'use_auto_smooth')\nname = 'use_auto_smooth'",                                             # a string is not an attribute access
    "import bpy\nmods = bpy.data.objects['o'].modifiers\nm = mods['Name']\nm.show_viewport = False",                        # the collection is subscripted by name, not a modifier
    "import bpy\nmod = bpy.data.objects['o'].modifiers.new('g', 'NODES')\nmod.properties.inputs.Socket_1.value = 2.0",      # the 5.2+ form
    "import bpy\nobj = bpy.data.objects['o']\nobj['my_prop'] = 1\nscene = bpy.context.scene\nscene['other'] = 2",             # ID properties of IDs are fine
    "import bpy\nmat = nodes.new('ShaderNodeBsdfPrincipled')\nmat.inputs['Specular IOR Level'].default_value = 0.5\nmat.inputs['Emission Color']",
    "import bpy\ngroup = nodes.new('NodeGroupInput')\ngroup.inputs['Specular']\ngroup.inputs['Emission']",                   # a user's group may name a socket 'Specular'
    "import bpy\nsep = nodes.new('ShaderNodeSeparateXYZ')\nz = sep.outputs['Z']\ncomb = nodes.new('ShaderNodeCombineXYZ')\ncomb.inputs['Z']",   # 'Z' on XYZ nodes
    "import bpy\nrl = nodes.new('CompositorNodeRLayers')\nx = rl.outputs['Diffuse Color']\ny = rl.outputs['Depth']",       # current pass names
    "import bpy\ngroup = nodes.new('NodeGroupInput')\nx = group.outputs['AO']",                                          # a group output named AO
    "import bpy\ncmp = nodes.new('FunctionNodeCompare')\ncmp.inputs['A'].default_value = 1.0\nr = nodes.new('FunctionNodeRandomValue')\nr.inputs['Min']",
    "import bpy\nsky.sky_type = 'MULTIPLE_SCATTERING'\ns.file_format = 'PNG'",
    "import bpy\ntree.nodes.new('ShaderNodeMix')\ntree.nodes.new('ShaderNodeGamma')\ntree.nodes.new('ShaderNodeTexNoise')",
    "import bpy\nbpy.ops.wm.usd_export(filepath='a.usd', export_textures_mode='NEW', selected_objects_only=False)\nbpy.ops.export_scene.gltf(filepath='a.glb', collection_center_mode='ACTIVE')",
    "import bpy\nbpy.ops.wm.usd_import(filepath='a.usd', export_textures=1)",                                                # keyword of another operator
    "import bpy\ns.media_type = 'MULTI_LAYER_IMAGE'\ns.file_format = 'OPEN_EXR_MULTILAYER'",
    "import bpy\ndef f(s):\n    s.media_type = 'VIDEO'\n    s.file_format = 'FFMPEG'\ns.file_format = 'PNG'\ns.file_format = 'OPEN_EXR'",
    "import gpu\nimport gpu.texture\ngpu.init()\nt = gpu.texture.from_image(img)",
    "import gpu\nfrom gpu_extras.batch import batch_for_shader\nx = 1",                                                       # importing is not a call
    "import bpy\nng = bpy.data.node_groups.new('g', 'GeometryNodeTree')\nng.interface.new_socket(name='x', in_out='INPUT', socket_type='NodeSocketFloat')",
    "import bpy\nnode = nodes.new('NodeCustom')\nnode.inputs.new('NodeSocketFloat', 'x')",                                  # a custom Python node still has inputs.new
    "import bpy\nsc = bpy.context.scene\nsc.compositing_node_group = bpy.data.node_groups.new('c', 'CompositorNodeTree')\nm = mat.node_tree",   # material.node_tree is fine
    "import bpy\nbag = action.layers[0].strips[0].channelbag(slot)\nfc = bag.fcurves\ndrivers = obj.animation_data.drivers\nx = nla_strip.fcurves",
    # a variable called *bsdf* that the same line binds to another node type: the Specular BSDF node still has 'Specular'
    "import bpy\nspec_bsdf = nt.nodes.new('ShaderNodeEeveeSpecular')\nspec_bsdf.inputs['Specular'].default_value = (1, 1, 1, 1)",
    # a removed Blender attribute name used as an attribute of the script's own class
    "class Cfg:\n    def __init__(self):\n        self.base_path = 'x'\n    def get(self):\n        return self.base_path\nresult = Cfg().get()",
    "class Cfg:\n    base_path: str = 'x'\n    use_space = True\nc = Cfg()\nresult = [c.base_path, c.use_space]",
]


@pytest.mark.parametrize("src", NEAR_MISSES)
def test_near_misses_are_not_flagged(src):
    assert lint_script(src) == [], [str(i) for i in lint_script(src)]


def test_the_exemptions_for_other_nodes_and_own_classes_do_not_hide_the_real_thing():
    # a Principled BSDF is still recognised by its type, by the node's name, and by a variable name with no type beside it
    for src in ("n = nt.nodes.new('ShaderNodeBsdfPrincipled')\nn.inputs['Specular'].default_value = 0.5",
                "bsdf = nt.nodes['Principled BSDF']\nbsdf.inputs['Specular'].default_value = 0.5",
                "bsdf = nt.nodes[0]\nbsdf.inputs['Specular'].default_value = 0.5",
                "spec_bsdf = nt.nodes.new('ShaderNodeEeveeSpecular')\nn = nt.nodes.new('ShaderNodeBsdfPrincipled')\nn.inputs['Specular'].default_value = 0.5"):
        assert codes("import bpy\n" + src) == {"AG017"}, src
    # and a File Output node's base_path is still refused when the script declares no attribute of that name
    assert codes("import bpy\nnode = tree.nodes.new('CompositorNodeOutputFile')\nnode.base_path = '/tmp/x'") == {"AG015"}
    assert codes("import bpy\nclass Cfg:\n    root = 'x'\nnode.base_path = Cfg.root") == {"AG015"}


# ---- AG005: the one dynamic getattr that is allowed (Geometry Nodes modifier inputs by socket identifier)

ALLOWED_GETATTR = [
    "import bpy\nmod = bpy.data.objects['o'].modifiers['g']\nfor s in mod.node_group.interface.items_tree:\n    getattr(mod.properties.inputs, s.identifier).value = 1.0",
    "import bpy\nv = getattr(bpy.data.objects['o'].modifiers['g'].properties.inputs, sock.identifier).value",
    "import bpy\nv = getattr(mods['g'].properties.inputs, item.identifier)",
    "import bpy\nmod = bpy.data.objects['o'].modifiers['g']\nmod.properties.inputs.Socket_12.value = 3",           # the constant-attribute form is plain attribute access
]
DYNAMIC_GETATTR = [
    "getattr(mod.properties.inputs, name)",                                    # a name, not <x>.identifier
    "getattr(mod.properties.inputs, 'Socket_' + str(i))",                      # computed string
    "getattr(mod.properties.inputs, f'Socket_{i}')",
    "getattr(mod.properties.inputs, sock.name)",                               # the wrong attribute
    "getattr(mod.properties.inputs, sock.identifier.upper())",                 # not an attribute access ending in .identifier
    "getattr(mod.properties.inputs, sock.identifier, None)",                   # three arguments
    "getattr(mod.properties, sock.identifier)",                                # not .properties.inputs
    "getattr(mod.inputs, sock.identifier)",
    "getattr(mod.properties.outputs, sock.identifier)",
    "getattr(properties.inputs, sock.identifier)",
    "getattr(bpy.context.scene, sock.identifier)",
    "getattr(bpy.ops.wm, sock.identifier)",
    "setattr(mod.properties.inputs, sock.identifier, 1.0)",                    # only getattr is allowed
    "getattr(getattr(mod, 'properties'), sock.identifier)",
]


@pytest.mark.parametrize("src", ALLOWED_GETATTR)
def test_geonodes_input_forms_are_allowed(src):
    assert lint_script(src) == []


@pytest.mark.parametrize("src", DYNAMIC_GETATTR)
def test_other_dynamic_getattr_stays_an_error(src):
    assert "AG005" in codes("import bpy\n" + src), src


def test_a_constant_string_getattr_is_as_before():
    assert lint_script("import bpy\nv = getattr(mod.properties.inputs, 'Socket_1')") == []  # AG005 has only ever refused computed and dunder names
    assert "AG005" in codes("import bpy\ngetattr(mod.properties.inputs, '__class__')")


def test_rule_tables_are_consistent_with_the_hints_and_the_plugin_rules():
    """Each rule's hint names every replacement it checks, and the plugin's rules document names the code and the replacement."""
    from antigravity3d.adapters.blender_5_3 import linter as L
    from antigravity3d.plugin_install import bundle_dir

    doc = (bundle_dir() / "rules" / "blender-5-3-api.md").read_text()
    for rule in L.RULES:
        for spec in rule.new:
            leaf = spec.partition(":")[2] if spec.startswith(("socket:", "node:")) else spec.partition(":")[2].split("=")[-1].split(".")[-1]
            assert leaf in rule.hint, f"{rule.code} {rule.old}: hint does not name {leaf!r}"
            if not spec.startswith("socket:"):  # socket and pass names come in families of 7-15: the document gives the pattern and the code
                assert leaf in doc, f"{rule.code} {rule.old}: the rules document does not name {leaf!r}"
        assert rule.code in doc, f"{rule.code} is missing from the rules document"
    for code in ("AG005", "AG010", "AG011", "AG012", "AG013", "AG014", "AG016", "AG022", "AG023"):
        assert code in doc
