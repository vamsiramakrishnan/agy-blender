"""The plugin's agent-facing documents (skills, rules, agent definitions) name only things that exist.

The ground truth is read from the real modules: the tool registry and each stage's tool-set (harness.tools), the
management and inspector tools that the MCP server adds (runtime.mcp_server, runtime.inspector_server), the Scene IR
node kinds and patch operations (ir) and the subagent table (harness.tools.SUBAGENT_TOOLS).

Only backticked names that look like a tool call (a lowercase identifier with an underscore) are checked, so prose,
file names and manifest keys are not. Each name must be a tool, an IR operation or node kind, a parameter of the tool
named in the same sentence, or on ALLOWED. Primitive names and delegation targets are checked separately.
"""

from __future__ import annotations

import inspect
import re
import typing
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from antigravity3d import ir
from antigravity3d.harness import tools as T
from antigravity3d.plugin_install import bundle_dir
from antigravity3d.runtime import inspector_server, mcp_server

BUNDLE = bundle_dir()
DOCS = sorted([*BUNDLE.glob("skills/*/SKILL.md"), *BUNDLE.glob("rules/*.md"), *BUNDLE.glob("agents/*.md")])

# Names the documents use on purpose that are not studio tools: platform tools, Blender modules and the Blender
# operators the 5.3 linter blocks (rules/blender-5-3-api.md).
ALLOWED = frozenset({"generate_image", "run_workflow", "bpy_extras", "gpu_extras", "bl_math",
                     "open_mainfile", "quit_blender"})
# Words that name a geometry primitive when they appear in a sentence about primitives.
PRIMITIVE_WORDS = frozenset({"box", "cube", "cuboid", "cylinder", "cone", "sphere", "capsule", "torus", "plane",
                             "circle", "grid", "sweep", "lathe", "prism", "pyramid", "tube"})

# Modelling operations that Scene IR patches do not have (the IR ops are upsert_node ... flush_back).
UNSUPPORTED_OPS = frozenset({"boolean", "booleans", "array", "arrays", "mirror", "bevel", "subdivide", "extrude",
                             "decimate", "instancing", "instanced"})

_FENCE = re.compile(r"```.*?```", re.S)
_SPAN = re.compile(r"`([^`\n]+)`")
_IDENT = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+")
_ARG = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_CALL = re.compile(r"([a-z][a-z0-9_]*)\((.*)\)")
_DELEGATION = (
    re.compile(r"`([a-z][a-z0-9_]*)`\s+subagent"),
    re.compile(r"(?i:delegat\w*|hand to)[^.`]*`([a-z][a-z0-9_]*)`"),
)


def _params(fn: Callable[..., Any]) -> set[str]:
    return set(inspect.signature(fn).parameters) - {"ctx"}


def _property_names(node: Any) -> set[str]:
    """Every property name in a tool's JSON input schema, nested ones included."""
    names: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "properties" and isinstance(value, dict):
                names |= set(value)
            names |= _property_names(value)
    elif isinstance(node, list):
        for value in node:
            names |= _property_names(value)
    return names


class _Recorder:
    """Stands in for the MCP server, so the inspector registers its tools without starting one."""

    def __init__(self) -> None:
        self.tools: dict[str, Callable[..., Any]] = {}

    def add_tool(self, fn: Callable[..., Any], name: str | None = None, **_kwargs: Any) -> None:
        self.tools[name or fn.__name__] = fn

    def __getattr__(self, _attr: str) -> Callable[..., Any]:
        return lambda *_a, **_k: (lambda fn: fn)


def _studio_params() -> dict[str, set[str]]:
    """Every tool the MCP server serves, mapped to its parameter names."""
    tools: dict[str, set[str]] = {}
    for name, tool in T.ALL_TOOLS.items():
        fn = getattr(tool, "fn", tool)
        tools[name] = _params(fn) | _property_names(getattr(tool, "input_schema", {}))
    for name, (fn, _doc) in mcp_server._management(None).items():
        tools[name] = _params(fn)
    recorder = _Recorder()
    inspector_server.register(recorder, None)
    for name, fn in recorder.tools.items():
        tools[name] = _params(fn)
    return tools


def _ir_ops() -> dict[str, set[str]]:
    """Each Scene IR patch operation mapped to the fields it carries."""
    ops: dict[str, set[str]] = {}
    for cls in typing.get_args(typing.get_args(ir.IROp)[0]):
        fields = set(cls.model_fields) - {"op"}
        for op in typing.get_args(cls.model_fields["op"].annotation):
            ops[op] = fields
    return ops


