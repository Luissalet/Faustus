// The live line says what the model is doing, and the VRAM gate reaches the turn.
//
// "Waiting for the model" with the model loaded read as a hang (09-09-2026).
// The heartbeat now carries `model_state` from Ollama and the chat streams
// `vram_admission` events; both must come out of the adapter as the screen
// expects. Run by tests/test_studio_vram_live_js.py, or by hand:
//   node studio/checks/vram-live.check.mjs
import assert from 'node:assert/strict';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules/esbuild/lib/main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-vram-live-'));
const chatOut = join(dir, 'chat.mjs');
await build({ entryPoints: [join(root, 'studio/src/adapters/chat.ts')], bundle: true,
  platform: 'node', format: 'esm', outfile: chatOut, logLevel: 'silent' });
const chat = await import(pathToFileURL(chatOut).href);
const modelOut = join(dir, 'model.mjs');
await build({ entryPoints: [join(root, 'studio/src/screens/studio/model.ts')], bundle: true,
  platform: 'node', format: 'esm', outfile: modelOut, logLevel: 'silent' });
const model = await import(pathToFileURL(modelOut).href);

// model_state → a sentence, never the raw fields
assert.equal(chat.modelStateLabel({ resident: false }), 'Loading the model into memory');
assert.equal(chat.modelStateLabel({ resident: true, spill: false }), 'The model is reading the context');
assert.match(chat.modelStateLabel({ resident: true, spill: true, vram_bytes: 29 * 2 ** 30, size_bytes: 33 * 2 ** 30 }), /29\.0 of 33\.0 GB/);
assert.equal(chat.modelStateLabel(undefined), '');
assert.equal(chat.modelStateLabel({}), '');

// the heartbeat carries it as the waiting label
const turn0 = model.blankTurn('assistant', '');
const hb = chat.decode({ type: 'run_activity', data: { phase: 'waiting_model', phase_since: 1, model_state: { resident: false } } }, null);
assert.equal(hb.type, 'heartbeat');
assert.equal(hb.detail, 'Loading the model into memory');
const turn1 = model.apply(turn0, hb);
assert.equal(turn1.live.phase, 'waiting');
assert.equal(turn1.live.label, 'Loading the model into memory');
// no model_state and no detail: the label goes away, "waiting" stays generic
const quiet = model.apply(turn1, chat.decode({ type: 'run_activity', data: { phase: 'waiting_model', phase_since: 1 } }, null));
assert.equal(quiet.live.label, undefined);
// the gate's events decode to `vram`
const gate = chat.decode({ type: 'vram_admission', data: { phase: 'vram_blocked', ticket: 't9', model: 'x', message: 'x does not fit in VRAM', residents: [] } }, null);
assert.equal(gate.type, 'vram');
assert.equal(gate.blocked?.ticket, 't9');

// the gate: a blocked ticket lands on the turn, a later phase clears it
const blocked = model.apply(turn0, { type: 'vram', phase: 'vram_blocked', message: 'x does not fit',
  blocked: { ticket: 't1', model: 'x', residents: [], shortBy: 1, message: 'x does not fit', suggestion: [] } });
assert.equal(blocked.vram?.ticket, 't1');
assert.equal(blocked.live.phase, 'waiting');
const unloading = model.apply(blocked, { type: 'vram', phase: 'unloading_model', message: 'Unloading y…' });
assert.equal(unloading.vram, undefined);
assert.equal(unloading.live.label, 'Unloading y…');

console.log('ok vram-live');
