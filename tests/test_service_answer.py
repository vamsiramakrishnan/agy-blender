"""A call the service did not serve, or whose connection broke, as seen live on 2026-11-21.

* run r2_crate_C.s2: pass A's repair (gemini-4-argon) was answered ``503 UNAVAILABLE`` in 0.11 s, was not retried, failed
  the pyramid node (no package, after the design and the textures were paid for) and kept its whole reservation (45,486
  tokens) for a request the service turned away;
* run c2: pass A's visual QA (fallback judge) lost its connection while reading the answer (httpx ``ReadError``): no
  retry, the node failed, nothing was exported.

Pinned here, offline against ``tests/no_decisions_key.py``: the classification and the schedule (``service_answer``); an
attempt that was not served books nothing on authoring, judge and image calls, while an attempt whose outcome is unknown
keeps its reservation; a repair that cannot be had ends pass A as "author repair failed", a verdict that cannot be had
stops its pass as "judge unavailable" with the gates failed, and either way the run exports what exists. No test sleeps
for real (the shared policy's ``sleep`` is replaced) and none calls a live API.
"""

from __future__ import annotations

import asyncio
import glob
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from antigravity3d import cli, service_answer
from antigravity3d.decisions import questions as Q
from antigravity3d.decisions.engine import DecisionControlPlane
from antigravity3d.decisions.models import TransientDecisionError
from antigravity3d.tactics.governor import Governor, plan_for

sys.path.insert(0, str(Path(__file__).parent))
from no_decisions_key import NoDecisionsKey  # noqa: E402

FAST = ["--art-direction", "retro-lowres"]
BLOCKOUT_QA = {"questions": ("score_silhouette",), "exclude": ("next_action",)}  # pass A's visual QA only


class ApiErr(Exception):
    """The shape of ``google.genai.errors.APIError``: code, status and message."""

    def __init__(self, code: int, status: str | None, message: str = "") -> None:
        super().__init__(f"{code} {status}. {message}")
        self.code, self.status, self.message = code, status, message


class ReadError(Exception):  # httpx.ReadError's name, without importing httpx
    pass


class TransportError(Exception):
    pass


class RemoteProtocolError(TransportError):
    pass


# ---------------------------------------------------------------------------------------------- the classification


@pytest.mark.parametrize("exc, elapsed, deadline, outcome", [
    (ApiErr(503, "UNAVAILABLE", "high demand"), 0.11, 540.0, "not_served"),  # the live 503
    (ApiErr(429, "RESOURCE_EXHAUSTED"), 0.2, None, "not_served"),
    (ApiErr(503, "UNAVAILABLE"), 7.5, 540.0, "unknown"),  # slower than the bound: it may have followed work
    (ApiErr(503, None), 0.1, None, "unknown"),  # no service status (a proxy's 503): not proof it was not served
    (ApiErr(500, "INTERNAL"), 0.1, None, "unknown"),
    (ApiErr(502, None), 0.1, None, "unknown"),
    (ReadError(""), 43.0, 60.0, "unknown"),  # the live dropped connection
    (RemoteProtocolError("Server disconnected"), 0.0, None, "unknown"),
    (ConnectionResetError(), 0.0, None, "unknown"),
    (ApiErr(504, "DEADLINE_EXCEEDED"), 59.0, 60.0, "final"),  # ran out its own deadline: not sent again
    (ApiErr(504, "DEADLINE_EXCEEDED"), 0.2, 60.0, "unknown"),  # an early 504: a gateway
    (TimeoutError(), 0.0, 90.0, "final"),  # a deadline fired
    (ApiErr(400, "INVALID_ARGUMENT"), 0.1, None, "final"),  # a bug in the request
    (RuntimeError("boom"), 0.1, None, "final"),
])
def test_classification(exc, elapsed, deadline, outcome):
    assert service_answer.classify(exc, elapsed, deadline_s=deadline) == outcome


