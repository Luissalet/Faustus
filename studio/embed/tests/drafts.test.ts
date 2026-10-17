/** Behavioral checks against the real component and SDK, with isolated HTTP state.
 * Bundle with esbuild (platform=node, packages=external), then run with Node.
 * No live model, production store or browser profile is involved.
 */
import assert from 'node:assert/strict';
import { Window } from 'happy-dom';
import { webcrypto } from 'node:crypto';

if (!globalThis.crypto) Object.defineProperty(globalThis, 'crypto', { value: webcrypto });

const dom = new Window({ url: 'https://draft-host.example/' });
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
function server(initial: Record<string, string> = {}) {
  const drafts = new Map(Object.entries(initial));
  const attachments = new Map<string, string[]>();
  const writes: Array<{ sid: string; text: string; attachment_ids?: string[] }> = [];
  const reads: string[] = [];
  const turns: string[] = [];
  const creates: string[] = [];
  const held = new Map<string, ReturnType<typeof deferred>>();
  let failWrites = false;
  let streamError = '';
  let errorDone = false;
  const transport: typeof fetch = async (input, init = {}) => {
    const url = new URL(String(input));
    const match = url.pathname.match(/^\/api\/sessions\/([^/]+)\/draft$/);
    if (match) {
      const sid = decodeURIComponent(match[1]);
      if (init.method === 'PUT' || init.method === 'PATCH') {
        if (failWrites) return Response.json({ detail: 'Offline' }, { status: 503 });
        const body = JSON.parse(String(init.body));
        writes.push({ sid, ...body });
        drafts.set(sid, body.text);
        attachments.set(sid, body.attachment_ids ?? (init.method === 'PATCH' ? attachments.get(sid) ?? [] : []));
        return Response.json({ ...body, updated_at: 1 });
      }
      reads.push(sid);
      // Capture the old server value before waiting, as a genuine HTTP response can.
      const body = { text: drafts.get(sid) ?? '', attachment_ids: attachments.get(sid) ?? [], updated_at: 1 };
      await held.get(sid)?.promise;
      return Response.json(body);
    }
    if (url.pathname === '/api/hoard-copilot/conversations') {
      if (init.method === 'POST') {
        const session_id = `created-${creates.length + 1}`; creates.push(session_id);
        await held.get('create')?.promise;
        return Response.json({ session_id });
      }
      return Response.json({ conversations: ['a', 'b', ...creates].map(session_id => ({ session_id, name: session_id })) });
    }
    if (url.pathname.startsWith('/api/hoard-copilot/conversations/')) return Response.json({ messages: [] });
    if (url.pathname === '/api/chat_stream') {
      turns.push(String(init.body));
      // A turn the server refuses mid-stream: an `error` frame, then the stream closes without [DONE].
      if (streamError) return new Response(`event: error\ndata: ${JSON.stringify({ error: streamError, error_class: 'first_token_timeout', sequence: 1 })}\n\n${errorDone ? 'data: [DONE]\n\n' : ''}`, { headers: { 'content-type': 'text/event-stream', 'X-Odysseus-Run-Id': 'draft-check' } });
      return new Response('data: {"delta":"done"}\n\ndata: [DONE]\n\n', { headers: { 'content-type': 'text/event-stream', 'X-Odysseus-Run-Id': 'draft-check' } });
    }
    if (url.pathname.startsWith('/api/chat/resume/')) return Response.json({ detail: 'No active run for this session' }, { status: 404 });
    throw new Error(`Unhandled request ${init.method || 'GET'} ${url.pathname}`);
  };
  return { drafts, attachments, writes, reads, turns, creates, held, transport, setOffline(value: boolean) { failWrites = value; }, failTurns(message: string, done = false) { streamError = message; errorDone = done; } };
}
function panel(api: ReturnType<typeof server>, sid: string | null = 'a') {
  const element = document.createElement('faustus-chat') as InstanceType<typeof FaustusChatElement>;
  element.server = 'https://faustus.example';
  element.fetch = api.transport;
  element.contextProvider = () => ({ origin: { app_id: 'writer', data_space_id: 'draft-tests', surface: 'editor' } });
  element.session = sid;
  document.body.append(element);
  return element;
}
async function changeChat(element: InstanceType<typeof FaustusChatElement>, sid: string) {
  await element.refreshConversations();
  const select = element.shadowRoot!.querySelector('select')!;
  select.value = sid;
  select.dispatchEvent(new Event('change'));
}
const checks: Array<[string, () => Promise<void>]> = [
  ['a terminal error followed by DONE stays visible and retains the draft for retry', async () => {
    const api = server(); const element = panel(api); await pause(30);
    const reason = 'The model returned no usable response.'; api.failTurns(reason, true);
    const ends: Array<{ reason: string; error?: string }> = [];
    element.addEventListener('faustus:turn-end', event => ends.push((event as CustomEvent).detail));
    element.draft = 'Finish the original request';
    await element.send(element.draft);
    await until(() => ends.length === 1, 'Turn ends after the terminal error');
    assert.equal(ends[0].reason, 'error');
    assert.equal(ends[0].error, reason);
    assert.equal(element.shadowRoot!.querySelector('.fc-status')!.textContent, reason);
    assert.equal(element.draft, 'Finish the original request');
    assert.equal(api.turns.length, 1);
  }],
  ['new host draft before SID initialization survives panel recreation without generating', async () => {
    const api = server(); const gate = deferred(); api.held.set('create', gate);
    const element = panel(api, null); let remembered: string | null = null;
    element.addEventListener('faustus:session-change', event => { remembered = (event as CustomEvent).detail.sessionId; });
    element.draft = 'typed before canonical identity';
    await until(() => api.creates.length === 1, 'Initialize canonical conversation'); gate.resolve();
    await until(() => Boolean(remembered), 'Host receives canonical identity'); const sid = remembered!;
    element.remove(); await until(() => api.drafts.get(sid) === 'typed before canonical identity', 'Persist draft before generation');
    const reopened = panel(api, sid); await until(() => reopened.draft === 'typed before canonical identity', 'Recreated panel restores draft');
    assert.equal(api.creates.length, 1); assert.equal(api.turns.length, 0, 'Opening a host and typing never generates a turn');
  }],
  ['opening a host and immediate send share one in-flight canonical creation', async () => {
    const api = server(); const gate = deferred(); api.held.set('create', gate); const element = panel(api, null);
    await until(() => api.creates.length === 1, 'Opening created one pending identity');
    const sending = element.send('one immediate message'); await pause(20); gate.resolve(); await sending;
    assert.equal(api.creates.length, 1, 'Concurrent initializer and send cannot create separate sessions');
    assert.equal(api.turns.length, 1); assert.equal(JSON.parse(api.turns[0]).session, element.sessionId);
  }],
  ['New conversation within an existing panel also preserves an unsent draft', async () => {
    const api = server(); const element = panel(api); await pause(30);
    await changeChat(element, ''); element.draft = 'new conversation unsent words';
    await until(() => Boolean(element.sessionId), 'New conversation receives canonical identity before sending');
    const sid = element.sessionId!; element.remove();
    await until(() => api.drafts.get(sid) === 'new conversation unsent words', 'Persist unsent new conversation');
    const reopened = panel(api, sid); await until(() => reopened.draft === 'new conversation unsent words', 'Recover new conversation draft');
    assert.equal(api.turns.length, 0); assert.equal(api.creates.length, 1);
  }],
  ['saved remote draft is restored into a fresh host panel', async () => {
    const api = server({ a: 'remote words' }); const element = panel(api);
    await until(() => element.draft === 'remote words', 'Restore server draft');
    assert.equal(api.writes.length, 0, 'Reading a draft must not write it');
  }],
  ['debounce saves only the latest text for its canonical SID', async () => {
    const api = server(); const element = panel(api); await pause(30);
    element.draft = 'first'; element.draft = 'latest';
    await until(() => api.writes.length > 0, 'Persist latest draft');
    assert.deepEqual(api.writes.map(row => [row.sid, row.text]), [['a', 'latest']]);
  }],
  ['rapid conversation switch and typing persist both drafts', async () => {
    const api = server(); const element = panel(api); await pause(30);
    element.draft = 'draft A'; await changeChat(element, 'b'); element.draft = 'draft B';
    await until(() => api.drafts.get('b') === 'draft B', 'Persist B');
    assert.equal(api.drafts.get('a'), 'draft A', 'Changing SID must flush A before B cancels the timer');
    await changeChat(element, 'a'); assert.equal(element.draft, 'draft A');
  }],
  ['disconnect after switching flushes prior SID without erasing untouched B', async () => {
    const api = server(); const element = panel(api); await pause(30);
    element.draft = 'draft A'; await changeChat(element, 'b'); element.remove();
    await until(() => api.drafts.get('a') === 'draft A', 'Flush previous SID');
    assert.equal(api.drafts.get('a'), 'draft A', 'Unmount cannot discard another SID pending save');
    assert.ok(!api.writes.some(row => row.sid === 'b'), 'An untouched B needs no empty overwrite');
  }],
  ['ordinary disconnect flushes immediately without waiting for debounce', async () => {
    const api = server(); const element = panel(api); await pause(30);
    element.draft = 'unsent words'; element.remove();
    await until(() => api.drafts.get('a') === 'unsent words', 'Flush unmounted panel');
  }],
  ['late remote response never replaces a deliberate local clear', async () => {
    const api = server({ a: 'old remote words' }); const gate = deferred(); api.held.set('a', gate);
    const element = panel(api); await until(() => api.reads.includes('a'), 'Remote read started');
    element.draft = 'new local words'; element.draft = ''; gate.resolve(); await pause(30);
    assert.equal(element.draft, '', 'An empty local edit still wins over the old remote value');
  }],
  ['late draft for A cannot appear in B after session switch', async () => {
    const api = server({ a: 'remote A', b: 'remote B' }); const gate = deferred(); api.held.set('a', gate);
    const element = panel(api); await until(() => api.reads.includes('a'), 'A read started');
    await changeChat(element, 'b'); gate.resolve();
    await until(() => element.draft === 'remote B', 'Latest SID retries history and loads B');
    assert.equal(element.sessionId, 'b');
  }],
  ['editing shared draft text preserves existing attachment identities', async () => {
    const api = server({ a: 'with attachment' }); api.attachments.set('a', ['studio-attachment']);
    const element = panel(api); await until(() => element.draft === 'with attachment', 'Load attached draft');
    element.draft = 'revised words'; await until(() => api.drafts.get('a') === 'revised words', 'Save edit');
    assert.deepEqual(api.attachments.get('a'), ['studio-attachment'], 'Host editing text cannot erase Studio attachments');
  }],
  ['failed save retains local draft and disconnect can retry it', async () => {
    const api = server(); const element = panel(api); const errors: Event[] = [];
    element.addEventListener('faustus:error', event => errors.push(event)); await pause(30);
    api.setOffline(true); element.draft = 'offline words'; await until(() => errors.length > 0, 'Report failed save');
    assert.equal(element.draft, 'offline words'); api.setOffline(false); element.remove();
    await until(() => api.drafts.get('a') === 'offline words', 'Retry on disconnect');
  }],
  ['failed saves for A and B are both retried without cross-session loss', async () => {
    const api = server(); const element = panel(api); const errors: Event[] = [];
    element.addEventListener('faustus:error', event => errors.push(event)); await pause(30);
    api.setOffline(true); element.draft = 'offline A'; await until(() => errors.length >= 1, 'A failed save');
    await changeChat(element, 'b'); element.draft = 'offline B'; await until(() => errors.length >= 2, 'B failed save');
    api.setOffline(false); element.remove();
    await until(() => api.drafts.get('a') === 'offline A' && api.drafts.get('b') === 'offline B', 'Retry both canonical drafts');
  }],
  ['reconnecting a detached element retries its failed draft', async () => {
    const api = server(); const element = panel(api); const errors: Event[] = [];
    element.addEventListener('faustus:error', event => errors.push(event)); await pause(30);
    api.setOffline(true); element.draft = 'reconnect words'; await until(() => errors.length > 0, 'Failed save');
    element.remove(); await pause(30); api.setOffline(false); document.body.append(element);
    await until(() => api.drafts.get('a') === 'reconnect words', 'Reconnect retries local unsaved text');
  }],
  ['accepted composer send clears the server draft without a second turn', async () => {
    const api = server({ a: 'send these words' }); const element = panel(api);
    const errors: string[] = [];
    element.addEventListener('faustus:error', event => errors.push((event as CustomEvent).detail.message));
    await until(() => element.draft === 'send these words', 'Load composer');
    element.shadowRoot!.querySelector('form')!.dispatchEvent(new Event('submit', { cancelable: true, bubbles: true }));
    try { await until(() => api.writes.some(row => row.sid === 'a' && row.text === ''), 'Clear accepted draft'); }
    catch (error) { throw new Error(`${(error as Error).message}; errors=${JSON.stringify(errors)}; local=${element.draft}`); }
    assert.equal(element.draft, '');
    assert.equal(api.turns.length, 1, 'Clearing an accepted draft must not send another turn');
  }],
  ['a turn the server refuses keeps its explanation, ends as an error and keeps the draft', async () => {
    const api = server(); const element = panel(api); await pause(30);
    const reason = 'The turn waited too long in the queue.'; api.failTurns(reason);
    const ends: Array<{ reason: string; error?: string }> = [];
    element.addEventListener('faustus:turn-end', event => ends.push((event as CustomEvent).detail));
    element.draft = 'please retry me';
    await element.send('please retry me');
    await until(() => ends.length === 1, 'Turn ends after the refused stream');
    assert.equal(ends[0].reason, 'error', 'A refused turn is an error, not a vanished run');
    assert.equal(ends[0].error, reason);
    const status = element.shadowRoot!.querySelector('[role="status"], .fc-status')?.textContent ?? '';
    assert.ok(status.includes(reason), `The status keeps the server's reason (got: ${status})`);
    assert.equal(element.draft, 'please retry me', 'The words stay for a retry');
  }],
];
let failed = 0;
for (const [name, check] of checks) {
  try { await check(); console.log(`PASS ${name}`); }
  catch (error) { failed += 1; console.error(`FAIL ${name}: ${(error as Error).message}`); }
  finally { document.body.replaceChildren(); await pause(20); }
}
await dom.happyDOM.close();
if (failed) process.exitCode = 1;
console.log(`${checks.length - failed}/${checks.length} draft checks passed`);
