"""A read of an asset script before it runs: what a build script has no use for, judged by ordinary code.

This is a guard against accidents, not a sandbox. It catches what an agent writing a build script does by mistake or
habit (reading a file with ``open`` or ``numpy.loadtxt``, saving a debug dump with ``numpy.save`` or
``mesh.export(path)``, importing ``os`` to make a folder, ``exec`` of generated text, reflection on Python's internals)
and answers with what to use instead, before a process is started; ordinary code (classes, ``super().__init__()``,
``self.__x``, ``x.__len__()``, a variable named ``open``, the pure-computation standard modules) passes.

What it still does not stop, said plainly: a script that means harm. It reads names, not intent: a module object
passed around under another name, a function stored in a variable and called later, ``str`` formatting tricks, a
library function this list does not name (``trimesh`` and ``scipy`` have many), all pass. Nothing here limits memory or
the network. The limits that hold whatever the script does are the runner's (``antigravity3d.build.runner``): a bare
environment with no key, its own process group killed at the time limit.

``check(source, filename)`` returns ``None`` when the script may run, else one line: where (line number and text), the
rule, and what to use instead.
"""

from __future__ import annotations

import ast

# The modules a build script may import: the reference page's (SKILL.md: "math, random, numpy, scipy, trimesh and
# agy3d") and standard modules that only compute (no file, process or network of their own). Measured on the
# reviewer's 60 plausible snippets (S/r6/scratch_review_door/legit.py): the earlier, shorter list refused 22.
ALLOWED_IMPORTS = frozenset({
    "agy3d", "antigravity3d.build", "math", "cmath", "random", "itertools", "functools", "collections", "dataclasses",
    "typing", "typing_extensions", "enum", "colorsys", "numpy", "scipy", "trimesh", "__future__", "copy", "time",
    "operator", "statistics", "decimal", "string", "abc", "bisect", "heapq", "warnings", "json", "re", "numbers",
    "fractions",
})
# Builtins refused where they are CALLED (a variable or a keyword argument of the same name is ordinary code).
FORBIDDEN_CALLS = {
    "open": "a build script reads no file and writes none: a mesh file comes in with a.load(path), the design leaves "
            "with a.save()",
    "exec": "write the code itself in the script",
    "eval": "write the expression itself in the script",
    "compile": "write the code itself in the script",
    "__import__": "import an allowed module with an import statement",
    "input": "a build script runs unattended; put the value in the script",
    "breakpoint": "a build script runs unattended; print the value instead",
    "globals": "use the variable by its name",
    "locals": "use the variable by its name",
    "vars": "use the attribute by its name",
    "getattr": "use the attribute by its name (p.center, not getattr(p, 'center'))",
    "setattr": "assign the attribute by its name",
    "delattr": "a build script has no reason to delete an attribute",
}
# Double-underscore attributes that reach Python's internals (objects by reflection, modules, code): refused. Every
# other one (__init__, __len__, a private self.__x, __array_interface__) is ordinary code.
REFLECTION = frozenset({
    "__class__", "__bases__", "__base__", "__mro__", "__subclasses__", "__globals__", "__builtins__", "__dict__",
    "__code__", "__closure__", "__getattribute__", "__import__", "__loader__", "__spec__", "__reduce__", "__reduce_ex__",
    "__func__", "__self__", "__defaults__", "__kwdefaults__", "__wrapped__", "__traceback__", "__subclasshook__",
    "__init_subclass__", "__set_name__", "__path__", "__file__", "__cached__", "__package__",
})
# Modules an allowed library imports for itself; reached through it (numpy.os, random._os, trimesh.util.sys) they are
# the system. Refused as an attribute of an imported module's name.
SYSTEM_ATTRS = frozenset({"os", "_os", "sys", "subprocess", "shutil", "socket", "builtins", "importlib", "pathlib",
                          "ctypes", "ctypeslib", "io", "urllib", "requests", "resolvers"})
# The ordinary ways a model reads or writes a file through the allowed libraries, by the module they are reached from.
NUMPY_IO = frozenset({"save", "savez", "savez_compressed", "savetxt", "load", "loadtxt", "genfromtxt", "fromfile", "memmap",
                      "DataSource", "fromregex", "npyio"})
