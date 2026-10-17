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
import type { ToolApprovalDecision, HoardContext, UploadedFile } from '../../../sdk/ts/src/types.js';
import { EMBED_STYLES } from './styles.js';
import { renderMarkdownInto, toolLabel } from './markdown-dom.js';

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
  /** The server's own explanation when the turn failed (queue wait exceeded, model error...). */
  error?: string;
}
export interface EmbedErrorDetail {
  sessionId: string | null;
  message: string;
  cause?: unknown;
}

type AttachmentDraft = { text: string; attachmentIds?: string[] };
type PendingFile = { key: string; file: File; state: 'uploading' | 'failed'; error?: string };
type PendingSend = { message: string; context?: HoardContext; id: string; attachmentIds: string[] };

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
  #attachBtn!: HTMLButtonElement;
  #fileInput!: HTMLInputElement;
  #attachmentList!: HTMLDivElement;
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
  #latestUserBubble: HTMLDivElement | null = null;
  #connected = false;
  #pendingInit: Promise<string> | null = null;
  #observer: AbortController | null = null;
  #fetch: typeof fetch | undefined;
  #contextProvider: (() => HoardContext | Promise<HoardContext>) | null = null;
  #contextLabel!: HTMLDivElement;
  #conversationSelect!: HTMLSelectElement;
  #conversationBar!: HTMLDivElement;
  #historyLoading = false;
  #pendingSends = new Map<string, PendingSend>();
  #sessionEpoch = 0;
  get #pendingSend(): PendingSend | null { return this.#pendingSends.get(this.#sessionId || '') || null; }
  set #pendingSend(value: PendingSend | null) {
    if (value) this.#pendingSends.set(this.#sessionId || '', value);
    else this.#pendingSends.delete(this.#sessionId || '');
  }
  onToolUndo: ((receipt: Record<string, unknown>) => Promise<void>) | null = null;
  onOpenSettings: (() => void) | null = null;
  #locale = '';
  #drafts = new Map<string, string>();
  #draftTimer: ReturnType<typeof setTimeout> | null = null;
  #pendingDraft: { sid: string; text: string; attachmentIds?: string[] } | null = null;
  #draftSave: Promise<unknown> = Promise.resolve();
  #failedDrafts = new Map<string, AttachmentDraft>();
  #attachments = new Map<string, UploadedFile[]>();
  #pendingFiles = new Map<string, PendingFile[]>();
  get locale(): string { return this.#locale || this.lang || document.documentElement.lang || 'en'; }
  set locale(value: string) { this.#locale = value || ''; this.#applyLocale(); }

  #applyLocale(): void {
    const es = this.locale.startsWith('es');
    this.#textarea.placeholder = es ? 'Mensaje…' : 'Message…';
    this.#textarea.setAttribute('aria-label', es ? 'Mensaje' : 'Message');
    this.#sendBtn.textContent = es ? 'Enviar' : 'Send';
    this.#sendBtn.setAttribute('aria-label', es ? 'Enviar mensaje' : 'Send message');
    this.#cancelBtn.textContent = es ? 'Detener' : 'Cancel';
    this.#cancelBtn.setAttribute('aria-label', es ? 'Detener' : 'Cancel');
    this.#attachBtn.textContent = es ? 'Adjuntar' : 'Attach';
    this.#attachBtn.setAttribute('aria-label', es ? 'Adjuntar archivos' : 'Attach files');
    this.#fileInput.setAttribute('aria-label', es ? 'Archivos adjuntos' : 'Attachments');
    this.#renderAttachments();
    const empty = this.#shadow.querySelector('.fc-empty');
    if (empty) empty.textContent = es ? 'Escribe un mensaje para empezar.' : 'Send a message to start this session.';
    this.#conversationSelect.setAttribute('aria-label', es ? 'Conversaciones del proyecto' : 'Project conversations');
    const newConversation = this.#conversationSelect.querySelector('option[value=""]');
    if (newConversation) newConversation.textContent = es ? 'Nueva conversación' : 'New conversation';
    for (const button of this.#shadow.querySelectorAll<HTMLButtonElement>('[data-label-en]')) {
      button.textContent = button.dataset[es ? 'labelEs' : 'labelEn'] || '';
    }
  }

  get fetch(): typeof fetch | undefined { return this.#fetch; }
  set fetch(value: typeof fetch | undefined) { this.#fetch = value; this.#client = null; }
  get contextProvider(): (() => HoardContext | Promise<HoardContext>) | null { return this.#contextProvider; }
  set contextProvider(value: (() => HoardContext | Promise<HoardContext>) | null) {
    this.#contextProvider = value;
    this.#conversationBar.hidden = !value;
    if (this.#connected && value) void this.refreshConversations();
  }
  get draft(): string { return this.#textarea.value; }
  set draft(value: string) { this.#textarea.value = value || ''; this.#queueDraft(); }
  appendDraft(text: string): void { this.draft = [this.draft, text].filter(Boolean).join('\n\n'); }
  /** Upload through the same native service used by Studio; keep files in their original chat. */
  async attachFiles(files: File[]): Promise<void> {
    if (!files.length || this.#busy) return;
    const originalSid = this.#sessionId, originalKey = originalSid || '';
    const pendingInit = this.#pendingInit, provider = this.#contextProvider, epoch = this.#sessionEpoch;
    const entries = files.map(file => ({ key: crypto.randomUUID(), file, state: 'uploading' as const })) as PendingFile[];
    this.#pendingFiles.set(originalKey, [...(this.#pendingFiles.get(originalKey) || []), ...entries]);
    this.#renderAttachments();
    try {
      const client = this.#ensureClient();
      const context = originalSid ? undefined : provider ? await provider() : undefined;
      const sid = await this.#ensureSession(client, context, originalSid, pendingInit, epoch);
      if (sid !== originalKey) {
        this.#pendingFiles.set(originalKey, (this.#pendingFiles.get(originalKey) || []).filter(entry => !entries.includes(entry)));
        this.#pendingFiles.set(sid, [...(this.#pendingFiles.get(sid) || []), ...entries]);
      }
      if (!this.#attachments.has(sid) && provider) {
        const remote = await client.copilot.getDraft(sid);
        this.#restoreAttachments(sid, remote.attachment_ids);
      }
      this.#renderAttachments();
      for (const entry of entries) await this.#uploadFile(sid, entry);
    } catch (error) {
      for (const entry of entries) { entry.state = 'failed'; entry.error = String((error as Error).message || error); }
      this.#renderAttachments();
      this.#emitError(String((error as Error).message || error), error);
    }
  }

  #attachmentIds(sid: string): string[] | undefined {
    return this.#attachments.get(sid)?.map(file => file.id);
  }

  #restoreAttachments(sid: string, ids: string[]): void {
    if (!this.#attachments.has(sid)) this.#attachments.set(sid, (ids || []).map(id => ({
      id, name: '', mime: '', size: 0,
    })));
    this.#renderAttachments();
  }

  async #uploadFile(sid: string, entry: PendingFile): Promise<void> {
    entry.state = 'uploading'; entry.error = undefined;
    this.#renderAttachments();
    try {
      const result = await this.#ensureClient().uploads.create([entry.file], sid);
      const file = result.files?.[0];
      if (!file?.id) throw new Error(this.locale.startsWith('es') ? 'No se pudo adjuntar el archivo. Reintenta.' : 'The file could not be attached. Retry.');
      if (!this.#pendingFiles.get(sid)?.includes(entry)) return;
      const ready = this.#attachments.get(sid) || [];
      if (!ready.some(item => item.id === file.id)) this.#attachments.set(sid, [...ready, file]);
      this.#pendingFiles.set(sid, this.#pendingFiles.get(sid)!.filter(item => item !== entry));
      this.#saveDraft(sid, this.#drafts.get(sid) ?? (sid === this.#sessionId ? this.draft : ''), this.#attachmentIds(sid));
    } catch (error) {
      entry.state = 'failed'; entry.error = String((error as Error).message || error);
    }
    this.#renderAttachments();
  }

  #renderAttachments(): void {
    if (!this.#attachmentList) return;
    const sid = this.#sessionId || '', es = this.locale.startsWith('es');
    const ready = this.#attachments.get(sid) || [], pending = this.#pendingFiles.get(sid) || [];
    this.#attachmentList.replaceChildren();
    this.#attachmentList.hidden = !ready.length && !pending.length;
    const row = (name: string, remove: () => void) => {
      const item = el('div', { class: 'fc-attachment', role: 'listitem' });
      item.append(el('span', { class: 'fc-attachment-name' }, name));
      const button = el('button', { type: 'button', class: 'fc-btn', 'aria-label': `${es ? 'Retirar' : 'Remove'} ${name}` }, es ? 'Retirar' : 'Remove');
      button.disabled = this.#busy;
      button.addEventListener('click', remove);
      item.append(button); this.#attachmentList.append(item);
      return item;
    };
    for (const [index, file] of ready.entries()) {
      const item = row(file.name || (es ? `Archivo adjunto ${index + 1}` : `Attached file ${index + 1}`), () => {
        this.#attachments.set(sid, (this.#attachments.get(sid) || []).filter(other => other.id !== file.id));
        this.#queueDraft(); this.#renderAttachments();
      });
      if (file.partial) item.append(el('span', { class: 'fc-attachment-note' }, file.partial_reason || (es ? 'Lectura parcial' : 'Partial reading')));
    }
    for (const entry of pending) {
      const item = row(entry.file.name, () => {
        this.#pendingFiles.set(sid, (this.#pendingFiles.get(sid) || []).filter(other => other !== entry));
        this.#renderAttachments();
      });
      item.append(el('span', { class: 'fc-attachment-note', role: 'status' }, entry.error || (es ? 'Adjuntando…' : 'Uploading…')));
      if (entry.state === 'failed') {
        const retry = el('button', { type: 'button', class: 'fc-btn', 'aria-label': `${es ? 'Reintentar' : 'Retry'} ${entry.file.name}` }, es ? 'Reintentar' : 'Retry');
        retry.addEventListener('click', () => {
          if (sid && this.#attachments.has(sid)) void this.#uploadFile(sid, entry);
          else {
            this.#pendingFiles.set(sid, (this.#pendingFiles.get(sid) || []).filter(other => other !== entry));
            void this.attachFiles([entry.file]);
          }
        });
        item.append(retry);
      }
    }
    this.#sendBtn.disabled = this.#busy || Boolean(pending.length);
  }
  async retry(): Promise<void> { if (this.#pendingSend && !this.#busy) await this.#runTurn(this.#pendingSend.message); }

  async refreshConversations(): Promise<void> {
    if (!this.#contextProvider) return;
    try {
      const context = await this.#contextProvider();
      if (typeof context.view?.locale === 'string') this.locale = context.view.locale;
      this.#contextLabel.textContent = [context.origin.app_name || context.origin.app_id, context.project?.title].filter(Boolean).join(' · ');
      const result = await this.#ensureClient().copilot.list(context);
      this.#conversationSelect.replaceChildren(el('option', { value: '' }, this.locale.startsWith('es') ? 'Nueva conversación' : 'New conversation'));
      for (const conversation of result.conversations) {
        if (!conversation.archived) this.#conversationSelect.appendChild(el('option', { value: conversation.session_id }, conversation.name || 'Conversación'));
      }
      this.#conversationSelect.value = this.#sessionId || '';
    } catch (error) { this.#emitError(String((error as Error).message || error), error); }
  }

  async loadHistory(): Promise<void> {
    if (!this.#sessionId || this.#busy || this.#historyLoading) return;
    this.#historyLoading = true;
    const sid = this.#sessionId;
    try {
      const client = this.#ensureClient();
      const conversation = await client.copilot.get(sid);
      if (sid !== this.#sessionId || this.#busy) return;
      if (this.#contextProvider && ((!this.draft && !this.#drafts.has(sid)) || !this.#attachments.has(sid))) {
        const remote = await client.copilot.getDraft(sid);
        if (sid !== this.#sessionId || this.#busy) return;
        // A locally recovered draft wins over an asynchronously fetched one.
        if (!this.draft && !this.#drafts.has(sid) && remote.text) this.#textarea.value = remote.text;
        this.#restoreAttachments(sid, remote.attachment_ids);
      }
      this.#transcript.replaceChildren();
      this.#messageCount = 0;
      for (const message of conversation.messages || []) {
        if (message.role === 'tool') { this.#appendTool(message.content); continue; }
        // A reply's tools ran before its words: show them first, the way the turn looked while it streamed.
        const toolEvents = message.metadata?.tool_events;
        if (message.role !== 'user' && Array.isArray(toolEvents)) for (const item of toolEvents) {
          if (item && typeof item === 'object') {
            const event = item as Record<string, unknown>;
            const output = String(event.output || '');
            this.#appendTool(`${toolLabel(event.tool) || 'Herramienta'}\n${output || JSON.stringify(item)}`, this.#undoReceipt(output));
          }
        }
        const bubble = this.#appendBubble(message.role === 'user' ? 'user' : 'agent', message.content);
        if (message.role === 'user') this.#appendMessageAttachments(bubble, message.metadata?.attachments);
        const legacy = message.metadata?.writer_tool_call as Record<string, unknown> | undefined;
        if (legacy && typeof legacy.auditIndex === 'number') this.#appendTool(`Writer\n${JSON.stringify(legacy)}`, { app_id: 'writer', audit_index: legacy.auditIndex });
      }
      this.#syncEmptyState();
      if (conversation.run_id) {
        this.#observer = new AbortController();
        try {
          const turn = await client.turns.resume(sid, { signal: this.#observer.signal });
          this.#setBusy(true);
          void this.#consumeTurn(client, turn, sid);
        } catch (error) {
          // A completed run needs history, not another generation.
          if ((error as { status?: number }).status !== 404) throw error;
        }
      }
    } catch (error) { this.#emitError(String((error as Error).message || error), error); }
    finally {
      this.#historyLoading = false;
      if (this.#connected && this.#sessionId !== sid) void this.loadHistory();
    }
  }

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
    const changed = this.#sessionId !== (v || null);
    this.#flushDraft();
    this.#drafts.set(this.#sessionId || '', this.draft);
    this.#sessionId = v || null;
    this.#textarea.value = this.#drafts.get(this.#sessionId || '') || '';
    if (changed) {
      ++this.#sessionEpoch;
      this.#pendingInit = null;
      if (this.#busy) {
        this.#observer?.abort(); // Detach this view; never cancel the server's turn.
        this.#observer = null; this.#activeTurn = null;
        this.#setBusy(false);
        this.#status.textContent = '';
        this.#transcript.replaceChildren(); this.#latestUserBubble = null;
        this.#messageCount = 0; this.#syncEmptyState();
        if (this.#connected && this.#sessionId) void this.loadHistory();
      }
    }
    this.#renderAttachments();
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
    if (this.#connected) return;
    this.#connected = true;
    this.#retryDrafts();
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
    this.#applyLocale();
    this.#syncEmptyState();
    if (this.#contextProvider) {
      void this.#initializeHostConversation();
    }
  }

  // Native dialog hosts use moveBefore to keep this same connected panel.
  // A state-preserving move must keep its observer, active turn and draft.
  connectedMoveCallback(): void {}

  disconnectedCallback(): void {
    // append-based reparenting disconnects and reconnects within this turn.
    // Only genuine removal detaches the observer; it never cancels remote work.
    queueMicrotask(() => {
      if (this.isConnected) return;
      this.#connected = false;
      this.#observer?.abort();
      this.#observer = null;
      this.#activeTurn = null;
      this.#setBusy(false);
      this.#flushDraft();
      this.#retryDrafts();
    });
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
        this.session = newValue;
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
    if ((!text && !this.#attachmentIds(this.#sessionId || '')?.length) || this.#busy || this.#pendingFiles.get(this.#sessionId || '')?.length) return;
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
    // A constructed sheet is allowed by a host page's strict content security policy (``style-src 'self'``), which
    // blocks a <style> element; browsers without adoptable sheets get the <style>.
    let adopted = false;
    try {
      const sheet = new CSSStyleSheet();
      sheet.replaceSync(EMBED_STYLES);
      this.#shadow.adoptedStyleSheets = [sheet];
      adopted = true;
    } catch {
      adopted = false;
    }
    if (!adopted) {
      const style = document.createElement('style');
      style.textContent = EMBED_STYLES;
      this.#shadow.appendChild(style);
    }

    this.#root = el('div', { class: 'fc-root' });

    const headerSlotHost = el('div', { class: 'fc-header' });
    headerSlotHost.appendChild(el('slot', { name: 'header' }));
    this.#root.appendChild(headerSlotHost);

    this.#conversationBar = el('div', { class: 'fc-conversations' });
    this.#conversationBar.hidden = true;
    this.#contextLabel = el('div', { class: 'fc-context' });
    this.#conversationSelect = el('select', { 'aria-label': 'Conversaciones del proyecto' }) as HTMLSelectElement;
    this.#conversationSelect.addEventListener('change', () => {
      if (this.#busy) return;
      this.#flushDraft();
      this.#drafts.set(this.#sessionId || '', this.draft);
      this.session = this.#conversationSelect.value || null;
      this.#textarea.value = this.#drafts.get(this.#sessionId || '') || '';
      this.#renderAttachments();
      this.#transcript.replaceChildren();
      this.#messageCount = 0;
      this.#syncEmptyState();
      this.#emit('faustus:session-change', { sessionId: this.#sessionId });
      if (!this.#sessionId && this.#contextProvider) void this.#initializeHostConversation();
      else void this.loadHistory();
    });
    const refresh = el('button', { type: 'button', class: 'fc-btn', 'data-label-en': 'Refresh', 'data-label-es': 'Actualizar' }, 'Refresh');
    refresh.addEventListener('click', () => { void this.refreshConversations(); void this.loadHistory(); });
    const retry = el('button', { type: 'button', class: 'fc-btn', 'data-label-en': 'Retry', 'data-label-es': 'Reintentar' }, 'Retry');
    retry.addEventListener('click', () => void this.retry());
    const remove = el('button', { type: 'button', class: 'fc-btn', 'data-label-en': 'Delete chat', 'data-label-es': 'Borrar chat' }, 'Delete chat');
    remove.addEventListener('click', () => {
      if (!this.#sessionId || this.#busy) return;
      void this.#ensureClient().sessions.remove(this.#sessionId).then(() => {
        this.#sessionId = null;
        this.#transcript.replaceChildren();
        this.#messageCount = 0;
        this.#syncEmptyState();
        this.#emit('faustus:session-change', { sessionId: null });
        return this.refreshConversations();
      }).catch(error => this.#emitError(String(error.message || error), error));
    });
    const settings = el('button', { type: 'button', class: 'fc-btn', 'data-label-en': 'Faustus settings', 'data-label-es': 'Ajustes de Faustus' }, 'Faustus settings');
    settings.addEventListener('click', () => {
      if (this.onOpenSettings) this.onOpenSettings();
      else window.open(this.#server.replace(/\/$/, '') + '/studio/settings', '_blank', 'noopener');
    });
    this.#conversationBar.append(this.#contextLabel, this.#conversationSelect, refresh, retry, remove, settings);
    this.#root.appendChild(this.#conversationBar);

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
    this.#attachmentList = el('div', { class: 'fc-attachments', role: 'list' });
    this.#attachmentList.hidden = true;
    this.#root.appendChild(this.#attachmentList);
    this.#fileInput = el('input', { type: 'file', multiple: '', hidden: '' }) as HTMLInputElement;
    this.#fileInput.addEventListener('change', () => {
      const files = Array.from(this.#fileInput.files || []);
      this.#fileInput.value = '';
      void this.attachFiles(files);
    });
    this.#attachBtn = el('button', { type: 'button', class: 'fc-btn fc-attach' }, 'Attach');
    this.#attachBtn.addEventListener('click', () => this.#fileInput.click());
    this.#textarea = el('textarea', {
      role: 'textbox',
      'aria-label': 'Message',
      'aria-multiline': 'true',
      placeholder: 'Message…',
      rows: '4',
    }) as HTMLTextAreaElement;
    this.#textarea.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        this.#onSubmit();
      }
    });
    this.#textarea.addEventListener('input', () => this.#queueDraft());
    composer.appendChild(this.#textarea);
    composer.append(this.#fileInput, this.#attachBtn);

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
    if (this.#busy || this.#pendingFiles.get(this.#sessionId || '')?.length) return;
    const text = this.#textarea.value;
    this.#textarea.value = '';
    void this.send(text);
  }

  #queueDraft(): void {
    this.#drafts.set(this.#sessionId || '', this.draft);
    if (!this.#contextProvider || !this.#sessionId) return;
    if (this.#pendingDraft && this.#pendingDraft.sid !== this.#sessionId) this.#flushDraft();
    if (this.#draftTimer) clearTimeout(this.#draftTimer);
    const sid = this.#sessionId;
    const text = this.draft;
    this.#pendingDraft = { sid, text, attachmentIds: this.#attachmentIds(sid) };
    this.#draftTimer = setTimeout(() => this.#flushDraft(), 400);
  }

  #flushDraft(): void {
    if (this.#draftTimer) clearTimeout(this.#draftTimer);
    this.#draftTimer = null;
    const pending = this.#pendingDraft;
    this.#pendingDraft = null;
    if (pending) this.#saveDraft(pending.sid, pending.text, pending.attachmentIds);
  }

  #saveDraft(sid: string, text: string, attachmentIds?: string[]): void {
    const client = this.#ensureClient();
    this.#draftSave = this.#draftSave.catch(() => {}).then(() => client.copilot.saveDraft(sid, text, attachmentIds))
      .then(() => { this.#failedDrafts.delete(sid); })
      .catch(error => {
        this.#failedDrafts.set(sid, { text: this.#drafts.get(sid) ?? text, attachmentIds: this.#attachmentIds(sid) ?? attachmentIds });
        this.#emitError(String((error as Error).message || error), error);
      });
  }

  #retryDrafts(): void {
    for (const [sid, draft] of this.#failedDrafts) this.#saveDraft(sid, draft.text, draft.attachmentIds);
  }

  async #initializeHostConversation(): Promise<void> {
    try {
      if (!this.#sessionId) await this.#ensureSession(this.#ensureClient(), await this.#contextProvider!());
      if (!this.#connected) return;
      await this.refreshConversations();
      await this.loadHistory();
    } catch (error) { this.#emitError(String((error as Error).message || error), error); }
  }

  #ensureClient(): FaustusClient {
    if (!this.#server) throw new Error('faustus-chat: the "server" attribute/property is required');
    if (!this.#client) {
      this.#client = new FaustusClient({ baseUrl: this.#server, token: this.#token ?? undefined, fetch: this.#fetch });
    }
    return this.#client;
  }

  async #ensureSession(client: FaustusClient, context?: HoardContext,
    originalSid = this.#sessionId, originalInit = this.#pendingInit, epoch = this.#sessionEpoch): Promise<string> {
    if (originalSid) return originalSid;
    if (originalInit) return originalInit;
    const pending = (async () => {
      const created = context ? await client.copilot.create(context) : await client.sessions.create({ skipValidation: true });
      const createdId = 'session_id' in created ? created.session_id : created.id;
      if (this.#sessionId || epoch !== this.#sessionEpoch) return createdId;
      this.#sessionId = createdId;
      this.#emit('faustus:session-change', { sessionId: this.#sessionId });
      if (this.#contextProvider) this.#queueDraft();
      return this.#sessionId;
    })();
    if (epoch === this.#sessionEpoch) this.#pendingInit = pending;
    try { return await pending; }
    finally { if (this.#pendingInit === pending) this.#pendingInit = null; }
  }

  #setBusy(busy: boolean): void {
    this.#busy = busy;
    this.#sendBtn.disabled = busy || Boolean(this.#pendingFiles.get(this.#sessionId || '')?.length);
    this.#attachBtn.disabled = busy;
    this.#textarea.disabled = busy;
    this.#cancelBtn.hidden = !busy;
    this.#conversationSelect.disabled = busy;
    this.#renderAttachments();
  }

  #syncEmptyState(): void {
    this.#root.setAttribute('data-has-messages', this.#messageCount > 0 ? 'true' : 'false');
    this.#transcript.setAttribute('data-empty', this.#messageCount > 0 ? 'false' : 'true');
  }

  #appendBubble(role: 'user' | 'agent' | 'error', text: string): HTMLDivElement {
    const node = el('div', { class: 'fc-msg', 'data-role': role });
    if (role === 'agent') this.#renderAgent(node, text);
    else node.textContent = text;
    if (role === 'user') this.#latestUserBubble = node;
    this.#transcript.appendChild(node);
    this.#messageCount += 1;
    this.#syncEmptyState();
    this.#transcript.scrollTop = this.#transcript.scrollHeight;
    return node;
  }

  #appendMessageAttachments(bubble: HTMLDivElement, attachments: unknown): void {
    if (!Array.isArray(attachments)) return;
    const names = attachments.map(file => typeof file?.name === 'string' ? file.name : '').filter(Boolean);
    if (!names.length) return;
    bubble.querySelector('.fc-message-files')?.remove();
    bubble.append(el('div', { class: 'fc-message-files' }, names.join(' · ')));
  }

  /** A folded "Reasoning" block for a turn's thinking deltas; returns the element the text goes into. */
  #appendThinking(): HTMLElement {
    const details = el('details', { class: 'fc-thinking' });
    const es = this.locale.startsWith('es');
    details.appendChild(el('summary', { 'data-label-en': 'Reasoning', 'data-label-es': 'Razonamiento' }, es ? 'Razonamiento' : 'Reasoning'));
    const body = el('div', { class: 'fc-thinking-body' });
    details.appendChild(body);
    this.#transcript.appendChild(details);
    return body;
  }

  /** The agent's words as formatted Markdown; the raw text stays on the node for the streaming deltas. */
  #renderAgent(node: HTMLDivElement, text: string): void {
    node.dataset.raw = text;
    node.classList.add('fc-md');
    renderMarkdownInto(node, text);
  }

  /** Streaming: append a delta to the raw text and re-render at most once per frame. */
  #appendDelta(node: HTMLDivElement, delta: string): void {
    node.dataset.raw = (node.dataset.raw || '') + delta;
    if (node.dataset.pending) return;
    node.dataset.pending = '1';
    const flush = () => {
      delete node.dataset.pending;
      this.#renderAgent(node, node.dataset.raw || '');
      this.#transcript.scrollTop = this.#transcript.scrollHeight;
    };
    if (typeof requestAnimationFrame === 'function') requestAnimationFrame(flush);
    else setTimeout(flush, 16);
  }

  #appendTool(label: string, receipt?: Record<string, unknown>): HTMLDivElement {
    const node = el('div', { class: 'fc-tool' });
    this.#fillTool(node, label, receipt);
    this.#transcript.appendChild(node);
    this.#transcript.scrollTop = this.#transcript.scrollHeight;
    return node;
  }

  /** One line per tool call: the running line becomes the result when it arrives (matched by call id, else by tool). */
  #finishTool(callId: string, tool: string, label: string, receipt?: Record<string, unknown>): void {
    const running = [...this.#transcript.querySelectorAll<HTMLDivElement>('.fc-tool[data-running="1"]')];
    const node = (callId && running.find(item => item.dataset.call === callId))
      || running.find(item => !item.dataset.call && item.dataset.tool === tool)
      || (!callId ? running.find(item => item.dataset.tool === tool) : undefined);
    if (!node) { this.#appendTool(label, receipt); return; }
    node.replaceChildren();
    delete node.dataset.running;
    this.#fillTool(node, label, receipt);
  }

  #fillTool(node: HTMLDivElement, label: string, receipt?: Record<string, unknown>): void {
    const split = label.indexOf('\n');
    if (split >= 0) {
      const details = el('details');
      details.append(el('summary', {}, label.slice(0, split)), el('pre', {}, label.slice(split + 1)));
      node.appendChild(details);
    } else node.textContent = label;
    if (receipt && this.onToolUndo) {
      const undo = el('button', { type: 'button', class: 'fc-btn', 'data-label-en': 'Undo', 'data-label-es': 'Deshacer' }, this.locale.startsWith('es') ? 'Deshacer' : 'Undo');
      undo.addEventListener('click', () => {
        undo.disabled = true;
        void this.onToolUndo!(receipt).then(() => {
          undo.dataset.labelEn = 'Undone';
          undo.dataset.labelEs = 'Deshecho';
          undo.textContent = this.locale.startsWith('es') ? 'Deshecho' : 'Undone';
        })
          .catch(error => { undo.disabled = false; this.#emitError(String(error.message || error), error); });
      });
      node.appendChild(undo);
    }
  }

  #undoReceipt(output: string): Record<string, unknown> | undefined {
    try {
      const result = JSON.parse(output);
      const candidate = result?._undo || result?.result?._undo;
      return candidate && typeof candidate === 'object' ? candidate : undefined;
    } catch { return undefined; }
  }

  #emit<T>(name: string, detail: T): void {
    this.dispatchEvent(new CustomEvent(name, { detail, bubbles: true, composed: true }));
  }

  #emitError(message: string, cause?: unknown): void {
    this.#status.textContent = message;
    this.#emit<EmbedErrorDetail>('faustus:error', { sessionId: this.#sessionId, message, cause });
  }

  async #runTurn(message: string): Promise<void> {
    const originalSid = this.#sessionId, originalKey = originalSid || '';
    const originalInit = this.#pendingInit, epoch = this.#sessionEpoch;
    const mode = this.#mode;
    const attachmentIds = this.#attachmentIds(originalKey) || [];
    const previous = this.#pendingSend;
    const pending: PendingSend = previous && previous.message === message && JSON.stringify(previous.attachmentIds) === JSON.stringify(attachmentIds)
      ? previous : {message, id: crypto.randomUUID(), attachmentIds};
    this.#pendingSends.set(originalKey, pending);
    const restore = (sid = originalSid) => {
      if (epoch === this.#sessionEpoch) {
        this.#setBusy(false);
        if (!this.draft || this.draft === message) this.draft = message;
      } else if (sid && !this.#drafts.get(sid)) {
        this.#drafts.set(sid, message); this.#saveDraft(sid, message, pending.attachmentIds);
      }
    };
    let client: FaustusClient;
    try {
      client = this.#ensureClient();
    } catch (err) {
      restore();
      if (epoch === this.#sessionEpoch) this.#emitError(String((err as { message?: string })?.message ?? err), err);
      return;
    }

    this.#setBusy(true);
    this.#status.textContent = this.locale.startsWith('es') ? 'Preparando el trabajo…' : 'Sending…';

    let context: HoardContext | undefined;
    try {
      context = pending.context ?? (this.#contextProvider ? await this.#contextProvider() : undefined);
      if (!pending.context) pending.context = context ? structuredClone(context) : undefined;
      if (pending !== previous && epoch === this.#sessionEpoch) {
        const bubble = this.#appendBubble('user', message);
        this.#appendMessageAttachments(bubble, this.#attachments.get(this.#sessionId || ''));
      }
    } catch (error) {
      restore();
      if (epoch === this.#sessionEpoch) this.#emitError(String((error as Error).message || error), error);
      return;
    }

    let sessionId: string;
    try {
      sessionId = await this.#ensureSession(client, context, originalSid, originalInit, epoch);
      if (this.#pendingSends.get(originalKey) === pending) this.#pendingSends.delete(originalKey);
      this.#pendingSends.set(sessionId, pending);
    } catch (err) {
      restore();
      if (epoch === this.#sessionEpoch) this.#emitError(String((err as { message?: string })?.message ?? err), err);
      return;
    }

    let turn: Turn;
    const observer = new AbortController();
    try {
      if (epoch === this.#sessionEpoch) this.#observer = observer;
      turn = await client.turns.create(sessionId, {
        message, mode, hoardContext: context, clientMessageId: pending.id,
        attachments: pending.attachmentIds,
      }, { signal: observer.signal });
    } catch (err) {
      restore(sessionId);
      if (epoch === this.#sessionEpoch) this.#emitError(String((err as { message?: string })?.message ?? err), err);
      return;
    }
    if (epoch !== this.#sessionEpoch) { observer.abort(); return; }
    await this.#consumeTurn(client, turn, sessionId);
  }

  async #consumeTurn(client: FaustusClient, initialTurn: Turn, sessionId: string): Promise<void> {
    let turn = initialTurn;
    this.#activeTurn = turn;
    this.#emit<TurnStartDetail>('faustus:turn-start', { sessionId, runId: turn.runId });
    if (this.#pendingSend && !this.draft && this.#contextProvider) this.#queueDraft();

    let agentBubble: HTMLDivElement | null = null;
    let thinkingBox: HTMLElement | null = null;
    let sawAnswer = false;
    // The server's `error` frame says why a turn failed; the stream then closes without [DONE], the SDK's resume
    // finds no run and ends with `run_gone`. Keep the explanation so that ending does not wipe it.
    let turnError: string | undefined;

    outer: for (;;) {
      sawAnswer = false;
      try {
        for await (const event of turn) {
          this.#emit<SseEvent>('faustus:event', event);
          if (!this.#connected || this.#activeTurn !== turn || this.#sessionId !== sessionId) break outer;

          if (event.type === 'delta') {
            // Reasoning deltas (thinking: true) are the model thinking aloud, not the answer: they go in their own
            // folded block, the way Studio shows them, and never into the reply (which is saved without them).
            if (event.thinking) {
              if (!thinkingBox) thinkingBox = this.#appendThinking();
              thinkingBox.textContent += String(event.delta ?? '');
              continue;
            }
            if (!agentBubble) agentBubble = this.#appendBubble('agent', '');
            this.#appendDelta(agentBubble, String(event.delta ?? ''));
            continue;
          }
          if (event.type === 'attachments' && this.#latestUserBubble) {
            this.#appendMessageAttachments(this.#latestUserBubble, event.data);
            continue;
          }
          if (event.type === 'tool_start') {
            const node = this.#appendTool(`▶ ${toolLabel(event.tool)}${event.command && event.command !== '{}' ? `: ${event.command}` : ''}`);
            node.dataset.running = '1';
            node.dataset.tool = String(event.tool ?? '');
            if (event.call_id) node.dataset.call = String(event.call_id);
            continue;
          }
          if (event.type === 'tool_output') {
            const receipt = this.#undoReceipt(String(event.output || ''));
            this.#finishTool(String(event.call_id ?? ''), String(event.tool ?? ''),
              `✓ ${toolLabel(event.tool)}${event.output ? `\n${event.output}` : ''}`, receipt);
            continue;
          }
          if (event.type === 'ask_user') {
            // The SDK union also admits unknown event types, so the narrowing by \	ype\ needs saying.
            const decision = await this.#askUser(event as Extract<SseEvent, { type: 'ask_user' }>);
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
            turnError = String(event.error || event.message || event.text || event.detail || 'error');
            this.#emitError(turnError, event);
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
    if (this.#activeTurn !== turn) return;
    this.#setBusy(false);
    const failed = end.reason === 'error' || turnError !== undefined;
    this.#status.textContent = turnError
      ? turnError
      : end.reason === 'error' ? 'No se pudo completar la conexión. Puedes reintentar el mismo envío.' : '';
    this.#activeTurn = null;
    this.#observer = null;
    if (!failed && end.reason !== 'aborted') {
      const sentIds = new Set(this.#pendingSend?.attachmentIds || []);
      if (sentIds.size) {
        this.#attachments.set(sessionId, (this.#attachments.get(sessionId) || []).filter(file => !sentIds.has(file.id)));
        this.#queueDraft(); this.#renderAttachments();
      }
      if (this.#pendingSend && this.draft === this.#pendingSend.message) this.draft = '';
      this.#pendingSend = null;
    }
    this.#emit<TurnEndDetail>('faustus:turn-end', { sessionId, reason: failed ? 'error' : end.reason, error: turnError });
    if (this.#contextProvider && this.#connected) void this.refreshConversations();
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
