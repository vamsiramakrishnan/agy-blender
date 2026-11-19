"""OpenUSD stage manager: transactional sublayers + variant-set branches.

Layout of a managed stage::

    root layer (on disk)          committed state
      class /__agy_base           the plain scene: nodes, Looks, customData IR
      def Xform /<asset>          default prim, kind=component,
                                  references </__agy_base>, variant sets
    session layer
      subLayerPaths = [tx_n, ..., tx_1]   anonymous per-edit delta layers

Every edit authors into a fresh anonymous layer inserted as the strongest
session sublayer, so rollback is removing a sublayer path. The plain scene
lives under an internal *reference* (not directly on the default prim)
because USD's LIVRPS ordering makes local opinions stronger than variants:
behind a reference, a variant can override any base node, so a variant only
has to hold the delta of its proposal against the base.
"""

from __future__ import annotations

import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import numpy as np
from pxr import Kind, Sdf, Usd, UsdGeom, UsdShade, UsdUtils

from antigravity3d import ir
from antigravity3d.ir import IRPatch, SceneIR
from antigravity3d.kernel.compile_ir import IR_KEY, author_scene, node_paths, read_ir
from antigravity3d.kernel.types import MeshData

BASE = Sdf.Path("/__agy_base")


def _fmt(v: float) -> str:
    s = f"{round(v, 3) + 0.0:.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _vec(v) -> str:
    return "[" + ",".join(_fmt(x) for x in v) + "]"


def _triangulate(counts: np.ndarray, idx: np.ndarray) -> np.ndarray:
    if len(counts) and np.all(counts == 3):
        return idx.reshape(-1, 3).astype(np.int32)
    tris: list[tuple[int, int, int]] = []
    start = 0
    for c in counts:  # fan triangulation of convex polygons
        f = idx[start : start + c]
        tris.extend((f[0], f[i], f[i + 1]) for i in range(1, c - 1))
        start += c
    return np.asarray(tris, dtype=np.int32).reshape(-1, 3)


