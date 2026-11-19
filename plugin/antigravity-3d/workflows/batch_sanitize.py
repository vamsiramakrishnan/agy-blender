# antigravity-3d batch-sanitize DAG, for the `run_workflow` tool.
#
#   run_workflow(ScriptPath="<plugin dir>/workflows/batch_sanitize.py",
#                Args={"files": ["/abs/a.fbx", ...], "target_engine": "unreal_5", "export": false},
#                Description="Batch-sanitize depot files")
#
# Uses only the documented script primitives (args, phase, log, agent, parallel)
# and passes google.antigravity.workflows.validate_workflow_source: no print(),
# no top-level return/yield. Per file: sanitize (game-ready-sanitizer skill) ->
# gate (usd_verifier custom subagent) -> optional export (engine-truth-verify).
# One failing file never cancels the others: each branch catches its own errors.

files = [str(f) for f in (args.get("files") or [])]
engine = str(args.get("target_engine") or "unreal_5")
do_export = bool(args.get("export", False))

SANITIZE_SCHEMA = {
    "type": "object",
    "properties": {
        "file": {"type": "string"},
        "stage": {"type": "string", "description": "absolute path of the sanitized USD stage"},
        "ok": {"type": "boolean"},
        "tris": {"type": "integer"},
        "lods": {"type": "integer"},
        "hulls": {"type": "integer"},
        "blocker": {"type": "string"},
    },
    "required": ["file", "ok", "stage", "blocker"],
}
GATE_SCHEMA = {
    "type": "object",
    "properties": {
        "tier1_ok": {"type": "boolean"},
        "tier15": {"type": "number"},
        "blocker": {"type": "string"},
    },
    "required": ["tier1_ok", "tier15", "blocker"],
}
EXPORT_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}, "package": {"type": "string"}, "mismatch": {"type": "string"}},
    "required": ["ok", "package", "mismatch"],
}


async def process(path):
    name = path.rsplit("/", 1)[-1]
    row = {"file": path, "ok": False, "stage": "", "tier1_ok": False, "tier15": 0.0, "blocker": "", "package": ""}
    try:
        san = await agent(
            f"Sanitize {name} for {engine}.\n"
            f"Use the antigravity-3d game-ready-sanitizer skill on exactly this file: {path}\n"
            "Import it into a new USD stage via apply_scene_ir, normalise to metres / Z-up, fix geometry and UVs, "
            f"run generate_lods_and_colliders for {engine}, then run_usd_validator until clean. "
            "Do not export. Return the stage path and counts.",
            schema=SANITIZE_SCHEMA,
            role=f"Sanitize {name}",
        )
        row.update({"stage": san.get("stage", ""), "blocker": san.get("blocker", "")})
        if not san.get("ok"):
            return row
        gate = await agent(
            f"Gate the USD stage {row['stage']} (target {engine}). Run Tier-1 and Tier-1.5 and return the verdict.",
            schema=GATE_SCHEMA,
            role=f"Verify {name}",
            type_name="usd_verifier",
        )
        row.update({"tier1_ok": bool(gate.get("tier1_ok")), "tier15": float(gate.get("tier15", 0.0)),
                    "blocker": gate.get("blocker", "")})
        row["ok"] = row["tier1_ok"] and row["tier15"] >= 3.4
        if row["ok"] and do_export:
            exp = await agent(
                f"Export {row['stage']} to {engine} with the antigravity-3d engine-truth-verify skill "
                "(export_engine_package, headless import, parity check). Return the package path.",
                schema=EXPORT_SCHEMA,
                role=f"Export {name}",
            )
            row["package"] = exp.get("package", "")
            if not exp.get("ok"):
                row["ok"] = False
                row["blocker"] = "export parity: " + exp.get("mismatch", "")
    except Exception as e:  # keep the other files running
        row["blocker"] = f"{type(e).__name__}: {e}"
    return row


if not files:
    log('batch_sanitize: no files given; pass Args {"files": ["/abs/path.fbx", ...]}')
else:
    phase(f"Sanitize and gate {len(files)} file(s) for {engine}")
    rows = await parallel(files, process)
    phase("Report")
    passed = 0
    for r in rows:
        passed += 1 if r["ok"] else 0
        verdict = "PASS" if r["ok"] else "FAIL"
        tail = r["package"] if r["package"] else (r["blocker"] or "-")
        log(f"{r['file']}: {verdict} tier1={'ok' if r['tier1_ok'] else 'fail'} tier15={r['tier15']:.2f} {tail}")
    log(f"batch_sanitize: {passed}/{len(rows)} passed the QA gate")
