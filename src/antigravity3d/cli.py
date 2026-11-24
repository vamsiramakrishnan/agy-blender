"""``agy3d``: command line for the Layer-4 pipelines.

    agy3d prop "wooden crate with iron brackets" --engine unreal_5 --out OUT [--offline] [--max-tris N]
                [--style anchor.png] [--art-direction ID]
    agy3d sanitize FILES... --engine unreal_5 --out OUT
    agy3d kit "ruined stone temple" --pieces 4 --out OUT [--offline] [--splat bg.ply] [--metric interior|exterior[,key=value...]] [--art-direction ID]
    agy3d variants ASSET.usda... --out OUT [--offline]
    agy3d calibrate LABELS.json --out studio_policy.yaml [--offline]
    agy3d models
    agy3d plugin install [--target DIR] | plugin validate [PATH]
    agy3d install PACKAGE_DIR --project PROJECT_DIR [--dest DIR_IN_PROJECT] [--dry-run] [--force] [--no-check] [--json]
    agy3d uninstall --project PROJECT_DIR --dest DIR_IN_PROJECT [--dry-run] [--force]
    agy3d edit OUT "<change>" [--token-ceiling N] [--max-images M] [--role ROLE] | edit OUT --patch OPS.json | edit OUT --undo

``--offline`` runs everything on the fakes (FakeDecisionsClient, FakeImageBackend) and the TemplateAuthor.
``--style`` is a style-anchor IMAGE; ``--art-direction`` a named style (``antigravity3d.art_direction``; ids are the
files in the plugin's ``skills/art-direction/styles``). They compose. An unknown id is a usage error (exit 2) raised
while the arguments are parsed, before anything is requested.
Exit status: 0 = every gate passed, 1 = a gate failed, 2 = usage / environment error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any, get_args

from antigravity3d.state import TEXTURE_PX_CHOICES, EngineBudget, TargetEngine

ENGINES = list(get_args(TargetEngine))
# The brief's questions are answered at the fake's defaults (clean, 3.6), stated here so an offline run meets the brief.
OFFLINE_ANSWERS = {"pipeline_dag": "prop_to_engine", "needs_ortho_blueprint": 0.8, "complexity_tier": 2.0,
                   "defect_brief_part_missing": 0.05, "score_brief_depiction": 3.6}


class CliError(Exception):
    """A usage or environment problem (exit 2)."""


def _art_direction(text: str) -> Any:
    """argparse type: the named art direction, or a usage error that lists the valid ids."""
    from antigravity3d.art_direction import UnknownArtDirection, load

    try:
        return load(text)
    except UnknownArtDirection as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def _print_art(art: Any) -> None:
    if art is not None:
        x = art
        print(f"  art direction: {x.id} (tris<={x.tri_budget_prop or 'engine'} prop / {x.tri_budget_kit_piece or 'engine'} kit piece, "
              f"crease {x.crease_deg():g} deg, maps {x.texture_px_prop or 'default'}/{x.texture_px_kit or 'default'} px, "
              f"filter {x.texture_filter or 'engine'}, palette {x.palette_max_colors or 'free'})")


def _budget(a: argparse.Namespace) -> EngineBudget:
    keep = bool(getattr(a, "keep_contact_faces", False))
    extra = {} if getattr(a, "surfaces", None) is None else {"generated_surfaces": a.surfaces == "generated"}
    if getattr(a, "max_texture_px", None):
        extra["max_texture_px"] = a.max_texture_px
    if a.max_tris is not None:
        return EngineBudget(a.engine, max_tris=a.max_tris, keep_contact_faces=keep, **extra)
    return EngineBudget(a.engine, keep_contact_faces=keep, **extra)


def offline_plane() -> Any:
    from antigravity3d.decisions.engine import DecisionControlPlane
    from antigravity3d.decisions.fakes import FakeDecisionsClient

    return DecisionControlPlane(FakeDecisionsClient(answers=dict(OFFLINE_ANSWERS)))


def _client() -> Any:
    if not os.environ.get("GEMINI_API_KEY"):
        raise CliError("GEMINI_API_KEY is not set (use --offline to run on the fakes)")
    from google import genai

    return genai.Client(api_key=os.environ["GEMINI_API_KEY"])


async def make_collaborators(offline: bool, *, need_plane: bool = True, workdir: Any = None) -> tuple[Any, Any, Any, list[str]]:
    """(plane, forge, author, notes) for offline or live operation."""
    from antigravity3d.genmedia.nano_banana import FakeImageBackend, ImageForge
    from antigravity3d.tactics.authoring import TemplateAuthor, best_author

    if offline:
        return (offline_plane() if need_plane else None), ImageForge(FakeImageBackend()), TemplateAuthor(), ["offline: fake decisions, fake images, TemplateAuthor"]
    client = _client()
    notes: list[str] = []
    plane = None
    if need_plane:
        try:
            from antigravity3d.decisions.factory import make_control_plane

            plane = await make_control_plane(client)
        except ImportError:
            from antigravity3d.decisions.engine import DecisionControlPlane

            plane = DecisionControlPlane(client)
        notes.append(f"decision backend: {plane.backend_kind}" + ("" if plane.calibrated else " (UNCALIBRATED: can judge only an unverified preview)"))
    forge = ImageForge(client=client)
    author, note = best_author(client=client, plane=plane, forge=forge, workdir=workdir)
    notes.append(note)
    return plane, forge, author, notes


def _web_lines(manifest_path: Any) -> list[str]:
    """What a web_three delivery says about the three.js check and the page download (nothing for other targets)."""
    from antigravity3d.adapters.engines.web_three import reply_lines

    try:
        return reply_lines(json.loads(Path(manifest_path).read_text())) if manifest_path else []
    except (OSError, ValueError):
        return []


class _EngineGiven(argparse.Action):
    """``--engine``, also recording that it was typed (``namespace.engine_given``): a refusal for the default engine says it is the default."""

    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        namespace.engine_given = True


def _print_skips(skipped: list[dict[str, Any]]) -> None:
    print("skipped:" if skipped else "skipped: none")
    for s in skipped:
        print(f"  - {s['step']}: {s['reason']}")


def _print_record_and_run(c: dict[str, Any]) -> None:
    """The design record's line (reused / recorded) and, on a rerun, what this run added to the asset's ledger."""
    from antigravity3d.tactics.cached import DesignRecord

    if (d := c.get("design_record")) is not None:
        print(DesignRecord.summary_line(d))
    if (t := c.get("this_run")) is not None:
        spent = t.get("spent_tokens", t.get("spent_usd", 0))
        amount = f"{spent:,.0f} tokens" if "spent_tokens" in t else f"${spent:.4f}"
        print(f"this run: {amount} and {t['images']} image(s); the ledger's totals above include the earlier runs of this asset")


@asynccontextmanager
async def _progress(author: Any = None) -> Any:
    """Progress of this command's model calls on stderr (``progress_view``) while the block runs: the author's streamed
    calls through its sink, the judge's calls through the context's sink (``stream_call.CURRENT_SINK``, inherited by
    every task the run starts). Stdout is untouched."""
    from antigravity3d.progress_view import ProgressView
    from antigravity3d.tactics.stream_call import CURRENT_SINK

    view = ProgressView()
    if author is not None and hasattr(author, "progress"):  # AgentAuthor (passes it to the MeteredAuthor it binds) or MeteredAuthor
        author.progress = view
    token = CURRENT_SINK.set(view)
    try:
        async with view:
            yield view
    finally:
        CURRENT_SINK.reset(token)


def _print_gate(ok: bool, why: str = "") -> int:
    print(f"gates: {'PASS' if ok else 'FAIL'}" + (f" ({why})" if why else ""))
    return 0 if ok else 1


# --------------------------------------------------------------------------- start-up checks (before any model call)


def _text_prices_configured() -> bool:
    """True when some authoring role's model has configured text prices (AGY3D_TEXT_RATES_JSON or the price table)."""
    from antigravity3d.models import tier
    from antigravity3d.tactics.governor import BudgetExceeded
    from antigravity3d.tactics.metered_author import _rates

    for role in ("worker", "bulk", "builder", "director"):
        try:
            _rates(tier(role).model)  # type: ignore[arg-type]
            return True
        except BudgetExceeded:
            continue
    return False


