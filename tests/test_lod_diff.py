from __future__ import annotations

import math
import time
from types import SimpleNamespace

import numpy as np
import pytest
import trimesh

from antigravity3d.kernel.fast_ops import generate_lods
from antigravity3d.kernel.lod_diff import (
    LOD_SCREEN_FRACTIONS, PopThresholds, default_transitions, lod_pop_report, render_lod_pair, transition_distance,
)


def part(tm):
    return SimpleNamespace(points=np.array(tm.vertices, float), triangles=np.array(tm.faces, np.int32))


def sphere(sub=3):
    return part(trimesh.creation.icosphere(sub, 0.5))


def test_transition_distance_hand_calculation():
    # R=1 m, covers 50% of a 60 deg frame: d = 1 / (0.5 * tan 30deg) = 3.4641
    assert transition_distance(1.0, 0.5) == pytest.approx(3.4641016, rel=1e-6)
    assert transition_distance(2.0, 1.0, 90.0) == pytest.approx(2.0)          # tan 45 = 1
    assert transition_distance(1.0, 0.25) == pytest.approx(2 * transition_distance(1.0, 0.5))
    assert LOD_SCREEN_FRACTIONS == (1.0, 0.5, 0.25, 0.1)
    assert default_transitions(1) == () and default_transitions(2) == (0.5,)
    assert default_transitions(4) == (0.5, 0.25, 0.1) and default_transitions(6) == (0.5, 0.25, 0.1, 0.05, 0.025)


def test_error_budget_and_level_fractions_share_one_definition():
    from antigravity3d.kernel.lod_diff import LOD_REFERENCE_SCREEN_PX, lod_candidate_fractions, lod_error_budget_m, lod_transitions

    # 1 px of a 1080 px frame when a 2 m sphere (R = 1) spans half of it: 540 px across 2 m
    assert LOD_REFERENCE_SCREEN_PX == 1080 and lod_error_budget_m(1.0, 0.5) == pytest.approx(2.0 / 540)
    # the same geometric error projects to the same pixels at the matching transition distance, any fov
    for fov in (40.0, 60.0, 90.0):
        d = transition_distance(1.0, 0.25, fov)
        px_per_m = (1080 / 2) / (d * math.tan(math.radians(fov) / 2))
        assert lod_error_budget_m(1.0, 0.25) * px_per_m == pytest.approx(1.0)
    assert lod_candidate_fractions() == (1.0, 0.5, 0.25, 0.1, 0.05, 0.025)
    a, b, c = (SimpleNamespace(points=None, triangles=None, screen_fraction=f) for f in (1.0, 0.25, 0.05))
    assert lod_transitions([a, b, c]) == (0.25, 0.05)  # a skipped candidate keeps its level's own switch
    assert lod_transitions([sphere(), sphere()]) == (0.5,)


def test_identical_lods_are_perfect():
    s = sphere()
    p = render_lod_pair(s, s, screen_fraction=0.25)
    assert p.iou == 1.0 and p.shading_diff == 0.0 and p.boundary_px == 0.0
    assert not np.asarray(p.crop_diff).any()
    # the bounding sphere (r = half bbox diagonal = 0.866) spans 25% of 256 px; the unit-diameter sphere inside it is 1/1.732 of that
    ys, xs = np.nonzero(p.mask_a)
    assert xs.max() - xs.min() + 1 == pytest.approx(0.25 * 256 / math.sqrt(3), abs=2)
    assert p.distance_m == pytest.approx(transition_distance(0.5 * math.sqrt(3), 0.25))
    r = lod_pop_report([s, s, s])
    assert r.ok() and all(t.iou == 1.0 for t in r.transitions)


def test_octahedron_lod_is_worst_and_fails():
    s = sphere(4)
    mid = generate_lods(s.points, s.triangles, (1.0, 0.25))[1]
    octa = part(trimesh.creation.icosphere(0, 0.5))      # 20 triangles
    r = lod_pop_report([s, mid, octa])
    assert len(r.transitions) == 2
    assert r.worst.index == 1 and r.transitions[1].iou < 0.9 < r.transitions[0].iou
    assert r.transitions[1].score > 5 * r.transitions[0].score
    assert not r.ok() and r.failing() == [r.transitions[1]]
    assert r.ok(PopThresholds(min_iou=0.5, max_shading_diff=0.5, max_boundary_px=20))
    line = r.one_line()
    assert "1 FAIL" in line and "pair 1" in line and "\n" not in line


def test_part_removed_scores_worse():
    body = trimesh.creation.box((1, 1, 1)).apply_translation((0, 0, 0.5))
    arm = trimesh.creation.box((0.2, 0.2, 1.4)).apply_translation((0.8, 0, 0.7))
    full = part(trimesh.util.concatenate([body, arm]))
    nobody = part(body)
    r = lod_pop_report([full, full, nobody], transitions=(0.5, 0.25))
    assert r.worst.index == 1 and r.transitions[1].iou < 0.95 and r.transitions[0].iou == 1.0
    assert r.transitions[1].boundary_px > 5


