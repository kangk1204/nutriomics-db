import { build } from 'esbuild';
await build({
  entryPoints: ['src/main.tsx'], outfile: 'dist/offline-runtime.js',
  bundle: true, format: 'iife', minify: true, target: 'es2022',
  define: { 'process.env.NODE_ENV': '"production"' },
  legalComments: 'inline',
});
