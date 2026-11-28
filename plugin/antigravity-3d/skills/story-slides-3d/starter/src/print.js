// print.js: the film's look. Every scene is drawn into a target (colour and depth), and one full-screen pass prints
// it in the film's treatment: the same pass for every scene, so the film looks like one film. During a cut it reads
// two targets and the cut (cuts.js) decides, cell by cell, which one shows.
//
// A treatment is { cell(width), samples, cutCell?, soft?, glyphs?, glsl }:
// - cell(width): its cell in CSS pixels [w, h] for a picture `width` CSS pixels wide (multiplied by the pixel ratio);
// - samples: target pixels per cell along each side (the target is the grid of cells times this);
// - cutCell: the cut's cell in CSS pixels where the treatment has no cells of its own (grade);
// - soft: true where a fade may mix the two pictures (grade) instead of switching whole cells;
// - glyphs: true to build the glyph atlas;
// - glsl: `vec3 treat(vec2 px)`, the colour (sRGB, 0..1) of the device pixel px, using the helpers below.
import * as THREE from 'three';
import { CUTS, CUT_NAMES } from './cuts.js';

export const TREATMENTS = {
  // The picture as rendered, tone-curved, toned towards the film's two inks, with grain and a vignette. For a film
  // whose look is in its sets.
  grade: {
    cell: () => [1, 1], samples: 1, cutCell: 12, soft: true,
    glsl: /* glsl */ `
vec3 treat(vec2 px) {
  Cut c = cutAt(px);
  vec3 col = pow(curve(colourAt(c)), vec3(1.0 / 2.2));
  col = mix(col, mix(uPaper, uInk, luma(col)), 0.22);
  col *= vignette(px) * (1.0 - c.black);
  col += (hash(floor(px / 2.0) + uSeed) - 0.5) * 0.05 + c.hot * 0.6;
  return clamp(col, 0.0, 1.0);
}`,
  },
  // Two inks: dots of ink on paper on a grid turned 45 degrees, each dot's area the brightness of the picture under
  // it. The cut decides per dot (at its centre), so a cut is made of whole dots.
  halftone: {
    cell: (w) => (w < 700 ? [4.5, 4.5] : [6, 6]), samples: 2,
    glsl: /* glsl */ `
vec3 treat(vec2 px) {
  const float A = 0.7853982;
  mat2 turn = mat2(cos(A), sin(A), -sin(A), cos(A)), back = mat2(cos(A), -sin(A), sin(A), cos(A));
  vec2 r = turn * px / uCellPx.x, id = floor(r) + 0.5;
  vec2 centre = back * (id * uCellPx.x);
  Cut c = cutAt(centre);
  float l = curveL(luma(colourAt(c))) * vignette(centre);
  l = clamp(max(l, c.hot) + (hash(id + uSeed) - 0.5) * 0.07, 0.0, 1.0);
  float radius = sqrt(l) * 0.74, aa = 0.75 / uCellPx.x;
  float dot = 1.0 - smoothstep(radius - aa, radius + aa, length(r - id));
  return mix(uPaper, uInk, dot * (1.0 - c.black));
}`,
  },
  // Text characters: a ramp of glyphs sorted by how much ink each puts on its cell (measured in the page's monospace
  // font when the atlas is built), a cell taking the glyph of its brightness, or an edge glyph | / - \ where the depth
  // buffer has an edge (its Laplacian), turned by the depth gradient's direction. What makes it read as a picture
  // and not as a grey texture: glyphs that fill their cell (the font is the cell's width / 0.6), a black floor (dark
  // prints nothing, and the grain scales with the brightness so it never lifts a dark cell into a dot), a tone curve
  // that lifts the mid tones so a lit wall reaches the dense glyphs, and a glow of ink behind bright cells so a lamp or
  // a sky reads as light.
  glyph: {
    cell: (w) => { const cw = w < 520 ? 4.2 : w < 1000 ? 5 : 6; return [cw, cw * 1.72]; }, samples: 2, glyphs: true,
    glsl: /* glsl */ `
float tone(float l) {
  l = 1.0 - exp(-l * 2.3);
  l = mix(l, l * l * (3.0 - 2.0 * l), 0.75);
  return clamp((l - 0.025) / 0.975, 0.0, 1.0);
}
float lumAt(float side, vec2 px) {
  vec2 uv = clamp(px / uRes, 0.0, 1.0);
  return luma(side < 0.5 ? texture2D(tA, uv).rgb : texture2D(tB, uv).rgb);
}
vec3 treat(vec2 px) {
  vec2 cell = floor(px / uCellPx), f = fract(px / uCellPx), w = vec2(uCellPx.x, 0.0), h = vec2(0.0, uCellPx.y);
  Cut c = cutAt((cell + 0.5) * uCellPx);
  float side = step(0.5, c.side);
  float l = tone(luma(colourAt(c)));
  // the glow: the brightness two cells around, for a halo of ink behind a light
  float glow = tone((lumAt(side, c.src - 2.0 * w) + lumAt(side, c.src + 2.0 * w) + lumAt(side, c.src - 2.0 * h) + lumAt(side, c.src + 2.0 * h)) * 0.25);
  l *= mix(0.5, 1.0, smoothstep(0.92, 0.3, length((cell + 0.5) * uCellPx / uRes - 0.5)));
  l = clamp(l + (hash(cell + uSeed) - 0.5) * 0.05 * (0.25 + l), 0.0, 1.0);
  l = max(l, c.hot);
  float qc = nearness(side, c.src), ql = nearness(side, c.src - w), qr = nearness(side, c.src + w);
  float qd = nearness(side, c.src - h), qu = nearness(side, c.src + h);
  float lap = (4.0 * qc - ql - qr - qu - qd) / (max(max(max(ql, qr), max(qu, qd)), qc) + 1e-5);
  float idx = l < 0.06 ? 0.0 : floor(l * (uRamp - 1.0) + 0.5), bright = mix(0.5, 1.0, l);
  if (lap > 0.09 && l < 0.72 && c.hot == 0.0) {
    float k = mod(floor((atan(qu - qd, qr - ql) / 3.14159265 + 1.0) * 4.0 + 0.5), 4.0);
    idx = uRamp + (k < 0.5 ? 0.0 : k < 1.5 ? 3.0 : k < 2.5 ? 2.0 : 1.0);
    bright = 0.36 + 0.85 * max(l, glow * 0.7);
  }
  float a = texture2D(tAtlas, vec2((idx + f.x) / uGlyphs, f.y)).r;
  float ink = a * clamp(bright, 0.0, 1.0) + l * 0.045 + glow * 0.07 + smoothstep(0.9, 1.0, l) * 0.3;
  return mix(uPaper, uInk, clamp(ink, 0.0, 1.0) * (1.0 - c.black));
}`,
  },
};

