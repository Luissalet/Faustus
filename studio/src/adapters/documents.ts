import { ApiError, asArray, getJson } from './api';
import { t } from '../i18n';

/**
 * Living documents (routes/document): the agent creates and edits them, the
 * person reads, edits, versions and exports them. The side panel's editor
 * uses exactly the endpoints the legacy editor uses.
 */

export interface Doc {
  id: string;
  sessionId: string | null;
  title: string;
  language: string;
  content: string;
  versionCount: number;
  archived: boolean;
  updatedAt: string | null;
  createdAt: string | null;
  /** Set when the document came from an email attachment: a signed reply can go back. */
  sourceEmail: { uid: string; folder: string; accountId: string | null } | null;
}

export interface DocVersion {
  id: string;
  number: number;
  content: string;
  summary: string;
  source: string;
  createdAt: string | null;
}

function docFrom(raw: Record<string, unknown>): Doc {
  return {
    id: String(raw.id ?? ''),
    sessionId: typeof raw.session_id === 'string' ? raw.session_id : null,
    title: String(raw.title ?? t('Untitled')),
    language: String(raw.language ?? ''),
    content: String(raw.current_content ?? raw.content ?? ''),
    versionCount: typeof raw.version_count === 'number' ? raw.version_count : 1,
    archived: Boolean(raw.archived),
    updatedAt: typeof raw.updated_at === 'string' ? raw.updated_at : null,
    createdAt: typeof raw.created_at === 'string' ? raw.created_at : null,
    sourceEmail: typeof raw.source_email_uid === 'string' && raw.source_email_uid && typeof raw.source_email_folder === 'string' ? { uid: raw.source_email_uid, folder: raw.source_email_folder, accountId: typeof raw.source_email_account_id === 'string' ? raw.source_email_account_id : null } : null,
  };
}

async function send(path: string, method: string, body?: unknown): Promise<Record<string, unknown>> {
  const response = await fetch(path, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    let detail = '';
    try {
      detail = String(((await response.json()) as { detail?: unknown }).detail ?? '');
    } catch {
      detail = '';
    }
    throw new ApiError(detail || `${path} responded ${response.status}`, response.status);
  }
  try {
    return (await response.json()) as Record<string, unknown>;
  } catch {
    return {};
  }
}

const enc = encodeURIComponent;

export async function getDoc(id: string, signal?: AbortSignal): Promise<Doc> {
  return docFrom(await getJson<Record<string, unknown>>(`/api/document/${enc(id)}`, signal));
}

export async function createDoc(input: { title: string; language?: string; content?: string; sessionId?: string | null }): Promise<Doc> {
  return docFrom(
    await send('/api/document', 'POST', {
      title: input.title,
      language: input.language ?? null,
      content: input.content ?? '',
      session_id: input.sessionId ?? null,
    }),
  );
}

/** Saves the content; the server coalesces quick successive saves into one
 *  version unless `forceVersion`. */
export async function saveDoc(id: string, content: string, summary?: string, forceVersion = false): Promise<Doc> {
  return docFrom(await send(`/api/document/${enc(id)}`, 'PUT', { content, summary: summary ?? null, force_version: forceVersion }));
}

export async function renameDoc(id: string, title: string, language?: string): Promise<Doc> {
  return docFrom(await send(`/api/document/${enc(id)}`, 'PATCH', { title, language: language ?? null }));
}

export async function archiveDoc(id: string): Promise<void> {
  await send(`/api/document/${enc(id)}/archive`, 'POST');
}

export async function deleteDoc(id: string): Promise<void> {
  await send(`/api/document/${enc(id)}`, 'DELETE');
}

export async function listDocVersions(id: string): Promise<DocVersion[]> {
  const raw = await getJson<unknown>(`/api/document/${enc(id)}/versions`);
  return asArray<Record<string, unknown>>(raw).map((v) => ({
    id: String(v.id ?? ''),
    number: typeof v.version_number === 'number' ? v.version_number : 0,
    content: String(v.content ?? ''),
    summary: String(v.summary ?? ''),
    source: String(v.source ?? ''),
    createdAt: typeof v.created_at === 'string' ? v.created_at : null,
  }));
}

