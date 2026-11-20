# Performance results, 2026-11-20

Measured on `main` at `36313b4` (harness commit `1e3e150`). Raw data: `results/offline.json`,
`results/offline_default_threads.json`, `results/live.json`. How to reproduce: [`README.md`](README.md).

## Summary

- The deterministic tools are fast. An edit with its geometry check takes 5–18 ms for scenes up to 50 nodes, and a
  rejected edit is rolled back in 0.1–0.2 ms.
- With a model in the loop that speed barely registers. In live agent runs, tools were 0.5–2% of wall time; the model
  was the rest (7–9 s per small task on `gemini-4-hydrogen`, 32 s on `gemini-4-carbon`).
- Two kernels miss the design figures. UV unwrapping takes 170–360 ms at prop sizes (the design assumed about 40 ms)
  and 16.7 s at 50,000 triangles. Convex decomposition of a 2,000-triangle concave mesh takes 446 ms (assumed 60 ms).
  The "UV + LOD + collider under 250 ms" gate holds only for the smallest prop tested.
- The live pipeline could not be run end to end on the development key. The native Decisions API returns 404, and the
  pipeline refuses to spend on the unpriced fallback judge. Live components were measured individually instead.
- The live probes found three defects in the model-facing code (see "Defects found").
- Nothing here measures asset quality or the Blender path. See "Not measured".

## Environment

| | |
|---|---|
| Machine | 4 vCPU Intel Xeon @ 2.10 GHz, 15.7 GB RAM, no GPU, Linux 6.18 |
| Python | 3.13.18 |
| Libraries | usd-core 26.11, trimesh 5.2.1, xatlas 0.0.11, meshoptimizer 0.2.30a0, coacd 1.0.14, numpy 2.5.5 |
| SDKs | google-genai 3.6.1, google-antigravity 0.1.24 |
| Load | idle (load average 0.32 at start of the offline run) |
| Threads | numeric libraries and OpenUSD pinned to 1 thread. A second run with library defaults was within 15% for every operation above 1 ms, except the edit-to-preview loop, which was 8–20% slower |

Offline numbers are medians. p95 is given only where at least 20 samples were taken.

## Against the design document's targets

| Design-document target | Measured | Verdict |
|---|---|---|
| Per-edit latency under 90 ms | Edit + geometry check: 4.8 / 9.7 / 17.9 / 64 ms for 5 / 20 / 50 / 200 nodes. Edit + check + 4-view preview: 36 / 59 / 95 / 311 ms. | Met to about 20 nodes including the preview; borderline at 50; missed at 200 |
| Rollback under 1 ms | Rejected speculative edit: 0.10–0.21 ms. Undo that keeps history: 0.27 / 0.41 / 0.66 / 1.92 ms. First read after a rollback: 0.7–13.8 ms. | Met for rejected edits at every size; undo exceeds 1 ms at 200 nodes |
| UV + LOD + collider under 250 ms | Crate (144 tris, 12 parts) 235 ms; barrel (320 tris) 288 ms; 1,920-tri sphere 369 ms; 1,964-tri concave shape 596 ms; 10,080 tris 1.4 s; 49,920 tris 16.7 s. | Met only for the crate |
| Visual QA pass about 200 ms | Native Decisions API: 404 on this key. Fallback judge (`gemini-4-neon`, uncalibrated): 15–18 s for 9 questions, 17–22 s for 21 questions. Tool-side overhead: 5 ms. | Not measurable natively; fallback is 75–110x the target |
| First-pass engine QA above 99% | No engine was run. Offline with fixture judges, 4 of 5 template props passed the preview gates. | Not measured |
| All-in cost per hero asset $0.95–1.60 | No prices are configured for this key, and the hero authoring call returned no output (see defects). | Not measured |
| 120+ assets per hour on 8 workers | Local compute alone: 1,746 assets/hour on 1 worker, 4,501 on 4 workers (fixture images and judges). | Local compute is not the constraint; live throughput was not measured |

Design-document kernel figures:

| Operation | Design figure | Measured |
|---|---|---|
| Geometry QA (Tier-1) | about 15 ms | 2.0 ms (crate), 5.7 ms (barrel) |
| UV unwrap (xatlas) | about 40 ms | 259 ms at 448 tris; 358 ms at 1,920; 1,394 ms at 10,080; 16,703 ms at 49,920 |
| LOD generation (meshoptimizer) | about 25 ms | 0.7 ms at 448 tris; 2.5 ms at 1,920; 13 ms at 10,080; 92 ms at 49,920 |
| Collision hulls (CoACD) | about 60 ms | Convex shapes skip CoACD: 1.7–40 ms up to 10,080 tris. Concave 1,964-tri shape through CoACD: 446 ms |
| Clay preview render | about 15 ms (Hydra) | Software rasteriser, 256 px: 14 ms per view at 960 tris, 27 ms at 5,280, 68 ms at 20,160 |

