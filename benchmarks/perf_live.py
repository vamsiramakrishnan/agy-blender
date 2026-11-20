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
  * The ``stage`` group drives the Antigravity SDK Agent with its model endpoint pointed at a local relay
    (``ModelRelay``, ``GeminiAPIEndpoint(base_url=...)``) that forwards each model call to the Gemini API, so every
    call is counted and the limiter is checked before each one; the SDK's API retries are set to 0. Tasks: ``crate``
    (the original) and ``flush_repair`` (a floating lid made flush plus a handle on it: needs a repair edit).
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

TIMEOUTS = {"stage": 600.0, "judge": 120.0, "image": 180.0}
# Text samples are dispatched with the production request (metered_author.build_author_request) and the production
# per-tier timeout (metered_author.AUTHOR_TIMEOUT_S), so a call that production would wait for is not cut short here.
# (role, n) per group. Judge: (stage, n).
TEXT_PLAN = (("worker", 3), ("builder", 3), ("bulk", 3), ("director", 1))
STAGE_PLAN = (("worker", 2), ("builder", 1))
JUDGE_PLAN = (("blockout", 3), ("lookdev", 2))
IMAGE_PLAN = (1024, 2048)  # one seamless albedo per size tier (1K, 2K)
# Token reservations checked before each call (a floor-check, not a budget): real usage is booked after the call.
# Text: the request's own max_output_tokens plus TEXT_INPUT_EST (rules + budget + task ~1k, schema ~5k prompt tokens).
TEXT_INPUT_EST = 7_000
EST_TOKENS = {"stage": 40_000, "judge": 6_000, "image": 4_500, "probe": 200}


def text_timeout(role: str) -> float:
    from antigravity3d.tactics.metered_author import author_timeout

    return author_timeout(role)


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
    """Exactly the request MeteredAuthor.propose sends (metered_author.build_author_request), minus count_tokens and
    the spend governor; the response is classified with the same check_response / parse as production."""
    from antigravity3d.state import EngineBudget
    from antigravity3d.tactics.authoring import _slug
    from antigravity3d.tactics.metered_author import (
        AuthorOutcomeError, build_author_request, check_response, propose_task, usage_tokens,
    )

    budget = EngineBudget("unreal_5")
    asset = _slug(ASSET_PROMPT)
    task = propose_task(ASSET_PROMPT, index=0, n=1, asset_name=asset, references=False)
    for role, n in plan:
        timeout = text_timeout(role)
        req = build_author_request(role, "propose", task, budget=budget, context={}, timeout_s=timeout, asset_name=asset)
        client.timeout_s, client.est_tokens = timeout, TEXT_INPUT_EST + req.max_output_tokens
        key = f"{role}:{req.model}"
        samples: list[dict[str, Any]] = []
        out[key] = {"samples": samples}
        for i in range(n):
            n_ev = len(client.events)
            mt = req.config.thinking_config
            sample: dict[str, Any] = {"thinking_level": mt.thinking_level.name.lower() if mt else None, "timeout_s": timeout,
                                      "max_output_tokens": req.max_output_tokens, "answer_tokens": req.budget.answer_tokens,
                                      "thinking_tokens_allowed": req.budget.thinking_tokens}
            resp = None
            try:
                resp = await client.aio.models.generate_content(model=req.model, contents=req.contents, config=req.config)
            except LimitReached:
                raise
            except Exception:  # noqa: BLE001 - already in the event log
                pass
            sample.update(_sample_from_event(client.events[n_ev] if len(client.events) > n_ev else None))
            if resp is not None:
                try:
                    text = check_response(req, resp, usage_tokens(resp))
                    sample["response_chars"] = len(text)
                    scene = req.parse(text)
                    sample.update(outcome="ok", scene_ir_valid=True, nodes=len(scene.nodes), materials=len(scene.materials),
                                  asset_name_matches=scene.asset_name == asset)
                    sample.update(compile_tier1(scene, budget))
                except AuthorOutcomeError as e:
                    sample.update(outcome=type(e).__name__, scene_ir_valid=False, parse_error=one_line(e, 200), tier1_ok=False)
                except Exception as e:  # noqa: BLE001
                    sample.update(outcome="invalid", scene_ir_valid=False, parse_error=one_line(e, 200), tier1_ok=False)
            samples.append(sample)
            out[key] = stats_block(samples)
            _progress("text", req.model, sample, client.limiter)
            flush()


