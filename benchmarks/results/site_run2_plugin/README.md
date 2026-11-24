# A whole website from Antigravity, second run: Neon with the plugin of round eight

The same request as `../site_run1/`, run again after the plugin was changed for what run 1's logs showed
(`../../ROUND_EIGHT.md`). This folder is what was asked, what came back and what it cost. Nothing here is graded.

## What ran

- The Antigravity SDK's agent (google-antigravity 0.1.24), headless, in the comparison harness's sandbox, model
  `gemini-4-neon`, this plugin attached in its default mode from this branch at commit 5e3c0a8. Image generation
  disabled. One session.
- The task: `task.md`. It is the owner's sentence, where the site will live, the rules of the host (static files,
  nothing from another host, works on a phone), and the facts of the machine.
- The sandbox has no internet. The agent was given a copy of the Straitjacket repository (commit 50f8e60), saved
  text and pictures of the reference site, Node 22, a headless Chromium, and these packages already installed:
  three 0.187.0, gsap 3.15.1, lenis 1.3.28, postprocessing 6.39.6, n8ao 2.1.0, troika-three-text 0.53.1,
  three-custom-shader-material 6.4.0, three-mesh-bvh 0.9.18, camera-controls 3.1.2, detect-gpu 5.0.70,
  maath 0.10.8, motion 14.14.0, lygia 1.4.1, react 19.3.1 with @react-three/fiber 9.8.2, drei 10.7.9 and
  @react-three/postprocessing 3.2.2, vite 8.3.8, lil-gui, stats-gl, three font packages, and the six compression
  packages the plugin's installer uses.
- The run was started by `benchmarks/site_task.py run --live --arm plugin`; `run.json` holds the command, the
  seed's digests and the options. The stand-in for the user approved the opening of each asset session and each
  install into the run's own workspace, and nothing else.

| | |
|---|---|
| wall time | 102.6 min, about 87 of them waiting for the model service (median 44 s per call; four calls dropped after 300 s each) |
| model calls | 64 answered |
| tokens | 8,042,453: input 7,425,166 (1,173,931 of it served from cache), output 79,974, thinking 34,148, and 503,165 held for the four dropped calls, whose real use is not known |
| images | 0 |
| tool calls | 128, of them 30 to the plugin |

## What came back

- `report.md`: the agent's own final report, as written.
- `site_src/`: the site's source as the agent left it: `STORYBOARD.md`, `index.html`, `vite.config.js` and seven
  files in `src/` (about 2,000 lines), and what the plugin's installer wrote for the three props (`src/agy3d/`,
  `public/agy3d/*/collision.json`). Left out by the collector: models, textures and other binary files (named in
  `run.json`). Left out by hand, because they are other projects' files that the agent only copied: the benchmark
  tables of the detect-gpu package, the host logos from the Straitjacket repository, and three.js's texture
  transcoder.
- `site_check.txt`: the plugin's check of the built site, made after the run from a copy of the folder.
- `record.json`, `events.jsonl`: the harness's record and every tool call with its result. Local paths are
  replaced by labels.
- The built site is not in the repository (7.2 MB). It is published as a hosted page.

Three objects were made through the plugin, all for `web_three` at a 1024 px atlas: `capture_gate`,
`artifact_vault` and `anchor_clamp`. Each was delivered and then installed by `install_into_project`: 11 checks
passed in a browser with the project's three.js and one was skipped (the React component is bundled with the
project's esbuild, and a project on Vite 8 has none; an earlier version of this note said 12 of 12). The site is one fixed three.js canvas and a
camera that travels along a bench with seven chapters, the text panel pinned beside it.

## What was changed for hosting, and by whom

By hand, because the page host requires it, and nothing else:

- The host supplies the page's outer tags, so the built `index.html` was turned into a fragment
  (`harness/to_artifact.py`).
- The host serves neither `.glb` nor `.ktx2` files. Each model was rewritten losslessly as `.gltf` JSON
  (`harness/glb_to_gltf.py`) and the three file names were changed in the built script. The one prop the installer
  had compressed to KTX2 (`anchor_clamp`) was installed again uncompressed for the hosted copy.

The measures in `site_check.txt` and in the round's report are of the agent's own build, not of the hosted copy.

## What the transcript shows that the report does not say

- **The page lost its best receipt to a fault of the plugin's check.** The agent first showed Straitjacket's real
  digest output with lines left out, and the check at minute 9 rightly refused it. At minute 11 the agent replaced
  it with the first five lines, copied exactly from `evals/field-needle-record.json`. The check at minute 38
  refused that as well, because it wanted a quote to be at least half of the value it quotes. The agent then put
  command lines from the README in its place. The check is fixed (`../../ROUND_EIGHT.md`); this site was built
  before the fix.
- **The props were delivered as first drawn.** Each took about four modelling calls and one preview. The plugin's
  review scored them 1.8 of 4 (an uncalibrated judge), reported that as findings for a person, and the agent went
  on to the next prop.
- **One delivery was refused** by the Blender check (two faces in the same plane on `artifact_vault`) after 78 s,
  and passed after one fix.
- **Each install took about two minutes**, almost all of it spent encoding compressed textures that were then
  not kept. Fixed after the run; the same install now takes 24 s.
- **The install's answer did not reach the agent.** It was 10,292 characters, so the host saved it to a file and
  showed a title. The agent found the loader by reading the files. Fixed after the run.
- **Twice the agent opened the next prop's session in the same turn as a slow call**, and the plugin answered the
  quick call only when the slow one was done (117 s and 78 s).
- `harness/analyse_run.py events.jsonl record.json` prints the time split and the plugin calls this summary is
  drawn from.
