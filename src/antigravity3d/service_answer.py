"""What a failed model call's answer says about the request, and the one retry schedule for authoring, judge and image calls.

Three outcomes (:func:`classify`):

* ``not_served``: the service said it did not run the request: 503 UNAVAILABLE or 429 RESOURCE_EXHAUSTED, with the
  service's own status string (a bare proxy 503, or an error that carries only a code, is not enough), AND the answer came
  back within ``NOT_SERVED_MAX_S``. Measured on every live relay log of 2026-11-21: the fastest *served*
  ``generateContent`` took 7.02 s (23 calls); the 503 that lost run r2_crate_C came back in 0.11 s. An attempt that was not
  served books nothing. A slower answer with the same status might have followed work the service did (a backend
  dying mid-request), so it is ``unknown``.
* ``unknown``: the request may have run: a 500 / 502, a not-served status that came back slowly, a broken connection
  (httpx's ``TransportError`` family, recognised by class name so nothing here depends on the SDK's transport) or a
  timeout that came back well before its own deadline, or a stream that went silent past its idle limit
  (:class:`StreamStalled`: the far end stopped sending; no deadline ran out). The attempt keeps its whole reservation and may be retried,
  within a smaller count, because every retry holds another full reservation.
* ``final``: anything else: a client error (4xx other than 429) is a bug in the request; a client-side timeout, or a
  504 DEADLINE_EXCEEDED that came back at the request's own deadline, ran the whole deadline (sent again it would hold
  a second reservation for the same outcome). A 504 that came back early, or from a request that carried no deadline
  of its own, is ``unknown``. (The judge plane keeps retrying a timeout of a request that carried no deadline of its own,
  as it always did: ``decisions.engine``.)

The schedule (:class:`RetryPolicy`): waits of 2, 4, 8, 16, 32 s. The message of the 503 says spikes in demand are
"usually temporary"; a preview run takes minutes, so up to a minute of waiting on one call (62 s) is proportionate,
where the judge's former 0.5 + 1 + 2 = 3.5 s was not. A 429 that names a ``retryDelay`` waits at least that long, each
wait capped at the last step (32 s). Not-served attempts are retried up to 5 times (they cost no tokens); an unknown
outcome up to ``unknown_retries`` times (the caller's choice: each retry keeps another reservation).
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

Outcome = Literal["not_served", "unknown", "final"]

NOT_SERVED: frozenset[tuple[int, str]] = frozenset({(503, "UNAVAILABLE"), (429, "RESOURCE_EXHAUSTED")})
NOT_SERVED_MAX_S = 2.0
UNKNOWN_CODES = frozenset({408, 500, 502, 503, 429})  # 503 / 429 only when slow or without the service's status
DEFAULT_DELAYS_S = (2.0, 4.0, 8.0, 16.0, 32.0)
# Class names of transport failures (httpx: TransportError and its NetworkError / ProtocolError / ConnectError /
# ReadError / WriteError / RemoteProtocolError family; httpcore's equivalents). Checked over the MRO by name.
_TRANSPORT_NAMES = ("TransportError", "NetworkError", "ProtocolError", "ConnectError", "ReadError", "WriteError",
                    "RemoteProtocolError", "LocalProtocolError", "ConnectionError", "ServerDisconnectedError")


class OutcomeUnknown(Exception):
    """A call made of several dispatches failed after at least one of them was served (the fallback judge's chunks):
    the outcome of the whole call is unknown, so its reservation is kept. Wraps the first failure."""

    def __init__(self, message: str, *, cause: BaseException, served: int, failed: int, turned_away: int) -> None:
        super().__init__(message)
        # dispatches that returned an answer, that failed, and (of those that failed) that the service turned away
        self.cause, self.served, self.failed, self.turned_away = cause, served, failed, turned_away


class StreamStalled(TimeoutError):
    """A streamed call went silent: no chunk for longer than its idle limit (``tactics.stream_call``). A timeout
    (``is_timeout``), but not a spent deadline: the far end stopped sending, as a broken connection does, so the outcome
    is ``unknown`` (the attempt keeps its reservation and may be retried) and never ``final``. ``seen``: what arrived
    before the silence (seconds to the first chunk, seconds of silence, characters received, last usage seen)."""

    def __init__(self, message: str, *, seen: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.seen = dict(seen or {})


def _get(obj: Any, name: str) -> Any:
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def code_status(exc: BaseException) -> tuple[int | None, str | None]:
    """The HTTP code and the service's status string of an API error (``google.genai.errors.APIError`` shape)."""
    code = _get(exc, "code") or _get(exc, "status_code")
    status = _get(exc, "status")
    if isinstance(status, int) and not isinstance(code, int):
        code, status = status, None
    return (code if isinstance(code, int) else None), (status if isinstance(status, str) else None)


def is_transport(exc: BaseException) -> bool:
    """A connection-level failure: the request may or may not have reached the model, and no answer came back."""
    return any(cls.__name__ in _TRANSPORT_NAMES for cls in type(exc).__mro__)


def is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    if any("Timeout" in cls.__name__ for cls in type(exc).__mro__):
        return True
    code, status = code_status(exc)
    return code == 504 or status == "DEADLINE_EXCEEDED"


def classify(exc: BaseException, elapsed_s: float, *, deadline_s: float | None = None) -> Outcome:
    """See the module docstring. ``elapsed_s``: how long this one attempt took; ``deadline_s``: the deadline the request
    carried, if any (a timeout that ran it out is final)."""
    if isinstance(exc, (OutcomeUnknown, StreamStalled)):
        return "unknown"
    if not isinstance(exc, Exception):
        return "final"  # cancellation, KeyboardInterrupt
    code, status = code_status(exc)
    if (code, status) in NOT_SERVED and elapsed_s < NOT_SERVED_MAX_S:
        return "not_served"
    if is_timeout(exc):
        if code != 504 and status != "DEADLINE_EXCEEDED":
            # A client-side timeout: a deadline fired (httpx applies the request's timeout to each phase, so it fires at
            # the deadline, not before). Sent again it would hold a second reservation for the same outcome.
            return "final"
        ran_out = deadline_s is not None and elapsed_s >= 0.9 * float(deadline_s)
        return "final" if ran_out else "unknown"  # an early 504 comes from a gateway, not from the request's deadline
    if code in UNKNOWN_CODES:
        return "unknown"
    if is_transport(exc):
        return "unknown"
    return "final"


def describe(exc: BaseException, limit: int = 240) -> str:
    """The service's answer in one line: ``503 UNAVAILABLE: <message>`` for an API error, else the exception."""
    code, status = code_status(exc)
    message = _get(exc, "message")
    if code is not None and isinstance(message, str) and message:
        text = f"{code} {status or ''}: {message}".replace(" :", ":")
    else:
        text = f"{type(exc).__name__}: {' '.join(str(exc).split())}".rstrip(": ")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def retry_delay_hint(exc: BaseException) -> float | None:
    """A 429's ``RetryInfo.retryDelay`` (``"17s"``), when the service named one."""
    details = _get(exc, "details")
    if isinstance(details, str):
        try:
            details = json.loads(details)
        except ValueError:
            return None
    for item in _get(_get(details, "error") or {}, "details") or []:
        if isinstance(item, dict) and str(item.get("@type", "")).endswith("RetryInfo"):
            m = re.fullmatch(r"\s*([0-9.]+)s\s*", str(item.get("retryDelay", "")))
            if m:
                return float(m.group(1))
    return None


@dataclass
class Attempt:
    """One rejected or failed attempt, as receipts and notes record it."""

    attempt: int
    outcome: Outcome
    answer: str
    elapsed_s: float
    wait_s: float | None = None  # the wait before the next attempt (None: no next attempt)

    def to_dict(self) -> dict[str, Any]:
        return {"attempt": self.attempt, "outcome": self.outcome, "answer": self.answer, "elapsed_s": round(self.elapsed_s, 3),
                "wait_s": self.wait_s}


@dataclass
class RetryPolicy:
    """The shared schedule. ``sleep`` is injectable so tests never wait for real."""

    delays_s: tuple[float, ...] = DEFAULT_DELAYS_S
    not_served_retries: int = len(DEFAULT_DELAYS_S)
    unknown_retries: int = 1
    sleep: Callable[[float], Awaitable[Any]] = field(default=asyncio.sleep, repr=False)

    def delay(self, retry: int, exc: BaseException | None = None) -> float:
        """The wait before retry number ``retry`` (1-based)."""
        step = self.delays_s[min(retry, len(self.delays_s)) - 1] if self.delays_s else 0.0
        hint = retry_delay_hint(exc) if exc is not None else None
        if hint is not None:
            step = min(max(step, hint), max(self.delays_s, default=step))
        return step

    def allows(self, outcome: Outcome, *, not_served: int, unknown: int) -> bool:
        """May another attempt follow, after ``not_served`` / ``unknown`` such failures so far (this one included)?"""
        if outcome == "not_served":
            return not_served <= self.not_served_retries
        if outcome == "unknown":
            return unknown <= self.unknown_retries
        return False

    @property
    def max_wait_s(self) -> float:
        return float(sum(self.delays_s[: self.not_served_retries]))

    def with_unknown_retries(self, n: int) -> RetryPolicy:
        return RetryPolicy(self.delays_s, self.not_served_retries, n, self.sleep)


NO_RETRY = RetryPolicy(delays_s=(), not_served_retries=0, unknown_retries=0)


class Tracker:
    """Counts the failures of one call and decides, records and waits for each retry."""

    def __init__(self, policy: RetryPolicy) -> None:
        self.policy, self.attempts = policy, []  # type: list[Attempt]
        self.not_served = self.unknown = 0

    @property
    def n(self) -> int:
        return len(self.attempts) + 1  # the number of the attempt about to be made

    def decide(self, exc: BaseException, elapsed_s: float, outcome: Outcome) -> float | None:
        """Record a failed attempt; the wait before the next one, or None when no other may follow."""
        if outcome == "not_served":
            self.not_served += 1
        elif outcome == "unknown":
            self.unknown += 1
        a = Attempt(len(self.attempts) + 1, outcome, describe(exc), elapsed_s)
        self.attempts.append(a)
        if not self.policy.allows(outcome, not_served=self.not_served, unknown=self.unknown):
            return None
        a.wait_s = self.policy.delay(self.not_served + self.unknown, exc)
        return a.wait_s

    async def failed(self, exc: BaseException, elapsed_s: float, outcome: Outcome) -> bool:
        """Record a failed attempt; wait and return True when another may follow."""
        wait = self.decide(exc, elapsed_s, outcome)
        if wait is None:
            return False
        await self.policy.sleep(wait)
        return True

    @property
    def waited_s(self) -> float:
        return float(sum(a.wait_s or 0.0 for a in self.attempts))

    def record(self) -> list[dict[str, Any]]:
        return [a.to_dict() for a in self.attempts]

    def summary(self) -> str:
        if not self.attempts:
            return ""
        rejected = sum(a.outcome == "not_served" for a in self.attempts)
        return (f"{len(self.attempts)} failed attempt(s) ({rejected} not served, nothing booked for those), "
                f"waited {self.waited_s:.0f} s: " + "; ".join(f"#{a.attempt} {a.answer[:80]}" for a in self.attempts[:3]))


DEFAULT_POLICY = RetryPolicy()


def not_served_receipt(exc: BaseException, attempt: int, elapsed_s: float) -> dict[str, Any]:
    """The receipt fields of an attempt that books nothing, and why."""
    code, status = code_status(exc)
    return {"status": "not_served", "attempt": attempt, "http_code": code, "service_status": status,
            "elapsed_s": round(elapsed_s, 3), "booked_reason": f"the service answered {code} {status} in {elapsed_s:.2f} s "
            f"(under {NOT_SERVED_MAX_S:.0f} s): the request was not run, nothing is booked"}
