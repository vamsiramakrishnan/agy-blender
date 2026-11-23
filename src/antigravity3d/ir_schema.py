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
import os
import re
from typing import Annotated, Any, Literal, Union, get_args

import annotated_types
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, create_model, model_validator
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from antigravity3d import ir, ir_shortcuts

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


# --- colour as a model writes and reads it ------------------------------------------------------------------------
#
# The scene format's colours are linear RGB (``ir.MaterialIR``; USD, glTF and the package are linear too), and every
# stored or programmatic scene keeps that meaning. A model writes what a colour picker or a hex code means, sRGB-like
# numbers: live on 2026-11-22 each of thirteen materials of this round's designs had the hue and saturation of its words
# read as sRGB (r5/scratch_colour/EVIDENCE.txt: olive drab (0.18, 0.22, 0.14) is #2e3824 as sRGB, shipped #768169).
# Read exactly as sRGB they first lost a blind judgement 6 of 6 (the generated wear was tuned for the pale colours: white
# seams, dark gunmetal on dark olive stopped separating); with the corrected surfaces they win (``COLOUR_RULE``). The
# boundary is plumbed once and the rule is one switch:
# * every colour in a model's answer goes through ``ir_linear`` exactly once, where the answer becomes scene data
#   (``scene_from_wire`` and ``patch_from_wire``, which every design, repair, improvement, correction, edit, kit piece
#   and native-session edit goes through, ``apply_look`` and ``apply_material_diff``), and through ``written_colour``
#   when it is shown to a model (``as_written`` / ``written_json``, the look round's table), so the two always agree;
# * the rule is ``COLOUR_RULE`` (default "srgb"; "as_written" is exactly the behaviour before this change; the other two
#   are unmeasured candidates, ``linear_colour``).
# The wire models hold colours as written; nothing converts on validation, so a written answer copied into another one
# (a correction) is still converted once.
COLOUR_FIELDS = ("base_color", "emissive_color", "sheen_color", "specular_color")
HEX_COLOUR = r"^#[0-9A-Fa-f]{6}$"
SHOWN_DIGITS = 4  # a written colour shown back reads as written (the model writes two or three decimals)
COLOUR_RULES = ("as_written", "srgb", "srgb_albedo", "gamma")
# THE SWITCH. "srgb" (the default): exact sRGB -> linear, with the request saying so. Measured live on 2026-11-22 with the
# corrected generated surfaces (wear relative to the paint, none in seams, metal 0 or 1 per layer): the same designs
# built with converted colours against the colours as stored before, blind pairwise, both orders: crate 4 of 4 and
# hydrant 4 of 4 for the converted ones ("richer military olive colouring, better value contrast", "richer red colour
# and more realistic paint"); before the surface fix the same comparison had gone 0 of 6 the other way.
# "as_written": the number is the stored (linear) value and the request says what it said on c8565a7, byte for byte
# (tests/test_colour.py): the behaviour before this change. "srgb_albedo" (sRGB -> linear, then a base colour's
# luminance kept inside the PBR albedo range by kind: ALBEDO_LUMINANCE, METAL_LUMINANCE_MIN) and "gamma" (linear =
# written ** COLOUR_GAMMA): candidates, NOT MEASURED.
# For an experiment without editing the code: AGY3D_COLOUR_RULE=<rule> / AGY3D_STATE_METALLIC=1 in the environment of
# the process (read once, at import: the schemas and the rules text are built from them).
COLOUR_RULE = os.environ.get("AGY3D_COLOUR_RULE") or "srgb"
if COLOUR_RULE not in COLOUR_RULES:
    raise ValueError(f"AGY3D_COLOUR_RULE={COLOUR_RULE!r}: one of {COLOUR_RULES}")
# The second switch, separate because it too changes what authors write and is unmeasured: whether the request says
# that metallic is 1 for bare metal and 0 for everything else (the live designs wrote 0.15-0.25 for painted metal and
# 0.85-0.9 for bare metal and brass). Off: the request says what it always said.
STATE_METALLIC = os.environ.get("AGY3D_STATE_METALLIC") == "1"
COLOUR_GAMMA = 1.5
# Non-metal albedo luminance, linear: sRGB 50 to 240 of 255 (charcoal to fresh snow), the usual PBR validation range.
ALBEDO_LUMINANCE = (0.0319, 0.8714)
# A metal's base colour is its reflectance at normal incidence: real metals are 0.5 or more (iron 0.56, steel 0.56-0.6,
# brass (0.91, 0.78, 0.42)), so a darker "metal" is a coating, which metallic-roughness writes as metallic 0.
METAL_LUMINANCE_MIN = 0.5


def _unit3(v: tuple[float, float, float]) -> tuple[float, float, float]:
    if not all(0.0 <= c <= 1.0 for c in v):
        raise ValueError(f"colour components are 0 to 1 (sRGB, as a colour picker), got {list(v)}")
    return v


WrittenColour = Union[Annotated[tuple[float, float, float], AfterValidator(_unit3)],
                      Annotated[str, Field(pattern=HEX_COLOUR)]]


def told() -> bool:
    """Whether the request tells the model the colour rule (the sRGB clause with its anchors, colours shown converted,
    "#rrggbb" offered). Under "as_written" it does not: what it is told, shown and offered is what it always was
    (tests/test_colour.py pins that byte for byte), so the default rule is inert."""
    return COLOUR_RULE != "as_written"


def srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def linear_to_srgb(c: float) -> float:
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def _numbers(written: Any) -> list[float]:
    if isinstance(written, str):
        h = written.lstrip("#")
        return [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    return [float(c) for c in written]


def _luminance(rgb: Any) -> float:
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]


