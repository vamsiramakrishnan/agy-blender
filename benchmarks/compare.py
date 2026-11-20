#!/usr/bin/env python3
"""Before/after table from two ``perf_offline.py`` result files.

    python benchmarks/compare.py benchmarks/results/before/offline.json benchmarks/results/offline.json

Benchmarks are matched by name and cases by their ``params``. For every matched case the table gives both
medians and the change. A case present on one side only is listed, not guessed at. Changes smaller than
``--noise`` (default 10%) are marked as within noise: the harness's own run-to-run spread on an idle machine is
a few percent, and nothing below that threshold should be read as a result.

The tool only reads the two files; it measures nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _key(params: dict) -> str:
    return json.dumps(params, sort_keys=True, separators=(",", ":"))


def _label(params: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in params.items()) or "-"


def _ms(v: float | None) -> str:
    if v is None:
        return "-"
    if v >= 10_000:
        return f"{v / 1000:.1f} s"
    if v >= 100:
        return f"{v:.0f} ms"
    if v >= 1:
        return f"{v:.1f} ms"
    return f"{v:.3f} ms"


def _change(before: float, after: float, noise: float) -> str:
    if before <= 0 or after <= 0:
        return "-"
    ratio = after / before
    if abs(ratio - 1.0) < noise:
        return "within noise"
    return f"{before / after:.1f}x faster" if ratio < 1 else f"{ratio:.1f}x slower"


def rows(before: dict, after: dict, noise: float) -> tuple[list[tuple[str, str, str, str, str]], list[str]]:
    out: list[tuple[str, str, str, str, str]] = []
    notes: list[str] = []
    b, a = before["benchmarks"], after["benchmarks"]
    for name in [*b, *(n for n in a if n not in b)]:
        if name not in a:
            notes.append(f"`{name}`: only in the first file")
            continue
        if name not in b:
            notes.append(f"`{name}`: only in the second file")
            continue
        for side, data in (("first", b[name]), ("second", a[name])):
            if data.get("status") != "ok":
                notes.append(f"`{name}`: status {data.get('status')!r} in the {side} file ({str(data.get('reason') or data.get('error') or '')[:80]})")
        bc = {_key(c["params"]): c for c in b[name].get("cases", [])}
        ac = {_key(c["params"]): c for c in a[name].get("cases", [])}
        for k in [*bc, *(k for k in ac if k not in bc)]:
            cb, ca = bc.get(k), ac.get(k)
            params = (cb or ca)["params"]
            if cb is None or ca is None:
                notes.append(f"`{name}` ({_label(params)}): only in the {'second' if cb is None else 'first'} file")
                continue
            mb, ma = cb.get("median_ms"), ca.get("median_ms")
            if mb is None or ma is None:
                continue
            out.append((name, _label(params), _ms(mb), _ms(ma), _change(mb, ma, noise)))
    return out, notes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("before", type=Path)
    ap.add_argument("after", type=Path)
    ap.add_argument("--noise", type=float, default=0.10, help="relative change treated as noise (default 0.10)")
    ap.add_argument("--only", nargs="*", default=None, help="benchmark name prefixes to include")
    ap.add_argument("--changed", action="store_true", help="omit rows within noise")
    a = ap.parse_args(argv)
    before, after = json.loads(a.before.read_text()), json.loads(a.after.read_text())
    table, notes = rows(before, after, a.noise)
    if a.only:
        table = [r for r in table if any(r[0].startswith(p) for p in a.only)]
    if a.changed:
        table = [r for r in table if r[4] != "within noise"]
    for label, d in (("before", before), ("after", after)):
        m = d.get("machine", {})
        print(f"<!-- {label}: commit {str(m.get('git_commit', '?'))[:10]}, {m.get('cpu_model', '?')}, "
              f"{m.get('logical_cpus', '?')} logical CPUs, harness wall {d.get('total_wall_s', '?')} s -->")
    print("| Benchmark | Case | Before (median) | After (median) | Change |")
    print("|---|---|---|---|---|")
    for r in table:
        print(f"| `{r[0]}` | {r[1]} | {r[2]} | {r[3]} | {r[4]} |")
    if notes:
        print("\nNot compared:")
        for n in notes:
            print(f"- {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
