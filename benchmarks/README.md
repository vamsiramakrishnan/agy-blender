# Performance benchmarks

Five tools and the report written from them:

- `perf_offline.py` measures the deterministic, local parts of antigravity-3d. Most of this page is about it.
- `perf_live.py` measures the model-facing components against the real API, within hard call and token limits. See
  "Live probes".
- `compare.py` prints a before/after table from two offline result files. See "Comparing two runs".
- `quality_facts.py` records what the export pipeline produces, not how long it takes: UV distortion, achieved texel
  density and the LOD chain of the offline props and of computed spheres. See "Quality facts".
- `perf_engines.py` times a real Blender and a real Godot, and Blender against the kernels on the same meshes. It runs
  in CI, where those binaries are. See "Engine benchmark (CI)".
- [`RESULTS.md`](RESULTS.md) is the written report; `results/` holds the data it was written from. See "Result files".
- `compare_agents.py` (with `compare_sandbox.py`, `compare_eval.py` and `compare_vanilla_mcp.py`) runs the same briefs
  through a stock Antigravity agent (with a plain Blender MCP server, a shell Blender, or this project's plugin) and
  through this project's command line, under one token and image ledger, and scores what each leaves the same way.
  It spends real quota, needs root and Linux namespaces, and never runs in CI. See "Agent comparison".
- [`UPSTREAM_SKILLS.md`](UPSTREAM_SKILLS.md) is the third round's report: two upstream Blender skill packs adapted, the
  first live runs of the pipeline, and the comparison with a stock agent.
- [`AFTER_THE_COMPARISON.md`](AFTER_THE_COMPARISON.md) is the fourth round's report: why the stock agent's crate was
  better and why the command did not finish, cause by cause, and what was changed. Its figures are in
  `results/round4/checks.json`.
- [`ROUND_FIVE.md`](ROUND_FIVE.md) is the fifth round's report: this project's command and its plugin in a session
  against two stock agents on three briefs, row by row, and what was changed where it was worse. Its records,
  scorecards and blind verdicts are in `results/round5/`.
- [`ROUND_SIX.md`](ROUND_SIX.md) is the sixth round's report: an asset written as one script (the plugin's opt-in
  script mode) against the plugin's default session, under a stop rule set before the runs, which it did not meet.
  The full table, the blind verdicts and the script an agent wrote for the chair are in `results/round6/`.
- [`ROUND_SEVEN.md`](ROUND_SEVEN.md) is the seventh round's report: three.js as a package and install target, generated
  surfaces that know the part they are on, and the README rewritten for developers, each against a measure written
  before the work. Two of the three measures were not met, and the report says where. The measures as written, every
  blind verdict of both measurements and the pictures are in `results/round7/`.

## Offline benchmarks

`perf_offline.py` measures the USD kernel, the QA / UV / LOD / collider / packaging kernels, the model-facing harness
tools, the in-process pipelines, and the CLI. It makes no network calls and no model or API calls. It never starts
Blender or Godot. The pipeline benchmarks do start headless Chromium, for the three.js verification step, when
Playwright is installed (`pip install -e '.[web]'`); without it that step is skipped and recorded as skipped, the
pipeline and CLI figures come out lower than the committed ones by the time of that step (about 0.7 s per asset once a
browser is running, more for the first asset of a process), and `web.verify` is recorded as skipped.

It is not a live-latency benchmark. Anything that waits on a model or an image API in a real run is replaced by the
package's fakes here, so those waits are zero (or, for fake image synthesis, replaced by local CPU work, see
"Fixture time" below).

### Run

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

The JSON is rewritten after every benchmark, so a native crash keeps the results gathered so far, and `faulthandler`
is enabled, so the crash leaves a Python traceback naming the benchmark it happened in. (The pre-fix tree ended one
full run with a segmentation fault as it entered `kernel.cache.hit`; CoACD has crashed on odd input before.) Run the
benchmarks that are missing with `--only` into a second file; `compare.py --before-rest` reads both. A benchmark that
raises is recorded as `{"status": "error", "error": "<one line>"}` and the run continues; one that cannot run on the
machine (`web.verify` without Playwright or a browser) is recorded as `{"status": "skipped", "reason": "..."}` and
is not a failure. The exit status is 1 if any
benchmark failed. Set `PERF_TRACE=1` to print tracebacks.

