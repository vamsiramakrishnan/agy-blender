// main.js: boot, scroll -> story time -> scene state, reduced motion, no WebGL, and window.__site.
// Everything the scene shows is shots.evaluate(T) for the story time T of the current scroll position (chapters.js).
// Nothing is eased per frame here: the picture at a scroll position is the same however the visitor got there.
// Smooth scrolling (Lenis) and scroll-linked page effects (GSAP) are layers on top: see the skill's libraries.md.
import { measure, timeAt, scrollFor } from './chapters.js';
import { evaluate, problems, restingT, timeline, REST } from './shots.js';

const root = document.documentElement;
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
// The phone layout (style.css uses the same query): a portrait window narrower than 900 px.
const phone = matchMedia('(max-width: 899px) and (orientation: portrait)');
// The same test GSAP's ScrollTrigger makes for a touch-only device (gsap 3.15.1, Observer.isTouch === 1).
const touchOnly = matchMedia('(hover: none), (pointer: coarse)');
// A context that is lost and not given back within this time leaves the reader the article instead of a blank canvas.
const RESTORE_WAIT_MS = 3000;

// A hook for quick looks from the browser's console or a script of your own: wait for `ready`, then ask for any
// moment by story time. check_site scrolls the page the way a visitor does and reads info().onScreen at each step.
let resolveReady;
const site = {
  ready: new Promise((r) => { resolveReady = r; }),
  mode: 'starting',          // 'webgl', or 'no-webgl' with the reason in `why`
  chapters: [],
  setProgress() {},          // setProgress(T): scroll to story time T (0 .. chapters.length) and draw it now
  progress: () => 0,         // the story time drawn last
  jumpTo() {},               // jumpTo(id): go to where chapter `id` rests; the chapter links use it
  at: () => 0,               // at(id, local): story time of a moment in chapter `id` (shots.js, timeline)
  // How the page moves to a scroll position for jumpTo. A smooth-scroll layer replaces it (libraries.md, Lenis).
  scrollWith: (y) => scrollTo({ top: y, behavior: reduced.matches ? 'instant' : 'smooth' }),
  info: () => ({}),
};
window.__site = site;

function webglWhyNot() {
  try {
    const gl = document.createElement('canvas').getContext('webgl2');
    if (!gl) return 'this browser has no WebGL 2';
    // Give the test context back at once: a browser keeps only a few live contexts per page and drops the oldest past
    // its limit, and run 2 kept this one, and detect-gpu's, alive beside the renderer's for the life of the page.
    gl.getExtension('WEBGL_lose_context')?.loseContext();
    return null;
  } catch (e) {
    return 'WebGL failed: ' + (e && e.message ? e.message : e);
  }
}

// Chapter links (<a href="#how">) land where that chapter rests, not at its top: at its top the camera has not moved
// yet and shows the previous chapter's picture, which is where run 2's chapter links landed.
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

// Without WebGL the page is an article: the HTML already says everything, the canvas is hidden by style.css.
function articleOnly(why) {
  root.classList.remove('webgl-on');
  root.classList.add('webgl-off');
  site.mode = 'no-webgl';
  site.why = why;
  let boxes = measure();
  site.chapters = boxes.map((b) => b.id);
  site.setProgress = (T) => { boxes = measure(); scrollTo(0, scrollFor(boxes, T)); };
  site.progress = () => timeAt(boxes, scrollY);
  site.jumpTo = (id) => document.getElementById(id)?.scrollIntoView(); // the sections' ids, as a link without script
  site.info = () => ({});
  resolveReady();
}

async function boot() {
  const why = webglWhyNot();
  if (why) return articleOnly(why);
  root.classList.add('webgl-on'); // before measuring: the chapters' heights depend on this class
  let boxes = measure(), measures = 1;
  const ids = boxes.map((b) => b.id);
  for (const p of problems(ids)) console.error(p);
  const at = timeline(ids);

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

  const wanted = () => {
    const T = timeAt(boxes, scrollY);
    return reduced.matches ? restingT(T, ids.length) : T;
  };
  // The chapter of the story time drawn gets .is-current, and style.css shows its panel: the text changes as the
  // camera starts its move to the chapter's shot, so text and picture always belong to the same chapter.
  function mark(T) {
    const i = Math.min(ids.length - 1, Math.floor(T));
    if (i === current) return;
    if (current >= 0) boxes[current].el.classList.remove('is-current');
    boxes[i].el.classList.add('is-current');
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

  // The renderer follows the canvas's own box (stage.js). The chapters are measured again when the layout can have
  // changed: a new width (text wraps differently), fonts, or a large change of height. A touch device's address bar
  // changes the height by a few per cent as it slides; the chapters are in vh, which ignores it, so re-measuring then
  // only made the page jump (run 2). ScrollTrigger ignores the same resizes for the same reason.
  let baseW = innerWidth, baseH = innerHeight;
  addEventListener('resize', () => {
    if (touchOnly.matches && innerWidth === baseW && Math.abs(innerHeight - baseH) <= 0.25 * innerHeight) return;
    baseW = innerWidth; baseH = innerHeight;
    needMeasure = true;
  });
  reduced.addEventListener('change', () => { dirty = true; });
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => { needMeasure = true; });

  // Draw only when the story time changed or something loaded: a page that redraws while nothing moves burns a
  // phone's battery and is what check_site reports as drawing while idle.
  function frame() {
    if (stopped) return;
    requestAnimationFrame(frame);
    if (needMeasure) { needMeasure = false; remeasure(); }
    const T = wanted();
    if (T !== shown || dirty) draw(T);
  }

  Object.assign(site, {
    mode: 'webgl',
    chapters: ids,
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
  // Ready once every installed prop has loaded or failed, so a screenshot never shows a stand-in by accident.
  stage.ready.then(() => { draw(wanted()); resolveReady(); });
}

boot().catch((e) => {
  console.error('the page failed to start:', e);
  articleOnly('the page failed to start');
});
