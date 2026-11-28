---
name: story-slides-3d
description: >-
  Builds a deck, a talk or a short film that plays in the browser, told beat by beat in a fixed frame and not by
  scrolling: slides in HTML over one fluid three.js stage, one theme for both (type, colour, light, treatment), a
  metaphor in space where a claim needs one, a prop built by the plugin where a shape matters, designed cuts, keys
  and a tap to step through, speaker notes, auto-play. Gives the order of work (a beat sheet first, the story in
  blocks from a tested starter, light and treatment, motion and cuts, check_site beat by beat, revision) and a
  staging guide. Use when the user asks for slides, a deck, a presentation, a talk, a pitch, a keynote, a story, a
  short film, an animated explainer or a title sequence in three.js or WebGL, for slides that are not boring, for
  scenes or beats, or for an ASCII, halftone or noir look. Not for a page whose scrolling moves a camera through
  one scene (use scroll-3d-website) or for modelling one prop on its own (use parametric-kitbash).
---

# Story slides in 3D

A story told in a fixed frame: a few small 3D sets, one look over all of them, a line or two of text for each beat.
The viewer steps through it with a key or a tap, or lets it play. Two kinds share this method and its starter:

- a **film**: a short fiction, or a portrait of a place or a machine, narrated in one voice under the picture;
- a **talk**: an argument in which each claim is an object doing something, with a few large words in the picture
  and the speaker's sentences in notes.

This skill is the method. Two things beside it hold the details, read when the step needs them:

- `starter/`: the smallest film with the plumbing right (the story's text in the page, scenes and beats, a state that
  every picture is a function of, three print treatments, five cuts, captions, keys, a link to every beat, auto-play,
  reduced motion, no WebGL, a hook for checks) and a five-scene example to replace. Its test runs it in a browser.
- [staging.md](staging.md): how a picture is made here: light, sets, the camera, events, type, treatments, cuts and
  props, each with the code.

The method was read from two pieces the plugin's owner pointed to: a noir of seven scenes printed in text characters,
and a conference talk of 13 scenes and 43 beats in which a dull slide turns into a world. Where a number below says
"the noir" or "the talk", it was counted in them.

## What the two have in common

1. A fixed frame. The viewer moves beat by beat, forwards or back, or lets it play.
2. Scenes, each one idea in one picture. Beats inside a scene, each changing one thing. A line of text per beat.
3. One look that holds the sets together: one treatment over every scene, or one motif carried through all of them.
4. Sets of a few boxes. Light, haze and shadow do the drawing.
5. One slow camera move and one event per beat, and a little ambient life.
6. A cut between scenes that belongs to the film.
7. Every picture is a function of the scene, the beat's progress and the time, so any beat can be opened directly.

## Write first, build later

1. Read what the story is about. For a talk or an explainer: the project's README and docs, and its own design rules
   when it has them (colours, type, what its visual language rules out). For a fiction: the brief, and then choose a
   genre before anything else, because a genre brings a voice, places and a light with it. A noir brings a tired
   narrator, an office, rain and window blinds; a ghost story a house, a corridor and a candle. A film without one
   has to invent all three.
2. Read the workspace's package.json: use the libraries it has installed and no others (a sandbox cannot install).
   The starter needs three and Vite only.
3. Do not open the starter yet. Write the beat sheet: the film is decided there, and a set built before it ties the
   film to that set.

## The order of work

| Step | What you make | Tools |
|---|---|---|
| 1. Beat sheet | premise, voice, look and the table of beats, in a file in the workspace | none |
| 2. The film in blocks | the starter copied in, the story's text in the page, a file per scene: grey blocks at real sizes, one light, the camera's first and last position | `check_site` once |
| 3. The picture | each scene's light and set, the film's treatment, a prop or two | `check_site` |
| 4. What moves | each beat's event and camera move, ambient life, the cuts | |
| 5. Look, revise | contact sheets at desktop and phone size, the fixes, the sheets again | `check_site` |

