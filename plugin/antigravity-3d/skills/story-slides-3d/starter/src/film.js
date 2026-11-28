// film.js: the engine. It holds where the viewer is (a scene, a beat, when that beat began), builds the state `s`
// every scene's update(s) draws from, runs cuts, travels and auto mode, and draws a frame only while something moves.
// Everything on screen is a function of (scene, beat progress, time): the same state gives the same picture, so any
// beat can be shown directly, forwards or backwards, and the check can photograph it (window.__story, main.js).
//
// A scene either builds a set of its own (create(kit) returns { scene, camera, update }) or names a world
// (`world: 'sky'`): the world is built once (src/worlds/, create(kit) returns { scene, camera, ...its parts }) and
// the scene's create(kit, world) returns only { update }, which poses the world's parts and its camera. Between two
// scenes of one world the picture is not cut: the camera travels (see `travel` below).
//
// The look follows the page's theme (data-story-theme on <html>, theme.js): the print's treatment, kit.paint and kit.rig.
// A story that names its own treatment in story.js (a film) keeps that treatment and its two inks in any theme.
import * as THREE from 'three';
import * as kit from './kit.js';
import { createPrint, TREATMENTS, FILM_LOOK } from './print.js';
import { CUTS, CUT_NAMES, QUICK, TRAVEL } from './cuts.js';
import { THEMES, THEME_NAMES, DEFAULT_THEME, readTheme } from './theme.js';

const clamp01 = (x) => Math.min(1, Math.max(0, x));
// How fast the pointer's parallax follows the pointer, per frame: slow enough to feel like a camera operator.
const FOLLOW = 0.08;

// What does not match between story.js and the HTML: each is a broken story (a beat with no picture or no text).
function problems(film, story) {
  const out = [];
  const ids = film.scenes.map((d) => d.id), html = story.map((s) => s.id);
  if (ids.join() !== html.join()) out.push(`story.js has scenes [${ids}] and the HTML has [${html}], in that order`);
  film.scenes.forEach((d, i) => {
    const s = story[i], before = film.scenes[i - 1];
    if (s && s.beats.length !== d.beats.length) out.push(`scene ${d.id}: ${d.beats.length} beats in its file, ${s.beats.length} in the HTML`);
    if (d.world && !film.worlds?.[d.world]) out.push(`scene ${d.id}: story.js has no world "${d.world}" (worlds: { ${d.world} })`);
    if (d.enterWith === 'travel' && !(d.world && before?.world === d.world)) out.push(`scene ${d.id}: a travel needs the scene before it to be in the same world`);
    if (d.enterWith && d.enterWith !== 'travel' && !CUTS[d.enterWith]) out.push(`scene ${d.id}: no cut named "${d.enterWith}" in cuts.js`);
    s?.beats.forEach((b, k) => { if (!b.lines.length && !d.beats[k]?.silent) out.push(`scene ${d.id} beat ${k + 1} has no line and is not marked silent`); });
  });
  return out;
}

// A theme's paints for the print, in pairs: each colour as painted and as it is in shade (times the share of the lit
// colour the shade keeps, which is what kit.rig's fill gives every paint). The poster print gives every pixel one of
// them, and a print in two inks tells by them what is painted in the accent. The accent's pair comes first (ACCENT is
// its place); the page's paper comes last and alone, as nothing shades a sky.
const PAINTS = ['accent', 'paper-2', 'ink', 'accent-2', 'lit', 'shade'], ACCENT = 0;
function paintsOf(theme) {
  const c = theme.colours, lit = new THREE.Color(c.lit), shade = new THREE.Color(c.shade);
  const keep = [lit.r, lit.g, lit.b].map((v, k) => (v > 1e-4 ? Math.min(1, [shade.r, shade.g, shade.b][k] / v) : 1) * theme.fill);
  const pairs = [];
  for (const t of PAINTS) {
    const k = new THREE.Color(c[t]);
    pairs.push(c[t], '#' + k.setRGB(Math.min(1, k.r * keep[0]), Math.min(1, k.g * keep[1]), Math.min(1, k.b * keep[2])).getHexString());
  }
  return pairs.concat(c.paper, c.paper);
}

