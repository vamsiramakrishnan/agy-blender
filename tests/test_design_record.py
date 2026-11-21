"""A run that ends early keeps the design it paid for, and a texture call answered without an image is asked again once.

Findings of the live run of 2026-11-21: a run ended by a service error had paid for an authored design (about 9,000
tokens) and two texture images; the images were cached, the design was not, so a rerun paid for another design whose
material words no longer matched the cached images. And one albedo call came back with no image (MAX_TOKENS, 652 output
tokens), so in single-candidate mode its material shipped flat.

Pinned here, offline (``tests/no_decisions_key.py``; no key, no live call, no price variable set here):
* the same command again after a run that failed after authoring sends no authoring request, reuses the design and the
  images, finishes, and the rows it adds to the asset's ledger are only what it spent (the design: a receipt from the
  record, nothing booked);
* ``--new-design`` asks for a design again and still reuses the cached images and the route;
* the record's key is the whole request: one changed character, the rules text, the budget, the tier, the schema miss;
  the timeout does not; a referenced file counts by its bytes, not its (expiring) URI;
* best-of-N: N designs recorded and reused as N; a repair is reused when its request (scene, findings) is the same;
* an image answer without an image is asked again once inside the ledger, in a whole run;
* a kit: a first run in a fresh --out makes the same calls and the same files with the cache as without it, and a rerun
  reuses its pieces.
"""

from __future__ import annotations

import asyncio
import collections
import glob
import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from antigravity3d import cli, ir_schema
from antigravity3d.kernel.cache import BuildCache, LocalBlobStore
from antigravity3d.state import EngineBudget
from antigravity3d.tactics.cached import DesignRecord
from antigravity3d.tactics.governor import Governor, plan_for
from antigravity3d.tactics.metered_author import MeteredAuthor, build_author_request, propose_task

sys.path.insert(0, str(Path(__file__).parent))
from no_decisions_key import NoDecisionsKey  # noqa: E402

PREVIEW = ["--preview", "--token-ceiling", "150000", "--max-images", "4", "--single-candidate", "--art-direction", "retro-lowres"]


@pytest.fixture
def key(monkeypatch):
    def start(**kw):
        api = NoDecisionsKey(**kw).__enter__()
        monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", api.url)
        monkeypatch.setenv("GEMINI_API_KEY", "dummy-not-a-key")
        monkeypatch.delenv("AGY3D_THREE_JS", raising=False)
        started.append(api)
        return api

    started: list[NoDecisionsKey] = []
    yield start
    for api in started:
        api.__exit__(None, None, None)


def prop(out, *args):
    return cli.main(["prop", "wooden crate with iron brackets", "--out", str(out), *args])


def report(out) -> dict:
    data = json.loads((Path(out) / "report.json").read_text())
    return data.get("extras", data)


def count(api, kind) -> int:
    return sum(k == kind for k in api.kinds())


# ---------------------------------------------------------------------------------------------- a whole run, twice


