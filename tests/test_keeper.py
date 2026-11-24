"""No tool call outlives the host's three minutes, and none loses its work (harness/keeper.py).

Run 1 of the website task (benchmarks/results/site_run1, 2026-11-24): Antigravity ended deliver_package on the 70-part
containment_gate at 3m0s twice, each time while the package for the checks was being written, and the work was thrown
away both times. Each test here makes one step deliberately slow and checks the four facts the round asks for: the call
answers at the bound, the next call returns the result, the slow step ran once, a client's cancel does not reach the
work, and an edit in between makes the kept work stale (stopped, its folder removed, never delivered)."""

from __future__ import annotations

import asyncio
import time

import anyio
import pytest

from antigravity3d.harness import keeper
from antigravity3d.harness import tools as T
from antigravity3d.harness.session import session
from antigravity3d.runtime import mcp_server as M
from test_native_views import _design, _fail_fast_session, _slow_review
from test_run_build import crate, write, ws  # noqa: F401 - ws is a fixture

pytest.importorskip("antigravity3d.delivery")


def _slow_check(monkeypatch, seconds: float) -> list[float]:
    """The Blender check of the package written for the checks takes ``seconds``; returns when each one started."""
    from antigravity3d.adapters.blender_5_3 import upstream_checks

    started: list[float] = []

    async def asset_qa(pool, glb, target_engine):
        started.append(time.monotonic())
        await asyncio.sleep(seconds)
        return "passed", "stand-in qa_check", {}

    monkeypatch.setattr(upstream_checks, "asset_qa", asset_qa)
    return started


async def test_a_slow_step_answers_at_the_bound_and_the_next_call_returns_the_result(tmp_path, monkeypatch):
    env, pool, _ = _fail_fast_session(tmp_path, monkeypatch)
    started = _slow_check(monkeypatch, 1.5)
    await T.apply_scene_ir(_design(), env.ctx)
    monkeypatch.setattr(keeper, "ANSWER_S", 0.6)
    t0 = time.monotonic()
    first = await T.deliver_package(env.ctx, "crate", dest="out/crate")
    assert time.monotonic() - t0 < 1.2  # at the bound, not when the step ends
    assert first.startswith("STILL WORKING (nothing needs fixing, nothing failed): deliver_package is at step 2 of 5 "
                            "(the package written for the checks"), first
    assert 'Call deliver_package(out_name="crate", dest="out/crate") again now' in first and "\n" not in first
    replies = [first]
    while replies[-1].startswith("STILL WORKING"):
        replies.append(await T.deliver_package(ctx=env.ctx, out_name="crate", dest="out/crate"))  # same call, other spelling
    assert replies[-1].startswith("DELIVERED: EXPORTED PREVIEW crate"), replies[-1]
    assert len(started) == 1 and len(pool.renders) == 1  # the slow check and the render each ran once
    assert (tmp_path / "out" / "crate" / "Crate.glb").is_file() or (env.sess().export_dir / "crate").is_dir()


async def test_an_edit_in_between_stops_the_kept_delivery_removes_its_folder_and_never_delivers_it(tmp_path, monkeypatch):
    env, pool, checks = _fail_fast_session(tmp_path, monkeypatch)
    asked = _slow_review(env, monkeypatch, 1.0)
    await T.apply_scene_ir(_design(), env.ctx)
    monkeypatch.setattr(keeper, "ANSWER_S", 0.3)
    first = await T.deliver_package(env.ctx, "crate")
    assert first.startswith("STILL WORKING"), first
    while not asked:
        await asyncio.sleep(0.05)
    assert list(env.sess().artifacts_dir.glob(".preflight-*"))  # the checked package it would deliver
    monkeypatch.setattr(keeper, "ANSWER_S", 30.0)
    edit = await T.apply_shader_diff("paint", {"base_color": [0.6, 0.2, 0.2]}, ctx=env.ctx)
    assert edit.startswith("OK") or edit.startswith("SHADER"), edit
    assert "STOPPED the deliver_package that was still running" in edit, edit
    assert not list(env.sess().artifacts_dir.glob(".preflight-*"))  # its folder is removed
    await asyncio.sleep(1.5)  # longer than the review would have taken
    assert not (env.sess().export_dir / "crate").exists()  # never delivered
    assert T._keeper(env.sess()).calls == {}
    again = await T.deliver_package(env.ctx, "crate")  # a delivery of the design as it is now, from the start
    assert again.startswith("DELIVERED"), again
    assert len(asked) == 2 and len(checks) == 2


