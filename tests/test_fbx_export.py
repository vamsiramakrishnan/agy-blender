"""FBX export of a shipped package: the reviewed upstream script as a worker operation, its contract, and its proof.

Real Blender 5.3 (marked ``blender``; CI's ``stock-engines`` job runs them): each engine preset on a textured multi-part
package, the round-trip comparison (including the header parsed straight from the written file) catching a planted scale
error and a planted quarter turn, the contract refusing every excluded keyword and any path outside the given directory,
a rejected export leaving nothing behind. Offline: the fake worker enforcing the same contract, the engine plan, and the
header checks. No Unity, Unreal Engine or Roblox Studio exists here: what a test can claim is "re-imported in Blender and
equal to the source, header as the preset promises", never "imports correctly in an engine".
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
import textwrap
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from antigravity3d.adapters.blender_5_3 import fbx_export, skills
from antigravity3d.adapters.blender_5_3.pool import BlenderOpError, BlenderPool, find_blender

ASSET = "Prop"
ENGINE_OF = {"unity": "unity_urp", "unreal": "unreal_5", "roblox": "roblox_r15"}
BLENDER_ENV = {"AGY3D_BLENDER_SKILLS": ""}  # the vendored copy, as production loads it


# ------------------------------------------------------------------------------------------------- subjects


def _png(path: Path, seed: int) -> str:
    rng = np.random.default_rng(seed)
    Image.fromarray(rng.integers(0, 255, (32, 32, 3), dtype=np.uint8)).save(path)
    return str(path)


def build_package(tmp: Path, engine: str, subject: str = "prop") -> Path:
    """A real package (``write_engine_package``): LOD levels, collision hulls, copies of the maps under ``textures/``."""
    from antigravity3d.ir import MaterialIR, NodeIR, SceneIR, Transform
    from antigravity3d.kernel.fast_ops import run_export_pipeline
    from antigravity3d.kernel.package import write_engine_package
    from antigravity3d.kernel.usd_stage import StageManager
    from antigravity3d.state import EngineBudget

    maps = tmp / "maps"
    maps.mkdir(parents=True, exist_ok=True)
    albedo, orm, normal = (_png(maps / f"{k}.png", i) for i, k in enumerate(("albedo", "orm", "normal")))
    paint = MaterialIR(id="paint", albedo_uri=albedo, orm_uri=orm, normal_uri=normal)
    if subject == "prop":  # three parts, one textured material, a parent that moves and turns them
        materials = [paint]
        nodes = [
            NodeIR(id="root", kind="group", transform=Transform(translate=(2, 1, 0), rotate_euler_deg=(0, 0, 90))),
            NodeIR(id="base", kind="cylinder", params={"radius": 0.5, "height": 0.4}, parent="root",
                   transform=Transform(translate=(0, 0, 0.2), scale=(1.5, 1, 1)), material_id="paint"),
            NodeIR(id="ball", kind="sphere", params={"radius": 0.3}, parent="root", transform=Transform(translate=(0, 0, 0.7)), material_id="paint"),
            NodeIR(id="slab", kind="box", params={"size": (1, 1, 0.1)}, parent="root", transform=Transform(translate=(0, 0, 0.05)), material_id="paint"),
        ]
    else:  # two materials: the textured one on two parts, a plain one with an emission map
        glow = MaterialIR(id="glow", base_color=(0.2, 0.4, 0.9), emissive_color=(1.0, 0.5, 0.2), emissive_uri=_png(maps / "emissive.png", 9))
        materials = [paint, glow]
        nodes = [
            NodeIR(id="a", kind="box", params={"size": (1.0, 0.5, 0.25)}, material_id="paint", transform=Transform(translate=(0, 0, 0.125))),
            NodeIR(id="b", kind="cylinder", params={"radius": 0.2, "height": 0.6}, material_id="glow", transform=Transform(translate=(0.9, 0, 0.3))),
            NodeIR(id="c", kind="sphere", params={"radius": 0.25}, material_id="paint", transform=Transform(translate=(-0.9, 0, 0.25))),
        ]
    scene = SceneIR(asset_name=ASSET, materials=materials, nodes=nodes)
    sm = StageManager.create(tmp / "work.usda", ASSET)
    sm.apply_ir(scene)
    sm.commit_all()
    budget = EngineBudget(engine)
    bundle = run_export_pipeline(sm.extract_meshes(), budget, ASSET)
    return write_engine_package(sm, bundle, budget, tmp / engine, asset_name=ASSET, qa={"tier1_ok": True}).out_dir


def request(package: Path, preset: str, **extra) -> dict:
    """The explicit files of a package: the layout knowledge stays out of the worker."""
    lods = sorted(package.glob(f"{ASSET}_LOD*.glb"))
    colliders = package / f"{ASSET}_colliders.glb"
    return {"source": str(package / f"{ASSET}.glb"), "lods": [str(p) for p in lods],
            "colliders": str(colliders) if colliders.exists() else None, "textures_dir": str(package / "textures"),
            "asset_name": ASSET, "preset": preset, **extra}


def snapshot(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): fbx_export.sha256_file(p) if p.is_file() else "dir" for p in sorted(root.rglob("*"))}


def failed(error: BlenderOpError) -> list[dict]:
    return json.loads(error.msg.split("failed checks: ", 1)[1])


def patch_unit_scale(src: Path, dst: Path, was: float, now: float) -> None:
    data = bytearray(src.read_bytes())
    at = data.index(b"UnitScaleFactor")
    pos = data.index(b"D" + struct.pack("<d", was), at)  # the property's value: type tag D, then a little-endian double
    data[pos + 1:pos + 9] = struct.pack("<d", now)
    dst.write_bytes(data)


# ------------------------------------------------------------------------------------------------- real Blender


@pytest.mark.blender
@pytest.mark.parametrize("preset", ["unity", "unreal", "roblox"])
async def test_fbx_export_each_preset_on_a_textured_multipart_package(tmp_path, preset):
    if not find_blender():
        pytest.skip("requires stock Blender")
    package = build_package(tmp_path, ENGINE_OF[preset])
    before = snapshot(package)
    async with BlenderPool(1, startup_timeout=120, extra_env=BLENDER_ENV) as pool:
        version = await pool.call("version")
        assert version["writers"] == ["export_asset"] and "export_asset" not in version["primitives"], "a writer is never an inspect_usd primitive"
        r = await pool.call("export_fbx", out_dir=str(package), **request(package, preset))
        fbx = Path(r["fbx"]["path"])
        assert r["ok"] and not r["fake"] and fbx == package.resolve() / f"{ASSET}.fbx" and r["fbx"]["bytes"] == fbx.stat().st_size
        assert r["fbx"]["sha256"] == fbx_export.sha256_file(fbx) and r["blender"].startswith("5.3")
        assert r["script"]["sha256"] == skills.PINNED_WRITERS["export_asset"][1] and r["script"]["commit"] == skills.UPSTREAM_COMMIT
        assert "NOT been opened in Unity" in r["verified"] and any("No Unity, Unreal" in s for s in r["not_verified"])
        # exactly one new file, beside the package's own: nothing was copied, no temporary name is left
        after = snapshot(package)
        assert set(after) - set(before) == {f"{ASSET}.fbx"} and all(after[k] == before[k] for k in before)
        # the contents per engine, and the round trip against the sources
        lod_tris = [lv["tris"] for lv in r["sources"]["levels"]]
        assert len(lod_tris) >= 2 and lod_tris == sorted(lod_tris, reverse=True)
        render = [(m["name"], m["tris"]) for m in r["contents"]["render_meshes"]]
        if preset == "unity":
            assert render == [(f"{ASSET}_LOD{i}", t) for i, t in enumerate(lod_tris)] and r["contents"]["collision_hulls"] == []
        else:
            assert render == [(ASSET, lod_tris[0])]
        if preset == "unreal":
            hulls = r["sources"]["collision_hulls"]["count"]
            assert hulls >= 1 and r["contents"]["collision_hulls"] == [f"UCX_{ASSET}_{i:02d}" for i in range(hulls)]
        assert [o["what"] for o in r["contents"]["omitted"]] == (["collision hulls"] if preset == "unity" else
                                                                  ["LOD levels 1.." + str(len(lod_tris) - 1)] + (["collision hulls"] if preset == "roblox" else []))
        assert r["comparison"]["ok"] and all(c["ok"] for c in r["comparison"]["checks"]) and len(r["comparison"]["checks"]) >= 20
        names = {c["name"] for c in r["comparison"]["checks"]}
        assert {"header.unit_scale_factor", "header.up_axis", f"bbox_m.{ASSET if preset != 'unity' else ASSET + '_LOD0'}", "materials.count",
                "images.resolvable", "images.bytes_from_the_sources"} <= names
        # the joined mesh is the sum of the batches: triangles, UV layer and custom normals survive the join
        assert all(j["tris"] == j["tris_after"] and j["uv_layers"] == j["uv_layers_after"] == 1 and j["custom_normals_after"] for j in r["join"])
        assert r["join"][0]["parts"] == 3 and len(r["join"][0]["material_slots_after"]) == 1
        # header facts parsed from the file: unit in the header (metres), no scale on any model, rotation as the preset promises
        h = r["header"]
        assert (h["UnitScaleFactor"], h["OriginalUnitScaleFactor"], h["UpAxis"], h["UpAxisSign"]) == (100.0, 100.0, 1, 1)
        quarter = (0.0, 0.0, 0.0) if preset == "unity" else (-90.0, 0.0, 0.0)
        for name, model in h["models"].items():
            assert model["scaling"] == [1.0, 1.0, 1.0] and model["parent"] is None, name
            assert tuple(round(a + b, 3) for a, b in zip(model["rotation_deg"], model["pre_rotation_deg"])) == quarter, name
        # textures: unity and unreal refer to the package's own maps by a relative path; roblox embeds
        refs = {t["slot"]: t for t in r["textures"]["references"]}
        assert set(refs) == {"DiffuseColor", "NormalMap"}
        if preset == "roblox":
            assert r["textures"]["mode"] == "embedded" and r["textures"]["embedded_bytes"] == 2 * 3172 > 0
            assert all(t["relative"].startswith(f"{ASSET}.fbm/") and t["relative"].endswith(".png") for t in refs.values())
            assert r["limits"]["roblox"]["ok"] and r["limits"]["roblox"]["meshes"][0]["open_edges"] == 0
        else:
            assert r["textures"]["mode"] == "relative_reference" and r["textures"]["embedded_bytes"] == 0 and r["limits"] is None
            assert {t["relative"] for t in refs.values()} == {"textures/albedo.png", "textures/normal.png"}
            assert r["fbx"]["bytes"] < 200_000, "no image bytes in the file"
        # the maps an FBX cannot carry are listed, never hidden
        assert set(r["maps"]["lost"]) == {"paint.roughness", "paint.metallic", "paint.occlusion"}
        # the package can be moved: the original location is gone, the FBX still resolves its textures (re-verified there)
        moved = tmp_path / "moved"
        shutil.copytree(package, moved)
        shutil.rmtree(package)
        again = await pool.call("verify_fbx", fbx=str(moved / f"{ASSET}.fbx"), **request(moved, preset))
        assert again["comparison"]["ok"] and again["fbx"]["sha256"] == r["fbx"]["sha256"]
    print(f"\n{preset}: {r['fbx']['bytes']} bytes, seconds {r['seconds']}")


@pytest.mark.blender
async def test_fbx_round_trip_catches_a_planted_scale_error_and_a_quarter_turn(tmp_path):
    if not find_blender():
        pytest.skip("requires stock Blender")
    package = build_package(tmp_path, "unreal_5")
    async with BlenderPool(1, startup_timeout=120, extra_env=BLENDER_ENV) as pool:
        good = await pool.call("export_fbx", out_dir=str(package), **request(package, "unreal"))
        fbx = Path(good["fbx"]["path"])
        assert (await pool.call("verify_fbx", fbx=str(fbx), **request(package, "unreal")))["comparison"]["ok"]
        # the hundredfold error: the header says centimetres where the vertices are metres
        bad = package / "Planted.fbx"
        patch_unit_scale(fbx, bad, 100.0, 1.0)
        with pytest.raises(BlenderOpError) as ei:
            await pool.call("verify_fbx", fbx=str(bad), **request(package, "unreal"))
        assert ei.value.cls == "FbxMismatch"
        by_name = {c["name"]: c for c in failed(ei.value)}
        assert by_name["header.unit_scale_factor"]["expected"] == 100.0 and by_name["header.unit_scale_factor"]["actual"] == 1.0
        assert by_name[f"extent_m.{ASSET}"]["actual"]["max"][0] == pytest.approx(0.025), "the header numbers alone also move the extent in metres"
        assert by_name[f"bbox_m.{ASSET}"]["actual"]["max"][0] == pytest.approx(0.025), "and Blender's own re-import sees the same error"
        # the quarter turn: a Unity-style FBX (axis conversion baked, no rotation) of a single-level asset, checked as the
        # Unreal one that wants the -90 degree turn on every model (same names, so only the transform differs)
        single = tmp_path / "single"
        shutil.copytree(package, single)
        solo = {"source": str(single / f"{ASSET}.glb"), "asset_name": "Solo", "textures_dir": str(single / "textures")}
        unity = await pool.call("export_fbx", out_dir=str(single), preset="unity", **solo)
        assert [m["name"] for m in unity["contents"]["render_meshes"]] == ["Solo"] and unity["comparison"]["ok"]
        with pytest.raises(BlenderOpError) as ei:
            await pool.call("verify_fbx", fbx=unity["fbx"]["path"], preset="unreal", **solo)
        turns = [c for c in failed(ei.value) if c["name"].endswith("rotation_deg")]
        assert turns and turns[0]["expected"] == [-90.0, 0.0, 0.0] and turns[0]["actual"] == pytest.approx([0.0, 0.0, 0.0])
        # a texture reference that no longer resolves is a failure too
        (package / "textures" / "albedo.png").rename(package / "textures" / "albedo.png.moved")
        with pytest.raises(BlenderOpError) as ei:
            await pool.call("verify_fbx", fbx=str(fbx), **request(package, "unreal"))
        assert any(c["name"] == "images.resolvable" for c in failed(ei.value))


@pytest.mark.blender
async def test_fbx_contract_refuses_excluded_keywords_presets_and_paths_outside_the_directory(tmp_path):
    if not find_blender():
        pytest.skip("requires stock Blender")
    built = build_package(tmp_path, "unreal_5")
    package = tmp_path / "site"  # the package directory, where the FBX is written
    shutil.copytree(built, package)
    out = package
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    base = request(package, "unreal", out_dir=str(out))
    before = snapshot(tmp_path)
    async with BlenderPool(1, startup_timeout=120, extra_env=BLENDER_ENV) as pool:
        for key, value in (("set", ["export_format=GLB"]), ("out", str(elsewhere / "x.fbx")), ("path", str(elsewhere / "x.fbx")), ("filepath", "x"),
                           ("batch", True), ("collection", "Export"), ("objects", ["Prop"]), ("draco", True), ("no_anim", False),
                           ("path_mode", "ABSOLUTE"), ("embed_textures", True), ("use_selection", False), ("bake_anim", True)):
            with pytest.raises(BlenderOpError, match="does not accept") as ei:
                await pool.call("export_fbx", **{**base, key: value})
            assert ei.value.cls == "ValueError" and f"'{key}'" in ei.value.msg, key
        for preset in (*skills.EXCLUDED_PRESETS, "FBX", "", None):
            with pytest.raises(BlenderOpError, match="allowed: \\['unity', 'unreal', 'roblox'\\]"):
                await pool.call("export_fbx", **{**base, "preset": preset})
        for name in ("../escape", "a/b", "/abs", "x.fbx", "", "..", ".hidden", "a b", "n" * 65, None, 7):
            with pytest.raises(BlenderOpError) as ei:
                await pool.call("export_fbx", **{**base, "asset_name": name})
            assert ei.value.cls in ("ValueError", "TypeError"), name
        for out_dir in (str(tmp_path / "missing"), "", None, 5, str(package / f"{ASSET}.glb")):
            with pytest.raises(BlenderOpError, match="out_dir must be an existing directory"):
                await pool.call("export_fbx", **{**base, "out_dir": out_dir})
        # a symbolic link at the output path (even one pointing into the directory) and a directory in its place are refused
        (out / f"{ASSET}.fbx").symlink_to(elsewhere / "target.fbx")
        with pytest.raises(BlenderOpError, match="not a plain file path"):
            await pool.call("export_fbx", **base)
        (out / f"{ASSET}.fbx").unlink()
        (out / f"{ASSET}.fbx").mkdir()
        with pytest.raises(BlenderOpError, match="not a plain file path"):
            await pool.call("export_fbx", **base)
        (out / f"{ASSET}.fbx").rmdir()
        # input files that are not glTF, or do not exist
        for key, value in (("source", str(package / f"{ASSET}.usd")), ("source", str(tmp_path / "no.glb")), ("lods", [str(package / "manifest.json")]),
                           ("colliders", str(tmp_path / "no.glb")), ("textures_dir", str(tmp_path / "no-dir"))):
            with pytest.raises(BlenderOpError):
                await pool.call("export_fbx", **{**base, key: value})
        # an FBX whose references could not be relative (the maps lie outside the output directory) is refused, nothing written
        with pytest.raises(BlenderOpError, match="outside the output directory"):
            await pool.call("export_fbx", **{**base, "out_dir": str(elsewhere)})
        assert os.listdir(elsewhere) == []
        # the same operation through a directory symlink writes inside the directory it resolves to, nothing elsewhere
        link = tmp_path / "link"
        link.symlink_to(out, target_is_directory=True)
        r = await pool.call("export_fbx", **{**base, "out_dir": str(link)})
        assert Path(r["fbx"]["path"]) == out.resolve() / f"{ASSET}.fbx"
        # a request that fails after Blender started leaves the existing file as it was and no temporary file
        kept = fbx_export.sha256_file(out / f"{ASSET}.fbx")
        listing = sorted(os.listdir(out))
        (package / "textures" / "normal.png").write_bytes(b"not the bytes the GLB embeds")
        with pytest.raises(BlenderOpError, match="no file in textures_dir"):
            await pool.call("export_fbx", **base)
        assert sorted(os.listdir(out)) == listing and fbx_export.sha256_file(out / f"{ASSET}.fbx") == kept
    after = snapshot(tmp_path)
    changed = {k for k in after if k not in before or after[k] != before[k]}
    assert changed == {f"site/{ASSET}.fbx", "site/textures/normal.png", "link"}, changed  # the FBX, the texture this test overwrote, the symlink it made
    assert not any(p.suffix == ".fbx" for p in elsewhere.iterdir()), "nothing was written outside the directory"
    assert not any(p.name.startswith(".") for p in out.iterdir())


@pytest.mark.blender
def test_fbx_export_that_fails_its_comparison_removes_what_it_wrote(tmp_path):
    """Run inside Blender: the comparison is forced to fail after the file was written; a good file already there survives,
    and so does a file somebody else wrote into the directory while the export ran (the directory is usually the package)."""
    if not find_blender():
        pytest.skip("requires stock Blender")
    package = build_package(tmp_path, "unreal_5")
    out = tmp_path / "out"
    out.mkdir()
    adapter = Path(skills.__file__).parent
    script = tmp_path / "forced_failure.py"
    script.write_text(textwrap.dedent(f"""
        import importlib.util, json, os, sys
        import bpy
        def load(stem):
            spec = importlib.util.spec_from_file_location("_t_" + stem, {str(adapter)!r} + "/" + stem + ".py")
            mod = importlib.util.module_from_spec(spec); sys.modules[spec.name] = mod; spec.loader.exec_module(mod); return mod
        daemon = load("daemon")
        daemon.register_bpy_ops(bpy)
        fbx = daemon.fbx  # the module instance the operation runs
        req = {json.dumps(request(package, "roblox", out_dir=str(out)))}
        good = daemon.OPS["export_fbx"](req)
        kept = open(good["fbx"]["path"], "rb").read()
        real = fbx.compare
        def neighbour(name):  # another step writing into the same directory while the export runs
            with open(os.path.join({str(out)!r}, name), "w") as f:
                f.write("not the export's file")
        def failing(*a, **k):
            neighbour("neighbour_during_failure.txt")
            return {{**real(*a, **k), "ok": False, "checks": [{{"name": "forced", "ok": False, "expected": 1, "actual": 2}}]}}
        fbx.compare = failing
        try:
            daemon.OPS["export_fbx"](req)
            outcome = "no error"
        except fbx.FbxMismatch as e:
            outcome = "FbxMismatch: " + str(e)[:80]
        same = open(good["fbx"]["path"], "rb").read() == kept
        def passing(*a, **k):
            neighbour("neighbour_during_success.txt")
            return real(*a, **k)
        fbx.compare = passing
        again = daemon.OPS["export_fbx"](req)
        print("RESULT", json.dumps({{"outcome": outcome, "listing": sorted(os.listdir({str(out)!r})), "same": same, "again_ok": again["ok"]}}))
    """))
    import msgpack

    env = {**os.environ, "AGY3D_BLENDER_SKILLS": "", "AGY3D_MSGPACK_INIT": str(Path(msgpack.__file__).resolve())}
    proc = subprocess.run([find_blender(), "-b", "--factory-startup", "--python-exit-code", "3", "-P", str(script)], env=env, capture_output=True, text=True, timeout=180)
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")), None)
    assert line, proc.stdout[-2000:] + proc.stderr[-2000:]
    result = json.loads(line[7:])
    assert result["outcome"].startswith("FbxMismatch: ") and result["same"] and result["again_ok"], result
    # the export removed its own temporary files and nothing else: both neighbours are still there
    assert result["listing"] == [f"{ASSET}.fbx", "neighbour_during_failure.txt", "neighbour_during_success.txt"], result


def test_only_paths_carrying_the_exports_temporary_name_are_its_own():
    out = "/pkg"
    tmp = f"{out}/.Prop.1a2b.part.fbx"
    new = {tmp, f"{out}/.Prop.1a2b.part.fbm", f"{out}/.Prop.1a2b.part.fbm/albedo.png", f"{out}/manifest.json", f"{out}/Prop.fbx",
           f"{out}/.Prop.9z9z.part.fbx", f"{out}/textures/extra.png"}
    assert fbx_export.written_by_export(new, tmp, None) == {tmp, f"{out}/.Prop.1a2b.part.fbm", f"{out}/.Prop.1a2b.part.fbm/albedo.png"}
    private = f"{out}/.agy-fbx-1a2b"
    tmp = f"{private}/Prop.fbx"
    new = {private, tmp, f"{private}/Prop.fbm", f"{private}/Prop.fbm/albedo.png", f"{out}/.agy-fbx-1a2bX", f"{out}/Prop.fbx", f"{out}/manifest.json"}
    assert fbx_export.written_by_export(new, tmp, private) == {private, tmp, f"{private}/Prop.fbm", f"{private}/Prop.fbm/albedo.png"}


@pytest.mark.blender
async def test_fbx_two_materials_one_slot_each_and_the_roblox_limits_are_reported(tmp_path):
    if not find_blender():
        pytest.skip("requires stock Blender")
    package = build_package(tmp_path, "godot_4", "two_materials")
    async with BlenderPool(1, startup_timeout=120, extra_env=BLENDER_ENV) as pool:
        r = await pool.call("export_fbx", out_dir=str(package), **request(package, "unity"))
        assert r["comparison"]["ok"] and sorted(r["join"][0]["material_slots_after"]) == ["glow", "paint"], r["join"]
        assert r["join"][0]["parts"] >= 3 and r["join"][0]["tris"] == r["join"][0]["tris_after"]
        assert [m["materials"] for m in r["contents"]["render_meshes"]] == [["glow", "paint"]] * len(r["contents"]["render_meshes"])
        lost = set(r["maps"]["lost"])
        assert {"paint.roughness", "paint.metallic", "paint.occlusion"} <= lost
        print("\nmaps lost by the FBX:", sorted(lost), "| kept:", {m: [s for s, v in per.items() if v] for m, per in r["maps"]["fbx"].items()})
        # a mesh over Roblox's 20,000 triangles is reported, the file is still written (the limit is Roblox's, not the format's)
        import trimesh

        dense = tmp_path / "dense"
        dense.mkdir()
        trimesh.creation.icosphere(subdivisions=6, radius=0.5).export(dense / "Dense.glb")
        assert len(trimesh.load(dense / "Dense.glb", force="mesh").faces) == 81920
        out = dense / "out"
        out.mkdir()
        r = await pool.call("export_fbx", source=str(dense / "Dense.glb"), asset_name="Dense", preset="roblox", out_dir=str(out))
        (mesh,) = r["limits"]["roblox"]["meshes"]
        assert mesh["tris"] == 81920 and mesh["open_edges"] == 0 and r["limits"]["roblox"]["ok"] is False and r["comparison"]["ok"]
        print(f"dense roblox export: {r['seconds']}")


# ------------------------------------------------------------------------------------------------- the fake worker


@pytest.fixture
def files(tmp_path):
    for name in ("a.glb", "a_LOD1.glb", "a_colliders.glb", "a.usd"):
        (tmp_path / name).write_bytes(b"glTF")
    (tmp_path / "textures").mkdir()
    (tmp_path / "out").mkdir()
    return tmp_path


async def test_fake_worker_enforces_the_same_contract_and_never_pretends_to_export(files):
    base = {"source": str(files / "a.glb"), "lods": [str(files / "a_LOD1.glb")], "colliders": str(files / "a_colliders.glb"),
            "textures_dir": str(files / "textures"), "asset_name": ASSET, "out_dir": str(files / "out")}
    async with BlenderPool(1, fake=True) as pool:
        version = await pool.call("version")
        assert {"export_fbx", "verify_fbx"} <= set(version["ops"]) and version["writers"] == ["export_asset"] and "export_asset" not in version["primitives"]
        for preset in skills.EXPORT_PRESETS:
            r = await pool.call("export_fbx", preset=preset, **base)
            assert r["fake"] is True and r["ok"] is False and r["fbx"] == {"path": str(files.resolve() / "out" / f"{ASSET}.fbx"), "bytes": 0, "sha256": None}
            assert r["comparison"] == {"ok": False, "checks": []} and "fake worker" in r["verified"]
            assert [m["name"] for m in r["contents"]["render_meshes"]] == fbx_export.plan(preset, ASSET, 2, 1)["render"]
        assert os.listdir(files / "out") == [], "the fake worker writes nothing"
        for key, value in (("set", ["x=1"]), ("out", "/tmp/x.fbx"), ("batch", True), ("collection", "c"), ("objects", ["o"]), ("draco", True), ("no_anim", False)):
            with pytest.raises(BlenderOpError, match="does not accept") as ei:
                await pool.call("export_fbx", preset="unity", **base, **{key: value})
            assert ei.value.cls == "ValueError"
        for preset in ("usd", "gltf", "unity_skeletal", "unreal_skeletal", "roblox_gltf", "godot", "web"):
            with pytest.raises(BlenderOpError, match="allowed"):
                await pool.call("export_fbx", preset=preset, **base)
        for name in ("../x", "a/b", "x.fbx", ""):
            with pytest.raises(BlenderOpError):
                await pool.call("export_fbx", preset="unity", **{**base, "asset_name": name})
        with pytest.raises(BlenderOpError):
            await pool.call("export_fbx", preset="unity", **{**base, "source": str(files / "a.usd")})
        (files / "out" / f"{ASSET}.fbx").symlink_to(files / "elsewhere.fbx")
        with pytest.raises(BlenderOpError, match="not a plain file path"):
            await pool.call("export_fbx", preset="unity", **base)
        v = await pool.call("verify_fbx", preset="unity", fbx=str(files / "x.fbx") if (files / "x.fbx").write_bytes(b"FBX") else "", **{k: base[k] for k in ("source", "lods", "colliders", "textures_dir", "asset_name")})
        assert v["fake"] is True and v["ok"] is False
        with pytest.raises(BlenderOpError, match="does not accept"):
            await pool.call("verify_fbx", preset="unity", fbx=str(files / "x.fbx"), **base)


@pytest.mark.blender
async def test_fake_worker_result_has_the_real_results_keys(tmp_path):
    if not find_blender():
        pytest.skip("requires stock Blender")
    package = build_package(tmp_path, "unreal_5")
    out = package
    async with BlenderPool(1, fake=True) as fake, BlenderPool(1, startup_timeout=120, extra_env=BLENDER_ENV) as real:
        f = await fake.call("export_fbx", out_dir=str(out), **request(package, "unreal"))
        r = await real.call("export_fbx", out_dir=str(out), **request(package, "unreal"))
        assert set(r) == set(f) and set(r["fbx"]) == set(f["fbx"]) and set(r["contents"]) == set(f["contents"]) and set(r["textures"]) == set(f["textures"])
        assert set(r["comparison"]) == set(f["comparison"]) and set(r["maps"]) == set(f["maps"]) and set(r["script"]) == set(f["script"])
        fv = await fake.call("verify_fbx", fbx=r["fbx"]["path"], **request(package, "unreal"))
        rv = await real.call("verify_fbx", fbx=r["fbx"]["path"], **request(package, "unreal"))
        assert set(rv) == set(fv)


# ------------------------------------------------------------------------------------------------- offline: plan and header checks


def test_the_engine_plan_follows_each_engines_documented_naming():
    assert fbx_export.plan("unity", "Crate", 3, 4) == {
        "render": ["Crate_LOD0", "Crate_LOD1", "Crate_LOD2"], "hulls": [], "levels_written": 3,
        "omitted": [{"what": "collision hulls", "why": fbx_export.plan("unity", "Crate", 3, 4)["omitted"][0]["why"]}]}
    assert fbx_export.plan("unity", "Crate", 1, 0)["render"] == ["Crate"], "one level needs no LOD Group"
    ue = fbx_export.plan("unreal", "Crate", 3, 2)
    assert ue["render"] == ["Crate"] and ue["hulls"] == ["UCX_Crate_00", "UCX_Crate_01"] and ue["omitted"][0]["what"] == "LOD levels 1..2"
    rb = fbx_export.plan("roblox", "Crate", 2, 2)
    assert rb["render"] == ["Crate"] and rb["hulls"] == [] and [o["what"] for o in rb["omitted"]] == ["LOD levels 1..1", "collision hulls"]
    with pytest.raises(ValueError, match="at most 8"):
        fbx_export.plan("unity", "Crate", 9, 0)
    with pytest.raises(ValueError):
        fbx_export.plan("godot", "Crate", 1, 0)


def header(unit=100.0, rotation=(-90.0, 0.0, 0.0), scaling=(1.0, 1.0, 1.0), lo=(1.5, 0.0, -1.75), hi=(2.5, 1.0, -0.25), name="Crate"):
    return {"global": {"UpAxis": 1, "UpAxisSign": 1, "FrontAxis": 2, "FrontAxisSign": 1, "CoordAxis": 0, "CoordAxisSign": 1,
                       "UnitScaleFactor": unit, "OriginalUnitScaleFactor": unit},
            "models": {name: {"parent": None, "rotation_deg": list(rotation), "pre_rotation_deg": [0.0, 0.0, 0.0], "scaling": list(scaling),
                              "unsupported": [], "extent_min_m": list(lo), "extent_max_m": list(hi)}}}


def test_header_checks_pass_for_what_the_presets_promise_and_fail_with_numbers_otherwise():
    box = fbx_export.expected_extent_in_file_frame([1.5, 0.25, 0.0], [2.5, 1.75, 1.0])
    assert box == ([1.5, 0.0, -1.75], [2.5, 1.0, -0.25]), "Blender (x, y, z) -> file (x, z, -y)"
    ok = fbx_export.header_checks(header(), "unreal", {"Crate": box}, None, [])
    assert all(c["ok"] for c in ok)
    bad = {c["name"]: c for c in fbx_export.header_checks(header(unit=1.0, lo=(0.015, 0.0, -0.0175), hi=(0.025, 0.01, -0.0025)), "unreal", {"Crate": box}, None, [])}
    assert not bad["header.unit_scale_factor"]["ok"] and bad["header.unit_scale_factor"]["actual"] == 1.0 and not bad["extent_m.Crate"]["ok"]
    turn = {c["name"]: c for c in fbx_export.header_checks(header(rotation=(0.0, 0.0, 0.0)), "unreal", {"Crate": box}, None, [])}
    assert not turn["model.Crate.rotation_deg"]["ok"]
    assert all(c["ok"] for c in fbx_export.header_checks(header(rotation=(0.0, 0.0, 0.0)), "unity", {"Crate": box}, None, []))
    assert not {c["name"]: c for c in fbx_export.header_checks(header(rotation=(-90.0, 0.0, 0.0)), "unity", {"Crate": box}, None, [])}["model.Crate.rotation_deg"]["ok"]
    scaled = {c["name"]: c for c in fbx_export.header_checks(header(scaling=(100.0, 100.0, 100.0)), "roblox", {"Crate": box}, None, [])}
    assert not scaled["model.Crate.scaling"]["ok"]
    extra = {c["name"]: c for c in fbx_export.header_checks(header(name="Other"), "roblox", {"Crate": box}, None, [])}
    assert not extra["model.Crate.present"]["ok"] and extra["models.no_others"]["actual"] == ["Other"]


def test_importing_the_module_needs_no_blender():
    out = subprocess.run([sys.executable, "-c", "import antigravity3d.adapters.blender_5_3.fbx_export as m, sys; print('bpy' in sys.modules)"],
                         capture_output=True, text=True, env={**os.environ})
    assert out.stdout.strip() == "False", out.stderr


@pytest.mark.blender
@pytest.mark.parametrize("preset", ["unity", "unreal", "roblox"])
async def test_fbx_two_exports_of_one_package_differ_only_in_the_header_time_stamp(tmp_path, preset):
    """Measured, not assumed: Blender's FBX exporter writes the clock into the header and has no option to fix it, so the hash
    of an FBX is not reproducible. Nothing else may differ (the Roblox preset used to embed a random temporary directory)."""
    import asyncio

    if not find_blender():
        pytest.skip("requires stock Blender")
    package = build_package(tmp_path, ENGINE_OF[preset])
    async with BlenderPool(1, startup_timeout=120, extra_env=BLENDER_ENV) as pool:
        first = await pool.call("export_fbx", out_dir=str(package), **request(package, preset))
        a = Path(first["fbx"]["path"]).read_bytes()
        await asyncio.sleep(1.2)  # a different second
        second = await pool.call("export_fbx", out_dir=str(package), **request(package, preset))
        b = Path(second["fbx"]["path"]).read_bytes()
    assert first["reproducibility"]["byte_identical_across_runs"] is False and "CreationTimeStamp" in first["reproducibility"]["differs"][0]
    assert len(a) == len(b) and a != b
    differing = [i for i in range(len(a)) if a[i] != b[i]]
    stamp = a.index(b"CreationTimeStamp")
    assert all(stamp < i < stamp + 300 for i in differing), f"bytes outside the header time stamp differ: {differing[:10]}"
    assert len(differing) <= 6  # the Second and Millisecond fields (more, a minute or more apart)
