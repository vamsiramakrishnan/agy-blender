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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage as ndi

# gemini-2.5-flash-image is the image-generation model the installed google-genai SDK's own
# generate_content image tests/docs use; override with AGY3D_IMAGE_MODEL (e.g. a newer image model).
DEFAULT_IMAGE_MODEL = "gemini-2.5-flash-image"
IMAGE_MODEL_ENV = "AGY3D_IMAGE_MODEL"
CHROMA_KEY = (255, 0, 255)


def default_image_model() -> str:
    return os.environ.get(IMAGE_MODEL_ENV) or DEFAULT_IMAGE_MODEL


@dataclass(frozen=True)
class GeneratedImage:
    png_bytes: bytes
    uri: str | None
    prompt: str
    model: str
    latency_ms: float

    def image(self) -> Image.Image:
        return Image.open(io.BytesIO(self.png_bytes))


class ImageBackend(Protocol):
    async def generate(self, *, model: str, prompt: str, images: Sequence[bytes] = (), size: int | None = None,
                       aspect_ratio: str = "1:1") -> bytes: ...

    async def upload(self, data: bytes, *, mime_type: str = "image/png", display_name: str | None = None) -> str: ...


def _size_tier(size: int | None) -> str | None:
    return None if size is None else "1K" if size <= 1024 else "2K" if size <= 2048 else "4K"


class GenAIImageBackend:
    """Real google-genai backend. Untested against the live API in this environment."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def generate(self, *, model: str, prompt: str, images: Sequence[bytes] = (), size: int | None = None,
                       aspect_ratio: str = "1:1") -> bytes:
        resp = await self._generate_content(model, prompt, images, _size_tier(size), aspect_ratio)
        for cand in resp.candidates or []:
            for part in (cand.content.parts if cand.content else None) or []:
                if part.inline_data is not None and part.inline_data.data:
                    return part.inline_data.data
        raise RuntimeError("image model returned no inline image data")

    async def upload(self, data: bytes, *, mime_type: str = "image/png", display_name: str | None = None) -> str:
        return (await self._files_upload(data, mime_type, display_name)).uri

    async def _generate_content(self, model: str, prompt: str, images: Sequence[bytes], image_size: str | None, aspect: str) -> Any:
        from google.genai import types

        cfg = types.GenerateContentConfig(response_modalities=["IMAGE"],
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
        n = size or 256
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
                 auto_upload: bool = False) -> None:
        if backend is None:
            if client is None:
                raise ValueError("ImageForge needs a backend or a genai client")
            backend = GenAIImageBackend(client)
        self._backend, self.model, self.auto_upload = backend, model or default_image_model(), auto_upload

    async def _run(self, prompt: str, *, images: Sequence[bytes] = (), size: int | None = None, aspect: str = "1:1",
                   key_alpha: bool = False) -> GeneratedImage:
        t0 = time.perf_counter()
        raw = await self._backend.generate(model=self.model, prompt=prompt, images=images, size=size, aspect_ratio=aspect)
        img = Image.open(io.BytesIO(raw))
        if key_alpha:
            img = _chroma_to_alpha(img)
        png = _png(img)
        uri = await self._backend.upload(png, display_name="agy3d-generated") if self.auto_upload else None
        return GeneratedImage(png, uri, prompt, self.model, (time.perf_counter() - t0) * 1000)

    async def ortho_sheet(self, prompt: str, *, emphasis: str, style_anchor: bytes | None = None) -> GeneratedImage:
        full = _ORTHO.format(prompt=prompt, emphasis=emphasis)
        if style_anchor is not None:
            full += " Match the visual style of the supplied reference image."
        return await self._run(full, images=[style_anchor] if style_anchor else (), size=2048)

    async def seamless_albedo(self, prompt: str, *, size: int = 2048, variant_hint: str = "") -> GeneratedImage:
        return await self._run(_ALBEDO.format(prompt=prompt, variant=f"Variation: {variant_hint}. " if variant_hint else ""), size=size)

    async def trim_sheet(self, prompt: str, *, size: int = 2048) -> GeneratedImage:
        return await self._run(_TRIM.format(prompt=prompt), size=size)

    async def decal(self, prompt: str, *, size: int = 1024) -> GeneratedImage:
        return await self._run(_DECAL.format(prompt=prompt), size=size, key_alpha=True)

    async def paint_over(self, render_png: bytes, direction: str) -> GeneratedImage:
        return await self._run(_PAINT.format(direction=direction), images=[render_png])

    async def upload(self, path_or_bytes: str | os.PathLike[str] | bytes, *, mime_type: str = "image/png") -> str:
        """Upload to the Files API; returns a URI reusable across turns."""
        if isinstance(path_or_bytes, (bytes, bytearray)):
            data, name = bytes(path_or_bytes), None
        else:
            p = Path(path_or_bytes)
            data, name = await asyncio.to_thread(p.read_bytes), p.name
        return await self._backend.upload(data, mime_type=mime_type, display_name=name)


def _chroma_to_alpha(img: Image.Image, tol: float = 60.0) -> Image.Image:
    a = np.asarray(img.convert("RGB"), np.float32)
    dist = np.linalg.norm(a - np.array(CHROMA_KEY, np.float32), axis=-1)
    alpha = np.clip((dist - tol * 0.5) / (tol * 0.5), 0, 1)
    return Image.fromarray(np.dstack([a, alpha * 255]).astype(np.uint8), "RGBA")
