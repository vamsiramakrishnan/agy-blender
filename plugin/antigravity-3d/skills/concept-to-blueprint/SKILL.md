---
name: concept-to-blueprint
description: >-
  Turns a text brief, mood board or concept image into one approved orthographic
  blueprint (front, side, top, three-quarter): four candidate sheets are generated,
  scored and one wins, with a parts list and dimensions in metres for the build.
  Use when the user starts a new 3D asset from an idea, sketch or reference and no
  approved orthos exist, or asks for concept art, a turnaround or a design sheet of
  a prop. Not for building the geometry (use parametric-kitbash), choosing a style
  such as low-poly or hand-painted (use art-direction) or making textures (use
  pbr-material-forge).
---

# Concept to blueprint (ortho Best-of-4)

Output: one winning ortho set (file paths) plus a one-paragraph design note.
No geometry is created in this skill.

## Steps

1. **Pin the brief.** Call `session_status` to read the session's target
   engine and budget. `inspect_manifest` lists only scene nodes, and no tool
   holds a style anchor, so take that from the brief (art bible). Restate the
   asset in one sentence: function, scale in metres, silhouette keywords,
   materials. Ask the user only if the real-world scale or target engine is
   unknown.
2. **Generate four candidates.** The antigravity-3d tool set has no ortho
   generator, so use the platform's `generate_image` tool: four ortho sheets
   (front, side, top, three-quarter; neutral grey background, flat even
   lighting, no perspective), each with a different design seed and the same
   brief and style anchor. Keep candidates as file paths; do not open the
   images in this conversation.
3. **Score the tournament.** No studio tool scores candidate sheets:
   `run_visual_qa` scores only the asset's own renders and takes no arguments.
   Score each candidate with a mean over these criteria: view consistency
   across the four projections, silhouette readability at 64 px, brief
   adherence, style-anchor match, buildability (no impossible overhangs,
   plausible thickness). Use the platform's own image review for this. If it
   is not available, stop and report the candidate paths as unscored.
4. **Pick the winner.** Highest mean score wins; break ties on view
   consistency. If the best score is below 3.0, write a one-line critique per
   loser, fold the critiques into the brief, and run one more round of four.
   Stop after two rounds and show the user the top two side by side.
5. **Record.** Keep the winner's file paths for the hand-off (no studio tool
   stores them) and write the design note: dimensions in metres, key
   proportions, parts list (these become Scene IR node names), material zones.

## Checks before handing off

Run these, in this order, before handing over:

- All four views agree on the major proportions (within about 5%).
- The parts list uses engine-safe names (`[A-Za-z0-9_]+`): check each name
  against that pattern.
- The winner's score is 3.0 or higher, or the user has seen the top two.

A pass is: one set of four file paths, a parts list whose every name matches
the pattern, and a design note with dimensions in metres. Then hand off to
`parametric-kitbash` with the winner and the parts list.

## Gotchas

- **Symptom:** no orthographic blueprint exists for an asset and nothing
  failed. **Cause:** the router of `agy3d prop` decides per brief whether a
  reference drawing is worth an image, and skips it for most briefs
  (`benchmarks/AFTER_THE_COMPARISON.md`, "Open decisions"). **Remedy:** do not
  treat a missing blueprint as an error. Ask the user whether they want one;
  if not, build from the brief.
- **Symptom:** the visual QA that follows never asks about the parts the user
  named. **Cause:** the session was opened without the user's request, and
  the judge only checks parts and depiction against a `brief` it was given.
  **Remedy:** pass the user's words as `brief` to `open_asset_session`
  (`rules/AGENTS.md`).
- **Symptom:** the tournament uses more images than the user expected.
  **Cause:** each round is four images and a second round is four more, and a
  preview session has a hard image limit (`max_images` of `open_asset_session`
  or `start_preview`) that is the user's number, never yours. **Remedy:** tell
  the user the cost (four images a round) before round one, and stop after two
  rounds.

## Version notes

Last verified 2026-11-22 against the tool set at commit 29660b8.

- The antigravity-3d tool set has no ortho generator and no tool that scores
  a candidate sheet; the steps above name the platform tools that fill that
  gap. If a studio tool for either is added, this skill should call it.
- Score threshold 3.0 and the two-round limit are this skill's own, not a
  gate of the studio.
