from __future__ import annotations

import asyncio
import io
from dataclasses import replace
from types import SimpleNamespace

import pytest
from google.genai import types
from PIL import Image

from antigravity3d.genmedia.nano_banana import FakeImageBackend, ImageForge
from antigravity3d.genmedia.usage import ImageRates
from antigravity3d.kernel.cache import BuildCache, LocalBlobStore
from antigravity3d.models import IMAGE, tier
from antigravity3d.state import EngineBudget
from antigravity3d.tactics.cached import CachedForge
from antigravity3d.tactics.governor import BudgetExceeded, Governor, plan_for


class Provider:
    """Exercise SDK request/response models through a non-billing transport."""

    def __init__(self):
        self.client = SimpleNamespace(aio=SimpleNamespace(models=self))
        self.calls = []
        self.started = asyncio.Event()
        self.proceed = asyncio.Event()
        self.proceed.set()
        self.failure = None
        img = io.BytesIO()
        Image.new("RGB", (2048, 2048), (20, 80, 120)).save(img, "PNG")
        self.response = types.GenerateContentResponse(
            response_id="provider-1", candidates=[types.Candidate(content=types.Content(
                parts=[types.Part.from_bytes(data=img.getvalue(), mime_type="image/png")]))],
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=100, candidates_token_count=1700, thoughts_token_count=70,
                candidates_tokens_details=[types.ModalityTokenCount(modality="IMAGE", token_count=1680),
                                          types.ModalityTokenCount(modality="TEXT", token_count=20)]))

    async def generate_content(self, *, model, contents, config):
        self.calls.append((model, config))
        self.started.set()
        await self.proceed.wait()
        if self.failure:
            raise self.failure
        return self.response


async def test_100_concurrent_requests_across_governors_and_restart(tmp_path):
    provider = Provider()
    provider.proceed.clear()
    plan = replace(plan_for(2), ceiling_usd=0.15)
    journal = tmp_path / "asset" / "spend.sqlite3"
    governors = [Governor(plan, journal=journal) for _ in range(4)]
    forges = [ImageForge(client=provider.client, governor=g) for g in governors]
    tasks = [asyncio.create_task(forges[i % 4].seamless_albedo(f"surface {i}")) for i in range(100)]
    await provider.started.wait()
    # Every non-admitted task finishes while the one admitted request is held at the transport.
    await asyncio.wait(tasks, timeout=0.1)
    assert len(provider.calls) == 1
    assert governors[1].receipts()[0]["booked_usd"] is None
    provider.proceed.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert sum(isinstance(r, BudgetExceeded) for r in results) == 99
    gen, = [r for r in results if not isinstance(r, BaseException)]
    receipt = gen.receipt
    assert gen.model == tier("image").model == IMAGE == provider.calls[0][0]
    assert provider.calls[0][1].image_config.image_size == "2K"
    assert provider.calls[0][1].http_options.retry_options.attempts == 1
    expected = (100 * 1.5 + 1680 * 30 + (70 + 20) * 7.5) / 1e6
    assert receipt.source == "provider_usage" and receipt.booked_usd == pytest.approx(expected)
    assert receipt.artifact_sha256 and receipt.response_id == "provider-1"
    recovered = Governor(plan, journal=journal)
    assert recovered.spent == pytest.approx(expected)
    assert recovered.receipts()[0]["overrun_usd"] == 0


async def test_100_replays_across_cache_instances_dispatch_once(tmp_path):
    provider = Provider()
    ledger = Governor(plan_for(1), journal=tmp_path / "asset.sqlite3")

    def forge(budget):
        return CachedForge(ImageForge(client=provider.client, governor=ledger),
                           BuildCache(LocalBlobStore(tmp_path / "cache")), budget=budget)

    budget = EngineBudget("godot_4")
    a, b = forge(budget), forge(budget)
    results = await asyncio.gather(*(f.seamless_albedo("same") for f in ([a, b] * 50)))
    assert len(provider.calls) == 1
    assert all(r.png_bytes == results[0].png_bytes for r in results)
    assert sum(r.receipt.source == "cache" for r in results) == 99
    assert len(ledger.receipts()) == 100
    assert a.cache.stats.saved_cost_usd + b.cache.stats.saved_cost_usd == pytest.approx(99 * ledger.spent)
    assert ledger.spent == pytest.approx(sum(r.receipt.booked_usd for r in results))
    # Reopened cache still replays at zero cost even after the spend limit is exhausted.
    old_plan = replace(plan_for(1), ceiling_usd=ledger.spent)
    restarted = forge(budget).with_governor(Governor(old_plan, journal=ledger.journal))
    assert (await restarted.seamless_albedo("same")).receipt.booked_usd == 0
    assert len(provider.calls) == 1
    assert (await forge(replace(budget, max_tris=4)).seamless_albedo("same")).receipt.source == "provider_usage"
    offline = CachedForge(ImageForge(FakeImageBackend()), b.cache, budget=budget)
    assert (await offline.seamless_albedo("same", size=32)).receipt.source == "offline"


async def test_ambiguous_failure_retries_reserve_again_and_billed_refusal_retains_usage(tmp_path):
    provider = Provider()
    ledger = Governor(replace(plan_for(1), ceiling_usd=0.15), journal=tmp_path / "spend.sqlite3")
    forge = ImageForge(client=provider.client, governor=ledger)
    provider.failure = TimeoutError("after dispatch")
    with pytest.raises(TimeoutError):
        await forge.seamless_albedo("retry")
    receipt, = ledger.receipts()
    assert receipt["booked_usd"] == receipt["reserved_usd"]
    assert receipt["source"] == "reservation_estimate" and receipt["status"] == "TimeoutError"
    with pytest.raises(BudgetExceeded):
        await forge.seamless_albedo("retry")
    assert len(provider.calls) == 1
    provider.failure = None
    provider.response.candidates = []
    roomy = Governor(plan_for(3))
    with pytest.raises(RuntimeError, match="no inline image"):
        await forge.with_governor(roomy).seamless_albedo("safety refusal")
    refusal, = roomy.receipts()
    assert refusal["source"] == "provider_usage" and refusal["status"] == "RuntimeError"
    assert refusal["booked_usd"] > 0 and refusal["artifact_sha256"] is None


async def test_partial_usage_is_estimated_and_overrun_reported():
    provider = Provider()
    provider.response.usage_metadata.candidates_tokens_details = None
    forge = ImageForge(client=provider.client)
    image = await forge.seamless_albedo("unitemized")
    assert image.receipt.source == "reservation_estimate"
    assert image.receipt.image_tokens is None
    provider.response.usage_metadata.candidates_token_count = 9999
    provider.response.usage_metadata.candidates_tokens_details = [types.ModalityTokenCount(modality="IMAGE", token_count=9999)]
    await forge.seamless_albedo("overrun")
    assert forge.governor.receipts()[-1]["overrun_usd"] > 0


def test_unpriced_image_model_requires_configured_rates(monkeypatch):
    with pytest.raises(ValueError, match="token rates"):
        ImageRates.for_model("private-deployment")
    monkeypatch.setenv("AGY3D_IMAGE_IMAGE_OUTPUT_USD_PER_M", "9")
    monkeypatch.setenv("AGY3D_IMAGE_TEXT_OUTPUT_USD_PER_M", "6")
    monkeypatch.setenv("AGY3D_IMAGE_INPUT_USD_PER_M", "2")
    assert ImageRates.for_model("private-deployment").ceiling(1000, 1000) == pytest.approx(0.011)
