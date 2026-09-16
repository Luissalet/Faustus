#!/usr/bin/env node
/**
 * Builds `@faustus/embed`: bundles `studio/embed/src/index.ts` (which pulls
 * in `sdk/ts/src/*.ts` directly — esbuild's TS-extension resolver turns the
 * SDK's own `./client.js`-style specifiers back into the `.ts` sources, so
 * this never needs `sdk/ts` pre-built) into a single dependency-free ESM
 * file the host page can load with a plain `<script type="module">` or a
 * bundler. Run: `node build.mjs` (or `node build.mjs --watch`).
 */
import { build, context } from 'esbuild';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const outfile = path.join(here, 'dist', 'faustus-embed.js');

const opts = {
  entryPoints: [path.join(here, 'src', 'index.ts')],
  outfile,
  bundle: true,
  format: 'esm',
  platform: 'browser',
  target: ['es2020'],
  minify: true,
  sourcemap: true,
  legalComments: 'none',
};

if (process.argv.includes('--watch')) {
  const ctx = await context(opts);
  await ctx.watch();
  console.log('watching studio/embed/src for changes…');
} else {
  await build({ ...opts, metafile: false });
  console.log(`built ${path.relative(process.cwd(), outfile)}`);
}