def _bound_author(author: Any, a: argparse.Namespace) -> Any:
    """The live author bound to a throwaway token ledger, only to ask it for its worst case (nothing is called)."""
    from antigravity3d.tactics.authoring import AgentAuthor
    from antigravity3d.tactics.governor import Governor, plan_for

    if not isinstance(author, AgentAuthor):
        return author
    return author.bind(Governor(plan_for(1), token_ceiling=a.token_ceiling), _budget(a), {})


def _refuse_unverifiable_target(a: argparse.Namespace, quoted: str) -> None:
    """A run that must be verified, for a target it can never verify on this machine, is refused here: it would make and pay
    for its authoring, judge and image calls and end in "publication requires ... captured engine parity" because the
    target has no engine capture (or its engine is not installed). Same answer for every key (``verification_gap``)."""
    from antigravity3d.adapters.engines.validators import verifiable_targets, verification_gap

    if (gap := verification_gap(a.engine)) is None:
        return
    which = (f"{a.engine} (the default engine, as no --engine was given)" if not getattr(a, "engine_given", False) else a.engine)
    ok = verifiable_targets()
    instead = (f"; to have it verified, use a target that can be verified on this machine: {', '.join(f'--engine {t}' for t in ok)}"
               if ok else "; no target can be verified on this machine (it needs Godot 4, set $GODOT_BIN, or a Chromium browser "
                          "with the Python 'playwright' package, set $CHROMIUM_BIN)")
    raise CliError(f"a verified run for {which} cannot succeed on this machine: {gap}. It would end in a refusal after "
                   "its model calls were made and paid for: nothing was generated. For an UNVERIFIED preview of "
                   f"{a.engine} run: agy3d prop {quoted} --engine {a.engine} --preview --token-ceiling N --max-images M --out {a.out}"
                   + instead)


def preflight_prop(a: argparse.Namespace, plane: Any, author: Any, strict: bool) -> Any:
    """Refuse at start, with what is missing and what to run instead, every live run that would be refused later
    anyway; return the plane the run uses (the preview copy when an uncalibrated judge will judge a preview)."""
    from antigravity3d.tactics.token_plan import author_worst_tokens, judge_worst_tokens, plan_lines

    if a.offline:
        return plane
    quoted = json.dumps(a.prompt)
    uncalibrated = plane is not None and not plane.calibrated
    if uncalibrated and strict:
        raise CliError("the native Decisions API is not available on this key (decisions.create answered 404 or permission "
                       "denied), and a verified run needs it: nothing was generated. For an UNVERIFIED preview judged by the "
                       "generative fallback (uncalibrated), run: "
                       f"agy3d prop {quoted} --preview --token-ceiling N --max-images M --out {a.out}")
    if a.token_ceiling is None:
        if uncalibrated:
            raise CliError("a preview judged by the generative fallback (uncalibrated) has no price to bound its judge in "
                           "dollars: pass your own --token-ceiling N and --max-images M (nothing was generated)")
        from antigravity3d.tactics.authoring import AgentAuthor

        if isinstance(author, AgentAuthor) and not _text_prices_configured():  # the metered author would refuse every call
            raise CliError("no text prices are configured (AGY3D_TEXT_RATES_JSON) for any authoring model, so live authoring "
                           "cannot be bounded in dollars: pass your own --token-ceiling N and --max-images M to bound the "
                           "run in tokens and images instead (nothing was generated)")
        return plane
    if a.max_images is None:
        lines = "; ".join(plan_lines(single_candidate=a.single_candidate, max_rounds=a.max_rounds))
        raise CliError("--token-ceiling needs --max-images: a token says almost nothing about what an image costs. With these "
                       f"flags the router's plans need: {lines}. Pass --max-images M"
                       + ("" if a.single_candidate else " (--single-candidate needs the fewest images: 1 blueprint + 1 per material)"))
    judge, writer = judge_worst_tokens(plane), author_worst_tokens(_bound_author(author, a))
    need = max(judge, writer)
    a.min_call_tokens = need  # a rerun compares it with what is LEFT of the ceiling (prop_to_engine, the route step)
    if a.token_ceiling < need:
        raise CliError(f"--token-ceiling {a.token_ceiling:,} cannot admit this run's calls: one judge call can bill up to "
                       f"{judge:,} tokens and one authoring call up to {writer:,}; the smallest ceiling that can admit them "
                       f"is {need:,} tokens (nothing was generated)")
    return plane.preview_copy() if uncalibrated and not strict else plane