def test_a_rerun_after_a_late_failure_continues_from_the_design_it_paid_for(key, tmp_path, capsys):
    out = tmp_path / "out"
    first = key(refuse_questions=("score_pbr_realism",))  # pass B's texture ranking refused: the run fails after authoring
    assert prop(out, *PREVIEW) == 1
    capsys.readouterr()
    assert count(first, "author:propose") == 1 and count(first, "image:sent") == 3
    before = report(out)["cost"]
    rows_before = len(before["receipts"])
    assert before["design_record"] == {"reused": 0, "reused_designs": 0, "reused_repairs": 0, "recorded": 1,
                                       "new_design": False, "original_booked": {}}
    original = next(r for r in before["receipts"] if r["category"] == "author.propose")

    again = key()  # the same command, the service healthy again
    rc = prop(out, *PREVIEW)
    text = capsys.readouterr().out
    assert rc == 0, text
    # no authoring request at all (not even a count of one), no image: the design and the images come from the run's cache
    assert count(again, "author:propose") == 0 and count(again, "image:sent") == 0
    assert not any("nodes" in json.dumps(r) for r in again.requests if r["kind"] == "count")
    x = report(out)
    c = x["cost"]
    assert c["design_record"]["reused_designs"] == 1 and c["design_record"]["recorded"] == 0
    assert c["design_record"]["original_booked"] == {"tokens": original["booked_tokens"]}
    assert x["geometry_candidates"]["outcomes"][0]["recorded"]["booked"] == original["booked_tokens"]
    assert x["geometry_candidates"]["outcomes"][0]["usage"] is None  # this run used nothing for it
    # the rows this run added to the asset's ledger: the design from the record books nothing and is no call
    added = c["receipts"][rows_before:]
    design, = [r for r in added if r["category"] == "author.propose"]
    assert design["source"] == "record" and design["booked_tokens"] == 0 and design["reserved_tokens"] == 0
    assert design["original"]["booked"] == original["booked_tokens"] and design["original"]["usage"] == {
        k: original[k] for k in ("input_tokens", "output_tokens", "thinking_tokens")}
    assert all(r["source"] == "cache" and r["booked_tokens"] == 0 for r in added if r["category"].startswith("image."))
    assert c["this_run"]["spent_tokens"] == sum(r["booked_tokens"] for r in added)
    assert c["this_run"]["images"] == 0 and c["governor_tokens"] == before["governor_tokens"] + c["this_run"]["spent_tokens"]
    # said before any call, and in the summary beside the image cache
    lines = text.splitlines()
    note = next(i for i, l in enumerate(lines) if l.startswith("note: this asset's ledger already holds spend from earlier runs"))
    assert note < next(i for i, l in enumerate(lines) if l.startswith("agy3d prop:"))  # printed as the run starts
    assert "higher --token-ceiling" in text
    assert "design record: 1 design(s) and 0 repair(s) reused, no call made (originally" in text
    assert "this run: " in text
    # 3 of 4 images were counted and the plan needs at least 2: on a first run that is refused at start; here the design
    # is recorded, so the run continues and says so, in numbers, and its images come from the cache
    assert x["comparison"] is not None
    rnodes = {n["name"]: n for n in json.loads((out / "report.json").read_text())["nodes"]}
    assert rnodes["route"]["data"]["image_check"] == "deferred to propose: rerun with a design record"
    assert rnodes["propose"]["data"]["image_check"] == {"need_at_least": 2, "left": 1, "recorded_designs": 1}
    assert any("could ask for at least 2 image(s) and 1 remain under --max-images" in n and "ships untextured and is named" in n
               for n in rnodes["propose"]["notes"])


def test_new_design_asks_again_and_still_reuses_the_images(key, tmp_path, capsys):
    out = tmp_path / "out"
    roomy = [a if a != "4" else "8" for a in PREVIEW]  # --max-images 8: room for a new design's images
    key()
    assert prop(out, *roomy) == 0
    again = key()
    assert prop(out, *roomy, "--new-design") == 0
    capsys.readouterr()
    assert count(again, "author:propose") == 1  # asked again
    # the stand-in answers the same template scene, so the material words match and the images still come from the cache
    assert count(again, "image:sent") == 0
    d = report(out)["cost"]["design_record"]
    assert d["reused"] == 0 and d["recorded"] == 1 and d["new_design"] is True
    third = key()  # and the next rerun continues from the new design
    assert prop(out, *roomy) == 0
    capsys.readouterr()
    assert count(third, "author:propose") == 0


def test_a_rerun_short_of_images_with_no_recorded_design_is_refused_before_authoring(key, tmp_path, capsys):
    """The earlier run died before authoring answered (after its blueprint image): a rerun would author a new design and
    need new images, so it is refused before any authoring call, exactly as a first run is refused at start."""
    out = tmp_path / "out"
    args = [a if a != "4" else "2" for a in PREVIEW]  # --max-images 2: the blueprint plus one albedo
    first = key(fail=[{"kind": "author:propose", "how": "drop"}])
    assert prop(out, *args) == 1
    capsys.readouterr()
    assert count(first, "image:sent") == 1 and count(first, "author:propose") == 0
    again = key()
    assert prop(out, *args) == 2
    err = capsys.readouterr().err
    assert "needs at least 2 image(s)" in err and "no authoring call was made" in err and "no design for this request is recorded" in err
    assert count(again, "author:propose") == 0 and count(again, "image:sent") == 0
    again2 = key()
    assert prop(out, *args, "--new-design") == 2  # a new design needs new images: refused at start as on a first run
    assert "only the routing call was made" in capsys.readouterr().err and again2.generated() == 0


