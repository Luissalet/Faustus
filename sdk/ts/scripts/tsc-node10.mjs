#!/usr/bin/env node
// Runs `tsc -p <config>` for the two CommonJS builds (cjs, test), which need
// `moduleResolution: Node10` because this package is `"type": "module"` and
// Node16/NodeNext would emit ESM for the same sources. TypeScript 6 turned
// that option into a hard error unless `ignoreDeprecations: "6.0"` is set —
// and TypeScript 5.9 rejects that very value (TS5103). One repo, two machines,
// two compilers: detect the major version and pass the flag only when the
// compiler wants it. No dependencies.
import { spawnSync } from 'node:child_process';
import { readFileSync, writeFileSync, unlinkSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const config = process.argv[2];
if (!config) {
  console.error('usage: tsc-node10.mjs <tsconfig.json>');
  process.exit(1);
}
const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..');
const tscCmd = process.platform === 'win32' ? 'tsc.cmd' : 'tsc';
const ver = spawnSync(tscCmd, ['--version'], { cwd: root, encoding: 'utf8', shell: process.platform === 'win32' });
const major = parseInt(((ver.stdout || '') + (ver.stderr || '')).replace(/[^0-9.]/g, ' ').trim().split(/[\s.]/)[0] || '5', 10);
let target = join(root, config);
let temp = null;
if (major >= 6) {
  const parsed = JSON.parse(readFileSync(target, 'utf8'));
  parsed.compilerOptions = { ...(parsed.compilerOptions || {}), ignoreDeprecations: '6.0' };
  temp = join(root, `.${config}.ts6.json`);
  writeFileSync(temp, JSON.stringify(parsed, null, 2));
  target = temp;
}
const res = spawnSync(tscCmd, ['-p', target], { cwd: root, stdio: 'inherit', shell: process.platform === 'win32' });
if (temp) {
  try { unlinkSync(temp); } catch { /* ignore */ }
}
process.exit(res.status ?? 1);