All temporary output lives in one `tempfile.TemporaryDirectory()` that is removed on exit. `AGY3D_STATE_DIR` is pointed
into it, because otherwise the QA state and the fix log are written to `./.agy3d` in the current directory. After a run,
`pgrep -f "daemon.py|mcp_server|worker_daemon"` should print nothing.

### Output

```
{"machine": {cpu_model, logical_cpus, ram_gb, os, python, git_commit, git_dirty_tracked_files, versions{...},
             thread_env, timestamp_utc, quick, loadavg_at_start, loadavg_at_end, offline{...}},
 "quick": bool, "total_wall_s": ...,
 "benchmarks": {"<name>": {"status": "ok"|"error"|"skipped", "includes": "...", "excludes": "...", "bench_wall_s": ...,
                           "cases": [{"params": {...}, "n", "median_ms", "p95_ms", "min_ms", "max_ms", "mean_ms",
                                      "extra": {...}}]}}}
```

All `*_ms` values are milliseconds per call of the timed region. `extra` carries per-case detail: sub-timings
(`parts_median_ms`), result facts (hull counts, gate results, issue codes), and `process_cpu_over_wall`.

### Methodology

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
  including setup) shows whether anything still ran threads. CoACD, which the package gives no thread argument, measured
  single-threaded. A decomposition that runs in a child process (input that is not one clean closed body) spends its
  CPU there, so the ratio falls below 1 for those cases. The throughput benchmarks use processes, each single-threaded
  by the same settings.
* Offline: `GEMINI_API_KEY`, `GOOGLE_API_KEY`, Vertex variables and `AGY3D_POOL_SOCKET` are removed from this process and
  from every subprocess it starts, and `AGY3D_OFFLINE=1` is set. The decisions are `FakeDecisionsClient`, images are
  `FakeImageBackend`, authoring is `TemplateAuthor`, the CLI is run with `--offline`.
* Scenes for the edit loop (`make_scene`): N nodes (5, 20, 50, 200; 5 and 50 in `--quick`) cycling box, cylinder (16
  segments) and sphere (16 segments, 8 rings) over 3 materials, on a 2 m grid, each resting on z = 0, none touching. Tier-1
  passes. The session budget for these scenes is set to 1,000,000 triangles so the size sweep is not cut off by the 15,000
  triangle default of `EngineBudget`; the triangle count of each scene is in `params.tris`. The base scene is committed
  (`commit_all`) before kernel-level edit benchmarks, so an edit is one transaction on a clean base.

### Benchmarks

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
| `kernel.uv.unwrap` | `unwrap_uvs` at 1024 px/m into a 2048 atlas on UV spheres of about 500 / 2,000 / 10,000 / 50,000 triangles and one concave U prism. These are computed meshes: xatlas computes their charts and the rectangle packer places them; primitives, which get exact charts, are timed in the crate and barrel rows of `kernel.export.pipeline` | USD authoring |
| `kernel.lod.generate` | `generate_lods`: levels bounded by a measured deviation of one pixel on a 1080-pixel frame (ratio-driven levels before the fixes); `with_uvs` true is the pipeline's call, false the weld-first path. `extra.lod_tris` lists the levels | unwrap |
| `kernel.collider.convex` | `generate_colliders` on the convex spheres (hull fast path, no CoACD) | hull authoring |
| `kernel.lod.assembly` | `generate_asset_lods` on a 2 m table top with 30 small parts on it, with UVs: every part's levels and the search for parts that may be absent at far levels. One sample, no warm-up (the tree before the second round of fixes took about a minute for this call) | unwrap |
| `kernel.collider.concave` | `generate_colliders` on the U prism and on one mesh that holds three closed bodies (two U prisms and a cube); CoACD does the decomposition. `extra.tightness` is hull volume over mesh volume | hull authoring |
| `kernel.collider.imperfect` | `generate_colliders` on input that is not one closed solid: two cubes sharing an edge, a sphere with six faces missing, an open cup. One sample each, no warm-up (the tree before the second round of fixes took 9 s, about 2 minutes and 45 s). `extra.sources` and `extra.reasons` say how each hull was made, `extra.fill` how closely the hulls follow the shape | hull authoring |
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
| `pipeline.prop_to_engine.offline` | `PropToEngineDAG.run` in process, same wiring as `agy3d prop --offline`, for 5 prompts, governed and single-candidate, cold and warm cache. Includes the three.js verification of the written package (headless Chromium) when Playwright is installed | interpreter start |
| `pipeline.prop.replayed_images` | headline fixture-free pipeline wall time (fixture images replayed), with CPU time | image synthesis |
| `pipeline.prop_to_engine.node_classification` | per-node fixture share, derived from the cold runs above | |
| `cli.prop.offline` | `agy3d prop ... --offline` as a subprocess, wall time (3 runs) | |
| `cli.models` | `agy3d models` as a subprocess (5 runs) | |
| `import.usd_stage` | import time of `antigravity3d.kernel.usd_stage` in a fresh interpreter, measured inside the child | interpreter start (reported as `extra.bare_interpreter_wall_ms`) |
| `throughput.prop_offline` | K = 12 distinct offline prop runs on P = 1, 2, 4 spawned worker processes: wall time, assets per hour, scaling efficiency | worker start-up |
| `dag.batch_sanitize` | `BatchSanitizeDAG` on 24 generated meshes with jobs = 1, 2, 4 | mesh generation |
| `web.verify` | `WebThreeValidator.validate` on a written crate package, four times in a row in one process, without textures and with six 1,024 px maps. Samples are verifications 2 to 4; `extra.first_ms` is the first. Skipped where no browser can run | writing the package |

