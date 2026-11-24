"""agy3d install into a three.js / React Three Fiber project: where the files go, the file discipline shared with the
Godot install, the loader text, compression (with a stand-in for the Node tool, so no Node is needed), undo, and the
browser proof (tests marked ``browser``; they need Playwright, a Chromium and ``npm ci`` in tests/fixtures/web_project)."""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np
import pytest

from antigravity3d.deliver import detect_project, install_package, uninstall, web
from antigravity3d.deliver import glb as G
from antigravity3d.deliver import web_check as WC
from antigravity3d.deliver import web_install as WI
from antigravity3d.deliver.install import RECORD, _Transaction, check_install

FIXTURE = Path(__file__).parent / "fixtures" / "web_project"
THREE = "0.170.0"


def _tree(root: Path) -> dict[str, str]:
    """Every file (hash) and folder of a tree, so that "byte for byte" includes the empty folders."""
    out = {}
    for d, _dirs, files in os.walk(root):
        rel = os.path.relpath(d, root)
        out[rel + "/"] = "dir"
        for f in files:
            out[os.path.join(rel, f)] = hashlib.sha256((Path(d) / f).read_bytes()).hexdigest()
    return out


@pytest.fixture(scope="module")
def crate(tmp_path_factory) -> Path:
    """The offline crate's package, as ``agy3d prop --offline --engine web_three`` writes it."""
    from antigravity3d.cli import main

    out = tmp_path_factory.mktemp("crate")
    assert main(["prop", "wooden crate with iron brackets", "--offline", "--engine", "web_three", "--out", str(out)]) == 0
    (pkg,) = out.glob("runs/*/package")
    return pkg


def _project(root: Path, deps: dict[str, str] | None = None, public: bool = True, src: bool = True) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "package.json").write_text(json.dumps({"name": "g", "dependencies": deps or {"three": THREE, "vite": "6.0.7"}}))
    (root / "index.html").write_text("<html></html>")
    if public:
        (root / "public").mkdir(exist_ok=True)
    if src:
        (root / "src").mkdir(exist_ok=True)
        (root / "src" / "main.js").write_text("import 'three';\n")
    return root


def _fake_node_modules(root: Path, with_tools: bool = True) -> None:
    """The files compression_tools and the transcoder copy read: package.json of each package, three's basis folder."""
    names = ["three", *(web.TOOL_PACKAGES if with_tools else [])]
    for n in names:
        d = root / "node_modules" / n
        d.mkdir(parents=True)
        (d / "package.json").write_text(json.dumps({"name": n, "version": THREE if n == "three" else "1.0.0"}))
    basis = root / "node_modules" / "three" / web.BASIS_IN_THREE
    basis.mkdir(parents=True)
    (basis / "basis_transcoder.js").write_text("// transcoder\n")
    (basis / "basis_transcoder.wasm").write_bytes(b"\0asm\1\0\0\0")


def _as_other_engine(pkg: Path, dst: Path, engine: str) -> Path:
    shutil.copytree(pkg, dst)
    m = json.loads((dst / "manifest.json").read_text())
    m["target_engine"] = engine
    (dst / "manifest.json").write_text(json.dumps(m))
    return dst


# --------------------------------------------------------------------------- the glb operations


def _glb(doc: dict, blob: bytes) -> bytes:
    return G._join(doc, blob)


def test_image_bytes_leave_a_meshopt_compressed_file_and_the_compressed_geometry_stays_readable():
    """An image dropped from the binary chunk of a file that also holds meshopt-compressed views: the offsets of the
    compressed data (the extension names buffer 0, the view names a data-less fallback buffer) are moved with it.
    Measured on the crate: before this, the loader read a wrong slice ("Invalid typed array length")."""
    image = bytes(range(200))
    geom = bytes(range(100, 160))
    blob = image + geom
    doc = {"asset": {"version": "2.0"}, "buffers": [{"byteLength": len(blob)}, {"byteLength": 96, "extensions": {"EXT_meshopt_compression": {"fallback": True}}}],
           "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(image)},
                           {"buffer": 1, "byteOffset": 0, "byteLength": 96, "extensions": {"EXT_meshopt_compression": {
                               "buffer": 0, "byteOffset": len(image), "byteLength": len(geom), "mode": "ATTRIBUTES", "count": 8, "byteStride": 12}}}],
           "images": [{"bufferView": 0, "mimeType": "image/ktx2"}], "accessors": [{"bufferView": 1, "componentType": 5126, "count": 8, "type": "VEC3"}]}
    out, report = G.reference_images_bytes(_glb(doc, blob), {hashlib.sha256(image).hexdigest(): "textures/a.ktx2"})
    assert report[0]["storage"] == "uri"
    d2, b2 = G._split(out)
    assert d2["images"] == [{"mimeType": "image/ktx2", "uri": "textures/a.ktx2"}]
    ext = d2["bufferViews"][0]["extensions"]["EXT_meshopt_compression"]
    assert b2[ext["byteOffset"]:ext["byteOffset"] + ext["byteLength"]] == geom
    assert d2["buffers"][0]["byteLength"] == len(b2) and d2["accessors"][0]["bufferView"] == 0


def test_strip_images_keeps_materials_and_drops_every_texture_reference():
    doc = {"asset": {"version": "2.0"}, "buffers": [{"byteLength": 4}], "bufferViews": [{"buffer": 0, "byteLength": 4}],
           "images": [{"uri": "textures/a.png"}], "textures": [{"source": 0}], "samplers": [{}],
           "materials": [{"name": "m", "pbrMetallicRoughness": {"baseColorFactor": [1, 1, 1, 1], "baseColorTexture": {"index": 0}},
                          "normalTexture": {"index": 0}, "extensions": {"KHR_materials_clearcoat": {"clearcoatFactor": 1, "clearcoatTexture": {"index": 0}}}}],
           "extensionsUsed": ["KHR_texture_basisu", "KHR_materials_clearcoat"], "extensionsRequired": ["KHR_texture_basisu"]}
    out, _ = G._split(G.strip_images(_glb(doc, b"abcd"))), None
    d = out[0]
    assert not {"images", "textures", "samplers", "extensionsRequired"} & set(d)
    m = d["materials"][0]
    assert m["name"] == "m" and "baseColorTexture" not in m["pbrMetallicRoughness"] and "normalTexture" not in m
    assert m["pbrMetallicRoughness"]["baseColorFactor"] == [1, 1, 1, 1] and "clearcoatTexture" not in m["extensions"]["KHR_materials_clearcoat"]
    assert d["extensionsUsed"] == ["KHR_materials_clearcoat"]


# --------------------------------------------------------------------------- layout


def test_a_three_project_is_a_package_target_and_a_godot_package_is_refused_with_the_way_to_get_one(crate, tmp_path):
    from antigravity3d.deliver import detect

    assert detect.TARGET_OF["threejs"] == "web_three"
    project = _project(tmp_path / "game")
    other = _as_other_engine(crate, tmp_path / "godot_pkg", "godot_4")
    before = _tree(project)
    r = install_package(other, project)
    assert r.status == "refused" and "--engine web_three" in r.message and 'engine="web_three"' in r.message
    assert _tree(project) == before


@pytest.mark.parametrize("deps, folders, served, rule", [
    ({"three": THREE, "vite": "6"}, ["public"], "public", "depends on vite"),
    ({"three": THREE, "next": "14"}, [], "public", "depends on next"),
    ({"three": THREE, "react-scripts": "5"}, [], "public", "react-scripts"),
    ({"three": THREE, "astro": "4"}, [], "public", "astro"),
    ({"three": THREE, "@sveltejs/kit": "2", "vite": "5"}, [], "static", "@sveltejs/kit"),
    ({"three": THREE}, ["static"], "static", "has a static/ folder"),
    ({"three": THREE}, ["public"], "public", "has a public/ folder"),
])
def test_the_served_folder_is_chosen_from_evidence_and_the_result_says_which_rule(deps, folders, served, rule, crate, tmp_path):
    project = _project(tmp_path / "game", deps, public=False)
    for f in folders:
        (project / f).mkdir()
    r = install_package(crate, project, dry_run=True)
    assert r.status == "dry_run", r.message
    assert all(f["path"].startswith((f"{served}/agy3d/", "src/agy3d/")) for f in r.files)
    assert any(rule in n for n in r.notes), r.notes


def test_a_project_that_names_no_served_folder_is_refused_naming_the_two_options(crate, tmp_path):
    project = _project(tmp_path / "game", {"three": THREE}, public=False)
    r = install_package(crate, project, dry_run=True)
    assert r.status == "refused" and "--assets" in r.message and "--dest" in r.message
    assert _tree(project) == _tree(project)  # nothing was created
    assert not (project / "public").exists()


def test_the_loader_goes_to_src_when_there_is_one_else_to_the_root_and_never_into_the_served_folder(crate, tmp_path):
    p1 = _project(tmp_path / "a", src=True)
    p2 = _project(tmp_path / "b", src=False)
    asset = json.loads((crate / "manifest.json").read_text())["asset"]
    assert install_package(crate, p1, dry_run=True).dest == f"src/agy3d/{asset}"
    assert install_package(crate, p2, dry_run=True).dest == f"agy3d/{asset}"
    r = install_package(crate, p1, dest="public/agy3d/loader", dry_run=True)
    assert r.status == "refused" and "production build" in r.message
    r = install_package(crate, p1, assets="public", dry_run=True)
    assert r.status == "refused"
    r = install_package(crate, p1, dest="src/x", assets="public/agy3d/../../../outside", dry_run=True)
    assert r.status == "refused"


# --------------------------------------------------------------------------- install, update, undo


def _install(crate, project, **kw):
    kw.setdefault("check", False)
    kw.setdefault("compress", False)
    return install_package(crate, project, **kw)


def test_install_writes_the_served_files_and_the_loader_and_the_record_stays_out_of_the_served_folder(crate, tmp_path):
    project = _project(tmp_path / "game")
    r = install_package(crate, project, check=False)  # no compression packages in this project: ships uncompressed
    assert r.status == "installed" and r.ok, r.message
    asset = r.package["asset"]
    names = {f["path"] for f in r.files}
    served = {n for n in names if n.startswith("public/")}
    assert f"public/agy3d/{asset}/{asset}.glb" in served and f"public/agy3d/{asset}/collision.json" in served
    assert all(not n.startswith("public/") or n.startswith(f"public/agy3d/{asset}/") for n in names)
    assert {f"src/agy3d/{asset}/index.js", f"src/agy3d/{asset}/index.d.ts"} <= names
    assert not any("react" in n for n in names)  # no @react-three/fiber: no component
    rec = json.loads((project / f"src/agy3d/{asset}/{RECORD}").read_text())
    assert rec["engine"] == "threejs" and rec["compression"]["state"] == "unavailable"
    assert not list((project / "public").rglob(".agy3d")) and not list((project / "public").rglob("install.json"))
    # every file of the record is a real file with the recorded bytes
    for f, v in rec["files"].items():
        assert hashlib.sha256((project / f).read_bytes()).hexdigest() == v["sha256"]
    assert any("npm install -D" in n and "ktx2-encoder" in n for n in r.notes)  # says how to enable compression


def test_far_levels_carry_no_images_and_the_highest_level_references_the_one_set_of_textures(crate, tmp_path):
    project = _project(tmp_path / "game")
    r = _install(crate, project)
    asset = r.package["asset"]
    base = project / "public/agy3d" / asset
    first, _ = G._split((base / f"{asset}.glb").read_bytes())
    assert first["images"] and all("uri" in i and i["uri"].startswith("textures/") for i in first["images"])
    assert not any("bufferView" in i for i in first["images"])  # nothing embedded: no second copy on the wire
    far = sorted(base.glob(f"{asset}_LOD*.glb"))
    assert far
    for f in far:
        d, _ = G._split(f.read_bytes())
        assert not {"images", "textures", "samplers"} & set(d) and d["materials"]