def linear_colour(written: Any, *, field: str = "base_color", metallic: float = 0.0, rule: str | None = None) -> tuple[float, float, float]:
    """The one conversion: a colour as a model writes it (three numbers 0-1, or "#rrggbb") -> the scene format's linear
    RGB, by ``rule`` (None: ``COLOUR_RULE``). ``field`` and ``metallic``: "srgb_albedo" keeps a base colour's luminance
    in the range of its kind (a non-metal between ``ALBEDO_LUMINANCE``, a metal, metallic 0.5 or more, at least
    ``METAL_LUMINANCE_MIN``), scaling the colour so its hue and saturation stay; other colours only convert."""
    rule = rule or COLOUR_RULE
    rgb = _numbers(written)  # a hex is its own values over 255 under every rule (under "as_written": what was written)
    if rule == "as_written":
        return tuple(rgb)  # type: ignore[return-value]
    if rule == "gamma":
        return tuple(c ** COLOUR_GAMMA for c in rgb)  # type: ignore[return-value]
    lin = [srgb_to_linear(c) for c in rgb]
    if rule == "srgb_albedo" and field == "base_color":
        y = _luminance(lin)
        lo, hi = (METAL_LUMINANCE_MIN, 1.0) if metallic >= 0.5 else ALBEDO_LUMINANCE
        if y < lo:
            lin = [min(1.0, c * lo / y) if y > 0 else lo for c in lin]
        elif y > hi:
            lin = [c * hi / y for c in lin]
    elif rule not in COLOUR_RULES:
        raise ValueError(f"unknown colour rule {rule!r}; one of {COLOUR_RULES}")
    return tuple(lin)  # type: ignore[return-value]


def written_colour(linear: Any, *, rule: str | None = None) -> list[float]:
    """A stored (linear) colour as a model reads and writes it under ``rule`` (None: ``COLOUR_RULE``), ``SHOWN_DIGITS``
    decimals: the inverse of ``linear_colour`` (for "srgb_albedo", of its conversion; a lifted colour shows as lifted)."""
    rule = rule or COLOUR_RULE
    if rule == "as_written":
        out = [float(c) for c in linear]
    elif rule == "gamma":
        out = [float(c) ** (1 / COLOUR_GAMMA) for c in linear]
    else:
        out = [linear_to_srgb(float(c)) for c in linear]
    return [round(c, SHOWN_DIGITS) + 0.0 for c in out]


def ir_linear(fields: dict[str, Any], *, metallic: float | None = None) -> dict[str, Any]:
    """Material fields as an answer wrote them -> as the scene format holds them (every colour field converted once).
    ``metallic``: the material's when the fields do not write it (a partial change of a stored material)."""
    m = float(fields.get("metallic", metallic if metallic is not None else 0.0) or 0.0)
    return {k: (linear_colour(v, field=k, metallic=m) if k in COLOUR_FIELDS and v is not None else v) for k, v in fields.items()}


def as_written(scene: ir.SceneIR) -> dict[str, Any]:
    """``scene`` as a model reads it (and would write it): the dump a request shows (``exclude_defaults``), every
    material colour in the written form of the rule. ``parse_scene(json.dumps(as_written(s)))`` is ``s`` again
    (tests/test_colour.py). Under "as_written" the dump itself, as requests always showed it."""
    doc = scene.model_dump(mode="json", exclude_defaults=True)
    if not told():
        return doc
    if "materials" in doc:  # left out, as a default, when there are none: the dump is the one requests always showed
        doc["materials"] = [{k: (written_colour(v) if k in COLOUR_FIELDS else v) for k, v in m.items()} for m in doc["materials"]]
    return doc


def written_json(scene: ir.SceneIR) -> str:
    """``as_written`` as the compact JSON a request carries (the "Scene: ..." of a repair or improvement request); under
    "as_written" exactly the text it always was (pydantic's own JSON)."""
    if not told():
        return scene.model_dump_json(exclude_defaults=True)
    return json.dumps(as_written(scene), separators=(",", ":"))


def _material_model() -> type[_Wire]:
    """``MaterialIR`` as a model writes it: every colour field ``WrittenColour`` (three sRGB numbers or "#rrggbb", not
    converted here); the cross-field rules are checked on the converted material (``ir.MaterialIR``), where the answer
    becomes scene data. No field descriptions: what the colours mean is said once, in IR_SEMANTICS, which every request
    and every stage that edits materials carries (a description would be repeated in each tool declaration)."""
    fields: dict[str, Any] = {}
    for name, info in ir.MaterialIR.model_fields.items():
        f = copy.copy(info)
        if name in COLOUR_FIELDS and told():  # under "as_written" the colour fields are typed as they always were
            f.metadata = []
            fields[name] = (WrittenColour, f)
        else:
            fields[name] = (info.annotation, f)
    return create_model("MaterialIR", __base__=_Wire, __doc__=ir.MaterialIR.__doc__, **fields)


# What a model is told about colour and metalness, once: clauses of IR_SEMANTICS, which the authoring rules text and every
# stage agent that edits geometry or materials carry. Metallic: the same live designs wrote 0.15-0.25 for painted metal
# and 0.85-0.9 for bare metal and brass; in metallic-roughness PBR a surface is metal or not.
COLOUR_CLAUSE = ('colours are sRGB, as a colour picker or hex code means them: three numbers 0-1 or "#rrggbb" (military '
                 'olive drab is about #3d4a2b, black rubber about #1c1c1c)')
METAL_CLAUSE = "metallic is 1 for bare metal and 0 for everything else, paint on metal included"
WireMaterial = _material_model()


