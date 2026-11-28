// main.js: boot, the viewer's and the presenter's controls, the fallbacks, and window.__story.
// The story is the HTML (caption.js reads it); its pictures are story.js and src/scenes/; the engine is film.js. This
// file only connects them to the page: the form and the theme, keys, clicks, buttons, the hash, the count, the
// progress line, the overview, reduced motion, and no WebGL.
import { readStory, createCaption, formOf } from './caption.js';
// The themes' faces (theme.css names them first in its stacks). They are optional packages: where the workspace has
// none, vite.config.js makes each of these an empty module and the stacks' next faces are used.
import '@fontsource-variable/space-grotesk';
import '@fontsource-variable/inter';
import '@fontsource/jetbrains-mono/400.css';
import '@fontsource/jetbrains-mono/500.css';
import '@fontsource/jetbrains-mono/700.css';

const root = document.documentElement;
const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
const story = readStory($('story'));
const beatsInAll = story.reduce((n, s) => n + s.beats.length, 0);
// ?pace=N plays the whole story N times faster (beats, cuts, auto mode): for watching a long one through quickly.
const pace = Number(params.get('pace')) > 0 ? Number(params.get('pace')) : 1;

// The themes theme.css defines, in the order the key t goes through them (the engine's own list takes this one's
// place when it has one). <html data-story-theme> names the story's; ?theme=name shows it in another.
const THEMES = ['paper', 'ink', 'poster', 'halftone', 'glyph'];
// The form is story.js's to say (form: 'deck' or 'film'), but story.js arrives with three.js, after the frame is up.
// Until then the frame takes the form the page itself names (<html data-form>), or else a deck's when a scene asks
// for a layout, so a deck does not begin as a film.
let form = ['deck', 'film'].includes(root.dataset.form) ? root.dataset.form : story.some((s) => s.layout !== 'caption') ? 'deck' : 'film';
// A page that names no theme is `paper`; a film that names none keeps the film's colours, which are `ink`.
const themeNamed = root.hasAttribute('data-story-theme') || THEMES.includes(params.get('theme'));
if (THEMES.includes(params.get('theme'))) root.dataset.storyTheme = params.get('theme');
else if (!themeNamed) root.dataset.storyTheme = form === 'film' ? 'ink' : 'paper';

// The hook check_site and the tests drive. Until the story is ready its calls wait for it; without WebGL they reject.
// What the page knows of where the story is (the scene's layout, the form, the theme) is in every info(); where the
// engine gives the same fields its own are the ones returned.
let resolveReady, engine = null, why = null;
const withPage = (info) => (info && typeof info === 'object'
  ? { layout: story.find((s) => s.id === info.scene)?.layout ?? 'caption', form, theme: root.dataset.storyTheme, ...info }
  : info);
