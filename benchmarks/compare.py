#!/usr/bin/env python3
"""Before/after table from ``perf_offline.py`` result files.

    python benchmarks/compare.py BEFORE.json AFTER.json
    python benchmarks/compare.py BEFORE.json AFTER.json --before-rest REST.json --repeat SAME_CODE.json --changed

Benchmarks are matched by name and cases by their ``params`` (leaving out a package's size in bytes, which older
result files recorded there although it is something the code produced). For every matched case the table gives
both medians and the change. A case present on one side only is listed, not guessed at.

What counts as a change. A difference smaller than ``--noise`` (default 10%) is marked as within noise. That
floor is not enough for every benchmark: the same code measured twice differs by more than 10% on some of them
(the tool-level edit benchmarks, which go through an event loop and worker threads). ``--repeat`` takes one or
more other runs of the *first* side's code. The largest ratio between those runs and BEFORE, times the floor,
becomes that case's own noise band, and a change is reported only when it is larger. One repeat gives a lower
bound on the noise, not the noise: a row that barely clears its band deserves a second look, not a headline.

``--before-rest`` takes further files for the first side. A benchmark missing from BEFORE is read from them: a
run that was split, for example after a native crash, stays in the files it was written to.

The tool only reads the files; it measures nothing.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path


#: params that are an outcome of the code under test, not an input: two trees can differ in them for the same case
_OUTCOME_PARAMS = ("package_bytes",)


def _key(params: dict) -> str:
    return json.dumps({k: v for k, v in params.items() if k not in _OUTCOME_PARAMS}, sort_keys=True, separators=(",", ":"))


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


def _change(before: float, after: float, band: float) -> str:
    """``band`` is the noise band as a ratio above 1 (1.10 = differences under 10% are noise)."""
    if before <= 0 or after <= 0:
        return "-"
    ratio = after / before
    if abs(math.log(ratio)) < math.log(band):
        return "within noise"
    return f"{before / after:.1f}x faster" if ratio < 1 else f"{ratio:.1f}x slower"


def merged(first: dict, rest: list[dict]) -> dict:
    """The first side's benchmarks: ``first``, plus any benchmark only a ``rest`` file has."""
    out = dict(first["benchmarks"])
    for extra in rest:
        for name, data in extra["benchmarks"].items():
            out.setdefault(name, data)
    return out


def noise_bands(before: dict, repeats: list[dict], floor: float) -> dict[tuple[str, str], float]:
    """Per case, the noise band as a ratio above 1: the largest ratio between BEFORE's median and a repeat run's
    median of the same code, times ``1 + floor``. Cases no repeat covers get ``1 + floor``."""
    bands: dict[tuple[str, str], float] = {}
    for rep in repeats:
        for name, data in rep.items():
            mine = {_key(c["params"]): c.get("median_ms") for c in before.get(name, {}).get("cases", [])}
            for c in data.get("cases", []):
                k, other = _key(c["params"]), c.get("median_ms")
                if mine.get(k) and other:
                    spread = math.exp(abs(math.log(other / mine[k])))
                    bands[(name, k)] = max(bands.get((name, k), 1.0 + floor), spread * (1.0 + floor))
    return bands


def rows(b: dict, a: dict, noise: float, bands: dict[tuple[str, str], float] | None = None) -> tuple[list[tuple[str, ...]], list[str]]:
    """``b`` and ``a`` are the two sides' ``benchmarks`` mappings."""
    out: list[tuple[str, ...]] = []
    notes: list[str] = []
    bands = bands or {}
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
            band = bands.get((name, k), 1.0 + noise)
            out.append((name, _label(params), _ms(mb), _ms(ma), _change(mb, ma, band), f"{(band - 1) * 100:.0f}%"))
    return out, notes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("before", type=Path)
    ap.add_argument("after", type=Path)
    ap.add_argument("--before-rest", type=Path, nargs="*", default=[], metavar="FILE",
                    help="more files for the first side; a benchmark BEFORE lacks is read from them")
    ap.add_argument("--repeat", type=Path, nargs="*", default=[], metavar="FILE",
                    help="other runs of the first side's code; their difference from BEFORE is each case's noise band")
    ap.add_argument("--noise", type=float, default=0.10, help="smallest relative change reported (default 0.10)")
    ap.add_argument("--only", nargs="*", default=None, help="benchmark name prefixes to include")
    ap.add_argument("--changed", action="store_true", help="omit rows within noise")
    a = ap.parse_args(argv)
    load = lambda p: json.loads(p.read_text())  # noqa: E731
    before, after, rest, repeats = load(a.before), load(a.after), [load(p) for p in a.before_rest], [load(p) for p in a.repeat]
    b = merged(before, rest)
    bands = noise_bands(b, [r["benchmarks"] for r in repeats], a.noise)
    table, notes = rows(b, after["benchmarks"], a.noise, bands)
    if a.only:
        table = [r for r in table if any(r[0].startswith(p) for p in a.only)]
    if a.changed:
        table = [r for r in table if r[4] != "within noise"]
    for label, d in (("before", before), *(("before, rest", r) for r in rest), *(("same code as before", r) for r in repeats), ("after", after)):
        m = d.get("machine", {})
        print(f"<!-- {label}: commit {str(m.get('git_commit', '?'))[:10]}, {m.get('timestamp_utc', '?')}, {m.get('cpu_model', '?')}, "
              f"{m.get('logical_cpus', '?')} logical CPUs, harness wall {d.get('total_wall_s', '?')} s -->")
    head = ("Benchmark", "Case", "Before (median)", "After (median)", "Change") + (("Noise band",) if repeats else ())
    print("| " + " | ".join(head) + " |")
    print("|" + "---|" * len(head))
    for r in table:
        print(f"| `{r[0]}` | " + " | ".join(r[1:len(head)]) + " |")
    if notes:
        print("\nNot compared:")
        for n in notes:
            print(f"- {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
