"""Model-facing IR schema: derived from ir.py, accepted by the API's keyword subset, converted back losslessly.

LIVE FINDINGS (2026-11-20, gemini-4-hydrogen, response_json_schema):
* accepted: $defs/$ref, anyOf, oneOf+discriminator, const, enum, prefixItems+minItems+maxItems, additionalProperties
  false, {"type": "null"} inside anyOf, pattern, maxLength, exclusiveMinimum, default (the raw pydantic schema was not
  rejected); the API schema here is still reduced to the documented subset (API_SCHEMA_KEYWORDS).
* refs are expanded server-side: the inlined schema billed exactly the same prompt tokens (6,045) as the $ref form.
  (Later, same request on gemini-4-neon: writing the 3-number tuple once under $defs and dropping descriptions and
  float spellings took the propose schema from 7,954 to 6,064 bytes and billed 1,377 fewer prompt tokens.)
* with ``nodes`` optional, a constrained answer contained materials and no nodes -> nodes/materials/transform/translate
  are required on the wire (WIRE_REQUIRED).
* (2026-11-22, gemini-4-hydrogen, round five's schema) arrays of 2-number arrays inside 2..32 / 3..32 lists, a list of
  enum strings and the shortcut fields are accepted; an inlined schema (11,020 bytes) bills the same prompt tokens as the
  $defs form (7,797 bytes): 9,554 each. With repeat as three flat fields one answer in three wrote count + offset +
  around, which the parse refuses; repeat is now a row or a ring object, each forbidding the other's key.
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
        # the wire node is NodeIR plus the mirror / repeat shortcuts, which scene_from_wire expands away
        # ... less the fields only the script hand-off writes (a part's run, round seven): no model is asked for them
        assert set(model.model_fields) == (set(ir.NodeIR.model_fields) - set(S.HANDOFF_ONLY)) | set(S.SHORTCUT_FIELDS), kind
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
    # A stored scene's colours are linear and a model reads and writes sRGB (tests/test_colour.py), so the wire form of a
    # stored scene is the one a model is shown (S.as_written), not its own dump. Its colours are shown to
    # S.SHOWN_DIGITS sRGB decimals, so they come back within 1e-4 (an 80th of an 8-bit step); everything else exactly.
    for scene in await template_scenes():
        back = S.parse_scene(S.written_json(scene))
        assert back.model_copy(update={"materials": scene.materials}) == scene
        for m, n in zip(scene.materials, back.materials, strict=True):
            for f in S.COLOUR_FIELDS:
                assert max(abs(a - b) for a, b in zip(getattr(m, f), getattr(n, f))) < 1e-4, (m.id, f)
            assert m.model_copy(update={f: getattr(n, f) for f in S.COLOUR_FIELDS}) == n


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
    # the same patch read as stored data, except the written colour, which is sRGB on the wire (tests/test_colour.py)
    stored = json.loads(text)
    stored["ops"][7]["material"]["base_color"] = list(S.linear_colour([0.5, 0.3, 0.1]))
    assert S.parse_patch(text) == ir.IRPatch.model_validate(stored)
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
    assert S.SHAPE_SEMANTICS in agents._system("BLOCKOUT") and S.SHAPE_SEMANTICS not in agents._system("LOOKDEV")
    assert S.ir_rules_text().splitlines()[3] not in agents._system("BLOCKOUT")  # the "scene: {...}" type line
    assert S.ir_rules_text().splitlines()[3].startswith("scene: {")


# --- the response schema carries no bytes that are neither a constraint nor guidance ---------------------------


def _old_api(node):
    """The response schema as it was sent before it was written compactly: every keyword kept, nothing shared."""
    if isinstance(node, list):
        return [_old_api(x) for x in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for key, value in node.items():
        if key == "const":
            out["enum"] = [value]
        elif key == "oneOf":
            out["anyOf"] = _old_api(value)
        elif key in ("properties", "$defs"):
            out[key] = {k: _old_api(v) for k, v in value.items()}
        elif key in S.API_SCHEMA_KEYWORDS and key != "title":
            out[key] = _old_api(value)
    if "exclusiveMinimum" in node and "minimum" not in out:
        out["minimum"] = node["exclusiveMinimum"]
    if "$ref" in out:
        out = {k: v for k, v in out.items() if k.startswith("$")}
    return out


def _old_scene(name=None):
    schema = _old_api(S.raw_schema("scene"))
    if name:
        schema["properties"]["asset_name"] = {"type": "string", "enum": [name]}
    return schema


def _normal(root, node):
    """Inline every $ref, drop what has no effect on acceptance (descriptions), spell tuples and numbers one way."""
    if isinstance(node, list):
        return [_normal(root, x) for x in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        return _normal(root, root["$defs"][node["$ref"].rsplit("/", 1)[-1]])
    out = {}
    for key, value in node.items():
        if key in ("$defs", "description"):
            continue
        if key in ("properties",):
            out[key] = {k: _normal(root, v) for k, v in value.items()}
        elif key in ("minimum", "maximum"):
            out[key] = float(value)
        else:
            out[key] = _normal(root, value)
    items = out.get("prefixItems")
    if items and out.get("minItems") == out.get("maxItems") == len(items) and all(i == items[0] for i in items):
        del out["prefixItems"]
        out["items"] = items[0]
    return out


def test_new_response_schemas_are_the_old_ones_with_only_inert_text_removed():
    # The compact schema also merges anyOf branches that accept the same documents (S._merge_alternatives: set_param
    # names whose API value schema is the same, cone and capsule nodes, whose params are the same once exclusiveMinimum
    # is written as minimum), so the old spelling is compared after the same merge. That the merge accepts exactly
    # what the branches did is test_merged_alternatives_accept_what_the_branches_accepted.
    old = S._merge_alternatives(_normal(_old_scene("Crate"), _old_scene("Crate")))
    assert old == _normal(S.scene_response_schema("Crate"), S.scene_response_schema("Crate"))
    old_patch = _old_api(S.raw_schema("patch"))
    assert S._merge_alternatives(_normal(old_patch, old_patch)) == _normal(S.patch_response_schema(), S.patch_response_schema())


def test_merged_alternatives_accept_what_the_branches_accepted():
    branch = lambda name, value: {"type": "object", "properties": {"name": {"type": "string", "enum": [name]}, "value": value},  # noqa: E731
                                  "required": ["name", "value"], "additionalProperties": False}
    num, cnt = {"type": "number", "minimum": 0}, {"type": "integer", "minimum": 3}
    schema = {"anyOf": [branch("radius", num), branch("bevel", num), branch("segments", cnt), branch("radius", num)]}
    merged = S._merge_alternatives(schema)
    assert len(merged["anyOf"]) == 2 and merged["anyOf"][0]["properties"]["name"]["enum"] == ["radius", "bevel"]
    docs = [{"name": n, "value": v} for n in ("radius", "bevel", "segments", "rings") for v in (-1, 0, 0.5, 3, 4.5, "a")]
    for doc in docs:
        assert _accepts(schema, doc) == _accepts(merged, doc), doc
    two = {"anyOf": [branch("a", num), {**branch("b", num), "required": ["name"]}]}  # differs in more than the enum
    assert len(S._merge_alternatives(two)["anyOf"]) == 2


NODE = {"id": "n", "kind": "box", "params": {"size": [1, 1, 1]}, "transform": {"translate": [0, 0, 0]}}
PARAMS = {"box": {"size": [1, 2, 3]}, "cylinder": {"radius": 0.5, "height": 1, "segments": 8}, "cone": {"radius": 1, "height": 2},
          "sphere": {"radius": 1, "segments": 12, "rings": 6}, "capsule": {"radius": 0.2, "height": 0}, "mesh_ref": {"path": "a.glb"},
          "group": {}}
MATERIAL_FIELDS = {
    "base_color": [0.1, 0.2, 0.3], "metallic": 1, "roughness": 0, "albedo_uri": "a.png", "orm_uri": "o.png", "normal_uri": "n.png",
    "normal_convention": "directx", "alpha_mode": "MASK", "alpha_cutoff": 0.5, "opacity": 1, "double_sided": True,
    "emissive_color": [1, 1, 1], "emissive_strength": 12.5, "emissive_uri": "e.png", "clearcoat": 1, "clearcoat_roughness": 0.3,
    "transmission": 0.9, "ior": 3, "sheen_color": [0.5, 0.5, 0.5], "sheen_roughness": 1, "specular": 0.4, "specular_color": [1, 0, 0],
}
BAD_FIELD_VALUES = {  # one wrong value per material field
    "base_color": [0.1, 0.2], "metallic": 1.5, "roughness": -0.1, "albedo_uri": 3, "orm_uri": [], "normal_uri": True,
    "normal_convention": "dx", "alpha_mode": "opaque", "alpha_cutoff": 2, "opacity": 1.01, "double_sided": "yes",
    "emissive_color": [1, 1, 1, 1], "emissive_strength": -1, "emissive_uri": 0, "clearcoat": 2, "clearcoat_roughness": -1,
    "transmission": 1.1, "ior": 0.9, "sheen_color": ["a", 0, 0], "sheen_roughness": 7, "specular": -1, "specular_color": None,
}


def scene_corpus():
    def scene(nodes=None, materials=None, **kw):
        return {"asset_name": "Crate", "nodes": nodes if nodes is not None else [NODE], "materials": materials if materials is not None else [], **kw}

    def node(**kw):
        return {**NODE, **kw}

    valid = [scene()]
    valid += [scene([node(kind=k, params=p)]) for k, p in PARAMS.items()]
    valid += [scene([{**NODE, "id": "a"}, node(id="b", parent="a", material_id="m", collider=False)], [{"id": "m"}])]
    valid += [scene([node(parent=None, material_id=None)]), scene([node(transform={"translate": [1, 2, 3], "rotate_euler_deg": [0, 0, 90], "scale": [1, 1, 1]})])]
    valid += [scene(materials=[{"id": "m"}]), scene(materials=[{"id": "m", **MATERIAL_FIELDS, "metallic": 0.5}]), scene(materials=[{"id": "m", "alpha_cutoff": None}])]
    valid += [scene(materials=[{"id": "m", k: v}]) for k, v in MATERIAL_FIELDS.items()]
    valid += [scene(materials=[{"id": "m", "metallic": b, "roughness": b, "opacity": b}]) for b in (0, 1, 0.0, 1.0)]
    valid += [scene([node(params={"size": [1e-9, 1e9, 3]})]), scene([node(kind="sphere", params={"radius": 1, "segments": 3, "rings": 2})])]
    valid += [scene(materials=[{"id": "m", "ior": 1}, {"id": "n", "emissive_strength": 0}])]
    invalid = [
        {}, {"asset_name": "Crate"}, scene(nodes=[]), {"asset_name": "Crate", "nodes": [NODE]}, {"asset_name": "Crate", "materials": []},
        scene(asset_name="Other"), scene(extra=1), scene(nodes="x"), scene(materials={}), scene(materials=[{}]),
        scene([{k: v for k, v in NODE.items() if k != "transform"}]), scene([{k: v for k, v in NODE.items() if k != "params"}]),
        scene([{k: v for k, v in NODE.items() if k != "id"}]), scene([{k: v for k, v in NODE.items() if k != "kind"}]),
        scene([node(kind="torus")]), scene([node(extra=1)]), scene([node(collider="box")]), scene([node(collider=None)]),
        scene([node(id=3)]), scene([node(parent=3)]), scene([node(material_id=[])]), scene([node(transform=None)]),
        scene([node(transform={})]), scene([node(transform={"translate": [0, 0]})]), scene([node(transform={"translate": [0, 0, 0, 0]})]),
        scene([node(transform={"translate": [0, 0, "a"]})]), scene([node(transform={"translate": [0, 0, 0], "rotate_euler_deg": [0, 0]})]),
        scene([node(transform={"translate": [0, 0, 0], "scale": [1, 1, 1, 1]})]), scene([node(transform={"translate": [0, 0, 0], "skew": 1})]),
        scene([node(params={})]), scene([node(params={"size": [1, 1]})]), scene([node(params={"size": [1, 1, 1, 1]})]),
        scene([node(params={"size": [1, -1, 1]})]), scene([node(params={"size": ["1", 1, 1]})]), scene([node(params={"size": [1, 1, 1], "segments": 8})]),
        scene([node(params={"size": [1, 1, 1], "radius": 1})]),  # a parameter belonging to another kind
        scene([node(kind="cylinder", params={"size": [1, 1, 1]})]), scene([node(kind="cylinder", params={"radius": 1})]),
        scene([node(kind="cylinder", params={"radius": 1, "height": 1, "rings": 4})]), scene([node(kind="cone", params={"radius": 1, "height": 1, "rings": 4})]),
        scene([node(kind="cylinder", params={"radius": 1, "height": 1, "segments": 2})]), scene([node(kind="cylinder", params={"radius": 1, "height": 1, "segments": 8.5})]),
        scene([node(kind="sphere", params={"radius": 1, "height": 1})]), scene([node(kind="sphere", params={"radius": 1, "rings": 1})]),
        scene([node(kind="capsule", params={"radius": 1})]), scene([node(kind="capsule", params={"radius": 1, "height": -1})]),
        scene([node(kind="mesh_ref", params={})]), scene([node(kind="mesh_ref", params={"path": 3})]), scene([node(kind="mesh_ref", params={"path": "a", "size": [1, 1, 1]})]),
        scene([node(kind="group", params={"radius": 1})]), scene([node(kind="box", params={"path": "a"})]),
    ]
    invalid += [scene(materials=[{"id": "m", k: v}]) for k, v in BAD_FIELD_VALUES.items()]
    invalid += [scene(materials=[{"id": "m", "gloss": 1}]), scene(materials=[{"id": 1}]), scene(materials=[{"id": "m", "metallic": "1"}])]
    return valid, invalid


def patch_corpus():
    n = {"id": "lid", "kind": "box", "params": {"size": [1, 1, 0.1]}, "transform": {"translate": [0, 0, 1]}}
    tr = {"translate": [0, 0, 2], "rotate_euler_deg": [0, 0, 90], "scale": [1, 1, 1]}
    valid_ops = [
        {"op": "upsert_node", "node": n}, {"op": "remove_node", "node_id": "x"}, {"op": "set_transform", "node_id": "x", "transform": tr},
        {"op": "set_param", "node_id": "x", "name": "size", "value": [2, 1, 0.1]}, {"op": "set_param", "node_id": "x", "name": "radius", "value": 0.05},
        {"op": "set_param", "node_id": "x", "name": "height", "value": 2}, {"op": "set_param", "node_id": "x", "name": "segments", "value": 12},
        {"op": "set_param", "node_id": "x", "name": "rings", "value": 3}, {"op": "set_param", "node_id": "x", "name": "path", "value": "p.glb"},
        {"op": "upsert_material", "material": {"id": "m", **MATERIAL_FIELDS, "metallic": 0.5}}, {"op": "assign_material", "node_id": "x", "material_id": "m"},
        {"op": "on_top_of", "node_id": "x", "target_id": "y", "gap_m": 0.01}, {"op": "flush_back", "node_id": "x", "target_id": "y"},
        {"node_id": "x", "name": "radius", "value": 1},  # op defaults to set_param
    ]
    bad_ops = [
        {}, {"op": "explode"}, {"op": "remove_node"}, {"op": "remove_node", "node_id": 3}, {"op": "remove_node", "node_id": "x", "extra": 1},
        {"op": "set_transform", "node_id": "x"}, {"op": "set_transform", "node_id": "x", "transform": {}},
        {"op": "set_transform", "node_id": "x", "transform": {"translate": [0, 0]}},
        {"op": "set_param", "node_id": "x", "name": "size", "value": [1, 1]}, {"op": "set_param", "node_id": "x", "name": "size", "value": [1, 1, -1]},
        {"op": "set_param", "node_id": "x", "name": "size", "value": 1}, {"op": "set_param", "node_id": "x", "name": "radius", "value": -1},
        {"op": "set_param", "node_id": "x", "name": "radius", "value": "big"}, {"op": "set_param", "node_id": "x", "name": "segments", "value": 2},
        {"op": "set_param", "node_id": "x", "name": "segments", "value": 4.5}, {"op": "set_param", "node_id": "x", "name": "rings", "value": 1},
        {"op": "set_param", "node_id": "x", "name": "path", "value": 3}, {"op": "set_param", "node_id": "x", "name": "color", "value": 1},
        {"op": "set_param", "node_id": "x", "name": "height", "value": [1, 2, 3]}, {"op": "set_param", "name": "radius", "value": 1},
        {"op": "upsert_node"}, {"op": "upsert_node", "node": {**n, "kind": "torus"}}, {"op": "upsert_node", "node": {**n, "params": {"radius": 1}}},
        {"op": "upsert_material", "material": {}}, {"op": "upsert_material", "material": {"id": "m", "opacity": 2}},
        {"op": "assign_material", "node_id": "x"}, {"op": "assign_material", "node_id": "x", "material_id": None},
        {"op": "on_top_of", "node_id": "x"}, {"op": "on_top_of", "node_id": "x", "target_id": "y", "gap_m": -1},
        {"op": "flush_back", "node_id": "x", "target_id": "y", "gap_m": "a"}, {"op": "flush_back", "node_id": "x", "target_id": "y", "z": 1},
    ]
    valid = [{"ops": [op]} for op in valid_ops] + [{"ops": valid_ops}]
    invalid = [{}, {"ops": []}, {"ops": {}}, {"ops": [], "extra": 1}, {"ops": [valid_ops[0]], "extra": 1}] + [{"ops": [op]} for op in bad_ops]
    return valid, invalid


def _accepts(schema, doc):
    return jsonschema.Draft202012Validator(schema).is_valid(doc)


def test_old_and_new_response_schemas_accept_and_reject_the_same_documents():
    valid, invalid = scene_corpus()
    new, old = S.scene_response_schema("Crate"), _old_scene("Crate")
    for doc in valid:
        assert _accepts(old, doc) and _accepts(new, doc), doc
        try:  # the strict parse, the real acceptance test, takes every valid document too ...
            S.parse_scene(json.dumps(doc))
        except ValidationError as err:  # ... except a material that is well typed but whose fields contradict each other
            assert "Value error, material " in str(err), err  # (a rule the schema cannot state; unchanged by how it is written)
    for doc in invalid:
        assert not _accepts(old, doc) and not _accepts(new, doc), doc
        if doc.get("asset_name") == "Other" or "yes" in json.dumps(doc) or '"1"' in json.dumps(doc):
            continue  # the schema pins the asset name and is strict about JSON types; the parse is lax about "1" and "yes"
        with pytest.raises(ValidationError):
            S.parse_scene(json.dumps(doc))
    valid, invalid = patch_corpus()
    new, old = S.patch_response_schema(), _old_api(S.raw_schema("patch"))
    for doc in valid:
        assert _accepts(old, doc) and _accepts(new, doc), doc
        S.parse_patch(json.dumps(doc))
    for doc in invalid:
        assert not _accepts(old, doc) and not _accepts(new, doc), doc
        with pytest.raises(ValidationError):
            S.parse_patch(json.dumps(doc))


def test_api_schemas_carry_no_prose_and_no_tuple_is_written_out_twice():
    for schema in (S.scene_response_schema("Crate"), S.patch_response_schema()):
        assert not [p for p, n in walk(schema) if "description" in n or "title" in n or "default" in n]
        tuples = [json.dumps(n, sort_keys=True) for p, n in walk(schema) if n.get("type") == "array" and n.get("minItems") == n.get("maxItems")]
        assert len(tuples) == len(set(tuples)), "a repeated tuple shape belongs in $defs"
        assert all(isinstance(n[k], int) for _, n in walk(schema) for k in ("minimum", "maximum") if k in n)
    # Pins moved deliberately (bytes, measured on this change). What a byte pin protects is what the model has to read,
    # not billed tokens: live (2026-11-22, gemini-4-hydrogen, the crate propose) the 7,797-byte $defs form and the same
    # schema inlined to 11,020 bytes billed exactly 9,554 prompt tokens each, so the service bills the expanded schema.
    # Base (7 kinds, no new parameters): scene 6,092, patch 9,317. The new parameters and kinds, after writing once-used
    # definitions in place (_inline_once), merging equivalent branches (_merge_alternatives) and exact sharing (_share):
    # scene 6,474 (inside the old pin), patch 10,102 (+785: set_param variants for the new parameters whose value has a
    # type of its own: bevel_segments, inset_faces, profile, closed, outline). The mirror / repeat shortcuts (two optional
    # fields on each of the nine node kinds; per-kind node typing is kept, see LIVE FINDINGS) bring them to 7,523 and
    # 11,162.
    assert len(json.dumps(S.scene_response_schema("WoodenCrateWithIron"), separators=(",", ":"))) < 7_700
    assert len(json.dumps(S.patch_response_schema(), separators=(",", ":"))) < 11_400


def test_guidance_dropped_from_descriptions_is_stated_once_in_the_rules_text():
    text = S.ir_rules_text()
    for fact in ("full extent", "capsule height is the cylinder part only", "base at -height/2", "local asset path",
                 "transform-only", "thin-surface", "opacity is the base colour's alpha", "back is +Y"):
        assert fact in text, fact
