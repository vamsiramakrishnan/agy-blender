# A whole website from Antigravity, fourth run: after the review of run 3 and the blind verdicts

The same request as `../site_run2_plugin/` and `../site_run3_plugin/`, run again after the plugin was corrected for
what the third run, an independent review of its three.js and the blind comparison of the sites showed
(`../../ROUND_EIGHT.md`). This folder is what was asked, what came back and what it cost. Nothing here is graded.

## What ran

- The Antigravity SDK's agent (google-antigravity 0.1.24), headless, in the comparison harness's sandbox, model
  `gemini-4-neon`, this plugin attached in its default mode from commit 1b81144. Image generation disabled. One
  session.
- The task, the copy of the Straitjacket repository (commit 50f8e60), the reference notes and the installed packages
  are those of runs 2 and 3, file for file: `run.json` holds the same digests. The plugin and the state of the model
  service are what differ.
- The stand-in for the user approved the opening of each asset session and each install into the run's own
  workspace, and nothing else.

| | |
|---|---|
| wall time | 38.9 min. The model service was fast that evening: 24.9 min in 72 answered calls (12 s at the median, one over two minutes), none failed. Run 3's median was 45 s |
| time inside tool calls | 20.2 min, 16.2 of them in the plugin's tools (run 3: 8.3). Three modelling calls each waited behind an install made in the same turn, about three and a half minutes in all |
| model calls | 69 by the agent, and 3 by the plugin's review of the props |
| tokens | 10,460,760: input 10,316,675 (4,222,175 of it served from cache), output 101,145, thinking 42,940 |
| images | 0 |
| tool calls | 123, of them 30 to the plugin |

## What came back

- `report.md`: the agent's own final report, as written.
- `site_src/`: the site's source as the agent left it: `STORYBOARD.md`, `index.html`, `vite.config.js` and eight
  files in `src/` (2,119 lines), and what the plugin's installer wrote for the three props (`src/agy3d/`,
  `public/agy3d/*/collision.json`). Left out by the collector: models, textures and other binary files (named in
  `run.json`). Left out by hand, because they are other projects' files that the agent or the installer only
  copied: the benchmark tables of the detect-gpu package, the texture transcoder, and the host logos from the
  Straitjacket repository.
- `site_check.txt`: the plugin's check of the built site, made after the run from a copy of the folder, with the
  check as it is after this run's findings about it (below).
- `record.json`, `events.jsonl`: the harness's record and every tool call with its result. Local paths are
  replaced by labels.
- The built site is not in the repository (9.4 MB in 92 files). It is published as a hosted page.

The site is one fixed three.js canvas and a camera that travels along a bench of three open machines in six
chapters, following one real log through them: a press, a vault with a drawer out, a manifold of seven lanes. The
log is 6,053 slabs, one for every 50 tokens, which the press turns into 11. The text panel is at the left on a
desktop and below the scene on a phone. Three objects were made through the plugin, all for `web_three` at a
1024 px atlas: `containment_press` (69 parts, 6,702 triangles), `evidence_vault` (58, 3,406) and `anchor_manifold`
(65, 5,606). Each took two to seven modelling calls and one preview. One delivery was refused (36 pairs of
triangles lying in the same plane) and fixed in the next call. The plugin's uncalibrated review put them at 2.25,
2.35 and 2.48 against a target of 3.4.

## What was changed for hosting, and by whom

By hand, because the page host requires it:

- The host does not serve KTX2 textures, which the agent's installs produced. For the hosted copy the three props
  were installed again from the packages the agent delivered, without compression (`agy3d install --compress
  never`), and the site was built again with the project's own Vite. Nothing the agent wrote was changed; the
  installer's generated loader files and the texture files are what differ. Checked against the agent's build with
  the plugin's check: the same 119 draw calls and 188,211 triangles, no failed request, and in the nine desktop
  frames at most 0.20 % of the pixels differ by more than 24 of 255 levels.
- The built `index.html` was turned into a fragment and each of the five models was rewritten losslessly as `.gltf`
  JSON, with the names changed in the three loader scripts (`../site_run2_plugin/harness/`).

The hosted copy downloads 3.8 MB before the rewrite as JSON, where the agent's build downloads 7.4 (see below). The
measures in `site_check.txt` and in the round's report are of the agent's own build.

## What the transcript shows that the report does not say

- **The page came first, as the skill asks.** The starter was copied, built and checked with stand-ins at minute 7
  (run 3: minute 45). That first check named eleven things; the next three named two, one and one.
- **The picture shows the mechanism.** The briefs the agent gave the three props begin "An open-throat ...", "An
  open-drawer ...", "An open seven-lane ...", and the storyboard follows one log from station to station. In the
  blind comparison this site is the better explanation in all 24 verdicts.
- **The scene is dark.** The props are steel at `#242931` and three quarters metal, on a ground of `#0D0F12`, under
  the starter's light. At six of the nine desktop steps under a tenth of the canvas differs from the background.
  Every blind verdict on craft that goes against this site says so.
- **The page downloads twice what it needed to, and the skill told it to.** The skill said to install with
  `compress: "always"` where the props' textures pass 32 MB of graphics memory, and that it costs a third of a
  megabyte. It cost 2.6 MB of files and a 0.6 MB transcoder, for 38 MB less on the graphics card: the page
  downloads 7.4 MB, and the same site with the default downloads 3.8. The check named the size in all three later
  checks, and the agent's report names it as what could not be done. The paragraph is corrected.
- **Two positions where the subject is out of the frame went unseen.** The agent's checks took twelve steps. A check
  at nine steps names six such things at two positions: at 38 % the text has changed to the vault's chapter and the
  camera is still on the press, and at 75 % the close shot of the manifold is larger than the frame. Twelve steps
  fell on neither.
- **The look-first line was shown and followed.** Two deliveries were more than a point under their target. The
  agent opened each render in a turn of its own before it installed the prop, and went on without a second round.
- **The check's last frame had a fault of the check's own.** At the end of the page its picture showed the first
  chapter's panel over the last one's, marked "still moving", in all four of the agent's checks. The agent left it
  alone. A visitor does not see it, and the check is corrected (`../../ROUND_EIGHT.md`).
- **A button reads "FOLLOW ONE20,001-LINE LOG".** The number carries its source in an element of its own, inside a
  button laid out with flex, which drops the spaces around it. The blind judge names it in five of six verdicts on
  type and layout against run 3.
- `../site_run2_plugin/harness/analyse_run.py events.jsonl record.json` prints the time split and the plugin calls
  this summary is drawn from.
