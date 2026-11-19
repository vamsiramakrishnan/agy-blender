"""Blender worker daemon.

    blender --background --factory-startup --python daemon.py -- --socket <path>
    python daemon.py --fake --socket <path>          # tests: no bpy

The protocol/dispatch core (everything above ``# ---- bpy handlers``) is plain
CPython and VERIFIED HERE through the ``--fake`` handler set. The bpy handlers
are written against the Blender 5.3 Python API but have NEVER been executed in
this container (no Blender binary); treat them as unverified until the
``@pytest.mark.blender`` tests have run on a real build.

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


proto = _load_sibling("protocol")
linter = _load_sibling("linter")
shim = _load_sibling("shim_5_3")

Handler = Callable[[dict[str, Any]], Any]
OPS: dict[str, Handler] = {}
DENOISER_CHAIN = ("DLSS", "OPTIX", "OPENIMAGEDENOISE")  # preference order; filtered by what the build offers
ENGINE_IDS = {"workbench": "BLENDER_WORKBENCH", "eevee": "BLENDER_EEVEE", "cycles": "CYCLES"}
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


def prepare_tree_spec(tree_json: str | dict[str, Any]) -> dict[str, Any]:
    """Validates a node-tree spec and guarantees the boolean ``is_preview`` input.

    Spec: ``{name, inputs:[{name, type, default}], nodes:[{name, type, props, inputs}],
    links:[[node, out_socket, node, in_socket]]}``; ``Group Input``/``Group Output``
    node names are implicit. The first input is always the geometry.
    """
    spec = json.loads(tree_json) if isinstance(tree_json, str) else dict(tree_json)
    spec.setdefault("name", "AGY_GeoNodes")
    spec.setdefault("nodes", [])
    spec.setdefault("links", [])
    inputs = [dict(i) for i in spec.get("inputs", [])]
    if not any(i.get("type") == "NodeSocketGeometry" for i in inputs):
        inputs.insert(0, {"name": "Geometry", "type": "NodeSocketGeometry"})
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
            "blender": getattr(getattr(bpy, "app", None), "version_string", None), "ops": sorted(OPS)}


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

    for name in ("import_usd", "export_usd", "reset_scene", "import_splat"):
        OPS[name] = (lambda n: lambda a: {"fake": True, "op": n, **{k: v for k, v in a.items() if k != "source"}})(name)

    @op("apply_geonodes")
    def apply_geonodes_fake(a: dict[str, Any]) -> dict[str, Any]:
        spec = prepare_tree_spec(a["tree_json"])
        return {"fake": True, "object": a["object_name"], "is_preview": bool(a.get("is_preview", True)),
                "inputs": [i["name"] for i in spec["inputs"]]}

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


# ---- bpy handlers (written to spec against Blender 5.3; UNVERIFIED here)
def register_bpy_ops(bpy: Any) -> None:
    import numpy as np

    shim.install(bpy)

    def _obj(name: str) -> Any:
        obj = bpy.data.objects.get(name)
        if obj is None:
            raise KeyError(f"no object named {name!r}")
        return obj

    @op("import_usd")
    def import_usd(a: dict[str, Any]) -> dict[str, Any]:
        bpy.ops.wm.usd_import(filepath=a["path"])
        return {"objects": [o.name for o in bpy.context.scene.objects]}

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
        scene.render.engine = ENGINE_IDS[engine]
        scene.render.resolution_x, scene.render.resolution_y = w, h
        scene.render.resolution_percentage = 100
        used = None
        if engine == "cycles":
            cy = scene.cycles
            cy.samples = samples
            items = [i.identifier for i in bpy.types.CyclesRenderSettings.bl_rna.properties["denoiser"].enum_items]
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
        for k, v in values.items():
            if k in idents and k != "Geometry":
                mod[idents[k]] = v
        obj.update_tag()
        return {"object": obj.name, "node_group": nt.name, "is_preview": values["is_preview"], "inputs": sorted(idents)}

    BAKE_TYPES = {"normal": "NORMAL", "ao": "AO", "roughness": "ROUGHNESS", "diffuse": "DIFFUSE",
                  "emit": "EMIT", "combined": "COMBINED", "position": "POSITION", "uv": "UV"}

    @op("bake_maps")
    def bake_maps(a: dict[str, Any]) -> dict[str, Any]:
        obj, res = _obj(a["object_name"]), int(a.get("resolution", 1024))
        unknown = [m for m in a["maps"] if m not in BAKE_TYPES]
        if unknown:
            raise ValueError(f"unknown bake maps {unknown}")
        out = Path(a["out_dir"])
        out.mkdir(parents=True, exist_ok=True)
        bpy.context.scene.render.engine = "CYCLES"
        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        if not obj.data.materials:
            m = bpy.data.materials.new(f"{obj.name}_bake")
            m.use_nodes = True
            obj.data.materials.append(m)
        nt = obj.active_material.node_tree
        paths = []
        for m in a["maps"]:
            image = bpy.data.images.new(f"{obj.name}_{m}", res, res, float_buffer=(m in ("position", "normal")))
            node = nt.nodes.new("ShaderNodeTexImage")
            node.image = image
            nt.nodes.active = node
            kw = {"pass_filter": {"COLOR"}} if m == "diffuse" else {}
            try:
                bpy.ops.object.bake(type=BAKE_TYPES[m], **kw)
                image.filepath_raw = str(out / f"{obj.name}_{m}.png")
                image.file_format = "PNG"
                image.save()
                paths.append(image.filepath_raw)
            finally:
                nt.nodes.remove(node)
                bpy.data.images.remove(image)
        return {"paths": paths}

    @op("import_splat")
    def import_splat(a: dict[str, Any]) -> dict[str, Any]:
        path = a["path"]
        ext = Path(path).suffix.lower()
        if ext == ".ply":  # imported as a vertex proxy; Blender has no native splat renderer
            before = set(bpy.data.objects.keys())
            bpy.ops.wm.ply_import(filepath=path)
            new = sorted(set(bpy.data.objects.keys()) - before)
            return {"objects": new, "mode": "point_proxy"}
        raise NotImplementedError(f"{ext or 'file'} splats need external decoding to .ply first")


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


def serve(sock_path: str, parent_pid: int, poll: float = 0.25) -> None:
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        if os.path.exists(sock_path):
            os.unlink(sock_path)
        srv.bind(sock_path)
        srv.listen(1)
        running = True
        while running and parent_alive(parent_pid):
            if not select.select([srv], [], [], poll)[0]:
                continue
            conn, _ = srv.accept()
            with conn:
                while running and parent_alive(parent_pid):
                    if not select.select([conn], [], [], poll)[0]:
                        continue
                    try:
                        msg = proto.recv_msg(conn)
                    except (proto.ProtocolError, OSError):
                        break
                    if msg is None:
                        break
                    if msg.get("op") == "shutdown":
                        PUBLISHED.release()
                        proto.send_msg(conn, proto.ok_response(msg.get("id", 0), {"bye": True}))
                        running = False
                        break
                    try:
                        proto.send_msg(conn, dispatch(msg))
                    except OSError:
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
    p.add_argument("--socket", required=True)
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
    serve(ns.socket, ns.parent_pid)
    return 0


if __name__ == "__main__":
    rc = main()
    if rc:
        sys.exit(rc)
