"""Geometry intent shared by one-shot authors and native stage agents.

BLOCKOUT is a toolset, not a promise that every scene built with it is a finished prop. Proposal entrypoints select
production props or metric-led kit pieces; callers of the intermediate stage keep a coarse blockout by default.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class AuthoringIntent(StrEnum):
    PRODUCTION_PROP = "production_prop"
    KIT_PIECE = "kit_piece"
    BLOCKOUT = "blockout"

    @property
    def subject(self) -> str:
        return {self.PRODUCTION_PROP: "production game prop", self.KIT_PIECE: "modular kit piece",
                self.BLOCKOUT: "intermediate blockout"}[self]


# Keep the one-shot request byte-identical: its output allowance and recorded measurements were sized for this text.
# The native production-prop stage uses the same requirements, with its own patch/QA workflow around them. This is
# guidance, not an acceptance test: part and triangle counts alone cannot establish artistic completeness.
PROP_CRAFT_GUIDANCE = (
    "Model a production game prop, not a blockout.\n"
    "- Every part the brief names gets its own nodes, built from the pieces a real one has: a latch is a base plate, a clasp "
    "and a hinge barrel; a handle is two brackets and a grip; a corner guard wraps its corner.\n"
    "- Add the secondary forms an object of this kind has wherever they change the silhouette or catch the light: rims, ribs, "
    "frames, skids or feet, straps, hinges, bolt heads. Break large flat faces with raised or recessed panels made from thin "
    "boxes.\n"
    "- Aim for 50 to 90 parts, within max_tris: a box is 12 triangles, a cylinder 4 x segments (8 to 12 segments is enough "
    "for a small part), a sphere or capsule costs hundreds. Do not pad with hidden or repeated filler.\n"
    "- Keep parts in proportion: thin plates and slim bars, not chunky blocks.\n"
    "- Attach by contact: a part's face lies exactly on the surface it is mounted on, with no gap and no overlap (hidden "
    "contact faces are removed at export). Two parts mounted on the same surface must not share area: where one crosses "
    "another, mount it on the other's face.\n"
    "- Set collider false on small detail parts; keep it for the main solids."
)


def stage_geometry_guidance(intent: AuthoringIntent | str, *, repair: bool = False) -> str:
    """Requirements for a geometry session, independent of the tools it is allowed to use."""
    intent = AuthoringIntent(intent)
    if repair:
        return (f"Repair the existing {intent.subject}. Preserve correct geometry and its level of detail; make only the "
                "requested corrections. Add missing named parts only when the QA reasons request them. "
                "Keep any supplied kit metric, mating surfaces and openings unchanged.")
    if intent is AuthoringIntent.PRODUCTION_PROP:
        return PROP_CRAFT_GUIDANCE
    if intent is AuthoringIntent.KIT_PIECE:
        return ("Build a modular kit piece to the supplied metric and any rejection feedback. Preserve exact dimensions, "
                "mating surfaces, openings and pivots; keep detail within the metric's relief and face-size limits. "
                "The metric determines the required geometry; no prop part-count target applies.")
    return ("Build an intermediate blockout: establish the primary forms, proportions, ground contact and silhouette. "
            "Defer secondary construction detail and surface decoration to the production modelling stage.")


def repair_scope(reasons: Any) -> tuple[str, Any, str | None]:
    """The same localized or brief-completeness repair contract for JSON and tool-driven authors."""
    from antigravity3d.decisions.compose import BRIEF_REASON

    brief = [r for r in (reasons or []) if isinstance(r, str) and r.startswith(BRIEF_REASON)] if isinstance(reasons, list) else []
    if not brief:
        return "correcting only the flagged nodes", reasons, None
    return ("correcting the flagged nodes and adding nodes for every part the requested asset names that is missing or "
            "unrecognisable, plus the secondary detail a finished one has; keep the nodes that are already right",
            [r for r in reasons if r not in brief], brief[0][len(BRIEF_REASON):])
