"""Surface distance between parts (kernel/surface_gap.py): the exact test behind tier1_qa's floating-part check."""

from __future__ import annotations

import numpy as np
import pytest

from antigravity3d.ir import NodeIR
from antigravity3d.kernel import surface_gap as sg
from antigravity3d.kernel.compile_ir import _box, primitive_mesh


def box(lo, hi):
    lo, hi = np.asarray(lo, float), np.asarray(hi, float)
    p, t = _box(*(hi - lo))
    return np.asarray(p, float) + (lo + hi) / 2, np.asarray(t)


def prim(kind, at=(0, 0, 0), **params):
    p, t = primitive_mesh(NodeIR(id="x", kind=kind, params=params))
    return np.asarray(p, float) + np.asarray(at, float), np.asarray(t)


def within(a, b, limit):
    (pa, ta), (pb, tb) = a, b
    return sg.within(sg.Tris(pa, ta), pa.min(0), pa.max(0), sg.Tris(pb, tb), pb.min(0), pb.max(0), limit)


def test_box_distance_apart_touching_crossing_nested():
    one = (np.zeros(3), np.ones(3))
    assert sg.box_distance(*one, np.array([1.003, 0, 0]), np.array([2, 1, 1])) == pytest.approx(0.003)
    assert sg.box_distance(*one, np.array([2, 2, 1]), np.array([3, 3, 2])) == pytest.approx(np.sqrt(2))  # corner to corner
    assert sg.box_distance(*one, np.array([1, 0, 0]), np.array([2, 1, 1])) == 0.0                       # face to face
    assert sg.box_distance(*one, np.array([0.5, 0.5, 0.5]), np.array([1.5, 1.5, 1.5])) == 0.0           # surfaces cross
    assert sg.box_distance(*one, np.array([0.3, 0.2, 0.4]), np.array([0.6, 0.7, 0.5])) == pytest.approx(0.2)  # inside: margin


def test_is_axis_box_needs_a_filled_closed_box():
    p, t = box((0, 0, 0), (1, 2, 3))
    assert sg.is_axis_box(p, p.min(0), p.max(0), 6.0)
    assert not sg.is_axis_box(p, p.min(0), p.max(0), 5.0)  # volume says it is not the whole box
    s, ts = prim("sphere", radius=1.0, segments=16, rings=8)
    assert not sg.is_axis_box(s, s.min(0), s.max(0), 4.0)


def test_sphere_beside_a_box_corner_is_not_touching_though_the_boxes_touch():
    """The audit's probe B1: a sphere 41 mm off a box's vertical corner edge. Their bounding boxes touch."""
    a = box((-0.5, -0.5, 0), (0.5, 0.5, 1))
    s = prim("sphere", (0.6, 0.6, 0.5), radius=0.1, segments=24, rings=12)
    assert not within(a, s, 0.002) and not within(a, s, 0.04) and within(a, s, 0.045)


def test_a_part_that_crosses_another_without_a_vertex_near_it_touches():
    """A barrel hoop (wider than the body, a few cm tall) crosses the body's surface; no vertex of either is near the other
    (13-17 mm on the saved stages): only the edge-through-face test sees it."""
    body = prim("cylinder", (0, 0, 0.5), radius=0.4, height=1.0, segments=16)
    hoop = prim("cylinder", (0, 0, 0.5), radius=0.416, height=0.04, segments=16)
    assert within(body, hoop, 1e-6) and within(hoop, body, 1e-6)
    # and no corner of either is within 10 mm of the other's surface
    from antigravity3d.kernel.fast_ops import _SurfaceIndex
    assert _SurfaceIndex(*hoop).distances(body[0], 0.0).min() > 0.01
    assert _SurfaceIndex(*body).distances(hoop[0], 0.0).min() > 0.01


def test_two_bars_in_a_cross_touch_with_no_corner_near():
    bar_x = box((-1, -0.05, -0.05), (1, 0.05, 0.05))
    bar_y = box((-0.05, -1, -0.05), (0.05, 1, 0.05))
    assert within(bar_x, bar_y, 1e-9)
    lifted = box((-0.05, -1, 0.07), (0.05, 1, 0.17))  # the same cross with the second bar 20 mm clear of the first
    assert not within(bar_x, lifted, 0.019) and within(bar_x, lifted, 0.021)


def test_face_on_face_gap_is_measured_edge_on_edge_too():
    a = box((0, 0, 0), (1, 1, 1))
    assert within(a, box((0.2, 0.2, 1.001), (0.8, 0.8, 1.2)), 0.002) and not within(a, box((0.2, 0.2, 1.003), (0.8, 0.8, 1.2)), 0.002)
    # edge to edge, skew: two bars 5 mm apart in z cross at right angles, one above the other
    low = box((-1, -0.05, 0), (1, 0.05, 0.1))
    high = box((-0.05, -1, 0.105), (0.05, 1, 0.2))
    assert within(low, high, 0.0051) and not within(low, high, 0.0049)


def test_a_part_inside_another_does_not_touch_it():
    outer = box((-1, -1, -1), (1, 1, 1))
    inner = prim("sphere", radius=0.2, segments=16, rings=8)
    assert not within(outer, inner, 0.002)
    assert within(outer, prim("sphere", (0.7995, 0, 0), radius=0.2, segments=16, rings=8), 0.002)  # its surface reaches the wall


def test_corner_touches_is_a_proof_and_its_miss_proves_nothing():
    cyl = prim("cylinder", radius=0.3, height=1.0, segments=16)
    cap = prim("cylinder", (0, 0, 0.6), radius=0.2, height=0.2, segments=16)       # rests on the top cap
    pc, tc = cyl
    t = sg.Tris(pc, tc)
    assert sg.corner_touches(cap[0], t, pc.min(0), pc.max(0), 0.002)
    lifted = prim("cylinder", (0, 0, 0.61), radius=0.2, height=0.2, segments=16)
    assert not sg.corner_touches(lifted[0], t, pc.min(0), pc.max(0), 0.002)
    # a hoop crossing the body: a miss here, yet they touch (within() finds it)
    hoop = prim("cylinder", (0, 0, 0.5), radius=0.416, height=0.04, segments=16)
    assert not sg.corner_touches(hoop[0], t, pc.min(0), pc.max(0), 0.002) and within(cyl, hoop, 1e-6)
