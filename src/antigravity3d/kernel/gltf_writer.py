"""GLB writer that embeds already-encoded images as they are.

Why this exists: ``trimesh.Scene.export`` accepts only decoded PIL images and PNG-encodes every one of them
again for every GLB it writes (and once more per material slot that uses it). A package with four GLB levels,
two materials and 2K maps therefore spent most of its wall time in the PNG encoder, and the embedded bytes were
never the bytes of the texture files that sit next to the GLBs. This writer takes ``EncodedImage`` objects
(the bytes of a PNG or JPEG file) and copies them into the binary chunk: nothing is decoded or re-encoded, so the
embedded image is bit-identical to its source file, and the cost scales with the bytes copied.

Design
------
* Pure glTF data in, bytes out. ``GltfMesh`` carries geometry that is already in glTF convention (Y-up
  positions, UVs with the origin at the top left). ``z_up_to_y_up`` and ``flip_v`` do the two conversions the
  kernel needs; ``fast_ops.write_gltf`` applies them.
* One ``GltfMaterial`` object is one glTF material, however many meshes bind it. Images are de-duplicated by
  content digest (one image per distinct file in a GLB), and textures by (image, sampler), so a texture used by
  two slots (the packed ORM map is both ``occlusionTexture`` and ``metallicRoughnessTexture``) is stored once.
* Extension point for ``KHR_materials_*``: ``GltfMaterial.extensions`` maps an extension name to a dict, and
  any ``TextureRef`` inside that dict (at any depth) is replaced with ``{"index": ..., "texCoord": ...}`` after
  the texture has been registered, so ``KHR_materials_clearcoat`` / ``_sheen`` / ``_specular`` ... textures
  share the image and texture tables with the core slots. ``extensionsUsed`` is derived from what is present;
  ``extensions_required`` names the ones a reader must understand.
* Deterministic: buffers, images, textures, materials and JSON keys appear in first-use order, with a fixed
  ``asset.generator`` (no library version is written), so equal input gives byte-identical files.

JSON and schema objects come from ``pygltflib`` (typed glTF 2.0 dataclasses); the container framing and the
buffer layout are done here because both must be exactly controlled (4-byte alignment, deterministic order).
"""

from __future__ import annotations

import hashlib
import io
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pygltflib as gl

GENERATOR = "antigravity-3d"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"
REPEAT = 10497  # sampler default wrap mode
_ARRAY_BUFFER, _ELEMENT_ARRAY_BUFFER = 34962, 34963


# --------------------------------------------------------------------------- inputs


@dataclass(frozen=True)
class EncodedImage:
    """The bytes of a PNG or JPEG file, embedded verbatim. ``digest`` identifies the content."""

    data: bytes = field(repr=False, compare=False)
    mime_type: str
    digest: str
    name: str | None = field(default=None, compare=False)
    transcoded: bool = field(default=False, compare=False)

    @classmethod
    def from_bytes(cls, data: bytes, name: str | None = None) -> EncodedImage:
        """Wraps PNG/JPEG bytes unchanged. Any other format is decoded and PNG-encoded once (``transcoded``)."""
        data = bytes(data)
        if data.startswith(PNG_MAGIC):
            mime = "image/png"
        elif data.startswith(JPEG_MAGIC):
            mime = "image/jpeg"
        else:  # glTF allows only PNG and JPEG; this is the one place pixels are re-encoded
            from PIL import Image

            with Image.open(io.BytesIO(data)) as im:
                im.load()
                buf = io.BytesIO()
                im.convert("RGBA" if "A" in im.getbands() or "transparency" in im.info else "RGB").save(buf, "PNG")
            return cls(buf.getvalue(), "image/png", hashlib.sha256(buf.getvalue()).hexdigest(), name, True)
        return cls(data, mime, hashlib.sha256(data).hexdigest(), name)

    @classmethod
    def from_file(cls, path: str | Path) -> EncodedImage:
        p = Path(path)
        return cls.from_bytes(p.read_bytes(), p.stem)


