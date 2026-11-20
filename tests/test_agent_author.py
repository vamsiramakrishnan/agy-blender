"""Stage-session authoring (an unbound ``AgentAuthor``): no requested candidate ends without a recorded outcome.

The governed pipeline binds the author to its spend governor and authors through one-shot model calls
(``tests/test_metered_author.py``). The environment-kit pipeline and direct callers use the stage sessions tested here.
The stage run itself is scripted: these tests are about what the author does with a session's result or failure.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from antigravity3d.ir import NodeIR, SceneIR, Transform
from antigravity3d.state import EngineBudget
from antigravity3d.tactics.authoring import STAGE_TIMEOUT_S, AgentAuthor

BUDGET = EngineBudget("unreal_5")
CONTEXT = {"asset_name": "Crate"}


def _scene() -> SceneIR:
    return SceneIR(asset_name="Crate", nodes=[NodeIR(id="body", kind="box", params={"size": [1, 1, 1]}, transform=Transform(translate=(0, 0, 0.5)))])


def _author(script: dict, tmp_path, seen: list | None = None) -> AgentAuthor:
    """An author whose stage runs follow ``script``: per candidate index (or "repair") a scene the session builds, an
    exception the run raises, or None for a session that ends without building anything."""
    from antigravity3d.harness import session as S

    async def run_stage(stage, sid, prompt, *, role, timeout_s):
        tag = sid.split("-")[0]
        if seen is not None:
            seen.append({"stage": stage, "tag": tag, "role": role, "timeout_s": timeout_s})
        what = script[int(tag.removeprefix("cand"))] if tag.startswith("cand") else script[tag]
        if isinstance(what, BaseException):
            raise what
        if what is not None:
            S.DEFAULT_REGISTRY.resource(sid, S.R_STAGE).apply_ir(what)
        return SimpleNamespace(usage={"prompt_tokens": 1})

    author = AgentAuthor(workdir=tmp_path)
    author._run_stage = lambda: run_stage
    return author


async def test_a_failed_session_is_a_recorded_outcome_beside_the_ones_that_worked(tmp_path):
    seen: list = []
    author = _author({0: _scene(), 1: TimeoutError(), 2: RuntimeError("Error 502: upstream unreachable")}, tmp_path, seen)
    result = await author.propose_with_outcomes("a crate", budget=BUDGET, n=3, context=CONTEXT)
    assert [s.asset_name for s in result.scenes] == ["Crate"] and [n.id for n in result.scenes[0].nodes] == ["body"]
    assert [(o.index, o.status) for o in result.outcomes] == [(0, "produced"), (1, "timed_out"), (2, "error")]
    assert f"{STAGE_TIMEOUT_S:.0f} s" in result.outcomes[1].detail  # a bare TimeoutError says nothing by itself
    assert "RuntimeError" in result.outcomes[2].detail and "502" in result.outcomes[2].detail
    assert sorted(s["tag"] for s in seen) == ["cand0", "cand1", "cand2"]
    assert all(s["stage"] == "BLOCKOUT" and s["timeout_s"] == STAGE_TIMEOUT_S for s in seen)
    assert len(author.usage) == 1  # usage is booked for the session that returned


async def test_propose_returns_the_scenes_of_the_sessions_that_worked(tmp_path):
    author = _author({0: RuntimeError("boom"), 1: _scene()}, tmp_path)
    scenes = await author.propose("a crate", budget=BUDGET, n=2, context=CONTEXT)
    assert len(scenes) == 1 and scenes[0].nodes[0].id == "body"


async def test_one_failed_session_raises_its_own_error_instead_of_returning_nothing(tmp_path):
    """The environment-kit pipeline asks for one candidate and takes element 0: an empty list hid the cause."""
    author = _author({0: RuntimeError("Error 502: upstream unreachable")}, tmp_path)
    with pytest.raises(RuntimeError, match="502"):
        await author.propose("a crate", budget=BUDGET, n=1, context=CONTEXT)
    author = _author({0: TimeoutError()}, tmp_path)
    with pytest.raises(TimeoutError, match=f"{STAGE_TIMEOUT_S:.0f} s"):
        await author.propose("a crate", budget=BUDGET, n=1, context=CONTEXT)


async def test_when_no_session_produces_a_scene_the_error_names_every_candidate(tmp_path):
    author = _author({0: RuntimeError("Error 502: upstream unreachable"), 1: TimeoutError()}, tmp_path)
    with pytest.raises(RuntimeError, match=r"502.*no stage session of 2 produced a scene.*#0 error.*#1 timed_out"):
        await author.propose("a crate", budget=BUDGET, n=2, context=CONTEXT)


async def test_a_session_that_builds_nothing_is_not_a_candidate(tmp_path):
    author = _author({0: None, 1: _scene()}, tmp_path)
    result = await author.propose_with_outcomes("a crate", budget=BUDGET, n=2, context=CONTEXT)
    assert [o.status for o in result.outcomes] == ["invalid", "produced"] and "no nodes" in result.outcomes[0].detail
    assert len(result.scenes) == 1
    with pytest.raises(ValueError, match="no nodes"):
        await _author({0: None}, tmp_path).propose("a crate", budget=BUDGET, n=1, context=CONTEXT)


async def test_a_failed_repair_session_raises(tmp_path):
    author = _author({0: _scene(), "repair": RuntimeError("Error 502: upstream unreachable")}, tmp_path)
    scene = (await author.propose("a crate", budget=BUDGET, n=1, context=CONTEXT))[0]
    with pytest.raises(RuntimeError, match="502"):
        await author.repair(scene, action="iterate", located={"floating": ["body"]}, manifest="body: box")
