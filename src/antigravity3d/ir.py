"""Declarative Scene/Asset IR.

The agent never writes bpy or USD directly: it emits a :class:`SceneIR` or a
list of :class:`IROp` patches, which the kernel compiles into a USD delta
layer. Units are metres, Z-up, right-handed; rotations are XYZ Euler degrees.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

Vec3 = tuple[float, float, float]
NodeId = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", max_length=64)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Transform(_Model):
    translate: Vec3 = (0.0, 0.0, 0.0)
    rotate_euler_deg: Vec3 = (0.0, 0.0, 0.0)
    scale: Vec3 = (1.0, 1.0, 1.0)


class MaterialIR(_Model):
    id: NodeId
    base_color: Vec3 = (0.8, 0.8, 0.8)
    metallic: float = Field(0.0, ge=0.0, le=1.0)
    roughness: float = Field(0.5, ge=0.0, le=1.0)
    albedo_uri: str | None = None
    orm_uri: str | None = None  # packed R=occlusion G=roughness B=metallic
    normal_uri: str | None = None
    normal_convention: Literal["opengl", "directx"] = "opengl"


class NodeIR(_Model):
    """One scene node. Primitives are centred on their local origin.

    ``params`` by kind:
      box       size: Vec3 (full extents)
      cylinder  radius, height, segments=24            (axis = local Z)
      cone      radius, height, segments=24            (base at -h/2, apex +h/2)
      sphere    radius, segments=24, rings=12
      capsule   radius, height (cylinder part), segments=16
      mesh_ref  path: str (glTF/OBJ/PLY/USD file to reference)
      group     no params; pure transform parent
    """

    id: NodeId
    kind: Literal["box", "cylinder", "cone", "sphere", "capsule", "mesh_ref", "group"]
    params: dict[str, float | int | str | Vec3] = Field(default_factory=dict)
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
    value: float | int | str | Vec3


class UpsertMaterial(_Model):
    op: Literal["upsert_material"] = "upsert_material"
    material: MaterialIR


class AssignMaterial(_Model):
    op: Literal["assign_material"] = "assign_material"
    node_id: NodeId
    material_id: NodeId


IROp = Annotated[
    Union[UpsertNode, RemoveNode, SetTransform, SetParam, UpsertMaterial, AssignMaterial],
    Field(discriminator="op"),
]


class IRPatch(_Model):
    ops: list[IROp] = Field(min_length=1)


def apply_patch(scene: SceneIR, patch: IRPatch) -> SceneIR:
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
            if isinstance(op, SetTransform):
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