# --------------------------------------------------------------------------- commands


async def cmd_prop(a: argparse.Namespace) -> int:
    from antigravity3d import delivery as D
    from antigravity3d.dags.prop_to_engine import PropToEngineDAG
    from antigravity3d.kernel.cache import BuildCache, LocalBlobStore
    from antigravity3d.tactics.distill import FixLog

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    strict = a.require_verified if a.require_verified is not None else not a.offline
    if a.token_ceiling is None and a.max_images is not None:
        raise CliError("--max-images applies to a token-limited run: pass --token-ceiling N with it")
    if not a.offline and not os.environ.get("GEMINI_API_KEY"):
        raise CliError("GEMINI_API_KEY is not set (use --offline to run on the fakes)")
    if strict and not a.offline:  # the same answer for every key: decided before the Decisions probe, the first model call
        _refuse_unverifiable_target(a, json.dumps(a.prompt))
    async with AsyncExitStack() as stack:
        # The backend probe and the start-up checks come first: a run that would be refused does not start Blender.
        plane, forge, author, notes = await make_collaborators(a.offline, workdir=out / "agent")
        plane = preflight_prop(a, plane, author, strict)  # refuses here, before any model call, what would fail later
        await stack.enter_async_context(_progress(author))
        pool = None
        if a.pool_socket:
            from antigravity3d.runtime.worker_daemon import PoolClient

            pool = await stack.enter_async_context(PoolClient(a.pool_socket))
        elif a.blender or strict:
            from antigravity3d.adapters.blender_5_3.pool import BlenderPool

            pool = await stack.enter_async_context(BlenderPool(1, blender_bin=a.blender))
        cache = None if a.no_cache else BuildCache(LocalBlobStore(out / ".agy3d-cache"))
        dag = PropToEngineDAG(
            plane=plane, forge=forge, author=author, workdir=out, budget=_budget(a), pool=pool,
            cache=cache, style_anchor=a.style, fixlog=FixLog.for_prop(out), max_rounds=a.max_rounds,
            require_verified=strict, single_candidate=a.single_candidate, art_direction=a.art_direction,
            new_design=a.new_design, announce=lambda line: print(f"note: {line}", flush=True), improve_rounds=a.improve_rounds,
            min_call_tokens=getattr(a, "min_call_tokens", None), strict_review=a.strict_review,
            **({"token_ceiling": a.token_ceiling, "max_images": a.max_images} if a.token_ceiling is not None else {}),
        )
        rep = await dag.run(a.prompt)
    x = rep.extras
    for step in ("route", "propose"):  # refusals before any generation of this run: the whole text, with its numbers
        if (rn := rep.nodes.get(step)) is not None and (rn.error or "").startswith(("ImageLimitRefused", "CeilingLeftRefused")):
            raise CliError(rn.error.split(": ", 1)[1])
    print(f"agy3d prop: {a.prompt!r} -> {a.engine}")
    for n in notes:
        print(f"  {n}")
    _print_art(a.art_direction)
    print(rep.summary())
    res = x.get("pyramid") or {}
    if res:
        print("pyramid rounds: " + "; ".join(f"{r['stage']}{r['n']}:{r['action']}{'(rolled back)' if r['rolled_back'] else ''} t15={r['tier15']:.2f}" for r in res["rounds"]))
        print("pyramid steps:  " + ", ".join(f"{s['name']}={s['status']}" for s in res["steps"]))
        print(f"render tier: {res['render_tier']}; stopped: {res['stopped']}")
    sc = x.get("scores") or {}
    t15 = sc.get("tier15", 0)
    t15 = f"{t15:.2f}" if t15 is not None else "n/a (" + (res.get("judge_unavailable") or "never judged") + ")"
    print("scores: " + (f"tier15={t15} tier1={'OK' if sc.get('tier1_ok') else 'FAIL'} " + " ".join(f"{k[6:]}={v:.2f}" for k, v in sc.items() if k.startswith("score_")) if sc else "n/a"))
    c = x["cost"]
    if c.get("spend_unit") == "tokens":  # no price applied: tokens and images only, never a dollar sign
        cs = f" cache hits={c['cache']['hits']} misses={c['cache']['misses']}" if c.get("cache") else ""
        print(f"spend (tokens, no price applied): {c['plan']} | decisions plane {c['plane']['calls']} calls, "
              f"{c['plane']['input_tokens']:,} input + {c['plane']['output_tokens']:,} output tokens{cs}")
    else:
        cs = f" cache hits={c['cache']['hits']} misses={c['cache']['misses']} saved=${c['cache']['saved_cost_usd']:.2f}/{c['cache']['saved_ms']:.0f}ms" if c.get("cache") else ""
        print(f"cost: governor(est) ${c['governor_usd']:.4f} | decisions plane ${c['plane']['cost_usd']:.6f} over {c['plane']['calls']} calls | {c['plan']}{cs}")
    _print_record_and_run(c)
    print("outputs:")
    for k, v in x["outputs"].items():
        print(f"  {k}: {v}")
    print(f"  report: {x['report_path']}")
    for line in _web_lines(x["outputs"].get("manifest")):
        print(line)
    _print_skips(x["skipped"])
    warned = [s for s in res.get("steps", []) if s["status"] == "warned"]
    if warned:
        print("warnings:")
        for w in warned:
            print(f"  - {w['name']}: {w['detail']}")
    for f in res.get("failed_outputs", []):  # an output that could not be made (the FBX): not a blocked package, but plainly a failure
        print(f"FAILED OUTPUT {f['step']}: {f['reason']}")
    failed = [n.name for n in rep.nodes.values() if n.status == "failed"]
    # a pass the judge could not judge: the last line says so (its step is failed, its stop reason names the cause)
    unjudged = "; ".join(f"pass {k} {v}" for k, v in (res.get("stopped") or {}).items() if str(v).startswith("judge unavailable"))
    if strict:
        print("publication: " + ("published and verified" if x["published"] else "qualified preview" if x["verified"] else "unverified"))
        return _print_gate(x["gates_ok"], f"failed nodes: {failed}" if failed else unjudged[:300] if unjudged
                           else "verified publication required")
    if pv := x.get("preview"):  # judged by an uncalibrated judge: the last lines say so
        print(f"publication: UNVERIFIED PREVIEW (verified=false; judged by {pv['judge']['label']})")
        print(f"  judge: {pv['judge']['meaning']}")
        print("review: required (the judge is uncalibrated; a person must review this package before any use)")
    else:
        print("publication: " + ("published and verified" if x["published"] else "qualified preview" if x["verified"] else "unverified"))
    ex = x["exit"]
    if ex["rule"] == D.RULE_DELIVERY:  # an uncalibrated judge: its review is stated, the exit code is about the delivery
        for line in D.finding_lines(x["review"], x["delivered"]["why"]):
            print(line)
        print(D.delivery_line(ex))
    print(f"preview gates: {'PASS' if x['preview_gates_ok'] else 'FAIL'}" + (f" ({unjudged[:300]})" if unjudged else ""))
    return ex["code"] if ex["rule"] == D.RULE_DELIVERY else 0 if x["preview_gates_ok"] else 1