The design document's speed-up factors over Blender (15–80x) are not verified: no Blender binary was run.

## Edit loop (offline)

Median milliseconds by scene size. Scenes are grids of boxes, cylinders and spheres resting on the ground.

| Operation | 5 nodes (376 tris) | 20 (1,876) | 50 (4,876) | 200 (19,876) |
|---|---|---|---|---|
| Transaction only (`apply_patch`) | 0.49 | 0.84 | 1.70 | 6.35 |
| Transaction + mesh extraction + Tier-1 QA | 2.5 | 6.3 | 14.9 | 58.6 |
| `apply_scene_ir` tool, accepted edit | 4.8 | 9.7 | 17.9 | 64.0 |
| `apply_scene_ir` tool, rejected edit with auto-rollback | 4.6 | 9.0 | 18.3 | 65.8 |
| `undo_last_edit` tool | 5.9 | 10.5 | 20.4 | 60.9 |
| Edit + QA + 4-view preview sheet (256 px) | 36 | 59 | 95 | 311 |
| `run_visual_qa` tool overhead, fixture judge | 4.7 | 4.8 | 4.9 | 5.0 |

Other measurements:

- Reaching the same tool through the MCP stdio server adds 1.3–2.6 ms per call.
- The stand-in Blender worker answers a ping in 0.08 ms and returns a 16.8 MB render buffer through shared memory in
  21 ms. This is IPC cost only; no Blender ran.
- An export-pipeline cache hit takes 3.9–10.2 ms against 249–284 ms for a miss.
- Settling 10 props takes 7.2 ms. A spatial index over 10,000 boxes builds in 72 ms and answers a small region query
  in 1.4 ms.

## Packaging

| Operation | Crate | Barrel |
|---|---|---|
| Write package, no textures | 64 ms | 42 ms |
| Write package, six 256 px maps | 878 ms | 694 ms |
| Write package, six 1024 px maps (one sample) | 13.5 s | not run |
| Validate a written package from cold | 20 ms | 15 ms |

Package writing scales with texture pixels. The 1024 px package was 79.7 MB for six maps, which is consistent with the
maps being embedded in every LOD GLB and in the USDZ. That reading comes from the harness author and was not profiled.

## Offline pipeline

`PropToEngineDAG` in process, fixture images and judges, template geometry, `unreal_5`, three repeats each.

| Prompt | Best-of-N, cold | Single candidate, cold | Best-of-N, warm cache | Preview gates |
|---|---|---|---|---|
| wooden crate with iron brackets | 2.28 s | 2.07 s | 1.76 s | pass |
| rusty metal barrel | 3.07 s | 2.68 s | 2.15 s | pass |
| oak dining table | 1.38 s | 1.17 s | 1.07 s | pass |
| iron street lamp post | 1.94 s | 1.72 s | 1.68 s | fail (LOD pop) |
| wooden storage shelf | 1.49 s | 1.44 s | 1.21 s | pass |

- Best-of-N costs 3–18% more wall time than a single candidate offline and four times the image generations (12
  against 3, or 8 against 2). In a live run the extra images are the main added cost.
- A warm cache saves 13–30% overall. The refinement stage dominates (1.0–2.4 s cold) and a warm cache reduces it
  by only 4–30%.
- With image synthesis replayed instead of generated, the Best-of-N runs take 1.2–2.8 s and 1.4–3.5 CPU-seconds per
  asset.
- From a shell, `agy3d prop ... --offline` takes 4.1 s for the crate. About 1.8 s of that is interpreter start,
  imports and exit. `agy3d models` takes 0.58 s.

Throughput, 12 distinct offline props:

| Worker processes | Wall | Assets per hour | Scaling efficiency |
|---|---|---|---|
| 1 | 24.7 s | 1,746 | 100% |
| 2 | 13.5 s | 3,207 | 92% |
| 4 | 9.6 s | 4,501 | 64% |

This is local compute with fixtures. A live run waits on model and image APIs, so it is not a live throughput figure.

Batch sanitize, 24 meshes of 192–28,320 triangles (some deliberately defective):

| `--jobs` | Wall | Meshes per minute |
|---|---|---|
| 1 | 39.6 s | 36 |
| 2 | 30.3 s | 48 |
| 4 | 25.8 s | 56 |

