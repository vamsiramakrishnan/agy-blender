"""The same command run again in a copy of its folder continues from everything the first run paid for.

Live 2026-11-23 (r5/live_scorecard, r5_crate_C and its repeat): the comparison harness ran arm C's command again into a
copy of its folder (its design record and image cache), with only --out and --token-ceiling different, and it reused
nothing: 298,912 tokens paid again, a different design, different package bytes. The keys did not depend on the ceiling
or the folder (tests below: the first propose's key is the same for both); the first run's design had been rescued by a
correction, and the record held it only under the correction's key, so the repeat asked the propose again, got a
different answer, and every request after it that carries the design missed too. Offline, with the stand-in answering
the propose with a real failed live answer (tests/data/live_answers) and the correction with its fix.
"""

from __future__ import annotations

import glob
import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest

from antigravity3d import cli

sys.path.insert(0, str(Path(__file__).parent))
from no_decisions_key import NoDecisionsKey  # noqa: E402
from test_author_correction import BOLT, FAILED, RIB  # noqa: E402  (the real failed answer and its correction)

BRIEF = "a rugged military supply crate kit-bashed from panels, corner guards, latches and two side handles"
AUTHOR = ("author:propose", "author:correct", "author:improve", "author:look", "author:look_correct", "author:repair")


def run(folder: Path, ceiling: str) -> int:
    return cli.main(["prop", BRIEF, "--engine", "godot_4", "--max-tris", "3000", "--out", str(folder / "out"), "--preview",
                     "--token-ceiling", ceiling, "--max-images", "0", "--single-candidate"])


def shipped(folder: Path) -> dict[str, str]:
    """sha256 of every file the package's manifest lists as shipped (what the harness compares: same bytes?)."""
    [man] = glob.glob(str(folder / "out" / "runs" / "*" / "package" / "manifest.json"))
    return {f: v.get("sha256") for f, v in (json.loads(Path(man).read_text()).get("file_hashes") or {}).items()}


@pytest.fixture
def standin(monkeypatch):
    started = []

    def start(**kw):
        api = NoDecisionsKey(propose_answers=[FAILED], corrections=[{"nodes": [RIB, BOLT], "materials": [], "remove": []}], **kw)
        api.__enter__()
        started.append(api)
        monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", api.url)
        monkeypatch.setenv("GEMINI_API_KEY", "dummy-not-a-key")
        monkeypatch.delenv("AGY3D_THREE_JS", raising=False)
        return api

    yield start
    for api in started:
        api.__exit__(None, None, None)


def test_a_repeat_in_a_copy_of_the_folder_with_another_ceiling_reuses_every_answer_and_ships_the_same_bytes(standin, tmp_path, capsys):
    api = standin()
    first, again = tmp_path / "r5_crate_C", tmp_path / "r5_crate_C.repeat"
    assert run(first, "1185000") in (0, 3)
    out = capsys.readouterr().out
    paid = [k for k in api.kinds() if k in AUTHOR]
    assert paid[:2] == ["author:propose", "author:correct"], paid  # the design was rescued by a correction
    assert "design record: 0 design(s)" in out
    # the harness's repeat (compare_agents.run_c with out_seed): a new --out holding a copy of the first run's cache
    (again / "out").mkdir(parents=True)
    shutil.copytree(first / "out" / ".agy3d-cache", again / "out" / ".agy3d-cache")
    n = len(api.requests)
    assert run(again, "585000") in (0, 3)
    out = capsys.readouterr().out
    assert [r["kind"] for r in api.requests[n:] if r["kind"] in AUTHOR] == []  # not one author call
    assert "design record: 1 design(s)" in out and "; 0 recorded" in out
    assert shipped(again) == shipped(first)  # same request, same bytes


def test_the_records_key_holds_neither_the_ceiling_nor_the_folder(standin, tmp_path, monkeypatch):
    """What the key of the first design is made of, for the two live runs' ceilings and folders: the same key (offline
    it is design-78735296544c0882..., the key the live repeat recorded for its propose)."""
    from antigravity3d.tactics import cached

    keys: list[tuple[str, str]] = []
    real = cached.DesignRecord.key

    def key(self, req, index):
        k = real(self, req, index)
        keys.append((req.operation, k))
        return k

    monkeypatch.setattr(cached.DesignRecord, "key", key)
    standin()
    run(tmp_path / "a", "1185000")
    first = next(k for op, k in keys if op == "propose")
    keys.clear()
    run(tmp_path / "b", "585000")
    assert next(k for op, k in keys if op == "propose") == first


def test_a_rescued_design_is_recorded_under_the_request_that_was_asked(standin, tmp_path):
    """The record now holds the rescued design twice: under the correction's key (as before) and under the propose
    request's own key, with both calls' cost, so a rerun that asks the propose finds it."""
    from antigravity3d.kernel.cache import LocalBlobStore

    standin()
    run(tmp_path / "a", "1185000")
    store = LocalBlobStore(tmp_path / "a" / "out" / ".agy3d-cache")
    recs = [json.loads(store.get(Path(p).name)) for p in glob.glob(str(tmp_path / "a" / "out" / ".agy3d-cache" / "de" / "si" / "design-*"))]
    by_op = {r["original"]["operation"]: r for r in recs}
    assert {"propose", "correct"} <= set(by_op)
    assert by_op["propose"]["answer"] == by_op["correct"]["answer"] and by_op["propose"]["original"]["rescued_by_correction"]
    assert by_op["propose"]["original"]["booked"] > by_op["correct"]["original"]["booked"]  # both calls


def test_improvement_rounds_and_the_look_round_are_reused_too_when_the_judge_answers_the_same(standin, tmp_path, capsys):
    """The repeat paid author.improve and author.look again live: they are recorded (keyed by the design, its renders'
    bytes, the brief and the manifest; no judge output is in the request), so with the design reused they are found as
    long as the judge sends the run down the same path. The judge's own answers are not recorded: here both runs get the
    same scripted judge (a fresh stand-in each), as a deterministic judge would answer."""
    lid = {"ops": [{"op": "set_param", "node_id": "crate_lid", "name": "bevel", "value": 0.01}]}
    script = dict(score_sequence={"score_brief_depiction": ["3", "4"]}, patches={"author:improve": [lid]})
    first, again = tmp_path / "a", tmp_path / "b"
    api = standin(**script)
    assert run(first, "1185000") in (0, 3)
    capsys.readouterr()
    paid = [k for k in api.kinds() if k in AUTHOR]
    assert "author:improve" in paid and "author:look" in paid, paid
    (again / "out").mkdir(parents=True)
    shutil.copytree(first / "out" / ".agy3d-cache", again / "out" / ".agy3d-cache")
    api2 = standin(**script)
    assert run(again, "585000") in (0, 3)
    assert [k for k in api2.kinds() if k in AUTHOR] == []
    assert shipped(again) == shipped(first)
