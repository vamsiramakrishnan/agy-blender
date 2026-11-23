"""Which targets a run that must be verified can succeed for, and the refusal at the start for the others.

A verified run needs the engine_capture and engine_parity steps to pass (``PyramidResult.verified``); a target whose
validator renders no frame (Unreal, Unity, Roblox) can never pass them, so the run used to make and pay for its authoring,
judge and image calls and end in "publication requires ... captured engine parity". ``verification_gap`` is the one question
"can this target be verified on this machine", asked before any model call on the command path (``cli.preflight_prop``) and on
the session path (``Session.waits_for_user``, which ``deliver_package`` and the stop hook read). No test here calls a model."""

from __future__ import annotations

import sys
from argparse import Namespace
from types import SimpleNamespace

import pytest
from test_run_build import build, crate, write, ws  # noqa: F401 - ws is a fixture

from antigravity3d import cli
from antigravity3d.adapters.engines import validators as V
from antigravity3d.adapters.engines import web_browser
from antigravity3d.adapters.engines.web_three import WebThreeValidator
from antigravity3d.harness import tools as T
from antigravity3d.runtime import mcp_server as M
from antigravity3d.state import TargetEngine

from typing import get_args

UNVERIFIABLE = ["unreal_5", "unity_urp", "roblox_r15"]
VERIFIABLE = ["godot_4", "web_three"]


@pytest.fixture
def engines_present(monkeypatch):
    """Godot and a browser are installed (stand-ins that exist and execute)."""
    monkeypatch.setenv("GODOT_BIN", sys.executable)
    monkeypatch.setattr(WebThreeValidator, "available", lambda self: True)
    # "A browser is installed" also makes a whole offline run's ``web_three.prestart()`` take the shared browser's lease, which
    # imports Playwright and starts a thread. These tests are about the target refusal, so no browser is leased (the pool's lease
    # is stood in for), with or without the package installed.
    monkeypatch.setattr(web_browser.SHARED, "lease", lambda *_a, **_k: web_browser.Lease(None))


@pytest.fixture
def engines_absent(monkeypatch):
    monkeypatch.delenv("GODOT_BIN", raising=False)
    monkeypatch.setattr(V.shutil, "which", lambda *_a, **_k: None)
    monkeypatch.setattr(WebThreeValidator, "available", lambda self: False)


def test_the_question_has_one_answer_per_target(engines_present):
    assert V.verifiable_targets() == VERIFIABLE
    assert [t for t in get_args(TargetEngine) if V.verification_gap(t)] == UNVERIFIABLE
    for t in UNVERIFIABLE:
        assert "no engine capture" in V.verification_gap(t)
    # the answer follows the validator's capture, not a list of names: only these two classes render a frame
    assert [t for t in get_args(TargetEngine) if V.validator_for(t).captures] == VERIFIABLE


def test_a_verifiable_target_whose_engine_is_missing_says_which_variable_to_set(engines_absent):
    assert V.verifiable_targets() == []
    assert "set $GODOT_BIN" in V.verification_gap("godot_4") and "not installed" in V.verification_gap("godot_4")
    assert "no engine capture" in V.verification_gap("unreal_5")


def args(engine: str, **kw) -> Namespace:
    base = dict(engine=engine, engine_given=True, offline=False, prompt="wooden crate", out="OUT")
    return Namespace(**{**base, **kw})


def refusal(a: Namespace) -> str:
    with pytest.raises(cli.CliError) as e:
        cli._refuse_unverifiable_target(a, '"wooden crate"')
    return str(e.value)


@pytest.mark.parametrize("engine", UNVERIFIABLE)
def test_the_refusal_names_the_target_what_would_work_and_the_targets_that_can_be_verified(engine, engines_present):
    text = refusal(args(engine))
    assert f"a verified run for {engine} cannot succeed on this machine" in text and "nothing was generated" in text
    assert f"agy3d prop \"wooden crate\" --engine {engine} --preview" in text  # what would work
    assert "--engine godot_4, --engine web_three" in text  # and the targets that can be verified here


