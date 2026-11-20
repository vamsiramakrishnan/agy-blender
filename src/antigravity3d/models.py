"""Model tiering: which Gemini 4 model and thinking level each role runs on.

One table, so a studio can re-tier without touching call sites. Every id is
overridable by environment variable (``AGY3D_MODEL_<ROLE>``).

Verified live against the Gemini API: ``gemini-4-hydrogen``, ``gemini-4-carbon``
and ``gemini-4-neon`` answer; ``gemini-4-argon`` exists but is slow (give it a
generous timeout). The harness bundled with google-antigravity 0.1.24 names
hydrogen, carbon and argon but not neon; set ``AGY3D_NEON_AVAILABLE=0`` to
route the bulk tier to hydrogen if a harness build rejects it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from google.antigravity import ThinkingLevel

Role = Literal["director", "builder", "worker", "bulk", "decision", "image"]

HYDROGEN = "gemini-4-hydrogen"
CARBON = "gemini-4-carbon"
NEON = "gemini-4-neon"
ARGON = "gemini-4-argon"
DECISION = "gemini-decision-1"
IMAGE = "gemini-nano-banana-2.1"


@dataclass(frozen=True)
class ModelTier:
    role: Role
    model: str
    thinking: ThinkingLevel | None
    purpose: str


_DEFAULTS: dict[Role, ModelTier] = {
    # Turn-0 planning, Geometry Nodes maths, low-confidence escalations.
    "director": ModelTier("director", ARGON, ThinkingLevel.HIGH, "plan, hard maths, ambiguous escalations"),
    # Stage agents that author IR and drive lookdev.
    "builder": ModelTier("builder", CARBON, ThinkingLevel.MEDIUM, "stage agents: blockout, lookdev, export"),
    # Isolated critics and parallel prop builders.
    "worker": ModelTier("worker", HYDROGEN, ThinkingLevel.LOW, "ephemeral critics, swarm prop builders"),
    # High-volume, low-judgement work: batch sanitise, simple parameter edits.
    # The live API rejects ThinkingLevel.MINIMAL for neon and hydrogen (HTTP 400), so LOW is the floor.
    "bulk": ModelTier("bulk", NEON, ThinkingLevel.LOW, "batch sanitise, trivial IR edits"),
    "decision": ModelTier("decision", DECISION, None, "routing, Best-of-N ranking, visual QA, policy"),
    "image": ModelTier("image", IMAGE, None, "orthos, albedo, trim sheets, paint-overs"),
}


def tier(role: Role) -> ModelTier:
    """Returns the effective tier for ``role`` after env overrides."""
    base = _DEFAULTS[role]
    model = os.environ.get(f"AGY3D_MODEL_{role.upper()}", base.model)
    if role == "image":
        model = os.environ.get("AGY3D_IMAGE_MODEL") or model
    if model == NEON and os.environ.get("AGY3D_NEON_AVAILABLE") == "0":
        model = HYDROGEN
    return ModelTier(role, model, base.thinking, base.purpose)


def all_tiers() -> list[ModelTier]:
    return [tier(r) for r in _DEFAULTS]