export async function restoreDocVersion(id: string, number: number): Promise<Doc> {
  return docFrom(await send(`/api/document/${enc(id)}/restore/${number}`, 'POST'));
}

export function docPdfUrl(id: string): string {
  return `/api/document/${enc(id)}/export-pdf`;
}

/* ── The documents library (lot AA) ── */

export interface LibraryDoc {
  id: string;
  title: string;
  language: string;
  preview: string;
  sessionId: string | null;
  sessionName: string | null;
  versionCount: number;
  archived: boolean;
  updatedAt: string | null;
  createdAt: string | null;
}

export interface LibraryPage {
  documents: LibraryDoc[];
  total: number;
  languages: Record<string, number>;
  sessionCount: number;
}

export type LibrarySort = 'recent' | 'oldest' | 'alpha' | 'most-versions';

function libraryDocFrom(raw: Record<string, unknown>): LibraryDoc {
  return {
    id: String(raw.id ?? ''),
    title: String(raw.title ?? '') || t('Untitled'),
    language: String(raw.language ?? '') || 'text',
    preview: String(raw.preview ?? '').replace(/<!--\s*pdf(?:_form)?_source[^>]*-->\s*/g, '').trim(),
    sessionId: typeof raw.session_id === 'string' ? raw.session_id : null,
    sessionName: typeof raw.session_name === 'string' ? raw.session_name : null,
    versionCount: typeof raw.version_count === 'number' ? raw.version_count : 1,
    archived: Boolean(raw.archived),
    updatedAt: typeof raw.updated_at === 'string' ? raw.updated_at : null,
    createdAt: typeof raw.created_at === 'string' ? raw.created_at : null,
  };
}

export async function loadDocLibrary(query: { search?: string; language?: string; sort?: LibrarySort; offset?: number; limit?: number; archived?: boolean } = {}, signal?: AbortSignal): Promise<LibraryPage> {
  const q = new URLSearchParams();
  if (query.search) q.set('search', query.search);
  if (query.language) q.set('language', query.language);
  q.set('sort', query.sort ?? 'recent');
  q.set('offset', String(query.offset ?? 0));
  q.set('limit', String(query.limit ?? 50));
  if (query.archived) q.set('archived', 'true');
  const raw = await getJson<Record<string, unknown>>(`/api/documents/library?${q}`, signal);
  const languages: Record<string, number> = {};
  const rawLang = raw.languages;
  if (rawLang && typeof rawLang === 'object') for (const [k, v] of Object.entries(rawLang as Record<string, unknown>)) languages[k] = Number(v) || 0;
  return {
    documents: asArray<Record<string, unknown>>(raw, 'documents').map(libraryDocFrom),
    total: typeof raw.total === 'number' ? raw.total : 0,
    languages,
    sessionCount: typeof raw.session_count === 'number' ? raw.session_count : 0,
  };
}

export async function setDocArchived(id: string, archived: boolean): Promise<void> {
  await send(`/api/document/${enc(id)}/archive?archived=${archived ? 'true' : 'false'}`, 'POST');
}

/** A copy of a document, in the library (no session) or inside a chat. */
export async function duplicateDoc(id: string, sessionId?: string | null, title?: string): Promise<Doc> {
  const src = await getDoc(id);
  return createDoc({ title: title ?? src.title, language: src.language || 'markdown', content: src.content, sessionId: sessionId ?? null });
}

export const EXT_BY_LANGUAGE: Record<string, string> = {
  javascript: '.js',
  python: '.py',
  html: '.html',
  css: '.css',
  markdown: '.md',
  json: '.json',
  yaml: '.yml',
  bash: '.sh',
  sql: '.sql',
  rust: '.rs',
  go: '.go',
  java: '.java',
  c: '.c',
  cpp: '.cpp',
  typescript: '.ts',
  ruby: '.rb',
  php: '.php',
  text: '.txt',
  xml: '.xml',
  toml: '.toml',
  ini: '.ini',
  csv: '.csv',
};

export function docFilename(doc: { title: string; language: string }): string {
  const ext = EXT_BY_LANGUAGE[doc.language] ?? '.txt';
  return (doc.title || 'document') + (doc.title && doc.title.includes('.') ? '' : ext);
}

