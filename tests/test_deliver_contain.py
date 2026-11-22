"""agy3d install reads only inside the package and writes only inside DEST, whatever a manifest or an install record says.

Each test builds a hostile package or a tampered destination, runs install / update / uninstall, and requires a
refusal naming the entry and no change anywhere outside DEST (a file planted beside the project stays as it was,
nothing appears next to it)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from antigravity3d.deliver import install_package, uninstall
from antigravity3d.deliver.install import RECORD
from test_deliver import GODOT4, _copy_pkg, _project, _sha, crate  # noqa: F401  (crate is a fixture)

DEST = "agy3d/WoodenCrateIron"


def _snapshot(root: Path, skip: Path | None = None) -> dict[str, str]:
    """Every file and symlink under root (outside ``skip``), with its content hash or link target."""
    out = {}
    for p in sorted(root.rglob("*")):
        if skip is not None and (p == skip or skip in p.parents):
            continue
        if p.is_symlink():
            out[p.relative_to(root).as_posix()] = "-> " + os.readlink(p)
        elif p.is_file():
            out[p.relative_to(root).as_posix()] = _sha(p)
    return out


def _world(tmp_path: Path) -> tuple[Path, Path]:
    """A project and a victim file beside it (outside DEST and outside the project)."""
    project = _project(tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    return project, victim


def _manifest_edit(fn):
    return lambda m, root: fn(m, root)


def _rehash(m: dict, root: Path, rel: str) -> None:
    m["file_hashes"][rel] = {"sha256": _sha(root / rel), "bytes": (root / rel).stat().st_size}


def _refused_and_untouched(r, tmp_path: Path, before: dict[str, str], dest: Path, needle: str) -> None:
    assert r.status == "refused", (r.status, r.message)
    assert needle in r.message, r.message
    assert _snapshot(tmp_path, skip=dest) == before, "a file outside DEST changed"


# --------------------------------------------------------------------------- names in a package's manifest


@pytest.mark.parametrize("entry", [
    "textures/../../../injected.gd",                 # climbs out of DEST (and, on the read side, out of the package)
    "textures/../../../../injected.gd",
    "textures/./x.png", "textures//x.png", "textures/",  # dot or empty segments
    "textures/sub\\..\\..\\x.png",                   # a backslash (a separator on Windows)
    "textures/C:x.png",                              # a drive
])
def test_a_manifest_entry_that_is_not_a_plain_relative_name_is_refused(crate, tmp_path, entry):
    def add(m, root):
        src = root / entry
        if not entry.endswith("/"):
            try:
                src.parent.mkdir(parents=True, exist_ok=True)
                src.write_bytes(b"extends Node\n# injected\n")  # the source exists, wherever the name points
            except OSError:
                pass
        m["files"].append(entry)

    pkg = _copy_pkg(crate, tmp_path / "pkgs" / "src" / "pkg", add)
    project, victim = _world(tmp_path)
    before = _snapshot(tmp_path, skip=project / DEST)
    r = install_package(pkg, project, check=False)
    _refused_and_untouched(r, tmp_path, before, project / DEST, entry)
    assert not (project / DEST).exists()


def test_an_absolute_manifest_entry_is_refused(crate, tmp_path):
    outside = tmp_path / "abs_target"
    outside.mkdir()
    target = outside / "x.png"
    target.write_bytes(b"theirs")
    pkg = _copy_pkg(crate, tmp_path / "pkg", lambda m, root: m["files"].append(str(target)))
    project, _ = _world(tmp_path)
    before = _snapshot(tmp_path, skip=project / DEST)
    r = install_package(pkg, project, check=False)
    _refused_and_untouched(r, tmp_path, before, project / DEST, str(target))


def test_an_asset_name_that_is_not_an_identifier_is_refused(crate, tmp_path):
    """The asset name makes file names (<asset>.glb, .tscn): one with a path in it is refused, also with --dest."""
    def rename(m, root):
        old = m["asset"]
        m["asset"] = "../../../Evil"
        for f in list(m["files"]):
            if f.startswith(old):
                new = "../../../Evil" + f[len(old):]
                (root / new).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(root / f, root / new)
                m["files"].append(new)
                _rehash(m, root, new)

    pkg = _copy_pkg(crate, tmp_path / "a" / "b" / "c" / "pkg", rename)
    project, _ = _world(tmp_path)
    before = _snapshot(tmp_path, skip=project / "assets/x")
    r = install_package(pkg, project, "assets/x", check=False)
    _refused_and_untouched(r, tmp_path, before, project / "assets/x", "../../../Evil")


def test_a_gltf_material_name_cannot_name_a_file_outside_dest(crate, tmp_path):
    """Material names come from the GLB and become materials/<name>.tres (the files Godot extracts, which uninstall
    removes): a name with a path in it is refused at install."""
    from antigravity3d.deliver.glb import _join, _split

    def evil(m, root):
        for f in [f for f in m["files"] if f.endswith(".glb") and "_colliders" not in f]:
            doc, blob = _split((root / f).read_bytes())
            for mat in doc.get("materials") or []:
                mat["name"] = "../../../../victim"
            (root / f).write_bytes(_join(doc, blob))
            _rehash(m, root, f)

    pkg = _copy_pkg(crate, tmp_path / "pkg", evil)
    project, victim = _world(tmp_path)
    before = _snapshot(tmp_path, skip=project / DEST)
    r = install_package(pkg, project, check=False)
    _refused_and_untouched(r, tmp_path, before, project / DEST, "../../../../victim")


def test_a_package_file_that_is_a_symlink_out_of_the_package_is_refused(crate, tmp_path):
    """The bytes are the expected ones (the hash matches), but they are read from outside the package."""
    m = json.loads((crate / "manifest.json").read_text())
    rel = next(t["path"] for t in m["textures"] if t["role"] == "albedo")
    pkg = _copy_pkg(crate, tmp_path / "pkg")
    outside = tmp_path / "elsewhere.png"
    shutil.copyfile(pkg / rel, outside)
    (pkg / rel).unlink()
    (pkg / rel).symlink_to(outside)
    project, _ = _world(tmp_path)
    before = _snapshot(tmp_path, skip=project / DEST)
    r = install_package(pkg, project, check=False)
    _refused_and_untouched(r, tmp_path, before, project / DEST, rel)


# --------------------------------------------------------------------------- a destination that leads elsewhere


def _with_extra_texture(crate: Path, root: Path) -> Path:
    """The crate's package with one more file under textures/ (an update then creates it)."""
    def extra(m, r):
        (r / "textures" / "extra.png").write_bytes((r / next(t["path"] for t in m["textures"])).read_bytes())
        m["files"].append("textures/extra.png")
        _rehash(m, r, "textures/extra.png")

    return _copy_pkg(crate, root, extra)


