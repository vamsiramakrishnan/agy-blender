# Performance results after two rounds of fixes (2026-11-20)

The first version of this report measured the system as it was and listed the defects the measurements exposed.
The second fixed them, and ended with two lists: what the fixes had made worse, and what was still open. This
version covers the round that worked through those two lists.

The rule was the same as before: remove waste and raise quality, and never buy either by cutting capability. No
threshold, gate, resolution, candidate count, thinking level or sample count was lowered in this round.

The first round's report follows this one, unchanged, from "First round" down. The data is in `results/`;
[`README.md`](README.md) says what each file is and how to reproduce every table.

## Summary

### The open items, and what happened to each

| Open after the first round | Now |
|---|---|
| The offline pipeline waited 0.6 to 0.9 s per asset on a headless browser | One browser per process, started while the run works. The in-process pipeline is 1.4 to 1.6 times faster per asset (crate 2.9 to 2.0 s) and offline throughput 1.4 to 1.5 times. A single `agy3d prop --offline` process, which starts its own browser, shows no change beyond noise |
| LOD generation on dense meshes was 9 to 21 times slower than before its error was measured | The measurement stays; the work around it that produced nothing is gone, and the same input still gives the same levels bit for bit: 890 to 510 ms at 49,920 triangles without UVs, 918 to 722 ms with them |
| Authoring requests carried 7,238 input tokens, nearly all of it schema | 5,928, with the same documents accepted and rejected |
| Computed meshes reached up to 5% less texel density | 3 to 6% more than before this round on spheres and 27% more on a torus, with area distortion 2.5 to 6.6 times lower |
| A small computed mesh paid a quarter of a second in the UV packer | 262 to 34 ms at 448 triangles, 285 to 50 ms at 1,920 |
| Convex decomposition of a concave mesh took about 450 ms | 438 to 148 ms for the 1,964-triangle prism, same three hulls |
| Textures were embedded once per GLB file | Far LOD files reference `textures/`: the crate package with six 1,024 px maps went from 69.7 to 49.5 MB |
| Environment-kit authoring was outside the spend ceiling | Admitted against one kit ledger, concurrent, every piece accounted for |
| No Blender and no game engine ran on the benchmark machine | Still none on this machine. CI now times Blender 5.3 and Godot 4.7 against the kernels on every pull request |
| Visual QA ran on the fallback judge; the governed live pipeline was not run | Unchanged. The native Decisions API still returns 404 on the development key |
| The inspector was not tried inside a running Antigravity application | Unchanged |
| Artist-judged quality and dollar cost were not measured | Unchanged |

### Found while fixing them

None of these was on either list. Each was found by measuring something the first round had not measured.

- **The web check could not verify a package with 2K textures**, and took 18 s with 1K ones. It now takes 1.5 s
  with six 1,024 px maps, and 4 to 7 s with six 2,048 px maps on a loaded machine.
- **Collider generation could run away on an imperfect mesh.** A sphere with six faces missing took 98 s; a sphere
  with a four-triangle hole and a flat sheet gave no result in 130 s. They now take 5 ms, 17 ms and 0.16 s, and any
  decomposition that is not known to be bounded has a deadline.
- **LOD generation took 54 s on a table top with 30 small parts on it.** It now takes 0.55 s.
- **The preview rasteriser left one-pixel holes between triangles**, and the LOD pop check failed dense open
  surfaces because of them. Coverage is now exact, and sheets of a few thousand triangles render 20% faster.
- **The LOD pop check lit levels with one normal per triangle**, which no engine does. It now lights each level
  with the normals it ships.
- **A UV seam closed with rounding noise opened into cracks** in far LOD levels.
- **A tube that needed computed charts failed the UV gate**: 0.080 area distortion against a budget of 0.05. It is
  now 0.0000. About half the computed charts were also mirrored.
- **A failed kit piece surfaced as an index error**, and two imports could deadlock each other in a fresh process.

### What got worse

- **Far LOD files are no longer self-contained.** `<asset>_LOD<n>.glb` need the package's `textures/` beside them.
  `<asset>.glb` is unchanged and still stands alone. A package must be copied or imported as a directory.
- **Some far LOD levels of computed meshes have more triangles.** The levels follow the UVs, and the UVs changed.
  On a 10,080-triangle sphere the nearest level went from 3,448 to 2,608 triangles and the farthest from 216 to
  310; on a 49,920-triangle sphere the sixth level, 224 triangles, is gone. Levels of meshes without UV seams are
  unchanged.
- **Computed meshes carry 0 to 15% more seam vertices**, where charts that fill their rectangle poorly are cut.
- **The pop check computes shading normals**: about 10 ms at 2,000 triangles and 200 ms at 40,000, once per level.
- **Scenes of about 500 triangles per view rasterise about 0.1 ms slower per view.** From a few thousand triangles
  per view up, rendering is faster.
- **An open surface still costs 2.5 to 12 s of collider time** (it was 37 to 74 s), and a decomposition that runs
  in a child process pays about 0.15 s to start it.

### What is still open

- A dense curved concave solid still costs CoACD about 1 ms per triangle: 1.9 s for a 1,600-triangle torus. Its
  input could be reduced within a bound far below the collider tolerance. Not done.
- A 10,080-triangle sphere reaches 887 px/m of a requested 1,024. Before the first round it reached 909, with its
  distortion over the budget (0.064 against 0.05); now the distortion is 0.005.
- Far LOD levels compute their shading normals from their own geometry, so a curved mesh can turn faceted at a far
  level. The pop check now sees that; carrying LOD0's normals through simplification would prevent it.
- The LOD generator bounds geometry; the pop check also judges shading. On the 30 subjects measured no level is
  rejected any more, with the torus at 0.057 of an allowed 0.06, but nothing makes that true by construction.
- Shading normals are computed in a loop over vertices, 5 microseconds per triangle, once per level when a package
  is written and now once more in the pop check.
- A one-asset command-line run still starts a browser of its own: 4.7 s, against 4.8 and 5.2 s for the two runs of the
  starting tree. The gain is in processes that verify more than one package.
- The first render of a package with 2K textures takes about a second in the web check, on a software GPU.
- A map in a format glTF cannot reference (anything but PNG or JPEG) is still embedded in every LOD file.
- The child process that bounds a decomposition is killed by process group, which is POSIX only. Unity staging of
  far LOD files is untested: there is no Unity anywhere in this project's CI.
- A governed live kit was not run: it needs configured prices.
- Four timing tests fail when the machine is loaded and pass alone, and the tool-level edit benchmarks vary by up to
  half between two runs of the same code. A fifth, on the kit's concurrency, failed once in CI: it bounded the
  duration of a step that also writes the ledger to disk. It now asserts the order of the calls, which a slow disk
  does not change.
- No Blender and no game engine ran on the benchmark machine, the native Decisions API is unavailable, the
  inspector has not been tried inside a running Antigravity application, and asset quality as an artist would
  judge it and dollar cost are not measured. All as before.

## How the second round was measured

Same machine as before (4 vCPU Intel Xeon @ 2.10 GHz, 15.7 GB RAM, no GPU, Linux 6.18, Python 3.13.18), same
libraries, numeric libraries and OpenUSD pinned to one thread, medians throughout.

**Both sides in one session, with one harness.** "Before" is the tree this round started from (`5a440fa`, the merge
of the first round) and "after" is `5e1e8f9`. Both were measured with the final harness, which has three benchmarks
and several cases the first round did not have, so every row exists on both sides: the before tree from a separate checkout, the
harness pointed at its code. The before tree was run, then the after tree, then the before tree again, with nothing
else running. Each result file records the commit of the code it measured.

**Noise was measured, not assumed.** The second run of the before tree is the same code twice. `compare.py` takes
it as `--repeat` and reports a change only when it is larger than the same-code difference for that case, times a
10% floor. The tool-level edit benchmarks, which go through an event loop and worker threads, are the noisy ones
again: the same code gave 67 and 93 ms for a 200-node edit. The paired run flagged three of their rows as slower
(a 200-node edit at 112 ms). Nothing on that path changed in this round, so those benchmarks were run again alone,
three times per tree, alternating: the medians agree within 5% at 200 nodes and the minimums are equal
(`results/edit_recheck.json`). The table below gives the paired figures as measured and marks those rows.

**Where the paired run has no row,** the text says so. Those figures come from the engineer who did the work:
both trees in separate processes, interleaved, medians with their ranges, on this machine while it was loaded by
the other work (load average 5 to 13 on 4 cores). Times taken that way are higher than on an idle machine and are
labelled; densities, distortions, triangle counts and byte sizes do not depend on load.

**Quality beside every speed.** `quality_facts.py` records UV distortion, texel density and LOD chains for both
trees. For LOD generation, 30 subjects were run through both trees and compared level by level, down to a hash of
each level's points, triangles and UVs. For colliders the harness records hull tightness beside the time.

**Blender and Godot** figures come from the `stock-engines` job on GitHub's runners, not from this machine: see
"Blender and Godot in CI".

**Live figures** come from bounded runs with limits enforced in code, retries off and no prices set. Sample counts
are 1 to 3, so they are observations.

## The second round, item by item

Each item says what was measured first, what the cause was, what changed, and what it measures now. Where a figure
is not in the paired tables further down, the text says how it was taken.

### 1. The web check waited on its own start-up

