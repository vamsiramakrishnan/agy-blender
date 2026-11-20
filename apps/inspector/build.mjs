// Reproducible build of the inspector front end.
//
//   cd apps/inspector && npm ci && npm run build
//
// Inputs (hashed into build.json; tests/test_inspector_app.py recomputes the same hash and fails
// when the committed outputs are stale): this script, package.json, package-lock.json, src/**,
// the shared HTML shell src/antigravity3d/artifacts/inspector_template.html and the vendored
// three.js r160 module (src/antigravity3d/artifacts/vendor/three-r160.module.min.js.gz).
//
// Outputs (committed, under src/antigravity3d/artifacts/web/):
//   inspector_app.html          the MCP App: one self-contained document (shell + three.js r160 +
//                               MCP Apps SDK + src/mcp.js), served as ui://antigravity-3d/inspector.html
//   inspector_standalone.js     src/standalone.js + src/viewer.js; inspector.py inlines it into the
//                               standalone inspector.html next to the embedded payload
//   build.json                  input hash, output hashes, bundled third-party packages
//                               (version, lockfile integrity, licence)
//   THIRD_PARTY_NOTICES.txt     licence texts of everything bundled into inspector_app.html
// and, for the browser test only (not shipped), apps/inspector/test-host/dist/host.bundle.js: the
// SDK's AppBridge plus test-host/host.js, a minimal MCP Apps host.
//
// Third-party code is bundled unmodified (esbuild only links and minifies it).
import { build } from "esbuild";
import { createHash } from "node:crypto";
import { gunzipSync } from "node:zlib";
import { mkdirSync, readFileSync, readdirSync, statSync, writeFileSync, existsSync } from "node:fs";
import { dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(HERE, "..", "..");
const ARTIFACTS = join(REPO, "src", "antigravity3d", "artifacts");
const OUT = join(ARTIFACTS, "web");
const TEMPLATE = join(ARTIFACTS, "inspector_template.html");
const THREE_GZ = join(ARTIFACTS, "vendor", "three-r160.module.min.js.gz");
const THREE_SHA256 = "3e690ac7d180b0aadf0891bea39eec643e29e2d3e75c99b18689518665f69ba6"; // three@0.160.0 build/three.module.min.js
const SCHEMA = "antigravity3d.inspector_build/1";

const sha256 = (buf) => createHash("sha256").update(buf).digest("hex");
const posix = (p) => p.split(sep).join("/");

function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const p = join(dir, name);
    return statSync(p).isDirectory() ? walk(p) : [p];
  });
}

/** The build inputs, as repo-relative POSIX paths (keep in step with tests/test_inspector_app.py). */
export function inputFiles() {
  return [
    join(HERE, "build.mjs"), join(HERE, "package.json"), join(HERE, "package-lock.json"),
    ...walk(join(HERE, "src")), join(HERE, "test-host", "host.js"), TEMPLATE, THREE_GZ,
  ].map((p) => posix(relative(REPO, p))).sort();
}

export function inputsDigest(files) {
  const h = createHash("sha256");
  for (const rel of files) h.update(`${rel}\0${sha256(readFileSync(join(REPO, rel)))}\n`);
  return h.digest("hex");
}

/** Make bundled JS safe inside an inline <script> element. */
function scriptSafe(js, what) {
  const out = js.replace(/<\/(script)/gi, "<\\/$1");
  if (/<!--|<script/i.test(out)) throw new Error(`${what}: contains "<!--" or "<script"; cannot inline safely`);
  return out;
}

/** Same single-pass token substitution as antigravity3d.artifacts.inspector.build_inspector. */
function fill(template, subs) {
  return template.replace(/@@([A-Z_]+)@@/g, (_, k) => {
    if (!(k in subs)) throw new Error(`template token @@${k}@@ has no value`);
    return subs[k];
  });
}

