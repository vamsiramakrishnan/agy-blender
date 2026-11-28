// print.js: the story's look. Every scene is drawn into a target (colour and depth), and one full-screen pass prints
// it in the story's treatment: the same pass for every scene, so the story looks like one story. During a cut it reads
// two targets and the cut (cuts.js) decides, cell by cell, which one shows. The treatment and its numbers can change
// while the story runs (a change of theme: film.js calls set).
//
// A treatment is { cell(width), samples, cutCell?, soft?, glyphs?, smooth?, grain, glsl }:
// - cell(width): its cell in CSS pixels [w, h] for a picture `width` CSS pixels wide (multiplied by the pixel ratio);
// - samples: target pixels per cell along each side (the target is the grid of cells times this);
// - cutCell: the cut's cell in CSS pixels where the treatment has no cells of its own (grade, poster);
// - soft: true where a fade may mix the two pictures (grade) instead of switching whole cells;
// - glyphs: true to build the glyph atlas;
// - smooth: samples per pixel when the scene is drawn (multisampling), for a treatment that shows the set's edges
//   pixel for pixel;
// - grain: how much grain it has when a look asks for all of it;
// - glsl: `vec3 treat(vec2 px)`, the colour (sRGB, 0..1) of the device pixel px, using the helpers below.
//
// A look is the numbers a treatment prints with: { curve, tone, vignette, grain }. `curve` 1 is the film's tone curve
// (a set drawn by light: its brightness rolled off, and in a cell print 0..1 of it is no ink to all ink); `curve` 0
// prints what is painted (a set in a theme's colours: grade shows each colour exactly, and a cell print puts down as
// much ink as a tone lies from the paper's own tone towards the ink's, whichever of the two is the darker). `tone`
// pulls grade towards the two inks, `vignette` darkens the corners, `grain` is a share of the treatment's own.
import * as THREE from 'three';
import { CUTS, CUT_NAMES } from './cuts.js';

// The look of a story that names its own treatment (a film): what part 1 of this engine always printed with.
export const FILM_LOOK = { curve: 1, tone: 0.22, vignette: 1, grain: 1 };

