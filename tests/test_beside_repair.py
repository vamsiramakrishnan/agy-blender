"""A part flagged floating that has another part beside it (a gap measured sideways or at an edge) is left where it is by
the repairs; a part hovering over a support or over empty ground is moved as before (kernel/settle.beside_a_part)."""

from __future__ import annotations

import asyncio

import numpy as np

from antigravity3d.ir import MaterialIR, NodeIR, SceneIR, Transform
from antigravity3d.kernel.settle import beside_a_part
from antigravity3d.tactics.authoring import TemplateAuthor, _reground, world_bounds


def box(nid: str, lo, hi) -> NodeIR:
    lo, hi = np.asarray(lo, float), np.asarray(hi, float)
    return NodeIR(id=nid, kind="box", params={"size": tuple(hi - lo)}, transform=Transform(translate=tuple((lo + hi) / 2)), material_id="m")


def scene(*nodes: NodeIR) -> SceneIR:
    return SceneIR(asset_name="T", nodes=list(nodes), materials=[MaterialIR(id="m")])


BODY = box("body", (-0.5, -0.5, 0), (0.5, 0.5, 1))


def test_decision_table():
    bounds = lambda *ns: world_bounds(scene(*ns))  # noqa: E731
    # over a support: seat it (not beside)
    assert not beside_a_part(bounds(BODY, box("lid", (-0.5, -0.5, 1.2), (0.5, 0.5, 1.3))), "lid")
    # nothing under it, a part's box within 5 mm: beside
    assert beside_a_part(bounds(BODY, box("knob", (0.503, -0.1, 0.4), (0.6, 0.1, 0.6))), "knob")
    # nothing under it, nothing near: hovering over empty ground, the ground is its support (as before)
    assert not beside_a_part(bounds(BODY, box("far", (2, 2, 1), (2.2, 2.2, 1.2))), "far")
    # 2 cm sideways is farther than the old window: unchanged behaviour
    assert not beside_a_part(bounds(BODY, box("side", (0.52, -0.1, 0.4), (0.56, 0.1, 0.6))), "side")


def test_reground_leaves_a_part_beside_another_and_still_drops_a_hovering_one():
    beside = box("knob", (0.5, -0.1, 0.4), (0.6, 0.1, 0.6))                # touches the body's side box, hangs 0.4 m up
    assert _reground(scene(BODY, beside), {"knob"}) == []
    hover = box("lid", (-0.5, -0.5, 1.2), (0.5, 0.5, 1.3))
    (op,) = _reground(scene(BODY, hover), {"lid"})
    assert op.node_id == "lid" and abs(op.transform.translate[2] - 1.05) < 1e-9                      # seated on the body
    high = box("far", (2, 2, 1), (2.2, 2.2, 1.2))
    (op,) = _reground(scene(BODY, high), {"far"})
    assert abs(op.transform.translate[2] - 0.1) < 1e-9                                               # onto the ground: today's behaviour


def test_template_author_declines_a_beside_part_and_reports_nothing_to_do():
    author = TemplateAuthor()
    s = scene(BODY, box("knob", (0.5, -0.1, 0.4), (0.6, 0.1, 0.6)))
    patch = asyncio.run(author.repair(s, action=None, located={"tier1:floating_part": ["knob"]}, manifest=""))
    assert patch is None
    # a floating group has no mechanical repair: moving its members one at a time would pull it apart
    s2 = scene(BODY, box("g1", (2, 2, 1), (2.4, 2.4, 1.4)), box("g2", (2, 2, 1.4), (2.4, 2.4, 1.8)))
    assert asyncio.run(author.repair(s2, action=None, located={"tier1:floating_group": ["g1"]}, manifest="")) is None