def test_the_refusal_for_a_default_engine_says_it_is_the_default(engines_present):
    assert "a verified run for unreal_5 (the default engine, as no --engine was given) cannot succeed" in refusal(args("unreal_5", engine_given=False))


def test_the_refusal_without_any_verifiable_target_says_what_to_install(engines_absent):
    text = refusal(args("roblox_r15"))
    assert "no target can be verified on this machine" in text and "$GODOT_BIN" in text


@pytest.mark.parametrize("engine", VERIFIABLE)
def test_a_verifiable_target_is_admitted(engine, engines_present):
    cli._refuse_unverifiable_target(args(engine), '"x"')  # no refusal


class StubClient:
    """The SDK client of a key, counting every model-facing call (the Decisions probe included): see what a run asks before it refuses."""

    def __init__(self):
        self.calls: list[str] = []
        outer = self

        class Decisions:
            async def create(self, **kw):
                outer.calls.append("decisions.create")
                return SimpleNamespace()

        class Models:
            async def generate_content(self, **kw):
                outer.calls.append("generate_content")
                raise RuntimeError("no")

        self.aio = SimpleNamespace(decisions=Decisions(), models=Models())
        self.models = SimpleNamespace()


def prop(tmp_path, *extra) -> int:
    return cli.main(["prop", "wooden crate with iron brackets", "--out", str(tmp_path / "out"), *extra])


@pytest.mark.parametrize("engine", [*UNVERIFIABLE, None])
def test_the_command_refuses_before_the_first_model_call_for_every_key(engine, tmp_path, monkeypatch, capsys, engines_present):
    """``agy3d prop`` without --preview (and, for None, without --engine), the SDK client a stub that counts: not even the
    Decisions probe that comes before the collaborators is made."""
    stub = StubClient()
    monkeypatch.setattr(cli, "_client", lambda: stub)
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-not-a-key")  # only passes the start check: the client is the stub
    rc = prop(tmp_path, *(["--engine", engine] if engine else []))
    err = capsys.readouterr().err
    shown = engine or "unreal_5 (the default engine, as no --engine was given)"
    assert rc == 2 and f"a verified run for {shown} cannot succeed on this machine" in err
    assert stub.calls == [] and not (tmp_path / "out" / "runs").exists()


def test_a_verifiable_target_does_make_the_probe(tmp_path, monkeypatch, capsys, engines_present):
    """The control: the stub does count. For godot_4 the run goes on to the probe (and fails later for the stub's emptiness)."""
    stub = StubClient()
    monkeypatch.setattr(cli, "_client", lambda: stub)
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-not-a-key")
    prop(tmp_path, "--engine", "godot_4")
    assert "decisions.create" in stub.calls and "cannot succeed on this machine" not in capsys.readouterr().err


@pytest.mark.parametrize("engine", UNVERIFIABLE)
def test_previews_and_offline_runs_are_untouched(engine, tmp_path, monkeypatch, capsys, engines_present):
    stub = StubClient()
    monkeypatch.setattr(cli, "_client", lambda: stub)
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-not-a-key")
    prop(tmp_path, "--engine", engine, "--preview", "--token-ceiling", "150000", "--max-images", "4")
    assert "cannot succeed on this machine" not in capsys.readouterr().err  # a preview is not asked
    prop(tmp_path / "o", "--engine", engine, "--offline")  # offline: nothing asked either (it runs on the fakes)
    assert "cannot succeed on this machine" not in capsys.readouterr().err


def test_the_engine_flag_records_that_it_was_typed():
    parser = cli.build_parser()
    assert parser.parse_args(["prop", "x", "--out", "o", "--engine", "godot_4"]).engine_given is True
    assert getattr(parser.parse_args(["prop", "x", "--out", "o"]), "engine_given", False) is False
    assert parser.parse_args(["prop", "x", "--out", "o"]).engine == "unreal_5"  # the default is unchanged


# --------------------------------------------------------------------------- the session path


