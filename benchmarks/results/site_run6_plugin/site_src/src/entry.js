// entry.js: Self-hosted fonts, Lenis smooth scrolling, GSAP ScrollTrigger scrubbed panel choreography,
// detect-gpu quality telemetry, and HUD state binding on top of the starter.
import '@fontsource-variable/space-grotesk';
import '@fontsource-variable/inter';
import '@fontsource/jetbrains-mono/400.css';
import '@fontsource/jetbrains-mono/500.css';
import '@fontsource/jetbrains-mono/600.css';
import '@fontsource/jetbrains-mono/700.css';

import Lenis from 'lenis';
import gsap from 'gsap';
import { ScrollTrigger } from 'gsap/ScrollTrigger';
import { getGPUTier } from 'detect-gpu';
import { MOVE } from './shots.js';

gsap.registerPlugin(ScrollTrigger);

const reduced = matchMedia('(prefers-reduced-motion: reduce)');
const phone = matchMedia('(max-width: 899px) and (orientation: portrait)');

// 1. Smooth wheel scrolling on desktop (native touch; disabled under prefers-reduced-motion).
let lenis = null;
if (!reduced.matches) {
  lenis = new Lenis({ allowNestedScroll: true });
  lenis.on('scroll', ScrollTrigger.update);
  gsap.ticker.add((seconds) => lenis.raf(seconds * 1000));
  gsap.ticker.lagSmoothing(0);
}

// 2. Boot the starter stage and wait until WebGL + story layers are mounted.
await import('./main.js');
const site = window.__site;
site.scrollWith = (y) => (lenis ? lenis.scrollTo(y) : window.scrollTo(0, y));
await site.ready;

// 3. Parallel GPU tier probe using the existing WebGL context and self-hosted benchmarks (zero external requests).
if (site.mode === 'webgl') {
  const gl = site.glContext ? site.glContext() : null;
  if (gl) {
    Promise.race([
      getGPUTier({ glContext: gl, benchmarksURL: new URL('detect-gpu', document.baseURI).href }),
      new Promise((resolve) => setTimeout(() => resolve({ tier: 1, type: 'TIMEOUT' }), 1500)),
    ]).then((gpu) => {
      document.documentElement.dataset.gpuTier = String(gpu.tier ?? 1);
      document.documentElement.dataset.gpuType = String(gpu.type ?? 'FALLBACK');
    }).catch(() => {});
  }
}

// 4. GSAP ScrollTrigger scrubbed stagger inside each panel after the first (starts at MOVE when panel becomes visible).
if (site.mode === 'webgl' && !reduced.matches && !phone.matches) {
  const chapters = [...document.querySelectorAll('[data-chapter]')];
  chapters.slice(1).forEach((chapter) => {
    const kids = chapter.querySelectorAll('.panel > *:not(.still)');
    if (kids.length < 2) return;
    gsap.from(kids, {
      y: 14,
      opacity: 0.01,
      ease: 'none',
      stagger: 0.18,
      scrollTrigger: {
        trigger: chapter,
        start: () => `top+=${Math.round(MOVE * chapter.offsetHeight)} top`,
        end: () => `top+=${Math.round((MOVE + 0.16) * chapter.offsetHeight)} top`,
        scrub: true,
      },
    });
  });
}

// 5. Wire top HUD progress bar and active chapter indicator directly from story time T (never a duplicate progress).
const progressFill = document.getElementById('progress-fill');
const navLinks = [...document.querySelectorAll('.chapter-nav a[data-nav]')];
if (site.onDraw) {
  site.onDraw((T, chapterId, total) => {
    if (progressFill && total > 0) {
      const pct = Math.max(0, Math.min(100, (T / total) * 100));
      progressFill.style.width = `${pct.toFixed(2)}%`;
    }
    for (const a of navLinks) {
      a.classList.toggle('is-active', a.dataset.nav === chapterId);
    }
  });
}
