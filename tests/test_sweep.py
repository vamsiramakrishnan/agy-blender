"""Reusable curved rods: topology, exact costs, frame stability, UVs, and refusal of invalid paths.

No model calls or engines. The examples are ordinary planar/nonplanar paths, including a bent arm rail and a
straight spindle specified by its endpoints; no asset-specific geometry is introduced into the implementation.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
import trimesh

from antigravity3d.ir import NodeIR
from antigravity3d.kernel import fast_ops, sweep
from antigravity3d.kernel.compile_ir import check_node, primitive_charts, primitive_mesh, triangle_count
from antigravity3d.kernel.types import MeshData


def _arc(n=17):
    return [(0.3 * math.cos(a), 0.3 * math.sin(a), 0.7) for a in np.linspace(0, math.pi, n)]


PATHS = [
    [[0, 0, 0], [0, 0, 1]],
    [[0, 0, 0], [0, 0, -1]],
    [[0, 0, 0], [1, 0, 0]],
    [[0, 0, 0], [0, -1, 0]],
    [[-0.09, 0.02, 0.4], [-0.18, 0.11, 0.8]],
    _arc(),
    [(0.3 * math.cos(a), 0.3 * math.sin(a), 0.12 * a) for a in np.linspace(0, 1.5 * math.pi, 25)],
    [(x, 0.1 * math.sin(2 * math.pi * x), 0) for x in np.linspace(-0.5, 0.5, 21)],
    [[0, 0, 0], [0, 0, 0.15], [0, 0, 0.4], [0.1, 0.03, 0.65], [0.25, 0.1, 0.8]],
]


def _node(path, *, radius=0.02, segments=12):
    return NodeIR(id="rod", kind="sweep", params={"path": path, "radius": radius, "segments": segments})


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("segments", [7, 12])
def test_sweep_is_one_closed_outward_solid_with_exact_cost(path, segments):
    node = _node(path, segments=segments)
    check_node(node)
    pts, tris = primitive_mesh(node)
    chart = primitive_charts(node)
    mesh = trimesh.Trimesh(pts, tris, process=False)
    assert pts.dtype == np.float32 and tris.dtype == chart.dtype == np.int32
    assert mesh.is_watertight and mesh.is_winding_consistent and mesh.volume > 0
    assert mesh.euler_number == 2 and len(mesh.split()) == 1
    assert mesh.area_faces.min() > 1e-9
    assert len(np.unique(pts, axis=0)) == len(pts)
    assert len(pts) == segments * len(path) + 2
    assert len(tris) == triangle_count(node) == 2 * segments * len(path)
    assert chart.shape == (len(tris),)
    # Checking both sides of every surface catches inside-out patches hidden by positive total volume.
    outside = mesh.triangles_center + mesh.face_normals * 1e-5
    inside = mesh.triangles_center - mesh.face_normals * 1e-5
    assert not fast_ops._winding_inside(outside, mesh.vertices[mesh.faces]).any()
    assert fast_ops._winding_inside(inside, mesh.vertices[mesh.faces]).all()
    again = sweep.build(path, node.params["radius"], segments)
    assert all(a.tobytes() == b.tobytes() for a, b in zip((pts, tris, chart), again))


@pytest.mark.parametrize("path", PATHS[:5])
@pytest.mark.parametrize("segments", [3, 12, 64])
def test_straight_sweep_volume_matches_regular_polygon_area_times_length(path, segments):
    r = 0.025
    mesh = trimesh.Trimesh(*primitive_mesh(_node(path, radius=r, segments=segments)), process=False)
    area = 0.5 * segments * math.sin(2 * math.pi / segments) * r * r
    length = np.linalg.norm(np.asarray(path[1]) - path[0])
    assert mesh.volume == pytest.approx(area * length, rel=3e-6)


@pytest.mark.parametrize("path", PATHS[4:])
def test_samples_are_preserved_and_caps_are_perpendicular_to_end_segments(path):
    r, n = 0.02, 12
    pts, tris, charts = sweep.build(path, r, n)
    rings = pts[:-2].reshape(len(path), n, 3).astype(np.float64)
    centers = np.asarray(path)
    assert np.allclose(rings.mean(axis=1), centers, atol=2e-8)
    assert np.allclose(np.linalg.norm(rings - centers[:, None, :], axis=2), r, atol=5e-8)
    assert np.allclose(pts[-2:], centers[[0, -1]])
    mesh = trimesh.Trimesh(pts, tris, process=False)
    for c, a, b, sign in ((n, centers[0], centers[1], -1), (n + 1, centers[-2], centers[-1], 1)):
        direction = (b - a) / np.linalg.norm(b - a)
        assert np.allclose(mesh.face_normals[charts == c], sign * direction, atol=2e-6)


@pytest.mark.parametrize("path", PATHS[5:])
def test_parallel_transport_does_not_flip_at_an_inflection_or_straight_span(path):
    points = np.asarray(path, dtype=np.float64)
    tangent, normal, binormal = sweep._frames(points)
    assert np.allclose(np.linalg.norm(normal, axis=1), 1)
    assert np.allclose(np.einsum("ij,ij->i", normal, tangent), 0, atol=1e-14)
    assert np.allclose(np.cross(normal, binormal), tangent)
    assert (np.einsum("ij,ij->i", normal[:-1], normal[1:]) > 0.9).all()


@pytest.mark.parametrize("path", PATHS[4:])
@pytest.mark.parametrize("scale", [[1, 1, 1], [1.7, 0.5, 2.2]])
def test_longitudinal_ribbons_unwrap_without_distortion(path, scale):
    node = _node(path)
    pts, tris = primitive_mesh(node)
    charts = primitive_charts(node)
    assert len(np.unique(charts)) == node.params["segments"] + 2
    mesh = MeshData(node.id, "", pts.astype(np.float64) * scale, tris, uv_charts=charts)
    _, unfolded_tris, unfolded_uv, unfolded_charts = fast_ops._unfold_charts(mesh.points, tris, charts)
    assert fast_ops._valid_charts(unfolded_uv, unfolded_tris, unfolded_charts).all()
    uv = fast_ops.unwrap_uvs(mesh, 512)
    assert uv.chart_source == "analytic" and uv.fitted
    assert uv.stretch_mean < 1e-5 and uv.angle_distortion < 1e-5
    assert uv.texel_density == pytest.approx(512)
    assert fast_ops.uv_overlap_fraction([uv]) == 0


BAD = [
    ({"path": []}, "2 to 128"),
    ({"path": [[0, 0, 0]]}, "2 to 128"),
    ({"path": [[0, 0, i] for i in range(129)]}, "2 to 128"),
    ({"path": "curve"}, "2 to 128"),
    ({"path": [[0, 0], [0, 0, 1]]}, "triple"),
    ({"path": [[0, 0, 0], [0, False, 1]]}, "finite numbers"),
    ({"path": [[0, 0, 0], [0, float("inf"), 1]]}, "finite numbers"),
    ({"path": [[0, 0, 0], [0, float("nan"), 1]]}, "finite numbers"),
    ({"path": [[0, 0, 0], [0, 0, 0]]}, "coincide"),
    ({"path": [[0, 0, 0], [0, 0, 1], [0, 0, 0]]}, "reverses direction"),
    ({"path": [[0, 0, 0], [1, 0, 0], [0, 0.01, 0]], "radius": 0.05}, "too large for the bend"),
    ({"path": _arc(), "radius": 0.31}, "too large for the bend"),
    ({"path": [[0, 0, 0], [1, 1, 0], [0, 1, 0], [1, 0, 0]]}, "crosses or touches"),
    ({"path": [[0, 0, 0], [1, 0, 0], [1, 1, 0], [-0.02, 1, 0], [-0.02, 0, 0]]}, "too close"),
    ({"radius": 0}, "radius"),
    ({"radius": -1}, "radius"),
    ({"radius": True}, "radius"),
    ({"radius": float("nan")}, "radius"),
    ({"segments": 2}, "segments"),
    ({"segments": 65}, "segments"),
    ({"segments": 12.5}, "segments"),
    ({"segments": True}, "segments"),
]


@pytest.mark.parametrize("override,message", BAD)
def test_invalid_sweep_has_a_node_specific_repair_message(override, message):
    params = {"path": [[0, 0, 0], [0, 0, 1]], "radius": 0.02, "segments": 12, **override}
    for fn in (sweep.validate, sweep.build):
        with pytest.raises(ValueError, match=f"^bad_rod: .*{message}"):
            fn(**params, nid="bad_rod")


def test_dense_straight_samples_are_not_falsely_treated_as_colliding_capsules():
    path = [[0, 0, float(z)] for z in np.linspace(0, 1, 128)]
    pts, tris, _ = sweep.build(path, 0.05)
    mesh = trimesh.Trimesh(pts, tris, process=False)
    assert mesh.is_watertight and mesh.is_winding_consistent and mesh.volume > 0


def test_curved_rod_does_not_fall_back_to_a_convex_collision_proxy():
    from antigravity3d.kernel.compile_ir import collider_proxy

    # The U-shaped opening must remain available to the existing concave collider builder.
    assert collider_proxy(_node(_arc())) is None
