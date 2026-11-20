"""A source mesh can have valid UVs that differ from the atlas used by the engine package."""

import numpy as np
import pytest
import trimesh
from PIL import Image
from pxr import Usd, UsdGeom

from antigravity3d.harness import session, tools
from antigravity3d.ir import MaterialIR, NodeIR, SceneIR
from antigravity3d.kernel.usd_stage import StageManager
from antigravity3d.state import VisualVerdict
from antigravity3d.tactics import pyramid
from antigravity3d.tactics.authoring import CallableAuthor
from antigravity3d.tactics.governor import Governor, plan_for
from test_harness_tools import make_session, simulate_engine_import
from test_pyramid import BUDGET, Script, maps, setup, up


def textured_scene(root, asset, width=1.0):
    # Use the same reference URI across replacements, as a re-import would.
    mesh = trimesh.creation.box(extents=[width, 1, 0.7])
    mesh.apply_translation([0, .5, 0])
    uv = mesh.vertices[:, [0, 1]] * .08 + .25  # samples only one patch of the source material
    mesh.visual = trimesh.visual.TextureVisuals(uv=uv)
    path = root / "import.glb"
    mesh.export(path)
    y, x = np.mgrid[:32, :32]
    tex = root / "pattern.png"
    Image.fromarray(np.stack([x * 8, y * 8, (x ^ y) * 8], axis=-1).astype(np.uint8)).save(tex)
    return SceneIR(asset_name=asset, nodes=[NodeIR(id="scan", kind="mesh_ref", params={"path": str(path)},
                                                 material_id="paint")],
                   materials=[MaterialIR(id="paint", albedo_uri=str(tex))])


def uv_corners(stage):
    mesh, = [UsdGeom.Mesh(p) for p in stage.Traverse() if p.GetName() == "scan" and p.IsA(UsdGeom.Mesh)]
    st = UsdGeom.PrimvarsAPI(mesh).GetPrimvar("st")
    return np.asarray(st.ComputeFlattened())[np.asarray(mesh.GetFaceVertexIndicesAttr().Get())]


class SnapshotPool:
    """Capture the exact USD that crosses the DCC boundary. Return a labelled test frame."""
    fake = False

    def __init__(self, size=512):
        self.size = size
        self.samples = []

    async def call(self, op, *, path, engine, **_):
        assert op == "render_usd"
        self.samples.append(uv_corners(Usd.Stage.Open(path)))
        image = np.zeros((self.size, self.size, 4), dtype=np.float32)
        edge = self.size // 4
        image[edge:-edge, edge:-edge] = 1
        return {"engine": engine, "fake": False, "image": image}


def count_builds(monkeypatch, module):
    calls = []
    build = module.run_export_pipeline

    def counted(*args):
        calls.append(1)
        return build(*args)

    monkeypatch.setattr(module, "run_export_pipeline", counted)
    return calls


def assert_layout(source, samples, usd):
    expected = uv_corners(Usd.Stage.Open(str(usd)))
    assert not np.allclose(source, expected)
    assert len(samples) >= 2
    for seen in samples:
        np.testing.assert_array_equal(seen, expected)


async def test_staged_lookdev_and_export_share_one_imported_mesh_atlas(tmp_path, monkeypatch):
    simulate_engine_import(monkeypatch)
    pool = SnapshotPool(16)
    env = make_session(tmp_path, pool=pool)
    scene = textured_scene(tmp_path, "Crate")
    assert (await tools.apply_scene_ir(scene.model_dump_json(), env.ctx)).startswith("OK")
    sm = env.sess().stage
    assert sm.has_uvs()  # the original source already supplied a full st primvar
    source = uv_corners(sm.stage)
    calls = count_builds(monkeypatch, tools)
    lookdev = {fn.__name__: fn for fn in tools.STAGE_TOOLS["LOOKDEV"]}
    export = {fn.__name__: fn for fn in tools.STAGE_TOOLS["EXPORT"]}
    session.set_toolset(env.reg, env.sid, "LOOKDEV")
    env.state.active_stage = "LOOKDEV"
    for _ in range(2):
        assert (await lookdev["render_lookdev"](env.ctx)).startswith("LOOKDEV")
    preview = env.sess().latest_preview()
    env.sess().set_tier15(3.8, VisualVerdict(env.state.revision, env.state.budget.target_engine, "lookdev",
                                             "eevee", "decisions", "approve", preview.texture_hash), preview)
    session.set_toolset(env.reg, env.sid, "EXPORT")
    env.state.active_stage = "EXPORT"
    assert (await export["generate_lods_and_colliders"](env.ctx)).startswith("LODS")
    await export["run_usd_validator"](env.ctx)
    assert (await export["export_engine_package"](env.ctx, "atlas")).startswith("EXPORTED")
    assert_layout(source, pool.samples, env.sess().last_export / "Crate.usd")
    assert len(calls) == 1


async def test_refinement_lookdev_fixer_and_verification_reuse_the_same_atlas(tmp_path, monkeypatch):
    scene = textured_scene(tmp_path, "Prop")
    sm, plane = setup(tmp_path, Script(lookdev=lambda n: {"defect_cg_plastic": .9} if n == 1 else {}), scene)
    source = uv_corners(sm.stage)
    calls = count_builds(monkeypatch, pyramid)
    pool = SnapshotPool()
    res = await pyramid.refine(sm, plane=plane, author=CallableAuthor(lambda *a, **k: []),
                               governor=Governor(plan_for(2)), budget=BUDGET, pool=pool, uploader=up,
                               textures={"paint": maps()}, workdir=tmp_path / "run")
    assert_layout(source, pool.samples, res.artifacts["usd"])
    assert len(calls) == 1


def test_cached_atlas_rekeys_after_same_path_mesh_replacement(tmp_path):
    from antigravity3d.kernel.cache import BuildCache, LocalBlobStore
    from antigravity3d.tactics.cached import cached_export_pipeline

    cache = BuildCache(LocalBlobStore(tmp_path / "cache"))
    for width in [1, 2]:
        sm = StageManager.create(tmp_path / f"work_{width}.usda", "Prop")
        scene = textured_scene(tmp_path, "Prop", width)
        sm.apply_ir(scene)
        meshes = sm.extract_meshes()
        bundle, hit = cached_export_pipeline(cache, scene, meshes, BUDGET)
        assert not hit
        assert np.ptp(bundle.uvs[0].points[:, 0]) == pytest.approx(width)
        _, hit = cached_export_pipeline(cache, scene, meshes, BUDGET)
        assert hit
