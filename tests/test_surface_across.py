"""What limits wear, relief and grime on a part is its size ACROSS, not its smallest extent (round seven, layer 1).

Round seven's first pass sized the wear band, the relief ceiling, the scratch count and the grime halo on a part's
thickness (its smallest extent). That fixed round six's chair, whose 12-14 mm spindles were worn and grimed all
round, but on the crate it took the 12 mm wear band down to 3-10 mm and squeezed the 1 mm edge roll on every 8-14 mm
steel plate (corner caps, hinge, latch and handle plates) and removed the grime beside its rails. The blind judge
preferred round six's crate 4 of 6 ("crisper edge wear on the top corners", "grime details in the crevices").
Measured (S/r7/scratch_surface2/attrib_crate.txt): on the crate's plates the narrower band carried 76 % of the albedo
change and the grime rule 14 %.

A rod is thin in two directions and a plate in one: a plate's thinness says nothing about how far wear reaches across
its faces or how deep a dent in it may look. ``surface_frame.part_across_m`` is the one definition: a rod's diameter,
a plate's width. Each test below fails on the integration head before this change (99d9d88) and passes after it,
except the spindle-ring guard, which holds on both.
"""

from __future__ import annotations

import numpy as np
import pytest

from antigravity3d.ir import MaterialIR, NodeIR, SceneIR, Transform
from antigravity3d.kernel import mesh_masks as mm
from antigravity3d.kernel import smart_materials as sm
from antigravity3d.kernel import surface_frame as sf
from antigravity3d.kernel.coplanar import opaque_materials
from antigravity3d.kernel.fast_ops import run_export_pipeline
from antigravity3d.kernel.usd_stage import StageManager
from antigravity3d.state import EngineBudget

STEEL = MaterialIR(id="steel", surface="worn dark painted steel, chipped, dusty", base_color=(0.1, 0.1, 0.1), roughness=0.5)


def _scene(tmp_path, nodes, material=STEEL):
    """A default-path design (scene operations, no script): both install modes paint through the same code."""
    scene = SceneIR(asset_name="T", nodes=nodes, materials=[material])
    stage = StageManager.create(tmp_path / "T.usda", "T")
    stage.apply_ir(scene)
    return stage.extract_meshes(), scene


def _scripted(tmp_path, monkeypatch, build, surface):
    from agy3d import Asset

    from antigravity3d.build import handoff

    monkeypatch.setenv("AGY3D_BUILD_OUT", str(tmp_path / "out"))
    a = Asset("T", "godot_4")
    m = a.material("mat", "#6e4224", surface=surface)
    build(a, m)
    scene = handoff.read(a.save()).scene()
    stage = StageManager.create(tmp_path / "T.usda", "T")
    stage.apply_ir(scene)
    return stage.extract_meshes(), scene


def _paint(meshes, scene, px=1024):
    bundle = run_export_pipeline(meshes, EngineBudget("godot_4", shared_atlas=False), "T", opaque_materials=opaque_materials(scene))
    lay = mm.layouts_from_uvs(bundle.uvs, px)[0]
    m = scene.materials[0]
    masks = mm.compute_masks(lay, meshes)
    layer, c = sm.paint_material(sm.classify(m), m, masks, {x.node_id: x for x in meshes})
    return layer, c, masks


def _by_id(meshes, nid):
    return next(m for m in meshes if m.node_id == nid)


def _part(c, masks, nid):
    ids = [p.node_id for p in masks.texels.layout.parts]
    return c.part == ids.index(nid)


# ----------------------------------------------------------------------------------------- a plate keeps its edges


def test_a_plate_4_mm_thick_and_8_cm_wide_keeps_the_12_mm_band_and_its_edge_roll(tmp_path, monkeypatch):
    """The crate's corner caps (8 x 68 x 68 mm) and hinge plates: wear and the bevel's highlight as on a panel."""
    plate = NodeIR(id="plate", kind="box", params={"size": [0.2, 0.08, 0.004]}, material_id="steel")
    meshes, scene = _scene(tmp_path, [plate])
    assert sf.part_across_m(_by_id(meshes, "plate")) == pytest.approx(0.08, rel=0.02)
    assert mm.edge_band_m(_by_id(meshes, "plate")) == pytest.approx(mm.EDGE_RADIUS_M)
    layer, c, masks = _paint(meshes, scene, px=1024)
    # the band: the same convex field as with no size limit at all (round six's 12 mm everywhere)
    cvx, _ = mm.edge_fields(masks.texels, {m.node_id: m for m in meshes})
    monkeypatch.setattr(mm, "EDGE_PER_THICKNESS", 1e9)
    full, _ = mm.edge_fields(masks.texels, {m.node_id: m for m in meshes})
    assert np.array_equal(cvx, full)
    # the roll: the relief the ceiling lets through is all of it (the ceiling does not bind on a plate this wide)
    monkeypatch.setattr(sm, "RELIEF_PER_THICKNESS", 1e9)
    free, _ = _paint(meshes, scene, px=1024)[:2]
    assert np.array_equal(layer.height, free.height)
    assert -layer.height.min() > 0.9 * sm.EDGE_ROLL_M


