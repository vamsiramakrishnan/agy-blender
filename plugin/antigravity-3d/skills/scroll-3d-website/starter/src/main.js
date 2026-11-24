// main.js: boot, scroll -> story time -> scene state, reduced motion, no WebGL, and window.__site.
// Everything the scene shows is shots.evaluate(T) for the story time T of the current scroll position (chapters.js).
// Nothing is eased per frame here: the picture at a scroll position is the same however the visitor got there.
// Smooth scrolling (Lenis) and scroll-linked page effects (GSAP) are layers on top: see the skill's libraries.md.
import { measure, timeAt, scrollFor } from './chapters.js';
import { evaluate, problems, restingT } from './shots.js';

const root = document.documentElement;
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
const phone = matchMedia('(max-width: 899px)');
// Where the reader's eye is, as a share of the window's height: just below where the panels stick (style.css: top
// 18vh on a desktop, 56vh on a phone).
const readLine = () => (phone.matches ? 0.6 : 0.25);

// The hook a screenshot tool or check uses: wait for `ready`, then ask for any moment by story time.
// TODO(integrate): check_site scrolls like a visitor; confirm whether it also reads this hook when the branches meet.
let resolveReady;
const site = {
  ready: new Promise((r) => { resolveReady = r; }),
  mode: 'starting',          // 'webgl', or 'no-webgl' with the reason in `why`
  chapters: [],
  setProgress() {},          // setProgress(T): scroll to story time T (0 .. chapters.length) and draw it now
  progress: () => 0,         // the story time drawn last
  info: () => ({}),
};
window.__site = site;

function webglWhyNot() {
  try {
    const c = document.createElement('canvas');
    return c.getContext('webgl2') ? null : 'this browser has no WebGL 2';
  } catch (e) {
    return 'WebGL failed: ' + (e && e.message ? e.message : e);
  }
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
  resolveReady();
}

async function boot() {
  const why = webglWhyNot();
  if (why) return articleOnly(why);
  root.classList.add('webgl-on'); // before measuring: the chapters' heights depend on this class
  let boxes = measure();
  const ids = boxes.map((b) => b.id);
  for (const p of problems(ids)) console.error(p);

  let shown = null, dirty = true, needMeasure = false, current = -1, stage;
  try {
    const { createStage } = await import('./stage.js');
    stage = createStage(document.getElementById('stage'), { phone: phone.matches, onChange: () => { dirty = true; } });
  } catch (e) {
    return articleOnly('the scene failed to start: ' + (e && e.message ? e.message : e));
  }

  const wanted = () => {
    const T = timeAt(boxes, scrollY);
    return reduced.matches ? restingT(T, ids.length) : T;
  };
  // The chapter whose panel the reader is on gets .is-current (style.css shows its panel at full strength). It is read
  // at the reading line of the window, where the sticky panels rest, not at its top: a panel lights up as it arrives.
  function mark() {
    const T = timeAt(boxes, scrollY + innerHeight * readLine());
    const i = Math.min(ids.length - 1, Math.floor(T));
    if (i === current) return;
    if (current >= 0) boxes[current].el.classList.remove('is-current');
    boxes[i].el.classList.add('is-current');
    current = i;
  }
  function draw(T) {
    shown = T;
    dirty = false;
    mark();
    stage.render(evaluate(T, ids));
  }
  const resize = () => {
    boxes = measure();
    stage.resize(innerWidth, innerHeight, devicePixelRatio);
    dirty = true;
  };

  addEventListener('resize', () => { needMeasure = true; });
  reduced.addEventListener('change', () => { dirty = true; });
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => { needMeasure = true; });

  // Draw only when the story time changed or something loaded: a page that redraws while nothing moves burns a
  // phone's battery and is what check_site reports as drawing while idle.
  function frame() {
    requestAnimationFrame(frame);
    if (needMeasure) { needMeasure = false; resize(); }
    const T = wanted();
    if (T !== shown || dirty) draw(T);
  }

  Object.assign(site, {
    mode: 'webgl',
    chapters: ids,
    setProgress(T) {
      if (needMeasure) { needMeasure = false; resize(); }
      scrollTo(0, scrollFor(boxes, T));
      draw(wanted());
    },
    progress: () => shown,
    evaluate: (T) => evaluate(T, ids),
    info: () => ({ T: shown, chapter: ids[current], ...stage.info() }),
  });

  resize();
  draw(wanted());
  requestAnimationFrame(frame);
  // Ready once every installed prop has loaded or failed, so a screenshot never shows a stand-in by accident.
  stage.ready.then(() => { draw(wanted()); resolveReady(); });
}

boot().catch((e) => {
  console.error('the page failed to start:', e);
  articleOnly('the page failed to start');
});
