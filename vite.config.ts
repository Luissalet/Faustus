import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { resolve } from 'path';

export default defineConfig({
  plugins: [react()],
  root: 'studio',
  base: '/static/studio/',
  build: {
    outDir: resolve(__dirname, 'static/studio'),
    emptyOutDir: true,
    sourcemap: false,
    rollupOptions: {
      output: {
        // Stable filenames — FastAPI serves these with its own cache
        // headers, and the nonce mechanism handles CSP. No content
        // hashes: the build script checks freshness by comparing
        // source mtime against the bundle.
        entryFileNames: 'studio.js',
        chunkFileNames: 'chunks/[name].js',
        assetFileNames: 'assets/[name][extname]',
      },
    },
  },
  resolve: {
    alias: {
      '@studio': resolve(__dirname, 'studio/src'),
    },
  },
  server: {
    // Dev server proxies API calls to the running Faustus instance
    proxy: {
      '/api': 'http://127.0.0.1:7001',
      '/static': {
        target: 'http://127.0.0.1:7001',
        // Don't proxy our own studio assets back to Faustus
        bypass(req) {
          if (req.url?.startsWith('/static/studio/')) return req.url;
        },
      },
    },
  },
});
