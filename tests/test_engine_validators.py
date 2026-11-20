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
    good.write_text("#!/bin/sh\necho ran \"$@\"\necho 'AGY_IMPORT_OK {\"triangles\":12}'\n")
    rep = await v.validate(pkg)
    assert rep.ok and "--headless" in rep.log_tail and rep.skipped_reason is None
    good.write_text("#!/bin/sh\necho 'SCRIPT ERROR: boom'\nexit 0\n")
    assert not (await v.validate(pkg)).ok
    good.write_text("#!/bin/sh\nexit 1\n")
    assert not (await v.validate(pkg)).ok


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
