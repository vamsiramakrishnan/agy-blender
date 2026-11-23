"""The free-form geometry of the build API (antigravity3d.build.shapes): tube and loft, and the part file."""

from __future__ import annotations

import math

import numpy as np
import pytest

from antigravity3d.build import shapes
from antigravity3d.build.shapes import BuildError, loft, tube


def closed(points: np.ndarray, tris: np.ndarray) -> bool:
    """Every edge shared by exactly two faces running opposite ways (watertight and consistently wound)."""
    edges: dict[tuple[int, int], int] = {}
    for a, b, c in tris.tolist():
        for u, v in ((a, b), (b, c), (c, a)):
            edges[(u, v)] = edges.get((u, v), 0) + 1
    return all(n == 1 and edges.get((v, u)) == 1 for (u, v), n in edges.items())


def test_a_straight_rod_is_closed_outward_and_has_the_stated_triangle_cost():
    t = tube("rod", [(0, 0, 0), (0, 0, 1)], 0.05, 10, False, False)
    assert closed(t.points, t.tris)
    vol = shapes.signed_volume(t.points, t.tris)
    assert vol > 0 and vol == pytest.approx(10 / 2 * 0.05**2 * math.sin(2 * math.pi / 10) * 1.0)  # a 10-gon prism
    assert len(t.tris) == 2 * 10 + 2 * (10 - 2)  # one band, two flat caps


def test_a_90_degree_corner_keeps_the_section_on_both_legs():
    """The corner ring is a true mitre: every point of it lies on both legs' surfaces (radius 0.1 from each axis). A
    ring merely placed in the bisecting plane would come within 0.1 * cos(45 deg) of one axis: 29 % thinner."""
    t = tube("bend", [(0, 0, 0), (1, 0, 0), (1, 1, 0)], 0.1, 16, False, False)
    ring = t.points[16:32]
    to_leg1 = np.hypot(ring[:, 1], ring[:, 2])  # distance to the x axis
    to_leg2 = np.hypot(ring[:, 0] - 1, ring[:, 2])  # distance to the line x = 1 along y
    assert np.allclose(to_leg1, 0.1) and np.allclose(to_leg2, 0.1)
    assert np.allclose(ring @ np.array([1.0, 1.0, 0.0]), 1.0), "the ring lies in the bisecting plane x + y = 1"
    assert closed(t.points, t.tris) and shapes.signed_volume(t.points, t.tris) > 0


def test_frames_do_not_twist_on_a_bent_rod():
    """Parallel transport: across a bend the section turns only by the bend, so the quads of each leg are rectangles
    (each generator line is parallel to its leg)."""
    t = tube("rail", [(0, 0, 0), (0.3, 0.1, 0.2), (0.5, 0.6, 0.3), (0.2, 1.0, 0.8)], 0.02, 8, False, False)
    rings = t.points.reshape(-1, 8, 3)
    for i in range(len(rings) - 1):
        leg = t.centre[i + 1] - t.centre[i]
        gen = rings[i + 1] - rings[i]
        across = gen - np.outer(gen @ leg / (leg @ leg), leg)
        assert np.abs(across).max() < 1e-9, f"generators of segment {i} are not parallel to it"


def test_a_closed_tube_is_watertight_and_its_seam_does_not_twist():
    t = tube("hoop", [(1, 0, 0), (0, 1, 0.4), (-1, 0, 0), (0, -1, -0.4), (0.7, -0.7, 0.2)], 0.05, 8, True, True)
    assert closed(t.points, t.tris) and shapes.signed_volume(t.points, t.tris) > 0
    assert len(t.tris) == 2 * 8 * 5 * shapes.SMOOTH_STEPS, "8 rings per span, 2 * sides triangles per band"
    rings = t.points.reshape(-1, 8, 3)
    seam = np.linalg.norm(rings[0] - rings[-1], axis=1)
    ref = np.linalg.norm(rings[-1] - rings[-2], axis=1)
    # a quad strip twisted by the leftover turn would have generators of very different lengths across the seam
    assert seam.max() / seam.min() < 1.2 * ref.max() / ref.min() + 0.05


def test_a_tapered_rod_has_the_end_radii_asked_for_and_a_ring_at_each_value():
    t = tube("leg", [(0, 0, 0), (0, 0, 1)], [0.03, 0.02, 0.01], 12, False, False)
    rings = t.points.reshape(-1, 12, 3)
    assert len(rings) == 3, "a ring at every value's position"
    for ring, r, z in zip(rings, (0.03, 0.02, 0.01), (0.0, 0.5, 1.0), strict=True):
        assert np.allclose(np.hypot(ring[:, 0], ring[:, 1]), r) and np.allclose(ring[:, 2], z)