### Package textures (`kernel.package.*`)

`kernel.package.write` is run twice per prop. `textured: false` gives the materials no texture files. `textured: true`
gives every material three 256 px maps (albedo from `FakeImageBackend`, ORM and normal from `derive_pbr`; 256 px is what
the offline pipeline itself generates), so the texture copy, the DirectX normal flip (unreal_5) and the embedding of the
texture files in every GLB level are exercised. The untextured number is a lower bound on a real package. `write_large_textures` shows how
the time grows with texture size.

### Fixture time (pipeline benchmarks)

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

### Reading the numbers against the design-doc claims

| Claim | Where to look | Note |
| --- | --- | --- |
| per-edit latency < 90 ms | `harness.edit.apply_scene_ir`, `kernel.edit.patch_qa`, `harness.edit.to_pixels` | `to_pixels` adds the 4-view preview an agent does each turn; none of these include model latency |
| rollback < 1 ms | `kernel.rollback.raw` | the `include_next_read` cases show the cost that moves to the next read |
| UV + LOD + collider < 250 ms outside Blender | `kernel.export.pipeline`, crate and barrel rows | `parts_median_ms` shows which stage dominates |
| 120+ assets/hour on 8 workers | `throughput.prop_offline` | 4 vCPUs, fixture images and judges: only the deterministic local portion; not comparable with a live node |

### Caveats

* Run it on a quiet machine. The box has 4 vCPUs; `throughput.prop_offline` at P = 4 and the MCP server (a second
  process) compete with whatever else is running.
* `dag.batch_sanitize` runs each file in a worker process: `jobs` is the number of workers, and one worker is used even at `jobs` = 1 so that a crash in a native library fails one file, not the batch. Each sample's wall time includes about a second of worker start-up.
  The harness pins native thread pools to one thread per process, so scaling with `jobs` is the workers' own.
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

### Comparison run with default threading

The main run pins the numeric libraries and OpenUSD to one thread so results are comparable between machines. To see
what the same operations cost with each library's own defaults, set `AGY3D_BENCH_THREADS=default`:

```bash
AGY3D_BENCH_THREADS=default python benchmarks/perf_offline.py \
  --only kernel.edit harness.edit kernel.rollback kernel.raster kernel.uv kernel.export \
  --out benchmarks/results/offline_default_threads.json
```

`machine.thread_pinning` in the JSON records which mode produced a file.

## Comparing two runs

```bash
python benchmarks/compare.py benchmarks/results/before/offline.json benchmarks/results/offline.json \
  --repeat benchmarks/results/before/offline_repeat.json --changed
```

`compare.py` matches benchmarks by name and cases by their parameters and prints both medians and the change. It
measures nothing. Two things decide whether its table can be trusted:

