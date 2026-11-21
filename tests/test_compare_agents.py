"""benchmarks/compare_agents.py: spend limits, relay accounting, the sandbox, the facts read from a GLB, the judge
plan and the gallery. No live call: the model side is a local stand-in (``FakeUpstream``)."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BENCH = ROOT / "benchmarks"


def _load(name: str):
    if str(BENCH) not in sys.path:
        sys.path.insert(0, str(BENCH))
    spec = importlib.util.spec_from_file_location(name, BENCH / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def ca():
    return _load("compare_agents")


@pytest.fixture(autouse=True)
def _budget_restored():
    """main() applies its flags to the module's BUDGET; give every test the committed one back."""
    import copy

    mod = sys.modules.get("compare_agents")
    saved = copy.deepcopy(mod.BUDGET) if mod else None
    yield
    if mod is not None and saved is not None:
        mod.BUDGET.clear()
        mod.BUDGET.update(saved)


@pytest.fixture(scope="module")
def ce():
    return _load("compare_eval")


# ----------------------------------------------------------------------------------------------- design


def test_the_round_2_plan_fits_the_ceilings_and_tells_each_arm_its_budget(ca):
    b = ca.BUDGET
    assert (b["ceiling_tokens"], b["ceiling_images"], b["round"]) == (1_700_000, 12, "r2")
    t = ca.plan_totals()
    assert t == {"tokens": 500_000 + 300_000 + 2 * 12_500 + 200_000 + 225_000, "images": 12}  # continuations reuse images
    assert ca.order() == [("crate", "A")] and ca.check_plan() is None
    n = ca.calls_allowed("A")
    assert n == 11 and f"about {n} model calls" in ca.task_text(ca.BRIEF_BY_ID["crate"], n)  # measured growth since round 2
    assert ca.ARM_LABEL["C"] == "this project's pipeline (command line, preview mode)"
    assert "model calls" not in ca.task_text(ca.BRIEF_BY_ID["crate"])  # round 1's text, unchanged
    assert ca.cell_name("crate", "A") == "r2:crate:A"


def test_round_1_accounts_must_be_closed_before_round_2_fits_and_closing_never_lowers_what_was_booked(ca, tmp_path, capsys,
                                                                                                    monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "not-a-key")  # never used: the plan check refuses before any relay exists
    led = ca.Ledger(tmp_path / "ledger.json")
    for name in ("crate:A", "crate:B", "hydrant:B", "hydrant:A", "smoke:crate:A"):
        led.open_account(name, 195_000 if name != "smoke:crate:A" else 40_000, 1 if "smoke" not in name else 0)
        led.settle(led.reserve(name, 30_000, 0), {"input": 25_000, "output": 500, "thinking": 400}, 0)
    led.reserve("hydrant:A", 35_982, 0)  # a call in flight when round 1 was stopped
    assert ca.main(["live", "--go", "--root", str(tmp_path), "--resume", "--only", "crate:A"]) == 2  # does not fit
    assert "counting what" in capsys.readouterr().err
    again = ca.Ledger(tmp_path / "ledger.json")  # reloading books the in-flight reservation as unknown usage
    assert ca.main(["close", "--root", str(tmp_path), "--accounts", "crate:A", "crate:B", "hydrant:B", "hydrant:A",
                    "smoke:crate:A", "--reason", "round 1 stopped"]) == 0
    data = json.loads((tmp_path / "ledger.json").read_text())
    assert data["accounts"]["hydrant:A"]["share_tokens"] == 25_900 + 35_982 and data["accounts"]["hydrant:A"]["closed"]
    assert len(data["closures"]) == 5 and all(c["reason"] == "round 1 stopped" for c in data["closures"])
    assert again is not None and ca.check_plan(tmp_path / "ledger.json") is None
    with pytest.raises(ca.LimitReached):
        ca.Ledger(tmp_path / "ledger.json").reserve("crate:A", 1, 0)  # a closed account spends nothing more


def test_a_plan_over_a_ceiling_is_refused_before_anything_starts(ca, capsys):
    assert ca.main(["plan", "--briefs", "crate", "hydrant", "chair"]) == 2
    assert "refusing: the plan needs" in capsys.readouterr().err
    assert ca.main(["plan", "--briefs", "crate", "hydrant", "chair", "--ceiling-tokens", "3000000", "--ceiling-images", "36"]) == 0