def test_a_rerun_with_too_little_left_of_the_ceiling_is_refused_at_start(key, tmp_path, capsys):
    key()
    assert prop(tmp_path / "probe", "--preview", "--token-ceiling", "1000", "--max-images", "4") == 2
    need = int(capsys.readouterr().err.split("the smallest ceiling that can admit them is ")[1].split(" ")[0].replace(",", ""))
    out = tmp_path / "out"
    key(refuse_questions=("score_pbr_realism",))
    assert prop(out, *PREVIEW) == 1
    capsys.readouterr()
    spent = int(report(out)["cost"]["governor_tokens"])
    with_ceiling = lambda c: [a if a != "150000" else str(c) for a in PREVIEW]  # noqa: E731
    short = key()
    assert prop(out, *with_ceiling(spent + need - 1)) == 2  # above the first-run minimum, below it once the spend is counted
    err = capsys.readouterr().err
    assert f"--token-ceiling {spent + need - 1:,}: earlier runs of this asset in this --out already used {spent:,} tokens" in err
    assert f"so {need - 1:,} remain" in err and f"up to {need:,} tokens" in err
    assert f"the smallest ceiling that lets it continue is {spent + need:,} tokens" in err and "nothing was generated" in err
    assert short.generated() == 0
    enough = key()
    assert prop(out, *with_ceiling(spent + need)) == 0  # exactly that ceiling: it continues and finishes
    capsys.readouterr()
    assert count(enough, "author:propose") == 0 and enough.generated() > 0


def test_no_cache_and_new_design_are_alternatives(capsys):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["prop", "x", "--out", "o", "--no-cache", "--new-design"])
    assert "not allowed with argument" in capsys.readouterr().err


def test_a_first_run_with_no_cache_writes_no_record(key, tmp_path, capsys):
    out = tmp_path / "out"
    key()
    assert prop(out, *PREVIEW, "--no-cache") == 0
    capsys.readouterr()
    assert "design_record" not in report(out)["cost"] and not (out / ".agy3d-cache").exists()


# ---------------------------------------------------------------------------------------------- the key


def request(task="Build design 1/1 for crate.", *, role="worker", budget=EngineBudget("unreal_5"), context=None,
            timeout_s=90.0, asset="Crate"):
    return build_author_request(role, "propose", task, budget=budget, context=context or {}, timeout_s=timeout_s,
                                asset_name=asset)


def test_the_key_is_the_whole_request(tmp_path, monkeypatch):
    rec = DesignRecord(BuildCache(LocalBlobStore(tmp_path)))
    base = rec.key(request(), 0)
    assert rec.key(request(), 0) == base
    assert rec.key(request("Build design 1/1 for crate!"), 0) != base  # one character of the request
    assert rec.key(request(), 1) != base  # another candidate
    assert rec.key(request(role="director"), 0) != base  # another tier: model, thinking level, output cap
    assert rec.key(request(budget=EngineBudget("unreal_5", max_tris=999)), 0) != base
    assert rec.key(request(asset="Barrel"), 0) != base  # the response schema
    assert rec.key(request(context={"art_direction": "Low-poly."}), 0) != base
    assert rec.key(request(timeout_s=30.0), 0) == base  # how the request is sent is not what it asks
    rules = ir_schema.ir_rules_text()
    monkeypatch.setattr(ir_schema, "ir_rules_text", lambda: rules + ".")  # one character of the rules text
    assert rec.key(request(), 0) != base


def test_a_referenced_file_counts_by_its_bytes_not_its_uri(tmp_path):
    cache = BuildCache(LocalBlobStore(tmp_path))
    rec = DesignRecord(cache)
    cache.note_upload("https://files/a", b"blueprint")
    cache.note_upload("https://files/b", b"blueprint")  # the same bytes uploaded again by a rerun: a new URI
    cache.note_upload("https://files/c", b"another blueprint")
    k = lambda uri: rec.key(request(context={"ortho_uri": uri}), 0)  # noqa: E731
    assert k("https://files/a") == k("https://files/b") != k("https://files/c")
    assert k("https://files/unknown") != k("https://files/other-unknown")  # not uploaded through this cache: kept as is


# ---------------------------------------------------------------------------------------------- the author


