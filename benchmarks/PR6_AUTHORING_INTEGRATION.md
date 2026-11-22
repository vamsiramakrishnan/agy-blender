# PR #6: authoring contracts and comparison integration

Reviewed 22 November 2026 (Australia/Melbourne). Integration target: existing PR #6,
`vamsi-/gifted-euler-vg6ygz`. Its inspected head is
`a75ed4a1b27f25cdf29b167c8cfd2b131e0e122e`. Main is
`814f436fcc692f09c47140d5f37c72b69ed50d28`, which merged the earlier authoring-contract fix in PR #5.
PR #4 had already merged as `bbc3945`.

This work merges current main into PR #6. It does not open another PR or replace the existing PR #6 commits.
Publication requires the user's explicit approval. No billable model run was made for this integration.

## What the actual contracts require

| Entry point | PR #4 contract | Integrated PR #6 contract |
| --- | --- | --- |
| `agy3d prop` via `AgentAuthor.bind()` / `MeteredAuthor` | One-shot production prop with craft guidance | Same PR #6 one-shot request, including bevel, inset, lathe, extrude, mirror and repeat; canonical guidance imported from `authoring_contract.py` |
| Direct `AgentAuthor.propose_with_outcomes()` | Task requests a blockout and says to stop after applying IR, despite production instructions in the system | Explicit production intent; complete named construction and material assignments, then geometry QA |
| Direct proposal with valid kit context | Kit metric in the task, prop-detail target in the system | Explicit kit intent; metric dimensions, mating surfaces, openings, pivots and relief determine geometry |
| Bare `build_stage_agent("BLOCKOUT")` | Production-detail target injected into an intermediate stage | Intermediate intent by default; primary forms, proportion and contact; secondary detail may be deferred |
| Direct repair | Blockout wording; mutable most-recent proposal context | Asset-specific intent, brief, budget, art and metric; localized corrections and explicit brief-deficit scope |
| One-shot visual repair / improvement | Earlier repair contract without PR #6's design loop | PR #6's images, numbered-part legend, judge observations, blueprint and rejected-patch feedback retained |

`BLOCKOUT` remains the geometry toolset. `AuthoringIntent` supplies the geometry completion requirement independently.
No tool permission, topology threshold, export gate or candidate count is relaxed. An intermediate can finish its
Tier-1 stage while retaining a failed visual verdict; that does not allow export.

Production guidance is shared verbatim between the native production stage and the one-shot author. Its part-count
advice is conditional on triangle budget and art direction; it is not a minimum geometry gate. Kits and intermediate
blockouts receive neither that advice nor the full production craft text. Shape syntax remains available to all
geometry stages, including intermediate stages that need a lathe or an extrusion for the primary silhouette.

## Conflict resolution

The merge produced six textual conflicts. Taking either side wholesale would lose behavior:

| File | Resolution |
| --- | --- |
| `harness/agents.py` | Retain main's intent and repair selection, plus PR #6's shape semantics and expanded primitive vocabulary |
| `tactics/authoring.py` | Retain asset-scoped contracts and production task wording, plus new shape bounds, exact triangle costs and progress forwarding |
| `tactics/metered_author.py` | Import one shared copy of PR #6's craft guidance; combine shared repair scope with PR #6's rendered design evidence |
| `tests/test_stage_request.py` | Keep real wire-request tests for intent isolation and new shape tools; measure prompt budgets for the combined contract |
| `benchmarks/compare_agents.py` | Preserve the explicit paired pilot alongside PR #6's vanilla-MCP, change-request, repeat and scorecard modes |
| `benchmarks/README.md` | Retain both the round-five workflow and the paired comparison documentation |

The shared `PROP_CRAFT_GUIDANCE` remains byte-identical to the one-shot guidance on `a75ed4a`: **2,362 characters**,
SHA-256 `c11d8345c6626bc1034bcde95c8cab44a7b0f05aeb7c403275d806869ab11e6c`.
Two proposal variants and twenty repair variants were compared against the original functions extracted from that
commit. Prop/kit tasks, localized/brief-deficit repairs, rendered views, blueprints and rejected-patch feedback produce
identical task bytes. This integration does not invalidate the one-shot design cache merely by relocating the text.