async def cmd_edit(a: argparse.Namespace) -> int:
    """Change an asset this project shipped (``dags.edit_asset``): OUT is a prop --out folder or a package folder."""
    from antigravity3d.dags import edit_asset as E

    if a.undo:
        try:
            r = E.undo(a.target)
        except E.EditError as e:
            raise CliError(str(e)) from e
        print(f"agy3d edit --undo: restored {r['restored']} (edit {r['undid_edit']} undone; its record is kept at {r['history_kept_at']})")
        return 0
    if not a.instruction and not a.patch:
        raise CliError("say what to change (agy3d edit OUT \"make the handles 30 % larger\") or give --patch OPS.json")
    patch = None
    if a.patch:
        from antigravity3d.ir import IRPatch

        patch = IRPatch.model_validate_json(Path(a.patch).read_text())
    try:
        shipped = E.load_shipped(a.target)
    except E.EditError as e:
        raise CliError(str(e)) from e
    instruction = a.instruction or f"apply the patch {Path(a.patch).name}"
    if patch is None and a.offline:
        raise CliError("--offline has no author that reads an instruction (the offline author is a template): give --patch "
                       "OPS.json, or run live with --token-ceiling N")
    if patch is None and a.token_ceiling is None:
        raise CliError("a live edit needs your own --token-ceiling N (an edit generates no image; --max-images defaults to 0)")
    from antigravity3d.tactics.governor import PLANS, Governor, plan_for

    plan = next((p for p in PLANS.values() if p.label == shipped.plan_label), plan_for(2))
    ledger_dir = shipped.history_base / "ledger"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    ledger = {"token_ceiling": a.token_ceiling, "max_images": a.max_images if a.max_images is not None else 0} if a.token_ceiling else {}
    governor = Governor(plan, journal=ledger_dir / ("spend_tokens.sqlite3" if ledger else "spend.sqlite3"), **ledger)
    cache = None
    if shipped.run_folder is not None and not a.no_cache:
        from antigravity3d.kernel.cache import BuildCache, LocalBlobStore

        cache = BuildCache(LocalBlobStore(shipped.run_folder / ".agy3d-cache"))
    async with AsyncExitStack() as stack:
        if a.offline or patch is not None and not os.environ.get("GEMINI_API_KEY"):
            plane, forge, _, notes = await make_collaborators(True)
            author = None
        else:
            plane, forge, author0, notes = await make_collaborators(False, workdir=(shipped.run_folder or shipped.package.parent) / "agent")
            if plane is not None and not plane.calibrated:
                plane = plane.preview_copy()
            from antigravity3d.tactics.authoring import AgentAuthor
            from antigravity3d.tactics.cached import DesignRecord

            author = (author0.bind(governor, shipped.budget, {"asset_name": shipped.asset},
                                   record=DesignRecord(cache) if cache is not None else None)
                      if isinstance(author0, AgentAuthor) and patch is None else None)
            if patch is None and author is None:
                raise CliError(f"no author that reads an instruction is available ({'; '.join(notes)})")
        await stack.enter_async_context(_progress(author))
        pool = None
        if a.pool_socket:
            from antigravity3d.runtime.worker_daemon import PoolClient

            pool = await stack.enter_async_context(PoolClient(a.pool_socket))
        elif a.blender:
            from antigravity3d.adapters.blender_5_3.pool import BlenderPool

            pool = await stack.enter_async_context(BlenderPool(1, blender_bin=a.blender))
        try:
            r = await E.edit_asset(a.target, instruction, plane=plane, author=author, forge=forge, governor=governor, patch=patch,
                                   pool=pool, cache=cache, role=a.role, announce=lambda line: print(f"note: {line}", flush=True),
                                   strict_review=a.strict_review)
        except E.EditError as e:
            raise CliError(str(e)) from e
    if a.json:
        print(json.dumps(r.to_dict(), indent=1, default=str))
    print(f"agy3d edit: {instruction!r} on {shipped.asset} ({shipped.package})")
    for n in notes:
        print(f"  {n}")
    print(f"  materials from: {r.materials_from}")
    for att in r.attempts:
        sc = att.scope or {}
        print(f"  attempt {att.n}: {'KEPT' if att.kept else 'undone'}" + (f" ({att.why})" if att.why else "")
              + f"; {len(att.ops)} op(s); parts unchanged {sc.get('unchanged', '?')}, changed {len(sc.get('changed') or {})}, "
              f"added {len(sc.get('added') or [])}, removed {len(sc.get('removed') or [])}"
              + (f"; judge: done P={att.done_p:.2f}, collateral P={att.collateral_p:.2f}" if att.done_p is not None else "")
              + f"; {att.seconds:.0f} s")
        for nid, c in sorted((sc.get("changed") or {}).items())[:12]:
            print(f"      {nid}: size ratio {c['ratio']}, centre moved {c['centre_moved_m']} m")
    for w in r.warnings:
        print(f"  warning: {w}")
    if r.untextured:
        print(f"  untextured (an edit generates no image): {', '.join(r.untextured)}")
    spent = r.spend.get("spent")
    unit = r.spend.get("unit")
    print(f"cost: {r.seconds:.0f} s, calls: author {r.calls.get('author', 0)}, judge {r.calls.get('judge', 0)}"
          + (f", {spent:,.0f} tokens (no price applied)" if unit == "tokens" else f", ${spent:.4f}" if spent is not None else ""))
    if not r.kept:
        print(f"edit: NOT MADE, the asset is unchanged: {r.why}")
        return 1
    print(f"edit {r.number}: made; package {r.package}; the replaced package is kept in {r.history} (agy3d edit {a.target} --undo)")
    for line in _web_lines(Path(r.package) / "manifest.json"):  # the same lines as `prop`: one function says them
        print(line)
    print(f"note: {r.note}")
    from antigravity3d import delivery as D

    ex = r.exit or {"rule": D.RULE_GATES, "code": 0 if r.preview_gates_ok else 1}
    if ex["rule"] == D.RULE_DELIVERY:  # an uncalibrated judge: its review is stated, the exit code is about the delivery
        for line in D.finding_lines(r.review or {}, (r.delivered or {}).get("why") or ()):
            print(line)
        print(D.delivery_line(ex))
    print(f"preview gates: {'PASS' if r.preview_gates_ok else 'FAIL'}")
    return ex["code"]


