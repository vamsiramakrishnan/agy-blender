"""A delivered package holds nothing of the machine that made it, and is enough to re-open itself.

Round seven, "order": the shipped USD of a script asset with free-form parts held, twice per part (the prim's
``agy:meshRef`` and the embedded scene the package is re-opened from), the absolute path of the part's PLY in the session's
work folder: a developer's home directory inside a file they publish, a USD that differs between two machines, and a
package that could not be edited once its work folder was gone (``FileNotFoundError`` in ``mesh_io.triangle_parts``).
The parts now travel in the package (``parts/``) and are referenced relative to it."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import textwrap
import zipfile
from pathlib import Path

import numpy as np
import pytest
from pxr import Sdf

from antigravity3d import cli
from antigravity3d.build import shapes
from antigravity3d.dags import edit_asset as E
from antigravity3d.harness import tools as T
from antigravity3d.harness.session import session
from antigravity3d.runtime import mcp_server as M

TETRA = "[(0, 0, 0), (0.1, 0, 0), (0, 0.1, 0), (0, 0, 0.1)], [(0, 2, 1), (0, 1, 3), (0, 3, 2), (1, 2, 3)]"
SCRIPT = textwrap.dedent(f"""\
    from agy3d import Asset
    a = Asset("crate", engine="godot_4", max_tris=4000, brief="a red crate with two knobs")
    red = a.material("red", "#c02020", surface="red enamel paint")
    body = a.box("body", size=(1.0, 0.8, 0.6), bottom=(0, 0, 0), material=red)
    a.mesh("knob", {TETRA}, at=(-0.05, -0.05, body.top), material=red)
    a.mesh("knob2", {TETRA}, at=(0.3, -0.05, body.top), material=red)
    a.save()
    """)
MOVE = {"ops": [{"op": "set_transform", "node_id": "body",
                 "transform": {"translate": [0, 0, 0.31], "rotate_euler_deg": [0, 0, 0], "scale": [1, 1, 1]}}]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGY3D_OFFLINE", "1")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("AGY3D_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.delenv("AGY3D_POOL_SOCKET", raising=False)
    return tmp_path


# ------------------------------------------------------------------------------------------------ what a scan reads

def _layer_text(path: Path) -> str:
    layer = Sdf.Layer.FindOrOpen(str(path))
    assert layer is not None, path
    return layer.ExportToString()


def texts(folder: Path) -> dict[str, str]:
    """Every file of a package as text: USD and USDZ through pxr (the crate format is not greppable), a GLB as its JSON
    chunk, everything else as its bytes."""
    out: dict[str, str] = {}
    for p in sorted(folder.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(folder).as_posix()
        if p.suffix in (".usd", ".usda", ".usdc"):
            out[rel] = _layer_text(p)
        elif p.suffix == ".usdz":
            with zipfile.ZipFile(p) as z, tempfile.TemporaryDirectory() as tmp:
                for name in z.namelist():
                    inner = Path(z.extract(name, tmp))
                    out[f"{rel}!{name}"] = _layer_text(inner) if inner.suffix in (".usd", ".usda", ".usdc") else inner.read_bytes().decode("latin-1")
        elif p.suffix == ".glb":
            raw = p.read_bytes()
            n = int.from_bytes(raw[12:16], "little")
            out[rel] = raw[20:20 + n].decode("utf-8")
        else:
            out[rel] = p.read_bytes().decode("latin-1")
    return out


def leaks(folder: Path, needles: list[str]) -> list[tuple[str, str]]:
    return [(rel, n) for rel, t in texts(folder).items() for n in needles if n and n in t]


def digests(folder: Path) -> dict[str, str]:
    return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.rglob("*")) if p.is_file()}


# ---------------------------------------------------------------------------------------------- the two install modes

async def script_package(root: Path, name: str) -> tuple[Path, Path, str]:
    """(workspace, package folder, session id) of the script asset, delivered offline from a workspace named ``name``."""
    ws = root / name
    ws.mkdir()
    (ws / "crate.py").write_text(SCRIPT)
    host = M.StudioHost(script=True)
    r = await host.run_build("crate.py", str(ws), "conv-port-" + "0" * 32, preview=True, token_ceiling=100_000, max_images=0)
    assert r.startswith("BUILT crate.py"), r
    d = await host.call_tool("deliver_package", T.deliver_package, "", {"dest": "output"})
    assert d.startswith("DELIVERED"), d
    return ws, session(host.registry, host.active).last_export, host.active


async def default_package(root: Path, name: str) -> tuple[Path, Path, str]:
    """The default mode: a box and a supplied PLY file (a free-form part the user gave) placed by apply_scene_ir."""
    ws = root / name
    (ws / "supplied").mkdir(parents=True)
    pts = np.array([(0, 0, 0), (0.1, 0, 0), (0, 0.1, 0), (0, 0, 0.1)], float)
    (ws / "supplied" / "knob.ply").write_bytes(shapes.ply_bytes(pts, np.array([(0, 2, 1), (0, 1, 3), (0, 3, 2), (1, 2, 3)])))
    host = M.StudioHost(script=False)
    sid = await host._open("crate", "godot_4", 4000, "", str(ws), "", "", False, True, 100_000, 0, "")
    ops = [{"op": "upsert_node", "node": {"id": "body", "kind": "box", "params": {"size": [1.0, 0.8, 0.6]},
                                          "transform": {"translate": [0, 0, 0.3]}}},
           {"op": "upsert_node", "node": {"id": "knob", "kind": "mesh_ref", "params": {"path": str(ws / "supplied" / "knob.ply")},
                                          "transform": {"translate": [0, 0, 0.6]}}}]
    r = await host.call_tool("apply_scene_ir", T.apply_scene_ir, sid, {"ops": ops})
    assert r.startswith("OK"), r
    d = await host.call_tool("deliver_package", T.deliver_package, sid, {"dest": "output"})
    assert d.startswith("DELIVERED"), d
    return ws, session(host.registry, sid).last_export, sid


@pytest.mark.parametrize("make", [script_package, default_package], ids=["script mode", "default mode"])
async def test_no_file_of_a_package_holds_the_workspace_the_home_directory_or_the_session(env, make):
    ws, pkg, sid = await make(env, "zz_private_workspace")
    found = leaks(pkg, [str(ws), str(env), str(Path.home()), sid, ws.name])
    assert found == []
    # and the part files the scene names are in the package, by a path relative to it
    scene = E.load_shipped(pkg).scene
    refs = [n.params["path"] for n in scene.nodes if n.kind == "mesh_ref"]
    assert refs and all(not Path(r).is_absolute() and (pkg / r).is_file() for r in refs), refs


async def test_two_deliveries_of_one_script_in_two_folders_ship_the_same_usd_and_usdz(env):
    _, a, _ = await script_package(env, "first_workspace")
    _, b, _ = await script_package(env, "second_place")
    da, db = digests(a), digests(b)
    for k in ("crate.usd", "crate.usdz", "crate.glb"):
        assert da[k] == db[k], k
    assert {k: v for k, v in da.items() if k.startswith("parts/")} == {k: v for k, v in db.items() if k.startswith("parts/")} != {}


# ------------------------------------------------------------------------------------ a moved package edits by itself

def test_a_moved_package_whose_work_folder_is_gone_is_edited_by_the_cli(env, capsys):
    import asyncio

    ws, pkg, _ = asyncio.run(script_package(env, "gone_soon"))  # cli.main runs its own loop
    moved = env / "elsewhere" / "crate"
    shutil.copytree(pkg, moved)
    shutil.rmtree(ws)  # the work folder: the script, the parts it wrote, the session
    patch = env / "ops.json"
    patch.write_text(json.dumps(MOVE))
    rc = cli.main(["edit", str(moved), "lift the body a little", "--patch", str(patch), "--offline"])
    cap = capsys.readouterr()
    assert rc == 0, cap.out + cap.err
    man = json.loads((moved / "manifest.json").read_text())
    assert man["edits"][0]["touched"] == ["body"]
    assert leaks(moved, [str(ws), str(env / "gone_soon"), "gone_soon"]) == []
    assert {n.id for n in E.load_shipped(moved).scene.nodes if n.kind == "mesh_ref"} == {"knob", "knob2"}


async def test_a_moved_package_whose_work_folder_is_gone_is_reopened_and_delivered_again(env):
    ws, pkg, _ = await script_package(env, "gone_too")
    moved = env / "elsewhere2" / "crate"
    shutil.copytree(pkg, moved)
    shutil.rmtree(ws)
    work = env / "newplace"
    work.mkdir()
    host = M.StudioHost(script=False)
    r = await host.open_existing_asset(str(moved), str(work), preview=True, token_ceiling=100_000, max_images=0)
    assert "opened" in r and "(3 parts" in r, r
    sid = host.active
    r = await host.call_tool("apply_scene_ir", T.apply_scene_ir, sid, MOVE)
    assert r.startswith("OK"), r
    d = await host.call_tool("deliver_package", T.deliver_package, sid, {})
    assert d.startswith("DELIVERED"), d
    again = session(host.registry, sid).last_export
    assert leaks(again, [str(ws), "gone_too", str(work), sid]) == []


# ------------------------------------------------------------------------- what travels, what does not, what is refused

def _usd_scene_path(pkg: Path, new: str, node: str = "knob") -> None:
    """Rewrite the mesh_ref path of ``node`` in the package's embedded scene (a hand-edited or hostile package)."""
    from pxr import Usd

    from antigravity3d.kernel.compile_ir import IR_KEY, read_ir

    f = pkg / "crate.usd"
    stage = Usd.Stage.Open(str(f))
    asset = stage.GetDefaultPrim()
    scene = read_ir(asset)
    nodes = [n.model_copy(update={"params": {**n.params, "path": new}}) if n.id == node else n for n in scene.nodes]
    asset.SetCustomDataByKey(IR_KEY, scene.model_copy(update={"nodes": nodes}).model_dump_json())
    stage.GetRootLayer().Export(str(f))