# ----------------------------------------------------------------------------------------------- group: stage

# A part that must sit flush on another, starting from a scene where it does not: needs at least one repair edit.
FLUSH_PROMPT = ("The crate's lid floats above its body. Make the lid sit flush on the body (no gap, no overlap) and add a "
                "cylindrical handle (id handle, radius 0.02 m, length 0.3 m) lying along X, resting on top of the lid at its "
                "centre. Then run visual QA.")
STAGE_TASKS = ("crate", "flush_repair")
STAGE_TASK_PLAN = {"crate": STAGE_PLAN, "flush_repair": (("worker", 1), ("builder", 1))}
STAGE_CALL_EST = 12_000  # per model call inside a stage run: token reservation checked by the relay before forwarding
GEMINI_UPSTREAM = "https://generativelanguage.googleapis.com"
EDIT_TOOLS = ("apply_scene_ir", "propose_variants", "apply_shader_diff")


def _flush_seed() -> Any:
    from antigravity3d.ir import SceneIR

    def box(nid: str, size: list[float], z: float) -> dict[str, Any]:
        return {"id": nid, "kind": "box", "params": {"size": size}, "transform": {"translate": [0, 0, z]}}

    return SceneIR.model_validate({"asset_name": "Crate", "nodes": [box("body", [0.8, 0.6, 0.5], 0.25),
                                                                     box("lid", [0.84, 0.64, 0.06], 0.60)]})


def _flush_check(meshes: list[Any]) -> dict[str, Any]:
    """The flush task's own success test, from world-space vertices: lid on body, handle on lid along X."""
    import numpy as np

    bb = {m.node_id: (m.points.min(axis=0), m.points.max(axis=0)) for m in meshes}
    if not {"body", "lid", "handle"} <= set(bb):
        return {"ok": False, "why": f"nodes {sorted(bb)}"}
    gap_lid = float(bb["lid"][0][2] - bb["body"][1][2])
    gap_handle = float(bb["handle"][0][2] - bb["lid"][1][2])
    ext = bb["handle"][1] - bb["handle"][0]
    centred = float(np.abs((bb["handle"][0][:2] + bb["handle"][1][:2]) / 2 - (bb["lid"][0][:2] + bb["lid"][1][:2]) / 2).max())
    ok = abs(gap_lid) <= 2e-3 and abs(gap_handle) <= 2e-3 and abs(float(ext[0]) - 0.3) <= 0.01 and float(ext[1]) <= 0.05 and centred <= 0.01
    return {"ok": ok, "lid_gap_m": round(gap_lid, 4), "handle_gap_m": round(gap_handle, 4),
            "handle_extent_m": [round(float(x), 3) for x in ext], "handle_offcentre_m": round(centred, 4)}


def _static_chars(body: dict[str, Any]) -> dict[str, int]:
    system = "".join(p.get("text", "") for p in (body.get("systemInstruction") or {}).get("parts", []))
    decls = [d for t in body.get("tools") or [] for d in t.get("functionDeclarations") or []]
    tools = sum(len(json.dumps(d, separators=(",", ":"))) for d in decls)
    return {"system_chars": len(system), "tool_chars": tools, "tools": len(decls)}


