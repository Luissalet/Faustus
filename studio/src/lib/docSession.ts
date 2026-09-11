import { useSyncExternalStore } from 'react';

/**
 * A shared document session (CMP-01/02, W2-A1): identity, draft, selection,
 * undo/redo and pending proposals for ONE document, kept in a module-level
 * store instead of component state. `SidePanel.tsx`'s `DocTab` and
 * `screens/documents/Editor.tsx` are two different mounted trees (the
 * panel lives inside `/studio`; the full editor is its own route at
 * `/documents/{id}`), so anything kept only in `useState` is lost the
 * moment either unmounts — which is exactly what "cambia de panel a
 * editor completo, a otra conversación y vuelve" does. A plain module
 * singleton survives every one of those navigations for the life of the
 * page load; the draft additionally survives a full reload via
 * localStorage (best-effort, wrapped in try/catch: private browsing or a
 * full storage quota must never break editing).
 *
 * What this module does NOT do: call `saveDoc`/`saveWorkspaceFile`, decide
 * three-way conflict resolution, or talk to the server at all. Saving
 * stays exactly where `fileConflict.ts`'s three-way view and the base-
 * revision-guarded `saveDoc(..., expectedContent)` already live — this
 * only tracks what the two surfaces show before either of them saves, so a
 * document nobody touched is never written just because the view changed
 * (`isDirty()` is the single source of truth both surfaces gate `save()`
 * on).
 */

/** A selection as index ranges into the document's plain-text content —
 *  never just the selected string, so a comment/reference can point at
 *  *where* the text was even if the same words appear elsewhere (CMP-03,
 *  §3.2: "el mismo párrafo dos veces"). Multiple ranges = a discontinuous
 *  selection (e.g. picked across a preview's rendered blocks). */
export interface DocRange {
  start: number;
  end: number;
}

/** One agent suggestion pending in this session — same shape as
 *  `adapters/chat.ts`'s `DocSuggestion` (kept structurally compatible,
 *  not imported, so this module has no dependency on the chat stream:
 *  Editor.tsx, which never sees SSE events, can still read suggestions the
 *  panel collected earlier in the same page load). */
export interface PendingSuggestion {
  id: string;
  find: string;
  replace: string;
  reason: string;
}

export interface DocSessionState {
  docId: string;
  /** The server's `version_count` this session last agreed with. Bumped by
   *  `sync()`, never guessed. */
  baseRevision: number;
  /** The content `draftText` is a diff against — i.e. what was last known
   *  to be on the server (or the initial content, before any save). */
  baseText: string;
  /** `null` = no draft: the document reads exactly as `baseText`. Distinct
   *  from `draftText === baseText` (a draft that happens to match, e.g.
   *  after an undo back to the saved text) — both count as "not dirty"
   *  (see `isDirty`), but only the latter still occupies a
   *  localStorage row and an undo history, because the user did type. */
  draftText: string | null;
  selection: DocRange[];
  undoStack: string[];
  redoStack: string[];
  suggestions: PendingSuggestion[];
  /** Set by `sync()` when it moves `baseRevision` forward WHILE a draft was
   *  pending — i.e. the agent (or another tab) changed the document under
   *  an unsaved edit. Cleared by `markSaved`/`discardDraft`. Distinct from
   *  `isDirty`: a freshly-typed, never-yet-rebased draft is dirty but not
   *  `rebasedPending` — only a draft that outlived a server update is. */
  rebasedPending: boolean;
}

type Listener = () => void;

const STORE = new Map<string, DocSessionState>();
const LISTENERS = new Map<string, Set<Listener>>();
const DRAFT_KEY_PREFIX = 'faustus.docSession.draft.';
const UNDO_LIMIT = 50;

interface StoredDraft {
  text: string;
  base: string;
  baseRevision: number;
}

function draftKey(docId: string): string {
  return DRAFT_KEY_PREFIX + docId;
}

