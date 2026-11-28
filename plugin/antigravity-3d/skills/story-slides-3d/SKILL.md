---
name: story-slides-3d
description: >-
  Builds a deck, a talk or a short film that plays in the browser, told beat by beat in a fixed frame and not by
  scrolling: slides in HTML over one fluid three.js stage, one theme for both (type, colour, light, treatment), a
  metaphor in space where a claim needs one, a prop built by the plugin where a shape matters, camera moves and
  cuts between slides, keys, speaker notes, auto-play. Gives the order of work
  (the argument and a beat sheet first, the deck in blocks from a tested starter, theme and light, mechanisms and
  motion, check_site beat by beat, revision) and a staging guide. Use when the user asks for slides, a deck, a
  presentation, a talk, a pitch, a keynote, a story, a short film, an animated explainer or a title sequence in
  three.js or WebGL, for slides that are not boring, for scenes or beats, or for an ASCII, halftone or noir look. Not
  for a page whose scrolling moves a camera through one scene (use scroll-3d-website) or for modelling one prop on
  its own (use parametric-kitbash).
---

# Story slides in 3D

A deck that is not a list of bullet points: each slide is a few words in HTML over a 3D stage that shows what the
words claim, the stage is one world the camera moves through from slide to slide, and one theme styles the words and
the world alike. A viewer steps through it with a key or a tap; a speaker has notes; left alone it plays as a film.

This skill is the method. Three things beside it hold the details, read when the step needs them:

- `starter/`: a small deck with the plumbing right. The slides are the page's own HTML (a section per scene, a part
  per beat, seven layouts); the stage is an engine in which every picture is a function of a state; five themes;
  camera travel inside a world and five cuts between worlds; mechanisms for the common kinds of claim; keys, a link
  to every beat, an overview, speaker notes, auto-play, reduced motion, an article where WebGL is missing; a hook for
  checks. Its example is a six-slide talk to replace. Its tests run it in a browser.
- `examples/film/`: the same engine as a short film: captions under the picture, a halftone print, cuts.
- [staging.md](staging.md): how a picture is made here: a world and its scenes, the theme on the stage, the camera,
  travel, the mechanisms, events, the slide's words, a film's light, cuts and props, each with the starter's own code.

The method was read from two pieces the plugin's owner pointed to: a conference talk of 13 scenes and 43 beats in
which a dull slide turns into a world, and a noir of seven scenes printed in text characters. Where a number below
says "the talk" or "the noir", it was counted in them.

## What makes slides not boring

1. **One claim a slide, shown and not listed.** The stage shows the claim happening; the slide carries one to twelve
   words of it; what a speaker would say goes in the notes.
2. **A metaphor where the claim needs one, and only there.** A quantity, a ratio, a size, a change, a constraint or a
   mechanism is made into a thing in space that behaves as the claim says. A definition, a transition or a thank-you
   is a plain slide: good type over a quiet stage.
3. **One world.** The things the claims are made of stand in one place, and going to the next slide is a camera move
   through it. What a later slide reveals was already there.
4. **One theme.** The same few colours, the same type, the same light and the same motion on every slide, in the
   words and in the world.
5. **A beat changes one thing**: a line of words, or the world. Never nothing, rarely both.
6. **Every picture is a function of the scene, the beat's progress and the time**, so any slide opens from a link,
   plays backwards, and photographs the same twice.

## Write first, build later

1. Read what the deck is about: the project's README and docs, its results, and its own design rules when it has them
   (colours, type, what its visual language rules out). For a fiction, the brief, and then a genre: a genre brings a
   voice, places and a light with it.
2. Read the workspace's package.json: use the libraries it has installed and no others (a sandbox cannot install).
   The starter needs three and Vite; it uses the workspace's font packages when they are there.
3. Do not open the starter yet. Write the argument and the beat sheet: the deck is decided there, and a set built
   before it ties the deck to that set.

## The order of work

| Step | What you make | Tools |
|---|---|---|
| 1. Argument and beat sheet | the claims in order, and the table of beats, in a file in the workspace | none |
| 2. The deck in blocks | the starter copied in, the slides' text in the page, the world in grey blocks, a camera station per scene | `check_site` once |
| 3. Theme, light, sets | the theme chosen from two, the world's light, what the sets are made of | `check_site` |
| 4. Mechanisms and motion | each metaphor at work, each beat's one change, travel and cuts | |
| 5. A prop | the one or two things whose shape matters, built by the plugin | the plugin |
| 6. Look, revise | contact sheets at desktop and phone size, the fixes, the sheets again | `check_site` |

