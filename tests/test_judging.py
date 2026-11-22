"""The judge is shown each design as separate views, one design per request (tactics.judging, decided 2026-11-22)."""

from __future__ import annotations

import asyncio
import dataclasses
import io

import numpy as np
import pytest
import trimesh
from PIL import Image
from scipy import ndimage

from antigravity3d.decisions import engine as E, questions as Q
from antigravity3d.decisions.engine import DecisionControlPlane, MediaResolution
from antigravity3d.decisions.fakes import FakeDecisionsClient, FakeWireError, validate_request
from antigravity3d.kernel import som
from antigravity3d.kernel.raster import JUDGE_VIEWS, contact_sheet, render_buffers
from antigravity3d.kernel.types import MeshData
from antigravity3d.tactics import judging
from antigravity3d.tactics.governor import Governor, plan_for

VIEWS = tuple((v, f"https://x/{v}.png") for v in JUDGE_VIEWS)


def md(tm, nid, t=(0, 0, 0)) -> MeshData:
    tm = tm.copy()
    tm.apply_translation(t)
    return MeshData(nid, f"/A/{nid}", np.array(tm.vertices, float), np.array(tm.faces, np.int32), "m")


def crate() -> list[MeshData]:
    """Asymmetric on purpose (a handle on +x only, a drum at -y), so a view from the opposite corner differs."""
    return [md(trimesh.creation.box((1.2, 0.8, 0.7)), "body", (0, 0, 0.35)),
            md(trimesh.creation.box((0.08, 0.3, 0.06)), "handle_r", (0.64, 0, 0.5)),
            md(trimesh.creation.cylinder(0.2, 0.5, sections=24), "drum", (0, -0.6, 0.25)),
            md(trimesh.creation.box((0.3, 0.3, 0.05)), "lid_plate", (0.2, 0.1, 0.725)),
            md(trimesh.creation.box((0.02, 0.02, 0.02)), "speck", (0.5, 0.3, 0.71))]


async def _nosleep(_: float) -> None:
    return None


# ---- the rear perspective ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("wire", [False, True])
def test_rear_perspective_is_the_front_one_of_the_design_turned_half_a_turn(wire):
    """Camera and key light both turned: exactly what the live measurement showed the judge (the meshes turned)."""
    ms = crate()
    turned = [dataclasses.replace(m, points=m.points * np.array([-1.0, -1.0, 1.0])) for m in ms]
    a = render_buffers(ms, "persp_rear", 160, wireframe=wire)
    b = render_buffers(turned, "persp", 160, wireframe=wire)
    assert np.array_equal(a.color, b.color) and np.array_equal(a.prim_id, b.prim_id) and a.camera.persp
    assert not np.array_equal(a.color, render_buffers(ms, "persp", 160, wireframe=wire).color)


def test_rear_perspective_faces_are_key_lit():
    """With the world key light the faces the rear camera sees would get fill light only (all about equally dark)."""
    ms = [md(trimesh.creation.box((1, 1, 1)), "cube", (0, 0, 0.5))]
    img = render_buffers(ms, "persp_rear", 128).color
    shades = {tuple(c) for c in img.reshape(-1, 3) if tuple(c) != tuple(int(x) for x in [34, 36, 42])}
    assert len(shades) >= 3  # top and two sides distinctly lit, as in the front perspective
    assert len(shades) == len({tuple(c) for c in render_buffers(ms, "persp", 128).color.reshape(-1, 3)}) - 1


# ---- set-of-mark speed-ups are exact ---------------------------------------------------------------------------------

def test_cropped_distance_transform_equals_the_full_frame_one():
    ms = crate()
    for v in JUDGE_VIEWS:
        b = render_buffers(ms, v, 192)
        boxes = ndimage.find_objects(b.prim_id + 1, max_label=len(b.node_ids))
        for i in np.unique(b.prim_id[b.prim_id >= 0]):
            full = ndimage.distance_transform_edt(np.pad(b.prim_id == i, 1))[1:-1, 1:-1]
            ys, xs = boxes[i]
            crop = np.zeros_like(full)
            crop[ys, xs] = ndimage.distance_transform_edt(np.pad(b.prim_id[ys, xs] == i, 1))[1:-1, 1:-1]
            assert np.array_equal(full, crop)


