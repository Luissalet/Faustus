// PENDIENTES.md M1 — a remembered model that no longer resolves is NAMED as
// missing, never silently replaced, and a send with it is refused.
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';
const src = readFileSync(new URL('../src/screens/Studio.tsx', import.meta.url), 'utf8');
assert.match(src, /missing:\s*true/, 'the ghost route is flagged missing');
assert.match(src, /if \(route\?\.missing\)/, 'run() refuses to send with a missing route');
assert.match(src, /setModelSignal\(\(n\) => n \+ 1\)/, 'and opens the picker instead');
// The point is that the endpoint travels with the id, so a model that no
// longer resolves can still be NAMED. Written as `writeJson(ROUTE_KEY,
// snapshot)` once and as `{ ...snapshot, picked: true }` since a `picked`
// flag was added -- the same guarantee, plus one. So: a snapshot carrying
// the endpoint is built, and it is what goes into storage.
assert.match(src, /const snapshot = \{[^}]*endpointUrl:/,
  'the remembered route must carry the endpoint, not only the id');
assert.match(src, /writeJson\(ROUTE_KEY, \{?\s*\.{0,3}snapshot/,
  'the whole route is remembered, not only its id');
const chat = readFileSync(new URL('../src/adapters/chat.ts', import.meta.url), 'utf8');
assert.match(chat, /missing\?: boolean/, 'ModelRoute carries the flag');
console.log('l72-missing-route: ok');
