# A whole website from Antigravity, third run: Neon with the corrected plugin

The same request as `../site_run1/` and `../site_run2_plugin/`, run again after the plugin was corrected for what
run 2 and an independent review of its three.js showed (`../../ROUND_EIGHT.md`). This folder is what was asked, what
came back and what it cost. Nothing here is graded.

## What ran

- The Antigravity SDK's agent (google-antigravity 0.1.24), headless, in the comparison harness's sandbox, model
  `gemini-4-neon`, this plugin attached in its default mode from commit f3b2266. Image generation disabled. One
  session.
- The task, the copy of the Straitjacket repository (commit 50f8e60), the saved text and pictures of the reference
  site and the installed packages are those of run 2, file for file: `task.md` is identical and `run.json` holds
  the same digests. The plugin and the state of the model service are what differ.
- The stand-in for the user approved the opening of each asset session and each install into the run's own
  workspace, and nothing else.

| | |
|---|---|
| wall time | 104.9 min. A model call was in flight for 97.5 of them (median 45 s per answered call, 16 over two minutes; three calls failed, two of them dropped after 300 s) |
| time inside tool calls | 8.3 min (run 2: 15.8, run 1: 40.5) |
| model calls | 73 answered |
| tokens | 10,367,486: input 9,767,699 (913,395 of it served from cache), output 86,927, thinking 52,825, and 460,035 held for the three failed calls, whose real use is not known |
| images | 0 |
| tool calls | 132, of them 26 to the plugin |

## What came back

- `report.md`: the agent's own final report, as written.
- `site_src/`: the site's source as the agent left it: `STORYBOARD.md`, `index.html`, `vite.config.js` and seven
  files in `src/` (1,683 lines), and what the plugin's installer wrote for the three props (`src/agy3d/`,
  `public/agy3d/*/collision.json`). Left out by the collector: models, textures and other binary files (named in
  `run.json`). Left out by hand, because they are other projects' files that the agent only copied: the benchmark
  tables of the detect-gpu package and the host logos from the Straitjacket repository.
- `site_check.txt`: the plugin's check of the built site, made after the run from a copy of the folder, with the
  check as it is after this run's finding about its pictures (below).
- `record.json`, `events.jsonl`: the harness's record and every tool call with its result. Local paths are
  replaced by labels.
- The built site is not in the repository (4.9 MB in 89 files). It is published as a hosted page.

The site is one fixed three.js canvas and a camera that visits three instruments on a bench in six chapters, the
text panel pinned at the left on a desktop and at the bottom on a phone. Three objects were made through the plugin,
all for `web_three` at a 1024 px atlas: `containment_gate`, `artifact_vault` and `anchor_clamp`. Each took two to
six modelling calls and one preview, was delivered in one call (94 s at the longest) and installed by
`install_into_project` in 18 s or less: 11 checks passed in a browser with the project's three.js, and one was
skipped (the React component, see below).

## What was changed for hosting, and by whom

By hand, because the page host requires it, and nothing else:

- The host supplies the page's outer tags, so the built `index.html` was turned into a fragment
  (`../site_run2_plugin/harness/to_artifact.py`).
- The host does not serve `.glb` files. Each of the nine models was rewritten losslessly as `.gltf` JSON
  (`../site_run2_plugin/harness/glb_to_gltf.py`) and the nine file names were changed in the three loader scripts.

The hosted form was checked against the agent's build with the plugin's check: the same 18 draw calls and 24,368
triangles, no failed request, and in five frames compared with the agent's build no pixel differs by more than
24 of 255 levels. The measures in
`site_check.txt` and in the round's report are of the agent's own build.

## What the transcript shows that the report does not say

- **The check's subject measure did its work.** The first check, at minute 47 and with stand-in shapes, named eight
  things: props and 3D labels under the text panel, cut by the frame's edge, a phone step showing almost nothing.
  The second named five, the third and fourth none. Run 2's check said "0 things a visitor would notice" of a site
  that had 18 of these.
- **16 of the 105 minutes went on a fault of the check's own pictures.** The phone frames showed a black box over
  the 3D label. The agent compared them with the canvas-only frames, wrote six screenshot scripts of its own
  (minutes 57 to 60 and 95 to 98), which showed the same box, and changed the site's styles twice for it
  (`visibility: hidden` on inactive panels, the panel's colour at 0.99 opacity). No visitor would have seen the
  box: a Chromium that puts pages together without its GPU path leaves a WebGL canvas black opposite every opaque
  box, in screenshots only, and the container these runs happen in starts the browser that way. The check now tests
  its camera first and photographs the truth (`../../ROUND_EIGHT.md`). The two style changes are harmless and are
  still in the site.
- **The props were installed without their review being acted on.** They were delivered at 2.31, 2.14 and 2.07
  against a target of 3.4 (an uncalibrated judge). The skill says to open the render and to give a plain prop one
  more round; the delivery's answer said "do not inspect" and "NEXT: install", and the agent opened each render in
  the same turn in which it installed the prop. The answer now says "look first" in that case.
- **The first build came at minute 45.** The agent read the skill, its two guides and all eight files of the
  starter, then the project's documents, and wrote the storyboard and the whole site before building once. Run 2
  copied the starter and built it at minute 9. The first model call of this run was dropped after 300 s, and the
  first six that were answered took between 141 and 276 s each.
- **The React component of each prop was not checked.** The installer bundles it with the project's esbuild, and a
  project on Vite 8 has Rolldown instead. The check says so and is skipped; the site does not use the component.
  The same happened in run 2, whose notes here said "12 of 12 checks" where 11 passed and one was skipped.
- **One modelling call was refused**: a lathe profile that crossed itself (`tension_knob`), fixed in the next call.
- `../site_run2_plugin/harness/analyse_run.py events.jsonl record.json` prints the time split and the plugin calls
  this summary is drawn from.