const call = (f) => (...a) => film.ready.then(() => {
  if (!engine) throw new Error('the story has no picture here: ' + why);
  return f(...a);
});
const film = {
  ready: new Promise((r) => { resolveReady = r; }),
  mode: 'starting', // 'webgl', or 'no-webgl' with the reason in `why`
  title: document.querySelector('.mark')?.textContent.trim() ?? document.title,
  treatment: '',
  form,
  scenes: story.map((s) => ({ id: s.id, title: s.title, beats: s.beats.length, layout: s.layout })),
  go: call((id, beat, opts) => engine.go(id, beat, opts).then(withPage)),
  play: call((id, beat) => engine.play(id, beat).then(withPage)),
  at: call((id, beat, p, t) => engine.at(id, beat, p, t).then(withPage)),
  // Not in the contract's table: a cut held at progress p, from the picture shown to (id, beat), for the tests' and
  // an author's look at a cut frame by frame.
  cutAt: call((name, p, id, beat) => engine.cutAt(name, p, id, beat).then(withPage)),
  advance: () => engine?.advance(),
  back: () => engine?.back(),
  setAuto: (on) => engine?.setAuto(on),
  info: () => (engine ? withPage(engine.info()) : {}),
  // Also not in the table: the themes, and the story shown in one of them (what the key t does).
  get themes() { return themes(); },
  setTheme: (name) => setTheme(name),
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

// The themes: the engine's when it has its own list, else theme.css's. Setting one names it on <html>, which is all
// the page needs (every colour and face is a token), and tells the engine, which repaints the stage in it.
const themes = () => (Array.isArray(engine?.themes) && engine.themes.length ? [...engine.themes] : [...THEMES]);
function setTheme(name) {
  if (!themes().includes(name)) return;
  root.dataset.storyTheme = name;
  engine?.setTheme?.(name);
  loadFaces();
}
const nextTheme = () => { const all = themes(); setTheme(all[(all.indexOf(root.dataset.storyTheme) + 1) % all.length]); };

// The faces the theme sets the page in (its display face, its text face, the mono of the labels), fetched before the
// first slide shows: a line first set in a fallback face is set again, and moves, when its face arrives mid-talk,
// and type the stage draws into its pictures would be drawn in the wrong face. Faces the workspace does not have
// resolve at once (the stacks' next faces are a system's).
function loadFaces() {
  if (!document.fonts?.load) return Promise.resolve();
  const cs = getComputedStyle(root), token = (name, fallback) => cs.getPropertyValue(name).trim() || fallback;
  const wanted = [
    `${token('--style-display', 'normal')} ${token('--weight-display', '400')} 1em ${token('--font-display', 'sans-serif')}`,
    `400 1em ${token('--font-mono', 'monospace')}`,
    `500 1em ${token('--font-mono', 'monospace')}`,
  ];
  // The text face sets a slide's smaller words; a film whose every scene is a typed caption never uses it.
  if (form === 'deck' || story.some((s) => s.layout !== 'caption')) wanted.push(`${token('--weight-text', '400')} 1em ${token('--font-text', 'sans-serif')}`);
  return Promise.all(wanted.map((font) => document.fonts.load(font).catch(() => {})));
}

// The presenter's furniture, made here so index.html needs no markup for it: the progress line along the bottom
// edge (beats done over all beats) and the overview (the key o), every scene as a card.
let progress = null, overview = null;
function presenter() {
  progress = document.createElement('div');
  progress.id = 'progress';
  progress.className = 'progress';
  progress.setAttribute('role', 'progressbar');
  progress.setAttribute('aria-label', 'Beats shown');
  progress.setAttribute('aria-valuemin', '0');
  progress.setAttribute('aria-valuemax', String(beatsInAll));
  progress.append(document.createElement('span'));

  const el = document.createElement('div');
  el.id = 'overview';
  el.className = 'overview';
  el.hidden = true;
  el.setAttribute('role', 'dialog');
  el.setAttribute('aria-modal', 'true');
  el.setAttribute('aria-label', 'Scenes');
  const list = el.appendChild(document.createElement('ol'));
  const close = () => {
    if (el.hidden) return;
    el.hidden = true;
    root.classList.remove('overview-on');
    if (el.contains(document.activeElement)) document.activeElement.blur();
  };
  const cards = story.map((s, i) => {
    const card = document.createElement('button');
    card.type = 'button';
    card.dataset.scene = s.id;
    const n = document.createElement('span'), h = document.createElement('span'), marks = document.createElement('span');
    n.className = 'n';
    n.textContent = String(i + 1).padStart(2, '0');
    h.className = 'h';
    h.textContent = s.title || s.id;
    marks.className = 'b';
    marks.setAttribute('aria-hidden', 'true');
    for (let k = 0; k < s.beats.length; k++) marks.append(document.createElement('i'));
    card.append(n, h, marks);
    card.addEventListener('click', () => { close(); engine.jump(s.id, 0); });
    list.appendChild(document.createElement('li')).append(card);
    return card;
  });
  el.addEventListener('click', (e) => { if (!e.target.closest('button')) close(); }); // a click beside the cards
  document.body.append(progress, el);
  overview = {
    get open() { return !el.hidden; },
    close,
    toggle() {
      if (!el.hidden) return close();
      el.hidden = false;
      root.classList.add('overview-on');
      (cards.find((c) => c.getAttribute('aria-current') === 'true') ?? cards[0])?.focus();
    },
    // The arrow keys move among the cards as they lie: left and right by one, up and down by a row.
    move(key) {
      const at = Math.max(0, cards.indexOf(document.activeElement));
      const perRow = Math.max(1, cards.filter((c) => c.offsetTop === cards[0].offsetTop).length);
      const step = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: perRow, ArrowUp: -perRow }[key] ?? 0;
      cards[Math.min(cards.length - 1, Math.max(0, at + step))]?.focus();
    },
    // The current scene's card is marked, and each card shows how many of its beats are done.
    follow(i, beat) {
      cards.forEach((card, k) => {
        if (k === i) card.setAttribute('aria-current', 'true');
        else card.removeAttribute('aria-current');
        [...card.querySelectorAll('.b i')].forEach((mark, b) => mark.classList.toggle('done', k < i || (k === i && b <= beat)));
      });
    },
  };
}

