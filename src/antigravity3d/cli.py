"""``agy3d``: command line for the Layer-4 pipelines.

    agy3d prop "wooden crate with iron brackets" --engine unreal_5 --out OUT [--offline] [--max-tris N]
                [--style anchor.png] [--art-direction ID]
    agy3d sanitize FILES... --engine unreal_5 --out OUT
    agy3d kit "ruined stone temple" --pieces 4 --out OUT [--offline] [--splat bg.ply] [--metric interior|exterior[,key=value...]] [--art-direction ID]
    agy3d variants ASSET.usda... --out OUT [--offline]
    agy3d calibrate LABELS.json --out studio_policy.yaml [--offline]
    agy3d models
    agy3d plugin install [--target DIR] | plugin validate [PATH]

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
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, get_args

from antigravity3d.state import EngineBudget, TargetEngine

ENGINES = list(get_args(TargetEngine))
OFFLINE_ANSWERS = {"pipeline_dag": "prop_to_engine", "needs_ortho_blueprint": 0.8, "complexity_tier": 2.0}


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
    if a.max_tris is not None:
        return EngineBudget(a.engine, max_tris=a.max_tris, keep_contact_faces=keep)
    return EngineBudget(a.engine, keep_contact_faces=keep)


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


def _print_skips(skipped: list[dict[str, Any]]) -> None:
    print("skipped:" if skipped else "skipped: none")
    for s in skipped:
        print(f"  - {s['step']}: {s['reason']}")


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
    if a.token_ceiling < need:
        raise CliError(f"--token-ceiling {a.token_ceiling:,} cannot admit this run's calls: one judge call can bill up to "
                       f"{judge:,} tokens and one authoring call up to {writer:,}; the smallest ceiling that can admit them "
                       f"is {need:,} tokens (nothing was generated)")
    return plane.preview_copy() if uncalibrated and not strict else plane


# --------------------------------------------------------------------------- commands


async def cmd_prop(a: argparse.Namespace) -> int:
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
    async with AsyncExitStack() as stack:
        # The backend probe and the start-up checks come first: a run that would be refused does not start Blender.
        plane, forge, author, notes = await make_collaborators(a.offline, workdir=out / "agent")
        plane = preflight_prop(a, plane, author, strict)  # refuses here, before any model call, what would fail later
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
            **({"token_ceiling": a.token_ceiling, "max_images": a.max_images} if a.token_ceiling is not None else {}),
        )
        rep = await dag.run(a.prompt)
    x = rep.extras
    if (rn := rep.nodes.get("route")) is not None and (rn.error or "").startswith("ImageLimitRefused"):
        raise CliError(rn.error.split(": ", 1)[1])  # the whole refusal, with its numbers (the summary line truncates)
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
    print("scores: " + (f"tier15={sc.get('tier15', 0):.2f} tier1={'OK' if sc.get('tier1_ok') else 'FAIL'} " + " ".join(f"{k[6:]}={v:.2f}" for k, v in sc.items() if k.startswith("score_")) if sc else "n/a"))
    c = x["cost"]
    if c.get("spend_unit") == "tokens":  # no price applied: tokens and images only, never a dollar sign
        cs = f" cache hits={c['cache']['hits']} misses={c['cache']['misses']}" if c.get("cache") else ""
        print(f"spend (tokens, no price applied): {c['plan']} | decisions plane {c['plane']['calls']} calls, "
              f"{c['plane']['input_tokens']:,} input + {c['plane']['output_tokens']:,} output tokens{cs}")
    else:
        cs = f" cache hits={c['cache']['hits']} misses={c['cache']['misses']} saved=${c['cache']['saved_cost_usd']:.2f}/{c['cache']['saved_ms']:.0f}ms" if c.get("cache") else ""
        print(f"cost: governor(est) ${c['governor_usd']:.4f} | decisions plane ${c['plane']['cost_usd']:.6f} over {c['plane']['calls']} calls | {c['plan']}{cs}")
    print("outputs:")
    for k, v in x["outputs"].items():
        print(f"  {k}: {v}")
    print(f"  report: {x['report_path']}")
    _print_skips(x["skipped"])
    warned = [s for s in res.get("steps", []) if s["status"] == "warned"]
    if warned:
        print("warnings:")
        for w in warned:
            print(f"  - {w['name']}: {w['detail']}")
    for f in res.get("failed_outputs", []):  # an output that could not be made (the FBX): not a blocked package, but plainly a failure
        print(f"FAILED OUTPUT {f['step']}: {f['reason']}")
    failed = [n.name for n in rep.nodes.values() if n.status == "failed"]
    if strict:
        print("publication: " + ("published and verified" if x["published"] else "qualified preview" if x["verified"] else "unverified"))
        return _print_gate(x["gates_ok"], f"failed nodes: {failed}" if failed else "verified publication required")
    if pv := x.get("preview"):  # judged by an uncalibrated judge: the last lines say so
        print(f"publication: UNVERIFIED PREVIEW (verified=false; judged by {pv['judge']['label']})")
        print(f"  judge: {pv['judge']['meaning']}")
        print("review: required (the judge is uncalibrated; a person must review this package before any use)")
    else:
        print("publication: " + ("published and verified" if x["published"] else "qualified preview" if x["verified"] else "unverified"))
    print(f"preview gates: {'PASS' if x['preview_gates_ok'] else 'FAIL'}")
    return 0 if x["preview_gates_ok"] else 1


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
    _, forge, author, notes = await make_collaborators(a.offline, need_plane=False)
    limits = {"token_ceiling": a.token_ceiling, "max_images": a.max_images} if a.token_ceiling is not None else {}
    try:
        async with AsyncExitStack() as stack:
            pool = None
            if a.pool_socket:
                from antigravity3d.runtime.worker_daemon import PoolClient

                pool = await stack.enter_async_context(PoolClient(a.pool_socket))
            elif a.blender:
                from antigravity3d.adapters.blender_5_3.pool import BlenderPool

                pool = await stack.enter_async_context(BlenderPool(1, blender_bin=a.blender))
            rep = await EnvironmentKitDAG(forge=forge, author=author, workdir=Path(a.out), budget=_budget(a), sheet_resolution=1024 if a.offline else 4096,
                                          art_direction=a.art_direction, metric=metric, pool=pool, **limits).run(a.prompt, a.pieces, a.splat)
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

    def common(p: argparse.ArgumentParser, out: bool = True) -> None:
        p.add_argument("--engine", choices=ENGINES, default="unreal_5")
        p.add_argument("--max-tris", type=int, default=None, help="triangle budget (default: engine budget default)")
        p.add_argument("--keep-contact-faces", action="store_true",
                       help="ship every authored triangle, also the faces parts press together (for parts that will be moved apart)")
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
    p.add_argument("--no-cache", action="store_true")
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
