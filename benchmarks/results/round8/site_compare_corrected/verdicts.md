# The sites, judged blind on corrected frames

60 verdicts, six on each pair of sites (three in each order), by `gemini-4-carbon` (thinking low) through `benchmarks/site_compare.py`. The judge saw nine desktop frames of each site, at even scroll steps and 960 px wide, as "the first site" and "the second site", with the request in `request.txt`. It never saw which run made which. The frames are those of the corrected site check (no black band or box from the browser's software path; see `../../ROUND_EIGHT.md`).

- `run1`: the first run's site (`../../site_run1/`, the plugin as it was after round seven)
- `run2`: the second run's (`../../site_run2_plugin/`)
- `run3`: the third run's (`../../site_run3_plugin/`)
- `stock`: the stock agent's, the same request without the plugin (`../../site_stock/`)
- `run4`: the fourth run's (`../../site_run4_plugin/`)

The model service was busy for much of the day: of the eighteen calls on the first three plugin sites, seventeen were refused at least once (HTTP 503) and asked again over two hours (13:25 to 15:27 UTC); the eighteen calls that involve the stock site were asked in six passes (15:55 to 16:32 UTC), the twenty-four that involve run 4 in two (22:14 to 22:26 UTC). A kept verdict was never asked twice. Run 4's frames were taken with the check as corrected after that run (it waits for a heavy page to arrive before it counts its four seconds); the other sites' frames are the ones taken earlier, which that correction does not change for a page whose every frame was already settled (runs 2 and 3) or that never settles (run 1, the stock agent's).

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
| run3 vs stock | every position composed | run3 6 | run3 |
| run3 vs stock | explains the project | run3 6 | run3 |
| run3 vs stock | craft of the 3d | run3 6 | run3 |
| run3 vs stock | type and layout | run3 6 | run3 |
| run3 vs stock | overall | run3 6 | run3 |
| run4 vs stock | every position composed | run4 6 | run4 |
| run4 vs stock | explains the project | run4 6 | run4 |
| run4 vs stock | craft of the 3d | run4 6 | run4 |
| run4 vs stock | type and layout | run4 6 | run4 |
| run4 vs stock | overall | run4 6 | run4 |

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
| run1 vs stock | run1 | run1 1, stock 2 |
| run1 vs stock | stock | run1 3 |
| run2 vs run3 | run2 | run3 3 |
| run2 vs run3 | run3 | run2 3 |
| run2 vs run4 | run2 | run4 3 |
| run2 vs run4 | run4 | run4 3 |
| run2 vs stock | run2 | run2 3 |
| run2 vs stock | stock | run2 3 |
| run3 vs run4 | run3 | run4 3 |
| run3 vs run4 | run4 | run3 2, run4 1 |
| run3 vs stock | run3 | run3 3 |
| run3 vs stock | stock | run3 3 |
| run4 vs stock | run4 | run4 3 |
| run4 vs stock | stock | run4 3 |

Over all 60 calls the overall verdict went to the site shown second 37 times.

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
