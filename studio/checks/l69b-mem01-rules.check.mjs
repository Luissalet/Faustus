// Lote 69b — MEM-01: the learned-rules adapter (studio/src/adapters/memory.ts)
// parses `sensitivity` and `confidence_state` off `GET /api/memory-engine/items`
// (see tests/test_l69b_mem01_wire.py for the Python side of this same
// contract), and `forgetRule` calls the DISTINCT tombstoning route
// (`DELETE .../forget`), never conflated with the plain `deleteRule`
// (`DELETE .../{id}`).
//
// Run by tests/test_l69b_studio_checks_js.py, or by hand:
//   node studio/checks/l69b-mem01-rules.check.mjs
import assert from 'node:assert/strict';
import { build } from 'esbuild';

async function bundle(entry) {
  const result = await build({ entryPoints: [entry], bundle: true, format: 'esm', platform: 'node', write: false, logLevel: 'silent' });
  return import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString('base64')}`);
}

const { listRules, deleteRule, forgetRule } = await bundle('studio/src/adapters/memory.ts');

let failed = 0;
const check = (condition, message) => {
  if (!condition) { failed += 1; console.error('FAIL:', message); }
  else console.log('ok', message);
};

// ── listRules(): sensitivity/confidence_state parse off each item ──
{
  globalThis.fetch = async () => new Response(JSON.stringify({
    status: 'success',
    items: [
      { id: 'r1', text: 'Never force-push main', level: 'procedural', status: 'active', trust_class: 'human_explicit', maturity: 'established', effective_score: 0.9, harmful_ratio: 0, helpful_count: 3, harmful_count: 0, sensitivity: 'secret', confidence_state: 'confirmed' },
      { id: 'r2', text: 'An inferred, unmarked rule', level: 'semantic', status: 'active', trust_class: 'agent_assertion', maturity: 'candidate', effective_score: 0.4, harmful_ratio: 0, helpful_count: 0, harmful_count: 0 },
    ],
    stats: { total: 2, active: 2, anti_pattern: 0, deprecated: 0, semantic_lane: true },
  }), { status: 200 });
  const { rules } = await listRules();
  check(rules[0].sensitivity === 'secret', 'a secret item keeps its sensitivity');
  check(rules[0].confidenceState === 'confirmed', 'confidence_state → confidenceState');
  check(rules[1].sensitivity === 'normal', 'a missing sensitivity defaults to normal, not undefined');
  check(rules[1].confidenceState === 'inferred', 'a missing confidence_state defaults to inferred, never confirmed by default');
}

// ── deleteRule vs forgetRule: two distinct endpoints, never conflated ──
{
  let calledUrl = null, calledMethod = null, calledBody = null;
  globalThis.fetch = async (url, init) => {
    calledUrl = String(url);
    calledMethod = init.method;
    calledBody = init.body ? JSON.parse(init.body) : null;
    return new Response('{}', { status: 200 });
  };
  await deleteRule('r1');
  check(calledUrl === '/api/memory-engine/items/r1', 'deleteRule hits the plain delete route, no /forget suffix');
  check(calledMethod === 'DELETE', 'deleteRule uses DELETE');

  await forgetRule('r2', 'no longer true');
  check(calledUrl === '/api/memory-engine/items/r2/forget', 'forgetRule hits the DISTINCT /forget route');
  check(calledMethod === 'DELETE', 'forgetRule also uses DELETE (the route accepts a body on DELETE)');
  check(calledBody && calledBody.reason === 'no longer true', 'forgetRule sends the reason in the body');
}

if (failed) {
  console.error(`${failed} check(s) FAILED`);
  process.exit(1);
}
console.log('ALL OK: memory.ts parses sensitivity/confidence_state, and forgetRule/deleteRule stay two distinct actions');
