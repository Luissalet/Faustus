/**
 * `<faustus-chat>` — an embeddable, isolated chat session. A21 (paridad
 * blueprint §8): "Embed two sessions in an existing application" ->
 * "styles routing auth and session state remain isolated and accessible".
 *
 * Every instance owns its own `FaustusClient` (from `faustus-sdk`), its own
 * session id, its own in-flight `Turn`, and its own Shadow DOM — nothing is
 * module-level or shared across elements, so two `<faustus-chat>` tags on
 * one page never see each other's events, styles, or auth. There is no
 * router, no global CSS, and no framework runtime: this is a plain custom
 * element the host page can drop next to whatever UI it already has.
 *
 * The bearer token is read once (attribute or `.token = …` property),
 * kept only in a private field, and never written to `localStorage`,
 * `sessionStorage`, a URL, or left sitting in the light-DOM `token`
 * attribute (it is stripped the instant it is read) — see
 * `studio/checks/embed.check.mjs` for the check that enforces this.
 */
import { FaustusClient } from '../../../sdk/ts/src/client.js';
import type { Turn } from '../../../sdk/ts/src/turn.js';
import type { SseEvent } from '../../../sdk/ts/src/sse.js';
import type { ToolApprovalDecision } from '../../../sdk/ts/src/types.js';
import { EMBED_STYLES } from './styles.js';

export type FaustusChatTheme = 'light' | 'dark' | 'auto';

const OBSERVED = ['server', 'token', 'session', 'theme', 'mode'] as const;

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs?: Record<string, string>,
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text != null) node.textContent = text;
  return node;
}

/** Detail payloads for the three DOM events this element dispatches. Kept
 *  narrow on purpose — a host that wants the raw SSE stream listens to
 *  `faustus:event` instead, which carries every decoded `SseEvent`. */
export interface TurnStartDetail {
  sessionId: string;
  runId: string | null;
}
export interface TurnEndDetail {
  sessionId: string;
  reason: string;
}
export interface EmbedErrorDetail {
  sessionId: string | null;
  message: string;
  cause?: unknown;
}

export class FaustusChatElement extends HTMLElement {
  static get observedAttributes(): readonly string[] {
    return OBSERVED;
  }

  #shadow: ShadowRoot;
  #root!: HTMLDivElement;
  #transcript!: HTMLDivElement;
  #status!: HTMLDivElement;
  #textarea!: HTMLTextAreaElement;
  #sendBtn!: HTMLButtonElement;
  #cancelBtn!: HTMLButtonElement;
  #emptySlotHost!: HTMLDivElement;

  // Never reflected to an attribute, never persisted anywhere. This is the
  // one and only place the bearer token lives for this instance.
  #token: string | null = null;
  #server = '';
  #sessionId: string | null = null;
  #mode: 'chat' | 'agent' = 'agent';
  #client: FaustusClient | null = null;
  #activeTurn: Turn | null = null;
  #busy = false;
  #messageCount = 0;
  #connected = false;
  #pendingInit: string | null = null;

  constructor() {
    super();
    this.#shadow = this.attachShadow({ mode: 'open' });
    this.#buildShadowDom();
  }

  // ── Public property surface (mirrors the attributes, `token` write-only) ──

  get server(): string {
    return this.#server;
  }
  set server(v: string) {
    this.#server = v || '';
    this.#client = null;
  }

  /** Write-only in spirit: setting it never touches the DOM. Reading it
   *  back is allowed (a host framework may re-read its own state), but it
   *  is sourced from the private field only, never from an attribute. */
  get token(): string | null {
    return this.#token;
  }
  set token(v: string | null) {
    this.#token = v || null;
    this.#client = null;
    if (this.hasAttribute('token')) this.removeAttribute('token');
  }

  get session(): string | null {
    return this.#sessionId;
  }
  set session(v: string | null) {
    this.#sessionId = v || null;
  }

  get theme(): FaustusChatTheme {
    return (this.getAttribute('theme') as FaustusChatTheme) || 'auto';
  }
  set theme(v: FaustusChatTheme) {
    this.setAttribute('theme', v);
  }

  get mode(): 'chat' | 'agent' {
    return this.#mode;
  }
  set mode(v: 'chat' | 'agent') {
    this.#mode = v === 'chat' ? 'chat' : 'agent';
  }

  // ── Lifecycle ──────────────────────────────────────────────────────────