class Endpoint:
    """Answers design i of n with i+1 boxes, so the reused designs can be told apart."""

    def __init__(self):
        self.generated, self.counted = [], []

    async def count_tokens(self, **kw):
        self.counted.append(kw)
        return SimpleNamespace(total_tokens=100)

    async def generate_content(self, **kw):
        self.generated.append(kw)
        text = kw["contents"][0].parts[0].text
        if "Return one JSON patch" in text:
            answer = '{"ops":[{"op":"remove_node","node_id":"b0"}]}'
        else:
            i = int(text.split("Build design ")[1].split("/")[0])
            answer = json.dumps({"asset_name": "Crate", "materials": [], "nodes": [
                {"id": f"b{j}", "kind": "box", "params": {"size": [1, 1, 1]}, "transform": {"translate": [2 * j, 0, 0.5]}}
                for j in range(i)]})
        from google.genai import types

        return SimpleNamespace(text=answer, prompt_feedback=None, candidates=[SimpleNamespace(finish_reason=types.FinishReason.STOP)],
                               usage_metadata=SimpleNamespace(prompt_token_count=100, candidates_token_count=30,
                                                              thoughts_token_count=20, total_token_count=150))


def author(tmp_path, governor, *, fresh=False):
    ep = Endpoint()
    rec = DesignRecord(BuildCache(LocalBlobStore(tmp_path / "cache")), fresh=fresh)
    a = MeteredAuthor(SimpleNamespace(aio=SimpleNamespace(models=ep)), governor, EngineBudget("unreal_5"), {"asset_name": "Crate"},
                      timeout_s=5, record=rec)
    return a, ep, rec


def tokens_ledger(tmp_path):
    return Governor(replace(plan_for(1), geometry_candidates=4), token_ceiling=500_000, journal=tmp_path / "spend.sqlite3")


async def test_best_of_n_records_and_reuses_n_designs(tmp_path):
    gov = tokens_ledger(tmp_path)
    a, ep, rec = author(tmp_path, gov)
    first = await a.propose_with_outcomes("crate", budget=a.budget, n=4, context={"asset_name": "Crate"})
    assert len(ep.generated) == 4 and rec.recorded == 4 and gov.spent == 4 * 150
    b, ep2, rec2 = author(tmp_path, gov)  # the rerun: a new author on the same cache folder and ledger
    again = await b.propose_with_outcomes("crate", budget=b.budget, n=4, context={"asset_name": "Crate"})
    assert ep2.generated == [] and ep2.counted == []  # no call, not even a count
    assert [len(s.nodes) for s in again.scenes] == [len(s.nodes) for s in first.scenes] == [1, 2, 3, 4]
    assert [s.model_dump() for s in again.scenes] == [s.model_dump() for s in first.scenes]
    assert all(o.produced and o.recorded == {"unit": "tokens", "booked": 150.0, "model": o.recorded["model"], "role": "worker",
                                             "operation": "propose", "usage": first.outcomes[o.index].usage}
               for o in again.outcomes)
    assert gov.spent == 4 * 150  # nothing booked again
    reused = gov.receipts()[4:]
    assert len(reused) == 4 and all(r["source"] == "record" and r["booked_tokens"] == 0 for r in reused)
    assert rec2.summary()["reused_designs"] == 4 and rec2.summary()["original_booked"] == {"tokens": 600.0}


async def test_a_repair_is_reused_for_the_same_scene_and_findings_only(tmp_path):
    gov = tokens_ledger(tmp_path)
    a, ep, _ = author(tmp_path, gov)
    [scene] = await a.propose("crate", budget=a.budget, n=1, context={"asset_name": "Crate"})
    action = SimpleNamespace(kind="repair", reasons=["floating"])
    p1 = await a.repair(scene, action=action, located={"defect_floating": ["b0"]}, manifest="b0")
    b, ep2, rec2 = author(tmp_path, gov)
    p2 = await b.repair(scene, action=action, located={"defect_floating": ["b0"]}, manifest="b0")
    assert p1 == p2 and ep2.generated == [] and rec2.summary()["reused_repairs"] == 1
    await b.repair(scene, action=SimpleNamespace(kind="repair", reasons=["clipping"]), located={"defect_clipping": ["b0"]},
                   manifest="b0")  # other findings: another question, asked
    assert len(ep2.generated) == 1


async def test_new_design_reads_nothing_and_replaces_the_record(tmp_path):
    gov = tokens_ledger(tmp_path)
    a, _, _ = author(tmp_path, gov)
    await a.propose("crate", budget=a.budget, n=1, context={"asset_name": "Crate"})
    b, ep2, rec2 = author(tmp_path, gov, fresh=True)
    await b.propose("crate", budget=b.budget, n=1, context={"asset_name": "Crate"})
    assert len(ep2.generated) == 1 and rec2.summary() == {**rec2.summary(), "reused": 0, "recorded": 1}