@dataclass(frozen=True)
class Sampler:
    """glTF sampler. ``None`` filters leave the choice to the engine; wrap defaults to REPEAT like glTF."""

    mag_filter: int | None = None
    min_filter: int | None = None
    wrap_s: int = REPEAT
    wrap_t: int = REPEAT


@dataclass(frozen=True)
class TextureRef:
    """A texture slot: the image, an optional sampler, the UV set, and slot parameters.

    ``scale`` is ``normalTexture.scale`` (also used by extension normal textures); ``strength`` is
    ``occlusionTexture.strength``. ``extensions`` is for texture-info extensions such as ``KHR_texture_transform``.
    """

    image: EncodedImage
    sampler: Sampler | None = None
    tex_coord: int = 0
    scale: float | None = None
    strength: float | None = None
    extensions: Mapping[str, Any] | None = None


@dataclass(eq=False)
class GltfMaterial:
    """One glTF material (identity semantics: reuse the object to share the material between meshes)."""

    name: str | None = None
    base_color_factor: Sequence[float] = (1.0, 1.0, 1.0, 1.0)
    metallic_factor: float = 1.0
    roughness_factor: float = 1.0
    base_color_texture: TextureRef | None = None
    metallic_roughness_texture: TextureRef | None = None
    normal_texture: TextureRef | None = None
    occlusion_texture: TextureRef | None = None
    emissive_texture: TextureRef | None = None
    emissive_factor: Sequence[float] | None = None
    alpha_mode: str | None = None  # None keeps the glTF default (OPAQUE) implicit
    alpha_cutoff: float | None = None
    double_sided: bool = False
    # ``{"KHR_materials_clearcoat": {"clearcoatFactor": 1.0, "clearcoatTexture": TextureRef(...)}}``
    extensions: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)


@dataclass
class GltfMesh:
    """One node + mesh + single triangle primitive. Positions are Y-up; ``uvs`` use the glTF (top-left) origin."""

    name: str
    positions: np.ndarray
    triangles: np.ndarray
    uvs: np.ndarray | None = None
    material: GltfMaterial | None = None


# --------------------------------------------------------------------------- conventions


def z_up_to_y_up(points: np.ndarray) -> np.ndarray:
    """Kernel world (Z-up) -> glTF (Y-up): (x, y, z) -> (x, z, -y)."""
    p = np.asarray(points, dtype=np.float64)
    return np.stack([p[:, 0], p[:, 2], -p[:, 1]], axis=1)


def flip_v(uvs: np.ndarray) -> np.ndarray:
    """Bottom-left UV origin (USD, kernel) -> glTF's top-left origin: v' = 1 - v."""
    uv = np.array(uvs, dtype=np.float64)
    uv[:, 1] = 1.0 - uv[:, 1]
    return uv


# --------------------------------------------------------------------------- writer