  connectedCallback(): void {
    this.#connected = true;
    if (this.hasAttribute('server')) this.#server = this.getAttribute('server') || '';
    if (this.hasAttribute('session')) this.#sessionId = this.getAttribute('session') || null;
    if (this.hasAttribute('mode')) this.mode = (this.getAttribute('mode') as 'chat' | 'agent') || 'agent';
    if (this.hasAttribute('token')) {
      // Capture once, then scrub — the attribute never lingers in the
      // light DOM past this turn of the microtask queue.
      this.#token = this.getAttribute('token') || null;
      this.removeAttribute('token');
    }
    this.#applyTheme();
    this.#syncEmptyState();
  }

  disconnectedCallback(): void {
    this.#connected = false;
    this.#activeTurn?.cancel().catch(() => {});
    this.#activeTurn = null;
  }

  attributeChangedCallback(name: string, oldValue: string | null, newValue: string | null): void {
    if (oldValue === newValue) return;
    switch (name) {
      case 'server':
        this.#server = newValue || '';
        this.#client = null;
        break;
      case 'token':
        // A token set (or re-set) via markup after connect is handled the
        // same way as the initial one: read once, strip immediately. The
        // resulting `removeAttribute` re-enters this callback with
        // `newValue === null`, which is a no-op below.
        if (newValue != null) {
          this.#token = newValue;
          this.#client = null;
          this.removeAttribute('token');
        }
        break;
      case 'session':
        this.#sessionId = newValue || null;
        break;
      case 'theme':
        this.#applyTheme();
        break;
      case 'mode':
        this.#mode = newValue === 'chat' ? 'chat' : 'agent';
        break;
      default:
        break;
    }
  }

  // ── Public API ─────────────────────────────────────────────────────────

