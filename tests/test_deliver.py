"""agy3d install: project detection, the install's file discipline, and the real-Godot proof with negative controls."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from antigravity3d.deliver import detect_project, install_package, uninstall
from antigravity3d.deliver import godot as G
from antigravity3d.deliver.glb import reference_images
from antigravity3d.deliver.install import RECORD, _install_godot, check_install, read_package

GODOT4 = 'config_version=5\n\n[application]\n\nconfig/name="t"\nconfig/features=PackedStringArray("4.7", "Forward Plus")\n'


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _tree(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): _sha(p) for p in sorted(root.rglob("*")) if p.is_file()}


# --------------------------------------------------------------------------- detection


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if rel.endswith("/"):
            p.mkdir(exist_ok=True)
        else:
            p.write_text(text)
    return root


@pytest.mark.parametrize("files, engine, version", [
    ({"project.godot": GODOT4}, "godot", "4.7"),
    ({"project.godot": 'config_version=5\n[application]\nconfig/name="t"\n'}, "godot", "4"),
    ({"project.godot": 'config_version=4\n'}, "godot", "3"),
    # project.godot decides over a .csproj (Godot C#)
    ({"project.godot": GODOT4, "Game.csproj": "<Project/>"}, "godot", "4.7"),
    ({"Assets/": "", "ProjectSettings/ProjectVersion.txt": "m_EditorVersion: 2022.3.10f1\n"}, "unity", "2022.3.10f1"),
    ({"Game.uproject": '{"EngineAssociation": "5.3"}'}, "unreal", "5.3"),
    ({"Game.uproject": '{"EngineAssociation": "{A1B2C3D4-0000-0000-0000-000000000000}"}'}, "unreal", None),
    ({"place.rbxl": "x"}, "roblox", None),
    ({"default.project.json": '{"name": "g", "tree": {"$className": "DataModel"}}'}, "roblox", None),
    ({"package.json": '{"dependencies": {"three": "^0.160.0"}}'}, "threejs", "^0.160.0"),
    ({"package.json": '{"dependencies": {"three": "^0.160.0"}}',
      "package-lock.json": '{"packages": {"node_modules/three": {"version": "0.160.1"}}}'}, "threejs", "0.160.1"),
    # a project-file engine decides over web tooling in the same root
    ({"project.godot": GODOT4, "package.json": '{"devDependencies": {"three": "0.160.0"}}'}, "godot", "4.7"),
])
def test_detection_reads_the_projects_own_files(tmp_path, files, engine, version):
    info = detect_project(_write(tmp_path, files))
    assert (info.engine, info.version) == (engine, version), info
    assert info.evidence
    if version is None or engine == "godot" and version in ("3", "4"):
        assert info.reason  # a version that is not stated says why


@pytest.mark.parametrize("files, needle", [
    ({}, "no engine project file"),
    ({"Assets/": ""}, "Assets/ without ProjectSettings"),      # Unity needs the pair
    ({"Game.csproj": "<Project/>"}, "C# alone names no engine"),
    ({"tool.project.json": '{"name": "not rojo"}'}, "no engine project file"),  # not Rojo: no tree
    ({"project.godot": GODOT4, "Game.uproject": "{}"}, "more than one engine"),
    ({"A.uproject": "{}", "B.uproject": "{}"}, "several .uproject"),
])
def test_detection_says_when_it_cannot_tell(tmp_path, files, needle):
    info = detect_project(_write(tmp_path, files))
    assert info.engine is None and needle in (info.reason or ""), info


# --------------------------------------------------------------------------- the offline crate


@pytest.fixture(scope="module")
def crate(tmp_path_factory) -> Path:
    """The offline crate's package, as ``agy3d prop --offline --engine godot_4`` writes it."""
    from antigravity3d.cli import main

    out = tmp_path_factory.mktemp("crate")
    assert main(["prop", "wooden crate with iron brackets", "--offline", "--engine", "godot_4", "--out", str(out)]) == 0
    (pkg,) = out.glob("runs/*/package")
    return pkg


def _map(pkg: Path, role: str) -> str:
    """The package's map file for a role, as its manifest names it (never derived from a file name)."""
    m = json.loads((pkg / "manifest.json").read_text())
    return next(t["path"] for t in m["textures"] if t["role"] == role)


