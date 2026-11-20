# Offline performance benchmarks

`perf_offline.py` measures the deterministic, local parts of antigravity-3d: the USD kernel, the QA / UV / LOD /
collider / packaging kernels, the model-facing harness tools, the in-process pipelines, and the CLI. It makes no
network calls and no model or API calls. It never starts Blender or Godot.

It is not a live-latency benchmark. Anything that waits on a model or an image API in a real run is replaced by the
package's fakes here, so those waits are zero (or, for fake image synthesis, replaced by local CPU work, see
"Fixture time" below).

## Run

```
. .venv/bin/activate
python benchmarks/perf_offline.py --out benchmarks/results/offline.json            # full run
python benchmarks/perf_offline.py --out /tmp/offline_quick.json --quick            # smoke run
python benchmarks/perf_offline.py --out /tmp/x.json --only kernel.edit harness.qa  # subset (exact name or dotted prefix)
python benchmarks/perf_offline.py --list
```

Expected wall time on the 4-vCPU reference box with nothing else running: the quick run is about 2 minutes, the full
run about 12 to 15 minutes. Timings taken while another job uses the CPU are not comparable; the JSON records the load
average at start and end so a contaminated run can be recognised.

The JSON is rewritten after every benchmark, so a native crash (CoACD has crashed on odd input before) keeps the
results gathered so far. A benchmark that raises is recorded as `{"status": "error", "error": "<one line>"}` and the
run continues. The exit status is 1 if any benchmark failed. Set `PERF_TRACE=1` to print tracebacks.

All temporary output lives in one `tempfile.TemporaryDirectory()` that is removed on exit. `AGY3D_STATE_DIR` is pointed
into it, because otherwise the QA state and the fix log are written to `./.agy3d` in the current directory. After a run,
`pgrep -f "daemon.py|mcp_server|worker_daemon"` should print nothing.

## Output

```
{"machine": {cpu_model, logical_cpus, ram_gb, os, python, git_commit, git_dirty_tracked_files, versions{...},
             thread_env, timestamp_utc, quick, loadavg_at_start, loadavg_at_end, offline{...}},
 "quick": bool, "total_wall_s": ...,
 "benchmarks": {"<name>": {"status": "ok"|"error", "includes": "...", "excludes": "...", "bench_wall_s": ...,
                           "cases": [{"params": {...}, "n", "median_ms", "p95_ms", "min_ms", "max_ms", "mean_ms",
                                      "extra": {...}}]}}}
```

All `*_ms` values are milliseconds per call of the timed region. `extra` carries per-case detail: sub-timings
(`parts_median_ms`), result facts (hull counts, gate results, issue codes), and `process_cpu_over_wall`.

## Methodology

* Timer: `time.perf_counter_ns()` around the timed call only.
* Warm-up: 2 untimed iterations, then one `gc.collect()`; the garbage collector stays enabled while timing.
* Samples: at least 20 when the fastest warm-up iteration is under 100 ms, at least 5 (3 in `--quick`) otherwise. The
  loop continues until that minimum is reached and a per-case cap has elapsed (3 s full, 0.4 s quick; the time spent in
  untimed setup and teardown counts toward the cap). One deliberate exception: when the fastest warm-up iteration is 5 s
  or more (only the 50,000-triangle UV unwrap and export-pipeline cases), 3 samples are taken instead of 5, to keep the
  full run bounded. `n` is always recorded.
* Percentiles: `p95_ms` is `null` when `n < 20`, otherwise numpy's linear-interpolation 95th percentile. No slow sample
  is ever dropped; `max_ms` is the real maximum.
* Fresh state: operations that change state are timed with apply-and-undo per iteration. Setup builds the state (untimed),
  the call is timed, teardown restores it (untimed) and asserts that the result was the intended one (for example that an
  edit was accepted, or that a "rejected" edit really rolled back). Each benchmark's `includes` string says what is inside
  the timed region.
* Threads: `OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS` and `MKL_NUM_THREADS` are set to 1 before numpy is imported.
  `PXR_WORK_THREAD_LIMIT=1` is set as well (an addition to the brief) because OpenUSD runs its own worker pool, which
  otherwise shows up as CPU time above wall time in the validation benchmarks. The values are recorded in
  `machine.thread_env`. `extra.process_cpu_over_wall` (process CPU seconds over wall seconds across the timed loop,
  including setup) shows whether anything still ran threads: CoACD is the thing to watch, because the package gives it no
  thread argument. The throughput benchmarks use processes, each single-threaded by the same settings.
* Offline: `GEMINI_API_KEY`, `GOOGLE_API_KEY`, Vertex variables and `AGY3D_POOL_SOCKET` are removed from this process and
  from every subprocess it starts, and `AGY3D_OFFLINE=1` is set. The decisions are `FakeDecisionsClient`, images are
  `FakeImageBackend`, authoring is `TemplateAuthor`, the CLI is run with `--offline`.
