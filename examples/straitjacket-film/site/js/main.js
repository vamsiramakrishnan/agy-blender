// The page of the Straitjacket scroll film: boot, scroll, film time, panels, HUD, rail, theme, quality, window.__film.
// Everything the scene shows is a function of film time T, which is a function of scroll position (see chapters.js).
import { CHAPTERS, ARRIVAL, measure, timeAt, scrollFor, restingT } from './chapters.js';

const root = document.documentElement;
const TOKENS = ['bg', 'surface', 'surface2', 'ink', 'muted', 'faint', 'grid', 'blue', 'blue2', 'amber', 'amber2', 'red', 'red2', 'green'];
const readPalette = () => {
  const cs = getComputedStyle(root);
  return Object.fromEntries(TOKENS.map((t) => [t, cs.getPropertyValue('--' + t).trim()]));
};
const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
const finePointer = matchMedia('(hover: hover) and (pointer: fine)').matches;
// Under browser automation the scene is software rendered and slow: no ambient drift, no automatic quality step-down,
// so that screenshots show the quality that was asked for and are repeatable.
const automated = navigator.webdriver === true;

let resolveReady;
const ready = new Promise((r) => { resolveReady = r; });
const api = { ready, setT() {}, getT: () => 0, info: () => ({ readout: null }), evaluate: null };
window.__film = api;

/* ---- the copy button (works with the film on or off) ------------------------------------------------------------ */
function setupCopy() {
  document.querySelectorAll('[data-copy]').forEach((btn) => {
    const target = document.getElementById(btn.dataset.copy);
    if (!target) return;
    const label = btn.textContent;
    let timer = 0;
    btn.setAttribute('aria-live', 'polite');
    const done = () => {
      btn.textContent = 'Copied';
      clearTimeout(timer);
      timer = setTimeout(() => { btn.textContent = label; }, 2000);
    };
    const select = () => {
      try {
        const range = document.createRange();
        range.selectNodeContents(target);
        const sel = getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
      } catch (e) { /* nothing more to do */ }
    };
    btn.addEventListener('click', () => {
      const text = target.textContent.replace(/\n$/, '');
      try {
        navigator.clipboard.writeText(text).then(done, select);
      } catch (e) { select(); }
    });
  });
}

/* ---- boot ------------------------------------------------------------------------------------------------------- */
function hasWebGL() {
  try {
    const c = document.createElement('canvas');
    return !!(c.getContext('webgl2') || c.getContext('webgl'));
  } catch (e) { return false; }
}

