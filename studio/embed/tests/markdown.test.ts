/** The agent's Markdown inside the panel: real formatting, never HTML from the model, safe links only.
 * Bundle with esbuild (platform=node, packages=external), then run with Node. */
import assert from 'node:assert/strict';
import { Window } from 'happy-dom';
import { webcrypto } from 'node:crypto';

if (!globalThis.crypto) Object.defineProperty(globalThis, 'crypto', { value: webcrypto });
const dom = new Window({ url: 'https://host-app.example/notes/42' });
for (const key of ['window', 'document', 'HTMLElement', 'customElements', 'CustomEvent', 'Event', 'Node', 'ShadowRoot', 'MutationObserver', 'getComputedStyle']) {
  (globalThis as Record<string, unknown>)[key] = (dom as unknown as Record<string, unknown>)[key];
}
const { renderMarkdownInto, toolLabel } = await import('../src/markdown-dom.js');
const { FaustusChatElement, defineFaustusChat } = await import('../src/faustus-chat.js');
defineFaustusChat();
const pause = (ms = 10) => new Promise(resolve => setTimeout(resolve, ms));

const checks: Array<[string, () => Promise<void> | void]> = [
  ['bold, lists, code and tables become elements, text stays text', () => {
    const box = document.createElement('div');
    renderMarkdownInto(box, 'Argus tiene **44.872 capturas**.\n\n- hoy: `777`\n- ayer\n\n| app | n |\n|---|--:|\n| a | 1 |\n\n```py\nprint(1)\n```');
    assert.equal(box.querySelector('strong')?.textContent, '44.872 capturas');
    assert.equal(box.querySelectorAll('ul > li').length, 2);
    assert.equal(box.querySelector('li code')?.textContent, '777');
    assert.equal(box.querySelector('table td:last-child')?.getAttribute('style')?.includes('right'), true);
    assert.equal(box.querySelector('pre code')?.textContent, 'print(1)');
    assert.ok(!box.textContent!.includes('**'));
  }],
  ['the model cannot inject HTML or script links', () => {
    const box = document.createElement('div');
    renderMarkdownInto(box, '<img src=x onerror=alert(1)> [click](javascript:alert(1)) [web](https://example.com) [file](notes.md) ![p](http://example.com/a.png) ![q](/api/secret)');
    assert.equal(box.querySelector('img[onerror]'), null, 'raw HTML stays text');
    assert.ok(box.textContent!.includes('<img src=x onerror=alert(1)>'));
    const links = [...box.querySelectorAll('a')].map(a => a.getAttribute('href'));
    assert.deepEqual(links, ['https://example.com'], 'only web/mail links are live');
    assert.equal(box.querySelector('a')!.getAttribute('rel'), 'noopener noreferrer');
    assert.deepEqual([...box.querySelectorAll('img')].map(i => i.getAttribute('src')), ['http://example.com/a.png']);
  }],
  ['tool names read like tools, not like qualified ids', () => {
    assert.equal(toolLabel('mcp__4b142d9e__screen_status'), 'screen_status');
    assert.equal(toolLabel('mcp__fixture_server__read_file'), 'read_file');
    assert.equal(toolLabel('web_search'), 'web_search');
    assert.equal(toolLabel(undefined), '');
  }],
  ['a streamed reply ends formatted, and the history renders the same way', async () => {
    const frames = ['data: {"delta":"Active objects: dataset X and version v1.","thinking":true}', 'data: {"type":"tool_start","tool":"mcp__4b142d9e__screen_status","command":"{}"}', 'data: {"delta":"Hay **12"}', 'data: {"delta":"** capturas."}', 'data: [DONE]'];
    const transport: typeof fetch = async (input, init = {}) => {
      const url = new URL(String(input));
      if (url.pathname === '/api/chat_stream') return new Response(frames.join('\n\n') + '\n\n', { headers: { 'content-type': 'text/event-stream', 'X-Odysseus-Run-Id': 'md' } });
      if (url.pathname.startsWith('/api/hoard-copilot/conversations/')) return Response.json({ messages: [{ role: 'user', content: 'pregunta' }, { role: 'assistant', content: '*antes*', metadata: { tool_events: [{ tool: 'mcp__4b142d9e__screen_status', output: '{}' }] } }] });
      if (url.pathname.endsWith('/draft')) return Response.json({ text: '', attachment_ids: [], updated_at: 1 });
      if (url.pathname === '/api/hoard-copilot/conversations') return Response.json({ conversations: [{ session_id: 'md', name: 'md' }] });
      throw new Error(`Unhandled ${init.method || 'GET'} ${url.pathname}`);
    };
    const element = document.createElement('faustus-chat') as InstanceType<typeof FaustusChatElement>;
    element.server = 'https://faustus.example';
    element.fetch = transport;
    element.contextProvider = () => ({ origin: { app_id: 'argus', data_space_id: 'md-tests', surface: 'page' } });
    element.session = 'md';
    document.body.append(element);
    await element.loadHistory();
    const root = element.shadowRoot!;
    assert.equal(root.querySelector('.fc-msg[data-role="agent"] em')?.textContent, 'antes');
    const order = [...root.querySelectorAll('.fc-msg, .fc-tool')].map(n => n.classList.contains('fc-tool') ? 'tool' : n.getAttribute('data-role'));
    assert.deepEqual(order, ['user', 'tool', 'agent'], 'a saved reply shows its tools before its words');
    await element.send('¿cuántas?');
    for (let i = 0; i < 100 && root.querySelectorAll('.fc-msg[data-role="agent"] strong').length === 0; i += 1) await pause();
    const replies = root.querySelectorAll('.fc-msg[data-role="agent"]');
    const last = replies[replies.length - 1];
    assert.equal(last.querySelector('strong')?.textContent, '12', 'the two deltas join before formatting');
    assert.equal(last.textContent, 'Hay 12 capturas.', 'reasoning never lands in the reply');
    const thinking = root.querySelector('details.fc-thinking');
    assert.ok(thinking && !thinking.hasAttribute('open'), 'reasoning sits in its own folded block');
    assert.equal(thinking!.querySelector('.fc-thinking-body')?.textContent, 'Active objects: dataset X and version v1.');
    const tools = [...root.querySelectorAll('.fc-tool')]; const tool = tools[tools.length - 1].textContent || '';
    assert.ok(tool.includes('▶ screen_status') && !tool.includes('mcp__') && !tool.includes('{}'), tool);
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
console.log(`${checks.length - failed}/${checks.length} markdown checks passed`);
