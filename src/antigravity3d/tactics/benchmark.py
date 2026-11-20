"""Compare paired prop runs against studio labels and reconciled provider usage."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    report: str
    first_pass: bool
    accepted: bool
    rework_minutes: float = Field(ge=0)
    billed_usd: float = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def check_acceptance(self):
        if self.first_pass and (not self.accepted or self.rework_minutes > 0):
            raise ValueError("first_pass requires final acceptance and zero rework")
        return self


class Pair(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    brief_id: str = Field(min_length=1)
    single: Observation
    governed: Observation


class Cohort(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    ceiling_usd: float = Field(gt=0)
    pairs: list[Pair] = Field(min_length=1)


def _summary(observations: list[Observation], wall: list[float]) -> dict:
    accepted = sum(x.accepted for x in observations)
    spend = sum(x.billed_usd for x in observations)
    return {
        "accepted": accepted, "first_pass_rate": sum(x.first_pass for x in observations) / len(observations),
        "rework_minutes": sum(x.rework_minutes for x in observations),
        "billed_usd": spend, "usd_per_accepted": spend / accepted if accepted else None,
        "input_tokens": sum(x.input_tokens for x in observations), "output_tokens": sum(x.output_tokens for x in observations),
        "wall_p50_ms": float(np.percentile(wall, 50)), "wall_p95_ms": float(np.percentile(wall, 95)),
    }


def compare(path: Path) -> dict:
    cohort = Cohort.model_validate_json(path.read_bytes())
    seen_ids: set[str] = set()
    seen_reports: set[Path] = set()
    times: dict[str, list[float]] = {"single": [], "governed": []}
    for pair in cohort.pairs:
        if pair.brief_id in seen_ids:
            raise ValueError(f"duplicate brief {pair.brief_id}")
        seen_ids.add(pair.brief_id)
        reference = None
        modes: tuple[Literal["single", "governed"], ...] = ("single", "governed")
        for mode in modes:
            obs = getattr(pair, mode)
            report_path = (path.parent / obs.report).resolve()
            if report_path in seen_reports:
                raise ValueError(f"reused report {obs.report}")
            seen_reports.add(report_path)
            run = json.loads(report_path.read_bytes())
            if run.get("candidate_mode") != mode or run.get("decision_backend") != "decisions":
                raise ValueError(f"{obs.report} requires a live native Decisions run in {mode} mode")
            conditions = run.get("comparison")
            if not isinstance(conditions, dict) or not conditions.get("strict"):
                raise ValueError(f"{obs.report} requires the verified production configuration")
            key = (run["prompt"], run["engine"], run["budget"], conditions)
            if reference is not None and key != reference:
                raise ValueError(f"unmatched prompt, engine budget or run configuration for {pair.brief_id}")
            reference = key
            if obs.first_pass and not run.get("verified"):
                raise ValueError(f"{obs.report} first-pass label requires engine-verified output")
            ms = run["wall_ms"]
            if not isinstance(ms, (int, float)) or not np.isfinite(ms) or ms <= 0:
                raise ValueError(f"{obs.report} has invalid wall time")
            times[mode].append(ms)
    single = _summary([p.single for p in cohort.pairs], times["single"])
    governed = _summary([p.governed for p in cohort.pairs], times["governed"])
    gain = governed["first_pass_rate"] - single["first_pass_rate"]
    rework = 1 - governed["rework_minutes"] / single["rework_minutes"] if single["rework_minutes"] else None
    within = all(p.governed.billed_usd <= cohort.ceiling_usd for p in cohort.pairs)
    return {"briefs": len(cohort.pairs), "single": single, "governed": governed, "first_pass_gain_pp": 100 * gain,
            "rework_reduction_pct": None if rework is None else 100 * rework, "within_ceiling": within,
            "rollout_pass": len(cohort.pairs) >= 50 and within and (gain >= .10 - 1e-12 or (rework is not None and rework >= .20 - 1e-12))}