Every package verification started a Playwright driver (0.32 s) and a Chromium (0.15 s), closed both again (0.1 s),
and recomputed in its page a prefiltered environment map that is the same for every package (0.3 s). The reference
silhouette was rendered before the browser was even started, so nothing overlapped.

A process now keeps one browser and gives each verification a fresh context of its own, with its own request
routing, console, page errors and blocked-request list. A pipeline run starts the browser when the run starts, so
start-up overlaps the earlier passes, and the reference silhouette is rendered on a worker thread while the browser
renders the package. The first verification of a process renders its own environment map, as before; the second
renders its own and hands it back after its frames are read; from the third on, pages load the shared one. A process
that verifies one package therefore does no work it did not do before. The browser stays up after a successful run
and closes after two minutes idle; a failed or cancelled run closes it at once; one that crashed or hangs is
replaced, and killed with its process tree if it will not close.

The checks, the pass rule and the skip reasons are untouched. On the same packages the verdicts and the evidence
are identical: the same 25 checks pass, and the silhouette image, the rendered frame and the texture content
hashes are the same bytes.

### 2. On packages with real textures the web check took tens of seconds, and at 2K it could not finish

Nothing in the first round's benchmarks carried textures larger than 256 px through the web check. With six
1,024 px maps the check took 18 s; with six 2,048 px maps it did not finish in 300 s, and with a longer limit the
browser crashed during page load. Generated textures are 1K to 2K, so this was the normal case.

The cost was not rendering. Every file the page asked for was answered through the browser's request
interception, which passes each response body through the DevTools pipe as base64: a 40 MB GLB took 17 to 22 s
that way. The GLBs were also inlined in the HTML as base64, and every LOD file carried its own copy of every map
(item 6).

Each verification now serves its page directory from a server on 127.0.0.1, under a random 128-bit path, GET
only. Interception still sees every request: one for that server is let through, anything else is aborted and
recorded as blocked, and the server records any request it cannot answer from the page directory the same way, so
a page that reaches for anything else still fails. The GLBs are files beside the page. Each distinct image is
fetched, hashed and decoded once, whichever levels use it; a far LOD gets the image decoded from bytes proven
identical, a file that differs by one byte is decoded and compared on its own, and a missing file still fails.
Far LODs are described but never rendered, so their textures never reach the GPU. A machine that cannot open the
local port gets a skip with that reason, like a machine without a browser.

What remains at 2K is about a second for the first render (uploading six 2,048 px maps and generating their
mipmaps on a software GPU), decoding and hashing the six images, and browser start-up.

### 3. LOD generation spent most of its time on work that produced nothing

The first round made LOD levels provably within one pixel of LOD0, and paid for it: generation on a dense mesh
became 9 to 21 times slower. Profiling showed how much of that was the measurement and how much was waste around
it:

- The reference vertices were deduplicated on every measurement (8 to 13 ms each), the copies of a vertex on a UV
  seam were measured separately, and every shared edge midpoint twice.
- A rejected attempt settled every sample above the budget exactly, although only the largest decides.
- One matrix product ran on the threaded BLAS path at 21 to 32 ms where the same product in blocks takes 1.2 ms.
- The search for parts that may be absent at far levels rebuilt an index of "all other parts" for every candidate
  set.
- The exhaustive pass of the surface index searched one tree of triangle boxes with the largest triangle's
  half-diagonal as the margin. One 2 m table-top triangle made every query return every box.

The last one was not a slowdown but a pathology: an assembly of 30 small parts on a table top took about a minute.
No benchmark had that shape. It now has one.

All of it is removed and the output is unchanged: on 30 subjects (the template props, spheres from 448 to 49,920
triangles with and without seams, a torus, a thin plate, a thin rod, open sheets, seamed tubes and the assembly),
every level has the same triangles, switch distance, recorded deviation and bytes as before. What remains at
49,920 triangles is the simplifier's own 14 runs, about half the time, and 16 distance passes, about 40%: one per
simplification attempt, not one per level as the first report said.

A level's recorded deviation is a bound, not the largest distance sampled: a sample is settled exactly only when
it could exceed the budget. The field said "measured"; it now says what it is.

### 4. Computed-mesh UVs paid for two packings, then for a projection

**Packing.** A mesh that needs computed charts went through xatlas at the requested density, and again when the
surface overflowed the atlas. Packing cost follows texel area, not triangle count: a 448-triangle sphere spent
162 ms and 113 ms packing charts that take 3 to 7 ms to compute, and the first packing was thrown away. xatlas's
placement fills 0.55 to 0.63 of the square, which is why a sphere overflowed at all. It also rescales each chart to
whole texels, so the density it reported was not the density every chart had.

xatlas now computes the charts once, at 64 texels across, and nothing else. The rescale is undone exactly, every
chart is scaled to its surface area, and computed charts go through the same rectangle packer as the exact charts
of primitives. A group that mixes both is no longer repacked, and its exact charts no longer distorted, by xatlas.

**Flattening.** xatlas flattens 95% or more of its charts by orthographic projection, and the area distortion of
a projection is the cosine of the tilt. A quarter of a cylinder came out at 0.080 against the pipeline's budget of
0.05, so a tube that needed computed charts failed the UV gate; a sphere cap at the curvature bound measured 0.015.
About half the charts came back mirrored.

Each chart is now re-flattened inside the border xatlas gave it, as rigidly as possible, starting from that
projection. A chart that can be unrolled converges to its exact unrolling; a doubly curved one gets close to the
least distortion one flat chart of it can have. Mirrored charts are mirrored back, so every computed chart has the
same handedness. A flattened chart is used only if it is one-to-one (no triangle flipped, no two border edges
touching), decided exactly; otherwise the chart keeps the projection. None needed to on any subject measured.

Area distortion, before this round and now: both tubes 0.080 to 0.0000; spheres 0.014-0.018 to 0.003-0.006; torus
0.027 to 0.004; an irregular blob, an open sheet and nested shells 0.014-0.015 to 0.002-0.003. Texel density, in
pixels per metre at a requested 1,024: spheres of 448 / 1,920 / 10,080 / 49,920 triangles 888 / 892 / 864 / 857 to
922 / 930 / 887 / 906; torus 720 to 913; both tubes from about 720 to the full 1,024. (Interleaved runs on a
loaded machine; the densities and distortions do not depend on load.)

The curvature bound on chart size was measured again with the new flattening, from half to twice its value and
without it. Larger charts would save 4 to 20% of seam vertices, but density does not follow chart size and
xatlas's chart growing takes 2 to 5 times as long, so it stays.

### 5. Colliders decomposed the tessellation, not the solid

CoACD is single-threaded and its time follows the triangle count of its input: the same concave prism at 764,
1,964 and 3,964 triangles took 294, 416 and 683 ms. A modelled solid is often a few dozen flat faces cut into
thousands of triangles; that prism is 28 triangles once its coplanar triangles are merged, with the same volume to
twelve digits.

A closed mesh now reaches CoACD with coplanar triangles merged. The merged mesh is used only if it is still closed
and manifold, has no degenerate face and the same volume; curved meshes skip the step and their hulls are
bit-identical. A closed mesh that holds several separate bodies used to go through CoACD's repair pass, which
re-meshes the surface; its bodies are now decomposed one at a time.

On the convex fast path about 90% of the time is the hull computation itself. Nothing was removed there.

Running colliders on a second thread beside the unwrap was tried and not shipped: in about 860 such runs the
output never differed, but one crashed inside the hull library while the other thread was in xatlas, and the gain
was 0 to 15%.

### 6. Collider generation could run away on an imperfect mesh

Found while measuring item 5. Given a mesh that is not one closed solid, CoACD's own repair pass rebuilds the
surface at a fixed resolution. The repair is cheap; what it builds is a dense double-walled shell, and decomposing
that is not. Two cubes sharing one edge, 24 triangles, became 62,012 triangles. A sphere with six faces missing
became 177,128. A sphere with a four-triangle hole, a flat sheet and two voxel shapes whose surface touches itself
gave no result in 130 s. Nothing bounded the call, so one such mesh stalled a run, and an imported mesh with a
small hole is ordinary input.

The collider input, which is a private copy, is now repaired the way such a mesh needs: welded, duplicate faces
dropped, winding made consistent, bodies pulled apart where they only touch, and a hole capped when the cap is at
most 5% of the body's surface (a few missing faces are; a cup's mouth is not). A body that is closed after that
takes the normal path. An open surface is made a thin solid and decomposed with the repair pass off. A surface
that still touches itself gets the hull of its body.

A decomposition that is not known to be bounded runs in a child process with one budget: 30 s, or three times the
slowest legitimate time measured for the triangle count if that is longer. When it expires, or the child crashes,
the body gets its hull, and the result says so in the tool output, the pipeline step, the export cache and the
manifest. Clean closed bodies under 25,000 triangles still run in-process, with no process start.

Coverage of the mesh by its hulls is equal or better on every subject. In 60 random imperfect inputs the longest
run was 30.2 s, the deadline, and every result covered its mesh.

### 7. Far LOD files each carried every map

A package held every map once in `textures/`, once in the USDZ and once more inside every GLB. `<asset>.glb`, LOD0,
stays self-contained and byte-identical: it is the one file that may be handed over alone. `<asset>_LOD<n>.glb`
now reference the maps as `textures/<file>`, which glTF allows in a GLB, with the same texture, sampler and
material tables.

