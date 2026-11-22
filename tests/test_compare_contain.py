"""The comparison harness reads what a stock agent delivered, on the host, outside the agent's sandbox: nothing the
agent wrote (a URI in its GLB, a symbolic link in its folder, a path it reported) may make the harness read outside
the agent's delivered folder or write outside the evaluation folder. Every victim lives under the test's tmp_path."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"


def _bench(name: str):
    if str(BENCH) not in sys.path:
        sys.path.insert(0, str(BENCH))
    spec = importlib.util.spec_from_file_location(name, BENCH / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def ce():
    return _bench("compare_eval")


@pytest.fixture(scope="module")
def ca():
    return _bench("compare_agents")


def _glb(path: Path, *, images: list[str] = (), buffers: list[str] = ()) -> Path:
    """A minimal GLB whose images and buffers are the given URIs (what an agent's exporter could write)."""
    from antigravity3d.deliver.glb import _join

    doc = {"asset": {"version": "2.0"}, "images": [{"uri": u} for u in images],
           "buffers": [{"uri": u, "byteLength": 4} for u in buffers]}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_join(doc, b""))
    return path


def _files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() or p.is_symlink()}


# --------------------------------------------------------------------------- URIs in a delivered GLB


def test_bare_copy_does_not_write_outside_the_evaluation_folder(ce, tmp_path):
    ws = tmp_path / "ws"
    (ws / "planted.png").parent.mkdir(parents=True)
    (ws / "planted.png").write_bytes(b"agent bytes")  # inside the agent's workspace, beside its output folder
    glb = _glb(ws / "output" / "a.glb", images=["../planted.png"])
    dest = tmp_path / "eval" / "cell" / "bare"
    before = _files(tmp_path)
    with pytest.raises(ValueError, match=r"\.\./planted\.png"):
        ce.bare_copy(glb, dest, "asset")
    assert _files(tmp_path) - before <= {f"eval/cell/bare/{n}" for n in ("asset.glb",)}
    assert not (tmp_path / "eval" / "cell" / "planted.png").exists()


def test_bare_copy_does_not_read_outside_the_delivered_folder(ce, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("host secret")
    glb = _glb(tmp_path / "ws" / "output" / "a.glb", images=["../../secret.txt"])
    dest = tmp_path / "eval" / "x" / "bare"
    with pytest.raises(ValueError, match="secret"):
        ce.bare_copy(glb, dest, "asset")
    assert not any("host secret" in p.read_text(errors="ignore") for p in (tmp_path / "eval").rglob("*") if p.is_file())


@pytest.mark.parametrize("uri", ["/abs/secret.bin", "../../secret.bin", "sub/../../../secret.bin", "c:secret.bin", "a\\..\\..\\secret.bin"])
def test_a_buffer_uri_outside_the_folder_is_refused_when_read(ce, tmp_path, uri):
    (tmp_path / "secret.bin").write_bytes(b"\x01\x02\x03\x04")
    if uri.startswith("/abs/"):
        uri = str(tmp_path / "secret.bin")
    glb = _glb(tmp_path / "ws" / "output" / "a.glb", buffers=[uri])
    with pytest.raises(ValueError):
        ce._load(glb)


def test_an_image_uri_outside_the_folder_reads_as_no_image(ce, tmp_path):
    (tmp_path / "secret.png").write_bytes(b"host secret")
    glb = _glb(tmp_path / "ws" / "output" / "a.glb", images=["../../secret.png"])
    g, bufs = ce._load(glb)
    assert ce._image_bytes(g, bufs, glb, 0) is None


def test_a_symlink_in_the_delivered_folder_is_not_followed(ce, tmp_path):
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"host secret")
    out = tmp_path / "ws" / "output"
    glb = _glb(out / "a.glb", images=["textures/t.png"])
    (out / "textures").mkdir()
    (out / "textures" / "t.png").symlink_to(secret)
    dest = tmp_path / "eval" / "bare"
    with pytest.raises(ValueError, match="symbolic link"):
        ce.bare_copy(glb, dest, "asset")
    assert not (dest / "textures" / "t.png").exists()
    g, bufs = ce._load(glb)
    assert ce._image_bytes(g, bufs, glb, 0) is None


# --------------------------------------------------------------------------- files the harness picks from the workspace


def test_the_main_glb_is_never_a_link(ca, tmp_path):
    host = tmp_path / "host.glb"
    host.write_bytes(b"x" * 4096)
    out = tmp_path / "ws" / "output"
    real = _glb(out / "small.glb")
    (out / "big.glb").symlink_to(host)
    assert ca.find_main_glb(out) == real


def test_the_plugin_self_report_reads_no_linked_manifest(ca, tmp_path):
    host = tmp_path / "host_manifest.json"
    host.write_text(json.dumps({"asset": "HostSecret"}))
    work = tmp_path / "ws"
    (work / "pkg").mkdir(parents=True)
    (work / "pkg" / "manifest.json").symlink_to(host)
    assert ca.plugin_self_report(work)["manifests"] == []


def test_a_reported_manifest_outside_the_workspace_is_not_used(ce, tmp_path):
    outside = tmp_path / "elsewhere" / "manifest.json"
    outside.parent.mkdir()
    outside.write_text("{}")
    work = tmp_path / "ws"
    glb = _glb(work / "output" / "a.glb")
    rec = SimpleNamespace(pipeline={"workspace": str(work), "self_report": {"manifests": [{"path": str(outside)}]}})
    assert ce.find_manifest(rec, glb) is None


def test_the_evaluation_reads_a_snapshot_without_links(ce, tmp_path):
    """The delivered folder is copied into the evaluation folder first, real files only: a link is left out and named,
    and the delivered GLB itself being a link makes the cell not usable, with the reason."""
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"host secret")
    out = tmp_path / "ws" / "output"
    glb = _glb(out / "a.glb", images=["t.png"])
    (out / "t.png").symlink_to(secret)
    (out / "ok.txt").write_text("fine")
    snap, refused = ce.delivered_snapshot(glb, tmp_path / "eval" / "delivered")
    assert snap == tmp_path / "eval" / "delivered" / "a.glb" and snap.is_file() and not snap.is_symlink()
    assert (snap.parent / "ok.txt").read_text() == "fine" and not (snap.parent / "t.png").exists()
    assert any("t.png" in r for r in refused)
    (out / "linked.glb").symlink_to(glb)
    with pytest.raises(ValueError, match="symbolic link"):
        ce.delivered_snapshot(out / "linked.glb", tmp_path / "eval" / "d2")


# --------------------------------------------------------------------------- copies the harness makes


def test_copies_leave_links_out(ca, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("host secret")
    src = tmp_path / "ws"
    (src / "output").mkdir(parents=True)
    (src / "output" / "a.glb").write_bytes(b"glb")
    (src / "output" / "link.txt").symlink_to(secret)
    (src / "linkdir").symlink_to(tmp_path, target_is_directory=True)
    skipped = ca.copy_without_links(src, tmp_path / "copy")
    assert _files(tmp_path / "copy") == {"output/a.glb"}
    assert sorted(skipped) == ["linkdir", "output/link.txt"]
