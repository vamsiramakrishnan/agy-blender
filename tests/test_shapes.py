"""The shaped primitives (kernel/shapes.py): bevelled and inset boxes, bevelled cylinders, lathes, extrusions.

Geometry per shape (closed, outward, no degenerate triangle, volume against its formula, triangle count against
compile_ir.triangle_count, extents unchanged by a bevel), exact UV charts, shading creases, the errors an author
repairs from, the stage round trip, collision, and a scene with every shape through Tier-1, unwrap, LODs and colliders.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
import trimesh
from pxr import Usd

from antigravity3d.ir import MaterialIR, NodeIR, SceneIR, Transform
from antigravity3d.kernel import compile_ir, fast_ops, shapes
from antigravity3d.kernel.compile_ir import (author_scene, collider_proxy, primitive_charts, primitive_mesh, read_ir,
                                             triangle_count)
from antigravity3d.kernel.types import MeshData
from antigravity3d.state import EngineBudget

SIZE = (1.0, 0.6, 0.4)
U = [(-0.1, 0.0), (-0.08, 0.0), (-0.08, 0.04), (0.08, 0.04), (0.08, 0.0), (0.1, 0.0), (0.1, 0.06), (-0.1, 0.06)]
CIRCLE_CW = [(math.cos(-a), math.sin(-a)) for a in np.linspace(0, 2 * np.pi, 20, endpoint=False)]


def box(**p) -> NodeIR:
    return NodeIR(id="b", kind="box", params={"size": SIZE, **p})


SHAPES = [
    *[box(bevel=0.05, bevel_segments=s) for s in (1, 2, 3, 4)],
    box(bevel=0.03, bevel_segments=3, inset_faces=["+x", "-x", "+y", "+z", "-z"], inset_margin=0.04, inset_depth=0.02),
    box(inset_faces=["+y", "-y"], inset_margin=0.05, inset_depth=0.1),
    box(bevel=0.02, bevel_segments=1, inset_faces=["-z"], inset_margin=0.02, inset_depth=0.3),
    NodeIR(id="cyb", kind="cylinder", params={"radius": 0.3, "height": 0.8, "segments": 16, "bevel": 0.05, "bevel_segments": 2}),
    NodeIR(id="cyb1", kind="cylinder", params={"radius": 0.3, "height": 0.8, "segments": 7, "bevel": 0.1, "bevel_segments": 1}),
    NodeIR(id="vase", kind="lathe", params={"profile": [(0, 0.5), (0.2, 0.5), (0.3, 0.2), (0.25, -0.3), (0.1, -0.5)], "segments": 12}),
    NodeIR(id="vase_up", kind="lathe", params={"profile": [(0.1, -0.5), (0.25, -0.3), (0.3, 0.2), (0.2, 0.5)], "segments": 12}),
    NodeIR(id="cup", kind="lathe", params={"profile": [(0, 0), (0.3, 0), (0.35, 0.4), (0.3, 0.4), (0.26, 0.05), (0, 0.05)], "segments": 12}),
    NodeIR(id="disc", kind="lathe", params={"profile": [(0.4, 0.05), (0.4, -0.05)], "segments": 9}),
    NodeIR(id="torus", kind="lathe", params={"profile": [(0.5, 0), (0.6, 0.1), (0.7, 0), (0.6, -0.1)], "segments": 16, "closed": True}),
    NodeIR(id="pipe", kind="lathe", params={"profile": [(0.3, 0.5), (0.3, -0.5), (0.25, -0.5), (0.25, 0.5), (0.3, 0.5)], "segments": 10,
                                            "closed": True}),
    NodeIR(id="bracket", kind="extrude", params={"outline": U, "depth": 0.03}),
    NodeIR(id="ell", kind="extrude", params={"outline": [(0, 0), (1, 0), (1, 0.2), (0.2, 0.2), (0.2, 1), (0, 1)], "depth": 0.1}),
    NodeIR(id="disc_cw", kind="extrude", params={"outline": CIRCLE_CW, "depth": 0.3}),
    NodeIR(id="wedge", kind="extrude", params={"outline": [(0, 0), (0.5, 0), (0.5, 0.0001), (0, 0.3)], "depth": 0.2}),
]


def _id(n: NodeIR) -> str:
    return f"{n.id}-{n.params.get('bevel_segments', '')}-{len(n.params.get('inset_faces', []))}"


def _mesh(n: NodeIR) -> trimesh.Trimesh:
    return trimesh.Trimesh(*primitive_mesh(n), process=False)


@pytest.mark.parametrize("node", SHAPES, ids=_id)
def test_shape_is_closed_outward_and_costs_what_triangle_count_says(node: NodeIR) -> None:
    pts, tris = primitive_mesh(node)
    assert pts.dtype == np.float32 and tris.dtype == np.int32
    m = trimesh.Trimesh(pts, tris, process=False)
    assert m.is_watertight and m.is_winding_consistent and m.volume > 0
    assert m.area_faces.min() > 1e-6 * m.area  # no degenerate triangle
    assert len(np.unique(pts.round(7), axis=0)) == len(pts)  # every vertex once: the shape is welded by construction
    assert len(tris) == triangle_count(node)
    charts = primitive_charts(node)
    assert charts.shape == (len(tris),) and charts.dtype == np.int32
    a, b = primitive_mesh(node)
    assert a.tobytes() == pts.tobytes() and b.tobytes() == tris.tobytes()  # deterministic


def test_triangle_count_table() -> None:
    """The per-shape cost formulas handed to the authoring guidance: a change here must be passed on."""
    for s, want in ((0, 12), (1, 44), (2, 108), (3, 204), (4, 332)):
        assert triangle_count(box(bevel=0.05 if s else 0, bevel_segments=max(s, 1))) == want == 12 + 16 * s * s + 16 * s
    assert triangle_count(box(bevel=0.05, bevel_segments=2, inset_faces=["+x", "-z"], inset_margin=0.02, inset_depth=0.01)) == 108 + 32
    assert triangle_count(NodeIR(id="c", kind="cylinder", params={"radius": 1, "height": 1, "segments": 12, "bevel": 0.1,
                                                                   "bevel_segments": 3})) == 12 * (4 * 3 + 4)
    assert triangle_count(NodeIR(id="c", kind="cylinder", params={"radius": 1, "height": 1})) == 24 * 4
    for n in PRIMS_OLD:
        assert triangle_count(n) == len(primitive_mesh(n)[1]), n.id
    assert triangle_count(NodeIR(id="g", kind="group")) == 0
    with pytest.raises(ValueError):
        triangle_count(NodeIR(id="r", kind="mesh_ref", params={"path": "x.glb"}))


PRIMS_OLD = [
    NodeIR(id="co", kind="cone", params={"radius": 0.5, "height": 2.0, "segments": 7}),
    NodeIR(id="s", kind="sphere", params={"radius": 1.0}),
    NodeIR(id="s3", kind="sphere", params={"radius": 1.0, "segments": 3, "rings": 2}),
    NodeIR(id="ca", kind="capsule", params={"radius": 0.25, "height": 1.0}),
    NodeIR(id="ca0", kind="capsule", params={"radius": 0.25, "height": 0}),
    NodeIR(id="ca9", kind="capsule", params={"radius": 0.25, "height": 1.0, "segments": 9}),
]


def test_plain_box_is_unchanged() -> None:
    """A box without bevel or inset is the 12-triangle box it always was (packages stay byte-identical)."""
    pts, tris = primitive_mesh(box())
    assert len(tris) == 12 and len(pts) == 8 and collider_proxy(box()) is None
    assert pts.tobytes() == compile_ir._box(*SIZE)[0].tobytes()


@pytest.mark.parametrize("s", [1, 2, 3, 4])
def test_bevel_keeps_the_extents_and_the_faces(s: int) -> None:
    m = _mesh(box(bevel=0.05, bevel_segments=s))
    assert np.allclose(m.bounds, [[-0.5, -0.3, -0.2], [0.5, 0.3, 0.2]], atol=1e-7)
    # each of the six faces is still flat at its plane, inside the bevel
    for ax in range(3):
        for sg in (-1, 1):
            on = np.abs(m.triangles_center[:, ax] - sg * SIZE[ax] / 2) < 1e-7
            assert m.area_faces[on].sum() == pytest.approx(np.prod([SIZE[k] - 0.1 for k in range(3) if k != ax]), rel=1e-6)
    cyl = _mesh(NodeIR(id="c", kind="cylinder", params={"radius": 0.3, "height": 0.8, "segments": 16, "bevel": 0.05, "bevel_segments": s}))
    assert cyl.bounds[0][2] == pytest.approx(-0.4) and cyl.bounds[1][2] == pytest.approx(0.4)
    assert np.abs(cyl.vertices[:, :2]).max() == pytest.approx(0.3, rel=1e-6)


def _ideal_rounded_box(size, b: float) -> float:
    x, y, z = size
    return x * y * z - (4 - math.pi) * b * b * (x + y + z - 6 * b) - 8 * b ** 3 * (1 - math.pi / 6)


def test_box_volumes() -> None:
    x, y, z = SIZE
    b = 0.05
    # one segment: twelve prisms of b*b/2 per length and eight corner tetrahedra of b^3/6 cut from the box
    chamfer = x * y * z - 4 * (b * b / 2) * (x + y + z - 6 * b) - 8 * b ** 3 * (1 - 1 / 6) + 0 * 0
    # corners: the cube b^3 at each corner minus the octant's tetra... expressed directly:
    a = np.array(SIZE) / 2 - b
    v1 = (8 * a.prod() + 2 * b * 4 * (a[0] * a[1] + a[1] * a[2] + a[0] * a[2]) + 0.5 * b * b * 2 * 4 * a.sum() * 1 + 8 * b ** 3 / 6)
    assert _mesh(box(bevel=b, bevel_segments=1)).volume == pytest.approx(v1, rel=1e-6)
    assert chamfer == pytest.approx(v1, rel=1e-9)
    ideal = _ideal_rounded_box(SIZE, b)
    vols = [_mesh(box(bevel=b, bevel_segments=s)).volume for s in (1, 2, 3, 4)]
    assert vols == sorted(vols) and vols[-1] < ideal  # inscribed: more segments, closer to the rounded box from inside
    assert vols[-1] == pytest.approx(ideal, rel=2e-3)  # 4 segments: 0.16 % inside
    # a recessed panel removes exactly (panel area x depth)
    d, m = 0.02, 0.04
    plain = _mesh(box(bevel=0.03, bevel_segments=3)).volume
    inset = _mesh(box(bevel=0.03, bevel_segments=3, inset_faces=["+x", "-z"], inset_margin=m, inset_depth=d)).volume
    px = (y - 2 * 0.03 - 2 * m) * (z - 2 * 0.03 - 2 * m)
    pz = (x - 2 * 0.03 - 2 * m) * (y - 2 * 0.03 - 2 * m)
    assert plain - inset == pytest.approx((px + pz) * d, rel=1e-6)


def _lathe_volume(rows, n: int, closed: bool) -> float:
    """Exact volume of an n-gon lathe: each band is an n-gon frustum, signed by the direction of travel."""
    k = 0.5 * n * math.sin(2 * math.pi / n)
    pairs = list(zip(rows, rows[1:] + rows[:1] if closed else rows[1:]))
    return k * sum(-(z2 - z1) * (r1 * r1 + r1 * r2 + r2 * r2) / 3 for (r1, z1), (r2, z2) in pairs)


@pytest.mark.parametrize("node", [n for n in SHAPES if n.kind in ("lathe", "cylinder")], ids=_id)
def test_lathe_volume(node: NodeIR) -> None:
    if node.kind == "lathe":
        rows = node.params["profile"]
        closed = bool(node.params.get("closed"))
        if closed and tuple(rows[0]) == tuple(rows[-1]):
            rows = rows[:-1]
        if not closed:
            rows = ([(0, rows[0][1])] if rows[0][0] else []) + list(rows) + ([(0, rows[-1][1])] if rows[-1][0] else [])
    else:
        p = node.params
        rows, closed = shapes.rounded_cylinder_profile(p["radius"], p["height"], p["bevel"], p["bevel_segments"]), False
    assert _mesh(node).volume == pytest.approx(abs(_lathe_volume(rows, node.params["segments"], closed)), rel=1e-5)


def test_lathe_profile_direction_does_not_matter() -> None:
    down, up = SHAPES[[n.id for n in SHAPES].index("vase")], SHAPES[[n.id for n in SHAPES].index("vase_up")]
    assert _mesh(down).volume == pytest.approx(_mesh(up).volume, rel=1e-6)
    torus = SHAPES[[n.id for n in SHAPES].index("torus")]
    rev = NodeIR(id="t", kind="lathe", params={**torus.params, "profile": torus.params["profile"][::-1]})
    assert _mesh(rev).volume == pytest.approx(_mesh(torus).volume, rel=1e-6) and _mesh(rev).volume > 0


@pytest.mark.parametrize("node", [n for n in SHAPES if n.kind == "extrude"], ids=_id)
def test_extrude_volume_is_area_times_depth(node: NodeIR) -> None:
    area = abs(shapes.signed_area([tuple(p) for p in node.params["outline"]]))
    assert _mesh(node).volume == pytest.approx(area * node.params["depth"], rel=1e-6)
    m = _mesh(node)
    assert m.bounds[0][2] == pytest.approx(-node.params["depth"] / 2) and m.bounds[1][2] == pytest.approx(node.params["depth"] / 2)


# --------------------------------------------------------------------------- UV charts


@pytest.mark.parametrize("node", SHAPES, ids=_id)
def test_charts_are_exact_and_open(node: NodeIR) -> None:
    """Every chart unfolds with zero area and angle distortion at the requested texel density, and no chart is a
    closed ring (the unfold would cut a ring at an arbitrary edge, possibly across a flat face)."""
    pts, tris = primitive_mesh(node)
    charts = primitive_charts(node)
    p = pts.astype(np.float64)
    vm, t2, uv, ch = fast_ops._unfold_charts(p, tris, charts)
    for c in np.unique(ch):
        src, n = np.unique(vm[np.unique(t2[ch == c])], return_counts=True)
        cut = src[n > 1]
        if node.kind in ("box", "extrude"):  # open strips and flat pieces: each source vertex appears once
            assert not len(cut), f"chart {c} was cut by the unfold"
        else:  # a lathe band is a ring, cut along one meridian, as a sphere's are (at most its two ring vertices)
            direction = pts[cut, :2] / np.linalg.norm(pts[cut, :2], axis=1, keepdims=True)
            assert len(cut) <= 2 and np.allclose(direction, direction[:1], atol=1e-5)
    u = fast_ops.unwrap_uvs(MeshData(node.id, "", p, tris, uv_charts=charts), 512.0)
    assert u.chart_source == "analytic" and u.fitted
    assert u.stretch_mean < 1e-5 and u.angle_distortion < 1e-5  # zero but for float32 uv rounding
    assert u.stretch_max < (2e-3 if node.id == "wedge" else 1e-4)  # the wedge's 0.1 mm sliver side rounds worst
    assert u.texel_density == pytest.approx(512.0)


def test_rounded_box_charts_are_few_and_fill_their_rectangles() -> None:
    """Chart count and how much of their packing rectangles the charts fill: the measure that decides the texel
    density an atlas reaches (measured: halves of each latitude ring filled 0.90 / 0.86 at 3 / 4 segments and a
    frame with a hole 0.57 on a lid, and the crate's wood atlas missed 1024 px/m; quarters and frame-plus-wall pieces
    fill >= 0.91 and meet it)."""
    for s, charts in ((1, 12), (2, 20), (3, 28), (4, 36)):
        assert len(np.unique(primitive_charts(box(bevel=0.05, bevel_segments=s)))) == charts == 4 + 8 * s
    for node in (box(bevel=0.02, bevel_segments=3, inset_faces=["+y", "-y"], inset_margin=0.04, inset_depth=0.015),
                 NodeIR(id="lid", kind="box", params={"size": (0.82, 0.52, 0.06), "bevel": 0.015, "bevel_segments": 2,
                                                      "inset_faces": ["+z"], "inset_margin": 0.05, "inset_depth": 0.01})):
        pts, tris = primitive_mesh(node)
        c = fast_ops._charted(MeshData("n", "", pts.astype(np.float64), tris, uv_charts=primitive_charts(node)), 2.0)
        area = fast_ops._tri_areas(c.uv_m, c.tris, 2).sum()
        rect = sum(w * h for *_, (w, h) in fast_ops._chart_frames(c))
        assert area / rect > 0.9


# --------------------------------------------------------------------------- shading


def test_crease_angle_and_the_two_segment_profile() -> None:
    """The 2-segment profile is placed for a 40 degree crease (shapes.quarter): change one, revisit the other."""
    assert shapes.CREASE_DEG == compile_ir.CREASE_DEG == fast_ops.UV_SMOOTH_CREASE_DEG == 40.0
    q = np.array(shapes.quarter(2))
    d = np.diff(q, axis=0)
    turns = [math.degrees(math.acos(np.clip(a @ b / np.linalg.norm(a) / np.linalg.norm(b), -1, 1)))
             for a, b in zip([np.array([0.0, 1.0]), *d], [*d, np.array([-1.0, 0.0])])]
    assert sum(turns) == pytest.approx(90.0) and turns == pytest.approx([30.0, 30.0, 30.0])
    assert max(turns) < compile_ir.CREASE_DEG - 5  # the corner quads bend more than the edge itself (shapes.quarter)


@pytest.mark.parametrize("s", [1, 2, 3, 4])
@pytest.mark.parametrize("kind", ["box", "cylinder"])
def test_one_segment_is_faceted_more_are_smooth(s: int, kind: str) -> None:
    params = {"size": SIZE} if kind == "box" else {"radius": 0.3, "height": 0.8, "segments": 64}
    m = _mesh(NodeIR(id="n", kind=kind, params={**params, "bevel": 0.05, "bevel_segments": s}))
    ang = np.degrees(m.face_adjacency_angles)
    bent = ang[ang > 1e-3]
    if s == 1:
        assert (bent > compile_ir.CREASE_DEG).sum() >= (24 if kind == "box" else 128)  # face | chamfer | face
    else:
        assert bent.max() < compile_ir.CREASE_DEG  # nothing creases: crease_normals shades the bevel smooth
    # and the shaded normals: a chamfer splits them at both its edges, a rounded bevel keeps every vertex's corners
    # within a few degrees of each other (2 segments: 6.8 at the box corners, see shapes.quarter)
    pts, tris = primitive_mesh(NodeIR(id="n", kind=kind, params={**params, "bevel": 0.05, "bevel_segments": s}))
    nn = compile_ir.crease_normals(pts, tris).reshape(-1, 3)
    flat = tris.reshape(-1)
    spread = max(math.degrees(math.acos(np.clip((g := nn[flat == v]) @ g.T, -1, 1).min())) for v in range(len(pts)))
    assert spread >= 40 if s == 1 else spread < 10


# --------------------------------------------------------------------------- errors


@pytest.mark.parametrize("params,kind,message", [
    ({"size": SIZE, "bevel": 0.2}, "box", r"bevel 0\.2 must be below half the smallest extent \(0\.2\)"),
    ({"size": SIZE, "bevel": 0.05, "bevel_segments": 5}, "box", r"bevel_segments' must be an integer from 1 to 4"),
    ({"size": SIZE, "inset_faces": ["+x"], "inset_margin": 0.2, "inset_depth": 0.01}, "box",
     r"inset_margin 0\.2 leaves no panel on face \+x: the flat part of the face is 0\.4 wide along z"),
    ({"size": SIZE, "inset_faces": ["+z"], "inset_margin": 0.05, "inset_depth": 0.4}, "box", r"inset_depth 0\.4 on face \+z reaches through the box"),
    ({"size": SIZE, "inset_faces": ["+z", "-z"], "inset_margin": 0.05, "inset_depth": 0.2}, "box",
     r"reaches into the inset from the opposite face -z"),
    ({"size": SIZE, "bevel": 0.01, "inset_faces": ["+x", "+y"], "inset_margin": 0.02, "inset_depth": 0.03}, "box",
     r"adjacent faces \+x and \+y .* keep inset_depth below bevel \+ inset_margin \(0\.03\)"),
    ({"size": SIZE, "inset_faces": ["+x"], "inset_margin": 0.05}, "box", r"inset_depth must be > 0"),
    ({"size": SIZE, "inset_faces": ["+x"], "inset_depth": 0.05}, "box", r"inset_margin must be > 0"),
    ({"size": SIZE, "inset_faces": ["top"], "inset_margin": 0.05, "inset_depth": 0.01}, "box", r"\['top'\] are not face names"),
    ({"size": SIZE, "inset_faces": ["+x", "+x"], "inset_margin": 0.05, "inset_depth": 0.01}, "box", r"names a face twice"),
    ({"radius": 0.3, "height": 0.8, "bevel": 0.3}, "cylinder", r"bevel 0\.3 must be below the radius"),
    ({"profile": [(0, 1), (-0.1, 0), (0, -1)]}, "lathe", r"profile point 1 has negative radius"),
    ({"profile": [(0, 1), (0.3, 0.5), (0, 0), (0.3, -0.5), (0, -1)]}, "lathe", r"profile point 2 lies on the axis"),
    ({"profile": [(0, 1), (0.3, 1), (0.3, 1), (0, -1)]}, "lathe", r"profile points 1 and 2 coincide"),
    ({"profile": [(0, 1), (0.3, 0), (0.0, 0.0001), (0.3, 0.5), (0, -1)]}, "lathe", r"lies on the axis"),
    ({"profile": [(0, 1), (0.4, 0.5), (0.1, 0.5), (0.4, 0.0), (0.2, 0.9), (0, -1)]}, "lathe", r"crosses or touches itself"),
    ({"profile": [(0.5, 0), (0.7, 0.1)], "closed": True}, "lathe", r"needs at least 3 distinct points"),
    ({"profile": [(0.5, 0), (0.7, 0.1), (0.0, 0.1)], "closed": True}, "lathe", r"closed profile must not touch the axis"),
    ({"profile": [(0.5, 0), (0.7, 0.1), (0.7, -0.1), (0.5, 0.1)], "closed": True}, "lathe", r"crosses or touches itself"),
    ({"profile": [(0.5, 0), (0.6, 0), (0.7, 0)], "closed": True}, "lathe", r"(folds back|no area)"),
    ({"profile": [(0, 1), (0, -1)]}, "lathe", r"encloses nothing"),
    ({"outline": [(0, 0), (1, 1), (1, 0), (0, 1)], "depth": 0.1}, "extrude", r"outline crosses or touches itself"),
    ({"outline": [(0, 0), (1, 0), (1, 0), (0, 1)], "depth": 0.1}, "extrude", r"outline points 1 and 2 coincide"),
    ({"outline": [(0, 0), (1, 0), (2, 0)], "depth": 0.1}, "extrude", r"(folds back|no area)"),
    ({"outline": [(0, 0), (1, 0)], "depth": 0.1}, "extrude", r"at least 3 distinct points"),
    ({"outline": [(0, 0), (1, 0), (0, 1)], "depth": 0}, "extrude", r"'depth' must be > 0"),
    ({"outline": "square", "depth": 1}, "extrude", r"must be a list of"),
])
def test_bad_shapes_say_why(params: dict, kind: str, message: str) -> None:
    with pytest.raises(ValueError, match=f"^bad_node: .*{message}"):
        primitive_mesh(NodeIR(id="bad_node", kind=kind, params=params))


def test_repeated_closing_point_is_accepted() -> None:
    sq = [(0, 0), (1, 0), (1, 1), (0, 1)]
    a = primitive_mesh(NodeIR(id="a", kind="extrude", params={"outline": sq, "depth": 1}))
    b = primitive_mesh(NodeIR(id="a", kind="extrude", params={"outline": [*sq, (0, 0)], "depth": 1}))
    assert a[0].tobytes() == b[0].tobytes() and a[1].tobytes() == b[1].tobytes()
    assert triangle_count(NodeIR(id="a", kind="extrude", params={"outline": [*sq, (0, 0)], "depth": 1})) == len(a[1])


def test_inset_face_order_does_not_change_the_mesh() -> None:
    p = dict(bevel=0.02, inset_margin=0.03, inset_depth=0.01)
    a, b = primitive_mesh(box(inset_faces=["+x", "-y"], **p)), primitive_mesh(box(inset_faces=["-y", "+x"], **p))
    assert a[0].tobytes() == b[0].tobytes() and a[1].tobytes() == b[1].tobytes()


# --------------------------------------------------------------------------- stage, collision, pipeline


GUARD_OUTLINE = [(0, 0), (0.06, 0), (0.06, 0.006), (0.006, 0.006), (0.006, 0.06), (0, 0.06)]


def _crate() -> SceneIR:
    def T(t, r=(0, 0, 0)):
        return Transform(translate=t, rotate_euler_deg=r)

    corners = ((1, 1), (-1, 1), (-1, -1), (1, -1))
    nodes = [
        NodeIR(id="body", kind="box", material_id="wood", transform=T((0, 0, 0.265)),
               params={"size": (0.8, 0.5, 0.45), "bevel": 0.02, "bevel_segments": 3, "inset_faces": ["+y", "-y"],
                       "inset_margin": 0.04, "inset_depth": 0.015}),
        NodeIR(id="lid", kind="box", material_id="wood", transform=T((0, 0, 0.52)),
               params={"size": (0.82, 0.52, 0.06), "bevel": 0.015, "bevel_segments": 2, "inset_faces": ["+z"],
                       "inset_margin": 0.05, "inset_depth": 0.01}),
        *[NodeIR(id=f"foot_{i}", kind="cylinder", material_id="metal", transform=T((sx * 0.33, sy * 0.18, 0.02)),
                 params={"radius": 0.04, "height": 0.04, "segments": 16, "bevel": 0.01, "bevel_segments": 2})
          for i, (sx, sy) in enumerate(corners)],
        # L-section corner guards wrapping the rounded vertical edges, their inner faces on the body's flat faces
        *[NodeIR(id=f"guard_{i}", kind="extrude", material_id="metal", transform=T((sx * 0.406, sy * 0.256, 0.265), (0, 0, rz)),
                 params={"outline": GUARD_OUTLINE, "depth": 0.38})
          for i, ((sx, sy), rz) in enumerate(zip(corners, (180, -90, 0, 90)))],
        NodeIR(id="handle_px", kind="extrude", material_id="metal", transform=T((0.4, 0, 0.32), (0, 0, -90)),
               params={"outline": U, "depth": 0.03}),
        NodeIR(id="handle_nx", kind="extrude", material_id="metal", transform=T((-0.4, 0, 0.32), (0, 0, 90)),
               params={"outline": U, "depth": 0.03}),
        NodeIR(id="knob", kind="lathe", material_id="metal", transform=T((0.25, 0, 0.54)),
               params={"profile": [(0.0, 0.06), (0.02, 0.06), (0.035, 0.045), (0.03, 0.025), (0.015, 0.012), (0.02, 0.0)],
                       "segments": 16}),
        NodeIR(id="ring", kind="lathe", material_id="metal", transform=T((-0.2, 0, 0.548)),
               params={"profile": [(0.04, -0.008), (0.048, 0.0), (0.04, 0.008), (0.032, 0.0)], "segments": 20, "closed": True}),
    ]
    return SceneIR(asset_name="ShapesCrate", nodes=nodes, materials=[
        MaterialIR(id="wood", base_color=(0.35, 0.4, 0.2), roughness=0.8),
        MaterialIR(id="metal", base_color=(0.2, 0.2, 0.22), metallic=1.0, roughness=0.4)])


def test_a_scene_with_every_shape_survives_the_stage_round_trip(tmp_path) -> None:
    from antigravity3d.kernel.usd_stage import StageManager

    scene = _crate()
    stage = Usd.Stage.CreateInMemory()
    author_scene(stage, scene)
    back = read_ir(stage.GetPrimAtPath("/ShapesCrate"))
    assert back == scene
    assert back.nodes[0].params["inset_faces"] == ["+y", "-y"] and back.nodes[-1].params["profile"][1] == (0.048, 0.0)
    before = stage.GetRootLayer().ExportToString()
    author_scene(stage, back)  # nothing changed: nothing re-authored
    assert stage.GetRootLayer().ExportToString().replace(" ", "") == before.replace(" ", "")
    sm = StageManager.create(tmp_path / "a.usda", scene.asset_name)
    sm.apply_ir(scene)
    sm.commit_all()
    sm2 = StageManager.open(tmp_path / "a.usda")
    assert read_ir(sm2.asset_prim) == scene
    meshes = {m.node_id: m for m in sm2.extract_meshes()}
    assert all(meshes[n.id].uv_charts is not None for n in scene.nodes)
    assert meshes["body"].collider_points is not None and len(meshes["body"].collider_points) == 8
    assert np.allclose(meshes["body"].collider_points.min(0), [-0.4, -0.25, 0.04], atol=1e-6)
    assert meshes["knob"].collider_points is None and meshes["handle_px"].collider_points is None
    # editing a bevel away removes the proxy
    plain = scene.model_copy(update={"nodes": [n.model_copy(update={"params": {"size": n.params["size"]}}) if n.id == "body" else n
                                               for n in scene.nodes]})
    sm2.apply_ir(plain)
    assert {m.node_id: m for m in sm2.extract_meshes()}["body"].collider_points is None


def _hulls(node: NodeIR, proxy: bool) -> list:
    pts, tris = primitive_mesh(node)
    cp = collider_proxy(node) if proxy else None
    m = MeshData(node.id, "", pts.astype(np.float64), tris, collider_points=None if cp is None else cp.astype(np.float64))
    return fast_ops.generate_colliders([m], 8)


def test_rounded_or_recessed_box_collides_as_the_sharp_box() -> None:
    sharp = _hulls(box(), True)
    assert len(sharp) == 1 and len(sharp[0].points) == 8
    for node in (box(bevel=0.05, bevel_segments=4), box(bevel=0.03, bevel_segments=2, inset_faces=["+x", "+z"], inset_margin=0.04,
                                                                inset_depth=0.02)):
        h = _hulls(node, True)
        assert len(h) == 1 and len(h[0].points) == 8 and h[0].volume == pytest.approx(sharp[0].volume, rel=1e-6)
        assert h[0].reason == fast_ops.PROXY_REASON
        assert fast_ops.PROXY_REASON in fast_ops.describe_collider_sources(h)
    # negative control: without the proxy the rounded box's hull carries every rim vertex
    assert len(_hulls(box(bevel=0.05, bevel_segments=4), False)[0].points) == 8 * (1 + 4 * 5)
    cyl = NodeIR(id="c", kind="cylinder", params={"radius": 0.3, "height": 0.8, "segments": 16, "bevel": 0.05, "bevel_segments": 3})
    assert len(_hulls(cyl, True)[0].points) == 32 < len(_hulls(cyl, False)[0].points)


def test_extrude_caps_are_a_separation_parameter() -> None:
    from antigravity3d.kernel.separate import _face_param, _nodes

    scene = SceneIR(asset_name="A", nodes=[NodeIR(id="e", kind="extrude", params={"outline": U, "depth": 0.03}),
                                          NodeIR(id="l", kind="lathe", params={"profile": [(0, 1), (1, 0), (0, -1)]})])
    nd = _nodes(scene)
    assert _face_param(nd["e"], 2) == ("depth", 2, 1.0)
    assert _face_param(nd["e"], 0) is None and _face_param(nd["l"], 2) is None


@pytest.fixture(scope="module")
def crate_meshes(tmp_path_factory):
    from antigravity3d.kernel.usd_stage import StageManager

    sm = StageManager.create(tmp_path_factory.mktemp("crate") / "a.usda", "ShapesCrate")
    sm.apply_ir(_crate())
    return sm.extract_meshes()


def test_tier1_finds_nothing_on_the_crate_and_finds_a_lifted_knob(crate_meshes) -> None:
    budget = EngineBudget("godot_4", max_tris=6000)
    rep = fast_ops.tier1_qa(crate_meshes, budget)
    assert rep.ok and not rep.issues, rep.one_line()
    # negative control: a knob 1 cm above the recessed lid panel floats
    lifted = [m if m.node_id != "knob" else MeshData(m.node_id, m.prim_path, m.points + [0, 0, 0.01], m.triangles) for m in crate_meshes]
    assert any(i.code == "floating_part" and i.node_id == "knob" for i in fast_ops.tier1_qa(lifted, budget).issues)


def test_the_crate_unwraps_exactly_at_the_target_density_with_lods_and_cheap_colliders(crate_meshes) -> None:
    from antigravity3d.kernel.coplanar import opaque_materials

    budget = EngineBudget("godot_4", max_tris=6000)
    b = fast_ops.run_export_pipeline(crate_meshes, budget, "ShapesCrate", opaque_materials=opaque_materials(_crate()))
    for u in b.uvs:
        assert u.chart_source == "analytic" and u.fitted and u.stretch_mean < 1e-5 and u.angle_distortion < 1e-5, u.node_id
        assert u.within(budget)
    rep = fast_ops.texel_density_report(b.uvs, budget.texel_density_px_per_m)
    assert "missed" not in fast_ops.describe_texel_density(rep)
    levels = max(len(v) for v in b.lods.values())
    tris = [sum(fast_ops.shipped(v[i]).tri_count for v in b.lods.values() if i < len(v)) for i in range(levels)]
    assert levels >= 2 and tris == sorted(tris, reverse=True) and tris[-1] < tris[0] / 2
    assert len(b.colliders) <= budget.max_collision_hulls
    assert any(fast_ops.PROXY_REASON in h.reason for h in b.colliders)
    from antigravity3d.kernel.validator import _is_convex

    # every hull is convex as shipped (float32), the check the package validation runs: before fast_ops._hull took
    # the hull of float32 values, the merged knob-and-ring hull of this crate failed it (79 of its 88 points on the hull)
    assert all(_is_convex(h.points.astype(np.float32).astype(np.float64), h.triangles) for h in b.colliders)


# --------------------------------------------------------------------------- parts the live author wrote (sugar's schema)

LAMP_BASE = NodeIR(id="lamp_base", kind="lathe", params={"segments": 24, "profile": [
    (0.0, 0.15), (0.026, 0.15), (0.028, 0.144), (0.024, 0.136), (0.052, 0.122), (0.068, 0.104), (0.064, 0.086), (0.036, 0.068),
    (0.02, 0.048), (0.028, 0.034), (0.018, 0.02), (0.048, 0.01), (0.056, 0.004), (0.056, 0.0), (0.0, 0.0)]})
CHIMNEY = NodeIR(id="chimney", kind="lathe", params={"segments": 24, "closed": True, "profile": [
    (0.022, 0.18), (0.022, 0.13), (0.042, 0.075), (0.031, 0.025), (0.031, 0.0), (0.029, 0.0), (0.029, 0.025), (0.04, 0.075),
    (0.02, 0.13), (0.02, 0.18)]})
GUARD_L = NodeIR(id="guard", kind="extrude", transform=Transform(rotate_euler_deg=(0, 0, 90)),
                 params={"outline": [(0, 0), (0.06, 0), (0.06, 0.01), (0.01, 0.01), (0.01, 0.06), (0, 0.06)], "depth": 0.06})
HANDLE_CW = NodeIR(id="handle", kind="extrude", params={"depth": 0.012, "outline": [
    (-0.062, 0.018), (-0.088, 0.016), (-0.096, 0.0), (-0.088, -0.024), (-0.056, -0.028), (-0.056, -0.021), (-0.082, -0.018),
    (-0.088, 0.0), (-0.082, 0.01), (-0.062, 0.011)]})
LIVE = [LAMP_BASE, CHIMNEY, GUARD_L, HANDLE_CW,
        NodeIR(id="ring4mm", kind="cylinder", params={"radius": 0.0025, "height": 0.004, "segments": 8, "bevel": 0.001, "bevel_segments": 1}),
        NodeIR(id="small_box", kind="box", params={"size": (0.012, 0.03, 0.02), "bevel": 0.002, "bevel_segments": 2}),
        NodeIR(id="panel_box", kind="box", params={"size": (0.8, 0.5, 0.4), "bevel": 0.012, "bevel_segments": 2,
                                                   "inset_faces": ["+x", "-x", "+y", "-y"], "inset_margin": 0.05, "inset_depth": 0.012})]


@pytest.mark.parametrize("node", LIVE, ids=lambda n: n.id)
def test_live_author_parts_build_closed_and_outward(node: NodeIR) -> None:
    m = _mesh(node)
    assert m.is_watertight and m.is_winding_consistent and m.volume > 0
    assert m.area_faces.min() > 1e-6 * m.area
    assert len(m.faces) == triangle_count(node)
    # outward: every face normal points away from the solid (a point just outside the face is not inside)
    probe = m.triangles_center + m.face_normals * 1e-5
    assert not fast_ops._winding_inside(probe, m.vertices[m.faces]).any()
    assert fast_ops._winding_inside(m.triangles_center - m.face_normals * 1e-5, m.vertices[m.faces]).mean() > 0.95
    u = fast_ops.unwrap_uvs(MeshData(node.id, "", m.vertices, m.faces, uv_charts=primitive_charts(node)), 1024.0)
    assert u.chart_source == "analytic" and u.stretch_mean < 1e-5


def test_lamp_base_discs_are_fans_at_the_poles() -> None:
    m = _mesh(LAMP_BASE)
    for z, nz in ((0.15, 1.0), (0.0, -1.0)):
        disc = np.abs(m.triangles_center[:, 2] - z) < 1e-6
        assert disc.sum() == 24 and np.allclose(m.face_normals[disc][:, 2], nz)  # one fan of 24, facing out
    assert _mesh(LAMP_BASE).volume == pytest.approx(_lathe_volume(LAMP_BASE.params["profile"], 24, False), rel=1e-6)


def test_chimney_inner_wall_faces_the_axis_and_separation_leaves_it_alone() -> None:
    from antigravity3d.kernel.separate import separate_coplanar

    m = _mesh(CHIMNEY)
    r = np.linalg.norm(m.triangles_center[:, :2], axis=1)
    radial = (m.face_normals[:, :2] * m.triangles_center[:, :2]).sum(1) / r
    lower = (m.triangles_center[:, 2] > 0.003) & (m.triangles_center[:, 2] < 0.022)  # the straight foot: walls at 31 and 29 mm
    assert (radial[lower & (r > 0.0305)] > 0.99).all() and (radial[lower & (r < 0.0295)] < -0.99).all()
    rows = CHIMNEY.params["profile"]
    assert m.volume == pytest.approx(abs(_lathe_volume(rows, 24, True)), rel=1e-6)
    scene = SceneIR(asset_name="Lamp", nodes=[CHIMNEY])
    pts, tris = primitive_mesh(CHIMNEY)
    res = separate_coplanar(scene, [MeshData("chimney", "", pts.astype(np.float64), tris)])
    assert not res.moves


def test_mirrored_extrude_from_sugar_parameters_stays_outward() -> None:
    """sugar's mirror shortcut reflects an extrusion by negating one outline coordinate and reversing the order (no
    negative scale); the builder normalises the winding from the signed area either way."""
    out = GUARD_L.params["outline"]
    for flipped in ([(-x, y) for x, y in out], [(-x, y) for x, y in out][::-1]):
        m = _mesh(NodeIR(id="g", kind="extrude", params={"outline": flipped, "depth": 0.06}))
        assert m.is_winding_consistent and m.volume == pytest.approx(_mesh(GUARD_L).volume, rel=1e-9)


# --------------------------------------------------------------------------- check_node: the compiler's refusals, without a mesh


def _random_nodes(seed: int, n: int) -> list[NodeIR]:
    """Nodes with random parameters, valid and invalid (sizes near the limits, profiles and outlines that may cross)."""
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n):
        k = ["box", "box", "cylinder", "lathe", "lathe", "extrude", "sphere", "capsule", "cone"][i % 9]
        if k == "box":
            size = tuple(float(x) for x in rng.uniform(0.01, 1.0, 3))
            faces = [f for f in shapes.BOX_FACES if rng.random() < 0.3]
            p = {"size": size, "bevel": float(rng.choice([0.0, rng.uniform(0, 0.6) * min(size)])), "bevel_segments": int(rng.integers(1, 5)),
                 "inset_faces": faces, "inset_margin": float(rng.uniform(0, 0.3) * min(size)), "inset_depth": float(rng.uniform(0, 0.4) * min(size))}
        elif k == "cylinder":
            r, h = (float(x) for x in rng.uniform(0.002, 0.5, 2))
            p = {"radius": r, "height": h, "segments": int(rng.integers(3, 33)), "bevel": float(rng.uniform(0, 0.6) * min(r, h)),
                 "bevel_segments": int(rng.integers(1, 5))}
        elif k == "lathe":
            m = int(rng.integers(2, 12))
            closed = bool(rng.random() < 0.3)
            prof = [(float(abs(rng.normal(0.2, 0.15))) if not (not closed and j in (0, m - 1) and rng.random() < 0.5) else 0.0,
                     float(0.5 - j / max(m - 1, 1) + rng.normal(0, 0.05))) for j in range(m)]
            p = {"profile": prof, "segments": int(rng.integers(3, 25)), "closed": closed}
        elif k == "extrude":
            m = int(rng.integers(3, 12))
            ang = np.sort(rng.uniform(0, 2 * np.pi, m)) * (1 if rng.random() < 0.5 else -1)
            rad = rng.uniform(0.2, 1.0, m)
            pts = [(float(r * math.cos(a)), float(r * math.sin(a))) for r, a in zip(rad, ang)]
            if rng.random() < 0.3:
                rng.shuffle(pts)  # often self-intersecting
            p = {"outline": pts, "depth": float(rng.uniform(0.01, 0.5))}
        elif k == "sphere":
            p = {"radius": float(rng.uniform(0.01, 1)), "segments": int(rng.integers(3, 30)), "rings": int(rng.integers(2, 16))}
        elif k == "capsule":
            p = {"radius": float(rng.uniform(0.01, 1)), "height": float(rng.choice([0.0, rng.uniform(0, 1)])), "segments": int(rng.integers(3, 30))}
        else:
            p = {"radius": float(rng.uniform(0.01, 1)), "height": float(rng.uniform(0.01, 1)), "segments": int(rng.integers(3, 30))}
        out.append(NodeIR(id=f"n{i}", kind=k, params=p))
    return out


def test_check_node_agrees_with_the_compiler() -> None:
    ok = bad = 0
    for node in _random_nodes(7, 900):
        try:
            compile_ir.check_node(node)
            checked = None
        except ValueError as e:
            checked = str(e)
        try:
            pts, tris = primitive_mesh(node)
            built = None
        except ValueError as e:
            built = str(e)
        assert checked == built, (node, checked, built)
        if built is None:
            ok += 1
            assert len(tris) == triangle_count(node), node
        else:
            bad += 1
            assert built.startswith(f"{node.id}: ")
    assert ok > 300 and bad > 150  # both sides exercised


def test_check_node_is_cheap() -> None:
    import time

    nodes = [n for n in _random_nodes(3, 360) if n.kind in ("box", "lathe", "extrude", "cylinder")]
    t0 = time.perf_counter()
    for n in nodes:
        try:
            compile_ir.check_node(n)
        except ValueError:
            pass
    per = (time.perf_counter() - t0) / len(nodes)
    assert per < 2e-3, f"{per * 1e3:.2f} ms per node"


# --------------------------------------------------------------------------- mirrored parts (a negative scale)

MIRROR_PARTS = [
    box(),
    box(bevel=0.03, bevel_segments=2, inset_faces=["+x", "+z"], inset_margin=0.04, inset_depth=0.02),
    NodeIR(id="b", kind="extrude", params={"outline": U, "depth": 0.03}),
    NodeIR(id="b", kind="lathe", params={"profile": [(0, 0.5), (0.2, 0.5), (0.3, 0.2), (0.1, -0.5)], "segments": 9}),
]


def _signed_volume(m: MeshData) -> float:
    p = np.asarray(m.points, np.float64)[np.asarray(m.triangles)]
    return float(np.einsum("ij,ij->i", p[:, 0], np.cross(p[:, 1], p[:, 2])).sum() / 6)


@pytest.mark.parametrize("part", MIRROR_PARTS, ids=lambda n: n.kind + str(len(n.params)))
@pytest.mark.parametrize("how", ["scale_x", "scale_yz", "parent"])
def test_a_mirrored_part_still_faces_out(tmp_path, part: NodeIR, how: str) -> None:
    """A part under a transform with a negative determinant is baked with its triangles reversed, so it faces out and
    passes Tier-1 like its unmirrored twin; scale [1, -1, -1] (determinant +1) is a rotation and is not reversed. The
    unwrap written back onto the mirrored prim keeps the local winding (no flip on the next read)."""
    from antigravity3d.kernel.usd_stage import StageManager

    lift = Transform(translate=(0, 0, 0.6))
    if how == "parent":
        nodes = [NodeIR(id="g", kind="group", transform=Transform(translate=(0, 0, 0.6), scale=(1, -1, 1))),
                 part.model_copy(update={"id": "m", "parent": "g"})]
    else:
        nodes = [part.model_copy(update={"id": "m", "transform": lift.model_copy(
            update={"scale": (-1, 1, 1) if how == "scale_x" else (1, -1, -1)})})]
    twin = SceneIR(asset_name="T", nodes=[part.model_copy(update={"id": "m", "transform": lift})])
    budget = EngineBudget("godot_4", max_tris=6000)
    reads = []
    for k, scene in enumerate((twin, SceneIR(asset_name="T", nodes=nodes))):
        sm = StageManager.create(tmp_path / f"{k}.usda", "T")
        sm.apply_ir(scene)
        (m,) = sm.extract_meshes()
        reads.append(m)
        uvs = fast_ops.unwrap_scene([m], budget)
        sm.author_uvs(uvs)
        (again,) = sm.extract_meshes()
        assert _signed_volume(again) == pytest.approx(_signed_volume(m), rel=1e-6)
    plain, mirrored = reads
    assert _signed_volume(mirrored) == pytest.approx(_signed_volume(plain), rel=1e-5) and _signed_volume(plain) > 0
    rep_plain, rep_mirror = (fast_ops.tier1_qa([m], budget) for m in reads)
    assert [i.code for i in rep_mirror.issues] == [i.code for i in rep_plain.issues]
    assert rep_mirror.ok


# --------------------------------------------------------------------------- two fixes in fast_ops the shapes exposed
# (both written with what commit 29660b8 already had, so they fail there and pass with the fix)


def _l_prism(a: float, t: float, h: float, at) -> tuple[np.ndarray, np.ndarray]:
    """An L-section prism by hand (outer corner at the origin, legs along +x and +y), moved to ``at``."""
    ring = [(0, 0), (a, 0), (a, t), (t, t), (t, a), (0, a)]
    pts = np.array([(x, y, z) for z in (0.0, h) for x, y in ring], np.float64) + at
    cap = [(0, 1, 2), (0, 2, 3), (0, 3, 4), (0, 4, 5)]  # counter-clockwise; the L is star-shaped from its outer corner
    tris = [(a_ + 6, b + 6, c + 6) for a_, b, c in cap] + [(a_, c, b) for a_, b, c in cap]
    for i in range(6):
        j = (i + 1) % 6
        tris += [(i, j, j + 6), (i, j + 6, i + 6)]
    tris = np.array(tris, np.int32)
    return pts, tris


def test_parts_in_flush_contact_are_not_interpenetrating() -> None:
    """An L-section guard whose inner faces lie on a box's faces, its inner corner on the box's edge: probes on the
    box's surface used to read as inside (the winding number is undefined on an edge), 31 % of them here. A guard
    pushed 1 mm into the box is still found."""
    bp, bt = compile_ir._box(0.8, 0.5, 0.45)
    body = MeshData("body", "", bp.astype(np.float64), bt)
    budget = EngineBudget("godot_4")
    for shift, found in ((0.0, False), (-0.001, True)):
        gp, gt = _l_prism(0.06, 0.006, 0.38, np.array([-0.406 - shift, -0.256 - shift, -0.19]))
        guard = MeshData("guard", "", gp, gt)
        rep = fast_ops.tier1_qa([body, guard], budget)
        assert any(i.code == "interpenetration" for i in rep.issues) == found, (shift, rep.one_line())


def test_a_merged_collision_hull_is_convex_as_shipped() -> None:
    """Hull of a turned knob and a ring (flat facets of 16 and 20 coplanar points): Qhull kept 85 points in float64,
    and the float32 copy the package ships failed the validator's convexity check; the hull is now taken of the
    float32 values until Qhull keeps all of them."""
    from antigravity3d.kernel.validator import _is_convex

    knob = compile_ir._lathe([(0.0, 0.06), (0.02, 0.06), (0.035, 0.045), (0.03, 0.025), (0.015, 0.012), (0.02, 0.0), (0.0, 0.0)], 16)[0]
    ring = compile_ir._lathe([(0.04, -0.008), (0.048, 0.0), (0.04, 0.008), (0.032, 0.0), (0.04, -0.008)], 20)[0]
    pts = np.vstack([knob.astype(np.float64) + [0.25, 0, 0.54], ring.astype(np.float64) + [-0.2, 0, 0.548]])
    p, t, v = fast_ops._hull(pts)
    assert _is_convex(np.asarray(p, np.float32).astype(np.float64), t)
    assert v == pytest.approx(trimesh.Trimesh(pts).convex_hull.volume, rel=1e-6)  # still the hull of the visual points