The layout is part of the package contract and is checked where a package is checked. The manifest (now
`schema_version` 2) records, per GLB and image, whether it is embedded or referenced, its hash, and the texture
file with those bytes. Package validation fails a far LOD whose reference is missing, leaves `textures/` or has
other content than recorded, and a level that binds other image content than LOD0 in any material slot. The
three.js check compares each level's materials with LOD0's down to a hash of the decoded pixels. The Godot check
counts the images the file declares against those that loaded. CI requires the Godot import of far LODs from a
relocated package and the browser tests for a present and a missing texture. A package written before this change
is still judged by the layout its own manifest declares.

### 8. The preview rasteriser left holes, and the LOD pop check read them as pops

Found while reviewing item 3: on some stress subjects the generator emitted levels its own check rejected. A flat
sheet of 3,200 triangles and its two-triangle LOD1 cover exactly the same rectangle, yet the check reported a
silhouette jump of 95 px.

The rasteriser evaluated its weights in single precision from absolute pixel coordinates. Their constant term has
the size of the frame squared over the triangle's area, so for a triangle of a few pixels in a 700 px frame the
weights were wrong in the third digit, against an inside tolerance in the fifth: a pixel centre near a shared edge
could be rejected by both triangles. The dense sheet's render had six one-pixel holes, and the check measures the
distance between silhouette boundaries, holes included. Over a sweep of dense sheets, views and sizes there were
about 8,000 such holes. On a closed mesh a hole shows the back surface, so the mask was right and the colour,
depth and id buffers were wrong there. In the pipeline the pop check is a gate, so a dense open surface (a banner,
a leaf card, cloth) failed verification for a defect that existed only in the preview renderer.

Coverage is now exact. A pixel centre is covered when it lies strictly inside the triangle, or on its boundary
and a tie rule that depends only on the edge's direction gives it to that triangle. Every decision is the exact
sign of the edge function, so two triangles sharing an edge evaluate the same value with opposite signs and
exactly one takes a centre on it; the same holds around a vertex. The evaluation is still single precision,
relative to each triangle's tile and with a rigorous error bound, and only a value within that bound of zero is
decided exactly: on a 49,920-triangle sphere at 1,024 px, 255 of about a million.

It is also faster, because the per-triangle setup is done once and overlapping fragments are resolved with a
min/max pass instead of a sort. The LOD chains of all 30 subjects are unchanged.

### 9. A seam closed with rounding noise opened into cracks

One stress subject still failed after item 8, and its holes were real. Its UV seam was closed with the sine and
cosine of 2 pi, so the two copies of each seam vertex differed by 2.4e-16. The simplifier joins the copies of a
seam vertex by bitwise equality of their positions; these were collapsed separately, and the seam opened into
hairline cracks in the far levels.

The simplifier now gets every copy at the position of the first. A mesh whose copies are already identical is
unaffected: 28 of the 30 chains are bit-identical. The seamed tube goes from four levels to six with no open edge
along the seam.

### 10. The pop check judged a picture no engine shows

The last failing subject was a torus, on shading at its last switch: 0.0675 against a limit of 0.06. The check lit
both levels with one normal per triangle. A package ships each level with crease normals, smooth across faces
within 40 degrees, and both of those levels ship smooth. The check was also blind the other way round: a level
coarse enough to turn faceted was compared as if neither level had normals at all.

The rasteriser can now light a mesh per pixel from per-corner normals, and the check passes each level the normals
it ships, computed by the same function the GLB and USD writers use. Previews keep one normal per triangle, which
is what a preview is for. The thresholds are unchanged. The torus now measures 0.057.

With items 8 to 10, all 30 stress subjects pass the check. The generator's own rule still bounds geometry only;
no subject measured is left on which it emits a level the check rejects.

### 11. Authoring requests carried the schema's commentary

The first round made authoring calls return valid scenes by sending a response schema, which raised the input from
546 to 7,238 tokens per call. The schema carried every field's title and description, written for maintainers, and
repeated the same shapes inline.

Titles and descriptions are no longer sent, repeated shapes are defined once and referenced, and the guidance a
model needs is in the instruction text. The schema went from 7,954 to 6,064 bytes, and the old and new forms
accept and reject the same 119 scene documents and 51 patch documents. Live, input per call fell from 7,238 to
5,928 tokens. In a paired check on the smallest model, three requests each, the old schema returned scenes of 10,
9 and 9 parts and the new one 14, 14 and 13, all valid. Three samples show that nothing was lost, not that
anything was gained.

### 12. Environment-kit authoring was outside the spend ceiling

A live kit authored its pieces one after another through stage sessions that reserved nothing against the spend
ceiling; only its images were governed. A failed piece surfaced as the first exception, or as an index error when
the author returned nothing.

The kit now binds a live author to one ledger, as the prop pipeline does. Its ceiling is the plan's per-asset
ceiling times the number of pieces plus one for the shared trim sheet. Every authoring call reserves its worst
case, waits for admission, books real usage and leaves a receipt. Pieces are authored concurrently and indexed by
piece number, so the kit's files do not depend on which call finishes first. A piece that failed on something a
second request can fix is requested once more; if a piece is still missing the step fails and names every piece's
outcome. A governed live kit refuses to start when no text prices are configured, before anything is requested.
Offline output is byte-identical.

A governed live kit was not run: it needs configured prices, and this work does not set any.

### 13. Three small costs from the first round were traced, and are what they buy

Import time rose by 7 to 10 ms, which is the larger material class (the glTF parameter set). The first read after
a rollback is slower because each prim carries more authored data; the mesh extraction itself is not slower. The
compliance check costs 0.1 to 0.2 ms more, which is the material checks. Nothing was changed.

## Offline numbers, second round

Medians. "Before" is `5a440fa`, "after" is `5e1e8f9`, both measured in one session with the same harness. The noise
band is the difference between two runs of the before tree for that case, times a 10% floor; a change inside it is
"within noise". `results/compare.md` has every case, including the ones left out here.

### Geometry export

| Operation | Case | Before | After | Change | Noise band |
|---|---|---:|---:|---|---:|
| UV + LOD + colliders | sphere, 448 triangles | 254 ms | 47.5 ms | 5.3x faster | 15% |
| UV + LOD + colliders | sphere, 1,920 triangles | 353 ms | 100 ms | 3.5x faster | 12% |
| UV + LOD + colliders | sphere, 10,080 triangles | 821 ms | 444 ms | 1.9x faster | 12% |
| UV + LOD + colliders | sphere, 49,920 triangles | 3593 ms | 3310 ms | within noise | 14% |
| UV + LOD + colliders | concave prism, 1,964 triangles | 558 ms | 156 ms | 3.6x faster | 11% |
| UV + LOD + colliders | crate, 12 boxes, 144 triangles | 120 ms | 86.8 ms | 1.4x faster | 15% |
| UV + LOD + colliders | barrel, 4 parts, 320 triangles | 79.9 ms | 76.1 ms | within noise | 11% |
| UV unwrap | sphere, 448 triangles | 262 ms | 33.6 ms | 7.8x faster | 12% |
| UV unwrap | sphere, 1,920 triangles | 285 ms | 49.9 ms | 5.7x faster | 18% |
| UV unwrap | sphere, 10,080 triangles | 562 ms | 274 ms | 2.1x faster | 22% |
| UV unwrap | sphere, 49,920 triangles | 2248 ms | 2348 ms | within noise | 20% |
| UV unwrap | concave prism, 1,964 triangles | 70.1 ms | 14.3 ms | 4.9x faster | 23% |
| LOD levels, with UVs | sphere, 448 triangles | 13.8 ms | 10.9 ms | 1.3x faster | 11% |
| LOD levels, no UVs | sphere, 448 triangles | 9.5 ms | 9.5 ms | within noise | 12% |
| LOD levels, with UVs | sphere, 1,920 triangles | 54.9 ms | 36.9 ms | 1.5x faster | 16% |
| LOD levels, no UVs | sphere, 1,920 triangles | 36.1 ms | 30.3 ms | 1.2x faster | 11% |
| LOD levels, with UVs | sphere, 10,080 triangles | 224 ms | 141 ms | 1.6x faster | 15% |
| LOD levels, no UVs | sphere, 10,080 triangles | 159 ms | 88.9 ms | 1.8x faster | 11% |
| LOD levels, with UVs | sphere, 49,920 triangles | 918 ms | 722 ms | 1.3x faster | 20% |
| LOD levels, no UVs | sphere, 49,920 triangles | 890 ms | 510 ms | 1.7x faster | 30% |
| LOD levels, with UVs | concave prism, 1,964 triangles | 8.8 ms | 9.8 ms | within noise | 22% |
| LOD levels, no UVs | concave prism, 1,964 triangles | 6.5 ms | 7.0 ms | within noise | 12% |
| LOD levels, assembly (one sample) | table top with 30 small parts, 5,028 triangles | 54.5 s | 546 ms | 99.9x faster | 13% |
| Colliders, convex | sphere, 448 triangles | 1.7 ms | 1.7 ms | within noise | 13% |
| Colliders, convex | sphere, 1,920 triangles | 6.2 ms | 5.0 ms | 1.2x faster | 12% |
| Colliders, convex | sphere, 10,080 triangles | 40.9 ms | 40.8 ms | within noise | 11% |
| Colliders, convex | sphere, 49,920 triangles | 281 ms | 237 ms | 1.2x faster | 15% |
| Colliders, concave | concave prism, 1,964 triangles | 438 ms | 148 ms | 3.0x faster | 13% |
| Colliders, concave | three closed bodies in one mesh, 1,940 triangles | 838 ms | 260 ms | 3.2x faster | 16% |
| Colliders, imperfect input (one sample) | two cubes sharing an edge, 24 triangles | 6443 ms | 2.9 ms | 2,200x faster | 13% |
| Colliders, imperfect input (one sample) | sphere with six faces missing, 394 triangles | 98.2 s | 5.3 ms | 18,000x faster | 13% |
| Colliders, imperfect input (one sample) | open cup, 72 triangles | 37.2 s | 2499 ms | 14.9x faster | 18% |

