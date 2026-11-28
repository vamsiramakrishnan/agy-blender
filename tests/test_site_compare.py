"""The website comparison's judge (benchmarks/site_compare.py): what is asked, in which order, and how verdicts add up.
No model is called: ``judge`` takes the function that asks."""
import json
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))
import site_compare as sc  # noqa: E402


def _site(root: Path, name: str, colour: tuple[int, int, int], n: int = 3) -> Path:
    d = root / name
    d.mkdir(parents=True)
    for i in range(n):
        Image.new("RGB", (1440, 900), colour).save(d / f"desktop_{i:02d}.jpg")
        Image.new("RGB", (1440, 900), colour).save(d / f"desktop_{i:02d}_canvas.jpg")  # the canvas-only frame: not judged
    return d


def _verdict(overall: str, **criteria: str) -> dict:
    return {"criteria": {c: {"winner": criteria.get(c, "tie"), "reason": "r"} for c in sc.CRITERIA},
            "overall": {"winner": overall, "reason": "r"}}


def test_every_pair_is_asked_in_both_orders_the_same_number_of_times():
    calls = sc.plan(["run1", "plugin", "stock"], repeats=3)
    assert len(calls) == 3 * 2 * 3
    for a, b in (("plugin", "run1"), ("plugin", "stock"), ("run1", "stock")):
        assert sum(c["first"] == a and c["second"] == b for c in calls) == 3
        assert sum(c["first"] == b and c["second"] == a for c in calls) == 3
    assert calls == sc.plan(["stock", "run1", "plugin"], repeats=3), "the order of the calls is a function of the names and the seed"


def test_frames_are_the_page_frames_in_scroll_order_and_are_shrunk_for_the_judge(tmp_path):
    d = _site(tmp_path, "a", (10, 20, 30), n=11)
    got = sc.frames(d)
    assert [p.name for p in got] == [f"desktop_{i:02d}.jpg" for i in range(11)]
    with Image.open(__import__("io").BytesIO(sc.prepared(got[0]))) as im:
        assert im.size == (960, 600)
    with pytest.raises(SystemExit):
        sc.frames(tmp_path / "nothing")


def test_a_site_wins_a_criterion_only_with_more_than_half_of_the_verdicts_cast():
    calls = [{"first": "a", "second": "b", "rep": 0, "verdict": _verdict("1", craft_of_the_3d="1")},
             {"first": "b", "second": "a", "rep": 0, "verdict": _verdict("2", craft_of_the_3d="1")},
             {"first": "a", "second": "b", "rep": 1, "verdict": _verdict("1", craft_of_the_3d="tie")},
             {"first": "b", "second": "a", "rep": 1, "error": "refused"}]
    slot = sc.aggregate(calls)["a vs b"]
    assert slot["calls"] == 3
    assert slot["votes"]["overall"] == {"a": 3} and slot["result"]["overall"] == "a"
    assert slot["votes"]["craft_of_the_3d"] == {"a": 1, "b": 1, "tie": 1} and slot["result"]["craft_of_the_3d"] == "no majority"
    assert slot["result"]["type_and_layout"] == "no majority", "three ties are not a win"


def test_judge_shows_both_sites_blind_keeps_each_call_and_asks_again_only_for_new_frames(tmp_path):
    sites = {"plugin": _site(tmp_path, "p", (200, 0, 0)), "stock": _site(tmp_path, "s", (0, 0, 200))}
    asked = []

    def ask(parts):
        asked.append(parts)
        text = " ".join(p for kind, p in parts if kind == "text")
        assert "plugin" not in text and "stock" not in text, "the judge never learns which run made a site"
        assert sum(kind == "image" for kind, _ in parts) == 6
        if len(asked) == 2:
            raise RuntimeError("503 high demand")
        return _verdict("1")

    out = tmp_path / "out"
    res = sc.judge(sites, "build a site", out, ask, repeats=1)
    assert len(asked) == 2 and [bool(c.get("verdict")) for c in res["calls"]] == [True, False]
    assert "503" in res["calls"][1]["error"]
    res = sc.judge(sites, "build a site", out, ask, repeats=1)  # only the failed call is asked again
    assert len(asked) == 3 and all(c.get("verdict") for c in res["calls"])
    assert json.loads((out / "judge.json").read_text())["aggregate"]["plugin vs stock"]["calls"] == 2
    Image.new("RGB", (1440, 900), (0, 200, 0)).save(sites["stock"] / "desktop_00.jpg")  # the site changed: its verdicts are stale
    sc.judge(sites, "build a site", out, ask, repeats=1)
    assert len(asked) == 5
    assert "plugin 1" in sc.table(res) or "stock 1" in sc.table(res)


def _film(root: Path, name: str, colour: tuple[int, int, int], beats: int = 5) -> Path:
    d = root / name
    d.mkdir(parents=True)
    for i in range(beats):
        Image.new("RGB", (1440, 900), colour).save(d / f"desktop_b{i:02d}.jpg")
    Image.new("RGB", (1440, 900), colour).save(d / "desktop_sheet.jpg")  # the contact sheet: not a beat
    return d


def test_stories_are_judged_beat_by_beat_on_criteria_of_their_own(tmp_path):
    films = {"plugin": _film(tmp_path, "p", (200, 0, 0), 5), "stock": _film(tmp_path, "s", (0, 0, 200), 7)}
    assert [p.name for p in sc.frames(films["stock"], kind="story")] == [f"desktop_b{i:02d}.jpg" for i in range(7)]
    with pytest.raises(SystemExit):
        sc.frames(films["stock"])  # a story's folder holds no frames of a scrolling site
    asked = []

    def ask(parts):
        asked.append(parts)
        text = " ".join(p for kind, p in parts if kind == "text")
        assert "told beat by beat" in text and "one per beat in order" in text and "scroll" not in text
        assert "plugin" not in text and "stock" not in text
        assert sum(kind == "image" for kind, _ in parts) == 12
        return {"criteria": {c: {"winner": "1" if c == "makes_its_case" else "tie", "reason": "r"} for c in sc.STORY_CRITERIA},
                "overall": {"winner": "1", "reason": "r"}}

    res = sc.judge(films, "tell a story", tmp_path / "out", ask, repeats=1, kind="story")
    assert len(asked) == 2 and res["kind"] == "story" and res["criteria"] == list(sc.STORY_CRITERIA)
    slot = res["aggregate"]["plugin vs stock"]
    assert slot["votes"]["makes_its_case"] == {"plugin": 1, "stock": 1} and slot["result"]["words"] == "no majority"
    assert "makes its case" in sc.table(res) and "craft of the 3d" not in sc.table(res)
    assert set(sc.schema(sc.STORY_CRITERIA)["properties"]["criteria"]["required"]) == set(sc.STORY_CRITERIA)
    assert sc.SCHEMA == sc.schema(sc.CRITERIA), "a site's answer is what it was"


def test_a_call_is_sized_from_the_frames_it_shows():
    assert sc.call_tokens([9, 9]) == sc.TOKENS_PER_CALL, "two sites of nine frames: as before"
    assert sc.call_tokens([48, 30, 12]) == 78 * sc.TOKENS_PER_FRAME + 12_000, "the two longest stories meet in some call"
