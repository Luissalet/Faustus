// Assign a todo to an agent, Studio side, checked without a browser:
//   1. the adapter reads `agent_run` off a note and maps it;
//   2. assignAgent POSTs /api/todos/{id}/assign-agent with mode agent and returns the run;
//   3. the chip never says the agent finished unless the server said `done`.
// Run by tests/test_todo_assign_agent.py, or by hand: node studio/checks/todo-agent.check.mjs
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const dir = mkdtempSync(join(tmpdir(), 'faustus-todoagent-'));
const out = join(dir, 'notes.mjs');
await build({ entryPoints: ['studio/src/adapters/notes.ts'], bundle: true, platform: 'node', format: 'esm', outfile: out });

const calls = [];
const listBody = { notes: [{ id: 'n1', title: 'T', note_type: 'todo', agent_session_id: 's1',
  agent_run: { status: 'needs_review', session_id: 's1', summary: 'half', autonomy_preset: 'read_only', autonomy_clamped: true } }] };
globalThis.fetch = async (url, init = {}) => {
  calls.push({ url: String(url), method: init.method || 'GET', body: init.body ? JSON.parse(init.body) : null });
  const body = String(url).includes('assign-agent')
    ? { ok: true, run: { status: 'running', session_id: 's2', autonomy_preset: 'supervised' } }
    : listBody;
  return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
};

const m = await import(pathToFileURL(out).href);
const [note] = await m.listNotes();
assert.equal(note.agentRun.status, 'needs_review');
assert.equal(note.agentRun.autonomyClamped, true);
assert.equal(m.parseAgentRun({ status: 'bogus' }), null);
assert.equal(m.parseAgentRun(null), null);

const run = await m.assignAgent('n1', { autonomyPreset: 'read_only' });
assert.equal(run.status, 'running');
const post = calls.find((c) => c.url.includes('assign-agent'));
assert.equal(post.url, '/api/todos/n1/assign-agent');
assert.equal(post.method, 'POST');
assert.deepEqual(post.body, { mode: 'agent', autonomy_preset: 'read_only' });

assert.notEqual(m.agentRunLabel({ status: 'needs_review' }), m.agentRunLabel({ status: 'done' }));
assert.notEqual(m.agentRunLabel({ status: 'failed' }), m.agentRunLabel({ status: 'done' }));
console.log('todo-agent check ok');
