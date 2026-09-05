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
        // The entry and the stylesheet keep stable names: index.html's
        // bootstrap references them with a `?v=` query for cache-busting.
        //
        // Chunks carry a content hash and the entry is kept almost empty
        // (main.tsx does one dynamic import of app.tsx). Both are
        // load-bearing. A lazy chunk imports whatever it shares with the
        // entry FROM the entry, by relative URL — `../studio.js`, no
        // query — and the browser treats that as a second module next to
        // `studio.js?v=…`: two Reacts, "invalid hook call", the whole
        // tree unmounting the first time a lazy dialog opened. With all
        // real code in hashed chunks, the entry owns nothing anyone else
        // needs, and a new build changes the chunk names, so nothing stale
        // can be reused either. No manualChunks: a forced vendor chunk
        // would drag the gallery-only Radix menus into every page load.
        //
        // One thing the entry cannot help owning is Vite's own preload
        // helper (`__vitePreload`), which the app chunk imports from
        // `../studio.js`, so main.tsx still runs twice. That is harmless
        // now that app.tsx mounts once (see `mounted` there); it was not
        // before — two roots on one container, the shell dying on the first
        // language change.
        entryFileNames: 'studio.js',
        chunkFileNames: 'chunks/[name]-[hash].js',
        assetFileNames: 'assets/[name]-[hash][extname]',
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
