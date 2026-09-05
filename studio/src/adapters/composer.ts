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
}

export function attachmentUrl(id: string): string {
  return `/api/upload/${encodeURIComponent(id)}`;
}

export function isImage(mime: string): boolean {
  return mime.startsWith('image/');
}

export async function uploadFiles(files: File[], sessionId?: string | null): Promise<Attachment[]> {
  const fd = new FormData();
  for (const file of files) fd.append('files', file, file.name);
  if (sessionId) fd.append('session_id', sessionId);
  const response = await fetch('/api/upload', {
    method: 'POST',
    body: fd,
    credentials: 'same-origin',
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
  const raw = (await response.json()) as { files?: unknown };
  return asArray<Record<string, unknown>>(raw.files).map((f) => ({
    id: String(f.id),
    name: String(f.name ?? 'archivo'),
    mime: String(f.mime ?? 'application/octet-stream'),
    size: typeof f.size === 'number' ? f.size : 0,
    width: typeof f.width === 'number' ? f.width : undefined,
    height: typeof f.height === 'number' ? f.height : undefined,
  }));
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
