"""kernel/gltf_writer.py: GLBs embed already-encoded images verbatim, once, and stay valid glTF 2.0."""

from __future__ import annotations

import hashlib
import io
import json
import struct
from pathlib import Path

import numpy as np
import pytest
import trimesh
from PIL import Image

from antigravity3d.ir import MaterialIR, NodeIR, SceneIR
from antigravity3d.kernel import gltf_writer as gw
from antigravity3d.kernel.fast_ops import run_export_pipeline, write_gltf
from antigravity3d.kernel.package import write_engine_package
from antigravity3d.kernel.usd_stage import StageManager
from antigravity3d.kernel.validator import validate_package
from antigravity3d.state import EngineBudget


def _png(seed: int, size: int = 16, mode: str = "RGB", **save) -> bytes:
    rng = np.random.default_rng(seed)
    shape = (size, size) if mode in ("L", "I;16") else (size, size, len(mode))
    arr = rng.integers(0, 255 if mode != "I;16" else 65535, shape, dtype=np.uint8 if mode != "I;16" else np.uint16)
    buf = io.BytesIO()
    (Image.fromarray(arr) if mode in ("L", "I;16") else Image.fromarray(arr, mode)).save(buf, "PNG", **save)
    return buf.getvalue()


def parse(blob: bytes) -> tuple[dict, bytes]:
    magic, version, total = struct.unpack_from("<4sII", blob, 0)
    assert (magic, version, total) == (b"glTF", 2, len(blob))
    jlen, jtype = struct.unpack_from("<I4s", blob, 12)
    assert jtype == b"JSON" and jlen % 4 == 0
    doc = json.loads(blob[20:20 + jlen])
    blen, btype = struct.unpack_from("<I4s", blob, 20 + jlen)
    assert btype == b"BIN\0" and blen % 4 == 0 and 28 + jlen + blen == len(blob)
    return doc, blob[28 + jlen:]


def view_bytes(doc: dict, binary: bytes, index: int) -> bytes:
    v = doc["bufferViews"][index]
    return binary[v["byteOffset"]:v["byteOffset"] + v["byteLength"]]


def tri(offset: float = 0.0) -> gw.GltfMesh:
    pos = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0.5 + offset]], dtype=np.float64)
    return gw.GltfMesh("tri", pos, np.array([[0, 1, 2]]), np.array([[0, 0], [1, 0], [0, 1]], dtype=np.float64))


def pbr_material(images: dict[str, bytes]) -> gw.GltfMaterial:
    refs = {k: gw.TextureRef(gw.EncodedImage.from_bytes(v, k)) for k, v in images.items()}
    return gw.GltfMaterial(name="paint", base_color_texture=refs["albedo"], normal_texture=refs["normal"],
                           occlusion_texture=refs["orm"], metallic_roughness_texture=refs["orm"])


# --------------------------------------------------------------------------- writer unit tests


def test_embedded_bytes_equal_source_bytes_for_awkward_pngs():
    # 16-bit grey, palette, RGBA with a gamma chunk: a decode + RGB convert would change every one of these
    sources = {
        "albedo": _png(1, mode="RGBA", gamma=1 / 2.2),
        "normal": _png(2, mode="I;16"),
        "orm": _png(3, mode="L", optimize=True),
    }
    m = tri()
    m.material = pbr_material(sources)
    doc, binary = parse(gw.build_glb([m]))
    for slot, key in ((doc["materials"][0]["pbrMetallicRoughness"]["baseColorTexture"], "albedo"),
                      (doc["materials"][0]["normalTexture"], "normal"),
                      (doc["materials"][0]["occlusionTexture"], "orm")):
        image = doc["images"][doc["textures"][slot["index"]]["source"]]
        assert image["mimeType"] == "image/png"
        assert view_bytes(doc, binary, image["bufferView"]) == sources[key]


def test_one_image_per_distinct_texture_and_orm_shared_by_both_slots():
    albedo, normal, orm = _png(1), _png(2), _png(3)
    a, b = tri(), tri(0.1)
    b.name = "tri2"
    a.material = pbr_material({"albedo": albedo, "normal": normal, "orm": orm})
    # a second material object with the same files, plus the same material object bound twice
    other = pbr_material({"albedo": albedo, "normal": normal, "orm": orm})
    other.name = "paint2"
    c = tri(0.2)
    c.name = "tri3"
    b.material, c.material = a.material, other
    doc, binary = parse(gw.build_glb([a, b, c]))
    assert len(doc["images"]) == 3 and len(doc["textures"]) == 3
    assert [m["name"] for m in doc["materials"]] == ["paint", "paint2"]  # a material bound twice is one material
    m = doc["materials"][0]
    assert m["occlusionTexture"]["index"] == m["pbrMetallicRoughness"]["metallicRoughnessTexture"]["index"]
    assert sorted(len(view_bytes(doc, binary, i["bufferView"])) for i in doc["images"]) == sorted(map(len, (albedo, normal, orm)))