async function boot() {
  setupCopy();
  const canvas = document.getElementById('stage');
  const labels = document.getElementById('labels');
  let film, tracks;
  try {
    if (!hasWebGL()) throw new Error('WebGL is not available');
    const [data, bin] = await Promise.all([
      fetch('data/film.json').then((r) => { if (!r.ok) throw new Error('film.json ' + r.status); return r.json(); }),
      fetch('data/lines.bin').then((r) => { if (!r.ok) throw new Error('lines.bin ' + r.status); return r.arrayBuffer(); }),
    ]);
    const [{ createFilm }, tr] = await Promise.all([import('./scene/index.js'), import('./scene/tracks.js')]);
    tracks = tr;
    film = await createFilm({
      canvas, labels, data, lines: new Uint8Array(bin), palette: readPalette(),
      quality: finePointer ? 'hi' : 'mid', reducedMotion: reduced,
    });
  } catch (e) {
    console.warn('film off:', e && e.message ? e.message : e);
    if (film && film.dispose) { try { film.dispose(); } catch (e2) { /* ignore */ } }
    root.classList.add('film-off');
    let boxes = measure();
    addEventListener('resize', () => { boxes = measure(); });
    api.setT = (T) => { boxes = measure(); scrollTo(0, scrollFor(boxes, T)); };
    api.getT = () => timeAt(boxes, scrollY);
    api.evaluate = tracks ? tracks.evaluate : null;
    resolveReady();
    return;
  }

  /* smooth scrolling: fine pointers only, never under reduced motion */
  let lenis = null;
  if (finePointer && !reduced) {
    try {
      const { default: Lenis } = await import('../vendor/lenis.mjs');
      lenis = new Lenis({ lerp: 0.09, smoothWheel: true });
    } catch (e) { console.warn('smooth scrolling off:', e && e.message ? e.message : e); }
  }

  root.classList.add('film-on');
  const hudBox = document.getElementById('hud');
  const hudV = document.getElementById('hud-value');
  const hudN = document.getElementById('hud-note');
  const railLinks = [...document.querySelectorAll('#rail a')];
  const layout = () => (innerWidth >= 900 ? 'wide' : 'narrow');

  let boxes = measure();
  let shownT = null;       // the film time the scene is drawn at
  let dirty = true;
  let needMeasure = false;
  let sizeDirty = true;
  let swapTimer = 0;
  let current = -1;
  let docMode = false;
  const tVals = new Array(CHAPTERS.length).fill(NaN);

  const markScrollers = () => {
    document.querySelectorAll('.panel, .specimen--digest').forEach((el) => {
      if (lenis && el.scrollHeight > el.clientHeight + 1) el.setAttribute('data-lenis-prevent', '');
      else el.removeAttribute('data-lenis-prevent');
    });
  };

  /* panel state: --t (0..1 through each chapter), .is-current, the rail's current tick */
  function chapterState(raw) {
    const i = Math.floor(raw);
    for (let k = 0; k < boxes.length; k++) {
      const v = k < i ? 1 : k > i ? 0 : raw - i;
      if (v !== tVals[k]) { tVals[k] = v; boxes[k].el.style.setProperty('--t', v.toFixed(4)); }
    }
    if (i !== current) {
      if (current >= 0) {
        boxes[current].el.classList.remove('is-current');
        railLinks[current] && railLinks[current].removeAttribute('aria-current');
      }
      current = i;
      boxes[i].el.classList.add('is-current');
      const needle = boxes[i].el.querySelector('.specimen--digest .dg--needle');
      if (needle) { const sp = needle.closest('.specimen'); sp.scrollTop = Math.max(0, needle.offsetTop - sp.clientHeight / 2); }
      railLinks[i] && railLinks[i].setAttribute('aria-current', 'true');
      const doc = boxes[i].el.classList.contains('ch--doc');
      if (doc !== docMode) { docMode = doc; root.classList.toggle('is-doc', doc); }
    }
  }

  let hudKey = '';
  function showHud(T) {
    const r = tracks.evaluate(T).readout;
    if (!r) return;
    const key = r.value + '|' + r.note + '|' + r.tone;
    if (key === hudKey) return;
    hudKey = key;
    hudV.textContent = r.value;
    hudN.textContent = r.note;
    hudBox.dataset.tone = r.tone;
  }

  /* read the scroll position, update the DOM, decide which film time the scene should show */
  function sync(instant) {
    const raw = timeAt(boxes, scrollY);
    chapterState(raw);
    const want = reduced ? restingT(raw) : raw;
    if (want === shownT) return;
    if (reduced && shownT !== null && !instant) {
      if (!swapTimer) {
        root.classList.add('is-swapping');
        swapTimer = setTimeout(() => {
          swapTimer = 0;
          shownT = restingT(timeAt(boxes, scrollY));
          showHud(shownT);
          dirty = true;
          root.classList.remove('is-swapping');
        }, 150);
      }
      return;
    }
    if (swapTimer) { clearTimeout(swapTimer); swapTimer = 0; root.classList.remove('is-swapping'); }
    shownT = want;
    showHud(want);
    dirty = true;
  }

  /* quality: start high on fine pointers, step down at most twice when frames are slow, never up */
  let quality = finePointer ? 'hi' : 'mid';
  let steps = 0;
  let samples = [];
  function sampleFrame() {
    if (automated || steps >= 2 || quality === 'lo') return;
    samples.push(film.info().frameMs || 0);
    if (samples.length < 40) return;
    const mean = samples.reduce((a, b) => a + b, 0) / samples.length;
    samples = [];
    if (mean > 24) {
      quality = quality === 'hi' ? 'mid' : 'lo';
      steps++;
      film.setQuality(quality);
      dirty = true;
    }
  }

  const doResize = () => {
    film.resize(innerWidth, innerHeight, devicePixelRatio || 1, layout());
    sizeDirty = false;
    dirty = true;
  };

  /* scrolling */
  const jump = (y) => {
    if (lenis) lenis.scrollTo(y, { immediate: true, force: true });
    scrollTo(0, y);
  };
  const landing = (i) => {
    const b = boxes[i];
    return i === 0 || b.el.classList.contains('ch--doc') ? b.top : scrollFor(boxes, i + ARRIVAL);
  };
  function goTo(i) {
    const y = landing(i);
    if (lenis) lenis.scrollTo(y, { duration: 1.6, easing: (t) => 1 - Math.pow(1 - t, 4) });
    else scrollTo({ top: y, behavior: reduced ? 'auto' : 'smooth' });
  }
  const chapterOf = (link) => {
    const id = link.dataset.ch || (link.getAttribute('href') || '').slice(1);
    return id === 'top' ? 0 : CHAPTERS.indexOf(id);
  };
  document.querySelectorAll('#rail a, .bar__brand').forEach((a) => {
    a.addEventListener('click', (e) => {
      const i = chapterOf(a);
      if (i < 0) return;
      e.preventDefault();
      goTo(i);
    });
  });
  /* keyboard: focusing something inside a panel that is not showing brings its chapter to it */
  document.addEventListener('focusin', (e) => {
    const panel = e.target.closest && e.target.closest('.panel');
    if (!panel) return;
    const i = boxes.findIndex((b) => b.el.contains(panel));
    if (i < 0 || boxes[i].el.classList.contains('ch--doc')) return;
    const t = tVals[i];
    if (i !== current || (t < 0.1 && i > 0) || t > 0.9) { jump(scrollFor(boxes, i + 0.5)); sync(true); }
  });

  /* theme */
  const onTheme = () => { film.setPalette(readPalette()); dirty = true; };
  matchMedia('(prefers-color-scheme: light)').addEventListener('change', onTheme);
  new MutationObserver(onTheme).observe(root, { attributes: true, attributeFilter: ['data-theme'] });

  /* size and layout */
  addEventListener('resize', () => { sizeDirty = true; needMeasure = true; });
  if ('ResizeObserver' in window) new ResizeObserver(() => { needMeasure = true; }).observe(document.getElementById('film'));
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => { needMeasure = true; });

  /* the loop: renders only when something changed, plus a gentle ambient rate when motion is allowed */
  const ambient = !reduced && !automated;
  let lastAmbient = -1e9;
  function frame(ms) {
    requestAnimationFrame(frame);
    if (lenis) lenis.raf(ms);
    if (needMeasure) {
      needMeasure = false;
      boxes = measure();
      tVals.fill(NaN);
      markScrollers();
      shownT = null;
    }
    if (sizeDirty) doResize();
    sync(false);
    if (!dirty && ambient && ms - lastAmbient > 33) dirty = true;
    if (!dirty) return;
    dirty = false;
    lastAmbient = ms;
    film.render(shownT, ms / 1000);
    sampleFrame();
  }

  Object.assign(api, {
    setT(T) {
      boxes = measure();
      tVals.fill(NaN);
      jump(scrollFor(boxes, T));
      sync(true);
      if (sizeDirty) doResize();
      film.render(shownT, 0);
      dirty = false;
    },
    getT: () => (shownT === null ? 0 : shownT),
    info: () => ({ ...film.info(), readout: tracks.evaluate(shownT === null ? 0 : shownT).readout }),
    evaluate: tracks.evaluate,
  });

  /* first frame (honouring a #chapter in the address) */
  doResize();
  markScrollers();
  const hashId = decodeURIComponent(location.hash.slice(1));
  const hashIdx = hashId ? (hashId === 'top' ? 0 : CHAPTERS.indexOf(hashId)) : -1;
  if (hashIdx > 0) jump(landing(hashIdx));
  sync(true);
  film.render(shownT, 0);
  dirty = false;
  requestAnimationFrame(frame);
  resolveReady();
}

boot().catch((e) => {
  console.warn('film off:', e && e.message ? e.message : e);
  root.classList.remove('film-on');
  root.classList.add('film-off');
  resolveReady();
});
