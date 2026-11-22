"""Install a finished package into a game project, and check it there.

``install_package`` is the interface (the command line and the MCP tool are thin wrappers): it returns an
:class:`InstallResult` whose ``to_dict()`` is the machine-readable record (files written, each check with
pass / fail / skipped and its reason).

Rules that hold for every engine:

* Nothing outside DEST is written. DEST is a directory inside the project; the tool's own record lives in
  ``DEST/.agy3d/`` (``install.json``: every file written with its hash). The in-project check runs on an isolated
  copy (``godot.isolated_project``), so even the engine's import cache of the real project is not touched.
* A file the tool did not write is never overwritten, with or without ``force``. An update replaces only files the
  record lists, and refuses when one of them was edited since (``force`` overwrites the tool's own edited files).
  Unchanged files are left as they are on disk, so an engine's own additions to them (Godot writes a ``uid`` into
  each ``.import`` file, which the project's scenes may reference) survive an update.
* A failed install or update is rolled back: created files and directories are removed, replaced ones restored.
* ``dry_run`` computes the same plan and writes nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from antigravity3d.deliver.detect import TARGET_OF, ProjectInfo, detect_project, version_tuple
from antigravity3d.deliver.paths import UnsafePath, asset_name, inside, no_links, safe_name

RECORD_DIR = ".agy3d"
RECORD = f"{RECORD_DIR}/install.json"
RECORD_SCHEMA = 1
# The manifest fields an install reads. They exist from schema_version 2 on (2 made far LODs reference textures/
# and added glb_images); a later version is accepted when it still carries them.
MIN_SCHEMA = 2
REQUIRED_FIELDS = ("asset", "target_engine", "files", "file_hashes", "glb_bounds_m", "tris")
# Where an install's own files may be (top level: the levels, their .import files, the scene) and where Godot extracts
# the shared materials. Every name from a record is held to these before it is joined to DEST (deliver/paths.py).
INSTALL_FOLDERS = ("", "textures")
MATERIAL_FOLDERS = ("materials",)


@dataclass
class Check:
    name: str
    status: str          # "passed" | "failed" | "skipped"
    detail: str = ""
    data: dict[str, Any] | None = None


@dataclass
class InstallResult:
    status: str                      # installed | updated | unchanged | dry_run | uninstalled | refused | not_built | failed
    message: str
    engine: str | None = None
    project: dict[str, Any] | None = None
    package: dict[str, Any] | None = None
    dest: str | None = None          # relative to the project
    files: list[dict[str, Any]] = field(default_factory=list)   # {path, action, sha256?, bytes?}
    checks: list[Check] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    screenshot: str | None = None
    report_path: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("installed", "updated", "unchanged", "dry_run", "uninstalled") and \
            not any(c.status == "failed" for c in self.checks)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["ok"] = self.ok
        return d

    def step_line(self) -> str:
        """One pipeline-style line, as ``agy3d prop`` prints its steps."""
        counts = {s: sum(c.status == s for c in self.checks) for s in ("passed", "failed", "skipped")}
        return (f"  install_{self.engine or 'project'}  {self.status:<10} {self.dest or ''}  checks: {counts['passed']} passed, "
                f"{counts['failed']} failed, {counts['skipped']} skipped")


class Refused(Exception):
    """The install would damage the project or cannot be done as asked; nothing was written."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- the package


def read_package(pkg: Path) -> dict[str, Any]:
    """The package's manifest, after checking that every file it lists is there with the bytes it records."""
    try:
        mf = inside(pkg, "manifest.json", "package manifest", write=False)
    except UnsafePath as e:
        raise Refused(f"{e}; nothing was written") from e
    if not mf.is_file():
        raise Refused(f"{pkg} has no manifest.json: not an antigravity-3d package")
    try:
        data = json.loads(mf.read_text())
    except ValueError as e:
        raise Refused(f"{mf} is not valid JSON ({e})") from e
    sv = data.get("schema_version")
    if not isinstance(sv, int) or sv < MIN_SCHEMA:
        raise Refused(f"manifest schema_version {sv!r} is older than {MIN_SCHEMA}, the lowest this install reads "
                      "(re-export the package)")
    missing = [k for k in REQUIRED_FIELDS if k not in data]
    if missing:
        raise Refused(f"manifest schema_version {sv} lacks {missing}, which an install needs")
    try:
        asset_name(data["asset"])
        for f in [*(data.get("files") or []), *(data.get("file_hashes") or {})]:
            safe_name(f, "manifest file")
        unhashed = [f for f in data.get("files") or [] if f not in (data.get("file_hashes") or {})]
        if unhashed:  # an install copies only what the manifest vouches for byte by byte
            raise UnsafePath(f"manifest file {unhashed[0]!r} is not accepted: the manifest records no hash for it")
        paths = {f: inside(pkg, f, "manifest file", write=False) for f in data.get("file_hashes") or {}}
    except UnsafePath as e:
        raise Refused(f"{e}; nothing was written") from e
    bad = []
    for f, rec in (data.get("file_hashes") or {}).items():
        p = paths[f]
        if not p.is_file():
            bad.append(f"{f} missing")
        elif rec.get("sha256") and _sha_file(p) != rec["sha256"]:
            bad.append(f"{f} differs from its recorded hash")
    if bad:
        raise Refused("the package does not match its manifest: " + "; ".join(bad[:5]))
    return data


