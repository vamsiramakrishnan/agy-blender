"""Mirror and repeat shortcuts: expanded exactly, deterministically and only on the wire (ir_shortcuts, ir_schema).

Exactness is checked on geometry, not on the transform numbers: the world-space vertices of a mirrored copy must be
the reflection of the original's (as a set), for every kind, under rotation and non-uniform scale, and with a positive
scale (no negative scale is ever written)."""

from __future__ import annotations

import itertools
import json
import random

import numpy as np
import pytest
from pydantic import ValidationError

from antigravity3d import ir, ir_schema as S, ir_shortcuts as X
from antigravity3d.kernel.compile_ir import _lathe, primitive_mesh
from antigravity3d.tactics.metered_author import repair_task

PROFILE = [[0.0, 0.3], [0.08, 0.3], [0.12, 0.2], [0.1, 0.05], [0.14, 0.0], [0.0, 0.0]]
OUTLINE = [[0.0, 0.0], [0.3, 0.0], [0.3, 0.05], [0.08, 0.05], [0.05, 0.25], [0.0, 0.25]]  # an L-ish bracket, concave
PARAMS = {
    "box": {"size": [0.4, 0.2, 0.1], "inset_faces": ["+x", "-y", "+z"], "inset_margin": 0.02, "inset_depth": 0.01},
    "cylinder": {"radius": 0.1, "height": 0.3, "segments": 7},
    "cone": {"radius": 0.1, "height": 0.3, "segments": 9},
    "sphere": {"radius": 0.1, "segments": 7, "rings": 5},
    "capsule": {"radius": 0.05, "height": 0.2, "segments": 9},
    "lathe": {"profile": PROFILE, "segments": 7},
    "extrude": {"outline": OUTLINE, "depth": 0.04},
}
FACE_NORMAL = {f"{s}{a}": np.eye(3)[i] * (1 if s == "+" else -1) for i, a in enumerate("xyz") for s in "+-"}


def local_points(node: ir.NodeIR) -> np.ndarray:
    p = node.params
    if node.kind == "lathe":
        return _lathe([tuple(q) for q in p["profile"]], int(p.get("segments", 16)))[0].astype(float)
    if node.kind == "extrude":
        xy = np.asarray(p["outline"], float)
        d = float(p["depth"]) / 2
        return np.vstack([np.c_[xy, np.full(len(xy), z)] for z in (-d, d)])
    return primitive_mesh(ir.NodeIR(id=node.id, kind=node.kind, params={k: v for k, v in p.items() if not k.startswith("inset")}))[0].astype(float)


def matrix(t: ir.Transform) -> np.ndarray:
    m = np.eye(4)
    m[:3, :3] = X._rot(t.rotate_euler_deg) @ np.diag(t.scale)
    m[:3, 3] = t.translate
    return m


def world(scene: ir.SceneIR, nid: str) -> np.ndarray:
    by = {n.id: n for n in scene.nodes}
    m, cur = np.eye(4), by[nid]
    while cur is not None:
        m = matrix(cur.transform) @ m
        cur = by.get(cur.parent) if cur.parent else None
    return m


def world_points(scene: ir.SceneIR, nid: str) -> np.ndarray:
    node = next(n for n in scene.nodes if n.id == nid)
    pts = local_points(node)
    return (world(scene, nid) @ np.c_[pts, np.ones(len(pts))].T).T[:, :3]


def same_set(a: np.ndarray, b: np.ndarray) -> bool:
    key = lambda p: sorted(map(tuple, np.round(p, 6) + 0.0))  # noqa: E731
    return len(a) == len(b) and key(a) == key(b)


def scene(*nodes: dict) -> ir.SceneIR:
    return S.parse_scene(json.dumps({"asset_name": "A", "materials": [], "nodes": list(nodes)}))


def node(nid: str, kind: str, translate=(0.6, 0.6, 0.6), rot=(0, 0, 0), scale=(1, 1, 1), **kw) -> dict:
    return {"id": nid, "kind": kind, "params": PARAMS.get(kind, {}),
            "transform": {"translate": list(translate), "rotate_euler_deg": list(rot), "scale": list(scale)}, **kw}


