"""The FBX of a package in the product: one helper for pass C and the native export, the manifest, the validator.

Offline here: the helper's skip / failure contract, the FBX reader and the validator's checks (on hand-built FBX files),
the manifest record and what the FBX says against the Roblox surface record, the step status that distinguishes a failed
output from a failed check, and the Roblox batch split. Real Blender (marked ``blender``; CI's ``stock-engines`` job):
the helper on real packages of the three engines, Unreal's DirectX normal map, the prop DAG, and the native export in a
verified and in a preview configuration. Nothing here opens Unity, Unreal Engine or Roblox Studio: the claim is
"re-imported in Blender and equal to the package's GLB files".
"""

from __future__ import annotations

import hashlib
import json
import shutil
import struct
import sys
from pathlib import Path
from typing import Any

import pytest

from antigravity3d.adapters.blender_5_3 import fbx_package
from antigravity3d.adapters.blender_5_3.pool import BlenderOpError, BlenderPool, WorkerError, find_blender
from antigravity3d.kernel import fbx_read
from antigravity3d.kernel.package import drop_fbx, fbx_contradictions, fbx_failed_record, fbx_inputs, fbx_record, record_fbx, split_batch
from antigravity3d.kernel.validator import validate_package
from antigravity3d.tactics.pyramid import PyramidResult, StepResult

sys.path.insert(0, str(Path(__file__).parent))
from test_blender_verification_steps import REQUIRED, Stub, box, run_pyramid  # noqa: E402
from test_fbx_export import ASSET, BLENDER_ENV, build_package, request  # noqa: E402

from antigravity3d.ir import MaterialIR, SceneIR  # noqa: E402

needs_blender = pytest.mark.skipif(not find_blender(), reason="no Blender binary ($BLENDER_BIN or PATH)")


# ------------------------------------------------------------------------------------------------- a hand-built binary FBX


def _prop(value: Any) -> bytes:
    if isinstance(value, bytes):
        return b"S" + struct.pack("<I", len(value)) + value
    if isinstance(value, int):
        return b"I" + struct.pack("<i", value)
    raise TypeError(value)


def _node(name: bytes, props: list[Any], children: list[Any], at: int) -> bytes:
    """One node record at file offset ``at``; ``children`` are functions of their own offset (record ends are absolute)."""
    body = b"".join(_prop(p) for p in props)
    cursor = at + 13 + len(name) + len(body)
    kids = b""
    for child in children:
        piece = child(cursor)
        kids += piece
        cursor += len(piece)
    if children:
        kids += b"\x00" * 13
    end = at + 13 + len(name) + len(body) + len(kids)
    return struct.pack("<IIIB", end, len(props), len(body), len(name)) + name + body + kids


def write_fbx(path: Path, videos: list[tuple[str, str | None, bytes]]) -> None:
    """A binary FBX 7400 with only an ``Objects`` node holding one ``Video`` per (name, relative file name, embedded bytes)."""
    def video(name: str, rel: str | None, content: bytes):
        def make(at: int) -> bytes:
            kids = []
            if rel is not None:
                kids.append(lambda a, rel=rel: _node(b"RelativeFilename", [rel.encode()], [], a))
            if content:
                kids.append(lambda a, content=content: _node(b"Content", [content], [], a))
            return _node(b"Video", [7, name.encode() + b"\x00\x01Video", b"Clip"], kids, at)
        return make

    head = b"Kaydara FBX Binary  \x00\x1a\x00" + struct.pack("<I", 7400)
    objects = _node(b"Objects", [], [video(*v) for v in videos], len(head))
    path.write_bytes(head + objects + b"\x00" * 13)


