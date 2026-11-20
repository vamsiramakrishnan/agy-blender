"""The environment kit's live authoring path: admitted, concurrent, ordered, and every piece accounted for.

The provider is a scripted endpoint and prices are this module's own fixture; nothing is called and no price
environment variable is set outside a test's monkeypatch.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.genai import types

from antigravity3d import cli
from antigravity3d.dags.environment_kit import PIECE_CYCLE, EnvironmentKitDAG, PiecesFailed, kit_plan
from antigravity3d.genmedia.nano_banana import FakeImageBackend, ImageForge
from antigravity3d.models import tier
from antigravity3d.state import EngineBudget
from antigravity3d.tactics.authoring import AgentAuthor, TemplateAuthor
from antigravity3d.tactics.governor import BudgetExceeded, Governor, plan_for
from antigravity3d import ir_schema
from antigravity3d.tactics.metered_author import AUTHOR_OUTPUT_BUDGETS, schema_input_token_bound

UE = EngineBudget("unreal_5")
RATES = (1.0, 2.0)  # USD per million tokens, this module's fixture
COUNTED = 100
BOX = ('{"asset_name":"%s","materials":[],"nodes":[{"id":"body","kind":"box","params":{"size":[1,1,1]},'
       '"transform":{"translate":[0,0,0.5]}}]}')


class KitEndpoint:
    """Scripted provider: answers per asset name, takes ``delay`` seconds per call, records concurrency."""

    def __init__(self, delay: float = 0.0, script: dict[str, object] | None = None, usage: tuple[int, int] = (100, 50),
                 reverse: bool = False) -> None:
        self.delay, self.script, self.usage, self.reverse = delay, script or {}, usage, reverse
        self.running = self.peak = 0
        self.generated: list[str] = []
        self.counted = 0

    async def count_tokens(self, **kwargs):
        self.counted += 1
        return SimpleNamespace(total_tokens=COUNTED)

    async def generate_content(self, **kwargs):
        text = kwargs["contents"][0].parts[0].text
        asset = re.search(r"asset_name='([^']+)'", text).group(1)
        self.generated.append(asset)
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            # With ``reverse`` the later pieces answer first, so completion order is the opposite of piece order.
            await asyncio.sleep(self.delay * (1 - int(asset[-2:]) / 20) if self.reverse else self.delay)
            what = self.script.get(asset, "ok")
            if isinstance(what, list):  # one entry per successive call for this piece; the last repeats
                what = what[min(self.generated.count(asset) - 1, len(what) - 1)]
            if isinstance(what, BaseException):
                raise what
            finish, answer = "STOP", BOX % asset
            if what == "truncated":
                finish = "MAX_TOKENS"
            elif what == "garbled":
                answer = "not json"
            prompt, out = self.usage
            usage = SimpleNamespace(prompt_token_count=prompt, candidates_token_count=out, thoughts_token_count=0, total_token_count=prompt + out)
            return SimpleNamespace(text=answer, usage_metadata=usage, prompt_feedback=None,
                                   candidates=[SimpleNamespace(finish_reason=types.FinishReason(finish))])
        finally:
            self.running -= 1


@pytest.fixture
def rates(monkeypatch):
    monkeypatch.setenv("AGY3D_TEXT_RATES_JSON", json.dumps({tier("worker").model: list(RATES)}))


def worst_case(role: str = "worker") -> float:
    tokens_in = COUNTED + schema_input_token_bound(ir_schema.scene_response_schema("X"))
    return (tokens_in * RATES[0] + AUTHOR_OUTPUT_BUDGETS[role].max_output_tokens * RATES[1]) / 1_000_000


def kit(tmp_path: Path, endpoint: KitEndpoint, *, per_asset_ceiling: float, author: object | None = None) -> EnvironmentKitDAG:
    governor = Governor(replace(plan_for(2), ceiling_usd=per_asset_ceiling))
    forge = ImageForge(FakeImageBackend(), governor=governor)
    author = author or AgentAuthor(client=SimpleNamespace(aio=SimpleNamespace(models=endpoint)), timeout_s=5)
    return EnvironmentKitDAG(forge=forge, author=author, workdir=tmp_path / "kit", budget=UE, sheet_resolution=256)


def piece_ms(rep) -> float:
    return rep.nodes["pieces"].duration_ms


def test_kit_ceiling_is_one_asset_allowance_per_piece_plus_the_trim_sheet() -> None:
    plan = plan_for(2)
    assert kit_plan(plan, 8).ceiling_usd == pytest.approx(9 * plan.ceiling_usd)
    assert kit_plan(plan, 1).ceiling_usd == pytest.approx(2 * plan.ceiling_usd)
    assert kit_plan(plan, 3) == replace(plan, ceiling_usd=4 * plan.ceiling_usd)  # nothing but the ceiling changes
    with pytest.raises(ValueError):
        kit_plan(plan, 0)


async def test_live_pieces_are_authored_concurrently_through_the_metered_path(tmp_path: Path, rates) -> None:
    delay = 0.4
    ep = KitEndpoint(delay=delay, reverse=True)
    dag = kit(tmp_path, ep, per_asset_ceiling=1.0)
    rep = await dag.run("ruined stone corridor", 8)
    x = rep.extras
    assert rep.ok and x["gates_ok"], rep.summary()
    assert ep.peak == 8 and ep.counted == 8  # all in flight at once, each counted (admission) before it was sent
    assert piece_ms(rep) < 3 * delay * 1000, piece_ms(rep)  # one delay plus overhead, not eight
    # piece k is piece k whatever finished first (the endpoint answered the last piece first)
    names = [f"{PIECE_CYCLE[k % 4].title().replace('_', '')}{k:02d}" for k in range(8)]
    assert list(x["pieces"]) == names and [o["piece"] for o in x["authoring"]["pieces"]] == names
    assert [o["status"] for o in x["authoring"]["pieces"]] == ["produced"] * 8
    assert [p["name"] for p in rep.nodes["pieces"].data["pieces"]] == names
    # one ledger: authoring and the trim sheet are in the same receipts, authoring is admitted and booked
    rec = x["authoring"]["receipts"]
    assert len(rec) == 8 and all(r["category"] == "author.propose" and r["status"] == "ok" and r["booked_usd"] < r["reserved_usd"] for r in rec)
    assert [r["category"] for r in x["genmedia_receipts"]] == ["image.trim"]
    assert x["spend"]["ceiling_usd"] == pytest.approx(9.0) and x["spend"]["per_asset_ceiling_usd"] == 1.0
    assert x["spend"]["spent_usd"] == pytest.approx(8 * (100 * RATES[0] + 50 * RATES[1]) / 1e6)
    assert (tmp_path / "kit" / "report.json").exists() and "authoring" in json.loads((tmp_path / "kit" / "report.json").read_text())


async def test_ceiling_that_admits_two_at_a_time_waits_and_loses_no_piece(tmp_path: Path, rates) -> None:
    delay, n = 0.3, 6
    limit = worst_case()
    ep = KitEndpoint(delay=delay)
    # kit ceiling = per-asset x (n + 1) = 2.5 worst cases: two reservations fit at once, not three
    dag = kit(tmp_path, ep, per_asset_ceiling=2.5 * limit / (n + 1))
    rep = await dag.run("ruined stone corridor", n)
    x = rep.extras
    assert rep.ok, rep.summary()
    assert ep.peak == 2 and len(ep.generated) == n
    assert [o["status"] for o in x["authoring"]["pieces"]] == ["produced"] * n
    assert [o["piece"] for o in x["authoring"]["pieces"]] == list(x["pieces"])
    waited = [o for o in x["authoring"]["pieces"] if o["admission_wait_s"] > delay / 2]  # waited for an earlier call to settle
    assert len(waited) == n - 2 and all(o["reserved_usd"] == pytest.approx(limit, rel=0.01) for o in x["authoring"]["pieces"])
    assert 2.5 * delay * 1000 < piece_ms(rep) < 6 * delay * 1000  # three rounds of two, not one round and not six


async def test_a_kit_is_not_refused_for_being_budgeted_as_one_asset(tmp_path: Path, rates) -> None:
    """Each call books nearly its whole reservation. Under one asset's ceiling the second piece could never fit."""
    limit = worst_case()
    ep = KitEndpoint(usage=(100, AUTHOR_OUTPUT_BUDGETS["worker"].max_output_tokens))
    per_asset = 1.4 * limit
    rep = await kit(tmp_path, ep, per_asset_ceiling=per_asset).run("ruined stone corridor", 4)
    x = rep.extras
    assert rep.ok, rep.summary()
    assert [o["status"] for o in x["authoring"]["pieces"]] == ["produced"] * 4
    assert x["spend"]["spent_usd"] > 2 * per_asset  # the kit spent more than two assets would be allowed, inside its own ceiling