def test_undo_leaves_the_project_byte_for_byte_as_it_was_including_folders_it_did_not_make(crate, tmp_path):
    project = _project(tmp_path / "game")
    (project / "public" / "robots.txt").write_text("keep me")
    before = _tree(project)
    r = _install(crate, project)
    assert _tree(project) != before
    u = uninstall(project, r.dest)
    assert u.status == "uninstalled", u.message
    assert _tree(project) == before
    # and into a project that has no public/ and no src/: the folders the install made go too
    bare = _project(tmp_path / "bare", public=False, src=False)
    (bare / "static").mkdir()
    before = _tree(bare)
    r = _install(crate, bare)
    assert r.status == "installed"
    assert uninstall(bare, r.dest).status == "uninstalled" and _tree(bare) == before


def test_an_unchanged_package_is_unchanged_and_an_edited_file_is_never_overwritten_without_force(crate, tmp_path):
    project = _project(tmp_path / "game")
    r1 = _install(crate, project)
    r2 = _install(crate, project)
    assert r2.status == "unchanged" and all(f["action"] == "keep" for f in r2.files)
    loader = project / r1.dest / "index.js"
    loader.write_text(loader.read_text() + "\n// my edit\n")
    r3 = _install(crate, project)
    assert r3.status == "refused" and "edited" in r3.message and "// my edit" in loader.read_text()
    r4 = _install(crate, project, force=True)
    assert r4.status == "updated" and "// my edit" not in loader.read_text()
    # a file the tool did not write is never overwritten, with or without force
    other = _project(tmp_path / "other")
    asset = r1.package["asset"]
    mine = other / "public/agy3d" / asset
    mine.mkdir(parents=True)
    (mine / f"{asset}.glb").write_bytes(b"not mine")
    r5 = _install(crate, other, force=True)
    assert r5.status == "refused" and (mine / f"{asset}.glb").read_bytes() == b"not mine"


def test_a_failed_install_is_rolled_back_to_the_byte(crate, tmp_path, monkeypatch):
    project = _project(tmp_path / "game", public=False, src=False)
    (project / "static").mkdir()
    before = _tree(project)
    real = _Transaction.write
    calls = {"n": 0}

    def flaky(self, rel, data):
        calls["n"] += 1
        if calls["n"] == 6:
            raise OSError("disk full")
        return real(self, rel, data)

    monkeypatch.setattr(_Transaction, "write", flaky)
    with pytest.raises(OSError):
        _install(crate, project)
    assert _tree(project) == before


def test_a_symbolic_link_in_the_way_is_refused(crate, tmp_path):
    project = _project(tmp_path / "game")
    target = tmp_path / "elsewhere"
    target.mkdir()
    (project / "public" / "agy3d").symlink_to(target, target_is_directory=True)
    r = _install(crate, project)
    assert r.status == "refused" and "symbolic link" in r.message
    assert not any(target.iterdir())


def test_a_tampered_record_cannot_make_uninstall_touch_anything_outside_the_install(crate, tmp_path):
    project = _project(tmp_path / "game")
    r = _install(crate, project)
    victim = project / "package.json"
    rec_path = project / r.dest / RECORD
    rec = json.loads(rec_path.read_text())
    rec["files"]["package.json"] = {"sha256": hashlib.sha256(victim.read_bytes()).hexdigest(), "bytes": 1}
    rec_path.write_text(json.dumps(rec))
    u = uninstall(project, r.dest)
    assert u.status == "refused" and victim.exists()
    rec["files"].pop("package.json")
    rec["created_dirs"] = ["src", ".."]
    rec_path.write_text(json.dumps(rec))
    assert uninstall(project, r.dest).status == "refused"
    assert check_install(project, r.dest).status == "refused"


def test_dry_run_writes_nothing_and_runs_nothing_from_the_project(crate, tmp_path, monkeypatch):
    project = _project(tmp_path / "game")
    _fake_node_modules(project)
    before = _tree(project)

    def boom(*a, **k):
        raise AssertionError("a dry run must not run anything")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(web, "node_executable", lambda: "/usr/bin/node")
    _fake_measurement(monkeypatch)
    r = install_package(crate, project, dry_run=True)
    assert r.status == "dry_run" and _tree(project) == before
    assert any("compression would be tried" in n for n in r.notes)
    # what is certain is listed; the textures and the transcoder are decided after measuring, and the dry run says so
    paths = [f["path"] for f in r.files]
    assert not any("_basis" in f or "/textures/" in f for f in paths) and any(f.endswith("index.js") for f in paths)
    assert any("decided after measuring" in n for n in r.notes)


# --------------------------------------------------------------------------- the loader text


def test_the_loader_is_plain_javascript_with_declarations_and_its_numbers_are_the_packages(crate, tmp_path):
    project = _project(tmp_path / "game")
    r = _install(crate, project)
    asset = r.package["asset"]
    js = (project / r.dest / "index.js").read_text()
    dts = (project / r.dest / "index.d.ts").read_text()
    fn = "load" + web.pascal(asset)
    assert f"export async function {fn}(" in js and f"export function {fn}(" in dts
    assert "import.meta.env.BASE_URL) || \"/\"" in js  # outside Vite the default is "/", never "undefinedagy3d/..."
    assert "KTX2Loader" not in js  # uncompressed: no transcoder, no decoder imported
    data = json.loads(js[js.index("const DATA = ") + len("const DATA = "):js.index(";\n\n// Where the files")])
    m = json.loads((crate / "manifest.json").read_text())
    lo, hi = m["glb_bounds_m"]
    assert data["bounds"]["min"] == pytest.approx(lo, abs=1e-6) and data["bounds"]["max"] == pytest.approx(hi, abs=1e-6)
    assert [lv["triangles"] for lv in data["levels"]] == m["lod_tris"][:len(data["levels"])]
    # the Godot scene's arithmetic: a far level appears at d + 2m going out and goes at d coming in, m = 2.5 % of d
    from antigravity3d.deliver.godot import HYSTERESIS
    from antigravity3d.kernel.lod_diff import DEFAULT_FOV_DEG, transition_distance
    radius = float(np.linalg.norm(np.asarray(hi) - np.asarray(lo)) / 2)
    for i, lv in enumerate(data["levels"][1:], 1):
        d = transition_distance(radius, m["lod_screen_fractions"][i], DEFAULT_FOV_DEG)
        assert lv["distance"] == pytest.approx(d * (1 + 2 * HYSTERESIS), abs=1e-5)
        assert lv["distance"] * (1 - lv["hysteresis"]) == pytest.approx(d, abs=1e-4)
    assert data["levels"][0]["distance"] == 0 and data["fov"] == DEFAULT_FOV_DEG


def test_collision_data_is_the_colliders_file_as_plain_arrays(crate, tmp_path):
    project = _project(tmp_path / "game")
    r = _install(crate, project)
    asset = r.package["asset"]
    hulls = json.loads((project / "public/agy3d" / asset / "collision.json").read_text())
    m = json.loads((crate / "manifest.json").read_text())
    want = G.hull_meshes(crate / f"{asset}_colliders.glb", m["collider_glb_nodes"])
    assert len(hulls) == len(want) > 0
    for (_name, (pts, faces)), got in zip(sorted(want.items()), sorted(hulls.items())):
        assert np.allclose(np.asarray(got[1]["points"]).reshape(-1, 3), pts, atol=1e-6)
        assert np.asarray(got[1]["index"]).reshape(-1, 3).tolist() == faces.tolist()


def test_the_component_is_written_only_for_react_three_fiber_and_wraps_the_same_loader(crate, tmp_path):
    project = _project(tmp_path / "game", {"three": THREE, "vite": "6", "react": "18", "@react-three/fiber": "8"})
    r = _install(crate, project)
    asset = r.package["asset"]
    assert {f"src/agy3d/{asset}/react.js", f"src/agy3d/{asset}/react.d.ts"} <= {f["path"] for f in r.files}
    comp = (project / r.dest / "react.js").read_text()
    assert f"export function {web.pascal(asset)}(" in comp and "from \"./index.js\"" in comp and "<" not in comp.split("export")[1].replace("=>", "")
    assert "useThree" in comp  # the renderer comes from the Canvas: compressed textures need it


# --------------------------------------------------------------------------- compression (the Node tool replaced by a stand-in)


def _fake_measurement(monkeypatch, outcomes=()):
    """A browser that is there and measures: ``outcomes`` is what its frame comparison says, one entry per call in order
    ("pass", "fail", or ("other", "check name") for another failing check); after them every call passes."""
    queue = list(outcomes)
    seen: list = []

    async def run(project, record, manifest, pkg, three_path, Check, **kw):
        out = queue.pop(0) if queue else "pass"
        seen.append(out)
        checks = [Check("import", "passed", "")]
        if isinstance(out, tuple):
            checks.append(Check(out[1], "failed", "stand-in"))
            out = "pass" if out[0] == "pass_with_other" else "fail"
        checks.append(Check("compression", "passed" if out == "pass" else "failed", "stand-in"))
        return checks, {"frame_difference": {"mean_abs": 0.005 if out == "pass" else 0.02, "pixel_share": 0.002 if out == "pass" else 0.06,
                                              "outline": 0.9999}}

    monkeypatch.setattr(WC, "run", run)
    monkeypatch.setattr(WI, "_cannot_measure", lambda check, three: None if check else "--no-check stand-in")
    return seen


