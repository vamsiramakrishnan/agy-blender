"""Model-facing Scene IR schema: one source of truth for what a generative model is asked to emit.

``ir.py``'s public models stay as they are (``NodeIR.params`` is a free-form dict; patch ops are a pydantic
discriminated union). A model cannot be constrained by "a dict of anything", so this module derives *wire* models
from them mechanically:

* one node model per ``NodeIR.kind`` whose ``params`` is the typed model in ``ir.NODE_PARAMS[kind]``;
  every other node field (``id``, ``transform``, ``parent``, ``material_id``, ``collider``) is copied from ``NodeIR``
  with its annotation, constraints and default;
* one op model per member of ``ir.IROp``, with ``NodeIR`` replaced by the wire node union and ``set_param`` split
  into one variant per parameter name so that ``value`` is typed;
* ``WireScene`` / ``WirePatch`` mirror ``SceneIR`` / ``SceneIRDiff``.

From those models come (1) the JSON schema sent to the API as ``response_json_schema`` (``scene_response_schema`` /
``patch_response_schema``, reduced to the feature set the Gemini API accepts, see ``API_SCHEMA_KEYWORDS``), (2) the
strict parser that turns a model response back into the existing ``SceneIR`` / ``IRPatch`` types, and (3) the
prose rules text (``ir_rules_text``) given to models, so the prose cannot drift from the schema.
"""

from __future__ import annotations

import copy
import functools
import json
from typing import Annotated, Any, Literal, Union, get_args

import annotated_types
from pydantic import BaseModel, ConfigDict, Field, create_model
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from antigravity3d import ir

# JSON-schema keywords the Gemini API documents for ``response_json_schema`` (google-genai GenerateContentConfig
# docstring) and that were accepted live on gemini-4-hydrogen / neon / carbon / argon (see tests/test_ir_schema.py,
# "LIVE FINDINGS"). Anything else pydantic emits is either rewritten (``const`` -> ``enum``, ``oneOf`` +
# ``discriminator`` -> ``anyOf``) or removed; removed constraints (``pattern``, ``exclusiveMinimum``, string lengths)
# are still enforced by the strict wire parse below.
API_SCHEMA_KEYWORDS = frozenset({
    "$id", "$defs", "$ref", "$anchor", "type", "format", "title", "description", "enum", "items", "prefixItems",
    "minItems", "maxItems", "minimum", "maximum", "anyOf", "oneOf", "properties", "additionalProperties", "required",
    "propertyOrdering",
})


