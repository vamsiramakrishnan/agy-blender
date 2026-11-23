"""What an install may write into the developer's game project about the machine it ran on: nothing.

The project is committed and pushed; a file in it that names the package's folder on this machine puts the home
directory (and so the user's name) into the game's repository. These are the three pieces that keep that out:

* :func:`package_ref`: how the install record names the package (a path relative to the project when that tells
  nothing about the machine, else only the folder's name; always with the manifest's hash);
* :func:`locate_package`: how a later check finds it again from that record. The record is a file a person can
  edit, so what it names is only looked at (a directory, a manifest to hash), never written or deleted;
* :func:`scrub`: the report a check writes into the project, with every absolute path of this machine replaced.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


def _home() -> Path | None:
    """The user's home directory, resolved; None when it cannot be told or is the filesystem root (then every path would
    count as "under home" and the test would say nothing)."""
    try:
        home = Path.home().resolve()
    except (RuntimeError, OSError):
        return None
    return None if home == Path(home.anchor) else home


def _under(p: Path, root: Path) -> bool:
    return p == root or root in p.parents


def _relative_is_safe(pkg: Path, pkg_r: Path, proj_r: Path) -> bool:
    """A relative path shows how many folders it climbs and the package's path below the common ancestor of the two. That
    is nothing about the machine when the package is inside the project; otherwise when the ancestor is not a filesystem
    or drive root, the package was reached without a link (a link's own name would be shown, and its target is not what the
    path says), and the ancestor is not above a home directory (``../../alice/pkgs/crate`` names the user; the same
    holds when the package is in another user's home). A home that cannot be told means the second case cannot be judged."""
    if _under(pkg_r, proj_r):
        return True
    try:
        common = Path(os.path.commonpath([pkg_r, proj_r]))
    except ValueError:  # another drive
        return False
    if common == Path(common.anchor):
        return False
    home = _home()
    if home is None or common in home.parents:
        return False
    chain = [Path(os.path.abspath(pkg)), *Path(os.path.abspath(pkg)).parents]
    return not any(a.is_symlink() and _under(a.resolve(), common) and a.resolve() != common for a in chain)


def package_ref(pkg: Path, project: Path, manifest_sha256: str) -> dict[str, Any]:
    """The part of the install record that names the package: ``path`` is POSIX and relative to the project when that
    reveals nothing about the machine (:func:`_relative_is_safe`); otherwise ``path`` is None and ``folder`` (the folder's
    name) and the manifest's hash are all there is. What the user typed is not used: it is relative to a folder the next
    check may not run from, and may be absolute."""
    pkg_r, proj_r = pkg.resolve(), project.resolve()
    rel = os.path.relpath(pkg_r, proj_r) if _relative_is_safe(pkg, pkg_r, proj_r) else None
    return {"path": PurePosixPath(*Path(rel).parts).as_posix() if rel else None,
            "path_base": "project" if rel else None, "folder": pkg_r.name, "manifest_sha256": manifest_sha256}


def _sha_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def locate_package(project: Path, record: dict[str, Any]) -> tuple[Path | None, str | None]:
    """The package folder an install record names, when it is there and is the package that was installed: a directory
    whose manifest.json has the hash the record holds. Returns ``(folder, None)`` or ``(None, note)``, the note saying what
    to do. A record from before relative paths holds an absolute ``path``; it is used as it is. Nothing is written."""
    ref = record.get("package")
    ref = ref if isinstance(ref, dict) else {}
    raw, want = ref.get("path"), ref.get("manifest_sha256")
    folder = ref.get("folder") or (PurePosixPath(raw).name if isinstance(raw, str) and raw else None)
    named = f" (folder {folder!r})" if isinstance(folder, str) and folder else ""
    gone = f"the package was not found from this record{named}: pass it again"
    if not isinstance(raw, str) or not raw:
        return None, gone
    absolute = PurePosixPath(raw).is_absolute() or PureWindowsPath(raw).is_absolute()
    try:
        cand = Path(raw) if absolute else (project / raw).resolve()
        manifest = cand / "manifest.json"
        if not cand.is_dir() or not manifest.is_file():
            return None, gone
        if isinstance(want, str) and _sha_file(manifest) != want:
            return None, f"{gone}; the folder at that place holds another version of it (its manifest differs from the installed one)"
    except (OSError, ValueError):
        return None, gone
    return cand, None


def scrub(obj: Any, roots: dict[str, str]) -> Any:
    """``obj`` (JSON-able) with every occurrence of a path in ``roots`` replaced by its stand-in, longest first; the home
    directory and the temp root are always among them. A report in the project then names places by role
    (``<project>``, ``<package>``), not by where this machine keeps them."""
    import tempfile

    table = dict(roots)
    home = _home()
    if home is not None:
        table.setdefault(str(home), "<home>")
    try:
        table.setdefault(str(Path(tempfile.gettempdir()).resolve()), "<tmp>")
    except OSError:
        pass
    ordered = sorted((k for k in table if k and k != os.sep), key=len, reverse=True)

    def go(x: Any) -> Any:
        if isinstance(x, str):
            for k in ordered:
                x = x.replace(k, table[k])
            return x
        if isinstance(x, dict):
            return {go(k) if isinstance(k, str) else k: go(v) for k, v in x.items()}
        if isinstance(x, (list, tuple)):
            return [go(v) for v in x]
        return x

    return go(obj)


def rel_to(project: Path, p: str | Path | None) -> str | None:
    """``p`` relative to the project (POSIX) when it is inside it, else None."""
    if not p:
        return None
    try:
        return Path(p).resolve().relative_to(project.resolve()).as_posix()
    except ValueError:
        return None


def write_report(project: Path, out: Path, result: Any, record: dict[str, Any], measured: dict[str, Any]) -> None:
    """``out/report.json``: the result and what the check measured, as the project may keep it. The package is the
    record's name for it, the screenshot and the report are named from the project, and any other absolute path of this
    machine (the project's, the package as typed, the temp and home folders) is replaced by its role."""
    import json

    d = {**result.to_dict(), "measured": measured, "package": record.get("package")}
    d["screenshot"] = rel_to(project, result.screenshot)
    d["report_path"] = rel_to(project, out / "report.json")
    roots = {str(project): "<project>", str(project.resolve()): "<project>"}
    typed = (result.package or {}).get("path")
    if isinstance(typed, str) and typed:
        roots[str(Path(typed).resolve())] = "<package>"
        if os.path.isabs(typed):
            roots[typed] = "<package>"
    (out / "report.json").write_text(json.dumps(scrub(d, roots), indent=1, default=str))
