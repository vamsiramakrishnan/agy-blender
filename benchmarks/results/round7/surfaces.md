# Surfaces that know the part: both measurements

Six verdicts a pair (three repeats, both orders). 'new preferred' is how many verdicts chose round seven's surface.

| pair | measure | first version | corrected version |
|---|---|---|---|
| chair, new against its round-six surface | at least 5 of 6 | 6 of 6 | 6 of 6 |
| crate, new against its round-six surface | at least 3 of 6 | 2 of 6 | 3 of 6 |
| hydrant, new against its round-six surface | at least 3 of 6 | 4 of 6 | 0 of 6 |
| chair, new against the stock agent's | reported | 5 of 6 | 2 of 5 |
| crate, new against the stock agent's | reported | 6 of 6 | 6 of 6 |
| hydrant, new against the stock agent's | reported | 6 of 6 | 6 of 6 |

Per criterion, corrected version (new / other / tie):

| pair | reads as the brief | proportion and scale | surface and material | defects | first image preferred |
|---|---|---|---|---|---|
| chair, new against its round-six surface | 0 / 0 / 6 | 0 / 0 / 6 | 6 / 0 / 0 | 6 / 0 / 0 | 3 of 6 |
| crate, new against its round-six surface | 0 / 0 / 6 | 0 / 0 / 6 | 0 / 0 / 6 | 0 / 0 / 6 | 6 of 6 |
| hydrant, new against its round-six surface | 0 / 0 / 6 | 0 / 0 / 6 | 0 / 1 / 5 | 0 / 2 / 4 | 3 of 6 |
| chair, new against the stock agent's | 0 / 0 / 5 | 2 / 2 / 1 | 5 / 0 / 0 | 1 / 3 / 1 | 5 of 5 |
| crate, new against the stock agent's | 6 / 0 / 0 | 3 / 0 / 3 | 6 / 0 / 0 | 3 / 2 / 1 | 3 of 6 |
| hydrant, new against the stock agent's | 6 / 0 / 0 | 6 / 0 / 0 | 6 / 0 / 0 | 6 / 0 / 0 | 3 of 6 |

Delivery seconds, old then new, every sample (alternated; rounds 1 and 2 of the corrected version ran while other jobs loaded the machine):

- attempt1, chair: old 24.2, 23.4, 28.4, 24.8, 23.5; new 25.0, 26.5, 26.7, 25.4, 24.6; mean +3.1 %, median +5.0 %
- attempt1, crate: old 48.3, 48.1; new 51.8, 47.5; mean +3.0 %, median +3.0 %
- attempt1, hydrant: old 40.2, 39.4; new 40.1, 42.2; mean +3.4 %, median +3.4 %
- attempt2, chair: old 27.4, 22.8, 23.7, 25.5, 24.7; new 29.9, 26.4, 25.7, 25.1, 24.8; mean +6.3 %, median +4.0 %
- attempt2, crate: old 58.9, 49.3, 50.6, 48.3, 47.7; new 55.4, 48.5, 52.6, 51.6, 51.0; mean +1.7 %, median +4.7 %
- attempt2, hydrant: old 44.0, 42.0, 44.3, 40.1, 40.7; new 45.2, 42.0, 43.4, 41.8, 43.4; mean +2.2 %, median +3.3 %

Game-ready checks: 11 of 11 on all six packages in both measurements (`surfaces.json`).

How much of each texture differs from round six's after the correction, by kind of part: `texture_difference_by_kind.txt` (mean absolute difference of the albedo in 8-bit levels, share of texels darker and brighter by more than 8, metallic and normal-map strength before and after).
