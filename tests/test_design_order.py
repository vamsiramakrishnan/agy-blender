"""The package of a design depends on the design, not on the builds that came before it in the session.

run_build applies each script's design to the session's stage as one edit. The IR kept the script's order of parts
but the USD stage composed a part added in the middle after the parts that already existed, so a script built after an
earlier version shipped its parts (and so its vertex and index order, and the manifest's part list) in the history of
the session: the same script delivered twice gave two different GLBs (round seven, "order"). A part dropped and
brought back also left an ``active = true`` opinion that made the shipped USD and USDZ differ.

Two levels: the stage alone (fast, no package) and run_build + deliver_package end to end."""

from __future__ import annotations

import hashlib
import json
import textwrap
from pathlib import Path

import pytest
from pxr import Sdf, Usd

from antigravity3d import ir_schema
from antigravity3d.harness import tools as T
from antigravity3d.harness.session import session
from antigravity3d.ir import MaterialIR, NodeIR, SceneIR, Transform
from antigravity3d.kernel.usd_stage import StageManager
from antigravity3d.runtime import mcp_server as M

# ---------------------------------------------------------------------------------------------------------- the stage

ASSET = "Order"


def _scene(*ids: str, parent: dict[str, str] | None = None) -> SceneIR:
    parent = parent or {}
    return SceneIR(
        asset_name=ASSET,
        materials=[MaterialIR(id="m", base_color=(0.5, 0.5, 0.5))],
        nodes=[NodeIR(id=i, kind="group" if i.startswith("g") else "box", params={} if i.startswith("g") else {"size": (0.1, 0.1, 0.1)},
                      parent=parent.get(i), transform=Transform(translate=(0.2 * n, 0, 0)), material_id=None if i.startswith("g") else "m")
               for n, i in enumerate(ids)],
    )


def _stage(tmp_path: Path) -> StageManager:
    return StageManager.create(tmp_path / "s.usda", ASSET)


def _mesh_order(sm: StageManager) -> list[str]:
    return [m.node_id for m in sm.extract_meshes()]


def _reorders(layer: Sdf.Layer) -> bool:
    return "reorder" in layer.ExportToString()


def test_a_node_added_in_the_middle_is_composed_in_the_designs_order_and_it_takes_a_reorder(tmp_path):
    sm = _stage(tmp_path)
    sm.apply_ir(_scene("a", "c"))
    sm.apply_ir(_scene("a", "b", "c"))
    assert _mesh_order(sm) == ["a", "b", "c"] == [n.id for n in sm.base_ir().nodes]
    assert _reorders(sm._txs[-1][1])  # the positive control: this is the case the opinion exists for


def test_the_second_reorder_wins_over_the_first(tmp_path):
    sm = _stage(tmp_path)
    sm.apply_ir(_scene("a", "c"))
    sm.apply_ir(_scene("a", "b", "c"))       # reorder in a weaker layer
    # a new prim in a stronger layer, over the weaker layer's reorder
    sm.apply_ir(_scene("a", "b", "c", "d"))
    assert _mesh_order(sm) == ["a", "b", "c", "d"]
    sm.apply_ir(_scene("d", "a", "b", "c"))  # a second reorder over the first
    assert _mesh_order(sm) == ["d", "a", "b", "c"]
    sm.apply_ir(_scene("c", "b", "a", "d"))
    assert _mesh_order(sm) == ["c", "b", "a", "d"]


def test_undo_gives_back_the_order_of_the_edit_before(tmp_path):
    sm = _stage(tmp_path)
    sm.apply_ir(_scene("a", "c"))
    sm.apply_ir(_scene("a", "b", "c"))
    sm.rollback()
    assert _mesh_order(sm) == ["a", "c"]
    sm.apply_ir(_scene("a", "b", "c"))
    assert _mesh_order(sm) == ["a", "b", "c"]


def test_a_dropped_node_and_a_renamed_one_leave_the_designs_order(tmp_path):
    sm = _stage(tmp_path)
    sm.apply_ir(_scene("a", "b", "c"))
    sm.apply_ir(_scene("a", "c"))                  # b dropped (deactivated, not removed)
    assert _mesh_order(sm) == ["a", "c"]
    sm.apply_ir(_scene("a", "b", "c"))             # and back: in its place, not last
    assert _mesh_order(sm) == ["a", "b", "c"]
    sm.apply_ir(_scene("a", "b2", "c"))            # renamed = removed + added under the same parent
    assert _mesh_order(sm) == ["a", "b2", "c"]
    sm.apply_ir(_scene("b2", "a", "c"))
    assert _mesh_order(sm) == ["b2", "a", "c"]


