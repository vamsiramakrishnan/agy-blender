# Explicit swept rods: implementation and local evidence

Date: 23 November 2026. Original implementation baseline: `1668bada0ca5c0facc4fe62664879f0f22481105` (PR #6).
Refreshed PR base: `0edb0ff295eabe8424c21bd0d6da24b69fcab3ec`, the latest `main` after PR #7.

## Reconciliation with current main

PR #7 added opt-in Python script authoring with `Asset.tube`, lofts and arbitrary meshes. Bent rods therefore
already exist in the script API. The default typed Scene IR still has no sweep node; this change adds that
capability to native patches, parameter edits and analytic UV compilation.

The script tube and this primitive have different contracts. `Asset.tube` uses true mitres, supports variable
radii, closed loops and optional Catmull-Rom smoothing, and persists the result through a PLY-backed `mesh_ref`.
The bounded Scene IR sweep preserves explicit circular rings, adds cap centres and analytic chart IDs, and keeps
the path and radius editable as parameters. Their cap topology and triangle counts differ. This PR preserves
the script API's existing geometry and tests rather than silently changing either contract to share a builder.

The refresh also preserves PR #7's actual-mesh bounds calculation for rotated primitives and both of its new
mandatory script-engine qualification cases. The only workflow change adds the two swept-rail cases.

[Round six](ROUND_SIX.md) already reports a script chair beating the default session chair 6/6 but still losing
to stock plain Blender 0/6, with remaining criticism of generated surfaces on rods and rails. Those are fixed
asset pairs. This PR addresses native representation and export support; it does not establish a new visual win.

## Problem established by round five

The [round-five report](ROUND_FIVE.md) records both project chairs losing every blind verdict to the plain-MCP
stock chair. The raw comparisons are [command versus stock](results/round5/looks/final_command_vs_stock/chair_V_vs_C.json)
and [plugin versus stock](results/round5/looks/final_session_vs_stock/chair_V_vs_N.json): 0/6 for each project arm.
Each file calls the stock sheet `A`; its `sheets` paths identify the actual V/C/N arms. Six judgments of one pair
are not six independent generated assets.

The stock chair has a continuous bent arm rail and fanned spindles. The default IR can rotate a cylinder or
lathe to make a fanned straight spindle, but cannot describe a round section following an arbitrary centreline.
Adding triangles or stronger production-detail wording does not supply that missing shape.

This change adds that reusable geometry operation. It does not establish that either project arm now beats
the stock chair. Existing verdicts also criticize seat thickness, proportions and repetitive wood grain.
No new model-generated chair or blind judgment was requested or run during this work.

## Contract

`sweep` describes a constant circular section along an explicit open centreline, with closed end caps.
Applications include furniture bows and rails, handles, pipes, cables and bent rods.

| Parameter | Meaning | Bounds/default |
|---|---|---|
| `path` | Ordered local XYZ centreline samples, in metres | 2–128 points |
| `radius` | Circular cross-section radius, in metres | Finite and positive |
| `segments` | Vertices around each cross-section | 3–64; default 12 |

```json
{
  "id": "arm_rail",
  "kind": "sweep",
  "params": {
    "path": [[-0.25, 0, 0.65], [-0.25, 0.15, 0.70], [0, 0.25, 0.75], [0.25, 0.15, 0.70], [0.25, 0, 0.65]],
    "radius": 0.015,
    "segments": 12
  },
  "transform": {"translate": [0, 0, 0]}
}
```

The example costs 120 triangles. Add centreline samples where a smooth silhouette needs them. Two points make
a straight rod between arbitrary endpoints; there is no need to derive Euler angles for an inclined spindle.

The exact cost, including both caps, is `2 × segments × len(path)` triangles. The vertex count before UV and
normal seam splitting is `segments × len(path) + 2`. The bounded contract permits at most 16,384 triangles
per sweep; the scene's existing triangle budget still decides whether the complete asset is admissible.

The kernel preserves the samples. It does not fit a spline, recenter the path, infer a chair or silently repair
invalid geometry. Cross-section frames use parallel transport, so straight intervals and inflections do not
cause a Frenet-frame flip. Interior rings follow tangent bisectors; end caps are perpendicular to end segments.

## Integration

- Typed IR parameters generate scene and patch response schemas. Public `ParamValue` also carries XYZ lists.
- The lean MCP schema accepts both uses of `path`: a sweep's point list and an imported mesh's filename.
- CLI authors, stage agents and the director's part plan see the new shape. Production-prop, kit-piece and
  intermediate-blockout instructions keep their existing distinctions.
- The packaged kitbash skill describes the path, bounds, cap behavior, cost and mirror limitation. Its existing
  exact node-kind coverage test remains; parsing the declared names also detects unsupported extra kinds.
- The compiler provides the mesh, analytic UV charts and exact triangle estimate. USD persistence, local path
  edits, rollback, LODs, collision decomposition and package export use the existing machinery.
- Budget repair can lower radial resolution without changing the centreline, radius or unrelated nodes.
- Row/ring repeats are supported. The positive-scale `mirror` shortcut explicitly refuses sweeps, including
  children of a mirrored group: rebuilding reflected transported frames can change nonplanar quad diagonals.
  Supply the other path explicitly. Existing negative-scale transforms retain their compiler winding behavior.

Each radial side is an open triangle ribbon; each cap has its own UV chart. Tests require analytic unwrap,
negligible area/angle distortion, zero overlap on the tested curves, and the existing texel-density target,
including nonuniform scale. Collision uses the actual concave rod through existing decomposition, rather than
an arch-filling convex proxy.

Malformed/nonfinite points, repeated adjacent samples, reversals, insufficient bend clearance, centreline
crossings and obvious nonlocal tube contacts are refused with node-specific repair messages. The bend guard
uses `radius × tan(turn/2)`; a regression covers an almost reversing three-point hairpin. These guards are not
a complete triangle self-intersection solver. Existing geometry and package checks remain required.

## Quality gates

No scorecard threshold, topology rule, UV tolerance, LOD error bound, collision limit or existing mandatory
qualification case was relaxed. `qualification.yml` adds two required swept-rail cases to the existing engine
set. No benchmark results from round five were replaced.

The new schema initially exceeded existing request-size limits. Internal `$defs` references now use compact
aliases, and repeated guidance is shorter; response fields and validation constraints are preserved. The
existing valid/invalid schema corpora verify acceptance equivalence. This is a schema byte-size measurement,
not a provider-token or billing claim.

| Response schema (compact JSON) | Bytes | Existing gate |
|---|---:|---:|
| Scene, named-asset fixture used by the existing test | 7,671 | <7,700 |
| Patch | 11,317 | <11,400 |

The request snapshot is versioned explicitly for the added geometry language. The original `C8565A7` hashes
remain in the test, with an eight-key `SWEEP_V1_REQUESTS` delta. Unaffected improvement/repair/look text, look
schema and shader-tool hashes remain exact. Replacing only the corrected coordinate sentence reconstructs
the previous complete LOOKDEV prompt hash. All colour-conversion tests remain in force.

The documentation check's negative-control sentence now names unsupported `torus`, because `sweep` is a real
primitive. Its rejection assertion remains, and a positive assertion checks that `sweep` is accepted.

## Reproduction

Install the project using its existing development extra. For the local unit configuration, explicitly prevent
live execution and point optional unavailable runtimes at absent paths:

```bash
env -u GEMINI_API_KEY -u GOOGLE_API_KEY -u AGY3D_THREE_JS -u GODOT_BIN \
  AGY3D_RUN_LIVE=0 BLENDER_BIN=/nonexistent/agy3d-blender \
  CHROMIUM_BIN=/nonexistent/agy3d-chromium OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 \
  .venv/bin/python -m pytest -q -m 'not live and not blender'
```

The focused feature tests are:

```bash
AGY3D_RUN_LIVE=0 BLENDER_BIN=/nonexistent/agy3d-blender \
  OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 .venv/bin/python -m pytest -q \
  tests/test_sweep.py tests/test_sweep_wiring.py tests/test_sweep_pipeline.py
```

Engine coverage runs `tests/test_shapes_engines.py` with the pinned Blender 5.3 binary and Godot 4.7.2 from
`.github/workflows/qualification.yml`. The Blender archive was checked against that workflow's SHA-256:
`dacb8264197a3f7fda6001285a5a506fc97ee943881261c2c3bd63c882f22e05`.
The real-engine tests use `offline_plane`, `CallableAuthor` and `FakeImageBackend`; they make no paid model calls.

The hand-authored rail fixture uses 33 centreline samples and 12 radial segments. The direct export test at
1,024 px/m produces LOD counts `[792, 402]`. The full delivery pipeline's shared atlas for the flat material
uses its existing 128 px/m detail target and produces `[792, 368]`; its density spread is 1.00×. Both keep the
existing screen-space and silhouette gates. A 17-sample variant produces only LOD0 under those gates; the
thresholds were kept and the qualification fixture uses enough detail for safe reduction. This fixture is
geometry/export evidence, not a newly authored chair benchmark.

### Original validation before the main refresh

These measurements were recorded against the PR #6 baseline plus this sweep patch. They are retained as prior
evidence, not presented as a rerun of the PR #7 code. Fresh compatibility checks are recorded separately below.

The selection `-m 'not live and not blender'` contains 2,683 tests. The long process was killed with exit 137
after completing 1,928 tests, before it wrote JUnit XML. Its progress log records 1,902 passes, 22 skips and four
failures. The remaining 755 exact test IDs, starting with the interrupted cache-replay test, were run in three
fresh processes without changing the selection or any assertions:

| Execution | Passed | Skipped | Failed |
|---|---:|---:|---:|
| Interrupted process, completed tests only | 1,902 | 22 | 4 |
| Remaining A (254 tests) | 252 | 0 | 2 |
| Remaining B (281 tests) | 279 | 0 | 2 |
| Remaining C (220 tests) | 167 | 53 | 0 |
| All selected IDs, before targeted rechecks | 2,600 | 75 | 8 |

Two failures were feature integration gaps: the obsolete unsupported-sweep negative control and the packaged
skill's missing sweep declaration. Both were corrected and their whole test modules pass (116 tests). The
shared-atlas failure passes with the supported Blender binary, and the deadline case passes in isolation on
both revisions. Four existing socket-dependent cases remain blocked: two inspector-browser tests and two
worker-daemon tests; all four reproduce on unchanged `main`. This is complete selected-test coverage across
processes, **not a clean monolithic full-suite run or full engine qualification**.

- Focused geometry, schema/tool and offline package tests: 97 passed in the independent review. The final
  native-tool test also rejects a filename used as an existing sweep's path without replacing the valid scene.
- Existing baseline shape/compiler/schema/shortcut/author-stage tests: 282 passed, one live case deselected.
- Colour-contract and posted-stage-request tests after the explicit snapshot update: 48 passed.
- Documentation checks after updating the obsolete unsupported-primitive fixture: 90 passed.
- Packaged skill and documentation checks after adding the sweep guidance: 116 passed. The previously failing
  kind-coverage assertion now includes sweep and still requires exact equality with the IR's node kinds.
- Supported engine checks: four passed (two existing shaped-crate cases and two new swept-rail cases), 34.02 s.
  Blender 5.3 independently checks the rail with zero errors and zero warnings in both game and Roblox profiles.
  Godot 4.7.2 imports the 792-triangle mesh and all three texture maps. Roblox FBX re-import in Blender agrees
  with the package across 24 checks; this is not a Roblox Studio runtime validation.
- Eight CoACD collision hulls are produced, within the existing eight-hull budget. The cold package validator
  reports no USD, engine-format or package errors.
- The existing shared-atlas Blender rendering test passes with the pinned 5.3 binary (4.72 s). Its run with the
  deliberately absent unit-test binary failed because that test checks whether the environment variable is set,
  rather than whether the named executable exists; no test behavior was changed.
- Browser qualification is blocked in this environment: Chromium aborts with `socket() failed: Operation not
  permitted`. The initial configured-browser run fails on both unchanged crate fixtures and both sweep fixtures
  at that same step. The supported-engine run marks the unavailable browser as skipped, never passed. The new
  cases remain required in CI, where the existing browser setup and assertions run unchanged.
- The two existing inspector-browser tests also fail unchanged on `main`. With the deliberately unavailable
  browser path, the installed Playwright's lower-case `executable doesn't exist` message misses their existing
  case-sensitive skip check. Running just those tests with real `/usr/bin/chromium` independently confirms the
  same forbidden-socket abort before any page opens. No browser test or exception handling was changed.
- Both worker-daemon failures reproduce unchanged on `main` (0.84 s): socket construction raises
  `PermissionError: [Errno 1] Operation not permitted` for `AF_UNIX`, before a daemon can become ready.
- An existing local-HTTP deadline test failed during the broad run, but passes in isolation on both unchanged
  `main` (2.52 s) and this checkout (2.38 s), with the same proxy settings. Its broad-run failure is retained in
  the evidence; isolated passes do not establish a clean full-suite run.
- These offline runs use a fixed author, fake model decisions and fake image backend. Their decision scores
  are test fixtures, not visual judgments. Packages remain unverified previews; neither full browser qualification
  nor artistic improvement is established.
- The first broad unit attempt exposed a missing `socksio` dependency in the supplied proxy-configured test
  environment. Twenty-four selected failures reproduced on unchanged `main`; installing the optional transport
  dependency fixed the reproduced case. No project dependency or quality threshold was changed to resolve it.

### Fresh validation after updating to PR #7

On `0edb0ff295eabe8424c21bd0d6da24b69fcab3ec` plus this patch, three independent local runs passed:

| Group | Passed | Skipped | Deselected | Duration |
|---|---:|---:|---:|---:|
| Sweep, existing shapes, compiler, schema and shortcuts | 312 | 0 | 0 | 20.09 s |
| Colour, stage requests, skills/docs and authoring agents | 210 | 0 | 1 | 29.34 s |
| Upstream script API and runner compatibility | 217 | 1 | 2 | 91.41 s |
| Total | 739 | 1 | 3 | Separate processes |

The script compatibility group covers `test_build_shapes`, `test_build_asset`, `test_build_blender`,
`test_run_build`, `test_run_build_runner` and `test_run_build_plugin`. Its only skip is an unavailable external
reviewer's snippet list; the embedded legitimate-snippet cases still run. All three groups used
`-m 'not live and not blender'`, removed model keys, disabled live execution, and pointed unavailable engines
and Chromium at absent paths. No paid model calls were made.

This fresh pass is focused compatibility validation, not a repeat of the full historical suite or supported-engine
measurements above. The PR's existing CI and engine/browser qualification remain required. The separate paid
provider canary is manual-only and was not dispatched. The refreshed diff passes `git diff --check` and keeps
all current-main changes, including the script API, its engine gates and the rotated-primitive bounds fix.

## Future live comparison

Any live execution requires separate approval. Keep the existing chair brief, 5,000-triangle ceiling, model and
execution settings, image allowance, render setup and neutral scorecard. Use matched repeated generations,
retain failed runs, randomize blind order, and report deterministic acceptance separately from aesthetic
preference. The current correction enables the geometry; it does not establish changed win rate, production
yield, latency or cost.
