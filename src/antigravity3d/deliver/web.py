"""three.js / React Three Fiber projects: the files an install writes, and the optional compression for the web.

What is written (all names relative to the project):

* ``<served>/agy3d/<asset>/``  the asset's files, where the dev server serves them unchanged: the level GLBs, one
  ``textures/`` folder, ``collision.json``. A bundler hashes and renames what it processes (``src/``), which breaks a
  GLB that fetches its textures by name; ``public/`` (Vite, Next, CRA, Astro) and ``static/`` (SvelteKit) are copied
  as they are.
* ``<served>/agy3d/_basis/``  the KTX2 transcoder, copied from the project's own three.js (compressed installs only).
  One copy for every asset of the project: a page downloads it once and runs one worker pool.
* ``src/agy3d/<asset>/``  ``index.js`` + ``index.d.ts`` (and ``react.js`` + ``react.d.ts`` with @react-three/fiber),
  and the install record in ``.agy3d/``. The record is not under the served folder: a production build copies that
  folder whole, and a record, a manifest copy and check screenshots have no business on a web server.

Compression (KTX2 textures that stay compressed on the GPU, meshopt geometry) is done by ``web_compress.mjs`` with
packages the project already has in ``node_modules``; see :func:`compression_tools`. Nothing is downloaded.

Choices below were measured on three.js 0.169 and 0.170 projects (Vite 5/6, React Three Fiber 8) with the three
round-six assets; the numbers are in the comments that use them.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np

from antigravity3d.deliver import glb as glbtool
from antigravity3d.deliver.detect import ProjectInfo, version_tuple
from antigravity3d.deliver.godot import HYSTERESIS, _gltf_mesh_node_paths, material_names
from antigravity3d.deliver.paths import UnsafePath, asset_name, inside, safe_name
from antigravity3d.kernel.lod_diff import DEFAULT_FOV_DEG, transition_distance

HERE = Path(__file__).parent
TEXTURE_DIR = "textures"
# three.js revisions (the minor number of 0.<rev>.0) the in-project check has been run on: the supported range is
# exactly what was run, nothing wider (see SUPPORTED_NOTE; outside it the install still writes and says so).
SUPPORTED_REVISIONS: tuple[int, int] = (152, 187)
SUPPORTED_RUNS = ("0.152.2", "0.160.0", "0.169.0", "0.170.0", "0.187.0")  # the versions the in-project check was run on
# the web_compress.mjs imports: all must be in the project's node_modules
TOOL_PACKAGES = ("@gltf-transform/core", "@gltf-transform/extensions", "@gltf-transform/functions", "ktx2-encoder",
                 "meshoptimizer", "sharp")
SERVED_FOLDERS = ("public", "static")
ENABLE_COMPRESSION = "npm install -D " + " ".join(TOOL_PACKAGES)
# the transcoder and its loader files, in the project's three.js
BASIS_FILES = ("basis_transcoder.js", "basis_transcoder.wasm")
BASIS_IN_THREE = "examples/jsm/libs/basis"
COMPRESS_MODES = ("auto", "always", "never")
# ktx2-encoder options per texture role, as an ordered ladder from the smallest files to the safest. The install encodes
# the first tier, measures the frame difference in the browser (the tolerance of deliver/web_check.py) and moves down
# the ladder only while the tolerance fails (``build_plan``'s probe), so a compressed install is always inside it and
# its size is what that takes. Measured on the three round-six assets (frame against the package's own GLB, share of
# the asset's pixels over 8 % of full scale; the limit is 1 %; bytes are the asset's files):
#   tier 0, ETC1S albedo and ORM at quality 255, UASTC normal with RDO 1:  crate 0.85 % / 1.12 MB, chair 0.16 % /
#       0.68 MB, hydrant 6.0 % (fails: ETC1S blurs the metal edges' highlights)
#   tier 1, ORM as UASTC (RDO 1) as well:  hydrant 3.1 % (fails: the albedo's ETC1S too)
#   tier 2, every map UASTC with RDO 3:  hydrant 0.15 % / 1.50 MB
#   tier 3, every map UASTC without RDO: hydrant 0.08 % / 1.97 MB; the offline test crate (noise textures, 512 px) is
#       outside the tolerance at tier 2 (1.16 %) and inside at this one
# Why per role: an ETC1S block stores one colour pair and assumes the channels are correlated (albedo mostly is; ORM is
# three unrelated data channels: 26 dB against 49 dB as UASTC on the crate); normals are UASTC always (ETC1S: mean 0.85
# degrees but 99th percentile 7 degrees, which shades as noise; UASTC 0.13 and 1.7; RDO 1 0.26 and 2.7).
# The ladder is also in order of size, and ``auto`` relies on it: a tier whose files are already larger than the
# uncompressed ones ends the search (``_choose_tier``). Since round eight a web package carries WebP maps, which every
# tier here exceeds: run 2 of the website task (1024 px atlas, 766,200 bytes of WebP files) measured 916,515, 1,466,961,
# 1,861,869 and 2,767,056 bytes in 25, 25, 63 and 25 s of encoding, and each of its installs encoded three of them and
# compared two in the browser (114 of 130 s) to keep none.
_UASTC = {"isUASTC": True, "uastcLDRQualityLevel": 2, "needSupercompression": True}
_ETC1S = {"isUASTC": False, "qualityLevel": 255, "compressionLevel": 2}
_COLOUR = {"isPerceptual": True, "isSetKTX2SRGBTransferFunc": True}
_DATA = {"isPerceptual": False, "isSetKTX2SRGBTransferFunc": False}
TEXTURE_TIERS: list[tuple[str, dict[str, dict[str, Any]]]] = [
    ("etc1s_albedo_and_orm", {"albedo": {**_ETC1S, **_COLOUR}, "orm": {**_ETC1S, **_DATA},
                              "normal": {**_UASTC, **_DATA, "isNormalMap": True, "enableRDO": True, "rdoQualityLevel": 1.0}}),
    ("uastc_orm", {"albedo": {**_ETC1S, **_COLOUR}, "orm": {**_UASTC, **_DATA, "enableRDO": True, "rdoQualityLevel": 1.0},
                   "normal": {**_UASTC, **_DATA, "isNormalMap": True, "enableRDO": True, "rdoQualityLevel": 1.0}}),
    ("uastc_everything", {"albedo": {**_UASTC, **_COLOUR, "enableRDO": True, "rdoQualityLevel": 3.0},
                          "orm": {**_UASTC, **_DATA, "enableRDO": True, "rdoQualityLevel": 3.0},
                          "normal": {**_UASTC, **_DATA, "isNormalMap": True, "enableRDO": True, "rdoQualityLevel": 3.0}}),
    ("uastc_no_rdo", {"albedo": {**_UASTC, **_COLOUR}, "orm": {**_UASTC, **_DATA}, "normal": {**_UASTC, **_DATA, "isNormalMap": True}}),
]
# glTF-Transform meshopt(): quantisation widths. 14-bit positions are 0.05 mm on a 0.85 m asset; the default 12-bit
# texture coordinates are half a texel on a 2048 px atlas, so 14.
MESHOPT_SETTINGS: dict[str, Any] = {"quantizePosition": 14, "quantizeNormal": 10, "quantizeTexcoord": 14, "quantizeColor": 8}


class ChildError(RuntimeError):
    """A child process (node, the project's esbuild) did not finish: it timed out, could not start or crashed."""


_NODE: dict[str, str | None] = {}


def node_executable() -> str | None:
    """The real node binary as an absolute path, resolved once here with this process's own environment. The ``node`` on
    PATH may be a shim (asdf, mise, Volta) that finds the real binary under HOME, which a bare environment replaces; a
    child started from the absolute path needs no shim. ``node -p process.execPath`` runs node itself and none of the
    project's code (it starts in the temporary folder)."""
    path = os.environ.get("PATH", "")
    if path not in _NODE:
        found = shutil.which("node")
        real = None
        if found:
            try:
                out = subprocess.run([found, "-p", "process.execPath"], capture_output=True, text=True, timeout=30,
                                     cwd=tempfile.gettempdir(), stdin=subprocess.DEVNULL).stdout.strip()
                real = out if out and os.path.isabs(out) and os.access(out, os.X_OK) else None
            except (OSError, subprocess.TimeoutExpired):
                real = None
        _NODE[path] = real
    return _NODE[path]


def bare_env(home: Path) -> dict[str, str]:
    """The environment of a process that runs the project's own npm code (node with its packages, its esbuild): PATH, a
    HOME and TMPDIR that are a throwaway folder, LANG, SYSTEMROOT on Windows, the worker count for texture encoding when
    it is set, and nothing else. A key in this process's
    environment is not for third-party code (build/runner.py does the same for asset scripts). The real node's folder
    leads PATH, so a script that starts ``env node`` (the project's esbuild shim) finds the real one before any version
    manager's shim."""
    path = os.environ.get("PATH") or "/usr/bin:/bin"
    node = node_executable()
    if node:
        path = os.pathsep.join([str(Path(node).parent), path])
    env = {"PATH": path, "HOME": str(home), "TMPDIR": str(home), "LANG": "C.UTF-8"}
    # AGY3D_KTX2_WORKERS is this tool's own setting, read by web_compress.mjs: how many texture maps it encodes at once
    # (each holds one encoder: measured 810 MiB for three 2048 px maps side by side, 424 MiB one at a time).
    for k in ("SYSTEMROOT", "COMSPEC", "PATHEXT", "AGY3D_KTX2_WORKERS"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def run_child(cmd: list[str], *, cwd: Path, timeout: float, input: str | None = None) -> tuple[int, str, str]:
    """Run ``cmd`` with a bare environment, in its own session, and kill the whole process group at the timeout (a
    ``subprocess.run`` timeout kills only the direct child: a grandchild keeps running and keeps the pipes open).
    Returns (exit code, stdout, stderr); raises :class:`ChildError` for a timeout or a process that cannot start."""
    with tempfile.TemporaryDirectory(prefix="agy3d-child-") as home:
        try:
            proc = subprocess.Popen(cmd, cwd=cwd, env=bare_env(Path(home)), stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    start_new_session=(os.name == "posix"))
        except OSError as e:
            raise ChildError(f"{cmd[0]} could not be started: {e}") from e
        try:
            out, err = proc.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            _kill_group(proc)
            raise ChildError(f"{Path(cmd[0]).name} did not finish within {timeout:.0f} s and was stopped") from e
        except BaseException:
            _kill_group(proc)
            raise
        return proc.returncode, out or "", err or ""


def _kill_group(proc: subprocess.Popen) -> None:
    import signal

    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.communicate(timeout=10)
    except Exception:  # noqa: BLE001  nothing more to do: the group is killed
        pass


class WebRefused(ValueError):
    """The project or the request cannot be installed into; the message says what to change. Nothing was written."""


# --------------------------------------------------------------------------- the project


def _package_json(project: Path) -> dict[str, Any]:
    try:
        data = json.loads((project / "package.json").read_text(errors="replace"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def dependencies(project: Path) -> set[str]:
    data = _package_json(project)
    return {k for key in ("dependencies", "devDependencies", "peerDependencies") if isinstance(data.get(key), dict)
            for k in data[key]}


def search_dirs(project: Path) -> list[Path]:
    """The folders whose ``node_modules`` belong to this project: the project, and above it only as far as the workspace
    root (a package.json with ``workspaces``: npm hoists packages there) or the repository root (a ``.git``), whichever
    comes first. Where neither exists above, the project alone: a stray node_modules in a home folder is not the
    project's, and what is found here is executed (compression) or served."""
    here = project.resolve()
    chain = [here]
    for d in here.parents:
        chain.append(d)
    for i, d in enumerate(chain):
        is_root = (d / ".git").exists() or bool(_package_json(d).get("workspaces"))
        if is_root:
            return chain[: i + 1]
    return [here]


def find_package(project: Path, name: str) -> Path | None:
    """``node_modules/<name>`` of the project, or of a folder above it up to its workspace or repository root."""
    for d in search_dirs(project):
        p = d / "node_modules" / name
        if (p / "package.json").is_file():
            return p
    return None


def installed_version(pkg_dir: Path | None) -> str | None:
    if pkg_dir is None:
        return None
    try:
        v = json.loads((pkg_dir / "package.json").read_text()).get("version")
    except (OSError, ValueError):
        return None
    return v if isinstance(v, str) else None


def three_revision(version: str | None) -> int | None:
    """0.170.0 -> 170 (three.js numbers its releases by the minor part)."""
    t = version_tuple(version)
    return t[1] if t and len(t) > 1 and t[0] == 0 else None


@dataclass
class ThreeInfo:
    version: str | None            # what is installed (node_modules), else what the project asks for
    revision: int | None
    path: str | None               # the installed package's folder, None when it is not installed
    installed: bool
    supported: bool | None         # None: not known
    note: str | None = None


def three_info(project: Path, info: ProjectInfo) -> ThreeInfo:
    pkg = find_package(project, "three")
    v = installed_version(pkg)
    rev = three_revision(v or info.version)
    lo, hi = SUPPORTED_REVISIONS
    supported = None if rev is None else lo <= rev <= hi
    note = None
    if pkg is None:
        note = "three is not installed (no node_modules/three): run npm install, then agy3d install again, to check it in the browser"
    elif supported is False:
        note = (f"not checked on this version: three.js {v} is outside r{lo} to r{hi}, the range this install has been "
                f"run on (three.js {', '.join(SUPPORTED_RUNS)}); the files are written, the loader may need changes")
    return ThreeInfo(v or info.version, rev, str(pkg) if pkg else None, pkg is not None, supported, note)


def compression_tools(project: Path) -> tuple[dict[str, str] | None, list[str], Path | None]:
    """(versions of the six packages, what is missing, the node_modules folder that holds them). Reads files only: it
    runs nothing from the project (a dry run uses this)."""
    found: dict[str, str] = {}
    missing: list[str] = []
    roots: set[Path] = set()
    for name in TOOL_PACKAGES:
        p = find_package(project, name)
        v = installed_version(p)
        if p is None or v is None:
            missing.append(name)
        else:
            found[name] = v
            roots.add(p.parents[1] if name.startswith("@") else p.parent)
    if node_executable() is None:
        missing.append("node (not on PATH, or it did not run)")
    three = find_package(project, "three")
    if three is not None and not all((three / BASIS_IN_THREE / f).is_file() for f in BASIS_FILES):
        missing.append("three's examples/jsm/libs/basis transcoder files")
    if missing:
        return None, missing, None
    if len(roots) != 1:
        return None, [f"the six packages are in different node_modules folders ({len(roots)})"], None
    return found, [], next(iter(roots))


# --------------------------------------------------------------------------- where things go


@dataclass
class Layout:
    code: str            # DEST: project-relative folder of the loader and the record
    assets: str          # project-relative folder of the asset's served files
    served: str          # the served root: public | static (first segment of ``assets``)
    assets_url: str      # path of ``assets`` below the served root (the site root is the served folder)
    basis: str           # project-relative folder of the shared transcoder
    basis_url: str
    rule: str            # which rule chose the served root, for the result

    def to_dict(self) -> dict[str, str]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def _rel_inside(project: Path, rel: str, what: str) -> str:
    rel = rel.strip().strip("/")
    if ".." in rel.split("/") or "\\" in rel:
        raise WebRefused(f"{what} {rel!r} must stay inside the project")
    target = (project / rel).resolve()
    proj = project.resolve()
    if not rel or target == proj or not target.is_relative_to(proj):
        raise WebRefused(f"{what} {rel!r} must be a directory inside the project")
    parts = Path(rel).parts
    if any(p.startswith(".") for p in parts) or "node_modules" in parts:
        raise WebRefused(f"{what} {rel!r}: a hidden folder or node_modules is not somewhere a page serves files from")
    return Path(rel).as_posix()


def _evidence(project: Path) -> tuple[str, str] | None:
    """(served folder, the rule) from the project's own files, or None where they say nothing."""
    deps = dependencies(project)
    if "@sveltejs/kit" in deps:
        return "static", "the project depends on @sveltejs/kit, which serves static/"
    if deps & {"vite", "next", "react-scripts", "astro"}:
        dep = sorted(deps & {"vite", "next", "react-scripts", "astro"})[0]
        return "public", f"the project depends on {dep}, which serves public/"
    if (project / "public").is_dir():
        return "public", "the project has a public/ folder"
    if (project / "static").is_dir():
        return "static", "the project has a static/ folder"
    return None


def _overlaps(a: str, b: str) -> bool:
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def choose_layout(project: Path, asset: str, dest: str | None, assets: str | None,
                  taken: list[tuple[str, list[str]]] | None = None) -> Layout:
    """``taken``: (loader folder, [its folders]) of each install in this project: neither ``--assets`` nor ``--dest`` may
    contain, lie inside or equal a folder of an install in another loader folder, nor the shared transcoder's folder."""
    found = _evidence(project)
    if assets:
        if assets.strip().startswith(("/", "\\")) or ":" in assets or "\\" in assets:
            raise WebRefused(f"--assets {assets!r} must be a relative folder inside the project, not an absolute path")
        rel = _rel_inside(project, assets, "--assets")
        served = rel.split("/")[0]
        if served not in SERVED_FOLDERS:
            raise WebRefused(f"--assets {assets!r}: its first folder must be one a dev server serves unchanged, "
                             f"{' or '.join(SERVED_FOLDERS)}")
        if found and found[0] != served:
            raise WebRefused(f"--assets {assets!r} is under {served}/, but {found[1]}: files there are not served")
        if rel == served:
            raise WebRefused(f"--assets {assets!r} is the served folder itself: name a folder inside it")
        rule = f"--assets {assets}: {served}/ is the served folder"
    else:
        if found is None:
            raise WebRefused("cannot tell which folder this project serves its files from (it does not depend on vite, next, "
                             "react-scripts, astro or @sveltejs/kit and has no public/ or static/ folder): pass --assets "
                             "<served folder>/<name> for the asset's files and --dest <folder> for the loader")
        served, rule = found
        rel = f"{served}/agy3d/{asset}"
    code = _rel_inside(project, dest, "--dest") if dest else (f"src/agy3d/{asset}" if (project / "src").is_dir() else f"agy3d/{asset}")
    if code == served or code.startswith(served + "/"):
        raise WebRefused(f"the loader folder {code!r}" + (f" (--dest {dest!r})" if dest else "") + f" is inside the served folder "
                         f"{served}/: the install record and the loader would be copied into the production build")
    if rel == code or rel.startswith(code + "/") or code.startswith(rel + "/"):
        raise WebRefused("--dest and --assets must be different folders, neither inside the other")
    basis = f"{served}/agy3d/_basis"
    for mine, what in ((rel, "--assets"), (code, "--dest")):
        if _overlaps(mine, basis):
            raise WebRefused(f"{what} {mine!r} contains or lies inside the shared transcoder folder {basis}")
        for other_dest, folders in taken or []:
            if other_dest == code:  # this install's own earlier record
                continue
            for other in folders:
                if _overlaps(mine, other):
                    raise WebRefused(f"{what} {mine!r} contains, lies inside or is the folder of another install ({other})")
    return Layout(code, rel, served, rel[len(served) + 1:], basis, "agy3d/_basis", rule)


def pascal(asset: str) -> str:
    return "".join(p[:1].upper() + p[1:] for p in asset.split("_") if p) or "Asset"


# --------------------------------------------------------------------------- the package's parts


@dataclass
class Parts:
    asset: str
    textures: dict[str, bytes]        # textures/<name>.png -> bytes
    roles: dict[str, str]
    level_files: list[str]            # LOD0 first
    levels: dict[str, bytes]          # package bytes of each level GLB (LOD0 embeds its images)
    hulls: dict[str, tuple[np.ndarray, np.ndarray]]
    bounds: np.ndarray
    fractions: list[float]
    lod_tris: list[int]
    # LOD0 with the package's PNG maps where it ships WebP ones (manifest ``web_textures``; adapters/engines/web_textures.py):
    # what the KTX2 encoding starts from, never a lossy copy. The package's LOD0 when it ships PNG.
    lossless_first: bytes = b""


def read_parts(pkg: Path, manifest: dict[str, Any]) -> Parts:
    def src(rel: str) -> Path:
        return inside(pkg, safe_name(rel, "manifest file"), "manifest file", write=False)

    asset = asset_name(manifest["asset"])
    roles = {t["path"]: t["role"] for t in manifest.get("textures") or []}
    textures = {f: src(f).read_bytes() for f in manifest.get("files") or [] if f.startswith(TEXTURE_DIR + "/")}
    from antigravity3d.deliver.godot import level_files

    lv = level_files(manifest)
    missing = [f for f in lv if not src(f).is_file()]
    if missing:
        raise ValueError(f"package is missing level file(s) {missing}")
    levels = {f: src(f).read_bytes() for f in lv}
    for f in lv:
        if not _gltf_mesh_node_paths(src(f)):
            raise ValueError(f"{f} has no mesh nodes")
    base = set(material_names(levels[lv[0]]))
    for f in lv[1:]:  # the loader gives a far level the highest level's materials by name
        stray = sorted(set(material_names(levels[f])) - base)
        if stray:
            raise ValueError(f"{f}: material(s) {stray} are not materials of {lv[0]}")
    hull_names = list(manifest.get("collider_glb_nodes") or [])
    hulls: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    if hull_names:
        colliders = src(f"{asset}_colliders.glb")
        if not colliders.is_file():
            raise ValueError(f"manifest names {len(hull_names)} collision hull(s) but {colliders.name} is missing")
        hulls = {re.sub(r"-(conv)?col(only)?$", "", n): v for n, v in glbtool.hull_meshes(colliders, hull_names).items()}
    fractions = list(manifest.get("lod_screen_fractions") or [1.0])
    if len(fractions) < len(lv):
        raise ValueError(f"manifest lod_screen_fractions has {len(fractions)} entries for {len(lv)} level files")
    return Parts(asset, textures, roles, lv, levels, hulls, np.asarray(manifest["glb_bounds_m"], np.float64), fractions,
                 list(manifest.get("lod_tris") or [manifest["tris"]]), lossless_level0(levels[lv[0]], textures, manifest))


def lossless_level0(first: bytes, textures: dict[str, bytes], manifest: dict[str, Any]) -> bytes:
    """``first`` (the package's LOD0) with every WebP map it embeds replaced by the package's PNG of the same map
    (manifest ``web_textures.maps``: ``webp`` -> ``texture``). Raises when a WebP map has no PNG in the package."""
    maps = [m for m in (manifest.get("web_textures") or {}).get("maps") or [] if m.get("webp")]
    if (manifest.get("web_textures") or {}).get("format") != "webp" or not maps:
        return first
    swap = {}
    for m in maps:
        if m["webp"] not in textures or m["texture"] not in textures:
            raise ValueError(f"web_textures names {m['webp']} and {m['texture']}, which the package does not both hold")
        swap[sha(textures[m["webp"]])] = (textures[m["texture"]], "image/png")
    out = glbtool.swap_images(first, embedded=swap)
    if any(mime == "image/webp" for _b, mime in glbtool.embedded_images(out)):
        raise ValueError("the package's LOD0 embeds a WebP map whose PNG the manifest does not name")
    return out


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def uncompressed_asset_files(parts: Parts) -> dict[str, bytes]:
    """The asset folder's files for an install without compression: LOD0 references ``textures/`` (the package's own
    LOD0 embeds them, so a page that also loads a far level would hold two copies), far levels carry no images."""
    uri_of = {}
    for rel, data in parts.textures.items():
        uri_of.setdefault(sha(data), rel)
    first, report = glbtool.reference_images_bytes(parts.levels[parts.level_files[0]], uri_of, parts.level_files[0])
    left = [r["image"] for r in report if r["storage"] == "embedded"]
    if left:
        raise ValueError(f"{parts.level_files[0]}: image(s) {left} have no file in the package's textures/ with the same bytes")
    # the maps LOD0 shows, and only those: with WebP maps the package's PNG copies (the USD's, the KTX2 encoder's source)
    # would be dead weight in the served folder
    shown = {r["uri"] for r in report if r["storage"] == "uri"}
    files = {rel: data for rel, data in parts.textures.items() if rel in shown}
    files[parts.level_files[0]] = first
    for f in parts.level_files[1:]:
        files[f] = glbtool.strip_images(parts.levels[f])
    return files


# --------------------------------------------------------------------------- compression


@dataclass
class Compressed:
    files: dict[str, bytes]           # asset-folder-relative: level GLBs and textures/*.ktx2
    meta: dict[str, Any]


def compression_key(parts: Parts, versions: dict[str, str], script: bytes) -> str:
    h = hashlib.sha256()
    for name in sorted(parts.levels):
        h.update(name.encode() + sha(parts.levels[name]).encode())
    for name in sorted(parts.textures):
        h.update(name.encode() + sha(parts.textures[name]).encode())
    h.update(json.dumps([TEXTURE_TIERS, MESHOPT_SETTINGS, versions], sort_keys=True).encode() + sha(script).encode())
    return h.hexdigest()


def compress(parts: Parts, versions: dict[str, str], node_modules: Path, tier: tuple[str, dict[str, dict[str, Any]]],
             cache_dir: Path, *, timeout: float = 900.0) -> Compressed:
    """Run web_compress.mjs on the package's levels with ``tier``'s settings. The project's node_modules is linked beside a
    copy of the script in a temporary folder, so Node resolves the packages from there and the project itself is not
    written. ``cache_dir`` holds each (image, role, settings) encoding once, so the next tier only encodes what differs."""
    script = (HERE / "web_compress.mjs").read_bytes()
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="agy3d-compress-") as td:
        work = Path(td)
        (work / "node_modules").symlink_to(node_modules, target_is_directory=True)
        (work / "web_compress.mjs").write_bytes(script)
        (work / "in").mkdir()
        (work / "out").mkdir()
        job_levels = []
        for f in parts.level_files:
            data = (parts.lossless_first or parts.levels[f]) if f == parts.level_files[0] else glbtool.strip_images(parts.levels[f])
            (work / "in" / f).write_bytes(data)
            job_levels.append({"in": str(work / "in" / f), "out": str(work / "out" / f)})
        (work / "job.json").write_text(json.dumps({"levels": job_levels, "textures": tier[1],
                                                   "meshopt": MESHOPT_SETTINGS, "report": str(work / "report.json"),
                                                   "cache": str(cache_dir)}))
        try:
            node = node_executable()
            if node is None:
                raise ChildError("node is not available")
            code, out_text, err_text = run_child([node, "web_compress.mjs", "job.json"], cwd=work, timeout=timeout)
        except ChildError as e:
            raise RuntimeError(f"compression failed: {e}") from e
        if code != 0 or not (work / "report.json").is_file():
            raise RuntimeError(f"compression failed (node exit {code}): {(err_text or out_text)[-600:]}")
        report = json.loads((work / "report.json").read_text())
        out = {f: (work / "out" / f).read_bytes() for f in parts.level_files}
    name_of = {sha(d): rel for rel, d in parts.textures.items()}
    uri_of: dict[str, str] = {}
    images = []
    for im in report["images"]:
        rel = name_of.get(im["source_sha256"])
        if rel is None:
            raise RuntimeError("compression returned an image that is not one of the package's textures")
        target = f"{TEXTURE_DIR}/{Path(rel).stem}.ktx2"
        if target in uri_of.values():
            raise RuntimeError(f"two compressed images would both be written to {target}")
        uri_of[im["ktx2_sha256"]] = target
        images.append({**im, "source": rel, "file": target})
    files: dict[str, bytes] = {}
    first, rep = glbtool.reference_images_bytes(out[parts.level_files[0]], uri_of, parts.level_files[0])
    if any(r["storage"] == "embedded" for r in rep):
        raise RuntimeError("compressed LOD0 holds an image that is not one of the KTX2 files")
    files[parts.level_files[0]] = first
    for f in parts.level_files[1:]:
        files[f] = out[f]
    emb = {}
    for im in images:
        emb[im["file"]] = _ktx2_bytes(out[parts.level_files[0]], im["ktx2_sha256"])
    files.update(emb)
    meta = {"tool": "glTF-Transform + ktx2-encoder + meshoptimizer (the project's own node_modules, run by web_compress.mjs)",
            "versions": versions, "tier": tier[0], "texture_settings": tier[1], "meshopt_settings": MESHOPT_SETTINGS,
            "images": images, "seconds": round(time.perf_counter() - t0, 2)}
    return Compressed(files, meta)


def _ktx2_bytes(glb_bytes: bytes, want_sha: str) -> bytes:
    for data, _mime in glbtool.embedded_images(glb_bytes):
        if sha(data) == want_sha:
            return data
    raise RuntimeError("compressed GLB does not hold the image its report names")


# --------------------------------------------------------------------------- levels and the loader


@dataclass
class WebLevel:
    index: int
    file: str
    triangles: int
    distance: float                  # the far level appears at this distance going out
    back: float                      # ... and goes at this one coming in (never shown closer than that)
    hysteresis: float                # three's addLevel() argument that gives ``back``
    switch_in_m: float | None        # as the Godot plan names it: where this level is first shown coming closer


def plan_levels(parts: Parts, fov_deg: float) -> tuple[list[WebLevel], list[float], float]:
    lo, hi = parts.bounds
    centre = (lo + hi) / 2
    radius = float(np.linalg.norm(hi - lo) / 2)
    dist = [0.0] + [transition_distance(radius, parts.fractions[i], fov_deg) for i in range(1, len(parts.level_files))]
    levels: list[WebLevel] = []
    for i, f in enumerate(parts.level_files):
        d = dist[i]
        m = d * HYSTERESIS
        appear = d + 2 * m  # Godot's arithmetic: begin = d + m with margin m, seen at d + 2m going out, gone at d coming in
        nxt = dist[i + 1] if i + 1 < len(dist) else None
        levels.append(WebLevel(i, f, int(parts.lod_tris[i]) if i < len(parts.lod_tris) else -1, appear if i else 0.0,
                               d if i else 0.0, (1 - d / appear) if i else 0.0, nxt))
    return levels, centre.tolist(), radius


#: A far level is fetched the first time the camera comes within this share of the distance where it appears (the
#: loader's REACH; why 0.8 is said there).
ON_DEMAND_REACH = 0.8


def _r(x: float) -> float:
    return round(float(x), 6)


def collision_json(parts: Parts) -> bytes:
    body = {name: {"points": [_r(v) for v in pts.reshape(-1)], "index": [int(i) for i in faces.reshape(-1)]}
            for name, (pts, faces) in parts.hulls.items()}
    return json.dumps(body, separators=(",", ":")).encode()


def _template(name: str) -> str:
    return (HERE / "web_templates" / name).read_text()


def loader_files(layout: Layout, parts: Parts, levels: list[WebLevel], centre: list[float], radius: float, fov_deg: float,
                 compressed: bool, react: bool) -> dict[str, bytes]:
    """index.js, index.d.ts and, for React Three Fiber, react.js and react.d.ts."""
    lo, hi = parts.bounds
    data = {
        "asset": parts.asset, "assetsUrl": layout.assets_url, "basisUrl": layout.basis_url, "fov": fov_deg,
        "compressed": compressed, "hulls": bool(parts.hulls),
        "bounds": {"min": [_r(v) for v in lo], "max": [_r(v) for v in hi], "size": [_r(v) for v in hi - lo],
                   "center": [_r(v) for v in centre], "radius": _r(radius)},
        "levels": [{"file": lv.file, "triangles": lv.triangles, "distance": _r(lv.distance), "hysteresis": _r(lv.hysteresis)}
                   for lv in levels],
    }
    load, comp = f"load{pascal(parts.asset)}", pascal(parts.asset)
    imports = ('import { KTX2Loader } from "three/examples/jsm/loaders/KTX2Loader.js";\n'
               'import { MeshoptDecoder } from "three/examples/jsm/libs/meshopt_decoder.module.js";\n') if compressed else ""
    setup = ("  const ktx2 = leaseKtx2(options);\n  loader.setKTX2Loader(ktx2.loader);\n"
             "  loader.setMeshoptDecoder(MeshoptDecoder);\n  farLoader.setMeshoptDecoder(MeshoptDecoder);"
             if compressed else "  const ktx2 = { release() {} };")

    def fill(text: str) -> str:
        for k, v in {"%%ASSET%%": parts.asset, "%%LOAD%%": load, "%%COMPONENT%%": comp, "%%ASSETS_URL%%": layout.assets_url,
                     "%%BASIS_URL%%": layout.basis_url, "%%REACH%%": f"{ON_DEMAND_REACH:g}"}.items():
            text = text.replace(k, v)
        return text

    js = _template("loader.js").replace("%%IMPORTS%%", imports).replace("%%KTX2%%", _template("ktx2.js") if compressed else "") \
        .replace("%%LOADER_SETUP%%", setup).replace("%%DATA%%", json.dumps(data, indent=1))
    dts = _template("loader.d.ts").replace("%%KTX2_TYPE_IMPORT%%", 'import type { KTX2Loader } from "three/examples/jsm/loaders/KTX2Loader.js";' if compressed else "") \
        .replace("%%KTX2_OPTION%%", "  /** The project's own KTX2Loader, instead of the one made for the duration of the load. */\n  ktx2Loader?: KTX2Loader;\n" if compressed else "")
    files = {"index.js": fill(js).encode(), "index.d.ts": fill(dts).encode()}
    if react:
        files["react.js"] = fill(_template("react.js")).encode()
        files["react.d.ts"] = fill(_template("react.d.ts")).encode()
    return files


def transcoder_bytes(three: Path | None) -> int:
    return sum((three / BASIS_IN_THREE / n).stat().st_size for n in BASIS_FILES) if three is not None else 0


def gpu_memory(manifest: dict[str, Any], settings: dict[str, dict[str, Any]] | None) -> dict[str, int]:
    """Texture memory in bytes with a full mip chain (4/3), computed from the format and not observed: RGBA8 is 32 bits
    per pixel; UASTC transcodes to BC7 (8 bits per pixel) and ETC1S to BC1 (4 bits per pixel) on a desktop GPU
    (a phone gets ASTC 8 bpp or ETC2). ``settings``: the tier's options per role, None for an uncompressed install."""
    seen: dict[str, tuple[int, int, str]] = {t["path"]: (t["width"], t["height"], t["role"]) for t in manifest.get("textures") or []}
    raw = packed = 0
    for w, h, role in seen.values():
        raw += int(w * h * 4 * 4 / 3)
        bits = 32 if settings is None else (8 if settings[role if role in settings else "albedo"].get("isUASTC") else 4)
        packed += int(w * h * bits / 8 * 4 / 3)
    return {"uncompressed": raw, "block_compressed": packed}


# --------------------------------------------------------------------------- the plan


@dataclass
class WebPlan:
    files: dict[str, bytes]               # project-relative name -> bytes
    shared: set[str]                      # names (in ``files``) that other installs of the project may also hold
    layout: Layout
    levels: list[WebLevel]
    hulls: dict[str, int]
    centre: list[float]
    radius_m: float
    fov_deg: float
    react: bool
    compression: dict[str, Any]           # what happened or would: {"state": ..., ...}
    notes: list[str] = field(default_factory=list)
    asset_bytes: int = 0                  # the asset folder's own files (without the shared transcoder)


def build_plan(pkg: Path, manifest: dict[str, Any], layout: Layout, *, fov_deg: float, project: Path, info: ProjectInfo,
               dry_run: bool, mode: str = "auto", reuse: dict[str, bytes] | None = None,
               reuse_meta: dict[str, Any] | None = None,
               probe: Callable[["WebPlan"], dict[str, Any]] | None = None, cannot_measure: str | None = None) -> WebPlan:
    """Every file the install writes, as bytes (nothing written into the project).

    Nothing compressed is written without a measurement that passed: ``probe(plan)`` renders a candidate in a browser
    and returns ``{"passed": True | False, ...numbers}`` (``"stop": True``: it fails for a reason a finer encoding
    would not change). ``cannot_measure`` is the reason there is no probe (no browser, no check requested): then the
    files are uncompressed and the record says "unmeasured". The first tier of TEXTURE_TIERS that passes is the plan;
    where none passes, or the install fails other checks, the files are uncompressed and the record says why.

    ``mode``: ``never`` installs the uncompressed files; ``always`` takes the first tier inside the tolerance whatever
    the size; ``auto`` does that only when the compressed files are no larger than the uncompressed ones, the transcoder
    counted only if the project does not have it yet (a page must not download more than before), and stops at the
    first tier that is larger without comparing its frames.
    ``reuse``: compressed files an earlier install of the same inputs, tool versions and settings wrote (asset-folder
    names, measured then); then nothing is encoded again."""
    parts = read_parts(pkg, manifest)
    levels, centre, radius = plan_levels(parts, fov_deg)
    react = "@react-three/fiber" in dependencies(project)
    versions, missing, node_modules = compression_tools(project)
    three = find_package(project, "three")
    notes: list[str] = ["compression uses the packages in the project's node_modules only; a native gltfpack or KTX-Software "
                        "`ktx` binary is not used"]
    plain_files = uncompressed_asset_files(parts)
    collision = collision_json(parts) if parts.hulls else b""
    uncompressed_bytes = sum(len(b) for b in plain_files.values()) + len(collision)

    def assemble(asset_files: dict[str, bytes], comp: dict[str, Any]) -> WebPlan:
        files = {f"{layout.assets}/{n}": b for n, b in asset_files.items()}
        if parts.hulls:
            files[f"{layout.assets}/collision.json"] = collision
        asset_bytes = sum(len(b) for b in files.values())
        shared: set[str] = set()
        compressed = comp["state"] in ("compressed", "kept")
        if compressed:
            if three is None:
                raise RuntimeError("compression planned without three.js installed")
            for n in BASIS_FILES:
                rel = f"{layout.basis}/{n}"
                files[rel] = (three / BASIS_IN_THREE / n).read_bytes()
                shared.add(rel)
            comp = {**comp, "bytes_before": uncompressed_bytes}
        for n, b in loader_files(layout, parts, levels, centre, radius, fov_deg, compressed, react).items():
            files[f"{layout.code}/{n}"] = b
        return WebPlan(files, shared, layout, levels, {n: len(v[0]) for n, v in parts.hulls.items()}, centre, radius, fov_deg,
                       react, comp, list(notes), asset_bytes)

    if mode not in COMPRESS_MODES:
        raise ValueError(f"compress must be one of {COMPRESS_MODES}, not {mode!r}")
    have_transcoder = (project / layout.basis / BASIS_FILES[0]).is_file()
    if mode == "never":
        plan = assemble(plain_files, {"state": "off", "reason": "compression not requested (--compress never)"})
    elif versions is None:
        plan = assemble(plain_files, {"state": "unavailable", "missing": missing, "enable": ENABLE_COMPRESSION})
    else:
        key = compression_key(parts, versions, (HERE / "web_compress.mjs").read_bytes()) + mode
        # a decision not to compress depends on whether the shared transcoder is there: the key says so, so that an asset
        # declined before another asset brought the transcoder is decided again
        decline_key = key + ("+transcoder" if have_transcoder else "-transcoder")
        if dry_run and cannot_measure is None:
            plan = assemble(plain_files, {"state": "would_run", "versions": versions, "mode": mode})
        elif not dry_run and reuse is not None and reuse_meta and reuse_meta.get("state") in ("compressed", "kept") \
                and reuse_meta.get("key") == key and reuse_meta.get("tolerance_measured") is True and reuse_meta.get("tiers_tried"):
            plan = assemble(reuse, {**reuse_meta, "state": "kept"})  # only files whose record says the tolerance was measured
        elif cannot_measure is not None:
            plan = assemble(plain_files, {"state": "unmeasured", "reason": cannot_measure, "mode": mode})
        elif reuse_meta and reuse_meta.get("state") in ("declined", "rejected") and reuse_meta.get("key") == decline_key:
            plan = assemble(plain_files, dict(reuse_meta))  # decided before for these inputs: not encoded again
        else:
            extra = 0 if mode != "auto" or have_transcoder else transcoder_bytes(three)
            plan = _choose_tier(parts, versions, node_modules, key, assemble, plain_files, probe,  # type: ignore[arg-type]
                                keep_bytes=uncompressed_bytes - extra if mode == "auto" else None)
            plan.compression["mode"] = mode
            if mode == "auto" and plan.compression["state"] == "compressed":
                if plan.asset_bytes + extra > uncompressed_bytes:
                    tier = next(t for t in TEXTURE_TIERS if t[0] == plan.compression["tier"])
                    tried = plan.compression.get("tiers_tried") or []
                    which = ("the next tier after those outside the frame tolerance" if len(tried) > 1
                             else "the smallest tier there is")
                    plan = assemble(plain_files, {
                        "state": "declined", "mode": mode, "tier": tier[0], "tiers_tried": tried,
                        "compressed_bytes": plan.asset_bytes, "uncompressed_bytes": uncompressed_bytes,
                        "transcoder_bytes_counted": extra, "gpu_memory_computed": gpu_memory(manifest, tier[1]),
                        "reason": f"at {tier[0]} ({which}) the asset's files are {plan.asset_bytes:,} bytes against "
                                  f"{uncompressed_bytes:,} uncompressed"
                                  + (f", plus {extra:,} for the KTX2 transcoder this project does not have yet" if extra else "")
                                  + "; the tiers after it are larger, so its frames were not compared and nothing more was encoded"})
            if plan.compression["state"] in ("declined", "rejected"):
                plan.compression["key"] = decline_key
    if plan.compression["state"] in ("compressed", "kept") and "gpu_memory_computed" not in plan.compression:
        tier = next((t for t in TEXTURE_TIERS if t[0] == plan.compression.get("tier")), None)
        if tier:
            plan.compression["gpu_memory_computed"] = gpu_memory(manifest, tier[1])
    if abs(fov_deg - DEFAULT_FOV_DEG) > 1e-9:
        plan.notes.append(f"switch distances are recorded for a {fov_deg:g} degree vertical field of view (the package's levels "
                          f"were generated for {DEFAULT_FOV_DEG:g}); the loader follows the live camera's field of view from there")
    if not parts.hulls:
        plan.notes.append("the package has no collision hulls (manifest collider_glb_nodes is empty): userData.agy3d.hulls is empty")
    if not (project / layout.served).is_dir():
        plan.notes.append(f"{layout.served}/ did not exist and is created: a dev server that was already running may need a "
                          "restart to serve it")
    return plan


def _choose_tier(parts: Parts, versions: dict[str, str], node_modules: Path, key: str, assemble: Callable[..., "WebPlan"],
                 plain_files: dict[str, bytes], probe: Callable[["WebPlan"], dict[str, Any]] | None,
                 keep_bytes: int | None = None) -> "WebPlan":
    """The first tier whose frame difference was measured inside the tolerance; otherwise the uncompressed files.

    ``keep_bytes`` (``auto``): the most the asset's compressed files may weigh to be kept. A tier over it is returned
    at once and unmeasured (``tolerance_measured`` False): it cannot be kept whatever its frames look like, and the
    tiers after it are larger (TEXTURE_TIERS), so encoding and comparing them is work whose result is already known."""
    if probe is None:
        raise RuntimeError("no measurement available: compression is not written without one")
    tried: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="agy3d-ktx2-cache-") as cache:
        for i, tier in enumerate(TEXTURE_TIERS):
            try:
                use = compress(parts, versions, node_modules, tier, Path(cache))
            except RuntimeError as e:
                # The project's encoder refusing a map is no reason to install nothing: ktx2-encoder 0.6.0 (the version in
                # the fixture and in the round-eight site seed) throws "Total source texels 16777216 exceed the encoder
                # limit of 12582912" for one 4096 x 4096 map, which run 1's first containment_gate had; the install then
                # failed outright. The uncompressed files are installed and the record says why.
                said = re.search(r"\bError: ([^\n]+)", str(e))
                return assemble(plain_files, {"state": "rejected", "tiers_tried": tried,
                                              "reason": f"the project's compression tools failed at {tier[0]} "
                                                        f"({(said.group(1) if said else str(e))[:300].strip()})"})
            plan = assemble(use.files, {**use.meta, "state": "compressed", "key": key})
            if keep_bytes is not None and plan.asset_bytes > keep_bytes:
                tried.append({"tier": tier[0], "asset_bytes": plan.asset_bytes, "not_compared": True})
                plan.compression.update({"tolerance_measured": False, "tiers_tried": tried})
                return plan
            r = probe(plan)
            tried.append({"tier": tier[0], "asset_bytes": plan.asset_bytes, **{k: v for k, v in r.items() if k != "stop"}})
            if r["passed"] is True:
                plan.compression.update({"tolerance_measured": True, "tiers_tried": tried})
                return plan
            if r.get("stop") or r["passed"] is None:
                others = r.get("other_failures") or []
                why = (f"the install fails check(s) {', '.join(others)}, which a finer encoding would not change, and the "
                       f"frame tolerance was not met at {tier[0]}" if others else f"the frame difference could not be measured ({r.get('why', 'no reason given')})")
                out = assemble(plain_files, {"state": "rejected" if others else "unmeasured", "tiers_tried": tried, "reason": why})
                return out
        out = assemble(plain_files, {"state": "rejected", "tiers_tried": tried,
                                     "reason": f"even {TEXTURE_TIERS[-1][0]} was outside the frame tolerance"})
        return out


__all__ = ["Layout", "WebPlan", "WebRefused", "build_plan", "choose_layout", "compression_tools", "three_info", "pascal",
           "SUPPORTED_REVISIONS", "ENABLE_COMPRESSION", "UnsafePath"]
