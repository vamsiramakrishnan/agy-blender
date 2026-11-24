"""Invariants at revision, async, publication and native context boundaries."""

import asyncio
import json
from dataclasses import replace

import pytest

from antigravity3d.harness import tools as T
from antigravity3d.kernel.publish import publish_directory
from antigravity3d.runtime.qa_state import read_qa_state
from test_harness_tools import CRATE, box, composed, make_session, masked_fixture, record_approval, simulate_engine_import
from test_plugin_bundle import pre


async def test_only_qualified_verdict_can_publish(tmp_path, monkeypatch):
    monkeypatch.setenv("AGY3D_STATE_DIR", str(tmp_path / "state"))
    env = make_session(tmp_path)
    await T.apply_scene_ir(json.dumps(CRATE), env.ctx)
    record_approval(env)
    good = env.state.verdict
    for changes in [{"action": "escalate_human"}, {"action": "agent_repair"}, {"action": "auto_fix"},
                    {"backend": "fake"}, {"backend": "generative_fallback"}, {"renderer": "clay"},
                    {"target": "godot_4"}, {"revision": "older"}, {"stage": "blockout"}]:
        env.sess().set_tier15(4.0, replace(good, **changes))
        assert not env.state.export_allowed
        assert not read_qa_state(env.ctx.conversation_id).export_allowed
        assert pre(tmp_path / "state", "export_engine_package", conv=env.ctx.conversation_id)["decision"] == "deny"
    assert read_qa_state("another-session") is None


