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

  // ---- the stand-in (site_check.py, CAMERA_ATTR): <html> carries the attribute before any script of the page runs, so
  // a page can draw what a visitor with a GPU would see although this browser renders in software (the skill's starter
  // turns its ambient occlusion on). At this moment the document may have no element yet; the parser's <html> is then
  // marked as it is inserted, and a mutation observer's callback runs before the parser runs the first script.
  if (cfg.cameraAttr) {
    const mark = () => {
      if (!document.documentElement) return false;
      document.documentElement.setAttribute(cfg.cameraAttr, '');
      return true;
    };
    if (!mark()) {
      const seen = new MutationObserver((_, o) => { if (mark()) o.disconnect(); });
      seen.observe(document, { childList: true });
    }
  }

  // ---- the canvas layer (site_check.py, "the camera"): where this browser's screenshots would leave a black box in a
  // WebGL canvas, a canvas is given a layer of its own: a rule that is first in the head and without specificity, so
  // any rule of the page's own wins, for canvases this script marks (S.layerCanvases, before every screenshot).
  // The layer is a filter, and a filter makes its element a stacking context, painted with the positioned content.
  // For a positioned canvas (fixed, absolute, relative, sticky) nothing changes. A canvas in the normal flow would be
  // painted over positioned content that comes before it in the document, a header written above the canvas for one.
  // So such a canvas is tried: what is painted over it at a grid of points is read without the mark and with it, and
  // where anything that was over the canvas is no longer over it, the canvas stays as it is and is counted.
  if (cfg.canvasLayerCss) {
    const MARK = cfg.canvasLayerAttr || 'data-agy3d-layer';
    const add = () => {
      const st = document.createElement('style'); st.dataset.agySite = 'canvas-layer'; st.textContent = cfg.canvasLayerCss;
      document.head.prepend(st);
    };
    if (document.head) add();
    else {
      const seen = new MutationObserver((_, o) => { if (document.head) { o.disconnect(); add(); } });
      seen.observe(document, { childList: true, subtree: true });
      // a document that never gets a head (an SVG in a frame): nothing to add to, and nothing left watching
      document.addEventListener('DOMContentLoaded', () => seen.disconnect(), { once: true });
    }
    const over = (c) => {
      const r = c.getBoundingClientRect(), out = [];
      const x0 = Math.max(0, r.left), x1 = Math.min(innerWidth, r.right), y0 = Math.max(0, r.top), y1 = Math.min(innerHeight, r.bottom);
      if (x1 - x0 < 2 || y1 - y0 < 2) return out;
      for (let j = 0; j < 6; j++) for (let i = 0; i < 6; i++) {
        const stack = document.elementsFromPoint(x0 + (i + 0.5) * (x1 - x0) / 6, y0 + (j + 0.5) * (y1 - y0) / 6);
        const at = stack.indexOf(c);
        out.push(at < 0 ? null : stack.slice(0, at));
      }
      return out;
    };
    S.layer = { given: 0, left: 0 };
    S.layerLeft = 0;
    S.layerCanvases = () => {
      let given = 0, left = 0, force = null;
      try {
        for (const c of S.canvases) {
          if (!c.isConnected) continue;
          if (getComputedStyle(c).position !== 'static') { c.setAttribute(MARK, ''); given++; continue; }
          if (!force) {  // as in measureSubjects: elementsFromPoint skips what has pointer-events: none
            force = document.createElement('style'); force.textContent = '*{pointer-events:auto!important}';
            (document.head || document.documentElement).appendChild(force);
          }
          c.removeAttribute(MARK);
          const before = over(c);
          if (!before.length) continue;  // not in the window now: nothing of it is photographed, and nothing can be compared
          c.setAttribute(MARK, '');
          const after = over(c);
          const same = before.length === after.length && before.every((b, k) => b === null || (after[k] !== null && b.every((el) => after[k].includes(el))));
          if (same) given++; else { c.removeAttribute(MARK); left++; }
        }
      } finally { if (force) force.remove(); }
      S.layer = { given, left };
      S.layerLeft = Math.max(S.layerLeft, left);
      return S.layer;
    };
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
          // kind: a 3D label, a layer (an instanced layer or generated geometry a shot is about, measured as a prop
          // is) or a prop. subject: whether the current shot is about it, where the page says (true or false; absent:
          // unknown)
          const kind = v.kind === 'label' || v.kind === 'layer' ? v.kind : 'prop';
          list.push({ name: String(k).slice(0, NAME_CHARS), kind, box,
                      subject: v.subject === true ? true : v.subject === false ? false : null });
        } catch (e) { boxless++; }
      }
    } catch (e) { return { why: 'reading onScreen threw: ' + String((e && e.message) || e).slice(0, 120) }; }
    if (!list.length) return { why: seen ? 'onScreen gives no box' : 'onScreen is empty', seen };
    // moving: the page's camera is between two shots (the shared contract of round 8). Only true counts; anything else,
    // or a getter that throws, is a page that holds its shot, as before the flag existed.
    let moving = false;
    try { moving = info.moving === true; } catch (e) {}
    return { list, seen, boxless, more, moving };
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
    const b = backgroundAlpha(el) * chainOpacity(el);
    blockOf.set(el, b);
    return b;
  };
  // The alpha of what an element paints behind its content: its background colour's, or 1 with a backdrop blur.
  const backgroundAlpha = (el) => {
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
    return a;
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
  // ---- chapter text a visitor can read over the scene
  // The characters of headings, paragraphs and list items that are drawn over the visible part of the canvas's box at
  // TEXT_ALPHA or more of the chain of opacities (as the cover measure above), outside the page's header, nav and
  // footer. A page that says its camera is moving while this is up asks the visitor to stop and read a picture on its
  // way (site_check.py, subject_faults). A label drawn in the scene is pixels of the canvas, not text, and a top bar is
  // a header or a nav. A text node counts by the share of its box over the canvas, so a line half out of the window
  // counts half.
  const TEXT_ALPHA = 0.5, TEXT_BLOCKS = 'h1,h2,h3,h4,h5,h6,p,li', TEXT_NODES = 20000;
  const chapterText = () => {
    const canvas = refCanvas();
    if (!canvas) return 0;
    const r = canvas.getBoundingClientRect();
    const cx0 = Math.max(0, r.left), cy0 = Math.max(0, r.top);
    const cx1 = Math.min(innerWidth, r.right), cy1 = Math.min(innerHeight, r.bottom);
    if (cx1 <= cx0 || cy1 <= cy0 || !document.body) return 0;
    opacityOf.clear();
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const range = document.createRange();
    let n = 0, seen = 0;
    for (let t = walker.nextNode(); t && seen < TEXT_NODES; t = walker.nextNode()) {
      seen++;
      const chars = t.data.replace(/\s+/g, '').length;
      const el = t.parentElement;
      if (!chars || !el) continue;
      const block = el.closest(TEXT_BLOCKS);
      if (!block || block.closest('header,nav,footer')) continue;
      if (chainOpacity(el) < TEXT_ALPHA || getComputedStyle(el).visibility !== 'visible') continue;
      range.selectNodeContents(t);
      const b = range.getBoundingClientRect();
      if (b.width < 1 || b.height < 1) continue;
      const ix = Math.min(b.right, cx1) - Math.max(b.left, cx0), iy = Math.min(b.bottom, cy1) - Math.max(b.top, cy0);
      if (ix > 0 && iy > 0) n += chars * (ix * iy) / (b.width * b.height);
    }
    return Math.round(n);
  };
  S.chapterText = () => { try { return chapterText(); } catch (e) { return null; } };
  // What a scroll moves in the page's HTML (site_check.py, _Visitor.first_notch). Text that scrolls with the page
  // answers a scroll as plainly as a scene that moves, and a count of changed pixels undersells it: a block of thin
  // glyphs that moves 120 px changes few pixels. textPlaces() notes where each readable text node in the window is;
  // textMoved(px) says how many of the noted characters are now px or more from that place, or no longer readable.
  let places = null;
  const readable = (t) => {
    const el = t.parentElement;
    if (!el || chainOpacity(el) < TEXT_ALPHA || getComputedStyle(el).visibility !== 'visible') return null;
    const range = document.createRange();
    range.selectNodeContents(t);
    const b = range.getBoundingClientRect();
    if (b.width < 1 || b.height < 1 || b.bottom <= 0 || b.top >= innerHeight || b.right <= 0 || b.left >= innerWidth) return null;
    return b;
  };
  S.textPlaces = () => {
    places = [];
    try {
      opacityOf.clear();
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      let seen = 0;
      for (let t = walker.nextNode(); t && seen < TEXT_NODES; t = walker.nextNode()) {
        seen++;
        const chars = t.data.replace(/\s+/g, '').length, b = chars ? readable(t) : null;
        if (b) places.push({ t, chars, x: b.left, y: b.top });
      }
    } catch (e) { places = null; }
    return places ? places.length : null;
  };
  S.textMoved = (px) => {
    if (!places) return null;
    const out = { chars: 0, moved: 0 };
    try {
      opacityOf.clear();
      for (const q of places) {
        out.chars += q.chars;
        const b = q.t.isConnected ? readable(q.t) : null;
        if (!b || Math.abs(b.top - q.y) >= px || Math.abs(b.left - q.x) >= px) out.moved += q.chars;
      }
    } catch (e) { return null; }
    places = null;
    return out;
  };
  S.subjects = async () => {
    const got = await readHook();
    if (!got.list) return got;
    let measured;
    try { measured = measureSubjects(got.list); } catch (e) { return { why: 'the measure failed: ' + String((e && e.message) || e).slice(0, 120) }; }
    return { list: measured, seen: got.seen, boxless: got.boxless, more: got.more, moving: got.moving,
             text: S.chapterText() };
  };
  // The subjects between the photographed steps (site_check.py, _Visitor.between): no screenshot and no wait for
  // the picture to settle, only the page's next two frames, and then, for at most capMs, until no CSS transition is
  // still running. The starter's text panels cross-fade in 0.4 s; on a page that draws quickly two frames are 33 ms,
  // and the panel of the chapter just left would still be measured over the subject at nearly full opacity. A
  // transition that is still running after capMs is measured as it is.
  // A transition whose clock stands still is finished, so the measure reads what a visitor who stops here sees once
  // it is over. With its GPU path (CHROMIUM_KEEP_GL_SWITCHES=1) this headless browser runs an opacity transition on
  // its compositor and stops giving the page frames after two (requestAnimationFrame no longer fires): the time the
  // page's styles see stood at 17 ms of the starter's 400 ms fade for the whole 600 ms, and a panel fading out was
  // measured at 0.97 of its opacity, its text counted as readable during the move. Without the GPU path the clock runs
  // and the transition ends by itself within the wait.
  const STALLED_POLLS = 5;
  S.readBetween = async (capMs) => {
    await S.afterFrames(2);
    await transitionsOver(capMs);
    return S.subjects();
  };
  const transitionsOver = async (capMs) => {
    const t0 = pnow();
    let stalled = 0;
    for (;;) {
      let running = [];
      try { running = document.getAnimations().filter((a) => a instanceof CSSTransition && a.playState === 'running'); }
      catch (e) {}
      if (!running.length || pnow() - t0 >= capMs) break;
      const before = running.map((a) => a.currentTime);
      await new Promise((r) => setTimeout(r, 30));
      stalled = running.every((a, i) => a.currentTime === before[i]) ? stalled + 1 : 0;
      if (stalled >= STALLED_POLLS) for (const a of running) { try { a.finish(); } catch (e) {} }
    }
  };

  // ---- chapter text faded or missing in a card that is up (site_check.py, faded_findings)
  // Run 6's agent tied the opacity of each line of a chapter's card to the scroll position (a GSAP scrub): a visitor
  // who stopped in the stretch after the card came up read a card whose lines were part way (0.76, 0.41, 0.05 ...) or
  // not drawn at all (0.01 from 35 % to 64 % of the last chapter), and the nine photographed steps missed it. A blind
  // judge preferred the other site in six of six verdicts, twice, for that card alone. S.fadedText() reads, at every
  // settled position of a visit, each text node of a heading, paragraph or list item in the window (outside header,
  // nav and footer, as chapterText) with its chain of opacities, and its card: the nearest element around its block
  // that paints a background (alpha over CARD_BG), not <body> or <html> and not one that fills the window (a backdrop),
  // which is up at CARD_UP or more of its own chain and in the window. Only text in a card that is up is ever judged:
  // text drawn straight on the scene that fades with the scroll (a title leaving the first screen, the steps of a list
  // dimmed while another is read) is a design, and a card that is up is a promise of text to read.
  // Whether a node is faded is judged against the fullest opacity it is seen at anywhere in the visit (best), which is
  // known only at the end: every read is logged, and S.fadedReport() judges them all at once. A node is faded at a read
  // when its card is up, it is drawn somewhere at TEXT_SHOWN or more (text never drawn fuller than that is dim by
  // design, or never meant to be read: another fault, not named here), and here it is under TEXT_FADE of its best;
  // it is whole at TEXT_WHOLE of its best or more. Identity across reads is the node object itself, so a document
  // replaced by a navigation starts a new log (fadedDoc tells the check).
  const TEXT_FAINT = 0.1, TEXT_FADE = 0.75, TEXT_WHOLE = 0.9, TEXT_SHOWN = 0.5;
  const CARD_BG = 0.1, CARD_UP = 0.9, CARD_MAX = 0.9;
  // A read must be of a page at rest: after a jump a timed entrance may still be playing, a CSS transition (the
  // starter's panels cross-fade in 0.4 s) or a JS tween that sets inline styles every frame (the plugin's guide shows a
  // 0.6 s staggered entrance played once when a card comes up), and S.readBetween waits for CSS transitions only.
  // Where a card that is up holds a line that is not whole at first look, the read looks again, REST_LOOK_MS and one
  // animation frame later (a page that draws once a second would show two equal looks with no frame between them),
  // until two looks agree to REST_SAME, for at most REST_MS; a read still changing then is logged as unsettled and
  // judged nowhere. Where every line of every card that is up is whole, nothing waits. Not told apart from rest: an
  // entrance that holds every line still for longer than REST_LOOK_MS before it starts (a delay of its own); the
  // guide's staggers its lines, so one of them is always moving until the last is whole.
  const REST_MS = 1500, REST_LOOK_MS = 100, REST_SAME = 0.02, REST_FRAME_MS = 500, FADED_READS = 256;
  const fadedSeen = new WeakMap();  // Text -> { group, best }
  const fadedLog = [];
  const fadedDoc = Math.random().toString(36).slice(2);
  const groupOf = (el) => {
    const ch = el.closest('[data-chapter]');
    if (ch) return { kind: 'chapter', name: String(ch.getAttribute('data-chapter')).slice(0, NAME_CHARS) };
    const sec = el.closest('section[id],article[id]');
    if (sec) return { kind: 'section', name: String(sec.id).slice(0, NAME_CHARS) };
    return { kind: 'page', name: '' };
  };
  const fadedLook = () => {
    const W = innerWidth, H = innerHeight, items = [];
    if (!document.body) return items;
    opacityOf.clear();
    const cards = new Map();
    const cardOf = (block) => {
      if (cards.has(block)) return cards.get(block);
      let card = null;
      for (let e = block.parentElement; e && e !== document.body && e !== document.documentElement; e = e.parentElement) {
        if (!(backgroundAlpha(e) > CARD_BG)) continue;
        const r = e.getBoundingClientRect();
        if (r.width * r.height >= CARD_MAX * W * H) break;
        const x0 = Math.max(0, r.left), y0 = Math.max(0, r.top), x1 = Math.min(W, r.right), y1 = Math.min(H, r.bottom);
        const shown = x1 > x0 && y1 > y0 && getComputedStyle(e).visibility === 'visible';
        card = { up: shown && chainOpacity(e) >= CARD_UP, x0, y0, x1, y1 };
        break;
      }
      cards.set(block, card);
      return card;
    };
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const range = document.createRange();
    let seen = 0;
    for (let t = walker.nextNode(); t && seen < TEXT_NODES; t = walker.nextNode()) {
      seen++;
      const chars = t.data.replace(/\s+/g, '').length;
      const el = t.parentElement;
      if (!chars || !el) continue;
      const block = el.closest(TEXT_BLOCKS);
      if (!block || block.closest('header,nav,footer')) continue;
      range.selectNodeContents(t);
      const b = range.getBoundingClientRect();
      if (b.width < 1 || b.height < 1) continue;
      // what of the line is in the window, and inside its card (a card that scrolls its own text shows only part)
      const card = cardOf(block);
      const x0 = Math.max(0, card ? card.x0 : 0), y0 = Math.max(0, card ? card.y0 : 0);
      const x1 = Math.min(W, card ? card.x1 : W), y1 = Math.min(H, card ? card.y1 : H);
      const ix = Math.min(b.right, x1) - Math.max(b.left, x0), iy = Math.min(b.bottom, y1) - Math.max(b.top, y0);
      if (ix <= 0 || iy <= 0) continue;
      const o = getComputedStyle(el).visibility === 'visible' ? chainOpacity(el) : 0;
      let rec = fadedSeen.get(t);
      if (!rec) { rec = { group: groupOf(block), best: 0 }; fadedSeen.set(t, rec); }
      items.push({ rec, o, n: chars * (ix * iy) / (b.width * b.height), up: !!(card && card.up) });
    }
    return items;
  };
  const restless = (items) => items.some((it) => it.up && it.o < 1 - REST_SAME);
  const sameLook = (a, b) => a.length === b.length
    && a.every((it, i) => it.rec === b[i].rec && it.up === b[i].up && Math.abs(it.o - b[i].o) <= REST_SAME);
  S.fadedText = async () => {
    try {
      if (fadedLog.length >= FADED_READS) return { doc: fadedDoc, read: null };
      let items = fadedLook(), settled = true;
      const t0 = pnow();
      if (restless(items)) {
        settled = false;
        while (pnow() - t0 < REST_MS) {
          await new Promise((r) => setTimeout(r, REST_LOOK_MS));
          await late(S.afterFrames(1), REST_FRAME_MS);
          const next = fadedLook();
          const same = sameLook(items, next);
          items = next;
          if (same) { settled = true; break; }
        }
      }
      for (const it of items) if (it.o > it.rec.best) it.rec.best = it.o;
      fadedLog.push({ settled, items: items.map((it) => [it.rec, it.o, it.n, it.up]) });
      return { doc: fadedDoc, read: fadedLog.length - 1, settled, waited_ms: Math.round(pnow() - t0) };
    } catch (e) { return { doc: fadedDoc, read: null, why: String((e && e.message) || e).slice(0, 120) }; }
  };
  // Per read: settled, and for each group with text faded there by the final measure its characters faded or missing
  // (faded), whole (whole), all its text in the window that is drawn or in a card that is up (total), and the lowest
  // opacity of what is faded (low). Characters count by the share of each line's box in the window (as chapterText).
  S.fadedReport = () => {
    const r2 = (v) => Math.round(v * 100) / 100;
    const reads = fadedLog.map(({ settled, items }) => {
      const groups = new Map();
      for (const [rec, o, n, up] of items) {
        if (o < TEXT_FAINT && !up) continue;  // neither drawn nor meant to be read here
        const key = rec.group.kind + ':' + rec.group.name;
        let g = groups.get(key);
        if (!g) { g = { kind: rec.group.kind, group: rec.group.name, faded: 0, whole: 0, total: 0, low: 1 }; groups.set(key, g); }
        g.total += n;
        if (up && rec.best >= TEXT_SHOWN && o < TEXT_FADE * rec.best) { g.faded += n; g.low = Math.min(g.low, o); }
        else if (o >= TEXT_FAINT && o >= TEXT_WHOLE * rec.best) g.whole += n;
      }
      const out = [...groups.values()].filter((g) => g.faded > 0)
        .map((g) => ({ ...g, faded: Math.round(g.faded), whole: Math.round(g.whole), total: Math.round(g.total), low: r2(g.low) }));
      return { settled, groups: out };
    });
    return { doc: fadedDoc, reads };
  };
  // The text pass (site_check.py, _Visitor.text_pass): the chapter text alone at many more positions than the
  // pictures need. Run 6's faded stretches are 2.7 % of the scroll wide in four chapters (5.9 % in the last); the pass
  // between the steps reads every 1/32 at best, and on run 6's page, whose frame takes about a second in software, it
  // read 7 to 9 of its 24 positions in its 20 s. A read of the text needs the page's frames (scroll libraries and
  // tweens advance in them) but not its picture, so while S.quiet is set the WebGL draw calls are not passed on: the
  // page runs all its own code, and only the drawing is left out. Measured on run 6's page at 40 positions: 1.9 s a
  // read drawing, 0.07 s without. Nothing is photographed while it is set. 66 ms after a jump the starter's 0.4 s
  // cross-fade has the new card at a third of its opacity, so a read first ends the CSS transitions that are running
  // (finish(): their end state at once, transitionend fired as it would be) rather than waiting for them as
  // S.readBetween does: what is read is what a visitor who stops here sees once they are over, and the starter's
  // check took 5 s longer with the waits (65 s to 75 s for its checks at 9 and 12 steps). A JS entrance that follows
  // is waited for by S.fadedText.
  S.quiet = false;
  S.quietDraws = (on) => { S.quiet = !!on; return S.quiet; };
  S.readText = async () => {
    await late(S.afterFrames(2), REST_MS);
    try {
      for (const a of document.getAnimations()) {
        if (a instanceof CSSTransition && a.playState === 'running') { try { a.finish(); } catch (e) {} }
      }
    } catch (e) {}
    return S.fadedText();
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

  // ---- a film (site_check.py, film mode): a page with no scroll, stepped beat by beat through window.__story (the
  // story-film contract: ready, mode, title, treatment, scenes, go, play, at, advance, back, setAuto, info). The hook
  // is the page's own code, as window.__site is: it may lack members, throw, never resolve or return anything, so
  // every call is bounded in time and everything it returns is cut and checked here and again in site_check.py.
  const FILM_MEMBERS = ['ready', 'mode', 'title', 'treatment', 'scenes', 'go', 'play', 'at', 'advance', 'back', 'setAuto', 'info'];
  const FILM_CALLS = ['go', 'play', 'at', 'advance', 'back', 'setAuto', 'info'];
  const FILM_SCENES = 64, FILM_BEATS = 200, FILM_LINES = 8, LINE_CHARS = 2000;
  const say = (e) => String((e && e.message) || e).slice(0, 160);
  // When ready resolved, on the real clock (performance.now counts from the navigation's start): watched from the
  // first frame on (tick), so a promise that resolved before the check asked is still timed when it resolved.
  S.filmReady = { of: null, state: 'none', at: null, why: '', promise: null };
  const watchFilm = () => {
    let f;
    try { f = window.__story; } catch (e) { return; }
    if (!f || S.filmReady.of === f) return;
    const r = { of: f, state: 'missing', at: null, why: '', promise: null };
    S.filmReady = r;
    try {
      const ready = f.ready;
      if (ready && typeof ready.then === 'function') {
        r.state = 'pending';
        r.promise = Promise.resolve(ready).then(() => { r.state = 'resolved'; r.at = pnow(); },
                                                (e) => { r.state = 'rejected'; r.at = pnow(); r.why = say(e); });
      }
    } catch (e) { r.why = say(e); }
  };
  S.watchFilm = watchFilm;
  const str = (v, n) => (typeof v === 'string' ? v.slice(0, n) : null);
  S.filmHook = () => {
    watchFilm();
    let f;
    try { f = window.__story; } catch (e) { return { hook: false }; }
    if (!f || (typeof f !== 'object' && typeof f !== 'function')) return { hook: false };
    const missing = [];
    for (const k of FILM_MEMBERS) {
      try {
        const v = f[k];
        const ok = FILM_CALLS.includes(k) ? typeof v === 'function'
          : k === 'ready' ? !!(v && typeof v.then === 'function')
          : k === 'scenes' ? !!(v && typeof v.length === 'number') : typeof v === 'string';
        if (!ok) missing.push(k);
      } catch (e) { missing.push(k); }
    }
    const scenes = [];
    let more = false;
    try {
      const list = f.scenes;
      const n = list && typeof list.length === 'number' ? Math.min(Number(list.length) || 0, FILM_SCENES + 1) : 0;
      for (let i = 0; i < n; i++) {
        if (scenes.length >= FILM_SCENES) { more = true; break; }
        try {
          const s = list[i], id = s && s.id, beats = s && s.beats;
          if ((typeof id !== 'string' && typeof id !== 'number') || !Number.isInteger(beats) || beats < 0 || beats > FILM_BEATS) continue;
          scenes.push({ id: String(id).slice(0, NAME_CHARS), title: str(s.title, 200), beats });
        } catch (e) { /* one bad entry: the others still count */ }
      }
    } catch (e) {}
    let mode = null, why = null, title = null, treatment = null;
    try { mode = str(f.mode, 40); why = str(f.why, 200); title = str(f.title, 200); treatment = str(f.treatment, 80); } catch (e) {}
    const r = S.filmReady;
    return { hook: true, missing, scenes, more, mode, why, title, treatment,
             ready: { state: r.state, at: r.at, why: r.why } };
  };
  // the hook as it is, after waiting at most ms for ready
  S.filmWait = async (ms) => {
    watchFilm();
    const r = S.filmReady;
    if (r.state === 'pending' && r.promise) await late(r.promise, ms);
    return S.filmHook();
  };
  const cleanInfo = (i) => {
    if (!i || typeof i !== 'object') return null;
    try {
      const lines = [];
      if (i.lines && typeof i.lines.length === 'number') {
        for (let k = 0; k < Math.min(i.lines.length, FILM_LINES); k++) if (typeof i.lines[k] === 'string') lines.push(i.lines[k].slice(0, LINE_CHARS));
      }
      return { scene: typeof i.scene === 'string' || typeof i.scene === 'number' ? String(i.scene).slice(0, NAME_CHARS) : null,
               beat: Number.isInteger(i.beat) ? i.beat : null, beats: Number.isInteger(i.beats) ? i.beats : null,
               title: str(i.title, 200), lines, linesGiven: !!(i.lines && typeof i.lines.length === 'number'),
               playing: i.playing === true, cut: str(i.cut, 40), auto: i.auto === true,
               place: str(i.place, 20), notes: str(i.notes, 400) };
    } catch (e) { return null; }
  };
  S.filmInfo = () => {
    try { const f = window.__story; return f && typeof f.info === 'function' ? cleanInfo(f.info()) : null; } catch (e) { return null; }
  };
  // The picture as the hook left it, read in the task its promise resolved in: a canvas without preserveDrawingBuffer
  // is readable only there, and a page whose loop runs on (ambient time) has moved on by its next frame. A buffer
  // already handed to the screen reads as one flat black; then null, and site_check.py photographs the canvas.
  const freshCanvas = (type) => {
    let best = null;
    for (const c of S.canvases) {
      const r = c.getBoundingClientRect();
      const a = Math.max(0, Math.min(innerWidth, r.right) - Math.max(0, r.left)) * Math.max(0, Math.min(innerHeight, r.bottom) - Math.max(0, r.top));
      if (a > 64 && c.width > 0 && c.height > 0 && (!best || a > best.a)) best = { c, a };
    }
    if (!best) return null;
    try {
      const probe = document.createElement('canvas'); probe.width = 16; probe.height = 16;
      const g = probe.getContext('2d'); g.drawImage(best.c, 0, 0, 16, 16);
      const px = g.getImageData(0, 0, 16, 16).data;
      let flat = true;
      for (let k = 0; k < px.length; k += 4) if (px[k] || px[k + 1] || px[k + 2]) { flat = false; break; }
      if (flat) return null;
      return best.c.toDataURL(type || 'image/jpeg', 0.8);
    } catch (e) { return null; }
  };
  // One call of the hook: the frame counts start afresh (S.mark), and the answer is {ok, info, picture, ms} or
  // {why, ms}: 'late' when it did not resolve within ms, else what it threw or rejected with.
  S.filmCall = async (method, args, ms, type) => {
    let f;
    try { f = window.__story; } catch (e) { f = null; }
    if (!f || typeof f[method] !== 'function') return { why: 'missing', ms: 0 };
    S.mark();
    const t0 = pnow();
    let r;
    try { r = await late(Promise.resolve(f[method](...(args || []))), ms); }
    catch (e) { return { why: 'rejected: ' + say(e), ms: pnow() - t0 }; }
    if (r && r.__late) return { why: 'late', ms: pnow() - t0 };
    // type 'both': a PNG beside the JPEG, from the same drawing buffer (a scene's still, site_check.py)
    const picture = type ? freshCanvas(type === 'both' ? 'image/jpeg' : type) : null;
    const png = type === 'both' && picture ? freshCanvas('image/png') : null;
    return { ok: true, info: cleanInfo(r) || S.filmInfo(), picture, png, ms: pnow() - t0 };
  };
  const rendered = (el) => {
    if (!el.getClientRects().length) return false;
    const cs = getComputedStyle(el);
    return cs.visibility === 'visible' && cs.display !== 'none' && chainOpacity(el) >= TEXT_ALPHA;
  };
  const beatElement = (id, beat) => {
    const sec = [...document.querySelectorAll('section[data-scene]')].find((s) => s.getAttribute('data-scene') === String(id));
    if (!sec) return { sec: null, el: null };
    return { sec, el: sec.querySelectorAll('[data-beat]')[beat] || null };
  };
  // The lines of a beat in its HTML: its text without a speaker's note (<aside data-notes>, never a line).
  const lineNodes = (el) => {
    const out = [];
    const w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    for (let t = w.nextNode(); t; t = w.nextNode()) {
      if (!t.data.trim() || !t.parentElement || t.parentElement.closest('aside[data-notes],script,style')) continue;
      out.push(t);
    }
    return out;
  };
  // What the HTML says of the beat the film shows, and whether its lines fit where they are shown: every visible
  // line box inside the window and inside each ancestor that clips (overflow other than visible), none of those
  // scrolling over its height, and a headline over the picture (data-place="over") inside the picture's box.
  S.filmBeat = (id, beat) => {
    try {
      opacityOf.clear();
      const { sec, el } = beatElement(id, beat);
      if (!el) return { section: !!sec, found: false };
      const nodes = lineNodes(el);
      const over = el.getAttribute('data-place') === 'over';
      const out = { section: true, found: true, silent: el.hasAttribute('data-silent'), over,
                    text: nodes.map((t) => t.data).join(' ').replace(/\s+/g, ' ').trim().slice(0, LINE_CHARS),
                    shown: false, boxes: 0, outside: 0, scrolls: null };
      const shownNodes = nodes.filter((t) => rendered(t.parentElement));
      if (!shownNodes.length) return out;
      out.shown = true;
      let area = { l: 0, t: 0, r: innerWidth, b: innerHeight };
      const clip = (b) => { area = { l: Math.max(area.l, b.left), t: Math.max(area.t, b.top), r: Math.min(area.r, b.right), b: Math.min(area.b, b.bottom) }; };
      const boxOf = (e) => e.getBoundingClientRect();
      // up the chain of containing blocks only: a fixed element is clipped by nothing above it, an absolute one only
      // by a positioned ancestor, and the overflow of <body> and <html> belongs to the window (first test film: its
      // body, overflow hidden and 20 px tall above a fixed caption area, would have cut every line)
      const body = document.body, root = document.documentElement;
      for (let e = shownNodes[0].parentElement; e && e !== body && e !== root;) {
        const cs = getComputedStyle(e);
        if (cs.overflowX !== 'visible' || cs.overflowY !== 'visible') {
          clip(boxOf(e));
          if (!out.scrolls && (e.scrollHeight > e.clientHeight + 1 || e.scrollWidth > e.clientWidth + 1)) {
            out.scrolls = { sel: sel(e), height: e.scrollHeight, box: e.clientHeight };
          }
        }
        if (cs.position === 'fixed') break;
        let next = e.parentElement;
        if (cs.position === 'absolute') while (next && next !== body && getComputedStyle(next).position === 'static') next = next.parentElement;
        e = next;
      }
      if (over) {
        const pic = document.getElementById('picture') || refCanvas();
        if (pic) clip(boxOf(pic));
      }
      const range = document.createRange();
      for (const t of shownNodes) {
        range.selectNodeContents(t);
        for (const b of range.getClientRects()) {
          if (b.width < 1 || b.height < 1) continue;
          out.boxes++;
          if (b.left < area.l - 1 || b.right > area.r + 1 || b.top < area.t - 1 || b.bottom > area.b + 1) out.outside++;
        }
      }
      return out;
    } catch (e) { return { found: false, why: say(e) }; }
  };
  // The story as the page without WebGL shows it: how many beats have lines in the HTML, and how many of those are on
  // the page as text a reader can see (rendered, at half opacity or more; below the fold counts, the article scrolls).
  S.filmStory = () => {
    const hook = S.filmHook();
    let withText = 0, shown = 0, chars = 0;
    try {
      opacityOf.clear();
      for (const el of [...document.querySelectorAll('section[data-scene] [data-beat]')].slice(0, FILM_SCENES * FILM_BEATS)) {
        const nodes = lineNodes(el);
        if (!nodes.length) continue;
        withText++;
        const seen = nodes.filter((t) => rendered(t.parentElement));
        if (seen.length === nodes.length) shown++;
        chars += seen.reduce((s, t) => s + t.data.replace(/\s+/g, '').length, 0);
      }
    } catch (e) {}
    return { hook: hook.hook, mode: hook.mode || null, why: hook.why || null, withText, shown, chars };
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
  S.afterFrames = (n) => {
    if (S.layerCanvases) { try { S.layerCanvases(); } catch (e) {} }  // before the frames, so that the screenshot after them has it
    return new Promise((done) => waiting.push({ at: S.frames + n, done }));
  };
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
    watchFilm();
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
  // a draw call: counted, and left out while S.quiet is set (the text pass, which needs the page's frames, not its
  // picture); a draw call returns nothing, so leaving one out changes nothing the page's code can see but the pixels
  const wrapDraw = (proto, name, after) => {
    const f = proto[name];
    if (typeof f !== 'function') return;
    proto[name] = function (...a) { if (S.quiet) return undefined; const r = f.apply(this, a); try { after.call(this, a, r); } catch (e) {} return r; };
  };
  const wrapContext = (proto) => {
    if (!proto) return;
    wrapDraw(proto, 'drawArrays', (a) => draw(a[0], a[2], 1));
    wrapDraw(proto, 'drawElements', (a) => draw(a[0], a[1], 1));
    wrapDraw(proto, 'drawRangeElements', (a) => draw(a[0], a[3], 1));
    wrapDraw(proto, 'drawArraysInstanced', (a) => draw(a[0], a[2], a[3]));
    wrapDraw(proto, 'drawElementsInstanced', (a) => draw(a[0], a[1], a[4]));
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
        wrapDraw(ext, 'drawArraysInstancedANGLE', (b) => draw(b[0], b[2], b[3]));
        wrapDraw(ext, 'drawElementsInstancedANGLE', (b) => draw(b[0], b[1], b[4]));
      } else if (a[0] === 'WEBGL_multi_draw') {
        const sum = (list, off, n, inst, ioff) => { let t = 0; for (let i = 0; i < n; i++) t += tris(b0, list[off + i], inst ? inst[ioff + i] : 1); return t; };
        let b0 = 4;
        wrapDraw(ext, 'multiDrawArraysWEBGL', (b) => { b0 = b[0]; S.cur.draws++; S.cur.tris += sum(b[3], b[4], b[5]); });
        wrapDraw(ext, 'multiDrawElementsWEBGL', (b) => { b0 = b[0]; S.cur.draws++; S.cur.tris += sum(b[1], b[2], b[6]); });
        wrapDraw(ext, 'multiDrawArraysInstancedWEBGL', (b) => { b0 = b[0]; S.cur.draws++; S.cur.tris += sum(b[3], b[4], b[7], b[5], b[6]); });
        wrapDraw(ext, 'multiDrawElementsInstancedWEBGL', (b) => { b0 = b[0]; S.cur.draws++; S.cur.tris += sum(b[1], b[2], b[8], b[6], b[7]); });
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
