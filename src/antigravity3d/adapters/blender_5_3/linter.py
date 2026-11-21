"""AST compatibility + safety gate for scripts destined for ``run_script``.

IMPORTANT: this is a guard-rail against accidents and naive prompt-injected
code, NOT a security boundary. Python cannot be sandboxed by static analysis;
a determined author can find escapes this does not model. In production the
daemon must additionally run under OS-level sandboxing (seccomp/namespaces,
read-only filesystem, no network, dropped capabilities).

Verified here: everything in this module (pure Python, ``ast`` only). The removed-name
tables below (``RULES``) were each checked against Blender 5.3.0 and are re-checked, in
whatever Blender is installed, by tests/test_blender_api_53.py::test_linter_tables_match_blender;
a rule is an error only when the old name is gone or raises there. Names that still work in 5.3
(``Material.blend_method``, ``Strip.frame_final_*``, ``bpy.ops.pose.convert_rotation``,
``bpy.ops.anim.convert_legacy_action``) are deliberately not listed. The idea of a cross-check
of the 5.x API changes comes from the table in EL4CTEO/blender-skills (MIT),
skills/blender-core/references/api-changes-5x.md at commit 9e7e04a6; this project's rules and
messages are its own and every one was confirmed by running 5.3.0, which also showed four of
that table's rows no longer hold.

Codes: AG000 syntax error; AG001 module not allowlisted; AG002 dynamic import;
AG003 eval/exec/compile & friends; AG004 open() for writing; AG005 reflection
with dynamic/dangerous name; AG006 blocked attribute chain; AG007 dunder
access; AG008 ``bpy.app.handlers`` use; AG010 bgl; AG011 BLENDER_EEVEE_NEXT;
AG012 boolean solver FAST; AG013 AssetHandle; AG014 Image.bindcode;
AG015 removed attribute, data or type name; AG016 Geometry Nodes modifier input by key;
AG017 removed socket name on ``inputs``; AG018 old render pass name on a Render Layers
``outputs``; AG019 removed enum value; AG020 removed node type; AG021 removed operator
keyword; AG022 ``file_format`` before ``media_type``; AG023 ``gpu`` call without ``gpu.init()``;
AG024 ``inputs.new``/``outputs.new`` on a node group; AG025 ``scene.node_tree``; AG026 legacy
Action ``fcurves``/``groups``; AG030 (warning) unbounded loop.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import NamedTuple

ALLOWED_MODULES = frozenset({
    "bpy", "bmesh", "mathutils", "bpy_extras", "gpu", "gpu_extras", "bl_math", "idprop",
    "math", "random", "json", "numpy", "colorsys", "itertools", "functools", "collections",
    "dataclasses", "typing", "re", "time", "enum", "string", "statistics", "copy",
    "fractions", "operator", "heapq", "bisect", "__future__",
})

BLOCKED_ATTR_PREFIXES = (
    "bpy.ops.wm.quit_blender", "bpy.ops.wm.read_factory_settings", "bpy.ops.wm.read_homefile",
    "bpy.ops.wm.save_mainfile", "bpy.ops.wm.save_as_mainfile", "bpy.ops.wm.open_mainfile",
    "bpy.ops.wm.url_open", "bpy.ops.wm.path_open", "bpy.ops.script", "bpy.utils.execfile",
    "bpy.ops.preferences.addon_install", "bpy.ops.wm.save_userpref",
)
HANDLERS = "bpy.app.handlers"
DANGEROUS_BUILTINS = frozenset({"eval", "exec", "compile", "globals", "locals", "vars", "breakpoint", "input"})
REFLECTION = frozenset({"getattr", "setattr", "delattr"})
DUNDER_OK = frozenset({"__name__", "__doc__", "__file__"})
BLOCKED_MODULE_ROOTS = frozenset({"os", "subprocess", "shutil", "socket", "ctypes", "sys", "importlib",
                                  "pickle", "multiprocessing", "builtins", "pathlib", "io", "inspect"})
WRITE_MODE_CHARS = frozenset("wax+")
READ_MODE_CHARS = frozenset("rbtU")

# Deprecated 5.3 symbols -> fix hint (one line each to correct).
EEVEE_OLD, EEVEE_NEW = "BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"
SOLVER_OLD, SOLVER_NEW = "FAST", "FLOAT"
BGL_HINT = "bgl was removed; use the `gpu` module: gpu.init() first, then e.g. gpu.texture.from_image(image)"
ASSET_HANDLE_HINT = "AssetHandle was removed; use AssetRepresentation"
BINDCODE_HINT = "Image.bindcode was removed; call gpu.init() first, then gpu.texture.from_image(image)"


class Rule(NamedTuple):
    """One removed name. ``new`` are existence checks for the replacement the hint names
    (``rna:Struct.prop``, ``op:module.name``, ``data:name``, ``type:Name``, ``enum:Struct.prop=VALUE``,
    ``node:IdName``, ``opkw:module.op.kw``); the last dotted component of each is a word the hint must contain."""
    code: str
    kind: str
    old: str
    new: tuple[str, ...]
    hint: str
    owner: str = ""


RULES: tuple[Rule, ...] = (
    # AG015: attribute names that no RNA struct has any more (the test checks every struct)
    Rule("AG015", "attr", "use_auto_smooth", ("op:object.shade_smooth_by_angle",), "Mesh.use_auto_smooth was removed in 4.1; use bpy.ops.object.shade_smooth_by_angle (Smooth by Angle)"),
    Rule("AG015", "attr", "auto_smooth_angle", ("op:object.shade_smooth_by_angle",), "Mesh.auto_smooth_angle was removed in 4.1; use bpy.ops.object.shade_smooth_by_angle(angle=...)"),
    Rule("AG015", "attr", "calc_normals_split", ("rna:Mesh.corner_normals",), "Mesh.calc_normals_split was removed in 4.1; read Mesh.corner_normals"),
    Rule("AG015", "attr", "face_maps", ("rna:Mesh.attributes",), "face maps were removed in 4.0; store a boolean face attribute with mesh.attributes.new(name, 'BOOLEAN', 'FACE')"),
    Rule("AG015", "attr", "shadow_method", ("rna:Material.surface_render_method",), "Material.shadow_method was removed in 4.2; use Material.surface_render_method"),
    Rule("AG015", "attr", "use_gtao", ("rna:ViewLayerEEVEE.ambient_occlusion_distance",), "scene.eevee.use_gtao was removed in 5.0; ambient occlusion has no switch, use view_layer.eevee.ambient_occlusion_distance"),
    Rule("AG015", "attr", "gtao_distance", ("rna:ViewLayerEEVEE.ambient_occlusion_distance",), "scene.eevee.gtao_distance was removed in 5.0; use view_layer.eevee.ambient_occlusion_distance"),
    Rule("AG015", "attr", "sculpt_tool", ("rna:Brush.sculpt_brush_type",), "Brush.sculpt_tool was removed in 5.0; use Brush.sculpt_brush_type"),
    Rule("AG015", "attr", "use_airbrush", ("rna:Brush.stroke_method",), "Brush.use_airbrush was removed in 5.1; use Brush.stroke_method"),
    Rule("AG015", "attr", "use_anchor", ("rna:Brush.stroke_method",), "Brush.use_anchor was removed in 5.1; use Brush.stroke_method"),
    Rule("AG015", "attr", "use_space", ("rna:Brush.stroke_method",), "Brush.use_space was removed in 5.1; use Brush.stroke_method"),
    Rule("AG015", "attr", "base_path", ("rna:CompositorNodeOutputFile.directory",), "File Output node base_path was removed in 5.0; use the node's directory (with file_name and file_output_items)"),
    Rule("AG015", "attr", "file_slots", ("rna:CompositorNodeOutputFile.file_output_items",), "File Output node file_slots was removed in 5.0; use file_output_items"),
    Rule("AG015", "attr", "layer_slots", ("rna:CompositorNodeOutputFile.file_output_items",), "File Output node layer_slots was removed in 5.0; use file_output_items"),
    Rule("AG015", "attr", "id_root", ("rna:ActionSlot.target_id_type",), "Action.id_root was removed in 5.0 (slotted actions); use ActionSlot.target_id_type"),
    Rule("AG015", "data", "grease_pencils_v3", ("data:grease_pencils",), "bpy.data.grease_pencils_v3 was renamed in 5.0; use bpy.data.grease_pencils"),
    Rule("AG015", "type", "GreasePencilv3", ("type:GreasePencil",), "bpy.types.GreasePencilv3 was renamed in 5.0; use bpy.types.GreasePencil"),
    Rule("AG015", "type", "GPencilStroke", ("type:AnnotationStroke",), "bpy.types.GPencilStroke was removed in 5.0; annotation strokes are bpy.types.AnnotationStroke"),
    Rule("AG015", "tool_attr", "unified_paint_settings", ("rna:Sculpt.unified_paint_settings",), "tool_settings.unified_paint_settings was removed in 5.0; use tool_settings.sculpt.unified_paint_settings", "ToolSettings"),
    # AG017: socket names removed from Principled BSDF (only when the node is recognisably a Principled BSDF) ...
    Rule("AG017", "socket_in", "Subsurface", ("socket:Subsurface Weight",), "Principled BSDF input 'Subsurface' was renamed in 4.0; use 'Subsurface Weight'", "ShaderNodeBsdfPrincipled"),
    Rule("AG017", "socket_in", "Specular", ("socket:Specular IOR Level",), "Principled BSDF input 'Specular' was renamed in 4.0; use 'Specular IOR Level'", "ShaderNodeBsdfPrincipled"),
    Rule("AG017", "socket_in", "Transmission", ("socket:Transmission Weight",), "Principled BSDF input 'Transmission' was renamed in 4.0; use 'Transmission Weight'", "ShaderNodeBsdfPrincipled"),
    Rule("AG017", "socket_in", "Clearcoat", ("socket:Coat Weight",), "Principled BSDF input 'Clearcoat' was renamed in 4.0; use 'Coat Weight'", "ShaderNodeBsdfPrincipled"),
    Rule("AG017", "socket_in", "Clearcoat Roughness", ("socket:Coat Roughness",), "Principled BSDF input 'Clearcoat Roughness' was renamed in 4.0; use 'Coat Roughness'", "ShaderNodeBsdfPrincipled"),
    Rule("AG017", "socket_in", "Sheen", ("socket:Sheen Weight",), "Principled BSDF input 'Sheen' was renamed in 4.0; use 'Sheen Weight'", "ShaderNodeBsdfPrincipled"),
    Rule("AG017", "socket_in", "Emission", ("socket:Emission Color",), "Principled BSDF input 'Emission' was renamed in 4.0; use 'Emission Color'", "ShaderNodeBsdfPrincipled"),
    # ... and the per-type duplicate identifiers of Compare and Random Value (one socket per name since 5.2)
    *(Rule("AG017", "socket_in", old, (f"socket:{new}",), f"{node_label} socket '{old}' no longer exists (5.2: one socket per name, typed by data_type); use '{new}'", node)
      for node, node_label, pairs in (
          ("FunctionNodeCompare", "Compare", (("A_INT", "A"), ("B_INT", "B"), ("A_VEC3", "A"), ("B_VEC3", "B"), ("A_COL", "A"), ("B_COL", "B"), ("A_STR", "A"), ("B_STR", "B"))),
          ("FunctionNodeRandomValue", "Random Value", (("Min_001", "Min"), ("Max_001", "Max"), ("Min_002", "Min"), ("Max_002", "Max"))))
      for old, new in pairs),
    # AG018: render pass sockets of a Render Layers node (names before 5.0 -> names now)
    *(Rule("AG018", "rl_out", old, (f"socket:{new}",), f"render pass '{old}' is called '{new}' since 5.0", "CompositorNodeRLayers")
      for old, new in (("Z", "Depth"), ("DiffCol", "Diffuse Color"), ("DiffDir", "Diffuse Direct"), ("DiffInd", "Diffuse Indirect"),
                       ("GlossCol", "Glossy Color"), ("GlossDir", "Glossy Direct"), ("GlossInd", "Glossy Indirect"),
                       ("TransCol", "Transmission Color"), ("TransDir", "Transmission Direct"), ("TransInd", "Transmission Indirect"),
                       ("Emit", "Emission"), ("Env", "Environment"), ("AO", "Ambient Occlusion"),
                       ("IndexOB", "Object Index"), ("IndexMA", "Material Index"))),
    # AG019: values that are not in the enum any more
    Rule("AG019", "enum", "NISHITA", ("enum:ShaderNodeTexSky.sky_type=MULTIPLE_SCATTERING", "enum:ShaderNodeTexSky.sky_type=SINGLE_SCATTERING"),
         "Sky Texture sky_type 'NISHITA' was replaced in 5.0; use 'MULTIPLE_SCATTERING' or 'SINGLE_SCATTERING'", "sky_type"),
    Rule("AG019", "enum", "AVI_RAW", ("enum:ImageFormatSettings.file_format=FFMPEG",), "AVI output was removed; set image_settings.media_type = 'VIDEO' and then file_format = 'FFMPEG'", "file_format"),
    Rule("AG019", "enum", "AVI_JPEG", ("enum:ImageFormatSettings.file_format=FFMPEG",), "AVI output was removed; set image_settings.media_type = 'VIDEO' and then file_format = 'FFMPEG'", "file_format"),
    # AG020: node types that nodes.new() refuses
    Rule("AG020", "node_type", "CompositorNodeGamma", ("node:ShaderNodeGamma",), "CompositorNodeGamma was removed in 5.0; compositor trees use the shader node ShaderNodeGamma"),
    Rule("AG020", "node_type", "CompositorNodeMixRGB", ("node:ShaderNodeMix",), "CompositorNodeMixRGB was removed in 5.0; compositor trees use the shader node ShaderNodeMix"),
    Rule("AG020", "node_type", "CompositorNodeMath", ("node:ShaderNodeMath",), "CompositorNodeMath was removed in 5.0; compositor trees use the shader node ShaderNodeMath"),
    Rule("AG020", "node_type", "ShaderNodeTexPointDensity", (), "the Point Density texture node (ShaderNodeTexPointDensity) was removed in 5.0"),
    # AG021: operator keywords that the operator rejects with TypeError
    Rule("AG021", "opkw", "export_textures", ("opkw:wm.usd_export.export_textures_mode",), "wm.usd_export has no export_textures in 5.x; use export_textures_mode", "wm.usd_export"),
    Rule("AG021", "opkw", "visible_objects_only", ("opkw:wm.usd_export.selected_objects_only",), "wm.usd_export has no visible_objects_only in 5.x; use selected_objects_only", "wm.usd_export"),
    Rule("AG021", "opkw", "export_image_add_webp", ("opkw:export_scene.gltf.export_image_add_compressed_images", "opkw:export_scene.gltf.export_compressed_images_type"),
         "export_scene.gltf has no export_image_add_webp in 5.3; use export_image_add_compressed_images with export_compressed_images_type", "export_scene.gltf"),
    Rule("AG021", "opkw", "export_image_webp_fallback", ("opkw:export_scene.gltf.export_compressed_images_fallback",),
         "export_scene.gltf has no export_image_webp_fallback in 5.3; use export_compressed_images_fallback", "export_scene.gltf"),
    Rule("AG021", "opkw", "at_collection_center", ("opkw:export_scene.gltf.collection_center_mode",), "export_scene.gltf has no at_collection_center in 5.3; use collection_center_mode", "export_scene.gltf"),
    # AG024 / AG025 / AG026: names that are gone only on one kind of owner (the linter looks at the owner)
    Rule("AG024", "ng_sockets", "inputs", ("rna:NodeTreeInterface.new_socket",), "node groups have no inputs.new()/outputs.new() since 4.0; use node_group.interface.new_socket(name=..., in_out='INPUT' or 'OUTPUT', socket_type=...)", "NodeTree"),
    Rule("AG024", "ng_sockets", "outputs", ("rna:NodeTreeInterface.new_socket",), "node groups have no inputs.new()/outputs.new() since 4.0; use node_group.interface.new_socket(name=..., in_out='INPUT' or 'OUTPUT', socket_type=...)", "NodeTree"),
    Rule("AG025", "scene_attr", "node_tree", ("rna:Scene.compositing_node_group",), "scene.node_tree was removed in 5.0; use scene.compositing_node_group (create it with bpy.data.node_groups.new(name, 'CompositorNodeTree'))", "Scene"),
    Rule("AG026", "action_attr", "fcurves", ("rna:ActionChannelbag.fcurves",), "Action.fcurves was removed in 5.0 (slotted actions); use the channelbag's fcurves (action.layers[0].strips[0].channelbag(slot).fcurves)", "Action"),
    Rule("AG026", "action_attr", "groups", ("rna:ActionChannelbag.groups",), "Action.groups was removed in 5.0 (slotted actions); use the channelbag's groups", "Action"),
)
# AG011 and AG012 predate the tables; the cross-check in the tests covers them too.
LEGACY_ENUM_RULES = (
    Rule("AG011", "enum", EEVEE_OLD, (f"enum:RenderSettings.engine={EEVEE_NEW}",), f"'{EEVEE_OLD}' was renamed; use '{EEVEE_NEW}'", "engine"),
    Rule("AG012", "enum", SOLVER_OLD, (f"enum:BooleanModifier.solver={SOLVER_NEW}",), f"boolean solver '{SOLVER_OLD}' was removed; use '{SOLVER_NEW}'", "solver"),
)
ATTR_RULES = {r.old: r for r in RULES if r.kind in ("attr", "data")}
TYPE_RULES = {r.old: r for r in RULES if r.kind == "type"}
SOCKET_IN_RULES = {r.old: r for r in RULES if r.kind == "socket_in"}
RL_OUT_RULES = {r.old: r for r in RULES if r.kind == "rl_out"}
ENUM_RULES = {(r.owner, r.old): r for r in RULES if r.kind == "enum"}
NODE_TYPE_RULES = {r.old: r for r in RULES if r.kind == "node_type"}
OPKW_RULES = {(r.owner, r.old): r for r in RULES if r.kind == "opkw"}
UNIFIED_RULE = next(r for r in RULES if r.kind == "tool_attr")
ACTION_RULES = {r.old: r for r in RULES if r.kind == "action_attr"}
NG_RULE = next(r for r in RULES if r.kind == "ng_sockets")
SCENE_NODE_TREE_RULE = next(r for r in RULES if r.kind == "scene_attr")
GN_MODIFIER_HINT = ("Geometry Nodes modifier inputs are not ID properties since 5.2 (mod[...] raises TypeError); "
                    "use mod.properties.inputs.<identifier>.value, e.g. getattr(mod.properties.inputs, socket.identifier).value = v")
MEDIA_TYPE_FORMATS = {"OPEN_EXR_MULTILAYER": "MULTI_LAYER_IMAGE", "FFMPEG": "VIDEO"}
GPU_INIT_HINT = "call gpu.init() before any gpu call: in the background worker gpu.texture.from_image without it crashes Blender"
PRINCIPLED_NAME = re.compile(r"principled|bsdf", re.I)
OTHER_NODE_TYPE = re.compile(r"^(?!.*Principled)(Shader|Compositor|Geometry|Function|Texture)Node\w+$")
RLAYERS_NAME = re.compile(r"render_?layers?|rlayers?|^rl$", re.I)


def describe_rules() -> list[dict[str, object]]:
    """The removed-name tables as plain data (what the Blender-side cross-check reads)."""
    return [r._asdict() for r in (*RULES, *LEGACY_ENUM_RULES)]


@dataclass(frozen=True)
class LintIssue:
    code: str
    line: int
    col: int
    message: str
    severity: str = "error"

    def __str__(self) -> str:
        return f"{self.line}:{self.col} {self.code} [{self.severity}] {self.message}"


class UnsafeScriptError(Exception):
    def __init__(self, issues: list[LintIssue]):
        self.issues = issues
        super().__init__("script rejected:\n" + "\n".join(str(i) for i in issues))


class _Linter(ast.NodeVisitor):
    def __init__(self, tree: ast.AST):
        self.issues: list[LintIssue] = []
        self.alias: dict[str, str] = {}
        self._collect_aliases(tree)
        self.mod_names: set[str] = set()
        self.principled: set[str] = set()
        self.rlayers: set[str] = set()
        self.node_groups: set[str] = set()
        self.own_attrs: set[str] = set()
        self._collect_bindings(tree)
        self._scan_scopes(tree)

    # ---- helpers
    def add(self, node: ast.AST, code: str, msg: str, sev: str = "error") -> None:
        issue = LintIssue(code, getattr(node, "lineno", 0), getattr(node, "col_offset", 0), msg, sev)
        if issue not in self.issues:
            self.issues.append(issue)

    def dotted(self, node: ast.AST) -> str | None:
        parts: list[str] = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(self.alias.get(node.id, node.id))
            return ".".join(reversed(parts))
        return None

    def _collect_aliases(self, tree: ast.AST) -> None:
        for _ in range(3):  # fixpoint for chained aliases (a = bpy; b = a.ops)
            for n in ast.walk(tree):
                if isinstance(n, ast.Import):
                    for a in n.names:
                        self.alias[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
                elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
                    for a in n.names:
                        self.alias[a.asname or a.name] = f"{n.module}.{a.name}"
                elif isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                    d = self.dotted(n.value)
                    if d and d.split(".")[0] in (set(self.alias.values()) | {"bpy"} | {v.split(".")[0] for v in self.alias.values()}):
                        self.alias[n.targets[0].id] = d


    # ---- what a name was bound to (a syntactic guess, used only to scope the rules that need a known owner)
    def _collect_bindings(self, tree: ast.AST) -> None:
        for n in ast.walk(tree):  # attribute names the script gives its own classes: class-body fields and self.x / cls.x
            if isinstance(n, ast.ClassDef):
                for stmt in n.body:
                    targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target] if isinstance(stmt, ast.AnnAssign) else []
                    self.own_attrs.update(t.id for t in targets if isinstance(t, ast.Name))
            elif isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store) and _is_self(n.value):
                self.own_attrs.add(n.attr)
        for n in ast.walk(tree):
            if isinstance(n, ast.Assign):
                names, value = [t.id for t in n.targets if isinstance(t, ast.Name)], n.value
            elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.value is not None:
                names, value = [n.target.id], n.value
            elif isinstance(n, ast.For) and isinstance(n.target, ast.Name):
                names, value = [n.target.id], n.iter
                if _attr_is(value, "modifiers"):
                    self.mod_names.update(names)
            else:
                continue
            strings = [c.value for c in ast.walk(value) if isinstance(c, ast.Constant) and isinstance(c.value, str)]
            for name in names:
                if _is_modifier_lookup(value):
                    self.mod_names.add(name)
                if _is_group_lookup(value):
                    self.node_groups.add(name)
                named = any(re.search(r"Principled|BSDF_PRINCIPLED", x) for x in strings)
                other = any(OTHER_NODE_TYPE.search(x) for x in strings)  # e.g. ShaderNodeEeveeSpecular, which still has 'Specular'
                if named or (PRINCIPLED_NAME.search(name) and not other):
                    self.principled.add(name)
                if RLAYERS_NAME.search(name) or any(re.search(r"Render Layers|RLayers", x) for x in strings):
                    self.rlayers.add(name)

    def _scan_scopes(self, tree: ast.AST) -> None:
        """Positions of ``gpu.init()`` calls and of ``media_type`` assignments, per scope, for AG022 and AG023."""
        self.gpu_init: tuple[int, int] | None = None
        self.media_type_at: dict[int, list[tuple[int, int]]] = {}
        self.scope_of: dict[int, int] = {}

        def walk(node: ast.AST, scope: int) -> None:
            for child in ast.iter_child_nodes(node):
                self.scope_of[id(child)] = scope
                inner = id(child) if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) else scope
                if isinstance(child, ast.Assign) and any(isinstance(t, ast.Attribute) and t.attr == "media_type" for t in child.targets):
                    self.media_type_at.setdefault(scope, []).append((child.lineno, child.col_offset))
                if isinstance(child, ast.Call) and self.dotted(child.func) == "gpu.init":
                    pos = (child.lineno, child.col_offset)
                    self.gpu_init = pos if self.gpu_init is None else min(self.gpu_init, pos)
                walk(child, inner)

        walk(tree, id(tree))

    def _owner_ok(self, base: ast.AST, names: set[str], pattern: str) -> bool:
        """``base`` is a name bound to the wanted kind of node, or an expression that spells the node's type or name."""
        if isinstance(base, ast.Name):
            return base.id in names
        return any(isinstance(c, ast.Constant) and isinstance(c.value, str) and re.search(pattern, c.value) for c in ast.walk(base))

    def _key_rules(self, node: ast.AST, base: ast.AST, key: ast.AST | None, collection: str) -> None:
        """AG017 / AG018: ``X.inputs[key]`` / ``X.inputs.get(key)`` with a name that no longer exists."""
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            return
        if not (isinstance(base, ast.Attribute) and base.attr == collection):
            return
        owner = base.value
        if collection == "inputs":
            rule = SOCKET_IN_RULES.get(key.value)
            if rule and (rule.owner != "ShaderNodeBsdfPrincipled" or self._owner_ok(owner, self.principled, r"Principled|BSDF_PRINCIPLED")):
                self.add(node, rule.code, rule.hint)
        else:
            rule = RL_OUT_RULES.get(key.value)
            if rule and self._owner_ok(owner, self.rlayers, r"Render Layers|RLayers"):
                self.add(node, rule.code, rule.hint)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        key = node.slice
        self._key_rules(node, node.value, key, "inputs")
        self._key_rules(node, node.value, key, "outputs")
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            v = node.value
            if (isinstance(v, ast.Name) and v.id in self.mod_names) or _is_modifier_lookup(v):
                self.add(node, "AG016", GN_MODIFIER_HINT)
        self.generic_visit(node)

    def check_chain(self, node: ast.AST, dotted: str | None) -> None:
        if not dotted:
            return
        root = dotted.split(".")[0]
        if root in BLOCKED_MODULE_ROOTS and "." in dotted:
            self.add(node, "AG006", f"use of blocked module attribute `{dotted}`")
        for p in BLOCKED_ATTR_PREFIXES:
            if dotted == p or dotted.startswith(p + "."):
                self.add(node, "AG006", f"`{p}` is not permitted in run_script")
        if dotted == HANDLERS or dotted.startswith(HANDLERS + "."):
            self.add(node, "AG008", "bpy.app.handlers must not be touched from run_script")

    # ---- imports
    def _import(self, node: ast.AST, mod: str) -> None:
        root = mod.split(".")[0]
        if root == "bgl":
            self.add(node, "AG010", BGL_HINT)
        elif root not in ALLOWED_MODULES:
            self.add(node, "AG001", f"import of `{mod}` is not allowlisted")

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            self._import(node, a.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level or not node.module:
            self.add(node, "AG001", "relative imports are not allowed")
        else:
            self._import(node, node.module)
            for a in node.names:
                self.check_chain(node, f"{node.module}.{a.name}")
                if a.name == "AssetHandle":
                    self.add(node, "AG013", ASSET_HANDLE_HINT)
                if a.name == "import_module" or a.name == "__import__":
                    self.add(node, "AG002", "dynamic import")

    # ---- names / attributes
    def visit_Name(self, node: ast.Name) -> None:
        if node.id in DANGEROUS_BUILTINS:
            self.add(node, "AG003", f"`{node.id}` is not permitted")
        elif node.id == "__import__":
            self.add(node, "AG002", "`__import__` is not permitted")
        elif node.id.startswith("__") and node.id.endswith("__") and node.id not in DUNDER_OK:
            self.add(node, "AG007", f"dunder name `{node.id}` is not permitted")
        elif node.id == "AssetHandle":
            self.add(node, "AG013", ASSET_HANDLE_HINT)
        elif node.id == "bgl":
            self.add(node, "AG010", BGL_HINT)
        elif node.id in TYPE_RULES:
            self.add(node, TYPE_RULES[node.id].code, TYPE_RULES[node.id].hint)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        a = node.attr
        if a.startswith("__") and a.endswith("__") and a not in DUNDER_OK:
            self.add(node, "AG007", f"dunder attribute `{a}` is not permitted")
        elif a == "bindcode":
            self.add(node, "AG014", BINDCODE_HINT)
        elif a == "AssetHandle":
            self.add(node, "AG013", ASSET_HANDLE_HINT)
        elif a == "import_module":
            self.add(node, "AG002", "dynamic import via import_module")
        self._removed_attribute(node)
        d = self.dotted(node)
        self.check_chain(node, d)
        if d and d.split(".")[0] == "importlib":
            self.add(node, "AG002", "importlib use")
        self.generic_visit(node)

    def _removed_attribute(self, node: ast.Attribute) -> None:
        a, base = node.attr, node.value
        if a in ATTR_RULES:
            # the rule is about Blender's objects: not an attribute of the script's own class (self.base_path, a dataclass field)
            if not (_is_self(base) or a in self.own_attrs):
                r = ATTR_RULES[a]
                self.add(node, r.code, r.hint)
        elif a in TYPE_RULES:
            self.add(node, TYPE_RULES[a].code, TYPE_RULES[a].hint)
        elif a == UNIFIED_RULE.old and _attr_is(base, "tool_settings"):
            self.add(node, UNIFIED_RULE.code, UNIFIED_RULE.hint)
        elif a == SCENE_NODE_TREE_RULE.old and (isinstance(base, ast.Name) and base.id == "scene" or _attr_is(base, "scene")):
            self.add(node, SCENE_NODE_TREE_RULE.code, SCENE_NODE_TREE_RULE.hint)
        elif a in ACTION_RULES and (isinstance(base, ast.Name) and base.id in ("action", "act") or _attr_is(base, "action")):
            self.add(node, ACTION_RULES[a].code, ACTION_RULES[a].hint)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and EEVEE_OLD in node.value:
            self.add(node, "AG011", f"'{EEVEE_OLD}' was renamed; use '{EEVEE_NEW}'")

    # ---- calls
    def visit_Call(self, node: ast.Call) -> None:
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else None
        d = self.dotted(fn)
        self.check_chain(node, d)
        if name == "open" or d in ("io.open", "builtins.open"):
            self._check_open(node)
        if name in REFLECTION:
            self._check_reflection(node, name)
        if d in ("importlib.import_module", "importlib.__import__") or name == "__import__":
            self.add(node, "AG002", "dynamic import is not permitted")
        for kw in node.keywords:
            if kw.arg == "solver" and _is_const(kw.value, SOLVER_OLD):
                self.add(kw.value, "AG012", f"boolean solver '{SOLVER_OLD}' was removed; use '{SOLVER_NEW}'")
            self._removed_enum(kw.value, kw.arg or "")
        self._call_rules(node, d)
        self.generic_visit(node)

    def _removed_enum(self, value: ast.AST, attr: str) -> None:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            r = ENUM_RULES.get((attr, value.value))
            if r:
                self.add(value, r.code, r.hint)

    def _call_rules(self, node: ast.Call, d: str | None) -> None:
        fn = node.func
        # AG021 operator keywords
        if d and d.startswith("bpy.ops."):
            op = d[len("bpy.ops."):]
            for kw in node.keywords:
                r = OPKW_RULES.get((op, kw.arg or ""))
                if r:
                    self.add(kw, r.code, r.hint)
        if isinstance(fn, ast.Attribute):
            # AG020 nodes.new("Type") / nodes.new(type="Type")
            if fn.attr == "new" and _attr_is(fn.value, "nodes"):
                types = [a for a in node.args[:1]] + [kw.value for kw in node.keywords if kw.arg == "type"]
                for t in types:
                    if isinstance(t, ast.Constant) and t.value in NODE_TYPE_RULES:
                        self.add(t, NODE_TYPE_RULES[t.value].code, NODE_TYPE_RULES[t.value].hint)
            # AG017 / AG018 through .get("name")
            if fn.attr == "get" and node.args:
                self._key_rules(node, fn.value, node.args[0], "inputs")
                self._key_rules(node, fn.value, node.args[0], "outputs")
            # AG024 node group inputs.new / outputs.new
            if fn.attr == "new" and isinstance(fn.value, ast.Attribute) and fn.value.attr in ("inputs", "outputs"):
                owner = fn.value.value
                if (isinstance(owner, ast.Name) and owner.id in self.node_groups) or _is_group_lookup(owner) or _attr_is(owner, "node_groups"):
                    self.add(node, NG_RULE.code, NG_RULE.hint)
        # AG023 gpu use without gpu.init()
        if d and d.startswith("gpu.") and d != "gpu.init":
            pos = (node.lineno, node.col_offset)
            if self.gpu_init is None or pos < self.gpu_init:
                self.add(node, "AG023", GPU_INIT_HINT)

    def _check_open(self, node: ast.Call) -> None:
        mode: ast.AST | None = node.args[1] if len(node.args) > 1 else None
        for kw in node.keywords:
            if kw.arg == "mode":
                mode = kw.value
        if mode is None:
            return
        if not (isinstance(mode, ast.Constant) and isinstance(mode.value, str)):
            self.add(node, "AG004", "open() with a non-constant mode")
        elif set(mode.value) & WRITE_MODE_CHARS or not set(mode.value) <= READ_MODE_CHARS:
            self.add(node, "AG004", f"open() in write mode '{mode.value}' is not permitted")

    def _check_reflection(self, node: ast.Call, name: str) -> None:
        if len(node.args) < 2:
            return
        base, attr = node.args[0], node.args[1]
        if (name == "getattr" and len(node.args) == 2 and not node.keywords and isinstance(attr, ast.Attribute)
                and attr.attr == "identifier" and _attr_is(base, "inputs") and _attr_is(base.value, "properties")):  # type: ignore[attr-defined]
            return  # the one dynamic form: getattr(<x>.properties.inputs, <socket>.identifier) (Geometry Nodes modifier inputs)
        if not (isinstance(attr, ast.Constant) and isinstance(attr.value, str)):
            self.add(node, "AG005", f"{name}() with a dynamic attribute name")
            return
        s = attr.value
        if s.startswith("__") and s.endswith("__"):
            self.add(node, "AG005", f"{name}() of dunder `{s}`")
        d = self.dotted(base)
        if d:
            if d.split(".")[0] in BLOCKED_MODULE_ROOTS:
                self.add(node, "AG005", f"{name}() on blocked module `{d}`")
            self.check_chain(node, f"{d}.{s}")
        if name == "setattr" and s == "solver" and len(node.args) > 2 and _is_const(node.args[2], SOLVER_OLD):
            self.add(node, "AG012", f"boolean solver '{SOLVER_OLD}' was removed; use '{SOLVER_NEW}'")

    # ---- assignments
    def _check_assign(self, targets: list[ast.expr], value: ast.AST | None, node: ast.AST) -> None:
        for t in targets:
            if isinstance(t, ast.Attribute) and t.attr == "solver" and value is not None and _is_const(value, SOLVER_OLD):
                self.add(node, "AG012", f"boolean solver '{SOLVER_OLD}' was removed; use '{SOLVER_NEW}'")
            if isinstance(t, ast.Attribute) and value is not None:
                self._removed_enum(value, t.attr)
                if t.attr == "file_format" and isinstance(value, ast.Constant) and value.value in MEDIA_TYPE_FORMATS:
                    scope = self.scope_of.get(id(node), 0)
                    pos = (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))
                    if not any(p < pos for p in self.media_type_at.get(scope, [])):
                        self.add(node, "AG022", f"set image_settings.media_type = '{MEDIA_TYPE_FORMATS[value.value]}' before file_format = '{value.value}' (5.0: the format is not accepted otherwise)")

    def visit_Assign(self, node: ast.Assign) -> None:
        self._check_assign(node.targets, node.value, node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._check_assign([node.target], node.value, node)
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        if isinstance(node.test, ast.Constant) and node.test.value and not any(
                isinstance(n, (ast.Break, ast.Return)) for n in ast.walk(node)):
            self.add(node, "AG030", "unbounded `while True` loop would hang the daemon", "warning")
        self.generic_visit(node)


def _is_self(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id in ("self", "cls")


def _attr_is(node: ast.AST, attr: str) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == attr


def _is_modifier_lookup(node: ast.AST) -> bool:
    """``<x>.modifiers.new(...)``, ``<x>.modifiers.get(...)`` or ``<x>.modifiers[...]``."""
    if isinstance(node, ast.Call):
        return isinstance(node.func, ast.Attribute) and node.func.attr in ("new", "get") and _attr_is(node.func.value, "modifiers")
    return isinstance(node, ast.Subscript) and _attr_is(node.value, "modifiers")


def _is_group_lookup(node: ast.AST) -> bool:
    """``<x>.node_groups.new(...)``, ``.get(...)`` or ``[...]``."""
    if isinstance(node, ast.Call):
        return isinstance(node.func, ast.Attribute) and node.func.attr in ("new", "get") and _attr_is(node.func.value, "node_groups")
    return isinstance(node, ast.Subscript) and _attr_is(node.value, "node_groups")


def _is_const(node: ast.AST, value: str) -> bool:
    return isinstance(node, ast.Constant) and node.value == value


def lint_script(source: str) -> list[LintIssue]:
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [LintIssue("AG000", e.lineno or 0, e.offset or 0, f"syntax error: {e.msg}")]
    lin = _Linter(tree)
    lin.visit(tree)
    return sorted(lin.issues, key=lambda i: (i.line, i.col, i.code))


def assert_safe(source: str) -> None:
    errors = [i for i in lint_script(source) if i.severity == "error"]
    if errors:
        raise UnsafeScriptError(errors)
