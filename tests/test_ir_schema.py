"""Model-facing IR schema: derived from ir.py, accepted by the API's keyword subset, converted back losslessly.

LIVE FINDINGS (2026-11-20, gemini-4-hydrogen, response_json_schema):
* accepted: $defs/$ref, anyOf, oneOf+discriminator, const, enum, prefixItems+minItems+maxItems, additionalProperties
  false, {"type": "null"} inside anyOf, pattern, maxLength, exclusiveMinimum, default (the raw pydantic schema was not
  rejected); the API schema here is still reduced to the documented subset (API_SCHEMA_KEYWORDS).
* refs are expanded server-side: the inlined schema billed exactly the same prompt tokens (6,045) as the $ref form.
* with ``nodes`` optional, a constrained answer contained materials and no nodes -> nodes/materials/transform/translate
  are required on the wire (WIRE_REQUIRED).
* per-kind node typing costs prompt tokens: 6,045 prompt tokens vs 2,866 for a single node shape with an untied
  ``params`` anyOf (same rules text); kept because it makes kind/params mismatches unrepresentable.
"""

from __future__ import annotations

import json
import re
from typing import get_args

import jsonschema
import pytest
from pydantic import ValidationError

from antigravity3d import ir, ir_schema as S
from antigravity3d.kernel.compile_ir import primitive_mesh
from antigravity3d.state import EngineBudget
from antigravity3d.tactics.authoring import TemplateAuthor

PROMPTS = ("wooden crate with iron brackets", "rusty metal barrel", "oak dining table", "iron street lamp post",
           "wooden storage shelf")


async def template_scenes() -> list[ir.SceneIR]:
    out = []
    for p in PROMPTS:
        out += await TemplateAuthor(seed=1).propose(p, budget=EngineBudget("unreal_5"), n=2, context={"asset_name": "Asset"})
    return out


def walk(node, path=""):
    if isinstance(node, dict):
        yield path, node
        for k, v in node.items():
            yield from walk(v, f"{path}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk(v, f"{path}/{i}")


# --- derivation from ir.py -------------------------------------------------------------------------------------


def test_every_kind_and_node_field_is_on_the_wire():
    kinds = get_args(ir.NodeIR.model_fields["kind"].annotation)
    assert set(S.NODE_MODELS) == set(kinds) == set(ir.NODE_PARAMS)
    for kind, model in S.NODE_MODELS.items():
        assert set(model.model_fields) == set(ir.NodeIR.model_fields), kind
        assert model.model_fields["params"].annotation is ir.NODE_PARAMS[kind]
        assert model.model_fields["collider"].annotation is bool


def test_every_op_and_op_field_is_on_the_wire():
    members = get_args(get_args(ir.IROp)[0])
    wire_ops = {m.model_fields["op"].annotation for m in S.OP_MODELS}
    for cls in members:
        assert cls.model_fields["op"].annotation in wire_ops, cls
    op_names = {v for cls in members for v in get_args(cls.model_fields["op"].annotation)}
    assert {"on_top_of", "flush_back", "set_param", "upsert_node", "remove_node", "set_transform", "upsert_material",
            "assign_material"} <= op_names
    set_params = [m for m in S.OP_MODELS if m.__name__.startswith("SetParam")]
    assert {get_args(m.model_fields["name"].annotation)[0] for m in set_params} == {
        p for model in ir.NODE_PARAMS.values() for p in model.model_fields}
    for m in S.OP_MODELS:
        if not m.__name__.startswith("SetParam"):
            src = getattr(ir, m.__name__.removeprefix("Wire"))
            assert set(m.model_fields) == set(src.model_fields)


def test_kernel_defaults_match_typed_param_defaults():
    for kind in ("cylinder", "cone", "sphere", "capsule"):
        params = ir.NODE_PARAMS[kind]
        given = {k: 0.5 for k, f in params.model_fields.items() if f.is_required()}
        bare = ir.NodeIR(id="n", kind=kind, params=given)
        full = ir.NodeIR(id="n", kind=kind, params=params(**given).model_dump())
        assert [a.shape for a in primitive_mesh(bare)] == [a.shape for a in primitive_mesh(full)], kind


# --- conversion ------------------------------------------------------------------------------------------------


async def test_template_scenes_round_trip_through_the_wire():
    for scene in await template_scenes():
        assert S.parse_scene(scene.model_dump_json()) == scene


def test_every_op_round_trips_to_irpatch():
    node = {"id": "lid", "kind": "box", "params": {"size": [1, 1, 0.1]}, "transform": {"translate": [0, 0, 1]}}
    ops = [
        {"op": "upsert_node", "node": node},
        {"op": "remove_node", "node_id": "old"},
        {"op": "set_transform", "node_id": "lid", "transform": {"translate": [0, 0, 2], "rotate_euler_deg": [0, 0, 90]}},
        {"op": "set_param", "node_id": "lid", "name": "size", "value": [2, 1, 0.1]},
        {"op": "set_param", "node_id": "leg", "name": "radius", "value": 0.05},
        {"op": "set_param", "node_id": "leg", "name": "segments", "value": 12},
        {"op": "set_param", "node_id": "ref", "name": "path", "value": "parts/handle.glb"},
        {"op": "upsert_material", "material": {"id": "oak", "base_color": [0.5, 0.3, 0.1], "roughness": 0.7}},
        {"op": "assign_material", "node_id": "lid", "material_id": "oak"},
        {"op": "on_top_of", "node_id": "lid", "target_id": "body", "gap_m": 0.01},
        {"op": "flush_back", "node_id": "lid", "target_id": "body"},
    ]
    text = json.dumps({"ops": ops})
    assert S.parse_patch(text) == ir.IRPatch.model_validate_json(text)
    jsonschema.validate(json.loads(text), S.patch_response_schema())


@pytest.mark.parametrize("bad", [
    {"collider": "box"},  # the live failure: a non-boolean collider
    {"collider": "none"},
    {"params": {"radius": 0.3, "height": 1}},  # box params on a cylinder's shape
    {"params": {"size": [1, 1]}},
    {"params": {"size": [1, 1, 0]}},
    {"params": {"size": [1, 1, 1], "segments": 8}},
    {"transform": None},
    {"transform": {"rotate_euler_deg": [0, 0, 0]}},  # translate is required on the wire
])
def test_strict_parse_rejects_what_the_ir_would_only_catch_late(bad):
    node = {"id": "body", "kind": "box", "params": {"size": [1, 1, 1]}, "transform": {"translate": [0, 0, 0.5]}, **bad}
    node = {k: v for k, v in node.items() if v is not None}
    doc = {"asset_name": "Crate", "nodes": [node], "materials": []}
    with pytest.raises(ValidationError):
        S.parse_scene(json.dumps(doc))


def test_scene_without_nodes_is_rejected():
    with pytest.raises(ValidationError):
        S.parse_scene('{"asset_name": "Crate", "nodes": [], "materials": []}')
    with pytest.raises(ValidationError):
        S.parse_scene('{"asset_name": "Crate", "materials": []}')


# --- the schema sent to the API --------------------------------------------------------------------------------


@pytest.mark.parametrize("schema", [S.scene_response_schema("Crate"), S.scene_response_schema(), S.patch_response_schema()])
def test_api_schema_uses_only_documented_keywords(schema):
    jsonschema.Draft202012Validator.check_schema(schema)
    for path, node in walk(schema):
        parent = path.rsplit("/", 1)[-1]
        if parent in ("properties", "$defs"):
            continue  # keys here are field / definition names
        assert set(node) <= S.API_SCHEMA_KEYWORDS, (path, set(node) - S.API_SCHEMA_KEYWORDS)
        if "$ref" in node:
            assert all(k.startswith("$") for k in node), path


async def test_api_schema_accepts_every_wire_valid_scene_and_rejects_the_live_failures():
    schema = S.scene_response_schema("Asset")
    for scene in await template_scenes():
        jsonschema.validate(json.loads(scene.model_dump_json()), schema)
    bad = {"asset_name": "Asset", "materials": [], "nodes": [
        {"id": "a", "kind": "box", "params": {"size": [1, 1, 1]}, "transform": {"translate": [0, 0, 0]}, "collider": "box"}]}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)
    bad["nodes"][0]["collider"] = True
    bad["asset_name"] = "Other"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


