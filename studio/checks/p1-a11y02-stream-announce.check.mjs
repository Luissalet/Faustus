// A11Y-02 - streamed text announced in grouped batches, not per token
// (studio/src/adapters/streamAnnounce.ts, wired into Transcript.tsx's
// useGroupedStreamAnnouncement).
//
// Bundled with esbuild on the fly; run by tests/test_p1_a11y02_stream_js.py,
// or by hand:
//   node studio/checks/p1-a11y02-stream-announce.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-stream-'));
async function load(rel, name) {
  const out = join(dir, name);
  await build({ entryPoints: [join(root, 'studio', 'src', rel)], bundle: true, format: 'esm', platform: 'node', outfile: out, logLevel: 'silent' });
  return import(pathToFileURL(out).href);
}

const m = await load(join('adapters', 'streamAnnounce.ts'), 'streamAnnounce.mjs');

let failed = 0;
const assert = (c, msg) => { if (!c) { failed += 1; console.error('FAIL:', msg); } else console.log('ok:', msg); };

const f = m.nextStreamAnnouncement;

// ── nothing new: no announcement ──
assert(f(5, 'hello', 0, 1000) === null, 'no new text -> nothing to announce');

// ── too soon since the last announcement: gated ──
assert(f(0, 'hello', 1000, 1500, 2000) === null, 'inside the minimum interval -> gated, even with new text');

// ── enough time passed: announces only the NEW part ──
{
  const r = f(5, 'hello world', 0, 3000, 2000);
  assert(r !== null, 'enough time passed -> announces');
  assert(r.chunk === ' world', 'only the new suffix is announced, not the whole message again');
  assert(r.length === 'hello world'.length, 'length advances to the full text seen so far');
}

// ── a very long stream: repeated calls at 500ms apart with a 2s gate never
//    fire more than once every 2s, so hundreds of token-deltas collapse
//    into a handful of announcements ──
{
  let length = 0, at = 0, announced = 0, text = '';
  for (let ms = 0; ms < 20000; ms += 500) {
    text += 'x'; // one more "token" every 500ms, for 40 deltas
    const r = f(length, text, at, ms, 2000);
    if (r) { length = r.length; at = r.at; announced += 1; }
  }
  assert(announced <= 11, `20s of 500ms deltas gated at 2000ms announces ~10 times, got ${announced}`);
  assert(announced >= 8, `and still announces regularly, not just once (got ${announced})`);
}

console.log(failed ? `${failed} CHECK(S) FAILED` : 'ALL OK');
process.exit(failed ? 1 : 0);