export async function exportDocsZip(ids: string[]): Promise<Blob> {
  const response = await fetch('/api/documents/export-zip', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids }) });
  if (!response.ok) throw new ApiError(t('The zip could not be built'), response.status);
  return response.blob();
}

export interface TidyResult {
  deleted: number;
  fixedTitles: number;
  message: string;
}

/** Fix empty titles and drop broken documents; then let a model judge the junk. */
export async function tidyDocuments(withAi: boolean): Promise<TidyResult> {
  const first = await send('/api/documents/tidy', 'POST');
  const out: TidyResult = { deleted: Number(first.deleted) || 0, fixedTitles: Number(first.fixed_titles) || 0, message: '' };
  if (withAi) {
    try {
      const second = await send('/api/documents/ai-tidy', 'POST');
      out.deleted += Number(second.deleted) || 0;
      if (typeof second.message === 'string') out.message = second.message;
    } catch {
      /* the AI pass is optional */
    }
  }
  return out;
}

/* ── Importing files ── */

const EXT_TO_LANG: Record<string, string> = {
  '.py': 'python',
  '.js': 'javascript',
  '.jsx': 'javascript',
  '.ts': 'typescript',
  '.tsx': 'typescript',
  '.html': 'html',
  '.htm': 'html',
  '.vue': 'html',
  '.svelte': 'html',
  '.css': 'css',
  '.scss': 'css',
  '.sass': 'css',
  '.less': 'css',
  '.md': 'markdown',
  '.json': 'json',
  '.yml': 'yaml',
  '.yaml': 'yaml',
  '.sh': 'bash',
  '.bash': 'bash',
  '.sql': 'sql',
  '.rs': 'rust',
  '.go': 'go',
  '.java': 'java',
  '.c': 'c',
  '.h': 'c',
  '.cpp': 'cpp',
  '.hpp': 'cpp',
  '.rb': 'ruby',
  '.php': 'php',
  '.xml': 'xml',
  '.toml': 'toml',
  '.ini': 'ini',
  '.cfg': 'ini',
  '.conf': 'ini',
  '.csv': 'csv',
  '.tsv': 'csv',
  '.txt': '',
  '.log': '',
  '.env': '',
};

declare global {
  interface Window {
    XLSX?: { read: (data: ArrayBuffer, opts: { type: string }) => { SheetNames: string[]; Sheets: Record<string, unknown> }; utils: { sheet_to_csv: (sheet: unknown) => string } };
    mammoth?: { convertToHtml: (input: { arrayBuffer: ArrayBuffer }) => Promise<{ value: string }> };
  }
}

const scriptLoads = new Map<string, Promise<void>>();

/** The spreadsheet and Word parsers are vendored under /static/lib and only fetched when a file needs them. */
function loadScript(src: string): Promise<void> {
  let p = scriptLoads.get(src);
  if (!p) {
    p = new Promise<void>((resolve, reject) => {
      const s = document.createElement('script');
      s.src = src;
      s.onload = () => resolve();
      s.onerror = () => reject(new Error(t('Could not load {what}', { what: src })));
      document.head.appendChild(s);
    });
    scriptLoads.set(src, p);
  }
  return p;
}

function htmlToMarkdown(html: string): string {
  const div = document.createElement('div');
  div.innerHTML = html;
  const out: string[] = [];
  const walk = (node: Node): string => {
    if (node.nodeType === Node.TEXT_NODE) return node.textContent ?? '';
    if (node.nodeType !== Node.ELEMENT_NODE) return '';
    const el = node as HTMLElement;
    const inner = () => Array.from(el.childNodes).map(walk).join('');
    switch (el.tagName) {
      case 'H1':
        return `\n# ${inner()}\n\n`;
      case 'H2':
        return `\n## ${inner()}\n\n`;
      case 'H3':
        return `\n### ${inner()}\n\n`;
      case 'H4':
      case 'H5':
      case 'H6':
        return `\n#### ${inner()}\n\n`;
      case 'P':
        return `${inner()}\n\n`;
      case 'BR':
        return '\n';
      case 'STRONG':
      case 'B':
        return `**${inner()}**`;
      case 'EM':
      case 'I':
        return `*${inner()}*`;
      case 'A':
        return `[${inner()}](${el.getAttribute('href') ?? ''})`;
      case 'LI':
        return `- ${inner()}\n`;
      case 'UL':
      case 'OL':
        return `${inner()}\n`;
      case 'TABLE': {
        const rows = Array.from(el.querySelectorAll('tr')).map((tr) => Array.from(tr.children).map((td) => (td.textContent ?? '').trim()));
        if (!rows.length) return '';
        const head = rows[0];
        return `\n| ${head.join(' | ')} |\n| ${head.map(() => '---').join(' | ')} |\n${rows
          .slice(1)
          .map((r) => `| ${r.join(' | ')} |`)
          .join('\n')}\n\n`;
      }
      default:
        return inner();
    }
  };
  out.push(walk(div));
  return out.join('').replace(/\n{3,}/g, '\n\n').trim();
}

