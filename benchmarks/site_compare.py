#!/usr/bin/env python
"""Which of two built websites answers the request better: blind pairwise verdicts on their frames.

The site check (``antigravity3d.deliver.site_check``) measures what can be measured: errors, weight, frames in which
the 3D shows little, facts against their sources. Whether a page is composed at every scroll position, and whether
its 3D explains the project, is a judgement. As for the props since round 5 (``compare_eval.judge_all``), a general
model gives it blind: it sees the frames of two sites as "site 1" and "site 2" and never which run made them, every
pair is asked in both orders the same number of times (a judge that favours the first or the second cancels out), and
each verdict is kept with its reasons so that a person can disagree with it.

Input, per site: the folder of frames the site check wrote (``desktop_00.jpg`` ... at even scroll steps). The model
call goes through the comparison's relay and ledger (``compare_agents``), so it has a token share like every other
call of a run; ``--plan`` prints what would be asked and spends nothing.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import random
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

CRITERIA = ("every_position_composed", "explains_the_project", "craft_of_the_3d", "type_and_layout")
RUBRIC = """You are reviewing two websites built for the same request. For each site you get its frames in scroll order: the page photographed at even steps from the top of the scroll to the bottom, at a desktop window size. The frames are stills of a page whose 3D scene moves with the scroll.

The request: {request}

Judge only what the frames show. For each criterion answer "1" (the first site is better), "2" (the second site is better) or "tie", with one short sentence that names frames:
- every_position_composed: at which site is every scroll position a composed picture (the 3D subject in frame and not hidden by text, no frame that is empty or cut off, a sense of travel from one frame to the next)?
- explains_the_project: at which site does the 3D help a visitor understand how the project works, instead of decorating the text?
- craft_of_the_3d: at which site do the 3D objects look made and grounded (form, materials, light, shadow, depth)?
- type_and_layout: at which site are type, hierarchy and layout better, with text that is readable and leaves the 3D room?
Then give an overall winner ("1" or "2"): which site answers the request better, with one sentence. More text or more effects are not by themselves better."""
SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "criteria": {"type": "OBJECT", "properties": {c: {"type": "OBJECT", "properties": {
            "winner": {"type": "STRING", "enum": ["1", "2", "tie"]}, "reason": {"type": "STRING"}},
            "required": ["winner", "reason"]} for c in CRITERIA}, "required": list(CRITERIA)},
        "overall": {"type": "OBJECT", "properties": {"winner": {"type": "STRING", "enum": ["1", "2"]},
                                                     "reason": {"type": "STRING"}}, "required": ["winner", "reason"]},
    },
    "required": ["criteria", "overall"],
}
# What one call may cost: up to 2 x 9 frames at 960 px wide (the relay counts about 1,100 tokens an image), the rubric
# and 8,192 tokens of answer.
TOKENS_PER_CALL = 40_000
FRAME_WIDTH = 960


def frames(folder: Path, size: str = "desktop") -> list[Path]:
    """The site check's frames of one size, in scroll order (``desktop_00.jpg`` ...; not the canvas-only frames)."""
    found = sorted(p for p in Path(folder).glob(f"{size}_*.jpg") if re.fullmatch(rf"{size}_\d+\.jpg", p.name))
    if len(found) < 2:
        raise SystemExit(f"{folder} holds no {size}_NN.jpg frames of a site check")
    return found


def prepared(path: Path, width: int = FRAME_WIDTH) -> bytes:
    """One frame as the judge gets it: at most ``width`` px wide, JPEG."""
    from PIL import Image

    with Image.open(path) as im:
        im = im.convert("RGB")
        if im.width > width:
            im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=88)
    return buf.getvalue()