Do the whole film in blocks before any scene is finished. A film fails on its story and on its compositions, and
both show with grey blocks under the right light: the first contact sheet is the storyboard in pictures, and a scene
that does not work there costs minutes to change. A prop built by the plugin is optional and belongs to step 3: two
at most, a quarter of the time at most, and the film must be whole without it (run 1 of the scroll site spent 45 of
its 69 minutes on props and had one pass at its page).

### 1. Beat sheet

Head the file with four lines:

- **Premise**: one sentence that holds someone or something, a place, and what changes. "A lighthouse keeper sees a
  light answer his" is a premise; "a lighthouse at night" is a set.
- **Voice**: who speaks, in which person and tense, and two adjectives for how. Every line is then written in it. For
  a talk: who is listening and what they believe before it starts.
- **Look**: the treatment (step 3), two or three colours (the dark, the light, one accent) and where light comes
  from in this world.
- **Motif**: one thing that is in every scene. The noir's is a light with a source, named in every caption (blinds, a
  street lamp, lit windows, headlights, an open door). The talk's is the slide itself: its page number in a corner
  of every scene, and an editor's selection handles on whatever is being talked about. It is what makes five sets one
  film.

The starter's example film is headed so:

    Premise  A lighthouse keeper sees a light at sea answer his, and by morning it has come ashore.
    Voice    A narrator close to the keeper; third person, past tense; plain and dry.
    Look     Halftone, lamp-light ink on night paper; all light comes from the lamp, until the morning.
    Motif    The lamp's light: in every scene something is lit by it, answers it or follows it.

Then one row per beat. These two rows are that film's second scene:

| scene | beat | the picture: place, light source, subject | what changes in this beat | camera | text | cut in |
|---|---|---|---|---|---|---|
| tower | 1 | a night sea, a lighthouse, its beam | the beam comes round and passes over the camera | a slow push in | "The light went round every nine seconds. It had done so for sixty years." | iris |
| tower | 2 | the same | far out, a small light appears and blinks back | holds | "Tonight something out there answered it." | |

What the table has to satisfy, and why:

- **Five to nine scenes, one to four beats in each.** The noir has seven scenes of one beat (about two minutes), the
  talk 13 scenes of three or four beats (twenty minutes with a speaker). One place and one idea per scene.
- **A beat changes one thing in the picture.** Something appears, moves or lights up, or the camera shows what was
  hidden. If only the words change it is not a beat: give its words to the beat before. The check names a beat whose
  picture is the picture before it.
