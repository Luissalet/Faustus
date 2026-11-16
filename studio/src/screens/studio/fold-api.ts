import { CLIENT_API_VERSION, CLIENT_VERSION_HEADER } from '../../adapters/api';
import { t } from '../../i18n';

/**
 * Folding a long answer or tool output into five levels of detail
 * (routes/text_fold_routes.py, src/text_fold.py). Levels 1 and 2 need no
 * model; 3-5 are generated on demand and cached by the server.
 */

export type FoldLevel = 1 | 2 | 3 | 4 | 5;

export interface FoldItem {
  paragraph?: string;
  section?: string;
  kind?: string;
  title?: string;
  sections?: string[];
  topic?: number;
  text: string;
}

export interface FoldView {
  id: string;
  level: FoldLevel;
  name: string;
  chars: number;
  original_chars: number;
  items: FoldItem[];
  text: string;
}

/** Below this a text is shown whole and the control stays out of the way. */
export const FOLD_MIN_CHARS = 1200;

export function isFoldable(text: string): boolean {
  return text.length >= FOLD_MIN_CHARS && text.split(/\n\s*\n/).length >= 3;
}

/** The level a plain digit key asks for, or null for any other key. */
export function foldLevelFromKey(key: string): FoldLevel | null {
  return /^[1-5]$/.test(key) ? (Number(key) as FoldLevel) : null;
}

export function foldLevelLabel(level: FoldLevel): string {
  switch (level) {
    case 1: return t('Full');
    case 2: return t('Key sentences');
    case 3: return t('Key idea');
    case 4: return t('One line per section');
    default: return t('Topics');
  }
}

export class FoldError extends Error {
  constructor(message: string, readonly errorClass: string, readonly status: number) {
    super(message);
  }
  /** The model did not answer: levels 3-5 cannot be produced right now. */
  get modelUnavailable(): boolean {
    return this.errorClass === 'fold.model_unavailable';
  }
}

async function failure(response: Response): Promise<FoldError> {
  let body: { error?: string; error_class?: string } = {};
  try { body = await response.json(); } catch { /* not JSON */ }
  return new FoldError(body.error || `/api/fold responded ${response.status}`, body.error_class || 'fold.invalid', response.status);
}

/** A cheap content key, so the same text reuses the document the server already built. */
export function textKey(text: string): string {
  let a = 0x811c9dc5;
  let b = 0x01000193;
  for (let i = 0; i < text.length; i += 1) {
    const c = text.charCodeAt(i);
    a = Math.imul(a ^ c, 0x01000193);
    b = Math.imul(b + c, 0x85ebca6b) ^ (b >>> 13);
  }
  return `${text.length}:${(a >>> 0).toString(36)}${(b >>> 0).toString(36)}`;
}

const ids = new Map<string, string>();
const levels = new Map<string, FoldLevel>();
const views = new Map<string, FoldView>();
const MEMORY_LIMIT = 200;

function remember<V>(memory: Map<string, V>, key: string, value: V): void {
  memory.delete(key);
  memory.set(key, value);
  if (memory.size > MEMORY_LIMIT) memory.delete(memory.keys().next().value as string);
}

/** The level a text was last read at: a transcript row that is torn down and
 *  rebuilt (scrolling, a reload of the history) comes back where it was. */
export function rememberedLevel(text: string): FoldLevel {
  return levels.get(textKey(text)) ?? 1;
}

export function rememberLevel(text: string, level: FoldLevel): void {
  remember(levels, textKey(text), level);
}

/** A level already read for this text, so a rebuilt row paints it at once. */
export function cachedView(text: string, level: FoldLevel): FoldView | undefined {
  return views.get(`${textKey(text)}#${level}`);
}

function query(level: number, selector: { section?: string; paragraph?: string }): string {
  const q = new URLSearchParams({ level: String(level) });
  if (selector.section) q.set('section', selector.section);
  if (selector.paragraph) q.set('paragraph', selector.paragraph);
  return q.toString();
}

async function post(text: string, level: number, signal?: AbortSignal): Promise<FoldView> {
  const response = await fetch('/api/fold', {
    method: 'POST',
    signal,
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json', [CLIENT_VERSION_HEADER]: CLIENT_API_VERSION },
    body: JSON.stringify({ text, level }),
  });
  if (!response.ok) throw await failure(response);
  const data = (await response.json()) as { document: { id: string }; view: FoldView };
  remember(ids, textKey(text), data.document.id);
  remember(views, `${textKey(text)}#${level}`, data.view);
  return data.view;
}

async function get(path: string, signal?: AbortSignal): Promise<FoldView> {
  const response = await fetch(path, {
    signal,
    credentials: 'same-origin',
    headers: { Accept: 'application/json', [CLIENT_VERSION_HEADER]: CLIENT_API_VERSION },
  });
  if (!response.ok) throw await failure(response);
  return (await response.json()) as FoldView;
}

/** One level of `text`. The first call sends the text; later ones ask by id. */
export async function foldView(text: string, level: FoldLevel, signal?: AbortSignal): Promise<FoldView> {
  const id = ids.get(textKey(text));
  if (id) {
    try {
      const view = await get(`/api/fold/${encodeURIComponent(id)}?${query(level, {})}`, signal);
      remember(views, `${textKey(text)}#${level}`, view);
      return view;
    } catch (error) {
      // The server may have dropped its copy: send the text again. Anything else is a real failure.
      if (!(error instanceof FoldError) || error.status !== 404) throw error;
    }
  }
  return post(text, level, signal);
}

/** The original text of one section or paragraph of a folded text. */
export async function foldOriginal(text: string, selector: { section?: string; paragraph?: string }, signal?: AbortSignal): Promise<string> {
  let id = ids.get(textKey(text));
  if (!id) {
    await post(text, 2, signal);
    id = ids.get(textKey(text));
  }
  return (await get(`/api/fold/${encodeURIComponent(id ?? '')}?${query(1, selector)}`, signal)).text;
}