def _count(text: str) -> int:
    n = int(text)
    if n < 0:
        raise argparse.ArgumentTypeError("must be at least 0")
    return n


def _positive_int(text: str) -> int:
    n = int(text)
    if n < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return n


async def cmd_sanitize(a: argparse.Namespace) -> int:
    from antigravity3d.dags.batch_sanitize import BatchSanitizeDAG, table

    rep = await BatchSanitizeDAG(workdir=Path(a.out), budget=_budget(a), max_parallel=a.jobs, timeout_s=a.timeout).run(
        [Path(f) for f in a.files])
    print(f"agy3d sanitize: {len(a.files)} file(s) -> {a.engine}")
    w = rep.extras["workers"]
    print(f"workers: {w['jobs']} {w['mode']}, per-file timeout {w['timeout_s'] or 'off'}{' s' if w['timeout_s'] else ''}"
          + (f", {w['restarted']} replaced after a crash or timeout" if w["restarted"] else ""))
    print(rep.summary())
    print(table(rep.extras["rows"]))
    for r in rep.extras["rows"]:
        if r.get("issues_remaining"):
            print(f"  remaining in {Path(r['file']).name}: {', '.join(r['issues_remaining'])}")
        for k in ("validate_critical", "compliance_critical"):
            if r.get(k):
                print(f"  {k} in {Path(r['file']).name}: {', '.join(r[k])}")
    print(f"outputs: {a.out} (report.json, <file>/<asset>.usda|glb)")
    _print_skips(rep.extras["skipped"])
    return _print_gate(rep.extras["gates_ok"])


async def cmd_kit(a: argparse.Namespace) -> int:
    from antigravity3d.dags.environment_kit import EnvironmentKitDAG
    from antigravity3d.kernel.kit_metric import KitMetric
    from antigravity3d.tactics.governor import BudgetExceeded

    try:
        metric = KitMetric.parse(getattr(a, "metric", None))
    except ValueError as e:
        raise CliError(str(e)) from e
    if a.token_ceiling is None and a.max_images is not None:
        raise CliError("--max-images applies to a token-limited kit: pass --token-ceiling N with it")
    from antigravity3d.kernel.cache import BuildCache, LocalBlobStore

    _, forge, author, notes = await make_collaborators(a.offline, need_plane=False)
    limits = {"token_ceiling": a.token_ceiling, "max_images": a.max_images} if a.token_ceiling is not None else {}
    Path(a.out).mkdir(parents=True, exist_ok=True)
    cache = None if a.no_cache else BuildCache(LocalBlobStore(Path(a.out) / ".agy3d-cache"))
    try:
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(_progress(author))
            pool = None
            if a.pool_socket:
                from antigravity3d.runtime.worker_daemon import PoolClient

                pool = await stack.enter_async_context(PoolClient(a.pool_socket))
            elif a.blender:
                from antigravity3d.adapters.blender_5_3.pool import BlenderPool

                pool = await stack.enter_async_context(BlenderPool(1, blender_bin=a.blender))
            rep = await EnvironmentKitDAG(forge=forge, author=author, workdir=Path(a.out), budget=_budget(a), sheet_resolution=1024 if a.offline else 4096,
                                          art_direction=a.art_direction, metric=metric, pool=pool, cache=cache, new_design=a.new_design,
                                          announce=lambda line: print(f"note: {line}", flush=True), **limits).run(a.prompt, a.pieces, a.splat)
    except BudgetExceeded as e:  # live authoring is admitted against a ceiling; a missing price is a refusal, not a crash
        raise CliError(str(e)) from e
    x = rep.extras
    print(f"agy3d kit: {a.prompt!r} x{a.pieces} -> {a.engine}")
    for n in notes:
        print(f"  {n}")
    _print_art(a.art_direction)
    print(rep.summary())
    sp = x["spend"]
    if sp.get("unit") == "tokens":
        print(f"spend (tokens, no price applied): {sp['summary']} ({sp['rule']})")
    else:
        print(f"spend: {sp['summary']} (kit ceiling = ${sp['per_asset_ceiling_usd']:.2f} x ({a.pieces} pieces + 1 trim sheet))")
    _print_record_and_run({"design_record": x.get("design_record"), "this_run": x.get("this_run")})
    bad = [o for o in x["authoring"]["pieces"] if o["status"] != "produced"]
    for o in bad:
        print(f"  piece #{o['index']} {o['piece']}: {o['status']}" + (f" ({o['detail']})" if o["detail"] else ""))
    if x.get("vram"):
        v = x["vram"]
        print(f"vram: shared trim {v['shared_bytes'] / 2**20:.1f} MiB vs unique textures {v['unique_bytes'] / 2**20:.1f} MiB ({v['ratio']:.1f}x; BC7: {v['compressed']['ratio']:.1f}x)")
    m = x["metric"]
    print(f"metric: {m['name']} grid {m['grid_m']:g} m, module {m['module_m']:g} m, walls {m['wall_height_m']:g} x {m['wall_thickness_m']:g} m, "
          f"door {m['door_width_m']:g} x {m['door_height_m']:g} m, pivot {m['pivot']}, part faces <= {m['max_face_m']:g} m")
    if x.get("kit_check"):
        kc = x["kit_check"]
        print(f"kit check: {'PASS' if kc['pass'] else 'FAIL'} (texel spread from median {kc['texel_spread_from_median']})")
        for n, p in kc["pieces"].items():
            for e in p["errors"]:
                print(f"  {n}: {e}")
    if x.get("snap"):
        sn = x["snap"]
        print(f"snap test: {'PASS' if sn['pass'] else 'FAIL'} " + ", ".join(f"{k} gap {v['max_gap_m'] * 1000:.2f} mm" for k, v in sn["layouts"].items())
              + f"; see {sn['path']}")
        for v in sn["layouts"].values():
            for p in v["problems"]:
                print(f"  {p}")
    if x.get("blender_kit_check"):
        bk = x["blender_kit_check"]
        print(f"blender kit check: {'PASS' if bk['pass'] else 'FAIL'} ({len(bk['pieces'])} pieces; {bk['source']})")
    elif rep.nodes["blender_kit_check"].status == "failed":
        print(f"blender kit check: FAIL ({rep.nodes['blender_kit_check'].error})")
    if a.splat and rep.nodes["splat_import"].status == "ok":  # what the file became in Blender (the skip, if any, is listed below)
        print("splat import: " + ", ".join(f"{k}={v}" for k, v in rep.nodes["splat_import"].data.items()))
    elif a.splat and not (a.blender or a.pool_socket):  # the request was given and could not be honoured: say how to get it
        print(f"splat import: NOT DONE: {a.splat} needs a Blender worker; pass --blender PATH or --pool-socket PATH")
    print(f"trim issues: {len(x['trim_issues'])}; piece min z: {x.get('piece_min_z')}")
    print(f"outputs: kit={x['kit_usd']} trim_sheet={x['trim_sheet']} pieces={len(x['pieces'])} report={a.out}/report.json")
    _print_skips(x["skipped"])
    return _print_gate(x["gates_ok"])


