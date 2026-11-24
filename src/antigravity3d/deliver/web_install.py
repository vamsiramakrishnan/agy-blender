"""The three.js / React Three Fiber install, on the machinery of ``install.py`` (plan in memory, per-file keep / replace /
create / remove, the refusals, the transaction with rollback, the record, ``--dry-run``, ``check_install``,
``uninstall``). What differs from the Godot install is where files go and what is checked; see ``deliver/web.py``.

The record is ``<dest>/.agy3d/install.json`` with project-relative names (one install writes into two folders, the served
one and the source one, and the record must not be in the served one). ``created_dirs`` lists the folders the install
made, so an uninstall removes exactly those (a project's own empty ``public/`` stays).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from antigravity3d.deliver import web
from antigravity3d.deliver.detect import ProjectInfo, detect_project
from antigravity3d.deliver.install import (
    RECORD,
    RECORD_DIR,
    RECORD_SCHEMA,
    Check,
    InstallResult,
    Refused,
    _file_actions,
    _prune_empty,
    _sha,
    _sha_file,
    _Transaction,
    edited,
    read_record,
)
from antigravity3d.deliver.paths import UnsafePath, asset_name, inside, no_links, safe_name
from antigravity3d.deliver.portable import locate_package, package_ref, write_report

_SKIP_DIRS = {"node_modules", ".git", "dist", "build", ".next", ".svelte-kit", ".cache", "coverage", ".turbo", ".nuxt", ".output"}


LOADER_NAMES = ("index.js", "index.d.ts", "react.js", "react.d.ts")


def _check_record_names(project: Path, record: dict[str, Any], dest: str) -> None:
    """The install record is a file in the project, which anyone can edit or commit: it is data to check against what
    this command derives, never the authority on what to write or delete. ``dest`` is the folder in use. Every name the
    record holds must be a file an install of this tool writes: the loader files in ``dest``, the asset's files under the
    served folder, and (marked shared) exactly the two transcoder files in ``<served>/agy3d/_basis``. Refuses the whole
    operation on the first that is not."""
    try:
        asset = asset_name(record.get("asset"))
        code = web._rel_inside(project, dest, "install destination")
        if record.get("dest") != code:
            raise UnsafePath(f"install record dest {record.get('dest')!r} is not the folder in use ({code!r})")
        assets = record.get("assets")
        if not isinstance(assets, str):
            raise UnsafePath(f"install record assets {assets!r} is not accepted")
        assets = web._rel_inside(project, assets, "install record assets")
        served = assets.split("/")[0]
        if served not in web.SERVED_FOLDERS or assets == served:
            raise UnsafePath(f"install record assets {assets!r} is not a folder inside {' or '.join(web.SERVED_FOLDERS)}")
        if assets == code or assets.startswith(code + "/") or code.startswith(assets + "/"):
            raise UnsafePath("install record assets and dest overlap")
        basis = record.get("basis")
        if basis is not None and basis != f"{served}/agy3d/_basis":
            raise UnsafePath(f"install record basis {basis!r} is not {served}/agy3d/_basis")
        shared_ok = {f"{basis}/{n}" for n in web.BASIS_FILES} if basis else set()
        mine = re.compile(rf"(?:{re.escape(asset)}(?:_LOD\d+)?\.glb|collision\.json|textures/{re.escape(asset)}_[A-Za-z0-9_.\-]+\.(?:png|jpe?g|webp|ktx2))")
        for folder in (f"{served}/agy3d/_basis", *[x for od, fs in _taken(project, code) for x in fs]):
            if web._overlaps(assets, folder) or web._overlaps(code, folder):
                raise UnsafePath(f"install record folders {assets!r} / {code!r} contain, lie inside or are another install's "
                                 f"folder ({folder})")
        files = record.get("files")
        if not isinstance(files, dict):
            raise UnsafePath(f"install record files {type(files).__name__} is not accepted: a mapping of names is expected")
        for f, v in files.items():
            safe_name(f, "install record entry")
            if not isinstance(v, dict) or not isinstance(v.get("sha256"), str):
                raise UnsafePath(f"install record entry {f!r} is not accepted: no hash")
            if v.get("shared"):
                if f not in shared_ok:
                    raise UnsafePath(f"install record entry {f!r} is marked shared: only the transcoder files in "
                                     f"{served}/agy3d/_basis are" + ("" if basis else ", and this record names no transcoder folder"))
            elif f.startswith(code + "/"):
                if f[len(code) + 1:] not in LOADER_NAMES:
                    raise UnsafePath(f"install record entry {f!r} is not one of the loader files {LOADER_NAMES}")
            elif not f.startswith(assets + "/"):
                raise UnsafePath(f"install record entry {f!r} is not accepted: it is not under {code} or {assets}")
            elif not mine.fullmatch(f[len(assets) + 1:]):
                raise UnsafePath(f"install record entry {f!r} is not a file an install of {asset} writes (its levels, its "
                                 "textures, collision.json)")
        roots = [code, assets, f"{served}/agy3d/_basis"]  # the transcoder folder may be one this install made, whatever the record holds now
        for d in record.get("created_dirs") or []:  # may include the hidden record folder; only ever removed when empty
            if not isinstance(d, str) or "\\" in d or d.startswith("/") or ".." in d.split("/") or \
                    not any(r == d or r.startswith(d + "/") or d.startswith(r + "/") for r in roots):
                raise UnsafePath(f"install record folder {d!r} is not accepted: it is not on the way to or under {roots}")
    except web.WebRefused as e:
        raise Refused(f"{e}; nothing was changed") from e
    except UnsafePath as e:
        raise Refused(f"{e}; nothing was changed") from e


def _shared_bytes_are_threes(project: Path, rel: str, basis: str) -> bool:
    """True when the project's file ``rel`` (in the shared transcoder folder) is byte for byte the copy in the project's
    own three.js: the only thing the install puts there, and so the only thing an uninstall removes from there."""
    three = web.find_package(project, "three")
    name = rel[len(basis) + 1:]
    p = project / rel
    return bool(three and name in web.BASIS_FILES and p.is_file() and not p.is_symlink()
                and p.read_bytes() == (three / web.BASIS_IN_THREE / name).read_bytes())


def find_records(project: Path) -> list[tuple[Path, dict[str, Any]]]:
    """The web install records under the project (anywhere but node_modules and build output)."""
    out = []
    for d, dirs, _files in os.walk(project, followlinks=False):
        here = Path(d)
        if here.name == RECORD_DIR:
            dirs[:] = []
            f = here / "install.json"
            if f.is_file() and not f.is_symlink():
                try:
                    rec = json.loads(f.read_text())
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("engine") == "threejs":
                    out.append((here.parent, rec))
            continue
        dirs[:] = [x for x in dirs if x not in _SKIP_DIRS and (not x.startswith(".") or x == RECORD_DIR)]
        if len(here.relative_to(project).parts) > 8:
            dirs[:] = []
    return out


def _others(project: Path, dest: str) -> list[dict[str, Any]]:
    """The records of the other installs in the project (not the one at ``dest``)."""
    return [rec for d, rec in find_records(project) if d.resolve() != (project / dest).resolve()]


def _held_elsewhere(project: Path, dest: str) -> set[str]:
    """The shared files another install lists."""
    return {f for rec in _others(project, dest) for f, v in (rec.get("files") or {}).items() if isinstance(v, dict) and v.get("shared")}


def _listed_elsewhere(project: Path, dest: str) -> set[str]:
    """Every file another install's record lists: never removed or replaced by this one."""
    return {f for rec in _others(project, dest) for f in (rec.get("files") or {})}


