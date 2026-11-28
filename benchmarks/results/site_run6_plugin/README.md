# A whole website from Antigravity, sixth run: after the corrections made for run 5

The same request as `../site_run2_plugin/` to `../site_run5_plugin/`, run again after the plugin was corrected for
what the fifth run showed (`../../ROUND_EIGHT.md`). This folder is what was asked, what came back and what it cost.
Nothing here is graded.

## What ran

- The Antigravity SDK's agent (google-antigravity 0.1.24), headless, in the comparison harness's sandbox, model
  `gemini-4-neon`, this plugin attached in its default mode from commit 3cce7c1. Image generation disabled. One
  session.
- The task, the copy of the Straitjacket repository (commit 50f8e60), the reference notes and the installed packages
  are those of runs 2 to 5, file for file: `run.json` holds the same digests. The plugin is what differs.
- The stand-in for the user approved the opening of each asset session and each install into the run's own
  workspace, and nothing else.

| | |
|---|---|
| wall time | 49.8 min. The model service was slower than for run 5: 37.4 min in 70 answered requests (20 s at the median, two over two minutes), and three more were turned away ("high demand") and asked again |
| time inside tool calls | 21.5 min, 16.7 of them in the plugin's tools; deliveries and installs 5.3 min |
| model calls | 67: 64 by the agent and 3 by the plugin's review of the props |
| tokens | 8,963,368: input 8,839,730 (2,910,995 of it served from cache), output 83,409, thinking 40,229 |
| images | 0 |
| tool calls | 144, of them 29 to the plugin |

## What came back

- `report.md`: the agent's own final report, as written.
- `site_src/`: the site's source as the agent left it: `STORYBOARD.md`, `index.html`, `vite.config.js` and ten
  files in `src/` (2,344 lines without the storyboard), and what the plugin's installer wrote for the three props
  (`src/agy3d/`, `public/agy3d/*/collision.json`). Left out by the collector: models, textures and other binary
  files (named in `run.json`). Left out by hand, because they are other projects' files that the agent only copied:
  the host logos from the Straitjacket repository and the benchmark tables of `detect-gpu`.
- `site_check.txt`: the plugin's check of the built site, made by the harness after the run, with the check as the
  agent had it.
- `record.json`, `events.jsonl`: the harness's record and every tool call with its result. Local paths are
  replaced by labels.
- The built site is not in the repository (6.7 MB in 111 files). It is published as a hosted page.

The site is one fixed three.js canvas and a camera that visits three machines on one bench in six chapters,
following one real log through them: 302,628 tokens as 6,053 slabs, a press that turns them into a digest of 531
tokens, a rack where the addresses of edited code are found again (1,452 relocated, 2 verified, 466 refused, none
wrong), and a manifold of seven lanes for the seven hosts. The text panel is at the left on a desktop and below the
scene on a phone. Three objects were made through the plugin, all for `web_three`: `digest_press` (4,608
triangles), `anchor_rack` (4,272) and `host_manifold` (6,308), in twelve modelling calls and three previews, each
delivered once, with WebP textures and one draw call in its manifest. One modelling call was an error of the
agent's that the plugin's wording invited (a lathe profile), and one was rolled back for a fault of the plugin's own
(below). The plugin's uncalibrated review put the props at 1.76, 1.26 and 1.50 against a target of 3.4, lower than
run 5's (1.77, 2.19, 2.14).

## What was changed for hosting, and by whom

By hand, because the page host requires it: the built `index.html` was turned into a fragment and each of the seven
models was rewritten losslessly as `.gltf` JSON, with the names changed in the three loader scripts
(`../site_run2_plugin/harness/`). Nothing the agent wrote was changed, and no prop had to be installed again: this
run's textures are WebP, which the host serves. Checked against the agent's build with the plugin's check: the same
58 draw calls and 216,175 triangles, no failed request, and in the nine desktop frames no pixel more than 24 of 255
levels apart. The hosted copy downloads 4.8 MB (the models as JSON), where the agent's build downloads 4.5. The
measures in `site_check.txt` and in the round's report are of the agent's own build.

## What the blind comparison says

Six verdicts on each pair, three in each order (`../round8/site_compare_corrected/verdicts.md`). This site is
preferred to the stock agent's 6 to 0, to run 1's 6 to 0 and to run 5's 4 to 2. Against run 2 it is 3 to 3.
**Run 3's is preferred to it 4 to 2 and run 4's 6 to 0.**

| against | every position composed | explains the project | craft of the 3D | type and layout |
|---|---|---|---|---|
| run 4 | run 4, 6 to 0 | run 4, 6 to 0 | 3 to 3 | run 6, 6 to 0 |
| run 3 | run 3, 4 to 2 | 3 to 3 | run 3, 3 to 2 and a tie | run 3, 2 to 1 and three ties |
| run 5 | run 6, 6 to 0 | run 5, 6 to 0 | run 6, 4 to 2 | run 6, 6 to 0 |
| run 2 | run 6, 6 to 0 | run 2, 5 to 1 | run 2, 6 to 0 | run 6, 6 to 0 |
| run 1 | run 6, 6 to 0 | run 6, 6 to 0 | run 1, 5 to 1 | run 6, 6 to 0 |
| stock | run 6, 6 to 0 | run 6, 6 to 0 | run 6, 6 to 0 | run 6, 6 to 0 |