def _project(tmp_path: Path, name: str = "game") -> Path:
    root = tmp_path / name
    root.mkdir()
    (root / "project.godot").write_text(GODOT4)
    (root / "main.tscn").write_text("[gd_scene format=3]\n[node name=\"Main\" type=\"Node3D\"]\n")
    return root


def _copy_pkg(pkg: Path, dst: Path, edit=None) -> Path:
    shutil.copytree(pkg, dst)
    if edit:
        m = json.loads((dst / "manifest.json").read_text())
        edit(m, dst)
        (dst / "manifest.json").write_text(json.dumps(m))
    return dst


def test_lod0_references_the_package_textures_and_nothing_else_changes(crate):
    """The reusable rewrite: mesh data and materials unchanged, every image resolves to the package file with the
    same bytes the GLB embedded (manifest glb_images), and the image bytes leave the file."""
    import trimesh

    from antigravity3d.deliver.glb import _split
    from antigravity3d.kernel.validator import glb_images

    manifest = json.loads((crate / "manifest.json").read_text())
    src = crate / f"{manifest['asset']}.glb"
    uri_of = {_sha(crate / f): f for f in manifest["files"] if f.startswith("textures/")}
    data, report = reference_images(src, uri_of)
    out = src.parent.parent / "lod0_referenced.glb"
    out.write_bytes(data)
    try:
        before, after = _split(src.read_bytes())[0], _split(data)[0]
        assert before["materials"] == after["materials"] and before["meshes"] == after["meshes"]
        assert before["textures"] == after["textures"] and len(before["images"]) == len(after["images"])
        embedded = {r["image"]: r["sha256"] for r in manifest["glb_images"][src.name]}
        for rec in glb_images(out):
            assert rec["storage"] == "uri"
            assert _sha(crate / rec["uri"]) == embedded[rec["image"]]  # the same bytes the GLB embedded
        assert [r["storage"] for r in report] == ["uri"] * len(report)
        assert len(data) < src.stat().st_size - sum(r["bytes"] for r in manifest["glb_images"][src.name]) + 4096
        a, b = (trimesh.load_scene(p, process=False) for p in (src, out))
        assert sorted(a.geometry) == sorted(b.geometry)
        for k in a.geometry:
            assert np.array_equal(a.geometry[k].vertices, b.geometry[k].vertices)
            assert np.array_equal(a.geometry[k].faces, b.geometry[k].faces)
            assert np.array_equal(a.geometry[k].visual.uv, b.geometry[k].visual.uv)
    finally:
        out.unlink()


def test_switch_distance_comes_from_the_package_record(crate, tmp_path):
    from antigravity3d.kernel.lod_diff import transition_distance

    manifest = json.loads((crate / "manifest.json").read_text())
    plan = G.build_plan(crate, manifest, "agy3d/x")
    b = np.asarray(manifest["glb_bounds_m"])
    radius = float(np.linalg.norm(b[1] - b[0]) / 2)
    want = transition_distance(radius, manifest["lod_screen_fractions"][1], 60.0)
    assert plan.levels[0].switch_in_m == pytest.approx(want)
    m = want * G.HYSTERESIS
    assert (plan.levels[0].end, plan.levels[0].end_margin) == pytest.approx((want + m, m))
    assert (plan.levels[1].begin, plan.levels[1].begin_margin) == pytest.approx((want + m, m))
    wide = G.build_plan(crate, manifest, "agy3d/x", fov_deg=75.0)
    assert wide.levels[0].switch_in_m == pytest.approx(transition_distance(radius, manifest["lod_screen_fractions"][1], 75.0))


