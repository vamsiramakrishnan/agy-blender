"""A material's texture is generated from what its surface is, never from the asset brief.

Live finding (2026-11-21, brief "a rugged military supply crate kit-bashed from panels, corner guards, latches and two
side handles"): all nine albedos were pictures of crate panels with latches, handles and frames drawn in, because each
material's prompt was ``f"{brief}: {m.id} surface"``. Tiled over geometry that already models those parts, they would
paint hardware onto every face.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from antigravity3d import ir_schema
from antigravity3d.genmedia.material_prompt import id_words, texture_prompt
from antigravity3d.genmedia.nano_banana import FakeImageBackend, ImageForge
from antigravity3d.ir import SURFACE_MAX_CHARS, IRPatch, MaterialIR, NodeIR, SceneIR, Transform, UpsertMaterial
from antigravity3d.kernel.cache import ir_subtree_key
from antigravity3d.kernel.usd_stage import StageManager
from antigravity3d.state import EngineBudget
from antigravity3d.tactics import reviewed_rules
from antigravity3d.tactics.authoring import TemplateAuthor
from antigravity3d.tactics.governor import Governor
from antigravity3d.tactics.metered_author import build_author_request, propose_task
from antigravity3d.tactics.pyramid import synthesize_materials

BRIEF = "a rugged military supply crate kit-bashed from panels, corner guards, latches and two side handles"
PARTS = ("crate", "panel", "corner", "guard", "latch", "handle", "kit-bashed", "supply", "military")
LIVE_MATERIALS = [MaterialIR(id="mat_olive_drab", base_color=(0.3, 0.33, 0.2), roughness=0.8),
                  MaterialIR(id="mat_accent", base_color=(0.6, 0.5, 0.1)),
                  MaterialIR(id="mat_metal", base_color=(0.5, 0.5, 0.5), metallic=1.0, roughness=0.25)]


def leaks(prompt: str) -> list[str]:
    """Words of the asset brief (its parts, the object) found in a texture prompt."""
    low = prompt.lower()
    return [w for w in PARTS if re.search(rf"\b{re.escape(w)}", low)] + ([BRIEF] if BRIEF.lower() in low else [])


def old_expression(m: MaterialIR, brief: str = "") -> str:
    """The prompt synthesize_materials built before this change (pyramid.py, base 7f33db1)."""
    return f"{BRIEF}: {m.id} surface" + (f". {brief}" if brief else "")


# ---------------------------------------------------------------------------------------------- the builder


@pytest.mark.parametrize("m", [*LIVE_MATERIALS, *(x.model_copy(update={"surface": s}) for x, s in zip(
    LIVE_MATERIALS, ("olive drab painted steel, chipped at the edges", "faded yellow enamel paint", "bare gunmetal steel")))],
    ids=lambda m: f"{m.id}:{'surface' if m.surface else 'id'}")
@pytest.mark.parametrize("art", ["", "low-resolution retro game texture: chunky readable detail"])
def test_texture_prompt_never_carries_the_asset_brief_or_its_parts(m, art):
    p = texture_prompt(m, art)
    assert not leaks(p), p
    assert p.endswith(f". {art}") if art else "." not in p
    assert not p.endswith(".")  # the forge's template adds the full stop
    assert leaks(old_expression(m, art))  # negative control: the old expression is caught by the same check


def test_surface_words_are_the_subject_and_id_words_the_fallback():
    m = LIVE_MATERIALS[0].model_copy(update={"surface": "olive drab painted steel, chipped at the edges"})
    assert texture_prompt(m) == "olive drab painted steel, chipped at the edges"
    assert texture_prompt(m, "Art.") == "olive drab painted steel, chipped at the edges. Art"
    assert [texture_prompt(x) for x in LIVE_MATERIALS] == [
        "olive drab surface, matte", "accent surface", "metal surface, polished"]
    assert texture_prompt(MaterialIR(id="iron", metallic=1.0, roughness=0.45)) == "iron surface"
    assert texture_prompt(MaterialIR(id="trim", metallic=1.0, roughness=0.8)) == "trim surface, rough metal"
    assert texture_prompt(MaterialIR(id="paint", roughness=0.2)) == "paint surface, glossy"
    assert [id_words(i) for i in ("mat_olive_drab", "rustyIron_02", "wood_mat", "mat", "material_glass_3")] == [
        "olive drab", "rusty iron", "wood", "mat", "glass"]


async def test_synthesize_materials_prompts_carry_no_brief(tmp_path: Path):
    backend = FakeImageBackend()
    forge = ImageForge(backend)
    scene = SceneIR(asset_name="Crate", materials=[LIVE_MATERIALS[0].model_copy(update={"surface": "olive drab painted steel"}),
                                                   LIVE_MATERIALS[2]])
    from antigravity3d.decisions.engine import DecisionControlPlane
    from antigravity3d.decisions.fakes import FakeDecisionsClient
    from antigravity3d.tactics.governor import plan_for

    out = await synthesize_materials(scene, forge, plane=DecisionControlPlane(FakeDecisionsClient()),
                                     governor=Governor(plan_for(2)), prompt=BRIEF, size=32, brief="Painted look.")
    assert set(out) == {"mat_olive_drab", "mat_metal"}
    prompts = [c["prompt"] for c in backend.calls]
    assert prompts and not [p for p in prompts if leaks(p)], prompts
    assert any("of: olive drab painted steel. Painted look. Variation:" in p for p in prompts)
    assert any("of: metal surface, polished. Painted look. Variation:" in p for p in prompts)


# ---------------------------------------------------------------------------------------------- the field


def test_a_document_written_before_the_field_validates_and_serialises_unchanged():
    old = {"id": "wood", "base_color": [0.5, 0.3, 0.2], "metallic": 0.0, "roughness": 0.8}
    m = MaterialIR.model_validate(old)
    assert m.surface == "" and "surface" not in m.model_dump() and "surface" not in m.model_dump_json()
    scene = SceneIR(asset_name="A", materials=[m])
    assert "surface" not in scene.model_dump_json()  # the USD customData of an old scene is byte-identical


def test_surface_is_cleaned_and_bounded_not_rejected():
    # Cleaned: the text is a prompt fragment written by a model; rejecting it would discard a whole paid scene.
    m = MaterialIR(id="m", surface="olive\ndrab​ painted\x00steel‮,\tchipped ")
    assert m.surface == "olive drab painted steel , chipped"
    long = MaterialIR(id="m", surface="painted steel " * 30)
    assert len(long.surface) <= SURFACE_MAX_CHARS and ("painted steel " * 30).startswith(long.surface + " ")  # whole words
    blob = MaterialIR(id="m", surface="x" * 500)  # one long token: hard cut
    assert blob.surface == "x" * SURFACE_MAX_CHARS
    with pytest.raises(ValidationError):
        MaterialIR(id="m", surface=3)
    # the model's own answer goes through the same validator (the wire model is derived from MaterialIR)
    doc = {"asset_name": "C", "materials": [{"id": "m", "surface": "a\u0007b " + "c " * 200}],
           "nodes": [{"id": "b", "kind": "box", "params": {"size": [1, 1, 1]}, "transform": {"translate": [0, 0, 0.5]}, "material_id": "m"}]}
    s = ir_schema.parse_scene(json.dumps(doc))
    assert s.materials[0].surface.startswith("a b c") and len(s.materials[0].surface) <= SURFACE_MAX_CHARS
    assert ir_schema.apply_material_diff(MaterialIR(id="m", surface="oak"), {"roughness": 0.9}).surface == "oak"


def test_surface_counts_in_no_geometry_or_holdout_key():
    node = NodeIR(id="b", kind="box", params={"size": (1, 1, 1)}, transform=Transform(translate=(0, 0, 0.5)), material_id="m",
                  parent=None)
    cyl = NodeIR(id="c", kind="cylinder", params={"radius": 0.2, "height": 1, "segments": 8}, material_id="m",
                 transform=Transform(translate=(2, 0, 0.5)))
    a = SceneIR(asset_name="A", nodes=[node, cyl], materials=[MaterialIR(id="m")])
    b = a.model_copy(update={"materials": [MaterialIR(id="m", surface="olive drab painted steel")]})
    assert reviewed_rules.scene_hash(a) == reviewed_rules.scene_hash(b)
    assert ir_subtree_key(a, "b") == ir_subtree_key(b, "b")


def test_surface_survives_every_stage_round_trip(tmp_path: Path):
    mat = MaterialIR(id="paint", surface="olive drab painted steel, chipped edges", roughness=0.7)
    box = NodeIR(id="body", kind="box", params={"size": (1, 1, 1)}, transform=Transform(translate=(0, 0, 0.5)), material_id="paint")
    sm = StageManager.create(tmp_path / "s.usda", "Crate")
    sm.apply_ir(SceneIR(asset_name="Crate", nodes=[box], materials=[mat]))
    assert sm.base_ir().materials[0].surface == mat.surface

    def surfaces(scene: SceneIR) -> dict[str, str]:
        return {m.id: m.surface for m in scene.materials}

    variant = SceneIR(asset_name="Crate", nodes=[box], materials=[mat, MaterialIR(id="alt", surface="bare gunmetal steel")])
    sm.add_variant("look", "v1", variant)
    sm.select_variant("look", "v1")
    assert surfaces(sm.scene_ir()) == {"paint": mat.surface, "alt": "bare gunmetal steel"}
    sm.promote_variant("look", "v1")
    assert surfaces(sm.base_ir()) == {"paint": mat.surface, "alt": "bare gunmetal steel"}
    sm.apply_patch(IRPatch(ops=[UpsertMaterial(material=mat.model_copy(update={"albedo_uri": "a.png", "roughness": 0.6}))]))
    assert surfaces(sm.base_ir())["paint"] == mat.surface
    sm.export(tmp_path / "out.usda")
    assert surfaces(StageManager.open(tmp_path / "out.usda").base_ir())["alt"] == "bare gunmetal steel"


# ---------------------------------------------------------------------------------------------- authoring and templates


def test_the_authoring_request_asks_for_surface_words_and_costs_little():
    task = propose_task("a rugged military supply crate", index=0, n=3, asset_name="Crate", references=False)
    req = build_author_request("worker", "propose", task, budget=EngineBudget("unreal_5"), context={}, timeout_s=90)
    text = req.contents[0].parts[0].text
    assert ir_schema.SURFACE_RULE in text and "never object parts, shapes or layout" in text
    assert "surface" in req.schema["$defs"]["MaterialIR"]["properties"]
    # Measured 2026-11-21: propose text 4,029 -> 4,234 chars; scene schema 6,064 -> 6,092 bytes (pin < 6,500).
    assert len(ir_schema.SURFACE_RULE.encode()) <= 200
    assert ir_schema.SURFACE_RULE not in ir_schema.IR_SEMANTICS  # not repeated into every stage agent's prompt


@pytest.mark.parametrize("prompt", ["wooden crate with iron brackets", "steel barrel", "stone table", "street lamp",
                                    "plastic shelf"])
async def test_template_materials_carry_surface_words(prompt):
    [scene] = await TemplateAuthor().propose(prompt, budget=EngineBudget("unreal_5"), n=1, context={"asset_name": "A"})
    assert scene.materials and all(m.surface for m in scene.materials)
    assert not [m for m in scene.materials if leaks(texture_prompt(m))]


# ---------------------------------------------------------------------------------------------- native tool


async def test_native_forge_prompts_from_the_surface_and_keeps_it(tmp_path, monkeypatch):
    from test_harness_tools import CRATE, check_line, make_session

    from antigravity3d.harness import tools as T

    monkeypatch.setattr(T, "TEXTURE_PX", 32)
    env = make_session(tmp_path, stage="LOOKDEV")
    await T.apply_scene_ir(json.dumps(CRATE), env.ctx)
    r = check_line(await T.forge_pbr_material("wood", "weathered oak,\nopen grain", env.ctx), 1)
    assert r.startswith("MATERIAL wood v1"), r
    assert env.backend.calls[-1]["prompt"].startswith("Seamless tileable albedo (base colour) texture of: weathered oak, open grain. ")
    assert env.sess().stage.base_ir().materials[0].surface == "weathered oak, open grain"
    check_line(await T.forge_pbr_material("wood", "", env.ctx), 1)  # empty: the material's own surface words
    assert "texture of: weathered oak, open grain. " in env.backend.calls[-1]["prompt"]