def test_the_children_of_a_nested_parent_are_ordered_too(tmp_path):
    """A design can nest (NodeIR.parent: the default mode's groups, imported kits); a script cannot (Asset has no parent)."""
    sm = _stage(tmp_path)
    p = {"x": "g", "y": "g", "z": "g"}
    sm.apply_ir(_scene("g", "x", "z", parent=p))
    sm.apply_ir(_scene("g", "x", "y", "z", parent=p))
    assert _mesh_order(sm) == ["x", "y", "z"]
    sm.apply_ir(_scene("g", "z", "y", "x", parent=p))
    assert _mesh_order(sm) == ["z", "y", "x"]


def test_a_patch_edit_authors_no_reorder_and_keeps_the_order_of_the_ir(tmp_path):
    """agy3d edit and the default mode's apply_scene_ir go through apply_patch: an appended node is already last in the
    IR and in the composed stage, so no reorder opinion is written and the keep-layout path sees nothing new."""
    sm = _stage(tmp_path)
    sm.apply_ir(_scene("a", "b"))
    up = ir_schema.parse_ops([{"op": "upsert_node", "node": {"id": "c", "kind": "box", "params": {"size": [0.1, 0.1, 0.1]}, "transform": {"translate": [0, 0, 0]}, "material_id": "m"}}])
    sm.apply_patch(up)
    assert not _reorders(sm._txs[-1][1])
    assert _mesh_order(sm) == [n.id for n in sm.base_ir().nodes] == ["a", "b", "c"]
    sm.apply_patch(ir_schema.parse_ops([{"op": "set_param", "node_id": "a", "name": "size", "value": [0.2, 0.1, 0.1]}]))
    assert not _reorders(sm._txs[-1][1])
    # an unchanged full scene authors nothing at all
    sm.apply_ir(sm.base_ir())
    assert not _reorders(sm._txs[-1][1])


def test_a_patch_that_appends_after_a_reorder_still_lands_the_node_last(tmp_path):
    sm = _stage(tmp_path)
    sm.apply_ir(_scene("a", "c"))
    sm.apply_ir(_scene("a", "b", "c"))
    sm.apply_patch(ir_schema.parse_ops([{"op": "upsert_node", "node": {"id": "d", "kind": "box", "params": {"size": [0.1, 0.1, 0.1]},
                                                                      "transform": {"translate": [0, 0, 0]}, "material_id": "m"}}]))
    assert _mesh_order(sm) == [n.id for n in sm.base_ir().nodes] == ["a", "b", "c", "d"]


def test_the_shipped_usd_is_flat_so_it_carries_no_reorder_statement(tmp_path):
    sm = _stage(tmp_path)
    sm.apply_ir(_scene("a", "c"))
    sm.apply_ir(_scene("a", "b", "c"))
    text = sm.export_layer().ExportToString()
    assert "reorder" not in text and text.index('"a"') < text.index('"b"') < text.index('"c"')


def test_a_part_brought_back_leaves_no_active_opinion_in_the_shipped_usd(tmp_path):
    fresh = StageManager.create(tmp_path / "x.usda", ASSET)
    sm = StageManager.create(tmp_path / "y.usda", ASSET)
    sm.apply_ir(_scene("a", "b"))
    sm.apply_ir(_scene("a"))
    sm.apply_ir(_scene("a", "b"))
    fresh.apply_ir(_scene("a", "b"))
    assert sm.export_layer().ExportToString() == fresh.export_layer().ExportToString()


# ------------------------------------------------------------------------------------- run_build + deliver_package

PARTS = {
    "body": 'body = a.box("body", size=(1.0, 0.8, 0.6), bottom=(0, 0, 0), material=red)',
    "post": 'post = a.box("post", size=(0.1, 0.1, 0.3), bottom=(0.55, 0, 0), material=blue)',
    "post2": 'post2 = a.box("post2", size=(0.1, 0.1, 0.3), bottom=(0.55, 0, 0), material=blue)',
    "step": 'step = a.box("step", size=(0.3, 0.3, 0.1), bottom=(-0.65, 0, 0), material=blue)',
    "cap": 'cap = a.box("cap", size=(0.4, 0.4, 0.1), bottom=(0, 0, 0.6), material=blue)',
}


def script(order: list[str]) -> str:
    head = textwrap.dedent("""\
        from agy3d import Asset
        a = Asset("crate", engine="godot_4", max_tris=4000, brief="a red crate")
        red = a.material("red", "#c02020", surface="red enamel paint")
        blue = a.material("blue", "#2040d0", surface="blue enamel paint")
        """)
    return head + "\n".join(PARTS[k] for k in order) + "\na.save()\n"