### Rendering and the edit loop

The three rows marked "not confirmed" were re-measured alone, three alternating runs per tree, and show no
difference (`results/edit_recheck.json`).

| Operation | Case | Before | After | Change | Noise band |
|---|---|---:|---:|---|---:|
| Four-view sheet | 960 triangles, 256 px per view | 58.6 ms | 51.0 ms | within noise | 25% |
| Four-view sheet | 960 triangles, 512 px per view | 203 ms | 153 ms | 1.3x faster | 21% |
| Four-view sheet | 5,280 triangles, 256 px per view | 111 ms | 91.0 ms | 1.2x faster | 10% |
| Four-view sheet | 5,280 triangles, 512 px per view | 293 ms | 232 ms | 1.3x faster | 10% |
| Four-view sheet | 20,160 triangles, 256 px per view | 303 ms | 289 ms | within noise | 15% |
| Four-view sheet | 20,160 triangles, 512 px per view | 491 ms | 437 ms | within noise | 32% |
| Marked sheet | 960 triangles | 131 ms | 147 ms | within noise | 16% |
| Marked sheet | 5,280 triangles | 169 ms | 144 ms | within noise | 20% |
| Marked sheet | 20,160 triangles | 212 ms | 195 ms | within noise | 13% |
| Edit to pixels | 5 nodes | 41.0 ms | 45.4 ms | within noise | 14% |
| Edit to pixels | 20 nodes | 60.4 ms | 69.4 ms | 1.1x slower (not confirmed) | 13% |
| Edit to pixels | 50 nodes | 107 ms | 107 ms | within noise | 23% |
| Edit to pixels | 200 nodes | 372 ms | 326 ms | within noise | 27% |
| Edit tool | 5 nodes | 5.1 ms | 5.9 ms | 1.2x slower (not confirmed) | 15% |
| Edit tool | 20 nodes | 11.0 ms | 12.8 ms | within noise | 22% |
| Edit tool | 50 nodes | 20.5 ms | 23.9 ms | within noise | 62% |
| Edit tool | 200 nodes | 67.0 ms | 112 ms | 1.7x slower (not confirmed) | 52% |
| Edit + QA in the kernel | 5 nodes | 2.6 ms | 2.6 ms | within noise | 10% |
| Edit + QA in the kernel | 20 nodes | 7.0 ms | 7.1 ms | within noise | 30% |
| Edit + QA in the kernel | 50 nodes | 15.5 ms | 15.8 ms | within noise | 12% |
| Edit + QA in the kernel | 200 nodes | 60.7 ms | 66.2 ms | within noise | 19% |

### Packaging and verification

| Operation | Case | Before | After | Change | Noise band |
|---|---|---:|---:|---|---:|
| Write package | crate, no textures | 75.0 ms | 75.6 ms | within noise | 15% |
| Write package | crate, 256 px maps | 129 ms | 134 ms | within noise | 14% |
| Write package | barrel, no textures | 50.5 ms | 52.4 ms | within noise | 12% |
| Write package | barrel, 256 px maps | 95.4 ms | 103 ms | within noise | 17% |
| Write package (one sample) | crate, six 1,024 px maps | 806 ms | 787 ms | within noise | 15% |
| Validate package | crate | 18.9 ms | 18.0 ms | within noise | 13% |
| Validate package | barrel | 14.6 ms | 15.5 ms | within noise | 13% |
| Web check, browser already running | crate, no textures | 1416 ms | 736 ms | 1.9x faster | 21% |
| Web check, browser already running | crate, six 1,024 px maps | 18.4 s | 1463 ms | 12.6x faster | 20% |

### Pipeline, command line and batch

| Operation | Case | Before | After | Change | Noise band |
|---|---|---:|---:|---|---:|
| Offline pipeline, in process | crate, governed, cache cold | 2920 ms | 2022 ms | 1.4x faster | 12% |
| Offline pipeline, in process | crate, governed, cache warm | 2543 ms | 1824 ms | 1.4x faster | 13% |
| Offline pipeline, in process | barrel, governed, cache cold | 3255 ms | 2279 ms | 1.4x faster | 15% |
| Offline pipeline, in process | barrel, governed, cache warm | 2782 ms | 1880 ms | 1.5x faster | 15% |
| Offline pipeline, in process | table, governed, cache cold | 2450 ms | 1535 ms | 1.6x faster | 22% |
| Offline pipeline, in process | table, governed, cache warm | 2093 ms | 1294 ms | 1.6x faster | 16% |
| Offline pipeline, in process | lamp post, governed, cache cold | 2712 ms | 1998 ms | 1.4x faster | 11% |
| Offline pipeline, in process | lamp post, governed, cache warm | 2446 ms | 1638 ms | 1.5x faster | 11% |
| Offline pipeline, in process | shelf, governed, cache cold | 2605 ms | 1627 ms | 1.6x faster | 24% |
| Offline pipeline, in process | shelf, governed, cache warm | 2215 ms | 1368 ms | 1.6x faster | 19% |
| `agy3d prop --offline` | crate | 5215 ms | 4686 ms | within noise | 18% |
| `agy3d models` | - | 654 ms | 654 ms | within noise | 18% |
| 12 props, offline | 1 worker process | 34.5 s | 24.7 s | 1.4x faster | 15% |
| 12 props, offline | 2 worker processes | 20.6 s | 15.2 s | 1.4x faster | 12% |
| 12 props, offline | 4 worker processes | 18.3 s | 12.4 s | 1.5x faster | 40% |
| Batch sanitize, 24 meshes | 1 job | 37.1 s | 29.9 s | 1.2x faster | 13% |
| Batch sanitize, 24 meshes | 2 jobs | 18.9 s | 15.5 s | 1.2x faster | 12% |
| Batch sanitize, 24 meshes | 4 jobs | 14.6 s | 11.5 s | 1.3x faster | 24% |

### What the export produces

From `quality_facts.py` on both trees (`results/before/quality.jsonl`, `results/quality.jsonl`). The five template
props are unchanged in every field: their charts are exact and their levels are the same. The computed spheres:

| Sphere | Area distortion | Angle distortion | Texel density (px/m of 1,024) | LOD levels (triangles) |
|---|---|---|---|---|
| 448 triangles | 0.0151 to 0.0027 | 0.0296 to 0.0062 | 887.5 to 922.3 | [448, 242] to [448, 238] |
| 1,920 triangles | 0.0148 to 0.0059 | 0.0292 to 0.0089 | 891.8 to 930.0 | [1920, 1166, 672, 298] to [1920, 1346, 522, 370, 270] |
| 10,080 triangles | 0.0142 to 0.0046 | 0.0263 to 0.0056 | 863.8 to 887.4 | [10080, 3448, 1902, 690, 216] to [10080, 2608, 1862, 698, 310] |

The levels of a mesh with UVs follow its UVs, and the UVs changed, so those chains moved: some levels are smaller
and some larger, and the 1,920-triangle sphere gained one. The chains of the same spheres without UVs are
bit-identical before and after.

## Blender and Godot in CI

The benchmark machine has no Blender and no game engine. `perf_engines.py` now runs in the `stock-engines` job on
every pull request, after the engine tests, against Blender 5.3.0 and Godot 4.7.2, on GitHub's hosted runners. Four
of its runs were read during this round; they came from two CPU models and took 49 to 59 s each. The figures below
are from the run on the final code (`results/engines_ci.json`; AMD EPYC 9V74, 4 CPUs). Compare the two sides
within that file, not that file with this machine.

**The Blender worker pool.** A cold start, from spawning the process to the first answered ping, takes 0.44 s
(0.38 s on the other runner). A warm ping takes 0.07 ms. Importing a written package's USD takes 4 to 5 ms.
Rendering the crate at 512 px and 16 samples takes 138 ms with Workbench (485 ms on the first call), 3.5 s with
Cycles (1.1 s on the other runner), and 11.8 s with EEVEE. The runner has no GPU, so EEVEE renders through software
GL; that figure says nothing about a workstation.

**Godot.** The package check takes 157 to 159 ms, of which 155 ms is the engine starting and quitting: the glTF
import itself is 2 to 3 ms for these packages. A capture takes 0.61 s.

**Blender against the kernels on the same meshes.** The Blender side has two times: the operator alone, timed
inside Blender, and the whole call per repetition, which is what a pipeline calling Blender would pay.

