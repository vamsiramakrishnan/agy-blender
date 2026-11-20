#!/usr/bin/env python
"""Bounded LIVE latency probe for antigravity-3d (measures the live COMPONENTS, not the governed pipeline).

    python benchmarks/perf_live.py --out benchmarks/results/live.json [--dry-run] [--only GROUP ...]
                                   [--max-calls N] [--max-tokens N] [--self-test]

Groups: text, stage, judge, image (see PLAN below; nothing else is ever called).

Spend control, enforced in code
  * One global ``Limiter`` (calls + input/output/thinking tokens) shared by every group. It is checked BEFORE every
    call; when a limit would be exceeded the run stops cleanly, writes partial results and records ``"aborted"``.
  * Every ``generate_content`` / ``decisions.create`` made through the genai client goes through ``CountedClient``,
    including calls issued inside the package's own backends (the generative fallback, ImageForge), so the limit also
    binds those.  Failed calls count, and a dispatched call whose usage is unknown (timeout, error, or no usage
    metadata) keeps its pre-call token reservation booked against ``--max-tokens``.  No retries here; SDK retry is switched off (``attempts=1``) and the package
    backends' own retry loops are disabled (``max_parse_retries=0``, ``max_retries=0``).
  * The ``stage`` group drives the Antigravity SDK Agent, which talks to the model on its own connection. Its internal
    model requests cannot be intercepted, so a stage run counts as ONE call with an estimated token reservation checked
    up front and its reported usage booked afterwards; the 600 s timeout bounds it.
  * No price / rate environment variable is read or set here. Dollar cost is never reported; tokens only.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
import types as pytypes
from dataclasses import asdict
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent

ASSET_PROMPT = "wooden crate with iron corner brackets"
STAGE_PROMPT = "Build a simple wooden crate with a separate lid from two boxes, then run visual QA."
ALBEDO_PROMPT = "weathered oak planks"
GROUPS = ("text", "stage", "judge", "image")

TIMEOUTS = {"text": 120.0, "director": 300.0, "stage": 600.0, "judge": 120.0, "image": 180.0}
# (role, n) per group. Judge: (stage, n).
TEXT_PLAN = (("worker", 3), ("builder", 3), ("bulk", 3), ("director", 1))
STAGE_PLAN = (("worker", 2), ("builder", 1))
JUDGE_PLAN = (("blockout", 3), ("lookdev", 2))
# Token reservations checked before each call (a floor-check, not a budget): real usage is booked after the call.
EST_TOKENS = {"text": 8_000, "director": 12_000, "stage": 40_000, "judge": 6_000, "image": 2_500, "probe": 200}


# ----------------------------------------------------------------------------------------------- spend limiter


class LimitReached(Exception):
    """A spend limit would be exceeded by the next call; the run stops cleanly."""


class Limiter:
    def __init__(self, max_calls: int, max_tokens: int) -> None:
        self.max_calls, self.max_tokens = max_calls, max_tokens
        self.calls = self.input_tokens = self.output_tokens = self.thinking_tokens = 0
        self.unknown_usage_reserved = 0  # reservations kept for dispatched calls whose usage was never reported

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.thinking_tokens + self.unknown_usage_reserved

    def before(self, label: str, est_tokens: int = 0) -> None:
        if self.calls + 1 > self.max_calls:
            raise LimitReached(f"max_calls={self.max_calls} would be exceeded by the next call ({label})")
        if self.tokens + est_tokens > self.max_tokens:
            raise LimitReached(f"max_tokens={self.max_tokens} would be exceeded (used {self.tokens}, "
                               f"next call reserves ~{est_tokens}) ({label})")

    def record(self, inp: int = 0, out: int = 0, think: int = 0) -> None:
        self.calls += 1
        self.input_tokens += inp
        self.output_tokens += out
        self.thinking_tokens += think

    def record_unknown(self, reserved: int) -> None:
        """A dispatched call with no usage metadata (timeout, error, or a response without usage) may still have
        been billed, so its pre-call reservation stays booked against ``--max-tokens``."""
        self.calls += 1
        self.unknown_usage_reserved += max(0, int(reserved))

    def totals(self) -> dict[str, int]:
        return {"calls": self.calls, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "thinking_tokens": self.thinking_tokens, "unknown_usage_reserved_tokens": self.unknown_usage_reserved}


def one_line(e: BaseException, n: int = 300) -> str:
    s = f"{type(e).__name__}: {e}".replace("\n", " ").replace("\r", " ")
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        s = s.replace(key, "<redacted>")
    return s[:n]


def _usage_tuple(u: Any) -> tuple[int, int, int] | None:
    """(input, output, thinking) from genai usage metadata; None when absent."""
    if u is None:
        return None
    g = lambda name: int(getattr(u, name, None) or 0)  # noqa: E731
    return g("prompt_token_count"), g("candidates_token_count"), g("thoughts_token_count")


class _Models:
    def __init__(self, owner: CountedClient, inner: Any) -> None:
        self._o, self._i = owner, inner

    async def generate_content(self, **kw: Any) -> Any:
        return await self._o._call("generate_content", self._i.generate_content, kw, kw.get("model"))

    async def count_tokens(self, **kw: Any) -> Any:
        return await self._o._call("count_tokens", self._i.count_tokens, kw, kw.get("model"))

    def __getattr__(self, name: str) -> Any:  # anything else is a pass-through (never a paid call used by this script)
        return getattr(self._i, name)


class _Decisions:
    def __init__(self, owner: CountedClient, inner: Any) -> None:
        self._o, self._i = owner, inner

    async def create(self, **kw: Any) -> Any:
        return await self._o._call("decisions.create", self._i.create, kw, kw.get("model"), est=EST_TOKENS["probe"])


class _Aio:
    def __init__(self, owner: CountedClient, inner: Any) -> None:
        self._i = inner
        self.models = _Models(owner, inner.models)
        self.decisions = _Decisions(owner, inner.decisions)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._i, name)


class CountedClient:
    """Wraps a genai client: every paid call is limiter-checked first, timed, token-counted and logged."""

    def __init__(self, inner: Any, limiter: Limiter) -> None:
        self.inner, self.limiter = inner, limiter
        self.events: list[dict[str, Any]] = []
        self.timeout_s = 120.0
        self.est_tokens = 0
        self.aio = _Aio(self, inner.aio)

    async def _call(self, kind: str, fn: Any, kw: dict[str, Any], model: str | None, est: int | None = None) -> Any:
        reserve = self.est_tokens if est is None else est
        self.limiter.before(f"{kind} {model}", reserve)
        ev: dict[str, Any] = {"kind": kind, "model": model, "error": None, "status": None}
        t0 = time.perf_counter()
        try:
            res = await asyncio.wait_for(fn(**kw), self.timeout_s + 5)
        except Exception as e:  # noqa: BLE001 - recorded, counted, re-raised
            ev.update(wall_ms=round((time.perf_counter() - t0) * 1000, 1), error=one_line(e),
                      error_class=type(e).__name__, status=getattr(e, "code", None), input_tokens=0, output_tokens=0,
                      thinking_tokens=0, tokens_known=False, reserved_tokens=reserve)
            self.limiter.record_unknown(reserve)
            self.events.append(ev)
            raise
        ev["wall_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        usage = _usage_tuple(getattr(res, "usage_metadata", None))
        usage_known = usage is not None
        ev["tokens_known"] = usage_known
        if usage is None:
            usage = (0, 0, 0)
            ev["reserved_tokens"] = reserve
        ev["input_tokens"], ev["output_tokens"], ev["thinking_tokens"] = usage
        ev["total_tokens_reported"] = getattr(getattr(res, "usage_metadata", None), "total_token_count", None)
        raw = getattr(res, "usage_metadata", None)
        if raw is not None and hasattr(raw, "model_dump"):
            ev["usage_raw"] = raw.model_dump(mode="json", exclude_none=True)
        cands = getattr(res, "candidates", None) or []
        fr = getattr(cands[0], "finish_reason", None) if cands else None
        ev["finish_reason"] = getattr(fr, "name", fr)
        if usage_known:
            self.limiter.record(*usage)
        else:
            self.limiter.record_unknown(reserve)
        self.events.append(ev)
        return res


# ----------------------------------------------------------------------------------------------- helpers


def stats_block(samples: list[dict[str, Any]], key: str = "wall_ms") -> dict[str, Any]:
    ok = [s[key] for s in samples if not s.get("error") and s.get(key) is not None]
    out: dict[str, Any] = {"samples": samples, "n_ok": len(ok), "n_failed": len(samples) - len(ok)}
    if ok:  # n <= 3: median / min / max only, no percentiles
        out.update(median_ms=round(statistics.median(ok), 1), min_ms=round(min(ok), 1), max_ms=round(max(ok), 1))
    return out


def ev_tokens(events: list[dict[str, Any]]) -> dict[str, int]:
    return {"input_tokens": sum(e["input_tokens"] for e in events), "output_tokens": sum(e["output_tokens"] for e in events),
            "thinking_tokens": sum(e["thinking_tokens"] for e in events)}


def compile_tier1(scene: Any, budget: Any) -> dict[str, Any]:
    """Compile a SceneIR with the kernel and run Tier-1 QA. Never raises."""
    from antigravity3d.kernel.fast_ops import tier1_qa
    from antigravity3d.kernel.usd_stage import StageManager

    try:
        with tempfile.TemporaryDirectory(prefix="perf_live_") as d:
            sm = StageManager.create(Path(d) / "scene.usda", scene.asset_name)
            sm.apply_ir(scene)
            rep = tier1_qa(sm.extract_meshes(), budget)
        return {"compiled": True, "tier1_ok": rep.ok, "tier1": rep.one_line(160), "tris": rep.stats.get("tris")}
    except Exception as e:  # noqa: BLE001
        return {"compiled": False, "tier1_ok": False, "compile_error": one_line(e, 200)}


def _sample_from_event(ev: dict[str, Any] | None) -> dict[str, Any]:
    if ev is None:
        return {"error": "no call was made"}
    keys = ("wall_ms", "input_tokens", "output_tokens", "thinking_tokens", "total_tokens_reported", "tokens_known", "finish_reason", "error",
            "error_class", "status")
    return {k: ev[k] for k in keys if k in ev}


# ----------------------------------------------------------------------------------------------- group: text


async def text_group(client: CountedClient, plan: tuple[tuple[str, int], ...], out: dict[str, Any], flush: Any) -> None:
    """The live author's request (MeteredAuthor._generate / .propose, minus count_tokens and the spend governor)."""
    from google.genai import types

    from antigravity3d.harness.agents import _IR_RULES
    from antigravity3d.ir import SceneIR
    from antigravity3d.models import tier
    from antigravity3d.state import EngineBudget
    from antigravity3d.tactics.authoring import _slug

    budget = EngineBudget("unreal_5")
    asset = _slug(ASSET_PROMPT)
    task = (f"Build design 1/1 for {ASSET_PROMPT}. Use the attached references. Return one JSON SceneIR "
            f"with asset_name={asset!r}, nodes and materials. Ground all parts. Include material assignments.")
    prompt_text = f"{_IR_RULES}\nBudget: {json.dumps(asdict(budget))}\n{task}"
    for role, n in plan:
        mt = tier(role)  # type: ignore[arg-type]
        timeout = TIMEOUTS["director"] if role == "director" else TIMEOUTS["text"]
        client.timeout_s, client.est_tokens = timeout, EST_TOKENS["director" if role == "director" else "text"]
        key = f"{role}:{mt.model}"
        samples: list[dict[str, Any]] = []
        out[key] = {"samples": samples}
        for i in range(n):
            http = types.HttpOptions(timeout=int(timeout * 1000), retry_options=types.HttpRetryOptions(attempts=1))
            cfg = types.GenerateContentConfig(
                response_mime_type="application/json", max_output_tokens=4096, candidate_count=1,
                thinking_config=types.ThinkingConfig(thinking_level=mt.thinking.value.upper()) if mt.thinking else None,
                http_options=http, automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
            contents = [types.Content(role="user", parts=[types.Part.from_text(text=prompt_text)])]
            n_ev = len(client.events)
            sample: dict[str, Any] = {"thinking_level": mt.thinking.value if mt.thinking else None, "timeout_s": timeout}
            try:
                resp = await client.aio.models.generate_content(model=mt.model, contents=contents, config=cfg)
                text = resp.text or ""
            except LimitReached:
                raise
            except Exception:  # noqa: BLE001 - already in the event log
                text = None
            sample.update(_sample_from_event(client.events[n_ev] if len(client.events) > n_ev else None))
            if text is not None:
                sample["response_chars"] = len(text)
                try:
                    scene = SceneIR.model_validate_json(text)
                    sample.update(scene_ir_valid=True, nodes=len(scene.nodes), asset_name_matches=scene.asset_name == asset)
                    sample.update(compile_tier1(scene, budget))
                except Exception as e:  # noqa: BLE001
                    sample.update(scene_ir_valid=False, parse_error=one_line(e, 200), tier1_ok=False)
            samples.append(sample)
            out[key] = stats_block(samples)
            _progress("text", mt.model, sample, client.limiter)
            flush()


# ----------------------------------------------------------------------------------------------- group: stage


async def stage_group(limiter: Limiter, plan: tuple[tuple[str, int], ...], out: dict[str, Any], flush: Any) -> None:
    """run_stage('BLOCKOUT') with the real model, FAKE decisions and FAKE image backend (as the package's live test)."""
    from antigravity3d.decisions.engine import DecisionControlPlane
    from antigravity3d.decisions.fakes import FakeDecisionsClient
    from antigravity3d.genmedia.nano_banana import FakeImageBackend, ImageForge
    from antigravity3d.harness import session as S
    from antigravity3d.harness.agents import run_stage
    from antigravity3d.kernel.fast_ops import tier1_qa
    from antigravity3d.models import tier
    from antigravity3d.state import EngineBudget, SceneSessionRegistry

    for role, n in plan:
        mt = tier(role)  # type: ignore[arg-type]
        key = f"{role}:{mt.model}"
        samples: list[dict[str, Any]] = []
        out[key] = {"samples": samples}
        for i in range(n):
            limiter.before(f"stage {mt.model}", EST_TOKENS["stage"])
            reg, sid = SceneSessionRegistry(), f"bench-{role}-{i}"
            budget = EngineBudget("unreal_5")
            workdir = Path(tempfile.mkdtemp(prefix="perf_live_stage_"))
            S.open_session(reg, sid, workdir / sid, "Crate", budget, plane=DecisionControlPlane(FakeDecisionsClient()),
                           forge=ImageForge(FakeImageBackend()))
            sample: dict[str, Any] = {"timeout_s": TIMEOUTS["stage"], "thinking_level": mt.thinking.value if mt.thinking else None}
            t0 = time.perf_counter()
            run = None
            try:
                run = await run_stage("BLOCKOUT", sid, STAGE_PROMPT, registry=reg, role=role, timeout_s=TIMEOUTS["stage"])
            except Exception as e:  # noqa: BLE001
                sample.update(error=one_line(e), error_class=type(e).__name__)
            wall_s = time.perf_counter() - t0
            sample["wall_s"] = round(wall_s, 2)
            sample["wall_ms"] = round(wall_s * 1000, 1)
            u = run.usage if run else None
            tk = _usage_tuple(u)
            if tk is not None:
                sample.update(prompt_tokens=tk[0], output_tokens=tk[1], thinking_tokens=tk[2],
                              total_tokens=int(getattr(u, "total_token_count", None) or 0), tokens_known=True)
                limiter.record(*tk)
            else:  # unknown usage (failure/timeout): book the reservation so the limiter stays conservative
                sample["tokens_known"] = False
                sample["reserved_tokens"] = EST_TOKENS["stage"]
                limiter.record_unknown(EST_TOKENS["stage"])
            try:
                s = S.session(reg, sid)
                tel_path = s.workdir / "telemetry.jsonl"
                recs = [json.loads(x) for x in tel_path.read_text().splitlines() if x.strip()] if tel_path.exists() else []
                sample["tool_calls"] = len(recs)
                sample["tool_durations_ms"] = [{"tool": r["tool"], "ms": r["ms"], "error": r["error"], "soft_fail": r["soft_fail"]}
                                               for r in recs]
                tool_ms = sum(r["ms"] or 0 for r in recs)
                sample["tool_time_s"] = round(tool_ms / 1000, 3)
                sample["model_side_s"] = round(wall_s - tool_ms / 1000, 2)
                if run:
                    sample["telemetry_summary"] = run.telemetry
                    sample["stop_continuations"] = run.stop_continuations
                scene = s.stage.base_ir()
                sample["final_nodes"] = len(scene.nodes)
                sample["tier1_ok"] = tier1_qa(s.stage.extract_meshes(), budget).ok if scene.nodes else False
            except Exception as e:  # noqa: BLE001
                sample["post_error"] = one_line(e, 200)
            samples.append(sample)
            out[key] = stats_block(samples)
            _progress("stage", mt.model, sample, limiter)
            flush()


# ----------------------------------------------------------------------------------------------- group: judge


async def _contact_sheet_data_uri() -> tuple[str, dict[str, Any]]:
    """Real 4-view clay contact sheet of a TemplateAuthor crate, rendered by the kernel (local, no model)."""
    import base64
    import io

    from antigravity3d.kernel.raster import contact_sheet
    from antigravity3d.kernel.usd_stage import StageManager
    from antigravity3d.state import EngineBudget
    from antigravity3d.tactics.authoring import TemplateAuthor

    scene = (await TemplateAuthor(seed=0).propose(ASSET_PROMPT, budget=EngineBudget("unreal_5"), n=1,
                                                  context={"asset_name": "Crate"}))[0]
    with tempfile.TemporaryDirectory(prefix="perf_live_") as d:
        sm = StageManager.create(Path(d) / "crate.usda", scene.asset_name)
        sm.apply_ir(scene)
        img = contact_sheet(sm.extract_meshes(), ("front", "right", "top", "persp"), 256)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(), {"sheet_px": list(img.size), "sheet_bytes": buf.tell()}


async def judge_group(client: CountedClient, plan: tuple[tuple[str, int], ...], out: dict[str, Any], flush: Any,
                      sheet: tuple[str, dict[str, Any]] | None = None) -> None:
    from antigravity3d.decisions import questions as Q
    from antigravity3d.decisions.factory import make_control_plane

    client.timeout_s, client.est_tokens = TIMEOUTS["judge"], EST_TOKENS["judge"]
    # 1) native Decisions probe: ONE call, no retry.
    n_ev = len(client.events)
    probe: dict[str, Any] = {}
    try:
        await client.aio.decisions.create(model="gemini-decision-1", input="probe",
                                          questions={"probe": Q.noul("Is the input text non-empty?")})
        probe["available"] = True
    except LimitReached:
        raise
    except Exception:  # noqa: BLE001
        probe["available"] = False
    probe.update(_sample_from_event(client.events[n_ev] if len(client.events) > n_ev else None))
    probe.pop("finish_reason", None)
    out["native_probe"] = probe
    _progress("judge", "gemini-decision-1 (probe)", probe, client.limiter)
    flush()
    # 2) generative fallback through the package's control plane. governor=None: the plane's unpriced-fallback guard
    #    ("cannot reserve an unpriced generative fallback for automatic judging") only fires when a Governor is passed.
    plane = await make_control_plane(client, prefer="generative")  # prefer=generative: no probe call, no retry loop
    backend = plane._client
    backend.max_parse_retries, backend.timeout_s = 0, TIMEOUTS["judge"]
    plane.max_retries = 0
    uri, sheet_info = sheet or await _contact_sheet_data_uri()
    out["sheet"] = sheet_info
    for stage, n in plan:
        key = f"visual_qa:{stage}:{backend.model}"
        samples: list[dict[str, Any]] = []
        out[key] = {"samples": samples}
        for i in range(n):
            n_ev = len(client.events)
            sample: dict[str, Any] = {"stage": stage, "backend_kind": plane.backend_kind, "backend_model": backend.model}
            t0 = time.perf_counter()
            try:
                r = await plane.evaluate_visual_qa(uri, None, None, stage)  # type: ignore[arg-type]
                res = r.result
                sample.update(question_count=res.n_questions, calibrated=res.calibrated, source=res.source,
                              resolution=r.resolution, plane_latency_ms=round(res.latency_ms, 1),
                              plane_input_tokens=res.input_tokens, plane_output_tokens=res.output_tokens,
                              next_action=r.next_action, skipped=len(r.skipped))
            except LimitReached:
                raise
            except Exception as e:  # noqa: BLE001
                sample.update(error=one_line(e), error_class=type(e).__name__)
            evs = client.events[n_ev:]
            sample["wall_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            sample["backend_requests"] = len(evs)
            sample["thinking_level"] = backend.thinking_level
            sample.update(ev_tokens(evs))
            if evs and any(e.get("error") for e in evs) and "error" not in sample:
                sample["error"] = next(e["error"] for e in evs if e.get("error"))
            samples.append(sample)
            out[key] = stats_block(samples)
            _progress("judge", backend.model, sample, client.limiter)
            flush()


# ----------------------------------------------------------------------------------------------- group: image


async def image_group(client: CountedClient, out: dict[str, Any], flush: Any) -> None:
    """ONE seamless albedo via the real ImageForge/GenAIImageBackend (smallest size tier the backend maps: 1K)."""
    from antigravity3d.genmedia.nano_banana import ImageForge, default_image_model

    client.timeout_s, client.est_tokens = TIMEOUTS["image"], EST_TOKENS["image"]
    model = default_image_model()
    key = f"image:{model}"
    sample: dict[str, Any] = {"requested_size": 1024, "size_tier": "1K", "timeout_s": TIMEOUTS["image"]}
    n_ev = len(client.events)
    try:
        # http timeout: the backend sets retry attempts=1 itself; the client-level timeout is configured in main().
        g = await ImageForge(client=client, model=model).seamless_albedo(ALBEDO_PROMPT, size=1024)
        w, h = g.image().size
        rc = g.receipt
        sample.update(wall_ms=round(g.latency_ms, 1), pixel_size=[w, h], bytes=len(g.png_bytes), model=g.model,
                      receipt_input_tokens=rc.input_tokens, receipt_output_tokens=rc.output_tokens,
                      receipt_image_tokens=rc.image_tokens, receipt_thinking_tokens=rc.thinking_tokens,
                      receipt_source=rc.source)  # booked_usd deliberately NOT recorded: dollar cost is unknown here
    except LimitReached:
        raise
    except Exception as e:  # noqa: BLE001
        sample.update(error=one_line(e), error_class=type(e).__name__)
    if len(client.events) > n_ev:
        ev = client.events[n_ev]
        sample.update({k: ev[k] for k in ("input_tokens", "output_tokens", "thinking_tokens", "tokens_known") if k in ev})
        if "usage_raw" in ev:
            sample["usage_metadata"] = ev["usage_raw"]
        sample.setdefault("wall_ms", ev["wall_ms"])
        sample["api_wall_ms"] = ev["wall_ms"]
        if ev.get("error") and "error" not in sample:
            sample["error"] = ev["error"]
    out[key] = stats_block([sample])
    _progress("image", model, sample, client.limiter)
    flush()


# ----------------------------------------------------------------------------------------------- plan / driver


def _progress(group: str, model: str, s: dict[str, Any], lim: Limiter) -> None:
    ms = s.get("wall_ms")
    tok = (s.get("input_tokens", s.get("prompt_tokens", 0)) or 0, s.get("output_tokens", 0) or 0, s.get("thinking_tokens", 0) or 0)
    err = f" ERROR={s['error'][:80]}" if s.get("error") else ""
    print(f"[{group:5}] {model:28} {ms if ms is not None else '-':>9} ms  tokens in/out/think={tok[0]}/{tok[1]}/{tok[2]}"
          f"  | total calls={lim.calls}/{lim.max_calls} tokens={lim.tokens}/{lim.max_tokens}{err}", flush=True)


def build_plan(groups: list[str], roles: list[str] | None, n_override: int | None) -> list[dict[str, Any]]:
    from antigravity3d.models import tier

    keep = lambda role: roles is None or role in roles  # noqa: E731
    nn = lambda n: n_override if n_override is not None else n  # noqa: E731
    rows: list[dict[str, Any]] = []
    if "text" in groups:
        for role, n in TEXT_PLAN:
            if keep(role):
                rows.append(dict(group="text", role=role, model=tier(role).model, n=nn(n),  # type: ignore[arg-type]
                                 timeout_s=TIMEOUTS["director" if role == "director" else "text"], calls=nn(n)))
    if "stage" in groups:
        for role, n in STAGE_PLAN:
            if keep(role):
                rows.append(dict(group="stage", role=role, model=tier(role).model, n=nn(n),  # type: ignore[arg-type]
                                 timeout_s=TIMEOUTS["stage"], calls=nn(n), note="counts as 1 call each; SDK-internal requests not interceptable"))
    if "judge" in groups:
        rows.append(dict(group="judge", role="decision", model="gemini-decision-1 (native probe)", n=1, timeout_s=TIMEOUTS["judge"], calls=1))
        for stg, n in JUDGE_PLAN:
            rows.append(dict(group="judge", role=f"visual_qa:{stg}", model=f"{tier('bulk').model} (generative fallback)", n=nn(n),
                             timeout_s=TIMEOUTS["judge"], calls=nn(n), note="1 request per evaluate unless packets split"))
    if "image" in groups:
        rows.append(dict(group="image", role="image", model=tier("image").model, n=1, timeout_s=TIMEOUTS["image"], calls=1))
    return rows


def print_plan(rows: list[dict[str, Any]], max_calls: int, max_tokens: int) -> int:
    print(f"{'group':6} {'role':18} {'model':46} {'n':>3} {'timeout_s':>9} {'calls':>5}")
    for r in rows:
        print(f"{r['group']:6} {r['role']:18} {r['model']:46} {r['n']:>3} {r['timeout_s']:>9.0f} {r['calls']:>5}  {r.get('note', '')}")
    total = sum(r["calls"] for r in rows)
    print(f"planned calls: {total} (limit {max_calls}); token limit {max_tokens}; retries: none; prices: none (cost unknown)")
    return total


def machine_info() -> dict[str, Any]:
    from importlib.metadata import PackageNotFoundError, version

    def v(p: str) -> str | None:
        try:
            return version(p)
        except PackageNotFoundError:
            return None

    def git(*a: str) -> str | None:
        try:
            return subprocess.run(["git", *a], cwd=REPO, capture_output=True, text=True, timeout=10).stdout.strip() or None
        except Exception:  # noqa: BLE001
            return None

    return {"python": platform.python_version(), "platform": platform.platform(),
            "package_git_sha": git("rev-parse", "HEAD"), "package_git_dirty": bool(git("status", "--porcelain", "--", "src")),
            "sdk_versions": {"google-genai": v("google-genai"), "google-antigravity": v("google-antigravity"),
                             "antigravity-3d": v("antigravity-3d")},
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}


async def run_groups(args: argparse.Namespace, client_factory: Any, limiter: Limiter, result: dict[str, Any], flush: Any) -> None:
    groups = result["groups"]
    roles, n_over = args.roles, args.n_override
    keep = lambda role: roles is None or role in roles  # noqa: E731
    nn = lambda n: n_over if n_over is not None else n  # noqa: E731
    client: CountedClient | None = None

    def get_client() -> CountedClient:
        nonlocal client
        if client is None:
            client = CountedClient(client_factory(), limiter)
        return client

    try:
        for g in GROUPS:
            if g not in args.only:
                continue
            groups[g] = {}
            if g == "text":
                await text_group(get_client(), tuple((r, nn(n)) for r, n in TEXT_PLAN if keep(r)), groups[g], flush)
            elif g == "stage":
                await stage_group(limiter, tuple((r, nn(n)) for r, n in STAGE_PLAN if keep(r)), groups[g], flush)
            elif g == "judge":
                await judge_group(get_client(), tuple((s, nn(n)) for s, n in JUDGE_PLAN), groups[g], flush)
            elif g == "image":
                await image_group(get_client(), groups[g], flush)
    except LimitReached as e:
        result["aborted"] = str(e)
        print(f"ABORTED: {e}", flush=True)
    result["totals"] = limiter.totals()


def make_result(args: argparse.Namespace, limiter: Limiter) -> dict[str, Any]:
    return {"machine": machine_info(),
            "limits": {"max_calls": args.max_calls, "max_tokens": args.max_tokens, "timeouts_s": TIMEOUTS,
                       "retries": "none (genai HttpRetryOptions attempts=1; package fallback/plane retry loops set to 0; "
                                  "the Antigravity SDK Agent used by `stage` exposes no switch, so its internal retries are unknown)",
                       "prices": "none configured; dollar cost is unknown, token counts only",
                       "stage_accounting": "1 call per run_stage with a token reservation; SDK-internal model requests are not counted individually"},
            "totals": limiter.totals(), "groups": {}, "aborted": None}


def write_json(path: Path, result: dict[str, Any], limiter: Limiter) -> None:
    result["totals"] = limiter.totals()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(result, indent=2, default=str))
    tmp.replace(path)


# ----------------------------------------------------------------------------------------------- self test (no network)


def self_test() -> int:
    """Drive the limiter and the text group against a stub client; no genai client is created, no network."""
    from antigravity3d.state import EngineBudget
    from antigravity3d.tactics.authoring import TemplateAuthor

    # 1) bare limiter: tokens
    lim = Limiter(max_calls=100, max_tokens=1000)
    for _ in range(3):
        lim.before("x")
        lim.record(100, 200, 50)  # 350 each
    try:
        lim.before("x")
        print("FAIL: limiter allowed a call past max_tokens")
        return 1
    except LimitReached as e:
        print(f"ok  token limit stops the run: {e}")
    lim = Limiter(max_calls=100, max_tokens=1000)
    lim.record(500, 400, 0)
    try:
        lim.before("x", est_tokens=200)
        print("FAIL: reservation not enforced")
        return 1
    except LimitReached as e:
        print(f"ok  reservation counted before the call: {e}")

    # 2) text group against a stub: calls limit stops the run mid-plan with partial results + failed calls counted
    scene = asyncio.run(TemplateAuthor().propose(ASSET_PROMPT, budget=EngineBudget("unreal_5"), n=1,
                                                  context={"asset_name": "WoodenCrateIron"}))[0]
    payload = scene.model_dump_json()
    n_calls = {"n": 0}

    class Usage:
        prompt_token_count, candidates_token_count, thoughts_token_count = 100, 200, 50

    class Resp:
        text, usage_metadata, candidates = payload, Usage(), []

    class StubModels:
        async def generate_content(self, **kw: Any) -> Any:
            n_calls["n"] += 1
            if n_calls["n"] == 2:
                raise RuntimeError("stub failure\nsecond line")
            await asyncio.sleep(0.01)
            return Resp()

    stub = pytypes.SimpleNamespace(aio=pytypes.SimpleNamespace(models=StubModels(), decisions=None))
    args = pytypes.SimpleNamespace(max_calls=4, max_tokens=400_000, only=["text"], roles=None, n_override=None)
    lim = Limiter(args.max_calls, args.max_tokens)
    result = make_result(args, lim)
    asyncio.run(run_groups(args, lambda: stub, lim, result, lambda: None))
    t = result["groups"]["text"]
    got = sum(len(v["samples"]) for v in t.values())
    assert result["aborted"] and "max_calls=4" in result["aborted"], result["aborted"]
    assert got == 4 and n_calls["n"] == 4 and lim.calls == 4, (got, n_calls, lim.calls)
    fails = [s for v in t.values() for s in v["samples"] if s.get("error")]
    assert len(fails) == 1 and "stub failure" in fails[0]["error"] and "\n" not in fails[0]["error"]
    ok = next(s for v in t.values() for s in v["samples"] if not s.get("error"))
    assert ok["scene_ir_valid"] and ok["compiled"] and ok["tier1_ok"], ok
    assert result["totals"] == lim.totals() and lim.totals()["thinking_tokens"] == 150 and lim.totals()["input_tokens"] == 300
    print(f"ok  max_calls=4 stops the 10-call text plan after 4 stub calls (1 failed call counted); aborted={result['aborted']!r}")
    print(f"ok  stub sample validated as SceneIR, compiled, Tier-1 ok={ok['tier1_ok']}; totals={lim.totals()}")

    # 3) token limit inside the group runner
    n_calls["n"] = 0
    args = pytypes.SimpleNamespace(max_calls=100, max_tokens=7_000, only=["text"], roles=None, n_override=None)
    lim = Limiter(args.max_calls, args.max_tokens)
    result = make_result(args, lim)
    asyncio.run(run_groups(args, lambda: stub, lim, result, lambda: None))
    assert result["aborted"] and "max_tokens=7000" in result["aborted"] and lim.calls == 0, (result["aborted"], lim.calls)
    print(f"ok  reservation (8000) > max_tokens (7000) refuses before any call; aborted={result['aborted']!r}")

    # 4) a dispatched call with unknown usage keeps its reservation, so repeated failures cannot stay admissible
    class FailModels:
        async def generate_content(self, **kw: Any) -> Any:
            raise TimeoutError("stub timeout")

    lim = Limiter(max_calls=100, max_tokens=8_000)
    cc = CountedClient(pytypes.SimpleNamespace(aio=pytypes.SimpleNamespace(models=FailModels(), decisions=None)), lim)
    cc.est_tokens = 8_000

    async def _fail_twice() -> None:
        try:
            await cc.aio.models.generate_content(model="stub")
        except TimeoutError:
            pass
        await cc.aio.models.generate_content(model="stub")

    try:
        asyncio.run(_fail_twice())
        print("FAIL: a second call was admitted after a timed-out call with an 8000-token reservation")
        return 1
    except LimitReached as e:
        assert lim.calls == 1 and lim.unknown_usage_reserved == 8_000 and lim.tokens == 8_000, lim.totals()
        print(f"ok  timed-out call keeps its reservation; the next call is refused: {e}")

    class NoUsageResp:
        text, usage_metadata, candidates = "{}", None, []

    class NoUsageModels:
        async def generate_content(self, **kw: Any) -> Any:
            return NoUsageResp()

    lim = Limiter(max_calls=100, max_tokens=8_000)
    cc = CountedClient(pytypes.SimpleNamespace(aio=pytypes.SimpleNamespace(models=NoUsageModels(), decisions=None)), lim)
    cc.est_tokens = 5_000
    asyncio.run(cc.aio.models.generate_content(model="stub"))
    assert lim.unknown_usage_reserved == 5_000 and lim.tokens == 5_000 and cc.events[-1]["tokens_known"] is False, lim.totals()
    print("ok  successful response without usage metadata books its reservation")
    print("SELF-TEST PASSED")
    return 0


# ----------------------------------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default="benchmarks/results/live.json")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and exit; no client, no network")
    ap.add_argument("--only", nargs="+", choices=GROUPS, default=list(GROUPS), metavar="GROUP")
    ap.add_argument("--max-calls", type=int, default=24)
    ap.add_argument("--max-tokens", type=int, default=400_000)
    ap.add_argument("--self-test", action="store_true", help="exercise the spend limiter against a stub (no network)")
    ap.add_argument("--n-override", type=int, default=None, help=argparse.SUPPRESS)  # dev: samples per entry
    ap.add_argument("--roles", nargs="+", default=None, help=argparse.SUPPRESS)  # dev: restrict text/stage tiers
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    rows = build_plan(args.only, args.roles, args.n_override)
    total = print_plan(rows, args.max_calls, args.max_tokens)
    if total > args.max_calls:
        print(f"refusing to start: planned calls {total} > --max-calls {args.max_calls}", file=sys.stderr)
        return 2
    if args.dry_run:
        return 0
    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        print("GEMINI_API_KEY is not set", file=sys.stderr)
        return 2

    def client_factory() -> Any:
        from google import genai
        from google.genai import types

        # retries off at the client level too; per-request timeouts are set in each group.
        return genai.Client(http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=1)))

    limiter = Limiter(args.max_calls, args.max_tokens)
    result = make_result(args, limiter)
    out = Path(args.out)
    flush = lambda: write_json(out, result, limiter)  # noqa: E731
    try:
        asyncio.run(run_groups(args, client_factory, limiter, result, flush))
    except KeyboardInterrupt:
        result["aborted"] = "KeyboardInterrupt"
    write_json(out, result, limiter)
    print(f"wrote {out}  totals={limiter.totals()} aborted={result['aborted']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