Measured static request sizes (characters, not tokens):

| Native geometry session | System text plus tool declarations |
| --- | ---: |
| Production prop | 12,010 |
| Kit piece | 9,930 |
| Intermediate blockout | 9,942 |
| Localized prop repair | 9,910 |
| Localized kit repair | 9,907 |

These are actual requests posted by the SDK to the local scripted endpoint. The production increase pays for the
same detailed shape guidance used by the one-shot author. LOOKDEV and EXPORT retain their existing prompt budgets.

## Fair comparison through the existing harness

The opt-in paired pilot in `compare_agents.py` holds the following choices fixed before any call:

| Control | Pilot setting |
| --- | --- |
| Arms | A: stock Antigravity with scriptable Blender; C: `agy3d prop --preview` |
| Brief | Identical component, construction-detail, scale, triangle, UV, PBR and material-count requirements |
| C candidate mode | Explicit `single`; not selected from leftover image quota |
| Per-arm ceiling | 700,000 tokens and 12 generated images |
| Wall allowance | 75 minutes per arm |
| Blind judging | Same full-size rubric, both presentation orders; two calls, 25,000 tokens in total |
| Overall ceiling | 1,425,000 tokens and 24 images |
| Continuations / change requests / repeats | Excluded from this one-session pilot |
| Provenance | Exact prompts, source and harness hashes, SDK versions, configured toolchain and fake/live identity |

The round-five default remains available separately, including stock vanilla MCP (V), the plugin arm (N), change
requests, cache repeats and game-size judging. Those extra treatments cannot silently enter the explicitly reviewed
paired pilot or consume its quota. Equal ceilings do not imply equal consumed cost or identical workflows.

The stock arm retains the benchmark's existing execution overrides, now serialized explicitly in the plan:

| Stock A setting | Actual pilot value |
| --- | --- |
| Text / image model | Installed SDK defaults: `gemini-4-hydrogen` / `gemini-3.1-flash-lite-image` |
| Thinking | Unset; SDK/model default |
| Output limit per response | 16,384 tokens |
| Call count in the task | About 14; advice based on historical context growth |
| SDK hard limits | 40 model calls and 120 tool calls |
| Wall allowance | 4,500 seconds in both the SDK and host runner |
| Built-ins disabled | Web search and URL reading |
| Project customization | No project plugin or project skills |

C's worker author permits 24,576 output tokens with low thinking. The stock call/tool caps or per-response output
cap may bind while aggregate quota remains. They are declared system-level differences; the relay is not guaranteed
to be the first limit reached. A future result must retain actual stop reasons, truncations, usage and refusals.
The plan and result notes no longer repeat the round-five defaults of zero images and a change request per cell.

C uses `MeteredAuthor` through `bind()`. This measures the end-to-end command, not an isolated ablation of
`AgentAuthor.propose_with_outcomes()`. The native correction is covered independently by real SDK request and tool
dispatch regressions. One crate pair is a pilot; it cannot establish studio acceptance or a population win rate.

The shared three-material requirement is checked on the delivered GLB even though C's engine budget permits four.
Failed CLI gates remain failures when a diagnostic GLB exists. Scoring requires readable images for all required
PBR slots: binding a missing or corrupt image no longer earns maps credit or hides the material-repair hand step.
Unused broken images do not invalidate otherwise readable required maps.

Separately executed cells must retain the same selected toolchain. Paired execution checks for a shared Blender
5.3-or-newer executable before opening the relay, so C cannot silently omit Blender after A has spent its allowance.
Disk-only `score --root` recovers the saved paired round and reads the saved ledger without rewriting ceilings or
settling outstanding reservations.

No-cost planning command:

```bash
python benchmarks/compare_agents.py plan --json \
  --paired --round prop-contracts-pr6 --c-candidates single \
  --run-tokens A=700000 C=700000 \
  --run-images A=12 C=12 \
  --ceiling-tokens 1425000 --ceiling-images 24
```

