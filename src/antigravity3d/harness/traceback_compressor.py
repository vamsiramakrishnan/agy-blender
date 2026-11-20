"""Squeeze an exception into one short line for LLM tool results.

Output format::

    <ErrorClass> @ <node_id>: <root cause, one line> | hint: <recovery hint>

This module is a pure function: no I/O and no third-party imports. It never
raises; if anything goes wrong while compressing, a fixed fallback line is
returned instead.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

__all__ = ["compress_exception", "HINT_RULES", "DEFAULT_HINT"]

ELLIPSIS = "…"
DEFAULT_HINT = "inspect state, then retry with a smaller edit"

_DEFAULT_MAX_LEN = 240
_NODE_ID_MAX = 64
_CHAIN_MAX = 50

HintPredicate = Callable[[BaseException, str], bool]


def _named(exc: BaseException, *names: str) -> bool:
    """True if any class in the exception's MRO has one of ``names``.

    Matching by name lets us recognise third-party classes (for example
    pydantic's ``ValidationError``) without importing them.
    """
    return any(cls.__name__ in names for cls in type(exc).__mro__)


# Ordered (predicate, hint) table. The first matching predicate wins.
# Predicates receive the root exception and its lower-cased message.
HINT_RULES: tuple[tuple[HintPredicate, str], ...] = (
    (
        lambda e, m: _named(e, "ValidationError"),
        "fix the IR fields named above and resend",
    ),
    (
        lambda e, m: _named(e, "KeyError") or "unknown node" in m,
        "call inspect_manifest to list valid node ids",
    ),
    (
        lambda e, m: "non_manifold" in m or "non-manifold" in m or "watertight" in m,
        "close open edges or replace the part with a primitive",
    ),
    (
        lambda e, m: "tris" in m and ">" in m,
        "reduce segments or remove detail to meet the triangle budget",
    ),
    (
        lambda e, m: _named(e, "TimeoutError"),
        "retry once; if it repeats, simplify the request",
    ),
    (
        lambda e, m: _named(e, "UnsafeScriptError"),
        "remove the blocked import/call; use the IR tools instead of raw scripts",
    ),
    (
        lambda e, m: _named(e, "FileNotFoundError"),
        "check the path with inspect_manifest",
    ),
    (
        lambda e, m: _named(e, "ValueError"),
        "check parameter ranges",
    ),
)

# Node-id patterns, tried in order. The flag marks patterns whose candidates
# are filtered against common English words (bare "node is ..." etc.).
_NODE_PATTERNS: tuple[tuple[re.Pattern[str], bool], ...] = (
    (re.compile(r"""\bnode\s+['"]([^'"\s]+)['"]"""), False),  # node 'lid' / "lid"
    (re.compile(r"\bnode\s+([A-Za-z_][\w.:\-]*)"), True),  # node lid
    (re.compile(r"(?:^|[\s(\[])@([A-Za-z_][\w.\-]*)"), False),  # @lid
    # USD prim path /crate/lid -> lid (identifier segments only)
    (re.compile(r"(?<![\w/.])/(?:[A-Za-z_]\w*/)+([A-Za-z_]\w*)(?![\w./-])"), False),
)

_NON_IDS = frozenset(
    {
        "a", "an", "and", "as", "at", "be", "by", "did", "does", "error", "exists",
        "for", "found", "from", "has", "have", "id", "ids", "if", "in", "is", "it",
        "list", "missing", "must", "named", "no", "not", "of", "on", "or", "should",
        "the", "this", "to", "type", "types", "unknown", "was", "when", "with",
    }
)

_ADDRESS = re.compile(r"\b0x[0-9A-Fa-f]{6,16}\b")
# Absolute paths with at least two separators, e.g. /a/b or /home/user/x.py.
_ABS_PATH = re.compile(r"(?<![\w.~])(?:/[\w.+@%~\-]+){2,}")


def compress_exception(exc: BaseException, *, max_len: int = _DEFAULT_MAX_LEN) -> str:
    """Return a single line describing ``exc`` in at most ``max_len`` characters."""
    try:
        limit = max(1, int(max_len))
    except Exception:
        limit = _DEFAULT_MAX_LEN
    try:
        return _compress(exc, limit)
    except Exception:
        return _fallback(exc, limit)


def _compress(exc: BaseException, limit: int) -> str:
    chain = _cause_chain(exc)
    messages = [str(e) for e in chain]
    root = chain[-1]
    root_message = messages[-1]

    cls = type(root).__name__
    node = _node_id(chain, messages)
    cause = _root_cause(root_message)
    hint = _hint_for(root, root_message.lower())
    return _assemble(cls, node, cause, hint, limit)


def _fallback(exc: BaseException, limit: int) -> str:
    try:
        name = type(exc).__name__
    except Exception:
        name = "Exception"
    return _fit(f"{name} @ -: unprintable error | hint: {DEFAULT_HINT}", limit)


def _cause_chain(exc: BaseException) -> list[BaseException]:
    """Return [exc, exc.__cause__, ...] ending at the innermost cause."""
    chain = [exc]
    seen = {id(exc)}
    current = exc
    while len(chain) < _CHAIN_MAX:
        nxt = current.__cause__
        if nxt is None or id(nxt) in seen:
            break
        seen.add(id(nxt))
        chain.append(nxt)
        current = nxt
    return chain


def _node_id(chain: Sequence[BaseException], messages: Sequence[str]) -> str:
    for exc in chain:
        try:
            value = getattr(exc, "node_id", None)
            if value is None:
                continue
            text = " ".join(str(value).split())
        except Exception:
            continue
        if text:
            return _fit(text, _NODE_ID_MAX)
    found = _find_node(messages)
    return _fit(found, _NODE_ID_MAX) if found else "-"


def _find_node(messages: Sequence[str]) -> str | None:
    for pattern, filtered in _NODE_PATTERNS:
        for message in messages:
            for match in pattern.finditer(message):
                candidate = match.group(1)
                if filtered and candidate.lower() in _NON_IDS:
                    continue
                return candidate
    return None


def _shorten_path(match: re.Match[str]) -> str:
    parts = match.group(0).split("/")[1:]
    base = parts[-1]
    # Keep short two-part identifiers such as /crate/lid untouched.
    if len(parts) >= 3 or "." in base:
        return base
    return match.group(0)


def _root_cause(message: str) -> str:
    line = next((ln for ln in message.splitlines() if ln.strip()), "")
    line = _ADDRESS.sub("0x" + ELLIPSIS, line)
    line = _ABS_PATH.sub(_shorten_path, line)
    return " ".join(line.split()) or "no message"


def _hint_for(exc: BaseException, message_lower: str) -> str:
    for predicate, hint in HINT_RULES:
        try:
            if predicate(exc, message_lower):
                return hint
        except Exception:
            continue
    return DEFAULT_HINT


def _fit(text: str, limit: int) -> str:
    """Truncate ``text`` to ``limit`` characters, ending with an ellipsis."""
    if len(text) <= limit:
        return text
    if limit <= 0:
        return ""
    return text[: limit - 1].rstrip() + ELLIPSIS


def _assemble(cls: str, node: str, cause: str, hint: str, limit: int) -> str:
    head = f"{cls} @ {node}: "
    tail = f" | hint: {hint}"
    line = head + cause + tail
    if len(line) <= limit:
        return line
    # Shorten only the root cause; class, node id and hint stay intact.
    budget = limit - len(head) - len(tail)
    if budget >= 1:
        return head + _fit(cause, budget) + tail
    # Pathologically small limit: keep as much of the skeleton as fits.
    return _fit(f"{cls} @ {node} | hint: {hint}", limit)