export interface ImportOutcome {
  imported: number;
  failed: string[];
}

/**
 * Bring files into the library: PDFs go to the server (they keep their
 * pages for annotation), spreadsheets become one CSV document per sheet,
 * Word files become Markdown, everything else is read as text.
 */
export async function importDocumentFiles(files: File[], onEach?: (done: number, total: number) => void): Promise<ImportOutcome> {
  const out: ImportOutcome = { imported: 0, failed: [] };
  let done = 0;
  for (const file of files) {
    const name = file.name;
    const dot = name.lastIndexOf('.');
    const ext = dot >= 0 ? name.slice(dot).toLowerCase() : '';
    const baseTitle = dot > 0 ? name.slice(0, dot) : name;
    try {
      if (ext === '.pdf') {
        const fd = new FormData();
        fd.append('file', file);
        const res = await fetch('/api/documents/import-pdf', { method: 'POST', credentials: 'same-origin', body: fd });
        if (!res.ok) {
          let detail = `HTTP ${res.status}`;
          try {
            const j = (await res.json()) as { detail?: string; error?: string };
            detail = j.detail || j.error || detail;
          } catch {
            /* keep */
          }
          throw new Error(detail);
        }
      } else if (ext === '.xlsx' || ext === '.xls' || ext === '.ods') {
        await loadScript('/static/lib/xlsx.full.min.js');
        const wb = window.XLSX!.read(await file.arrayBuffer(), { type: 'array' });
        for (const sheetName of wb.SheetNames) {
          const csv = window.XLSX!.utils.sheet_to_csv(wb.Sheets[sheetName]);
          if (!csv.trim()) continue;
          await createDoc({ title: wb.SheetNames.length > 1 ? `${baseTitle} - ${sheetName}` : baseTitle, language: 'csv', content: csv });
        }
      } else if (ext === '.docx') {
        await loadScript('/static/lib/mammoth.browser.min.js');
        const result = await window.mammoth!.convertToHtml({ arrayBuffer: await file.arrayBuffer() });
        await createDoc({ title: baseTitle, language: 'markdown', content: htmlToMarkdown(result.value) });
      } else {
        const content = await file.text();
        const language = EXT_TO_LANG[ext];
        await createDoc({ title: baseTitle, language: language === undefined ? undefined : language || undefined, content });
      }
      out.imported++;
    } catch (e) {
      out.failed.push(`${name}: ${(e as Error).message}`);
    }
    done++;
    onEach?.(done, files.length);
  }
  return out;
}

/* ── PDF-backed documents (lot AB) ── */

export interface PdfField {
  name: string;
  type: string;
  label: string;
  options: string[];
  value: string;
  /** Pixel rectangle at the render scale: x0, y0, x1, y1. */
  rect: [number, number, number, number];
}

export interface PdfPage {
  page: number;
  width: number;
  height: number;
  fields: PdfField[];
}

export async function renderPdfPages(id: string): Promise<{ scale: number; pages: PdfPage[] }> {
  const res = await fetch(`/api/document/${enc(id)}/render-pages`, { credentials: 'same-origin', headers: { Accept: 'application/json' } });
  if (!res.ok) {
    let detail = '';
    try {
      detail = String(((await res.json()) as { detail?: unknown }).detail ?? '');
    } catch {
      /* not json */
    }
    throw new ApiError(detail || `render-pages responded ${res.status}`, res.status);
  }
  const raw = (await res.json()) as Record<string, unknown>;
  return {
    scale: Number(raw.scale) || 2,
    pages: asArray<Record<string, unknown>>(raw, 'pages').map((p) => ({
      page: Number(p.page) || 1,
      width: Number(p.width) || 0,
      height: Number(p.height) || 0,
      fields: asArray<Record<string, unknown>>(p, 'fields').map((f) => ({
        name: String(f.name ?? ''),
        type: String(f.type ?? 'text'),
        label: String(f.label ?? ''),
        options: asArray<unknown>(f.options).map(String),
        value: typeof f.value === 'boolean' ? (f.value ? 'true' : '') : String(f.value ?? ''),
        rect: (Array.isArray(f.rect_px) ? f.rect_px.map(Number) : [0, 0, 0, 0]) as [number, number, number, number],
      })),
    })),
  };
}

