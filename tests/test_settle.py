from __future__ import annotations

import time

import numpy as np
import pytest
import trimesh
from scipy.spatial.transform import Rotation

from antigravity3d.ir import IRPatch, NodeIR, SceneIR, Transform, apply_patch
from antigravity3d.kernel.settle import GROUND_ID, contact_report, settle
from antigravity3d.kernel.spatial import vertical_hits
from antigravity3d.kernel.types import MeshData


def to_mesh(nid: str, m: trimesh.Trimesh, at=(0, 0, 0)) -> MeshData:
    pts = np.asarray(m.vertices, float) + np.asarray(at, float)
    return MeshData(nid, f"/{nid}", pts, np.asarray(m.faces, np.int32))


def box(nid, size=(1, 1, 1), center=(0, 0, 0)):
    return to_mesh(nid, trimesh.creation.box(extents=size), center)


def zmin(m: MeshData) -> float:
    return float(m.points[:, 2].min())


def by_id(ms):
    return {m.node_id: m for m in ms}


def test_floating_box_lands_on_ground():
    r = settle([box("a", center=(0, 0, 5.3))], {"a"})
    n = r.nodes["a"]
    assert n.dz == pytest.approx(-4.8) and abs(n.min_gap) < 1e-3 and n.max_penetration < 1e-3
    assert zmin(r.apply([box("a", center=(0, 0, 5.3))])[0]) == pytest.approx(0.0, abs=1e-9)
    assert r.converged and r.ms >= 0


def test_sunk_box_is_lifted():
    ms = [box("a", center=(0, 0, 0.2))]  # sunk 0.3 into the ground
    r = settle(ms, {"a"})
    assert r.nodes["a"].dz == pytest.approx(0.3)
    assert zmin(r.apply(ms)[0]) == pytest.approx(0.0, abs=1e-9)


def test_custom_ground_height():
    ms = [box("a", center=(0, 0, 3))]
    assert zmin(settle(ms, {"a"}, ground_z=1.25).apply(ms)[0]) == pytest.approx(1.25)


def test_box_on_box_static_and_static_never_moves():
    table = box("table", (4, 4, 1), (0, 0, 0.5))
    cup = box("cup", (0.5, 0.5, 0.5), (1.0, 0.5, 4.0))
    ms = [table, cup]
    r = settle(ms, {"cup"})
    assert set(r.nodes) == {"cup"}
    out = by_id(r.apply(ms))
    assert zmin(out["cup"]) == pytest.approx(1.0)
    assert out["table"] is table and np.array_equal(out["table"].points, table.points)


def test_box_on_sphere_top():
    sph = to_mesh("ball", trimesh.creation.uv_sphere(radius=0.5, count=[24, 24]), (0, 0, 0.5))
    ms = [sph, box("b", (0.2, 0.2, 0.2), (0, 0, 3.0))]
    top = vertical_hits(sph.points[sph.triangles], np.array([[0.0, 0.0]]))[0]
    expect = np.nanmax(top)
    r = settle(ms, {"b"})
    assert zmin(r.apply(ms)[1]) == pytest.approx(expect, abs=1e-9)
    assert abs(r.nodes["b"].min_gap) < 1e-3


def test_box_on_sphere_offset_footprint_no_penetration():
    """Footprint not covering the pole: dense brute force bounds the true resting height."""
    sph = to_mesh("ball", trimesh.creation.uv_sphere(radius=0.5, count=[24, 24]), (0, 0, 0.5))
    ms = [sph, box("b", (0.2, 0.2, 0.2), (0.3, 0.1, 3.0))]
    r = settle(ms, {"b"})
    placed = zmin(r.apply(ms)[1])
    g = np.linspace(0.2, 0.4, 41)
    gx, gy = np.meshgrid(g, np.linspace(0.0, 0.2, 41))
    h = np.nanmax(vertical_hits(sph.points[sph.triangles], np.c_[gx.ravel(), gy.ravel()]), axis=1)
    truth = np.nanmax(h)
    assert placed >= truth - 1e-6  # never penetrates
    assert placed - truth < 2e-3  # and is within the sampling tolerance of exact contact


def test_box_on_cylinder_top():
    cyl = to_mesh("cyl", trimesh.creation.cylinder(radius=0.5, height=2.0, sections=32), (0, 0, 1.0))
    ms = [cyl, box("b", (0.3, 0.3, 0.3), (0.1, 0.0, 6.0))]
    r = settle(ms, {"b"})
    assert zmin(r.apply(ms)[1]) == pytest.approx(2.0, abs=1e-9)


