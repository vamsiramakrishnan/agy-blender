"""Reviewed EL4CTEO/blender-skills primitives for the Blender worker.

Four upstream scripts (and their shared helper ``_bpyx``) are vendored byte for byte, in upstream's layout, under
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
    """``{name: run}`` for every pinned script under ``root`` (default: the vendored copy).

    Every file is read and checked before any is executed, so one unexpected file stops the load before any
    upstream code runs. The scripts prepend their own directory to ``sys.path`` and import ``_bpyx``; both are
    undone afterwards, so the worker's imports stay isolated (each script keeps its own reference to the helper).
    """
    root = VENDOR_DIR if root is None else Path(root)
    helper = (root / HELPER[0], _verified(root / HELPER[0], HELPER[1]))
    scripts = {name: (root / rel, _verified(root / rel, digest)) for name, (rel, digest) in PINNED_SCRIPTS.items()}
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