async def test_another_call_waits_for_the_kept_one_and_a_read_answers_at_once(tmp_path, monkeypatch):
    env, pool, checks = _fail_fast_session(tmp_path, monkeypatch)
    asked = _slow_review(env, monkeypatch, 1.5)
    await T.apply_scene_ir(_design(), env.ctx)
    monkeypatch.setattr(keeper, "ANSWER_S", 0.3)
    assert (await T.deliver_package(env.ctx, "crate")).startswith("STILL WORKING")
    while not asked:
        await asyncio.sleep(0.05)
    t0 = time.monotonic()
    manifest = await T.inspect_manifest(env.ctx)
    assert not manifest.startswith("STILL WORKING") and time.monotonic() - t0 < 0.3
    busy = await T.render_preview(env.ctx)
    assert busy.startswith('STILL WORKING (nothing needs fixing, nothing failed): deliver_package(out_name="crate"), '
                           "called earlier, is still running (step 4 of 5"), busy
    assert busy.endswith("then call render_preview.") and len(env.sess().previews) == 1  # it did not render
    monkeypatch.setattr(keeper, "ANSWER_S", 30.0)
    preview = await T.render_preview(env.ctx)  # waits for the delivery to end, then renders
    assert preview.startswith("PREVIEW"), preview
    done = await T.deliver_package(env.ctx, "crate")  # the delivery's answer waited for its call
    assert done.startswith("DELIVERED"), done
    assert len(asked) == 1


async def test_a_client_cancel_through_the_server_does_not_reach_the_work_and_status_shows_the_step(tmp_path, monkeypatch):
    """As the MCP server runs a call: the request handler's cancel scope is cancelled when the client gives up."""
    env, pool, checks = _fail_fast_session(tmp_path, monkeypatch)
    started = _slow_check(monkeypatch, 1.0)
    host = M.StudioHost(script=False)
    host.registry = env.reg
    host.gates[env.sid] = __import__("antigravity3d.harness.hooks", fromlist=["x"]).InvariantAndSafetyGate(env.reg, env.sid)
    host.active = env.sid
    await host.call_tool("apply_scene_ir", T.apply_scene_ir, env.sid, {"ops": _design()})
    with anyio.move_on_after(0.4):  # the client's deadline
        await host.call_tool("deliver_package", T.deliver_package, env.sid, {"out_name": "crate"})
        pytest.fail("the call should have been cancelled")
    status = await host.session_status(env.sid)
    assert "RUNNING: deliver_package is at step 2 of 5 (the package written for the checks" in status, status
    assert 'call deliver_package(out_name="crate") again to get its answer' in status
    r = await host.call_tool("deliver_package", T.deliver_package, env.sid, {"out_name": "crate"})
    assert r.startswith("DELIVERED"), r
    assert len(started) == 1 and len(pool.renders) == 1
    assert "RUNNING" not in await host.session_status(env.sid)


async def test_run_build_answers_at_the_bound_and_the_same_call_joins_the_build(ws, monkeypatch):  # noqa: F811
    from antigravity3d.build import runner

    real, runs = runner.run_script, []

    def slow(*a, **k):
        runs.append(1)
        time.sleep(1.0)
        return real(*a, **k)

    monkeypatch.setattr(runner, "run_script", slow)
    monkeypatch.setattr(keeper, "ANSWER_S", 0.3)
    host = M.StudioHost(script=True)
    name = write(ws, crate())
    first = await host.run_build(name, str(ws))
    assert first.startswith("STILL WORKING (nothing needs fixing") and 'Call run_build(path="crate.py") again now' in first, first
    monkeypatch.setattr(keeper, "ANSWER_S", 30.0)
    second = await host.run_build(name, str(ws))
    assert second.startswith("BUILT crate.py -> session crate-"), second
    assert len(runs) == 1
    # a changed script is another build: it waits for nothing (none runs) and builds the new text
    write(ws, crate(lid_colour="#20d040"))
    third = await host.run_build(name, str(ws))
    assert third.startswith("BUILT crate.py"), third
    assert len(runs) == 2


