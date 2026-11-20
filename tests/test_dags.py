from __future__ import annotations

import asyncio
import json
from pathlib import Path

import numpy as np
import pytest
import trimesh
from pxr import Usd

from antigravity3d.dags.batch_sanitize import BatchSanitizeDAG, repair_mesh
from antigravity3d.dags.environment_kit import EnvironmentKitDAG
from antigravity3d.dags.prop_to_engine import PropToEngineDAG
from antigravity3d.dags.runner import Dag, NodeCtx
from antigravity3d.dags.variant_factory import VariantFactoryDAG
from antigravity3d.decisions.engine import DecisionControlPlane
from antigravity3d.decisions.fakes import FakeDecisionsClient
from antigravity3d.genmedia.nano_banana import FakeImageBackend, ImageForge
from antigravity3d.kernel.cache import BuildCache, LocalBlobStore
from antigravity3d.kernel.types import MeshData
from antigravity3d.kernel.usd_stage import StageManager
from antigravity3d.state import EngineBudget
from antigravity3d.tactics.authoring import TemplateAuthor

UE = EngineBudget("unreal_5")


# --------------------------------------------------------------------------- runner


async def test_runner_orders_overlaps_and_isolates_failures() -> None:
    d = Dag("t")

    async def slow(c: NodeCtx) -> int:
        await asyncio.sleep(0.1)
        return 1

    async def boom(c: NodeCtx) -> None:
        raise RuntimeError("boom")

    d.node("a", slow)
    d.node("b", slow)
    d.node("join", lambda c: c["a"] + c["b"], ["a", "b"])
    d.node("bad", boom, ["a"])
    d.node("after_bad", lambda c: 1, ["bad"])
    d.node("sk", lambda c: c.skip("not needed"))
    d.node("after_skip", lambda c: c["sk"], ["sk"])
    r = await d.run()
    n = r.nodes
    assert r.results["join"] == 2 and n["join"].start_ms >= max(n["a"].end_ms, n["b"].end_ms) - 1  # ordering
    assert n["a"].start_ms < n["b"].end_ms and n["b"].start_ms < n["a"].end_ms and r.wall_ms < 190  # a and b overlapped
    assert n["bad"].status == "failed" and "boom" in n["bad"].error
    assert n["after_bad"].status == "skipped" and n["after_bad"].blocked and "bad" in n["after_bad"].reason
    assert n["join"].status == "ok"  # independent branch unaffected
    assert n["sk"].status == "skipped" and not n["sk"].blocked and n["after_skip"].status == "ok"  # a self-skip does not block dependents
    assert not r.ok and json.loads(r.to_json())["nodes"] and "bad" in r.summary()


async def test_runner_rejects_cycles_and_unknown_deps_and_bounds_parallelism() -> None:
    d = Dag("c")
    d.node("x", lambda c: 1, ["y"])
    d.node("y", lambda c: 1, ["x"])
    with pytest.raises(ValueError, match="cycle"):
        await d.run()
    d2 = Dag("u")
    d2.node("x", lambda c: 1, ["nope"])
    with pytest.raises(ValueError, match="unknown"):
        await d2.run()
    d3 = Dag("p")

    async def s(c: NodeCtx) -> None:
        await asyncio.sleep(0.03)

    for i in range(4):
        d3.node(f"n{i}", s)
    assert (await d3.run(max_parallel=2)).max_overlap() == 2


# --------------------------------------------------------------------------- prop -> engine (+ cache)


def make_plane() -> DecisionControlPlane:
    return DecisionControlPlane(FakeDecisionsClient(answers={"pipeline_dag": "prop_to_engine", "needs_ortho_blueprint": 0.9, "complexity_tier": 2.0}))


