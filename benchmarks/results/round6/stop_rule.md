# Stop rule: the script path against today's session

Verdict: **FAIL** (the script path does not beat today's session; the rule says stop)

Arms: N: today's session (stock agent + plugin as merged) from live_N_p1, live_N_main; S: script path (same agent, plugin in script mode) from live_S_p1; V: stock agent + plain Blender MCP server from round5:live_scorecard

Tokens: input (of which cached), output, thinking; fresh = input - cached + output + thinking. A cache miss is an agent-model call after the first whose cached share is below 5 % of its input.
With several runs of one cell the figure is the median with the range beside it. Seconds in model calls are the agent model's calls; the plugin's own model calls happen inside a tool call.

## 1. Per brief, one row per arm

### crate

|  | N | S | V |
|---|---:|---:|---:|
| runs | 2 | 1 | 1 |
| delivered (how it ended) | 2 of 2 (finished) | yes (finished) | yes (finished) |
| wall seconds | 240.2 (range 213.2 to 267.1) | 254.2 | 609.3 |
| model calls | 12 (range 11 to 14) | 15 | 19 |
|   of which by the agent's model | 12 (range 10 to 13) | 14 | 19 |
| tool calls | 21 (range 20 to 22) | 16 | 41 |
| input tokens | 569,132 (range 477,161 to 661,102) | 582,708 | 1,031,888 |
|   of which cached | 486,954 (range 401,768 to 572,141) | 443,941 | 787,199 |
| output tokens | 8,251 (range 7,835 to 8,667) | 12,993 | 54,470 |
| thinking tokens | 17,570 (range 16,796 to 18,345) | 8,876 | 14,190 |
| fresh tokens | 107,998 (range 100,024 to 115,973) | 160,636 | 313,349 |
| cache misses (calls) | 0 (range 0 to 0) | 1 | 2 |
|   input tokens of those calls | 0 (range 0 to 0) | 59,129 | 133,181 |
| seconds inside model calls | 130.6 (range 105.4 to 155.9) | 141.3 | 315.4 |
| seconds inside tool calls | 84.0 (range 82.2 to 85.7) | 86.9 | 270.9 |
|   largest tool call | deliver_package 71.4 s; deliver_package 72.7 s | deliver_package 66.9 s | get_viewport_screenshot 92.0 s |
| checks passed | 11 (range 11 to 11) of 11 | 11 of 11 | 10 of 11 |
|   failed checks | none | none | Independent Blender check passes [upstream_qa] |
| triangles (main file) | 2,772 (range 2,654 to 2,890) | 2,784 | 1,392 |
| hand steps | 2 (range 2 to 2) | 2 | 2 |

- arm N: r5_crate_N (from live_N_p1), delivered, finished, 213.2 s, 11 model calls, fresh 100,024, cache misses 0 (0 input), checks 11 of 11
    1 calls without usage (skipped)
- arm N: r5_crate_N (from live_N_main), delivered, finished, 267.1 s, 14 model calls, fresh 115,973, cache misses 0 (0 input), checks 11 of 11
    1 calls without usage (skipped)
- arm S: r5_crate_S (from live_S_p1), delivered, finished, 254.2 s, 15 model calls, fresh 160,636, cache misses 1 (59,129 input), checks 11 of 11
    1 calls without usage (skipped)
- arm V: r5_crate_V (from live_scorecard), delivered, finished, 609.3 s, 19 model calls, fresh 313,349, cache misses 2 (133,181 input), checks 10 of 11

### hydrant

|  | N | S | V |
|---|---:|---:|---:|
| runs | 2 | 1 | 1 |
| delivered (how it ended) | 2 of 2 (finished) | yes (finished) | yes (finished) |
| wall seconds | 281.6 (range 257.1 to 306.1) | 312.6 | 377.2 |
| model calls | 14 (range 13 to 15) | 12 | 17 |
|   of which by the agent's model | 13 (range 12 to 14) | 11 | 17 |
| tool calls | 23 (range 20 to 26) | 13 | 23 |
| input tokens | 745,886 (range 652,105 to 839,668) | 602,786 | 645,632 |
|   of which cached | 612,690 (range 560,379 to 665,001) | 475,035 | 474,868 |
| output tokens | 11,850 (range 10,183 to 13,518) | 20,787 | 22,422 |
| thinking tokens | 23,962 (range 23,682 to 24,243) | 17,234 | 19,160 |
| fresh tokens | 169,010 (range 125,591 to 212,428) | 165,772 | 212,346 |
| cache misses (calls) | 0 (range 0 to 1) | 1 | 2 |
|   input tokens of those calls | 34,440 (range 0 to 68,879) | 36,426 | 101,998 |
| seconds inside model calls | 176.0 (range 156.5 to 195.5) | 195.2 | 222.5 |
| seconds inside tool calls | 79.5 (range 74.7 to 84.4) | 91.0 | 132.1 |
|   largest tool call | deliver_package 67.1 s; deliver_package 60.4 s | deliver_package 69.6 s | execute_blender_code 61.1 s |
| checks passed | 11 (range 11 to 11) of 11 | 11 of 11 | 9 of 11 |
|   failed checks | none | none | Independent Blender check passes [upstream_qa]; Collision present and simple [collision] |
| triangles (main file) | 3,358 (range 3,282 to 3,433) | 3,912 | 3,820 |
| hand steps | 2 (range 2 to 2) | 2 | 2 |

- arm N: r5_hydrant_N (from live_N_p1), delivered, finished, 306.1 s, 15 model calls, fresh 212,428, cache misses 1 (68,879 input), checks 11 of 11
    1 calls without usage (skipped)
- arm N: r5_hydrant_N (from live_N_main), delivered, finished, 257.1 s, 13 model calls, fresh 125,591, cache misses 0 (0 input), checks 11 of 11
    1 calls without usage (skipped)
- arm S: r5_hydrant_S (from live_S_p1), delivered, finished, 312.6 s, 12 model calls, fresh 165,772, cache misses 1 (36,426 input), checks 11 of 11
    1 calls without usage (skipped)
- arm V: r5_hydrant_V (from live_scorecard), delivered, finished, 377.2 s, 17 model calls, fresh 212,346, cache misses 2 (101,998 input), checks 9 of 11

### chair

|  | N | S | V |
|---|---:|---:|---:|
| runs | 2 | 1 | 1 |
| delivered (how it ended) | 2 of 2 (finished) | yes (finished) | no (stopped at limit) |
| wall seconds | 275.8 (range 261.5 to 290.1) | 258.2 | 380.3 |
| model calls | 13 (range 12 to 14) | 17 | 20 |
|   of which by the agent's model | 12 (range 11 to 13) | 16 | 20 |
| tool calls | 22 (range 17 to 27) | 19 | 45 |
| input tokens | 668,492 (range 572,663 to 764,320) | 865,384 | 1,064,082 |
|   of which cached | 571,520 (range 476,995 to 666,046) | 763,293 | 949,920 |
| output tokens | 11,774 (range 8,586 to 14,962) | 23,320 | 43,033 |
| thinking tokens | 26,080 (range 23,754 to 28,405) | 13,053 | 17,243 |
| fresh tokens | 134,824 (range 132,659 to 136,990) | 138,464 | 174,438 |
| cache misses (calls) | 0 (range 0 to 0) | 0 | 0 |
|   input tokens of those calls | 0 (range 0 to 0) | 0 | 0 |
| seconds inside model calls | 188.8 (range 173.8 to 203.9) | 164.3 | 306.5 |
| seconds inside tool calls | 62.0 (range 61.3 to 62.8) | 68.1 | 117.2 |
|   largest tool call | deliver_package 49.6 s; deliver_package 53.5 s | deliver_package 53.7 s | execute_blender_code 66.6 s |
| checks passed | 10 (range 10 to 10) of 11 | 11 of 11 | 9 of 11 |
|   failed checks | LOD files present and valid [lods] | none | Texel density even [texel_even]; Collision present and simple [collision] |
| triangles (main file) | 3,089 (range 3,050 to 3,128) | 4,072 | 3,580 |
| hand steps | 2 (range 2 to 2) | 2 | 2 |

- arm N: r5_chair_N (from live_N_p1), delivered, finished, 290.1 s, 14 model calls, fresh 136,990, cache misses 0 (0 input), checks 10 of 11
    1 calls without usage (skipped)
- arm N: r5_chair_N (from live_N_main), delivered, finished, 261.5 s, 12 model calls, fresh 132,659, cache misses 0 (0 input), checks 10 of 11
    1 calls without usage (skipped)
- arm S: r5_chair_S (from live_S_p1), delivered, finished, 258.2 s, 17 model calls, fresh 138,464, cache misses 0 (0 input), checks 11 of 11
    1 calls without usage (skipped)
- arm V: r5_chair_V (from live_scorecard), not delivered, stopped at limit, 380.3 s, 20 model calls, fresh 174,438, cache misses 0 (0 input), checks 9 of 11
    1 model calls that did not return 200 (skipped)

## 2. The stop rule

The owner heard: "If it does not beat today's session on time, fresh tokens and the chair, we stop. You will see those numbers before anything else gets built."

Read as: S must be lower than N on wall time and on fresh tokens on every one of the three briefs (medians when a cell has several runs), and the chair must not read worse. A tie is not a win. Any FAIL makes the verdict FAIL; a result that could not be measured is shown as NOT MEASURED and does not count as a pass.

S against N on time:

| brief | N (median of runs) | S (median of runs) | S minus N (medians) | percent | runs | result |
|---|---:|---:|---:|---:|---|---|
| crate | 240.2, range 213.2 to 267.1 | 254.2 | +14.0 s | +5.9 % | ranges overlap | FAIL |
| hydrant | 281.6, range 257.1 to 306.1 | 312.6 | +31.0 s | +11.0 % | ranges do not overlap | FAIL |
| chair | 275.8, range 261.5 to 290.1 | 258.2 | -17.6 s | -6.4 % | ranges do not overlap | PASS |

S against N on fresh tokens:

| brief | N (median of runs) | S (median of runs) | S minus N (medians) | percent | runs | result |
|---|---:|---:|---:|---:|---|---|
| crate | 107,998, range 100,024 to 115,973 | 160,636 | +52,638 tokens | +48.7 % | ranges do not overlap | FAIL |
| hydrant | 169,010, range 125,591 to 212,428 | 165,772 | -3,238 tokens | -1.9 % | ranges overlap | PASS |
| chair | 134,824, range 132,659 to 136,990 | 138,464 | +3,640 tokens | +2.7 % | ranges do not overlap | FAIL |

Blind comparisons (both orders are run; each count is one comparison):

- crate, S vs V: overall S 6, V 0, ties 0 of 6 comparisons
    reads as the brief: S 6, V 0, ties 0
    proportion and scale: S 3, V 0, ties 3
    surface and material: S 6, V 0, ties 0
    defects: S 6, V 0, ties 0
- hydrant, S vs V: overall S 5, V 0, ties 0 of 5 comparisons (1 without a verdict)
    reads as the brief: S 2, V 3, ties 0
    proportion and scale: S 5, V 0, ties 0
    surface and material: S 5, V 0, ties 0
    defects: S 5, V 0, ties 0
- chair, S vs V: overall S 0, V 6, ties 0 of 6 comparisons
    reads as the brief: S 0, V 0, ties 6
    proportion and scale: S 0, V 3, ties 3
    surface and material: S 0, V 6, ties 0
    defects: S 0, V 6, ties 0
- crate, S vs N: overall S 4, N 2, ties 0 of 6 comparisons
    reads as the brief: S 6, N 0, ties 0
    proportion and scale: S 4, N 0, ties 2
    surface and material: S 5, N 1, ties 0
    defects: S 1, N 5, ties 0
- hydrant, S vs N: overall S 6, N 0, ties 0 of 6 comparisons
    reads as the brief: S 6, N 0, ties 0
    proportion and scale: S 2, N 0, ties 4
    surface and material: S 6, N 0, ties 0
    defects: S 6, N 0, ties 0
- chair, S vs N: overall S 6, N 0, ties 0 of 6 comparisons
    reads as the brief: S 5, N 0, ties 1
    proportion and scale: S 6, N 0, ties 0
    surface and material: S 5, N 0, ties 1
    defects: S 3, N 3, ties 0
- crate, N vs V: overall N 6, V 0, ties 0 of 6 comparisons
    reads as the brief: N 6, V 0, ties 0
    proportion and scale: N 0, V 0, ties 6
    surface and material: N 6, V 0, ties 0
    defects: N 6, V 0, ties 0
- hydrant, N vs V: overall N 6, V 0, ties 0 of 6 comparisons
    reads as the brief: N 0, V 6, ties 0
    proportion and scale: N 6, V 0, ties 0
    surface and material: N 6, V 0, ties 0
    defects: N 5, V 1, ties 0
- chair, N vs V: overall N 0, V 6, ties 0 of 6 comparisons
    reads as the brief: N 0, V 6, ties 0
    proportion and scale: N 0, V 6, ties 0
    surface and material: N 2, V 4, ties 0
    defects: N 0, V 6, ties 0

Result per test:

- FAIL: S beats N on time on every one of the three briefs (median of the runs) (S lower than N on 1 of 3 briefs; not beaten on crate, hydrant)
- FAIL: S beats N on fresh tokens on every one of the three briefs (median of the runs) (S lower than N on 1 of 3 briefs; not beaten on crate, chair)
- PASS: chair: in the blind comparison S wins more comparisons than it loses to today's session (S 6, N 0, ties 0 of 6 comparisons)
- FAIL: chair: in the blind comparison S does not win fewer comparisons than the stock Blender agent (S 0, V 6, ties 0 of 6 comparisons) [shown beside the rule; not part of the verdict]
- PASS: checks passed stay at 11 of 11 for S (guard from the contract) (3 of 3 evaluated S runs passed every check)

**Overall verdict: FAIL**

Beside the result, not the result: fresh tokens without the cache-miss calls' input (the input those calls paid in full because the cache did not serve them), so a reader can tell a design difference from cache luck.

| brief | N fresh | N fresh without miss input | S fresh | S fresh without miss input |
|---|---:|---:|---:|---:|
| crate | 107,998 (range 100,024 to 115,973) | 107,998 (range 100,024 to 115,973) | 160,636 | 101,507 |
| hydrant | 169,010 (range 125,591 to 212,428) | 134,570 (range 125,591 to 143,549) | 165,772 | 129,346 |
| chair | 134,824 (range 132,659 to 136,990) | 134,824 (range 132,659 to 136,990) | 138,464 | 138,464 |

## 3. Change requests

| arm | brief | status | wall s | model calls | input | cached | output | thinking | fresh | scope: unchanged / on target / collateral / added / removed | scope check passed |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| N | chair | crashed | 1,278.3 | 14 | 853,469 | 539,132 | 2,515 | 2,907 | 319,759 | not evaluated | n/a |
| N | crate | finished | 252.5 | 20 | 998,694 | 861,402 | 8,124 | 19,714 | 165,130 | 51 / 14 / 0 / 0 / 0 | yes |
| N | hydrant | finished | 278.5 | 19 | 1,159,954 | 905,270 | 6,110 | 20,827 | 281,621 | 49 / 10 / 5 / 0 / 0 | no |
| S | chair | finished | 131.2 | 10 | 495,452 | 413,534 | 3,003 | 5,370 | 90,291 | 16 / 1 / 6 / 0 / 0 | no |
| S | crate | finished | 180.4 | 10 | 446,031 | 298,420 | 4,837 | 8,574 | 161,022 | 54 / 16 / 0 / 0 / 0 | yes |
| S | hydrant | finished | 224.2 | 13 | 658,206 | 566,248 | 4,919 | 12,672 | 109,549 | 38 / 9 / 7 / 0 / 0 | no |
| V | chair | finished | 253.2 | 9 | 392,523 | 292,976 | 2,792 | 11,223 | 113,562 | 12 / 1 / 10 / 0 / 0 | no |
| V | crate | stopped at limit | 104.1 | 12 | 519,742 | 439,600 | 8,613 | 8,115 | 96,870 | 58 / 8 / 0 / 0 / 0 | yes |
| V | hydrant | stopped at limit | 143.5 | 13 | 548,883 | 477,903 | 9,429 | 8,646 | 89,055 | 0 / 6 / 51 / 0 / 0 | no |

Scope check: at least one part changed by the asked ratio and no collateral change (compare_eval's rule).

## 4. Where the time went

One run per arm and brief (the first in the list). Each line is a model call: seconds, output and thinking tokens, the tool it asked for. MISS marks a cache miss. Long lists show the ten slowest calls in order.

**crate, arm N** (r5_crate_N): 213.2 s = 105.4 s in model calls + 82.2 s in tool calls + 25.6 s elsewhere (waiting, harness).
Tools by time: deliver_package 71.4 s (1); render_preview 8.8 s (2); apply_scene_ir 1.8 s (4); open_asset_session 0.6 s (1).

| call | starts at s | seconds | output | thinking | cache | tool asked for |
|---:|---:|---:|---:|---:|---|---|
| 1 | 0.0 | 3.9 | 478 | 181 |  | view_file x5 |
| 2 | 4.1 | 3.4 | 352 | 200 |  | view_file x2, open_asset_session |
| 3 | 8.3 | 32.8 | 935 | 6,298 |  | apply_scene_ir |
| 4 | 41.3 | 13.5 | 1,325 | 2,019 |  | apply_scene_ir, inspect_manifest |
| 5 | 55.2 | 18.8 | 1,809 | 2,886 |  | apply_scene_ir, render_preview |
| 6 | 79.0 | 2.3 | 164 | 35 |  | view_file x2 |
| 7 | 81.4 | 13.4 | 952 | 2,170 |  | apply_scene_ir, session_status |
| 8 | 95.3 | 3.9 | 78 | 530 |  | render_preview |
| 9 | 103.2 | 3.6 | 183 | 174 |  | view_file, deliver_package |
| 10 | 178.2 | 9.8 | 1,111 | 881 |  | (reply text only) |

**crate, arm S** (r5_crate_S): 254.2 s = 141.3 s in model calls + 86.9 s in tool calls + 26.0 s elsewhere (waiting, harness).
Tools by time: deliver_package 66.9 s (1); run_build 19.8 s (4); view_file 0.2 s (6); run_command 0.1 s (1).

| call | starts at s | seconds | output | thinking | cache | tool asked for |
|---:|---:|---:|---:|---:|---|---|
| 1 | 0.0 | 3.2 | 337 | 74 |  | view_file x3, run_command |
| 2 | 3.4 | 39.6 | 4,230 | 3,950 |  | write_to_file |
| 3 | 43.1 | 6.4 | 116 | 70 |  | run_build |
| 4 | 50.1 | 9.1 | 413 | 278 |  | replace_file_content |
| 5 | 59.3 | 2.0 | 116 | 12 |  | run_build |
| 6 | 61.9 | 8.5 | 1,155 | 392 |  | replace_file_content |
| 7 | 70.5 | 1.9 | 116 | 11 |  | run_build |
| 8 | 82.0 | 3.1 | 90 | 69 |  | view_file |
| 9 | 85.2 | 26.3 | 4,569 | 1,616 |  | replace_file_content |
| 10 | 111.6 | 2.2 | 116 | 25 |  | run_build |
| 11 | 123.1 | 4.0 | 89 | 125 |  | view_file |
| 12 | 127.2 | 17.2 | 91 | 191 |  | deliver_package |
| 13 | 211.3 | 6.1 | 111 | 396 | MISS | view_file |
| 14 | 217.5 | 11.5 | 996 | 1,071 |  | (reply text only) |

**crate, arm V** (r5_crate_V): 609.3 s = 315.4 s in model calls + 270.9 s in tool calls + 23.0 s elsewhere (waiting, harness).
Tools by time: get_viewport_screenshot 471.4 s (13); run_command 44.5 s (4); execute_blender_code 35.2 s (7); view_file 2.8 s (16).

| call | starts at s | seconds | output | thinking | cache | tool asked for |
|---:|---:|---:|---:|---:|---|---|
| 2 | 3.3 | 9.2 | 223 | 1,548 |  | get_scene_info, execute_blender_code |
| 3 | 13.9 | 50.4 | 6,510 | 5,090 |  | execute_blender_code |
| 6 | 76.3 | 26.1 | 5,789 | 781 |  | execute_blender_code |
| 9 | 114.8 | 44.9 | 8,291 | 2,212 |  | execute_blender_code |
| 10 | 164.2 | 9.7 | 583 | 429 |  | execute_blender_code |
| 13 | 271.5 | 54.5 | 12,873 | 1,454 |  | execute_blender_code |
| 15 | 440.3 | 11.9 | 2,418 | 31 | MISS | view_file x3, run_command |
| 16 | 453.6 | 56.5 | 12,909 | 974 |  | execute_blender_code |
| 17 | 518.9 | 9.4 | 1,851 | 99 |  | get_viewport_screenshot x2, run_command |
| 19 | 578.3 | 8.8 | 780 | 669 |  | (reply text only) |

9 other agent calls: 33.9 s.

**hydrant, arm N** (r5_hydrant_N): 306.1 s = 195.5 s in model calls + 84.4 s in tool calls + 26.2 s elsewhere (waiting, harness).
Tools by time: deliver_package 67.1 s (1); render_preview 14.6 s (3); apply_scene_ir 4.8 s (6); view_file 2.1 s (10).

| call | starts at s | seconds | output | thinking | cache | tool asked for |
|---:|---:|---:|---:|---:|---|---|
| 1 | 0.0 | 5.4 | 494 | 564 |  | view_file x5 |
| 2 | 5.7 | 6.4 | 467 | 453 |  | open_asset_session, view_file x3 |
| 3 | 12.8 | 26.9 | 2,177 | 3,784 |  | apply_scene_ir |
| 4 | 39.9 | 10.1 | 1,636 | 735 |  | apply_scene_ir |
| 5 | 50.5 | 26.0 | 1,919 | 2,763 |  | inspect_manifest, apply_scene_ir |
| 6 | 77.4 | 41.1 | 1,450 | 7,114 |  | apply_scene_ir, render_preview |
| 7 | 123.2 | 7.7 | 153 | 960 |  | view_file, session_status |
| 8 | 131.0 | 15.2 | 560 | 2,389 |  | run_command |
| 9 | 146.4 | 10.4 | 578 | 952 | MISS | run_command |
| 10 | 156.8 | 19.9 | 2,051 | 1,258 |  | apply_scene_ir, render_preview |
| 11 | 182.1 | 8.3 | 304 | 706 |  | apply_scene_ir, render_preview |
| 12 | 195.3 | 4.1 | 148 | 129 |  | view_file, session_status |
| 13 | 199.4 | 4.6 | 85 | 270 |  | deliver_package |
| 14 | 271.2 | 9.5 | 948 | 764 |  | (reply text only) |

**hydrant, arm S** (r5_hydrant_S): 312.6 s = 195.2 s in model calls + 91.0 s in tool calls + 26.4 s elsewhere (waiting, harness).
Tools by time: deliver_package 69.6 s (1); run_build 21.1 s (4); view_file 0.2 s (3); run_command 0.1 s (1).

| call | starts at s | seconds | output | thinking | cache | tool asked for |
|---:|---:|---:|---:|---:|---|---|
| 1 | 0.0 | 5.0 | 360 | 121 |  | view_file x3, run_command |
| 2 | 5.2 | 59.6 | 4,573 | 8,012 |  | write_to_file |
| 3 | 64.8 | 2.6 | 116 | 73 | MISS | run_build |
| 4 | 68.2 | 40.1 | 6,804 | 1,086 |  | replace_file_content |
| 5 | 108.4 | 7.1 | 116 | 12 |  | run_build |
| 6 | 123.1 | 34.5 | 5,747 | 2,699 |  | replace_file_content |
| 7 | 157.7 | 3.0 | 118 | 17 |  | run_build |
| 8 | 167.1 | 9.4 | 1,275 | 671 |  | replace_file_content |
| 9 | 176.6 | 2.2 | 115 | 19 |  | run_build |
| 10 | 185.3 | 11.1 | 74 | 1,982 |  | deliver_package |
| 11 | 266.0 | 20.7 | 1,036 | 1,139 |  | (reply text only) |

**hydrant, arm V** (r5_hydrant_V): 377.2 s = 222.5 s in model calls + 132.1 s in tool calls + 22.5 s elsewhere (waiting, harness).
Tools by time: execute_blender_code 117.4 s (6); get_viewport_screenshot 13.4 s (4); run_command 12.1 s (5); get_scene_info 0.9 s (1).

| call | starts at s | seconds | output | thinking | cache | tool asked for |
|---:|---:|---:|---:|---:|---|---|
| 2 | 3.6 | 7.6 | 205 | 1,114 |  | get_scene_info, execute_blender_code |
| 4 | 14.2 | 17.5 | 275 | 2,378 |  | execute_blender_code |
| 6 | 37.7 | 69.1 | 6,022 | 8,965 |  | execute_blender_code |
| 8 | 113.0 | 7.2 | 106 | 69 |  | view_file |
| 9 | 120.2 | 29.2 | 4,455 | 2,050 |  | execute_blender_code |
| 10 | 210.5 | 11.4 | 1,431 | 608 | MISS | execute_blender_code |
| 13 | 233.8 | 25.9 | 5,435 | 1,297 |  | execute_blender_code |
| 15 | 324.5 | 6.4 | 1,035 | 197 |  | view_file, get_viewport_screenshot, run_command |
| 16 | 335.6 | 11.7 | 809 | 197 |  | view_file, run_command |
| 17 | 347.5 | 7.6 | 628 | 696 |  | (reply text only) |

7 other agent calls: 29.1 s.

**chair, arm N** (r5_chair_N): 290.1 s = 203.9 s in model calls + 61.3 s in tool calls + 24.9 s elsewhere (waiting, harness).
Tools by time: deliver_package 49.6 s (1); render_preview 7.9 s (2); apply_scene_ir 4.0 s (8); inspect_manifest 2.2 s (6).

| call | starts at s | seconds | output | thinking | cache | tool asked for |
|---:|---:|---:|---:|---:|---|---|
| 1 | 0.0 | 5.5 | 495 | 574 |  | view_file x5 |
| 2 | 5.7 | 4.2 | 354 | 262 |  | open_asset_session, view_file x2 |
| 3 | 10.8 | 28.2 | 1,271 | 4,060 |  | apply_scene_ir, inspect_manifest |
| 4 | 39.2 | 10.5 | 1,472 | 910 |  | apply_scene_ir, inspect_manifest |
| 5 | 49.8 | 39.8 | 1,625 | 5,808 |  | apply_scene_ir, inspect_manifest |
| 6 | 90.0 | 25.5 | 1,931 | 3,755 |  | apply_scene_ir, inspect_manifest, render_preview |
| 7 | 119.2 | 25.4 | 2,339 | 2,379 |  | apply_scene_ir, inspect_manifest |
| 8 | 144.7 | 16.5 | 3,281 | 758 |  | apply_scene_ir, inspect_manifest, render_preview |
| 9 | 165.7 | 4.0 | 90 | 44 |  | view_file |
| 10 | 169.7 | 18.1 | 499 | 1,434 |  | apply_scene_ir, session_status |
| 11 | 188.8 | 5.6 | 115 | 218 |  | apply_scene_ir |
| 12 | 195.3 | 3.7 | 74 | 141 |  | deliver_package |
| 13 | 248.7 | 16.9 | 960 | 2,462 |  | (reply text only) |

**chair, arm S** (r5_chair_S): 258.2 s = 164.3 s in model calls + 68.1 s in tool calls + 25.8 s elsewhere (waiting, harness).
Tools by time: deliver_package 53.7 s (1); run_build 14.1 s (4); view_file 0.2 s (9); run_command 0.1 s (1).

| call | starts at s | seconds | output | thinking | cache | tool asked for |
|---:|---:|---:|---:|---:|---|---|
| 1 | 0.0 | 2.9 | 207 | 185 |  | view_file, run_command |
| 3 | 5.9 | 56.6 | 5,956 | 6,760 |  | write_to_file |
| 5 | 66.6 | 8.8 | 832 | 599 |  | replace_file_content |
| 7 | 78.7 | 6.1 | 79 | 612 |  | view_file |
| 8 | 84.8 | 24.1 | 7,394 | 89 |  | replace_file_content |
| 9 | 109.0 | 2.8 | 129 | 11 |  | run_build |
| 11 | 121.0 | 30.2 | 6,230 | 1,678 |  | replace_file_content |
| 14 | 161.8 | 4.4 | 72 | 425 |  | deliver_package |
| 15 | 219.9 | 5.1 | 109 | 511 |  | view_file |
| 16 | 225.1 | 8.2 | 794 | 835 |  | (reply text only) |

6 other agent calls: 15.1 s.

**chair, arm V** (r5_chair_V): 380.3 s = 306.5 s in model calls + 117.2 s in tool calls + -43.4 s elsewhere (waiting, harness).
Tools by time: execute_blender_code 111.6 s (8); get_viewport_screenshot 52.4 s (11); run_command 7.4 s (2); get_scene_info 1.3 s (4).

| call | starts at s | seconds | output | thinking | cache | tool asked for |
|---:|---:|---:|---:|---:|---|---|
| 3 | 6.7 | 25.2 | 153 | 3,579 |  | execute_blender_code |
| 4 | 32.0 | 50.6 | 7,982 | 2,789 |  | write_to_file |
| 6 | 90.3 | 7.3 | 316 | 110 |  | execute_blender_code, get_scene_info, get_viewport_screenshot x2 |
| 8 | 102.5 | 50.0 | 8,757 | 3,049 |  | write_to_file |
| 11 | 163.2 | 55.7 | 10,974 | 3,454 |  | write_to_file |
| 13 | 222.2 | 37.0 | 6,437 | 1,915 |  | write_to_file |
| 14 | 259.3 | 5.7 | 107 | 89 |  | execute_blender_code |
| 17 | 312.1 | 5.5 | 265 | 403 |  | view_file x3 |
| 18 | 317.9 | 30.6 | 4,492 | 874 |  | replace_file_content x2 |
| 20 | 364.4 | 6.0 | 704 | 146 |  | get_viewport_screenshot x2, run_command |

10 other agent calls: 32.8 s.

## Checks of this script against the records

- N r5_chair_N (live_N_p1): agrees on 5 figures (record lacks fresh, cache_misses, cache_miss_input)
- N r5_crate_N (live_N_p1): agrees on 5 figures (record lacks fresh, cache_misses, cache_miss_input)
- N r5_hydrant_N (live_N_p1): agrees on 5 figures (record lacks fresh, cache_misses, cache_miss_input)
- N r5_chair_N (live_N_main): agrees on 5 figures (record lacks fresh, cache_misses, cache_miss_input)
- N r5_crate_N (live_N_main): agrees on 5 figures (record lacks fresh, cache_misses, cache_miss_input)
- N r5_hydrant_N (live_N_main): agrees on 5 figures (record lacks fresh, cache_misses, cache_miss_input)
- N r5_chair_N.change (live_N_p1): agrees on 5 figures (record lacks fresh, cache_misses, cache_miss_input)
- N r5_crate_N.change (live_N_p1): agrees on 5 figures (record lacks fresh, cache_misses, cache_miss_input)
- N r5_hydrant_N.change (live_N_p1): agrees on 5 figures (record lacks fresh, cache_misses, cache_miss_input)
- S r5_chair_S (live_S_p1): agrees on 8 figures
- S r5_crate_S (live_S_p1): agrees on 8 figures
- S r5_hydrant_S (live_S_p1): agrees on 8 figures
- S r5_chair_S.change (live_S_p1): agrees on 8 figures
- S r5_crate_S.change (live_S_p1): agrees on 8 figures
- S r5_hydrant_S.change (live_S_p1): agrees on 8 figures
- V r5_chair_V (live_scorecard): agrees on 5 figures (record lacks fresh, cache_misses, cache_miss_input)
- V r5_crate_V (live_scorecard): agrees on 5 figures (record lacks fresh, cache_misses, cache_miss_input)
- V r5_hydrant_V (live_scorecard): agrees on 5 figures (record lacks fresh, cache_misses, cache_miss_input)
- V r5_chair_V.change (live_scorecard): agrees on 5 figures (record lacks fresh, cache_misses, cache_miss_input)
- V r5_crate_V.change (live_scorecard): agrees on 5 figures (record lacks fresh, cache_misses, cache_miss_input)
- V r5_hydrant_V.change (live_scorecard): agrees on 5 figures (record lacks fresh, cache_misses, cache_miss_input)

Invoked as: --arm N=live_N_p1,live_N_main --arm S=live_S_p1 --arm V=round5:live_scorecard --judge S vs V=judge_p1_SV --judge S vs N=judge_p1_SN --judge N vs V=judge_p1_NV --md report/p1.md --json report/p1.json

