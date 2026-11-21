"""Parts of the prop templates that are meant to touch do, exactly, and stand exactly on the ground (the helpers used
to round size and centre separately: 50 micrometres off, up to 450 on the lamp's head)."""

from __future__ import annotations

import random

import numpy as np
import pytest

from antigravity3d.kernel import coplanar
from antigravity3d.kernel.fast_ops import hidden_contact_faces, tier1_qa
from antigravity3d.kernel.usd_stage import StageManager
from antigravity3d.state import EngineBudget
from antigravity3d.tactics import authoring

PROPS = ("crate", "barrel", "table", "lamp_post", "shelf")
SEEDS = range(24)


def build(key: str, seed: int, tmp_path):
    scene = authoring.TEMPLATES[key](random.Random(seed), "T", "x")
    sm = StageManager.create(tmp_path / f"{key}{seed}.usda", "T")
    sm.apply_ir(scene)
    return scene, sm.extract_meshes()


def test_snap_keeps_equal_ends_equal_and_exact():
    # a leg under a table top: the leg's top end and the top's underside are the same number computed two ways
    h, t = 0.7523456789, 0.06
    leg_c, leg_s = authoring._snap((h - t) / 2, h - t)
    top_c, top_s = authoring._snap(h - t / 2, t)
    assert leg_c + leg_s / 2 == pytest.approx(top_c - top_s / 2, abs=1e-12)
    rng = random.Random(1)
    for _ in range(200):  # both ends on the 0.1 mm grid; the centre (half way between two grid values) needs a fifth decimal
        c, size = authoring._snap(rng.uniform(-2, 2), rng.uniform(0.01, 2))
        lo, hi = c - size / 2, c + size / 2
        assert abs(lo * 1e4 - round(lo * 1e4)) < 1e-6 and abs(hi * 1e4 - round(hi * 1e4)) < 1e-6
        assert abs(c * 1e5 - round(c * 1e5)) < 1e-6 and abs(size * 1e4 - round(size * 1e4)) < 1e-6


@pytest.mark.parametrize("key", PROPS)
def test_no_two_parts_miss_each_other_by_a_hair_and_parts_stand_on_the_ground(key, tmp_path):
    for seed in SEEDS:
        scene, meshes = build(key, seed, tmp_path)
        lo = np.array([m.points.min(axis=0) for m in meshes])
        hi = np.array([m.points.max(axis=0) for m in meshes])
        assert abs(lo[:, 2].min()) < 1e-6, (key, seed, lo[:, 2].min())  # the lowest part stands on z = 0, not 50 um under it
        for i in range(len(meshes)):
            for j in range(i + 1, len(meshes)):
                sep = np.maximum(lo[i] - hi[j], lo[j] - hi[i])
                if (sep <= 1e-9).sum() >= 2 and 1e-6 < sep.max() <= 2e-3:  # touching in the other two axes, a sliver apart on one
                    pytest.fail(f"{key} seed {seed}: {meshes[i].node_id} / {meshes[j].node_id} miss by {sep.max() * 1e6:.1f} um")
        rep = tier1_qa(meshes, EngineBudget("unreal_5"))
        assert not [i for i in rep.issues if i.code in ("floating_part", "floating_group", "enclosed_part", "ungrounded")], (key, seed)


@pytest.mark.parametrize("key,expected", [("table", lambda scene: 8),
                                          ("lamp_post", lambda scene: next(n.params["segments"] for n in scene.nodes if n.id == "pole"))])
def test_the_exact_hidden_face_test_finds_the_contacts_of_the_table_and_the_lamp(key, expected, tmp_path):
    """Before the fix: the table's legs stopped 50 um short of its top (0 triangles removed on most seeds) and the lamp's
    pole sank into its base and head (0 on most). Now every leg top (2 triangles x 4) and the pole's bottom face go."""
    for seed in SEEDS:
        scene, meshes = build(key, seed, tmp_path)
        masks = hidden_contact_faces([(m.node_id, m.points, m.triangles, m.material_id) for m in meshes],
                                     coplanar.opaque_materials(scene))
        assert sum(int(m.sum()) for m in masks) == expected(scene), (key, seed)


@pytest.mark.parametrize("key,least", [("crate", 18), ("shelf", 12), ("barrel", 16)])
def test_the_other_props_hide_at_least_their_pressed_faces(key, least, tmp_path):
    for seed in SEEDS:
        scene, meshes = build(key, seed, tmp_path)
        masks = hidden_contact_faces([(m.node_id, m.points, m.triangles, m.material_id) for m in meshes],
                                     coplanar.opaque_materials(scene))
        assert sum(int(m.sum()) for m in masks) >= least, (key, seed)


def test_kit_templates_do_not_use_the_prop_helpers():
    """The kit pieces are built on the kit's metric (their snap test measures 0 mm gaps): they never went through _box / _cyl."""
    for fn in authoring.KIT_TEMPLATE_FNS.values():
        assert not {"_box", "_cyl", "_snap"} & set(fn.__code__.co_names), fn.__name__