def test_real_lod_chain_is_ok_and_sheet_size():
    s = sphere(4)
    lods = generate_lods(s.points, s.triangles, (1.0, 0.5, 0.25, 0.1))
    r = lod_pop_report(lods, size=128)
    assert len(r.transitions) == 3 and r.ok()
    sheet = r.pair_sheet()
    assert sheet.size == (3 * 128, 3 * (128 + 16))
    assert lod_pop_report(lods[:2], size=64).pair_sheet().size == (192, 80)
    assert r.pairs[0].crop_a.size == (128, 128)
    assert [t.index for t in r.transitions] == [0, 1, 2]


def test_timing():
    s = sphere(4)
    lods = generate_lods(s.points, s.triangles, (1.0, 0.5, 0.25, 0.1))
    lod_pop_report(lods)
    t = time.perf_counter()
    r = lod_pop_report(lods)
    ms = (time.perf_counter() - t) * 1e3
    print(f"\nLOD pop report ({len(lods)} LODs, {len(lods[0].triangles)} tris top, 3 pairs @256): {ms:.0f} ms")
    assert ms < 1500 and r.pair_sheet() is not None


# --------------------------------------------------------------------------- the check lights what ships


def cylinder(sections: int):
    return part(trimesh.creation.cylinder(radius=0.4, height=1.0, sections=sections))


def test_the_pop_check_lights_each_level_with_its_shipped_normals():
    from antigravity3d.kernel import lod_diff
    from antigravity3d.kernel.compile_ir import crease_normals

    fine, coarse = sphere(4), sphere(2)
    lit = render_lod_pair(fine, coarse, screen_fraction=0.25, size=256)
    # a level that carries its normals is lit with exactly those
    radial = lambda p: (p.points / np.linalg.norm(p.points, axis=1, keepdims=True))[p.triangles]  # noqa: E731
    given = render_lod_pair(SimpleNamespace(**vars(fine), normals=radial(fine)), SimpleNamespace(**vars(coarse), normals=radial(coarse)),
                            screen_fraction=0.25, size=256)
    # both spheres ship smooth (their dihedral angles are under the crease angle), so the two agree closely, and
    # both show far less of a jump than one normal per triangle would
    flat = lambda p: np.repeat(np.asarray(crease_normals(p.points, p.triangles, crease_deg=0.0)).reshape(-1, 3, 3)[:, :1], 3, axis=1)  # noqa: E731
    faceted = render_lod_pair(SimpleNamespace(**vars(fine), normals=flat(fine)), SimpleNamespace(**vars(coarse), normals=flat(coarse)),
                              screen_fraction=0.25, size=256)
    assert abs(lit.shading_diff - given.shading_diff) < 0.01
    assert lit.shading_diff < 0.6 * faceted.shading_diff, (lit.shading_diff, faceted.shading_diff)
    assert lod_diff.shipped_normals(fine).shape == (len(fine.triangles), 3, 3)


def test_a_level_that_turns_faceted_is_judged_as_faceted():
    # 32 sides ship smooth (11 degrees between sides); 6 sides ship with hard edges (60 degrees, over the 40 degree
    # crease). The switch between them is a smooth tube becoming a faceted one, and the check has to see that:
    # judged as if both stayed smooth, the jump is smaller.
    smooth, hard = cylinder(32), cylinder(6)
    shipped = render_lod_pair(smooth, hard, screen_fraction=0.25, size=256, view="front")

    def round_normals(p):
        n = p.points * [1.0, 1.0, 0.0]
        n = np.where(np.linalg.norm(n, axis=1, keepdims=True) > 1e-9, n / np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-9), [0.0, 0.0, 1.0])
        return n[p.triangles]

    as_if_smooth = render_lod_pair(SimpleNamespace(**vars(smooth), normals=round_normals(smooth)),
                                   SimpleNamespace(**vars(hard), normals=round_normals(hard)), screen_fraction=0.25, size=256, view="front")
    assert shipped.shading_diff > as_if_smooth.shading_diff * 1.2, (shipped.shading_diff, as_if_smooth.shading_diff)


def test_shipped_normals_are_computed_once_per_level(monkeypatch):
    from antigravity3d.kernel import compile_ir

    calls = []
    real = compile_ir.crease_normals
    monkeypatch.setattr(compile_ir, "crease_normals", lambda p, t, *a, **k: calls.append(len(t)) or real(p, t, *a, **k))
    lods = [part(trimesh.creation.icosphere(s, 0.5)) for s in (4, 3, 2)]
    for lod, f in zip(lods[1:], (0.5, 0.25)):
        lod.screen_fraction = f
    rep = lod_pop_report(lods)
    assert len(rep.transitions) == 2 and sorted(calls) == sorted(len(lod.triangles) for lod in lods)
