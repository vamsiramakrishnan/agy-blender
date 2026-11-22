"""Plain, SDK-independent result types for the decision control plane."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Stage = Literal["blockout", "lookdev", "final", "look"]  # "look": tactics.look_round (decisions.questions.LOOK)
Kind = Literal["ortho", "geometry", "pbr"]


class DecisionError(RuntimeError):
    """Base class for control-plane failures."""


class PromptBlockedError(DecisionError):
    """The input tripped a content filter (`prompt_feedback.block_reason`); no answers exist."""

    def __init__(self, reason: str, message: str | None = None, tag: str = "", *, result: DecisionResult | None = None) -> None:
        super().__init__(f"decision input blocked ({reason}) [{tag}]: {message or ''}".strip())
        self.reason, self.detail, self.tag = reason, message, tag
        self.result = result


class DecisionBudgetError(ValueError):
    """The complete request exceeds the pre-dispatch input budget."""


class TransientDecisionError(DecisionError):
    """Raised after retries are exhausted on a retryable failure."""


@dataclass(frozen=True)
class Answer:
    kind: Literal["noul", "choice", "score", "text"]
    confidence: float
    noul: float | None = None  # P(true)
    choice: str | None = None
    score: float | None = None  # expected level
    # "text": a sentence of the judge's own (the reason of a pairwise comparison, engine.compare_states); asked only of
    # the generative fallback (fallback.py): the native Decisions API has no such answer type
    text: str | None = None
    probabilities: dict[str, float] = field(default_factory=dict)
    legend: dict[str, str] = field(default_factory=dict)

    @property
    def value(self) -> float | str:
        return {"noul": self.noul, "choice": self.choice, "score": self.score, "text": self.text}[self.kind]  # type: ignore[return-value]


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
    # Provenance. calibrated=False means the probabilities are a generative model's self-report
    # (see fallback.py), not Decisions API output: compose.py will not auto-approve on them.
    calibrated: bool = True
    output_tokens: int = 0
    cost_known: bool = True  # False: est_cost_usd is 0.0 only because no price is configured
    source: str = "decisions"  # "decisions" | "generative_fallback" | "fake"

    booked_usd: float = 0.0  # dollars settled against the caller's asset governor (0.0 on a token ledger)
    booked_tokens: int = 0  # tokens settled against a token ledger (Governor(token_ceiling=...)); no price applied

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
    policy: PolicyVerdict | None = None
    error: str | None = None
    billed_on_error: float = 0.0


@dataclass(frozen=True)
class PolicyVerdict:
    allowed: bool
    needs_review: bool
    flags: dict[str, float]
    blocked_by_filter: bool = False
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class EngineParityResult:
    """DCC render vs target-engine render of the same asset."""

    defects: dict[str, float]  # defect_* -> P(true)
    scores: dict[str, float]  # score_engine_parity -> expected 0-4
    result: DecisionResult
    skipped: tuple[str, ...] = ()
    resolution: str = "high"


@dataclass(frozen=True)
class ItemQA:
    """Answers for one item (LOD pair / rig pose) of a multi-item sheet."""

    index: int  # 1-based ordinal as written in the questions
    label: str
    defects: dict[str, float] = field(default_factory=dict)  # defect_* -> P(true)
    scores: dict[str, float] = field(default_factory=dict)  # score_* -> expected 0-4


@dataclass(frozen=True)
class ItemizedQAResult:
    items: list[ItemQA]
    result: DecisionResult
    resolution: str = "high"


@dataclass(frozen=True)
class LodPopResult(ItemizedQAResult):
    """One ItemQA per LOD(k) -> LOD(k+1) pair."""


@dataclass(frozen=True)
class RigRomResult(ItemizedQAResult):
    """One ItemQA per range-of-motion pose."""


@dataclass
class PlaneStats:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    uncalibrated_calls: int = 0
    unknown_cost_calls: int = 0
    measured_knee_overruns: int = 0
    cost_usd: float = 0.0
    retries: int = 0
    hedges: int = 0  # design judgements asked a second time because the first ran long (engine.HEDGE_AFTER_S)
    latencies_ms: list[float] = field(default_factory=list)
    by_tag: dict[str, int] = field(default_factory=dict)

    def record(self, tag: str, latency_ms: float, tokens: int, cost: float, attempts: int, *,
               output_tokens: int = 0, calibrated: bool = True, cost_known: bool = True) -> None:
        self.calls += 1
        self.input_tokens += tokens
        self.output_tokens += output_tokens
        self.uncalibrated_calls += not calibrated
        self.unknown_cost_calls += not cost_known
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
            "output_tokens": self.output_tokens,
            "uncalibrated_calls": self.uncalibrated_calls,
            "unknown_cost_calls": self.unknown_cost_calls,
            "measured_knee_overruns": self.measured_knee_overruns,
            "cost_usd": round(self.cost_usd, 8),
            "retries": self.retries,
            "p50_ms": self.percentile(50),
            "p95_ms": self.percentile(95),
            "by_tag": dict(self.by_tag),
        }