@pytest.fixture(scope="module")
def prop_runs(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("prop")
    cache = BuildCache(LocalBlobStore(root / "cache"))
    out = []
    for name in ("run1", "run2"):
        dag = PropToEngineDAG(plane=make_plane(), forge=ImageForge(FakeImageBackend()), author=TemplateAuthor(), workdir=root / name, budget=UE, cache=cache)
        before = (cache.stats.hits, cache.stats.misses)
        out.append((asyncio.run(dag.run("oak wine barrel with iron hoops")), before, (cache.stats.hits, cache.stats.misses)))
    return root, cache, out


def test_prop_dag_outputs_are_real(prop_runs) -> None:
    root, _, runs = prop_runs
    rep = runs[0][0]
    x = rep.extras
    assert rep.ok, rep.summary()
    out = x["outputs"]
    stage = Usd.Stage.Open(out["usd"])
    assert stage.GetDefaultPrim() and out["usd"].endswith(".usd")
    names = [p.GetName() for p in stage.Traverse()]
    assert any(n.startswith("UCX_") for n in names)  # colliders named for Unreal
    glb = trimesh.load(out["glb"], force="scene")
    assert 1 <= len(glb.geometry) <= UE.max_draw_calls
    pyr = x["pyramid"]
    lod = next(s["detail"] for s in pyr["steps"] if s["name"] == "uv_lod_colliders")
    tris = json.loads(lod.split("lod_tris=")[1].split("]")[0] + "]")
    assert tris == sorted(tris, reverse=True) and tris[0] > tris[-1]  # LOD triangle counts descend
    assert "<html" in Path(out["inspector"]).read_text().lower()
    assert json.loads(Path(x["report_path"]).read_text())["dag"] == "prop_to_engine"
    assert (Path(out["usd"]).parent / "textures").is_dir()
    # skipped steps carry reasons
    assert x["skipped"] and all(s["reason"] for s in x["skipped"])
    assert any("binary not found" in s["reason"] for s in x["skipped"])


def test_prop_dag_runs_texture_synthesis_while_geometry_compiles(prop_runs) -> None:
    n = prop_runs[2][0][0].nodes
    assert n["geometry"].start_ms < n["pbr"].end_ms and n["pbr"].start_ms < n["geometry"].end_ms
    assert n["style_anchor"].status == "skipped" and n["ortho"].status == "ok" and n["propose"].start_ms >= n["ortho"].end_ms - 1


def test_second_identical_run_hits_the_cache_for_everything(prop_runs) -> None:
    _, cache, runs = prop_runs
    (_, _, after1), (rep2, before2, after2) = runs
    assert after1[1] > 0  # run 1 filled the cache (images, PBR, UV/LOD/colliders)
    assert after2[1] == before2[1], "run 2 must have no cache misses"
    assert after2[0] - before2[0] >= after1[1]
    assert cache.stats.saved_cost_usd == 0 and cache.stats.saved_ms > 0  # fake generation has no bill
    assert rep2.ok and rep2.extras["cost"]["cache"]["hits"] > 0


def test_two_runs_of_one_prompt_ship_identical_packages(prop_runs) -> None:
    """Through the whole pipeline the package is a function of the scene and the budget: a cold-cache run and a
    warm-cache run of one prompt, in different directories, ship the same bytes. The manifest used to carry each
    step's duration, which made it the one file that differed; durations belong to the run report."""
    _, _, runs = prop_runs
    packages = [Path(rep.extras["outputs"]["usd"]).parent for rep, _, _ in runs]
    assert packages[0] != packages[1]
    files = [{str(p.relative_to(pkg)): p.read_bytes() for p in sorted(pkg.rglob("*")) if p.is_file()} for pkg in packages]
    assert set(files[0]) == set(files[1]) and {"manifest.json", "inspector.html"} <= set(files[0])
    different = sorted(name for name in files[0] if files[0][name] != files[1][name])
    assert not different, f"files that differ between two runs of the same prompt: {different}"
    shipped = json.loads(files[0]["manifest.json"])["evidence"]["steps"]
    assert shipped and all(set(step) == {"name", "status", "detail"} for step in shipped)
    recorded = [rep.extras["pyramid"]["steps"] for rep, _, _ in runs]  # the run reports keep timings and cache state
    assert [s["name"] for s in recorded[0]] == [s["name"] for s in shipped] and all("ms" in s for s in recorded[0])
    assert [next(s["run"] for s in steps if s["name"] == "uv_lod_colliders") for steps in recorded] == ["cache=miss", "cache=hit"]


# --------------------------------------------------------------------------- batch sanitize


def broken_obj(path: Path) -> None:
    b = trimesh.creation.box(extents=(1, 1, 1))
    b.apply_translation((0, 0, 2.5))  # floating
    v = b.vertices[b.faces].reshape(-1, 3)
    f = np.arange(len(v)).reshape(-1, 3).copy()
    f[[0, 3]] = f[[0, 3]][:, ::-1]  # two flipped faces
    v = np.vstack([v, np.zeros((3, 3))])
    f = np.vstack([f, [[len(v) - 3, len(v) - 2, len(v) - 1]]])  # one degenerate face
    trimesh.Trimesh(v, f, process=False).export(path)


def test_repair_mesh_welds_duplicates_and_drops_degenerates() -> None:
    b = trimesh.creation.box()
    v = b.vertices[b.faces].reshape(-1, 3)
    soup = MeshData("m", "", np.vstack([v, np.zeros((3, 3))]), np.vstack([np.arange(36).reshape(-1, 3), [[36, 37, 38]]]).astype(np.int32))
    fixed, c = repair_mesh(soup)
    assert c["merged_vertices"] > 20 and c["degenerate_faces"] == 1 and len(fixed.points) == 8 and len(fixed.triangles) == 12


async def test_batch_sanitize_repairs_broken_mesh_and_isolates_a_bad_file(tmp_path: Path) -> None:
    broken_obj(tmp_path / "broken.obj")
    trimesh.creation.cylinder(radius=0.3, height=1.0, sections=32).export(tmp_path / "cyl.glb")
    (tmp_path / "garbage.stl").write_text("this is not a mesh")
    sm = StageManager.create(tmp_path / "prop.usda", "Prop")
    sm.apply_ir((await TemplateAuthor().propose("oak table", budget=UE, n=1, context={"asset_name": "Prop"}))[0])
    sm.commit_all()
    rep = await BatchSanitizeDAG(workdir=tmp_path / "out", budget=UE, max_parallel=2).run(
        [tmp_path / "broken.obj", tmp_path / "cyl.glb", tmp_path / "garbage.stl", tmp_path / "prop.usda"])
    rows = {Path(r["file"]).name: r for r in rep.extras["rows"]}
    b = rows["broken.obj"]
    assert b["status"] == "ok" and b["tris_before"] == 13 and b["tris_after"] == 12
    assert {"degenerate_tris@broken_obj", "inconsistent_winding@broken_obj"} <= set(b["issues_fixed"]) and b["issues_remaining"] == []
    assert b["repairs"]["grounded_dz"] == -2.0 and b["repairs"]["reoriented_faces"] > 0
    cyl = rows["cyl.glb"]
    assert cyl["lod_tris"] == sorted(cyl["lod_tris"], reverse=True) and cyl["lod_tris"][0] > cyl["lod_tris"][-1]
    assert rows["garbage.stl"]["status"] == "failed"  # isolated: the others still completed
    assert rows["prop.usda"]["status"] == "ok"
    for r in (b, cyl, rows["prop.usda"]):
        stage = Usd.Stage.Open(r["outputs"]["usd"])
        assert any(p.GetName().startswith("UCX_") for p in stage.Traverse())
        glb = Path(r["outputs"]["glb"])
        geometry = trimesh.load(glb, force="scene").geometry
        assert sum(len(g.faces) for g in geometry.values()) == r["tris_after"]
        assert glb.with_name(glb.stem + "_colliders.glb").is_file()
        assert r["validate_critical"] == [] and r["compliance_critical"] == []
    assert not rep.extras["gates_ok"] and json.loads((tmp_path / "out" / "report.json").read_text())["rows"]
    assert rep.extras["skipped"][0]["reason"]


async def test_batch_sanitize_reports_remaining_issues(tmp_path: Path) -> None:
    trimesh.creation.box().export(tmp_path / "box.glb")
    rep = await BatchSanitizeDAG(workdir=tmp_path / "o", budget=EngineBudget("unreal_5", max_tris=5)).run([tmp_path / "box.glb"])
    r = rep.extras["rows"][0]
    assert r["status"] == "needs_attention" and any(t.startswith("tri_budget") for t in r["issues_remaining"])


# --------------------------------------------------------------------------- environment kit


async def test_environment_kit_shares_one_trim_sheet(tmp_path: Path) -> None:
    (tmp_path / "bg.ply").write_bytes(b"ply\nformat ascii 1.0\nend_header\n")
    dag = EnvironmentKitDAG(forge=ImageForge(FakeImageBackend()), author=TemplateAuthor(), workdir=tmp_path / "kit", budget=UE, sheet_resolution=512)
    rep = await dag.run("ruined stone temple", 4, tmp_path / "bg.ply")
    x = rep.extras
    assert rep.ok and x["gates_ok"], rep.summary()
    assert Path(x["trim_sheet"]).exists() and x["trim_issues"] == []
    stage = Usd.Stage.Open(x["kit_usd"])
    kids = [c for c in stage.GetDefaultPrim().GetChildren() if c.GetName() != "Backdrop"]
    assert len(kids) == 4 and all(any(p.IsA("Mesh") for p in Usd.PrimRange(k)) for k in kids)  # references resolved
    assert all(abs(z) < 1e-3 for z in x["piece_min_z"].values())  # settle brought every lifted piece to the ground
    assert all(dz < -0.3 for dz in x["settle_dz"].values())
    assert x["vram"]["unique_bytes"] > x["vram"]["shared_bytes"] and x["vram"]["ratio"] > 1
    assert x["spatial_overview"]
    back = stage.GetPrimAtPath("/Kit/Backdrop")
    assert back.GetCustomDataByKey("agy:splat")["format"] == "ply"
    assert rep.nodes["splat_import"].status == "skipped" and "Blender" in rep.nodes["splat_import"].reason
    assert any("Blender" in s["reason"] for s in x["skipped"])


async def test_environment_kit_bad_splat_is_reported_not_fatal(tmp_path: Path) -> None:
    dag = EnvironmentKitDAG(forge=ImageForge(FakeImageBackend()), author=TemplateAuthor(), workdir=tmp_path / "kit", budget=UE, sheet_resolution=256)
    rep = await dag.run("kit", 2, tmp_path / "missing.spz")
    assert rep.ok and not rep.extras["gates_ok"] and "not found" in rep.extras["backdrop"]["error"]


# --------------------------------------------------------------------------- variants


async def test_variant_factory_builds_k_variants_each_validated_and_exported(tmp_path: Path) -> None:
    sm = StageManager.create(tmp_path / "crate.usda", "Crate")
    sm.apply_ir((await TemplateAuthor().propose("wooden crate", budget=UE, n=1, context={"asset_name": "Crate"}))[0])
    sm.commit_all()
    rep = await VariantFactoryDAG(workdir=tmp_path / "v", budget=UE, forge=ImageForge(FakeImageBackend())).run([tmp_path / "crate.usda"])
    row = rep.extras["rows"][0]
    assert rep.ok and row["status"] == "ok" and row["overlays"] == "forge"
    stage = Usd.Stage.Open(row["looks_usd"])
    assert sorted(stage.GetDefaultPrim().GetVariantSet("look").GetVariantNames()) == ["battle_scarred", "muddy", "pristine", "snow_dusted"]
    means = {v["look"]: v["albedo_mean"]["wood"] for v in row["variants"]}
    assert len({tuple(m) for m in means.values()}) == 4  # the looks really differ
    assert means["snow_dusted"][0] > means["muddy"][0]
    for v in row["variants"]:
        assert v["validate_critical"] == [] and Path(v["export"]).exists() and Usd.Stage.Open(v["export"])
        assert v["look"] in v["composed_albedo"]["wood"]  # the selected variant composed its own textures
    sm2 = StageManager.open(Path(row["looks_usd"]))
    assert sm2.base_ir().materials[0].albedo_uri is None or "textures" not in sm2.base_ir().materials[0].albedo_uri  # base untouched


async def test_variant_factory_without_forge_records_skipped_overlays(tmp_path: Path) -> None:
    sm = StageManager.create(tmp_path / "t.usda", "T")
    sm.apply_ir((await TemplateAuthor().propose("oak table", budget=UE, n=1, context={"asset_name": "T"}))[0])
    sm.commit_all()
    rep = await VariantFactoryDAG(workdir=tmp_path / "v", budget=UE, looks=("pristine", "muddy")).run([tmp_path / "t.usda"])
    assert rep.ok and len(rep.extras["rows"][0]["variants"]) == 2 and "no forge" in rep.extras["skipped"][0]["reason"]


async def test_batch_sanitize_godot_target_uses_valid_usd_collider_names(tmp_path: Path) -> None:
    trimesh.creation.cylinder(radius=0.3, height=1.0).export(tmp_path / "cyl.glb")
    rep = await BatchSanitizeDAG(workdir=tmp_path / "o", budget=EngineBudget("godot_4")).run([tmp_path / "cyl.glb"])
    r = rep.extras["rows"][0]
    assert r["status"] == "ok" and rep.extras["gates_ok"], r
    stage = Usd.Stage.Open(r["outputs"]["usd"])
    names = [p.GetName() for p in stage.Traverse()]
    assert any(n.startswith("UCX_") for n in names) and not any("-" in n for n in names)


async def test_required_publication_preserves_previous_generation_on_missing_proof(tmp_path):
    published = tmp_path / "package"
    published.mkdir()
    (published / "previous").write_bytes(b"verified before this run")
    dag = PropToEngineDAG(plane=make_plane(), forge=ImageForge(FakeImageBackend()), author=TemplateAuthor(),
                          workdir=tmp_path, budget=UE, require_verified=True)
    report = await dag.run("oak wine barrel")
    assert not report.ok and not report.extras["verified"] and not report.extras["gates_ok"]
    assert list(published.iterdir()) == [published / "previous"]
    candidate = Path(report.extras["outputs"]["manifest"])
    assert candidate.is_relative_to(tmp_path / "runs")
    assert json.loads(candidate.read_text())["verified"] is False


# --------------------------------------------------------------------------- candidate accounting in the propose node


from antigravity3d.tactics.metered_author import CandidateOutcome, ProposeResult  # noqa: E402


class LosingAuthor(TemplateAuthor):
    """Metered-looking author: has a cost profile and reports one candidate it could not admit."""

    def __init__(self, profile):
        super().__init__()
        self.profile, self.asked = profile, None

    def cost_profile(self, role=None, *, asset_name="asset"):
        return self.profile

    async def propose_with_outcomes(self, prompt, *, budget, n, context):
        self.asked = n
        scenes = await TemplateAuthor.propose(self, prompt, budget=budget, n=n - 1, context=context)
        outcomes = [CandidateOutcome(i, "produced") for i in range(n - 1)]
        outcomes.append(CandidateOutcome(n - 1, "not_admitted", "author.propose needs $0.50000 but can never fit"))
        return ProposeResult(list(scenes), outcomes)


def run_dag(tmp_path: Path, author) -> tuple[object, object]:
    dag = PropToEngineDAG(plane=make_plane(), forge=ImageForge(FakeImageBackend()), author=author, workdir=tmp_path, budget=UE)
    return asyncio.run(dag.run("oak wine barrel with iron hoops"))


def test_propose_node_records_every_candidate_outcome_and_sizes_by_expected_cost(tmp_path: Path) -> None:
    author = LosingAuthor((0.05, 0.10))  # expected $0.05 + grading each; the standard plan asks for 4
    rep = run_dag(tmp_path, author)
    data = rep.nodes["propose"].data
    assert data["n_planned"] == 4 and data["n_requested"] == author.asked == 4 and data["n_proposed"] == 3
    assert [o["status"] for o in data["outcomes"]] == ["produced", "produced", "produced", "not_admitted"]
    assert data["sizing"]["basis"] == "expected author cost + grading" and data["sizing"]["worst_case_candidate_usd"] > data["sizing"]["expected_candidate_usd"]
    assert any("1 of 4 requested geometry candidates produced nothing" in n and "#3 not_admitted" in n for n in rep.nodes["propose"].notes)
    extra = rep.extras["geometry_candidates"]
    assert extra["n_requested"] == 4 and extra["outcomes"][3]["status"] == "not_admitted"
    saved = json.loads(Path(rep.extras["report_path"]).read_text())
    node = next(n for n in saved["nodes"] if n["name"] == "propose")
    assert node["data"]["n_proposed"] == 3 and node["data"]["outcomes"][3]["status"] == "not_admitted"


def test_propose_node_sizes_the_plan_down_by_expected_cost_and_says_so(tmp_path: Path) -> None:
    author = LosingAuthor((0.30, 0.50))  # the ceiling cannot carry four of these
    rep = run_dag(tmp_path, author)
    data = rep.nodes["propose"].data
    assert data["n_requested"] < 4 == data["n_planned"]
    assert any("sizes geometry best-of-4 down to" in n for n in rep.nodes["propose"].notes)


def test_authors_without_outcomes_still_report_n_requested_and_n_proposed(tmp_path: Path) -> None:
    rep = run_dag(tmp_path, TemplateAuthor())
    data = rep.nodes["propose"].data
    assert data["n_requested"] == data["n_planned"] == 4 and data["sizing"]["basis"].startswith("grading only")
    assert data["n_proposed"] == len(data["outcomes"])
