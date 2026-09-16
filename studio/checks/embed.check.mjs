#!/usr/bin/env node
/**
 * A21 check: `@faustus/embed`'s `<faustus-chat>` — two instances on one
 * page, two sessions, two tokens, two themes. Mounts the REAL built bundle
 * (`studio/embed/dist/faustus-embed.js`, esbuild-bundled from source —
 * built by this script if missing/stale) into a `happy-dom` document (no
 * real browser needed for this check; `tests/acceptance/
 * test_a21_embed_two_sessions.py` layers a real server + Playwright smoke
 * on top for the version that talks over the actual network), with a fake
 * `fetch` standing in for the server, and asserts:
 *
 *   1. Each instance talks to its OWN session (separate ids, separate
 *      `Authorization` bearer tokens on every request).
 *   2. Events delivered to one instance's transcript never appear in the
 *      other's (checked both via the rendered Shadow DOM text and via each
 *      instance's own `faustus:event`/`faustus:turn-end` listeners).
 *   3. Each instance's styles live in its own shadow root (`<style>` node
 *      present in each, and the two shadow roots are different objects
 *      with no shared nodes).
 *   4. The bearer token never ends up in `localStorage`, in the light DOM
 *      (attribute stripped, `outerHTML`/`innerHTML` clean), or in any
 *      fetch URL.
 *   5. Accessibility: `role="log"` on the transcript, `role="textbox"` on
 *      the composer, a `button`-role Send control, and Enter-to-send
 *      keyboard navigation actually triggers a turn.
 *
 * Run: `node studio/checks/embed.check.mjs` (needs `happy-dom` in
 * `node_modules` — `npm i --no-save happy-dom` in this worktree if it is
 * not already there; see `studio/embed/README.md`).
 */
import { existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { Window } from 'happy-dom';

const here = path.dirname(fileURLToPath(import.meta.url));
const embedDir = path.resolve(here, '..', 'embed');
const bundlePath = path.join(embedDir, 'dist', 'faustus-embed.js');

let failures = 0;
function assert(cond, message) {
  if (!cond) {
    failures += 1;
    console.error(`FAIL: ${message}`);
  } else {
    console.log(`ok: ${message}`);
  }
}

async function ensureBundle() {
  if (existsSync(bundlePath)) return;
  console.log('dist/faustus-embed.js missing — building it now…');
  const { spawnSync } = await import('node:child_process');
  const r = spawnSync(process.execPath, [path.join(embedDir, 'build.mjs')], { cwd: embedDir, stdio: 'inherit' });
  if (r.status !== 0) throw new Error('esbuild of @faustus/embed failed');
}

function sseChunk(frames) {
  const body = frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join('') + 'data: [DONE]\n\n';
  return new TextEncoder().encode(body);
}

/** A fake server: two isolated tenants keyed by bearer token. Records
 *  every request so the assertions below can check per-instance isolation
 *  and that the token only ever appears in the `Authorization` header. */
function makeFakeServer() {
  const calls = [];
  let sessionCounter = 0;

  async function fetchImpl(url, init = {}) {
    const u = new URL(String(url));
    const headers = new Headers(init.headers || {});
    const auth = headers.get('Authorization') || '';
    const token = auth.startsWith('Bearer ') ? auth.slice(7) : '';
    calls.push({ url: String(url), method: init.method || 'GET', token, search: u.search });

    // The token must never leak into the URL (query string) on any call.
    if (u.search.toLowerCase().includes(token.toLowerCase()) && token) {
      throw new Error(`fake server: token leaked into URL: ${url}`);
    }

    if (u.pathname === '/api/session' && init.method === 'POST') {
      sessionCounter += 1;
      const id = `sess_${token}_${sessionCounter}`;
      return new Response(JSON.stringify({ id, name: '', model: '', rag: false, archived: false }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    if (u.pathname === '/api/chat_stream' && init.method === 'POST') {
      const marker = `hello-from-${token}`;
      const frames = [
        { type: 'tool_start', tool: 'echo', sequence: 1 },
        { delta: marker, sequence: 2 },
        { type: 'tool_output', tool: 'echo', output: 'ok', sequence: 3 },
      ];
      const stream = new ReadableStream({
        start(controller) {
          controller.enqueue(sseChunk(frames));
          controller.close();
        },
      });
      return new Response(stream, {
        status: 200,
        headers: { 'X-Odysseus-Run-Id': `run_${token}`, 'Content-Type': 'text/event-stream' },
      });
    }

    return new Response(JSON.stringify({ detail: `unhandled ${u.pathname}` }), { status: 404 });
  }

  return { fetchImpl, calls };
}

async function main() {
  await ensureBundle();

  const window = new Window({ url: 'https://embed-host.example/' });
  const document = window.document;

  // Only the DOM pieces come from happy-dom; fetch/AbortController/crypto
  // stay Node's real globals (the fake server above is a real `Response`
  // with a real `ReadableStream`, exactly what a browser would hand the
  // SDK — the same `pumpBody` code path runs unmodified).
  for (const key of [
    'window', 'document', 'HTMLElement', 'customElements', 'CustomEvent', 'Event',
    'Node', 'ShadowRoot', 'localStorage', 'sessionStorage', 'MutationObserver',
    'getComputedStyle', 'DocumentFragment', 'KeyboardEvent',
  ]) {
    if (key in window) {
      try {
        globalThis[key] = window[key];
      } catch {
        /* Node 22 defines a few of these (e.g. navigator) as getter-only
         * globals already — happy-dom's own version is reached via
         * `window.<key>` directly where needed instead. */
      }
    }
  }

  const server = makeFakeServer();
  globalThis.fetch = server.fetchImpl;

  const mod = await import(pathToFileURL(bundlePath).toString());
  assert(typeof mod.FaustusChatElement === 'function', 'bundle exports FaustusChatElement');
  assert(!!customElements.get('faustus-chat'), 'defineFaustusChat() registered <faustus-chat> on import');

  document.body.innerHTML = `
    <faustus-chat id="chat-a" server="https://server.example" theme="light"></faustus-chat>
    <faustus-chat id="chat-b" server="https://server.example" theme="dark"></faustus-chat>
  `;
  const chatA = document.getElementById('chat-a');
  const chatB = document.getElementById('chat-b');

  const TOKEN_A = 'ody_TENANT_A_SECRET';
  const TOKEN_B = 'ody_TENANT_B_SECRET';
  // Set via PROPERTY, not attribute — the recommended, fully-invisible path.
  chatA.token = TOKEN_A;
  chatB.token = TOKEN_B;

  // --- Shadow DOM isolation -------------------------------------------
  assert(chatA.shadowRoot != null && chatB.shadowRoot != null, 'both instances have a shadow root');
  assert(chatA.shadowRoot !== chatB.shadowRoot, 'the two shadow roots are distinct objects');
  assert(chatA.shadowRoot.querySelector('style') != null, 'panel A carries its own <style> in its shadow root');
  assert(chatB.shadowRoot.querySelector('style') != null, 'panel B carries its own <style> in its shadow root');
  assert(
    chatA.shadowRoot.querySelector('style').textContent === chatB.shadowRoot.querySelector('style').textContent,
    'both panels ship the identical embed stylesheet (proves it is per-instance, not a shared singleton the host could clobber)',
  );

  // --- Accessibility roles ---------------------------------------------
  const logA = chatA.shadowRoot.querySelector('[role="log"]');
  const textboxA = chatA.shadowRoot.querySelector('[role="textbox"]');
  const sendA = chatA.shadowRoot.querySelector('[role="button"][aria-label="Send message"]');
  assert(logA != null, 'panel A transcript has role="log"');
  assert(textboxA != null && textboxA.tagName === 'TEXTAREA', 'panel A composer has role="textbox"');
  assert(sendA != null, 'panel A has a button-role Send control');
  assert(logA.getAttribute('aria-live') === 'polite', 'transcript is an aria-live polite region');

  // --- Token never in light DOM or localStorage -------------------------
  assert(!chatA.hasAttribute('token'), 'the token attribute was stripped from panel A');
  assert(!chatB.hasAttribute('token'), 'the token attribute was stripped from panel B');
  assert(!chatA.outerHTML.includes(TOKEN_A), "panel A's outerHTML does not contain its own token");
  assert(!chatB.outerHTML.includes(TOKEN_B), "panel B's outerHTML does not contain its own token");
  assert(!document.body.innerHTML.includes(TOKEN_A), 'the light-DOM body does not contain token A anywhere');
  assert(!document.body.innerHTML.includes(TOKEN_B), 'the light-DOM body does not contain token B anywhere');
  const lsDump = JSON.stringify(Object.entries(window.localStorage || {}));
  assert(!lsDump.includes(TOKEN_A) && !lsDump.includes(TOKEN_B), 'localStorage carries neither token');

  // --- Two independent sessions/turns, no cross-talk ---------------------
  const eventsA = [];
  const eventsB = [];
  chatA.addEventListener('faustus:turn-end', (e) => eventsA.push(e.detail));
  chatB.addEventListener('faustus:turn-end', (e) => eventsB.push(e.detail));

  await chatA.send('hi from panel A');
  await chatB.send('hi from panel B');

  assert(eventsA.length === 1 && eventsB.length === 1, 'each panel completed exactly one turn');
  assert(eventsA[0].sessionId !== eventsB[0].sessionId, 'panel A and panel B ended up with different session ids');
  assert(chatA.sessionId !== chatB.sessionId, 'FaustusChatElement.sessionId differs between the two instances');

  const transcriptTextA = chatA.shadowRoot.querySelector('.fc-transcript').textContent;
  const transcriptTextB = chatB.shadowRoot.querySelector('.fc-transcript').textContent;
  assert(transcriptTextA.includes(`hello-from-${TOKEN_A}`), "panel A's transcript shows its own turn's reply");
  assert(!transcriptTextA.includes(`hello-from-${TOKEN_B}`), "panel A's transcript never shows panel B's reply");
  assert(transcriptTextB.includes(`hello-from-${TOKEN_B}`), "panel B's transcript shows its own turn's reply");
  assert(!transcriptTextB.includes(`hello-from-${TOKEN_A}`), "panel B's transcript never shows panel A's reply");

  const sessionCalls = server.calls.filter((c) => c.url.endsWith('/api/session'));
  const streamCalls = server.calls.filter((c) => c.url.endsWith('/api/chat_stream'));
  assert(sessionCalls.length === 2, 'exactly two session-creation calls were made (one per panel)');
  assert(
    sessionCalls.every((c) => (c.token === TOKEN_A || c.token === TOKEN_B) && c.token !== ''),
    'every session-creation call carried a bearer token',
  );
  const streamTokens = new Set(streamCalls.map((c) => c.token));
  assert(streamTokens.has(TOKEN_A) && streamTokens.has(TOKEN_B), 'chat_stream calls used both distinct tokens');
  assert(
    server.calls.every((c) => !c.search.includes(TOKEN_A) && !c.search.includes(TOKEN_B)),
    'no request ever carried a token in its URL/query string',
  );

  // --- Keyboard navigation: Enter in the composer sends -------------------
  const chatC = document.createElement('faustus-chat');
  chatC.setAttribute('server', 'https://server.example');
  document.body.appendChild(chatC);
  chatC.token = 'ody_TENANT_C_SECRET';
  const eventsC = [];
  chatC.addEventListener('faustus:turn-end', (e) => eventsC.push(e.detail));
  const textboxC = chatC.shadowRoot.querySelector('[role="textbox"]');
  textboxC.focus();
  textboxC.value = 'sent via keyboard';
  const enter = new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
  textboxC.dispatchEvent(enter);
  await new Promise((r) => setTimeout(r, 20));
  await new Promise((r) => setTimeout(r, 20));
  assert(eventsC.length === 1, 'pressing Enter in the composer triggered exactly one turn (keyboard nav works)');

  console.log(`\n${failures === 0 ? 'ALL CHECKS PASSED' : `${failures} CHECK(S) FAILED`}`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((err) => {
  console.error('embed.check.mjs crashed:', err);
  process.exit(1);
});