def colour_note() -> str:
    """What an agent that writes colours through the MCP server's tools is told, as a sentence to append (the server's
    instructions and the descriptions of the tools that take a colour): the clause of IR_SEMANTICS, and that the tools do
    the conversion. "" under "as_written": nothing is added to what such an agent was told before the colour rule.

    The stage agents carry IR_SEMANTICS; a session in a stock host reads only the server's instructions, its tool
    descriptions and the skills, and none of them said what a colour means. Live 2026-11-22 (r5/live_rerun_N): the
    agent wrote olive drab as (0.22, 0.27, 0.16), then on its second pass "corrected" its three materials to linear
    values, (0.065, 0.09, 0.038) for the olive; read as sRGB the crate shipped nearly black and lost 0 of 6 blind
    verdicts to each stock crate. Every first answer of the nine live sessions on record (27 materials) was sRGB-like."""
    if not told():
        return ""
    return f" {COLOUR_CLAUSE[0].upper()}{COLOUR_CLAUSE[1:]}; the tools convert them, so never convert a colour to linear yourself."


def _material_ir(m: BaseModel) -> dict[str, Any]:
    """A wire material as the scene format holds it: the fields it wrote (so an unwritten colour keeps the format's
    own default, which is linear), colours converted."""
    return ir_linear(m.model_dump(exclude_unset=True))


Axis = Literal["x", "y", "z"]
RepeatCount = Annotated[int, Field(ge=2, le=ir_shortcuts.MAX_REPEAT)]


class RepeatRow(_Wire):
    count: RepeatCount
    offset: ir.Vec3


class RepeatRing(_Wire):
    count: RepeatCount
    around: Axis


# The mirror / repeat shortcuts (``ir_shortcuts``): wire-only node fields, expanded into plain nodes by
# ``scene_from_wire`` / ``patch_from_wire``. Their default is None but their type is not nullable, so the schema the
# model is constrained by has no ``null`` branch for them: a node without a shortcut simply leaves the field out.
# ``repeat`` is a row or a ring, each forbidding the other's key (see ir_shortcuts for the live failure behind it).
SHORTCUT_FIELDS: dict[str, Any] = {
    "mirror": (Axis, None),
    "repeat": (Union[RepeatRow, RepeatRing], None),
}
assert tuple(SHORTCUT_FIELDS) == ir_shortcuts.SHORTCUT_FIELDS


class _WireNode(_Wire):
    @model_validator(mode="after")
    def _shortcuts(self) -> "_WireNode":
        ir_shortcuts.check({"id": self.id, "kind": self.kind, "mirror": self.mirror,
                            "repeat": None if self.repeat is None else self.repeat.model_dump()})
        return self


def _node_model(kind: str) -> type[_Wire]:
    params = ir.NODE_PARAMS[kind]
    needs = any(f.is_required() for f in params.model_fields.values())
    fields: dict[str, Any] = {}
    for fname, info in ir.NodeIR.model_fields.items():
        fields[fname] = _field(ir.NodeIR, fname, info, {ir.Transform: WireTransform}.get(info.annotation, info.annotation))
    fields["kind"] = (Literal[kind], ...)
    fields["params"] = (params, ... if needs else Field(default_factory=params))
    fields.update(SHORTCUT_FIELDS)
    return create_model(f"{_camel(kind)}Node", __base__=_WireNode, __doc__=f"A {kind} node.", **fields)


NODE_MODELS: dict[str, type[_Wire]] = {k: _node_model(k) for k in NODE_KINDS}
WireNode = Annotated[Union[tuple(NODE_MODELS.values())], Field(discriminator="kind")]  # type: ignore[valid-type]
_SUBST: dict[Any, Any] = {ir.NodeIR: WireNode, ir.Transform: WireTransform, ir.MaterialIR: WireMaterial}


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
    "nodes": _field(ir.SceneIR, "nodes", ir.SceneIR.model_fields["nodes"], list[WireNode]),
    "materials": _field(ir.SceneIR, "materials", ir.SceneIR.model_fields["materials"], list[WireMaterial])})
WirePatch = _derive("WirePatch", ir.SceneIRDiff, {}, override={
    "ops": _field(ir.SceneIRDiff, "ops", ir.SceneIRDiff.model_fields["ops"], list[WireOp])})


# A correction of a design that failed to read (tactics.metered_author: one per candidate): only what must change, as
# whole wire nodes and materials (an existing id replaces that entry, a new id adds one) and ids to remove, applied to
# the failed answer before it is read again. Not a whole new design: a large answer (a director's crate was 29,521 output
# tokens live) would be paid twice, could truncate, and could break parts that were right.
WireCorrection = create_model(
    "WireCorrection", __base__=_Wire, __doc__="Replacement nodes / materials and ids to remove.",
    nodes=(list[WireNode], ...), materials=(list[WireMaterial], ...), remove=(list[ir.NodeId], ...))


def correction_response_schema() -> dict[str, Any]:
    return _api(WireCorrection.model_json_schema())


def apply_correction(failed: dict[str, Any], text: str) -> ir.SceneIR:
    """``failed`` (the answer as written, a JSON object) with the correction ``text`` applied, read as a scene.
    Raises ValueError (a ValidationError included) naming what is still wrong."""
    fix = WireCorrection.model_validate_json(text)
    out = dict(failed)
    gone = set(fix.remove)
    # both as written (colours not converted): the merged answer is read, and converted once, by parse_scene
    for key, new in (("nodes", [_node_wire(n) for n in fix.nodes]), ("materials", [m.model_dump(exclude_unset=True) for m in fix.materials])):
        entries = [e for e in (failed.get(key) or []) if not (isinstance(e, dict) and e.get("id") in gone)]
        at = {e.get("id"): i for i, e in enumerate(entries) if isinstance(e, dict)}
        for e in new:
            if e["id"] in at:
                entries[at[e["id"]]] = e
            else:
                at[e["id"]] = len(entries)
                entries.append(e)
        out[key] = entries
    return parse_scene(json.dumps(out))