def _taken(project: Path, dest: str | None = None) -> list[tuple[str, list[str]]]:
    return [(str(rec.get("dest")), [x for x in (rec.get("dest"), rec.get("assets"), rec.get("basis")) if isinstance(x, str)])
            for rec in (_others(project, dest) if dest else [r for _d, r in find_records(project)])]


def install_web(pkg: Path, project: Path, manifest: dict[str, Any], info: ProjectInfo, package: dict[str, Any],
                dest: str | None, assets: str | None, dry_run: bool, force: bool, check: bool, *, fov_deg: float | None,
                compress: str = "auto") -> InstallResult:
    from antigravity3d.kernel.lod_diff import DEFAULT_FOV_DEG

    asset = manifest["asset"]
    try:
        layout = web.choose_layout(project, asset, dest, assets, _taken(project))
    except web.WebRefused as e:
        raise Refused(f"{e}; nothing was written") from e
    dest_dir = project / layout.code
    try:
        for rel in (layout.code, layout.assets):
            no_links(project / rel, f"the destination {rel}")
        inside(project, layout.code, "install destination", write=True)
        inside(project, layout.assets, "install assets folder", write=True)
    except UnsafePath as e:
        raise Refused(f"{e}; nothing was changed") from e
    old = read_record(dest_dir) if dest_dir.is_dir() else None
    if old is not None:
        if old.get("engine") != "threejs":
            raise Refused(f"{layout.code} holds an install of another engine ({old.get('engine')!r}); choose another --dest")
        _check_record_names(project, old, layout.code)
    if old is None and dest_dir.is_dir() and any(dest_dir.iterdir()):
        raise Refused(f"{layout.code} exists and holds files this tool did not write (no {RECORD}); choose another --dest")
    if old is not None and old.get("asset") != asset:
        raise Refused(f"{layout.code} holds the install of {old.get('asset')!r}, not {asset!r}; choose another --dest")
    if old is not None and old.get("assets") != layout.assets:
        raise Refused(f"{layout.code} was installed with its files in {old.get('assets')}, not {layout.assets}: "
                      "uninstall it first, or pass the same --assets")
    fov = float(fov_deg if fov_deg is not None else (old or {}).get("fov_deg", DEFAULT_FOV_DEG))
    three = web.three_info(project, info)

    reuse = reuse_meta = None
    if old is not None and (old.get("compression") or {}).get("state") in ("compressed", "kept", "declined", "rejected"):
        reuse, reuse_meta = _reusable(project, old)

    def record_of(plan: web.WebPlan) -> dict[str, Any]:
        tracked = {f: {"sha256": _sha(b), "bytes": len(b), **({"shared": True} if f in plan.shared else {})}
                   for f, b in plan.files.items()}
        comp = dict(plan.compression)
        comp["tiers_tried"] = [{k: v for k, v in t.items() if k != "_frames"} for t in comp.get("tiers_tried") or []]
        comp["outputs"] = sorted(f[len(layout.assets) + 1:] for f in plan.files
                                 if f.startswith(layout.assets + "/") and f.rsplit("/", 1)[-1] != "collision.json") \
            if comp.get("state") in ("compressed", "kept") else []
        return {
            "schema": RECORD_SCHEMA, "tool": "antigravity3d", "asset": asset, "dest": layout.code, "engine": "threejs",
            "assets": layout.assets, "assets_url": layout.assets_url, "basis": layout.basis if plan.shared else None,
            "basis_url": layout.basis_url, "layout_rule": layout.rule,
            # committed with the game: relative to the project, or the folder's name and the hash, never this machine's path
            "package": {**{k: v for k, v in package.items() if k != "path"},
                        **package_ref(pkg, project, _sha_file(pkg / "manifest.json"))},
            "project": {"engine": info.engine, "version": info.version,
                        "three": {"version": three.version, "revision": three.revision, "supported": three.supported}},
            "fov_deg": fov, "levels": [asdict(lv) for lv in plan.levels], "hulls": plan.hulls, "centre": plan.centre,
            "radius_m": plan.radius_m, "switch_m": [lv.switch_in_m for lv in plan.levels[:-1]], "react": plan.react,
            "compression": comp, "files": tracked, "created_dirs": list((old or {}).get("created_dirs") or []),
            "asset_bytes": plan.asset_bytes,
        }

    def probe(plan: web.WebPlan) -> dict[str, Any]:
        """The frame difference of a candidate plan, measured in a browser on a copy: nothing is written into the project
        until a tier is inside the tolerance."""
        from antigravity3d.deliver import web_check

        if not check:
            return {"passed": None, "why": "check not requested"}
        with tempfile.TemporaryDirectory(prefix="agy3d-stage-") as td:
            stage = Path(td) / "project"
            for rel, data in plan.files.items():
                dst = stage / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(data)
            checks, measured = asyncio.run(web_check.run(stage, record_of(plan), manifest, pkg, three.path, Check))
        by = {c.name: c for c in checks}
        if "in_project" in by and by["in_project"].status != "passed":
            return {"passed": None, "why": by["in_project"].detail[:200]}
        if "compression" not in by or by["compression"].status == "skipped":
            return {"passed": None, "why": "no uncompressed frame to compare with"}
        m = measured.get("frame_difference") or {}
        out = {"passed": by["compression"].status == "passed", **{k: round(m[k], 5) for k in ("mean_abs", "pixel_share", "outline") if k in m}}
        others = [c.name for c in checks if c.status == "failed" and c.name != "compression"]
        out["_frames"] = measured.get("frames")  # the pictures of this tier, kept beside the report (not in the record)
        if others:  # a broken install, not a too-coarse encoding: a safer tier would not change it
            out.update({"stop": True, "other_failures": others})
        return out

    cannot = _cannot_measure(check, three)
    try:
        plan = web.build_plan(pkg, manifest, layout, fov_deg=fov, project=project, info=info, dry_run=dry_run,
                              mode=compress, reuse=reuse, reuse_meta=reuse_meta, probe=None if cannot else probe,
                              cannot_measure=cannot)
    except web.WebRefused as e:
        raise Refused(f"{e}; nothing was written") from e
    except ValueError as e:
        raise Refused(f"cannot install this package: {e}") from e
    except RuntimeError as e:
        return InstallResult("failed", f"{e}; nothing was written", "threejs", info.to_dict(), package, layout.code)
    try:
        for f in plan.files:
            safe_name(f, "install file")
    except UnsafePath as e:
        raise Refused(f"{e}; nothing was written") from e

    record = record_of(plan)
    tracked = record["files"]
    comp = record["compression"]
    held = frozenset(_held_elsewhere(project, layout.code))
    shared_names = frozenset(f"{layout.basis}/{n}" for n in web.BASIS_FILES)
    would_run = comp.get("state") == "would_run"
    if would_run:  # a dry run lists what is certain: the textures and the transcoder depend on a measurement made later
        undecided = {f for f in tracked if f.startswith(f"{layout.assets}/{web.TEXTURE_DIR}/") or f in shared_names}
        tracked = {f: v for f, v in tracked.items() if f not in undecided}
    old_files = (old or {}).get("files", {})
    if (old or {}).get("state") == "pending":
        # an interrupted install or update: a file it had not written yet is not an edit, and a file on disk is whichever
        # of the two versions the pending record knows (the new hash, or the ``previous`` one an update was replacing)
        adapted = {}
        for f, v in old_files.items():
            p = project / f
            if not p.is_file():
                continue
            d = _sha_file(p)
            adapted[f] = {**v, "sha256": d} if d in (v["sha256"], v.get("previous")) else v
        old_files = adapted
    actions, blocked, edits = _file_actions(project, tracked, old_files, shared_names, held)
    if would_run:
        for f in [f for f, a in actions.items() if a == "remove" and (f.startswith(f"{layout.assets}/{web.TEXTURE_DIR}/") or f in shared_names)]:
            del actions[f]
    # the old record's shared files that the new plan no longer has (compression turned off): other installs may use them
    listed = frozenset(_listed_elsewhere(project, layout.code))
    kept_foreign: list[str] = []
    for f, a in list(actions.items()):
        if a != "remove":
            continue
        if f in held or f in listed:  # another install lists it: not this one's to remove
            del actions[f]
        elif f in shared_names and not _shared_bytes_are_threes(project, f, layout.basis):
            del actions[f]  # a shared transcoder file that is not the copy in this project's three.js: not ours any more
            kept_foreign.append(f)
    if blocked:
        raise Refused(f"file(s) {blocked[:5]} exist and were not written by this tool; nothing was changed")
    if edits and not force:
        raise Refused(f"file(s) of the earlier install were edited since: {'; '.join(edits[:6])}. Nothing was changed; "
                      "pass --force to replace them with the package's version")
    status = "dry_run" if dry_run else ("installed" if old is None else
                                        "unchanged" if all(a == "keep" for a in actions.values()) else "updated")
    warnings = []
    if three.note:
        warnings.append(three.note)
    result = InstallResult(status, "", "threejs", info.to_dict(), package, layout.code,
                           [{"path": f, "action": a, **({k: tracked[f][k] for k in ("sha256", "bytes")} if f in tracked else {})}
                            for f, a in sorted(actions.items())],
                           warnings=warnings, notes=[f"files are served from {layout.served}/: {layout.rule}", *plan.notes])
    if edits:
        result.warnings.append(f"replaced edited file(s) (--force): {'; '.join(edits[:6])}")
    if kept_foreign:
        result.notes.append(f"kept {kept_foreign}: not the copy in this project's three.js, so not removed")
    result.notes.append(_compression_note(comp, plan))
    if would_run:
        result.notes.append(f"not listed, decided after measuring: the textures (the package's WebP or PNG maps, or KTX2 files in the same folder) and, "
                            f"with KTX2, the shared transcoder in {layout.basis}")
    if dry_run:
        n = {a: sum(v == a for v in actions.values()) for a in ("create", "replace", "keep", "remove")}
        result.message = (f"dry run: would write {layout.code} (loader) and {layout.assets} (files): {n['create']} new, "
                          f"{n['replace']} replaced, {n['keep']} kept, {n['remove']} removed; nothing was written")
        return result

    txn = _Transaction(project)
    try:
        txn.ensure_root()
        # The record goes first, in a pending state that already lists every file and folder the install will make: a
        # process killed between writes leaves something a later install can finish and an uninstall can undo (rollback
        # only helps while the process lives). The final record replaces it as the last write.
        planned_dirs = _planned_dirs(project, [f for f, a in actions.items() if a in ("create", "replace")], layout.code)
        txn.write(f"{layout.code}/{RECORD_DIR}/package_manifest.json", (pkg / "manifest.json").read_bytes())
        # Each file an update replaces keeps its previous hash beside the new one, and each file it will remove is listed
        # (with the hash it has now): after a kill, finishing or undoing accepts either version and orphans nothing.
        pend_files = {f: ({**v, "previous": old_files[f]["sha256"]} if actions.get(f) == "replace" and f in old_files else v)
                      for f, v in record["files"].items()}
        for f, a in actions.items():
            if a == "remove" and f in old_files:
                pend_files[f] = {"sha256": old_files[f]["sha256"], "bytes": old_files[f].get("bytes", 0), "removing": True,
                                 **({"shared": True} if old_files[f].get("shared") else {})}
        pending = {**record, "state": "pending", "files": pend_files,
                   "basis": record["basis"] or (layout.basis if any(v.get("shared") for v in pend_files.values()) else None),
                   "created_dirs": sorted({*record["created_dirs"], *planned_dirs})}
        txn.write(f"{layout.code}/{RECORD}", json.dumps(pending, indent=1).encode())
        for f, a in sorted(actions.items()):
            if a in ("create", "replace"):
                txn.write(f, plan.files[f])
            elif a == "remove":
                txn.remove(f)
        made = [d.relative_to(project).as_posix() for d in txn.dirs]
        record["state"] = "complete"
        record["created_dirs"] = sorted({*record["created_dirs"], *made, f"{layout.code}/{RECORD_DIR}"})
        txn.write(f"{layout.code}/{RECORD}", json.dumps(record, indent=1).encode())
    except UnsafePath as e:  # something on disk changed since it was checked
        txn.rollback()
        raise Refused(f"{e}; nothing was changed") from e
    except BaseException:
        txn.rollback()
        raise
    for f, a in actions.items():
        if a == "remove":
            _prune_empty((project / f).parent, project)
    result.message = {"installed": f"installed {asset}: loader in {layout.code}, files in {layout.assets}",
                      "updated": f"updated {layout.code} and {layout.assets} to the package's current files",
                      "unchanged": f"{layout.code} already holds this package; nothing changed"}[status]
    _write_tier_frames(project, layout.code, plan.compression.get("tiers_tried"))
    if check:
        check_into(result, project, record, manifest, pkg, three)
    return result