- **Text is short and does not describe the picture.** In a film: one or two sentences per beat, 8 to 22 words each
  (the noir's range), in the voice. The line says what a picture cannot (a thought, what came before, a judgement)
  and points at what the light is on; the picture shows what the line leaves out. In a talk: one to five words in
  the picture (the talk's own count; half its scenes have two or fewer), and what the speaker says, about 25 words a
  beat, in the notes. "A lighthouse stands by the dark sea, its beam sweeping the night" describes the picture and
  adds nothing to it. "The light went round every nine seconds. It had done so for sixty years" gives a number the
  picture cannot and a past it does not show. A fact with a number in it is nearly always the better line.
- **Plant, then reveal.** What a later beat reveals stands in the earlier picture unremarked. In the talk, the chain
  that holds a paper boat is at the waterline two beats before the camera goes under the surface and the words name
  it. A reveal that was not planted is only a new slide.
- **The first and the last picture answer each other.** The first sets a question (who answers the light?), the last
  shows it answered, or returns to the first picture changed. Put the turn, the beat that changes what the earlier
  ones meant, at about two thirds.
- **In a talk, each claim is an object or a behaviour whose physics is the claim's logic.** Write the argument first,
  as five to nine sentences that each follow from the one before. Then name, for each, the thing that makes it true
  to the eye: "twice the rate" is two gears, one with half the teeth; "it compounds" is a row of dominoes each larger
  than the last; "one becomes fifty thousand" is one page and then fifty thousand of them. The test: with the words
  hidden, could a viewer say what happened and guess the claim? A picture that only decorates its claim (a glowing
  network for "AI") fails it.
- **In an explainer of a project, one real thing is followed through, and somebody needs it to arrive.** Give the
  film someone who wants something the project provides (an agent with a failing test and a log too long to pass
  on): a mechanism is understood by what it does for them. Each scene is then a station at work on the followed
  thing, in the order it meets them, and every number carries its source. The scroll skill's recipe "Facts with their
  source" ([recipes.md](../scroll-3d-website/recipes.md)) works here unchanged: the check reads the same attributes.

### 2. The film in blocks

Copy `starter/index.html`, `starter/src/` and `starter/vite.config.js` into the project (keep the workspace's
package.json; the starter needs only three and Vite) and replace its example film, all of it: the story's text,
every scene file, the film's name (the page's title and description, the mark in the strip, `src/story.js`) and its
two inks (in `src/story.js` for the print and at the top of `src/style.css` for the page round it).

- `index.html` holds the story's text, and nothing else does: one `<section data-scene="id">` per scene with its
  title in an `<h2>`, and in it one `<div data-beat>` per beat with that beat's one or two `<p>`. Why there: it is
  what a browser without WebGL shows (an article that reads from top to bottom), what a screen reader reads, and what
  the check compares the captions with. The engine shows the current beat's lines and never changes the text. A
  beat's lines go under the picture; `data-place="over"` on the beat sets them large over it instead, for a talk. A
  speaker's note is an `<aside data-notes>` inside the beat.
- `src/story.js` names the film: its title, its treatment, its two inks (`paper` and `ink`) and its scenes in the
  page's order. The engine says so on the console when the page's scenes and beats and the scene files disagree.
- `src/scenes/<id>.js`, one per scene: the beats' times (`dur`, the seconds a beat takes to play, and `hold`, the
  seconds auto-play then waits), a `create` function that builds the set once out of the kit, and an `update`
  function that poses the set for a state.

**The one rule of the engine: `update` is a function of the state it is given, and of nothing else.** No counter kept
in the scene, no clock of its own, no `Math.random()` while updating (the kit has a seeded one for building). Why: a
viewer opens the third beat from a link, steps back, lets it play; the check photographs each beat directly. A scene
that accumulates shows each of them a different picture.

| in the state | what it is | what it is for |
|---|---|---|
| `s.beat` | the current beat's index, from 0 (a link counts from 1: `#tower-2` is `s.beat` 1) | |
| `s.p[k]` | the progress of beat k, 0 to 1: 1 for beats already played, rising for the current one, 0 for later ones | what a beat changes: `door.rotation.y = -1.2 * s.p[1]` |
| `s.k(beat, from, to)` | 0 to 1, eased, between two moments in seconds inside that beat | placing an event in time |
| `s.t` | the seconds since the scene was entered | ambient life: rain, a fan, a blink |
| `s.enter`, `s.exit` | 0 to 1 while a cut into or out of the scene is running | a light that comes up with the cut |
| `s.pointer` | the pointer's place, eased, -1 to 1 | a few centimetres of camera parallax |
| `s.aspect` | the picture's width over its height | fitting the subject on a phone |
| `s.still` | true under reduced motion: beats appear finished and `s.t` stands still | |

"In blocks" means every set out of the kit's boxes at real sizes (a door 2 m, a desk 0.75 m; the kit has a figure of
1.8 m), one light where the scene's light will be, and the camera at the beat's first and last position. `npx vite`
serves the film; `npx vite build` writes it to `dist/` with relative paths, so it works from any folder of a host
(`npx vite build --outDir site` when the task names another folder for the result). Build, run `check_site` on the
built folder, and read its contact sheet as a storyboard:

- Is each scene's subject found at the tile's size, and is it the brightest thing in the tile?
- Does every beat's tile differ from the one before it in its scene?
- With the captions covered, do the tiles in order still tell something?

Where the answer is no, change the beat sheet first and the scene second.

### 3. The picture: light, set, treatment

[staging.md](staging.md) has each of these with its code. In short:

