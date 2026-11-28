# The sites, judged blind on corrected frames

126 verdicts, six on each pair of sites (three in each order), by `gemini-4-carbon` (thinking low) through `benchmarks/site_compare.py`. The judge saw nine desktop frames of each site, at even scroll steps and 960 px wide, as "the first site" and "the second site", with the request in `request.txt`. It never saw which run made which. The frames are those of the corrected site check (no black band or box from the browser's software path; see `../../ROUND_EIGHT.md`).

- `run1`: the first run's site (`../../site_run1/`, the plugin as it was after round seven)
- `run2`: the second run's (`../../site_run2_plugin/`)
- `run3`: the third run's (`../../site_run3_plugin/`)
- `stock`: the stock agent's, the same request without the plugin (`../../site_stock/`)
- `run4`: the fourth run's (`../../site_run4_plugin/`)
- `run5`: the fifth run's (`../../site_run5_plugin/`)
- `run6`: the sixth run's (`../../site_run6_plugin/`)

The model service was busy for much of the day: of the eighteen calls on the first three plugin sites, seventeen were refused at least once (HTTP 503) and asked again over two hours (13:25 to 15:27 UTC); the eighteen calls that involve the stock site were asked in six passes (15:55 to 16:32 UTC), the twenty-four that involve run 4 in two (22:14 to 22:26 UTC), the thirty that involve run 5 in three (00:14 to 00:34 UTC the next day), and the thirty-six that involve run 6 on two days: nine before the service began turning calls away again, twenty-seven two days later. A kept verdict was never asked twice. Run 4's frames were taken with the check as corrected after that run (it waits for a heavy page to arrive before it counts its four seconds); the other sites' frames are the ones taken earlier, which that correction does not change for a page whose every frame was already settled (runs 2 and 3) or that never settles (run 1, the stock agent's). Run 5's and run 6's frames were taken with the same check as run 4's.

## Totals

| pair | criterion | verdicts | result |
|---|---|---|---|
| run1 vs run2 | every position composed | run2 6 | run2 |
| run1 vs run2 | explains the project | run2 6 | run2 |
| run1 vs run2 | craft of the 3d | run2 6 | run2 |
| run1 vs run2 | type and layout | run2 6 | run2 |
| run1 vs run2 | overall | run2 6 | run2 |
| run1 vs run3 | every position composed | run3 6 | run3 |
| run1 vs run3 | explains the project | run3 6 | run3 |
| run1 vs run3 | craft of the 3d | run1 2, run3 4 | run3 |
| run1 vs run3 | type and layout | run3 6 | run3 |
| run1 vs run3 | overall | run3 6 | run3 |
| run1 vs run4 | every position composed | run4 6 | run4 |
| run1 vs run4 | explains the project | run4 6 | run4 |
| run1 vs run4 | craft of the 3d | run1 2, run4 4 | run4 |
| run1 vs run4 | type and layout | run4 6 | run4 |
| run1 vs run4 | overall | run4 6 | run4 |
| run1 vs run5 | every position composed | run5 6 | run5 |
| run1 vs run5 | explains the project | run5 6 | run5 |
| run1 vs run5 | craft of the 3d | run1 3, run5 3 | no majority |
| run1 vs run5 | type and layout | run5 6 | run5 |
| run1 vs run5 | overall | run5 6 | run5 |
| run1 vs run6 | every position composed | run6 6 | run6 |
| run1 vs run6 | explains the project | run6 6 | run6 |
| run1 vs run6 | craft of the 3d | run1 5, run6 1 | run1 |
| run1 vs run6 | type and layout | run6 6 | run6 |
| run1 vs run6 | overall | run6 6 | run6 |
| run1 vs stock | every position composed | stock 6 | stock |
| run1 vs stock | explains the project | run1 3, stock 3 | no majority |
| run1 vs stock | craft of the 3d | run1 6 | run1 |
| run1 vs stock | type and layout | run1 4, stock 2 | run1 |
| run1 vs stock | overall | run1 4, stock 2 | run1 |
| run2 vs run3 | every position composed | run3 6 | run3 |
| run2 vs run3 | explains the project | run2 4, run3 2 | run2 |
| run2 vs run3 | craft of the 3d | run2 4, tie 2 | run2 |
| run2 vs run3 | type and layout | run3 6 | run3 |
| run2 vs run3 | overall | run2 3, run3 3 | no majority |
| run2 vs run4 | every position composed | run4 6 | run4 |
| run2 vs run4 | explains the project | run4 6 | run4 |
| run2 vs run4 | craft of the 3d | run2 6 | run2 |
| run2 vs run4 | type and layout | run4 6 | run4 |
| run2 vs run4 | overall | run4 6 | run4 |
| run2 vs run5 | every position composed | run2 1, run5 5 | run5 |
| run2 vs run5 | explains the project | run5 6 | run5 |
| run2 vs run5 | craft of the 3d | run2 6 | run2 |
| run2 vs run5 | type and layout | run5 6 | run5 |
| run2 vs run5 | overall | run2 1, run5 5 | run5 |
| run2 vs run6 | every position composed | run6 6 | run6 |
| run2 vs run6 | explains the project | run2 5, run6 1 | run2 |
| run2 vs run6 | craft of the 3d | run2 6 | run2 |
| run2 vs run6 | type and layout | run6 6 | run6 |
| run2 vs run6 | overall | run2 3, run6 3 | no majority |
| run2 vs stock | every position composed | run2 6 | run2 |
| run2 vs stock | explains the project | run2 6 | run2 |
| run2 vs stock | craft of the 3d | run2 6 | run2 |
| run2 vs stock | type and layout | run2 6 | run2 |
| run2 vs stock | overall | run2 6 | run2 |
| run3 vs run4 | every position composed | run3 2, run4 4 | run4 |
| run3 vs run4 | explains the project | run4 6 | run4 |
| run3 vs run4 | craft of the 3d | run3 3, run4 3 | no majority |
| run3 vs run4 | type and layout | run3 5, tie 1 | run3 |
| run3 vs run4 | overall | run3 2, run4 4 | run4 |
| run3 vs run5 | every position composed | run3 6 | run3 |
| run3 vs run5 | explains the project | run5 6 | run5 |
| run3 vs run5 | craft of the 3d | run3 3, run5 3 | no majority |
| run3 vs run5 | type and layout | run3 3, tie 3 | no majority |
| run3 vs run5 | overall | run3 3, run5 3 | no majority |
| run3 vs run6 | every position composed | run3 4, run6 2 | run3 |
| run3 vs run6 | explains the project | run3 3, run6 3 | no majority |
| run3 vs run6 | craft of the 3d | run3 3, run6 2, tie 1 | no majority |
| run3 vs run6 | type and layout | run3 2, run6 1, tie 3 | no majority |
| run3 vs run6 | overall | run3 4, run6 2 | run3 |
| run3 vs stock | every position composed | run3 6 | run3 |
| run3 vs stock | explains the project | run3 6 | run3 |
| run3 vs stock | craft of the 3d | run3 6 | run3 |
| run3 vs stock | type and layout | run3 6 | run3 |
| run3 vs stock | overall | run3 6 | run3 |
| run4 vs run5 | every position composed | run4 6 | run4 |
| run4 vs run5 | explains the project | run4 6 | run4 |
| run4 vs run5 | craft of the 3d | run4 4, run5 2 | run4 |
| run4 vs run5 | type and layout | run5 5, tie 1 | run5 |
| run4 vs run5 | overall | run4 6 | run4 |
| run4 vs run6 | every position composed | run4 6 | run4 |
| run4 vs run6 | explains the project | run4 6 | run4 |
| run4 vs run6 | craft of the 3d | run4 3, run6 3 | no majority |
| run4 vs run6 | type and layout | run6 6 | run6 |
| run4 vs run6 | overall | run4 6 | run4 |
| run4 vs stock | every position composed | run4 6 | run4 |
| run4 vs stock | explains the project | run4 6 | run4 |
| run4 vs stock | craft of the 3d | run4 6 | run4 |
| run4 vs stock | type and layout | run4 6 | run4 |
| run4 vs stock | overall | run4 6 | run4 |
| run5 vs run6 | every position composed | run6 6 | run6 |
| run5 vs run6 | explains the project | run5 6 | run5 |
| run5 vs run6 | craft of the 3d | run5 2, run6 4 | run6 |
| run5 vs run6 | type and layout | run6 6 | run6 |
| run5 vs run6 | overall | run5 2, run6 4 | run6 |
| run5 vs stock | every position composed | run5 6 | run5 |
| run5 vs stock | explains the project | run5 6 | run5 |
| run5 vs stock | craft of the 3d | run5 6 | run5 |
| run5 vs stock | type and layout | run5 6 | run5 |
| run5 vs stock | overall | run5 6 | run5 |
| run6 vs stock | every position composed | run6 6 | run6 |
| run6 vs stock | explains the project | run6 6 | run6 |
| run6 vs stock | craft of the 3d | run6 6 | run6 |
| run6 vs stock | type and layout | run6 6 | run6 |
| run6 vs stock | overall | run6 6 | run6 |

## The overall verdict, by which site was shown first

A pair is asked three times in each order so that a judge who favours a position cancels out. Where the two orders disagree, the overall verdict followed the position and not the site.

| pair | first shown | overall verdicts |
|---|---|---|
| run1 vs run2 | run1 | run2 3 |
| run1 vs run2 | run2 | run2 3 |
| run1 vs run3 | run1 | run3 3 |
| run1 vs run3 | run3 | run3 3 |
| run1 vs run4 | run1 | run4 3 |
| run1 vs run4 | run4 | run4 3 |
| run1 vs run5 | run1 | run5 3 |
| run1 vs run5 | run5 | run5 3 |
| run1 vs run6 | run1 | run6 3 |
| run1 vs run6 | run6 | run6 3 |
| run1 vs stock | run1 | run1 1, stock 2 |
| run1 vs stock | stock | run1 3 |
| run2 vs run3 | run2 | run3 3 |
| run2 vs run3 | run3 | run2 3 |
| run2 vs run4 | run2 | run4 3 |
| run2 vs run4 | run4 | run4 3 |
| run2 vs run5 | run2 | run5 3 |
| run2 vs run5 | run5 | run2 1, run5 2 |
| run2 vs run6 | run2 | run2 1, run6 2 |
| run2 vs run6 | run6 | run2 2, run6 1 |
| run2 vs stock | run2 | run2 3 |
| run2 vs stock | stock | run2 3 |
| run3 vs run4 | run3 | run4 3 |
| run3 vs run4 | run4 | run3 2, run4 1 |
| run3 vs run5 | run3 | run5 3 |
| run3 vs run5 | run5 | run3 3 |
| run3 vs run6 | run3 | run3 1, run6 2 |
| run3 vs run6 | run6 | run3 3 |
| run3 vs stock | run3 | run3 3 |
| run3 vs stock | stock | run3 3 |
| run4 vs run5 | run4 | run4 3 |
| run4 vs run5 | run5 | run4 3 |
| run4 vs run6 | run4 | run4 3 |
| run4 vs run6 | run6 | run4 3 |
| run4 vs stock | run4 | run4 3 |
| run4 vs stock | stock | run4 3 |
| run5 vs run6 | run5 | run5 2, run6 1 |
| run5 vs run6 | run6 | run6 3 |
| run5 vs stock | run5 | run5 3 |
| run5 vs stock | stock | run5 3 |
| run6 vs stock | run6 | run6 3 |
| run6 vs stock | stock | run6 3 |

Over all 126 calls the overall verdict went to the site shown second 75 times.

## What the judge said, call by call

**Shown first: run1; second: run2 (repeat 0).** Overall: run2. Site 2 delivers a cohesive scroll-driven 3D workbench that clearly visualizes Straitjacket's mechanics without text collisions or empty transitions.

- every position composed: run2. Site 2 maintains a clear, continuous camera path across the workbench in frames 1-9 without overlapping the 3D subjects, whereas Site 1 obscures models behind scrolling cards in frames 2-3 and leaves frame 6 empty.
- explains the project: run2. Site 2 directly visualizes data reduction, needle preservation, and routing via physical blocks and floor labels in frames 2-8, while Site 1 relies more on abstract sci-fi props like in frame 7.
- craft of the 3d: run2. Site 2 grounds its textured industrial machines on a shadowed floor grid across frames 1-9, whereas Site 1 floats shiny assets in dark space with harsh specular highlights as seen in frames 1-5.
- type and layout: run2. Site 2 uses a compact, consistent left-hand card across frames 1-9 that gives the 3D scene ample room, while Site 1 uses oversized scrolling panels that clip at viewport edges in frames 3, 6, and 7 and crowd out the 3D in frames 8-9.

**Shown first: run1; second: run2 (repeat 1).** Overall: run2. Site 2 delivers a cohesive, well-paced scroll experience where grounded 3D machinery clearly illustrates how Straitjacket processes and routes agent context without obstructing the view.

- every position composed: run2. Site 2 maintains a clear, continuous camera path across the floor grid in frames 1-9 without overlapping the 3D subject, whereas Site 1 obscures its 3D models behind cards in frames 2-3 and leaves an empty void in frame 6.
- explains the project: run2. Site 2 concretely visualizes data compression, needle preservation, and host routing through labeled spatial machinery in frames 3, 4, and 8, whereas Site 1's machinery in frames 1-7 acts more as abstract sci-fi decoration alongside interactive UI.
- craft of the 3d: run2. Site 2 grounds its tactile machinery with contact shadows on a unified workbench grid in frames 1-9, while Site 1's metallic objects float ungrounded in dark space in frames 5 and 7.
- type and layout: run2. Site 2 keeps compact, legible cards consistently anchored to the left in frames 1-9 to give the 3D scene breathing room, whereas Site 1's oversized scrolling panels crowd the viewport and clip at the edges in frames 3, 6, and 8.

**Shown first: run1; second: run2 (repeat 2).** Overall: run2. Site 2 delivers a cohesive, grounded scroll narrative where clear typography and continuous 3D machinery work together to visually explain Straitjacket's mechanics.

- every position composed: run2. Site 2 keeps its 3D subjects cleanly framed across frames 1–9 with continuous camera travel, whereas Site 1 obscures the 3D with text in frames 2–3 and leaves frames 6, 8, and 9 mostly empty.
- explains the project: run2. Site 2 visually demonstrates data compression, quiet needle preservation, and routing via labeled 3D blocks and machines in frames 3, 4, and 8, whereas Site 1's objects in frames 1, 5, and 7 act more as abstract props.
- craft of the 3d: run2. Site 2 grounds its tactile machinery with ambient shadows on a continuous workbench plane in frames 1–9, while Site 1's metallic assets float in dark space with harsh specular highlights in frames 1, 5, and 7.
- type and layout: run2. Site 2 maintains a consistent, scannable left-aligned card in frames 1–9 that leaves ample room for the 3D scene, while Site 1's dense, shifting panels clip at viewport edges in frames 3, 6, and 7 and crowd out the 3D in frames 8–9.

**Shown first: run2; second: run1 (repeat 0).** Overall: run2. Site 1 better fulfills the request through cohesive scroll choreography, grounded 3D craft, and a clear visual metaphor that directly explains Straitjacket's workflow.

- every position composed: run2. Site 1 maintains a clear, continuous camera path across the floor grid from frames 1 to 9 without obscuring the 3D subjects, whereas Site 2 has empty 3D space in frame 6 and obscured or absent 3D subjects in frames 3, 8, and 9.
- explains the project: run2. Site 1 physically illustrates token floods, the preserved needle in frame 3, compression in frame 4, and harness routing in frame 8 as a literal assembly line, whereas Site 2's chambers and nodes in frames 1, 5, and 7 are more abstract.
- craft of the 3d: run2. Site 1 grounds its machinery on a continuous blueprint floor with cohesive lighting, bevels, and contact shadows across frames 1 to 9, while Site 2's metallic props float ungrounded in dark voids in frames 5 and 7.
- type and layout: run2. Site 1 uses compact, consistently positioned left-hand cards from frames 1 to 9 that leave ample breathing room for the 3D stage, whereas Site 2's oversized cards clip at viewport edges in frames 3, 6, and 7 and crowd out the scene in frames 8 and 9.

**Shown first: run2; second: run1 (repeat 1).** Overall: run2. Site 1 delivers a cohesive, well-composed scroll journey with grounded 3D assembly-line visuals that clearly explain how Straitjacket processes and routes agent context.

- every position composed: run2. Site 1 frames 1 to 9 keep the 3D pipeline clearly framed alongside a fixed left card, whereas Site 2 has empty space in frame 6 and obscured 3D in frames 8 and 9.
- explains the project: run2. Site 1 visually tracks log compression and routing across connected machines in frames 3 to 8, while Site 2's chambers in frames 1 to 7 are more abstract.
- craft of the 3d: run2. Site 1 grounds its machinery with contact shadows on a unified grid across frames 1 to 9, whereas Site 2's metallic props float ungrounded in frames 5 and 7.
- type and layout: run2. Site 1 maintains clean, consistent left-side cards in frames 1 to 9, while Site 2 clips text panels at viewport edges in frames 3, 6, and 7.

**Shown first: run2; second: run1 (repeat 2).** Overall: run2. Site 1 better fulfills the request with a continuous, grounded 3D assembly line that visually explains Straitjacket's mechanics without obstructing the scene during scroll.

- every position composed: run2. Site 1 maintains clean framing across all 9 frames by pinning a compact card to the left, whereas Site 2 hides its 3D scene or leaves empty dead zones in frames 3, 6, 8, and 9.
- explains the project: run2. Site 1 concretely maps blocks through connected factory stations from raw flood (frame 3) to digest (frame 4) and gates (frame 8), while Site 2 relies on more abstract floating sci-fi props.
- craft of the 3d: run2. Site 1 grounds its tactile machinery onto a shadowed blueprint floor across frames 1 to 9, whereas Site 2 floats metallic props in dark space.
- type and layout: run2. Site 1 keeps readable, uniform cards in a dedicated left column from frame 1 to 9, avoiding the clipped cards and wide text overlays seen in Site 2's frames 3, 6, 8, and 9.

**Shown first: run1; second: run3 (repeat 0).** Overall: run3. Site 2 better fulfills the request by pairing a clean, unobstructed scroll layout with grounded, literal 3D visual metaphors that clearly explain how Straitjacket processes and hands off context.

- every position composed: run3. Site 2 keeps its subjects clearly framed across frames 1 to 9, whereas Site 1 obscures its 3D models behind tall text panels in frames 2, 3, and 8 and leaves frame 6 empty.
- explains the project: run3. Site 2 visually demonstrates log compression, storage, and line anchoring with discrete block stacks in frames 3 to 8, while Site 1 uses more abstract machinery and particles in frames 1 to 7.
- craft of the 3d: run3. Site 2 grounds its textured models with contact shadows on a continuous grid across frames 1 to 9, whereas Site 1's glossy assets float in dark space in frames 5 and 7.
- type and layout: run3. Site 2 uses a compact, consistent top-left card across frames 1 to 9 that preserves 3D space, while Site 1's oversized cards overlap the scene and clip at viewport edges in frames 3, 6, and 7.

**Shown first: run1; second: run3 (repeat 1).** Overall: run3. Site 2 delivers a cleaner, better-composed scroll experience where grounded 3D machinery directly visualizes how Straitjacket processes, stores, and hands off code context.

- every position composed: run3. Site 2 keeps the 3D pipeline clearly framed in frames 1 through 9, whereas Site 1 obscures models with UI cards in frames 2 and 3 and leaves frame 6 empty.
- explains the project: run3. Site 2 physically visualizes log compression, storage, and line anchoring via discrete block stacks across frames 3 to 9, making the data pipeline much clearer than Site 1's abstract particles.
- craft of the 3d: run3. Site 2's machinery sits grounded with contact shadows on a cohesive grid across frames 1 to 9, while Site 1's assets float disconnectedly in black space in frames 5 and 7.
- type and layout: run3. Site 2 uses a compact, consistent top-left card across all 9 frames that leaves ample room for the 3D scene, while Site 1's massive scrolling panels crowd the viewport in frames 3, 8, and 9.

**Shown first: run1; second: run3 (repeat 2).** Overall: run3. Site 2 better fulfills the request through a cohesive, grounded 3D assembly line that visually explains Straitjacket's mechanics while maintaining clean layout and scroll composition.

- every position composed: run3. Site 2 keeps its subjects clearly framed across frames 1-9, whereas Site 1 overlaps text onto the 3D subject in frames 2-3 and leaves frame 6 empty.
- explains the project: run3. Site 2 uses literal metaphors like stacked log blocks, a filing cabinet, and shifting line stacks in frames 3-8 to show data reduction and anchoring, while Site 1 relies on abstract machinery.
- craft of the 3d: run3. Site 2 grounds its stylized machinery with contact shadows on a continuous grid across frames 1-9, while Site 1's metallic assets float ungrounded in space in frames 5 and 7.
- type and layout: run3. Site 2 anchors concise, readable cards in the top-left across frames 1-9, whereas Site 1 uses oversized cards that clip at viewport edges in frames 3, 6, and 7 and crowd out the 3D scene in frames 8-9.

**Shown first: run3; second: run1 (repeat 0).** Overall: run3. Site 1 delivers a cleaner, uninterrupted scroll-driven 3D visualization that directly clarifies Straitjacket's architecture without visual clutter.

- every position composed: run3. Site 1 keeps the 3D pipeline unobstructed across frames 1-9, whereas Site 2 hides models behind overlapping text cards in frames 2-4 and leaves frame 6 empty.
- explains the project: run3. Site 1 clearly maps log compression and storage using distinct stacks and stations in frames 3-8, while Site 2's chambers and particles in frames 2-4 are more abstract.
- craft of the 3d: run3. Site 1 grounds its cohesive stations with shadows on a continuous floor grid across frames 1-9, whereas Site 2's metallic props float ungrounded in space in frames 5 and 7.
- type and layout: run3. Site 1 uses compact, fixed-position cards across frames 1-9 that leave ample breathing room, while Site 2's oversized, scrolling panels clutter the viewport in frames 2-3 and 8-9.

**Shown first: run3; second: run1 (repeat 1).** Overall: run3. Site 1 better fulfills the request with cohesive scroll choreography and an intuitive visual metaphor across all frames, avoiding Site 2's layout occlusions and empty transitions.

- every position composed: run3. Site 1 keeps the 3D pipeline unobstructed from frames 1 to 9, whereas Site 2 hides models behind cards in frames 2-3 and leaves frame 6 nearly empty.
- explains the project: run3. Site 1 clearly visualizes log compression, storage, and anchoring as a connected physical assembly line across frames 3-9, outperforming Site 2's abstract particle chambers.
- craft of the 3d: run1. Site 2 displays superior metallic materials, reflections, and volumetric lighting in frames 1-4 and 7 compared to Site 1's flatter shading and basic block stacks.
- type and layout: run3. Site 1 anchors concise cards to the top-left across all 9 frames to give the 3D scene room, while Site 2 uses oversized scrolling panels that crowd frames 3, 6, 8, and 9.

**Shown first: run3; second: run1 (repeat 2).** Overall: run3. Site 1 delivers a cleaner scroll-driven visualization with unobstructed compositions and clearer visual metaphors that directly explain how Straitjacket works.

- every position composed: run3. Site 1 keeps the 3D subjects unobstructed across frames 1 to 9, whereas Site 2 clips text cards and leaves an empty gap in frame 6.
- explains the project: run3. Site 1 clearly maps log compression and storage using block stacks across frames 3 to 8, while Site 2 relies on abstract particles and rings.
- craft of the 3d: run1. Site 2 displays superior metallic shaders, specular reflections, and volumetric lighting in frames 1 to 7 compared to Site 1's flatter matte models.
- type and layout: run3. Site 1 anchors concise, readable cards consistently on the left in frames 1 to 9, avoiding the clipped panels and dense grids seen in Site 2's frames 3, 6, and 9.

**Shown first: run1; second: run4 (repeat 0).** Overall: run4. Site 2 delivers a true scroll-driven 3D visualization where every camera stop is well-composed and directly illustrates how Straitjacket processes and routes agent context.

- every position composed: run4. Site 2 keeps the 3D factory line cleanly framed across all 9 frames, whereas Site 1 hides the 3D subject behind text cards in frames 2 and 3 and leaves frame 6 empty.
- explains the project: run4. Site 2 concretely visualizes data compression, filing, line-anchor clamping, and 7-lane routing in frames 3 through 8, while Site 1's machines in frames 1, 5, and 7 remain mostly abstract.
- craft of the 3d: run4. Site 2 grounds its cohesive mechanical line on a shadowed grid floor across frames 1 to 9, whereas Site 1's disconnected props float in dark space with harsh metallic highlights in frames 4 and 7.
- type and layout: run4. Site 2 keeps concise, readable cards pinned cleanly to the top-left in frames 1 through 9, while Site 1's tall scrolling panels collide with the 3D scene and cover the viewport in frames 3, 8, and 9.

**Shown first: run1; second: run4 (repeat 1).** Overall: run4. Site 2 delivers a cohesive, unobstructed scroll journey down a mechanical pipeline that clearly visualizes how Straitjacket compresses, stores, anchors, and routes agent evidence.

- every position composed: run4. Site 2 keeps the 3D factory line unobstructed across frames 1-9, whereas Site 1 covers its 3D models with text cards in frames 2-3 and leaves frame 6 empty.
- explains the project: run4. Site 2 concretely visualizes log compression, storage drawers, line-anchor calipers, and 7 harness lanes across frames 3-8, whereas Site 1 uses more abstract sci-fi props.
- craft of the 3d: run4. Site 2 grounds its cohesive mechanical line on a shadowed floor grid across frames 1-9, while Site 1 floats disconnected metallic props in empty space in frames 5-7.
- type and layout: run4. Site 2 maintains a clean, fixed top-left card across frames 1-9 that leaves room for the 3D scene, while Site 1 crowds the viewport with tall scrolling panels in frames 3 and 8-9.

**Shown first: run1; second: run4 (repeat 2).** Overall: run4. Site 2 delivers a smoother scroll-driven camera path along a continuous 3D machine that directly visualizes how Straitjacket compresses logs, anchors lines, and routes across harnesses.

- every position composed: run4. Site 2 keeps its assembly line unobstructed across frames 1–9, whereas Site 1 hides 3D subjects behind text cards in frames 2–3 and leaves frame 6 empty.
- explains the project: run4. Site 2 visibly compresses a massive red slab in frame 3, stores slices in frame 5, and clamps shifted lines in frame 6, making the mechanics clearer than Site 1's abstract devices.
- craft of the 3d: run4. Site 2 grounds a cohesive industrial assembly line on a lit floor grid in frames 1–9, while Site 1's isolated objects float in dark space in frames 5 and 7.
- type and layout: run4. Site 2 pins concise cards cleanly to the top-left across frames 1–9, whereas Site 1's tall scrolling panels clip against headers and cover the 3D scene in frames 3, 7, and 8.

**Shown first: run4; second: run1 (repeat 0).** Overall: run4. Site 1 delivers a cohesive, smooth scroll journey along a grounded 3D assembly line that clearly visualizes how Straitjacket processes and hands off evidence without layout collisions.

- every position composed: run4. Site 1 maintains a clear, unobstructed composition across all frames 1–9, whereas Site 2 covers its 3D subject with text cards in frames 2 and 3 and leaves frame 6 mostly empty.
- explains the project: run4. Site 1 visually demonstrates the workflow as a continuous assembly line—compressing logs in frame 3, filing handles in frame 5, and anchoring line shifts in frame 6—more legibly than Site 2's disconnected sci-fi props.
- craft of the 3d: run4. Site 1's machinery sits grounded on a continuous perspective floor grid with cohesive shadows and forms from frame 1 to 9, while Site 2's metallic models float disconnectedly in dark space as seen in frames 5 and 7.
- type and layout: run4. Site 1 uses a consistent, compact top-left card across frames 1–9 that leaves ample room for the 3D scene, while Site 2's scrolling panels clip at viewport edges in frames 3, 6, and 7 and crowd out the 3D in frames 8 and 9.

**Shown first: run4; second: run1 (repeat 1).** Overall: run4. Site 1 delivers a cohesive, unobstructed scroll-driven visualization that clearly explains how Straitjacket bounds logs, anchors edits, and routes across harnesses.

- every position composed: run4. Site 1 keeps the 3D factory line cleanly framed on the right from frame 1 to 9, whereas Site 2 covers the 3D subject with scrolling UI cards in frames 2 and 3 and leaves frame 6 nearly empty.
- explains the project: run4. Site 1 visually demonstrates the workflow as a continuous machine—pressing the log block in frame 3, storing slices in frame 5, and clamping shifted lines in frames 6 and 7.
- craft of the 3d: run1. Site 2 features richer metallic materials, specular highlights, and emissive particle streams in frames 1 through 5, though Site 1 is better grounded on its floor grid.
- type and layout: run4. Site 1 anchors compact, legible cards consistently on the top-left across frames 1 to 9, avoiding the cut-off text boxes and visual clutter seen in Site 2's frames 3, 6, and 7.

**Shown first: run4; second: run1 (repeat 2).** Overall: run4. Site 1 delivers a cohesive, unbroken scroll journey with an intuitive visual metaphor that clearly explains how Straitjacket works without layout collisions.

- every position composed: run4. Site 1 keeps the 3D factory line cleanly framed across frames 1 to 9, whereas Site 2 has text overlapping the 3D subject in frame 3 and an empty 3D scene in frames 6 and 9.
- explains the project: run4. Site 1 concretely visualizes log compression, storage drawers, and line-shift anchors in frames 3, 5, and 6, while Site 2's chambers and particles in frames 1 to 4 feel more abstract.
- craft of the 3d: run1. Site 2's metallic chambers and calipers in frames 1, 4, and 5 feature richer materials, specular highlights, and depth than Site 1's flatter, unlit-looking blocks in frames 6 and 7.
- type and layout: run4. Site 1 maintains a consistent, readable top-left card across frames 1 to 9 that gives the 3D scene room, unlike Site 2's shifting panels that clip at viewport edges in frames 3, 6, and 7.

**Shown first: run1; second: run5 (repeat 0).** Overall: run5. Site 2 better fulfills the request by pairing smooth, unobstructed scroll choreography with a clear visual metaphor that genuinely explains how Straitjacket processes and hands off agent context.

- every position composed: run5. Site 2 keeps its workbench stages framed cleanly alongside left-aligned cards across frames 1–9, whereas Site 1 obscures its 3D subject behind cards in frames 2–3 and leaves frame 6 nearly empty.
- explains the project: run5. Site 2's connected press, filing cabinet, and relocator in frames 3–7 concretely illustrate log compression, storage, and anchor tracking, whereas Site 1's isolated sci-fi props in frames 1–7 feel more abstract.
- craft of the 3d: run1. Site 1 exhibits richer metallic materials, specular highlights, and depth on its chambers and calipers in frames 1, 4, and 5 compared to the flatter, matte textures of Site 2's models in frames 1–9.
- type and layout: run5. Site 2 uses concise, consistently positioned cards in frames 1–9 that leave ample room for the 3D scene, while Site 1's oversized, dense panels crowd the viewport and cover the 3D in frames 2, 3, and 8–9.

**Shown first: run1; second: run5 (repeat 1).** Overall: run5. Site 2 delivers a smoother, well-composed scroll journey that clearly visualizes how Straitjacket processes logs and anchors.

- every position composed: run5. Site 2 maintains clear framing across frames 1-9, whereas Site 1 obscures models behind UI cards in frames 2-3 and leaves frame 6 nearly empty.
- explains the project: run5. Site 2's connected workbench assembly line in frames 3-8 visually traces log compression, storage, and anchor relocation much clearer than Site 1's abstract sci-fi props.
- craft of the 3d: run1. Site 1 features richer metallic materials, specular reflections, and volumetric lighting in frames 1-5 compared to Site 2's flatter, matte-shaded blocks.
- type and layout: run5. Site 2 uses concise left-aligned cards across frames 1-9 that give the 3D scene breathing room, while Site 1's oversized scrolling panels crowd the viewport in frames 3 and 8-9.

**Shown first: run1; second: run5 (repeat 2).** Overall: run5. Site 2 delivers a far more cohesive scroll choreography and an intuitive visual metaphor that genuinely helps a visitor see how Straitjacket compresses logs, stores artifacts, and coordinates harnesses without text crowding out the 3D stage.

- every position composed: run5. Site 2 keeps its 3D workbench cleanly framed on the right across frames 1 to 9 with smooth camera travel, whereas Site 1 obscures its 3D chamber behind scrolling cards in frames 2 and 3 and leaves an almost empty scene in frame 6.
- explains the project: run5. Site 2 turns the pipeline into a clear visual diagram—compressing a massive red-and-grey log slab through a press into a small digest with a yellow needle in frames 3 and 4, filing it in a cabinet in frame 5, and routing to seven harness lanes in frame 8.
- craft of the 3d: run1. Site 1 exhibits richer metallic and emissive materials, specular highlights, and dynamic lighting on its chamber, caliper, and router in frames 1, 5, and 7, while Site 2 relies on flatter shading and simple colored blocks.
- type and layout: run5. Site 2 uses concise, well-proportioned cards pinned cleanly to the top-left across all 9 frames to give the 3D scene room, while Site 1 crowds the viewport with tall, dense panels that clip at the edges in frames 3, 6, and 7 and hide the 3D entirely in frames 8 and 9.

**Shown first: run5; second: run1 (repeat 0).** Overall: run5. Site 1 better fulfills the request through cohesive scroll choreography, clear spatial layout, and an intuitive mechanical metaphor that directly illustrates how Straitjacket works.

- every position composed: run5. Site 1 maintains clear framing across frames 1-9, whereas Site 2 suffers from overlapping text on the 3D subject in frame 3 and an empty 3D background in frame 6.
- explains the project: run5. Site 1 concretely visualizes log compression and retrieval via physical press and filing metaphors in frames 3-6, while Site 2 uses more abstract sci-fi machinery in frames 1-4.
- craft of the 3d: run5. Site 1 grounds its textured industrial workbench on a continuous floor grid across frames 1-9, whereas Site 2's metallic props float ungrounded in dark space in frames 5 and 7.
- type and layout: run5. Site 1 anchors concise cards cleanly on the left in frames 1-9, avoiding the clipped cards and dense walls of text seen in Site 2's frames 3, 6, 8, and 9.

**Shown first: run5; second: run1 (repeat 1).** Overall: run5. Site 1 delivers a cohesive, well-composed 3D workbench narrative that clearly visualizes how Straitjacket processes logs and anchors code without crowding the screen.

- every position composed: run5. Site 1 maintains clear framing across frames 1-9, whereas Site 2 has text occluding the 3D subject in frame 3 and an empty 3D space in frame 6.
- explains the project: run5. Site 1 visually traces a single log slab through compression, storage, and anchor relocation across frames 3-8, making the data pipeline much clearer than Site 2's disconnected sci-fi props.
- craft of the 3d: run5. Site 1 grounds its textured industrial machines on a continuous floor grid with cohesive shadows in frames 1-9, while Site 2's shiny objects float disjointedly in dark space as seen in frames 5 and 7.
- type and layout: run5. Site 1 uses a consistent left-pinned card in frames 1-9 that leaves generous breathing room for the 3D workbench, unlike Site 2's oversized scrolling cards that clip and crowd the viewport in frames 3, 6, and 7.

**Shown first: run5; second: run1 (repeat 2).** Overall: run5. Site 1 delivers a cohesive, Scrolltide-style guided tour along a grounded 3D workbench where every scroll stop is cleanly composed and directly visualizes how Straitjacket processes, stores, and routes evidence.

- every position composed: run5. Site 1 keeps the active 3D workstation cleanly framed on the right across all 9 frames (with minor text-card overlap only on secondary background pieces in frames 5-6), whereas Site 2 loses its 3D subject entirely in frames 6 and 9 and clips oversized text cards across frames 2, 3, 6, and 7.
- explains the project: run5. Site 1 traces a continuous physical assembly line—from the massive raw log slab entering the Capture Press (frames 3-4) into the Local Store cabinet (frame 5), Anchor Relocator (frame 7), and Host Lanes (frame 8)—making the mechanics visually intuitive compared to Site 2's disconnected sci-fi props.
- craft of the 3d: run5. Site 1 grounds its weathered industrial machines on a cohesive gridded workbench with shadows and physical connecting lines across frames 1-9, while Site 2's metallic props float ungrounded in dark space with noisy particle effects in frames 1-5.
- type and layout: run5. Site 1 uses a disciplined, compact left-hand card that updates cleanly from frames 1 to 9 while leaving generous room for the 3D scene, whereas Site 2's oversized cards crowd the viewport, get sliced off at the top and bottom edges (frames 3, 6, 7), and cover the entire screen in frames 8-9.

**Shown first: run1; second: run6 (repeat 0).** Overall: run6. Site 2 delivers a cleaner scroll-driven choreography where grounded 3D machinery and restrained UI cards work together to clearly visualize how Straitjacket compresses context and routes work.

- every position composed: run6. Site 2 keeps its 3D subject cleanly framed across all steps (e.g., frames 4-8), whereas Site 1 covers models with text in frames 2-3 and leaves an empty gap in frame 6.
- explains the project: run6. Site 2 physically visualizes token compression and routing via an assembly line press and lanes in frames 3-8, whereas Site 1's machines in frames 1-7 feel more abstract.
- craft of the 3d: run6. Site 2 grounds its models on a shadowed grid floor with cohesive matte industrial materials across frames 1-9, while Site 1's glossy objects float ungrounded in dark space (frames 5, 7).
- type and layout: run6. Site 2 pins compact, scannable cards to the top-left (frames 1-3, 5-6, 8-9) leaving generous room for the 3D scene, whereas Site 1's oversized scrolling panels crowd and clip the viewport in frames 3, 6, and 8.

**Shown first: run1; second: run6 (repeat 1).** Overall: run6. Site 2 better fulfills the request for a smooth, explanatory scroll experience by maintaining clean compositions at every step and unifying Straitjacket's mechanics into a single, legible 3D system.

- every position composed: run6. Site 2 keeps its 3D assembly centered and unobstructed across frames 1-9, whereas Site 1 obscures the 3D subject with overlapping text cards in frames 2-3 and leaves an empty gap in frame 6.
- explains the project: run6. Site 2 visually connects raw log slabs, a digest press, an anchor rack, and seven host lanes into one continuous machine across frames 1-8, making the workflow clearer than Site 1's isolated objects in frames 1, 5, and 7.
- craft of the 3d: run1. Site 1 exhibits richer metallic shaders, emissive lighting, and particle depth in frames 1-5 compared to the flatter materials on Site 2's models in frames 4-7, though Site 2 grounds its shadows well on the grid.
- type and layout: run6. Site 2 places concise, scannable cards consistently in the top-left in frames 1-3, 5-6, and 8-9, avoiding the cramped multi-column text blocks and clipped card transitions seen in Site 1's frames 3, 6, and 8-9.

**Shown first: run1; second: run6 (repeat 2).** Overall: run6. Site 2 better fulfills the request by seamlessly pairing clean scroll choreography with a clear, literal 3D mechanical visualization of how Straitjacket processes logs, anchors, and harnesses.

- every position composed: run6. Site 2 keeps its 3D apparatus clearly framed and unobstructed across all frames 1-9, whereas Site 1 hides its 3D subject behind cards in frames 2-3 and leaves frame 6 empty of 3D.
- explains the project: run6. Site 2 visually demonstrates token compression, anchor storage, and host routing via physical factory mechanics in frames 3-8, whereas Site 1 relies more on abstract sci-fi machinery in frames 1-4 and 7.
- craft of the 3d: run1. Site 1 displays richer metallic materials, specular highlights, and emissive lighting in frames 1, 4, and 7 compared to the flatter, matte-shaded models on a plain grid in Site 2's frames 1-9.
- type and layout: run6. Site 2 anchors concise, readable cards consistently in the top-left across frames 1-3, 5-6, and 8-9, while Site 1 uses tall, scrolling cards that clip at viewport edges and overlap the 3D in frames 2-3 and 6-9.

**Shown first: run6; second: run1 (repeat 0).** Overall: run6. Site 1 delivers a cohesive, Scrolltide-like spatial journey where well-proportioned typography and clear mechanical metaphors work together at every scroll step to visually explain how Straitjacket tames context floods.

- every position composed: run6. Site 1 keeps its 3D machinery cleanly framed on the right across frames 1 to 9, whereas Site 2 hides its 3D behind giant scrolling cards in frames 2 and 3 and leaves frames 6 and 9 nearly empty of 3D.
- explains the project: run6. Site 1 physically illustrates the data pipeline—showing the massive token pile in frame 3 compressed by a press into a tiny handle on a conveyor belt in frame 5—more clearly than Site 2's abstract sci-fi rings and calipers.
- craft of the 3d: run1. Site 2's models in frames 1, 5, and 7 feature richer metallic materials, specular highlights, emissive cores, and particle shaders compared to Site 1's flatter matte-plastic aesthetic.
- type and layout: run6. Site 1 uses compact, consistent cards on the upper left (frames 1-3, 5-6, 8-9) that give the 3D scene breathing room, while Site 2's oversized, shifting panels clip off-screen and crowd the viewport in frames 2, 3, 6, and 8.

**Shown first: run6; second: run1 (repeat 1).** Overall: run6. Site 1 delivers a cohesive, unobstructed scroll-driven visualization that clearly illustrates Straitjacket's workflow without layout collisions.

- every position composed: run6. Site 1 maintains clear framing throughout frames 1–9, whereas Site 2 suffers from overlapping text in frame 3 and misses the 3D subject in frames 6 and 9.
- explains the project: run6. Site 1 uses a literal factory assembly line across frames 3, 5, 6, and 8 to visually map how logs are compressed, anchored, and routed.
- craft of the 3d: run1. Site 2 features richer metallic shaders, specular reflections, and particle lighting in frames 1–4 and 7 compared to Site 1's flatter matte materials.
- type and layout: run6. Site 1 keeps concise text cards anchored cleanly on the left in frames 1–3, 5, 6, 8, and 9, avoiding the oversized panels and text cutoffs seen in Site 2's frames 3, 6, and 7.

**Shown first: run6; second: run1 (repeat 2).** Overall: run6. Site 1 better fulfills the request for a smooth, visual scroll experience by keeping every scroll step cleanly composed and translating Straitjacket's workflow into a single, legible 3D machine.

- every position composed: run6. Site 1 keeps the 3D scene framed cleanly beside pinned text cards across frames 1–9, whereas Site 2 has text cards blocking the 3D model in frames 2–3 and leaves empty or obscured backgrounds in frames 6 and 8–9.
- explains the project: run6. Site 1 visualizes a cohesive mechanical pipeline where a giant log block (frame 3) is compressed by a press into a tiny handle on a conveyor (frame 5) and stored in an anchor rack (frame 6), making the mechanics intuitive.
- craft of the 3d: run1. Site 2 shows richer metallic shaders, specular reflections, and glowing elements in frames 1–5 and 7, giving its objects stronger material presence than Site 1's flatter matte models.
- type and layout: run6. Site 1 uses concise, pinned left-hand cards in frames 1–3, 5–6, and 8–9 that leave generous breathing room for the 3D scene, while Site 2's scrolling panels clip mid-frame in frames 3, 6, and 7 and crowd the viewport in frames 8–9.

**Shown first: run1; second: stock (repeat 0).** Overall: run1. Site 1 delivers higher-craft, grounded 3D hardware models paired with cleaner typography and interactive controls that visualize Straitjacket's pipeline without layout collisions.

- every position composed: stock. Site 2 keeps a clear 3D subject framed across frames 1-8 without empty travel gaps like Site 1's frame 6, though Site 2's hero text slightly overlaps the emblem in frame 1.
- explains the project: stock. Site 2's 3D diagrams in frames 3, 6, and 7 directly illustrate the vault pipeline, frozen prefix, and 7-harness delegation, whereas Site 1's props in frames 1-5 feel more abstract.
- craft of the 3d: run1. Site 1's models in frames 1-5 and 7 show richer mechanical detail, metallic surface variation, and grounded base plates compared to Site 2's simpler floating primitives in frames 3-6.
- type and layout: run1. Site 1 keeps text cleanly contained in crisp dark cards in frames 1, 4, and 5 without colliding with the 3D subject like Site 2's hero headline in frame 1 or top bar overlap in frame 5.

**Shown first: run1; second: stock (repeat 1).** Overall: stock. Site 2 delivers a more cohesive, uninterrupted scroll journey whose 3D diagrams directly visualize Straitjacket's store, gates, prefix caching, and multi-harness handoffs.

- every position composed: stock. Site 2 keeps a distinct 3D subject framed at every scroll step from frame 1 through frame 8, whereas Site 1 leaves an empty 3D void in frame 6 and has cards colliding with the central chamber in frames 2 and 3.
- explains the project: stock. Site 2 builds legible diagrams for each concept—such as the vault funnel into four rings in frame 3, the frozen prefix blocks in frame 6, and the seven harness pods passing signals in frame 7—while Site 1's machinery is more abstract.
- craft of the 3d: run1. Site 1's mechanical assemblies in frames 1, 4, 5, and 7 have detailed multi-part modeling, worn metallic materials, and grounded bases, compared to Site 2's simpler floating primitives in frames 1 and 5.
- type and layout: stock. Despite minor text-over-3D overlap in frame 1, Site 2 uses cleaner typographic hierarchy, calmer card spacing, and atmospheric color shifts (frames 2, 3, 7) compared to Site 1's tall, dense UI cards.

**Shown first: run1; second: stock (repeat 2).** Overall: stock. Site 2 better answers the request to visualize how Straitjacket works through a cohesive scroll journey where every stop pairs clear, readable layout with a purposeful 3D diagram of the architecture.

- every position composed: stock. Site 2 keeps a distinct 3D subject framed at almost every scroll step (frames 1 through 8), whereas Site 1 leaves frame 6 completely empty of 3D objects during transition and overlaps text cards directly over the 3D chamber in frames 2 and 3.
- explains the project: stock. Site 2 builds clear visual diagrams for each system stage—such as the vault and four lifecycle gates in frame 3, frozen prefix blocks in frame 6, and seven connected harness pods in frame 7—while Site 1's machines in frames 1, 5, and 7 are more abstract mechanical props.
- craft of the 3d: run1. Site 1's models in frames 1, 4, 5, and 7 look physically constructed and grounded on bases with worn metallic bevels, fasteners, and specular depth, whereas Site 2's objects in frames 1, 5, and 8 look like simpler geometric primitives floating against a flat grid.
- type and layout: stock. Site 2 uses cleaner typography, crisper hierarchy, and narrower cards that leave generous breathing room around the 3D scenes in frames 2, 3, 6, and 7, although frame 1 slightly overlaps the hero headline with the logo.

**Shown first: stock; second: run1 (repeat 0).** Overall: run1. Site 2 delivers higher-craft mechanical 3D assets and clearer interactive metaphors that directly visualize how Straitjacket works.

- every position composed: stock. Site 1 maintains a subject in frames 1 through 8 without completely empty transitions like Site 2's frame 6.
- explains the project: run1. Site 2's intake chamber, caliper, and router in frames 2, 5, and 7 tangibly demonstrate token filtering, line anchoring, and orchestration.
- craft of the 3d: run1. Site 2's mechanical models in frames 1, 4, and 7 exhibit richer surface materials, specular highlights, and grounded weight than Site 1's simpler geometry in frames 3 and 5.
- type and layout: run1. Site 2 uses clean side-docked cards in frames 1, 4, and 5 that prevent text from colliding with the 3D subject as seen in Site 1's frame 1.

**Shown first: stock; second: run1 (repeat 1).** Overall: run1. Site 2 delivers crafted, grounded 3D machinery that tangibly illustrates how Straitjacket contains streams, locks anchors, and routes work across harnesses.

- every position composed: stock. Site 1 maintains a subject in every shot from frames 1 to 8, whereas Site 2 loses its 3D subject completely in frame 6 and overlaps cards with the 3D chamber in frame 3.
- explains the project: run1. Site 2's tactile mechanical devices—like the particle intake manifold in frames 2-4 and vernier caliper in frame 5—make abstract concepts concrete compared to Site 1's floating primitives in frames 4-6.
- craft of the 3d: run1. Site 2's machined metal and gold assemblies in frames 1-5 and 7 show richer surface detail, beveled form, and specular lighting than Site 1's simple glowing tubes and rings.
- type and layout: run1. Site 2 keeps its hero text cleanly separated on a dark panel in frame 1 while Site 1's hero headline directly collides with the bright 3D bracket in frame 1.

**Shown first: stock; second: run1 (repeat 2).** Overall: run1. Site 2 better fulfills the prompt with tactile, custom-crafted 3D machinery that tangibly visualizes how Straitjacket filters logs, locks content anchors, and routes tasks across harnesses.

- every position composed: stock. Site 1 keeps a clear 3D subject framed across frames 1-8, whereas Site 2 loses its 3D subject entirely in frame 6 and overlaps UI cards over the machinery in frame 3.
- explains the project: run1. Site 2 uses mechanical metaphors like the particle intake chamber in frames 2-4 and vernier caliper in frame 5 to directly visualize log filtering and line-drift locking, outperforming Site 1's abstract rings in frames 3-6.
- craft of the 3d: run1. Site 2's custom-modeled metallic hardware in frames 1-5 and 7 features detailed geometry, specular wear, and grounded bases compared to Site 1's simpler primitive extrusions in frames 1-6.
- type and layout: run1. Site 2 maintains cleaner typographic hierarchy and dark-card separation in frames 1, 4, and 5, while Site 1 overlaps headline text directly onto the bright 3D logo in frame 1.

**Shown first: run2; second: run3 (repeat 0).** Overall: run3. Site 2 better visualizes how Straitjacket works through clear spatial composition, labeled 3D metaphors, and an unobstructed layout across the scroll.

- every position composed: run3. Site 2 keeps its subjects clearly framed on the right across frames 1-9, whereas Site 1 lets text cards collide with 3D blocks and machines in frames 1, 2, 4, and 9.
- explains the project: run3. Site 2 uses labeled stations and literal metaphors like the filing cabinet in frame 6 to clarify the pipeline, while Site 1's machines in frames 5-6 remain more abstract.
- craft of the 3d: run2. Site 1 displays richer mechanical detail, bevels, and grounded contact shadows on the floor in frames 1-7 compared to Site 2's simpler geometry in frames 3-4.
- type and layout: run3. Site 2's compact cards and named top navigation in frames 1-9 create a cleaner hierarchy that leaves generous breathing room for the 3D scene, unlike Site 1's taller cards in frame 7.

**Shown first: run2; second: run3 (repeat 1).** Overall: run3. Site 2 better fulfills the request by pairing a clean, non-overlapping layout with a clear, well-paced 3D visual metaphor that explains how Straitjacket processes and hands off work.

- every position composed: run3. Site 2 keeps its primary 3D subjects clearly framed on the right from frame 1 to frame 9, whereas Site 1 lets its text card overlap key 3D stacks and machines in frames 1, 2, 4, and 9.
- explains the project: run3. Site 2 uses labeled stations, floor pathways, and clear metaphors like the file cabinet in frame 6 and clamped line stack in frame 8 to make the pipeline immediately legible.
- craft of the 3d: run2. Site 1 exhibits slightly richer mechanical detailing, grounded contact shadows, and surface wear on its presses across frames 1 through 7.
- type and layout: run3. Site 2 features more compact, readable cards and descriptive navigation labels in frames 1 to 9 that leave generous breathing room for the 3D scene, unlike Site 1's taller cards that shift down in frame 7.

**Shown first: run2; second: run3 (repeat 2).** Overall: run3. Site 2 delivers a cleaner, better-composed scroll journey where the UI and 3D stage never collide, making the visualization much easier to read and follow.

- every position composed: run3. Site 2 consistently frames the 3D subjects cleanly to the right across frames 1 to 9, whereas Site 1 lets the UI card overlap and obscure the 3D blocks and machines in frames 1, 2, 4, and 9.
- explains the project: run2. Site 1 visualizes the internal mechanics more clearly, showing the open press compressing logs in frame 4 and the seven agent lanes in frame 8, whereas Site 2 hides the digestion inside a closed box in frames 4 and 5.
- craft of the 3d: tie. Both sites display very similar stylized metallic machinery, ground shadows, and matte data blocks across all frames, such as the presses and cabinets in frames 1, 6, and 7.
- type and layout: run3. Site 2 uses more compact cards and labeled top navigation in frames 1 through 9, avoiding the cramped vertical overflow of Site 1's tall card in frame 7 and leaving generous room for the 3D scene.

**Shown first: run3; second: run2 (repeat 0).** Overall: run2. Site 2 better fulfills the request to visualize how Straitjacket works through detailed 3D mechanical metaphors and explicit stage-by-stage visual storytelling.

- every position composed: run3. Site 1 keeps the active 3D subjects clearly framed in the right viewport across frames 1-9, whereas Site 2's text card overlaps key 3D elements in frames 1, 2, 4, and 7.
- explains the project: run2. Site 2 illustrates the data pipeline more explicitly with annotated floor callouts and distinct stages for log floods, needle survival, line retrieval, and host gates in frames 3-8.
- craft of the 3d: run2. Site 2's machinery features richer mechanical detailing, conveyor rollers, and grounded floor typography in frames 4-7 compared to Site 1's simpler floating labels and cabinet meshes.
- type and layout: run3. Site 1's more compact left-hand card and descriptive top navigation labels keep the layout cleaner and leave unobstructed room for the 3D scene across frames 1-9, unlike Site 2's taller card cut off in frame 7.

**Shown first: run3; second: run2 (repeat 1).** Overall: run2. Site 2 better fulfills the request to visualize how Straitjacket works through detailed, open-interior 3D machinery and explicit visual metaphors for log compression, slicing, and multi-harness routing.

- every position composed: run3. Site 1 keeps the active 3D machines cleanly separated from the left card across frames 1 to 9, whereas Site 2's text card overlaps the red blocks in frames 1-2 and clips off at the bottom in frame 7.
- explains the project: run2. Site 2 illustrates internal mechanics like the needle press in frame 4, floor command callouts in frame 5, and the 7-harness gate matrix in frame 8 more concretely than Site 1's closed cabinets in frames 5-6.
- craft of the 3d: run2. Site 2's industrial machines in frames 4, 6, and 7 show richer mechanical interiors, brushed metal textures, and grounded contact shadows than Site 1's simpler geometry in frames 3-4.
- type and layout: run3. Site 1 maintains compact, fully visible cards and labeled top navigation across frames 1-9, while Site 2's tall card overflows the bottom viewport edge in frame 7.

**Shown first: run3; second: run2 (repeat 2).** Overall: run2. Despite minor text-card overlap in early frames, Site 2 better fulfills the core request by turning every step—including the quiet needle and multi-harness routing—into an intelligible 3D visualization.

- every position composed: run3. Site 1 keeps the active 3D machines cleanly separated from the left card across frames 1–9, whereas Site 2's text box overlaps the red stacks in frames 1–2 and clips at the bottom in frame 7.
- explains the project: run2. Site 2's 3D scene more clearly visualizes the mechanics, showing the yellow needle isolated in frame 3, fetched on rollers in frames 5–6, and mapped to seven harness gates in frame 8.
- craft of the 3d: tie. Both sites feature comparable industrial 3D machinery with worn metallic edges, soft ground shadows, and depth fog across all frames (e.g., Site 1 frame 6 and Site 2 frame 4).
- type and layout: run3. Site 1 keeps its left-aligned cards compact and legible with descriptive top-nav labels in frames 1–9, while Site 2's taller card gets cut off at the bottom edge in frame 7.

**Shown first: run2; second: run4 (repeat 0).** Overall: run4. Site 2 better visualizes how Straitjacket works through distinct mechanical stages and cleaner, unobstructed scroll compositions.

- every position composed: run4. Site 2 keeps its 3D assembly clearly framed on the right across frames 1-9, whereas Site 1 obscures red stacks and machines behind the text card in frames 1-4 and 9.
- explains the project: run4. Site 2 builds clearer mechanical metaphors for each step, such as the compression press in frame 3, drawer vault in frame 5, and line-shift caliper in frame 6.
- craft of the 3d: run2. Site 1 displays chunkier industrial forms, stronger edge wear, and clearer lighting and ground shadows across frames 1-9 than Site 2's darker scene.
- type and layout: run4. Site 2 uses sharper typographic hierarchy and a compact two-column table in frames 7-8 that prevents the card from growing too tall, unlike Site 1's frame 7.

**Shown first: run2; second: run4 (repeat 1).** Overall: run4. Site 2 delivers a cleaner scroll choreography and clearer visual metaphors that directly explain how Straitjacket compresses, anchors, and routes agent outputs.

- every position composed: run4. Site 2 keeps its subjects clear of the text panel across frames 1-9, whereas Site 1 obscures key 3D elements behind the text card in frames 1, 2, 4, and 9.
- explains the project: run4. Site 2 translates specific mechanics into clear visual metaphors, such as the caliper clamping shifting code lines in frame 6 and the seven-lane gate manifold in frame 8.
- craft of the 3d: run2. Site 1 features brighter lighting, clearer depth, and well-defined metallic and matte materials in frames 1-7 compared to Site 2's darker, lower-contrast machinery in frames 4-5.
- type and layout: run4. Site 2 maintains a consistent top-left card placement and compact two-column lists in frames 7-8, avoiding the layout jump and tall list seen in Site 1's frame 7.

**Shown first: run2; second: run4 (repeat 2).** Overall: run4. Site 2 better visualizes how Straitjacket works through distinct, unobstructed 3D metaphors along a well-paced scroll path.

- every position composed: run4. Site 2 keeps its 3D pipeline clearly in the right viewport across frames 1-9, whereas Site 1's text card overlaps the red blocks and machinery in frames 1-4 and 9.
- explains the project: run4. Site 2 builds clearer visual metaphors for specific mechanics, like the code-bracket caliper shifting on lines in frame 6 and the 7-lane gate manifold in frame 8.
- craft of the 3d: run2. Site 1's studio lighting, beveled edges, and contact shadows in frames 1-9 make the machines feel more solid and readable than Site 2's dark, low-contrast models in frames 1-5.
- type and layout: run4. Site 2's compact left-pinned cards and two-column lists in frames 7-8 give the 3D scene breathing room, while Site 1's taller card shifts awkwardly downward in frame 7.

**Shown first: run4; second: run2 (repeat 0).** Overall: run4. Site 1 delivers a better-composed spatial narrative where the camera travel and unobstructed 3D assembly line clearly visualize how Straitjacket bounds, stores, and anchors agent output.

- every position composed: run4. Site 1 keeps the active 3D apparatus cleanly framed to the right across frames 1-9, whereas Site 2 lets its text card occlude the red stacks and machine edges in frames 1, 2, 4, and 9.
- explains the project: run4. Site 1 clearly illustrates log compression in frame 3, local drawer storage in frame 5, and line-shift anchoring in frame 6 as a continuous assembly line.
- craft of the 3d: run2. Site 2 features brighter studio lighting, subtle metallic surface wear, beveled edges, and grounded contact shadows in frames 1, 4, and 7 compared to Site 1's darker scene.
- type and layout: run4. Site 1 maintains clean visual separation between its left UI panel and the 3D subject in frames 1-9, though frame 1 has a minor button spacing typo, while Site 2's card clips into the 3D models in frames 1, 2, and 7.

**Shown first: run4; second: run2 (repeat 1).** Overall: run4. Site 1 better fulfills the request by presenting a cohesive, unobstructed 3D assembly line across the scroll that clearly visualizes how Straitjacket processes, anchors, and routes agent evidence.

- every position composed: run4. Site 1 keeps the active 3D stations cleanly framed in the right viewport across frames 1-9, whereas Site 2 lets the UI panel overlap key 3D elements in frames 1, 2, 4, 7, and 9.
- explains the project: run4. Site 1 connects the stations into a continuous conveyor pipeline showing compression in frame 3, drawer storage in frame 5, line anchoring in frame 6, and seven parallel lanes in frame 8.
- craft of the 3d: run2. Site 2 features brighter studio lighting, worn metallic textures, beveled edges, and grounded contact shadows in frames 1-9, while Site 1's dark machinery blends into the dark floor in frames 4-5.
- type and layout: run4. Site 1 keeps its text card compact and higher up so it avoids colliding with the 3D scene in frames 1-9, unlike Site 2's taller card in frame 7, despite a minor spacing typo on Site 1's button in frame 1.

**Shown first: run4; second: run2 (repeat 2).** Overall: run4. Site 1 better fulfills the request through cleaner scroll-linked framing, zero text-over-3D collisions, and a more coherent visual pipeline that accurately mirrors each stage of Straitjacket.

- every position composed: run4. Site 1 keeps the active 3D apparatus unobstructed in the right half of frames 1-9, whereas Site 2 lets the text card overlap the red log columns in frames 1-2 and cuts off the press in frame 9.
- explains the project: run4. Site 1 clearly visualizes log compression in frame 3, drawer storage in frame 5, and line-shift anchoring in frames 6-7, while Site 2 misaligns the anchor machinery with the host lanes text in frame 7.
- craft of the 3d: run2. Site 2 exhibits better lighting, beveled metallic materials, and grounded contact shadows across frames 1-9 compared to the darker, flatter shading in Site 1's frames 4-5.
- type and layout: run4. Site 1 keeps its left-docked card compact and legible with superior spatial separation from the 3D scene in frames 1-9, whereas Site 2's card crowds and overlaps the 3D models in frames 1, 2, 4, and 7.

**Shown first: run2; second: run5 (repeat 0).** Overall: run5. Site 2 better visualizes how Straitjacket works by pairing clear mechanical metaphors and unobstructed 3D framing with clean, readable typography.

- every position composed: run5. Site 2 keeps its 3D subjects framed in the open right space across frames 1-9, whereas Site 1's text card frequently collides with and obscures key 3D elements in frames 1, 2, 3, and 4.
- explains the project: run5. Site 2 maps data flow with clear machine metaphors and floating labels in frames 3-8, such as a literal filing drawer for local storage and explicit lines tracing retrieval and anchor relocation.
- craft of the 3d: run2. Site 1 exhibits stronger lighting, contact shadows, and stylized metallic edge wear across frames 1-9, whereas Site 2's models look somewhat dark and muddy against the black floor.
- type and layout: run5. Site 2 uses more compact cards, named navigation tabs, and crisp hierarchy throughout frames 1-9, leaving generous breathing room for the 3D scene.

**Shown first: run2; second: run5 (repeat 1).** Overall: run5. Site 2 delivers a better-composed visual narrative where the layout stays out of the way of the 3D diagram, making Straitjacket's workflow immediately understandable.

- every position composed: run5. Site 2 keeps its subjects clearly framed on the right (frames 3-9), whereas Site 1's text card frequently collides with and obscures 3D elements and labels (frames 1, 2, 4).
- explains the project: run5. Site 2 illustrates the end-to-end architecture more legibly through distinct mechanical metaphors—like the Capture Press (frame 4), filing cabinet Local Store (frame 5), and Anchor Relocator (frame 7)—linked by clear pathways.
- craft of the 3d: run2. Site 1 exhibits stronger 3D craft with bolder forms, beveled edges, richer color contrast, and firmer contact shadows on the ground grid (frames 1, 4, 7) compared to Site 2's darker, flatter metallic textures.
- type and layout: run5. Site 2 uses more compact cards with cleaner internal hierarchy and labeled top navigation, giving the 3D scene ample room to breathe across frames 1-9 unlike Site 1's oversized overlapping cards.

**Shown first: run2; second: run5 (repeat 2).** Overall: run5. Site 2 better fulfills the request by pairing a legible, unobstructed scroll composition with a clearer mechanical visualization of how Straitjacket captures, stores, and routes evidence.

- every position composed: run5. Site 2 keeps its 3D subjects cleanly framed to the right in frames 3, 4, and 7, whereas Site 1 lets its text panel obscure the 3D blocks and labels in frames 1, 2, and 4.
- explains the project: run5. Site 2 clearly visualizes the continuous pipeline from raw flood to press, cabinet store, and relocator across frames 3 to 7 with clear in-scene labels.
- craft of the 3d: run2. Site 1 features richer industrial materials, beveled edges, and grounded contact shadows on the floor grid in frames 1, 4, and 7 compared to Site 2's flatter dark scene.
- type and layout: run5. Site 2 uses more compact cards, named top-bar sections, and cleaner spacing in frames 1 through 9, giving the 3D scene room to breathe unlike Site 1's oversized overlapping cards in frames 2 and 7.

**Shown first: run5; second: run2 (repeat 0).** Overall: run2. Site 2 delivers stronger 3D craft and tighter camera choreography across the scroll while still clearly illustrating Straitjacket's pipeline.

- every position composed: run2. Site 2 frames each machine prominently on the right (frames 3-8) with clear camera progression, whereas Site 1 lets text overlap the cabinet in frame 6 and cuts off the label on the right.
- explains the project: run5. Site 1's dense block slab compressing into a small digest in frames 3-4 and filing into an open drawer in frame 5 visualizes log compression and retrieval more intuitively than Site 2's abstract blocks.
- craft of the 3d: run2. Site 2's machinery in frames 1-7 shows richer industrial form, metallic bevels, and grounded contact shadows on the blueprint floor compared to Site 1's simpler geometry.
- type and layout: run5. Site 1 keeps a named section navbar and compact left cards across frames 1-9 that leave wider breathing room, while Site 2's taller card in frame 7 crowds the bottom edge.

**Shown first: run5; second: run2 (repeat 1).** Overall: run5. Site 1 better fulfills the request by pairing clear spatial composition with a more legible visual metaphor for log compression, local storage, and anchor tracking.

- every position composed: run5. Site 1 keeps the active 3D stations cleanly framed on the right (though frame 6 clips the cabinet), whereas Site 2's text card overlaps the red stacks in frames 1-2 and clips the right-hand machine in frames 3 and 9.
- explains the project: run5. Site 1's dense log slab entering the press in frames 3-4 and filing into an actual drawer cabinet in frame 5 conveys log compression and local retrieval more intuitively than Site 2's abstract blocks.
- craft of the 3d: run2. Site 2's machinery in frames 1, 4, and 7 features richer bevels, metallic wear, lighting, and grounded contact shadows compared to Site 1's flatter textures on a dark grid.
- type and layout: run5. Site 1 uses descriptive navigation labels and compact left-side cards in frames 1-9 that leave open breathing room for the 3D bench, while Site 2's taller cards crowd the scene in frames 2 and 7.

**Shown first: run5; second: run2 (repeat 2).** Overall: run5. Site 1 better fulfills the request by pairing clearer visual metaphors for log digestion and retrieval with camera compositions that give the 3D workbench room to breathe alongside the text.

- every position composed: run5. Site 1 maintains clean framing across most steps (aside from minor overlap in frame 6), whereas Site 2 repeatedly places its text card directly over key 3D stacks and machines in frames 1, 2, 4, and 9.
- explains the project: run5. Site 1's literal filing-drawer metaphor (frames 5-6) and dense token slab pressing into a needle (frames 3-4) more intuitively illustrate log compression and local retrieval than Site 2's abstract block machines.
- craft of the 3d: run2. Site 2's industrial machines in frames 1-8 exhibit stronger material definition, bolder bevels, richer color contrast, and firmer grounding with floor markings than Site 1's flatter grey models.
- type and layout: run5. Site 1 uses descriptive section names in the top nav and compact horizontal cards (frames 1-9) that leave the right two-thirds open for the 3D scene, while Site 2's taller cards crowd the viewport (frame 7).

**Shown first: run2; second: run6 (repeat 0).** Overall: run6. Site 2 delivers a cleaner, better-composed scroll experience where text and 3D machinery work together without colliding, while still visualizing the log flood, digest press, anchor rack, and seven host gates clearly across the journey.

- every position composed: run6. Site 2 keeps its 3D subject cleanly separated on the right (frames 1-3, 6, 8-9) or fills the stage cleanly during transitions (frames 4, 7), whereas Site 1 repeatedly lets the left text card cover the 3D stacks and labels in frames 1, 2, 3, and 4.
- explains the project: run2. Site 1 zooms close enough to read the visual metaphor step by step—highlighting the single yellow needle slab at line 14,238 inside the red flood (frame 3) and feeding it through the press into a small blue digest (frame 4)—while Site 2's distant camera in frames 1-3 makes the 302,628-token flood look like a tiny undifferentiated block.
- craft of the 3d: run2. Site 1's machines in frames 1, 4, and 7 have substantial bevels, rivets, worn metallic roughness, and soft contact shadows on a grounded grid, whereas Site 2's smaller models sit in a dark void with harsher, flatter lighting (frames 1-3, 9).
- type and layout: run6. Site 2's cards are more compact, use named top-bar chapters, and leave generous breathing room around the 3D scene in frames 1-3 and 6-9, while Site 1's taller cards crowd the viewport and collide with the 3D scene in frames 1-4 and 7.

**Shown first: run2; second: run6 (repeat 1).** Overall: run2. Site 1 better fulfills the request to visualize how Straitjacket works through close, readable 3D mechanical metaphors and stronger industrial 3D craft across the scroll.

- every position composed: run6. Site 2 avoids covering its 3D subjects with text across all frames (even letting the scene breathe without cards in frames 4 and 7), whereas Site 1's floating card overlaps the red blocks and machine edges in frames 1, 2, 3, and 4.
- explains the project: run2. Site 1 makes the mechanics much clearer up close, such as isolating the yellow 'quiet needle' block inside the red stack in frame 3 and pairing floor CLI labels with specific slices in frames 5 and 6, while Site 2's log flood in frames 1-3 is too distant and dense to read.
- craft of the 3d: run2. Site 1's machinery in frames 1, 4, and 7 shows richer brushed-metal wear, beveled industrial forms, and grounded contact shadows on a crisp workbench grid, whereas Site 2's models sit small in dark space in frames 1, 2, and 9.
- type and layout: run6. Site 2 uses named navigation steps in the header and more compact cards that keep the right two-thirds of the canvas completely clear in frames 3, 6, and 8, while Site 1's taller cards crowd the 3D scene in frames 1 through 4.

**Shown first: run2; second: run6 (repeat 2).** Overall: run6. Site 2 better visualizes Straitjacket's end-to-end workflow as a connected 3D machine while keeping its typography and camera framing cleanly separated across the scroll.

- every position composed: run6. Site 2 keeps the 3D subjects framed in the open right area across all frames (even giving full-bleed breathing room in frames 4 and 7), whereas Site 1's floating text card overlaps and hides the 3D stacks and machinery in frames 1, 2, 3, and 4.
- explains the project: run6. Site 2 connects its machines into a single legible pipeline—pressing the dense 302,628-token slab (frame 3) into a compact conveyor handle (frame 5), anchor rack (frame 6), and seven-lane gate (frame 8)—more clearly than Site 1's looser assembly.
- craft of the 3d: run2. Site 1 gets closer to its industrial models in frames 1, 4, and 7, showing beveled edges, metallic wear, bolts, and grounded contact shadows with greater tactile presence than Site 2's smaller, darker models in frames 1–3 and 9.
- type and layout: run6. Site 2 uses labeled top navigation and more compact cards that stay clear of the 3D stage in frames 1, 3, 6, and 8, whereas Site 1's taller cards collide with the 3D artwork in frames 1–4.

**Shown first: run6; second: run2 (repeat 0).** Overall: run2. Despite text-card overlaps in several scroll steps, Site 2 better fulfills the request to visualize how Straitjacket works with richly crafted, close-up 3D stations that clearly demonstrate truncation, digest compression, and line-range retrieval.

- every position composed: run6. Site 1 keeps its 3D machinery cleanly separated to the right of the text cards (aside from minor corner touch in frame 5) with clear camera travel across frames 1-9, whereas Site 2's text cards repeatedly collide with and obscure the 3D subjects in frames 1, 2, 4, 7, and 9.
- explains the project: run2. Site 2's close-up camera framing makes the data mechanics unmistakable—such as the yellow 'quiet needle' surviving truncation planes in frame 3 and narrow line-range retrieval in frame 5—whereas Site 1's machines sit small and distant in frames 1-3 and 9.
- craft of the 3d: run2. Site 2's industrial stations in frames 1, 4, and 7 feature tactile brushed-metal roughness, beveled edges, fasteners, and grounded shadows on a blueprint floor, while Site 1's distant models in frames 1-3 and 7 look simpler and float in dark space.
- type and layout: run6. Site 1 maintains a consistent top-left card placement and descriptive top navigation across frames 1-9 that leaves generous breathing room for the 3D scene, whereas Site 2's cards crowd the 3D machinery and jump downward in frame 7.

**Shown first: run6; second: run2 (repeat 1).** Overall: run2. Site 2 better fulfills the request to visualize how Straitjacket works through detailed, tactile 3D machinery and close-up scroll choreography that clearly illustrates the token flood, needle preservation, and anchor retrieval.

- every position composed: run6. Site 1 frames the 3D machinery cleanly to the right of the card throughout (except a minor corner touch in frame 5), whereas Site 2 repeatedly hides key 3D elements behind the text card in frames 1, 2, 4, 7, and 9.
- explains the project: run2. Site 2's close-up camera travel makes each mechanical step clear—such as isolating the yellow 'quiet needle' slab in frame 3, compressing it in frame 4, and pulling line ranges in frames 5–6—whereas Site 1 stays further back and harder to read.
- craft of the 3d: run2. Site 2's machines in frames 1–8 show richer brushed-metal textures, beveled edges, bolts, and grounded floor shadows and markings compared to Site 1's smaller, flatter models in frames 1–9.
- type and layout: run6. Site 1 keeps its left-hand card compact and consistent in frames 1–3, 5–6, and 8–9 with descriptive top-nav labels, whereas Site 2's taller cards drop down in frame 7 and collide with the 3D scene.

**Shown first: run6; second: run2 (repeat 2).** Overall: run6. Site 1 delivers a much cleaner, better-composed 3D scroll experience where the text cards and 3D machinery share the viewport harmoniously rather than constantly colliding and occluding one another.

- every position composed: run6. Site 1 keeps the 3D machinery cleanly in the right viewport across frames 1–9 (only slightly overlapping the card corner in frame 5), whereas Site 2 hides its 3D blocks and machines directly behind the left text card in frames 1, 2, 4, 5, 7, and 9.
- explains the project: run2. Site 2's close-ups clearly isolate the mechanics of each stage—like the yellow quiet needle in frame 3, the compression press in frame 4, and the seven host gate lanes in frame 8—making the pipeline easier to read visually than Site 1's more distant, dense assemblies in frames 1–3.
- craft of the 3d: run2. In frames 1, 4, and 7, Site 2's industrial presses feature richer metallic roughness, worn edge highlights, bolts, and grounded floor markings compared to Site 1's smaller, more toy-like models in frames 1–9.
- type and layout: run6. Site 1 maintains a consistent top-left card placement (when visible in frames 1–3, 5, 6, 8, and 9) with descriptive navigation labels and breathing room around the 3D scene, while Site 2 crowds the viewport and shifts the card down in frame 7.

**Shown first: run2; second: stock (repeat 0).** Overall: run2. Site 1 delivers a cohesive, grounded 3D factory metaphor with smooth scroll choreography and clean layout that genuinely visualizes how Straitjacket processes, bounds, and routes evidence.

- every position composed: run2. Site 1 smoothly tracks across a continuous 3D assembly line from frames 1 to 9 with clear framing, whereas Site 2 has awkward intermediate frames like frame 4 and obscures the 3D scene behind dense cards in frame 9.
- explains the project: run2. Site 1 concretely visualizes the mechanism—such as the yellow 'quiet needle' block surviving compression from frame 3 to frame 4 and line-retrieval rollers in frames 5 and 6—while Site 2 relies more on abstract sci-fi rings and logos in frames 1, 2, and 8.
- craft of the 3d: run2. Site 1's industrial machines in frames 1 through 9 feel grounded on a shadowed workbench with cohesive, tactile materials and bevels, whereas Site 2's objects float disconnectedly in space as seen in frames 4, 5, and 8.
- type and layout: run2. Site 1 keeps its clean, readable cards consistently docked to the left across frames 1 to 9 so the 3D stage stays open, whereas Site 2 overlaps headline text onto the 3D logo in frame 1 andclips scrolling cards under the top bar in frames 4 and 5.

**Shown first: run2; second: stock (repeat 1).** Overall: run2. Site 1 delivers a cohesive, well-composed 3D assembly-line metaphor that clearly illustrates how Straitjacket processes and routes agent context without layout glitches.

- every position composed: run2. Site 1 maintains clean framing across frames 1-9 with a pinned card and panning camera, whereas Site 2 suffers from clipped cards, header collisions, and obscured 3D scenes in frames 4, 5, and 9.
- explains the project: run2. Site 1 concretely visualizes data compression, needle preservation, and routing via physical blocks and machinery in frames 3-8, whereas Site 2 uses more abstract emblems and rings in frames 1-6.
- craft of the 3d: run2. Site 1 features grounded industrial machinery with beveled edges, worn textures, and contact shadows on a workshop grid across frames 1-9, while Site 2's objects float weightlessly in space.
- type and layout: run2. Site 1 keeps readable typography inside a consistent left-aligned card across frames 1-9, avoiding the text-over-3D collisions and top-bar overlaps seen in Site 2's frames 1, 4, 5, and 9.

**Shown first: run2; second: stock (repeat 2).** Overall: run2. Site 1 delivers a cohesive, well-crafted 3D assembly line that clearly visualizes how Straitjacket processes and routes agent context without layout collisions.

- every position composed: run2. Site 1 smoothly pans along an integrated assembly line from frames 1 to 9 with text neatly left-aligned, whereas Site 2 covers 3D subjects with UI cards in frames 5 and 9 and clips text against the header in frames 4 and 5.
- explains the project: run2. Site 1 concretely visualizes data compression, rare needle preservation, and agent routing through sequential machinery in frames 3 through 8, while Site 2 relies mostly on abstract floating rings and emblems in frames 1, 2, 5, and 8.
- craft of the 3d: run2. Site 1 features tactile, grounded machinery with beveled edges, subtle wear, and contact shadows on a floor plane across frames 1 to 9, whereas Site 2 uses ungrounded, glowing geometric primitives floating in space in frames 3 to 7.
- type and layout: run2. Site 1 keeps cards consistently pinned to the left with clean typographic hierarchy across frames 1 to 9, avoiding the header collisions and full-width card clutter seen in Site 2's frames 4, 5, and 9.

**Shown first: stock; second: run2 (repeat 0).** Overall: run2. Site 2 delivers a cohesive, continuous 3D mechanical journey that clearly visualizes how Straitjacket processes and hands off evidence without layout collisions.

- every position composed: run2. Site 2 tracks smoothly across a continuous workbench from frames 1 to 9 with clear framing, whereas Site 1 suffers from awkward overlaps, cut-off cards, and buried 3D scenes in frames 4, 5, 8, and 9.
- explains the project: run2. Site 2 physically diagrams the exact pipeline—tracking the yellow anomaly block from the raw flood in frame 3 through compression in frame 4 and vault retrieval in frame 5—while Site 1 relies on abstract floating rings in frames 3 to 6.
- craft of the 3d: run2. Site 2's industrial presses in frames 1 through 7 feature grounded shadows, tactile bevels, and cohesive PBR materials on a physical grid, whereas Site 1's floating primitives in frames 4 through 6 feel ungrounded.
- type and layout: run2. Site 2 keeps legible, consistently anchored left-hand cards across frames 1 to 9 that leave the right side open for the 3D scene, while Site 1's cards collide with the header and obscure the 3D in frames 1, 4, 5, and 9.

**Shown first: stock; second: run2 (repeat 1).** Overall: run2. Site 2 delivers a cohesive, tactile 3D assembly-line journey that clearly visualizes how Straitjacket processes logs and hands off work with clean scroll composition.

- every position composed: run2. Site 2 tracks smoothly along a continuous machine line across frames 1-9 without occlusion, whereas Site 1 overlaps text on the hero model in frame 1, clips cards in frames 4 and 5, and covers the scene in frame 9.
- explains the project: run2. Site 2 concretely visualizes a yellow needle surviving compression from red log stacks to blue digests and conveyor gates in frames 3-8, while Site 1 relies on abstract rings and floating pods in frames 3-7.
- craft of the 3d: run2. Site 2 builds grounded, tactile machinery with weathered metal materials and contact shadows on a floor plane in frames 1-9, whereas Site 1 floats basic emissive primitives in empty space across frames 1-8.
- type and layout: run2. Site 2 keeps clean, readable cards consistently anchored to the left in frames 1-9 to leave the 3D stage clear, while Site 1 shifts cards around and clips text under the sticky header in frames 4, 5, and 8.

**Shown first: stock; second: run2 (repeat 2).** Overall: run2. Site 2 delivers a cohesive, grounded 3D assembly-line tour that visually explains how Straitjacket compresses logs, preserves anomalies, and routes work without cluttering the viewport.

- every position composed: run2. Site 2 tracks cleanly along a continuous bench from frames 1 to 9 with clear framing, whereas Site 1 suffers from awkward clipping, obscured 3D elements, and sparse transitions in frames 4, 5, and 9.
- explains the project: run2. Site 2 turns the pipeline into a clear physical assembly line—compressing a red flood while preserving the yellow needle in frames 2–4 and pulling anchored slices from a vault in frames 5–6—while Site 1 relies on abstract rings and floating shapes in frames 3–6.
- craft of the 3d: run2. Site 2's industrial machines in frames 1–9 feel tangible and grounded on a floor grid with worn metal edges, bevels, and contact shadows, whereas Site 1's glossy primitives float ungrounded in space.
- type and layout: run2. Site 2 pins concise, well-proportioned cards to the left across frames 1–9 without colliding with the header or covering the 3D focal points, unlike Site 1 where scrolling cards clash with the top bar and obscure the scene in frames 1, 4, 5, and 9.

**Shown first: run3; second: run4 (repeat 0).** Overall: run4. Site 2 delivers a more cinematic scroll journey and clearer 3D mechanical metaphors that genuinely visualize how Straitjacket compresses, anchors, and routes agent output.

- every position composed: run4. Site 2 introduces dynamic camera tilts and varied angles across frames 1 to 9, whereas Site 1 mostly slides laterally with text overlapping 3D models in frames 6 and 7.
- explains the project: run4. Site 2 illustrates exact mechanics like compression in frame 3, shifted-line calipers in frame 6, and the 7-lane harness manifold in frame 8 more concretely than Site 1's generic machines.
- craft of the 3d: run4. Site 2 features detailed mechanical parts like rollers, pistons, and open drawers in frames 3 to 5 with richer material contrast than Site 1's simpler stylized boxes.
- type and layout: run3. Site 1 has cleaner card spacing and named top navigation across frames 1 to 9, avoiding typos like Site 2's missing button space in frame 1.

**Shown first: run3; second: run4 (repeat 1).** Overall: run4. Site 2 delivers more purposeful 3D mechanical metaphors and varied camera choreography that directly illustrate how Straitjacket bounds output and coordinates harnesses.

- every position composed: run4. Site 2 varies camera angles dynamically from an overview in frame 1 to close-ups in frames 6 and 8, whereas Site 1 mainly pans sideways and overlaps text with machines in frames 6 and 7.
- explains the project: run4. Site 2 concretely visualizes compression in frame 3, line relocation in frame 6, and seven harness lanes in frame 8, whereas Site 1 uses generic machine icons in frame 9.
- craft of the 3d: run4. Site 2 features detailed mechanical assemblies like rollers and pistons in frames 3-4, while Site 1 relies on simpler blocky shapes with flat shading in frames 3-5.
- type and layout: tie. Both sites use clean, readable left-aligned dark cards with clear hierarchy across frames 1-9, though Site 2 has a minor spacing typo on the button in frame 1.

**Shown first: run3; second: run4 (repeat 2).** Overall: run4. Site 2 delivers a more dynamic camera journey and clearer mechanical metaphors that directly illustrate how Straitjacket bounds logs, anchors code, and routes across harnesses.

- every position composed: run4. Site 2 varies camera angles smoothly from isometric to close-up to overhead across frames 1, 6, and 8 without crowding the left text card.
- explains the project: run4. Site 2 concretely visualizes compression in frame 3, line relocation in frame 6, and the seven-harness manifold in frames 7 and 8.
- craft of the 3d: run4. Site 2 features more intricate mechanical detailing, metallic rollers, and realistic dark industrial shading in frames 3 through 8 compared to Site 1's simpler toy-like blocks.
- type and layout: run3. Site 1 keeps cards slightly more compact with labeled top navigation across frames 1 to 9, avoiding minor spacing glitches like 'ONE20,001-LINE' in Site 2's frame 1.

**Shown first: run4; second: run3 (repeat 0).** Overall: run4. Site 1 better fulfills the core request to visualize how Straitjacket works through a cohesive, well-choreographed 3D assembly line that illustrates every stage from log compression to multi-harness gating without UI collisions.

- every position composed: run4. Site 1 smoothly frames each active station in the right viewport without overlap from frames 1 to 9, whereas Site 2 clips the red log wall behind the text card in frame 4 and hides prior stations behind the card in frames 6 and 7.
- explains the project: run4. Site 1 builds a literal physical assembly line—compressing a massive red block in a press (frame 3), storing slices in drawers (frame 5), clamping shifting lines with a caliper (frame 6), and routing across a 7-lane gate manifold (frames 7-8)—whereas Site 2 omits a dedicated 3D visualization for the multi-harness handoff in frame 9.
- craft of the 3d: run3. Site 2's machinery features stronger lighting, bevels, edge-wear highlights, and grounded contact shadows across frames 1 to 8, while Site 1's dark metal frames and flat-shaded blocks in frames 4 to 7 look dimmer and less tactile.
- type and layout: run3. Site 2 uses more concise copy, cleaner spacing, and labeled navigation tabs from frames 1 to 9, whereas Site 1 packs denser text and suffers from a missing space typo ('ONE20,001-LINE') on its primary button in frames 1 and 2.

**Shown first: run4; second: run3 (repeat 1).** Overall: run3. Site 2 delivers a cleaner, better-crafted 3D scroll experience with superior lighting, readable typography, and unobstructed framing at every step.

- every position composed: run3. Site 2 keeps each 3D station cleanly framed on the right (frames 1-9), whereas Site 1 collides its 3D models behind the text card in frames 6 and 7.
- explains the project: run4. Site 1 visually illustrates specific mechanics like log compression (frame 3), line-shift anchoring (frame 6), and seven-lane routing (frame 8) much more literally than Site 2's static boxes.
- craft of the 3d: run3. Site 2's models feature better beveled edges, specular highlights, ground shadows, and worn metallic textures (frames 1, 6, 8) compared to Site 1's darker, flatter primitives.
- type and layout: run3. Site 2 uses more concise copy, named top-bar navigation, and compact cards (frames 1-9) that leave ample breathing room for the 3D scene without text typos like Site 1's button in frame 1.

**Shown first: run4; second: run3 (repeat 2).** Overall: run3. Site 2 delivers a cleaner, better-composed scrollytelling experience with superior 3D material craft and uncluttered typography that stays out of the scene's way.

- every position composed: run3. Site 2 keeps each active 3D station cleanly framed in the right viewport across frames 1 to 9, whereas Site 1 lets the UI card overlap background 3D geometry in frames 6 and 7.
- explains the project: run4. Site 1 translates every concept into concrete mechanical metaphors—such as the hydraulic press in frame 3, the caliper shifting over code blocks in frame 6, and the seven-lane manifold in frames 7 and 8.
- craft of the 3d: run3. Site 2 exhibits stronger material definition, edge wear, and grounded contact shadows on the machinery in frames 1, 6, and 8, whereas Site 1's dark metal and unshaded blocks in frame 6 look flatter.
- type and layout: run3. Site 2 uses more concise copy, cleaner spacing, and labeled top navigation across frames 1 to 9, avoiding the denser cards and button-text typo ('ONE20,001-LINE') seen in Site 1 frame 1.

**Shown first: run3; second: run5 (repeat 0).** Overall: run5. Site 2 better answers the request to visualize how Straitjacket works through detailed, functional 3D metaphors for log compression, local storage retrieval, and multi-harness gating.

- every position composed: run3. Site 1 keeps the active 3D subject cleanly centered on the right across frames 1-9, whereas Site 2 cuts off the Anchor Relocator and obscures the store behind the text card in frame 6.
- explains the project: run5. Site 2's 3D scene illustrates the exact mechanics—such as the massive log feeding into a press to output a labeled needle in frame 4, an open drawer in frame 5, and the 7 harness lanes in frame 8.
- craft of the 3d: run5. Site 2 features more intricate, articulated machinery with open interior drawers and weathered metal textures in frames 4-7, compared to Site 1's simpler closed boxes in frames 1-6.
- type and layout: tie. Both sites use nearly identical left-aligned dark cards with clear monospace and sans-serif hierarchy from frames 1 to 9 that leave ample room for the 3D viewport.

**Shown first: run3; second: run5 (repeat 1).** Overall: run5. Site 2 better fulfills the request to visualize how Straitjacket works through detailed, functional 3D machinery that illustrates log compression, local storage retrieval, and multi-harness gating.

- every position composed: run3. Site 1 keeps the active 3D subject cleanly centered on the right across frames 1-9, whereas Site 2 cuts off the Anchor Relocator and its label in frame 6 while overlapping the cabinet with the text card.
- explains the project: run5. Site 2 visualizes the internal mechanics clearly—such as log compression in frame 4, open-drawer retrieval in frame 5, and the 7 harness lanes in frame 8—whereas Site 1 mostly places static stacks beside closed boxes.
- craft of the 3d: run5. Site 2 features more intricate, believable mechanical forms like the roller press and open filing drawer in frames 4-5 with grounded edge wear, compared to Site 1's simpler toy-like geometry.
- type and layout: tie. Both sites use an almost identical, clean HUD layout with a top bar and readable left-aligned dark cards from frames 1 to 9 that leave ample space for the 3D viewport.

**Shown first: run3; second: run5 (repeat 2).** Overall: run5. Despite a mid-transition framing hiccup in frame 6, Site 2 better fulfills the request to visualize how Straitjacket works by building transparent, mechanically legible 3D stations for log compression, local retrieval, anchor relocation, and multi-harness gating.

- every position composed: run3. Site 1 keeps the active 3D station clearly framed to the right across frames 1–9, whereas Site 2 cuts off the Anchor Relocator and its label on the right edge in frame 6 while overlapping the Local Store with the text card.
- explains the project: run5. Site 2 shows the mechanics inside each station—such as the log stream compressing into a labeled 'L14,238 · NEEDLE' digest in frame 4, the open filing drawer in frame 5, and the 7-harness Birth/Output gate matrix in frame 8—more clearly than Site 1's opaque closed boxes.
- craft of the 3d: run5. Site 2's industrial machinery in frames 4, 5, and 7 features detailed internal parts like rollers, open drawer dividers, gantry rails, and brushed metal wear that look more grounded than Site 1's blockier toy-like forms.
- type and layout: tie. Both sites use nearly identical, readable left-hand HUD cards and top step bars across frames 1–9, though Site 2 clips 'Antigravity' in frame 8's badge grid while Site 1 leaves harness icons unlabeled in frame 9.

**Shown first: run5; second: run3 (repeat 0).** Overall: run3. Site 2 delivers a cleaner, better-composed scroll experience where every station is clearly framed, well-crafted, and free from text collisions while tracing the pipeline from flood to multi-harness handoff.

- every position composed: run3. Site 2 keeps the active 3D station clearly centered on the right across all frames (1 to 9), whereas Site 1 lets the text card collide with the active cabinet and cut off the right-side machine and its label in frame 6.
- explains the project: run5. Site 1 better visualizes the mechanics of how Straitjacket works, such as the massive log slab physically feeding through the capture press into a compact digest with a needle in frame 4 and the explicit seven-harness gates in frame 8.
- craft of the 3d: run3. Site 2's 3D stations in frames 1, 6, and 8 have stronger form, cohesive dark-navy and brass materials, and crisp contact shadows grounded inside glowing floor plots.
- type and layout: run3. Site 2 features cleaner typographic hierarchy, tighter cards, and better spatial separation from the 3D subjects (frames 1 to 9), while Site 1's card overlaps key 3D machinery in frames 5 and 6 and clips 'Antigravity' in frame 8.

**Shown first: run5; second: run3 (repeat 1).** Overall: run3. Site 2 delivers a better-composed scroll journey with superior 3D modeling craft and cleaner layout separation while still clearly visualizing Straitjacket's pipeline.

- every position composed: run3. Site 2 centers each active 3D station cleanly on the right across frames 1-9, whereas Site 1 lets the UI card overlap and obscure key machinery in frames 5 and 6.
- explains the project: run5. Site 1 illustrates the system mechanics more literally, showing the slab being compressed in frame 4, lines pulled from an open drawer in frame 5, and distinct host gates in frame 8.
- craft of the 3d: run3. Site 2 features chunkier, more cohesive industrial models with crisp edge highlights, subtle surface wear, and grounded floor outlines visible across frames 1, 6, and 8.
- type and layout: run3. Site 2 maintains tighter card proportions, cleaner typographic hierarchy with highlighted metrics in frames 3-4, and better separation from the 3D focal points than Site 1's overlapping cards in frames 5-6.

**Shown first: run5; second: run3 (repeat 2).** Overall: run3. Site 2 delivers a cleaner, better-composed 3D scroll journey with superior model craft and unoccluded framing at every step.

- every position composed: run3. Site 2 keeps each active 3D station cleanly framed to the right across frames 1-9, whereas Site 1 overlaps its text card with 3D machinery in frames 5 and 6 and clips the label in frame 6.
- explains the project: run5. Site 1 physically compresses a massive log slab through the press in frames 3-4, routes handles into open filing drawers in frame 5, and visualizes the seven harness gates in frame 8.
- craft of the 3d: run3. Site 2 features bolder, more cohesive industrial models with crisp bevels, metallic wear, and grounded floor outlines in frames 1, 6, and 8.
- type and layout: run3. Site 2 uses sharper typographic hierarchy, color-coded numerical callouts in frames 3-8, and opaque cards that never collide with focal 3D subjects.

**Shown first: run3; second: run6 (repeat 0).** Overall: run3. Site 1 delivers a more cohesive, uninterrupted scroll composition where readable cards and clear 3D metaphors work together smoothly at every step.

- every position composed: run3. Site 1 maintains a balanced left-card and right-subject composition across frames 1-9, whereas Site 2 drops its UI cards in frames 4 and 7 and overlaps text with 3D geometry in frame 5.
- explains the project: run6. Site 2 visualizes the exact mechanical flow better, showing a massive token slab compressed by a press with a handle on a conveyor in frames 3-5 and seven distinct host lanes in frame 8.
- craft of the 3d: tie. Both sites feature grounded, worn metallic machinery with crisp edge highlights, contact shadows, and atmospheric depth on a dark grid across frames 1-9.
- type and layout: run3. Site 1 keeps its left-aligned information card consistent and clear of the focal 3D stations throughout frames 1-9, unlike Site 2 which collides with the rack in frame 5.

**Shown first: run3; second: run6 (repeat 1).** Overall: run6. Site 2 better fulfills the request to visualize how Straitjacket works by building an interconnected 3D mechanical factory with diegetic labels, conveyor belts, and multi-host gates that physically demonstrate log compression, content anchoring, and harness routing.

- every position composed: run3. Site 1 maintains a balanced composition with both the UI card and focused 3D station in every frame (frames 1–9), whereas Site 2 drops the text card completely during transitions in frames 4 and 7 and clips a machine at the bottom edge of frame 8.
- explains the project: run6. Site 2's 3D machinery actively visualizes the data pipeline—compressing a massive red log flood in frames 2–3, routing labeled handle `L14238 · run:8956c843e556` on a conveyor in frame 5, and mapping seven host lanes in frame 8—more concretely than Site 1's isolated stations.
- craft of the 3d: run6. Site 2's interconnected industrial machines (frames 4–8) feature richer mechanical detail, physical conveyor tracks, grounded contact shadows, and diegetic floor typography compared to Site 1's simpler floating text labels and blockier props.
- type and layout: tie. Both sites use a nearly identical, clean top navigation bar and left-aligned dark HUD card layout (frames 1–9 in Site 1; frames 1–3, 5–6, 8–9 in Site 2) with crisp monospace accents and generous right-hand space for the 3D scene.

**Shown first: run3; second: run6 (repeat 2).** Overall: run6. Despite transition framing issues where text disappears, Site 2 better fulfills the core request to visualize how Straitjacket works through clear mechanical metaphors for log pressing, handle tracking, and multi-host gates.

- every position composed: run3. Site 1 maintains a balanced composition with the card on the left and active 3D machinery on the right across frames 1-9, whereas Site 2 loses its explanatory card in transition frames 4 and 7 and overlaps the card with the 3D subject in frame 5.
- explains the project: run6. Site 2's 3D mechanics directly visualize the process—pressing raw log blocks into a digest on a conveyor with an explicit run handle in frame 5 and routing across 7 host lanes in frame 8—better than Site 1's more abstract cabinet metaphors in frames 6-8.
- craft of the 3d: run6. Both sites share similar metallic materials and lighting, but Site 2's connected conveyor machinery, grounded shadows, and diegetic floor labels in frames 4, 5, and 8 feel more cohesive than Site 1's floating text labels and simpler block stacks in frames 3-5.
- type and layout: run3. Site 1 keeps a clean, consistent card layout throughout frames 1-9 that leaves the active 3D station clear, while Site 2 suffers from missing text panels during scroll transitions in frames 4 and 7 and text-over-model collision in frame 5.

**Shown first: run6; second: run3 (repeat 0).** Overall: run3. Site 2 delivers a clearer linear visual metaphor with consistently composed scroll steps that smoothly trace data through each stage of Straitjacket's workflow.

- every position composed: run3. Site 2 keeps active stations cleanly framed on the right (frames 1-9), whereas Site 1 overlaps the text card with 3D machinery in frame 5 and drops text panels entirely during transitions in frames 4 and 7.
- explains the project: run3. Site 2 clearly visualizes sequential flow from raw log flood to digest, artifact store, and content anchor along a linear track across frames 3-9, making the architecture easier to follow than Site 1's clustered factory layout.
- craft of the 3d: run3. Both sites feature stylized, grounded PBR machinery with edge wear and floor shadows, but Site 2's larger close-up scale in frames 1-8 better showcases the tactile bevels, metallic materials, and glowing indicators.
- type and layout: tie. Both sites use nearly identical dark-mode UI cards, crisp monospace data callouts, and top navigation bars, though Site 1 loses points for text-3D collision in frame 5 while Site 2 overlaps secondary background machines in frames 6-8.

**Shown first: run6; second: run3 (repeat 1).** Overall: run3. Site 2 better fulfills the request to visualize how Straitjacket works through a clear, well-crafted, step-by-step 3D pipeline from log flood containment to multi-harness handoff.

- every position composed: run6. Site 1 keeps the active 3D machines mostly clear of the left-hand card across frames 1-9 (with only minor corner overlap in frame 5), whereas Site 2 repeatedly hides background 3D machines directly behind the text card in frames 6, 7, and 8.
- explains the project: run3. Site 2's linear left-to-right assembly line—moving from the Containment Gate (frames 3-5) to the Artifact Store (frame 6), Content Anchor (frames 7-8), and full pipeline handoff (frame 9)—makes the data lifecycle much easier to follow than Site 1's clustered machine layout.
- craft of the 3d: run3. Site 2's hero machines in frames 1-8 feature closer framing, richer metallic bevels and wear, clearer cast shadows, and grounded floor boundary lines compared to Site 1's smaller, more distant models in frames 1-3 and 9.
- type and layout: tie. Both sites share nearly identical typographic systems, dark technical cards, top progress navigation, and left-aligned layouts across frames 1-9, though Site 1 drops the text card completely during transitions in frames 4 and 7 while Site 2 keeps a card present in every frame.

**Shown first: run6; second: run3 (repeat 2).** Overall: run3. Despite some background overlap behind the text card, Site 2 delivers a clearer step-by-step 3D visualization of how Straitjacket contains floods, stores artifacts, anchors edits, and hands off work, backed by better-crafted 3D models.

- every position composed: run6. Site 1 mostly keeps the text panel clear of the active 3D subject (with minor overlap only in frame 5 and transition gaps in frames 4 and 7), whereas Site 2 repeatedly collides its text card with the 3D scene in frames 3, 4, 6, 7, and 8.
- explains the project: run3. Site 2 breaks Straitjacket into distinct, legible linear stages—Containment Gate (frames 1-5), Artifact Store (frame 6), Content Anchor (frames 7-8), and full pipeline handoff (frame 9)—making the data flow much clearer than Site 1's clustered machinery.
- craft of the 3d: run3. Site 2's 3D stations in frames 1, 6, and 8 have stronger scale, bevels, metallic wear, contact shadows, and glowing floor footprints than Site 1's smaller, flatter-looking cluster in frames 1 and 9.
- type and layout: run6. While both use nearly identical clean UI cards and typography, Site 1 keeps its left column isolated from the 3D artwork in frames 1-3, 6, 8, and 9, whereas Site 2's card crowds and covers background 3D stations in frames 6-8.

**Shown first: run3; second: stock (repeat 0).** Overall: run3. Site 1 delivers a cohesive, well-composed 3D assembly-line visualization that clearly explains how Straitjacket processes and hands off context without layout collisions.

- every position composed: run3. Site 1 keeps the 3D pipeline clearly framed on the right from frame 1 to 9, whereas Site 2 clips cards at the edges and obscures the 3D scene in frames 4, 8, and 9.
- explains the project: run3. Site 1 concretely visualizes log truncation, digest storage, and anchor tracking as physical blocks moving along a conveyor across frames 3 to 9, outperforming Site 2's abstract floating rings in frames 3 to 6.
- craft of the 3d: run3. Site 1's machines in frames 1 to 8 feature cohesive edge wear, grounded contact shadows, and physical weight on a floor grid, while Site 2's elements float ungrounded in space in frames 1 to 8.
- type and layout: run3. Site 1 anchors concise, readable cards consistently on the left across frames 1 to 9, whereas Site 2's hero text collides with the 3D logo in frame 1 and scrolling cards clip under the header in frames 4 and 5.

**Shown first: run3; second: stock (repeat 1).** Overall: run3. Site 1 delivers a cohesive, grounded 3D assembly line that visually explains how Straitjacket digests, stores, and anchors code outputs without any layout collisions or awkward scroll transitions.

- every position composed: run3. Site 1 smoothly pans along a continuous 3D pipeline from frames 1 to 9 with clear framing, whereas Site 2 has text colliding with 3D objects in frame 1, awkward cutoffs in frame 4, and text covering the 3D scene completely in frame 9.
- explains the project: run3. Site 1 concretely visualizes raw logs as a wall of red blocks being compressed into a compact digest with a yellow needle intact (frames 3–5) and stored in a cabinet (frame 6), making the mechanics much clearer than Site 2's abstract glowing rings in frames 3–6.
- craft of the 3d: run3. Site 1's machinery in frames 1–8 looks tactile and grounded on a floor plane with worn metallic edges, bevels, and contact shadows, while Site 2's objects in frames 1–5 float ungrounded in space with generic primitives and bloom.
- type and layout: run3. Site 1 keeps its clean, readable cards consistently anchored in the top-left across frames 1–9 to give the 3D stage breathing room, whereas Site 2's scrolling cards overlap the top header bar and clip off-screen in frames 2, 4, 5, and 8.

**Shown first: run3; second: stock (repeat 2).** Overall: run3. Site 1 delivers a cohesive, Scrolltide-like spatial journey along a grounded 3D assembly line that visually explains Straitjacket's log digestion, local storage, and content anchoring without layout collisions.

- every position composed: run3. Site 1 smoothly pans along a continuous 3D pipeline from frames 1 to 9 with clear framing, whereas Site 2 has text colliding with 3D objects in frames 1, 5, and 8 and completely covers the 3D scene with UI in frame 9.
- explains the project: run3. Site 1's 3D conveyor of physical stations—showing a massive red log wall compressed into a small digest in frames 3–5, a filing cabinet store in frame 6, and a shifting byte-vise in frame 8—makes the mechanics immediately legible compared to Site 2's abstract glowing rings in frames 3–6.
- craft of the 3d: run3. Site 1's machinery in frames 1–9 feels solid, grounded on a shadowed floor plane with cohesive stylized edge-worn materials, while Site 2's objects float ungrounded in space with generic primitives and harsh bloom in frames 1–5.
- type and layout: run3. Site 1 keeps text neatly contained in a consistent top-left card across frames 1–9 so the 3D scene always breathes, whereas Site 2's scrolling cards clip awkwardly under the translucent top header in frames 2, 4, 5, and 9 and obscure the 3D.

**Shown first: stock; second: run3 (repeat 0).** Overall: run3. Site 2 delivers a smoother, well-composed spatial pipeline that tangibly visualizes how Straitjacket processes and hands off evidence without layout collisions.

- every position composed: run3. Site 2 keeps its subjects clearly framed on the right across frames 1-9, whereas Site 1 obscures 3D elements behind text cards in frames 4, 5, and 9.
- explains the project: run3. Site 2 concretely visualizes log blocks compressing through a gate into a cabinet and caliper (frames 3-8), whereas Site 1 uses abstract rings and floating shapes (frames 3-7).
- craft of the 3d: run3. Site 2 grounds tactile, textured machinery with shadows onto a floor grid (frames 1-9), while Site 1 floats simpler glossy primitives in space (frames 1-8).
- type and layout: run3. Site 2 uses a clean, fixed top-left card across frames 1-9 that never collides with the 3D scene or header, unlike Site 1's overlapping scrolling cards in frames 4, 5, and 8.

**Shown first: stock; second: run3 (repeat 1).** Overall: run3. Site 2 better fulfills the request by pairing clean, unobstructed typography with a grounded, continuous 3D pipeline that clearly illustrates how Straitjacket processes and hands off evidence.

- every position composed: run3. Site 2 tracks smoothly along a grounded pipeline from frames 1 to 9 without occluding the subject, whereas Site 1 covers 3D elements with text cards in frames 1, 5, and 9.
- explains the project: run3. Site 2 visualizes log compression, storage, and anchor clamping through literal physical machines in frames 3 to 8, while Site 1 relies on more abstract floating shapes in frames 4 to 6.
- craft of the 3d: run3. Site 2 grounds beveled, textured machinery with contact shadows on a floor plane across frames 1 to 9, whereas Site 1 uses ungrounded primitives floating in space.
- type and layout: run3. Site 2 anchors concise cards consistently in the top-left across frames 1 to 9, avoiding the header collisions and text-over-3D clutter seen in Site 1 frames 4, 5, and 8.

**Shown first: stock; second: run3 (repeat 2).** Overall: run3. Site 2 delivers a cohesive, grounded 3D assembly line that visually explains Straitjacket's log digestion, storage, anchoring, and handoff mechanics with clean composition at every scroll step.

- every position composed: run3. Site 2 keeps its subjects clearly framed on the right and travels smoothly along the conveyor line from frames 1 to 9, whereas Site 1 collides text with the 3D subject in frames 1, 5, and 8 and cuts cards off at the viewport edges in frames 4 and 5.
- explains the project: run3. Site 2 builds a clear spatial machine where a wall of log blocks with a yellow needle in frame 3 gets compressed into a small stack in frame 5, stored in a cabinet in frame 6, and clamped by an anchor press in frame 8, making the pipeline much more legible than Site 1's abstract floating rings in frames 3 through 6.
- craft of the 3d: run3. Site 2's machinery in frames 1, 6, and 8 feels tactile and grounded on a floor plane with contact shadows and worn metallic edges, whereas Site 1's primitives float ungrounded in space in frames 4, 5, and 6.
- type and layout: run3. Site 2 anchors concise, readable cards consistently in the top-left across frames 1 to 9, while Site 1 suffers from hero text overlapping the 3D brace in frame 1 and scrolling cards colliding with the top navigation bar in frames 4, 5, and 9.

**Shown first: run4; second: run5 (repeat 0).** Overall: run4. Site 1 better fulfills the core request to visualize how Straitjacket works through a cohesive, well-framed 3D assembly line where every scroll step clearly illustrates the system mechanics without UI occlusion.

- every position composed: run4. Site 1 maintains clean separation between the UI card and the active 3D assembly across frames 1 to 9, whereas Site 2 lets the text card overlap key machinery in frames 5 and 6 and clips the 'ANCHOR REL' label in frame 6.
- explains the project: run4. Site 1 creates a clearer sequential linear pipeline—especially the line-shift caliper in frame 6 and the seven-lane gate conveyor in frames 7 and 8—compared to Site 2's flatter, disconnected block layout in frames 7 and 8.
- craft of the 3d: run5. Site 2 features superior worn metallic edge wear, specular lighting, and mechanical detailing on the press, cabinet, and gantry in frames 1 through 9, whereas Site 1's dark black frames lose interior detail.
- type and layout: run5. Site 2 uses more concise copy, descriptive top-bar navigation labels across frames 1 to 9, and avoids the spacing typo ('ONE20,001-LINE') seen on Site 1's button in frames 1 and 2.

**Shown first: run4; second: run5 (repeat 1).** Overall: run4. Site 1 better fulfills the request by delivering a cohesive, unobstructed scroll-driven 3D assembly line that clearly visualizes how Straitjacket processes and hands off agent context.

- every position composed: run4. Site 1 keeps the active 3D stations cleanly framed to the right across frames 1-9, whereas Site 2 hides 3D objects behind the text card in frames 2, 5, and 6 and cuts off a label in frame 6.
- explains the project: run4. Site 1's linear assembly line clearly visualizes the flow from raw log compression in frame 3 to caliper line-shift anchoring in frame 6 and gated lanes in frame 8.
- craft of the 3d: run5. Site 2's models in frames 1-9 feature richer metallic edge wear, surface shading, and grounded industrial detail than Site 1's darker, flatter geometry.
- type and layout: tie. Both sites use clean left-aligned cards and legible typography across frames 1-9, balancing Site 1's minor button kerning typo in frame 1 against Site 2's text-box overlap in frame 6.

**Shown first: run4; second: run5 (repeat 2).** Overall: run4. Site 1 delivers a better-crafted, continuously composed 3D assembly line whose camera travel and physical metaphors clearly visualize how Straitjacket compresses logs, anchors code ranges, and gates agent harnesses.

- every position composed: run4. Site 1 keeps the active 3D apparatus cleanly framed in the right viewport across frames 1–9, whereas Site 2's frame 6 cuts off the Anchor Relocator and its label on the right while hiding the cabinet behind the text card.
- explains the project: run4. Site 1 builds a clearer linear conveyor metaphor—especially in frame 6 where a caliper shifts with code lines and frames 7–8 where seven lanes pass under two physical gate bars—compared to Site 2's static floor blocks in frame 8.
- craft of the 3d: run4. Site 1's machinery features cohesive dark-steel materials, crisp yellow/blue accents, and convincing conveyor rollers in frames 3–8, whereas Site 2's cabinet in frames 5–6 has blotchy metallic wear and flat unshaded blocks on the floor.
- type and layout: run5. Site 2's cards are more concise, use descriptive top-bar navigation labels across frames 1–9, and avoid minor copy glitches like Site 1's missing space in the frame 1–2 button ('FOLLOW ONE20,001-LINE LOG').

**Shown first: run5; second: run4 (repeat 0).** Overall: run4. Site 2 delivers a better-composed, more legible 3D mechanical pipeline that smoothly guides the camera down the line to visualize how Straitjacket compresses logs, anchors source ranges, and gates agent harnesses.

- every position composed: run4. Site 2 tracks along a clean linear assembly line with focused framing on the active station in frames 1 through 9, whereas Site 1 lets the text card collide directly with the 3D cabinet in frame 6 and cuts off the relocator label.
- explains the project: run4. Site 2 uses clearer mechanical metaphors like the source-line bracket caliper shifting with edits in frame 6 and the two overhead enforcement gate bars across seven lanes in frames 7 and 8, compared to Site 1's flatter slab array in frame 8.
- craft of the 3d: run4. Site 2's dark industrial machinery with yellow cross-braces, metallic rollers, and crisp lighting in frames 1 through 8 looks more cohesive and grounded than Site 1's blotchy distressed textures on the cabinet in frames 5 and 6.
- type and layout: run5. Both use clean left-hand HUD cards, but Site 1 avoids minor typographical glitches like the missing space in 'ONE20,001-LINE' on Site 2's primary button in frames 1 and 2 and provides descriptive section names in the top bar.

**Shown first: run5; second: run4 (repeat 1).** Overall: run4. Site 2 delivers a better-composed 3D scroll journey along a continuous mechanical pipeline that clearly visualizes how Straitjacket bounds logs, anchors edits, and gates harnesses.

- every position composed: run4. Site 2 tracks cleanly down a continuous assembly line from frames 1 to 9 without the UI card occluding the primary 3D subject as happens in Site 1 frames 5 and 6.
- explains the project: run4. Site 2 makes each concept clearer, such as the code-line bracket shifting in frame 6 and the two physical enforcement gate bars across seven lanes in frames 7 and 8, compared to Site 1's abstract blocks in frame 8.
- craft of the 3d: run4. Site 2's dark industrial machinery in frames 1 through 9 features richer materials, yellow cross-bracing details, and grounded lighting compared to Site 1's flatter grey models in frames 1 and 7.
- type and layout: run5. Site 1 avoids typography errors like the missing space in Site 2's button ('ONE20,001-LINE') in frames 1 and 2, and provides clearer named navigation tabs across all frames.

**Shown first: run5; second: run4 (repeat 2).** Overall: run4. Site 2 delivers a better-crafted, unobstructed 3D scroll journey along a linear containment bench whose visual metaphors clearly illustrate how Straitjacket compresses logs, anchors code ranges, and gates agent harnesses.

- every position composed: run4. Site 2 maintains a clean linear travel along the assembly line with subjects framed to the right in frames 1 through 9, whereas Site 1 lets the left text card overlap and obscure key 3D machinery in frames 5 and 6.
- explains the project: run4. Site 2's 3D metaphors directly clarify the concepts—especially the code-line bracket shifting with edits in frame 6 and the two physical gate bars across seven roller lanes in frame 8—more clearly than Site 1's abstract blocks in frames 7 and 8.
- craft of the 3d: run4. Site 2's industrial machinery features richer dark metallic materials, crisp edge wear, grounded base rails, and cohesive mechanical details across frames 1 to 9, while Site 1's cabinets in frames 5 and 6 show muddier, blotchy textures.
- type and layout: run5. Both sites share a sharp dark-card layout, but Site 1 avoids typographical glitches like the missing space in Site 2's 'ONE20,001-LINE' button in frames 1 and 2 and provides descriptive section names in the top bar across all frames.

**Shown first: run4; second: run6 (repeat 0).** Overall: run4. Site 1 better fulfills the request by turning Straitjacket's architecture into an intuitive, uninterrupted 3D assembly-line scroll where every position stays composed and legible.

- every position composed: run4. Site 1 maintains a continuous linear travel along the assembly line across frames 1-9 without losing the UI card, whereas Site 2 loses its explanatory card completely in transition frames 4 and 7 and clips an out-of-focus foreground object in frame 8.
- explains the project: run4. Site 1's linear pipeline clearly visualizes the step-by-step flow from log compression (frame 3) to local storage (frame 5), line-shift caliper anchoring (frame 6), and multi-lane gates (frame 8).
- craft of the 3d: run6. Site 2's machinery in frames 4-8 features more refined bevels, cohesive matte-metallic surface shading, and grounded contact shadows compared to Site 1's starker geometry in frame 6.
- type and layout: run6. Site 2 uses labeled top navigation and cleaner typographic spacing—avoiding text bugs like Site 1's 'ONE20,001-LINE' in frame 1—though both position cards cleanly away from the focal 3D models.

**Shown first: run4; second: run6 (repeat 1).** Overall: run4. Site 1 better fulfills the request to visualize how Straitjacket works through a smooth, continuously composed linear 3D pipeline where every scroll step pairs readable context with clear mechanical metaphors.

- every position composed: run4. Site 1 keeps the active 3D station framed cleanly alongside its card across frames 1–9, whereas Site 2 drops text cards entirely during transitions in frames 4 and 7 and clips a foreground box in frame 8.
- explains the project: run4. Site 1 arranges a clear linear assembly line from frame 1 to 8—showing log compression in frame 3, drawer storage in frame 5, and a clear code-line caliper in frame 6—making the workflow much easier to read than Site 2's clustered layout.
- craft of the 3d: run6. Site 2's machines have stronger beveled edge wear, cohesive matte-metal shaders, and grounded contact shadows in frames 4, 6, and 7, whereas Site 1's anchor slab and blocks in frames 6 and 7 look somewhat flat.
- type and layout: run6. Site 2 features cleaner spacing, descriptive top navigation labels across frames 1–9, and avoids minor typography bugs like Site 1's missing space in the 'ONE20,001-LINE' button in frames 1 and 2.

**Shown first: run4; second: run6 (repeat 2).** Overall: run4. Site 1 better fulfills the request by organizing the 3D scene into a readable, continuous conveyor belt that clearly visualizes how Straitjacket processes logs and anchors code across every scroll step.

- every position composed: run4. Site 1 maintains a balanced composition throughout, though the text card clips a machine in frames 6 and 7, whereas Site 2 loses its text cards entirely in frames 4 and 7 and clips the 3D cabinet in frame 5.
- explains the project: run4. Site 1 presents a clearer linear assembly line from frame 1 to 8—showing the log compressed in frame 3 and lines shifting under an anchor bracket in frame 6—making the pipeline easier to follow than Site 2's clustered layout.
- craft of the 3d: run6. Site 2 features sharper edge wear, chamfered metallic surfaces, and stronger grounding shadows on the grid in frames 4 through 8 compared to Site 1's flatter blocks in frame 6.
- type and layout: run6. Site 2 has cleaner typography, descriptive top navigation labels in frames 1 to 9, and avoids the missing space typo ('ONE20,001-LINE') seen on Site 1's button in frame 1.

**Shown first: run6; second: run4 (repeat 0).** Overall: run4. Site 2 delivers a far clearer, better-crafted, and continuously composed scroll-driven 3D visualization that genuinely illustrates how Straitjacket compresses logs, anchors source ranges, and gates agent harnesses.

- every position composed: run4. Site 2 keeps its text card consistently present and frames the linear assembly line cleanly across frames 1 to 9, whereas Site 1 drops its text panels entirely in frames 4 and 7 and overlaps its card with the 3D cabinet in frame 5.
- explains the project: run4. Site 2 builds a clear left-to-right mechanical pipeline where each station visually acts out its concept—especially the sliding line-range bracket surviving a red insertion in frame 6 and the compression press in frame 3—much more legibly than Site 1's clustered racks in frames 5 and 6.
- craft of the 3d: run4. Site 2's machinery features finer mechanical detailing like conveyor rollers, hydraulic pistons, cross-bracing, drawer handles, and a grounded progress track in frames 3 through 7, whereas Site 1's models in frames 4 and 6 look blockier and more toy-like.
- type and layout: run6. Site 1 has slightly cleaner button spacing and named navigation tabs in frame 1 and a scannable single-column host list in frame 8, avoiding Site 2's missing space typo ('ONE20,001-LINE') in frame 1 and tighter two-column table in frame 8.

**Shown first: run6; second: run4 (repeat 1).** Overall: run4. Site 2 better fulfills the request through a cohesive, linearly staged 3D assembly line that clearly visualizes token compression, storage, and code-line anchoring as you scroll.

- every position composed: run4. Site 2 tracks cleanly along a linear path across frames 1-9 without losing the text cards, whereas Site 1 drops its text overlays mid-transition in frames 4 and 7 and overlaps the 3D cabinet with the card in frame 5.
- explains the project: run4. Site 2 builds clearer visual metaphors like the hydraulic press crushing tokens in frame 3 and the caliper bracket holding shifting code lines in frame 6, compared to Site 1's more abstract cabinet in frame 6.
- craft of the 3d: run4. Site 2 exhibits more grounded industrial form, metallic rollers, subtle wear, and floor progress indicators in frames 3-8, while Site 1 looks slightly more flat and toy-like in frames 4 and 8.
- type and layout: run6. Site 1 provides descriptive top-bar navigation labels across frames 1-9 and avoids typographical errors like Site 2's missing space ('ONE20,001-LINE') in frames 1 and 2.

**Shown first: run6; second: run4 (repeat 2).** Overall: run4. Site 2 better fulfills the request by pairing smooth, uninterrupted camera travel along a well-crafted 3D assembly line with clear mechanical metaphors that make Straitjacket's workflow immediately understandable.

- every position composed: run4. Site 2 keeps its text card consistently present while traveling linearly down the assembly line from frames 1 to 9, whereas Site 1 drops its UI cards in frames 4 and 7 and overlaps the card with the 3D machine in frame 5.
- explains the project: run4. Site 2 creates clearer visual metaphors for the pipeline—especially the red slab compressing in frame 3 and the caliper shifting across code lines in frames 6 and 7—compared to Site 1's more abstract cabinet in frame 6.
- craft of the 3d: run4. Site 2's machinery features richer mechanical detail like conveyor rollers, cross-braces, and worn metallic edges on a grounded track in frames 1 to 5, while Site 1's models in frames 4 to 8 look slightly flatter and more toy-like.
- type and layout: run6. Site 1 has cleaner typography and descriptive top navigation labels across frames 1 to 9, avoiding minor spacing glitches like 'FOLLOW ONE20,001-LINE LOG' seen in Site 2's frame 1 button.

**Shown first: run4; second: stock (repeat 0).** Overall: run4. Site 1 delivers a cohesive, unobstructed scroll-driven 3D visualization that tangibly demonstrates how Straitjacket processes and hands off agent context.

- every position composed: run4. Site 1 keeps the 3D assembly line cleanly framed on the right from frame 1 to 9, whereas Site 2 hides or clips 3D subjects behind text cards in frames 1, 5, and 9.
- explains the project: run4. Site 1's continuous factory metaphor—pressing logs in frame 3, storing in frame 5, anchoring lines in frame 6, and gating lanes in frame 8—explains the workflow clearer than Site 2's disconnected vignettes.
- craft of the 3d: run4. Site 1's machinery in frames 1–9 feels grounded on a dark floor with cohesive industrial detailing and shadows, while Site 2's models float ungrounded in space in frames 4, 5, and 8.
- type and layout: run4. Site 1 pins concise cards consistently to the top-left across frames 1–9 without overlapping the header or 3D scene, unlike Site 2 where scrolling cards collide with the top bar in frames 4, 5, and 9.

**Shown first: run4; second: stock (repeat 1).** Overall: run4. Site 1 delivers a cohesive, grounded 3D factory journey that clearly visualizes how Straitjacket processes, stores, anchors, and routes agent evidence without layout collisions.

- every position composed: run4. Site 1 keeps the 3D assembly line continuously visible and clearly framed from frames 1 to 9, whereas Site 2 hides or cuts off 3D elements behind text cards in frames 1, 4, 5, and 9.
- explains the project: run4. Site 1's cohesive factory metaphor visually connects log compression (frame 3), local vault storage (frame 5), line anchoring (frame 6), and seven-lane routing (frame 8) much more concretely than Site 2's disconnected abstract shapes.
- craft of the 3d: run4. Site 1 grounds its detailed mechanical models onto a physical floor plane with shadows and tactile textures across frames 1–9, while Site 2 floats simpler glowing primitives in empty space.
- type and layout: run4. Site 1 anchors a consistent, readable left-hand panel across frames 1–9 (aside from a minor spacing typo in frame 1's button) that leaves open room for the 3D scene, whereas Site 2's scrolling cards collide with its sticky header and 3D subjects in frames 4, 5, and 9.

**Shown first: run4; second: stock (repeat 2).** Overall: run4. Site 1 delivers a cohesive, grounded 3D factory journey with clean layout separation that tangibly visualizes how Straitjacket processes and hands off evidence.

- every position composed: run4. Site 1 smoothly tracks along a continuous assembly line in frames 1-9 without overlapping text, whereas Site 2 collides text with 3D objects in frame 1 and obscures the scene in frame 9.
- explains the project: run4. Site 1 uses concrete mechanical metaphors like the hydraulic press in frame 3, drawer vault in frame 5, and caliper in frame 6 to clearly illustrate log compression, storage, and line anchoring.
- craft of the 3d: run4. Site 1 grounds its detailed industrial machinery on a gridded floor with cohesive lighting across frames 1-9, while Site 2 relies on disconnected, floating sci-fi shapes in frames 4-8.
- type and layout: run4. Site 1 keeps readable, well-structured cards consistently docked on the left across frames 1-9 (despite minor button kerning in frame 1), whereas Site 2 clips cards at the viewport edges in frames 4, 5, and 8.

**Shown first: stock; second: run4 (repeat 0).** Overall: run4. Site 2 delivers a cohesive, continuous 3D scroll journey along a grounded physical machine that clearly visualizes how Straitjacket processes and routes code evidence without layout collisions.

- every position composed: run4. Site 2 tracks cleanly along a continuous assembly line across frames 1-9 without overlapping text, whereas Site 1 cuts off cards under the header and obscures 3D objects in frames 4, 5, and 9.
- explains the project: run4. Site 2 uses an intuitive mechanical factory metaphor—pressing raw blocks in frame 3, storing in drawers in frame 5, and clamping shifted lines in frame 6—making the workflow clearer than Site 1's abstract floating rings in frames 3 and 5.
- craft of the 3d: run4. Site 2 builds a grounded, cohesive machine on a floor plane with worn metal edges and rollers in frames 3-8, while Site 1 relies on disconnected, ungrounded glowing shapes floating in space in frames 4-6.
- type and layout: run4. Site 2 keeps its clean typography fixed in a dedicated top-left card across frames 1-9 to leave the 3D scene open, whereas Site 1's scrolling cards collide with the sticky header and hero model in frames 1, 4, and 5.

**Shown first: stock; second: run4 (repeat 1).** Overall: run4. Site 2 delivers a cohesive, unobstructed scroll-driven 3D assembly line that clearly visualizes how Straitjacket bounds, stores, and hands off code evidence.

- every position composed: run4. Site 2 tracks smoothly down a single industrial line across frames 1 to 9 without colliding with UI, whereas Site 1 clips cards and obscures 3D elements in frames 4, 5, 8, and 9.
- explains the project: run4. Site 2 makes the mechanics tangible by pressing a log slab in frame 3, storing slices in frame 5, and clamping shifted lines in frame 6, whereas Site 1 uses more abstract floating rings in frames 3 to 6.
- craft of the 3d: run4. Site 2 builds a grounded, cohesive assembly line with believable materials and contact shadows on a floor plane across frames 1 to 9, while Site 1's objects float ungrounded in space.
- type and layout: run4. Site 2 keeps legible, well-proportioned cards anchored cleanly on the left across frames 1 to 9, whereas Site 1 suffers from header collisions and overlapping text in frames 1, 4, 5, and 9.

**Shown first: stock; second: run4 (repeat 2).** Overall: run4. Site 2 turns Straitjacket's pipeline into a cohesive, grounded 3D assembly line with smooth camera choreography and unobstructed, well-structured typography.

- every position composed: run4. Site 2 tracks smoothly down a continuous assembly line from frames 1 to 9 without text blocking the 3D subject, whereas Site 1 clips cards and obscures 3D forms in frames 4, 5, and 9.
- explains the project: run4. Site 2 physically visualizes log compression, local vault storage, edit-surviving line calipers, and harness lanes across frames 3 to 8, while Site 1 uses disconnected, abstract glowing shapes.
- craft of the 3d: run4. Site 2's industrial machinery in frames 1 to 9 sits grounded on a floor grid with believable materials and contact shadows, whereas Site 1's primitives float weightlessly in space.
- type and layout: run4. Site 2 anchors concise, readable cards cleanly to the top-left across frames 1 to 9, avoiding the header collisions and text-over-3D clutter seen in Site 1's frames 1, 4, 5, and 9.

**Shown first: run5; second: run6 (repeat 0).** Overall: run5. Site 1 delivers higher 3D craft and a clearer mechanical metaphor for how Straitjacket processes, stores, and anchors logs, despite minor card overlaps.

- every position composed: run6. Site 1's text card overlaps the 3D cabinet and press in frames 2, 5, and 6, while Site 2 mostly keeps the 3D subjects clear in the right space across frames 1–9.
- explains the project: run5. Site 1 tracks a clearer step-by-step physical pipeline from the raw log slab into the press, drawer store, and relocator across frames 3–7.
- craft of the 3d: run5. Site 1's machinery features richer metallic edge wear, realistic mechanical forms, and better contact grounding in frames 1, 5, and 7 than Site 2's simpler stylized models.
- type and layout: run6. Site 2 keeps its cards crisp and opaque without colliding with the 3D scene (save a tiny corner in frame 5), and removes cards during camera transitions in frames 4 and 7.

**Shown first: run5; second: run6 (repeat 1).** Overall: run5. Site 1 better visualizes how Straitjacket works through distinct, well-crafted 3D stations that trace the exact lifecycle of a log from raw flood to local storage and anchored edits.

- every position composed: run6. Site 1 lets its left text card overlap key 3D machinery in frames 5 and 6, whereas Site 2 keeps subjects clear or drops the card during camera transitions like frames 4 and 7.
- explains the project: run5. Site 1 builds a clearer linear bench from flood to press, drawer store, and relocator across frames 3 to 7, whereas Site 2 clusters machines tightly and skips a dedicated local-store stage.
- craft of the 3d: run5. Site 1 features more distinct mechanical forms and worn metallic surface textures on the press and cabinet in frames 4 through 7 compared to Site 2's simpler, uniform boxy models.
- type and layout: run6. Site 2 keeps its cards crisp and opaque without colliding with the 3D focal points in frames 5, 6, and 8, unlike Site 1 where cards sit directly over the machines in frames 5 and 6.

**Shown first: run5; second: run6 (repeat 2).** Overall: run6. Site 2 delivers a cleaner, better-composed scroll experience with cohesive 3D industrial craft and unobstructed framing that clearly visualizes Straitjacket's pipeline from log flood to host gates.

- every position composed: run6. Site 2 keeps the active 3D stations cleanly framed on the right (with dedicated full-bleed transitions in frames 4 and 7), whereas Site 1's left text card overlaps the 3D press and filing cabinet in frames 5, 6, and 7 and cuts off the 'ANCHOR R' label in frame 6.
- explains the project: run5. Site 1 translates each mechanism into distinct, legible physical metaphors—pressing raw log blocks into a digest with a highlighted yellow needle (frame 4), filing in a drawer (frame 5), relocating anchors on a gantry (frame 7), and passing through Pre/Post gates (frame 8).
- craft of the 3d: run6. Site 2's machinery features cohesive chamfered dark-metal materials, brass hardware accents, and strong grounded contact shadows across frames 1–9, whereas Site 1's flat-shaded floor pads in frame 8 and muddy mottled cabinet textures in frame 5 look less unified.
- type and layout: run6. Site 2 maintains tighter card hierarchy—like the clean per-host gate badges in frame 8—and avoids overlapping the 3D focal points, unlike Site 1 where the card collides with the 3D cabinetry in frames 5 and 6.

**Shown first: run6; second: run5 (repeat 0).** Overall: run6. Site 1 delivers a more cohesive, well-crafted 3D visual aesthetic and cleaner layout composition throughout the scroll without suffering from severe UI-to-3D collisions.

- every position composed: run6. Site 1 keeps its 3D subjects well-framed to the right across frames 1–9 (though the UI card disappears in transition frames 4 and 7), whereas Site 2 lets its text card directly collide with and cover the 3D models in frames 2, 5, and 6 and clips a 3D label off-screen in frame 6.
- explains the project: run5. Site 2 breaks the architecture into clearer physical analogies—showing the flood compressing in frame 4, filing into a drawer cabinet in frame 5, relocating via a gantry in frame 7, and passing through labeled PreToolUse/PostToolUse gates in frame 8.
- craft of the 3d: run6. Site 1's machinery in frames 4–8 has crisp, cohesive stylized edge-wear and grounded proportions, whereas Site 2 mixes blotchy procedural metal noise on the cabinet in frame 5 with flat, untextured blocks on the floor in frame 8.
- type and layout: run6. Site 1 maintains clean separation between the left UI card and the 3D stage (aside from minor overlap in frame 5) and presents a much clearer harness table in frame 8 compared to Site 2's cramped, colliding cards in frames 5–6 and clipped 'Antigravity' pill in frame 8.

**Shown first: run6; second: run5 (repeat 1).** Overall: run6. Site 1 delivers a more cohesive, well-composed 3D scroll experience where custom-crafted machinery, clean typography, and unobstructed framing work together seamlessly from top to bottom.

- every position composed: run6. Site 1 keeps the active 3D apparatus well-framed in the right viewport across frames 1-9 (with only minor card overlap on the rack in frame 5), whereas Site 2 hides the press and cabinet behind the text card in frames 2, 5, and 6 and clips the 3D label on the right edge in frame 6.
- explains the project: run5. Site 2 breaks the pipeline into four distinct, legible metaphors—press (frames 3-4), filing cabinet store (frame 5), gantry relocator (frame 7), and labeled pre/post gate lanes (frame 8)—making the step-by-step data flow clearer than Site 1's three-machine cluster.
- craft of the 3d: run6. Site 1's machinery has cohesive stylized edge-wear, bevels, and grounded contact shadows across all three units (frames 4-8), while Site 2 mixes blotchy procedural metal on the cabinet (frame 5) with flat, untextured blocks for the host gates (frame 8).
- type and layout: run6. Site 1's cards present crisp hierarchy, clear tabular harness comparisons in frame 8, and leave open space for the 3D scene, whereas Site 2's card collides with the 3D models in frames 5-6 and truncates 'Antigravity' inside a cramped pill grid in frame 8.

**Shown first: run6; second: run5 (repeat 2).** Overall: run6. Site 1 delivers a much more polished, cohesive 3D world with superior camera framing, material consistency, and typographic clarity that avoids colliding text cards into the 3D models during scroll.

- every position composed: run6. Site 1 keeps its 3D subjects well-separated in the right viewport across most steps (only lightly clipping in frame 5), whereas Site 2 repeatedly collides its text cards with the 3D machinery in frames 2, 5, and 6 and cuts off a 3D label on the right edge of frame 6.
- explains the project: run5. Site 2 breaks the pipeline into distinct, intelligible physical metaphors—pressing the raw flood (frame 4), filing in a local drawer store (frame 5), relocating anchors on a gantry (frame 7), and routing through labeled PreToolUse/PostToolUse gates (frame 8)—more clearly than Site 1's fewer, more abstract machines.
- craft of the 3d: run6. Site 1 has cohesive stylized industrial machinery with crisp edge highlights and purposeful mechanical forms across frames 1–9, while Site 2 mixes blotchy procedural metal textures on its cabinet (frames 5–6) with flat, ungrounded primitive blocks for its harness gates (frame 8).
- type and layout: run6. Site 1 uses cleaner typographic hierarchy, crisp structured tables (especially the seven-harness breakdown in frame 8 compared to Site 2's cramped pill grid where 'Antigravity' clips in frame 8), and positions cards cleanly away from the 3D focal points.

**Shown first: run5; second: stock (repeat 0).** Overall: run5. Site 1 better fulfills the request by pairing clean, unobstructed scroll choreography with grounded mechanical 3D metaphors that clearly visualize how Straitjacket processes and routes data.

- every position composed: run5. Site 1 maintains a clear, continuous camera path across the workbench from frames 1 to 9 without text covering the active subject, whereas Site 2 suffers from overlapping text and cut-off elements in frames 1, 4, 5, and 9.
- explains the project: run5. Site 1 uses a literal, cohesive assembly-line metaphor—compressing logs in a press (frame 4), filing in drawers (frame 5), and tracking edits (frame 7)—while Site 2 relies more on abstract sci-fi rings and logos (frames 1, 3, and 8).
- craft of the 3d: run5. Site 1's industrial machines feature grounded shadows, worn metal textures, and mechanical detail on a physical grid (frames 1–9), whereas Site 2's objects float ungrounded in space with simpler emissive geometry (frames 2–8).
- type and layout: run5. Site 1 keeps text cleanly contained in a consistent left-hand card across frames 1 to 9, while Site 2's scrolling panels collide with the top header bar and obscure the 3D scene in frames 1, 4, 5, and 9.

**Shown first: run5; second: stock (repeat 1).** Overall: run5. Site 1 delivers a cohesive, grounded 3D workbench narrative that clearly visualizes how Straitjacket processes logs without layout collisions.

- every position composed: run5. Site 1 maintains a clear, continuous camera path across the workbench from frames 1 to 9, whereas Site 2 collides text with 3D subjects in frame 1 and obscures the background scene completely in frame 9.
- explains the project: run5. Site 1 uses concrete physical metaphors like the capture press compressing blocks in frames 3-4 and a filing cabinet in frame 5, whereas Site 2 relies mostly on abstract floating rings and emblems in frames 1-3.
- craft of the 3d: run5. Site 1 grounds its textured industrial machinery with shadows on a shared floor plane across frames 1-9, while Site 2 floats disconnected, emissive primitives in empty space in frames 2-8.
- type and layout: run5. Site 1 keeps readable, compact cards pinned cleanly to the left in frames 1-9, while Site 2 suffers from text overlapping the 3D logo in frame 1 and clipped cards under the header in frames 4 and 5.

**Shown first: run5; second: stock (repeat 2).** Overall: run5. Site 1 better fulfills the request by pairing clean, unobtrusive typography with a continuous, grounded 3D workbench metaphor that clearly visualizes how Straitjacket processes logs.

- every position composed: run5. Site 1 maintains a steady left-card layout that leaves the workbench clear across frames 1-9, whereas Site 2 clips cards at the viewport edges and obscures the 3D scene in frames 4, 5, and 9.
- explains the project: run5. Site 1 visually traces a single log slab through physical machinery from raw flood to digest and store across frames 3-8, while Site 2 uses disconnected abstract sci-fi motifs like rings and pods in frames 1, 3, and 7.
- craft of the 3d: run5. Site 1 features grounded, textured industrial machines resting on a gridded floor with contact shadows in frames 1-9, whereas Site 2 floats untethered glowing primitives in space in frames 1-8.
- type and layout: run5. Site 1 keeps readable, consistently positioned cards in frames 1-9, while Site 2 overlaps headline text on bright 3D geometry in frame 1 and suffers from cut-off scrolling panels in frames 4 and 5.

**Shown first: stock; second: run5 (repeat 0).** Overall: run5. Site 2 delivers a cohesive, grounded 3D mechanical workbench with smooth scroll choreography and clean layout that genuinely helps a visitor visualize how Straitjacket captures, stores, and routes evidence.

- every position composed: run5. Site 2 keeps its left-hand card fixed and smoothly pans across the 3D workbench from frames 1 to 9, whereas Site 1 collides text with the 3D subject in frame 1 and leaves awkward transitions and covered 3D in frames 4, 5, and 9.
- explains the project: run5. Site 2's connected factory bench—tracing a massive token slab through the Capture Press, Local Store cabinet, Anchor Relocator, and host gate lanes across frames 3 to 8—concretely visualizes how the tool works compared to Site 1's abstract floating rings and pods in frames 3 to 7.
- craft of the 3d: run5. In frames 1 through 9, Site 2's industrial press, filing drawers, and machinery sit grounded with worn metal textures and contact shadows on a floor plane, while Site 1's glowing primitives in frames 1 to 8 float weightlessly in empty space.
- type and layout: run5. Site 2 maintains a clean, compact left-aligned card across frames 1 to 9 that never crowds the 3D scene, whereas Site 1's hero headline overlaps the 3D logo in frame 1 and its scrolling cards clip into the top header in frames 3, 4, 5, and 8.

**Shown first: stock; second: run5 (repeat 1).** Overall: run5. Site 2 better fulfills the request by pairing clean scroll choreography and grounded 3D craft with a clear mechanical metaphor that actually visualizes how Straitjacket works.

- every position composed: run5. Site 2 keeps its industrial bench smoothly framed across frames 1-9, whereas Site 1 obscures or clips 3D subjects behind text cards in frames 4, 5, and 9.
- explains the project: run5. Site 2's labeled mechanical press, filing drawer, and relocator in frames 3-7 concretely demonstrate log compression and retrieval, unlike Site 1's abstract portals in frames 3-6.
- craft of the 3d: run5. Site 2's weathered metal machinery grounded with shadows on a grid in frames 1-9 feels substantially crafted compared to Site 1's floating primitives in frames 1-8.
- type and layout: run5. Site 2 uses consistent left-docked cards in frames 1-9 that leave room for the 3D scene, while Site 1's shifting panels overlap headers and models in frames 1, 4, and 5.

**Shown first: stock; second: run5 (repeat 2).** Overall: run5. Site 2 delivers a cohesive, well-crafted 3D workbench visualization that clearly explains Straitjacket's architecture while maintaining clean typography and composition at every scroll step.

- every position composed: run5. Site 2 keeps its workbench stages cleanly framed across frames 1-9, whereas Site 1 has text colliding with 3D objects in frames 1, 5, and 9, and awkward mid-transition framing in frame 4.
- explains the project: run5. Site 2's continuous physical pipeline—from the raw log slab entering the capture press in frames 3-4 to the local store drawer in frame 5 and anchor relocator in frame 7—visually demonstrates how data is processed better than Site 1's abstract shapes in frames 4-6.
- craft of the 3d: run5. Site 2's industrial machinery in frames 1-9 features worn metal textures, contact shadows, and physical grounding on a floor plane, while Site 1's glowing primitives in frames 1-8 float ungrounded in space.
- type and layout: run5. Site 2 uses a consistent, readable left-hand card across frames 1-9 that leaves ample room for the 3D scene, whereas Site 1's cards shift sides and obscure both the 3D subject and top header bar in frames 4, 5, and 9.

**Shown first: run6; second: stock (repeat 0).** Overall: run6. Site 1 delivers a cohesive, grounded 3D factory visualization that clearly illustrates how Straitjacket compresses logs, anchors lines, and routes across harnesses, paired with disciplined layout and camera choreography from top to bottom.

- every position composed: run6. Site 1 smoothly pans across one continuous 3D scene in frames 1–9 with subjects cleanly framed on the right, whereas Site 2 suffers from text overlapping the 3D logo in frame 1, awkward clipped transitions in frame 4, and full-width text obscuring the 3D scene in frame 9.
- explains the project: run6. Site 1 uses a literal, cohesive factory machine metaphor—showing raw log blocks getting pressed into a small digest in frames 3–5, stored in an anchor rack in frame 6, and routed through seven host lanes in frame 8—while Site 2 swaps between disconnected abstract shapes like logo brackets in frames 1–2 and glowing rings in frames 3 and 5.
- craft of the 3d: run6. Site 1's machinery in frames 4–8 looks tactile, detailed, and physically grounded with contact shadows on a floor plane, whereas Site 2's models in frames 1–8 float ungrounded in space with simpler primitive geometry and generic bloom.
- type and layout: run6. Site 1 anchors crisp, consistent HUD cards to the top-left in frames 1–3, 5–6, and 8–9 so the 3D stage always breathes, while Site 2's scrolling cards collide with the hero 3D object in frame 1, clip under the top navbar in frames 4 and 5, and crowd the viewport in frames 8 and 9.

**Shown first: run6; second: stock (repeat 1).** Overall: run6. Site 1 turns Straitjacket's architecture into a cohesive, grounded 3D machine with clean spatial choreography and disciplined typography that gives the visualization room to breathe.

- every position composed: run6. Site 1 keeps the 3D factory clearly framed to the right in frames 1 through 9 with smooth camera travel, whereas Site 2 has text overlapping the 3D logo in frame 1, an awkward sparse view in frame 4, and full-width cards covering the 3D scene in frame 9.
- explains the project: run6. Site 1 builds a single continuous mechanical metaphor—pressing a 302,628-token slab in frame 3 into a small digest with an address in frame 5 and routing it to an anchor rack in frame 6—whereas Site 2 swaps between disconnected abstract shapes across frames 1, 3, 5, and 7.
- craft of the 3d: run6. In frames 4 through 8 of Site 1, the industrial press, rack, and gate lanes have cohesive beveled edges, matte metallic materials, and grounded contact shadows on the floor grid, while Site 2's objects in frames 1, 4, and 5 float ungrounded in space with basic glowing primitives.
- type and layout: run6. Site 1 pins concise, scannable cards to the top-left in frames 1–3, 5–6, and 8–9 so the 3D stage stays completely open, whereas Site 2 collides its hero headline with the 3D model in frame 1 and crowds the viewport with tall scrolling panels in frames 4, 8, and 9.

**Shown first: run6; second: stock (repeat 2).** Overall: run6. Site 1 delivers a cohesive, grounded 3D factory visualization that genuinely explains how Straitjacket compresses logs and anchors code across a smooth, well-composed scroll.

- every position composed: run6. Site 1 smoothly pans across one continuous 3D scene in frames 1 through 9 with only minor overlap in frame 5, whereas Site 2 suffers from text overlapping the 3D subject in frame 1, awkward clipped transitions in frame 4, and cards obscuring the 3D background in frames 5 and 9.
- explains the project: run6. Site 1 uses a clear physical factory metaphor—showing a massive red-and-black token flood in frame 3 compressed by a press into a tiny digest in frame 5 and routed to an anchor rack in frame 6—whereas Site 2 relies on disconnected, abstract glowing logos and rings in frames 1, 2, and 8.
- craft of the 3d: run6. Site 1's machinery in frames 4, 6, and 7 looks solid, grounded on a floor grid with cast shadows, chamfered edges, and cohesive matte-industrial materials, while Site 2's objects float ungrounded in space with generic bloom and primitive geometry like the rings in frame 3.
- type and layout: run6. Site 1 keeps text inside clean, consistent left-hand cards (frames 1–3, 5–6, 8–9) that leave the right side open for the 3D scene, while Site 2 collides headline text with the 3D logo in frame 1 and scrolls tall, dense cards across both sides of the screen in frames 4, 5, and 9.

**Shown first: stock; second: run6 (repeat 0).** Overall: run6. Site 2 delivers a cohesive, grounded 3D factory metaphor with disciplined camera choreography and clean card layout that clearly visualizes how Straitjacket processes logs, anchors, and host gates.

- every position composed: run6. Site 2 keeps its 3D machinery cleanly framed and unobstructed across all 9 frames with smooth camera travel, whereas Site 1 collides headlines and cards directly into the 3D subject in frames 1, 5, 8, and 9 and clips text cards at viewport edges in frames 4 and 8.
- explains the project: run6. Site 2 builds one cohesive, labeled physical machine—showing a 302,628-token slab entering a digest press (frames 3–5), moving to an anchor rack (frame 6), and routing through seven host gates (frames 7–8)—while Site 1 swaps between disconnected, abstract sci-fi rings and pods (frames 3, 5, 7).
- craft of the 3d: run6. Site 2's industrial machinery sits grounded on a floor plane with contact shadows, beveled edges, and cohesive materials across frames 1–9, whereas Site 1's floating primitives and glowing rings in frames 3–6 lack grounding shadows and cohesive materiality.
- type and layout: run6. Site 2 places concise, well-proportioned cards consistently in the top-left corner (frames 1–3, 5–6, 8–9) leaving ample breathing room for the 3D scene, while Site 1's oversized scrolling panels overlap the 3D art and header bar in frames 1, 4, 5, 8, and 9.

**Shown first: stock; second: run6 (repeat 1).** Overall: run6. Site 2 delivers a cohesive, unobstructed 3D scroll journey through a tangible mechanical metaphor that clearly visualizes how Straitjacket works.

- every position composed: run6. Site 2 keeps its 3D assembly unobstructed across frames 1 to 9 with smooth camera travel, whereas Site 1 obscures the 3D under text in frames 1, 5, and 9.
- explains the project: run6. Site 2 uses a unified factory press, rack, and host lanes with in-scene labels in frames 5, 6, and 8 to directly visualize token compression and anchoring, whereas Site 1 uses disconnected abstract shapes.
- craft of the 3d: run6. Site 2 features cohesive, grounded machinery with contact shadows on a floor plane across frames 1 to 9, while Site 1 floats simple glowing primitives in space.
- type and layout: run6. Site 2 places concise, readable cards consistently in the top-left in frames 1 to 3, 5, 6, 8, and 9, while Site 1 suffers from text overlapping the header in frames 4 and 5.

**Shown first: stock; second: run6 (repeat 2).** Overall: run6. Site 2 delivers a cohesive, grounded 3D factory diorama with smooth scroll choreography and clean layout separation that genuinely visualizes how Straitjacket compresses logs, anchors lines, and connects harnesses.

- every position composed: run6. Site 2 keeps its 3D machinery cleanly framed on the right or center with clear camera travel across frames 1–9, whereas Site 1 collides text with 3D in frame 1, clips cards under the header in frames 4–5, and obscures the 3D scene behind full-width tables in frame 9.
- explains the project: run6. Site 2 builds one coherent physical machine with in-world labels—showing 302,628 token slabs entering a 'DIGEST PRESS · 570x' in frames 3–5, an 'ANCHOR RACK' in frame 6, and '7 HOSTS' gates in frame 8—while Site 1 jumps between disconnected sci-fi rings and floating pods (frames 3, 5, 7).
- craft of the 3d: run6. Site 2's industrial press, rack, and conveyor sit grounded on a dark floor grid with beveled edges, matte metallic materials, and contact shadows (frames 4–8), while Site 1's primitives float ungrounded in space with flat shading and harsh bloom (frames 1–7).
- type and layout: run6. Site 2 anchors concise, scannable cards consistently on the left so the 3D diorama always has room to breathe (frames 1–3, 5–6, 8–9), whereas Site 1's hero headline overlaps the 3D bracket in frame 1 and its scrolling cards collide with the sticky header in frames 2, 4, 5, and 8.