function packageOf(inputPath) {
  const m = posix(inputPath).match(/node_modules\/((?:@[^/]+\/)?[^/]+)\//);
  return m ? m[1] : null;
}

async function main() {
  const lock = JSON.parse(readFileSync(join(HERE, "package-lock.json"), "utf8"));
  const threeBytes = gunzipSync(readFileSync(THREE_GZ));
  if (sha256(threeBytes) !== THREE_SHA256) throw new Error("vendored three.js does not match three@0.160.0");
  const tmp = join(HERE, ".build");
  mkdirSync(tmp, { recursive: true });
  const threePath = join(tmp, "three.module.min.js");
  writeFileSync(threePath, threeBytes);

  const common = {
    bundle: true, format: "iife", platform: "browser", target: ["es2020"], minify: true,
    charset: "ascii", legalComments: "none", write: false, metafile: true, logLevel: "warning",
    define: { "process.env.NODE_ENV": '"production"' },
  };
  const mcp = await build({ ...common, entryPoints: [join(HERE, "src", "mcp.js")], alias: { three: threePath } });
  const standalone = await build({ ...common, entryPoints: [join(HERE, "src", "standalone.js")] });
  const testHost = await build({ ...common, entryPoints: [join(HERE, "test-host", "host.js")] });

  // Third-party packages that actually ended up in the app bundle (from esbuild's metafile).
  const used = new Set();
  let threeUsed = false;
  for (const p of Object.keys(mcp.metafile.inputs)) {
    if (resolve(p) === threePath || resolve(HERE, p) === threePath) threeUsed = true;
    const name = packageOf(p);
    if (name) used.add(name);
  }
  for (const p of Object.keys(standalone.metafile.inputs)) {
    if (packageOf(p) || p.includes("three.module")) throw new Error(`standalone bundle must not contain third-party code: ${p}`);
  }
  if (!threeUsed) throw new Error("three.js is missing from the app bundle");
  const packages = [...used].sort().map((name) => {
    const entry = lock.packages[`node_modules/${name}`];
    const dir = join(HERE, "node_modules", ...name.split("/"));
    const pkg = JSON.parse(readFileSync(join(dir, "package.json"), "utf8"));
    const licFile = readdirSync(dir).find((f) => /^licen[cs]e/i.test(f));
    return { name, version: entry.version, integrity: entry.integrity, resolved: entry.resolved, license: pkg.license,
      license_text: licFile ? readFileSync(join(dir, licFile), "utf8") : null };
  });
  const threeLicense = readFileSync(join(ARTIFACTS, "vendor", "THREE-LICENSE.txt"), "utf8");
  const notices = [
    "Third-party software bundled into inspector_app.html (antigravity-3d inspector MCP App).",
    "",
    `three 0.160.0 (MIT), build/three.module.min.js sha256 ${THREE_SHA256}`,
    threeLicense.trim(),
    ...packages.flatMap((p) => ["", "-".repeat(72), `${p.name} ${p.version} (${p.license}), ${p.integrity}`,
      (p.license_text || "(no licence file in the package; licence field: " + p.license + ")").trim()]),
    "",
  ].join("\n");
  if (notices.includes("*/")) throw new Error("licence text contains */");

  const mcpJs = scriptSafe(`/*! antigravity-3d inspector MCP App. Bundles three 0.160.0 (MIT) and ${packages
    .map((p) => `${p.name} ${p.version} (${p.license})`).join(", ")}. Licence texts: THIRD_PARTY_NOTICES.txt next to this file in the antigravity3d package. */\n`
    + mcp.outputFiles[0].text, "inspector_app.html");
  const standaloneJs = scriptSafe(standalone.outputFiles[0].text, "inspector_standalone.js");

  const template = readFileSync(TEMPLATE, "utf8");
  const appHtml = fill(template, {
    TITLE: "3D inspector", ASSET_NAME: "3D inspector", ENGINE: "-", GAUGES: "", TABLE_ROWS: "", QA: "", LEGEND: "",
    PAYLOAD: "null", THREE_INLINE: "", APP_JS: mcpJs,
  });

  const hostJs = "/* Test host for tests/test_inspector_app.py (not shipped). Bundles @modelcontextprotocol/ext-apps app-bridge; licences: src/antigravity3d/artifacts/web/THIRD_PARTY_NOTICES.txt */\n"
    + testHost.outputFiles[0].text;
  // repo-relative output path -> text
  const outputs = {
    [posix(relative(REPO, join(OUT, "inspector_app.html")))]: appHtml,
    [posix(relative(REPO, join(OUT, "inspector_standalone.js")))]: standaloneJs,
    [posix(relative(REPO, join(OUT, "THIRD_PARTY_NOTICES.txt")))]: notices,
    [posix(relative(REPO, join(HERE, "test-host", "dist", "host.bundle.js")))]: hostJs,
  };
  for (const [rel, text] of Object.entries(outputs)) {
    mkdirSync(dirname(join(REPO, rel)), { recursive: true });
    writeFileSync(join(REPO, rel), text);
  }
  const files = inputFiles();
  const record = {
    schema: SCHEMA,
    inputs_sha256: inputsDigest(files),
    inputs: files,
    outputs: Object.fromEntries(Object.entries(outputs).map(([n, t]) => [n, { sha256: sha256(Buffer.from(t, "utf8")), bytes: Buffer.byteLength(t, "utf8") }])),
    toolchain: { esbuild: lock.packages["node_modules/esbuild"].version },
    bundled: [
      { name: "three", version: "0.160.0", source: "src/antigravity3d/artifacts/vendor/three-r160.module.min.js.gz", sha256: THREE_SHA256, license: "MIT" },
      ...packages.map(({ license_text, ...p }) => p),
    ],
  };
  writeFileSync(join(OUT, "build.json"), JSON.stringify(record, null, 2) + "\n");
  console.log(`inspector_app.html ${appHtml.length} bytes, inspector_standalone.js ${standaloneJs.length} bytes, inputs ${record.inputs_sha256.slice(0, 12)}`);
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((e) => { console.error(e); process.exit(1); });
}