def _cannot_measure(check: bool, three: web.ThreeInfo) -> str | None:
    """Why there is no frame comparison to gate compression on (None: there is one). Compression is only written after a
    measurement inside the tolerance; without one the install is uncompressed and says which tool or flag allows it."""
    from antigravity3d.adapters.engines.web_three import WebThreeValidator

    tail = "compression is only written after a frame comparison measured in a browser"
    if not check:
        return f"--no-check turns off the in-project check, and {tail}: run without --no-check to allow compression"
    if three.path is None:
        return f"node_modules/three was not found, and {tail}: run npm install, then agy3d install again"
    if (why := WebThreeValidator()._unavailable()) is not None:
        return f"{why}; {tail}: install the browser tooling named there to allow compression"
    return None


def _planned_dirs(project: Path, files: list[str], code: str) -> list[str]:
    """The folders an install of ``files`` will make: every parent that does not exist yet, plus the record folder."""
    out = {f"{code}/{RECORD_DIR}"}
    for f in files:
        d = Path(f).parent
        while d.as_posix() not in (".", "") and not (project / d).exists():
            out.add(d.as_posix())
            d = d.parent
    d = Path(code)
    while d.as_posix() not in (".", "") and not (project / d).exists():
        out.add(d.as_posix())
        d = d.parent
    return sorted(out)


