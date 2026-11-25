import fs from 'node:fs';
import path from 'node:path';
import { NodeIO } from '@gltf-transform/core';
import { ALL_EXTENSIONS } from '@gltf-transform/extensions';
import { dedup, prune } from '@gltf-transform/functions';

const modelsDir = path.resolve('public/models');
const files = fs.readdirSync(modelsDir).filter((f) => f.endsWith('.glb')).sort();
const io = new NodeIO().registerExtensions(ALL_EXTENSIONS);

for (const file of files) {
  const fullPath = path.join(modelsDir, file);
  const beforeBytes = fs.statSync(fullPath).size;
  const doc = await io.read(fullPath);

  await doc.transform(dedup(), prune({ keepLeaves: true }));
  await io.write(fullPath, doc);

  const afterBytes = fs.statSync(fullPath).size;
  const root = doc.getRoot();
  const nodes = root.listNodes().map((n) => n.getName());
  const exts = root.listExtensionsUsed().map((e) => e.extensionName);
  let tris = 0;
  for (const mesh of root.listMeshes()) {
    for (const prim of mesh.listPrimitives()) {
      const idx = prim.getIndices();
      if (idx) tris += idx.getCount() / 3;
      else {
        const pos = prim.getAttribute('POSITION');
        if (pos) tris += pos.getCount() / 3;
      }
    }
  }
  console.log(
    `${file.padEnd(24)} | ${(beforeBytes / 1024).toFixed(1)} KiB -> ${(afterBytes / 1024).toFixed(1)} KiB | ${Math.round(tris)} tris | ${nodes.length} nodes | exts: [${exts.join(', ')}]`
  );
}
