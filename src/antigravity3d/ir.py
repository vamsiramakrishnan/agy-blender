"""Declarative Scene/Asset IR.

The agent never writes bpy or USD directly: it emits a :class:`SceneIR` or a
list of :class:`IROp` patches, which the kernel compiles into a USD delta
layer. Units are metres, Z-up, right-handed; rotations are XYZ Euler degrees.
"""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Callable
from typing import Annotated, Literal, Union

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, model_validator

Vec3 = tuple[float, float, float]
Vec2 = tuple[float, float]
NodeId = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", max_length=64)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Transform(_Model):
    translate: Vec3 = (0.0, 0.0, 0.0)
    rotate_euler_deg: Vec3 = (0.0, 0.0, 0.0)
    scale: Vec3 = (1.0, 1.0, 1.0)


UnitFloat = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]


def _unit_rgb(v: tuple[float, float, float]) -> tuple[float, float, float]:
    if not all(math.isfinite(c) and 0.0 <= c <= 1.0 for c in v):
        raise ValueError(f"colour components must be within [0, 1], got {v}")
    return v


Color3 = Annotated[tuple[float, float, float], AfterValidator(_unit_rgb)]  # linear RGB, each 0..1
AlphaMode = Literal["OPAQUE", "MASK", "BLEND"]  # glTF names: opaque, cut-out at alpha_cutoff, blended

SURFACE_MAX_CHARS = 120


