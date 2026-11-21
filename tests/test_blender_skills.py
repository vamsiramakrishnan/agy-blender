"""The vendored EL4CTEO/blender-skills scripts: provenance, digest pins, loader isolation, and the worker contract.

No Blender here: the scripts are never executed against bpy. Loading them for real is tested in CI
(tests/test_blender_pool.py, job stock-engines).
"""

import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

from antigravity3d.adapters.blender_5_3 import skills
from antigravity3d.adapters.blender_5_3.pool import BlenderOpError, BlenderPool


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def provenance() -> dict:
    return json.loads(skills.PROVENANCE.read_text())


def test_provenance_matches_the_vendored_files_and_the_pins():
    record = provenance()
    assert record["repository"] == skills.UPSTREAM_REPOSITORY
    assert record["commit"] == skills.UPSTREAM_COMMIT and record["release"] == skills.UPSTREAM_RELEASE
    pins = {skills.HELPER[0]: skills.HELPER[1], **{rel: digest for rel, digest in [*skills.PINNED_SCRIPTS.values(), *skills.PINNED_WRITERS.values()]}}
    assert set(record["files"]) == set(pins), "every executed file is recorded, and nothing else"
    for rel, entry in record["files"].items():
        path = skills.VENDOR_DIR / rel
        assert entry["upstream_path"] == rel, "vendored in upstream's own layout"
        assert entry["sha256"] == pins[rel] == sha256(path), rel
        assert entry["bytes"] == path.stat().st_size, rel
    lic = record["license"]
    assert (lic["file"], lic["sha256"]) == skills.LICENSE and lic["upstream_path"] == "LICENSE"
    assert sha256(skills.VENDOR_DIR / lic["file"]) == lic["sha256"] and lic["bytes"] == (skills.VENDOR_DIR / "LICENSE").stat().st_size
    assert (skills.VENDOR_DIR / "LICENSE").read_text().startswith("MIT License")
    vendored = sorted(p.relative_to(skills.VENDOR_DIR).as_posix() for p in skills.VENDOR_DIR.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    assert vendored == sorted([*pins, "LICENSE"]), "no unrecorded file in the vendored tree"


def test_the_writer_is_pinned_but_is_not_a_measurement():
    """export_asset writes a file: it is vendored and checked with the others, but inspect_usd can never name it."""
    assert set(skills.PINNED_WRITERS) == {"export_asset"} and not set(skills.PINNED_WRITERS) & set(skills.PINNED_SCRIPTS)
    assert not set(skills.PINNED_WRITERS) & set(skills.PRIMITIVE_KWARGS)
    rel, digest = skills.PINNED_WRITERS["export_asset"]
    assert rel == "skills/blender-export-pipelines/scripts/export_asset.py"
    assert digest == "58534892042e5c954f0875b3b41b45c2609b7a4ec5dc44f6c06922b06cbd6c88" == sha256(skills.VENDOR_DIR / rel)
    entry = provenance()["files"][rel]
    assert (entry["sha256"], entry["bytes"], entry["upstream_path"]) == (digest, (skills.VENDOR_DIR / rel).stat().st_size, rel)
    with pytest.raises(KeyError):
        skills.check_kwargs("export_asset", {"preset": "unity"})  # not reachable as a measurement


def test_the_export_contract_offers_three_static_fbx_presets_and_nothing_that_chooses_a_path_or_a_keyword(tmp_path):
    assert skills.EXPORT_PRESETS == ("unity", "unreal", "roblox")
    assert not set(skills.EXPORT_PRESETS) & set(skills.EXCLUDED_PRESETS)
    assert set(skills.EXPORT_PRESETS) | set(skills.EXCLUDED_PRESETS) == {"roblox", "roblox_gltf", "unity", "unity_skeletal", "unreal", "unreal_skeletal", "godot", "web", "gltf", "usd"}, "every upstream preset is either offered or excluded on purpose"
    assert not skills.EXPORT_REQUEST_KEYS & {"set", "out", "path", "batch", "collection", "objects", "draco", "no_anim", "filepath"}
    assert skills.VERIFY_REQUEST_KEYS == (skills.EXPORT_REQUEST_KEYS - {"out_dir"}) | {"fbx"}
    glb = tmp_path / "a.glb"
    glb.write_bytes(b"glTF")
    good = {"source": str(glb), "asset_name": "Crate_01", "preset": "unity", "out_dir": str(tmp_path)}
    checked = skills.check_export_request(good)
    assert checked["final"] == str(tmp_path.resolve() / "Crate_01.fbx") and checked["lods"] == [] and checked["colliders"] is None
    for bad in ({"set": ["a=1"]}, {"out": "/x.fbx"}, {"batch": True}, {"preset": "usd"}, {"preset": "unity_skeletal"}, {"asset_name": "../x"},
                {"asset_name": "a.b"}, {"asset_name": "a/b"}, {"asset_name": ""}, {"out_dir": str(tmp_path / "none")}, {"source": str(tmp_path / "none.glb")}):
        with pytest.raises((ValueError, FileNotFoundError, TypeError)):
            skills.check_export_request({**good, **bad})
    with pytest.raises(ValueError, match="at most 7"):
        skills.check_export_request({**good, "lods": [str(glb)] * 8})
    with pytest.raises(TypeError):
        skills.check_export_request(["unity"])


def test_every_primitive_has_a_contract_and_no_editing_keyword_is_allowed():
    assert set(skills.PRIMITIVE_KWARGS) == set(skills.PINNED_SCRIPTS)
    allowed = {k for keys in skills.PRIMITIVE_KWARGS.values() for k in keys}
    assert not allowed & {"fix", "set", "margin_px", "objects", "save", "no_modifiers"}
    assert skills.PRIMITIVE_KWARGS["qa_check"] == {"profile", "eps", "tol", "max_tris", "uv"}
    assert skills.PRIMITIVE_KWARGS["kit_check"] == {"grid", "pivot", "texture", "tol", "prefix"}
    assert skills.check_kwargs("texel_density", {"res": 1024}) == {"res": 1024} and skills.check_kwargs("mesh_stats", None) == {}
    with pytest.raises(ValueError, match=r"does not accept \['fix'\]"):
        skills.check_kwargs("kit_check", {"grid": 0.5, "fix": True})
    with pytest.raises(ValueError, match=r"\['margin_px', 'set'\]"):
        skills.check_kwargs("texel_density", {"set": 512, "margin_px": 4})
    with pytest.raises(KeyError):
        skills.check_kwargs("rig_check", {})
    with pytest.raises(TypeError):
        skills.check_kwargs("qa_check", ["profile"])


def test_the_vendored_tree_is_the_default_and_the_override_is_a_checkout(monkeypatch):
    monkeypatch.delenv(skills.OVERRIDE_ENV, raising=False)
    assert skills.source_root() == (skills.VENDOR_DIR, "vendored")
    monkeypatch.setenv(skills.OVERRIDE_ENV, "")  # empty means unset (CI clears it for the benchmark this way)
    assert skills.source_root() == (skills.VENDOR_DIR, "vendored")
    monkeypatch.setenv(skills.OVERRIDE_ENV, "/somewhere/blender-skills")
    assert skills.source_root() == (Path("/somewhere/blender-skills"), "checkout")


def test_daemon_finds_the_vendored_files_relative_to_its_own_file():
    """Blender runs daemon.py as a script and loads skills.py by path: the vendored tree must sit beside them."""
    daemon = Path(skills.__file__).with_name("daemon.py")
    assert daemon.is_file()
    spec = importlib.util.spec_from_file_location("_agy_skills_probe", daemon.with_name("skills.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.VENDOR_DIR == daemon.parent / "vendor" / "blender-skills"
    assert all((mod.VENDOR_DIR / rel).is_file() for rel, _ in [mod.HELPER, *mod.PINNED_SCRIPTS.values(), mod.LICENSE])
    assert "_bpyx" not in sys.modules and not any(name in sys.modules for name in mod.PINNED_SCRIPTS)  # nothing ran


def copy_vendored(dst: Path) -> Path:
    shutil.copytree(skills.VENDOR_DIR, dst / "blender-skills")
    return dst / "blender-skills"


@pytest.mark.parametrize("victim", ["mesh_stats", "qa_check", "kit_check", "texel_density", "export_asset", "helper"])
def test_a_tampered_vendored_file_is_refused_before_any_upstream_code_runs(tmp_path, monkeypatch, victim):
    root = copy_vendored(tmp_path)
    rel = skills.HELPER[0] if victim == "helper" else {**skills.PINNED_SCRIPTS, **skills.PINNED_WRITERS}[victim][0]
    with open(root / rel, "a") as f:
        f.write("\nraise AssertionError('unreviewed code executed')\n")
    # Untouched files would import bpy if executed: a stub that records the import proves nothing ran.
    executed = []
    stub = ModuleType("bpy")
    stub.__getattr__ = lambda name: executed.append(name)
    monkeypatch.setitem(sys.modules, "bpy", stub)
    path_before = sys.path.copy()
    with pytest.raises(ValueError, match="unexpected blender-skills source digest"):
        skills.load_reviewed(root)
    assert executed == [] and "_bpyx" not in sys.modules and sys.path == path_before


@pytest.mark.parametrize("victim", ["texel_density", "export_asset"])
def test_a_missing_vendored_file_fails_the_load(tmp_path, victim):
    root = copy_vendored(tmp_path)
    (root / {**skills.PINNED_SCRIPTS, **skills.PINNED_WRITERS}[victim][0]).unlink()
    with pytest.raises(FileNotFoundError):
        skills.load_reviewed(root)


def stub_blender(monkeypatch) -> None:
    """Just enough of bpy/bmesh/mathutils for the scripts' module-level imports; nothing is called."""
    for name in ("bpy", "bmesh", "mathutils", "mathutils.bvhtree", "mathutils.kdtree"):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    for attr in ("Vector", "Matrix"):
        setattr(sys.modules["mathutils"], attr, type(attr, (), {}))
    sys.modules["mathutils.bvhtree"].BVHTree = type("BVHTree", (), {})
    sys.modules["mathutils.kdtree"].KDTree = type("KDTree", (), {})


def test_loader_isolates_sys_path_and_the_helper_module(monkeypatch):
    stub_blender(monkeypatch)
    sentinel = ModuleType("_bpyx")
    monkeypatch.setitem(sys.modules, "_bpyx", sentinel)  # someone else's _bpyx must survive the load
    path_before = sys.path.copy()
    runs = skills.load_reviewed()
    assert sorted(runs) == ["export_asset", "kit_check", "mesh_stats", "qa_check", "texel_density"] and all(callable(r) for r in runs.values())
    assert sys.path == path_before, "the scripts' sys.path.insert(0, <their dir>) is undone"
    assert sys.modules["_bpyx"] is sentinel
    helpers = {r.__globals__["bx"] for r in runs.values()}
    assert len(helpers) == 1, "every script shares the one vendored helper"
    (helper,) = helpers
    assert helper is not sentinel and Path(helper.__file__) == skills.VENDOR_DIR / skills.HELPER[0]
    for name, run in runs.items():
        assert Path(run.__globals__["__file__"]) == skills.VENDOR_DIR / {**skills.PINNED_SCRIPTS, **skills.PINNED_WRITERS}[name][0]
    monkeypatch.delitem(sys.modules, "_bpyx")
    skills.load_reviewed()
    assert "_bpyx" not in sys.modules


# ---- the worker contract, on the fake worker (the real worker enforces the same function; see test_blender_pool)

FAKE_SHAPES = {
    "mesh_stats": ({"budget": "pc"}, {"ok", "objects", "total_tris", "total_verts", "draw_calls_est", "unique_materials", "textures",
                                      "texture_vram_mb_uncompressed", "bones", "kind", "budget"}),
    "qa_check": ({"profile": "roblox", "tol": 0.002}, {"ok", "pass", "errors", "warnings", "ground_z", "zfighting", "floating", "back_to_back",
                                                       "enclosed", "objects"}),
    "kit_check": ({"grid": 0.5, "pivot": "bottom-center", "texture": 1024}, {"ok", "pass", "grid_m", "pivot", "fixed", "pieces"}),
    "texel_density": ({"res": 1024}, {"ok", "res", "objects"}),
}


async def test_fake_worker_offers_every_primitive_with_the_real_result_shape():
    async with BlenderPool(1, fake=True) as pool:
        version = await pool.call("version")
        assert version["primitives"] == sorted(FAKE_SHAPES) and version["primitives_source"]["kind"] == "fake"
        assert version["primitives_error"] is None and "inspect_usd" in version["ops"]
        for name, (kwargs, keys) in FAKE_SHAPES.items():
            result = await pool.call("inspect_usd", path="/no/such.usd", primitive=name, kwargs=kwargs)
            assert result.pop("agy_inspect") == {"objects": ["FakeMesh"], "weld": None, "importer": "usd"}, name  # the worker names what it measures
            from_glb = await pool.call("inspect_usd", path="/no/such.glb", primitive=name, kwargs=kwargs)  # same contract for a GLB
            assert from_glb["agy_inspect"]["importer"] == "gltf" and set(from_glb) - {"agy_inspect", "fake"} == keys
            assert result.pop("fake") is True and set(result) == keys, name
            welded = await pool.call("inspect_usd", path="/no/such.usd", primitive=name, kwargs=kwargs, weld=True)
            assert welded["agy_inspect"]["weld"]["distance_m"] == 1e-6 and welded["agy_inspect"]["weld"]["fake"] is True
        with pytest.raises(BlenderOpError, match="weld must be true or false"):
            await pool.call("inspect_usd", path="x.usd", primitive="qa_check", weld=0.01)
        qa = await pool.call("inspect_usd", path="x.usd", primitive="qa_check", kwargs={})
        assert set(qa["objects"]["FakeMesh"]) >= {"islands", "boundary_edges", "duplicate_verts", "inside_out_islands", "scale", "uv_overlap_pct"}
        kit = await pool.call("inspect_usd", path="x.usd", primitive="kit_check", kwargs={"grid": 1.0, "pivot": "center"})
        assert (kit["grid_m"], kit["pivot"], kit["fixed"]) == (1.0, "center", False)
        rendered = await pool.call("render_usd", path="x.usd", width=8, height=8)
        assert "mesh_stats" not in rendered  # fake counts are never attached to a render as if measured


@pytest.mark.parametrize("primitive,kwargs,refused", [
    ("kit_check", {"fix": True}, "['fix']"),
    ("texel_density", {"res": 1024, "set": 512.0}, "['set']"),
    ("texel_density", {"margin_px": 8}, "['margin_px']"),
    ("qa_check", {"objects": ["Crate"]}, "['objects']"),
    ("mesh_stats", {"budget": "pc", "save": "/tmp/x.blend"}, "['save']"),
])
async def test_fake_worker_refuses_keywords_outside_the_contract(primitive, kwargs, refused):
    async with BlenderPool(1, fake=True) as pool:
        with pytest.raises(BlenderOpError) as ei:
            await pool.call("inspect_usd", path="x.usd", primitive=primitive, kwargs=kwargs)
        assert ei.value.cls == "ValueError" and refused in ei.value.msg and "allowed" in ei.value.msg
        with pytest.raises(BlenderOpError) as ei:
            await pool.call("inspect_usd", path="x.usd", primitive="unlisted")
        assert ei.value.cls == "KeyError"
        assert (await pool.call("ping"))["pong"]
