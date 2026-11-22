import pytest

from antigravity3d.adapters.engines.validators import (
    EngineReport, GodotValidator, NoValidator, UnityValidator, UnrealValidator, validator_for)


@pytest.fixture(autouse=True)
def no_engines(monkeypatch):
    for v in ("GODOT_BIN", "UNREAL_EDITOR_CMD", "UNITY_BIN"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("PATH", "/nonexistent")


@pytest.mark.parametrize("cls", [GodotValidator, UnrealValidator, UnityValidator])
async def test_skipped_when_binary_absent(cls, tmp_path):
    v = cls()
    assert not v.available()
    rep = await v.validate(tmp_path / "x.glb")
    assert isinstance(rep, EngineReport) and not rep.ok and rep.skipped
    assert v.env_var in rep.skipped_reason


def test_mapping():
    assert isinstance(validator_for("godot_4"), GodotValidator)
    assert isinstance(validator_for("unreal_5"), UnrealValidator)
    assert isinstance(validator_for("unity_urp"), UnityValidator)
    assert isinstance(validator_for("roblox_r15"), NoValidator)
    with pytest.raises(ValueError):
        validator_for("quake")  # type: ignore[arg-type]


async def test_roblox_always_skipped(tmp_path):
    rep = await validator_for("roblox_r15").validate(tmp_path)
    assert rep.skipped_reason and "roblox" in rep.skipped_reason and not validator_for("roblox_r15").available()


async def test_env_override_and_run(tmp_path, monkeypatch):
    # a fake "godot" that echoes args and exits 0, then one that logs a script error
    good = tmp_path / "godot"
    good.write_text("#!/bin/sh\necho ran \"$@\"\n")
    good.chmod(0o755)
    pkg = tmp_path / "a.glb"
    pkg.write_bytes(b"glTF")
    monkeypatch.setenv("GODOT_BIN", str(good))
    v = GodotValidator()
    assert v.available()
    assert not (await v.validate(pkg)).ok  # zero exit alone cannot attest an import
    good.write_text("#!/bin/sh\necho ran \"$@\"\necho 'AGY_IMPORT_OK {\"triangles\":12,\"bounds_m\":[[0,0,0],[1,1,1]]}'\n")
    rep = await v.validate(pkg)
    assert rep.ok and "--headless" in rep.log_tail and rep.skipped_reason is None
    good.write_text("#!/bin/sh\necho 'SCRIPT ERROR: boom'\nexit 0\n")
    assert not (await v.validate(pkg)).ok
    good.write_text("#!/bin/sh\nexit 1\n")
    assert not (await v.validate(pkg)).ok


async def test_godot_import_with_an_unloaded_image_fails(tmp_path, monkeypatch):
    fake = tmp_path / "godot"
    pkg = tmp_path / "a_LOD1.glb"
    pkg.write_bytes(b"glTF")
    monkeypatch.setenv("GODOT_BIN", str(fake))
    line = 'AGY_IMPORT_OK {"triangles":12,"bounds_m":[[0,0,0],[1,1,1]],"images":3,"images_loaded":%d}'
    for loaded, ok in ((3, True), (2, False)):
        fake.write_text(f"#!/bin/sh\necho '{line % loaded}'\n")
        fake.chmod(0o755)
        rep = await GodotValidator().validate(pkg)
        assert rep.ok is ok and rep.metrics["images_loaded"] == loaded


def test_staging_a_far_lod_brings_the_textures_it_references(tmp_path):
    import numpy as np

    from antigravity3d.adapters.engines.validators import stage_glb
    from antigravity3d.kernel.gltf_writer import EncodedImage, GltfMaterial, GltfMesh, TextureRef, build_glb

    pkg = tmp_path / "pkg"
    (pkg / "textures").mkdir(parents=True)
    png = b"\x89PNG\r\n\x1a\n" + b"x" * 8  # bytes only: staging does not decode
    a, b = EncodedImage(png + b"a", "image/png", "a"), EncodedImage(png + b"b", "image/png", "b")
    (pkg / "textures" / "a map.png").write_bytes(a.data)
    (tmp_path / "outside.png").write_bytes(b.data)
    mesh = GltfMesh("t", np.eye(3), np.array([[0, 1, 2]]), np.zeros((3, 2)),
                    GltfMaterial(name="m", base_color_texture=TextureRef(a), normal_texture=TextureRef(b)))
    (pkg / "Prop_LOD1.glb").write_bytes(build_glb([mesh], image_uris={"a": "textures/a%20map.png", "b": "../outside.png"}))
    work = tmp_path / "unity"
    cmd, env = UnityValidator().command("unity", pkg / "Prop_LOD1.glb", work)
    assets = work / "Assets"
    assert env["AGY_ASSET"] == "Assets/Prop_LOD1.glb" and (assets / "Prop_LOD1.glb").read_bytes() == (pkg / "Prop_LOD1.glb").read_bytes()
    assert (assets / "textures" / "a map.png").read_bytes() == a.data
    assert not (work / "outside.png").exists() and not (assets / "outside.png").exists()  # never written outside the project


async def test_missing_package(tmp_path, monkeypatch):
    exe = tmp_path / "unity"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    monkeypatch.setenv("UNITY_BIN", str(exe))
    rep = await UnityValidator().validate(tmp_path / "missing.fbx")
    assert not rep.ok and not rep.skipped_reason and "not found" in rep.log_tail


def test_measured_silhouette_rejects_mismatch_and_missing_foreground():
    import numpy as np
    from antigravity3d.adapters.engines.validators import blender_frame_png, silhouette_iou

    frame = np.zeros((32, 32, 4))
    frame[5:15, 5:15] = 1
    a = blender_frame_png(frame)
    assert silhouette_iou(a, a) == 1
    b = blender_frame_png(np.roll(frame, 5, axis=1))
    assert silhouette_iou(a, b) == pytest.approx(1 / 3)
    with pytest.raises(ValueError, match="foreground"):
        silhouette_iou(a, blender_frame_png(frame * 0))


async def test_web_three_validator_follows_the_engine_validator_contract(tmp_path, monkeypatch):
    from antigravity3d.adapters.engines.web_three import WebThreeReport, WebThreeValidator

    import sys
    import types

    # "Package present, browser absent", independent of what this machine has installed or on PATH.
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.delenv("CHROMIUM_BIN", raising=False)
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setenv("HOME", str(tmp_path))
    v = WebThreeValidator()
    assert not v.available()
    rep = await v.validate(tmp_path / "x.glb")
    assert isinstance(rep, EngineReport) and isinstance(rep, WebThreeReport) and rep.skipped and not rep.ok
    assert v.env_var in rep.skipped_reason