21 of 24 passed. The other 3 exceed the 15,000-triangle budget and were reported as needing attention. Scaling is
1.5x at 4 jobs because the stage runs in threads within one process.

## Live components

20 calls, 95,578 input tokens, 16,034 output tokens and 29,916 thinking tokens. No prices are configured, so cost is
unknown. Sample counts are small (1 to 3); read these as observations, not distributions.

Authoring one Scene IR with the request the package's own author sends (prompt: a wooden crate with iron corner
brackets; 546 input tokens):

| Tier | Model | Thinking | Wall | Result |
|---|---|---|---|---|
| worker | `gemini-4-hydrogen` | low | 11.5–12.5 s | 1 of 3 valid; 2 failed schema validation |
| bulk | `gemini-4-neon` | low | 27–36 s | 0 of 3; output cut off at the token cap |
| builder | `gemini-4-carbon` | medium | 54–112 s | 0 of 3; no output |
| director | `gemini-4-argon` | high | 112 s | 0 of 1; no output |

Agent edit loop through the Antigravity SDK (build a two-box crate, then run QA; decisions and images are fixtures):

| Tier | Model | Wall | Tool calls | Tool time | Prompt tokens | Output + thinking tokens |
|---|---|---|---|---|---|---|
| worker | `gemini-4-hydrogen` | 9.0 s, 6.9 s | 2 | 0.14 s, 0.12 s | 21,739; 21,878 | 624; 683 |
| builder | `gemini-4-carbon` | 32.5 s | 3 | 0.16 s | 28,741 | 1,459 |

The `apply_scene_ir` call inside those runs took 15–22 ms.

Judge:

| Check | Model | Questions | Wall | Input tokens |
|---|---|---|---|---|
| Native Decisions probe | `gemini-decision-1` | 1 | 0.3 s, HTTP 404 | none |
| Blockout QA, low-resolution sheet | `gemini-4-neon` fallback | 9 | 15.1–18.0 s | 2,312 |
| Lookdev QA, high-resolution sheet | `gemini-4-neon` fallback | 21 | 16.7–21.8 s | 5,381 |

Fallback answers are marked uncalibrated and cannot approve an asset.

Images (`gemini-nano-banana-2.1`):

- A 1K seamless albedo request returned no image after 9.7 s (one sample).
- The repository's own live image test at 2K produced a 2048 x 2048 image inside a 26 s test run, then failed its
  metering assertion: the provider returned no per-modality token counts, so the spend ledger recorded a reservation
  estimate instead of provider usage.

## Defects found

1. **The authoring output cap starves the higher tiers.** `MeteredAuthor` caps output at 4,096 tokens (2,048 for
   repairs) and thinking tokens count toward the cap. `gemini-4-carbon` and `gemini-4-argon` spent 4,093 tokens
   thinking and returned 3. The hero spend plan authors on the director tier, so hero authoring returns no scene on
   this key. `gemini-4-neon` was cut off mid-JSON.
2. **The author can emit an invalid `collider` value.** Two of three worker-tier responses used `"box"` or `"none"`
   where the schema requires a boolean. The rules text names the field without a type and no response schema is sent.
3. **Image generation at 1K returned no image.** The 1K output cap is 2,048 tokens and the call used 762 on thinking.
   One sample, so the cause is not established.
4. **Texture-heavy package writes are slow.** 13.5 s for six 1024 px maps on a 144-triangle prop.
5. **UV unwrapping has a floor of roughly 160–250 ms** even for a 144-triangle prop, and grows to 16.7 s at 50,000
   triangles.
6. **`sanitize --jobs` scales poorly** (1.5x at 4 jobs).
7. **The lamp-post template fails the LOD pop gate offline** (silhouette overlap 0.667 between LOD2 and LOD3).

None of these were changed in this pull request.

## Not measured

- **Blender.** Release archives for Blender 5.3.0 and Godot 4.7.2 were downloaded and matched the checksums pinned in
  `.github/workflows/qualification.yml`, but neither binary was executed on the development machine. Every comparison
  with a Blender path in the design document is therefore unverified.
- **Engine imports** into Unreal, Unity, Godot or Roblox.
- **The native Decisions API** (404 on this key), and therefore the governed live pipeline, which requires it.
- **Asset quality.** `agy3d benchmark` is the instrument for that. It needs at least 50 paired runs with studio
  labels and reconciled billing on a native Decisions deployment.
- **Dollar cost.** No rates are configured.
- **Scaling beyond 4 workers.** The machine has 4 vCPUs.