def digest(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for p in paths:
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


def plan(names: list[str], repeats: int = 3, seed: int = 20261124) -> list[dict[str, Any]]:
    """Every pair of sites, ``repeats`` times in each order; the call order shuffled with a recorded seed."""
    names = sorted(names)
    pairs = [(x, y) for i, x in enumerate(names) for y in names[i + 1:]]
    calls = [{"first": a, "second": b, "rep": r} for p in pairs for a, b in (p, p[::-1]) for r in range(repeats)]
    random.Random(seed).shuffle(calls)
    return calls


def aggregate(calls: list[dict[str, Any]]) -> dict[str, Any]:
    """Per pair and criterion: how many verdicts each site got, how many ties, and the site that has more than half of
    the verdicts cast (else "no majority")."""
    out: dict[str, Any] = {}
    for c in calls:
        v = c.get("verdict")
        if not v:
            continue
        pair = " vs ".join(sorted((c["first"], c["second"])))
        slot = out.setdefault(pair, {"votes": {k: Counter() for k in (*CRITERIA, "overall")}, "calls": 0})
        slot["calls"] += 1
        for k in (*CRITERIA, "overall"):
            w = (v["criteria"][k] if k != "overall" else v["overall"])["winner"]
            slot["votes"][k][c["first"] if w == "1" else c["second"] if w == "2" else "tie"] += 1
    for slot in out.values():
        slot["result"] = {}
        for k, votes in slot["votes"].items():
            cast = sum(votes.values())
            top, n = max(((name, n) for name, n in votes.items() if name != "tie"), key=lambda kv: kv[1], default=("", 0))
            slot["result"][k] = top if n * 2 > cast else "no majority"
            slot["votes"][k] = dict(sorted(votes.items()))
    return out


def judge(sites: dict[str, Path], request: str, out: Path, ask: Callable[[list[tuple[str, bytes | str]]], dict[str, Any]],
          repeats: int = 3, size: str = "desktop") -> dict[str, Any]:
    """Ask every planned call that has no kept answer for these very frames, and write ``out/judge.json``.
    ``ask`` takes the parts of one request, ("text", str) or ("image", jpeg bytes), and returns the verdict."""
    shots = {name: frames(folder, size) for name, folder in sites.items()}
    marks = {name: digest(paths) for name, paths in shots.items()}
    (out / "calls").mkdir(parents=True, exist_ok=True)
    done = []
    for c in plan(list(sites), repeats):
        f = out / "calls" / f"{c['first']}__{c['second']}__{c['rep']}.json"
        rec = {**c, "frames": [marks[c["first"]], marks[c["second"]]]}
        if f.exists():
            prev = json.loads(f.read_text())
            if prev.get("verdict") and prev.get("frames") == rec["frames"]:  # a verdict holds only for the same frames
                done.append(prev)
                continue
        parts: list[tuple[str, bytes | str]] = [("text", RUBRIC.format(request=request))]
        for label, name in (("first", c["first"]), ("second", c["second"])):
            parts.append(("text", f"The {label} site, {len(shots[name])} frames from the top of the scroll to the bottom:"))
            parts += [("image", prepared(p)) for p in shots[name]]
        try:
            rec["verdict"] = ask(parts)
        except Exception as e:  # noqa: BLE001 - a refused or failed call is a recorded outcome, asked again on a rerun
            rec["error"] = f"{type(e).__name__}: {e}"[:400]
        f.write_text(json.dumps(rec, indent=1))
        done.append(rec)
    res = {"criteria": list(CRITERIA), "size": size, "repeats_per_order": repeats, "sites": marks, "calls": done,
           "aggregate": aggregate(done)}
    (out / "judge.json").write_text(json.dumps(res, indent=1))
    return res


def live_ask(endpoint: str, model: str, thinking: str, max_output: int) -> Callable[[list[tuple[str, bytes | str]]], dict[str, Any]]:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key="via-relay", http_options=types.HttpOptions(
        base_url=endpoint, retry_options=types.HttpRetryOptions(attempts=1), timeout=600_000))
    cfg = types.GenerateContentConfig(response_mime_type="application/json", response_schema=SCHEMA, max_output_tokens=max_output,
                                      thinking_config=types.ThinkingConfig(thinking_level=thinking.upper()))

    def ask(parts: list[tuple[str, bytes | str]]) -> dict[str, Any]:
        contents = [types.Part.from_text(text=p) if kind == "text" else types.Part.from_bytes(data=p, mime_type="image/jpeg")
                    for kind, p in parts]
        return json.loads(client.models.generate_content(model=model, contents=contents, config=cfg).text)

    return ask