Do the whole deck in blocks before any scene is finished. A deck fails on its argument and on its compositions, and
both show with grey blocks: the first contact sheet is the storyboard in pictures, and a slide that does not work
there costs minutes to change. A prop built by the plugin is optional: two at most, a quarter of the time at most,
and the deck must be whole without it (run 1 of the scroll site spent 45 of its 69 minutes on props and had one pass
at its page).

### 1. The argument and the beat sheet

Head the file with five lines:

- **Claim**: the one sentence the audience should leave with. For a fiction, the premise: someone, a place, what
  changes.
- **Audience and voice**: who is listening and what they believe before it starts; who speaks, and how.
- **World**: the one place the deck's things stand in (a workbench, a harbour, a sky over a plain), chosen so that
  every metaphor below can be an object in it.
- **Theme**: one of the starter's five, or its tokens changed to the project's own colours and type (step 3).
- **Motif**: one thing that is in every scene. The talk's is the slide itself: its page number in a corner of every
  scene, which grows as its argument does. The noir's is a light with a source, named in every caption.

Then the argument: five to nine sentences, each a claim, each following from the one before with a "so", a "but" or
a "because". If two of them can swap places, the deck has no argument yet. The starter's example is argued so:

    1  A cloud looks like nothing at all.                                   say
    2  But take an ordinary one: a kilometre long, wide and tall.           show: a frame round it
    3  It is water: drops too small to see, half a gram to the cubic metre. show: a field, a bead
    4  So there are a hundred thousand million million of them.             show: the field, filled
    5  Poured together: a cube eight metres on a side. 500 tonnes.          show: the field gathered, a ruler; prove
    6  But each drop falls a centimetre a second, and the air rises faster. show: two streams
    7  So five hundred tonnes float.                                        say: the first picture again

For each claim, decide how the slide makes it:

- **Say it**: a few large words over the world at rest. The default.
- **Show it**: the claim is made into something in space. Use it where the claim is one of these, and name the
  mechanism (staging.md has each):

  | the claim is | show it as | mechanism |
  |---|---|---|
  | a count | that many things arriving | `field` |
  | a thing made of many | the many gathering into the thing, or the thing coming apart | `field`, morphed |
  | a part of a whole | one marked slab in a stack of them | `stack` |
  | a size, a distance, a volume | the thing beside something known, with a measure | `ruler`, `frame`, a prop for scale |
  | a rate, or two rates | things moving at those speeds | `streams` |
  | a few numbers compared | bars that rise | `bars` |
  | a change | the same thing before and after, the camera going from one to the other | travel |
  | a process | stations along a path, the followed thing moving between them | the world itself |

  The test of a metaphor: with the words hidden, could a viewer say what happened and guess the claim? A picture
  that only decorates its claim (a glowing network for "AI") fails it, and a plain slide is better.
- **Prove it**: a number with its source on the slide. In a deck about a project every number carries its source;
  the scroll skill's recipe "Facts with their source" ([recipes.md](../scroll-3d-website/recipes.md)) works here
  unchanged, and the check reads the same attributes.

Then one row per beat. These rows are the fourth scene of the starter's example:

| scene | layout | beat | words on the slide | what changes on the stage | notes |
|---|---|---|---|---|---|
| weight | number | 1 | "Pour them together." | the drops of the cloud gather into a cube beside the house | the turn of the talk: slow down |
| weight | number | 2 | "A cube of water eight metres on a side." | a measure of 8 m stands by the cube | |
| weight | number | 3 | "500 tonnes", and under it where it comes from | holds | let the number sit |

What the table has to satisfy, and why:

- **Five to nine scenes, one to four beats in each.** The talk has 13 scenes of three or four beats for twenty
  minutes; the noir seven scenes of one beat for two.