export async function createFilm({ film, story, caption, canvas, picture, treatment: asked, pace = 1, still = false, start, loading, onChange }) {
  for (const p of problems(film, story)) console.error(p);
  const root = canvas.ownerDocument.documentElement;
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: false, powerPreference: 'high-performance' });
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFShadowMap;
  renderer.info.autoReset = false; // a frame is several renders (scenes, then the print): count them together
  // Where the page is drawn in software (no graphics card: a headless check, a virtual machine) a frame is drawn
  // lighter: no multisampling and a shadow map half the size. It costs a third as much there, and the picture is the
  // same but for the smoothness of its edges. info().light says so; ?light=0 or ?light=1 in the address sets it by
  // hand, for a look at the other.
  const gl = renderer.getContext(), named = gl.getExtension('WEBGL_debug_renderer_info');
  const wish = new URLSearchParams(canvas.ownerDocument.defaultView?.location.search ?? '').get('light');
  const light = wish === '1' || (wish !== '0' && /swiftshader|llvmpipe|softpipe|software/i.test(String(gl.getParameter(named ? named.UNMASKED_RENDERER_WEBGL : gl.RENDERER))));
  kit.useRenderer(renderer, { light });

  // The theme is the page's: data-story-theme on <html>, read with the CSS as it stands. kit.paint and kit.rig take it
  // before any scene is built.
  let theme = readTheme(root.dataset.storyTheme || DEFAULT_THEME, root);
  kit.useTheme(theme);
  // How the picture is printed: the treatment asked for in the address (?print=) for a look, else the story's own
  // (then with the film's look and the story's two inks, whatever the theme), else the theme's.
  const own = film.treatment != null;
  function printing() {
    const name = TREATMENTS[asked] ? asked : own ? (typeof film.treatment === 'string' ? film.treatment : film.treatment.name) : theme.print.treatment;
    return {
      name, treatment: TREATMENTS[name] ?? film.treatment, look: own ? FILM_LOOK : theme.print,
      paper: film.paper ?? theme.colours.paper, ink: film.ink ?? theme.colours.ink,
      palette: own ? [] : paintsOf(theme), accent: own ? -1 : ACCENT,
    };
  }
  let printed = printing();
  const print = createPrint(renderer, { ...printed, smooth: !light });
  const size = (share = 1) => print.resize(Math.max(1, Math.round(picture.clientWidth * share)), Math.max(1, Math.round(picture.clientHeight * share)));
  size(0.25); // the warm-up below draws small: a shader compiles the same at any size

  const defs = film.scenes;
  const clock = () => (performance.now() / 1000) * pace;
  const stillAt = (i) => defs[i].stillAt ?? 0;
  const dur = (i, b) => defs[i].beats[b].dur;
  const sameWorld = (a, b) => !!defs[a].world && defs[a].world === defs[b].world;
  // The layout of each scene's slide, as the page says it (data-layout on its section): info() passes it on.
  const layouts = defs.map((d) => canvas.ownerDocument.querySelector(`section[data-scene="${d.id}"]`)?.dataset.layout || 'caption');
  let view = { i: 0, beat: 0, beatAt: 0, sceneAt: 0, hold: { p: 1, t: 0 } }; // hold: a fixed {p, t}, or null
  let cut = null; // { index, name, from: {i, beat, p, t}, at, dur, fixed: a fixed progress or null, back, q }
  let auto = false, dirty = true, frames = 0, stats = { calls: 0, triangles: 0, shots: 0 };
  const pointer = { x: 0, y: 0 }, aim = { x: 0, y: 0 };
  const waiting = []; // resolvers for the next frame drawn
  const plays = [];   // resolvers for the current beat's end

  // A beat's progress and the scene's ambient time as the viewer sees them now.
  const pOf = (v) => (v.hold ? v.hold.p : still ? 1 : clamp01((clock() - v.beatAt) / dur(v.i, v.beat)));
  const tOf = (v) => (v.hold ? v.hold.t : still ? stillAt(v.i) : clock() - v.sceneAt);

  // The state handed to update(s): see the contract's table. Nothing else reaches a scene. One is made per scene and
  // filled before each update (stateOf), so a frame makes no garbage.
  function state(d) {
    const ps = d.beats.map(() => 0), pointerNow = { x: 0, y: 0 };
    const k = (b, from, to) => kit.ease((ps[b] * d.beats[b].dur - from) / Math.max(1e-6, to - from));
    return { beat: 0, p: ps, k, t: 0, enter: 1, exit: 0, still, aspect: 1, pointer: pointerNow };
  }
  const states = defs.map(state);
  function stateOf(i, beat, p, t, enter, exit) {
    const s = states[i];
    for (let k = 0; k < s.p.length; k++) s.p[k] = k < beat ? 1 : k > beat ? 0 : p;
    s.beat = beat; s.t = t; s.enter = enter; s.exit = exit; s.still = still; s.aspect = print.aspect;
    s.pointer.x = pointer.x; s.pointer.y = pointer.y;
    return s;
  }
  // Poses a set for a state. A world is first put to rest (when it has a rest()), so a part this scene does not pose
  // is where the world keeps it, whichever scene posed the world last.
  const sets = [], worlds = {};
  function pose(i, beat, p, t, enter, exit) {
    const set = sets[i];
    set.camera.aspect = print.aspect;
    set.world?.rest?.();
    set.update(stateOf(i, beat, p, t, enter, exit));
    set.camera.updateProjectionMatrix();
    return set;
  }
  function shoot(which, i, beat, p, t, enter, exit) {
    const set = pose(i, beat, p, t, enter, exit);
    print.shoot(which, set.scene, set.camera);
  }

  // A travel: the camera between two poses of one world. Position and field of view go straight from one to the
  // other, the orientation turns the short way, and the near and far planes go in proportion (one may be a pea's
  // distance and the other a kilometre's).
  const note = () => ({ position: new THREE.Vector3(), quaternion: new THREE.Quaternion(), fov: 0, near: 0, far: 0 });
  const camA = note(), camB = note();
  function take(camera, into) {
    into.position.copy(camera.position);
    into.quaternion.copy(camera.quaternion);
    into.fov = camera.fov; into.near = camera.near; into.far = camera.far;
  }
  function between(camera, a, b, x) {
    camera.position.lerpVectors(a.position, b.position, x);
    camera.quaternion.slerpQuaternions(a.quaternion, b.quaternion, x);
    camera.fov = a.fov + (b.fov - a.fov) * x;
    camera.near = a.near * Math.pow(b.near / a.near, x);
    camera.far = a.far * Math.pow(b.far / a.far, x);
    camera.updateProjectionMatrix();
  }

  // One frame, to the canvas or to `out`: the beat `v` ({ i, beat, p, t }) alone, or with `c` a cut or a travel into
  // it from c.from at progress c.q.
  // - a cut: both scenes are drawn, the leaving one with s.exit and the arriving one with s.enter, and the print
  //   chooses between them cell by cell;
  // - a travel: the leaving scene is posed and its camera noted, the arriving scene is posed (s.enter is the travel's
  //   progress: it moves its parts from where the last scene left them) and its camera noted, and the world is drawn
  //   once with the camera between the two. Going back, the arriving scene is as it was left (s.enter 1).
  function render(v, c, out = null) {
    if (!c) {
      shoot(0, v.i, v.beat, v.p, v.t, 1, 0);
      print.show(-1, 0, Math.floor(v.t * 12), out); // grain changes 12 times a second of scene time, and not when held
      return;
    }
    const f = c.from, q = c.q;
    if (c.name !== 'travel') {
      shoot(0, f.i, f.beat, f.p, f.t, 1, q);
      shoot(1, v.i, v.beat, v.p, v.t, q, 0);
      print.show(c.index, q, Math.floor((q < 0.5 ? f.t : v.t) * 12), out);
      return;
    }
    if (q <= 0) shoot(0, f.i, f.beat, f.p, f.t, 1, 0); // its ends are the scenes' own pictures, exactly
    else if (q >= 1) shoot(0, v.i, v.beat, v.p, v.t, 1, 0);
    else {
      take(pose(f.i, f.beat, f.p, f.t, 1, q).camera, camA);
      const set = pose(v.i, v.beat, v.p, v.t, c.back ? 1 : q, 0);
      take(set.camera, camB);
      between(set.camera, camA, camB, kit.ease(q));
      print.shoot(0, set.scene, set.camera);
    }
    print.show(-1, 0, Math.floor(v.t * 12), out);
  }

  const now = { i: 0, beat: 0, p: 1, t: 0 }; // what the last frame showed
  function draw() {
    renderer.info.reset();
    const shots = print.shots;
    const q = cut ? cut.fixed ?? clamp01((clock() - cut.at) / cut.dur) : 1;
    if (cut && cut.fixed == null && (q >= 1 || still)) cut = null;
    now.i = view.i; now.beat = view.beat; now.p = pOf(view); now.t = tOf(view);
    if (cut) cut.q = q;
    render(now, cut);
    caption.reveal(still || now.p >= 1 ? Infinity : now.p * dur(view.i, view.beat));
    stats.calls = renderer.info.render.calls;
    stats.triangles = renderer.info.render.triangles;
    stats.shots = print.shots - shots;
    frames++;
    dirty = false;
  }

  // How scene `to` is entered from scene `from` going forward: the cut it names, else a travel when the two share a
  // world, else a fade.
  function entrance(from, to) {
    const d = defs[to];
    if (d.enterWith && d.enterWith !== 'travel') return CUTS[d.enterWith] ? d.enterWith : 'fade';
    return sameWorld(from, to) ? 'travel' : 'fade';
  }

  // Warm-up: every world and scene created, every scene drawn at the end of each of its beats, every travel and every
  // cut the story names (and the fade of a jump) at its middle, into a target off screen and at a quarter of the
  // picture's size, so no shader compiles during the story. The frame says how far it has got.
  const warm = new THREE.WebGLRenderTarget(64, 64);
  const yieldFrame = () => new Promise((r) => requestAnimationFrame(() => r()));
  for (let i = 0; i < defs.length; i++) {
    loading(i + 1, defs.length + 1);
    await yieldFrame();
    const d = defs[i];
    if (d.world) {
      const made = film.worlds[d.world];
      const world = worlds[d.world] ??= await (typeof made === 'function' ? made : made.create)(kit);
      sets[i] = { scene: world.scene, camera: world.camera, world, update: (await d.create(kit, world)).update };
    } else sets[i] = await d.create(kit);
    for (let b = 0; b < d.beats.length; b++) render({ i, beat: b, p: 1, t: stillAt(i) }, null, warm);
    if (i && entrance(i - 1, i) === 'travel') {
      const from = { i: i - 1, beat: defs[i - 1].beats.length - 1, p: 1, t: stillAt(i - 1) };
      render({ i, beat: 0, p: 0, t: stillAt(i) }, { name: 'travel', from, q: 0.5, back: false }, warm);
    }
  }
  loading(defs.length + 1, defs.length + 1);
  await yieldFrame();
  for (const name of new Set(['fade', ...defs.map((d) => d.enterWith).filter((n) => CUTS[n])])) {
    const last = defs.length - 1;
    render({ i: last, beat: 0, p: 1, t: stillAt(last) }, { name, index: CUT_NAMES.indexOf(name), from: { i: 0, beat: 0, p: 1, t: stillAt(0) }, q: 0.5 }, warm);
  }
  warm.dispose();
  size();
  new ResizeObserver(() => { size(); dirty = true; }).observe(picture);

  function info() {
    const d = defs[view.i], beat = story[view.i]?.beats[view.beat], camera = sets[view.i].camera;
    return {
      scene: d.id, beat: view.beat, beats: d.beats.length, title: story[view.i]?.title ?? '', lines: beat ? [...beat.lines] : [],
      notes: beat?.notes ?? '', place: beat?.place ?? 'under', playing: !view.hold && !still && pOf(view) < 1,
      cut: cut ? cut.name : null, auto, frames, ...stats, props: { ...kit.shown },
      layout: layouts[view.i], form: film.form ?? 'film', theme: theme.name, treatment: printed.name, light,
      // where the last frame was taken from: for a look at a camera move, and for the tests of a travel
      camera: { position: camera.position.toArray(), fov: camera.fov },
    };
  }
  function changed() {
    caption.show(view.i, view.beat);
    dirty = true;
    onChange(info());
  }
  const nextDraw = () => new Promise((r) => { dirty = true; waiting.push(r); });

  // The theme: re-read from the page, handed to every paint and rig, and to the print. The engine follows the
  // attribute on <html>, so the page and the picture cannot be in two themes; setTheme sets the attribute.
  function applyTheme(name) {
    theme = readTheme(name, root);
    kit.useTheme(theme);
    printed = printing();
    print.set(printed);
    changed();
  }
  new MutationObserver(() => {
    const name = THEMES[root.dataset.storyTheme] ? root.dataset.storyTheme : DEFAULT_THEME;
    if (name !== theme.name) applyTheme(name);
  }).observe(root, { attributes: true, attributeFilter: ['data-story-theme'] });

  // The viewer's moves. To another beat of the same scene, or to another scene of the same world, the ambient time
  // carries on; to another set it starts at 0. `finished` shows the beat already played (going back). A cut plays
  // over the beginning of its beat, as in a film; a travel is the scene's entrance, and its first beat begins when
  // the camera has arrived. A cut or a travel runs to its end before the next move.
  function moveTo(i, beat, { via = null, quick = false, finished = false } = {}) {
    const at = clock(), from = { i: view.i, beat: view.beat, p: pOf(view), t: tOf(view) };
    const travel = via === 'travel';
    const length = !via || still ? 0 : travel ? (quick ? TRAVEL.back : defs[i].travel ?? TRAVEL.dur) : quick ? QUICK : CUTS[via].dur;
    view = {
      i, beat, beatAt: finished ? at - dur(i, beat) : travel ? at + length : at,
      sceneAt: i === from.i || sameWorld(i, from.i) ? at - from.t : at, hold: null,
    };
    cut = length ? { index: CUT_NAMES.indexOf(via), name: via, from, at, dur: length, fixed: null, back: travel && quick, q: 0 } : null;
    changed();
  }
  const busy = () => cut && cut.fixed == null;
  function advance() {
    if (busy()) return;
    if (view.beat < defs[view.i].beats.length - 1) return moveTo(view.i, view.beat + 1);
    if (defs[view.i + 1]) moveTo(view.i + 1, 0, { via: entrance(view.i, view.i + 1) });
  }
  function back() {
    if (busy()) return;
    if (view.beat > 0) return moveTo(view.i, view.beat - 1, { via: 'fade', quick: true, finished: true });
    if (view.i > 0) {
      const via = entrance(view.i - 1, view.i) === 'travel' ? 'travel' : 'fade';
      moveTo(view.i - 1, defs[view.i - 1].beats.length - 1, { via, quick: true, finished: true });
    }
  }
  // A jump (Home, End, a hash): a quick fade to that beat, which then plays; earlier beats are shown finished.
  const jump = (i, beat) => moveTo(i, beat, { via: 'fade', quick: true });
  function setAuto(on) {
    auto = !!on;
    if (auto && view.hold) { const at = clock(); view = { ...view, beatAt: at - dur(view.i, view.beat), sceneAt: at - view.hold.t, hold: null }; }
    changed();
  }
  // Auto mode: the next beat dur + hold seconds after this one began; at the end of the story it turns itself off.
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
    // A beat that plays is drawn until a frame has shown it finished (now.p is the last frame's), not only until its
    // time is up: its last frame is its end, exactly.
    const live = !view.hold && !still;
    if (cut || dirty || following || (live && (now.p < 1 || defs[view.i].ambient))) {
      draw();
      if (waiting.length) { const i = info(); waiting.splice(0).forEach((r) => r(i)); }
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
    get treatment() { return printed.name; },
    themes: [...THEME_NAMES],
    info, advance, back, setAuto, nextDraw,
    first: () => jump(0, 0),
    last: () => jump(defs.length - 1, defs[defs.length - 1].beats.length - 1),
    jump: (id, beat) => jump(find(id, beat), beat),
    point(x, y) { aim.x = x; aim.y = y; },
    setStill(on) { still = on; dirty = true; },
    // The theme by name: sets it on the page (the slides follow through the CSS) and on the stage, and draws.
    setTheme(name) {
      if (!THEMES[name]) throw new Error(`no theme "${name}"`);
      if (root.dataset.storyTheme !== name) root.dataset.storyTheme = name;
      applyTheme(name);
      return nextDraw();
    },
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
      const i = find(id, beat), at = clock();
      cut = null;
      view = { i, beat, beatAt: at, sceneAt: at - stillAt(i), hold: null };
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
    // The cut `cutName` held at progress p, from the picture shown now to beat `beat` of scene `id` settled. Or
    // 'travel' held at p of its way, as a viewer sees it: from the picture shown now to the beginning of that beat
    // (a travel is an entrance: the beat begins when the camera has arrived), the world's time carrying on.
    cutAt(cutName, p, id, beat) {
      const travel = cutName === 'travel';
      if (!travel && !CUTS[cutName]) throw new Error(`no cut "${cutName}"`);
      const i = find(id, beat), from = { i: view.i, beat: view.beat, p: pOf(view), t: tOf(view) };
      if (travel && !sameWorld(from.i, i)) throw new Error(`no travel from "${defs[from.i].id}" to "${id}": not one world`);
      settle(i, beat);
      if (travel) view.hold = { p: 0, t: from.t };
      cut = { index: CUT_NAMES.indexOf(cutName), name: cutName, from, at: 0, dur: 1, fixed: clamp01(+p || 0), back: false, q: 0 };
      changed();
      return nextDraw();
    },
  };

  const at = clock();
  view = { i: start.i, beat: start.beat, beatAt: at, sceneAt: at, hold: null };
  changed();
  requestAnimationFrame(tick);
  return engine;
}
