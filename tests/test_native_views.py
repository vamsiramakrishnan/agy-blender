"""A native session sees its design and gets the command's review (harness tools, the MCP server's image results)."""

from __future__ import annotations

import base64
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


async def test_blockout_review_is_the_commands_and_names_the_parts(tmp_path):
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
