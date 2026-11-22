#!/usr/bin/env python
"""A plain Blender MCP server: arm V of ``compare_agents.py`` (the "vanilla MCP system").

What the common Blender MCP servers give an agent, and nothing of this project's: run Python in one long-lived
Blender session, read the scene, and get an image of the current scene from a chosen view. Modelled on the tool set
of the most used open-source Blender MCP server (``execute_blender_code``, ``get_scene_info``, ``get_object_info``,
``get_viewport_screenshot``) without its asset-library and generation services (Poly Haven, Sketchfab, Hyper3D):
the sandbox has no network, and none of them is Blender.

Two processes, one file (stdlib only, because it is copied into the sandbox and must not import this project):

- ``serve`` (the MCP server, stdio, JSON-RPC 2.0 one message per line): started by the agent's harness. On the first
  tool call it starts Blender and talks to it over a unix socket.
- ``bridge`` (inside Blender: ``blender -b --factory-startup --python THIS -- bridge SOCKET``): a blocking loop that
  executes each request in Blender's main thread. The session lives as long as the server: objects, data and the
  Python namespace of ``execute_blender_code`` persist between calls, as in a GUI Blender with the add-on.

Screenshots: a background Blender has no viewport, so "the viewport" is a Workbench render (Blender's solid viewport
renderer, textures shown) from a camera framed on the visible meshes, or a Cycles render on request. Workbench needs
an OpenGL context: measured on this machine, Blender 5.3 in ``-b`` with no X display exits at the first render, and
with Xvfb a 512 px Workbench render takes 0.8 s (EEVEE 35 s, Cycles at 8 samples 2.2 s, software GL). The sandbox
starts Xvfb for this arm, as a desktop Blender would have a display.
"""
from __future__ import annotations

import json
import os
import sys

SERVER_NAME = "blender"
SERVER_VERSION = "1.0"
VIEWS = ("front", "back", "left", "right", "top", "three_quarter", "camera")

TOOLS = [
    {"name": "get_scene_info",
     "description": "Get information about the current Blender scene: every object (name, type, location, dimensions, "
                    "material names), totals (objects, meshes, vertices, triangles), the scene's bounds and its materials.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_object_info",
     "description": "Get detailed information about one object: transform, world bounds, mesh counts, UV maps, "
                    "modifiers and materials (with the image textures their node trees use).",
     "inputSchema": {"type": "object", "properties": {"object_name": {"type": "string", "description": "Object name"}},
                     "required": ["object_name"]}},
    {"name": "execute_blender_code",
     "description": "Execute Python code in the running Blender session (bpy is imported; variables persist between "
                    "calls). Returns what the code printed, or the error with its traceback. Break large work into "
                    "smaller calls.",
     "inputSchema": {"type": "object", "properties": {"code": {"type": "string", "description": "Python code to run"}},
                     "required": ["code"]}},
    {"name": "get_viewport_screenshot",
     "description": "Get an image of the current scene. view: front | back | left | right | top | three_quarter | camera "
                    "(the scene's own camera). shading: solid (Blender's solid viewport look, textures shown) or "
                    "rendered (Cycles). The camera frames every visible mesh.",
     "inputSchema": {"type": "object", "properties": {
         "view": {"type": "string", "enum": list(VIEWS), "default": "three_quarter"},
         "shading": {"type": "string", "enum": ["solid", "rendered"], "default": "solid"},
         "max_size": {"type": "integer", "default": 800, "description": "Largest side of the image in pixels (64-1024)"}}}},
]


# ----------------------------------------------------------------------------------------------- the MCP side


