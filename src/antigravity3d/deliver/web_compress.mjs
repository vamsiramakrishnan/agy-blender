// Compress a package's glTF binaries for the web: KTX2 textures (Basis Universal) and meshopt geometry.
//
// Run by antigravity3d.deliver.web with the PROJECT's own node_modules (the installer links them next to this file);
// it imports glTF-Transform, ktx2-encoder, meshoptimizer and sharp from there and never downloads anything. This
// file is glue: the encoders are those packages'. Usage: node web_compress.mjs JOB.json
//
// JOB.json: { "levels": [{"in": path, "out": path}], "textures": {"albedo": {...}, ...}, "meshopt": {...}, "report": path }
//   "textures": ktx2-encoder options per role (albedo | normal | orm), applied to the images embedded in "in".
//   The report lists, per distinct source image, the role, the settings, the byte counts and the KTX2 file's sha256.
import fs from "node:fs";
import crypto from "node:crypto";
import path from "node:path";

const job = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
// read from the linked node_modules directly: some of these packages do not export their package.json
const versionOf = (name) => JSON.parse(fs.readFileSync(new URL(`./node_modules/${name}/package.json`, import.meta.url), "utf8")).version;

const { NodeIO } = await import("@gltf-transform/core");
const { ALL_EXTENSIONS, KHRTextureBasisu } = await import("@gltf-transform/extensions");
const { meshopt } = await import("@gltf-transform/functions");
const { MeshoptEncoder, MeshoptDecoder } = await import("meshoptimizer");
const { encodeToKTX2 } = await import("ktx2-encoder");
const sharp = (await import("sharp")).default;

await MeshoptEncoder.ready;
await MeshoptDecoder.ready;
const io = new NodeIO().registerExtensions(ALL_EXTENSIONS)
  .registerDependencies({ "meshopt.encoder": MeshoptEncoder, "meshopt.decoder": MeshoptDecoder });

const imageDecoder = async (buf) => {
  const { data, info } = await sharp(buf).ensureAlpha().raw().toBuffer({ resolveWithObject: true });
  return { data: new Uint8Array(data), width: info.width, height: info.height };
};
const sha = (b) => crypto.createHash("sha256").update(b).digest("hex");

// Every level of a package shares the same pictures: each distinct (image, role) is encoded once.
const encoded = new Map();
const images = [];

for (const level of job.levels) {
  const doc = await io.read(level.in);
  const roles = new Map();
  for (const m of doc.getRoot().listMaterials()) {
    const set = (t, r) => { if (t && !roles.has(t)) roles.set(t, r); };
    set(m.getNormalTexture(), "normal");
    set(m.getMetallicRoughnessTexture(), "orm");
    set(m.getOcclusionTexture(), "orm");
    set(m.getBaseColorTexture(), "albedo");
    set(m.getEmissiveTexture(), "albedo");
  }
  let any = false;
  for (const t of doc.getRoot().listTextures()) {
    const src = t.getImage();
    const role = roles.get(t) ?? "albedo";
    const key = sha(src) + role;
    if (!encoded.has(key)) {
      const t0 = Date.now();
      // one encoding per (image, role, settings), kept across the installer's tiers
      const cached = path.join(job.cache, sha(key + JSON.stringify(job.textures[role])) + ".ktx2");
      let out;
      if (fs.existsSync(cached)) out = new Uint8Array(fs.readFileSync(cached));
      else { out = await encodeToKTX2(src, { imageDecoder, generateMipmap: true, ...job.textures[role] }); fs.writeFileSync(cached, out); }
      encoded.set(key, out);
      images.push({ role, source_sha256: sha(src), source_bytes: src.byteLength, ktx2_sha256: sha(out),
                    ktx2_bytes: out.byteLength, seconds: (Date.now() - t0) / 1000, settings: job.textures[role] });
    }
    t.setImage(encoded.get(key)).setMimeType("image/ktx2");
    any = true;
  }
  if (any) doc.createExtension(KHRTextureBasisu).setRequired(true);
  if (job.meshopt !== null) await doc.transform(meshopt({ encoder: MeshoptEncoder, level: "high", ...job.meshopt }));
  await io.write(level.out, doc);
}

fs.writeFileSync(job.report, JSON.stringify({
  images,
  versions: Object.fromEntries(["@gltf-transform/core", "@gltf-transform/extensions", "@gltf-transform/functions",
                                 "ktx2-encoder", "meshoptimizer", "sharp"].map((n) => [n, versionOf(n)])),
}));
