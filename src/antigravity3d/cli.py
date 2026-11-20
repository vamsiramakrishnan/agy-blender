"""``agy3d``: command line for the Layer-4 pipelines.

    agy3d prop "wooden crate with iron brackets" --engine unreal_5 --out OUT [--offline] [--max-tris N]
    agy3d sanitize FILES... --engine unreal_5 --out OUT
    agy3d kit "ruined stone temple" --pieces 4 --out OUT [--offline] [--splat bg.ply]
    agy3d variants ASSET.usda... --out OUT [--offline]
    agy3d calibrate LABELS.json --out studio_policy.yaml [--offline]
    agy3d models
    agy3d plugin install [--target DIR] | plugin validate [PATH]

``--offline`` runs everything on the fakes (FakeDecisionsClient, FakeImageBackend) and the TemplateAuthor.
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


def _budget(a: argparse.Namespace) -> EngineBudget:
    return EngineBudget(a.engine, max_tris=a.max_tris) if a.max_tris is not None else EngineBudget(a.engine)


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
        notes.append(f"decision backend: {plane.backend_kind}" + ("" if plane.calibrated else " (UNCALIBRATED: cannot auto-approve)"))
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


# --------------------------------------------------------------------------- commands


async def cmd_prop(a: argparse.Namespace) -> int:
    from antigravity3d.dags.prop_to_engine import PropToEngineDAG
    from antigravity3d.kernel.cache import BuildCache, LocalBlobStore
    from antigravity3d.tactics.distill import FixLog

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    strict = a.require_verified if a.require_verified is not None else not a.offline
    if not a.offline and not os.environ.get("GEMINI_API_KEY"):
        raise CliError("GEMINI_API_KEY is not set (use --offline to run on the fakes)")
    async with AsyncExitStack() as stack:
        pool = None
        if a.pool_socket:
            from antigravity3d.runtime.worker_daemon import PoolClient

            pool = await stack.enter_async_context(PoolClient(a.pool_socket))
        elif a.blender or strict:
            from antigravity3d.adapters.blender_5_3.pool import BlenderPool

            pool = await stack.enter_async_context(BlenderPool(1, blender_bin=a.blender))
        plane, forge, author, notes = await make_collaborators(a.offline, workdir=out / "agent")
        cache = None if a.no_cache else BuildCache(LocalBlobStore(out / ".agy3d-cache"))
        dag = PropToEngineDAG(
            plane=plane, forge=forge, author=author, workdir=out, budget=_budget(a), pool=pool,
            cache=cache, style_anchor=a.style, fixlog=FixLog.for_prop(out), max_rounds=a.max_rounds,
            require_verified=strict, single_candidate=a.single_candidate,
        )
        rep = await dag.run(a.prompt)
    x = rep.extras
    print(f"agy3d prop: {a.prompt!r} -> {a.engine}")
    for n in notes:
        print(f"  {n}")
    print(rep.summary())
    res = x.get("pyramid") or {}
    if res:
        print("pyramid rounds: " + "; ".join(f"{r['stage']}{r['n']}:{r['action']}{'(rolled back)' if r['rolled_back'] else ''} t15={r['tier15']:.2f}" for r in res["rounds"]))
        print("pyramid steps:  " + ", ".join(f"{s['name']}={s['status']}" for s in res["steps"]))
        print(f"render tier: {res['render_tier']}; stopped: {res['stopped']}")
    sc = x.get("scores") or {}
    print("scores: " + (f"tier15={sc.get('tier15', 0):.2f} tier1={'OK' if sc.get('tier1_ok') else 'FAIL'} " + " ".join(f"{k[6:]}={v:.2f}" for k, v in sc.items() if k.startswith("score_")) if sc else "n/a"))
    c = x["cost"]
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
    failed = [n.name for n in rep.nodes.values() if n.status == "failed"]
    print("publication: " + ("published and verified" if x["published"] else "qualified preview" if x["verified"] else "unverified"))
    if strict:
        return _print_gate(x["gates_ok"], f"failed nodes: {failed}" if failed else "verified publication required")
    print(f"preview gates: {'PASS' if x['preview_gates_ok'] else 'FAIL'}")
    return 0 if x["preview_gates_ok"] else 1


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
    from antigravity3d.tactics.governor import BudgetExceeded

    _, forge, author, notes = await make_collaborators(a.offline, need_plane=False)
    try:
        rep = await EnvironmentKitDAG(forge=forge, author=author, workdir=Path(a.out), budget=_budget(a), sheet_resolution=1024 if a.offline else 4096).run(
            a.prompt, a.pieces, a.splat)
    except BudgetExceeded as e:  # live authoring is admitted against a ceiling; a missing price is a refusal, not a crash
        raise CliError(str(e)) from e
    x = rep.extras
    print(f"agy3d kit: {a.prompt!r} x{a.pieces} -> {a.engine}")
    for n in notes:
        print(f"  {n}")
    print(rep.summary())
    sp = x["spend"]
    print(f"spend: {sp['summary']} (kit ceiling = ${sp['per_asset_ceiling_usd']:.2f} x ({a.pieces} pieces + 1 trim sheet))")
    bad = [o for o in x["authoring"]["pieces"] if o["status"] != "produced"]
    for o in bad:
        print(f"  piece #{o['index']} {o['piece']}: {o['status']}" + (f" ({o['detail']})" if o["detail"] else ""))
    if x.get("vram"):
        v = x["vram"]
        print(f"vram: shared trim {v['shared_bytes'] / 2**20:.1f} MiB vs unique textures {v['unique_bytes'] / 2**20:.1f} MiB ({v['ratio']:.1f}x; BC7: {v['compressed']['ratio']:.1f}x)")
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

    def common(p: argparse.ArgumentParser, out: bool = True) -> None:
        p.add_argument("--engine", choices=ENGINES, default="unreal_5")
        p.add_argument("--max-tris", type=int, default=None, help="triangle budget (default: engine budget default)")
        if out:
            p.add_argument("--out", required=True, help="output directory")

    p = sub.add_parser("prop", help="prompt -> engine-ready prop package")
    p.add_argument("prompt")
    common(p)
    p.add_argument("--offline", action="store_true", help="fakes + TemplateAuthor; no network, no API key")
    p.add_argument("--style", default=None, help="style-anchor image path")
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
    p.add_argument("--offline", action="store_true")
    p.add_argument("--splat", default=None, help="optional .ply/.spz Gaussian-splat backdrop (recorded; import needs Blender)")
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