- **A beat changes one thing.** A new line of words, or the world. A click that changes nothing is a broken remote.
- **Few words, and none that describe the picture.** One to twelve on a slide (the talk's headlines: one to five);
  what is said aloud, about 25 words a beat, is the note. In a film, one or two sentences of 8 to 22 words under the
  picture, in one voice. A fact with a number in it is nearly always the better line.
- **Plant, then reveal.** What a later beat reveals stands in the earlier picture unremarked. In the talk the chain
  that holds a paper boat is at the waterline two beats before the camera goes under the surface and the words name
  it. A reveal that was not planted is only a new slide.
- **The first and the last picture answer each other.** The first sets a question, the last shows it answered or
  returns to the first picture changed. Put the turn, the beat that changes what the earlier ones meant, at about two
  thirds.
- **In a deck about a project, one real thing is followed through, and somebody needs it to arrive.** Give the deck
  someone who wants something the project provides (an agent with a failing test and a log too long to pass on): a
  mechanism is understood by what it does for them.

### 2. The deck in blocks

Copy `starter/index.html`, `starter/facts.json`, `starter/src/` and `starter/vite.config.js` into the project (keep
the workspace's package.json) and replace its example, all of it: the slides' text, the facts, the world, every scene
file, the deck's name (the page's title and description, the mark in the strip, `src/story.js`).

- `index.html` holds the deck's text, and nothing else does: one `<section data-scene="id" data-layout="...">` per
  scene with its heading, and in it one `<div data-beat>` per beat. Why there: it is what a browser without WebGL
  shows (an article that reads from top to bottom), what a screen reader reads, and what the check compares the
  slides with. The engine shows the scene's heading and its beats up to the current one and never changes the text.
  A speaker's note is an `<aside data-notes>` inside the beat. The theme is `data-story-theme` on `<html>` (a name of
  its own: sites and page hosts write their light or dark choice into `data-theme` there).

  | layout | the words are |
  |---|---|
  | `title` | the deck's name, large, with a line under it |
  | `statement` | a few large words in the lower left third; the rest is the picture's |
  | `left`, `right` | a column on that side; the subject has the other |
  | `number` | one figure as large as the frame allows, with the line that says where it is from |
  | `end` | the last words, centred |
  | `caption` | a film's narration: the current beat's lines only, typed in under the picture |

- `src/story.js` names the deck: its title, its form (`deck` or `film`), its worlds and its scenes in the page's
  order. The engine says so on the console when the page and the scene files disagree.
- `src/worlds/<name>.js` builds the world once: its ground, its light, the things the claims are made of.
- `src/scenes/<id>.js`, one per scene: the world it is in, the beats' times (`dur`, the seconds a beat takes to play,
  and `hold`, the seconds auto-play then waits), and an `update` function that poses the world and its camera for a
  state.

**The one rule of the engine: `update` is a function of the state it is given, and of nothing else.** No counter kept
in the scene, no clock of its own, no `Math.random()` while updating (the kit has a seeded one for building). Why: a
viewer opens the third beat from a link, steps back, lets it play; the check photographs each beat directly. A scene
that accumulates shows each of them a different picture.

| in the state | what it is | what it is for |
|---|---|---|
| `s.beat` | the current beat's index, from 0 (a link counts from 1: `#weight-2` is `s.beat` 1) | |
| `s.p[k]` | the progress of beat k, 0 to 1: 1 for beats already played, rising for the current one, 0 for later ones | what a beat changes: `cube.scale.setScalar(s.p[0])` |
| `s.k(beat, from, to)` | 0 to 1, eased, between two moments in seconds inside that beat | placing an event in time |
| `s.t` | the seconds since the scene was entered | ambient life: a drift, a blink |
| `s.enter`, `s.exit` | 0 to 1 while the way into or out of the scene is running | moving things from where the last scene left them |
| `s.pointer` | the pointer's place, eased, -1 to 1 | a few centimetres of camera parallax |
| `s.aspect` | the picture's width over its height | fitting the subject on a phone |
| `s.still` | true under reduced motion: beats appear finished and `s.t` stands still | |

"In blocks" means the world out of the kit's boxes at real sizes (a door 2 m, a desk 0.75 m, a house 6 m; the kit has
a figure of 1.8 m), the theme's light, and for each scene the camera where it will stand. `npx vite` serves the deck;
`npx vite build` writes it to `dist/` with relative paths, so it works from any folder of a host (`npx vite build
--outDir site` when the task names another folder for the result). Build, run `check_site` on the built folder, and
read its contact sheet as a storyboard:

- Is each slide's subject found at the tile's size, and do the words lie where the picture is quiet?
- Does every beat's tile differ from the one before it, in its words or in its picture?
- With the words covered, do the tiles in order still say something?

Where the answer is no, change the beat sheet first and the scene second.

### 3. Theme, light, sets

[staging.md](staging.md) has each of these with its code. In short:

