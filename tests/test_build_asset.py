"""The build API a script writes an asset with (``from agy3d import Asset``): every call and keyword of the reference
page (src/antigravity3d/build/plugin_script_mode/SKILL.md), its refusals, the hand-off folder, and the page's own
example through the kernel's stage and Tier-1 checks."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import get_args

import numpy as np
import pytest

from agy3d import Asset, BuildError, Part
from antigravity3d import ir_schema
from antigravity3d.build import asset as asset_mod
from antigravity3d.build import handoff, shapes

SRC = Path(__file__).resolve().parents[1] / "src"
SKILL = SRC / "antigravity3d" / "build" / "plugin_script_mode" / "SKILL.md"


def example_code() -> str:
    return re.search(r"## A complete script\n\n```python\n(.*?)```", SKILL.read_text(encoding="utf-8"), re.DOTALL).group(1)


def run_script(code: str, folder: Path, out: Path) -> subprocess.CompletedProcess:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "build.py").write_text(code, encoding="utf-8")
    env = {**os.environ, "AGY3D_BUILD_OUT": str(out), "PYTHONPATH": str(SRC)}
    return subprocess.run([sys.executable, "build.py"], cwd=folder, env=env, capture_output=True, text=True, timeout=120, check=False)


def stage_meshes(build: handoff.Build, tmp: Path):
    from antigravity3d.kernel.usd_stage import StageManager

    sm = StageManager.create(tmp / "stage.usda", build.name)
    sm.apply_ir(build.scene())
    return {m.node_id: m for m in sm.extract_meshes()}


def saved(a: Asset, out: Path, monkeypatch) -> handoff.Build:
    monkeypatch.setenv("AGY3D_BUILD_OUT", str(out))
    a.save()
    return handoff.read(out)


# --- the reference page's example ------------------------------------------------------------------------------


def test_the_reference_example_builds_and_passes_the_kernel_checks(tmp_path):
    from antigravity3d.kernel.fast_ops import tier1_qa
    from antigravity3d.state import EngineBudget

    r = run_script(example_code(), tmp_path / "ws", tmp_path / "out")
    assert r.returncode == 0, r.stderr
    build = handoff.read(tmp_path / "out")
    assert r.stdout == (f"agy3d: wrote {build.stats['parts']} parts, 3 materials, {build.stats['triangles']:,} triangles "
                        f"to {tmp_path / 'out'}\n")
    meshes = stage_meshes(build, tmp_path)
    assert set(meshes) == {n["id"] for n in build.nodes}
    tris = sum(len(m.triangles) for m in meshes.values())
    assert tris == build.stats["triangles"] <= build.max_tris
    report = tier1_qa(list(meshes.values()), EngineBudget(build.engine, max_tris=build.max_tris))
    issues = [(i.code, i.severity, i.node_id) for i in report.issues]
    assert not [i for i in issues if i[1] == "critical"], issues
    assert not [i for i in issues if i[0] in ("floating_part", "floating_group")], issues
    assert build.notes == []
    assert {"tray", "wheel", "axle", "rail", "rail__mx", "grip", "grip__mx", "leg", "leg__mx", "brace"} == set(meshes)


def test_the_same_script_writes_the_same_bytes_wherever_it_runs(tmp_path):
    outs = []
    for k in range(2):
        r = run_script(example_code(), tmp_path / f"ws{k}", tmp_path / f"out{k}")
        assert r.returncode == 0, r.stderr
        outs.append(tmp_path / f"out{k}")
    files = [sorted(p.relative_to(o).as_posix() for p in o.rglob("*") if p.is_file()) for o in outs]
    assert files[0] == files[1] and len(files[0]) == 1 + 5  # design.json, the tray, two rails and two legs
    for rel in files[0]:
        assert (outs[0] / rel).read_bytes() == (outs[1] / rel).read_bytes(), rel
    text = (outs[0] / "design.json").read_text()
    assert str(tmp_path) not in text and text.endswith("}\n")
    doc = json.loads(text)
    assert text == json.dumps(doc, sort_keys=True, indent=1) + "\n"


# --- ready shapes: the kernel's nodes, measured with the kernel's mesh ------------------------------------------


def all_kinds(a: Asset) -> list[Part]:
    m = a.material("m", "#808080")
    return [
        a.box("b", size=(0.4, 0.3, 0.2), bevel=0.01, bevel_segments=2, inset_faces=["+x"], inset_margin=0.03,
              inset_depth=0.005, material=m, bottom=(0, 0, 0)),
        a.cylinder("c", radius=0.1, height=0.3, segments=12, bevel=0.01, axis="x", at=(0.5, 0, 0.5), material=m),
        a.cone("k", radius=0.1, height=0.2, axis="y", top=(0, 0.5, 1.0), material=m),
        a.sphere("s", radius=0.1, segments=10, rings=6, at=(-0.5, 0, 0.3), material=m),
        a.capsule("p", radius=0.05, height=0.2, axis=(1, 1, 1), at=(0, -0.5, 0.4), material=m),
        a.lathe("l", profile=[(0.0, 0.3), (0.1, 0.25), (0.12, 0.0)], rot=(10, 20, 30), at=(0.5, 0.5, 0.2), material=m),
        a.extrude("e", outline=[(0, 0), (0.2, 0), (0.2, 0.05), (0.05, 0.05), (0.05, 0.2), (0, 0.2)], depth=0.04,
                  axis=(0, -1, 0.2), bottom=(-0.5, -0.5, 0), material=m),
        a.cylinder("down", radius=0.05, height=0.1, axis=(0, 0, -1), at=(0.8, 0.8, 0.1), material=m),
    ]


def test_every_ready_shape_is_where_its_part_says_it_is_in_the_kernels_stage(tmp_path, monkeypatch):
    """The bounds a part reports are the bounds of the kernel's compiled mesh (rotation convention included: axis
    vectors, rot=, bottom= and top= all go through the stage and are measured there)."""
    a = Asset("shapes", engine="unreal_5")
    parts = all_kinds(a)
    meshes = stage_meshes(saved(a, tmp_path / "out", monkeypatch), tmp_path)
    for p in parts:
        pts = meshes[p.id].points
        assert np.allclose(p.min, pts.min(0), atol=2e-6) and np.allclose(p.max, pts.max(0), atol=2e-6), p
        assert len(meshes[p.id].triangles) == a._tris[p.id]
    b, k, e = parts[0], parts[2], parts[6]
    assert b.bottom == 0.0 and b.center[:2] == (0.0, 0.0)
    assert k.top == 1.0 and np.allclose(k.center[:2], (0, 0.5))
    assert e.bottom == 0.0 and np.allclose(e.center[:2], (-0.5, -0.5))
    c = parts[1]
    assert np.allclose(c.size, (0.3, 0.2, 0.2), atol=1e-6) and c.center == (0.5, 0.0, 0.5), "axis='x': the height along x"
    assert np.allclose(parts[4].size[0], parts[4].size[1]), "axis=(1, 1, 1): x and y are alike"
    assert parts[7].center == (0.8, 0.8, 0.1)


def test_a_ready_shape_is_the_wire_node_the_kernel_reads_with_defaults_left_out(tmp_path):
    a = Asset("w", engine="godot_4")
    m = a.material("steel", "#8a8f96", surface="brushed steel", metallic=1, roughness=0.3)
    a.cylinder("c", radius=0.1, height=0.2, material=m, axis="x")
    a.lathe("l", profile=[(0.0, 0.1), (0.05, 0.0)], at=(0, 0, 0.5))
    nodes = a.design()["nodes"]
    assert nodes[0] == {"id": "c", "kind": "cylinder", "params": {"radius": 0.1, "height": 0.2}, "material_id": "steel",
                        "transform": {"translate": [0.0, 0.0, 0.0], "rotate_euler_deg": [0.0, 90.0, 0.0]}}
    assert nodes[1]["params"]["segments"] == 24, "the page's lathe default (24) differs from the kernel's (16): written"
    assert a.design()["materials"] == [{"id": "steel", "base_color": "#8a8f96", "surface": "brushed steel",
                                        "metallic": 1, "roughness": 0.3}]
    scene = ir_schema.parse_tool_scene("w", nodes, a.design()["materials"])
    assert scene.materials[0].base_color == pytest.approx(ir_schema.linear_colour("#8a8f96"))


def test_cylinder_bevel_segments_rounds_the_rim_at_the_kernels_cost():
    a = Asset("c", engine="godot_4")
    a.cylinder("chamfer", radius=0.1, height=0.1, bevel=0.01)
    a.cylinder("round", radius=0.1, height=0.1, bevel=0.01, bevel_segments=2, at=(0.5, 0, 0))
    nodes = {n["id"]: n for n in a.design()["nodes"]}
    assert "bevel_segments" not in nodes["chamfer"]["params"] and nodes["round"]["params"]["bevel_segments"] == 2
    assert a._tris["round"] == 24 * (4 * 2 + 4) > a._tris["chamfer"] == 24 * (4 + 4)  # compile_ir.triangle_count


def test_axis_points_the_shapes_own_z_by_the_shortest_turn():
    for axis, want in (("x", (1, 0, 0)), ("y", (0, 1, 0)), ("z", (0, 0, 1)), ((0, 0, -2), (0, 0, -1)),
                       ((1, 2, 3), np.array([1, 2, 3]) / np.sqrt(14))):
        _, m = asset_mod._orientation("t", axis, None)
        assert np.allclose(m @ [0, 0, 1], want), axis


@pytest.mark.parametrize("axis, x_to, depth", [
    ("x", (0, 1, 0), (1, 0, 0)), ("y", (1, 0, 0), (0, 1, 0)), ("z", (1, 0, 0), (0, 0, 1)),
    ((1, 1, 0), (np.sqrt(0.5), -np.sqrt(0.5), 0), (np.sqrt(0.5), np.sqrt(0.5), 0)),
])
def test_an_upright_outline_stays_upright_whatever_the_extrusion_axis(tmp_path, monkeypatch, axis, x_to, depth):
    """An L drawn upright (its tall arm along the outline's +y) keeps its arm pointing up: the outline's y is world +z
    for a horizontal axis, its x world +x for "y", +y for "x", and for "z" nothing turns. Measured on the kernel's
    stage: the L's inner corner (0.05, 0.2) is where the rule puts it."""
    a = Asset("l", engine="godot_4")
    a.extrude("ell", outline=[(0, 0), (0.2, 0), (0.2, 0.05), (0.05, 0.05), (0.05, 0.2), (0, 0.2)], depth=0.04, axis=axis)
    pts = stage_meshes(saved(a, tmp_path / "out", monkeypatch), tmp_path)["ell"].points
    up = (0, 0, 1) if axis != "z" else (0, 1, 0)
    for side in (-0.02, 0.02):
        want = 0.05 * np.array(x_to) + 0.2 * np.array(up) + side * np.array(depth)
        assert np.linalg.norm(pts - want, axis=1).min() < 1e-6, (axis, want)


def test_mirror_on_a_ready_shape_makes_the_kernels_copy():
    a = Asset("m", engine="godot_4")
    p = a.extrude("bracket", outline=[(0.1, 0), (0.3, 0), (0.3, 0.1)], depth=0.02, axis="y", at=(0.2, 0.1, 0.3),
                  mirror="x")
    nodes = a.design()["nodes"]
    assert [n["id"] for n in nodes] == ["bracket", "bracket__mx"]
    kernel = ir_schema.parse_tool_scene("m", [{**nodes[0], "mirror": "x"}], [])
    assert kernel.nodes[1].model_dump() == ir_schema.parse_tool_scene("m", nodes, []).nodes[1].model_dump()
    assert isinstance(p.mirrored, Part) and p.mirrored.id == "bracket__mx"
    assert np.allclose(p.mirrored.min, (-p.max[0], p.min[1], p.min[2]))
    assert a.box("solo", size=(0.1, 0.1, 0.1)).mirrored is None
    with pytest.raises(BuildError) as e:
        a.box("wide", size=(1, 0.1, 0.1), mirror="x")
    assert str(e.value).startswith("box wide: mirror x needs the part on one side of the asset's x = 0 plane, but it spans x -0.500 to 0.500")
    assert "wide" not in a._parts, "a refused part leaves nothing behind"


# --- free-form parts --------------------------------------------------------------------------------------------


def test_free_form_parts_become_mesh_ref_files_where_their_parts_say(tmp_path, monkeypatch):
    a = Asset("free", engine="unity_urp", max_tris=4000)
    m = a.material("m", "#a0a0a0")
    rail = a.tube("rail", path=[(0.1, 0, 0), (0.1, 0.5, 0.2), (0.3, 1, 0.2)], radius=0.02, sides=6, material=m,
                  mirror="x", at=(0.05, 0, 0.1))
    a.loft("seat", [[(-0.2, -0.2, 0), (-0.2, 0.2, 0), (0.2, 0.2, 0), (0.2, -0.2, 0)], [(0, 0, 0.1)]], bottom=(0, 0, 0.5),
           material=m)
    a.mesh("plate", [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (0.5, 0.5, 0.3)],
           [[0, 3, 2, 1], [0, 1, 4], [1, 2, 4], [2, 3, 4], [3, 0, 4]], axis="x", top=(0, 0, 0.4), material=m)
    build = saved(a, tmp_path / "out", monkeypatch)
    assert sorted(build.part_files()) == ["plate", "rail", "rail__mx", "seat"]
    for nid, f in build.part_files().items():
        assert f.is_absolute() and f.parent == tmp_path.resolve() / "out" / "parts"
        assert re.fullmatch(rf"{nid}-[0-9a-f]{{12}}\.ply", f.name)
    raw = json.loads((tmp_path / "out" / "design.json").read_text())
    assert all(n["params"]["path"].startswith("parts/") and n["transform"] == {"translate": [0.0, 0.0, 0.0]}
               for n in raw["nodes"])
    meshes = stage_meshes(build, tmp_path)
    for p in (rail, rail.mirrored, a._parts["seat"], a._parts["plate"]):
        pts = meshes[p.id].points
        assert np.allclose(p.min, pts.min(0), atol=1e-6) and np.allclose(p.max, pts.max(0), atol=1e-6), p.id
    assert build.stats["triangles"] == sum(len(m.triangles) for m in meshes.values())
    assert np.allclose(rail.along(0), (0.15, 0, 0.1)) and np.allclose(rail.along(1), (0.35, 1, 0.3))
    assert np.allclose(rail.mirrored.along(1), (-0.35, 1, 0.3))
    assert a._parts["seat"].bottom == 0.5 and a._parts["plate"].top == 0.4
    from antigravity3d.build.shapes import signed_volume
    for nid in ("rail", "rail__mx", "seat", "plate"):
        assert signed_volume(meshes[nid].points, meshes[nid].triangles) > 0, f"{nid} faces outward"


def test_mesh_repairs_are_one_note_each_and_a_trimesh_works():
    import trimesh

    a = Asset("r", engine="godot_4")
    v = [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1), (0, 0, 0)]  # point 4 repeats point 0
    a.mesh("tet", v, [(0, 1, 2), (0, 1, 3), (4, 2, 3), (1, 2, 3), (0, 0, 1)])  # two faces wound in, one degenerate
    assert a._notes == ["mesh tet: 1 duplicate points merged", "mesh tet: 1 degenerate faces dropped",
                        "mesh tet: 6 faces flipped to a consistent outward winding"]  # the sanitizer's own count
    box = trimesh.creation.box((0.2, 0.2, 0.2))
    p = a.mesh("cube", box, bottom=(0, 0, 0))
    assert p.size == (0.2, 0.2, 0.2) and a._tris["cube"] == 12


