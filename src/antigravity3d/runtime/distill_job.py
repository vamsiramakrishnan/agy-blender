"""Discover prop repair logs, replay a bounded rule on authored labels, and review a promotion.

    python -m antigravity3d.runtime.distill_job --workspace PROJECT --holdout fixtures.json
    python -m antigravity3d.runtime.distill_job --workspace PROJECT --holdout fixtures.json \
        --promote seat-floating-box-v1 --evidence DIGEST --reviewed-by NAME

Promotion reruns replay and rejects changed evidence. The scheduled default only writes a report.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import os
import re
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from antigravity3d.runtime.qa_state import state_root
from antigravity3d.tactics.distill import FixLog, FixRecord, cluster, observed_fixes
from antigravity3d.tactics import reviewed_rules as rules


def summarize_telemetry(path: Path, *, since: float = 0.0) -> str:
    calls: Counter[str] = Counter()
    fails: Counter[str] = Counter()
    errors: dict[str, Counter[str]] = defaultdict(Counter)
    dur: dict[str, list[float]] = defaultdict(list)
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict) or float(rec.get("ts", 0)) < since:
                continue
            tool = str(rec.get("tool", "?"))
            calls[tool] += 1
            if rec.get("duration_ms") is not None:
                dur[tool].append(float(rec["duration_ms"]))
            if not rec.get("ok", True):
                fails[tool] += 1
                errors[tool][str(rec.get("error", ""))[:120]] += 1
    lines = ["## Tool failure rates", ""]
    if not calls:
        return "\n".join(lines + ["_No telemetry in this window._", ""])
    lines += ["| tool | calls | failed | fail % | p50 ms | top error |", "|---|---:|---:|---:|---:|---|"]
    for tool, n in sorted(calls.items(), key=lambda kv: (-fails[kv[0]] / kv[1], kv[0])):
        d = sorted(dur[tool])
        p50 = f"{d[len(d) // 2]:.0f}" if d else "-"
        top = errors[tool].most_common(1)[0][0].replace("|", "/") if errors[tool] else ""
        lines.append(f"| {tool} | {n} | {fails[tool]} | {100 * fails[tool] / n:.0f} | {p50} | {top} |")
    return "\n".join(lines + [""])


def discover_logs(workspace: Path | None) -> list[Path]:
    project = Path(workspace or Path.cwd())
    root = state_root(workspace)
    paths = {root / "fixes.jsonl"}
    for pointer in (root / "fixlogs").glob("*.json"):
        path = json.loads(pointer.read_text())["path"]
        paths.add(Path(path))
    # Historical CLIs wrote this filename without registering it. Bound the
    # walk to the project, excluding VCS and dependency trees.
    for directory, subdirs, names in os.walk(project):
        subdirs[:] = [s for s in subdirs if not s.startswith(".") and s not in {"node_modules", "__pycache__"}]
        if "fixlog.jsonl" in names:
            paths.add(Path(directory) / "fixlog.jsonl")
    return sorted({p.resolve() for p in paths if p.is_file()})


def evaluate(records: list[FixRecord], corpus: rules.Corpus | None, *, history: list[FixRecord] | None = None) -> dict:
    eligible = [r for r in records if r.rule == rules.RULE and r.origin == "decisions/author"
                and re.fullmatch("[0-9a-f]{64}", r.scene_hash) and r.score_after > r.score_before]
    reasons = []
    # Repeated successes on copies of the same geometry cannot supply recurrence.
    by_shape: dict[str, list[float]] = defaultdict(list)
    for r in eligible:
        by_shape[r.scene_hash].append(r.score_after - r.score_before)
    independent_ids, independent_shapes = set(), set()
    for item in sorted(eligible, key=lambda r: (r.asset_id, r.scene_hash)):
        if item.asset_id not in independent_ids and item.scene_hash not in independent_shapes:
            independent_ids.add(item.asset_id)
            independent_shapes.add(item.scene_hash)
    if len(independent_ids) < 3:
        reasons.append("three distinct observed native Decisions/model-author fixes required")
    gains = [min(v) for v in by_shape.values()]
    if not gains or sum(gains) / len(gains) < .3 - 1e-9:
        reasons.append("mean training improvement must reach 0.3 Tier-1.5 score units")

    replayed = []
    if corpus is None:
        reasons.append("held-out authored SceneIR corpus required")
    else:
        known = records if history is None else history
        seen_ids = {r.asset_id for r in known}
        seen_shapes = {r.scene_hash for r in known}
        if any(f.scene.asset_name in seen_ids or rules.scene_hash(f.scene) in seen_shapes for f in corpus.fixtures):
            reasons.append("held-out asset or geometry overlaps observed repairs")
        replayed = [rules.replay(f) for f in corpus.fixtures]
        if not any(r["before_mae_m"] <= 1e-6 and not r["changed"] for r in replayed):
            reasons.append("an already-correct held-out control must remain unchanged")
        if not any(r["changed"] for r in replayed):
            reasons.append("no held-out fixture exercised the repair")
    improvement = sum(r["before_mae_m"] - r["after_mae_m"] for r in replayed) / max(1, len(replayed))
    if improvement <= 1e-6 or any(r["regressed"] for r in replayed):
        reasons.append("held-out mean centre-Z error must fall with zero node or Tier-1 regressions")
    payload = {"rule": rules.RULE, "implementation": rules.implementation_hash(),
               "fixes": sorted((dataclasses.asdict(r) for r in eligible), key=lambda row: json.dumps(row, sort_keys=True)),
               "corpus": corpus.model_dump(mode="json") if corpus else None, "replay": replayed}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()
    return {"rule": rules.RULE, "eligible": not reasons, "reasons": reasons, "training_shapes": len(by_shape),
            "replay": replayed, "mae_reduction_m": improvement, "digest": digest, "evidence": payload}


def run(workspace: Path | None, fix_log: Path | None, out_dir: Path | None, days: float = 7.0, *,
        holdout: Path | None = None, promote: str | None = None, evidence: str | None = None,
        reviewer: str | None = None) -> Path:
    root = state_root(workspace)
    since = time.time() - days * 86400
    logs = [fix_log] if fix_log is not None else discover_logs(workspace)
    records = [r for path in logs for r in FixLog(path).read(since=since)]
    default_corpus = root / "distill" / "holdout.json"
    holdout = holdout or (default_corpus if default_corpus.exists() else None)
    corpus = rules.Corpus.model_validate_json(holdout.read_text()) if holdout else None
    history = [r for path in logs for r in FixLog(path).read()] if corpus else records
    result = evaluate(records, corpus, history=history)
    if promote is not None:
        if promote != rules.RULE or not reviewer or not reviewer.strip() or evidence != result["digest"] or not result["eligible"]:
            raise ValueError("promotion requires an eligible replay, its exact digest, an allowlisted rule and a reviewer")

    telemetry = root / "telemetry" / "tool_calls.jsonl"
    observations = cluster(records)
    lines = [f"# Repair distillation ({time.strftime('%Y-%m-%d')})", "",
             f"{len(records)} observed fixes across {len(logs)} logs in {days:g} days.",
             *[f"- {p}" for p in logs], "", summarize_telemetry(telemetry, since=since),
             observed_fixes(observations),
             "", "## Proposed guardrails", "", f"`{rules.RULE}` — " +
             ("eligible for explicit review" if result["eligible"] else "withheld"),
             *[f"- {reason}" for reason in result["reasons"]], "",
             f"Held-out authored labels: {holdout or 'none supplied'}.",
             f"Distinct qualifying training geometries: {result['training_shapes']}.",
             f"Held-out geometric MAE reduction: {result['mae_reduction_m']:.6f} m.",
             f"Evidence digest: `{result['digest']}`.", "",
             "| Held-out asset | Before MAE (m) | After MAE (m) | Regression |",
             "|---|---:|---:|---|",
             *[f"| {r['asset']} | {r['before_mae_m']:.6f} | {r['after_mae_m']:.6f} | {r['regressed']} |"
               for r in result["replay"]]]
    out_dir = out_dir or root / "distill"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"proposal-{time.strftime('%Y%m%d')}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if promote is not None:
        rules.record_approval(workspace, result, reviewer.strip())
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="antigravity3d.runtime.distill_job", description=__doc__)
    ap.add_argument("--workspace", type=Path)
    ap.add_argument("--fix-log", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--holdout", type=Path, help='JSON {"fixtures":[{"scene":<SceneIR>,"expected_z":{"node":0.5}}]}')
    ap.add_argument("--promote", choices=[rules.RULE])
    ap.add_argument("--evidence", help="digest from the reviewed proposal")
    ap.add_argument("--reviewed-by", help="recorded reviewer identity")
    ns = ap.parse_args(argv)
    try:
        print(run(ns.workspace, ns.fix_log, ns.out, ns.days, holdout=ns.holdout, promote=ns.promote,
                  evidence=ns.evidence, reviewer=ns.reviewed_by))
    except (ValueError, OSError) as exc:
        ap.exit(2, f"distill: {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
