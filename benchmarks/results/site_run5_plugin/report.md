Built the scroll-driven 3D explainer front page for **Straitjacket** (`ctx-harness v0.5.0`) in [`site/`](file:///home/agent/project/site/) with [`site/index.html`](file:///home/agent/project/site/index.html) as its entry point. Everything inside [`site/`](file:///home/agent/project/site/) loads via relative URLs (`base: './'`), makes zero external host requests, links to [`./docs/`](file:///home/agent/project/site/index.html), and leaves [`straitjacket/`](file:///home/agent/project/straitjacket/) untouched.

---

### 1. What Was Built

A seven-chapter scroll-driven 3D instrument bench following one real evaluation log (`20,001` lines, `302,628` tokens, hiding a quiet anomaly at line `14,238`) through Straitjacket's architecture, styled strictly to Straitjacket's *Addressable Evidence* design system ([`straitjacket/docs/VISUAL-DESIGN.md`](file:///home/agent/project/straitjacket/docs/VISUAL-DESIGN.md) — `#0D0F12` ground, `#15181D`/`#1C2026` panels, square corners, 1 px rules, tabular numerals, semantic colour only, no bloom/gradients/glassmorphism):

- **Story & layout:** [`STORYBOARD.md`](file:///home/agent/project/STORYBOARD.md), [`index.html`](file:///home/agent/project/index.html), [`src/style.css`](file:///home/agent/project/src/style.css), [`src/chapters.js`](file:///home/agent/project/src/chapters.js)
  1. **Overview (`#intro`):** Wide establishing view of the evidence bench with primary links to `#flood` and `./docs/`.
  2. **Evidence fates (`#flood`):** Low-angle view of `6,053` instanced token slabs (`InstancedMesh`) queued beside the capture press, comparing raw flood (`302,628` tok) and silent head+tail truncation (`1,219` tok, losing line `14,238`).
  3. **Digest anatomy (`#digest`):** Slabs feed into the capture press and emerge as `11` bounded blue digest slabs (`531` tok, `570×` smaller) with the amber quiet needle (`L14,238 · NEEDLE`) preserved beside an immutable run handle.
  4. **Local store & retrieval (`#retrieve`):** Close view of the local artifact vault (`64 MiB` / `30 days`) with an address vector connecting the digest to a single retrieved excerpt slab lifting out of the extended indexing drawer.
  5. **Anchor drift (`#anchors`):** Live demonstration on the anchor caliper where inserted amber lines shift source bars along the datum bed, the unanchored red pointer stays stale (`1,918` silent wrong reads out of `1,920`), and the green anchored lock tracks the moved span (`1,452` relocated, `2` verified, `466` refused, `0` wrong).
  6. **Host lanes & ACP (`#harnesses`):** Top-down view of the 7 supported coding harnesses (Claude, Codex, Antigravity, Hermes, Oh My Pi, OpenCode, DeepSeek) with colour-coded `BIRTH GATE · PreToolUse` and `OUTPUT GATE · PostToolUse` barriers matching [`straitjacket/docs/HOST-CAPABILITIES.md`](file:///home/agent/project/straitjacket/docs/HOST-CAPABILITIES.md).
  7. **Start here (`#start`):** Wide hero framing of all three stations and floor traces with verified install/run commands (`ctx-harness==0.5.0`) and `./docs/` CTA.
- **Camera, shader, and fallbacks:**
  - Monotone Hermite camera spline with move/hold rhythm and per-shot framing for desktop and stacked portrait phone layouts ([`src/shots.js`](file:///home/agent/project/src/shots.js), [`src/stage.js`](file:///home/agent/project/src/stage.js)).
  - Custom GLSL calibrated drafting-grid and scroll-driven semantic floor trace shader built on `MeshStandardMaterial` with `fwidth` anti-aliasing and `THREE.Color` linear uniforms ([`src/stage.js`](file:///home/agent/project/src/stage.js)).
  - Four procedural evidence layers (`log_stream`, `digest_output`, `vault_excerpts`, `anchor_spans`, `host_lanes`) and chapter-scoped 3D labels ([`src/layers.js`](file:///home/agent/project/src/layers.js)).
  - Full `prefers-reduced-motion: reduce` support (disables Lenis and GSAP panel motion, snaps to resting chapter shots) and a no-WebGL / no-JS article fallback displaying all 7 rendered 1280×800 WebP chapter stills in [`site/stills/`](file:///home/agent/project/site/stills/).

---

### 2. Libraries Used and Why

| Library | Purpose |
| --- | --- |
| [`three`](file:///home/agent/project/src/stage.js) (`0.187.0`) | WebGL 2 renderer (`NeutralToneMapping`, `PCFShadowMap`, `RoomEnvironment` PMREM), `InstancedMesh` evidence streams, `RoundedBoxGeometry`, `GLTFLoader` / `KTX2Loader` / `MeshoptDecoder`. |
| [`three-custom-shader-material`](file:///home/agent/project/src/stage.js) (`6.4.0`) | Extends `MeshStandardMaterial` for the calibrated 0.5 m / 2 m drafting-grid and scroll-driven semantic floor trace shader while keeping PBR lighting and shadows. |
| [`postprocessing`](file:///home/agent/project/src/occlusion.js) (`6.39.6`) + [`n8ao`](file:///home/agent/project/src/occlusion.js) (`2.1.0`) | Screen-space ambient occlusion (`N8AOPostPass`), Khronos Neutral tone mapping, and SMAA edge anti-aliasing (no bloom, per Straitjacket's visual rules). |
| [`troika-three-text`](file:///home/agent/project/src/layers.js) (`0.53.1`) | Crisp SDF 3D station and gate callouts rendered with the locally bundled [`site/fonts/jetbrains-mono.woff`](file:///home/agent/project/site/fonts/jetbrains-mono.woff). |
| [`lenis`](file:///home/agent/project/src/main.js) (`1.3.28`) | Smooth wheel scrolling on desktop, synchronized with GSAP's ticker and disabled automatically under `prefers-reduced-motion: reduce`. |
| [`gsap`](file:///home/agent/project/src/main.js) (`3.15.1`, `ScrollTrigger`) | Frame-locked scroll ticker and subtle scrubbed vertical entrance inside chapter panels. |
| `@fontsource-variable/space-grotesk`, `@fontsource-variable/inter`, `@fontsource/jetbrains-mono` | Self-hosted display, UI, and monospace fonts bundled into [`site/assets/`](file:///home/agent/project/site/assets/). |
| `vite` (`8.3.8`) | Static bundle build with `base: './'` into [`site/`](file:///home/agent/project/site/). |

---

### 3. 3D Objects Made and With What

All three hero industrial stations were authored through the attached **`antigravity-3d` plugin** (OpenUSD stage → Scene IR parametric kitbashing in Blender 5.3 → automatic LOD/collider generation, procedural PBR surface baking, and `web_three` packaging → installed into the project with `install_into_project` and loaded via [`src/props.js`](file:///home/agent/project/src/props.js)):

1. **Capture Press (`capture_press`)** — Loader: [`src/agy3d/capture_press/index.js`](file:///home/agent/project/src/agy3d/capture_press/index.js) · Assets: [`site/agy3d/capture_press/`](file:///home/agent/project/site/agy3d/capture_press/)
   - Structural steel gantry, intake roller bed, hydraulic actuator, amber scan bar, and blue output aperture.
   - 61 nodes, 2 LODs (`4,014` / `2,233` tris), 4 draw calls, 1024 px PBR atlas; passed USD/Blender validation, 23/23 headless `web_three` parity checks (IoU `0.9998`), and 13/13 project install checks.
2. **Artifact Vault (`artifact_vault`)** — Loader: [`src/agy3d/artifact_vault/index.js`](file:///home/agent/project/src/agy3d/artifact_vault/index.js) · Assets: [`site/agy3d/artifact_vault/`](file:///home/agent/project/site/agy3d/artifact_vault/) + [`site/agy3d/_basis/`](file:///home/agent/project/site/agy3d/_basis/)
   - Three-tier industrial indexing cabinet with extended middle tray, internal lane dividers, blue linear retrieval carriage, and amber top header.
   - 46 nodes, 3 LODs (`2,576` / `1,676` / `932` tris), 4 draw calls, KTX2 + meshopt compressed (`1.87 MB → 1.16 MB`, GPU texture memory `16.8 MB → 3.5 MB`); passed USD/Blender validation, 25/25 headless `web_three` parity checks (IoU `0.9998`), and 13/13 project install checks.
3. **Anchor Caliper (`anchor_caliper`)** — Loader: [`src/agy3d/anchor_caliper/index.js`](file:///home/agent/project/src/agy3d/anchor_caliper/index.js) · Assets: [`site/agy3d/anchor_caliper/`](file:///home/agent/project/site/agy3d/anchor_caliper/)
   - Precision twin-rail datum bench with micrometer dial, sliding green optical inspection bridge, and recessed bed supporting the live relocating line bars.
   - 44 nodes, 2 LODs (`2,838` / `1,184` tris), 4 draw calls, `416 KB` WebP GLB; passed USD/Blender validation, 23/23 headless `web_three` parity checks (IoU `0.9998`), and 13/13 project install checks.

*(All three packages are marked `PREVIEW (UNVERIFIED)` because this offline environment uses the uncalibrated fallback judge and procedural `kernel.smart_source` textures rather than the native Decisions API and image-model texture generator.)*

---

### 4. How It Was Checked

- **Automated browser & visual check (`check_site` via headless Chromium on built [`site/`](file:///home/agent/project/site/)):**
  - **Result:** `0` broken items, `0` console/page/network errors, `0` visitor-facing notices.
  - **Desktop (`1440×900`) & Portrait Phone (`390×844`):** 9 scroll steps + intermediate positions checked; both contact sheets ([`sheet_desktop.jpg`](file:///home/agent/project/agy3d_site_check/site/sheet_desktop.jpg), [`sheet_phone.jpg`](file:///home/agent/project/agy3d_site_check/site/sheet_phone.jpg)) inspected visually frame-by-frame to verify prop alignment, contrast, label placement, scale rhythm, and text wrapping.
  - **Performance & footprint:** `5.9 MB` across `37` files over the full scroll, peak `60` draw calls (`188,215` triangles with all 6,053 instanced log slabs + 3 LOD0 props).
  - **Accessibility & fallbacks:** Checked `prefers-reduced-motion: reduce` ([`reduced.jpg`](file:///home/agent/project/agy3d_site_check/site/reduced.jpg) — stays still when untouched, skips smooth scroll and panel motion) and WebGL-disabled mode ([`nowebgl.jpg`](file:///home/agent/project/agy3d_site_check/site/nowebgl.jpg) — `0` errors, all `3,019` characters of text + all `7` chapter WebP stills displayed).
  - **Fact verification:** All `23` `data-src` figures and code specimens matched verbatim against [`straitjacket/`](file:///home/agent/project/straitjacket/).
- **Static & offline audit:** Verified zero external `http://`/`https://` asset URLs in [`site/index.html`](file:///home/agent/project/site/index.html) and [`site/assets/`](file:///home/agent/project/site/assets/), and confirmed all referenced scripts, CSS, fonts, agent icons, stills, KTX2/Basis files, and GLBs exist inside [`site/`](file:///home/agent/project/site/).

---

### 5. What Could Not Be Done

- **Native Decisions API verification & generative image textures:** In this offline environment without access to the native Decisions API or image generation (`image_limit=0`), the three `antigravity-3d` packages were exported as `PREVIEW (UNVERIFIED)` using procedural `smart_source` PBR atlases derived from the Blender geometry rather than image-model-forged textures.
- **Godot 4 engine import check:** Because the site delivers browser 3D assets, the three props were packaged and headless-checked for the `web_three` target (Blender + USD validator + headless three.js parity and project-install checks) rather than exporting separate Godot 4 packages.
- **Live external reference fetch:** As noted in the environment constraints, `https://www.scrolltide.co/` could not be fetched live; visual rhythm and motion craft were informed by the local captures in [`reference/`](file:///home/agent/project/reference/).