class BlenderLink:
    """The Blender process and its socket; started on first use, restarted if it died."""

    def __init__(self, blender: str, sock: str, log: str) -> None:
        self.blender, self.sock, self.log, self.proc = blender, sock, log, None

    def _start(self) -> None:
        import subprocess
        import time

        if os.path.exists(self.sock):
            os.unlink(self.sock)
        out = open(self.log, "ab")
        self.proc = subprocess.Popen([self.blender, "-b", "--factory-startup", "--python", os.path.abspath(__file__), "--",
                                      "bridge", self.sock], stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                     start_new_session=True)
        t0 = time.monotonic()
        while not os.path.exists(self.sock):
            if self.proc.poll() is not None:
                raise RuntimeError(f"Blender exited during start-up (code {self.proc.returncode}); see {self.log}")
            if time.monotonic() - t0 > 120:
                raise RuntimeError("Blender did not start within 120 s")
            time.sleep(0.2)

    def call(self, op: str, **args: object) -> dict:
        import socket

        if self.proc is None or self.proc.poll() is not None:
            if self.proc is not None:
                note = f"Blender had exited (code {self.proc.returncode}); a new session was started and the earlier scene is gone"
            else:
                note = None
            self._start()
        else:
            note = None
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(self.sock)
            s.sendall(json.dumps({"op": op, "args": args}).encode() + b"\n")
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(1 << 20)
                if not chunk:
                    break
                buf += chunk
        if not buf:
            code = self.proc.wait(5) if self.proc else None
            raise RuntimeError(f"Blender closed the connection (exit code {code}); the session is gone. See {self.log}")
        out = json.loads(buf)
        if note:
            out["note"] = note
        return out


def _content(op: str, res: dict) -> tuple[list[dict], bool]:
    if not res.get("ok"):
        return [{"type": "text", "text": f"Error: {res.get('error')}" + (f"\n{res['note']}" if res.get("note") else "")}], True
    if op == "screenshot":
        return [{"type": "image", "data": res["png_b64"], "mimeType": "image/png"},
                {"type": "text", "text": json.dumps({k: v for k, v in res.items() if k not in ("png_b64", "ok")})}], False
    body = res.get("output") if op == "exec" else json.dumps(res.get("result"), indent=1)
    if res.get("note"):
        body = f"{res['note']}\n{body}"
    return [{"type": "text", "text": body if body else "(no output)"}], False


def serve() -> None:
    blender = os.environ.get("VANILLA_BLENDER") or "blender"
    home = os.environ.get("HOME", "/tmp")
    link = BlenderLink(blender, os.environ.get("VANILLA_SOCKET", "/tmp/vanilla_blender.sock"),
                       os.path.join(home, "vanilla_blender.log"))
    calls_log = open(os.path.join(home, "vanilla_mcp_calls.jsonl"), "a", buffering=1)
    ops = {"get_scene_info": "scene_info", "get_object_info": "object_info", "execute_blender_code": "exec",
           "get_viewport_screenshot": "screenshot"}

    def reply(mid: object, result: dict | None = None, error: dict | None = None) -> None:
        msg = {"jsonrpc": "2.0", "id": mid}
        msg.update({"error": error} if error else {"result": result})
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            reply(None, error={"code": -32700, "message": "parse error"})
            continue
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:  # a notification (initialized, cancelled): nothing to answer
            continue
        if method == "initialize":
            reply(mid, {"protocolVersion": (msg.get("params") or {}).get("protocolVersion", "2025-06-18"),
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}})
        elif method == "ping":
            reply(mid, {})
        elif method == "tools/list":
            reply(mid, {"tools": TOOLS})
        elif method == "tools/call":
            p = msg.get("params") or {}
            name, args = p.get("name"), dict(p.get("arguments") or {})
            import time

            t0 = time.monotonic()
            if name not in ops:
                content, err = [{"type": "text", "text": f"Error: unknown tool {name!r}"}], True
            else:
                try:
                    content, err = _content(ops[name], link.call(ops[name], **args))
                except Exception as e:  # noqa: BLE001 - reported to the agent like any tool error
                    content, err = [{"type": "text", "text": f"Error: {type(e).__name__}: {e}"}], True
            calls_log.write(json.dumps({"t": time.time(), "tool": name, "args": {k: (v[:2000] if isinstance(v, str) else v)
                                                                                for k, v in args.items()},
                                        "error": err, "wall_s": round(time.monotonic() - t0, 2),
                                        "kinds": [c["type"] for c in content]}) + "\n")
            reply(mid, {"content": content, "isError": err})
        else:
            reply(mid, error={"code": -32601, "message": f"method not found: {method}"})
    if link.proc is not None and link.proc.poll() is None:
        link.proc.terminate()


# ----------------------------------------------------------------------------------------------- the Blender side


