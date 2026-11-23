// The chapters of the film, in order, and the mapping between the document and film time T (see scene/tracks.js).
export const CHAPTERS = ['hero', 'flood', 'residency', 'fates', 'insight', 'capture', 'digest', 'retrieve', 'contained', 'anchors', 'handoff', 'gates', 'receipts', 'start'];

// Where in a chapter a reader lands from the rail (just past the panel's ease-in) and where reduced motion rests.
export const ARRIVAL = 0.14;
export const RESTING = 0.6;

// Measure the chapter sections once per layout: [{ id, el, top, height }].
export function measure(root = document) {
  return CHAPTERS.map((id) => {
    const el = root.querySelector(`[data-ch="${id}"].ch`);
    const r = el.getBoundingClientRect();
    return { id, el, top: r.top + window.scrollY, height: r.height };
  });
}

// Film time for a scroll position: i + the share of chapter i that has passed the top of the viewport.
export function timeAt(boxes, scrollY) {
  let i = 0;
  while (i < boxes.length - 1 && scrollY >= boxes[i + 1].top - 0.5) i++;
  const b = boxes[i];
  return i + Math.min(Math.max((scrollY - b.top) / b.height, 0), 0.9999);
}

// The scroll position at which film time is T.
export function scrollFor(boxes, T) {
  const i = Math.min(Math.max(Math.floor(T), 0), boxes.length - 1);
  return boxes[i].top + (T - i) * boxes[i].height;
}

// Reduced motion: the film rests at one key pose per chapter.
export const restingT = (T) => Math.floor(T) + RESTING;