async def test_the_parts_are_listed_with_hashes_but_no_engine_file_and_no_placed_or_installed_copy_holds_them(env):
    from antigravity3d.deliver.install import install_package

    ws, pkg, _ = await script_package(env, "ws_parts")
    man = json.loads((pkg / "manifest.json").read_text())
    listed = man["parts"]["files"]
    assert sorted(listed) == sorted(p.name for p in (pkg / "parts").iterdir()) and len(listed) == 2
    for name, rec in listed.items():
        assert rec["sha256"] == hashlib.sha256((pkg / "parts" / name).read_bytes()).hexdigest() and name.startswith(rec["node"] + "-")
    assert not [f for f in man["files"] if f.startswith("parts/")] and not [f for f in man["file_hashes"] if f.startswith("parts/")]
    assert not (ws / "output" / "parts").exists() and not list((ws / "output").rglob("*.ply"))  # deliver_package's placed folder
    project = env / "game"
    project.mkdir()
    (project / "project.godot").write_text('config_version=5\n[application]\nconfig/name="t"\n')
    r = install_package(pkg, project, check=False)
    assert r.status == "installed", r
    assert not list(project.rglob("*.ply")) and not list(project.rglob("parts"))
    # provenance names each part file by its hash, where the file is
    srcs = [k for k in man["provenance"]["sources"] if k.startswith("script_output:")]
    assert sorted(k.split(":", 1)[1].split("-")[0] for k in srcs) == ["knob", "knob2"]


