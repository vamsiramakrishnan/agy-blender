"""The comparison scores required PBR maps from their decoded GLB references, without model calls."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pygltflib as gltf
import pytest
from PIL import Image


@pytest.fixture(scope="module")
def image_eval():
    path = Path(__file__).resolve().parents[1] / "benchmarks" / "compare_eval.py"
    spec = importlib.util.spec_from_file_location("compare_eval_images", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pbr_glb(tmp_path: Path, *, broken_role: str | None = None, failure: str = "missing",
             unused_missing_image: bool = False, invalid_source: int | None = None,
             unset_source: bool = False) -> Path:
    """A real triangle GLB with independently referenced base, normal and packed metal/roughness PNGs."""
    roles = ("baseColor", "normal", "metallicRoughness")
    # Varied, nonblack colour and normal maps meet the independent blank-map rules.
    pixels = np.array([[[128, 128, 255], [192, 128, 255]],
                       [[128, 192, 255], [192, 192, 255]]], dtype=np.uint8)
    for role in roles:
        path = tmp_path / f"{role}.png"
        if role != broken_role:
            Image.fromarray(pixels).save(path)
        elif failure == "corrupt":
            path.write_bytes(b"these bytes do not decode as an image")

    positions = np.array([[-0.5, 0, 0], [0.5, 0, 0], [0, 1, 0]], dtype="<f4").tobytes()
    g = gltf.GLTF2(
        asset=gltf.Asset(version="2.0"),
        buffers=[gltf.Buffer(byteLength=len(positions))],
        bufferViews=[gltf.BufferView(buffer=0, byteOffset=0, byteLength=len(positions), target=34962)],
        accessors=[gltf.Accessor(bufferView=0, componentType=5126, count=3, type="VEC3",
                                 min=[-0.5, 0, 0], max=[0.5, 1, 0])],
        meshes=[gltf.Mesh(primitives=[gltf.Primitive(attributes=gltf.Attributes(POSITION=0), material=0)])],
        nodes=[gltf.Node(mesh=0, name="prop")], scenes=[gltf.Scene(nodes=[0])], scene=0,
        materials=[gltf.Material(pbrMetallicRoughness=gltf.PbrMetallicRoughness(
            baseColorTexture=gltf.TextureInfo(index=0), metallicRoughnessTexture=gltf.TextureInfo(index=2)),
            normalTexture=gltf.NormalMaterialTexture(index=1))],
        textures=[gltf.Texture(source=i) for i in range(3)],
        images=[gltf.Image(uri=f"{role}.png", mimeType="image/png") for role in roles],
    )
    if invalid_source is not None or unset_source:
        g.textures[1].source = invalid_source
    if unused_missing_image:
        g.images.append(gltf.Image(uri="unused-missing.png", mimeType="image/png"))
    g.set_binary_blob(positions)
    path = tmp_path / "prop.glb"
    g.save_binary(str(path))
    return path


def _scorecard(image_eval, tmp_path: Path, path: Path):
    facts = image_eval.glb_facts(path, size_m=(0.5, 1.6), max_tris=3000)
    rec = {"brief": "crate", "arm": "A", "status": "finished", "glb": str(path), "wall_s": 0,
           "model_calls": 0, "tokens": {"total": 0}, "images": 0, "pipeline": {}}
    result = image_eval.scorecard(tmp_path, {"records": [rec], "evaluation": {
        "crate:A": {"usable": True, "facts": facts}}}, {"crate": SimpleNamespace()}, "image-check")
    return facts, result["cells"]["crate:A"]


@pytest.mark.parametrize("role", ["baseColor", "normal", "metallicRoughness"])
@pytest.mark.parametrize("failure", ["missing", "corrupt"])
def test_missing_or_corrupt_required_images_fail_maps_and_need_material_repair(image_eval, tmp_path, role, failure):
    path = _pbr_glb(tmp_path, broken_role=role, failure=failure)
    facts, row = _scorecard(image_eval, tmp_path, path)
    assert facts["pbr_complete"]  # Binding presence is distinct from reading the image bytes.
    failed = next(image for image in facts["images"] if role in image["roles"])
    assert "error" in failed
    assert row["maps"]["pass"] is False
    assert row["game_ready_rate"]["pass"] is False
    assert "wire or fix the materials" in row["hand_steps"]["value"]["steps"]


def test_readable_required_images_pass_even_with_an_unused_missing_image(image_eval, tmp_path):
    path = _pbr_glb(tmp_path, unused_missing_image=True)
    facts, row = _scorecard(image_eval, tmp_path, path)
    assert "error" in facts["images"][-1] and facts["images"][-1]["roles"] == []
    assert row["maps"]["pass"] is True
    assert "wire or fix the materials" not in row["hand_steps"]["value"]["steps"]


@pytest.mark.parametrize("source", [-1, 3, None])
def test_required_texture_cannot_reference_an_absent_image(image_eval, tmp_path, source):
    path = _pbr_glb(tmp_path, invalid_source=source, unset_source=source is None)
    _, row = _scorecard(image_eval, tmp_path, path)
    assert row["maps"]["pass"] is False
    assert "wire or fix the materials" in row["hand_steps"]["value"]["steps"]


def test_invalid_texture_index_does_not_wrap_to_a_valid_texture(image_eval, tmp_path):
    path = _pbr_glb(tmp_path)
    g = gltf.GLTF2().load_binary(str(path))
    g.materials[0].normalTexture.index = -1
    g.save_binary(str(path))
    facts, row = _scorecard(image_eval, tmp_path, path)
    assert not facts["pbr_complete"]
    assert row["maps"]["pass"] is False


def test_invalid_embedded_image_view_is_recorded_as_unreadable(image_eval, tmp_path):
    path = _pbr_glb(tmp_path)
    g = gltf.GLTF2().load_binary(str(path))
    g.images[1].uri = None
    g.images[1].bufferView = 20
    g.save_binary(str(path))
    facts, row = _scorecard(image_eval, tmp_path, path)
    assert "error" in facts["images"][1]
    assert row["maps"]["pass"] is False
