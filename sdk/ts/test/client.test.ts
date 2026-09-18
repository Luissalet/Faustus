import { test } from 'node:test';
import assert from 'node:assert/strict';
import { FaustusClient } from '../src/client.js';
import { CLIENT_API_VERSION } from '../src/http.js';
import { FaustusApiError, QuestionConflictError, UpgradeRequiredError } from '../src/errors.js';
import { emptyResponse, fakeFetch, headerValue, jsonResponse, SSE_DONE, streamResponse, textStream } from './helpers.js';

test('sessions.create() sends form-urlencoded, not JSON', async () => {
  const { fetch, calls } = fakeFetch((call) => {
    assert.equal(call.method, 'POST');
    assert.ok(call.url.endsWith('/api/session'));
    assert.ok(call.init.body instanceof URLSearchParams, 'body must be a URLSearchParams (form-urlencoded)');
    const form = call.init.body as URLSearchParams;
    assert.equal(form.get('name'), 'My chat');
    assert.equal(form.get('skip_validation'), 'true');
    assert.equal(form.get('endpoint_id'), 'ep1');
    return jsonResponse({ id: 'sid1', name: 'My chat', model: '', rag: false, archived: false });
  });
  const client = new FaustusClient({ baseUrl: 'http://sdk-test', fetch });
  const session = await client.sessions.create({ name: 'My chat', skipValidation: true, endpointId: 'ep1' });
  assert.equal(session.id, 'sid1');
  assert.equal(calls.length, 1);
});

test('sessions.list() reads the bare array GET /api/sessions returns', async () => {
  const { fetch } = fakeFetch(() =>
    jsonResponse([{ id: 's1', name: 'a', folder: null, tokens: 0, is_important: false, created_at: null, updated_at: null, last_message_at: null, has_documents: false, has_images: false, mode: 'chat', message_count: 1 }]),
  );
  const client = new FaustusClient({ baseUrl: 'http://sdk-test', fetch });
  const sessions = await client.sessions.list();
  assert.equal(sessions.length, 1);
  assert.equal(sessions[0]?.id, 's1');
});

test('turns.create() sends JSON with `session` (never `session_id`) and compare_mode "false"', async () => {
  const { fetch, calls } = fakeFetch((call) => {
    assert.equal(headerValue(call.init, 'Content-Type'), 'application/json');
    const body = JSON.parse(String(call.init.body));
    assert.equal(body.session, 's1');
    assert.equal('session_id' in body, false);
    assert.equal(body.compare_mode, 'false');
    assert.equal(body.mode, 'agent');
    assert.ok(typeof body.client_message_id === 'string' && body.client_message_id.length > 0);
    return streamResponse(textStream([SSE_DONE]), { headers: { 'X-Faustus-Run-Id': 'run-1' } });
  });
  const client = new FaustusClient({ baseUrl: 'http://sdk-test', fetch });
  const turn = await client.turns.create('s1', { message: 'hi', mode: 'agent' });
  await turn.done;
  assert.equal(calls.length, 1);
});

test('turns.create() switches to multipart/form-data when attachments are present', async () => {
  const { fetch } = fakeFetch((call) => {
    assert.ok(call.init.body instanceof FormData);
    const fd = call.init.body as FormData;
    assert.equal(fd.get('session'), 's1');
    assert.equal(fd.get('attachments'), JSON.stringify(['up1']));
    return streamResponse(textStream([SSE_DONE]));
  });
  const client = new FaustusClient({ baseUrl: 'http://sdk-test', fetch });
  const turn = await client.turns.create('s1', { message: 'see this', mode: 'chat', attachments: ['up1'] });
  await turn.done;
});

test('headers: Authorization from token, no Cookie sent alongside it', async () => {
  const { fetch, calls } = fakeFetch(() => jsonResponse({ version: '9', build: {}, served_studio: null, client_adaptation_notice: null }));
  const client = new FaustusClient({ baseUrl: 'http://sdk-test', token: 'ody_abc', cookie: 'faustus_session=zzz', fetch });
  await client.version();
  const call = calls[0];
  assert.ok(call);
  assert.equal(headerValue(call.init, 'Authorization'), 'Bearer ody_abc');
  assert.equal(headerValue(call.init, 'Cookie'), undefined);
  assert.equal(headerValue(call.init, 'X-Faustus-Client-Version'), CLIENT_API_VERSION);
});