async def _session(ws, engine: str, monkeypatch, *, preview: bool = False) -> str:
    monkeypatch.setenv("AGY3D_OFFLINE", "1")
    host = M.StudioHost()
    kw = dict(preview=True, token_ceiling=100_000, max_images=0) if preview else {}
    await host.open_asset_session("crate", engine, workdir=str(ws / "s"), workspace=str(ws), conversation_id="conv-1", **kw)
    return await host.call_tool("deliver_package", T.ALL_TOOLS["deliver_package"], host.active, {})


@pytest.fixture
def paid_calls(monkeypatch):
    """Every judge and image-generation call a tool can make, recorded (and still run)."""
    from antigravity3d.decisions.engine import DecisionControlPlane
    from antigravity3d.genmedia.nano_banana import ImageForge

    calls: list[str] = []
    for cls, names in ((DecisionControlPlane, [n for n in dir(DecisionControlPlane) if n.startswith("evaluate")] + ["rank_candidates", "policy_gate", "locate_defects"]),
                       (ImageForge, ["seamless_albedo", "paint_over"])):
        for name in names:
            orig = getattr(cls, name, None)
            if orig is None:
                continue

            async def wrap(self, *a, __o=orig, __n=name, **k):
                calls.append(__n)
                return await __o(self, *a, **k)

            monkeypatch.setattr(cls, name, wrap)
    return calls


@pytest.mark.parametrize("engine", UNVERIFIABLE)
async def test_a_session_that_can_never_be_delivered_says_so_at_open_and_no_tool_makes_a_paid_call(engine, ws, monkeypatch, engines_present, paid_calls):
    from antigravity3d.ir import MaterialIR  # noqa: F401
    from antigravity3d.state import EngineBudget
    from antigravity3d.tactics.authoring import TemplateAuthor

    monkeypatch.setenv("AGY3D_OFFLINE", "1")
    host = M.StudioHost()
    opened = await host.open_asset_session("crate", engine, workdir=str(ws / "s"), workspace=str(ws), conversation_id="conv-1")
    assert f"WARNING: {engine} cannot be verified on this machine" in opened
    assert "ask the user whether they want an unverified preview" in opened and "a session for godot_4 or web_three can be verified here" in opened
    sid = host.active
    scene = (await TemplateAuthor().propose("wooden crate", budget=EngineBudget(engine), n=1, context={"asset_name": "crate"}))[0]
    ops = [{"op": "upsert_material", "material": m.model_dump(mode="json", exclude_defaults=True)} for m in scene.materials]
    ops += [{"op": "upsert_node", "node": n.model_dump(mode="json", exclude_defaults=True)} for n in scene.nodes]
    await host.call_tool("apply_scene_ir", T.ALL_TOOLS["apply_scene_ir"], sid, {"ops": ops})
    await host.call_tool("render_preview", T.ALL_TOOLS["render_preview"], sid, {})
    host._set_stage(sid, "LOOKDEV")  # the image tools are served there
    replies = {}
    for tool, a in (("run_visual_qa", {}), ("qa_probe", {}), ("forge_pbr_material", {"material_id": scene.materials[0].id, "prompt": "oak"}),
                    ("run_paintover_loop", {"direction": "worn"}), ("deliver_package", {})):
        replies[tool] = str(await host.call_tool(tool, T.ALL_TOOLS[tool], sid, a))
    assert paid_calls == [], paid_calls  # no judge call and no image call by any tool
    for tool in ("run_visual_qa", "qa_probe", "forge_pbr_material", "run_paintover_loop"):
        assert "nothing was asked of the judge or the image service" in replies[tool] and "ask the user whether they want an unverified preview" in replies[tool], (tool, replies[tool])
    assert replies["deliver_package"].startswith("NOT DELIVERED: a person must decide")


async def test_a_script_session_that_can_never_be_delivered_says_so_in_the_first_reply(ws, monkeypatch, engines_present):
    host = M.StudioHost(script=True)
    first = await build(host, ws, write(ws, crate(engine="unity_urp")))
    assert "WARNING: unity_urp cannot be verified on this machine" in first and "start_preview" in first
    assert "change engine= in the script to a target that can be verified here" in first


