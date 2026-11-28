// story.js: the film. Its scenes in the order of the HTML's sections (film.js reports any mismatch) and its form:
// 'film' is the picture with the scene's title and the current beat's lines under it.
// A film has a look of its own: it names its treatment (print.js; ?print=grade|halftone|glyph shows the alternatives)
// and its two inks, which the print uses for paper and ink in any theme. The page round the picture takes its colours
// from the theme named on <html> (index.html: data-story-theme="ink", the same night and lamp light).
import title from './scenes/title.js';
import tower from './scenes/tower.js';
import room from './scenes/room.js';
import shore from './scenes/shore.js';
import end from './scenes/end.js';

export const film = {
  title: 'Keeper',
  form: 'film',
  treatment: 'halftone',
  paper: '#0b0f17', // night: the ground everything is printed on
  ink: '#efe4cb', // lamp light: what the light draws with
  scenes: [title, tower, room, shore, end],
};
