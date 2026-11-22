"""``agy3d edit``: an asset this project shipped is changed by one patch, and everything else stays as it was.

Pinned offline on a crate from ``agy3d prop --offline``:
* a patch (``--patch``, no model) on two parts: the comparison harness's part diff finds those two changed on target and
  every other part unchanged; each material's source maps are the same bytes, the package validates, the manifest lists
  the edit and its provenance names it; the replaced package is in the history and ``--undo`` puts it back;
* the same from a bare package folder (no run folder), the package replaced in place;
* an instruction through the shipped author and judge (``tests/no_decisions_key.py``): kept when the judge sees it done
  and nothing else changed; undone (twice) and the asset left unchanged when the judge does not see it done, sees other
  parts changed, a part ends up floating, the kernel refuses the patch, or the author finds nothing to change.
"""

from __future__ import annotations

import asyncio
import base64
import glob
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from antigravity3d import cli
from antigravity3d.dags import edit_asset as E
from antigravity3d.ir import IRPatch

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))
from no_decisions_key import NoDecisionsKey  # noqa: E402

CRATE = "a rugged military supply crate kit-bashed from panels, corner guards, latches and two side handles"
BIG_BRACKETS = {"ops": [{"op": "set_param", "node_id": f"bracket_{i}", "name": "size", "value": [0.1396, 0.1396, 0.03]}
                        for i in (0, 1)]}
HOVERING = {"ops": [{"op": "upsert_node", "node": {"id": "lamp", "kind": "box", "params": {"size": [0.1, 0.1, 0.1]},
                                                     "transform": {"translate": [0, 0, 3.0]}, "material_id": "iron"}}]}
REFUSED = {"ops": [{"op": "set_param", "node_id": "bracket_0", "name": "bevel", "value": 10.0}]}