class ModelRelay:
    """Local relay between the Antigravity harness and the Gemini API, so a stage run's model calls are counted.

    The harness posts plain Gemini REST requests to ``GeminiAPIEndpoint(base_url=...)``; the relay forwards each one
    upstream (key from the environment, never logged), and books its reported tokens on the limiter (a stage run still
    counts as one limiter call, booked by ``stage_group``). The token limit is checked BEFORE every forwarded call
    (``STAGE_CALL_EST`` reservation); a call that would exceed it is answered with HTTP 400 and never reaches the API.
    Network failures and answers without usage keep their reservation booked.
    """

    def __init__(self, limiter: Limiter, upstream: str = GEMINI_UPSTREAM) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.limiter, self.upstream = limiter, upstream
        self.calls: list[dict[str, Any]] = []
        self.aborted: str | None = None
        self._lock = threading.Lock()
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # http.server API name
                raw = self.rfile.read(int(self.headers.get("content-length", 0)))
                status, payload = relay.forward(self.path, raw)
                self.send_response(status)
                self.send_header("Content-Type", "text/event-stream" if status == 200 else "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def forward(self, path: str, raw: bytes) -> tuple[int, bytes]:
        import urllib.error
        import urllib.request

        body = json.loads(raw or b"{}")
        rec: dict[str, Any] = {"path": path.split("?")[0].rsplit("/", 1)[-1], **_static_chars(body)}
        with self._lock:
            try:
                if self.limiter.tokens + STAGE_CALL_EST > self.limiter.max_tokens:
                    raise LimitReached(f"max_tokens={self.limiter.max_tokens} would be exceeded (used {self.limiter.tokens}, "
                                       f"next stage model call reserves ~{STAGE_CALL_EST})")
            except LimitReached as e:
                self.aborted = str(e)
                rec["refused"] = str(e)
                self.calls.append(rec)
                return 400, json.dumps({"error": {"code": 400, "message": f"benchmark spend limit: {e}"}}).encode()
            key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""
            req = urllib.request.Request(self.upstream + path, data=raw, method="POST",
                                         headers={"Content-Type": "application/json", "x-goog-api-key": key})
            t0 = time.perf_counter()
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUTS["stage"]) as r:
                    status, payload = r.status, r.read()
            except urllib.error.HTTPError as e:
                status, payload = e.code, e.read()
            except Exception as e:  # noqa: BLE001 - network failure: unknown usage, keep the reservation
                self.limiter.unknown_usage_reserved += STAGE_CALL_EST
                rec.update(error=one_line(e, 200), tokens_known=False)
                self.calls.append(rec)
                return 502, json.dumps({"error": {"code": 502, "message": "relay: upstream unreachable"}}).encode()
            rec["wall_s"] = round(time.perf_counter() - t0, 2)
            usage: dict[str, Any] = {}
            finish = None
            for line in payload.decode("utf-8", "replace").splitlines():
                if line.startswith("data: "):
                    try:
                        event = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    usage = event.get("usageMetadata") or usage
                    finish = next((c.get("finishReason") for c in event.get("candidates") or [] if c.get("finishReason")), finish)
            rec["finish_reason"] = finish
            rec["usage"] = usage
            if status == 200 and usage:
                inp, out, think = (int(usage.get(k) or 0) for k in ("promptTokenCount", "candidatesTokenCount", "thoughtsTokenCount"))
                self.limiter.input_tokens += inp
                self.limiter.output_tokens += out
                self.limiter.thinking_tokens += think
                rec.update(prompt_tokens=inp, output_tokens=out, thinking_tokens=think,
                           cached_tokens=int(usage.get("cachedContentTokenCount") or 0), tokens_known=True)
            elif status >= 500 or status == 200:  # no usage reported: may have been billed
                self.limiter.unknown_usage_reserved += STAGE_CALL_EST
                rec.update(status=status, tokens_known=False)
            else:  # rejected request (4xx): not billed
                rec.update(status=status, error=payload.decode("utf-8", "replace")[:300], tokens_known=True)
            self.calls.append(rec)
            return status, payload


def _relay_config(relay: ModelRelay) -> Any:
    """Route every model of a stage config through the relay, with the SDK's own retries off."""
    from google.antigravity import types
    from google.antigravity.models import GeminiAPIEndpoint

    def configure(cfg: Any) -> Any:
        models = [m.model_copy(update={"endpoint": GeminiAPIEndpoint(base_url=relay.url, api_key="via-relay",
                                                                      options=m.endpoint.options)}) for m in cfg.models]
        # API retries off. Not ModelOutputRetryConfig(max_retries=0): with it the 0.1.24 harness sends every request
        # with functionCallingConfig mode NONE (verified offline on the posted request), so no tool can be called.
        # A malformed-output re-ask is therefore possible; it is one more relayed call, counted and shown with its
        # finish_reason in the sample.
        retry = types.RetryConfig(api_retry=types.ModelAPIRetryConfig(max_retries=0))
        return cfg.model_copy(update={"model": None, "models": models, "retry_config": retry})

    return configure