def test_a_part_that_did_not_change_ships_the_same_file_and_a_dropped_part_leaves_none(env, capsys):
    import asyncio

    ws, pkg, _ = asyncio.run(script_package(env, "ws_drop"))
    before = digests(pkg / "parts")
    drop = env / "drop.json"
    drop.write_text(json.dumps({"ops": [{"op": "remove_node", "node_id": "knob2"}]}))
    moved = env / "pkgcopy"
    shutil.copytree(pkg, moved)
    shutil.rmtree(ws)
    rc = cli.main(["edit", str(moved), "remove the second knob", "--patch", str(drop), "--offline"])
    cap = capsys.readouterr()
    assert rc == 0, cap.out + cap.err
    after = digests(moved / "parts")
    assert sorted(after) == [k for k in sorted(before) if k.startswith("knob-")] and all(after[k] == before[k] for k in after)


async def test_an_older_package_is_refused_whether_or_not_its_file_is_still_there(env):
    """A package from before parts travelled with it names its file by an absolute path. It is refused even when that file
    exists: a package can come from anywhere, every file it names is held to the package (round five's containment rule), and
    opening it would make the tool read any mesh file on the machine and copy it into the next package's parts/. (An
    earlier version of this test pinned 'opened while the file is there'.)"""
    ws, pkg, _ = await script_package(env, "ws_old")
    old = env / "old"
    shutil.copytree(pkg, old)
    man = json.loads((old / "manifest.json").read_text())
    man.pop("parts")
    (old / "manifest.json").write_text(json.dumps(man))
    kept = {n: next((pkg / "parts").glob(f"{n}-*")) for n in ("knob", "knob2")}  # the files exist, outside the package
    for node, f in kept.items():
        _usd_scene_path(old, str(f), node)
    with pytest.raises(E.EditError, match=r"build it again from its script \(run_build on source/build.py\)"):
        E.load_shipped(old)
    (old / "source" / "build.py").unlink()
    with pytest.raises(E.EditError, match="its part files are gone; export it again from the workspace that made it"):
        E.load_shipped(old)
    for node, f in kept.items():
        _usd_scene_path(old, str(f.parent / "gone.ply"), node)  # and when it is not there
    with pytest.raises(E.EditError, match="its part files are gone"):
        E.load_shipped(old)


@pytest.mark.parametrize("bad", ["/etc/hostname", "parts/../manifest.json", "../parts/knob-000000000000.ply", "parts/knob2-000000000000.ply",
                                 "parts/knob.ply", "parts//knob-000000000000.ply", "parts/knob-000000000000.ply/..", "knob.ply"])
async def test_a_part_path_that_is_not_the_writers_pattern_inside_the_package_is_refused(env, bad):
    _, pkg, _ = await script_package(env, "ws_bad")
    _usd_scene_path(pkg, bad)
    with pytest.raises(E.EditError):
        E.load_shipped(pkg)


async def test_a_part_that_is_a_link_or_is_not_the_file_its_name_says_is_refused(env):
    _, pkg, _ = await script_package(env, "ws_link")
    knob = next((pkg / "parts").glob("knob-*"))
    name = knob.name
    knob.write_bytes(knob.read_bytes() + b"\0")
    with pytest.raises(E.EditError, match="not the file its name says"):
        E.load_shipped(pkg)
    outside = env / "outside.ply"
    outside.write_bytes(b"x")
    knob.unlink()
    knob.symlink_to(outside)
    with pytest.raises(E.EditError):
        E.load_shipped(pkg)
    assert name


# ------------------------------------------------------------------- the user's own file, in any format, outside the workspace

def _tetra():
    import trimesh

    return trimesh.Trimesh(np.array([(0, 0, 0), (0.1, 0, 0), (0, 0.1, 0), (0, 0, 0.1)], float),
                           np.array([(0, 2, 1), (0, 1, 3), (0, 3, 2), (1, 2, 3)]), process=False)