def test_install_dry_run_update_refusal_and_uninstall(crate, tmp_path):
    project = _project(tmp_path)
    before = _tree(project)
    dry = install_package(crate, project, dry_run=True, check=False)
    assert dry.status == "dry_run" and dry.ok and _tree(project) == before
    assert {f["action"] for f in dry.files} == {"create"}

    r = install_package(crate, project, check=False)
    assert r.status == "installed", r.message
    dest = project / r.dest
    assert r.dest == "agy3d/WoodenCrateIron"
    # nothing outside DEST
    assert {k: v for k, v in _tree(project).items() if not k.startswith(r.dest + "/")} == before
    record = json.loads((dest / RECORD).read_text())
    for f, rec in record["files"].items():
        assert _sha(dest / f) == rec["sha256"], f
    assert set(record["files"]) == {f["path"] for f in dry.files}
    assert not (dest / f"{record['asset']}_colliders.glb").exists()  # its hulls are shapes in the scene

    # the engine rewrites its .import files (uid, dest paths, defaults): not an edit, and kept as Godot wrote it
    albedo_import = _map(crate, "albedo") + ".import"
    imp = dest / albedo_import
    imp.write_text(imp.read_text().replace('[remap]\n', '[remap]\n\nuid="uid://abc"\n') + "process/new_default=1\n")
    keep = _sha(imp)
    same = install_package(crate, project, check=False)
    assert same.status == "unchanged", same.message
    assert _sha(imp) == keep

    # a file the developer added is kept; an edited file of ours blocks an update until forced
    (dest / "notes.txt").write_text("mine")
    scene = dest / "WoodenCrateIron.tscn"
    scene.write_text(scene.read_text() + "\n# tweak\n")
    tree = _tree(project)
    refused = install_package(crate, project, check=False, fov_deg=75.0)
    assert refused.status == "refused" and "WoodenCrateIron.tscn" in refused.message and _tree(project) == tree
    # an import setting the developer changed is an edit too
    imp.write_text(imp.read_text().replace("mipmaps/generate=true", "mipmaps/generate=false"))
    assert "mipmaps/generate" in install_package(crate, project, check=False).message
    forced = install_package(crate, project, check=False, force=True, fov_deg=75.0)
    assert forced.status == "updated" and forced.warnings, forced.message
    assert {f["path"] for f in forced.files if f["action"] == "replace"} >= {"WoodenCrateIron.tscn", albedo_import}
    assert (dest / "notes.txt").read_text() == "mine"
    assert json.loads((dest / RECORD).read_text())["fov_deg"] == 75.0

    # uninstall: refuses an edited file, then removes exactly what was written and keeps the developer's file
    scene.write_text(scene.read_text() + "# again\n")
    assert uninstall(project, r.dest).status == "refused"
    assert uninstall(project, r.dest, dry_run=True, force=True).status == "dry_run" and scene.exists()
    gone = uninstall(project, r.dest, force=True)
    assert gone.status == "uninstalled" and gone.notes
    assert _tree(project) == {**before, f"{r.dest}/notes.txt": hashlib.sha256(b"mine").hexdigest()}


def test_a_file_the_tool_did_not_write_is_never_overwritten(crate, tmp_path):
    project = _project(tmp_path)
    dest = project / "agy3d/WoodenCrateIron"
    dest.mkdir(parents=True)
    (dest / "WoodenCrateIron.tscn").write_text("theirs")
    for force in (False, True):
        r = install_package(crate, project, check=False, force=force)
        assert r.status == "refused" and "did not write" in r.message
    assert (dest / "WoodenCrateIron.tscn").read_text() == "theirs"
    for bad_dest in ("../outside", "/abs/../..", ".hidden/x", ""):
        assert install_package(crate, project, bad_dest or ".", check=False).status == "refused", bad_dest


def test_a_failed_install_leaves_no_half_written_directory(crate, tmp_path):
    from antigravity3d.deliver.detect import detect_project as dp

    project = _project(tmp_path)
    before = _tree(project)
    manifest = read_package(crate)
    n = {"writes": 0, "at": 5}

    def boom(path: Path) -> None:
        n["writes"] += 1
        if n["writes"] == n["at"]:
            raise OSError("disk full")

    with pytest.raises(OSError):
        _install_godot(crate, project, manifest, dp(project), {}, None, False, False, False, fov_deg=None, godot_bin=None,
                       render=False, _write_hook=boom)
    assert _tree(project) == before and not (project / "agy3d").exists()
    # an update that fails restores the files it had replaced
    assert install_package(crate, project, check=False).status == "installed"
    installed = _tree(project)
    n.update(writes=0, at=1)  # the new scene (another field of view) is written, then the update fails
    with pytest.raises(OSError):
        _install_godot(crate, project, manifest, dp(project), {}, None, False, True, False, fov_deg=75.0, godot_bin=None,
                       render=False, _write_hook=boom)
    assert _tree(project) == installed