async def cmd_variants(a: argparse.Namespace) -> int:
    from antigravity3d.dags.variant_factory import VariantFactoryDAG
    from antigravity3d.genmedia.nano_banana import FakeImageBackend, ImageForge

    forge = ImageForge(FakeImageBackend()) if a.offline else (ImageForge(client=_client()) if os.environ.get("GEMINI_API_KEY") else None)
    rep = await VariantFactoryDAG(workdir=Path(a.out), budget=_budget(a), forge=forge).run([Path(f) for f in a.assets])
    print(f"agy3d variants: {len(a.assets)} asset(s)")
    print(rep.summary())
    for r in rep.extras["rows"]:
        print(f"  {Path(r['asset']).name}: {r['status']} variants={r.get('variant_names')} usd={r.get('looks_usd')}")
    _print_skips(rep.extras["skipped"])
    return _print_gate(rep.extras["gates_ok"])


async def cmd_calibrate(a: argparse.Namespace) -> int:
    from antigravity3d.decisions.calibration import LabelledAsset, calibrate, run_shadow_calibration, write_studio_policy
    from antigravity3d.decisions.compose import Thresholds

    data = json.loads(Path(a.labels).read_text())
    items = data["records"] if isinstance(data, dict) and "records" in data else data
    if not isinstance(items, list) or not items:
        raise CliError("labels file must be a non-empty JSON list (or {'records': [...]})")
    if all(isinstance(i, (list, tuple)) for i in items):
        records = [(str(r[0]), str(r[1]), float(r[2]), bool(r[3])) for r in items]
        report = calibrate(records)
    elif all(isinstance(i, dict) and "question_id" in i for i in items):
        report = calibrate([(str(i["asset_id"]), i["question_id"], float(i.get("p", i.get("predicted"))), bool(i["approved"])) for i in items])
    else:
        assets = [LabelledAsset(i["asset_id"], i["contact_sheet_uri"], bool(i["approved"]), i.get("ortho_uri"), i.get("style_anchor_uri"), i.get("stage", "final")) for i in items]
        plane, *_ = await make_collaborators(a.offline)
        report = await run_shadow_calibration(plane, assets)
    th = report.thresholds(Thresholds())
    write_studio_policy(a.out, th, report)
    print(f"agy3d calibrate: {report.n_assets} asset(s), {len(report.per_question)} question(s) -> {a.out}")
    bad = report.not_calibrated()
    print(f"not calibrated ({len(bad)}): {', '.join(bad) if bad else 'none'}")
    return 0


def cmd_benchmark(a: argparse.Namespace) -> int:
    from antigravity3d.tactics.benchmark import compare

    result = compare(Path(a.cohort))
    print(json.dumps(result, indent=2))
    return 0 if result["rollout_pass"] else 1


def cmd_models(_: argparse.Namespace) -> int:
    from antigravity3d.models import all_tiers

    print(f"{'role':<10}{'model':<32}{'thinking':<10}purpose")
    for t in all_tiers():
        print(f"{t.role:<10}{t.model:<32}{(t.thinking.name if t.thinking is not None else '-'):<10}{t.purpose}")
    return 0