* **Measure both sides in one session.** The machine drifted within a day: the pre-fix code, run again three to four
  hours after the first report's run, was 4 to 6% slower at the median, more than 10% slower on about a quarter of
  the cases, and up to 1.4 times slower, with no change to the code. A before file from another day or another hour
  compares the machine, not the code. To measure an older commit beside the current one, check it out with
  `git worktree add <dir> <commit>` and run the harness from that directory with `PYTHONPATH=<dir>/src`, immediately
  before or after the current tree, with nothing else running. Use the current harness for both when it has
  benchmarks the older one lacks (`python <current>/benchmarks/perf_offline.py`, still from `<dir>`): the result
  file records the commit of the code that was measured, and the harness's own when it differs.
* **Know each benchmark's own noise.** A flat 10% threshold (`--noise`) is right for the kernels and wrong for the
  tool-level edit benchmarks, where two runs of identical code differed by up to 26% in the median. `--repeat` takes
  another run of the first side's code; the ratio between that run and the first file, times the `--noise` floor,
  becomes each case's noise band, shown in the last column, and only a change beyond it is reported. One repeat gives
  a lower bound on the noise: a row that barely clears its band needs a second look before anyone acts on it.

`--before-rest` reads benchmarks the first file lacks from further files, for a run that was split. `--changed` omits
rows within noise, `--only` filters by name prefix.
Cases are matched by their parameters, leaving out a package's size in bytes, which older result files recorded
among them although it is something the code produced.

## Quality facts

```bash
python benchmarks/quality_facts.py > benchmarks/results/quality.jsonl
```

A faster kernel that produces worse UVs or LODs is not an improvement, and timings cannot show the difference. This
script runs the export pipeline on the five offline template props and on computed UV spheres and prints one JSON line
per subject: area and angle distortion of the worst part, the lowest achieved texel density and whether the requested
density was reached, where the charts came from, and the LOD chain with the on-screen size each level is used at and
its deviation from LOD0. It runs against whichever tree is first on `PYTHONPATH`, so the same script measures an older
commit from a worktree; fields that tree does not record come out null, and its deviation figure is the simplifier's
own estimate, where the final code measures the distance. No network, no model calls, a few seconds.

## Live probes

`perf_live.py` calls the real model APIs, so it spends quota. It measures the components one at a time, not the governed
pipeline: one-shot authoring on each of the four tiers with the request production sends, a stage-agent session on two
tasks, the visual-QA judge, and one texture per size tier.

```bash
python benchmarks/perf_live.py --dry-run                                   # print the plan; no client, no network
python benchmarks/perf_live.py --out benchmarks/results/live.json          # needs GEMINI_API_KEY
python benchmarks/perf_live.py --self-test                                 # the spend limiter against a stub
```

* The limits are enforced in code, before every call: `--max-calls` (default 24) and `--max-tokens` (default 400,000,
  input plus output plus thinking). When one would be exceeded the run stops, writes what it has and records why.
* A stage run counts as one call against `--max-calls`; each of its model calls goes through a local relay that checks
  the token limit first and books the reported usage. A call whose usage never arrives keeps its reservation booked.