| Operation | Mesh | Kernel | Blender, whole call | Blender, operator alone | What each produced |
|---|---|---:|---:|---:|---|
| UV unwrap | sphere, 1,920 triangles | 45 ms | 27 ms | 1.6 ms | kernel 39 islands, area distortion 0.006; Blender 8 islands, 0.105 |
| UV unwrap | sphere, 10,080 triangles | 231 ms | 215 ms | 7.7 ms | kernel 45 islands, 0.005; Blender 6 islands, 0.104 |
| UV unwrap, as the pipeline runs it | crate, 12 boxes | 32 ms | 22 ms | 2.1 ms | both zero distortion; the kernel also packs the parts into one atlas at a set density |
| LOD levels | sphere, 1,920 triangles | 27 ms | 36 ms | 6.0 ms | same triangle counts; the kernel measures each level's deviation |
| LOD levels | sphere, 10,080 triangles | 78 ms | 286 ms | 47 ms | same |
| Convex hull | sphere, 10,080 triangles | 29 ms | 195 ms | 13 ms | 5,042 and 5,034 hull vertices |
| Four-view sheet, 256 px | sphere, 10,080 triangles | 64 ms | 384 ms | 201 ms | covered pixels agree within 2% |

What the table shows, and what it does not:

- **Blender's operators are fast.** Smart UV Project unwraps a 10,080-triangle sphere in 8 ms and the hull
  operator takes half the kernel's time. Most of a Blender call is getting the mesh in and the result out.
- **Per call, Blender's unwrap is faster on the small sphere and level on the large one; on levels, hulls and sheets
  the kernel is 1.3 to 7 times faster.** At the start of this round the kernel's unwrap of those two spheres took
  227 and 438 ms in the same job.
- **The two unwraps are not the same work.** Smart UV Project leaves 0.10 area distortion on a sphere, twice the
  pipeline's budget of 0.05, and does no density fit and no shared atlas. The kernel's figure is 0.005. Both are
  computed by the same formula from the UVs each side wrote.
- **The angle-based unwrap is a weak comparison** and is left out of the table: without seams it returns a sphere
  as 1,802 islands.
- **None of this is the pipeline with Blender in it.** The pipeline does not call Blender for these operations.
  The table says what replacing a kernel with a Blender call would cost and buy.

The first run of the benchmark measured the pool, the renders, the hulls and the sheets, and failed every unwrap
and decimation: its own scripts read Blender's object list right after removing an object, and Blender still lists
the removed one with nothing behind it. That was a defect of the benchmark, fixed before any figure was used.

## Live calls spent in the second round

| What | Calls | Tokens |
|---|---:|---:|
| Probe of the native Decisions API (still 404) | 1 | 0 |
| Stage runs re-measured on the builder tier | 3 runs, 13 model calls | 40,706 |
| Authoring schema: old and new form | 8 | 81,161 |
| Paired check of the two schema forms on the smallest tier | 6 | about 53,100 |

The stage runs took 26.0 s and 37.6 s for a crate and 32.9 s for a flush repair, on a provider that was answering
normally; all three were valid and approved by QA. No price was read or set.

## First round

What follows is the previous version of this report, as published after the first round of fixes (2026-11-20), with
its headings one level down and nothing else changed. Its figures were measured in that round's session and are not
comparable with the tables above. Its two closing lists, "What got worse" and "Not fixed, and not measured", are the
lists the second round worked through; the table at the top of this document says what became of each item. Its
data is in `results/round1/`.

The first version of that report measured the system as it was (commit `14814fd`) and listed the defects the
measurements exposed; none of them was changed at the time. The version below covers what came next: each defect
was traced to its cause and fixed, more turned up along the way and were fixed too, and everything was measured
again.

### Summary

What someone making assets has now that they did not have before:

- **Authoring calls return scenes.** Live, 10 of 10 one-shot authoring requests returned a scene that passes the
  geometry checks, on all four model tiers. Before, 1 of 10 did: the two larger models spent their whole output
  allowance thinking and returned three tokens.
- **A 1K texture request returns a texture**, and image spend is booked from what the provider reports.
- **Stage agents send the asset task, not a coding-assistant prompt.** Prompt tokens per turn fell from about
  21.8k to about 7.7k on the same task, with the same number of model calls and the same result.
- **UVs of primitives have no distortion.** The lamp post went from 0.083 area distortion, which is over the 0.05
  budget, to zero, and primitives reach the same or a higher texel density than before.
- **Unwrapping no longer dominates export.** A crate exports in 108 ms instead of 231, a 50,000-triangle mesh in
  3.4 s instead of 17.5.
- **LOD levels are bounded by what a viewer can see**: one pixel on a 1080-pixel frame, measured. Levels that
  were copies of LOD0, and levels that collapsed thin parts, are gone; three of the five template props, which
  had nothing real to remove, now ship LOD0 alone.
- **Packages with textures write 7 to 20 times faster**, are byte-identical from run to run, and no longer carry
  the author's local paths.
- **Every exported package is loaded in three.js** and checked against its manifest and its USD silhouette. Until
  then nothing in the pipeline had loaded the shipped GLB in any runtime. The first time it was loaded, it had no
  vertex normals.
- **The review inspector shows the GLB that ships.** It is an MCP App, and a reviewer's notes are now stored with
  the asset session where the agent can read them.
- **Materials cover the glTF PBR set**: emission, alpha modes, double-sided, clearcoat, transmission, index of
  refraction, sheen and specular, from the scene description through USD and GLB into the web runtime.
- **Batch sanitize uses the cores it is given**: 52 to 108 meshes per minute at four jobs. A crash in a native
  library now fails one file, not the batch.

What got worse (details under "What got worse"):

- **The offline pipeline, by 0.6 to 0.9 s per asset**, because the three.js check waits on a headless browser.
  Offline throughput on fixtures is a quarter lower. The pipeline's own CPU per asset fell by 20 to 40% at the
  same time. In a live run the step is a second or two beside minutes of model time.
- **LOD generation on dense single meshes**, 9 to 21 times, because the error is now measured: 98 ms to 914 ms
  at 50,000 triangles. The export of that mesh as a whole is still 5 times faster.
- **Authoring requests carry about 6.7k tokens of schema.** On the smallest model the call takes the same 12.5 s
  as before; the difference is that the answer is valid.
- **Computed meshes reach up to 5% less texel density**, because bounding the curvature a chart may flatten means
  more charts and more gutter. Their distortion went from over the budget to well inside it.

What is still open (details under "Not fixed, and not measured"):

- A small computed mesh still pays about a quarter of a second in the UV packer.
- Convex decomposition of a concave mesh still takes about 450 ms.
- Visual QA still runs on the fallback judge at 16 to 22 s per call, because the native Decisions API returns 404
  on the development key.
- No Blender and no game engine ran on the benchmark machine. Those paths run in CI.

### How the comparison was made

Same machine as the first report (4 vCPU Intel Xeon @ 2.10 GHz, 15.7 GB RAM, no GPU, Linux 6.18, Python 3.13.18;
usd-core 26.11, trimesh 5.2.1, xatlas 0.0.11, meshoptimizer 0.2.30a0, coacd 1.0.14, numpy 2.5.5; google-genai
3.6.1, google-antigravity 0.1.24), same harness, numeric libraries and OpenUSD pinned to one thread, medians
throughout.

**The machine drifted, so both sides were measured together.** The pre-fix code, run again three to four hours
after the first report's run, was slower on most benchmarks: 4 to 6% at the median, more than 10% on about a
quarter of the cases, and up to 1.4 times. Comparing the fixed code with the first report's numbers would have
blamed the fixes for the machine. Every offline "before" figure below is therefore the pre-fix tree (`14814fd`,
in a separate checkout with its own harness) measured in the same session as the final tree (`f9e039a`), on the
idle machine: 23 of its 35 benchmarks immediately before the final tree's run and the other 12 immediately after.
The split was not planned. The pre-fix run ended with a segmentation fault as it entered the export-cache
benchmark, in native code that ran without incident in three other runs of the same tree that day. Commits after
`f9e039a` change timeouts, error handling and what the manifest records, not the measured paths.

**Noise was measured, not assumed.** A second full run of the pre-fix code, an hour earlier, shows what the same
code does twice: 86% of the cases agree within 10%, and the tool-level edit benchmarks differ by up to 26%.
`compare.py` takes that run as `--repeat` and reports a change only when it is larger than the same-code
difference for that case, times a 10% floor. One repeat is a lower bound on noise, so the rows that only just
cleared their band were checked against the other runs of the day; where that changes the reading, the text says
so.

**Live figures** come from one bounded run of `perf_live.py` on the fixed code (`acfc094`): 23 counted calls, 36
requests to the API, limits enforced in code, retries off, no prices set. The live "before" figures are the first
report's. Sample counts are 1 to 3, so these are observations, not distributions.

### The defects, their causes, and what changed

#### 1. Authoring requests returned nothing usable

*Finding.* One of ten one-shot authoring requests produced a valid scene. Carbon and Argon returned 3 output
tokens after 4,093 thinking tokens. Neon's JSON was cut off. Two of three Hydrogen answers had a string where a
boolean belongs.

*Cause.* On these models thinking counts against `max_output_tokens`: in every cut-off response, answer plus
thinking equalled the cap exactly (4,096). The cap had been sized for the answer alone. A cut-off answer was then
parsed as if it were complete, and re-asked at the same budget. The scene structure was requested in prose, with
no schema.

