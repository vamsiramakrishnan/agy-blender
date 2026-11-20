from __future__ import annotations

import io
import os
from types import SimpleNamespace

import pytest
from PIL import Image

from antigravity3d.genmedia import nano_banana as nb
from antigravity3d.genmedia.nano_banana import FakeImageBackend, GenAIImageBackend, GeneratedImage, ImageForge
from antigravity3d.genmedia.pbr_deriver import seam_error


def forge(**kw) -> tuple[ImageForge, FakeImageBackend]:
    b = FakeImageBackend(seed=7)
    return ImageForge(b, **kw), b


def test_model_from_env(monkeypatch):
    monkeypatch.delenv(nb.IMAGE_MODEL_ENV, raising=False)
    assert ImageForge(FakeImageBackend()).model == nb.DEFAULT_IMAGE_MODEL
    monkeypatch.setenv(nb.IMAGE_MODEL_ENV, "my-image-model")
    assert ImageForge(FakeImageBackend()).model == "my-image-model"
    assert ImageForge(FakeImageBackend(), model="explicit").model == "explicit"
    with pytest.raises(ValueError):
        ImageForge()


async def test_ortho_sheet_prompt_template():
    f, b = forge()
    g = await f.ortho_sheet("a brass lantern", emphasis="handle proportions", style_anchor=b"png")
    p = b.calls[0]["prompt"]
    for needle in ("FRONT", "SIDE", "BACK", "TOP", "no perspective", "Flat, even lighting", "neutral", "brass lantern", "handle proportions"):
        assert needle in p or needle.lower() in p.lower()
    assert b.calls[0]["n_images"] == 1 and isinstance(g, GeneratedImage) and g.model == f.model and g.latency_ms >= 0
    assert g.image().format == "PNG" and g.uri is None and g.prompt == p


async def test_seamless_albedo_forbids_baked_light_and_tiles():
    f, b = forge()
    g = await f.seamless_albedo("mossy cobblestone", size=64, variant_hint="darker")
    assert "NO baked" in b.calls[0]["prompt"] and "darker" in b.calls[0]["prompt"]
    assert g.image().size == (64, 64) and seam_error(g.image()) < 1.5


async def test_deterministic_by_seed_and_prompt():
    a = await ImageForge(FakeImageBackend(1)).seamless_albedo("x", size=32)
    b = await ImageForge(FakeImageBackend(1)).seamless_albedo("x", size=32)
    c = await ImageForge(FakeImageBackend(2)).seamless_albedo("x", size=32)
    assert a.png_bytes == b.png_bytes != c.png_bytes


async def test_trim_decal_paint_over():
    f, b = forge()
    t = await f.trim_sheet("sci-fi panels", size=64)
    assert t.image().size == (64, 64) and "trim sheet" in b.calls[-1]["prompt"].lower()
    d = await f.decal("hazard stripes", size=64)
    im = d.image()
    assert im.mode == "RGBA" and im.getpixel((0, 0))[3] == 0 and im.getpixel((32, 32))[3] == 255
    src = (await f.seamless_albedo("x", size=32)).png_bytes
    p = await f.paint_over(src, "make it rusty")
    assert b.calls[-1]["n_images"] == 1 and "rusty" in b.calls[-1]["prompt"] and p.image().size == (32, 32)


async def test_upload_and_auto_upload(tmp_path):
    f, b = forge()
    path = tmp_path / "a.png"
    Image.new("RGB", (4, 4)).save(path)
    u1, u2 = await f.upload(path), await f.upload(path.read_bytes())
    assert u1 == u2 and u1.startswith("fake://") and b.uploads == ["a.png", None]
    f2, _ = forge(auto_upload=True)
    assert (await f2.seamless_albedo("x", size=16)).uri.startswith("fake://")


async def test_genai_backend_sdk_shape():
    """Pure plumbing check with a stub client mirroring google-genai's response shape."""
    out = io.BytesIO()
    Image.new("RGB", (2, 2), (9, 9, 9)).save(out, "PNG")
    seen = {}

    async def generate_content(*, model, contents, config):
        seen.update(model=model, contents=contents, config=config)
        part = SimpleNamespace(inline_data=SimpleNamespace(data=out.getvalue()))
        return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(inline_data=None), part]))])

    async def upload(*, file, config):
        seen["up"] = config.mime_type
        return SimpleNamespace(uri="https://files/abc")

    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content),
                                                 files=SimpleNamespace(upload=upload)))
    be = GenAIImageBackend(client)
    data = await be.generate(model="m", prompt="p", images=[b"x"], size=2048, aspect_ratio="1:1")
    assert data == out.getvalue() and seen["config"].response_modalities == ["IMAGE"]
    assert seen["config"].image_config.image_size == "2K" and len(seen["contents"]) == 2
    assert await be.upload(b"zz") == "https://files/abc" and seen["up"] == "image/png"

    async def empty(**kw):
        return SimpleNamespace(candidates=[])

    client.aio.models.generate_content = empty
    with pytest.raises(RuntimeError):
        await be.generate(model="m", prompt="p")


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("GEMINI_API_KEY"), reason="needs GEMINI_API_KEY")
async def test_live_image_generation():
    from google import genai

    g = await ImageForge(client=genai.Client()).seamless_albedo("weathered oak planks", size=1024)
    assert g.image().size[0] >= 256