class StageManager:
    def __init__(self, stage: Usd.Stage) -> None:
        self._stage = stage
        self._txs: list[tuple[str, Sdf.Layer]] = []  # bottom -> top; holds layers alive

    # --- construction ----------------------------------------------------------

    @classmethod
    def create(cls, path: Path, asset_name: str) -> StageManager:
        stage = Usd.Stage.CreateNew(str(path))
        cls._init_structure(stage, SceneIR(asset_name=asset_name))
        stage.GetRootLayer().Save()
        return cls(stage)

    @classmethod
    def open(cls, path: Path) -> StageManager:
        """Opens a managed stage; a plain exported file is adopted from its stored IR."""
        stage = Usd.Stage.Open(str(path))
        if not stage.GetPrimAtPath(BASE):
            scene = read_ir(stage.GetDefaultPrim())
            if scene is None:
                raise ValueError(f"{path}: default prim has no '{IR_KEY}' customData")
            stage.GetRootLayer().Clear()  # rebuilt from the IR; saved only on commit_all
            cls._init_structure(stage, scene)
        return cls(stage)

    @staticmethod
    def _init_structure(stage: Usd.Stage, scene: SceneIR) -> None:
        stage.SetEditTarget(stage.GetRootLayer())
        stage.CreateClassPrim(BASE)
        asset = UsdGeom.Xform.Define(stage, f"/{scene.asset_name}").GetPrim()
        Usd.ModelAPI(asset).SetKind(Kind.Tokens.component)
        asset.GetReferences().AddInternalReference(BASE)
        stage.SetDefaultPrim(asset)
        UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
        UsdGeom.SetStageMetersPerUnit(stage, 1.0)
        author_scene(stage, scene, root=BASE)

    # --- accessors -----------------------------------------------------------

    @property
    def stage(self) -> Usd.Stage:
        return self._stage

    @property
    def asset_prim(self) -> Usd.Prim:
        return self._stage.GetDefaultPrim()

    def scene_ir(self) -> SceneIR:
        """IR of the composed scene (including the selected variants)."""
        scene = read_ir(self.asset_prim)
        assert scene is not None, "managed stage lost its scene IR"
        return scene

    def base_ir(self) -> SceneIR:
        """IR of the plain scene, ignoring variant selections."""
        scene = read_ir(self._stage.GetPrimAtPath(BASE))
        assert scene is not None, "managed stage lost its base scene IR"
        return scene

    # --- transactions --------------------------------------------------------

    @property
    def tx_stack(self) -> list[str]:
        """Anonymous layer identifiers, oldest first (mirrors AssetSessionState.tx_stack)."""
        return [layer.identifier for _, layer in self._txs]

    @property
    def tx_ids(self) -> list[str]:
        return [tid for tid, _ in self._txs]

    def begin(self, tx_id: str | None = None) -> str:
        tx_id = tx_id or uuid.uuid4().hex[:12]
        if tx_id in self.tx_ids:
            raise ValueError(f"transaction {tx_id!r} already open")
        layer = Sdf.Layer.CreateAnonymous(f"tx_{tx_id}")
        self._stage.GetSessionLayer().subLayerPaths.insert(0, layer.identifier)
        self._txs.append((tx_id, layer))
        self._stage.SetEditTarget(layer)
        return tx_id

    def _index(self, tx_id: str) -> int:
        for i, (tid, layer) in enumerate(self._txs):
            if tx_id in (tid, layer.identifier):
                return i
        raise KeyError(f"unknown transaction {tx_id!r}")

    def rollback(self, tx_id: str | None = None) -> list[str]:
        """Drops ``tx_id`` (default: the top tx) and every tx above it; returns dropped ids."""
        if not self._txs:
            raise RuntimeError("no open transaction to roll back")
        i = len(self._txs) - 1 if tx_id is None else self._index(tx_id)
        dropped, self._txs = self._txs[i:], self._txs[:i]
        sub = self._stage.GetSessionLayer().subLayerPaths
        with Sdf.ChangeBlock():  # one recomposition for the whole drop
            for _, layer in dropped:
                sub.remove(layer.identifier)
        self._stage.SetEditTarget(self._txs[-1][1] if self._txs else self._stage.GetRootLayer())
        return [tid for tid, _ in dropped]

    @contextmanager
    def transaction(self, tx_id: str | None = None) -> Iterator[str]:
        """Opens a tx; on exception it (and anything above it) is rolled back."""
        tid = self.begin(tx_id)
        try:
            yield tid
        except BaseException:
            if tid in self.tx_ids:
                self.rollback(tid)
            raise

    def _flatten_stack(self) -> Sdf.Layer:
        # Merges session layer + tx sublayers + root layer stack, keeping arcs
        # (reference to the base, variant sets) intact; not a composed flatten.
        return UsdUtils.FlattenLayerStack(self._stage)

    def commit_all(self) -> None:
        """Bakes every open tx into the root layer and saves it (if it is on disk)."""
        root = self._stage.GetRootLayer()
        flat = self._flatten_stack()
        session = self._stage.GetSessionLayer()
        with Sdf.ChangeBlock():
            session.subLayerPaths.clear()
            root.TransferContent(flat)
        self._txs.clear()
        self._stage.SetEditTarget(root)
        if not root.anonymous:
            root.Save()

    def flatten_to(self, path: Path) -> None:
        """Writes the baked layer stack (still a managed stage) without touching the session."""
        self._flatten_stack().Export(str(path))

    # --- edits ---------------------------------------------------------------

    def _check_scene(self, scene: SceneIR) -> None:
        if scene.asset_name != self.asset_prim.GetName():
            raise ValueError(f"asset_name {scene.asset_name!r} != stage asset {self.asset_prim.GetName()!r}")

    def _check_no_selection(self) -> None:
        vsets = self.asset_prim.GetVariantSets()
        sel = {n: vsets.GetVariantSelection(n) for n in vsets.GetNames()}
        if any(sel.values()):
            raise RuntimeError(f"variant selected {sel}; promote it or roll back before editing the base")

    def apply_ir(self, scene: SceneIR, tx_id: str | None = None) -> str:
        self._check_scene(scene)
        self._check_no_selection()
        with self.transaction(tx_id) as tid:
            author_scene(self._stage, scene, root=BASE)
        return tid

    def apply_patch(self, patch: IRPatch, tx_id: str | None = None) -> str:
        self._check_no_selection()
        with self.transaction(tx_id) as tid:
            author_scene(self._stage, ir.apply_patch(self.base_ir(), patch), root=BASE)
        return tid

    # --- variants ------------------------------------------------------------

    def add_variant(self, set_name: str, variant: str, scene: SceneIR, tx_id: str | None = None) -> str:
        """Authors ``scene`` as the delta of variant ``set_name=variant`` against the base."""
        self._check_scene(scene)
        prim = self.asset_prim
        with self.transaction(tx_id) as tid:
            vset = prim.GetVariantSets().AddVariantSet(set_name)
            if not vset.AddVariant(variant):
                raise ValueError(f"cannot add variant {set_name}={variant!r}")
            previous = vset.GetVariantSelection()
            vset.SetVariantSelection(variant)  # the variant must compose to be authored into
            layer = self._txs[-1][1]
            with vset.GetVariantEditContext(layer):
                author_scene(self._stage, scene, root=prim.GetPath())
            if previous:
                vset.SetVariantSelection(previous)
            else:
                vset.ClearVariantSelection()
        return tid

    def list_variants(self, set_name: str) -> list[str]:
        vsets = self.asset_prim.GetVariantSets()
        return list(vsets.GetVariantSet(set_name).GetVariantNames()) if vsets.HasVariantSet(set_name) else []

    def _vset(self, set_name: str, variant: str) -> Usd.VariantSet:
        if variant not in self.list_variants(set_name):
            raise KeyError(f"unknown variant {set_name}={variant!r}")
        return self.asset_prim.GetVariantSets().GetVariantSet(set_name)

    def select_variant(self, set_name: str, variant: str, tx_id: str | None = None) -> str:
        vset = self._vset(set_name, variant)
        with self.transaction(tx_id) as tid:
            vset.SetVariantSelection(variant)
        return tid

    def promote_variant(self, set_name: str, variant: str, tx_id: str | None = None) -> str:
        """Makes the variant's scene the plain base scene and removes the variant set."""
        vset = self._vset(set_name, variant)
        with self.transaction(tx_id) as tid:
            vset.SetVariantSelection(variant)
            winner = self.scene_ir()
            # Other variants are deltas against the old base; drop the whole set.
            # (An authored "" selection does not hide weaker selections; a
            # listOp delete of the set name does.)
            spec = Sdf.CreatePrimInLayer(self._txs[-1][1], self.asset_prim.GetPath())
            spec.variantSetNameList.deletedItems = [*spec.variantSetNameList.deletedItems, set_name]
            if set_name in spec.variantSelections:
                del spec.variantSelections[set_name]
            author_scene(self._stage, winner, root=BASE)
        return tid

    # --- read-out ------------------------------------------------------------

    def extract_meshes(self) -> list[MeshData]:
        """Active composed meshes in world space (triangulated) for the geometry kernels."""
        xc = UsdGeom.XformCache()
        out: list[MeshData] = []
        for prim in Usd.PrimRange(self.asset_prim):  # default predicate: active, defined, concrete
            if not prim.IsA(UsdGeom.Mesh):
                continue
            mesh = UsdGeom.Mesh(prim)
            pts = mesh.GetPointsAttr().Get()
            idx = mesh.GetFaceVertexIndicesAttr().Get()
            if not pts or not idx:
                continue
            counts = np.asarray(mesh.GetFaceVertexCountsAttr().Get(), dtype=np.int64)
            tris = _triangulate(counts, np.asarray(idx, dtype=np.int64))
            m = np.array(xc.GetLocalToWorldTransform(prim))  # row-vector convention
            world = np.asarray(pts, dtype=np.float64) @ m[:3, :3] + m[3, :3]
            node = prim
            while node and not node.HasAttribute("agy:nodeId"):
                node = node.GetParent()
            node_id = node.GetAttribute("agy:nodeId").Get() if node else prim.GetName()
            collider = bool(node.GetAttribute("agy:collider").Get()) if node else True
            mat, _ = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()
            out.append(
                MeshData(
                    node_id=node_id,
                    prim_path=str(prim.GetPath()),
                    points=world,
                    triangles=tris,
                    material_id=mat.GetPrim().GetName() if mat else None,
                    collider=collider,
                )
            )
        return out

    def manifest(self) -> str:
        """Terse per-node summary of the composed scene for LLM context."""
        scene = self.scene_ir()
        root = self.asset_prim.GetPath()
        paths = node_paths(scene, root)
        tris: dict[str, int] = {}
        for md in self.extract_meshes():
            tris[md.node_id] = tris.get(md.node_id, 0) + len(md.triangles)
        cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_], useExtentsHint=False)

        def bbox(path: Sdf.Path) -> str:
            rng = cache.ComputeWorldBound(self._stage.GetPrimAtPath(path)).ComputeAlignedRange()
            return "-" if rng.IsEmpty() else f"{_vec(rng.GetMin())}..{_vec(rng.GetMax())}"

        vsets = self.asset_prim.GetVariantSets()
        sel = " ".join(f"{n}={vsets.GetVariantSelection(n)}" for n in vsets.GetNames() if vsets.GetVariantSelection(n))
        head = (
            f"# {scene.asset_name} nodes={len(scene.nodes)} mats={len(scene.materials)} "
            f"tris={sum(tris.values())} bbox={bbox(root)}" + (f" variant {sel}" if sel else "")
        )
        lines = [
            f"{n.id} {n.kind} {bbox(paths[n.id])} tris={tris.get(n.id, 0)} mat={n.material_id or '-'}"
            for n in scene.nodes
        ]
        return "\n".join([head, *lines])

    def export(self, path: Path) -> None:
        """Writes the composed scene (current variant selections) as .usda/.usdc/.usdz."""
        path = Path(path)
        suffix = path.suffix.lower()
        if suffix not in (".usda", ".usdc", ".usdz"):
            raise ValueError(f"unsupported export format {suffix!r}")
        flat = self._stage.Flatten()
        edit = Sdf.BatchNamespaceEdit()
        edit.Add(BASE, Sdf.Path.emptyPath)  # the base class is an authoring detail
        if not flat.Apply(edit):
            raise RuntimeError("failed to strip base class from export")
        if suffix != ".usdz":
            flat.Export(str(path))
            return
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / f"{path.stem}.usdc"
            flat.Export(str(src))
            if not UsdUtils.CreateNewUsdzPackage(Sdf.AssetPath(str(src)), str(path)):
                raise RuntimeError(f"usdz packaging failed for {path}")
