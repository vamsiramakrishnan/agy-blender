// labels.js: plain DOM labels in #labels, placed by projecting a world point each frame.
// Each label is <div class="lbl" data-kind="mono|tag|turn|gate">. Invisible labels get visibility:hidden (no layout).
import * as THREE from '../../vendor/three.module.min.js';

export function createLabels(root) {
  const items = new Map();
  const v = new THREE.Vector3();
  let W = 1, H = 1, layout = 'wide';
  const get = (id, text, kind) => {
    let it = items.get(id);
    if (!it) {
      const el = document.createElement('div');
      el.className = 'lbl'; el.dataset.kind = kind; el.textContent = text;
      el.style.visibility = 'hidden'; el.style.willChange = 'transform, opacity';
      root.appendChild(el);
      it = { el, shown: false, text, x: NaN, y: NaN, o: -1 };
      items.set(id, it);
    }
    if (it.text !== text) { it.el.textContent = text; it.text = text; }
    return it;
  };
  let seen = new Set();
  return {
    resize(w, h, l) { W = w; H = h; layout = l; },
    begin() { seen = new Set(); },
    // Place label `id` at world point `at` with opacity `o`; align: 'right' of the point (default), 'center', 'left'.
    put(camera, id, text, kind, at, o, align = 'right', caps = false) {
      const it = get(id, text, kind);
      if (caps && !it.caps) { it.el.style.fontVariantCaps = 'all-small-caps'; it.el.style.letterSpacing = '0.04em'; it.caps = true; }
      seen.add(id);
      if (o > 0.01) {
        v.set(at[0], at[1], at[2]).project(camera);
        if (v.z > 1 || v.z < -1) o = 0;
        let x = (v.x * 0.5 + 0.5) * W, y = (-v.y * 0.5 + 0.5) * H;
        // Keep clear of the text panel: the left third on wide, the bottom 45 % on narrow.
        if (layout === 'wide') o *= Math.min(1, Math.max(0, (x - W * 0.36) / (W * 0.04)));
        else o *= Math.min(1, Math.max(0, (H * 0.53 - y) / (H * 0.04)));
        if (x < 4 || x > W - 4 || y < 40 || y > H - 8) o = 0;
        if (o > 0.01) {
          if (align === 'center') x = `calc(${x.toFixed(1)}px - 50%)`; else if (align === 'above') x = `calc(${x.toFixed(1)}px - 50%)`; else if (align === 'left') x = `calc(${x.toFixed(1)}px - 100% - 8px)`; else x = `${(x + 8).toFixed(1)}px`;
          const tr = `translate(${x}, ${(align === 'above' ? y - 18 : y - 8).toFixed(1)}px)`;
          if (tr !== it.tr) { it.el.style.transform = tr; it.tr = tr; }
        }
      }
      const shown = o > 0.01;
      if (shown !== it.shown) { it.el.style.visibility = shown ? 'visible' : 'hidden'; it.shown = shown; }
      const oo = Math.round(o * 100) / 100;
      if (shown && oo !== it.o) { it.el.style.opacity = String(oo); it.o = oo; }
    },
    end() {
      for (const [id, it] of items) if (!seen.has(id) && it.shown) { it.el.style.visibility = 'hidden'; it.shown = false; }
    },
    count() { let n = 0; for (const it of items.values()) if (it.shown) n++; return n; },
    dispose() { for (const it of items.values()) it.el.remove(); items.clear(); },
  };
}
