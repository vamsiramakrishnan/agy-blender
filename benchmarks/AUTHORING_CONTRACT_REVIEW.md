# Production-prop authoring contracts: PR #4 follow-up

Reviewed 22 November 2026 (Melbourne). PR #4 is merged. Its final head is
`49c498bbd679cba9d125f09bde714f22379db794`; the follow-up starts from merged main
`bbc39452dc4f831b77e31b780cf2604d8646e451`. Their source trees are identical.
The follow-up branch is `fix/prop-authoring-contracts`. The evidence below records local validation completed before
publication. No billable model run was made.

## Finding

The shipped CLI already requests production props. The remaining defect is inconsistent contracts between its
one-shot author, direct stage-based authoring, and an intermediate geometry stage.

| Entry point on PR #4 | Actual contract | Correction |
| --- | --- | --- |
| `agy3d prop` → `AgentAuthor.bind()` → `MeteredAuthor` | Production prop with `PROP_CRAFT_GUIDANCE` | Import the same unchanged guidance from one shared module |
| Direct `AgentAuthor.propose_with_outcomes()` | Task says “Build a blockout” and stop after `apply_scene_ir`; system says production prop | Select production intent; request complete geometry and material assignments, then geometry QA |
| Native `build_stage_agent("BLOCKOUT")` | Every session receives production craft instructions and a 50–90-part target | Intermediate blockout by default; explicit production or kit intent when needed |
| `agy3d kit` → bound `MeteredAuthor` | Exact metric, no production craft text, smaller worker output cap | Preserve request bytes, metric and cap |
| Direct kit proposal | Metric in task, production craft text in system with an override clause | Select kit intent; omit prop craft and part target entirely |
| Native repair | “Repair this blockout”, globally injected craft target, most recent asset context | Preserve the repaired asset's own intent, brief, budget, art and metric; localize edits |

Source: [authoring.py](../src/antigravity3d/tactics/authoring.py),
[metered_author.py](../src/antigravity3d/tactics/metered_author.py),
[agents.py](../src/antigravity3d/harness/agents.py), and
[environment_kit.py](../src/antigravity3d/dags/environment_kit.py).

## Proposed implementation

[`authoring_contract.py`](../src/antigravity3d/authoring_contract.py) defines three intents:

| Intent | Geometry requirement | Completion scope |
| --- | --- | --- |
| `production_prop` | Every named component, plausible construction, useful secondary forms, proportion, exact contact and appropriate colliders | Complete prop geometry, then the existing geometry QA workflow |
| `kit_piece` | Exact dimensions, mating surfaces, opening, pivot, relief and face-size constraints from the kit metric | A compliant module, without a prop-specific part-count target |
| `blockout` | Primary forms, silhouette, proportions and contact | Intermediate geometry; secondary-detail requests are reported as deferred work |

A direct proposal selects production or kit intent from its valid kit context. A bare native BLOCKOUT stage remains
intermediate. Callers that want a finished prop can select it explicitly:

```python
from antigravity3d.authoring_contract import AuthoringIntent
from antigravity3d.harness.agents import run_stage

await run_stage(
    "BLOCKOUT", session_id, task,
    authoring_intent=AuthoringIntent.PRODUCTION_PROP,
)
```

The toolset name, tools, subagents and validation thresholds stay the same. TWEAK, VARIANTS, LOOKDEV and EXPORT
receive no additional production-detail instructions.

### Shared production requirements

The canonical `PROP_CRAFT_GUIDANCE` is byte-identical to PR #4: 1,136 characters,
SHA-256 `fc5b2cda1c677bb218876ab7a13baade1fc9a0c8470d8d0ae10f7673f8902327`.
The old `metered_author.PROP_CRAFT_GUIDANCE` import remains available. The native production stage uses that same
text rather than a second, separately maintained version.

The worker's one-shot output allowances remain **24,576 tokens for a prop** and **16,384 for a kit piece**.
One-shot proposal and repair task bytes were compared against the original functions from `bbc3945`: prop, kit,
localized repair, brief-deficit repair, no reasons, and string reasons all match. Design-record keys and admission
behavior therefore retain the existing requests. Stage sessions remain uncached.

The 50–90-part wording is retained as production guidance, within the triangle ceiling. No minimum triangle or part
count has become an acceptance gate. Structural validity and increased polygon count do not establish artistic quality.

### Intermediate QA retains its failed verdict

A recognizable intermediate can score 2.0 on the existing finished-prop depiction rubric; the production threshold
is 2.5. Requiring both “defer secondary detail” and “continue until no geometry repair” would still conflict.

