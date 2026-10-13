/** Real component/SDK behavior with private HTTP responses, no model or user data. */
import assert from 'node:assert/strict';
import { Window } from 'happy-dom';
import { webcrypto } from 'node:crypto';

if (!globalThis.crypto) Object.defineProperty(globalThis, 'crypto', { value: webcrypto });
const dom = new Window({ url: 'https://attachments.example/' });
for (const key of ['window', 'document', 'HTMLElement', 'customElements', 'CustomEvent', 'Event', 'Node', 'ShadowRoot', 'MutationObserver', 'getComputedStyle']) {
  (globalThis as Record<string, unknown>)[key] = (dom as unknown as Record<string, unknown>)[key];
}
const { FaustusChatElement, defineFaustusChat } = await import('../src/faustus-chat.js');
defineFaustusChat();
const pause = (ms = 10) => new Promise(resolve => setTimeout(resolve, ms));
async function until(predicate: () => boolean, label: string) {
  const deadline = Date.now() + 2500;
  while (!predicate() && Date.now() < deadline) await pause();
  assert.ok(predicate(), label);
}
function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>(done => { resolve = done; });
  return { promise, resolve };
}
function server() {
  const uploads: Array<{ sid: string; name: string; bytes: string }> = [];
  const drafts = new Map<string, { text: string; attachment_ids: string[] }>();
  const turns: Array<{ sid: string; ids: string[]; id: string; mode: string }> = [];
  const creates: string[] = [];
  let failUpload = false, failTurn = false;
  let uploadGate: ReturnType<typeof deferred> | null = null;
  let createGate: ReturnType<typeof deferred> | null = null;
  const transport: typeof fetch = async (input, init = {}) => {
    const url = new URL(String(input));
    const match = url.pathname.match(/^\/api\/sessions\/([^/]+)\/draft$/);
    if (match) {
      const sid = decodeURIComponent(match[1]);
      if (init.method === 'PATCH') {
        const body = JSON.parse(String(init.body));
        drafts.set(sid, {text: body.text, attachment_ids: body.attachment_ids ?? drafts.get(sid)?.attachment_ids ?? []});
      }
      return Response.json({...drafts.get(sid) ?? {text: '', attachment_ids: []}, updated_at: 1});
    }
    if (url.pathname === '/api/hoard-copilot/conversations') {
      if (init.method === 'POST') {
        const session_id = `created-${creates.length + 1}`; creates.push(session_id);
        await createGate?.promise;
        return Response.json({session_id});
      }
      return Response.json({conversations: ['a', 'b', ...creates].map(session_id => ({session_id, name: session_id}))});
    }
    if (url.pathname.startsWith('/api/hoard-copilot/conversations/')) return Response.json({messages: []});
    if (url.pathname === '/api/upload') {
      assert.ok(init.body instanceof FormData, 'Native multipart upload');
      const body = init.body as FormData;
      const file = body.get('files') as File;
      const sid = String(body.get('session_id'));
      uploads.push({sid, name: file.name, bytes: await file.text()});
      await uploadGate?.promise;
      if (failUpload) return Response.json({detail: 'Private upload offline'}, {status: 503});
      return Response.json({files: [{id: `${sid}-${file.name}`, name: file.name, size: file.size, mime: file.type}]});
    }
    if (url.pathname === '/api/chat_stream') {
      assert.ok(init.body instanceof FormData, 'Attached turns use native multipart');
      const body = init.body as FormData;
      turns.push({sid: String(body.get('session')), ids: JSON.parse(String(body.get('attachments'))), id: String(body.get('client_message_id')), mode: String(body.get('mode'))});
      if (failTurn) return Response.json({detail: 'Private turn unavailable'}, {status: 503});
      return new Response('data: {"delta":"Done"}\n\ndata: [DONE]\n\n', {headers: {'Content-Type': 'text/event-stream'}});
    }
    throw new Error(`Unhandled ${url.pathname}`);
  };
  return {uploads, drafts, turns, creates, transport, failUpload(value: boolean) {failUpload = value;}, failTurn(value: boolean) {failTurn = value;}, holdUpload() {uploadGate = deferred(); return uploadGate;}, holdCreate() {createGate = deferred(); return createGate;}};
}
function panel(api: ReturnType<typeof server>, sid: string | null = 'a') {
  const element = document.createElement('faustus-chat') as InstanceType<typeof FaustusChatElement>;
  element.server = 'https://faustus.example'; element.fetch = api.transport;
  element.contextProvider = () => ({origin: {app_id: 'writer', data_space_id: 'private-attachments', surface: 'editor'}});
  element.session = sid; document.body.append(element);
  return element;
}
const checks: Array<[string, () => Promise<void>]> = [
  ['uploaded files persist in the original draft and reach the native turn as IDs', async () => {
    const api = server(), element = panel(api); await pause(30);
    element.draft = 'Read these';
    await element.attachFiles([new File(['año,3'], 'informe.csv', {type: 'text/csv'}), new File(['second'], 'second.txt')]);
    await until(() => api.drafts.get('a')?.attachment_ids.length === 2, 'Persist both files');
    assert.deepEqual(api.uploads.map(item => item.bytes), ['año,3', 'second']);
    await element.send('Read these');
    assert.equal(api.turns[0].sid, 'a');
    assert.deepEqual(api.turns[0].ids, ['a-informe.csv', 'a-second.txt']);
    await until(() => api.drafts.get('a')?.attachment_ids.length === 0, 'Clear only delivered IDs');
  }],
  ['pending upload cannot move to another chat or appear in its draft', async () => {
    const api = server(), element = panel(api); await pause(30);
    const gate = api.holdUpload(); const attaching = element.attachFiles([new File(['A'], 'only-a.txt')]);
    await until(() => api.uploads.length === 1, 'Upload started');
    element.session = 'b'; element.draft = 'B draft';
    gate.resolve(); await attaching;
    await until(() => api.drafts.get('a')?.attachment_ids[0] === 'a-only-a.txt', 'Save to A');
    assert.equal(api.uploads[0].sid, 'a');
    assert.equal(element.draft, 'B draft');
    assert.equal(element.shadowRoot!.querySelector('.fc-attachments')!.textContent, '');
    element.session = 'a'; assert.ok(element.shadowRoot!.textContent!.includes('only-a.txt'));
  }],
  ['switching chats during context capture keeps upload ownership fixed', async () => {
    const api = server(), element = panel(api); await pause(30);
    const gate = deferred();
    element.contextProvider = async () => {await gate.promise; return {origin: {app_id: 'writer', data_space_id: 'private-attachments'}};};
    const attaching = element.attachFiles([new File(['A'], 'captured-a.txt')]);
    element.session = 'b'; gate.resolve(); await attaching;
    assert.equal(api.uploads[0].sid, 'a');
  }],
  ['upload failure retains the file and offers retry without clearing the message', async () => {
    const api = server(), element = panel(api); await pause(30); api.failUpload(true);
    element.draft = 'Keep this'; await element.attachFiles([new File(['retry'], 'retry.txt')]);
    assert.equal(element.draft, 'Keep this');
    assert.ok(element.shadowRoot!.textContent!.includes('Private upload offline'));
    assert.ok((element.shadowRoot!.querySelector('.fc-btn-send') as HTMLButtonElement).disabled);
    api.failUpload(false);
    (element.shadowRoot!.querySelector('[aria-label="Retry retry.txt"]') as HTMLButtonElement).click();
    await until(() => api.drafts.get('a')?.attachment_ids[0] === 'a-retry.txt', 'Retry completed');
    assert.equal(api.uploads.length, 2);
  }],
  ['restored draft files survive recreation and removing one never removes another', async () => {
    const api = server(); api.drafts.set('a', {text: 'Prior draft', attachment_ids: ['first', 'second']});
    const element = panel(api); await until(() => element.shadowRoot!.querySelectorAll('.fc-attachment').length === 2, 'Restore attachments');
    assert.equal(element.draft, 'Prior draft');
    (element.shadowRoot!.querySelector('[aria-label="Remove Attached file 1"]') as HTMLButtonElement).click();
    await until(() => api.drafts.get('a')?.attachment_ids.length === 1, 'Save removal');
    assert.deepEqual(api.drafts.get('a')!.attachment_ids, ['second']);
  }],
  ['failed send retries the same frozen file IDs and idempotency key', async () => {
    const api = server(), element = panel(api); await pause(30);
    await element.attachFiles([new File(['keep'], 'keep.txt')]); api.failTurn(true);
    await element.send('Use file'); assert.equal(element.draft, 'Use file');
    api.failTurn(false); await element.retry();
    assert.equal(api.turns.length, 2); assert.deepEqual(api.turns[0], api.turns[1]);
  }],
  ['attachment-only sends use the native empty-message contract', async () => {
    const api = server(), element = panel(api); await pause(30);
    await element.attachFiles([new File(['standalone'], 'standalone.txt')]);
    await element.send('');
    assert.equal(api.turns.length, 1);
    assert.deepEqual(api.turns[0].ids, ['a-standalone.txt']);
  }],
  ['sending during an asynchronous context capture stays in the original conversation', async () => {
    const api = server(), element = panel(api); await pause(30);
    await element.attachFiles([new File(['original'], 'original.txt')]);
    const gate = deferred();
    element.contextProvider = async () => {await gate.promise; return {origin: {app_id: 'writer', data_space_id: 'private-attachments'}};};
    const sending = element.send('Use the original file');
    element.session = 'b'; gate.resolve(); await sending;
    assert.equal(api.turns[0].sid, 'a');
    assert.deepEqual(api.turns[0].ids, ['a-original.txt']);
  }],
  ['a new B turn and draft survive the late completion of A context capture', async () => {
    const api = server(), element = panel(api); await pause(30);
    await element.attachFiles([new File(['A'], 'a.txt')]);
    api.drafts.set('b', {text: 'B draft', attachment_ids: ['b-file']});
    const gate = deferred();
    element.contextProvider = async () => {await gate.promise; return {origin: {app_id: 'writer', data_space_id: 'private-attachments'}};};
    const sendingA = element.send('Use A');
    element.session = 'b'; element.mode = 'chat';
    element.contextProvider = () => ({origin: {app_id: 'writer', data_space_id: 'private-attachments'}});
    await until(() => element.draft === 'B draft', 'B draft loads');
    await element.send('Use B');
    gate.resolve(); await sendingA;
    assert.deepEqual(api.turns.map(turn => [turn.sid, turn.ids]), [['b', ['b-file']], ['a', ['a-a.txt']]]);
    assert.deepEqual(api.turns.map(turn => [turn.sid, turn.mode]), [['b', 'chat'], ['a', 'agent']]);
    assert.equal(element.session, 'b'); assert.equal(element.draft, 'B draft');
    assert.equal(element.shadowRoot!.querySelector('.fc-transcript')!.textContent!.includes('Use A'), false);
  }],
  ['failed original context capture restores A without replacing B message', async () => {
    const api = server(), element = panel(api); await pause(30);
    await element.attachFiles([new File(['A'], 'a.txt')]);
    const gate = deferred();
    element.contextProvider = async () => {await gate.promise; throw new Error('Original context failed');};
    const sending = element.send('Restore A'); element.session = 'b'; element.draft = 'Preserve B';
    gate.resolve(); await sending;
    await until(() => api.drafts.get('a')?.text === 'Restore A', 'Original A draft restored');
    assert.equal(element.draft, 'Preserve B'); assert.equal(element.session, 'b');
    assert.deepEqual(api.drafts.get('a')!.attachment_ids, ['a-a.txt']);
  }],
  ['late new-conversation initialization cannot select itself over B', async () => {
    const api = server(), gate = api.holdCreate(); const element = panel(api, null);
    await until(() => api.creates.length === 1, 'Original creation in progress');
    element.session = 'b'; gate.resolve(); await pause(50);
    assert.equal(element.session, 'b');
    assert.equal(api.creates.length, 1);
  }],
];
let failed = 0;
for (const [name, check] of checks) {
  try {await check(); console.log(`PASS ${name}`);}
  catch (error) {++failed; console.error(`FAIL ${name}: ${(error as Error).message}`);}
  finally {document.body.replaceChildren(); await pause(20);}
}
await dom.happyDOM.close();
if (failed) process.exitCode = 1;
console.log(`${checks.length - failed}/${checks.length} attachment checks passed`);
