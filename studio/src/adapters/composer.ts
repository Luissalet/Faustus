import { ApiError, asArray, getJson } from './api';
import { t } from '../i18n';

/**
 * Everything the composer needs beyond the stream itself: uploads, the
 * workspace (folder picker and `@` file search), the `#` rule, and the
 * per-session generation overrides. All existing endpoints, all shared
 * state kept in the exact localStorage keys the legacy UI reads
 * (`static/js/storage.js`), so switching shells never loses the folder.
 */

/* ── Workspace: the same key the legacy pill uses ── */

const WORKSPACE_KEY = 'odysseus-workspace';
const RAG_KEY = 'odysseus-rag-active';

function readLegacy(key: string): string {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return '';
    try {
      const parsed = JSON.parse(raw) as unknown;
      return typeof parsed === 'string' ? parsed : parsed ? String(parsed) : '';
    } catch {
      return raw;
    }
  } catch {
    return '';
  }
}

export function getWorkspace(): string {
  return readLegacy(WORKSPACE_KEY);
}

export function setWorkspace(path: string): void {
  try {
    // Raw, not JSON: the legacy Storage.set/get for this key are raw.
    if (path) localStorage.setItem(WORKSPACE_KEY, path);
    else localStorage.removeItem(WORKSPACE_KEY);
    document.dispatchEvent(
      new CustomEvent('odysseus:workspace-change', { detail: { workspace: path } }),
    );
  } catch {
    /* private mode */
  }
}

export function getRagActive(): boolean {
  const raw = readLegacy(RAG_KEY);
  return raw === 'true' || raw === '1';
}

export function setRagActive(on: boolean): void {
  try {
    localStorage.setItem(RAG_KEY, on ? 'true' : 'false');
  } catch {
    /* private mode */
  }
}

export function basename(path: string): string {
  const parts = path.replace(/[\\/]+$/, '').split(/[\\/]/);
  return parts[parts.length - 1] || path;
}

/* ── Folder browsing ── */

export interface BrowseResult {
  path: string;
  parent: string | null;
  dirs: { name: string; path: string }[];
  truncated: boolean;
  selectable: boolean;
}

export async function browseWorkspace(path: string, signal?: AbortSignal): Promise<BrowseResult> {
  const raw = await getJson<Partial<BrowseResult>>(
    `/api/workspace/browse?path=${encodeURIComponent(path)}`,
    signal,
  );
  return {
    path: raw.path ?? path,
    parent: raw.parent ?? null,
    dirs: asArray<{ name: string; path: string }>(raw.dirs),
    truncated: Boolean(raw.truncated),
    selectable: raw.selectable !== false,
  };
}

export async function vetWorkspace(path: string): Promise<string | null> {
  const raw = await getJson<{ ok?: boolean; path?: string; valid?: boolean }>(
    `/api/workspace/vet?path=${encodeURIComponent(path)}`,
  );
  if (raw.ok === false || raw.valid === false) return null;
  return raw.path ?? path;
}

/* ── Native OS picker ── */

export type PickKind = 'folder' | 'file' | 'files';

export interface NativePick {
  /** 'ok' with a vetted path/paths; 'cancelled' when the user closed the
   *  dialog; 'unavailable' when the server cannot open one (remote browser,
   *  no display, no toolkit) and the caller should fall back to the in-page
   *  browser. */
  status: 'ok' | 'cancelled' | 'unavailable';
  path?: string;
  paths?: string[];
  detail?: string;
}

/**
 * Ask the server to open the real Explorer/Finder/GTK dialog on its own
 * desktop (only possible when the browser runs on the same machine). Never
 * throws for the "can't" cases — those return `unavailable` so the UI can
 * show its own dialog instead; a rejected folder (vet failed) throws.
 */
