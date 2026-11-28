// caption.js: the article as the story's words on screen. It reads the story from the HTML (scenes, their layouts,
// beats, lines, notes) and shows the current scene's words: under the picture in a film, over it in a deck, laid out
// by the scene's data-layout (style.css). It never changes the story's text: it only sets classes, wraps a scene's
// content in one block, and (for the typed caption) wraps each letter in a span, so textContent stays what the page
// said and the check can compare the two.
const clean = (s) => s.replace(/\s+/g, ' ').trim();

// Letters a second for the typed-in caption: a little faster than reading aloud, so a line is whole before the eye
// has finished it and the viewer never waits on the type.
export const TYPE_RATE = 38;

// The layouts a section may name with data-layout. Anything else, or nothing, is `caption`.
export const LAYOUTS = ['caption', 'title', 'statement', 'left', 'right', 'number', 'end'];
// A scene's layout, from its <section>. film.js returns it as info().layout (readStory puts the same on each scene).
export const layoutOf = (section) => (LAYOUTS.includes(section?.dataset?.layout) ? section.dataset.layout : 'caption');
// The story's form, from story.js's definition: 'deck' (the picture fills the frame, words over it) or 'film' (the
// picture with the caption under it), which is also what a definition that names none is. film.js returns it as
// info().form.
export const formOf = (definition) => (definition?.form === 'deck' ? 'deck' : 'film');

// What counts as a line of a beat: these elements' own text, in order. A speaker's note is never a line.
const LINE = 'p, li, figcaption, h1, h2, h3, h4, h5, h6';
const HEADING = 'h1, h2, h3, h4, h5, h6';
const NOTE = 'aside[data-notes]';
// What enters as one piece though it is not text: a drawing or an image in a beat.
const UNIT = 'svg, img, canvas, video';

// A beat's lines: [{ el, text }]. Text nodes are taken in order and joined while they share the nearest line
// element, so an <li> that holds a nested list gives its own words and then the nested items, each once.
function linesOf(beat) {
  const out = [];
  const walker = document.createTreeWalker(beat, NodeFilter.SHOW_TEXT);
  for (let t = walker.nextNode(); t; t = walker.nextNode()) {
    const parent = t.parentElement;
    if (!t.data.trim() || !parent || parent.closest(NOTE) || parent.closest(UNIT)) continue;
    const el = parent.closest(LINE);
    if (!el || !beat.contains(el)) continue;
    const last = out[out.length - 1];
    if (last && last.el === el) last.text += t.data;
    else out.push({ el, text: t.data });
  }
  return out.map((l) => ({ el: l.el, text: clean(l.text) })).filter((l) => l.text);
}

// The story as the HTML tells it:
// [{ id, el, title, layout, heading, beats: [{ el, lines, lineEls, notes, place, swap, letters }] }].
export function readStory(article) {
  return [...article.querySelectorAll('section[data-scene]')].map((el) => {
    const heading = [...el.children].find((c) => c.matches(HEADING)) ?? null;
    return {
      id: el.dataset.scene,
      el,
      heading,
      title: clean(heading?.textContent ?? ''),
      layout: layoutOf(el),
      beats: [...el.querySelectorAll(':scope > [data-beat]')].map((b) => {
        const lines = linesOf(b);
        return {
          el: b,
          lines: lines.map((l) => l.text),
          lineEls: lines.map((l) => l.el),
          notes: clean([...b.querySelectorAll(NOTE)].map((a) => a.textContent).join(' ')),
          place: b.dataset.place === 'over' ? 'over' : 'under',
          swap: b.dataset.beat === 'swap', // this beat replaces what the scene showed before it
          letters: 0,
        };
      }),
    };
  });
}

