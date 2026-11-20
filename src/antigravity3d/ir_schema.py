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
