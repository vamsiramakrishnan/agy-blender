"""The swept rod is available through response schemas, MCP arguments, patches and budget repair.

These tests use authored coordinates and local tool calls only. They do not make model calls or judge a chair.
"""

from __future__ import annotations

import json

import jsonschema
import numpy as np
import pytest

from antigravity3d import ir, ir_schema as S, ir_shortcuts as X
from antigravity3d.authoring_contract import AuthoringIntent, PROP_CRAFT_GUIDANCE
from antigravity3d.kernel.compile_ir import primitive_mesh, triangle_count
from antigravity3d.tactics.authoring import _shrink, count_tris, world_bounds


PATH = [[0.0, 0.0, 0.0], [0.15, 0.0, 0.2], [0.2, 0.1, 0.4]]


def node(**extra):
    return {"id": "rail", "kind": "sweep", "params": {"path": PATH, "radius": 0.02, "segments": 12},
            "transform": {"translate": [0, 0, 0]}, **extra}


def scene(*nodes):
    return {"asset_name": "BentRail", "materials": [], "nodes": list(nodes) or [node()]}


def test_response_and_tool_schemas_preserve_both_meanings_of_path():
    rod = node()
    ref = {"id": "reference", "kind": "mesh_ref", "params": {"path": "parts/bracket.glb"},
           "transform": {"translate": [0, 0, 0]}}
    for n in (rod, ref):
        doc = scene(n)
        jsonschema.validate(doc, S.scene_response_schema())
        jsonschema.validate(n, S.tool_node_schema())
        parsed = S.parse_scene(json.dumps(doc))
        assert parsed == S.parse_tool_scene("BentRail", [n], [])
        op = {"op": "upsert_node", "node": n}
        jsonschema.validate({"ops": [op]}, S.patch_response_schema())
        jsonschema.validate({"ops": [op]}, S.apply_scene_ir_parameters())
        assert S.parse_ops([op]).ops[0].node == parsed.nodes[0]


def test_path_patch_preserves_both_parameter_types_and_round_trips_without_losing_xyz():
    replacement = [[0, 0, 0], [0.12, 0.05, 0.3], [0.3, 0.1, 0.5]]
    source = S.parse_scene(json.dumps(scene()))
    op = {"op": "set_param", "node_id": "rail", "name": "path", "value": replacement}
    patch = S.parse_ops([op])
    jsonschema.validate({"ops": [op]}, S.patch_response_schema())
    jsonschema.validate({"ops": [op]}, S.apply_scene_ir_parameters())
    changed = ir.apply_patch(source, patch)
    assert changed.nodes[0].params["path"] == [tuple(p) for p in replacement]
    assert ir.SceneIR.model_validate_json(changed.model_dump_json()) == changed
    # The shared parameter name must still accept a file reference in the existing mesh_ref contract.
    ref_op = {**op, "node_id": "ref", "value": "parts/new.glb"}
    jsonschema.validate({"ops": [ref_op]}, S.patch_response_schema())
    assert S.parse_ops([ref_op]).ops[0].value == "parts/new.glb"


@pytest.mark.parametrize("params", [
    {"path": [[0, 0, 0]], "radius": 0.02},
    {"path": [[0, 0], [1, 1]], "radius": 0.02},
    {"path": PATH * 43, "radius": 0.02},
    {"path": "part.glb", "radius": 0.02},
    {"path": PATH, "radius": 0},
    {"path": PATH, "radius": 0.02, "segments": 65},
    {"path": PATH, "radius": 0.02, "closed": True},
])
def test_bad_sweep_arguments_name_the_node_kind_and_parameter(params):
    with pytest.raises(S.IRArgumentError, match=r"ops\[0\].*sweep.*params"):
        S.parse_ops([{"op": "upsert_node", "node": node(params=params)}])


@pytest.mark.parametrize("repeat", [{"count": 3, "offset": [0.5, 0, 0]}, {"count": 4, "around": "z"}])
def test_sweep_repeats_preserve_geometry_and_have_accounted_cost(repeat):
    value = S.parse_scene(json.dumps(scene(node(repeat=repeat))))
    original = primitive_mesh(value.nodes[0])
    assert len(value.nodes) == repeat["count"]
    assert count_tris(value) == repeat["count"] * 2 * 12 * len(PATH)
    for copy in value.nodes[1:]:
        assert copy.params == value.nodes[0].params
        assert all(np.array_equal(a, b) for a, b in zip(primitive_mesh(copy), original, strict=True))
        assert copy.transform != value.nodes[0].transform


def test_sweep_mirror_refuses_unsupported_reflection_including_in_a_parent():
    with pytest.raises(ValueError, match="rail: a sweep cannot be mirrored.*other path explicitly"):
        S.parse_scene(json.dumps(scene(node(mirror="x"))))
    parent = {"id": "handle", "kind": "group", "transform": {"translate": [1, 0, 0]}, "mirror": "x"}
    with pytest.raises(ValueError, match="rail: a sweep cannot be mirrored.*other path"):
        S.parse_scene(json.dumps(scene(parent, node(parent="handle"))))