def test_refusals_name_the_reason(crate, tmp_path):
    unity = _write(tmp_path / "unity", {"Assets/": "", "ProjectSettings/ProjectVersion.txt": "m_EditorVersion: 6000.0.1f1\n"})
    r = install_package(crate, unity, check=False)
    assert r.status == "refused" and "built for godot_4" in r.message and "unity" in r.message and "--engine unity_urp" in r.message
    assert install_package(crate, tmp_path / "unity" / "Assets", check=False).status == "refused"
    godot3 = _write(tmp_path / "g3", {"project.godot": "config_version=4\n"})
    assert "Godot 3" in install_package(crate, godot3, check=False).message
    # every map the package ships is hashed by its manifest: a tampered one (the shared atlas images included) is refused
    manifest = json.loads((crate / "manifest.json").read_text())
    maps = sorted({t["path"] for t in manifest["textures"]} | set((manifest.get("texture_atlas") or {}).get("files", {}).values()))
    assert maps
    for i, rel in enumerate(maps):
        damaged = _copy_pkg(crate, tmp_path / f"damaged{i}")
        (damaged / rel).write_bytes(b"not it")
        r = install_package(damaged, _project(tmp_path, f"d{i}"), check=False)
        assert r.status == "refused" and "does not match its manifest" in r.message and rel in r.message, (rel, r.message)
    old = _copy_pkg(crate, tmp_path / "old", lambda m, _: m.update(schema_version=1))
    assert "older than 2" in install_package(old, _project(tmp_path, "g2"), check=False).message


def test_other_engines_are_told_what_to_import_and_nothing_is_written(crate, tmp_path):
    unity_pkg = _copy_pkg(crate, tmp_path / "u", lambda m, _: m.update(target_engine="unity_urp"))
    project = _write(tmp_path / "unity", {"Assets/": "", "ProjectSettings/ProjectVersion.txt": "m_EditorVersion: 6000.0.1f1\n"})
    before = _tree(project)
    r = install_package(unity_pkg, project)
    assert r.status == "not_built" and "WoodenCrateIron.glb" in r.message and "not built yet" in r.message
    assert _tree(project) == before and not r.ok


def test_a_package_without_colliders_installs_and_says_why(crate, tmp_path):
    def drop(m, root):
        name = f"{m['asset']}_colliders.glb"
        (root / name).unlink()
        m.update(colliders=[], collider_glb_nodes=[], collider_sources=[], collider_structure=None,
                 files=[f for f in m["files"] if f != name], file_hashes={k: v for k, v in m["file_hashes"].items() if k != name})

    pkg = _copy_pkg(crate, tmp_path / "nohulls", drop)
    r = install_package(pkg, _project(tmp_path), check=False)
    assert r.status == "installed" and any("no collision hulls" in n for n in r.notes)
    assert "StaticBody3D" not in (tmp_path / "game" / r.dest / "WoodenCrateIron.tscn").read_text()


def test_the_version_the_package_was_checked_in_is_compared(crate, tmp_path):
    def checked(version):
        return lambda m, _: m.setdefault("validation", {}).update(engine_import={"engine": "godot_4", "engine_version": version})

    same = install_package(_copy_pkg(crate, tmp_path / "a", checked("4.7.2-stable (official)")), _project(tmp_path, "p1"), dry_run=True)
    assert not any("checked against" in w for w in same.warnings), same.warnings
    older = install_package(_copy_pkg(crate, tmp_path / "b", checked("4.5.1-stable")), _project(tmp_path, "p2"), dry_run=True)
    assert any("newer than the one the package was checked against (4.5.1" in w for w in older.warnings), older.warnings
    unknown = install_package(crate, _project(tmp_path, "p3"), dry_run=True) if "engine_version" not in json.dumps(
        json.loads((crate / "manifest.json").read_text()).get("validation", {})) else None
    if unknown is not None:
        assert any("does not record which godot version" in w for w in unknown.warnings)


