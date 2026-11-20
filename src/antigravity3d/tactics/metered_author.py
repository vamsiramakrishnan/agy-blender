"""One-shot, budget-admitted Gemini authoring for governed asset runs."""

from __future__ import annotations

import asyncio
import json
import math
import os
from dataclasses import asdict
from typing import Any

from google.genai import types

from antigravity3d.decisions.fallback import PRICE_TABLE_USD_PER_M
from antigravity3d.harness.agents import _IR_RULES
from antigravity3d.ir import IRPatch, SceneIR
from antigravity3d.models import tier
from antigravity3d.state import EngineBudget
from antigravity3d.tactics.governor import BudgetExceeded, Governor, usage_cost_usd


def _rates(model: str) -> tuple[float, float]:
    rates = json.loads(os.environ.get("AGY3D_TEXT_RATES_JSON", "{}"))
    pair = rates.get(model, PRICE_TABLE_USD_PER_M.get(model))
    if (not isinstance(pair, (list, tuple)) or len(pair) != 2
            or any(not isinstance(x, (float, int)) or not math.isfinite(x) or x < 0 for x in pair)):
        raise BudgetExceeded(f"configure AGY3D_TEXT_RATES_JSON with [USD/M input, USD/M output] for {model}")
    return float(pair[0]), float(pair[1])


class MeteredAuthor:
    """Bound to one route's budget, spend ledger and image references, including during repair."""

    def __init__(self, client: Any, governor: Governor, budget: EngineBudget, context: dict, *, timeout_s: float) -> None:
        self.client, self.governor, self.budget = client, governor, budget
        self.context, self.timeout_s = dict(context), timeout_s

    async def _generate(self, role: str, operation: str, prompt: str, output_cap: int) -> str:
        mt = tier(role)
        pin, pout = _rates(mt.model)
        parts = [types.Part.from_text(text=f"{_IR_RULES}\nBudget: {json.dumps(asdict(self.budget))}\n{prompt}")]
        for key, label in (("ortho_uri", "Orthographic blueprint"), ("style_uri", "Style reference")):
            if uri := self.context.get(key):
                parts.extend([types.Part.from_text(text=label), types.Part.from_uri(file_uri=uri, mime_type="image/png")])
        contents = [types.Content(role="user", parts=parts)]
        http = types.HttpOptions(timeout=int(self.timeout_s * 1000), retry_options=types.HttpRetryOptions(attempts=1))
        api = self.client.aio.models
        counted = await api.count_tokens(model=mt.model, contents=contents, config=types.CountTokensConfig(http_options=http))
        n = counted.total_tokens
        if not isinstance(n, int) or n <= 0:
            raise BudgetExceeded(f"no input token count for {mt.model}")
        config = types.GenerateContentConfig(
            response_mime_type="application/json", max_output_tokens=output_cap, candidate_count=1,
            thinking_config=types.ThinkingConfig(thinking_level=mt.thinking.value.upper()) if mt.thinking else None,
            http_options=http, automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        def cost(inputs: int, outputs: int) -> float:
            return usage_cost_usd(inputs, outputs, usd_per_m_in=pin, usd_per_m_out=pout)
        receipt = {"model": mt.model, "role": role, "counted_input_tokens": n, "max_output_tokens": output_cap,
                   "input_usd_per_m": pin, "output_usd_per_m": pout}
        hold = self.governor.reserve(f"author.{operation}", cost(n, output_cap), receipt=receipt)
        booked = None
        try:
            result = await api.generate_content(model=mt.model, contents=contents, config=config)
            usage = result.usage_metadata
            if usage is not None and usage.prompt_token_count is not None and usage.total_token_count is not None:
                actual_in = usage.prompt_token_count
                actual_out = max(usage.total_token_count - actual_in,
                                 (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0))
                booked = cost(actual_in, actual_out)
                receipt.update(input_tokens=actual_in, output_tokens=actual_out)
            return result.text or ""
        finally:
            receipt["usage_known"] = booked is not None
            hold.settle(booked, receipt=receipt)

    async def propose(self, prompt: str, *, budget: EngineBudget, n: int, context: dict[str, Any]) -> list[SceneIR]:
        if budget != self.budget:
            raise ValueError("author bound to another asset budget")
        asset = context["asset_name"]
        async def candidate(i: int) -> SceneIR:
            task = (f"Build design {i + 1}/{n} for {prompt}. Use the attached references. Return one JSON SceneIR "
                    f"with asset_name={asset!r}, nodes and materials. Ground all parts. Include material assignments.")
            scene = SceneIR.model_validate_json(await self._generate(self.governor.plan.builder_role, "propose", task, 4096))
            if scene.asset_name != asset or not scene.nodes:
                raise ValueError("candidate must contain nodes for the requested asset")
            return scene
        results = await asyncio.gather(*(candidate(i) for i in range(n)), return_exceptions=True)
        scenes = [r for r in results if isinstance(r, SceneIR)]
        if not scenes:
            if results:
                first_error = results[0]
                if isinstance(first_error, BaseException):
                    raise first_error
            raise BudgetExceeded("no geometry candidate fits the current plan")
        return scenes

    async def repair(self, scene: SceneIR, *, action: Any, located: dict[str, list[str]], manifest: str) -> IRPatch | None:
        role = self.governor.plan.builder_role
        if action.kind == "escalate_director":
            if not self.governor.plan.allow_director:
                raise BudgetExceeded("director escalation is outside this asset's plan")
            role = "director"
        prompt = (f"Return one JSON patch {{\"ops\":[...]}} correcting only the flagged nodes. "
                  f"Reasons: {action.reasons}; locations: {json.dumps(located)}.\n"
                  f"Scene: {scene.model_dump_json(exclude_defaults=True)}\nManifest: {manifest}")
        patch = IRPatch.model_validate_json(await self._generate(role, "repair", prompt, 2048))
        return patch if patch.ops else None
