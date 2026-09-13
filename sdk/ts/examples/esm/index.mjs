// A20 (paridad blueprint): the full walk — version → create session → send
// a turn → iterate events, answering any tool_approval ask_user → list
// artifacts — against a real server with a fake model, run by the
// integrator. MODE=cancel exercises cancel()/resume() instead. One line of
// JSON on stdout, `process.exit(ok ? 0 : 1)` — deterministic, no
// dependencies beyond the packed `faustus-sdk` tarball
// (`npm pack` in `sdk/ts`, then `npm install ../../faustus-sdk-0.1.0.tgz`
// here — see this package's README.md).
import { FaustusClient } from 'faustus-sdk';

const baseUrl = process.env.FAUSTUS_URL;
const token = process.env.FAUSTUS_TOKEN;
const endpointId = process.env.FAUSTUS_ENDPOINT_ID || '';
const endpointUrl = process.env.FAUSTUS_ENDPOINT_URL || '';
const model = process.env.FAUSTUS_MODEL || '';
const mode = process.env.MODE === 'cancel' ? 'cancel' : 'run';

function report(fields) {
  process.stdout.write(JSON.stringify(fields) + '\n');
}

async function main() {
  if (!baseUrl) throw new Error('FAUSTUS_URL is required');

  const client = new FaustusClient({ baseUrl, token });
  await client.version(); // fails fast, comprehensibly, if the server is unreachable or too old

  const session = await client.sessions.create({
    skipValidation: true,
    endpointId: endpointId || undefined,
    endpointUrl: endpointUrl || undefined,
    model: model || undefined,
  });

  let turn = await client.turns.create(session.id, {
    message: 'Say hello in one short sentence, then stop.',
    mode: 'agent',
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
        break; // resume the outer loop against the new Turn
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
  if (mode === 'cancel') {
    let resumeConfirmsGone = false;
    try {
      await client.turns.resume(session.id);
    } catch (err) {
      resumeConfirmsGone = err && err.name === 'RunNotActiveError';
    }
    ok = Boolean(stopResult && stopResult.stopped) && (resumeConfirmsGone || end.reason === 'run_gone' || end.reason === 'done');
  } else {
    ok = end.reason === 'done';
  }

  const artifacts = await client.artifacts.list({ sessionId: session.id });

  report({
    ok,
    runId,
    replayed: turn.replayed,
    events,
    approvals,
    text: text.slice(0, 200),
    artifacts: artifacts.length,
    end: end.reason,
  });
  process.exit(ok ? 0 : 1);
}

main().catch((err) => {
  report({ ok: false, error: String((err && err.message) || err) });
  process.exit(1);
});