def test_stack_of_three_arbitrary_order():
    ms = [
        box("top", center=(0.1, 0, 9.5)),
        box("bottom", center=(0, 0, 2.5)),
        box("mid", center=(0.05, 0.05, 6.5)),
    ]
    r = settle(ms, {"top", "mid", "bottom"})
    out = by_id(r.apply(ms))
    assert zmin(out["bottom"]) == pytest.approx(0.0, abs=1e-9)
    assert zmin(out["mid"]) == pytest.approx(1.0, abs=1e-9)
    assert zmin(out["top"]) == pytest.approx(2.0, abs=1e-9)
    assert not contact_report(list(out.values()), ground_z=0.0)


def test_box_on_20_degree_ramp():
    h = 4 * np.tan(np.radians(20))
    pts = [(0, -2, 0), (4, -2, 0), (4, -2, h), (0, 2, 0), (4, 2, 0), (4, 2, h)]
    ramp = to_mesh("ramp", trimesh.convex.convex_hull(np.array(pts, float)))
    ms = [ramp, box("b", (1, 1, 1), (2.0, 0.0, 4.0))]
    r = settle(ms, {"b"})
    placed = r.apply(ms)[1]
    # resting on the uphill bottom edge (x = 2.5): z = 2.5*tan(20deg)
    assert zmin(placed) == pytest.approx(2.5 * np.tan(np.radians(20)), abs=1e-9)
    # no penetration: ramp surface under the whole footprint is at or below the box bottom
    g = np.linspace(1.5, 2.5, 21)
    gx, gy = np.meshgrid(g, np.linspace(-0.5, 0.5, 21))
    surf = np.nanmax(vertical_hits(ramp.points[ramp.triangles], np.c_[gx.ravel(), gy.ravel()]), axis=1)
    assert np.nanmax(surf) <= zmin(placed) + 1e-9
    assert r.nodes["b"].max_penetration < 1e-3
    assert not [c for c in contact_report([ramp, placed]) if c.kind == "penetration"]


def test_movable_between_ceiling_and_floor_ignores_ceiling():
    ceiling = box("ceil", (4, 4, 0.2), (0, 0, 3.1))
    ms = [ceiling, box("a", center=(0, 0, 1.5))]
    r = settle(ms, {"a"})
    assert zmin(r.apply(ms)[1]) == pytest.approx(0.0, abs=1e-9)


def test_determinism_bit_identical():
    rng = np.random.default_rng(3)
    ms = [box(f"p{i}", tuple(rng.random(3) * 0.6 + 0.2), tuple(rng.random(3) * [3, 3, 4])) for i in range(12)]
    mov = {m.node_id for m in ms[:9]}
    a, b = settle(ms, mov), settle(list(reversed(ms)), mov)
    for k in mov:
        assert a.nodes[k].dz.hex() == b.nodes[k].dz.hex()
        assert a.nodes[k].min_gap.hex() == b.nodes[k].min_gap.hex()
    assert a.iterations == b.iterations


def test_timing_ten_props_500_tris():
    props = []
    rng = np.random.default_rng(1)
    for i in range(10):
        m = trimesh.creation.uv_sphere(radius=0.3, count=[11, 11])  # 480 tris
        props.append(to_mesh(f"p{i}", m, (rng.random() * 6, rng.random() * 6, 0.3 + rng.random() * 3)))
    floor = box("floor", (20, 20, 0.2), (3, 3, -0.1))
    ms = [floor, *props]
    assert 400 < len(props[0].triangles) < 600
    settle(ms, {m.node_id for m in props})  # warm-up
    best = min(
        (settle(ms, {m.node_id for m in props}).ms for _ in range(5))
    )
    print(f"settle 10 props x ~{len(props[0].triangles)} tris: {best:.1f} ms")
    assert best < 200


def test_unknown_movable():
    with pytest.raises(KeyError):
        settle([box("a")], {"nope"})


def test_contact_report():
    ms = [
        box("base", (2, 2, 1), (0, 0, 0.5)),
        box("floaty", (1, 1, 1), (0, 0, 1.5 + 0.02)),  # 2 cm gap above base
        box("sunk", (0.5, 0.5, 0.5), (5, 0, 0.25 - 0.05)),  # 5 cm below ground
    ]
    rep = contact_report(ms, ground_z=0.0)
    kinds = {(c.a, c.b): (c.kind, c.gap) for c in rep}
    assert kinds[("floaty", "base")][0] == "floating" and kinds[("floaty", "base")][1] == pytest.approx(0.02)
    assert kinds[("sunk", GROUND_ID)][0] == "penetration" and kinds[("sunk", GROUND_ID)][1] == pytest.approx(-0.05)
    assert ("base", GROUND_ID) not in kinds  # resting contact is not reported
    ms2 = [box("base", (2, 2, 1), (0, 0, 0.5)), box("in", (0.5, 0.5, 1), (0.2, 0, 1.2))]
    (c,) = contact_report(ms2)
    assert c.kind == "penetration" and c.gap == pytest.approx(-0.3)


# --- IR round trip ------------------------------------------------------------