* Retries are off everywhere (the SDK's, the package's own loops, the Antigravity harness's), so a provider error is a
  failed sample, not a hidden second call.
* No price is read or set. The files report tokens, never dollars.
* Sample counts are 1 to 3 per entry. Read the results as observations, not distributions.

## Engine benchmark (CI)

`perf_engines.py` times the paths that need a real Blender and a real Godot. The benchmark machine has neither, so
it runs in the `stock-engines` job of the qualification workflow, on every pull request, after the engine tests,
against the pinned Blender 5.3.0 and Godot 4.7.2. It cannot fail the job: a measurement that is unavailable is
recorded inside the JSON, and the step raises one warning when any measurement failed.

```
xvfb-run -a python benchmarks/perf_engines.py --out /tmp/engine-bench.json --markdown /tmp/engine-bench.md
python benchmarks/perf_engines.py --fake --quick --out /tmp/fake.json      # plumbing only, anywhere
```

It needs `$BLENDER_BIN` and `$GODOT_BIN`; without them it says what is missing and exits 0. `--fake` runs every code
path against the worker pool's fake daemon, which only lints the Blender scripts: those are not Blender numbers and
the JSON says so (`mode: "fake"`).

What it measures:

- **The Blender worker pool**: cold start to the first answered ping (a fresh process each sample), a warm ping,
  `import_usd` of three written packages, and `render_usd` of the crate with Workbench, EEVEE and Cycles at the
  size the pipeline asks for (512 px, 16 samples). The runner has no GPU, so EEVEE's figure is software GL and is
  not what a workstation would see.
- **Blender against the kernels, same meshes**: UV unwrap, decimation to the kernel's own triangle counts, convex
  hull, and a four-view sheet, on the crate, the barrel and two spheres. The Blender side reports two times: the
  operator alone, timed inside Blender, and the whole `run_script` call per repetition, which is what a caller
  pays. The ratio in the table uses the whole call.
- **Godot**: the package check the pipeline runs (`GodotValidator.validate`), a capture, and the same launch with a
  script that quits at once, so the difference is the import itself.
- **Upstream checks survey** (`upstream_survey.py`, JSON key `upstream_checks`, schema 2): what the reviewed
  EL4CTEO/blender-skills scripts vendored in the worker (`qa_check`, `mesh_stats`, `texel_density`, `kit_check`; their
  commit and hashes are in the JSON) say about what the pipeline ships, beside what the project's own checks say
  (`tier1_qa`, manifest triangles, draw calls, achieved texel density, UV distortion and overlap). Subjects: the
  crate, barrel and sphere packaged for each target engine (each package USD, and the GLB once per distinct file),
  five planted defects with known answers (coplanar overlapping tops, faces pressed back to back, a floating part,
  inverted winding, no UVs), and an offline kit (unreal_5 and godot_4), each piece checked with upstream's defaults
  and with the origin the piece templates imply, since the project states no grid or pivot convention. Every
  topology check runs twice: on the scene as imported, and after the worker merges coincident vertices part by part
  (`inspect_usd(weld=True)`; two parts in contact are not merged with each other), because the package writes one
  vertex per (position, UV) and the GLB splits at seams too. Measurement only: nothing is gated on it. Each call records kwargs, triangles, duration and its outcome
  (`ok`, `error`, `checks_skipped` when `qa_check` skipped its z-fighting and floating checks above `max_tris`, or
  `skipped` for the time budget, at 80 % of it, leaving time for the Godot section that follows). It is skipped, with the reason, when the worker has no primitives.
  In CI the step clears `$AGY3D_BLENDER_SKILLS` so the worker uses the vendored scripts, as production does.

The two sides do not run the same algorithms, and the table says so under itself. Read these before the ratios:

- Unwrap: the kernel computes charts with a bound on distortion and packs them at a requested texel density;
  Blender's Smart UV Project projects and packs with neither. Both sides report islands, UV area and the area
  distortion the pipeline gates on (budget 0.05), computed by the same formula. A second kernel row gives the time
  of the path the pipeline takes for the same mesh, where primitives get exact charts.
- LOD: the kernel simplifies, measures the deviation of every attempt and optimises the result; Blender's edit-mode
  Decimate collapses to the same triangle counts and measures nothing.
- Hull: roughly the same work; the kernel also checks topology and computes volume.
- Sheet: different renderers. The Blender cameras copy the kernel's framing, and both sides report the fraction of
  pixels covered so that a mismatch would show.

The job uploads `engine-bench.json` and `engine-bench.md` as the artifact `engine-bench` and writes the Markdown
into the job summary. `results/engines_ci.json` is the artifact of one run; its `ci` block names the commit and
the runner. Hosted runners differ from run to run (two CPU models were seen in three runs), so compare the two
sides within one file, not one file with another.

## Result files

The second round's files are at the top of `results/`; the first round's are kept as published in `results/round1/`.

| File | What it is |
| --- | --- |
| `results/offline.json` | The code after the second round (`machine.git_commit`), full offline run |
| `results/before/offline.json` | The tree the second round started from (`5a440fa`), measured with the same harness in the same session, immediately before `results/offline.json` |
| `results/before/offline_repeat.json` | The same tree again, immediately after `results/offline.json`: the same-code repeat behind the noise bands |
| `results/compare.md` | `compare.py` on those three files: every case, with its noise band |
| `results/edit_recheck.json` | The edit-loop benchmarks alone, three alternating runs of each tree: the paired run flagged three of those rows as slower, and this shows they are not |
| `results/quality.jsonl`, `results/before/quality.jsonl` | `quality_facts.py` on the two trees |
| `results/engines_ci.json` | `perf_engines.py` from the `stock-engines` job on the final code (GitHub runner, Blender 5.3.0, Godot 4.7.2) |
| `results/live_stage.json` | Stage runs on the builder tier, re-measured in the second round |
| `results/live_schema_before.json`, `results/live_schema_after.json` | The paired authoring check of the old and the new response schema on the smallest tier |
| `results/round1/offline.json`, `offline_default_threads.json`, `live.json`, `quality.jsonl` | The first round's final code (`f9e039a`) |
| `results/round1/before/` | The first round's "before" files: `offline_paired.json` and `offline_paired_rest.json` (the pre-fix tree `14814fd` beside the final one), `offline_repeat.json` (its same-code repeat), `quality.jsonl`, and the first report's data (`offline.json`, `offline_default_threads.json`, `live.json`) |

Do not compare timings across the two rounds' files, or with the first report's: the machine was not the same from
one session to the next.

## Agent comparison

`compare_agents.py` is not a benchmark of speed. It answers, per brief, what a stock agent with a Blender leaves behind
and what this project leaves behind, when all are held to the same limits and judged from their files, and what each
costs a game developer afterwards.

- **Arms.** V: the stock Antigravity agent with a plain Blender MCP server (`compare_vanilla_mcp.py`: Python in one
  long-lived Blender session, the scene described, an image of it from a chosen view; nothing of this project): the
  "vanilla MCP system". A: the same agent with a scriptable headless Blender on its PATH. B: A with the EL4CTEO skills.
  N: the same agent with this project's plugin attached, driving it tool by tool. C: `agy3d prop ... --preview` from
  this tree. V, A, B and N run inside Linux namespaces as an unprivileged user with no network; the task text is the
  same for all of them except the one sentence that says how Blender is reached.
- **One ledger.** Every model call of every arm, and of the judge, goes through a relay on the host that holds the
  key, reserves each call's worst case against the run's share, and refuses what does not fit. `ledger.json` is the
  record; a share is closed at what it used before another run may plan with the rest. `plan` prints every cell's
  share, the model calls its task names and the harness's own stop before anything is spent.
- **One evaluation.** Facts read from the GLB, an import in Godot, the three.js check, the upstream `qa_check`,
  `mesh_stats` and `texel_density` in Blender, Cycles renders, and a blind pairwise judge asked in both orders, on the
  full contact sheet and on a 128 px one (the read at game size).
- **The scorecard** (`compare_eval.SCORE_ROWS`, written to `scorecard_<round>.json` and the gallery): finishing,
  game-ready checks and their rate, runtime cost (draw calls, triangles per level, texture memory from the images'
  sizes, package bytes), looks, the change request, hand steps left in Godot 4, and trust. Each row carries the rule it
  applies. Rows only this project can pass (installed into a project, same bytes from the same request, provenance)
  are marked and kept out of the shared rate.
- **Change requests.** `--change BRIEF:ARM` gives a cell that delivered one follow-up ("make the two side handles 30 %
  larger" for the crate; `CHANGES`) in a new session over a copy of its files, and compares the two GLBs part by part
  (`compare_eval.package_diff`: parts resized as asked, and everything else that changed). Arm C's change is the same
  command with the change sentence appended, into a folder holding a copy of its cache (`C_CHANGE_MODE`); the command
  line has no edit path yet. `--repeat BRIEF:C` asks arm C the same request again and compares the bytes.
- **Commands.** `dry-run` rehearses everything against local stand-ins and spends nothing. `live --go` spends quota:
  run one cell at a time (`--only`, `--change`, `--continue`, `--repeat`) and read its record before the next.
  `close` closes accounts. `score --root DIR` rebuilds the scorecard and gallery from what is on disk.

Its tests (`tests/test_compare_agents.py`) cover the ledger, the relay's refusals, the sandbox, the vanilla MCP
server, the scorecard's rules and the change requests; the sandbox tests need root, `unshare` and `setpriv` and are
skipped without them, and arm V's needs a real Blender and Xvfb. What it found is in `UPSTREAM_SKILLS.md`.

### Fresh paired stock/pipeline runs

Use the opt-in `--paired` profile for a new comparison with identical content requirements, equal explicit token/image
shares, a fixed C candidate mode and immutable source/run provenance. `plan --json` is a no-cost preflight. Historical
round-two defaults and records remain intact. See [the authoring-contract follow-up](AUTHORING_CONTRACT_REVIEW.md)
for the production/kit/blockout distinction, regression evidence, proposed pilot limits and execution prerequisites.
