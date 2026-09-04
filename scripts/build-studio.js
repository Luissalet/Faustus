#!/usr/bin/env node
/**
 * Build the Studio bundle into static/studio/.
 *
 * Called by Start-Faustus.ps1 before serving the app. The contract:
 *   - If the bundle is missing or stale, build it.
 *   - If it cannot build, exit non-zero with a message — never serve
 *     a stale bundle in silence.
 *   - If the bundle is fresh, exit 0 immediately.
 *
 * "Stale" means any source file under studio/ is newer than the bundle.
 *
 * Usage:
 *   node scripts/build-studio.js          # build if stale
 *   node scripts/build-studio.js --force  # always rebuild
 */

import { execFileSync } from 'node:child_process';
import { existsSync, statSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = fileURLToPath(new URL('.', import.meta.url));
const root = resolve(__dirname, '..');
const bundlePath = join(root, 'static', 'studio', 'studio.js');
const studioSrc = join(root, 'studio');
const force = process.argv.includes('--force');

function newestMtime(dir) {
  let newest = 0;
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.name === 'node_modules' || entry.name === '.git') continue;
    if (entry.isDirectory()) {
      newest = Math.max(newest, newestMtime(full));
    } else {
      newest = Math.max(newest, statSync(full).mtimeMs);
    }
  }
  return newest;
}

function isStale() {
  if (!existsSync(bundlePath)) return true;
  const bundleMtime = statSync(bundlePath).mtimeMs;
  const srcMtime = newestMtime(studioSrc);
  return srcMtime > bundleMtime;
}

if (!force && !isStale()) {
  process.exit(0);
}

// Check that node_modules exist
const viteBin = join(root, 'node_modules', '.bin', 'vite');
const viteJs = join(root, 'node_modules', 'vite', 'bin', 'vite.js');
if (!existsSync(join(root, 'node_modules', 'vite'))) {
  console.error(
    'Studio build failed: node_modules/vite not found.\n' +
    'Run `npm install --ignore-scripts && node node_modules/esbuild/install.js` first.'
  );
  process.exit(1);
}

console.log('Building Faustus Studio...');
try {
  // Use node to run vite directly — avoids shell-spawn issues on Windows
  // where npm run scripts can fail with ERR_INVALID_ARG_TYPE.
  execFileSync(process.execPath, [viteJs, 'build'], {
    cwd: root,
    stdio: 'inherit',
  });
  console.log('Studio build complete.');
} catch (err) {
  console.error('Studio build failed. The app will not start with a stale bundle.');
  process.exit(1);
}