ROTATIONS = [(0, 0, 0), (0, 0, 90), (90, 0, 0), (0, 90, 0), (30, -45, 110), (-170, 12, 64)]


@pytest.mark.parametrize("kind", sorted(PARAMS))
@pytest.mark.parametrize("axis", X.AXES)
def test_a_mirrored_copy_is_the_exact_reflection_with_a_positive_scale(kind, axis):
    a = X.AXES.index(axis)
    for rot in ROTATIONS:
        s = scene(node("p", kind, rot=rot, scale=(1.0, 1.5, 0.75), mirror=axis))
        copy = next(n for n in s.nodes if n.id == f"p__m{axis}")
        assert all(c > 0 for c in copy.transform.scale)
        want = world_points(s, "p") * np.where(np.arange(3) == a, -1.0, 1.0)
        assert same_set(world_points(s, copy.id), want), (kind, axis, rot)
        if kind == "box":  # each recessed panel faces where the reflected original's does
            orig, m = next(n for n in s.nodes if n.id == "p"), world(s, copy.id)[:3, :3]
            reflect = np.diag(np.where(np.arange(3) == a, -1.0, 1.0))
            src = {f: reflect @ world(s, "p")[:3, :3] @ FACE_NORMAL[f] for f in orig.params["inset_faces"]}
            got = {f: m @ FACE_NORMAL[f] for f in copy.params["inset_faces"]}
            assert sorted(tuple(np.round(v / np.linalg.norm(v), 6) + 0.0) for v in src.values()) == \
                sorted(tuple(np.round(v / np.linalg.norm(v), 6) + 0.0) for v in got.values())
        if kind == "extrude":  # still counter-clockwise: the kernel's winding contract
            xy = np.asarray(copy.params["outline"])
            assert np.sum(xy[:, 0] * np.roll(xy[:, 1], -1) - np.roll(xy[:, 0], -1) * xy[:, 1]) > 0


def test_an_unrotated_part_mirrors_to_the_obvious_numbers():
    s = scene(node("guard", "box", translate=(0.4, 0.3, 0.05), mirror="x"),
              node("bracket", "extrude", translate=(0.2, 0, 0.5), mirror="x"))
    by = {n.id: n for n in s.nodes}
    assert by["guard__mx"].transform.translate == (-0.4, 0.3, 0.05)
    assert by["guard__mx"].transform.rotate_euler_deg == (0.0, 0.0, 0.0)
    assert by["guard__mx"].params["inset_faces"] == ["-x", "-y", "+z"]
    assert by["bracket__mx"].transform.rotate_euler_deg == (0.0, 0.0, 0.0)  # the outline is flipped instead
    assert by["bracket__mx"].params["outline"][0] == (0.0, 0.25)


def test_a_mirrored_parent_carries_its_subtree():
    handle = node("handle", "group", translate=(0.5, 0.1, 0.4), rot=(0, 0, 25), mirror="x")
    grip = node("grip", "cylinder", translate=(0, 0.06, 0), rot=(90, 0, 0), parent="handle")
    brk = node("brk", "extrude", translate=(0.02, -0.05, 0.01), rot=(0, 30, 0), parent="handle", repeat={"count": 2, "offset": [0, 0.12, 0]})
    s = scene(handle, grip, brk)
    ids = [n.id for n in s.nodes]
    assert ids == ["handle", "grip", "brk", "brk__r1", "handle__mx", "handle__mx__grip", "handle__mx__brk", "handle__mx__brk__r1"]
    by = {n.id: n for n in s.nodes}
    assert by["handle__mx__grip"].parent == "handle__mx" and by["handle__mx__brk__r1"].parent == "handle__mx"
    flip = np.array([-1.0, 1.0, 1.0])
    for nid in ("grip", "brk", "brk__r1"):
        assert same_set(world_points(s, f"handle__mx__{nid}"), world_points(s, nid) * flip), nid


