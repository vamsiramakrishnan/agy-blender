"""A native session sees its design and gets the command's review (harness tools, the MCP server's image results)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json

import numpy as np
from PIL import Image

from antigravity3d.decisions.fakes import FakeDecisionsClient
from antigravity3d.harness import session as S
from antigravity3d.harness import tools as T
from antigravity3d.kernel import som
from antigravity3d.runtime import mcp_server
from test_harness_tools import CRATE, box, make_session


def _yellow(img: Image.Image) -> int:
    a = np.asarray(img.convert("RGB")).reshape(-1, 3).astype(int)
    return int(((a[:, 0] == som.LEADER[0]) & (a[:, 1] == som.LEADER[1]) & (a[:, 2] == som.LEADER[2])).sum())


async def test_render_preview_shows_five_badged_views_and_the_mcp_result_carries_them(tmp_path):
    env = make_session(tmp_path)
    parts = [box("body", (1.0, 0.8, 0.6), 0.3), box("lid", (1.05, 0.85, 0.08), 0.64),
             box("bolt", (0.02, 0.02, 0.02), 0.69)]  # a small part: its badge sits on a yellow leader line
    await T.apply_scene_ir(json.dumps({"ops": parts}), env.ctx)
    r = await T.render_preview(env.ctx)
    assert [n for n, _ in r.images] == ["front", "right", "top", "persp", "persp_rear"]
    assert "[03] lid" in r and "Legend" in r and r.count("(attached)") + r.count("view_file") == 5
    blocks = mcp_server._with_images(r)
    assert blocks[0].type == "text" and blocks[0].text == str(r) and len(blocks) == 6
    for (name, path), block in zip(r.images, blocks[1:]):
        data = base64.b64decode(block.data)
        assert block.mime_type == ("image/webp" if path.suffix == ".webp" else "image/png") and data == path.read_bytes()
        assert Image.open(io.BytesIO(data)).size == (512, 512)
        if len(data) > T.INLINE_IMAGE_BYTES:
            assert f"{path}  <- too large to show inline: open it with view_file" in r
    assert sum(_yellow(Image.open(p)) for _, p in r.images) > 0  # the leader lines keep their colour
    assert mcp_server._with_images("OK tx=1") == "OK tx=1"  # every other reply as it was


def test_agent_image_keeps_the_badge_colours_and_fits_the_inline_limit_when_it_can():
    from antigravity3d.tactics.judging import render_design
    from antigravity3d.kernel.types import MeshData
    import trimesh

    def md(tm, nid, t):
        tm = tm.copy()
        tm.apply_translation(t)
        return MeshData(nid, f"/A/{nid}", np.array(tm.vertices, float), np.array(tm.faces, np.int32), "m")

    ms = [md(trimesh.creation.box((1, 0.8, 0.6)), "body", (0, 0, 0.3)), md(trimesh.creation.box((0.02, 0.02, 0.02)), "pin", (0.3, 0.2, 0.61))]
    for name, png in render_design(ms).marked:
        data, ext = T._agent_image(png)
        assert len(data) <= T.INLINE_IMAGE_BYTES and ext in ("png", "webp")
        assert _yellow(Image.open(io.BytesIO(png))) == _yellow(Image.open(io.BytesIO(data)))


# any_target_can_be_verified: this test is about what the review asks and names, not the target; a session that must be verified for unreal_5 (the default) refuses
# run_visual_qa and qa_probe on this machine (Session.unverifiable_target), which tests/test_verifiable_targets.py covers on its own.
async def test_blockout_review_is_the_commands_and_names_the_parts(tmp_path, any_target_can_be_verified):
    env = make_session(tmp_path, client=FakeDecisionsClient({"defect_clipping": 0.9, "defect_clipping__2": 0.85}))
    await T.apply_scene_ir(json.dumps(CRATE), env.ctx)
    r = await T.run_visual_qa(env.ctx)
    asked = [c for c in env.client.calls if "defect_clipping" in c["questions"]][0]
    assert asked["n_images"] == 5 and {b["resolution"] for b in asked["input"] if b["type"] == "image"} == {"high"}
    assert ("finding clipping p=0.90: [01] body (front, right, persp, persp_rear) p=0.90; "
            "[02] lid (front, right, top, persp, persp_rear) p=0.85") in r  # the parts, best first, with their views
    p = await T.qa_probe(env.ctx)  # read-only: the same review on the renders already made, no new render
    assert p.startswith("PROBE ") and "finding clipping" in p and len(env.sess().previews) == 1


async def test_a_preview_without_images_says_what_to_do_instead_of_failing(tmp_path):
    env = make_session(tmp_path, stage="LOOKDEV")
    S.switch_to_preview(env.reg, env.sid, 400_000, 0)
    await T.apply_scene_ir(json.dumps(CRATE), env.ctx)
    for r in (await T.forge_pbr_material("wood", "oiled oak", env.ctx), await T.run_paintover_loop("warmer", env.ctx)):
        assert r.startswith("NO IMAGES: this preview's image limit is 0") and "generated from the geometry" in r


# ---- a preview judged by an uncalibrated judge delivers on review (the command's rule, antigravity3d.delivery) -----

import importlib
import sys
from pathlib import Path

import pytest

from antigravity3d.runtime import qa_state
from antigravity3d.state import VisualVerdict


def _verdict(state, action="agent_repair", backend="uncalibrated", stage="lookdev"):
    return VisualVerdict(state.revision, state.budget.target_engine, stage, "clay", backend, action)


def _plugin_common():
    scripts = Path(__file__).resolve().parents[1] / "plugin" / "antigravity-3d" / "hooks" / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        return importlib.import_module("_common")
    finally:
        sys.path.pop(0)


@pytest.mark.parametrize(("action", "backend", "preview", "allowed"), [
    ("agent_repair", "uncalibrated", True, True),     # tonight's live scores (about 2.6): a finding, delivered
    ("escalate_director", "uncalibrated", True, True),
    ("escalate_human", "uncalibrated", True, False),  # a person must decide
    ("agent_repair", "decisions", True, False),       # a calibrated judge's gates decide, as before
    ("approve", "decisions", True, False),            # ... and 2.6 is below its 3.4
    ("agent_repair", "uncalibrated", False, False),   # never outside a preview
])
def test_preview_export_gate_follows_the_delivery_rule(tmp_path, action, backend, preview, allowed):
    env = make_session(tmp_path, stage="LOOKDEV")
    st = env.state
    st.preview, st.last_tier1_ok, st.last_tier15_score = preview, True, 2.6
    st.verdict = _verdict(st, action, backend)
    assert st.export_allowed is allowed
    assert (T.export_block_reason(st) is None) is allowed
    rec = qa_state.QAState(True, 2.6, "s", "c", "a", st.revision, st.budget.target_engine, st.verdict, None, preview=preview)
    assert rec.export_allowed is allowed
    # the plugin's PreToolUse hook reads the same record and decides the same
    write = dict(tier1_ok=True, tier15_score=2.6, session_id="s1", conversation_id="conv", asset="a", revision=st.revision,
                 target=st.budget.target_engine, verdict=st.verdict, workspace=tmp_path, preview=preview)
    qa_state.write_qa_state(**write)
    ok, why = _plugin_common().export_gate(tmp_path / ".agy3d", "conv", "s1")
    assert ok is allowed, why


def test_a_stale_review_or_a_hard_policy_flag_still_blocks(tmp_path):
    env = make_session(tmp_path, stage="LOOKDEV")
    st = env.state
    st.preview, st.last_tier1_ok, st.last_tier15_score = True, True, 2.6
    st.verdict = VisualVerdict("an-older-revision", st.budget.target_engine, "lookdev", "clay", "uncalibrated", "agent_repair")
    assert "a lookdev/final visual QA review of the current revision" in T.export_block_reason(st)
    st.verdict = _verdict(st)
    plane = env.sess().plane
    plane.preview, plane.hard_policy_flags = True, ["policy_unsafe_content"]
    assert "hard policy flag(s) policy_unsafe_content" in T.export_block_reason(st, plane)


async def test_uncalibrated_preview_exports_with_its_findings(tmp_path):
    pytest.importorskip("antigravity3d.delivery")  # finish's module (merged with this change)
    env = make_session(tmp_path, client=FakeDecisionsClient(uncalibrated=True, default_score=2.6), stage="BLOCKOUT")
    S.switch_to_preview(env.reg, env.sid, 400_000, 0)
    await T.apply_scene_ir(json.dumps(CRATE), env.ctx)
    S.set_toolset(env.reg, env.sid, "LOOKDEV")
    env.state.active_stage = "LOOKDEV"
    assert (await T.run_visual_qa(env.ctx)).startswith("QA[lookdev]")
    S.set_toolset(env.reg, env.sid, "EXPORT")
    env.state.active_stage = "EXPORT"
    r = await T.export_engine_package(env.ctx, "crate")
    if r.startswith("REFUSED"):  # the offline engine import is not part of this rule: say which check stopped it
        assert "export blocked" not in r, r
        return
    assert r.startswith("EXPORTED PREVIEW crate") and "finding: silhouette 2.60, below the target 3.3" in r
    manifest = json.loads((env.sess().export_dir / "crate" / "manifest.json").read_text())
    assert manifest["review"]["status"] == "below_target" and manifest["review"]["findings"] and manifest["verified"] is False


# ---- surfaces are said at the moment they matter; a call's maps are screened together --------------------------

async def test_a_session_without_images_and_lookdev_say_what_happens_to_surfaces(tmp_path):
    from test_mcp_server import call, client

    async with client(tmp_path / "on", env={"AGY3D_GENERATED_SURFACES": "1"}) as s:  # the suite turns them off
        r = await call(s, "open_asset_session", asset_name="Crate", target_engine="godot_4", preview=True,
                       token_ceiling=400_000, max_images=0)
        assert "NO IMAGES in this session (image limit 0). SURFACES:" in r and "neither needed nor wanted" in r
        r = await call(s, "set_stage", stage="LOOKDEV")
        assert r.startswith("STAGE LOOKDEV callable:") and "SURFACES:" in r
        assert "SURFACES" not in await call(s, "set_stage", stage="BLOCKOUT")
        r = await call(s, "open_asset_session", asset_name="Crate", target_engine="godot_4", preview=True,
                       token_ceiling=400_000, max_images=4)
        assert "SURFACES" not in r  # images can be made: said at LOOKDEV only
    async with client(tmp_path / "off") as s:  # generated surfaces off: nothing is promised
        r = await call(s, "open_asset_session", asset_name="Crate", target_engine="godot_4", preview=True,
                       token_ceiling=400_000, max_images=0)
        assert "SURFACES" not in r and "SURFACES" not in await call(s, "set_stage", stage="LOOKDEV")


async def test_a_calls_maps_are_screened_together(tmp_path):
    from antigravity3d.harness.hooks import InvariantAndSafetyGate

    env = make_session(tmp_path, stage="LOOKDEV")
    await T.apply_scene_ir(json.dumps(CRATE), env.ctx)
    paths = []
    for k in range(3):
        p = tmp_path / f"map{k}.png"
        Image.new("RGB", (8, 8), (k * 40, 90, 120)).save(p)
        paths.append(str(p))
    gate = InvariantAndSafetyGate(env.reg, env.sid)
    running = peak = 0
    plane = env.sess().plane

    async def slow_policy(uri, *, governor=None):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
        from antigravity3d.decisions.models import PolicyVerdict
        return PolicyVerdict(True, False, {})

    plane.policy_gate = slow_policy
    v = await gate.decide("apply_shader_diff", {"material_id": "wood", "diff": {"albedo_uri": paths[0], "orm_uri": paths[1],
                                                                              "normal_uri": paths[2]}})
    assert v.allow and peak == 3


async def test_lookdev_renders_the_package_with_its_generated_surfaces(tmp_path):
    """With generated surfaces on and an image-less opaque material, render_lookdev renders the package the export will
    write (its shared atlas carries the painted surfaces), not the stage's flat colours."""
    from dataclasses import replace

    seen: list[Path] = []

    class Pool:
        fake = False

        async def call(self, op, *, path, **kw):
            p = Path(path)
            assert p.is_file()
            seen.append((p, (p.parent / "manifest.json").is_file() and any((p.parent / "textures").glob("*albedo*"))))
            return {"image": np.zeros((512, 512, 4), np.float32) + 0.5, "fake": False, "engine": kw.get("engine")}

    env = make_session(tmp_path, stage="LOOKDEV", pool=Pool())
    crate = {"nodes": [dict(o["node"], material_id="paint") for o in CRATE["ops"]],
             "materials": [{"id": "paint", "base_color": [0.3, 0.4, 0.25], "surface": "olive drab painted steel"}]}
    await T.apply_scene_ir(crate, env.ctx)
    env.state.budget = replace(env.state.budget, generated_surfaces=False)
    r = await T.render_lookdev(env.ctx, "eevee")
    assert "generated surfaces" not in r and seen[-1][0].suffix == ".usdc" and not seen[-1][1]
    env.state.budget = replace(env.state.budget, generated_surfaces=True)
    r = await T.render_lookdev(env.ctx, "eevee")
    assert "generated surfaces shown" in r
    assert seen[-1][1]  # rendered from a written package with its textures


