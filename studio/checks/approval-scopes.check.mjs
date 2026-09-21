/**
 * 20-09-2026 — the approval card's own buttons vs. what the decisions mean.
 *
 * The card used to carry four hand-written buttons: «Approve» (in fact
 * chat-session scope) and «Approve the whole task» (in fact the NARROWEST
 * one, this turn only). Clicking the wide-sounding one therefore made every
 * later turn ask again. The card now renders the scopes the server sends,
 * narrowest first, and `askOptionsFrom` keeps each option's wire `value`.
 */
import assert from 'node:assert/strict';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules/esbuild/lib/main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-approval-scopes-'));

const chatOut = join(dir, 'chat.mjs');
await build({ entryPoints: [join(root, 'studio/src/adapters/chat.ts')], bundle: true,
  platform: 'node', format: 'esm', outfile: chatOut, logLevel: 'silent' });
const { askOptionsFrom } = await import(pathToFileURL(chatOut).href);

// The server's own payload (src/tool_approvals.py public_payload).
const served = askOptionsFrom([
  { label: 'Allow for this task', value: 'approve_task', description: 'this request only' },
  { label: 'Allow for this chat session', value: 'approve', description: 'rest of this chat' },
  { label: 'Always for this workspace folder', value: 'approve_workspace', description: 'and later chats' },
  { label: 'Deny', value: 'deny', description: 'do not run it' },
]);
assert.deepEqual(served.map((o) => o.value), ['approve_task', 'approve', 'approve_workspace', 'deny']);
// Older history rows (bare strings, or objects without a value) still decode.
assert.deepEqual(askOptionsFrom(['Sí', { label: 'No' }]).map((o) => o.value), [undefined, undefined]);

const cardOut = join(dir, 'approval-scopes.mjs');
await build({ entryPoints: [join(root, 'studio/src/screens/studio/approval-scopes.ts')], bundle: true,
  platform: 'node', format: 'esm', outfile: cardOut, logLevel: 'silent' });
const { approvalChoices } = await import(pathToFileURL(cardOut).href);

const choices = approvalChoices(served);
assert.deepEqual(choices.map((c) => c.decision), ['approve_task', 'approve', 'approve_workspace', 'deny'],
  'narrowest scope first, deny last');
assert.equal(choices[0].variant, 'primary', 'the default button is the narrowest allow');
assert.equal(choices[3].variant, 'danger');
// The wording comes from the server, so the label can never drift from the
// scope the decision actually has.
assert.equal(choices[1].label, 'Allow for this chat session');
assert.equal(choices[2].description, 'and later chats');

// A history row with no options at all still offers all four scopes, and
// none of them is labelled in a way that claims a wider scope than it has.
const fallback = approvalChoices([]);
assert.deepEqual(fallback.map((c) => c.decision), ['approve_task', 'approve', 'approve_workspace', 'deny']);
assert.match(fallback[0].label, /task/i);
assert.match(fallback[1].label, /chat/i);
assert.ok(!/whole task/i.test(fallback.map((c) => c.label).join(' ')),
  'no button may read as "the whole task" for a one-turn scope');

console.log('approval-scopes.check.mjs OK');
