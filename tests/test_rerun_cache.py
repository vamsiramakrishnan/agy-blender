"""A rerun uses the images the first run paid for without needing image allowance or budget for them.

Found on 2026-11-22 (round five): the second run of the same command (same --out) shipped one material flat and blamed
tokens. The ledger belongs to the asset (round four), so the rerun starts with the first run's images already counted
(3 of ``--max-images 4``); ``Governor.allocate`` granted the 1 image left to the largest material, and the other,
whose image was in the cache and would have cost none, was named "budget: 95,483 tokens left" while 95,483 tokens were
left and the image limit was what ran out. Now a material whose every planned candidate is in the image cache is
generated from it whatever the limits say (best-of-N too: the tournament does not trim candidates that need no image),
a material with only some variants cached is allocated as before and the report says how many were cached, and the
reason text names the limit that refused.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

from antigravity3d.decisions.engine import DecisionControlPlane
from antigravity3d.decisions.fakes import FakeDecisionsClient
from antigravity3d.genmedia.nano_banana import FakeImageBackend, ImageForge
from antigravity3d.ir import MaterialIR, SceneIR
from antigravity3d.kernel.cache import BuildCache, LocalBlobStore
from antigravity3d.tactics.cached import CachedForge
from antigravity3d.tactics.governor import Governor, plan_for
from antigravity3d.tactics.pyramid import synthesize_materials

sys.path.insert(0, str(Path(__file__).parent))
from test_provenance import key, manifest_of, prop  # noqa: E402,F401  (the stub service fixture and helpers)

PLANE = DecisionControlPlane(FakeDecisionsClient(answers={"policy_trademark_logo": 0.0, "policy_legible_text": 0.0,
                                                          "policy_real_person": 0.0, "policy_unsafe_content": 0.0}))


def mats(*ids: str) -> SceneIR:
    return SceneIR(asset_name="a", materials=[MaterialIR(id=i) for i in ids])


async def run(scene: SceneIR, governor: Governor, cache: BuildCache):
    forge = CachedForge(ImageForge(FakeImageBackend()), cache).with_governor(governor)
    return await synthesize_materials(scene, forge, plane=PLANE, governor=governor, prompt="crate", size=64, cache=cache)


def albedos(out) -> dict[str, bytes]:
    return {k: np.asarray(v.maps.albedo).tobytes() for k, v in out.items() if v.maps is not None}


async def test_best_of_n_from_the_cache_under_a_used_up_image_limit(tmp_path):
    cache = BuildCache(LocalBlobStore(tmp_path))
    n = plan_for(2).pbr_candidates
    g = Governor(plan_for(2), token_ceiling=10**7, max_images=2 * n)
    first = await run(mats("a", "b"), g, cache)
    assert g.images_used == 2 * n and all(v.tournament.n_planned == n for v in first.values())
    again = await run(mats("a", "b"), g, cache)  # the same ledger: every image slot is already counted
    assert g.images_used == 2 * n  # no image was generated
    assert all(v.tournament.n_planned == n and v.maps is not None for v in again.values()), {k: v.tournament.notes for k, v in again.items()}
    assert albedos(again) == albedos(first)  # the same winner
    assert all("not_textured" not in v.tournament.notes for v in again.values())


async def test_one_candidate_each_and_the_larger_material_no_longer_takes_the_last_slot(tmp_path):
    cache = BuildCache(LocalBlobStore(tmp_path))
    per = plan_for(1).pbr_candidates
    g = Governor(plan_for(1), token_ceiling=10**7, max_images=2 * per)
    first = await run(mats("wood", "iron"), g, cache)
    assert g.images_used == 2 * per and all(v.maps is not None for v in first.values())
    g2 = Governor(plan_for(1), token_ceiling=10**7, max_images=2 * per)
    g2.reserve("image.earlier", 0, images=2 * per - 1).settle(0)  # the first run's images, but one slot left
    again = await run(mats("wood", "iron"), g2, cache)
    assert all(v.maps is not None for v in again.values()) and g2.images_used == 2 * per - 1


async def test_partly_cached_is_allocated_as_before_and_says_what_the_cache_covered(tmp_path):
    cache = BuildCache(LocalBlobStore(tmp_path))
    g = Governor(plan_for(2), token_ceiling=10**7, max_images=2)
    await run(mats("a", "b"), g, cache)  # best-of-1 each: only the first variant of each is cached
    assert g.images_used == 2
    out = await run(mats("a", "b"), g, cache)  # used up: a material with 1 of N cached still needs images
    assert all(v.maps is None and v.tournament.notes["not_textured"].startswith("image limit reached (2 of 2 images)") for v in out.values())
    roomy = Governor(plan_for(2), token_ceiling=10**7, max_images=20)
    out = await run(mats("a", "b"), roomy, cache)
    n = plan_for(2).pbr_candidates
    assert all(v.maps is not None and v.tournament.notes["cached_variants"] == f"1 of {n} candidate image(s) were in the cache" for v in out.values())
    assert roomy.images_used == 2 * (n - 1)  # the cached variant of each cost no image


async def test_the_reason_names_the_limit_that_refused(tmp_path):
    cache = BuildCache(LocalBlobStore(tmp_path))
    images = Governor(plan_for(2), token_ceiling=10**7, max_images=1)
    images.reserve("image.earlier", 0, images=1).settle(0)
    out = await run(mats("a"), images, cache)
    assert out["a"].tournament.notes["not_textured"].startswith("image limit reached")  # tokens were plentiful
    tokens = Governor(plan_for(2), token_ceiling=5_000, max_images=50)
    out = await run(mats("a"), tokens, cache)
    why = out["a"].tournament.notes["not_textured"]
    assert why.startswith("budget:") and "tokens left" in why  # images were plentiful


# ------------------------------------------------------------------------- the command, twice


def used(out: Path) -> tuple[int, int]:
    cost = json.loads((out / "report.json").read_text())
    cost = cost.get("extras", cost)["cost"]
    return cost["images"] if "images" in cost else 0, cost["this_run"]["images"] if cost.get("this_run") else 0


def test_the_second_run_of_the_same_command_textures_what_the_first_did_single_candidate(key, tmp_path, capsys):
    out = tmp_path / "out"
    args = ["--preview", "--token-ceiling", "600000", "--max-images", "4", "--single-candidate"]
    first_api = key()
    assert prop(out, *args) == 0
    first = manifest_of(out)
    second_api = key()
    assert prop(out, *args) == 0
    capsys.readouterr()
    second = manifest_of(out)
    assert not any(k == "image:sent" for k in second_api.kinds()) and sum(k == "image:sent" for k in first_api.kinds()) >= 2
    mats_of = lambda m: sorted({t["material"] for t in m["textures"]})  # noqa: E731
    assert len(mats_of(first)) >= 2 and mats_of(second) == mats_of(first)
    assert [w for w in second.get("warnings", []) if "untextured" in w] == [] and "textures_missing" not in json.dumps(second["evidence"])
    assert {k: v["sha256"] for k, v in second["file_hashes"].items() if k.startswith("textures/")} == \
           {k: v["sha256"] for k, v in first["file_hashes"].items() if k.startswith("textures/")}  # the same winners


def test_the_second_run_of_the_same_command_with_best_of_n_and_the_limit_used_up(key, tmp_path, capsys):
    """Both runs say ``--max-images 6``: the limit sizes the spend plan ('simple': best-of-2 for the blueprint and each
    material), and a different limit would be a different plan, hence different requests and no design to reuse."""
    out = tmp_path / "out"
    args = ["--preview", "--token-ceiling", "900000", "--max-images", "6"]
    first_api = key()
    assert prop(out, *args) == 0
    first = manifest_of(out)
    sent = sum(k == "image:sent" for k in first_api.kinds())
    assert sent >= 5  # best-of-2 for the blueprint and each of the materials: the limit is (nearly) used up
    second_api = key()
    assert prop(out, *args) == 0
    capsys.readouterr()
    second = manifest_of(out)
    assert not any(k == "image:sent" for k in second_api.kinds())
    assert sorted({t["material"] for t in second["textures"]}) == sorted({t["material"] for t in first["textures"]})
    assert {k: v["sha256"] for k, v in second["file_hashes"].items() if k.startswith("textures/")} == \
           {k: v["sha256"] for k, v in first["file_hashes"].items() if k.startswith("textures/")}  # the same winners