* Scenes for the edit loop (`make_scene`): N nodes (5, 20, 50, 200; 5 and 50 in `--quick`) cycling box, cylinder (16
  segments) and sphere (16 segments, 8 rings) over 3 materials, on a 2 m grid, each resting on z = 0, none touching. Tier-1
  passes. The session budget for these scenes is set to 1,000,000 triangles so the size sweep is not cut off by the 15,000
  triangle default of `EngineBudget`; the triangle count of each scene is in `params.tris`. The base scene is committed
  (`commit_all`) before kernel-level edit benchmarks, so an edit is one transaction on a clean base.

## Benchmarks

| Name | Timed region | Not in the timed region |
| --- | --- | --- |
| `kernel.edit.patch` | `StageManager.apply_patch` of a one-node `SetTransform` | stage build, JSON, Tier-1, rollback |
| `kernel.edit.patch_qa` | the patch, `extract_meshes`, `tier1_qa`; parts reported separately | JSON, harness state, rollback |
| `harness.edit.apply_scene_ir` | the real tool: JSON parse, transaction, Tier-1 in a thread, QA-state write to disk, lock | session open, undo |
| `harness.edit.rejected_rollback` | the tool given an edit that fails Tier-1 (open quad via `mesh_ref`); transaction, Tier-1, automatic rollback | session open |
| `kernel.rollback.raw` | `StageManager.rollback` of a pending edit; `discard` true (rejected-edit path) and false (undo path); `include_next_read` adds the first `extract_meshes` afterwards | the edit |
| `harness.edit.undo` | `undo_last_edit`: rollback plus Tier-1 re-run plus QA write | the edit |
| `harness.edit.to_pixels` | accepted edit then `render_preview` (4 views, 256 px each, wireframe on, PNG save, fake upload) | undo, visual QA |
| `harness.qa.visual_overhead` | `run_visual_qa` against the fake client with a current preview: tool-side overhead only; no model latency exists here | preview render, any model call |
| `kernel.raster.contact_sheet` | 4-view contact sheet at 256 and 512 px, about 1k / 5k / 20k triangles (12 spheres) | USD mesh extraction, PNG encode |
| `kernel.som.marked_sheet` | set-of-mark sheet at 256 px, same scenes | extraction, any judge call |
| `kernel.uv.unwrap` | `unwrap_uvs` (xatlas, 1024 px/m, 2048 atlas) on UV spheres of about 500 / 2,000 / 10,000 / 50,000 triangles and one concave U prism | USD authoring |
| `kernel.lod.generate` | `generate_lods`, default ratios; `with_uvs` true is the pipeline's call, false the weld-first path | unwrap |
| `kernel.collider.convex` | `generate_colliders` on the convex spheres (hull fast path, no CoACD) | hull authoring |
| `kernel.collider.concave` | `generate_colliders` on the U prism (CoACD) | hull authoring |
| `kernel.export.pipeline` | `run_export_pipeline` (UV + LOD + colliders): the single meshes, and the TemplateAuthor crate and barrel; per-stage split in `parts_median_ms` | mesh extraction, packaging, validation |
| `kernel.qa.tier1` | `tier1_qa` on the crate and barrel | extraction |
| `kernel.validate.usd` | `validate_usd(stage)`, `check_engine_compliance`, and `validate_usd(path)` (includes the file open), on the written package USD | package write |
| `kernel.package.write` | `write_engine_package` on a fresh stage, including its built-in validation | stage build, bundle, cleanup |
| `kernel.package.write_large_textures` | one sample, no warm-up: the same call with 1,024 px maps (384 px in `--quick`) | everything untimed above |
| `kernel.package.validate_cold` | `validate_package` on a written package | the write |
| `kernel.settle` | `settle`: 10 props (3 stacked) dropped onto a floor slab | applying results |
| `kernel.spatial.build` | octree build for 10,000 boxes (2,000 in `--quick`) from meshes and from `PrimInfo`; plus the first `StageManager.query_region` after an edit on a 200-node stage | building the inputs |
| `kernel.spatial.query_region` | `query_region` plus `to_text(1200)` for small, medium and whole-scene regions; plus the cached `StageManager.query_region` | index build |
| `kernel.cache.hit` / `kernel.cache.miss` | `cached_export_pipeline` against a populated or an empty local blob store; the miss/hit ratio is in `extra` | the store creation |
| `ipc.blender_pool.fake` | **stand-in daemon, IPC overhead only**: ping round trip, and a 1024x1024x4 float32 fake render read back through shared memory | everything real Blender does |
| `mcp.roundtrip.apply_scene_ir` | `apply_scene_ir` as an MCP `tools/call` to the real stdio server in offline mode; `extra.mcp_overhead_ms` is the median minus the same tool called directly on an equivalent session | server start-up, handshake, undo |
| `pipeline.prop_to_engine.offline` | `PropToEngineDAG.run` in process, same wiring as `agy3d prop --offline`, for 5 prompts, governed and single-candidate, cold and warm cache | interpreter start |
| `pipeline.prop.replayed_images` | headline fixture-free pipeline wall time (fixture images replayed), with CPU time | image synthesis |
| `pipeline.prop_to_engine.node_classification` | per-node fixture share, derived from the cold runs above | |
| `cli.prop.offline` | `agy3d prop ... --offline` as a subprocess, wall time (3 runs) | |
| `cli.models` | `agy3d models` as a subprocess (5 runs) | |
| `import.usd_stage` | import time of `antigravity3d.kernel.usd_stage` in a fresh interpreter, measured inside the child | interpreter start (reported as `extra.bare_interpreter_wall_ms`) |
| `throughput.prop_offline` | K = 12 distinct offline prop runs on P = 1, 2, 4 spawned worker processes: wall time, assets per hour, scaling efficiency | worker start-up |
| `dag.batch_sanitize` | `BatchSanitizeDAG` on 24 generated meshes with jobs = 1, 2, 4 | mesh generation |

