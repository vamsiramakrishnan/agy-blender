"""A running node none of whose consumers can run any more is stopped, when the pipeline opts in (dags/runner.py)."""

from __future__ import annotations

import asyncio

import pytest

from antigravity3d.dags.runner import Dag


def diamond(stop: bool, *, keep: bool = False, fail_after: float = 0.0):
    """a -> (fails) ; b (slow, speculative) ; c needs a and b ; d is a leaf on b alone when asked."""
    log: list[str] = []
    dag = Dag("t", stop_unusable=stop)

    async def fails(ctx):
        await asyncio.sleep(fail_after)
        raise RuntimeError("no usable candidate")

    async def slow(ctx):
        try:
            for i in range(50):
                await asyncio.sleep(0.01)
                log.append(f"b{i}")
            return "b"
        except asyncio.CancelledError:
            log.append("b cancelled")
            raise

    async def join(ctx):
        log.append("c ran")
        return ctx["a"], ctx["b"]

    dag.node("a", fails)
    dag.node("b", slow, keep=keep)
    dag.node("c", join, ("a", "b"))
    return dag, log


async def test_a_speculative_node_is_stopped_once_its_only_consumer_cannot_run():
    dag, log = diamond(True)
    rep = await dag.run()
    b = rep.nodes["b"]
    assert b.status == "stopped" and b.reason == "stopped: no consumer can run (c: upstream a failed)"
    assert "b cancelled" in log and len([x for x in log if x.startswith("b") and x[1:].isdigit()]) < 10
    assert rep.nodes["c"].status == "skipped" and rep.nodes["c"].blocked and "c ran" not in log
    assert not rep.ok  # because a failed; a stop is not a failure of its own
    assert "stopped=1" in rep.summary() and rep.to_dict()["nodes"][1]["status"] == "stopped"


async def test_without_opting_in_nothing_changes():
    dag, log = diamond(False)
    rep = await dag.run()
    assert rep.nodes["b"].status == "ok" and len([x for x in log if x[1:].isdigit()]) == 50 and "b cancelled" not in log
    assert rep.nodes["c"].status == "skipped" and "stopped" not in rep.summary()


async def test_a_kept_node_and_a_leaf_always_finish():
    dag, log = diamond(True, keep=True)
    rep = await dag.run()
    assert rep.nodes["b"].status == "ok" and "b cancelled" not in log
    dag = Dag("leaf", stop_unusable=True)

    async def fails(ctx):
        raise RuntimeError("x")

    async def slow(ctx):
        await asyncio.sleep(0.2)
        return 1

    dag.node("a", fails)
    dag.node("b", slow)  # no consumer: an output of the run
    rep = await dag.run()
    assert rep.nodes["b"].status == "ok"


async def test_a_node_with_another_live_consumer_keeps_running():
    dag = Dag("t", stop_unusable=True)

    async def fails(ctx):
        raise RuntimeError("x")

    async def slow(ctx):
        await asyncio.sleep(0.2)
        return 1

    async def use(ctx):
        return ctx["b"]

    dag.node("a", fails)
    dag.node("b", slow)
    dag.node("c", use, ("a", "b"))
    dag.node("d", use, ("b",))  # still runnable
    rep = await dag.run()
    assert rep.nodes["b"].status == "ok" and rep.nodes["d"].status == "ok" and rep.nodes["c"].blocked


async def test_stopping_propagates_and_a_skip_does_not_stop_anything():
    dag = Dag("t", stop_unusable=True)

    async def skip(ctx):
        ctx.skip("nothing to do")

    async def fails(ctx):
        await asyncio.sleep(0.05)
        raise RuntimeError("x")

    async def slow(ctx):
        await asyncio.sleep(1.0)
        return 1

    async def use(ctx):
        return 1

    dag.node("s", skip)
    dag.node("f", fails)
    dag.node("b", slow, ("s",))  # its dependency was skipped, not failed: it runs
    dag.node("m", use, ("f", "b"))  # can never run once f fails
    dag.node("n", use, ("m",))
    rep = await dag.run()
    assert rep.nodes["b"].status == "stopped" and "m: upstream f failed" in rep.nodes["b"].reason
    assert rep.nodes["m"].blocked and rep.nodes["n"].blocked and rep.nodes["b"].duration_ms < 900