  /** Sends a turn programmatically (the composer button calls this too). */
  async send(message: string): Promise<void> {
    const text = message.trim();
    if (!text || this.#busy) return;
    await this.#runTurn(text);
  }

  /** Cancels the in-flight turn, if any. Mirrors `Turn.cancel()`. */
  async cancel(): Promise<void> {
    if (this.#activeTurn) {
      try {
        await this.#activeTurn.cancel();
      } catch (err) {
        this.#emitError(String((err as { message?: string })?.message ?? err), err);
      }
    }
  }

  get sessionId(): string | null {
    return this.#sessionId;
  }

  get busy(): boolean {
    return this.#busy;
  }

  // ── Internals ──────────────────────────────────────────────────────────

  #buildShadowDom(): void {
    const style = document.createElement('style');
    style.textContent = EMBED_STYLES;
    this.#shadow.appendChild(style);

    this.#root = el('div', { class: 'fc-root' });

    const headerSlotHost = el('div', { class: 'fc-header' });
    headerSlotHost.appendChild(el('slot', { name: 'header' }));
    this.#root.appendChild(headerSlotHost);

    this.#transcript = el('div', {
      class: 'fc-transcript',
      role: 'log',
      'aria-live': 'polite',
      'aria-relevant': 'additions',
      'aria-label': 'Chat transcript',
    });
    this.#root.appendChild(this.#transcript);

    this.#emptySlotHost = el('div', { class: 'fc-empty-slot-host' });
    const emptySlot = el('slot', { name: 'empty' });
    const emptyFallback = el('div', { class: 'fc-empty' }, 'Send a message to start this session.');
    emptySlot.appendChild(emptyFallback);
    this.#emptySlotHost.appendChild(emptySlot);
    this.#root.appendChild(this.#emptySlotHost);

    this.#status = el('div', { class: 'fc-status', role: 'status', 'aria-live': 'polite' });
    this.#root.appendChild(this.#status);

    const composer = el('form', { class: 'fc-composer' });
    this.#textarea = el('textarea', {
      role: 'textbox',
      'aria-label': 'Message',
      'aria-multiline': 'true',
      placeholder: 'Message…',
      rows: '1',
    }) as HTMLTextAreaElement;
    this.#textarea.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        this.#onSubmit();
      }
    });
    composer.appendChild(this.#textarea);

    this.#cancelBtn = el('button', {
      type: 'button',
      class: 'fc-btn',
      'aria-label': 'Cancel',
    }, 'Cancel') as HTMLButtonElement;
    this.#cancelBtn.hidden = true;
    this.#cancelBtn.addEventListener('click', () => void this.cancel());
    composer.appendChild(this.#cancelBtn);

    this.#sendBtn = el('button', {
      type: 'submit',
      class: 'fc-btn-send',
      role: 'button',
      'aria-label': 'Send message',
    }, 'Send') as HTMLButtonElement;
    composer.appendChild(this.#sendBtn);

    composer.addEventListener('submit', (e) => {
      e.preventDefault();
      this.#onSubmit();
    });
    this.#root.appendChild(composer);

    const footerSlotHost = el('div', { class: 'fc-footer' });
    footerSlotHost.appendChild(el('slot', { name: 'footer' }));
    this.#root.appendChild(footerSlotHost);

    this.#shadow.appendChild(this.#root);
  }

  #applyTheme(): void {
    const t = this.theme;
    let resolved: 'light' | 'dark' = 'light';
    if (t === 'dark') resolved = 'dark';
    else if (t === 'auto' && typeof window !== 'undefined' && window.matchMedia) {
      resolved = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    }
    this.setAttribute('data-fc-theme', resolved);
  }

  #onSubmit(): void {
    const text = this.#textarea.value;
    this.#textarea.value = '';
    void this.send(text);
  }

  #ensureClient(): FaustusClient {
    if (!this.#server) throw new Error('faustus-chat: the "server" attribute/property is required');
    if (!this.#client) {
      this.#client = new FaustusClient({ baseUrl: this.#server, token: this.#token ?? undefined });
    }
    return this.#client;
  }

  async #ensureSession(client: FaustusClient): Promise<string> {
    if (this.#sessionId) return this.#sessionId;
    if (this.#pendingInit) return this.#pendingInit;
    const created = await client.sessions.create({ skipValidation: true });
    this.#sessionId = created.id;
    return created.id;
  }

  #setBusy(busy: boolean): void {
    this.#busy = busy;
    this.#sendBtn.disabled = busy;
    this.#textarea.disabled = busy;
    this.#cancelBtn.hidden = !busy;
  }

  #syncEmptyState(): void {
    this.#root.setAttribute('data-has-messages', this.#messageCount > 0 ? 'true' : 'false');
    this.#transcript.setAttribute('data-empty', this.#messageCount > 0 ? 'false' : 'true');
  }

  #appendBubble(role: 'user' | 'agent' | 'error', text: string): HTMLDivElement {
    const node = el('div', { class: 'fc-msg', 'data-role': role });
    node.textContent = text;
    this.#transcript.appendChild(node);
    this.#messageCount += 1;
    this.#syncEmptyState();
    this.#transcript.scrollTop = this.#transcript.scrollHeight;
    return node;
  }

  #appendTool(label: string): void {
    const node = el('div', { class: 'fc-tool' }, label);
    this.#transcript.appendChild(node);
    this.#transcript.scrollTop = this.#transcript.scrollHeight;
  }

  #emit<T>(name: string, detail: T): void {
    this.dispatchEvent(new CustomEvent(name, { detail, bubbles: true, composed: true }));
  }

  #emitError(message: string, cause?: unknown): void {
    this.#status.textContent = message;
    this.#emit<EmbedErrorDetail>('faustus:error', { sessionId: this.#sessionId, message, cause });
  }

  async #runTurn(message: string): Promise<void> {
    let client: FaustusClient;
    try {
      client = this.#ensureClient();
    } catch (err) {
      this.#emitError(String((err as { message?: string })?.message ?? err), err);
      return;
    }

    this.#appendBubble('user', message);
    this.#setBusy(true);
    this.#status.textContent = 'Sending…';

    let sessionId: string;
    try {
      const initPromise = this.#ensureSession(client);
      this.#pendingInit = await initPromise;
      sessionId = this.#pendingInit;
      this.#pendingInit = null;
    } catch (err) {
      this.#pendingInit = null;
      this.#setBusy(false);
      this.#emitError(String((err as { message?: string })?.message ?? err), err);
      return;
    }

    let turn: Turn;
    try {
      turn = await client.turns.create(sessionId, { message, mode: this.#mode });
    } catch (err) {
      this.#setBusy(false);
      this.#emitError(String((err as { message?: string })?.message ?? err), err);
      return;
    }
    this.#activeTurn = turn;
    this.#emit<TurnStartDetail>('faustus:turn-start', { sessionId, runId: turn.runId });

    let agentBubble: HTMLDivElement | null = null;
    let sawAnswer = false;

    outer: for (;;) {
      sawAnswer = false;
      try {
        for await (const event of turn) {
          this.#emit<SseEvent>('faustus:event', event);
          if (!this.#connected) break outer;

          if (event.type === 'delta') {
            if (!agentBubble) agentBubble = this.#appendBubble('agent', '');
            agentBubble.textContent += event.delta;
            this.#transcript.scrollTop = this.#transcript.scrollHeight;
            continue;
          }
          if (event.type === 'tool_start') {
            this.#appendTool(`▶ ${event.tool}${event.command ? `: ${event.command}` : ''}`);
            continue;
          }
          if (event.type === 'tool_output') {
            this.#appendTool(`✓ ${event.tool}${event.output ? `\n${event.output}` : ''}`);
            continue;
          }
          if (event.type === 'ask_user') {
            const decision = await this.#askUser(event);
            if (decision.kind === 'tool_approval') {
              turn = await client.turns.decideToolApproval(sessionId, {
                approvalId: decision.approvalId,
                decision: decision.decision,
              });
            } else {
              turn = await client.turns.answerQuestion(sessionId, {
                questionId: decision.questionId,
                optionIds: decision.optionIds,
                text: decision.text,
                revision: decision.revision,
              });
            }
            this.#activeTurn = turn;
            sawAnswer = true;
            break;
          }
          if (event.type === 'error') {
            this.#emitError(event.message || event.text || event.detail || 'error', event);
          }
        }
      } catch (err) {
        this.#emitError(String((err as { message?: string })?.message ?? err), err);
        break;
      }
      if (sawAnswer) continue;
      break;
    }

    const end = await turn.done.catch((err) => ({ reason: 'error' as const, lastSequence: 0, error: err }));
    this.#setBusy(false);
    this.#status.textContent = '';
    this.#activeTurn = null;
    this.#emit<TurnEndDetail>('faustus:turn-end', { sessionId, reason: end.reason });
  }

  /** Renders the ask_user card and resolves once the person picks an
   *  option (or free-text, when the event allows it). One card is shown at
   *  a time; the composer stays disabled (`#busy`) for the duration, so
   *  there is no way to race a second turn onto the same open question. */
  #askUser(event: Extract<SseEvent, { type: 'ask_user' }>): Promise<
    | { kind: 'tool_approval'; approvalId: string; decision: ToolApprovalDecision }
    | { kind: 'question'; questionId: string; optionIds?: string[]; text?: string; revision?: number }
  > {
    return new Promise((resolve) => {
      const card = el('div', { class: 'fc-ask' });
      card.appendChild(el('div', {}, event.data.question));
      const optsRow = el('div', { class: 'fc-ask-options' });

      const finish = (
        result:
          | { kind: 'tool_approval'; approvalId: string; decision: ToolApprovalDecision }
          | { kind: 'question'; questionId: string; optionIds?: string[]; text?: string; revision?: number },
      ) => {
        card.remove();
        resolve(result);
      };

      if (event.data.kind === 'tool_approval') {
        const approvalId = event.data.approval_id || '';
        const approve = el('button', { type: 'button', class: 'fc-btn fc-btn-primary' }, 'Approve');
        approve.addEventListener('click', () => finish({ kind: 'tool_approval', approvalId, decision: 'approve' }));
        const approveTask = el('button', { type: 'button', class: 'fc-btn' }, 'Approve for task');
        approveTask.addEventListener('click', () =>
          finish({ kind: 'tool_approval', approvalId, decision: 'approve_task' }),
        );
        const deny = el('button', { type: 'button', class: 'fc-btn' }, 'Deny');
        deny.addEventListener('click', () => finish({ kind: 'tool_approval', approvalId, decision: 'deny' }));
        optsRow.append(approve, approveTask, deny);
      } else {
        const questionId = event.data.question_id || '';
        const revision = event.data.revision;
        const options = Array.isArray(event.data.options) ? event.data.options : [];
        if (options.length === 0) {
          const text = el('div', {}, '(free text expected — use the composer)');
          optsRow.appendChild(text);
        }
        for (const raw of options) {
          const opt = raw as { id?: unknown; label?: unknown } | string;
          const id = typeof opt === 'string' ? opt : String(opt?.id ?? opt?.label ?? '');
          const label = typeof opt === 'string' ? opt : String(opt?.label ?? opt?.id ?? '');
          const btn = el('button', { type: 'button', class: 'fc-btn' }, label);
          btn.addEventListener('click', () =>
            finish({ kind: 'question', questionId, optionIds: [id], revision }),
          );
          optsRow.appendChild(btn);
        }
      }

      card.appendChild(optsRow);
      this.#transcript.appendChild(card);
      this.#transcript.scrollTop = this.#transcript.scrollHeight;
      const firstBtn = optsRow.querySelector('button');
      (firstBtn as HTMLButtonElement | null)?.focus();
    });
  }
}

let defined = false;

/** Registers `<faustus-chat>` if it isn't already — safe to call more than
 *  once (e.g. two bundles on the same page), unlike a bare
 *  `customElements.define` which throws on a duplicate tag name. */
export function defineFaustusChat(): void {
  if (defined || (typeof customElements !== 'undefined' && customElements.get('faustus-chat'))) {
    defined = true;
    return;
  }
  customElements.define('faustus-chat', FaustusChatElement);
  defined = true;
}