def cmd_plugin(a: argparse.Namespace) -> int:
    try:
        from antigravity3d import plugin_install as pi
    except ImportError as e:
        print(f"plugin support unavailable: antigravity3d.plugin_install could not be imported ({e})")
        return 2
    if a.plugin_cmd == "install":
        dest = pi.install(Path(a.target) if a.target else None)
        problems = pi.validate(dest)
        print(f"installed {dest}")
    else:
        dest = Path(a.path) if a.path else pi.bundle_dir()
        problems = pi.validate(dest)
        print(f"validated {dest}")
    for p in problems:
        print(f"  problem: {p}")
    print("OK" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def _print_install(r: Any, as_json: bool, verb: str = "install") -> int:
    if as_json:
        print(json.dumps(r.to_dict(), indent=1))
    else:
        print(f"agy3d {verb}: {r.message}")
        if r.project and r.project.get("engine"):
            print(f"  project: {r.project['engine']} {r.project.get('version') or '(version not stated)'}")
        changed = [f for f in r.files if f["action"] != "keep"]
        for f in changed[:40]:
            print(f"  {f['action']:<8} {f['path']}")
        if len(changed) > 40:
            print(f"  ... {len(changed) - 40} more")
        for c in r.checks:
            print(f"  check {c.name:<16} {c.status:<8} {c.detail}")
        for w in r.warnings:
            print(f"warning: {w}")
        for n in r.notes:
            print(f"note: {n}")
        if r.screenshot:
            print(f"  screenshot: {r.screenshot}")
        if r.report_path:
            print(f"  report: {r.report_path}")
        if r.checks:
            print(r.step_line())
    if r.status in ("refused",):
        return 2
    return 0 if r.ok else 1


def cmd_install(a: argparse.Namespace) -> int:
    from antigravity3d.deliver import install_package

    r = install_package(a.package, a.project, a.dest, dry_run=a.dry_run, force=a.force, check=not a.no_check,
                        fov_deg=a.camera_fov, godot_bin=a.godot, assets=a.assets, compress=a.compress)
    return _print_install(r, a.json)


def cmd_uninstall(a: argparse.Namespace) -> int:
    from antigravity3d.deliver import uninstall

    return _print_install(uninstall(a.project, a.dest, dry_run=a.dry_run, force=a.force), a.json, "uninstall")


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="agy3d", description="antigravity-3d: layered agentic 3D & game-asset pipelines")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def limits(p: argparse.ArgumentParser) -> None:
        p.add_argument("--token-ceiling", type=_positive_int, default=None, metavar="N",
                       help="the most tokens (input + output, thinking included) the whole command may use; spend is then "
                            "counted in tokens and no price is applied (for keys without configured prices)")
        p.add_argument("--max-images", type=_count, default=None, metavar="M",
                       help="the most images the command may generate; required with --token-ceiling")

    def cache_flags(p: argparse.ArgumentParser) -> None:
        g = p.add_mutually_exclusive_group()
        g.add_argument("--no-cache", action="store_true",
                       help="use no cache folder (OUT/.agy3d-cache): no cached images and no recorded designs are read "
                            "or written")
        g.add_argument("--new-design", action="store_true",
                       help="ask the author for a new design instead of continuing from the one an earlier run in this "
                            "--out paid for (recorded in OUT/.agy3d-cache); cached images are still reused where their "
                            "request is unchanged, and the new design replaces the recorded one")

    def common(p: argparse.ArgumentParser, out: bool = True) -> None:
        p.add_argument("--engine", choices=ENGINES, default="unreal_5", action=_EngineGiven)
        p.add_argument("--max-tris", type=int, default=None, help="triangle budget (default: engine budget default)")
        p.add_argument("--keep-contact-faces", action="store_true",
                       help="ship every authored triangle, also the faces parts press together (for parts that will be moved apart)")
        p.add_argument("--max-texture-px", type=int, choices=TEXTURE_PX_CHOICES, default=None, metavar="PX",
                       help="largest side of the texture atlas: 256, 512, 1024, 2048 or 4096 (default: the size the parts need, "
                            "at most 4096); for a web page each step down quarters the texture memory")
        p.add_argument("--surfaces", choices=("generated", "flat"), default=None,
                       help="materials without an image: 'generated' paints wear, dirt, grain and markings from the geometry "
                            "(the default), 'flat' ships their flat colours")
        if out:
            p.add_argument("--out", required=True, help="output directory")

    p = sub.add_parser("prop", help="prompt -> engine-ready prop package")
    p.add_argument("prompt")
    common(p)
    p.add_argument("--offline", action="store_true", help="fakes + TemplateAuthor; no network, no API key")
    p.add_argument("--style", default=None, help="style-anchor image path")
    p.add_argument("--art-direction", type=_art_direction, default=None, metavar="ID",
                   help="named art direction (see skills/art-direction); composes with --style")
    p.add_argument("--max-rounds", type=int, default=4)
    p.add_argument("--improve-rounds", type=_count, default=None, metavar="N",
                   help="improvement rounds after the design passes its checks (default: the spend plan's, 2 for a simple "
                        "prop, 4 for a standard or hero one; 0 turns them off). A round costs about 40-80 s on the worker "
                        "tier (its author call and its review) and every two rounds a validation adds about 20-30 s "
                        "(measured live 2026-11-22)")
    cache_flags(p)
    worker = p.add_mutually_exclusive_group()
    worker.add_argument("--blender", metavar="PATH", help="start a warm worker with this Blender executable")
    worker.add_argument("--pool-socket", metavar="PATH", help="connect to a running Blender sidecar")
    publication = p.add_mutually_exclusive_group()
    publication.add_argument("--require-verified", action="store_true", default=None,
                             help="require real Blender, native QA and engine verification (default for live prop runs)")
    publication.add_argument("--preview", dest="require_verified", action="store_false",
                             help="build a review candidate without requiring publication (default for --offline)")
    p.add_argument("--single-candidate", action="store_true",
                   help="use one candidate per tournament for matched baseline measurement")
    p.add_argument("--strict", dest="strict_review", action="store_true",
                   help="with an uncalibrated judge (a preview), exit 3 when its review is below target (default: exit 0 and "
                        "say so: the review is a finding; a calibrated judge's gates decide either way)")
    limits(p)
    p.set_defaults(fn=cmd_prop)

    p = sub.add_parser("sanitize", help="batch-repair meshes (GLB/OBJ/PLY/STL/USD)")
    p.add_argument("files", nargs="+")
    common(p)
    p.add_argument("--jobs", type=_positive_int, default=None, metavar="N",
                   help="worker processes (default: the CPU cores this process may use, at most one per file)")
    p.add_argument("--timeout", type=float, default=900.0, metavar="SECONDS",
                   help="per-file limit; a file that exceeds it is killed and reported as failed (default 900, 0 = no limit)")
    p.set_defaults(fn=cmd_sanitize)

    p = sub.add_parser("kit", help="modular environment kit on one shared trim sheet")
    p.add_argument("prompt")
    common(p)
    p.add_argument("--pieces", type=_positive_int, default=4)
    p.add_argument("--metric", default="interior", metavar="SPEC",
                   help="the kit's shared metric: interior (0.5 m grid, 2 m modules, 3 m walls) or exterior (1 m grid, 4 m modules, "
                        "4 m walls), optionally with overrides, e.g. interior,module=4,wall_height=3.5,pivot=bottom-center")
    p.add_argument("--offline", action="store_true")
    p.add_argument("--splat", default=None, help="optional .ply/.spz Gaussian-splat backdrop (recorded; import needs Blender)")
    cache_flags(p)
    worker = p.add_mutually_exclusive_group()
    worker.add_argument("--blender", metavar="PATH", help="start a warm worker with this Blender executable (adds the blender_kit_check node)")
    worker.add_argument("--pool-socket", metavar="PATH", help="connect to a running Blender sidecar")
    p.add_argument("--art-direction", type=_art_direction, default=None, metavar="ID", help="named art direction (see skills/art-direction)")
    limits(p)
    p.set_defaults(fn=cmd_kit)

    p = sub.add_parser("variants", help="look variants (pristine/muddy/snow_dusted/battle_scarred) as a USD variant set")
    p.add_argument("assets", nargs="+")
    common(p)
    p.add_argument("--offline", action="store_true")
    p.set_defaults(fn=cmd_variants)

    p = sub.add_parser("calibrate", help="fit studio thresholds from human-labelled history")
    p.add_argument("labels")
    p.add_argument("--out", required=True, help="studio_policy.yaml to write")
    p.add_argument("--offline", action="store_true")
    p.set_defaults(fn=cmd_calibrate)

    p = sub.add_parser("benchmark", help="measure paired N=1 and governed runs against studio labels and billing records")
    p.add_argument("cohort", help="JSON with studio ceiling, paired report paths, labels and metered usage")
    p.set_defaults(fn=cmd_benchmark)

    p = sub.add_parser("models", help="print the model tier table")
    p.set_defaults(fn=cmd_models)

    p = sub.add_parser("plugin", help="install / validate the Antigravity plugin bundle")
    ps = p.add_subparsers(dest="plugin_cmd", required=True)
    pi = ps.add_parser("install")
    pi.add_argument("--target", default=None)
    pv = ps.add_parser("validate")
    pv.add_argument("path", nargs="?", default=None)
    p.set_defaults(fn=cmd_plugin)

    p = sub.add_parser("install", help="install a package into a game project (Godot 4, three.js / React Three Fiber) and check it there")
    p.add_argument("package", help="package directory (holds manifest.json)")
    p.add_argument("--project", required=True, help="the game project's root directory")
    p.add_argument("--dest", default=None, help="directory inside the project (Godot: agy3d/<asset>; three.js: where the loader "
                   "and the install record go, src/agy3d/<asset>)")
    p.add_argument("--assets", default=None, metavar="DIR",
                   help="three.js only: the folder, inside the project's served folder (public/ or static/), for the asset's files "
                        "(default <served>/agy3d/<asset>)")
    p.add_argument("--compress", choices=("auto", "always", "never"), default="auto",
                   help="three.js only, with the compression packages in the project: auto (default) compresses only when the "
                        "files do not grow (the transcoder counted unless the project has it), always compresses for the GPU "
                        "memory whatever the size, never installs the PNG textures")
    p.add_argument("--dry-run", action="store_true", help="print what would be written; write nothing")
    p.add_argument("--force", action="store_true", help="replace this tool's own files even where they were edited since")
    p.add_argument("--no-check", action="store_true", help="do not run the in-project check (Godot: no import and load of the installed scene; three.js: no browser check, and so no compression, which is only written after a measured frame comparison)")
    p.add_argument("--camera-fov", type=float, default=None, metavar="DEG",
                   help="vertical field of view the level switch distances are computed for (default 60, the levels' own)")
    p.add_argument("--godot", default=None, metavar="PATH", help="Godot binary for the check (default $GODOT_BIN, then PATH)")
    p.add_argument("--json", action="store_true", help="print the machine-readable result")
    p.set_defaults(fn=cmd_install)

    p = sub.add_parser("edit", help="change an asset this project made: one patch, everything else kept, the old package kept")
    p.add_argument("target", help="a prop --out folder, or a package folder (holds manifest.json)")
    p.add_argument("instruction", nargs="?", default="", help='what to change, e.g. "make the two side handles 30 %% larger"')
    p.add_argument("--patch", default=None, metavar="OPS.json",
                   help="apply this patch ({\"ops\": [...]}, the scene format's patch) instead of asking a model; no model call")
    p.add_argument("--undo", action="store_true", help="put back the package the newest edit replaced")
    p.add_argument("--role", default=None, help="authoring tier for the edit (default: the run's improvement tier, else worker)")
    p.add_argument("--offline", action="store_true", help="fakes only (needs --patch)")
    p.add_argument("--no-cache", action="store_true", help="do not read or write the run folder's cache")
    p.add_argument("--json", action="store_true", help="print the machine-readable result first")
    p.add_argument("--strict", dest="strict_review", action="store_true",
                   help="with an uncalibrated judge (a preview), exit 3 when its review is below target (default: exit 0 and "
                        "say so: the review is a finding; a calibrated judge's gates decide either way)")
    worker = p.add_mutually_exclusive_group()
    worker.add_argument("--blender", metavar="PATH", help="start a warm worker with this Blender executable")
    worker.add_argument("--pool-socket", metavar="PATH", help="connect to a running Blender sidecar")
    limits(p)
    p.set_defaults(fn=cmd_edit)

    p = sub.add_parser("uninstall", help="remove exactly what agy3d install wrote")
    p.add_argument("--project", required=True)
    p.add_argument("--dest", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="remove this tool's files even where they were edited since")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_uninstall)
    return ap


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    try:
        r = a.fn(a)
        return asyncio.run(r) if asyncio.iscoroutine(r) else r
    except CliError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
