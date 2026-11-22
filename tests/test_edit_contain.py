"""``agy3d edit`` reads only inside the package (or the run folder named) and writes only in its work folder and the target.

A package is data from someone else: its manifest, the scene embedded in its USD and a run folder's report.json name
files. Each test plants such a name (or a symbolic link) and requires a refusal naming it, with nothing read from or
written to anywhere else. Every victim lives under the test's tmp_path."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from PIL import Image

from antigravity3d import cli
from antigravity3d.dags import edit_asset as E

CRATE = "a rugged military supply crate kit-bashed from panels, corner guards, latches and two side handles"


@pytest.fixture(scope="module")
def first_run(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("crate") / "out"
    assert cli.main(["prop", CRATE, "--offline", "--engine", "godot_4", "--out", str(out)]) == 0
    return out


def _bare(first_run: Path, tmp_path: Path) -> Path:
    pkg = tmp_path / "pkgs" / "Crate"
    shutil.copytree(E.find_package(first_run)[0], pkg)
    return pkg


def _secret_png(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), (200, 10, 10)).save(path)
    return path


def _manifest(pkg: Path, fn) -> None:
    m = json.loads((pkg / "manifest.json").read_text())
    fn(m)
    (pkg / "manifest.json").write_text(json.dumps(m))


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _set_ir_uris(pkg: Path, uri: str) -> None:
    """Every material of the scene embedded in the package's USD reads its albedo from ``uri``."""
    from pxr import Usd

    from antigravity3d.kernel.compile_ir import IR_KEY

    m = json.loads((pkg / "manifest.json").read_text())
    stage = Usd.Stage.Open(str(pkg / m["usd"]))
    prim = stage.GetDefaultPrim()
    ir = json.loads(prim.GetCustomDataByKey(IR_KEY))
    for mat in ir["materials"]:
        mat["albedo_uri"] = uri
    prim.SetCustomDataByKey(IR_KEY, json.dumps(ir))
    stage.GetRootLayer().Save()


def _no_secret_copied(root: Path, secret: Path) -> None:
    data = secret.read_bytes()
    assert not [p for p in root.rglob("*") if p.is_file() and p != secret and p.read_bytes() == data], "the secret was copied"


# --------------------------------------------------------------------------- names in a bare package


def test_an_atlas_source_outside_the_package_is_refused(first_run, tmp_path):
    secret = _secret_png(tmp_path / "secret.png")
    pkg = _bare(first_run, tmp_path)

    def point_out(m):
        for rec in m["texture_atlas"]["members"].values():
            for role in rec.get("sources") or {}:
                rec["sources"][role] = {"file": "../../secret.png", "sha256": _sha(secret)}

    _manifest(pkg, point_out)
    with pytest.raises(E.EditError, match=r"\.\./\.\./secret\.png"):
        sh = E.load_shipped(pkg)
        E.source_materials(sh, tmp_path / "work")
    _no_secret_copied(tmp_path, secret)


def test_a_material_uri_in_the_embedded_scene_cannot_leave_the_package_or_the_work_folder(first_run, tmp_path):
    planted = _secret_png(tmp_path / "planted.png")
    pkg = _bare(first_run, tmp_path)
    _manifest(pkg, lambda m: m["texture_atlas"].update(members={}))  # every material reads its own (packaged) maps
    _set_ir_uris(pkg, "./../../planted.png")
    work = tmp_path / "w1" / "w2" / "work"
    with pytest.raises(E.EditError, match=r"\.\./\.\./planted\.png"):
        sh = E.load_shipped(pkg)
        E.source_materials(sh, work)
    assert not (tmp_path / "w1" / "planted.png").exists()  # work / "../../planted.png"
    _no_secret_copied(tmp_path, planted)


def test_the_atlas_image_a_look_check_reads_stays_in_the_package(first_run, tmp_path):
    _secret_png(tmp_path / "secret.png")  # the file the name points at exists: only the rule stops the read
    pkg = _bare(first_run, tmp_path)
    _manifest(pkg, lambda m: m["texture_atlas"]["files"].update(albedo="../../secret.png"))
    with pytest.raises(E.EditError, match=r"\.\./\.\./secret\.png"):
        E.load_shipped(pkg)
    with pytest.raises(ValueError, match=r"\.\./\.\./secret\.png"):
        E.look_check(pkg, pkg, ["body"])


def test_a_manifest_usd_name_outside_the_package_is_refused(first_run, tmp_path):
    pkg = _bare(first_run, tmp_path)
    m = json.loads((pkg / "manifest.json").read_text())
    shutil.copyfile(pkg / m["usd"], tmp_path / "elsewhere.usd")
    _manifest(pkg, lambda m: m.update(usd="../../elsewhere.usd"))
    with pytest.raises(E.EditError, match=r"\.\./\.\./elsewhere\.usd"):
        E.load_shipped(pkg)


def test_a_symbolic_link_in_the_package_is_refused(first_run, tmp_path):
    """The history copy and the undo copy would follow it; the edit is refused before anything is read through it."""
    secret = _secret_png(tmp_path / "secret.png")
    pkg = _bare(first_run, tmp_path)
    (pkg / "textures" / "extra.png").symlink_to(secret)
    with pytest.raises(E.EditError, match="symbolic link"):
        E.load_shipped(pkg)


def test_undo_does_not_follow_a_link_in_the_history(first_run, tmp_path):
    """The kept copy of a replaced package is put back without following a link planted in it."""
    secret = _secret_png(tmp_path / "secret.png")
    pkg = _bare(first_run, tmp_path)
    saved = pkg.parent / f"{pkg.name}.history" / "001" / "package"
    shutil.copytree(pkg, saved)
    (saved / "textures" / "extra.png").symlink_to(secret)
    E.undo(pkg)
    assert not (pkg / "textures" / "extra.png").exists()
    _no_secret_copied(pkg, secret)


# --------------------------------------------------------------------------- a run folder's report


def test_a_report_naming_a_package_outside_the_run_folder_is_refused(first_run, tmp_path):
    out = tmp_path / "out"
    shutil.copytree(first_run, out)
    elsewhere = tmp_path / "elsewhere" / "pkg"
    shutil.copytree(E.find_package(first_run)[0], elsewhere)
    rep = json.loads((out / "report.json").read_text())
    ex = rep.get("extras", rep)
    m = json.loads((elsewhere / "manifest.json").read_text())
    ex["outputs"]["usd"] = str(elsewhere / m["usd"])
    (out / "report.json").write_text(json.dumps(rep))
    with pytest.raises(E.EditError, match="outside the run folder"):
        E.find_package(out)


def test_undo_does_not_restore_a_package_the_report_names_outside_the_run_folder(first_run, tmp_path):
    out = tmp_path / "out"
    shutil.copytree(first_run, out)
    pkg = E.find_package(out)[0]
    hist = out / "history" / "001"
    hist.mkdir(parents=True)
    shutil.copytree(pkg, hist / "package")
    elsewhere = tmp_path / "elsewhere" / "pkg"
    shutil.copytree(pkg, elsewhere)  # the same manifest bytes, outside the run folder
    rep = json.loads((out / "report.json").read_text())
    ex = rep.get("extras", rep)
    ex["outputs"]["manifest"] = str(elsewhere / "manifest.json")
    (hist / "report.json").write_text(json.dumps(rep))
    res = E.undo(out)
    assert Path(res["restored"]).is_relative_to(out), res
