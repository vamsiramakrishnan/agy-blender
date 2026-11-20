"""Headless engine import smoke tests behind one interface.

Godot checks loaded mesh surfaces and can capture the imported asset with a real
renderer. Binary discovery uses the environment override first, followed by PATH.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import signal
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from antigravity3d.kernel.types import bounds_issue
from antigravity3d.state import TargetEngine

TAIL_CHARS = 4000
DEFAULT_TIMEOUT = 300.0


MIN_SILHOUETTE_IOU = 0.98


def silhouette_iou(dcc_png: bytes, engine_png: bytes) -> float:
    """Compare foreground coverage in matched transparent, unresampled orthographic frames."""
    import numpy as np
    from PIL import Image

    masks = []
    for data in (dcc_png, engine_png):
        with Image.open(io.BytesIO(data)) as image:
            if "A" not in image.getbands():
                raise ValueError("silhouette evidence requires a rendered alpha channel")
            mask = np.asarray(image.getchannel("A")) >= 128
        if not mask.any() or mask.all():
            raise ValueError("silhouette must contain foreground and background")
        masks.append(mask)
    a, b = masks
    if a.shape != b.shape:
        raise ValueError("DCC and engine frame dimensions differ")
    return float(np.count_nonzero(a & b) / np.count_nonzero(a | b))


def blender_frame_png(frame) -> bytes:
    """Convert Blender's top-down scene-linear EXR pixels to display sRGB, preserving coverage."""
    import numpy as np
    from PIL import Image

    rgba = np.asarray(frame)
    if rgba.ndim != 3 or rgba.shape[2] != 4 or not np.isfinite(rgba).all():
        raise ValueError("expected finite RGBA Blender frame")
    linear, alpha = np.clip(rgba[..., :3], 0, 1), np.clip(rgba[..., 3:], 0, 1)
    srgb = np.where(linear < 0.0031308, 12.92 * linear, 1.055 * linear ** (1 / 2.4) - 0.055)
    buf = io.BytesIO()
    Image.fromarray(np.rint(np.concatenate([srgb, alpha], axis=2) * 255).astype(np.uint8)).save(buf, "PNG")
    return buf.getvalue()


@dataclass(frozen=True)
class EngineReport:
    ok: bool
    engine: str
    log_tail: str = ""
    skipped_reason: str | None = None
    metrics: dict[str, int] | None = None
    bounds_m: list[list[float]] | None = None

    def geometry_issue(self, manifest: dict) -> str | None:
        if self.metrics is not None and self.metrics.get("triangles") != manifest["tris"]:
            return "target import triangle count differs from the package"
        if self.engine == "godot_4":
            return bounds_issue(manifest.get("glb_bounds_m"), self.bounds_m)
        return None

    @property
    def skipped(self) -> bool:
        return self.skipped_reason is not None


class EngineValidator(ABC):
    engine: str
    env_var: str
    binary_names: tuple[str, ...]
    error_markers: tuple[str, ...] = ()
    success_marker = "AGY_IMPORT_OK"

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

    async def capture(self, package_path: str | Path, png: Path, timeout: float = DEFAULT_TIMEOUT) -> EngineReport:
        return self.skipped(f"{self.engine} has no render adapter")

    async def _run(self, cmd: list[str], env: dict[str, str], timeout: float) -> EngineReport:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                env={**os.environ, **env}, stdin=asyncio.subprocess.DEVNULL, start_new_session=(os.name == "posix"))
        except OSError as e:
            return EngineReport(False, self.engine, f"failed to launch: {e}")
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            try:
                if os.name == "posix":
                    os.killpg(proc.pid, signal.SIGKILL)  # xvfb-run may still be waiting for its renderer
                else:
                    proc.kill()
            except ProcessLookupError:
                pass
            await proc.communicate()
            if isinstance(exc, asyncio.CancelledError):
                raise
            return EngineReport(False, self.engine, f"timed out after {timeout}s")
        text = out.decode(errors="replace")
        bad = proc.returncode != 0 or self.success_marker not in text or any(m in text for m in self.error_markers)
        metrics = bounds = None
        for line in text.splitlines():
            if line.startswith("AGY_IMPORT_OK {"):
                try:
                    metrics = json.loads(line.removeprefix("AGY_IMPORT_OK "))
                    if not isinstance(metrics, dict) or not isinstance(metrics.get("triangles"), int) or metrics["triangles"] < 1:
                        bad = True
                    if isinstance(metrics, dict) and self.engine == "godot_4":
                        bounds = metrics.pop("bounds_m", None)
                        if bounds_issue(bounds, bounds):
                            bad = True
                except ValueError:
                    bad = True
        return EngineReport(not bad, self.engine, text[-TAIL_CHARS:], metrics=metrics, bounds_m=bounds)

    @abstractmethod
    def command(self, exe: str, package: Path, workdir: Path) -> tuple[list[str], dict[str, str]]:
        """Builds the headless command (may write helper files into ``workdir``)."""


