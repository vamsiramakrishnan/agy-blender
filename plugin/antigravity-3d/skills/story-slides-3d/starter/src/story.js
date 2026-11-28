// story.js: the deck. Its scenes in the order of the HTML's sections (film.js reports any mismatch), its form, and
// its worlds.
// - form: 'deck' (the picture fills the frame and each scene's words lie over it, as its data-layout says) or 'film'
//   (the picture, with the scene's title and the current beat's lines under it).
// - worlds: a world is built once (src/worlds/) and posed by every scene that names it (`world: 'sky'`); between two
//   scenes of one world the camera travels and the picture is not cut.
// The look is the theme's (data-story-theme on <html>): its colours, its light, its print. A story that wants a look of
// its own names `treatment`, `paper` and `ink` here, as the film in ../examples/film does.
import * as sky from './worlds/sky.js';
import cloud from './scenes/cloud.js';
import looks from './scenes/looks.js';
import drops from './scenes/drops.js';
import weight from './scenes/weight.js';
import floats from './scenes/floats.js';
import up from './scenes/up.js';

export const film = {
  title: 'How heavy is a cloud?',
  form: 'deck',
  worlds: { sky },
  scenes: [cloud, looks, drops, weight, floats, up],
};