def _node_wire(node: BaseModel) -> dict[str, Any]:
    """A wire node as the author wrote it (params unfilled, shortcuts kept), for writing back into an answer."""
    d = node.model_dump(exclude_none=True)
    d["params"] = _params_as_written(node)
    if node.repeat is not None:
        d["repeat"] = node.repeat.model_dump()
    return d


def _is_written_colour(v: Any) -> bool:
    if isinstance(v, str):
        return re.fullmatch(HEX_COLOUR[1:-1], v) is not None
    return isinstance(v, (list, tuple)) and len(v) == 3 and all(isinstance(c, (int, float)) and not isinstance(c, bool) and 0 <= c <= 1 for c in v)


def located_nodes(failed: dict[str, Any], exc: BaseException) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(nodes, materials) of ``failed`` that ``exc`` points at: a validation error's ``nodes.<i>`` / ``materials.<i>``
    locations, else every node or material whose id the message names (an expansion or reference error names ids)."""
    from pydantic import ValidationError

    nodes = [n for n in failed.get("nodes") or [] if isinstance(n, dict)]
    mats = [m for m in failed.get("materials") or [] if isinstance(m, dict)]
    ni, mi = set(), set()
    if isinstance(exc, ValidationError):
        for e in exc.errors():
            loc = e.get("loc") or ()
            if len(loc) >= 2 and loc[0] == "nodes" and isinstance(loc[1], int):
                ni.add(loc[1])
            elif len(loc) >= 2 and loc[0] == "materials" and isinstance(loc[1], int):
                mi.add(loc[1])
    if not ni and not mi:
        words = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", str(exc)))
        stems = {w.split("__", 1)[0] for w in words}  # a copy's id names the node it was made from
        ni = {i for i, n in enumerate(nodes) if n.get("id") in words or n.get("id") in stems}
        mi = {i for i, m in enumerate(mats) if m.get("id") in words}
    return [nodes[i] for i in sorted(ni) if i < len(nodes)], [mats[i] for i in sorted(mi) if i < len(mats)]


# --- conversion back into the public IR types ---------------------------------------------------------------


def _params_as_written(node: BaseModel) -> dict[str, Any]:
    """A node's params exactly as the answer wrote them. ``NodeIR.params`` is free-form, so a typed default filled in
    here would be written into the scene: a box that never mentions ``bevel`` would come back with ``bevel: 0`` and
    five other new keys, and a round trip would not be exact. The kernel applies the same defaults (see
    tests/test_ir_schema.py ``test_kernel_defaults_match_typed_param_defaults``)."""
    return node.params.model_dump(exclude_unset=True)


def _node_dump(node: BaseModel) -> dict[str, Any]:
    return {**node.model_dump(exclude_none=True), "params": _params_as_written(node)}


def scene_from_wire(wire: BaseModel) -> ir.SceneIR:
    """The wire scene as ``ir.SceneIR``, mirror / repeat shortcuts expanded into plain nodes (``ir_shortcuts``)."""
    plain, copies = ir_shortcuts.expand([_node_dump(n) for n in wire.nodes])
    doc = {**wire.model_dump(exclude_none=True, exclude={"nodes", "materials"}), "nodes": plain + [c for _, c in copies],
           "materials": [_material_ir(m) for m in wire.materials]}
    return ir.SceneIR.model_validate(doc)


def patch_from_wire(wire: BaseModel) -> ir.IRPatch:
    """The wire patch as ``ir.IRPatch``. Shortcuts on ``upsert_node`` ops expand over the nodes upserted in the same
    patch (a node already in the scene is not seen here, so its children are copied only when the patch upserts
    them too); each copy is upserted right after the op that upserted its source node."""
    ops = [{**op.model_dump(exclude_none=True), **({"node": _node_dump(op.node)} if op.op == "upsert_node" else {}),
            **({"material": _material_ir(op.material)} if op.op == "upsert_material" else {})} for op in wire.ops]
    ups = [i for i, op in enumerate(ops) if op["op"] == "upsert_node"]
    plain, copies = ir_shortcuts.expand([ops[i]["node"] for i in ups])
    after: dict[int, list[dict[str, Any]]] = {}
    for i, node in zip(ups, plain, strict=True):
        ops[i]["node"] = node
    last = {ops[i]["node"]["id"]: i for i in ups}
    for source, copy_ in copies:
        after.setdefault(last[source], []).append({"op": "upsert_node", "node": copy_})
    out = [x for i, op in enumerate(ops) for x in (op, *after.get(i, ()))]
    return ir.IRPatch.model_validate({"ops": out})


def parse_scene(text: str) -> ir.SceneIR:
    """Strictly typed parse of a model's SceneIR JSON (per-kind params), returned as ``ir.SceneIR``."""
    return scene_from_wire(WireScene.model_validate_json(text))


def parse_patch(text: str) -> ir.IRPatch:
    return patch_from_wire(WirePatch.model_validate_json(text))


# A look round's answer (tactics.look_round): material operations only, and possibly none. upsert_material changes the
# material fields it writes (the others stay); assign_material moves a part to another material. Nothing
# that moves or reshapes geometry can be written: the geometry was judged and kept before the look round.
def _look_material() -> type[_Wire]:
    """``MaterialIR`` with every field but ``id`` optional and no cross-field rule: a look answer writes only the fields it
    changes, and the rules are checked on the material it makes with them (``apply_look``)."""
    fields: dict[str, Any] = {}
    for name, info in WireMaterial.model_fields.items():  # colours as written (converted in apply_look)
        if name == "id":
            fields[name] = (info.annotation, info)
            continue
        f = copy.copy(info)
        f.default, f.default_factory = None, None
        f.exclude_if = None
        fields[name] = (info.annotation, f)
    return create_model("LookMaterial", __base__=_Wire, __doc__="The fields of one material to change.", **fields)


LookMaterial = _look_material()
LookUpsertMaterial = create_model("LookUpsertMaterial", __base__=_Wire, op=(Literal["upsert_material"], ...),
                                  material=(LookMaterial, ...))