def test_a_live_start_refuses_a_ledger_that_already_shows_spending_and_prints_the_ceilings(ca, tmp_path, monkeypatch, capsys):
    led = ca.Ledger(tmp_path / "ledger.json")
    led.open_account("x", 1000, 0)
    led.settle(led.reserve("x", 10, 0), {"input": 5}, 0)
    monkeypatch.setenv("GEMINI_API_KEY", "not-a-key")  # never used: the guard refuses first
    assert ca.main(["live", "--go", "--root", str(tmp_path), "--skip-eval"]) == 2
    out = capsys.readouterr()
    assert "already shows spending" in out.err and "1,700,000 tokens, 12 images" in out.out
    assert ca.main(["live", "--root", str(tmp_path)]) == 2  # no --go
    monkeypatch.delenv("GEMINI_API_KEY")
    assert ca.main(["smoke", "--go", "--root", str(tmp_path / "s")]) == 2  # no key: refused


def test_arms_a_and_b_get_the_same_task_text(ca):
    for b in ca.BRIEFS:
        t = ca.task_text(b)
        assert b.prompt in t and f"{b.max_tris} triangles" in t and "output/" in t and "Godot 4" in t
        assert "skill" not in t.lower()  # nothing in the text points at B's extra material


# ----------------------------------------------------------------------------------------------- ledger


def test_ledger_refuses_a_call_that_could_exceed_the_share_and_keeps_unknown_usage(ca, tmp_path):
    led = ca.Ledger(tmp_path / "l.json", ceiling_tokens=1000, ceiling_images=3)
    led.open_account("r", 600, 1)
    with pytest.raises(ValueError):
        led.open_account("s", 500, 1)  # shares above the ceiling are refused at planning time
    r = led.reserve("r", 400, 0)
    with pytest.raises(ca.LimitReached):
        led.reserve("r", 300, 0)  # 400 in flight + 300 > 600
    led.settle(r, {"input": 100, "output": 20, "thinking": 30}, 0)
    r2 = led.reserve("r", 400, 1)
    led.settle(r2, None, 0)  # unknown usage: the reservation stays booked, the image too
    a = led.data["accounts"]["r"]
    assert ca.Ledger.used_tokens(a) == 150 + 400 and a["images"] == 1
    with pytest.raises(ca.LimitReached):
        led.reserve("r", 100, 0)
    with pytest.raises(ca.LimitReached):
        led.reserve("r", 0, 1)
    assert len(a["refused"]) == 3


def test_ledger_survives_a_restart_without_forgetting_spend(ca, tmp_path):
    led = ca.Ledger(tmp_path / "l.json", ceiling_tokens=1000, ceiling_images=3)
    led.open_account("r", 600, 1)
    led.reserve("r", 250, 0)  # never settled: the process dies here
    again = ca.Ledger(tmp_path / "l.json", ceiling_tokens=1000, ceiling_images=3)
    assert ca.Ledger.used_tokens(again.data["accounts"]["r"]) == 250
    with pytest.raises(ValueError):
        ca.Ledger(tmp_path / "l.json", ceiling_tokens=5000, ceiling_images=3)


# ----------------------------------------------------------------------------------------------- relay