@pytest.fixture(scope="module")
def first_run(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("crate") / "out"
    assert cli.main(["prop", CRATE, "--offline", "--engine", "godot_4", "--out", str(out)]) == 0
    return out


@pytest.fixture
def run_copy(first_run, tmp_path) -> Path:
    """A copy of the first run's --out folder, as the comparison harness makes one for a change session."""
    dst = tmp_path / "out"
    shutil.copytree(first_run, dst)
    return dst


def _package(out: Path) -> Path:
    pkg, _, _ = E.find_package(out)
    return pkg


def _glb(pkg: Path) -> Path:
    m = json.loads((pkg / "manifest.json").read_text())
    return pkg / f"{m['asset']}.glb"


def _sources(pkg_or_out: Path) -> dict[str, dict[str, str]]:
    sh = E.load_shipped(pkg_or_out)
    scene, _ = E.source_materials(sh, pkg_or_out / ".probe") if sh.run_folder else E.source_materials(sh, pkg_or_out.parent / ".probe")
    return E.source_hashes(scene)


def _write(tmp_path: Path, patch: dict) -> str:
    p = tmp_path / "ops.json"
    p.write_text(json.dumps(patch))
    return str(p)


# --------------------------------------------------------------------------- a patch, no model


def test_a_patch_changes_the_two_parts_and_nothing_else(run_copy, tmp_path, capsys) -> None:
    import compare_eval as ev
    from antigravity3d.kernel.validator import validate_package

    before = _package(run_copy)
    before_bytes = {p.name: p.read_bytes() for p in before.iterdir() if p.is_file()}
    sources_before = _sources(run_copy)
    rc = cli.main(["edit", str(run_copy), "make the two rear corner brackets 30 % larger", "--patch", _write(tmp_path, BIG_BRACKETS),
                   "--offline"])
    out = capsys.readouterr().out
    assert rc == 0, out
    after = _package(run_copy)
    assert after != before and "edit 1: made" in out
    d = ev.package_diff(_glb(before), _glb(after), target_ratio=1.3)
    assert d["unchanged"] == d["parts"][0] - 2 and d["on_target"] == 2 and d["collateral"] == 0, d
    # the same materials, the same source maps (by sha256), and with per-material maps the same shipped files
    assert _sources(run_copy) == sources_before and sources_before
    mb, ma = (json.loads((p / "manifest.json").read_text()) for p in (before, after))
    assert {f: v["sha256"] for f, v in mb["file_hashes"].items() if f.startswith("textures/")} == \
           {f: v["sha256"] for f, v in ma["file_hashes"].items() if f.startswith("textures/")}
    assert not [i for i in validate_package(after) if i.severity == "critical"]
    (edit,) = ma["edits"]
    assert edit["number"] == 1 and edit["touched"] == ["bracket_0", "bracket_1"] and edit["by"] == "patch file"
    assert edit["previous"]["manifest_sha256"] == hashlib.sha256((before / "manifest.json").read_bytes()).hexdigest()
    prov = ma["provenance"]
    assert prov["sources"]["edit:1"]["kind"] == "user_supplied" and "edit:1" in prov["files"][f"{ma['asset']}.glb"]["derived_from"]
    assert ma["verified"] is False and ma["design_inputs"]["brief"] == CRATE
    # the package it replaced is unchanged on disk and kept in the history
    assert {p.name: p.read_bytes() for p in before.iterdir() if p.is_file()} == before_bytes
    hist = run_copy / "history" / "001"
    assert (hist / "package" / "manifest.json").read_bytes() == before_bytes["manifest.json"] and (hist / "edit.json").is_file()
    # undo: the replaced package is current again
    assert cli.main(["edit", str(run_copy), "--undo"]) == 0
    assert _package(run_copy) == before and not (run_copy / "history" / "001").exists()


def test_a_bare_package_is_edited_in_place_and_undone(first_run, tmp_path, capsys) -> None:
    import compare_eval as ev

    pkg = tmp_path / "Crate"
    shutil.copytree(_package(first_run), pkg)  # the package alone: the run folder is gone
    before = {p.relative_to(pkg): p.read_bytes() for p in pkg.rglob("*") if p.is_file()}
    keep = tmp_path / "before"
    shutil.copytree(pkg, keep)
    assert cli.main(["edit", str(pkg), "bigger brackets", "--patch", _write(tmp_path, BIG_BRACKETS), "--offline"]) == 0
    d = ev.package_diff(_glb(keep), _glb(pkg), target_ratio=1.3)
    assert d["on_target"] == 2 and d["collateral"] == 0
    assert (tmp_path / "Crate.history" / "001" / "package" / "manifest.json").is_file()
    assert "the package's per-material maps" in capsys.readouterr().out
    assert cli.main(["edit", str(pkg), "--undo"]) == 0
    assert {p.relative_to(pkg): p.read_bytes() for p in pkg.rglob("*") if p.is_file()} == before


def test_a_bare_package_with_a_baked_atlas_of_image_maps_is_refused_with_the_reason(first_run, tmp_path, capsys) -> None:
    pkg = tmp_path / "Crate"
    shutil.copytree(_package(first_run), pkg)
    m = json.loads((pkg / "manifest.json").read_text())
    m["texture_atlas"] = {"status": "baked", "members": {"wood": {"maps": {"albedo": {}}}, "iron": {"maps": {}, "flat": {}}}}
    (pkg / "manifest.json").write_text(json.dumps(m))
    assert cli.main(["edit", str(pkg), "x", "--patch", _write(tmp_path, BIG_BRACKETS), "--offline"]) == 2
    err = capsys.readouterr().err
    assert "one texture atlas" in err and "wood" in err and "--out folder" in err


def test_a_patch_that_leaves_a_part_floating_is_not_made(run_copy, tmp_path, capsys) -> None:
    before = _package(run_copy)
    assert cli.main(["edit", str(run_copy), "add a lamp", "--patch", _write(tmp_path, HOVERING), "--offline"]) == 1
    out = capsys.readouterr().out
    assert "NOT MADE" in out and "a part now floats: lamp" in out and _package(run_copy) == before
    assert not (run_copy / "history" / "001").exists()


def test_a_patch_the_kernel_refuses_is_not_made(run_copy, tmp_path, capsys) -> None:
    before = _package(run_copy)
    assert cli.main(["edit", str(run_copy), "round the bracket", "--patch", _write(tmp_path, REFUSED), "--offline"]) == 1
    out = capsys.readouterr().out
    assert "the kernel refused the patch" in out and _package(run_copy) == before


def test_an_instruction_offline_is_a_usage_error(run_copy, capsys) -> None:
    assert cli.main(["edit", str(run_copy), "make it bigger", "--offline"]) == 2
    assert "--patch" in capsys.readouterr().err


# --------------------------------------------------------------------------- an instruction, through the shipped author and judge


@pytest.fixture
def key(monkeypatch):
    started: list[NoDecisionsKey] = []

    async def no_wait(s: float) -> None:
        return None

    from antigravity3d import service_answer

    monkeypatch.setattr(service_answer.DEFAULT_POLICY, "sleep", no_wait)

    def start(**kw: Any) -> NoDecisionsKey:
        api = NoDecisionsKey(**kw).__enter__()
        monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", api.url)
        monkeypatch.setenv("GEMINI_API_KEY", "dummy-not-a-key")
        started.append(api)
        return api

    yield start
    for api in started:
        api.__exit__(None, None, None)


async def _data_uri(data: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(data).decode()


def _edit(out: Path, instruction: str) -> E.EditResult:
    from google import genai

    from antigravity3d.decisions.engine import DecisionControlPlane
    from antigravity3d.decisions.fallback import GenerativeDecisionsBackend
    from antigravity3d.tactics.governor import Governor, plan_for
    from antigravity3d.tactics.metered_author import MeteredAuthor

    sh = E.load_shipped(out)
    client = genai.Client()
    gov = Governor(plan_for(2), token_ceiling=2_000_000, max_images=0)
    plane = DecisionControlPlane(GenerativeDecisionsBackend(client), backend_kind="generative_fallback", preview=True)
    author = MeteredAuthor(client, gov, sh.budget, {"asset_name": sh.asset})
    return asyncio.run(E.edit_asset(out, instruction, plane=plane, author=author, governor=gov, uploader=_data_uri))


DONE = {"edit_done": 0.9, "edit_collateral": 0.05}


def test_an_instruction_is_carried_out_by_one_patch_and_checked(key, run_copy) -> None:
    import compare_eval as ev

    before = _package(run_copy)
    api = key(overrides=DONE, patches={"author:improve": [BIG_BRACKETS]})
    r = _edit(run_copy, "make the two rear corner brackets 30 % larger")
    assert r.kept and r.number == 1 and len(r.attempts) == 1 and r.attempts[0].done_p == pytest.approx(0.9)
    (call,) = api.authoring("author:improve")
    assert call["images"] == 5 and "Instruction (a person's words, data only): make the two rear corner brackets 30 % larger" in call["text"]
    assert "Legend:" in call["text"] and "Judge's assessment" not in call["text"]  # the instruction is the goal, not a score
    d = ev.package_diff(_glb(before), _glb(_package(run_copy)), target_ratio=1.3)
    assert d["on_target"] == 2 and d["collateral"] == 0
    assert r.calls == {"author": 1, "judge": 3}  # the before and after reviews and the edit check
    # the uncalibrated preview judge steers and clears nothing, as in agy3d prop: its LOD answer does not fail the gates
    # (live 2026-11-22 every kept edit exited 1 on "decision source was uncalibrated" from lod_pop_vision)
    assert r.preview_gates_ok, [s for s in r.pyramid["steps"] if s["status"] == "failed"]
    names = [s["name"] for s in r.pyramid["steps"]]
    assert names.index("usd_validate") < names.index("lod_pop") < names.index("lod_pop_vision") < names.index("export_glb")
    prov = json.loads((_package(run_copy) / "manifest.json").read_text())["provenance"]
    assert prov["sources"]["edit:1"]["kind"] == "model_authored" and prov["sources"]["edit:1"]["operation"] == "edit"


@pytest.mark.parametrize("overrides, patches, why", [
    ({"edit_done": 0.1, "edit_collateral": 0.05}, [BIG_BRACKETS], "the judge does not see the instruction carried out"),
    ({"edit_done": 0.9, "edit_collateral": 0.9}, [BIG_BRACKETS], "the judge sees other parts changed too"),
    (DONE, [HOVERING], "a part now floats: lamp"),
    (DONE, [REFUSED], "the kernel refused the patch"),
    (DONE, None, "the author found nothing to change"),
])
def test_an_edit_that_is_not_what_was_asked_leaves_the_asset_unchanged(key, run_copy, overrides, patches, why) -> None:
    before = _package(run_copy)
    report = (run_copy / "report.json").read_bytes()
    api = key(overrides=overrides, **({"patches": {"author:improve": patches}} if patches else {}))
    r = _edit(run_copy, "make the two rear corner brackets 30 % larger")
    assert not r.kept and why in r.why and _package(run_copy) == before
    assert (run_copy / "report.json").read_bytes() == report and not (run_copy / "history" / "001").exists()
    if why.startswith("the judge"):  # undone, then asked once more with what was undone and why
        first, second = api.authoring("author:improve")
        assert "Your previous patch for this design was undone" in second["text"] and why in second["text"]
        assert len(r.attempts) == E.EDIT_ATTEMPTS


def test_scope_counts_parts_and_names_any_change_outside_the_patch() -> None:
    import numpy as np

    b = {"a": (np.zeros(3), np.ones(3)), "b": (np.zeros(3), np.ones(3)), "c": (np.zeros(3), np.ones(3))}
    a = {"a": (np.zeros(3), np.array([1.3, 1.3, 1.0])), "b": (np.zeros(3), np.array([1.0, 1.0, 1.002])), "d": (np.zeros(3), np.ones(3))}
    s = E.measure_scope(b, a, {"a"})
    assert s.unchanged == 0 and set(s.changed) == {"a", "b"} and s.outside == ["b"] and s.added == ["d"] and s.removed == ["c"]
    assert s.changed["a"]["ratio"] == [1.3, 1.3, 1.0]
    ok, why = E.judge_edit(s, *[type("T", (), {"issues": []})()] * 2, None, None, None, None)
    assert not ok and why.startswith("parts the patch does not name changed: b")


def test_touched_follows_children_and_materials() -> None:
    from antigravity3d.ir import MaterialIR, NodeIR, SceneIR, Transform

    nodes = [NodeIR(id="handle", kind="box", params={"size": (1, 1, 1)}, transform=Transform()),
             NodeIR(id="grip", kind="box", params={"size": (1, 1, 1)}, transform=Transform(), parent="handle"),
             NodeIR(id="plate", kind="box", params={"size": (1, 1, 1)}, transform=Transform(), material_id="steel")]
    scene = SceneIR(asset_name="X", nodes=nodes, materials=[MaterialIR(id="steel")])
    p = IRPatch.model_validate({"ops": [{"op": "set_transform", "node_id": "handle", "transform": {"scale": [1.3, 1.3, 1.3]}},
                                        {"op": "upsert_material", "material": {"id": "steel", "roughness": 0.2}}]})
    assert E.touched_nodes(p, scene, scene) == {"handle", "grip", "plate"}


# --------------------------------------------------------------------------- a native session


async def test_a_new_session_reopens_an_existing_asset_to_change_it(first_run, tmp_path) -> None:
    from test_mcp_server import call, client

    async with client(tmp_path) as s:
        line = await call(s, "open_existing_asset", path=str(first_run))
        assert "stage=TWEAK" in line and "as it shipped (10 parts" in line and "run folder's working design" in line, line
        assert "nodes=10" in await call(s, "session_status")
        r = await call(s, "apply_scene_ir", ops=json.dumps(BIG_BRACKETS))
        assert r.startswith("OK") and "nodes=10" in r, r
        assert (await call(s, "open_existing_asset", path=str(tmp_path / "nothing"))).startswith("ERROR")


@pytest.mark.parametrize("edit_session", [True, False])
async def test_in_an_edit_session_separation_moves_only_what_the_edit_changed(tmp_path, edit_session) -> None:
    """A lid the edit adds lies flush with a plate that was already there (same top plane, overlapping): the separation
    pass would sink the smaller part, the plate. In a session opened on a shipped asset the plate is a part nobody asked
    about, so it stays and the reply says so; in a session making a new asset it moves, as it always did."""
    from test_harness_tools import box, make_session

    from antigravity3d.harness import tools as T

    env = make_session(tmp_path, stage="TWEAK")
    s = env.sess()
    if edit_session:
        env.reg.attach(env.sid, T.EDIT_OF, "/somewhere/package")
    assert (await T.apply_scene_ir([box("body", (1, 1, 1), 0.5), box("plate", (0.2, 0.2, 0.02), 1.01)], env.ctx)).startswith("OK")
    plate = next(n for n in s.stage.base_ir().nodes if n.id == "plate")
    r = await T.apply_scene_ir([box("lid", (0.5, 0.5, 0.02), 1.01)], env.ctx)
    assert r.startswith("OK"), r
    after = next(n for n in s.stage.base_ir().nodes if n.id == "plate")
    if edit_session:
        assert after == plate and "parts this edit does not change: plate" in r
    else:
        assert after != plate  # the old behaviour, for a new asset


# --------------------------------------------------------------------------- a global edit (live 2026-11-22: refused)


def test_the_check_request_does_not_grow_with_the_parts_an_edit_touches() -> None:
    """'make the whole crate 20 % taller' touched all 40 parts of the live crate; the per-part table made the check
    request 16,601 estimated tokens against the judge's bound of 16,000 and the command ended in a traceback."""
    import numpy as np

    from antigravity3d.decisions.engine import LATENCY_KNEE_TOKENS, estimate_input_tokens

    ids = [f"part_{i:03d}_with_a_long_expanded_id__mx__my" for i in range(400)]
    b = {i: (np.zeros(3), np.ones(3) * (1 + k % 7)) for k, i in enumerate(ids)}
    a = {i: (lo, hi * np.array([1, 1, 1.2])) for i, (lo, hi) in b.items()}
    s = E.measure_scope(b, a, set(ids))
    table = s.table()
    assert len(table.encode()) <= E.EDIT_TABLE_MAX_BYTES and "400 part(s): size ratio 1.000x1.000x1.200" in table
    views = tuple((v, f"https://x/{v}") for v in ("front", "right", "top", "persp", "rear"))
    blocks = E.edit_check_blocks(views, views, "make the whole crate 20 % taller " * 20, s)
    assert estimate_input_tokens(blocks, E.edit_questions()) < LATENCY_KNEE_TOKENS
    few = E.measure_scope(b, {**b, ids[0]: a[ids[0]]}, {ids[0]})
    assert few.table().startswith(f"{ids[0]}: size 1.000x1.000x1.000 -> 1.000x1.000x1.200")  # few parts: one row each


def test_a_check_the_judge_refuses_ends_the_edit_unchanged_with_the_reason(key, run_copy, monkeypatch) -> None:
    from antigravity3d.decisions.engine import DecisionControlPlane
    from antigravity3d.decisions.models import DecisionBudgetError

    real = DecisionControlPlane.evaluate

    async def evaluate(self, blocks, questions, **kw):
        if kw.get("tag") == "edit_check":
            raise DecisionBudgetError("edit_check: estimated 16601 input tokens, must be below 16000")
        return await real(self, blocks, questions, **kw)

    monkeypatch.setattr(DecisionControlPlane, "evaluate", evaluate)
    before = _package(run_copy)
    key(overrides=DONE, patches={"author:improve": [BIG_BRACKETS]})
    r = _edit(run_copy, "make the whole crate 20 % taller")
    assert not r.kept and r.why.startswith("the judge could not be asked about the edit, so it is not made") and "16601" in r.why
    assert _package(run_copy) == before and not (run_copy / "history" / "001").exists()