def test_load_reads_a_file_relative_to_the_script_and_rewrites_it(tmp_path, monkeypatch):
    t = shapes.tube("x", [(0, 0, 0), (0, 0, 0.3)], 0.05, 6, False, False)
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "rod.ply").write_bytes(shapes.ply_bytes(t.points, t.tris))
    monkeypatch.chdir(tmp_path)
    a = Asset("l", engine="godot_4")
    p = a.load("rod", "in/rod.ply", at=(1, 0, 0))
    assert p.min[2] == 0.0 and p.max[2] == 0.3 and p.center[0] == 1.0
    build = saved(a, tmp_path / "out", monkeypatch)
    assert "in/rod.ply" not in (tmp_path / "out" / "design.json").read_text()
    assert build.part_files()["rod"].read_bytes() != (tmp_path / "in" / "rod.ply").read_bytes()  # moved by at=
    with pytest.raises(BuildError, match=r"^load gone: no file 'in/gone.ply' \(paths are relative to the script's folder\)"):
        a.load("gone", "in/gone.ply")


# --- refusals: each names the part and says what was expected ---------------------------------------------------


@pytest.mark.parametrize("call, message", [
    (lambda a: a.box("b", size=(1, 1, 1), at=(0, 0, 0), bottom=(0, 0, 0)), "box b: give one position, at=, bottom= or top=, not at and bottom"),
    (lambda a: a.box("b", size=(1, 1, 1), axis="x", rot=(0, 0, 0)), "box b: give one orientation, axis= or rot=, not both"),
    (lambda a: a.box("b", size=(1, 1, 1), axis=(0, 0, 0)), "box b: axis is a zero vector"),
    (lambda a: a.box("b", size=(1, 1, 1), axis="w"), 'box b: axis must be "x", "y", "z" or a vector'),
    (lambda a: a.box("b", size=(1, 1, 1), at=(0, 0)), "box b: at must be three numbers (x, y, z), got [0, 0]"),
    (lambda a: a.box("b", size=(1, 1, 1), mirror="q"), 'box b: mirror must be "x", "y" or "z"'),
    (lambda a: a.box("b", size=(1, 1, 1), material="paint"), "box b: material 'paint' is not defined; a.material(...) first (defined: m)"),
    # the wire model's checks, worded with the script's keywords instead of the scene format's params paths
    (lambda a: a.box("b", size=(1, -1, 1)), "box b: size y must be greater than 0, got -1"),
    (lambda a: a.box("b", size=(1, 1)), "box b: size needs three numbers (x, y, z), got (1, 1)"),
    (lambda a: a.cylinder("c", radius=0, height=1), "cylinder c: radius must be greater than 0, got 0"),
    (lambda a: a.cylinder("c", radius=1, height=1, bevel=0.1, bevel_segments=7), "cylinder c: bevel_segments must be less than or equal to 4, got 7"),
    (lambda a: a.box("b", size=(1, 1, 1), inset_faces=["up"]), "box b: inset_faces item 0 must be '+x', '-x', '+y', '-y', '+z' or '-z', got 'up'"),
    (lambda a: a.lathe("l", profile=[(0, 1)]), "lathe l: profile needs at least 2 points, got 1"),
    (lambda a: a.extrude("x", outline=[(0, 0), (1, 0), (1, 1, 1)], depth=1), "extrude x: outline point 2 needs two numbers (x, y), got (1, 1, 1)"),
    (lambda a: a.cylinder("c", radius=0.1, height=0.2, bevel=0.2), "cylinder c: bevel 0.2 must be below the radius (0.1) and half the height (0.1)"),
    (lambda a: a.box("2b", size=(1, 1, 1)), "box '2b': an id is letters, digits and underscores"),
    (lambda a: (a.box("b", size=(1, 1, 1)), a.sphere("b", radius=1)), "sphere b: the id 'b' is already used by another part"),
    (lambda a: a.box("b", size=(1, 1, 1)).along(0.5), "box b: along() is for tube parts"),
    (lambda a: a.tube("t", [(0, 0, 0), (0, 0, 1)], 0.1).along(1.5), "tube t: along(t) takes t from 0"),
    (lambda a: a.mesh("e", [(0, 0, 0)], []), "mesh e: faces is empty"),
    (lambda a: a.mesh("e", [(0, 0, 0), (1, 0, 0), (0, 1, 0)], [(0, 1, 3)]), "mesh e: face 0 uses vertex 3, but there are 3 vertices"),
    (lambda a: a.mesh("e", [(0, 0, 0), (1, 0, 0), (2, 0, 0)], [(0, 1, 2)]), "mesh e: every face is degenerate"),
    (lambda a: a.tube("t", [(1, 0, 0), (-1, 0, 1)], 0.1, mirror="x"), "tube t: mirror x needs the part on one side of the asset's x = 0 plane, but it spans x -1.043 to 1.043"),
    (lambda a: a.material("m2", "olive"), 'material m2: color must be "#rrggbb"'),
    (lambda a: a.material("m2", "#112233", roughness=2), "material m2.roughness: Input should be less than or equal to 1"),
    (lambda a: a.material("m", "#112233"), "material m: the id 'm' is already used by another material"),
])
def test_a_wrong_call_raises_a_build_error_that_teaches(call, message):
    a = Asset("e", engine="godot_4")
    a.material("m", "#808080")
    with pytest.raises(BuildError) as e:
        call(a)
    assert str(e.value).startswith(message), str(e.value)
    assert isinstance(e.value, ValueError)


