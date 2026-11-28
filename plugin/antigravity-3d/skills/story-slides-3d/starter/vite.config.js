// A relative base, so the built film works from any folder of any host (a prop's loader finds its files beside
// index.html, and nothing is requested from the server root).
import { defineConfig } from 'vite';

export default defineConfig({ base: './' });