def _post(url: str, path: str, body: dict) -> tuple[int, dict | str]:
    req = urllib.request.Request(url + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return r.status, raw if raw.startswith("data:") else json.loads(raw)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_relay_counts_usage_logs_function_calls_and_refuses_before_forwarding(ca, tmp_path):
    fake = ca.FakeUpstream({"r": [ca._fc("run_command", CommandLine="ls")]})
    try:
        led = ca.Ledger(tmp_path / "l.json")
        relay = ca.Relay(led, fake.url, None, fake=True)
        acc = relay.open("r", 20_000, 0, tmp_path, sandboxed=False)
        body = {"contents": [{"role": "user", "parts": [{"text": "x" * 300}]}], "generationConfig": {"maxOutputTokens": 1000}}
        status, _ = _post(acc.endpoint, "/v1beta/models/gemini-4-hydrogen:streamGenerateContent?alt=sse", body)
        assert status == 200
        a = led.data["accounts"]["r"]
        assert a["calls"] == 1 and a["input_tokens"] > 0 and a["inflight_tokens"] == 0
        assert acc.function_calls == [{"name": "run_command", "args": {"CommandLine": "ls"}}]
        n_before = len(fake.requests)
        big = {"contents": [{"parts": [{"text": "x"}]}], "generationConfig": {"maxOutputTokens": 50_000}}
        status, err = _post(acc.endpoint, "/v1beta/models/gemini-4-hydrogen:generateContent", big)
        assert status == 400 and "spend limit" in err["error"]["message"]
        assert len(fake.requests) == n_before  # never forwarded
        status, _ = _post(acc.endpoint, "/v1beta/models/gemini-4-hydrogen:countTokens", body)
        assert status == 200 and led.data["accounts"]["r"]["free_calls"] == 1
        img = relay.open("img", 50_000, 1, tmp_path / "i", sandboxed=False)
        ibody = {"contents": [{"parts": [{"text": "albedo"}]}], "generationConfig": {"responseModalities": ["IMAGE"], "maxOutputTokens": 4000}}
        assert _post(img.endpoint, "/v1beta/models/gemini-3.1-flash-lite-image:generateContent", ibody)[0] == 200
        assert _post(img.endpoint, "/v1beta/models/gemini-3.1-flash-lite-image:generateContent", ibody)[0] == 400  # image share
        lines = [json.loads(x) for x in (tmp_path / "relay_calls.jsonl").read_text().splitlines()]
        assert [x["status"] for x in lines] == [200, 400, 200]
    finally:
        fake.close()


def test_relay_for_a_sandboxed_run_only_serves_generation_paths_and_never_runs_live_by_mistake(ca, tmp_path):
    led = ca.Ledger(tmp_path / "l.json")
    with pytest.raises(ValueError):
        ca.Relay(led, ca.UPSTREAM, None, fake=True)  # a dry run cannot point at the real API
    with pytest.raises(ValueError):
        ca.Relay(led, "http://127.0.0.1:9", "k", fake=False)
    fake = ca.FakeUpstream()
    try:
        relay = ca.Relay(led, fake.url, None, fake=True)
        acc = relay.open("s", 10_000, 0, tmp_path, sandboxed=True)
        status, err = _post(acc.endpoint, "/v1beta/files:upload", {})
        assert status == 403 and fake.requests == []
    finally:
        fake.close()


def test_parse_response_reads_sse_and_json_usage_images_and_calls(ca):
    ev = {"candidates": [{"content": {"parts": [{"text": "hi"}, {"inlineData": {"mimeType": "image/png", "data": "AA=="}},
                                                {"functionCall": {"name": "f", "args": {}}}]}, "finishReason": "STOP"}],
          "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "thoughtsTokenCount": 2, "totalTokenCount": 20}}
    for payload, ctype in ((f"data: {json.dumps(ev)}\n\n".encode(), "text/event-stream"), (json.dumps(ev).encode(), "application/json")):
        p = ca.parse_response(payload, ctype)
        assert p["usage"] == {"input": 13, "output": 5, "thinking": 2, "cached": 0}  # untemised total booked as input
        assert p["images"] == 1 and p["function_calls"] == [{"name": "f", "args": {}}] and p["text"] == "hi"


def test_upstream_skills_install_like_the_installer_copy_mode(ca, tmp_path):
    if not (ca.UPSKILLS / "skills").is_dir():
        pytest.skip("upstream checkout not present")
    names = ca.install_upstream_skills(tmp_path)
    assert "blender-core" in names and len(names) == len(list((ca.UPSKILLS / "skills").iterdir()))
    assert (tmp_path / ".agents" / "skills" / "blender-core" / "SKILL.md").is_file()
    assert not any(p.is_symlink() for p in (tmp_path / ".agents" / "skills").rglob("*"))


# ----------------------------------------------------------------------------------------------- the stock harness, offline


def test_the_headless_harness_attaches_skills_only_through_skills_paths(ca, tmp_path):
    """Why arm B passes skills_paths: the SDK harness does not read <workspace>/.agents/skills on its own."""
    from google.antigravity import Agent
    from google.antigravity.connections.local.local_connection_config import LocalAgentConfig
    from google.antigravity.hooks import policy
    from google.antigravity.models import GeminiAPIEndpoint, ModelTarget, ModelType

    ws = tmp_path / "ws"
    sk = ws / ".agents" / "skills" / "demo-skill"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text("---\nname: demo-skill\ndescription: Demo skill.\n---\n# Demo\n")
    fake = ca.FakeUpstream()
    try:
        led = ca.Ledger(tmp_path / "l.json")
        relay = ca.Relay(led, fake.url, None, fake=True)
        listed = []
        for i, extra in enumerate(({}, {"skills_paths": [str(ws / ".agents" / "skills")]})):
            acc = relay.open(f"h{i}", 500_000, 0, tmp_path / f"h{i}", sandboxed=False)
            ep = GeminiAPIEndpoint(base_url=acc.endpoint, api_key="x")
            cfg = LocalAgentConfig(models=[ModelTarget(name="gemini-4-hydrogen", types=[ModelType.TEXT], endpoint=ep),
                                           ModelTarget(name="img", types=[ModelType.IMAGE], endpoint=ep)],
                                   policies=[policy.workspace_only([str(ws)]), policy.allow_all()], workspaces=[str(ws)],
                                   app_data_dir=str(tmp_path / f"app{i}"), save_dir=str(tmp_path / f"s{i}"), **extra)

            async def go(cfg=cfg) -> None:
                async with Agent(cfg) as agent:
                    await (await agent.chat("hi")).text()

            asyncio.run(asyncio.wait_for(go(), 120))
            listed.append(json.loads((tmp_path / f"h{i}" / "relay_calls.jsonl").read_text().splitlines()[0])["skills_listed"])
        assert "demo-skill" not in listed[0] and "demo-skill" in listed[1]
    finally:
        fake.close()


def _can_sandbox() -> str | None:
    if os.geteuid() != 0:
        return "needs root for the namespaces"
    if not shutil.which("unshare") or not shutil.which("setpriv"):
        return "needs unshare and setpriv"
    if subprocess.run(["unshare", "--net", "--mount", "--pid", "--fork", "true"], capture_output=True).returncode != 0:
        return "namespaces not permitted here"
    if not Path("/usr/local/bin/python3").exists():
        return "sandbox expects /usr/local/bin/python3"
    return None


def _probes(transcript: Path) -> dict:
    """The probe command's printed JSON, from the tool output the harness sent back to the model."""
    for line in transcript.read_text().splitlines():
        for part in json.loads(line)["sent_last"].get("parts", []):
            out = ((part.get("functionResponse") or {}).get("response") or {}).get("output", "")
            if "PROBES {" in out:
                return json.loads(out.split("PROBES ", 1)[1].splitlines()[0])
    raise AssertionError("no probe output in the transcript")


def test_sandboxed_scripted_stock_agent_writes_a_glb_and_cannot_get_out(ca, tmp_path, monkeypatch):
    """The real harness, in the sandbox, driven by a scripted model: it writes a GLB, and every probe stays closed."""
    if (why := _can_sandbox()) is not None:
        pytest.skip(why)
    blender = os.environ.get("BLENDER_BIN")
    if not blender:  # the sandbox mounts a Blender directory; an empty stand-in is enough without Blender
        stand_in = tmp_path / "no-blender"
        stand_in.mkdir()
        (stand_in / "blender").write_text("#!/bin/sh\nexit 1\n")
        monkeypatch.setenv("BLENDER_BIN", str(stand_in / "blender"))
    os.chmod(tmp_path, 0o755)
    root = tmp_path / "pilot"
    brief = ca.BRIEF_BY_ID["crate"]
    fake = ca.FakeUpstream({ca.cell_name("crate", "A"): ca.scripted_agent(use_blender=False)})
    try:
        led = ca.Ledger(root / "ledger.json")
        relay = ca.Relay(led, fake.url, None, fake=True)
        rec = ca.run_stock(relay, root, brief, "A", wall_s=300)
    finally:
        fake.close()
    assert rec.status == "finished", rec
    assert rec.glb and rec.glb.endswith("output/crate.glb") and Path(rec.glb).stat().st_size > 500
    assert rec.model_calls == 3 and rec.tokens["total"] > 0
    run = root / "runs" / ca.cell_name("crate", "A").replace(":", "_")
    ident = next(json.loads(x) for x in (run / "events.jsonl").read_text().splitlines() if '"identity"' in x)
    assert ident["uid"] == 65534 and ident["pid"] == 1
    probes = _probes(run / "transcript.jsonl")
    for k in ("repo_readme", "upstream_clone", "gitconfig", "worktrees", "net_public", "net_dns", "write_pydeps",
              "write_blender", "remount"):
        assert probes[k].startswith("closed"), (k, probes[k])
    assert probes["plugin_bundle"] == "OPEN: []" and probes["root_home"] == "OPEN: []"
    assert probes["pid1_environ"] == "OPEN: []" and probes["secret_env"] == [] and probes["uid"] == 65534
    assert int(probes["host_procs"].split()[-1]) < 20  # only the sandbox's own processes
    first = json.loads((run / "relay_calls.jsonl").read_text().splitlines()[0])
    assert "search_web" not in first["tool_names"] and "run_command" in first["tool_names"]
    assert first["max_output_tokens"] == ca.STOCK_MAX_OUTPUT


# ----------------------------------------------------------------------------------------------- evaluation


def _box_glb(ca, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, "-c", ca._make_glb_script()], cwd=folder, check=True, capture_output=True)
    return folder / "output" / "crate.glb"


def test_glb_facts_read_scale_uvs_materials_and_textures(ca, ce, tmp_path):
    glb = _box_glb(ca, tmp_path)
    f = ce.glb_facts(glb, size_m=(0.5, 1.6), upright=False, max_tris=3000)
    assert f["triangles"] == 12 and f["draw_calls"] == 1 and f["within_tri_budget"]
    assert f["size_m_xyz"] == [0.8, 0.8, 0.8] and f["grounded"] and f["centred_xz"] and f["scale_plausible"]
    assert f["uv"]["primitives_with_uv"] == 1 and f["uv"]["verts_outside_0_1_frac"] == 0
    assert f["uv"]["by_material"]["0"]["overlap_frac"] == 0.0  # six separate islands
    assert f["materials"] == 1 and not f["pbr_complete"]  # base colour only: no normal or metal-roughness map
    assert f["images"][0]["width"] == 64 and f["images"][0]["roles"] == ["baseColor"] and f["images"][0]["flat"]
    assert ce.sidecar_files(glb) == {"lod_files": [], "collision_files": []}


def test_uv_overlap_counts_stacked_islands(ce):
    import numpy as np

    tri = np.array([[[0.1, 0.1], [0.5, 0.1], [0.1, 0.5]]])
    ov, cov = ce._uv_overlap(np.concatenate([tri, tri]))
    assert ov == 1.0 and 0.05 < cov < 0.1
    ov, _ = ce._uv_overlap(np.concatenate([tri, tri + 0.45]))
    assert ov == 0.0


def test_judge_plan_covers_every_pair_in_both_orders_and_aggregate_names_splits(ce):
    assert len(ce.judge_plan(["crate", "hydrant"], ["A", "B"])) == 4
    plan = ce.judge_plan(["crate", "hydrant", "chair"])
    assert len(plan) == 18
    seen = {(c["brief"], c["first"], c["second"]) for c in plan}
    assert len(seen) == 18 and all((b, y, x) in seen for b, x, y in seen)
    assert plan != ce.judge_plan(["crate", "hydrant", "chair"], seed=1)  # order is shuffled, with a recorded seed

    def v(w, overall):
        return {"criteria": {c: {"winner": w, "reason": ""} for c in ce.CRITERIA}, "overall": {"winner": overall, "reason": ""}}

    calls = [{"brief": "crate", "first": "A", "second": "B", "verdict": v("1", "1")},
             {"brief": "crate", "first": "B", "second": "A", "verdict": v("2", "1")}]
    res = ce.aggregate(calls)["crate:A-B"]["result"]
    assert res["reads_as_brief"] == "A" and res["overall"] == "split"


def test_gallery_is_static_and_says_it_is_a_pilot(ca, ce, tmp_path):
    from PIL import Image

    sheet = tmp_path / "eval" / "crate" / "A" / "sheet.png"
    sheet.parent.mkdir(parents=True)
    Image.new("RGB", (8, 8)).save(sheet)
    results = {"mode": "dry-run", "records": [{"brief": "crate", "arm": "A", "status": "finished", "tokens": {}}],
               "evaluation": {"crate:A": {"glb_exists": True, "sheet": str(sheet)}}, "unfair": list(ca.UNFAIR), "tally": {}}
    out = ce.write_gallery(tmp_path, results, ca.BRIEF_BY_ID, ca.ARM_LABEL)
    text = out.read_text()
    assert "1 observations" in text and "eval/crate/A/sheet.png" in text
    assert "http://" not in text and "https://" not in text and "<script" not in text


def test_a_sandboxed_run_that_reaches_its_share_is_stopped_and_recorded_as_such(ca, tmp_path, monkeypatch):
    if (why := _can_sandbox()) is not None:
        pytest.skip(why)
    if not os.environ.get("BLENDER_BIN"):
        stand_in = tmp_path / "no-blender"
        stand_in.mkdir()
        (stand_in / "blender").write_text("#!/bin/sh\nexit 1\n")
        monkeypatch.setenv("BLENDER_BIN", str(stand_in / "blender"))
    os.chmod(tmp_path, 0o755)
    root = tmp_path / "pilot"
    fake = ca.FakeUpstream({"smoke:crate:A": ca.scripted_agent(use_blender=False)})
    try:
        relay = ca.Relay(ca.Ledger(root / "ledger.json"), fake.url, None, fake=True)
        # the smoke run's shape: room for the first call's worst case, not the second's; no image
        rec = ca.run_stock(relay, root, ca.BRIEF_BY_ID["crate"], "A", wall_s=300, share_tokens=40_000,
                           share_images=0, tag="smoke:")
        forwarded = len(fake.requests)
    finally:
        fake.close()
    assert rec.status == "stopped at limit", rec
    assert rec.model_calls == 1 and forwarded == 1 and rec.refused
    a = json.loads((root / "ledger.json").read_text())["accounts"]["smoke:crate:A"]
    assert ca.Ledger.used_tokens(a) <= 40_000 and a["inflight_tokens"] == 0
    assert (root / "runs" / "smoke_crate_A" / "record.json").exists()
    assert ca.collect_records(root) == []  # the smoke run is not a pilot cell
    assert rec.wall_s < 120


def test_blender_invocations_are_counted_from_the_command_line(ca):
    assert ca.blender_call({"CommandLine": "blender -b --python make.py"})
    assert ca.blender_call({"CommandLine": "cd x && /opt/blender/blender -b -P a.py"})
    assert ca.blender_call({"CommandLine": "python .agents/skills/blender-core/scripts/run_in_blender.py s.py"})
    assert not ca.blender_call({"CommandLine": "ls /opt/blender/x; cat blender_notes.txt"})


def test_plugin_scope_passes_decisions_and_proxies_a_files_api_upload_through_the_relay(ca, tmp_path):
    """The plugin's MCP server probes the Decisions API and uploads renders: both go through the relay, the upload URL
    the API hands out is rewritten to the relay, and nothing for a stock run's account."""
    import io

    from google import genai
    from google.genai import types as gt

    fake = ca.FakeUpstream()
    try:
        led = ca.Ledger(tmp_path / "l.json")
        relay = ca.Relay(led, fake.url, None, fake=True)
        acc = relay.open("c", 50_000, 0, tmp_path / "c", scope="plugin")
        status, _ = _post(acc.endpoint, "/v1/decisions", {"input": "x", "questions": {}})
        assert status == 404  # the condition under test: no native judge on this key
        client = genai.Client(api_key="via-relay", http_options=gt.HttpOptions(base_url=acc.endpoint))
        png = ca._tiny_png()
        f = client.files.upload(file=io.BytesIO(png), config=gt.UploadFileConfig(mime_type="image/png"))
        assert f.uri.endswith("/files/f1")
        log = [json.loads(x) for x in (tmp_path / "c" / "relay_calls.jsonl").read_text().splitlines()]
        kinds = [(r.get("kind"), r["status"]) for r in log]
        assert kinds == [("decisions", 404), ("upload_start", 200), ("upload_data", 200)]
        assert log[-1]["request_bytes"] == len(png) and led.data["accounts"]["c"]["upload_bytes"] == len(png)
        assert ca.Ledger.used_tokens(led.data["accounts"]["c"]) == 0 and led.data["accounts"]["c"]["inflight_tokens"] == 0
        stock = relay.open("a", 50_000, 0, tmp_path / "a", scope="stock")
        assert _post(stock.endpoint, "/v1/decisions", {})[0] == 403
        assert _post(stock.endpoint, "/upload/v1beta/files", {})[0] == 403
    finally:
        fake.close()


def test_the_headless_harness_discovers_the_plugin_nowhere(ca, tmp_path):
    """Why arm C attaches the plugin by hand: installed in each documented place, nothing of it reaches the request."""
    from antigravity3d import plugin_install as pi
    from google.antigravity import Agent
    from google.antigravity.connections.local.local_connection_config import LocalAgentConfig
    from google.antigravity.hooks import policy
    from google.antigravity.models import GeminiAPIEndpoint, ModelTarget, ModelType

    fake = ca.FakeUpstream()
    try:
        relay = ca.Relay(ca.Ledger(tmp_path / "l.json"), fake.url, None, fake=True)
        ws_install, ws_register, ws_plain, home = (tmp_path / n for n in ("ws_install", "ws_register", "ws_plain", "home"))
        for d in (ws_install, ws_register, ws_plain):
            d.mkdir()
        pi.install(ws_install / ".agents", workspace=ws_install)
        pi.register(ws_register)
        pi.install(home / ".gemini" / "config")
        firsts = []
        for i, (ws, env) in enumerate(((ws_install, None), (ws_register, None), (ws_plain, {"HOME": str(home)}))):
            acc = relay.open(f"d{i}", 300_000, 0, tmp_path / f"d{i}", sandboxed=False)
            ep = GeminiAPIEndpoint(base_url=acc.endpoint, api_key="x")
            cfg = LocalAgentConfig(models=[ModelTarget(name="gemini-4-hydrogen", types=[ModelType.TEXT], endpoint=ep),
                                           ModelTarget(name="img", types=[ModelType.IMAGE], endpoint=ep)],
                                   policies=[policy.workspace_only([str(ws)]), policy.allow_all()], workspaces=[str(ws)],
                                   app_data_dir=str(tmp_path / f"app{i}"), save_dir=str(tmp_path / f"s{i}"), env=env)

            async def go(cfg=cfg) -> None:
                async with Agent(cfg) as agent:
                    await (await agent.chat("hi")).text()

            asyncio.run(asyncio.wait_for(go(), 120))
            firsts.append(json.loads((tmp_path / f"d{i}" / "relay_calls.jsonl").read_text().splitlines()[0]))
        body = json.dumps([r for r in fake.requests])
        for f in firsts:
            assert "call_mcp_tool" not in f["tool_names"] and f["skills_listed"] == ["run-workflow"], f
        assert "antigravity-3d_studio" not in body and "parametric-kitbash" not in body and "studio invariants" not in body
    finally:
        fake.close()


def test_plugin_arm_runs_the_plugin_inside_the_sandbox_and_its_server_cannot_get_out(ca, tmp_path, monkeypatch):
    """Arm C against fakes: every plugin part attached from the installed files, limits injected by the harness, the
    plugin's own hooks deciding, and the MCP server confined to the sandbox's network like the agent."""
    if (why := _can_sandbox()) is not None:
        pytest.skip(why)
    if not os.environ.get("BLENDER_BIN"):
        stand_in = tmp_path / "no-blender"
        stand_in.mkdir()
        (stand_in / "blender").write_text("#!/bin/sh\nexit 1\n")
        monkeypatch.setenv("BLENDER_BIN", str(stand_in / "blender"))
    os.chmod(tmp_path, 0o755)
    root = tmp_path / "pilot"
    fake = ca.FakeUpstream({ca.cell_name("crate", "C"): ca.scripted_plugin_agent()})
    try:
        relay = ca.Relay(ca.Ledger(root / "ledger.json", ceiling_tokens=2_000_000), fake.url, None, fake=True)
        rec = ca.run_stock(relay, root, ca.BRIEF_BY_ID["crate"], "C", wall_s=600, share_tokens=600_000, share_images=4)
    finally:
        fake.close()
    run = root / "runs" / ca.cell_name("crate", "C").replace(":", "_")
    events = [json.loads(x) for x in (run / "events.jsonl").read_text().splitlines()]
    att = next(e for e in events if e["kind"] == "plugin_attached")
    assert att["servers"] == ["antigravity-3d_studio"] and set(att["subagents"]) == {"lookdev_critic", "usd_verifier"}
    assert any(r.startswith("AGENTS.md") for r in att["rules"]) and set(att["hooks"]) == {"PreToolUse", "PostToolUse", "SessionStart", "Stop"}
    lim = next(e for e in events if e["kind"] == "limits_injected")["arguments"]
    assert lim["preview"] is True and lim["token_ceiling"] == 600_000 and lim["max_images"] == 4
    hooks = [e for e in events if e["kind"] == "plugin_hook"]
    assert any(h["event"] == "SessionStart" and h["out"].get("injectSteps") for h in hooks)  # the hook scripts ran
    assert any(h["event"] == "PreToolUse" and h["out"].get("decision") == "allow" for h in hooks)
    first = json.loads((run / "relay_calls.jsonl").read_text().splitlines()[0])
    assert "call_mcp_tool" in first["tool_names"] and "studio invariants" in json.dumps(fake.requests[0]["body"])
    log = [json.loads(x) for x in (run / "relay_calls.jsonl").read_text().splitlines()]
    assert any(r.get("kind") == "decisions" and r["status"] == 404 for r in log)  # the condition under test
    probes = None
    for line in (run / "transcript.jsonl").read_text().splitlines():
        for part in json.loads(line)["sent_last"].get("parts", []):
            out = ((part.get("functionResponse") or {}).get("response") or {}).get("output", "")
            if "PLUGIN_PROBES {" in out:
                probes = json.loads(out.split("PLUGIN_PROBES ", 1)[1].splitlines()[0])
    assert probes is not None
    assert probes["mcp_server_found"] and probes["mcp_server_same_netns"] and probes["interfaces"] == ["lo"]
    assert probes["mcp_server_key"] == "via-relay" and probes["mcp_server_base_url_is_local"]
    assert probes["mcp_server_secret_env"] == [] and probes["direct_api_ip"].startswith("closed")
    assert rec.pipeline["mcp_tools"].get("open_asset_session") == 1 and rec.pipeline["self_report"]["qa_records"]


def test_a_continuation_runs_in_the_same_workspace_under_its_own_account_and_the_cell_reports_both_sessions(ca, tmp_path,
                                                                                                            monkeypatch):
    if (why := _can_sandbox()) is not None:
        pytest.skip(why)
    if not os.environ.get("BLENDER_BIN"):
        stand_in = tmp_path / "no-blender"
        stand_in.mkdir()
        (stand_in / "blender").write_text("#!/bin/sh\nexit 1\n")
        monkeypatch.setenv("BLENDER_BIN", str(stand_in / "blender"))
    os.chmod(tmp_path, 0o755)
    root = tmp_path / "pilot"
    brief = ca.BRIEF_BY_ID["crate"]
    first_script = [ca._fc("run_command", CommandLine="mkdir -p build && echo 'x' > build/notes.txt && cat > make_glb.py <<'EOF'\n"
                                                      + ca._make_glb_script() + "\nEOF", Cwd="/home/agent/project"),
                    ca._fc("run_command", CommandLine="ls", Cwd="/home/agent/project")]
    second_script = [ca._fc("run_command", CommandLine="python3 make_glb.py", Cwd="/home/agent/project"),
                     {"parts": [{"text": "delivered"}]}]
    cont = ca.continuation_name("crate", "A", 2)
    fake = ca.FakeUpstream({ca.cell_name("crate", "A"): first_script, cont: second_script})
    try:
        relay = ca.Relay(ca.Ledger(root / "ledger.json"), fake.url, None, fake=True)
        first = ca.run_stock(relay, root, brief, "A", share_tokens=60_000, share_images=1, wall_s=300)  # stops at its share
        assert first.status == "stopped at limit" and first.glb is None
        with pytest.raises(RuntimeError, match="close r2:crate:A first"):
            ca.run_continuation(relay, root, brief, "A", wall_s=300)  # its image is still with the first session
        relay.ledger.close_account(ca.cell_name("crate", "A"), "first session stopped at its share")
        rec = ca.run_continuation(relay, root, brief, "A", wall_s=300)
    finally:
        fake.close()
    assert rec.status == "finished" and rec.account == cont and rec.session == 2
    work = Path(first.pipeline["workspace"])
    assert rec.glb == str(work / "output" / "crate.glb")  # the same workspace, not a new one
    task = json.loads((root / "runs" / cont.replace(":", "_") / "ctl" / "config.json").read_text())["task"]
    assert task.startswith(ca.task_text(brief)) and "ran out of its budget before delivering" in task
    assert "make_glb.py" in task and "build/notes.txt" in task and "model calls in total" in task
    assert "python3" not in task.split("ran out of its budget")[1]  # told what exists, not what to run
    merged = {r.arm: r for r in ca.collect_records(root)}["A"]
    assert [s["session"] for s in merged.pipeline["sessions"]] == [1, 2]
    assert merged.tokens["total"] == first.tokens["total"] + rec.tokens["total"] and merged.glb == rec.glb
    led = json.loads((root / "ledger.json").read_text())["accounts"]
    assert cont in led and led[cont]["share_tokens"] == ca.BUDGET["continuations"]["crate:A"]["tokens"]


def test_arm_c_second_attempt_is_forced_single_candidate_with_the_images_left_and_needs_the_first_closed(ca, tmp_path,
                                                                                                         monkeypatch):
    """The command is captured, not run: the attempt's account, its limits, the forced mode and the fresh output folder."""
    seen = {}

    class FakeProc:
        def __init__(self, cmd, **kw):
            seen["cmd"], seen["env"] = cmd, kw["env"]
            self.pid = os.getpid()

        def wait(self, timeout=None):
            return 2

    monkeypatch.setattr(ca.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(ca, "c_mode", lambda src, n: (False, ["hero: up to 11 image(s)"], ["hero: up to 3 image(s)"]))
    root = tmp_path / "pilot"
    relay = ca.Relay(ca.Ledger(root / "ledger.json"), "http://127.0.0.1:9", None, fake=True)
    relay.ledger.open_account(ca.cell_name("crate", "C"), 300_000, 11)
    (root / "runs" / "r2_crate_C").mkdir(parents=True)
    (root / "runs" / "r2_crate_C" / "record.json").write_text(json.dumps({"brief": "crate", "arm": "C", "status": "gave up"}))
    with pytest.raises(RuntimeError, match="close r2:crate:C first"):
        ca.run_continuation(relay, root, ca.BRIEF_BY_ID["crate"], "C")
    relay.ledger.close_account(ca.cell_name("crate", "C"), "first attempt failed")
    rec = ca.run_continuation(relay, root, ca.BRIEF_BY_ID["crate"], "C")
    cmd = seen["cmd"]
    assert "--single-candidate" in cmd and cmd[cmd.index("--max-images") + 1] == "3"
    assert cmd[cmd.index("--token-ceiling") + 1] == str(225_000 - ca.C_TOKEN_MARGIN)
    assert cmd[cmd.index("--out") + 1].endswith("runs/r2_crate_C.s2/out")  # a fresh output folder
    assert seen["env"]["GEMINI_API_KEY"] == "via-relay" and seen["env"]["GOOGLE_GEMINI_BASE_URL"].startswith("http://127.0.0.1:")
    assert rec.account == "r2:crate:C.s2" and rec.session == 2 and "forced" in rec.pipeline["mode"]
    assert relay.ledger.data["accounts"]["r2:crate:C.s2"]["share_images"] == 3
    merged = {r.arm: r for r in ca.collect_records(root)}["C"]
    assert [s_["session"] for s_ in merged.pipeline["sessions"]] == [1, 2]