def clean_surface(value: object) -> object:
    """Model-written words that end up in an image prompt: control and format characters (newlines, zero-width,
    bidi overrides) become spaces, whitespace collapses, and the text is cut at a word boundary to
    ``SURFACE_MAX_CHARS``. Cleaned rather than rejected: a rejection would discard a whole authored scene for a
    prompt fragment, and nothing but the texture prompt reads this text."""
    if not isinstance(value, str):
        return value  # the str annotation rejects it
    text = " ".join("".join(" " if unicodedata.category(c) in ("Cc", "Cf") else c for c in value).split())
    if len(text) > SURFACE_MAX_CHARS:
        cut = text[: SURFACE_MAX_CHARS + 1]
        words = cut.rsplit(" ", 1)[0] if " " in cut else ""  # up to the last whole word, unless that drops most of it
        text = (words if len(words) >= SURFACE_MAX_CHARS // 2 else cut[:SURFACE_MAX_CHARS]).rstrip(" ,;:.")
    return text


class MaterialIR(_Model):
    """A glTF 2.0 metallic-roughness material plus the ratified ``KHR_materials_*`` parameters game assets need.

    Names are glTF's, in snake_case, except ``opacity`` (``baseColorFactor[3]``, UsdPreviewSurface's own name) and
    there is no thickness/volume (transmission is thin-surface, no ``KHR_materials_volume``). Every new field
    defaults to the glTF default, so a document written before they existed validates unchanged. The cross-field
    rules reject input that would silently do nothing or contradict itself (see ``_coherent``).
    """

    id: NodeId
    # What the surface is, in a few words ("olive drab painted steel, scuffed and chipped"): the subject of the texture
    # prompt (genmedia.material_prompt). Empty is left out of every dump, so documents without it serialise unchanged.
    surface: Annotated[str, BeforeValidator(clean_surface), Field(max_length=SURFACE_MAX_CHARS)] = Field(
        "", exclude_if=lambda v: v == "")
    base_color: Vec3 = (0.8, 0.8, 0.8)
    metallic: float = Field(0.0, ge=0.0, le=1.0)
    roughness: float = Field(0.5, ge=0.0, le=1.0)
    albedo_uri: str | None = None  # RGB base colour; its alpha channel is the alpha when alpha_mode is MASK or BLEND
    orm_uri: str | None = None  # packed R=occlusion G=roughness B=metallic
    normal_uri: str | None = None
    normal_convention: Literal["opengl", "directx"] = "opengl"
    # alpha: OPAQUE ignores alpha; MASK keeps pixels with alpha >= alpha_cutoff (needs albedo_uri); BLEND mixes
    alpha_mode: AlphaMode = "OPAQUE"
    alpha_cutoff: float | None = Field(None, gt=0.0, le=1.0)  # required with MASK, forbidden otherwise
    opacity: UnitFloat = 1.0  # constant alpha; with an albedo map the map's alpha channel is the alpha instead
    double_sided: bool = False  # render back faces (thin shells: leaves, cloth, decals); stored on the geometry in USD
    emissive_color: Color3 = (0.0, 0.0, 0.0)  # linear; multiplies emissive_uri when one is set
    emissive_strength: float = Field(1.0, ge=0.0, allow_inf_nan=False)  # KHR_materials_emissive_strength
    emissive_uri: str | None = None  # sRGB map; needs a non-black emissive_color
    clearcoat: UnitFloat = 0.0  # KHR_materials_clearcoat
    clearcoat_roughness: UnitFloat = 0.0
    transmission: UnitFloat = 0.0  # KHR_materials_transmission: share of light passing through (thin glass)
    ior: float = Field(1.5, ge=1.0, le=3.0, allow_inf_nan=False)  # KHR_materials_ior
    sheen_color: Color3 = (0.0, 0.0, 0.0)  # KHR_materials_sheen (cloth)
    sheen_roughness: UnitFloat = 0.0
    specular: UnitFloat = 1.0  # KHR_materials_specular
    specular_color: Color3 = (1.0, 1.0, 1.0)

    @model_validator(mode="after")
    def _coherent(self) -> MaterialIR:
        def bad(why: str) -> ValueError:
            return ValueError(f"material {self.id}: {why}")

        black = (0.0, 0.0, 0.0)
        if self.alpha_mode == "MASK":
            if self.alpha_cutoff is None:
                raise bad("alpha_mode MASK needs alpha_cutoff in (0, 1] (pixels with alpha below it are discarded)")
            if not self.albedo_uri:
                raise bad("alpha_mode MASK needs albedo_uri: the map's alpha channel is the cut-out shape")
        elif self.alpha_cutoff is not None:
            raise bad("alpha_cutoff applies only to alpha_mode MASK; remove it or set alpha_mode MASK")
        if self.alpha_mode == "OPAQUE" and self.opacity < 1.0:
            raise bad(f"opacity {self.opacity} < 1 has no effect with alpha_mode OPAQUE; set alpha_mode BLEND")
        if self.alpha_mode != "OPAQUE" and self.albedo_uri and self.opacity != 1.0:
            raise bad("with albedo_uri the map's alpha channel is the opacity: leave opacity at 1 and bake the fade into the map")
        if self.alpha_mode == "BLEND" and not self.albedo_uri and self.opacity == 1.0:
            raise bad("alpha_mode BLEND with opacity 1 and no albedo_uri blends nothing; set opacity < 1 or use OPAQUE")
        if self.clearcoat_roughness > 0.0 and self.clearcoat == 0.0:
            raise bad("clearcoat_roughness has no effect while clearcoat is 0")
        if self.transmission > 0.0 and self.metallic == 1.0 and not self.orm_uri:
            raise bad("transmission > 0 on a fully metallic surface (metallic 1) transmits nothing; lower metallic (glass is 0)")
        if self.sheen_roughness > 0.0 and self.sheen_color == black:
            raise bad("sheen_roughness has no effect while sheen_color is black")
        if self.emissive_strength != 1.0 and self.emissive_color == black:
            raise bad("emissive_strength has no effect while emissive_color is black")
        if self.emissive_uri and self.emissive_color == black:
            raise bad("emissive_uri needs a non-black emissive_color (it multiplies the map; use [1,1,1] for the map as is)")
        if self.specular == 0.0 and self.specular_color != (1.0, 1.0, 1.0):
            raise bad("specular_color has no effect while specular is 0")
        return self


PositiveFloat = Annotated[float, Field(gt=0, allow_inf_nan=False)]
NonNegativeFloat = Annotated[float, Field(ge=0, allow_inf_nan=False)]


BoxFace = Literal["+x", "-x", "+y", "-y", "+z", "-z"]


class BoxParams(_Model):
    size: tuple[PositiveFloat, PositiveFloat, PositiveFloat] = Field(description="full extents x, y, z")
    bevel: NonNegativeFloat = Field(0.0, description="radius, in metres, of the rounding on all twelve edges; 0 keeps them sharp")
    bevel_segments: int = Field(1, ge=1, le=4, description="1 = a flat chamfer; 2 to 4 = a rounded edge")
    inset_faces: list[BoxFace] = Field(default_factory=list, description="faces that carry a recessed panel")
    inset_margin: NonNegativeFloat = Field(0.0, description="width, in metres, of the frame left around each recessed panel")
    inset_depth: NonNegativeFloat = Field(0.0, description="depth, in metres, of each recessed panel below its face")


class CylinderParams(_Model):
    radius: PositiveFloat
    height: PositiveFloat
    segments: int = Field(24, ge=3)
    bevel: NonNegativeFloat = Field(0.0, description="radius, in metres, of the rounding on both cap rims; 0 keeps them sharp")
    bevel_segments: int = Field(1, ge=1, le=4, description="1 = a flat chamfer; 2 to 4 = a rounded rim")


class ConeParams(_Model):
    radius: PositiveFloat = Field(description="base radius; base at -height/2, apex at +height/2")
    height: PositiveFloat
    segments: int = Field(24, ge=3)


class SphereParams(_Model):
    radius: PositiveFloat
    segments: int = Field(24, ge=3)
    rings: int = Field(12, ge=2)


class CapsuleParams(_Model):
    radius: PositiveFloat
    height: NonNegativeFloat = Field(description="length of the cylinder part, caps excluded")
    segments: int = Field(16, ge=3)


class LatheParams(_Model):
    """A profile turned about local Z: barrels, bottles, lamps, wheels, knobs; with ``closed``, tubes and rings."""

    profile: list[Vec2] = Field(min_length=2, max_length=32, description="(radius, z) points along the outline, top to bottom; radius >= 0")
    segments: int = Field(16, ge=3)
    closed: bool = Field(False, description="the profile is a closed loop that never touches the axis: a tube, pipe or ring")


class ExtrudeParams(_Model):
    """A flat outline pushed along local Z: brackets, arches, wedges, ramps, blades, L, T and U sections."""

    outline: list[Vec2] = Field(min_length=3, max_length=32, description="(x, y) corners of a simple polygon, counter-clockwise")
    depth: PositiveFloat = Field(description="extent along local Z, centred on the origin")


class MeshRefParams(_Model):
    path: str = Field(min_length=1, description="glTF/OBJ/PLY/USD file to reference")


class GroupParams(_Model):
    """No params: a pure transform parent."""


# Typed ``NodeIR.params`` per kind: what the kernel (``kernel/compile_ir.py``) reads. ``NodeIR.params`` itself stays a
# free-form dict for backward compatibility; the model-facing schema (``antigravity3d.ir_schema``) is built from this.
NODE_PARAMS: dict[str, type[_Model]] = {
    "box": BoxParams, "cylinder": CylinderParams, "cone": ConeParams, "sphere": SphereParams,
    "capsule": CapsuleParams, "lathe": LatheParams, "extrude": ExtrudeParams, "mesh_ref": MeshRefParams, "group": GroupParams,
}

# What one entry of ``NodeIR.params`` may hold: a number, a flag, a name, a vector, a list of face names
# (``inset_faces``) or a list of 2D points (``profile``, ``outline``).
ParamValue = Union[bool, int, float, str, Vec3, list[Vec2], list[str]]


class NodeIR(_Model):
    """One scene node. Primitives are centred on their local origin.

    ``params`` by kind:
      box       size: Vec3 (full extents); bevel=0, bevel_segments=1 (all twelve edges rounded);
                inset_faces=[], inset_margin=0, inset_depth=0 (a recessed panel on each listed face)
      cylinder  radius, height, segments=24            (axis = local Z); bevel=0, bevel_segments=1 (both cap rims)
      cone      radius, height, segments=24            (base at -h/2, apex +h/2)
      sphere    radius, segments=24, rings=12
      capsule   radius, height (cylinder part), segments=16
      lathe     profile: [(radius, z), ...] turned about local Z, segments=16, closed=False (True: a tube or ring)
      extrude   outline: [(x, y), ...] counter-clockwise in local XY, depth along local Z (centred)
      mesh_ref  path: str (glTF/OBJ/PLY/USD file to reference)
      group     no params; pure transform parent
    """

    id: NodeId
    kind: Literal["box", "cylinder", "cone", "sphere", "capsule", "lathe", "extrude", "mesh_ref", "group"]
    params: dict[str, ParamValue] = Field(default_factory=dict)
    transform: Transform = Field(default_factory=Transform)
    parent: NodeId | None = None
    material_id: NodeId | None = None
    collider: bool = True  # include in collision hull generation


class SceneIR(_Model):
    asset_name: NodeId
    nodes: list[NodeIR] = Field(default_factory=list)
    materials: list[MaterialIR] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_references(self) -> "SceneIR":
        node_ids = [n.id for n in self.nodes]
        if len(set(node_ids)) != len(node_ids):
            raise ValueError("duplicate node ids")
        mat_ids = {m.id for m in self.materials}
        known = set(node_ids)
        for n in self.nodes:
            if n.parent is not None and n.parent not in known:
                raise ValueError(f"{n.id}: unknown parent {n.parent}")
            if n.material_id is not None and n.material_id not in mat_ids:
                raise ValueError(f"{n.id}: unknown material {n.material_id}")
        parents = {n.id: n.parent for n in self.nodes}
        checked = set()
        for start in parents:
            seen, cur = set(), start
            while cur is not None and cur not in checked:
                if cur in seen:
                    raise ValueError(f"{start}: parent cycle")
                seen.add(cur)
                cur = parents[cur]
            checked.update(seen)
        return self


# --- Patch ops: the unit of one transactional edit ---------------------------


class UpsertNode(_Model):
    op: Literal["upsert_node"] = "upsert_node"
    node: NodeIR


class RemoveNode(_Model):
    op: Literal["remove_node"] = "remove_node"
    node_id: NodeId


class SetTransform(_Model):
    op: Literal["set_transform"] = "set_transform"
    node_id: NodeId
    transform: Transform


class SetParam(_Model):
    op: Literal["set_param"] = "set_param"
    node_id: NodeId
    name: str
    value: ParamValue  # what NodeIR.params holds: a profile, an outline or inset_faces is set whole, a flag stays a bool


class UpsertMaterial(_Model):
    op: Literal["upsert_material"] = "upsert_material"
    material: MaterialIR


class AssignMaterial(_Model):
    op: Literal["assign_material"] = "assign_material"
    node_id: NodeId
    material_id: NodeId


class PlaceRelative(_Model):
    """One-shot placement in world space. Back is the +Y face in this Z-up convention."""

    op: Literal["on_top_of", "flush_back"]
    node_id: NodeId
    target_id: NodeId
    gap_m: float = Field(0.0, ge=0, allow_inf_nan=False)


IROp = Annotated[
    Union[UpsertNode, RemoveNode, SetTransform, SetParam, UpsertMaterial, AssignMaterial, PlaceRelative],
    Field(discriminator="op"),
]


class SceneIRDiff(_Model):
    ops: list[IROp] = Field(min_length=1)


IRPatch = SceneIRDiff  # existing clients import IRPatch


def apply_patch(
    scene: SceneIR,
    patch: SceneIRDiff,
    *,
    anchor_resolver: Callable[[SceneIR, PlaceRelative], Transform] | None = None,
) -> SceneIR:
    """Returns a new validated SceneIR with ``patch`` applied (pure function)."""
    nodes = {n.id: n for n in scene.nodes}
    mats = {m.id: m for m in scene.materials}
    for op in patch.ops:
        if isinstance(op, UpsertNode):
            nodes[op.node.id] = op.node
        elif isinstance(op, UpsertMaterial):
            mats[op.material.id] = op.material
        elif isinstance(op, RemoveNode):
            if op.node_id not in nodes:
                raise KeyError(f"remove_node: unknown node {op.node_id}")
            del nodes[op.node_id]
            if any(n.parent == op.node_id for n in nodes.values()):
                raise ValueError(f"remove_node: {op.node_id} still has children")
        else:
            if op.node_id not in nodes:
                raise KeyError(f"{op.op}: unknown node {op.node_id}")
            node = nodes[op.node_id]
            if isinstance(op, PlaceRelative):
                from antigravity3d.kernel.compile_ir import resolve_anchor

                current = SceneIR(asset_name=scene.asset_name, nodes=list(nodes.values()), materials=list(mats.values()))
                transform = (anchor_resolver or resolve_anchor)(current, op)
                nodes[op.node_id] = node.model_copy(update={"transform": transform})
            elif isinstance(op, SetTransform):
                nodes[op.node_id] = node.model_copy(update={"transform": op.transform})
            elif isinstance(op, SetParam):
                nodes[op.node_id] = node.model_copy(
                    update={"params": {**node.params, op.name: op.value}}
                )
            else:
                nodes[op.node_id] = node.model_copy(update={"material_id": op.material_id})
    return SceneIR.model_validate(
        {
            "asset_name": scene.asset_name,
            "nodes": [n.model_dump() for n in nodes.values()],
            "materials": [m.model_dump() for m in mats.values()],
        }
    )