`plan` makes no model call. Execution additionally requires the user's approval, a fresh output root,
`live --go --allow-c`, and passing local toolchain/sandbox prerequisites. Approval to publish code does not authorize
the billable pilot.

## Validation

The [evidence summary](results/authoring_contracts/pr6-integration-validation.json),
[frozen paired plan](results/authoring_contracts/pr6-paired-comparison-plan.json), and
[task-byte comparison](results/authoring_contracts/pr6-one-shot-compatibility.json) record the source at merge commit
`dea0916bdb267a13816c22c588d8783901402041`. The following evidence commit changes only documentation/results.
Validation uses local stand-ins for model calls and does not measure new model output quality.

- Final focused suite covering authoring, actual SDK wire requests, streaming, design improvement, parse correction,
  skills, comparison controls, scorecards, modular kits, kit metrics and native hooks: **349 passed, 8 skipped**
  in 74.23 seconds.
- MCP server subprocess and material-prompt checks, with the source path pinned to this checkout: **37 passed**
  in 30.37 seconds.
- Required-map scorecard regression: **12 passed**. The same tests against unmodified PR #6 produce
  **11 failures and 1 pass**. Fixtures are real GLBs with required PNGs, missing/corrupt images, invalid references
  and an unused-image positive control.
- One-shot proposal/repair compatibility: **22 of 22 task variants byte-identical** to PR #6's original head.

The eight focused skips are six stock-sandbox tests requiring root namespace privileges, one absent upstream-skill
checkout, and the live model test. No permission or model-spend gate was bypassed.

The extended core sweep (`not blender and not browser and not live`) was killed with exit code **137** before
completion, after its last complete progress line at 55%. It emitted no completed summary/JUnit report. Its cause
beyond SIGKILL was not established, and it is not counted as a full-suite pass. An earlier sweep was discarded after
reproducing a source-selection problem: relative `PYTHONPATH=src` allowed subprocesses with a temporary working
directory to load the older editable checkout. The final focused/MCP runs use an absolute source path. The evidence
also records an interrupted concurrent preview run without assigning it a completed pass/fail count; its isolated
deadline check passed on both the integrated source and original PR #6.

The current environment has Godot 4.7.2 and Blender 4.3.2. It cannot qualify the required Blender 5.3 worker. The
offline suite excludes real Blender/browser/live markers and records unavailable checks explicitly; no substitute
engine result is described as Blender 5.3 qualification. The optional Playwright package is absent from the core
test environment. Hosted engine qualification and any paid quality comparison remain separate evidence.

## Historical geometry evidence

The November 21 numbers describe different specimens and must not be conflated:

| Observation | Raw authored triangles |
| --- | ---: |
| Five older one-shot designs | 272–356 |
| Two early craft-guidance designs | 936 and 976 |
| Exact-contact wording experiment | 1,004 |
| Later shipped one-shot author call | 868 |

The original stock-versus-project exported GLBs had 1,476 and 266 triangles respectively. Those are final-file
counts, whereas the table above counts authored geometry before export transformations. Triangle count provides
context; it does not prove artistic quality. The original stock package's stronger geometry, failed material/runtime
checks, and the project's assisted completion are documented in
[the historical review](AUTHORING_CONTRACT_REVIEW.md) and the underlying round-three/four records.

PR #6 already adds the bevel/inset/lathe/extrusion vocabulary that PR #4 lacked. Preserving that implementation during
the merge closes the representation gap identified in the historical review; it does not establish that the newly
integrated native author now beats stock. No new paid comparison was executed.

## Separate open review finding

PR #6's [installer path-containment finding](https://github.com/vamsiramakrishnan/agy-blender/pull/6#discussion_r4656681979) is outside this authoring/comparison integration
and remains open. The merge-conflict resolution does not certify the whole PR as ready to merge. The required-map
finding ([review comment](https://github.com/vamsiramakrishnan/agy-blender/pull/6#discussion_r4656681983)) is addressed here because it affects the requested comparison's scorecard.