export const TREATMENTS = {
  // The picture as rendered: through the film's tone curve or exactly as painted, toned towards the two inks, with
  // grain and a vignette as the look asks. For a story whose look is in its sets.
  grade: {
    cell: () => [1, 1], samples: 1, cutCell: 12, soft: true, smooth: 4, grain: 0.05,
    glsl: /* glsl */ `
vec3 treat(vec2 px) {
  Cut c = cutAt(px);
  vec3 col = screenColour(colourAt(c));
  col = mix(col, mix(uPaper, uInk, luma(col)), uTone);
  col = mix(col * vig(px), uPaper * uExact, c.black);
  col += (hash(floor(px / 2.0) + uSeed) - 0.5) * uGrain;
  return clamp(lead(col, c.hot * 0.6), 0.0, 1.0);
}`,
  },
  // Two inks: dots of ink on paper on a grid turned 45 degrees, each dot's area the tone of the picture under it. The
  // cut decides per dot (at its centre), so a cut is made of whole dots. In a theme, what is painted in the accent is
  // printed in a second ink, the accent's own (a duplicator's second drum): the thing the story follows keeps its
  // colour, and everything else is tone.
  halftone: {
    cell: (w) => (w < 700 ? [4.5, 4.5] : [6, 6]), samples: 2, grain: 0.07,
    glsl: /* glsl */ `
vec3 treat(vec2 px) {
  const float A = 0.7853982;
  mat2 turn = mat2(cos(A), sin(A), -sin(A), cos(A)), back = mat2(cos(A), -sin(A), sin(A), cos(A));
  vec2 r = turn * px / uCellPx.x, id = floor(r) + 0.5;
  vec2 centre = back * (id * uCellPx.x);
  Cut c = cutAt(centre);
  // a cell with the accent in it: a dot of the second ink, its size by how lit the accent is and how much of the
  // cell it takes
  float lit, share = accentIn(c, uCellPx, lit), second = step(0.2, share);
  float l = mix(inkOf(colourAt(c)) * vig(centre), secondInk(lit, 0.42, 1.0) * mix(0.5, 1.0, share), second);
  // the grain moves a dot's size; where a tone is the paper's own it stays out, so clean paper stays clean
  l = clamp(max(l, c.hot) + (hash(id + uSeed) - 0.5) * uGrain * mix(1.0, smoothstep(0.0, 0.15, l), uExact), 0.0, 1.0);
  float radius = sqrt(l) * 0.74, aa = 0.75 / uCellPx.x;
  float dot = (1.0 - smoothstep(radius - aa, radius + aa, length(r - id))) * mix(1.0, step(0.004, l), uExact);
  return mix(uPaper, mix(uInk, uSecond, second), dot * (1.0 - c.black));
}`,
  },
  // Text characters: a ramp of glyphs sorted by how much ink each puts on its cell (measured in the page's monospace
  // font when the atlas is built), a cell taking the glyph of its tone, or an edge glyph | / - \ where the depth
  // buffer has an edge (its Laplacian), turned by the depth gradient's direction. What makes it read as a picture
  // and not as a grey texture: glyphs that fill their cell (the font is the cell's width / 0.6), a floor (the paper's
  // tone prints nothing, and the grain scales with the tone so it never lifts an empty cell into a dot), a tone curve
  // that lifts the mid tones so a lit wall reaches the dense glyphs, and a glow of ink behind the densest cells so a
  // lamp or a sky reads as light. In a theme, what is painted in the accent is set in the accent's colour, as a
  // terminal colours a word.
  glyph: {
    cell: (w) => { const cw = w < 520 ? 4.2 : w < 1000 ? 5 : 6; return [cw, cw * 1.72]; }, samples: 2, glyphs: true, grain: 0.05,
    glsl: /* glsl */ `
float tone(float l) {
  l = 1.0 - exp(-l * 2.3);
  l = mix(l, l * l * (3.0 - 2.0 * l), 0.75);
  return clamp((l - 0.025) / 0.975, 0.0, 1.0);
}
// How much ink a colour of the scene prints: its light through the curve above, or its place between the two inks.
float inkAt(vec3 lin) { return mix(tone(luma(lin)), inkOf(lin), uExact); }
vec3 sceneAt(float side, vec2 px) {
  vec2 uv = clamp(px / uRes, 0.0, 1.0);
  return side < 0.5 ? texture2D(tA, uv).rgb : texture2D(tB, uv).rgb;
}
vec3 treat(vec2 px) {
  vec2 cell = floor(px / uCellPx), f = fract(px / uCellPx), w = vec2(uCellPx.x, 0.0), h = vec2(0.0, uCellPx.y);
  Cut c = cutAt((cell + 0.5) * uCellPx);
  float side = step(0.5, c.side);
  // a cell with the accent in it: a character in the second ink, a dense one where the accent is lit
  float lit, share = accentIn(c, uCellPx, lit), second = step(0.2, share);
  float l = mix(inkAt(colourAt(c)), secondInk(lit, 0.35, 0.95) * mix(0.5, 1.0, share), second);
  // the glow: the tone two cells around, for a halo of ink behind a light
  float glow = inkAt((sceneAt(side, c.src - 2.0 * w) + sceneAt(side, c.src + 2.0 * w) + sceneAt(side, c.src - 2.0 * h) + sceneAt(side, c.src + 2.0 * h)) * 0.25);
  l *= mix(1.0, mix(0.5, 1.0, smoothstep(0.92, 0.3, length((cell + 0.5) * uCellPx / uRes - 0.5))), uVignette);
  l = clamp(l + (hash(cell + uSeed) - 0.5) * uGrain * (0.25 + l), 0.0, 1.0);
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
  return mix(uPaper, mix(uInk, uSecond, second), clamp(ink, 0.0, 1.0) * (1.0 - c.black));
}`,
  },
  // Flat inks: every pixel takes one of the two colours of its paint (paintOf below): as painted where it has more
  // than a third of the light, in shade where it has less. So a soft shadow or a rounded form becomes hard-edged
  // shapes of the theme's own colours, and nothing is graded. The scene is drawn at twice the size and four of its
  // pixels are matched separately and averaged: the edge between two flat colours is then smooth, and no third
  // colour appears along it.
  poster: {
    cell: () => [1, 1], samples: 2, cutCell: 12, soft: true, grain: 0.04,
    glsl: /* glsl */ `
vec3 flatAt(vec2 px) {
  Cut c = cutAt(px);
  vec3 lin = colourAt(c);
  float lit;
  int i = paintOf(lin, lit);
  vec3 col = i < 0 ? screenColour(lin) : uPalette[lit > 0.35 ? i : i + 1];
  return lead(mix(col, uPaper, c.black), c.hot * 0.6);
}
vec3 treat(vec2 px) {
  vec3 col = (flatAt(px + vec2(-0.25, -0.25)) + flatAt(px + vec2(0.25, -0.25)) + flatAt(px + vec2(-0.25, 0.25)) + flatAt(px + vec2(0.25, 0.25))) * 0.25;
  return clamp(col + (hash(floor(px / 2.0) + uSeed) - 0.5) * uGrain, 0.0, 1.0);
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

// What every treatment can use: the two pictures, the cut, the look's numbers and a few tone helpers.
const COMMON = /* glsl */ `
uniform sampler2D tA, tB, dA, dB, tAtlas;
uniform vec2 uRes, uCellPx, uCutPx, uSpan;
uniform int uCut, uInks, uAccent;
uniform float uP, uSeed, uSoft, uRamp, uGlyphs, uCurve, uExact, uTone, uVignette, uGrain;
uniform vec3 uPaper, uInk, uSecond, uPalette[16], uFlat[16];
struct Cut { float side; vec2 src; float black; float hot; };
float hash(vec2 p) { p = fract(p * vec2(123.34, 456.21)); p += dot(p, p + 45.32); return fract(p.x * p.y); }
float luma(vec3 c) { return dot(c, vec3(0.2126, 0.7152, 0.0722)); }
// The tone curve: the scene's linear light (it may pass 1 in a beam) rolled off to 0..1, with a little S for contrast.
vec3 curve(vec3 c) { c = 1.0 - exp(-c * 2.2); return mix(c, c * c * (3.0 - 2.0 * c), 0.35); }
// The same for one brightness, made perceptual for the inks (a dot's area, a glyph's weight).
float curveL(float l) { return pow(curve(vec3(l)).r, 0.75); }
// A colour of the scene as the screen's own numbers (the sRGB curve, exactly: a painted paper is the page's paper).
vec3 srgb(vec3 c) { c = clamp(c, 0.0, 1.0); return mix(c * 12.92, 1.055 * pow(c, vec3(1.0 / 2.4)) - 0.055, step(0.0031308, c)); }
// The scene's colour on the screen: through the film's curve, or as painted (the look's curve).
vec3 screenColour(vec3 lin) { return mix(srgb(lin), pow(curve(lin), vec3(1.0 / 2.2)), uCurve); }
// How much ink a colour of the scene prints in a two-ink print, 0..1: uSpan is the tone that prints none and the tone
// that prints all (0 and 1 of the film's light; or the paper's own tone and the ink's, in either order).
float inkOf(vec3 lin) {
  float l = mix(luma(srgb(lin)), curveL(luma(lin)), uCurve);
  return clamp((l - uSpan.x) / (uSpan.y - uSpan.x), 0.0, 1.0);
}
float vignette(vec2 px) { return mix(0.55, 1.0, smoothstep(0.95, 0.35, length(px / uRes - 0.5))); }
float vig(vec2 px) { return mix(1.0, vignette(px), uVignette); }
// The bright edge that leads a cut: added light in a film, a line of the ink where colours are printed as painted.
vec3 lead(vec3 col, float hot) { return mix(col + hot, mix(col, uInk, min(hot * 1.5, 1.0)), uExact); }
// The paint a colour of the scene belongs to. A theme's paints come in pairs, uFlat[2k] as painted and uFlat[2k + 1]
// in shade (both in the scene's linear light; uPalette holds the same for the screen), and whatever the light does to
// a painted surface lies between its two. Returns the pair the colour lies nearest to (its first index; -1 when the
// look has no paints) and sets lit: 0 in shade .. 1 as painted.
int paintOf(vec3 lin, out float lit) {
  int best = -1;
  float least = 1e9;
  lit = 1.0;
  for (int i = 0; i < 16; i += 2) {
    if (i >= uInks) break;
    vec3 a = uFlat[i + 1], ab = uFlat[i] - a;
    float t = clamp(dot(lin - a, ab) / max(dot(ab, ab), 1e-9), 0.0, 1.0);
    vec3 d = lin - (a + ab * t);
    float e = dot(d, d);
    if (e < least) { least = e; best = i; lit = t; }
  }
  return best;
}
// How much of the second ink a cell of the accent's paint takes, by how lit it is: most on the side that is nearer
// the ink's own tone (the lit side on a paper darker than the ink, the shaded side on a lighter one), least on the
// other.
float secondInk(float lit, float least, float most) { return mix(least, most, mix(lit, 1.0 - lit, step(uSpan.y, uSpan.x))); }
`;

// For the prints made of cells: the accent in a cell.
const SECOND = /* glsl */ `
// One pixel of a picture as it was drawn, not mixed with its neighbours: a paint is told by its exact colour.
vec3 drawnAt(float side, vec2 px) {
  ivec2 size = textureSize(tA, 0), at = clamp(ivec2(px / uRes * vec2(size)), ivec2(0), size - 1);
  return side < 0.5 ? texelFetch(tA, at, 0).rgb : texelFetch(tB, at, 0).rgb;
}
// The share of a cell that is painted in the accent, in a look that prints the accent in a second ink (uAccent is
// where its pair begins, or -1: then 0): of four pixels of the picture round the cell's middle, a quarter of span
// each way. A thing smaller than a cell is still found, and still printed in its own ink. Sets lit: how lit they are.
float accentIn(Cut c, vec2 span, out float lit) {
  lit = 1.0;
  if (uAccent < 0) return 0.0;
  float side = step(0.5, c.side), n = 0.0, sum = 0.0, t;
  for (int k = 0; k < 4; k++) {
    vec2 o = vec2(k == 0 || k == 2 ? -0.25 : 0.25, k < 2 ? -0.25 : 0.25) * span;
    if (paintOf(drawnAt(side, c.src + o), t) == uAccent) { n += 1.0; sum += t; }
  }
  if (n > 0.0) lit = sum / n;
  return n * 0.25;
}
`;

function shader(treatment) {
  const cuts = CUT_NAMES.map((n) => CUTS[n].glsl).join('\n');
  const pick = CUT_NAMES.map((n, i) => `if (uCut == ${i}) c = cut_${n}(q, grid, uP);`).join(' else ');
  return /* glsl */ `precision highp float;
${COMMON}
${SECOND}
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
function srgb(hex, into = new THREE.Vector3()) {
  const n = parseInt(hex.replace('#', ''), 16);
  return into.set((n >> 16 & 255) / 255, (n >> 8 & 255) / 255, (n & 255) / 255);
}
const lumaOf = (v) => 0.2126 * v.x + 0.7152 * v.y + 0.0722 * v.z;
// One channel of a screen colour as the scene's linear light (the sRGB curve, backwards).
const linear = (v) => (v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4));

// `smooth` false leaves multisampling out whatever the treatment asks (film.js: a machine that draws in software).
export function createPrint(renderer, { treatment, look = FILM_LOOK, paper, ink, palette = [], accent = -1, smooth = true }) {
  // How many samples a pixel a scene is drawn with: what the treatment asks for, where this machine can (a half-float
  // target cannot be multisampled everywhere) and, past three megapixels, two at most (the pixels are then fine
  // enough, and four samples of such a picture are over a hundred megabytes).
  const gl = renderer.getContext();
  const counts = gl.getInternalformatParameter ? gl.getInternalformatParameter(gl.RENDERBUFFER, gl.RGBA16F, gl.SAMPLES) : null;
  const most = counts && counts.length ? Math.max(...counts) : 0;
  const wanted = (pixels) => (smooth ? Math.min(treatment.smooth || 0, most, pixels > 3e6 ? 2 : 4) : 0);
  // A scene's target: half-float colour, so a beam or a lamp may be brighter than 1 and the tone curve rolls it off,
  // and a depth texture for edges. The second is only drawn into during a cut, and takes its size then.
  const target = (samples) => new THREE.WebGLRenderTarget(2, 2, { type: THREE.HalfFloatType, depthTexture: new THREE.DepthTexture(2, 2), samples });
  let samples = 0, targets = [target(0), target(0)], tw = 2, th = 2;
  const uniforms = {
    tA: { value: targets[0].texture }, tB: { value: targets[1].texture },
    dA: { value: targets[0].depthTexture }, dB: { value: targets[1].depthTexture },
    tAtlas: { value: null }, uRes: { value: new THREE.Vector2(1, 1) },
    uCellPx: { value: new THREE.Vector2(1, 1) }, uCutPx: { value: new THREE.Vector2(1, 1) },
    uCut: { value: -1 }, uP: { value: 0 }, uSeed: { value: 0 }, uSoft: { value: 0 },
    uRamp: { value: 1 }, uGlyphs: { value: 1 }, uPaper: { value: new THREE.Vector3() }, uInk: { value: new THREE.Vector3() },
    uCurve: { value: 1 }, uExact: { value: 0 }, uTone: { value: 0 }, uVignette: { value: 0 }, uGrain: { value: 0 },
    uSpan: { value: new THREE.Vector2(0, 1) }, uInks: { value: 0 }, uAccent: { value: -1 }, uSecond: { value: new THREE.Vector3() },
    uPalette: { value: Array.from({ length: 16 }, () => new THREE.Vector3()) },
    uFlat: { value: Array.from({ length: 16 }, () => new THREE.Vector3()) },
  };
  // One program per treatment, made the first time the treatment is used; they share the uniforms.
  const materials = new Map();
  const materialOf = (t) => {
    if (!materials.has(t)) {
      materials.set(t, new THREE.ShaderMaterial({
        uniforms, depthTest: false, depthWrite: false, fragmentShader: shader(t),
        vertexShader: 'void main() { gl_Position = vec4(position.xy, 0.0, 1.0); }',
      }));
    }
    return materials.get(t);
  };
  const quad = new THREE.Mesh(new THREE.PlaneGeometry(2, 2), materialOf(treatment));
  quad.frustumCulled = false;
  const post = new THREE.Scene(), postCamera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 1);
  post.add(quad);
  let atlas = null, cellKey = '', width = 1, height = 1;

  const print = {
    aspect: 1,
    shots: 0, // how many times a scene has been drawn into a target (film.js counts them per frame)
    // Sizes everything for a picture of w x h CSS pixels: the canvas at its pixel ratio (at most 2), the cells, the
    // targets (cells times samples), and the atlas when the cell changed.
    resize(w, h) {
      width = w; height = h;
      renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
      renderer.setSize(w, h, false);
      const res = renderer.getDrawingBufferSize(new THREE.Vector2()), pr = renderer.getPixelRatio();
      const [cw, ch] = treatment.cell(w).map((v) => Math.max(1, Math.round(v * pr)));
      const cut = treatment.cutCell ? Math.round(treatment.cutCell * pr) : 0;
      uniforms.uRes.value.copy(res);
      uniforms.uCellPx.value.set(cw, ch);
      uniforms.uCutPx.value.set(cut || cw, cut || ch);
      tw = Math.ceil(res.x / cw) * treatment.samples;
      th = Math.ceil(res.y / ch) * treatment.samples;
      if (wanted(tw * th) !== samples) {
        for (const t of targets) { t.depthTexture.dispose(); t.dispose(); }
        samples = wanted(tw * th);
        targets = [target(samples), target(samples)];
        uniforms.tA.value = targets[0].texture; uniforms.tB.value = targets[1].texture;
        uniforms.dA.value = targets[0].depthTexture; uniforms.dB.value = targets[1].depthTexture;
      }
      targets[0].setSize(tw, th);
      if (treatment.glyphs && cellKey !== cw + 'x' + ch) {
        atlas?.texture.dispose();
        atlas = glyphAtlas(cw, ch);
        uniforms.tAtlas.value = atlas.texture;
        uniforms.uRamp.value = atlas.ramp;
        uniforms.uGlyphs.value = atlas.total;
        cellKey = cw + 'x' + ch;
      }
      print.aspect = w / h;
    },
    // The look, now or after a change of theme: a treatment, its numbers, the two inks, and the theme's paints
    // (`palette`: pairs of colours, each paint as painted and then in shade, eight pairs at most; `accent`: where the
    // accent's pair begins in it, or -1 for a look with no second ink). What is left out stays as it was.
    set({ treatment: next = treatment, look: l = look, paper: p = paper, ink: i = ink, palette: flat = palette, accent: second = accent }) {
      look = l; paper = p; ink = i; palette = flat; accent = second;
      const exact = l.curve < 0.5;
      srgb(p, uniforms.uPaper.value);
      srgb(i, uniforms.uInk.value);
      uniforms.uCurve.value = l.curve;
      uniforms.uExact.value = exact ? 1 : 0;
      uniforms.uTone.value = l.tone;
      uniforms.uVignette.value = l.vignette;
      uniforms.uGrain.value = l.grain * next.grain;
      // what prints no ink and what prints all: the paper's tone and the ink's, when they are far enough apart to tell
      const lp = lumaOf(uniforms.uPaper.value), li = lumaOf(uniforms.uInk.value);
      if (exact && Math.abs(li - lp) > 0.05) uniforms.uSpan.value.set(lp, li); else uniforms.uSpan.value.set(0, 1);
      uniforms.uInks.value = Math.min(flat.length, 16);
      flat.slice(0, 16).forEach((c, k) => {
        const s = srgb(c, uniforms.uPalette.value[k]);
        uniforms.uFlat.value[k].set(linear(s.x), linear(s.y), linear(s.z));
      });
      uniforms.uAccent.value = second >= 0 && second + 1 < uniforms.uInks.value ? second : -1;
      if (uniforms.uAccent.value >= 0) uniforms.uSecond.value.copy(uniforms.uPalette.value[second]);
      uniforms.uSoft.value = next.soft ? 1 : 0;
      if (next !== treatment) {
        treatment = next;
        quad.material = materialOf(next);
        print.resize(width, height);
      }
    },
    // Draws a scene into target 0 (the picture, or the leaving one in a cut) or 1 (the arriving one).
    shoot(which, scene, camera) {
      if (targets[which].width !== tw || targets[which].height !== th) targets[which].setSize(tw, th);
      renderer.setRenderTarget(targets[which]);
      renderer.render(scene, camera);
      renderer.setRenderTarget(null);
      print.shots++;
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
  print.set({});
  return print;
}
