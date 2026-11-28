// theme.js: the stage's half of a theme. The page's half is src/theme.css: the same names as custom properties
// (--paper, --ink, --accent ...), which style the slides. A theme's colours are read from there at run time, so the
// words and the picture are painted from one palette; what only the stage needs (how the picture is printed, where the
// light stands, how soft its shadows are) is the numbers below. kit.paint and kit.rig follow the current theme.

// The colours a theme names. `paper`, `paper-2`: the page, and the stage's sky and ground. `ink`, `muted`: text, and
// lines on the stage. `accent`, `accent-2`: the thing the story follows, and a second emphasis. `lit`, `shade`: what
// sets are painted with, where the light falls on them and where it does not.
export const TOKENS = ['paper', 'paper-2', 'ink', 'muted', 'accent', 'accent-2', 'lit', 'shade'];

// One entry per theme.
// - colours: the theme's colours as src/theme.css has them (a repository test holds the two together). The CSS is
//   what is read; these are used whole when the page's CSS has no theme (see readTheme), and one by one for a token
//   it leaves out.
// - print: the treatment (print.js) and how it treats the picture. `curve` 0 prints a colour exactly as it is painted,
//   so the sky is the page's own paper; 1 is the film's tone curve. `tone` pulls the picture towards the two inks,
//   `vignette` darkens the corners, `grain` is a share of the treatment's own grain.
// - key: the light. `from` is the direction towards it (a world may fix its own: kit.rig's `from`), `intensity` 1
//   shows a surface that faces it squarely in exactly its paint, `softness` is the blur of its shadows' edges.
// - fill: how much light reaches what the key does not: 1 gives a `lit` surface in shadow exactly the `shade` colour.
// - fog: how much of the ground's colour the air has taken at the rig's reach (0: clear air, a hard horizon).
// - roughness: of a paint that names none.
// - background: 'gradient' (paper overhead, towards paper-2 at the horizon) or 'flat' (paper).
// - line: how many times thicker a drawn line is (a frame's bars, a ruler): a line thinner than a cell and a half
//   does not survive a cell print.
export const THEMES = {
  // Daylight on a cool off-white: a model on a table by a window. Soft shadows, a little tooth.
  paper: {
    colours: { paper: '#eef1f5', 'paper-2': '#d2dce9', ink: '#11141a', muted: '#566070', accent: '#1f44ff', 'accent-2': '#ff5a1f', lit: '#fbfcfe', shade: '#97a6ba' },
    print: { treatment: 'grade', curve: 0, tone: 0, vignette: 0, grain: 0.3 },
    key: { from: [-0.5, 0.8, 0.35], intensity: 1, softness: 2.5 },
    fill: 1, fog: 1, roughness: 0.9, background: 'gradient', line: 1,
  },
  // Night: deep blue-black paper, warm light. The corners fall off and the grain shows, as in the film.
  ink: {
    colours: { paper: '#0b0f17', 'paper-2': '#171f2e', ink: '#efe4cb', muted: '#9c978b', accent: '#ff8a3d', 'accent-2': '#8fb8ff', lit: '#d6c9ae', shade: '#252c3d' },
    print: { treatment: 'grade', curve: 0, tone: 0.1, vignette: 0.7, grain: 1 },
    key: { from: [-0.75, 0.45, 0.25], intensity: 1.1, softness: 2 },
    fill: 1.3, fog: 1, roughness: 0.8, background: 'gradient', line: 1,
  },
  // Flat fields of a few saturated colours: every pixel is one of the theme's inks. Hard light, no grain, no air.
  poster: {
    colours: { paper: '#ff7a38', 'paper-2': '#ff9a62', ink: '#0b1138', muted: '#3b1a3c', accent: '#1c36e0', 'accent-2': '#fff1d6', lit: '#fff1d6', shade: '#0b1138' },
    print: { treatment: 'poster', curve: 0, tone: 0, vignette: 0, grain: 0 },
    key: { from: [-0.6, 0.6, 0.5], intensity: 1, softness: 0 },
    fill: 1, fog: 0, roughness: 1, background: 'flat', line: 1.2,
  },
  // Two inks printed as dots: tone only, so the light is given more contrast than paint alone would have.
  halftone: {
    colours: { paper: '#dfeadd', 'paper-2': '#c3d6c4', ink: '#103b2c', muted: '#365e4e', accent: '#ff4f79', 'accent-2': '#103b2c', lit: '#f1f7ee', shade: '#2c5d4b' },
    print: { treatment: 'halftone', curve: 0, tone: 0, vignette: 0, grain: 0.5 },
    key: { from: [-0.55, 0.65, 0.5], intensity: 1, softness: 1 },
    fill: 0.8, fog: 0.6, roughness: 0.9, background: 'gradient', line: 2.2,
  },
  // Text characters on a dark ground. A character is there or it is not, so the fill is high (a surface in shade
  // still prints a few, and is told from the empty sky) and the air is nearly clear.
  glyph: {
    colours: { paper: '#0d2647', 'paper-2': '#143660', ink: '#e6f0ff', muted: '#9bb4d6', accent: '#ffd166', 'accent-2': '#ff8f70', lit: '#e6f0ff', shade: '#3f6fb0' },
    print: { treatment: 'glyph', curve: 0, tone: 0, vignette: 0, grain: 0.5 },
    key: { from: [-0.7, 0.5, 0.4], intensity: 1, softness: 0.5 },
    fill: 1.3, fog: 0.4, roughness: 0.9, background: 'flat', line: 2,
  },
};
export const THEME_NAMES = Object.keys(THEMES);
// The theme of a page that names none, or one that is not here: theme.css gives :root this one.
export const DEFAULT_THEME = 'paper';

// Any colour CSS can write (a hex, rgb(), oklch(), a name) as '#rrggbb', by painting one pixel with it; null for what
// is no colour. The stage needs numbers, and a custom property is handed over as the text the author wrote.
let pixel = null;
function hex(value) {
  if (!value || !CSS.supports('color', value)) return null;
  if (!pixel) {
    const c = document.createElement('canvas');
    c.width = c.height = 1;
    pixel = c.getContext('2d', { willReadFrequently: true });
  }
  pixel.clearRect(0, 0, 1, 1);
  pixel.fillStyle = value;
  pixel.fillRect(0, 0, 1, 1);
  const [r, g, b] = pixel.getImageData(0, 0, 1, 1).data;
  return '#' + [r, g, b].map((v) => v.toString(16).padStart(2, '0')).join('');
}

// The theme `name` as the stage uses it: its numbers from THEMES and its colours from the page's CSS as it stands now
// (so set data-story-theme on <html> first). A token the CSS leaves out takes the built-in colour. A page whose CSS has no
// theme at all (none of the tokens only a theme defines) takes the built-in colours whole: a --paper or an --ink of
// some other stylesheet must not be mixed into a palette it was not chosen for.
export function readTheme(name, root = document.documentElement) {
  const known = THEMES[name] ? name : DEFAULT_THEME, base = THEMES[known];
  const css = getComputedStyle(root), read = (token) => css.getPropertyValue('--' + token).trim();
  const themed = ['paper-2', 'accent', 'accent-2', 'lit', 'shade'].some((t) => hex(read(t)));
  const colours = {};
  for (const token of TOKENS) colours[token] = (themed && hex(read(token))) || base.colours[token];
  return { ...base, name: known, colours, fromCss: themed };
}