def world(scene: SceneIR, nid: str) -> np.ndarray:
    """Independent reference: 4x4 world matrix, local = T . R(xyz extrinsic) . S."""
    nodes = {n.id: n for n in scene.nodes}
    chain, cur = [], nodes[nid]
    while cur is not None:
        chain.append(cur)
        cur = nodes[cur.parent] if cur.parent else None
    w = np.eye(4)
    for n in reversed(chain):
        loc = np.eye(4)
        loc[:3, :3] = Rotation.from_euler("xyz", n.transform.rotate_euler_deg, degrees=True).as_matrix() @ np.diag(n.transform.scale)
        loc[:3, 3] = n.transform.translate
        w = w @ loc
    return w


def test_to_ir_ops_round_trip_with_rotated_scaled_parents():
    scene = SceneIR(
        asset_name="s",
        nodes=[
            NodeIR(id="root", kind="group", transform=Transform(translate=(1, -2, 0.5), rotate_euler_deg=(15, -25, 40), scale=(2.0, 0.5, 3.0))),
            NodeIR(id="mid", kind="group", parent="root", transform=Transform(translate=(0.3, 0.2, 0.1), rotate_euler_deg=(0, 70, 10), scale=(1.0, 2.0, 0.7))),
            NodeIR(id="leaf", kind="box", params={"size": (1.0, 1.0, 1.0)}, parent="mid", transform=Transform(translate=(0.5, 0.1, 0.9), rotate_euler_deg=(5, 6, 7), scale=(1.5, 1.0, 0.8))),
            NodeIR(id="free", kind="box", params={"size": (1.0, 1.0, 1.0)}, transform=Transform(translate=(4, 4, 2))),
            NodeIR(id="other", kind="box", params={"size": (1.0, 1.0, 1.0)}, parent="root"),
        ],
    )
    res = settle([box("leaf"), box("free")], set())  # empty, only for constructing
    from antigravity3d.kernel.settle import NodeSettle

    res.nodes.update({
        "leaf": NodeSettle("leaf", -0.8125, 0.0, 0.0, 1),
        "free": NodeSettle("free", -2.0, 0.0, 0.0, 1),
    })
    ops = res.to_ir_ops(scene)
    assert {o.node_id for o in ops} == {"leaf", "free"}
    new = apply_patch(scene, IRPatch(ops=ops))
    for nid, dz in (("leaf", -0.8125), ("free", -2.0), ("other", 0.0), ("mid", 0.0)):
        w0, w1 = world(scene, nid), world(new, nid)
        np.testing.assert_allclose(w1[:3, :3], w0[:3, :3], atol=1e-12)  # orientation/scale untouched
        np.testing.assert_allclose(w1[:3, 3] - w0[:3, 3], [0, 0, dz], atol=1e-12)
    leaf_new = {n.id: n for n in new.nodes}["leaf"].transform
    assert leaf_new.scale == (1.5, 1.0, 0.8) and leaf_new.rotate_euler_deg == (5, 6, 7)


def test_to_ir_ops_errors():
    from antigravity3d.kernel.settle import NodeSettle, SettleResult

    scene = SceneIR(asset_name="s", nodes=[
        NodeIR(id="g", kind="group"),
        NodeIR(id="c", kind="box", parent="g"),
        NodeIR(id="flat", kind="group", transform=Transform(scale=(1, 1, 0.0))),
        NodeIR(id="under", kind="box", parent="flat"),
    ])
    ns = lambda i: NodeSettle(i, -1.0, 0.0, 0.0, 1)  # noqa: E731
    with pytest.raises(KeyError):
        SettleResult({"zz": ns("zz")}, 1, 0.0).to_ir_ops(scene)
    with pytest.raises(ValueError, match="ancestor"):
        SettleResult({"g": ns("g"), "c": ns("c")}, 1, 0.0).to_ir_ops(scene)
    with pytest.raises(ValueError, match="singular"):
        SettleResult({"under": ns("under")}, 1, 0.0).to_ir_ops(scene)


def test_settle_end_to_end_into_ir():
    """settle -> ops -> IR: the node's world position matches the settled mesh."""
    scene = SceneIR(asset_name="s", nodes=[
        NodeIR(id="room", kind="group", transform=Transform(translate=(0, 0, 0), rotate_euler_deg=(0, 0, 90), scale=(2, 2, 1))),
        NodeIR(id="crate", kind="box", params={"size": (1.0, 1.0, 1.0)}, parent="room", transform=Transform(translate=(1, 0, 3.0))),
    ])
    w = world(scene, "crate")
    m = box("crate", (2.0, 2.0, 1.0), tuple(w[:3, 3]))  # world-space footprint of the scaled crate
    r = settle([m], {"crate"})
    new = apply_patch(scene, IRPatch(ops=r.to_ir_ops(scene)))
    assert world(new, "crate")[2, 3] == pytest.approx(0.5)  # half-height on the ground
    _ = time
