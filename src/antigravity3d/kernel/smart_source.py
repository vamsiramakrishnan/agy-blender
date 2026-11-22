"""The generated surface as a surface source of the shared atlas (``surface_detail``): what an opaque material without
an image ships instead of its flat factors.

* Layout: an opaque material with no map file asks the layout for ``DETAIL_PX_PER_M`` (512 px/m: chips, scratches,
  grain and 12 mm stencil capitals hold at 2 mm per texel), or ``PLAIN_PX_PER_M`` for the plain and glass classes,
  read from the material's words (``MaterialParams.surface``, authored as customData agy:surface).
* Bake: the first role asked for a raster paints every such material of that atlas at once
  (``smart_materials.texture_scene``: markings are chosen across materials, the occlusion depth maps are shared), and
  the three images are kept for the other roles. The material's words come from the scene description embedded in the
  stage (``AtlasRaster.scene``); the brief is not part of a package, so the asset name stands in for it (wear and
  markings read 'RuggedMilitarySupply' as 'rugged military supply').
* Record: ``record(name)`` is what the manifest's ``texture_atlas.members[name]["generated"]`` carries: class, words
  read, wear, dirt, masks, every marking placed (strings included) and timings.

Switch: ``EngineBudget.generated_surfaces`` (``--surfaces generated|flat``; its default from AGY3D_GENERATED_SURFACES),
applied around the layout and the bake with ``using``. ``AGY3D_MARKINGS`` = full (default) | pictograms | off.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
import re
import time
from typing import Any

import numpy as np

from antigravity3d.kernel import mesh_masks as mm
from antigravity3d.kernel import smart_materials as sm

ROLES = {"albedo", "orm", "normal"}
#: Texel density (px/m) a painted class asks of the layout, and what the plain and glass classes ask (they paint only
#: occlusion, slow colour variation and crease dirt). Capped by the budget's target. Chosen on tonight's 48-part crate
#: with the comparison harness's renderer at the sheet's 330 px: 512 and 1024 read the same (edge wear, three label
#: lines of 12 mm capitals, the arrows' THIS SIDE UP); 256 loses the label's second and third lines (under 5 texels a
#: stroke stops reading, so they are dropped) and the end text. 512 makes a 2048 x 1024 atlas where 1024 made 4096 x
#: 2048 (a quarter of the texels: 2.0 MB of PNG against 6.4 MB) and the package 24 s where 1024 took 99 s, under load.
DETAIL_PX_PER_M = 512.0
PLAIN_PX_PER_M = 256.0


_ON: contextvars.ContextVar[bool] = contextvars.ContextVar("agy3d_generated_surfaces", default=False)


def enabled() -> bool:
    """Whether the current export asked for generated surfaces (``EngineBudget.generated_surfaces``, set around the
    layout and the bake by ``using``). Outside an export nothing is painted: a caller without a budget gets flat."""
    return _ON.get()


@contextlib.contextmanager
def using(on: bool):
    token = _ON.set(bool(on))
    try:
        yield
    finally:
        _ON.reset(token)


def _has_map(material: Any) -> bool:
    from antigravity3d.kernel.surface_detail import _map_file

    return any(_map_file(material, role) is not None for role in getattr(material, "maps", {}) or {})


def _paintable(material: Any) -> bool:
    return (material is not None and enabled() and getattr(material, "alpha_mode", "OPAQUE") == "OPAQUE"
            and float(getattr(material, "transmission", 0.0) or 0.0) == 0.0 and not _has_map(material))


class SmartSource:
    name = "smart"

    def __init__(self) -> None:
        self._raster_id: int | None = None
        self._maps: dict[str, sm.SmartMaps] = {}
        self._records: dict[str, dict[str, Any]] = {}
        self.timings: dict[str, Any] = {}  # the last atlas painted: where the seconds went (not in the manifest)

    # --- the layout's question
    def detail_px_per_m(self, material: Any, target_px_per_m: float) -> float | None:
        if not _paintable(material):
            return None
        cls = sm.classify(material).surface_class  # MaterialParams carries the words (customData agy:surface)
        want = PLAIN_PX_PER_M if cls in ("plain", "glass") else DETAIL_PX_PER_M
        return float(min(target_px_per_m, want))

    def roles(self, material: Any) -> set[str]:
        return set(ROLES)

    # --- the bake's question
    def texels(self, material: Any, role: str, raster: Any) -> np.ndarray | None:
        if role not in ROLES or not _paintable(material) or material.name not in raster.labels:
            return None
        if self._raster_id != id(raster):
            self._paint(raster, material)
        got = self._maps.get(material.name)
        if got is None:
            return None
        return np.asarray(getattr(got, role).convert("RGB"), np.uint8)

    def record(self, material_name: str) -> dict[str, Any] | None:
        return self._records.get(material_name)

    def _paint(self, raster: Any, asking: Any) -> None:
        """Every paintable member of this raster's atlas, at once."""
        from antigravity3d.ir import MaterialIR

        t0 = time.perf_counter()
        self._raster_id, self._maps, self._records = id(raster), {}, {}
        W, H = raster.size
        scene = getattr(raster, "scene", None)
        ir = {m.id: m for m in scene.materials} if scene is not None else {}
        groups: dict[str, list[mm.LayoutPart]] = {}
        for p in raster.parts:
            groups.setdefault(p.material, []).append(mm.LayoutPart(p.node_id, np.asarray(p.points, np.float64),
                                                                    np.asarray(p.triangles, np.int64), np.asarray(p.uvs, np.float64)))
        mats = []
        for name in sorted(groups):
            m = ir.get(name)
            if m is None:  # no scene description (an imported asset): the factors, no words, so the plain class
                m = MaterialIR(id=name, base_color=tuple(asking.base_color) if name == asking.name else (0.8, 0.8, 0.8))
            if m.albedo_uri or m.orm_uri or m.normal_uri or m.alpha_mode != "OPAQUE" or m.transmission > 0:
                continue
            mats.append(m)
        if not mats:
            return
        layouts = [mm.AtlasLayout(m.id, W, H, tuple(groups[m.id])) for m in mats]
        meshes = list(getattr(raster, "meshes", ()) or ())
        if not meshes:  # the shipped triangles stand in for the working meshes (contact faces then read as open rims)
            from antigravity3d.kernel.types import MeshData

            meshes = [MeshData(p.node_id, f"/{p.node_id}", p.points, p.triangles, p.material) for p in raster.parts]
        asset = getattr(raster, "asset_name", "") or (scene.asset_name if scene is not None else "")
        mode = os.environ.get("AGY3D_MARKINGS", "full").strip().lower() or "full"
        brief = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", asset).replace("_", " ")   # the name stands in for the brief
        self._maps = sm.texture_scene(mats, layouts, meshes, brief=brief, asset_name=asset, markings=mode)
        # the manifest gets what was decided, never how long it took: two builds of one design are the same bytes
        volatile = {"timings_ms", "occluder_ms"}
        self.timings = {"atlas_px": [W, H], "total_ms": round((time.perf_counter() - t0) * 1e3, 1), "occluder_ms": None}
        for name, mp in self._maps.items():
            self._records[name] = {k: v for k, v in mp.record.items() if k not in volatile}
            self.timings[name] = mp.record.get("timings_ms")
            self.timings["occluder_ms"] = mp.record.get("occluder_ms")


def placed_strings(manifest: dict) -> list[str]:
    """Every string the generated surface painted on a package, in order, from its manifest (for the visual QA
    request: decisions.engine.evaluate_visual_qa ``placed_markings``)."""
    out: list[str] = []
    for m in ((manifest.get("texture_atlas") or {}).get("members") or {}).values():
        for d in (m.get("generated") or {}).get("markings") or []:
            if d.get("placed"):
                out += [x["text"] for x in d.get("strings") or [] if x.get("legible")]
    return list(dict.fromkeys(out))


SOURCE = SmartSource()
