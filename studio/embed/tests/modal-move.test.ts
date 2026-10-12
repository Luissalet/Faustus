import assert from 'node:assert/strict';
import { Window } from 'happy-dom';
import { webcrypto } from 'node:crypto';

if (!globalThis.crypto) Object.defineProperty(globalThis, 'crypto', { value: webcrypto });
const dom = new Window({ url: 'https://modal-move.example/' });
for (const key of ['window', 'document', 'HTMLElement', 'customElements', 'CustomEvent', 'Event', 'Node', 'ShadowRoot', 'MutationObserver', 'getComputedStyle']) {
  (globalThis as Record<string, unknown>)[key] = (dom as unknown as Record<string, unknown>)[key];
}
const { FaustusChatElement, defineFaustusChat } = await import('../src/faustus-chat.js');
defineFaustusChat();
let controller!: ReadableStreamDefaultController<Uint8Array>;
let streamed = false;
let aborted = false;
const transport: typeof fetch = async (input, init = {}) => {
  const url = new URL(String(input));
  if (url.pathname === '/api/hoard-copilot/conversations') {
    return Response.json(init.method === 'POST' ? { session_id: 'move-session' } : { conversations: [] });
  }
  if (url.pathname.startsWith('/api/hoard-copilot/conversations/')) return Response.json({ messages: [] });
  if (url.pathname.includes('/draft')) return Response.json({ text: '', attachment_ids: [] });
  if (url.pathname.startsWith('/api/chat/resume/')) return Response.json({ detail: 'No active run for this session' }, { status: 404 });
  if (url.pathname === '/api/chat_stream') {
    init.signal?.addEventListener('abort', () => {
      aborted = true;
      controller.error(new DOMException('The local observer detached', 'AbortError'));
    });
    const stream = new ReadableStream<Uint8Array>({ start(value) { controller = value; streamed = true; } });
    return new Response(stream, { headers: { 'content-type': 'text/event-stream', 'X-Odysseus-Run-Id': 'move-run' } });
  }
  throw new Error(`Unexpected request ${url.pathname}`);
};
const panel = document.createElement('faustus-chat') as InstanceType<typeof FaustusChatElement>;
panel.server = 'https://faustus.example';
panel.fetch = transport;
panel.contextProvider = () => ({ origin: { app_id: 'modal-qa', data_space_id: 'private-move' } });
document.body.append(panel);
const sending = panel.send('A normal request');
for (let index = 0; index < 200 && !streamed; index++) await new Promise(resolve => setTimeout(resolve, 10));
assert.ok(streamed, 'Normal agent stream started');
const composer = panel.shadowRoot!.querySelector('textarea')!;
assert.equal(composer.disabled, true);
panel.draft = 'Next unsent draft';
for (let index = 0; index < 4; index++) panel.connectedMoveCallback();
const modal = document.createElement('dialog');
document.body.append(modal);
// Older hosts reparent with append instead of the state-preserving move API.
for (let index = 0; index < 2; index++) {
  modal.append(panel);
  document.body.append(panel);
  await Promise.resolve();
}
assert.equal(panel.sessionId, 'move-session');
assert.equal(panel.draft, 'Next unsent draft');
assert.equal(composer.disabled, true, 'A state-preserving move keeps the active turn');
assert.equal(aborted, false, 'Both moveBefore and synchronous append keep the live observer');
controller.enqueue(new TextEncoder().encode('data: {"type":"delta","delta":"Response after moving"}\n\ndata: [DONE]\n\n'));
controller.close();
await sending;
for (let index = 0; index < 100 && !panel.shadowRoot!.querySelector('.fc-transcript')!.textContent!.includes('Response after moving'); index++) {
  await new Promise(resolve => setTimeout(resolve, 10));
}
assert.match(panel.shadowRoot!.querySelector('.fc-transcript')!.textContent || '', /Response after moving/);
assert.equal(aborted, false);
streamed = false;
const detachedTurn = panel.send('A turn while closing the host');
for (let index = 0; index < 200 && !streamed; index++) await new Promise(resolve => setTimeout(resolve, 10));
assert.ok(streamed);
panel.remove();
await Promise.resolve();
assert.equal(aborted, true, 'Genuine removal detaches the local stream observer');
assert.equal(panel.busy, false);
await detachedTurn;
await dom.happyDOM.abort();
console.log('PASS: shared component keeps SID, draft and live observer through moveBefore and append, and detaches only on genuine removal.');