NODE_KINDS = frozenset(typing.get_args(ir.NodeIR.model_fields["kind"].annotation))
TOOLS = _studio_params()
OPS = _ir_ops()


def _sentences(text: str) -> Iterator[str]:
    """Prose sentences: code fences dropped, wrapped lines joined, blank lines and ``.``/``;`` end a sentence."""
    for block in re.split(r"\n\s*\n", _FENCE.sub(" ", text)):
        yield from re.split(r"(?<=[.;])\s+", " ".join(block.split()))


def name_problems(text: str) -> list[str]:
    problems: list[str] = []
    for sentence in _sentences(text):
        spans = _SPAN.findall(sentence)
        calls = [m.groups() for s in spans if (m := _CALL.fullmatch(s))]
        idents = [s for s in spans if _IDENT.fullmatch(s)]
        named = {name for name, _ in calls} | {s for s in idents if s in TOOLS or s in OPS}
        scope: set[str] = set()
        for name in named:
            scope |= TOOLS.get(name, set()) | OPS.get(name, set())
        for name, arglist in calls:
            if name in ALLOWED:
                continue
            if name not in TOOLS and name not in OPS:
                problems.append(f"`{name}(...)` is not a studio tool: {sentence[:100]!r}")
                continue
            for arg in _ARG.findall(arglist):
                if arg not in TOOLS.get(name, set()) | OPS.get(name, set()):
                    problems.append(f"`{arg}` is not a parameter of `{name}`: {sentence[:100]!r}")
        for ident in idents:
            if ident in TOOLS or ident in OPS or ident in NODE_KINDS or ident in ALLOWED or ident in T.SUBAGENT_TOOLS:
                continue
            if ident not in scope:
                problems.append(f"`{ident}` is not a tool, IR operation or parameter of the tool named in its "
                                f"sentence: {sentence[:100]!r}")
    return problems


def primitive_problems(text: str) -> list[str]:
    problems: list[str] = []
    for sentence in _sentences(text):
        if "primitive" not in sentence.lower():
            continue
        for word in re.findall(r"[a-z]+", sentence.lower()):
            if word in PRIMITIVE_WORDS and word not in NODE_KINDS:
                problems.append(f"`{word}` is not a node kind (NodeIR.kind is {sorted(NODE_KINDS)}): "
                                f"{sentence[:100]!r}")
    return problems


def unsupported_op_problems(text: str) -> list[str]:
    """A sentence about patches or Scene IR must not offer a modelling operation the IR does not have."""
    problems: list[str] = []
    for sentence in _sentences(text):
        low = sentence.lower()
        if ("patch" not in low and "scene ir" not in low) or re.search(r"\b(no|not|never)\b", low):
            continue
        for word in re.findall(r"[a-z]+", low):
            if word in UNSUPPORTED_OPS:
                problems.append(f"`{word}` is not a Scene IR operation: {sentence[:100]!r}")
    return problems


def delegation_problems(text: str) -> list[str]:
    body = _FENCE.sub(" ", text)
    return [f"`{name}` is delegated to but is not a subagent (tools.SUBAGENT_TOOLS)"
            for pattern in _DELEGATION for name in pattern.findall(body) if name not in T.SUBAGENT_TOOLS]


def _id(path: Path) -> str:
    return path.relative_to(BUNDLE).as_posix()


@pytest.mark.parametrize("doc", DOCS, ids=_id)
def test_backticked_tool_names_exist(doc: Path) -> None:
    problems = name_problems(doc.read_text())
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("doc", DOCS, ids=_id)
def test_primitives_named_in_docs_are_node_kinds(doc: Path) -> None:
    problems = primitive_problems(doc.read_text())
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("doc", DOCS, ids=_id)
def test_patches_do_not_offer_unsupported_operations(doc: Path) -> None:
    problems = unsupported_op_problems(doc.read_text())
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("doc", DOCS, ids=_id)
def test_delegated_subagents_exist(doc: Path) -> None:
    problems = delegation_problems(doc.read_text())
    assert not problems, "\n".join(problems)


def test_checker_flags_names_that_do_not_exist() -> None:
    bad = ("Run `run_visual_qa` with rubric `ortho_blueprint`. Use `sweep` as a primitive. "
           "Add booleans as further patches. Delegate the check to the `ghost_checker` subagent.")
    assert name_problems(bad) and primitive_problems(bad) and unsupported_op_problems(bad)
    assert delegation_problems(bad)
    assert not name_problems("Call `apply_scene_ir` with `ops`, then `inspect_manifest`.")
    assert not unsupported_op_problems("Scene IR has no boolean or mirror ops.")