async def test_the_preview_switch_is_the_way_out_and_a_preview_session_is_not_told(ws, monkeypatch, engines_present, paid_calls):
    monkeypatch.setenv("AGY3D_OFFLINE", "1")
    host = M.StudioHost()
    opened = await host.open_asset_session("crate", "roblox_r15", workdir=str(ws / "s"), workspace=str(ws), conversation_id="conv-1",
                                           preview=True, token_ceiling=100_000, max_images=0)
    assert "cannot be verified on this machine" not in opened


@pytest.mark.parametrize("engine", UNVERIFIABLE)
async def test_a_session_that_must_be_verified_for_an_unverifiable_target_refuses_at_once(engine, ws, monkeypatch, engines_present):
    reply = await _session(ws, engine, monkeypatch)
    assert reply.startswith("NOT DELIVERED: a person must decide")
    assert f"{engine} cannot be verified on this machine" in reply and "a session for godot_4 or web_three can be verified here" in reply
    assert "start_preview" in reply  # and the preview switch, the other way out


@pytest.mark.parametrize("engine", VERIFIABLE)
async def test_a_verifiable_target_gets_past_the_admission_in_a_session(engine, ws, monkeypatch, engines_present):
    reply = await _session(ws, engine, monkeypatch)
    assert "cannot be verified on this machine" not in reply  # it fails later on what it lacks (an empty scene), not on its target


@pytest.mark.parametrize("engine", UNVERIFIABLE)
async def test_a_preview_session_for_any_target_is_not_asked(engine, ws, monkeypatch, engines_present):
    reply = await _session(ws, engine, monkeypatch, preview=True)
    assert "cannot be verified on this machine" not in reply


async def test_control_a_verifiable_session_does_call_the_judge(ws, monkeypatch, engines_present, paid_calls):
    """The recorder sees calls: the same tools on a session for a target that can be verified do reach the judge."""
    from antigravity3d.ir import MaterialIR  # noqa: F401
    from antigravity3d.state import EngineBudget
    from antigravity3d.tactics.authoring import TemplateAuthor

    monkeypatch.setenv("AGY3D_OFFLINE", "1")
    host = M.StudioHost()
    await host.open_asset_session("crate", "godot_4", workdir=str(ws / "s"), workspace=str(ws), conversation_id="conv-1")
    sid = host.active
    scene = (await TemplateAuthor().propose("wooden crate", budget=EngineBudget("godot_4"), n=1, context={"asset_name": "crate"}))[0]
    ops = [{"op": "upsert_node", "node": n.model_dump(mode="json", exclude_defaults=True)} for n in scene.nodes]
    ops = [{"op": "upsert_material", "material": m.model_dump(mode="json", exclude_defaults=True)} for m in scene.materials] + ops
    await host.call_tool("apply_scene_ir", T.ALL_TOOLS["apply_scene_ir"], sid, {"ops": ops})
    await host.call_tool("render_preview", T.ALL_TOOLS["render_preview"], sid, {})
    await host.call_tool("run_visual_qa", T.ALL_TOOLS["run_visual_qa"], sid, {})
    assert paid_calls, "the recorder saw no judge call from a session that may call it"


# --------------------------------------------------------------------------- a waiting session can end its turn


async def test_a_session_waiting_on_the_user_may_stop_and_start_preview_is_let_through(tmp_path):
    """Without the any_target_can_be_verified fixture: a default (unreal_5) session that must be verified waits for the user. The
    stop hook lets the agent end its turn to ask (it does not push it back into work that cannot end in a delivery), and the
    pre-tool gate lets start_preview through, the way out the reply names."""
    from google.antigravity import types
    from test_harness_hooks import pre, runner_for, turn
    from test_harness_tools import make_session

    env = make_session(tmp_path)
    assert env.sess().waits_for_user().startswith("unreal_5 cannot be verified on this machine")
    runner, _ = runner_for(env)
    tctx = await turn(runner, env)
    for n in (0, 1):  # an empty scene is an unmet gate: with a wait that only the user can clear, the agent is let stop at once
        assert (await runner.dispatch_stop(tctx, types.StopArgs(continuation_count=n))).decision == types.StopDecision.ALLOW_STOP
    res, _ = await pre(runner, tctx, "start_preview", token_ceiling=100_000, max_images=0)
    assert res.allow, res.message
