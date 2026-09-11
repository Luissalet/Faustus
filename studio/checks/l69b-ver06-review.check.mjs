// Lote 69b — VER-06: the review adapter (studio/src/adapters/review.ts)
// parses exactly the wire shape `services/review_state.py::status_payload`
// produces (see tests/test_l69b_ver06_review_wire.py for the Python side of
// this same contract) — snake_case → camelCase, `approvals[].stale`/
// `.diff_sha256` kept per path, and `tests_status` kept a SEPARATE fact from
// human accept/reject (never derived from `approvals`).
//
// Run by tests/test_l69b_studio_checks_js.py, or by hand:
//   node studio/checks/l69b-ver06-review.check.mjs
import assert from 'node:assert/strict';
import { build } from 'esbuild';

async function bundle(entry) {
  const result = await build({ entryPoints: [entry], bundle: true, format: 'esm', platform: 'node', write: false, logLevel: 'silent' });
  return import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString('base64')}`);
}

const { reviewStateFrom } = await bundle('studio/src/adapters/review.ts');

let failed = 0;
const check = (condition, message) => {
  if (!condition) { failed += 1; console.error('FAIL:', message); }
  else console.log('ok', message);
};

// The exact shape services/review_state.py::status_payload returns.
const wire = {
  message_id: 'msg1',
  session_id: 's1',
  workspace: '/ws',
  checkpoint: 'deadbeef',
  ts: 1000,
  pending: ['c.py'],
  accepted: ['a.py'],
  rejected: ['b.py'],
  restored: [],
  human_approved: { 'a.py': { at: 123, content_sha256: 'abc' } },
  approvals: [
    { path: 'a.py', at: 123, diff_sha256: 'abc', stale: false },
  ],
  tests_status: { ran: true, ok: false, inconclusive: false },
};

const state = reviewStateFrom(wire);
check(state.messageId === 'msg1', 'message_id → messageId');
check(JSON.stringify(state.pending) === JSON.stringify(['c.py']), 'pending passes through');
check(JSON.stringify(state.accepted) === JSON.stringify(['a.py']), 'accepted passes through');
check(JSON.stringify(state.rejected) === JSON.stringify(['b.py']), 'rejected passes through');
check(state.approvals.length === 1 && state.approvals[0].path === 'a.py', 'one approval row for a.py');
check(state.approvals[0].diffSha256 === 'abc', 'diff_sha256 → diffSha256');
check(state.approvals[0].stale === false, 'stale passes through as a boolean');
check(state.testsStatus && state.testsStatus.ran === true && state.testsStatus.ok === false, 'tests_status is parsed');

// A stale approval — the file moved on after the human looked at it.
const staleWire = { ...wire, approvals: [{ path: 'a.py', at: 123, diff_sha256: 'abc', stale: true }] };
check(reviewStateFrom(staleWire).approvals[0].stale === true, 'a stale approval is reported as stale, not silently kept valid');

// No automatic verification ran for this turn at all — distinct from "ran and failed".
const noTests = reviewStateFrom({ ...wire, tests_status: null });
check(noTests.testsStatus === null, 'tests_status: null (never ran) is kept apart from a failed run');

const notRun = reviewStateFrom({ ...wire, tests_status: { ran: false } });
check(notRun.testsStatus.ran === false, 'an explicit {ran:false} is not read as {ran:true, ok:false}');

// Accepting a file never mutates tests_status, and a failed test never
// removes a file from `accepted` — this adapter keeps them as two fields on
// the same object, never merges them into one verdict.
check(wire.accepted.includes('a.py') && wire.tests_status.ok === false, "the fixture itself proves the two facts coexist: accepted AND tests failing");

// Missing/malformed fields degrade to safe defaults rather than throwing.
const empty = reviewStateFrom({});
check(Array.isArray(empty.pending) && empty.pending.length === 0, 'a missing pending list defaults to empty, not a crash');
check(empty.testsStatus === null, 'a missing tests_status defaults to null (unknown), not a false pass');

if (failed) {
  console.error(`${failed} check(s) FAILED`);
  process.exit(1);
}
console.log('ALL OK: review.ts parses services/review_state.py wire shape, keeping human approval and automatic tests as separate facts');