async def test_deliver_package_goes_from_a_design_to_a_package_in_one_call(tmp_path):
    pytest.importorskip("antigravity3d.delivery")
    env = make_session(tmp_path, client=FakeDecisionsClient(uncalibrated=True, default_score=2.6), stage="BLOCKOUT")
    S.switch_to_preview(env.reg, env.sid, 400_000, 0)
    assert (await T.deliver_package(env.ctx)).startswith("ERROR")  # nothing to deliver yet
    await T.apply_scene_ir(json.dumps(CRATE), env.ctx)
    r = await T.deliver_package(env.ctx, "crate")
    lines = r.split("\n")
    # the outcome first, then the findings for the person (no gate verdict: r5/live_native5 read "agent_repair" as an
    # order), the steps, and the line that ends the run
    assert lines[0].startswith("DELIVERED: EXPORTED PREVIEW crate"), r
    assert lines[1].startswith("finding: ") and any(x.startswith("finding: silhouette 2.60") for x in lines)
    assert any(x.startswith("LODS ") for x in lines) and any(x.startswith("USD ") for x in lines)
    assert lines[-1].startswith("FINISHED: the package at ") and "do not inspect" in lines[-1]
    assert "agent_repair" not in r and "gates win" not in r
    assert env.sess().toolset == "EXPORT" and (env.sess().export_dir / "crate" / "manifest.json").is_file()
    again = await T.deliver_package(env.ctx, "crate2")  # the review of this revision is reused, not asked again
    assert again.startswith("DELIVERED: EXPORTED PREVIEW crate2")


