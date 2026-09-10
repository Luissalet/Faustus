// A11Y-04 - detecting insecure remote access (studio/src/adapters/remoteAccess.ts).
//
// Bundled with esbuild on the fly; run by tests/test_p1_a11y04_remote_js.py,
// or by hand:
//   node studio/checks/p1-a11y04-remote.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-remote-'));
async function load(rel, name) {
  const out = join(dir, name);
  await build({ entryPoints: [join(root, 'studio', 'src', rel)], bundle: true, format: 'esm', platform: 'node', outfile: out, logLevel: 'silent' });
  return import(pathToFileURL(out).href);
}

const m = await load(join('adapters', 'remoteAccess.ts'), 'remoteAccess.mjs');
const f = m.isInsecureRemoteAccess;

let failed = 0;
const assert = (c, msg) => { if (!c) { failed += 1; console.error('FAIL:', msg); } else console.log('ok:', msg); };

assert(f('localhost', 'http:') === false, 'localhost over http is fine — nothing leaves the machine');
assert(f('127.0.0.1', 'http:') === false, 'loopback IP over http is fine');
assert(f('::1', 'http:') === false, 'loopback IPv6 is fine');
assert(f('192.168.1.20', 'http:') === true, 'LAN IP over plain http is the exact case the warning is for');
assert(f('faustus.example.com', 'http:') === true, 'a real hostname over http is not safe either');
assert(f('faustus.example.com', 'https:') === false, 'https covers a tunnel/reverse-proxy doing TLS termination');
assert(f('192.168.1.20', 'https:') === false, 'https on a LAN IP is fine too — the transport is what matters');

console.log(failed ? `${failed} CHECK(S) FAILED` : 'ALL OK');
process.exit(failed ? 1 : 0);
