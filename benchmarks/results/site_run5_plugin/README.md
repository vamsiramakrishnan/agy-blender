# A whole website from Antigravity, fifth run: after the corrections made for run 4

The same request as `../site_run2_plugin/` to `../site_run4_plugin/`, run again after the plugin was corrected for
what the fourth run showed (`../../ROUND_EIGHT.md`). This folder is what was asked, what came back and what it cost.
Nothing here is graded.

## What ran

- The Antigravity SDK's agent (google-antigravity 0.1.24), headless, in the comparison harness's sandbox, model
  `gemini-4-neon`, this plugin attached in its default mode from commit bcfbf13. Image generation disabled. One
  session.
- The task, the copy of the Straitjacket repository (commit 50f8e60), the reference notes and the installed packages
  are those of runs 2 to 4, file for file: `run.json` holds the same digests. The plugin is what differs.
- The stand-in for the user approved the opening of each asset session and each install into the run's own
  workspace, and nothing else.

| | |
|---|---|
| wall time | 39.4 min. The model service was fast: 25.5 min in 84 answered calls (10 s at the median, one over two minutes), none failed |
| time inside tool calls | 18.4 min, 16.4 of them in the plugin's tools; deliveries and installs 6.4 min |
| model calls | 81 by the agent, and 3 by the plugin's review of the props |
| tokens | 11,187,436: input 11,057,211 (6,653,939 of it served from cache), output 91,857, thinking 38,368 |
| images | 0 |
| tool calls | 132, of them 30 to the plugin |

## What came back

- `report.md`: the agent's own final report, as written.
- `site_src/`: the site's source as the agent left it: `STORYBOARD.md`, `index.html`, `vite.config.js` and eight
  files in `src/` (2,080 lines), and what the plugin's installer wrote for the three props (`src/agy3d/`,
  `public/agy3d/*/collision.json`). Left out by the collector: models, textures and other binary files (named in
  `run.json`). Left out by hand, because they are other projects' files that the agent or the installer only
  copied: the texture transcoder and the host logos from the Straitjacket repository.
- `site_check.txt`: the plugin's check of the built site, made by the harness after the run, with the check as the
  agent had it.
- `record.json`, `events.jsonl`: the harness's record and every tool call with its result. Local paths are
  replaced by labels.
- The built site is not in the repository (7.5 MB in 77 files). It is published as a hosted page.

The site is one fixed three.js canvas and a camera that visits three open machines and a field of seven lanes in
seven chapters, following one real log through them: a press, a vault with a drawer out, a caliper. The log is 6,053
slabs, one for every 50 tokens, which the press turns into 11. The text panel is at the left on a desktop and below
the scene on a phone. Three objects were made through the plugin, all for `web_three` at a 1024 px atlas:
`capture_press` (61 parts, 4,014 triangles), `artifact_vault` (46, 2,576) and `anchor_caliper` (44, 2,838), in
eleven modelling calls and three previews. One modelling call was an error of the agent's (a lathe profile), one was
rolled back (six draw calls where four are allowed), and one delivery was refused (below). The plugin's
uncalibrated review put the props at 1.77, 2.19 and 2.14 against a target of 3.4. The seven lanes are plain boxes
made in the page's own code.

## What was changed for hosting, and by whom

By hand, because the page host requires it:

- The host does not serve KTX2 textures, which the agent's install of the vault produced. For the hosted copy the
  vault was installed again from the package the agent delivered, without compression (`agy3d install --compress
  never`), and the site was built again with the project's own Vite. Nothing the agent wrote was changed; the
  installer's generated loader file and the vault's texture files are what differ. Checked against the agent's build
  with the plugin's check: the same 60 draw calls and 188,215 triangles, no failed request, and in the nine desktop
  frames at most 0.08 % of the pixels differ by more than 24 of 255 levels.
- The built `index.html` was turned into a fragment and each of the seven models was rewritten losslessly as `.gltf`
  JSON, with the names changed in the three loader scripts (`../site_run2_plugin/harness/`).

The hosted copy downloads 5.7 MB before the rewrite as JSON, where the agent's build downloads 5.9. The measures in
`site_check.txt` and in the round's report are of the agent's own build.

## What the blind comparison says

Six verdicts on each pair, three in each order (`../round8/site_compare_corrected/verdicts.md`). This site is
preferred to the stock agent's 6 to 0, to run 1's 6 to 0 and to run 2's 5 to 1. Against run 3 it is 3 to 3, each
order choosing the site shown second. **Run 4's site is preferred to it 6 to 0.**