def test_the_short_path_says_how_big_a_call_is_and_why_not_to_script_blender():
    """r5/live_native3: told to build in as few calls as it takes, the agent sent the whole crate in one reply, which was
    cut off at the output limit before any tool call; it then scripted Blender in files and never used the studio."""
    agents_md = (Path(__file__).resolve().parents[1] / "plugin" / "antigravity-3d" / "rules" / "AGENTS.md").read_text()
    for text in (T.SHORT_PATH, mcp_server.INSTRUCTIONS, " ".join(agents_md.split())):
        assert "at most 25 operations" in text and "16,384" in text and "split and sent again" in text
        assert "not by scripting Blender" in text and "as few calls as it takes" not in text
        assert "DELIVERED" in text and "do not inspect it again" in text and "NOT DELIVERED: a person must decide" in text


def _named_tools(text: str) -> set[str]:
    import re

    from antigravity3d.runtime import inspector_server

    known = set(T.ALL_TOOLS) | set(mcp_server.MANAGEMENT_TOOLS) | set(inspector_server.TOOL_NAMES)
    return {w for w in re.findall(r"[a-z_]+", text) if w in known}


def _bundle_text(rel: str) -> str:
    return (Path(__file__).resolve().parents[1] / "plugin" / "antigravity-3d" / rel).read_text()


