# Staging a deck

How a picture is made on this stage, part by part. The code below is the starter's own: each block is taken from the
file named on its first line, and the repository's tests run those files in a browser against three 0.187.0. Read
the part the step needs; the method is in [SKILL.md](SKILL.md).

## A world and its scenes

A deck's things stand in one world, which is built once. Each scene poses it.

- `src/worlds/<name>.js` exports `create`. It builds the scene, the camera, the light and every part the deck will
  show, at real sizes in metres, and returns them together with the functions that pose them.
- `src/scenes/<id>.js` exports the scene: its id, the world it is in, its beats' times, and a `create` that is given
  the kit and the world and returns `update`. `update` poses the world for a state `s` and reads nothing else. In
  the code, beats count from 0.
- `src/story.js` lists the worlds and the scenes in the page's order.

This is the whole of the example's title scene:

```js
// from starter/src/scenes/cloud.js
export default {
  id: 'cloud',
  world: 'sky',
  ambient: true, // the cloud drifts while the title holds
  beats: [{ dur: 6, hold: 6 }],
  create(kit, w) {
    const from = kit.view(), to = kit.view();
    return {
      update(s) {
        w.cloudTo(0, s.t);
        w.rig.cover(-650, 0, 0, 1300); // the whole of the cloud's shadow, and the house at its tip
        // One slow push, a tenth of the way in.
        w.views.vista(from, s.aspect);
        w.views.vista(to, s.aspect);
        to.eye.lerp(to.aim, 0.1);
        kit.move(w.camera, from, to, s.k(0, 0, 6));
        w.camera.position.x += s.pointer.x * 30;
        w.camera.position.y -= s.pointer.y * 12;
      },
    };
  },
};
```

What to take from it:

- **Every call says where a thing is, not how far to move it.** `w.cloudTo(0, s.t)` puts the cloud at a place for a
  time. Nothing adds to a position, so the same state always gives the same picture.
- **`ambient: true`** tells the engine to keep drawing while a beat holds, because the scene reads `s.t`.
- **The world owns the parts, the scene owns the moment.** A scene never builds anything in `update`.

The engine calls the world's `rest` before every `update`. It puts every part away, so a scene names only what it
shows, and it does not matter which scene posed the world last:

```js
// from starter/src/worlds/sky.js
rest() {
  world.cloudTo(0, 0);
  world.solid(1);
  cloud.visible = true;
  for (let i = 0; i < away.length; i++) away[i].visible = false;
  rig.haze(0);
  rig.cover(-500, 0, 0, 1900);
  camera.near = 5;
  camera.far = 90000;
},
```

### Numbers come from one file

A deck that states numbers keeps them in a facts file beside the page. The page's words point into it (an element's
`data-src` names the file and the place in it) and the world reads the same values, so the picture cannot disagree
with the words. `check_site` compares the page's numbers with the file:

```js
// from starter/src/worlds/sky.js
export const SIDE = facts.premises.side.value;          // the cloud: a kilometre long, wide and tall
export const CUBE = facts.derived.cube.value;           // its water poured together: a cube 7.94 m on a side
export const FALL = facts.derived.fall.value / 100;     // a drop's fall, metres a second
export const RISE = facts.premises.rise.value;          // the air's rise under the cloud, metres a second
```

## The theme on the stage

The page's theme is a set of colour and type tokens in `src/theme.css`. The stage reads the same colours when it
starts and again when the theme changes, so a set painted with the kit follows the theme.

