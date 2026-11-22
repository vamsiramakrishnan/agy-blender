"""An edit keeps the layout of what it did not touch (fast_ops._pack_kept), a package carries what a re-bake needs
(sources/), and the manifest says where each part is in a one-primitive GLB (glb_part_ranges).

The promise, as a developer reads it: a part an edit does not change keeps its geometry, its uvs and the texels under
them. Its uvs are the same bytes while the atlas keeps its size; when the atlas has to grow, they are rescaled and the
texels under them are where they were. Texels are identical for image-mapped and flat materials; a surface generated
from the geometry, and any part whose chart had to move (the layout was repacked: the result says so and why), is held
to a bound on its look instead (dags.edit_asset.look_check)."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from antigravity3d.ir import IRPatch, MaterialIR, NodeIR, SceneIR, SetTransform, Transform
from antigravity3d.kernel.coplanar import opaque_materials
from antigravity3d.kernel.fast_ops import run_export_pipeline
from antigravity3d.kernel.package import layout_from_package, write_engine_package
from antigravity3d.kernel.surface_detail import rasterise
from antigravity3d.kernel.usd_stage import StageManager
from antigravity3d.state import EngineBudget

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))

ASSET = "Shelf"
B = EngineBudget("godot_4")


def noise(path: Path, size: int, tint) -> str:
    """A tileable noise map: fine detail, so a part's look reads the same wherever its chart samples it."""
    rng = np.random.default_rng(len(path.name))
    a = rng.integers(0, 60, (size, size, 1)) + np.asarray(tint, np.int64)
    Image.fromarray(np.clip(a, 0, 255).astype(np.uint8).repeat(1, 2)).save(path)
    return str(path)