// The candidates for the glyph ramp, and the four edge glyphs (in the order the shader numbers them).
const CANDIDATES = " .`'-,:;_~\"^!()[]{}<>+=*?7ltrjicvxzunoaeskyhdbpqgmw0OQ#%&@$MWNB8XKRAEHSZ";
const EDGES = ['|', '/', '-', '\\'];
const FONT = 'ui-monospace, "SF Mono", Menlo, Consolas, "DejaVu Sans Mono", monospace';

// The atlas: one row of cells, the ramp (from no ink to most, about 22 steps, each the candidate whose measured ink is
// nearest that step) then the edges. Measured, not listed, because a glyph's weight differs from font to font.
function glyphAtlas(cw, ch) {
  const font = `500 ${cw / 0.6}px ${FONT}`, base = Math.round(ch * 0.74); // a glyph as wide as its cell
  const m = document.createElement('canvas');
  m.width = cw; m.height = ch;
  const mx = m.getContext('2d', { willReadFrequently: true });
  const inked = [...new Set(CANDIDATES)].map((g) => {
    mx.fillStyle = '#000'; mx.fillRect(0, 0, cw, ch);
    mx.fillStyle = '#fff'; mx.font = font; mx.textAlign = 'center';
    mx.fillText(g, cw / 2, base);
    let sum = 0;
    for (const v of mx.getImageData(0, 0, cw, ch).data.filter((_, i) => i % 4 === 0)) sum += v;
    return [g, sum];
  }).sort((a, b) => a[1] - b[1]);
  const most = inked[inked.length - 1][1], ramp = [];
  for (let j = 0; j < 22; j++) {
    const want = (most * j) / 21;
    const best = inked.reduce((a, b) => (Math.abs(b[1] - want) < Math.abs(a[1] - want) ? b : a))[0];
    if (ramp[ramp.length - 1] !== best) ramp.push(best);
  }
  const glyphs = ramp.concat(EDGES);
  const a = document.createElement('canvas');
  a.width = glyphs.length * cw; a.height = ch;
  const ax = a.getContext('2d');
  ax.fillStyle = '#000'; ax.fillRect(0, 0, a.width, a.height);
  ax.fillStyle = '#fff'; ax.font = font; ax.textAlign = 'center';
  glyphs.forEach((g, i) => ax.fillText(g, i * cw + cw / 2, base));
  const texture = new THREE.CanvasTexture(a);
  texture.minFilter = texture.magFilter = THREE.NearestFilter;
  texture.generateMipmaps = false;
  return { texture, ramp: ramp.length, total: glyphs.length };
}