def _tiers_text(tried: list[dict[str, Any]] | None) -> str:
    return "; ".join(f"{t['tier']}: {t['asset_bytes']:,} B" + (f", {t['pixel_share']:.2%} of pixels over 8 %, mean {t['mean_abs']:.2%}, "
                     f"outline {t['outline']:.5f}, {'inside' if t.get('passed') else 'outside'} the tolerance" if "pixel_share" in t else "")
                     + (", too large to keep, frames not compared" if t.get("not_compared") else "")
                     for t in tried or [])


def _gpu_text(comp: dict[str, Any]) -> str:
    g = comp.get("gpu_memory_computed")
    return (f"GPU texture memory {g['uncompressed'] / 1e6:.1f} MB -> {g['block_compressed'] / 1e6:.1f} MB (computed from the block "
            "format with mipmaps, 32 bits per pixel against 8 or 4, not observed: the software renderer here has no block formats)") if g else ""


def _write_tier_frames(project: Path, dest: str, tried: list[dict[str, Any]] | None) -> None:
    """The frame each tier tried rendered, beside the check's report: what the tolerance did or did not accept."""
    frames = [(t["tier"], t["_frames"]) for t in tried or [] if t.get("_frames")]
    if not frames:
        return
    out = project / dest / RECORD_DIR / "check"
    out.mkdir(parents=True, exist_ok=True)
    for name, f in frames:
        (out / f"tier_{name}.png").write_bytes(f["installed"])
    (out / "package.png").write_bytes(frames[0][1]["package"])


