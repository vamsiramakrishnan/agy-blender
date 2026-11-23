"""Which engine a game project is, and which version, read from the project's own files.

The fingerprints and their ambiguities are adapted from the router's engine-detection table of
awesome-gamedev-agent-skills (``router/references/engine-detection.md``, commit c6ca6c4, Apache-2.0). Differences
from that table, on purpose:

* Only the root of the given directory is read (no recursive search): a project is the directory that holds its
  project file. A monorepo root is not a project, and is reported as such.
* The table stops at the first match in its precedence order. Here two project-file engines in one directory
  (``project.godot`` beside a ``*.uproject``, say) are "ambiguous": an install must not pick one of them on a
  guess. The documented disambiguations are kept: ``project.godot`` decides over a ``.csproj``; Unity needs the
  ``Assets/`` + ``ProjectSettings/ProjectVersion.txt`` pair; three.js counts only where no project-file engine is.
* A Rojo ``*.project.json`` counts only when it has Rojo's ``tree`` object (other tools use the same suffix).

The version is what the project's files say, or None with the reason. Nothing is inferred from an installed engine.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# engine id -> the package target_engine that installs into it (None: no package target exists for it; none is None today)
TARGET_OF = {"godot": "godot_4", "unity": "unity_urp", "unreal": "unreal_5", "roblox": "roblox_r15", "threejs": "web_three"}


@dataclass
class ProjectInfo:
    engine: str | None                 # "godot" | "unity" | "unreal" | "roblox" | "threejs" | None (cannot tell)
    version: str | None = None         # as the project's files state it ("4.7", "2022.3.10f1", "5.3", "^0.160.0")
    evidence: list[str] = field(default_factory=list)   # which files said so
    reason: str | None = None          # why engine or version is None
    candidates: list[str] = field(default_factory=list)  # engines whose fingerprints were found (ambiguous)
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def known(self) -> bool:
        return self.engine is not None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _godot(root: Path) -> ProjectInfo | None:
    f = root / "project.godot"
    if not f.is_file():
        return None
    text = f.read_text(errors="replace")
    info = ProjectInfo("godot", evidence=["project.godot"])
    cv = re.search(r"^\s*config_version\s*=\s*(\d+)", text, re.M)
    info.details["config_version"] = int(cv.group(1)) if cv else None
    # Godot 4 writes config_version=5; Godot 3 wrote 4 (and 3 before it)
    feats = re.search(r'^\s*config/features\s*=\s*PackedStringArray\(([^)]*)\)', text, re.M)
    tags = re.findall(r'"([^"]*)"', feats.group(1)) if feats else []
    info.details["features"] = tags
    numbered = [t for t in tags if re.fullmatch(r"\d+\.\d+", t)]
    if numbered:
        info.version = numbered[0]
    elif cv and int(cv.group(1)) == 5:
        info.version, info.reason = "4", "config/features names no engine version; config_version=5 means Godot 4"
    elif cv and int(cv.group(1)) < 5:
        info.version, info.reason = "3", f"config_version={cv.group(1)} is a Godot 3 project"
    else:
        info.reason = "project.godot states neither config/features with a version nor config_version"
    pe = re.search(r'^\s*3d/physics_engine\s*=\s*"([^"]*)"', text, re.M)
    info.details["physics_engine"] = pe.group(1) if pe else None
    return info


def _unity(root: Path) -> ProjectInfo | None:
    pv = root / "ProjectSettings" / "ProjectVersion.txt"
    if not ((root / "Assets").is_dir() and pv.is_file()):
        return None
    info = ProjectInfo("unity", evidence=["Assets/", "ProjectSettings/ProjectVersion.txt"])
    m = re.search(r"^m_EditorVersion:\s*(\S+)", pv.read_text(errors="replace"), re.M)
    if m:
        info.version = m.group(1)
    else:
        info.reason = "ProjectVersion.txt has no m_EditorVersion"
    manifest = root / "Packages" / "manifest.json"
    if manifest.is_file():
        try:
            deps = json.loads(manifest.read_text()).get("dependencies") or {}
        except ValueError:
            deps = {}
        info.details["urp"] = "com.unity.render-pipelines.universal" in deps
        info.details["hdrp"] = "com.unity.render-pipelines.high-definition" in deps
        info.details["gltfast"] = deps.get("com.unity.cloud.gltfast")
        info.evidence.append("Packages/manifest.json")
    return info


def _unreal(root: Path) -> ProjectInfo | None:
    found = sorted(root.glob("*.uproject"))
    if not found:
        return None
    info = ProjectInfo("unreal", evidence=[p.name for p in found])
    if len(found) > 1:
        info.reason = f"{len(found)} .uproject files"
        return info
    try:
        assoc = json.loads(found[0].read_text(errors="replace")).get("EngineAssociation")
    except ValueError:
        assoc = None
        info.reason = f"{found[0].name} is not valid JSON"
    if isinstance(assoc, str) and re.fullmatch(r"\d+\.\d+(\.\d+)?", assoc):
        info.version = assoc
    elif assoc:
        info.reason = f"EngineAssociation {assoc!r} names a source or custom build, not a version"
    elif info.reason is None:
        info.reason = "EngineAssociation is empty"
    return info


def _roblox(root: Path) -> ProjectInfo | None:
    places = sorted(p.name for p in [*root.glob("*.rbxl"), *root.glob("*.rbxlx")])
    rojo = []
    for p in sorted(root.glob("*.project.json")):
        try:
            if isinstance(json.loads(p.read_text(errors="replace")).get("tree"), dict):
                rojo.append(p.name)
        except ValueError:
            continue
    if not places and not rojo:
        return None
    return ProjectInfo("roblox", evidence=places + rojo,
                       reason="Roblox has no project version: the platform is a rolling release",
                       details={"rojo": rojo, "places": places})


def _threejs(root: Path) -> ProjectInfo | None:
    pj = root / "package.json"
    if not pj.is_file():
        return None
    try:
        data = json.loads(pj.read_text(errors="replace"))
    except ValueError:
        return None
    spec = next((d["three"] for k in ("dependencies", "devDependencies", "peerDependencies")
                 if isinstance(d := data.get(k), dict) and "three" in d), None)
    if spec is None:
        return None
    info = ProjectInfo("threejs", evidence=["package.json"], version=str(spec))
    lock = root / "package-lock.json"
    if lock.is_file():
        try:
            resolved = (json.loads(lock.read_text()).get("packages") or {}).get("node_modules/three", {}).get("version")
        except ValueError:
            resolved = None
        if resolved:
            info.version = resolved
            info.evidence.append("package-lock.json")
    if info.version == str(spec):
        info.reason = "version is package.json's range; no resolved lockfile entry"
    return info


def detect_project(directory: str | Path) -> ProjectInfo:
    """The engine and version of the project rooted at ``directory``; ``engine`` None when it cannot tell."""
    root = Path(directory)
    if not root.is_dir():
        return ProjectInfo(None, reason=f"{root} is not a directory")
    project_engines = [i for i in (_godot(root), _unreal(root), _unity(root), _roblox(root)) if i is not None]
    if len(project_engines) > 1:
        return ProjectInfo(None, reason="more than one engine's project files in this directory: "
                           + "; ".join(f"{i.engine} ({', '.join(i.evidence)})" for i in project_engines)
                           + ". Point --project at the engine project's own directory",
                           candidates=[i.engine for i in project_engines],
                           evidence=[e for i in project_engines for e in i.evidence])
    if project_engines:
        info = project_engines[0]
        if info.engine == "unreal" and len(info.evidence) > 1:
            return ProjectInfo(None, reason=f"several .uproject files: {', '.join(info.evidence)}", candidates=["unreal"],
                               evidence=info.evidence)
        return info
    if (web := _threejs(root)) is not None:
        return web
    half = [n for n, ok in (("Assets/ without ProjectSettings/ProjectVersion.txt (not a Unity project on its own)",
                             (root / "Assets").is_dir() and not (root / "ProjectSettings" / "ProjectVersion.txt").is_file()),
                            ("a .csproj (C# alone names no engine)", any(root.glob("*.csproj")))) if ok]
    return ProjectInfo(None, reason="no engine project file in this directory" + (f"; found {', '.join(half)}" if half else ""))


def version_tuple(text: str | None) -> tuple[int, ...] | None:
    """Leading numeric components of a version string ("4.7.2-stable" -> (4, 7, 2)), or None."""
    m = re.match(r"\D*(\d+(?:\.\d+)*)", text or "")
    return tuple(int(x) for x in m.group(1).split(".")) if m else None
