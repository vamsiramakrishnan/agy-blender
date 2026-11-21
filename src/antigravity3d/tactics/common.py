"""Small shared helpers for the Layer-4 tactics: image I/O, uploading, cost constants."""

from __future__ import annotations

import asyncio
import base64
import io
from collections.abc import Awaitable, Callable
from typing import Any

import numpy as np
from PIL import Image

Uploader = Callable[[bytes], Awaitable[str]]

# Published standard-tier 2K image-output cost, used for candidate planning only.
# ImageForge separately reserves the capped input/output cost and reconciles provider usage.
IMAGE_USD = dict.fromkeys(("ortho", "albedo", "trim", "decal", "paintover"), 0.0504)
# The same planning figures for a token ledger (Governor(token_ceiling=...)), from measured provider usage
# (benchmarks/results/round1/live.json): one 2K image 62 input + 2,001 candidate + 804 thinking tokens; one low-resolution
# grading call on the generative fallback 2,312 input + 1,006 output; one visual-QA round (lookdev 5,381 + 1,715, plus a
# locate call like a grading call). Sizing only: admission always reserves the counted worst case.
IMAGE_TOKENS_EST = 2_867
GRADE_TOKENS_EST = 3_318
QA_TOKENS_EST = 10_414


async def to_thread_drained(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """``asyncio.to_thread`` that, when its caller is cancelled (a stopped DAG node), waits for the thread to finish
    before letting the cancellation through: a thread cannot be interrupted, so nothing it does (a cache write, a
    derivation) may outlive the step that reports itself stopped."""
    work = asyncio.ensure_future(asyncio.to_thread(fn, *args, **kwargs))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        try:
            await work
        finally:
            raise


def png_bytes(img: Image.Image | np.ndarray) -> bytes:
    im = Image.fromarray(img) if isinstance(img, np.ndarray) else img
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def load_png(data: bytes) -> Image.Image:
    im = Image.open(io.BytesIO(data))
    im.load()
    return im


async def data_uri_uploader(data: bytes) -> str:
    """Last-resort uploader (inline data URI); the real Decisions API wants Files API URIs."""
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def uploader_for(forge: Any | None) -> Uploader:
    """Files-API upload through the forge when there is one, else inline data URIs."""
    return (lambda data: forge.upload(data)) if forge is not None else data_uri_uploader