class _Wire(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _camel(kind: str) -> str:
    return "".join(p.capitalize() for p in kind.split("_"))


NODE_KINDS: tuple[str, ...] = get_args(ir.NodeIR.model_fields["kind"].annotation)
if set(NODE_KINDS) != set(ir.NODE_PARAMS):  # a new kind must come with its typed params
    raise RuntimeError(f"ir.NODE_PARAMS {sorted(ir.NODE_PARAMS)} does not cover NodeIR.kind {sorted(NODE_KINDS)}")


# Fields a model must state explicitly although the IR has a default for them. Evidence (live, gemini-4-hydrogen):
# with ``nodes`` optional the constrained answer was a scene with materials and no nodes at all. A transform omitted
# (or a set_transform without ``translate``) silently places the part at the origin.
WIRE_REQUIRED: dict[type[BaseModel], frozenset[str]] = {
    ir.SceneIR: frozenset({"nodes", "materials"}),
    ir.NodeIR: frozenset({"transform"}),
    ir.Transform: frozenset({"translate"}),
}
WIRE_MIN_ITEMS: dict[tuple[type[BaseModel], str], int] = {(ir.SceneIR, "nodes"): 1}


def _field(source: type[BaseModel], name: str, info: FieldInfo, annotation: Any) -> tuple[Any, FieldInfo]:
    info = copy.copy(info)
    if name in WIRE_REQUIRED.get(source, ()):
        info.default, info.default_factory = PydanticUndefined, None
    if (source, name) in WIRE_MIN_ITEMS:
        info.metadata = [*info.metadata, annotated_types.MinLen(WIRE_MIN_ITEMS[(source, name)])]
    return annotation, info


def _derive(name: str, source: type[BaseModel], subst: dict[Any, Any], doc: str | None = None,
            override: dict[str, Any] | None = None) -> type[_Wire]:
    """Copy ``source``'s fields (annotation, constraints, default), substituting wire types for IR types."""
    fields: dict[str, Any] = {}
    for fname, info in source.model_fields.items():
        if override and fname in override:
            fields[fname] = override[fname]
            continue
        fields[fname] = _field(source, fname, info, subst.get(info.annotation, info.annotation))
    return create_model(name, __base__=_Wire, __doc__=doc if doc is not None else source.__doc__, **fields)


WireTransform = _derive("Transform", ir.Transform, {})


def _node_model(kind: str) -> type[_Wire]:
    params = ir.NODE_PARAMS[kind]
    needs = any(f.is_required() for f in params.model_fields.values())
    return _derive(f"{_camel(kind)}Node", ir.NodeIR, {ir.Transform: WireTransform}, doc=f"A {kind} node.", override={
        "kind": (Literal[kind], ...),
        "params": (params, ... if needs else Field(default_factory=params)),
    })


NODE_MODELS: dict[str, type[_Wire]] = {k: _node_model(k) for k in NODE_KINDS}
WireNode = Annotated[Union[tuple(NODE_MODELS.values())], Field(discriminator="kind")]  # type: ignore[valid-type]
_SUBST: dict[Any, Any] = {ir.NodeIR: WireNode, ir.Transform: WireTransform}


def _set_param_models() -> list[type[_Wire]]:
    """One ``set_param`` variant per parameter name, its ``value`` typed as that parameter is in ``NODE_PARAMS``."""
    by_name: dict[str, list[Any]] = {}
    for model in ir.NODE_PARAMS.values():
        for pname, info in model.model_fields.items():
            typed = Annotated[(info.annotation, *info.metadata)] if info.metadata else info.annotation
            if all(repr(typed) != repr(t) for t in by_name.setdefault(pname, [])):
                by_name[pname].append(typed)
    out = []
    for pname, types_ in by_name.items():
        value = types_[0] if len(types_) == 1 else Union[tuple(types_)]
        out.append(_derive(f"SetParam{_camel(pname)}", ir.SetParam, _SUBST, doc=f"set_param for '{pname}'.",
                           override={"name": (Literal[pname], ...), "value": (value, ...)}))
    return out


def _op_models() -> list[type[_Wire]]:
    out: list[type[_Wire]] = []
    for cls in get_args(get_args(ir.IROp)[0]):
        if cls is ir.SetParam:
            out.extend(_set_param_models())
        else:
            out.append(_derive(f"Wire{cls.__name__}", cls, _SUBST))
    return out


OP_MODELS: list[type[_Wire]] = _op_models()
WireOp = Union[tuple(OP_MODELS)]  # type: ignore[valid-type]
WireScene = _derive("WireScene", ir.SceneIR, {}, override={
    "nodes": _field(ir.SceneIR, "nodes", ir.SceneIR.model_fields["nodes"], list[WireNode])})
WirePatch = _derive("WirePatch", ir.SceneIRDiff, {}, override={
    "ops": _field(ir.SceneIRDiff, "ops", ir.SceneIRDiff.model_fields["ops"], list[WireOp])})


# --- conversion back into the public IR types ---------------------------------------------------------------


def scene_from_wire(wire: BaseModel) -> ir.SceneIR:
    return ir.SceneIR.model_validate(wire.model_dump(exclude_none=True))


def patch_from_wire(wire: BaseModel) -> ir.IRPatch:
    return ir.IRPatch.model_validate(wire.model_dump(exclude_none=True))


def parse_scene(text: str) -> ir.SceneIR:
    """Strictly typed parse of a model's SceneIR JSON (per-kind params), returned as ``ir.SceneIR``."""
    return scene_from_wire(WireScene.model_validate_json(text))


def parse_patch(text: str) -> ir.IRPatch:
    return patch_from_wire(WirePatch.model_validate_json(text))


# --- API schema ----------------------------------------------------------------------------------------------


def _api(node: Any) -> Any:
    """Reduce a pydantic JSON schema to ``API_SCHEMA_KEYWORDS`` (recursively)."""
    if isinstance(node, list):
        return [_api(x) for x in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "const":
            out["enum"] = [value]
        elif key == "oneOf":
            out["anyOf"] = _api(value)
        elif key in ("properties", "$defs"):
            out[key] = {k: _api(v) for k, v in value.items()}
        elif key in API_SCHEMA_KEYWORDS and key != "title":  # titles only cost prompt tokens
            out[key] = _api(value)
    if "exclusiveMinimum" in node and "minimum" not in out:  # closest documented bound; the wire parse keeps it strict
        out["minimum"] = node["exclusiveMinimum"]
    if "$ref" in out:  # the API allows only $-keywords next to $ref
        out = {k: v for k, v in out.items() if k.startswith("$")}
    return out


@functools.cache
def _raw_schema(kind: str) -> str:
    return json.dumps((WireScene if kind == "scene" else WirePatch).model_json_schema())


def raw_schema(kind: Literal["scene", "patch"]) -> dict[str, Any]:
    """Full pydantic JSON schema of the wire model (defaults, patterns and descriptions included)."""
    return json.loads(_raw_schema(kind))


def scene_response_schema(asset_name: str | None = None) -> dict[str, Any]:
    """``response_json_schema`` for a SceneIR answer; pins ``asset_name`` when given."""
    schema = _api(raw_schema("scene"))
    if asset_name is not None:
        schema["properties"]["asset_name"] = {"type": "string", "enum": [asset_name]}
    return schema


def patch_response_schema() -> dict[str, Any]:
    return _api(raw_schema("patch"))


# --- rules text, generated from the same schema ----------------------------------------------------------------


def _resolve(schema: dict[str, Any], node: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    ref = node.get("$ref")
    if ref:
        name = ref.rsplit("/", 1)[-1]
        return name, schema["$defs"][name]
    return None, node


def type_str(schema: dict[str, Any], node: dict[str, Any]) -> str:
    """Compact type of one schema node, e.g. ``boolean``, ``number>0``, ``[number,number,number]``, ``"a"|"b"``."""
    name, node = _resolve(schema, node)
    if name is not None and node.get("type") == "object":
        return _ref_label(name)
    for key in ("anyOf", "oneOf"):
        if key in node:
            alts = [type_str(schema, alt) for alt in node[key] if alt.get("type") != "null"]
            nullable = any(alt.get("type") == "null" for alt in node[key])
            return "|".join(dict.fromkeys(alts)) + ("|null" if nullable else "")
    if "const" in node:
        return json.dumps(node["const"])
    if "enum" in node:
        return "|".join(json.dumps(v) for v in node["enum"])
    t = node.get("type")
    if t == "array":
        if "prefixItems" in node:
            return "[" + ",".join(type_str(schema, x) for x in node["prefixItems"]) + "]"
        return "[" + type_str(schema, node.get("items", {})) + ", ...]"
    if t in ("number", "integer"):
        bound = ""
        if "exclusiveMinimum" in node:
            bound = f">{node['exclusiveMinimum']:g}"
        elif "minimum" in node:
            bound = f">={node['minimum']:g}"
        if "maximum" in node:
            bound += f"<={node['maximum']:g}"
        return f"{t}{bound}"
    if t == "string" and "pattern" in node:
        return "id"
    return str(t)


def _ref_label(name: str) -> str:
    if name in {m.__name__ for m in NODE_MODELS.values()}:
        return "node"
    if name in {m.__name__ for m in OP_MODELS}:
        return "op"
    return {"Transform": "transform", "MaterialIR": "material"}.get(name, name)


def field_types(schema: dict[str, Any], obj: dict[str, Any]) -> dict[str, str]:
    """``{field: type string}`` for one object schema, ``?`` marking optional fields and ``=x`` defaults."""
    _, obj = _resolve(schema, obj)
    required = set(obj.get("required", ()))
    out = {}
    for name, prop in obj.get("properties", {}).items():
        default = prop.get("default")
        suffix = "" if default is None or isinstance(default, (dict, list)) else f"={json.dumps(default)}"
        out[name] = ("" if name in required else "?") + type_str(schema, prop) + suffix
    return out


def _obj_line(schema: dict[str, Any], obj: dict[str, Any]) -> str:
    return "{" + ", ".join(f"{k}: {v}" for k, v in field_types(schema, obj).items()) + "}"


IR_SEMANTICS = (
    "Scene IR: metres, Z-up, right-handed, rotations XYZ Euler degrees. Primitives are centred on their local origin; "
    "cylinder/cone/capsule axis is local Z (cone base at -height/2, apex at +height/2). A group is a transform-only "
    "parent; mesh_ref params.path is a local asset path. id fields match [A-Za-z_][A-Za-z0-9_]* (at most 64 chars) "
    "and parent / material_id / node_id / target_id must name an existing node / material."
)
OP_SEMANTICS = (
    "Anchors use world AABBs. on_top_of centres X/Y and aligns bottom to target top. flush_back aligns +Y faces. "
    "For ground contact use z = half the primitive height."
)


@functools.cache
def ir_rules_text() -> str:
    """Prose description of the IR for model prompts, generated from the wire schema (fields AND types)."""
    scene, patch = raw_schema("scene"), raw_schema("patch")
    defs = scene["$defs"]
    lines = [IR_SEMANTICS, "Types below: ? marks an optional field, =x its default, [a,b,c] a fixed-length array.",
             "scene: " + _obj_line(scene, scene)]
    node_any = scene["properties"]["nodes"]["items"]
    alts = [_resolve(scene, alt)[1] for alt in node_any.get("oneOf", node_any.get("anyOf", []))]
    common = field_types(scene, alts[0])
    common.pop("kind"), common.pop("params")
    lines.append("node: {kind, params, " + ", ".join(f"{k}: {v}" for k, v in common.items()) + "}")
    lines.append("transform: " + _obj_line(scene, defs["Transform"]))
    kinds = []
    for alt in alts:
        kind = alt["properties"]["kind"]["const"]
        params = alt["properties"]["params"]
        kinds.append(f"{kind} params {_obj_line(scene, params)}")
    lines.append("kind -> params: " + "; ".join(kinds))
    lines.append("material: " + _obj_line(scene, defs["MaterialIR"]))
    ops = []
    for alt in patch["properties"]["ops"]["items"]["anyOf"]:
        _, obj = _resolve(patch, alt)
        ftypes = field_types(patch, obj)
        op = ftypes.pop("op").lstrip("?").split("=")[0]
        ops.append(f"{op} {{" + ", ".join(f"{k}: {v}" for k, v in ftypes.items()) + "}")
    lines.append("patch: " + _obj_line(patch, patch) + '; each op is {"op": <name>, ...fields}: ' + "; ".join(ops))
    lines.append(OP_SEMANTICS)
    return "\n".join(lines)


# --- tool parameters: the same IR as function declarations -----------------------------------------------------
#
# A response schema is sent once per authoring call; a tool declaration is re-sent on every step of a stage loop.
# The per-kind union above costs about 7k prompt tokens as a patch schema, so the tools declare a *lean* form derived
# mechanically from the same wire models: each union (op, node kind, set_param name) becomes one object whose
# discriminator is an enum and whose properties are the union of the members' properties, with each member's own
# required fields stated in the discriminator's description. Everything the lean form cannot express (which params
# belong to which kind, id patterns, exclusive bounds) is enforced by ``parse_ops`` / ``parse_tool_scene``: one model
# per discriminator value, so an error names the op index, the op, the field and what was expected.

_DROP = frozenset({"title", "default", "pattern", "maxLength", "minLength", "additionalProperties", "discriminator"})


def _lean(root: dict[str, Any], node: Any) -> Any:
    """Inline ``$ref``s, drop keywords that only cost tokens, unwrap ``X|null``, ``prefixItems`` of one type -> items."""
    if isinstance(node, list):
        return [_lean(root, x) for x in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        return _lean(root, root["$defs"][node["$ref"].rsplit("/", 1)[-1]])
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _DROP:
            continue
        if key == "const":
            out["enum"] = [value]
        elif key == "exclusiveMinimum":
            out.setdefault("minimum", value)
        elif key == "properties":
            out[key] = {k: _lean(root, v) for k, v in value.items()}
        elif key == "prefixItems":
            items = [_lean(root, x) for x in value]
            if all(i == items[0] for i in items):
                out["items"] = items[0]
            else:
                out["prefixItems"] = items
        else:
            out[key] = _lean(root, value)
    for key in ("anyOf", "oneOf"):
        if key in out:
            alts = [a for a in out.pop(key) if a.get("type") != "null"]
            if len(alts) == 1:
                out.update(alts[0])
            else:
                out["anyOf"] = alts
    return out


def _member_line(tag: str, obj: dict[str, Any], skip: tuple[str, ...]) -> str:
    req = set(obj.get("required", ()))
    fields = [f if f in req else f"{f}?" for f in obj.get("properties", {}) if f not in skip]
    return f"{tag} {{{', '.join(fields)}}}"


def _shape(s: dict[str, Any]) -> Any:
    """What a lean schema accepts, bounds and descriptions aside (integer counts as number)."""
    if "anyOf" in s:
        return tuple(sorted({repr(_shape(a)) for a in s["anyOf"]}))
    t = "number" if s.get("type") == "integer" else s.get("type")
    return (t, _shape(s["items"]) if "items" in s else None, s.get("minItems"))


def _union(variants: list[dict[str, Any]]) -> dict[str, Any]:
    """One lean schema for a property that members type differently: string enums merge into one enum, otherwise an
    ``anyOf`` of the distinct shapes (bounds dropped: the strict parse checks them per member)."""
    flat = [a for v in variants for a in v.get("anyOf", [v])]
    if all("anyOf" not in v for v in variants) and all(v == variants[0] for v in variants):
        return variants[0]
    if all(v.get("type") == "string" and "enum" in v for v in flat):
        return {"type": "string", "enum": list(dict.fromkeys(e for v in flat for e in v["enum"]))}
    shapes: dict[Any, dict[str, Any]] = {}
    for v in flat:
        lean = {k: x for k, x in v.items() if k not in ("minimum", "maximum", "description")}
        if "items" in lean:
            lean["items"] = {k: x for k, x in lean["items"].items() if k not in ("minimum", "maximum")}
        if lean.get("type") == "integer":
            lean["type"] = "number"
        shapes.setdefault(_shape(lean), lean)
    return next(iter(shapes.values())) if len(shapes) == 1 else {"anyOf": list(shapes.values())}


def _merge(root: dict[str, Any], alts: list[dict[str, Any]], tag: str, doc: str) -> dict[str, Any]:
    """One object for a tagged union: ``tag`` becomes an enum, properties are merged (a property typed differently by
    different members becomes ``anyOf`` of its distinct lean types), only fields every member requires are required."""
    members = [_resolve(root, a)[1] for a in alts]
    props: dict[str, Any] = {}
    tags: list[str] = []
    lines: dict[str, str] = {}
    for m in members:
        t = m["properties"][tag]
        values = [t["const"]] if "const" in t else t["enum"]
        tags += [v for v in values if v not in tags]
        lines.setdefault("|".join(values), _member_line("|".join(values), m, (tag,)))
        for name, prop in m["properties"].items():
            if name == tag:
                continue
            props.setdefault(name, []).append(_lean(root, prop))
    props = {name: _union(variants) for name, variants in props.items()}
    required = [tag] + [f for f in props if all(f in m.get("required", ()) for m in members)]
    desc = f"{doc}: " + "; ".join(lines.values())
    return {"type": "object", "properties": {tag: {"type": "string", "enum": tags, "description": desc}, **props},
            "required": required}


def _node_schema(root: dict[str, Any]) -> dict[str, Any]:
    alts = root["$defs"]["WireUpsertNode"]["properties"]["node"]["anyOf"]
    node = _merge(root, alts, "kind", "required fields per kind")
    params = [_resolve(root, _resolve(root, a)[1]["properties"]["params"])[1] for a in alts]
    merged: dict[str, Any] = {}
    per_kind = []
    for kind, p in zip([_resolve(root, a)[1]["properties"]["kind"]["const"] for a in alts], params, strict=True):
        for name, prop in p.get("properties", {}).items():
            merged.setdefault(name, _lean(root, prop))
        req = set(p.get("required", ()))
        fields = [f if f in req else f"{f}?={json.dumps(v['default'])}" if "default" in v else f"{f}?"
                  for f, v in p.get("properties", {}).items()]
        per_kind.append(f"{kind} {{{', '.join(fields)}}}")
    node["properties"]["kind"]["description"] = "params per kind: " + "; ".join(per_kind)
    node["properties"]["params"] = {"type": "object", "properties": merged,
                                    "description": "only the params of this node's kind (see kind)"}
    return node


@functools.cache
def _tool_parts() -> str:
    patch = raw_schema("patch")
    op = _merge(patch, patch["properties"]["ops"]["items"]["anyOf"], "op", "fields per op")
    node = _node_schema(patch)
    for container in ("node",):  # upsert_node.node -> the lean node
        op["properties"][container] = node
    material = _lean(patch, patch["$defs"]["MaterialIR"])
    return json.dumps({"op": op, "node": node, "material": material})


def tool_op_schema() -> dict[str, Any]:
    """One patch op, lean form (see the section comment)."""
    return json.loads(_tool_parts())["op"]


def tool_node_schema() -> dict[str, Any]:
    return json.loads(_tool_parts())["node"]


def tool_material_schema(*, partial: bool = False) -> dict[str, Any]:
    """``MaterialIR`` as a tool parameter; ``partial`` drops ``id`` and every requirement (a diff onto a material)."""
    m = json.loads(_tool_parts())["material"]
    if partial:
        m["properties"].pop("id", None)
        m.pop("required", None)
    return m


def _ops_param(min_items: int = 1) -> dict[str, Any]:
    return {"type": "array", "items": tool_op_schema(), "minItems": min_items}


def apply_scene_ir_parameters() -> dict[str, Any]:
    """Only the patch form is declared: a full scene costs the node and material schemas a second time on every step,
    and ``upsert_node`` / ``upsert_material`` / ``remove_node`` ops express the same edit."""
    return {"type": "object", "properties": {"ops": {**_ops_param(), "description": "Applied in order, as one edit."}},
            "required": ["ops"]}


def propose_variants_parameters() -> dict[str, Any]:
    return {"type": "object", "properties": {
        "set_name": {"type": "string", "description": "Variant set name, e.g. lid."},
        "variants": {"type": "array", "minItems": 1, "description": "Each variant is a patch on the base scene.",
                     "items": {"type": "object", "properties": {"name": {"type": "string"}, "ops": _ops_param()},
                               "required": ["name", "ops"]}},
    }, "required": ["set_name", "variants"]}


def apply_shader_diff_parameters() -> dict[str, Any]:
    return {"type": "object", "properties": {
        "material_id": {"type": "string", "description": "Material to create or change."},
        "diff": {**tool_material_schema(partial=True), "description": "Only the fields to change."},
        "assign_to": {"type": "array", "items": {"type": "string"}, "description": "Node ids to bind this material to."},
    }, "required": ["material_id"]}


# --- strict parse of tool arguments, with errors a model can act on -------------------------------------------


def _op_models_by_tag() -> dict[tuple[str, str | None], type[_Wire]]:
    out: dict[tuple[str, str | None], type[_Wire]] = {}
    for m in OP_MODELS:
        name_ann = m.model_fields.get("name")
        name = get_args(name_ann.annotation)[0] if name_ann is not None and get_args(name_ann.annotation) else None
        for tag in get_args(m.model_fields["op"].annotation):
            out[(tag, name)] = m
    return out


OP_BY_TAG = _op_models_by_tag()
NODE_BY_KIND = NODE_MODELS
MAX_ERRORS = 3


class IRArgumentError(ValueError):
    """Tool arguments that do not match the IR; the message is one line naming where and what."""


def _loc(prefix: str, loc: tuple[Any, ...]) -> str:
    out = prefix
    for part in loc:
        out += f"[{part}]" if isinstance(part, int) else f".{part}"
    return out


def _explain(prefix: str, err: Any) -> list[str]:
    from pydantic import ValidationError

    if not isinstance(err, ValidationError):
        return [f"{prefix}: {err}"]
    out = []
    for e in err.errors(include_url=False):
        loc = tuple(p for p in e["loc"] if p not in NODE_BY_KIND)  # drop the union-member hop of a tagged union
        got = "" if e["type"] == "missing" else f" (got {json.dumps(e.get('input'), default=str)[:40]})"
        out.append(f"{_loc(prefix, loc)}: {e['msg']}{got}")
    return out


def _parse_op(i: int, op: Any) -> tuple[BaseModel | None, list[str]]:
    where = f"ops[{i}]"
    if not isinstance(op, dict):
        return None, [f"{where}: expected an object with an 'op' field"]
    tag = op.get("op")
    name = op.get("name") if tag == "set_param" else None
    model = OP_BY_TAG.get((tag, name))
    if model is None:
        if tag == "set_param":
            names = sorted(n for t, n in OP_BY_TAG if t == "set_param")
            return None, [f"{where}: set_param name must be one of {', '.join(names)} (got {name!r})"]
        tags = sorted({t for t, _ in OP_BY_TAG})
        return None, [f"{where}: unknown op {tag!r}; one of {', '.join(tags)}"]
    where = f"{where}({tag}{'/' + name if name else ''})"
    node = op.get("node") if tag == "upsert_node" else None
    if isinstance(node, dict) and node.get("kind") not in NODE_BY_KIND:
        return None, [f"{where}.node.kind: one of {', '.join(NODE_BY_KIND)} (got {node.get('kind')!r})"]
    try:
        return model.model_validate(op), []
    except ValueError as e:
        return None, _explain(where, e)


def _raise(errors: list[str]) -> None:
    more = f" (+{len(errors) - MAX_ERRORS} more)" if len(errors) > MAX_ERRORS else ""
    raise IRArgumentError("invalid IR: " + "; ".join(errors[:MAX_ERRORS]) + more)


def parse_ops(ops: Any) -> ir.IRPatch:
    """Strict parse of a tool's ``ops`` list into ``ir.IRPatch``; raises ``IRArgumentError`` (one line)."""
    if not isinstance(ops, list) or not ops:
        _raise(["ops: expected a non-empty list of ops"])
    parsed, errors = [], []
    for i, op in enumerate(ops):
        model, errs = _parse_op(i, op)
        parsed.append(model)
        errors += errs
    if errors:
        _raise(errors)
    try:
        return patch_from_wire(WirePatch(ops=parsed))
    except ValueError as e:
        _raise(_explain("patch", e))
        raise  # unreachable


def parse_tool_scene(asset_name: str, nodes: Any, materials: Any) -> ir.SceneIR:
    """Strict parse of a full scene given as tool arguments (``materials`` may be omitted)."""
    payload = {"asset_name": asset_name, "nodes": nodes, "materials": materials if materials is not None else []}
    errors = []
    for i, n in enumerate(nodes if isinstance(nodes, list) else []):
        if isinstance(n, dict) and n.get("kind") not in NODE_BY_KIND:
            errors.append(f"nodes[{i}].kind: one of {', '.join(NODE_BY_KIND)} (got {n.get('kind')!r})")
    if errors:
        _raise(errors)
    try:
        return scene_from_wire(WireScene.model_validate(payload))
    except ValueError as e:
        _raise(_explain("scene", e))
        raise  # unreachable