def test_bounds_include_a_noncentred_sweep_after_parent_rotation_and_scale():
    parent = ir.NodeIR(id="g", kind="group", transform=ir.Transform(translate=(3, 2, 1), rotate_euler_deg=(30, 20, 60)))
    child = ir.NodeIR(id="rail", kind="sweep", params={"path": PATH, "radius": 0.02}, parent="g",
                      transform=ir.Transform(translate=(1, 2, 3), scale=(2, 0.5, 1.5)))
    value = ir.SceneIR(asset_name="Bounds", nodes=[parent, child])
    lo, hi = world_bounds(value)["rail"]
    points, _ = primitive_mesh(child)
    world = X._rot(parent.transform.rotate_euler_deg) @ np.diag(child.transform.scale)
    offset = X._rot(parent.transform.rotate_euler_deg) @ np.asarray(child.transform.translate) + parent.transform.translate
    points = points @ world.T + offset
    assert np.all(points >= lo - 1e-7) and np.all(points <= hi + 1e-7)


def test_budget_repair_reduces_radial_resolution_without_changing_path_or_other_parts():
    rail = ir.NodeIR(id="rail", kind="sweep", params={"path": PATH, "radius": 0.02, "segments": 64})
    other = ir.NodeIR(id="unchanged", kind="box", params={"size": [0.1, 0.1, 0.1]})
    source = ir.SceneIR(asset_name="Budget", nodes=[rail, other])
    patch = ir.IRPatch(ops=_shrink(source, 220, only={"rail"}))
    changed = ir.apply_patch(source, patch)
    assert count_tris(changed) <= int(220 * 0.9)
    assert changed.nodes[0].params["path"] == rail.params["path"]
    assert changed.nodes[0].params["radius"] == rail.params["radius"]
    assert changed.nodes[1] == other
    assert all(op.node_id == "rail" and op.name == "segments" for op in patch.ops)
    assert triangle_count(changed.nodes[0]) == len(primitive_mesh(changed.nodes[0])[1])


def test_planner_and_geometry_stages_can_express_sweep_without_changing_intent():
    from antigravity3d.harness import agents as A

    assert A.PartPlan(name="rail", primitive="sweep", approx_size_m=(0.3, 0.1, 0.4)).primitive == "sweep"
    for intent in AuthoringIntent:
        system = A._system("BLOCKOUT", authoring_intent=intent)
        assert S.SHAPE_SEMANTICS in system and "sweep path" in system
        assert (PROP_CRAFT_GUIDANCE in system) == (intent is AuthoringIntent.PRODUCTION_PROP)
    assert "sweep" in S.ir_rules_text()


async def test_native_edit_tool_authors_and_edits_a_bent_rod_with_tier1_checks(tmp_path):
    from test_harness_tools import make_session
    from antigravity3d.harness import tools as T

    env = make_session(tmp_path)
    path = [[0, 0, 0.02], [0.2, 0, 0.02], [0.35, 0, 0.15], [0.35, 0, 0.4]]
    rail = node(params={"path": path, "radius": 0.02, "segments": 12})
    result = await T.apply_scene_ir(json.dumps({"ops": [{"op": "upsert_node", "node": rail}]}), env.ctx)
    assert result.startswith("OK tx="), result
    assert env.state.last_tier1_ok
    original = env.sess().stage.base_ir().nodes[0]
    path[-1] = [0.4, 0, 0.4]
    result = await T.apply_scene_ir(json.dumps({"ops": [
        {"op": "set_param", "node_id": "rail", "name": "path", "value": path}]}), env.ctx)
    assert result.startswith("OK tx="), result
    assert env.state.last_tier1_ok
    changed = env.sess().stage.base_ir().nodes[0]
    assert changed.params["path"] == [tuple(p) for p in path]
    assert changed.params["radius"] == original.params["radius"]
    # set_param is a union by parameter name; compilation validates it against the existing node's kind.
    result = await T.apply_scene_ir(json.dumps({"ops": [
        {"op": "set_param", "node_id": "rail", "name": "path", "value": "part.glb"}]}), env.ctx)
    assert result.startswith("ERROR") and "rail" in result and "path" in result, result
    assert env.sess().stage.base_ir().nodes[0] == changed  # the invalid patch never replaces the valid rod


def test_short_schema_addresses_preserve_accepted_documents_and_field_names():
    # Reuse the existing acceptance corpus: reference renaming must not remove constraints to meet byte budgets.
    from test_ir_schema import scene_corpus, patch_corpus

    for kind, corpus in (("scene", scene_corpus), ("patch", patch_corpus)):
        before = S._share(S._merge_alternatives(S._inline_once(S._reduce(S.raw_schema(kind)))))
        after = S._short_refs(before)
        old, new = jsonschema.Draft202012Validator(before), jsonschema.Draft202012Validator(after)
        valid, invalid = corpus()
        for doc in [*valid, *invalid]:
            assert old.is_valid(doc) == new.is_valid(doc)
        assert len(json.dumps(after)) < len(json.dumps(before))
    # A field that happens to have the old definition name is still a field of that name.
    custom = {"$defs": {"Shape": {"type": "number"}}, "type": "object", "properties": {"Shape": {"$ref": "#/$defs/Shape"}}}
    compact = S._short_refs(custom)
    assert "Shape" in compact["properties"] and "Shape" in custom["$defs"]
    assert jsonschema.Draft202012Validator(compact).is_valid({"Shape": 3})
    assert not jsonschema.Draft202012Validator(compact).is_valid({"Shape": "wrong"})
