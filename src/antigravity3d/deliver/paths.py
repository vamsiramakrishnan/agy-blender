"""The one place that decides whether a file name taken from data may be read or written.

A package is a folder a developer may have received from someone else; its manifest, its GLBs' material names and the
install record in a project are data. Every name from them passes :func:`safe_name` before it is joined to a
directory, and every read, write and removal goes through :func:`inside`, which checks the path on disk at that
moment: no symbolic link between the root and the file, and the result still under the root.

Before this module (commit 8aaa359) a manifest entry ``textures/../../../injected.gd`` made an update write into the
project root, an asset name ``../../Evil`` wrote ``Evil.tscn`` there, a tampered install record or a glTF material
name made uninstall delete files outside the destination (an absolute name deleted a system file), and a symlinked
``textures/`` or ``materials/`` folder redirected writes and removals (tests/test_deliver_contain.py).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable


class UnsafePath(ValueError):
    """A name or a path that would leave its root; the message is one sentence naming the entry."""


def _quote(rel: object) -> str:
    """The entry as written, so a user can find it in the file; repr only where it holds control characters."""
    if isinstance(rel, str) and not any(ord(c) < 32 or c == "\x7f" for c in rel):
        return f"'{rel}'"
    return repr(rel)


_ASSET = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")  # the package writer's own rule for asset_name (a USD identifier)


def safe_name(rel: object, what: str, folders: Iterable[str] | None = None) -> str:
    """``rel`` when it is a plain relative name: forward slashes only, no absolute path, drive, backslash, control
    character, empty, ``.`` or ``..`` segment, and no hidden segment (``.agy3d`` is the tool's own record folder).
    ``folders``: the sub-folders it may be in, ``""`` for the top level; one level deep. None: any depth (reads
    inside a package). Raises :class:`UnsafePath` naming ``what`` and the entry."""
    def no(why: str) -> UnsafePath:
        return UnsafePath(f"{what} {_quote(rel)} is not accepted: {why}")

    if not isinstance(rel, str) or not rel:
        raise no("not a non-empty name")
    if "\\" in rel or ":" in rel or any(ord(c) < 32 or c == "\x7f" for c in rel):
        raise no("it holds a backslash, a drive or a control character")
    if rel.startswith("/"):
        raise no("it is an absolute path")
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise no("it has an empty, '.' or '..' segment")
    if any(p.startswith(".") for p in parts):
        raise no("it has a hidden segment")
    if folders is not None:
        allowed = set(folders)
        folder = parts[0] if len(parts) == 2 else ("" if len(parts) == 1 else None)
        if folder is None or folder not in allowed:
            raise no(f"it is not in {sorted(f or '(top level)' for f in allowed)}")
    return rel


def asset_name(name: object) -> str:
    if not isinstance(name, str) or not _ASSET.fullmatch(name):
        raise UnsafePath(f"manifest asset {_quote(name)} is not accepted: an asset name is an identifier (letters, digits, _)")
    return name


def inside(root: Path, rel: str, what: str, *, write: bool) -> Path:
    """``root / rel`` checked on disk now: no component from ``root`` down to it is a symbolic link (a write through
    one would land wherever it points), and the resolved path is under the resolved root. ``write``: the file may not
    exist yet (its existing parents are checked). The name itself must already have passed :func:`safe_name`."""
    base = root.resolve()
    path = root / rel
    cur = root
    for part in rel.split("/"):
        cur = cur / part
        if cur.is_symlink():
            raise UnsafePath(f"{what} {_quote(rel)} is not accepted: {cur.relative_to(root).as_posix()} is a symbolic link")
    probe = path
    while not probe.exists() and probe != root:
        if not write:
            break
        probe = probe.parent
    if probe.exists() and not probe.resolve().is_relative_to(base):
        raise UnsafePath(f"{what} {_quote(rel)} is not accepted: it resolves outside {root}")
    return path


def no_links(root: Path, what: str) -> None:
    """Refuse when anything under ``root`` (an installed destination) is a symbolic link, or ``root`` is one."""
    if root.is_symlink():
        raise UnsafePath(f"{what} {root} is a symbolic link")
    if not root.is_dir():
        return
    for d, dirs, files in os.walk(root, followlinks=False):
        for n in [*dirs, *files]:
            p = Path(d) / n
            if p.is_symlink():
                raise UnsafePath(f"{what} holds a symbolic link, {p.relative_to(root).as_posix()}: an install writes "
                                 "and removes only real files and folders inside its destination")
