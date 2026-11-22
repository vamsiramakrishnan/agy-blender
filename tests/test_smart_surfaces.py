"""Surfaces made from the geometry without an image model: kernel/mesh_masks.py and kernel/smart_materials.py.

Real kernel (compile_ir, the unwrap, the rasteriser); small atlases (256 px) so the file runs in seconds.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from antigravity3d.ir import (
    IRPatch,
    MaterialIR,
    NodeIR,
    SceneIR,
    Transform,
    UpsertMaterial,
)
from antigravity3d.kernel import mesh_masks as mm
from antigravity3d.kernel import smart_materials as sm
from antigravity3d.kernel.coplanar import opaque_materials
from antigravity3d.kernel.fast_ops import run_export_pipeline
from antigravity3d.kernel.package import write_engine_package
from antigravity3d.kernel.usd_stage import StageManager
from antigravity3d.state import EngineBudget

BRIEF = "a rugged military supply crate with two side handles"


def _scene() -> SceneIR:
    """A crate body, a lid panel on top, a handle standing off one end and a steel foot: contacts, a gap, edges."""
    return SceneIR(asset_name="Crate", nodes=[
        NodeIR(id="body", kind="box", params={"size": [0.8, 0.5, 0.4]}, transform=Transform(translate=(0, 0, 0.22)), material_id="paint"),
        NodeIR(id="lid", kind="box", params={"size": [0.7, 0.4, 0.02]}, transform=Transform(translate=(0, 0, 0.43)), material_id="paint"),
        NodeIR(id="handle", kind="box", params={"size": [0.02, 0.2, 0.02]}, transform=Transform(translate=(0.43, 0, 0.25)), material_id="steel"),
        NodeIR(id="post_a", kind="box", params={"size": [0.02, 0.02, 0.02]}, transform=Transform(translate=(0.41, 0.09, 0.25)), material_id="steel"),
        NodeIR(id="post_b", kind="box", params={"size": [0.02, 0.02, 0.02]}, transform=Transform(translate=(0.41, -0.09, 0.25)), material_id="steel"),
        NodeIR(id="foot", kind="cylinder", params={"radius": 0.05, "height": 0.02, "segments": 24},
               transform=Transform(translate=(0, 0, 0.01)), material_id="steel"),
    ], materials=[
        MaterialIR(id="paint", surface="olive drab painted steel, chipped", base_color=(0.16, 0.2, 0.11), roughness=0.6, metallic=0.1),
        MaterialIR(id="steel", surface="dark galvanized steel", base_color=(0.2, 0.21, 0.22), roughness=0.45, metallic=0.85),
    ])


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    root = tmp_path_factory.mktemp("smart")
    scene = _scene()
    stage = StageManager.create(root / "Crate_stage.usda", "Crate")
    stage.apply_ir(scene)
    meshes = stage.extract_meshes()
    # one atlas per material (as before the shared atlas): these tests look at one material's maps at a time; the
    # shared atlas through the package is tested below (test_the_shared_atlas_ships_generated_surfaces)
    budget = EngineBudget("godot_4", shared_atlas=False)
    bundle = run_export_pipeline(meshes, budget, "Crate", opaque_materials=opaque_materials(scene))
    return root, scene, stage, meshes, budget, bundle


def _maps(scene, meshes, bundle, px=256, brief=BRIEF):
    return sm.texture_scene(scene.materials, mm.layouts_from_uvs(bundle.uvs, px), meshes, brief=brief, asset_name=scene.asset_name)


# --------------------------------------------------------------------------------------------- classes


@pytest.mark.parametrize("surface, metallic, expected", [
    ("matte olive drab painted steel with light edge wear", 0.2, "painted_metal"),
    ("powder-coated dark gunmetal steel", 0.85, "painted_metal"),
    ("weathered red painted cast iron with pebbled foundry grain", 0.2, "painted_metal"),
    ("satin dark forest green milk-painted hardwood with subtle woodgrain", 0.0, "painted_wood"),
    ("parkerized dark gunmetal steel with fine grain", 0.85, "painted_metal"),
    ("dark gunmetal steel with fine grain", 0.85, "bare_metal"),
    # anodised, parkerised, blackened: a thin dark dielectric coat that wears through to bright metal, like paint
    ("matte black anodized steel", 0.85, "painted_metal"),
    ("blackened brushed steel", 0.85, "painted_metal"),
    ("dark oxidized cast iron with coarse pitted metallic grain", 0.85, "cast_metal"),
    ("tarnished cast bronze with aged patina", 0.88, "brass"),
    ("textured black vulcanized rubber", 0.0, "rubber"),
    ("matte olive drab textured high-impact polymer", 0.0, "polymer"),
    ("matte olive drab fibreglass reinforced polymer with fine textured grain", 0.0, "polymer"),
    ("hand-rubbed dark walnut wood with satin finish and fine grain", 0.0, "wood"),
    ("compressed dark grey wool felt", 0.0, "fabric"),
    ("clear blown glass", 0.0, "glass"),
    ("", 0.9, "plain"),
    ("something nobody wrote a rule for", 0.0, "plain"),
])
def test_class_follows_the_surface_words(surface, metallic, expected):
    c = sm.classify(MaterialIR(id="m", surface=surface, metallic=metallic))
    assert c.surface_class == expected, c.reason
    assert c.surface_class in sm.CLASSES


def test_unknown_words_never_get_a_feature_and_metallic_alone_decides_nothing():
    c = sm.classify(MaterialIR(id="m", surface="", metallic=1.0, base_color=(0.8, 0.6, 0.2)))
    assert c.surface_class == "plain" and c.features == ()


def test_wear_comes_from_the_words_and_the_brief():
    new = sm.classify(MaterialIR(id="m", surface="new olive painted steel"))
    old = sm.classify(MaterialIR(id="m", surface="heavily weathered olive painted steel"))
    light = sm.classify(MaterialIR(id="m", surface="olive painted steel"))
    assert new.wear < 0.2 and old.wear > 0.8 and 0.2 < light.wear < 0.5
    rugged = sm.classify(MaterialIR(id="m", surface="olive painted steel"), "a rugged crate")
    assert rugged.wear == pytest.approx(0.75) and "brief: rugged" in rugged.words
    assert sm.classify(MaterialIR(id="m", surface="olive painted steel"), "a military footlocker").wear == pytest.approx(0.75)
    assert sm.classify(MaterialIR(id="m", surface="weathered olive painted steel"), "a brand-new toolbox").wear <= 0.1
    assert sm.classify(MaterialIR(id="m", surface="heavily weathered olive painted steel"), "a rugged crate").wear == old.wear
    assert sm.classify(MaterialIR(id="g", surface="clear glass"), "a rugged crate").wear < 0.2


def test_features_need_their_words():
    assert "rust" in sm.classify(MaterialIR(id="m", surface="dark oxidized cast iron with light rust")).features
    assert "rust" not in sm.classify(MaterialIR(id="m", surface="dark cast iron")).features
    assert "patina" in sm.classify(MaterialIR(id="m", surface="tarnished brass with patina")).features


# --------------------------------------------------------------------------------------------- masks


def _box(size=(1.0, 1.0, 1.0)) -> tuple[np.ndarray, np.ndarray]:
    from antigravity3d.kernel.compile_ir import primitive_mesh

    return primitive_mesh(NodeIR(id="b", kind="box", params={"size": list(size)}))


def test_box_edges_are_twelve_convex_right_angles():
    p, t = _box()
    e = mm.mesh_edges(p, t)
    convex = e.turn > 0
    assert convex.sum() == 12 and (~convex).sum() == 0
    assert np.allclose(np.degrees(e.turn), 90.0)


def test_an_l_section_has_one_concave_edge():
    from antigravity3d.kernel.compile_ir import primitive_mesh

    p, t = primitive_mesh(NodeIR(id="l", kind="extrude", params={"outline": [[0, 0], [2, 0], [2, 1], [1, 1], [1, 2], [0, 2]], "depth": 1.0}))
    e = mm.mesh_edges(p, t)
    concave = e.turn < 0
    assert concave.sum() == 1
    assert np.degrees(-e.turn[concave][0]) == pytest.approx(90.0)


def test_turn_field_is_full_on_an_edge_and_zero_away_from_it():
    p, t = _box()
    e = mm.mesh_edges(p, t)
    pts = np.array([[0.5, 0.0, 0.5], [0.5, 0.0, 0.497], [0.5, 0.0, 0.0], [0.5, 0.2, 0.2]])  # on an edge, 3 mm off, face centre
    f = np.degrees(mm.turn_field(pts, e, 0.008, +1.0))
    assert f[0] == pytest.approx(90.0, abs=0.5)
    assert 45.0 < f[1] < 60.0              # 90 * (1 - 3/8)
    assert f[2] == 0.0 and f[3] == 0.0
    assert np.all(mm.turn_field(pts, e, 0.008, -1.0) == 0.0)


def test_facet_lines_of_a_thin_rod_do_not_count_as_edges():
    from antigravity3d.kernel.compile_ir import primitive_mesh

    p, t = primitive_mesh(NodeIR(id="r", kind="cylinder", params={"radius": 0.01, "height": 0.4, "segments": 16}))
    e = mm.mesh_edges(p, t)
    side = np.degrees(mm.turn_field(np.array([[0.01, 0.0, 0.0]]), e, 0.012, +1.0))[0]
    assert side < 35.0                                   # a sharp edge reads 90; the edge mask starts at 20 and is
    assert mm.smoothstep(20.0, 80.0, np.array([side]))[0] < 0.15   # under 0.15 here: no chips at any wear


def test_facet_lines_of_a_cylinder_do_not_count_as_edges():
    from antigravity3d.kernel.compile_ir import primitive_mesh

    p, t = primitive_mesh(NodeIR(id="c", kind="cylinder", params={"radius": 0.05, "height": 0.2, "segments": 24}))
    e = mm.mesh_edges(p, t)
    side = np.array([[0.05, 0.0, 0.0]])        # on a facet line, half-way up
    rim = np.array([[0.05, 0.0, 0.1]])         # on the top rim
    assert np.degrees(mm.turn_field(side, e, 0.008, +1.0))[0] < 8.0
    assert np.degrees(mm.turn_field(rim, e, 0.008, +1.0))[0] > 80.0


def test_atlas_texels_lie_on_their_parts(built):
    bundle = built[5]
    layouts = mm.layouts_from_uvs(bundle.uvs, 256)
    for L in layouts:
        tex = mm.rasterize_atlas(L)
        assert tex.covered.any() and (tex.valid >= tex.covered).all()
        ys, xs = np.nonzero(tex.covered)
        for i, part in enumerate(L.parts):
            sel = tex.part[ys, xs] == i
            if not sel.any():
                continue
            q = tex.pos[ys[sel], xs[sel]].astype(np.float64)
            lo, hi = part.points.min(axis=0) - 1e-4, part.points.max(axis=0) + 1e-4
            assert ((q >= lo) & (q <= hi)).all()
        # texel size follows the layout's density: 1024 px/m at 2048 px, so 256 px is about 8 mm
        assert 4e-3 < tex.texel_m < 16e-3


def test_occlusion_sees_other_parts_and_not_the_ground(built):
    _, _, _, meshes, _, _ = built
    occ = mm.Occluder(meshes)
    n_side = np.array([[1.0, 0.0, 0.0]])
    behind_handle = occ(np.array([[0.4, 0.0, 0.25]]), n_side)       # the end wall right behind the handle bar
    open_wall = occ(np.array([[0.4, 0.2, 0.08]]), n_side)           # the same wall, low and to the side
    top = occ(np.array([[0.0, 0.0, 0.44]]), np.array([[0.0, 0.0, 1.0]]))
    assert behind_handle[0] < open_wall[0] - 0.1
    assert top[0] > 0.95
    bottom = occ(np.array([[0.3, 0.2, 0.02]]), np.array([[0.0, 0.0, -1.0]]))
    assert bottom[0] > 0.9                                         # no ground: the engine adds its own contact shade


def test_clearance_finds_the_handle_in_front_of_the_wall(built):
    _, _, _, meshes, _, _ = built
    occ = mm.Occluder(meshes)
    pts = np.array([[0.4, 0.0, 0.25], [0.4, 0.2, 0.1]])
    assert occ.in_front(pts, np.array([1.0, 0.0, 0.0]), 0.15).tolist() == [True, False]


# --- a wall with a thin proud plate, and a wall with a frame: what is in front of a label's texels, however thin

WALL_Y = 0.25          # the body's +y and -y walls


def _plated_scene() -> SceneIR:
    """The crate body with, on its +y wall, a 1 mm plate standing 0.5 mm proud over the label's usual place, and on its
    -y wall a frame of four 10 mm x 3 mm bars around that place (open in the middle)."""
    def box(i, size, at, mat="steel"):
        return NodeIR(id=i, kind="box", params={"size": list(size)}, transform=Transform(translate=at), material_id=mat)

    fz, fw, fh, bar = 0.30, 0.34, 0.12, 0.01
    frame = [box("bar_top", (fw, 0.003, bar), (0, -WALL_Y - 0.0015, fz + fh / 2)),
             box("bar_bottom", (fw, 0.003, bar), (0, -WALL_Y - 0.0015, fz - fh / 2)),
             box("bar_left", (bar, 0.003, fh), (-fw / 2, -WALL_Y - 0.0015, fz)),
             box("bar_right", (bar, 0.003, fh), (fw / 2, -WALL_Y - 0.0015, fz))]
    return SceneIR(asset_name="Crate", nodes=[
        box("body", (0.8, 0.5, 0.4), (0, 0, 0.22), "paint"),
        box("plate", (0.36, 0.001, 0.16), (0, WALL_Y + 0.001, 0.30)),
        *frame,
    ], materials=_scene().materials)


@pytest.fixture(scope="module")
def plated(tmp_path_factory):
    root = tmp_path_factory.mktemp("plated")
    scene = _plated_scene()
    stage = StageManager.create(root / "Crate_stage.usda", "Crate")
    stage.apply_ir(scene)
    meshes = stage.extract_meshes()
    bundle = run_export_pipeline(meshes, EngineBudget("godot_4", shared_atlas=False), "Crate", opaque_materials=opaque_materials(scene))
    return scene, meshes, bundle


def test_a_thin_plate_and_the_bars_of_a_frame_are_in_front_and_a_frames_opening_is_not(plated):
    _, meshes, _ = plated
    occ = mm.Occluder(meshes)
    y = np.array([0.0, 1.0, 0.0])
    under_plate = np.array([[0.0, WALL_Y, 0.30], [0.17, WALL_Y, 0.37], [-0.17, WALL_Y, 0.23]])
    beside_plate = np.array([[0.0, WALL_Y, 0.10], [0.25, WALL_Y, 0.30]])
    assert occ.in_front(under_plate, y, 0.15).all()                   # 1 mm thick, 0.5 mm proud
    assert not occ.in_front(beside_plate, y, 0.15).any()               # the wall's own triangles are not in front
    under_bars = np.array([[0.0, -WALL_Y, 0.36], [-0.17, -WALL_Y, 0.30], [0.1, -WALL_Y, 0.24]])
    in_opening = np.array([[0.0, -WALL_Y, 0.30], [0.12, -WALL_Y, 0.33]])
    assert occ.in_front(under_bars, -y, 0.15).all()
    assert not occ.in_front(in_opening, -y, 0.15).any()


def _face_box(d) -> tuple[float, float, float, float]:
    cx, cz = d["centre_m"][0], d["centre_m"][2]
    w, h = d["size_m"]
    return cx - w / 2, cx + w / 2, cz - h / 2, cz + h / 2


def _overlaps(a, b) -> bool:
    return a[0] < b[1] and b[0] < a[1] and a[2] < b[3] and b[2] < a[3]


def test_no_marking_is_painted_under_a_thin_plate_or_across_a_frame(plated):
    scene, meshes, bundle = plated
    maps = sm.texture_scene(scene.materials, mm.layouts_from_uvs(bundle.uvs, 1024), meshes, brief=BRIEF)
    decl = [d for m in maps.values() for d in m.record["markings"]]
    walls = [d for d in decl if d.get("placed") and d.get("face_normal") and abs(d["face_normal"][1]) > 0.9]
    plate = (-0.18, 0.18, 0.22, 0.38)
    bars = [(-0.17, 0.17, 0.355, 0.365), (-0.17, 0.17, 0.235, 0.245), (-0.175, -0.165, 0.24, 0.36), (0.165, 0.175, 0.24, 0.36)]
    for d in walls:
        box = _face_box(d)
        if d["face_normal"][1] > 0:
            assert not _overlaps(box, plate), d
        else:
            assert not any(_overlaps(box, b) for b in bars), d
    # every label is still accounted for: placed somewhere clear, or declared not placed with the reason
    labels = [d for d in decl if d["kind"] in ("stencil_label", "label")]
    assert labels and all(d.get("placed") or d.get("reason") for d in labels)


# --------------------------------------------------------------------------------------------- maps


def test_maps_pass_the_scorecard_rules_on_every_material(built):
    """compare_eval 'maps': no base colour or normal map flat (all channels' std under 1 %), no base colour black
    (95 % of texels under 30/255). At the density that ships (1024 px/m: 2048 px for this layout, 1 mm texels); at
    8 mm per texel a tenth-of-a-millimetre paint chip is honestly flat in a normal map."""
    _, scene, _, meshes, _, bundle = built
    maps = _maps(scene, meshes, bundle, px=2048)
    assert set(maps) == {"paint", "steel"}
    for mid, m in maps.items():
        alb = np.asarray(m.albedo, np.float64)
        nrm = np.asarray(m.normal, np.float64) / 255.0
        orm = np.asarray(m.orm, np.float64) / 255.0
        assert m.albedo.size == m.orm.size == m.normal.size == (2048, 2048)
        assert (alb.reshape(-1, 3).std(axis=0) / 255.0 >= 0.01).any(), mid
        assert (nrm.reshape(-1, 3).std(axis=0) >= 0.01).any(), mid
        assert (alb.max(axis=2) < 30).mean() < 0.95, mid
        assert nrm[..., 2].mean() > 0.9                       # a detail map: mostly facing out
        assert 0.0 < orm[..., 1].mean() < 1.0
        rec = m.record
        assert rec["class"] in sm.CLASSES and rec["masks"] and rec["timings_ms"]["total"] > 0


def test_painted_edges_chip_to_metal_and_creases_are_rougher(built):
    _, scene, _, meshes, _, bundle = built
    L = next(x for x in mm.layouts_from_uvs(bundle.uvs, 512) if x.material_id == "paint")
    masks = mm.compute_masks(L, meshes)
    m = scene.materials[0]
    layer, c = sm.paint_material(sm.classify(m, BRIEF), m, masks, {x.node_id: x for x in meshes})
    edge, flat = c.edge > 0.9, (c.edge == 0) & (c.crease == 0) & (c.ao > 0.95)
    assert layer.metal[edge].mean() > layer.metal[flat].mean() + 0.1   # chips show steel
    # paint is a dielectric: 0 whatever the author's factor (0.1 here); it used to carry the factor through, which left
    # most texels of a painted prop in between metal and not (measured on live packages: 68-90 %)
    assert layer.metal[flat].mean() == pytest.approx(0.0, abs=0.05)


def test_same_design_same_bytes(built):
    _, scene, _, meshes, _, bundle = built
    a, b = _maps(scene, meshes, bundle), _maps(scene, meshes, bundle)
    for mid in a:
        for role in ("albedo", "orm", "normal"):
            assert getattr(a[mid], role).tobytes() == getattr(b[mid], role).tobytes(), (mid, role)


def test_noise_is_pinned():
    """The lattice hash is integer arithmetic: these values are the same on every machine. If they change, every
    generated surface changes: update them only on purpose."""
    p = np.array([[0.13, 0.27, 0.31], [1.55, -2.21, 0.79]])   # between lattice points: hash and interpolation
    v = sm.value_noise(p, 10.0, 1234)
    assert v.dtype == np.float32
    assert v.tolist() == pytest.approx(PINNED_NOISE, abs=1e-6)


PINNED_NOISE = [0.8510163426399231, 0.310971736907959]


def test_markings_follow_the_brief_and_every_string_is_declared(built):
    _, scene, _, meshes, _, bundle = built
    plain = _maps(scene, meshes, bundle, px=512, brief="a garden storage box")
    assert all(not m.record["markings"] for m in plain.values())
    marked = _maps(scene, meshes, bundle, px=512)
    decl = [d for m in marked.values() for d in m.record["markings"]]
    placed = [d for d in decl if d.get("placed")]
    assert placed, decl
    assert {d["material"] for d in placed} == {"paint"}            # the main painted surface, never the steel
    labels = [d for d in placed if d["kind"] == "stencil_label"]
    assert labels and all(d["legible"] and d["strings"] for d in labels)
    texts = [x["text"] for d in placed for x in d["strings"]]
    assert "SUPPLY" in texts and "THIS SIDE UP" in texts
    assert all(sm.stencil.supported(t) for t in texts)
    # closed vocabulary: nothing names a nation, force or standard
    assert not any(w in t.split() for t in texts for w in ("US", "NATO", "ARMY", "MIL", "UN"))
    # the end wall with the handle gets its arrows only where the handle is not in front of them
    for d in placed:
        if d["kind"] == "this_way_up_pictogram" and d["face_normal"][0] > 0.9:      # the wall with the handle
            y, z = d["centre_m"][1], d["centre_m"][2]
            half_w, half_h = d["size_m"][0] / 2, d["size_m"][1] / 2
            overlaps = abs(y) < 0.11 + half_w and abs(z - 0.25) < 0.02 + half_h
            assert not overlaps, json.dumps(d)
    assert any(d["kind"] == "this_way_up_pictogram" and d["face_normal"][0] > 0.9 for d in placed)


def test_marking_modes(built):
    _, scene, _, meshes, _, bundle = built
    layouts = mm.layouts_from_uvs(bundle.uvs, 512)
    pict = sm.texture_scene(scene.materials, layouts, meshes, brief=BRIEF, markings="pictograms")
    placed = [d for m in pict.values() for d in m.record["markings"] if d.get("placed")]
    assert placed and not any(d["strings"] for d in placed) and not any(d["legible"] for d in placed)
    off = sm.texture_scene(scene.materials, layouts, meshes, brief=BRIEF, markings="off")
    assert not any(m.record["markings"] for m in off.values())
    with pytest.raises(ValueError):
        sm.plan_markings(BRIEF, "Crate", {}, mode="loud")


def test_lot_and_serial_follow_the_design_not_the_run():
    a = sm.plan_markings(BRIEF, "Crate", {}, design_key="abc", size_m=(0.8, 0.5, 0.45))
    b = sm.plan_markings(BRIEF, "Crate", {}, design_key="abc", size_m=(0.8, 0.5, 0.45))
    c = sm.plan_markings(BRIEF, "Crate", {}, design_key="abd", size_m=(0.8, 0.5, 0.45))
    assert a["label"] == b["label"] and a["label"] != c["label"]
    assert a["label"][0] == "SUPPLY" and a["label"][2] == "GROSS WT 27 KG"      # 0.18 m3 at 150 kg/m3
    assert sm.plan_markings("a crate of artillery ammunition", "Box", {})["heading"] == "AMMUNITION"
    assert sm.plan_markings("a crate of artillery ammunition", "Box", {})["hazard"]


def test_stencil_text_is_drawn_and_measured_consistently():
    from antigravity3d.kernel import stencil

    assert all(ch in stencil.GLYPHS for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-/.:")
    cap, texel = 0.03, 0.001
    w = stencil.text_width("LOT K-472", cap)
    ys, xs = np.mgrid[0:60, 0:220]
    s, t = xs.ravel() * texel, ys.ravel() * texel
    cov = stencil.draw_text(s, t, "LOT K-472", 0.005, 0.01, cap, texel)
    ink = s[cov > 0.5]
    assert ink.min() >= 0.005 - texel and ink.max() <= 0.005 + w + texel
    assert 0.1 < (cov > 0.5).mean() < 0.4
    assert np.array_equal(cov, stencil.draw_text(s, t, "LOT K-472", 0.005, 0.01, cap, texel))
    # stencil bridges: the bowl of an O is open where its stroke closes
    o = stencil.draw_text(s, t, "O", 0.005, 0.01, cap, texel).reshape(60, 220)
    assert o.max() == 1.0


def test_normal_map_follows_the_height_slope():
    h = np.tile(np.arange(8, dtype=np.float32) * 1e-4, (8, 1))         # rising along +u (columns)
    valid = np.ones((8, 8), bool)
    n = sm.normal_map(h, valid, np.zeros((8, 8), np.int32), 1e-3)
    assert (n[..., 0] < -0.05).all() and np.allclose(n[..., 1], 0.0)
    v = np.tile((np.arange(8, dtype=np.float32) * 1e-4)[:, None], (1, 8))  # rising down the image = falling in +v
    n = sm.normal_map(v, valid, np.zeros((8, 8), np.int32), 1e-3)
    assert (n[..., 1] > 0.05).all()


def test_normal_map_does_not_difference_across_parts():
    h = np.zeros((4, 8), np.float32)
    h[:, 4:] = 1.0                                                   # a cliff exactly at a part border
    part = np.zeros((4, 8), np.int32)
    part[:, 4:] = 1
    n = sm.normal_map(h, np.ones((4, 8), bool), part, 1e-3)
    assert np.allclose(n[..., 2], 1.0)


def test_a_transparent_material_is_left_alone(built):
    _, scene, _, meshes, _, bundle = built
    mats = [m if m.id != "steel" else m.model_copy(update={"alpha_mode": "BLEND", "opacity": 0.5}) for m in scene.materials]
    maps = sm.texture_scene(mats, mm.layouts_from_uvs(bundle.uvs, 256), meshes, brief=BRIEF)
    assert "steel" not in maps and "paint" in maps


def test_the_package_ships_the_generated_maps(built, tmp_path):
    _, scene, stage, meshes, budget, bundle = built
    maps = _maps(scene, meshes, bundle)
    ops = []
    for mid, m in maps.items():
        paths = {}
        for role in ("albedo", "orm", "normal"):
            p = tmp_path / f"{mid}_{role}.png"
            getattr(m, role).save(p)
            paths[role] = str(p)
        mat = next(x for x in scene.materials if x.id == mid)
        ops.append(UpsertMaterial(material=mat.model_copy(update={f"{r}_uri": v for r, v in paths.items()})))
    stage.apply_patch(IRPatch(ops=ops))
    manifest = write_engine_package(stage, bundle, budget, tmp_path / "package", asset_name="Crate")
    assert manifest.ok, [i for i in manifest.issues if i.severity == "critical"]
    shipped = sorted(p.name for p in (tmp_path / "package" / "textures").iterdir())
    assert {f"{mid}_{r}.png" for mid in maps for r in ("albedo", "orm", "normal")} <= set(shipped), shipped


def test_layout_density_follows_what_the_class_paints():
    assert sm.detail_px_per_m(MaterialIR(id="a", surface="olive painted steel"), 1024) == 1024
    assert sm.detail_px_per_m(MaterialIR(id="b", surface=""), 1024) == 256
    assert sm.detail_px_per_m(MaterialIR(id="c", surface="clear glass", alpha_mode="BLEND", opacity=0.4), 1024) == 256


# --------------------------------------------------------------------------------------------- the policy question


async def test_the_qa_request_states_the_placed_markings_and_asks_about_other_text():
    from antigravity3d.decisions.engine import DecisionControlPlane
    from antigravity3d.decisions.fakes import FakeDecisionsClient

    c = FakeDecisionsClient()
    plane = DecisionControlPlane(c)
    await plane.evaluate_visual_qa("https://x/sheet", stage="lookdev")
    await plane.evaluate_visual_qa("https://x/sheet", stage="lookdev", placed_markings=["SUPPLY", "LOT A-458", "SUPPLY"])
    await plane.evaluate_visual_qa("https://x/sheet", stage="lookdev", placed_markings=[])
    before, after, none = c.calls
    assert none == before                                            # no markings: the request is what it was
    texts = [b["text"] for b in after["input"] if b["type"] == "text"]
    assert texts[-1] == 'This pipeline painted these stencil markings on the asset on purpose; they are part of its design: "SUPPLY", "LOT A-458".'
    q0, q1 = before["questions"]["defect_ip_or_text"], after["questions"]["defect_ip_or_text"]
    assert q0["type"] == q1["type"] == "noul" and "Apart from the stencil markings listed in the input" in q1["instructions"]
    assert {k: v for k, v in after["questions"].items() if k != "defect_ip_or_text"} == \
        {k: v for k, v in before["questions"].items() if k != "defect_ip_or_text"}


def test_placed_strings_come_from_the_manifest():
    from antigravity3d.kernel.smart_source import placed_strings

    m = {"texture_atlas": {"members": {"paint": {"generated": {"markings": [
        {"placed": True, "strings": [{"text": "SUPPLY", "legible": True}, {"text": "LOT A-458", "legible": True}]},
        {"placed": False, "strings": ["X"]},
        {"placed": True, "strings": []}]}}, "steel": {}}}}
    assert placed_strings(m) == ["SUPPLY", "LOT A-458"]
    assert placed_strings({}) == []


# --------------------------------------------------------------------------------------------- relief and metalness per class

CLASS_SURFACES = {"painted_metal": "olive painted steel", "painted_wood": "green painted wood", "bare_metal": "dark steel",
                  "cast_metal": "cast iron", "brass": "brass", "rubber": "black rubber grip", "polymer": "olive polymer",
                  "wood": "oak wood", "fabric": "grey felt", "glass": "frosted glass", "plain": ""}


@pytest.fixture(scope="module")
def per_class(tmp_path_factory):
    """One bevelled 0.5 m box per class (and a plate on each, so seams and contacts exist), each class its own atlas,
    at 2 mm per texel: the density the layout ships painted surfaces at (smart_source.DETAIL_PX_PER_M, 512 px/m)."""
    mats = [MaterialIR(id=k, surface=v, base_color=(0.3, 0.3, 0.3), metallic=0.6, roughness=0.5) for k, v in CLASS_SURFACES.items()]
    nodes = []
    for i, k in enumerate(CLASS_SURFACES):
        x = i * 0.8
        nodes.append(NodeIR(id=f"box_{k}", kind="box", params={"size": [0.5, 0.5, 0.5], "bevel": 0.004},
                            transform=Transform(translate=(x, 0, 0.25)), material_id=k))
        nodes.append(NodeIR(id=f"plate_{k}", kind="box", params={"size": [0.2, 0.01, 0.12]},
                            transform=Transform(translate=(x, -0.255, 0.3)), material_id=k))
    scene = SceneIR(asset_name="Probe", nodes=nodes, materials=mats)
    root = tmp_path_factory.mktemp("per_class")
    stage = StageManager.create(root / "Probe_stage.usda", "Probe")
    stage.apply_ir(scene)
    meshes = stage.extract_meshes()
    bundle = run_export_pipeline(meshes, EngineBudget("godot_4", shared_atlas=False), "Probe")
    out = {}
    for L in mm.layouts_from_uvs(bundle.uvs):
        tex = mm.rasterize_atlas(L)
        side = round(L.width * tex.texel_m / 0.002 / 64) * 64          # 2 mm texels
        L2 = mm.AtlasLayout(L.material_id, side, side, L.parts)
        out[L.material_id] = (sm.texture_scene(scene.materials, [L2], meshes, brief="")[L.material_id], mm.rasterize_atlas(L2))
    return out


def _slopes(maps, tex) -> np.ndarray:
    n = np.asarray(maps.normal, np.float64) / 127.5 - 1.0
    return np.hypot(n[..., 0], n[..., 1])[tex.covered]


@pytest.mark.parametrize("cls", ["painted_metal", "painted_wood", "bare_metal", "brass", "polymer", "wood", "glass", "plain"])
def test_smooth_materials_are_smooth_between_their_features(per_class, cls):
    """Varnished wood, paint, sheet metal, brass and moulded plastic carry relief only where something happened to
    them (chips, dings, seams, the roll of an edge, grain at a very low amplitude): an isotropic noise relief on them
    read as "noisy surface artifacts" and "hammered leather" on a dark oak seat (blind judge, 6 of 6). On a 0.5 m box
    those features cover the edges only: under a fifth of the texels tilt more than one degree."""
    s = _slopes(*per_class[cls])
    assert (s > 0.0175).mean() < 0.2, (cls, (s > 0.0175).mean())


@pytest.mark.parametrize("cls", ["cast_metal", "fabric", "rubber"])
def test_textured_materials_carry_their_texture(per_class, cls):
    """Cast iron's pebbled skin, cloth's weave, a rubber grip's knurl: most of their surface tilts."""
    s = _slopes(*per_class[cls])
    assert (s > 0.0175).mean() > 0.5, (cls, (s > 0.0175).mean())


@pytest.mark.parametrize("cls", list(CLASS_SURFACES))
def test_metallic_is_metal_or_not(per_class, cls):
    """In metallic-roughness PBR a texel is metal or not; in between is for the blend at a boundary. The author's 0.6 is
    not carried through: paint, plastic, rubber, wood, felt and glass are 0, bare steel, cast iron and brass are 1."""
    maps, tex = per_class[cls]
    metal = np.asarray(maps.orm, np.float64)[..., 2][tex.covered] / 255.0
    decided = (metal < 0.05) | (metal > 0.95)
    assert decided.mean() >= 0.9, (cls, decided.mean())
    base = sm.class_metal(cls, 0.6)
    assert abs(np.median(metal) - base) < 0.05, (cls, np.median(metal))
    assert maps.record["metallic"]["overridden"] == (abs(base - 0.6) > 0.05)


def test_chips_on_painted_steel_show_metal():
    assert sm.CLASS_METAL["painted_metal"] == 0.0 and sm.binary_metal(np.array([0.3, 0.7], np.float32)).tolist() == [0.0, 1.0]


def test_a_second_package_of_the_same_design_reuses_the_paint_and_ships_the_same_maps(tmp_path, monkeypatch):
    """deliver_package writes the package for its checks before the review and again for the export: on run 7's 63-part
    crate the paint was 22 of the writer's 27 s. The second write reuses it; a changed design is painted again."""
    from antigravity3d.kernel import smart_source

    painted = []
    real = sm.texture_scene
    monkeypatch.setattr(sm, "texture_scene", lambda *a, **k: painted.append(1) or real(*a, **k))
    monkeypatch.setattr(smart_source, "_LAST_PAINTED", None)
    scene = _scene()
    stage = StageManager.create(tmp_path / "Crate_stage.usda", "Crate")
    stage.apply_ir(scene)
    budget = EngineBudget("godot_4", generated_surfaces=True)

    def package(name):
        meshes = stage.extract_meshes()
        bundle = run_export_pipeline(meshes, budget, "Crate", opaque_materials=opaque_materials(stage.scene_ir()))
        m = write_engine_package(stage, bundle, budget, tmp_path / name, asset_name="Crate")
        assert m.ok
        return {p.name: p.read_bytes() for p in sorted((tmp_path / name / "textures").iterdir())}

    first, second = package("a"), package("b")
    assert len(painted) == 1 and first == second
    stage.apply_patch(IRPatch(ops=[UpsertMaterial(material=scene.materials[0].model_copy(update={"base_color": (0.5, 0.2, 0.1)}))]))
    # Painted again, and its own maps: in a long test process (native5b) this package came out identical to the first,
    # the source having matched the freed raster's reused id() (smart_source.SmartSource keeps a weak reference now).
    assert package("c") != first and len(painted) == 2


def test_two_packages_written_at_once_in_two_threads_ship_their_own_maps(tmp_path, monkeypatch):
    """One SOURCE serves every package of the process (two sessions of one MCP server write theirs in worker threads):
    each package ships the maps and records its own design gets when written alone."""
    from concurrent.futures import ThreadPoolExecutor

    from antigravity3d.kernel import smart_source

    budget = EngineBudget("godot_4", generated_surfaces=True)
    designs = {"olive": _scene(), "rust": _scene()}
    designs["rust"].materials[0] = designs["rust"].materials[0].model_copy(update={"base_color": (0.5, 0.2, 0.1)})

    def package(name, out):
        stage = StageManager.create(out / f"{name}_stage.usda", "Crate")
        stage.apply_ir(designs[name])
        meshes = stage.extract_meshes()
        bundle = run_export_pipeline(meshes, budget, "Crate", opaque_materials=opaque_materials(stage.scene_ir()))
        m = write_engine_package(stage, bundle, budget, out / name, asset_name="Crate")
        assert m.ok
        files = {p.name: p.read_bytes() for p in sorted((out / name / "textures").iterdir())}
        return files, json.dumps(m.data["texture_atlas"]["members"], sort_keys=True)

    alone = {}
    for name in designs:
        monkeypatch.setattr(smart_source, "_LAST_PAINTED", None)
        (tmp_path / "alone").mkdir(exist_ok=True)
        alone[name] = package(name, tmp_path / "alone")
    assert alone["olive"][0] != alone["rust"][0]
    monkeypatch.setattr(smart_source, "_LAST_PAINTED", None)
    (tmp_path / "together").mkdir()
    import threading

    meet, real = threading.Barrier(2), sm.texture_scene

    def overlapping(*a, **k):  # both writers are inside the painting at once
        try:
            meet.wait(timeout=10)
        except threading.BrokenBarrierError:
            pass
        return real(*a, **k)

    monkeypatch.setattr(sm, "texture_scene", overlapping)
    with ThreadPoolExecutor(2) as pool:
        together = dict(zip(designs, pool.map(lambda n: package(n, tmp_path / "together"), designs)))
    assert together == alone