async def test_every_tool_the_instructions_name_is_listed_before_a_session_exists(tmp_path):
    """r5/live_native4: the agent listed the server's tools before opening a session, found none of the tools the
    instructions named (the catalogue showed them only after open_asset_session) and scripted Blender instead. A scripted
    model cannot catch this (it never looks): the catalogue a fresh connection gets must hold every tool the texts name."""
    from test_mcp_server import call, client

    agents = _bundle_text("rules/AGENTS.md")
    skill = _bundle_text("skills/parametric-kitbash/SKILL.md")
    new_asset = {"server instructions": mcp_server.INSTRUCTIONS, "SHORT_PATH": T.SHORT_PATH,
                 "AGENTS.md short path": agents[agents.index("## The short path"):agents.index("## How the tools reach you")],
                 "kitbash steps": skill[skill.index("## Steps"):skill.index("## Changing an asset that already exists")]}
    edit = skill[skill.index("## Changing an asset that already exists"):skill.index("## The same loop")]
    async with client(tmp_path) as s:
        fresh = {t.name for t in (await s.list_tools()).tools}
        for label, text in new_asset.items():
            assert _named_tools(text) <= fresh, (label, _named_tools(text) - fresh)
        assert {"apply_scene_ir", "render_preview", "deliver_package"} <= fresh
        for name in ("apply_scene_ir", "render_preview", "deliver_package", "inspect_manifest", "run_visual_qa"):
            r = await call(s, name, **({"ops": []} if name == "apply_scene_ir" else {}))
            assert "no asset session: call open_asset_session first" in r, (name, r)
    # the edit flow: open_existing_asset puts the session in TWEAK; what its texts name is listed then
    host = mcp_server.StudioHost()
    tweak = set(mcp_server.MANAGEMENT_TOOLS) | {f.__name__ for f in (*T.STAGE_TOOLS["TWEAK"], *T.SHORT_PATH_TOOLS)}
    assert _named_tools(edit) <= tweak, _named_tools(edit) - tweak
    assert host.visible_tools() >= {f.__name__ for f in T.SHORT_PATH_TOOLS}