@pytest.mark.parametrize("failure", [RuntimeError("QA timed out"), asyncio.CancelledError()])
async def test_edit_restored_on_exception_and_cancellation(tmp_path, monkeypatch, failure):
    env = make_session(tmp_path)
    await T.apply_scene_ir(json.dumps(CRATE), env.ctx)
    record_approval(env)
    revision, before = env.state.revision, composed(env)
    async def fail(_session):
        raise failure
    monkeypatch.setattr(T, "_tier1", fail)
    task = T.apply_scene_ir(json.dumps({"ops": [box("new", (1, 1, 1), 0.5)]}), env.ctx)
    if isinstance(failure, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert (await task).startswith("ERROR")
    assert composed(env) == before and env.state.revision == revision and env.state.export_allowed
    assert env.state.tx_stack == env.sess().stage.tx_stack


async def test_stale_render_is_discarded(tmp_path, monkeypatch):
    env = make_session(tmp_path)
    await T.apply_scene_ir(json.dumps(CRATE), env.ctx)
    started, finish = asyncio.Event(), asyncio.Event()
    async def upload(_path):
        started.set()
        await finish.wait()
        return "test:uri"
    monkeypatch.setattr(env.sess().forge, "upload", upload)
    task = asyncio.create_task(T.render_preview(env.ctx))
    await asyncio.wait_for(started.wait(), 5)
    await T.apply_scene_ir(json.dumps({"ops": [box("new", (1, 1, 1), 0.5)]}), env.ctx)
    finish.set()
    # Since calls are kept (harness/keeper.py) the edit stops the render it makes stale, and the render's answer says so.
    answer = await task
    assert answer.startswith("STOPPED: render_preview was stopped because the design changed while it ran"), answer
    assert not env.sess().previews and not list(env.sess().artifacts_dir.glob("preview_*.png"))


async def test_refused_reexport_preserves_previous_directory(tmp_path, monkeypatch):
    env = make_session(tmp_path)
    await T.apply_scene_ir(json.dumps(CRATE), env.ctx)
    record_approval(env)
    prior = env.sess().export_dir / "pkg"
    prior.mkdir()
    (prior / "marker").write_text("old generation")
    simulate_engine_import(monkeypatch, ok=False)
    assert "REFUSED" in await T.export_engine_package(env.ctx, "pkg")
    assert list(prior.iterdir()) == [prior / "marker"] and env.sess().last_export is None
    assert not list(env.sess().export_dir.glob(".package-*"))
    simulate_engine_import(monkeypatch)
    assert "EXPORTED" in await T.export_engine_package(env.ctx, "pkg")
    assert (prior / "manifest.json").is_file() and not (prior / "marker").exists()


def test_atomic_exchange_preserves_old_generation_for_reader(tmp_path):
    dest = tmp_path / "pkg"
    dest.mkdir()
    (dest / "old").touch()
    src = tmp_path / "candidate"
    src.mkdir()
    (src / "new").touch()
    publish_directory(src, dest)
    assert (dest / "new").is_file() and (src / "old").is_file()


# any_target_can_be_verified: this test is about the QA receipt and the validator's effect on export, not the target; a session that must be verified for unreal_5 (the default) refuses
# run_visual_qa on this machine (Session.unverifiable_target), which tests/test_verifiable_targets.py covers on its own.
async def test_full_qa_receipt_and_failed_validator(tmp_path, monkeypatch, any_target_can_be_verified):
    from antigravity3d.kernel.fast_ops import Issue

    env = make_session(tmp_path, stage="LOOKDEV")
    await T.apply_scene_ir(json.dumps(CRATE), env.ctx)
    # Transport fixtures simulate a calibrated response and a renderer-supplied image.
    s = env.sess()
    s.plane.backend_kind = "decisions"
    s.add_preview(masked_fixture(tmp_path / "test-render.png"), "https://fixture.test/render.png", kind="cycles")
    assert (await T.run_visual_qa(env.ctx)).startswith("QA[lookdev] approve")
    assert env.state.export_allowed and env.state.verdict.renderer == "cycles"
    monkeypatch.setattr(T, "validate_usd", lambda _p: [Issue("bad", "critical", None, "fixture")])
    assert "USD 1 crit" in await T.run_usd_validator(env.ctx)
    assert not env.state.export_allowed and env.state.validator_ok is False
    assert (await T.export_engine_package(env.ctx, "denied")).startswith("REFUSED")


async def test_approval_revocation_failure_rolls_back_edit(tmp_path, monkeypatch):
    from antigravity3d.runtime import qa_state

    env = make_session(tmp_path)
    await T.apply_scene_ir(json.dumps(CRATE), env.ctx)
    record_approval(env)
    before, revision = composed(env), env.state.revision
    def fail(**kwargs):
        raise OSError("volume read-only")
    monkeypatch.setattr(qa_state, "write_qa_state", fail)
    msg = await T.apply_scene_ir(json.dumps({"ops": [box("new", (1, 1, 1), 0.5)]}), env.ctx)
    assert "volume read-only" in msg
    assert composed(env) == before and env.state.revision == revision and env.state.export_allowed


async def test_export_rechecks_gate_after_waiting_for_session_lock(tmp_path):
    env = make_session(tmp_path)
    await T.apply_scene_ir(json.dumps(CRATE), env.ctx)
    record_approval(env)
    async with env.reg.locked(env.sid):
        queued = asyncio.create_task(T.export_engine_package(env.ctx, "pkg"))
        await asyncio.sleep(0)
        env.sess().visuals_changed()
    assert (await queued).startswith("REFUSED") and not (env.sess().export_dir / "pkg").exists()


# any_target_can_be_verified: this test is about a texture changed after QA, not the target; a session that must be verified for unreal_5 (the default) refuses
# run_visual_qa on this machine (Session.unverifiable_target), which tests/test_verifiable_targets.py covers on its own.
async def test_external_texture_change_requires_new_render(tmp_path, any_target_can_be_verified):
    env = make_session(tmp_path, stage="LOOKDEV")
    albedo = masked_fixture(tmp_path / "albedo.png")
    await T.apply_scene_ir(json.dumps(CRATE), env.ctx)
    await T.apply_shader_diff("wood", json.dumps({"albedo_uri": str(albedo), "assign_to": ["body"]}), env.ctx)
    s = env.sess()
    s.plane.backend_kind = "decisions"
    s.add_preview(masked_fixture(tmp_path / "frame.png"), "https://fixture.test/frame",
                  kind="eevee", texture_hash=s.texture_digest())
    assert "approve" in await T.run_visual_qa(env.ctx)
    assert env.state.export_allowed
    albedo.write_bytes(b"externally changed after QA")
    assert "material files changed" in await T.export_engine_package(env.ctx, "pkg")
    assert not env.state.export_allowed and not (s.export_dir / "pkg").exists()


async def test_one_hundred_interleaved_session_export_attempts(tmp_path, monkeypatch):
    """Two logical assets share a native hook workspace and publish through independent USD locks."""
    state_dir = tmp_path / "state"
    monkeypatch.setenv("AGY3D_STATE_DIR", str(state_dir))
    simulate_engine_import(monkeypatch)
    a = make_session(tmp_path, sid="gate-a", stage="EXPORT")
    b = make_session(tmp_path, sid="gate-b", stage="EXPORT")
    await asyncio.gather(*(T.apply_scene_ir(json.dumps(CRATE), e.ctx) for e in (a, b)))
    attempts = []
    for round_num in range(50):
        # Alternate the approved session. Queue both exports before resolving the revision under lock.
        allowed = a if round_num % 2 else b
        blocked = b if allowed is a else a
        record_approval(allowed)
        record_approval(blocked)
        async with blocked.reg.locked(blocked.sid):
            stale = asyncio.create_task(T.export_engine_package(blocked.ctx, "pkg"))
            good = asyncio.create_task(T.export_engine_package(allowed.ctx, "pkg"))
            await asyncio.sleep(0)
            blocked.sess().visuals_changed()
        ok, denied = await asyncio.gather(good, stale)
        assert ok.startswith("EXPORTED") and denied.startswith("REFUSED"), (round_num, ok, denied)
        assert read_qa_state(allowed.ctx.conversation_id).export_allowed
        assert not read_qa_state(blocked.ctx.conversation_id).export_allowed
        manifest = json.loads((allowed.sess().export_dir / "pkg" / "manifest.json").read_text())
        assert manifest["qa"]["revision"] == allowed.state.revision != blocked.state.revision
        attempts.extend([ok, denied])
    assert len(attempts) == 100 and sum(r.startswith("EXPORTED") for r in attempts) == 50