function readStoredDraft(docId: string): StoredDraft | null {
  try {
    const raw = localStorage.getItem(draftKey(docId));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<StoredDraft>;
    if (typeof parsed.text !== 'string' || typeof parsed.base !== 'string') return null;
    return { text: parsed.text, base: parsed.base, baseRevision: typeof parsed.baseRevision === 'number' ? parsed.baseRevision : 0 };
  } catch {
    return null; // private mode, corrupt JSON, or storage unavailable
  }
}

function writeStoredDraft(docId: string, draft: StoredDraft | null): void {
  try {
    if (draft) localStorage.setItem(draftKey(docId), JSON.stringify(draft));
    else localStorage.removeItem(draftKey(docId));
  } catch {
    /* private mode / quota — the in-memory session still works this page load */
  }
}

function notify(docId: string): void {
  for (const cb of LISTENERS.get(docId) ?? []) cb();
}

function emptySession(docId: string, baseText: string, baseRevision: number): DocSessionState {
  return { docId, baseRevision, baseText, draftText: null, selection: [], undoStack: [], redoStack: [], suggestions: [], rebasedPending: false };
}

/**
 * Get (creating if needed) the session for `docId`. `serverBase`, when
 * given, is what the caller just fetched/received from the server — used
 * only to SEED a session that does not exist yet, or to fast-forward one
 * whose stored draft was against an older revision and matches it exactly
 * (nothing to preserve). An existing in-memory session is otherwise left
 * alone: `sync()` is the explicit, deliberate way to reconcile a live
 * session against fresh server content.
 */
export function getSession(docId: string, serverBase?: { content: string; version: number }): DocSessionState {
  let session = STORE.get(docId);
  if (session) return session;
  const stored = readStoredDraft(docId);
  const baseText = serverBase?.content ?? stored?.base ?? '';
  const baseRevision = serverBase?.version ?? stored?.baseRevision ?? 0;
  session = emptySession(docId, baseText, baseRevision);
  if (stored && stored.text !== stored.base) {
    // A draft survived a reload. Keep it even if `serverBase` disagrees —
    // exactly the "your draft is preserved" rule `SidePanel.tsx` already
    // applies to a mid-air server update; only `sync()` (called when the
    // surface actually observes a NEW server revision) may reconcile it.
    session.draftText = stored.text;
    session.baseText = stored.base;
    session.baseRevision = stored.baseRevision;
  }
  STORE.set(docId, session);
  return session;
}

function setSession(docId: string, next: DocSessionState): void {
  STORE.set(docId, next);
  if (next.draftText !== null && next.draftText !== next.baseText) writeStoredDraft(docId, { text: next.draftText, base: next.baseText, baseRevision: next.baseRevision });
  else writeStoredDraft(docId, null);
  notify(docId);
}

/** Whether this session has unsaved content — the one call both `DocTab`
 *  and the full editor gate `save()` on, so alternating panel/editor/tab
 *  never writes a document nobody changed. */
export function isDirty(session: DocSessionState): boolean {
  return session.draftText !== null && session.draftText !== session.baseText;
}

/** The text either surface should show: the draft if there is one, the
 *  base content otherwise. */
export function currentText(session: DocSessionState): string {
  return session.draftText ?? session.baseText;
}

/** Type into the document. Pass `{record: true}` to snapshot the PREVIOUS
 *  text onto the undo stack first — used for a discrete action (apply a
 *  suggestion, accept a comment's proposal, replace-all), not for every
 *  keystroke: the textarea already has native undo for typing, and an
 *  undo entry per keystroke would make the stack useless. */
export function setDraftText(docId: string, text: string, opts: { record?: boolean } = {}): DocSessionState {
  const session = getSession(docId);
  const before = currentText(session);
  if (text === before && !opts.record) return session;
  const undoStack = opts.record ? [...session.undoStack, before].slice(-UNDO_LIMIT) : session.undoStack;
  const next: DocSessionState = { ...session, draftText: text, undoStack, redoStack: opts.record ? [] : session.redoStack };
  setSession(docId, next);
  return next;
}

/** Discard the draft entirely: the document goes back to reading exactly
 *  as `baseText`, and the localStorage row is dropped. Undo/redo/selection
 *  are cleared with it — there is nothing left to undo back to once the
 *  edit itself is gone. */