The intermediate workflow now fixes primary-form, proportion and contact defects, reports secondary-detail
shortfalls as deferred, and finishes its existing Tier-1 stage. Its visual verdict remains `agent_repair` when the
production rubric fails. A regression exercises real native QA with a local fake score of 2.0, checks that the
intermediate can stop, and verifies export is still blocked. No final approval is manufactured.

### Repairs preserve the asset they belong to

Direct `AgentAuthor` records an immutable context per asset name. Concurrent prop and kit proposals, followed by
repairs, use each asset's own brief, budget, art direction and metric. A different contract under an already-used
asset name is rejected before dispatch; use a distinct name or author for that new contract. This is an intentional
API behavior change for ambiguous direct-call reuse; the CLI's bound one-shot paths are unaffected.

Kit rejection feedback remains per attempt, so retries can update feedback without changing the metric identity.
A repair to an unknown asset is refused before a session opens. Brief-deficit repairs share the one-shot author's
instruction to add missing named components while retaining correct nodes; a localized repair does not receive the
full production part target.

## What the historical measurements mean

The November 21 experiment contains several different observations. Raw authored triangles and final GLB triangles
must be kept separate, and the two 55-part designs are distinct specimens.

| Observation | Parts | Raw triangles | Materials | Evidence |
| --- | ---: | ---: | ---: | --- |
| Five older one-shot designs | 15–21 | 272–356 | Varies | `results/round4/checks.json`, `authoring.before` |
| Early guidance, overlap wording | 63 | 976 | 4 | `tests/data/coplanar/overlap_style_63.json` |
| Early guidance, overlap wording | 64 | 936 | 3 | `tests/data/coplanar/overlap_style_64.json` |
| Exact-contact wording experiment | 55 | 1,004 | 4 | `tests/data/coplanar/exact_contact_55.json` |
| Later shipped `MeteredAuthor` call | 55 | 868 | 4 | `tests/data/coplanar/hinge_barrels_55.json`; 15,752 booked tokens, 35.6 seconds |

The recorded round-three comparison is broader than a design-only call:

| Recorded measure | Stock agent | Project's earlier result |
| --- | ---: | ---: |
| Final GLB triangles | 1,476 | 266 |
| Reported workflow tokens | 625,818 | 104,382 |
| Completion | Two stock sessions | Failed command followed by assisted completion |
| Godot import | Pass | Pass |
| three.js / upstream Blender QA | Fail / fail | Pass / pass |
| Blind overall and brief depiction, both orders | Preferred | Not preferred |
| Blind material/surface, both orders | Not preferred | Preferred |

Sources: [`results/round3/agent_comparison.json`](results/round3/agent_comparison.json),
[`results/round4/checks.json`](results/round4/checks.json), and
[`AFTER_THE_COMPARISON.md`](AFTER_THE_COMPARISON.md).

The later blind comparison preferred the project's assembled result overall and for brief depiction, while stock
retained the preference for proportion and scale. That package combined a recorded live design, an offline pipeline
and a reused texture. It was not one completed live run. The fallback judge is uncalibrated and the sample is one
brief. There is no measured new win rate.

The SceneIR's primitive vocabulary remains a constraint: stock Blender used bevel and inset operations that the
current authoring representation does not express. Better instructions address under-specified construction detail;
those modelling operations remain a separate capability gap.

## Validation performed without billable models

The review bundle contains raw results, request captures, JUnit output and the exact paired plan. The included
[evidence summary](results/authoring_contracts/offline-validation.json) records the source hash, baseline failures,
compatibility checks, fixture hashes and limitations; the
[paired plan](results/authoring_contracts/paired-comparison-plan.json) records exact prompts and limits.

| Check | Result | What it establishes |
| --- | --- | --- |
| Same direct-contract probe on `bbc3945` and the patch | 1/7 → 7/7 requirements satisfied | Six existing contract failures reproduced and corrected |
| Native author, posted requests, stage configuration and hooks | 82 passed, 1 live test skipped | Intent dispatch, repair association, intermediate stop/export behavior, existing tools/gates |
| One-shot author, design record, environment kit, kit metric and brief QA | 126 passed | Prompt/cap/cache compatibility and metric/QA regressions |
| Existing and new comparison-harness tests | 38 passed, 5 skipped | Fair-plan constraints, provenance, candidate mode, approval flags and failed-gate reporting |
| Four recorded SceneIRs × Godot/Roblox export | 8/8 Tier-1 OK; zero critical package-validation issues | Deterministic compatibility of saved geometry with current export code |
| Real CLI C through local fake endpoint | GLB produced; Blender QA and Godot import passed; browser launch failed; CLI exit 1 | Execution smoke only; not a completed comparative model run |