- **Choose the theme by looking.** Set `data-story-theme` on `<html>` to each of two candidates in turn, build, run
  `check_site` and compare the two contact sheets; keep one. (A viewer steps through the themes with `t` or the
  Theme button, and `?theme=<name>` on the address opens the deck in one.)

  | theme | the deck is | suits |
  |---|---|---|
  | `paper` | light, in daylight: a cool white, near-black type, one strong accent | most talks; anything read in a bright room |
  | `ink` | dark: deep blue-black, warm light type | a keynote in a dark room; things that glow |
  | `poster` | flat: a field of one strong colour and three more, heavy capitals, hard light | a pitch; an idea that should feel simple |
  | `halftone` | two inks printed as dots | a story that should feel printed or drawn |
  | `glyph` | text characters chosen by tone, with edge characters from depth | a noir, a terminal, anything made of text |

  A theme is a set of tokens at the top of `src/theme.css` (two grounds, two text colours, two accents, the lit and
  the shaded colour of a set, three type faces, one easing and one duration) and the stage's numbers for it in
  `src/theme.js` (the light, the fog, the print). For a project with its own colours and type, change the tokens of
  the nearest theme and nothing else: the words and the world follow together.
- **Paint with the theme, light with the theme.** The kit's `paint` is a material in a theme colour and its `rig` is
  the theme's light; a deck made with them reads in every theme. A literal colour is for the one thing that must
  keep it.
- **One accent, for the thing the deck follows.** Everything else in the lit and the shaded colour.
- **Keep the words' part of the frame quiet.** Each layout leaves the picture a part of the frame and takes a part
  for the words: keep the subject out of the words' part, and that part even in tone. The check measures the
  contrast of every line against the picture under it.
- **Three depths**: something near at an edge of the frame, the subject, something far and dim.
- **Build what the camera sees, from the side it sees it.**
- **Under a cell theme** (`halftone`, `glyph`) a detail smaller than two cells does not exist, and neither does a
  small difference in tone: compose in large shapes and clear silhouettes, and look at the phone's sheet, where the
  cells are fewest.

### 4. Mechanisms and motion

- **One change per beat, placed in time.** Begin it a second or so into the beat (`s.k(1, 1, 3.5)` runs from 1 s to
  3.5 s of the second beat), so the viewer has seen the picture before it changes, and end it before the beat ends.
- **A mechanism is posed, not animated.** Each takes a number (how much of the count is shown, how far the morph has
  gone) and a scene gives it one from the state. It then plays, holds, and goes back, with no code of its own.
- **Between scenes of one world the camera travels.** The engine takes the camera from where the last scene left it
  to where this one puts it, eased, in about a second and a half; the scene moves its things from where they were
  with `s.enter`. That continuity is most of what makes a deck feel made: a thing stays itself while the view of it
  changes.
- **One camera move per beat, and a slow one.** In a street-sized world 3 to 5 m over a beat; in a desk-sized one a
  tenth of that. The noir's moves: a push of 5 m in 16 s, a rise of 1.5 m with a push of 3 m in 20 s, a sway of
  0.18 m to either side.
- **Ambient life from `s.t`**: a cloud that drifts, a lamp that falters. Small and periodic.
- **Nothing turns for its own sake.** An object that spins because it can belongs to a product viewer.
- **A cut only where the world changes**, chosen for what happens between the two:

  | cut | reads as |
  |---|---|
  | `fade` | time passing, or the default when nothing else is meant |
  | `iris` | attention closing on one thing, or opening from it |
  | `wipe` | the next place being written over this one: a report, a list, a typewriter |
  | `fall` | the picture coming down: an ending, rain, a collapse |
  | `flip` | the other side of the same thing |

  The talk names its cuts after acts that belong to the scene they open: a fold into a paper boat, a slam for a
  calendar's page. A cut of the deck's own is one more entry in `src/cuts.js`.
- **Auto-play leaves a beat `dur` plus `hold` seconds after it began.** Give `hold` the time to read: about a second
  for every 12 characters of the beat's words, and never under 4 s.

### 5. A prop from the plugin