- **`kit.paint`** gives a material in a theme colour: `lit` for what sets are made of (in shadow it becomes the
  theme's `shade`), `accent` for the thing the deck follows, `accent-2` for a second emphasis, `ink` and `muted` for
  marks, `paper` and `paper-2` for grounds. With `flat: true` it is a mark the light does not model (a wire, a tick).
- **`kit.rig`** gives the theme's light for a scene: one key light that casts the shadows, a fill, the sky and the
  air. A world may fix where the light stands (`from`) when a shadow has to fall on a certain spot. Its `plain` is
  the ground, in the page's own colour where the light falls on it, so a slide's words lie on the page's paper.

```js
// from starter/src/worlds/sky.js
const rig = kit.rig(scene, { shadow: 2048, from: SUN.toArray(), reach: 11000, height: BASE + SIDE + 100 });
scene.add(rig.plain(180000));
```

```js
// from starter/src/worlds/sky.js
const wall = kit.paint('lit', sunward), roof = kit.paint('accent-2', sunward), dark = kit.paint('ink', { roughness: 0.6 });
```

What a theme changes on the stage is in `src/theme.js`, one entry a theme:

| entry | what it sets |
|---|---|
| `print` | the treatment the picture is printed with: `grade` (as painted), `poster` (flat fields), `halftone` (dots), `glyph` (text characters) |
| `key` | the light's direction, its strength and how soft its shadows are |
| `fill` | how dark a surface is where the key does not reach |
| `fog`, `background` | how much of the ground's colour the air takes with distance; a sky that is a gradient or flat |
| `line` | how much thicker drawn lines are, so a frame or a ruler survives a print made of cells |

Rules that keep a deck readable in every theme:

- **Paint with tokens only.** A literal colour stays the same when the theme changes, and then it is wrong in four
  themes out of five.
- **Do not let colour alone tell two parts apart.** A theme may give two tokens one colour (in `poster` the second
  accent is the lit colour). Shape and light must do it.
- **Put the shadow box where the camera looks.** `rig.cover` spends the shadow map on a square of the given radius:
  small round a close subject, large for a vista. A scene calls it in `update` as its camera moves.
- **Under a print made of cells** (`halftone`, `glyph`) a detail smaller than two cells does not exist. Compose in
  large shapes and clear silhouettes.

## The camera

- **A view** is where the camera stands, what it looks at, its field of view and its near plane. `kit.view` makes an
  empty one. A world offers its places as functions that fill a view for the frame's shape, so an upright phone gets
  the same subject as a desktop.
- **`kit.fit`** sets the field of view for the frame's shape and returns the distance at which a subject of a given
  width and height fills the frame. **`kit.look`** then stands the camera there and puts the subject at a chosen
  point of the frame. The example's world wraps the two so that the subject takes the part of the frame the words
  leave free (the right and upper part in a wide frame, the upper half in an upright one):

```js
// from starter/src/worlds/sky.js
const wide = aspect >= 1, sx = wide ? fx : 0.9, sy = wide ? fy : 0.5;
const d = kit.fit(camera, aspect, w / sx, h / sy, fovWide, fovTall);
kit.look(camera, at, d3, d, wide ? 1 - sx : 0, 1 - sy);
```

  A place in the world is then one line: the subject's middle, its width and height, and the side it is seen from.

```js
// from starter/src/worlds/sky.js
yard: (view, aspect) => on(view, aspect, a.set(6.5, 4, 4), 31, 11, c.set(-0.66, 0.07, 0.74), 0.5, 0.64, 0.7, 36, 52, 0.3),
```

- **`kit.move`** takes the camera a share of the way from one view to another. The distance to the subject changes
  in proportion, so a move from a kilometre off to a few metres off is as even at its end as at its start.
- **One move per beat**, driven by `s.k`, eased at both ends: a push in, a rise, a step back to take in a second
  thing. What a move reveals was in the world from the first beat.
- **The pointer moves the camera a little**, in proportion to the subject's distance: a few centimetres near a desk,
  tens of metres a kilometre off. `s.pointer` is already eased.

## Travel

Between two scenes of one world the engine does not cut. It takes the camera from where the leaving scene had it to
where the arriving scene puts it, in 1.6 s (a scene may give its own length as `travel`), and 0.6 s when the viewer
goes back. The arriving scene sees the way in as `s.enter`, from 0 to 1.

- **Two scenes that stand in the same place share its view.** The travel then has nowhere to go and the picture
  carries on.
- **A change that begins with the way in and runs on through the first beat is put on one clock.** In the example
  the drops start to pour as the scene is entered and finish 7.6 s into its first beat:

```js
// from starter/src/scenes/weight.js
const pour = Math.min(1, (ENTER * s.enter + BEATS[0].dur * s.p[0]) / (ENTER + 7.6)), down = s.k(0, 1.2, 7.6);
```

- **A scene cuts instead when it names a cut** (`enterWith: 'iris'`). Between two worlds the engine fades unless the
  arriving scene names another cut.
- `window.__story.cutAt('travel', p, id, beat)` holds a travel at a share `p` of its way, to look at it.

## Mechanisms

A mechanism turns a kind of claim into a thing in space. Each is a file in `src/mechanisms/`, is built once in the
world, and is posed with a number, so it plays, holds and goes back with no code of its own. Each draws in theme
colours and costs one or two draw calls whatever its count.

### frame: a volume or an extent

A wire box at its true size round the thing. `km.grow` draws it: the base, the uprights, then the top.

```js
// from starter/src/worlds/sky.js
const km = frame(kit, { size: SIDE, thickness: 8 });
```

```js
// from starter/src/scenes/looks.js
w.km.mesh.visible = s.beat >= 1;
w.km.grow(s.k(1, 2.4, 6.6));
```

### field: a count, or a thing made of many

Thousands of one small shape. A layout gives each one a place and a size, under a name. The field then shows a
share of them, or moves all of them from one layout to another, and a scene gives it the share from the state.

```js
// from starter/src/worlds/sky.js
const drops = field(kit, { count: DOTS, seed: 7, shape: 'dot', paint: 'accent', stagger: 1.6, order: 'index' });
drops.layout('cloud', (i, o) => { o.x = home.x + inside[i][0]; o.y = home.y + inside[i][1]; o.z = home.z + inside[i][2]; o.size = 11; });
```

```js
// from starter/src/scenes/weight.js
w.drops.morph('cloud', 'cube', pour);
w.water(w.drops.arrived(pour));
```

- **Say how many each one stands for.** The example's three thousand dots stand for a hundred thousand million
  million drops, and the speaker's note says so.
- **Let what gathers fill what it makes.** `drops.arrived` is the share that has reached the second layout: the
  example's cube of water rises exactly as fast as its drops arrive.
- **A size of 0 in a layout hides that one.** It is how a layout holds fewer things than the field has.

### ruler: a length

A spine with ticks, its length a number of the world's metres, standing beside the thing with something familiar in
the same frame.

```js
// from starter/src/worlds/sky.js
const tape = ruler(kit, { length: Math.round(CUBE), step: 1, major: 4, thickness: 0.09, tick: 0.55 });
```

```js
// from starter/src/scenes/weight.js
w.tape.mesh.visible = s.beat >= 1;
w.tape.grow(s.k(1, 0.5, 3.8));
```

### streams: a rate, or two rates

Specks that run through a stretch of space at a speed in metres a second and come round again. Two streams on one
clock show a ratio without a word. `streak` makes a speck that many times longer than it is wide: give a fast
stream the length it travels in a tenth of a second and a slow one a round speck, and a still frame shows the two
speeds too.

```js
// from starter/src/worlds/sky.js
const air = streams(kit, [
  { speed: FALL, direction: [0, -1, 0], length: 0.36, across: [0.02, 0.06], count: 6, size: 0.005, seed: 3, paint: 'accent', at: [MARK.x, MARK.y + 0.05, MARK.z - 0.055] },
  { speed: RISE, direction: [0, 1, 0], length: 0.56, across: [0.05, 0.15], count: 17, size: 0.0034, streak: (RISE * 0.1) / 0.0034, seed: 5, paint: 'accent-2', at: [MARK.x, MARK.y + 0.05, MARK.z + 0.115] },
]);
```

```js
// from starter/src/scenes/floats.js
w.air.group.visible = s.beat >= 2;
w.air.flow(s.t, s.k(2, 1.0, 3));
```

### stack: a part of a whole. bars: a few numbers compared

The example does not use these two. A stack is a whole cut into equal slabs with some of them in the accent, so
counting slabs is reading the share. Bars stand on one ground under one light, each as tall as its value: use them
for a handful of numbers, and put a real chart in the slide's HTML. Both rise with their own `grow`.

```js
// from starter/src/mechanisms/stack.js
export function stack(kit, { parts = 10, marked = 1, size = [1, 2, 1], gap = 0.12, axis = 'y', paint = 'lit', mark = 'accent' } = {}) {
```

```js
// from starter/src/mechanisms/bars.js
export function bars(kit, { values, height = 1, width = 0.3, gap = 0.5, depth = null, marked = [], paint = 'lit', mark = 'accent' } = {}) {
```

### A mechanism of the deck's own

When none of the six fits, write one in the same shape: build everything once, take the theme's paints from the
kit, and give it one function that poses it for a number. The test is the one for every metaphor: with the words
hidden, a viewer can say what happened.

## Events and ambient life

An event is the one thing a beat is about. Place it in time with `s.k(beat, from, to)`: 0 before `from` seconds of
that beat, 1 after `to`, eased between, and it stays 1 once the beat has been played.

Ambient life is not an event: a cloud that drifts, a drop that sinks. It reads `s.t` and repeats. In the example one
drop sinks past a ruler at its true speed and comes round again at the top:

```js
// from starter/src/scenes/floats.js
const fallen = (FALL * s.t + 0.042) % 0.1, end = Math.min(1, Math.min(fallen, 0.1 - fallen) / 0.008);
w.drop.position.set(w.MARK.x, w.MARK.y + 0.1 - fallen, w.MARK.z - 0.012);
```

## The slide's words

- **A scene's words build up.** Each beat adds its lines to the slide. A beat written `data-beat="swap"` clears the
  slide first, for a scene that makes two statements. (A film's caption shows the current beat's lines only.)
- **Four kinds of line.** A plain paragraph is the large words. Class `lede` is a smaller line, `big` a figure that
  fills the width, `source` the small line that says where a number is from. A heading with class `kicker` is a
  label above the words. A speaker's note is an `aside` with `data-notes` inside the beat, and is never on the slide.
- **The layout decides where the picture has to be quiet.** `statement` and `title` take the lower left, `left` and
  `right` a column, `number` the lower left with a figure, `end` the middle. Keep the subject out of that part.
- **`check_site` measures every line against the picture under it** and names a line that is too faint. Move the
  camera first. Where the picture has to be busy under the words, `class="scrim"` on the scene's section sets a
  label of the page's colour under them. A theme whose picture can be as dark as its type (`poster`, `halftone`,
  `glyph`) sets that label under every scene's words by itself.

