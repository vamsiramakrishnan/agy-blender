"""A local Gemini API stand-in that records what the Antigravity harness sends and answers from a script.

The local harness posts plain Gemini REST requests (``POST /v1beta/models/<model>:streamGenerateContent?alt=sse``,
JSON body with ``systemInstruction``, ``tools[].functionDeclarations``, ``contents``, ``generationConfig``) to
``GeminiAPIEndpoint(base_url=...)``. This server keeps every request body and replies with one SSE event per
scripted answer, so an offline test can drive a real ``Agent`` through a stage and assert on the assembled request.

    with GeminiStub([call("apply_scene_ir", patch={...}), text("done")]) as stub:
        cfg = stub.route(build_stage_agent(...), tmp_path)
        async with Agent(cfg) as agent: ...
        stub.requests[0]  # the parsed JSON body of the first model call

``prompt_breakdown(body)`` attributes the static prompt by system section and by tool declaration (characters).
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

Answer = dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]]
_SECTION = re.compile(r"<([a-z_]{3,40})>\n(.*?)\n</\1>", re.S)


def text(t: str) -> dict[str, Any]:
    return {"parts": [{"text": t}]}


def call(tool: str, /, **args: Any) -> dict[str, Any]:
    return {"parts": [{"functionCall": {"name": tool, "args": args}}]}


class GeminiStub:
    """Answers scripted turns in order; once the script is exhausted it answers ``fallback``."""

    def __init__(self, script: list[Answer] | None = None, fallback: Answer | None = None) -> None:
        self.script = list(script or [])
        self.fallback = fallback or text("done")
        self.requests: list[dict[str, Any]] = []
        self.paths: list[str] = []
        self._lock = threading.Lock()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server API
                body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
                with stub._lock:
                    stub.requests.append(body)
                    stub.paths.append(self.path)
                    answer = stub.script.pop(0) if stub.script else stub.fallback
                content = answer(body) if callable(answer) else answer
                event = {"candidates": [{"content": {"role": "model", **content}, "finishReason": "STOP"}],
                         "usageMetadata": {"promptTokenCount": len(json.dumps(body)) // 4, "candidatesTokenCount": 1,
                                           "totalTokenCount": len(json.dumps(body)) // 4 + 1}}
                payload = f"data: {json.dumps(event)}\n\n".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> "GeminiStub":
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()

    def route(self, cfg: Any, workdir: Path) -> Any:
        """``cfg`` (a LocalAgentConfig) with every model pointed at this stub (thinking options kept)."""
        from google.antigravity.models import GeminiAPIEndpoint

        def ep(m: Any) -> Any:
            return m.model_copy(update={"endpoint": GeminiAPIEndpoint(base_url=self.url, api_key="local-stub",
                                                                      options=m.endpoint.options)})

        return cfg.model_copy(update={"model": None, "models": [ep(m) for m in cfg.models],
                                      "app_data_dir": str(workdir / "app-data"), "save_dir": str(workdir / "sessions")})


def system_text(body: dict[str, Any]) -> str:
    return "".join(p.get("text", "") for p in (body.get("systemInstruction") or {}).get("parts", []))


def declarations(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {d["name"]: d for t in body.get("tools") or [] for d in t.get("functionDeclarations") or []}


def sections(body: dict[str, Any]) -> dict[str, str]:
    """Top-level ``<tag>...</tag>`` sections of the system instruction (later duplicates win)."""
    return {m.group(1): m.group(0) for m in _SECTION.finditer(system_text(body))}


def prompt_breakdown(body: dict[str, Any]) -> dict[str, Any]:
    """Static characters per call: system sections, tool declarations, and the totals."""
    sys_text = system_text(body)
    secs = {k: len(v) for k, v in sections(body).items()}
    tools = {k: len(json.dumps(v, separators=(",", ":"))) for k, v in declarations(body).items()}
    return {"system_chars": len(sys_text), "sections": secs, "unsectioned_chars": len(sys_text) - sum(secs.values()),
            "tool_chars": sum(tools.values()), "tools": tools, "static_chars": len(sys_text) + sum(tools.values())}
