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


def test_identical_lods_are_perfect():
    s = sphere()
    p = render_lod_pair(s, s, screen_fraction=0.25)
    assert p.iou == 1.0 and p.shading_diff == 0.0 and p.boundary_px == 0.0
    assert not np.asarray(p.crop_diff).any()
    # object really is small: ~25% of 256 px across
    ys, xs = np.nonzero(p.mask_a)
    assert 0.2 * 256 < xs.max() - xs.min() < 0.3 * 256
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
    assert r.worst.index == 1 and r.transitions[1].iou < 0.9 and r.transitions[0].iou == 1.0
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