export async function pickNative(kind: PickKind, initial = ''): Promise<NativePick> {
  let response: Response;
  try {
    response = await fetch('/api/workspace/pick', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ kind, initial }),
    });
  } catch {
    return { status: 'unavailable' };
  }
  let body: { path?: string; paths?: unknown; cancelled?: boolean; detail?: unknown } = {};
  try {
    body = (await response.json()) as typeof body;
  } catch {
    body = {};
  }
  const detail = body.detail === undefined ? '' : String(body.detail);
  if (response.status === 501 || response.status === 403 || response.status === 404) {
    return { status: 'unavailable', detail };
  }
  if (response.status === 409) {
    return { status: 'cancelled', detail: detail || t('A picker is already open.') };
  }
  if (!response.ok) throw new ApiError(detail || `pick responded ${response.status}`, response.status);
  if (body.cancelled) return { status: 'cancelled' };
  if (body.path) return { status: 'ok', path: body.path };
  const paths = asArray<string>(body.paths).map(String).filter(Boolean);
  if (paths.length) return { status: 'ok', paths };
  return { status: 'cancelled' };
}

/* ── `@` mentions ── */

export interface WorkspaceFile {
  path: string;
  size?: number;
  score?: number;
}

export async function searchWorkspaceFiles(
  workspace: string,
  q: string,
  signal?: AbortSignal,
): Promise<WorkspaceFile[]> {
  if (!workspace) return [];
  const raw = await getJson<{ files?: unknown }>(
    `/api/workspace/files?workspace=${encodeURIComponent(workspace)}&q=${encodeURIComponent(q)}&limit=10`,
    signal,
  );
  // file_mentions.search rows: {rel, name, dir, score}
  return asArray<Record<string, unknown>>(raw.files)
    .map((f) => ({
      path: String(f.rel ?? f.path ?? f.name ?? ''),
      size: typeof f.size === 'number' ? f.size : undefined,
      score: typeof f.score === 'number' ? f.score : undefined,
    }))
    .filter((f) => f.path);
}

/* ── `#` standing rule ── */

export async function rememberRule(
  workspace: string,
  text: string,
): Promise<{ path?: string; duplicate?: boolean }> {
  const response = await fetch('/api/workspace/instructions/remember', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify({ workspace, text }),
  });
  if (!response.ok) {
    let detail = '';
    try {
      detail = String(((await response.json()) as { detail?: unknown }).detail ?? '');
    } catch {
      detail = '';
    }
    throw new ApiError(detail || `remember responded ${response.status}`, response.status);
  }
  return (await response.json()) as { path?: string; duplicate?: boolean };
}

/* ── Uploads ── */

export interface Attachment {
  id: string;
  name: string;
  mime: string;
  size: number;
  width?: number;
  height?: number;
  /** Explicit user guidance, serialized into the visible message when sent. */
  referenceRole?: 'subject' | 'style' | 'composition';
  /** IDX-04: what `save_upload` (`src/upload_handler.py`) made of this file —
   *  "extracting" only ever means the ingestion signal itself errored (the
   *  file is usable, its readability just wasn't checked); a PDF otherwise
   *  comes back "ready" or "partial" in the same response as the upload
   *  itself, never a later transition. Absent on an older server. */
  status?: 'extracting' | 'ready' | 'partial';
  partial?: boolean;
  /** `"scanned"` (no extractable text layer) or `"cover_only"` (only the
   *  first page yielded text) — `document_processor.py::pdf_ingestion_signal`'s
   *  two reasons for `partial`. */
  partialReason?: string | null;
}

export function attachmentUrl(id: string): string {
  return `/api/upload/${encodeURIComponent(id)}`;
}

export function isImage(mime: string): boolean {
  return mime.startsWith('image/');
}

function decodeUploadedFiles(raw: { files?: unknown }): Attachment[] {
  return asArray<Record<string, unknown>>(raw.files).filter((f) => typeof f.id === 'string' && f.id.trim()).map((f) => ({
    id: String(f.id),
    name: String(f.name ?? 'archivo'),
    mime: String(f.mime ?? 'application/octet-stream'),
    size: typeof f.size === 'number' ? f.size : 0,
    width: typeof f.width === 'number' ? f.width : undefined,
    height: typeof f.height === 'number' ? f.height : undefined,
    status: f.status === 'extracting' || f.status === 'ready' || f.status === 'partial' ? f.status : undefined,
    partial: f.partial === true,
    partialReason: typeof f.partial_reason === 'string' ? f.partial_reason : undefined,
  }));
}

