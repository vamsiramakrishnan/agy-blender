"""The colour boundary: a colour a model writes becomes scene data through one function, by one switchable rule.

The model's numbers have the hue and saturation of their words read as sRGB (r5/scratch_colour/EVIDENCE.txt); with the
corrected generated surfaces the exact conversion won a blind judgement 4 of 4 on the crate and the hydrant, so the
default rule (``ir_schema.COLOUR_RULE``) is "srgb". "as_written" is exactly the behaviour before (a process started with
it sends what c8565a7 sent, pinned byte for byte); the metallic sentence (``ir_schema.STATE_METALLIC``) is off. Pinned
too, under every rule:
* every path where a model's answer becomes scene data converts each written colour exactly once (counted; under
  "srgb" a second conversion would also change the value);
* a stored or programmatic scene is never converted (SceneIR built directly, apply_patch, the stage, the design record);
* what a model is shown is the inverse, in the form it writes: model text -> scene -> the text shown back is the same.
What a rule changes at import (the schemas' "#rrggbb", the request's clauses) is checked in a fresh process with the
switches set in its environment (AGY3D_COLOUR_RULE, AGY3D_STATE_METALLIC).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from antigravity3d import ir, ir_schema as S
from antigravity3d.tactics.look_round import look_task, material_table
from antigravity3d.tactics.metered_author import improve_task, repair_task

sys.path.insert(0, str(Path(__file__).parent))

DATA = Path(__file__).parent / "data" / "live_answers"
LIN = S.linear_colour  # the conversion itself, for expected values (the fixture below counts calls to S.linear_colour)
OLIVE = [0.18, 0.22, 0.14]
NODE = {"id": "body", "kind": "box", "params": {"size": [1, 1, 1]}, "transform": {"translate": [0, 0, 0.5]}, "material_id": "paint"}
ANSWER = {"asset_name": "Crate", "nodes": [NODE], "materials": [
    {"id": "paint", "base_color": OLIVE, "roughness": 0.6, "metallic": 0, "surface": "olive drab paint"},
    {"id": "lamp", "base_color": [1, 0.82, 0.54], "emissive_color": [1, 0.8, 0.5], "emissive_strength": 4}]}


@pytest.fixture
def conversions(monkeypatch):
    seen: list = []

    def counted(v, **kw):
        seen.append(v)
        return LIN(v, **kw)

    monkeypatch.setattr(S, "linear_colour", counted)
    return seen


@pytest.fixture(params=S.COLOUR_RULES)
def rule(request, monkeypatch):
    """The rule, switched at run time: the conversion and what is shown follow it (the schemas and the request's clauses
    are built at import, checked in ``test_a_switched_process_...``)."""
    monkeypatch.setattr(S, "COLOUR_RULE", request.param)
    return request.param


# --------------------------------------------------------------------------- the default is inert


# sha256 of what c8565a7 sent (r5/scratch_colour/req_hashes.py run on a checkout of c8565a7): the first design's request
# text and schema, an improvement, a repair and a look-round request on a scene with unrounded colours, the BLOCKOUT and
# LOOKDEV stage texts, the patch, look and correction schemas, the two material-carrying tool declarations, the rules text
C8565A7 = {
    "propose_text": "9d1a4157949f2674287b768cfb57cac5c1fbba9b72094cc81a2b13b5ada93fb9",  # less its Budget: line (environment)
    "propose_schema": "15bfb1f88640fdaacd101e1931ad0e69bf24809a14d85ebec739b22b4fd2f502",
    "improve_text": "d25b020347a94638f3f6a09bf2a7b0380b68b13bc4caabbdc372e5d52978cdcb",
    "repair_text": "1e8ce1cbf9c981faa6fdf0ef38a59afe8d880af3510e1fad586532c3403c77db",
    "look_text": "38225043af014e6c549edce4a7541b52fb4f2a22881384f1851ef24d1e39c08e",
    "blockout_stage": "8ec93a73325b6dc8f6b49dd7b4ef3b03ac93f0b310dbe672b5702b8f71ab6030",
    "lookdev_stage": "c87db5fff0b86a4fcc290727cd9771482501be159182f5d03f69a98d9f6af1f8",
    "patch_schema": "23aaa363b48f63f751519e0c4f9750a589efea445d2ef96f4cbd7ce3118c1e84",
    "look_schema": "1e1d768c1de2ec50a17ee39b8a8d9ba440014452a57e1e77152144450b45151a",
    "correction_schema": "f84aa1cfc6d9d7115e5ffb5a1f8a04a0f17405c88dfd69a68ce4aed5c406f669",
    "apply_scene_ir_tool": "4d9500e81df06a2069c8bf10f399b72f40e5234fd4decf44b163720d7d79ae4d",
    "apply_shader_diff_tool": "7e28193242bec8eb55f33248b1c29755d86007609aea080dd3d24b05b8c3a0b0",
    "rules_text": "28d4c022776fa49686504f02cfdbcbc5173735a2b56c8b497bf8dff2b1ca5df0",
}


def _sent() -> dict:
    import hashlib

    from antigravity3d.harness import agents as A
    from antigravity3d.state import EngineBudget
    from antigravity3d.tactics import look_round as L
    from antigravity3d.tactics.metered_author import DesignViews, build_author_request, propose_task

    scene = ir.SceneIR(asset_name="Crate", nodes=[
        ir.NodeIR(id="body", kind="box", params={"size": [0.8, 0.5, 0.4]}, transform=ir.Transform(translate=(0, 0, 0.2)), material_id="paint"),
        ir.NodeIR(id="latch", kind="box", params={"size": [0.05, 0.01, 0.08]}, transform=ir.Transform(translate=(0.3, -0.255, 0.3)), material_id="steel")],
        materials=[ir.MaterialIR(id="paint", base_color=(0.18, 0.22, 0.14), roughness=0.65, metallic=0.2, surface="olive drab paint"),
                   ir.MaterialIR(id="steel", base_color=(0.4567891, 0.43, 0.41), metallic=0.85, roughness=0.4),
                   ir.MaterialIR(id="lamp", base_color=(1.0, 0.9, 0.7), emissive_color=(1.0, 0.8, 0.5), emissive_strength=3.0)])
    views = DesignViews(images=(("front view of the current design (Image 1)", "https://x/v1"),), legend="[01] body\n[02] latch")
    req = build_author_request("worker", "propose", propose_task("a rugged military supply crate", index=0, n=1, asset_name="Crate",
                               references=False), budget=EngineBudget("godot_4"), context={}, timeout_s=90, asset_name="Crate")
    texts = {
        # the "Budget: ..." line is the budget's dump, which the test environment's settings change; it carries no colour
        "propose_text": "\n".join(line for line in req.contents[0].parts[0].text.splitlines() if not line.startswith("Budget: ")),
        "propose_schema": req.schema,
        "improve_text": improve_task(scene, brief="a rugged military supply crate", views=views, manifest="m", triangles=24),
        "repair_text": repair_task(scene, reasons=["x"], located={}, manifest="m"),
        "look_text": L.look_task(scene, brief="a rugged military supply crate", verdict=("score_brief_look 2.00: x",)),
        "blockout_stage": A._system("BLOCKOUT"), "lookdev_stage": A._system("LOOKDEV"),
        "patch_schema": S.patch_response_schema(), "look_schema": S.look_response_schema(),
        "correction_schema": S.correction_response_schema(), "apply_scene_ir_tool": S.apply_scene_ir_parameters(),
        "apply_shader_diff_tool": S.apply_shader_diff_parameters(), "rules_text": S.ir_rules_text(),
    }
    return {k: hashlib.sha256((v if isinstance(v, str) else json.dumps(v, sort_keys=True)).encode()).hexdigest()
            for k, v in texts.items()}


def _switched_env(**env: str) -> dict[str, str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("AGY3D_COLOUR") and k != "AGY3D_STATE_METALLIC"}
    return base | env


def test_as_written_sends_byte_for_byte_what_c8565a7_sent():
    """The rule "as_written" is the behaviour before this change: a process started with it sends what c8565a7 sent."""
    code = "import sys, json; sys.path.insert(0, 'tests'); import test_colour as T; print(json.dumps(T._sent()))"
    run = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=300,
                         cwd=Path(__file__).parent.parent, env=_switched_env(AGY3D_COLOUR_RULE="as_written"))
    assert run.returncode == 0, run.stderr[-2000:]
    assert json.loads(run.stdout.strip().splitlines()[-1]) == C8565A7


def test_the_default_is_the_measured_rule_and_the_metallic_sentence_is_off():
    assert S.COLOUR_RULE == "srgb" and S.told() and S.STATE_METALLIC is False
    assert S.COLOUR_CLAUSE in S.IR_SEMANTICS and S.METAL_CLAUSE not in S.IR_SEMANTICS
    scene = S.parse_scene((DATA / "crate_p1_2026-11-22.json").read_text())
    assert next(m for m in scene.materials if m.id == "mat_crate_body").base_color == LIN([0.16, 0.21, 0.12], rule="srgb")
    assert _sent() != C8565A7  # the request does change under it: the clause, the shown form, "#rrggbb"


def test_an_edit_of_a_package_made_before_shows_its_stored_colours_as_they_are_and_converts_only_what_it_writes():
    """A package made before this change holds linear colours (a pale olive 0.18, 0.22, 0.14 shipped as #768169). An edit
    shows the author those stored values in the written form, i.e. the pale colour it is, and converts only the colours
    its patch writes; every material it does not write keeps its stored value."""
    from antigravity3d.tactics.metered_author import DesignViews

    old = ir.SceneIR(asset_name="Crate", nodes=[ir.NodeIR(id="body", kind="box", params={"size": [1, 1, 1]}, material_id="paint"),
                                                ir.NodeIR(id="grip", kind="box", params={"size": [0.1, 0.1, 0.1]}, material_id="rubber")],
                     materials=[ir.MaterialIR(id="paint", base_color=(0.18, 0.22, 0.14)), ir.MaterialIR(id="rubber", base_color=(0.05, 0.05, 0.05))])
    views = DesignViews(images=(("front view of the current design (Image 1)", "https://x/v1"),), legend="[01] body")
    text = improve_task(old, brief="a crate", views=views, manifest="", triangles=24, instruction="make the paint darker")
    assert f'"base_color":{json.dumps(S.written_colour((0.18, 0.22, 0.14)), separators=(",", ":"))}' in text  # [0.4614,0.5064,0.41]
    patch = S.parse_patch(json.dumps({"ops": [{"op": "upsert_material", "material": {"id": "paint", "base_color": OLIVE}}]}))
    edited = ir.apply_patch(old, patch)
    assert edited.materials[0].base_color == LIN(OLIVE) and edited.materials[1].base_color == (0.05, 0.05, 0.05)


# --------------------------------------------------------------------------- the candidates


def test_the_candidates_are_what_they_say():
    assert LIN([0, 0.5, 1], rule="srgb") == (0.0, pytest.approx(0.21404, abs=1e-5), 1.0)
    assert LIN([0.25, 0.5, 1], rule="gamma") == pytest.approx(tuple(c ** S.COLOUR_GAMMA for c in (0.25, 0.5, 1)))
    assert LIN("#2e3824", rule="srgb") == pytest.approx(LIN([0x2e / 255, 0x38 / 255, 0x24 / 255], rule="srgb"))
    rubber = LIN([0.05, 0.05, 0.05], rule="srgb_albedo")  # sRGB 13: below the albedo floor, lifted to sRGB 50
    assert S._luminance(rubber) == pytest.approx(S.ALBEDO_LUMINANCE[0])
    assert LIN(OLIVE, rule="srgb_albedo") == LIN(OLIVE, rule="srgb")  # inside the range: the exact conversion
    red, exact = LIN([0.65, 0.08, 0.06], rule="srgb_albedo"), LIN([0.65, 0.08, 0.06], rule="srgb")
    assert red[1] / red[0] == pytest.approx(exact[1] / exact[0])  # hue kept
    steel = LIN([0.12, 0.13, 0.14], rule="srgb_albedo", metallic=1.0)  # "metal" this dark is a coating: floored to F0 0.5
    assert S._luminance(steel) == pytest.approx(S.METAL_LUMINANCE_MIN, rel=1e-6)
    assert LIN([1, 0.8, 0.5], field="emissive_color", rule="srgb_albedo") == LIN([1, 0.8, 0.5], rule="srgb")  # base colour only
    for r in S.COLOUR_RULES:  # a hex is three numbers under every rule; under "as_written" its own values over 255
        assert len(LIN("#2e3824", rule=r)) == 3 and all(isinstance(c, float) for c in LIN("#2e3824", rule=r))
    assert LIN("#2e3824", rule="as_written") == (0x2e / 255, 0x38 / 255, 0x24 / 255)


# --------------------------------------------------------------------------- once, never twice, never on stored data


def test_a_written_answer_is_converted_once_and_shown_back_as_written(rule, conversions):
    scene = S.parse_scene(json.dumps(ANSWER))
    assert len(conversions) == 3  # base_color twice, emissive_color once: each written colour, once
    paint, lamp = scene.materials
    assert paint.base_color == LIN(OLIVE, metallic=0) and lamp.emissive_color == LIN([1, 0.8, 0.5], field="emissive_color")
    assert lamp.specular_color == (1.0, 1.0, 1.0) and lamp.sheen_color == (0.0, 0.0, 0.0)  # unwritten: the format's default
    shown = S.as_written(scene)
    assert shown["materials"][0]["base_color"] == OLIVE  # the round trip: model text -> scene -> the text shown back
    again = S.parse_scene(json.dumps(shown))
    assert again.materials[0] == paint and again.nodes == scene.nodes  # read again: the same scene


def test_every_path_from_an_answer_converts_once(monkeypatch, conversions):
    monkeypatch.setattr(S, "COLOUR_RULE", "srgb")  # a rule under which converting twice would also change the value
    patch = S.parse_patch(json.dumps({"ops": [{"op": "upsert_material", "material": {"id": "paint", "base_color": OLIVE}}]}))
    assert patch.ops[0].material.base_color == LIN(OLIVE) and len(conversions) == 1
    conversions.clear()
    tool = S.parse_ops([{"op": "upsert_material", "material": {"id": "paint", "base_color": OLIVE}}])  # native session
    assert tool.ops[0].material.base_color == LIN(OLIVE) and len(conversions) == 1
    conversions.clear()
    tool_scene = S.parse_tool_scene("Crate", [NODE], [{"id": "paint", "base_color": OLIVE}])
    assert tool_scene.materials[0].base_color == LIN(OLIVE) and len(conversions) == 1
    conversions.clear()
    stored = ir.MaterialIR(id="paint", base_color=(0.5, 0.5, 0.5), roughness=0.6)
    diffed = S.apply_material_diff(stored, {"base_color": OLIVE})  # apply_shader_diff
    assert diffed.base_color == LIN(OLIVE) and diffed.roughness == 0.6 and len(conversions) == 1
    conversions.clear()
    scene = ir.SceneIR(asset_name="Crate", nodes=[ir.NodeIR(**{**NODE, "transform": ir.Transform(translate=(0, 0, 0.5))})],
                       materials=[stored])
    look = S.apply_look(scene, json.dumps({"ops": [{"op": "upsert_material", "material": {"id": "paint", "base_color": OLIVE}}]}))
    assert look.ops[0].material.base_color == LIN(OLIVE) and len(conversions) == 1
    conversions.clear()
    # a correction: the failed answer's material is merged as written and the whole is read once
    failed = {**ANSWER, "nodes": [{**NODE, "kind": "torus"}]}
    fixed = S.apply_correction(failed, json.dumps({"nodes": [NODE], "materials": [{"id": "lamp", "base_color": [0.5, 0.5, 0.5]}],
                                                   "remove": []}))
    assert fixed.materials[0].base_color == LIN(OLIVE)  # from the failed answer: once, not twice
    assert fixed.materials[1].base_color == LIN([0.5, 0.5, 0.5]) and len(conversions) == 2  # the replaced lamp has no emissive


def test_a_stored_scene_is_never_converted(rule, conversions, tmp_path):
    from antigravity3d.kernel.usd_stage import StageManager

    stored = ir.SceneIR(asset_name="Crate", nodes=[ir.NodeIR(id="body", kind="box", params={"size": [1, 1, 1]}, material_id="paint")],
                        materials=[ir.MaterialIR(id="paint", base_color=(0.18, 0.22, 0.14))])
    patched = ir.apply_patch(stored, ir.IRPatch(ops=[ir.UpsertMaterial(material=ir.MaterialIR(id="paint", base_color=(0.2, 0.2, 0.2)))]))
    assert patched.materials[0].base_color == (0.2, 0.2, 0.2)
    sm = StageManager.create(tmp_path / "Crate_stage.usda", "Crate")
    sm.apply_ir(stored)
    assert sm.base_ir().materials[0].base_color == pytest.approx((0.18, 0.22, 0.14))
    assert conversions == []


def test_a_rerun_reads_the_recorded_design_without_converting_it_again(tmp_path, monkeypatch, conversions):
    """The design record stores the parsed scene (tactics.cached.DesignRecord.put), not the model's text: a rerun in
    the same folder gets the scene the first run made, colours as they were converted then, and converts nothing."""
    from antigravity3d.kernel.cache import BuildCache, LocalBlobStore
    from antigravity3d.tactics.cached import DesignRecord

    monkeypatch.setattr(S, "COLOUR_RULE", "srgb")
    scene = S.parse_scene(json.dumps(ANSWER))
    conversions.clear()
    record = DesignRecord(BuildCache(LocalBlobStore(tmp_path / "cache")))
    record.put("design-x", scene, {"unit": "tokens"})
    assert record.get("design-x").answer == scene and conversions == []


def test_what_a_model_is_shown_follows_the_rule(rule):
    scene = S.parse_scene(json.dumps(ANSWER))
    for text in (repair_task(scene, reasons=["x"], located={}, manifest=""),
                 improve_task(scene, brief="a crate", views=_views(), manifest="", triangles=12)):
        assert f'"base_color":{json.dumps(OLIVE, separators=(",", ":"))}' in text.replace(".0,", ",").replace(", ", ","), text[-400:]
    table = material_table(scene)
    assert (f"base_color {OLIVE} (#2e3824)" in table) if S.told() else ("base_color [0.18, 0.22, 0.14] (sRGB #768169)" in table)


def _views():
    from antigravity3d.tactics.metered_author import DesignViews

    return DesignViews(images=(("front view of the current design (Image 1)", "https://x/v1"),), legend="[01] body")


def test_the_live_answer_written_in_hex_after_the_clause_parses_under_the_default():
    """Live 2026-11-22, worker tier, told the colours are sRGB: the chair's author wrote hex (r5/scratch_colour/live1)."""
    raw = json.loads((DATA / "chair_srgb_hex_2026-11-22.json").read_text())
    scene = S.parse_scene(json.dumps(raw))
    written = {m["id"]: m["base_color"] for m in raw["materials"]}
    for m in scene.materials:
        assert m.base_color == LIN(written[m.id], metallic=m.metallic)
    oak = next(m for m in scene.materials if m.id == "oak_seat_wood")
    assert written["oak_seat_wood"] == "#8b5a2b" and oak.base_color == pytest.approx((0.2582, 0.1022, 0.0242), abs=1e-4)


def test_the_recorded_live_answers_parse_under_every_rule(rule):
    for name, material, written in (("crate_p1_2026-11-22.json", "mat_crate_body", [0.16, 0.21, 0.12]),
                                    ("lamp_p2_2026-11-22.json", "mat_brass", [0.82, 0.62, 0.24])):
        scene = S.parse_scene((DATA / name).read_text())
        m = next(m for m in scene.materials if m.id == material)
        assert m.base_color == LIN(written, metallic=m.metallic)
        if rule != "srgb_albedo":  # a lifted colour shows as lifted
            assert S.as_written(scene)["materials"][[x.id for x in scene.materials].index(material)]["base_color"] == written


# --------------------------------------------------------------------------- what a switched process is told


SWITCHED = textwrap.dedent('''
    import json
    from antigravity3d import ir, ir_schema as S
    from antigravity3d.harness import agents as A
    from antigravity3d.tactics.look_round import LOOK_ASK, look_task
    told, metal = S.told(), S.STATE_METALLIC
    rules = S.ir_rules_text()
    assert (S.COLOUR_CLAUSE in S.IR_SEMANTICS) is told and ("colours are linear RGB 0-1" in S.IR_SEMANTICS) is (not told)
    assert (S.METAL_CLAUSE in S.IR_SEMANTICS) is metal and ("paint on metal included" in LOOK_ASK) is metal
    assert ("linear value looks much lighter" in LOOK_ASK) is (not told)
    for stage in ("BLOCKOUT", "LOOKDEV"):
        assert (S.COLOUR_CLAUSE in A._system(stage)) is told
    hexed = {"asset_name": "A", "materials": [{"id": "m", "base_color": "#2e3824"}],
             "nodes": [{"id": "b", "kind": "box", "params": {"size": [1, 1, 1]}, "transform": {"translate": [0, 0, 0.5]}}]}
    try:
        S.WireScene.model_validate(hexed)  # "#rrggbb" is offered (typed in the wire model) only under a told rule
        offered = True
    except ValueError:
        offered = False
    assert offered is told
    if told:
        assert rules.count("sRGB") == 1 and "linear" not in rules.lower()
        scene = S.parse_scene(json.dumps({"asset_name": "A", "materials": [{"id": "m", "base_color": "#2e3824"}],
                                          "nodes": [{"id": "b", "kind": "box", "params": {"size": [1, 1, 1]},
                                                     "transform": {"translate": [0, 0, 0.5]}, "material_id": "m"}]}))
        assert scene.materials[0].base_color == S.linear_colour("#2e3824")
        assert "linear" not in look_task(scene, brief="a crate", verdict=()).lower()
        for bad in ([1.2, 0, 0], [-0.1, 0, 0], "olive", "#12345", "#12345g", [0.1, 0.2]):
            try:
                S.parse_scene(json.dumps({"asset_name": "A", "materials": [{"id": "m", "base_color": bad}], "nodes": []}))
            except ValueError:
                pass
            else:
                raise AssertionError(bad)
            try:
                S.apply_material_diff(ir.MaterialIR(id="m"), {"base_color": bad})
            except S.IRArgumentError:
                pass
            else:
                raise AssertionError(bad)
    print("OK", S.COLOUR_RULE, metal)
''')


@pytest.mark.parametrize("env", [{}, {"AGY3D_COLOUR_RULE": "gamma", "AGY3D_STATE_METALLIC": "1"},
                                 {"AGY3D_COLOUR_RULE": "as_written", "AGY3D_STATE_METALLIC": "1"}, {"AGY3D_COLOUR_RULE": "as_written"}])
def test_a_process_is_told_its_rule_and_as_written_is_told_nothing_new(env):
    run = subprocess.run([sys.executable, "-c", SWITCHED], capture_output=True, text=True, timeout=300, env=_switched_env(**env))
    assert run.returncode == 0 and run.stdout.startswith("OK"), run.stderr[-2000:]