def _compression_note(comp: dict[str, Any], plan: web.WebPlan) -> str:
    s = comp.get("state")
    if s in ("compressed", "kept"):
        before, after = comp.get("bytes_before", 0), plan.asset_bytes
        verb = "compressed" if s == "compressed" else "kept the compressed files of the earlier install (same inputs, tool versions and settings)"
        measured = ("the frame difference was measured in a browser and is inside the tolerance" if comp.get("tolerance_measured")
                    else "measured when these files were made")
        return (f"{verb} at tier {comp.get('tier')} (KTX2 textures, meshopt geometry; {measured}): the asset's files {before:,} -> "
                f"{after:,} bytes; {_gpu_text(comp)}. These textures are procedurally painted and PNG is already small, so the "
                f"gain is mostly GPU memory, not download. Tiers tried: {_tiers_text(comp.get('tiers_tried')) or 'none (reused)'}. "
                f"Tools: {', '.join(f'{k} {v}' for k, v in (comp.get('versions') or {}).items())}")
    if s == "declined":
        g = comp.get("gpu_memory_computed") or {}
        return (f"not compressed (--compress auto): {comp['reason']}. A page must not download more than before; the gain would be "
                f"GPU memory only, {g.get('uncompressed', 0) / 1e6:.1f} MB -> {g.get('block_compressed', 0) / 1e6:.1f} MB (computed from "
                f"the block format, not observed). --compress always on the command line, compress=\"always\" through "
                f"the install_into_project tool, takes it. Tiers tried: {_tiers_text(comp.get('tiers_tried'))}")
    if s == "rejected":
        tried = _tiers_text(comp.get("tiers_tried"))
        return f"not compressed: {comp.get('reason')}" + (f". Tiers tried: {tried}" if tried else "")
    if s == "unmeasured":
        return f"not compressed (the tolerance could not be measured): {comp.get('reason')}"
    if s == "would_run":
        return (f"compression would be tried (--compress {comp.get('mode')}; the six packages are in the project): each encoding is "
                "measured in a browser first and written only if it is inside the frame tolerance; a dry run runs nothing from the "
                "project, so the result, the sizes and the tier are unknown until it does")
    if s == "unavailable":
        return f"not compressed: {', '.join(comp.get('missing', []))} not found; to enable compression: {comp.get('enable')}"
    return f"not compressed ({comp.get('reason', 'off')})"