// Wraps every visible letter of every line in <span class="ch" style="--i:n">, once, for the typed reveal. Spaces
// stay text, so lines still wrap between words; the letters are the same, so textContent is unchanged.
function wrapLetters(beat) {
  let n = 0;
  for (const line of beat.lineEls) {
    const walker = document.createTreeWalker(line, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    for (const node of nodes) {
      if (node.parentElement.closest('.ch')) continue; // a line inside a line: its letters are wrapped already
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

// How long a scene's words take to leave when the next scene's arrive (style.css fades them over the same time).
const LEAVE_MS = 240;

// `form` is the story's ('deck' or 'film'). A scene's words go over the picture in a deck, and in a film when its
// layout is not `caption`; those scenes are prepared here: one block round their content, and the marks the
// entrance and the type's sizes hang on.
export function createCaption(scenes, { form = 'film' } = {}) {
  for (const s of scenes) {
    s.over = form === 'deck' || s.layout !== 'caption'; // its words are laid over the picture
    s.typed = s.layout === 'caption'; // the current beat's lines only, typed in
    if (s.typed) for (const b of s.beats) wrapLetters(b);
    if (!s.over) continue;
    // One block for the scene's words: the layout places it, and its width is the widest line shown. The heading
    // gets a block of its own, as a beat is one, so the words are a column of blocks of one width, one to a row
    // (--row; the beats of a typed caption share a row, since only one of them shows). Behind them lies one empty
    // element, the scrim's patch, which CSS stretches over the rows of the blocks shown (--first to --last).
    const words = s.words = document.createElement('div');
    words.className = 'words';
    words.append(...s.el.childNodes);
    s.el.append(words);
    if (s.heading) {
      s.head = document.createElement('div');
      s.head.className = 'head';
      s.heading.replaceWith(s.head);
      s.head.append(s.heading);
      s.head.style.setProperty('--row', '1');
    }
    s.beats.forEach((b, k) => b.el.style.setProperty('--row', String((s.typed ? 0 : k) + (s.head ? 2 : 1))));
    const ground = document.createElement('span');
    ground.className = 'patch';
    ground.setAttribute('aria-hidden', 'true');
    words.prepend(ground);
    s.el.classList.add('over');
    s.el.classList.toggle('typed', s.typed);
    if (s.typed) continue;
    // The entrance comes in line by line: each line knows its place in its beat (--n), and the lines of the first
    // beat come after the heading, which arrives with them.
    s.heading?.classList.add('line');
    s.beats.forEach((b, k) => {
      let n = k === 0 && s.heading ? 1 : 0;
      const pieces = [...b.el.querySelectorAll(`${LINE}, ${UNIT}`)].filter((e) => !e.closest(NOTE) && !e.parentElement.closest(UNIT));
      for (const e of pieces) {
        if (e.querySelector(LINE)) continue; // an <li> holding a list: its items come in, each on its own
        e.classList.add('line');
        e.style.setProperty('--n', String(n++));
      }
      // A figure's size follows how many characters it has, so a long one still fits its frame (style.css, .big).
      for (const e of b.el.querySelectorAll('.big')) e.style.setProperty('--chars', String(Math.max(2, clean(e.textContent).length)));
    });
  }

  let cur = null; // { i, b }: what is shown
  let pending = null; // { scene, beat, arriving }: the current beat, shown at the next reveal()
  let touched = false; // show() was called since the last reveal(): a whole reveal after it is a settled view
  const leaving = new Map(); // scene -> timer, while its words fade out

  const blocks = (s) => (s.head ? [s.head, ...s.beats.map((b) => b.el)] : s.beats.map((b) => b.el));
  // The rows the scrim's patch covers: from the topmost block shown to the last. No measuring: only which rows.
  function patch(s) {
    if (!s.words) return;
    const rows = blocks(s).filter((el) => el.classList.contains('is-shown')).map((el) => Number(el.style.getPropertyValue('--row')));
    s.words.classList.toggle('has-shown', rows.length > 0);
    if (!rows.length) return;
    s.words.style.setProperty('--first', String(Math.min(...rows)));
    s.words.style.setProperty('--last', String(Math.max(...rows)));
  }
  const reset = (s) => {
    for (const el of blocks(s)) el.classList.remove('is-shown', 'is-entering', 'is-top', 'in-group');
    s.el.classList.remove('is-arriving');
    patch(s);
  };
  function drop(s) {
    clearTimeout(leaving.get(s));
    leaving.delete(s);
    s.el.classList.remove('is-leaving');
    if (s !== scenes[cur?.i]) reset(s);
  }

  // Which beats of a scene are on the slide at beat b: from the last beat marked swap at or before b (or the first
  // beat) up to b. The rest of that run is laid out but not shown, so nothing moves when a beat arrives.
  function stage(s, b, arriving) {
    let from = 0;
    for (let k = 0; k <= b; k++) if (s.beats[k].swap) from = k;
    let to = from + 1;
    while (to < s.beats.length && !s.beats[to].swap) to++;
    const wasShown = s.beats[b].el.classList.contains('is-shown') && !arriving;
    s.beats.forEach((beat, k) => {
      beat.el.classList.toggle('in-group', k >= from && k < to);
      const shown = s.typed ? k === b : k >= from && k < b;
      if (shown) beat.el.classList.add('is-shown');
      else if (k !== b || !wasShown) beat.el.classList.remove('is-shown');
      if (k !== b) beat.el.classList.remove('is-entering');
    });
    if (!arriving) s.head?.classList.remove('is-entering'); // what is there already stands still when a beat arrives
    if (s.typed) { s.head?.classList.add('is-shown'); s.beats[b].el.classList.add('is-shown'); }
    // The block that tops the words (the heading's, or the first beat of the run): the scrim's upper corners are its.
    blocks(s).forEach((el) => el.classList.remove('is-top'));
    (s.head ?? s.beats[s.typed ? b : from].el).classList.add('is-top');
    pending = s.typed || wasShown ? null : { scene: s, beat: s.beats[b], arriving };
    patch(s);
  }

  // Shows what show() left pending: with the entrance (the lines rise and fade in, style.css) or whole at once.
  function enter({ scene, beat, arriving }, animate) {
    const els = arriving && scene.head ? [scene.head, beat.el] : [beat.el];
    scene.el.classList.toggle('is-arriving', arriving && animate);
    // The entrance is a CSS transition from the lines' hidden state. A scene that was display:none a moment ago has
    // no such state yet, so its style is computed once here before the classes that end it are set.
    if (animate) void scene.el.offsetWidth;
    for (const el of els) {
      el.classList.add('is-shown');
      el.classList.toggle('is-entering', animate);
    }
    scene.head?.classList.add('is-shown');
    patch(scene);
  }

  return {
    // Shows scene i's heading and what beat b brings; which sections and beats show is CSS's, from these classes.
    show(i, b) {
      touched = true;
      if (cur && cur.i === i && cur.b === b) return;
      const s = scenes[i], prev = cur && scenes[cur.i], arriving = prev !== s;
      if (prev) prev.beats[cur.b].el.classList.remove('is-current');
      if (prev && arriving) {
        prev.el.classList.remove('is-current');
        // The words of the scene left behind fade out as the picture moves on (over the picture only: a film's
        // caption changes at once, as it always has).
        if (prev.over) { prev.el.classList.add('is-leaving'); leaving.set(prev, setTimeout(() => drop(prev), LEAVE_MS)); }
        else reset(prev);
      }
      cur = { i, b };
      if (leaving.has(s)) drop(s);
      s.el.classList.add('is-current');
      s.beats[b].el.classList.add('is-current');
      if (s.over) stage(s, b, arriving);
      else pending = null;
      // The engine calls reveal() with its next frame. Should one never come (an engine that does not draw the
      // caption's progress), the words still arrive, two frames on.
      const mine = pending;
      if (mine) requestAnimationFrame(() => requestAnimationFrame(() => {
        if (pending !== mine) return;
        enter(mine, !matchMedia('(prefers-reduced-motion: reduce)').matches);
        pending = null;
      }));
    },
    // How far the shown beat has played: seconds into the beat, or Infinity for a beat shown finished. The typed
    // caption follows the seconds; a slide's lines start their entrance at the first call after show(), and are
    // whole at once when that call says Infinity (a settled view, a step back, reduced motion).
    reveal(seconds) {
      if (!cur) return;
      const s = scenes[cur.i], beat = s.beats[cur.b], whole = seconds === Infinity;
      if (s.typed) {
        const n = whole ? beat.letters + 1 : Math.floor(seconds * TYPE_RATE);
        beat.el.style.setProperty('--shown', String(Math.min(n, beat.letters + 1)));
      }
      if (pending) { enter(pending, !whole); pending = null; }
      else if (whole && touched && s.over) {
        for (const el of blocks(s)) el.classList.remove('is-entering');
        s.el.classList.remove('is-arriving');
      }
      if (whole && touched) for (const old of [...leaving.keys()]) drop(old);
      touched = false;
    },
  };
}
