"""One rule for shipped normals at every site that computes them (round seven): a round part's sides ship smooth in the
prop package, in the hidden-face check that decides which contact faces may be dropped, in the level-of-detail pop
check, and in the environment kit's pieces; an imported mesh (batch sanitize) has no run, so nothing changes there."""

from __future__ import annotations

import math

import numpy as np

from antigravity3d.ir import MaterialIR, NodeIR, SceneIR, Transform
from antigravity3d.kernel import compile_ir
from antigravity3d.kernel.coplanar import opaque_materials
from antigravity3d.kernel.fast_ops import hidden_contact_faces, run_export_pipeline
from antigravity3d.kernel.surface_frame import round_dirs, round_side_faces
from antigravity3d.kernel.types import Run
from antigravity3d.kernel.usd_stage import StageManager
from antigravity3d.state import EngineBudget

WOOD = MaterialIR(id="w", surface="oak wood", base_color=(0.3, 0.2, 0.1))


def _post_scene() -> SceneIR:
    """An 8-segment post on a plinth: round by is_round (7 or more segments), the default path's ready shapes."""
    return SceneIR(asset_name="T", nodes=[
        NodeIR(id="plinth", kind="box", params={"size": [0.3, 0.3, 0.1]}, transform=Transform(translate=(0, 0, 0.05)), material_id="w"),
        NodeIR(id="post", kind="cylinder", params={"radius": 0.04, "height": 0.6, "segments": 8},
               transform=Transform(translate=(0, 0, 0.4)), material_id="w"),
    ], materials=[WOOD])


def _side_normals(n: np.ndarray, up_axis: int = 2) -> int:
    side = n[np.abs(n[:, up_axis]) < 0.5]
    return len(np.unique(np.round(side, 3), axis=0))


def test_the_hidden_face_check_compares_with_the_normals_that_ship():
    """``fast_ops._crease_safe`` keeps a hidden contact face whose removal would change a shipped corner normal. An
    8-sided rod lying on a board with one facet flat on it: shipped faceted (45 degrees, a hard edge) the facet can go;
    shipped round, the facets beside it smooth with it, so it must stay."""
    r, n = 0.02, 8
    ang = (np.arange(n) + 0.5) * 2 * math.pi / n - math.pi / 2          # one facet straight down
    ring = np.stack([np.zeros(n), r * np.cos(ang), r * np.sin(ang)], 1)
    ring[:, 2] -= ring[:, 2].min()                                       # that facet on z = 0
    pts = np.vstack([ring + [-0.1, 0, 0], ring + [0.1, 0, 0]])
    tris = []
    for i in range(n):
        j = (i + 1) % n
        tris += [(i, j, n + i), (j, n + j, n + i)]
    tris += [(0, k + 1, k) for k in range(1, n - 1)] + [(n, n + k, n + k + 1) for k in range(1, n - 1)]
    tris = np.array(tris)
    bp, bt = compile_ir.primitive_mesh(NodeIR(id="b", kind="box", params={"size": [0.4, 0.2, 0.02]}))
    bp = np.asarray(bp, np.float64) - [0, 0, 0.01]
    axis = np.array([[-0.1, 0, ring[:, 2].mean()], [0.1, 0, ring[:, 2].mean()]])
    run = Run(axis, np.full(2, r), True)
    parts = [("rod", pts, tris, "w"), ("board", bp, np.asarray(bt), "w")]
    faceted = hidden_contact_faces(parts, {"w"})[0]
    rnd = hidden_contact_faces(parts, {"w"}, [run, None])[0]
    side = round_side_faces(pts, tris, run)
    assert (faceted & side).any()          # the bottom facet is hidden by the board and may go when shipped faceted
    assert not (rnd & side).any()          # shipped round, dropping it would change its neighbours' normals


def test_levels_of_detail_are_judged_with_their_shipped_normals(tmp_path):
    from antigravity3d.tactics.package import merged_lods

    scene = _post_scene()
    stage = StageManager.create(tmp_path / "T.usda", "T")
    stage.apply_ir(scene)
    meshes = stage.extract_meshes()
    bundle = run_export_pipeline(meshes, EngineBudget("godot_4"), "T", opaque_materials=opaque_materials(scene))
    plain = merged_lods(bundle)
    runs = {m.node_id: m.run for m in meshes if m.run is not None}
    assert set(runs) == {"post"} and runs["post"].round
    judged = merged_lods(bundle, runs)
    assert not hasattr(plain[0], "normals")
    expect = compile_ir.crease_normals(judged[0].points, judged[0].triangles)
    assert _side_normals(judged[0].normals) > _side_normals(expect) >= 8   # the post's sides smooth at every level


def test_a_kit_piece_ships_its_round_parts_round(tmp_path):
    """The environment kit writes its pieces with ``tactics.package.write_package_usd``: a kit's 8-segment pillar
    ships round like a prop's."""
    from pxr import Usd, UsdGeom

    from antigravity3d.tactics.package import write_package_usd

    scene = _post_scene()
    stage = StageManager.create(tmp_path / "T.usda", "T")
    stage.apply_ir(scene)
    meshes = stage.extract_meshes()
    bundle = run_export_pipeline(meshes, EngineBudget("godot_4"), "T")
    post = next(u for u in bundle.uvs if u.node_id == "post")
    run = next(m.run for m in meshes if m.node_id == "post")
    dirs = {"post": round_dirs(post.points, post.triangles, run)}
    path = write_package_usd(tmp_path / "pkg" / "T.usda", "T", bundle.uvs, {}, [], [], "godot_4", round_dirs=dirs)
    st = Usd.Stage.Open(str(path))
    mesh = UsdGeom.Mesh(st.GetPrimAtPath("/T/post"))
    n = np.asarray(mesh.GetNormalsAttr().Get(), np.float64)
    # Godot is Y-up natively: side normals have a small y; faceted, 8 directions; round, one per vertex
    assert _side_normals(n, 1) > 8
    assert not UsdGeom.Mesh(st.GetPrimAtPath("/T/plinth")).GetNormalsAttr().HasAuthoredValue()   # as before


def test_an_imported_mesh_has_no_run(tmp_path):
    """Batch sanitize repairs meshes read from files (``dags.batch_sanitize.load_meshes``): a file carries no run, so
    its shipped normals are the crease normals they always were."""
    import trimesh

    from antigravity3d.dags.batch_sanitize import load_meshes

    f = tmp_path / "rod.obj"
    trimesh.creation.cylinder(radius=0.02, height=0.4, sections=8).export(f)
    assert all(m.run is None for m in load_meshes(f))