def _checked_engine_version(manifest: dict[str, Any]) -> str | None:
    rec = (manifest.get("validation") or {}).get("engine_import") or {}
    return rec.get("engine_version")


def _version_warnings(manifest: dict[str, Any], project: ProjectInfo) -> list[str]:
    out = []
    checked = _checked_engine_version(manifest)
    if checked is None:
        out.append(f"the package does not record which {project.engine} version it was checked against "
                   "(manifest validation.engine_import.engine_version)")
    elif project.version and (a := version_tuple(checked)) and (b := version_tuple(project.version)):
        n = min(len(b), 2)
        if a[:n] != b[:n]:
            older = "newer" if b[:n] > a[:n] else "older"
            out.append(f"this project's engine ({project.version}) is {older} than the one the package was checked "
                       f"against ({checked})")
    return out


# --------------------------------------------------------------------------- destination and record


def resolve_dest(project: Path, dest: str | None, asset: str) -> str:
    rel = (dest or f"agy3d/{asset}").strip()
    if rel.startswith("res://"):
        rel = rel[len("res://"):]
    rel = rel.strip("/")
    target = (project / rel).resolve()
    proj = project.resolve()
    if not rel or target == proj or not target.is_relative_to(proj):
        raise Refused(f"--dest {dest!r} must be a directory inside the project")
    parts = Path(rel).parts
    if any(p.startswith(".") for p in parts):
        raise Refused(f"--dest {dest!r}: a directory whose name starts with '.' is hidden from the engine")
    if (target / "project.godot").exists():
        raise Refused(f"--dest {dest!r} is a project root")
    return Path(rel).as_posix()


def read_record(dest_dir: Path) -> dict[str, Any] | None:
    p = dest_dir / RECORD
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except ValueError as e:
        raise Refused(f"{p} is not valid JSON ({e}); nothing was changed") from e


def edited(dest_dir: Path, rel: str, rec: dict[str, Any]) -> str | None:
    """Why the tool's own file ``rel`` is no longer what it wrote (None: it is). ``rel`` has passed safe_name."""
    p = inside(dest_dir, rel, "install record entry", write=False)
    if not p.is_file():
        return "deleted"
    if "params" in rec:  # an engine import file: the engine rewrites the rest of it; the settings are ours
        from antigravity3d.deliver.godot import read_import_params

        now = read_import_params(p.read_text(errors="replace"))
        diff = sorted(k for k, v in rec["params"].items() if now.get(k) != v)
        return f"import settings changed: {', '.join(diff)}" if diff else None
    return None if _sha_file(p) == rec["sha256"] else "content changed"


def _check_record_names(record: dict[str, Any]) -> None:
    """Every name an install record holds, held to what an install can have written (it is a file in the project,
    which anyone can edit or commit): refuse the whole operation on the first one that is not."""
    try:
        files = record.get("files")
        if not isinstance(files, dict):
            raise UnsafePath(f"install record files {type(files).__name__} is not accepted: a mapping of names is expected")
        for f in files:
            safe_name(f, "install record entry", INSTALL_FOLDERS)
        for g in record.get("engine_generated") or []:
            safe_name(g, "install record entry", MATERIAL_FOLDERS)
            if not g.endswith(".tres"):
                raise UnsafePath(f"install record entry {g!r} is not accepted: Godot's extracted materials are .tres files")
    except UnsafePath as e:
        raise Refused(f"{e}; nothing was changed") from e


