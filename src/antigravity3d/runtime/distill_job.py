"""Weekly failure-to-skill distillation (run by the ``weekly_distill`` schedule).

    python -m antigravity3d.runtime.distill_job [--workspace DIR] [--fix-log PATH] [--out DIR]

Reads the plugin's post-tool telemetry (``<state>/telemetry/tool_calls.jsonl``,
written by the PostToolUse hook) and the fix log consumed by
:mod:`antigravity3d.tactics.distill`, and writes one Markdown proposal with

1. per-tool failure rates and the most frequent error strings, and
2. ``tactics.distill.propose_rules`` output for recurring fixes.

It only *proposes*: the scheduled agent turns the proposal into a SKILL.md /
rule edit for a human to review. Exit code 0 even when there is nothing to do.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from antigravity3d.runtime.qa_state import state_root
from antigravity3d.tactics.distill import FixLog, cluster, propose_rules


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


def run(workspace: Path | None, fix_log: Path | None, out_dir: Path | None, days: float = 7.0) -> Path:
    root = state_root(workspace)
    since = time.time() - days * 86400
    telemetry = root / "telemetry" / "tool_calls.jsonl"
    fix_log = fix_log or root / "fixes.jsonl"
    records = FixLog(fix_log).read()
    body = (
        f"# antigravity-3d distillation proposal ({time.strftime('%Y-%m-%d')})\n\n"
        f"Window: last {days:g} days. Telemetry: `{telemetry}`. Fix log: `{fix_log}` ({len(records)} records).\n\n"
        + summarize_telemetry(telemetry, since=since) + "\n" + propose_rules(cluster(records))
    )
    out_dir = out_dir or root / "distill"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"proposal-{time.strftime('%Y%m%d')}.md"
    out.write_text(body, encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="antigravity3d.runtime.distill_job")
    ap.add_argument("--workspace", type=Path, default=None)
    ap.add_argument("--fix-log", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--days", type=float, default=7.0)
    ns = ap.parse_args(argv)
    print(run(ns.workspace, ns.fix_log, ns.out, ns.days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