async def test_an_answer_for_another_asset_is_not_recorded(tmp_path):
    gov = tokens_ledger(tmp_path)
    a, ep, rec = author(tmp_path, gov)
    with pytest.raises(ValueError, match="requested asset"):
        await a.propose("crate", budget=a.budget, n=1, context={"asset_name": "Barrel"})
    assert rec.recorded == 0


async def test_the_dollar_ledger_books_nothing_for_a_reused_design(tmp_path, monkeypatch):
    from test_metered_author import make_author  # the existing fixture: its rates are fixtures, not prices

    a, ep, budget, context, gov = make_author(monkeypatch)
    rec = DesignRecord(BuildCache(LocalBlobStore(tmp_path / "cache")))
    a.record = rec
    await a.propose("green crate", budget=budget, n=1, context=context)
    spent = gov.spent
    a.record = DesignRecord(rec.cache)
    await a.propose("green crate", budget=budget, n=1, context=context)
    assert len(ep.generated) == 1 and gov.spent == spent
    last = gov.receipts()[-1]
    assert last["source"] == "record" and last["booked_usd"] == 0 and last["original"] == {
        **last["original"], "unit": "usd", "booked": pytest.approx(spent)}


# ---------------------------------------------------------------------------------------------- an image without an image


def test_an_albedo_answer_without_an_image_is_asked_again_in_a_run(key, tmp_path, capsys):
    api = key(fail=[{"kind": "image", "how": "no_image", "prompt": "dark wrought iron", "times": 1}])
    assert prop(tmp_path / "out", *PREVIEW) == 0
    capsys.readouterr()
    assert count(api, "image:no_image") == 1
    iron = [r for r in api.requests if r["kind"] in ("image:no_image", "image:1K", "image:2K") and "dark wrought iron" in r.get("prompt", "")]
    assert [r["kind"] for r in iron] == ["image:no_image", "image:1K"]  # asked again once, then textured
    x = report(tmp_path / "out")
    assert "not_textured" not in json.dumps(x["pyramid"])
    albedo = [r for r in x["cost"]["receipts"] if r["category"] == "image.albedo"]
    first, = [r for r in albedo if r["status"] == "ImageNotReturned"]
    assert first["images"] == 0 and first["booked_tokens"] == 62 + 40 + 612 and first["image_counted"] == "no image in response"
    second, = [r for r in albedo if r.get("reask") == 1]
    assert second["status"] == "ok" and second["images"] == 1


def test_no_image_twice_leaves_the_material_untextured_with_both_named(key, tmp_path, capsys):
    api = key(fail=[{"kind": "image", "how": "no_image", "prompt": "dark wrought iron", "times": 2}])
    rc = prop(tmp_path / "out", *PREVIEW)
    out = capsys.readouterr().out
    assert count(api, "image:no_image") == 2
    x = report(tmp_path / "out")
    text = json.dumps(x)
    assert "no image in 2 answers (#1 MAX_TOKENS, 652 output tokens; #2 MAX_TOKENS, 652 output tokens)" in text, out
    assert rc in (0, 1)


# ---------------------------------------------------------------------------------------------- a kit


def kit(out, *args):
    return cli.main(["kit", "ruined stone temple", "--pieces", "2", "--out", str(out), "--token-ceiling", "200000",
                     "--max-images", "4", "--art-direction", "retro-lowres", *args])


def files(out) -> dict[str, str]:
    skip = (".agy3d-cache", "spend", "report", "snap")
    return {str(p.relative_to(out)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path(out).rglob("*"))
            if p.is_file() and not any(part.startswith(skip) for part in p.relative_to(out).parts)}


def test_a_first_kit_run_is_the_same_with_the_cache_and_a_rerun_reuses_its_pieces(key, tmp_path, capsys):
    plain = key()
    assert kit(tmp_path / "plain", "--no-cache") == 0
    cached = key()
    assert kit(tmp_path / "cached") == 0
    capsys.readouterr()
    assert collections.Counter(plain.kinds()) == collections.Counter(cached.kinds())
    assert files(tmp_path / "plain") == files(tmp_path / "cached")
    assert (tmp_path / "cached" / ".agy3d-cache").is_dir() and not (tmp_path / "plain" / ".agy3d-cache").exists()
    again = key()
    assert kit(tmp_path / "cached") == 0
    text = capsys.readouterr().out
    assert count(again, "author:propose") == 0 and count(again, "image:sent") == 0
    assert "design record: 2 design(s) and 0 repair(s) reused" in text
    assert files(tmp_path / "plain") == files(tmp_path / "cached")
