// main.js: boots local fonts, Lenis smooth scrolling, GSAP ScrollTrigger progress ruler,
// and drives stage.js strictly as a pure function of story time T on demand.
import '@fontsource-variable/space-grotesk';
import '@fontsource-variable/inter';
import '@fontsource/jetbrains-mono/400.css';
import '@fontsource/jetbrains-mono/600.css';

import Lenis from 'lenis';
import gsap from 'gsap';
import { ScrollTrigger } from 'gsap/ScrollTrigger';

import { measure, timeAt, scrollFor } from './chapters.js';
import { evaluate, problems, restingT, timeline, REST } from './shots.js';

gsap.registerPlugin(ScrollTrigger);

const root = document.documentElement;
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
const phone = matchMedia('(max-width: 899px) and (orientation: portrait)');
const touchOnly = matchMedia('(hover: none), (pointer: coarse)');
const RESTORE_WAIT_MS = 3000;

let resolveReady;
const site = {
  ready: new Promise((r) => { resolveReady = r; }),
  mode: 'starting',
  chapters: () => [],
  setProgress() {},
  progress: () => 0,
  jumpTo() {},
  at: () => 0,
  scrollWith: (y) => scrollTo({ top: y, behavior: reduced.matches ? 'instant' : 'smooth' }),
  info: () => ({}),
};
window.__site = site;

function webglWhyNot() {
  try {
    const gl = document.createElement('canvas').getContext('webgl2');
    if (!gl) return 'this browser has no WebGL 2';
    gl.getExtension('WEBGL_lose_context')?.loseContext();
    return null;
  } catch (e) {
    return 'WebGL failed: ' + (e && e.message ? e.message : e);
  }
}

function linkChapters(ids) {
  document.addEventListener('click', (e) => {
    const a = e.target.closest && e.target.closest('a[href^="#"]');
    const id = a && decodeURIComponent(a.hash.slice(1));
    if (!id || !ids.includes(id)) return;
    e.preventDefault();
    history.replaceState(null, '', '#' + id);
    site.jumpTo(id);
  });
}

function initScrollUI() {
  let lenis = null;
  const tickLenis = (seconds) => { if (lenis) lenis.raf(seconds * 1000); };

  const syncLenis = () => {
    if (reduced.matches) {
      if (lenis) {
        gsap.ticker.remove(tickLenis);
        lenis.destroy();
        lenis = null;
      }
      site.scrollWith = (y) => scrollTo({ top: y, behavior: 'instant' });
      return;
    }
    if (!lenis) {
      lenis = new Lenis({ allowNestedScroll: true });
      lenis.on('scroll', ScrollTrigger.update);
      gsap.ticker.add(tickLenis);
      gsap.ticker.lagSmoothing(0);
      site.scrollWith = (y) => lenis.scrollTo(y);
    }
  };
  syncLenis();
  reduced.addEventListener('change', syncLenis);

  // Progress bar synced with ScrollTrigger (HTML-only effect; 3D stays pure function of scrollY)
  ScrollTrigger.create({
    trigger: '#story',
    start: 'top top',
    end: 'bottom bottom',
    onUpdate: (self) => {
      root.style.setProperty('--progress', self.progress.toFixed(4));
    },
  });
}

function articleOnly(why) {
  root.classList.remove('webgl-on');
  root.classList.add('webgl-off');
  site.mode = 'no-webgl';
  site.why = why;
  let boxes = measure();
  site.chapters = () => boxes.map((b, i) => ({ id: b.id, rest: i + REST }));
  site.setProgress = (T) => { boxes = measure(); scrollTo(0, scrollFor(boxes, T)); };
  site.progress = () => timeAt(boxes, scrollY);
  site.jumpTo = (id) => document.getElementById(id)?.scrollIntoView();
  site.info = () => ({});
  resolveReady();
}

async function boot() {
  const why = webglWhyNot();
  if (why) return articleOnly(why);
  root.classList.add('webgl-on');
  let boxes = measure(), measures = 1;
  const ids = boxes.map((b) => b.id);
  for (const p of problems(ids)) console.error(p);
  const at = timeline(ids);

  const navLinks = new Map([...document.querySelectorAll('.chapter-nav a[data-nav]')].map((a) => [a.dataset.nav, a]));

  let shown = null, dirty = true, needMeasure = false, current = -1, stage, stopped = false, lostTimer = 0;
  const onLost = (lost) => {
    clearTimeout(lostTimer);
    if (lost) lostTimer = setTimeout(() => { stopped = true; articleOnly('the WebGL context was lost'); }, RESTORE_WAIT_MS);
  };
  try {
    const { createStage } = await import('./stage.js');
    stage = createStage(document.getElementById('stage'), { phone: phone.matches, onChange: () => { dirty = true; }, onLost });
  } catch (e) {
    return articleOnly('the scene failed to start: ' + (e && e.message ? e.message : e));
  }

  initScrollUI();

  const wanted = () => {
    const T = timeAt(boxes, scrollY);
    return reduced.matches ? restingT(T, ids.length) : T;
  };

  function mark(T) {
    const i = Math.min(ids.length - 1, Math.floor(T));
    if (i === current) return;
    if (current >= 0) {
      boxes[current].el.classList.remove('is-current');
      navLinks.get(ids[current])?.classList.remove('is-active');
    }
    boxes[i].el.classList.add('is-current');
    navLinks.get(ids[i])?.classList.add('is-active');
    current = i;
  }

  function draw(T) {
    shown = T;
    dirty = false;
    mark(T);
    const pct = ids.length > 0 ? Math.max(0, Math.min(1, T / ids.length)) : 0;
    root.style.setProperty('--progress', pct.toFixed(4));
    stage.render(evaluate(T, ids));
  }

  const remeasure = () => { boxes = measure(); measures++; dirty = true; };
  const jumpTo = (id) => site.scrollWith(scrollFor(boxes, at(id, REST)));

  let baseW = innerWidth, baseH = innerHeight;
  addEventListener('resize', () => {
    if (touchOnly.matches && innerWidth === baseW && Math.abs(innerHeight - baseH) <= 0.25 * innerHeight) return;
    baseW = innerWidth; baseH = innerHeight;
    needMeasure = true;
  });
  reduced.addEventListener('change', () => { dirty = true; });
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => { needMeasure = true; });

  function frame() {
    if (stopped) return;
    requestAnimationFrame(frame);
    if (needMeasure) { needMeasure = false; remeasure(); }
    const T = wanted();
    if (T !== shown || dirty) draw(T);
  }

  Object.assign(site, {
    mode: 'webgl',
    chapters: () => ids.map((id) => ({ id, rest: at(id, REST) })),
    setProgress(T) {
      if (needMeasure) { needMeasure = false; remeasure(); }
      scrollTo(0, scrollFor(boxes, T));
      draw(wanted());
    },
    progress: () => shown,
    jumpTo,
    at,
    label: stage.label,
    evaluate: (T) => evaluate(T, ids),
    info: () => ({ T: shown, chapter: ids[current], measures, ...stage.info() }),
  });
  linkChapters(ids);

  const hash = decodeURIComponent(location.hash.slice(1));
  if (ids.includes(hash)) scrollTo(0, scrollFor(boxes, at(hash, REST)));
  draw(wanted());
  requestAnimationFrame(frame);
  stage.ready.then(() => { draw(wanted()); resolveReady(); });
}

boot().catch((e) => {
  console.error('the page failed to start:', e);
  articleOnly('the page failed to start');
});
