---
name: concept-to-blueprint
description: >-
  Turns a text brief or concept image into an approved orthographic blueprint
  (front, side, top, three-quarter) by running a Best-of-4 ortho tournament and
  picking one winner. Use when the user starts a new 3D asset from an idea,
  mood board or sketch and no approved orthos exist yet, before any blockout.
---

# Concept to blueprint (ortho Best-of-4)

Output: one winning ortho set registered on the session (`ortho_uris`) plus a
one-paragraph design note. No geometry is created in this skill.

## Steps

1. **Pin the brief.** Call `inspect_manifest` to read the session's target
   engine, budget and any style anchor (art bible). Restate the asset in one
   sentence: function, scale in metres, silhouette keywords, materials. Ask
   the user only if the real-world scale or target engine is unknown.
2. **Generate four candidates.** The antigravity-3d tool set has no ortho
   generator, so use the platform's `generate_image` tool: four ortho sheets
   (front, side, top, three-quarter; neutral grey background, flat even
   lighting, no perspective), each with a different design seed and the same
   brief and style anchor. Keep candidates as file paths; do not open the
   images in this conversation.
3. **Score the tournament.** Call `run_visual_qa` on each candidate with the
   rubric `ortho_blueprint`: view consistency across the four projections,
   silhouette readability at 64 px, brief adherence, style-anchor match,
   buildability (no impossible overhangs, plausible thickness). Run the
   four calls in parallel. For a second opinion on the top two, delegate to
   the `lookdev_critic` subagent and keep only its two-line delta.
4. **Pick the winner.** Highest mean score wins; break ties on view
   consistency. If the best score is below 3.0, write a one-line critique per
   loser, fold the critiques into the brief, and run one more round of four.
   Stop after two rounds and show the user the top two side by side.
5. **Register.** Record the winner's URIs as the session's orthos and write
   the design note: dimensions in metres, key proportions, parts list (these
   become Scene IR node names), material zones.

## Checks before handing off

- All four views agree on the major proportions (within about 5%).
- The parts list uses engine-safe names (`[A-Za-z0-9_]+`).
- Hand off to `parametric-kitbash` with the winner and the parts list.