def _scene_info() -> dict:
    import bpy

    objs, tris, verts = [], 0, 0
    lo, hi = [float("inf")] * 3, [float("-inf")] * 3
    dg = bpy.context.evaluated_depsgraph_get()
    for o in bpy.context.scene.objects:
        rec = {"name": o.name, "type": o.type, "location": [round(v, 4) for v in o.location],
               "dimensions": [round(v, 4) for v in o.dimensions], "visible": not o.hide_get() and not o.hide_render,
               "materials": [s.material.name for s in getattr(o, "material_slots", []) if s.material]}
        if o.type == "MESH":
            me = o.evaluated_get(dg).to_mesh()
            me.calc_loop_triangles()
            rec["triangles"], rec["vertices"] = len(me.loop_triangles), len(me.vertices)
            tris += rec["triangles"]
            verts += rec["vertices"]
            o.evaluated_get(dg).to_mesh_clear()
            for c in o.bound_box:
                w = o.matrix_world @ __import__("mathutils").Vector(c)
                for i in range(3):
                    lo[i], hi[i] = min(lo[i], w[i]), max(hi[i], w[i])
        objs.append(rec)
    bounds = [[round(v, 4) for v in lo], [round(v, 4) for v in hi]] if verts else None
    return {"scene": bpy.context.scene.name, "units": bpy.context.scene.unit_settings.system,
            "object_count": len(objs), "mesh_count": sum(1 for o in objs if o["type"] == "MESH"),
            "vertices": verts, "triangles_evaluated": tris, "bounds_world": bounds, "objects": objs[:200],
            "objects_listed": min(len(objs), 200),
            "materials": [{"name": m.name, "users": m.users} for m in bpy.data.materials],
            "images": [{"name": i.name, "size": list(i.size), "filepath": i.filepath} for i in bpy.data.images]}


def _object_info(object_name: str) -> dict:
    import bpy
    from mathutils import Vector

    o = bpy.data.objects.get(object_name)
    if o is None:
        raise ValueError(f"no object named {object_name!r}")
    rec = {"name": o.name, "type": o.type, "parent": o.parent.name if o.parent else None,
           "location": list(o.location), "rotation_euler": list(o.rotation_euler), "scale": list(o.scale),
           "dimensions": list(o.dimensions), "modifiers": [{"name": m.name, "type": m.type} for m in o.modifiers]}
    ws = [o.matrix_world @ Vector(c) for c in o.bound_box]
    rec["world_bounds"] = [[min(w[i] for w in ws) for i in range(3)], [max(w[i] for w in ws) for i in range(3)]]
    if o.type == "MESH":
        me = o.data
        me.calc_loop_triangles()
        rec.update(vertices=len(me.vertices), edges=len(me.edges), faces=len(me.polygons), triangles=len(me.loop_triangles),
                   uv_maps=[u.name for u in me.uv_layers])
    mats = []
    for s in getattr(o, "material_slots", []):
        m = s.material
        if m is None:
            continue
        imgs = [n.image.name for n in (m.node_tree.nodes if m.node_tree else []) if n.type == "TEX_IMAGE" and n.image]
        mats.append({"name": m.name, "image_textures": imgs})
    rec["materials"] = mats
    return rec