def test_smooth_passes_through_the_points_and_along_follows_the_curve():
    pts = [(0, 0, 0), (1, 0, 0), (1, 1, 0)]
    t = tube("s", pts, 0.05, 8, False, True)
    assert len(t.centre) == 1 + 2 * shapes.SMOOTH_STEPS
    for p in pts:
        assert np.linalg.norm(t.centre - p, axis=1).min() < 1e-12
    assert np.allclose(shapes.along(t.centre, 0.0), pts[0]) and np.allclose(shapes.along(t.centre, 1.0), pts[-1])


def test_whole_number_floats_are_whole_numbers():
    assert len(tube("t", [(0, 0, 0), (0, 0, 1)], 0.1, 8.0, False, False).tris) == 2 * 8 + 2 * 6
    with pytest.raises(BuildError, match="sides must be a whole number of at least 3, got 8.5"):
        tube("t", [(0, 0, 0), (0, 0, 1)], 0.1, 8.5, False, False)


def test_a_fold_on_a_smooth_tube_names_its_bend_and_does_not_offer_smooth():
    with pytest.raises(BuildError) as e:  # a hook (review repro r9): the bends at points 1 and 2 are tighter than the rod
        tube("hook", [(0, 0, 0), (0.3, 0, 0), (0.3, 0.03, 0), (0, 0.03, 0)], 0.02, 10, False, True)
    assert "the bend at point 1 is too tight" in str(e.value) or "the bend at point 2 is too tight" in str(e.value)
    assert "smooth=True" not in str(e.value)
    with pytest.raises(BuildError, match="the bend at point 1 is too tight"):  # a radius list adds rings inside legs
        tube("t", [(0, 0, 0), (1, 0, 0), (1, 0.05, 0)], [0.1, 0.1, 0.1, 0.1], 8, False, False)