| against | every position composed | explains the project | craft of the 3D | type and layout |
|---|---|---|---|---|
| run 4 | run 4, 6 to 0 | run 4, 6 to 0 | run 4, 4 to 2 | run 5, 5 and a tie |
| run 3 | run 3, 6 to 0 | run 5, 6 to 0 | 3 to 3 | run 3, 3 and three ties |
| run 2 | run 5, 5 to 1 | run 5, 6 to 0 | run 2, 6 to 0 | run 5, 6 to 0 |
| run 1 | run 5, 6 to 0 | run 5, 6 to 0 | 3 to 3 | run 5, 6 to 0 |
| stock | run 5, 6 to 0 | run 5, 6 to 0 | run 5, 6 to 0 | run 5, 6 to 0 |

So the corrections made after run 4 did not make a better site than run 4. What the verdicts name is below.

## What the transcript and the verdicts show that the report does not say

- **The check said "0 things" three times, and the judge names one frame every time.** At 62 % of the scroll the
  text of chapter 04 is fully shown while the camera is half way from chapter 03's cabinet to chapter 04's caliper:
  the cabinet is half under the text card, and the caliper and its label are cut by the right edge ("lets the text
  card collide directly with the 3D cabinet in frame 6 and cuts off the relocator label"). The page said its camera
  was moving there, and since the corrections made for run 4 the check says nothing at such a position. That
  correction was wrong, and so is the starter it protected: it shows a chapter's text when the camera starts its
  move, not when it arrives. Run 4's site has the same timing; its stations stand on one line, so a frame in
  transit still looks composed. Both are being corrected (`../../ROUND_EIGHT.md`).
- **A delivery was refused for a fault of the plugin's own, and the agent took the vault's lights off to pass.**
  "NOT DELIVERED: web_three import: LOD2 materials == LOD0 materials: artifact_vault_atlas: emissiveIntensity 0.35
  != LOD0 1". The vault had two plain materials and two signal lights of the same emissive strength; each pair
  became one glTF material and the plugin named both `artifact_vault_atlas`, which the check compares by name. The
  next call removed the lights' emission, and the caliper after it was made without any. Corrected: a second merged
  group has its own name.
- **Two of the three props shipped PNG textures.** Their roughness/metal maps as lossy WebP put 1.07 % and 2.02 % of
  a close-up's pixels over the tolerance (the limit is 1 %), and the rule then kept every map as PNG: 1.65 and
  1.48 MB where WebP was 0.30 and 0.22 MB. Lossy WebP halves the colour resolution at every quality, and in that
  map the metal mask is colour. Corrected: that one map ships as lossless WebP. Installed into a copy of this
  project, the two corrected packages bring the page from 5.9 MB to 3.8 MB, with the same picture and the same
  answer from the check.
- **The cabinet's metal is blotchy, and the words did not ask for it.** The agent wrote "brushed machined aluminum,
  lightly scuffed". The surface generator read "scuffed" (the strongest word won, so "lightly" could not lower it)
  and laid 40 cm clouds of colour and roughness over the panels, which paint had lost earlier for the same
  complaint and bare metal had not. Three of the craft verdicts against this site say "blotchy". Corrected: both.
- **The props stand out from the ground, and now read as flat and grey.** After run 4 the skill told the agent to
  keep large surfaces away from the page's colour, and it did: steel and aluminium at `#AEB6C0` to `#B8C1CC`, paint
  at `#7A838E` and `#858E9A`. The check's measure of the subject against its ground is over its limit at eight of
  nine steps (run 4: two of nine). Of the sixteen craft verdicts that go against this site, nine say "flat" or
  "flatter" (with "matte" or "grey"), three "blotchy", and four still call the scene or its textures dark. Run 2's
  site is preferred on craft 6 to 0, as it is to run 4's. The look of the 3D has not improved since run 2.
- **The fourth station is boxes.** The lanes of the seven hosts were made in the page's code as plain boxes, the
  coloured ones unshaded ("flat unshaded blocks on the floor", "abstract blocks in frame 8"), where run 4 spent a
  prop on two gate bars over seven lanes.
- **The page came first, as the skill asks.** The starter was copied, built and checked with stand-ins at minute
  7.5. That check named twelve things; the next was a page that did not start (a label for a prop not yet in the
  table), then two things, then none, three times.
- **Type and layout hold up.** On that criterion this site is preferred to run 4's (5, and a tie) and to run 2's
  (6 to 0); run 3's is preferred to it (3, and three ties). The navigation names its chapters and no button has
  lost its spaces.
- **It cost what run 4 cost**: 81 model calls and 11.2 M tokens.
- `../site_run2_plugin/harness/analyse_run.py events.jsonl record.json` prints the time split and the plugin calls
  this summary is drawn from.