@pytest.mark.parametrize("sub", ["textures", "materials"])
def test_a_symlinked_folder_in_dest_does_not_redirect_writes(crate, tmp_path, sub):
    project, _ = _world(tmp_path)
    assert install_package(crate, project, check=False).status == "installed"
    dest = project / DEST
    elsewhere = tmp_path / f"elsewhere_{sub}"
    shutil.copytree(dest / sub, elsewhere)  # the same files, so nothing reads as edited
    shutil.rmtree(dest / sub)
    (dest / sub).symlink_to(elsewhere, target_is_directory=True)
    pkg2 = _with_extra_texture(crate, tmp_path / "pkg2")
    before = _snapshot(tmp_path, skip=dest)
    r = install_package(pkg2, project, check=False, fov_deg=75.0)
    _refused_and_untouched(r, tmp_path, before, dest, sub)


def test_uninstall_does_not_follow_a_symlinked_materials_folder(crate, tmp_path):
    project, _ = _world(tmp_path)
    assert install_package(crate, project, check=False).status == "installed"
    dest = project / DEST
    record = json.loads((dest / RECORD).read_text())
    elsewhere = tmp_path / "their_materials"
    elsewhere.mkdir()
    for g in record["engine_generated"]:
        (elsewhere / Path(g).name).write_text("their material")
    shutil.rmtree(dest / "materials")
    (dest / "materials").symlink_to(elsewhere, target_is_directory=True)
    before = _snapshot(tmp_path, skip=dest)
    r = uninstall(project, DEST)
    _refused_and_untouched(r, tmp_path, before, dest, "materials")