// The count, the progress line, the buttons, the overview and the hash follow every move; the hash with
// replaceState, so Back in the browser leaves the story instead of stepping through it.
function follow(info) {
  const i = story.findIndex((s) => s.id === info.scene);
  $('count').textContent = form === 'deck' ? `${i + 1} / ${story.length}` : `Scene ${i + 1} of ${story.length}`;
  $('back').disabled = i === 0 && info.beat === 0;
  $('next').disabled = i === story.length - 1 && info.beat === info.beats - 1;
  $('auto').setAttribute('aria-pressed', String(info.auto));
  const done = story.slice(0, i).reduce((n, s) => n + s.beats.length, 0) + info.beat + 1;
  progress?.setAttribute('aria-valuenow', String(done));
  progress?.style.setProperty('--done', String(done / Math.max(1, beatsInAll)));
  overview?.follow(i, info.beat);
  const hash = `#${info.scene}-${info.beat + 1}`;
  if (location.hash !== hash) history.replaceState(null, '', hash);
}

// The picture's box, for words laid over it where the picture is not the whole frame (style.css).
function measurePicture() {
  const r = $('picture').getBoundingClientRect();
  root.style.setProperty('--pic-left', r.left + 'px');
  root.style.setProperty('--pic-bottom', innerHeight - r.bottom + 'px');
  root.style.setProperty('--pic-w', r.width + 'px');
  root.style.setProperty('--pic-h', r.height + 'px');
}

// A deck's strip and controls fade when the pointer rests (style.css, .idle) and come back when it moves. Keys do
// not bring them back: a presenter with a clicker should not see them at every beat. Under reduced motion they stay:
// a viewer who asked for less motion is shown a page on which nothing changes by itself.
const REST_MS = 2600;
let resting = 0;
function wake() {
  root.classList.remove('idle');
  clearTimeout(resting);
  if (!reduced.matches) resting = setTimeout(() => root.classList.add('idle'), REST_MS);
}

