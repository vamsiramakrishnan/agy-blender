"""Compatibility shim: normalise pre-5.3 Blender API usage in legacy scripts
(e.g. the open-source ``EL4CTEO/blender-skills`` scripts).

Layout
------
(a) Pure-Python, bpy-free tables and normalisers (top of file). VERIFIED HERE by
    unit tests -- but only as string/AST logic. The *values* in the tables are
    transcribed from the migration notes in the task brief, not checked against
    a Blender 5.3 binary. Anything uncertain lives in a ``*_MAP`` / ``*_SYMBOL``
    constant so correcting it is a one-line edit.
(b) ``install(bpy)``: thin runtime patch layer. UNVERIFIED without a Blender 5.3
    binary; written to spec only. RNA properties cannot be monkey-patched, so
    most fixes are done by ``rewrite_source`` (AST rewrite before execution);
    ``install`` only exposes helper functions under ``bpy.agy_compat`` for the
    cases that need runtime logic.
"""

from __future__ import annotations

import ast
from typing import Any

# ---- tables (edit here if a 5.3 symbol differs) --------------------------------------
ENGINE_ID_MAP: dict[str, str] = {"BLENDER_EEVEE_NEXT": "BLENDER_EEVEE"}
BOOLEAN_SOLVER_MAP: dict[str, str] = {"FAST": "FLOAT"}
# old Render Layers socket / pass name -> name in 5.3 (UNVERIFIED mapping; Depth is identity).
RENDER_PASS_MAP: dict[str, str] = {
    "Depth": "Depth", "Z": "Depth", "Diffuse Color": "DiffCol", "Diffuse Direct": "DiffDir",
    "Glossy Color": "GlossCol", "Glossy Direct": "GlossDir", "Emission": "Emit",
    "Ambient Occlusion": "AO", "Normal": "Normal", "Position": "Position", "Mist": "Mist",
}
BGL_REPLACEMENT_MODULE = "gpu"
BINDCODE_ATTR = "bindcode"
GPU_TEXTURE_FROM_IMAGE = ("gpu", "texture", "from_image")
ASSET_HANDLE_OLD, ASSET_REPRESENTATION_NEW = "AssetHandle", "AssetRepresentation"
READONLY_PROPGROUP_ATTRS = frozenset({"name"})


# ---- (a) pure normalisers -------------------------------------------------------------
def normalize_engine_id(engine: str) -> str:
    return ENGINE_ID_MAP.get(engine, engine)


def normalize_boolean_solver(solver: str) -> str:
    return BOOLEAN_SOLVER_MAP.get(solver, solver)


def normalize_pass_name(name: str) -> str:
    return RENDER_PASS_MAP.get(name, name)


