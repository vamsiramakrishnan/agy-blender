"""An install must not write the machine it ran on into the game project: no file of either engine's install holds the
home directory, the user's name or the temp root, and what the package's location was for (a later check of a three.js
install comparing the loader with the package) still works from the record's own name for it."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from antigravity3d.deliver import install_package
from antigravity3d.deliver import install as INS
from antigravity3d.deliver import web_check as WC
from antigravity3d.deliver import web_install as WI
from antigravity3d.deliver.install import RECORD, Check, check_install, uninstall
from antigravity3d.deliver.portable import locate_package, package_ref, scrub

THREE = "0.170.0"
USER = "alice"


def _build(tmp_path_factory, engine: str) -> Path:
    from antigravity3d.cli import main

    out = tmp_path_factory.mktemp(f"crate_{engine}")
    assert main(["prop", "wooden crate with iron brackets", "--offline", "--engine", engine, "--out", str(out)]) == 0
    (pkg,) = out.glob("runs/*/package")
    return pkg


@pytest.fixture(scope="module")
def godot_crate(tmp_path_factory) -> Path:
    return _build(tmp_path_factory, "godot_4")


@pytest.fixture(scope="module")
def web_crate(tmp_path_factory) -> Path:
    return _build(tmp_path_factory, "web_three")


@pytest.fixture()
def home(tmp_path, monkeypatch) -> Path:
    """A folder standing in for the user's home directory, and the user's name that goes with it."""
    h = tmp_path / "home" / USER
    h.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    for k, v in (("HOME", str(h)), ("USER", USER), ("LOGNAME", USER), ("USERNAME", USER)):
        monkeypatch.setenv(k, v)
    return h


def _godot_project(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "project.godot").write_text('config_version=5\n\n[application]\n\nconfig/name="t"\n'
                                        'config/features=PackedStringArray("4.7", "Forward Plus")\n')
    return root


def _web_project(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "package.json").write_text(json.dumps({"name": "g", "dependencies": {"three": THREE, "vite": "6.0.7"}}))
    (root / "index.html").write_text("<html></html>")
    (root / "public").mkdir()
    (root / "src").mkdir()
    (root / "src" / "main.js").write_text("import 'three';\n")
    return root


def _leaks(project: Path, tmp_path: Path, home: Path) -> list[str]:
    """Every file under the project that holds the home directory, the user's name or the temp root."""
    temp = str(Path(tempfile.gettempdir()).resolve())
    needles = [str(home), USER, str(tmp_path), temp + "/", temp + "\\", str(project), str(project.resolve())]
    hits = []
    for d, _dirs, files in os.walk(project):
        for f in files:
            p = Path(d) / f
            data = p.read_bytes().lower()
            for n in needles:
                if n.lower().encode() in data:
                    hits.append(f"{p.relative_to(project)}: {n}")
    return hits


def _written(project: Path) -> int:
    return sum(len(f) for _d, _s, f in os.walk(project))


def _fake_web_check(monkeypatch, seen: list):
    """A browser that measures: it records the package folder it was given, and puts the paths of this machine into what it
    reports (a log line, a screenshot) the way a real engine's output does."""
    async def run(project, record, manifest, pkg, three_path, Check, **kw):
        seen.append(pkg)
        return [Check("import", "passed", f"loaded {project}/x from {pkg}")], {
            "screenshot": b"\x89PNG", "log": f"served {project} and {pkg} from {tempfile.gettempdir()}/agy3d-check-home-1"}

    monkeypatch.setattr(WC, "run", run)
    monkeypatch.setattr(WI, "_cannot_measure", lambda check, three: None)


def _fake_godot_check(monkeypatch, tmp_path):
    shot = tmp_path / "scratch" / "godot.png"
    shot.parent.mkdir(exist_ok=True)
    shot.write_bytes(b"\x89PNG")
    from antigravity3d.deliver import godot

    monkeypatch.setattr(godot, "find_godot", lambda b=None: "/usr/bin/fake-godot")
    monkeypatch.setattr(godot, "godot_version", lambda exe: "4.7")
    monkeypatch.setattr(godot, "run_check", lambda exe, project, rel, rec, work, **kw: {
        "render": {"screenshot": str(shot)}, "import": {"log_tail": f"imported {project}"}, "where": str(project / rel)})
    monkeypatch.setattr(INS, "godot_checks", lambda manifest, record, measured: [
        Check("scene", "passed", f"loaded from {record['dest']} in {tmp_path}")])


# ---------------------------------------------------------------- what an install writes, scanned


