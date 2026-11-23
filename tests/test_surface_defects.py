"""The four surface defects round six's chair lost on, each asserted through what any version of the pipeline makes: a
script's design (``agy3d.Asset``) through the hand-off, the stage, the unwrap and the painter (``paint_material``,
``texture_material``), and the package's shipped normals. Only interfaces that existed before round seven are used,
so this file runs against the round-six tree and fails there (checked 2026-11-23: every test below fails on
/home/user/agy-blender at 0edb0ff and passes with round seven's surface work).
"""

from __future__ import annotations

import numpy as np
import pytest

from antigravity3d.ir import MaterialIR, NodeIR, SceneIR, Transform
from antigravity3d.kernel import mesh_masks as mm
from antigravity3d.kernel import smart_materials as sm
from antigravity3d.kernel.coplanar import opaque_materials
from antigravity3d.kernel.fast_ops import run_export_pipeline
from antigravity3d.kernel.usd_stage import StageManager
from antigravity3d.state import EngineBudget

OAK = MaterialIR(id="oak", surface="oak wood", base_color=(0.16, 0.08, 0.035), roughness=0.45)
LUM = np.array([0.2126, 0.7152, 0.0722])


def _scripted(tmp_path, monkeypatch, build) -> tuple[list, object]:
    """A design written with the script API, read back through the hand-off and authored on a stage."""
    from agy3d import Asset

    from antigravity3d.build import handoff

    monkeypatch.setenv("AGY3D_BUILD_OUT", str(tmp_path / "out"))
    a = Asset("T", "godot_4")
    m = a.material("oak", "#6e4224", surface="oak wood")
    build(a, m)
    scene = handoff.read(a.save()).scene()
    stage = StageManager.create(tmp_path / "T.usda", "T")
    stage.apply_ir(scene)
    return stage.extract_meshes(), (stage, scene)


def _paint(meshes, scene, px=1024):
    bundle = run_export_pipeline(meshes, EngineBudget("godot_4", shared_atlas=False), "T", opaque_materials=opaque_materials(scene))
    lay = mm.layouts_from_uvs(bundle.uvs, px)[0]
    m = scene.materials[0]
    masks = mm.compute_masks(lay, meshes)
    layer, c = sm.paint_material(sm.classify(m), m, masks, {x.node_id: x for x in meshes})
    return layer, c, masks, bundle


# ----------------------------------------------------------------------------------------- a bent rod's grain


def test_a_bent_rods_grain_runs_along_its_path(tmp_path, monkeypatch):
    """Round six drew rings about the bow's chord (its principal axis): where the bow turns away from the chord the
    rings cut across it. Measured here on the painted colour: along a line that follows the rod's path at a fixed side,
    the wood changes slowly; around the rod it changes faster."""
    R, r = 0.25, 0.012
    a = np.linspace(0.0, np.pi, 33)
    path = [(R * np.cos(t), 0.0, 0.3 + R * np.sin(t)) for t in a]
    meshes, (_, scene) = _scripted(tmp_path, monkeypatch, lambda A, m: A.tube("bow", path=path, radius=r, sides=10,
                                                                             smooth=False, material=m))
    layer, c, _, _ = _paint(meshes, scene)
    lum = layer.color @ LUM
    p = c.pos
    t = np.arctan2(p[:, 2] - 0.3, p[:, 0])                        # where along the bow (its own path, test-side)
    rad = np.stack([np.cos(t), np.zeros(len(t)), np.sin(t)], 1)
    off = p - np.stack([R * np.cos(t), np.zeros(len(t)), 0.3 + R * np.sin(t)], 1)
    keep = (t > 0.3) & (t < np.pi - 0.3) & (np.linalg.norm(off, axis=1) < 1.3 * r)
    # the rod's surface unrolled: metres along the path, metres around the rod; the colour's mean change per mm each way
    from scipy.interpolate import griddata

    u = t * R
    v = np.arctan2(off[:, 1], (off * rad).sum(1)) * r
    gu, gv = np.mgrid[0.3 * R:(np.pi - 0.3) * R:0.001, -np.pi * r * 0.9:np.pi * r * 0.9:0.001]
    img = griddata(np.stack([u[keep], v[keep]], 1), lum[keep], (gu, gv), method="linear")
    along = np.nanmean(np.abs(np.diff(img, axis=0)))
    across = np.nanmean(np.abs(np.diff(img, axis=1)))
    assert along < 0.5 * across, (along, across)