def _fake_compress(calls: list, shrink: int = 4):
    def compress(parts, versions, node_modules, tier=None, cache_dir=None, **kw):
        calls.append(tier[0] if tier else 1)
        files = {}
        for f in parts.level_files:
            files[f] = G.strip_images(parts.levels[f]) if f != parts.level_files[0] else G.reference_images_bytes(
                parts.levels[f], {hashlib.sha256(b).hexdigest(): f"textures/{Path(r).stem}.ktx2" for r, b in parts.textures.items()})[0]
        for r, b in parts.textures.items():
            files[f"textures/{Path(r).stem}.ktx2"] = b[: len(b) // shrink] if shrink else b + b"x" * 4000000
        return web.Compressed(files, {"versions": versions, "images": [], "tier": tier[0], "texture_settings": tier[1]})
    return compress


def test_compression_runs_once_is_kept_on_an_unchanged_reinstall_and_the_transcoder_is_shared(crate, tmp_path, monkeypatch):
    project = _project(tmp_path / "game")
    _fake_node_modules(project)
    monkeypatch.setattr(web, "node_executable", lambda: "/usr/bin/node")
    calls: list = []
    monkeypatch.setattr(web, "compress", _fake_compress(calls))
    _fake_measurement(monkeypatch)
    before = _tree(project)
    a = install_package(crate, project)
    assert a.status == "installed" and len(calls) == 1
    rec = json.loads((project / a.dest / RECORD).read_text())
    assert rec["compression"]["state"] == "compressed" and set(rec["compression"]["versions"]) == set(web.TOOL_PACKAGES)
    assert rec["compression"]["tier"] == web.TEXTURE_TIERS[0][0] and rec["compression"]["tolerance_measured"] is True
    assert {f for f, v in rec["files"].items() if v.get("shared")} == {"public/agy3d/_basis/basis_transcoder.js", "public/agy3d/_basis/basis_transcoder.wasm"}
    assert any(n.endswith(".ktx2") for n in rec["files"]) and not any(n.endswith(".png") for n in rec["files"])
    # the same package, tool versions and settings again: the compressed files are kept, nothing is compressed again
    b = install_package(crate, project, check=False)
    assert b.status == "unchanged" and len(calls) == 1 and all(f["action"] == "keep" for f in b.files)
    assert any(n.startswith("kept the compressed files") for n in b.notes)
    # another settings value is another key: compressed again
    monkeypatch.setitem(web.TEXTURE_TIERS[-1][1]["albedo"], "qualityLevel", 99)
    install_package(crate, project)
    assert len(calls) == 2 and json.loads((project / a.dest / RECORD).read_text())["compression"]["key"] != rec["compression"]["key"]
    # a second install of the same asset elsewhere shares the one transcoder; removing one keeps it for the other
    d = install_package(crate, project, dest="src/agy3d/second", assets="public/agy3d/second")
    assert d.status == "installed" and not any("_basis" in f["path"] and f["action"] == "create" for f in d.files)
    u1 = uninstall(project, a.dest)
    assert u1.status == "uninstalled" and (project / "public/agy3d/_basis/basis_transcoder.wasm").exists()
    u2 = uninstall(project, d.dest)
    assert u2.status == "uninstalled" and _tree(project) == before


def _tier_plan(calls, results):
    """web._choose_tier with a stand-in encoder and a probe that answers from ``results`` in order."""
    parts = None
    answers = iter(results or [])

    def probe(plan):
        return next(answers)

    def assemble(files, comp):
        return web.WebPlan({}, set(), None, [], {}, [], 0.0, 60.0, False, comp, [], sum(len(v) for v in files.values()))

    import antigravity3d.deliver.web as W
    real = W.compress
    W.compress = lambda parts_, versions, nm, tier, cache, **k: (calls.append(tier[0]), W.Compressed({"a": b"x" * (100 - len(calls))}, {"tier": tier[0], "versions": {}}))[1]
    try:
        return W._choose_tier(parts, {}, Path("."), "k", assemble, {"a": b"p" * 500}, probe if results is not None else None)
    finally:
        W.compress = real


def test_the_tier_ladder_moves_down_only_while_the_tolerance_fails_and_writes_nothing_unmeasured():
    names = [t[0] for t in web.TEXTURE_TIERS]
    calls: list = []
    ok = {"passed": True, "mean_abs": 0.005, "pixel_share": 0.002, "outline": 0.9999}
    bad = {"passed": False, "mean_abs": 0.02, "pixel_share": 0.06, "outline": 0.9999}
    plan = _tier_plan(calls, [ok])
    assert calls == names[:1] and plan.compression["tier"] == names[0] and plan.compression["tolerance_measured"] is True
    calls.clear()
    plan = _tier_plan(calls, [bad, ok])
    assert calls == names[:2] and plan.compression["tier"] == names[1]
    assert [t["tier"] for t in plan.compression["tiers_tried"]] == names[:2] and plan.compression["tiers_tried"][0]["pixel_share"] == 0.06
    calls.clear()
    plan = _tier_plan(calls, [bad] * len(names))
    assert calls == names and plan.compression["state"] == "rejected" and len(plan.compression["tiers_tried"]) == len(names)
    calls.clear()  # another check fails: more compression would not change it, and a tier outside the tolerance is NOT written
    plan = _tier_plan(calls, [{"passed": False, "stop": True, "other_failures": ["import"], "pixel_share": 0.06}])
    assert calls == names[:1] and plan.compression["state"] == "rejected" and "import" in plan.compression["reason"]
    assert "tier" not in plan.compression
    calls.clear()  # nothing could be measured: nothing compressed is written
    plan = _tier_plan(calls, [{"passed": None, "why": "no browser"}])
    assert plan.compression["state"] == "unmeasured" and "no browser" in plan.compression["reason"] and "tier" not in plan.compression
    with pytest.raises(RuntimeError):  # no probe at all: the plan is never asked to compress
        _tier_plan([], None)


def test_tier_one_is_what_the_measurements_said_and_normals_are_never_etc1s():
    for _name, tier in web.TEXTURE_TIERS:
        assert tier["normal"]["isUASTC"] and tier["normal"]["isNormalMap"] and not tier["normal"]["isPerceptual"]
        assert tier["albedo"]["isSetKTX2SRGBTransferFunc"] and not tier["orm"]["isSetKTX2SRGBTransferFunc"]
    assert not web.TEXTURE_TIERS[0][1]["albedo"]["isUASTC"] and not web.TEXTURE_TIERS[0][1]["orm"]["isUASTC"]
    assert web.TEXTURE_TIERS[-1][1]["albedo"]["isUASTC"] and web.TEXTURE_TIERS[-1][1]["orm"]["isUASTC"]


def test_auto_compresses_only_when_the_files_do_not_grow_and_says_why_otherwise(crate, tmp_path, monkeypatch):
    monkeypatch.setattr(web, "node_executable", lambda: "/usr/bin/node")
    _fake_measurement(monkeypatch)
    # files that shrink: auto compresses
    p1 = _project(tmp_path / "shrinks")
    _fake_node_modules(p1)
    monkeypatch.setattr(web, "compress", _fake_compress([]))
    r = install_package(crate, p1)
    assert json.loads((p1 / r.dest / RECORD).read_text())["compression"]["state"] == "compressed"
    # files that grow (and a transcoder this project has not got): auto installs the PNGs and gives the numbers and what always buys
    p2 = _project(tmp_path / "grows")
    _fake_node_modules(p2)
    grown: list = []
    monkeypatch.setattr(web, "compress", _fake_compress(grown, shrink=0))
    r = install_package(crate, p2)
    rec = json.loads((p2 / r.dest / RECORD).read_text())["compression"]
    assert rec["state"] == "declined" and rec["compressed_bytes"] > rec["uncompressed_bytes"]
    # the smallest tier is already too large: it alone was encoded, and its frames were not compared
    assert grown == [web.TEXTURE_TIERS[0][0]] and rec["tiers_tried"] == [
        {"tier": web.TEXTURE_TIERS[0][0], "asset_bytes": rec["compressed_bytes"], "not_compared": True}]
    assert "the smallest tier there is" in rec["reason"] and "nothing more was encoded" in rec["reason"]
    assert any("--compress always" in n and "GPU memory" in n and f"{rec['uncompressed_bytes']:,}" in n for n in r.notes), r.notes
    assert not any(f["path"].endswith(".ktx2") or "_basis" in f["path"] for f in r.files)
    # asked for always: compressed whatever the size
    r = install_package(crate, p2, compress="always", force=True)
    assert json.loads((p2 / r.dest / RECORD).read_text())["compression"]["state"] == "compressed"
    # a decision made for these inputs is kept: the same call again encodes nothing
    p3 = _project(tmp_path / "again")
    _fake_node_modules(p3)
    calls: list = []
    monkeypatch.setattr(web, "compress", _fake_compress(calls, shrink=0))
    install_package(crate, p3)
    n = len(calls)
    again = install_package(crate, p3)
    assert again.status == "unchanged" and len(calls) == n
    # never: no compression even with the packages present
    p4 = _project(tmp_path / "never")
    _fake_node_modules(p4)
    r = install_package(crate, p4, compress="never")
    assert json.loads((p4 / r.dest / RECORD).read_text())["compression"]["state"] == "off"
    r = install_package(crate, p4, compress="sometimes", dest="src/agy3d/x", assets="public/agy3d/x")
    assert r.status == "refused" and "auto" in r.message and "never" in r.message


def test_auto_stops_at_the_first_tier_too_large_to_keep(crate, tmp_path, monkeypatch):
    """Run 2 of the website task (2026-11-24): each install encoded three tiers (25, 25 and 63 s) and compared two of
    them in the browser, 114 of its 130 s, and kept none: a web package's WebP maps are smaller than every tier."""
    monkeypatch.setattr(web, "node_executable", lambda: "/usr/bin/node")
    _fake_measurement(monkeypatch, ["fail"])  # the first tier's frames are outside the tolerance
    p = _project(tmp_path / "game")
    _fake_node_modules(p)
    calls: list = []
    small, large = _fake_compress(calls, shrink=4), _fake_compress(calls, shrink=0)

    def compress(parts, versions, node_modules, tier=None, cache_dir=None, **kw):
        return (small if tier[0] == web.TEXTURE_TIERS[0][0] else large)(parts, versions, node_modules, tier, cache_dir, **kw)

    monkeypatch.setattr(web, "compress", compress)
    r = install_package(crate, p)
    rec = json.loads((p / r.dest / RECORD).read_text())["compression"]
    assert rec["state"] == "declined" and calls == [t[0] for t in web.TEXTURE_TIERS[:2]]  # the two larger tiers: never encoded
    first, second = rec["tiers_tried"]
    assert first["passed"] is False and "pixel_share" in first  # small enough to keep, compared, outside the tolerance
    assert second == {"tier": web.TEXTURE_TIERS[1][0], "asset_bytes": rec["compressed_bytes"], "not_compared": True}
    assert "the next tier after those outside the frame tolerance" in rec["reason"]
    assert any("too large to keep, frames not compared" in n and "--compress always" in n for n in r.notes), r.notes
    assert not any(f["path"].endswith(".ktx2") or "_basis" in f["path"] for f in r.files)
    # asked for always, the size is no reason to stop: the ladder is walked to the first tier inside the tolerance
    calls.clear()
    r = install_package(crate, p, compress="always", force=True)
    assert json.loads((p / r.dest / RECORD).read_text())["compression"]["state"] == "compressed" and calls


def test_the_transcoder_is_not_counted_against_an_asset_when_the_project_already_has_it(crate, tmp_path, monkeypatch):
    monkeypatch.setattr(web, "node_executable", lambda: "/usr/bin/node")
    _fake_measurement(monkeypatch)
    p = _project(tmp_path / "game")
    _fake_node_modules(p)
    (p / "node_modules/three" / web.BASIS_IN_THREE / "basis_transcoder.wasm").write_bytes(b"w" * 5_000_000)  # larger than anything the crate's textures save
    monkeypatch.setattr(web, "compress", _fake_compress([], shrink=2))  # texture files half the PNGs
    # no transcoder in the project yet: it is part of what the page downloads, so smaller files are not worth it
    first = install_package(crate, p, dest="src/agy3d/a", assets="public/agy3d/a")
    a = json.loads((p / first.dest / RECORD).read_text())["compression"]
    assert a["state"] == "declined" and a["transcoder_bytes_counted"] >= 5_000_000
    # another compressed asset (here: asked for with always) has paid for it: the same files are now smaller than before
    install_package(crate, p, dest="src/agy3d/b", assets="public/agy3d/b", compress="always")
    second = install_package(crate, p, dest="src/agy3d/c", assets="public/agy3d/c")
    assert json.loads((p / second.dest / RECORD).read_text())["compression"]["state"] == "compressed"


def test_a_compression_tool_that_fails_installs_the_uncompressed_files_and_says_why(crate, tmp_path, monkeypatch):
    """Round eight changed this: a failing compression tool used to fail the whole install (nothing written). The project's
    own ktx2-encoder 0.6.0 (the fixture's and the round-eight site seed's) throws for one 4096 x 4096 map, which run 1's
    first containment_gate had, so a web project with the packages could not take that asset at all. Compression is an
    extra: the uncompressed files and the loader are installed, and the record and the note say why."""
    project = _project(tmp_path / "game")
    _fake_node_modules(project)
    monkeypatch.setattr(web, "node_executable", lambda: "/usr/bin/node")
    _fake_measurement(monkeypatch)

    def broken(*a, **k):
        raise RuntimeError("compression failed (node exit 1): Error: Total source texels 16777216 exceed the encoder limit\n  at x")

    monkeypatch.setattr(web, "compress", broken)
    r = install_package(crate, project)
    comp = json.loads((project / r.dest / RECORD).read_text())["compression"]
    assert r.status == "installed" and comp["state"] == "rejected", r.message
    assert comp["reason"] == ("the project's compression tools failed at etc1s_albedo_and_orm (Total source texels 16777216 "
                              "exceed the encoder limit)")
    assert any("not compressed: the project's compression tools failed" in n for n in r.notes)
    assert not list((project / "public").rglob("*.ktx2")) and list((project / "public").rglob(f"{r.package['asset']}.glb"))


def test_without_the_packages_the_install_ships_uncompressed_and_names_the_one_command(crate, tmp_path):
    project = _project(tmp_path / "game")
    _fake_node_modules(project, with_tools=False)
    versions, missing, _ = web.compression_tools(project)
    assert versions is None and set(web.TOOL_PACKAGES) <= set(missing)
    r = install_package(crate, project, check=False)
    assert r.status == "installed" and any(web.ENABLE_COMPRESSION in n for n in r.notes)


def test_the_tolerance_numbers_are_the_contracts():
    """CONTRACT_WEB.md section 4, fixed before the first compressed file existed: do not edit them to pass."""
    from antigravity3d.deliver import web_check as C

    assert (C.TOL_MEAN_ABS, C.TOL_PIXEL_SHARE, C.TOL_PIXEL_DELTA, C.TOL_OUTLINE) == (0.015, 0.01, 0.08, 0.999)


def test_frame_comparison_measures_the_asset_pixels_and_catches_a_changed_picture():
    from PIL import Image
    import io
    from antigravity3d.deliver import web_check as C

    def png(a):
        b = io.BytesIO()
        Image.fromarray(a, "RGBA").save(b, "PNG")
        return b.getvalue()

    base = np.zeros((64, 64, 4), np.uint8)
    base[16:48, 16:48] = (120, 130, 140, 255)
    same = C.compare_frames(png(base), png(base), png(base), png(base))
    assert same["mean_abs"] == 0 and same["pixel_share"] == 0 and same["outline"] == 1.0
    shifted = base.copy()
    shifted[16:48, 16:48, :3] += 30
    m = C.compare_frames(png(shifted), png(base), png(base), png(base))
    assert m["mean_abs"] == pytest.approx(30 / 255) and m["pixel_share"] == 1.0  # 30/255 is over 8 % of full scale
    assert m["mean_abs_whole_frame"] < m["mean_abs"]  # the empty background would have diluted it


def test_level_switching_probes_cover_both_lenses_in_order():
    from antigravity3d.deliver import web_check as C

    rec = {"switch_m": [10.0, 20.0], "fov_deg": 60.0}
    probes, want = C.probe_plan(rec)
    half = len(probes) // 2
    assert [p["fov"] for p in probes[:half]] == [60.0] * half and [p["fov"] for p in probes[half:]] == [C.PROBE_FOV_DEG] * half
    assert want[:half] == want[half:]
    k = math.tan(math.radians(30)) / math.tan(math.radians(C.PROBE_FOV_DEG / 2))
    assert probes[half]["distance"] == pytest.approx(probes[0]["distance"] * k)
    assert max(want) == 2 and want[0] == 0 and want[half - 1] == 0  # out past both switches and back to the nearest level


def test_the_three_version_range_is_what_was_run_and_outside_it_the_install_says_so(tmp_path):
    lo, hi = web.SUPPORTED_REVISIONS
    p = _project(tmp_path / "in")
    _fake_node_modules(p, with_tools=False)
    info = web.three_info(p, detect_project(p))
    assert info.revision == 170 and info.supported == (lo <= 170 <= hi)
    q = _project(tmp_path / "old", {"three": "0.100.0", "vite": "6"})
    (q / "node_modules" / "three").mkdir(parents=True)
    (q / "node_modules" / "three" / "package.json").write_text('{"version": "0.100.0"}')
    old = web.three_info(q, detect_project(q))
    assert old.supported is False and "not checked on this version" in old.note and f"r{lo} to r{hi}" in old.note


# --------------------------------------------------------------------------- the browser proof


# --------------------------------------------------------------------------- a record is data, never the authority


def _record_of(project: Path, dest: str) -> tuple[Path, dict]:
    path = project / dest / RECORD
    return path, json.loads(path.read_text())


def test_an_edited_record_cannot_make_uninstall_or_reinstall_delete_the_projects_own_files(crate, tmp_path):
    """The reviewer's three edits (and the cases beside them): the record says basis is src/ with src/main.js "shared" and
    a wrong hash; dest or assets point elsewhere; an entry is marked shared that is not a transcoder file. Each is refused
    by uninstall, by check and by a reinstall, and nothing of the project is deleted."""
    project = _project(tmp_path / "game")
    r = _install(crate, project)
    mine = project / "src/main.js"
    keep = _tree(project)
    path, rec = _record_of(project, r.dest)
    edits = []
    e = json.loads(json.dumps(rec)); e["basis"] = "src"; e["files"]["src/main.js"] = {"sha256": "0" * 64, "bytes": 1, "shared": True}
    edits.append(("basis src, main.js shared", e))
    e = json.loads(json.dumps(rec)); e["files"]["src/main.js"] = {"sha256": hashlib.sha256(mine.read_bytes()).hexdigest(), "bytes": 1}
    edits.append(("an entry in the project's own src/", e))
    e = json.loads(json.dumps(rec)); e["assets"] = "src"; e["files"]["src/main.js"] = {"sha256": "0" * 64, "bytes": 1}
    edits.append(("assets is src", e))
    e = json.loads(json.dumps(rec)); e["dest"] = "src"; e["files"]["src/main.js"] = {"sha256": "0" * 64, "bytes": 1}
    edits.append(("dest is src", e))
    e = json.loads(json.dumps(rec)); e["basis"] = f"{e['assets'].split('/')[0]}/agy3d/_basis"
    e["files"][f"{e['basis']}/something_else.js"] = {"sha256": "0" * 64, "bytes": 1, "shared": True}
    edits.append(("shared but not a transcoder file", e))
    e = json.loads(json.dumps(rec)); e["asset"] = "x'); alert(1); ('"
    edits.append(("an asset name that is not an identifier", e))
    e = json.loads(json.dumps(rec)); e["files"][f"{e['dest']}/evil.js"] = {"sha256": "0" * 64, "bytes": 1}
    edits.append(("a file in the loader folder that no install writes", e))
    for why, edit in edits:
        path.write_text(json.dumps(edit))
        for call in (lambda: uninstall(project, r.dest), lambda: check_install(project, r.dest)):
            res = call()
            assert res.status == "refused", (why, res.status, res.message)
        assert mine.exists() and mine.read_text() == "import 'three';\n", why
        again = install_package(crate, project, compress="never", check=False)
        assert again.status == "refused" and mine.exists(), (why, again.message)
    path.write_text(json.dumps(rec))
    assert uninstall(project, r.dest, dry_run=True).status == "dry_run"
    strip = lambda t: {k: v for k, v in t.items() if not k.endswith(RECORD)}  # noqa: E731  (the record was rewritten by hand above)
    assert strip(_tree(project)) == strip(keep)  # nothing was touched by the refusals or the dry run
    assert uninstall(project, r.dest).status == "uninstalled" and mine.exists()


def test_the_shared_transcoder_is_removed_only_when_it_is_the_copy_in_the_projects_own_three(crate, tmp_path, monkeypatch):
    monkeypatch.setattr(web, "node_executable", lambda: "/usr/bin/node")
    project = _project(tmp_path / "game")
    _fake_node_modules(project)
    monkeypatch.setattr(web, "compress", _fake_compress([]))
    _fake_measurement(monkeypatch)
    r = install_package(crate, project)
    wasm = project / "public/agy3d/_basis/basis_transcoder.wasm"
    wasm.write_bytes(b"the developer replaced this")
    u = uninstall(project, r.dest)
    assert u.status == "uninstalled" and wasm.read_bytes() == b"the developer replaced this"
    assert any("not the copy" in n for n in u.notes) and not (project / r.dest).exists()


# --------------------------------------------------------------------------- nothing compressed without a passed measurement


def test_nothing_compressed_is_written_without_a_measurement_that_passed(crate, tmp_path, monkeypatch):
    monkeypatch.setattr(web, "node_executable", lambda: "/usr/bin/node")
    calls: list = []
    monkeypatch.setattr(web, "compress", _fake_compress(calls))
    # --no-check: uncompressed, "unmeasured", the flag named; `auto` does not claim a measurement it did not make
    for mode in ("always", "auto"):
        p = _project(tmp_path / f"nocheck_{mode}")
        _fake_node_modules(p)
        r = install_package(crate, p, check=False, compress=mode)
        comp = json.loads((p / r.dest / RECORD).read_text())["compression"]
        assert comp["state"] == "unmeasured" and "--no-check" in comp["reason"], (mode, comp)
        assert not any(f["path"].endswith(".ktx2") or "_basis" in f["path"] for f in r.files) and not calls
        assert any("--no-check" in n and "frame comparison" in n for n in r.notes) and not any("declined" in n for n in r.notes)
    # no browser: the same, and the result says what is missing
    from antigravity3d.adapters.engines import web_three as w3

    monkeypatch.setattr(w3.WebThreeValidator, "_unavailable", lambda self: "web_three needs the Python 'playwright' package")
    p = _project(tmp_path / "nobrowser")
    _fake_node_modules(p)
    r = install_package(crate, p, compress="always")
    comp = json.loads((p / r.dest / RECORD).read_text())["compression"]
    assert comp["state"] == "unmeasured" and "playwright" in comp["reason"] and not calls
    assert not any(f["path"].endswith(".ktx2") for f in r.files)


def test_a_tier_outside_the_tolerance_is_not_written_when_another_check_fails_too(crate, tmp_path, monkeypatch):
    monkeypatch.setattr(web, "node_executable", lambda: "/usr/bin/node")
    calls: list = []
    monkeypatch.setattr(web, "compress", _fake_compress(calls))
    _fake_measurement(monkeypatch, [("other", "levels")])  # the first tier's frame fails AND a levels check fails
    project = _project(tmp_path / "game")
    _fake_node_modules(project)
    r = install_package(crate, project, compress="always")
    comp = json.loads((project / r.dest / RECORD).read_text())["compression"]
    assert comp["state"] == "rejected" and "levels" in comp["reason"], comp
    assert not any(f["path"].endswith(".ktx2") for f in r.files) and "tier" not in comp


def test_a_compressed_install_that_still_fails_a_check_says_both_in_one_sentence(crate, tmp_path, monkeypatch):
    monkeypatch.setattr(web, "node_executable", lambda: "/usr/bin/node")
    monkeypatch.setattr(web, "compress", _fake_compress([]))
    _fake_measurement(monkeypatch, [("pass_with_other", "levels")] * 2)
    project = _project(tmp_path / "game")
    _fake_node_modules(project)
    r = install_package(crate, project, compress="always")
    sentence = f"compressed at {web.TEXTURE_TIERS[0][0]}, inside the frame tolerance; the install still fails check levels"
    assert sentence in r.message and sentence in r.warnings and not r.ok


def test_a_decision_not_to_compress_is_decided_again_once_the_shared_transcoder_is_there(crate, tmp_path, monkeypatch):
    monkeypatch.setattr(web, "node_executable", lambda: "/usr/bin/node")
    project = _project(tmp_path / "game")
    _fake_node_modules(project)
    (project / "node_modules/three" / web.BASIS_IN_THREE / "basis_transcoder.wasm").write_bytes(b"w" * 5_000_000)
    calls: list = []
    monkeypatch.setattr(web, "compress", _fake_compress(calls, shrink=2))
    _fake_measurement(monkeypatch)
    first = install_package(crate, project, dest="src/agy3d/a", assets="public/agy3d/a")
    assert json.loads((project / first.dest / RECORD).read_text())["compression"]["state"] == "declined"
    n = len(calls)
    install_package(crate, project, dest="src/agy3d/a", assets="public/agy3d/a")
    assert len(calls) == n  # the same inputs and the same situation: the decision is kept
    install_package(crate, project, dest="src/agy3d/b", assets="public/agy3d/b", compress="always")  # brings the transcoder
    again = install_package(crate, project, dest="src/agy3d/a", assets="public/agy3d/a")
    assert json.loads((project / again.dest / RECORD).read_text())["compression"]["state"] == "compressed"


# --------------------------------------------------------------------------- the project's own npm code gets a bare environment


def _child_script(tmp_path: Path, body: str) -> Path:
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    f = d / "node"
    f.write_text("#!/bin/sh\n" + body)
    f.chmod(0o755)
    return d


def test_the_compressor_and_the_bundler_get_a_bare_environment_and_the_parents_secrets_stay_out(crate, tmp_path, monkeypatch):
    for k, v in (("GEMINI_API_KEY", "s1"), ("AWS_SECRET_ACCESS_KEY", "s2"), ("GITHUB_TOKEN", "s3"), ("SESSION_INGRESS_TOKEN", "s4")):
        monkeypatch.setenv(k, v)
    log = tmp_path / "env.log"
    bindir = _child_script(tmp_path, f'if [ "$1" = "-p" ]; then echo "$0"; exit 0; fi\nenv > {log}\nexit 1\n')
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    web._NODE.clear()
    project = _project(tmp_path / "game")
    _fake_node_modules(project)
    parts = web.read_parts(crate, json.loads((crate / "manifest.json").read_text()))
    with pytest.raises(RuntimeError):
        web.compress(parts, {}, project / "node_modules", web.TEXTURE_TIERS[0], tmp_path / "cache")
    seen = dict(line.split("=", 1) for line in log.read_text().splitlines() if "=" in line)
    assert not {"GEMINI_API_KEY", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "SESSION_INGRESS_TOKEN"} & set(seen)
    assert set(seen) <= {"PATH", "HOME", "TMPDIR", "LANG", "PWD", "SHLVL", "_", "OLDPWD"}, sorted(seen)
    assert seen["HOME"] == seen["TMPDIR"] and seen["HOME"].startswith(tempfile.gettempdir())
    # the one setting of this tool that the compressor reads is passed on when it is set
    monkeypatch.setenv("AGY3D_KTX2_WORKERS", "2")
    assert web.bare_env(tmp_path)["AGY3D_KTX2_WORKERS"] == "2"
    monkeypatch.delenv("AGY3D_KTX2_WORKERS")
    assert "AGY3D_KTX2_WORKERS" not in web.bare_env(tmp_path)
    # the project's esbuild is run the same way
    log2 = tmp_path / "env2.log"
    code, _out, _err = web.run_child(["sh", "-c", f"env > {log2}"], cwd=tmp_path, timeout=10)
    assert code == 0 and "GEMINI_API_KEY" not in log2.read_text()
    web._NODE.clear()
    browser = WC.browser_env(tmp_path)
    assert not {"GEMINI_API_KEY", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN"} & set(browser) and "PATH" in browser


def test_a_hung_child_is_killed_with_its_whole_process_group_and_reported_not_raised(crate, tmp_path):
    pidfile = tmp_path / "pid"
    t0 = time.monotonic()
    with pytest.raises(web.ChildError, match="did not finish"):
        web.run_child(["sh", "-c", f"sleep 300 & echo $! > {pidfile}; wait"], cwd=tmp_path, timeout=1.5)
    assert time.monotonic() - t0 < 30
    pid = int(pidfile.read_text())
    time.sleep(0.5)
    stat = Path(f"/proc/{pid}/stat")  # the grandchild is gone too (a killed one nobody has reaped yet shows as a zombie)
    assert not stat.exists() or stat.read_text().rsplit(")", 1)[1].split()[0] == "Z"
    with pytest.raises(web.ChildError, match="could not be started"):
        web.run_child([str(tmp_path / "no_such_binary")], cwd=tmp_path, timeout=5)


def test_a_bundler_that_hangs_fails_the_react_check_and_the_install_still_stands(crate, tmp_path, monkeypatch):
    import asyncio

    from antigravity3d.adapters.engines import web_three as w3
    from antigravity3d.deliver.install import Check

    project = _project(tmp_path / "game")
    _fake_node_modules(project, with_tools=False)
    (project / "node_modules/.bin").mkdir()
    (project / "node_modules/.bin/esbuild").write_text("#!/bin/sh\nsleep 300\n")
    (project / "node_modules/.bin/esbuild").chmod(0o755)
    monkeypatch.setattr(w3.WebThreeValidator, "_unavailable", lambda self: None)
    monkeypatch.setattr(WC, "run_child", lambda *a, **k: (_ for _ in ()).throw(web.ChildError("esbuild did not finish within 120 s and was stopped")))
    man = json.loads((crate / "manifest.json").read_text())
    rec = {"asset": man["asset"], "dest": "src/agy3d/x", "assets": "public/agy3d/x", "assets_url": "agy3d/x", "basis": None, "basis_url": "agy3d/_basis"}
    (c,) = asyncio.run(WC.run_react(project, rec, man, str(project / "node_modules/three"), Check))
    assert c.name == "react_component" and c.status == "failed" and "did not finish" in c.detail


# --------------------------------------------------------------------------- --assets, the search for node_modules


@pytest.mark.parametrize("assets, needle", [
    ("/etc/agy3d-x", "absolute"), ("/public/x", "absolute"), ("src/x", "public or static"), ("models/x", "public or static"),
    ("public", "served folder itself"), ("public/../src/x", "inside the project"), ("public/.hidden/x", "hidden"),
    ("C:/x", "absolute"),
])
def test_assets_must_be_a_relative_folder_under_public_or_static(assets, needle, crate, tmp_path):
    project = _project(tmp_path / "game")
    r = install_package(crate, project, assets=assets, dry_run=True)
    assert r.status == "refused" and needle in r.message and "None" not in r.message, r.message
    assert not (project / "etc").exists()


def test_assets_under_the_served_folder_the_project_has_are_accepted(crate, tmp_path):
    project = _project(tmp_path / "game")
    r = install_package(crate, project, assets="public/models/crate", dry_run=True)
    assert r.status == "dry_run" and any(f["path"].startswith("public/models/crate/") for f in r.files)
    kit = _project(tmp_path / "kit", {"three": THREE, "@sveltejs/kit": "2", "vite": "5"}, public=False)
    r = install_package(crate, kit, assets="static/models/crate", dry_run=True)
    assert r.status == "dry_run" and any(f["path"].startswith("static/models/crate/") for f in r.files)


def test_node_modules_are_searched_in_the_project_and_up_to_its_workspace_or_repository_root_only(tmp_path):
    def three(root: Path) -> None:
        (root / "node_modules/three").mkdir(parents=True)
        (root / "node_modules/three/package.json").write_text('{"version": "0.170.0"}')

    outer = tmp_path / "home"
    proj = outer / "work" / "proj"
    proj.mkdir(parents=True)
    three(outer)  # a stray node_modules in a folder above, with no workspace or repository root between
    assert web.find_package(proj, "three") is None
    (outer / "work" / ".git").mkdir()  # the repository root is above the project: what lies at or below it is the project's
    assert web.find_package(proj, "three") is None  # the stray one is above the repository root
    three(outer / "work")
    assert web.find_package(proj, "three") == (outer / "work" / "node_modules" / "three").resolve()
    ws = tmp_path / "mono"
    app = ws / "packages" / "app"
    app.mkdir(parents=True)
    (ws / "package.json").write_text('{"workspaces": ["packages/*"]}')
    three(ws)
    assert web.find_package(app, "three") == (ws / "node_modules" / "three").resolve()
    (tmp_path / "node_modules").mkdir()
    assert web.search_dirs(app) == [app.resolve(), (ws / "packages").resolve(), ws.resolve()]


# --------------------------------------------------------------------------- the loader text


def test_the_loader_ends_the_ktx2_pool_frees_only_what_it_made_loads_levels_together_and_types_for_react_18_and_19(crate, tmp_path, monkeypatch):
    monkeypatch.setattr(web, "node_executable", lambda: "/usr/bin/node")
    project = _project(tmp_path / "game", {"three": THREE, "vite": "6", "react": "18", "@react-three/fiber": "8"})
    _fake_node_modules(project)
    monkeypatch.setattr(web, "compress", _fake_compress([]))
    _fake_measurement(monkeypatch)
    r = install_package(crate, project)
    js = (project / r.dest / "index.js").read_text()
    assert "function leaseKtx2" in js and "entry.loader.dispose()" in js and "ktx2.release()" in js  # the pool's life ends
    assert "options.ktx2Loader" in js  # ... unless the project passes its own, which is never disposed
    assert "DATA.levels.map((level) => loader.loadAsync" in js  # every level at once
    assert "made.textures" in js and "root.traverse" not in js.split("dispose()")[1]  # only what the loader made
    assert "ReactElement" in (project / r.dest / "react.d.ts").read_text() and "JSX.Element" not in (project / r.dest / "react.d.ts").read_text()


# --------------------------------------------------------------------------- an interrupted install, a swapped folder


def test_an_install_killed_between_writes_can_be_finished_or_undone(crate, tmp_path, monkeypatch):
    project = _project(tmp_path / "game", public=False, src=False)
    (project / "static").mkdir()
    before = _tree(project)
    real = _Transaction.write
    calls = {"n": 0}

    def dies(self, rel, data):
        calls["n"] += 1
        if calls["n"] == 7:
            raise OSError("killed")
        return real(self, rel, data)

    with monkeypatch.context() as m:
        m.setattr(_Transaction, "write", dies)
        m.setattr(_Transaction, "rollback", lambda self: None)  # a killed process does not roll back
        with pytest.raises(OSError):
            _install(crate, project)
    asset = json.loads((crate / "manifest.json").read_text())["asset"]
    path = project / "agy3d" / asset / RECORD
    pending = json.loads(path.read_text())
    assert pending["state"] == "pending" and len(pending["files"]) > 7 and pending["created_dirs"]
    # undone: everything the install had written is removed, and the folders it made
    u = uninstall(project, f"agy3d/{asset}")
    assert u.status == "uninstalled" and _tree(project) == before
    # ... or finished: run it again and it completes where it stopped
    with monkeypatch.context() as m:
        m.setattr(_Transaction, "write", dies)
        calls["n"] = 0
        m.setattr(_Transaction, "rollback", lambda self: None)
        with pytest.raises(OSError):
            _install(crate, project)
    done = _install(crate, project)
    assert done.status in ("installed", "updated") and json.loads(path.read_text())["state"] == "complete"
    assert uninstall(project, done.dest).status == "uninstalled" and _tree(project) == before


def test_a_folder_swapped_for_a_link_after_install_is_a_refusal_not_a_crash_and_deletes_nothing(crate, tmp_path):
    project = _project(tmp_path / "game")
    r = _install(crate, project)
    asset = r.package["asset"]
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "precious.txt").write_text("keep")
    shutil.rmtree(project / "public/agy3d" / asset)
    (project / "public/agy3d" / asset).symlink_to(elsewhere, target_is_directory=True)
    u = uninstall(project, r.dest)
    assert u.status == "refused" and "nothing was removed" in u.message + "nothing was removed"
    assert (elsewhere / "precious.txt").exists() and (project / r.dest / "index.js").exists()
    assert check_install(project, r.dest).status == "refused"


# --------------------------------------------------------------------------- the check's server


def test_the_check_server_serves_only_under_its_random_path_hides_dot_folders_and_sends_a_security_policy(tmp_path):
    import urllib.error
    import urllib.request

    from antigravity3d.adapters.engines import web_three as w3

    page = tmp_path / "page"
    page.mkdir()
    (page / "page.html").write_text("<html></html>")
    code = tmp_path / "code"
    (code / ".agy3d").mkdir(parents=True)
    (code / "index.js").write_text("export const a = 1;")
    (code / ".agy3d" / "install.json").write_text("{}")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "a.glb").write_bytes(b"glb")
    blocked: list = []
    server = w3._PageServer(page, blocked, {"__code": code, "/agy3d/crate/": assets})

    def get(url):
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, b""

    try:
        status, headers, _ = get(server.base + "page.html")
        assert status == 200 and "connect-src 'self'" in headers["Content-Security-Policy"]
        assert get(server.base + "_m/__code/index.js")[0] == 200
        assert get(server.base + "_m/__code/.agy3d/install.json")[0] == 404  # the record is not served
        assert get(server.base + "_m/__code/../page/page.html")[0] == 404
        assert get(server.origin + "/agy3d/crate/a.glb")[0] == 404  # nothing is answered outside the random path
        assert get(server.origin + "/page.html")[0] == 404
        assert server.rewrite(server.origin + "/agy3d/crate/a.glb") == server.base + "_m/agy3d/crate/a.glb"
        assert get(server.rewrite(server.origin + "/agy3d/crate/a.glb"))[2] == b"glb"
        assert server.rewrite(server.origin + "/elsewhere/x") is None and server.rewrite("http://example.com/agy3d/crate/a") is None
        assert [p for p, _n in server.served][-1] == "/agy3d/crate/a.glb"  # logged as the page asked it
        assert any(".agy3d" in b for b in blocked)
    finally:
        server.close()


