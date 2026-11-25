Waiting for the compositor test script to finish.I built the scroll-driven 3D explainer front page for **Straitjacket** (`ctx-harness==0.5.0`) in [`site/`](file:///home/agent/project/site/) with [`site/index.html`](file:///home/agent/project/site/index.html) as its entry point. Nothing inside [`straitjacket/`](file:///home/agent/project/straitjacket/) was modified.

---

### 1. What Was Built

- **Built static site**: [`site/index.html`](file:///home/agent/project/site/index.html) and its bundled assets in [`site/`](file:///home/agent/project/site/) (built with Vite `base: './'` from [`vite.config.js`](file:///home/agent/project/vite.config.js)). Every script, stylesheet, font, icon, 3D model, GPU benchmark table, and fallback still is stored inside [`site/`](file:///home/agent/project/site/) and referenced by relative URLs so it works under `/straitjacket/` or any path with `./docs/` linking to the documentation.
- **Six-chapter scroll-driven 3D story** planned in [`STORYBOARD.md`](file:///home/agent/project/STORYBOARD.md) and authored in [`index.html`](file:///home/agent/project/index.html), [`src/shots.js`](file:///home/agent/project/src/shots.js), [`src/evidence.js`](file:///home/agent/project/src/evidence.js), [`src/stage.js`](file:///home/agent/project/src/stage.js), [`src/main.js`](file:///home/agent/project/src/main.js), and [`src/style.css`](file:///home/agent/project/src/style.css):
  1. **01 / Addressable Evidence (`#intro`)** — Introduces the Containment Gate on the calibrated metrology deck and Straitjacket's core invariant ("Omission keeps an address").
  2. **02 / The Context Flood (`#flood`)** — Visualizes the 20,001-line / 302,628-token raw log flood (`evals/field-needle-record.json`) piling up at the intake beside a head-and-tail truncated stack (1,219 tokens) that drops the quiet failure at line 14,238.
  3. **03 / Bounded Digest (`#digest`)** — Diverts the raw flood into local archival storage behind the vault while emitting the 531-token (`570×` smaller) `logtemplate/v1` digest topped by the immutable amber handle and line 14,238.
  4. **04 / Local Artifact Store (`#retrieve`)** — Pans to the Artifact Store vault (up to 64 MiB per stream) sliding out a targeted line slice on demand via `ctx search` / `ctx get`.
  5. **05 / Content Anchors (`#anchors`)** — Pans to the Content Anchor vise showing an edit shifting source lines: an unanchored red line marker stays stuck on the wrong code (1,918 of 1,920 unanchored lookups wrong), while the amber content-anchor caliper tracks the moved green byte span (1,452 relocated, 2 verified, 466 refused, 0 wrong).
  6. **06 / Multi-Harness Handoff (`#handoff`)** — Pulls out to the full metrology bench linking all three instruments with compact blue/amber handoff packets flowing across the bus and the seven supported ACP harness icons from [`straitjacket/assets/`](file:///home/agent/project/straitjacket/assets/).
- **Visual design & accessibility**: Follows Straitjacket's *Addressable Evidence* specification ([`straitjacket/docs/VISUAL-DESIGN.md`](file:///home/agent/project/straitjacket/docs/VISUAL-DESIGN.md)) — square 0 px corners, 1 px grid hairlines, strictly semantic colours (`#6F96FF` bounded/addressed, `#F0B429` identity/receipt, `#FF7768` flood/drift, `#63D3A4` verified, neutral `#0D0F12`/`#15181D`), no gradients, no bloom, no glass blur. Supports desktop (`1440×900`), portrait phone (`390×844` stacked camera fitting), `prefers-reduced-motion: reduce` (snapping to chapter rest poses and disabling smooth-scroll inertia), and a full no-WebGL / no-JS article fallback with six lazy-loaded `1280×800` WebP chapter stills in [`site/stills/`](file:///home/agent/project/site/stills/).

---

### 2. Libraries Used and Purpose

- **`three` (`0.187.0`)** in [`src/stage.js`](file:///home/agent/project/src/stage.js), [`src/evidence.js`](file:///home/agent/project/src/evidence.js), and [`src/props.js`](file:///home/agent/project/src/props.js): `WebGLRenderer` (`NeutralToneMapping`, `PCFShadowMap`), `RoomEnvironment` PMREM lighting, `GLTFLoader` with `THREE.LOD` switching, single-draw-call `InstancedMesh` (169 semantic evidence slabs), and demand-driven rendering (0 idle redraws).
- **`three-custom-shader-material` (`6.4.0`)** in [`src/evidence.js`](file:///home/agent/project/src/evidence.js): Extends `MeshStandardMaterial` with a custom GLSL fragment/vertex shader for the metrology coordinate deck, `fwidth`-antialiased minor/major gridlines, calibration pads under each instrument, and scroll-reactive semantic bus rails driven by `THREE.Color` uniforms.
- **`troika-three-text` (`0.53.1`)** in [`src/stage.js`](file:///home/agent/project/src/stage.js): Crisp SDF 3D instrument labels (`CONTAINMENT GATE`, `ARTIFACT STORE`, `CONTENT ANCHOR`) rendered with a locally hosted JetBrains Mono WOFF font ([`site/fonts/jetbrains-mono-600.woff`](file:///home/agent/project/site/fonts/jetbrains-mono-600.woff)) and horizontally billboarded toward the camera.
- **`lenis` (`1.3.28`)** in [`src/main.js`](file:///home/agent/project/src/main.js): Smooth wheel scrolling on desktop with `allowNestedScroll: true`, automatically disabled/destroyed when `prefers-reduced-motion: reduce` is active.
- **`gsap` (`3.15.1`, `ScrollTrigger`)** in [`src/main.js`](file:///home/agent/project/src/main.js): Drives the top progress ruler (`--progress`) and ticks Lenis cleanly without interfering with the pure-function `scrollY → T` 3D camera curve.
- **`detect-gpu` (`5.0.70`)** in [`src/stage.js`](file:///home/agent/project/src/stage.js): Caps `devicePixelRatio` by GPU tier using offline benchmark JSON copied locally to [`site/detect-gpu/`](file:///home/agent/project/site/detect-gpu/) (zero external requests).
- **`@fontsource-variable/space-grotesk`**, **`@fontsource-variable/inter`**, and **`@fontsource/jetbrains-mono`** in [`src/main.js`](file:///home/agent/project/src/main.js): Self-hosted local fonts bundled into [`site/assets/`](file:///home/agent/project/site/assets/).
- **`vite` (`8.3.8`)**: Static production bundler configured with `base: './'` in [`vite.config.js`](file:///home/agent/project/vite.config.js).

---

### 3. 3D Objects Made and Tooling Used

All three hero instruments were authored in OpenUSD via the `antigravity-3d` studio plugin (`apply_scene_ir` parametric kitbashing in Blender 5.3 + procedural PBR surfaces + LOD and collider generation), delivered as `web_three` packages (`max_texture_px=1024`, WebP-compressed GLBs), and installed into the project with `install_into_project` (loaders in [`src/agy3d/`](file:///home/agent/project/src/agy3d/) and assets in [`site/agy3d/`](file:///home/agent/project/site/agy3d/)):

1. **`containment_gate`** ([`src/agy3d/containment_gate/containment_gate.js`](file:///home/agent/project/src/agy3d/containment_gate/containment_gate.js)) — Heavy matte-slate steel containment frame with Straitjacket's diagonal brass X-restraint straps, central cobalt lock boss, side intake chute, exit digest bay, and twin brass tension knobs (`1.63 × 1.62 × 0.94 m`, 3 LODs: `3,546 / 2,648 / 1,395` tris, 8 UCX hulls, `520 KB` LOD0).
2. **`artifact_vault`** ([`src/agy3d/artifact_vault/artifact_vault.js`](file:///home/agent/project/src/agy3d/artifact_vault/artifact_vault.js)) — Precision multi-drawer local archival vault with recessed brass trim rails, pull handles, and status indicators (`1.24 × 1.60 × 0.93 m`, 3 LODs: `3,432 / 2,254 / 1,334` tris, 8 UCX hulls, `500 KB` LOD0).
3. **`anchor_clamp`** ([`src/agy3d/anchor_clamp/anchor_clamp.js`](file:///home/agent/project/src/agy3d/anchor_clamp/anchor_clamp.js)) — Twin-column precision vise/caliper with threaded leadscrews, brass handwheels, cantilevered clamping jaws, and emerald verification bars (`1.22 × 1.48 × 0.84 m`, 3 LODs: `3,148 / 2,170 / 1,494` tris, 8 UCX hulls, `487 KB` LOD0).

> **Verification status disclosure**: All three packages were exported as **`UNVERIFIED` previews** (native Decisions API unavailable in this environment, image generation limit `0` using procedural surfaces, uncalibrated fallback lookdev scores `2.31`, `2.14`, and `2.07`). All structural gates passed: Blender QA passed, USD / `web_three` validation reported 0 critical / 0 warning issues, and headless `three.js` import verification passed `25/25` checks on every prop (`IoU ≥ 0.9998`).

---

### 4. How It Was Checked

- **3D asset checks (`antigravity-3d` `deliver_package` + `install_into_project`)**: Every prop passed OpenUSD validation, Blender structural/PBR inspection, and headless `three.js` rendering parity (`25/25` checks each), plus dry-run and real installation verification in `three.js`.
- **Full end-to-end site verification (`check_site` on [`site/`](file:///home/agent/project/site/) against [`straitjacket/`](file:///home/agent/project/straitjacket/))**:
  - **Result**: **0 broken, 0 noticed** across both `1440×900` desktop (12 steps) and `390×844` portrait phone (12 steps).
  - **Visual review**: Inspected [`sheet_desktop.jpg`](file:///home/agent/project/agy3d_site_check/site/sheet_desktop.jpg), [`sheet_phone.jpg`](file:///home/agent/project/agy3d_site_check/site/sheet_phone.jpg), [`reduced.jpg`](file:///home/agent/project/agy3d_site_check/site/reduced.jpg), and [`nowebgl.jpg`](file:///home/agent/project/agy3d_site_check/site/nowebgl.jpg) directly. Caught and fixed a Chromium mobile Viz opaque-quad occlusion artifact on portrait phone (`--panel: rgba(21, 24, 29, 0.99)`), mid-transition camera framing clips on `retrieve` and `handoff`, and Lenis smooth-scroll inertia under `prefers-reduced-motion`.
  - **Performance & hygiene**: `3.6 MB` total desktop download across 40 local files (zero external hosts, zero console/page/network errors); peak `18` draw calls and `24,368` triangles; `0 / 82` frames drawn while idle; `22 / 22` `data-src` factual claims matched [`straitjacket/`](file:///home/agent/project/straitjacket/) with `0` unsourced numbers or code blocks; no-WebGL fallback renders `2,395` characters of text and all `6` WebP chapter stills cleanly.
  - **Relative URL check**: Verified every `src` and `href` in [`site/index.html`](file:///home/agent/project/site/index.html) is a relative path or hash anchor with zero absolute or external URLs.

---

### 5. What Could Not Be Done

- **Calibrated verified 3D export (`verified: true`)**: The native Decisions API is unavailable in this environment, so the three 3D props were exported as `UNVERIFIED` preview packages judged by the uncalibrated fallback and textured with procedural surfaces rather than image-generated PBR maps.
- **Godot 4 headless import check**: Because the deliverable is a browser `three.js` site, the three props were packaged for `web_three` (which runs Blender QA + `three.js` 25/25 headless verification rather than a Godot 4 import pass). Re-exporting the same OpenUSD sessions for `godot_4` would run Godot's importer if a Godot project package is needed separately.
- **Live reference inspection & external assets**: Because this machine has no internet access, `scrolltide.co` was studied solely from the offline captures in [`reference/`](file:///home/agent/project/reference/), and no external packages or CDN resources were fetched.
- **KTX2/Basis texture transcoding**: `install_into_project`'s `--compress auto` kept WebP textures because WebP produced a smaller web payload than KTX2 plus the Basis transcoder.