WireLook = create_model(
    "WireLook", __base__=_Wire, __doc__="Material changes of a look round; an empty list is no change.",
    ops=(list[Union[LookUpsertMaterial, next(m for m in OP_MODELS if m.__name__ == "WireAssignMaterial")]], ...))  # type: ignore[misc]


def look_response_schema() -> dict[str, Any]:
    return _api(WireLook.model_json_schema())


# What a look answer may not change: what the export bundle depends on (whether a material is opaque decides hidden
# contact faces and the atlas, fast_ops.run_export_pipeline), and the image maps, which only an image call makes.
LOOK_FIXED_FIELDS = ("alpha_mode", "alpha_cutoff", "opacity", "transmission", "double_sided", "albedo_uri", "orm_uri",
                     "normal_uri", "emissive_uri", "normal_convention")


def apply_look(scene: ir.SceneIR, text: str) -> ir.IRPatch | None:
    """A look answer read against ``scene``: an ``upsert_material`` changes only the fields it writes (the rest stay
    as the material has them), an ``assign_material`` must name a part and a material that exist. None: no change.
    Raises ValueError naming what is wrong (a field it may not change, a colour under an image map, a contradiction
    between material fields), which the correction is given."""
    wire = WireLook.model_validate_json(text)
    mats = {m.id: m for m in scene.materials}
    nodes = {n.id for n in scene.nodes}
    ops: list[dict[str, Any]] = []
    errors: list[str] = []
    for i, op in enumerate(wire.ops):
        if op.op == "upsert_material":
            written = op.material.model_dump(exclude_unset=True)
            if op.material.id in mats:  # the answer becomes scene data: converted once, by the material's own metalness
                written = ir_linear(written, metallic=mats[op.material.id].metallic)
            written.pop("id", None)
            cur = mats.get(op.material.id)
            if cur is None:
                errors.append(f"ops[{i}]: material {op.material.id!r} does not exist; change one of {sorted(mats)} or move a "
                              "part to one of them with assign_material")
                continue
            fixed = [k for k in LOOK_FIXED_FIELDS if k in written and written[k] != getattr(cur, k)]
            if fixed:
                errors.append(f"ops[{i}]: {cur.id}: {', '.join(fixed)} cannot change in a look round (only colour, "
                              "roughness, metalness, emission, coat, sheen, specular and surface words)")
                continue
            if cur.albedo_uri and "base_color" in written and tuple(written["base_color"]) != tuple(cur.base_color):
                errors.append(f"ops[{i}]: {cur.id}: its colour comes from an image map; base_color cannot change without a new "
                              "image (change roughness or metallic, or leave it)")
                continue
            try:
                new = ir.MaterialIR.model_validate({**cur.model_dump(), **written, "id": cur.id})
            except ValueError as e:
                errors.append(f"ops[{i}]: {' '.join(str(e).split())[:300]}")
                continue
            mats[new.id] = new
            ops.append({"op": "upsert_material", "material": new.model_dump()})
        else:
            if op.node_id not in nodes:
                errors.append(f"ops[{i}]: assign_material: no part {op.node_id!r}")
            elif op.material_id not in mats:
                errors.append(f"ops[{i}]: assign_material: no material {op.material_id!r}; one of {sorted(mats)}")
            else:
                ops.append(op.model_dump(exclude_none=True))
    if errors:
        raise ValueError("; ".join(errors))
    return ir.IRPatch.model_validate({"ops": ops}) if ops else None


# --- API schema ----------------------------------------------------------------------------------------------


# Keywords the API accepts that the response schema still leaves out: they only cost prompt tokens. ``title`` repeats a
# field name. ``description`` is pydantic's class docstring (developer documentation, not guidance); what a model needs
# to know about units, axes and what a field means is stated once in ``IR_SEMANTICS`` / ``OP_SEMANTICS``.
_API_UNSENT = frozenset({"title", "description"})


def _api(node: Any) -> Any:
    """Reduce a pydantic JSON schema to ``API_SCHEMA_KEYWORDS`` (recursively), then write it compactly."""
    return _short_refs(_share(_merge_alternatives(_inline_once(_reduce(node)))))


def _short_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Short local definition names; field names, types and constraints stay unchanged.

    Each kind repeats references to transforms, vectors and nullable strings. Keeping the definition names out of
    those repetitions makes room for new shape parameters without growing the response-schema budget. These names
    are internal addresses, not the names a model writes in its answer.
    """
    defs = schema.get("$defs")
    if not defs:
        return schema
    names = {name: f"D{i}" for i, name in enumerate(defs)}

    def replace(value: Any) -> Any:
        if isinstance(value, list):
            return [replace(v) for v in value]
        if not isinstance(value, dict):
            return value
        return {k: (f"#/$defs/{names[v[8:]]}" if k == "$ref" and isinstance(v, str) and v.startswith("#/$defs/")
                    and v[8:] in names else replace(v)) for k, v in value.items()}

    out = replace(schema)
    out["$defs"] = {names[name]: value for name, value in out["$defs"].items()}
    return out


def _merge_alternatives(node: Any) -> Any:
    """Shorter spellings of an ``anyOf`` that accept the same documents: identical branches once, and branches that
    differ only in one string enum property merged into one branch with the union of the enums. The case it is for is
    ``set_param``: one variant per parameter name, so that ``value`` is typed per name, but with the API's bounds
    (``exclusiveMinimum`` written as ``minimum``) radius, bevel, inset_margin, inset_depth, depth and height take the
    same value schema, and {name: a, value: T} | {name: b, value: T} is {name: a|b, value: T}. The wire parse still
    checks each name's own bounds."""
    if isinstance(node, list):
        return [_merge_alternatives(x) for x in node]
    if not isinstance(node, dict):
        return node
    node = {k: _merge_alternatives(v) for k, v in node.items()}
    alts = node.get("anyOf")
    if not isinstance(alts, list):
        return node
    out: list[Any] = []
    for alt in alts:
        if alt in out:
            continue
        for i, prev in enumerate(out):
            merged = _enum_union(prev, alt)
            if merged is not None:
                out[i] = merged
                break
        else:
            out.append(alt)
    if len(out) == 1:
        rest = {k: v for k, v in node.items() if k != "anyOf"}
        return {**rest, **out[0]} if not set(rest) & set(out[0]) else {**node, "anyOf": out}
    return {**node, "anyOf": out}


