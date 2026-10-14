import { defineConfig } from 'vite';
export default defineConfig({
  base: '/explorer/',
  build: { target: 'es2022', sourcemap: false, chunkSizeWarningLimit: 900 },
  server: { proxy: { '/api': 'http://127.0.0.1:18000' } },
});