@pytest.mark.parametrize("where", ["beside_under_home", "outside_home"])
def test_godot_install_writes_nothing_of_this_machine(where, godot_crate, home, tmp_path, monkeypatch):
    pkg = home / "pkgs" / "crate"
    shutil.copytree(godot_crate, pkg)
    project = _godot_project((home / "games" if where == "beside_under_home" else tmp_path / "srv") / "game")
    _fake_godot_check(monkeypatch, tmp_path)
    r = install_package(pkg, project, check=True)
    assert r.status == "installed", r.message
    assert r.screenshot and (Path(r.screenshot)).is_file() and Path(r.report_path).is_file()
    assert _written(project) > 5
    assert _leaks(project, tmp_path, home) == []
    ref = json.loads((project / r.dest / RECORD).read_text())["package"]
    if where == "beside_under_home":
        assert ref["path"] == "../../pkgs/crate"
    else:
        assert ref["path"] is None and ref["folder"] == "crate" and ref["manifest_sha256"]
    # the terminal keeps what was typed
    assert r.package["path"] == str(pkg)


@pytest.mark.parametrize("where", ["beside_under_home", "outside_home"])
def test_web_install_writes_nothing_of_this_machine(where, web_crate, home, tmp_path, monkeypatch):
    pkg = home / "pkgs" / "crate"
    shutil.copytree(web_crate, pkg)
    project = _web_project((home / "games" if where == "beside_under_home" else tmp_path / "srv") / "game")
    seen: list = []
    _fake_web_check(monkeypatch, seen)
    r = install_package(pkg, project, check=True, compress=False)
    assert r.status == "installed", r.message
    assert Path(r.report_path).is_file() and seen
    assert _written(project) > 5
    assert _leaks(project, tmp_path, home) == []
    rec = json.loads((project / r.dest / RECORD).read_text())
    assert (rec["package"]["path"] is None) == (where == "outside_home")
    # a later check still reads the package where the record says it is, and says so when it cannot
    again = check_install(project, r.dest)
    assert again.status == "unchanged" and seen[-1] == (pkg.resolve() if where == "beside_under_home" else None)
    if where == "outside_home":
        assert any("pass it again" in n for n in again.notes)
    assert _leaks(project, tmp_path, home) == []


def test_the_scan_sees_a_leak_when_there_is_one(home, tmp_path):
    """Negative control: the scan is not blind."""
    project = tmp_path / "p"
    project.mkdir()
    (project / "x.json").write_text(json.dumps({"path": str(home / "pkgs")}))
    assert _leaks(project, tmp_path, home)


# ---------------------------------------------------------------- how the record names the package: each branch


def _pkg(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "manifest.json").write_text('{"asset": "a"}')
    return root


def _ref(pkg: Path, project: Path) -> dict:
    return package_ref(pkg, project, "h")


def test_a_package_inside_the_project_is_named_relative(home, tmp_path):
    project = tmp_path / "game"  # not under home: still fine, the path only descends
    pkg = _pkg(project / "vendor" / "crate")
    assert _ref(pkg, project)["path"] == "vendor/crate"


def test_siblings_under_one_home_are_named_relative(home, tmp_path):
    ref = _ref(_pkg(home / "pkgs" / "crate"), _godot_project(home / "games" / "g"))
    assert ref == {"path": "../../pkgs/crate", "path_base": "project", "folder": "crate", "manifest_sha256": "h"}


def test_siblings_outside_any_home_are_named_relative(home, tmp_path):
    ref = _ref(_pkg(tmp_path / "srv" / "packages" / "crate"), _godot_project(tmp_path / "srv" / "game"))
    assert ref["path"] == "../packages/crate"


def test_a_package_that_climbs_to_above_home_names_no_path(home, tmp_path):
    ref = _ref(_pkg(home / "pkgs" / "crate"), _godot_project(tmp_path / "srv" / "game"))  # common ancestor: tmp_path
    assert ref["path"] is None and ref["folder"] == "crate"


def test_a_package_in_another_users_home_names_no_path(home, tmp_path):
    other = tmp_path / "home" / "bob"
    ref = _ref(_pkg(other / "pkgs" / "crate"), _godot_project(home / "games" / "g"))  # common ancestor: the folder of homes
    assert ref["path"] is None and "bob" not in json.dumps(ref)


def test_a_root_as_the_only_common_ancestor_names_no_path(home, tmp_path, monkeypatch):
    monkeypatch.setattr(os.path, "commonpath", lambda paths: os.sep)
    assert _ref(_pkg(tmp_path / "a" / "crate"), _godot_project(tmp_path / "b" / "g"))["path"] is None