So the corrections made after run 5 gave a site that is better composed and better set than run 5's and still not
as good as run 4's. Three things the verdicts name were then put to the judge one at a time, on this site, with
nothing else changed (`../round8/floor_compare/`, `../round8/text_timing/`); what they showed is below.

## What the transcript, the verdicts and the controlled comparisons show that the report does not say

- **The floor is as dark as the page, and that is the plugin's doing.** Of the nineteen craft verdicts against this
  site, ten say "flat", seven "small", four "dark", four "matte", four "toy". The agent's `--ground` is the design's
  card colour, `#15181D`, as the starter's was a neighbour of its page colour and as the guide asked ("the ground
  comes out as the page's ground colour"); on screen it is at 19 of 255 on a page at 16. Rebuilt with the floor at
  61 and nothing else changed, the site is preferred to itself in five of six verdicts, and at 91 in six of six
  ("the near-black floor swallows the shadows and leaves the dark navy machines floating"); 61 is preferred to 91
  in six of six. Runs 4 and 5 had the same floor. Corrected: the starter's ground is a lit slate and the guide says
  what a ground is for.
- **Every composition verdict against it names frames 4 and 7, where there is no text card.** Those are the camera's
  moves, and since run 5 the starter shows no card during a move ("drops its text overlays mid-transition"). Put
  to the judge on this site: a card that stays up through the move loses to no card in six of six ("the 3D anchor
  rack is not covered by the overlay"). The same question on run 4's site went the other way, five of six for the
  card kept up, so the rule is not settled (`../round8/text_timing/`). Seven of the ten also name frame 5, where the card lies over a machine that is
  not the shot's subject, and three name frame 8, where a machine in the foreground is cut by the frame. The check
  names neither.
- **A card's text is half faded wherever a visitor stops just after the camera arrives, and the last card is all
  but empty where its menu link lands.** The agent tied each card's lines to the scroll (a GSAP stagger with
  `scrub`, `site_src/src/entry.js`), as the guide's example showed, over the 0.16 of the chapter after the
  arrival. The photographed steps fell beside those stretches, so neither the check nor the judge saw them; when
  one of the controlled builds moved a stretch onto a step, the judge chose the other build in six of six, twice,
  each time for the "half-faded, unreadable text card". Read in a browser, this site's cards are part way from 35 %
  to about 50 % of chapters two to five. In the last chapter the guide's own sum is wrong as well (it used the
  chapter's height where the starter's last chapter runs on its height less the window): the card of the call to
  action is empty from 35 % to 64 %, not whole before 95 %, and the menu's link lands at 70 %
  (`../round8/text_timing/run6_card_lines.txt`). Corrected: the example is a timed entrance, and the starter says
  where on the page a story time is.
- **A modelling call was rolled back for six draw calls where the package ships one.** "ROLLED BACK TIER1 FAIL:
  draws 6>4", for a prop of six opaque materials; run 5 lost a call the same way. The fast check counted material
  names, the packager merges every opaque material into one atlas. Corrected: both count what ships. Replayed on
  this run's call, the corrected check accepts it.
- **The lathe error came again**: "profile crosses or touches itself" for a solid housing written with
  `closed=true`, which the schema described as "a tube, pipe or ring". Corrected: the schema says what each value
  does and the error names the fix.
- **The check told the agent its reduced-motion page takes 9.5 s to come to rest.** It rests in its first frame;
  the 9.5 s was the wait for that frame on a machine the check itself had loaded. Corrected: the check times how
  long the picture goes on changing, not the wait.
- **The machines stand close together and the camera stays back.** Of the twenty verdicts on "explains the project"
  that go against this site, eight say "cluster" and five "distant" or "further back", and run 5's is preferred on
  that criterion 6 to 0 for its "clearer linear bench from flood to press, drawer store, and relocator". This
  site has three machines where run 5's had three and a field of lanes, and no station for the local store. That is
  the agent's storyboard; nothing in the plugin asked for it.
- **The props are plainer than run 5's by the plugin's own review** (1.76, 1.26 and 1.50 against 1.77, 2.19 and
  2.14), each delivered at its first attempt without a second look.
- **The first scroll is answered**: one wheel notch at the top moves the picture by 3.4 px as the check reads it
  (run 5: 0.15 px), the starter's opening and its curve at work.
- **The page came first, as the skill asks.** The starter was built at minute 8.0 and checked at once; that check
  named three things broken and four noticed, the next ones (after the props, from minute 38) one and seven, one
  and none, none and one, and none.
- **Type and layout hold up**: preferred to runs 1, 2, 4 and 5 and to the stock agent's in six of six each.
- **It cost less than runs 3 to 5**: 67 model calls and 9.0 M tokens against 69 to 81 and 10.4 to 11.2 M.
- `../site_run2_plugin/harness/analyse_run.py events.jsonl record.json` prints the time split and the plugin calls
  this summary is drawn from.
