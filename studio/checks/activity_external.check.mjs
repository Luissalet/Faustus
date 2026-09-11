// W3-D (CONTRATO_W3.md) — Studio: the "Externos" section/filter on
// Activity.tsx (badges for certainty structured|heuristic and
// signal_age_s), fed by `adapters/activity.ts`'s new `external` category
// (`loadExternalRuns`/`externalRunFrom`) reading `adapters/externalRuntimes.ts`
// (CMP-06, untouched by this lote). Static source inspection, like
// topology.check.mjs / alternatives.check.mjs: this is wiring across a
// large screen, not pure logic a bundled import can exercise on its own.
//
// Run by tests/test_w3d_activity_external_js.py, or by hand:
//   node studio/checks/activity_external.check.mjs
import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const path = (p) => join(root, p);
const read = (p) => readFileSync(path(p), 'utf8').replace(/\r\n/g, '\n');

for (const p of [
  'studio/src/adapters/activity.ts',
  'studio/src/adapters/externalRuntimes.ts',
  'studio/src/screens/Activity.tsx',
  'studio/src/screens/activity.css',
]) {
  assert.ok(existsSync(path(p)), `missing ${p}`);
}

// ── adapters/activity.ts: a real `external` kind, fed from
// adapters/externalRuntimes.ts, "not configured" resolved to [] rather
// than thrown ──
{
  const src = read('studio/src/adapters/activity.ts');
  assert.ok(src.includes("from './externalRuntimes'"), 'must import from adapters/externalRuntimes.ts');
  assert.match(src, /kind:\s*'task'\s*\|\s*'render'\s*\|\s*'approval'\s*\|\s*'chat'\s*\|\s*'workflow'\s*\|\s*'question'\s*\|\s*'external'/, "ActivityRun['kind'] must add 'external'");
  assert.ok(src.includes('external?:'), 'ActivityRun must carry an `external` detail field');
  assert.ok(src.includes('certainty'), 'the external detail must carry certainty');
  assert.ok(src.includes('signalAgeS'), 'the external detail must carry signalAgeS');
  assert.ok(src.includes('export async function loadExternalRuns'), 'must export loadExternalRuns');
  assert.ok(src.includes("kind: 'external'"), 'externalRunFrom must tag rows kind: "external"');
  assert.ok(src.includes("errorClass === 'external_runtimes.not_configured'") && /return\s*\[\]/.test(src),
    '"not configured" must resolve to an empty list, not reject the whole screen');
}

// ── Activity.tsx: reads through the adapter only for this feature, the
// external poller is wired into start/dispose, the kind chip/counts exist,
// and the detail pane shows certainty + signal age (never raw fetch) ──
{
  const src = read('studio/src/screens/Activity.tsx');
  assert.ok(src.includes('loadExternalRuns'), 'must import loadExternalRuns from adapters/activity');
  assert.ok(src.includes('externalPoller'), 'must run its own poller for external runs');
  assert.ok(src.includes('externalPoller.start()') && src.includes('externalPoller.dispose()'),
    'the external poller must be started and disposed like queuePoller/attentionPoller');
  assert.match(src, /Kind = 'all' \| 'task' \| 'render' \| 'approval' \| 'notification' \| 'chat' \| 'workflow' \| 'question' \| 'external'/,
    "the Kind union must add 'external'");
  assert.ok(src.includes("['external', t('External'), counts.external]"), 'the kind-filter chips must include External with its own count');
  assert.ok(src.includes('external: 0'), 'the counts accumulator must seed an external key');
  assert.ok(src.includes("current.kind === 'external' && current.external"), 'the detail pane must have an external-kind section');
  assert.ok(src.includes('fs-act__external-badge'), 'the row must render the certainty/signal-age badge');
  assert.ok(/data-certainty=\{current\.external\.certainty\}|current\.external\.certainty ===/.test(src),
    'the detail pane must show the certainty value itself, not just the row badge');
}

// ── activity.css: W3-D's own rules live at the end, behind their marker,
// and stay inside the token system (no literal colours) ──
{
  const src = read('studio/src/screens/activity.css');
  assert.ok(src.includes('/* W3-D external'), 'W3-D CSS additions must carry the agreed marker comment');
  assert.ok(src.includes('.fs-act__external-badge'), 'must style the external badge');
  assert.ok(!/#[0-9a-fA-F]{3,8}\b/.test(src.slice(src.indexOf('/* W3-D external'))),
    'no literal hex colours in the W3-D block — use var(--fs-*) tokens');
}

console.log('ok activity_external');