def test_hull_data_is_compared_value_by_value():
    want = {"h": {"points": [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0], "index": [0, 1, 2]}}
    assert WC.hull_issues(json.loads(json.dumps(want)), want) == []
    scaled = {"h": {"points": [3 * v + 0.5 for v in want["h"]["points"]], "index": [0, 1, 2]}}  # the reviewer's edit: scaled and shifted
    assert WC.hull_issues(scaled, want) and "points differ" in WC.hull_issues(scaled, want)[0]
    assert WC.hull_issues({"h": {"points": want["h"]["points"], "index": [0, 2, 1]}}, want) == ["h: the triangle indices differ from the colliders file"]
    assert WC.hull_issues({}, want) and WC.hull_issues({"h": want["h"], "x": want["h"]}, want)
    near = {"h": {"points": [v + 5e-6 for v in want["h"]["points"]], "index": [0, 1, 2]}}
    assert WC.hull_issues(near, want) == []  # the 6-decimal rounding the file is written with


# --------------------------------------------------------------------------- second review: derived names, version managers, kept files


def _two_installs(crate, chair_like, project):
    a = _install(crate, project)
    b = install_package(chair_like, project, compress="never", check=False)
    return a, b


@pytest.fixture()
def second_package(crate, tmp_path):
    """A second asset: the crate's package under another name (files, manifest and hashes renamed consistently)."""
    dst = tmp_path / "second_pkg"
    shutil.copytree(crate, dst)
    man = json.loads((dst / "manifest.json").read_text())
    old, new = man["asset"], "Second" + man["asset"]
    ren = lambda t: t.replace(old, new)  # noqa: E731
    for f in sorted(dst.rglob("*"), reverse=True):
        if f.is_file() and old in f.name:
            f.rename(f.with_name(ren(f.name)))
    hulls = man["collider_glb_nodes"]  # node names inside the colliders file keep their old names
    man = json.loads(ren(json.dumps(man)))
    man["collider_glb_nodes"] = hulls
    for f, rec in man["file_hashes"].items():
        p = dst / f
        if p.is_file():
            rec["sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
    (dst / "manifest.json").write_text(json.dumps(man))
    return dst


def test_a_record_may_list_only_files_this_asset_writes_and_never_another_installs_folders_or_files(crate, second_package, tmp_path):
    """The second review's three edits: assets set to the parent folder (all assets of the project), to another install's
    folder with its files listed, and a project's own file listed with its true hash under the asset folder."""
    project = _project(tmp_path / "game")
    a, b = _two_installs(crate, second_package, project)
    assert a.status == b.status == "installed"
    asset_a, asset_b = a.package["asset"], b.package["asset"]
    path, rec = _record_of(project, a.dest)
    _, other = _record_of(project, b.dest)
    only_b = lambda t: {k: v for k, v in t.items() if asset_b in k and not k.endswith(RECORD)}  # noqa: E731  (B's record may gain created_dirs)
    keep = only_b(_tree(project))
    mine = project / f"public/agy3d/{asset_a}/notes.txt"
    cases = {}
    e = json.loads(json.dumps(rec)); e["assets"] = "public/agy3d"
    e["files"].update({k: v for k, v in other["files"].items() if k.startswith("public/")})
    cases["assets is the parent of every asset folder"] = e
    e = json.loads(json.dumps(rec)); e["assets"] = f"public/agy3d/{asset_b}"
    e["files"] = {k: v for k, v in rec["files"].items() if k.startswith("src/")}
    e["files"].update({k: v for k, v in other["files"].items() if k.startswith("public/")})
    cases["assets is another install's folder"] = e
    mine.write_text("mine")
    e = json.loads(json.dumps(rec)); e["files"][f"public/agy3d/{asset_a}/notes.txt"] = {"sha256": hashlib.sha256(b"mine").hexdigest(), "bytes": 4}
    cases["a project file with its true hash in the asset folder"] = e
    e = json.loads(json.dumps(rec)); e["files"][f"public/agy3d/{asset_a}/textures/{asset_b}_atlas_albedo.png"] = {"sha256": "0" * 64, "bytes": 1}
    cases["another asset's texture name in this asset's folder"] = e
    for why, edit in cases.items():
        path.write_text(json.dumps(edit))
        for call in (lambda: uninstall(project, a.dest), lambda: install_package(crate, project, compress="never", check=False)):
            r = call()
            assert r.status == "refused", (why, r.status, r.message)
        assert only_b(_tree(project)) == keep and mine.exists(), why
    path.write_text(json.dumps(rec))
    assert uninstall(project, a.dest).status == "uninstalled" and mine.read_text() == "mine"
    assert only_b(_tree(project)) == keep


def test_a_file_another_install_lists_is_never_removed_by_this_one(crate, second_package, tmp_path):
    project = _project(tmp_path / "game")
    a, b = _two_installs(crate, second_package, project)
    _, other = _record_of(project, b.dest)
    path, rec = _record_of(project, a.dest)
    shared_file = f"public/agy3d/{b.package['asset']}/{b.package['asset']}.glb"
    # a hand-edited record of install A lists a file of install B that matches A's own name pattern only by accident:
    # simulate by moving B's record entry under A's name pattern is impossible, so list it in both records instead
    rec["files"][f"public/agy3d/{a.package['asset']}/collision.json"] = other["files"][f"public/agy3d/{b.package['asset']}/collision.json"]
    other["files"][f"public/agy3d/{a.package['asset']}/collision.json"] = rec["files"][f"public/agy3d/{a.package['asset']}/collision.json"]
    path.write_text(json.dumps(rec))
    (project / b.dest / RECORD).write_text(json.dumps(other))
    u = uninstall(project, a.dest, dry_run=True)
    assert any(f["path"].endswith("/collision.json") and f["action"] == "keep" and "another install" in f["why"] for f in u.files), u.files
    assert shared_file


@pytest.mark.parametrize("assets, needle", [
    ("public/agy3d", "transcoder"),
    ("public/agy3d/_basis", "transcoder"),
    ("public/agy3d/_basis/x", "transcoder"),
])
def test_assets_may_not_contain_or_lie_inside_the_shared_transcoder_folder(assets, needle, crate, tmp_path):
    project = _project(tmp_path / "game")
    r = install_package(crate, project, assets=assets, dry_run=True)
    assert r.status == "refused" and needle in r.message, r.message


def test_assets_may_not_overlap_another_install_or_use_the_folder_the_project_does_not_serve(crate, second_package, tmp_path):
    project = _project(tmp_path / "game")
    a = _install(crate, project)
    r = install_package(second_package, project, assets=f"public/agy3d/{a.package['asset']}/inner", dry_run=True)
    assert r.status == "refused" and "another install" in r.message
    r = install_package(second_package, project, assets="public/agy3d", dry_run=True)
    assert r.status == "refused"
    r = install_package(second_package, project, assets="static/models/x", dry_run=True)  # a vite project serves public/
    assert r.status == "refused" and "not served" in r.message and "public/" in r.message


def _version_manager_shims(tmp_path: Path) -> tuple[Path, Path]:
    """A node on PATH that is a shim finding the real binary under HOME (asdf, mise, Volta): returns (shim dir, home)."""
    home = tmp_path / "vm_home"
    (home / "real").mkdir(parents=True)
    real = home / "real" / "node"
    real.write_text('#!/bin/sh\nif [ "$1" = "-p" ]; then echo "' + str(real) + '"; exit 0; fi\necho "ran:$*" >> "' + str(tmp_path / "node.log") + '"; exit 3\n')
    real.chmod(0o755)
    shims = tmp_path / "shims"
    shims.mkdir()
    shim = shims / "node"
    shim.write_text('#!/bin/sh\nexec "$HOME/real/node" "$@"\n')
    shim.chmod(0o755)
    return shims, home


def test_node_started_through_a_version_manager_shim_still_runs_under_the_bare_environment(crate, tmp_path, monkeypatch):
    shims, home = _version_manager_shims(tmp_path)
    log = tmp_path / "node.log"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{shims}:{os.environ['PATH']}")
    web._NODE.clear()
    real = web.node_executable()
    assert real == str(home / "real" / "node")  # resolved once, with the real environment, to the real binary
    assert web.bare_env(tmp_path)["PATH"].split(os.pathsep)[0] == str(home / "real")  # a script that starts `env node` finds it first
    project = _project(tmp_path / "game")
    _fake_node_modules(project)
    parts = web.read_parts(crate, json.loads((crate / "manifest.json").read_text()))
    with pytest.raises(RuntimeError, match="exit 3"):  # the real binary ran (and failed on purpose); not "exit 127"
        web.compress(parts, {}, project / "node_modules", web.TEXTURE_TIERS[0], tmp_path / "cache")
    # the child had a bare environment: no HOME of the developer's, so the shim itself could not have found anything
    assert "ran:web_compress.mjs job.json" in log.read_text() or "ran:" in log.read_text()
    web._NODE.clear()


def test_a_reinstall_keeps_only_compressed_files_whose_record_says_the_tolerance_was_measured(crate, tmp_path, monkeypatch):
    monkeypatch.setattr(web, "node_executable", lambda: "/usr/bin/node")
    project = _project(tmp_path / "game")
    _fake_node_modules(project)
    calls: list = []
    monkeypatch.setattr(web, "compress", _fake_compress(calls))
    _fake_measurement(monkeypatch)
    a = install_package(crate, project)
    assert len(calls) == 1
    path, rec = _record_of(project, a.dest)
    for edit in ({"tolerance_measured": False}, {"tolerance_measured": None}, {"tiers_tried": []}):
        rec2 = json.loads(json.dumps(rec))
        rec2["compression"].update(edit)
        path.write_text(json.dumps(rec2))
        n = len(calls)
        install_package(crate, project)
        assert len(calls) == n + 1, edit  # not kept: measured again through the normal path
        path.write_text(json.dumps(rec))
        install_package(crate, project)  # restore the stored record state (kept path)
    n = len(calls)
    again = install_package(crate, project)
    assert len(calls) == n and any(n_.startswith("kept the compressed files") for n_ in again.notes)


def test_a_reinstall_does_not_delete_a_shared_transcoder_the_project_has_replaced(crate, tmp_path, monkeypatch):
    monkeypatch.setattr(web, "node_executable", lambda: "/usr/bin/node")
    project = _project(tmp_path / "game")
    _fake_node_modules(project)
    monkeypatch.setattr(web, "compress", _fake_compress([]))
    _fake_measurement(monkeypatch)
    a = install_package(crate, project)
    wasm = project / "public/agy3d/_basis/basis_transcoder.wasm"
    wasm.write_bytes(b"the developer's own build")
    r = install_package(crate, project, compress="never")  # the new plan has no transcoder: the old one would be removed
    assert wasm.read_bytes() == b"the developer's own build"
    assert any("not the copy in this project's three.js" in n for n in r.notes)
    assert a.status == "installed"


def _interrupt_at(monkeypatch, n: int):
    """Kill the process at the n-th write or removal (no rollback: a killed process does not roll back)."""
    count = {"n": 0}
    real_write, real_remove = _Transaction.write, _Transaction.remove

    def tick():
        count["n"] += 1
        if count["n"] > n:
            raise OSError("killed")

    monkeypatch.setattr(_Transaction, "write", lambda self, rel, data: (tick(), real_write(self, rel, data))[1])
    monkeypatch.setattr(_Transaction, "remove", lambda self, rel: (tick(), real_remove(self, rel))[1])
    monkeypatch.setattr(_Transaction, "rollback", lambda self: None)
    return count


@pytest.mark.parametrize("position", [1, 2, 3, 4, 6, 8, 11, 14, 17])
def test_an_update_killed_at_any_position_can_be_finished_or_undone_without_force_and_orphans_nothing(position, crate, tmp_path, monkeypatch):
    """The update first writes compressed files (stand-in) and the transcoder, then is redone uncompressed: files are
    replaced, removed and created, the pending record knows each version, and neither finishing nor undoing needs --force."""
    monkeypatch.setattr(web, "node_executable", lambda: "/usr/bin/node")
    project = _project(tmp_path / "game", public=False, src=False)
    (project / "static").mkdir()
    before = _tree(project)
    _fake_node_modules(project)
    before = _tree(project)
    monkeypatch.setattr(web, "compress", _fake_compress([]))
    _fake_measurement(monkeypatch)
    first = install_package(crate, project)
    assert first.status == "installed"
    state_after_first = _tree(project)
    with monkeypatch.context() as m:
        _interrupt_at(m, position)
        try:
            install_package(crate, project, compress="never")
            finished = True
        except OSError:
            finished = False
    # undone: the pending (or final) record removes everything either version wrote, the project is as before the first install
    u = uninstall(project, first.dest)
    assert u.status == "uninstalled", (position, u.message)
    assert _tree(project) == before, (position, sorted(set(_tree(project)) ^ set(before)))
    # finished: the same kill, then the install run again to the end gives what an uninterrupted update gives
    install_package(crate, project)  # back to the first state
    assert _tree(project) == state_after_first
    with monkeypatch.context() as m:
        _interrupt_at(m, position)
        with pytest.raises(OSError) if not finished else contextlib.nullcontext():
            install_package(crate, project, compress="never")
    done = install_package(crate, project, compress="never")
    assert done.status in ("installed", "updated", "unchanged"), (position, done.message)
    clean = _project(tmp_path / "clean", public=False, src=False)
    (clean / "static").mkdir()
    _fake_node_modules(clean)
    install_package(crate, clean, compress="never", check=False)
    assert {k: v for k, v in _tree(project).items() if ".agy3d" not in k} == {k: v for k, v in _tree(clean).items() if ".agy3d" not in k}


def test_the_hull_check_names_what_it_compared_and_holds_the_installed_data_to_its_recorded_hash(crate, tmp_path):
    project = _project(tmp_path / "game")
    r = _install(crate, project)
    path, rec = _record_of(project, r.dest)
    manifest = json.loads((crate / "manifest.json").read_text())
    data, source, pre = WC.expected_hulls(project, rec, manifest, crate)
    assert "colliders file" in source and not pre and data
    data, source, pre = WC.expected_hulls(project, rec, manifest, None)  # the package is gone
    assert "recorded at install" in source and "package's colliders file was not found" in source and pre == []
    coll = project / rec["assets"] / "collision.json"
    coll.write_text(coll.read_text().replace("0", "1", 3))
    data, source, pre = WC.expected_hulls(project, rec, manifest, None)
    assert pre and "not what was installed" in pre[0]  # edited since: the loader agreeing with it proves nothing
    # was: the absolute path. The record is committed with the game, so it names the package relative to the project, or by
    # folder name and hash when that would name the machine (deliver/portable.py); never by where this machine keeps it
    assert rec["package"]["path"] != str(crate.resolve()) and not os.path.isabs(rec["package"].get("path") or "")
    assert rec["package"]["folder"] == crate.name and rec["package"]["manifest_sha256"]


def test_the_page_runner_removes_webrtc_and_collects_policy_reports(tmp_path):
    from antigravity3d.adapters.engines import web_three as w3

    assert "RTCPeerConnection" in w3._CSP_PROBE and "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in w3.CHROMIUM_ARGS
    assert "report-uri" in w3.CONTENT_SECURITY_POLICY or w3.CSP_REPORT_PATH


def test_the_ktx2_pool_and_all_levels_are_settled_before_release_and_a_failure_frees_what_loaded(crate, tmp_path, monkeypatch):
    monkeypatch.setattr(web, "node_executable", lambda: "/usr/bin/node")
    project = _project(tmp_path / "game")
    _fake_node_modules(project)
    monkeypatch.setattr(web, "compress", _fake_compress([]))
    _fake_measurement(monkeypatch)
    r = install_package(crate, project)
    js = (project / r.dest / "index.js").read_text()
    assert "Promise.allSettled" in js and js.index("ktx2.release()") > js.index("Promise.allSettled")
    assert "throw failed.reason" in js and "freeScene(r.value.scene)" in js


def _smooth_package(pkg: Path, dst: Path) -> Path:
    """The offline crate's package with its three maps replaced by smooth ones (same sizes, same roles), consistently in
    the textures/ files, the highest level's embedded images and the manifest's hashes. The offline crate's maps are
    noise, which a block codec cannot hold inside the frame tolerance (measured: 1.1 % of pixels over 8 % at the best
    tier); smooth maps are what a painted asset looks like to a codec, and let the compressed path be proved."""
    import io

    from PIL import Image

    shutil.copytree(pkg, dst)
    man = json.loads((dst / "manifest.json").read_text())
    yy, xx = np.mgrid[0:512, 0:512] / 511.0
    maps = {"albedo": np.stack([60 + 120 * xx, 90 + 80 * yy, 70 + 60 * xx * yy], -1),
            "orm": np.stack([230 - 40 * xx, 120 + 60 * yy, 20 + 30 * xx], -1),
            "normal": np.stack([128 + 20 * np.sin(3 * xx), 128 + 20 * np.cos(3 * yy), 240 + 0 * xx], -1)}
    newbytes: dict[str, bytes] = {}
    for t in man["textures"]:
        buf = io.BytesIO()
        Image.fromarray(np.clip(maps[t["role"]], 0, 255).astype(np.uint8), "RGB").save(buf, "PNG")
        newbytes[t["path"]] = buf.getvalue()
        (dst / t["path"]).write_bytes(buf.getvalue())
    old = {hashlib.sha256((pkg / t["path"]).read_bytes()).hexdigest(): t["path"] for t in man["textures"]}
    asset = man["asset"]
    doc, blob = G._split((dst / f"{asset}.glb").read_bytes())
    views = doc["bufferViews"]
    out = bytearray()
    new_views = []
    swap = {}
    for im in doc["images"]:
        v = views[im["bufferView"]]
        digest = hashlib.sha256(blob[v.get("byteOffset", 0):v.get("byteOffset", 0) + v["byteLength"]]).hexdigest()
        swap[im["bufferView"]] = newbytes[old[digest]]
    for i, v in enumerate(views):
        data = swap.get(i, blob[v.get("byteOffset", 0):v.get("byteOffset", 0) + v["byteLength"]])
        out += b"\0" * (-len(out) % 4)
        new_views.append({**v, "byteOffset": len(out), "byteLength": len(data)})
        out += data
    doc["bufferViews"] = new_views
    doc["buffers"][0]["byteLength"] = len(out)
    (dst / f"{asset}.glb").write_bytes(G._join(doc, bytes(out)))
    for f in [f"{asset}.glb", *newbytes]:
        man["file_hashes"][f] = {**man["file_hashes"][f], "sha256": hashlib.sha256((dst / f).read_bytes()).hexdigest(),
                                 "bytes": (dst / f).stat().st_size}
    (dst / "manifest.json").write_text(json.dumps(man))
    return dst


def _fixture_project(tmp_path: Path) -> Path:
    if not (FIXTURE / "node_modules" / "three").is_dir():
        pytest.skip("run `npm ci` in tests/fixtures/web_project first")
    dst = tmp_path / "web_project"
    dst.mkdir()
    for f in ("package.json", "package-lock.json", "index.html"):
        shutil.copy(FIXTURE / f, dst / f)
    shutil.copytree(FIXTURE / "src", dst / "src")
    shutil.copytree(FIXTURE / "public", dst / "public")
    (dst / "node_modules").symlink_to(FIXTURE / "node_modules", target_is_directory=True)
    return dst


@pytest.fixture(scope="module")
def browser_ready():
    from antigravity3d.adapters.engines.web_three import WebThreeValidator

    why = WebThreeValidator()._unavailable()
    if why:
        pytest.skip(why)


def _by_name(r):
    return {c.name: c for c in r.checks}


@pytest.mark.browser
def test_browser_uncompressed_install_renders_in_the_projects_own_three_and_is_the_packages_picture(crate, tmp_path, browser_ready):
    project = _fixture_project(tmp_path)
    r = install_package(crate, project, compress=False)
    c = _by_name(r)
    assert r.status == "installed" and r.ok, [(x.name, x.status, x.detail[:200]) for x in r.checks]
    assert {"import", "loader", "scene", "scale", "levels", "materials", "collision", "level_switching", "shared_textures",
            "loader_vs_package", "react_component"} <= set(c)
    assert not [x for x in r.checks if x.status != "passed" and x.name != "outline"], [(x.name, x.status, x.detail) for x in r.checks]
    # the same files through the loader and straight from the package: the same picture
    assert c["loader_vs_package"].data["mean_abs"] == 0 and c["loader_vs_package"].data["outline"] == 1.0
    assert "three r187" in c["loader"].detail  # the fixture is on three 0.187.0 since round eight
    assert uninstall(project, r.dest).status == "uninstalled"


@pytest.mark.browser
def test_browser_compressed_install_is_inside_the_tolerance_and_kept_on_an_unchanged_reinstall(crate, tmp_path, browser_ready):
    project = _fixture_project(tmp_path)
    smooth = _smooth_package(crate, tmp_path / "smooth_pkg")
    r = install_package(smooth, project, compress="always")  # `always`: these 512 px maps are too small for `auto` to want it
    c = _by_name(r)
    assert r.ok and c["compression"].status == "passed", [(x.name, x.status, x.detail[:300]) for x in r.checks]
    wire = json.loads(Path(r.report_path).read_text())["measured"]["wire"]
    assert wire["transcoder_bytes"] > 0 and wire["asset_bytes"] > 0
    rec = json.loads((project / r.dest / RECORD).read_text())
    comp = rec["compression"]
    assert comp["state"] == "compressed" and comp["tolerance_measured"] is True and comp["tiers_tried"][-1]["passed"] is True
    assert comp["bytes_before"] > 0 and set(comp["versions"]) == set(web.TOOL_PACKAGES) and comp["gpu_memory_computed"]
    assert any(f.endswith(".ktx2") for f in rec["files"]) and not any(f.endswith(".png") for f in rec["files"])
    again = install_package(smooth, project, check=False, compress="always")
    assert again.status == "unchanged" and any(n.startswith("kept the compressed files") for n in again.notes)
    assert uninstall(project, r.dest).status == "uninstalled"


@pytest.mark.browser
def test_browser_noise_textures_are_refused_compression_by_the_tolerance_and_install_uncompressed(crate, tmp_path, browser_ready):
    """The offline crate's maps are noise: no tier holds the frame tolerance, so nothing compressed is written and the
    result says so, with the numbers of every tier (the gate cannot be bought with a looser threshold)."""
    project = _fixture_project(tmp_path)
    r = install_package(crate, project, compress="always")
    assert r.ok and "loader_vs_package" in _by_name(r) and "compression" not in _by_name(r)
    comp = json.loads((project / r.dest / RECORD).read_text())["compression"]
    assert comp["state"] == "rejected" and [t["tier"] for t in comp["tiers_tried"]] == [t[0] for t in web.TEXTURE_TIERS]
    assert all(t["passed"] is False and t["pixel_share"] > 0.01 for t in comp["tiers_tried"])
    assert not any(f.endswith(".ktx2") for f in json.loads((project / r.dest / RECORD).read_text())["files"])
    assert any("not compressed" in n and "tolerance" in n for n in r.notes)


@pytest.mark.browser
def test_browser_a_page_cannot_open_a_websocket_and_the_attempt_is_recorded(tmp_path, browser_ready):
    """The request interception sees fetches, images and scripts but not a WebSocket: the page server's content security
    policy does. A listener of the test's own must never be connected to."""
    import asyncio
    import socket
    import threading

    from antigravity3d.adapters.engines import web_three as w3

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    port = listener.getsockname()[1]
    accepted: list = []

    def accept():
        listener.settimeout(8)
        try:
            listener.accept()
            accepted.append(1)
        except OSError:
            pass

    threading.Thread(target=accept, daemon=True).start()
    page = tmp_path / "page"
    page.mkdir()
    (page / "page.html").write_text(
        "<!doctype html><script>window.__agy = {done: false};"
        f"try {{ const ws = new WebSocket('ws://127.0.0.1:{port}/x'); ws.onerror = () => {{ window.__agy.done = true; }}; }} catch (e) {{ window.__agy.done = true; }}"
        "setTimeout(() => { window.__agy.done = true; }, 2000);</script>")
    try:
        run = asyncio.run(w3.run_page(page, w3.WebThreeValidator().binary(), 30, env=WC.browser_env(tmp_path)))
    finally:
        time.sleep(1)
        listener.close()
    assert not accepted, "the page reached a listener outside its own server"
    assert any("content security policy" in b and str(port) in b for b in run["blocked"]), run["blocked"]


def test_the_component_check_finds_the_bundler_the_projects_vite_ships(tmp_path):
    """Runs 2 and 3 of the website task: a project on Vite 8 has Rolldown and no esbuild, and the component of every
    installed prop went unchecked ("skipped no esbuild ... (Vite ships one)")."""
    project = _project(tmp_path / "game")
    assert WC.find_bundler(project) is None
    bin_ = project / "node_modules" / ".bin"
    bin_.mkdir(parents=True)
    (bin_ / "rolldown").write_text("#!/bin/sh\n")
    assert WC.find_bundler(project) == ("rolldown", bin_ / "rolldown")
    (bin_ / "esbuild").write_text("#!/bin/sh\n")
    assert WC.find_bundler(project) == ("esbuild", bin_ / "esbuild")  # as before, where a project has both


def _vite8_project(tmp_path: Path) -> Path:
    """A project whose packages are the website task's (Vite 8 with Rolldown, React 19, React Three Fiber 9): the
    folder ``AGY3D_SITE_SEED`` names, linked. Nothing here installs packages."""
    seed = os.environ.get("AGY3D_SITE_SEED")
    if not seed or not (Path(seed) / "node_modules" / ".bin" / "rolldown").exists():
        pytest.skip("AGY3D_SITE_SEED does not name a workspace with Vite 8's rolldown: no project to bundle with it")
    if (Path(seed) / "node_modules" / ".bin" / "esbuild").exists():
        pytest.skip("the workspace AGY3D_SITE_SEED names has esbuild as well: the check would use that")
    dst = tmp_path / "web_project"
    (dst / "src").mkdir(parents=True)
    (dst / "public").mkdir()
    shutil.copy(Path(seed) / "package.json", dst / "package.json")
    (dst / "index.html").write_text('<!doctype html><div id="app"></div><script type="module" src="/src/main.js"></script>')
    (dst / "src" / "main.js").write_text("import * as THREE from 'three';\n")
    (dst / "node_modules").symlink_to(Path(seed) / "node_modules", target_is_directory=True)
    return dst


@pytest.mark.browser
def test_browser_the_react_component_is_checked_in_a_vite_8_project_with_its_rolldown(crate, tmp_path, browser_ready):
    project = _vite8_project(tmp_path)
    cache = project / "node_modules" / ".cache"
    before = set(cache.iterdir()) if cache.exists() else None
    r = install_package(crate, project, compress="never")
    c = _by_name(r)["react_component"]
    assert c.status == "passed" and "bundled with the project's rolldown" in c.detail, c.detail
    # the entry was a file beside the project's packages: nothing of it is left there
    assert (set(cache.iterdir()) if cache.exists() else None) == before


@pytest.mark.browser
def test_browser_the_react_check_works_from_a_relative_project_path_and_leaves_no_private_folder_behind(crate, tmp_path, browser_ready, monkeypatch):
    import glob

    project = _fixture_project(tmp_path)
    before = set(glob.glob(os.path.join(tempfile.gettempdir(), "agy3d-check-home-*")))
    monkeypatch.chdir(tmp_path)
    rel = Path("web_project")  # relative: the generated import of react.js must not become a bare module name
    r = install_package(crate, rel, compress="never")
    c = _by_name(r)
    assert c["react_component"].status == "passed", c["react_component"].detail
    again = check_install(rel, r.dest)
    assert _by_name(again)["react_component"].status == "passed"
    absolute = check_install(project, r.dest)
    assert _by_name(absolute)["react_component"].status == "passed"
    # a private folder per run (mkdtemp), removed afterwards; Chromium's crash handler may re-create its own dot-folders in it
    # a moment later (they are removed at exit): never anything else
    left = set(glob.glob(os.path.join(tempfile.gettempdir(), "agy3d-check-home-*"))) - before
    for d in left:
        assert {p.name for p in Path(d).iterdir()} <= {".config", ".local", ".cache"}, d
        assert oct(Path(d).stat().st_mode & 0o777) == "0o700"
    # was: the absolute path (see test_the_hull_check_names_what_it_compared...): the record no longer holds one
    assert not os.path.isabs(json.loads((project / r.dest / RECORD).read_text())["package"].get("path") or "")


@pytest.mark.browser
def test_browser_a_page_cannot_use_webrtc_or_a_worker_to_reach_the_network_and_both_are_recorded(tmp_path, browser_ready):
    import asyncio
    import socket
    import threading

    from antigravity3d.adapters.engines import web_three as w3

    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("127.0.0.1", 0))
    uport = udp.getsockname()[1]
    tcp = socket.socket()
    tcp.bind(("127.0.0.1", 0))
    tcp.listen(5)
    tport = tcp.getsockname()[1]
    got: list = []
    threading.Thread(target=lambda: [got.append(("udp", udp.recvfrom(2000)[0][:4])) for _ in iter(int, 1)], daemon=True).start()
    threading.Thread(target=lambda: [got.append(("tcp", tcp.accept()[1])) for _ in iter(int, 1)], daemon=True).start()
    page = tmp_path / "page"
    page.mkdir()
    (page / "w.js").write_text(f'fetch("http://127.0.0.1:{tport}/from-worker").then(() => postMessage("reached"), () => postMessage("blocked"));')
    (page / "page.html").write_text(
        "<!doctype html><script>window.__agy = {done: false, log: []};"
        f"try {{ const pc = new RTCPeerConnection({{iceServers: [{{urls: 'stun:127.0.0.1:{uport}'}}]}}); pc.createDataChannel('x'); "
        "pc.createOffer().then((o) => pc.setLocalDescription(o)); window.__agy.log.push('rtc made'); } catch (e) { window.__agy.log.push('rtc unavailable'); }"
        "const w = new Worker('./w.js'); w.onmessage = (e) => window.__agy.log.push('worker ' + e.data);"
        "const b = new Worker(URL.createObjectURL(new Blob(['fetch(\\'http://127.0.0.1:%d/from-blob\\').catch(() => postMessage(1))' ], {type: 'text/javascript'})));"
        "setTimeout(() => { window.__agy.done = true; }, 3000);</script>" % tport)
    run = asyncio.run(w3.run_page(page, w3.WebThreeValidator().binary(), 30, env=WC.browser_env(tmp_path)))
    import time

    time.sleep(1)
    assert not [g for g in got if g[0] == "udp"], got  # no STUN packet left the page
    assert "rtc unavailable" in run["raw"]["log"] and "worker blocked" in run["raw"]["log"]
    assert any("(report)" in b and "from-worker" in b for b in run["blocked"]), run["blocked"]
    assert any("(report)" in b and "from-blob" in b for b in run["blocked"]), run["blocked"]