async def test_a_refused_delivery_says_so_first_and_what_to_fix(tmp_path, monkeypatch):
    pytest.importorskip("antigravity3d.delivery")
    env = make_session(tmp_path, client=FakeDecisionsClient(uncalibrated=True, default_score=2.6), stage="BLOCKOUT")
    S.switch_to_preview(env.reg, env.sid, 400_000, 0)
    await T.apply_scene_ir(json.dumps(CRATE), env.ctx)

    async def refuse(ctx, out_name):
        return "REFUSED blender_asset_qa: 1 error(s): floating/detached part in Batch_0"

    monkeypatch.setattr(T, "export_engine_package", refuse)
    r = await T.deliver_package(env.ctx, "crate")
    assert r.startswith("NOT DELIVERED: blender_asset_qa: 1 error(s)") and "call deliver_package again" in r.split("\n")[0]
    assert "FINISHED" not in r and "agent_repair" not in r and "a person must decide" not in r


# any_target_can_be_verified: this test is about what the review is told about the render, not the target; a session that must be verified for unreal_5 (the default) refuses
# run_visual_qa on this machine (Session.unverifiable_target), which tests/test_verifiable_targets.py covers on its own.
async def test_a_review_of_a_render_with_placed_stencils_states_them_and_a_clay_one_does_not(tmp_path, any_target_can_be_verified):
    """r5/live_native6: the lookdev render showed the generated surfaces' stencils, the review (not told they were painted
    on purpose) escalated to a person for IP/text risk and the export was refused."""
    env = make_session(tmp_path, stage="LOOKDEV")
    await T.apply_scene_ir(json.dumps(CRATE), env.ctx)
    s = env.sess()
    for placed in (("FRAGILE", "THIS SIDE UP"), ()):
        s.add_preview(_png(tmp_path / f"r{len(placed)}.png"), f"https://x/r{len(placed)}.png", kind="eevee",
                      texture_hash=s.texture_digest(), placed=placed)
        await T.run_visual_qa(env.ctx)
        req = [c for c in env.client.calls if "defect_cg_plastic" in c["questions"]][-1]
        texts = " ".join(b.get("text", "") for b in req["input"])
        asked = req["questions"]["defect_ip_or_text"]["instructions"]
        if placed:
            assert "FRAGILE" in texts and "THIS SIDE UP" in texts and asked.startswith(Q_BEYOND_HEAD, asked.index("Apart"))
        else:
            assert "FRAGILE" not in texts and "Apart from the stencil markings" not in asked


Q_BEYOND_HEAD = "Apart from the stencil markings listed in the input as painted on purpose"


def _png(path):
    Image.new("RGB", (32, 32), (120, 110, 90)).save(path)
    return path


async def test_a_refusal_for_a_person_says_so_first_and_ends_the_work(tmp_path):
    pytest.importorskip("antigravity3d.delivery")
    env = make_session(tmp_path, client=FakeDecisionsClient({"defect_ip_or_text": 0.95}, uncalibrated=True,
                                                             default_score=2.6), stage="BLOCKOUT")
    S.switch_to_preview(env.reg, env.sid, 400_000, 0)
    await T.apply_scene_ir(json.dumps(CRATE), env.ctx)
    r = await T.deliver_package(env.ctx, "crate")
    first = r.split("\n")[0]
    assert first.startswith("NOT DELIVERED: a person must decide (") and "stop" in first and "not wanted" in first, r
    assert "FINISHED" not in r and not (env.sess().export_dir / "crate").exists()


