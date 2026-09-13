// A20 (paridad blueprint): the same walk as examples/esm/index.mjs, from a
// clean CommonJS project instead — proves `require('faustus-sdk')` resolves
// the package's `require` export (`dist/cjs/index.cjs`) and works exactly
// the same way: version → create session → send a turn (with a workspace
// and message read from the environment) → iterate events, answering any
// tool_approval ask_user → export the session → list/get/download
// artifacts and check the download's sha256 against the artifact's own
// metadata. MODE=cancel exercises cancel()/resume() instead; MODE=denied
// proves a sessions-scopeless token is refused with 403 when it tries to
// create a session. Install first: `npm pack` in `sdk/ts`, then
// `npm install ../../faustus-sdk-0.1.0.tgz` here (see the package's
// README.md).
'use strict';
const { createHash } = require('node:crypto');
const { FaustusClient } = require('faustus-sdk');

const baseUrl = process.env.FAUSTUS_URL;
const token = process.env.FAUSTUS_TOKEN;
const endpointId = process.env.FAUSTUS_ENDPOINT_ID || '';
const endpointUrl = process.env.FAUSTUS_ENDPOINT_URL || '';
const model = process.env.FAUSTUS_MODEL || '';
const workspace = process.env.FAUSTUS_WORKSPACE || '';
const message = process.env.FAUSTUS_MESSAGE || 'Say hello in one short sentence, then stop.';
const mode = process.env.MODE === 'cancel' ? 'cancel' : process.env.MODE === 'denied' ? 'denied' : 'run';

function report(fields) {
  process.stdout.write(JSON.stringify(fields) + '\n');
}

async function main() {
  if (!baseUrl) throw new Error('FAUSTUS_URL is required');

  const client = new FaustusClient({ baseUrl, token });

  if (mode === 'denied') {
    try {
      await client.sessions.create({ skipValidation: true });
      report({ ok: false, error: 'expected session creation to be refused (403) but it succeeded' });
      process.exit(1);
      return;
    } catch (err) {
      const status = err && err.status;
      const ok = status === 403;
      report({ ok, status: status ?? null, detail: String((err && err.detail) || (err && err.message) || err) });
      process.exit(ok ? 0 : 1);
      return;
    }
  }

  await client.version();

  const session = await client.sessions.create({
    // A real, non-placeholder name — otherwise the server schedules a
    // best-effort background "auto-name" call to the same model right
    // after the first message, racing this script's own scripted replies.
    name: 'A20 external SDK consumer',
    skipValidation: true,
    endpointId: endpointId || undefined,
    endpointUrl: endpointUrl || undefined,
    model: model || undefined,
  });

  let turn = await client.turns.create(session.id, {
    message,
    mode: 'agent',
    ...(workspace ? { workspace } : {}),
  });
  const runId = turn.runId;

  const events = {};
  let approvals = 0;
  let text = '';
  let cancelled = false;
  let stopResult = null;

  for (;;) {
    let sawApprovalAnswer = false;
    for await (const event of turn) {
      events[event.type] = (events[event.type] || 0) + 1;
      if (event.type === 'delta') text += event.delta;

      if (event.type === 'ask_user' && event.data.kind === 'tool_approval') {
        approvals += 1;
        turn = await client.turns.decideToolApproval(session.id, {
          approvalId: event.data.approval_id,
          decision: 'approve_task',
        });
        sawApprovalAnswer = true;
        break;
      }

      if (mode === 'cancel' && !cancelled && (event.type === 'run_activity' || event.type === 'delta')) {
        cancelled = true;
        stopResult = await turn.cancel('task');
      }
    }
    if (sawApprovalAnswer) continue;
    break;
  }

  const end = await turn.done;

  let ok;
  let exportFilename = null;
  let exportMediaType = null;
  let artifactsCount = 0;
  let sha256Match = null;

  if (mode === 'cancel') {
    let resumeConfirmsGone = false;
    try {
      await client.turns.resume(session.id);
    } catch (err) {
      resumeConfirmsGone = err && err.name === 'RunNotActiveError';
    }
    ok = Boolean(stopResult && stopResult.stopped) && (resumeConfirmsGone || end.reason === 'run_gone' || end.reason === 'done');
    report({ ok, runId, sessionId: session.id, replayed: turn.replayed, events, stop: stopResult, end: end.reason });
    process.exit(ok ? 0 : 1);
    return;
  }

  const exported = await client.sessions.export(session.id, { fmt: 'md' });
  exportFilename = exported.filename;
  exportMediaType = exported.mediaType;

  const artifacts = await client.artifacts.list({ sessionId: session.id });
  artifactsCount = artifacts.length;
  if (artifacts.length) {
    const meta = await client.artifacts.get(artifacts[0].id);
    const bytes = await client.artifacts.download(artifacts[0].id);
    const hash = createHash('sha256').update(bytes).digest('hex');
    sha256Match = hash === meta.sha256;
  }

  ok = end.reason === 'done' && artifactsCount >= 1 && sha256Match === true;

  report({
    ok,
    runId,
    sessionId: session.id,
    replayed: turn.replayed,
    events,
    approvals,
    text: text.slice(0, 200),
    artifacts: artifactsCount,
    exportFilename,
    exportMediaType,
    sha256Match,
    end: end.reason,
  });
  process.exit(ok ? 0 : 1);
}

main().catch((err) => {
  report({ ok: false, error: String((err && err.message) || err) });
  process.exit(1);
});
