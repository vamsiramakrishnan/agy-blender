// caption.js: the article as the film's caption. It reads the story from the HTML (scenes, beats, lines, notes) and
// shows the current beat's lines under the picture or over it. It never changes the story's text: the typed-in reveal
// wraps each letter once in a span (the text stays the same characters) and is drawn by CSS from one number.
const clean = (s) => s.replace(/\s+/g, ' ').trim();

// Letters a second for the typed-in reveal: a little faster than reading aloud, so a line is whole before the eye
// has finished it and the viewer never waits on the type.
export const TYPE_RATE = 38;

// The story as the HTML tells it: [{ id, title, el, beats: [{ el, lines, notes, place, letters }] }].
export function readStory(article) {
  return [...article.querySelectorAll('section[data-scene]')].map((el) => ({
    id: el.dataset.scene,
    el,
    title: clean(el.querySelector('h2')?.textContent ?? ''),
    beats: [...el.querySelectorAll(':scope > [data-beat]')].map((b) => ({
      el: b,
      lines: [...b.querySelectorAll(':scope > p')].map((p) => clean(p.textContent)),
      notes: clean([...b.querySelectorAll(':scope > aside[data-notes]')].map((a) => a.textContent).join(' ')),
      place: b.dataset.place === 'over' ? 'over' : 'under',
      letters: 0,
    })),
  }));
}

// Wraps every visible letter of every line in <span class="ch" style="--i:n">, once, for the reveal. Spaces stay
// text, so lines still wrap between words; the letters are the same, so textContent is unchanged.
function wrapLetters(beat) {
  let n = 0;
  for (const p of beat.el.querySelectorAll(':scope > p')) {
    const walker = document.createTreeWalker(p, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    for (const node of nodes) {
      const frag = document.createDocumentFragment();
      for (const part of node.data.split(/(\s+)/)) {
        if (!part) continue;
        if (/^\s+$/.test(part)) { frag.append(part); continue; }
        for (const ch of part) {
          const span = document.createElement('span');
          span.className = 'ch';
          span.style.setProperty('--i', String(n++));
          span.textContent = ch;
          frag.append(span);
        }
      }
      node.replaceWith(frag);
    }
  }
  beat.letters = n;
}

export function createCaption(scenes) {
  for (const s of scenes) for (const b of s.beats) wrapLetters(b);
  let shown = null;
  return {
    // Shows scene i's title and beat b's lines; the other sections and beats are hidden by CSS (.is-current).
    show(i, b) {
      const next = scenes[i].beats[b];
      if (shown === next) return;
      if (shown) { shown.el.classList.remove('is-current'); shown.el.closest('section').classList.remove('is-current'); }
      next.el.classList.add('is-current');
      scenes[i].el.classList.add('is-current');
      shown = next;
    },
    // How much of the shown beat is typed: seconds into the beat, or Infinity for the whole line.
    reveal(seconds) {
      if (!shown) return;
      const n = seconds === Infinity ? shown.letters + 1 : Math.floor(seconds * TYPE_RATE);
      shown.el.style.setProperty('--shown', String(Math.min(n, shown.letters + 1)));
    },
  };
}
