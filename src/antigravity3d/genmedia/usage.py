"""Nano Banana standard-tier image metering (per-million-token USD, excluding tax)."""

from __future__ import annotations

import math
import os
from dataclasses import asdict, dataclass
from typing import Any

from antigravity3d.models import IMAGE


@dataclass(frozen=True)
class ImageRates:
    input: float
    text: float
    image: float

    @classmethod
    def for_model(cls, model: str) -> ImageRates:
        # https://ai.google.dev/gemini-api/docs/pricing#gemini-nano-banana-2.1
        configured = [os.getenv(f"AGY3D_IMAGE_{k}_USD_PER_M") for k in ("INPUT", "TEXT_OUTPUT", "IMAGE_OUTPUT")]
        if not any(configured) and model == IMAGE:
            return cls(1.50, 7.50, 30.00)
        if not all(configured):
            raise ValueError(f"configure all three image token rates for {model}")
        rates = [float(v) for v in configured]
        if not all(math.isfinite(v) and v >= 0 for v in rates):
            raise ValueError("image token rates must be finite and non-negative")
        return cls(*rates)

    def ceiling(self, inputs: int, outputs: int) -> float:
        return (inputs * self.input + outputs * max(self.text, self.image)) / 1_000_000


@dataclass(frozen=True)
class ImageReceipt:
    model: str
    operation: str
    source: str  # provider_usage, reservation_estimate, offline, cache
    booked_usd: float
    input_tokens: int | None = None
    output_tokens: int | None = None
    image_tokens: int | None = None
    thinking_tokens: int | None = None
    response_id: str | None = None
    artifact_sha256: str | None = None
    status: str = "ok"
    width: int | None = None
    height: int | None = None
    finish_reason: str | None = None
    # candidates_token_count minus the itemized candidates_tokens_details (billed at the higher output rate)
    unitemized_output_tokens: int | None = None
    # usage_metadata fields the provider did not return, when they forced the reservation estimate
    missing_usage_fields: tuple[str, ...] = ()
    # Token ledgers only (Governor(token_ceiling=...)): the tokens booked, input + output + thinking, no price applied.
    # ``booked_usd`` is then 0.0 and is never written (the token ledger's receipts carry no *_usd key).
    booked_tokens: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """The receipt as written to the ledger; a money receipt has exactly the keys it always had."""
        d = asdict(self)
        if self.booked_tokens is None:
            del d["booked_tokens"]
        return d

    @property
    def generated_image(self) -> int:
        """1 unless the provider reported its output and it held no image tokens (a known 'no image')."""
        return 0 if self.source == "provider_usage" and self.image_tokens == 0 else 1

    @classmethod
    def from_response(cls, response: Any, *, model: str, op: str, rates: ImageRates, limit: float) -> ImageReceipt:
        """Book provider-reported tokens wherever they exist.

        Observed on gemini-nano-banana-2.1 (2026-11-20): ``candidates_tokens_details`` itemizes only the IMAGE tokens
        (1K: 1,120 of 1,431 candidate tokens; 2K: 1,680 of 1,957). The remainder is reported in the total but not
        itemized; it is booked at the higher of the text and image output rates (an upper bound, recorded as
        ``unitemized_output_tokens``). Only when a count is genuinely absent is the reservation kept, and the missing
        field is named in ``missing_usage_fields``.
        """
        usage = getattr(response, "usage_metadata", None)
        inputs = getattr(usage, "prompt_token_count", None)
        outputs = getattr(usage, "candidates_token_count", None)
        thoughts = getattr(usage, "thoughts_token_count", None) or 0
        modalities = getattr(usage, "candidates_tokens_details", None) or []
        cands = getattr(response, "candidates", None) or []
        fr = getattr(cands[0], "finish_reason", None) if cands else None
        finish = str(getattr(fr, "name", fr)) if fr is not None else None
        by_modality: dict[str, int] = {}
        for item in modalities:
            name = str(getattr(item.modality, "name", item.modality))
            by_modality[name] = by_modality.get(name, 0) + int(item.token_count or 0)
        missing = tuple(name for name, value in (("usage_metadata", usage), ("prompt_token_count", inputs),
                                                 ("candidates_token_count", outputs),
                                                 ("candidates_tokens_details", modalities or None)) if value is None)
        if usage is None:
            missing = ("usage_metadata",)
        image = by_modality.get("IMAGE", 0)
        if not missing:
            text = by_modality.get("TEXT", 0)
            unitemized = max(0, outputs - sum(by_modality.values()))
            booked = (inputs * rates.input + (text + thoughts) * rates.text + image * rates.image
                      + unitemized * max(rates.text, rates.image)) / 1_000_000
            return cls(model, op, "provider_usage", booked, inputs, outputs, image, thoughts,
                       getattr(response, "response_id", None), finish_reason=finish, unitemized_output_tokens=unitemized)
        return cls(model, op, "reservation_estimate", limit, inputs, outputs, image if modalities else None, thoughts,
                   getattr(response, "response_id", None), finish_reason=finish, missing_usage_fields=missing)

    @classmethod
    def from_response_tokens(cls, response: Any, *, model: str, op: str, limit_tokens: int) -> ImageReceipt:
        """Token-ledger receipt: input + candidates + thinking tokens as the provider reported them, no price applied.
        Any count missing keeps the whole reservation (``source`` reservation_estimate), as the money receipt does."""
        usage = getattr(response, "usage_metadata", None)
        inputs = getattr(usage, "prompt_token_count", None)
        outputs = getattr(usage, "candidates_token_count", None)
        thoughts = getattr(usage, "thoughts_token_count", None) or 0
        image = sum(int(i.token_count or 0) for i in (getattr(usage, "candidates_tokens_details", None) or [])
                    if str(getattr(i.modality, "name", i.modality)) == "IMAGE")
        cands = getattr(response, "candidates", None) or []
        fr = getattr(cands[0], "finish_reason", None) if cands else None
        finish = str(getattr(fr, "name", fr)) if fr is not None else None
        missing = tuple(n for n, v in (("usage_metadata", usage), ("prompt_token_count", inputs),
                                       ("candidates_token_count", outputs)) if v is None)
        if usage is None:
            missing = ("usage_metadata",)
        if missing:
            return cls(model, op, "reservation_estimate", 0.0, inputs, outputs, None, thoughts,
                       getattr(response, "response_id", None), finish_reason=finish, missing_usage_fields=missing,
                       booked_tokens=int(limit_tokens))
        return cls(model, op, "provider_usage", 0.0, inputs, outputs, image, thoughts, getattr(response, "response_id", None),
                   finish_reason=finish, booked_tokens=int(inputs) + int(outputs) + int(thoughts))