function controls() {
  $('next').addEventListener('click', () => engine.advance());
  $('back').addEventListener('click', () => engine.back());
  $('auto').addEventListener('click', () => engine.setAuto(!engine.info().auto));
  $('theme')?.addEventListener('click', nextTheme); // a page may leave this button out: the key t does the same
  $('picture').addEventListener('click', () => engine.advance());
  $('picture').addEventListener('pointermove', (e) => {
    if (e.pointerType !== 'mouse') return; // parallax is for a mouse; a finger on the picture is a tap
    const r = e.currentTarget.getBoundingClientRect();
    engine.point(((e.clientX - r.left) / r.width) * 2 - 1, ((e.clientY - r.top) / r.height) * 2 - 1);
  });
  $('picture').addEventListener('pointerleave', () => engine.point(0, 0));
  addEventListener('pointermove', wake, { passive: true });
  addEventListener('pointerdown', wake, { passive: true });
  wake();
  addEventListener('keydown', (e) => {
    if (e.altKey || e.ctrlKey || e.metaKey) return; // the browser's own shortcuts (Alt+Left is its Back)
    if (overview.open) {
      // The overview has the keys while it is up: Escape or o closes it, the arrows move among its cards, and Enter
      // or Space is the focused card's click.
      if (e.key === 'Escape' || e.key === 'o') { e.preventDefault(); overview.close(); }
      else if (e.key.startsWith('Arrow')) { e.preventDefault(); overview.move(e.key); }
      return;
    }
    if (e.target.closest?.('button') && (e.key === ' ' || e.key === 'Enter')) return; // the button does it
    const act = {
      ArrowRight: () => engine.advance(), ' ': () => engine.advance(), PageDown: () => engine.advance(), Enter: () => engine.advance(),
      ArrowLeft: () => engine.back(), PageUp: () => engine.back(), Backspace: () => engine.back(),
      Home: () => engine.first(), End: () => engine.last(),
      a: () => engine.setAuto(!engine.info().auto),
      n: () => root.classList.toggle('notes-on'),
      f: () => (document.fullscreenElement ? document.exitFullscreen() : root.requestFullscreen?.())?.catch?.(() => {}),
      o: () => overview.toggle(),
      t: nextTheme,
    }[e.key];
    if (!act) return;
    e.preventDefault();
    act();
  });
  addEventListener('hashchange', () => { const h = fromHash(); if (h) engine.jump(story[h.i].id, h.beat); });
  reduced.addEventListener('change', () => { engine.setStill(reduced.matches); wake(); });
  addEventListener('resize', measurePicture);
}

async function boot() {
  const reason = webglWhyNot();
  if (reason) return articleOnly(reason);
  root.dataset.form = form;
  root.classList.add('film-on');
  let faces = loadFaces();
  const [{ createFilm }, { film: definition }] = await Promise.all([import('./film.js'), import('./story.js')]);
  film.form = form = formOf(definition);
  root.dataset.form = form;
  const theme = themeNamed ? root.dataset.storyTheme : form === 'film' ? 'ink' : 'paper';
  if (theme !== root.dataset.storyTheme) { root.dataset.storyTheme = theme; faces = loadFaces(); }
  // The faces are given three seconds: a host that is slow with them delays the type, not the story.
  await Promise.race([faces, new Promise((r) => setTimeout(r, 3000))]);
  measurePicture();
  const caption = createCaption(story, { form });
  presenter();
  const loading = $('loading');
  engine = await createFilm({
    film: definition, story, caption, canvas: $('stage'), picture: $('picture'),
    treatment: params.get('print'), theme: root.dataset.storyTheme, pace, still: reduced.matches, start: fromHash() ?? { i: 0, beat: 0 },
    loading: (k, n) => { loading.textContent = `Loading ${k} of ${n}`; },
    onChange: follow,
  });
  // An engine with themes of its own is told which one the page is in, unless it has read that from the page; and a
  // theme asked for with ?theme= that only the engine's list has (one an author added) is taken up now.
  if (engine.setTheme && engine.info().theme !== root.dataset.storyTheme && themes().includes(root.dataset.storyTheme)) engine.setTheme(root.dataset.storyTheme);
  if (params.get('theme') !== root.dataset.storyTheme && themes().includes(params.get('theme'))) setTheme(params.get('theme'));
  film.title = definition.title;
  film.treatment = engine.treatment;
  controls();
  await engine.nextDraw();
  loading.hidden = true;
  film.mode = 'webgl';
  resolveReady();
}

boot().catch((e) => {
  console.error('the story failed to start:', e);
  engine = null;
  articleOnly('the story failed to start');
});