@pytest.fixture
def base(tmp_path, monkeypatch):
    monkeypatch.setenv("AGY3D_OFFLINE", "1")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("AGY3D_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.delenv("AGY3D_POOL_SOCKET", raising=False)
    return tmp_path


class Run:
    """One server session: build scripts one after another, deliver, read back the package folder."""

    def __init__(self, base: Path, name: str):
        self.ws = base / name
        self.ws.mkdir()
        self.host = M.StudioHost(script=True)
        self.n = 0

    async def build(self, order: list[str]) -> None:
        (self.ws / "crate.py").write_text(script(order))
        kw = {"preview": True, "token_ceiling": 100_000, "max_images": 0} if self.n == 0 else {}
        self.n += 1
        r = await self.host.run_build("crate.py", str(self.ws), "conv-order-" + "0" * 32, **kw)
        assert r.startswith("BUILT crate.py"), r

    async def undo(self) -> None:
        r = await self.host.call_tool("undo_last_edit", T.undo_last_edit, "", {})
        assert r.startswith("UNDONE"), r

    @property
    def stage(self) -> StageManager:
        return session(self.host.registry, self.host.active).stage

    async def deliver(self) -> dict[str, str]:
        r = await self.host.call_tool("deliver_package", T.deliver_package, "", {"dest": "output"})
        assert r.startswith("DELIVERED"), r
        return digests(session(self.host.registry, self.host.active).last_export)  # the whole package: usd, usdz, inspector too


def digests(folder: Path) -> dict[str, str]:
    """Every file's hash. The manifest's ``qa.revision`` is the session's random gate token (a new uuid on every edit,
    different even between two fresh sessions; tests/test_revision_gate.py pins it), so it is left out of the manifest's
    hash and the rest of the manifest, which lists every other file's hash, is compared."""
    out = {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.rglob("*")) if p.is_file()}
    man = json.loads((folder / "manifest.json").read_text())
    man["qa"].pop("revision")
    out["manifest.json"] = hashlib.sha256(json.dumps(man, sort_keys=True).encode()).hexdigest()
    return out


async def fresh_delivery(base: Path, order: list[str]) -> dict[str, str]:
    run = Run(base, "fresh-" + "-".join(order))
    await run.build(order)
    return await run.deliver()


def same(a: dict[str, str], b: dict[str, str]) -> None:
    assert sorted(a) == sorted(b)
    assert {k: (a[k][:8], b[k][:8]) for k in a if a[k] != b[k]} == {}


FINAL = ["body", "step", "post"]


@pytest.mark.parametrize("earlier", [
    [["body", "post"]],                                  # a part inserted in the middle of the script
    [["post", "step", "body"]],                          # the same parts in another order
    [["body", "step", "post"], ["body", "post"]],        # a part dropped, then put back in its place
])
async def test_a_script_built_after_other_versions_ships_the_files_of_a_fresh_build(base, earlier):
    run = Run(base, "session")
    for order in [*earlier, FINAL]:
        await run.build(order)
    assert [m.node_id for m in run.stage.extract_meshes()] == FINAL
    same(await run.deliver(), await fresh_delivery(base, FINAL))


SERIES = [["body", "post"], ["body", "step", "post"], ["body", "step", "post", "cap"], ["cap", "body", "step", "post"],
          ["body", "post2", "step"], ["body", "step"], ["step", "body"]]


@pytest.mark.parametrize("k", range(1, len(SERIES)))
async def test_each_build_of_a_series_delivers_as_a_fresh_build_of_it_would(base, k):
    """A reorder in a weaker layer plus a new prim in a stronger one, a second reorder over the first, a part removed,
    a part renamed. One delivery at the end: the manifest's audit trail and the inspector page list the session's own
    QA reviews, so a session that delivered more than once reads differently there by design."""
    run = Run(base, "series")
    for order in SERIES[:k + 1]:
        await run.build(order)
    same(await run.deliver(), await fresh_delivery(base, SERIES[k]))


async def test_undo_after_a_reordering_build_delivers_as_a_fresh_build_of_the_version_before(base):
    run = Run(base, "undo")
    await run.build(["body", "post"])
    await run.build(FINAL)
    await run.undo()
    assert [m.node_id for m in run.stage.extract_meshes()] == ["body", "post"]
    got, want = await run.deliver(), await fresh_delivery(base, ["body", "post"])
    # what undo leaves is the session's record of the script that was built last: that file and what lists its hash
    for d in (got, want):
        d.pop("source/build.py"), d.pop("manifest.json")
    same(got, want)
