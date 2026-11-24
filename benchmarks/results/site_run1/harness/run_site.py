"""run_site.py ROOT WORK: one sandboxed Antigravity SDK session (model gemini-4-neon, this project's plugin attached in
its default mode) on the task in TASK.md, in the workspace WORK. Uses the comparison harness's relay (every model call
counted against a share, image calls refused) and sandbox; only the model, the limits and the task differ."""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, "benchmarks")
import compare_agents as ca  # noqa: E402

root, work = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
MODEL = os.environ.get("SITE_MODEL", "gemini-4-neon")
SHARE = int(os.environ.get("SITE_SHARE_TOKENS", "45000000"))
WALL_S = int(os.environ.get("SITE_WALL_S", str(3 * 3600)))
LIMITS = {"max_model_calls": int(os.environ.get("SITE_MAX_MODEL_CALLS", "450")),
          "max_tool_calls": int(os.environ.get("SITE_MAX_TOOL_CALLS", "1400"))}
ca.BUDGET.update(ceiling_tokens=int(os.environ.get("SITE_CEILING_TOKENS", SHARE + 5_000_000)), ceiling_images=0, round="site")

_spec = ca.stock_spec


def spec(run_dir, arm, brief, **kw):
    out = _spec(run_dir, arm, brief, **kw)
    path = Path(out["host_config"])
    cfg = json.loads(path.read_text())
    cfg["model"] = MODEL
    cfg["max_output_tokens"] = None          # the SDK's own default (65,535), not the comparison's 16,384
    cfg["budget"] = LIMITS
    cfg["host"]["node_dir"] = "/opt/node22"   # bound read-only so that `node` and `npm` on PATH work in the sandbox
    path.write_text(json.dumps(cfg, indent=1))
    return out


ca.stock_spec = spec
root.mkdir(parents=True, exist_ok=True)
relay, _ = ca.make_relay(root, live=True, allow_new_ceilings=True)
brief = ca.Brief("site", "a website for Straitjacket", 0, (0.0, 0.0), False, "not a comparison brief")
task = (HERE / os.environ.get("SITE_TASK", "TASK.md")).read_text()
print(f"model {MODEL} | share {SHARE:,} tokens, 0 images | limits {LIMITS} | wall {WALL_S} s | workspace {work}", flush=True)
rec = ca.run_stock(relay, root, brief, "N", wall_s=WALL_S, share_tokens=SHARE, share_images=0, plugin=True,
                   account=os.environ.get("SITE_ACCOUNT", "site:neon"), workspace=work, task=task)
print(json.dumps({k: getattr(rec, k) for k in ("status", "wall_s", "model_calls", "tool_calls", "images", "tokens", "models", "notes", "refused")}, indent=1, default=str))
print("FINAL TEXT\n" + (rec.final_text or ""))