def split_properties(values: dict[str, Any], registered: set[str] | frozenset[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Splits keys into (bpy.props-defined, ID custom properties) given the names
    registered via ``bpy.props`` (``bl_rna.properties`` keys)."""
    rna = {k: v for k, v in values.items() if k in registered}
    idp = {k: v for k, v in values.items() if k not in registered}
    return rna, idp


def is_readonly_propgroup_assignment(attr: str) -> bool:
    return attr in READONLY_PROPGROUP_ATTRS


class _Rewriter(ast.NodeTransformer):
    def __init__(self) -> None:
        self.changes: list[str] = []

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if isinstance(node.value, str) and node.value in ENGINE_ID_MAP:
            self.changes.append(f"engine {node.value}")
            return ast.copy_location(ast.Constant(ENGINE_ID_MAP[node.value]), node)
        return node

    def _solver(self, value: ast.AST | None) -> None:
        if isinstance(value, ast.Constant) and value.value in BOOLEAN_SOLVER_MAP:
            self.changes.append(f"solver {value.value}")
            value.value = BOOLEAN_SOLVER_MAP[value.value]

    def visit_Assign(self, node: ast.Assign) -> ast.AST:
        if any(isinstance(t, ast.Attribute) and t.attr == "solver" for t in node.targets):
            self._solver(node.value)
        return self.generic_visit(node)

    def visit_keyword(self, node: ast.keyword) -> ast.AST:
        if node.arg == "solver":
            self._solver(node.value)
        return self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if node.id == ASSET_HANDLE_OLD:
            self.changes.append("AssetHandle")
            return ast.copy_location(ast.Name(ASSET_REPRESENTATION_NEW, node.ctx), node)
        return node

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        node = self.generic_visit(node)  # type: ignore[assignment]
        if node.attr == ASSET_HANDLE_OLD:
            node.attr = ASSET_REPRESENTATION_NEW
            self.changes.append("AssetHandle")
        elif node.attr == BINDCODE_ATTR and isinstance(node.ctx, ast.Load):
            self.changes.append("bindcode")
            fn: ast.expr = ast.Name(GPU_TEXTURE_FROM_IMAGE[0], ast.Load())
            for part in GPU_TEXTURE_FROM_IMAGE[1:]:
                fn = ast.Attribute(fn, part, ast.Load())
            return ast.copy_location(ast.Call(fn, [node.value], []), node)
        return node


def rewrite_source(source: str) -> tuple[str, list[str]]:
    """Rewrites deprecated symbols in ``source``; returns (new_source, change_log).
    ``bgl`` imports are NOT rewritten (no mechanical equivalent): the linter
    reports them. Source that fails to parse is returned unchanged."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source, []
    rw = _Rewriter()
    tree = rw.visit(tree)
    if not rw.changes:
        return source, []
    return ast.unparse(ast.fix_missing_locations(tree)), rw.changes


# ---- (b) runtime patches --------------------------------------------------------------
def install(bpy: Any) -> None:
    """Exposes ``bpy.agy_compat`` helpers. UNVERIFIED without a Blender 5.3 binary.

    Helpers: ``set_engine`` (normalises engine ids), ``set_boolean_solver``,
    ``pass_name``, ``get_prop`` / ``set_prop`` (bpy.props vs ID custom
    properties), ``safe_set_name`` (guards read-only PropertyGroup.name),
    ``texture_from_image`` (gpu.texture.from_image), ``asset_representation_type``.
    """
    import types

    ns = types.SimpleNamespace()

    def set_engine(scene: Any, engine: str) -> None:
        scene.render.engine = normalize_engine_id(engine)

    def set_boolean_solver(mod: Any, solver: str) -> None:
        mod.solver = normalize_boolean_solver(solver)

    def get_prop(idblock: Any, key: str, default: Any = None) -> Any:
        if key in idblock.bl_rna.properties:
            return getattr(idblock, key)
        return idblock.get(key, default)

    def set_prop(idblock: Any, key: str, value: Any) -> None:
        if key in idblock.bl_rna.properties:
            setattr(idblock, key, value)
        else:
            idblock[key] = value

    def safe_set_name(pg: Any, value: str) -> bool:
        if is_readonly_propgroup_assignment("name"):
            try:
                pg.name = value
            except (AttributeError, TypeError):
                return False
        return True

    def texture_from_image(image: Any) -> Any:
        import gpu.texture
        return gpu.texture.from_image(image)

    def asset_representation_type() -> Any:
        return getattr(bpy.types, ASSET_REPRESENTATION_NEW, None)

    for fn in (set_engine, set_boolean_solver, get_prop, set_prop, safe_set_name,
               texture_from_image, asset_representation_type):
        setattr(ns, fn.__name__, fn)
    ns.pass_name = normalize_pass_name
    ns.rewrite_source = rewrite_source
    bpy.agy_compat = ns
    if hasattr(bpy.types, ASSET_HANDLE_OLD) is False and hasattr(bpy.types, ASSET_REPRESENTATION_NEW):
        try:
            setattr(bpy.types, ASSET_HANDLE_OLD, getattr(bpy.types, ASSET_REPRESENTATION_NEW))
        except (AttributeError, TypeError):
            pass  # bpy.types may be immutable; rewrite_source covers scripts instead
