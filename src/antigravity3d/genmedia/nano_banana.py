"""ImageForge: image generation for blueprints, textures, trim sheets, decals and paint-overs.

Only `GenAIImageBackend._generate_content` and `._files_upload` touch the google-genai SDK;
everything else depends on the small `ImageBackend` protocol (see `FakeImageBackend`).
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import time
from copy import copy
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Protocol, Sequence

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage as ndi

from antigravity3d import service_answer
from antigravity3d.genmedia.usage import ImageRates, ImageReceipt
from antigravity3d.models import IMAGE, tier
from antigravity3d.tactics.governor import Governor, plan_for

DEFAULT_IMAGE_MODEL = IMAGE
IMAGE_MODEL_ENV = "AGY3D_IMAGE_MODEL"
CHROMA_KEY = (255, 0, 255)


def default_image_model() -> str:
    return tier("image").model


@dataclass(frozen=True)
class GeneratedImage:
    png_bytes: bytes
    uri: str | None
    prompt: str
    model: str
    latency_ms: float
    receipt: ImageReceipt | None = None

    def image(self) -> Image.Image:
        return Image.open(io.BytesIO(self.png_bytes))


class ImageBackend(Protocol):
    async def generate(self, *, model: str, prompt: str, images: Sequence[bytes] = (), size: int | None = None,
                       aspect_ratio: str = "1:1") -> bytes: ...

    async def upload(self, data: bytes, *, mime_type: str = "image/png", display_name: str | None = None) -> str: ...


# Output tokens of one image as reported by the provider (candidates_tokens_details, modality IMAGE): 1K 1,120 and
# 2K 1,680 (measured 2026-11-20). 4K was not measured; 4,096 is a bound (2.4x the 2K figure), not an observation.
IMAGE_OUTPUT_TOKENS = {"1K": 1_120, "2K": 1_680, "4K": 4_096}
# Thinking plus unitemized candidate tokens per image request: observed 1,038 / 919 / 984 (max 1,038); 2.47x headroom.
IMAGE_NON_IMAGE_OUTPUT_ALLOWANCE = 2_560
# Retries of an image attempt whose outcome is unknown (a broken connection, a 500): each keeps its reservation AND one
# image of the run's hard image limit, so one. An attempt the service said it did not serve books nothing, counts no image
# and follows the shared schedule (service_answer.DEFAULT_POLICY).
IMAGE_UNKNOWN_RETRIES = 1


class ImageNotReturned(RuntimeError):
    """The provider answered without an image (MAX_TOKENS, a safety stop, no candidate). Carries what was billed."""

    def __init__(self, message: str, *, finish_reason: str | None, usage: dict[str, Any] | None) -> None:
        super().__init__(message)
        self.finish_reason, self.usage = finish_reason, usage

    @classmethod
    def from_response(cls, resp: Any) -> ImageNotReturned:
        cands = getattr(resp, "candidates", None) or []
        fr = getattr(cands[0], "finish_reason", None) if cands else None
        finish = str(getattr(fr, "name", fr)) if fr is not None else ("NO_CANDIDATE" if not cands else None)
        u = getattr(resp, "usage_metadata", None)
        usage = None if u is None else {k: getattr(u, k, None) for k in ("prompt_token_count", "candidates_token_count",
                                                                        "thoughts_token_count", "total_token_count")}
        return cls(f"image model returned no inline image data (finish_reason {finish}; usage {usage})",
                   finish_reason=finish, usage=usage)


def _size_tier(size: int | None) -> str | None:
    return None if size is None else "1K" if size <= 1024 else "2K" if size <= 2048 else "4K"


class GenAIImageBackend:
    """Transport for Nano Banana generateContent and Files API calls."""

    def __init__(self, client: Any, governor: Governor | None = None) -> None:
        self._client = client
        self.governor = governor or Governor(plan_for(2))

    async def generate(self, *, model: str, prompt: str, images: Sequence[bytes] = (), size: int | None = None,
                       aspect_ratio: str = "1:1") -> bytes:
        forge = ImageForge(self, model=model, governor=self.governor)
        result = await forge._run("image", prompt, images=images, size=size, aspect=aspect_ratio)
        return result.png_bytes

    @staticmethod
    def _extract(resp: Any) -> bytes:
        for cand in resp.candidates or []:
            for part in (cand.content.parts if cand.content else None) or []:
                if not getattr(part, "thought", False) and part.inline_data is not None and part.inline_data.data:
                    return part.inline_data.data
        raise ImageNotReturned.from_response(resp)

    async def upload(self, data: bytes, *, mime_type: str = "image/png", display_name: str | None = None) -> str:
        return (await self._files_upload(data, mime_type, display_name)).uri

    @staticmethod
    def output_limit(image_size: str | None) -> int:
        """``max_output_tokens`` for one image: the image's own tokens plus room for thinking and other output.

        Thinking tokens count against ``max_output_tokens`` on gemini-nano-banana-2.1 just as on the text models.
        Measured 2026-11-20 (seamless albedo prompt, 62 input tokens), usage_metadata per response:
          1K  cap 2,048: no image; 276 candidate + 762 thinking tokens; a 1,120-token image did not fit in the 1,010 left.
          1K  cap 2,048: image; 1,120 IMAGE + 311 unitemized candidate + 608 thinking = 2,039 (99.6% of the cap).
          2K  cap 3,072: image; 1,680 IMAGE + 277 unitemized candidate + 707 thinking = 2,664 (87% of the cap).
        """
        tier = image_size or "1K"
        return IMAGE_OUTPUT_TOKENS[tier] + IMAGE_NON_IMAGE_OUTPUT_ALLOWANCE

    async def input_bound(self, model: str, prompt: str, images: Sequence[bytes]) -> int:
        if not images:
            return len(prompt.encode("utf-8")) + 64
        from google.genai import types

        # Ask the model's tokenizer about multimodal input before admitting paid generation.
        parts = [prompt, *(types.Part.from_bytes(data=b, mime_type="image/png") for b in images)]
        count = await self._client.aio.models.count_tokens(model=model, contents=parts)
        if count.total_tokens is None or count.total_tokens < 0:
            raise RuntimeError("cannot admit image generation without a multimodal input token count")
        return count.total_tokens + 64

    async def _generate_content(self, model: str, prompt: str, images: Sequence[bytes], image_size: str | None, aspect: str) -> Any:
        from google.genai import types

        cfg = types.GenerateContentConfig(response_modalities=["IMAGE"], candidate_count=1,
                                          max_output_tokens=self.output_limit(image_size),
                                          http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=1)),
                                          image_config=types.ImageConfig(aspect_ratio=aspect, image_size=image_size))
        parts: list[Any] = [prompt, *(types.Part.from_bytes(data=b, mime_type="image/png") for b in images)]
        return await self._client.aio.models.generate_content(model=model, contents=parts, config=cfg)

    async def _files_upload(self, data: bytes, mime_type: str, display_name: str | None) -> Any:
        from google.genai import types

        return await self._client.aio.files.upload(
            file=io.BytesIO(data), config=types.UploadFileConfig(mime_type=mime_type, display_name=display_name))


def _seed(*parts: object) -> int:
    return int.from_bytes(hashlib.sha256("|".join(map(str, parts)).encode()).digest()[:4], "big")


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


class FakeImageBackend:
    """Deterministic offline backend: tileable procedural textures, drawn 4-view orthos."""

    def __init__(self, seed: int = 0) -> None:
        self.seed, self.calls, self.uploads = seed, [], []

    async def generate(self, *, model: str, prompt: str, images: Sequence[bytes] = (), size: int | None = None,
                       aspect_ratio: str = "1:1") -> bytes:
        self.calls.append({"model": model, "prompt": prompt, "n_images": len(images), "size": size})
        return await asyncio.to_thread(self._draw, prompt, images, size or 256)

    def _draw(self, prompt: str, images: Sequence[bytes], n: int) -> bytes:
        rng = np.random.default_rng(_seed(self.seed, prompt))
        low = prompt.lower()
        if "orthographic" in low:
            return _png(self._ortho(n, rng))
        if images:  # paint-over: tint the input deterministically
            src = np.asarray(Image.open(io.BytesIO(images[0])).convert("RGB"), np.float32)
            return _png(Image.fromarray(np.clip(src * (0.8 + 0.4 * rng.random(3)), 0, 255).astype(np.uint8)))
        if "chroma" in low or "decal" in low:
            img = Image.new("RGB", (n, n), CHROMA_KEY)
            d = ImageDraw.Draw(img)
            d.ellipse([n // 4, n // 4, 3 * n // 4, 3 * n // 4], fill=tuple(int(x) for x in rng.integers(0, 200, 3)))
            return _png(img)
        tint = 0.35 + 0.5 * rng.random(3)
        field = sum(a * ndi.gaussian_filter(rng.standard_normal((n, n)), s, mode="wrap") / 1.0 for s, a in ((1.5, 0.5), (n / 16, 1.0)))
        field = (field - field.min()) / (np.ptp(field) + 1e-9)
        return _png(Image.fromarray((np.clip(field[..., None] * 0.6 + 0.2, 0, 1) * tint * 255 / 0.85).clip(0, 255).astype(np.uint8)))

    @staticmethod
    def _ortho(n: int, rng: np.random.Generator) -> Image.Image:
        img = Image.new("RGB", (n, n), (200, 200, 200))
        d, h = ImageDraw.Draw(img), n // 2
        w, t = int(h * (0.4 + 0.3 * rng.random())), int(h * (0.4 + 0.3 * rng.random()))
        for i, (bw, bh) in enumerate(((w, t), (t, t), (w, t), (w, w))):  # front, side, back, top
            cx, cy = (i % 2) * h + h // 2, (i // 2) * h + h // 2
            d.rectangle([cx - bw // 2, cy - bh // 2, cx + bw // 2, cy + bh // 2], fill=(90, 110, 140), outline=(30, 30, 30))
        return img

    async def upload(self, data: bytes, *, mime_type: str = "image/png", display_name: str | None = None) -> str:
        self.uploads.append(display_name)
        return f"fake://files/{hashlib.sha1(data).hexdigest()[:12]}"


_ORTHO = (
    "Create a four-view orthographic blueprint sheet of: {prompt}. Layout: FRONT, SIDE, BACK and TOP views "
    "arranged in a 2x2 grid, each exactly axis-aligned. Strictly orthographic: no perspective, no foreshortening. "
    "Flat, even lighting with no cast shadows. Plain neutral grey background. Consistent scale and alignment "
    "between views. Emphasis: {emphasis}."
)
_ALBEDO = (
    "Seamless tileable albedo (base colour) texture of: {prompt}. {variant}Flat, evenly diffused lighting: NO baked "
    "directional light, NO shadows, NO highlights, NO ambient occlusion, no vignetting. The edges must wrap "
    "seamlessly. Square, top-down, no perspective, no text or logos."
)
_TRIM = (
    "Game trim sheet texture atlas of: {prompt}. Horizontal tileable strips of varied height stacked vertically, "
    "each a distinct material or architectural trim, flat lighting with no baked shadows, no text or logos."
)
_DECAL = (
    "A single decal graphic of: {prompt}. Centred, isolated on a solid flat chroma magenta (#FF00FF) background, "
    "flat lighting, crisp edges, no shadows, no text."
)
_PAINT = "Edit the supplied render: {direction}. Keep the object's geometry, silhouette and framing unchanged."


class ImageForge:
    def __init__(self, backend: ImageBackend | None = None, *, client: Any = None, model: str | None = None,
                 auto_upload: bool = False, governor: Governor | None = None, max_request_usd: float | None = None) -> None:
        if backend is None:
            if client is None:
                raise ValueError("ImageForge needs a backend or a genai client")
            backend = GenAIImageBackend(client, governor)
        self._backend, self.model, self.auto_upload = backend, model or default_image_model(), auto_upload
        self.governor = governor or (backend.governor if isinstance(backend, GenAIImageBackend) else Governor(plan_for(2)))
        self.max_request_usd = max_request_usd
        self.retry: service_answer.RetryPolicy | None = None  # None: the shared schedule, read at call time

    def with_governor(self, governor: Governor) -> ImageForge:
        bound = copy(self)
        bound.governor = governor
        return bound

    @property
    def cache_namespace(self) -> str:
        return f"{type(self._backend).__module__}.{type(self._backend).__qualname__}/templates-v2"

    @property
    def offline(self) -> bool:
        return isinstance(self._backend, FakeImageBackend)

    async def _run(self, op: str, prompt: str, *, images: Sequence[bytes] = (), size: int | None = None, aspect: str = "1:1",
                   key_alpha: bool = False) -> GeneratedImage:
        start = time.perf_counter()
        backend = self._backend
        sdk = isinstance(backend, GenAIImageBackend)
        tokens = self.governor.unit == "tokens"  # a token ledger: the worst case is counted input + output cap, unpriced
        rates = ImageRates.for_model(self.model) if sdk and not tokens else None
        size_tier = _size_tier(size)
        policy = self.retry or service_answer.DEFAULT_POLICY.with_unknown_retries(IMAGE_UNKNOWN_RETRIES)
        tracker = service_answer.Tracker(policy)
        if sdk:
            while True:  # counting is free: a count turned away, or whose connection broke, is asked again
                sent = time.perf_counter()
                try:
                    inputs = await backend.input_bound(self.model, prompt, images)
                    break
                except Exception as exc:
                    outcome = service_answer.classify(exc, time.perf_counter() - sent)
                    if outcome == "final" or not await tracker.failed(exc, time.perf_counter() - sent, "not_served"):
                        raise
            limit = inputs + backend.output_limit(size_tier) if tokens else rates.ceiling(inputs, backend.output_limit(size_tier))
        elif self.offline:
            limit = 0.0
        elif tokens:
            raise ValueError("a custom image backend cannot be admitted against a token ceiling: it reports no tokens")
        else:
            if self.max_request_usd is None:
                raise ValueError("a custom image backend requires a contracted max_request_usd for admission")
            limit = self.max_request_usd
        while True:
            receipt = ImageReceipt(self.model, op, "offline" if self.offline else "reservation_estimate", 0.0 if tokens else limit,
                                   status="dispatched", booked_tokens=int(limit) if tokens else None)
            extra: dict[str, Any] = {"attempt": tracker.n, "service_retries": tracker.record()} if tracker.attempts else {}
            # One image per call against the ledger's image limit (when it has one); kept unless the call is known to
            # have produced no image.
            hold = self.governor.reserve("image." + op, limit, receipt={**receipt.to_dict(), **extra}, images=1)
            if sdk:
                sent = time.perf_counter()
                try:
                    response = await backend._generate_content(self.model, prompt, images, size_tier, aspect)
                except BaseException as exc:
                    elapsed = time.perf_counter() - sent
                    outcome = service_answer.classify(exc, elapsed)
                    failed = replace(receipt, status=type(exc).__name__).to_dict()
                    if outcome == "not_served":  # the service said it did not run it: no tokens, no image
                        hold.settle(0.0, receipt={**failed, **extra, **({"booked_tokens": 0} if tokens else {"booked_usd": 0.0}),
                                                  "source": "service_answer", **service_answer.not_served_receipt(exc, tracker.n, elapsed)},
                                    images=0)
                    else:  # it may have run: the reservation and the image are kept
                        hold.settle(receipt.booked_tokens if tokens else receipt.booked_usd, images=receipt.generated_image,
                                    receipt={**failed, **extra, **({"service_answer": service_answer.describe(exc)} if isinstance(exc, Exception) else {})})
                    if isinstance(exc, Exception) and outcome != "final" and await tracker.failed(exc, elapsed, outcome):
                        continue
                    raise
            break
        try:
            if sdk:
                receipt = (ImageReceipt.from_response_tokens(response, model=self.model, op=op, limit_tokens=int(limit)) if tokens
                           else ImageReceipt.from_response(response, model=self.model, op=op, rates=rates, limit=limit))
                raw = backend._extract(response)
            else:
                raw = await backend.generate(model=self.model, prompt=prompt, images=images, size=size, aspect_ratio=aspect)
            png, width, height = await asyncio.to_thread(_final_png, raw, key_alpha)
            receipt = replace(receipt, width=width, height=height)
            if sdk and aspect == "1:1" and size in (1024, 2048, 4096) and (width, height) != (size, size):
                raise ValueError(f"provider returned {width}x{height} for a {size}x{size} image")
            receipt = replace(receipt, status="ok", artifact_sha256=hashlib.sha256(png).hexdigest())
        except BaseException as exc:
            receipt = replace(receipt, status=type(exc).__name__)
            raise
        finally:
            hold.settle(receipt.booked_tokens if tokens else receipt.booked_usd, receipt={**receipt.to_dict(), **extra},
                        images=receipt.generated_image)
        uri = await backend.upload(png, display_name="agy3d-generated") if self.auto_upload else None
        return GeneratedImage(png, uri, prompt, self.model, (time.perf_counter() - start) * 1000, receipt)

    async def ortho_sheet(self, prompt: str, *, emphasis: str, style_anchor: bytes | None = None) -> GeneratedImage:
        full = _ORTHO.format(prompt=prompt, emphasis=emphasis)
        if style_anchor is not None:
            full += " Match the visual style of the supplied reference image."
        return await self._run("ortho", full, images=[style_anchor] if style_anchor else (), size=2048)

    async def seamless_albedo(self, prompt: str, *, size: int = 2048, variant_hint: str = "") -> GeneratedImage:
        return await self._run("albedo", _ALBEDO.format(prompt=prompt, variant=f"Variation: {variant_hint}. " if variant_hint else ""), size=size)

    async def trim_sheet(self, prompt: str, *, size: int = 2048) -> GeneratedImage:
        return await self._run("trim", _TRIM.format(prompt=prompt), size=size)

    async def decal(self, prompt: str, *, size: int = 2048) -> GeneratedImage:
        return await self._run("decal", _DECAL.format(prompt=prompt), size=size, key_alpha=True)

    async def paint_over(self, render_png: bytes, direction: str) -> GeneratedImage:
        return await self._run("paintover", _PAINT.format(direction=direction), images=[render_png], size=2048)

    async def upload(self, path_or_bytes: str | os.PathLike[str] | bytes, *, mime_type: str = "image/png") -> str:
        """Upload to the Files API; returns a URI reusable across turns."""
        if isinstance(path_or_bytes, (bytes, bytearray)):
            data, name = bytes(path_or_bytes), None
        else:
            p = Path(path_or_bytes)
            data, name = await asyncio.to_thread(p.read_bytes), p.name
        return await self._backend.upload(data, mime_type=mime_type, display_name=name)


def _final_png(raw: bytes, key_alpha: bool) -> tuple[bytes, int, int]:
    with Image.open(io.BytesIO(raw)) as img:
        if key_alpha:
            return _png(_chroma_to_alpha(img)), img.width, img.height
        if img.format == "PNG":
            img.verify()
            return raw, img.width, img.height
        return _png(img), img.width, img.height


def _chroma_to_alpha(img: Image.Image, tol: float = 60.0) -> Image.Image:
    a = np.asarray(img.convert("RGB"), np.float32)
    dist = np.linalg.norm(a - np.array(CHROMA_KEY, np.float32), axis=-1)
    alpha = np.clip((dist - tol * 0.5) / (tol * 0.5), 0, 1)
    return Image.fromarray(np.dstack([a, alpha * 255]).astype(np.uint8), "RGBA")
