"""Headless engine import smoke tests behind one interface.

None of Godot / Unreal / Unity is installed in this container, so only the
"binary absent -> skipped" paths are exercised by tests. The command lines and
embedded check scripts are written to spec from each engine's documented CLI and
are UNVERIFIED. Binary discovery: env-var override, then PATH.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from antigravity3d.state import TargetEngine

TAIL_CHARS = 4000
DEFAULT_TIMEOUT = 300.0


@dataclass(frozen=True)
class EngineReport:
    ok: bool
    engine: str
    log_tail: str = ""
    skipped_reason: str | None = None

    @property
    def skipped(self) -> bool:
        return self.skipped_reason is not None


class EngineValidator(ABC):
    engine: str
    env_var: str
    binary_names: tuple[str, ...]
    error_markers: tuple[str, ...] = ()

    def binary(self) -> str | None:
        override = os.environ.get(self.env_var)
        if override:
            return override if os.access(override, os.X_OK) else shutil.which(override)
        for n in self.binary_names:
            if p := shutil.which(n):
                return p
        return None

    def available(self) -> bool:
        return self.binary() is not None

    def skipped(self, reason: str) -> EngineReport:
        return EngineReport(False, self.engine, "", reason)

    async def validate(self, package_path: str | Path, timeout: float = DEFAULT_TIMEOUT) -> EngineReport:
        exe = self.binary()
        if exe is None:
            return self.skipped(f"{self.engine} binary not found (set ${self.env_var} or add {self.binary_names[0]} to PATH)")
        pkg = Path(package_path).resolve()
        if not pkg.exists():
            return EngineReport(False, self.engine, f"package not found: {pkg}")
        with tempfile.TemporaryDirectory(prefix=f"agy3d-{self.engine}-") as td:
            cmd, env = self.command(exe, pkg, Path(td))
            return await self._run(cmd, env, timeout)

    async def _run(self, cmd: list[str], env: dict[str, str], timeout: float) -> EngineReport:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                env={**os.environ, **env}, stdin=asyncio.subprocess.DEVNULL)
        except OSError as e:
            return EngineReport(False, self.engine, f"failed to launch: {e}")
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return EngineReport(False, self.engine, f"timed out after {timeout}s")
        text = out.decode(errors="replace")
        bad = proc.returncode != 0 or any(m in text for m in self.error_markers)
        return EngineReport(not bad, self.engine, text[-TAIL_CHARS:])

    @abstractmethod
    def command(self, exe: str, package: Path, workdir: Path) -> tuple[list[str], dict[str, str]]:
        """Builds the headless command (may write helper files into ``workdir``)."""


_GODOT_CHECK = """extends SceneTree
func _init():
\tvar doc := GLTFDocument.new()
\tvar state := GLTFState.new()
\tif doc.append_from_file(OS.get_environment("AGY_PACKAGE"), state) != OK:
\t\tpush_error("glTF load failed"); quit(1); return
\tvar n := doc.generate_scene(state)
\tif n == null:
\t\tpush_error("generate_scene failed"); quit(1); return
\tquit(0)
"""


class GodotValidator(EngineValidator):
    engine, env_var, binary_names = "godot_4", "GODOT_BIN", ("godot", "godot4", "Godot")
    error_markers = ("SCRIPT ERROR", "ERROR: Failed to load", "glTF load failed")

    def command(self, exe: str, package: Path, workdir: Path) -> tuple[list[str], dict[str, str]]:
        (workdir / "project.godot").write_text('config_version=5\n[application]\nconfig/name="agy3d_check"\n')
        (workdir / "check.gd").write_text(_GODOT_CHECK)
        return [exe, "--headless", "--path", str(workdir), "--import", "--script", "check.gd"], {"AGY_PACKAGE": str(package)}


_UNREAL_PY = """import os, sys, unreal
task = unreal.AssetImportTask()
task.set_editor_property('filename', os.environ['AGY_PACKAGE'])
task.set_editor_property('destination_path', '/Game/AgyCheck')
task.set_editor_property('automated', True)
task.set_editor_property('save', False)
unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
ok = bool(task.get_editor_property('imported_object_paths'))
unreal.log('AGY_IMPORT_OK' if ok else 'AGY_IMPORT_FAILED')
sys.exit(0 if ok else 1)
"""


class UnrealValidator(EngineValidator):
    engine, env_var, binary_names = "unreal_5", "UNREAL_EDITOR_CMD", ("UnrealEditor-Cmd",)
    error_markers = ("AGY_IMPORT_FAILED", "Traceback (most recent call last)")

    def command(self, exe: str, package: Path, workdir: Path) -> tuple[list[str], dict[str, str]]:
        (workdir / "check.uproject").write_text(
            '{"FileVersion":3,"Plugins":[{"Name":"PythonScriptPlugin","Enabled":true}]}')
        (workdir / "check.py").write_text(_UNREAL_PY)
        return [exe, str(workdir / "check.uproject"), "-run=pythonscript", f"-script={workdir / 'check.py'}",
                "-unattended", "-nopause", "-nosplash", "-stdout", "-FullStdOutLogOutput"], {"AGY_PACKAGE": str(package)}


_UNITY_CS = """using UnityEditor; using UnityEngine; using System;
public static class AgyValidate {
  public static void Run() {
    var path = Environment.GetEnvironmentVariable("AGY_ASSET");
    AssetDatabase.Refresh();
    var go = AssetDatabase.LoadAssetAtPath<GameObject>(path);
    if (go == null) { Debug.LogError("AGY_IMPORT_FAILED " + path); EditorApplication.Exit(1); return; }
    EditorApplication.Exit(0);
  }
}
"""


class UnityValidator(EngineValidator):
    engine, env_var, binary_names = "unity_urp", "UNITY_BIN", ("Unity", "unity-editor", "unity")
    error_markers = ("AGY_IMPORT_FAILED", "error CS")

    def command(self, exe: str, package: Path, workdir: Path) -> tuple[list[str], dict[str, str]]:
        (workdir / "Assets" / "Editor").mkdir(parents=True)
        (workdir / "Assets" / "Editor" / "AgyValidate.cs").write_text(_UNITY_CS)
        shutil.copy2(package, workdir / "Assets" / package.name)
        return [exe, "-batchmode", "-nographics", "-quit", "-projectPath", str(workdir),
                "-executeMethod", "AgyValidate.Run", "-logFile", "-"], {"AGY_ASSET": f"Assets/{package.name}"}


class NoValidator(EngineValidator):
    env_var, binary_names = "", ("",)

    def __init__(self, engine: str, reason: str):
        self.engine, self._reason = engine, reason

    def available(self) -> bool:
        return False

    async def validate(self, package_path: str | Path, timeout: float = DEFAULT_TIMEOUT) -> EngineReport:
        return self.skipped(self._reason)

    def command(self, exe: str, package: Path, workdir: Path) -> tuple[list[str], dict[str, str]]:
        raise NotImplementedError


def validator_for(target_engine: TargetEngine) -> EngineValidator:
    match target_engine:
        case "godot_4":
            return GodotValidator()
        case "unreal_5":
            return UnrealValidator()
        case "unity_urp":
            return UnityValidator()
        case "roblox_r15":
            return NoValidator("roblox_r15", "roblox has no headless import validator")
    raise ValueError(f"unknown target engine {target_engine!r}")