def test_alignment_and_accessor_ranges():
    odd = [_png(i, size=5 + i) for i in range(4)]  # byte lengths that are not multiples of 4
    m = tri()
    m.material = gw.GltfMaterial(name="m", base_color_texture=gw.TextureRef(gw.EncodedImage.from_bytes(odd[0])),
                                 normal_texture=gw.TextureRef(gw.EncodedImage.from_bytes(odd[1])),
                                 occlusion_texture=gw.TextureRef(gw.EncodedImage.from_bytes(odd[2])))
    blob = gw.build_glb([m, tri()])
    doc, binary = parse(blob)
    assert len(blob) % 4 == 0 and doc["buffers"][0]["byteLength"] == len(binary)
    for v in doc["bufferViews"]:
        assert v["byteOffset"] % 4 == 0 and v["byteOffset"] + v["byteLength"] <= len(binary)
    assert {v.get("target") for v in doc["bufferViews"] if "target" in v} == {34962, 34963}
    pos = doc["accessors"][doc["meshes"][0]["primitives"][0]["attributes"]["POSITION"]]
    assert pos["min"] == [0.0, 0.0, 0.0] and pos["max"] == [1.0, 1.0, 0.5]


def test_deterministic_bytes():
    sources = {"albedo": _png(1), "normal": _png(2), "orm": _png(3)}
    def make():
        m = tri()
        m.material = pbr_material(sources)
        return gw.build_glb([m, tri(0.3)])
    assert make() == make()
    assert b"pygltflib" not in make()  # no library version in the bytes


def test_jpeg_is_embedded_unchanged_and_other_formats_become_png_once():
    buf = io.BytesIO()
    Image.fromarray(np.full((8, 8, 3), 90, np.uint8)).save(buf, "JPEG")
    jpeg = buf.getvalue()
    img = gw.EncodedImage.from_bytes(jpeg)
    assert img.mime_type == "image/jpeg" and img.data == jpeg and not img.transcoded
    buf = io.BytesIO()
    Image.fromarray(np.full((8, 8, 3), 90, np.uint8)).save(buf, "BMP")
    bmp = gw.EncodedImage.from_bytes(buf.getvalue())
    assert bmp.mime_type == "image/png" and bmp.data.startswith(gw.PNG_MAGIC) and bmp.transcoded
    m = tri()
    m.material = gw.GltfMaterial(name="m", base_color_texture=gw.TextureRef(img))
    doc, binary = parse(gw.build_glb([m]))
    assert doc["images"][0]["mimeType"] == "image/jpeg"
    assert view_bytes(doc, binary, doc["images"][0]["bufferView"]) == jpeg


def test_round_trips_through_trimesh():
    albedo = _png(1)
    m = tri()
    m.material = gw.GltfMaterial(name="paint", base_color_texture=gw.TextureRef(gw.EncodedImage.from_bytes(albedo)),
                                 metallic_factor=0.25, roughness_factor=0.75)
    scene = trimesh.load(io.BytesIO(gw.build_glb([m, tri(0.5)])), file_type="glb", force="scene")
    geom = scene.geometry["tri"]
    np.testing.assert_allclose(geom.vertices, m.positions, atol=1e-6)
    np.testing.assert_array_equal(geom.faces, [[0, 1, 2]])
    mat = geom.visual.material
    assert mat.name == "paint" and mat.metallicFactor == 0.25 and mat.roughnessFactor == 0.75
    np.testing.assert_array_equal(np.asarray(mat.baseColorTexture), np.asarray(Image.open(io.BytesIO(albedo))))


def test_write_gltf_keeps_y_up_and_trimesh_uv_convention(tmp_path):
    pts = np.array([[0, 0, 0], [1, 0, 0], [0, 2, 3]], dtype=np.float64)  # Z-up kernel coordinates
    uvs = np.array([[0.1, 0.2], [0.9, 0.2], [0.5, 0.8]])
    lod = type("L", (), {"points": pts, "triangles": np.array([[0, 1, 2]]), "uvs": uvs})()
    path = write_gltf(tmp_path / "a.glb", [lod], names=["n"])
    doc, binary = parse(path.read_bytes())
    prim = doc["meshes"][0]["primitives"][0]
    read = lambda i: np.frombuffer(view_bytes(doc, binary, doc["accessors"][i]["bufferView"]), np.float32)
    np.testing.assert_allclose(read(prim["attributes"]["POSITION"]).reshape(-1, 3), [[0, 0, 0], [1, 0, 0], [0, 3, -2]])
    np.testing.assert_allclose(read(prim["attributes"]["TEXCOORD_0"]).reshape(-1, 2), uvs * [1, -1] + [0, 1], atol=1e-6)
    assert doc["materials"][doc["meshes"][0]["primitives"][0]["material"]]["name"] == "Default"
    loaded = trimesh.load(path, force="scene").geometry["n"]  # trimesh undoes the V flip: what went in comes out
    np.testing.assert_allclose(loaded.visual.uv, uvs, atol=1e-6)
    assert doc["nodes"][0]["name"] == "n" and doc["meshes"][0]["name"] == "n"