async function uploadFilesFetch(fd: FormData, signal?: AbortSignal): Promise<Attachment[]> {
  const response = await fetch('/api/upload', {
    method: 'POST',
    body: fd,
    credentials: 'same-origin',
    signal,
  });
  if (!response.ok) {
    let detail = '';
    try {
      detail = String(((await response.json()) as { detail?: unknown }).detail ?? '');
    } catch {
      detail = '';
    }
    throw new ApiError(detail || `upload responded ${response.status}`, response.status);
  }
  return decodeUploadedFiles((await response.json()) as { files?: unknown });
}

/** Multipart upload. Uses XHR when a progress callback is given (`fetch` has none). */
export async function uploadFiles(
  files: File[],
  sessionId?: string | null,
  signal?: AbortSignal,
  onProgress?: (ratio: number) => void,
): Promise<Attachment[]> {
  const fd = new FormData();
  for (const file of files) fd.append('files', file, file.name);
  if (sessionId) fd.append('session_id', sessionId);

  if (!onProgress || typeof XMLHttpRequest === 'undefined') {
    return uploadFilesFetch(fd, signal);
  }

  return new Promise<Attachment[]>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/upload');
    xhr.withCredentials = true;
    xhr.responseType = 'json';
    xhr.upload.onprogress = (event) => {
      if (!event.lengthComputable || event.total <= 0) return;
      onProgress(event.loaded / event.total);
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(decodeUploadedFiles((xhr.response ?? {}) as { files?: unknown }));
        return;
      }
      let detail = '';
      try {
        detail = String((xhr.response as { detail?: unknown } | null)?.detail ?? '');
      } catch {
        detail = '';
      }
      reject(new ApiError(detail || `upload responded ${xhr.status}`, xhr.status));
    };
    xhr.onerror = () => reject(new ApiError('upload failed', 0));
    xhr.onabort = () => reject(new DOMException('Upload aborted', 'AbortError'));
    if (signal) {
      if (signal.aborted) {
        reject(new DOMException('Upload aborted', 'AbortError'));
        return;
      }
      signal.addEventListener('abort', () => xhr.abort(), { once: true });
    }
    xhr.send(fd);
  });
}

/** History stores attachments in the user message's metadata; shapes vary. */
export function attachmentsFromMetadata(meta: Record<string, unknown>): Attachment[] {
  return asArray<unknown>(meta.attachments)
    .map((entry): Attachment | null => {
      if (typeof entry === 'string') {
        return { id: entry, name: entry, mime: 'application/octet-stream', size: 0 };
      }
      if (entry && typeof entry === 'object') {
        const e = entry as Record<string, unknown>;
        const id = String(e.id ?? e.file_id ?? '');
        if (!id) return null;
        return {
          id,
          name: String(e.name ?? e.filename ?? id),
          mime: String(e.mime ?? e.mime_type ?? e.type ?? 'application/octet-stream'),
          size: typeof e.size === 'number' ? e.size : 0,
        };
      }
      return null;
    })
    .filter((a): a is Attachment => a !== null);
}

/* ── Generation overrides (the /temp, /maxtokens, /topp, /think knobs) ── */

export interface GenOverrides {
  temperature?: number;
  max_tokens?: number;
  top_p?: number;
  top_k?: number;
  num_ctx?: number;
  think?: boolean;
}

export function describeGen(gen: GenOverrides): string {
  const parts: string[] = [];
  if (gen.temperature !== undefined) parts.push(`T ${gen.temperature}`);
  if (gen.max_tokens !== undefined) parts.push(`máx ${gen.max_tokens}`);
  if (gen.top_p !== undefined) parts.push(`top_p ${gen.top_p}`);
  if (gen.top_k !== undefined) parts.push(`top_k ${gen.top_k}`);
  if (gen.num_ctx !== undefined) parts.push(`ctx ${gen.num_ctx}`);
  if (gen.think !== undefined) parts.push(gen.think ? t('reasons') : t('no reasoning'));
  return parts.join(' · ');
}

