// A relative base, so the built story works from any folder of any host (a prop's loader finds its files beside
// index.html, and nothing is requested from the server root).
import { defineConfig } from 'vite';

// The font packages the themes use when the project has them (src/main.js imports them; src/theme.css names their
// faces first and a system's after). Vite stops a build at an import it cannot resolve, so a missing one becomes a
// module that only says it is missing, and the story is set in the next face of its stack.
const OPTIONAL = /^@fontsource(-variable)?\//;
const MISSING = '\0optional-missing:';
const optional = {
  name: 'optional-packages',
  enforce: 'pre',
  async resolveId(id, importer, options) {
    if (!OPTIONAL.test(id)) return null;
    // The stand-in's name ends in .js whatever the import was: one that ended in .css would be read as a stylesheet.
    return (await this.resolve(id, importer, { ...options, skipSelf: true })) || `${MISSING}${id}.js`;
  },
  load: (id) => (id.startsWith(MISSING) ? 'export const missing = true;' : null),
};

export default defineConfig({ base: './', plugins: [optional] });