def test_asset_arguments_are_checked_at_the_asset_line():
    for kwargs, message in (({"name": "my crate", "engine": "godot_4"}, "Asset: name must be letters"),
                            ({"name": "c", "engine": "godot"}, "Asset c: engine must be one of unreal_5, unity_urp, godot_4, roblox_r15"),
                            ({"name": "c", "engine": "godot_4", "max_tris": 0}, "Asset c: max_tris must be a positive whole number"),
                            ({"name": "c", "engine": "godot_4", "art_direction": "gothic"}, "Asset c: unknown art direction 'gothic'; valid ids: ")):
        with pytest.raises(BuildError) as e:
            Asset(**kwargs)
        assert str(e.value).startswith(message)
    a = Asset("c", engine="godot_4", art_direction="low-poly")
    a.box("b", size=(1, 1, 1))
    assert a.design()["asset"] == {"name": "c", "engine": "godot_4", "max_tris": 15000, "brief": "", "art_direction": "low-poly"}
    with pytest.raises(TypeError):
        a.box("x", size=(1, 1, 1), colour="red")  # an unknown keyword: Python's own error


def test_engines_are_the_scene_formats_engines():
    from antigravity3d.state import TargetEngine

    assert asset_mod.ENGINES == get_args(TargetEngine)


# --- save and the hand-off reader -------------------------------------------------------------------------------


