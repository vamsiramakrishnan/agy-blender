// film.js: the engine. It holds where the viewer is (a scene, a beat, when that beat began), builds the state `s`
// every scene's update(s) draws from, runs cuts and auto mode, and draws a frame only while something moves.
// Everything on screen is a function of (scene, beat progress, time): the same state gives the same picture, so any
// beat can be shown directly, forwards or backwards, and the check can photograph it (window.__story, main.js).
import * as THREE from 'three';
import * as kit from './kit.js';
import { createPrint, TREATMENTS } from './print.js';
import { CUTS, CUT_NAMES, QUICK } from './cuts.js';

const clamp01 = (x) => Math.min(1, Math.max(0, x));
// How fast the pointer's parallax follows the pointer, per frame: slow enough to feel like a camera operator.
const FOLLOW = 0.08;

// What does not match between story.js and the HTML: each is a broken film (a beat with no picture or no text).
function problems(film, story) {
  const out = [];
  const ids = film.scenes.map((d) => d.id), html = story.map((s) => s.id);
  if (ids.join() !== html.join()) out.push(`story.js has scenes [${ids}] and the HTML has [${html}], in that order`);
  film.scenes.forEach((d, i) => {
    const s = story[i];
    if (s && s.beats.length !== d.beats.length) out.push(`scene ${d.id}: ${d.beats.length} beats in its file, ${s.beats.length} in the HTML`);
    if (d.enterWith && !CUTS[d.enterWith]) out.push(`scene ${d.id}: no cut named "${d.enterWith}" in cuts.js`);
    s?.beats.forEach((b, k) => { if (!b.lines.length && !d.beats[k]?.silent) out.push(`scene ${d.id} beat ${k + 1} has no line and is not marked silent`); });
  });
  return out;
}

