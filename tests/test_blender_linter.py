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
    "import bpy\nimg = bpy.data.images.new('t', 8, 8)\nimport gpu.texture\ntex = gpu.texture.from_image(img)",
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