@pytest.mark.parametrize("path, radius, closed_, message", [
    ([(0, 0, 0)], 0.1, False, "path needs at least 2 points, got 1"),
    ([(0, 0, 0), (0, 0, 0.00005)], 0.1, False, "point 0 (the first) and point 1 are 0.050 mm apart"),
    ([(0, 0, 0), (1, 0, 0), (0, 0.01, 0)], 0.1, False, "the path doubles back at point 1 (it turns by 179 degrees"),
    ([(0, 0, 0), (1, 0, 0), (1, 0.05, 0)], 0.1, False, "the bend at point 1 is too tight for this radius"),
    ([(0, 0, 0), (0, 0, 1)], 0.0, False, "radius must be a positive number in metres, got 0.0"),
    ([(0, 0, 0), (0, 0, 1)], [0.1, -0.1], False, "radius must be a positive number in metres (value 1 of the list)"),
    ([(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 0)], 0.01, True, "do not repeat the first point"),
    ([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [0.01, 0.02], True, "must start and end with the same value"),
])
def test_tube_refusals_name_the_part_and_the_point(path, radius, closed_, message):
    with pytest.raises(BuildError, match=r"^tube t: ") as e:
        tube("t", path, radius, 8, closed_, False)
    assert message in str(e.value)


def test_a_loft_is_closed_and_outward_and_a_clockwise_one_is_flipped_whole():
    ccw = [[(-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0)], [(-0.5, -0.5, 1), (0.5, -0.5, 1), (0.5, 0.5, 1), (-0.5, 0.5, 1)]]
    p, t, flipped = loft("l", ccw)
    assert not flipped and closed(p, t) and shapes.signed_volume(p, t) == pytest.approx(7 / 3)
    p, t, flipped = loft("l", [s[::-1] for s in ccw])
    assert flipped and closed(p, t) and shapes.signed_volume(p, t) == pytest.approx(7 / 3)
    p, t, flipped = loft("l", [ccw[0], [(0, 0, 1)]])  # a tip: a pyramid, no cap at the tip
    assert closed(p, t) and shapes.signed_volume(p, t) == pytest.approx(4 / 3) and len(t) == 4 + 2


def test_a_concave_end_section_is_capped_as_the_polygon_it_is():
    """A U-channel: a fan from the centroid would fold the cap over the channel's mouth. The kernel's polygon
    triangulation (the one extrude caps with) covers exactly the U, so the skin's volume is the U's area times its
    length."""
    u = [(0, 0), (0.3, 0), (0.3, 0.1), (0.2, 0.1), (0.2, 0.02), (0.1, 0.02), (0.1, 0.1), (0, 0.1)]
    area = 0.3 * 0.1 - 0.1 * 0.08
    p, t, flipped = loft("u", [[(x, y, 0) for x, y in u], [(x, y, 0.5) for x, y in u]])
    assert not flipped and closed(p, t) and shapes.signed_volume(p, t) == pytest.approx(area * 0.5)
    caps = t[np.ptp(p[t][:, :, 2], axis=1) == 0]
    assert len(caps) == 2 * (len(u) - 2)
    mid = p[caps].mean(axis=1)  # no cap triangle's centre lies in the channel's mouth
    assert not ((mid[:, 0] > 0.1) & (mid[:, 0] < 0.2) & (mid[:, 1] > 0.02)).any()


def test_a_section_listed_on_a_side_is_measured_by_its_own_plane():
    sq = [(0, -0.1, -0.1), (0, 0.1, -0.1), (0, 0.1, 0.1), (0, -0.1, 0.1)]  # in the y-z plane, travel along +x
    p, t, _ = loft("x", [sq, [(0.4 + x, y, z) for x, y, z in sq]])
    assert closed(p, t) and abs(shapes.signed_volume(p, t)) == pytest.approx(0.04 * 0.4)


@pytest.mark.parametrize("sections, message", [
    ([[(0, 0, 0), (1, 0, 0), (0, 1, 0)]], "at least two sections"),
    ([[(0, 0, 0), (1, 0, 0), (0, 1, 0)], [(0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)]], "section 1 has 4 points but section 0 has 3"),
    ([[(0, 0, 0), (1, 0, 0), (0, 1, 0)], [(0, 0, 1)], [(0, 0, 1), (1, 0, 1), (0, 1, 1)]], "only the first or the last section can be a tip"),
    ([[(0, 0, 0), (1, 0, 0)], [(0, 0, 1), (1, 0, 1)]], "section 0 has 2 points"),
    ([(0, 0, 0), (1, 0, 0), (0, 1, 0)], "sections is a list of sections, each a list of (x, y, z) points; this is one list"),
    ([[(0, 0, 0), (0.1, 0, 0), (0.1, 0.1, 0), (0, 0.1, 0)], [(0, 0, 0.1), (0, 0.1, 0.1), (0.1, 0.1, 0.1), (0.1, 0, 0.1)],
      [(0, 0, 0.2), (0.1, 0, 0.2), (0.1, 0.1, 0.2), (0, 0.1, 0.2)]], "section 1 runs the other way round from section 0"),
    ([[(0, 0, 0), (0.1, 0, 0), (0.2, 0, 0)], [(0, 0, 1), (0.1, 0, 1), (0.2, 0, 1)]], "section 0's points lie on one line"),
    ([[(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [(0.2, 0.2, 0), (0.8, 0.2, 0), (0.8, 0.8, 0), (0.2, 0.8, 0)]],
     "the sections enclose no volume (they lie in one plane)"),
    ([[(0, 0, 0), (1, 0, 0), (0, 1, 0), (1, 1, 0)], [(0, 0, 1), (1, 0, 1), (0, 1, 1), (1, 1, 1)]], "section 0 crosses itself (its parts wind opposite ways"),
    ([[(0, 0, 0), (4, 0, 0), (4, 3, 0), (2, -1, 0), (0, 3, 0)], [(0, 0, 1), (4, 0, 1), (4, 3, 1), (2, -1, 1), (0, 3, 1)]],
     "section 0 crosses or touches itself"),
    ([[(0, 0, 0), (1, 0, 0.5), (1, 1, 0), (0, 1, 0.5)], [(0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)]], "section 0 is an end, capped by a flat face"),
])
def test_loft_refusals(sections, message):
    with pytest.raises(BuildError, match=r"^loft l: ") as e:
        loft("l", sections)
    assert message in str(e.value)


def test_the_part_file_reads_back_through_the_kernel_reader_with_its_winding(tmp_path):
    from antigravity3d.kernel.mesh_io import triangle_parts

    t = tube("rod", [(0, 0, 0), (0.2, 0, 0.3)], 0.05, 6, False, False)
    (tmp_path / "p.ply").write_bytes(shapes.ply_bytes(t.points, t.tris))
    [(_, pts, faces, _uv)] = list(triangle_parts(tmp_path / "p.ply"))
    assert np.array_equal(faces, t.tris)
    assert np.array_equal(pts, t.points.astype(np.float32).astype(np.float64))
    back = shapes.unpack_ply(shapes.ply_bytes(t.points, t.tris))
    assert np.array_equal(back[1], t.tris)
