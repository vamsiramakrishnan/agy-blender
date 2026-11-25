Built the scroll-driven 3D front page for **Straitjacket** (`ctx-harness v0.5.0`) in [`site/index.html`](file:///home/agent/project/site/index.html), ready to be served statically under `/straitjacket/` with relative links to `./docs/` and zero external host requests.

---

### 1. What Was Built

- **Six-chapter scroll narrative** ([`index.html`](file:///home/agent/project/index.html), [`STORYBOARD.md`](file:///home/agent/project/STORYBOARD.md)) following one real evaluation corpus from [`straitjacket/evals/field-needle-record.json`](file:///home/agent/project/straitjacket/evals/field-needle-record.json) (`20,001` lines, `302,628` raw o200k tokens, quiet anomaly at line `14,238`) through Straitjacket's full pipeline:
  1. **01 — Addressable Evidence (`#intro`)**: Wide oblique establishing shot along the three-station machined bench showing the incoming red token flood queued ahead of the Containment Press.
  2. **02 — Containment Press (`#flood`)**: Low-angle shot into the open press throat where `6,053` instanced red slabs (`1 slab = 50 tokens`, scaling `302,628 → 531` tokens, `570×` smaller) compress into `11` blue digest slabs plus the proud amber `L14,238` quiet-needle beacon on the exit tray, while a neutral archive shuttle rides along the rear rail to the vault.
  3. **03 — Local Artifact Vault (`#vault`)**: Medium 3/4 view of the open-drawer vault (`64 MiB` default per stream) where `18` archived neutral cartridges rest inside steel divider bays, and the overhead gantry lifts a single blue-and-amber bounded slice (`ctx search` / `ctx get`) onto the top of an undisturbed blue prompt-prefix stack (`96.5% → 97.5%` canary cache hit).
  4. **04 — Anchor Resolver (`#anchors`)**: Telephoto macro close-up of a 12-line source plate as 3 lines insert above—an unanchored bare pointer stalls in red (`1,918` silent stale reads across `1,920` cases in `40` files) while the anchored `repo:` caliper glides down in green (`1,452` relocated, `2` verified, `466` refused, `0` wrong answers).
  5. **05 — Seven-Lane Host Manifold (`#harnesses`)**: Top-down plan view across all seven supported coding harnesses (Claude Code, Codex CLI, Antigravity, Hermes, Oh My Pi, OpenCode, DeepSeek Harness) passing the amber Birth Gate (`PreToolUse`) and blue Output Gate (`PostToolUse`), with Antigravity's unsupported output-replacement boundary marked red and an amber ACP handoff token gliding across the exit lanes.
  6. **06 — Start in One Terminal (`#start`)**: Low opposite-flank hero panorama across all three stations with `ctx-harness==0.5.0` install commands and direct links to `./docs/`.
- **Visual design & accessibility** ([`src/style.css`](file:///home/agent/project/src/style.css)): Follows [`straitjacket/docs/VISUAL-DESIGN.md`](file:///home/agent/project/straitjacket/docs/VISUAL-DESIGN.md)—dark instrument-panel ground (`#0D0F12`), square corners, 1 px rules, strictly semantic palette (`#6F96FF` blue, `#F0B429` amber, `#FF7768` red, `#63D3A4` green, `#8D939A` neutral), no bloom/glow/gradients, portrait phone stacked layout, `prefers-reduced-motion` support, and a full no-WebGL/no-JS article fallback with 6 local WebP stills in [`site/stills/`](file:///home/agent/project/site/stills/).
- **Pure scroll-driven architecture** ([`src/chapters.js`](file:///home/agent/project/src/chapters.js), [`src/shots.js`](file:///home/agent/project/src/shots.js), [`src/stage.js`](file:///home/agent/project/src/stage.js), [`src/layers.js`](file:///home/agent/project/src/layers.js), [`src/main.js`](file:///home/agent/project/src/main.js)): Every camera pose, layer position, shader uniform, and 3D label is a deterministic function of story time `T` (`0..6`), drawing on demand (`0` idle frames when stationary).

---

### 2. Libraries Used and Purpose

- **`three`** — WebGL renderer (`NeutralToneMapping`, `PCFShadowMap`), `RoomEnvironment` PMREM lighting, `InstancedMesh` (6,053 token slabs + 18 vault cartridges), `RoundedBoxGeometry`, `GLTFLoader`, `KTX2Loader`, and `MeshoptDecoder`.
- **`three-custom-shader-material`** ([`createMachinedFloorMaterial`](file:///home/agent/project/src/layers.js#L32-L108)) — Custom GLSL fragment/vertex shader extending `MeshStandardMaterial` (retaining shadows and N8AO) for the anti-aliased `fwidth()` architectural drafting grid, recessed central instrument bench, and scroll-synced blue/amber data-bus rail.
- **`postprocessing` + `n8ao`** ([`src/occlusion.js`](file:///home/agent/project/src/occlusion.js)) — Screen-space ambient occlusion (`N8AOPostPass`) and Khronos PBR Neutral tone mapping pass, gated by GPU capability with direct-render fallback.
- **`troika-three-text`** ([`makeLabel`](file:///home/agent/project/src/layers.js#L110-L122)) — Crisp SDF 3D callouts using locally bundled JetBrains Mono, automatically hidden on wide/transition shots or below 16 CSS px.
- **`gsap` (`ScrollTrigger`) + `lenis`** ([`src/main.js`](file:///home/agent/project/src/main.js)) — Smooth wheel scrolling (disabled under `prefers-reduced-motion`) driving scroll-progress mapping into chapter time `T`.
- **`detect-gpu`** ([`src/stage.js`](file:///home/agent/project/src/stage.js#L84-L95)) — Offline GPU tier lookup using locally copied benchmark JSON files in `site/detect-gpu/` to adjust shadow resolution on low-tier devices.
- **`@fontsource-variable/space-grotesk`, `@fontsource-variable/inter`, `@fontsource/jetbrains-mono`** — Self-hosted typography matching Straitjacket's design system.
- **`vite`** ([`vite.config.js`](file:///home/agent/project/vite.config.js)) — Production bundler configured with `base: './'` so `./site/` works under any path (including `/straitjacket/`).

---

### 3. 3D Objects Created and How

All three open-mechanism industrial props were authored as OpenUSD stages via the **`antigravity-3d`** plugin (`open_asset_session` → `apply_scene_ir` → `render_preview` → `deliver_package` with Blender QA + headless three.js verification) and installed via `install_into_project` with **KTX2 UASTC texture compression** and **Meshopt geometry compression** (total GPU texture footprint 11.9 MB across all 3 props, shared Basis transcoder in `site/agy3d/_basis/`):

1. **`containment_press`** ([`src/agy3d/containment_press/index.js`](file:///home/agent/project/src/agy3d/containment_press/index.js), `site/agy3d/containment_press/`): Open `[ ]` mechanical compression press with crossed matte-amber restraint straps and cleats, 13 chrome conveyor rollers, twin chrome guide columns, suspended platen & die, turned lathe hydraulic actuator, pressure gauge, amber intake bar, and blue exit slot (`6,702` tris LOD0 / `3,734` tris LOD1, 4 draw calls, 23/23 three.js checks + 13/13 install checks passed).
2. **`evidence_vault`** ([`src/agy3d/evidence_vault/index.js`](file:///home/agent/project/src/agy3d/evidence_vault/index.js), `site/agy3d/evidence_vault/`): Open-drawer archival steel vault cabinet with recessed side vents, 2 closed lower drawers, 1 pulled-out upper specimen drawer on twin chrome telescopic rails with 6 internal divider bays, and a cantilevered overhead blue retrieval gantry (`3,406` tris LOD0 / `1,902` tris LOD1, 4 draw calls, 23/23 three.js checks + 13/13 install checks passed).
3. **`anchor_manifold`** ([`src/agy3d/anchor_manifold/index.js`](file:///home/agent/project/src/agy3d/anchor_manifold/index.js), `site/agy3d/anchor_manifold/`): Seven-lane stainless routing manifold with 8 divider ribs, front amber Birth Gate bridge + 7 actuators, rear blue Output Gate bridge + 7 actuators, and left-flank vertical leadscrew + stepper mount (`5,606` tris LOD0, 4 draw calls, 21/21 three.js checks + 11/11 install checks passed).

---

### 4. How It Was Checked

- Built `./site/` cleanly with `npx vite build --outDir site` and verified zero external URLs in [`site/index.html`](file:///home/agent/project/site/index.html) and that `./straitjacket/` was left unmodified.
- Ran `check_site(target="site", sources="straitjacket", steps=12, sizes="desktop,phone")` in headless Chromium across both **desktop (`1440×900`)** and **portrait phone (`390×844`)**, plus `prefers-reduced-motion` and no-WebGL modes:
  - **0 broken checks**, **0 console errors**, **0 failed requests**, **0 external host requests**.
  - **24 / 24** `data-src` factual elements verified directly against `./straitjacket/`.
  - **0 draws while idle** at rest (`0` of `41`–`88` frames).
  - **No-WebGL fallback** verified (`nowebgl.jpg`): all 6 chapter stills (`site/stills/*.webp`) and full 3,437-character article text render cleanly with zero errors.
  - Visually inspected every frame on both `agy3d_site_check/site/sheet_desktop.jpg` and `sheet_phone.jpg`, `nowebgl.jpg`, and `reduced.jpg`, iterating once on oblique wide framing, vault azimuth clearance, exit tray prominence, phone panel spacing, and host-grid layout.

---

### 5. What Could Not Be Done

- **Live reference inspection**: The environment has no internet access, so `https://www.scrolltide.co/` could not be visited live; scroll choreography and framing lessons were taken from the local `./reference/` captures instead.
- **Native-verified / AI-textured 3D packages**: In this offline environment without Decisions API approval or image-generation quota (`image_limit = 0`), the three `antigravity-3d` packages were exported in unverified preview mode (`verified: false`) using procedural geometry-derived PBR surfaces (`kernel.smart_source`) and checked with Blender QA + headless three.js rather than native Godot/Decisions-API verification.
- **Asset download size**: Total desktop scroll download is `7.4 MB` across 39 files (`4.7 MB` KTX2 PBR atlases for the 3 props + `2.2 MB` scripts/WASM Basis transcoder/N8AO), which `check_site` flags as ~6 s on a 10 Mbit/s connection (all three 1024 px atlases are already KTX2-UASTC and Meshopt compressed).