/**
 * Mirrors `src/llm_core.py`'s `_THINKING_MODEL_PATTERNS`/`_supports_thinking`
 * exactly — same reasoning `SAMPLING_FIELDS`'s own comment in
 * `screens/Settings.tsx` gives for mirroring the backend's clamp ranges: the
 * composer needs this answer client-side (to decide whether the think
 * switch even shows) and there is no route worth a round trip just to ask
 * it. Keep this list identical to the backend's; a model added there and
 * not here shows no switch, never a wrong one.
 */
const THINKING_MODEL_PATTERNS = [
  'qwen3', 'qwq', 'deepseek-r1', 'deepseek-reasoner', 'deepseek-v4',
  'minimax', 'm2-reap', 'gemma', 'stepfun', 'step-3', 'step3',
  'magistral', 'mistral-small', 'mistral-medium',
];

export function supportsThinking(model: string | null | undefined): boolean {
  if (!model) return false;
  const m = model.toLowerCase();
  return THINKING_MODEL_PATTERNS.some((p) => m.includes(p));
}

/* ── Reasoning mode (Auto / Rápido / Pensar / A fondo) ──
 * Sent per turn as the `think_mode` form field; the server
 * (`src/think_mode.py`) turns it into think/reasoning_budget overrides and
 * answers with a `think_mode` event saying what it ran with. Kept per chat
 * in localStorage; a chat with no pick of its own uses the
 * `think_mode_default` setting. */

export type ThinkMode = 'auto' | 'fast' | 'think' | 'deep';
export const THINK_MODES: ThinkMode[] = ['auto', 'fast', 'think', 'deep'];

const THINK_MODE_ALIASES: Record<string, ThinkMode> = {
  auto: 'auto', automatico: 'auto', 'automático': 'auto',
  fast: 'fast', rapido: 'fast', 'rápido': 'fast', quick: 'fast',
  think: 'think', pensar: 'think',
  deep: 'deep', 'a fondo': 'deep', fondo: 'deep', profundo: 'deep',
};

export function parseThinkMode(value: unknown): ThinkMode | null {
  if (typeof value !== 'string') return null;
  return THINK_MODE_ALIASES[value.trim().toLowerCase().replace(/\s+/g, ' ')] ?? null;
}

export function thinkModeLabel(mode: ThinkMode): string {
  switch (mode) {
    case 'fast': return t('Fast#think_mode');
    case 'think': return t('Think#think_mode');
    case 'deep': return t('Deep#think_mode');
    default: return t('Auto#think_mode');
  }
}

/** The chip text: the pick, and after an Auto turn what Auto chose
 *  ("Auto · Think"). */
export function thinkModeChipText(mode: ThinkMode, chosen?: ThinkMode | null): string {
  if (mode === 'auto' && chosen && chosen !== 'auto') return `${t('Auto#think_mode')} · ${thinkModeLabel(chosen)}`;
  return thinkModeLabel(mode);
}

const THINK_MODE_KEY = 'faustus_studio_think_mode';

export function readThinkMode(sessionId: string | null | undefined): ThinkMode | null {
  if (!sessionId) return null;
  try {
    return parseThinkMode(window.localStorage.getItem(`${THINK_MODE_KEY}_${sessionId}`));
  } catch {
    return null;
  }
}

export function writeThinkMode(sessionId: string | null | undefined, mode: ThinkMode | null): void {
  if (!sessionId) return;
  try {
    if (mode) window.localStorage.setItem(`${THINK_MODE_KEY}_${sessionId}`, mode);
    else window.localStorage.removeItem(`${THINK_MODE_KEY}_${sessionId}`);
  } catch {
    /* private window or blocked storage: the pick lasts this visit only */
  }
}