async def stage_group(limiter: Limiter, plan: tuple[tuple[str, int], ...], out: dict[str, Any], flush: Any,
                      tasks: tuple[str, ...] = ("crate",)) -> None:
    """run_stage('BLOCKOUT') with the real model, FAKE decisions and FAKE image backend (as the package's live test).

    Every model call of the stage goes through :class:`ModelRelay`, so calls, per-call prompt tokens and the static
    prompt size are measured, and the limiter is checked before each call."""
    from antigravity3d.decisions.engine import DecisionControlPlane
    from antigravity3d.decisions.fakes import FakeDecisionsClient
    from antigravity3d.genmedia.nano_banana import FakeImageBackend, ImageForge
    from antigravity3d.harness import session as S
    from antigravity3d.harness.agents import run_stage
    from antigravity3d.kernel.fast_ops import tier1_qa
    from antigravity3d.models import tier
    from antigravity3d.state import EngineBudget, SceneSessionRegistry

    relay = ModelRelay(limiter)
    try:
        for task in tasks:
            prompt, seed = (STAGE_PROMPT, None) if task == "crate" else (FLUSH_PROMPT, _flush_seed())
            roles = dict(plan) if task == "crate" else {r: n for r, n in STAGE_TASK_PLAN[task] if r in dict(plan)}
            for role, n in roles.items():
                mt = tier(role)  # type: ignore[arg-type]
                key = f"{task}:{role}:{mt.model}" if task != "crate" else f"{role}:{mt.model}"
                samples: list[dict[str, Any]] = []
                out[key] = {"samples": samples}
                for i in range(n):
                    if relay.aborted:
                        raise LimitReached(relay.aborted)
                    limiter.before(f"stage {task} {mt.model}", STAGE_CALL_EST)
                    reg, sid = SceneSessionRegistry(), f"bench-{task}-{role}-{i}"
                    budget = EngineBudget("unreal_5")
                    workdir = Path(tempfile.mkdtemp(prefix="perf_live_stage_"))
                    S.open_session(reg, sid, workdir / sid, "Crate", budget, plane=DecisionControlPlane(FakeDecisionsClient()),
                                   forge=ImageForge(FakeImageBackend()))
                    if seed is not None:
                        S.session(reg, sid).stage.apply_ir(seed)
                    sample: dict[str, Any] = {"task": task, "timeout_s": TIMEOUTS["stage"],
                                              "thinking_level": mt.thinking.value if mt.thinking else None}
                    first_call = len(relay.calls)
                    t0 = time.perf_counter()
                    run = None
                    try:
                        run = await run_stage("BLOCKOUT", sid, prompt, registry=reg, role=role, timeout_s=TIMEOUTS["stage"],
                                              configure=_relay_config(relay))
                    except Exception as e:  # noqa: BLE001
                        sample.update(error=one_line(e), error_class=type(e).__name__)
                    wall_s = time.perf_counter() - t0
                    limiter.calls += 1  # the run is one limiter call; its model calls' tokens were booked by the relay
                    calls = relay.calls[first_call:]
                    main = [c for c in calls if c.get("tools", 0) > 3]  # the stage agent (subagents declare fewer tools)
                    sample.update(wall_s=round(wall_s, 2), wall_ms=round(wall_s * 1000, 1), model_calls=len(calls),
                                  prompt_tokens_per_call=[c.get("prompt_tokens") for c in calls],
                                  static_chars_first_call=(main[0]["system_chars"] + main[0]["tool_chars"]) if main else None,
                                  prompt_tokens=sum(c.get("prompt_tokens") or 0 for c in calls),
                                  output_tokens=sum(c.get("output_tokens") or 0 for c in calls),
                                  thinking_tokens=sum(c.get("thinking_tokens") or 0 for c in calls),
                                  tokens_known=all(c.get("tokens_known") for c in calls))
                    sample["total_tokens"] = sample["prompt_tokens"] + sample["output_tokens"] + sample["thinking_tokens"]
                    sample["calls"] = [{k: c.get(k) for k in ("finish_reason", "prompt_tokens", "cached_tokens", "output_tokens",
                                                              "thinking_tokens", "system_chars", "tool_chars", "tools", "wall_s", "error")}
                                       for c in calls]
                    try:
                        s = S.session(reg, sid)
                        tel_path = s.workdir / "telemetry.jsonl"
                        recs = [json.loads(x) for x in tel_path.read_text().splitlines() if x.strip()] if tel_path.exists() else []
                        sample["tool_calls"] = len(recs)
                        sample["tool_durations_ms"] = [{"tool": r["tool"], "ms": r["ms"], "error": r["error"], "soft_fail": r["soft_fail"]}
                                                       for r in recs]
                        edits = [r for r in recs if r["tool"] in EDIT_TOOLS]
                        sample["first_edit_valid"] = bool(edits) and not (edits[0]["error"] or edits[0]["soft_fail"])
                        sample["edit_calls"] = len(edits)
                        tool_ms = sum(r["ms"] or 0 for r in recs)
                        sample["tool_time_s"] = round(tool_ms / 1000, 3)
                        sample["model_side_s"] = round(wall_s - tool_ms / 1000, 2)
                        if run:
                            sample["telemetry_summary"] = run.telemetry
                            sample["stop_continuations"] = run.stop_continuations
                            sample["final_text"] = run.text[:300]
                        scene = s.stage.base_ir()
                        meshes = s.stage.extract_meshes()
                        sample["final_nodes"] = len(scene.nodes)
                        sample["tier1_ok"] = tier1_qa(meshes, budget).ok if scene.nodes else False
                        sample["qa_last"] = s.state.audit_deltas[-1] if s.state.audit_deltas else None
                        sample["qa_approved"] = bool(sample["qa_last"]) and " approve" in sample["qa_last"]
                        sample["task_ok"] = sample["tier1_ok"] and sample["qa_approved"]
                        if task == "flush_repair":
                            sample["flush"] = _flush_check(meshes)
                            sample["task_ok"] = sample["task_ok"] and sample["flush"]["ok"]
                    except Exception as e:  # noqa: BLE001
                        sample["post_error"] = one_line(e, 200)
                    samples.append(sample)
                    out[key] = stats_block(samples)
                    _progress("stage", f"{task}/{mt.model}", sample, limiter)
                    flush()
    finally:
        relay.close()


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


