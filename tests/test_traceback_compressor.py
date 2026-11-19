"""Tests for antigravity3d.harness.traceback_compressor."""

from __future__ import annotations

import pytest

from antigravity3d.harness.traceback_compressor import (
    DEFAULT_HINT,
    HINT_RULES,
    compress_exception,
)


class ValidationError(Exception):
    """Local stand-in for pydantic.ValidationError (matched by class name)."""


class UnsafeScriptError(Exception):
    pass


class NodeError(ValueError):
    def __init__(self, message: str, node_id: str | None = None) -> None:
        super().__init__(message)
        self.node_id = node_id


class BrokenStr(Exception):
    def __str__(self) -> str:
        raise RuntimeError("cannot format")


class ExplodingNodeId(Exception):
    @property
    def node_id(self) -> str:
        raise RuntimeError("property failed")


def _chained(outer: BaseException, cause: BaseException) -> BaseException:
    outer.__cause__ = cause
    return outer


def test_hint_rules_table_is_module_level():
    assert isinstance(HINT_RULES, tuple)
    assert len(HINT_RULES) >= 8


@pytest.mark.parametrize(
    ("exc", "hint"),
    [
        (ValidationError("bad field"), "fix the IR fields named above and resend"),
        (KeyError("lid_9"), "call inspect_manifest to list valid node ids"),
        (RuntimeError("unknown node lid_9"), "call inspect_manifest to list valid node ids"),
        (RuntimeError("mesh is non_manifold at edge 3"), "close open edges or replace the part with a primitive"),
        (RuntimeError("mesh is not watertight"), "close open edges or replace the part with a primitive"),
        (RuntimeError("tris 5000 > budget 1000"), "reduce segments or remove detail to meet the triangle budget"),
        (TimeoutError("took too long"), "retry once; if it repeats, simplify the request"),
        (UnsafeScriptError("import os is blocked"), "remove the blocked import/call; use the IR tools instead of raw scripts"),
        (FileNotFoundError("scene.usda"), "check the path with inspect_manifest"),
        (ValueError("radius must be positive"), "check parameter ranges"),
        (RuntimeError("boom"), DEFAULT_HINT),
    ],
)
def test_each_hint_rule(exc, hint):
    assert compress_exception(exc).endswith(f" | hint: {hint}")


def test_first_matching_hint_rule_wins():
    # Both the TimeoutError rule and the "unknown node" rule match; the
    # earlier "unknown node" rule must win.
    line = compress_exception(TimeoutError("unknown node 'x'"))
    assert line.endswith("| hint: call inspect_manifest to list valid node ids")


def test_node_id_from_attribute():
    exc = NodeError("bad geometry", node_id="lid_7")
    assert compress_exception(exc) == (
        "NodeError @ lid_7: bad geometry | hint: check parameter ranges"
    )


def test_node_id_from_attribute_on_cause():
    exc = _chained(RuntimeError("tool failed"), NodeError("bad geometry", node_id="arm"))
    assert compress_exception(exc).startswith("NodeError @ arm: bad geometry")


@pytest.mark.parametrize(
    ("message", "node"),
    [
        ("missing node 'lid_1' in graph", "lid_1"),
        ('missing node "lid_2" in graph', "lid_2"),
        ("missing node lid_3 in graph", "lid_3"),
        ("bad reference @lid_4 in graph", "lid_4"),
        ("cannot open /crate/lid_5", "lid_5"),
        ("node is missing", "-"),
        ("nothing matched here", "-"),
    ],
)
def test_node_id_from_message(message, node):
    assert compress_exception(RuntimeError(message)).startswith(f"RuntimeError @ {node}: ")


def test_cause_chain_reports_root_cause():
    exc = _chained(RuntimeError("tool failed"), ValueError("disk on fire"))
    assert compress_exception(exc) == (
        "ValueError @ -: disk on fire | hint: check parameter ranges"
    )


def test_multiline_message_flattened_with_paths_and_addresses_shortened():
    msg = (
        "\n\n  failed at 0x7f3a2c001230 reading   /home/user/work/proj/mesh.usd\n"
        "Traceback (most recent call last):\n"
        '  File "/home/user/x.py", line 3'
    )
    line = compress_exception(RuntimeError(msg))
    assert line == f"RuntimeError @ -: failed at 0x… reading mesh.usd | hint: {DEFAULT_HINT}"
    assert "\n" not in line
    assert "/home/user" not in line


def test_first_non_empty_line_is_used_for_hint_too():
    line = compress_exception(RuntimeError("\n\n  tris > 9\n"))
    assert line.endswith("| hint: reduce segments or remove detail to meet the triangle budget")


def test_whitespace_is_collapsed():
    line = compress_exception(ValueError("bad \t  value\there"))
    assert line.startswith("ValueError @ -: bad value here | hint:")


def test_empty_message_uses_placeholder():
    assert compress_exception(RuntimeError()) == (
        f"RuntimeError @ -: no message | hint: {DEFAULT_HINT}"
    )


def test_truncation_keeps_class_node_and_hint():
    exc = NodeError("very long detail " * 40, node_id="lid_8")
    line = compress_exception(exc, max_len=120)
    assert len(line) <= 120
    assert "\n" not in line
    assert line.startswith("NodeError @ lid_8: very long detail")
    assert "…" in line
    assert line.endswith(" | hint: check parameter ranges")


def test_default_max_len_is_respected():
    line = compress_exception(RuntimeError("x " * 500))
    assert len(line) <= 240
    assert "\n" not in line


def test_tiny_max_len_still_within_limit():
    line = compress_exception(ValueError("x"), max_len=20)
    assert len(line) <= 20
    assert "\n" not in line


def test_broken_str_returns_fallback():
    assert compress_exception(BrokenStr()) == (
        "BrokenStr @ -: unprintable error | hint: inspect state, then retry with a smaller edit"
    )


def test_exploding_attribute_does_not_raise():
    line = compress_exception(ExplodingNodeId("oops"))
    assert line.startswith("ExplodingNodeId @ -: oops")
    assert "\n" not in line
