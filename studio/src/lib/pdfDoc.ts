/**
 * PDF-backed documents: the markdown of an imported PDF carries a source
 * marker, one bullet per form field (`<!-- field=name type=text -->`) and
 * an "Annotations" list (`<!-- annotation id=… page=… x=… -->`) that the
 * server bakes into the exported PDF. Ported from `document.js`; pure.
 */

export type AnnotationKind = 'text' | 'check' | 'signature';

export interface Annotation {
  id: string;
  page: number;
  /** Percentages of the page. */
  x: number;
  y: number;
  w: number;
  h: number;
  kind: AnnotationKind;
  lineHeight: number;
  value: string;
}

export function isPdfDoc(content: string): boolean {
  return /<!--\s*pdf(?:_form)?_source\s+upload_id="[^"]+"/.test(content || '');
}

export function stripPdfMarkers(content: string): string {
  return (content || '')
    .replace(/<!--\s*pdf(?:_form)?_source[^>]*-->\s*/g, '')
    .replace(/\n##\s+Annotations\s*\r?\n+[\s\S]*$/m, '')
    .trim();
}

const ANNOTATION_RE = () => /^[ \t]*-\s+(.*?)\s*<!--\s*annotation\s+id=([\w-]+)\s+page=(\d+)\s+x=([\d.]+)\s+y=([\d.]+)\s+w=([\d.]+)\s+h=([\d.]+)(?:\s+kind=(\w+))?(?:\s+lh=([\d.]+))?\s*-->[ \t]*$/gm;

const escapeValue = (s: string) => String(s ?? '').replace(/\\/g, '\\\\').replace(/\n/g, '\\n');
const unescapeValue = (s: string) => String(s ?? '').replace(/\\(.)/g, (m, c: string) => (c === 'n' ? '\n' : c === '\\' ? '\\' : m));

export function parseAnnotations(md: string): Annotation[] {
  const out: Annotation[] = [];
  const re = ANNOTATION_RE();
  let m: RegExpExecArray | null;
  while ((m = re.exec(md || '')) !== null) {
    out.push({
      value: m[1] === '_(empty)_' ? '' : unescapeValue(m[1]),
      id: m[2],
      page: parseInt(m[3], 10),
      x: parseFloat(m[4]),
      y: parseFloat(m[5]),
      w: parseFloat(m[6]),
      h: parseFloat(m[7]),
      kind: (m[8] as AnnotationKind) || 'text',
      lineHeight: m[9] ? parseFloat(m[9]) : 1.3,
    });
  }
  return out;
}

function annotationLine(a: Annotation): string {
  const lh = Number.isFinite(a.lineHeight) && a.lineHeight ? a.lineHeight : 1.3;
  const escaped = a.value === '' || a.value == null ? '_(empty)_' : escapeValue(a.value);
  return `- ${escaped} <!-- annotation id=${a.id} page=${a.page} x=${a.x.toFixed(2)} y=${a.y.toFixed(2)} w=${a.w.toFixed(2)} h=${a.h.toFixed(2)} kind=${a.kind || 'text'} lh=${lh.toFixed(2)} -->`;
}

export function writeAnnotations(md: string, annotations: Annotation[]): string {
  let out = (md || '').replace(ANNOTATION_RE(), '');
  out = out.replace(/\n##\s+Annotations\s*\r?\n+/g, '\n');
  out = out.replace(/\n{3,}/g, '\n\n');
  if (!annotations.length) return out;
  if (!out.endsWith('\n')) out += '\n';
  out += '\n## Annotations\n\n';
  for (const a of annotations) out += annotationLine(a) + '\n';
  return out;
}

export function newAnnotationId(): string {
  return 'ann-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 7);
}

/** Percent-encode a field name the way the server does (A-Z a-z 0-9 _ . - stay). */
export function encodeFieldName(name: string): string {
  return String(name ?? '').replace(/[^A-Za-z0-9_.-]/g, (c) => '%' + c.charCodeAt(0).toString(16).toUpperCase().padStart(2, '0'));
}

export type FieldType = 'text' | 'checkbox' | 'choice' | 'signature' | string;

export interface FieldValue {
  name: string;
  type: FieldType;
  value: string | boolean;
  signatureId?: string;
}

const escapeRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/** Write the pane's field values back into their bullets. */
export function writeFieldValues(md: string, fields: FieldValue[]): string {
  let out = md;
  for (const f of fields) {
    const enc = encodeFieldName(f.name);
    const re = new RegExp(`^(\\s*-\\s+)(.*?)(\\s*<!--\\s*field=${escapeRe(enc)}\\s+type=\\w+\\s*-->\\s*)$`, 'm');
    const m = out.match(re);
    if (!m) continue;
    const body = m[2];
    let next = body;
    if (f.type === 'checkbox') next = body.replace(/^\s*\[[ xX]\]/, f.value ? '[x]' : '[ ]');
    else if (f.type === 'choice') next = body.replace(/(\][\s]*:[ ]*).*$/, `$1${(f.value as string) || '_(not selected)_'}`);
    else if (f.type === 'signature') next = body.replace(/(:\*\*[ ]*).*$/, `$1${f.signatureId ? `signature:${f.signatureId}` : '_(unsigned)_'}`);
    else next = body.replace(/(:\*\*[ ]*).*$/, `$1${(f.value as string) === '' ? '_(empty)_' : (f.value as string)}`);
    if (next !== body) out = out.replace(re, `${m[1]}${next}${m[3]}`);
  }
  return out;
}

export function isSignatureField(f: { type: string; name?: string; label?: string }): boolean {
  return f.type === 'signature' || /sign(?:ed|ature)/i.test(`${f.name ?? ''} ${f.label ?? ''}`);
}
