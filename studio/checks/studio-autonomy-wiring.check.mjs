// L20 (integrates L13, item 2): Composer.tsx lets a person pick an autonomy
// preset into `knobs.autonomyPreset`, and chat.ts (autonomy-preset.check.mjs)
// forwards it on the wire exactly when `sendTurn` receives it — but nothing
// checked that `Studio.tsx`'s `sendTurn({...})` call actually READS
// `knobs.autonomyPreset` and passes it along. Without that one field the
// picker was decoration: every turn silently ran under the server default
// ('supervised'), preset choice included.
//
// A full render of Studio.tsx (screen-level component, many hooks/providers)
// is not worth the harness it would need for one field; this checks the
// actual call-site source instead, the same way a reviewer would.
//
// Run by tests/test_lote20_studio_autonomy_wiring.py, or by hand:
//   node studio/checks/studio-autonomy-wiring.check.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const src = readFileSync(join(root, 'studio', 'src', 'screens', 'Studio.tsx'), 'utf8');

// Find the `sendTurn({ ... })` call-site object body (it spans many lines).
const callStart = src.indexOf('for await (const event of sendTurn({');
assert.ok(callStart >= 0, 'sendTurn({...}) call site not found in Studio.tsx');
const bodyStart = src.indexOf('{', callStart + 'for await (const event of sendTurn('.length);
// Walk braces to find the matching close for the call's object literal.
let depth = 0;
let i = bodyStart;
for (; i < src.length; i++) {
  if (src[i] === '{') depth++;
  else if (src[i] === '}') {
    depth--;
    if (depth === 0) break;
  }
}
const callBody = src.slice(bodyStart, i + 1);

assert.match(
  callBody,
  /autonomyPreset\s*:\s*knobs\.autonomyPreset/,
  'sendTurn({...}) must pass `autonomyPreset: knobs.autonomyPreset` — the ' +
  'Composer preset picker is otherwise wired to nothing',
);

console.log('Studio.tsx: sendTurn({...}) forwards knobs.autonomyPreset');
