// site_probe.js: installed by check_site (site_check.py) in every page before the page's own scripts run.
//
// A built site seldom exposes its renderer (run 1's did not), so what its scene costs is read from outside: the WebGL
// methods every renderer must call are wrapped on the context prototypes, and the counts are kept per animation frame.
//   draw calls  = calls of drawArrays / drawElements / drawRangeElements and their instanced and multi-draw forms
//                 (one multi-draw call counts once, as three.js counts it);
//   triangles   = per call, count / 3 for TRIANGLES, count - 2 for TRIANGLE_STRIP and TRIANGLE_FAN, times the instance
//                 count (lines and points are not triangles and are not counted), the rule renderer.info uses;
//   textures    = bytes of every texture level the page allocates or uploads (texImage2D/3D, texStorage2D/3D,
//                 compressedTexImage2D/3D, plus a third of level 0 after generateMipmap), from the sized format or
//                 format x type, minus deleted textures; render buffers (depth, multisampling) are counted apart.
// A frame is the time between two of this script's requestAnimationFrame callbacks; it registers first, so a frame's
// count is the work the page did in its own callbacks of that frame (and in any event handler since).
// Not seen: a renderer in a worker (OffscreenCanvas) and WebGPU; the report says when a page used either.
//
// The page's clock: on software WebGL a heavy frame takes 0.5 to 2 s, a gap no visitor's GPU produces. GSAP treats any
// gap over 500 ms as 33 ms (lagSmoothing), so on run 1's page a one-second scrub needed some thirty such frames and
// never arrived. With cfg.frameCapMs the page sees at most that many milliseconds pass between two frames
// (performance.now, Date.now and the requestAnimationFrame timestamp move together); a page drawing faster than that
// sees the real clock.
(() => {
  if (window.__agySite) return;
  const cfg = window.__agySiteConfig || {};
  const S = window.__agySite = {
    contexts: [], canvases: [], webgpu: false, offscreen: false, reducedMotionQueried: false,
    frames: 0, cur: { draws: 0, tris: 0 }, win: null, textures: new Map(), renderbuffers: new Map(), held_ms: 0,
  };
  const pnow = performance.now.bind(performance), dnow = Date.now;
  const newWin = () => ({ frames: 0, drawn: 0, maxDraws: 0, maxTris: 0, t0: pnow() });
  S.win = newWin();
  S.mark = () => { const w = S.win; S.win = newWin(); return w; };

  // ---- the canvas layer (site_check.py, "the camera"): where this browser's screenshots would leave a black box in a
  // WebGL canvas, every canvas is given a layer of its own. First in the head and without specificity, so a rule of the
  // page's own wins; added as soon as the document has a head, which is before the page can draw anything.
  if (cfg.canvasLayerCss) {
    const add = () => {
      const st = document.createElement('style'); st.dataset.agySite = 'canvas-layer'; st.textContent = cfg.canvasLayerCss;
      document.head.prepend(st);
    };
    if (document.head) add();
    else new MutationObserver((_, o) => { if (document.head) { o.disconnect(); add(); } }).observe(document, { childList: true, subtree: true });
  }

  // ---- the clock
  const cap = cfg.frameCapMs || 0;
  let lastReal = null;
  if (cap) {
    performance.now = () => pnow() - S.held_ms;
    Date.now = () => dnow() - S.held_ms;
  }

  // ---- what check_site does to the page (one call per step, so a busy page is interrupted as little as possible)
  const root = () => document.scrollingElement || document.documentElement;
  let scroller = null;
  S.geometry = () => {
    let el = root(), max = el.scrollHeight - innerHeight;
    if (max <= 4) {  // the page scrolls inside an element instead of the window
      for (const e of document.querySelectorAll('body *')) {
        const cs = getComputedStyle(e); if (!/(auto|scroll)/.test(cs.overflowY)) continue;
        const m = e.scrollHeight - e.clientHeight; if (m > max) { max = m; el = e; }
      }
    }
    scroller = el;
    return { max: Math.max(0, max), inner: el !== root() };
  };
  S.y = () => (scroller && scroller !== root() ? scroller.scrollTop : scrollY);
  S.scrollTo = (t) => {
    S.mark();
    if (scroller && scroller !== root()) scroller.scrollTop = t; else window.scrollTo({ top: t, behavior: 'instant' });
    return S.y();
  };
  const canvasRect = () => {
    const out = [];
    for (const c of document.querySelectorAll('canvas[data-agy3d-gl]')) {
      const r = c.getBoundingClientRect(), cs = getComputedStyle(c);
      const x0 = Math.max(0, r.left), y0 = Math.max(0, r.top), x1 = Math.min(innerWidth, r.right), y1 = Math.min(innerHeight, r.bottom);
      if (x1 - x0 > 8 && y1 - y0 > 8 && cs.visibility !== 'hidden' && cs.display !== 'none') out.push({ x: x0, y: y0, width: x1 - x0, height: y1 - y0 });
    }
    out.sort((a, b) => b.width * b.height - a.width * a.height);
    return out[0] || null;
  };
  const sideways = () => {
    const W = document.documentElement.clientWidth, over = document.documentElement.scrollWidth - W;
    if (over <= 1) return { over: 0, culprits: [] };
    const found = [];
    for (const el of document.querySelectorAll('body *')) {
      const r = el.getBoundingClientRect();
      if (r.right > W + 1 && r.width > 0 && r.width <= W * 3) found.push([r.right - W, el.tagName.toLowerCase() + (el.id ? '#' + el.id : (el.classList[0] ? '.' + el.classList[0] : ''))]);
    }
    found.sort((a, b) => b[0] - a[0]);
    return { over, culprits: [...new Set(found.map((o) => o[1] + ' (' + Math.round(o[0]) + ' px)'))].slice(0, 3) };
  };
  // ---- the page's subjects (window.__site.info().onScreen), and how much of each the visitor cannot see
  // The hook is the page's own code: it may be missing, throw, never resolve, or return anything at all. Nothing it
  // returns is trusted: at most SUBJECTS entries are read, names are cut, every number is checked.
  const SUBJECTS = 64, NAME_CHARS = 80, POINTS = 3000;
  // At 0.6 what lies under shows only as a faint shadow; run 2's panels were 0.94 and its top bar 1.
  const COVER_ALPHA = 0.6;
  const finite = (v) => typeof v === 'number' && Number.isFinite(v) && Math.abs(v) < 1000;
  const readHook = async () => {
    let info;
    try {
      const site = window.__site;
      if (!site || typeof site.info !== 'function') return { why: 'no window.__site.info()' };
      info = site.info();
      if (info && typeof info.then === 'function') {
        info = await Promise.race([info, new Promise((r) => setTimeout(() => r({ __late: true }), 500))]);
        if (info && info.__late) return { why: 'window.__site.info() did not resolve within 0.5 s' };
      }
    } catch (e) { return { why: 'window.__site.info() threw: ' + String((e && e.message) || e).slice(0, 120) }; }
    const list = [];
    let seen = 0, boxless = 0, more = false;
    try {
      const on = info && info.onScreen;
      if (!on || typeof on !== 'object') return { why: 'info() has no onScreen object' };
      for (const k in on) {
        if (seen >= SUBJECTS) { more = true; break; }
        seen++;
        try {
          const v = on[k], b = v && v.box;
          if (!b || typeof b !== 'object') { boxless++; continue; }
          const box = [b[0], b[1], b[2], b[3]];
          if (!box.every(finite) || !(box[2] > box[0]) || !(box[3] > box[1])) { boxless++; continue; }
          // subject: whether the current shot is about this prop, where the page says (true or false; absent: unknown)
          list.push({ name: String(k).slice(0, NAME_CHARS), kind: v.kind === 'label' ? 'label' : 'prop', box,
                      subject: v.subject === true ? true : v.subject === false ? false : null });
        } catch (e) { boxless++; }
      }
    } catch (e) { return { why: 'reading onScreen threw: ' + String((e && e.message) || e).slice(0, 120) }; }
    if (!list.length) return { why: seen ? 'onScreen gives no box' : 'onScreen is empty', seen };
    return { list, seen, boxless, more };
  };
  const sel = (el) => { const p = []; for (let e = el; e && e !== document.body && p.length < 3; e = e.parentElement) {
    let s = e.tagName.toLowerCase(); if (e.id) { p.unshift(s + '#' + e.id); break; }
    const c = [...e.classList].slice(0, 2).join('.'); if (c) s += '.' + c; p.unshift(s); } return p.join(' > '); };
  // How much an element blocks the view of what is drawn under it: its background's alpha (or 1 with a backdrop
  // filter, which blurs what is behind past recognising) times its own and every ancestor's opacity. Text, borders and
  // images without a background are not counted: a heading drawn straight on the scene leaves it in view around the
  // letters.
  const opacityOf = new Map();
  const chainOpacity = (el) => {
    if (!el || el.nodeType !== 1) return 1;
    if (opacityOf.has(el)) return opacityOf.get(el);
    const o = (parseFloat(getComputedStyle(el).opacity) || 0) * chainOpacity(el.parentElement);
    opacityOf.set(el, o);
    return o;
  };
  const blockOf = new Map();
  const blocking = (el) => {
    if (blockOf.has(el)) return blockOf.get(el);
    const cs = getComputedStyle(el);
    // Chromium gives rgb()/rgba() for most colours, but keeps color(), oklch() and the like as written, alpha after '/'
    const bg = (cs.backgroundColor || '').trim();
    const alpha = (t) => (t === undefined ? 1 : parseFloat(t) * (t.endsWith('%') ? 0.01 : 1));
    let a;
    if (bg === '' || bg === 'transparent') a = 0;
    else if (/^rgba?\(/.test(bg)) a = alpha(bg.slice(bg.indexOf('(') + 1, -1).split(/[\s,/]+/).filter(Boolean)[3]);
    else a = alpha((/\/\s*([\d.]+%?)\s*\)$/.exec(bg) || [])[1]);
    if (!Number.isFinite(a)) a = 0;
    if (cs.backdropFilter && cs.backdropFilter !== 'none' && /blur/.test(cs.backdropFilter)) a = 1;
    const b = a * chainOpacity(el);
    blockOf.set(el, b);
    return b;
  };
  const refCanvas = () => {
    let best = null;
    const pool = S.canvases.length ? S.canvases : [...document.querySelectorAll('canvas')];
    for (const c of pool) {
      const r = c.getBoundingClientRect();
      const a = Math.max(0, Math.min(innerWidth, r.right) - Math.max(0, r.left)) * Math.max(0, Math.min(innerHeight, r.bottom) - Math.max(0, r.top));
      if (a > 64 && (!best || a > best.a)) best = { c, a };
    }
    return best && best.c;
  };
  const r4 = (v) => Math.round(v * 1e4) / 1e4;
  // The measure itself is synchronous: pointer-events is forced on only for the length of this task, so no frame is
  // drawn and no hover state changes while it is on. elementsFromPoint gives what is drawn at a point in paint order,
  // and skips elements with pointer-events: none, which a text panel often has while it fades (run 2's panels did).
  const measureSubjects = (list) => {
    const W = innerWidth, H = innerHeight, canvas = refCanvas();
    opacityOf.clear(); blockOf.clear();
    const force = document.createElement('style');
    force.textContent = '*{pointer-events:auto!important}';
    (document.head || document.documentElement).appendChild(force);
    const per = Math.max(9, Math.floor(POINTS / list.length));
    try {
      return list.map((s) => {
        const [x0, y0, x1, y1] = s.box, w = x1 - x0, h = y1 - y0;
        const edges = {};
        if (x0 < 0) edges.left = r4(Math.min(-x0, w) / w);
        if (x1 > 1) edges.right = r4(Math.min(x1 - 1, w) / w);
        if (y0 < 0) edges.top = r4(Math.min(-y0, h) / h);
        if (y1 > 1) edges.bottom = r4(Math.min(y1 - 1, h) / h);
        const ix = Math.max(0, Math.min(x1, 1) - Math.max(x0, 0)), iy = Math.max(0, Math.min(y1, 1) - Math.max(y0, 0));
        const out = { name: s.name, kind: s.kind, subject: s.subject, box: s.box.map(r4), px: [Math.round(w * W), Math.round(h * H)],
                      cut: r4(1 - (ix * iy) / (w * h)), edges, hidden: null, under: '' };
        const vw = ix * W, vh = iy * H;
        if (!canvas || vw < 2 || vh < 2) return out;
        const nx = Math.max(1, Math.min(16, Math.round(Math.sqrt(per * vw / vh)), Math.ceil(vw / 4)));
        const ny = Math.max(1, Math.min(16, Math.floor(per / nx), Math.ceil(vh / 4)));
        const left = Math.max(x0, 0) * W, top = Math.max(y0, 0) * H;
        let covered = 0, n = 0;
        const by = new Map();
        for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) {
          const stack = document.elementsFromPoint(left + (i + 0.5) * vw / nx, top + (j + 0.5) * vh / ny);
          const at = stack.indexOf(canvas);
          n++;
          if (at < 0) continue;  // the scene is not drawn here at all: nothing of it to hide
          for (let k = at - 1; k >= 0; k--) {
            if (blocking(stack[k]) >= COVER_ALPHA) { covered++; by.set(stack[k], (by.get(stack[k]) || 0) + 1); break; }
          }
        }
        out.hidden = n ? r4(covered / n) : null;
        let top1 = null;
        for (const [el, c] of by) if (!top1 || c > top1[1]) top1 = [el, c];
        if (top1) out.under = sel(top1[0]);
        return out;
      });
    } finally { force.remove(); }
  };
  S.subjects = async () => {
    const got = await readHook();
    if (!got.list) return got;
    let measured;
    try { measured = measureSubjects(got.list); } catch (e) { return { why: 'the measure failed: ' + String((e && e.message) || e).slice(0, 120) }; }
    return { list: measured, seen: got.seen, boxless: got.boxless, more: got.more };
  };

  // ---- stills for the page without WebGL: window.__site.chapters() -> [{ id, rest }], drawn with setProgress(rest)
  // Untrusted like onScreen: at most CHAPTERS entries, ids as strings cut to NAME_CHARS, rest a finite number.
  const CHAPTERS = 24;
  const late = (p, ms) => Promise.race([p, new Promise((r) => setTimeout(() => r({ __late: true }), ms))]);
  S.chapterList = async () => {
    let got;
    try {
      const site = window.__site;
      if (!site || typeof site.chapters !== 'function' || typeof site.setProgress !== 'function') {
        return { why: 'no window.__site.chapters() and setProgress()' };
      }
      got = site.chapters();
      if (got && typeof got.then === 'function') got = await late(got, 500);
      if (got && got.__late) return { why: 'window.__site.chapters() did not resolve within 0.5 s' };
    } catch (e) { return { why: 'window.__site.chapters() threw: ' + String((e && e.message) || e).slice(0, 120) }; }
    const list = [];
    let more = false;
    try {
      if (!got || typeof got.length !== 'number') return { why: 'chapters() did not return a list' };
      const n = Math.min(Number(got.length) || 0, CHAPTERS + 1);
      for (let i = 0; i < n; i++) {
        if (list.length >= CHAPTERS) { more = true; break; }
        try {
          const c = got[i], id = c && c.id, rest = c && c.rest;
          if ((typeof id !== 'string' && typeof id !== 'number') || !finite(rest)) continue;
          list.push({ id: String(id).slice(0, NAME_CHARS), rest });
        } catch (e) { /* one bad entry: the others still count */ }
      }
    } catch (e) { return { why: 'reading chapters() threw: ' + String((e && e.message) || e).slice(0, 120) }; }
    return list.length ? { list, more } : { why: 'chapters() gave no { id, rest } with a finite rest' };
  };
  // The largest WebGL canvas in view, read from its drawing buffer as a PNG data URL. Only right after a draw: a canvas
  // without preserveDrawingBuffer is blank to read once its frame is shown. The drawing buffer holds the scene alone, at
  // full size, without the page's HTML, a scrollbar, or what a browser without the GPU path leaves out of a WebGL
  // canvas that lies under an opaque box (site_check.py, "the camera": the band in every frame of run 2's site).
  const readCanvas = () => {
    let best = null;
    for (const c of S.canvases) {
      const r = c.getBoundingClientRect();
      const a = Math.max(0, Math.min(innerWidth, r.right) - Math.max(0, r.left)) * Math.max(0, Math.min(innerHeight, r.bottom) - Math.max(0, r.top));
      if (a > 64 && c.width > 0 && c.height > 0 && (!best || a > best.a)) best = { c, a };
    }
    if (!best) return null;
    try { return best.c.toDataURL('image/png'); } catch (e) { return null; }
  };
  const progress = (T) => { try { window.__site.setProgress(T); return null; } catch (e) { return String((e && e.message) || e).slice(0, 120); } };
  // Draw the moment T and read it as soon as it is drawn. The page draws on demand: setProgress draws at once (the
  // starter's does) or on its next frame; textures that arrive meanwhile ask for a redraw. So: draw, let frames pass
  // until the page has stopped drawing, then draw again and read in the same task, or catch its next frame (grab).
  // A page that redraws only when T changes is moved off T and back.
  S.stillAt = async (T) => {
    let why = progress(T);
    if (why) return { why: 'setProgress threw: ' + why };
    for (let i = 0; i < 20; i++) {
      const before = S.frames, drawnBefore = S.win.drawn;
      await late(S.afterFrames(3), 2000);
      if (S.win.drawn === drawnBefore || S.frames === before) break;
    }
    for (const nudge of [false, true]) {
      if (nudge) { progress(T - 1e-3); await late(S.afterFrames(2), 2000); }
      const d0 = S.cur.draws;
      why = progress(T);
      if (why) return { why: 'setProgress threw: ' + why };
      if (S.cur.draws > d0) { const data = readCanvas(); if (data) return { data, how: 'drawn by setProgress' }; }
      const data = await late(S.grabCanvas(4, 'image/png'), 3000);
      if (typeof data === 'string') return { data, how: 'drawn on the next frame' };
    }
    return { why: 'the page drew nothing after setProgress(' + T + ')' };
  };

  let hider = null;
  // After a step's frame: the frame counts, where the page is, and (hide) the WebGL canvas alone on screen, two frames on.
  S.stepEnd = (hide) => new Promise((done) => {
    const out = { win: S.mark(), last: S.lastDrawn || null, y: S.y(), sideways: sideways(), rect: canvasRect() };
    if (!hide || !out.rect) { done(out); return; }
    hider = document.createElement('style');
    hider.textContent = '*{visibility:hidden!important;transition:none!important}canvas[data-agy3d-gl]{visibility:visible!important}';
    document.head.appendChild(hider);
    raf(() => raf(() => done(out)));
  });
  S.unhide = () => { if (hider) { hider.remove(); hider = null; } };

  // ---- frames
  // The canvas alone: read from the drawing buffer right after the page's own frame callback drew into it (a canvas
  // without preserveDrawingBuffer is only readable in that task). One readback instead of hiding the page's HTML and
  // waiting two more frames for the compositor, which on a page that takes a second a frame was most of a step.
  let want = null;
  const grab = () => {
    if (!want || S.cur.draws === 0 || S.cur.draws === want.draws) return;
    want.draws = S.cur.draws;
    let best = null;
    for (const c of S.canvases) {
      const r = c.getBoundingClientRect();
      const a = Math.max(0, Math.min(innerWidth, r.right) - Math.max(0, r.left)) * Math.max(0, Math.min(innerHeight, r.bottom) - Math.max(0, r.top));
      if (a > 64 && (!best || a > best.a)) best = { c, a };
    }
    if (best) { try { want.data = best.c.toDataURL(want.type || 'image/jpeg', 0.8); } catch (e) {} }
  };
  // Resolves with the canvas as a data URL once a frame has drawn into it, or null after `frames` frames without one
  // (a page that draws only when something changes): check_site then hides the HTML and photographs the screen.
  S.grabCanvas = (frames, type) => new Promise((done) => { want = { draws: -1, data: null, left: frames, done, type }; });
  // Resolves once `n` more animation frames have run: two screenshots with no frame between them are the same
  // compositor frame, and would read as a picture that stopped changing while the page had not drawn yet.
  const waiting = [];
  S.afterFrames = (n) => new Promise((done) => waiting.push({ at: S.frames + n, done }));
  const raf = window.requestAnimationFrame.bind(window);
  window.requestAnimationFrame = (cb) => raf((ts) => { cb(cap ? ts - S.held_ms : ts); grab(); });
  const tick = () => {
    if (want) {  // the frame before this one is complete: hand over what it drew, if anything
      if (want.data) { want.done(want.data); want = null; }
      else if (--want.left < 0) { want.done(null); want = null; }
      else want.draws = -1;
    }
    const real = pnow();
    if (cap && lastReal !== null && real - lastReal > cap) S.held_ms += real - lastReal - cap;
    lastReal = real;
    const c = S.cur, w = S.win;
    w.frames++; S.frames++;
    if (c.draws > 0) w.drawn++;
    if (c.draws > w.maxDraws) w.maxDraws = c.draws;
    if (c.tris > w.maxTris) w.maxTris = c.tris;
    if (c.draws > 0) S.lastDrawn = c;  // the latest frame that drew: what is on screen now
    S.cur = { draws: 0, tris: 0 };
    for (let i = waiting.length - 1; i >= 0; i--) if (waiting[i].at <= S.frames) waiting.splice(i, 1)[0].done(S.frames);
    raf(tick);
  };
  raf(tick);

  // ---- what the page asks about the visitor
  const mm = window.matchMedia;
  window.matchMedia = function (q) {
    if (/prefers-reduced-motion/.test(String(q))) S.reducedMotionQueried = true;
    return mm.call(window, q);
  };

  // ---- contexts (and WebGL taken away for the no-WebGL visit)
  const isGL = (t) => /^(webgl2?|experimental-webgl)$/.test(String(t));
  const getContext = HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext = function (type, ...rest) {
    if (isGL(type) && cfg.noWebGL) return null;
    const ctx = getContext.call(this, type, ...rest);
    if (ctx && isGL(type) && !S.contexts.includes(ctx)) {
      S.contexts.push(ctx); S.canvases.push(this); this.setAttribute('data-agy3d-gl', String(S.contexts.length));
      // a lost context draws nothing until the page restores it: seen on run 1's page, whose textures take 454 MB.
      // Only a canvas in the document counts: the skill's starter asks a canvas it never shows whether WebGL 2 is
      // there and releases that context at once, which is tidy and was reported as "the WebGL context was lost".
      this.addEventListener('webglcontextlost', () => { if (this.isConnected) S.lost = (S.lost || 0) + 1; });
    }
    return ctx;
  };
  if (window.OffscreenCanvas) {
    const og = OffscreenCanvas.prototype.getContext;
    OffscreenCanvas.prototype.getContext = function (type, ...rest) {
      if (isGL(type)) { if (cfg.noWebGL) return null; S.offscreen = true; }
      return og.call(this, type, ...rest);
    };
  }
  if (navigator.gpu) {
    if (cfg.noWebGL) { try { Object.defineProperty(Navigator.prototype, 'gpu', { get: () => undefined, configurable: true }); } catch (e) {} }
    else { const ra = navigator.gpu.requestAdapter.bind(navigator.gpu); navigator.gpu.requestAdapter = (...a) => { S.webgpu = true; return ra(...a); }; }
  }
  if (cfg.noWebGL) {
    for (const k of ['WebGLRenderingContext', 'WebGL2RenderingContext']) {
      // a page that tests for the constructor is told the truth too
      try { Object.defineProperty(window, k, { value: undefined, configurable: true, writable: true }); } catch (e) {}
    }
    return;
  }

  // ---- triangles
  const tris = (mode, count, inst) => {
    if (mode === 4) return Math.floor(count / 3) * inst;
    if (mode === 5 || mode === 6) return Math.max(count - 2, 0) * inst;
    return 0;
  };
  const draw = (mode, count, inst) => { S.cur.draws++; S.cur.tris += tris(mode, count, inst === undefined ? 1 : inst); };

  // ---- texture bytes
  const SIZED = {
    0x8229: 1, 0x822B: 2, 0x8051: 3, 0x8058: 4, 0x8C41: 3, 0x8C43: 4, 0x822D: 2, 0x822F: 4, 0x881B: 6, 0x881A: 8,
    0x822E: 4, 0x8230: 8, 0x8815: 12, 0x8814: 16, 0x8C3A: 4, 0x8C3D: 4, 0x8059: 4, 0x8D62: 2, 0x8056: 2, 0x8057: 2,
    0x81A5: 2, 0x81A6: 4, 0x8CAC: 4, 0x88F0: 4, 0x8CAD: 8, 0x8232: 1, 0x8231: 1, 0x8D7C: 4, 0x8D8E: 4, 0x8234: 2,
    0x8233: 2, 0x8236: 4, 0x8235: 4, 0x8D70: 16, 0x8D82: 16, 0x8D76: 8, 0x8D88: 8, 0x8238: 2, 0x8237: 2, 0x823A: 4,
    0x8239: 4, 0x823C: 8, 0x823B: 8, 0x8D8F: 3, 0x8F96: 4, 0x8F94: 1, 0x8F95: 2, 0x8F97: 4, 0x906F: 4, 0x8D48: 1,
  };
  // compressed formats: bytes per pixel (block size / block area)
  const COMPRESSED = {
    0x83F0: 0.5, 0x83F1: 0.5, 0x83F2: 1, 0x83F3: 1, 0x8C4C: 0.5, 0x8C4D: 0.5, 0x8C4E: 1, 0x8C4F: 1,
    0x8E8C: 1, 0x8E8D: 1, 0x8E8E: 1, 0x8E8F: 1, 0x8DBB: 0.5, 0x8DBC: 0.5, 0x8DBD: 1, 0x8DBE: 1,
    0x9270: 0.5, 0x9271: 0.5, 0x9272: 1, 0x9273: 1, 0x9274: 0.5, 0x9275: 0.5, 0x9276: 0.5, 0x9277: 0.5, 0x9278: 1, 0x9279: 1,
    0x8D64: 0.5, 0x8C00: 0.5, 0x8C01: 0.25, 0x8C02: 0.5, 0x8C03: 0.25,
  };
  const ASTC = [[4, 4], [5, 4], [5, 5], [6, 5], [6, 6], [8, 5], [8, 6], [8, 8], [10, 5], [10, 6], [10, 8], [10, 10], [12, 10], [12, 12]];
  ASTC.forEach(([w, h], i) => { COMPRESSED[0x93B0 + i] = 16 / (w * h); COMPRESSED[0x93D0 + i] = 16 / (w * h); });
  const COMPONENTS = { 0x1908: 4, 0x1907: 3, 0x1906: 1, 0x1909: 1, 0x190A: 2, 0x1903: 1, 0x8227: 2, 0x8D94: 1, 0x8228: 2,
                       0x8D98: 3, 0x8D99: 4, 0x1902: 1, 0x84F9: 1, 0x8C40: 3, 0x8C42: 4 };
  const TYPE_BYTES = { 0x1401: 1, 0x1400: 1, 0x1403: 2, 0x1402: 2, 0x1405: 4, 0x1404: 4, 0x1406: 4, 0x140B: 2, 0x8D61: 2 };
  const PACKED = { 0x8363: 2, 0x8033: 2, 0x8034: 2, 0x84FA: 4, 0x8DAD: 8, 0x8368: 4, 0x8C3B: 4, 0x8C3E: 4 };
  const bpp = (ifmt, format, type) => {
    if (SIZED[ifmt] !== undefined) return SIZED[ifmt];
    if (COMPRESSED[ifmt] !== undefined) return COMPRESSED[ifmt];
    if (PACKED[type] !== undefined) return PACKED[type];
    return (COMPONENTS[format] || COMPONENTS[ifmt] || 4) * (TYPE_BYTES[type] || 1);
  };
  const srcSize = (s) => {
    if (!s) return [0, 0];
    const w = s.naturalWidth || s.videoWidth || s.displayWidth || s.width || 0;
    const h = s.naturalHeight || s.videoHeight || s.displayHeight || s.height || 0;
    return [w, h];
  };
  const kindOf = (target) => (target >= 0x8515 && target <= 0x851A ? 0x8513 : target);
  const state = (gl) => gl.__agy || (gl.__agy = { unit: 0, bound: {}, rb: null });
  const boundTex = (gl, target) => state(gl).bound[state(gl).unit + ':' + kindOf(target)];
  const setLevel = (gl, target, key, bytes, empty) => {
    const tex = boundTex(gl, target);
    if (!tex) return;
    let rec = S.textures.get(tex);
    if (!rec) { rec = { levels: {}, mip: false, empty: true }; S.textures.set(tex, rec); }
    rec.levels[key] = bytes;
    if (!empty) rec.empty = false;
  };
  const isEmpty = (d) => d === null || d === undefined;

  const wrap = (proto, name, after) => {
    const f = proto[name];
    if (typeof f !== 'function') return;
    proto[name] = function (...a) { const r = f.apply(this, a); try { after.call(this, a, r); } catch (e) {} return r; };
  };
  const wrapContext = (proto) => {
    if (!proto) return;
    wrap(proto, 'drawArrays', (a) => draw(a[0], a[2], 1));
    wrap(proto, 'drawElements', (a) => draw(a[0], a[1], 1));
    wrap(proto, 'drawRangeElements', (a) => draw(a[0], a[3], 1));
    wrap(proto, 'drawArraysInstanced', (a) => draw(a[0], a[2], a[3]));
    wrap(proto, 'drawElementsInstanced', (a) => draw(a[0], a[1], a[4]));
    wrap(proto, 'activeTexture', function (a) { state(this).unit = a[0]; });
    wrap(proto, 'bindTexture', function (a) { state(this).bound[state(this).unit + ':' + a[0]] = a[1]; });
    wrap(proto, 'deleteTexture', (a) => { S.textures.delete(a[0]); });
    wrap(proto, 'texImage2D', function (a) {
      const level = a[1];
      let w, h, b, empty;
      if (a.length >= 8) { [w, h] = [a[3], a[4]]; b = bpp(a[2], a[6], a[7]); empty = isEmpty(a[8]); }
      else { [w, h] = srcSize(a[5]); b = bpp(a[2], a[3], a[4]); empty = false; }
      setLevel(this, a[0], a[0] + ':' + level, w * h * b, empty);
    });
    wrap(proto, 'texImage3D', function (a) {
      setLevel(this, a[0], a[0] + ':' + a[1], a[3] * a[4] * a[5] * bpp(a[2], a[7], a[8]), isEmpty(a[9]));
    });
    const storage = function (target, levels, ifmt, w, h, d) {
      let total = 0;
      for (let l = 0; l < levels; l++) total += Math.max(1, w >> l) * Math.max(1, h >> l) * (target === 0x806F ? Math.max(1, d >> l) : d);
      total *= bpp(ifmt, 0, 0) * (target === 0x8513 ? 6 : 1);
      setLevel(this, target, 'storage', total, true);
    };
    wrap(proto, 'texStorage2D', function (a) { storage.call(this, a[0], a[1], a[2], a[3], a[4], 1); });
    wrap(proto, 'texStorage3D', function (a) { storage.call(this, a[0], a[1], a[2], a[3], a[4], a[5]); });
    // a texture allocated by texStorage gets its pixels through the sub-image calls: that is an upload
    for (const n of ['texSubImage2D', 'texSubImage3D', 'compressedTexSubImage2D', 'compressedTexSubImage3D']) {
      wrap(proto, n, function (a) { const t = boundTex(this, a[0]); const r = t && S.textures.get(t); if (r) r.empty = false; });
    }
    const compressedBytes = (a, at) => (typeof a[at] === 'number' ? a[at] : (a[at + 2] || (a[at] ? a[at].byteLength - (a[at + 1] || 0) : 0)));
    wrap(proto, 'compressedTexImage2D', function (a) { setLevel(this, a[0], a[0] + ':' + a[1], compressedBytes(a, 6), false); });
    wrap(proto, 'compressedTexImage3D', function (a) { setLevel(this, a[0], a[0] + ':' + a[1], compressedBytes(a, 7), false); });
    wrap(proto, 'generateMipmap', function (a) { const t = boundTex(this, a[0]); const r = t && S.textures.get(t); if (r) r.mip = true; });
    wrap(proto, 'bindRenderbuffer', function (a) { state(this).rb = a[1]; });
    wrap(proto, 'deleteRenderbuffer', (a) => { S.renderbuffers.delete(a[0]); });
    wrap(proto, 'renderbufferStorage', function (a) { const rb = state(this).rb; if (rb) S.renderbuffers.set(rb, a[2] * a[3] * bpp(a[1], 0, 0)); });
    wrap(proto, 'renderbufferStorageMultisample', function (a) {
      const rb = state(this).rb; if (rb) S.renderbuffers.set(rb, a[3] * a[4] * bpp(a[2], 0, 0) * Math.max(1, a[1]));
    });
    wrap(proto, 'getExtension', function (a, ext) {
      if (!ext || ext.__agyWrapped) return;
      ext.__agyWrapped = true;
      if (a[0] === 'ANGLE_instanced_arrays') {
        wrap(ext, 'drawArraysInstancedANGLE', (b) => draw(b[0], b[2], b[3]));
        wrap(ext, 'drawElementsInstancedANGLE', (b) => draw(b[0], b[1], b[4]));
      } else if (a[0] === 'WEBGL_multi_draw') {
        const sum = (list, off, n, inst, ioff) => { let t = 0; for (let i = 0; i < n; i++) t += tris(b0, list[off + i], inst ? inst[ioff + i] : 1); return t; };
        let b0 = 4;
        wrap(ext, 'multiDrawArraysWEBGL', (b) => { b0 = b[0]; S.cur.draws++; S.cur.tris += sum(b[3], b[4], b[5]); });
        wrap(ext, 'multiDrawElementsWEBGL', (b) => { b0 = b[0]; S.cur.draws++; S.cur.tris += sum(b[1], b[2], b[6]); });
        wrap(ext, 'multiDrawArraysInstancedWEBGL', (b) => { b0 = b[0]; S.cur.draws++; S.cur.tris += sum(b[3], b[4], b[7], b[5], b[6]); });
        wrap(ext, 'multiDrawElementsInstancedWEBGL', (b) => { b0 = b[0]; S.cur.draws++; S.cur.tris += sum(b[1], b[2], b[8], b[6], b[7]); });
      }
    });
  };
  wrapContext(window.WebGLRenderingContext && WebGLRenderingContext.prototype);
  wrapContext(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);

  S.memory = () => {
    let bytes = 0, uploaded = 0, count = 0;
    for (const r of S.textures.values()) {
      const levels = Object.values(r.levels);
      let b = levels.reduce((s, x) => s + x, 0);
      if (r.mip && !('storage' in r.levels)) b += Object.entries(r.levels).filter(([k]) => k.endsWith(':0')).reduce((s, [, x]) => s + x, 0) / 3;
      bytes += b; count++;
      if (!r.empty) uploaded += b;
    }
    let rb = 0; for (const b of S.renderbuffers.values()) rb += b;
    return { texture_bytes: Math.round(bytes), uploaded_bytes: Math.round(uploaded), textures: count, renderbuffer_bytes: rb };
  };
})();
