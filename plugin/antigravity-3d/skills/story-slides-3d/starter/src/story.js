// story.js: the film. Its scenes in the order of the HTML's sections (film.js reports any mismatch), its treatment
// (print.js; ?print=grade|halftone|glyph shows the alternatives) and its two inks, which the print uses for paper and
// ink (style.css repeats them for the page around the picture).
import title from './scenes/title.js';
import tower from './scenes/tower.js';
import room from './scenes/room.js';
import shore from './scenes/shore.js';
import end from './scenes/end.js';

export const film = {
  title: 'Keeper',
  treatment: 'halftone',
  paper: '#0b0f17', // night: the ground everything is printed on
  ink: '#efe4cb', // lamp light: what the light draws with
  scenes: [title, tower, room, shore, end],
};
