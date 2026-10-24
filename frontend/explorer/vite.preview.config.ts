import { defineConfig } from 'vite';
import { readFileSync } from 'node:fs';

// Read-only preview of an exported production release; excluded from deployment.
const snapshot = readFileSync('../../outputs/explorer/scientific-20261003/NutriOmics_Explorer_scientific.json', 'utf8');
export default defineConfig({
  base: '/explorer/',
  plugins: [{ name: 'production-snapshot-preview', transformIndexHtml: { order: 'pre', handler: () => [{ tag: 'script', children: `window.__NUTRIOMICS_SNAPSHOT__=${snapshot.replaceAll('<', '\\u003c')};`, injectTo: 'head' }] } }],
});