- **Start from black and add one light that has a source in the scene's world**: a window, a street lamp, headlights,
  an open door, a screen. What it lights is the subject, and what it does not light is not in the picture. Fill light
  is a last resort at a few hundredths of the key (the noir's sets: 0.03 to 0.05 of ambient against key lights of
  2.4 to 3.4). A set lit all over has no subject, and under a cell treatment it prints as one even field.
- **Break the light.** Slats in front of it lay stripes across a desk; haze makes its beam a cone that can be seen; a
  figure in front of a lit doorway is a silhouette that reads at any size. With boxes, those three are the noir's
  whole art department, and the kit has all three.
- **Three depths**: something near and dark at an edge of the frame, the subject in the light, something far and dim.
- **Build what the camera sees, from the side it sees it.** A street is two fronts and a road.
- **One treatment for the whole film**, chosen for what the film is. Look at the film in two of them before choosing:
  `?print=<name>` on the page's address shows it in another.

  | treatment | the picture is | suits | mind |
  |---|---|---|---|
  | `grade` | as rendered, with a tone curve, grain and a vignette | a film whose look is in its sets and colours; a talk that changes world from scene to scene | the sets carry everything: surfaces, colour, edges |
  | `halftone` | two inks: dots on a turned grid, sized by tone | a story that should feel printed or drawn | tone only: no colour but paper and ink |
  | `glyph` | text characters chosen by tone, edge characters from depth | a noir, a terminal, anything made of text | about 200 by 60 cells on a desktop: compose in silhouettes |

  Under a cell treatment a detail smaller than two cells does not exist, and neither does a difference in tone smaller
  than one step of its ramp: spend on silhouette and light and nothing on surface detail. The talk holds together
  the other way: every scene a world of its own under a plain grade (a sea, then flat coloured gears), with the
  motif, the type and the cuts carried through. That is a set designer's work per scene; choose it when crossing
  worlds is the story.
- **Two or three colours**: the dark, the light, and one accent for the thing the story follows.
- **A prop from the plugin** where the camera comes close to one thing whose shape matters and boxes cannot give it
  (a lens, an engine, a chair). `src/props.js` has an entry per prop, and a scene asks the kit for it by name: until
  the real one is installed it gets the scene's own version in blocks, so the film never waits for a prop. Build,
  deliver and install it as section 3 of [the scroll skill](../scroll-3d-website/SKILL.md) says (the same tools in
  the same order: `apply_scene_ir` and `render_preview` while modelling, `deliver_package`, a look at the render it
  names, `install_into_project`), then give its entry the `asset` and `module` fields. Under a cell treatment ask for its shape and leave surface detail out: it does not
  print.

### 4. What moves, and the cuts

- **One event per beat, placed in time.** The event is what the beat is about: the lamp comes on, the figure steps
  into the doorway, the second gear starts to turn. Begin it a second or two into the beat (`s.k(1, 1.5, 4)` runs
  from 1.5 s to 4 s of the second beat), so the viewer has seen the picture before it changes, and end it before the
  beat ends.
- **One camera move per beat, and a slow one.** The noir's: a push of 5 m in 16 s down a hallway; a rise of 1.5 m with
  a push of 3 m in 20 s over a city; a sway of 0.18 m to either side; or no move at all, and the pointer's parallax
  only. In a set the size of a desk, a tenth of those. A move starts where the last beat's ended.
- **Ambient life from `s.t`**: rain, a fan, a lamp that falters now and then, a lantern that blinks. Small and
  periodic: it says the film is running while the viewer reads.
- **Nothing turns for its own sake.** An object that spins because it can belongs to a product viewer.
- **A cut for each change of scene, chosen for what happens between the two**:

  | cut | reads as |
  |---|---|
  | `fade` | time passing, or the default when nothing else is meant |
  | `iris` | attention closing on one thing, or opening from it |
  | `wipe` | the next place being written over this one: a report, a list, a typewriter |
  | `fall` | the picture coming down: an ending, rain, a collapse |
  | `flip` | the other side of the same thing |

  The noir uses five kinds of cut for its six changes of scene, 1.8 to 2.6 s each. The talk names its cuts after acts
  that belong to the scene they open: a fold into a paper boat, a slam for a calendar's page.