async def test_run_build_stops_a_kept_delivery_of_the_design_it_replaces(ws, monkeypatch):  # noqa: F811
    host = M.StudioHost(script=True)
    name = write(ws, crate())
    assert (await host.run_build(name, str(ws))).startswith("BUILT")
    s = session(host.registry, host.active)
    gate = asyncio.Event()

    async def held():
        await gate.wait()
        return "never"

    kept = T._keeper(s)
    monkeypatch.setattr(keeper, "ANSWER_S", 0.1)
    assert (await kept.call("deliver_package", {"out_name": "crate"}, held)).startswith("STILL WORKING")
    write(ws, crate(lid_colour="#20d040"))
    monkeypatch.setattr(keeper, "ANSWER_S", 30.0)
    r = await host.run_build(name, str(ws))
    assert "STOPPED the deliver_package that was still running" in r, r
    assert kept.calls == {}


async def test_install_into_project_is_kept_too(tmp_path, monkeypatch):
    from antigravity3d import deliver

    runs = []

    def slow_install(package_dir, project, *a, **k):
        runs.append(package_dir)
        time.sleep(0.8)

        class R:
            status, message, checks, warnings = "installed", "stand-in", [], []

            def to_dict(self):
                return {"files": []}
        return R()

    monkeypatch.setattr(deliver, "install_package", slow_install)
    monkeypatch.setattr(keeper, "ANSWER_S", 0.2)
    server = M.build_server(M.StudioHost(script=False))
    fn = server._tool_manager.get_tool("install_into_project").fn
    args = {"package_dir": str(tmp_path / "pkg"), "project_dir": str(tmp_path), "dry_run": False}
    first = await fn(ctx=None, **args)
    assert first.startswith("STILL WORKING") and "install_into_project(package_dir=" in first, first
    monkeypatch.setattr(keeper, "ANSWER_S", 30.0)
    second = await fn(ctx=None, **args)
    assert second.startswith("INSTALLED stand-in"), second
    assert len(runs) == 1


async def test_the_keeper_itself():
    """The mechanism on its own: an answer at the bound, a join, a cancel that reaches only the waiting, an edit that
    stops the running call, and progress forwarded to whichever call waits."""
    k, runs = keeper.Keeper(), []

    async def work():
        runs.append(1)
        keeper.step("slow_tool", 1, 2, "the first half")
        await asyncio.sleep(0.3)
        keeper.step("slow_tool", 2, 2, "the second half")
        await asyncio.sleep(0.3)
        return "done"

    old = keeper.ANSWER_S
    keeper.ANSWER_S = 0.1
    try:
        r = await k.call("slow_tool", {"a": 1}, work)
        assert r.startswith("STILL WORKING") and "step 1 of 2 (the first half)" in r and 'slow_tool(a=1)' in r
        waiter = asyncio.ensure_future(k.call("slow_tool", {"a": 1}, work))
        await asyncio.sleep(0.05)
        waiter.cancel()  # the client gives up: the work goes on
        with pytest.raises(asyncio.CancelledError):
            await waiter
        keeper.ANSWER_S = 5.0
        assert await k.call("slow_tool", {"a": 1}, work) == "done" and len(runs) == 1
        assert k.calls == {}

        async def edit():
            return "edited"

        keeper.ANSWER_S = 0.1
        assert (await k.call("slow_tool", {"a": 2}, work)).startswith("STILL WORKING")
        keeper.ANSWER_S = 5.0
        out = await k.call("edit", {}, edit, edit=True, stopped_note=lambda names: f"stopped {names}")
        assert out == "edited\nstopped ['slow_tool']" and k.calls == {}
    finally:
        keeper.ANSWER_S = old


def test_the_texts_that_say_how_delivery_answers_name_the_new_answer():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    assert "\"STILL WORKING\"" in T.SHORT_PATH and "\"STILL WORKING\"" in M.SCRIPT_INSTRUCTIONS
    for page in ("src/antigravity3d/build/plugin_script_mode/SKILL.md", "src/antigravity3d/build/plugin_script_mode/AGENTS.md",
                 "plugin/antigravity-3d/rules/AGENTS.md", "plugin/antigravity-3d/skills/parametric-kitbash/SKILL.md"):
        text = (root / page).read_text()
        assert "STILL WORKING" in text, page
    assert T.EDIT_TOOLS.isdisjoint(T.READ_TOOLS) and (T.EDIT_TOOLS | T.READ_TOOLS) <= set(T.ALL_TOOLS)