The combined focused suite completed with **246 passed, 6 skipped** in 63.66 seconds, with zero failures. Its raw
output is in `out/contract-validation/focused-validation.log` and the accompanying JUnit XML.
Core integration uses the project's dev dependencies without the optional browser extra. The separate browser-enabled
smoke records the actual Chromium launch failure. Four stock integration tests are skipped because the sandbox needs
root namespace privileges unavailable in this environment; one upstream-skill test lacks its checkout at the expected
path. Live tests are not enabled, and real API keys were removed from test and dry-run environments.

The saved-scene replay used the comparison evaluator's `glb_facts()` on actual exported files:

| Fixture | Raw triangles | Godot GLB | Roblox GLB |
| --- | ---: | ---: | ---: |
| `overlap_style_63` | 976 | 976 | 976 |
| `overlap_style_64` | 936 | 934 | 936 |
| `exact_contact_55` | 1,004 | 928 | 1,004 |
| `hinge_barrels_55` | 868 | 752 | 868 |

These replays generate no new textures and do not establish artistic quality or PBR completeness. The original stock
GLB is not checked into the repository; its historical technical results were read, not independently rerun.

The fake-endpoint smoke used 36 local HTTP requests, 13 scripted model receipts and three synthetic images.
Its reported 41,193 tokens are fabricated accounting inputs, not a live cost measurement. It exposed another harness
bug: a diagnostic GLB was called `finished` despite CLI exit 1. The paired profile now reports `failed gates`, with a
focused regression. The original smoke record remains unchanged and records both its old label and the failing exit.

## Fair comparison prepared in the existing harness

[`compare_agents.py`](compare_agents.py) gains an opt-in `--paired` profile. The historical experiment is preserved.
The profile requires explicit equal arm shares, a fresh round and output root, and a fixed candidate policy.

| Control | Prepared setting |
| --- | --- |
| Systems | A: stock Antigravity with scriptable Blender; C: `agy3d prop --preview` |
| Content | Identical component, secondary-form, scale, triangle, UV, PBR and three-material requirements |
| Candidate policy | C explicitly single candidate for the first pilot; never downgraded from remaining images |
| Continuations | None; one session per system per brief; failed deliveries remain failed |
| Budget | 700,000 tokens and 12 images per arm; 25,000 tokens for two blind-judge calls |
| Total proposed ceiling | **1,425,000 tokens and 24 generated images** |
| Wall allowance | 75 minutes per arm |
| Evaluation | Same GLB checks, Godot, three.js, Blender QA and renders; blind judge in both orders |
| Provenance | Exact prompts, source/harness hashes, SDK versions, candidate mode and fake/live identity bound to one root |

The shared three-material cap is checked on delivered files. C's internal Godot budget still allows four, so a
four-material result fails the comparison requirement. C also retains its 15,000-token internal admission margin;
outer caps, actual spend and refusals remain separately visible. Equal ceilings do not imply identical algorithms or
identical consumed cost.

C uses **`MeteredAuthor` via `bind()`**. This is an end-to-end system comparison; it does not measure the native
`AgentAuthor.propose_with_outcomes()` correction. That path has the native configuration and request regressions above.
One crate pair remains a pilot. Broader claims need additional matched briefs and independent human acceptance labels.

### No-cost preflight

```bash
python benchmarks/compare_agents.py plan --json \
  --paired --round prop-contracts --c-candidates single \
  --run-tokens A=700000 C=700000 \
  --run-images A=12 C=12 \
  --ceiling-tokens 1425000 --ceiling-images 24
```

`plan` opens no relay and makes no model call. `dry-run` uses local stand-ins. Changed source/settings and mixed
fake/live reuse are rejected before a relay is created. A proposed default-candidate experiment must use its own
fresh root and recorded policy.

### Approval boundary and remaining execution prerequisites

Publication of this branch and its follow-up PR was explicitly approved after review. The billable pilot remains
unapproved and requires separate explicit approval of the token/image ceiling; no past quota allocation is assumed to
authorize it. The live command additionally
requires `live --go --allow-c --root <fresh-root>`.

A live comparison also needs a host where the existing stock sandbox and Chromium operate correctly. The current
host lacks stock sandbox privileges and the browser-enabled smoke failed at launch. No containment or QA gate was
bypassed. These prerequisites must pass without billable calls before the proposed paired pilot starts.