/* ── The model's own reasoning levels ──
 * Read from the running server (`GET /api/models/reasoning-levels`, from the
 * model's chat template): Qwen3.8 lists low / medium / xhigh and rejects a
 * generic "high". A pick is kept per chat and sent per turn as
 * `reasoning_effort`; it wins over the reasoning mode. */

export interface ReasoningLevels { levels: string[]; default?: string | null; source?: string }

export async function getReasoningLevels(endpointId: string, signal?: AbortSignal): Promise<ReasoningLevels> {
  if (!endpointId) return { levels: [] };
  try {
    const raw = await getJson<ReasoningLevels>(
      `/api/models/reasoning-levels?endpoint_id=${encodeURIComponent(endpointId)}`, signal);
    return { levels: asArray<string>(raw?.levels), default: raw?.default ?? null, source: raw?.source };
  } catch {
    return { levels: [] };
  }
}

export function reasoningLevelLabel(level: string): string {
  switch (level) {
    case 'none': return t('Off#effort');
    case 'minimal': return t('Minimal#effort');
    case 'low': return t('Low#effort');
    case 'medium': return t('Medium#effort');
    case 'high': return t('High#effort');
    case 'xhigh': return t('Maximum#effort');
    default: return level;
  }
}

const THINK_EFFORT_KEY = 'faustus_studio_think_effort';

export function readThinkEffort(sessionId: string | null | undefined): string | null {
  if (!sessionId) return null;
  try {
    const v = window.localStorage.getItem(`${THINK_EFFORT_KEY}_${sessionId}`);
    return v && /^[a-z]{3,10}$/.test(v) ? v : null;
  } catch {
    return null;
  }
}

export function writeThinkEffort(sessionId: string | null | undefined, effort: string | null): void {
  if (!sessionId) return;
  try {
    if (effort) window.localStorage.setItem(`${THINK_EFFORT_KEY}_${sessionId}`, effort);
    else window.localStorage.removeItem(`${THINK_EFFORT_KEY}_${sessionId}`);
  } catch {
    /* the pick lasts this visit only */
  }
}

/* ── Sampling panel (composer chip -> real controls, not just /temp etc.) ──
 * Each control's effective value is either the global default
 * (`local_*_default` settings, SET-07) or an explicit per-conversation
 * override already held in `GenOverrides`. `max_tokens` and `think` have no
 * global-default setting (SET-07 only covers temperature/top_p/top_k), so
 * their "default" is simply "unset — the model/provider decides". */

export interface SamplingDefaults {
  temperature?: number;
  top_p?: number;
  top_k?: number;
}

export type GenFieldKey = 'temperature' | 'max_tokens' | 'top_p' | 'top_k' | 'think';

/** The value a control should show — the explicit override when this chat
 *  has one, else the global default (or undefined for max_tokens/think). */
export function genEffectiveValue(
  key: GenFieldKey,
  gen: GenOverrides,
  defaults: SamplingDefaults,
): number | boolean | undefined {
  if (gen[key] !== undefined) return gen[key];
  if (key === 'temperature' || key === 'top_p' || key === 'top_k') return defaults[key];
  return undefined;
}

/** Whether a control is showing this chat's own override or the global
 *  default — drives the "default" vs "override" label and whether its
 *  per-control reset is enabled. */
export function genFieldSource(key: GenFieldKey, gen: GenOverrides): 'override' | 'default' {
  return gen[key] !== undefined ? 'override' : 'default';
}

/** Setting one control turns it into an explicit override for this
 *  conversation; the rest of `gen` is untouched. */
export function genWithOverride(gen: GenOverrides, key: GenFieldKey, value: number | boolean): GenOverrides {
  return { ...gen, [key]: value };
}

/** A control's own reset: back to the global default (or "unset"), leaving
 *  every other override in this chat exactly as it was. The chip's X still
 *  clears all of them at once (`onClearGen`, unchanged). */
export function genWithoutOverride(gen: GenOverrides, key: GenFieldKey): GenOverrides {
  const next = { ...gen };
  delete next[key];
  return next;
}