def test_save_replaces_the_previous_build_and_defaults_to_agy3d_build(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGY3D_BUILD_OUT", raising=False)
    a = Asset("crate", engine="godot_4")
    a.tube("t", [(0, 0, 0), (0, 0, 1)], 0.1)
    out = a.save()
    assert out == Path("agy3d_build/crate") and (out / "design.json").is_file()
    assert capsys.readouterr().out == "agy3d: wrote 1 parts, 0 materials, 36 triangles to agy3d_build/crate\n"
    (out / "keep.txt").write_text("not ours")
    b = Asset("crate", engine="godot_4")
    b.tube("t", [(0, 0, 0), (0, 0, 2)], 0.1)
    b.save()
    assert len(list((out / "parts").iterdir())) == 1 and (out / "keep.txt").exists()
    with pytest.raises(BuildError, match="no parts to save"):
        Asset("empty", engine="godot_4").save()


def test_a_rotated_part_clear_of_the_plane_is_mirrored():
    """The kernel's one-side rule measures the part's mesh, not its box's corners (review r5: a tilted cylinder 2 mm
    clear of x = 0 was refused as spanning x -0.015 to 0.205)."""
    a = Asset("r5", engine="godot_4")
    probe = a.cylinder("probe", radius=0.05, height=0.2, rot=(0, 45, 45), at=(0, 0, 0.3))
    bar = a.cylinder("bar", radius=0.05, height=0.2, rot=(0, 45, 45), at=(-probe.min[0] + 0.002, 0, 0.6), mirror="x")
    assert bar.min[0] == pytest.approx(0.002, abs=1e-6) and bar.mirrored.max[0] == pytest.approx(-0.002, abs=1e-6)


def test_a_mirror_copy_id_over_64_characters_is_refused_at_the_call():
    a = Asset("ids", engine="godot_4")
    long = "p" * 62
    for call in (lambda: a.tube(long, [(0.1, 0, 0), (0.2, 0, 0)], 0.01, mirror="x"),
                 lambda: a.box(long, size=(0.1, 0.1, 0.1), at=(0.2, 0, 0), mirror="x")):
        with pytest.raises(BuildError, match=rf"{long}__mx: the id made for a mirror/repeat copy is longer than 64 "
                                             "characters; shorten the original's id"):
            call()
    assert long not in a._parts
    a.mesh("m", [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)], [(0, 2, 1), (0, 1, 3), (0, 3, 2), (1.0, 2.0, 3.0)])
    with pytest.raises(BuildError, match=r"mesh n: face 0 must be a list of at least 3 vertex indices \(whole numbers\)"):
        a.mesh("n", [(0, 0, 0), (1, 0, 0), (0, 1, 0)], [(0, 1.5, 2)])


