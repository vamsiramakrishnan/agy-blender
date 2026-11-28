// main.js: boot, the viewer's controls, the fallbacks, and window.__story.
// The story is the HTML (caption.js reads it); the film is story.js and src/scenes/; the engine is film.js. This file
// only connects them to the page: keys, clicks, buttons, the hash, the count, reduced motion, and no WebGL.
import { readStory, createCaption } from './caption.js';

const root = document.documentElement;
const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
const story = readStory($('story'));
// ?pace=N plays the whole film N times faster (beats, cuts, auto mode): for watching a long film through quickly.
const pace = Number(params.get('pace')) > 0 ? Number(params.get('pace')) : 1;

// The hook check_site and the tests drive. Until the film is ready its calls wait for it; without WebGL they reject.
let resolveReady, engine = null, why = null;
const call = (f) => (...a) => film.ready.then(() => {
  if (!engine) throw new Error('the film has no picture here: ' + why);
  return f(...a);
});
const film = {
  ready: new Promise((r) => { resolveReady = r; }),
  mode: 'starting', // 'webgl', or 'no-webgl' with the reason in `why`
  title: document.querySelector('.mark')?.textContent.trim() ?? document.title,
  treatment: '',
  scenes: story.map((s) => ({ id: s.id, title: s.title, beats: s.beats.length })),
  go: call((id, beat, opts) => engine.go(id, beat, opts)),
  play: call((id, beat) => engine.play(id, beat)),
  at: call((id, beat, p, t) => engine.at(id, beat, p, t)),
  // Not in the contract's table: a cut held at progress p, from the picture shown to (id, beat), for the tests' and
  // an author's look at a cut frame by frame.
  cutAt: call((name, p, id, beat) => engine.cutAt(name, p, id, beat)),
  advance: () => engine?.advance(),
  back: () => engine?.back(),
  setAuto: (on) => engine?.setAuto(on),
  info: () => (engine ? engine.info() : {}),
};
window.__story = film;

function webglWhyNot() {
  try {
    const gl = document.createElement('canvas').getContext('webgl2');
    if (!gl) return 'this browser has no WebGL 2';
    gl.getExtension('WEBGL_lose_context')?.loseContext(); // give the test context back: a page keeps only a few
    return null;
  } catch (e) {
    return 'WebGL failed: ' + (e && e.message ? e.message : e);
  }
}

// Without WebGL the page is the article: the HTML already says everything; a one-line note says why.
function articleOnly(reason) {
  root.classList.remove('film-on');
  film.mode = 'no-webgl';
  film.why = why = reason;
  $('note').hidden = false;
  resolveReady();
}

// Where a load or a hash points: "#room-2" is scene room, beat 2 (1-based); anything else is the start.
function fromHash() {
  const m = /^#([a-z0-9-]+)-(\d+)$/.exec(decodeURIComponent(location.hash));
  const i = m ? story.findIndex((s) => s.id === m[1]) : -1;
  const beat = m ? Number(m[2]) - 1 : 0;
  return i >= 0 && beat >= 0 && beat < story[i].beats.length ? { i, beat } : null;
}

// The count, the buttons and the hash follow every move; the hash with replaceState, so Back in the browser leaves
// the film instead of stepping through it.
function follow(info) {
  const i = story.findIndex((s) => s.id === info.scene);
  $('count').textContent = `Scene ${i + 1} of ${story.length}`;
  $('back').disabled = i === 0 && info.beat === 0;
  $('next').disabled = i === story.length - 1 && info.beat === info.beats - 1;
  $('auto').setAttribute('aria-pressed', String(info.auto));
  const hash = `#${info.scene}-${info.beat + 1}`;
  if (location.hash !== hash) history.replaceState(null, '', hash);
}

// The picture's box for a headline over it (style.css, data-place="over").
function measurePicture() {
  const r = $('picture').getBoundingClientRect();
  root.style.setProperty('--pic-left', r.left + 'px');
  root.style.setProperty('--pic-bottom', innerHeight - r.bottom + 'px');
  root.style.setProperty('--pic-w', r.width + 'px');
  root.style.setProperty('--pic-h', r.height + 'px');
}

function controls() {
  $('next').addEventListener('click', () => engine.advance());
  $('back').addEventListener('click', () => engine.back());
  $('auto').addEventListener('click', () => engine.setAuto(!engine.info().auto));
  $('picture').addEventListener('click', () => engine.advance());
  $('picture').addEventListener('pointermove', (e) => {
    if (e.pointerType !== 'mouse') return; // parallax is for a mouse; a finger on the picture is a tap
    const r = e.currentTarget.getBoundingClientRect();
    engine.point(((e.clientX - r.left) / r.width) * 2 - 1, ((e.clientY - r.top) / r.height) * 2 - 1);
  });
  $('picture').addEventListener('pointerleave', () => engine.point(0, 0));
  addEventListener('keydown', (e) => {
    if (e.altKey || e.ctrlKey || e.metaKey) return; // the browser's own shortcuts (Alt+Left is its Back)
    if (e.target.closest?.('button') && (e.key === ' ' || e.key === 'Enter')) return; // the button does it
    const act = {
      ArrowRight: () => engine.advance(), ' ': () => engine.advance(), PageDown: () => engine.advance(), Enter: () => engine.advance(),
      ArrowLeft: () => engine.back(), PageUp: () => engine.back(), Backspace: () => engine.back(),
      Home: () => engine.first(), End: () => engine.last(),
      a: () => engine.setAuto(!engine.info().auto),
      n: () => root.classList.toggle('notes-on'),
      f: () => (document.fullscreenElement ? document.exitFullscreen() : root.requestFullscreen?.())?.catch?.(() => {}),
    }[e.key];
    if (!act) return;
    e.preventDefault();
    act();
  });
  addEventListener('hashchange', () => { const h = fromHash(); if (h) engine.jump(story[h.i].id, h.beat); });
  reduced.addEventListener('change', () => engine.setStill(reduced.matches));
  addEventListener('resize', measurePicture);
}

async function boot() {
  const reason = webglWhyNot();
  if (reason) return articleOnly(reason);
  root.classList.add('film-on');
  measurePicture();
  const caption = createCaption(story);
  const [{ createFilm }, { film: definition }] = await Promise.all([import('./film.js'), import('./story.js')]);
  const loading = $('loading');
  engine = await createFilm({
    film: definition, story, caption, canvas: $('stage'), picture: $('picture'),
    treatment: params.get('print'), pace, still: reduced.matches, start: fromHash() ?? { i: 0, beat: 0 },
    loading: (k, n) => { loading.textContent = `Loading ${k} of ${n}`; },
    onChange: follow,
  });
  film.title = definition.title;
  film.treatment = engine.treatment;
  controls();
  await engine.nextDraw();
  loading.hidden = true;
  film.mode = 'webgl';
  resolveReady();
}

boot().catch((e) => {
  console.error('the film failed to start:', e);
  engine = null;
  articleOnly('the film failed to start');
});