def table(res: dict[str, Any]) -> str:
    """The verdicts as a Markdown table: one row per pair and criterion."""
    rows = ["| pair | criterion | verdicts | result |", "|---|---|---|---|"]
    for pair, slot in res["aggregate"].items():
        for k in (*CRITERIA, "overall"):
            votes = ", ".join(f"{name} {n}" for name, n in slot["votes"][k].items())
            rows.append(f"| {pair} | {k.replace('_', ' ')} | {votes} | {slot['result'][k]} |")
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--site", action="append", required=True, metavar="NAME=FRAMES_DIR",
                   help="a site and the folder of its site-check frames; give two or more")
    p.add_argument("--request", type=Path, required=True, help="a text file: the request the sites were built for")
    p.add_argument("--out", type=Path, required=True, help="where the calls and judge.json are kept")
    p.add_argument("--work-root", type=Path, help="a folder of its own for the judge's ledger and call log (needed with --live)")
    p.add_argument("--repeats", type=int, default=3, help="verdicts per order of a pair (default 3: six per pair)")
    p.add_argument("--size", default="desktop", choices=("desktop", "phone"))
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true", help="ask the model (spends tokens; needs GEMINI_API_KEY)")
    mode.add_argument("--plan", action="store_true", help="print the calls and their token share; ask nothing")
    a = p.parse_args(argv)
    sites = {}
    for s in a.site:
        name, _, folder = s.partition("=")
        if not name or not folder or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise SystemExit(f"--site takes NAME=FRAMES_DIR with a plain name, got {s!r}")
        sites[name] = Path(folder)
    if len(sites) < 2:
        raise SystemExit("give at least two sites")
    calls = plan(list(sites), a.repeats)
    share = len(calls) * TOKENS_PER_CALL
    if a.plan:
        for name, folder in sites.items():
            print(f"{name}: {len(frames(folder, a.size))} frames")
        print(f"{len(calls)} calls, at most {share:,} tokens")
        return 0
    if not os.environ.get("GEMINI_API_KEY"):
        raise SystemExit("--live needs GEMINI_API_KEY in the environment")
    if a.work_root is None:
        raise SystemExit("--live needs --work-root (the ledger the calls are counted in)")
    import compare_agents as ca

    # its own ledger and ceiling: the judge's calls are counted apart from the runs it compares
    ca.BUDGET.update(ceiling_tokens=share + 100_000, ceiling_images=0, round="site-judge")
    relay, _ = ca.make_relay(a.work_root, live=True, allow_new_ceilings=True)
    acc = relay.open("site:judge", share, 0, a.work_root / "runs" / "site_judge", sandboxed=False)
    try:
        res = judge(sites, a.request.read_text().strip(), a.out, live_ask(acc.endpoint, ca.JUDGE_MODEL, ca.JUDGE_THINKING, ca.JUDGE_MAX_OUTPUT),
                    a.repeats, a.size)
    finally:
        relay.close("site:judge")
    res["model"], res["thinking"] = ca.JUDGE_MODEL, ca.JUDGE_THINKING
    (a.out / "judge.json").write_text(json.dumps(res, indent=1))
    print(table(res))
    failed = [c for c in res["calls"] if not c.get("verdict")]
    if failed:
        print(f"{len(failed)} call(s) have no verdict (run again to ask them): {failed[0].get('error')}")
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