async def test_cancelling_the_whole_run_still_cancels():
    dag = Dag("t", stop_unusable=True)

    async def slow(ctx):
        await asyncio.sleep(10)

    dag.node("a", slow)
    task = asyncio.ensure_future(dag.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------------------------- the pipelines

import json  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from no_decisions_key import NoDecisionsKey  # noqa: E402


@pytest.fixture
def key(monkeypatch):
    started = []

    def start(**kw):
        api = NoDecisionsKey(**kw).__enter__()
        monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", api.url)
        monkeypatch.setenv("GEMINI_API_KEY", "dummy-not-a-key")
        monkeypatch.delenv("AGY3D_THREE_JS", raising=False)
        started.append(api)
        return api

    yield start
    for api in started:
        api.__exit__(None, None, None)


def prop_report(tmp_path, *extra):
    from antigravity3d import cli

    rc = cli.main(["prop", "wooden crate with iron brackets", "--engine", "godot_4", "--max-tris", "3000", "--preview",
                   "--token-ceiling", "285000", "--max-images", "11", "--art-direction", "retro-lowres",
                   "--out", str(tmp_path / "out"), *extra])
    data = json.loads((tmp_path / "out" / "report.json").read_text())
    return rc, data, data.get("extras", data)


def test_a_geometry_failure_sends_no_material_call(key, tmp_path, capsys):
    """The live failure of 2026-11-21: geometry's grading refused, and the material branch went on to generate images
    and grade them for a package that could no longer be built. Before: 4 albedo images and 2 grading calls for nothing
    (the images were sent the moment geometry started). Now the materials wait for geometry: none is sent."""
    api = key(refuse_questions=("score_silhouette_vs_blueprint",))
    rc, data, x = prop_report(tmp_path)
    capsys.readouterr()
    nodes = {n["name"]: n for n in data["nodes"]}
    assert rc == 1 and nodes["geometry"]["status"] == "failed"
    assert nodes["pbr"]["status"] == "skipped" and nodes["pbr"]["blocked"] and "upstream geometry" in nodes["pbr"]["reason"]
    failed_at = next(r["t"] for r in api.requests if r["kind"] == "judge:refused")
    assert [r for r in api.requests if r["t"] > failed_at and r["kind"] in ("image:sent", "judge")] == []
    assert not [r for r in x["cost"]["receipts"] if r["category"] in ("image.albedo", "pbr_vision")]
    assert x["cost"]["images"] == sum(r["kind"] == "image:sent" for r in api.requests)  # only the blueprint(s)


def test_a_pass_a_failure_stops_the_material_node_and_the_ledger_keeps_what_was_sent(key, tmp_path, capsys):
    api = key(refuse_questions=("score_silhouette",), image_delay_s=3.0)  # pass A's QA refused while images are in flight
    rc, data, x = prop_report(tmp_path, "--single-candidate")
    capsys.readouterr()
    nodes = {n["name"]: n for n in data["nodes"]}
    assert nodes["pyramid"]["status"] == "failed" and nodes["pbr"]["status"] == "stopped"
    assert nodes["pbr"]["reason"] == "stopped: no consumer can run (pyramid: pyramid failed)"
    assert nodes["pbr"]["end_ms"] >= nodes["pyramid"]["end_ms"] and nodes["package"]["blocked"]
    albedo = [r for r in x["cost"]["receipts"] if r["category"] == "image.albedo"]
    sent = sum(r["kind"] == "image:sent" for r in api.requests)
    assert albedo and len(albedo) + sum(r["category"] == "image.ortho" for r in x["cost"]["receipts"]) == sent
    for r in albedo:  # sent, outcome unknown: the whole reservation and the image stay counted, nothing refunded
        assert r["status"] == "CancelledError" and r["source"] == "reservation_estimate"
        assert r["booked_tokens"] == r["reserved_tokens"] and r["images"] == 1
    assert x["cost"]["images"] == sent
    assert any(s["step"] == "dag:pbr" and s["reason"].startswith("stopped:") for s in x["skipped"])


async def test_materials_are_synthesised_for_the_winning_scene_only(tmp_path):
    from antigravity3d.cli import offline_plane
    from antigravity3d.dags.prop_to_engine import PropToEngineDAG
    from antigravity3d.genmedia.nano_banana import FakeImageBackend, ImageForge
    from antigravity3d.state import EngineBudget
    from antigravity3d.tactics.authoring import CallableAuthor, TemplateAuthor

    base = TemplateAuthor()

    async def propose(prompt, *, budget, n, context):  # every design names its materials differently
        scenes = await base.propose(prompt, budget=budget, n=n, context=context)
        out = []
        for i, s in enumerate(scenes):
            rename = {m.id: f"{m.id}_d{i}" for m in s.materials}
            out.append(s.model_copy(update={
                "materials": [m.model_copy(update={"id": rename[m.id]}) for m in s.materials],
                "nodes": [nd.model_copy(update={"material_id": rename.get(nd.material_id, nd.material_id)}) for nd in s.nodes]}))
        return out

    backend = FakeImageBackend()
    rep = await PropToEngineDAG(plane=offline_plane(), forge=ImageForge(backend), author=CallableAuthor(propose, base.repair),
                                workdir=tmp_path, budget=EngineBudget("unreal_5")).run("wooden crate with iron brackets")
    proposed = rep.nodes["propose"].data["materials"]
    textured = sorted(rep.nodes["pbr"].data["materials"])
    winner = rep.nodes["geometry"].data["notes"]["variant"]
    assert len(proposed) > len(textured) and all(t.endswith(f"_d{int(winner[-2:])}") for t in textured)
    albedo_calls = [c for c in backend.calls if c["prompt"].startswith("Seamless tileable albedo")]
    assert len(albedo_calls) == sum(rep.nodes["pbr"].data["materials"][t]["n_planned"] for t in textured) > 0


async def test_kit_stops_the_pieces_when_the_trim_sheet_fails(tmp_path):
    from antigravity3d.dags.environment_kit import EnvironmentKitDAG
    from antigravity3d.genmedia.nano_banana import FakeImageBackend, ImageForge
    from antigravity3d.state import EngineBudget
    from antigravity3d.tactics.authoring import CallableAuthor, TemplateAuthor

    class NoSheet(FakeImageBackend):
        async def generate(self, **kw):
            await asyncio.sleep(0.3)  # the pieces' authoring calls are in flight by then
            raise RuntimeError("image model returned no inline image data")

    base, started, cancelled = TemplateAuthor(), [], []

    async def slow(prompt, *, budget, n, context):
        started.append(context["asset_name"])
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.append(context["asset_name"])
            raise
        return await base.propose(prompt, budget=budget, n=n, context=context)

    rep = await EnvironmentKitDAG(forge=ImageForge(NoSheet()), author=CallableAuthor(slow), workdir=tmp_path,
                                  budget=EngineBudget("unreal_5"), sheet_resolution=256).run("ruined temple", 2)
    assert rep.nodes["trim_sheet"].status == "failed" and rep.nodes["pieces"].status == "stopped"
    assert rep.nodes["pieces"].reason.startswith("stopped: no consumer can run (") and "trim_sheet failed" in rep.nodes["pieces"].reason
    assert started and sorted(cancelled) == sorted(started) and rep.nodes["pieces"].duration_ms < 4000
    assert any(s["step"] == "dag:pieces" for s in rep.extras["skipped"])


async def test_batch_sanitize_and_variant_factory_do_not_opt_in(tmp_path):
    import inspect as _inspect

    from antigravity3d.dags import batch_sanitize, variant_factory

    for mod in (batch_sanitize, variant_factory):
        assert "stop_unusable=True" not in _inspect.getsource(mod)
