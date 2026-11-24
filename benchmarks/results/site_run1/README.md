# A whole website from Antigravity: Neon with this plugin, first run

The owner asked for a website for Straitjacket (github.com/vamsiramakrishnan/straitjacket), generated entirely by
Antigravity using the Neon model and this plugin, so that what it does poorly with three.js and current web libraries
can be added to the plugin. This folder is what was asked, what came back and what it cost. Nothing here is graded.

## What ran

- The Antigravity SDK's agent (google-antigravity 0.1.24), headless, in the comparison harness's sandbox, model
  `gemini-4-neon`, this plugin attached in its default mode (from main at 4daa8f6). Image generation disabled.
- The task is the owner's sentence plus the facts of the machine: `task_session1.md`.
- The sandbox has no internet. The agent was given a copy of the Straitjacket repository, saved text and pictures of
  the reference site, Node 22, and these packages already installed: three 0.170.0, gsap 3.15.1, lenis 1.3.27,
  vite 7.3.8. A headless Chromium was on the machine.
- A second session (`task_session2.md`) gave the agent a rule of the publishing host that the first task left out
  (no file over 15 MB; one model was 17.4 MB) and asked it to fix its own site.

| | session 1 | session 2 |
|---|---|---|
| wall time | 69 min | 36 min |
| model calls | 139 (133 on gemini-4-neon, 6 small ones by the runtime on gemini-2.5-flash-lite) | 60 |
| tokens, input + output + thinking | 14,569,612 (6,555,109 of the input cached) | 7,149,932 (4,736,701 cached) |
| images | 0 | 0 |
| tool calls | 181 | 93 |

## What came back

- `report_session1.md`, `report_session2.md`: the agent's own final reports, as written.
- `site_src/`: the site's source as the agent left it (about 3,600 lines; Vite project). The built site is not in
  the repository (22.8 MB, of which 21.9 MB is three models); it is published as a hosted page.
- `record_session*.json`, `events_session*.jsonl`: the harness's record and every tool call with its result. Local
  paths are replaced by labels.

Three objects were made through the plugin: containment_gate and anchor_caliper for `web_three`, harness_router for
`godot_4`. About the first 45 minutes of session 1 went to them. The site is one fixed three.js canvas with a camera on a spline through three
stations, scrubbed by GSAP ScrollTrigger and smoothed by Lenis, six custom shader materials, and six sections of
HTML with interactive controls. The agent checked its own pages with headless Chromium screenshots on a desktop and
a phone size and revised it several times.

## What was changed for hosting, and by whom

By the agent, in session 2: the 17.4 MB model was brought to 9.6 MB by scaling the object to 0.65 inside the plugin
so that its texture sheet came out at 2048 px instead of 4096, and loading it at 1/0.65 in the site.

By hand, because the host requires it, and nothing else:

- The host supplies the page's outer tags, so `<!doctype>`, `<html>`, `<head>`, `<body>` and the charset and
  viewport metas were removed (`harness/to_artifact.py`).
- The host does not serve `.glb` files. Each model was rewritten losslessly as `.gltf` JSON with its textures as PNG
  files (`harness/glb_to_gltf.py`), and the three file names were changed in the built script.

## What the transcripts show that the reports do not say

- The plugin gave the agent no direct way to make a model lighter for the web. Its compression needs WebAssembly
  decoders, which the second task ruled out; `agy3d` was not on the agent's PATH; and `open_asset_session` takes a
  triangle budget but no texture size, texel density or byte budget. The rescaling above is a workaround.
- The plugin made levels of detail for every object; the site loads only the full one.
- Several specifics on the page are invented although the page presents them as receipts: the "digest specimen"
  (a `WARN quiet-needle` line and a span id that are in no file; the real line is logged at INFO), the anchor example
  (lines 40:42 of `src/ctx/refs.py` with the hash from a diagram), and the handles in the routing table. The totals
  it quotes from the receipts are right.
- The page uses glow, particles and a gradient dome. Straitjacket's visual design page, which the agent read, rules
  those out.
- There is no handling of the reduced-motion preference.