export async function createFilm({ film, story, caption, canvas, picture, treatment: asked, pace = 1, still = false, start, loading, onChange }) {
  for (const p of problems(film, story)) console.error(p);
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: false, powerPreference: 'high-performance' });
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFShadowMap;
  renderer.info.autoReset = false; // a frame is several renders (scenes, then the print): count them together
  kit.useRenderer(renderer);
  const name = TREATMENTS[asked] ? asked : typeof film.treatment === 'string' ? film.treatment : film.treatment.name;
  const print = createPrint(renderer, { treatment: TREATMENTS[name] ?? film.treatment, paper: film.paper, ink: film.ink });
  const size = () => print.resize(Math.max(1, picture.clientWidth), Math.max(1, picture.clientHeight));
  size();

  const defs = film.scenes;
  const clock = () => (performance.now() / 1000) * pace;
  const stillAt = (i) => defs[i].stillAt ?? 0;
  const dur = (i, b) => defs[i].beats[b].dur;
  let view = { i: 0, beat: 0, beatAt: 0, sceneAt: 0, hold: { p: 1, t: 0 } }; // hold: a fixed {p, t}, or null
  let cut = null; // { index, name, from: {i, beat, p, t}, at, dur, fixed: a fixed progress or null }
  let auto = false, dirty = true, frames = 0, stats = { calls: 0, triangles: 0 };
  const pointer = { x: 0, y: 0 }, aim = { x: 0, y: 0 };
  const waiting = []; // resolvers for the next frame drawn
  const plays = [];   // resolvers for the current beat's end

  // A beat's progress and the scene's ambient time as the viewer sees them now.
  const pOf = (v) => (v.hold ? v.hold.p : still ? 1 : clamp01((clock() - v.beatAt) / dur(v.i, v.beat)));
  const tOf = (v) => (v.hold ? v.hold.t : still ? stillAt(v.i) : clock() - v.sceneAt);

  // The state handed to update(s): see the contract's table. Nothing else reaches a scene.
  function state(i, beat, p, t, enter, exit) {
    const beats = defs[i].beats, ps = beats.map((_, k) => (k < beat ? 1 : k > beat ? 0 : p));
    return {
      beat, p: ps, t, enter, exit, still, aspect: print.aspect, pointer: { x: pointer.x, y: pointer.y },
      k: (b, from, to) => kit.ease((ps[b] * beats[b].dur - from) / Math.max(1e-6, to - from)),
    };
  }
  const sets = [];
  function shoot(which, i, beat, p, t, enter, exit) {
    const set = sets[i];
    set.camera.aspect = print.aspect;
    set.camera.updateProjectionMatrix();
    set.update(state(i, beat, p, t, enter, exit));
    print.shoot(which, set.scene, set.camera);
  }

  function draw() {
    renderer.info.reset();
    let q = cut ? cut.fixed ?? clamp01((clock() - cut.at) / cut.dur) : 1;
    if (cut && cut.fixed == null && (q >= 1 || still)) cut = null;
    const p = pOf(view), t = tOf(view);
    if (cut) {
      shoot(0, cut.from.i, cut.from.beat, cut.from.p, cut.from.t, 1, q);
      shoot(1, view.i, view.beat, p, t, q, 0);
      print.show(cut.index, q, Math.floor((q < 0.5 ? cut.from.t : t) * 12));
    } else {
      shoot(0, view.i, view.beat, p, t, 1, 0);
      print.show(-1, 0, Math.floor(t * 12)); // grain changes 12 times a second of scene time, and not when held
    }
    caption.reveal(still || p >= 1 ? Infinity : p * dur(view.i, view.beat));
    stats = { calls: renderer.info.render.calls, triangles: renderer.info.render.triangles };
    frames++;
    dirty = false;
  }

  // Warm-up: every scene created and drawn at the end of each of its beats, and every cut at its middle, into a
  // target off screen, so no shader compiles during the film. The frame says how far it has got.
  const warm = new THREE.WebGLRenderTarget(64, 64);
  const yieldFrame = () => new Promise((r) => requestAnimationFrame(() => r()));
  for (let i = 0; i < defs.length; i++) {
    loading(i + 1, defs.length + 1);
    await yieldFrame();
    sets[i] = await defs[i].create(kit);
    for (let b = 0; b < defs[i].beats.length; b++) { shoot(0, i, b, 1, stillAt(i), 1, 0); print.show(-1, 0, 0, warm); }
  }
  loading(defs.length + 1, defs.length + 1);
  await yieldFrame();
  CUT_NAMES.forEach((n, k) => {
    shoot(0, 0, 0, 1, stillAt(0), 1, 0.5);
    shoot(1, defs.length - 1, 0, 1, stillAt(defs.length - 1), 0.5, 0);
    print.show(k, 0.5, 0, warm);
  });
  warm.dispose();
  new ResizeObserver(() => { size(); dirty = true; }).observe(picture);

  function info() {
    const d = defs[view.i], beat = story[view.i]?.beats[view.beat];
    return {
      scene: d.id, beat: view.beat, beats: d.beats.length, title: story[view.i]?.title ?? '', lines: beat ? [...beat.lines] : [],
      notes: beat?.notes ?? '', place: beat?.place ?? 'under', playing: !view.hold && !still && pOf(view) < 1,
      cut: cut ? cut.name : null, auto, frames, ...stats, props: { ...kit.shown },
    };
  }
  function changed() {
    caption.show(view.i, view.beat);
    dirty = true;
    onChange(info());
  }
  const nextDraw = () => new Promise((r) => { dirty = true; waiting.push(r); });

  // The viewer's moves. To another beat of the same scene the scene's time carries on; to another scene it starts at
  // 0. `finished` shows the beat already played (going back). A cut runs to its end before the next move.
  function moveTo(i, beat, { via = null, quick = false, finished = false } = {}) {
    const now = clock(), from = { i: view.i, beat: view.beat, p: pOf(view), t: tOf(view) };
    view = { i, beat, beatAt: finished ? now - dur(i, beat) : now, sceneAt: i === from.i ? now - from.t : now, hold: null };
    cut = via && !still ? { index: CUT_NAMES.indexOf(via), name: via, from, at: now, dur: quick ? QUICK : CUTS[via].dur, fixed: null } : null;
    changed();
  }
  const busy = () => cut && cut.fixed == null;
  function advance() {
    if (busy()) return;
    if (view.beat < defs[view.i].beats.length - 1) return moveTo(view.i, view.beat + 1);
    const next = defs[view.i + 1];
    if (next) moveTo(view.i + 1, 0, { via: CUTS[next.enterWith] ? next.enterWith : 'fade' });
  }
  function back() {
    if (busy()) return;
    if (view.beat > 0) return moveTo(view.i, view.beat - 1, { via: 'fade', quick: true, finished: true });
    if (view.i > 0) moveTo(view.i - 1, defs[view.i - 1].beats.length - 1, { via: 'fade', quick: true, finished: true });
  }
  // A jump (Home, End, a hash): a quick fade to that beat, which then plays; earlier beats are shown finished.
  const jump = (i, beat) => moveTo(i, beat, { via: 'fade', quick: true });
  function setAuto(on) {
    auto = !!on;
    if (auto && view.hold) { const now = clock(); view = { ...view, beatAt: now - dur(view.i, view.beat), sceneAt: now - view.hold.t, hold: null }; }
    changed();
  }
  // Auto mode: the next beat dur + hold seconds after this one began; at the end of the film it turns itself off.
  function autoStep() {
    if (!auto || busy() || view.hold) return;
    const b = defs[view.i].beats[view.beat];
    if (clock() - view.beatAt < b.dur + b.hold) return;
    if (view.i === defs.length - 1 && view.beat === defs[view.i].beats.length - 1) { auto = false; changed(); return; }
    advance();
  }

  function tick() {
    requestAnimationFrame(tick);
    pointer.x += (aim.x - pointer.x) * FOLLOW;
    pointer.y += (aim.y - pointer.y) * FOLLOW;
    const following = Math.abs(aim.x - pointer.x) + Math.abs(aim.y - pointer.y) > 0.002;
    autoStep();
    const live = !view.hold && !still;
    if (cut || dirty || following || (live && (pOf(view) < 1 || defs[view.i].ambient))) {
      draw();
      waiting.splice(0).forEach((r) => r(info()));
    }
    if (plays.length && pOf(view) >= 1 && !cut) plays.splice(0).forEach((r) => r(info()));
  }

  // The hook's ways in (main.js puts them on window.__story). Each throws a short message for a scene or beat that
  // does not exist; main.js turns that into a rejected promise.
  function find(id, beat) {
    const i = defs.findIndex((d) => d.id === id);
    if (i < 0) throw new Error(`no scene "${id}"`);
    if (!Number.isInteger(beat) || beat < 0 || beat >= defs[i].beats.length) throw new Error(`scene "${id}" has no beat ${beat}`);
    return i;
  }
  const settle = (i, beat) => { cut = null; view = { i, beat, beatAt: 0, sceneAt: 0, hold: { p: 1, t: stillAt(i) } }; };
  const engine = {
    treatment: name, info, advance, back, setAuto, nextDraw,
    first: () => jump(0, 0),
    last: () => jump(defs.length - 1, defs[defs.length - 1].beats.length - 1),
    jump: (id, beat) => jump(find(id, beat), beat),
    point(x, y) { aim.x = x; aim.y = y; },
    setStill(on) { still = on; dirty = true; },
    // The beat finished, no cut, ambient time at the scene's stillAt, no parallax: the same picture every time.
    go(id, beat, { settle: s = true } = {}) {
      const i = find(id, beat);
      if (!s) { jump(i, beat); return nextDraw(); }
      settle(i, beat);
      pointer.x = pointer.y = aim.x = aim.y = 0;
      changed();
      return nextDraw();
    },
    // The beat played from 0 as a viewer sees it (earlier beats finished); resolves when it has played.
    play(id, beat) {
      const i = find(id, beat), now = clock();
      cut = null;
      view = { i, beat, beatAt: now, sceneAt: now - stillAt(i), hold: null };
      changed();
      return still ? nextDraw() : new Promise((r) => plays.push(r));
    },
    // The beat held at progress p and ambient time t.
    at(id, beat, p, t) {
      const i = find(id, beat);
      cut = null;
      view = { i, beat, beatAt: 0, sceneAt: 0, hold: { p: clamp01(+p || 0), t: +t || 0 } };
      changed();
      return nextDraw();
    },
    // The cut `cutName` held at progress p, from the picture shown now to beat `beat` of scene `id` settled.
    cutAt(cutName, p, id, beat) {
      if (!CUTS[cutName]) throw new Error(`no cut "${cutName}"`);
      const i = find(id, beat), from = { i: view.i, beat: view.beat, p: pOf(view), t: tOf(view) };
      settle(i, beat);
      cut = { index: CUT_NAMES.indexOf(cutName), name: cutName, from, at: 0, dur: 1, fixed: clamp01(+p || 0) };
      changed();
      return nextDraw();
    },
  };

  const now = clock();
  view = { i: start.i, beat: start.beat, beatAt: now, sceneAt: now, hold: null };
  changed();
  requestAnimationFrame(tick);
  return engine;
}