async def image_group(client: CountedClient, out: dict[str, Any], flush: Any, sizes: tuple[int, ...] = IMAGE_PLAN) -> None:
    """One seamless albedo per size in ``sizes`` via the real ImageForge/GenAIImageBackend (production request + receipt)."""
    from antigravity3d.genmedia.nano_banana import GenAIImageBackend, ImageForge, _size_tier, default_image_model
    from antigravity3d.tactics.governor import Governor, plan_for

    client.timeout_s, client.est_tokens = TIMEOUTS["image"], EST_TOKENS["image"]
    model = default_image_model()
    for size in sizes:
        tier_name = _size_tier(size)
        key = f"image:{model}:{tier_name}"
        sample: dict[str, Any] = {"requested_size": size, "size_tier": tier_name, "timeout_s": TIMEOUTS["image"],
                                  "max_output_tokens": GenAIImageBackend.output_limit(tier_name)}
        n_ev = len(client.events)
        try:
            # A roomy ledger: the benchmark measures the request, not a plan's admission (no rates are read here).
            g = await ImageForge(client=client, model=model, governor=Governor(plan_for(3))).seamless_albedo(ALBEDO_PROMPT, size=size)
            w, h = g.image().size
            rc = g.receipt
            sample.update(wall_ms=round(g.latency_ms, 1), pixel_size=[w, h], bytes=len(g.png_bytes), model=g.model,
                          receipt_input_tokens=rc.input_tokens, receipt_output_tokens=rc.output_tokens,
                          receipt_image_tokens=rc.image_tokens, receipt_thinking_tokens=rc.thinking_tokens,
                          receipt_unitemized_output_tokens=rc.unitemized_output_tokens,
                          receipt_missing_usage_fields=list(rc.missing_usage_fields), receipt_finish_reason=rc.finish_reason,
                          receipt_source=rc.source)  # booked_usd deliberately NOT recorded: dollar cost is unknown here
        except LimitReached:
            raise
        except Exception as e:  # noqa: BLE001
            sample.update(error=one_line(e), error_class=type(e).__name__)
        if len(client.events) > n_ev:
            ev = client.events[n_ev]
            sample.update({k: ev[k] for k in ("input_tokens", "output_tokens", "thinking_tokens", "tokens_known", "finish_reason")
                           if k in ev})
            if "usage_raw" in ev:
                sample["usage_metadata"] = ev["usage_raw"]
            sample.setdefault("wall_ms", ev["wall_ms"])
            sample["api_wall_ms"] = ev["wall_ms"]
            if ev.get("error") and "error" not in sample:
                sample["error"] = ev["error"]
        out[key] = stats_block([sample])
        _progress("image", f"{model} {tier_name}", sample, client.limiter)
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
                                 timeout_s=text_timeout(role), calls=nn(n)))
    if "stage" in groups:
        for role, n in STAGE_PLAN:
            if keep(role):
                rows.append(dict(group="stage", role=role, model=tier(role).model, n=nn(n),  # type: ignore[arg-type]
                                 timeout_s=TIMEOUTS["stage"], calls=nn(n),
                                 note="task crate; 1 call per run, its model calls relayed and their tokens booked"))
        for role, n in STAGE_TASK_PLAN["flush_repair"]:
            if keep(role):
                rows.append(dict(group="stage", role=role, model=tier(role).model, n=nn(n),  # type: ignore[arg-type]
                                 timeout_s=TIMEOUTS["stage"], calls=nn(n),
                                 note="task flush_repair; 1 call per run, its model calls relayed and booked"))
    if "judge" in groups:
        rows.append(dict(group="judge", role="decision", model="gemini-decision-1 (native probe)", n=1, timeout_s=TIMEOUTS["judge"], calls=1))
        for stg, n in JUDGE_PLAN:
            rows.append(dict(group="judge", role=f"visual_qa:{stg}", model=f"{tier('bulk').model} (generative fallback)", n=nn(n),
                             timeout_s=TIMEOUTS["judge"], calls=nn(n), note="1 request per evaluate unless packets split"))
    if "image" in groups:
        rows.append(dict(group="image", role="image", model=tier("image").model, n=len(IMAGE_PLAN), timeout_s=TIMEOUTS["image"],
                         calls=len(IMAGE_PLAN), note="sizes " + ", ".join(map(str, IMAGE_PLAN))))
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
                await stage_group(limiter, tuple((r, nn(n)) for r, n in STAGE_PLAN if keep(r)), groups[g], flush,
                                  tasks=tuple(args.stage_tasks))
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
                                  "stage: Antigravity RetryConfig api_retry max_retries=0; a model-output re-ask, if any, is a counted call)",
                       "prices": "none configured; dollar cost is unknown, token counts only",
                       "stage_accounting": "every model call of a stage run is relayed (ModelRelay) and booked with its own usage"},
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
        prompt_token_count, candidates_token_count, thoughts_token_count, total_token_count = 100, 200, 50, 350

    class Resp:
        text, usage_metadata, prompt_feedback = payload, Usage(), None
        candidates = [pytypes.SimpleNamespace(finish_reason="STOP")]

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
    print(f"ok  reservation (input estimate + max_output_tokens) > max_tokens (7000) refuses before any call; "
          f"aborted={result['aborted']!r}")

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
    ap.add_argument("--stage-tasks", nargs="+", choices=STAGE_TASKS, default=list(STAGE_TASKS), help=argparse.SUPPRESS)
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