def _enum_union(a: Any, b: Any) -> dict[str, Any] | None:
    """``a`` and ``b`` as one object schema when they differ only in one string-enum property, else None."""
    if not (isinstance(a, dict) and isinstance(b, dict)) or set(a) != set(b) or "properties" not in a:
        return None
    if {k: v for k, v in a.items() if k != "properties"} != {k: v for k, v in b.items() if k != "properties"}:
        return None
    pa, pb = a["properties"], b["properties"]
    if list(pa) != list(pb):
        return None
    diff = [k for k in pa if pa[k] != pb[k]]
    if len(diff) != 1:
        return None
    x, y = pa[diff[0]], pb[diff[0]]
    if not all(set(v) == {"type", "enum"} and v["type"] == "string" for v in (x, y)):
        return None
    return {**a, "properties": {**pa, diff[0]: {**x, "enum": list(dict.fromkeys([*x["enum"], *y["enum"]]))}}}


def _inline_once(schema: dict[str, Any]) -> dict[str, Any]:
    """Write a definition referenced exactly once where it is used. pydantic gives every model a definition (each node
    kind and its params), and a reference plus its definition's key costs about 40 bytes more than the schema in place;
    the document a schema accepts is the same either way."""
    defs = schema.get("$defs")
    if not defs:
        return schema
    while True:
        uses: dict[str, list[tuple[Any, Any]]] = {}
        for root in (schema, *defs.values()):
            for container, key in _walk_slots(root):
                ref = container[key].get("$ref", "")
                if ref.startswith("#/$defs/"):
                    uses.setdefault(ref.rsplit("/", 1)[-1], []).append((container, key))
        once = [name for name, slots in uses.items() if len(slots) == 1]
        if not once:
            break
        for name in once:
            container, key = uses[name][0]
            container[key] = defs.pop(name)
        if not defs:
            del schema["$defs"]
            break
    return schema


def _whole(value: Any) -> Any:
    return int(value) if isinstance(value, float) and value.is_integer() else value


