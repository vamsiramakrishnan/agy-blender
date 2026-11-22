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
    ("parkerized dark gunmetal steel with fine grain", 0.85, "bare_metal"),
    ("matte black anodized steel", 0.85, "bare_metal"),
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
    clear = occ.clear_along(pts, np.array([1.0, 0.0, 0.0]), 0.15)
    assert clear.tolist() == [False, True]


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
    assert layer.metal[flat].mean() == pytest.approx(m.metallic, abs=0.05)


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