def _usd_mesh(path: Path, sublayer: Path | None = None) -> None:
    from pxr import Usd, UsdGeom

    stage = Usd.Stage.CreateNew(str(path))
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    root = UsdGeom.Xform.Define(stage, "/part")
    mesh = UsdGeom.Mesh.Define(stage, "/part/m")
    mesh.CreatePointsAttr([(0, 0, 0), (0.1, 0, 0), (0, 0.1, 0), (0, 0, 0.1)])
    mesh.CreateFaceVertexCountsAttr([3, 3, 3, 3])
    mesh.CreateFaceVertexIndicesAttr([0, 2, 1, 0, 1, 3, 0, 3, 2, 1, 2, 3])
    stage.SetDefaultPrim(root.GetPrim())
    stage.GetRootLayer().Save()
    if sublayer is not None:  # the same mesh, but through a file of its own
        outer = Usd.Stage.CreateNew(str(sublayer))
        outer.GetRootLayer().subLayerPaths.append(path.name)
        outer.SetDefaultPrim(outer.GetPrimAtPath("/part"))
        outer.GetRootLayer().Save()


def make_user_file(folder: Path, kind: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    if kind == "obj":
        (folder / "tet.mtl").write_text("newmtl m\nKd 1 0 0\nmap_Kd tet_diffuse.png\n")
        (folder / "tet.obj").write_text("mtllib tet.mtl\no tet\nv 0 0 0\nv 0.1 0 0\nv 0 0.1 0\nv 0 0 0.1\n"
                                        "vt 0 0\nvt 1 0\nvt 0 1\nvt 1 1\nusemtl m\n"
                                        "f 1/1 3/3 2/2\nf 1/1 2/2 4/4\nf 1/1 4/4 3/3\nf 2/2 3/3 4/4\n")
        return folder / "tet.obj"
    if kind == "glb":
        (folder / "tet.glb").write_bytes(_tetra().export(file_type="glb"))
        return folder / "tet.glb"
    if kind == "gltf_external_bin":
        files = _tetra().export(file_type="gltf")
        for name, data in files.items():
            (folder / name).write_bytes(data)
        assert len(files) > 1, list(files)
        return folder / "model.gltf"
    if kind == "usda_single":
        _usd_mesh(folder / "tet.usda")
        return folder / "tet.usda"
    if kind == "usda_sublayer":
        _usd_mesh(folder / "inner.usda", sublayer=folder / "outer.usda")
        return folder / "outer.usda"
    raise AssertionError(kind)


# A mesh_ref to a USD file cannot be delivered at all today, on head as well (package validation: glb_batch, "batches must
# partition all visual meshes": the referenced prim's name is not the node's), so no package with one exists to re-open; the
# rule for such files is tested on _portable_reason alone, below.
PORTABLE = ["obj", "glb"]
NOT_PORTABLE = ["gltf_external_bin"]


async def default_package_of(root: Path, name: str, src: Path) -> tuple[Path, Path, str, object]:
    """Default mode, a box and the user's own file ``src`` (outside the workspace) placed by apply_scene_ir."""
    ws = root / name
    ws.mkdir()
    host = M.StudioHost(script=False)
    sid = await host._open("crate", "godot_4", 4000, "", str(ws), "", "", False, True, 100_000, 0, "")
    ops = [{"op": "upsert_node", "node": {"id": "body", "kind": "box", "params": {"size": [1.0, 0.8, 0.6]},
                                          "transform": {"translate": [0, 0, 0.3]}}},
           {"op": "upsert_node", "node": {"id": "knob", "kind": "mesh_ref", "params": {"path": str(src)},
                                          "transform": {"translate": [0, 0, 0.6]}}}]
    r = await host.call_tool("apply_scene_ir", T.apply_scene_ir, sid, {"ops": ops})
    assert r.startswith("OK"), r
    d = await host.call_tool("deliver_package", T.deliver_package, sid, {"dest": "output"})
    assert d.startswith("DELIVERED"), d
    return ws, session(host.registry, sid).last_export, sid, host


@pytest.mark.parametrize("kind", PORTABLE + NOT_PORTABLE)
async def test_a_file_of_the_users_own_directory_in_any_format_is_carried_whole_or_named_as_not_portable(env, kind):
    mine = env / "users_own_dir_zq"
    src = make_user_file(mine, kind)
    ws, pkg, sid, _ = await default_package_of(env, "zz_ws", src)
    assert leaks(pkg, [str(mine), mine.name, str(ws), ws.name, sid, str(Path.home())]) == []
    man = json.loads((pkg / "manifest.json").read_text())
    scene = E.load_shipped.__globals__["_scene_from_usd"](pkg / "crate.usd")
    path = next(n.params["path"] for n in scene.nodes if n.id == "knob")
    assert not Path(path).is_absolute() and ".." not in path
    if kind in PORTABLE:
        assert man["parts"]["files"] and "not_portable" not in man["parts"]
        (name,) = man["parts"]["files"]
        assert path == f"parts/{name}" and (pkg / path).read_bytes() == src.read_bytes()  # whole: the same bytes
    else:
        assert man["parts"]["files"] == {} and "knob" in man["parts"]["not_portable"] and not (pkg / "parts").exists()
        assert path == src.name  # the bare name, never a path of this machine
    # the package moves, the user's file and the workspace go: what still re-opens is what travelled
    moved = env / "elsewhere" / "crate"
    shutil.copytree(pkg, moved)
    shutil.rmtree(mine)
    shutil.rmtree(ws)
    if kind in NOT_PORTABLE:
        with pytest.raises(E.EditError, match=r"part knob: its file could not travel in the package \(.*\): .*single-file mesh"):
            E.load_shipped(moved)
        return
    work = env / "newplace"
    work.mkdir()
    host = M.StudioHost(script=False)
    r = await host.open_existing_asset(str(moved), str(work), preview=True, token_ceiling=100_000, max_images=0)
    assert "opened" in r and "(2 parts" in r, r
    sid2 = host.active
    r = await host.call_tool("apply_scene_ir", T.apply_scene_ir, sid2, MOVE)
    assert r.startswith("OK"), r
    d = await host.call_tool("deliver_package", T.deliver_package, sid2, {})
    assert d.startswith("DELIVERED"), d
    again = session(host.registry, sid2).last_export
    assert leaks(again, [str(mine), mine.name, str(ws), str(work), sid2, str(Path.home())]) == []


@pytest.mark.parametrize("kind", ["obj", "glb"])
def test_what_the_compiler_reads_of_a_carried_file_is_what_it_reads_of_the_original(tmp_path, kind):
    """Carrying the one file is lossless: names, points, faces and uvs read from the copy alone (no .mtl beside it, in
    another folder) equal those read from the original with its companions."""
    from antigravity3d.kernel.mesh_io import triangle_parts

    src = make_user_file(tmp_path / "orig", kind)
    lone = tmp_path / "shipped" / src.name
    lone.parent.mkdir()
    shutil.copy2(src, lone)
    a, b = list(triangle_parts(src)), list(triangle_parts(lone))
    assert len(a) == len(b) >= 1
    for (na, pa, fa, ua), (nb, pb, fb, ub) in zip(a, b):
        assert na == nb and np.array_equal(pa, pb) and np.array_equal(fa, fb)
        assert (ua is None) == (ub is None) and (ua is None or np.array_equal(ua, ub))


def test_what_agy3d_sanitize_ships_holds_nothing_of_the_input_directory_but_its_run_report_names_the_input(env, capsys):
    """The shipped files (the USD and the GLB of each input) carry no path. report.json, the run's own record, names each
    input by the path it was given (rows[].file; tests/test_sanitize_parallel.py pins that): found, not changed, because it
    is a log of a local run and not a file a package ships; the owner decides whether it should hold the bare name."""
    mine = env / "users_inbox_zq"
    mine.mkdir()
    (mine / "tet.ply").write_bytes(shapes.ply_bytes(np.array([(0, 0, 0), (0.1, 0, 0), (0, 0.1, 0), (0, 0, 0.1)], float),
                                                    np.array([(0, 2, 1), (0, 1, 3), (0, 3, 2), (1, 2, 3)])))
    out = env / "sanitized"
    cli.main(["sanitize", str(mine / "tet.ply"), "--engine", "godot_4", "--out", str(out)])  # sanitize takes no --offline: no model
    capsys.readouterr()
    assert (out / "report.json").is_file() and list(out.rglob("*.usda")) and list(out.rglob("*.glb"))
    assert {rel for rel, _ in leaks(out, [str(mine), mine.name])} == {"report.json"}


def glb_with(doc: dict) -> bytes:
    import struct

    js = json.dumps(doc).encode()
    js += b" " * (-len(js) % 4)
    return b"glTF" + struct.pack("<II", 2, 20 + len(js)) + struct.pack("<I", len(js)) + b"JSON" + js


def test_the_one_check_of_what_a_part_file_names(tmp_path):
    from antigravity3d.kernel.part_files import problem

    glb = make_user_file(tmp_path / "d", "glb").read_bytes()
    assert problem(".glb", glb) is None and problem(".ply", shapes.ply_bytes(np.zeros((3, 3)), np.array([(0, 1, 2)]))) is None
    assert problem(".stl", b"solid x\nendsolid x\n") is None
    assert problem(".obj", b"mtllib tet.mtl\nv 0 0 0\n") is None
    for suffix, data in ((".obj", b"mtllib ../../decoy.mtl\n"), (".obj", b"mtllib /etc/passwd\n"), (".obj", b"mtllib a.mtl b/c.mtl\n"),
                         (".obj", b"mtllib C:\\x.mtl\n"), (".obj", b"mtllib file:x.mtl\n"), (".obj", b"mtllib\n"),
                         (".ply", b"ply\ncomment TextureFile ../../decoy.png\nend_header\n"),
                         (".glb", glb_with({"asset": {"version": "2.0"}, "buffers": [{"uri": "../decoy.bin", "byteLength": 1}]})),
                         (".glb", glb_with({"asset": {"version": "2.0"}, "images": [{"uri": "http://x/y.png"}]})),
                         (".glb", b"not a glb at all, a USD layer: #usda 1.0"),
                         (".gltf", b"{}"), (".usda", b"#usda 1.0\n"), (".usdc", b"PXR-USDC"), (".fbx", b""), ("", b"")):
        assert problem(suffix, data), (suffix, data[:30])
    assert problem(".glb", glb_with({"asset": {"version": "2.0"}, "buffers": [{"uri": "data:application/octet-stream;base64,AAAA", "byteLength": 3}]})) is None


# --------------------------------------------- a package is input: what its part file names is read from the bytes, not opened

class Opened:
    """Records what is opened while a package is read: python opens (audit hook) and the USD openers (patched to record)."""

    def __init__(self, monkeypatch, needle: str):
        import sys

        from pxr import Sdf, Usd

        self.needle, self.hits, self.live = needle, [], True
        sys.addaudithook(lambda ev, args: self.hits.append(str(args[0])) if ev == "open" and self.live and needle in str(args[0]) else None)

        def wrap(real, what):
            def f(*a, **k):
                if any(needle in str(x) or "/parts/" in str(x) for x in a):
                    self.hits.append(f"{what}({a[0]})")
                return real(*a, **k)
            return staticmethod(f)

        for cls, name in ((Usd.Stage, "Open"), (Sdf.Layer, "FindOrOpen"), (Sdf.Layer, "Find")):
            monkeypatch.setattr(cls, name, wrap(getattr(cls, name), f"{cls.__name__}.{name}"))

    def close(self):
        self.live = False


def plant(pkg: Path, node: str, suffix: str, content: bytes) -> str:
    """Replace ``node``'s part file by ``content`` under the writer's own name pattern (the right name and the right hash)."""
    next((pkg / "parts").glob(f"{node}-*")).unlink()
    name = f"{node}-{hashlib.sha256(content).hexdigest()[:12]}{suffix}"
    (pkg / "parts" / name).write_bytes(content)
    _usd_scene_path(pkg, f"parts/{name}", node)
    return name


DECOY_USDA = """#usda 1.0
(
    defaultPrim = "part"
    subLayers = [@{d}@]
)
def Xform "part" (
    references = @{d}@
    payload = @{d}@
)
{{
}}
"""


@pytest.mark.parametrize("suffix,make", [
    (".usda", lambda d: DECOY_USDA.format(d=d).encode()),                      # the reviewer's case: sublayer, reference, payload
    (".usd", lambda d: DECOY_USDA.format(d=d).encode()),
    (".glb", lambda d: glb_with({"asset": {"version": "2.0"}, "buffers": [{"uri": d, "byteLength": 4}]})),
    (".obj", lambda d: f"mtllib ../../../{Path(d).name}\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n".encode()),
    (".ply", lambda d: f"ply\nformat ascii 1.0\ncomment TextureFile {d}\nelement vertex 0\nend_header\n".encode()),
    (".gltf", lambda d: b'{"asset": {"version": "2.0"}}'),
    (".fbx", lambda d: b"x"),
], ids=["usda with sublayer+reference+payload", "usd likewise", "glb external buffer", "obj mtllib climbing out", "ply TextureFile path",
        "gltf", "suffix outside the set"])
async def test_a_part_file_that_names_another_file_is_refused_before_anything_opens_it(env, monkeypatch, suffix, make):
    _, pkg, _ = await script_package(env, "ws_decoy")
    decoy = env / "decoy_outside_zq" / "decoy.usda"
    decoy.parent.mkdir()
    decoy.write_text("this file must never be opened")
    plant(pkg, "knob", suffix, make(str(decoy)))
    rec = Opened(monkeypatch, "decoy_outside_zq")
    try:
        with pytest.raises(E.EditError, match=r"part knob: .*(travels alone|is not parts/knob-)"):
            E.load_shipped(pkg)
        # and by the server's own way in: a refusal sentence naming the part, not a loader's error
        host = M.StudioHost(script=False)
        with pytest.raises(ValueError, match="part knob"):  # open_existing_asset turns an EditError into its ValueError sentence
            await host.open_existing_asset(str(pkg), str(env / "w2"), preview=True, token_ceiling=100_000, max_images=0)
    finally:
        rec.close()
    assert rec.hits == [], rec.hits


async def test_a_part_that_passes_the_check_but_is_no_mesh_is_a_refusal_not_a_loader_error(env):
    _, pkg, _ = await script_package(env, "ws_nomesh")
    plant(pkg, "knob", ".ply", b"ply\nformat ascii 1.0\nelement vertex 0\nend_header\n")
    with pytest.raises(E.EditError, match=r"part knob: .* cannot be read as a mesh"):
        E.source_materials(E.load_shipped(pkg), env / "inputs")


# ------------------------------------------------- one rule for where a relative mesh_ref path is read from

async def test_a_relative_part_path_is_read_from_the_stages_folder_everywhere_not_from_the_working_directory(env, monkeypatch):
    """Default mode, params.path "supplied/knob.ply": the compiler reads it against the folder of the stage's root layer
    (compile_ir.mesh_ref_file). The writer, the provenance and the triangle count once did their own Path(raw) and read it
    against the working directory: with a decoy file at that path under the cwd the package shipped the decoy as the part.
    (Script mode cannot make a relative path: build.handoff makes every mesh_ref path absolute.)"""
    from antigravity3d.tactics.authoring import count_tris

    cwd = env / "cwd_with_a_decoy"
    (cwd / "supplied").mkdir(parents=True)
    pts = np.array([(0, 0, 0), (0.1, 0, 0), (0, 0.1, 0), (0, 0, 0.1)], float)
    tri = np.array([(0, 2, 1), (0, 1, 3), (0, 3, 2), (1, 2, 3)])
    real = shapes.ply_bytes(pts, tri)
    decoy = shapes.ply_bytes(pts * 5, tri)
    (cwd / "supplied" / "knob.ply").write_bytes(decoy)
    monkeypatch.chdir(cwd)
    ws = env / "ws_rel"
    ws.mkdir()
    host = M.StudioHost(script=False)
    sid = await host._open("crate", "godot_4", 4000, "", str(ws), "", "", False, True, 100_000, 0, "")
    s = session(host.registry, sid)
    folder = Path(s.stage.stage.GetRootLayer().realPath).parent
    (folder / "supplied").mkdir()
    (folder / "supplied" / "knob.ply").write_bytes(real)
    ops = [{"op": "upsert_node", "node": {"id": "body", "kind": "box", "params": {"size": [1.0, 0.8, 0.6]}, "transform": {"translate": [0, 0, 0.3]}}},
           {"op": "upsert_node", "node": {"id": "knob", "kind": "mesh_ref", "params": {"path": "supplied/knob.ply"},
                                          "transform": {"translate": [0, 0, 0.6]}}}]
    r = await host.call_tool("apply_scene_ir", T.apply_scene_ir, sid, {"ops": ops})
    assert r.startswith("OK"), r
    assert count_tris(s.stage.base_ir(), s.stage.mesh_ref_file) == 12 + 4  # the stage's file: 4 triangles, a box's 12
    d = await host.call_tool("deliver_package", T.deliver_package, sid, {"dest": "output"})
    assert d.startswith("DELIVERED"), d
    pkg = s.last_export
    man = json.loads((pkg / "manifest.json").read_text())
    (name,) = man["parts"]["files"]
    assert "not_portable" not in man["parts"]
    assert (pkg / "parts" / name).read_bytes() == real and hashlib.sha256(real).hexdigest()[:12] in name  # not the decoy
    assert [k for k in man["provenance"]["sources"] if k.startswith("supplied:")] == ["supplied:knob.ply"]
    assert man["provenance"]["sources"]["supplied:knob.ply"]["sha256"] == hashlib.sha256(real).hexdigest()
    # the package edits: moved, everything of the session gone
    moved = env / "elsewhere" / "crate"
    shutil.copytree(pkg, moved)
    shutil.rmtree(ws)
    shutil.rmtree(cwd)
    work = env / "w3"
    work.mkdir()
    monkeypatch.chdir(env)
    host2 = M.StudioHost(script=False)
    r = await host2.open_existing_asset(str(moved), str(work), preview=True, token_ceiling=100_000, max_images=0)
    assert "(2 parts" in r, r
    r = await host2.call_tool("apply_scene_ir", T.apply_scene_ir, host2.active, MOVE)
    assert r.startswith("OK"), r
    assert (await host2.call_tool("deliver_package", T.deliver_package, host2.active, {})).startswith("DELIVERED")


# ----------------------------------------------- a package's own USD is read alone, and refused when it declares other files

def _declare(pkg: Path, kind: str, decoy: Path) -> None:
    """Make the package's crate.usd declare ``decoy`` as a sublayer, a reference or a payload (a hostile package)."""
    from pxr import Sdf

    layer = Sdf.Layer.FindOrOpen(str(pkg / "crate.usd"))
    if kind == "sublayer":
        layer.subLayerPaths.append(str(decoy))
    else:
        spec = layer.GetPrimAtPath(f"/{layer.defaultPrim}")
        (spec.referenceList.Add(Sdf.Reference(str(decoy))) if kind == "reference" else spec.payloadList.Add(Sdf.Payload(str(decoy))))
    layer.Save()


@pytest.mark.parametrize("kind", ["sublayer", "reference", "payload"])
async def test_a_package_usd_that_declares_another_file_is_refused_before_anything_composes_it(env, monkeypatch, kind):
    from pxr import Sdf, Usd

    _, pkg, _ = await script_package(env, "ws_usd_decl")
    decoy = env / "decoy_outside_zq" / "decoy.usda"
    decoy.parent.mkdir()
    decoy.write_text('#usda 1.0\ndef Xform "x"\n{\n}\n')
    _declare(pkg, kind, decoy)
    loaded = lambda: {l.identifier for l in Sdf.Layer.GetLoadedLayers() if "decoy_outside_zq" in l.identifier}  # noqa: E731
    before = loaded()
    stages = []
    real = Usd.Stage.Open
    monkeypatch.setattr(Usd.Stage, "Open", staticmethod(lambda *a, **k: (stages.append(a), real(*a, **k))[1]))
    rec = Opened(monkeypatch, "decoy_outside_zq")
    try:
        with pytest.raises(E.EditError, match=rf"the package's USD: crate.usd declares other files to open \(decoy.usda\)"):
            E.load_shipped(pkg)
        host = M.StudioHost(script=False)
        with pytest.raises(ValueError, match="declares other files to open"):
            await host.open_existing_asset(str(pkg), str(env / "w4"), preview=True, token_ceiling=100_000, max_images=0)
    finally:
        rec.close()
    assert stages == [] and loaded() == before and [h for h in rec.hits if "decoy" in h] == []  # no stage, the decoy never loaded


def test_the_run_folders_authoring_usd_is_read_alone_too_and_an_internal_reference_is_not_a_declared_file(tmp_path):
    from antigravity3d.ir import MaterialIR, NodeIR, SceneIR, Transform
    from antigravity3d.kernel.usd_stage import StageManager

    sm = StageManager.create(tmp_path / "authoring.usda", "Order")
    sm.apply_ir(SceneIR(asset_name="Order", materials=[MaterialIR(id="m", base_color=(0.5, 0.5, 0.5))],
                        nodes=[NodeIR(id="a", kind="box", params={"size": (0.1, 0.1, 0.1)}, transform=Transform(translate=(0, 0, 0)), material_id="m")]))
    sm.commit_all()
    assert E._flat_layer(tmp_path / "authoring.usda", "the run folder's authoring USD")  # working stage: refs </__agy_base>, no file
    _declare_path = tmp_path / "authoring.usda"
    from pxr import Sdf

    layer = Sdf.Layer.FindOrOpen(str(_declare_path))
    layer.subLayerPaths.append(str(tmp_path / "other.usda"))
    layer.Save()
    with pytest.raises(E.EditError, match="the run folder's authoring USD: authoring.usda declares other files to open"):
        E._flat_layer(_declare_path, "the run folder's authoring USD")


# ------------------------------------------------------------------------ bad input is a refusal naming the part, never a traceback

def _manifest(pkg: Path, mutate) -> None:
    man = json.loads((pkg / "manifest.json").read_text())
    mutate(man)
    (pkg / "manifest.json").write_text(json.dumps(man))


@pytest.mark.parametrize("mutate,ident", [
    (lambda m: m.__setitem__("parts", "parts"), "parts string"),
    (lambda m: m.__setitem__("parts", ["parts/knob"]), "parts list"),
    (lambda m: m.__setitem__("parts", False), "parts false"),
    (lambda m: m.__setitem__("parts", 7), "parts number"),
    (lambda m: m["parts"].__setitem__("not_portable", ["knob"]), "not_portable list"),
    (lambda m: m["parts"].__setitem__("not_portable", "knob"), "not_portable string"),
    (lambda m: m["parts"].__setitem__("not_portable", None), "not_portable null"),
], ids=lambda x: x if isinstance(x, str) else "")
async def test_a_manifest_whose_parts_record_lies_is_an_edit_error_naming_the_part(env, mutate, ident):
    _, pkg, _ = await script_package(env, "ws_lie")
    _manifest(pkg, mutate)
    with pytest.raises(E.EditError, match=r"part (knob|knob2): the package manifest's parts record is not an object"):
        E.load_shipped(pkg)
    host = M.StudioHost(script=False)
    with pytest.raises(ValueError, match="part knob"):
        await host.open_existing_asset(str(pkg), str(env / "w5"), preview=True, token_ceiling=100_000, max_images=0)


async def test_a_manifest_that_is_not_json_or_not_an_object_is_an_edit_error(env):
    _, pkg, _ = await script_package(env, "ws_lie2")
    (pkg / "manifest.json").write_text("{not json")
    with pytest.raises(E.EditError, match="not valid JSON"):
        E.load_shipped(pkg)
    (pkg / "manifest.json").write_text("[1, 2]")
    with pytest.raises(E.EditError, match="not an object naming its asset"):
        E.load_shipped(pkg)


@pytest.mark.parametrize("case", ["notmesh", "exe", "parts_string"])
def test_agy3d_edit_of_a_hostile_package_prints_a_refusal_not_a_traceback_and_copies_nothing(env, capsys, case):
    import asyncio

    ws, pkg, _ = asyncio.run(script_package(env, "ws_cli_" + case))
    moved = env / f"hostile_{case}" / "crate"
    shutil.copytree(pkg, moved)
    if case == "notmesh":
        plant(moved, "knob", ".ply", b"ply\nformat ascii 1.0\nelement vertex 999999999\nend_header\n")
    elif case == "exe":
        plant(moved, "knob", ".exe", next((moved / "parts").glob("knob2-*")).read_bytes())
    else:
        _manifest(moved, lambda m: m.__setitem__("parts", "parts"))
    patch = env / "ops.json"
    patch.write_text(json.dumps(MOVE))
    rc = cli.main(["edit", str(moved), "lift", "--patch", str(patch), "--offline"])
    cap = capsys.readouterr()
    assert rc != 0 and "Traceback" not in cap.out + cap.err and "part knob" in cap.err + cap.out, cap.err + cap.out
    assert not list(env.glob(f"hostile_{case}/**/inputs"))  # nothing copied into an edit's work folder


# ------------------------------------------------------------------------------- the bytes checked are the bytes copied

async def test_a_part_is_read_once_and_the_edit_works_on_the_bytes_that_were_checked(env, monkeypatch):
    _, pkg, _ = await script_package(env, "ws_once")
    reads = []
    real = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: (reads.append(self.name), real(self))[1])
    shipped = E.load_shipped(pkg)
    parts = [n for n in reads if n.startswith("knob")]
    assert sorted(parts) == sorted(p.name for p in (pkg / "parts").iterdir())  # one read each, in load_shipped
    knob = next((pkg / "parts").glob("knob-*"))
    knob.write_bytes(b"swapped after the check")  # a file changed between the check and the copy
    scene, _ = E.source_materials(shipped, env / "inputs")
    assert sorted(n for n in reads if n.startswith("knob")) == sorted(parts)  # source_materials read none again
    copied = Path(next(n.params["path"] for n in scene.nodes if n.id == "knob"))
    assert hashlib.sha256(copied.read_bytes()).hexdigest()[:12] in copied.name  # the checked bytes, not the swapped file
