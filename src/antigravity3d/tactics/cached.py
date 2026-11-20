"""Merkle-cache wrappers for the expensive pure steps.

* image generation   keyed by (op, prompt/emphasis, style-anchor digest, model, backend seed)
* PBR derivation     keyed by (albedo digest, derive params)
* UV + LOD + colliders keyed by the ``ir_subtree_key`` of every root node + budget + backend version

A hit credits the saved USD and the *measured* compute time of the original miss (stored beside the blob).
Uploads are never cached: Files-API URIs expire, so a hit re-uploads the cached bytes.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any

import numpy as np

from antigravity3d.genmedia.nano_banana import GeneratedImage
from antigravity3d.genmedia.usage import ImageReceipt
from antigravity3d.genmedia.pbr_deriver import PBRMaps, derive_pbr
from antigravity3d.ir import SceneIR
from antigravity3d.kernel.cache import BuildCache, ir_subtree_key, node_key, stable_hash
from antigravity3d.kernel.fast_ops import LOD, ExportBundle, Hull, UVResult, run_export_pipeline
from antigravity3d.kernel.types import MeshData
from antigravity3d.state import EngineBudget
from antigravity3d.tactics.governor import Governor
from antigravity3d.tactics.common import load_png, png_bytes

BACKEND_VERSION = "kernel-fast_ops/1"
CACHED_TAG = "(cached)"  # appended to GeneratedImage.model on a cache hit


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

    def with_governor(self, governor: Governor) -> CachedForge:
        return CachedForge(self._forge.with_governor(governor), self.cache, seed=self._seed, budget=self.budget)

    async def _gen(self, op: str, key_args: dict[str, Any], call: Callable[[], Awaitable[GeneratedImage]]) -> GeneratedImage:
        key = node_key(f"img.{op}", inputs={**key_args, "budget": self.budget, "seed": self._seed},
                       backend_version=f"{self._forge.cache_namespace}|{self.model}")
        fresh: list[GeneratedImage] = []

        async def compute() -> bytes:
            g = await call()
            fresh.append(g)
            meta = {"ms": g.latency_ms, "usd": g.receipt.booked_usd, "response_id": g.receipt.response_id}
            await asyncio.to_thread(self.cache.store.put, _meta_key(key), json.dumps(meta).encode())
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


def export_key(scene: SceneIR, budget: EngineBudget) -> str:
    # UVs/LODs/colliders do not depend on texture files, whose (workdir-specific) paths would defeat reuse
    scene = scene.model_copy(update={"materials": [m.model_copy(update={"albedo_uri": None, "orm_uri": None, "normal_uri": None}) for m in scene.materials]})
    roots = sorted(n.id for n in scene.nodes if n.parent is None)
    return node_key("export.uv_lod_collider", inputs={"asset": scene.asset_name, "budget": asdict(budget)},
                    upstream=[ir_subtree_key(scene, r) for r in roots], backend_version=BACKEND_VERSION)


def _bundle_to_bytes(b: ExportBundle) -> bytes:
    arrays: dict[str, np.ndarray] = {}
    meta: dict[str, Any] = {"asset": b.asset_name, "timings": b.timings_ms, "uvs": [], "lods": {}, "hulls": []}
    for i, u in enumerate(b.uvs):
        arrays.update({f"uv{i}_p": u.points, f"uv{i}_t": u.triangles, f"uv{i}_uv": u.uvs, f"uv{i}_vm": u.vmapping})
        meta["uvs"].append({k: v for k, v in asdict(u).items() if not isinstance(v, np.ndarray)})
    for nid, lods in b.lods.items():
        meta["lods"][nid] = []
        for j, lod in enumerate(lods):
            arrays.update({f"lod_{nid}_{j}_p": lod.points, f"lod_{nid}_{j}_t": lod.triangles})
            if lod.uvs is not None:
                arrays[f"lod_{nid}_{j}_uv"] = lod.uvs
            meta["lods"][nid].append({"ratio": lod.ratio, "error": lod.error, "error_m": lod.error_m, "uv": lod.uvs is not None})
    for i, h in enumerate(b.colliders):
        arrays.update({f"hull{i}_p": h.points, f"hull{i}_t": h.triangles})
        meta["hulls"].append({"name": h.name, "volume": h.volume, "source": h.source})
    buf = io.BytesIO()
    np.savez(buf, __meta__=np.frombuffer(json.dumps(meta, default=float).encode(), np.uint8), **arrays)
    return buf.getvalue()


def _bundle_from_bytes(data: bytes) -> ExportBundle:
    with np.load(io.BytesIO(data), allow_pickle=False) as z:
        meta = json.loads(z["__meta__"].tobytes())
        uvs = [UVResult(points=z[f"uv{i}_p"], triangles=z[f"uv{i}_t"], uvs=z[f"uv{i}_uv"], vmapping=z[f"uv{i}_vm"],
                        **{k: v for k, v in m.items() if k not in ("points", "triangles", "uvs", "vmapping")}) for i, m in enumerate(meta["uvs"])]
        lods = {nid: [LOD(m["ratio"], z[f"lod_{nid}_{j}_p"], z[f"lod_{nid}_{j}_t"], z[f"lod_{nid}_{j}_uv"] if m["uv"] else None, m["error"], m["error_m"])
                      for j, m in enumerate(ms)] for nid, ms in meta["lods"].items()}
        hulls = [Hull(m["name"], z[f"hull{i}_p"], z[f"hull{i}_t"], m["volume"], m["source"]) for i, m in enumerate(meta["hulls"])]
    return ExportBundle(meta["asset"], uvs, lods, hulls, dict(meta["timings"]))


def cached_export_pipeline(cache: BuildCache, scene: SceneIR, meshes: list[MeshData], budget: EngineBudget) -> tuple[ExportBundle, bool]:
    """``run_export_pipeline`` behind the cache. The key covers IR content, so call it with meshes of that IR."""
    data, hit = cached_bytes(cache, export_key(scene, budget), lambda: _bundle_to_bytes(run_export_pipeline(meshes, budget, scene.asset_name)))
    return _bundle_from_bytes(data), hit


__all__ = ["CachedForge", "cached_derive_pbr", "cached_export_pipeline", "export_key", "stable_hash"]
