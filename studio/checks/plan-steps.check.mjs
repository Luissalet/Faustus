// TASK-01 / QA-38: plan_update's structured steps survive a history reload.
//
// plan_update was never persisted before — only streamed live — so a
// reloaded session lost the docked plan window entirely. restoreFromMetadata
// (studio/src/screens/studio/model.ts) now reads the last `plan_update` off
// the raw `tool_events` array and turns it into `Turn.planSteps` /
// `planRevision` / `planWarnings`, independent of adapters/chat.ts's
// `ChatEvent` union (which still only carries the plain markdown string for
// the live path). Run by tests/test_plan_state.py, or by hand:
//   node studio/checks/plan-steps.check.mjs
import assert from 'node:assert/strict';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules/esbuild/lib/main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-plan-steps-')), 'model.mjs');
await build({ entryPoints: [join(root, 'studio/src/screens/studio/model.ts')], bundle: true,
  platform: 'node', format: 'esm', outfile: out, logLevel: 'silent' });
const { planStepFromRaw, planUpdateFromMeta, restoreFromMetadata, blankTurn } = await import(pathToFileURL(out).href);

// planStepFromRaw: the shape src/plan_state.py's PlanStep.to_dict() emits.
const step = planStepFromRaw({
  id: 'step_abc123', title: 'write the report', status: 'done',
  depends_on: ['step_parent'], evidence_refs: ['file:report.md'], notes: 'draft ready',
  verified: false,
});
assert.deepEqual(step, {
  id: 'step_abc123', title: 'write the report', status: 'done',
  dependsOn: ['step_parent'], evidenceRefs: ['file:report.md'], notes: 'draft ready',
  verified: false,
});
assert.equal(planStepFromRaw({ title: '' }), null, 'a step with no title is dropped, not kept blank');
assert.equal(planStepFromRaw(null), null);

// planUpdateFromMeta: reads the raw persisted plan_update off tool_events —
// this is the field agent_loop.py now saves that used to be streamed-only.
const meta = {
  tool_events: [
    { tool: 'read_file', round: 1 },
    {
      tool: 'update_plan', round: 1,
      plan_update: {
        plan: '- [x] investigate\n- [ ] fix it',
        revision: 2,
        warnings: ['line 3: could not parse as a checklist item'],
        steps: [
          { id: 's1', title: 'investigate', status: 'done', depends_on: [], evidence_refs: [], notes: '', verified: false },
          { id: 's2', title: 'fix it', status: 'pending', depends_on: [], evidence_refs: [], notes: '', verified: true },
        ],
      },
    },
  ],
};
const restored = planUpdateFromMeta(meta);
assert.equal(restored.plan, '- [x] investigate\n- [ ] fix it');
assert.equal(restored.revision, 2);
assert.deepEqual(restored.warnings, ['line 3: could not parse as a checklist item']);
assert.equal(restored.steps.length, 2);
assert.equal(restored.steps[0].status, 'done');
assert.equal(restored.steps[0].verified, false);

// No plan_update anywhere in the turn: undefined, not a crash or a stale plan.
assert.equal(planUpdateFromMeta({ tool_events: [{ tool: 'read_file', round: 1 }] }), undefined);
assert.equal(planUpdateFromMeta({}), undefined);

// Two plan_update tool events in the same turn: the LAST one wins, same as
// the live reducer (`case 'plan'`), which always replaces the whole plan.
const two = planUpdateFromMeta({
  tool_events: [
    { tool: 'update_plan', round: 1, plan_update: { plan: '- [ ] first', steps: [] } },
    { tool: 'update_plan', round: 2, plan_update: { plan: '- [x] second', steps: [] } },
  ],
});
assert.equal(two.plan, '- [x] second');

// restoreFromMetadata: the full path a reload actually calls.
const turn = restoreFromMetadata(blankTurn('assistant'), meta);
assert.equal(turn.plan, '- [x] investigate\n- [ ] fix it');
assert.equal(turn.planRevision, 2);
assert.equal(turn.planSteps.length, 2);
assert.equal(turn.planSteps[1].title, 'fix it');

console.log('ok plan-steps');
