"""Plain, SDK-independent result types for the decision control plane."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Stage = Literal["blockout", "lookdev", "final"]
Kind = Literal["ortho", "geometry", "pbr"]


class DecisionError(RuntimeError):
    """Base class for control-plane failures."""


class PromptBlockedError(DecisionError):
    """The input tripped a content filter (`prompt_feedback.block_reason`); no answers exist."""

    def __init__(self, reason: str, message: str | None = None, tag: str = "") -> None:
        super().__init__(f"decision input blocked ({reason}) [{tag}]: {message or ''}".strip())
        self.reason, self.detail, self.tag = reason, message, tag


class TransientDecisionError(DecisionError):
    """Raised after retries are exhausted on a retryable failure."""


@dataclass(frozen=True)
class Answer:
    kind: Literal["noul", "choice", "score"]
    confidence: float
    noul: float | None = None  # P(true)
    choice: str | None = None
    score: float | None = None  # expected level
    probabilities: dict[str, float] = field(default_factory=dict)
    legend: dict[str, str] = field(default_factory=dict)

    @property
    def value(self) -> float | str:
        return {"noul": self.noul, "choice": self.choice, "score": self.score}[self.kind]  # type: ignore[return-value]


@dataclass(frozen=True)
class DecisionResult:
    tag: str
    answers: dict[str, Answer]
    latency_ms: float
    input_tokens: int
    est_input_tokens: int
    est_cost_usd: float
    n_questions: int = 0
    attempts: int = 1

    def noul(self, qid: str) -> float:
        return self.answers[qid].noul  # type: ignore[return-value]

    def score(self, qid: str) -> float:
        return self.answers[qid].score  # type: ignore[return-value]

    def choice(self, qid: str) -> str:
        return self.answers[qid].choice  # type: ignore[return-value]

    def by_prefix(self, prefix: str) -> dict[str, float]:
        """Scalar view (P(true) or expected score) of every answer whose id starts with prefix."""
        return {q: a.value for q, a in self.answers.items() if q.startswith(prefix) and a.kind != "choice"}  # type: ignore[misc]


@dataclass(frozen=True)
class RouteDecision:
    pipeline: str
    pipeline_probs: dict[str, float]
    needs_ortho: bool
    needs_ortho_p: float
    complexity: float  # expected tier 0-3
    complexity_tier: int
    confidence: float
    result: DecisionResult


@dataclass(frozen=True)
class VisualQAResult:
    stage: Stage
    defects: dict[str, float]  # defect_* -> P(true)
    scores: dict[str, float]  # score_* -> expected 0-4
    next_action: str | None
    next_action_probs: dict[str, float]
    skipped: tuple[str, ...]  # ids not asked (stage / missing images)
    result: DecisionResult
    resolution: str = "high"


@dataclass(frozen=True)
class CandidateScore:
    index: int
    uri: str
    kind: str
    defects: dict[str, float] = field(default_factory=dict)
    scores: dict[str, float] = field(default_factory=dict)
    blocked: bool = False
    utility: float | None = None
    result: DecisionResult | None = None


@dataclass(frozen=True)
class PolicyVerdict:
    allowed: bool
    needs_review: bool
    flags: dict[str, float]
    blocked_by_filter: bool = False
    reasons: tuple[str, ...] = ()


@dataclass
class PlaneStats:
    calls: int = 0
    input_tokens: int = 0
    cost_usd: float = 0.0
    retries: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    by_tag: dict[str, int] = field(default_factory=dict)

    def record(self, tag: str, latency_ms: float, tokens: int, cost: float, attempts: int) -> None:
        self.calls += 1
        self.input_tokens += tokens
        self.cost_usd += cost
        self.retries += attempts - 1
        self.latencies_ms.append(latency_ms)
        self.by_tag[tag] = self.by_tag.get(tag, 0) + 1

    def percentile(self, q: float) -> float:
        if not self.latencies_ms:
            return 0.0
        import numpy as np

        return float(np.percentile(self.latencies_ms, q))

    def summary(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "cost_usd": round(self.cost_usd, 8),
            "retries": self.retries,
            "p50_ms": self.percentile(50),
            "p95_ms": self.percentile(95),
            "by_tag": dict(self.by_tag),
        }