export function discardDraft(docId: string): DocSessionState {
  const session = getSession(docId);
  const next: DocSessionState = { ...session, draftText: null, undoStack: [], redoStack: [], rebasedPending: false };
  setSession(docId, next);
  return next;
}

/** Reconcile against a NEW server revision (a save just completed
 *  elsewhere, or a fresh `getDoc`/`doc_update` arrived). If there is no
 *  unsaved draft, the session simply follows the server. If there IS one,
 *  the draft is preserved and only `baseText`/`baseRevision` move forward
 *  — the same "your draft is preserved; review before saving" contract
 *  `SidePanel.tsx` already had, now shared by the full editor too. */
export function sync(docId: string, server: { content: string; version: number }): DocSessionState {
  const session = getSession(docId, server);
  if (session.baseRevision === server.version && session.baseText === server.content) return session;
  const next: DocSessionState = { ...session, baseText: server.content, baseRevision: server.version, rebasedPending: session.rebasedPending || isDirty(session) };
  setSession(docId, next);
  return next;
}

/** Record that THIS session's draft was just saved as `server` — content
 *  matches what was submitted, so the draft is cleared (nothing left
 *  unsaved) rather than merely rebased against itself. */
export function markSaved(docId: string, server: { content: string; version: number }): DocSessionState {
  const session = getSession(docId);
  const next: DocSessionState = { ...session, baseText: server.content, baseRevision: server.version, draftText: null, undoStack: [], redoStack: [], rebasedPending: false };
  setSession(docId, next);
  return next;
}

export function setSelection(docId: string, ranges: DocRange[]): DocSessionState {
  const session = getSession(docId);
  const next: DocSessionState = { ...session, selection: ranges };
  setSession(docId, next);
  return next;
}

export function undo(docId: string): DocSessionState {
  const session = getSession(docId);
  if (!session.undoStack.length) return session;
  const undoStack = session.undoStack.slice(0, -1);
  const prev = session.undoStack[session.undoStack.length - 1];
  const redoStack = [...session.redoStack, currentText(session)];
  // `prev` may equal `baseText` (undoing all the way back to the saved
  // text) — it still counts as a draft, not `null`: there is a redo entry
  // waiting, so the localStorage row and undo history stay alive.
  const next: DocSessionState = { ...session, draftText: prev, undoStack, redoStack };
  setSession(docId, next);
  return next;
}

export function redo(docId: string): DocSessionState {
  const session = getSession(docId);
  if (!session.redoStack.length) return session;
  const redoStack = session.redoStack.slice(0, -1);
  const next: DocSessionState = { ...session, draftText: session.redoStack[session.redoStack.length - 1], undoStack: [...session.undoStack, currentText(session)].slice(-UNDO_LIMIT), redoStack };
  setSession(docId, next);
  return next;
}

/** Pending suggestions collected for this doc across the page's lifetime —
 *  fed by the chat stream (panel only) but readable from the full editor
 *  too, since it is the same in-memory session. New ones with an id not
 *  already known are appended; nothing is ever silently dropped. */
export function mergeSuggestions(docId: string, incoming: PendingSuggestion[]): DocSessionState {
  const session = getSession(docId);
  const known = new Set(session.suggestions.map((s) => s.id));
  const added = incoming.filter((s) => !known.has(s.id));
  if (!added.length) return session;
  const next: DocSessionState = { ...session, suggestions: [...session.suggestions, ...added] };
  setSession(docId, next);
  return next;
}

export function setSuggestions(docId: string, suggestions: PendingSuggestion[]): DocSessionState {
  const session = getSession(docId);
  const next: DocSessionState = { ...session, suggestions };
  setSession(docId, next);
  return next;
}

/** Drop everything about a document (its tab was closed for good, not just
 *  switched away from). Distinct from `discardDraft`: this also forgets
 *  suggestions and undo history, not only the draft. */
export function forgetSession(docId: string): void {
  STORE.delete(docId);
  writeStoredDraft(docId, null);
  notify(docId);
}

function subscribe(docId: string, cb: Listener): () => void {
  let set = LISTENERS.get(docId);
  if (!set) {
    set = new Set();
    LISTENERS.set(docId, set);
  }
  set.add(cb);
  return () => {
    set!.delete(cb);
    if (set!.size === 0) LISTENERS.delete(docId);
  };
}