def test_the_reader_lists_texture_references_and_embedded_bytes(tmp_path):
    write_fbx(tmp_path / "a.fbx", [("albedo", "textures/albedo.png", b""), ("normal", "Prop.fbm/normal.png", b"x" * 40)])
    assert fbx_read.texture_references(tmp_path / "a.fbx") == [
        {"name": "albedo", "relative": "textures/albedo.png", "filename": None, "embedded_bytes": 0},
        {"name": "normal", "relative": "Prop.fbm/normal.png", "filename": None, "embedded_bytes": 40}]
    (tmp_path / "text.fbx").write_text("; FBX 7.4.0 project file\n")
    with pytest.raises(fbx_read.FbxFormatError, match="not a binary FBX"):
        fbx_read.texture_references(tmp_path / "text.fbx")
    data = (tmp_path / "a.fbx").read_bytes()
    (tmp_path / "cut.fbx").write_bytes(data[: len(data) // 2])
    with pytest.raises((fbx_read.FbxFormatError, struct.error)):
        fbx_read.texture_references(tmp_path / "cut.fbx")


def test_the_reader_refuses_a_file_nested_deeper_than_any_exporter_writes(tmp_path):
    """The validator opens packages from disk: a file of nodes inside nodes must end in the reader's own error (which
    ``validate_package`` reports as ``pkg:fbx_not_binary``), not in the interpreter's recursion limit."""
    def nest(k: int):
        return lambda at: _node(b"N", [], [nest(k - 1)] if k else [], at)

    head = b"Kaydara FBX Binary  \x00\x1a\x00" + struct.pack("<I", 7400)
    shallow = head + nest(fbx_read.MAX_DEPTH)(len(head)) + b"\x00" * 13
    assert fbx_read.read_nodes(shallow)[0] == 7400
    deep = head + nest(fbx_read.MAX_DEPTH + 40)(len(head)) + b"\x00" * 13
    with pytest.raises(fbx_read.FbxFormatError, match="nested too deeply"):
        fbx_read.read_nodes(deep)


# ------------------------------------------------------------------------------------------------- the validator on a package with an FBX


def real_package(tmp_path: Path, engine: str) -> tuple[Path, dict[str, Any]]:
    package = build_package(tmp_path, engine)
    return package, json.loads((package / "manifest.json").read_text())


def fake_written_result(manifest: dict[str, Any], preset: str, refs: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    """The worker's result shape for an FBX at ``<asset>.fbx``, with the given texture references (no Blender needed)."""
    return {"ok": True, "fake": False, "preset": preset, "asset_name": ASSET, "fbx": {"path": f"/somewhere/{ASSET}.fbx"}, "blender": "5.3.0",
            "script": {"name": "export_asset.py", "repository": "r", "commit": "c", "sha256": "d", "source": "vendored"},
            "sources": {"levels": [{"file": "/x/Prop.glb", "tris": 1}], "collision_hulls": None},
            "contents": {"render_meshes": [{"name": ASSET}], "collision_hulls": [], "omitted": []}, "join": [],
            "textures": {"mode": mode, "embedded_bytes": 0, "references": refs, "substituted": []},
            "header": {"version": 7400, "UpAxis": 1, "UpAxisSign": 1, "FrontAxis": 2, "FrontAxisSign": 1, "CoordAxis": 0, "CoordAxisSign": 1,
                       "UnitScaleFactor": 100.0, "OriginalUnitScaleFactor": 100.0, "models": {}},
            "comparison": {"ok": True, "checks": [{"name": "x", "ok": True}]}, "maps": {"source": {}, "fbx": {}, "lost": []}, "limits": None,
            "reproducibility": {"byte_identical_across_runs": False}, "verified": "v", "not_verified": ["n"]}


def issues_of(package: Path) -> dict[str, str]:
    return {i.code: i.detail for i in validate_package(package) if i.severity == "critical"}


def with_fbx(package: Path, manifest: dict[str, Any], preset: str, videos: list[tuple[str, str | None, bytes]], mode: str = "relative_reference") -> dict[str, Any]:
    write_fbx(package / f"{ASSET}.fbx", videos)
    refs = [{"slot": "DiffuseColor", "relative": rel, "sha256": None} for _, rel, _c in videos]
    record_fbx(manifest, package, fake_written_result(manifest, preset, refs, mode))
    (package / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return manifest


def test_a_package_without_an_fbx_is_as_valid_as_before_and_one_with_a_good_fbx_passes(tmp_path):
    package, manifest = real_package(tmp_path, "unreal_5")
    assert issues_of(package) == {}
    sha = manifest["file_hashes"]["textures/albedo.png"]["sha256"]
    manifest = with_fbx(package, manifest, "unreal", [("albedo", "textures/albedo.png", b"")])
    manifest["fbx"]["textures"]["references"][0]["sha256"] = sha
    (package / "manifest.json").write_text(json.dumps(manifest, indent=1))
    assert issues_of(package) == {}
    assert manifest["file_hashes"][f"{ASSET}.fbx"]["bytes"] == (package / f"{ASSET}.fbx").stat().st_size and f"{ASSET}.fbx" in manifest["files"]


@pytest.mark.parametrize("rel,code", [
    ("/abs/textures/albedo.png", "pkg:fbx_texture_not_relative"),
    ("C:\\build\\albedo.png", "pkg:fbx_texture_not_relative"),
    ("../elsewhere.png", "pkg:fbx_texture_outside"),
    ("textures/gone.png", "pkg:fbx_texture_missing"),
])
def test_an_fbx_texture_reference_must_be_relative_and_resolve_inside_the_package(tmp_path, rel, code):
    package, manifest = real_package(tmp_path, "unity_urp")
    with_fbx(package, manifest, "unity", [("albedo", rel, b"")])
    assert code in issues_of(package), issues_of(package)


def test_an_fbx_that_is_not_binary_or_not_the_recorded_file_fails_validation(tmp_path):
    package, manifest = real_package(tmp_path, "unreal_5")
    with_fbx(package, manifest, "unreal", [("albedo", "textures/albedo.png", b"")])
    (package / f"{ASSET}.fbx").write_text("; FBX 7.4.0 project file\n")  # ASCII FBX: not what the record describes, and its hash moved
    found = issues_of(package)
    assert {"pkg:fbx_not_binary", "pkg:hash_mismatch"} <= set(found), found


def test_an_fbx_record_must_match_the_engine_and_the_listing(tmp_path):
    package, manifest = real_package(tmp_path, "unreal_5")
    with_fbx(package, manifest, "unity", [("albedo", "textures/albedo.png", b"")])  # a Unity-preset FBX in an Unreal package
    assert "pkg:fbx_preset" in issues_of(package)
    manifest["fbx"]["preset"], manifest["fbx"]["comparison"]["ok"] = "unreal", False
    (package / "manifest.json").write_text(json.dumps(manifest))
    assert "pkg:fbx_comparison" in issues_of(package)
    manifest["fbx"]["comparison"]["ok"] = True
    del manifest["fbx"]
    (package / "manifest.json").write_text(json.dumps(manifest))
    assert "pkg:fbx_record" in issues_of(package)  # a listed FBX with no record


def test_an_embedded_fbx_must_carry_its_bytes(tmp_path):
    package, manifest = real_package(tmp_path, "roblox_r15")
    with_fbx(package, manifest, "roblox", [("albedo", "Prop.fbm/albedo.png", b"")], mode="embedded")
    assert "pkg:fbx_texture_not_embedded" in issues_of(package)
    write_fbx(package / f"{ASSET}.fbx", [("albedo", "Prop.fbm/albedo.png", b"x" * 10)])
    manifest["file_hashes"][f"{ASSET}.fbx"]["sha256"] = hashlib.sha256((package / f"{ASSET}.fbx").read_bytes()).hexdigest()
    (package / "manifest.json").write_text(json.dumps(manifest))
    assert not {c for c in issues_of(package) if c.startswith("pkg:fbx")}


def test_a_failed_fbx_is_recorded_without_a_file_and_the_package_stays_valid(tmp_path):
    package, manifest = real_package(tmp_path, "unreal_5")
    msg = 'unreal FBX does not match its sources; failed checks: [{"name": "triangles.Prop", "expected": 636, "actual": 600, "ok": false}]'
    manifest["fbx"] = fbx_failed_record(manifest, "FbxMismatch", msg)
    assert manifest["fbx"]["status"] == "failed" and manifest["fbx"]["failed_checks"][0]["name"] == "triangles.Prop" and "USD and GLB" in manifest["fbx"]["statement"]
    (package / "manifest.json").write_text(json.dumps(manifest))
    assert issues_of(package) == {}
    with_fbx(package, json.loads((package / "manifest.json").read_text()), "unreal", [("albedo", "textures/albedo.png", b"")])
    m = json.loads((package / "manifest.json").read_text())
    m["fbx"] = fbx_failed_record(m, "X", "y")  # the record says failed, a file is listed
    (package / "manifest.json").write_text(json.dumps(m))
    assert "pkg:fbx_record" in issues_of(package)
    drop_fbx({**m, "fbx": {"file": f"{ASSET}.fbx"}}, package, "PackageValidation", "why")  # removes the file again
    assert not (package / f"{ASSET}.fbx").exists()


def test_the_manifest_record_has_no_timings_or_machine_paths(tmp_path):
    package, manifest = real_package(tmp_path, "unreal_5")
    write_fbx(package / f"{ASSET}.fbx", [("albedo", "textures/albedo.png", b"")])
    result = fake_written_result(manifest, "unreal", [{"slot": "DiffuseColor", "relative": "textures/albedo.png", "sha256": "s"}], "relative_reference")
    result["seconds"] = {"total": 1.0}
    record_fbx(manifest, package, result)
    text = json.dumps(manifest["fbx"])
    assert "seconds" not in text and "/somewhere" not in text and "/x/" not in text and manifest["fbx"]["script"].get("source") is None
    assert manifest["fbx"]["status"] == "written" and manifest["fbx"]["reproducibility"]["byte_identical_across_runs"] is False
    assert manifest["fbx"]["file"] == f"{ASSET}.fbx" and manifest["fbx"]["sources"]["levels"][0]["file"] == "Prop.glb"


# ------------------------------------------------------------------------------------------------- inputs, Unreal's normal map, Roblox's record


def test_the_inputs_name_the_packages_files_and_unreals_directx_normal_map(tmp_path):
    package, manifest = real_package(tmp_path, "unreal_5")
    inputs = fbx_inputs(manifest, package)
    assert inputs["preset"] == "unreal" and inputs["asset_name"] == ASSET and inputs["source"] == str(package / f"{ASSET}.glb")
    assert inputs["lods"] == [str(package / f) for f in manifest["files"] if "_LOD" in f] and inputs["colliders"] == str(package / f"{ASSET}_colliders.glb")
    assert inputs["texture_map"] == {"normal.png": "normal_directx.png"} and inputs["textures_dir"] == str(package / "textures")
    unity, um = real_package(tmp_path / "u", "unity_urp")
    assert fbx_inputs(um, unity)["texture_map"] == {} and fbx_inputs(um, unity)["preset"] == "unity"
    godot, gm = real_package(tmp_path / "g", "godot_4")
    assert fbx_inputs(gm, godot) is None and "Godot imports the glTF" in fbx_package.eligible(gm)


def test_the_fbx_and_the_roblox_surface_record_must_not_contradict_each_other(tmp_path):
    package, manifest = real_package(tmp_path, "roblox_r15")
    record = manifest["roblox_surface_appearance"]
    (material,) = record["materials"].values()
    color = manifest["file_hashes"][material["ColorMap"]]["sha256"]
    normal = manifest["file_hashes"][material["NormalMap"]]["sha256"]
    sha = lambda slot, digest: {"slot": slot, "relative": "x", "sha256": digest}  # noqa: E731
    ok = {"preset": "roblox", "textures": {"references": [sha("DiffuseColor", color), sha("NormalMap", normal)]}}
    assert fbx_contradictions(manifest, package, ok) == []
    bad = {"preset": "roblox", "textures": {"references": [sha("DiffuseColor", normal), sha("NormalMap", normal)]}}
    assert "DiffuseColor image that is not one of the package's ColorMap files" in fbx_contradictions(manifest, package, bad)[0]
    missing = {"preset": "roblox", "textures": {"references": [sha("DiffuseColor", color)]}}
    assert "carries no NormalMap" in fbx_contradictions(manifest, package, missing)[0]
    assert fbx_contradictions(manifest, package, {"preset": "unity", "textures": {"references": []}}) == []  # only Roblox has the record
    # the metalness and roughness files the record ships are not in an FBX: said, not a contradiction
    assert material["MetalnessMap"] and material["RoughnessMap"]
    assert fbx_record(fake_written_result(manifest, "roblox", [], "embedded"))["maps"]["note"].startswith("an FBX carries the texture links")


# ------------------------------------------------------------------------------------------------- the helper's contract


class Dead:
    fake = False

    async def call(self, op: str, /, **args: Any) -> Any:
        raise WorkerError("died", "connection lost")


async def test_the_helper_skips_with_its_reason_and_never_pretends(tmp_path):
    package, manifest = real_package(tmp_path, "unreal_5")
    for pool, why in ((None, "no Blender pool"), (BlenderPool(1, fake=True), "fake")):
        status, detail, result = await fbx_package.export_package_fbx(pool, package, manifest)
        assert status == "skipped" and why in detail and result == {}
    marked = Stub({"ok": True, "fake": True})
    assert (await fbx_package.export_package_fbx(marked, package, manifest))[0] == "skipped" and marked.calls[0][0] == "export_fbx"
    notok = Stub({"ok": False})
    assert (await fbx_package.export_package_fbx(notok, package, manifest))[0] == "skipped"
    old = Stub(error=BlenderOpError("LookupError", "unknown op 'export_fbx'"))
    status, detail, _ = await fbx_package.export_package_fbx(old, package, manifest)
    assert status == "skipped" and "cannot export FBX" in detail
    godot, gm = real_package(tmp_path / "g", "godot_4")
    stub = Stub({"ok": True})
    status, detail, _ = await fbx_package.export_package_fbx(stub, godot, gm)
    assert status == "skipped" and "Godot" in detail and stub.calls == []
    assert not list(package.glob("*.fbx"))


async def test_a_worker_error_or_a_dead_worker_is_a_failed_output_with_a_record(tmp_path):
    package, manifest = real_package(tmp_path, "unreal_5")
    mismatch = BlenderOpError("FbxMismatch", 'unreal FBX does not match its sources; failed checks: [{"name": "extent_m.Prop", "ok": false}]')
    status, detail, record = await fbx_package.export_package_fbx(Stub(error=mismatch), package, manifest)
    assert status == "failed" and "FbxMismatch" in detail and record["status"] == "failed" and record["failed_checks"] == [{"name": "extent_m.Prop", "ok": False}]
    status, detail, record = await fbx_package.export_package_fbx(Dead(), package, manifest)
    assert status == "failed" and record["error"] == "WorkerError" and "connection lost" in record["reason"] and record["preset"] == "unreal"
    status, _, record = await fbx_package.export_package_fbx(Stub({"ok": True, "unexpected": "shape"}), package, manifest)
    assert status == "failed" and record["status"] == "failed"  # an answer that is not an FBX result is a failure, not a pass


# ------------------------------------------------------------------------------------------------- statuses


def test_a_failed_output_reads_as_a_failure_but_blocks_nothing():
    base = [StepResult(n, "passed") for n in REQUIRED]
    kw = dict(asset="x", tier1_ok=True, tier15=5.0, live_lookdev=True, live_parity=True, converged={"A": True, "B": True})
    skipped = PyramidResult(steps=[*base, StepResult("export_fbx", "skipped", "no Blender pool attached")], **kw)
    assert skipped.verified and skipped.ok
    out = PyramidResult(steps=[*base, StepResult("export_fbx", "output_failed", "FbxMismatch: x")], **kw)
    assert out.verified and out.ok and not out.failed and out.failed_outputs == [("export_fbx", "FbxMismatch: x")]
    assert out.to_dict()["failed_outputs"] == [{"step": "export_fbx", "reason": "FbxMismatch: x"}] and out.skipped == []
    assert "failed_outputs" not in skipped.to_dict(), "a run without a failed output writes the keys it always wrote"
    blocking = PyramidResult(steps=[*base, StepResult("blender_asset_qa", "failed", "x")], **kw)
    assert not blocking.ok and not blocking.verified


@pytest.mark.parametrize("pool", [None, BlenderPool(1, fake=True)])
async def test_the_prop_pipeline_skips_the_fbx_without_a_real_worker(tmp_path, pool):
    scene = SceneIR(asset_name="Crate", materials=[MaterialIR(id="m0")], nodes=[box("body", (1, 1, 1), (0, 0, 0.5))])
    res = await run_pyramid(tmp_path, scene, pool)
    step = next(s for s in res.steps if s.name == "export_fbx")
    assert step.status == "skipped" and step.detail and not res.failed_outputs and res.ok
    assert not list((tmp_path / "wd" / "package").glob("*.fbx")) and "fbx" not in res.objects


async def test_godot_gets_no_fbx(tmp_path):
    scene = SceneIR(asset_name="Crate", materials=[MaterialIR(id="m0")], nodes=[box("body", (1, 1, 1), (0, 0, 0.5))])
    stub = Stub({"ok": True})
    res = await run_pyramid(tmp_path, scene, stub, engine="godot_4")
    step = next(s for s in res.steps if s.name == "export_fbx")
    assert step.status == "skipped" and "Godot" in step.detail and not [c for c in stub.calls if c[0] == "export_fbx"]


# ------------------------------------------------------------------------------------------------- Roblox: what ships is under the limit


def test_a_materials_parts_are_split_into_meshes_at_the_limit_and_kept_whole():
    tris = {f"p{i}": 880 for i in range(40)}
    chunks = split_batch(list(tris), tris, 20_000)
    assert [len(c) for c in chunks] == [22, 18] and all(sum(tris[n] for n in c) <= 20_000 for c in chunks)
    assert [n for c in chunks for n in c] == list(tris), "order kept, every part exactly once"
    assert split_batch(list(tris), tris, None) == [list(tris)], "an engine without a per-mesh limit keeps one mesh per material"
    assert split_batch(["big", "a"], {"big": 25_000, "a": 10}, 20_000) == [["big"], ["a"]], "a part over the limit alone gets a mesh of its own (the authored-part rule rejects it)"
    assert split_batch([], {}, 20_000) == []


# ------------------------------------------------------------------------------------------------- real Blender: the helper on packages


@pytest.fixture
async def blender_pool():
    async with BlenderPool(1, startup_timeout=120, extra_env=BLENDER_ENV) as pool:
        yield pool


@pytest.mark.blender
@needs_blender
@pytest.mark.parametrize("engine", ["unity_urp", "unreal_5", "roblox_r15"])
async def test_the_helper_writes_the_fbx_into_the_package_and_the_manifest_and_validator_agree(tmp_path, blender_pool, engine):
    package, manifest = real_package(tmp_path, engine)
    before = sorted(p.name for p in package.iterdir())
    status, detail, result = await fbx_package.export_package_fbx(blender_pool, package, manifest)
    assert status == "passed", detail
    assert "not opened in Unity, Unreal Engine or Roblox Studio" in detail and "re-imported in Blender" in detail
    assert sorted(p.name for p in package.iterdir()) == sorted([*before, f"{ASSET}.fbx"]), "exactly one new file, nothing copied"
    fbx_package.record(manifest, package, result)
    (package / "manifest.json").write_text(json.dumps(manifest, indent=1))
    rec = manifest["fbx"]
    assert rec["status"] == "written" and rec["file"] == f"{ASSET}.fbx" and rec["comparison"]["ok"] and not rec["comparison"]["failed"]
    assert manifest["file_hashes"][f"{ASSET}.fbx"]["sha256"] == hashlib.sha256((package / f"{ASSET}.fbx").read_bytes()).hexdigest()
    assert rec["blender"].startswith("5.3") and rec["script"]["commit"] == "9e7e04a603c125a0d17447ab97112fa70a865067" and "source" not in rec["script"]
    assert rec["header"]["UnitScaleFactor"] == 100.0 and all(m["scaling"] == [1.0, 1.0, 1.0] for m in rec["header"]["models"].values())
    assert "not opened in Unity" in rec["verified"] or "NOT been opened" in rec["verified"]
    assert issues_of(package) == {}, issues_of(package)  # the validator re-opens the FBX with its own reader
    refs = fbx_read.texture_references(package / f"{ASSET}.fbx")
    if engine == "roblox_r15":
        assert rec["textures"]["mode"] == "embedded" and all(r["embedded_bytes"] > 0 for r in refs) and rec["limits"]["roblox"]["ok"]
        record = manifest["roblox_surface_appearance"]["materials"]["paint"]
        embedded = {r["slot"]: r["sha256"] for r in rec["textures"]["references"]}
        assert embedded["DiffuseColor"] == manifest["file_hashes"][record["ColorMap"]]["sha256"]
        assert embedded["NormalMap"] == manifest["file_hashes"][record["NormalMap"]]["sha256"]
        assert max(r["embedded_bytes"] for r in refs) == manifest["file_hashes"][record["ColorMap"]]["bytes"], "it embeds what the package ships, 1024 px maps"
        assert "paint.roughness" in rec["maps"]["lost"] and "paint.metallic" in rec["maps"]["lost"], "the separate metalness and roughness files are not in an FBX"
    else:
        assert rec["textures"]["mode"] == "relative_reference" and all(not Path(r["relative"]).is_absolute() and r["embedded_bytes"] == 0 for r in refs)
    if engine == "unreal_5":  # the map Unreal reads (DirectX), not the GLB's +Y one
        assert sorted(r["relative"] for r in refs) == ["textures/albedo.png", "textures/normal_directx.png"]
        assert rec["textures"]["substituted"] == [{"file": "normal_directx.png", "instead_of": "normal.png"}]
        directx = manifest["file_hashes"]["textures/normal_directx.png"]["sha256"]
        assert {r["slot"]: r["sha256"] for r in rec["textures"]["references"]}["NormalMap"] == directx != manifest["file_hashes"]["textures/normal.png"]["sha256"]
        assert "textures.engine_native_map_referenced" in {c["name"] for c in result["comparison"]["checks"]} and all(c["ok"] for c in result["comparison"]["checks"])
        assert rec["contents"]["collision_hulls"] == [f"UCX_{ASSET}_{i:02d}" for i in range(len(manifest["colliders"]))]
    if engine == "unity_urp":
        assert [m["name"] for m in rec["contents"]["render_meshes"]] == [f"{ASSET}_LOD{i}" for i in range(manifest["lod_levels"])]
        assert sorted(r["relative"] for r in refs) == ["textures/albedo.png", "textures/normal.png"]
    # the package is moved, with its FBX: the validator and the worker still resolve every reference
    moved = tmp_path / "moved"
    shutil.copytree(package, moved)
    shutil.rmtree(package)
    assert issues_of(moved) == {}
    again = await blender_pool.call("verify_fbx", fbx=str(moved / f"{ASSET}.fbx"), **request(moved, rec["preset"], **({"texture_map": fbx_inputs(manifest, moved)["texture_map"]})))
    assert again["comparison"]["ok"]


@pytest.mark.blender
@needs_blender
async def test_unreals_fbx_fails_when_it_references_the_opengl_normal_map(tmp_path, blender_pool):
    """The proof that the substitution is checked, not only done: without ``texture_map`` the same FBX references the GLB's
    normal.png, and verifying it against the DirectX map the package ships for Unreal fails on that slot."""
    package, manifest = real_package(tmp_path, "unreal_5")
    plain = {k: v for k, v in fbx_inputs(manifest, package).items() if k != "texture_map"}
    wrong = await blender_pool.call("export_fbx", **plain)
    assert {r["slot"]: r["relative"] for r in wrong["textures"]["references"]}["NormalMap"] == "textures/normal.png"
    with pytest.raises(BlenderOpError) as ei:
        await blender_pool.call("verify_fbx", fbx=wrong["fbx"]["path"], **{k: v for k, v in fbx_inputs(manifest, package).items() if k != "out_dir"})
    failed = {c["name"] for c in json.loads(ei.value.msg.split("failed checks: ", 1)[1])}
    assert "textures.engine_native_map_referenced" in failed


@pytest.mark.blender
@needs_blender
async def test_the_texture_map_is_as_narrow_as_the_rest_of_the_contract(tmp_path, blender_pool):
    package, manifest = real_package(tmp_path, "unreal_5")
    base = fbx_inputs(manifest, package)
    for bad in ({"normal.png": "../normal_directx.png"}, {"normal.png": "/etc/passwd"}, {"normal.png": "missing.png"}, {"normal.png": "manifest.json"},
                {"albedo.png": ".hidden.png"}, {"normal.png": 5}, ["normal.png"]):
        with pytest.raises(BlenderOpError, match="texture_map"):
            await blender_pool.call("export_fbx", **{**base, "texture_map": bad})
    with pytest.raises(BlenderOpError, match="relative references"):
        await blender_pool.call("export_fbx", **{**fbx_inputs(real_package(tmp_path / "r", "roblox_r15")[1], tmp_path / "r" / "roblox_r15"), "texture_map": {"normal.png": "normal.png"}})
    assert not list(package.glob("*.fbx"))


# ------------------------------------------------------------------------------------------------- real Blender: the prop pipeline and `agy3d prop`


class FailingFbx:
    """The real pool, except that the FBX export fails the way a comparison failure or a dying worker does."""

    def __init__(self, pool: BlenderPool, error: BaseException) -> None:
        self.pool, self.error = pool, error

    def __getattr__(self, name: str) -> Any:
        return getattr(self.pool, name)

    async def call(self, op: str, /, **args: Any) -> Any:
        if op == "export_fbx":
            raise self.error
        return await self.pool.call(op, **args)


async def prop_dag(out: Path, engine: str, pool: Any):
    from antigravity3d.cli import make_collaborators
    from antigravity3d.dags.prop_to_engine import PropToEngineDAG
    from antigravity3d.state import EngineBudget
    from antigravity3d.tactics.distill import FixLog

    plane, forge, author, _ = await make_collaborators(True, workdir=out / "agent")
    rep = await PropToEngineDAG(plane=plane, forge=forge, author=author, workdir=out, budget=EngineBudget(engine), pool=pool,
                                fixlog=FixLog.for_prop(out), max_rounds=4, require_verified=False).run("wooden crate")
    (manifest_path,) = sorted(out.glob("runs/*/package/manifest.json"))
    return rep, manifest_path.parent, json.loads(manifest_path.read_text())


def step_of(rep: Any, name: str) -> dict[str, Any]:
    return next(s for s in rep.extras["pyramid"]["steps"] if s["name"] == name)


@pytest.mark.blender
@needs_blender
@pytest.mark.parametrize("engine", ["unreal_5", "roblox_r15"])
async def test_the_prop_pipeline_ships_the_fbx_in_the_package_with_its_real_hash(tmp_path, blender_pool, engine):
    rep, package, manifest = await prop_dag(tmp_path, engine, blender_pool)
    assert rep.ok
    steps = [s["name"] for s in rep.extras["pyramid"]["steps"]]
    fbx = step_of(rep, "export_fbx")
    assert fbx["status"] == "passed" and "not opened in Unity, Unreal Engine or Roblox Studio" in fbx["detail"], fbx
    assert steps.index("export_fbx") > max(steps.index("web_runtime"), steps.index("blender_asset_qa"), steps.index("engine_parity")), "last: nothing else writes to the folder"
    assert manifest["fbx"]["status"] == "written" and manifest["fbx"]["file"] == "WoodenCrate.fbx"
    assert "WoodenCrate.fbx" in manifest["files"] and manifest["file_hashes"]["WoodenCrate.fbx"]["sha256"] == hashlib.sha256((package / "WoodenCrate.fbx").read_bytes()).hexdigest()
    assert {p.name for p in package.iterdir() if p.is_file()} <= {*manifest["files"], "manifest.json"}, "no unlisted file in the package folder"
    assert issues_of(package) == {}, issues_of(package)
    (evidence,) = [s for s in manifest["evidence"]["steps"] if s["name"] == "export_fbx"]
    assert evidence["status"] == "passed" and "re-imported in Blender" in evidence["detail"]
    normals = [r["relative"] for r in manifest["fbx"]["textures"]["references"] if r["slot"] == "NormalMap"]
    if engine == "unreal_5":  # every material's normal map is the one the package ships for Unreal (DirectX), not the GLB's +Y copy
        native = {t["path"] for t in manifest["textures"] if t["role"] == "normal"}
        assert normals and set(normals) <= native and all(n.endswith("_directx.png") for n in normals), (normals, native)
    else:
        assert manifest["fbx"]["textures"]["mode"] == "embedded" and manifest["fbx"]["textures"]["embedded_bytes"] > 0
    assert manifest["verified"] is False  # the offline run is never verified: the FBX does not change that


@pytest.mark.blender
@needs_blender
@pytest.mark.parametrize("error", [BlenderOpError("FbxMismatch", 'unreal FBX does not match its sources; failed checks: [{"name": "extent_m.Crate", "ok": false}]'),
                                   WorkerError("died", "connection lost")])
async def test_a_failed_fbx_ships_the_package_without_it_and_says_so(tmp_path, blender_pool, error):
    rep, package, manifest = await prop_dag(tmp_path, "unreal_5", FailingFbx(blender_pool, error))
    fbx = step_of(rep, "export_fbx")
    assert fbx["status"] == "output_failed" and "extent_m.Crate" in fbx["detail"] + json.dumps(manifest["fbx"]) or "connection lost" in fbx["detail"]
    assert rep.ok, "a failed output does not fail the run"
    assert [f["step"] for f in rep.extras["pyramid"]["failed_outputs"]] == ["export_fbx"]
    assert not list(package.glob("*.fbx")) and "WoodenCrate.fbx" not in manifest["files"] and "WoodenCrate.fbx" not in manifest["file_hashes"]
    assert manifest["fbx"]["status"] == "failed" and manifest["fbx"]["preset"] == "unreal" and "USD and GLB" in manifest["fbx"]["statement"]
    assert issues_of(package) == {}
    assert step_of(rep, "blender_asset_qa")["status"] == "passed" and step_of(rep, "engine_import")["status"] in ("passed", "skipped")
    (evidence,) = [s for s in manifest["evidence"]["steps"] if s["name"] == "export_fbx"]
    assert evidence["status"] == "output_failed"


@pytest.mark.blender
@needs_blender
def test_agy3d_prop_prints_the_fbx_step_like_the_others(tmp_path):
    import os
    import subprocess

    r = subprocess.run([sys.executable, "-m", "antigravity3d.cli", "prop", "wooden crate", "--offline", "--engine", "unreal_5", "--blender", find_blender(),
                        "--out", str(tmp_path / "o"), "--no-cache"], capture_output=True, text=True, timeout=900, env={**os.environ, "AGY3D_BLENDER_SKILLS": ""})
    assert r.returncode == 0, r.stdout[-1500:] + r.stderr[-1500:]
    steps = next(line for line in r.stdout.splitlines() if line.startswith("pyramid steps:"))
    assert "blender_asset_qa=passed" in steps and "export_fbx=passed" in steps, steps
    report = json.loads((tmp_path / "o" / "report.json").read_text())
    assert not report["extras"]["pyramid"]["failed_outputs"] if "extras" in report else True
    (manifest,) = (tmp_path / "o").glob("runs/*/package/manifest.json")
    assert json.loads(manifest.read_text())["fbx"]["status"] == "written"


# ------------------------------------------------------------------------------------------------- the native export (harness/tools.py)


async def native_export(tmp_path: Path, monkeypatch, pool: Any, *, preview: bool = False) -> tuple[str, dict[str, Any], Path]:
    """The native session's export_engine_package (engine import and parity simulated, as in test_harness_tools)."""
    from test_blender_verification_steps import export_crate
    from test_harness_tools import make_session

    env = make_session(tmp_path, pool=pool)
    if preview:  # the unverified preview mode: its own label, no parity (the engine import is simulated, as above)
        env.state.preview = True
    reply, manifest = await export_crate(env, monkeypatch)
    assert manifest is not None, reply
    return reply, manifest, env.sess().export_dir / "crate_pkg"


@pytest.mark.parametrize("preview", [False, True])
async def test_the_native_export_without_a_worker_skips_the_fbx_and_changes_nothing_else(tmp_path, monkeypatch, preview):
    reply, manifest, package = await native_export(tmp_path, monkeypatch, None, preview=preview)
    assert reply.startswith("EXPORTED") and "fbx skipped: no Blender pool attached" in reply
    assert "fbx" not in manifest and not list(package.glob("*.fbx")) and issues_of(package) == {}
    assert manifest.get("preview", False) is preview and (manifest["verified"] is False if preview else True)


@pytest.mark.parametrize("preview", [False, True])
async def test_a_failed_fbx_in_the_native_export_ships_the_package_without_it_and_prints_it(tmp_path, monkeypatch, preview):
    class OnlyTheFbxFails:  # a worker that has no qa_check (that check is skipped) and whose FBX export fails its comparison
        async def call(self, op: str, /, **args: Any) -> Any:
            if op == "export_fbx":
                raise BlenderOpError("FbxMismatch", 'unreal FBX does not match its sources; failed checks: [{"name": "bbox_m.Crate", "ok": false}]')
            raise BlenderOpError("LookupError", f"unknown op {op!r}")

    reply, manifest, package = await native_export(tmp_path, monkeypatch, OnlyTheFbxFails(), preview=preview)
    assert reply.startswith("EXPORTED") and "FBX FAILED (not shipped; the package is intact)" in reply and "bbox_m.Crate" in reply
    assert manifest["fbx"]["status"] == "failed" and manifest["fbx"]["failed_checks"] == [{"name": "bbox_m.Crate", "ok": False}]
    assert not list(package.glob("*.fbx")) and "Crate.fbx" not in manifest["files"] and issues_of(package) == {}
    assert manifest.get("preview", False) is preview and (manifest["verified"] is False if preview else True)


@pytest.mark.blender
@needs_blender
@pytest.mark.parametrize("preview", [False, True])
async def test_the_native_export_writes_the_fbx_in_verified_and_preview_sessions_alike(tmp_path, monkeypatch, blender_pool, preview):
    reply, manifest, package = await native_export(tmp_path, monkeypatch, blender_pool, preview=preview)
    assert reply.startswith("EXPORTED") and "fbx: Crate.fbx re-imported in Blender, not opened in an engine" in reply
    assert manifest["fbx"]["status"] == "written" and "Crate.fbx" in manifest["files"]
    assert manifest["file_hashes"]["Crate.fbx"]["sha256"] == hashlib.sha256((package / "Crate.fbx").read_bytes()).hexdigest()
    assert issues_of(package) == {}  # in the published folder, after the temporary directory was moved
    assert manifest.get("preview", False) is preview and (manifest["verified"] is False if preview else True), "the FBX never changes a package's state"
    assert manifest["fbx"]["textures"]["references"] == [], "this crate has no maps: nothing to reference (a package without a textures folder is exported too)"