export const pdfPageUrl = (id: string, page: number) => `/api/document/${enc(id)}/page/${page}.png`;

export async function aiFillAnnotations(id: string, instruction: string): Promise<{ page: number; x: number; y: number; w: number; h: number; value: string }[]> {
  const raw = await send(`/api/document/${enc(id)}/ai-fill-annotations`, 'POST', { instruction });
  return asArray<Record<string, unknown>>(raw, 'annotations').map((a) => ({ page: Number(a.page) || 1, x: Number(a.x) || 0, y: Number(a.y) || 0, w: Number(a.w) || 10, h: Number(a.h) || 3, value: String(a.value ?? '') }));
}

export async function extractPdfText(id: string): Promise<Doc> {
  return docFrom(await send(`/api/document/${enc(id)}/extract-pdf-text`, 'POST'));
}

export interface SignedReply {
  attachment: { token: string; filename: string; size: number };
  reply: { to: string; toName: string; subject: string; inReplyTo: string; references: string; accountId: string | null; sourceUid: string; sourceFolder: string };
}

/** Bake fields, signatures and annotations into a PDF and get the reply headers for it. */
export async function prepareSignedReply(id: string): Promise<SignedReply> {
  const raw = await send(`/api/document/${enc(id)}/prepare-signed-reply`, 'POST');
  const att = (raw.attachment ?? {}) as Record<string, unknown>;
  const rep = (raw.reply ?? {}) as Record<string, unknown>;
  return {
    attachment: { token: String(att.token ?? ''), filename: String(att.filename ?? 'signed.pdf'), size: Number(att.size) || 0 },
    reply: {
      to: String(rep.to ?? ''),
      toName: String(rep.to_name ?? ''),
      subject: String(rep.subject ?? ''),
      inReplyTo: String(rep.in_reply_to ?? ''),
      references: String(rep.references ?? ''),
      accountId: typeof rep.account_id === 'string' ? rep.account_id : null,
      sourceUid: String(rep.source_uid ?? ''),
      sourceFolder: String(rep.source_folder ?? ''),
    },
  };
}

/** The exported PDF as a blob (annotated, for PDF-backed documents; rendered, for the rest). */
export async function exportPdfBlob(id: string): Promise<{ blob: Blob; filename: string | null }> {
  const res = await fetch(docPdfUrl(id), { credentials: 'same-origin' });
  if (!res.ok) throw new ApiError((await res.text().catch(() => '')) || `PDF export responded ${res.status}`, res.status);
  const cd = res.headers.get('Content-Disposition') || '';
  const m = /filename\*?=(?:UTF-8'')?"?([^"';]+)/i.exec(cd);
  return { blob: await res.blob(), filename: m ? decodeURIComponent(m[1]) : null };
}

/** Run a snippet on the server (bash or python) through the same shell endpoint the legacy runner used. */
export async function runOnServer(code: string, lang: 'python' | 'bash'): Promise<{ stdout: string; stderr: string; exitCode: number }> {
  const b64 = btoa(unescape(encodeURIComponent(code)));
  const command = lang === 'python' ? `python3 -c "import base64; exec(base64.b64decode('${b64}').decode('utf-8'))"` : `python3 -c "import base64, subprocess, sys; sys.exit(subprocess.run(['bash','-c',base64.b64decode('${b64}').decode('utf-8')]).returncode)"`;
  const raw = await send('/api/shell/exec', 'POST', { command });
  return { stdout: String(raw.stdout ?? ''), stderr: String(raw.stderr ?? ''), exitCode: Number(raw.exit_code) || 0 };
}
