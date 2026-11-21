import asyncio
import glob
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from antigravity3d.adapters.blender_5_3.pool import BlenderOpError, BlenderPool, WorkerError, find_blender

DAEMON = Path(__file__).parents[1] / "src/antigravity3d/adapters/blender_5_3/daemon.py"


def alive(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


async def wait_dead(pid: int, t: float = 5.0) -> bool:
    end = time.monotonic() + t
    while time.monotonic() < end:
        if not alive(pid):
            return True
        await asyncio.sleep(0.05)
    return False


async def test_ping_render_shm_and_latency():
    async with BlenderPool(1, fake=True) as pool:
        t0 = time.perf_counter()
        n = 50
        for _ in range(n):
            assert (await pool.call("ping"))["pong"]
        print(f"\nfake ping round-trip: {(time.perf_counter() - t0) / n * 1000:.2f} ms")
        t0 = time.perf_counter()
        r = await pool.call("render", engine="cycles", width=1024, height=1024)
        print(f"fake 1024x1024x4 render round-trip: {(time.perf_counter() - t0) * 1000:.1f} ms")
        img = r["image"]
        assert isinstance(img, np.ndarray) and img.shape == (1024, 1024, 4) and img.dtype == np.float32
        assert img[0, 0, 3] == 1.0 and img[0, -1, 0] == pytest.approx(1.0)
        prefix = pool.prefix
        await pool.call("ping")  # next request makes the daemon unlink the previous segment
        assert glob.glob(f"/dev/shm/{prefix}*") == []
    assert glob.glob(f"/dev/shm/{prefix}*") == []


async def test_concurrency_12_calls_3_workers():
    delay = 0.4
    async with BlenderPool(3, fake=True) as pool:
        t0 = time.perf_counter()
        res = await asyncio.gather(*(pool.call("render", width=32, height=32, seed=i, delay=delay) for i in range(12)))
        wall = time.perf_counter() - t0
    assert len({r["pid"] for r in res}) == 3
    assert wall < 12 * delay * 0.6, wall  # serial would be 4.8s; ideal parallel 1.6s
    assert wall >= 4 * delay * 0.9


async def test_map_preserves_order():
    async with BlenderPool(3, fake=True) as pool:
        out = await pool.map("render", [{"width": 8, "height": 8, "seed": s} for s in range(7)])
        assert [int(round(o["image"][0, 0, 2] * 255)) for o in out] == list(range(7))
        errs = await pool.map("fail", [{"msg": "a"}, {"msg": "b"}], return_exceptions=True)
        assert all(isinstance(e, BlenderOpError) for e in errs)


async def test_remote_error_mapping():
    async with BlenderPool(1, fake=True) as pool:
        with pytest.raises(BlenderOpError) as ei:
            await pool.call("fail", msg="nope")
        assert ei.value.cls == "ValueError" and ei.value.msg == "nope" and "ValueError" in ei.value.trace_tail
        with pytest.raises(BlenderOpError) as ei:
            await pool.call("no_such_op")
        assert ei.value.cls == "LookupError"
        assert (await pool.call("ping"))["pong"]  # worker survives op errors


async def test_timeout_replaces_worker():
    async with BlenderPool(1, fake=True) as pool:
        (old,) = pool.worker_pids
        with pytest.raises(WorkerError) as ei:
            await pool.call("sleep", seconds=5, _timeout=0.3)
        assert ei.value.kind == "timeout"
        assert await wait_dead(old)
        assert (await pool.call("ping"))["pong"]
        assert pool.worker_pids[0] != old


async def test_sigkilled_worker_replaced():
    async with BlenderPool(3, fake=True) as pool:
        victim = pool.worker_pids[1]
        os.kill(victim, signal.SIGKILL)
        assert await wait_dead(victim)
        await asyncio.sleep(0.1)
        res = await asyncio.gather(*(pool.call("ping") for _ in range(9)), return_exceptions=True)
        assert all(not isinstance(r, Exception) for r in res), res
        assert victim not in pool.worker_pids


async def test_crash_midcall_raises_died_then_recovers():
    async with BlenderPool(1, fake=True) as pool:
        with pytest.raises(WorkerError) as ei:
            await pool.call("crash")
        assert ei.value.kind == "died"
        assert (await pool.call("ping"))["pong"]


async def test_clean_stop_leaves_nothing():
    pool = BlenderPool(3, fake=True)
    await pool.start()
    pids, tmp, prefix = [p for p in pool.worker_pids if p], pool.tmpdir, pool.prefix
    await pool.call("render", width=16, height=16)
    assert os.path.isdir(tmp) and all(alive(pid) for pid in pids)
    await pool.stop()
    assert not os.path.exists(tmp)
    assert glob.glob(f"/dev/shm/{prefix}*") == []
    for p in pids:
        assert await wait_dead(p)


async def test_daemon_refuses_unsafe_run_script():
    async with BlenderPool(1, fake=True) as pool:
        with pytest.raises(BlenderOpError) as ei:
            await pool.call("run_script", source="import os\nos.system('id')")
        assert ei.value.cls == "UnsafeScriptError" and "AG001" in ei.value.msg
        with pytest.raises(BlenderOpError):
            await pool.call("run_script", source="import bpy\nbpy.ops.wm.quit_blender()")
        ok = await pool.call("run_script", source="import bpy\nx = 1")
        assert ok["linted"] and not ok["executed"]
        # legacy flag rewrites deprecated symbols before linting
        assert (await pool.call("run_script", source="e = 'BLENDER_EEVEE_NEXT'", legacy=True))["linted"]
        with pytest.raises(BlenderOpError):
            await pool.call("run_script", source="e = 'BLENDER_EEVEE_NEXT'")


async def test_fake_ops_geonodes_and_bake(tmp_path):
    async with BlenderPool(1, fake=True) as pool:
        r = await pool.call("apply_geonodes", tree_json='{"name": "t", "inputs": [{"name": "w", "type": "NodeSocketFloat"}]}',
                            object_name="Cube", inputs={}, is_preview=True)
        assert r["inputs"] == ["Geometry", "w", "is_preview"]
        b = await pool.call("bake_maps", object_name="Cube", maps=["normal", "ao"], resolution=64, out_dir=str(tmp_path))
        assert all(os.path.exists(p) for p in b["paths"])
        v = await pool.call("version")
        assert v["fake"] and "render" in v["ops"]


async def test_startup_failure_is_typed(tmp_path):
    bad = tmp_path / "blender"
    bad.write_text("#!/bin/sh\nexit 1\n")
    bad.chmod(0o755)
    pool = BlenderPool(1, blender_bin=str(bad), startup_timeout=5)
    with pytest.raises(WorkerError) as ei:
        await pool.start()
    assert ei.value.kind == "startup"
    assert not pool.tmpdir


async def test_missing_blender_binary(monkeypatch):
    monkeypatch.delenv("BLENDER_BIN", raising=False)
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(FileNotFoundError):
        await BlenderPool(1).start()


def test_daemon_exits_when_parent_dies():
    code = (
        "import socket,subprocess,sys,time\n"
        "a,b=socket.socketpair()\n"
        f"p=subprocess.Popen([sys.executable,{str(DAEMON)!r},'--fake','--socket-fd',str(b.fileno())],"
        "pass_fds=(b.fileno(),))\n"
        "b.close()\n"
        "print(p.pid,flush=True)\ntime.sleep(60)\n"
    )
    parent = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        child = int(parent.stdout.readline())
        assert alive(child)
        parent.kill()
        parent.wait()
        end = time.monotonic() + 5
        while alive(child) and time.monotonic() < end:
            time.sleep(0.05)
        assert not alive(child), "daemon orphaned"
    finally:
        parent.kill()


def write_cube_snapshot(path, name, proportions):
    path.write_text(
        '#usda 1.0\n(\n    upAxis = "Z"\n    metersPerUnit = 1\n)\n'
        f'def Cube "{name}" {{\n'
        '    double3 xformOp:translate = (42, 7, 20)\n'
        f'    double3 xformOp:scale = {proportions}\n'
        '    uniform token[] xformOpOrder = ["xformOp:translate", "xformOp:scale"]\n}\n'
    )


@pytest.mark.blender
async def test_real_blender_concurrent_snapshots(tmp_path):
    if not find_blender():
        pytest.skip("no Blender binary ($BLENDER_BIN or PATH)")
    bar, cube = tmp_path / "bar.usda", tmp_path / "cube.usda"
    write_cube_snapshot(bar, "Bar", (4, 0.3, 0.3))
    write_cube_snapshot(cube, "Cube", (1, 1, 1))
    paths = [cube, bar, bar, cube]
    async with BlenderPool(2, startup_timeout=120) as pool:
        pids = pool.worker_pids
        assert (await pool.call("version"))["blender_version"][:2] >= [5, 3]
        results = await pool.map("render_usd", [
            {"path": str(path), "engine": "cycles", "width": 48, "height": 48, "samples": 2, "denoiser": "none"}
            for path in paths
        ])
        assert pool.worker_pids == pids
        for path, result in zip(paths, results, strict=True):
            assert not result["fake"] and result["objects"] == [path.stem.title()]
            image = result["image"]
            assert image.shape == (48, 48, 4) and np.isfinite(image).all()
            assert np.ptp(image[:, :, :3]) > 0.1
        assert not np.allclose(results[0]["image"], results[1]["image"])
        assert np.allclose(results[0]["image"], results[3]["image"], atol=1e-4)
        empty = tmp_path / "empty.usda"
        empty.write_text('#usda 1.0\ndef Xform "Empty" {}\n')
        with pytest.raises(BlenderOpError, match="no renderable geometry"):
            await pool.call("render_usd", path=str(empty), engine="cycles")


@pytest.mark.blender
@pytest.mark.parametrize("engine", ["eevee", "workbench", "cycles"])
async def test_real_blender_lookdev_engines(tmp_path, engine):
    if not find_blender():
        pytest.skip("no Blender binary ($BLENDER_BIN or PATH)")
    path = tmp_path / "cube.usda"
    write_cube_snapshot(path, "Cube", (1, 1, 1))
    async with BlenderPool(1, startup_timeout=120) as pool:
        r = await pool.call("render_usd", path=str(path), engine=engine, width=32, height=24, samples=2, transparent=True)
        assert r["image"].shape == (24, 32, 4) and r["engine"] == engine
        assert np.std(r["image"][:, :, :3]) > 0.01
        assert r["image"][:, :, 3].min() == 0 and r["image"][:, :, 3].max() > 0.95


PRIMITIVE_NAMES = ["kit_check", "mesh_stats", "qa_check", "texel_density"]


@pytest.mark.blender
async def test_reviewed_upstream_primitives(tmp_path):
    if not find_blender():
        pytest.skip("requires stock Blender")
    path = tmp_path / "cube.usda"
    write_cube_snapshot(path, "Cube", (1, 1, 1))
    # The vendored copy, as production loads it. CI also sets $AGY3D_BLENDER_SKILLS (for the byte comparison below);
    # an empty value means unset.
    async with BlenderPool(1, startup_timeout=120, extra_env={"AGY3D_BLENDER_SKILLS": ""}) as pool:
        version = await pool.call("version")
        assert version["primitives"] == PRIMITIVE_NAMES, version.get("primitives_error")
        assert version["primitives_source"]["kind"] == "vendored" and version["primitives_error"] is None
        report = await pool.call("inspect_usd", path=str(path), primitive="mesh_stats", kwargs={"budget": "web"})
        assert report["total_tris"] == 12 and report["budget"]["pass"] and report["agy_inspect"]["objects"] == ["Cube"]
        qa = await pool.call("inspect_usd", path=str(path), primitive="qa_check", kwargs={"profile": "game"})
        assert "Cube" in qa["objects"] and not qa["pass"]   # fixture has no material
        kit = await pool.call("inspect_usd", path=str(path), primitive="kit_check", kwargs={"grid": 0.5, "pivot": "center"})
        assert kit["pieces"]["Cube"]["dims_m"] == pytest.approx([2.0, 2.0, 2.0]) and not kit["fixed"]
        assert not any("pivot" in e for e in kit["pieces"]["Cube"]["errors"])
        corner = await pool.call("inspect_usd", path=str(path), primitive="kit_check", kwargs={})
        assert any("pivot is not at the corner" in e for e in corner["pieces"]["Cube"]["errors"])
        texel = await pool.call("inspect_usd", path=str(path), primitive="texel_density", kwargs={"res": 1024})
        assert texel["res"] == 1024 and "Cube" in texel["objects"]
        welded = await pool.call("inspect_usd", path=str(path), primitive="qa_check", kwargs={}, weld=True)
        weld = welded["agy_inspect"]["weld"]["objects"]["Cube"]
        assert weld["verts_after"] <= weld["verts_before"] and welded["objects"]["Cube"]["boundary_edges"] == 0
        for primitive, kwargs in (("kit_check", {"fix": True}), ("texel_density", {"set": 512.0}), ("qa_check", {"objects": ["Cube"]})):
            with pytest.raises(BlenderOpError, match="does not accept"):
                await pool.call("inspect_usd", path=str(path), primitive=primitive, kwargs=kwargs)
        with pytest.raises(BlenderOpError, match="KeyError"):
            await pool.call("inspect_usd", path=str(path), primitive="unlisted")
        rendered = await pool.call("render_usd", path=str(path), engine="cycles", width=24, height=24, samples=2)
        assert rendered["mesh_stats"]["total_tris"] == 12
    if os.environ.get("AGY3D_BLENDER_SKILLS"):  # the override loads the same reviewed files from the checkout
        async with BlenderPool(1, startup_timeout=120) as pool:
            version = await pool.call("version")
            assert version["primitives"] == PRIMITIVE_NAMES and version["primitives_source"]["kind"] == "checkout", version


@pytest.mark.blender
def test_vendored_blender_skills_match_the_pinned_checkout():
    """CI checks EL4CTEO/blender-skills out at the pinned commit: every vendored file must be byte-identical to it."""
    from antigravity3d.adapters.blender_5_3 import skills

    checkout = os.environ.get("AGY3D_BLENDER_SKILLS")
    if not checkout:
        pytest.skip("set AGY3D_BLENDER_SKILLS to a checkout of the pinned upstream commit")
    root = Path(checkout)
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    assert head == skills.UPSTREAM_COMMIT
    for rel in (skills.HELPER[0], *(rel for rel, _ in skills.PINNED_SCRIPTS.values()), skills.LICENSE[0]):
        assert (skills.VENDOR_DIR / rel).read_bytes() == (root / rel).read_bytes(), rel
    assert (skills.VENDOR_DIR / skills.HELPER[0]).read_bytes() == (root / "shared/_bpyx.py").read_bytes()  # upstream's source of truth


@pytest.mark.blender
async def test_upstream_primitives_on_a_package_match_the_fake_shape_and_weld_closes_seams(tmp_path):
    """The fake worker's results have the real scripts' shape, and inspect_usd(weld=True) restores a box's connectivity."""
    if not find_blender():
        pytest.skip("requires stock Blender")
    from antigravity3d.ir import MaterialIR, NodeIR, SceneIR, Transform
    from antigravity3d.kernel import fast_ops
    from antigravity3d.kernel.package import write_engine_package
    from antigravity3d.kernel.usd_stage import StageManager
    from antigravity3d.state import EngineBudget

    budget = EngineBudget("unreal_5")
    scene = SceneIR(asset_name="Block", materials=[MaterialIR(id="m0")],
                    nodes=[NodeIR(id="block", kind="box", params={"size": (1.0, 0.5, 0.25)}, material_id="m0", transform=Transform(translate=(0, 0, 0.125)))])
    sm = StageManager.create(tmp_path / "Block.usda", "Block")
    sm.apply_ir(scene)
    sm.commit_all()
    bundle = fast_ops.run_export_pipeline(sm.extract_meshes(), budget, "Block")
    usd = str(write_engine_package(sm, bundle, budget, tmp_path / "package", asset_name="Block", qa={"tier1_ok": True}).out_dir / "Block.usd")
    calls = {"mesh_stats": {"budget": "pc"}, "qa_check": {"profile": "game"}, "kit_check": {"texture": 1024}, "texel_density": {"res": 1024}}
    per_object = {"mesh_stats": "objects", "qa_check": "objects", "kit_check": "pieces", "texel_density": "objects"}
    async with BlenderPool(1, fake=True) as fake, BlenderPool(1, startup_timeout=120, extra_env={"AGY3D_BLENDER_SKILLS": ""}) as real:
        for name, kwargs in calls.items():
            f = await fake.call("inspect_usd", path=usd, primitive=name, kwargs=kwargs)
            r = await real.call("inspect_usd", path=usd, primitive=name, kwargs=kwargs)
            assert "block" in r["agy_inspect"]["objects"], r["agy_inspect"]  # whether guide colliders come along is recorded by the benchmark
            assert set(r) == set(f) - {"fake"}, name
            assert set(r[per_object[name]]["block"]) <= set(f[per_object[name]]["FakeMesh"]), name
        welded = await real.call("inspect_usd", path=usd, primitive="qa_check", kwargs={"profile": "game"}, weld=True)
        assert welded["agy_inspect"]["weld"]["objects"]["block"]["verts_after"] == 8
        block = welded["objects"]["block"]
        assert (block["boundary_edges"], block["islands"], block["duplicate_verts"], block["inside_out_islands"]) == (0, 1, 0, 0)
        glb = str(Path(usd).with_suffix(".glb"))  # the same asset as shipped in the GLB, through Blender's glTF importer
        welded = await real.call("inspect_usd", path=glb, primitive="qa_check", kwargs={"profile": "game"}, weld=True)
        assert welded["agy_inspect"]["importer"] == "gltf" and len(welded["agy_inspect"]["objects"]) == 1
        (block,) = welded["objects"].values()
        assert (block["boundary_edges"], block["islands"], block["inside_out_islands"]) == (0, 1, 0)


def test_unreviewed_blender_skills_are_rejected_before_import(tmp_path):
    from antigravity3d.adapters.blender_5_3.skills import load_reviewed

    helper = tmp_path / "skills/blender-game-optimization/scripts/_bpyx.py"
    helper.parent.mkdir(parents=True)
    helper.write_text("raise AssertionError('unapproved code executed')")
    with pytest.raises(ValueError, match="unexpected blender-skills source digest"):
        load_reviewed(tmp_path)


@pytest.mark.blender
async def test_three_worker_mixed_asset_soak(tmp_path):
    if not find_blender() or os.environ.get("AGY3D_BLENDER_SOAK") != "1":
        pytest.skip("set AGY3D_BLENDER_SOAK=1 with stock Blender to run 300 renders")
    shapes = {"Bar": (4, 0.3, 0.3), "Cube": (1, 1, 1), "Column": (0.4, 0.4, 3)}
    paths = {}
    for name, scale in shapes.items():
        paths[name] = tmp_path / f"{name}.usda"
        write_cube_snapshot(paths[name], name, scale)
    async with BlenderPool(3, startup_timeout=120) as pool:
        pids = pool.worker_pids
        options = dict(engine="cycles", width=24, height=24, samples=1, denoiser="none")
        reference = {name: (await pool.call("render_usd", path=str(path), **options))["image"]
                     for name, path in paths.items()}
        for name, baseline in reference.items():
            assert all(not np.allclose(baseline, ref, atol=1e-4) for k, ref in reference.items() if k != name)
        for _ in range(100):
            results = await pool.map("render_usd", [{"path": str(path), **options} for path in paths.values()])
            for (name, baseline), result in zip(reference.items(), results, strict=True):
                assert result["objects"] == [name]
                np.testing.assert_allclose(result["image"], baseline, atol=1e-4)
        latencies_ms = []
        for _ in range(150):
            start = time.perf_counter_ns()
            assert (await pool.call("ping"))["pong"]
            latencies_ms.append((time.perf_counter_ns() - start) / 1e6)
        p95_ms = float(np.percentile(latencies_ms, 95))
        print(f"Blender warm idle ping p95: {p95_ms:.3f} ms (150 calls, 3 workers)")
        assert p95_ms < 10, f"warm idle ping p95 {p95_ms:.3f} ms exceeds the 10 ms budget"
        assert pool.worker_pids == pids


@pytest.mark.blender
async def test_real_blender_legacy_migrations():
    if not find_blender():
        pytest.skip("requires stock Blender")
    async with BlenderPool(1, startup_timeout=120) as pool:
        result = await pool.call(
            "run_script", legacy=True, source=(
                'import bpy\n'
                'from bpy.types import AssetHandle as OldHandle\n'
                'scene = bpy.context.scene\n'
                'scene.render.engine = "BLENDER_EEVEE_NEXT"\n'
                'obj = bpy.data.objects.new("Block", bpy.data.meshes.new("Block"))\n'
                'scene.collection.objects.link(obj)\n'
                'modifier = obj.modifiers.new("Test", "BOOLEAN")\n'
                'modifier.solver = "FAST"\n'
                'result = [scene.render.engine, modifier.solver, OldHandle == bpy.types.AssetRepresentation]'))
        assert result["result"] == ["BLENDER_EEVEE", "FLOAT", True]


@pytest.mark.blender
@pytest.mark.parametrize("which", ["interior", "exterior"])
async def test_upstream_kit_check_passes_every_piece_of_an_offline_kit(tmp_path, which):
    """EL4CTEO/blender-skills' kit check, run by the worker on the piece files a kit ships (coincident vertices
    merged, as every topology check on these files must be), with the kit's own grid, pivot and sheet size: no
    piece has an error. Before kits were built to one metric every object failed it."""
    if not find_blender():
        pytest.skip("no Blender binary ($BLENDER_BIN or PATH)")
    from antigravity3d.dags.environment_kit import EnvironmentKitDAG
    from antigravity3d.genmedia.nano_banana import FakeImageBackend, ImageForge
    from antigravity3d.kernel.kit_metric import KitMetric
    from antigravity3d.state import EngineBudget
    from antigravity3d.tactics.authoring import TemplateAuthor

    metric = KitMetric.parse(which)
    dag = EnvironmentKitDAG(forge=ImageForge(FakeImageBackend()), author=TemplateAuthor(), workdir=tmp_path / "kit",
                            budget=EngineBudget("unreal_5"), sheet_resolution=1024, metric=metric)
    rep = await dag.run("ruined stone temple", 4)
    assert rep.ok and rep.extras["gates_ok"], rep.summary()
    async with BlenderPool(1, startup_timeout=120) as pool:
        for name, path in rep.extras["pieces"].items():
            r = await pool.call("inspect_usd", path=path, primitive="kit_check", weld=True,
                                kwargs={"grid": metric.grid_m, "pivot": metric.pivot, "texture": 1024, "prefix": ""})
            (obj, rec), = r["pieces"].items()  # one mesh per piece (Blender may suffix the name: the root prim has it too)
            assert obj.startswith(name) and r["pass"] and not rec["errors"], (name, obj, rec)
