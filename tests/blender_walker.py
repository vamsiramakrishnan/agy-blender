"""Runs INSIDE Blender (not a test module): checks the linter's removed-name tables against the installed Blender.

    blender -b --factory-startup --python tests/blender_walker.py -- RULES.json

It reads the rules the linter exports (``describe_rules()``), checks each one and prints one ``WALKER {json}`` line:
``{"checked": n, "failures": [...]}``. A rule fails when the old name still exists (so the linter would flag
something that works) or when a replacement its hint names does not exist. Not imported by pytest and not run
through ``run_script`` (its tables contain the very names the linter rejects).
"""

import json
import sys

import bpy

rules = json.load(open(sys.argv[sys.argv.index("--") + 1]))
failures: list[str] = []
checked = 0


def fail(rule: dict, why: str) -> None:
    failures.append(f"{rule['code']} {rule['kind']} {rule['old']!r}: {why}")


STRUCTS = {n: getattr(bpy.types, n) for n in dir(bpy.types) if hasattr(getattr(bpy.types, n, None), "bl_rna")}


def struct_has(struct: str, name: str) -> bool:
    cls = STRUCTS[struct]
    return name in cls.bl_rna.properties or name in cls.bl_rna.functions


def any_struct_has(name: str) -> list[str]:
    return [n for n, c in STRUCTS.items() if name in c.bl_rna.properties or name in c.bl_rna.functions]


bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.compositing_node_group = bpy.data.node_groups.new("walker_comp", "CompositorNodeTree")
comp = scene.compositing_node_group
material = bpy.data.materials.new("walker_mat")
geo = bpy.data.node_groups.new("walker_geo", "GeometryNodeTree")
world = bpy.data.worlds.new("walker_world")
mesh_obj = bpy.data.objects.new("walker_obj", bpy.data.meshes.new("walker_mesh"))
scene.collection.objects.link(mesh_obj)


def check_new(rule: dict, spec: str) -> None:
    kind, _, rest = spec.partition(":")
    ok = False
    if kind == "rna":
        struct, _, name = rest.partition(".")
        ok = struct in STRUCTS and struct_has(struct, name)
    elif kind == "op":
        mod, _, name = rest.partition(".")
        ok = hasattr(getattr(bpy.ops, mod), name)
    elif kind == "data":
        ok = hasattr(bpy.data, rest)
    elif kind == "type":
        ok = hasattr(bpy.types, rest)
    elif kind == "node":
        try:
            comp.nodes.new(rest)
            ok = True
        except RuntimeError:
            ok = False
    elif kind == "opkw":
        mod, op, kw = rest.split(".")
        ok = kw in getattr(getattr(bpy.ops, mod), op).get_rna_type().properties
    elif kind in ("socket", "enum"):
        ok = True  # checked by the rule's own branch below
    else:
        fail(rule, f"unknown check {spec!r}")
        return
    if not ok:
        fail(rule, f"replacement {spec!r} does not exist")


def media_types():
    return [i.identifier for i in bpy.types.ImageFormatSettings.bl_rna.properties["media_type"].enum_items]


def enum_assign(owner: str, attr: str, value: str) -> bool:
    """True if ``value`` can be assigned to the enum in some state of a fresh object of the owner type."""
    if attr == "sky_type":
        node = world.node_tree.nodes.new("ShaderNodeTexSky")
        target = node
    elif attr == "engine":
        target = scene.render
    elif attr == "solver":
        target = mesh_obj.modifiers.new("walker_bool", "BOOLEAN")
    elif attr == "file_format":
        target = scene.render.image_settings
    else:
        raise AssertionError(attr)
    states = media_types() if attr == "file_format" else [None]
    for state in states:
        if state:
            target.media_type = state
        try:
            setattr(target, attr, value)
            return True
        except TypeError:
            continue
    return False