def test_geometry_without_uvs_has_no_material(tmp_path):
    lod = type("L", (), {"points": np.eye(3), "triangles": np.array([[0, 1, 2]])})()
    doc, _ = parse(write_gltf(tmp_path / "c.glb", [lod], names=["UCX_a_00"]).read_bytes())
    assert "materials" not in doc and "TEXCOORD_0" not in doc["meshes"][0]["primitives"][0]["attributes"]


def test_khr_extension_textures_share_the_image_table():
    orm = gw.TextureRef(gw.EncodedImage.from_bytes(_png(3), "orm"))
    cc = gw.TextureRef(gw.EncodedImage.from_bytes(_png(4), "cc"), scale=0.5, extensions={"KHR_texture_transform": {"scale": [2, 2]}})
    m = tri()
    m.material = gw.GltfMaterial(
        name="m", occlusion_texture=orm, metallic_roughness_texture=orm,
        extensions={"KHR_materials_clearcoat": {"clearcoatFactor": 1.0, "clearcoatTexture": cc, "clearcoatRoughnessTexture": orm}})
    doc, _ = parse(gw.build_glb([m], extensions_required=[]))
    ext = doc["materials"][0]["extensions"]["KHR_materials_clearcoat"]
    assert len(doc["images"]) == 2 and len(doc["textures"]) == 2
    assert ext["clearcoatRoughnessTexture"]["index"] == doc["materials"][0]["occlusionTexture"]["index"]
    assert ext["clearcoatTexture"]["scale"] == 0.5 and ext["clearcoatTexture"]["extensions"]["KHR_texture_transform"] == {"scale": [2, 2]}
    assert doc["extensionsUsed"] == ["KHR_materials_clearcoat", "KHR_texture_transform"] and "extensionsRequired" not in doc


def test_bad_geometry_is_rejected():
    bad = tri()
    bad.triangles = np.array([[0, 1, 7]])
    with pytest.raises(ValueError, match="outside"):
        gw.build_glb([bad])
    with pytest.raises(ValueError, match="no geometry"):
        gw.build_glb([gw.GltfMesh("e", np.zeros((0, 3)), np.zeros((0, 3), int))])


# --------------------------------------------------------------------------- package level

ASSET = "Prop"


def _package(tmp: Path, engine: str, textures: Path):
    maps = {}
    rng = np.random.default_rng(5)
    for k in ("albedo", "orm", "normal"):
        Image.fromarray(rng.integers(0, 255, (48, 48, 3), dtype=np.uint8)).save(textures / f"{k}.png")
        maps[f"{k}_uri"] = str(textures / f"{k}.png")
    scene = SceneIR(asset_name=ASSET, materials=[MaterialIR(id="paint", normal_convention="opengl", **maps)],
                    nodes=[NodeIR(id="a", kind="box", params={"size": (1, 1, 1)}, material_id="paint"),
                           NodeIR(id="b", kind="sphere", params={"radius": 0.3}, material_id="paint")])
    sm = StageManager.create(tmp / "work.usda", ASSET)
    sm.apply_ir(scene)
    budget = EngineBudget(engine)
    bundle = run_export_pipeline(sm.extract_meshes(), budget, ASSET)
    return write_engine_package(sm, bundle, budget, tmp / engine, asset_name=ASSET, qa={"tier1_ok": True})


@pytest.mark.parametrize("engine", ["unreal_5", "godot_4"])
def test_package_glbs_embed_the_texture_files_byte_for_byte(tmp_path, engine, monkeypatch):
    textures = tmp_path / "src"
    textures.mkdir()
    encodes = []
    real_save = Image.Image.save
    monkeypatch.setattr(Image.Image, "save", lambda self, *a, **k: (encodes.append(1), real_save(self, *a, **k))[1])
    pm = _package(tmp_path, engine, textures)
    assert pm.ok and validate_package(pm.out_dir) == []
    on_disk = {r["role"]: (pm.out_dir / r["usd_path"]).read_bytes() for r in pm.data["textures"]}
    # the only PNG encodes are the source-file fixtures (3) and, for a DirectX engine, the converted normal map (1)
    assert len(encodes) == 3 + (1 if engine == "unreal_5" else 0)
    glbs = [p for p in sorted(pm.out_dir.glob("*.glb")) if "colliders" not in p.stem]
    assert glbs
    for glb in glbs:
        doc, binary = parse(glb.read_bytes())
        assert len(doc["images"]) == 3 and len(doc["textures"]) == 3  # ORM once, not twice
        mat = doc["materials"][0]
        slots = {"albedo": mat["pbrMetallicRoughness"]["baseColorTexture"], "normal": mat["normalTexture"],
                 "orm": mat["occlusionTexture"]}
        assert mat["pbrMetallicRoughness"]["metallicRoughnessTexture"]["index"] == slots["orm"]["index"]
        for role, slot in slots.items():
            image = doc["images"][doc["textures"][slot["index"]]["source"]]
            got = view_bytes(doc, binary, image["bufferView"])
            assert hashlib.sha256(got).digest() == hashlib.sha256(on_disk[role]).digest()
