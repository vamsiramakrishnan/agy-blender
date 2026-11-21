"""Reviewed EL4CTEO/blender-skills primitives for the Blender worker.

Five upstream scripts (and their shared helper ``_bpyx``) are vendored byte for byte, in upstream's layout, under
``vendor/blender-skills/`` next to this file, with upstream's MIT ``LICENSE``; ``vendor/blender-skills.provenance.json``
records the repository, commit, release, and each file's upstream path, size and SHA-256. Every file is checked
against the digest pinned below before any of them is executed: that check is what "reviewed" means here, and a
test fails when the pins and the provenance record disagree.

``AGY3D_BLENDER_SKILLS`` may point at a checkout of the pinned commit instead (same layout, same digests). Without
it the vendored copy is used, so the primitives work wherever Blender does.

This module runs inside Blender (the daemon loads it by path), so it uses the standard library only. Nothing here
imports or executes the scripts outside :func:`load_reviewed`, and they never run outside Blender.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

UPSTREAM_REPOSITORY = "https://github.com/EL4CTEO/blender-skills"
UPSTREAM_COMMIT = "9e7e04a603c125a0d17447ab97112fa70a865067"
UPSTREAM_RELEASE = "1.3.2"
VENDOR_DIR = Path(__file__).resolve().parent / "vendor" / "blender-skills"
PROVENANCE = VENDOR_DIR.parent / "blender-skills.provenance.json"
OVERRIDE_ENV = "AGY3D_BLENDER_SKILLS"

# relative path (upstream layout) and SHA-256 at UPSTREAM_COMMIT
HELPER = ("skills/blender-game-optimization/scripts/_bpyx.py", "90f44d70198b199cd922e4e57f7e55c56a3041d3a498ef7d444b019f9795d2ff")
PINNED_SCRIPTS = {
    "mesh_stats": ("skills/blender-game-optimization/scripts/mesh_stats.py", "6f48790b87b90cd15d4c20e6884dcfedf9aa65292f4afcc7fad9fcf7eb8216bc"),
    "qa_check": ("skills/blender-asset-qa/scripts/qa_check.py", "37d01051f98fdafd3a9f13f7525804174c68a7bacb446d5bd5b2f43fc2785cca"),
    "kit_check": ("skills/blender-environment-kits/scripts/kit_check.py", "5130ad74e2c19dd1839e354a06abd204bafeead277cc447107db017f359e20e5"),
    "texel_density": ("skills/blender-retopology-uv/scripts/texel_density.py", "9fd15366aa20aa3005163745b12aae216c0b23a5fdafa0bbec98c1786f0c3cd6"),
}
# The one script that WRITES a file. It is pinned and loaded with the others but kept out of PRIMITIVE_KWARGS, so
# ``inspect_usd`` (a measurement) can never run it; only the ``export_fbx`` operation does, through
# :func:`check_export_request`.
PINNED_WRITERS = {
    "export_asset": ("skills/blender-export-pipelines/scripts/export_asset.py", "58534892042e5c954f0875b3b41b45c2609b7a4ec5dc44f6c06922b06cbd6c88"),
}
LICENSE = ("LICENSE", "d81d0037e3a29a01ccfefaae420d5ba73e905cabf3995700e9075c7205c06cc5")

# The keyword arguments each primitive accepts from a caller of the worker. The list must be exact: every upstream
# ``run()`` ends in ``**_``, so a misspelt keyword would be dropped silently and the script would run with its
# default ("ok": true). Checking against this list turns that into an error. The pipeline uses these scripts as
# measurements, so nothing that edits the scene can be passed: kit_check's ``fix`` (applies transforms, moves
# origins) and texel_density's ``set`` / ``margin_px`` (rescale and repack UVs) are refused. ``objects`` is refused
# from callers because the worker fills it in with the mesh objects the import produced (upstream's default is the
# current selection). qa_check's ``no_modifiers`` is left out: an imported USD carries no modifiers.
PRIMITIVE_KWARGS: dict[str, frozenset[str]] = {
    "mesh_stats": frozenset({"budget"}),
    "qa_check": frozenset({"profile", "eps", "tol", "max_tris", "uv"}),
    "kit_check": frozenset({"grid", "pivot", "texture", "tol", "prefix"}),
    "texel_density": frozenset({"res"}),
}


# ---- the contract of the one writer: "writes exactly one file, where the worker says"
#
# Presets: the three static FBX presets of export_asset.py. Left out on purpose: unity_skeletal and unreal_skeletal
# (no skeleton in a package), roblox_gltf / godot / web / gltf / usd (the project writes glTF and USD itself and
# validates them elsewhere). The caller names no path of its own for the output and no exporter keyword: the worker
# passes upstream's ``preset``, ``objects`` (what it built), ``out`` (``<out_dir>/<asset_name>.fbx`` through a temporary
# name) and ``no_anim=True``; ``set``, ``batch``, ``collection``, ``draco`` and ``out`` are never reachable.
EXPORT_PRESETS = ("unity", "unreal", "roblox")
EXCLUDED_PRESETS = ("unity_skeletal", "unreal_skeletal", "roblox_gltf", "godot", "web", "gltf", "usd")
EXPORT_REQUEST_KEYS = frozenset({"source", "lods", "colliders", "textures_dir", "texture_map", "out_dir", "asset_name", "preset"})
VERIFY_REQUEST_KEYS = frozenset({"source", "lods", "colliders", "textures_dir", "texture_map", "fbx", "asset_name", "preset"})
ASSET_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,63}")
MAX_LOD_FILES = 7  # LOD0 plus seven: Unity's LOD Group takes at most eight levels


def _file(request: dict[str, Any], key: str) -> str:
    value = request.get(key)
    if not isinstance(value, str) or not value or "\0" in value:
        raise TypeError(f"export: {key} must be a non-empty path string, got {value!r}")
    if not os.path.isfile(value):
        raise FileNotFoundError(f"export: {key} is not a file: {value}")
    return value


def check_export_request(request: Any, *, verify: bool = False) -> dict[str, Any]:
    """The validated request of ``export_fbx`` (or, with ``verify``, of ``verify_fbx``); raises for anything outside the contract.

    Everything is checked before Blender touches the disk: only the keys of the contract, one of the three presets, an
    asset name that is a plain identifier, source files that exist and are glTF, an output directory that exists and an
    output path that, after resolving symbolic links, is a file of that very directory (it is also refused when it is a
    symbolic link). Returns a new dict with ``final`` (the output path) for a write.
    """
    if not isinstance(request, dict):
        raise TypeError(f"export: the request must be a mapping, got {type(request).__name__}")
    allowed = VERIFY_REQUEST_KEYS if verify else EXPORT_REQUEST_KEYS
    refused = sorted(set(request) - allowed)
    if refused:
        raise ValueError(f"export does not accept {refused} through the worker (it writes one file, where the worker says); allowed: {sorted(allowed)}")
    preset = request.get("preset")
    if preset not in EXPORT_PRESETS:
        why = "not offered: the package's own glTF and USD writers cover it, or it needs a skeleton" if preset in EXCLUDED_PRESETS else "unknown"
        raise ValueError(f"export preset {preset!r} is {why}; allowed: {list(EXPORT_PRESETS)}")
    name = request.get("asset_name")
    if not isinstance(name, str) or not ASSET_NAME.fullmatch(name):
        raise ValueError(f"export: asset_name must match {ASSET_NAME.pattern}, got {name!r}")
    out: dict[str, Any] = {"preset": preset, "asset_name": name, "source": _file(request, "source")}
    lods = request.get("lods") or []
    if not isinstance(lods, (list, tuple)) or len(lods) > MAX_LOD_FILES:
        raise ValueError(f"export: lods must be a list of at most {MAX_LOD_FILES} files")
    out["lods"] = [_file({"lod": lod}, "lod") for lod in lods]
    out["colliders"] = _file(request, "colliders") if request.get("colliders") is not None else None
    for key in ("source", "colliders"):
        if out[key] is not None and Path(out[key]).suffix.lower() not in (".glb", ".gltf"):
            raise ValueError(f"export: {key} must be a .glb or .gltf file, got {out[key]!r}")
    for lod in out["lods"]:
        if Path(lod).suffix.lower() not in (".glb", ".gltf"):
            raise ValueError(f"export: lods must be .glb or .gltf files, got {lod!r}")
    textures = request.get("textures_dir")
    if textures is not None and (not isinstance(textures, str) or not os.path.isdir(textures)):
        raise ValueError(f"export: textures_dir must be an existing directory, got {textures!r}")
    out["textures_dir"] = textures
    # texture_map: {file name in textures_dir the GLB's image is a copy of: file name there the FBX must reference instead}
    # (Unreal reads the package's DirectX normal map, the GLB embeds the +Y one). Plain image file names, both present.
    tmap = request.get("texture_map") or {}
    if not isinstance(tmap, dict) or (tmap and textures is None):
        raise ValueError("export: texture_map must be a mapping of file names in textures_dir (and needs textures_dir)")
    if tmap and preset == "roblox":
        raise ValueError("export: texture_map applies to relative references (unity, unreal); roblox embeds the GLB's bytes")
    for was, now in tmap.items():
        for image in (was, now):
            if (not isinstance(image, str) or not image or os.path.basename(image) != image or image.startswith(".")
                    or Path(image).suffix.lower() not in (".png", ".jpg", ".jpeg", ".tga", ".bmp", ".webp")
                    or not os.path.isfile(os.path.join(textures, image))):
                raise ValueError(f"export: texture_map names must be image files in textures_dir, got {image!r}")
    out["texture_map"] = dict(tmap)
    if verify:
        fbx = request.get("fbx")
        if not isinstance(fbx, str) or not os.path.isfile(fbx) or Path(fbx).suffix.lower() != ".fbx":
            raise ValueError(f"verify: fbx must be an existing .fbx file, got {fbx!r}")
        out["fbx"] = fbx
        return out
    directory = request.get("out_dir")
    if not isinstance(directory, str) or not directory or "\0" in directory or not os.path.isdir(directory):
        raise ValueError(f"export: out_dir must be an existing directory, got {directory!r}")
    real_dir = os.path.realpath(directory)
    final = os.path.join(real_dir, f"{name}.fbx")
    if os.path.realpath(os.path.dirname(final)) != real_dir or os.path.islink(final) or os.path.isdir(final):
        raise ValueError(f"export: {final} is not a plain file path inside {real_dir}")
    out["out_dir"], out["final"] = real_dir, final
    return out


def source_root() -> tuple[Path, str]:
    """(directory to load from, "checkout" | "vendored"): the override when it is set and non-empty."""
    override = os.environ.get(OVERRIDE_ENV)
    return (Path(override), "checkout") if override else (VENDOR_DIR, "vendored")


def check_kwargs(primitive: str, kwargs: Any) -> dict[str, Any]:
    """The kwargs a worker may pass to ``primitive``; raises for anything outside its contract."""
    allowed = PRIMITIVE_KWARGS.get(primitive)
    if allowed is None:
        raise KeyError(f"no reviewed primitive named {primitive!r}; known: {sorted(PRIMITIVE_KWARGS)}")
    if kwargs is None:
        return {}
    if not isinstance(kwargs, dict):
        raise TypeError(f"{primitive}: kwargs must be a mapping, got {type(kwargs).__name__}")
    refused = sorted(set(kwargs) - allowed)
    if refused:
        raise ValueError(f"{primitive} does not accept {refused} through the worker (it is run as a measurement); "
                         f"allowed: {sorted(allowed)}")
    return dict(kwargs)


def _verified(path: Path, digest: str) -> bytes:
    source = path.read_bytes()
    if hashlib.sha256(source).hexdigest() != digest:
        raise ValueError(f"unexpected blender-skills source digest for {path.name} ({path})")
    return source


def _module(path: Path, source: bytes) -> ModuleType:
    module = ModuleType(path.stem)
    module.__file__ = str(path)
    exec(compile(source, str(path), "exec"), module.__dict__)
    return module


def load_reviewed(root: Path | None = None) -> dict[str, Callable[..., dict[str, Any]]]:
    """``{name: run}`` for every pinned script, measurements and writers, under ``root`` (default: the vendored copy).

    Every file is read and checked before any is executed, so one unexpected file stops the load before any
    upstream code runs. The scripts prepend their own directory to ``sys.path`` and import ``_bpyx``; both are
    undone afterwards, so the worker's imports stay isolated (each script keeps its own reference to the helper).
    """
    root = VENDOR_DIR if root is None else Path(root)
    helper = (root / HELPER[0], _verified(root / HELPER[0], HELPER[1]))
    scripts = {name: (root / rel, _verified(root / rel, digest)) for name, (rel, digest) in {**PINNED_SCRIPTS, **PINNED_WRITERS}.items()}
    original_path = sys.path.copy()
    previous_helper = sys.modules.get("_bpyx")
    try:
        sys.modules["_bpyx"] = _module(*helper)
        return {name: _module(path, source).run for name, (path, source) in scripts.items()}
    finally:
        sys.path[:] = original_path
        if previous_helper is None:
            sys.modules.pop("_bpyx", None)
        else:
            sys.modules["_bpyx"] = previous_helper
