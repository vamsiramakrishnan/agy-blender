# The same request without the plugin: a stock agent's site

The baseline for `../site_run2_plugin/` and `../site_run3_plugin/`: the same task, the same copy of the Straitjacket
repository, the same reference notes and installed packages, given to the same agent with no plugin attached. This
folder is what was asked, what came back and what it cost. The comparison is in `../../ROUND_EIGHT.md`.

## What ran

- The Antigravity SDK's agent (google-antigravity 0.1.24), headless, in the comparison harness's sandbox, model
  `gemini-4-neon`, no plugin and no skills of this project. Blender 5.3, Node and a Chromium were in the sandbox, as
  for the plugin's runs. Image generation disabled. One session.
- `task.md` is the task in its stock reading: the same text as the plugin's runs without the sentences about the
  plugin. `run.json` holds the same digests of the project copy, the notes and the packages as runs 2 and 3.
- It was the fifth attempt. The model service refused the first four (`../round8/baseline_attempts.json`).

| | |
|---|---|
| wall time | 53.2 min. A model call was in flight for 50.0 of them: 33.9 min in 37 answered calls (33 s at the median, five over two minutes), and 16.1 min in eight calls that failed, three of them dropped after 300 s or more |
| time inside tool calls | 3.3 min (run 3: 8.3, run 2: 15.8) |
| model calls | 37 answered |
| tokens | 4,960,373: input 4,428,044 (708,281 of it served from cache), output 91,747, thinking 18,542, and 422,040 held for the failed calls, whose real use is not known |
| images | 0 |
| tool calls | 91: 48 files or pictures opened, 22 files written, 15 commands, 6 edits |

## What came back

- `report.md`: the agent's own final report, as written.
- `site_src/`: the site's source as the agent left it: `index.html`, `vite.config.js`, 12 files in `src/` (4,036
  lines), the Blender script that builds its five models (`blender/build_assets.py`, 866 lines) and its two scripts
  (`scripts/`). Left out by the collector: the five models and one picture (named in `run.json`). Left out by hand,
  as for run 3: the host logos from the Straitjacket repository, which the agent only copied.
- `site_check.txt`: this project's check of the built site, made after the run from a copy of the folder with the
  check as it is now. The stock agent did not have this tool.
- `record.json`, `events.jsonl`: the harness's record and every tool call with its result. Local paths are
  replaced by labels.
- The built site is not in the repository (2.3 MB in 57 files). It is published as a hosted page, changed only as
  the host requires: the page as a fragment, and the five models rewritten losslessly as `.gltf` JSON with their
  names changed in the one script that loads them (`../site_run2_plugin/harness/`). The hosted form was checked
  against the agent's build: the same 132 draw calls and 32,761 triangles, no failed request.

The site is one fixed three.js canvas behind seven "acts" of text, written as a React page. Five models were built
by one Blender script (30,972 triangles together, 0.5 MB, materials without textures), and four shaders draw a
coloured dome behind the scene, a stream of 4,200 particles, ribbons between the stations and a glass-like beam.
A post-processing pass adds bloom, a vignette and filmic tone mapping. Three of the acts have controls that change
the 3D.

## How the run went

- Minutes 0 to 7: the first model call was refused, the second dropped after 300 s, the third took 143 s.
- Minutes 7 to 20: reading. The project's README and eight of its documents, among them its design page, two of
  its evaluation records, and the reference notes.
- Minute 24: one Blender script for the five models. Blender ran it in 3 s. A second script passed the models
  through glTF-Transform.
- Minutes 27 to 35: the site, in fourteen files, and the first build.
- Minutes 35 to 43: a screenshot script of its own (334 lines), run for 64 s, and eleven pictures opened.
- Minutes 46 to 53: two rounds of changes, each followed by the script and the pictures (14, then 3).

## What the check and the frames show that the report does not say

- **Without WebGL the page is empty.** It shows no text at all and throws "Error creating WebGL context". The
  report says nothing about a browser without WebGL.
- **Reduced motion is not honoured.** The page asks for the setting and has a CSS rule for it; the picture keeps
  changing.
- **It never rests.** It redraws every frame while nothing is touched, with up to 132 draw calls in a frame.
- **Text lies over the 3D.** On a desktop in four of the nine frames; on a phone the text covers most of the scene
  at most steps (`../round8/stock_phone.jpg`).
- **It used what the project's design page rules out.** The agent read `docs/VISUAL-DESIGN.md` at minute 9. That
  page says "Do not add gradients, glow, glass effects, or decorative colour" and "Colour is semantic, never
  atmospheric". The site has a coloured gradient behind every act, a bloom pass and a glass shader, and its report
  calls this "grounded in" that page.
- **Its numbers name files, in a form the check cannot compare.** 35 numbers and 4 code specimens are on the page.
  Most receipts show a file name in small type. The eleven numbers looked up by hand are in the project's files;
  the others were not checked. One line of copy (a description of a harness) is in no file of the project.
- **One uncaught error in one of three checks** ("Cannot read properties of undefined (reading 'curve')"): the
  harness's check as the run ended saw it, two later checks did not.
- **The machine was not quiet.** For the run's first 20 minutes other tests ran on it, and at minute 35 a file
  search ran for two minutes. The run's tools took 3.3 minutes in all.

## The blind verdicts

A general model judged this site against each of the plugin's three, six times each, without being told which was
which (`../round8/site_compare_corrected/`). It preferred run 2's and run 3's site in all twelve verdicts, on every
criterion, and run 1's in four of six. Its reasons are about a floor, contact shadows and worn surfaces on the
plugin's side, "glowing geometric primitives floating in space" on this one, and text over the 3D.