## Package textures (`kernel.package.*`)

`kernel.package.write` is run twice per prop. `textured: false` gives the materials no texture files. `textured: true`
gives every material three 256 px maps (albedo from `FakeImageBackend`, ORM and normal from `derive_pbr`; 256 px is what
the offline pipeline itself generates), so the texture copy, the DirectX normal flip (unreal_5) and the PNG embedding in
every GLB level are exercised. The untextured number is a lower bound on a real package. `write_large_textures` shows how
the time grows with texture size.

## Fixture time (pipeline benchmarks)

The fake image backend draws its textures and orthographic sheets on the local CPU, which a real run would not do. The
fake decisions client validates every request against the real wire format, which costs CPU a real run would spend
waiting. For each DAG run the harness records the wall-clock spans spent inside those two fakes and reports:

* `fixture_union_ms`: time during which at least one fixture call was in progress (image and judge reported separately).
* `wall_outside_fixture_spans_ms` = wall time minus `fixture_union_ms`. This is NOT local compute. Fixture image calls run
  in a worker thread beside local work (geometry runs beside speculative PBR), and local work inside a fixture span is
  subtracted too, so the figure is only a lower bound on local work.
* `pipeline.prop.replayed_images` is the headline fixture-free figure: a second cold-cache run (fresh directory) in which
  the image backend returns the bytes generated by the first run, so no procedural synthesis runs. It still includes the
  fake judge (wire validation, canned answers) and everything local: USD, Tier-1 QA, raster and set-of-mark sheets,
  UV/LOD/collider, packaging and validation, inspector, cache hashing and I/O. It is run for every repeat.
* `cpu_ms` (process CPU time, all threads, from `time.process_time()`) is recorded for every pipeline run. When stages
  overlap, CPU-seconds per asset is the quantity a throughput estimate should rest on, not wall time.
* `pipeline.prop_to_engine.node_classification`: for each DAG node, the median share of its own duration during which a
  fixture call was in progress. A node is called fixture-dominated when that share is 0.5 or more, otherwise local
  deterministic work. Node durations overlap (the speculative `pbr` node runs beside `geometry`), so they do not sum to
  the wall time.

`cache = warm` is a second run on the same output directory and cache, so the routing decision, generated images, derived
PBR maps and the export bundle are reused.

## Reading the numbers against the design-doc claims

| Claim | Where to look | Note |
| --- | --- | --- |
| per-edit latency < 90 ms | `harness.edit.apply_scene_ir`, `kernel.edit.patch_qa`, `harness.edit.to_pixels` | `to_pixels` adds the 4-view preview an agent does each turn; none of these include model latency |
| rollback < 1 ms | `kernel.rollback.raw` | the `include_next_read` cases show the cost that moves to the next read |
| UV + LOD + collider < 250 ms outside Blender | `kernel.export.pipeline`, crate and barrel rows | `parts_median_ms` shows which stage dominates |
| 120+ assets/hour on 8 workers | `throughput.prop_offline` | 4 vCPUs, fixture images and judges: only the deterministic local portion; not comparable with a live node |

## Caveats

* Run it on a quiet machine. The box has 4 vCPUs; `throughput.prop_offline` at P = 4 and the MCP server (a second
  process) compete with whatever else is running.
* `dag.batch_sanitize` parallelises with `asyncio.to_thread`, so `jobs` > 1 means threads in one process, not processes.
  Stages that hold the GIL do not scale; the result shows the package's behaviour, it is not a harness setting.
* Some inputs exceed the default `unreal_5` limits and are reported as such, not adjusted: meshes above 15,000 triangles
  in `dag.batch_sanitize` come back `needs_attention`, and a prompt that fails a gate in the offline pipeline is reported
  with its gate result (`preview_gates_ok`, `failed_nodes`, `skipped_nodes` in `extra`). `gates_ok_verified_publication`
  is expected to be false offline: publication requires real Blender pixels and engine import.
* `harness.*` benchmarks drive the tools through one `asyncio.Runner` per benchmark, which adds a few tens of
  microseconds per call.
* The Blender pool benchmark uses the daemon's fake mode: it times the socket and shared-memory path and the daemon's
  numpy gradient, nothing else.
* Every case's `extra.process_cpu_over_wall` should be about 1.0 for single-threaded kernels. A value well above 1 means a
  library ran threads that the environment variables did not cap.