test('headers: Cookie is sent when there is no token', async () => {
  const { fetch, calls } = fakeFetch(() => jsonResponse({ version: '9', build: {}, served_studio: null, client_adaptation_notice: null }));
  const client = new FaustusClient({ baseUrl: 'http://sdk-test', cookie: 'faustus_session=zzz', fetch });
  await client.version();
  assert.equal(headerValue(calls[0]!.init, 'Cookie'), 'faustus_session=zzz');
});

test('426 → UpgradeRequiredError, on both a plain request and a turn POST', async () => {
  const { fetch } = fakeFetch(() => jsonResponse({ detail: 'Reload the app to pick up a compatible build.' }, { status: 426 }));
  const client = new FaustusClient({ baseUrl: 'http://sdk-test', fetch });
  await assert.rejects(() => client.sessions.list(), UpgradeRequiredError);
  await assert.rejects(() => client.turns.create('s1', { message: 'hi', mode: 'chat' }), UpgradeRequiredError);
});

test('409 question_not_resolved → QuestionConflictError carries `reason` and `questionId`', async () => {
  const { fetch } = fakeFetch(() =>
    jsonResponse({ error: 'question_not_resolved', reason: 'stale_revision', question_id: 'q1', detail: 'stale' }, { status: 409 }),
  );
  const client = new FaustusClient({ baseUrl: 'http://sdk-test', fetch });
  await assert.rejects(
    () => client.turns.answerQuestion('s1', { questionId: 'q1', optionIds: ['a'], revision: 2 }),
    (err: unknown) => {
      assert.ok(err instanceof QuestionConflictError);
      assert.equal(err.reason, 'stale_revision');
      assert.equal(err.questionId, 'q1');
      return true;
    },
  );
});

test('FaustusApiError reads a string `detail`', async () => {
  const { fetch } = fakeFetch(() => jsonResponse({ detail: 'The workspace does not exist.' }, { status: 400 }));
  const client = new FaustusClient({ baseUrl: 'http://sdk-test', fetch });
  await assert.rejects(
    () => client.sessions.list(),
    (err: unknown) => {
      assert.ok(err instanceof FaustusApiError);
      assert.equal(err.status, 400);
      assert.equal(err.detail, 'The workspace does not exist.');
      return true;
    },
  );
});

test('FaustusApiError reads a `{message}` detail object, then falls back to `error`/`message`', async () => {
  const { fetch } = fakeFetch(() => jsonResponse({ detail: { message: 'Approval denied by policy.', error_class: 'policy.denied' } }, { status: 403 }));
  const client = new FaustusClient({ baseUrl: 'http://sdk-test', fetch });
  await assert.rejects(
    () => client.sessions.list(),
    (err: unknown) => {
      assert.ok(err instanceof FaustusApiError);
      assert.equal(err.detail, 'Approval denied by policy.');
      assert.equal(err.errorClass, 'policy.denied');
      return true;
    },
  );

  const { fetch: fetch2 } = fakeFetch(() => jsonResponse({ error: 'top-level error string' }, { status: 500 }));
  const client2 = new FaustusClient({ baseUrl: 'http://sdk-test', fetch: fetch2 });
  await assert.rejects(
    () => client2.sessions.list(),
    (err: unknown) => {
      assert.ok(err instanceof FaustusApiError);
      assert.equal(err.detail, 'top-level error string');
      return true;
    },
  );
});

test('sessions.remove() calls DELETE and resolves without a body', async () => {
  const { fetch, calls } = fakeFetch((call) => {
    assert.equal(call.method, 'DELETE');
    return jsonResponse({ status: 'deleted' });
  });
  const client = new FaustusClient({ baseUrl: 'http://sdk-test', fetch });
  await client.sessions.remove('s1');
  assert.equal(calls.length, 1);
});