async def test_a_design_whose_only_text_is_the_placed_stencils_is_delivered(tmp_path, monkeypatch):
    """With the stand-in judge answering the plain text question yes and the 'apart from the placed markings' question
    no (as the live judge does), the placed stencils no longer stop the export."""
    pytest.importorskip("antigravity3d.delivery")

    def responder(req):
        q = req["questions"].get("defect_ip_or_text")
        return {"defect_ip_or_text": 0.05 if q and "Apart from the stencil markings" in q["instructions"] else 0.95}

    env = make_session(tmp_path, client=FakeDecisionsClient(responder=responder, uncalibrated=True, default_score=2.6),
                       stage="BLOCKOUT")
    S.switch_to_preview(env.reg, env.sid, 400_000, 0)
    await T.apply_scene_ir(json.dumps(CRATE), env.ctx)
    real = T.render_lookdev

    async def lookdev_with_stencils(ctx, engine="eevee"):  # the package's placed strings, as render_lookdev records them
        s = T.resolve(ctx)
        s.add_preview(_png(tmp_path / "ld.png"), "https://x/ld.png", kind="eevee", texture_hash=s.texture_digest(),
                      placed=("FRAGILE",))
        env.reg.attach(env.sid, S.R_POOL, None)  # no Blender for the export's own checks offline
        return "LOOKDEV eevee render (stand-in)"

    monkeypatch.setattr(T, "render_lookdev", lookdev_with_stencils)
    # deliver_package renders the lookdev only with a pool. A fake one (its Blender check is skipped, never passed):
    # since native5 the package's Blender check runs before the lookdev render, while this stand-in is attached, and a
    # bare object() made it fail on "'object' object has no attribute 'call'".
    from types import SimpleNamespace

    env.reg.attach(env.sid, S.R_POOL, SimpleNamespace(fake=True))
    try:
        r = await T.deliver_package(env.ctx, "crate")
    finally:
        monkeypatch.setattr(T, "render_lookdev", real)
    assert r.startswith("DELIVERED: EXPORTED PREVIEW crate"), r


# ---- the package goes where the user asked -----------------------------------------------------------------------

def _preview_session(tmp_path):
    env = make_session(tmp_path, client=FakeDecisionsClient(uncalibrated=True, default_score=2.6), stage="BLOCKOUT")
    S.switch_to_preview(env.reg, env.sid, 400_000, 0)
    env.reg.attach(env.sid, S.R_QA_WORKSPACE, tmp_path)  # the workspace the session was opened in
    return env


async def test_deliver_package_places_the_engine_files_in_the_folder_named(tmp_path):
    """r5/live_native7: told only where its package was, the agent copied it into output/ by hand in three calls and
    left the colliders out."""
    pytest.importorskip("antigravity3d.delivery")
    env = _preview_session(tmp_path)
    await T.apply_scene_ir(json.dumps(CRATE), env.ctx)
    out = tmp_path / "output"
    out.mkdir()
    (out / "notes.txt").write_text("mine")
    (out / "Crate.glb").write_bytes(b"old")
    r = await T.deliver_package(env.ctx, "crate_pkg", dest="output")  # the folder's name is not the asset's
    lines = r.split("\n")
    assert lines[0].startswith("DELIVERED: EXPORTED PREVIEW crate_pkg") and lines[0].endswith("placed in output/"), r
    assert lines[1].startswith("placed: Crate.glb (the main file)") and "Crate_colliders.glb" in lines[1]
    assert "replaced files of the same name: Crate.glb" in lines[1]
    assert "nothing is left to copy" in lines[-1]
    assert (out / "notes.txt").read_text() == "mine" and (out / "Crate.glb").read_bytes() != b"old"
    assert (out / "Crate_colliders.glb").is_file() and (out / "manifest.json").is_file()
    assert not list(out.glob("*.usd*")) and not (out / "inspector.html").exists()


@pytest.mark.parametrize("dest", ["../outside", "/tmp/elsewhere", "a/../../x", ".hidden"])
async def test_a_destination_outside_the_workspace_is_refused_in_one_sentence(tmp_path, dest):
    env = _preview_session(tmp_path)
    await T.apply_scene_ir(json.dumps(CRATE), env.ctx)
    r = await T.deliver_package(env.ctx, "Crate", dest=dest)
    assert r.startswith("NOT DELIVERED: destination") and "\n" not in r and "not accepted" in r, r
    assert not (env.sess().export_dir / "Crate").exists()  # nothing was done


async def test_a_destination_through_a_symbolic_link_is_refused(tmp_path):
    env = _preview_session(tmp_path)
    await T.apply_scene_ir(json.dumps(CRATE), env.ctx)
    outside = tmp_path.parent / f"{tmp_path.name}_outside"
    outside.mkdir()
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    r = await T.deliver_package(env.ctx, "Crate", dest="link/output")
    assert r.startswith("NOT DELIVERED: destination") and "symbolic link" in r, r
    assert not list(outside.iterdir())


# ---- a detached part is said when it is made, and refuses delivery before anything is rendered ---------------------

