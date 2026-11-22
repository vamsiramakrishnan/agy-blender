"""Merkle-cache wrappers for the expensive pure steps.

* image generation   keyed by (op, prompt/emphasis, style-anchor digest, model, backend seed)
* PBR derivation     keyed by (albedo digest, derive params)
* UV + LOD + colliders keyed by the ``ir_subtree_key`` of every root node + budget + backend version
* authored designs   keyed by the whole authoring request (``DesignRecord``): a rerun continues from the design it paid for

A hit credits the saved USD and the *measured* compute time of the original miss (stored beside the blob).
Uploads are never cached: Files-API URIs expire, so a hit re-uploads the cached bytes.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

from antigravity3d.genmedia.nano_banana import GeneratedImage
from antigravity3d.genmedia.usage import ImageReceipt
from antigravity3d.genmedia.pbr_deriver import PBRMaps, derive_pbr
from antigravity3d.ir import IRPatch, SceneIR
from antigravity3d.kernel.cache import BuildCache, ir_subtree_key, node_key, stable_hash
from antigravity3d.kernel.coplanar import opaque_materials
from antigravity3d.kernel.fast_ops import LOD, ExportBundle, Hull, UVResult, run_export_pipeline
from antigravity3d.kernel.types import MeshData
from antigravity3d.state import EngineBudget
from antigravity3d.tactics.common import to_thread_drained
from antigravity3d.tactics.governor import Governor
from antigravity3d.tactics.common import load_png, png_bytes

# 2: analytic UV charts, screen-space-error LODs; 3: parts absent at far LODs; 4: exact removal bracket;
# 5: computed UV charts scaled to their surface area and placed by the rectangle packer;
# 6: collider input with coplanar triangles merged, closed bodies of one mesh decomposed separately;
# 7: computed UV charts re-flattened (as rigid as possible) and all counter-clockwise;
# 8: collider input repaired before decomposition (welded, holes capped, bodies pulled apart), open surfaces made solid;
# 9: the copies of a UV-seam vertex share one position in LOD levels (they could differ by rounding noise);
# 10: the bundle records which triangles are hidden contact faces (per part and per level) and packs the atlas
#     without the charts that never ship; a collision hull over its engine's limit is replaced by an enclosing one
# 11: opaque materials share one UV atlas whose side is chosen from need (fast_ops.ATLAS_SIDES), not 2048 per material
BACKEND_VERSION = "kernel-fast_ops/11"
CACHED_TAG = "(cached)"  # appended to GeneratedImage.model on a cache hit


def image_identity(g: GeneratedImage, *, prompt: str, size: int | None = None, variant: str = "", offline: bool = False) -> dict[str, Any]:
    """Who made an image, for the package manifest's ``provenance`` (kernel/provenance.py): identities and a hash, no
    time and no cost. The same for the run that paid for the image and a rerun that took it from this cache: the model
    is the one the service was asked for (the cache's mark is dropped), ``response_id`` is the paying call's (the cache
    keeps it beside the blob), and ``prompt`` is the one the caller asked with (the forge's own wording of it may
    differ between the two). An image of the offline stand-in says so and names no model."""
    return {"kind": "offline_fake_image" if offline else "model_generated_image",
            "model": "offline fake image backend" if offline else g.model.replace(f" {CACHED_TAG}", ""),
            "prompt": prompt, "size": size, "variant": variant, "image_sha256": _digest(g.png_bytes),
            "response_id": getattr(g.receipt, "response_id", None)}


def _meta_key(key: str) -> str:
    return key + ".meta"


def _saved_ms(cache: BuildCache, key: str) -> float:
    raw = cache.store.get(_meta_key(key))
    return float(json.loads(raw)["ms"]) if raw else 0.0


def _record_ms(cache: BuildCache, key: str, t0: float) -> None:
    cache.store.put(_meta_key(key), json.dumps({"ms": (time.perf_counter() - t0) * 1e3}).encode())


def cached_bytes(cache: BuildCache, key: str, compute: Callable[[], bytes], *, cost: float = 0.0) -> tuple[bytes, bool]:
    def run() -> bytes:
        t0 = time.perf_counter()
        data = compute()
        _record_ms(cache, key, t0)
        return data

    return cache.get_or_compute(key, run, saved_cost_usd=cost, saved_ms=_saved_ms(cache, key))


def _digest(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- image generation


class CachedForge:
    """Drop-in for ``ImageForge`` whose generation calls go through the build cache."""

    def __init__(self, forge: Any, cache: BuildCache, *, seed: int | None = None, budget: EngineBudget | None = None) -> None:
        self._forge, self.cache = forge, cache
        self._seed = seed if seed is not None else int(getattr(getattr(forge, "_backend", None), "seed", 0))
        self.model, self.budget = forge.model, budget

    def __getattr__(self, name: str) -> Any:  # upload, auto_upload, ...
        return getattr(self._forge, name)

    async def upload(self, path_or_bytes: Any, **kw: Any) -> str:
        """The forge's Files-API upload; the URI is noted with the digest of its bytes (``BuildCache.upload_digest``)."""
        uri = await self._forge.upload(path_or_bytes, **kw)
        if isinstance(path_or_bytes, (bytes, bytearray)):
            self.cache.note_upload(uri, bytes(path_or_bytes))
        return uri

    def with_governor(self, governor: Governor) -> CachedForge:
        return CachedForge(self._forge.with_governor(governor), self.cache, seed=self._seed, budget=self.budget)

    def _key(self, op: str, key_args: dict[str, Any]) -> str:
        return node_key(f"img.{op}", inputs={**key_args, "budget": self.budget, "seed": self._seed},
                        backend_version=f"{self._forge.cache_namespace}|{self.model}")

    def has_trim_sheet(self, prompt: str, *, size: int = 2048) -> bool:
        """Whether :meth:`trim_sheet` with these arguments would be served from the cache (no call, no image)."""
        return self.cache.store.get(self._key("trim", {"prompt": prompt, "size": size})) is not None

    def has_ortho(self, prompt: str, *, emphasis: str, style_anchor: bytes | None = None) -> bool:
        """Whether :meth:`ortho_sheet` with these arguments would be served from the cache (see :meth:`has_albedo`)."""
        return self.cache.store.get(self._key("ortho", {"prompt": prompt, "emphasis": emphasis, "anchor": _digest(style_anchor)})) is not None

    def has_albedo(self, prompt: str, *, size: int = 2048, variant_hint: str = "") -> bool:
        """Whether :meth:`seamless_albedo` with these arguments would be served from the cache (no call, no image, no
        budget): a rerun's allocation uses it, so that images the earlier run paid for are not charged to the rerun's
        image limit."""
        return self.cache.store.get(self._key("albedo", {"prompt": prompt, "size": size, "variant": variant_hint})) is not None

    async def _gen(self, op: str, key_args: dict[str, Any], call: Callable[[], Awaitable[GeneratedImage]]) -> GeneratedImage:
        key = self._key(op, key_args)
        fresh: list[GeneratedImage] = []

        async def compute() -> bytes:
            g = await call()
            fresh.append(g)
            if g.uri:  # uploaded by the forge itself (auto_upload): a request that references it is keyed by its bytes
                self.cache.note_upload(g.uri, g.png_bytes)
            meta = {"ms": g.latency_ms, "usd": g.receipt.booked_usd, "response_id": g.receipt.response_id}
            await to_thread_drained(self.cache.store.put, _meta_key(key), json.dumps(meta).encode())
            return g.png_bytes

        data, hit = await self.cache.aget_or_compute(key, compute)
        if not hit:
            return fresh[0]
        meta_raw = await asyncio.to_thread(self.cache.store.get, _meta_key(key))
        meta = json.loads(meta_raw) if meta_raw else {}
        self.cache.credit_savings(cost_usd=meta.get("usd", 0), ms=meta.get("ms", 0))
        receipt = ImageReceipt(self.model, op, "cache", 0.0, artifact_sha256=_digest(data), response_id=meta.get("response_id"))
        self.governor.reserve("image." + op, 0.0).settle(0.0, receipt=asdict(receipt))
        uri = await self.upload(data) if self.auto_upload else None
        return GeneratedImage(data, uri, str(key_args.get("prompt", "")), f"{self.model} {CACHED_TAG}", 0.0, receipt)

    async def ortho_sheet(self, prompt: str, *, emphasis: str, style_anchor: bytes | None = None) -> GeneratedImage:
        return await self._gen("ortho", {"prompt": prompt, "emphasis": emphasis, "anchor": _digest(style_anchor)},
                               lambda: self._forge.ortho_sheet(prompt, emphasis=emphasis, style_anchor=style_anchor))

    async def seamless_albedo(self, prompt: str, *, size: int = 2048, variant_hint: str = "") -> GeneratedImage:
        return await self._gen("albedo", {"prompt": prompt, "size": size, "variant": variant_hint},
                               lambda: self._forge.seamless_albedo(prompt, size=size, variant_hint=variant_hint))

    async def trim_sheet(self, prompt: str, *, size: int = 2048) -> GeneratedImage:
        return await self._gen("trim", {"prompt": prompt, "size": size}, lambda: self._forge.trim_sheet(prompt, size=size))

    async def decal(self, prompt: str, *, size: int = 2048) -> GeneratedImage:
        return await self._gen("decal", {"prompt": prompt, "size": size}, lambda: self._forge.decal(prompt, size=size))

    async def paint_over(self, render_png: bytes, direction: str) -> GeneratedImage:
        return await self._gen("paintover", {"render": _digest(render_png), "prompt": direction},
                               lambda: self._forge.paint_over(render_png, direction))


# --------------------------------------------------------------------------- authored designs

DESIGN_RECORD_VERSION = "design-record/1"


@dataclass(frozen=True)
class RecordedAnswer:
    answer: SceneIR | IRPatch
    key: str
    original: dict[str, Any]  # what the original call cost: unit, booked, usage, model, role, operation


def request_digest(req: Any, index: int | None, upload_digest: Callable[[str], str | None] | None = None) -> str:
    """Hash of a whole authoring request: the content of :meth:`DesignRecord.key` (which is ``"design-" +`` this), for
    the manifest's provenance, where a run with no design record needs it too. ``upload_digest``: ``BuildCache.upload_digest``
    (a file reference counts by the bytes uploaded under it); without it a reference stays as it is."""
    def content(part: dict[str, Any]) -> dict[str, Any]:
        uri = (part.get("file_data") or {}).get("file_uri")
        digest = upload_digest(uri) if uri and upload_digest else None
        return part if digest is None else {**part, "file_data": {**part["file_data"], "file_uri": None, "uploaded_sha256": digest}}

    contents = [{**c, "parts": [content(p) for p in c.get("parts") or []]}
                for c in (x.model_dump(mode="json", exclude_none=True) for x in req.contents)]
    config = req.config.model_dump(mode="json", exclude_none=True, exclude={"http_options"})
    return stable_hash({"v": DESIGN_RECORD_VERSION, "model": req.model, "role": req.role, "operation": req.operation,
                        "contents": contents, "config": config, "index": index})


class DesignRecord:
    """Every answered authoring request of a run (a design, or a repair), beside the image cache, so a rerun with the
    same ``--out`` continues from the design it paid for instead of buying a different one.

    The key is the whole request (``tactics.metered_author.AuthorRequest``): model, role, operation, contents (the rules
    text, budget, task with its brief, art direction and attached references) and generation config (thinking level,
    output cap, response schema), plus the candidate's index. Any change to any of them misses by construction. A file
    reference counts by the sha256 of the bytes uploaded under it (a Files-API URI changes with every upload); a URI
    not uploaded through this cache stays in the key as it is (a safe miss). The value is the answer after the
    project's own parsing and what the original call cost.

    ``fresh`` (``--new-design``): nothing is read, and every new answer replaces the recorded one, so the next rerun
    continues from it. The image cache is untouched either way.
    """

    def __init__(self, cache: BuildCache, *, fresh: bool = False) -> None:
        self.cache, self.fresh = cache, fresh
        self.reused: list[dict[str, Any]] = []  # one entry per answer taken from the record in this run
        self.recorded = 0  # answers written in this run
        self._lock = threading.Lock()

    def _content(self, part: dict[str, Any]) -> dict[str, Any]:
        uri = (part.get("file_data") or {}).get("file_uri")
        digest = self.cache.upload_digest(uri) if uri else None
        if digest is None:
            return part
        return {**part, "file_data": {**part["file_data"], "file_uri": None, "uploaded_sha256": digest}}

    def key(self, req: Any, index: int | None) -> str:
        contents = [{**c, "parts": [self._content(p) for p in c.get("parts") or []]}
                    for c in (x.model_dump(mode="json", exclude_none=True) for x in req.contents)]
        # http_options (the timeout, the SDK's own retries) changes how the request is sent, not what it asks
        config = req.config.model_dump(mode="json", exclude_none=True, exclude={"http_options"})
        return "design-" + stable_hash({"v": DESIGN_RECORD_VERSION, "model": req.model, "role": req.role,
                                        "operation": req.operation, "contents": contents, "config": config,
                                        "index": index})

    def get(self, key: str) -> RecordedAnswer | None:
        if self.fresh:
            return None
        raw = self.cache.store.get(key)
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            model = {"SceneIR": SceneIR, "IRPatch": IRPatch}[data["type"]]
            return RecordedAnswer(model.model_validate(data["answer"]), key, dict(data["original"]))
        except Exception:  # noqa: BLE001 - a record this version cannot read is a miss, never a failure
            return None

    def put(self, key: str, answer: SceneIR | IRPatch, original: dict[str, Any]) -> None:
        kind = "SceneIR" if isinstance(answer, SceneIR) else "IRPatch"  # IRPatch is ir.SceneIRDiff
        data = {"v": DESIGN_RECORD_VERSION, "type": kind, "answer": answer.model_dump(mode="json"),
                "original": original}
        self.cache.store.put(key, json.dumps(data, sort_keys=True).encode())
        with self._lock:
            self.recorded += 1

    def note_reuse(self, hit: RecordedAnswer) -> None:
        with self._lock:
            self.reused.append({"operation": hit.original.get("operation"), "key": hit.key[:23], "original": hit.original})

    def summary(self) -> dict[str, Any]:
        """What this run took from the record and what it added; ``original_booked``: what the reused answers cost
        when they were bought, by unit (nothing of it is booked again)."""
        by_unit: dict[str, float] = {}
        for r in self.reused:
            o = r["original"]
            if o.get("booked") is not None:
                by_unit[o.get("unit") or "?"] = by_unit.get(o.get("unit") or "?", 0.0) + float(o["booked"])
        ops = [r["operation"] for r in self.reused]
        # an improvement (pass A's improvement rounds) is a patch like a repair and is counted with them
        return {"reused": len(self.reused), "reused_designs": ops.count("propose"),
                "reused_repairs": ops.count("repair") + ops.count("improve"),
                "recorded": self.recorded, "new_design": self.fresh, "original_booked": by_unit}

    def line(self) -> str:
        return self.summary_line(self.summary())

    @staticmethod
    def summary_line(s: dict[str, Any]) -> str:
        """The run summary's line for :meth:`summary` (also read back from a report)."""
        cost = ", ".join(f"{v:,.0f} tokens" if u == "tokens" else f"${v:.4f}" if u == "usd" else f"{v} {u}"
                         for u, v in s["original_booked"].items())
        return (f"design record: {s['reused_designs']} design(s) and {s['reused_repairs']} repair(s) reused, no call made"
                + (f" (originally {cost}, not booked again)" if cost else "") + f"; {s['recorded']} recorded"
                + ("; --new-design: the record was not read" if s["new_design"] else ""))


# --------------------------------------------------------------------------- PBR derivation


def cached_derive_pbr(cache: BuildCache, albedo_png: bytes, *, metallic_hint: float = 0.0, normal_strength: float = 2.0,
                      convention: str = "opengl", tileable: bool = True, prepare: bool = False) -> tuple[PBRMaps, bool]:
    params = {"metallic": metallic_hint, "normal_strength": normal_strength, "convention": convention, "tileable": tileable, "prepare": prepare}
    key = node_key("pbr.derive", inputs={"albedo": _digest(albedo_png), **params}, backend_version="pbr_deriver/2")

    def compute() -> bytes:
        m = derive_pbr(load_png(albedo_png), metallic_hint=metallic_hint, normal_strength=normal_strength,
                       convention=convention, tileable=tileable, prepare=prepare)  # type: ignore[arg-type]
        buf = io.BytesIO()
        np.savez(buf, albedo=np.frombuffer(png_bytes(m.albedo), np.uint8), orm=np.frombuffer(png_bytes(m.orm), np.uint8),
                 normal=np.frombuffer(png_bytes(m.normal), np.uint8))
        return buf.getvalue()

    data, hit = cached_bytes(cache, key, compute)
    with np.load(io.BytesIO(data), allow_pickle=False) as z:
        return PBRMaps(*(load_png(z[k].tobytes()) for k in ("albedo", "orm", "normal"))), hit


# --------------------------------------------------------------------------- UV / LOD / colliders


def export_key(scene: SceneIR, budget: EngineBudget, meshes: list[MeshData] | None = None) -> str:
    # UVs/LODs/colliders do not depend on texture files, whose (workdir-specific) paths would defeat reuse
    scene = scene.model_copy(update={"materials": [m.model_copy(update={"albedo_uri": None, "orm_uri": None, "normal_uri": None}) for m in scene.materials]})
    roots = sorted(n.id for n in scene.nodes if n.parent is None)
    inputs = {"asset": scene.asset_name, "budget": asdict(budget)}
    if meshes is not None and any(n.kind == "mesh_ref" for n in scene.nodes):
        # The same external path can resolve to different geometry in another stage/run.
        inputs["composed_meshes"] = [
            (m.node_id, m.material_id, m.collider, hashlib.sha256(np.ascontiguousarray(m.points, dtype="<f8")).hexdigest(),
             hashlib.sha256(np.ascontiguousarray(m.triangles, dtype="<i4")).hexdigest()) for m in meshes]
    if meshes is not None:
        # the shared atlas is laid out for the detail its materials carry (surface_detail): a map's size, not its path
        from antigravity3d.kernel import smart_source, surface_detail

        seen = {}
        with smart_source.using(budget.generated_surfaces):  # (also in the key through asdict(budget) above)
            for m in meshes:
                if m.material_id not in seen:
                    d = surface_detail.detail_px_per_m(m.material, budget.texel_density_px_per_m)
                    seen[m.material_id] = round(d, 3) if d else None
        inputs["layout_detail_px_per_m"] = sorted(seen.items(), key=lambda kv: str(kv[0]))
    return node_key("export.uv_lod_collider", inputs=inputs,
                    upstream=[ir_subtree_key(scene, r) for r in roots], backend_version=BACKEND_VERSION)


def _bundle_to_bytes(b: ExportBundle) -> bytes:
    arrays: dict[str, np.ndarray] = {}
    meta: dict[str, Any] = {"asset": b.asset_name, "timings": b.timings_ms, "uvs": [], "lods": {}, "hulls": []}
    for i, u in enumerate(b.uvs):
        arrays.update({f"uv{i}_p": u.points, f"uv{i}_t": u.triangles, f"uv{i}_uv": u.uvs, f"uv{i}_vm": u.vmapping})
        if u.hidden is not None:
            arrays[f"uv{i}_h"] = u.hidden
        meta["uvs"].append({k: v for k, v in asdict(u).items() if not isinstance(v, np.ndarray)})
    for nid, lods in b.lods.items():
        meta["lods"][nid] = []
        for j, lod in enumerate(lods):
            arrays.update({f"lod_{nid}_{j}_p": lod.points, f"lod_{nid}_{j}_t": lod.triangles})
            if lod.uvs is not None:
                arrays[f"lod_{nid}_{j}_uv"] = lod.uvs
            if lod.hidden is not None:
                arrays[f"lod_{nid}_{j}_h"] = lod.hidden
            meta["lods"][nid].append({"ratio": lod.ratio, "error": lod.error, "error_m": lod.error_m, "uv": lod.uvs is not None,
                                      "screen_fraction": lod.screen_fraction})
    for i, h in enumerate(b.colliders):
        arrays.update({f"hull{i}_p": h.points, f"hull{i}_t": h.triangles})
        meta["hulls"].append({"name": h.name, "volume": h.volume, "source": h.source, "reason": h.reason})
    buf = io.BytesIO()
    np.savez(buf, __meta__=np.frombuffer(json.dumps(meta, default=float).encode(), np.uint8), **arrays)
    return buf.getvalue()


def _bundle_from_bytes(data: bytes) -> ExportBundle:
    with np.load(io.BytesIO(data), allow_pickle=False) as z:
        meta = json.loads(z["__meta__"].tobytes())
        uvs = [UVResult(points=z[f"uv{i}_p"], triangles=z[f"uv{i}_t"], uvs=z[f"uv{i}_uv"], vmapping=z[f"uv{i}_vm"],
                        hidden=z[f"uv{i}_h"] if f"uv{i}_h" in z.files else None,
                        **{k: v for k, v in m.items() if k not in ("points", "triangles", "uvs", "vmapping", "hidden")})
               for i, m in enumerate(meta["uvs"])]
        lods = {nid: [LOD(m["ratio"], z[f"lod_{nid}_{j}_p"], z[f"lod_{nid}_{j}_t"], z[f"lod_{nid}_{j}_uv"] if m["uv"] else None, m["error"], m["error_m"],
                          m.get("screen_fraction", 1.0), z[f"lod_{nid}_{j}_h"] if f"lod_{nid}_{j}_h" in z.files else None)
                      for j, m in enumerate(ms)] for nid, ms in meta["lods"].items()}
        hulls = [Hull(m["name"], z[f"hull{i}_p"], z[f"hull{i}_t"], m["volume"], m["source"], m.get("reason", "")) for i, m in enumerate(meta["hulls"])]
    return ExportBundle(meta["asset"], uvs, lods, hulls, dict(meta["timings"]))


def cached_export_pipeline(cache: BuildCache, scene: SceneIR, meshes: list[MeshData], budget: EngineBudget) -> tuple[ExportBundle, bool]:
    """``run_export_pipeline`` behind the cache. The key covers IR content, so call it with meshes of that IR."""
    opaque = opaque_materials(scene)  # the materials are in the key (ir_subtree_key hashes the ones a node uses)
    data, hit = cached_bytes(cache, export_key(scene, budget, meshes),
                             lambda: _bundle_to_bytes(run_export_pipeline(meshes, budget, scene.asset_name, opaque_materials=opaque)))
    return _bundle_from_bytes(data), hit


__all__ = ["CachedForge", "DesignRecord", "RecordedAnswer", "cached_derive_pbr", "cached_export_pipeline", "export_key", "image_identity",
           "stable_hash"]
