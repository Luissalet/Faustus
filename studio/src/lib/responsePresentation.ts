import { safeExternal } from './markdown';

/** Observed turn sources identify a citation; they do not certify its claim. */
export interface ResponseSource {
  url: string;
  title: string;
  domain?: string;
  favicon?: string;
}

export function sourceKey(raw: string): string | null {
  const href = safeExternal(raw);
  if (!href) return null;
  try {
    const url = new URL(href);
    url.hash = '';
    return url.href;
  } catch { return null; }
}

export function sourceDomain(source: ResponseSource): string {
  try { return new URL(source.url).hostname.replace(/^www\./, ''); }
  catch { return source.domain || source.title || source.url; }
}

export function findResponseSource(href: string, sources: readonly ResponseSource[]): ResponseSource | undefined {
  const key = sourceKey(href);
  return key ? sources.find(source => sourceKey(source.url) === key) : undefined;
}

export type Presentation =
  | { kind: 'details'; summary: string; body: string }
  | { kind: 'entity'; title: string; url?: string; image?: string; alt?: string; body: string };

export const PRESENTATION_LANGS = new Set(['details', 'faustus-details', 'entity', 'faustus-entity']);

/** A small native layout, with Markdown content and the reader's usual fallbacks. */
export function parsePresentation(lang: string, code: string): Presentation | null {
  if (!PRESENTATION_LANGS.has(lang.toLowerCase())) return null;
  let value: unknown;
  try { value = JSON.parse(code); } catch { return null; }
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const obj = value as Record<string, unknown>;
  if (typeof obj.body !== 'string') return null;
  const details = lang.toLowerCase().endsWith('details');
  const label = details ? obj.summary : obj.title;
  if (typeof label !== 'string' || !label.trim()) return null;
  if (details) return { kind: 'details', summary: label, body: obj.body };
  for (const key of ['url', 'image', 'alt']) {
    if (obj[key] !== undefined && typeof obj[key] !== 'string') return null;
  }
  return { kind: 'entity', title: label, body: obj.body,
    url: obj.url as string | undefined, image: obj.image as string | undefined, alt: obj.alt as string | undefined };
}