test('artifacts.download() returns raw bytes', async () => {
  const { fetch } = fakeFetch(() => new Response(new Uint8Array([1, 2, 3, 4]), { status: 200 }));
  const client = new FaustusClient({ baseUrl: 'http://sdk-test', fetch });
  const bytes = await client.artifacts.download('a1');
  assert.deepEqual(Array.from(bytes), [1, 2, 3, 4]);
});

test('approvals.pending()/active() read the "pending"/"active" arrays', async () => {
  const { fetch } = fakeFetch((call) => {
    if (call.url.endsWith('/api/approvals/pending')) return jsonResponse({ checked_at: 't', pending: [{ id: 'c1' }], count: 1 });
    return jsonResponse({ checked_at: 't', active: [{ id: 'c2' }], count: 1 });
  });
  const client = new FaustusClient({ baseUrl: 'http://sdk-test', fetch });
  const pending = await client.approvals.pending();
  const active = await client.approvals.active();
  assert.equal(pending[0]?.id, 'c1');
  assert.equal(active[0]?.id, 'c2');
});

test('questions.list() reads the "questions" array', async () => {
  const { fetch } = fakeFetch(() => jsonResponse({ questions: [{ question_id: 'q1' }], count: 1 }));
  const client = new FaustusClient({ baseUrl: 'http://sdk-test', fetch });
  const questions = await client.questions.list();
  assert.equal(questions[0]?.question_id, 'q1');
});

test('sessions.export() reads the raw bytes and the Content-Disposition filename', async () => {
  const { fetch, calls } = fakeFetch((call) => {
    assert.equal(call.method, 'GET');
    assert.ok(call.url.endsWith('/api/session/s1/export?fmt=md'));
    return new Response(new TextEncoder().encode('# chat\n'), {
      status: 200,
      headers: {
        'Content-Type': 'text/markdown',
        'Content-Disposition': "attachment; filename=\"chat.md\"; filename*=UTF-8''Informe%202026.md",
      },
    });
  });
  const client = new FaustusClient({ baseUrl: 'http://sdk-test', fetch });
  const result = await client.sessions.export('s1', { fmt: 'md' });
  assert.equal(new TextDecoder().decode(result.content), '# chat\n');
  assert.equal(result.filename, 'Informe 2026.md');
  assert.equal(result.mediaType, 'text/markdown');
  assert.equal(calls.length, 1);
});

test('sessions.export() defaults fmt to "md" and falls back to the plain filename when there is no filename*', async () => {
  const { fetch } = fakeFetch((call) => {
    assert.ok(call.url.endsWith('/api/session/s1/export?fmt=md'));
    return new Response(new Uint8Array([1, 2, 3]), {
      status: 200,
      headers: { 'Content-Disposition': 'attachment; filename="export.md"' },
    });
  });
  const client = new FaustusClient({ baseUrl: 'http://sdk-test', fetch });
  const result = await client.sessions.export('s1');
  assert.deepEqual(Array.from(result.content), [1, 2, 3]);
  assert.equal(result.filename, 'export.md');
});

test('sessions.export() surfaces a non-2xx as FaustusApiError, same as artifacts.download()', async () => {
  const { fetch } = fakeFetch(() => jsonResponse({ detail: 'Session not found' }, { status: 404 }));
  const client = new FaustusClient({ baseUrl: 'http://sdk-test', fetch });
  await assert.rejects(
    () => client.sessions.export('gone'),
    (err: unknown) => {
      assert.ok(err instanceof FaustusApiError);
      assert.equal(err.status, 404);
      assert.equal(err.detail, 'Session not found');
      return true;
    },
  );
});

test('turns.status() returns null on a 404 rather than throwing', async () => {
  const { fetch } = fakeFetch(() => emptyResponse(404));
  const client = new FaustusClient({ baseUrl: 'http://sdk-test', fetch });
  const status = await client.turns.status('s1');
  assert.equal(status, null);
});
