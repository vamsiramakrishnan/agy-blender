"""The hand-off folder a build script's ``Asset.save()`` writes, read back (CONTRACT.md section 2).

``<out>/design.json`` (version 1: asset, wire materials, wire nodes, notes, stats) and ``<out>/parts/*.ply``, one per
free-form part. :func:`read` checks what the session relies on and makes each ``mesh_ref`` path absolute, so the scene
can be authored from anywhere; the runner copies the folder into the session first and reads the copy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from antigravity3d.build.shapes import BuildError

OUT_ENV = "AGY3D_BUILD_OUT"
VERSION = 1
_ASSET_KEYS = {"name": str, "engine": str, "max_tris": int, "brief": str, "art_direction": str}


@dataclass(frozen=True)
class Build:
    folder: Path  # where it was read from
    name: str
    engine: str
    max_tris: int
    brief: str
    art_direction: str
    nodes: list[dict]  # wire form; mesh_ref paths made absolute (folder / path)
    materials: list[dict]
    notes: list[str]
    stats: dict

    def scene(self):
        """The design as ``ir.SceneIR`` (``ir_schema.parse_tool_scene``: the same parse a tool's full scene gets)."""
        from antigravity3d import ir_schema

        return ir_schema.parse_tool_scene(self.name, self.nodes, self.materials)

    def part_files(self) -> dict[str, Path]:
        """Node id -> its PLY, for every free-form part."""
        return {n["id"]: Path(n["params"]["path"]) for n in self.nodes if n.get("kind") == "mesh_ref"}


def read(folder: Path) -> Build:
    """The build in ``folder``; raises BuildError naming what is missing or malformed."""
    folder = Path(folder).resolve()
    path = folder / "design.json"
    if not path.is_file():
        raise BuildError(f"{folder}: no design.json (the script did not reach a.save())")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise BuildError(f"{path}: not readable JSON ({e})") from None
    if not isinstance(doc, dict):
        raise BuildError(f"{path}: expected an object")
    if doc.get("version") != VERSION:
        raise BuildError(f"{path}: version {doc.get('version')!r}, this reader reads version {VERSION}")
    asset = doc.get("asset")
    if not isinstance(asset, dict):
        raise BuildError(f"{path}: 'asset' is missing or not an object")
    for key, typ in _ASSET_KEYS.items():
        if not isinstance(asset.get(key), typ) or isinstance(asset.get(key), bool):
            raise BuildError(f"{path}: asset.{key} is missing or not a {typ.__name__}")
    for key, typ in (("materials", list), ("nodes", list), ("notes", list), ("stats", dict)):
        if not isinstance(doc.get(key), typ):
            raise BuildError(f"{path}: '{key}' is missing or not a {typ.__name__}")
    if not all(isinstance(n, str) for n in doc["notes"]):
        raise BuildError(f"{path}: every note is a sentence (a string)")
    nodes: list[dict[str, Any]] = []
    for i, n in enumerate(doc["nodes"]):
        if not isinstance(n, dict):
            raise BuildError(f"{path}: nodes[{i}] is not an object")
        if n.get("kind") == "mesh_ref":
            params = n.get("params")
            rel = params.get("path") if isinstance(params, dict) else None
            if not isinstance(rel, str) or not rel:
                raise BuildError(f"{path}: nodes[{i}] ({n.get('id')}) mesh_ref has no params.path")
            if Path(rel).is_absolute():
                raise BuildError(f"{path}: nodes[{i}] ({n.get('id')}) path {rel!r} must be relative to the folder")
            target = (folder / rel).resolve()
            if not target.is_relative_to(folder):
                raise BuildError(f"{path}: nodes[{i}] ({n.get('id')}) path {rel!r} leaves the folder")
            if not target.is_file():
                raise BuildError(f"{path}: nodes[{i}] ({n.get('id')}) part file {rel} is missing")
            n = {**n, "params": {**params, "path": str(target)}}
        nodes.append(n)
    return Build(folder=folder, name=asset["name"], engine=asset["engine"], max_tris=asset["max_tris"],
                 brief=asset["brief"], art_direction=asset["art_direction"], nodes=nodes,
                 materials=list(doc["materials"]), notes=list(doc["notes"]), stats=dict(doc["stats"]))