def scene(d: Path, n: int = 40) -> SceneIR:
    """``n`` separate boxes on a grid, half on an image-mapped material (512 px noise: 256 px/m) and half flat."""
    mats = [MaterialIR(id="wood", albedo_uri=noise(d / "wood.png", 512, (120, 80, 40))),
            MaterialIR(id="iron", base_color=(0.3, 0.3, 0.35), metallic=1.0)]
    nodes = [NodeIR(id=f"b{i:02d}", kind="box", params={"size": (0.2, 0.15, 0.1 + 0.01 * (i % 5))},
                    transform=Transform(translate=(0.4 * (i % 8), 0.4 * (i // 8), 0.05)), material_id="wood" if i % 2 else "iron")
             for i in range(n)]
    return SceneIR(asset_name=ASSET, materials=mats, nodes=nodes)


def export(d: Path, sc: SceneIR, keep=None, budget: EngineBudget = B):
    sm = StageManager.create(d / "w.usda", sc.asset_name)
    sm.apply_ir(sc)
    sm.keep_layout = keep
    bundle = run_export_pipeline(sm.extract_meshes(), budget, sc.asset_name, opaque_materials=opaque_materials(sc))
    pm = write_engine_package(sm, bundle, budget, d / "pkg", asset_name=sc.asset_name)
    assert pm.ok, [i for i in pm.issues if i.severity == "critical"]
    return bundle, pm


def scaled(sc: SceneIR, ids, k: float, d: Path) -> SceneIR:
    import uuid

    d.mkdir(parents=True, exist_ok=True)
    sm = StageManager.create(d / f"patch-{uuid.uuid4().hex[:8]}.usda", sc.asset_name)
    sm.apply_ir(sc)
    sm.apply_patch(IRPatch(ops=[SetTransform(node_id=n.id, transform=n.transform.model_copy(update={"scale": (k, k, k)}))
                                for n in sc.nodes if n.id in ids]))
    return sm.base_ir()


def kept_parts(a: Path, b: Path, changed) -> tuple[list[str], list[str], int, int]:
    """(parts not changed, of them with byte-identical uvs, texels under those, of them identical)."""
    la, lb = layout_from_package(a), layout_from_package(b)
    rest = [n for n in la.parts if n not in changed]
    same = [n for n in rest if np.array_equal(la.parts[n][2], lb.parts[n][2]) and np.array_equal(la.parts[n][1], lb.parts[n][1])]
    fa, fb = (json.loads((p / "manifest.json").read_text())["texture_atlas"]["files"]["albedo"] for p in (a, b))
    ia, ib = np.asarray(Image.open(a / fa).convert("RGB")), np.asarray(Image.open(b / fb).convert("RGB"))
    _, drawn, _, _ = rasterise(la.size, [(1, la.parts[n][2], la.parts[n][1]) for n in same])
    eq = (ia[drawn > 0] == ib[drawn > 0]).all(axis=1) if ia.shape == ib.shape else np.zeros(int((drawn > 0).sum()), bool)
    return rest, same, int(len(eq)), int(eq.sum())


def test_an_unchanged_design_keeps_every_chart_and_without_a_shipped_layout_nothing_changes(tmp_path):
    sc = scene(tmp_path)
    b0, p0 = export(tmp_path / "a", sc)
    assert all(u.layout is None for u in b0.uvs) and "layout" not in p0.data["texture_atlas"]
    b1, _ = export(tmp_path / "b", sc)  # no shipped layout: the export as before, byte for byte
    assert all(np.array_equal(x.uvs, y.uvs) for x, y in zip(b0.uvs, b1.uvs))
    assert (tmp_path / "a" / "pkg" / f"{ASSET}.glb").read_bytes() == (tmp_path / "b" / "pkg" / f"{ASSET}.glb").read_bytes()
    keep = layout_from_package(tmp_path / "a" / "pkg")
    b2, p2 = export(tmp_path / "c", sc, keep)
    (lay,) = {u.layout for u in b2.uvs if u.shared_atlas}
    n_all = int(lay.split(" of ")[1].split()[0])
    assert lay.startswith(f"kept: {n_all} of {n_all} charts") and p2.data["texture_atlas"]["layout"]["result"] == "kept"
    assert all(np.array_equal(x.uvs, y.uvs) for x, y in zip(b0.uvs, b2.uvs))
    assert (tmp_path / "a" / "pkg" / f"{ASSET}.glb").read_bytes() == (tmp_path / "c" / "pkg" / f"{ASSET}.glb").read_bytes()


def test_two_enlarged_parts_of_forty_leave_the_others_uvs_and_texels_as_they_were(tmp_path):
    sc = scene(tmp_path)
    _, p0 = export(tmp_path / "a", sc)
    keep = layout_from_package(tmp_path / "a" / "pkg")
    changed = {"b03", "b04"}  # one image-mapped, one flat
    b1, p1 = export(tmp_path / "b", scaled(sc, changed, 1.25, tmp_path / 'p'), keep)
    assert p1.data["texture_atlas"]["layout"]["result"] == "kept", p1.data["texture_atlas"]["layout"]
    rest, same, n_tex, n_same = kept_parts(tmp_path / "a" / "pkg", tmp_path / "b" / "pkg", changed)
    assert len(rest) == 38 and same == rest and n_same == n_tex > 0
    # both kinds of material among the kept parts: flat patches and image-mapped texels alike
    mats = {n.material_id for n in sc.nodes if n.id in same}
    assert mats == {"wood", "iron"}


def test_a_change_that_does_not_fit_repacks_says_why_and_keeps_the_look_within_the_bound(tmp_path):
    from antigravity3d.dags.edit_asset import look_check

    sc = scene(tmp_path)
    capped = EngineBudget("godot_4", max_texture_px=512)  # an art direction's texture size: the atlas cannot grow past it
    export(tmp_path / "a", sc, budget=capped)
    keep = layout_from_package(tmp_path / "a" / "pkg")
    changed = {f"b{i:02d}" for i in range(0, 40, 2)}  # every flat part twice its size, and four mapped ones
    _, p1 = export(tmp_path / "b", scaled(sc, changed | {"b01", "b03", "b05", "b07"}, 2.0, tmp_path / 'p'), keep, budget=capped)
    lay = p1.data["texture_atlas"]["layout"]
    assert lay["result"] == "repacked" and lay["detail"].startswith("repacked: ") and len(lay["detail"]) > 20
    look = look_check(tmp_path / "a" / "pkg", tmp_path / "b" / "pkg", [n.id for n in sc.nodes if n.id not in changed and n.id not in {"b01", "b03", "b05", "b07"}])
    assert look["parts"] and look["outside"] == 0, look


def test_a_refused_attempt_leaves_no_trace_in_the_next(tmp_path):
    """Every attempt of one edit reads the same shipped layout: an attempt that was refused (its package discarded) does
    not change what the next one keeps."""
    sc = scene(tmp_path)
    export(tmp_path / "a", sc)
    keep = layout_from_package(tmp_path / "a" / "pkg")
    export(tmp_path / "refused", scaled(sc, {f"b{i:02d}" for i in range(20)}, 2.0, tmp_path / 'p'), keep)
    b2, _ = export(tmp_path / "second", scaled(sc, {"b03"}, 1.2, tmp_path / 'p'), keep)
    b3, _ = export(tmp_path / "alone", scaled(sc, {"b03"}, 1.2, tmp_path / 'p'), keep)
    assert all(np.array_equal(x.uvs, y.uvs) for x, y in zip(b2.uvs, b3.uvs))


def test_the_manifest_says_where_each_part_is_and_the_diff_reads_parts_by_it(tmp_path):
    import compare_eval as ev
    import pygltflib

    sc = scene(tmp_path)
    _, p0 = export(tmp_path / "a", sc)
    ranges = p0.data["glb_part_ranges"][f"{ASSET}.glb"]
    g = pygltflib.GLTF2().load(str(tmp_path / "a" / "pkg" / f"{ASSET}.glb"))
    counts = {n.name: g.accessors[g.meshes[n.mesh].primitives[0].indices].count // 3 for n in g.nodes if n.mesh is not None}
    for mesh, rows in ranges.items():
        assert [r[1] for r in rows] == list(np.cumsum([0] + [r[2] for r in rows[:-1]])) and sum(r[2] for r in rows) == counts[mesh]
    assert sorted(r[0] for rows in ranges.values() for r in rows) == sorted(n.id for n in sc.nodes)
    keep = layout_from_package(tmp_path / "a" / "pkg")
    export(tmp_path / "b", scaled(sc, {"b03", "b04"}, 1.3, tmp_path / 'p'), keep)
    d = ev.package_diff(tmp_path / "a" / "pkg" / f"{ASSET}.glb", tmp_path / "b" / "pkg" / f"{ASSET}.glb", target_ratio=1.3)
    assert d["read_by"].startswith("part ranges") and d["parts"] == [40, 40] and d["unchanged"] == 38 and d["on_target"] == 2
    assert d["unchanged_uvs_identical"] == 38
    # a file with no manifest (another arm's): connected pieces, as before
    bare = tmp_path / "bare"
    bare.mkdir()
    shutil.copy(tmp_path / "a" / "pkg" / f"{ASSET}.glb", bare / "x.glb")
    assert ev.package_diff(bare / "x.glb", bare / "x.glb")["read_by"] == "connected pieces"


def test_a_package_keeps_the_maps_its_atlas_was_baked_from_outside_what_engines_load(tmp_path):
    from antigravity3d.dags import edit_asset as E
    from antigravity3d.kernel.validator import validate_package

    sc = scene(tmp_path)
    _, p0 = export(tmp_path / "a", sc)
    pkg = tmp_path / "a" / "pkg"
    ta = p0.data["texture_atlas"]
    src = ta["members"]["wood"]["sources"]["albedo"]
    assert src["file"] == "sources/wood/wood.png" and (pkg / src["file"]).read_bytes() == (tmp_path / "wood.png").read_bytes()
    assert ta["sources_bytes"] == (pkg / src["file"]).stat().st_size and ta["members"]["iron"]["sources"] == {}
    assert not any(f.startswith("sources/") for f in p0.data["files"]) and not any(f.startswith("sources/") for f in p0.data["file_hashes"])
    assert b"sources/" not in (pkg / f"{ASSET}.glb").read_bytes() and b"sources/" not in (pkg / f"{ASSET}.usd").read_bytes()
    assert not [i for i in validate_package(pkg) if i.severity == "critical"]
    # the package alone re-bakes from it; without it the edit is refused, saying what is missing and what to do
    sh = E.load_shipped(pkg)
    scene_src, how = E.source_materials(sh, tmp_path / "in")
    assert "sources/" in how and Path(next(m for m in scene_src.materials if m.id == "wood").albedo_uri).read_bytes() == (tmp_path / "wood.png").read_bytes()
    shutil.rmtree(pkg / "sources")
    with pytest.raises(E.EditError, match=r"one texture atlas.*wood.*sources/.*--out folder"):
        E.source_materials(E.load_shipped(pkg), tmp_path / "in2")
