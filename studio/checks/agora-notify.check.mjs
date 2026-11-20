import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';

/* What waits for the person in the Agora becomes a notification in the tray
   (shell/notifications.ts): a question put to them and a message that names
   them, once each, with the place to open; old things are left alone, and a
   row whose thing no longer waits is settled as read (notifications-tray.tsx). */

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const temporary = mkdtempSync(join(tmpdir(), 'faustus-agora-notify-'));
const sent = [];
globalThis.fetch = async (url, init) => {
  sent.push({ url: String(url), body: init?.body ? JSON.parse(init.body) : null });
  return { ok: true, status: 200, json: async () => ({ stored: true, notification: { id: 'n' + sent.length } }) };
};

try {
  const outfile = join(temporary, 'test.mjs');
  await build({ stdin: { contents: `export * from './studio/src/shell/notifications';`, resolveDir: root, loader: 'ts' },
    bundle: true, platform: 'node', format: 'esm', outfile, loader: { '.css': 'empty' }, logLevel: 'silent' });
  const m = await import(pathToFileURL(outfile));
  const now = 1_800_000_000;
  const inbox = {
    escalated: [
      { id: 9, title: 'Ship it now?', kind: 'question', space_id: 'vulcan', updated: now - 30, question: '**Yes** or `no`?', escalated_by: 'claude' },
      { id: 3, title: 'An old one', kind: 'question', space_id: 'vulcan', updated: now - 90000, question: 'x', escalated_by: null },
    ],
    pending: [
      { id: 41, thread_id: 7, author: 'codex-docs', kind: 'comment', body: 'hey @example-user, look at [this](http://x)', created: now - 60, thread_title: 'general', thread_kind: 'channel', task_id: null, space_id: 'atlas' },
      { id: 42, thread_id: 12, author: 'cursor', kind: 'comment', body: 'about the task', created: now - 5, thread_title: 'Import', thread_kind: 'task', task_id: 5, space_id: 'atlas x' },
      { id: 2, thread_id: 7, author: 'cursor', kind: 'comment', body: 'long ago', created: now - 200000, thread_title: 'general', thread_kind: 'channel', task_id: null, space_id: 'atlas' },
      { id: 43, thread_id: 9, author: 'claude', kind: 'escalation', body: '@example-user yes or no?', created: now - 30, thread_title: 'Ship it now?', thread_kind: 'question', task_id: null, space_id: 'vulcan' },
    ],
    reviews: [{ id: 1, title: 'not a notification', owner: 'x', thread_id: 1, space_id: 'atlas', submitted_at: now }],
  };

  const payloads = m.payloadsForInbox(inbox, now);
  const asked = payloads[0].dedupe_key;
  assert.match(asked, /^agora:q:vulcan:9:[0-9a-z]+$/, 'the key of a question carries what was asked');
  assert.notEqual(m.payloadsForInbox({ ...inbox, escalated: [{ ...inbox.escalated[0], question: 'In other words?' }] }, now)[0].dedupe_key, asked, 'asked again in other words, it is a new question');
  assert.deepEqual(payloads.map((p) => p.dedupe_key), [asked, 'agora:m:atlas:41', 'agora:m:atlas x:42'], 'fresh questions and mentions; nothing old, no reviews, and a question that names the person only once');
  assert.ok(payloads.every((p) => p.type === 'agora' && p.screen === 'agora'));
  assert.equal(payloads[0].title, 'claude asks you: Ship it now?');
  assert.equal(payloads[0].detail, 'Yes or no?', 'marks are not shown as noise');
  assert.equal(payloads[0].target, '/agora?space_id=vulcan&c=t%3A9');
  assert.equal(payloads[1].title, 'codex-docs mentions you in general');
  assert.equal(payloads[1].detail, 'hey @example-user, look at this');
  assert.equal(payloads[2].title, 'cursor mentions you in #5 Import');
  assert.equal(payloads[2].target, '/agora?space_id=atlas%20x&c=t%3A12', 'the place is the server and the conversation');

  // Everything that waits counts as waiting, fresh or not: that is what settles a row.
  const waitingKeys = [...m.agoraWaitingKeys(inbox)];
  assert.ok(waitingKeys.includes(asked) && waitingKeys.some((k) => k.startsWith('agora:q:vulcan:3:')));
  assert.deepEqual(waitingKeys.filter((k) => k.startsWith('agora:m:')).sort(), ['agora:m:atlas x:42', 'agora:m:atlas:2', 'agora:m:atlas:41', 'agora:m:vulcan:43']);

  // Told once per page, through the same idempotent route as everything else.
  const realNow = Date.now;
  Date.now = () => now * 1000;
  const seen = new Set();
  await m.emitForAgora(inbox, seen);
  assert.equal(sent.length, 3);
  assert.ok(sent.every((call) => call.url === '/api/notifications/emit' && call.body.type === 'agora'));
  await m.emitForAgora(inbox, seen);
  assert.equal(sent.length, 3, 'the next look says nothing again');
  assert.deepEqual(sent.map((call) => call.body.dedupe_key), ['agora:m:atlas:41', asked, 'agora:m:atlas x:42'], 'as they happened');

  // After a long absence only the latest few are told; the others are taken as seen.
  const many = { escalated: [], reviews: [], pending: Array.from({ length: 12 }, (_, n) => ({ ...inbox.pending[0], id: 500 + n, created: now - 600 + n })) };
  const before = sent.length;
  await m.emitForAgora(many, seen);
  assert.deepEqual(sent.slice(before).map((call) => call.body.dedupe_key), [507, 508, 509, 510, 511].map((id) => 'agora:m:atlas:' + id));
  await m.emitForAgora(many, seen);
  assert.equal(sent.length, before + 5, 'the older ones are not told on the next look either');
  Date.now = realNow;

  // Wiring: the tray looks at the Agora, opens the place a row names, and settles what no longer waits.
  const read = (path) => readFileSync(join(root, path), 'utf8');
  const tray = read('studio/src/shell/notifications-tray.tsx');
  assert.ok(tray.includes('await watchAgora();') && tray.includes('emitForAgora(inbox, seenAgora.current)'), 'the tray looks at the Agora');
  assert.ok(tray.includes("row.target.screen === 'agora' && row.target.run.startsWith('/agora')") && tray.includes('navigate(row.target.run)'), 'a row opens its conversation');
  assert.ok(tray.includes("row.type !== 'agora' || waiting.has(row.dedupe_key)") && tray.includes('markNotificationRead(row.id)'), 'what no longer waits is read');
  assert.ok(read('studio/src/adapters/agora.ts').includes("fetch('/api/agora/person-inbox'"), 'the tray uses the shared personal inbox');
  assert.ok(/NOTIFICATION_TYPES = \([^)]*"agora"\)/.test(read('routes/notifications_routes.py')), 'the server stores the type');
  for (const key of ['{name} asks you: {title}', '{name} mentions you in {where}']) {
    assert.ok(read('docs/ui/i18n/es.tsv').split('\n').some((line) => line.startsWith(key + '\t')), 'Spanish for: ' + key);
  }
  console.log('ok agora-notify');
} finally {
  rmSync(temporary, { recursive: true, force: true });
}
process.exit(0);