# ----------------------------------------------------------------------------------------- a board's face


def _dark_lines(pos, lum, axis, other, at):
    """The spacings of the dark lines met along ``axis`` in one strip of the face, 6 mm wide, at ``at`` on ``other``."""
    s = np.abs(pos[:, other] - at) < 0.003
    x, y = pos[s, axis], lum[s]
    o = np.argsort(x)
    x, y = x[o], y[o]
    grid = np.arange(x.min(), x.max(), 0.0005)
    prof = np.convolve(np.interp(grid, x, y), np.ones(5) / 5, "same")
    mid = np.convolve(prof, np.ones(81) / 81, "same")
    dark = prof < mid * 0.94   # a line 6 % darker than the wood around it
    return np.diff(grid[1:][dark[1:] & ~dark[:-1]])


STRIPS = 21


def _face_lines(pos, lum, axis, other):
    """The spacings of the dark lines over the whole face: ``STRIPS`` strips spread across it, pooled.

    One strip is not the face. It holds about ten lines, some of them at the detector's threshold, and the spread of
    their spacing differs from strip to strip: 0.25 to 0.82 over this board, 0.35 in the middle one. The first version
    of this test read the middle strip alone against a bar of 0.35, passed here at 0.352, and failed on another machine
    (CI, Python 3.13) at 0.303, where eight of the same nine lines were detected. Pooled, one line more or less is one
    in about two hundred."""
    ats = np.linspace(pos[:, other].min() + 0.02, pos[:, other].max() - 0.02, STRIPS)
    return np.concatenate([_dark_lines(pos, lum, axis, other, float(a)) for a in ats])


def _board(tmp_path, px=1024):
    seat = NodeIR(id="seat", kind="box", params={"size": [0.45, 0.40, 0.04]}, material_id="oak",
                  transform=Transform(translate=(0, 0, 0.42)))
    scene = SceneIR(asset_name="T", nodes=[seat], materials=[OAK])
    stage = StageManager.create(tmp_path / "T.usda", "T")
    stage.apply_ir(scene)
    return stage.extract_meshes(), scene


def test_a_boards_face_shows_uneven_lines_along_it_not_a_ruler(tmp_path):
    """Round six: a seat showed dark lines every 12.6 mm (spread 0.21), all parallel to one axis, the judge's
    "repetitive horizontal banding"."""
    meshes, scene = _board(tmp_path)
    layer, c, _, _ = _paint(meshes, scene)
    top = c.nrm[:, 2] > 0.97
    lum = layer.color @ LUM
    across = _face_lines(c.pos[top], lum[top], 1, 0)   # across the grain (the board runs along x, its longest side)
    along = _face_lines(c.pos[top], lum[top], 0, 1)
    assert len(across) >= 4 * STRIPS
    # uneven years, not a ruler. Measured over the whole face: round six's code 0.19 (a line every 12.4 mm), this code 0.63
    assert across.std() / across.mean() > 0.35
    assert len(along) < len(across)                      # the lines run along the board


def test_wood_is_not_aliased_into_a_lattice(tmp_path):
    """Round seven's first pass drew latewood lines 0.8 mm wide, narrower than a 2 mm texel: sampled at texel centres
    they broke into a regular grid of dots on the seat and the bow. A texture band-limited to its texels looks the
    same painted at one density as painted at twice the density and filtered down (measured on that first pass: 95th
    percentile 6.5 levels; now 2.0)."""
    from scipy import ndimage

    meshes, scene = _board(tmp_path)
    bundle = run_export_pipeline(meshes, EngineBudget("godot_4", shared_atlas=False), "T", opaque_materials=opaque_materials(scene))
    fine = mm.layouts_from_uvs(bundle.uvs, 1024)[0]
    coarse = mm.AtlasLayout(fine.material_id, 512, 512, fine.parts)
    occ = mm.Occluder(meshes)
    a1 = np.asarray(sm.texture_material(OAK, coarse, meshes, occluder=occ).albedo.convert("L"), np.float64)
    a2 = np.asarray(sm.texture_material(OAK, fine, meshes, occluder=occ).albedo.convert("L"), np.float64)
    box = a2.reshape(512, 2, 512, 2).mean(axis=(1, 3))
    tex = mm.rasterize_atlas(coarse)
    top = ndimage.binary_erosion(tex.covered & (tex.normal[..., 2] > 0.97), iterations=2)
    assert np.percentile(np.abs(a1 - box)[top], 95) < 4.5