*Change.* Each tier's cap is sized from measurements as answer plus thinking: 16,384 tokens for the two small
tiers, and the model's maximum of 32,768 for the two large ones. A cut-off or empty response is its own typed
outcome that carries the finish reason and the billed usage, and is never parsed or blindly re-asked. Requests
carry a response schema derived mechanically from the scene model, so prose, schema and parser cannot drift
apart. Thinking levels are unchanged.

| Tier (model, thinking) | Before | After | After: wall, median (range) | After: answer + thinking tokens |
|---|---|---|---|---|
| worker (Hydrogen, low) | 1 of 3 valid, 12.5 s | 3 of 3 valid and passing the geometry checks | 12.5 s (12.2–13.8) | 630–1,553 + 1,066–1,263 |
| bulk (Neon, low) | 0 of 3, cut off, 30 s | 3 of 3 | 36 s (30–39) | 1,747–1,919 + 1,410–2,418 |
| builder (Carbon, medium) | 0 of 3, 3 tokens of answer, 55 s | 3 of 3 | 209 s (203–285) | 9,965–10,286 + 9,289–9,846 |
| director (Argon, high) | 0 of 1, 3 tokens of answer, 112 s | 1 of 1 | 262 s | 17,634 + 7,893 |

The larger models do not write the same scene more slowly. For "wooden crate with iron corner brackets", Hydrogen
authored 9 to 13 parts, Neon 25 or 26, Carbon 123 to 131 and Argon 168. Input per call went from 546 to 7,238
tokens, nearly all of it the schema.

Three things follow from honest budgets and were fixed with them:

- **Candidates are not dropped silently.** A plan's best-of-N used to shrink without a word when N worst-case
  reservations did not fit under its ceiling at once (the hero plan asks for 8 director candidates). Admission
  now waits for budget that settling calls release, and every requested candidate ends with a recorded outcome.
- **A failed repair no longer ends the run.** It is a recorded stop reason, with one retry on the director tier
  where the plan allows it.
- **Timeouts follow the measurements.** They are per tier, at least twice the slowest call measured for that
  tier, and a test holds them to that rule. The old single 300 s was within reach of a healthy Argon call. The
  run in this report produced the slowest calls yet (Carbon 285 s, Argon 262 s), so the limits were raised again
  after it, to 570 s and 540 s. During the run they were 420 s and 480 s, and no call reached them.

One limit cannot be engineered away: Carbon and Argon cap output at 32,768 tokens, and the largest calls seen
used 25,433 and 25,527 of them. A scene that needs more comes back as a typed "cut off" outcome with its usage,
not as a parse error.

#### 2. A 1K texture request returned no image

*Cause.* The same budget error. At 1K the image alone is 1,120 tokens, and the call had already used 1,038 of
its 2,048 on thinking and text. Receipts fell back to an estimate because the provider itemises only the image
tokens and the code required the items to add up to the total.

*Change.* The cap is the image's own tokens plus measured headroom. A missing image is a typed outcome. Receipts
are booked from provider usage, with the unitemised part recorded as such.

| Size | Before | After |
|---|---|---|
| 1K | no image after 9.7 s | 1024×1024 in 12.4 s; 1,402 output tokens (1,120 image), 544 thinking; receipt from provider usage |
| 2K | an image, with a receipt from an estimate | 2048×2048 in 20.9 s; 2,001 output tokens (1,680 image), 804 thinking; receipt from provider usage |

#### 3. Stage agents re-sent a prompt that was 90% unrelated to the task

*Finding.* A stage agent sent about 7.2k prompt tokens on every model call. About a tenth of that described the
asset task.

*Cause.* Read from the harness's own saved request: the SDK appends a plain-string instruction to its default
coding-assistant prompt (identity, markdown and LaTeX style, how to read transcript files). Subagents were
enabled on every stage, which brings four tools and two prompt sections with them. A file viewer was enabled
that no stage uses. Tool arguments were JSON inside a string, with no schema.

*Change.* Stage agents give the harness their own instructions and enable only what the stage uses. Subagents
stay in the two stages where they do something the stage's own tools do not. Tool arguments are typed from the
same scene schema as the authoring requests, with a strict parse that names the operation, the field and the
expectation. An offline test pins what each stage's request contains.

| Stage | Static characters per model call, before | After |
|---|---|---|
| BLOCKOUT | 26,224 | 6,613 |
| TWEAK | 25,351 | 6,274 |
| VARIANTS | 25,946 | 14,203 |
| LOOKDEV | 26,256 | 11,855 |
| EXPORT | 25,914 | 1,667 |

| Live, BLOCKOUT stage | Before | After |
|---|---|---|
| Crate task on Hydrogen: prompt tokens per turn | 21,739 and 21,878 | 7,738 and 7,657 |
| Crate task on Hydrogen: wall | 9.0 and 6.9 s | 7.8 and 7.7 s |
| Crate task on Hydrogen: outcome | geometry checks pass | the same, in 3 model calls, first edit valid, visual QA approved |
| Repair task (seat a floating lid, add a handle) on Hydrogen | not in the first run | 10,825 prompt tokens over 4 calls, 8.3 s, geometry exact |
| Repair task on Carbon | not in the first run | 15,209 prompt tokens over 5 calls, geometry exact, 341 s (see below) |

The Carbon stage samples of this run met a slow provider. One model call took 195 s to return 25 tokens, and the
crate sample ended with an upstream 502 after 347 s; retries are off by design, so it is recorded as a failed
sample. Earlier the same day the same two tasks, through the same harness, took 22 to 54 s on Carbon in four
runs, with 11.3k to 15.3k prompt tokens per turn against 30.3k and 38.4k before the change. Wall time on this
tier is therefore not something this run can speak to.

*Also found.* The harness did not enforce the list of subagents a stage may start. A stage agent could start a
copy of itself with the stage's write tools, a general research agent, or a subagent it had just defined.
Starting anything but the stage's declared subagents is now refused at the gate.

#### 4. UV unwrapping was slow, distorted, and reported the wrong density

*Cause.* Every primitive was handed to xatlas to rediscover charts that the scene description already knows
exactly. On overflow the whole unwrap ran twice. xatlas's fit mode returned atlases larger than requested and not
square (3289×2808 was seen), and UVs were normalised to that, so texels were stretched and the reported density
was wrong. On dense meshes the time was in computing charts, which grows faster than linearly with chart size.

*Change.* Primitives get their exact developable charts from the compiler (box faces, cylinder sides and caps,
one chart per latitude band on round shapes), packed by a deterministic packer. Computed meshes run xatlas once
and repeat only the packing on overflow, and a chart may flatten only a bounded amount of curvature. UVs always
map into a square texture. The stretch warning band that had been added as a stopgap is removed: the budget is a
gate again.

| Subject | Area distortion, before | After | Achieved texel density, before | After |
|---|---|---|---|---|
| crate (12 boxes) | 0.005 | 0 | 820 px/m | 842 |
| barrel (5 cylinders) | 0.018 | 0 | 1,024 | 1,024 |
| dining table | 0.000 | 0 | 950 | 1,024 |
| lamp post | 0.083 (over the 0.05 budget) | 0 | 721 | 746 |
| shelf | 0.006 | 0 | 818 | 918 |
| UV sphere as a computed mesh, 448 triangles | 0.087 | 0.015 | 927 | 888 |
| the same, 1,920 triangles | 0.088 | 0.015 | 895 | 892 |
| the same, 10,080 triangles | 0.064 | 0.014 | 909 | 864 |

Area distortion is the area-weighted mean deviation of a triangle's share of UV area from its share of surface
area; the request was 1,024 px/m into a 2,048-pixel atlas. Angle distortion, measured only after the change, is 0
for the primitives and 0.026 to 0.030 for the computed spheres. One number went the wrong way: the computed
spheres reach up to 5% less texel density, because more charts mean more gutter. They were over the distortion
budget before and are well inside it now.

#### 5. LOD levels collapsed silhouettes or were identical

*Cause.* Simplification ran toward a triangle ratio with an unbounded error target, so a welded box could
collapse to four triangles. Levels were written whether or not they differed from the one before. The checker
rendered far levels so small that a collapsed lamp pole covered no pixel and could not fail.

*Change.* Generator and checker share one definition: a level may deviate from LOD0 by one pixel of a 1080-pixel
frame at the size it is shown. The deviation is measured as a two-sided surface distance (the simplifier's own
figure under-reports: 0.015 m where the surfaces were 0.031 m apart), thin parts are protected by a silhouette
test, and a level is emitted only if it removes at least a quarter of the triangles. In an assembly, a part may
be absent at a level when its whole surface is within that level's budget of what remains. The pop check renders
at the scale the budget is defined at, and is a gate again.

| Subject | LOD triangle counts, before | After (and the largest on-screen size each level is used at) |
|---|---|---|
| crate (12 boxes) | 144, 144, 144, 144 | 144; 24 below 2.5% of frame height (2 of 12 parts remain) |
| barrel | 400, 200, 108, 76 | 400; 258 below 2.5% |
| dining table | 60, 60, 60, 60 | 60 |
| lamp post | 264, 132, 66, 40 (failed the pop check) | 264 |
| shelf | 60, 60, 60, 60 | 60 |
| UV sphere, 1,920 triangles | 1,920, 960, 480, 192 | 1,920; 1,166 below 25%; 672 below 10%; 298 below 5% |
| UV sphere, 10,080 triangles | 10,080, 5,040, 2,520, 1,008 | 10,080; 3,448 below 50%; 1,902 below 25%; 690 below 10%; 216 below 5% |
| UV sphere, 49,920 triangles | 49,920, 24,960, 12,480, 4,992 | 49,920, 3,898, 2,354, 716, 314, 224 |