def test_an_unknown_home_names_no_path_unless_the_package_is_inside_the_project(tmp_path, monkeypatch):
    def nohome(cls):
        raise RuntimeError("no home")

    monkeypatch.setattr(Path, "home", classmethod(nohome))
    assert _ref(_pkg(tmp_path / "srv" / "crate"), _godot_project(tmp_path / "srv2" / "g"))["path"] is None
    proj = _godot_project(tmp_path / "srv3" / "g")
    assert _ref(_pkg(proj / "crate"), proj)["path"] == "crate"


def test_a_package_reached_through_a_link_names_no_path(home, tmp_path):
    real = _pkg(tmp_path / "srv" / "realname" / "crate")
    link = tmp_path / "srv" / "linkname"
    link.symlink_to(real.parent, target_is_directory=True)
    project = _godot_project(tmp_path / "srv" / "game")
    assert _ref(link / "crate", project)["path"] is None
    assert _ref(real, project)["path"] == "../realname/crate"


# ---------------------------------------------------------------- finding the package again from the record


def _record(project: Path, pkg: Path, sha: str | None = None, path: str | None = "") -> dict:
    ref = package_ref(pkg, project, sha or INS._sha_file(pkg / "manifest.json"))
    if path != "":
        ref["path"] = path
    return {"package": ref}


def test_locate_finds_the_package_where_it_was_and_only_that_package(home, tmp_path):
    project = _godot_project(home / "games" / "g")
    pkg = _pkg(home / "pkgs" / "crate")
    rec = _record(project, pkg)
    assert locate_package(project, rec) == (pkg.resolve(), None)
    (pkg / "manifest.json").write_text('{"asset": "other"}')  # a different package at the same place
    found, note = locate_package(project, rec)
    assert found is None and "pass it again" in note and "another version" in note
    shutil.rmtree(pkg)  # a moved package
    found, note = locate_package(project, rec)
    assert found is None and "not found from this record" in note and "'crate'" in note


def test_an_absolute_path_from_an_earlier_install_still_locates(home, tmp_path):
    project = _godot_project(home / "games" / "g")
    pkg = _pkg(tmp_path / "elsewhere" / "crate")
    assert locate_package(project, _record(project, pkg, path=str(pkg)))[0] == pkg


@pytest.mark.parametrize("path", [None, "", 5, "../../../../../../../../etc", "nothing/here", "x\0y", ["a"]])
def test_an_edited_record_is_read_never_followed_into_anything(path, home, tmp_path):
    project = _godot_project(home / "games" / "g")
    rec = {"package": {"path": path, "manifest_sha256": "h"}}
    before = sorted(p for p in tmp_path.rglob("*"))
    assert locate_package(project, rec)[0] is None
    assert sorted(p for p in tmp_path.rglob("*")) == before


def test_a_forged_path_to_a_folder_with_another_manifest_is_not_used(home, tmp_path):
    project = _godot_project(home / "games" / "g")
    other = _pkg(home / "pkgs" / "other")
    rec = {"package": {"path": "../../pkgs/other", "manifest_sha256": "0" * 64}}
    assert locate_package(project, rec)[0] is None and other.is_dir()


def test_a_record_written_before_this_change_still_checks_and_uninstalls(web_crate, home, tmp_path, monkeypatch):
    project = _web_project(tmp_path / "srv" / "game")
    seen: list = []
    _fake_web_check(monkeypatch, seen)
    r = install_package(web_crate, project, check=False, compress=False)
    path = project / r.dest / RECORD
    rec = json.loads(path.read_text())
    rec["package"] = {**rec["package"], "path": str(web_crate.resolve())}  # as the earlier version wrote it
    rec["package"].pop("folder", None)
    path.write_text(json.dumps(rec))
    again = check_install(project, r.dest)
    assert again.status == "unchanged" and seen[-1] == web_crate.resolve()
    assert uninstall(project, r.dest).status == "uninstalled"


def test_scrub_replaces_roots_longest_first_and_the_home_and_temp_roots(home, tmp_path):
    out = scrub({"a": [f"{tmp_path}/game/x", f"{home}/y", f"{tempfile.gettempdir()}/z"]}, {str(tmp_path / "game"): "<project>"})
    # the project is under the temp root and the home too: the longest root wins in each case
    assert out == {"a": ["<project>/x", "<home>/y", "<tmp>/z"]}
