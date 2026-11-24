"""analyse_run.py EVENTS.jsonl [RECORD.json]: where a site session's time went, from its event log.

Prints: the minute each phase began (first build, first plugin modelling call, last install), plugin tool calls by
name with their outcomes and seconds, how often the agent looked at its site and how (the plugin's check or a script
of its own), and the tool calls the host cut off."""
import collections
import json
import sys

ev = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
t0 = ev[0]["t"]
minute = lambda e: (e["t"] - t0) / 60  # noqa: E731
PLAIN = {"view_file", "run_command", "edit_file", "create_file", "write_to_file", "replace_file_content", "list_dir", "grep_search"}
MODEL = {"open_asset_session", "apply_scene_ir", "render_preview", "run_visual_qa", "set_stage", "generate_lods_and_colliders",
         "run_usd_validator", "export_engine_package", "deliver_package", "run_build", "session_status", "list_sessions",
         "inspect_manifest", "apply_shader_diff", "forge_pbr_material", "render_lookdev", "undo_last_edit"}

calls = [e for e in ev if e["kind"] == "tool_call"]
results = [e for e in ev if e["kind"] == "tool_result"]
pending = collections.defaultdict(list)
pairs = []
for e in ev:
    if e["kind"] == "tool_call":
        pending[e["name"]].append(e)
    elif e["kind"] == "tool_result" and pending[e["name"]]:
        pairs.append((pending[e["name"]].pop(0), e))


def cmd(e):
    a = e.get("args") or {}
    return str(a.get("CommandLine") or a.get("command") or "")


def text(e):
    r = e.get("result")
    return r if isinstance(r, str) else json.dumps(r)


end = minute(ev[-1])
first = lambda pred: next((minute(e) for e in calls if pred(e)), None)  # noqa: E731
last_result = lambda pred: next((minute(r) for c, r in reversed(pairs) if pred(c)), None)  # noqa: E731
first_model = first(lambda e: e["name"] in MODEL)
first_build = first(lambda e: e["name"] == "run_command" and ("vite build" in cmd(e) or "npm run build" in cmd(e)))
last_model = last_result(lambda e: e["name"] in MODEL or e["name"] == "install_into_project")
print(f"session: {end:.1f} min, {len(calls)} tool calls, {len(results)} answered")
fmt = lambda v: "never" if v is None else f"{v:.1f} min"  # noqa: E731
print(f"first site build: {fmt(first_build)} | first modelling call: {fmt(first_model)} | last modelling or install answer: {fmt(last_model)}")
if first_model is not None and last_model is not None:
    inside = [e for e in calls if first_model <= minute(e) <= last_model]
    site_edits = sum(e["name"] in ("edit_file", "create_file", "write_to_file", "replace_file_content") for e in inside)
    print(f"props window: {last_model - first_model:.1f} min ({(last_model - first_model) / end:.0%} of the session), with "
          f"{site_edits} file edits made inside it; after it: {end - last_model:.1f} min")

by = collections.defaultdict(list)
for c, r in pairs:
    if c["name"] not in PLAIN:
        by[c["name"]].append((r["t"] - c["t"], text(r)))
print("plugin tools (calls, seconds total, longest; answers by first word):")
for name, rows in sorted(by.items(), key=lambda kv: -sum(x[0] for x in kv[1])):
    heads = collections.Counter((t.split(":")[0].split(" ")[0] if t else "?")[:14] for _, t in rows)
    print(f"  {name:28s} {len(rows):3d}  {sum(x[0] for x in rows):6.0f} s  {max(x[0] for x in rows):5.0f} s  {dict(heads)}")
unanswered = {n: len(v) for n, v in pending.items() if v and n not in PLAIN}
print("plugin calls with no answer in the log:", unanswered or "none")
cut = sum("timed out after" in text(r) or "timed out after" in str(r.get("error") or "") for r in results)
print(f"answers that say 'timed out after': {cut}")
own = [e for e in calls if e["name"] == "run_command" and any(k in cmd(e) for k in ("playwright", "puppeteer", "--screenshot", "screenshot(", "headless"))]
looks = [minute(e) for e in calls if e["name"] == "check_site"]
print(f"looks at the site: check_site {len(looks)} at minutes {[round(x, 1) for x in looks]}; screenshot scripts of its own: {len(own)}")
views = sum(e["name"] == "view_file" and str((e.get("args") or {}).get("AbsolutePath", "")).lower().endswith((".png", ".jpg", ".jpeg", ".webp")) for e in calls)
print(f"pictures opened: {views}; builds: {sum('vite build' in cmd(e) or 'npm run build' in cmd(e) for e in calls if e['name'] == 'run_command')}; "
      f"installs: {sum(e['name'] == 'install_into_project' for e in calls)}; asks: {collections.Counter((e.get('tool'), e.get('answer')) for e in ev if e['kind'] == 'ask_answered')}")
if len(sys.argv) > 2:
    rec = json.load(open(sys.argv[2]))
    tk = rec.get("tokens") or {}
    print(f"record: status {rec.get('status')}, wall {rec.get('wall_s', 0) / 60:.1f} min, model calls {rec.get('model_calls')}, tool calls {rec.get('tool_calls')}, "
          f"tokens total {tk.get('total'):,} (cached {tk.get('cached_tokens', tk.get('cached')):,})")
