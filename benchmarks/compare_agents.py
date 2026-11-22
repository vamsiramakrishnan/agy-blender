#!/usr/bin/env python
"""Pilot: the same prop brief several ways, judged the same way.

    python benchmarks/compare_agents.py plan [--arms ...] [--run-images ARM=N ...]
    python benchmarks/compare_agents.py dry-run [--root DIR] [--only BRIEF:ARM ...] [--change BRIEF:ARM ...] [--skip-eval]
    python benchmarks/compare_agents.py live --go [--root DIR] [--only BRIEF:ARM ...] [--change BRIEF:ARM ...]
    python benchmarks/compare_agents.py score --root DIR      (the scorecard from what is on disk; spends nothing)

For a fresh stock/pipeline comparison, use ``--paired --round NAME --c-candidates single|default`` plus explicit
equal ``--run-tokens A=N C=N``, ``--run-images A=M C=M`` and total ceilings. ``plan --json`` prints the complete
contract without execution. Runs require a fresh explicit ``--root``; fake and live results cannot share a root.
The paired profile measures CLI MeteredAuthor, not the native AgentAuthor stage. Historical defaults stay intact.

A run whose ``runs/<brief>_<arm>/record.json`` exists is not run again: repeating a command re-evaluates, re-judges
(within the judge's remaining share) and rewrites the gallery without spending on runs twice.

Arms
  V  the stock Antigravity agent with a plain Blender MCP server (``compare_vanilla_mcp.py``): Python in one
     long-lived Blender session, the scene described, an image of it from a chosen view. The "vanilla MCP system".
     Blender is not on its PATH; the MCP server is its way to Blender. Inside the sandbox, like A.
  A  the stock Antigravity agent (SDK defaults: model, system prompt, tools), given the brief and a Blender it can
     script, inside the sandbox of ``compare_sandbox.py``. No plugin, no skills.
  B  the same agent and task text, with EL4CTEO/blender-skills (9e7e04a) copied into the workspace's
     ``.agents/skills`` the way that repository's installer does for a project (``--agent agents --project
     --copy``) and attached with the SDK's ``skills_paths`` (the headless harness does not discover
     ``.agents/skills`` by itself: measured, see ``tests/test_compare_agents.py``).
  N  the same stock agent with this project's plugin attached (``compare_sandbox.plugin_attachment``), driving it tool
     by tool: what a developer gets who installs the plugin in Antigravity.
  C  this project: ``agy3d prop "<brief>" --engine godot_4 --max-tris N --preview --blender $BLENDER_BIN``.

Change requests (``--change BRIEF:ARM``): after a cell has delivered, one follow-up in a new session over a copy of
the files it left (CHANGES), under its own account at half the arm's share; the two packages are then compared
(``compare_eval.package_diff``). Arm C's change is a setting (C_CHANGE_MODE): by default ``agy3d edit OUT "<change>"`` on a
copy of its first output folder (one patch to the shipped design, no image); "append" (the same command with the change
sentence appended, a new design) is kept for comparison.

Spend control, enforced in code (``Ledger`` and ``Relay``)
  Every model call of every arm and of the judge goes through one relay in this process: the sandboxed agents reach
  it over a unix socket, arm C and the judge over 127.0.0.1 with ``GOOGLE_GEMINI_BASE_URL``. The relay is the only
  holder of the API key. Before forwarding a call it reserves the call's worst case (input estimated from the request
  at 3 bytes per token, images at 1,800 tokens each, plus the request's own ``maxOutputTokens``) against the run's
  share and the global ceiling, and refuses the call (HTTP 400, never forwarded) if either would be exceeded. After
  the call the actual usage replaces the reservation; a call whose usage is unknown keeps its reservation booked.
  Shares add up to no more than the ceilings, so no run can take another's tokens. The tally is a JSON file under
  the pilot directory, rewritten after every change and reloaded on start, so a restarted pilot does not spend twice.
  No prices are read or set; tokens, images, wall time and tool calls are reported.

With one run per arm per brief every cell is one observation: nothing here is a rate or a ranking.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import datetime as dt
import hashlib
import http.server
import importlib.metadata
import json
import math
import os
import random
import re
import shutil
import signal
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from antigravity3d.safe_paths import copy_without_links, real_file_in

HERE = Path(__file__).resolve().parent
DEFAULT_TEXT_MODEL = "gemini-4-hydrogen"  # google.antigravity.models.DEFAULT_MODEL
REPO = HERE.parent
# Where runs, ledgers and evaluations are kept (never inside the package tree; ``out/`` is ignored by git).
DEFAULT_PILOT = Path(os.environ.get("AGY3D_PILOT_DIR", str(REPO / "out" / "agent_comparison")))
UPSTREAM = "https://generativelanguage.googleapis.com"
# A checkout of EL4CTEO/blender-skills at UPSKILLS_COMMIT (arm B installs its skills; the evaluation runs its scripts).
UPSKILLS = Path(os.environ.get("AGY3D_UPSTREAM_SKILLS", str(REPO.parent / "blender-skills")))
UPSKILLS_COMMIT = "9e7e04a603c125a0d17447ab97112fa70a865067"

# ----------------------------------------------------------------------------------------------- the design

ENGINE = "godot_4"
# The whole budget, in one place. Every number here can be changed from the command line (``--ceiling-tokens``,
# ``--ceiling-images``, ``--briefs``, ``--arms``, ``--run-tokens``, ``--run-images``); the plan is checked against the
# ceilings before anything starts, including the shares of arms that are planned but not run yet.
BUDGET: dict[str, Any] = {
    # Round 5 (advisor, 2026-11-22): the owner lifted the token ceiling ("I have no challenge with token count"), but
    # every cell still has a finite share so a runaway is stopped. 29 of the owner's 30 images are used, so the
    # ceiling is 0 images: V and A run now (a stock agent made no image in round 3), and the relay refuses any image
    # call. N and C need images for their textures and wait for the owner's allowance: add them with
    # ``--arms N C --run-images N=.. C=.. --ceiling-images ..`` (and a token ceiling that fits).
    "ceiling_tokens": 12_000_000,
    "ceiling_images": 0,
    "round": "r5",
    "briefs": ["crate", "hydrant", "chair"],
    "arms_now": ["V", "A"],  # the advisor's order: crate across V and A first; read; then hydrant and chair
    "arms_planned": ["V", "A"],
    "run_tokens": {"V": 1_200_000, "A": 1_200_000, "B": 195_000, "N": 1_500_000, "C": 500_000},
    "run_images": {"V": 0, "A": 0, "B": 0, "N": 0, "C": 0},
    "judge_tokens_per_call": 12_500,  # <= ~4k input (two 1024 px sheets + rubric) + 8,192 output
    "small_judge_tokens_per_call": 10_000,  # the game-size judge: two 512x256 sheets + rubric + 8,192 output
    "smoke_tokens": 0,
    "continuations": {},
    # One change request per planned cell (CHANGES), in a new session at this fraction of the arm's share.
    "changes": True,
    "change_share": 0.5,
    # None: the arm's run_images. N's change is a new plugin session (a new stage and cache: it re-textures) and C's a
    # new design (cached images are reused only where their request is unchanged), so both may need their run's images.
    "change_images": {"V": 0, "A": 0, "B": 0, "N": None, "C": None},
}
# Round 2's design, kept as the record of what produced the results in UPSTREAM_SKILLS.md and AFTER_THE_COMPARISON.md
# (cells named r2:<brief>:<arm>; arm C then was the command line, its native form was sized, not run).
ROUND2_BUDGET: dict[str, Any] = {
    # 2,000,000 tokens and 30 images approved; about 300,000 tokens and 18 images were spent by unintended test runs
    # and are counted against the pilot (advisor, 2026-11-21). Round 1 (A and B, crate and hydrant, 195,000 tokens
    # each) spent 540,857 and produced no model; its accounts are closed at what they used (``close``).
    "ceiling_tokens": 1_700_000,
    "ceiling_images": 12,
    "round": "r2",  # account, run, eval and judge names of this round: nothing mixes with the stopped round 1
    "briefs": ["crate"],
    "arms_now": ["A"],  # C waits for the preview path to be merged
    "arms_planned": ["A", "C"],  # the owner's question: stock against the plugin
    "run_tokens": {"A": 500_000, "B": 195_000, "C": 300_000},
    "run_images": {"A": 1, "B": 1, "C": 11},
    "judge_tokens_per_call": 12_500,  # <= ~4k input (two 1024 px sheets + rubric) + 8,192 output
    "smoke_tokens": 0,  # round 2 has no smoke run (round 1's proved the relay against the real API)
    # One continuation of arm A, as a person would handle "you ran out; carry on" (advisor, after A stopped one call
    # short of exporting): a fresh stock conversation in the same workspace, its own account and share.
    # Its image is the one the first session did not use: counted once (planned with the first session until that
    # account is closed at what it used, then with the continuation).
    "continuations": {"crate:A": {"tokens": 200_000, "images": 1, "session": 2},
                      # Arm C's second attempt after its first live run lost the geometry tournament to a refused token
                      # count (fixed in 7f33db1): the 3 images the ceiling has left force --single-candidate.
                      "crate:C": {"tokens": 225_000, "images": 3, "session": 2, "single_candidate": True,
                                  "why": "forced: the failed first attempt used 9 of the 12 images, and the default mode's "
                                         "plans need 8 or more"}},
}
# How a run's model calls grow, for the budget sentence (measured: round 1's 21 live calls; the native plugin arm: the
# happy-path plugin run against fakes with the preview tree, 24 agent turns). input = first + growth * k; reservation
# per call = input / ratio + the 16,384 output cap; ratio = measured actual/estimated input, lowest seen (0.70) for the
# conservative side. Arm A's growth was 2,500 per call for round 2's first session (told "about 14", it got 13).
# Measured there: 12,359 -> 59,032 over 12 calls, 4,200 per call on average, 11,000 after each script it wrote; 4,500
# from then on. Arm V has no live run yet: A's figures, its first call larger by the four MCP tool declarations (the
# dry run measures the first request; see test_vanilla_arm_*). Arm N is the native sizing (round 2's "C native").
CALL_MODEL = {"A": {"first": 12_400, "growth": 4_500, "out": 3_000}, "B": {"first": 14_700, "growth": 8_000, "out": 3_000},
              "V": {"first": 13_400, "growth": 4_500, "out": 3_000},
              "N": {"first": 16_400, "growth": 1_500, "out": 3_500, "plugin_calls_tokens": 26_000},
              "C": {"first": 16_400, "growth": 1_500, "out": 3_500, "plugin_calls_tokens": 26_000}}


def calls_allowed(arm: str, share: int | None = None) -> int:
    """Conservative call allowance under CALL_MODEL; advice to the agent, not an enforced call limit."""
    m, share = CALL_MODEL[arm], BUDGET["run_tokens"][arm] if share is None else share
    used, k = m.get("plugin_calls_tokens", 0), 0
    while True:
        inp = m["first"] + m["growth"] * k
        if used + inp / 0.70 + STOCK_MAX_OUTPUT > share:
            return k
        used += inp + m["out"]
        k += 1


def harness_budget(arm: str, share: int) -> dict[str, int]:
    """SDK backstops above the modeled call allowance. Cheap or tool-heavy calls can still reach them before the
    token ceiling; the recorded stop reason determines which limit actually bound a run."""
    n = calls_allowed(arm, share)
    return {"max_model_calls": max(STOCK_BUDGET["max_model_calls"], n + 10),
            "max_tool_calls": max(STOCK_BUDGET["max_tool_calls"], 3 * (n + 10))}


NOT_RUN = {"arm B": "not run in round 5: its two round-1 runs are reported as a finding (what it read, what it cost)"}
PENDING = {"N": "arm N (the stock agent driving this project's plugin) needs images for its textures; it waits for the "
                "owner's image allowance (29 of 30 used)",
           "C": "arm C (this project's command line, preview mode) needs images for its textures; it waits for the owner's "
                "image allowance (29 of 30 used)"}
JUDGE_MODEL = "gemini-4-carbon"
JUDGE_THINKING = "low"
JUDGE_MAX_OUTPUT = 8_192
STOCK_MAX_OUTPUT = 16_384  # the stock request asks for 65,535; see PLAN_NOTES
STOCK_BUDGET = {"max_model_calls": 40, "max_tool_calls": 120}  # floor of harness_budget; either backstop or quota may bind
STOCK_TIMEOUT_S = 40 * 60
# A share of 1.2M tokens is about 18 calls at round 2's growth, and a native plugin run makes 24+ calls with Blender
# renders between them: 40 minutes stopped nothing in round 2 (A's sessions took 6 and 10 minutes) and stays the floor.
ARM_TIMEOUT_S = {"V": 60 * 60, "A": 60 * 60, "B": 40 * 60, "N": 90 * 60}
C_TIMEOUT_S = 75 * 60
DEFAULT_OUT_TOKENS = 65_536  # reservation when a request names no maxOutputTokens
IMAGE_INPUT_TOKENS = 1_800  # reservation per inline image in a request
BYTES_PER_TOKEN = 3


@dataclass(frozen=True)
class Brief:
    id: str
    prompt: str
    max_tris: int
    size_m: tuple[float, float]  # plausible largest dimension, metres
    upright: bool  # tallest along the up axis
    why: str


BRIEFS = (
    Brief("crate", "a rugged military supply crate kit-bashed from panels, corner guards, latches and two side handles",
          3_000, (0.5, 1.6), False, "boxy, kit-bashed: many flat parts that must meet cleanly, hard edges, trim detail"),
    Brief("hydrant", "a cast-iron fire hydrant with bolted caps and a chained nozzle cap",
          4_000, (0.5, 1.2), True, "round parts: cylinders, caps, bolts; tessellation against a budget, smooth shading"),
    Brief("chair", "a wooden Windsor chair with a spindle back",
          5_000, (0.7, 1.3), True, "thin and many parts: legs, stretchers, a dozen spindles; proportions and budget"),
)
BRIEF_BY_ID = {b.id: b for b in BRIEFS}
ARMS = ("V", "A", "B", "N", "C")
ARM_LABEL = {"V": "stock agent + plain Blender MCP server", "A": "stock agent + scriptable Blender",
             "B": "stock agent + upstream skills", "N": "stock agent + this project's plugin",
             "C": "this project's pipeline (command line, preview mode)"}
STOCK_ARMS = ("V", "A", "B", "N")  # arms run as the stock agent in the sandbox
PLUGIN_ARMS = ("N",)  # arms with this project's plugin attached (compare_sandbox.plugin_attachment, src bound read-only)

# The change request each cell gets after its first delivery (brief.md item 4): one named part group made larger, the
# rest left alone. "ratio" is the size factor the request asks for, which package_diff looks for in the changed parts.
CHANGES = {"crate": {"text": "make the two side handles 30 % larger", "ratio": 1.3},
           "hydrant": {"text": "make the chained nozzle cap 30 % larger", "ratio": 1.3},
           "chair": {"text": "make the spindles of the back 30 % thicker", "ratio": 1.3}}
# Arm C's change path. "edit" (default): ``agy3d edit OUT "<change>"`` on a copy of the first run's whole output folder
# (dags.edit_asset: the shipped design, one patch, everything else kept, no image); live with the change account's
# --token-ceiling and --max-images 0; offline (a dry run) with a scripted patch (scripted_c_patch), since the offline
# author cannot read an instruction. "append": the same command with the change sentence appended to the prompt, into
# a copy of the first run's cache (the design record misses, so this is a new design; cached images whose request did
# not change are reused), kept to compare the two.
C_CHANGE_MODE = os.environ.get("AGY3D_PILOT_C_CHANGE", "edit")
if C_CHANGE_MODE not in ("edit", "append"):
    raise ValueError(f"AGY3D_PILOT_C_CHANGE must be 'edit' or 'append', not {C_CHANGE_MODE!r}")
# Words that name each brief's changed part group in a scene's node ids, for the dry run's scripted patch only.
C_CHANGE_PARTS = {"crate": ("handle",), "hydrant": ("nozzle", "cap"), "chair": ("spindle",)}


def order(briefs: list[str] | None = None, arms: list[str] | None = None) -> list[tuple[str, str]]:
    """Brief-major, so a ceiling reached late still leaves complete briefs; the arm order rotates per brief."""
    briefs = BUDGET["briefs"] if briefs is None else briefs
    arms = BUDGET["arms_now"] if arms is None else arms
    out = []
    for i, b in enumerate(briefs):
        out += [(b, arms[(i + k) % len(arms)]) for k in range(len(arms))]
    return out


def judge_calls_planned(briefs: list[str], arms: list[str]) -> int:
    n = len(arms)
    return len(briefs) * n * (n - 1)  # every pair, both orders


def change_share(arm: str) -> tuple[int, int]:
    """Tokens and images of one change-request session of ``arm``."""
    img = (BUDGET.get("change_images") or {}).get(arm, 0)
    return int(BUDGET["run_tokens"][arm] * BUDGET.get("change_share", 0.5)), int(BUDGET["run_images"][arm] if img is None else img)


def plan_totals() -> dict[str, int]:
    """Tokens and images the whole plan may use: runs, change requests and both judges for every planned arm,
    continuations, the smoke run. A continuation reuses its first session's image, so it adds none here."""
    acc = planned_accounts()
    cont = {continuation_name(*k.split(":"), c["session"]) for k, c in (BUDGET.get("continuations") or {}).items()}
    return {"tokens": sum(t for t, _ in acc.values()) + BUDGET["smoke_tokens"],
            "images": sum(i for n, (_, i) in acc.items() if n not in cont)}


def planned_accounts() -> dict[str, tuple[int, int]]:
    """Every ledger account the plan may open, with its share: cells, change requests, continuations, judge pairs
    (the full-size judge and the game-size judge)."""
    out = {cell_name(b, a): (BUDGET["run_tokens"][a], BUDGET["run_images"][a])
           for b, a in order(BUDGET["briefs"], BUDGET["arms_planned"])}
    if BUDGET.get("changes"):
        out |= {change_name(b, a): change_share(a) for b, a in order(BUDGET["briefs"], BUDGET["arms_planned"])}
    # the repeat asks for no new image: it should read every image from the first run's cache
    if not BUDGET.get("paired"):
        out |= {repeat_name(b, "C"): (change_share("C")[0], 0) for b in BUDGET["briefs"] if "C" in BUDGET["arms_planned"]}
    for key, c in (BUDGET.get("continuations") or {}).items():
        b, a = key.split(":")
        out[continuation_name(b, a, c["session"])] = (c["tokens"], c["images"])
    arms = sorted(BUDGET["arms_planned"])
    rnd = f"{BUDGET['round']}:" if BUDGET.get("round") else ""
    for b in BUDGET["briefs"]:
        for i, x in enumerate(arms):
            for y in arms[i + 1:]:
                out[f"{rnd}judge:{b}:{x}-{y}"] = (2 * BUDGET["judge_tokens_per_call"], 0)
                if BUDGET.get("small_judge_tokens_per_call"):
                    out[f"{rnd}judge_small:{b}:{x}-{y}"] = (2 * BUDGET["small_judge_tokens_per_call"], 0)
    return out


def change_name(brief: str, arm: str) -> str:
    return f"{cell_name(brief, arm)}.change"


def continuation_name(brief: str, arm: str, session: int) -> str:
    return f"{cell_name(brief, arm)}.s{session}"


def budget_sentence(calls: int) -> str:
    return (f"This run is limited to about {calls} model calls in total, counting every reply you make; plan so that a "
            "complete `output/<name>.glb` exists before then, and prefer delivering a simpler complete asset to an "
            "unfinished ambitious one.")


# How Blender is reached: the one sentence of the task that differs by arm (brief.md item 1).
BLENDER_SENTENCE = {
    "shell": "Blender 5.3 is installed and on PATH as `blender`; run it headless (for example `blender -b --factory-startup "
             "--python script.py`). There is no display and no network.",
    "mcp": "Blender 5.3 is running, and you reach it through the MCP server `blender`: it runs Python in that one "
           "long-lived Blender session, describes the scene and returns an image of it. There is no network.",
}


def task_text(b: Brief, calls: int | None = None, arm: str = "A") -> str:
    """The task, identical for every arm except the sentence that says how Blender is reached (arm V's MCP server);
    from round 2 it ends with the budget sentence (the arm's own call count)."""
    blender = BLENDER_SENTENCE["mcp" if arm == "V" else "shell"]
    if BUDGET.get("paired"):
        return (paired_brief(b) + "\n\nBlender 5.3 is installed on PATH as `blender`; run it headless. "
                "There is no display and no network. Deliver exactly one main output/<name>.glb and PNG textures "
                "in output/textures/. Optional LODs and collisions may be separate files. "
                "Reply with what you made and anything you could not do."
                + (f"\n\n{budget_sentence(calls)}" if calls else ""))
    return f"""Make a game-ready 3D prop: {b.prompt}.

Target engine: Godot 4 (import as glTF 2.0 binary).
{blender}

"Game-ready" here means all of the following, checked automatically afterwards:
- Real-world scale in metres (1 unit = 1 m). Up axis: glTF's +Y (Blender's glTF exporter converts Blender's +Z up for you). The prop stands on the ground: its lowest point at height 0, centred on the origin horizontally.
- At most {b.max_tris} triangles for the main model.
- Every mesh has a UV map; UV islands inside 0-1 and not overlapping.
- PBR metallic-roughness material(s), at most 3, with image textures: base colour, normal map, and roughness/metallic (an ORM or metallic-roughness texture). Texture sizes are powers of two, at most 2048 px.
- Optional: lower-detail LODs as separate files named <name>_LOD1.glb, <name>_LOD2.glb; a simple collision mesh as a separate object whose name ends in "-col".

Deliver into the folder `output/` of your working directory: exactly one main file `output/<name>.glb` (textures may be embedded) plus the texture images as PNG files in `output/textures/`. When you are done, reply with a short summary of what you made and anything you could not do.""" + (
        f"\n\n{budget_sentence(calls)}" if calls else "")


PLAN_NOTES = {
    "stock model": "SDK default text model gemini-4-hydrogen (google.antigravity.models.DEFAULT_MODEL), thinking left at "
                   "the model default; image tool on the SDK default gemini-3.1-flash-lite-image (0 images this round: "
                   "the relay refuses image calls).",
    "stock deviations": "web search and URL reading disabled (no network allowed); maxOutputTokens 16,384 instead of "
                        "the stock 65,535 so a call's worst case does not consume a third of the run's share; "
                        "file tools limited to the workspace; harness backstops exceed the modeled call allowance "
                        "but may bind before the token share (harness_budget).",
    "arm V": "the stock agent plus one MCP server, compare_vanilla_mcp.py (execute_blender_code, get_scene_info, "
             "get_object_info, get_viewport_screenshot), attached through LocalAgentConfig.mcp_servers; Blender runs "
             "inside the sandbox under Xvfb; nothing of this project is mounted.",
    "change requests": "one per cell after it delivers, a new session over a copy of its files, half the arm's share; "
                       "the two packages are diffed part by part (compare_eval.package_diff).",
}