def test_the_schedule_and_its_bound():
    p = service_answer.RetryPolicy()
    assert [p.delay(n) for n in range(1, 6)] == [2.0, 4.0, 8.0, 16.0, 32.0] and p.max_wait_s == 62.0
    hinted = ApiErr(429, "RESOURCE_EXHAUSTED")
    hinted.details = {"error": {"details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "17s"}]}}
    assert p.delay(1, hinted) == 17.0 and p.delay(5, hinted) == 32.0  # at least the service's hint, each wait capped
    hinted.details["error"]["details"][0]["retryDelay"] = "300s"
    assert p.delay(1, hinted) == 32.0
    assert service_answer.describe(ApiErr(503, "UNAVAILABLE", "high demand")) == "503 UNAVAILABLE: high demand"


# ---------------------------------------------------------------------------------------------- the judge plane


class Script:
    """A decisions client: raises the scripted errors in order, then answers."""

    def __init__(self, *errors: BaseException) -> None:
        self.errors, self.calls = list(errors), 0
        self.aio = SimpleNamespace(decisions=self)

    async def create(self, **_):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return {"answers": {"a": {"type": "noul", "noul": 0.2, "confidence": 0.9}}, "usage": {"total_input_tokens": 100}}


async def no_sleep(_s):
    return None


async def test_a_judge_call_not_served_books_nothing_and_an_unknown_outcome_keeps_its_reservation():
    waits: list[float] = []

    async def sleep(s):
        waits.append(s)

    gov = Governor(plan_for(2), token_ceiling=10_000_000)
    plane = DecisionControlPlane(Script(ApiErr(503, "UNAVAILABLE", "high demand"), ReadError("")), backend_kind="decisions", sleep=sleep)
    r = await plane.evaluate("x", {"a": Q.noul("q")}, tag="t", governor=gov)
    first, second, third = gov.receipts()
    assert first["status"] == "not_served" and first["booked_tokens"] == 0 and first["http_code"] == 503 and first["attempt"] == 1
    assert "nothing is booked" in first["booked_reason"] and first["next_wait_s"] == 2.0
    assert second["status"] == "ReadError" and second["booked_tokens"] == second["reserved_tokens"]  # it may have run
    assert third["status"] == "ok" and r.attempts == 3 and waits == [2.0, 4.0]
    assert r.booked_tokens == second["reserved_tokens"] + third["booked_tokens"]


async def test_a_judge_call_always_turned_away_gives_up_after_the_schedule_and_books_nothing():
    gov = Governor(plan_for(2), token_ceiling=10_000_000)
    script = Script(*[ApiErr(503, "UNAVAILABLE", "high demand")] * 10)
    with pytest.raises(TransientDecisionError, match="gave up after 6 attempts .*62 s of waiting; 6 turned away"):
        await DecisionControlPlane(script, backend_kind="decisions", sleep=no_sleep).evaluate("x", {"a": Q.noul("q")}, tag="t", governor=gov)
    assert script.calls == 6 and {r["status"] for r in gov.receipts()} == {"not_served"}
    assert sum(r["booked_tokens"] for r in gov.receipts()) == 0
    # the switch the live benchmark uses: every call sent once
    once = Script(ApiErr(503, "UNAVAILABLE"))
    with pytest.raises(TransientDecisionError):
        await DecisionControlPlane(once, backend_kind="decisions", sleep=no_sleep, max_retries=0,
                                   retry=service_answer.NO_RETRY).evaluate("x", {"a": Q.noul("q")}, tag="t")
    assert once.calls == 1


async def test_the_fallback_says_how_many_dispatches_were_served_when_a_call_was_partly_turned_away():
    """Two chunks: one served, one turned away. The served chunk's usage is lost with the call, so the attempt is not a
    rejection: it keeps its reservation, and the receipt counts served and turned-away dispatches."""
    from antigravity3d.decisions.fallback import GenerativeDecisionsBackend

    class Models:
        def __init__(self) -> None:
            self.n = 0

        async def count_tokens(self, **_):
            return SimpleNamespace(total_tokens=50)

        async def generate_content(self, *, contents, config, **_):
            self.n += 1
            if self.n == 2:
                raise ApiErr(503, "UNAVAILABLE", "high demand")
            props = config.response_json_schema["properties"]
            text = json.dumps({q: 0.2 for q in props})
            return SimpleNamespace(prompt_feedback=None, text=text, candidates=[SimpleNamespace(finish_reason="STOP")],
                                   usage_metadata=SimpleNamespace(prompt_token_count=10, candidates_token_count=5, thoughts_token_count=0))

    models = Models()
    backend = GenerativeDecisionsBackend(SimpleNamespace(aio=SimpleNamespace(models=models)), chunk_size=1, max_concurrency=1)
    gov = Governor(plan_for(2), token_ceiling=10_000_000)
    plane = DecisionControlPlane(backend, backend.model, backend_kind="generative_fallback", sleep=no_sleep, preview=True)
    r = await plane.evaluate("x", {"a": Q.noul("q"), "b": Q.noul("q")}, tag="t", governor=gov)
    first, second = gov.receipts()
    assert first["status"] == "OutcomeUnknown" and first["booked_tokens"] == first["reserved_tokens"]
    assert first["served_dispatches"] == 1 and first["failed_dispatches"] == 1 and first["turned_away_dispatches"] == 1
    assert second["status"] == "ok" and r.attempts == 2


# ---------------------------------------------------------------------------------------------- image calls


async def test_an_image_call_not_served_books_nothing_and_counts_no_image(monkeypatch):
    from antigravity3d.genmedia.nano_banana import GenAIImageBackend, ImageForge

    monkeypatch.setattr(service_answer.DEFAULT_POLICY, "sleep", no_sleep)
    calls = {"n": 0}

    async def gen(self, model, prompt, images, image_size, aspect):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise ApiErr(503, "UNAVAILABLE", "high demand")
        raise ApiErr(400, "INVALID_ARGUMENT", "stop here")  # a final answer ends the call: what matters is the ledger

    monkeypatch.setattr(GenAIImageBackend, "_generate_content", gen)
    gov = Governor(plan_for(2), token_ceiling=1_000_000, max_images=1)
    forge = ImageForge(GenAIImageBackend(SimpleNamespace(), gov), governor=gov)
    with pytest.raises(ApiErr, match="400"):
        await forge.seamless_albedo("oak", size=1024)
    a, b, c = gov.receipts()
    assert a["status"] == b["status"] == "not_served" and a["booked_tokens"] == b["booked_tokens"] == 0
    assert c["status"] == "ApiErr" and c["booked_tokens"] == c["reserved_tokens"] and c["attempt"] == 3
    assert gov.images_used == 1  # the two rejected attempts left the one-image limit for the third


# ---------------------------------------------------------------------------------------------- whole runs on the stand-in


@pytest.fixture
def key(monkeypatch):
    waits: list[float] = []

    async def sleep(s):
        waits.append(s)

    monkeypatch.setattr(service_answer.DEFAULT_POLICY, "sleep", sleep)

    def start(**kw):
        api = NoDecisionsKey(**kw).__enter__()
        api.waits = waits
        monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", api.url)
        monkeypatch.setenv("GEMINI_API_KEY", "dummy-not-a-key")
        monkeypatch.delenv("AGY3D_THREE_JS", raising=False)
        started.append(api)
        return api

    started: list[NoDecisionsKey] = []
    yield start
    for api in started:
        api.__exit__(None, None, None)


def run(tmp_path, capsys) -> tuple[int, str, dict]:
    rc = cli.main(["prop", "wooden crate with iron brackets", "--out", str(tmp_path / "out"), "--preview", "--token-ceiling", "210000",
                   "--max-images", "4", "--single-candidate", *FAST])
    out = capsys.readouterr().out
    data = json.loads((tmp_path / "out" / "report.json").read_text())
    x = dict(data.get("extras", data))
    x["nodes"] = {n["name"]: n for n in data.get("nodes", [])} if isinstance(data.get("nodes"), list) else data.get("nodes", {})
    return rc, out, x


def manifest(tmp_path) -> dict | None:
    found = glob.glob(str(tmp_path / "out" / "runs" / "*" / "package" / "manifest.json"))
    return json.loads(Path(found[0]).read_text()) if found else None


def receipts(x, category):
    return [r for r in x["cost"]["receipts"] if r["category"] == category]


def test_a_repair_turned_away_twice_then_served_books_nothing_for_the_two_and_records_the_waits(key, tmp_path, capsys):
    api = key(score_level="1", fail=[{"kind": "author:repair", "times": 2, "how": "503"}])  # silhouette 1: pass A asks for a repair
    rc, out, x = run(tmp_path, capsys)
    assert x["nodes"]["pyramid"]["status"] == "ok" and manifest(tmp_path) is not None, out
    rep = receipts(x, "author.repair")
    assert [r["status"] for r in rep] == ["not_served", "not_served", "AuthorInvalid"]  # the stand-in serves an empty patch
    assert [r["booked_tokens"] for r in rep[:2]] == [0, 0] and rep[2]["booked_tokens"] > 0 and rep[2]["attempt"] == 3
    assert [a["wait_s"] for a in rep[2]["service_retries"]] == [2.0, 4.0] and api.waits == [2.0, 4.0]
    assert api.failures() == ["author:repair:503", "author:repair:503"] and api.kinds().count("author:repair") == 1
    step = next(s for s in x["pyramid"]["steps"] if s["name"] == "author_repair")
    assert "not_served" in step["run"] and "waited 2 s" in step["run"] and "waited 4 s" in step["run"]


def test_a_repair_always_turned_away_ends_pass_a_and_the_run_exports_what_exists(key, tmp_path, capsys):
    api = key(score_level="1", fail=[{"kind": "author:repair", "times": None, "how": "503"}])
    rc, out, x = run(tmp_path, capsys)
    stopped = x["pyramid"]["stopped"]
    assert stopped["A"].startswith("author repair failed: AuthorUnavailable"), stopped
    assert "503 UNAVAILABLE: This model is currently experiencing high demand" in stopped["A"]
    assert "nothing booked: the service turned every attempt away" in stopped["A"]
    assert "B" in stopped and stopped["C"] == "verification run"  # passes B and C ran
    m = manifest(tmp_path)
    assert m is not None and x["nodes"]["package"]["status"] == "ok", out
    rep = receipts(x, "author.repair")
    assert len(rep) == 6 and {r["status"] for r in rep} == {"not_served"} and sum(r["booked_tokens"] for r in rep) == 0
    assert api.waits == [2.0, 4.0, 8.0, 16.0, 32.0] and api.kinds().count("author:repair") == 0


def test_a_judge_call_turned_away_then_served_books_nothing_for_the_rejected_attempt(key, tmp_path, capsys):
    api = key(fail=[{"kind": "judge", "times": 1, "how": "503"}])  # the first judge call: the route
    rc, out, x = run(tmp_path, capsys)
    assert rc == 0 and out.strip().splitlines()[-1] == "preview gates: PASS", out
    first, second = receipts(x, "route")
    assert first["status"] == "not_served" and first["booked_tokens"] == 0 and first["next_wait_s"] == 2.0
    assert second["status"] == "ok" and second["attempt"] == 2 and api.waits == [2.0]


def test_a_visual_qa_connection_dropped_once_is_retried_and_its_attempt_keeps_its_reservation(key, tmp_path, capsys):
    api = key(fail=[{"kind": "judge", "times": 1, "how": "drop", **BLOCKOUT_QA}])
    rc, out, x = run(tmp_path, capsys)
    assert rc == 0 and out.strip().splitlines()[-1] == "preview gates: PASS", out
    qa = receipts(x, "visual_qa")
    dropped = [r for r in qa if r["status"] not in ("ok", "blocked")]
    assert len(dropped) == 1 and dropped[0]["booked_tokens"] == dropped[0]["reserved_tokens"]  # outcome unknown: kept
    assert dropped[0]["source"] == "reservation_estimate" and api.failures() == ["judge:drop"] and api.waits == [2.0]


def test_a_visual_qa_always_dropped_stops_pass_a_and_exports_with_the_gates_failed(key, tmp_path, capsys):
    api = key(fail=[{"kind": "judge", "times": None, "how": "drop", **BLOCKOUT_QA}])
    rc, out, x = run(tmp_path, capsys)
    last = out.strip().splitlines()[-1]
    assert rc == 1 and last.startswith("preview gates: FAIL (pass A judge unavailable: blockout visual QA:"), out
    p = x["pyramid"]
    assert p["stopped"]["A"].startswith("judge unavailable:") and not p["converged"]["A"] and not p["ok"] and not p["verified"]
    assert p["tier1_ok"] is True and p["judge_unavailable"].startswith("pass A round 1")  # Tier-1 keeps its own result
    # pass B judged the textured asset that ships: its verdict is the score, and no pass A round was recorded
    assert [r["stage"] for r in p["rounds"]] == ["B"] and p["tier15"] == p["rounds"][-1]["tier15"]
    m = manifest(tmp_path)
    assert m is not None and m["verified"] is False and m["preview"] is True
    step = next(s for s in m["evidence"]["steps"] if s["name"] == "visual_qa_A")
    assert step["status"] == "failed" and step["detail"].startswith("judge unavailable: blockout visual QA")
    assert "B" in p["stopped"] and p["stopped"]["C"] == "verification run"
    dropped = [r for r in receipts(x, "visual_qa") if r["status"] not in ("ok", "blocked")]
    assert len(dropped) == 4 and all(r["booked_tokens"] == r["reserved_tokens"] for r in dropped)  # 1 + 3 retries, each kept
    assert api.waits == [2.0, 4.0, 8.0]


def test_with_no_visual_qa_verdict_at_all_the_judges_scores_are_missing_not_zero(key, tmp_path, capsys):
    api = key(fail=[{"kind": "judge", "times": None, "how": "503", "questions": ("score_silhouette",)}])
    rc, out, x = run(tmp_path, capsys)
    p = x["pyramid"]
    assert rc == 1 and p["stopped"]["A"].startswith("judge unavailable:") and p["stopped"]["B"].startswith("judge unavailable:")
    assert "503 UNAVAILABLE" in p["stopped"]["A"] and "preview gates: FAIL (pass A judge unavailable" in out.strip().splitlines()[-1]
    assert p["tier15"] is None and p["defects"] == {} and p["tier1_ok"] is True
    assert not [k for k in p["scores"] if k.startswith("score_")]  # no judge score (pass C's own measurements stay)
    assert x["scores"]["tier15"] is None and "tier15=n/a (pass A round 1" in out
    m = manifest(tmp_path)
    assert m is not None and m["qa"]["tier15"] is None and m["verified"] is False
    assert {s["name"]: s["status"] for s in m["evidence"]["steps"]}.keys() >= {"visual_qa_A", "visual_qa_B"}
    rejected = [r for r in receipts(x, "visual_qa") if r["status"] == "not_served"]
    assert rejected and sum(r["booked_tokens"] for r in rejected) == 0 and set(api.waits) <= {2.0, 4.0, 8.0, 16.0, 32.0}
