// A relative base, so the built site works from any folder of any host (the install loader's files are found beside
// index.html, and nothing is requested from the server root).
import { defineConfig } from 'vite';

// Packages the starter uses when the project has them (src/occlusion.js). Vite stops a build at an import it cannot
// resolve, a dynamic one too, so a missing one becomes a module that only says it is missing.
const OPTIONAL = ['postprocessing', 'n8ao'];
const MISSING = '\0optional-missing:';
const optional = {
  name: 'optional-packages',
  enforce: 'pre',
  async resolveId(id, importer, options) {
    if (!OPTIONAL.includes(id)) return null;
    return (await this.resolve(id, importer, { ...options, skipSelf: true })) || MISSING + id;
  },
  load: (id) => (id.startsWith(MISSING) ? 'export const missing = true;' : null),
};

export default defineConfig({ base: './', plugins: [optional] });
