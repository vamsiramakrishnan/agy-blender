"""Cost/quality budget governor.

Maps an asset's complexity score to a ``SpendPlan`` (how many Best-of-N
candidates to draw, which models may run, which render quality to use) and
tracks dollar spend against that plan's hard ceiling.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

_TOLERANCE = 1e-9


@dataclass(frozen=True)
class SpendPlan:
    complexity_tier: int  # 0..3
    label: str  # "clutter" | "standard" | "hero"
    ortho_candidates: int  # Best-of-N for orthographic blueprints
    pbr_candidates: int  # Best-of-N for albedo textures
    geometry_candidates: int  # Best-of-N for geometry seeds
    builder_role: str  # a role name from models.py
    allow_director: bool  # may escalate to the director model
    paintover_loop: bool
    engine_truth: bool  # run the headless engine parity check
    reuse_trim_sheet: bool  # prefer shared trim sheet over unique textures
    final_render: str  # "preview" | "eevee" | "cycles"
    ceiling_usd: float  # hard spend ceiling for this asset


_CLUTTER = SpendPlan(
    complexity_tier=0,
    label="clutter",
    ortho_candidates=1,
    pbr_candidates=2,
    geometry_candidates=1,
    builder_role="bulk",
    allow_director=False,
    paintover_loop=False,
    engine_truth=False,
    reuse_trim_sheet=True,
    final_render="preview",
    ceiling_usd=0.15,
)

_STANDARD = SpendPlan(
    complexity_tier=2,
    label="standard",
    ortho_candidates=4,
    pbr_candidates=4,
    geometry_candidates=4,
    builder_role="builder",
    allow_director=True,
    paintover_loop=False,
    engine_truth=False,
    reuse_trim_sheet=False,
    final_render="eevee",
    ceiling_usd=0.85,
)

_HERO = SpendPlan(
    complexity_tier=3,
    label="hero",
    ortho_candidates=4,
    pbr_candidates=6,
    geometry_candidates=8,
    builder_role="builder",
    allow_director=True,
    paintover_loop=True,
    engine_truth=True,
    reuse_trim_sheet=False,
    final_render="cycles",
    ceiling_usd=2.20,
)

PLANS: dict[int, SpendPlan] = {
    0: replace(_CLUTTER, complexity_tier=0),
    1: replace(_CLUTTER, complexity_tier=1),
    2: _STANDARD,
    3: _HERO,
}

_KINDS = {"ortho": "ortho_candidates", "pbr": "pbr_candidates", "geometry": "geometry_candidates"}


def plan_for(complexity_score: float) -> SpendPlan:
    """Return the plan for an expected complexity score in [0, 3].

    The score is rounded half up to the nearest integer tier and clamped to
    0..3. NaN is rejected.
    """
    score = float(complexity_score)
    if math.isnan(score):
        raise ValueError("complexity_score must not be NaN")
    clamped = min(3.0, max(0.0, score))
    tier_index = int(math.floor(clamped + 0.5))
    return PLANS[min(3, max(0, tier_index))]


class Governor:
    """Tracks spend for one asset against its plan's ceiling."""

    def __init__(self, plan: SpendPlan) -> None:
        self._plan = plan
        self._by_category: dict[str, float] = {}

    @property
    def plan(self) -> SpendPlan:
        return self._plan

    def charge(self, category: str, usd: float) -> None:
        amount = float(usd)
        if math.isnan(amount) or math.isinf(amount):
            raise ValueError(f"charge must be finite, got {usd!r}")
        if amount < 0:
            raise ValueError(f"charge must be non-negative, got {usd!r}")
        self._by_category[category] = self._by_category.get(category, 0.0) + amount

    @property
    def spent(self) -> float:
        return sum(self._by_category.values())

    @property
    def by_category(self) -> dict[str, float]:
        return dict(self._by_category)

    @property
    def remaining(self) -> float:
        return max(0.0, self._plan.ceiling_usd - self.spent)

    def can_afford(self, usd: float) -> bool:
        return self.spent + usd <= self._plan.ceiling_usd + _TOLERANCE

    def candidates(self, kind: str, *, unit_cost: float | None = None) -> int:
        """Best-of-N for ``kind``, reduced so N * unit_cost fits the remaining budget.

        Without ``unit_cost`` the plan's N is returned unchanged. The result is
        never below 1, even when a single candidate does not fit.
        """
        if kind not in _KINDS:
            raise ValueError(f"unknown candidate kind {kind!r}; expected one of {sorted(_KINDS)}")
        base = getattr(self._plan, _KINDS[kind])
        if unit_cost is None:
            return base
        if unit_cost < 0:
            raise ValueError(f"unit_cost must be non-negative, got {unit_cost!r}")
        if unit_cost == 0:
            return base
        fitting = int(math.floor((self.remaining + _TOLERANCE) / unit_cost))
        return max(1, min(base, fitting))

    def may_escalate_to_director(self, estimated_usd: float) -> bool:
        return self._plan.allow_director and self.can_afford(estimated_usd)

    def summary(self) -> str:
        parts = ", ".join(f"{name} ${amount:.4f}" for name, amount in sorted(self._by_category.items()))
        return f"{self._plan.label}: ${self.spent:.4f} of ${self._plan.ceiling_usd:.2f} ({parts})"


def usage_cost_usd(
    prompt_tokens: int,
    output_tokens: int,
    *,
    usd_per_m_in: float,
    usd_per_m_out: float,
) -> float:
    """Dollar cost of one model call given token counts and per-million-token prices."""
    if prompt_tokens < 0 or output_tokens < 0:
        raise ValueError("token counts must be non-negative")
    return (prompt_tokens * usd_per_m_in + output_tokens * usd_per_m_out) / 1_000_000