# ----------------------------------------------------------------------------------------- a 6-sided rod


def test_a_six_sided_rod_is_not_painted_as_edge_and_ships_smooth(tmp_path, monkeypatch):
    """Round six: a 6-sided 7 mm spindle read 102-110 degrees of convex turn on every texel (all edge, a 0.9 mm edge
    roll all round) and shipped as a hexagonal prism (40-degree crease, 60 degrees per facet)."""
    from antigravity3d.kernel.package import write_engine_package

    meshes, (stage, scene) = _scripted(tmp_path, monkeypatch, lambda A, m: A.tube(
        "spindle", path=[(0, 0, 0.0), (0, 0.02, 0.2), (0, 0, 0.4)], radius=0.007, sides=6, material=m))
    layer, c, masks, bundle = _paint(meshes, scene)
    middle = (c.pos[:, 2] > 0.05) & (c.pos[:, 2] < 0.35)
    assert np.median(c.convex_deg[middle]) < 10.0
    assert np.mean(c.edge[middle] > 0.5) < 0.05
    out = tmp_path / "pkg"
    write_engine_package(stage, bundle, EngineBudget("godot_4"), out, asset_name="T")
    import pygltflib

    g = pygltflib.GLTF2().load(str(out / "T.glb"))
    prim = g.meshes[0].primitives[0]
    acc = g.accessors[prim.attributes.NORMAL]
    bv = g.bufferViews[acc.bufferView]
    start = bv.byteOffset + (acc.byteOffset or 0)
    n = np.frombuffer(g.binary_blob()[start:start + acc.count * 12], np.float32).reshape(-1, 3)
    sides = n[np.abs(n[:, 1]) < 0.5]                    # glTF is Y-up: the rod's side normals, caps left out
    assert len(np.unique(np.round(sides, 3), axis=0)) > 12   # a prism ships 6 per straight leg; smooth: one per vertex


# ----------------------------------------------------------------------------------------- relief on a thin part


def test_relief_on_a_thin_part_is_in_proportion_to_it(tmp_path):
    """Round six put a 1 mm edge roll, 1.2 mm dings, a 0.25 mm paint step and 0.15 mm scratches on every part
    whatever its size: on a 4 mm bar (and the crate's 2.5 mm bolts, the chair's 7 mm spindles) that is relief as deep
    as the part is thin."""
    # Changed on purpose (round seven, surface2): this test first used a 4 mm plate 10 cm wide. The relief ceiling is now
    # sized on the part's size ACROSS (surface_frame.part_across_m), and a plate 10 cm wide keeps its 1 mm edge roll as a
    # crate panel does (keyed on thickness, the crate's 8 mm corner caps lost their bevel and the blind judge preferred
    # round six's crate: tests/test_surface_across.py). A part thin in both directions is what the ceiling protects.
    bar = NodeIR(id="bar", kind="box", params={"size": [0.2, 0.004, 0.004]}, material_id="steel")
    steel = MaterialIR(id="steel", surface="worn dark painted steel, chipped", base_color=(0.1, 0.1, 0.1), roughness=0.5)
    scene = SceneIR(asset_name="T", nodes=[bar], materials=[steel])
    stage = StageManager.create(tmp_path / "T.usda", "T")
    stage.apply_ir(scene)
    layer, _, _, _ = _paint(stage.extract_meshes(), scene, px=2048)
    assert -layer.height.min() < 1.0e-3   # under the old roll alone; every term is now at most 0.12 x 4 mm
