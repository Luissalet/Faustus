//
// What a conversation's dot means, from one `/api/chat/activity` snapshot.
//
// The rule that matters: a run parked on an approval card is ALSO registered
// as running, and calling that "working" is exactly the lie that cost an
// evening — the chat was not busy, it was waiting for a person who had no
// way of knowing.
//
// Run by tests/test_studio_activity_js.py, or by hand:
//   node studio/checks/activity.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-activity-')), 'activity.mjs');
await build({ entryPoints: [join(root, 'studio', 'src', 'lib', 'activity.ts')], bundle: true, format: 'esm', platform: 'node', outfile: out, logLevel: 'silent' });
const s = await import(pathToFileURL(out).href);

let failed = 0;
const assert = (c, msg) => {
  if (!c) {
    failed += 1;
    console.error('FAIL:', msg);
  } else console.log('ok:', msg);
};

const snap = (over = {}) => ({ running: [], awaiting: [], queued: {}, ...over });

// ── One session ──
{
  assert(s.sessionActivity(snap(), 'a') === null, 'a quiet session has no dot');
  assert(s.sessionActivity(snap(), null) === null, 'no session, no dot');
  assert(s.sessionActivity(snap({ running: ['a'] }), 'a') === 'running', 'a live run reads as running');
  assert(s.sessionActivity(snap({ running: ['b'] }), 'a') === null, "another session's run is not mine");
}

// ── Waiting outranks running ──
{
  // The server lists a parked run in BOTH: it is active and it is waiting.
  const parked = snap({ running: ['a'], awaiting: ['a'] });
  assert(s.sessionActivity(parked, 'a') === 'waiting', 'a run parked on an approval reads as waiting, not working');
  const noRun = snap({ awaiting: ['a'] });
  assert(s.sessionActivity(noRun, 'a') === 'waiting', 'an approval with no live run still reads as waiting');
}

// ── Queued ──
{
  const queued = snap({ running: ['a'], queued: { a: 2 } });
  assert(s.sessionActivity(queued, 'a') === 'queued', 'a run waiting for its lane reads as queued');
  const zero = snap({ running: ['a'], queued: { a: 0 } });
  assert(s.sessionActivity(zero, 'a') === 'running', 'position 0 is not a queue: it is running');
}

// ── A row that stands for many (a project) ──
{
  const many = snap({ running: ['a', 'b'], awaiting: ['b'], queued: { a: 3 } });
  assert(s.groupActivity(many, ['a', 'b']) === 'waiting', 'one chat waiting for a person sets the project’s tone');
  assert(s.groupActivity(many, ['a']) === 'queued', 'with only the queued one, the project is queued');
  assert(s.groupActivity(snap({ running: ['c'] }), ['c', 'd']) === 'running', 'one working chat is enough');
  assert(s.groupActivity(snap(), ['a', 'b']) === null, 'a quiet project has no dot');
  assert(s.groupActivity(snap({ running: ['a'] }), []) === null, 'a project with no chats has no dot');
}

console.log(failed ? `${failed} CHECK(S) FAILED` : 'ALL OK');
process.exit(failed ? 1 : 0);
