# Staging a story film

How a picture is made in a film of this kind, part by part. The code below is the starter's own: each block is taken
from the file named on its first line, and the starter's repository test runs those files in a browser against three
0.187.0. Read the part the step needs; the method is in [SKILL.md](SKILL.md).

A scene file exports its id, the cut it enters with, its beats' times, and two functions: `create` builds the set once
out of the kit and returns the scene, the camera and `update`; `update` poses the set for a state `s` and reads
nothing else. In the code, beats count from 0.

## Light

The picture is drawn by light, so light comes first and the set is whatever the light needs to fall on.

- **One light with a source in the world.** Before placing it, say what it is: a window, a street lamp, headlights, a
  door, a screen. That sentence is half of the scene's caption.
- **Dark is the default.** Fill at a few hundredths of the key, or none. What the key does not reach is black, and
  black is what makes the lit part the subject.
- **The kit's spot light does not fall off with distance** (its decay is 0), so an intensity means the same in a
  desk-sized set and in a street. Start at 4 and look.
- **Switch a light with its intensity, never with `visible`.** A light that appears changes the shader of every lit
  material, and the film stops for a moment to compile it.

### Stripes: slats in front of the light

Blinds, window bars, a fence, leaves. The kit's gobo stands slats close in front of a light that casts shadows, and
their shadows lie across whatever the light reaches. The lamp room's moonlight comes through six bars one way and
three the other, and lays a window on the wall for the keeper to stand against:

```js
// from starter/src/scenes/room.js
const moon = kit.spot(scene, [3.0, 2.9, -1.6], [-1.6, 1.5, -3.8], { intensity: 12, angle: 0.3, penumbra: 0.2, distance: 20, shadow: 1024, colour: '#cbd8f2' });
kit.gobo(moon, { count: 6, gap: 0.12, thickness: 0.035, width: 2, turn: Math.PI / 2, at: 1.0 });
kit.gobo(moon, { count: 3, gap: 0.3, thickness: 0.05, width: 2, turn: 0, at: 1.02 });
```

Keep the cone narrow (an angle of 0.3 or so): the shadow map is then spent on the slats, and 1024 px is enough under
a print's cells. The title card's blinds stir with `slide`, which takes an absolute offset, so it can be a function
of the state.

### A beam you can see: haze

A lamp in clear air lights the ground and nothing between. The kit's haze is an additive cone that makes the beam a
shape; it writes no depth, so what stands in it shows through. The lighthouse's beam is a spot light for what it
lights and a cone for the beam itself, turned together:

```js
// from starter/src/scenes/tower.js
const light = kit.spot(scene, [0, LAMP_Y, 0], [0, 0, 60], { intensity: 26, angle: 0.08, penumbra: 0.6, distance: 160, colour: '#fff0cf' });
const cone = kit.haze(0.3, 4.5, 95, 0.42);
cone.position.set(0, LAMP_Y, 0);
```

```js
// from starter/src/scenes/tower.js
b.light.target.position.set(0, LAMP_Y, 0).addScaledVector(dir, 60);
b.light.target.updateMatrixWorld();
b.cone.quaternion.setFromUnitVectors(DOWN, dir);
```

The opacity is the number to tune: between 0.3 and 0.45 the cone reads and does not wash out the set.

### A silhouette: light behind the subject

Put the light behind the figure and the camera in front: a dark shape against a bright one reads at any size, in any
treatment, on a phone. In the lamp room's first beat the keeper stands between the camera and the lit window on the
wall; in the second, between the camera and the lens. A doorway, a screen or a low sun do the same. The kit's figure
is seven parts and is meant for exactly this: a silhouette and a shadow.

## Sets

- **Real sizes.** A door is 2 m, a desk 0.75 m, a person 1.8 m, a street lamp 5 m. Lights, lenses and the kit's
  figure then agree without tuning.