class _Builder:
    def __init__(self) -> None:
        self.blob = bytearray()
        self.views: list[gl.BufferView] = []
        self.accessors: list[gl.Accessor] = []
        self.images: list[gl.Image] = []
        self.textures: list[gl.Texture] = []
        self.samplers: list[gl.Sampler] = []
        self.materials: list[gl.Material] = []
        self._image_ix: dict[str, int] = {}
        self._sampler_ix: dict[Sampler, int] = {}
        self._texture_ix: dict[tuple[int, int | None], int] = {}
        self._material_ix: dict[int, int] = {}
        self.extensions_used: set[str] = set()

    def view(self, data: bytes | memoryview, target: int | None = None) -> int:
        self.blob += b"\0" * (-len(self.blob) % 4)
        self.views.append(gl.BufferView(buffer=0, byteOffset=len(self.blob), byteLength=len(data), target=target))
        self.blob += data
        return len(self.views) - 1

    def accessor(self, array: np.ndarray, component: int, kind: str, target: int, *, bounds: bool) -> int:
        arr = np.ascontiguousarray(array)
        acc = gl.Accessor(bufferView=self.view(arr.tobytes(), target), componentType=component, type=kind, count=len(arr))
        if bounds:
            acc.min, acc.max = arr.min(axis=0).astype(float).tolist(), arr.max(axis=0).astype(float).tolist()
        self.accessors.append(acc)
        return len(self.accessors) - 1

    def image(self, image: EncodedImage) -> int:
        if image.digest not in self._image_ix:
            self.images.append(gl.Image(bufferView=self.view(image.data), mimeType=image.mime_type, name=image.name))
            self._image_ix[image.digest] = len(self.images) - 1
        return self._image_ix[image.digest]

    def sampler(self, s: Sampler | None) -> int | None:
        if s is None:
            return None
        if s not in self._sampler_ix:
            self.samplers.append(gl.Sampler(magFilter=s.mag_filter, minFilter=s.min_filter, wrapS=s.wrap_s, wrapT=s.wrap_t))
            self._sampler_ix[s] = len(self.samplers) - 1
        return self._sampler_ix[s]

    def texture(self, ref: TextureRef) -> int:
        key = (self.image(ref.image), self.sampler(ref.sampler))
        if key not in self._texture_ix:
            self.textures.append(gl.Texture(source=key[0], sampler=key[1]))
            self._texture_ix[key] = len(self.textures) - 1
        return self._texture_ix[key]

    def texture_info(self, ref: TextureRef | None, cls: type = gl.TextureInfo, **extra: Any) -> Any:
        if ref is None:
            return None
        info = cls(index=self.texture(ref), texCoord=ref.tex_coord, **extra)
        if ref.extensions:
            info.extensions = self.resolve(dict(ref.extensions))
            self.extensions_used.update(ref.extensions)
        return info

    def resolve(self, value: Any) -> Any:
        """Plain-JSON copy of an extension payload with every ``TextureRef`` replaced by its texture info."""
        if isinstance(value, TextureRef):
            out: dict[str, Any] = {"index": self.texture(value), "texCoord": value.tex_coord}
            if value.scale is not None:
                out["scale"] = value.scale
            if value.strength is not None:
                out["strength"] = value.strength
            if value.extensions:
                out["extensions"] = self.resolve(dict(value.extensions))
                self.extensions_used.update(value.extensions)
            return out
        if isinstance(value, Mapping):
            return {k: self.resolve(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.resolve(v) for v in value]
        if isinstance(value, np.generic):
            return value.item()
        return value

    def material(self, m: GltfMaterial) -> int:
        if id(m) in self._material_ix:
            return self._material_ix[id(m)]
        pbr = gl.PbrMetallicRoughness(
            baseColorFactor=[float(v) for v in m.base_color_factor],
            metallicFactor=float(m.metallic_factor), roughnessFactor=float(m.roughness_factor),
            baseColorTexture=self.texture_info(m.base_color_texture),
            metallicRoughnessTexture=self.texture_info(m.metallic_roughness_texture),
        )
        mat = gl.Material(
            name=m.name, pbrMetallicRoughness=pbr, doubleSided=bool(m.double_sided),
            normalTexture=self.texture_info(m.normal_texture, gl.NormalMaterialTexture, scale=m.normal_texture.scale if m.normal_texture and m.normal_texture.scale is not None else 1.0),
            occlusionTexture=self.texture_info(m.occlusion_texture, gl.OcclusionTextureInfo, strength=m.occlusion_texture.strength if m.occlusion_texture and m.occlusion_texture.strength is not None else 1.0),
            emissiveTexture=self.texture_info(m.emissive_texture),
            emissiveFactor=[float(v) for v in m.emissive_factor] if m.emissive_factor is not None else None,
        )
        if m.alpha_mode:
            mat.alphaMode = m.alpha_mode
        if m.alpha_cutoff is not None:
            mat.alphaCutoff = float(m.alpha_cutoff)
        if m.extensions:
            mat.extensions = self.resolve(dict(m.extensions))
            self.extensions_used.update(m.extensions)
        self.materials.append(mat)
        self._material_ix[id(m)] = len(self.materials) - 1
        return len(self.materials) - 1


def build_glb(meshes: Sequence[GltfMesh], *, extensions_required: Sequence[str] = ()) -> bytes:
    """Serialises ``meshes`` (one root node each) to GLB bytes. See the module docstring."""
    b = _Builder()
    nodes, gl_meshes = [], []
    for m in meshes:
        pos = np.asarray(m.positions, dtype=np.float32).reshape(-1, 3)
        tri = np.asarray(m.triangles).reshape(-1, 3)
        if len(pos) == 0 or len(tri) == 0:
            raise ValueError(f"mesh {m.name!r} has no geometry")
        if tri.min() < 0 or tri.max() >= len(pos):
            raise ValueError(f"mesh {m.name!r} has triangle indices outside its {len(pos)} vertices")
        attrs = {}
        idx = b.accessor(tri.astype(np.uint32).reshape(-1), 5125, "SCALAR", _ELEMENT_ARRAY_BUFFER, bounds=False)
        b.accessors[idx].min, b.accessors[idx].max = [int(tri.min())], [int(tri.max())]
        attrs["POSITION"] = b.accessor(pos, 5126, "VEC3", _ARRAY_BUFFER, bounds=True)
        if m.uvs is not None:
            uv = np.asarray(m.uvs, dtype=np.float32).reshape(-1, 2)
            if len(uv) != len(pos):
                raise ValueError(f"mesh {m.name!r}: {len(uv)} UVs for {len(pos)} vertices")
            attrs["TEXCOORD_0"] = b.accessor(uv, 5126, "VEC2", _ARRAY_BUFFER, bounds=True)
        prim = gl.Primitive(attributes=gl.Attributes(**attrs), indices=idx, mode=4)
        gl_meshes.append(gl.Mesh(name=m.name, primitives=[prim]))
        nodes.append(gl.Node(name=m.name, mesh=len(gl_meshes) - 1))
    # materials after the geometry so that images follow all vertex data; first-use order keeps it deterministic
    for m, mesh in zip(meshes, gl_meshes):
        if m.material is not None:
            mesh.primitives[0].material = b.material(m.material)
    b.blob += b"\0" * (-len(b.blob) % 4)

    doc = gl.GLTF2(
        asset=gl.Asset(version="2.0", generator=GENERATOR),
        scene=0, scenes=[gl.Scene(nodes=list(range(len(nodes))))], nodes=nodes, meshes=gl_meshes,
        accessors=b.accessors, bufferViews=b.views, buffers=[gl.Buffer(byteLength=len(b.blob))],
        materials=b.materials or None, textures=b.textures or None, images=b.images or None,
        samplers=b.samplers or None,
        extensionsUsed=sorted(b.extensions_used | set(extensions_required)) or None,
        extensionsRequired=sorted(set(extensions_required)) or None,
    )
    js = doc.gltf_to_json(separators=(",", ":"), indent=None).encode()
    js += b" " * (-len(js) % 4)
    total = 12 + 8 + len(js) + 8 + len(b.blob)
    return b"".join([struct.pack("<4sII", b"glTF", 2, total), struct.pack("<I4s", len(js), b"JSON"), js,
                     struct.pack("<I4s", len(b.blob), b"BIN\0"), bytes(b.blob)])


def write_glb(path: str | Path, meshes: Sequence[GltfMesh], *, extensions_required: Sequence[str] = ()) -> Path:
    path = Path(path)
    path.write_bytes(build_glb(meshes, extensions_required=extensions_required))
    return path


__all__ = ["EncodedImage", "GltfMaterial", "GltfMesh", "Sampler", "TextureRef", "build_glb", "flip_v",
           "write_glb", "z_up_to_y_up"]
