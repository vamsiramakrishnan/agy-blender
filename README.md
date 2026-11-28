# antigravity-3d

**Ask for a game prop in one sentence. Get a package your engine can use as it is, already checked.**

![A crate, a fire hydrant and a Windsor chair](docs/img/three_props.jpg)

*Each of these was asked for in one sentence. Real output of the plugin in script mode, no image model, rendered in Blender.*

antigravity-3d is a plugin for the Antigravity coding agent, with a command line beside it. It builds the model, its
textures, lower-detail versions for distance and collision shapes. It checks the result in real engines and installs
it into your Godot or three.js project.

[How it compares with a stock agent](#how-it-compares) · [What is checked](#what-is-checked-and-what-is-not) ·
[All commands](docs/commands.md)

## What you can do

| You want to | Say to Antigravity | You get |
|---|---|---|
| Make a prop | "Make a military supply crate for Godot" | A package your engine imports: the model, one texture atlas, lower-detail versions, collision shapes, and a record of every check it passed |
| Put it in your game | "Install the crate into this project" | Godot: one scene, imported and checked by your own Godot. three.js or React Three Fiber: the files where your dev server serves them and one function to import, checked in a browser with your own three.js. One command takes it out again |
| Change it later | "Make the two side handles 30 % larger" | A new version in which only what you named changed. The old one is kept |
| Clean up a mesh you have | "Make chair.obj ready for Unity" | Your mesh repaired, unwrapped, with lower-detail versions and collision shapes |
| Build a 3D website | "Build a scroll-driven 3D site that shows how this project works" | A static site in your project. The agent starts from a tested page (scroll, camera path, loading, reduced motion, a page that still reads without WebGL), makes its props with the plugin and installs them, and after each change has the built site opened as a visitor would see it |

On the command line:

```bash
agy3d prop "military supply crate" --preview --token-ceiling 600000 --max-images 0 --engine godot_4 --out out/crate
agy3d install out/crate/runs/<id>/package --project path/to/your/game
agy3d edit out/crate "make the two side handles 30 % larger" --token-ceiling 250000
agy3d sanitize chair.obj --engine unity_urp --out out/clean
agy3d check-site site --sources .
```

`prop` and `edit` need a Gemini API key; "Try it" shows how to run without one. For three.js, build with
`--engine web_three` and use the same `install` command.

`install` takes the package folder the run prints. In a Godot project it writes one folder with a scene to drag
into a level, then checks it with your own Godot:

```text
  check import           passed   headless import and scene load logged no error or warning naming the asset
  check collision        passed   one StaticBody3D with 8 convex shape(s), each within 256 points ...
  check level_switching  passed   rendered: the expected level alone at every stop, ...
  install_godot  installed  agy3d/WoodenCrateIron  checks: 7 passed, 0 failed, 0 skipped
```

In a three.js project it writes the files to `public/agy3d/<asset>/` and a loader to `src/agy3d/<asset>/`, then
runs eleven checks in a browser with your own three.js (twelve with React Three Fiber). What is left for you:

```js
import { loadMilitarySupplyCrate } from "./agy3d/military_supply_crate";
scene.add(await loadMilitarySupplyCrate({ renderer }));
```

With React Three Fiber: `<MilitarySupplyCrate />` in a `<Canvas>`, from `./agy3d/military_supply_crate/react`.

`edit` changes only the parts you name; `--undo` puts the old version back, and `install` again updates your game.

`check-site` opens a built website at desktop and phone size, scrolls through it and answers in text: what is
broken, what a visitor would notice, what the page weighs and what its 3D costs, with a contact sheet of every
scroll step. A number or a code sample on the page that names its source file (`data-src`) is compared with that
file. Where the page says where its 3D subjects are, as a site built from the skill's starter does, the check also
names the props and labels that sit under the text panel or are cut by the frame's edge, and saves a still of
each chapter for the page a visitor gets without WebGL. The agent has the same check as a tool and uses it after
every change:

```text
SITE CHECKED in 23 s: nothing broken, 0 things a visitor would notice. ...
- Desktop 1440x900: settled 8.8 s after the page was asked for; 9 of 9 steps photographed; share of the 3D canvas
  showing something, per step: 75% 76% 74% 77% 74% 62% 81% 36% 76%.
- Downloaded (desktop, whole scroll): 5.3 MB in 38 files: textures 1.8 MB, scripts 1.6 MB, models 1.0 MB, ...
- Scene (desktop, the largest over the steps): 14 draw calls and 31,726 triangles in one frame ...; textures
  51.7 MB on the graphics card in 23 textures ...
- Without WebGL: no errors; the page shows 3,640 characters of text (with WebGL 3,640) and 7 images, ...
- Facts: 31 elements name a source (data-src) against ...; all match.
```

For the web a package is sized for a page: a 2048 px texture atlas unless you name another size
(`--max-texture-px`), and WebP textures where they render the same as the PNG ones.

## How it compares

| | This plugin, default | This plugin, script mode | Stock agent with a Blender tool |
|---|---|---|---|
| Game-ready checks passed, of 33 | 32 | 33 | 28 |
| Time to a package, minutes | 4 to 5 | 4 to 5 | 6 to 10 |
| Blind verdicts won against the stock agent: crate / hydrant / chair | 6 of 6 / 6 of 6 / 0 of 6 | 6 of 6 / 5 of 5 / 0 of 6 | |
| A change request finished on its own | 2 of 3 | 3 of 3 | 1 of 3; 2 stopped at their limit |
| Installed into a Godot or three.js project and checked there by one command | yes | yes | not offered |

Three briefs; our runs are from round six, the stock agent's from round five, one or two runs each. Looks were
judged blind by a general model, six times per pair. Both of our chairs lost every verdict then, because of the
surface this project painted on them.

Round seven repainted the script-mode props without touching their meshes. The crate and the hydrant still win
(12 of 12 each). The chair is now about even with the stock agent's (7 of 11): every verdict prefers its wood, and
the ones it loses fault its shape. Details: [round seven](benchmarks/ROUND_SEVEN.md),
[round six](benchmarks/ROUND_SIX.md), [round five](benchmarks/ROUND_FIVE.md).

A whole website was asked for six times, in one sentence each time: before the plugin was changed for what the
first run's logs showed, after, after each of two independent reviews of the site's three.js, and after the faults
the fourth and the fifth run showed. A blind judge prefers the second site to the first, finds the third level
with the second, and prefers the fourth to every other, to the fifth and to the sixth in six of six verdicts
each. The fourth site shows a source for every number, was built with no tool call cut off (the first run lost
six) and with no screenshot script of the agent's own, and follows one real log through three open machines. Every
run has shown faults of the plugin's own, some of them made by the corrections for an earlier run: a check told to
excuse a travelling camera while the chapter's text is up, a skill that asked for light grey machines, a starter
and a guide that asked for a floor as dark as the page; and on every site built on the starter the first notch of
a mouse wheel moved the picture by a third of a pixel or less. After the sixth run what its verdicts name was put
to the judge one thing at a time, on the same site: with
its floor lit it is preferred to itself in eleven of twelve verdicts (and the fourth run's site, asked the same,
in five of six); and a card whose text is half faded where a
visitor stops, which the guide's own example taught, loses six of six. Whether a text card belongs over the
camera's move got opposite answers on two sites (six of six against it on the sixth run's, five of six for it on
the fourth's) and is left as it was. The floor and the text's entrance are corrected and tested, and no site has
been built with them yet. The same
request without the plugin took half the model calls and tokens (37 and 5.0 M against 64 to 81 and 8.0 to 11.2 M)
and gave a site the judge placed below the plugin's second to sixth in all thirty verdicts, for objects that
float where the plugin's stand on a floor and for text over its 3D; the site check adds that it shows nothing
without WebGL.
Details: [round eight](benchmarks/ROUND_EIGHT.md).

## Try it

1. From a clone of this repository, with Python 3.11 or newer: `pip install -e ".[dev]"`.
2. In your game's folder, run `agy3d-plugin`. It puts the plugin in `.agents/plugins/antigravity-3d`.
3. Open that folder in Antigravity and ask for a prop. Expect a first build to take four to five minutes.

Without a Gemini API key the plugin judges with a stand-in, so delivery waits until you allow an unverified preview
and give it a token and an image limit.

On a key without the calibrated judge, a run is a preview and you set its two limits: tokens and images. With
`--max-images 0` the textures are generated from the geometry and no image model is used. In round five such a prop
spent 0.37 to 0.45 million tokens, an edit 62 to 86 thousand ([keys and cost](docs/keys_and_cost.md)).

`--offline` needs no key and takes about 15 seconds, but builds a built-in template, not your sentence.

## What Antigravity gives it

- You ask in your own words; the right playbook (a skill) loads by itself.
- Your export is held until the checks pass; the agent is sent back if it stops before.
- You are asked before anything is written into your project or a preview spends tokens.
- You review the asset in a 3D panel beside the conversation; your notes reach the agent.
- At install you choose: the agent drives scene tools, or writes each asset as a Python file.
- Antigravity ends a tool call after three minutes. Every tool here answers within 150 seconds: with its result,
  or with the step it is at and the call to repeat. The work goes on meanwhile and no step runs twice.

## What is checked and what is not

- **Godot 4**: a real Godot imports every package when installed; the seven checks above run in your project.
- **three.js**: every package is loaded in headless Chromium and compared with its manifest. Installed in a
  three.js project it is rechecked with the project's own three.js (run here on r152 to r187). Textures are
  compressed only when a rendered comparison stays inside a fixed tolerance, so some assets ship uncompressed.
- **A website**: `check-site` loads the built site in headless Chromium with software rendering. It finds errors,
  empty stretches, subjects hidden or cut, weight and facts that disagree with their source, and it tests its own
  screenshots first (a browser without its GPU path draws a WebGL canvas wrongly in them). It does not judge how
  the site looks.
- **Blender**: with a worker attached, a script this project did not write checks the GLB.
- **Unreal, Unity, Roblox**: packages and FBX files follow each engine's documented rules; none of these engines
  has opened them here or in CI.
- **Looks** are judged by a model; without the calibrated judge, look yourself.
- **The review panel** is tested against the MCP Apps host bridge, not inside a running Antigravity.

## Reference

- [Commands](docs/commands.md), [keys and cost](docs/keys_and_cost.md), [how it works](docs/how_it_works.md),
  [what is verified and known limits](docs/verification_and_limits.md),
  [package provenance](docs/package_provenance.md).
- [The plugin](plugin/antigravity-3d/README.md) and [the measurements](benchmarks/).

## License

See [LICENSE](LICENSE).
