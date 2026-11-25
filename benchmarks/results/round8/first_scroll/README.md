# What the first scroll changes

Measured after run 5, because nothing in this round had measured the scroll itself: the blind judge sees nine
stills of each site.

## One wheel notch at the top of the page

A notch of a mouse wheel is 100 to 120 px of scroll. `check_site` now scrolls 120 px from the top of the page on its
desktop visit (1440 x 900) and reads how far the picture moved (`picture_shift` in
`src/antigravity3d/deliver/site_check.py`: the pixels that flip, per place a row or a column crosses an edge; a
picture that moves d pixels reads about d, and one whose edges move under half a pixel reads 0).

| site | the 3D canvas alone | the window, text included | what the check says |
|---|---|---|---|
| run 2 | 0.07 px | 0.03 px | the first scroll gets no answer |
| run 3 | 0.11 px | 0.05 px | the first scroll gets no answer |
| run 4 | 0.32 px | 0.14 px | the first scroll gets no answer |
| run 5 | 0.15 px | 0.07 px | the first scroll gets no answer |
| run 5, rebuilt with a camera curve that starts at speed | 2.46 px | 1.13 px | nothing |
| run 5, rebuilt with the starter's opening move as well | 8.83 px | 3.97 px | nothing |
| the skill's starter before the change | 0.02 px | 0.00 px | the first scroll gets no answer |
| the skill's starter now | 9.1 px | 1.41 px | nothing |
| the stock agent's site | not read | not read | nothing: the top of its page keeps changing by itself |

`first_notch_difference.jpg` shows the pixels that change over that notch on run 4's site (left) and run 5's
(right), in red over the darkened frame: thin outlines along every edge, the mark of a picture that moved by a
fraction of a pixel.

`first_notch_run5.jpg` shows run 5's site at 0, 120, 600 and 1575 px of scroll, as built (top row) and rebuilt with
the opening move (bottom row). The two rows end on the same picture: the first chapter's shot is unchanged, the
page only arrives at it from further round.

## The cause

Every plugin site since run 2 keeps the starter's timeline. Its first chapter had no move: the camera held the
first shot for the chapter's whole 180 % of the window, a hold travels slowly (8 degrees round its subject and a
tenth closer), and the curve through the camera's keys started from rest. One notch covered under 2 % of that
travel. The text is a fixed layer and does not move either.

## Lower down the page

`notch_profile.json`: the share of the window's pixels that change over one notch at 24 places down three sites.

| site | at the top | median | in a hold | in a move |
|---|---|---|---|---|
| run 4 | 0.76 % | 5.9 % | 3 to 6 % | 14 to 32 % |
| run 5 | 0.36 % | 7.2 % | 3 to 8 % | 11 to 43 % |
| the stock agent's | 32 % | 31.6 % | never under 20 % | |

So the dead place was the top. The stock agent's page moves its text with the scroll and flies its camera through
the scene from the first pixel; that is why its first touch is the livelier one.

## What changed

- `starter/src/shots.js`: the page opens `OPENING` (24) degrees back from its first shot, round the same target at
  the same distance, and swings round to the shot over the first `MOVE` of the first chapter; the camera curve's
  first key takes a one-sided slope instead of 0. The opening is no move in the page's sense (`info().moving` stays
  false), so the title is up through it.
- `check_site`: the first notch is read on the desktop visit, and "the first scroll gets no answer" is a finding
  when the picture moves by under a pixel and under a tenth of the text in view moves. It is not read where the
  top of the page changes by itself.

The two rebuilt copies of run 5 differ from the agent's source in `src/shots.js` only, by those lines.
