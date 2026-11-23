# Round seven: the measures, written before any code (2026-11-23, 03:40 UTC)

The owner said "go for it" to: step 1, three.js as a real target; step 2, surfaces written as code and still baked
to textures; and a rewrite of the repository's text around what a developer does with it. Round six taught the
method: say what would count as a pass before building, then report against exactly that.

Assets for both measures: the three packages the script path delivered in round six (crate, hydrant, chair), and
their scripts (`source/build.py`). Stock chair for comparison: the plain-Blender agent's chair of round five, the
one that beats both of our chairs 6 verdicts to 0 today.

## Step 1: three.js as a real target

Fixture projects, made with the standard tools and pinned by lockfile: (a) Vite + three.js, on a three.js version
that is NOT the one this project vendors for its own check; (b) Vite + React + React Three Fiber.

PASS needs all five, on every package in every fixture project:

1. **One command installs it.** `agy3d install PACKAGE PROJECT` (and the plugin's `install_into_project` tool) ends
   with every check passed. What is left for the developer is one import and one call: one hand step, as for Godot.
2. **It renders in the project's own three.js.** In a headless browser, through the loader the install wrote, with
   no console, loader or shader error; the outline agrees with the package's reference picture at the existing web
   check's threshold; every material slot is bound; the lower-detail versions switch with distance.
3. **Smaller on the wire without looking different.** With the compression tool present, the bytes the page
   downloads for the asset are at most half of today's, and the frame rendered from the compressed files differs
   from the frame rendered from the uncompressed files by no more than a tolerance fixed in the contract before the
   first compressed file is made. Without the tool the install still works, ships uncompressed and says which one
   command would enable compression. Nothing is downloaded from the network by the install itself.
4. **Undo.** Uninstall leaves the project byte for byte as it was.
5. **Nothing else moves.** The Godot install, the package and its checks behave as before (their tests untouched).

Reported beside the result, not gates: seconds per install; texture memory on a GPU with block-compressed formats,
computed from the file's format (this machine's software renderer cannot show it, and the report will say so).

FAIL: any cell missing any of the five. Then the report names the cell and the cause, and nothing is built on top.

## Step 2: surfaces

Two layers, measured separately, cheaper one first.

**Layer 1: the built-in surfaces know the part they are on** (which way it runs, how thick it is). Measured with no
authoring call at all: the three round-six scripts are delivered again with the fixed surface step, so the mesh is
identical and only the surface differs.

PASS needs all of:

1. Chair, new surface against its own round-six surface, blind: preferred in at least 5 of 6 verdicts.
2. Crate and hydrant, new against their own round-six surfaces: not worse (at least 3 of 6 each).
3. All eleven game-ready checks still pass on all three.
4. Delivery time within 10 % of round six's for each (the surface step may not become the slow part).

Reported: the chair against the plain-Blender chair (0 of 6 today). Layer 1 is not required to win that alone.

**Layer 2: the agent writes a surface as a function in its script** and gets real inputs for every point (where it
is, which part, which way the part runs, how exposed or tucked away the point is). Measured live: script path,
three briefs, one run each, no generated images.

PASS needs all of:

1. Chair against the plain-Blender chair, blind: at least 4 of 6 verdicts (0 of 6 today).
2. Chair against the Layer 1 chair of the same run's mesh (same script, surface functions removed): preferred in at
   least 4 of 6. If Layer 2 does not beat Layer 1 it is complexity without a gain and stays out.
3. Crate and hydrant against the plain-Blender ones: at least 5 of 6 each (today 6 of 6 and 5 of 5).
4. Eleven of eleven checks on all three.
5. Time to a package at most 1.2 times round six's script path for the same brief (crate 305 s, hydrant 375 s,
   chair 310 s). Fresh tokens are reported and are not a gate (the owner: tokens are not the constraint, waste is).

FAIL of Layer 1: stop, report, do not build Layer 2 on it. FAIL of Layer 2 with Layer 1 passing: keep Layer 1, leave
Layer 2 out, report why.