def _free_box_reference(integral, placed, anchor, w, h, size):
    """kernel.som._free_box as it was before 2026-11-22: every grid position tested against every placed box."""
    xs, ys = np.arange(0, size - w + 1, 2), np.arange(0, size - h + 1, 2)
    X, Y = np.meshgrid(xs, ys)
    occ = (integral[Y + h, X + w] - integral[Y, X + w] - integral[Y + h, X] + integral[Y, X]) / (w * h)
    cost = occ * 3 * size + np.hypot(X + w / 2 - anchor[0], Y + h / 2 - anchor[1])
    M = som.MARGIN
    for p in placed:
        cost = np.where(~((X + w - 1 + M < p[0]) | (p[2] + M < X) | (Y + h - 1 + M < p[1]) | (p[3] + M < Y)), np.inf, cost)
    j = int(np.argmin(cost))
    if not np.isfinite(cost.flat[j]):
        return None
    x, y = int(X.flat[j]), int(Y.flat[j])
    return x, y, x + w - 1, y + h - 1


def test_free_box_equals_the_brute_force_search():
    rng = np.random.default_rng(7)
    occupied = rng.random((96, 96)) < 0.3
    integral = np.pad(occupied.astype(np.int32).cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    for _ in range(200):
        placed = []
        for _ in range(int(rng.integers(0, 40))):
            x, y = int(rng.integers(-5, 96)), int(rng.integers(-5, 96))
            placed.append((x, y, x + int(rng.integers(0, 20)), y + int(rng.integers(0, 14))))
        w, h, anchor = int(rng.integers(8, 24)), int(rng.integers(8, 18)), (int(rng.integers(0, 96)), int(rng.integers(0, 96)))
        assert som._free_box(integral, placed, anchor, w, h, 96) == _free_box_reference(integral, placed, anchor, w, h, 96)


# ---- what the judge is shown -------------------------------------------------------------------------------------

def _rgb(png: bytes) -> np.ndarray:
    return np.asarray(Image.open(io.BytesIO(png)).convert("RGB"))


def test_render_design_wire_style_is_the_measured_image_and_badges_share_a_render_of_one_style():
    """Style "wire" is the image measured on 2026-11-22 (contact_sheet of one view, cropped); badged views drawn in the
    judge's own style differ from its views by the badges alone."""
    ms = crate()
    r = judging.render_design(ms, size=128, style="wire")
    assert [n for n, _ in r.plain] == [n for n, _ in r.marked] == list(JUDGE_VIEWS)
    for (name, png), (_, badged) in zip(r.plain, r.marked):
        if name != "persp_rear":
            assert np.array_equal(_rgb(png), np.asarray(contact_sheet(ms, views=(name,), size=128).crop((0, 0, 128, 128))))
        diff = (_rgb(png) != _rgb(badged)).any(axis=2)
        assert diff.any() and diff.mean() < 0.5  # the badges, nothing else redrawn
    assert r.legend == som.assign_badges(ms) and "[01]" in r.legend_text and "persp_rear" in r.legend_text


def test_judge_views_are_lit_as_shipped_with_feature_edges():
    """Changed 2026-11-22 (r5/live/view_style): the judge's views were the wireframe clay above; they are now the design
    lit with its shipped normals and drawn with its feature edges, and the badged views are the same render."""
    ms = [md(trimesh.creation.cylinder(0.4, 1.0, sections=32), "drum", (0, 0, 0.5)),
          md(trimesh.creation.box((0.3, 0.02, 0.3)), "plate", (0, -0.41, 0.5))]
    r = judging.render_design(ms, size=160)
    normals = judging.shipped_normals(ms)
    for (name, png), (_, badged) in zip(r.plain, r.marked):
        want = render_buffers(ms, name, 160, normals=normals, feature_edges=True).color
        assert np.array_equal(_rgb(png)[16:-1, 1:-1], want[16:-1, 1:-1])  # all but the label and the frame
        assert not np.array_equal(want, render_buffers(ms, name, 160, wireframe=True).color)
        diff = (_rgb(png) != _rgb(badged)).any(axis=2)
        assert diff.any() and diff.mean() < 0.5  # the badges over the same render
    # the smooth drum: no line inside its side; the flush plate: its outline is drawn in the author's front view
    front = render_buffers(ms, "front", 160, normals=normals, feature_edges=True)
    plain = render_buffers(ms, "front", 160, normals=normals)
    drawn = (front.color != plain.color).any(axis=2)
    plate = front.prim_id == 1
    side = ndimage.binary_erosion(front.prim_id == 0, iterations=2) & ~ndimage.binary_dilation(plate, iterations=2)
    assert drawn[plate].any() and side.sum() > 1000 and not drawn[side].any()
    wire = (render_buffers(ms, "front", 160, wireframe=True).color != render_buffers(ms, "front", 160).color).any(axis=2)
    assert wire[side].any()  # the triangulation the wireframe drew across the same smooth side


async def test_judge_design_one_request_of_five_high_views_with_the_brief():
    c = FakeDecisionsClient()
    uploads: list[bytes] = []

    async def up(b: bytes) -> str:
        uploads.append(b)
        return f"https://x/u{len(uploads)}.png"

    jd = await judging.judge_design(DecisionControlPlane(c), crate(), brief="a crate with a handle", governor=None, uploader=up,
                                    ortho_uri="https://x/o.png")
    (req,) = c.calls
    imgs = [b for b in req["input"] if b["type"] == "image"]
    assert req["n_images"] == 6 and [b["resolution"] for b in imgs] == ["high"] * 5 + ["low"]  # blueprint as it was
    assert [n for n, _ in jd.views] == list(JUDGE_VIEWS) and [b["uri"] for b in imgs[:5]] == [u for _, u in jd.views]
    assert len(uploads) == 10 and len(jd.author_views) == 5 and jd.author_views[0][0] == "front view of the current design (Image 1)"
    assert {u for _, u in jd.views}.isdisjoint(u for _, u in jd.author_views)  # judge_design.JUDGE_SEES_BADGES is off
    assert "score_brief_depiction" in jd.result.scores and jd.result.resolution == "high"
    for q in req["questions"].values():
        assert "contact sheet" not in q["instructions"]
    assert req["questions"]["defect_missing_parts"]["instructions"].startswith(
        "Images 1 to 5 are five separate renders of one 3D asset from different angles, one view each. Image 6 is a four-view")
    jd2 = await judging.judge_design(DecisionControlPlane(c), crate(), brief=None, governor=None, uploader=up)
    assert "score_brief_depiction" not in jd2.result.scores and "brief" not in str(c.calls[-1]["input"])


async def test_single_image_requests_are_unchanged():
    """The sheet requests (every caller that passes a uri) carry the sheet texts exactly."""
    c = FakeDecisionsClient()
    p = DecisionControlPlane(c)
    await p.evaluate_visual_qa("https://x/s.png", "https://x/o.png", stage="blockout", brief="crate", marked_legend={1: "body"})
    await p.rank_candidates("geometry", ["https://x/a", "https://x/b"], ["https://x/o"], brief="crate")
    await p.locate_defects("https://x/m.png", {1: "body"}, ["defect_floating"])
    vqa, bon, loc = c.calls
    assert vqa["input"][0]["text"] == "Image 1: contact sheet of renders of the asset under review. Each part carries a numbered badge: [01] = body."
    assert vqa["questions"]["defect_floating"]["instructions"] == (
        "Image 1 is a contact sheet of renders of one 3D asset from several angles. Does any part of the asset visibly float above "
        "the ground or hover detached from the part it should touch?")
    assert all(b["resolution"] == "low" for b in vqa["input"] + bon["input"] if b["type"] == "image")
    assert bon["questions"]["policy_legible_text__1"] == Q.render(Q.SHEET_POLICY_QUESTIONS["policy_legible_text"], {"img": "Image 2"})
    assert bon["questions"]["score_brief_depiction__2"]["instructions"].startswith(
        "Image 3 shows one candidate image. It is a contact sheet of renders of one 3D asset from several angles.")
    assert loc["questions"]["defect_floating__1"]["instructions"].startswith(Q.MARKED_SHEET_NOTE.replace("{sheet}", "Image 1"))


async def test_locate_defects_on_marked_views():
    c = FakeDecisionsClient({"defect_floating__2": 0.9})
    out = await DecisionControlPlane(c).locate_defects(VIEWS, {1: "body", 2: "handle_r"}, ["defect_floating"])
    assert out == {"defect_floating": [("handle_r", 0.9)]}
    (req,) = c.calls
    assert req["n_images"] == 5 and all(b["resolution"] == "high" for b in req["input"] if b["type"] == "image")
    assert "same number in every view" in req["questions"]["defect_floating__1"]["instructions"]


# ---- the ranking -------------------------------------------------------------------------------------------------

async def test_ranking_views_one_request_per_candidate_sent_together():
    c = FakeDecisionsClient(latency_s=0.05, responder=lambda req: {"defect_clipping": 0.9} if
                            any(b.get("uri") == "https://x/b/front.png" for b in req["input"]) else {})
    cands = [tuple((v, f"https://x/{k}/{v}.png") for v in JUDGE_VIEWS) for k in "abc"]
    out = await DecisionControlPlane(c).rank_candidates("geometry", cands, ["https://x/o.png"], brief="a crate")
    assert len(c.calls) == 3 and c.max_in_flight == 3  # concurrent, one design each
    assert [s.uri for s in out] == [f"https://x/{k}/front.png" for k in "abc"]
    assert [s.defects["defect_clipping"] for s in out] == [0.05, 0.9, 0.05]  # each its own answer
    assert len({id(s.result) for s in out}) == 3
    for i, req in enumerate(c.calls):
        assert req["n_images"] == 6 and set(req["questions"]) == {f"{k}__{i + 1}" for k in (
            "score_silhouette_vs_blueprint", "defect_floating", "defect_clipping", "defect_brief_part_missing",
            "score_brief_depiction", *Q.VIEWS_POLICY_QUESTIONS)}
        imgs = [b for b in req["input"] if b["type"] == "image"]
        assert imgs[0]["resolution"] == "low" and {b["resolution"] for b in imgs[1:]} == {"high"}
        legible = req["questions"][f"policy_legible_text__{i + 1}"]["instructions"]
        assert "Images 2 to 6" in legible and "persp_rear" in legible and "Apart from those labels" in legible
        assert "numbered badges" not in legible  # the ranking's views are unmarked


async def test_ranking_views_queue_under_the_ceiling(tmp_path):
    """Each native reservation holds the documented input cap; with room for two, two run at once and all are judged."""
    hold = E.MODEL_INPUT_LIMIT * E.PRICE_PER_M_INPUT_USD / 1e6
    gov = Governor(plan_for(0), journal=tmp_path / "spend.db")
    gov.charge("previous", gov.remaining - 2.5 * hold)
    c = FakeDecisionsClient(latency_s=0.05, input_tokens=7000)
    cands = [tuple((v, f"https://x/{k}/{v}.png") for v in JUDGE_VIEWS) for k in range(5)]
    p = DecisionControlPlane(c, sleep=_nosleep, backend_kind="decisions")
    out = await p.rank_candidates("geometry", cands, governor=gov, brief="a crate")
    assert all(s.error is None and not s.blocked for s in out) and len(c.calls) == 5
    assert c.max_in_flight == 2 and gov.spent <= gov.ceiling


async def test_ranking_view_that_can_never_fit_fails_alone(tmp_path):
    gov = Governor(plan_for(0), journal=tmp_path / "spend.db")
    gov.charge("previous", gov.remaining - 0.5 * E.MODEL_INPUT_LIMIT * E.PRICE_PER_M_INPUT_USD / 1e6)
    c = FakeDecisionsClient()
    out = await DecisionControlPlane(c, backend_kind="decisions").rank_candidates("geometry", [VIEWS, VIEWS], governor=gov)
    assert all(s.blocked and "BudgetExceeded" in (s.error or "") for s in out) and not c.calls


def test_view_requests_stay_under_the_admission_knee():
    """Five high views, the blueprint and style images, a brief of the longest length: one request, not split."""
    brief = "x" * E.MAX_BRIEF_CHARS
    for _, _, blocks, qs in E.bon_view_packets("geometry", [VIEWS], ["https://x/o"], brief=brief):
        assert E.estimate_input_tokens(blocks, qs) < E.LATENCY_KNEE_TOKENS
    vl = Q.views_labels(1, JUDGE_VIEWS)
    qs, _ = Q.select(Q.VISUAL_QA_QUESTIONS, stage="blockout", have={"ortho", "style", "brief"},
                     labels={"sheet": vl["span"], **vl, "ortho": "Image 6", "style": "Image 7"}, subjects=Q.SHEET_SUBJECTS)
    blocks = [*E.view_blocks(VIEWS, 1, MediaResolution.HIGH), E.brief_block(brief), E.image_block("o", "low"), E.image_block("s", "low")]
    assert E.estimate_input_tokens(blocks, qs) < E.LATENCY_KNEE_TOKENS


def test_every_shape_question_has_a_views_subject():
    for bank, subjects in ((Q.VISUAL_QA_QUESTIONS, Q.SHEET_SUBJECTS), (Q.BON_QUESTIONS["geometry"], Q.CAND_SUBJECTS)):
        for spec in bank.values():
            assert "contact sheet" not in Q.resubject(spec.question, subjects)["instructions"]
    with pytest.raises(ValueError):
        Q.resubject(Q.noul("Image 1 is something else."), Q.SHEET_SUBJECTS)


def test_fake_checks_view_spans():
    q = {"a": Q.noul("Do any of Images 1 to 6 show a logo?")}
    with pytest.raises(FakeWireError):
        validate_request("m", [E.image_block("https://x/1", "high")] * 5, q)
    assert validate_request("m", [E.image_block("https://x/1", "high")] * 6, q) == 6
    with pytest.raises(FakeWireError):
        validate_request("m", [E.image_block("https://x/1")] * 6, {"a": Q.noul("Images 1 to 6 are a contact sheet.")})


# ---- the tournament records each judgement ---------------------------------------------------------------------

async def test_geometry_tournament_notes_each_judgement(tmp_path):
    from antigravity3d.ir import MaterialIR, NodeIR, SceneIR
    from antigravity3d.kernel.usd_stage import StageManager
    from antigravity3d.state import EngineBudget
    from antigravity3d.tactics.tournament import geometry_tournament

    def scene(h: float) -> SceneIR:
        n = NodeIR(id="body", kind="box", params={"size": (1, 1, h)}, material_id="wood")
        n = n.model_copy(update={"transform": n.transform.model_copy(update={"translate": (0, 0, h / 2)})})
        return SceneIR(asset_name="Prop", nodes=[n], materials=[MaterialIR(id="wood")])

    c = FakeDecisionsClient(latency_s=0.02)
    sm = StageManager.create(tmp_path / "p.usda", "Prop")

    async def up(b: bytes) -> str:
        return "data:image/png;base64,AAAA"

    r = await geometry_tournament(None, sm, "a box", plane=DecisionControlPlane(c), governor=Governor(plan_for(2)),
                                  budget=EngineBudget("unreal_5"), uploader=up, scenes=[scene(1.0), scene(0.5), scene(0.8)], size=96)
    assert len(c.calls) == 3 and c.max_in_flight == 3
    assert set(r.notes["judged"]) == {0, 1, 2} and all(v["attempts"] == 1 for v in r.notes["judged"].values())
    assert r.notes["judging"].startswith("3 candidate(s) judged separately, about ")
    assert r.candidates[r.winner.index].views and len(r.candidates[r.winner.index].views) == 5


# ---- a judgement that runs long is asked a second time -------------------------------------------------------------

class _SlowFirst(FakeDecisionsClient):
    """The n-th request takes delays[n] seconds (the last value after that)."""

    def __init__(self, delays, **kw):
        super().__init__(**kw)
        self.delays, self.sent = list(delays), 0

    async def _create(self, model, input, questions):
        d = self.delays[min(self.sent, len(self.delays) - 1)]
        self.sent += 1
        await asyncio.sleep(d)
        return await super()._create(model, input, questions)


async def test_a_long_design_judgement_is_asked_again_and_the_first_answer_taken(tmp_path):
    gov = Governor(plan_for(0), journal=tmp_path / "spend.db")
    c = _SlowFirst([5.0, 0.01], input_tokens=7000)
    p = DecisionControlPlane(c, sleep=_nosleep, backend_kind="decisions", hedge_after_s=0.1)
    t0 = asyncio.get_running_loop().time()
    r = await p.evaluate_visual_qa(VIEWS, stage="blockout", governor=gov, brief="a crate")
    assert asyncio.get_running_loop().time() - t0 < 2.0 and p.stats.hedges == 1 and r.result.attempts == 2
    statuses = sorted(x["status"] for x in gov.receipts())
    assert statuses == ["CancelledError", "ok"]  # the abandoned request keeps its whole reservation
    out = await DecisionControlPlane(_SlowFirst([5.0, 0.01]), hedge_after_s=0.1).rank_candidates("geometry", [VIEWS], brief="a crate")
    assert out[0].error is None and out[0].result.attempts == 2


async def test_no_second_request_when_the_ceiling_has_no_room_for_it(tmp_path):
    hold = E.MODEL_INPUT_LIMIT * E.PRICE_PER_M_INPUT_USD / 1e6
    gov = Governor(plan_for(0), journal=tmp_path / "spend.db")
    gov.charge("previous", gov.remaining - 1.5 * hold)
    c = _SlowFirst([0.4, 0.01], input_tokens=7000)
    p = DecisionControlPlane(c, sleep=_nosleep, backend_kind="decisions", hedge_after_s=0.05)
    r = await p.evaluate_visual_qa(VIEWS, stage="blockout", governor=gov)
    assert c.sent == 1 and r.result.attempts == 1 and p.stats.hedges == 0  # tried, refused admission, first answered


async def test_sheet_requests_and_fast_judgements_are_never_hedged():
    c = _SlowFirst([0.3])
    p = DecisionControlPlane(c, hedge_after_s=0.05)
    await p.evaluate_visual_qa("https://x/s.png", stage="blockout")
    assert c.sent == 1 and p.stats.hedges == 0
    c2 = _SlowFirst([0.0])
    p2 = DecisionControlPlane(c2, hedge_after_s=0.05)
    await p2.evaluate_visual_qa(VIEWS, stage="blockout")
    assert c2.sent == 1 and p2.stats.hedges == 0