def _screenshot(view: str = "three_quarter", shading: str = "solid", max_size: int = 800) -> dict:
    import base64
    import math
    import tempfile

    import bpy
    from mathutils import Vector

    if view not in VIEWS:
        raise ValueError(f"view must be one of {', '.join(VIEWS)}")
    size = max(64, min(int(max_size), 1024))
    sc = bpy.context.scene
    r = sc.render
    saved = {"engine": r.engine, "x": r.resolution_x, "y": r.resolution_y, "pct": r.resolution_percentage,
             "path": r.filepath, "camera": sc.camera, "fmt": r.image_settings.file_format}
    pts = [o.matrix_world @ Vector(c) for o in sc.objects if o.type == "MESH" and not o.hide_render and o.visible_get()
           for c in o.bound_box]
    cam_obj = None
    try:
        if view == "camera":
            if sc.camera is None:
                raise ValueError("the scene has no camera; choose another view")
        else:
            if not pts:
                pts = [Vector((-1, -1, 0)), Vector((1, 1, 1))]
            lo = Vector([min(p[i] for p in pts) for i in range(3)])
            hi = Vector([max(p[i] for p in pts) for i in range(3)])
            az, el = {"front": (0, 5), "back": (180, 5), "left": (-90, 5), "right": (90, 5), "top": (0, 89),
                      "three_quarter": (-35, 25)}[view]
            az, el = math.radians(az), math.radians(el)
            d = Vector((math.sin(az) * math.cos(el), -math.cos(az) * math.cos(el), math.sin(el)))
            cd = bpy.data.cameras.new("_mcp_view")
            cd.lens = 50.0
            cam_obj = bpy.data.objects.new("_mcp_view", cd)
            sc.collection.objects.link(cam_obj)
            target = (lo + hi) / 2
            radius = max((hi - lo).length / 2, 0.01)
            fov = 2 * math.atan(cd.sensor_width / (2 * cd.lens))
            dist = radius / math.sin(fov / 2) * 1.05
            cam_obj.location = target + d * dist
            cam_obj.rotation_euler = (-d).to_track_quat("-Z", "Y").to_euler()
            cd.clip_start, cd.clip_end = 0.001, dist * 4 + 50
            sc.camera = cam_obj
        if shading == "rendered":
            r.engine = "CYCLES"
            sc.cycles.samples = 16
            sc.cycles.device = "CPU"
        else:
            r.engine = "BLENDER_WORKBENCH"
            sh = sc.display.shading
            sh.light, sh.color_type = "STUDIO", "TEXTURE"
        r.resolution_x = r.resolution_y = size
        r.resolution_percentage = 100
        r.image_settings.file_format = "PNG"
        out = os.path.join(tempfile.gettempdir(), "mcp_screenshot.png")
        r.filepath = out
        bpy.ops.render.render(write_still=True)
        data = open(out, "rb").read()
    finally:
        r.engine, r.resolution_x, r.resolution_y = saved["engine"], saved["x"], saved["y"]
        r.resolution_percentage, r.filepath, r.image_settings.file_format = saved["pct"], saved["path"], saved["fmt"]
        sc.camera = saved["camera"]
        if cam_obj is not None:
            cd = cam_obj.data
            bpy.data.objects.remove(cam_obj, do_unlink=True)
            bpy.data.cameras.remove(cd)
    return {"png_b64": base64.b64encode(data).decode(), "view": view, "shading": shading, "width": size, "height": size}


def bridge(sock_path: str) -> None:
    import contextlib
    import io
    import socket
    import traceback

    import bpy

    namespace: dict = {"bpy": bpy, "__name__": "__mcp__"}
    if os.path.exists(sock_path):
        os.unlink(sock_path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    srv.listen(4)
    while True:
        conn, _ = srv.accept()
        with conn:
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = conn.recv(1 << 20)
                if not chunk:
                    break
                buf += chunk
            try:
                req = json.loads(buf)
                op, args = req["op"], req.get("args") or {}
                if op == "exec":
                    out = io.StringIO()
                    try:
                        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                            exec(compile(args["code"], "<execute_blender_code>", "exec"), namespace)  # noqa: S102
                        res = {"ok": True, "output": out.getvalue()[-20000:]}
                    except BaseException:  # noqa: BLE001 - SystemExit from the agent's code must not end the session
                        res = {"ok": False, "error": (out.getvalue()[-6000:] + "\n" + traceback.format_exc()[-6000:]).strip()}
                elif op == "scene_info":
                    res = {"ok": True, "result": _scene_info()}
                elif op == "object_info":
                    res = {"ok": True, "result": _object_info(**args)}
                elif op == "screenshot":
                    res = {"ok": True, **_screenshot(**args)}
                else:
                    res = {"ok": False, "error": f"unknown op {op!r}"}
            except Exception as e:  # noqa: BLE001
                res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            with contextlib.suppress(OSError):
                conn.sendall(json.dumps(res, default=str).encode() + b"\n")


if __name__ == "__main__":
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]
    if argv and argv[0] == "bridge":
        bridge(argv[1])
    elif argv and argv[0] == "serve":
        serve()
    else:
        raise SystemExit("usage: compare_vanilla_mcp.py serve | (inside Blender) -- bridge SOCKET")