class _Transaction:
    """File writes under DEST that are all undone if any step fails. Every name is relative to DEST and checked on disk
    at the moment of the write or removal (deliver/paths.inside): a folder swapped for a symbolic link after planning
    still cannot redirect it."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.created: list[Path] = []
        self.dirs: list[Path] = []
        self.saved: dict[Path, bytes] = {}

    def _mkdirs(self, d: Path) -> None:
        missing = []
        while not d.exists():
            missing.append(d)
            d = d.parent
        for m in reversed(missing):
            m.mkdir()
            self.dirs.append(m)

    def ensure_root(self) -> None:
        """DEST and its missing parents (resolve_dest has placed DEST inside the project)."""
        self._mkdirs(self.root)

    def mkdir(self, rel: str) -> None:
        self._mkdirs(inside(self.root, rel, "install folder", write=True))

    def write(self, rel: str, data: bytes) -> Path:
        p = inside(self.root, rel, "install file", write=True)
        self._mkdirs(p.parent)
        p = inside(self.root, rel, "install file", write=True)  # again, now that its folders exist
        if p.exists():
            self.saved.setdefault(p, p.read_bytes())
        else:
            self.created.append(p)
        tmp = p.with_name(f".agy3d-tmp-{p.name}")  # hidden: an editor scanning the folder skips it
        tmp.write_bytes(data)
        os.replace(tmp, p)
        return p

    def remove(self, rel: str) -> None:
        p = inside(self.root, rel, "install file", write=False)
        if p.exists():
            self.saved.setdefault(p, p.read_bytes())
            p.unlink()

    def _real(self, p: Path) -> bool:
        """True while nothing between DEST and ``p`` is a link: rollback never acts through one (a folder may have been
        swapped for a link, which is why it is rolling back)."""
        try:
            inside(self.root, p.relative_to(self.root).as_posix(), "rollback", write=True)
            return True
        except (UnsafePath, ValueError):
            return p == self.root or not p.is_relative_to(self.root) and not p.is_symlink()

    def rollback(self) -> None:
        for p in self.created:
            if self._real(p):
                p.unlink(missing_ok=True)
                p.with_name(f".agy3d-tmp-{p.name}").unlink(missing_ok=True)
        for p, data in self.saved.items():
            if self._real(p):
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
        for d in reversed(self.dirs):  # only directories this transaction made, deepest first; never a link
            if not d.is_symlink() and d.is_dir() and not any(d.iterdir()):
                d.rmdir()


def _prune_empty(start: Path, stop: Path) -> None:
    d = start
    while d != stop and d.is_relative_to(stop) and d.is_dir() and not any(d.iterdir()):
        d.rmdir()
        d = d.parent


# --------------------------------------------------------------------------- checks on what the engine measured


def godot_checks(manifest: dict[str, Any], record: dict[str, Any], measured: dict[str, Any]) -> list[Check]:
    from antigravity3d.deliver.godot import HULL_POINT_LIMIT, HYSTERESIS
    from antigravity3d.kernel.types import bounds_issue

    checks: list[Check] = []
    imp = measured["import"]
    scene = measured["scene"]
    lines = [*imp["asset_lines"], *scene["asset_lines"]]
    if imp["exit"] != 0:
        checks.append(Check("import", "failed", f"godot --import exited {imp['exit']}: {imp['log_tail'][-300:]}"))
    elif lines:
        checks.append(Check("import", "failed", f"{len(lines)} error/warning line(s) name the asset: " + " || ".join(lines[:4]),
                            {"lines": lines}))
    else:
        checks.append(Check("import", "passed", "headless import and scene load logged no error or warning naming the asset"))
    data = scene.get("data")
    if not data or "levels" not in data:
        why = (data or {}).get("load_error") or f"exit {scene['exit']}: {scene['log_tail'][-300:]}"
        checks.append(Check("scene", "failed", f"the installed scene did not load: {why}"))
        return checks
    checks.append(Check("scene", "passed", f"{record['asset']}.tscn loaded in Godot {data['engine_version']}"))
    by_name = {lv["name"]: lv for lv in data["levels"]}

    # scale and axes: LOD0's world-space vertices against the package's own record
    lod0 = by_name.get("LOD0")
    if lod0 is None or not lod0["meshes"]:
        checks.append(Check("scale", "failed", "LOD0 has no mesh in the scene"))
    else:
        issue = bounds_issue(manifest["glb_bounds_m"], lod0["bounds_m"])
        checks.append(Check("scale", "failed" if issue else "passed",
                            issue or "LOD0 bounds in the scene equal manifest glb_bounds_m within 0.5 % per axis face",
                            {"expected": manifest["glb_bounds_m"], "measured": lod0["bounds_m"]}))

    # levels: present, triangle count, ranges as planned, one shared AABB, no engine-generated LODs
    problems = []
    lod_tris = manifest.get("lod_tris") or [manifest["tris"]]
    for plan in record["levels"]:
        lv = by_name.get(f"LOD{plan['index']}")
        if lv is None or not lv["meshes"]:
            problems.append(f"LOD{plan['index']} missing")
            continue
        tris = sum(m["tris"] for m in lv["meshes"])
        if plan["index"] < len(lod_tris) and tris != lod_tris[plan["index"]]:
            problems.append(f"LOD{plan['index']} has {tris} triangles, manifest lod_tris says {lod_tris[plan['index']]}")
        if len(lv["meshes"]) != len(plan["nodes"]):
            problems.append(f"LOD{plan['index']} has {len(lv['meshes'])} mesh node(s), the GLB {len(plan['nodes'])}")
        for m in lv["meshes"]:
            for key in ("begin", "begin_margin", "end", "end_margin"):
                if abs(m[key] - plan[key]) > 1e-3 * max(1.0, plan[key]):
                    problems.append(f"LOD{plan['index']}/{m['node']} visibility_range_{key} {m[key]:.3f} != {plan[key]:.3f}")
            if bounds_issue(manifest["glb_bounds_m"], m["custom_aabb"]):
                problems.append(f"LOD{plan['index']}/{m['node']} custom_aabb is not the asset's bounds")
            if any(s["lods"] for s in m["surfaces"]):
                problems.append(f"LOD{plan['index']}/{m['node']} has engine-generated LODs (meshes/generate_lods)")
    extra = sorted(set(by_name) - {f"LOD{p['index']}" for p in record["levels"]})
    if extra:
        problems.append(f"unexpected level node(s) {extra}")
    switch = ", ".join(f"LOD{p['index']}->LOD{p['index'] + 1} at {p['switch_in_m']:.2f} m" for p in record["levels"] if p["switch_in_m"])
    checks.append(Check("levels", "failed" if problems else "passed", "; ".join(problems[:6]) or
                        f"{len(record['levels'])} level(s) with their triangle counts; " + (switch or "a single level, no switch")))

    # materials: every surface's maps bound to the one installed copy, VRAM-compressed with mipmaps
    problems, bound = [], 0
    tex_prefix = f"res://{record['dest']}/textures/"
    for plan in record["levels"]:
        lv = by_name.get(f"LOD{plan['index']}")
        want = record["maps"].get(plan["file"], {})
        for m in (lv or {}).get("meshes", []):
            for s in m["surfaces"]:
                if s["material"] not in want:
                    problems.append(f"LOD{plan['index']}/{m['node']}: material {s['material']!r} is not one of the GLB's {sorted(want)}")
                    continue
                for slot in want[s["material"]]:
                    t = s.get(slot)
                    if not t or not t.get("path"):
                        problems.append(f"LOD{plan['index']}/{m['node']} {s['material']}: {slot} map not bound")
                    elif not t["path"].startswith(tex_prefix):
                        problems.append(f"{s['material']} {slot}: bound to {t['path']}, not the installed {tex_prefix}")
                    elif not (t["compressed"] and t["mipmaps"]):
                        problems.append(f"{t['path']}: format {t['format']}, compressed={t['compressed']}, mipmaps={t['mipmaps']}")
                    else:
                        bound += 1
    shared: dict[str, set[str]] = {}
    for lv in data["levels"]:
        for m in lv["meshes"]:
            for s in m["surfaces"]:
                shared.setdefault(str(s["material"]), set()).add(str(s.get("resource")))
    mat_prefix = f"res://{record['dest']}/materials/"
    for name, paths in sorted(shared.items()):
        if len(paths) != 1 or not next(iter(paths)).startswith(mat_prefix):
            problems.append(f"material {name!r} is not one shared resource under {mat_prefix} on every level: {sorted(paths)}")
    checks.append(Check("materials", "failed" if problems else "passed", "; ".join(problems[:6]) or
                        f"{bound} map binding(s) on every level, each to the installed textures/ file, VRAM-compressed with mipmaps; "
                        f"{len(shared)} material(s), each one resource shared by every level ({', '.join(sorted(mat_prefix + n + '.tres' for n in shared))})"))

    # collision
    want_hulls = record["hulls"]
    bodies = data.get("bodies") or []
    if not want_hulls:
        checks.append(Check("collision", "skipped", "the package has no collision hulls (manifest collider_glb_nodes is empty)"
                            + ("" if not bodies else f"; but the scene has {len(bodies)} body/bodies")))
    else:
        problems = []
        limit = (manifest.get("budget") or {}).get("max_collision_hulls")
        if len(bodies) != 1:
            problems.append(f"{len(bodies)} collision bodies, expected one StaticBody3D")
        shapes = [s for b in bodies for s in b["shapes"]]
        if len(shapes) != len(want_hulls):
            problems.append(f"{len(shapes)} shapes, the package has {len(want_hulls)} hulls (manifest colliders)")
        if limit and len(shapes) > limit:
            problems.append(f"{len(shapes)} shapes > the package budget's {limit}")
        for s in shapes:
            if s["class"] != "ConvexPolygonShape3D":
                problems.append(f"{s['node']} is a {s['class']}")
            elif s["points"] > HULL_POINT_LIMIT:
                problems.append(f"{s['node']} has {s['points']} points > {HULL_POINT_LIMIT} (Jolt keeps at most that many)")
            elif want_hulls.get(s["node"]) not in (None, s["points"]):
                problems.append(f"{s['node']} has {s['points']} points, the package hull {want_hulls[s['node']]}")
        checks.append(Check("collision", "failed" if problems else "passed", "; ".join(problems[:6]) or
                            f"one {bodies[0]['class']} with {len(shapes)} convex shape(s), each within {HULL_POINT_LIMIT} points "
                            f"(project physics engine setting: {data.get('physics_engine')})"))

    # switching, observed in a rendered frame
    render = measured.get("render") or {}
    if "skipped" in render:
        checks.append(Check("level_switching", "skipped", render["skipped"]))
    elif not render.get("data"):
        checks.append(Check("level_switching", "failed", f"render run produced no result: {render.get('log_tail', '')[-300:]}"))
    elif len(record["levels"]) < 2:
        checks.append(Check("level_switching", "skipped", "the package has one level: nothing switches"))
    else:
        from antigravity3d.deliver.godot import switch_probes

        problems, seen = [], []
        dists = [p["switch_in_m"] for p in record["levels"][:-1]]
        expected = [want for _, want in switch_probes(dists)]
        if len(render["data"]["probes"]) != len(expected):
            problems.append(f"{len(render['data']['probes'])} probe frames, expected {len(expected)}")
        for probe, want in zip(render["data"]["probes"], expected):
            px = probe["level_pixels"]
            total = sum(px)
            got = max(range(len(px)), key=lambda i: px[i]) if total else None
            seen.append({"distance_m": round(probe["distance_m"], 3), "expected": f"LOD{want}", "pixels": px})
            if total < 20:
                problems.append(f"at {probe['distance_m']:.2f} m only {total} level pixels were drawn")
            elif got != want or px[want] < 0.98 * total:
                problems.append(f"at {probe['distance_m']:.2f} m LOD{want} expected, pixels per level {px}")
        checks.append(Check("level_switching", "failed" if problems else "passed", "; ".join(problems[:4]) or
                            "rendered: the expected level alone at every stop, moving out past each switch and back in, including "
                            "the middle of each hysteresis band from both sides "
                            f"(switch at {', '.join(f'{d:.2f}' for d in dists)} m, hysteresis {HYSTERESIS:.1%})", {"probes": seen}))
    return checks


# --------------------------------------------------------------------------- the interface


def _not_built(engine: str, manifest: dict[str, Any], pkg: Path, info: ProjectInfo) -> InstallResult:
    asset = manifest["asset"]
    fbx = (manifest.get("fbx") or {}).get("file") if (manifest.get("fbx") or {}).get("status") == "written" else None
    what = {
        "unity": f"import {fbx or asset + '.glb'} (with its textures/ folder) into Assets/" + ("" if fbx else " with glTFast"),
        "unreal": f"import {fbx or asset + '.glb'} with the Content Browser's Import (Interchange)",
        "roblox": f"import {fbx or asset + '.glb'} with Studio's 3D Importer; assign the maps that manifest "
                  "roblox_surface_appearance names to a SurfaceAppearance",
    }[engine]
    return InstallResult("not_built", f"the package is ready for {engine} ({pkg}): {what}. Automatic installation into a "
                         f"{engine} project is not built yet; nothing was written.", engine, info.to_dict(),
                         {"asset": asset, "path": str(pkg), "target_engine": manifest["target_engine"]})


def install_package(pkg: str | Path, project: str | Path, dest: str | None = None, dry_run: bool = False,
                    force: bool = False, check: bool = True, *, fov_deg: float | None = None,
                    godot_bin: str | None = None, render: bool = True) -> InstallResult:
    """Install the package at ``pkg`` into the game project at ``project`` (see the module docstring)."""
    pkg, project = Path(pkg), Path(project)
    info = detect_project(project)
    try:
        manifest = read_package(pkg)
    except Refused as e:
        return InstallResult("refused", str(e), info.engine, info.to_dict())
    package = {"asset": manifest["asset"], "path": str(pkg), "target_engine": manifest["target_engine"],
               "schema_version": manifest["schema_version"], "checked_engine_version": _checked_engine_version(manifest)}
    if not info.known:
        return InstallResult("refused", f"cannot tell which engine {project} is: {info.reason}", None, info.to_dict(), package)
    target = TARGET_OF.get(info.engine)
    if target != manifest["target_engine"]:
        if target is None:
            why = f"{info.engine} has no package target; the package's {manifest['asset']}.glb loads there as a plain glTF"
        else:
            why = f"re-export it for this project: agy3d prop ... --engine {target}"
        return InstallResult("refused", f"the package was built for {manifest['target_engine']} but {project} is a "
                             f"{info.engine} project ({', '.join(info.evidence)}); {why}. Nothing was written.",
                             info.engine, info.to_dict(), package)
    if info.engine != "godot":
        return _not_built(info.engine, manifest, pkg, info)
    if info.version and (v := version_tuple(info.version)) and v[0] != 4:
        return InstallResult("refused", f"this is a Godot {info.version} project; the package is for Godot 4", info.engine,
                             info.to_dict(), package)
    try:
        return _install_godot(pkg, project, manifest, info, package, dest, dry_run, force, check,
                              fov_deg=fov_deg, godot_bin=godot_bin, render=render)
    except Refused as e:
        return InstallResult("refused", str(e), info.engine, info.to_dict(), package)


def _install_godot(pkg: Path, project: Path, manifest: dict[str, Any], info: ProjectInfo, package: dict[str, Any],
                   dest: str | None, dry_run: bool, force: bool, check: bool, *, fov_deg: float | None,
                   godot_bin: str | None, render: bool, _write_hook: Callable[[Path], None] | None = None) -> InstallResult:
    from antigravity3d.deliver import godot
    from antigravity3d.kernel.lod_diff import DEFAULT_FOV_DEG

    rel = resolve_dest(project, dest, manifest["asset"])
    dest_dir = project / rel
    try:
        no_links(dest_dir, f"the destination {rel}")
    except UnsafePath as e:
        raise Refused(f"{e}; nothing was changed") from e
    old = read_record(dest_dir) if dest_dir.is_dir() else None
    if old is not None:
        _check_record_names(old)
    if old is None and dest_dir.is_dir() and any(dest_dir.iterdir()):
        raise Refused(f"{rel} exists and holds files this tool did not write (no {RECORD}); choose another --dest")
    if old is not None and old.get("asset") != manifest["asset"]:
        raise Refused(f"{rel} holds the install of {old.get('asset')!r}, not {manifest['asset']!r}; choose another --dest")
    fov = float(fov_deg if fov_deg is not None else (old or {}).get("fov_deg", DEFAULT_FOV_DEG))
    try:
        plan = godot.build_plan(pkg, manifest, rel, fov_deg=fov)
    except ValueError as e:
        raise Refused(f"cannot install this package: {e}") from e
    try:
        for f in plan.files:
            safe_name(f, "install file", INSTALL_FOLDERS)
        generated = sorted({safe_name(f"{godot.MATERIAL_DIR}/{n}.tres", "glTF material name", MATERIAL_FOLDERS)
                            for lv in plan.levels for n in godot.material_names(plan.files[lv.file])})
    except UnsafePath as e:
        raise Refused(f"{e}; nothing was written") from e
    maps = {lv.file: godot.expected_maps_bytes(plan.files[lv.file]) for lv in plan.levels}
    tracked = {f: {"sha256": _sha(b), "bytes": len(b), **({"params": plan.imports[f]} if f in plan.imports else {})}
               for f, b in plan.files.items()}
    record = {
        "schema": RECORD_SCHEMA, "tool": "antigravity3d", "asset": manifest["asset"], "dest": rel, "engine": "godot",
        "package": {**package, "manifest_sha256": _sha_file(pkg / "manifest.json")},
        "project": {"engine": info.engine, "version": info.version}, "fov_deg": fov,
        "levels": [asdict(lv) for lv in plan.levels], "hulls": plan.hulls, "centre": plan.centre,
        "radius_m": plan.radius_m, "switch_m": [lv.switch_in_m for lv in plan.levels[:-1]], "maps": maps,
        "image_rewrites": plan.image_rewrites, "files": tracked,
        # written by Godot on import (materials/extract), not by this tool: shared by every level, the developer's to edit
        "engine_generated": generated,
    }

    # what each file needs, and what stands in the way
    actions: dict[str, str] = {}
    blocked, edits = [], []
    old_files = (old or {}).get("files", {})
    for f, rec in tracked.items():
        p = inside(dest_dir, f, "install file", write=True) if dest_dir.is_dir() else dest_dir / f
        if f in old_files:
            why = edited(dest_dir, f, old_files[f])
            if why:
                edits.append(f"{f}: {why}")
            unchanged = why is None and old_files[f].get("sha256") == rec["sha256"] and old_files[f].get("params") == rec.get("params")
            actions[f] = "keep" if unchanged else "replace"
        elif p.exists():
            blocked.append(f)
        else:
            actions[f] = "create"
    for f in old_files:
        if f not in tracked:
            why = edited(dest_dir, f, old_files[f])
            if why and why != "deleted":
                edits.append(f"{f}: {why}")
            actions[f] = "remove"
    if blocked:
        raise Refused(f"file(s) {blocked[:5]} exist in {rel} and were not written by this tool; nothing was changed")
    if edits and not force:
        raise Refused(f"file(s) of the earlier install were edited since: {'; '.join(edits[:6])}. Nothing was changed; "
                      "pass --force to replace them with the package's version")
    status = "dry_run" if dry_run else ("installed" if old is None else
                                        "unchanged" if all(a == "keep" for a in actions.values()) else "updated")
    result = InstallResult(status, "", "godot", info.to_dict(), package, rel,
                           [{"path": f, "action": a, **({k: tracked[f][k] for k in ("sha256", "bytes")} if f in tracked else {})}
                            for f, a in sorted(actions.items())],
                           warnings=_version_warnings(manifest, info), notes=[*plan.notes, godot.godot_camera_note()])
    if edits:
        result.warnings.append(f"replaced edited file(s) (--force): {'; '.join(edits[:6])}")
    exe = godot.find_godot(godot_bin)
    if exe and (bv := godot.godot_version(exe)) and info.version and (a := version_tuple(bv)) and (b := version_tuple(info.version)):
        if a[:len(b[:2])] != b[:2]:
            result.warnings.append(f"the Godot binary used for the check is {bv}; the project says {info.version}")
    if dry_run:
        n = {a: sum(v == a for v in actions.values()) for a in ("create", "replace", "keep", "remove")}
        result.message = (f"dry run: would write {rel}: {n['create']} new, {n['replace']} replaced, {n['keep']} kept, "
                          f"{n['remove']} removed; nothing was written")
        return result

    txn = _Transaction(dest_dir)
    try:
        txn.ensure_root()
        for f, a in sorted(actions.items()):
            if a in ("create", "replace"):
                written = txn.write(f, plan.files[f])
                if _write_hook:
                    _write_hook(written)
            elif a == "remove":
                txn.remove(f)
        txn.mkdir(godot.MATERIAL_DIR)  # Godot extracts the shared materials here (godot.scene_params)
        txn.write(f"{RECORD_DIR}/.gdignore", b"")  # Godot does not scan or import this folder
        txn.write(f"{RECORD_DIR}/package_manifest.json", (pkg / "manifest.json").read_bytes())
        txn.write(RECORD, json.dumps(record, indent=1).encode())
    except UnsafePath as e:  # something on disk changed under DEST since it was checked
        txn.rollback()
        raise Refused(f"{e}; nothing was changed") from e
    except BaseException:
        txn.rollback()
        raise
    for f, a in actions.items():
        if a == "remove":
            _prune_empty((dest_dir / f).parent, dest_dir)
    result.message = {"installed": f"installed {manifest['asset']} into {rel}",
                      "updated": f"updated {rel} to the package's current files",
                      "unchanged": f"{rel} already holds this package; nothing changed"}[status]
    if check:
        _check_into(result, project, rel, manifest, record, godot_bin, render)
    return result


def _check_into(result: InstallResult, project: Path, rel: str, manifest: dict[str, Any], record: dict[str, Any],
                godot_bin: str | None, render: bool) -> None:
    from antigravity3d.deliver import godot

    exe = godot.find_godot(godot_bin)
    if exe is None:
        result.checks.append(Check("in_project", "skipped", "no Godot binary (set $GODOT_BIN, pass --godot, or put godot on PATH)"))
        return
    with tempfile.TemporaryDirectory(prefix="agy3d-deliver-") as td:
        try:
            measured = godot.run_check(exe, project, rel, record, Path(td), render=render)
            result.checks.extend(godot_checks(manifest, record, measured))
        except (OSError, ValueError, KeyError, TypeError) as e:  # the install stands; its proof could not be read
            result.checks.append(Check("in_project", "failed", f"the check could not complete: {type(e).__name__}: {e}"))
            return
        out = project / rel / RECORD_DIR / "check"
        out.mkdir(parents=True, exist_ok=True)
        shot = (measured.get("render") or {}).get("screenshot")
        if shot:
            shutil.copyfile(shot, out / "godot.png")
            result.screenshot = str(out / "godot.png")
        if measured.get("isolated_copy_left_out"):
            result.notes.append(f"checked in an isolated copy of the project without its {measured['isolated_copy_left_out']} "
                                "section(s), so none of the project's own code ran")
        (out / "report.json").write_text(json.dumps({**result.to_dict(), "measured": _strip(measured)}, indent=1))
        result.report_path = str(out / "report.json")


def _strip(measured: dict[str, Any]) -> dict[str, Any]:
    return {k: {kk: vv for kk, vv in v.items() if kk != "log_tail"} if isinstance(v, dict) else v for k, v in measured.items()}


def check_install(project: str | Path, dest: str, *, godot_bin: str | None = None, render: bool = True) -> InstallResult:
    """Re-run the in-project check of an existing install (after the developer changed something)."""
    project = Path(project)
    rel = resolve_dest(project, dest, "_")
    try:
        no_links(project / rel, f"the destination {rel}")
        record = read_record(project / rel)
        if record is not None:
            _check_record_names(record)
    except (UnsafePath, Refused) as e:
        return InstallResult("refused", str(e), dest=rel)
    if record is None:
        return InstallResult("refused", f"{rel} has no {RECORD}: nothing installed there")
    manifest = json.loads((project / rel / RECORD_DIR / "package_manifest.json").read_text())
    result = InstallResult("unchanged", f"checked {rel}", "godot", detect_project(project).to_dict(), record["package"], rel)
    _check_into(result, project, rel, manifest, record, godot_bin, render)
    return result


def uninstall(project: str | Path, dest: str, *, dry_run: bool = False, force: bool = False) -> InstallResult:
    """Remove exactly what the install at ``dest`` wrote (refuses when one of its files was edited, unless ``force``)."""
    project = Path(project)
    try:
        rel = resolve_dest(project, dest, "_")
        dest_dir = project / rel
        try:
            no_links(dest_dir, f"the destination {rel}")
        except UnsafePath as e:
            raise Refused(f"{e}; nothing was removed") from e
        record = read_record(dest_dir) if dest_dir.is_dir() else None
        if record is None:
            raise Refused(f"{rel} has no {RECORD}: nothing installed there by this tool")
        _check_record_names(record)
        edits = [f"{f}: {why}" for f, rec in record["files"].items() if (why := edited(dest_dir, f, rec)) and why != "deleted"]
        if edits and not force:
            raise Refused(f"file(s) were edited since the install: {'; '.join(edits[:6])}. Nothing was removed; pass --force")
    except Refused as e:
        return InstallResult("refused", str(e), dest=dest)
    files = [{"path": f, "action": "remove"} for f in sorted(record["files"]) if (dest_dir / f).exists()]
    generated = [g for g in record.get("engine_generated", []) if (dest_dir / g).is_file()]
    files += [{"path": g, "action": "remove", "written_by": "godot import (materials/extract)"} for g in generated]
    others = sorted(str(p.relative_to(dest_dir)) for p in dest_dir.rglob("*") if p.is_file()
                    and not p.relative_to(dest_dir).as_posix().startswith(RECORD_DIR + "/")
                    and p.relative_to(dest_dir).as_posix() not in {*record["files"], *generated})
    result = InstallResult("dry_run" if dry_run else "uninstalled", "", record.get("engine"), None, record.get("package"), rel, files)
    if others:
        result.notes.append(f"kept {len(others)} file(s) this tool did not write: {others[:5]}")
    if edits:
        result.warnings.append(f"removed edited file(s) (--force): {'; '.join(edits[:6])}")
    if dry_run:
        result.message = f"dry run: would remove {len(files)} file(s) and {RECORD_DIR}/ from {rel}; nothing was removed"
        return result
    if generated:
        result.notes.append(f"removed Godot's material file(s) {generated} (made on import from the installed GLBs, edits "
                            "to them included): left behind they would point at the textures removed here")
    for f in [*record["files"], *generated]:  # names checked above; the path again now, as it is removed
        inside(dest_dir, f, "install record entry", write=False).unlink(missing_ok=True)
    shutil.rmtree(dest_dir / RECORD_DIR, ignore_errors=True)  # no_links above: a real folder, not a link
    dirs = {(dest_dir / f).parent for f in record["files"]} | ({dest_dir / "materials"} if "engine_generated" in record else set())
    for d in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
        _prune_empty(d, dest_dir)
    if dest_dir.is_dir() and not any(dest_dir.iterdir()):
        dest_dir.rmdir()
        _prune_empty(dest_dir.parent, project)
    result.message = f"removed {len(files)} file(s) from {rel}" + (f"; kept {len(others)} not written by this tool" if others else "")
    return result


__all__ = ["install_package", "uninstall", "check_install", "InstallResult", "Check", "Refused", "read_package",
           "godot_checks"]
