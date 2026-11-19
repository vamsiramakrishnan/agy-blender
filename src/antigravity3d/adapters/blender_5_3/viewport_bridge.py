"""Live-viewport bridge for artists (interactive Blender). Written to spec; NOT
exercised in this container (no Blender, no GUI).

A background thread accepts Unix-socket connections and reads newline-delimited
JSON (stdlib only: artists' Blender has no msgpack). The thread never touches
bpy; it only enqueues. A ``bpy.app.timers`` callback on the main thread drains
the queue. Messages: ``{"cmd": "load_usd", "path": ...}``,
``{"cmd": "set_variant", "prim": ..., "set": ..., "variant": ...}``,
``{"cmd": "frame_selected"}``. ``set_variant`` follows a naming convention
(objects ``<prim>__<set>__<variant>``) because Blender exposes no USD variant API.
"""

import json
import os
import queue
import socket
import threading

import bpy

bl_info = {"name": "AGY3D Viewport Bridge", "blender": (5, 3, 0), "category": "Import-Export"}
SOCKET = os.environ.get("AGY3D_VIEWPORT_SOCKET", os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "agy3d-viewport.sock"))
_q: "queue.Queue[dict]" = queue.Queue()
_srv: "socket.socket | None" = None


def _client(conn: socket.socket) -> None:
    with conn, conn.makefile("r", encoding="utf-8") as f:
        for line in f:
            try:
                _q.put(json.loads(line))
            except ValueError:
                pass


def _listen(srv: socket.socket) -> None:
    while True:
        try:
            conn, _ = srv.accept()
        except OSError:
            return  # socket closed by unregister()
        threading.Thread(target=_client, args=(conn,), daemon=True).start()


def _frame_selected() -> None:
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == "VIEW_3D":
                region = next(r for r in area.regions if r.type == "WINDOW")
                with bpy.context.temp_override(window=win, area=area, region=region):
                    bpy.ops.view3d.view_selected()
                return


def _set_variant(prim: str, vset: str, variant: str) -> None:
    for o in bpy.data.objects:
        head = f"{prim}__{vset}__"
        if o.name.startswith(head):
            o.hide_viewport = o.hide_render = not o.name.endswith(f"__{variant}")


def _apply(m: dict) -> None:
    cmd = m.get("cmd")
    if cmd == "load_usd":
        bpy.ops.wm.usd_import(filepath=m["path"])
    elif cmd == "set_variant":
        _set_variant(m["prim"], m["set"], m["variant"])
    elif cmd == "frame_selected":
        _frame_selected()


def _drain() -> float:
    while not _q.empty():
        try:
            _apply(_q.get_nowait())
        except Exception as e:  # keep the timer alive on a bad message
            print(f"[agy3d bridge] {e!r}")
    return 0.1


def register() -> None:
    global _srv
    if os.path.exists(SOCKET):
        os.unlink(SOCKET)
    _srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    _srv.bind(SOCKET)
    _srv.listen(4)
    threading.Thread(target=_listen, args=(_srv,), daemon=True).start()
    bpy.app.timers.register(_drain, persistent=True)


def unregister() -> None:
    if bpy.app.timers.is_registered(_drain):
        bpy.app.timers.unregister(_drain)
    if _srv:
        _srv.close()
    if os.path.exists(SOCKET):
        os.unlink(SOCKET)