// What every treatment can use: the two pictures, the cut, and a few tone helpers.
const COMMON = /* glsl */ `
uniform sampler2D tA, tB, dA, dB, tAtlas;
uniform vec2 uRes, uCellPx, uCutPx;
uniform int uCut;
uniform float uP, uSeed, uSoft, uRamp, uGlyphs;
uniform vec3 uPaper, uInk;
struct Cut { float side; vec2 src; float black; float hot; };
float hash(vec2 p) { p = fract(p * vec2(123.34, 456.21)); p += dot(p, p + 45.32); return fract(p.x * p.y); }
float luma(vec3 c) { return dot(c, vec3(0.2126, 0.7152, 0.0722)); }
// The tone curve: the scene's linear light (it may pass 1 in a beam) rolled off to 0..1, with a little S for contrast.
vec3 curve(vec3 c) { c = 1.0 - exp(-c * 2.2); return mix(c, c * c * (3.0 - 2.0 * c), 0.35); }
// The same for one brightness, made perceptual for the inks (a dot's area, a glyph's weight).
float curveL(float l) { return pow(curve(vec3(l)).r, 0.75); }
float vignette(vec2 px) { return mix(0.55, 1.0, smoothstep(0.95, 0.35, length(px / uRes - 0.5))); }
`;

function shader(treatment) {
  const cuts = CUT_NAMES.map((n) => CUTS[n].glsl).join('\n');
  const pick = CUT_NAMES.map((n, i) => `if (uCut == ${i}) c = cut_${n}(q, grid, uP);`).join(' else ');
  return /* glsl */ `precision highp float;
${COMMON}
${cuts}
// The cut at a device pixel: which picture, from where. Exactly the leaving picture at p = 0 and the arriving one at
// p = 1, whatever the cut computes there, so a cut's ends are the scenes' own pictures.
Cut cutAt(vec2 px) {
  if (uCut < 0 || uP <= 0.0) return Cut(0.0, px, 0.0, 0.0);
  if (uP >= 1.0) return Cut(1.0, px, 0.0, 0.0);
  vec2 q = px / uCutPx, grid = uRes / uCutPx;
  Cut c = Cut(0.0, q, 0.0, 0.0);
  ${pick}
  c.src *= uCutPx;
  return c;
}
vec3 colourAt(Cut c) {
  vec2 uv = clamp(c.src / uRes, 0.0, 1.0);
  if (c.side <= 0.0) return texture2D(tA, uv).rgb;
  if (c.side >= 1.0) return texture2D(tB, uv).rgb;
  return mix(texture2D(tA, uv).rgb, texture2D(tB, uv).rgb, c.side);
}
// 1 near the camera, 0 at the far plane or the sky: what the glyph treatment's edges are found in.
float nearness(float side, vec2 px) {
  vec2 uv = clamp(px / uRes, 0.0, 1.0);
  return 1.0 - (side < 0.5 ? texture2D(dA, uv).r : texture2D(dB, uv).r);
}
${treatment.glsl}
void main() { gl_FragColor = vec4(treat(gl_FragCoord.xy), 1.0); }`;
}

