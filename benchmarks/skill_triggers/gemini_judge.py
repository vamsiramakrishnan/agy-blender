#!/usr/bin/env python3
"""Judge command for run_trigger_evals.py: reads the selector prompt on stdin, asks a Gemini text model, prints the
model's answer (a JSON list of skill names) on stdout.

It sends nothing but the selector prompt (the skill descriptions and one request): no files, no project facts. The
key is read from GEMINI_API_KEY only (never from an argument, so it is not in the process list or in a shell
history). The default model is the cheapest text tier of this project (models.HYDROGEN); override it with --model or
AGY3D_SKILL_JUDGE_MODEL. Temperature 0 and no thinking setting: the task is one choice from a short list, and the
tier's lowest thinking level is the default of the service.

    GEMINI_API_KEY=... python run_trigger_evals.py --judge-cmd "python gemini_judge.py" --jobs 4 --json > scores.json
"""

from __future__ import annotations

import argparse
import os
import sys

DEFAULT_MODEL = "gemini-4-hydrogen"


def ask(prompt: str, model: str, *, client=None) -> str:
    """The model's text answer to ``prompt``; ``client`` is a google-genai client (made from the environment if None)."""
    if client is None:
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            raise SystemExit("gemini_judge: GEMINI_API_KEY is not set")
        from google import genai

        client = genai.Client(api_key=key)
    from google.genai import types

    config = types.GenerateContentConfig(temperature=0.0, response_mime_type="application/json")
    result = client.models.generate_content(model=model, contents=prompt, config=config)
    return result.text or ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--model", default=os.environ.get("AGY3D_SKILL_JUDGE_MODEL") or DEFAULT_MODEL)
    args = parser.parse_args(argv)
    prompt = sys.stdin.read()
    if not prompt.strip():
        raise SystemExit("gemini_judge: empty prompt on stdin")
    print(ask(prompt, args.model))
    return 0


if __name__ == "__main__":
    sys.exit(main())