def _reusable(project: Path, old: dict[str, Any]) -> tuple[dict[str, bytes] | None, dict[str, Any] | None]:
    """The compressed files of the earlier install, when every one is still the bytes it wrote."""
    comp = old.get("compression") or {}
    if comp.get("state") in ("declined", "rejected"):
        return None, comp
    out: dict[str, bytes] = {}
    for name in comp.get("outputs") or []:
        rel = f"{old['assets']}/{name}"
        rec = (old.get("files") or {}).get(rel)
        try:
            p = inside(project, rel, "install record entry", write=False)
        except UnsafePath:
            return None, None
        if rec is None or not p.is_file() or _sha_file(p) != rec["sha256"]:
            return None, None
        out[name] = p.read_bytes()
    return (out, comp) if out and comp.get("key") else (None, None)


def check_into(result: InstallResult, project: Path, record: dict[str, Any], manifest: dict[str, Any], pkg: Path | None,
               three: web.ThreeInfo) -> None:
    from antigravity3d.deliver import web_check

    try:
        checks, measured = asyncio.run(web_check.run(project, record, manifest, pkg, three.path, Check))
    except (OSError, ValueError, KeyError, TypeError) as e:  # the install stands; its proof could not be read
        result.checks.append(Check("in_project", "failed", f"the check could not complete: {type(e).__name__}: {e}"))
        return
    result.checks.extend(checks)
    if record.get("react"):
        try:
            result.checks.extend(asyncio.run(web_check.run_react(project, record, manifest, three.path, Check)))
        except Exception as e:  # noqa: BLE001  the install stands; this proof could not be completed
            result.checks.append(Check("react_component", "failed", f"the check could not complete: {type(e).__name__}: {e}"[:400]))
    comp = record.get("compression") or {}
    others_failed = [c.name for c in result.checks if c.status == "failed" and c.name != "compression"]
    if comp.get("state") in ("compressed", "kept") and others_failed:
        # the two statements belong together: the encoding was measured inside the tolerance, and the install still fails
        sentence = f"compressed at {comp.get('tier')}, inside the frame tolerance; the install still fails check {', '.join(others_failed)}"
        result.warnings.append(sentence)
        result.message = f"{result.message}; {sentence}"
    out = project / record["dest"] / RECORD_DIR / "check"
    out.mkdir(parents=True, exist_ok=True)
    frames = measured.pop("frames", {})
    for name, data in frames.items():
        (out / f"{name}.png").write_bytes(data)
    shot = measured.pop("screenshot", None)
    if shot:
        (out / "web_three.png").write_bytes(shot)
        result.screenshot = str(out / "web_three.png")
    if measured.get("wire"):
        w = measured["wire"]
        result.notes.append(_wire_note(w, record))
    write_report(project, out, result, record, measured)
    result.report_path = str(out / "report.json")


