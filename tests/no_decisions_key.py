"""A local stand-in for the Gemini REST API as a key WITHOUT the native Decisions API sees it (offline, no quota).

* Any path that is not a ``/models/`` call or a Files upload answers 404: the Decisions API (``/v1/decisions``)
  answers 404, as it does on the owner's key, so ``make_control_plane`` falls back to the generative judge.
* ``:countTokens``: ``len(json body) // 4``; 400 INVALID_ARGUMENT when an image given by file reference carries a
  per-image resolution, as the real API answers (seen 2026-11-21 on gemini-4-neon: it counts an inline image with a
  resolution and a referenced image without one, and generates with either).
* A structured judge call (a question-keyed response schema): defects ``defect``, policy ``policy``,
  ``needs_ortho_blueprint`` 0.8, choice 0.9 on the preferred option, score 0.9 on level ``score_level`` (default top).
  Usage is the measured blockout fallback call: 2,312 input, 169 answer + 837 thinking tokens.
  A judge call slower (``judge_delay_s``) than the deadline its request carries answers 504 DEADLINE_EXCEEDED when
  that deadline runs out. A geometry ranking (labelled contact sheets) answers 0.9 to "legible text" unless the
  question sets the renderer's view labels aside. Both as the live service did on 2026-11-21.
* Authoring (SceneIR schema): the project's TemplateAuthor scene for the requested prompt and asset name; repair
  schema: an empty patch. Usage: counted prompt, 1,140 answer + 1,066 thinking tokens (measured hydrogen medians).
* Image calls: a PNG of the requested size; usage 62 input, image tokens of the tier + 321, 804 thinking (measured 2K).
  The request's text is logged as ``prompt`` (``image_prompts()``).
* Files API resumable upload: a file URI.

    with NoDecisionsKey() as api:        # api.url, api.requests (path, kind)
        monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", api.url); monkeypatch.setenv("GEMINI_API_KEY", "dummy")
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Self

import numpy as np
from PIL import Image

KIT_TEMPLATES = ("wall_segment", "floor_tile", "pillar", "doorway")


class NoDecisionsKey:
    def __init__(self, *, defect: float = 0.05, policy: float = 0.02, score_level: str | None = None,
                 overrides: dict[str, float] | None = None, refuse_questions: tuple[str, ...] = (),
                 image_delay_s: float = 0.0, judge_delay_s: float = 0.0, author_delay_s: float = 0.0,
                 sheet_labels_are_text: bool = True) -> None:
        self.defect, self.policy, self.score_level = defect, policy, score_level
        # A judge call asking any of these question ids (without the __N suffix) answers 400 INVALID_ARGUMENT, as the
        # live API answered every geometry grading call on 2026-11-21: a step that fails early, on demand.
        self.refuse_questions = tuple(refuse_questions)
        self.image_delay_s = image_delay_s  # how long an image call takes (a live 2K image took about 21 s)
        # A live fallback judge call took 7 to 22 s, and a texture ranking 176 s once and more than 240 s once. A call that
        # takes longer than the deadline the request carries (X-Server-Timeout) is answered 504 DEADLINE_EXCEEDED when the
        # deadline runs out, as the live service did on 2026-11-21.
        self.judge_delay_s = judge_delay_s
        # A geometry candidate is a contact sheet with a view label in each tile. Asked whether it contains legible text
        # without those labels being set aside, the live judge answered yes (2026-11-21), and so does this one.
        self.sheet_labels_are_text = sheet_labels_are_text
        self.author_delay_s = author_delay_s  # a live hydrogen propose took 10-38 s
        self.overrides = dict(overrides or {})  # question id (without the __N suffix) -> P(yes)
        self.requests: list[dict[str, Any]] = []
        self._png: dict[int, bytes] = {}
        self._lock = threading.Lock()
        api = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send(self, status: int, payload: bytes, headers: dict[str, str] | None = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:  # noqa: N802 - http.server API
                api._log(self.path, "404")
                self._send(404, b'{"error": {"code": 404, "message": "not found (stand-in)", "status": "NOT_FOUND"}}')

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                status, payload, headers = api.answer(self.path, dict(self.headers), raw)
                self._send(status, payload, headers)

            def log_message(self, *a: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> Self:
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _log(self, path: str, kind: str, **extra: Any) -> None:
        with self._lock:
            self.requests.append({"path": path, "kind": kind, "t": time.monotonic(), **extra})  # extra may set t (arrival)

    def kinds(self) -> list[str]:
        return [r["kind"] for r in self.requests]

    def generated(self) -> int:
        """Generation calls served (judge, author, image): what would be billed."""
        return sum(r["kind"] in ("judge", "author:propose", "author:repair", "image:sent") for r in self.requests)

    # ---- answers
    def answer(self, path: str, headers: dict[str, str], raw: bytes) -> tuple[int, bytes, dict[str, str]]:
        arrived = time.monotonic()
        if path.startswith("/upload/"):
            if headers.get("X-Goog-Upload-Command", "").startswith("start"):
                return 200, b"{}", {"X-Goog-Upload-URL": f"{self.url}/upload-session/{uuid.uuid4().hex}"}
            return 404, b"{}", {}
        if path.startswith("/upload-session/"):
            fid = uuid.uuid4().hex[:12]
            self._log(path, "upload")
            f = {"name": f"files/{fid}", "uri": f"https://stand-in.files/v1beta/files/{fid}", "mimeType": "image/png", "state": "ACTIVE"}
            return 200, json.dumps({"file": f}).encode(), {"X-Goog-Upload-Status": "final"}
        body = json.loads(raw or b"{}")
        if ":countTokens" in path:
            parts = [part for c in body.get("contents") or [] for part in c.get("parts") or []]
            if any("fileData" in part and "mediaResolution" in part for part in parts):
                self._log(path, "count:400")
                return 400, b'{"error": {"code": 400, "message": "Request contains an invalid argument.", "status": "INVALID_ARGUMENT"}}', {}
            self._log(path, "count")
            return 200, json.dumps({"totalTokens": len(json.dumps(body)) // 4}).encode(), {}
        if "/models/" not in path:
            self._log(path, "404")
            return 404, b'{"error": {"code": 404, "message": "Requested entity was not found.", "status": "NOT_FOUND"}}', {}
        gc = body.get("generationConfig") or {}
        schema = gc.get("responseJsonSchema") or gc.get("responseSchema")
        prompt = len(json.dumps(body)) // 4
        if "IMAGE" in (gc.get("responseModalities") or []):
            self._log(path, "image:sent", t=arrived)  # dispatched: billed even if the caller stops waiting for it
            if self.image_delay_s:
                time.sleep(self.image_delay_s)
            tier = (gc.get("imageConfig") or {}).get("imageSize") or "1K"
            size, img = {"1K": (1024, 1120), "2K": (2048, 1680), "4K": (4096, 4096)}[tier]
            usage = {"promptTokenCount": 62, "candidatesTokenCount": img + 321, "thoughtsTokenCount": 804,
                     "totalTokenCount": 62 + img + 321 + 804, "candidatesTokensDetails": [{"modality": "IMAGE", "tokenCount": img}]}
            content = {"parts": [{"inlineData": {"mimeType": "image/png", "data": base64.b64encode(self._image(size)).decode()}}]}
            kind = f"image:{tier}"
        elif schema and "nodes" in (schema.get("properties") or {}):
            time.sleep(self.author_delay_s)
            content = {"parts": [{"text": json.dumps(self._scene(body))}]}
            usage = {"promptTokenCount": prompt, "candidatesTokenCount": 1140, "thoughtsTokenCount": 1066, "totalTokenCount": prompt + 2206}
            kind = "author:propose"
        elif schema and set(schema.get("properties") or {}) == {"ops"}:
            content = {"parts": [{"text": json.dumps({"ops": []})}]}
            usage = {"promptTokenCount": prompt, "candidatesTokenCount": 92, "thoughtsTokenCount": 515, "totalTokenCount": prompt + 607}
            kind = "author:repair"
        elif schema and any(re.sub(r"__\d+$", "", q) in self.refuse_questions for q in (schema.get("properties") or {})):
            self._log(path, "judge:refused")
            return 400, b'{"error": {"code": 400, "message": "Request contains an invalid argument. (stand-in)", "status": "INVALID_ARGUMENT"}}', {}
        elif schema:
            deadline = next((float(v) for k, v in headers.items() if k.lower() == "x-server-timeout"), None)
            if deadline is not None and self.judge_delay_s > deadline:
                time.sleep(deadline)
                self._log(path, "judge:504", t=arrived)
                return 504, b'{"error": {"code": 504, "message": "Deadline expired before operation could complete.", "status": "DEADLINE_EXCEEDED"}}', {}
            time.sleep(self.judge_delay_s)
            answers = self._judge(schema)
            if self.sheet_labels_are_text and any(re.fullmatch(r"defect_(floating|clipping)__\d+", q) for q in answers):
                # a geometry ranking: its candidates are labelled contact sheets (see __init__)
                asked = dict(re.findall(r"### (policy_legible_text__\d+)  \[noul\]\n([^\n]*)", _text(body)))
                for qid, text in asked.items():
                    if "Apart from those labels" not in text:
                        answers[qid] = 0.9
            content = {"parts": [{"text": json.dumps(answers)}]}
            usage = {"promptTokenCount": 2312, "candidatesTokenCount": 169, "thoughtsTokenCount": 837, "totalTokenCount": 3318}
            kind = "judge"
        else:
            content = {"parts": [{"text": "done"}]}
            usage = {"promptTokenCount": prompt, "candidatesTokenCount": 1, "totalTokenCount": prompt + 1}
            kind = "text"
        extra = {"prompt": _text(body)} if kind.startswith("image:") else {}
        self._log(path, kind, max_output_tokens=gc.get("maxOutputTokens"), t=arrived, **extra)
        ev = {"candidates": [{"content": {"role": "model", **content}, "finishReason": "STOP"}], "usageMetadata": usage,
              "responseId": uuid.uuid4().hex}
        return 200, json.dumps(ev).encode(), {}

    def _judge(self, schema: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for qid, sch in (schema.get("properties") or {}).items():
            base = re.sub(r"__\d+$", "", qid)
            if sch.get("type") == "number":
                out[qid] = self.overrides.get(base, self.policy if base.startswith("policy_") else 0.8 if base == "needs_ortho_blueprint"
                                              else self.defect if base.startswith("defect_") else 0.5)
                continue
            keys = list((sch.get("properties") or {}).keys())
            pref = {"pipeline_dag": "prop_to_engine", "next_action": "approve_export", "complexity_tier": "1"}.get(base)
            if all(k.isdigit() for k in keys) and base != "complexity_tier":
                pref = self.score_level if self.score_level in keys else keys[-1]
            if pref not in keys:
                pref = keys[0]
            rest = 0.1 / max(1, len(keys) - 1)
            out[qid] = {k: (0.9 if k == pref else rest) for k in keys}
        return out

    def _scene(self, body: dict[str, Any]) -> dict[str, Any]:
        from antigravity3d.kernel.kit_metric import KitMetric
        from antigravity3d.state import EngineBudget
        from antigravity3d.tactics.authoring import TemplateAuthor, pick_template

        text = _text(body)
        name = re.search(r"asset_name='([^']+)'", text).group(1)
        prompt = re.search(r"Build design \d+/\d+ for (.*?)\. (?:Use the attached|Return one JSON)", text, re.S).group(1)
        ctx: dict[str, Any] = {"asset_name": name}
        if pick_template(prompt) in KIT_TEMPLATES:
            ctx["kit_metric"] = KitMetric.interior()
        scene = asyncio.run(TemplateAuthor().propose(prompt, budget=EngineBudget("unreal_5"), n=1, context=ctx))[0]
        return scene.model_dump(mode="json", exclude_defaults=True)

    def image_prompts(self) -> list[str]:
        """The text of every image request served, in order."""
        return [r["prompt"] for r in self.requests if r["kind"].startswith("image:") and "prompt" in r]

    def _image(self, size: int) -> bytes:
        if size not in self._png:
            arr = (np.random.default_rng(size).random((size, size, 3)) * 60 + 100).astype(np.uint8)
            buf = io.BytesIO()
            Image.fromarray(arr).save(buf, "PNG", compress_level=1)
            self._png[size] = buf.getvalue()
        return self._png[size]


def _text(body: dict[str, Any]) -> str:
    return " ".join(p.get("text", "") for c in body.get("contents", []) for p in c.get("parts", []) if isinstance(p, dict))