def test_a_part_that_straddles_the_mirror_plane_is_refused_with_where_it_lies():
    pin = node("hinge_pin", "cylinder", translate=(0, 0.016, 0), parent="hinge", mirror="x")  # the live case
    with pytest.raises(ValueError, match=r"hinge_pin: mirror x needs the part on one side of its parent's x = 0 plane, "
                                         r"but it spans x -0.100 to 0.100"):
        scene(node("hinge", "group", translate=(0.26, 0.25, 0.37)), pin)
    with pytest.raises(ValueError, match="the asset's y = 0 plane"):  # a group's whole subtree counts
        scene(node("g", "group", translate=(0, 0.05, 0), mirror="y"), node("c", "box", translate=(0, 0, 0), parent="g"))
    touching = node("half", "box", translate=(0.2, 0, 0.5), mirror="x")  # size x 0.4: from 0 to 0.4, touches the plane
    assert len(scene(touching).nodes) == 2


def test_repeat_rows_and_rings():
    s = scene(node("slat", "box", translate=(0, -0.3, 0.5), repeat={"count": 4, "offset": [0, 0.2, 0]}),
              node("bolt", "cylinder", translate=(0.2, 0, 1), rot=(0, 0, 10), repeat={"count": 6, "around": "z"}))
    by = {n.id: n for n in s.nodes}
    assert [by[f"slat__r{k}"].transform.translate for k in (1, 2, 3)] == [(0, -0.1, 0.5), (0, 0.1, 0.5), (0, 0.3, 0.5)]
    for k in range(1, 6):
        b = by[f"bolt__r{k}"]
        ang = np.radians(60 * k)
        assert np.allclose(b.transform.translate, (0.2 * np.cos(ang), 0.2 * np.sin(ang), 1)), k
        assert np.allclose(world(s, b.id)[:3, :3], X._rot((0, 0, 60 * k)) @ X._rot((0, 0, 10)))
    assert "bolt__r6" not in by and len(s.nodes) == 2 + 3 + 5


def test_repeat_then_mirror_reflects_the_whole_row():
    s = scene(node("bolt", "cylinder", translate=(0.3, -0.2, 0.1), repeat={"count": 3, "offset": [0, 0.2, 0]}, mirror="x"))
    ids = [n.id for n in s.nodes]
    assert ids == ["bolt", "bolt__r1", "bolt__r2", "bolt__mx", "bolt__r1__mx", "bolt__r2__mx"]
    xs = sorted(round(n.transform.translate[0], 9) for n in s.nodes)
    assert xs == [-0.3] * 3 + [0.3] * 3


def test_expansion_is_deterministic_and_a_scene_without_shortcuts_is_untouched():
    rng = random.Random(3)
    nodes = [node(f"n{i}", rng.choice(sorted(PARAMS)), translate=(1 + rng.random(), 1 + rng.random(), 1 + rng.random()),
                  rot=tuple(rng.uniform(-180, 180) for _ in range(3)), mirror=rng.choice(X.AXES)) for i in range(8)]
    a, b = scene(*nodes), scene(*json.loads(json.dumps(nodes)))
    assert a.model_dump_json() == b.model_dump_json()
    plain = [{k: v for k, v in n.items() if k != "mirror"} for n in nodes]
    s = scene(*plain)
    assert [n.id for n in s.nodes] == [n["id"] for n in plain] and all("mirror" not in n.params for n in s.nodes)


@pytest.mark.parametrize("bad, why", [
    ({"kind": "mesh_ref", "params": {"path": "a.glb"}, "mirror": "x"}, "mesh_ref cannot be mirrored"),
    ({"repeat": {"count": 3}}, "repeat.RepeatRow.offset\n  Field required"),
    ({"repeat": {"count": 3, "offset": [0, 1, 0], "around": "z"}}, "Extra inputs are not permitted"),  # the live failure
    ({"repeat": {"offset": [0, 1, 0]}}, "count\n  Field required"),
    ({"repeat": {"count": 1, "offset": [0, 1, 0]}}, "greater than or equal to 2"),
    ({"repeat": {"count": 33, "around": "x"}}, "less than or equal to 32"),
    ({"mirror": "w"}, "Input should be 'x', 'y' or 'z'"),
    ({"mirror": None}, "Input should be 'x', 'y' or 'z'"),  # not nullable on the wire: the field is left out instead
])
def test_shortcut_errors_name_what_to_change(bad, why):
    with pytest.raises(ValidationError, match=why):
        scene({**node("p", "box"), **bad})


