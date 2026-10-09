// L20 / project defaults: display and request must use the same resolver.
// A valid project default is inherited by the server. When it is absent,
// the global knob (especially read_only) must still reach the request.
// Dynamic precedence and wire cases live in project-execution-defaults.check.mjs.
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
  /autonomyPreset\s*:\s*autonomy\.override/,
  'sendTurn must forward the shared autonomy resolution, not silently fall back to supervised',
);
assert.match(src, /effectiveAutonomy\s*=\s*resolveExecutionAutonomy\(executionPicks, executionDefaults, knobs\.autonomyPreset\)\.effective/);
assert.match(src, /const autonomy\s*=\s*resolveExecutionAutonomy\(executionPicks, resolvedExecution\.defaults, knobs\.autonomyPreset\)/);
assert.match(src, /const execution = await executionForSend\(sid\);\s*setDraft/,
  'Resolve project preferences before removing the draft on a first send');
assert.match(src, /sendBlocked=\{executionBlocked\}/, 'Pending or failed project resolution must block the composer');
console.log('Studio.tsx: forwards knobs.autonomyPreset through shared precedence and guards unresolved projects');