def test_godot_engine_version_is_recorded_by_the_import_check():
    """The validator's script prints the engine build; EngineReport carries it into validation.engine_import."""
    from antigravity3d.adapters.engines.validators import _GODOT_CHECK, EngineReport

    assert 'print("AGY_ENGINE_VERSION ", Engine.get_version_info().string)' in _GODOT_CHECK
    assert EngineReport(True, "godot_4", engine_version="4.7.2-stable (official)").engine_version


def test_install_decision_asks_before_writing():
    import importlib.util

    from antigravity3d.plugin_install import bundle_dir

    scripts = bundle_dir() / "hooks" / "scripts"
    import sys

    sys.path.insert(0, str(scripts))
    try:
        spec = importlib.util.spec_from_file_location("agy_pre_tool_gate", scripts / "pre_tool_gate.py")
        g = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(g)  # type: ignore[union-attr]
    finally:
        sys.path.remove(str(scripts))
    assert g.install_decision({"package_dir": "p"})["decision"] == "allow"  # dry run by default
    ask = g.install_decision({"package_dir": "p", "project_dir": "/game", "dry_run": False, "force": True})
    assert ask["decision"] == "ask" and "/game" in ask["reason"] and "REPLACES" in ask["reason"]


# --------------------------------------------------------------------------- real Godot


@pytest.fixture()
def godot_bin() -> str:
    exe = G.find_godot()
    if exe is None:
        pytest.skip("set GODOT_BIN to run stock Godot")
    return exe


def _checks(r) -> dict[str, tuple[str, str]]:
    return {c.name: (c.status, c.detail) for c in r.checks}


def test_offline_crate_installed_into_a_fresh_godot_project_passes_every_check(crate, tmp_path, godot_bin):
    project = _project(tmp_path)
    r = install_package(crate, project)
    checks = _checks(r)
    assert r.status == "installed" and r.ok, checks
    want = {"import", "scene", "scale", "levels", "materials", "collision", "level_switching"}
    if shutil.which("xvfb-run") is None:
        want.discard("level_switching")
    assert {k for k, (s, _) in checks.items() if s == "passed"} >= want, checks
    assert not (project / ".godot").exists()  # checked in an isolated copy: the real project's cache is untouched
    if r.screenshot:
        from PIL import Image

        with Image.open(r.screenshot) as im:
            assert im.size == (512, 512) and len(im.convert("RGB").getcolors(1 << 16) or []) > 50
    report = json.loads(Path(r.report_path).read_text())
    assert report["checks"] and report["measured"]["scene"]["data"]["engine_version"].startswith("4.")
    # the project imported in place, as the editor would, makes no texture copies and no import errors
    out = subprocess.run([godot_bin, "--headless", "--path", str(project), "--import"], capture_output=True, text=True, timeout=300)
    assert not G.asset_log_lines(out.stdout + out.stderr, r.dest, ["WoodenCrateIron"])
    files = {p.relative_to(project / r.dest).as_posix() for p in (project / r.dest).rglob("*") if p.is_file()}
    record = json.loads((project / r.dest / RECORD).read_text())
    # the install's files plus the shared materials Godot extracts on import, one per glTF material (godot.scene_params)
    assert {f for f in files if not f.startswith(".agy3d/")} == set(record["files"]) | set(record["engine_generated"])
    assert record["engine_generated"] == [f"materials/{m['name']}.tres" for m in json.loads((crate / "manifest.json").read_text())["materials"]]
    assert "one resource shared by every level" in checks["materials"][1]
    # uninstall takes Godot's material files with it (they would point at removed textures) and leaves the project as it was
    assert uninstall(project, r.dest).status == "uninstalled"
    assert not (project / r.dest).exists()


def _installed(crate: Path, tmp_path: Path, name: str = "game") -> tuple[Path, str]:
    project = _project(tmp_path, name)
    r = install_package(crate, project, check=False)
    assert r.status == "installed"
    return project, r.dest


def test_negative_control_wrong_scale_fails_the_scale_check(crate, tmp_path, godot_bin):
    project, dest = _installed(crate, tmp_path)
    imp = project / dest / "WoodenCrateIron.glb.import"
    imp.write_text(imp.read_text().replace("nodes/root_scale=1.0", "nodes/root_scale=1.1"))
    checks = _checks(check_install(project, dest, render=False))
    assert checks["scale"][0] == "failed" and "bounds drift" in checks["scale"][1], checks