- **Build for the camera.** What it never sees is not built. What is far is a flat strip: the town across the bay is
  one textured strip on the horizon (the kit's `windows`).
- **Three depths**: something near and dark at an edge, the subject in the light, something far and dim.
- **Matt surfaces.** Under a print a rough surface shows where the light falls; a shiny one shows a highlight and
  nothing else. The kit's materials are rough unless told otherwise.
- **Many of one thing is one mesh.** The footprints on the shore are forty-four prints in one `InstancedMesh` and
  one draw; so are the gobo's slats.

## The camera

- **Fit the subject, then move.** The kit's `fit` sets the field of view for the picture's shape and returns the
  distance at which a subject of a given width and height just fills the frame. Every camera position starts from
  it, so an upright phone gets the same subject as a desktop.
- **One move per beat**, from where the last beat's ended, eased at both ends: a push in, a rise, a slow turn to take
  in a second thing. In a street-sized set 3 to 5 m over a beat; in a desk-sized one a tenth of that.
- **A reveal is a camera move.** What it reveals was in the set from the first beat.

The tower's first beat pushes in on the lighthouse; the second turns and draws back until the lighthouse and the
lantern out at sea are in one frame. Both distances come from `fit`, and the move between them from the second beat's
progress:

```js
// from starter/src/scenes/tower.js
const turn = s.k(1, 0, 5);
const d = THREE.MathUtils.lerp(kit.fit(camera, s.aspect, 11, 14.5) * (1.2 - 0.15 * s.k(0, 0, 6)), kit.fit(camera, s.aspect, 34, 16), turn);
```

The pointer moves the camera by a few centimetres, never more: `s.pointer` is already eased.

## Events

An event is the one thing a beat is about. Place it in time with `s.k(beat, from, to)`: 0 before `from` seconds of
that beat, 1 after `to`, eased between, and it stays 1 once the beat has been played. The lantern at sea appears
between 0.6 s and 1.8 s of the tower's second beat, and then blinks on the scene's own time:

```js
// from starter/src/scenes/tower.js
const shown = s.k(1, 0.6, 1.8), on = ((s.t % 3) + 3) % 3 < 1.6 ? 1 : 0.12;
```

Ambient life is not an event: rain, a fan, a wick that falters. It reads `s.t` and repeats, and a scene that has any
says so with `ambient: true`, so that the engine keeps drawing while the beat holds:

```js
// from starter/src/scenes/room.js
lamp.intensity = 7 * (0.94 + 0.06 * Math.sin(s.t * 7.3) * Math.sin(s.t * 2.9)); // the wick
```

## Numbers and claims, for a talk

A number is shown as that many things, a ratio as two things moving at that ratio, a growth as sizes that grow.

- **A count**: one `InstancedMesh` of the full count, its `count` set from the beat's progress, so one thing becomes
  five thousand while the beat plays. Lay them out from a seeded generator (the kit's `rnd`), not at random.
- **A ratio**: two things driven by the same `s.t`, one multiplied by the ratio. Two wheels, two pendulums, two
  conveyor belts.
- **A size**: the same object at two scales in one frame, with something familiar beside the small one.
- **A source**: when the words carry a number, the page carries where it is from (SKILL.md, the beat sheet).

## Type

- **Under the picture** (the default): the beat's lines, typed out. For narration.
- **Over the picture**: `data-place="over"` on the beat sets its lines large over the picture's lower left. For a
  talk's few words. Keep that part of the set dark and empty.
- **In the set**: the kit's `card` makes a texture of large type for a wall or a sign. The set's light draws it and
  the treatment prints it, so the title is already in the film's look:

```js
// from starter/src/scenes/title.js
const map = kit.card(words);
const face = new THREE.MeshStandardMaterial({ map, emissive: '#ffffff', emissiveMap: map, roughness: 0.9 });
```

Under a cell treatment a letter needs strokes two cells wide and a height of about ten cells: a title for a phone is
a short word, or one word a line.

## Treatments

A treatment is one full-screen pass that prints every scene: `grade`, `halftone` and `glyph` are in
`starter/src/print.js`, each a cell size for a picture's width, a number of samples per cell, and a piece of GLSL
that returns the colour of a pixel from the scene's tone under it. A film can bring its own in the same shape and
name it in `src/story.js`. The two inks are the film's `paper` and `ink`; a treatment mixes between them, so a film's
palette is those two colours whatever the sets are painted.

## Cuts

A cut is a GLSL function the print runs for every cell: given the cell, the grid and a progress from 0 to 1, it says
whether the cell shows the picture that leaves or the one that arrives, from where in it, whether it is black, and
whether it is hot (a bright edge that leads the cut). The print shows the first picture exactly at 0 and the second
exactly at 1. The five in `starter/src/cuts.js` are short enough to read as examples; a cut of the film's own is one
more entry there, named by a scene's `enterWith`:

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

`window.__story.cutAt(name, p, id, beat)` holds a cut at a point of its way, to look at it frame by frame.

## A prop from the plugin

One entry in `starter/src/props.js`, one line in the scene. Until the prop is installed the scene's own version in
blocks stands in, so the film is whole at every moment:

```js
// from starter/src/props.js
boat: { size: [1.3, 0.6, 3.2] },
```

After `install_into_project` the entry gains two fields, `asset` and `module`, as the comment under it in that file
shows, and the scene needs no change. A scene that asks for a prop has an `async` `create`.