Read the table in two halves. The template props are already close to their minimum (boxes and 12-segment
cylinders): their old far levels were either copies of LOD0 or shapes that visibly popped, and three of the five
now ship LOD0 alone. Where a mesh is denser than a pixel can show, the bound cuts much harder than a ratio did:
the first far level of the 50,000-triangle sphere is 92% smaller instead of 50%.

#### 6. Package writing re-encoded every texture for every file

*Cause.* Each texture was decoded and PNG-encoded again for every GLB and for the inspector: 40 encodes for one
package with six maps.

*Change.* A GLB writer that embeds the bytes a texture file already holds, with images de-duplicated by content.
It also writes vertex normals, which the old exporter omitted.

#### 7. Batch sanitize did not scale with jobs

*Cause.* The UV and LOD kernels hold the interpreter lock, so threads ran one at a time, and one step ran on the
event loop and was starved.

*Change.* One worker process per job, with per-file timeouts and replacement of a worker that dies. Output files
are byte-identical to the in-process path.

#### Found while fixing those

- **Nothing had ever loaded the shipped GLB.** A three.js verifier now loads each package in headless Chromium as
  a pipeline step. It checks loader and shader errors, triangle counts, bounds, textures, materials and LOD files
  against the manifest, and the silhouette against the USD (IoU at least 0.98, the same gate the engine import
  checks use). CI runs it on all four engine targets.
- **The inspector did not show what ships.** It built its own geometry and materials in the page. It now loads a
  GLB built by the package's own writer, and notes that used to stay in the reviewer's browser are saved with the
  asset session and exposed to the agent as a tool.
- **The software rasteriser failed on triangles wider than 256 pixels.** Two callers had worked around it
  independently. Its tile table now follows the frame size.
- **Packages were not reproducible and leaked local paths.** USD carried the working directory in a comment and
  in the embedded scene description, USDZ archives carried the wall-clock time, and the manifest carried facts
  about the run: the export's timings, each pipeline step's duration, and whether the cache was hit. Those stay in
  the run report now. Two runs of one prompt, cold cache or warm, ship byte-identical packages, and a test holds
  the whole pipeline to that.
- **Every edit parsed the scene description twice.** Found while profiling a regression that one of the fixes
  had introduced (a material sync on every edit, also fixed). The transaction is now faster than it was at the
  start.
- **A failed stage session lost its error.** Outside the governed pipeline, the stage-session author dropped
  failed sessions and returned an empty list, which the environment-kit pipeline then indexed. A timeout or a 502
  surfaced as an `IndexError`. Every requested candidate now ends with a recorded outcome, and the first failure
  is raised when none produced a scene.
- **Plugin documents described tools that do not exist**, and a shipped workflow gated on a score its subagent
  could not measure. Thirty-three statements were corrected against the code, and a test now checks that the
  tools, operations and subagents the documents name exist.

### Offline numbers

Medians in milliseconds unless a unit is given. "Before" is the pre-fix tree measured in the same session.

#### Edit loop

Scenes are grids of boxes, cylinders and spheres resting on the ground.

| Operation | 5 nodes | 20 nodes | 50 nodes | 200 nodes |
|---|---|---|---|---|
| Transaction only (`apply_patch`) | 0.54 → 0.55 | 0.96 → 0.89 | 1.77 → 1.51 | 6.35 → 4.98 |
| Transaction + mesh extraction + geometry check | 2.9 → 2.7 | 8.1 → 6.7 | 19.5 → 16.2 | 62.5 → 63.8 |
| `apply_scene_ir` tool, accepted edit | 5.7 → 5.4 | 11.4 → 10.8 | 23.1 → 19.8 | 88.3 → 75.9 |
| `apply_scene_ir` tool, rejected edit with rollback | 5.4 → 5.4 | 10.3 → 10.4 | 20.9 → 21.1 | 80.7 → 76.5 |
| `undo_last_edit` tool | 6.6 → 6.5 | 12.1 → 11.7 | 23.7 → 22.5 | 62.4 → 75.7 |
| Edit + check + 4-view preview sheet (256 px) | 42 → 45 | 62 → 71 | 116 → 101 | 342 → 384 |
| `run_visual_qa` tool overhead, fixture judge | 5.7 → 5.7 | 5.8 → 6.1 | 6.0 → 6.4 | 5.7 → 5.9 |
| Rollback of a rejected edit | 0.17 → 0.10 | 0.13 → 0.13 | 0.17 → 0.17 | 0.22 → 0.23 |
| Undo that keeps history | 0.35 → 0.29 | 0.48 → 0.45 | 0.68 → 0.65 | 1.94 → 1.97 |
| First read after a rollback | 0.70 → 0.73 | 1.76 → 1.83 | 3.66 → 4.18 | 14.0 → 16.0 |

Two changes here are real. The transaction is 1.2 to 1.3 times faster at 50 and 200 nodes, because the scene
description is parsed once. The first read after a rollback is about 12% slower at 50 and 200 nodes, in every run
of the day. Everything else at the tool level is inside what two runs of the same code show: for the pre-fix code
alone, the accepted edit at 200 nodes measured 70 ms in one run and 88 ms in the next. The undo row at 200 nodes
looks like a slowdown and is not: run alone, alternating between the trees, undo measured 72 and 77 ms before
and 74 and 64 ms after.

Reaching the tool through the MCP stdio server adds 1.4 to 3.2 ms per call (1.6 to 4.4 before; inside noise).
Settling 10 props takes 7.0 ms, a spatial index over 10,000 boxes builds in 76 ms and answers a small region
query in 1.4 ms, and the geometry check takes 2.0 ms for the crate and 5.5 ms for the barrel: all unchanged.

#### Geometry export

| Operation | Subject | Before | After | Change |
|---|---|---|---|---|
| UV unwrap, computed mesh | sphere, 448 triangles | 268 | 256 | none |
| | sphere, 1,920 | 373 | 284 | 1.3x faster |
| | sphere, 10,080 | 1,487 | 530 | 2.8x faster |
| | sphere, 49,920 | 17.3 s | 2.49 s | 6.9x faster |
| | concave U prism, 1,964 | 165 | 80 | 2.1x faster |
| LOD generation, as the pipeline calls it | sphere, 448 | 0.7 | 13.4 | 19x slower |
| | sphere, 1,920 | 2.6 | 53 | 21x slower |
| | sphere, 10,080 | 13.4 | 217 | 16x slower |
| | sphere, 49,920 | 98 | 914 | 9.4x slower |
| | concave U prism, 1,964 | 3.5 | 9.2 | 2.6x slower |
| Collision hulls | convex spheres, 448 to 49,920 | 1.4 to 223 | 1.8 to 240 | none |
| | concave U prism (CoACD) | 436 | 454 | none |
| UV + LOD + colliders | crate, 144 triangles in 12 parts | 231 | 108 | 2.1x faster |
| | barrel, 320 triangles in 4 parts | 275 | 73.5 | 3.7x faster |
| | sphere, 448 | 270 | 278 | none |
| | sphere, 1,920 | 437 | 388 | 1.1x faster, inside noise |
| | sphere, 10,080 | 1,421 | 773 | 1.8x faster |
| | sphere, 49,920 | 17.5 s | 3.41 s | 5.1x faster |
| | concave U prism, 1,964 | 653 | 535 | 1.2x faster |

Inside the crate's 108 ms, unwrapping is 30 ms (223 before) and LOD generation 73 ms (3 before). The balance has
moved from a step that produced distortion to a step that measures what it removes.

#### Packaging

| Operation | Crate, before | After | Barrel, before | After |
|---|---|---|---|---|
| Write package, no textures | 73 | 70 | 43 | 49 |
| Write package, six 256 px maps | 917 | 136 | 867 | 95 |
| Write package, six 1024 px maps (one sample) | 13.6 s | 0.70 s | not run | not run |
| Validate a written package from cold | 22.6 | 17.2 | 16.3 | 13.5 |
| Export pipeline through the cache, miss | 269 | 135 | 285 | 92 |
| Export pipeline through the cache, hit | 10.4 | 9.1 | 3.8 | 3.0 |

The package with six 1024 px maps went from 79.7 MB to 69.7 MB. The maps are still embedded in every GLB and in
the USDZ.

#### Pipeline, command line and batch

`PropToEngineDAG` in process, fixture images and judges, template geometry, `unreal_5`, cold cache, three repeats.

| Prompt | Best-of-N, wall | Best-of-N, CPU of the pipeline process | Single candidate, wall | Preview gates |
|---|---|---|---|---|
| wooden crate with iron brackets | 2.34 → 3.02 s | 3.21 → 2.46 s | 2.13 → 2.72 s | pass → pass |
| rusty metal barrel | 3.13 → 3.13 s | 4.26 → 3.00 s | 2.85 → 2.91 s | pass → pass |
| oak dining table | 1.45 → 2.38 s | 1.94 → 1.58 s | 1.35 → 2.13 s | pass → pass |
| iron street lamp post | 2.10 → 2.94 s | 2.89 → 2.33 s | 1.76 → 2.57 s | fail (LOD pop) → pass |
| wooden storage shelf | 1.63 → 2.42 s | 2.15 → 1.61 s | 1.54 → 2.12 s | pass → pass |