def test_negative_control_missing_map_fails(crate, tmp_path, godot_bin):
    project, dest = _installed(crate, tmp_path)
    (project / dest / _map(crate, "normal")).unlink()
    checks = _checks(check_install(project, dest, render=False))
    assert checks["materials"][0] == "failed" and "normal map not bound" in checks["materials"][1], checks
    assert checks["import"][0] == "failed", checks


def test_negative_control_missing_level_fails(crate, tmp_path, godot_bin):
    project, dest = _installed(crate, tmp_path)
    for f in ("WoodenCrateIron_LOD1.glb", "WoodenCrateIron_LOD1.glb.import"):
        (project / dest / f).unlink()
    checks = _checks(check_install(project, dest, render=False))
    assert "failed" in (checks.get("levels", ("",))[0], checks["scene"][0]), checks


def test_negative_control_levels_drawn_together_fail_the_switching_check(crate, tmp_path, godot_bin):
    if shutil.which("xvfb-run") is None:
        pytest.skip("needs xvfb-run for Godot's renderer")
    project, dest = _installed(crate, tmp_path)
    scene = project / dest / "WoodenCrateIron.tscn"
    scene.write_text("\n".join(line for line in scene.read_text().splitlines() if not line.startswith("visibility_range_begin")))
    checks = _checks(check_install(project, dest))
    assert checks["level_switching"][0] == "failed" and "LOD0 expected" in checks["level_switching"][1], checks
    assert checks["levels"][0] == "failed", checks


def test_negative_control_collider_over_the_limit_fails(crate, tmp_path, godot_bin):
    from types import SimpleNamespace

    from scipy.spatial import ConvexHull

    from antigravity3d.kernel.fast_ops import write_gltf

    def big_hull(m, root):
        rng = np.random.default_rng(3)
        pts = rng.normal(size=(300, 3))
        pts = 0.3 * pts / np.linalg.norm(pts, axis=1, keepdims=True) + np.array([0, 0, 0.4])
        hull = ConvexHull(pts)
        name = f"{m['asset']}_colliders.glb"
        items = [SimpleNamespace(points=pts, triangles=hull.simplices.astype(np.int32))]
        write_gltf(root / name, items, [m["collider_glb_nodes"][0]])
        m.update(colliders=m["colliders"][:1], collider_glb_nodes=m["collider_glb_nodes"][:1])
        m["file_hashes"][name] = {"sha256": _sha(root / name), "bytes": (root / name).stat().st_size}

    pkg = _copy_pkg(crate, tmp_path / "bighull", big_hull)
    r = install_package(pkg, _project(tmp_path), render=False)
    checks = _checks(r)
    assert checks["collision"][0] == "failed" and "300 points > 256" in checks["collision"][1], checks


def test_godot_measures_visibility_range_from_the_aabb_centre(tmp_path, godot_bin):
    """Why every level instance gets custom_aabb = the asset's bounds (deliver/godot.py): Godot 4 measures a
    visibility range from the camera to the instance's world AABB centre, not to its origin, so two batches of one
    level with different bounds would switch at different camera positions. Undocumented; observed here."""
    if shutil.which("xvfb-run") is None:
        pytest.skip("needs xvfb-run for Godot's renderer")
    (tmp_path / "project.godot").write_text('config_version=5\n[application]\nconfig/name="t"\n')
    (tmp_path / "vr.gd").write_text('''extends SceneTree
func _init():
    call_deferred("go")
func go():
    var vp := SubViewport.new(); vp.size = Vector2i(64,64); vp.own_world_3d = true; vp.render_target_update_mode = SubViewport.UPDATE_ALWAYS
    root.add_child(vp)
    var st := SurfaceTool.new(); st.begin(Mesh.PRIMITIVE_TRIANGLES)
    for v in [Vector3(9,-1,0),Vector3(11,-1,0),Vector3(10,1,0)]:
        st.add_vertex(v)
    var mi := MeshInstance3D.new(); mi.mesh = st.commit()
    var mat := StandardMaterial3D.new(); mat.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED; mat.albedo_color = Color.RED; mat.cull_mode = BaseMaterial3D.CULL_DISABLED
    mi.material_override = mat
    mi.visibility_range_end = 5.0
    vp.add_child(mi)
    var cam := Camera3D.new(); vp.add_child(cam); cam.position = Vector3(10, 0, 3); cam.make_current()
    for mode in ["origin_far_aabb_near", "custom_aabb_on_origin"]:
        if mode == "custom_aabb_on_origin":
            mi.custom_aabb = AABB(Vector3(-1,-1,-1), Vector3(2,2,2))
        for i in 4:
            await RenderingServer.frame_post_draw
        print("AGY_VR ", mode, " ", vp.get_texture().get_image().get_pixel(32,32).r > 0.9)
    quit()
''')
    out = subprocess.run(["xvfb-run", "-a", godot_bin, "--rendering-method", "gl_compatibility", "--audio-driver", "Dummy",
                          "--path", str(tmp_path), "--script", "vr.gd"], capture_output=True, text=True, timeout=120)
    seen = dict(line.split()[1:3] for line in out.stdout.splitlines() if line.startswith("AGY_VR "))
    # origin 10.4 m away, AABB centre 3 m away, range end 5 m: drawn, so the AABB centre is what counts
    assert seen == {"origin_far_aabb_near": "true", "custom_aabb_on_origin": "false"}, out.stdout[-2000:]