Where the camera comes close to one thing whose shape matters and boxes cannot give it (a house, an engine, a lens),
have the plugin build it. `src/props.js` has an entry per prop, and a scene asks the kit for it by name: until the
real one is installed it gets the scene's own version in blocks, so the deck never waits for a prop. Build, deliver
and install it as section 3 of [the scroll skill](../scroll-3d-website/SKILL.md) says (the same tools in the same
order: `apply_scene_ir` and `render_preview` while modelling, `deliver_package`, a look at the render it names,
`install_into_project`), then give its entry the `asset` and `module` fields. Ask for the deck's own colours in its
brief (the theme's lit, shaded and accent colours), and under a cell theme ask for its shape and leave surface detail
out: it does not print.

### 6. Look, revise

Run `check_site`, open both contact sheets, and fix what the next section lists. Then go through the deck as its
audience would, from its first beat, and say every note aloud (`?pace=4` on the page's address plays everything four
times as fast). A slide whose words you have to read to follow has too many; a beat you want to skip changes nothing.

## A film is a deck that plays by itself

`form: 'film'` in `src/story.js` puts the scene's title and the current beat's lines under the picture, typed in, and
the `caption` layout is its only layout; auto-play is then the way it is watched. Everything above holds. A film
more often changes world from scene to scene, so it uses the cuts, and it suits a cell theme. `examples/film/` is
one: five scenes, a halftone print, four cuts. Lay its files over a copy of the starter to run it.

## Check

The deck is done when `check_site` on the built folder reports, at both sizes:

- nothing broken: no uncaught error, no console error, no failed request, no request to another host; every beat
  draws a picture; every beat has its words, and they are the page's own text;
- nothing a viewer would notice: no beat that changes neither the words nor the picture; no scene in which nothing
  moves while a beat plays; no words too faint against the picture under them; no line cut by the frame or that has
  to be scrolled; no line of more than about 140 characters; no picture that is nearly one tone at the phone's size;
  the right arrow advances; a beat's link opens that beat;
- reduced motion shows every beat finished and standing still; a browser without WebGL shows the deck as an article,
  with one still per scene (the check writes them: use them);
- in a deck about a project: no fact that disagrees with its source;
- a time to the first picture, draw calls, triangles and bytes you can state and defend.

The check finds a broken beat, not a dull one. Look at the sheets yourself for the three questions of step 2.
Between looks the page's own hook answers at once: `window.__story.go(id, beat)` shows a beat finished,
`window.__story.play(id, beat)` plays it as a viewer sees it, `window.__story.cutAt(name, p, id, beat)` holds a cut
or a travel at a point of its way, and `window.__story.info()` says where the deck is and what it draws.

## Gotchas

- **Symptom:** the deck is a handsome tour of 3D scenes and nobody can say what it argued. **Cause:** the scenes were
  built first and the words written to fit them. **Remedy:** the argument first: claims that follow from one another,
  a metaphor only where a claim needs one, a last picture that answers the first.
- **Symptom:** every slide has a spinning object beside three bullet points. **Cause:** the 3D decorates the words
  instead of making their claim. **Remedy:** one claim a slide; either a plain statement over the world at rest, or
  a mechanism that behaves as the claim says (the table in step 1).
- **Symptom:** a beat opened from its link, or reached by stepping back, shows a different picture from the same beat
  reached by playing. **Cause:** the scene keeps state of its own: a counter, a tween, a random number drawn while
  updating. **Remedy:** compute every position, angle and intensity from the state (`s.p`, `s.k`, `s.t`, `s.enter`);
  draw random values once, while building, from the kit's seeded generator.
- **Symptom:** the deck hitches the first time a scene or a cut appears. **Cause:** a material, a light or a texture
  was made during an update, or a light was switched with `visible`, so a shader was compiled in front of the
  audience. **Remedy:** make everything while building; turn a light off with an intensity of 0 and a mesh off with
  `visible`. The starter draws every beat once before the deck starts, which compiles what exists then.
- **Symptom:** the words cannot be read over the picture. **Cause:** the subject, or a bright or busy part of the
  world, lies under them. **Remedy:** move the camera so the layout's part of the frame is quiet, or choose the
  layout whose words are on the other side; the check names the line and its contrast.
- **Symptom:** the deck looks right in one theme and wrong in the others. **Cause:** literal colours in the scenes.
  **Remedy:** the kit's `paint` and `rig`; a literal colour only for the one thing that must keep it.
- **Symptom:** on a phone the subject is small, or cut at the sides. **Cause:** a perspective camera keeps its
  vertical angle, so an upright frame sees a narrower world. **Remedy:** the kit's `fit` sets the field of view for
  the frame's shape and returns the distance at which a subject of a given width and height fills it; place every
  camera through it, and look at the phone's sheet.
- **Symptom:** in a film with a dark print of its own a scene is an even field of middle tone with no subject.
  **Cause:** the set is lit all over, so every cell has about the same tone. **Remedy:** start from dark, keep one
  light with a source in the world, and let most of the frame be dark (staging.md, "A film").
- **Symptom:** the page works at the server's root and loses its files under a sub-path. **Cause:** an absolute
  path (`/assets/...`). **Remedy:** relative URLs and Vite's `base: './'`, as the starter has them; `check_site`
  serves the deck under a sub-path to catch this.

## Version notes

Last verified 2026-11-28 against three 0.187.0 and Vite 8.3.8 (the starter's repository tests ran against these, and
the code in staging.md is quoted from the starter). The starter needs no other library; it uses the font packages
@fontsource-variable/space-grotesk, @fontsource-variable/inter and @fontsource/jetbrains-mono when the workspace has
them. The two pieces the method was read from are not in the repository.