## A film: one light, a print, type in the set

`examples/film/` is the same engine as a film. Its scenes build sets of their own, lit by lights the set places
itself, and the film names its own print and two inks, which hold in any theme:

```js
// from examples/film/src/story.js
export const film = {
  title: 'Keeper',
  form: 'film',
  treatment: 'halftone',
  paper: '#0b0f17', // night: the ground everything is printed on
  ink: '#efe4cb', // lamp light: what the light draws with
  scenes: [title, tower, room, shore, end],
};
```

A film in a dark print is drawn by its light, so the light comes first:

- **One light with a source in the world**: a window, a lamp, headlights. Fill at a few hundredths of the key, or
  none. What the key does not reach is black, and black is what makes the lit part the subject.
- **Stripes.** The kit's gobo stands slats in front of a light that casts shadows. The lamp room's moonlight comes
  through bars both ways and lays a window on the wall:

```js
// from examples/film/src/scenes/room.js
const moon = kit.spot(scene, [3.0, 2.9, -1.6], [-1.6, 1.5, -3.8], { intensity: 12, angle: 0.3, penumbra: 0.2, distance: 20, shadow: 1024, colour: '#cbd8f2' });
kit.gobo(moon, { count: 6, gap: 0.12, thickness: 0.035, width: 2, turn: Math.PI / 2, at: 1.0 });
kit.gobo(moon, { count: 3, gap: 0.3, thickness: 0.05, width: 2, turn: 0, at: 1.02 });
```

