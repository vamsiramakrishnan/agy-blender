// chapters.js: the page's chapters, read from the HTML, and the mapping between scroll position and story time T.
//
// T = the index of the chapter at the top of the window + the share of that chapter scrolled past (0 at the top of
// the page, chapters.length at the bottom). The chapters are the page's own <section data-chapter="id"> elements, so
// adding, removing or lengthening a chapter is an edit of the HTML and nothing else.
// Lifted from examples/straitjacket-film/site/js/chapters.js, with one change: the last chapter's span is what can
// still scroll past (its height less the window), so every chapter, the last included, runs the whole way from 0 to 1.

// Measure the chapters once per layout (after load, resize and font loading): [{ id, el, top, span }].
export function measure(root = document) {
  const els = [...root.querySelectorAll('[data-chapter]')];
  const boxes = els.map((el) => {
    const r = el.getBoundingClientRect();
    return { id: el.dataset.chapter, el, top: r.top + window.scrollY, span: r.height };
  });
  if (boxes.length) {
    const last = boxes[boxes.length - 1];
    last.span = Math.max(1, last.span - window.innerHeight);
  }
  return boxes;
}

const clamp = (x, a, b) => (x < a ? a : x > b ? b : x);

// Story time for a scroll position.
export function timeAt(boxes, scrollY) {
  if (!boxes.length) return 0;
  let i = 0;
  while (i < boxes.length - 1 && scrollY >= boxes[i + 1].top - 0.5) i++;
  const b = boxes[i];
  const cap = i === boxes.length - 1 ? 1 : 0.9999;
  return i + clamp((scrollY - b.top) / b.span, 0, cap);
}

// The scroll position at which story time is T (the inverse of timeAt).
export function scrollFor(boxes, T) {
  if (!boxes.length) return 0;
  const i = clamp(Math.floor(T), 0, boxes.length - 1);
  return boxes[i].top + clamp(T - i, 0, 1) * boxes[i].span;
}