async def test_mcp_tool_dry_runs_by_default_and_installs_when_told(crate, tmp_path):
    from antigravity3d.runtime.mcp_server import StudioHost, _management

    tool = _management(StudioHost())["install_into_project"][0]
    project = _project(tmp_path)
    before = _tree(project)
    text = await tool(package_dir=str(crate), ctx=None, project_dir=str(project))
    assert text.startswith("DRY_RUN ") and _tree(project) == before
    # the plan a person approves: every file it would write, one per line, and no machine-readable dump after it
    assert text.splitlines()[0].startswith("DRY_RUN dry run: would write agy3d/WoodenCrateIron: ")
    assert "\nwould create WoodenCrateIron.tscn" in text and not text.splitlines()[-1].startswith("{")
    text = await tool(package_dir=str(crate), ctx=None, project_dir=str(project), dry_run=False)
    assert text.startswith("INSTALLED ") and (project / "agy3d/WoodenCrateIron/WoodenCrateIron.tscn").is_file()
    assert not text.startswith("INSTALLED installed") and "\nfiles: " in text


def test_the_install_answer_is_short_enough_to_be_shown():
    """Run 2 of the website task: 10,292 characters for one web prop, which the host put in a file. Passed checks are a
    count, a failed or skipped one keeps its line, notes are cut, and the record's path stands for the rest."""
    from antigravity3d.deliver.install import Check, InstallResult
    from antigravity3d.runtime.mcp_server import _install_text

    r = InstallResult("installed", "installed capture_gate: loader in src/agy3d/capture_gate, files in public/agy3d/capture_gate",
                      engine="threejs", package={"asset": "capture_gate"}, dest="src/agy3d/capture_gate",
                      files=[{"path": f"public/agy3d/capture_gate/f{i}", "action": "create", "bytes": 50_000} for i in range(14)],
                      checks=[Check(f"c{i}", "passed", "x" * 600) for i in range(11)]
                      + [Check("react_component", "skipped", "no esbuild in the project's node_modules/.bin")],
                      notes=["compression not used: " + "tier " * 400, "a page loading this asset received 776,203 bytes for it"],
                      report_path="src/agy3d/capture_gate/.agy3d/check/report.json")
    text = _install_text(r)
    assert len(text) < 2_000, len(text)
    assert text.splitlines()[0] == "INSTALLED capture_gate: loader in src/agy3d/capture_gate, files in public/agy3d/capture_gate"
    assert "use it: src/agy3d/capture_gate/index.js exports loadCaptureGate({ renderer })" in text
    assert "checks: 11 passed, 0 failed, 1 skipped" in text and "check react_component: skipped no esbuild" in text
    assert "check c0" not in text and "776,203 bytes" in text and "every check in full: src/agy3d/capture_gate/.agy3d/check/report.json" in text


