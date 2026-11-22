"""Material textures are allocated per material, largest surface first, and an untextured material is always named.

``synthesize_materials`` used to size one best-of-N for all k materials: with 3 images left and 4 materials that N was
0, no material got a texture, the allowance went unused and pass B said "no forge and none supplied". Now every
material gets one candidate before any gets a second, up to the plan's N, within the images and budget left.
"""

from __future__ import annotations

import numpy as np
import pytest

from antigravity3d.decisions.engine import DecisionControlPlane
from antigravity3d.decisions.fakes import FakeDecisionsClient
from antigravity3d.genmedia.nano_banana import FakeImageBackend, ImageForge
from antigravity3d.ir import MaterialIR, SceneIR
from antigravity3d.kernel.types import MeshData
from antigravity3d.tactics.common import IMAGE_USD
from antigravity3d.tactics.tournament import GRADE_USD_EST
from antigravity3d.tactics.governor import Governor, plan_for
from antigravity3d.tactics.pyramid import materials_by_area, synthesize_materials

PLANE = DecisionControlPlane(FakeDecisionsClient(answers={"policy_trademark_logo": 0.0, "policy_legible_text": 0.0,
                                                          "policy_real_person": 0.0, "policy_unsafe_content": 0.0}))


def mats(*ids: str) -> SceneIR:
    return SceneIR(asset_name="a", materials=[MaterialIR(id=i) for i in ids])


async def run(scene: SceneIR, governor: Governor, forge: ImageForge | None = None):
    forge = forge or ImageForge(FakeImageBackend())
    out = await synthesize_materials(scene, forge.with_governor(governor), plane=PLANE, governor=governor, prompt="crate", size=64)
    return out, {k: v.tournament.n_planned for k, v in out.items()}


async def test_four_materials_three_images_texture_the_three_largest_and_name_the_fourth():
    g = Governor(plan_for(2), token_ceiling=10**7, max_images=3)
    out, n = await run(mats("big", "mid", "small", "tiny"), g)
    assert n == {"big": 1, "mid": 1, "small": 1, "tiny": 0}
    assert all(out[k].maps is not None for k in ("big", "mid", "small")) and out["tiny"].maps is None
    assert "image limit reached (3 of 3 images)" in out["tiny"].tournament.notes["not_textured"]
    assert g.images_used == 3


async def test_a_blueprint_and_three_materials_with_three_images():
    g = Governor(plan_for(2), token_ceiling=10**7, max_images=3)
    g.reserve("image.ortho", 0, images=1).settle(0)  # the blueprint took one
    out, n = await run(mats("a", "b", "c"), g)
    assert n == {"a": 1, "b": 1, "c": 0} and out["c"].tournament.notes["not_textured"].startswith("image limit reached")


async def test_three_materials_eleven_images_get_four_four_three():
    g = Governor(plan_for(2), token_ceiling=10**7, max_images=11)  # the standard plan's best-of-4
    _, n = await run(mats("a", "b", "c"), g)
    assert n == {"a": 4, "b": 4, "c": 3} and g.images_used == 11


async def test_everything_fits_gives_every_material_the_plans_n_as_before():
    for g in (Governor(plan_for(2)), Governor(plan_for(2), token_ceiling=10**7, max_images=20)):
        before = g.candidates("pbr", unit_cost=3 * (IMAGE_USD["albedo"] + GRADE_USD_EST), images_each=3)
        _, n = await run(mats("a", "b", "c"), g)
        assert set(n.values()) == {g.plan.pbr_candidates} and (g.unit == "tokens" or before == g.plan.pbr_candidates)


async def test_the_dollar_unit_allocates_the_same_way():
    unit = IMAGE_USD["albedo"] + GRADE_USD_EST
    plan = plan_for(2)
    g = Governor(plan)
    g.charge("earlier", plan.ceiling_usd - 5 * unit)  # room for 5 candidates
    assert g.allocate("pbr", 3, unit_cost=unit, images_each=1) == [2, 2, 1]
    g.charge("more", 4.5 * unit)  # room for none
    out, n = await run(mats("a", "b"), g)
    assert n == {"a": 0, "b": 0} and out["a"].tournament.notes["not_textured"].startswith("budget: $")


def test_materials_are_ordered_by_the_surface_they_cover():
    def quad(mid: str, s: float) -> MeshData:
        pts = np.array([[0, 0, 0], [s, 0, 0], [s, s, 0], [0, s, 0]], float)
        return MeshData(mid, f"/{mid}", pts, np.array([[0, 1, 2], [0, 2, 3]], np.int32), material_id=mid)

    meshes = [quad("small", 1), quad("big", 3), quad("mid", 2)]
    order = [m.id for m in materials_by_area(meshes, [MaterialIR(id=i) for i in ("small", "unused", "mid", "big")])]
    assert order == ["big", "mid", "small", "unused"]


async def test_pass_b_names_untextured_materials_and_the_true_cause(tmp_path):
    from antigravity3d.kernel.usd_stage import StageManager
    from antigravity3d.state import EngineBudget
    from antigravity3d.tactics.authoring import TemplateAuthor
    from antigravity3d.tactics.pyramid import refine

    budget = EngineBudget("unreal_5")
    scene = (await TemplateAuthor().propose("wooden crate with iron brackets", budget=budget, n=1, context={"asset_name": "Crate"}))[0]
    assert len(scene.materials) >= 2
    sm = StageManager.create(tmp_path / "c.usda", "Crate")
    sm.apply_ir(scene)
    for images, expect in ((1, "textures_missing"), (0, "no material was textured: ")):
        g = Governor(plan_for(1), token_ceiling=10**7, max_images=images)
        res = await refine(sm, plane=PLANE, author=TemplateAuthor(), governor=g, budget=budget, forge=ImageForge(FakeImageBackend()).with_governor(g),
                           workdir=tmp_path / f"wd{images}", run_pass_c=False, settle_first=False)
        steps = {s.name: s for s in res.steps}
        if images:
            assert steps["textures"].status == "passed" and steps["textures_missing"].status == "warned"
            assert "image limit reached" in steps["textures_missing"].detail
        else:
            assert steps["textures"].status == "skipped" and steps["textures"].detail.startswith(expect)
            assert "no forge" not in steps["textures"].detail


async def test_pass_b_still_says_nothing_was_supplied_when_that_is_the_cause(tmp_path):
    from antigravity3d.kernel.usd_stage import StageManager
    from antigravity3d.state import EngineBudget
    from antigravity3d.tactics.authoring import TemplateAuthor
    from antigravity3d.tactics.pyramid import refine

    budget = EngineBudget("unreal_5")
    scene = (await TemplateAuthor().propose("wooden crate", budget=budget, n=1, context={"asset_name": "Crate"}))[0]
    sm = StageManager.create(tmp_path / "c.usda", "Crate")
    sm.apply_ir(scene)
    res = await refine(sm, plane=PLANE, author=TemplateAuthor(), governor=Governor(plan_for(1)), budget=budget, forge=None,
                       workdir=tmp_path / "wd", run_pass_c=False, settle_first=False)
    t = next(s for s in res.steps if s.name == "textures")
    assert t.status == "skipped" and t.detail == "no PBR textures available (no forge and none supplied)"