def _wire_note(w: dict[str, Any], record: dict[str, Any]) -> str:
    """What a page downloads for the asset, as the check's page server counted it."""
    shared = f" and {w['transcoder_bytes']:,} for the shared KTX2 transcoder" if w.get("transcoder_bytes") else ""
    if len(record.get("levels") or []) < 2:
        return (f"a page loading this asset received {w['asset_bytes']:,} bytes for it (its one level, textures once, "
                f"collision data){shared}")
    if not w.get("default_load_measured"):
        return (f"a page loading this asset with every level received {w['all_levels_bytes']:,} bytes for it{shared}; "
                f"the load with default options could not be measured, and fetches {w['far_level_bytes']:,} bytes "
                "of far levels less")
    return (f"a page loading this asset as the loader does by default received {w['asset_bytes']:,} bytes for it (the "
            f"nearest level, textures once, collision data){shared}; the {len(record['levels']) - 1} far level(s), "
            f"{w['far_level_bytes']:,} bytes, are each fetched the first time the camera comes within "
            f"{web.ON_DEMAND_REACH:.0%} of the distance where it appears, and never on a page whose camera stays "
            f'nearer (levels: "all" fetches them before the loader resolves: {w["all_levels_bytes"]:,} bytes)')


def check_web(project: Path, dest: str, record: dict[str, Any]) -> InstallResult:
    manifest = json.loads((project / dest / RECORD_DIR / "package_manifest.json").read_text())
    pkg, why_not = locate_package(project, record)
    three = web.three_info(project, detect_project(project))
    result = InstallResult("unchanged", f"checked {dest}", "threejs", detect_project(project).to_dict(), record["package"], dest)
    if pkg is None:
        result.notes.append(why_not)
    check_into(result, project, record, manifest, pkg, three)
    return result


def _hand_over(project: Path, rel: str, remaining: list[Path]) -> None:
    """A folder this install made that another install still has files in is that install's to remove from now on: it
    goes into the other record's ``created_dirs`` (the last uninstall then leaves the project as it was before the first
    install, whichever order they are removed in)."""
    if not remaining:
        return
    for d, rec in find_records(project):
        if d.resolve() == (project / rel).resolve():
            continue
        mine = {m.relative_to(project).as_posix() for m in remaining if any(f.startswith(m.relative_to(project).as_posix() + "/")
                                                                              for f in rec.get("files") or {})}
        if not mine - set(rec.get("created_dirs") or []):
            continue
        rec["created_dirs"] = sorted({*(rec.get("created_dirs") or []), *mine})
        (d / RECORD).write_text(json.dumps(rec, indent=1))