def stock_execution_settings() -> dict[str, Any]:
    """The stock sandbox's configured SDK defaults and benchmark overrides, without starting an agent."""
    from google.antigravity import types
    from google.antigravity.models import DEFAULT_IMAGE_GENERATION_MODEL, DEFAULT_MODEL, GeminiModelOptions

    options = GeminiModelOptions(max_output_tokens=STOCK_MAX_OUTPUT).model_dump(mode="json")
    return {"text_model": DEFAULT_MODEL, "image_model": DEFAULT_IMAGE_GENERATION_MODEL,
            "thinking_level": options["thinking_level"], "thinking_policy": "SDK/model default when unset",
            "max_output_tokens": options["max_output_tokens"], "service_tier": options["service_tier"],
            "disabled_tools": [types.BuiltinTools.SEARCH_WEB.value, types.BuiltinTools.READ_URL_CONTENT.value],
            "workspace_only": True, "network": "isolated; model traffic through the relay",
            "project_plugin": False, "project_skills": False,
            "advisory_model_calls": calls_allowed("A"), "advisory_is_enforced": False,
            "hard_limits": harness_budget("A", BUDGET["run_tokens"]["A"]),
            "sdk_timeout_s": C_TIMEOUT_S, "outer_timeout_s": C_TIMEOUT_S}


def plan_notes() -> dict[str, str]:
    """Project only the selected experiment's notes; r5's images and changes do not describe a paired pilot."""
    if not BUDGET.get("paired"):
        return dict(PLAN_NOTES)
    stock = stock_execution_settings()
    limits = stock["hard_limits"]
    return {
        "stock model": f"SDK default text model {stock['text_model']}; thinking left at the SDK/model default. "
                       f"Image model {stock['image_model']}; {BUDGET['run_images']['A']} images allowed for A.",
        "stock deviations": f"maxOutputTokens {stock['max_output_tokens']:,}; web search and URL reading disabled; "
                            "workspace-only file tools; isolated network. "
                            f"The task advises about {stock['advisory_model_calls']} model calls; this is not a hard limit. "
                            f"SDK backstops: {limits['max_model_calls']} model calls / {limits['max_tool_calls']} tool calls. "
                            "Either a backstop or the token ceiling may bind first.",
        "pipeline": f"CLI MeteredAuthor, fixed {BUDGET['c_candidates']} candidate policy; "
                    "its per-call output/thinking settings differ from stock. This is a system comparison.",
        "additional sessions": "No continuations, change requests or repeats in this paired profile.",
        "judging": "Two full-size judge calls per brief; no separate game-size judge allocation.",
    }


def paired_brief(b: Brief) -> str:
    """Content requirements shared verbatim by stock and pipeline; execution instructions stay arm-specific."""
    return f"""Make a finished production game prop: {b.prompt}.
Target engine: Godot 4; final delivery is glTF 2.0 binary.
- Model each named component and its recognisable construction; add visible secondary forms where they serve the brief.
- Keep believable proportions and real-world scale: largest dimension {b.size_m[0]} to {b.size_m[1]} metres.
- At most {b.max_tris} triangles. This is a ceiling, not a quality score or a target to fill with repeated detail.
- Ground the prop and centre it horizontally; exported glTF uses +Y up (use the authoring system's own axis convention).
- UVs inside 0-1 without overlap; at most 3 PBR metallic-roughness materials, with base colour, normal and metallic-roughness textures.
- Texture sizes are powers of two, at most 2048 px. The complete delivered prop, including geometry and materials, is evaluated."""


PAIRED_LIMITATIONS = (
    "A is a stock agent scripting Blender; C is agy3d prop in preview mode. Their workflows differ. "
    "C.bind() uses MeteredAuthor: this comparison does not exercise AgentAuthor.propose_with_outcomes().",
    "Both arms receive the same content requirements and token/image ceilings. C keeps its 15,000-token internal "
    "admission margin; account caps, actual spend and refusals are reported separately. The shared 3-material "
    "requirement is checked on delivered files; the pipeline's internal engine budget still permits 4.",
    "C's candidate mode is fixed before execution. Its internal alternatives and repair loop differ from the stock "
    "agent's iterative workflow; this is an end-to-end system comparison, not an isolated prompt ablation.",
    "Stock uses a 16,384-token output cap with thinking unset; C's worker author uses a 24,576-token output cap "
    "with low thinking. These per-call settings differ. Stock's advisory call count is not enforced; its separate "
    "SDK model/tool backstops can bind before its aggregate token ceiling.",
    "No manual continuations or hand-completed packages are admitted. One session per arm per brief; a failed "
    "delivery remains a failure. Rerunning reuses its record rather than spending on a replacement.",
    "Both outputs receive identical file checks and renders, then the same blind rubric in both orders. "
    "The fallback judge is uncalibrated and related to the author models; human acceptance is not established.",
    "One run per arm per brief is a pilot. Dry-run model answers are scripted and establish harness behaviour, "
    "not geometry quality, model capability, production yield or a win rate.",
    "The paired pilot reserves two full-size judge calls only. Round 5's separate game-size judge, change "
    "requests and repeat runs remain available outside this one-session profile.",
)


def comparison_toolchain() -> dict[str, Any]:
    """Bind the configured execution/evaluation tools across separately executed cells and resumes."""
    from antigravity3d.adapters.engines.validators import GodotValidator
    from antigravity3d.adapters.engines.web_three import WebThreeValidator

    configured = {name: os.environ.get(name) for name in
                  ("BLENDER_BIN", "GODOT_BIN", "CHROMIUM_BIN", "AGY3D_THREE_JS", "AGY3D_BLENDER_SKILLS",
                   "PLAYWRIGHT_BROWSERS_PATH")}
    configured.update(resolved_godot=GodotValidator().binary(), resolved_chromium=WebThreeValidator().binary())
    selected: dict[str, Any] = {}
    for name, value in configured.items():
        if not value:
            selected[name] = None
            continue
        path = Path(value).expanduser().resolve()
        try:
            stat = path.stat()
            selected[name] = {"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                              "mode": stat.st_mode}
        except OSError:
            selected[name] = {"path": str(path), "missing": True}
    return selected


def require_paired_blender() -> None:
    """Both arms must use the same Blender; C alone must not silently omit its worker."""
    value = os.environ.get("BLENDER_BIN")
    path = Path(value).expanduser().resolve() if value else None
    # The stock sandbox mounts the executable's parent and invokes its `blender`; C invokes BLENDER_BIN directly.
    if (path is None or not path.is_file() or not os.access(path, os.X_OK)
            or (path.parent / "blender").resolve() != path):
        raise ValueError("paired execution needs BLENDER_BIN pointing to the executable 'blender' shared by A and C")
    try:
        result = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=15, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"paired Blender version preflight failed: {type(exc).__name__}") from exc
    version = re.search(r"^Blender (\d+)\.(\d+)", result.stdout, re.MULTILINE)
    if version is None or tuple(map(int, version.groups())) < (5, 3):
        raise ValueError("paired execution requires Blender 5.3 or newer for both A and C")


