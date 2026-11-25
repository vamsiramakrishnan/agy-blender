// main.js: boot, scroll -> story time -> scene state, Lenis smooth scroll, GSAP ScrollTrigger, reduced motion, no WebGL, and window.__site.
import '@fontsource-variable/space-grotesk';
import '@fontsource-variable/inter';
import '@fontsource/jetbrains-mono/400.css';
import '@fontsource/jetbrains-mono/600.css';

import gsap from 'gsap';
import { ScrollTrigger } from 'gsap/ScrollTrigger';
import Lenis from 'lenis';
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

function setupScrollPolish(boxes) {
  // Lenis smooth wheel scrolling on desktop; native touch preserved; auto-disabled under prefers-reduced-motion.
  const lenis = new Lenis({ allowNestedScroll: true });
  lenis.on('scroll', ScrollTrigger.update);
  gsap.ticker.add((seconds) => lenis.raf(seconds * 1000));
  gsap.ticker.lagSmoothing(0);
  site.scrollWith = (y) => lenis.scrollTo(y);

  // Subtle scrubbed vertical glide on panel elements as each chapter activates (text never modified).
  if (!reduced.matches) {
    boxes.forEach((b, idx) => {
      if (idx === 0) return;
      const kids = b.el.querySelectorAll('.panel > *');
      if (!kids.length) return;
      gsap.from(kids, {
        y: 12,
        ease: 'none',
        stagger: 0.08,
        scrollTrigger: {
          trigger: b.el,
          start: 'top 35%',
          end: 'top -5%',
          scrub: true,
        },
      });
    });
  }
  return lenis;
}

async function boot() {
  const why = webglWhyNot();
  if (why) return articleOnly(why);
  root.classList.add('webgl-on');
  let boxes = measure(), measures = 1;
  const ids = boxes.map((b) => b.id);
  for (const p of problems(ids)) console.error(p);
  const at = timeline(ids);

  const navLinks = new Map(
    [...document.querySelectorAll('[data-nav]')].map((el) => [el.dataset.nav, el]),
  );
  const progressFill = document.getElementById('progress-fill');

  let shown = null, dirty = true, needMeasure = false, current = -1, stage, stopped = false, lostTimer = 0;
  const onLost = (lost) => {
    clearTimeout(lostTimer);
    if (lost) lostTimer = setTimeout(() => { stopped = true; articleOnly('the WebGL context was lost'); }, RESTORE_WAIT_MS);
  };
  try {
    const [{ createStage }, { installLayers }] = await Promise.all([
      import('./stage.js'),
      import('./layers.js'),
    ]);
    stage = createStage(document.getElementById('stage'), { phone: phone.matches, onChange: () => { dirty = true; }, onLost });
    installLayers(stage, at, stage.floorUniforms);
  } catch (e) {
    return articleOnly('the scene failed to start: ' + (e && e.message ? e.message : e));
  }

  const wanted = () => {
    const T = timeAt(boxes, scrollY);
    return reduced.matches ? restingT(T, ids.length) : T;
  };

  function mark(T) {
    if (progressFill) {
      const pct = Math.max(0, Math.min(100, (T / Math.max(1, ids.length)) * 100));
      progressFill.style.width = `${pct.toFixed(2)}%`;
    }
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
    layer: stage.layer,
    evaluate: (T) => evaluate(T, ids),
    info: () => ({ T: shown, chapter: ids[current], measures, ...stage.info() }),
  });
  linkChapters(ids);
  setupScrollPolish(boxes);

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
