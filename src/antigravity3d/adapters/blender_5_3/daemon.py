"""Blender worker daemon.

    blender --background --factory-startup --python daemon.py -- --socket <path>
    python daemon.py --fake --socket <path>          # tests: no bpy

Serving is strictly sequential on the main thread (bpy is not thread-safe).
Shared-memory results are created by this process (it is the owner) and are
unlinked lazily: at the start of the next request -- the pool copies a buffer
out before it releases the worker -- and at exit. If the daemon is SIGKILLed,
the pool sweeps ``/dev/shm/<AGY3D_SHM_PREFIX>*``.

The daemon exits when its parent dies (ppid poll), so no orphans accumulate.
"""

from __future__ import annotations

import argparse
import importlib.util
import itertools
import json
import os
import select
import socket
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any, Callable

DAEMON_VERSION = "0.1.0"
_HERE = Path(__file__).resolve().parent


def _load_sibling(stem: str) -> Any:
    """Loads a sibling module by path: inside Blender this package is not importable."""
    name = f"_agy_{stem}"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{stem}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _bootstrap_msgpack() -> None:
    """Stock Blender isolates sys.path. Import only the host's msgpack package."""
    path = os.environ.get("AGY3D_MSGPACK_INIT")
    if path is None or importlib.util.find_spec("msgpack") is not None:
        return
    spec = importlib.util.spec_from_file_location("msgpack", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load msgpack from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["msgpack"] = module
    # msgpack automatically uses its Python implementation for a foreign Python ABI.
    spec.loader.exec_module(module)


_bootstrap_msgpack()
proto = _load_sibling("protocol")
linter = _load_sibling("linter")
shim = _load_sibling("shim_5_3")
skills = _load_sibling("skills")  # reviewed upstream primitives: loads nothing until load_reviewed is called
fbx = _load_sibling("fbx_export")  # FBX export and its round-trip proof (bpy is imported inside its functions)

Handler = Callable[[dict[str, Any]], Any]
OPS: dict[str, Handler] = {}
PRIMITIVES: dict[str, Callable[..., dict[str, Any]]] = {}  # measurements: what inspect_usd may run
WRITERS: dict[str, Callable[..., dict[str, Any]]] = {}  # reviewed scripts that write a file: only export_fbx runs them
PRIMITIVES_INFO: dict[str, Any] = {"source": None, "error": None}  # where the primitives came from, or why there are none
# (imported, weld) -> (names of the imported mesh objects, weld report or None); set by register_*_ops
PREPARE: dict[str, Callable[[dict[str, Any], bool], tuple[list[str], dict[str, Any] | None]]] = {}
# names of the imported mesh objects -> their connected pieces (see inspect_usd's ``agy_inspect.pieces``)
PIECES: dict[str, Callable[[list[str]], list[dict[str, Any]] | None]] = {}
# inspect_usd(weld=True) merges vertices closer than this, per connected part of each imported mesh: coincident seam
# copies are bit-identical float32 points, and no real feature of an asset is a micrometre wide
WELD_DISTANCE_M = 1e-6
DENOISER_CHAIN = ("DLSS", "OPTIX", "OPENIMAGEDENOISE")  # preference order; filtered by what the build offers
ENGINE_IDS = {"workbench": "BLENDER_WORKBENCH", "eevee": "BLENDER_EEVEE", "cycles": "CYCLES"}


def _linear(hex_rgb: int) -> tuple[float, float, float]:
    """An sRGB colour 0xRRGGBB in linear (Blender's colour values)."""
    def one(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    return tuple(round(one(((hex_rgb >> s) & 0xFF) / 255.0), 4) for s in (16, 8, 0))  # type: ignore[return-value]


# The default rig's environment (``studio_world``): the web verification's gradient (web_three ENV_SKY / ENV_GROUND)
WORLD_SKY, WORLD_GROUND = _linear(0xCFE2F3), _linear(0x4A3F35)
# Strength 0.3, measured with the default suns (3.0 and 1.0) on a 0.5 m cube (tests/test_blender_pool.py, eevee,
# display luminance as the review sees the frame): grey metal (0.5, metallic 1, roughness 0.35) 0.13 with no world, 0.31
# at 0.3, 0.38 at 0.5, 0.49 at 1.0; a white dielectric (0.8) has 0 % of its pixels clipped with no world, about 1 % at
# 0.3, 2 % at 0.35, 11 % at 0.5 and 37 % at 1.0 (the frame is clipped, not tone mapped, as the web render's). The suns
# stay as they are: they give the shape, and web_three uses the same pair (3.0, 1.0).
WORLD_STRENGTH = 0.3
SAFE_MODULES = linter.ALLOWED_MODULES


def op(name: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        OPS[name] = fn
        return fn
    return deco


class Published:
    """Tracks shm segments this process created until the next request."""

    def __init__(self) -> None:
        self.segments: list[Any] = []
        self.prefix = os.environ.get("AGY3D_SHM_PREFIX", f"agy3d_{os.getpid()}_")
        self._n = itertools.count()

    def publish(self, arr: Any) -> dict[str, Any]:
        sa = proto.ShmArray.publish(arr, name=f"{self.prefix}{next(self._n)}")
        self.segments.append(sa)
        return sa.descriptor()

    def release(self) -> None:
        segs, self.segments = self.segments, []
        for sa in segs:
            sa.close()
            sa.unlink()


PUBLISHED = Published()


def pick_denoiser(requested: str, available: list[str]) -> str | None:
    """Fallback chain: requested (if offered), then DENOISER_CHAIN order, else None (denoise off)."""
    avail = {a.upper() for a in available}
    if requested.upper() == "NONE":
        return None
    for cand in (requested.upper(), *DENOISER_CHAIN):
        if cand in avail:
            return cand
    return None


def _same_value(got: Any, want: Any) -> bool:
    """Socket values are stored as float32: compare numbers with a tolerance, everything else exactly."""
    if isinstance(got, str) or isinstance(want, str) or isinstance(want, bool):
        return bool(got == want)
    if hasattr(got, "__len__"):
        return len(got) == len(want) and all(_same_value(g, w) for g, w in zip(got, want))
    return abs(float(got) - float(want)) <= 1e-6 * max(1.0, abs(float(want)))


def prepare_tree_spec(tree_json: str | dict[str, Any]) -> dict[str, Any]:
    """Validates a node-tree spec and guarantees the boolean ``is_preview`` input.

    Spec: ``{name, inputs:[{name, type, default}], nodes:[{name, type, props, inputs}],
    links:[[node, out_socket, node, in_socket]]}``; ``Group Input``/``Group Output``
    node names are implicit. The first input is always the geometry (a spec that lists another input first is refused).
    """
    spec = json.loads(tree_json) if isinstance(tree_json, str) else dict(tree_json)
    spec.setdefault("name", "AGY_GeoNodes")
    spec.setdefault("nodes", [])
    spec.setdefault("links", [])
    inputs = [dict(i) for i in spec.get("inputs", [])]
    if not any(i.get("type") == "NodeSocketGeometry" for i in inputs):
        inputs.insert(0, {"name": "Geometry", "type": "NodeSocketGeometry"})
    elif inputs[0].get("type") != "NodeSocketGeometry":
        # A Geometry Nodes modifier feeds the object's geometry into the FIRST input; with another socket first the
        # evaluated mesh is empty and nothing says why.
        raise ValueError(f"the geometry input must be first in 'inputs' (got {inputs[0].get('name')!r} first)")
    inputs = [i for i in inputs if i["name"] != "is_preview"]
    inputs.append({"name": "is_preview", "type": "NodeSocketBool", "default": True})
    spec["inputs"] = inputs
    names = [n["name"] for n in spec["nodes"]]
    if len(set(names)) != len(names):
        raise ValueError("duplicate node names in tree spec")
    return spec


# ---- common ops
@op("ping")
def _ping(a: dict[str, Any]) -> dict[str, Any]:
    return {"pong": True, "pid": os.getpid()}


@op("version")
def _version(a: dict[str, Any]) -> dict[str, Any]:
    bpy = sys.modules.get("bpy")
    return {"daemon": DAEMON_VERSION, "python": sys.version.split()[0], "fake": bpy is None or bool(getattr(bpy, "_agy_fake", False)),
            "blender_version": getattr(getattr(bpy, "app", None), "version", None),
            "blender": getattr(getattr(bpy, "app", None), "version_string", None), "ops": sorted(OPS), "primitives": sorted(PRIMITIVES), "writers": sorted(WRITERS),
            "primitives_source": PRIMITIVES_INFO["source"], "primitives_error": PRIMITIVES_INFO["error"]}


GLTF_EXTS = (".glb", ".gltf")


def _import_snapshot(path: str) -> dict[str, Any]:
    """Empty scene + one file: glTF (``.glb``/``.gltf``) through Blender's glTF importer, anything else as USD."""
    OPS["reset_scene"]({})
    kind = "gltf" if Path(path).suffix.lower() in GLTF_EXTS else "usd"
    imported = OPS[f"import_{kind}"]({"path": path})
    if not imported.get("fake") and not imported["renderable"]:
        raise ValueError(f"{kind.upper()} snapshot contains no renderable geometry")
    return imported


@op("render_usd")
def _render_usd(a: dict[str, Any]) -> dict[str, Any]:
    """Render a complete immutable USD snapshot on the worker handling this request."""
    imported = _import_snapshot(a["path"])
    result = OPS["render"](a)
    if "mesh_stats" in PRIMITIVES and not imported.get("fake"):  # the fake worker has no scene to count
        try:
            names, _ = PREPARE["prepare"](imported, False)
        except ValueError:  # renderable but no mesh object (curves, volumes): nothing for mesh_stats to count
            names = []
        if names:
            stats = PRIMITIVES["mesh_stats"](objects=names)  # exactly what was imported, whatever the selection
            result["mesh_stats"] = {key: stats[key] for key in ("total_tris", "total_verts", "draw_calls_est")}
    return {**result, "objects": imported.get("objects", []), "fake": bool(imported.get("fake"))}


@op("inspect_usd")
def _inspect_usd(a: dict[str, Any]) -> dict[str, Any]:
    """Import a USD (or ``.glb``/``.gltf``) snapshot into an empty scene and run one reviewed primitive on it, as a measurement.

    ``primitive`` names the script; ``kwargs`` must stay inside its contract (``skills.PRIMITIVE_KWARGS``): the
    call is refused before anything is imported otherwise. The worker passes ``objects`` itself: the mesh objects
    the import produced (upstream's default is "the selection, else everything visible", which depends on what the
    importer left selected). ``weld=True`` merges coincident vertices of each imported mesh first, part by part (see
    :data:`WELD_DISTANCE_M` and ``weld_within_parts``); UVs are per face corner in Blender, so they are unchanged, and
    only connectivity differs: UV seams of the package's seam-split points stop being open edges, and two parts in
    contact stay two parts. The result is the script's own, plus one key, ``agy_inspect``: the objects measured and
    the weld report (per object: vertices before and after, parts, vertices left coincident with another part's).
    """
    name = a.get("primitive")
    kwargs = skills.check_kwargs(name, a.get("kwargs"))
    run = PRIMITIVES.get(name)
    if run is None:
        raise KeyError(f"primitive {name!r} is not loaded in this worker (loaded: {sorted(PRIMITIVES)}; "
                       f"load error: {PRIMITIVES_INFO['error']})")
    weld = a.get("weld", False)
    if not isinstance(weld, bool):
        raise TypeError(f"weld must be true or false, got {weld!r}")
    imported = _import_snapshot(a["path"])
    names, welded = PREPARE["prepare"](imported, weld)
    result = run(objects=names, **kwargs)
    pieces = PIECES["pieces"](names) if name == "qa_check" else None
    result["agy_inspect"] = {"objects": names, "weld": welded, **({"pieces": pieces} if pieces is not None else {}), "importer": "gltf" if Path(a["path"]).suffix.lower() in GLTF_EXTS else "usd"}
    return result


def register_reviewed_primitives() -> None:
    """Load the vendored upstream scripts (or the $AGY3D_BLENDER_SKILLS checkout); a failure is reported, not raised.

    Rendering and the other operations do not depend on the primitives, so a worker whose primitives cannot be
    loaded still starts; ``version`` says why they are missing and ``inspect_usd`` refuses with that reason.
    """
    root, kind = skills.source_root()
    try:
        loaded = skills.load_reviewed(root)
        PRIMITIVES.update({k: v for k, v in loaded.items() if k not in skills.PINNED_WRITERS})
        WRITERS.update({k: v for k, v in loaded.items() if k in skills.PINNED_WRITERS})
        PRIMITIVES_INFO.update(source={"kind": kind, "path": str(root), "commit": skills.UPSTREAM_COMMIT}, error=None)
    except Exception as e:  # noqa: BLE001 - reported through version / inspect_usd
        PRIMITIVES_INFO.update(source={"kind": kind, "path": str(root), "commit": skills.UPSTREAM_COMMIT},
                               error=f"{type(e).__name__}: {e}"[:400])


def _export_script_record() -> dict[str, Any]:
    source = PRIMITIVES_INFO["source"] or {}
    return {"name": "export_asset.py", "repository": skills.UPSTREAM_REPOSITORY, "commit": skills.UPSTREAM_COMMIT,
            "sha256": skills.PINNED_WRITERS["export_asset"][1], "source": source.get("kind")}


def fake_primitive(name: str) -> Callable[..., dict[str, Any]]:
    """A stand-in with the result shape of the real script (top-level keys and per-object keys), marked fake.

    The values are placeholders (one object, ``FakeMesh``, with zero metrics): the fake worker has no scene.
    """
    obj = "FakeMesh"

    def mesh_stats(objects: list[str] | None = None, budget: str | None = None) -> dict[str, Any]:
        rep: dict[str, Any] = {
            "ok": True, "objects": {obj: {"tris": 0, "verts": 0, "materials": 0, "modifiers": [], "uv_maps": 0, "shape_keys": 0}},
            "total_tris": 0, "total_verts": 0, "draw_calls_est": 1, "unique_materials": 0, "textures": [],
            "texture_vram_mb_uncompressed": 0.0, "bones": 0, "kind": "prop", "fake": True}
        if budget:
            limits = dict.fromkeys(("char_tris", "prop_tris", "materials", "tex", "bones"))
            rep["budget"] = {"name": budget, "limits": limits, "over": [], "pass": True}
        return rep

    def qa_check(objects: list[str] | None = None, profile: str = "game", eps: float = 0.001, tol: float = 0.002, max_tris: int = 300000, uv: bool = True) -> dict[str, Any]:
        metrics: dict[str, Any] = dict.fromkeys(
            ("verts", "faces", "tris", "ngons", "boundary_edges", "nonmanifold_edges", "wire_edges", "loose_verts", "degenerate_faces",
             "zero_length_edges", "inconsistent_normals_edges", "duplicate_verts", "islands", "inside_out_islands", "materials"), 0)
        metrics["scale"] = [1.0, 1.0, 1.0]
        if uv:  # an object with a UV map (without one, the real script reports only uv_maps=0)
            metrics.update(uv_maps=1, uv_oob_pct=0.0, uv_zero_area_faces=0, uv_overlap_pct=0.0, uv_coverage_pct=0.0)
        return {"ok": True, "pass": True, "errors": [], "warnings": [], "ground_z": 0.0, "zfighting": [], "floating": [],
                "back_to_back": [], "enclosed": [], "objects": {obj: metrics}, "fake": True}

    def kit_check(objects: list[str] | None = None, grid: float = 0.5, pivot: str = "corner", texture: int = 2048, tol: float = 0.001, prefix: str = "SM_") -> dict[str, Any]:
        if pivot not in ("corner", "bottom-center", "center"):
            raise ValueError("--pivot must be corner, bottom-center or center")
        return {"ok": True, "pass": True, "grid_m": grid, "pivot": pivot, "fixed": False,
                "pieces": {obj: {"dims_m": [0.0, 0.0, 0.0], "tris": 0, "texel_px_per_m": None, "errors": [], "warnings": []}}, "fake": True}

    def texel_density(objects: list[str] | None = None, res: int = 2048) -> dict[str, Any]:
        return {"ok": True, "res": res,
                "objects": {obj: {"px_per_m": 0.0, "island_min": 0.0, "island_max": 0.0, "islands": 0, "spread": 0.0}}, "fake": True}

    return {"mesh_stats": mesh_stats, "qa_check": qa_check, "kit_check": kit_check, "texel_density": texel_density}[name]


@op("run_script")
def _run_script(a: dict[str, Any]) -> dict[str, Any]:
    source: str = a["source"]
    if a.get("legacy"):
        source, _ = shim.rewrite_source(source)
    linter.assert_safe(source)  # never trust the caller: always re-lint here
    if "bpy" not in sys.modules:
        return {"executed": False, "linted": True, "fake": True}
    return {"executed": True, "result": _exec_linted(source)}


def _exec_linted(source: str) -> Any:
    import builtins

    real_import = builtins.__import__

    def guarded_import(name: str, *args: Any, **kw: Any) -> Any:
        if name.split(".")[0] not in SAFE_MODULES:
            raise ImportError(f"import of {name!r} blocked")
        return real_import(name, *args, **kw)

    bi = {k: v for k, v in vars(builtins).items() if k not in linter.DANGEROUS_BUILTINS}
    bi["__import__"] = guarded_import
    ns: dict[str, Any] = {"__builtins__": bi, "__name__": "__agy_script__"}
    exec(compile(source, "<run_script>", "exec"), ns)
    res = ns.get("result")
    try:
        json.dumps(res)
        return res
    except TypeError:
        return repr(res)


# ---- fake handlers (CPython only)
def _fake_delay(a: dict[str, Any]) -> None:
    d = float(a.get("delay", os.environ.get("AGY3D_FAKE_DELAY", 0.0)))
    if d > 0:
        time.sleep(d)


def register_fake_ops() -> None:
    import numpy as np

    @op("render")
    def render(a: dict[str, Any]) -> dict[str, Any]:
        _fake_delay(a)
        w, h = int(a.get("width", 64)), int(a.get("height", 64))
        seed = int(a.get("seed", 0))
        x = np.linspace(0, 1, w, dtype=np.float32)[None, :].repeat(h, 0)
        y = np.linspace(0, 1, h, dtype=np.float32)[:, None].repeat(w, 1)
        img = np.stack([x, y, np.full_like(x, (seed % 256) / 255.0), np.ones_like(x)], axis=-1)
        return {"engine": a.get("engine", "workbench"), "pid": os.getpid(), "image": PUBLISHED.publish(img)}

    @op("echo")
    def echo(a: dict[str, Any]) -> Any:
        _fake_delay(a)
        return {**a, "pid": os.getpid()}

    @op("sleep")
    def sleep(a: dict[str, Any]) -> dict[str, Any]:
        time.sleep(float(a.get("seconds", 1)))
        return {"pid": os.getpid()}

    @op("fail")
    def fail(a: dict[str, Any]) -> None:
        raise ValueError(a.get("msg", "fake failure"))

    @op("crash")
    def crash(a: dict[str, Any]) -> None:
        os._exit(int(a.get("code", 3)))

    for name in ("import_usd", "import_gltf", "export_usd", "reset_scene", "import_splat"):
        OPS[name] = (lambda n: lambda a: {"fake": True, "op": n, **{k: v for k, v in a.items() if k != "source"}})(name)

    @op("apply_geonodes")
    def apply_geonodes_fake(a: dict[str, Any]) -> dict[str, Any]:
        spec = prepare_tree_spec(a["tree_json"])
        return {"fake": True, "object": a["object_name"], "is_preview": bool(a.get("is_preview", True)),
                "inputs": [i["name"] for i in spec["inputs"]]}

    for name in skills.PRIMITIVE_KWARGS:
        PRIMITIVES[name] = fake_primitive(name)

    def fake_fbx(a: dict[str, Any], *, verify: bool) -> dict[str, Any]:
        """The real contract (the same ``check_export_request``) and the real result's keys; no scene, so nothing is written
        or measured: ``ok`` is False and ``fake`` True, and a caller must treat that as "not exported"."""
        req = skills.check_export_request(a, verify=verify)
        names = fbx.plan(req["preset"], req["asset_name"], 1 + len(req["lods"]), 1 if req["colliders"] else 0)
        path = req["fbx"] if verify else req["final"]
        result: dict[str, Any] = {
            "ok": False, "fake": True, "preset": req["preset"], "asset_name": req["asset_name"],
            "fbx": {"path": path, "bytes": 0, "sha256": None}, "blender": None, "seconds": {"total": 0.0},
            "header": {}, "comparison": {"ok": False, "checks": []}, "maps": {"source": {}, "fbx": {}, "lost": []},
            "verified": "Nothing was exported or compared: the fake worker has no Blender.", "not_verified": list(fbx.NOT_VERIFIED)}
        if not verify:
            result.update(script=_export_script_record(), reproducibility=fbx.REPRODUCIBILITY, sources={"levels": [], "collision_hulls": None}, join=[], limits=None,
                          contents={"render_meshes": [{"name": n} for n in names["render"]], "collision_hulls": names["hulls"], "omitted": names["omitted"]},
                          textures={"mode": "embedded" if req["preset"] == "roblox" else "relative_reference", "rebound": [], "embedded_bytes": 0, "references": [], "substituted": []})
        return result

    WRITERS["export_asset"] = lambda **kw: {"ok": False, "fake": True, "files": []}  # never reached: the fake ops below write nothing
    OPS["export_fbx"] = lambda a: fake_fbx(a, verify=False)
    OPS["verify_fbx"] = lambda a: fake_fbx(a, verify=True)
    PRIMITIVES_INFO.update(source={"kind": "fake", "path": None, "commit": None}, error=None)

    def prepare_fake(imported: dict[str, Any], weld: bool) -> tuple[list[str], dict[str, Any] | None]:
        names = ["FakeMesh"]
        return names, ({"distance_m": WELD_DISTANCE_M, "fake": True,
                        "objects": {n: {"verts_before": 0, "verts_after": 0, "parts": 0, "coincident_between_parts": 0} for n in names}}
                       if weld else None)
    PREPARE["prepare"] = prepare_fake
    PIECES["pieces"] = lambda names: None  # the fake worker has no scene: no pieces key

    @op("bake_maps")
    def bake_fake(a: dict[str, Any]) -> dict[str, Any]:
        out = Path(a["out_dir"])
        out.mkdir(parents=True, exist_ok=True)
        paths = []
        for m in a["maps"]:
            p = out / f"{a['object_name']}_{m}.fake.txt"
            p.write_text(f"fake bake {m} @ {a.get('resolution', 1024)}\n")
            paths.append(str(p))
        return {"fake": True, "paths": paths}


# ---- bpy handlers
def register_bpy_ops(bpy: Any) -> None:
    import numpy as np

    from mathutils import Vector

    if bpy.app.version[:2] < (5, 3):
        raise RuntimeError(f"Blender 5.3+ required, got {bpy.app.version_string}")
    # --factory-startup loads Blender's default scene (Cube, Light, Camera). Start from an empty one, once, so no
    # operation or model-written script works next to objects nobody put there.
    bpy.ops.wm.read_factory_settings(use_empty=True)
    shim.install(bpy)
    register_reviewed_primitives()

    def weld_within_parts(bmesh: Any, bm: Any, dist: float) -> tuple[int, int]:
        """Merge the coincident vertices of ``bm`` part by part; returns (parts, vertices left coincident with a vertex
        of another part).

        The file splits every part at its UV seams and hard edges, so its copies of a vertex must be merged before
        topology can be read. Two different parts in contact also have vertices at one position (a lid on a box of the
        same footprint, a plate flush with a corner): merged as well, they stop being two closed shells and read as one
        surface with edges shared by four faces and faces wound against each other, which is how a sound Roblox
        package failed ``qa_check`` on "edges with inconsistent face winding" (2026-11-21). So the parts are found
        first. Faces belong to one part when they share a vertex of the file, or an edge position:

        * used by exactly two faces: a seam of one surface, whichever way the two are wound (a face wound the wrong
          way stays in its part, and is reported);
        * used by more: the faces are taken in order around the edge, and a face is joined to the next one across the
          solid it bounds (the material behind a face is on the side its normal points away from). A lid on a body
          gives body side with body top, and lid bottom with lid side, though body top and lid bottom lie in one
          plane. A face left without a partner (the rim of a part whose contact face was not shipped) joins nothing:
          its partner would be the first face round the edge, and that one opens another part's solid.
        """
        from math import atan2, tau

        doubles = bmesh.ops.find_doubles(bm, verts=bm.verts[:], dist=dist)["targetmap"]
        bm.verts.ensure_lookup_table()
        bm.verts.index_update()
        bm.faces.index_update()
        bm.normal_update()
        position = {v.index: t.index for v, t in doubles.items()}  # vertex -> the vertex that stands for its position
        parent = list(range(len(bm.faces)))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            a, b = find(a), find(b)
            if a != b:
                parent[b] = a

        for v in bm.verts:
            faces = v.link_faces
            for f in faces[1:]:
                union(faces[0].index, f.index)
        used: dict[tuple[int, int], list[tuple[Any, int]]] = {}  # edge position -> (face, +1 along it / -1 against it)
        for f in bm.faces:
            for loop in f.loops:
                a = position.get(loop.vert.index, loop.vert.index)
                b = position.get(loop.link_loop_next.vert.index, loop.link_loop_next.vert.index)
                if a != b:
                    used.setdefault((a, b) if a < b else (b, a), []).append((f, 1 if a < b else -1))
        for (a, b), faces in used.items():
            if len(faces) == 2:
                union(faces[0][0].index, faces[1][0].index)
                continue
            if len(faces) < 2:
                continue
            axis = bm.verts[b].co - bm.verts[a].co
            if axis.length < 1e-12:
                continue
            axis.normalize()
            # the direction in which each face leaves the edge; with the edge pointing at the viewer, the material
            # behind a face that runs along the edge lies clockwise of it, and counter-clockwise for one that runs against
            into = [(f.normal.cross(axis * sense), f, sense) for f, sense in faces]
            u = next((t.normalized() for t, _, _ in into if t.length > 1e-9), None)
            if u is None:
                continue
            v = axis.cross(u)
            around = [(atan2(t.dot(v), t.dot(u)) % tau, f, sense) for t, f, sense in into if t.length > 1e-9]
            taken: set[int] = set()
            for angle, f, sense in around:
                if sense != -1:
                    continue
                best = None
                first = tau  # how far round the first face of any kind is: the solid behind f ends there
                for other, g, sense_g in around:
                    if g is f:
                        continue
                    turn = (other - angle) % tau
                    if turn < 1e-6:
                        turn = tau  # a face in the same half-plane bounds no solid with this one
                    first = min(first, turn)
                    if sense_g != 1 or g.index in taken:
                        continue
                    if best is None or turn < best[0]:
                        best = (turn, g)
                # The partner is the first face round the edge. When a face that opens another solid comes before it,
                # f's own partner was not shipped (a hidden contact face) and f joins nothing. Without this, the rim of
                # a plate whose covered top was left out took the side of the plate standing on it, half a turn away:
                # the two parts were merged into one and qa_check reported "edges with inconsistent face winding" on
                # a sound corner guard (live 2026-11-22, r5/live_rerun_N3: five refused deliveries, no part named).
                if best is not None and best[0] <= first + 1e-6:
                    taken.add(best[1].index)
                    union(f.index, best[1].index)
        keep: dict[tuple[int, int], Any] = {}
        target: dict[Any, Any] = {}
        parts_at: dict[int, set[int]] = {}
        for vert in bm.verts:
            if not vert.link_faces:
                continue
            at, part = position.get(vert.index, vert.index), find(vert.link_faces[0].index)
            parts_at.setdefault(at, set()).add(part)
            if (at, part) in keep:
                target[vert] = keep[(at, part)]
            else:
                keep[(at, part)] = vert
        apart = sum(len(parts) for parts in parts_at.values() if len(parts) > 1)
        n_parts = len({find(f.index) for f in bm.faces})
        if target:
            bmesh.ops.weld_verts(bm, targetmap=target)
        return n_parts, apart

    def prepare(imported: dict[str, Any], weld: bool) -> tuple[list[str], dict[str, Any] | None]:
        """The imported mesh objects (by name), with transforms evaluated; optionally welded (see inspect_usd)."""
        bpy.context.view_layer.update()  # matrix_world of freshly imported objects is evaluated here, not lazily
        objs = [bpy.data.objects[n] for n in imported["objects"] if bpy.data.objects[n].type == "MESH"]
        if not objs:  # an empty list would make upstream fall back to the selection
            raise ValueError("the import produced no mesh objects")
        report = None
        if weld:
            import bmesh

            report = {"distance_m": WELD_DISTANCE_M, "objects": {}}
            seen: set[int] = set()
            for ob in objs:
                me = ob.data
                if me.as_pointer() in seen:  # mesh data shared by two objects is welded once
                    continue
                seen.add(me.as_pointer())
                bm = bmesh.new()
                bm.from_mesh(me)
                before = len(bm.verts)
                parts, apart = weld_within_parts(bmesh, bm, WELD_DISTANCE_M)
                bm.to_mesh(me)
                bm.free()
                me.update()
                report["objects"][ob.name] = {"verts_before": before, "verts_after": len(me.vertices), "parts": parts,
                                              "coincident_between_parts": apart}
            bpy.context.view_layer.update()
        return [ob.name for ob in objs], report
    PREPARE["prepare"] = prepare

    def pieces(names: list[str]) -> list[dict[str, Any]]:
        """The connected pieces (vertices joined by edges, world space) of each named object, as qa_check's detached-part
        test sees them: object, vertex count, bounding-box centre and lowest and highest z. A fact about the scene beside
        the upstream result, which does not carry the lowest point of what it reports."""
        import bmesh

        out = []
        for name in names:
            bm = bmesh.new()
            bm.from_mesh(bpy.data.objects[name].data)
            bm.transform(bpy.data.objects[name].matrix_world)
            bm.verts.ensure_lookup_table()
            parent = list(range(len(bm.verts)))

            def find(i: int) -> int:
                while parent[i] != i:
                    parent[i] = parent[parent[i]]
                    i = parent[i]
                return i
            for e in bm.edges:
                a, b = find(e.verts[0].index), find(e.verts[1].index)
                if a != b:
                    parent[a] = b
            groups: dict[int, list[Any]] = {}
            for v in bm.verts:
                groups.setdefault(find(v.index), []).append(v.co)
            for pts in groups.values():
                lo = [min(p[i] for p in pts) for i in range(3)]
                hi = [max(p[i] for p in pts) for i in range(3)]
                out.append({"object": name, "verts": len(pts), "center": [round((lo[i] + hi[i]) / 2, 3) for i in range(3)],
                            "min_z": round(lo[2], 5), "max_z": round(hi[2], 5)})
            bm.free()
        return out
    PIECES["pieces"] = pieces

    geometry_types = {"MESH", "CURVE", "SURFACE", "FONT", "META", "VOLUME", "POINTCLOUD"}

    def frame_scene(scene: Any, width: int, height: int) -> None:
        if scene.camera is None:
            bpy.context.view_layer.update()
            corners = [obj.matrix_world @ Vector(corner)
                       for obj in scene.objects if obj.type in geometry_types and not obj.hide_render
                       for corner in obj.bound_box]
            if not corners:
                corners = [Vector((-1, -1, -1)), Vector((1, 1, 1))]
            low = Vector(tuple(min(p[i] for p in corners) for i in range(3)))
            high = Vector(tuple(max(p[i] for p in corners) for i in range(3)))
            center = (low + high) / 2
            diameter = max((high - low).length, 0.001)
            camera = bpy.data.cameras.new("Lookdev camera")
            obj = bpy.data.objects.new(camera.name, camera)
            scene.collection.objects.link(obj)
            camera.type = "ORTHO"
            camera.ortho_scale = diameter * 1.1 * max(width / height, height / width)
            camera.clip_start = diameter * 0.01
            camera.clip_end = diameter * 5
            view = Vector((1.4, -2.0, 1.2)).normalized()
            obj.location = center + view * diameter * 2
            obj.rotation_euler = (-view).to_track_quat("-Z", "Y").to_euler()
            scene.camera = obj
        if not any(obj.type == "LIGHT" and not obj.hide_render for obj in scene.objects):
            for name, direction, energy in (("Key", (2, -3, 4), 3.0), ("Fill", (-2, 1, 2), 1.0)):
                light = bpy.data.lights.new(name, "SUN")
                light.energy = energy
                obj = bpy.data.objects.new(light.name, light)
                scene.collection.objects.link(obj)
                obj.rotation_euler = (-Vector(direction)).to_track_quat("-Z", "Y").to_euler()
            studio_world(scene)

    def studio_world(scene: Any) -> None:
        """The environment of the default rig: a vertical gradient from ``WORLD_GROUND`` (below) to ``WORLD_SKY``
        (above), the web verification's (adapters/engines/web_three.py ENV_SKY / ENV_GROUND, its colours in linear),
        strength 1. The suns give shape; this gives a metal surface something to reflect and fills the dark side of a
        dielectric. Live on 2026-11-22 (r5/live_final_N) the two suns alone, against a world-less scene, rendered every
        metal=1 part flat black and the lookdev review called the crate's steel "plastic" (0.85). The film stays
        transparent where asked, so the background of the frame is unchanged. No image: nothing to load."""
        world = bpy.data.worlds.new("agy3d_studio")
        world.use_nodes = True
        nodes, links = world.node_tree.nodes, world.node_tree.links
        bg = nodes.get("Background") or nodes.new("ShaderNodeBackground")
        coords = nodes.new("ShaderNodeTexCoord")
        split = nodes.new("ShaderNodeSeparateXYZ")
        span = nodes.new("ShaderNodeMapRange")
        ramp = nodes.new("ShaderNodeValToRGB")
        links.new(coords.outputs["Generated"], split.inputs[0])  # a world's Generated is the direction looked in
        links.new(split.outputs["Z"], span.inputs["Value"])
        span.inputs["From Min"].default_value, span.inputs["From Max"].default_value = -1.0, 1.0
        links.new(span.outputs["Result"], ramp.inputs["Fac"])  # straight down 0, straight up 1: web_three's lerp
        ramp.color_ramp.elements[0].color = (*WORLD_GROUND, 1.0)
        ramp.color_ramp.elements[1].color = (*WORLD_SKY, 1.0)
        links.new(ramp.outputs["Color"], bg.inputs["Color"])
        bg.inputs["Strength"].default_value = WORLD_STRENGTH
        scene.world = world

    def _obj(name: str) -> Any:
        obj = bpy.data.objects.get(name)
        if obj is None:
            raise KeyError(f"no object named {name!r}")
        return obj

    @op("import_usd")
    def import_usd(a: dict[str, Any]) -> dict[str, Any]:
        before = set(bpy.data.objects)
        if "FINISHED" not in bpy.ops.wm.usd_import(filepath=a["path"]):
            raise RuntimeError(f"USD import failed for {a['path']}")
        imported = set(bpy.data.objects) - before
        return {"objects": sorted(obj.name for obj in imported),
                "renderable": any(obj.type in geometry_types and not obj.hide_render for obj in imported)}

    @op("import_gltf")
    def import_gltf(a: dict[str, Any]) -> dict[str, Any]:
        """Blender's bundled glTF importer (add-on io_scene_gltf2, enabled in factory settings), default options."""
        import addon_utils

        if not addon_utils.check("io_scene_gltf2")[1]:  # (enabled by default, loaded)
            addon_utils.enable("io_scene_gltf2", default_set=True)
        if not addon_utils.check("io_scene_gltf2")[1]:
            raise RuntimeError("the glTF importer (add-on io_scene_gltf2) is not available in this Blender")
        before = set(bpy.data.objects)
        if "FINISHED" not in bpy.ops.import_scene.gltf(filepath=a["path"]):
            raise RuntimeError(f"glTF import failed for {a['path']}")
        imported = set(bpy.data.objects) - before
        return {"objects": sorted(obj.name for obj in imported),
                "renderable": any(obj.type in geometry_types and not obj.hide_render for obj in imported)}

    @op("export_fbx")
    def export_fbx(a: dict[str, Any]) -> dict[str, Any]:
        """Package GLBs -> one FBX of the target engine, proven equal to its sources by re-import and header parse.

        The request is checked by ``skills.check_export_request`` (a contract of its own: one file, where the worker
        says); the reviewed upstream ``export_asset`` runs only with the worker's own arguments. See ``fbx_export``.
        """
        req = skills.check_export_request(a)
        writer = WRITERS.get("export_asset")
        if writer is None:
            raise KeyError(f"export_asset is not loaded in this worker (load error: {PRIMITIVES_INFO['error']})")
        return fbx.export_fbx(bpy, req, writer, OPS["import_gltf"], OPS["reset_scene"], _export_script_record())

    @op("verify_fbx")
    def verify_fbx(a: dict[str, Any]) -> dict[str, Any]:
        """The export's comparison, for an FBX that already exists (read only); raises when it does not match its sources."""
        return fbx.verify_fbx(bpy, skills.check_export_request(a, verify=True), OPS["import_gltf"], OPS["reset_scene"])

    @op("export_usd")
    def export_usd(a: dict[str, Any]) -> dict[str, Any]:
        bpy.ops.wm.usd_export(filepath=a["path"])
        return {"path": a["path"]}

    @op("reset_scene")
    def reset_scene(a: dict[str, Any]) -> dict[str, Any]:
        bpy.ops.wm.read_factory_settings(use_empty=True)
        return {"objects": 0}

    @op("render")
    def render(a: dict[str, Any]) -> dict[str, Any]:
        engine = a.get("engine", "workbench")
        if engine not in ENGINE_IDS:
            raise ValueError(f"unknown engine {engine!r}")
        w, h, samples = int(a.get("width", 512)), int(a.get("height", 512)), int(a.get("samples", 16))
        scene = bpy.context.scene
        frame_scene(scene, w, h)
        scene.render.engine = ENGINE_IDS[engine]
        scene.render.resolution_x, scene.render.resolution_y = w, h
        scene.render.resolution_percentage = 100
        scene.render.film_transparent = bool(a.get("transparent", False))
        used = None
        if engine == "cycles":
            cy = scene.cycles
            cy.samples = samples
            # Static RNA enum_items is empty. Cycles populates this list per device.
            from cycles.properties import enum_denoiser
            items = [item[0] for item in enum_denoiser(cy, bpy.context)]
            used = pick_denoiser(a.get("denoiser", "auto"), items)
            cy.use_denoising = used is not None
            if used:
                cy.denoiser = used
        elif engine == "eevee":
            scene.eevee.taa_render_samples = samples
        # Render Result pixels are unreliable headless: round-trip through a float EXR.
        with tempfile.TemporaryDirectory(prefix="agy3d-render-") as td:
            out = os.path.join(td, "r.exr")
            s = scene.render.image_settings
            s.media_type = "IMAGE"  # 5.0: a scene left in MULTI_LAYER_IMAGE or VIDEO does not accept OPEN_EXR until this is set
            s.file_format, s.color_depth, s.color_mode = "OPEN_EXR", "32", "RGBA"
            scene.render.filepath = out
            bpy.ops.render.render(write_still=True)
            img = bpy.data.images.load(out)
            try:
                buf = np.empty(w * h * 4, dtype=np.float32)
                img.pixels.foreach_get(buf)
            finally:
                bpy.data.images.remove(img)
        arr = buf.reshape(h, w, 4)[::-1]  # Blender is bottom-up; publish top-down
        return {"engine": engine, "denoiser": used, "image": PUBLISHED.publish(arr)}

    SOCKET_TYPES = {"NodeSocketGeometry", "NodeSocketFloat", "NodeSocketInt", "NodeSocketBool",
                    "NodeSocketVector", "NodeSocketString", "NodeSocketColor"}

    @op("apply_geonodes")
    def apply_geonodes(a: dict[str, Any]) -> dict[str, Any]:
        spec = prepare_tree_spec(a["tree_json"])
        obj = _obj(a["object_name"])
        nt = bpy.data.node_groups.get(spec["name"]) or bpy.data.node_groups.new(spec["name"], "GeometryNodeTree")
        nt.nodes.clear()
        nt.interface.clear()
        idents: dict[str, str] = {}
        for i in spec["inputs"]:
            if i["type"] not in SOCKET_TYPES:
                raise ValueError(f"unsupported socket type {i['type']!r}")
            item = nt.interface.new_socket(name=i["name"], in_out="INPUT", socket_type=i["type"])
            idents[i["name"]] = item.identifier
        nt.interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
        nodes = {"Group Input": nt.nodes.new("NodeGroupInput"), "Group Output": nt.nodes.new("NodeGroupOutput")}
        for n in spec["nodes"]:
            node = nodes[n["name"]] = nt.nodes.new(n["type"])
            node.name = n["name"]
            for k, v in n.get("props", {}).items():
                setattr(node, k, v)
            for k, v in n.get("inputs", {}).items():
                node.inputs[k].default_value = v
        for src, so, dst, di in spec["links"]:
            nt.links.new(nodes[src].outputs[so], nodes[dst].inputs[di])
        mod = obj.modifiers.get("AGY_GeoNodes") or obj.modifiers.new("AGY_GeoNodes", "NODES")
        mod.node_group = nt
        values = {**{i["name"]: i.get("default") for i in spec["inputs"] if "default" in i}, **a.get("inputs", {})}
        values["is_preview"] = bool(a.get("is_preview", True))
        unknown = sorted(k for k in values if k not in idents)
        if unknown:
            raise KeyError(f"no such input(s) {unknown}; the tree has {sorted(idents)}")
        iface = mod.properties.inputs  # 5.2+: RNA entries named by socket identifier; mod[...] raises TypeError
        for k, v in values.items():
            if k != "Geometry":
                entry = getattr(iface, idents[k])
                entry.value = v
                if not _same_value(entry.value, v):
                    raise RuntimeError(f"input {k!r} did not take the value {v!r} (reads back {entry.value!r})")
        obj.update_tag()
        bpy.context.view_layer.update()
        evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
        return {"object": obj.name, "node_group": nt.name, "is_preview": values["is_preview"], "inputs": sorted(idents),
                "identifiers": dict(idents), "evaluated_vertices": len(evaluated.data.vertices)}

    BAKE_TYPES = {"normal": "NORMAL", "ao": "AO", "roughness": "ROUGHNESS", "diffuse": "DIFFUSE",
                  "emit": "EMIT", "combined": "COMBINED", "position": "POSITION", "uv": "UV"}
    DATA_MAPS = {"normal", "ao", "roughness", "position", "uv"}  # numbers, not colours: stored as they are, never colour-managed

    @op("bake_maps")
    def bake_maps(a: dict[str, Any]) -> dict[str, Any]:
        obj, res = _obj(a["object_name"]), int(a.get("resolution", 1024))
        unknown = [m for m in a["maps"] if m not in BAKE_TYPES]
        if unknown:
            raise ValueError(f"unknown bake maps {unknown}")
        scene = bpy.context.scene
        was = scene.cycles.samples
        samples = was if a.get("samples") is None else int(a["samples"])
        if samples < 1:
            raise ValueError(f"samples must be at least 1, got {samples}")
        out = Path(a["out_dir"])
        out.mkdir(parents=True, exist_ok=True)
        scene.render.engine = "CYCLES"
        scene.cycles.samples = samples
        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        if not any(slot.material for slot in obj.material_slots):
            m = bpy.data.materials.new(f"{obj.name}_bake")
            if obj.material_slots:
                obj.material_slots[0].material = m
            else:
                obj.data.materials.append(m)
        nt = obj.active_material.node_tree
        paths = []
        try:
            for m in a["maps"]:
                image = bpy.data.images.new(f"{obj.name}_{m}", res, res, float_buffer=(m in ("position", "normal")))
                # New images are sRGB (byte) or Linear Rec.709 (float) and are saved through the sRGB transfer function:
                # a roughness of 0.5 would be stored as 0.735. Data maps must be saved as they are.
                image.colorspace_settings.name = "Non-Color" if m in DATA_MAPS else "sRGB"
                node = nt.nodes.new("ShaderNodeTexImage")
                node.image = image
                nt.nodes.active = node
                kw = {"pass_filter": {"COLOR"}} if m == "diffuse" else {}
                try:
                    done = bpy.ops.object.bake(type=BAKE_TYPES[m], **kw)
                    if "FINISHED" not in done:
                        raise RuntimeError(f"bake of {m!r} on {obj.name!r} returned {sorted(done)}")
                    image.filepath_raw = str(out / f"{obj.name}_{m}.png")
                    image.file_format = "PNG"
                    image.save()
                    paths.append(image.filepath_raw)
                finally:
                    nt.nodes.remove(node)
                    bpy.data.images.remove(image)
        finally:
            scene.cycles.samples = was
        return {"paths": paths, "samples": samples}

    @op("import_splat")
    def import_splat(a: dict[str, Any]) -> dict[str, Any]:
        """Import a .ply or .spz splat file with Blender's own importers and say what it became.

        5.3 imports Gaussian-splat data as a POINTCLOUD object whose data.type is GAUSSIAN_SPLAT (``mode``
        "gaussian_splat"); a PLY without splat attributes is an ordinary mesh of vertices (``mode`` "mesh_points").
        """
        path = a["path"]
        ext = Path(path).suffix.lower()
        importer = {".ply": bpy.ops.wm.ply_import, ".spz": bpy.ops.wm.spz_import}.get(ext)
        if importer is None:
            raise ValueError(f"unsupported splat format {ext or 'file'!r}: expected .ply or .spz")
        before = set(bpy.data.objects.keys())
        if "FINISHED" not in importer(filepath=path):
            raise RuntimeError(f"splat import failed for {path}")
        objs = [bpy.data.objects[n] for n in sorted(set(bpy.data.objects.keys()) - before)]
        if not objs:
            raise RuntimeError(f"{path} imported no objects")

        def kind(o: Any) -> str:
            if o.type == "POINTCLOUD":
                return "gaussian_splat" if o.data.type == "GAUSSIAN_SPLAT" else "point_cloud"
            return "mesh_points" if o.type == "MESH" else o.type.lower()

        kinds = [kind(o) for o in objs]
        count = sum(len(o.data.points) if o.type == "POINTCLOUD" else len(o.data.vertices) for o in objs if o.type in ("POINTCLOUD", "MESH"))
        return {"objects": [o.name for o in objs], "kinds": kinds, "mode": "gaussian_splat" if "gaussian_splat" in kinds else kinds[0], "points": count}


# ---- server core
def parent_alive(parent_pid: int) -> bool:
    return os.getppid() == parent_pid


def dispatch(msg: dict[str, Any]) -> dict[str, Any]:
    PUBLISHED.release()
    mid = msg.get("id", 0)
    try:
        fn = OPS.get(msg.get("op", ""))
        if fn is None:
            raise LookupError(f"unknown op {msg.get('op')!r}")
        return proto.ok_response(mid, fn(msg.get("args") or {}))
    except Exception as e:  # noqa: BLE001 - everything is reported to the caller
        return proto.error_response(mid, e, "".join(traceback.format_exc().splitlines(True)[-6:]))


def serve_connection(conn: socket.socket, parent_pid: int, poll: float = 0.25) -> bool:
    """Return True after a clean shutdown request, False when a connection disappears."""
    try:
        while parent_alive(parent_pid):
            if not select.select([conn], [], [], poll)[0]:
                continue
            msg = proto.recv_msg(conn)
            if msg is None:
                break
            if msg.get("op") == "shutdown":
                proto.send_msg(conn, proto.ok_response(msg.get("id", 0), {"bye": True}))
                return True
            proto.send_msg(conn, dispatch(msg))
    except (proto.ProtocolError, OSError):
        pass
    finally:
        PUBLISHED.release()
    return False


def serve(sock_path: str, parent_pid: int, poll: float = 0.25) -> None:
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        if os.path.exists(sock_path):
            os.unlink(sock_path)
        srv.bind(sock_path)
        srv.listen(1)
        while parent_alive(parent_pid):
            if not select.select([srv], [], [], poll)[0]:
                continue
            conn, _ = srv.accept()
            with conn:
                if serve_connection(conn, parent_pid, poll):
                    break
    finally:
        PUBLISHED.release()
        srv.close()
        try:
            os.unlink(sock_path)
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" in argv:  # blender ... --python daemon.py -- --socket X
        argv = argv[argv.index("--") + 1:]
    p = argparse.ArgumentParser()
    transport = p.add_mutually_exclusive_group(required=True)
    transport.add_argument("--socket")
    transport.add_argument("--socket-fd", type=int)
    p.add_argument("--fake", action="store_true")
    p.add_argument("--parent-pid", type=int, default=os.getppid())
    ns = p.parse_args(argv)
    bpy = None
    if not ns.fake:
        try:
            import bpy  # type: ignore[import-not-found,no-redef]
        except ImportError:
            print("bpy not importable; use --fake outside Blender", file=sys.stderr)
            return 2
    if bpy is None:
        register_fake_ops()
    else:
        register_bpy_ops(bpy)
    if ns.socket_fd is None:
        serve(ns.socket, ns.parent_pid)
    else:
        with socket.socket(fileno=ns.socket_fd) as conn:
            serve_connection(conn, ns.parent_pid)
    return 0


if __name__ == "__main__":
    rc = main()
    if rc:
        sys.exit(rc)