Honest limits of both: one package per side per comparison, judged six times; the judge is a general model, not a
person; the plain-Blender chair is one run from round five.

## The README and documents

No number. Accepted when: a developer who has never seen the project can tell from the first screen what it does
for them, try it, and find what is and is not verified; every fact the old text stated is still stated somewhere
(moved, not lost); the tests that guard documents still guard them; and the integrator has read every line.

## Fixed at 04:05 UTC, still before any code (details the first version left to the contracts)

- Step 1 assets: the three round-six scripts delivered offline for the new `web_three` target (the engine name in
  the script's `Asset(...)` line is the only change), because a package built for another engine is refused for a
  three.js project, as it is for Godot today.
- Step 1, condition 3, the tolerance: on the web check's 512-pixel beauty frame, same camera and light, compressed
  against uncompressed: mean absolute difference at most 1.5 % of full scale; at most 1 % of the asset's pixels
  differing by more than 8 % of full scale; outline agreement 0.999 or better. If half the bytes cannot be had
  inside that tolerance, the report gives the size at which it holds; neither number is loosened and no texture is
  shrunk to get there.

## Fixed at 04:55 UTC, AFTER one trial number (said openly)

The install engineer's first trial on the crate: 2.2 MB today, 1.04 MB compressed, and a texture transcoder of
0.58 MB that a site downloads once however many assets it has. The first version of condition 3 did not say
whether the transcoder counts. Decided now, with that one number already known:

- Condition 3 is judged on the asset's own files (every level's geometry, the textures once, collision data).
- The report also gives a page that holds this one asset, transcoder included, as a share of today's bytes.
- If that one-asset total is larger than today's highest-level file, compression has failed for that asset,
  whatever the asset-only figure says.

## Layer 1, first attempt: the result (07:34 UTC), and the rule for a second attempt, written before its fix exists

First attempt, measured by the integrator on the merged tree: chair new against old 6 of 6 (met); hydrant 4 of 6
(met); **crate 2 of 6 (not met: the measure needs 3)**; eleven checks on all six packages (met); delivery time
+3 %, +3 %, +5 % (met). Layer 1 fails on the crate, and layer 2 is not built on it. Reported beside it: against
the plain-Blender agent's props the repainted chair wins 5 of 6 (0 of 6 before), the crate 6 of 6, the hydrant
6 of 6.

The cause on the crate was looked for and found in the textures before anything was changed (thin steel plates
treated as thin rods: less edge wear and bevel; grime lightened for every class). It is being fixed at the cause.

Second attempt, fixed now:

- The same four conditions and the same thresholds, on fresh deliveries and fresh verdicts for all three props.
  Nothing of the first attempt is reused, and both attempts are reported side by side.
- If it fails again, layer 1 has failed: the report says so and the owner decides what happens to it.
- A weakness of condition 2 found in the first attempt and NOT changed: on two nearly identical sheets the judge
  picked "image 1" in 5 of 6 verdicts (crate and hydrant alike). With both orders that is 3 of 6, give or take
  one, so "at least 3 of 6" is passed about two times in three even when nothing differs. The report therefore
  also gives, for the crate and the hydrant, how much of each texture differs from round six's, by kind of part.

## Layer 1, second attempt: the result (08:54 UTC)

Fresh deliveries, checks and verdicts for all three props, old code (0edb0ff) against the corrected new code:
chair 6 of 6 (met); crate 3 of 6 (met: the judge calls the two "visually identical" and picks the first image
each time); **hydrant 0 of 6 (not met: the measure needs 3)**; eleven checks on all six packages (met); delivery
time over five alternated rounds +6 %, +2 %, +2 % by the mean (met).

The hydrant's six verdicts: three say the sheets are identical and take the first image, which was the old one;
three, asked with the new one first, prefer the old one by what the judge itself calls a "marginal" or
"negligible" margin on the small hardware (bolts, chain).

By the rule written above before the fix existed, layer 1 has failed its measure twice on "not worse", for a
different prop each time. Layer 2 is not built. What happens to layer 1 is the owner's decision.
