/** Each tool call is one line in the panel: the running line turns into its result. */
import assert from 'node:assert/strict';
import { Window } from 'happy-dom';
import { webcrypto } from 'node:crypto';

if (!globalThis.crypto) Object.defineProperty(globalThis, 'crypto', { value: webcrypto });
const dom = new Window({ url: 'https://tool-lines.example/' });
for (const key of ['window', 'document', 'HTMLElement', 'customElements', 'CustomEvent', 'Event', 'Node', 'ShadowRoot', 'MutationObserver', 'getComputedStyle']) {
  (globalThis as Record<string, unknown>)[key] = (dom as unknown as Record<string, unknown>)[key];
}
const { FaustusChatElement, defineFaustusChat } = await import('../src/faustus-chat.js');
defineFaustusChat();
const frames = [
  { type: 'tool_start', tool: 'mcp__ac3e1027__deck_list', command: '{}', call_id: 'call-1' },
  { type: 'tool_start', tool: 'mcp__ac3e1027__deck_get', command: '{"deck_id":"d1"}', call_id: 'call-2' },
  { type: 'tool_output', tool: 'mcp__ac3e1027__deck_list', output: '{"items":[]}', call_id: 'call-1' },
  { type: 'tool_output', tool: 'mcp__ac3e1027__deck_get', output: '{"id":"d1"}', call_id: 'call-2' },
  { type: 'tool_start', tool: 'lookup_tools', command: '{"query":"x"}' },
  { type: 'tool_output', tool: 'lookup_tools', output: '{"tools":[]}' },
  { type: 'delta', delta: 'Done.' },
];
const body = frames.map(frame => `data: ${JSON.stringify(frame)}\n\n`).join('') + 'data: [DONE]\n\n';
const transport: typeof fetch = async (input, init = {}) => {
  const url = new URL(String(input));
  if (url.pathname === '/api/hoard-copilot/conversations') {
    return Response.json(init.method === 'POST' ? { session_id: 'tools-session' } : { conversations: [] });
  }
  if (url.pathname.startsWith('/api/hoard-copilot/conversations/')) return Response.json({ messages: [] });
  if (url.pathname.includes('/draft')) return Response.json({ text: '', attachment_ids: [] });
  if (url.pathname.startsWith('/api/chat/resume/')) return Response.json({ detail: 'No active run' }, { status: 404 });
  if (url.pathname === '/api/chat_stream') {
    return new Response(new TextEncoder().encode(body), { headers: { 'content-type': 'text/event-stream', 'X-Odysseus-Run-Id': 'tools-run' } });
  }
  throw new Error(`Unexpected request ${url.pathname}`);
};
const panel = document.createElement('faustus-chat') as InstanceType<typeof FaustusChatElement>;
panel.server = 'https://faustus.example';
panel.fetch = transport;
panel.contextProvider = () => ({ origin: { app_id: 'tools-qa', data_space_id: 'private-tools' } });
document.body.append(panel);
await panel.send('List the decks');
const transcript = panel.shadowRoot!.querySelector('.fc-transcript')!;
for (let index = 0; index < 100 && !(transcript.textContent || '').includes('Done.'); index++) await new Promise(resolve => setTimeout(resolve, 10));
const lines = [...transcript.querySelectorAll('.fc-tool')];
assert.equal(lines.length, 3, 'one line per tool call');
assert.equal(transcript.querySelectorAll('.fc-tool[data-running="1"]').length, 0, 'no call left looking unfinished');
assert.match(lines[0].textContent || '', /✓ .*deck_list/);
assert.match(lines[1].textContent || '', /✓ .*deck_get/);
assert.match(lines[2].textContent || '', /✓ .*lookup_tools/);
panel.remove();
console.log('tool lines ok');
