// A relative base, so the built site works from any folder of any host (the install loader's files are found beside
// index.html, and nothing is requested from the server root).
import { defineConfig } from 'vite';

export default defineConfig({ base: './' });
