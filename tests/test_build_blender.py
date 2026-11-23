"""``a.blender(id, code)``: a Blender snippet's meshes as one part (antigravity3d.build.blender_part)."""

from __future__ import annotations

import time

import numpy as np
import pytest

from agy3d import Asset, BuildError
from antigravity3d.adapters.blender_5_3.pool import find_blender
from antigravity3d.build import blender_part

needs_blender = pytest.mark.skipif(not find_blender(), reason="no Blender binary ($BLENDER_BIN or PATH)")

CUBE = """import bpy
bpy.ops.mesh.primitive_cube_add(size=0.4, location=(0.3, 0.0, 0.2))
cube = bpy.context.active_object
sub = cube.modifiers.new("sub", "SUBSURF")
sub.levels = 2
"""

CURVE = """import bpy
cu = bpy.data.curves.new("rail", "CURVE")
cu.dimensions = "3D"
cu.bevel_depth = 0.02
cu.use_fill_caps = True
sp = cu.splines.new("POLY")
sp.points.add(2)
for i, co in enumerate([(0, 0, 0, 1), (0.5, 0, 0.2, 1), (1, 0, 0, 1)]):
    sp.points[i].co = co
bpy.context.scene.collection.objects.link(bpy.data.objects.new("rail", cu))
"""


KNOB = """import bpy
bpy.ops.mesh.primitive_cylinder_add(radius=0.05, depth=0.04, location=(0, 0, 0.02))
c = bpy.context.active_object
bpy.ops.mesh.primitive_uv_sphere_add(radius=0.03, location=(0, 0, 0.05))
s = bpy.context.active_object
b = c.modifiers.new("u", "BOOLEAN")
b.object = s
b.operation = "DIFFERENCE"
s.hide_set(True)
"""


def edges_closed(tris: np.ndarray) -> bool:
    seen: dict[tuple[int, int], int] = {}
    for a, b, c in tris.tolist():
        for u, v in ((a, b), (b, c), (c, a)):
            seen[(u, v)] = seen.get((u, v), 0) + 1
    return all(n == 1 and seen.get((v, u)) == 1 for (u, v), n in seen.items())


def test_a_snippet_the_linter_refuses_never_reaches_blender(monkeypatch):
    monkeypatch.setattr(blender_part, "_find_blender", lambda: pytest.fail("Blender looked up for a refused snippet"))
    with pytest.raises(BuildError, match=r"^blender s: the snippet is refused: line 1: import of `os` is not allowlisted"):
        Asset("b", engine="godot_4").blender("s", "import os\n")


def test_no_blender_names_what_works_without_it(monkeypatch):
    monkeypatch.setattr(blender_part, "_find_blender", lambda: None)
    with pytest.raises(BuildError) as e:
        Asset("b", engine="godot_4").blender("s", "import bpy\n")
    assert "no Blender here" in str(e.value) and "a.mesh, a.tube and a.loft build without it" in str(e.value)


@pytest.mark.blender
@needs_blender
def test_real_blender_subdivided_cube_curve_error_and_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("AGY3D_BUILD_CACHE", str(tmp_path / "cache"))
    a = Asset("b", engine="godot_4")
    t0 = time.perf_counter()
    cube = a.blender("cube", CUBE, at=(0, 0, 0.1))
    cold = time.perf_counter() - t0
    _, tris, objects, _, cached = blender_part.run("cube", CUBE)
    assert cached and objects == 1 and len(tris) == 6 * 16 * 2, "the second run is served from the cache"
    assert edges_closed(tris), "the subdivided cube comes back closed"
    # subdivision shrinks a cube towards its centre: centred where it was put, inside its 0.4 m box
    assert np.allclose(cube.center, (0.3, 0.0, 0.3), atol=1e-6) and all(0.3 < s < 0.4 for s in cube.size)
    assert a._notes == ["blender cube: 1 object joined, 192 triangles"]
    rod = a.blender("rod", CURVE)
    assert np.allclose(rod.min, (-0.02 * np.sin(np.arctan(0.4)), -0.02, -0.02 * np.cos(np.arctan(0.4))), atol=2e-3)
    assert rod.max[0] > 1.0 and rod.max[2] > 0.2 and a._tris["rod"] > 0, "a bevelled curve comes back as a mesh"
    with pytest.raises(BuildError, match=r'^blender bad: line 2 of the snippet: `bpy.data.objects\["nope"\].location = \(0, 0, 0\)`: KeyError'):
        a.blender("bad", 'import bpy\nbpy.data.objects["nope"].location = (0, 0, 0)\n')
    knob = a.blender("knob", KNOB)  # the boolean idiom: the cutter is hidden and stays out of the part
    assert a._notes[-1].startswith("blender knob: 1 object joined, ") and a._notes[-1].endswith("; 1 hidden object left out")
    _, tris, _, skipped, _ = blender_part.run("knob", KNOB)
    assert skipped == {"hidden": 1} and edges_closed(tris), "the cut knob alone, closed"
    assert np.allclose(knob.max[2], 0.04, atol=1e-6)
    t0 = time.perf_counter()
    Asset("b2", engine="godot_4").blender("cube", CUBE)
    assert time.perf_counter() - t0 < cold / 4, "an unchanged snippet costs nothing on the next run"