# eight posts on the ground beside the crate, each through a collar: eight interpenetration warnings come first
PINS = [box(f"{part}_{c}", size, 0.1) for c in "abcdefgh" for part, size in (("collar", (0.06, 0.06, 0.05)), ("post", (0.03, 0.03, 0.2)))]
for _k, _pin in enumerate(PINS):
    _pin["node"]["transform"]["translate"][:2] = [-0.45 + 0.13 * (_k // 2), 0.6]
CAP_4MM = box("cap", (0.1, 0.1, 0.02), 0.694)  # lid top at 0.68: the cap's bottom is 4 mm above it
CAP_ON = box("cap", (0.1, 0.1, 0.02), 0.69)


class _RenderPool:
    """A Blender pool stand-in that counts lookdev renders and records the GLB beside the USD it was asked to render."""

    fake = False

    def __init__(self):
        self.renders: list[str] = []

    async def call(self, op, *, path, **kw):
        assert op == "render_usd", op
        glb = Path(path).with_suffix(".glb")  # the package's main GLB, beside its USD
        self.renders.append(hashlib.sha256(glb.read_bytes()).hexdigest() if glb.is_file() else "")
        return {"image": np.zeros((512, 512, 4), np.float32) + 0.5, "fake": False, "engine": kw.get("engine")}


def _fail_fast_session(tmp_path, monkeypatch, blender="passed"):
    from antigravity3d.adapters.blender_5_3 import upstream_checks
    from antigravity3d.state import EngineBudget

    checks: list[str] = []

    async def asset_qa(pool, glb, target_engine):  # the Blender check of the package's GLB, stood in
        checks.append(hashlib.sha256(Path(glb).read_bytes()).hexdigest())
        return blender, "stand-in qa_check" + ("; 1 error(s): floating/detached part in Batch_0" if blender == "failed" else ""), {}

    monkeypatch.setattr(upstream_checks, "asset_qa", asset_qa)
    pool = _RenderPool()
    env = make_session(tmp_path, budget=EngineBudget("godot_4", generated_surfaces=True), pool=pool, stage="BLOCKOUT",
                       client=FakeDecisionsClient(uncalibrated=True, default_score=2.6))
    S.switch_to_preview(env.reg, env.sid, 400_000, 0)
    return env, pool, checks


def _design(*extra):
    return {"nodes": [dict(o["node"], material_id="paint") for o in [*CRATE["ops"], *extra]],
            "materials": [{"id": "paint", "base_color": [0.3, 0.4, 0.25], "surface": "olive drab painted steel"}]}


async def test_apply_scene_ir_names_a_detached_part_with_its_gap_however_many_warnings_come_first(tmp_path):
    """r5/live_native7: the kernel found the hasp staple 4 mm from everything from the fourth edit on, but its token came
    after the interpenetration ones and was cut from the reply; the agent heard of it from the Blender check 77 s into
    deliver_package."""
    env = make_session(tmp_path)
    r = await T.apply_scene_ir({"ops": [*CRATE["ops"], *PINS, CAP_4MM]}, env.ctx)
    assert r.startswith("OK") and "interpenetration@collar_" in r and r.endswith("..."), r  # the warnings are cut
    # the finding ends with the move that fixes it (kernel fast_ops describe, added with the attach step)
    assert ("| detached: cap (surface 4.0 mm from lid, the nearest part, which lies below it (touch tolerance 2 mm); "
            "min z 0.684; to touch it, move cap by (+0.0, +0.0, -4.0) mm, which puts its nearest vertex on lid): "
            "deliver_package will refuse delivery until it touches the asset") in r, r
    assert r.index("detached: cap") < r.index("warn: ")  # never behind the list that is cut at 160 characters
    fixed = await T.apply_scene_ir({"ops": [CAP_ON]}, env.ctx)
    assert fixed.startswith("OK") and "detached" not in fixed, fixed


async def test_a_detached_part_refuses_delivery_with_no_render_and_no_judge_call(tmp_path, monkeypatch):
    pytest.importorskip("antigravity3d.delivery")
    env, pool, checks = _fail_fast_session(tmp_path, monkeypatch)
    await T.apply_scene_ir(_design(CAP_4MM), env.ctx)
    asked = len(env.client.calls)
    r = await T.deliver_package(env.ctx, "crate")
    assert r.startswith("NOT DELIVERED: cap is detached from the rest of the asset (surface 4.0 mm from lid"), r
    assert "call deliver_package again" in r.split("\n")[0] and "a person must decide" not in r
    assert pool.renders == [] and len(env.client.calls) == asked and checks == []  # refused by the kernel's rule alone
    assert not (env.sess().export_dir / "crate").exists()


async def test_a_blender_check_refusal_comes_before_the_render_and_the_review(tmp_path, monkeypatch):
    pytest.importorskip("antigravity3d.delivery")
    env, pool, checks = _fail_fast_session(tmp_path, monkeypatch, blender="failed")
    await T.apply_scene_ir(_design(), env.ctx)
    asked = len(env.client.calls)
    r = await T.deliver_package(env.ctx, "crate")
    assert r.startswith("NOT DELIVERED: blender_asset_qa: stand-in qa_check; 1 error(s): floating/detached part"), r
    assert len(checks) == 1 and pool.renders == [] and len(env.client.calls) == asked
    assert any(x.startswith("LODS ") for x in r.split("\n")) and not (env.sess().export_dir / "crate").exists()
    assert not list(env.sess().artifacts_dir.glob(".preflight-*"))  # the checked package is not left behind


async def test_a_delivered_package_is_the_one_that_was_checked_rendered_and_reviewed(tmp_path, monkeypatch):
    pytest.importorskip("antigravity3d.delivery")
    env, pool, checks = _fail_fast_session(tmp_path, monkeypatch)
    await T.apply_scene_ir(_design(), env.ctx)
    asked = len(env.client.calls)
    r = await T.deliver_package(env.ctx, "crate")
    lines = r.split("\n")
    assert lines[0].startswith("DELIVERED: EXPORTED PREVIEW crate"), r
    assert any(x.startswith("CHECKED before the review: package written and validated, blender_asset_qa passed") for x in lines)
    assert any(x.startswith("finding: ") for x in lines) and len(env.client.calls) > asked  # the review was asked
    shipped = hashlib.sha256((env.sess().export_dir / "crate" / "Crate.glb").read_bytes()).hexdigest()
    # one Blender check and one lookdev render, both of the GLB that shipped; the export records the check it passed
    assert checks == [shipped] and pool.renders == [shipped], (checks, pool.renders, shipped)
    manifest = json.loads((env.sess().export_dir / "crate" / "manifest.json").read_text())
    assert manifest["validation"]["blender_asset_qa"]["status"] == "passed" and manifest["review"]["findings"]
    assert not list(env.sess().artifacts_dir.glob(".preflight-*"))


# ---- a delivery answers inside the client's time, and the next call continues ----------------------------------------

def _slow_review(env, monkeypatch, seconds):
    """The lookdev review's judge answers after ``seconds``; returns the list of its calls."""
    plane, asked = env.sess().plane, []
    real = plane.evaluate_visual_qa

    async def slow(*a, **k):
        asked.append(1)
        await asyncio.sleep(seconds)
        return await real(*a, **k)

    monkeypatch.setattr(plane, "evaluate_visual_qa", slow)
    return asked


async def test_a_slow_review_is_waited_for_by_the_next_call_not_asked_again(tmp_path, monkeypatch):
    """r5/live_final_N: the lookdev review's one judge call took 131 s, the client abandoned the 3-minute call and the
    checked package, the render and the review were lost. Now the call answers in time and the next one continues."""
    pytest.importorskip("antigravity3d.delivery")
    env, pool, checks = _fail_fast_session(tmp_path, monkeypatch)
    await T.apply_scene_ir(_design(), env.ctx)
    monkeypatch.setattr(T, "DELIVER_ANSWER_S", 0.5)
    asked = _slow_review(env, monkeypatch, 2.0)
    first = await T.deliver_package(env.ctx, "crate")
    assert first.startswith("NOT DELIVERED: the review has not answered in") and "nothing needs fixing" in first, first
    assert not (env.sess().export_dir / "crate").exists()  # nothing exported while the review is out
    await asyncio.sleep(2.5)  # the review answers after the call returned
    monkeypatch.setattr(T, "DELIVER_ANSWER_S", 150.0)
    second = await T.deliver_package(env.ctx, "crate")
    assert second.startswith("DELIVERED: EXPORTED PREVIEW crate"), second
    assert len(asked) == 1 and len(pool.renders) == 1 and len(checks) == 1  # nothing done twice


async def test_an_abandoned_delivery_exports_nothing_and_the_next_call_continues(tmp_path, monkeypatch):
    pytest.importorskip("antigravity3d.delivery")
    env, pool, checks = _fail_fast_session(tmp_path, monkeypatch)
    await T.apply_scene_ir(_design(), env.ctx)
    asked = _slow_review(env, monkeypatch, 1.0)
    call = asyncio.ensure_future(T.deliver_package(env.ctx, "crate"))
    while not asked:  # the checks are done, the render made and the review asked
        await asyncio.sleep(0.05)
    call.cancel()  # the client gives up on the call
    with pytest.raises(asyncio.CancelledError):
        await call
    await asyncio.sleep(1.5)
    assert not (env.sess().export_dir / "crate").exists()  # the review went on; nothing was exported
    r = await T.deliver_package(env.ctx, "crate")
    assert r.startswith("DELIVERED: EXPORTED PREVIEW crate"), r
    assert len(asked) == 1 and len(pool.renders) == 1 and len(checks) == 1
