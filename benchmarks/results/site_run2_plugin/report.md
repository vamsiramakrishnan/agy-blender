I am re-capturing the seven scrollbar-free WebP stills and rebuilding `./site/` before running the final `check_site` pass.Built the scroll-driven 3D documentation landing page for **Straitjacket** (`ctx-harness==0.5.0`) in [site/](file:///home/agent/project/site) with entry point [site/index.html](file:///home/agent/project/site/index.html) (7.2 MB self-contained static build, relative base `./`, zero external host requests, links to `./docs/`, and `./straitjacket/` left untouched).

---

### 1. What Was Built

A seven-chapter architectural 3D scrollytelling site grounded in Straitjacket's **"Addressable Evidence"** design system ([VISUAL-DESIGN.md](file:///home/agent/project/straitjacket/docs/VISUAL-DESIGN.md): `#0D0F12` slate background, `#15181D` surface, `#272C33` 1 px hairline grid, square corners, semantic color only — red `#FF7768` for uncontained bulk/drift, amber `#F0B429` for addresses/handles/quiet needles, blue `#6F96FF` for bounded digests/slices, green `#63D3A4` for verified/relocated evidence — with no gradients, glow/bloom, or frosted glass):

1. **01 / Addressable Evidence (`intro`)** — Wide establishing shot of the ruled Evidence Bench, Containment Gate, incoming raw flood, and outgoing bounded digest strip; links to `./docs/` and scrolls into the pipeline.
2. **02 / The Flood (`flood`)** — Close inspection of the 20,001-line / 302,628-token integration log flood burying the amber line 14,238 quiet needle between red head-and-tail truncation planes (1,219 tokens).
3. **03 / Containment Gate (`digest`)** — Right-side view of `ctx run` diverting redundant red bulk downward while emitting a 531-token blue digest (`570×` smaller) with the amber quiet needle intact.
4. **04 / Local Artifact Vault (`retrieve`)** — Three-quarter view of the local store holding six archival bays as `ctx get --lines` extracts a single narrow blue slice with an amber address pin forward along the extraction rail.
5. **05 / Content Anchors (`anchors`)** — Precision Content Anchor Clamp replaying 1,920 edits across 40 files: relocated spans lock cleanly in green (1,452 relocated, 2 verified), rewritten spans refuse explicitly in amber (466), and wrong content stays `0` while an unanchored red line pointer drifts out of alignment above.
6. **06 / Host Lanes & ACP Routing (`harnesses`)** — Elevated view of seven calibrated host lanes (Claude Code, Codex CLI, Antigravity, Hermes, Oh My Pi, OpenCode, DeepSeek) showing birth-gate (pre) and output-gate (post) enforcement blocks.
7. **07 / Start & Measure (`start`)** — Wide overview of the complete Evidence Bench with install/run commands (`ctx-harness==0.5.0`), AgentBench canary receipt, and link to `./docs/`.

All copy facts, numbers, handles, and commands carry `data-src` pointers verified against `./straitjacket/`. Desktop (`1440×900`) frames the 3D bench on the right with a pinned left evidence panel; phone (`390×844`) frames the 3D scene in the top half and the panel below; `prefers-reduced-motion: reduce` disables Lenis/GSAP motion and snaps cleanly between resting poses; no-WebGL/no-JS degrades to a complete standalone article with seven captured WebP chapter stills ([site/stills/](file:///home/agent/project/site/stills)).

Source files: [STORYBOARD.md](file:///home/agent/project/STORYBOARD.md), [index.html](file:///home/agent/project/index.html), [vite.config.js](file:///home/agent/project/vite.config.js), [src/main.js](file:///home/agent/project/src/main.js), [src/stage.js](file:///home/agent/project/src/stage.js), [src/shots.js](file:///home/agent/project/src/shots.js), [src/props.js](file:///home/agent/project/src/props.js), [src/evidence.js](file:///home/agent/project/src/evidence.js), [src/chapters.js](file:///home/agent/project/src/chapters.js), [src/style.css](file:///home/agent/project/src/style.css).

---

### 2. Libraries Used and Purpose

- **`three`** (r187) — WebGL 2 renderer (`NeutralToneMapping`, `PCFShadowMap`), `RoomEnvironment` PMREM lighting, `GLTFLoader` + `KTX2Loader` + `MeshoptDecoder`, single-draw `InstancedMesh` token/lane system, and shadow-tracking key/rim lighting ([src/stage.js](file:///home/agent/project/src/stage.js), [src/evidence.js](file:///home/agent/project/src/evidence.js)).
- **`three-custom-shader-material`** (`CustomShaderMaterial`) — Extends `MeshStandardMaterial` with two custom GLSL shaders while preserving PBR shadows and environment reflections:
  - Architectural ruled Evidence Bench shader (`fwidth` anti-aliased metric grid, recessed station frames, and scroll-driven datum trace).
  - Instanced evidence slab shader preserving per-instance semantic `instanceColor` (`vInstCol`) with crisp chamfer darkening and micro-line striping ([src/evidence.js](file:///home/agent/project/src/evidence.js)).
- **`troika-three-text`** — Crisp MSDF 3D world-space technical annotations on the bench using a locally bundled `JetBrains Mono` `.woff` font ([public/fonts/jetbrains-mono-700.woff](file:///home/agent/project/public/fonts/jetbrains-mono-700.woff)).
- **`lenis`** — Smooth desktop wheel scrolling synchronized to GSAP's ticker (disabled completely under `prefers-reduced-motion: reduce`; touch remains native) ([src/main.js](file:///home/agent/project/src/main.js)).
- **`gsap` + `ScrollTrigger`** — Global topbar progress rail (`--progress`) and subtle scrubbed chapter panel entrance ([src/main.js](file:///home/agent/project/src/main.js)).
- **`detect-gpu`** — Local offline GPU tier detection against bundled benchmark tables ([public/detect-gpu/](file:///home/agent/project/public/detect-gpu)) to scale DPR and shadow map resolution (`1024`/`2048`).
- **`@fontsource-variable/space-grotesk`**, **`@fontsource-variable/inter`**, **`@fontsource/jetbrains-mono`** — Bundled locally into `./site/assets/` for offline typography matching `VISUAL-DESIGN.md`.
- **`vite`** — Production bundler configured with `base: './'` ([vite.config.js](file:///home/agent/project/vite.config.js)) so `./site/` works from `/straitjacket/` or any sub-path.
- **`sharp`** (build-time only) — Encoded the seven headless-Chromium chapter screenshots into `site/stills/*.webp` (48–79 KB each).

---

### 3. 3D Objects Made and Tooling

All three bespoke architectural props were authored in OpenUSD via **`antigravity-3d`** Scene IR (`apply_scene_ir`), processed and QA-checked with **Blender 5.3** (`blender_asset_qa`), packaged for **`web_three`** (`deliver_package`, `max_texture_px: 1024`), and installed into the project via `install_into_project` (generating loaders in `src/agy3d/<asset>/index.js` and runtime GLB/texture assets in `public/agy3d/<asset>/`):

1. **Containment Gate (`capture_gate`)** — Installed via [src/agy3d/capture_gate/index.js](file:///home/agent/project/src/agy3d/capture_gate/index.js): 66 nodes, 2 LODs (4,574 / 2,111 tris), 8 UCX collision hulls, 1024² WebP atlas (776 KB installed). Passed USD/web_three validation (0 crit / 0 warn), Blender QA, 23/23 three.js delivery checks (IoU 0.9996), and 12/12 install checks.
2. **Local Artifact Vault (`artifact_vault`)** — Installed via [src/agy3d/artifact_vault/index.js](file:///home/agent/project/src/agy3d/artifact_vault/index.js): 61 nodes, 2 LODs (4,760 / 2,781 tris), 8 UCX collision hulls, 1024² WebP atlas (733 KB installed). Passed USD/web_three validation (0 crit / 0 warn), Blender QA, 23/23 three.js delivery checks (IoU 0.9999), and 12/12 install checks.
3. **Content Anchor Clamp (`anchor_clamp`)** — Installed via [src/agy3d/anchor_clamp/index.js](file:///home/agent/project/src/agy3d/anchor_clamp/index.js): 50 nodes, 2 LODs (3,582 / 1,700 tris), 8 UCX collision hulls, KTX2/UASTC + meshopt compressed by the installer (1.95 MB + 585 KB shared `_basis` transcoder; GPU texture memory reduced from 16.8 MB to 4.2 MB). Passed USD/web_three validation (0 crit / 0 warn), Blender QA, 23/23 three.js delivery checks (IoU 0.9997), and 12/12 install checks.

> **Important package verification note:** All three 3D packages were delivered in `antigravity-3d` **unverified preview mode** (`verified: false`, `max_images: 0`, procedural PBR maps derived from material surface descriptors rather than image-model textures, and reviewed by the uncalibrated generative fallback judge: `capture_gate` tier15 1.82, `artifact_vault` tier15 2.20, `anchor_clamp` tier15 2.00 — findings are recorded in each package's `manifest.json`). They are **not** natively verified packages.

---

### 4. How It Was Checked

Verified the built `./site/` directory end-to-end with `check_site` (`target: "site"`, `sources: "straitjacket"`, `sizes: "desktop,phone"`, `steps: 11`), which serves `./site/` under a sub-path in headless Chromium, wraps WebGL draw/texture calls, tests scroll progression, idle redraws, reduced motion, no-WebGL fallback, and `data-src` factual accuracy, plus a grep verifying zero `http://` or `https://` URLs in `site/index.html` and `site/assets/*.css`:

- **Result**: `nothing broken, 0 things a visitor would notice` (0 console errors, 0 failed requests, 0 dead frames, 0 horizontal overflow on phone).
- **Desktop (`1440×900`) & Phone (`390×844`)**: All 11/11 scroll steps captured and visually inspected across [sheet_desktop.jpg](file:///home/agent/project/agy3d_site_check/site/sheet_desktop.jpg), [sheet_phone.jpg](file:///home/agent/project/agy3d_site_check/site/sheet_phone.jpg), [nowebgl.jpg](file:///home/agent/project/agy3d_site_check/site/nowebgl.jpg), and [reduced.jpg](file:///home/agent/project/agy3d_site_check/site/reduced.jpg).
- **Performance & payload**: 5.3 MB downloaded over full scroll across 38 files; max 14 draw calls and 31,726 triangles per frame; 51.7 MB GPU texture memory (including 12.6 MB render targets); idle redraw check drew in 1 of 51 frames with unchanged pixels.
- **Fact checking (`data-src`)**: 31/31 elements with `data-src` matched their source files in `./straitjacket/`.
- **No-WebGL fallback**: 0 errors, identical 3,640 characters of copy, all 7 chapter WebP stills displayed cleanly with no "missing WebGL" error banner.

---

### 5. What Could Not Be Done

- **Native Decisions API verification & image-model PBR textures**: The attached `antigravity-3d` session did not have access to the native Decisions API or image generation (`max_images: 0`), so the three 3D packages are unverified previews (`verified: false`) with procedural PBR maps generated from their surface descriptors and reviewed by the uncalibrated fallback judge.
- **Godot headless engine import**: Because the three props are delivered directly to the web page using the `web_three` target engine (which runs headless three.js + Blender QA validation rather than a Godot export), a separate Godot 4 engine export/import pass was not run.
- **Live external reference fetch**: With no internet access, `https://www.scrolltide.co/` was not fetched live; the local captures and notes in `./reference/` were used instead.