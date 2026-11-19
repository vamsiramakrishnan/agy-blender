"""Build a DecisionControlPlane on whichever backend this key / project can actually use."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from . import questions as Q
from .engine import DEFAULT_MODEL, DecisionControlPlane, _get, _is_transient
from .fallback import GenerativeDecisionsBackend

log = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 10.0
# The Decisions API is unavailable *to this caller* (as opposed to broken or throttled): fall back on these.
UNAVAILABLE_CODES = {403, 404}


def _status(exc: BaseException) -> int | None:
    code = _get(exc, "code") or _get(exc, "status_code")
    return code if isinstance(code, int) else None


async def make_control_plane(client: Any = None, *, prefer: str = "decisions", model: str = DEFAULT_MODEL,
                             probe_timeout_s: float = PROBE_TIMEOUT_S, generative_model: str | None = None) -> DecisionControlPlane:
    """Probe the Decisions API once and return a plane on the best available backend.

    prefer="decisions"   probe with a trivial one-question request; on 404 / permission errors (or a transient failure
                         that survives one retry) fall back to GenerativeDecisionsBackend and log ONE warning naming
                         the reason. Other errors (bad key, malformed request) propagate: a fallback would fail too.
    prefer="generative"  skip the probe and use the fallback.
    A `FakeDecisionsClient` is used as is (backend_kind "fake"). `plane.backend_kind` is "decisions" |
    "generative_fallback" | "fake"; fallback results carry `calibrated=False`.
    """
    if prefer not in ("decisions", "generative"):
        raise ValueError(f"prefer must be 'decisions' or 'generative', got {prefer!r}")
    if client is None:
        from google import genai

        client = genai.Client()
    plane = DecisionControlPlane(client, model)
    if plane.backend_kind == "fake":
        return plane

    def fallback(reason: str) -> DecisionControlPlane:
        backend = GenerativeDecisionsBackend(client, model=generative_model)
        log.warning("Decisions API unavailable (%s); using generative fallback %s. Probabilities are self-reported and "
                    "UNCALIBRATED: results carry calibrated=False and cannot auto-approve.", reason, backend.model)
        return DecisionControlPlane(backend, backend.model, backend_kind="generative_fallback")

    if prefer == "generative":
        return fallback("generative backend requested")
    probe = {"probe": Q.noul("Is the input text non-empty?")}
    for attempt in (1, 2):
        try:
            await asyncio.wait_for(client.aio.decisions.create(model=model, input="probe", questions=probe), probe_timeout_s)
            return plane
        except Exception as exc:  # noqa: BLE001 - classified below
            code = _status(exc)
            if code in UNAVAILABLE_CODES or type(exc).__name__ in ("NotFoundError", "PermissionDeniedError"):
                return fallback(f"{type(exc).__name__} {code}: Decisions API not available for this key/project")
            if not _is_transient(exc):
                raise
            if attempt == 2:
                return fallback(f"transient failure persisted after retry: {type(exc).__name__} {code or ''}".strip())
    raise AssertionError("unreachable")  # pragma: no cover