Wall time rose while the pipeline's own CPU fell. The refinement stage went from 1.56 to 2.26 s at the median:
the three.js check adds about 1.5 s of waiting on a headless browser, whose CPU is in child processes and not in
the CPU column, and the faster unwrap, LOD and package steps give about half of that back.

| Measure | Before | After |
|---|---|---|
| `agy3d prop ... --offline` from a shell (crate) | 4.20 s | 5.18 s |
| `agy3d models` | 0.65 s | 0.64 s |
| Import of `antigravity3d.kernel.usd_stage` in a fresh interpreter | 321 ms | 364 ms |
| 12 offline props, 1 worker process | 26.2 s, 1,647 per hour | 34.7 s, 1,244 per hour |
| 12 offline props, 2 worker processes | 14.8 s, 2,923 per hour | 19.5 s, 2,210 per hour |
| 12 offline props, 4 worker processes | 10.9 s, 3,963 per hour | 15.0 s, 2,876 per hour |
| Of those 12, passing the preview gates | 11 | 12 |
| Batch sanitize, 24 meshes, 1 job | 43.2 s, 33 per minute | 38.6 s, 37 per minute (inside noise) |
| Batch sanitize, 2 jobs | 32.4 s, 44 per minute | 19.9 s, 72 per minute |
| Batch sanitize, 4 jobs | 27.6 s, 52 per minute | 13.3 s, 108 per minute |

Throughput and sanitize are one sample each. In both trees 21 of the 24 sanitize meshes pass and 3 are reported
as over the 15,000-triangle budget. Offline throughput is local compute with fixtures: a live run waits on model
and image APIs, so none of these is a live throughput figure.

#### With library-default threading

The tables above pin the numeric libraries and OpenUSD to one thread so that results are comparable between
machines. The edit, rollback, raster, unwrap and export benchmarks were run again on the same commit with each
library's own defaults (`results/round1/offline_default_threads.json`). Of the 44 cases above 1 ms, 41 are within 15% of
the pinned run, and the median ratio is 0.99. The two extremes are both at 200 nodes: an undo that keeps history
is 25% faster with default threads (1.5 ms against 2.0), and the first read after a rollback is 27% slower (20
ms against 16).

### Against the design document's targets

| Design-document target | Before | After | Verdict |
|---|---|---|---|
| Per-edit latency under 90 ms | Edit + check: 5.7 / 11.4 / 23.1 / 88 ms for 5 / 20 / 50 / 200 nodes. With a 4-view preview: 42 / 62 / 116 / 342 ms | 5.4 / 10.8 / 19.8 / 76 ms. With the preview: 45 / 71 / 101 / 384 ms | Unchanged: met to about 20 nodes with the preview, missed at 200 |
| Rollback under 1 ms | Rejected edit 0.13–0.22 ms; undo keeping history 0.35–1.94 ms | 0.10–0.23 ms; 0.29–1.97 ms | Unchanged: met for rejected edits at every size; undo exceeds 1 ms at 200 nodes |
| UV + LOD + collider under 250 ms | Crate 231 ms, barrel 275 ms, 1,920-triangle sphere 437 ms, concave shape 653 ms, 10,080 triangles 1.4 s, 49,920 triangles 17.5 s | 108 ms, 73.5 ms, 388 ms, 535 ms, 0.77 s, 3.4 s | Met with margin for props built from primitives; still missed for computed meshes |
| Visual QA pass about 200 ms | Native Decisions API: 404. Fallback judge: 15–22 s | The same: 404, and 16–22 s | Not measurable natively |
| First-pass engine QA above 99% | No engine was run. Offline, 11 of 12 props passed the preview gates | 12 of 12, and every package is verified in three.js | Not measured in an engine on this machine; CI runs Blender 5.3, Godot 4.7 and Chrome |
| All-in cost per hero asset $0.95–1.60 | No prices configured; hero authoring returned no scene | No prices configured; one director call is 7.2k input and 25.5k output and thinking tokens, and returns a scene | Not measured in dollars |
| 120+ assets per hour on 8 workers | Local compute alone: 1,647 per hour on 1 worker, 3,963 on 4 | 1,244 and 2,876 | Local compute is still not the constraint; live throughput was not measured |

Design-document kernel figures:

| Operation | Design figure | Before | After |
|---|---|---|---|
| Geometry check (Tier-1) | about 15 ms | 2.0 ms (crate), 5.9 ms (barrel) | 2.0 ms, 5.5 ms |
| UV unwrap | about 40 ms | 223 ms inside the crate's export; 268 ms at 448 triangles; 17.3 s at 49,920 | 30 ms inside the crate's export; 256 ms at 448 computed triangles; 2.5 s at 49,920 |
| LOD generation | about 25 ms | 0.7 ms at 448 triangles; 98 ms at 49,920 | 13 ms at 448; 53 ms at 1,920; 914 ms at 49,920 |
| Collision hulls (CoACD) | about 60 ms | Convex shapes skip CoACD: 1.4–42 ms up to 10,080 triangles. Concave, 1,964 triangles: 436 ms | The same: 1.8–38 ms, and 454 ms |
| Clay preview render | about 15 ms | Software rasteriser, 256 px: 14 ms per view at 960 triangles, 27 ms at 5,280, 75 ms at 20,160 | The same: 14, 28 and 79 ms |

The design document's speed-up factors over Blender (15–80x) remain unverified: no Blender binary ran on this
machine.

### What got worse

- **The offline pipeline, by 0.6 to 0.9 s per asset** (the barrel, whose old unwrap and package write were the
  slowest, comes out even). The three.js check takes about 1.5 s when nothing overlaps it (1.46 and 1.60 s in
  two command-line runs), about a third of it starting Playwright and Chromium. Where an engine is installed it
  runs beside the engine import, capture and parity steps; offline there is nothing to overlap with. Two things
  would give most of it back and are not done: starting the browser while the earlier passes run, and keeping one
  browser alive across the assets of a batch.
- **LOD generation on dense single meshes.** Measuring the error costs more than not measuring it: one exact
  distance pass per candidate level on top of the simplifier's own work. Export as a whole is faster for props
  built from primitives and for computed meshes from 10,000 triangles up, and unchanged for computed meshes of
  2,000 triangles and below.
- **Authoring requests are larger**: 7,238 input tokens per call instead of 546. A leaner schema form was
  measured for the stage tools (1,326 tokens per call against 3,946 for the full form) and is used there. The
  authoring request keeps the full form because it makes a mismatch between a part's kind and its parameters
  impossible.
- **Three small ones.** Importing the USD kernel takes 43 ms longer (more model classes are built at import).
  The engine-compliance check went from 0.5 to 0.6–0.8 ms (the material checks). The first read after a rollback
  is about 12% slower at 50 nodes and above.
- **Up to 5% less texel density on computed meshes**, described under defect 4.

### Not fixed, and not measured

Not fixed:

- **The UV packer's fixed cost on computed meshes.** The cost follows the texel area being packed, not the
  triangle count, and a surface that does not fit the atlas at the requested density is packed twice. The
  448-triangle sphere (3.1 m² at 1,024 px/m into 2,048 pixels) takes 255 ms; the same mesh takes 38 ms at
  512 px/m, where it fits the first time. Primitives do not go through this packer.
- **Convex decomposition.** A 1,964-triangle concave mesh takes 450 ms in CoACD, against the design figure of
  60 ms. Its settings were left alone: lowering them trades hull quality for time.
- **Textures are embedded once per GLB file and once in the USDZ.** Writing them is now fast; the bytes on disk
  are still repeated.
- **The environment-kit pipeline authors through stage sessions**, which are not admitted against the spend
  ceiling the way one-shot authoring calls are.

Not measured:

- Blender and the game engines. Nothing was executed on the benchmark machine. CI runs Blender 5.3, Godot 4.7
  and Chrome on the final commit; their speed is not measured here.
- The native Decisions API, which still returns 404 on the development key, and with it the governed live
  pipeline end to end.
- Asset quality as an artist would judge it.
- Wall time of stage runs on Carbon (see defect 3), and how long an answer takes that uses a small tier's whole
  allowance: the allowance has four times headroom over the largest answer seen, the timeout two times over the
  slowest call seen, and no call came near either.
- How Unreal, Unity, Godot and Roblox importers treat a part that is inactive in a far LOD variant, and how they
  show the added material parameters.
- The inspector inside a running Antigravity application. It is tested against the MCP Apps SDK's own host
  bridge in Chromium.
- Dollar cost. No prices are configured.

### Live calls spent on measurement

| Run | Counted calls | Tokens (input / output / thinking) |
|---|---|---|
| First report | 20 | 95,578 / 16,034 / 29,916 |
| Fixing the model calls: probes and verification | 22 text, 4 image | at most 276,398 in total (one call's usage is unknown) |
| Fixing the stage agents: before and after runs | 64 model calls in 16 stage runs | 245,391 / 9,028 / 12,812 |
| This report | 23 (36 requests) | 136,597 / 63,118 / 53,881, plus 12,200 reserved for the failed sample |
