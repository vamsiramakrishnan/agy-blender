"""AST compatibility + safety gate for scripts destined for ``run_script``.

IMPORTANT: this is a guard-rail against accidents and naive prompt-injected
code, NOT a security boundary. Python cannot be sandboxed by static analysis;
a determined author can find escapes this does not model. In production the
daemon must additionally run under OS-level sandboxing (seccomp/namespaces,
read-only filesystem, no network, dropped capabilities).

Verified here: everything in this module (pure Python, ``ast`` only).
The deprecated-symbol list reflects the 5.3 migration notes in the task brief;
edit the tables below if a symbol turns out to differ.

Codes: AG000 syntax error; AG001 module not allowlisted; AG002 dynamic import;
AG003 eval/exec/compile & friends; AG004 open() for writing; AG005 reflection
with dynamic/dangerous name; AG006 blocked attribute chain; AG007 dunder
access; AG008 ``bpy.app.handlers`` use; AG010 bgl; AG011 BLENDER_EEVEE_NEXT;
AG012 boolean solver FAST; AG013 AssetHandle; AG014 Image.bindcode;
AG030 (warning) unbounded loop.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

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
BGL_HINT = "bgl was removed; use the `gpu` module (e.g. gpu.texture.from_image(image))"
ASSET_HANDLE_HINT = "AssetHandle was removed; use AssetRepresentation"
BINDCODE_HINT = "Image.bindcode was removed; use gpu.texture.from_image(image)"


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
        d = self.dotted(node)
        self.check_chain(node, d)
        if d and d.split(".")[0] == "importlib":
            self.add(node, "AG002", "importlib use")
        self.generic_visit(node)

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
        self.generic_visit(node)

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