TRIMESH_IO = frozenset({"exchange", "resolvers", "remote"})  # load*, export* by prefix (``_trimesh_io``)
# The same as methods, whatever they are called on: an array's .tofile / .dump, a mesh's .export(path). Never .load
# or .save: those are the script API's own (a.load(path), a.save()).
IO_METHODS = frozenset({"tofile", "dump", "export", "to_file"})
FILE_HINT = "a build script reads no file and writes none: a mesh file comes in with a.load(path), the design leaves with a.save()"


def _allowed(module: str) -> bool:
    if module == "scipy.io" or module.startswith("scipy.io."):
        return False
    return any(module == m or module.startswith(m + ".") for m in ALLOWED_IMPORTS)


def _where(lines: list[str], node: ast.AST, filename: str) -> str:
    n = getattr(node, "lineno", 0)
    text = lines[n - 1].strip() if 0 < n <= len(lines) else ""
    return f"{filename} line {n}: {text}"


def _chain(node: ast.AST) -> tuple[str | None, list[str]]:
    """``np.lib.npyio.load`` -> ("np", ["lib", "npyio", "load"]); (None, []) when the root is not a plain name."""
    attrs: list[str] = []
    while isinstance(node, ast.Attribute):
        attrs.append(node.attr)
        node = node.value
    return (node.id, attrs[::-1]) if isinstance(node, ast.Name) else (None, [])


def _trimesh_io(attr: str) -> bool:
    return attr.startswith(("load", "export")) or attr in TRIMESH_IO


def _library_io(module: str, attrs: list[str]) -> str | None:
    """The dotted name of a file read/write reached from ``module`` through ``attrs``, else None."""
    full = ".".join([module, *attrs])
    if module.split(".")[0] == "numpy" and any(a in NUMPY_IO for a in [*module.split(".")[1:], *attrs]):
        return full
    if module.split(".")[0] == "trimesh" and any(_trimesh_io(a) for a in [*module.split(".")[1:], *attrs]):
        return full
    if full == "scipy.io" or full.startswith("scipy.io."):
        return full
    return None


def check(source: str, filename: str = "script") -> str | None:
    """None when the script passes the guard, else the refusal in one line. A syntax error is not reported here: the
    runner compiles the script first and words it as such."""
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError:
        return None
    lines = source.splitlines()
    allowed = "agy3d, math, random, numpy, scipy, trimesh and the standard modules that only compute (itertools, functools, collections, dataclasses, typing, json, re, ...)"
    modules: dict[str, str] = {}  # local name -> the module (or module.attr) it is bound to by an import
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _allowed(alias.name):
                    return f"{_where(lines, node, filename)}: import of {alias.name} is not allowed: a build script imports only {allowed}"
                modules[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level or not _allowed(mod):
                return (f"{_where(lines, node, filename)}: import from {'.' * node.level}{mod} is not allowed: a build script "
                        f"imports only {allowed}")
            for alias in node.names:
                if (io := _library_io(mod, [alias.name])) is not None:
                    return f"{_where(lines, node, filename)}: {io} reads or writes a file: {FILE_HINT}"
                modules[alias.asname or alias.name] = f"{mod}.{alias.name}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS \
                and node.func.id not in modules:
            return f"{_where(lines, node, filename)}: {node.func.id}() is not allowed in a build script: {FORBIDDEN_CALLS[node.func.id]}"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in IO_METHODS:
            return f"{_where(lines, node, filename)}: .{node.func.attr}() writes a file: {FILE_HINT}"
        if isinstance(node, ast.Attribute):
            if node.attr in REFLECTION:
                return (f"{_where(lines, node, filename)}: {node.attr} reaches Python's internals, which a build script has "
                        "no use for: use the object's own attributes and the modules you import")
            root, attrs = _chain(node)
            module = modules.get(root) if root else None
            if module is not None and attrs:
                if any(a in SYSTEM_ATTRS for a in attrs):
                    return (f"{_where(lines, node, filename)}: {root}.{'.'.join(attrs)} reaches the system through a library, "
                            "which a build script has no use for: it computes the design and saves it with a.save()")
                if (io := _library_io(module, attrs)) is not None:
                    return f"{_where(lines, node, filename)}: {io} reads or writes a file: {FILE_HINT}"
    return None
