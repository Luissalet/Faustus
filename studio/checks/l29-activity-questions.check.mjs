// L29 (integrates L28's ACT-03 note): GET /api/questions feeds
// `loadActivity()` as `kind: 'question'` rows, and `answerQuestion()` drives
// the same `sendTurn`/`questionId` path the live AskCard uses.
//
// Run by tests/test_l29_activity_questions_js.py, or by hand:
//   node studio/checks/l29-activity-questions.check.mjs
import assert from 'node:assert/strict';
import { build } from 'esbuild';

const result = await build({ entryPoints: ['studio/src/adapters/activity.ts'], bundle: true, format: 'esm', platform: 'node', write: false });
const { loadActivity, answerQuestion } = await import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString('base64')}`);

const originalFetch = globalThis.fetch;

// ── loadActivity surfaces an open question as kind: 'question' ──
globalThis.fetch = async (path) => {
  const body = path === '/api/questions'
    ? { questions: [{ question_id: 'qst_1', session: 'sess-1', question: 'Which library?',
        options: [{ label: 'SQLite', description: 'one file', id: 'opt_a' }], multi: false,
        expires_at: null, revision: 1, opened_at: '2026-09-10T00:00:00Z' }], count: 1 }
    : path.includes('tasks/runs') ? { runs: [] }
    : path.includes('workflows/runs') ? { ok: true, runs: [] }
    : {};
  return new Response(JSON.stringify(body), { status: 200 });
};
try {
  const feed = await loadActivity();
  const q = feed.runs.find((r) => r.kind === 'question');
  assert.ok(q, 'an open question appears in the feed');
  assert.equal(q.status, 'waiting', 'a question is something waiting for a person, like an approval');
  assert.equal(q.title, 'Which library?');
  assert.equal(q.question.questionId, 'qst_1');
  assert.equal(q.question.session, 'sess-1');
  assert.deepEqual(q.question.options, [{ label: 'SQLite', description: 'one file', id: 'opt_a' }]);

  // ── a source that fails degrades gracefully, same as every other kind ──
  globalThis.fetch = async (path) => {
    if (path === '/api/questions') throw new Error('offline');
    return new Response(JSON.stringify(path.includes('workflows/runs') ? { ok: true, runs: [] } : {}), { status: 200 });
  };
  const partial = await loadActivity();
  assert.deepEqual(partial.unavailableKinds, ['question']);
  assert.deepEqual(partial.degraded, ['questions']);

  // ── answerQuestion drives sendTurn with questionId/optionIds, and only that ──
  let posted = null;
  globalThis.fetch = async (path, init) => {
    if (path === '/api/chat_stream') {
      posted = Object.fromEntries((init.body).entries());
      return new Response('data: [DONE]\n\n', { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
    }
    throw new Error(`unexpected fetch ${path}`);
  };
  await answerQuestion(
    { questionId: 'qst_1', session: 'sess-1', question: 'Which?', options: [], multi: false, expiresAt: null, revision: 1 },
    'SQLite', ['opt_a'],
  );
  assert.equal(posted.session, 'sess-1');
  assert.equal(posted.question_id, 'qst_1');
  assert.deepEqual(JSON.parse(posted.option_ids), ['opt_a']);
  assert.equal(posted.message, 'SQLite');
} finally {
  globalThis.fetch = originalFetch;
}
console.log('ALL OK: open questions reach the activity feed and answerQuestion uses the questionId path');