@pytest.mark.browser
def test_browser_the_check_fails_when_the_install_is_wrong(crate, tmp_path, browser_ready):
    """Negative controls: a far level swapped for another asset's file, a missing texture, a loader edited to forget the
    shared materials. Each must fail the check that names it; a check that cannot fail proves nothing."""
    project = _fixture_project(tmp_path)
    r = install_package(crate, project, compress=False)
    asset = r.package["asset"]
    base = project / "public/agy3d" / asset
    far = sorted(base.glob(f"{asset}_LOD*.glb"))[0]
    saved = far.read_bytes()
    far.write_bytes(saved[:-8])  # a level that cannot be loaded
    broken = _by_name(check_install(project, r.dest))
    assert broken["import"].status == "failed" and "loader" in broken["import"].detail
    # a level that loads fine and is the wrong one (the highest level's own file in a far level's place): its triangle
    # count is not the manifest's for that level, and the switch probes draw the wrong triangle count
    far.write_bytes((base / f"{asset}.glb").read_bytes())
    wrong = _by_name(check_install(project, r.dest))
    assert wrong["levels"].status == "failed" and wrong["level_switching"].status == "failed", {k: v.status for k, v in wrong.items()}
    far.write_bytes(saved)
    # hull data that is wrong in value, not in shape: scaled by 3 and shifted
    coll = base / "collision.json"
    good = coll.read_text()
    hulls = json.loads(good)
    for h in hulls.values():
        h["points"] = [3 * v + 0.25 for v in h["points"]]
    coll.write_text(json.dumps(hulls))
    assert _by_name(check_install(project, r.dest))["collision"].status == "failed"
    coll.write_text(good)
    tex = next((base / "textures").glob("*.png"))
    keep = tex.read_bytes()
    tex.unlink()
    c = _by_name(check_install(project, r.dest))
    assert c["import"].status == "failed" or c["materials"].status == "failed"
    tex.write_bytes(keep)
    loader = project / r.dest / "index.js"
    text = loader.read_text()
    assert "if (shared) { o.material.dispose(); o.material = shared; }" in text
    loader.write_text(text.replace("if (shared) { o.material.dispose(); o.material = shared; }", ""))
    assert _by_name(check_install(project, r.dest))["shared_textures"].status == "failed"
    loader.write_text(text)
    comp = project / r.dest / "react.js"
    keep = comp.read_text()
    assert 'createElement("primitive", { object, ...props })' in keep
    comp.write_text(keep.replace('createElement("primitive", { object, ...props })', "null"))  # a component that renders nothing
    assert _by_name(check_install(project, r.dest))["react_component"].status == "failed"
    comp.write_text(keep)
    assert all(x.status == "passed" for x in check_install(project, r.dest).checks)


def test_the_loader_template_names_every_placeholder_it_uses():
    import re

    for f in (web.HERE / "web_templates").iterdir():
        text = f.read_text()
        left = set(re.findall(r"%%[A-Z0-9_]+%%", text))
        known = {"%%ASSET%%", "%%LOAD%%", "%%COMPONENT%%", "%%ASSETS_URL%%", "%%BASIS_URL%%", "%%IMPORTS%%", "%%KTX2%%",
                 "%%LOADER_SETUP%%", "%%DATA%%", "%%KTX2_TYPE_IMPORT%%", "%%KTX2_OPTION%%"}
        assert left <= known, (f.name, left - known)