def test_the_reader_refuses_what_is_missing_or_malformed(tmp_path, monkeypatch):
    a = Asset("h", engine="godot_4")
    a.tube("t", [(0, 0, 0), (0, 0, 1)], 0.1)
    out = tmp_path / "out"
    build = saved(a, out, monkeypatch)
    assert build.name == "h" and build.scene().nodes[0].params["path"] == str(build.part_files()["t"])
    with pytest.raises(BuildError, match="no design.json"):
        handoff.read(tmp_path / "nowhere")
    doc = json.loads((out / "design.json").read_text())
    for change, message in ((lambda d: d.update(version=2), "version 2"),
                            (lambda d: d["asset"].pop("engine"), "asset.engine is missing"),
                            (lambda d: d["nodes"][0]["params"].update(path="../x.ply"), "leaves the folder"),
                            (lambda d: d["nodes"][0]["params"].update(path="parts/x.ply"), "part file parts/x.ply is missing")):
        bad = json.loads(json.dumps(doc))
        change(bad)
        (out / "design.json").write_text(json.dumps(bad))
        with pytest.raises(BuildError, match=re.escape(message)):
            handoff.read(out)


def test_import_agy3d_pulls_in_no_model_client_image_stack_or_mcp():
    code = ("import sys, agy3d; bad = sorted(m for m in sys.modules if m.split('.')[0] in "
            "('trimesh', 'google', 'PIL', 'mcp', 'httpx', 'yaml', 'scipy')); print(bad)")
    r = subprocess.run([sys.executable, "-c", code], env={**os.environ, "PYTHONPATH": str(SRC)}, capture_output=True,
                       text=True, timeout=120, check=False)
    assert r.returncode == 0 and r.stdout.strip() == "[]", r.stdout + r.stderr
