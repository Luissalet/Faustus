// TASK-06: the per-turn autonomy preset (src/autonomy_budget.py), on the
// Studio side — two things, checked without a browser:
//
//   1. `studio/src/adapters/chat.ts::sendTurn` sends the field on the wire
//      exactly when the caller set it, and omits it entirely otherwise — the
//      same "absent means unset, server defaults to supervised" contract the
//      backend relies on for back-compat with a client that never sends it
//      at all (see tests/test_autonomy_budget.py on the Python side).
//   2. `studio/src/screens/studio/Composer.tsx::AUTONOMY_PRESET_CHOICES` is
//      the three presets, in the same order and under the same ids as
//      `src.autonomy_budget.PRESETS`, each with a non-empty one-line
//      consequence — the "tres opciones con una línea de consecuencia" the
//      selector renders.
//
// Run by tests/test_autonomy_budget.py (node studio/checks/autonomy-preset.check.mjs),
// or by hand:
//   node studio/checks/autonomy-preset.check.mjs
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

// ── 1. chat.ts: the field on the wire ──────────────────────────────────
const chatOut = join(mkdtempSync(join(tmpdir(), 'faustus-autonomy-chat-')), 'chat.mjs');
await build({ entryPoints: ['studio/src/adapters/chat.ts'], bundle: true, platform: 'node', format: 'esm', outfile: chatOut });
const { sendTurn } = await import(pathToFileURL(chatOut).href);

let capturedForm = null;
globalThis.fetch = async (_url, init) => {
  capturedForm = init.body;
  return new Response('data: [DONE]\n\n');
};

async function drain(gen) {
  for await (const _event of gen) {
    // drain
  }
}

// A preset is set: it travels as `autonomy_preset`.
await drain(sendTurn({ sessionId: 's1', message: 'go', mode: 'agent', autonomyPreset: 'read_only' }));
assert.ok(capturedForm instanceof FormData, 'sendTurn posts a FormData body');
assert.equal(capturedForm.get('autonomy_preset'), 'read_only');

await drain(sendTurn({ sessionId: 's1', message: 'go', mode: 'agent', autonomyPreset: 'bounded_autonomous' }));
assert.equal(capturedForm.get('autonomy_preset'), 'bounded_autonomous');

// No preset chosen (a legacy client, or the user never touched the
// selector): the field is OMITTED entirely, not sent as '' or 'supervised' —
// the server-side default lives in src/autonomy_budget.py, once.
capturedForm = null;
await drain(sendTurn({ sessionId: 's1', message: 'go', mode: 'agent' }));
assert.ok(capturedForm instanceof FormData);
assert.equal(capturedForm.get('autonomy_preset'), null, 'no preset chosen means the field is absent, not a default value');

// A plain chat-mode send never carries it either.
capturedForm = null;
await drain(sendTurn({ sessionId: 's1', message: 'hi', mode: 'chat' }));
assert.equal(capturedForm.get('autonomy_preset'), null);

console.log('chat.ts: autonomy_preset travels exactly when set, absent otherwise (3 sends checked)');

// ── 2. Composer.tsx: the three choices ─────────────────────────────────
const composerBundle = await build({
  entryPoints: ['studio/src/screens/studio/Composer.tsx'],
  bundle: true, platform: 'node', format: 'esm', write: false, jsx: 'automatic', logLevel: 'silent',
});
const { AUTONOMY_PRESET_CHOICES } = await import(
  'data:text/javascript;base64,' + Buffer.from(composerBundle.outputFiles[0].text).toString('base64')
);

// Same three ids, same order, as src.autonomy_budget.PRESETS — a selector
// that drifted from the backend's own preset list would silently send an id
// the server falls back to 'supervised' for (normalize_preset), which is
// exactly the kind of mismatch this check exists to catch.
assert.deepEqual(
  AUTONOMY_PRESET_CHOICES.map((c) => c.value),
  ['supervised', 'bounded_autonomous', 'read_only'],
);
for (const choice of AUTONOMY_PRESET_CHOICES) {
  assert.ok(choice.label && choice.label.length > 0, `${choice.value}: needs a label`);
  assert.ok(choice.detail && choice.detail.length > 0, `${choice.value}: needs a one-line consequence`);
}
// The three consequences must actually differ — a copy-paste placeholder
// left identical across presets would pass the two checks above and still
// tell the user nothing.
assert.equal(new Set(AUTONOMY_PRESET_CHOICES.map((c) => c.detail)).size, 3);

console.log('Composer.tsx: three autonomy presets, same order as the backend, each with its own consequence line');
