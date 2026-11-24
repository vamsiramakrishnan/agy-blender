// main.js: boots bundled fonts, detect-gpu quality tier, Lenis smooth scroll, GSAP ScrollTrigger UI sync,
// deterministic scroll -> story time -> 3D stage rendering, reduced-motion, and no-WebGL article fallback.
import '@fontsource-variable/space-grotesk';
import '@fontsource-variable/inter';
import '@fontsource/jetbrains-mono/latin-400.css';
import '@fontsource/jetbrains-mono/latin-700.css';

import Lenis from 'lenis';
import gsap from 'gsap';
import { ScrollTrigger } from 'gsap/ScrollTrigger';
import { getGPUTier } from 'detect-gpu';
import { measure, timeAt, scrollFor } from './chapters.js';
import { evaluate, problems, restingT } from './shots.js';

const root = document.documentElement;
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
const phone = matchMedia('(max-width: 899px)');
const readLine = () => (phone.matches ? 0.56 : 0.58);

let resolveReady;
const site = {
  ready: new Promise((r) => {
    resolveReady = r;
  }),
  mode: 'starting',
  chapters: [],
  setProgress() {},
  progress: () => 0,
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

function articleOnly(why) {
  root.classList.remove('webgl-on');
  root.classList.add('webgl-off');
  site.mode = 'no-webgl';
  site.why = why;
  let boxes = measure();
  site.chapters = boxes.map((b) => b.id);
  site.setProgress = (T) => {
    boxes = measure();
    scrollTo(0, scrollFor(boxes, T));
  };
  site.progress = () => timeAt(boxes, scrollY);
  resolveReady();
}

function initScrollLayers() {
  gsap.registerPlugin(ScrollTrigger);

  let lenis = null;
  if (!reduced.matches) {
    // Lenis smooths wheel input on desktop; touch remains native.
    lenis = new Lenis({ anchors: true });
    for (const panel of document.querySelectorAll('.panel')) {
      panel.setAttribute('data-lenis-prevent', '');
    }
    lenis.on('scroll', ScrollTrigger.update);
    gsap.ticker.add((seconds) => lenis.raf(seconds * 1000));
    gsap.ticker.lagSmoothing(0);
  }

  // Anchor clicks jump cleanly (smoothly via Lenis, or instantly under reduced motion)
  for (const link of document.querySelectorAll('[data-jump]')) {
    link.addEventListener('click', (e) => {
      const id = link.getAttribute('data-jump');
      const target = id ? document.getElementById(id) : null;
      if (!target) return;
      e.preventDefault();
      if (lenis) lenis.scrollTo(target, { offset: 0 });
      else target.scrollIntoView({ behavior: 'auto' });
    });
  }

  // Global scroll progress bar via GSAP ScrollTrigger
  ScrollTrigger.create({
    trigger: '#story',
    start: 'top top',
    end: 'bottom bottom',
    onUpdate: (self) => {
      root.style.setProperty('--progress', self.progress.toFixed(4));
    },
  });

  // Subtle scrubbed vertical lift on each chapter's panel (skipped under reduced motion)
  if (!reduced.matches) {
    for (const chapter of document.querySelectorAll('[data-chapter]')) {
      const panel = chapter.querySelector('.panel');
      if (!panel) continue;
      gsap.fromTo(
        panel,
        { y: 18 },
        {
          y: 0,
          ease: 'none',
          scrollTrigger: {
            trigger: chapter,
            start: 'top 92%',
            end: 'top 45%',
            scrub: true,
          },
        },
      );
    }
  }
}

async function detectQuality() {
  const QUALITY = {
    0: { dpr: 1, shadow: 1024 },
    1: { dpr: 1.5, shadow: 1024 },
    2: { dpr: 2, shadow: 2048 },
    3: { dpr: 2, shadow: 2048 },
  };
  try {
    const gpu = await getGPUTier({
      benchmarksURL: new URL('detect-gpu', document.baseURI).href,
    });
    const q = QUALITY[gpu.tier] ?? QUALITY[1];
    root.dataset.gpuTier = String(gpu.tier);
    return q;
  } catch {
    return QUALITY[1];
  }
}

async function boot() {
  const why = webglWhyNot();
  if (why) return articleOnly(why);

  root.classList.add('webgl-on');
  const quality = await detectQuality();
  let boxes = measure();
  const ids = boxes.map((b) => b.id);
  for (const p of problems(ids)) console.error(p);

  let shown = null;
  let dirty = true;
  let needMeasure = false;
  let current = -1;
  let stage;
  try {
    const { createStage } = await import('./stage.js');
    stage = createStage(document.getElementById('stage'), {
      phone: phone.matches,
      maxDpr: phone.matches ? Math.min(quality.dpr, 1.5) : quality.dpr,
      shadowSize: quality.shadow,
      onChange: () => {
        dirty = true;
      },
    });
  } catch (e) {
    return articleOnly('the scene failed to start: ' + (e && e.message ? e.message : e));
  }

  initScrollLayers();

  const stepNum = document.getElementById('step-num');
  const dots = [...document.querySelectorAll('.chapter-dots .dot')];

  const wanted = () => {
    const T = timeAt(boxes, scrollY);
    return reduced.matches ? restingT(T, ids.length) : T;
  };

  function mark() {
    const T = timeAt(boxes, scrollY + innerHeight * readLine());
    const i = Math.min(ids.length - 1, Math.floor(T));
    if (i === current) return;
    if (current >= 0) {
      boxes[current].el.classList.remove('is-current');
      if (dots[current]) dots[current].classList.remove('is-active');
    }
    boxes[i].el.classList.add('is-current');
    if (dots[i]) dots[i].classList.add('is-active');
    if (stepNum) stepNum.textContent = String(i + 1).padStart(2, '0');
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
    ScrollTrigger.refresh();
    dirty = true;
  };

  addEventListener('resize', () => {
    needMeasure = true;
  });
  reduced.addEventListener('change', () => {
    dirty = true;
  });
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(() => {
      needMeasure = true;
    });
  }

  function frame() {
    requestAnimationFrame(frame);
    if (needMeasure) {
      needMeasure = false;
      resize();
    }
    const T = wanted();
    if (T !== shown || dirty) draw(T);
  }

  Object.assign(site, {
    mode: 'webgl',
    chapters: ids,
    setProgress(T) {
      if (needMeasure) {
        needMeasure = false;
        resize();
      }
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
  stage.ready.then(() => {
    draw(wanted());
    resolveReady();
  });
}

boot().catch((e) => {
  console.error('the page failed to start:', e);
  articleOnly('the page failed to start');
});