# --------------------------------------------------------------------------- a tampered install record


def _tamper(dest: Path, key: str, name: str, sha: str | None) -> None:
    rec = json.loads((dest / RECORD).read_text())
    if key == "files":
        rec["files"][name] = {"sha256": sha, "bytes": 7}
    else:
        rec[key] = [*rec.get(key, []), name]
    (dest / RECORD).write_text(json.dumps(rec))


@pytest.mark.parametrize("key", ["files", "engine_generated"])
@pytest.mark.parametrize("op", ["uninstall", "update"])
def test_a_tampered_record_cannot_remove_a_file_outside_dest(crate, tmp_path, key, op):
    project, victim = _world(tmp_path)
    assert install_package(crate, project, check=False).status == "installed"
    dest = project / DEST
    name = "../../../victim.txt"  # DEST is project/agy3d/WoodenCrateIron: three levels up is tmp_path
    assert (dest / name).resolve() == victim.resolve()
    _tamper(dest, key, name, hashlib.sha256(b"keep me").hexdigest())  # the hash an edit check would accept
    before = _snapshot(tmp_path, skip=dest)
    r = uninstall(project, DEST) if op == "uninstall" else install_package(crate, project, check=False, fov_deg=75.0)
    _refused_and_untouched(r, tmp_path, before, dest, name)
    assert victim.read_text() == "keep me"


@pytest.mark.parametrize("name", ["ABSOLUTE", "textures/../../x", ".agy3d/install.json", "a\\b"])
def test_a_tampered_record_with_a_bad_name_is_refused_on_uninstall(crate, tmp_path, name):
    # An absolute name is always one under this test's own tmp_path: the code under test may follow it, and an
    # absolute system path here once removed a real file on the machine running the tests.
    project, victim = _world(tmp_path)
    if name == "ABSOLUTE":
        name = str(victim)
    assert install_package(crate, project, check=False).status == "installed"
    dest = project / DEST
    _tamper(dest, "files", name, "0" * 64)
    before = _snapshot(tmp_path, skip=dest)
    r = uninstall(project, DEST, force=True)
    _refused_and_untouched(r, tmp_path, before, dest, name)


def test_a_folder_swapped_for_a_link_during_the_install_is_caught_at_the_write(crate, tmp_path):
    """The check is made at each write, not only when planning: after the first file is written the textures folder is
    replaced by a link to a folder outside; the next write into it is refused and everything is rolled back."""
    from antigravity3d.deliver.detect import detect_project
    from antigravity3d.deliver.install import Refused, _install_godot, read_package

    project, _ = _world(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    before = _snapshot(tmp_path, skip=project / DEST)
    dest = project / DEST
    swapped = {"done": False}

    def swap(path: Path) -> None:
        if not swapped["done"] and (dest / "textures").is_dir():
            shutil.rmtree(dest / "textures")
            (dest / "textures").symlink_to(outside, target_is_directory=True)
            swapped["done"] = True

    with pytest.raises(Refused, match="symbolic link"):
        _install_godot(crate, project, read_package(crate), detect_project(project), {}, None, False, False, False,
                       fov_deg=None, godot_bin=None, render=False, _write_hook=swap)
    assert swapped["done"]
    assert _snapshot(tmp_path, skip=project / DEST) == before and not any(outside.iterdir())


def test_check_install_refuses_a_destination_holding_a_link(crate, tmp_path):
    from antigravity3d.deliver import check_install

    project, _ = _world(tmp_path)
    assert install_package(crate, project, check=False).status == "installed"
    dest = project / DEST
    outside = tmp_path / "outside"
    shutil.copytree(dest / "textures", outside)
    shutil.rmtree(dest / "textures")
    (dest / "textures").symlink_to(outside, target_is_directory=True)
    r = check_install(project, DEST, render=False)
    assert r.status == "refused" and "symbolic link" in r.message and "textures" in r.message, r.message