_GODOT_CHECK = """extends SceneTree

func _init():
    call_deferred("check_asset")

func check_asset():
    var doc := GLTFDocument.new()
    var state := GLTFState.new()
    if doc.append_from_file(OS.get_environment("AGY_PACKAGE"), state) != OK:
        push_error("glTF load failed"); quit(1); return
    var scene := doc.generate_scene(state)
    if scene == null:
        push_error("generate_scene failed"); quit(1); return
    root.add_child(scene)
    var instances = scene.find_children("*", "MeshInstance3D", true, false)
    if scene is MeshInstance3D:
        instances.push_back(scene)
    var tri_count := 0
    var surfaces := 0
    var textured := 0
    var normal_mapped := 0
    var orm_mapped := 0
    var bounds := AABB()
    var has_vertex := false
    for instance in instances:
        var mesh: Mesh = instance.mesh
        if mesh == null:
            continue
        for i in mesh.get_surface_count():
            var arrays := mesh.surface_get_arrays(i)
            if mesh.surface_get_primitive_type(i) != Mesh.PRIMITIVE_TRIANGLES:
                continue
            var world_vertices: PackedVector3Array = instance.global_transform * arrays[Mesh.ARRAY_VERTEX]
            for vertex in world_vertices:
                bounds = bounds.expand(vertex) if has_vertex else AABB(vertex, Vector3.ZERO)
                has_vertex = true
            var indices = arrays[Mesh.ARRAY_INDEX]
            tri_count += (indices.size() if indices != null else arrays[Mesh.ARRAY_VERTEX].size()) / 3
            surfaces += 1
            var material = instance.get_active_material(i)
            if material is BaseMaterial3D and material.albedo_texture != null:
                textured += 1
            if material is BaseMaterial3D and material.normal_texture != null:
                normal_mapped += 1
            if material is BaseMaterial3D and material.ao_texture != null and material.roughness_texture != null and material.metallic_texture != null:
                orm_mapped += 1
    if tri_count <= 0 or bounds.size.length() <= 0:
        push_error("import produced no non-empty triangle geometry"); quit(1); return
    var extrema := [[bounds.position.x, bounds.position.y, bounds.position.z], [bounds.end.x, bounds.end.y, bounds.end.z]]
    print("AGY_IMPORT_OK ", JSON.stringify({"bounds_m":extrema, "triangles":tri_count, "surfaces":surfaces, "textured_surfaces":textured, "normal_surfaces":normal_mapped, "orm_surfaces":orm_mapped}))
    var path := OS.get_environment("AGY_CAPTURE")
    if path.is_empty():
        quit(0); return
    var viewport := SubViewport.new()
    viewport.size = Vector2i(512, 512)
    viewport.transparent_bg = true
    viewport.own_world_3d = true
    viewport.render_target_update_mode = SubViewport.UPDATE_ALWAYS
    root.add_child(viewport)
    scene.reparent(viewport, false)
    var camera := Camera3D.new()
    viewport.add_child(camera)
    var diameter: float = maxf(bounds.size.length(), 0.001)
    var center := bounds.get_center()
    camera.projection = Camera3D.PROJECTION_ORTHOGONAL
    camera.size = diameter * 1.1
    camera.near = diameter * 0.01
    camera.far = diameter * 5
    # Match Blender's auto-framed Z-up lookdev rig in the GLB's Y-up coordinates.
    camera.position = center + Vector3(1.4, 1.2, 2.0).normalized() * diameter * 2.0
    camera.look_at(center)
    camera.make_current()
    for spec in [[Vector3(2,4,3),3.0], [Vector3(-2,2,-1),1.0]]:
        var lamp := DirectionalLight3D.new()
        viewport.add_child(lamp)
        lamp.light_energy = spec[1]
        lamp.look_at_from_position(spec[0], Vector3.ZERO)
    await RenderingServer.frame_post_draw
    await RenderingServer.frame_post_draw
    var img := viewport.get_texture().get_image()
    if img == null or img.is_empty() or img.save_png(path) != OK:
        push_error("render capture failed"); quit(1); return
    print("AGY_RENDER_OK")
    quit(0)
"""


class GodotValidator(EngineValidator):
    engine, env_var, binary_names = "godot_4", "GODOT_BIN", ("godot", "godot4", "Godot")
    success_marker = "AGY_IMPORT_OK {"
    error_markers = ("SCRIPT ERROR", "ERROR: Failed to load", "glTF load failed")

    def command(self, exe: str, package: Path, workdir: Path) -> tuple[list[str], dict[str, str]]:
        (workdir / "project.godot").write_text('config_version=5\n[application]\nconfig/name="agy3d_check"\n')
        (workdir / "check.gd").write_text(_GODOT_CHECK)
        return [exe, "--headless", "--path", str(workdir), "--script", "check.gd"], {"AGY_PACKAGE": str(package), "AGY_CAPTURE": ""}

    async def capture(self, package_path: str | Path, png: Path, timeout: float = DEFAULT_TIMEOUT) -> EngineReport:
        from PIL import Image

        exe = self.binary()
        if not exe:
            return self.skipped("Godot binary not found")
        display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        xvfb = shutil.which("xvfb-run")
        if not display and not xvfb:
            return self.skipped("Godot render requires a display or xvfb-run for its OpenGL renderer")
        with tempfile.TemporaryDirectory(prefix="agy3d-godot-render-") as td:
            work = Path(td)
            cmd, env = self.command(exe, Path(package_path).resolve(), work)
            cmd[1:2] = ["--rendering-method", "gl_compatibility", "--audio-driver", "Dummy"]
            if not display:
                cmd = [xvfb, "-a", *cmd]
            rendered = work / "engine.png"
            env["AGY_CAPTURE"] = str(rendered)
            report = await self._run(cmd, env, timeout)
            if not report.ok or "AGY_RENDER_OK" not in report.log_tail or not rendered.is_file():
                return EngineReport(False, self.engine, report.log_tail)
            with Image.open(rendered) as image:
                if image.size != (512, 512) or all(lo == hi for lo, hi in image.convert("RGB").getextrema()):
                    return EngineReport(False, self.engine, "Godot returned an empty or incorrectly sized frame")
            png.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(rendered, png)
            return report


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
    Debug.Log("AGY_IMPORT_OK"); EditorApplication.Exit(0);
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
