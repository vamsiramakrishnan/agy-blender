"""Small shared helpers for the Layer-4 tactics: image I/O, uploading, cost constants."""

from __future__ import annotations

import base64
import io
from collections.abc import Awaitable, Callable
from typing import Any

import numpy as np
from PIL import Image

Uploader = Callable[[bytes], Awaitable[str]]

# Notional generation prices (USD) used for governor budgeting; the real backend reports no cost.
IMAGE_USD = {"ortho": 0.04, "albedo": 0.04, "trim": 0.04, "decal": 0.03, "paintover": 0.04}


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