- **Auto-play leaves a beat `dur` plus `hold` seconds after it began.** Give `hold` the time to read: about a second
  for every 12 characters of the beat's text, and never under 4 s (the noir types 26 ms a character and waits 6.5 s
  after the last one).

### 5. Look, revise

Run `check_site`, open both contact sheets, and fix what the next section lists. Then go through the film as a
viewer would, from its first beat, and read every line aloud (`?pace=4` on the page's address plays everything four
times as fast). A line that makes you look away from the picture is too long; a beat you want to skip has no event.

## Check

The film is done when `check_site` on the built folder reports, at both sizes:

- nothing broken: no uncaught error, no console error, no failed request, no request to another host; every beat
  draws a picture; every beat has its lines, and they are the page's own text;
- nothing a viewer would notice: no beat that shows the picture of the beat before it; no scene in which nothing
  moves while a beat plays; no caption that is cut or has to be scrolled; no line of more than about 140 characters;
  no picture that is nearly one tone at the phone's size; the right arrow and the Next button advance; a beat's link
  opens that beat;
- reduced motion shows every beat finished and standing still; a browser without WebGL shows the story as an
  article, with one still per scene (the check writes them: use them);
- for an explainer: no fact that disagrees with its source;
- a time to the first picture, draw calls, triangles and bytes you can state and defend.

The check finds a broken beat, not a dull one. Look at the sheets yourself for the three questions of step 2, and
for one more: does the darkest third of each tile stay dark? Between looks the page's own hook answers at once:
`window.__story.go(id, beat)` shows a beat finished, `window.__story.play(id, beat)` plays it as a viewer sees it,
`window.__story.cutAt(name, p, id, beat)` holds a cut at a point of its way, and `window.__story.info()` says where the
film is and what it draws.

## Gotchas

- **Symptom:** a beat opened from its link, or reached by stepping back, shows a different picture from the same beat
  reached by playing. **Cause:** the scene keeps state of its own: a counter, a tween, a random number drawn while
  updating. **Remedy:** compute every position, angle and intensity from the state (`s.p`, `s.k`, `s.t`); draw
  random values once, while building, from the kit's seeded generator.
- **Symptom:** the film hitches the first time a scene or a cut appears. **Cause:** a material, a light or a texture
  was made during an update, or a light was switched with `visible`, so a shader was compiled in front of the
  viewer. **Remedy:** make everything while building; turn a light off with an intensity of 0 and a mesh off with
  `visible`. The starter draws every beat once before the film starts, which compiles what exists then.
- **Symptom:** under `glyph` or `halftone` a scene is an even field of middle tone with no subject. **Cause:** the
  set is lit all over (ambient light, a sky light, several lamps), so every cell has about the same tone.
  **Remedy:** take the fill out, keep one key light, and let most of the frame be dark.
- **Symptom:** on a phone the subject is small, or cut at the sides. **Cause:** a perspective camera keeps its
  vertical angle, so an upright frame sees a narrower set. **Remedy:** the kit's `fit` sets the field of view for the
  aspect and returns the distance at which a subject of a given width and height fills the frame; place every
  camera through it, and look at the phone's sheet.
- **Symptom:** auto-play leaves a beat before its lines can be read. **Cause:** a `hold` shorter than the reading
  time. **Remedy:** about a second for every 12 characters of the beat's text, never under 4 s.
- **Symptom:** the film is a tour of five handsome sets and nobody can say what happened. **Cause:** the sets were
  built first and the lines written to fit them. **Remedy:** the beat sheet first: a premise with a change in it, a
  turn at two thirds, a last picture that answers the first.
- **Symptom:** the page works at the server's root and loses its files under a sub-path. **Cause:** an absolute
  path (`/assets/...`). **Remedy:** relative URLs and Vite's `base: './'`, as the starter has them; `check_site`
  serves the film under a sub-path to catch this.

## Version notes

Last verified 2026-11-28 against three 0.187.0 and Vite 8.3.8 (the starter's repository test and the examples in
staging.md ran against these). The starter uses no other library. The two pieces the method was read from are not in
the repository.