# --- rules text cannot drift from the schema -------------------------------------------------------------------


def _objects(schema):
    for _, node in walk(schema):
        if isinstance(node, dict) and node.get("type") == "object" and "properties" in node:
            yield node


@pytest.mark.parametrize("kind", ["scene", "patch"])
def test_rules_text_names_every_field_with_its_type(kind):
    text = S.ir_rules_text()
    raw = S.raw_schema(kind)
    for obj in _objects(raw):
        for name, typ in S.field_types(raw, obj).items():
            if name in ("op", "kind", "params"):
                continue  # rendered as the op / kind headings below
            assert f"{name}: {typ}" in text, f"{name}: {typ}"
    for kind_name, model in S.NODE_MODELS.items():
        assert f"{kind_name} params {{" in text
    for m in S.OP_MODELS:
        for op in get_args(m.model_fields["op"].annotation):
            assert f'"{op}"' in text


def test_rules_text_types_track_the_ir():
    text = S.ir_rules_text()
    assert "collider: ?boolean=true" in text
    assert "size: [number>0,number>0,number>0]" in text
    assert "segments: ?integer>=3=24" in text
    assert re.search(r'"on_top_of"\|"flush_back" \{node_id: id, target_id: id, gap_m: \?number>=0', text)


def test_stage_agents_use_the_generated_rules():
    """Stage agents get the types from their tool declarations (``*_parameters``) and only the prose a schema cannot
    carry in the prompt; see tests/test_harness_agents.py and tests/test_tool_schema.py."""
    agents = pytest.importorskip("antigravity3d.harness.agents")
    assert S.IR_SEMANTICS in agents._system("BLOCKOUT") and S.OP_SEMANTICS in agents._system("BLOCKOUT")
    assert S.ir_rules_text().splitlines()[2] not in agents._system("BLOCKOUT")  # the "scene: {...}" type line