for rule in rules:
    checked += 1
    kind, old, owner, new = rule["kind"], rule["old"], rule["owner"], rule["new"]
    for spec in new:
        check_new(rule, spec)
    if kind == "attr":
        if hits := any_struct_has(old):
            fail(rule, f"still exists on {hits[:4]}")
    elif kind == "data":
        if hasattr(bpy.data, old):
            fail(rule, "still exists in bpy.data")
    elif kind == "type":
        if hasattr(bpy.types, old):
            fail(rule, "still exists in bpy.types")
    elif kind == "tool_attr" or kind == "scene_attr" or kind == "action_attr" or kind == "ng_sockets":
        if struct_has(owner, old):
            fail(rule, f"still exists on {owner}")
    elif kind == "socket_in":
        node = geo.nodes.new(owner) if owner.startswith("FunctionNode") else material.node_tree.nodes.new(owner)
        types = [None]
        if owner == "FunctionNodeCompare":
            types = [(t, m) for t in ("FLOAT", "INT", "VECTOR", "RGBA", "STRING") for m in (("ELEMENT", "LENGTH", "DOT_PRODUCT", "DIRECTION", "AVERAGE") if t == "VECTOR" else (None,))]
        elif owner == "FunctionNodeRandomValue":
            types = [(t, None) for t in ("FLOAT", "INT", "FLOAT_VECTOR", "BOOLEAN")]
        for t in types:
            if t:
                node.data_type = t[0]
                if t[1]:
                    node.mode = t[1]
            if node.inputs.get(old) is not None:
                fail(rule, f"{owner}{' ' + str(t) if t else ''} still has an input {old!r}")
        want = new[0].partition(":")[2]
        found = False
        for t in types:
            if t:
                node.data_type = t[0]
                if t[1]:
                    node.mode = t[1]
            found = found or node.inputs.get(want) is not None
        if not found:
            fail(rule, f"{owner} has no input {want!r}")
    elif kind == "rl_out":
        want = new[0].partition(":")[2]
        seen_old, seen_new = False, False
        for engine in ("CYCLES", "BLENDER_EEVEE"):
            scene.render.engine = engine
            vl = scene.view_layers[0]
            for src in (vl, vl.cycles, vl.eevee):
                for p in src.bl_rna.properties:
                    if p.identifier.startswith("use_pass") and p.type == "BOOLEAN" and not p.is_readonly:
                        try:
                            setattr(src, p.identifier, True)
                        except Exception:
                            pass
            comp.nodes.clear()
            rl = comp.nodes.new("CompositorNodeRLayers")
            vl.use_pass_z = False
            vl.use_pass_z = True  # make the node rebuild its sockets
            bpy.context.view_layer.update()
            seen_old = seen_old or rl.outputs.get(old) is not None
            seen_new = seen_new or rl.outputs.get(want) is not None
        if seen_old:
            fail(rule, "a Render Layers node still has this socket")
        if not seen_new:
            fail(rule, f"a Render Layers node has no {want!r} socket")
    elif kind == "enum":
        if enum_assign(owner, owner, old):
            fail(rule, f"{old!r} is still accepted")
        for spec in new:
            if spec.startswith("enum:"):
                path, _, value = spec[5:].partition("=")
                if not enum_assign(path.split(".")[0], path.split(".")[1], value):
                    fail(rule, f"replacement value {value!r} is not accepted")
    elif kind == "node_type":
        for tree, label in ((comp, "compositor"), (geo, "geometry"), (material.node_tree, "shader")):
            try:
                tree.nodes.new(old)
                fail(rule, f"nodes.new({old!r}) still works in a {label} tree")
            except RuntimeError:
                pass
    elif kind == "opkw":
        mod, op = owner.split(".")
        if old in getattr(getattr(bpy.ops, mod), op).get_rna_type().properties:
            fail(rule, f"{owner} still has the keyword {old!r}")
    else:
        fail(rule, "unknown rule kind")

print("WALKER " + json.dumps({"checked": checked, "failures": failures, "blender": bpy.app.version_string}))