@pytest.fixture(scope="module")
def crate_parts(tmp_path_factory) -> Path:
    """The offline crate with --keep-contact-faces: the node rule that keeps part (or per-material batch) nodes, the
    layout in which authored ids reach the GLB. A default package ships one node per glTF material (Batch_<n>)."""
    from antigravity3d.cli import main

    out = tmp_path_factory.mktemp("crate_parts")
    assert main(["prop", "wooden crate with iron brackets", "--offline", "--engine", "godot_4", "--keep-contact-faces",
                 "--out", str(out)]) == 0
    (pkg,) = out.glob("runs/*/package")
    return pkg


def test_author_part_names_survive_the_godot_import(crate_parts, tmp_path, godot_bin):
    """Part ids become GLB node names where parts keep their nodes: an id ending in a Godot import suffix
    ("rear_wheel2") must stay a mesh, and the long ids of expanded mirrors and copies ("handle__mx__grip") must keep
    their names, or the scene's per-node settings would miss their nodes."""
    from antigravity3d.deliver.glb import _join, _split

    crate = crate_parts
    names = ["rear_wheel2", "handle__mx__grip"]

    def rename(m, root):
        for i, level in enumerate(G.level_files(m)):
            doc, blob = _split((root / level).read_bytes())
            meshes = [n for n in doc["nodes"] if "mesh" in n]
            assert len(meshes) == len(names)
            for n, new in zip(meshes, names):
                n["name"] = new + ("" if i == 0 else f"_LOD{i}")
            (root / level).write_bytes(_join(doc, blob))
            m["file_hashes"][level] = {"sha256": _sha(root / level), "bytes": (root / level).stat().st_size}

    pkg = _copy_pkg(crate, tmp_path / "named", rename)
    r = install_package(pkg, _project(tmp_path), render=False)
    checks = _checks(r)
    assert r.ok, checks
    assert "rear_wheel2" in (tmp_path / "game" / r.dest / "WoodenCrateIron.tscn").read_text()


def test_default_package_node_names_meet_no_godot_import_suffix(crate, tmp_path, godot_bin):
    """A default (shared-atlas) package names its nodes Batch_<n> and Batch_<n>_LOD<k>. Imported by stock Godot with
    its default settings (name suffixes on), every one stays a MeshInstance3D under its own name."""
    m = json.loads((crate / "manifest.json").read_text())
    project = tmp_path / "stock"
    project.mkdir()
    (project / "project.godot").write_text(GODOT4)
    for f in [*G.level_files(m), *(t["path"] for t in m["textures"])]:
        (project / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(crate / f, project / f)
    subprocess.run([godot_bin, "--headless", "--path", str(project), "--import"], capture_output=True, timeout=300, check=False)
    (project / "n.gd").write_text("extends SceneTree\nfunc _init():\n    for p in OS.get_environment(\"AGY_FILES\").split(\",\"):\n"
                                  "        for c in load(\"res://\" + p).instantiate().get_children():\n"
                                  "            print(\"AGY_NODE \", c.name, \" \", c.get_class())\n    quit()\n")
    out = subprocess.run([godot_bin, "--headless", "--path", str(project), "--script", "n.gd"], capture_output=True, text=True,
                         timeout=120, env={**__import__("os").environ, "AGY_FILES": ",".join(G.level_files(m))}, check=False)
    seen = [line.split()[1:] for line in out.stdout.splitlines() if line.startswith("AGY_NODE ")]
    expected = [n + ("" if i == 0 else f"_LOD{i}") for i, nodes in enumerate(m["lod_glb_nodes"]) for n in nodes]
    assert seen == [[n, "MeshInstance3D"] for n in expected], out.stdout[-1500:]


async def test_the_comparison_harness_reads_the_install_result(crate, tmp_path, godot_bin):
    """benchmarks/compare_eval.install_check calls install_package and reads its checks ("passed" / "failed" /
    "skipped"): the scorecard row "Installed and checked inside a Godot project" passes only when every check passed."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))
    try:
        import compare_eval
    finally:
        sys.path.pop(0)
    r = await compare_eval.install_check(crate / "manifest.json", tmp_path / "godot_project")
    assert r["available"] and r["offered"] and r["status"] == "installed" and r["checks_failed"] == 0, r
    if shutil.which("xvfb-run"):
        assert r["ok"] and r["checks_skipped"] == 0, r
    else:  # the rendered switching check cannot run: skipped, so the row does not pass
        assert not r["ok"] and r["checks_skipped"] == 1, r