async def test_failed_pieces_fail_the_step_with_every_outcome_listed(tmp_path: Path, rates) -> None:
    script = {"Pillar02": "truncated", "FloorTile05": TimeoutError(), "Doorway07": "garbled", "WallSegment04": RuntimeError("boom")}
    ep = KitEndpoint(script=script)
    rep = await kit(tmp_path, ep, per_asset_ceiling=1.0).run("ruined stone corridor", 8)
    assert not rep.ok and rep.nodes["pieces"].status == "failed"
    assert "IndexError" not in rep.nodes["pieces"].error
    outcomes = rep.extras["authoring"]["pieces"]
    assert [(o["index"], o["status"]) for o in outcomes] == [(0, "produced"), (1, "produced"), (2, "truncated"), (3, "produced"), (4, "error"),
                                                              (5, "timed_out"), (6, "produced"), (7, "invalid")]
    err = rep.nodes["pieces"].error
    assert err.startswith("PiecesFailed: 4 of 8 pieces were not produced")
    for fragment in ("#2 Pillar02 truncated", "#4 WallSegment04 error", "RuntimeError: boom", "#5 FloorTile05 timed_out", "#7 Doorway07 invalid", "#0 WallSegment00 produced"):
        assert fragment in err
    assert rep.nodes["pieces"].data["authoring"] == outcomes
    # later steps need every piece, so they did not run; the billed calls are all in the receipts
    assert all(rep.nodes[n].blocked for n in ("map_trim", "export_pieces", "assemble", "report"))
    assert len(rep.extras["authoring"]["receipts"]) == 8 + 3 and not rep.extras["gates_ok"]  # the error, timeout and invalid pieces were asked twice (the scripted failures repeat); the cut-off one was not
    assert [o["finish_reason"] for o in outcomes if o["status"] == "truncated"] == ["MAX_TOKENS"]


