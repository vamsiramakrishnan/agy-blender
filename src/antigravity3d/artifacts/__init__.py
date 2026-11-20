"""Interactive review artifacts (self-contained HTML) for the Antigravity side panel."""

from antigravity3d.artifacts.inspector import (
    THREE_JS_URL,
    THREE_JS_VERSION,
    InspectorCollider,
    InspectorLod,
    InspectorQA,
    ReviewNote,
    build_inspector,
    read_notes,
)

__all__ = [
    "THREE_JS_URL",
    "THREE_JS_VERSION",
    "InspectorCollider",
    "InspectorLod",
    "InspectorQA",
    "ReviewNote",
    "build_inspector",
    "read_notes",
]