/** React binding. Re-renders whenever `docId`'s session changes, from
 *  EITHER surface — `useSyncExternalStore` is what makes a module-level
 *  store safe to read during render (no tearing between the panel and a
 *  full editor that both happen to be mounted, e.g. two browser tabs is
 *  out of scope, but a `Popover`/portal reading the same doc is not). */
export function useDocSession(docId: string | null, serverBase?: { content: string; version: number }): DocSessionState | null {
  return useSyncExternalStore(
    (cb) => (docId ? subscribe(docId, cb) : () => {}),
    () => (docId ? getSession(docId, serverBase) : null),
    () => (docId ? getSession(docId, serverBase) : null),
  );
}

// ---------------------------------------------------------------------------
// Occurrence lookup (CMP-02, §3.2) — mirrors src/document_comments.py's
// quote+context anchoring so a repeated `find` is disambiguated the same
// way a comment's anchor is, without importing Python: the first match is
// NEVER silently applied once there is more than one.
// ---------------------------------------------------------------------------

export interface Occurrence {
  start: number;
  end: number;
  before: string;
  after: string;
}

const OCCURRENCE_CTX_CHARS = 40;

/** Every occurrence of `needle` in `text`, each with `OCCURRENCE_CTX_CHARS`
 *  of surrounding text — enough for a person to tell two occurrences of
 *  the same sentence apart without seeing the whole document. Empty
 *  `needle` finds nothing (never "matches everywhere"). */
export function findOccurrences(text: string, needle: string): Occurrence[] {
  if (!needle) return [];
  const out: Occurrence[] = [];
  let i = text.indexOf(needle);
  while (i !== -1) {
    const end = i + needle.length;
    out.push({ start: i, end, before: text.slice(Math.max(0, i - OCCURRENCE_CTX_CHARS), i), after: text.slice(end, end + OCCURRENCE_CTX_CHARS) });
    i = text.indexOf(needle, i + 1); // overlapping occurrences allowed, never skipped
  }
  return out;
}

/** Replace exactly the occurrence at `start`/`end` — never "the first
 *  match of this text", which is the bug CMP-02 exists to fix. */
export function replaceAt(text: string, start: number, end: number, replacement: string): string {
  return text.slice(0, start) + replacement + text.slice(end);
}

// ---------------------------------------------------------------------------
// Selection -> composer, comments -> composer (CMP-03, §3.2)
//
// Neither hand-writes into the chat as though the user typed it — both
// dispatch a structured, typed event that a listener (the composer) turns
// into its own context chip/attachment. This keeps `docSession.ts` free of
// any dependency on `Composer.tsx` (owned by a different lot this wave)
// while giving it a single, documented contract to wire up: see
// `docs/adaptations/decisions/CMP-01.md`.
// ---------------------------------------------------------------------------

export const COMPOSER_CONTEXT_EVENT = 'faustus:composer-doc-context';

export type ComposerContextAction = 'clarify' | 'keep_term' | 'fix' | 'comments';

export interface ComposerContextItem {
  quote: string;
  note?: string;
}

/** The `{doc, rangos, cita}` reference CMP-03 asks for — additional
 *  context for the NEXT message the human sends, never an instruction the
 *  agent executes by itself (the human still has to type and send). */
export interface ComposerContextDetail {
  doc: { id: string; title: string };
  ranges: DocRange[];
  action: ComposerContextAction;
  items: ComposerContextItem[];
}

/** Best-effort: a listener may not exist yet (Composer.tsx wires this up
 *  in whichever lot lands it), so this never throws — the caller decides
 *  what to tell the user (SidePanel.tsx shows a notice either way, since
 *  there is no ack from a `CustomEvent`). */
export function sendComposerContext(detail: ComposerContextDetail): void {
  try {
    window.dispatchEvent(new CustomEvent<ComposerContextDetail>(COMPOSER_CONTEXT_EVENT, { detail }));
  } catch {
    /* no window (SSR/tests) — nothing to send to */
  }
}
