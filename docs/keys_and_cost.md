# Keys and cost

What a run needs from your Gemini key, what it costs, how to cap what it may spend, and which model does what.
Commands and their other options are in [commands.md](commands.md).

## What your key decides

Live runs (any command without `--offline`) need `GEMINI_API_KEY`. What they can produce depends on whether the
project behind the key serves the **calibrated judge**, the one this project relies on to clear a package. The code
and its messages call it the native Decisions API; these pages say "calibrated judge".

| Your key | What you get |
|---|---|
| Serves the calibrated judge | A verified package is possible for Godot 4 (with Blender and Godot present) and for three.js, `--engine web_three` (with Blender and a Chromium browser) ([what verified means](verification_and_limits.md#what-verified-means)). For Unreal, Unity and Roblox a run can only be a preview. |
| `decisions.create` answers 404 (no calibrated judge) | Only an **unverified preview**, judged by a generative fallback. Spend is bounded by limits you give (below). |
| `--offline` on the command line | Stand-ins for the judge and the images, and a built-in template instead of your design. |
| No key, in an Antigravity session | Stand-ins for the judge and the images; delivery is refused until you allow an unverified preview with your two limits. |

The fallback judge is uncalibrated: it may route, rank candidates, drive the repair rounds and fail a gate, but it
never verifies, publishes or clears anything. Its probabilities are not calibrated. The credentialed canaries in the
test suite exercise the calibrated judge and the image model (Nano Banana) independently.

In an Antigravity session, the plugin's server uses the real judge and image model when `GEMINI_API_KEY` is set, and
the stand-ins when it is not (or with `AGY3D_OFFLINE=1`).

## Keys without the calibrated judge

Without configured prices, spend is bounded by two limits you state: a token ceiling (input plus output tokens,
thinking included, with no price applied) and a hard image count.

```bash
agy3d prop "wooden crate with iron brackets" --preview --token-ceiling 150000 --max-images 4 --single-candidate --out out/preview-crate
agy3d kit "ruined stone temple" --pieces 4 --token-ceiling 400000 --max-images 1 --out out/preview-kit
```

- `--token-ceiling` is the most the whole command may use. For a kit, every piece and the trim sheet draw from it.
  Every call reserves its worst case, in tokens, before it is sent, and books the provider's reported usage
  afterwards. Receipts say `"unit": "tokens", "price_applied": false`.
- `--max-images` is required with `--token-ceiling` whenever the command can generate an image. Without
  `--single-candidate`, a prop's plan needs several images per material.
- Texture images are generated for the winning design's materials only, largest surface first, from each
  material's own surface words. When the image limit does not cover every material, the ones left untextured are
  named in the run's warnings and in the manifest.
- The fallback judge can take minutes for one call: on 2026-11-21 the same texture ranking took 30 s, 176 s and more
  than 240 s. A call with no answer within 600 s is not sent again, and what it reserved stays counted.
- The preview's `manifest.json` says `verified: false`, `preview: true`, who judged it and what that means.
- The command ends with `publication: UNVERIFIED PREVIEW` and `review: required`.
- Exit status 0 means every other gate passed; a hard policy flag, an escalation to a person or a failed gate exits
  1 (`--strict` adds exit 3 for a review below target).
- Without `--preview`, or with `--require-verified`, such a key is refused at start with the command to run instead.
  A ceiling too small for one judge call and one authoring call is also refused at start, with the smallest ceiling
  that can work. In both cases nothing is generated.
- In an Antigravity session the same switch is `open_asset_session(preview=true, token_ceiling=N, max_images=M)`,
  or `start_preview` on an open session. The plugin asks you to approve both numbers.

A token ceiling bounds tokens, not money: image tokens cost far more than text tokens, which is why the image count
is a separate limit.

## Configuring prices in dollars

The structured command-line author, the Decisions plane and image generation report usage with provenance. Monetary
estimates depend on the rates you configure; studio acceptance and reconciled billing are separate inputs to the
cohort benchmark (below).

- **Text models.** Set `AGY3D_TEXT_RATES_JSON` to a JSON object mapping each configured text-model id to its
  contracted `[input USD per million tokens, output USD per million tokens]` pair.
- The metered author checks the configured rate before dispatch. Standard tiers use the `worker` role and hero
  assets the `director` role. Model overrides need matching rates.
- **A custom image deployment.** Set `AGY3D_MODEL_IMAGE` and all three per-million-token rates:
  `AGY3D_IMAGE_INPUT_USD_PER_M`, `AGY3D_IMAGE_TEXT_OUTPUT_USD_PER_M` and `AGY3D_IMAGE_IMAGE_OUTPUT_USD_PER_M`. The
  legacy `AGY3D_IMAGE_MODEL` override takes precedence.

## What a run typically spends

Measured on three briefs (a supply crate, a fire hydrant, a Windsor chair), no generated images, on a key without the
calibrated judge. Values are crate / hydrant / chair.

| What | Time | Tokens | Source |
|---|---|---|---|
| `agy3d prop` on the command line, `--max-images 0` | 661 / 994 / 527 s | 0.37 / 0.45 / 0.39 million | [round five](../benchmarks/ROUND_FIVE.md), "The short answer", column C |
| `agy3d edit` on the command line | 98 to 164 s | 0.06 to 0.09 million | round five, the same table |
| A session with the plugin, default mode (median of two runs) | 240 / 282 / 276 s | 108 / 169 / 135 thousand fresh | [round six](../benchmarks/ROUND_SIX.md), "The short answer", N |
| A session in script mode (one run) | 254 / 313 / 258 s | 161 / 166 / 138 thousand fresh | round six, S |
| A change request in a session, script mode | 180 / 224 / 131 s | 161 / 110 / 90 thousand fresh | round six, S |

Fresh tokens are what a run pays in full: input the model service did not serve from its cache, plus output, plus
thinking. Round five's harness gave the command a 485,000-token ceiling (`benchmarks/compare_agents.py`, `BUDGET` less
`C_TOKEN_MARGIN`); the 600,000 on the front page leaves room above the largest run.

## The spend ledger

The prop pipeline writes `ledgers/<asset-key>/spend.sqlite3` under the output directory. The key includes the
prompt, engine budget, style content, judge backend and candidate mode.

- Retries reuse the original route and ledger. Pending reservations count against the ceiling across restarts.
- Image receipts keep the model, the provider's response id, token counts by modality, the estimated USD and the
  artifact's SHA-256. Replays have zero generation cost.
- When a dispatched request times out or the provider omits modality counts, the ledger keeps the reserved amount.

### The spend ledger across reruns

The ledger is the asset's own and covers every run in one `--out` together (how a rerun reuses designs and images
is in [commands.md](commands.md#running-again-in-the-same-output-folder)).

- A rerun says at start how much is already used and how much is left, and its summary adds `this run: ...`. Raise
  `--token-ceiling` or `--max-images` to give it more.
- A rerun is refused before any call when what is left of the ceiling cannot admit its largest single call; the
  refusal names what was already used, what remains, and the smallest ceiling that lets it continue.
- A rerun that is short of images continues only when it can continue from a recorded design: images already
  generated for that design count none, and a material beyond the limit ships untextured and is named.
- Without a recorded design, a rerun short of images is refused before any authoring call, as a first run is. So is
  a rerun with `--new-design`.

## Model tiers

Defined in `src/antigravity3d/models.py`. `agy3d models` prints the effective table after overrides.

| Role | Model id | Thinking | Used for |
| --- | --- | --- | --- |
| `director` | `gemini-4-argon` | HIGH | Plan, hard maths, ambiguous escalations. Slow; give it a generous timeout. |
| `builder` | `gemini-4-carbon` | MEDIUM | Stage agents: blockout, lookdev, export. |
| `worker` | `gemini-4-hydrogen` | LOW | Ephemeral critics, swarm prop builders. |
| `bulk` | `gemini-4-neon` | LOW | Batch sanitise, trivial IR edits. |
| `decision` | `gemini-decision-1` | - | Routing, Best-of-N ranking, visual QA, policy. |
| `image` | `gemini-nano-banana-2.1` | - | 2K blueprints, texture maps, trim sheets, decals and paint-overs. |

The verification notes in `models.py`: `gemini-4-hydrogen`, `gemini-4-carbon` and `gemini-4-neon` answered live
against the Gemini API; `gemini-4-argon` exists but is slow. The harness bundled with google-antigravity 0.1.24
names hydrogen, carbon and argon but not neon. The live API rejects `ThinkingLevel.MINIMAL` for neon and hydrogen,
so LOW is the floor.

Overrides:

- `AGY3D_MODEL_DIRECTOR`, `AGY3D_MODEL_BUILDER`, `AGY3D_MODEL_WORKER`, `AGY3D_MODEL_BULK`, `AGY3D_MODEL_DECISION`,
  `AGY3D_MODEL_IMAGE`: replace the model id for one role.
- `AGY3D_NEON_AVAILABLE=0`: routes the `bulk` tier to hydrogen if a harness build rejects neon.

## Measure the cost per accepted asset

`agy3d benchmark cohort.json` compares single-candidate runs with governed runs on your own briefs.

For each brief, run the same prompt and engine budget twice in separate output directories under the same cache
policy. Add `--single-candidate` to the baseline only. Record studio first-pass and final acceptance, rework
minutes, reconciled spend and input/output tokens across the entire run, including rejected attempts. Use
independent human labels. The benchmark rejects reused reports, mismatched budgets, fixture-backed verdicts and
first-pass labels without a verified asset.

```json
{"ceiling_usd": 2.2, "pairs": [
  {"brief_id": "crate-01",
   "single": {"report": "baseline/report.json", "first_pass": false, "accepted": true,
              "rework_minutes": 12, "billed_usd": 0.41, "input_tokens": 9800, "output_tokens": 3200},
   "governed": {"report": "governed/report.json", "first_pass": true, "accepted": true,
                "rework_minutes": 0, "billed_usd": 0.70, "input_tokens": 16000, "output_tokens": 5400}}
]}
```

These numbers illustrate the input format. The command reports spend per accepted asset, total tokens, wall p50/p95
and paired quality deltas. The rollout gate needs at least 50 pairs, every governed run within the studio ceiling,
and either a 10-point first-pass gain or a 20% reduction in rework minutes.

`agy3d calibrate LABELS.json --out studio_policy.yaml` fits the studio's judge thresholds from labelled history.