async def test_an_author_that_returns_nothing_is_a_recorded_outcome_not_an_index_error(tmp_path: Path) -> None:
    class Empty:
        async def propose(self, prompt, *, budget, n, context):
            return []

    rep = await kit(tmp_path, KitEndpoint(), per_asset_ceiling=1.0, author=Empty()).run("kit", 2)
    assert rep.nodes["pieces"].status == "failed" and "IndexError" not in rep.nodes["pieces"].error
    assert [o["status"] for o in rep.extras["authoring"]["pieces"]] == ["no_answer", "no_answer"]


async def test_missing_text_prices_refuse_the_kit_before_anything_is_requested(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("AGY3D_TEXT_RATES_JSON", raising=False)
    monkeypatch.setattr("antigravity3d.decisions.fallback.PRICE_TABLE_USD_PER_M", {})
    ep = KitEndpoint()
    dag = kit(tmp_path, ep, per_asset_ceiling=1.0)
    with pytest.raises(BudgetExceeded) as e:
        await dag.run("ruined stone corridor", 3)
    msg = str(e.value)
    assert "AGY3D_TEXT_RATES_JSON" in msg and tier("worker").model in msg and "USD per million input tokens" in msg
    assert not ep.generated and not ep.counted and not (tmp_path / "kit" / "trim_sheet.png").exists()  # no image, no piece
    # the command turns it into a plain error
    args = cli.build_parser().parse_args(["kit", "x", "--pieces", "2", "--out", str(tmp_path / "cli")])
    forge, author = ImageForge(FakeImageBackend()), AgentAuthor(client=SimpleNamespace(aio=SimpleNamespace(models=ep)))

    async def collaborators(offline, **kw):
        return None, forge, author, []

    monkeypatch.setattr(cli, "make_collaborators", collaborators)
    with pytest.raises(cli.CliError, match="AGY3D_TEXT_RATES_JSON"):
        await cli.cmd_kit(args)


class Staggered:
    """Wraps the template author so that the last pieces finish first."""

    def __init__(self) -> None:
        self.inner = TemplateAuthor()

    async def propose(self, prompt, *, budget, n, context):
        await asyncio.sleep(0.05 * (4 - context["seed"]) if context["seed"] < 4 else 0)
        return await self.inner.propose(prompt, budget=budget, n=n, context=context)


async def test_output_does_not_depend_on_completion_order(tmp_path: Path) -> None:
    def forge():
        return ImageForge(FakeImageBackend())

    a = await EnvironmentKitDAG(forge=forge(), author=TemplateAuthor(), workdir=tmp_path / "a", budget=UE, sheet_resolution=256).run("stone temple", 4)
    b = await EnvironmentKitDAG(forge=forge(), author=Staggered(), workdir=tmp_path / "b", budget=UE, sheet_resolution=256).run("stone temple", 4)
    assert a.ok and b.ok and list(a.extras["pieces"]) == list(b.extras["pieces"])
    for rel in ("kit.usda", "trim_sheet.png", "trim_sheet.json", *(f"pieces/{Path(p).name}" for p in a.extras["pieces"].values()),
                *(f"src/{n}.usda" for n in a.extras["pieces"])):
        assert (tmp_path / "a" / rel).read_bytes() == (tmp_path / "b" / rel).read_bytes(), rel


# --- one bounded recovery round -----------------------------------------------------------------------------------


async def test_a_transient_error_is_retried_once_and_the_kit_is_complete(tmp_path: Path, rates) -> None:
    ep = KitEndpoint(script={"Pillar02": [ConnectionError("502 bad gateway"), "ok"]})
    rep = await kit(tmp_path, ep, per_asset_ceiling=1.0).run("ruined stone corridor", 4)
    x = rep.extras
    assert rep.ok and x["gates_ok"], rep.summary()
    assert len(ep.generated) == 5 and ep.generated.count("Pillar02") == 2  # N + 1 calls
    pieces = x["authoring"]["pieces"]
    assert [len(o["attempts"]) for o in pieces] == [1, 1, 2, 1] and [o["status"] for o in pieces] == ["produced"] * 4
    first, second = pieces[2]["attempts"]
    assert first["status"] == "error" and "ConnectionError" in first["detail"] and second["status"] == "produced" and second["usage"]
    assert len(x["authoring"]["receipts"]) == 5 and list(x["pieces"]) == [o["piece"] for o in pieces]


async def test_a_retried_kit_is_the_same_kit(tmp_path: Path, rates) -> None:
    clean = await kit(tmp_path / "a", KitEndpoint(), per_asset_ceiling=1.0).run("ruined stone corridor", 4)
    retried = await kit(tmp_path / "b", KitEndpoint(script={"WallSegment00": [TimeoutError(), "ok"]}), per_asset_ceiling=1.0).run("ruined stone corridor", 4)
    assert clean.ok and retried.ok
    for rel in ("kit.usda", "pieces/WallSegment00.usda", "src/WallSegment00.usda", "pieces/Doorway03.usda"):
        assert (tmp_path / "a" / "kit" / rel).read_bytes() == (tmp_path / "b" / "kit" / rel).read_bytes(), rel


async def test_a_cut_off_piece_is_not_requested_again(tmp_path: Path, rates) -> None:
    ep = KitEndpoint(script={"FloorTile01": "truncated"})
    rep = await kit(tmp_path, ep, per_asset_ceiling=1.0).run("ruined stone corridor", 3)
    assert not rep.ok and ep.generated.count("FloorTile01") == 1 and len(ep.generated) == 3
    pieces = rep.extras["authoring"]["pieces"]
    assert [o["status"] for o in pieces] == ["produced", "truncated", "produced"] and [len(o["attempts"]) for o in pieces] == [1, 1, 1]
    assert "#1 FloorTile01 truncated" in rep.nodes["pieces"].error and "#0 WallSegment00 produced" in rep.nodes["pieces"].error


async def test_a_piece_that_fails_twice_fails_the_step_with_both_attempts(tmp_path: Path, rates) -> None:
    ep = KitEndpoint(script={"Pillar02": [RuntimeError("first"), "garbled"]})
    rep = await kit(tmp_path, ep, per_asset_ceiling=1.0).run("ruined stone corridor", 3)
    assert not rep.ok and ep.generated.count("Pillar02") == 2  # exactly one extra attempt
    o = rep.extras["authoring"]["pieces"][2]
    assert o["status"] == "invalid" and [a["status"] for a in o["attempts"]] == ["error", "invalid"]
    assert "RuntimeError: first" in o["attempts"][0]["detail"] and "#2 Pillar02 invalid" in rep.nodes["pieces"].error
    assert len(rep.extras["authoring"]["receipts"]) == 4


async def test_the_second_attempt_is_refused_by_a_ceiling_that_cannot_fit_it(tmp_path: Path, rates) -> None:
    limit = worst_case()
    # kit ceiling 1.4 worst cases: the failed first call retains its whole reservation (no usage known), so the retry cannot fit
    ep = KitEndpoint(script={"WallSegment00": TimeoutError()})
    rep = await kit(tmp_path, ep, per_asset_ceiling=1.4 * limit / 2).run("ruined stone corridor", 1)
    o = rep.extras["authoring"]["pieces"][0]
    assert not rep.ok and [a["status"] for a in o["attempts"]] == ["timed_out", "not_admitted"] and o["status"] == "not_admitted"
    assert "never fit" in o["detail"] and len(ep.generated) == 1


async def test_an_author_without_outcomes_gets_the_same_single_extra_attempt(tmp_path: Path) -> None:
    class Flaky:
        def __init__(self, fail: int) -> None:
            self.fail, self.calls = fail, 0

        async def propose(self, prompt, *, budget, n, context):
            self.calls += 1
            if context["seed"] == 1 and self.calls <= self.fail * 2:
                raise ConnectionError("dropped")
            return await TemplateAuthor().propose(prompt, budget=budget, n=n, context=context)

    once = Flaky(1)
    rep = await kit(tmp_path / "a", KitEndpoint(), per_asset_ceiling=1.0, author=once).run("kit", 2)
    assert rep.ok and [len(o["attempts"]) for o in rep.extras["authoring"]["pieces"]] == [1, 2]
    rep = await kit(tmp_path / "b", KitEndpoint(), per_asset_ceiling=1.0, author=Flaky(10)).run("kit", 2)
    assert not rep.ok and [a["status"] for a in rep.extras["authoring"]["pieces"][1]["attempts"]] == ["error", "error"]