def comparison_contract() -> dict[str, Any]:
    """A reviewable paired plan, with exact prompts and local source hashes. No model calls or file writes."""
    files = sorted(p for p in PROJECT_SRC.rglob("*") if p.is_file() and p.suffix in {".py", ".json", ".yaml", ".yml"})
    hashes = {str(p.relative_to(PROJECT_SRC)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    harness = {name: hashlib.sha256((HERE / name).read_bytes()).hexdigest()
               for name in ("compare_agents.py", "compare_eval.py", "compare_sandbox.py")}
    revision = subprocess.run(["git", "-C", str(PROJECT_SRC.parent), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=10)
    versions = {}
    for package in ("google-antigravity", "google-genai"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    prompts = {b: {"content": paired_brief(BRIEF_BY_ID[b]),
                   "A": task_text(BRIEF_BY_ID[b], calls_allowed("A")), "C": paired_brief(BRIEF_BY_ID[b])}
               for b in BUDGET["briefs"]}
    return {"schema_version": 2, "profile": "paired", "budget": json.loads(json.dumps(BUDGET)),
            "planned_totals": plan_totals(), "order": order(), "prompts": prompts, "wall_limit_s_per_arm": C_TIMEOUT_S,
            "source": {"revision": revision.stdout.strip() if revision.returncode == 0 else None,
                       "project_src": str(PROJECT_SRC),
                       "source_sha256": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(),
                       "harness_sha256": harness, "sdk_versions": versions},
            "toolchain": comparison_toolchain(),
            "author_contracts": {"A": "stock SDK agent with recorded benchmark overrides", "C": "MeteredAuthor via CLI bind; production_prop",
                                 "native_AgentAuthor_tested": False},
            "stock_execution": stock_execution_settings(), "notes": plan_notes(),
            "judge": {"model": JUDGE_MODEL, "thinking": JUDGE_THINKING, "max_output_tokens": JUDGE_MAX_OUTPUT,
                      "both_orders": True, "order_seed": 20261121,
                      "view_size": "full", "game_size_calls": 0,
                      "calls": judge_calls_planned(BUDGET["briefs"], BUDGET["arms_planned"])},
            "limitations": list(PAIRED_LIMITATIONS), "approval_required_for_live": True}


def bind_comparison_contract(root: Path, *, live: bool, c_against_fake: bool = False,
                             use_blender: bool = True) -> dict[str, Any]:
    """Keep revisions, settings and fake/live provenance immutable under one output root."""
    contract = comparison_contract() | {"execution": {"mode": "live" if live else "dry-run",
                                                     "c_against_fake": c_against_fake, "use_blender": use_blender}}
    path = root / "comparison-contract.json"
    if path.exists():
        if json.loads(path.read_text()) != json.loads(json.dumps(contract)):
            raise ValueError("paired comparison source, plan or execution mode changed; use a fresh --root")
    else:
        if (root / "ledger.json").exists() or any((root / "runs").glob("*/record.json")):
            raise ValueError("paired comparison needs a fresh --root; existing records have no matching contract")
        if live or use_blender:
            require_paired_blender()
        root.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(contract, indent=2) + "\n")
    return contract


def paired_constraints(records: list[Any], evaluation: dict[str, Any]) -> dict[str, Any]:
    """Common numeric requirements, independent of either arm's own QA gates or the visual judge."""
    out = {}
    for rec in records:
        key = f"{rec.brief}:{rec.arm}"
        result = evaluation.get(key)
        if result is None:
            out[key] = {"status": "not evaluated"}
            continue
        facts = result.get("facts") or {}
        checks = {"delivered_unassisted": bool(result.get("glb_exists")) and rec.status == "finished"
                  and (rec.arm != "C" or rec.pipeline.get("exit_code") == 0),
                  "triangle_limit": isinstance(facts.get("triangles"), int)
                  and 0 < facts["triangles"] <= BRIEF_BY_ID[rec.brief].max_tris,
                  "material_limit": isinstance(facts.get("materials"), int) and 0 < facts["materials"] <= 3}
        out[key] = {"status": "passed" if all(checks.values()) else "failed", "checks": checks,
                    "scope": "delivery, triangle and material limits only; engine, scale, UV, PBR and visual checks are reported separately"}
    return out

# Measured 2026-11-21 against the local Gemini stand-in (benchmarks/compare_agents.py, tests/test_compare_agents.py
# test_the_headless_harness_discovers_the_plugin_nowhere): google-antigravity 0.1.24, its bundled localharness.
PLUGIN_DISCOVERY = {
    "sdk": "google-antigravity 0.1.24 (bundled localharness binary), LocalAgentConfig defaults",
    "tried": ["<workspace>/.agents/plugins/antigravity-3d (plugin_install.install, workspace scope, copy mode)",
              "<workspace>/.agents/plugins.json entry (plugin_install.register)",
              "$HOME/.gemini/config/plugins/antigravity-3d (global install, HOME passed to the harness)"],
    "request_carried": "in every case: no MCP server section and no MCP tools, no plugin skill (only the built-in "
                       "run-workflow skill), no plugin rule; system prompt about 11.6k characters, unchanged",
    "so": "arm C attaches each part by hand through its SDK equivalent, from the plugin's own files",
}

# Round 2's list (the record behind UPSTREAM_SKILLS.md); round 5's is UNFAIR below.
ROUND2_UNFAIR = (
    "Arm C is not an agent: it is this project's pipeline run from its command line in preview mode (the same kernel, "
    "gates, textures and exporter as a native plugin session; the plugin's own author writes the scene). Its judge is the "
    "generative fallback (uncalibrated: the native Decisions API answers 404 on this key), so nothing it makes is verified.",
    "Arm C is told less than A: its command line carries the brief, engine and triangle budget, but not the rest of the "
    "game-ready list (C_UNEXPRESSED); the pipeline applies its own conventions there.",
    "The stock agent driving the plugin tool by tool (the native arm) was sized, not run: against a stand-in API, an "
    "error-free scripted run took 24 agent calls and 7 plugin calls and about 734,000 to 887,000 tokens, of which the "
    "plugin's own model calls were about 26,000. A live run would most likely need a repair and more.",
    "Shares differ by arm: A 500,000 tokens and 1 image (an agent loop re-sends its whole context on every call), C 300,000 "
    "tokens and 11 images (its pipeline makes textures as images; its own ceilings sit just below the relay's).",
    "Arm A's task ends with a budget sentence (\"This run is limited to about N model calls ...\"), added in round 2 after "
    "round 1's four runs spent their 195,000-token shares on 4 to 8 calls of probing and reading and none began "
    "modelling: a stock agent is not told its budget otherwise. N is computed from its share (CALL_MODEL).",
    "Arm A runs without web search or URL reading (no network allowed), with maxOutputTokens 16,384 instead of the stock "
    "65,535 (so that one call's worst case does not hold a third of the share; each run's record counts the calls that "
    "ended at that cap), and without a display (Blender's Cycles works headless; EEVEE previews may not).",
    "Arm A has 1 image: a stock agent usually makes textures procedurally, but the cap is the owner's ceiling, not its need.",
    "The upstream qa_check / mesh_stats / texel_density scripts are by the author of the skills arm B used (round 1).",
    "The Godot import script and the three.js check are this project's own (C's authors); they are applied to the bare "
    "GLB of every arm with a manifest holding only the bounds read from that GLB.",
    "The judge is Gemini (gemini-4-carbon, thinking low) judging Gemini-made assets; renders are Blender's (Cycles), not "
    "the target engine's.",
    "Images: arm A had 1 and used none, so no image limit bound it. Arm C's first live attempt (default mode) failed "
    "before building a package, after generating 9 images; its second attempt had the 3 images left in the owner's "
    "ceiling and one candidate per choice (--single-candidate, forced, not chosen). A default-mode plugin run needs about "
    "9 to 11 images, which the remaining ceiling did not allow.",
    "Arm C's first live attempt failed on a defect the offline stand-in could not show: the preview path's token count "
    "for a judge call with a file-referenced image and a per-image resolution is refused by the real API (400), so every "
    "ranking call of the geometry tournament stopped; the material branch went on and generated 9 images for a package "
    "that could no longer be built (71,097 tokens, 9 images, no package). Fixed in 7f33db1 before the second attempt.",
    "The machine was shared: other test suites ran on its 4 cores during the live runs (1-minute load about 7 to 11); "
    "each run's record carries the load at its start and end. Wall times are not comparable with an idle machine.",
)

# Written before any result exists; the gallery and the report repeat them.
UNFAIR = (
    "Arm V's screenshots reach the model in two steps: the stock harness puts an MCP tool's image into the conversation "
    "only while it is tiny (base64 under about 10 to 16 KB, measured); a larger one, which is every useful render (a "
    "256 px solid view of a cube is 53 KB), is saved to a file and the path returned ('[Resource offloaded to "
    "file:///...media_0.png]'), and the agent sees the picture only if it opens that file with view_file "
    "(tests/test_compare_agents.py). That is the stock agent's behaviour with any MCP server and is left as it is; each "
    "V record counts screenshots taken and opened.",
    "Arm V's Blender has a virtual display (Xvfb) inside the sandbox, because its screenshot is Blender's solid-viewport "
    "renderer (Workbench), which needs OpenGL: without a display Blender 5.3 exits at the first render (measured). "
    "Arm A's Blender has none, as in round 3; Cycles works without one.",
    "Arm V's Blender binary is mounted in the sandbox (the MCP server starts it) but is not on PATH and not named in "
    "its task; an agent that finds it can still run it from the shell. Each record counts shell commands that start "
    "Blender (blender_invocations), so a V run that bypassed its MCP server is visible.",
    "Arms V and A run with 0 images: the owner's image allowance is used up (29 of 30), and the relay refuses an image "
    "call. A stock agent made no image in round 3 (its textures were procedural); the task text is unchanged.",
    "Arms N and C make their textures as images and are not run until the owner allows more images.",
    "Arm C is told less than the agents: its command line carries the brief, engine and triangle budget, but not the "
    "rest of the game-ready list (C_UNEXPRESSED); the pipeline applies its own conventions there.",
    "Arm C's change request is `agy3d edit OUT \"<change>\"` on a copy of its first output folder: one patch to the shipped "
    "design, no image (C_CHANGE_MODE; 'append', a new design from the sentence appended to the prompt, is kept for "
    "comparison). The agents' change sessions are new conversations over the files their first session left.",
    "Every arm's task ends with a budget sentence ('about N model calls'), N computed from its share (CALL_MODEL); "
    "maxOutputTokens is 16,384 instead of the stock 65,535; web search and URL reading are disabled (no network).",
    "The upstream qa_check / mesh_stats / texel_density scripts are by the author of the skills arm B used (round 1). "
    "The Godot import script and the three.js check are this project's own (C's authors); they are applied to the bare "
    "GLB of every arm with a manifest holding only the bounds read from that GLB.",
    "The judge is Gemini (gemini-4-carbon, thinking low) judging Gemini-made assets; renders are Blender's (Cycles), not "
    "the target engine's.",
    "Rows that only this project can pass are marked so in the scorecard (installed into a project, provenance, the "
    "same bytes from the same request): they are reported, not mixed into the shared game-ready rate.",
    "The machine is shared: other test suites may run on its 4 cores during live runs; each record carries the load at "
    "its start and end. Wall times are not comparable with an idle machine.",
)

# ----------------------------------------------------------------------------------------------- ledger


class LimitReached(Exception):
    pass


@dataclass
class Reservation:
    account: str
    tokens: int
    images: int
    id: int


class Ledger:
    """Token and image tally with per-account shares under global ceilings, persisted after every change."""

    def __init__(self, path: Path, ceiling_tokens: int | None = None, ceiling_images: int | None = None, *,
                 allow_new_ceilings: bool = False) -> None:
        ceiling_tokens = BUDGET["ceiling_tokens"] if ceiling_tokens is None else ceiling_tokens
        ceiling_images = BUDGET["ceiling_images"] if ceiling_images is None else ceiling_images
        self.path = Path(path)
        self.lock = threading.RLock()
        self._ids = 0
        self.inflight: dict[int, Reservation] = {}
        if self.path.exists():
            self.data = json.loads(self.path.read_text())
            if self.data["ceiling_tokens"] != ceiling_tokens or self.data["ceiling_images"] != ceiling_images:
                if not allow_new_ceilings:
                    raise ValueError(f"ledger {self.path} was opened with other ceilings "
                                     f"({self.data['ceiling_tokens']:,} tokens, {self.data['ceiling_images']} images)")
                self.data.setdefault("ceiling_history", []).append(
                    {"t": time.time(), "from": [self.data["ceiling_tokens"], self.data["ceiling_images"]],
                     "to": [ceiling_tokens, ceiling_images]})
                self.data["ceiling_tokens"], self.data["ceiling_images"] = ceiling_tokens, ceiling_images
            for acc in self.data["accounts"].values():  # a crash left reservations booked: they stay booked as unknown
                acc["unknown_reserved_tokens"] += acc.pop("inflight_tokens", 0)
                acc["images"] += acc.pop("inflight_images", 0)
                acc["inflight_tokens"] = acc["inflight_images"] = 0
        else:
            self.data = {"ceiling_tokens": ceiling_tokens, "ceiling_images": ceiling_images, "accounts": {},
                         "created": dt.datetime.now(dt.timezone.utc).isoformat()}
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        tmp.replace(self.path)

    def open_account(self, name: str, share_tokens: int, share_images: int) -> dict[str, Any]:
        with self.lock:
            accs = self.data["accounts"]
            if name in accs:
                if (accs[name]["share_tokens"], accs[name]["share_images"]) != (share_tokens, share_images):
                    raise ValueError(f"account {name} exists with another share")
                return accs[name]
            if sum(a["share_tokens"] for a in accs.values()) + share_tokens > self.data["ceiling_tokens"]:  # noqa: SIM102
                raise ValueError(f"shares would exceed the token ceiling with {name}")
            if sum(a["share_images"] for a in accs.values()) + share_images > self.data["ceiling_images"]:
                raise ValueError(f"shares would exceed the image ceiling with {name}")
            accs[name] = {"closed": False, "share_tokens": share_tokens, "share_images": share_images, "input_tokens": 0,
                          "output_tokens": 0, "thinking_tokens": 0, "cached_tokens": 0, "unknown_reserved_tokens": 0,
                          "images": 0, "calls": 0, "free_calls": 0, "refused": [], "inflight_tokens": 0,
                          "inflight_images": 0}
            self._save()
            return accs[name]

    @staticmethod
    def used_tokens(a: dict[str, Any]) -> int:
        return a["input_tokens"] + a["output_tokens"] + a["thinking_tokens"] + a["unknown_reserved_tokens"]

    def close_account(self, name: str, reason: str) -> dict[str, Any]:
        """Close a finished account at what it used: its share becomes its booked tokens and images (never less), so
        its unused share stops counting against the ceiling. Logged in the ledger with the before and after."""
        with self.lock:
            a = self.data["accounts"][name]
            if a["inflight_tokens"] or a["inflight_images"]:
                raise ValueError(f"{name} has a call in flight; close it after the call settles")
            before = (a["share_tokens"], a["share_images"])
            a["share_tokens"], a["share_images"] = max(self.used_tokens(a), 0), a["images"]
            a["closed"] = True
            rec = {"t": time.time(), "account": name, "share_before": before,
                   "share_after": (a["share_tokens"], a["share_images"]), "booked_tokens": self.used_tokens(a),
                   "reason": reason}
            self.data.setdefault("closures", []).append(rec)
            self._save()
            return rec

    def spent(self) -> bool:
        """Anything booked at all: tokens, images or calls (the guard against starting a live run twice)."""
        t = self.totals()
        return bool(t["tokens"] or t["images"] or t["calls"] or any(a["free_calls"] for a in self.data["accounts"].values()))

    def totals(self) -> dict[str, int]:
        with self.lock:
            return self.snapshot_totals(self.data)

    @staticmethod
    def snapshot_totals(data: dict[str, Any]) -> dict[str, int]:
        """Read a saved tally without reopening accounts, settling reservations or rewriting ceilings."""
        accs = data["accounts"].values()
        return {"tokens": sum(Ledger.used_tokens(a) for a in accs), "images": sum(a["images"] for a in accs),
                "input_tokens": sum(a["input_tokens"] for a in accs), "output_tokens": sum(a["output_tokens"] for a in accs),
                "thinking_tokens": sum(a["thinking_tokens"] for a in accs),
                "unknown_reserved_tokens": sum(a["unknown_reserved_tokens"] for a in accs),
                "calls": sum(a["calls"] for a in accs), "inflight_tokens": sum(a["inflight_tokens"] for a in accs)}

    def reserve(self, name: str, tokens: int, images: int, label: str = "") -> Reservation:
        with self.lock:
            a = self.data["accounts"][name]
            why = "account closed" if a.get("closed") else None
            if why:
                pass
            elif self.used_tokens(a) + a["inflight_tokens"] + tokens > a["share_tokens"]:
                why = (f"run share: {self.used_tokens(a)} used + {a['inflight_tokens']} in flight + {tokens} reserved "
                       f"> {a['share_tokens']}")
            elif a["images"] + a["inflight_images"] + images > a["share_images"]:
                why = f"run image share: {a['images']} + {a['inflight_images']} + {images} > {a['share_images']}"
            else:
                t = self.totals()
                if t["tokens"] + t["inflight_tokens"] + tokens > self.data["ceiling_tokens"]:
                    why = "global token ceiling"
                elif t["images"] + images + sum(x["inflight_images"] for x in self.data["accounts"].values()) > self.data["ceiling_images"]:
                    why = "global image ceiling"
            if why:
                a["refused"].append({"t": time.time(), "label": label, "why": why})
                self._save()
                raise LimitReached(why)
            self._ids += 1
            r = Reservation(name, tokens, images, self._ids)
            self.inflight[r.id] = r
            a["inflight_tokens"] += tokens
            a["inflight_images"] += images
            self._save()
            return r

    def _drop(self, r: Reservation) -> dict[str, Any]:
        a = self.data["accounts"][r.account]
        self.inflight.pop(r.id, None)
        a["inflight_tokens"] -= r.tokens
        a["inflight_images"] -= r.images
        return a

    def settle(self, r: Reservation, usage: dict[str, int] | None, images: int) -> None:
        """Book what the provider reported; ``usage=None`` (unknown) keeps the token reservation booked."""
        with self.lock:
            a = self._drop(r)
            a["calls"] += 1
            if usage is None:
                a["unknown_reserved_tokens"] += r.tokens
                a["images"] += max(images, r.images)
            else:
                a["input_tokens"] += usage.get("input", 0)
                a["output_tokens"] += usage.get("output", 0)
                a["thinking_tokens"] += usage.get("thinking", 0)
                a["cached_tokens"] += usage.get("cached", 0)
                a["images"] += images
            self._save()

    def release(self, r: Reservation, free: bool = True) -> None:
        """A request the provider rejected before generating (4xx) or a free call (countTokens): nothing billed."""
        with self.lock:
            a = self._drop(r)
            a["free_calls" if free else "calls"] += 1
            self._save()


# ----------------------------------------------------------------------------------------------- relay


def estimate_input_tokens(body: dict[str, Any]) -> tuple[int, int]:
    """(tokens, inline images) of a request body, conservatively: text bytes / 3, images at IMAGE_INPUT_TOKENS."""
    chars, images = 0, 0

    def walk(v: Any) -> None:
        nonlocal chars, images
        if isinstance(v, dict):
            inline = v.get("inlineData") or v.get("inline_data")
            if isinstance(inline, dict):
                images += 1
                return
            ref = v.get("fileData") or v.get("file_data")  # an uploaded file (Files API) costs its media tokens too
            if isinstance(ref, dict):
                images += 1
                chars += len(json.dumps(ref))
                return
            for k, x in v.items():
                chars += len(k)
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, str):
            chars += len(v.encode())
        else:
            chars += 8

    walk(body)
    return math.ceil(chars / BYTES_PER_TOKEN) + images * IMAGE_INPUT_TOKENS, images


def is_image_request(model: str, body: dict[str, Any]) -> bool:
    mods = [m.upper() for m in ((body.get("generationConfig") or {}).get("responseModalities") or [])]
    return "IMAGE" in mods or "image" in model or "banana" in model


def parse_response(payload: bytes, content_type: str = "") -> dict[str, Any]:
    """usage, finish reason, function calls, text and image count from a JSON or SSE generateContent reply."""
    text = payload.decode("utf-8", "replace")
    events: list[dict[str, Any]] = []
    if "event-stream" in content_type or text.lstrip().startswith("data:"):
        for line in text.splitlines():
            if line.startswith("data: "):
                with contextlib.suppress(json.JSONDecodeError):
                    events.append(json.loads(line[6:]))
    else:
        with contextlib.suppress(json.JSONDecodeError):
            doc = json.loads(text)
            events = doc if isinstance(doc, list) else [doc]
    usage: dict[str, Any] = {}
    finish, calls, texts, images = None, [], [], 0
    for ev in events:
        if not isinstance(ev, dict):
            continue
        usage = ev.get("usageMetadata") or usage
        for c in ev.get("candidates") or []:
            finish = c.get("finishReason") or finish
            for p in (c.get("content") or {}).get("parts") or []:
                if "functionCall" in p:
                    calls.append(p["functionCall"])
                elif "inlineData" in p and str(p["inlineData"].get("mimeType", "")).startswith("image/"):
                    images += 1
                elif "text" in p and not p.get("thought"):
                    texts.append(p["text"])
    booked = None
    if usage:
        inp = int(usage.get("promptTokenCount") or 0) + int(usage.get("toolUsePromptTokenCount") or 0)
        out, think = int(usage.get("candidatesTokenCount") or 0), int(usage.get("thoughtsTokenCount") or 0)
        total = int(usage.get("totalTokenCount") or 0)
        if total > inp + out + think:  # anything the provider totals but does not itemise is booked as input
            inp += total - (inp + out + think)
        booked = {"input": inp, "output": out, "thinking": think, "cached": int(usage.get("cachedContentTokenCount") or 0)}
    return {"usage": booked, "finish": finish, "function_calls": calls, "text": "".join(texts), "images": images}


def static_prompt(body: dict[str, Any]) -> dict[str, Any]:
    """Size of the request's fixed part and the skills it lists (what the agent was told it has)."""
    import re

    system = "".join(p.get("text", "") for p in (body.get("systemInstruction") or {}).get("parts", []) if isinstance(p, dict))
    decls = [d for t in body.get("tools") or [] for d in (t.get("functionDeclarations") or [])]
    m = re.search(r"<skills>(.*?)</skills>", system, re.S)
    return {"system_chars": len(system), "tool_decl_chars": sum(len(json.dumps(d)) for d in decls),
            "tool_names": sorted(d.get("name", "") for d in decls),
            "skills_listed": _listed_skills(m.group(1)) if m else []}


def _listed_skills(section: str) -> list[str]:
    import re

    names, on = [], False
    for line in section.splitlines():
        if line.strip().startswith("Available skills"):
            on = True
            continue
        if on:
            hit = re.match(r"^- (?:\*\*([^*]+)\*\*:|([\w.-]+) \()", line)  # "- **name**: ..." or "- name (path): ..."
            if hit:
                names.append(hit.group(1) or hit.group(2))
            elif line.strip() and not line.startswith(" "):
                break
    return names


SANDBOX_PATHS = (":streamGenerateContent", ":generateContent", ":countTokens")
# Paths per scope. "stock": a sandboxed stock agent (generation only). "plugin": the stock agent plus this project's
# plugin, whose MCP server also probes the Decisions API and uploads renders through the Files API. "trusted": this
# process's own callers (the judge) and the project's CLI.
GENERATE = re.compile(r"^/v1(beta|alpha)?/models/[A-Za-z0-9._-]+:(streamGenerateContent|generateContent|countTokens)$")
UPLOAD_START = re.compile(r"^/upload/v1(beta|alpha)?/files$")
FILES_GET = re.compile(r"^/v1(beta|alpha)?/files/[A-Za-z0-9._-]+$")
DECISIONS = re.compile(r"^/v1(beta|alpha)?/decisions(:[A-Za-z]+)?$")
RELAY_UPLOAD = re.compile(r"^/pilot-upload/([0-9a-f]{32})$")
PASS_REQ_HEADERS = ("content-type", "x-goog-upload-protocol", "x-goog-upload-command", "x-goog-upload-offset",
                    "x-goog-upload-header-content-length", "x-goog-upload-header-content-type")
DECISIONS_RESERVE = 4_096
# Statuses with which the service turns a call away before generating anything (measured tonight: 503 "overloaded").
TURNED_AWAY = (429, 503)


@dataclass
class Account:
    name: str
    scope: str  # stock | plugin | trusted
    log: Path
    transcript: Path
    endpoint: str = ""
    server: Any = None
    refused: list[str] = field(default_factory=list)
    calls: int = 0
    function_calls: list[dict[str, Any]] = field(default_factory=list)
    models: dict[str, dict[str, int]] = field(default_factory=dict)
    uploads: list[dict[str, Any]] = field(default_factory=list)

    @property
    def sandboxed(self) -> bool:
        return self.scope != "trusted"


class Relay:
    """Counts, limits and logs every model call; the only place the API key is used."""

    def __init__(self, ledger: Ledger, upstream: str, key: str | None, *, fake: bool) -> None:
        if fake and not upstream.startswith("http://127.0.0.1"):
            raise ValueError("a dry run must not reach a real API")
        if not fake and (not key or upstream.startswith("http://127.0.0.1")):
            raise ValueError("a live run needs the real API and a key")
        self.ledger, self.upstream, self._key, self.fake = ledger, upstream.rstrip("/"), key, fake
        self.accounts: dict[str, Account] = {}
        self.lock = threading.Lock()
        self._uploads: dict[str, tuple[str, str]] = {}  # relay id -> (account, upstream upload URL)

    def open(self, name: str, share_tokens: int, share_images: int, run_dir: Path, *, sandboxed: bool = True,
             unix_socket: Path | None = None, scope: str | None = None) -> Account:
        scope = scope or ("stock" if sandboxed else "trusted")
        if scope not in ("stock", "plugin", "trusted"):
            raise ValueError(f"unknown scope {scope!r}")
        self.ledger.open_account(name, share_tokens, share_images)
        run_dir.mkdir(parents=True, exist_ok=True)
        acc = Account(name, scope, run_dir / "relay_calls.jsonl", run_dir / "transcript.jsonl")
        relay = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _handle(self, method: str) -> None:
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                hdrs = {k.lower(): v for k, v in self.headers.items()}
                stream = _StreamOut(self)
                status, out, payload = relay.forward(acc, method, self.path, raw, hdrs, stream_out=stream)
                if stream.started:  # already sent as it arrived
                    stream.end()
                    return
                try:
                    self.send_response(status)
                    for k, v in out.items():
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):  # the client hung up: nothing left to tell it
                    self.close_connection = True

            def do_POST(self) -> None:  # http.server API
                self._handle("POST")

            def do_PUT(self) -> None:  # http.server API
                self._handle("PUT")

            def do_GET(self) -> None:  # http.server API
                self._handle("GET")

            def address_string(self) -> str:
                return "local"

            def log_message(self, *a: Any) -> None:
                pass

        if unix_socket is not None:
            class Srv(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
                daemon_threads = True

            unix_socket.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(FileNotFoundError):
                unix_socket.unlink()
            acc.server = Srv(str(unix_socket), Handler)
            os.chmod(unix_socket, 0o666)
            acc.endpoint = f"unix:{unix_socket}"
        else:
            acc.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            acc.endpoint = f"http://127.0.0.1:{acc.server.server_address[1]}"
        threading.Thread(target=acc.server.serve_forever, daemon=True).start()
        self.accounts[name] = acc
        return acc

    def close(self, name: str) -> None:
        acc = self.accounts.get(name)
        if acc and acc.server:
            acc.server.shutdown()
            acc.server.server_close()
            acc.server = None

    def _write(self, path: Path, rec: dict[str, Any]) -> None:
        with self.lock, path.open("a") as f:
            f.write(json.dumps(rec, default=str) + "\n")

    def _auth(self, acc: Account) -> dict[str, str]:
        return {"X-Pilot-Account": acc.name} if self.fake else {"x-goog-api-key": self._key or ""}

    def _send_streamed(self, url: str, method: str, raw: bytes | None, headers: dict[str, str],
                       out: "_StreamOut") -> tuple[int, Any, bytes]:
        """A streamed generateContent (``alt=sse``): a 200 is passed to the client chunk by chunk as it arrives (a relay
        that buffered it made every streamed call look silent to its client: live 2026-11-22, the command's arm stalled
        at its first-chunk limit with 0 chunks). The whole body is still collected, so it is metered as before. A client
        that hangs up stops being written to; the upstream body is still read to its end, because the service bills the
        call whether or not anyone reads it, and its usage is in the last event."""
        req = urllib.request.Request(url, data=raw, method=method, headers=headers)
        try:
            r = urllib.request.urlopen(req, timeout=600)
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()
        with r:
            if r.status != 200:
                return r.status, r.headers, r.read()
            out.start(r.status, {"Content-Type": r.headers.get("Content-Type", "text/event-stream")})
            parts = []
            while True:
                b = r.read1(65536)  # what has arrived, without waiting for the rest
                if not b:
                    break
                parts.append(b)
                out.write(b)
            return r.status, r.headers, b"".join(parts)

    def _send(self, url: str, method: str, raw: bytes | None, headers: dict[str, str]) -> tuple[int, Any, bytes]:
        req = urllib.request.Request(url, data=raw, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def _kind(self, acc: Account, method: str, clean: str) -> str | None:
        """What a request is, or None when this account may not make it."""
        if method == "POST" and GENERATE.match(clean):
            return "generate"
        if acc.scope in ("plugin", "trusted"):
            if method == "POST" and DECISIONS.match(clean):
                return "decisions"
            if method == "POST" and UPLOAD_START.match(clean):
                return "upload_start"
            if method in ("POST", "PUT") and RELAY_UPLOAD.match(clean):
                return "upload_data"
            if method == "GET" and FILES_GET.match(clean):
                return "file_get"
        if acc.scope == "trusted" and (clean.startswith(("/v1beta/", "/v1alpha/", "/v1/"))):
            return "generate" if method == "POST" else "file_get"
        return None

    def _files(self, acc: Account, kind: str, method: str, clean: str, raw: bytes, hdrs: dict[str, str],
               rec: dict[str, Any]) -> tuple[int, dict[str, str], bytes]:
        """Files API: no tokens; logged with byte sizes. The resumable upload URL is rewritten to this relay."""
        headers = {k: v for k, v in hdrs.items() if k in PASS_REQ_HEADERS} | self._auth(acc)
        if kind == "upload_data":
            rid = RELAY_UPLOAD.match(clean).group(1)
            owner, url = self._uploads.get(rid, ("", ""))
            if owner != acc.name:
                rec.update(status=404, refused="unknown upload id")
                self._write(acc.log, rec)
                return 404, {"Content-Type": "application/json"}, b'{"error": {"code": 404, "message": "pilot relay: unknown upload"}}'
        else:
            url = self.upstream + clean + ("?uploadType=resumable" if kind == "upload_start" else "")
        status, rh, payload = self._send(url, method, raw if method != "GET" else None, headers)
        out = {"Content-Type": rh.get("Content-Type", "application/json") if rh else "application/json"}
        for k in (rh.keys() if rh else []):
            if k.lower().startswith("x-goog-upload-") and k.lower() != "x-goog-upload-url":
                out[k] = rh[k]
        if kind == "upload_start" and rh and rh.get("x-goog-upload-url"):
            rid = os.urandom(16).hex()
            self._uploads[rid] = (acc.name, rh["x-goog-upload-url"])
            out["X-Goog-Upload-URL"] = f"http://{hdrs.get('host', '127.0.0.1')}/pilot-upload/{rid}"
        rec.update(status=status, kind=kind, request_bytes=len(raw), response_bytes=len(payload))
        acc.uploads.append({"kind": kind, "status": status, "bytes": len(raw)})
        with self.ledger.lock:
            a = self.ledger.data["accounts"][acc.name]
            a["free_calls"] += 1
            a.setdefault("upload_bytes", 0)
            a["upload_bytes"] += len(raw) if kind == "upload_data" else 0
            self.ledger._save()
        self._write(acc.log, rec)
        return status, out, payload

    def forward(self, acc: Account, method: str, path: str, raw: bytes,
                hdrs: dict[str, str] | None = None, stream_out: "_StreamOut | None" = None) -> tuple[int, dict[str, str], bytes]:
        """``stream_out``: where a streamed reply (``alt=sse``) is written as it arrives; when it has started, the reply
        is already with the client and the return value is only what was metered."""
        hdrs = hdrs or {}
        clean = path.split("?")[0]
        model = clean.rsplit("/", 1)[-1].split(":")[0] if "/models/" in clean else ""
        rec: dict[str, Any] = {"t": time.time(), "method": method, "path": clean, "model": model}
        js = {"Content-Type": "application/json"}

        def refuse(code: int, msg: str) -> tuple[int, dict[str, str], bytes]:
            rec.update(status=code, refused=msg)
            acc.refused.append(msg)
            self._write(acc.log, rec)
            return code, js, json.dumps({"error": {"code": code, "message": f"pilot relay: {msg}",
                                                   "status": "INVALID_ARGUMENT"}}).encode()

        kind = self._kind(acc, method, clean)
        if kind is None:
            return refuse(403, f"path not allowed for a {acc.scope} run: {method} {clean}")
        if kind in ("upload_start", "upload_data", "file_get"):
            return self._files(acc, kind, method, clean, raw, hdrs, rec)
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return refuse(400, "request body is not JSON")
        free = clean.endswith(":countTokens")
        est_in, inline_images = estimate_input_tokens(body)
        image_call = kind == "generate" and is_image_request(model, body)
        max_out = (body.get("generationConfig") or {}).get("maxOutputTokens")
        if free:
            tokens, images = 0, 0
        elif kind == "decisions":
            tokens, images = est_in + DECISIONS_RESERVE, 0
        else:
            tokens = est_in + int(max_out or (32_768 if image_call else DEFAULT_OUT_TOKENS))
            images = int((body.get("generationConfig") or {}).get("candidateCount") or 1) if image_call else 0
        rec.update(kind=kind, reserved_tokens=tokens, reserved_images=images, est_input_tokens=est_in,
                   max_output_tokens=max_out, inline_images=inline_images, **static_prompt(body))
        try:
            res = self.ledger.reserve(acc.name, tokens, images, label=f"{model or kind} {clean.rsplit(':', 1)[-1]}")
        except LimitReached as e:
            return refuse(400, f"spend limit: {e}")
        query = "?alt=sse" if "alt=sse" in path else ""
        t0 = time.monotonic()
        try:
            if stream_out is not None and query:
                status, rh, payload = self._send_streamed(self.upstream + clean + query, method, raw, js | self._auth(acc), stream_out)
                rec.update(streamed=stream_out.started, client_gone=stream_out.gone)
            else:
                status, rh, payload = self._send(self.upstream + clean + query, method, raw, js | self._auth(acc))
            ctype = rh.get("Content-Type", "application/json") if rh else "application/json"
        except Exception as e:  # noqa: BLE001 - unknown usage: the reservation stays booked
            if stream_out is not None and stream_out.started:  # broken mid-stream: the client sees it break, not end
                stream_out.gone = True
                stream_out.h.close_connection = True
            self.ledger.settle(res, None, 0)
            rec.update(status=502, error=f"{type(e).__name__}: {e}"[:300], tokens_known=False, wall_s=round(time.monotonic() - t0, 2),
                       service="broken")  # the connection broke: the service's failure, not the arm's
            self._write(acc.log, rec)
            return 502, js, json.dumps({"error": {"code": 502, "message": "pilot relay: upstream unreachable"}}).encode()
        rec["wall_s"] = round(time.monotonic() - t0, 2)
        rec["status"] = status
        parsed = parse_response(payload, ctype) if status == 200 else {"usage": None, "images": 0}
        if free:
            self.ledger.release(res, free=True)
        elif status == 200 and parsed["usage"] is not None:
            self.ledger.settle(res, parsed["usage"], parsed["images"])
        elif status in TURNED_AWAY:
            # The service turned the call away before generating (overloaded, rate-limited): nothing was billed, so
            # nothing is booked against the arm's share; logged as the service's doing.
            self.ledger.release(res, free=True)
            rec.update(error=payload.decode("utf-8", "replace")[:300], service="turned away")
        elif 400 <= status < 500:
            self.ledger.release(res, free=False)
            rec["error"] = payload.decode("utf-8", "replace")[:300]
        else:  # another server error, or a 200 without usage: what was billed is unknown, so the reservation stays booked
            self.ledger.settle(res, None, parsed.get("images", 0))
            rec["tokens_known"] = False
            if status >= 500:
                rec.update(error=payload.decode("utf-8", "replace")[:300], service="server error")
        if status == 200 and not free:
            u = parsed["usage"] or {}
            rec.update(usage=u, images=parsed["images"], finish=parsed["finish"],
                       function_calls=[{"name": c.get("name"), "args": _clip(c.get("args"))} for c in parsed["function_calls"]])
            m = acc.models.setdefault(model or kind, {"calls": 0, "input": 0, "output": 0, "thinking": 0})
            m["calls"] += 1
            for k in ("input", "output", "thinking"):
                m[k] += int(u.get(k, 0))
            acc.calls += 1
            acc.function_calls += rec["function_calls"]
            last = (body.get("contents") or [{}])[-1] if isinstance(body.get("contents"), list) else body.get("contents")
            self._write(acc.transcript, {"t": rec["t"], "model": model, "sent_last": _clip(last, 6000),
                                         "reply_text": parsed["text"][:20000], "function_calls": rec["function_calls"],
                                         "usage": u, "finish": parsed["finish"]})
        self._write(acc.log, rec)
        return status, {"Content-Type": ctype}, payload


class _StreamOut:
    """A reply written to the relay's client as it arrives (chunked transfer encoding). A client that hangs up is noted
    (``gone``) and written to no more; nothing raises into the relay."""

    def __init__(self, handler: Any) -> None:
        self.h, self.started, self.gone = handler, False, False

    def _put(self, data: bytes) -> None:
        if self.gone:
            return
        try:
            self.h.wfile.write(data)
            self.h.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.gone = True
            self.h.close_connection = True

    def start(self, status: int, headers: dict[str, str]) -> None:
        self.started = True
        try:
            self.h.send_response(status)
            for k, v in headers.items():
                self.h.send_header(k, v)
            self.h.send_header("Transfer-Encoding", "chunked")
            self.h.end_headers()
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.gone = True
            self.h.close_connection = True

    def write(self, data: bytes) -> None:
        if data:
            self._put(b"%x\r\n%s\r\n" % (len(data), data))

    def end(self) -> None:
        self._put(b"0\r\n\r\n")


def _clip(v: Any, n: int = 3000) -> Any:
    if isinstance(v, str):
        return v if len(v) <= n else v[:n] + f"...[+{len(v) - n}]"
    if isinstance(v, dict):
        return {k: (("<inline data>" if k in ("inlineData", "inline_data") else _clip(x, n))) for k, x in v.items()}
    if isinstance(v, list):
        return [_clip(x, n) for x in v]
    return v


# ----------------------------------------------------------------------------------------------- fake upstream (dry run)


def _make_glb_script() -> str:
    """Pure-Python writer of a small textured box GLB (used by the scripted agent when no Blender is wanted)."""
    return r'''
import json, struct, zlib, os
def png(w, h, rgb):
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    def chunk(t, d): return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
s = 0.4
P = [(-s,0,-s),(s,0,-s),(s,0,s),(-s,0,s),(-s,2*s,-s),(s,2*s,-s),(s,2*s,s),(-s,2*s,s)]
F = [(0,1,2,3,(0,-1,0)),(4,7,6,5,(0,1,0)),(0,4,5,1,(0,0,-1)),(1,5,6,2,(1,0,0)),(2,6,7,3,(0,0,1)),(3,7,4,0,(-1,0,0))]
pos, nor, uv, idx = [], [], [], []
for f, (a,b,c,d,n) in enumerate(F):
    base = len(pos); u0 = (f % 3) / 3; v0 = (f // 3) / 2
    for k, v in enumerate((a,b,c,d)):
        pos.append(P[v]); nor.append(n); uv.append((u0 + (0.3 if k in (1,2) else 0.03), v0 + (0.45 if k in (2,3) else 0.03)))
    idx += [base, base+2, base+1, base, base+3, base+2]
bin_ = b"".join(struct.pack("<3f", *p) for p in pos) + b"".join(struct.pack("<3f", *n) for n in nor) + b"".join(struct.pack("<2f", *t) for t in uv) + b"".join(struct.pack("<H", i) for i in idx)
while len(bin_) % 4: bin_ += b"\0"
img = png(64, 64, (150, 110, 70)); off = len(bin_); bin_ += img
while len(bin_) % 4: bin_ += b"\0"
nv = len(pos)
doc = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"name": "Crate", "mesh": 0}],
 "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "NORMAL": 1, "TEXCOORD_0": 2}, "indices": 3, "material": 0}]}],
 "materials": [{"name": "crate", "pbrMetallicRoughness": {"baseColorTexture": {"index": 0}, "metallicFactor": 0.0, "roughnessFactor": 0.8}}],
 "textures": [{"source": 0}], "images": [{"bufferView": 4, "mimeType": "image/png"}],
 "buffers": [{"byteLength": len(bin_)}],
 "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": nv*12}, {"buffer": 0, "byteOffset": nv*12, "byteLength": nv*12},
   {"buffer": 0, "byteOffset": nv*24, "byteLength": nv*8}, {"buffer": 0, "byteOffset": nv*32, "byteLength": len(idx)*2}, {"buffer": 0, "byteOffset": off, "byteLength": len(img)}],
 "accessors": [{"bufferView": 0, "componentType": 5126, "count": nv, "type": "VEC3", "min": [-s,0,-s], "max": [s,2*s,s]},
   {"bufferView": 1, "componentType": 5126, "count": nv, "type": "VEC3"}, {"bufferView": 2, "componentType": 5126, "count": nv, "type": "VEC2"},
   {"bufferView": 3, "componentType": 5123, "count": len(idx), "type": "SCALAR"}]}
js = json.dumps(doc).encode()
while len(js) % 4: js += b" "
os.makedirs("output/textures", exist_ok=True)
open("output/textures/crate_basecolor.png", "wb").write(img)
with open("output/crate.glb", "wb") as f:
    f.write(struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(js) + 8 + len(bin_)))
    f.write(struct.pack("<II", len(js), 0x4E4F534A) + js + struct.pack("<II", len(bin_), 0x004E4942) + bin_)
print("wrote output/crate.glb")
'''


BLENDER_ASSET_SCRIPT = r'''
import bpy, os
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add(size=0.8, location=(0, 0, 0.4))
ob = bpy.context.active_object; ob.name = "Crate"
bpy.ops.object.mode_set(mode="EDIT"); bpy.ops.uv.smart_project(); bpy.ops.object.mode_set(mode="OBJECT")
img = bpy.data.images.new("crate_basecolor", 256, 256); img.generated_color = (0.5, 0.35, 0.2, 1)
os.makedirs("output/textures", exist_ok=True)
img.filepath_raw = os.path.abspath("output/textures/crate_basecolor.png"); img.file_format = "PNG"; img.save()
mat = bpy.data.materials.new("crate"); mat.use_nodes = True
tex = mat.node_tree.nodes.new("ShaderNodeTexImage"); tex.image = img
bsdf = mat.node_tree.nodes["Principled BSDF"]; mat.node_tree.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
ob.data.materials.append(mat)
bpy.ops.export_scene.gltf(filepath=os.path.abspath("output/crate.glb"), export_format="GLB")
print("EXPORTED")
'''

# One shell line that tries every way out the sandbox is meant to close; each probe prints what happened. Paths are
# assembled at run time so the policy's string test (discouragement only) does not see them: this measures the
# namespaces, not the string test.
PROBE_COMMAND = r"""python3 - <<'EOF'
import os, socket, json
J = "".join
r = {}
def tryit(name, fn):
    try: r[name] = "OPEN: " + str(fn())[:80]
    except Exception as e: r[name] = "closed: " + type(e).__name__
H = J(["/ho", "me/us", "er"])
tryit("repo_readme", lambda: open(H + J(["/agy-", "blender/README.md"])).read(10))
tryit("upstream_clone", lambda: os.listdir(H + J(["/el4", "cteo"])))
tryit("root_home", lambda: os.listdir(J(["/ro", "ot"])))
tryit("gitconfig", lambda: open(J(["/ro", "ot/.gitconfig"])).read(10))
tryit("worktrees", lambda: os.listdir(J(["/tmp/cla", "ude-0"])))
tryit("pid1_environ", lambda: [k for k in open(J(["/proc/", "1/environ"]), "rb").read().split(b"\0") if b"KEY" in k or b"TOKEN" in k])
tryit("host_procs", lambda: len([p for p in os.listdir("/proc") if p.isdigit()]))
tryit("plugin_bundle", lambda: os.listdir("/opt/pydeps/antigravity3d"))
tryit("mnt", lambda: os.listdir("/mnt"))
tryit("net_public", lambda: socket.create_connection(("8.8.8.8", 53), timeout=3))
tryit("net_dns", lambda: socket.getaddrinfo("generativelanguage.googleapis.com", 443))
tryit("write_pydeps", lambda: open("/opt/pydeps/x", "w"))
tryit("write_blender", lambda: open("/opt/blender/x", "w"))
def remount():
    if os.system("umount /home 2>/dev/null") != 0: raise PermissionError("umount refused")
    return "unmounted"
tryit("remount", remount)
r["secret_env"] = sorted(k for k in os.environ if any(s in k for s in ("KEY", "TOKEN", "SECRET", "PROXY", "AWS", "GH_")))
r["uid"] = os.getuid()
print("PROBES " + json.dumps(r))
EOF"""


def scripted_agent(use_blender: bool) -> list[dict[str, Any]]:
    make = (f"cat > make_asset.py <<'EOF'\n{BLENDER_ASSET_SCRIPT}\nEOF\nblender -b --factory-startup --python make_asset.py 2>&1 | tail -3"
            if use_blender else f"cat > make_glb.py <<'EOF'\n{_make_glb_script()}\nEOF\npython3 make_glb.py")
    return [_fc("run_command", CommandLine=PROBE_COMMAND, Cwd="/home/agent/project"),
            _fc("run_command", CommandLine=make, Cwd="/home/agent/project"),
            {"parts": [{"text": "Made output/crate.glb: a textured box crate (scripted dry-run agent)."}]}]


def _tiny_png() -> bytes:
    import struct
    import zlib

    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\x80\x80\x80" * 4 for _ in range(4))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _response_schema(body: dict[str, Any]) -> dict[str, Any] | None:
    gc = body.get("generationConfig") or {}
    return gc.get("responseSchema") or gc.get("responseJsonSchema") or gc.get("response_json_schema")


def _scene_answer(body: dict[str, Any]) -> dict[str, Any]:
    """The stand-in author: the project's own TemplateAuthor scene for the brief in an authoring request."""
    import importlib

    authoring = importlib.import_module("antigravity3d.tactics.authoring")
    state = importlib.import_module("antigravity3d.state")
    text = " ".join(p.get("text", "") for c in body.get("contents", []) for p in c.get("parts", []) if isinstance(p, dict))
    name = (re.search(r"asset_name='([^']+)'", text) or re.search(r"asset name is (\w+)", text))
    prompt = re.search(r"Build design \d+/\d+ for (.*?)\. (?:Use the attached|Return one JSON)", text, re.S)
    scene = asyncio.run(authoring.TemplateAuthor().propose(prompt.group(1) if prompt else "crate", budget=state.EngineBudget("godot_4"),
                                                           n=1, context={"asset_name": name.group(1) if name else "Asset"}))[0]
    return scene.model_dump(mode="json", exclude_defaults=True)


def _question_schema(sch: dict[str, Any]) -> bool:
    """A judge's question-keyed schema: every property a probability or a distribution over named options."""
    props = sch.get("properties") or {}
    def num(x: Any) -> bool:
        return isinstance(x, dict) and str(x.get("type", "")).lower() == "number"
    return bool(props) and all(num(v) or (isinstance(v, dict) and (v.get("properties") or {}) and
                                          all(num(w) for w in v["properties"].values())) for v in props.values())


def _question_answer(sch: dict[str, Any]) -> dict[str, Any]:
    """A confident, approving stand-in judge: low defect and policy probabilities, 0.9 on the preferred option."""
    out: dict[str, Any] = {}
    for qid, q in (sch.get("properties") or {}).items():
        base = re.sub(r"__\d+$", "", qid)
        if str(q.get("type", "")).lower() == "number":
            out[qid] = 0.02 if base.startswith("policy_") else 0.05 if base.startswith("defect_") else \
                0.8 if base == "needs_ortho_blueprint" else 0.5
            continue
        keys = list(q["properties"])
        pref = {"pipeline_dag": "prop_to_engine", "next_action": "approve_export", "complexity_tier": "1"}.get(base)
        if pref not in keys:
            pref = keys[-1] if all(k.isdigit() for k in keys) else keys[0]
        rest = (1 - 0.9) / max(1, len(keys) - 1)
        out[qid] = {k: (0.9 if k == pref else rest) for k in keys}
    return out


def _noise_png(size: int) -> bytes:
    import io

    import numpy as np
    from PIL import Image

    rng = np.random.default_rng(size)
    a = (rng.random((size // 8, size // 8, 3)) * 80 + 90).astype("uint8")
    buf = io.BytesIO()
    Image.fromarray(a).resize((size, size)).save(buf, "PNG")
    return buf.getvalue()


def _schema_instance(sch: Any, depth: int = 0) -> Any:
    """A deterministic value that satisfies a (Gemini or JSON) schema: what a stand-in answers to a structured call."""
    if not isinstance(sch, dict) or depth > 12:
        return None
    if "enum" in sch and sch["enum"]:
        return sch["enum"][0]
    for k in ("anyOf", "oneOf"):
        if sch.get(k):
            return _schema_instance(sch[k][0], depth + 1)
    t = str(sch.get("type", "object")).lower()
    if isinstance(sch.get("type"), list):
        t = next((str(x).lower() for x in sch["type"] if x != "null"), "string")
    if t == "object":
        props = sch.get("properties") or {}
        return {k: _schema_instance(v, depth + 1) for k, v in props.items()}
    if t == "array":
        n = max(1, int(sch.get("minItems") or 1))
        return [_schema_instance(sch.get("items") or {}, depth + 1) for _ in range(n)]
    if t in ("number", "integer"):
        lo, hi = sch.get("minimum", 0), sch.get("maximum", 1)
        v = (lo + hi) / 2 if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) else 0.5
        return int(v) if t == "integer" else v
    if t == "boolean":
        return True
    return "stand-in"


def scripted_plugin_agent() -> list[dict[str, Any]]:
    """The scripted model for arm C in a dry run: the plugin's session workflow, a probe of the MCP server's network,
    then hand-off of the exported GLB (if export was allowed) into output/."""
    import importlib

    ir = importlib.import_module("antigravity3d.ir")
    authoring = importlib.import_module("antigravity3d.tactics.authoring")
    state = importlib.import_module("antigravity3d.state")
    scene = asyncio.run(authoring.TemplateAuthor().propose("wooden crate", budget=state.EngineBudget("godot_4"), n=1,
                                                           context={"asset_name": "Crate"}))[0]
    ops = [ir.UpsertMaterial(material=m).model_dump(mode="json") for m in scene.materials] + \
          [ir.UpsertNode(node=n).model_dump(mode="json") for n in scene.nodes]

    def mcp(tool: str, **args: Any) -> dict[str, Any]:
        return _fc("call_mcp_tool", ServerName="antigravity-3d_studio", ToolName=tool, Arguments=args,
                   toolSummary="Studio call", toolAction="Calling studio")

    return [_fc("run_command", CommandLine=PROBE_COMMAND + "\n" + PLUGIN_PROBE_COMMAND, Cwd="/home/agent/project"),
            mcp("open_asset_session", asset_name="Crate", target_engine="godot_4", max_tris=3000,
                conversation_id="{conversation_id}"),
            mcp("set_stage", stage="BLOCKOUT"), mcp("apply_scene_ir", ops=ops), mcp("run_visual_qa"),
            mcp("set_stage", stage="LOOKDEV"), mcp("render_lookdev", engine="eevee"), mcp("run_visual_qa"),
            mcp("set_stage", stage="EXPORT"), mcp("generate_lods_and_colliders"), mcp("run_usd_validator"),
            mcp("export_engine_package", out_name="crate", session_id="{session_id}"),
            # the newest package's GLB: a change session's workspace also holds the first session's package
            _fc("run_command", CommandLine="mkdir -p output && f=$(ls -t $(find agy3d_assets -name '*.glb' ! -name '*_LOD*' "
                                           "! -name '*_colliders*') | head -1); [ -n \"$f\" ] && cp \"$f\" output/crate.glb; ls output",
                Cwd="/home/agent/project"),
            {"parts": [{"text": "Scripted plugin-arm dry run finished."}]}]


def scripted_continuation() -> list[dict[str, Any]]:
    """The scripted continuation for a dry run: look at the workspace, run the earlier session's own export script."""
    return [_fc("run_command", CommandLine="ls -R . | head -50", Cwd="/home/agent/project"),
            _fc("run_command", CommandLine="sleep 15; blender -b build/crate_base.blend --python export_glb.py 2>&1 | tail -5; ls -R output",
                Cwd="/home/agent/project"),
            {"parts": [{"text": "Waiting for the export to finish."}]},  # the turn ends; the harness wakes it later
            {"parts": [{"text": "Continued: exported output/ from the earlier session's scene (scripted dry run)."}]}]


def scripted_plugin_happy_path() -> list[dict[str, Any]]:
    """A complete, successful plugin run as a careful model would make it, for sizing arm C's share: read the three
    skills it needs and each MCP tool's schema file before first use (as the harness's MCP section instructs), open a
    preview session, author a kit-bashed crate of about 36 parts, repair once, two textured materials, lookdev QA, LODs,
    validation, export, hand-off. Every call is one model turn; the measured quantity is each turn's input."""
    import importlib

    ir = importlib.import_module("antigravity3d.ir")
    authoring = importlib.import_module("antigravity3d.tactics.authoring")
    state = importlib.import_module("antigravity3d.state")
    scenes = asyncio.run(authoring.TemplateAuthor().propose("wooden crate", budget=state.EngineBudget("godot_4"), n=3,
                                                            context={"asset_name": "Crate"}))
    ops = [ir.UpsertMaterial(material=m).model_dump(mode="json") for m in scenes[0].materials]
    for i, sc in enumerate(scenes):  # three variants side by side: about 36 parts, a kit-bashed crate's order of size
        for n in sc.nodes:
            d = n.model_dump(mode="json")
            d["id"] = f"{d['id']}_{i}"
            if d.get("parent"):
                d["parent"] = f"{d['parent']}_{i}"
            ops.append({"op": "upsert_node", "node": d})
    skills = "/home/agent/project/.agents/plugins/antigravity-3d/skills"
    schema = "/home/agent/.antigravity/brain/{conversation_id}/mcp/antigravity-3d_studio"

    def mcp(tool: str, **args: Any) -> dict[str, Any]:
        return _fc("call_mcp_tool", ServerName="antigravity-3d_studio", ToolName=tool, Arguments=args,
                   toolSummary="Studio call", toolAction="Calling studio")

    def view(path: str) -> dict[str, Any]:
        return _fc("view_file", AbsolutePath=path, toolSummary="Reading", toolAction="Reading file")

    mats = [m.id for m in scenes[0].materials]
    turns = [view(f"{skills}/parametric-kitbash/SKILL.md"), view(f"{skills}/pbr-material-forge/SKILL.md"),
             view(f"{skills}/engine-truth-verify/SKILL.md"), view(f"{schema}/open_asset_session.json"),
             mcp("open_asset_session", asset_name="Crate", target_engine="godot_4", max_tris=3000,
                 conversation_id="{conversation_id}"),
             mcp("set_stage", stage="BLOCKOUT"), view(f"{schema}/apply_scene_ir.json"), mcp("apply_scene_ir", ops=ops),
             mcp("render_preview"), mcp("run_visual_qa"),
             mcp("apply_scene_ir", ops=[{"op": "set_param", "node_id": "body_0", "name": "size", "value": [0.9, 0.8, 0.7]}]),
             mcp("run_visual_qa"), mcp("set_stage", stage="LOOKDEV"), view(f"{schema}/forge_pbr_material.json"),
             mcp("forge_pbr_material", material_id=mats[0], prompt="weathered olive-drab painted plywood"),
             mcp("forge_pbr_material", material_id=mats[-1], prompt="scuffed black steel"),
             mcp("render_lookdev", engine="eevee"), mcp("run_visual_qa"), mcp("set_stage", stage="EXPORT"),
             mcp("generate_lods_and_colliders"), mcp("run_usd_validator"),
             mcp("export_engine_package", out_name="crate", session_id="{session_id}"),
             _fc("run_command", CommandLine="mkdir -p output/textures && f=$(find agy3d_assets -name '*.glb' ! -name '*_LOD*' "
                                            "! -name '*_colliders*' | head -1); cp \"$f\" output/crate.glb; "
                                            "cp $(dirname \"$f\")/textures/*.png output/textures/ 2>/dev/null; ls -R output",
                 Cwd="/home/agent/project"),
             {"parts": [{"text": "Delivered output/crate.glb: a kit-bashed military crate, preview (unverified) package."}]}]
    return turns


# Arm V's probe: Blender is not on PATH, the MCP server and its Blender run inside the sandbox's network namespace, and
# neither has a key in its environment.
VANILLA_PROBE_COMMAND = r"""python3 - <<'EOF'
import os, json, shutil
r = {"blender_on_path": shutil.which("blender")}
me = os.readlink("/proc/self/ns/net")
def procs(needle):
    out = []
    for p in os.listdir("/proc"):
        if p.isdigit():
            try:
                if needle in open(f"/proc/{p}/cmdline", "rb").read():
                    out.append(p)
            except OSError:
                pass
    return out
for name, needle in (("mcp_server", b"compare_vanilla_mcp.py\0serve"), ("blender", b"\0bridge\0")):
    ps = procs(needle)
    r[name + "_found"] = bool(ps)
    if ps:
        p = ps[0]
        r[name + "_same_netns"] = os.readlink(f"/proc/{p}/ns/net") == me
        env = open(f"/proc/{p}/environ", "rb").read().split(b"\0")
        r[name + "_secret_env"] = sorted(x.split(b"=")[0].decode() for x in env if any(s in x.split(b"=")[0] for s in (b"KEY", b"TOKEN", b"SECRET", b"PROXY")))
        r[name + "_uid"] = os.stat(f"/proc/{p}").st_uid
print("VANILLA_PROBES " + json.dumps(r))
EOF"""


def _vanilla_asset_code(size: float = 0.8) -> str:
    """The dry run's Blender code for arm V: the scripted asset, built in the live session and exported from it."""
    return ("import os\nos.chdir('/home/agent/project')\n" + BLENDER_ASSET_SCRIPT
            .replace("size=0.8, location=(0, 0, 0.4)", f"size={size}, location=(0, 0, {size / 2})"))


def scripted_vanilla_agent(size: float = 0.8, probe: bool = True) -> list[dict[str, Any]]:
    """The scripted model for arm V in a dry run: probe, read the scene, build and export through the MCP server, take
    a screenshot and open the file the harness saved it to (the stock harness's two steps), then reply."""
    def mcp(tool: str, **args: Any) -> dict[str, Any]:
        return _fc("call_mcp_tool", ServerName="blender", ToolName=tool, Arguments=args, toolSummary="Blender",
                   toolAction="Calling Blender")

    # the probe runs after the first MCP call: the server starts its Blender on first use
    turns = [_fc("run_command", CommandLine=PROBE_COMMAND + "\n" + VANILLA_PROBE_COMMAND, Cwd="/home/agent/project")] if probe else []
    return [mcp("get_scene_info"), *turns, mcp("execute_blender_code", code=_vanilla_asset_code(size)),
                    mcp("get_viewport_screenshot", view="three_quarter", max_size=512),
                    _fc("view_file", AbsolutePath="{media}", toolSummary="Looking", toolAction="Viewing the screenshot"),
                    {"parts": [{"text": "Made output/crate.glb: a textured box crate (scripted dry-run agent, arm V)."}]}]


def scripted_change(arm: str, use_blender: bool = True) -> list[dict[str, Any]]:
    """The scripted model of a change-request session in a dry run: look at the files, rebuild the asset 1.3 times
    larger (a stand-in for "make the handles 30 % larger": the dry run checks the plumbing and the diff, not the edit)."""
    look = _fc("run_command", CommandLine="ls -R . | grep -v '^\\.' | head -40", Cwd="/home/agent/project")
    if arm == "V":
        return [look, *scripted_vanilla_agent(size=1.04, probe=False)]
    if arm in PLUGIN_ARMS:
        return [look, *scripted_plugin_agent()[1:]]
    if use_blender:
        make = (f"cat > make_asset.py <<'EOF'\n{BLENDER_ASSET_SCRIPT.replace('size=0.8, location=(0, 0, 0.4)', 'size=1.04, location=(0, 0, 0.52)')}"
                "\nEOF\nblender -b --factory-startup --python make_asset.py 2>&1 | tail -3")
    else:
        make = f"cat > make_glb.py <<'EOF'\n{_make_glb_script().replace('s = 0.4', 's = 0.52')}\nEOF\npython3 make_glb.py"
    return [look, _fc("run_command", CommandLine=make, Cwd="/home/agent/project"),
            {"parts": [{"text": "Changed output/crate.glb (scripted dry-run change)."}]}]


# The plugin's MCP server runs inside the sandbox: find it, and show it shares the sandbox's network namespace (loopback
# only), that its environment has no key but the placeholder, and that the client it would use cannot reach the API
# except through the relay.
PLUGIN_PROBE_COMMAND = r"""python3 - <<'EOF'
import os, json, socket
r = {}
me = os.readlink("/proc/self/ns/net")
srv = [p for p in os.listdir("/proc") if p.isdigit() and b"antigravity3d.runtime.mcp_server" in open(f"/proc/{p}/cmdline", "rb").read()]
r["mcp_server_found"] = bool(srv)
if srv:
    p = srv[0]
    r["mcp_server_same_netns"] = os.readlink(f"/proc/{p}/ns/net") == me
    env = dict(x.split(b"=", 1) for x in open(f"/proc/{p}/environ", "rb").read().split(b"\0") if b"=" in x)
    r["mcp_server_key"] = env.get(b"GEMINI_" + b"API_KEY", b"").decode()  # split: the policy's string test is not what is measured
    r["mcp_server_base_url_is_local"] = env.get(b"GOOGLE_GEMINI_BASE_URL", b"").startswith(b"http://127.0.0.1:")
    r["mcp_server_secret_env"] = sorted(k.decode() for k in env if any(s in k for s in (b"TOKEN", b"SECRET", b"PROXY", b"AWS", b"GH_")))
r["interfaces"] = sorted(l.split(":")[0].strip() for l in open("/proc/self/net/dev").read().splitlines()[2:])
try:
    socket.create_connection(("142.250.0.1", 443), timeout=3); r["direct_api_ip"] = "OPEN"
except Exception as e:
    r["direct_api_ip"] = "closed: " + type(e).__name__
print("PLUGIN_PROBES " + json.dumps(r))
EOF"""


def _fc(name: str, **args: Any) -> dict[str, Any]:
    return {"parts": [{"functionCall": {"name": name, "args": args}}]}


class FakeUpstream:
    """Gemini REST stand-in for dry runs: scripted agent turns per account, a deterministic judge, 404 decisions."""

    def __init__(self, scripts: dict[str, list[dict[str, Any]]] | None = None, seed: int = 0, slow_stream_s: float = 0.0) -> None:
        """``slow_stream_s``: a streamed answer is sent as a thought event, then this pause, then the answer (chunked),
        as the service streams; ``last_sent_at`` is when the answer's chunk left (time.monotonic)."""
        self.slow_stream_s, self.last_sent_at = slow_stream_s, 0.0
        self.scripts = {k: list(v) for k, v in (scripts or {}).items()}
        self.requests: list[dict[str, Any]] = []
        self.rng = random.Random(seed)
        fake = self

        self.files: dict[str, dict[str, Any]] = {}

        class H(http.server.BaseHTTPRequestHandler):
            def _reply(self, status: int, payload: bytes, ctype: str = "application/json", extra: dict[str, str] | None = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self) -> None:  # http.server API
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                acct = self.headers.get("X-Pilot-Account", "")
                path = self.path.split("?")[0]
                if UPLOAD_START.match(path):  # resumable upload, step 1: hand out an upload URL
                    n = str(len(fake.files) + 1)
                    fake.files[n] = {"name": f"files/f{n}", "uri": f"https://generativelanguage.googleapis.com/v1beta/files/f{n}",
                                     "mimeType": self.headers.get("X-Goog-Upload-Header-Content-Type", "image/png"),
                                     "state": "ACTIVE", "account": acct}
                    fake.requests.append({"account": acct, "path": path, "body": {}})
                    return self._reply(200, b"{}", extra={"X-Goog-Upload-URL": f"{fake.url}/fake-upload/{n}",
                                                         "X-Goog-Upload-Status": "active"})
                if path.startswith("/fake-upload/"):  # step 2: the bytes, then the file record
                    f = fake.files.get(path.rsplit("/", 1)[-1])
                    if f is None:
                        return self._reply(404, b"{}")
                    f["bytes"] = len(raw)
                    fake.requests.append({"account": acct, "path": path, "body": {"bytes": len(raw)}})
                    rec = {k: v for k, v in f.items() if k != "account"}
                    return self._reply(200, json.dumps({"file": rec}).encode(), extra={"X-Goog-Upload-Status": "final"})
                body = json.loads(raw or b"{}")
                fake.requests.append({"account": acct, "path": self.path, "body": body})
                status, ctype, payload = fake.answer(acct, self.path, body)
                if status == 200 and "event-stream" in ctype and fake.slow_stream_s:
                    return self._slow_stream(payload, ctype)
                self._reply(status, payload, ctype)

            def _slow_stream(self, payload: bytes, ctype: str) -> None:
                thought = {"candidates": [{"content": {"role": "model", "parts": [{"text": "**Planning**", "thought": True}]}}]}
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for i, part in enumerate((f"data: {json.dumps(thought)}\n\n".encode(), payload)):
                    if i:
                        time.sleep(fake.slow_stream_s)
                        fake.last_sent_at = time.monotonic()
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")

            def do_GET(self) -> None:  # http.server API
                name = self.path.split("?")[0].rsplit("/", 1)[-1]
                f = fake.files.get(name.lstrip("f"))
                if f is None:
                    return self._reply(404, b"{}")
                self._reply(200, json.dumps({k: v for k, v in f.items() if k != "account"}).encode())

            def log_message(self, *a: Any) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    @staticmethod
    def _usage(body: dict[str, Any], out: int = 40) -> dict[str, int]:
        est, images = estimate_input_tokens(body)  # about 4 bytes per token for text, ~1,100 tokens per image
        p = (est - images * IMAGE_INPUT_TOKENS) * BYTES_PER_TOKEN // 4 + images * 1_100
        return {"promptTokenCount": p, "candidatesTokenCount": out, "thoughtsTokenCount": 10, "totalTokenCount": p + out + 10}

    def answer(self, acct: str, path: str, body: dict[str, Any]) -> tuple[int, str, bytes]:
        usage = None
        if ":countTokens" in path:
            return 200, "application/json", json.dumps({"totalTokens": len(json.dumps(body)) // 4}).encode()
        if "/models/" not in path:
            return 404, "application/json", b'{"error": {"code": 404, "message": "not found (fake)"}}'
        if acct.startswith("judge"):
            crit = {k: {"winner": self.rng.choice(["1", "2", "tie"]), "reason": "fake judge"}
                    for k in ("reads_as_brief", "proportion_scale", "surface_material", "defects")}
            doc = {"criteria": crit, "overall": {"winner": self.rng.choice(["1", "2"]), "reason": "fake judge"}}
            content = {"parts": [{"text": json.dumps(doc)}]}
        elif not body.get("tools") and _response_schema(body) is not None:  # a structured call (judging, authoring)
            # Usage as measured live (benchmarks/results/round1): a fallback judge call, a hydrogen authoring call.
            sch = _response_schema(body)
            props = set((sch.get("properties") or {}))
            if "nodes" in props:
                content = {"parts": [{"text": json.dumps(_scene_answer(body))}]}
                usage = self._usage(body, 1_140) | {"thoughtsTokenCount": 1_066}
            elif props == {"ops"}:
                content = {"parts": [{"text": json.dumps({"ops": []})}]}
                usage = self._usage(body, 92) | {"thoughtsTokenCount": 515}
            elif _question_schema(sch):
                content = {"parts": [{"text": json.dumps(_question_answer(sch))}]}
                usage = {"promptTokenCount": 2_312, "candidatesTokenCount": 169, "thoughtsTokenCount": 837, "totalTokenCount": 3_318}
            else:
                content = {"parts": [{"text": json.dumps(_schema_instance(sch))}]}
            if usage is not None:
                usage["totalTokenCount"] = usage["promptTokenCount"] + usage["candidatesTokenCount"] + usage["thoughtsTokenCount"]
        elif is_image_request(path.rsplit("/", 1)[-1].split(":")[0], body):
            tier = str(((body.get("generationConfig") or {}).get("imageConfig") or {}).get("imageSize", "1K")).upper()
            size = {"1K": 1024, "2K": 2048, "4K": 4096}.get(tier, 1024)
            content = {"parts": [{"inlineData": {"mimeType": "image/png", "data": base64.b64encode(_noise_png(size)).decode()}}]}
            img = {1024: 1_120, 2048: 1_680, 4096: 4_096}[size]  # measured: 1K 1,120 and 2K 1,680 image tokens
            usage = {"promptTokenCount": 62, "candidatesTokenCount": img + 321, "thoughtsTokenCount": 804,
                     "totalTokenCount": 62 + img + 321 + 804, "candidatesTokensDetails": [{"modality": "IMAGE", "tokenCount": img}]}
        else:
            script = self.scripts.get(acct)
            content = script.pop(0) if script else {"parts": [{"text": "done"}]}
            text = json.dumps(content)
            if "{conversation_id}" in text:  # a scripted model that read the SessionStart hook's message
                m = re.search(r"conversation_id=([0-9a-f-]{36})", json.dumps(body))
                content = json.loads(text.replace("{conversation_id}", m.group(1) if m else ""))
                text = json.dumps(content)
            if "{session_id}" in text:  # and the session id open_asset_session returned
                m = re.search(r"SESSION (\S+?) asset=", json.dumps(body))
                content = json.loads(text.replace("{session_id}", m.group(1) if m else ""))
                text = json.dumps(content)
            if "{media}" in text:  # a scripted model opening the file an MCP image was offloaded to (arm V)
                hits = re.findall(r"Resource offloaded to file://(/[^\]\"\\]+)\]", json.dumps(body))
                content = json.loads(text.replace("{media}", hits[-1] if hits else "/nonexistent.png"))
        ev = {"candidates": [{"content": {"role": "model", **content}, "finishReason": "STOP"}],
              "usageMetadata": usage or self._usage(body)}
        if ":streamGenerateContent" in path:
            return 200, "text/event-stream", f"data: {json.dumps(ev)}\n\n".encode()
        return 200, "application/json", json.dumps(ev).encode()


# ----------------------------------------------------------------------------------------------- runs


@dataclass
class RunRecord:
    brief: str
    arm: str
    status: str = "not run"  # finished | gave up | stopped at limit | crashed | timeout | refused
    wall_s: float = 0.0
    glb: str | None = None
    extra_files: list[str] = field(default_factory=list)
    model_calls: int = 0
    tool_calls: int = 0
    blender_invocations: int | None = None
    images: int = 0
    tokens: dict[str, int] = field(default_factory=dict)
    models: dict[str, Any] = field(default_factory=dict)
    final_text: str = ""
    notes: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    pipeline: dict[str, Any] = field(default_factory=dict)
    session: int = 1  # a continuation of the same cell is session 2 (its own account and run directory)
    account: str | None = None
    phase: str = "first"  # "first" (the cell's delivery, its sessions merged) or "change" (the change request)


def _account_tokens(ledger: Ledger, name: str) -> dict[str, int]:
    a = ledger.data["accounts"][name]
    return {k: a[k] for k in ("input_tokens", "output_tokens", "thinking_tokens", "cached_tokens", "unknown_reserved_tokens",
                              "share_tokens")} | {"total": Ledger.used_tokens(a), "images": a["images"], "share_images": a["share_images"]}


def host_blender_dir() -> Path:
    b = os.environ.get("BLENDER_BIN")
    if not b:
        raise SystemExit("BLENDER_BIN is not set (source engines_env.sh)")
    return Path(b).resolve().parent


def install_upstream_skills(dest_project: Path) -> list[str]:
    """What ``install/install.sh --agent agents --project DIR --copy`` does, reimplemented (the house rules forbid
    executing upstream files here): copy every ``skills/*/`` into ``DIR/.agents/skills/<name>``."""
    src = UPSKILLS / "skills"
    dst = dest_project / ".agents" / "skills"
    dst.mkdir(parents=True, exist_ok=True)
    names = []
    for s in sorted(p for p in src.iterdir() if p.is_dir()):
        copy_without_links(s, dst / s.name)  # a link in the reference checkout is not followed into the sandbox
        names.append(s.name)
    return names


def _chown_tree(path: Path, uid: int) -> None:
    for root, dirs, files in os.walk(path):
        os.chown(root, uid, uid)
        for n in dirs + files:
            with contextlib.suppress(FileNotFoundError):
                os.chown(os.path.join(root, n), uid, uid, follow_symlinks=False)


def socket_dir(run_dir: Path) -> Path:
    """A short host path for the relay socket (unix socket paths are limited to 107 bytes)."""
    import hashlib

    return Path("/run/agy-pilot") / hashlib.sha256(str(run_dir.resolve()).encode()).hexdigest()[:12]


def _imported_project_src() -> Path:
    """The src directory this process imports antigravity3d from (the frozen harness runs against the main tree)."""
    import importlib.util

    spec = importlib.util.find_spec("antigravity3d")
    return Path(spec.origin).resolve().parents[1] if spec and spec.origin else REPO / "src"


PROJECT_SRC = _imported_project_src()  # the tree arm C attaches (``--project-src`` points it at another worktree)


def install_project_plugin(dest_root: Path, workspace: str) -> Path:
    """The plugin bundle of PROJECT_SRC's tree, installed by that tree's own installer (copy mode, workspace scope)
    into ``dest_root/plugins/antigravity-3d``. Run as a subprocess so the installer is the tested tree's, not ours."""
    code = ("import sys; from pathlib import Path; from antigravity3d import plugin_install as p; "
            "print(p.install(Path(sys.argv[1]), workspace=Path(sys.argv[2])))")
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG")} | {"PYTHONPATH": str(PROJECT_SRC)}
    out = subprocess.run([sys.executable, "-c", code, str(dest_root), workspace], env=env, capture_output=True, text=True,
                         check=True)
    return Path(out.stdout.strip().splitlines()[-1])


def stock_spec(run_dir: Path, arm: str, brief: Brief, *, plugin: bool = False, share_tokens: int = 0,
               share_images: int = 0, calls: int | None = None, workspace: Path | None = None,
               task: str | None = None, timeout_s: int | None = None) -> dict[str, Any]:
    """Prepare a sandboxed run directory; returns the host-side spec (``compare_sandbox.command`` reads it)."""
    import compare_sandbox as sb

    for sub in (("home", "ctl") if workspace is not None else ("work/output", "home", "ctl")):
        (run_dir / sub).mkdir(parents=True, exist_ok=True)
    work = workspace if workspace is not None else run_dir / "work"
    sock_dir = socket_dir(run_dir)
    sock_dir.mkdir(parents=True, exist_ok=True)
    work = workspace if workspace is not None else run_dir / "work"
    skills = install_upstream_skills(work) if arm == "B" and not (work / ".agents" / "skills").is_dir() else []
    (run_dir / "home" / "project").mkdir(exist_ok=True)  # mount point for the workspace inside HOME
    import google.antigravity as ga

    pydeps = Path(ga.__file__).resolve().parents[2]
    cfg = {"task": task_text(brief, calls, arm), "timeout_s": C_TIMEOUT_S if BUDGET.get("paired") else (timeout_s or ARM_TIMEOUT_S.get(arm, STOCK_TIMEOUT_S)),
           "max_output_tokens": STOCK_MAX_OUTPUT,
           "budget": harness_budget(arm, share_tokens) if arm in CALL_MODEL and share_tokens else STOCK_BUDGET,
           "skills_path": f"{sb.WORKSPACE}/.agents/skills" if arm == "B" else None,
           "host": {"pydeps": str(pydeps), "blender_dir": str(host_blender_dir()), "ctl": str(run_dir / "ctl"),
                    "home": str(run_dir / "home"), "work": str(work), "sock_dir": str(sock_dir)}}
    if task is not None:
        cfg["task"] = task
    if plugin:
        installed = install_project_plugin(run_dir / "plugin_root", sb.WORKSPACE)
        (work / ".agents" / "plugins").mkdir(parents=True, exist_ok=True)  # the mount point (inside the workspace)
        host = cfg["host"]
        host.update(project_src=str(PROJECT_SRC), project_plugin=str(PROJECT_SRC.parent / "plugin"), plugins=str(installed.parent),
                    venv=str(Path(sys.prefix)))
        extra = {}
        if os.environ.get("GODOT_BIN"):
            host["godot_dir"] = str(Path(os.environ["GODOT_BIN"]).resolve().parent)
            extra["GODOT_BIN"] = f"{sb.GODOT_DIR}/{Path(os.environ['GODOT_BIN']).resolve().name}"
        if Path("/opt/pw-browsers").is_dir():
            host["browsers_dir"] = "/opt/pw-browsers"
            extra.update(PLAYWRIGHT_BROWSERS_PATH=sb.BROWSERS_DIR, CHROMIUM_BIN=f"{sb.BROWSERS_DIR}/chromium")
        if os.environ.get("AGY3D_THREE_JS"):
            host["three_dir"] = str(Path(os.environ["AGY3D_THREE_JS"]).resolve().parent)
            extra["AGY3D_THREE_JS"] = f"{sb.THREE_DIR}/{Path(os.environ['AGY3D_THREE_JS']).name}"
        cfg["extra_env"] = extra
        cfg["plugin"] = {"dir": f"{sb.PLUGINS_DIR}/{installed.name}", "limits": {"tokens": share_tokens, "images": share_images},
                         "project_src_host": str(PROJECT_SRC)}
    if arm == "V":  # the plain Blender MCP server: this file only, nothing of the project
        shutil.copyfile(HERE / "compare_vanilla_mcp.py", run_dir / "ctl" / "compare_vanilla_mcp.py")
        cfg["vanilla"] = {"server": f"{sb.CTL}/compare_vanilla_mcp.py", "blender": f"{sb.BLENDER_DIR}/blender"}
    shutil.copyfile(HERE / "compare_sandbox.py", run_dir / "ctl" / "compare_sandbox.py")
    (run_dir / "ctl" / "config.json").write_text(json.dumps(cfg, indent=1))
    _chown_tree(run_dir / "home", sb.NOBODY)
    _chown_tree(work, sb.NOBODY)
    os.chmod(sock_dir, 0o777)
    return {"host_sandbox_py": str(HERE / "compare_sandbox.py"), "host_config": str(run_dir / "ctl" / "config.json"),
            "skills": skills}


def find_main_glb(folder: Path) -> Path | None:
    """The delivered GLB: never a symbolic link the agent left (the harness reads it on the host, outside the sandbox)."""
    if not folder.is_dir() or folder.is_symlink():
        return None
    glbs = sorted(p for p in folder.rglob("*.glb") if "_LOD" not in p.stem and not p.stem.lower().endswith(("-col", "_col", "_collision", "_colliders"))
                  and real_file_in(folder, p))
    return max(glbs, key=lambda p: p.stat().st_size) if glbs else None


def run_conditions(run_dir: Path, load_log: Path | None = None) -> dict[str, Any]:
    """Facts about how a run went that the frozen A/B harness did not record itself, read back from its logs: model calls
    that ended at the output cap (finish reason MAX_TOKENS) and the machine's load average while it ran."""
    calls = [json.loads(x) for x in (run_dir / "relay_calls.jsonl").read_text().splitlines()] if (run_dir / "relay_calls.jsonl").exists() else []
    gen = [c for c in calls if c.get("status") == 200 and c.get("usage") is not None]
    finishes: dict[str, int] = {}
    for c in gen:
        finishes[str(c.get("finish"))] = finishes.get(str(c.get("finish")), 0) + 1
    out: dict[str, Any] = {"model_calls": len(gen), "finish_reasons": finishes,
                           "calls_at_output_cap": sum(1 for c in gen if str(c.get("finish")) == "MAX_TOKENS"),
                           "max_output_tokens_requested": sorted({c.get("max_output_tokens") for c in gen if c.get("max_output_tokens")}),
                           "service_failures": {k: sum(1 for c in calls if c.get("service") == k)
                                                for k in ("turned away", "broken", "server error")},
                           "last_call_service_failure": next((c.get("service") for c in reversed(calls) if c.get("kind") == "generate"
                                                              or c.get("service")), None)}
    ev = [json.loads(x) for x in (run_dir / "events.jsonl").read_text().splitlines()] if (run_dir / "events.jsonl").exists() else []
    t0 = min((e["t"] for e in ev), default=None)
    t1 = max((e["t"] for e in ev), default=None)
    out["start_end_unix"] = [t0, t1]
    if load_log is not None and load_log.exists() and t0 is not None:
        rows = [ln.split() for ln in load_log.read_text().splitlines() if ln.strip()]
        rows = [(float(r[0]), float(r[1]), float(r[2])) for r in rows if len(r) >= 3]
        near = lambda t: min(rows, key=lambda r: abs(r[0] - t)) if rows else None  # noqa: E731
        inside = [r[1] for r in rows if t0 <= r[0] <= t1]
        out["load_1min"] = {"start": near(t0)[1] if rows else None, "end": near(t1)[1] if rows else None,
                            "max_during": max(inside) if inside else None, "samples": len(inside), "cores": os.cpu_count()}
    return out


def mcp_tool_counts(calls: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in calls:
        a = c.get("args") or {}
        if c.get("name") == "call_mcp_tool" or a.get("ToolName"):
            t = str(a.get("ToolName") or "?")
            out[t] = out.get(t, 0) + 1
    return out


def vanilla_report(run_dir: Path) -> dict[str, Any]:
    """Arm V's MCP server log (written in the run's HOME): tool calls, errors, wall time in Blender, and whether the
    agent opened each screenshot the harness offloaded to a file (the only way a stock agent sees one)."""
    log = run_dir / "home" / "vanilla_mcp_calls.jsonl"
    rows = [json.loads(x) for x in log.read_text().splitlines() if x.strip()] if log.exists() else []
    shots = [r for r in rows if r.get("tool") == "get_viewport_screenshot" and not r.get("error")]
    viewed = 0
    tr = run_dir / "transcript.jsonl"
    if tr.exists():
        for line in tr.read_text().splitlines():
            for f in json.loads(line).get("function_calls") or []:
                if f.get("name") == "view_file" and "media_" in str((f.get("args") or {}).get("AbsolutePath", "")):
                    viewed += 1
    by_tool: dict[str, int] = {}
    for r in rows:
        by_tool[str(r.get("tool"))] = by_tool.get(str(r.get("tool")), 0) + 1
    return {"calls": len(rows), "by_tool": by_tool, "errors": sum(1 for r in rows if r.get("error")),
            "blender_wall_s": round(sum(float(r.get("wall_s") or 0) for r in rows), 1), "screenshots": len(shots),
            "screenshots_opened": viewed}


def plugin_self_report(work: Path) -> dict[str, Any]:
    """What the plugin says about its own output (shown in the gallery as that arm's self-report, never used to score)."""
    manifests = []
    for m in sorted(work.rglob("manifest.json")):
        if ".agents" in m.parts or not real_file_in(work, m):  # never through a link the agent left
            continue
        with contextlib.suppress(ValueError, OSError):
            d = json.loads(m.read_text())
            manifests.append({"path": str(m), "asset": d.get("asset"), "verified": d.get("verified"), "preview": d.get("preview"),
                              "tris": d.get("tris"), "evidence": d.get("evidence"), "validation": d.get("validation")})
    qa = []
    for q in [q for q in sorted((work / ".agy3d" / "qa").glob("*.json")) if real_file_in(work, q)] if (work / ".agy3d" / "qa").is_dir() else []:
        with contextlib.suppress(ValueError, OSError):
            qa.append(json.loads(q.read_text()))
    return {"manifests": manifests, "qa_records": qa}


def workspace_listing(work: Path) -> list[str]:
    """The files (and empty folders) a session left, relative to the workspace; hidden ones are the harness's own."""
    items = []
    for p in sorted(work.rglob("*")):
        rel = p.relative_to(work)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if p.is_file() or (p.is_dir() and not any(p.iterdir())):
            items.append(f"{rel.as_posix()}{'/ (empty folder)' if p.is_dir() else ''}")
    return items


def continuation_task(brief: Brief, files: list[str], calls: int) -> str:
    """The original task, then a short factual paragraph (what happened, what exists; nothing about what to run), then the
    budget sentence with this session's own N."""
    return (task_text(brief) + "\n\nAn earlier session of this task ran out of its budget before delivering. Your working "
            "directory holds what it produced: " + ", ".join(files) + ". Continue from there and deliver.\n\n"
            + budget_sentence(calls))


def run_continuation(relay: Relay, root: Path, brief: Brief, arm: str, *, wall_s: float = STOCK_TIMEOUT_S + 300,
                     offline: bool = False) -> RunRecord:
    """A fresh stock conversation in the same workspace as the cell's first session, under its own account; for arm C, a
    second attempt of the command in a fresh output folder, under its own account."""
    c = BUDGET["continuations"][f"{brief.id}:{arm}"]
    if arm == "C":
        first_acct = relay.ledger.data["accounts"].get(cell_name(brief.id, arm))
        name = continuation_name(brief.id, arm, c["session"])
        if not (root / "runs" / name.replace(":", "_") / "record.json").exists() and not (first_acct and first_acct.get("closed")):
            raise RuntimeError(f"close {cell_name(brief.id, arm)} first: the second attempt's images are the first's unused ones")
        return run_c(relay, root, brief, offline=offline, account=name, session=c["session"], share_tokens=c["tokens"],
                     share_images=c["images"], force_single=c.get("why") if c.get("single_candidate") else None)
    first_dir = root / "runs" / cell_name(brief.id, arm).replace(":", "_")
    first = json.loads((first_dir / "record.json").read_text()) if (first_dir / "record.json").exists() else None
    if first is None:
        raise RuntimeError(f"no first session of {brief.id}:{arm} to continue")
    if first["status"] == "finished":
        raise RuntimeError(f"{brief.id}:{arm} delivered in its first session: nothing to continue")
    work = Path(first.get("pipeline", {}).get("workspace") or first_dir / "work")
    name = continuation_name(brief.id, arm, c["session"])
    first_acct = relay.ledger.data["accounts"].get(cell_name(brief.id, arm))
    if not (run_dir_done := (root / "runs" / name.replace(":", "_") / "record.json").exists()) and \
            not (first_acct and first_acct.get("closed")):
        raise RuntimeError(f"close {cell_name(brief.id, arm)} first (close --accounts {cell_name(brief.id, arm)}): the "
                           "continuation reuses the image its first session did not use, and only a closed account gives it back")
    del run_dir_done
    run_dir = root / "runs" / name.replace(":", "_")
    if (run_dir / "record.json").exists():
        return RunRecord(**json.loads((run_dir / "record.json").read_text()))
    calls = calls_allowed(arm, c["tokens"])
    task = continuation_task(brief, workspace_listing(work), calls)
    rec = run_stock(relay, root, brief, arm, share_tokens=c["tokens"], share_images=c["images"], account=name,
                    workspace=work, task=task, session=c["session"], wall_s=wall_s)
    return rec


def merge_sessions(records: list[RunRecord]) -> RunRecord:
    """One cell's sessions as one record: sums of what was spent, the last session's outcome, each session listed."""
    records = sorted(records, key=lambda r: r.session)
    if len(records) == 1:
        return records[0]
    last = records[-1]
    tok: dict[str, int] = {}
    for r in records:
        for k, v in (r.tokens or {}).items():
            if isinstance(v, int) and not k.startswith("share"):
                tok[k] = tok.get(k, 0) + v
    m = RunRecord(**{**asdict(last)})
    m.tokens = tok
    m.wall_s = round(sum(r.wall_s for r in records), 1)
    m.model_calls = sum(r.model_calls or 0 for r in records)
    m.tool_calls = sum(r.tool_calls or 0 for r in records)
    m.blender_invocations = sum(r.blender_invocations or 0 for r in records)
    m.images = sum(r.images or 0 for r in records)
    m.notes = [f"session {r.session}: {n}" for r in records for n in r.notes]
    m.pipeline = {**last.pipeline, "sessions": [
        {"session": r.session, "account": r.account, "status": r.status, "tokens": r.tokens.get("total"),
         "mode": (r.pipeline or {}).get("mode") or ("default" if r.arm == "C" else None), "images_used": r.images,
         "cached_tokens": r.tokens.get("cached_tokens"), "model_calls": r.model_calls, "tool_calls": r.tool_calls,
         "wall_s": r.wall_s, "images": r.images, "glb": r.glb, "final_text": (r.final_text or "")[:1500]} for r in records]}
    return m


def change_task(brief: Brief, files: list[str], calls: int, arm: str) -> str:
    """The original task, then what exists and the change request, then the budget sentence with this session's own N.
    The same words for every agent arm; arm C gets the change sentence on its command line (run_change)."""
    return (task_text(brief, arm=arm) + "\n\nAn earlier session made this asset and delivered it. Your working directory "
            "holds what it left: " + ", ".join(files) + f".\n\nChange request: {CHANGES[brief.id]['text']}. Leave "
            "everything else as it is, and deliver the changed asset the same way (replace the files in `output/`).\n\n"
            + budget_sentence(calls))


def first_record(root: Path, brief: str, arm: str) -> RunRecord | None:
    """The cell's delivery: its first-phase sessions merged (a continuation included)."""
    recs = [r for r in collect_records(root) if (r.brief, r.arm) == (brief, arm)]
    return recs[0] if recs else None


def _sha(p: Path) -> str | None:
    import hashlib

    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() and not p.is_symlink() else None


def run_change(relay: Relay, root: Path, brief: Brief, arm: str, *, offline: bool = True,
               wall_s: float | None = None) -> RunRecord:
    """One change request (CHANGES) after the cell delivered: a new session over a copy of what the first left, under its
    own account at ``change_share`` of the arm's share; then the two packages are compared (package_diff). The first
    session's files stay as they were, so the comparison reads both."""
    import compare_eval as ev

    name = change_name(brief.id, arm)
    run_dir = root / "runs" / name.replace(":", "_")
    if (run_dir / "record.json").exists():
        return RunRecord(**json.loads((run_dir / "record.json").read_text()))
    first = first_record(root, brief.id, arm)
    if first is None or not first.glb or not Path(first.glb).is_file():
        raise RuntimeError(f"{brief.id}:{arm} has no delivered GLB: nothing to change")
    tokens, images = change_share(arm)
    change = CHANGES[brief.id]["text"]
    before = Path(first.glb)
    if arm == "C":
        seed = before.parent
        while seed.name != "out" and seed != seed.parent:  # the command's --out folder (the package sits below it)
            seed = seed.parent
        rec = run_c(relay, root, brief, offline=offline, account=name, share_tokens=tokens, share_images=images,
                    prompt=f"{brief.prompt}. Change: {change}", out_seed=seed, change=change, phase="change")
        after = Path(rec.glb) if rec.glb else None
    else:
        src = Path(first.pipeline.get("workspace") or "")
        work = root / "change_work" / name.replace(":", "_")
        if work.exists():
            shutil.rmtree(work)
        # real files and folders only: a link the first session left is not carried into the copy, where the harness
        # would later read through it on the host (find_main_glb, _sha, the evaluation)
        left_out = copy_without_links(src, work, ignore=lambda d, names: [
            n for n in names if Path(d) == src and n == ".agents" and arm in PLUGIN_ARMS])
        files = workspace_listing(work)
        task = change_task(brief, files, calls_allowed(arm, tokens), arm)
        seeded = _sha(work / before.relative_to(src)) if before.is_relative_to(src) else None
        rec = run_stock(relay, root, brief, arm, share_tokens=tokens, share_images=images, account=name, workspace=work,
                        task=task, phase="change", wall_s=wall_s)
        if left_out:
            rec.notes.append(f"links the first session left were not copied into the change workspace: {left_out[:6]}")
        after = Path(rec.glb) if rec.glb else None
        if after is not None and _sha(after) == seeded:  # the copy of the first delivery, untouched
            rec.notes.append("the main GLB is the first session's, unchanged: no new package was delivered")
            rec.status = "gave up" if rec.status == "finished" else rec.status
            after = None
    rec.pipeline["change"] = change
    rec.pipeline["before_glb"] = str(before)
    rec.pipeline["first"] = {"wall_s": first.wall_s, "model_calls": first.model_calls, "tokens": (first.tokens or {}).get("total")}
    if after is not None and after.is_file():
        try:
            rec.pipeline["diff"] = ev.package_diff(before, after, target_ratio=CHANGES[brief.id]["ratio"])
        except Exception as e:  # noqa: BLE001 - a GLB the diff cannot read is a recorded outcome
            rec.pipeline["diff"] = {"error": f"{type(e).__name__}: {e}"[:400]}
    (run_dir / "record.json").write_text(json.dumps(asdict(rec), indent=1))
    return rec


def repeat_name(brief: str, arm: str) -> str:
    return f"{cell_name(brief, arm)}.repeat"


def _shipped_hashes(glb: Path) -> dict[str, str] | None:
    """sha256 of every file the package's manifest lists as shipped (None without a manifest)."""
    m = glb.parent / "manifest.json"
    if not m.is_file():
        return None
    return {f: v.get("sha256") for f, v in (json.loads(m.read_text()).get("file_hashes") or {}).items()}


def run_repeat(relay: Relay, root: Path, brief: Brief, *, offline: bool = True) -> RunRecord:
    """Same request, same bytes (arm C only: a stock agent has no replay): the cell's command again, unchanged, into a
    copy of its first output folder, so it reads the design record and image cache the first run left. Every shipped
    file's sha256 is compared with the first package's."""
    name = repeat_name(brief.id, "C")
    run_dir = root / "runs" / name.replace(":", "_")
    if (run_dir / "record.json").exists():
        return RunRecord(**json.loads((run_dir / "record.json").read_text()))
    first = first_record(root, brief.id, "C")
    if first is None or not first.glb or not Path(first.glb).is_file():
        raise RuntimeError(f"{brief.id}:C has no delivered GLB: nothing to repeat")
    seed = Path(first.glb).parent
    while seed.name != "out" and seed != seed.parent:
        seed = seed.parent
    tokens = change_share("C")[0]
    rec = run_c(relay, root, brief, offline=offline, account=name, share_tokens=tokens, share_images=0,
                out_seed=seed, phase="repeat")
    a = _shipped_hashes(Path(first.glb))
    b = _shipped_hashes(Path(rec.glb)) if rec.glb else None
    if a is None or b is None:
        rec.pipeline["same_bytes"], rec.pipeline["bytes_compare"] = None, "no manifest on one side"
    else:
        diff = sorted(f for f in set(a) | set(b) if a.get(f) != b.get(f))
        rec.pipeline["same_bytes"] = not diff
        rec.pipeline["bytes_compare"] = {"files": len(set(a) | set(b)), "different": diff[:50]}
    (run_dir / "record.json").write_text(json.dumps(asdict(rec), indent=1))
    return rec


def collect_changes(root: Path, phase: str = "change") -> list[RunRecord]:
    prefix = f"{BUDGET['round']}_" if BUDGET.get("round") else ""
    out = []
    for p in sorted((root / "runs").glob("*/record.json")):
        if prefix and not p.parent.name.startswith(prefix):
            continue
        r = RunRecord(**json.loads(p.read_text()))
        if r.phase == phase:
            out.append(r)
    return out


def blender_call(args: dict[str, Any]) -> bool:
    """A shell command that starts Blender (directly or through the upstream skills' runner)."""
    import re

    line = str(args.get("CommandLine") or args.get("command_line") or "")
    return bool(re.search(r"(^|[\s;&|(/])blender(\s|$)", line) or "run_in_blender" in line)


def run_stock(relay: Relay, root: Path, brief: Brief, arm: str, *, wall_s: float | None = None,
              share_tokens: int | None = None, share_images: int | None = None, tag: str = "",
              plugin: bool | None = None, account: str | None = None, workspace: Path | None = None,
              task: str | None = None, session: int = 1, phase: str = "first") -> RunRecord:
    """One sandboxed stock-agent run. ``tag`` names a run outside the pilot's cells (the smoke run)."""
    import compare_sandbox as sb

    tag = tag or (f"{BUDGET['round']}:" if BUDGET.get("round") else "")
    name = account or f"{tag}{brief.id}:{arm}"
    run_dir = root / "runs" / (name.replace(":", "_") if account else
                               f"{tag.rstrip(':').replace(':', '_') + '_' if tag else ''}{brief.id}_{arm}")
    share_tokens = BUDGET["run_tokens"][arm] if share_tokens is None else share_tokens
    share_images = BUDGET["run_images"][arm] if share_images is None else share_images
    plugin = (arm in PLUGIN_ARMS) if plugin is None else plugin
    wall_s = (ARM_TIMEOUT_S.get(arm, STOCK_TIMEOUT_S) + 300) if wall_s is None else wall_s
    rec = RunRecord(brief.id, arm, session=session, account=name, phase=phase)
    if (run_dir / "record.json").exists():
        return RunRecord(**json.loads((run_dir / "record.json").read_text()))
    acct = relay.ledger.data["accounts"].get(name)
    if acct is not None and (Ledger.used_tokens(acct) or acct["calls"] or acct["free_calls"] or acct.get("closed")):
        raise RuntimeError(f"refusing to start {name}: its account already shows spending (or is closed) and the run "
                           "left no record; inspect runs/ and the ledger before anything else")
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    load_start = None
    with contextlib.suppress(OSError):
        load_start = float(Path("/proc/loadavg").read_text().split()[0])
    spec = stock_spec(run_dir, arm, brief, plugin=plugin, share_tokens=share_tokens, share_images=share_images,
                      calls=calls_allowed(arm, share_tokens) if BUDGET.get("round") and not tag.startswith("smoke") else None,
                      workspace=workspace, task=task, timeout_s=int(wall_s - 300) if wall_s > 600 else None)
    work = workspace if workspace is not None else run_dir / "work"
    rec.pipeline["workspace"] = str(work)
    rec.pipeline["task"] = json.loads(Path(spec["host_config"]).read_text())["task"]
    if spec["skills"]:
        rec.notes.append(f"{len(spec['skills'])} upstream skills installed in .agents/skills and attached via skills_paths")
    acc = relay.open(name, share_tokens, share_images, run_dir, scope="plugin" if plugin else "stock",
                     unix_socket=socket_dir(run_dir) / "relay.sock")
    if plugin:
        rec.notes.append(f"plugin attached by hand through SDK equivalents from {PROJECT_SRC.parent}/plugin (see PLUGIN_DISCOVERY)")
    argv, env = sb.command(spec)
    r_fd, w_fd = os.pipe()
    cfg_path = Path(spec["host_config"])
    cfg_path.write_text(json.dumps(json.loads(cfg_path.read_text()) | {"event_fd": w_fd}, indent=1))
    t0 = time.monotonic()
    events_path = run_dir / "events.jsonl"
    stderr = (run_dir / "sandbox_stderr.log").open("wb")
    proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=stderr,
                            pass_fds=(w_fd,), start_new_session=True)
    os.close(w_fd)
    events: list[dict[str, Any]] = []

    def reader() -> None:
        with os.fdopen(r_fd) as f, events_path.open("w") as out:
            for line in f:
                out.write(line)
                out.flush()
                with contextlib.suppress(json.JSONDecodeError):
                    events.append(json.loads(line))

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    stopped_by_limit_at = None
    while proc.poll() is None:
        time.sleep(1)
        if acc.refused and any("spend limit" in x for x in acc.refused) and stopped_by_limit_at is None:
            stopped_by_limit_at = time.monotonic()
        if stopped_by_limit_at and time.monotonic() - stopped_by_limit_at > 60:
            rec.notes.append("stopped by the harness 60 s after its share refused a model call")
            os.killpg(proc.pid, signal.SIGKILL)
        if time.monotonic() - t0 > wall_s:
            rec.notes.append(f"killed at the wall limit of {wall_s:.0f} s")
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)  # anything left in the session (the PID namespace dies with its PID 1)
    th.join(10)
    stderr.close()
    relay.close(name)
    rec.wall_s = round(time.monotonic() - t0, 1)
    end = next((e for e in reversed(events) if e.get("kind") == "end"), None)
    calls = [e for e in events if e.get("kind") == "tool_call"]
    rec.tool_calls = len(calls)
    rec.blender_invocations = sum(1 for c in calls if c.get("name") == "run_command" and blender_call(c.get("args") or {}))
    rec.model_calls = acc.calls
    rec.models = acc.models
    rec.refused = list(acc.refused)
    rec.tokens = _account_tokens(relay.ledger, name)
    rec.images = rec.tokens["images"]
    glb = find_main_glb(work / "output") or find_main_glb(work)
    rec.glb = str(glb) if glb else None
    if glb:
        rec.extra_files = sorted(str(p) for p in glb.parent.rglob("*") if p.is_file() and p != glb)[:200]
    rec.final_text = (end or {}).get("text") or ""
    with contextlib.suppress(OSError):
        rec.pipeline["load_1min_end"] = float(Path("/proc/loadavg").read_text().split()[0])
    rec.pipeline["load_1min_start"] = load_start
    rec.pipeline["conditions"] = run_conditions(run_dir)
    if arm == "V":
        rec.pipeline["mcp_tools"] = mcp_tool_counts(calls)
        rec.pipeline["vanilla"] = vanilla_report(run_dir)
    if plugin:
        rec.pipeline["self_report"] = plugin_self_report(work)
        rec.pipeline["mcp_tools"] = mcp_tool_counts(calls)
        rec.pipeline["attached"] = next((e for e in events if e.get("kind") == "plugin_attached"), None)
    limit = any("spend limit" in x for x in acc.refused) or "MAX_" in str((end or {}).get("stop_reason"))
    svc = rec.pipeline["conditions"].get("last_call_service_failure")
    if limit:
        rec.status = "stopped at limit"
    elif svc and not (end and end.get("status") == "finished" and glb):
        # The run ended without a delivery right after the service failed a call: the service's failure, not the arm's.
        # Its account shows spending, so it is not re-run silently; the record says what happened.
        rec.status = "stopped by the service"
        rec.notes.append(f"the last model call was {svc} by the service; failures this run: "
                         f"{rec.pipeline['conditions']['service_failures']}")
    elif end is None:
        rec.status = "crashed"
        rec.notes.append("no end event from the sandbox (see sandbox_stderr.log)")
    elif end["status"] == "finished":
        rec.status = "finished" if glb else "gave up"
    else:
        rec.status = end["status"]
        if end.get("error"):
            rec.notes.append(str(end["error"])[:500])
    (run_dir / "record.json").write_text(json.dumps(asdict(rec), indent=1))
    return rec


def c_env(relay_url: str | None, run_dir: Path) -> dict[str, str]:
    """Arm C's environment: an allow-list. The API key it sees is a placeholder; the relay holds the real one."""
    keep = ("PATH", "LANG", "LC_ALL", "DISPLAY", "XAUTHORITY", "BLENDER_BIN", "GODOT_BIN", "AGY3D_THREE_JS", "CHROMIUM_BIN",
            "AGY3D_BLENDER_SKILLS", "LIBGL_ALWAYS_SOFTWARE", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
            "PLAYWRIGHT_BROWSERS_PATH", "VIRTUAL_ENV", "PYTHONPATH")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    home = run_dir / "home"
    home.mkdir(parents=True, exist_ok=True)
    # A short TMPDIR: Chromium puts unix sockets there, and a socket path over 107 bytes crashes it at launch
    # (measured: the web check fails with TargetClosedError under the run directory's long path).
    import tempfile

    env.update(HOME=str(home), TMPDIR=tempfile.mkdtemp(prefix="agyc-", dir="/tmp"), PYTHONUNBUFFERED="1")
    if relay_url:
        env.update(GEMINI_API_KEY="via-relay", GOOGLE_GEMINI_BASE_URL=relay_url)
    return env


# Arm C in round 2: this project's pipeline from its command line, preview mode (advisor, 2026-11-21). What the task's
# game-ready list asks that the command line cannot be told (the pipeline applies its own defaults for these):
C_UNEXPRESSED = (
    "real-world scale, +Y up, grounded and centred (the pipeline's own conventions apply; not a flag)",
    "UVs inside 0-1 and not overlapping (the pipeline unwraps by its own rules)",
    "at most 3 materials; a base colour, normal and metallic-roughness texture per material (its own lookdev decides)",
    "texture sizes (power of two, at most 2048 px): no texture-size flag",
    "LODs and collision named as the task asks (<name>_LOD1.glb, '-col'): it writes its own package layout",
    "the delivery folder output/<name>.glb + output/textures/: it writes a package directory",
    "the budget sentence: there is no agent to read it; the command's own --token-ceiling / --max-images bound it",
)
C_TOKEN_MARGIN = 15_000  # the command's own ceiling sits this far below the relay's share


def c_mode(project_src: Path, max_images: int) -> tuple[bool, list[str], list[str]]:
    """(single_candidate, default-mode plan lines, single-candidate plan lines) from the tested tree's own planner.
    The plugin's default mode is used when every plan's base image need (one material) fits ``max_images``."""
    code = ("import json, sys; from antigravity3d.tactics.token_plan import plan_lines as p; "
            "print(json.dumps([p(single_candidate=False, max_rounds=4), p(single_candidate=True, max_rounds=4)]))")
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG")} | {"PYTHONPATH": str(project_src)}
    try:
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True, timeout=120)
        default, single = json.loads(out.stdout.strip().splitlines()[-1])
    except (subprocess.SubprocessError, ValueError, IndexError):
        return False, [], []  # an older tree has no planner: default mode, as before
    needs = [int(re.search(r"up to (\d+) image", ln).group(1)) for ln in default if re.search(r"up to (\d+) image", ln)]
    return (bool(needs) and max(needs) > max_images), default, single


def scripted_c_patch(out: Path, brief_id: str, ratio: float) -> dict[str, Any] | None:
    """The dry run's stand-in for the author of an edit: the nodes whose ids name the brief's changed part group
    (C_CHANGE_PARTS) scaled by ``ratio`` about their own centre. None when the design has no such node."""
    from antigravity3d.dags.edit_asset import load_shipped

    scene = load_shipped(out).scene
    words = C_CHANGE_PARTS.get(brief_id, ())
    ops = []
    for n in scene.nodes:
        if any(w in n.id for w in words):
            t = n.transform
            ops.append({"op": "set_transform", "node_id": n.id, "transform": {**t.model_dump(mode="json"),
                                                                             "scale": [v * ratio for v in t.scale]}})
    return {"ops": ops} if ops else None


def _run_c_edit(relay: Relay | None, acc: Any, rec: RunRecord, run_dir: Path, out: Path, brief: Brief, change: str, *,
                offline: bool, wall_s: float, share_t: int) -> RunRecord:
    """Arm C's change in edit mode: ``agy3d edit OUT "<change>"`` on the copy of the first run's output folder."""
    cmd = [sys.executable, "-m", "antigravity3d.cli", "edit", str(out), change]
    rec.pipeline["change_mode"] = "edit"
    if offline:
        patch = scripted_c_patch(out, brief.id, CHANGES[brief.id]["ratio"])
        if patch is not None:
            (run_dir / "scripted_patch.json").write_text(json.dumps(patch))
            cmd += ["--patch", str(run_dir / "scripted_patch.json")]
        cmd.append("--offline")
        rec.notes.append("dry run: --offline with a scripted patch (scripted_c_patch): no model call")
    else:
        cmd += ["--token-ceiling", str(share_t - C_TOKEN_MARGIN), "--max-images", "0"]
    if os.environ.get("BLENDER_BIN"):
        cmd += ["--blender", os.environ["BLENDER_BIN"]]
    rec.pipeline["command"] = cmd[1:]
    env = c_env(acc.endpoint if acc else None, run_dir)
    env["PYTHONPATH"] = str(PROJECT_SRC)
    t0, t0_unix = time.monotonic(), time.time()
    with (run_dir / "stdout.log").open("wb") as so, (run_dir / "stderr.log").open("wb") as se:
        proc = subprocess.Popen(cmd, env=env, cwd=run_dir, stdin=subprocess.DEVNULL, stdout=so, stderr=se, start_new_session=True)
        try:
            code = proc.wait(timeout=wall_s)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            code = None
            rec.notes.append(f"killed at the wall limit of {wall_s:.0f} s")
    rec.wall_s = round(time.monotonic() - t0, 1)
    (run_dir / "events.jsonl").write_text(json.dumps({"t": t0_unix, "kind": "start"}) + "\n" +
                                          json.dumps({"t": time.time(), "kind": "end", "exit": code}) + "\n")
    stdout = (run_dir / "stdout.log").read_text(errors="replace")
    report = {}
    if (out / "report.json").exists():
        with contextlib.suppress(ValueError):
            report = json.loads((out / "report.json").read_text())
    extras = report.get("extras") or report
    edit = extras.get("edit") or {}
    glb = Path(edit_glb) if (edit_glb := (extras.get("outputs") or {}).get("glb")) and Path(edit_glb).exists() and edit.get("kept") else None
    rec.glb = str(glb) if glb else None
    made = next((ln for ln in stdout.splitlines() if ln.startswith(("edit ", "edit:"))), None)
    rec.status = "finished" if glb else ("gave up" if code is not None else "timeout")
    if not glb:
        rec.notes.append("no edited package: " + (made or "the command did not finish"))
    rec.pipeline.update({"exit_code": code, "edit": edit, "edit_line": made,
                         "preview_gates": next((ln.split(":", 1)[1].strip() for ln in stdout.splitlines() if ln.startswith("preview gates:")), None),
                         "stdout_tail": stdout[-4000:], "stderr_tail": (run_dir / "stderr.log").read_text(errors="replace")[-3000:]})
    if acc:
        relay.close(rec.account)
        rec.model_calls, rec.models, rec.refused = acc.calls, acc.models, list(acc.refused)
        rec.tokens = _account_tokens(relay.ledger, rec.account)
        rec.images = rec.tokens["images"]
        rec.pipeline["conditions"] = run_conditions(run_dir)
    (run_dir / "record.json").write_text(json.dumps(asdict(rec), indent=1))
    return rec


def run_c(relay: Relay | None, root: Path, brief: Brief, *, offline: bool, wall_s: float = C_TIMEOUT_S,
          account: str | None = None, session: int = 1, share_tokens: int | None = None, share_images: int | None = None,
          force_single: str | None = None, prompt: str | None = None, out_seed: Path | None = None,
          change: str | None = None, phase: str = "first") -> RunRecord:
    """Arm C: ``agy3d prop "<brief>" --preview --token-ceiling N --max-images M --engine godot_4 --max-tris T`` from the
    project tree under test, through the relay with a placeholder key (the relay's count is the independent record).
    ``force_single`` (a reason) runs ``--single-candidate`` whatever the planner says."""
    if BUDGET.get("paired") and (prompt is not None or out_seed is not None or change is not None or force_single
                                  or session != 1):
        raise ValueError("paired arm C must use its declared brief and candidate policy in one fresh session")
    name = account or cell_name(brief.id, "C")
    run_dir = root / "runs" / name.replace(":", "_")
    if (run_dir / "record.json").exists():
        return RunRecord(**json.loads((run_dir / "record.json").read_text()))
    if relay is not None:
        acct = relay.ledger.data["accounts"].get(name)
        if acct is not None and (Ledger.used_tokens(acct) or acct["calls"] or acct["free_calls"] or acct.get("closed")):
            raise RuntimeError(f"refusing to start {name}: its account already shows spending (or is closed) and the run "
                               "left no record; inspect runs/ and the ledger before anything else")
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    rec = RunRecord(brief.id, "C", session=session, account=name, phase=phase)
    share_t = BUDGET["run_tokens"]["C"] if share_tokens is None else share_tokens
    share_i = BUDGET["run_images"]["C"] if share_images is None else share_images
    acc = relay.open(name, share_t, share_i, run_dir, sandboxed=False) if relay else None
    out = run_dir / "out"
    edit_mode = change is not None and C_CHANGE_MODE == "edit"
    if out_seed is not None and edit_mode:
        # An edit starts from the first run's whole output folder (its report names the package that shipped, its run
        # folder the source maps), except its asset ledger (ledgers/: the relay counts this session on its own account).
        shutil.copytree(out_seed, out, symlinks=True, ignore=lambda d, names: [n for n in names if Path(d) == out_seed and n == "ledgers"])
        rec.pipeline["seeded_from"] = str(out_seed)
    elif out_seed is not None:
        # A change or a repeat starts from the first run's cache (OUT/.agy3d-cache: its design record and images), which
        # is what a rerun into the same --out reads. Not copied: the first run's package and report (so any package
        # found afterwards is this run's) and its asset ledger (ledgers/: it would count the first run's spend against
        # this run's --token-ceiling; the relay counts this run on its own account either way).
        out.mkdir(parents=True)
        if (out_seed / ".agy3d-cache").is_dir():
            shutil.copytree(out_seed / ".agy3d-cache", out / ".agy3d-cache", symlinks=True)
        rec.pipeline["seeded_from"] = f"{out_seed}/.agy3d-cache"
    prompt = (paired_brief(brief) if BUDGET.get("paired") else brief.prompt) if prompt is None else prompt
    content = prompt
    if edit_mode:
        return _run_c_edit(relay, acc, rec, run_dir, out, brief, change, offline=offline, wall_s=wall_s, share_t=share_t)
    else:
        cmd = [sys.executable, "-m", "antigravity3d.cli", "prop", prompt, "--engine", ENGINE, "--max-tris",
               str(brief.max_tris), "--out", str(out), "--preview"]
    if change is not None:
        rec.pipeline["change_mode"] = C_CHANGE_MODE
    if os.environ.get("BLENDER_BIN"):
        cmd += ["--blender", os.environ["BLENDER_BIN"]]
        rec.notes.append("arm C had a Blender worker (--blender): lookdev renders and the Blender-side checks are those a user "
                         "with Blender gets")
    else:
        rec.notes.append("arm C had NO Blender worker (BLENDER_BIN unset)")
    single, default_lines, single_lines = c_mode(PROJECT_SRC, share_i)
    rec.pipeline["plan_lines"] = {"default": default_lines, "single_candidate": single_lines}
    if BUDGET.get("paired"):
        # Do not let remaining image headroom silently change the experimental treatment.
        single = BUDGET["c_candidates"] == "single"
        rec.pipeline["author_contract"] = "MeteredAuthor via CLI bind; production_prop (not native AgentAuthor)"
        rec.pipeline["content_requirements"] = content
    if offline:
        cmd.append("--offline")
        rec.notes.append("dry run: --offline (fake decisions, fake images, TemplateAuthor); no model calls")
    else:
        cmd += ["--token-ceiling", str(share_t - C_TOKEN_MARGIN), "--max-images", str(share_i)]
    if force_single:
        single = True
    if single:
        cmd.append("--single-candidate")
        rec.notes.append("--single-candidate: selected in the paired comparison plan" if BUDGET.get("paired") else
                         f"--single-candidate, {force_single}" if force_single else
                         f"--single-candidate: the default mode's plans need more than {share_i} images ({'; '.join(default_lines)})")
    rec.pipeline["mode"] = ("single-candidate" + (f" ({force_single})" if force_single else "")) if single else "default"
    rec.notes.append("arm C: this project's pipeline (command line, preview mode); "
                     + ("shared content requirements are included in the brief; package conventions remain pipeline-specific"
                        if BUDGET.get("paired") else "not expressible on its command line: " + "; ".join(C_UNEXPRESSED)))
    rec.pipeline["command"] = [c if c != content else f"<brief {brief.id}>" for c in cmd[1:]]
    rec.pipeline["prompt"] = prompt
    env = c_env(acc.endpoint if acc else None, run_dir)
    env["PYTHONPATH"] = str(PROJECT_SRC)
    load_start = None
    with contextlib.suppress(OSError):
        load_start = float(Path("/proc/loadavg").read_text().split()[0])
    t0 = time.monotonic()
    t0_unix = time.time()
    with (run_dir / "stdout.log").open("wb") as so, (run_dir / "stderr.log").open("wb") as se:
        proc = subprocess.Popen(cmd, env=env, cwd=run_dir, stdin=subprocess.DEVNULL, stdout=so, stderr=se, start_new_session=True)
        try:
            code = proc.wait(timeout=wall_s)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            code = None
            rec.notes.append(f"killed at the wall limit of {wall_s:.0f} s")
    rec.wall_s = round(time.monotonic() - t0, 1)
    (run_dir / "events.jsonl").write_text(json.dumps({"t": t0_unix, "kind": "start"}) + "\n" +
                                          json.dumps({"t": time.time(), "kind": "end", "exit": code}) + "\n")
    stdout = (run_dir / "stdout.log").read_text(errors="replace")
    report = {}
    if (out / "report.json").exists():
        with contextlib.suppress(ValueError):
            report = json.loads((out / "report.json").read_text())
    extras = report.get("extras") or report  # the CLI writes the report extras at the top level
    outputs = extras.get("outputs") or {}
    glb = Path(outputs["glb"]) if outputs.get("glb") and Path(outputs["glb"]).exists() else find_main_glb(out)
    rec.glb = str(glb) if glb else None
    if glb:
        rec.extra_files = sorted(str(p) for p in glb.parent.rglob("*") if p.is_file() and p != glb)[:200]
    pub = next((ln.split(":", 1)[1].strip() for ln in stdout.splitlines() if ln.startswith("publication:")), None)
    gates = next((ln.split(":", 1)[1].strip() for ln in stdout.splitlines() if ln.startswith("preview gates:")), None)
    rec.pipeline["own_spend"] = next((ln.strip() for ln in stdout.splitlines() if ln.startswith("spend")), None)
    rec.pipeline.update({"exit_code": code, "publication": pub, "preview_gates": gates,
                         "decision_backend": extras.get("decision_backend"), "preview": extras.get("preview"),
                         "skipped": extras.get("skipped"), "geometry_candidates": extras.get("geometry_candidates"),
                         "scores": extras.get("scores"), "plan": (extras.get("cost") or {}).get("plan"),
                         "pyramid_steps": [(s_.get("name"), s_.get("status"), (s_.get("detail") or "")[:200])
                                           for s_ in ((extras.get("pyramid") or {}).get("steps") or [])],
                         "stdout_tail": stdout[-4000:], "stderr_tail": (run_dir / "stderr.log").read_text(errors="replace")[-3000:]})
    if glb:  # the pipeline's own record of its output: shown as its self-report, never scored
        man = glb.parent / "manifest.json"
        with contextlib.suppress(OSError, ValueError):
            d = json.loads(man.read_text())
            rec.pipeline["self_report"] = {"manifests": [{"path": str(man), "asset": d.get("asset"), "verified": d.get("verified"),
                                                          "preview": d.get("preview"), "tris": d.get("tris"), "judge": d.get("judge"),
                                                          "spend": d.get("spend"), "evidence": d.get("evidence")}], "qa_records": []}
    with contextlib.suppress(OSError):
        rec.pipeline["load_1min_end"] = float(Path("/proc/loadavg").read_text().split()[0])
    rec.pipeline["load_1min_start"] = load_start
    if acc:
        relay.close(name)
        rec.model_calls, rec.models, rec.refused = acc.calls, acc.models, list(acc.refused)
        rec.tool_calls = None
        rec.tokens = _account_tokens(relay.ledger, name)
        rec.images = rec.tokens["images"]
        rec.pipeline["conditions"] = run_conditions(run_dir)
    rec.blender_invocations = None  # a warm worker pool: one Blender process serves many operations
    if acc and any("spend limit" in x for x in acc.refused):
        rec.status = "stopped at limit"
    elif code is None:
        rec.status = "timeout"
    elif glb is None and acc and (svc := rec.pipeline["conditions"].get("last_call_service_failure")):
        rec.status = "stopped by the service"
        rec.notes.append(f"the last model call was {svc} by the service; failures this run: "
                         f"{rec.pipeline['conditions']['service_failures']}")
    elif BUDGET.get("paired") and code != 0:
        # A diagnostic GLB can exist after a failed preview gate. Preserve it, but do not count a completed run.
        rec.status = "failed gates" if code == 1 and glb is not None else "crashed"
    elif glb is None:
        # 0 and 1 are the command's own outcomes, and 3 (--strict: delivered, review below target; never without a package)
        rec.status = "crashed" if code not in (0, 1, 3) else "gave up"
    else:
        rec.status = "finished"
    (run_dir / "record.json").write_text(json.dumps(asdict(rec), indent=1))
    return rec


# ----------------------------------------------------------------------------------------------- orchestration


def plan_table() -> str:
    b = BUDGET
    lines = [f"Ceilings enforced: {b['ceiling_tokens']:,} tokens, {b['ceiling_images']} images (input + output + thinking, "
             "all arms, all judging).", "", "| Step | Tokens (share) | Images (share) | Status |", "|---|---:|---:|---|"]
    if b["smoke_tokens"]:
        lines.append(f"| smoke run (crate:A, real API) | {b['smoke_tokens']:,} | 0 | first: proves relay, ledger and refusal |")
    for br, arm in order(b["briefs"], b["arms_planned"]):
        if b.get("paired"):
            task_mode = (f"fixed {b['c_candidates']} candidates" if arm == "C"
                         else f"task says about {calls_allowed(arm)} model calls")
            lines.append(f"| run {br}:{arm} | {b['run_tokens'][arm]:,} | {b['run_images'][arm]} | "
                         f"now; {task_mode}; {C_TIMEOUT_S // 60} min |")
            continue
        status = "now" if arm in b["arms_now"] else (f"reserved, pending: {PENDING[arm][:70]}..." if arm in PENDING
                                                     else "planned, not in --arms")
        if arm == "C":
            lines.append(f"| run {br}:C | {b['run_tokens']['C']:,} | {b['run_images']['C']} | {status}; the command's own "
                         f"--token-ceiling {b['run_tokens']['C'] - C_TOKEN_MARGIN:,} --max-images {b['run_images']['C']} |")
        else:
            hb = harness_budget(arm, b["run_tokens"][arm])
            lines.append(f"| run {br}:{arm} | {b['run_tokens'][arm]:,} | {b['run_images'][arm]} | {status}; task says about "
                         f"{calls_allowed(arm)} model calls; harness stops at {hb['max_model_calls']} calls, "
                         f"{ARM_TIMEOUT_S.get(arm, STOCK_TIMEOUT_S) // 60} min |")
        if arm == "C":
            lines.append(f"| repeat {br}:C (same request, same bytes?) | {change_share('C')[0]:,} | 0 | "
                         "after the run delivers; the same command into a copy of its folder |")
        if b.get("changes"):
            ct, ci = change_share(arm)
            how = (f"C_CHANGE_MODE={C_CHANGE_MODE}" if arm == "C" else f"new session, about {calls_allowed(arm, ct)} model calls")
            lines.append(f"| change {br}:{arm} ({CHANGES[br]['text']!r}) | {ct:,} | {ci} | after the run delivers; {how} |")
    n_now = judge_calls_planned(b["briefs"], b["arms_now"])
    n_all = judge_calls_planned(b["briefs"], b["arms_planned"])
    lines.append(f"| judge, {n_now} calls now + {n_all - n_now} when the pending arms exist ({JUDGE_MODEL}, thinking "
                 f"{JUDGE_THINKING}, max output {JUDGE_MAX_OUTPUT:,}) | {n_all * b['judge_tokens_per_call']:,} | 0 | "
                 f"{b['judge_tokens_per_call']:,} per call |")
    if b.get("small_judge_tokens_per_call"):
        lines.append(f"| game-size judge, {n_all} calls (same judge, 128 px views) | {n_all * b['small_judge_tokens_per_call']:,} "
                     f"| 0 | {b['small_judge_tokens_per_call']:,} per call |")
    for br, why in ({} if b.get("paired") else NOT_RUN).items():
        if br not in b["briefs"]:
            lines.append(f"| {br if br.startswith('arm ') else 'brief ' + br} | 0 | 0 | {why} |")
    t = plan_totals()
    lines.append(f"| **total** | **{t['tokens']:,}** of {b['ceiling_tokens']:,} | **{t['images']}** of {b['ceiling_images']} | "
                 f"unallocated: {b['ceiling_tokens'] - t['tokens']:,} tokens, {b['ceiling_images'] - t['images']} images |")
    return "\n".join(lines)


def round1_findings(root: Path) -> dict[str, Any]:
    """Round 1's stopped runs, as evidence: per run its model calls, tokens, how it ended, and what the agent did with
    them (the commands it ran, the files it read). Read from the logs; nothing is moved or rewritten."""
    out: dict[str, Any] = {}
    for d in sorted((root / "runs").glob("*")):
        if not d.is_dir() or d.name.startswith((f"{BUDGET.get('round')}_", "smoke")) or not (d / "relay_calls.jsonl").exists():
            continue
        calls = [json.loads(x) for x in (d / "relay_calls.jsonl").read_text().splitlines()]
        ok = [c for c in calls if c.get("status") == 200 and c.get("usage")]
        did = [{"tool": f["name"], "what": (f.get("args") or {}).get("AbsolutePath") or str((f.get("args") or {}).get("CommandLine") or "")[:160]}
               for c in ok for f in c.get("function_calls") or []]
        out[d.name] = {"model_calls": len(ok), "tokens": sum(sum(c["usage"].get(k, 0) for k in ("input", "output", "thinking")) for c in ok),
                       "input_per_call": [c["usage"].get("input") for c in ok], "refused": [c.get("refused") for c in calls if c.get("refused")],
                       "record": json.loads((d / "record.json").read_text()).get("status") if (d / "record.json").exists() else "stopped by the advisor (no record)",
                       "glb": bool(list((d / "work" / "output").glob("*.glb"))) if (d / "work" / "output").is_dir() else False,
                       "actions": did, "conditions": run_conditions(d, DEFAULT_PILOT / "loadavg_live_ab.log")}
    return out


def collect_records(root: Path) -> list[RunRecord]:
    """Every finished cell under ``root`` (not the smoke run), in plan order, whenever it was run."""
    recs = {}
    prefix = f"{BUDGET['round']}_" if BUDGET.get("round") else ""
    sessions: dict[tuple[str, str], list[RunRecord]] = {}
    for p in sorted((root / "runs").glob("*/record.json")):
        if p.parent.name.startswith("smoke") or (prefix and not p.parent.name.startswith(prefix)):
            continue
        r = RunRecord(**json.loads(p.read_text()))
        if r.phase != "first":
            continue
        sessions.setdefault((r.brief, r.arm), []).append(r)
    for key, rs in sessions.items():
        recs[key] = merge_sessions(rs)
    planned = order(BUDGET["briefs"], BUDGET["arms_planned"])
    return [recs[k] for k in planned if k in recs] + [r for k, r in recs.items() if k not in planned]


def make_relay(root: Path, *, live: bool, allow_new_ceilings: bool = False, use_blender: bool = True,
               scripts: dict[str, list[dict[str, Any]]] | None = None) -> tuple[Relay, Any]:
    ledger = Ledger(root / "ledger.json", allow_new_ceilings=allow_new_ceilings)
    if live:
        return Relay(ledger, UPSTREAM, os.environ.get("GEMINI_API_KEY"), fake=False), None
    if scripts is None:
        scripts = {cell_name(br, a): scripted_agent(use_blender) for br in BRIEF_BY_ID for a in ("A", "B")}
        scripts |= {cell_name(br, "V"): scripted_vanilla_agent() for br in BRIEF_BY_ID}
        scripts |= {cell_name(br, a): scripted_plugin_agent() for br in BRIEF_BY_ID for a in PLUGIN_ARMS}
        scripts |= {change_name(br, a): scripted_change(a, use_blender) for br in BRIEF_BY_ID for a in STOCK_ARMS}
        scripts |= {continuation_name(br, "A", 2): scripted_continuation() for br in BRIEF_BY_ID}
        scripts |= {f"smoke:{br}:{a}": scripted_agent(use_blender) for br in BRIEF_BY_ID for a in ("A", "B")}
    fake = FakeUpstream(scripts)
    return Relay(ledger, fake.url, None, fake=True), fake


def native_sizing(run_dir: Path | None = None) -> dict[str, Any] | None:
    """The measured finding about the native arm (stock agent driving the plugin): from the error-free scripted run
    against the stand-in API (pilot/sizeC), the agent's calls and the plugin's own, and what the whole costs."""
    run_dir = run_dir or DEFAULT_PILOT / "sizeC" / "runs" / "crate_C"
    log = run_dir / "relay_calls.jsonl"
    if not log.exists():
        return None
    rows = [json.loads(x) for x in log.read_text().splitlines()]
    agent = [r for r in rows if r.get("status") == 200 and r.get("model") == DEFAULT_TEXT_MODEL and r.get("usage")]
    plugin = [r for r in rows if r.get("status") == 200 and r.get("model") and r.get("model") != DEFAULT_TEXT_MODEL]
    est = [r["est_input_tokens"] for r in agent]
    plugin_measured = 6 * (2_312 + 169 + 837) + 2 * (62 + 2_001 + 804)  # round 1's live judge call; the measured 2K image
    out = {"method": "against a stand-in API (FakeUpstream), an error-free scripted happy path (scripted_plugin_happy_path), "
                     "preview tree, sandboxed; inputs from the relay's per-request estimate (3 bytes per token) and the "
                     "actual/estimate ratio round 1 measured live (0.70 to 0.95)",
           "agent_calls": len(agent), "plugin_calls": len([r for r in plugin if r.get("usage") is not None]),
           "agent_input_estimate_first_last": [est[0], est[-1]] if est else None, "agent_input_estimate_sum": sum(est),
           "plugin_own_tokens_approx": plugin_measured, "logs": str(run_dir)}
    totals = {}
    for ratio in (0.75, 0.85, 0.95):
        t = sum(e * ratio for e in est) + len(agent) * 3_000 + 10_000 + plugin_measured
        totals[str(ratio)] = round(t + (est[-1] + STOCK_MAX_OUTPUT if est else 0))
    out["share_needed_by_ratio"] = totals
    out["agent_to_plugin_cost"] = round((min(totals.values()) - plugin_measured) / plugin_measured, 1)
    out["consequence"] = ("driving the plugin tool by tool through the agent costs roughly "
                          f"{out['agent_to_plugin_cost']:.0f} times what the plugin's own model calls cost, because every tool "
                          "call re-sends the agent's whole context")
    return out


def cell_name(brief: str, arm: str) -> str:
    """A cell's ledger account (and, with ':' as '_', its run directory) in the current round."""
    return f"{BUDGET['round']}:{brief}:{arm}" if BUDGET.get("round") else f"{brief}:{arm}"


def run_smoke(root: Path, *, live: bool, brief: str = "crate", arm: str = "A") -> RunRecord:
    """One stock run under a small share and no image: against the real API it shows that calls are forwarded and
    counted, and that the call which could pass the share is refused before it is sent."""
    relay, fake = make_relay(root, live=live)
    try:
        rec = run_stock(relay, root, BRIEF_BY_ID[brief], arm, share_tokens=BUDGET["smoke_tokens"], share_images=0, tag="smoke:")
    finally:
        if fake is not None:
            fake.close()
    rec.notes.append("smoke run: not one of the pilot's cells")
    return rec


def run_all(root: Path, *, live: bool, only: list[str] | None, use_blender: bool, skip_eval: bool,
            c_against_fake: bool = False, allow_new_ceilings: bool = False, c_native: bool = False,
            allow_c: bool = False,
            continue_cells: list[str] | None = None, change_cells: list[str] | None = None,
            repeat_cells: list[str] | None = None) -> dict[str, Any]:
    """Run the cells asked for (``only``, else the arms run now), their continuations and change requests, then
    evaluate, judge (full size and game size), score and write the gallery. Nothing already recorded is run again."""
    contract = None
    if BUDGET.get("paired"):
        if continue_cells or change_cells or repeat_cells or c_native:
            raise ValueError("paired comparison uses one CLI/stock session per cell; no continuations, changes, repeats or native-arm substitution")
        if live and not allow_c:
            raise ValueError("paired live execution includes C and needs --allow-c")
        contract = bind_comparison_contract(root, live=live, c_against_fake=c_against_fake, use_blender=use_blender)
    root.mkdir(parents=True, exist_ok=True)
    relay, fake = make_relay(root, live=live, allow_new_ceilings=allow_new_ceilings, use_blender=use_blender)
    ledger = relay.ledger
    todo = [] if (continue_cells or change_cells or repeat_cells) and not only else order()
    if only:
        want = {tuple(x.split(":")) for x in only}
        todo = [o for o in order(BUDGET["briefs"], BUDGET["arms_planned"]) if o in want]
    offline_c = not (live or c_against_fake)
    try:
        for b, arm in todo:
            brief = BRIEF_BY_ID[b]
            print(f"[pilot] run {b}:{arm} ...", flush=True)
            if arm == "C" and not c_native:  # round 2: the project's pipeline from its command line, preview mode
                rec = run_c(relay, root, brief, offline=offline_c)
            else:
                rec = run_stock(relay, root, brief, arm,
                                wall_s=C_TIMEOUT_S if BUDGET.get("paired") else None,
                                plugin=True if c_native and arm == "C" else None)
            print(f"[pilot]   {rec.status}; glb={rec.glb}; {rec.tokens.get('total', 0):,} tokens; {rec.wall_s:.0f} s", flush=True)
        for cell in continue_cells or []:
            b, arm = cell.split(":")
            print(f"[pilot] continue {cell} (session {BUDGET['continuations'][cell]['session']}) ...", flush=True)
            rec = run_continuation(relay, root, BRIEF_BY_ID[b], arm, offline=offline_c)
            print(f"[pilot]   {rec.status}; glb={rec.glb}; {rec.tokens.get('total', 0):,} tokens; {rec.wall_s:.0f} s", flush=True)
        for cell in repeat_cells or []:
            b, arm = cell.split(":")
            if arm != "C":
                raise SystemExit(f"--repeat {cell}: only arm C can be asked the same request again")
            print(f"[pilot] repeat {cell} (same request, same bytes?) ...", flush=True)
            rec = run_repeat(relay, root, BRIEF_BY_ID[b], offline=offline_c)
            print(f"[pilot]   {rec.status}; same bytes: {rec.pipeline.get('same_bytes')}; {rec.tokens.get('total', 0):,} tokens", flush=True)
        for cell in change_cells or []:
            b, arm = cell.split(":")
            print(f"[pilot] change {cell}: {CHANGES[b]['text']!r} ...", flush=True)
            rec = run_change(relay, root, BRIEF_BY_ID[b], arm, offline=offline_c)
            d = (rec.pipeline or {}).get("diff") or {}
            print(f"[pilot]   {rec.status}; glb={rec.glb}; {rec.tokens.get('total', 0):,} tokens; {rec.wall_s:.0f} s; "
                  f"parts changed {len(d.get('changed') or [])}, unchanged {d.get('unchanged')}", flush=True)
        results = assemble(root, ledger, live=live, fake=fake)
        if contract:
            results["comparison_contract"] = contract
        rnd = BUDGET.get("round")
        (root / (f"runs_{rnd}.json" if rnd else "runs.json")).write_text(json.dumps(results, indent=1, default=str))
        records = [RunRecord(**r) for r in results["records"]]
        if not skip_eval:
            import compare_eval as ev

            results["evaluation"] = asyncio.run(ev.evaluate_all(root, records, BRIEF_BY_ID, BUDGET.get("round", "")))
            results["judge"] = ev.judge_all(root, records, BRIEF_BY_ID, relay, BUDGET["judge_tokens_per_call"], JUDGE_MODEL,
                                            JUDGE_THINKING, JUDGE_MAX_OUTPUT, BUDGET.get("round", ""))
            if BUDGET.get("small_judge_tokens_per_call"):
                results["judge_small"] = ev.judge_all(root, records, BRIEF_BY_ID, relay, BUDGET["small_judge_tokens_per_call"],
                                                      JUDGE_MODEL, JUDGE_THINKING, JUDGE_MAX_OUTPUT, BUDGET.get("round", ""),
                                                      small=True)
        else:  # keep the last evaluation and judgements in the results instead of dropping them
            load_saved_eval(root, results)
        if contract:
            results["shared_constraints"] = paired_constraints(records, results.get("evaluation", {}))
        results["tally"] = ledger.totals() | {"ceiling_tokens": ledger.data["ceiling_tokens"], "ceiling_images": ledger.data["ceiling_images"]}
        results["ledger"] = ledger.data
        if not skip_eval:
            import compare_eval as ev

            results["scorecard"] = ev.scorecard(root, results, BRIEF_BY_ID, BUDGET.get("round", ""))
        (root / (f"results_{rnd}.json" if rnd else "results.json")).write_text(json.dumps(results, indent=1, default=str))
        if not skip_eval:
            import compare_eval as ev

            ev.write_gallery(root, results, BRIEF_BY_ID, ARM_LABEL, name=f"gallery_{rnd}.html" if rnd else "gallery.html")
        return results
    finally:
        if fake is not None:
            fake.close()


def assemble(root: Path, ledger: Ledger | None, *, live: bool, fake: Any = None) -> dict[str, Any]:
    """The results document from what is on disk: records, change requests, the design notes."""
    records = collect_records(root)
    results: dict[str, Any] = {"mode": "live" if live else "dry-run", "records": [asdict(r) for r in records],
                               "changes": [asdict(r) for r in collect_changes(root)], "change_requests": CHANGES,
                               "repeats": [asdict(r) for r in collect_changes(root, "repeat")],
                               "c_change_mode": C_CHANGE_MODE,
                               "unfair": list(PAIRED_LIMITATIONS if BUDGET.get("paired") else UNFAIR), "plugin_discovery": PLUGIN_DISCOVERY,
                               "not_run": {**NOT_RUN, **{f"arm {k}": v for k, v in PENDING.items()
                                                         if k not in {r.arm for r in records}}}}
    rnd = BUDGET.get("round")
    results["round"] = rnd
    if BUDGET.get("paired"):
        results["not_run"] = {f"{b}:{a}": "planned cell has no run record"
                              for b, a in order(BUDGET["briefs"], BUDGET["arms_planned"])
                              if (b, a) not in {(r.brief, r.arm) for r in records}}
    elif rnd:
        results["native_sizing"] = native_sizing()
        results["c_unexpressed"] = list(C_UNEXPRESSED)
    results["plan"] = {"briefs": [asdict(BRIEF_BY_ID[b]) for b in BUDGET["briefs"]], "order": order(), "engine": ENGINE,
                       "notes": plan_notes(), "budget": BUDGET, "plan_totals": plan_totals()}
    if ledger is not None:
        results["tally"] = ledger.totals() | {"ceiling_tokens": ledger.data["ceiling_tokens"],
                                              "ceiling_images": ledger.data["ceiling_images"]}
    if fake is not None:
        results["fake_upstream_requests"] = len(fake.requests)
    return results


def load_saved_eval(root: Path, results: dict[str, Any]) -> None:
    rnd = BUDGET.get("round")
    for key, path in (("evaluation", root / (f"evaluation_{rnd}.json" if rnd else "evaluation.json")),
                      ("judge", root / "judge" / (rnd or "") / "judge.json"),
                      ("judge_small", root / "judge_small" / (rnd or "") / "judge.json")):
        if path.exists():
            with contextlib.suppress(ValueError):
                results[key] = json.loads(path.read_text())


def score_only(root: Path) -> dict[str, Any]:
    """The scorecard and gallery from what is on disk (``score``): no run, no evaluation, no judge call."""
    import compare_eval as ev

    global BUDGET
    original = BUDGET
    path = root / "comparison-contract.json"
    contract = json.loads(path.read_text()) if path.exists() else None
    try:
        if contract:
            BUDGET = contract["budget"]
        results = assemble(root, None, live=False)
        results["mode"] = "scored from disk"
        ledger_path = root / "ledger.json"
        if ledger_path.exists():
            data = json.loads(ledger_path.read_text())
            results["ledger"] = data
            results["tally"] = Ledger.snapshot_totals(data) | {"ceiling_tokens": data["ceiling_tokens"],
                                                             "ceiling_images": data["ceiling_images"]}
        load_saved_eval(root, results)
        rnd = BUDGET.get("round", "")
        if contract:
            results["comparison_contract"] = contract
            records = [RunRecord(**r) for r in results["records"]]
            results["shared_constraints"] = paired_constraints(records, results.get("evaluation", {}))
        results["scorecard"] = ev.scorecard(root, results, BRIEF_BY_ID, rnd)
        ev.write_gallery(root, results, BRIEF_BY_ID, ARM_LABEL, name=f"gallery_{rnd}.html" if rnd else "gallery.html")
        return results
    finally:
        BUDGET = original


def apply_budget_flags(a: argparse.Namespace) -> None:
    if a.paired:
        if a.cmd not in {"plan", "dry-run", "live"}:
            raise ValueError("--paired is available for plan, dry-run and live")
        if not a.round or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", a.round) or a.round in {"r1", "r2"}:
            raise ValueError("--paired needs a fresh --round name (letters, numbers, '-' and '_'; not r1/r2)")
        if a.c_candidates is None:
            raise ValueError("--paired needs explicit --c-candidates single|default")
        for flag, label in ((a.run_tokens, "--run-tokens"), (a.run_images, "--run-images")):
            values = dict(item.split("=", 1) for item in flag)
            if len(flag) != 2 or set(values) != {"A", "C"} or len({int(v) for v in values.values()}) != 1:
                raise ValueError(f"--paired needs equal explicit A=N C=N shares for {label}")
            if min(int(v) for v in values.values()) <= 0:
                raise ValueError(f"--paired needs positive {label} shares")
        if a.ceiling_tokens is None or a.ceiling_images is None:
            raise ValueError("--paired needs explicit --ceiling-tokens and --ceiling-images")
        if int(a.run_tokens[0].split("=", 1)[1]) <= C_TOKEN_MARGIN:
            raise ValueError("paired token shares must exceed C's internal admission margin")
        if a.arms is not None and (len(a.arms) != 2 or set(a.arms) != {"A", "C"}):
            raise ValueError("--paired compares A and C; use --only to execute a planned cell separately")
        if (getattr(a, "continue_cells", None) or getattr(a, "change_cells", None) or getattr(a, "repeat_cells", None)
                or getattr(a, "c_native", False) or getattr(a, "change_images", None)):
            raise ValueError("--paired uses one CLI/stock session per cell; no continuations, changes, repeats or native-arm substitution")
        BUDGET.update(paired=True, round=a.round, c_candidates=a.c_candidates, briefs=["crate"],
                      arms_now=["A", "C"], arms_planned=["A", "C"], continuations={}, smoke_tokens=0,
                      run_tokens={}, run_images={}, changes=False, change_images={}, small_judge_tokens_per_call=0)
    elif a.round is not None or a.c_candidates is not None:
        raise ValueError("--round and --c-candidates require --paired")
    if a.ceiling_tokens is not None:
        BUDGET["ceiling_tokens"] = a.ceiling_tokens
    if a.ceiling_images is not None:
        BUDGET["ceiling_images"] = a.ceiling_images
    if a.briefs:
        unknown = set(a.briefs) - set(BRIEF_BY_ID)
        if unknown:
            raise SystemExit(f"unknown brief(s): {sorted(unknown)}")
        BUDGET["briefs"] = list(a.briefs)
    if a.arms:
        BUDGET["arms_now"] = list(a.arms)
        BUDGET["arms_planned"] = sorted(set(BUDGET["arms_planned"]) | set(a.arms))
    for flag, key in ((a.run_tokens, "run_tokens"), (a.run_images, "run_images"), (getattr(a, "change_images", []), "change_images")):
        for item in flag:
            arm, n = item.split("=")
            if arm not in ARMS or int(n) < 0:
                raise ValueError(f"invalid {key} share: {item}")
            BUDGET[key][arm] = int(n)
    if BUDGET.get("paired"):
        if not BUDGET["briefs"] or len(set(BUDGET["briefs"])) != len(BUDGET["briefs"]):
            raise ValueError("paired briefs must be nonempty and unique")
        planned = {f"{b}:{arm}" for b, arm in order()}
        if any(cell not in planned for cell in getattr(a, "only", None) or []):
            raise ValueError("--only must name a cell in the paired plan")


def check_plan(ledger_path: Path | None = None) -> str | None:
    """Everything the ledger holds (closed accounts at what they used, open ones at their share) plus every account the
    plan may still open (at its share) must fit the ceilings. Without a ledger, the plan alone."""
    if ledger_path is None or not ledger_path.exists():
        t = plan_totals()
    else:
        data = json.loads(ledger_path.read_text())
        t = {"tokens": 0, "images": 0}
        for a in data["accounts"].values():
            t["tokens"] += (Ledger.used_tokens(a) + a.get("inflight_tokens", 0)) if a.get("closed") else a["share_tokens"]
            t["images"] += a["images"] if a.get("closed") else a["share_images"]
        for name, (tok, img) in planned_accounts().items():
            if name not in data["accounts"]:
                t["tokens"] += tok
                first = data["accounts"].get(name.rsplit(".s", 1)[0]) if ".s" in name.rsplit(":", 1)[-1] else None
                if ".s" in name.rsplit(":", 1)[-1] and not (first and first.get("closed")):
                    img = 0  # still counted with the first session, whose account is open
                t["images"] += img
        t["tokens"] += BUDGET["smoke_tokens"] if "smoke:crate:A" not in data["accounts"] else 0
    if t["tokens"] > BUDGET["ceiling_tokens"]:
        return f"the plan needs {t['tokens']:,} tokens, more than the ceiling of {BUDGET['ceiling_tokens']:,}"
    if t["images"] > BUDGET["ceiling_images"]:
        return f"the plan needs {t['images']} images, more than the ceiling of {BUDGET['ceiling_images']}"
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan").add_argument("--json", action="store_true", help="print the paired plan as JSON (no execution)")
    pc = sub.add_parser("close", help="close finished accounts at what they used (logged in the ledger)")
    pc.add_argument("--root", type=Path, required=True)
    pc.add_argument("--accounts", nargs="+", required=True)
    pc.add_argument("--reason", required=True)
    for name in ("dry-run", "live", "smoke"):
        p = sub.add_parser(name)
        p.add_argument("--root", type=Path, default=None)
        p.add_argument("--resume", action="store_true",
                       help="continue a ledger that already shows spending (refused otherwise)")
        if name != "smoke":
            p.add_argument("--only", nargs="*", default=None, help="BRIEF:ARM, e.g. crate:A")
            p.add_argument("--skip-eval", action="store_true")
        if name in ("live", "smoke"):
            p.add_argument("--go", action="store_true", help="required for real calls: the advisor said go")
        if name == "smoke":
            p.add_argument("--fake", action="store_true", help="against the local stand-in instead of the real API")
        if name == "dry-run":
            p.add_argument("--no-blender", action="store_true", help="scripted agent writes the GLB without Blender")
            p.add_argument("--c-native", action="store_true",
                           help="legacy native C sizing run; prefer --arms N (not available with --paired)")
            p.add_argument("--c-against-fake", action="store_true",
                           help="arm C's command line without --offline, its model calls answered by the local stand-in")
        if name == "live":
            p.add_argument("--allow-c", action="store_true", help="explicitly enable C in a paired live comparison")
        if name != "smoke":
            p.add_argument("--continue", dest="continue_cells", nargs="*", default=None, metavar="BRIEF:ARM",
                           help="run the planned continuation of a stopped cell (BUDGET['continuations']), then evaluate")
            p.add_argument("--change", dest="change_cells", nargs="*", default=None, metavar="BRIEF:ARM",
                           help="run the change request (CHANGES) of a cell that delivered, then evaluate")
            p.add_argument("--repeat", dest="repeat_cells", nargs="*", default=None, metavar="BRIEF:C",
                           help="arm C's command again into a copy of its folder: same request, same bytes?")
    ps = sub.add_parser("score", help="the scorecard and gallery from what is on disk (no run, no model call)")
    ps.add_argument("--root", type=Path, required=True)
    for p in sub.choices.values():
        p.add_argument("--paired", action="store_true", help="fresh A/C comparison with equal explicit shares and fixed candidate mode")
        p.add_argument("--round", default=None, help="fresh paired experiment name")
        p.add_argument("--c-candidates", choices=("single", "default"), default=None,
                       help="paired arm C's candidate mode; never inferred from remaining images")
        p.add_argument("--project-src", type=Path, default=None, help="the project tree arm C attaches (default: this one)")
        p.add_argument("--ceiling-tokens", type=int, default=None)
        p.add_argument("--ceiling-images", type=int, default=None)
        p.add_argument("--briefs", nargs="*", default=None)
        p.add_argument("--arms", nargs="*", default=None, help="arms to run now")
        p.add_argument("--run-tokens", nargs="*", default=[], metavar="ARM=N", help="per-run token share of an arm")
        p.add_argument("--run-images", nargs="*", default=[], metavar="ARM=N", help="per-run image share of an arm")
        p.add_argument("--change-images", nargs="*", default=[], metavar="ARM=N",
                       help="image share of an arm's change session (default: N and C as their run, others 0)")
    a = ap.parse_args(argv)
    if a.cmd == "score":
        apply_budget_flags(a)
        sys.path.insert(0, str(HERE))
        res = score_only(a.root)
        print(json.dumps(res["scorecard"].get("summary"), indent=1))
        return 0
    if a.cmd == "close":
        led = Ledger(a.root / "ledger.json", allow_new_ceilings=False)
        for name in a.accounts:
            print(json.dumps(led.close_account(name, a.reason)))
        print(json.dumps(led.totals()))
        return 0
    try:
        apply_budget_flags(a)
    except ValueError as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 2
    if a.project_src is not None:
        global PROJECT_SRC
        PROJECT_SRC = a.project_src.resolve()
    if (why := check_plan()) is not None:
        print(f"refusing: {why}", file=sys.stderr)
        return 2
    if a.cmd == "plan" and getattr(a, "json", False):
        if not BUDGET.get("paired"):
            print("refusing: --json requires --paired", file=sys.stderr)
            return 2
        print(json.dumps(comparison_contract(), indent=2))
        return 0
    print(plan_table(), flush=True)
    if a.cmd == "plan":
        for k, v in plan_notes().items():
            print(f"- {k}: {v}")
        for bid in BUDGET["briefs"]:
            b = BRIEF_BY_ID[bid]
            print(f"- brief {b.id}: {b.prompt!r} (<= {b.max_tris} tris, {b.size_m[0]}-{b.size_m[1]} m): {b.why}")
        if BUDGET.get("paired"):
            print(f"- paired mode: C candidates={BUDGET['c_candidates']}; no continuations; CLI MeteredAuthor, not native AgentAuthor")
            print("- live execution requires separate explicit approval; this plan makes no model calls")
        return 0
    sys.path.insert(0, str(HERE))
    live = a.cmd == "live" or (a.cmd == "smoke" and not a.fake)
    if live:
        if not a.go:
            print("refusing: real calls need --go (after the advisor's go)", file=sys.stderr)
            return 2
        if BUDGET.get("paired") and not getattr(a, "allow_c", False):
            print("refusing: paired live execution includes C and needs --allow-c", file=sys.stderr)
            return 2
        if not os.environ.get("GEMINI_API_KEY"):
            print("refusing: GEMINI_API_KEY is not set", file=sys.stderr)
            return 2
    root = a.root or (DEFAULT_PILOT / ("live" if live else "dryrun"))
    if BUDGET.get("paired"):
        if a.root is None:
            print("refusing: --paired needs an explicit fresh --root", file=sys.stderr)
            return 2
        try:
            bind_comparison_contract(root, live=live, c_against_fake=getattr(a, "c_against_fake", False),
                                     use_blender=not getattr(a, "no_blender", False))
        except ValueError as exc:
            print(f"refusing: {exc}", file=sys.stderr)
            return 2
    ledger_path = root / "ledger.json"
    if (why := check_plan(ledger_path)) is not None:
        print(f"refusing: {why} (counting what {ledger_path} has booked)", file=sys.stderr)
        return 2
    if ledger_path.exists():
        try:
            spent = Ledger(ledger_path, allow_new_ceilings=True).spent() if a.resume else \
                Ledger(ledger_path, allow_new_ceilings=False).spent()
        except ValueError as e:  # other ceilings in the file: only an explicit resume may change them
            print(f"refusing: {e}; pass --resume with the new ceilings to continue this ledger", file=sys.stderr)
            return 2
        if spent and not a.resume:
            print(f"refusing: {ledger_path} already shows spending; pass --resume to continue it", file=sys.stderr)
            return 2
    print(f"[pilot] {'LIVE' if live else 'dry run'}; ledger {ledger_path}; enforcing {BUDGET['ceiling_tokens']:,} tokens and "
          f"{BUDGET['ceiling_images']} images in total", flush=True)
    if a.cmd == "smoke":
        rec = run_smoke(root, live=live)
        print(json.dumps({k: getattr(rec, k) for k in ("status", "model_calls", "tokens", "refused", "wall_s", "notes")}, indent=1))
        return 0
    res = run_all(root, live=live, only=a.only, use_blender=not getattr(a, "no_blender", False), skip_eval=a.skip_eval,
                  c_against_fake=getattr(a, "c_against_fake", False), allow_new_ceilings=a.resume,
                  c_native=getattr(a, "c_native", False), allow_c=getattr(a, "allow_c", False),
                  continue_cells=getattr(a, "continue_cells", None), change_cells=getattr(a, "change_cells", None),
                  repeat_cells=getattr(a, "repeat_cells", None))
    print(json.dumps(res.get("tally"), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
