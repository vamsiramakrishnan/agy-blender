# The sites, judged blind on corrected frames

18 verdicts, six on each pair of sites (three in each order), by `gemini-4-carbon` (thinking low) through `benchmarks/site_compare.py`. The judge saw nine desktop frames of each site, at even scroll steps and 960 px wide, as "the first site" and "the second site", with the request in `request.txt`. It never saw which run made which. The frames are those of the corrected site check (no black band or box from the browser's software path; see `../../ROUND_EIGHT.md`).

- `run1`: the first run's site (`../../site_run1/`, the plugin as it was after round seven)
- `run2`: the second run's (`../../site_run2_plugin/`)
- `run3`: the third run's (`../../site_run3_plugin/`)

Seventeen of the eighteen calls were refused at least once by a busy model service (HTTP 503) and were asked again over two hours (13:25 to 15:27 UTC); a kept verdict was never asked twice.

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
| run2 vs run3 | every position composed | run3 6 | run3 |
| run2 vs run3 | explains the project | run2 4, run3 2 | run2 |
| run2 vs run3 | craft of the 3d | run2 4, tie 2 | run2 |
| run2 vs run3 | type and layout | run3 6 | run3 |
| run2 vs run3 | overall | run2 3, run3 3 | no majority |

## The overall verdict, by which site was shown first

A pair is asked three times in each order so that a judge who favours a position cancels out. Where the two orders disagree, the overall verdict followed the position and not the site.

| pair | first shown | overall verdicts |
|---|---|---|
| run1 vs run2 | run1 | run2 3 |
| run1 vs run2 | run2 | run2 3 |
| run1 vs run3 | run1 | run3 3 |
| run1 vs run3 | run3 | run3 3 |
| run2 vs run3 | run2 | run3 3 |
| run2 vs run3 | run3 | run2 3 |

Over all 18 calls the overall verdict went to the site shown second 12 times.

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