def test_expansion_errors_name_the_node():
    ref = node("ref", "mesh_ref", parent="g")
    ref["params"] = {"path": "a.glb"}
    with pytest.raises(ValueError, match="ref: a mesh_ref cannot be mirrored"):  # inside a mirrored group
        scene(node("g", "group", mirror="y"), ref)
    with pytest.raises(ValueError, match="p__mx: the id made for a mirror/repeat copy already exists"):
        scene(node("p", "box", mirror="x"), node("p__mx", "box"))
    with pytest.raises(ValueError, match="longer than 64"):
        scene(node("p" * 62, "box", mirror="x"))
    rows = [node("g", "group", repeat={"count": 32, "offset": [1, 0, 0]}),
            *[node(f"c{i}", "box", parent="g", repeat={"count": 2, "offset": [0, 1, 0]}) for i in range(8)]]
    with pytest.raises(ValueError, match="more than 512 nodes"):
        scene(*rows)
    nested = scene(*rows[:5])  # a repeated child inside a repeated parent: every id distinct, every copy where it belongs
    assert len(nested.nodes) == 32 * (1 + 4 * 2)  # 288: within the cap
    assert {"c0__r1", "g__r1__c0", "g__r1__c0__r1"} <= {n.id for n in nested.nodes}
    pos = {n.id: world(nested, n.id)[:3, 3] for n in nested.nodes}
    assert np.allclose(pos["g__r1__c0__r1"] - pos["c0"], [1, 1, 0])


def test_patch_shortcuts_expand_after_their_source_op_and_set_param_takes_the_new_values():
    ops = [{"op": "upsert_node", "node": node("lid", "box", translate=(0, 0, 1))},
           {"op": "upsert_node", "node": node("hinge", "cylinder", translate=(0.3, 0.2, 1), repeat={"count": 2, "offset": [-0.6, 0, 0]})},
           {"op": "on_top_of", "node_id": "hinge__r1", "target_id": "lid"},
           {"op": "set_param", "node_id": "lamp", "name": "profile", "value": PROFILE},
           {"op": "set_param", "node_id": "lamp", "name": "closed", "value": True},
           {"op": "set_param", "node_id": "lid", "name": "inset_faces", "value": ["+z"]}]
    patch = S.parse_patch(json.dumps({"ops": ops}))
    assert [(o.op, getattr(o, "node", None) and o.node.id) for o in patch.ops[:4]] == [
        ("upsert_node", "lid"), ("upsert_node", "hinge"), ("upsert_node", "hinge__r1"), ("on_top_of", None)]
    assert patch.ops[-2].value is True and patch.ops[-1].value == ["+z"] and patch.ops[-3].value[1] == (0.08, 0.3)
    assert patch.ops[2].node.transform.translate == (-0.3, 0.2, 1)
    assert S.parse_ops(ops) == patch  # the native session's tools read the same way


def test_the_repair_request_shows_the_expanded_scene():
    s = scene(node("handle", "box", mirror="y"))
    text = repair_task(s, reasons=["floating"], located={"floating": ["handle__my"]}, manifest="")
    assert '"handle__my"' in text and "mirror" not in text


def test_the_lean_tool_schema_offers_the_shortcuts_and_the_rules_explain_them():
    props = S.tool_node_schema()["properties"]
    assert {"mirror", "repeat"} <= set(props)
    assert not any("description" in v for v in props["params"]["properties"].values())  # in SHAPE_SEMANTICS instead
    for name in [p for m in ir.NODE_PARAMS.values() for p in m.model_fields] + list(S.SHORTCUT_FIELDS) + ["count", "offset", "around"]:
        assert name in S.SHAPE_SEMANTICS or name in ("size", "radius", "height", "segments", "rings", "path"), name


def test_every_kind_can_round_trip_every_axis_combination():
    for kind, axis, count in itertools.product(sorted(PARAMS), X.AXES, (2, 3)):
        s = scene(node("p", kind, repeat={"count": count, "around": axis}, mirror=axis))
        assert len(s.nodes) == 2 * count
        again = S.parse_scene(s.model_dump_json())  # an expanded scene is plain: it reads back unchanged
        assert again == s