// A colour as the print writes it: sRGB 0..1, straight to the canvas (the print does its own tone and gamma).
function srgb(hex) {
  const n = parseInt(hex.replace('#', ''), 16);
  return new THREE.Vector3((n >> 16 & 255) / 255, (n >> 8 & 255) / 255, (n & 255) / 255);
}

// A scene's target: half-float colour, so a beam or a lamp may be brighter than 1 and the tone curve rolls it off,
// and a depth texture for edges.
function target() {
  return new THREE.WebGLRenderTarget(2, 2, { type: THREE.HalfFloatType, depthTexture: new THREE.DepthTexture(2, 2) });
}

export function createPrint(renderer, { treatment, paper, ink }) {
  const targets = [target(), target()];
  const uniforms = {
    tA: { value: targets[0].texture }, tB: { value: targets[1].texture },
    dA: { value: targets[0].depthTexture }, dB: { value: targets[1].depthTexture },
    tAtlas: { value: null }, uRes: { value: new THREE.Vector2(1, 1) },
    uCellPx: { value: new THREE.Vector2(1, 1) }, uCutPx: { value: new THREE.Vector2(1, 1) },
    uCut: { value: -1 }, uP: { value: 0 }, uSeed: { value: 0 }, uSoft: { value: treatment.soft ? 1 : 0 },
    uRamp: { value: 1 }, uGlyphs: { value: 1 }, uPaper: { value: srgb(paper) }, uInk: { value: srgb(ink) },
  };
  const material = new THREE.ShaderMaterial({
    uniforms, depthTest: false, depthWrite: false, fragmentShader: shader(treatment),
    vertexShader: 'void main() { gl_Position = vec4(position.xy, 0.0, 1.0); }',
  });
  const quad = new THREE.Mesh(new THREE.PlaneGeometry(2, 2), material);
  quad.frustumCulled = false;
  const post = new THREE.Scene(), postCamera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 1);
  post.add(quad);
  let atlas = null, cellKey = '';

  return {
    aspect: 1,
    // Sizes everything for a picture of w x h CSS pixels: the canvas at its pixel ratio (at most 2), the cells, the
    // targets (cells times samples), and the atlas when the cell changed.
    resize(w, h) {
      renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
      renderer.setSize(w, h, false);
      const res = renderer.getDrawingBufferSize(new THREE.Vector2()), pr = renderer.getPixelRatio();
      const [cw, ch] = treatment.cell(w).map((v) => Math.max(1, Math.round(v * pr)));
      const cut = treatment.cutCell ? Math.round(treatment.cutCell * pr) : 0;
      uniforms.uRes.value.copy(res);
      uniforms.uCellPx.value.set(cw, ch);
      uniforms.uCutPx.value.set(cut || cw, cut || ch);
      const tw = Math.ceil(res.x / cw) * treatment.samples, th = Math.ceil(res.y / ch) * treatment.samples;
      for (const t of targets) t.setSize(tw, th);
      if (treatment.glyphs && cellKey !== cw + 'x' + ch) {
        atlas?.texture.dispose();
        atlas = glyphAtlas(cw, ch);
        uniforms.tAtlas.value = atlas.texture;
        uniforms.uRamp.value = atlas.ramp;
        uniforms.uGlyphs.value = atlas.total;
      }
      cellKey = cw + 'x' + ch;
      this.aspect = w / h;
    },
    // Draws a scene into target 0 (the picture, or the leaving one in a cut) or 1 (the arriving one).
    shoot(which, scene, camera) {
      renderer.setRenderTarget(targets[which]);
      renderer.render(scene, camera);
      renderer.setRenderTarget(null);
    },
    // Prints: cut index (-1 for none) at progress p, with the grain's seed, to the canvas or to `out`.
    show(cut, p, seed, out = null) {
      uniforms.uCut.value = cut;
      uniforms.uP.value = p;
      uniforms.uSeed.value = seed;
      renderer.setRenderTarget(out);
      renderer.render(post, postCamera);
      renderer.setRenderTarget(null);
    },
  };
}
