"""A package is a function of the scene and the budget: same input, same bytes, wherever and whenever it is built."""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from pxr import Sdf, Usd

from antigravity3d.ir import MaterialIR, NodeIR, SceneIR, Transform
from antigravity3d.kernel.fast_ops import run_export_pipeline
from antigravity3d.kernel.package import write_engine_package
from antigravity3d.kernel.usd_stage import StageManager
from antigravity3d.kernel.usdz import create_usdz, normalise_timestamps
from antigravity3d.state import EngineBudget


def _albedo(path: Path) -> Path:
    rng = np.random.default_rng(7)
    buf = io.BytesIO()
    Image.fromarray(rng.integers(40, 220, (32, 32, 3), dtype=np.uint8), "RGB").save(buf, "PNG")
    path.write_bytes(buf.getvalue())
    return path


def _export(root: Path, engine: str) -> Path:
    """Build the same two-part, two-material scene under ``root`` and write its package."""
    root.mkdir(parents=True)
    tex = _albedo(root / "wood_albedo.png")
    scene = SceneIR(asset_name="Crate", nodes=[
        NodeIR(id="body", kind="box", params={"size": [1, 0.8, 0.6]}, transform=Transform(translate=(0, 0, 0.3)), material_id="wood"),
        NodeIR(id="post", kind="cylinder", params={"radius": 0.1, "height": 0.5, "segments": 24},
               transform=Transform(translate=(0, 0, 0.85)), material_id="iron"),
    ], materials=[MaterialIR(id="wood", base_color=(0.5, 0.35, 0.2), albedo_uri=str(tex)),
                  MaterialIR(id="iron", base_color=(0.5, 0.5, 0.55), metallic=1.0)])
    sm = StageManager.create(root / "Crate_stage.usda", "Crate")
    sm.apply_ir(scene)
    budget = EngineBudget(engine)
    bundle = run_export_pipeline(sm.extract_meshes(), budget, "Crate")
    write_engine_package(sm, bundle, budget, root / "package", asset_name="Crate")
    return root / "package"


def _files(pkg: Path) -> dict[str, bytes]:
    return {str(p.relative_to(pkg)): p.read_bytes() for p in sorted(pkg.rglob("*")) if p.is_file()}


@pytest.mark.parametrize("engine", ["unreal_5", "godot_4"])
def test_two_exports_of_one_scene_are_byte_identical(tmp_path: Path, engine: str) -> None:
    a, b = _files(_export(tmp_path / "first" / "run", engine)), _files(_export(tmp_path / "second" / "elsewhere" / "run", engine))
    assert set(a) == set(b) and {"Crate.usd", "Crate.usdz", "Crate.glb", "manifest.json"} <= set(a)
    different = sorted(name for name in a if a[name] != b[name])
    assert not different, f"files that differ between two exports of the same scene: {different}"


def test_shipped_files_carry_no_local_path(tmp_path: Path) -> None:
    """The working directory must not leak into what ships (USD used to carry it in a 'doc' comment)."""
    pkg = _export(tmp_path / "private-workdir-marker" / "run", "godot_4")
    marker = b"private-workdir-marker"
    leaking = sorted(name for name, data in _files(pkg).items() if marker in data)
    assert not leaking, f"local path found in: {leaking}"
    with zipfile.ZipFile(pkg / "Crate.usdz") as z:
        assert not any(marker in z.read(n) for n in z.namelist())
    layer = Sdf.Layer.FindOrOpen(str(pkg / "Crate.usd"))
    assert "private-workdir-marker" not in layer.ExportToString()


def test_usdz_does_not_depend_on_when_it_was_packaged(tmp_path: Path) -> None:
    pkg = _export(tmp_path / "run", "godot_4")
    usd = pkg / "Crate.usd"
    archives = []
    for i, when in enumerate((1_000_000_000, 1_700_000_000)):  # the same files, "modified" 22 years apart
        for f in [usd, *sorted((pkg / "textures").glob("*"))]:
            os.utime(f, (when, when))
        out = tmp_path / f"at_{i}.usdz"
        assert create_usdz(usd, out)
        archives.append(out.read_bytes())
    assert archives[0] == archives[1]
    out = tmp_path / "at_0.usdz"
    with zipfile.ZipFile(out) as z:
        infos = z.infolist()
        assert z.testzip() is None  # every entry's CRC still matches its data
        assert len(infos) >= 2 and infos[0].filename == "Crate.usd"  # root layer first, the texture after it
        assert all(i.date_time == (1980, 1, 1, 0, 0, 0) and i.compress_type == zipfile.ZIP_STORED for i in infos)
    zf = Sdf.ZipFile.Open(str(out))  # pxr's own reader, and the layout the USDZ specification requires
    assert all(zf.GetFileInfo(n).dataOffset % 64 == 0 for n in zf.GetFileNames())
    stage = Usd.Stage.Open(str(out))
    assert stage and stage.GetDefaultPrim().GetName() == "Crate"


def test_normalise_timestamps_rejects_what_is_not_an_archive(tmp_path: Path) -> None:
    junk = tmp_path / "junk.usdz"
    junk.write_bytes(b"not a zip archive at all")
    with pytest.raises(ValueError, match="not a zip archive"):
        normalise_timestamps(junk)
    assert junk.read_bytes() == b"not a zip archive at all"  # left untouched
