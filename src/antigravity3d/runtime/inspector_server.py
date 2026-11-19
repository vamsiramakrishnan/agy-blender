"""Localhost server behind the antigravity-3d inspector side pane.

Run as the ``inspector`` UI sidecar::

    python -m antigravity3d.runtime.inspector_server [--root DIR] [--port N]

Serves the self-contained viewer pages that
:func:`antigravity3d.artifacts.inspector.build_inspector` writes, from one
directory (``--root`` / ``$AGY3D_INSPECTOR_DIR``, default
``$ANTIGRAVITY_EXECUTABLE_DATA_DIR/inspector``). The port comes from
``$ANTIGRAVITY_SIDECAR_WEB_PORT``, which the sidecar manager injects for
``has_web_ui`` sidecars (sidecar docs: "If your sidecar spins up a web server,
it should do it on top of this port").

- ``/`` -- HTML index of the viewer pages, newest first (the AUX_PANE view path)
- ``/api/list`` -- the same as JSON
- ``/<name>.html`` etc. -- files under the root

Binds 127.0.0.1 only. Requests whose path has a ``..`` segment, a dot-file
segment, or resolves (after symlinks) outside the root get 403/404.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import signal
import sys
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOOPBACK = "127.0.0.1"


def list_viewers(root: Path) -> list[dict[str, object]]:
    items = []
    for p in root.glob("*.html"):
        if p.name.startswith(".") or not p.is_file():
            continue
        st = p.stat()
        items.append({"name": p.name, "bytes": st.st_size, "mtime": st.st_mtime})
    items.sort(key=lambda d: (-float(d["mtime"]), str(d["name"])))  # type: ignore[arg-type]
    return items


def _index_html(items: list[dict[str, object]]) -> str:
    rows = "\n".join(
        f'<li><a href="{urllib.parse.quote(str(i["name"]))}">{html.escape(str(i["name"]))}</a> '
        f'<span class="m">{time.strftime("%Y-%m-%d %H:%M", time.localtime(float(i["mtime"])))} · '  # type: ignore[arg-type]
        f'{int(i["bytes"]) // 1024} KiB</span></li>'  # type: ignore[call-overload]
        for i in items
    ) or "<li><em>No inspector pages yet. They appear here as tools generate them.</em></li>"
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'><title>antigravity-3d inspector</title>"
        "<style>body{font:14px system-ui,sans-serif;margin:12px;color:var(--foreground,#222)}"
        "li{margin:4px 0}.m{opacity:.65;font-size:12px}</style></head><body>"
        f"<h3>3D inspector</h3><ul>{rows}</ul></body></html>"
    )


class InspectorHandler(SimpleHTTPRequestHandler):
    server_version = "agy3d-inspector/1"
    root: Path = Path(".")

    def __init__(self, *a, **kw):  # type: ignore[no-untyped-def]
        super().__init__(*a, directory=str(self.root), **kw)

    def log_message(self, fmt: str, *args: object) -> None:  # keep sidecar logs terse
        sys.stderr.write(f"[inspector] {self.address_string()} {fmt % args}\n")

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _segments(self) -> list[str] | None:
        raw = urllib.parse.urlsplit(self.path).path
        segs = [s for s in urllib.parse.unquote(raw).replace("\\", "/").split("/") if s]
        if any(s == ".." or s.startswith(".") for s in segs):
            return None
        return segs

    def _send_bytes(self, body: bytes, ctype: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _route(self) -> bool:
        """Handles index / API / rejections. Returns True when the request is done."""
        segs = self._segments()
        if segs is None:
            self.send_error(HTTPStatus.FORBIDDEN, "path traversal rejected")
            return True
        if not segs or segs == ["index.html"] and not (self.root / "index.html").exists():
            self._send_bytes(_index_html(list_viewers(self.root)).encode(), "text/html; charset=utf-8")
            return True
        if segs == ["api", "list"]:
            self._send_bytes(json.dumps(list_viewers(self.root)).encode(), "application/json")
            return True
        target = self.root.joinpath(*segs).resolve()
        if not target.is_relative_to(self.root) or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return True
        return False

    def do_GET(self) -> None:  # noqa: N802
        if not self._route():
            super().do_GET()

    def do_HEAD(self) -> None:  # noqa: N802
        if not self._route():
            super().do_HEAD()

    def list_directory(self, path):  # type: ignore[no-untyped-def]  # never list subdirectories
        self.send_error(HTTPStatus.NOT_FOUND)
        return None


def make_server(root: Path, port: int = 0, host: str = LOOPBACK) -> ThreadingHTTPServer:
    if host != LOOPBACK:
        raise ValueError("the inspector server binds 127.0.0.1 only")
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    handler = type("BoundInspectorHandler", (InspectorHandler,), {"root": root})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv


def _default_root() -> Path:
    env = os.environ.get("AGY3D_INSPECTOR_DIR")
    if env:
        return Path(env)
    data = os.environ.get("ANTIGRAVITY_EXECUTABLE_DATA_DIR")
    return (Path(data) if data else Path.home() / ".agy3d") / "inspector"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="antigravity3d.runtime.inspector_server")
    ap.add_argument("--root", type=Path, default=_default_root())
    ap.add_argument("--port", type=int, default=int(os.environ.get("ANTIGRAVITY_SIDECAR_WEB_PORT", "0")))
    ns = ap.parse_args(argv)
    srv = make_server(ns.root, ns.port)
    print(f"[inspector] serving {ns.root.resolve()} on http://{LOOPBACK}:{srv.server_address[1]}/", flush=True)

    def _stop(*_: object) -> None:
        threading.Thread(target=srv.shutdown, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _stop)
    try:
        srv.serve_forever(poll_interval=0.25)
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
