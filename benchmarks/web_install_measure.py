"""The step-1 measure of round seven, as one command: install packages into fixture three.js projects, check them in a
browser, undo, and print the table the measure asks for (``MEASURES.md``, "Step 1").

    xvfb-run -a python benchmarks/web_install_measure.py --out OUT_DIR \\
        --packages PKG1 PKG2 ... --projects PROJECT1 PROJECT2 ... [--json]

Every package goes into every project, in place, and is uninstalled again: the project's files (hashes; node_modules by
path, size and modification time) are compared before the install and after the uninstall. Per cell it reports:

* the five conditions: (1) the install's checks all passed, one hand step left; (2) the render checks (loader, levels,
  materials, outline, shared textures); (3) the bytes a page receives for the asset with and without compression and
  the two frame-difference numbers and the outline agreement against the tolerance of ``deliver/web_check.py``;
  (4) the project byte for byte after the uninstall; (5) is the test suite's business, not this script's;
* three installs per cell: ``never`` (the uncompressed layout), ``always`` (the tier that holds the tolerance, whatever
  the size: tiers tried with bytes and the three difference numbers, the tier chosen) and ``auto`` (what a developer gets
  by default and why), each uninstalled; then an install of the unchanged package (which keeps the compressed files);
* the texture memory on a GPU with block-compressed formats, computed from the format (this machine's software renderer
  has none, so it cannot be observed here): 32 bits per pixel for RGBA8, 8 for UASTC (BC7), 4 for ETC1S (BC1), each
  with the 4/3 of a full mip chain.

Pictures: ``OUT/<project>__<asset>.png``: the package's own frame, then one column per tier tried, the second row the
difference to the package amplified six times.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from antigravity3d.deliver import check_install, install_package, uninstall
from antigravity3d.deliver.web_check import TOL_MEAN_ABS, TOL_OUTLINE, TOL_PIXEL_DELTA, TOL_PIXEL_SHARE


def snapshot(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for d, _dirs, files in os.walk(root):
        rel = os.path.relpath(d, root)
        out[rel + "/"] = "dir"
        modules = "node_modules" in rel.split(os.sep)
        for f in files:
            p = Path(d) / f
            st = p.lstat()
            out[os.path.join(rel, f)] = f"{st.st_size}:{st.st_mtime_ns}" if modules else hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def picture(check_dir: Path, tiers: list[str], out: Path) -> None:
    """The package's own frame, then one column per tier tried; the second row is the difference to the package, amplified
    six times. What the frame tolerance accepted and what it refused, side by side."""
    pkg = check_dir / "package.png"
    cols = [(n, check_dir / f"tier_{n}.png") for n in tiers if (check_dir / f"tier_{n}.png").is_file()]
    if not pkg.is_file() or not cols:
        if (check_dir / "installed.png").is_file() and pkg.is_file():
            cols = [("installed", check_dir / "installed.png")]
        else:
            return
    a = np.asarray(Image.open(pkg).convert("RGB"))
    top, bottom = [a], [np.zeros_like(a)]
    for _n, f in cols:
        b = np.asarray(Image.open(f).convert("RGB"))
        top.append(b)
        bottom.append(np.stack([np.clip(np.abs(a.astype(int) - b.astype(int)).max(-1) * 6, 0, 255).astype(np.uint8)] * 3, -1))
    Image.fromarray(np.concatenate([np.concatenate(top, 1), np.concatenate(bottom, 1)], 0)).save(out)


def _install(pkg: Path, project: Path, mode: str, check: bool = True) -> tuple[Any, float, dict[str, Any], dict[str, Any]]:
    t = time.perf_counter()
    r = install_package(pkg, project, compress=mode, check=check)
    seconds = round(time.perf_counter() - t, 1)
    rec = json.loads((project / r.dest / ".agy3d/install.json").read_text()) if r.dest and (project / r.dest / ".agy3d/install.json").is_file() else {}
    rep = json.loads(Path(r.report_path).read_text()) if r.report_path else {}
    return r, seconds, rec, rep.get("measured") or {}


def cell(project: Path, pkg: Path, out: Path) -> dict[str, Any]:
    manifest = json.loads((pkg / "manifest.json").read_text())
    asset = manifest["asset"]
    before = snapshot(project)
    today = (manifest.get("web_download") or {}).get("bytes") or (pkg / f"{asset}.glb").stat().st_size
    row: dict[str, Any] = {"project": project.name, "asset": asset, "package": str(pkg), "today_highest_level_bytes": today}

    r0, sec0, rec0, m0 = _install(pkg, project, "never")  # the same loader without compression
    row["never"] = {"seconds": sec0, "status": r0.status, "checks": {c.name: c.status for c in r0.checks}, "wire": m0.get("wire")}
    uninstall(project, r0.dest)

    r1, sec1, rec1, m1 = _install(pkg, project, "always")  # compress at the tier that holds the tolerance, whatever the size
    comp = rec1.get("compression") or {}
    wire = m1.get("wire") or {}
    fd = m1.get("frame_difference") or {}
    tried = comp.get("tiers_tried") or []
    row["always"] = {
        "status": r1.status, "seconds": sec1, "dest": r1.dest, "assets": rec1.get("assets"), "rule": rec1.get("layout_rule"),
        "three": (rec1.get("project") or {}).get("three"), "state": comp.get("state"), "tier": comp.get("tier"),
        "tolerance_measured": comp.get("tolerance_measured"), "tiers_tried": tried, "tools": comp.get("versions"),
        "checks": [{"name": c.name, "status": c.status, "detail": c.detail[:240]} for c in r1.checks],
        "passed": sum(c.status == "passed" for c in r1.checks), "failed": [c.name for c in r1.checks if c.status == "failed"],
        "skipped": [f"{c.name}: {c.detail[:80]}" for c in r1.checks if c.status == "skipped"], "warnings": r1.warnings,
        "hand_steps": 1, "load_ms": m1.get("load_ms"), "browser": m1.get("browser"),
        "frames": {**{k: round(v, 5) for k, v in fd.items()}, "limits": {"mean_abs": TOL_MEAN_ABS, "pixel_share_over": TOL_PIXEL_DELTA,
                                                                           "pixel_share": TOL_PIXEL_SHARE, "outline": TOL_OUTLINE}},
        "bytes": {"today": today, "uncompressed_install": (row["never"]["wire"] or {}).get("asset_bytes"), "asset": wire.get("asset_bytes"),
                  "transcoder": wire.get("transcoder_bytes"), "one_asset_page": wire.get("total_bytes"),
                  "asset_over_today": round(wire["asset_bytes"] / today, 3) if wire else None,
                  "one_asset_page_over_today": round(wire["total_bytes"] / today, 3) if wire else None,
                  "half_reached": bool(wire) and wire["asset_bytes"] <= today / 2},
        "gpu_memory_computed": comp.get("gpu_memory_computed"),
    }
    if r1.dest:
        picture(project / r1.dest / ".agy3d/check", [t["tier"] for t in tried], out / f"{project.name}__{asset}.png")
    t = time.perf_counter()
    r2 = install_package(pkg, project, compress="always", check=False)
    row["reinstall"] = {"status": r2.status, "seconds": round(time.perf_counter() - t, 1),
                        "kept": sum(f["action"] == "keep" for f in r2.files), "files": len(r2.files),
                        "note": next((n[:120] for n in r2.notes if n.startswith(("kept", "compressed"))), None)}
    t = time.perf_counter()
    rc = check_install(project, r1.dest)
    row["recheck"] = {"seconds": round(time.perf_counter() - t, 1), "failed": [c.name for c in rc.checks if c.status == "failed"]}
    uninstall(project, r1.dest)

    r3, sec3, rec3, m3 = _install(pkg, project, "auto")  # what a developer gets by default
    c3 = rec3.get("compression") or {}
    row["auto"] = {"status": r3.status, "seconds": sec3, "state": c3.get("state"), "tier": c3.get("tier"), "reason": c3.get("reason"),
                   "compressed_bytes": c3.get("compressed_bytes") or (m3.get("wire") or {}).get("asset_bytes"),
                   "uncompressed_bytes": c3.get("uncompressed_bytes"), "transcoder_bytes_counted": c3.get("transcoder_bytes_counted"),
                   "checks": {c.name: c.status for c in r3.checks}, "wire": m3.get("wire")}
    uninstall(project, r3.dest)

    after = snapshot(project)
    row["undo"] = {"identical": after == before, "differences": sorted(set(before) ^ set(after) | {k for k in before if k in after and before[k] != after[k]})[:8]}
    a = row["always"]
    row["conditions"] = {
        "1_one_command_all_checks": a["status"] == "installed" and not a["failed"] and not a["skipped"],
        "3_inside_tolerance": a["state"] == "compressed" and bool(fd) and a["tolerance_measured"] is True and not any(
            c["name"] == "compression" and c["status"] != "passed" for c in a["checks"]),
        "3_half_the_bytes": a["bytes"]["half_reached"],
        "3_one_asset_page_not_larger_than_today": (a["bytes"]["one_asset_page_over_today"] or 9) <= 1.0,
        "4_undo_byte_for_byte": row["undo"]["identical"]}
    return row


def table(rows: list[dict[str, Any]]) -> str:
    head = ("| project | asset | three | checks (always) | install s | uncompressed (B) | tiers tried | tier | asset (B) | of today's | half? | "
            "+transcoder | of today's | `auto` did | GPU MB computed (raw > block) | re-install | undo identical |\n" + "|---" * 17 + "|\n")
    lines = []
    for r in rows:
        a, b = r["always"], r["always"]["bytes"]
        g = a.get("gpu_memory_computed") or {}
        tiers = "; ".join(f"{t['tier']} {t['asset_bytes']:,} ({t.get('pixel_share', float('nan')):.2%} px, mean {t.get('mean_abs', float('nan')):.2%}, "
                          f"{'in' if t.get('passed') else 'OUT'})" for t in a["tiers_tried"]) or a["state"]
        auto = r["auto"]
        lines.append(
            f"| {r['project']} | {r['asset']} | {(a['three'] or {}).get('version')} | {a['passed']} passed, {len(a['failed'])} failed, {len(a['skipped'])} skipped | "
            f"{a['seconds']} | {b['uncompressed_install']:,} | {tiers} | {a['tier']} | {b['asset']:,} | {b['asset_over_today']:.1%} | "
            f"{'yes' if b['half_reached'] else 'no'} | {b['one_asset_page']:,} | {b['one_asset_page_over_today']:.1%} | "
            f"{auto['state']}{' (' + str(auto['tier']) + ')' if auto['tier'] else ''} | "
            f"{g.get('uncompressed', 0) / 1e6:.1f} > {g.get('block_compressed', 0) / 1e6:.1f} | {r['reinstall']['status']} "
            f"({r['reinstall']['kept']}/{r['reinstall']['files']} kept, {r['reinstall']['seconds']} s) | {r['undo']['identical']} |")
    return head + "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--packages", nargs="+", required=True, type=Path, help="package folders (hold manifest.json), built for web_three")
    ap.add_argument("--projects", nargs="+", required=True, type=Path, help="fixture three.js project folders")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--json", action="store_true", help="print the rows as JSON instead of the table")
    a = ap.parse_args(argv)
    a.out.mkdir(parents=True, exist_ok=True)
    rows = []
    for project in a.projects:
        for pkg in a.packages:
            print(f"== {project.name} <- {pkg.name}", file=sys.stderr, flush=True)
            rows.append(cell(project, pkg, a.out))
    (a.out / "rows.json").write_text(json.dumps(rows, indent=1, default=str))
    print(json.dumps(rows, indent=1, default=str) if a.json else table(rows))
    bad = [(r["project"], r["asset"], k) for r in rows for k, v in r["conditions"].items() if not v]
    for b in bad:
        print(f"FAIL {b}", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