def _reduce(node: Any) -> Any:
    if isinstance(node, list):
        return [_reduce(x) for x in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "const":
            out["enum"] = [value]
        elif key == "oneOf":
            out["anyOf"] = _reduce(value)
        elif key in ("properties", "$defs"):
            out[key] = {k: _reduce(v) for k, v in value.items()}
        elif key in ("minimum", "maximum"):
            out[key] = _whole(value)  # 1.0 and 1 are one number; the shorter spelling is cheaper
        elif key in API_SCHEMA_KEYWORDS and key not in _API_UNSENT:
            out[key] = _reduce(value)
    if "exclusiveMinimum" in node and "minimum" not in out:  # closest documented bound; the wire parse keeps it strict
        out["minimum"] = _whole(node["exclusiveMinimum"])
    if "$ref" in out:  # the API allows only $-keywords next to $ref
        out = {k: v for k, v in out.items() if k.startswith("$")}
    items = out.get("prefixItems")
    if items and out.get("minItems") == out.get("maxItems") == len(items) and all(i == items[0] for i in items):
        del out["prefixItems"]  # a fixed-length tuple of one type is "exactly n items of that type"
        out["items"] = items[0]
    return out


def _schema_slots(node: dict[str, Any]):
    """(container, key) for every sub-schema of ``node``, so a caller can read or replace it."""
    for key in ("properties",):
        for k in node.get(key, {}):
            yield node[key], k
    for key in ("anyOf", "prefixItems"):
        for i in range(len(node.get(key, ()))):
            yield node[key], i
    for key in ("items", "additionalProperties"):
        if isinstance(node.get(key), dict):
            yield node, key


def _walk_slots(node: dict[str, Any]):
    for container, key in _schema_slots(node):
        yield container, key
        yield from _walk_slots(container[key])


def _shared_name(shape: dict[str, Any], taken: set[str]) -> str:
    items = shape.get("items")
    fixed = shape.get("minItems") is not None and shape.get("minItems") == shape.get("maxItems")
    if shape.get("type") == "array" and fixed and isinstance(items, dict):  # a list of any length is not a vector
        name = f"Vec{shape['minItems']}" + ("NonNeg" if items.get("minimum") == 0 else "")
    elif "anyOf" in shape and {"type": "null"} in shape["anyOf"] and len(shape["anyOf"]) == 2:
        other = next(a for a in shape["anyOf"] if a != {"type": "null"})
        name = "Nullable" + str(other.get("type", "Value")).capitalize()
    elif shape.get("type") == "number" and (shape.get("minimum"), shape.get("maximum")) == (0, 1):
        name = "UnitNumber"
    elif shape.get("type") == "number" and set(shape) == {"type", "minimum"} and shape["minimum"] == 0:
        name = "NonNeg"
    elif shape.get("type") == "string" and shape.get("enum") == list(ir_shortcuts.AXES):
        name = "Axis"
    elif shape.get("type") == "integer":
        name = "Count"
    else:
        name = "Shared"
    base, n = name, 1
    while name in taken:
        n, name = n + 1, f"{base}{n + 1}"
    return name


def _dumps(x: Any) -> str:
    return json.dumps(x, separators=(",", ":"), sort_keys=True)


def _share(schema: dict[str, Any]) -> dict[str, Any]:
    """Move a sub-schema written out several times into ``$defs`` once and ``$ref`` it, where that is shorter.

    Only the text sent changes, never what a document must satisfy. Plain ``$ref`` to a definition is already how
    the node and material definitions are referenced, so no keyword outside ``API_SCHEMA_KEYWORDS`` is introduced.
    """
    defs = schema.get("$defs")
    if defs is None:
        return schema
    roots = [schema, *defs.values()]
    while True:
        seen: dict[str, list[tuple[Any, Any]]] = {}
        for root in roots:
            for container, key in _walk_slots(root):
                sub = container[key]
                if "$ref" not in sub:
                    seen.setdefault(_dumps(sub), []).append((container, key))
        best, gain, name = None, 0, ""
        for text, slots in seen.items():
            if len(slots) < 2:
                continue
            candidate = _shared_name(slots[0][0][slots[0][1]], set(defs))
            # exact bytes: each slot becomes a reference; the definition is written once, with its key and a comma
            ref = len(_dumps({"$ref": f"#/$defs/{candidate}"}))
            g = len(slots) * (len(text) - ref) - (len(text) + len(candidate) + 4)
            if g > gain:
                best, gain, name = text, g, candidate
        if best is None:
            return schema
        slots = seen[best]
        shape = slots[0][0][slots[0][1]]
        for container, key in slots:
            container[key] = {"$ref": f"#/$defs/{name}"}
        defs[name] = shape
        roots.append(shape)


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
    return {"Transform": "transform", "MaterialIR": "material", "RepeatRow": "row", "RepeatRing": "ring"}.get(name, name)


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
    "Scene IR: metres, Z-up, right-handed, rotations XYZ Euler degrees. Shapes use local coordinates; "
    "cylinder/cone/capsule axis is local Z (cone base at -height/2, apex at +height/2; capsule height is the cylinder "
    "part only, the caps come on top). box params.size is the full extent along x, y, z. A group is a transform-only "
    "parent; mesh_ref params.path is a local asset path. id fields match [A-Za-z_][A-Za-z0-9_]* (at most 64 chars) "
    "and parent / material_id / node_id / target_id must name an existing node / material. Materials are glTF 2.0 PBR: "
    f"{COLOUR_CLAUSE if told() else 'colours are linear RGB 0-1'}; {METAL_CLAUSE + '; ' if STATE_METALLIC else ''}"
    "opacity is the base colour's alpha; transmission is thin-surface (no thickness or "
    "volume); alpha_mode MASK needs alpha_cutoff and an albedo_uri whose alpha channel is the shape, "
    "BLEND needs opacity < 1 or an albedo_uri with alpha (with an albedo_uri leave opacity at 1); emissive_uri, "
    "emissive_strength need a non-black emissive_color; transmission (thin glass) needs metallic < 1; clearcoat_roughness "
    "needs clearcoat > 0; sheen_roughness needs sheen_color; double_sided for thin shells (leaves, cloth, decals)."
)
# What the shape parameters and the node shortcuts mean, for every geometry-editing request (the one-shot author through
# ``ir_rules_text``, the BLOCKOUT / TWEAK / VARIANTS stage agents through harness.agents); LOOKDEV, which edits
# materials only, does not carry it. The response schema has no descriptions (``_API_UNSENT``) and the tool schema
# drops the params' (``_lean_params``), so this is the one place a model reads them. The limits are the kernel's
# (kernel/compile_ir.py refuses a design outside them with a message): stating them here saves a failed candidate.
SHAPE_SEMANTICS = (
    "box bevel (m): round all 12 edges, below half min(size); cylinder bevel: both cap rims. bevel_segments "
    "1=chamfer, 2-4=rounded. inset_faces [\"+x\"...\"-z\"]: recessed panels; inset_margin>0 frame, inset_depth "
    "recess; adjacent faces require inset_depth<=bevel+inset_margin. lathe profile [[radius,z],...]: turn about Z, "
    "top to bottom; only endpoints may touch axis; off-axis ends capped. closed=true joins an off-axis loop "
    "(tube/ring). extrude outline [[x,y],...]: simple CCW XY polygon, depth centred on Z. sweep path [[x,y,z],...]: "
    "2-128 local centreline samples, circular radius, segments=12 (3-64); open, capped, no recentering/spline. "
    "Add samples for bends; 2 points join endpoints. No duplicates/reversals/self-contact; radius must fit bends. "
    "mirror x|y|z reflects node+children about parent origin plane; keep whole part on one side. mesh_ref/sweep "
    "cannot mirror (write other sweep path). repeat {count:n,offset:[dx,dy,dz]}: row; {count:n,around:x|y|z}: ring "
    "about parent origin; n=2..32 includes original. Repeat before mirror. IDs: id__r1, id__mx, id__r1__mx; "
    "child parent_copy__child."
)
# Sent with the authoring request only (``ir_rules_text``), not in IR_SEMANTICS, which every stage agent carries.
# The pair in it is from a live run (2026-11-21): the author wrote "tarnished military brass hardware" and the image
# model drew hinges and latches; "olive drab painted steel, scuffed and chipped" in the same run came back as a surface.
SURFACE_RULE = (
    f"; surface (<= {ir.SURFACE_MAX_CHARS} chars) prompts a tiling texture: the substance and its finish, not what is "
    'made of it ("tarnished brass", not "brass hardware"); never object parts, shapes or layout.'
)
OP_SEMANTICS = (
    "Anchors use world AABBs and place once, in world space. on_top_of centres X/Y and aligns bottom to target top. "
    "flush_back aligns +Y faces (back is +Y in this Z-up convention). "
    "For ground contact use z = half the primitive height."
)