# ----------------------------------------------------------------------------------------- rods and bolts do not


def test_a_14_mm_rod_and_a_5_mm_bolt_still_get_a_narrow_band_and_capped_relief(tmp_path, monkeypatch):
    """Round six's chair defect stays fixed: on a rod or a bolt the size across is its diameter, the same number as its
    thickness, so wear and relief stay sized to it."""
    meshes, scene = _scripted(tmp_path, monkeypatch, lambda a, m: a.tube(
        "rod", path=[(0, 0, 0.0), (0, 0.02, 0.2), (0, 0, 0.4)], radius=0.007, sides=10, material=m),
        surface="worn painted steel, chipped")
    rod = _by_id(meshes, "rod")
    assert sf.part_across_m(rod) == pytest.approx(0.014, rel=0.02)
    assert mm.edge_band_m(rod) == pytest.approx(mm.EDGE_PER_THICKNESS * 0.014, rel=0.02)
    layer, c, _ = _paint(meshes, scene, px=2048)
    assert -layer.height.min() <= sm.RELIEF_PER_THICKNESS * 0.014 * 1.02
    bolt = NodeIR(id="bolt", kind="cylinder", params={"radius": 0.0025, "height": 0.004, "segments": 6}, material_id="steel")
    meshes, scene = _scene(tmp_path / "b", [bolt])
    b = _by_id(meshes, "bolt")
    assert sf.part_across_m(b) == pytest.approx(0.005, rel=0.15)        # its width (a hexagon: 4.3 to 5 mm)
    assert mm.edge_band_m(b) <= mm.EDGE_PER_THICKNESS * 0.005 + 1e-9
    layer, c, _ = _paint(meshes, scene, px=4096)
    assert -layer.height.min() <= sm.RELIEF_PER_THICKNESS * 0.005 * 1.02


# ----------------------------------------------------------------------------------------- grime beside a rail


def _round_six_dirt(c, amount):
    """Round six's grime (``smart_materials._dirt`` at 0edb0ff), occlusion counted linearly."""
    ground = np.clip(1.0 - c.height_m / 0.15, 0.0, 1.0) ** 2 * (c.up > -0.7)
    d = 1.2 * c.crease + 1.2 * (1.0 - c.ao) + 0.9 * ground
    d *= 0.45 + 1.1 * c.n_large
    return np.clip(d * (0.5 + amount), 0.0, 1.0).astype(np.float32)


def test_grime_on_a_wide_panel_beside_a_raised_rail_is_round_sixs(tmp_path):
    """The crate's panels and plates beside its ribs, rails and latches: the soft grime from occlusion is believable
    on a part far wider than the halo and was lost with the spindle fix."""
    panel = NodeIR(id="panel", kind="box", params={"size": [0.4, 0.4, 0.02]}, material_id="steel",
                   transform=Transform(translate=(0.0, 0.0, 0.2)))
    rail = NodeIR(id="rail", kind="box", params={"size": [0.03, 0.38, 0.03]}, material_id="steel",
                  transform=Transform(translate=(0.0, 0.0, 0.225)))
    meshes, scene = _scene(tmp_path, [panel, rail])
    _, c, masks = _paint(meshes, scene, px=1024)
    on = _part(c, masks, "panel")
    shallow = on & (c.ao < 0.97) & (c.ao > 0.3)
    assert shallow.sum() > 200                                         # the halo beside the rail is there to judge
    amt = sm.classify(STEEL).dirt
    assert np.array_equal(sm._dirt(c, amt)[on], _round_six_dirt(c, amt)[on])


# ----------------------------------------------------------------------------------------- the spindle rings stay gone


def test_no_grime_ring_round_a_spindle_beside_a_rail(tmp_path, monkeypatch):
    """Round six drew dark rings round the chair's 14 mm spindles 2 to 6 cm either side of the arm rail (blind judge:
    "dark shading rings at the joints"): a halo longer than the part is wide reads as a ring. Guard: it does not
    come back."""
    def build(a, m):
        a.tube("spindle", path=[(0, 0, 0.0), (0, 0, 0.6)], radius=0.007, sides=10, material=m)
        a.box("rail", size=(0.3, 0.04, 0.024), at=(0, 0, 0.4), material=m)

    meshes, scene = _scripted(tmp_path, monkeypatch, build, surface="aged oak wood")
    _, c, masks = _paint(meshes, scene, px=2048)
    on = _part(c, masks, "spindle")
    dz = np.abs(c.pos[:, 2] - 0.4)
    near = on & (dz > 0.032) & (dz < 0.072)
    far = on & (((c.pos[:, 2] > 0.2) & (c.pos[:, 2] < 0.28)) | (c.pos[:, 2] > 0.52))
    assert near.sum() > 100 and far.sum() > 100
    amt = sm.classify(scene.materials[0]).dirt
    ring = sm._dirt(c, amt)[near].mean() - sm._dirt(c, amt)[far].mean()
    ring6 = _round_six_dirt(c, amt)[near].mean() - _round_six_dirt(c, amt)[far].mean()
    assert ring6 > 0.05                                                 # round six's rule would draw it here
    assert ring < 0.25 * ring6