def uninstall_web(project: Path, rel: str, dest_dir: Path, record: dict[str, Any], dry_run: bool, force: bool) -> InstallResult:
    try:
        _check_record_names(project, record, rel)
    except Refused as e:
        return InstallResult("refused", str(e), dest=rel)
    held = _held_elsewhere(project, rel)
    files = record["files"]
    try:  # every path is checked on disk before anything is removed: a folder swapped for a link refuses, it does not crash
        paths = {f: inside(project, f, "install record entry", write=False) for f in files}
        for d in (dest_dir / RECORD_DIR,):
            no_links(d, "the install record folder")
    except UnsafePath as e:
        return InstallResult("refused", f"{e}; nothing was removed", dest=rel)
    basis = record.get("basis")
    keep_shared, foreign = [], []
    for f, r in files.items():
        if not r.get("shared"):
            continue
        if f in held:
            keep_shared.append(f)
        elif paths[f].exists() and not _shared_bytes_are_threes(project, f, basis):
            foreign.append(f)  # not the copy this project's three.js holds: not what an install put there
    pending = record.get("state") == "pending"

    def changed(f: str, r: dict[str, Any]) -> str | None:
        why = edited(project, f, r)
        if why and why != "deleted" and pending and paths[f].is_file() and _sha_file(paths[f]) == r.get("previous"):
            return None  # an interrupted update: the file still has the content it was going to replace
        return why

    edits = [f"{f}: {why}" for f, r in files.items() if not r.get("shared") and (why := changed(f, r)) and why != "deleted"]
    if edits and not force:
        return InstallResult("refused", f"file(s) were edited since the install: {'; '.join(edits[:6])}. Nothing was removed; pass --force", dest=rel)
    elsewhere = _listed_elsewhere(project, rel)  # another install's file is never this one's to remove
    kept = {*keep_shared, *foreign, *(f for f in files if f in elsewhere)}
    remove = [f for f in sorted(files) if f not in kept and paths[f].exists()]
    entries = [{"path": f, "action": "remove"} for f in remove] + [{"path": f, "action": "keep", "why": "another install lists it" if f in keep_shared or f in elsewhere
                                                                       else "it is not the copy in this project's three.js"} for f in sorted(kept)]
    result = InstallResult("dry_run" if dry_run else "uninstalled", "", "threejs", None, record.get("package"), rel, entries)
    others = sorted(p.relative_to(project).as_posix() for root in (dest_dir, project / record["assets"]) if root.is_dir()
                    for p in root.rglob("*") if p.is_file() and p.relative_to(project).as_posix() not in files
                    and RECORD_DIR not in p.relative_to(project).parts)
    if others:
        result.notes.append(f"kept {len(others)} file(s) this tool did not write: {others[:5]}")
    if keep_shared:
        result.notes.append("kept the shared KTX2 transcoder: another installed asset uses it")
    if foreign:
        result.notes.append(f"kept {foreign}: not the copy in this project's three.js, so not something to remove")
    if edits:
        result.warnings.append(f"removed edited file(s) (--force): {'; '.join(edits[:6])}")
    if dry_run:
        result.message = f"dry run: would remove {len(remove)} file(s) and {RECORD_DIR}/ from {rel}; nothing was removed"
        return result
    for f in remove:
        paths[f].unlink(missing_ok=True)
    shutil.rmtree(dest_dir / RECORD_DIR, ignore_errors=True)  # no_links above: a real folder, not a link
    made = [project / d for d in record.get("created_dirs") or []]
    for d in sorted(made, key=lambda p: len(p.parts), reverse=True):  # only folders this install made, deepest first
        if d.is_dir() and not d.is_symlink() and not any(d.iterdir()):
            d.rmdir()
    _hand_over(project, rel, [d for d in made if d.is_dir()])
    result.message = f"removed {len(remove)} file(s) from {rel} and {record['assets']}" + (f"; kept {len(others)} not written by this tool" if others else "")
    return result


__all__ = ["install_web", "uninstall_web", "check_web", "find_records"]