@functools.cache
def ir_rules_text() -> str:
    """Prose description of the IR for model prompts, generated from the wire schema (fields AND types)."""
    scene, patch = raw_schema("scene"), raw_schema("patch")
    defs = scene["$defs"]
    lines = [IR_SEMANTICS, SHAPE_SEMANTICS, "Types below: ? marks an optional field, =x its default, [a,b,c] a fixed-length array.",
             "scene: " + _obj_line(scene, scene)]
    node_any = scene["properties"]["nodes"]["items"]
    alts = [_resolve(scene, alt)[1] for alt in node_any.get("oneOf", node_any.get("anyOf", []))]
    common = field_types(scene, alts[0])
    common.pop("kind"), common.pop("params")
    lines.append("node: {kind, params, " + ", ".join(f"{k}: {v}" for k, v in common.items()) + "}")
    lines.append("transform: " + _obj_line(scene, defs["Transform"]))
    lines.append("repeat: " + " or ".join(_obj_line(scene, defs[m.__name__]) for m in (RepeatRow, RepeatRing)))
    kinds = []
    for alt in alts:
        kind = alt["properties"]["kind"]["const"]
        params = alt["properties"]["params"]
        kinds.append(f"{kind} params {_obj_line(scene, params)}")
    lines.append("kind -> params: " + "; ".join(kinds))
    lines.append("material: " + _obj_line(scene, defs["MaterialIR"]) + SURFACE_RULE)
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
    if out.get("type") == "object" and "properties" in out:
        out.pop("description", None)  # a model's class docstring is developer documentation, not a parameter hint
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


def _lean_params(prop: dict[str, Any]) -> dict[str, Any]:
    """A shape parameter without its description: what it means is in SHAPE_SEMANTICS, which every stage that edits
    geometry carries in its system prompt (harness.agents), so the declaration would say it a second time per call."""
    return {k: v for k, v in prop.items() if k != "description"}


def _node_schema(root: dict[str, Any]) -> dict[str, Any]:
    alts = root["$defs"]["WireUpsertNode"]["properties"]["node"]["anyOf"]
    node = _merge(root, alts, "kind", "required fields per kind")
    params = [_resolve(root, _resolve(root, a)[1]["properties"]["params"])[1] for a in alts]
    merged: dict[str, list[dict[str, Any]]] = {}
    per_kind = []
    for kind, p in zip([_resolve(root, a)[1]["properties"]["kind"]["const"] for a in alts], params, strict=True):
        for name, prop in p.get("properties", {}).items():
            merged.setdefault(name, []).append(_lean_params(_lean(root, prop)))
        req = set(p.get("required", ()))
        fields = [f if f in req else f"{f}?={json.dumps(_whole(v['default']))}" if "default" in v else f"{f}?"
                  for f, v in p.get("properties", {}).items()]
        per_kind.append(f"{kind} {{{', '.join(fields)}}}")
    node["properties"]["kind"]["description"] = "params per kind: " + "; ".join(per_kind)
    node["properties"]["params"] = {"type": "object", "properties": {name: _union(variants) for name, variants in merged.items()},
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
    if tag == "upsert_node" and isinstance(op.get("node"), dict):
        node, errs = _parse_node(f"{where}.node", op["node"])
        if node is None:
            return None, errs
        op = {**op, "node": node}
    try:
        return model.model_validate(op), []
    except ValueError as e:
        return None, _explain(where, e)


def _parse_node(where: str, node: dict[str, Any]) -> tuple[BaseModel | None, list[str]]:
    """Validate a node against its own kind's model, so errors name that kind's fields only."""
    kind = node.get("kind")
    if kind not in NODE_BY_KIND:
        return None, [f"{where}.kind: one of {', '.join(NODE_BY_KIND)} (got {kind!r})"]
    params = ir.NODE_PARAMS[kind].model_fields
    if "params" not in node and any(f.is_required() for f in params.values()):
        need = ", ".join(n for n, f in params.items() if f.is_required())
        missing = "" if "transform" in node else f"; {where}({kind}).transform: Field required"
        return None, [f"{where}({kind}).params: Field required, {kind} needs {{{need}}}{missing}"]
    try:
        return NODE_BY_KIND[kind].model_validate(node), []
    except ValueError as e:
        return None, _explain(f"{where}({kind})", e)


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
    errors, parsed = [], []
    for i, n in enumerate(nodes if isinstance(nodes, list) else []):
        model, errs = _parse_node(f"nodes[{i}]", n) if isinstance(n, dict) else (None, [f"nodes[{i}]: expected an object"])
        parsed.append(model)
        errors += errs
    if errors:
        _raise(errors)
    payload = {"asset_name": asset_name, "nodes": parsed if isinstance(nodes, list) else nodes,
               "materials": materials if materials is not None else []}
    try:
        return scene_from_wire(WireScene.model_validate(payload))
    except ValueError as e:
        _raise(_explain("scene", e))
        raise  # unreachable


def apply_material_diff(current: ir.MaterialIR, diff: dict[str, Any]) -> ir.MaterialIR:
    """``current`` with the fields of ``diff`` (a tool's arguments: colours as a model writes them, converted once
    here) replaced, validated as ``MaterialIR`` (so new material fields need no change here); raises
    ``IRArgumentError`` naming the field."""
    try:
        bad = {k: v for k, v in diff.items() if told() and k in COLOUR_FIELDS and v is not None and not _is_written_colour(v)}
        if bad:
            raise ValueError(f"{', '.join(bad)}: {COLOUR_CLAUSE}")
        # under "as_written" the diff is taken as it always was (no "#rrggbb": the format refuses it, as on c8565a7)
        fields = ir_linear(diff, metallic=current.metallic) if told() else diff
        return ir.MaterialIR.model_validate({**current.model_dump(), **fields, "id": current.id})
    except ValueError as e:
        _raise(_explain("diff", e))
        raise  # unreachable