- **A beam you can see.** The kit's haze is a cone of light that writes no depth. The lighthouse's beam is a spot
  light for what it lights and a cone for the beam itself:

```js
// from examples/film/src/scenes/tower.js
const light = kit.spot(scene, [0, LAMP_Y, 0], [0, 0, 60], { intensity: 26, angle: 0.08, penumbra: 0.6, distance: 160, colour: '#fff0cf' });
const cone = kit.haze(0.3, 4.5, 95, 0.42);
cone.position.set(0, LAMP_Y, 0);
```

- **A silhouette.** Put the light behind the figure and the camera in front: a dark shape against a bright one
  reads at any size and in any print. The kit's figure is made for it.
- **Switch a light with its intensity, never with `visible`.** A light that appears changes the shader of every lit
  material, and the film stops for a moment to compile it.
- **Type in the set.** The kit's `card` makes a texture of large type for a wall or a sign. The set's light draws it
  and the print prints it, so a title is already in the film's look:

```js
// from examples/film/src/scenes/title.js
const map = kit.card(words);
const face = new THREE.MeshStandardMaterial({ map, emissive: '#ffffff', emissiveMap: map, roughness: 0.9 });
```

## Cuts

A cut is a GLSL function the print runs for every cell: given the cell, the grid and a progress from 0 to 1, it says
whether the cell shows the picture that leaves or the one that arrives, whether it is black, and whether it is hot
(a bright edge that leads the cut). The five in `starter/src/cuts.js` are short enough to read as examples, and a
cut of the deck's own is one more entry there, named by a scene's `enterWith`:

```js
// from starter/src/cuts.js
Cut cut_iris(vec2 q, vec2 grid, float p) {
  vec2 aspect = vec2(1.0, uCutPx.y / uCutPx.x);
  float r = length((floor(q) + 0.5 - grid * 0.5) * aspect);
  float R = length(grid * 0.5 * aspect) + 2.0;
  float k = abs(1.0 - 2.0 * p); k = k * k * (3.0 - 2.0 * k);
  float black = step(R * k, r);
  float hot = (1.0 - black) * step(R * k - 1.5, r) * 0.8;
  return Cut(step(0.5, p), q, black, hot);
}
```

## A prop from the plugin

One entry in `starter/src/props.js`, one line in the world. Until the prop is installed the world's own version in
blocks stands in, so the deck is whole at every moment:

```js
// from starter/src/props.js
house: { size: [9, 7.6, 7] },
```

```js
// from starter/src/worlds/sky.js
const house = await kit.prop('house', { standIn: () => houseOfBlocks(kit) });
```

After `install_into_project` the entry gains two fields, `asset` and `module`, as the comment under it in that file
shows, and the world needs no change. Paint the stand-in with the kit so it follows the theme; a built prop keeps
the colours it was made with, so ask for the theme's colours in its brief.
