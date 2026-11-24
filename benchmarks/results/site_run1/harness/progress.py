"""progress.py RUN_DIR [N]: where an Antigravity site session stands: model calls, tokens, the last N tool calls, files in site/."""
import json, pathlib, sys, time
R = pathlib.Path(sys.argv[1]); n = int(sys.argv[2]) if len(sys.argv) > 2 else 14
run = R / "runs" / "site_neon"
ev = [json.loads(l) for l in (run / "events.jsonl").read_text().splitlines() if l.strip()]
rows = [json.loads(l) for l in (run / "relay_calls.jsonl").read_text().splitlines() if l.strip()]
ok = [r for r in rows if r.get("status") == 200 and r.get("usage")]
tot = {}
for r in ok:
    for k, v in r["usage"].items():
        if isinstance(v, (int, float)): tot[k] = tot.get(k, 0) + v
t0 = ev[0].get("t", time.time()) if ev else time.time()
calls = [e for e in ev if e.get("kind") == "tool_call"]
print(f"elapsed {(time.time() - t0) / 60:.1f} min | model calls ok {len(ok)} of {len(rows)} | statuses {dict((str(s), sum(1 for r in rows if r.get('status') == s)) for s in {r.get('status') for r in rows})}")
print("models:", {m: sum(1 for r in ok if r.get("model") == m) for m in sorted({r.get("model") for r in ok})})
print("tokens:", tot, "| last call input:", (ok[-1]["usage"].get("input") if ok else None))
names = {}
for c in calls: names[c.get("name")] = names.get(c.get("name"), 0) + 1
print("tool calls:", len(calls), names)
mcp = {}
for c in calls:
    a = c.get("args") or {}
    t = a.get("ToolName") or a.get("tool_name")
    if t: mcp[t] = mcp.get(t, 0) + 1
print("plugin tools:", mcp)
for c in calls[-n:]:
    a = c.get("args") or {}
    s = a.get("CommandLine") or a.get("AbsolutePath") or a.get("TargetFile") or a.get("ToolName") or json.dumps(a)[:160]
    print("  ", c.get("name"), "|", str(s)[:170].replace("\n", " "))
end = [e for e in ev if e.get("kind") == "end"]
if end: print("END:", end[-1].get("status"), end[-1].get("stop_reason"), str(end[-1].get("error"))[:200])
site = R / "work" / "site"
if site.is_dir():
    files = sorted(p for p in site.rglob("*") if p.is_file())
    print(f"site/: {len(files)} files, {sum(p.stat().st_size for p in files):,} bytes")
    for p in files[:40]: print("   ", p.relative_to(site), p.stat().st_size)
else:
    print("site/: not created yet")
other = sorted(p.name for p in (R / "work").iterdir() if p.name not in ("node_modules", "straitjacket", "reference", "package.json", "package-lock.json", ".agents"))
print("workspace top level:", other)
