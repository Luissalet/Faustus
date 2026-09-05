import { renderToStaticMarkup } from 'react-dom/server';
import { createElement } from 'react';
import { Rich } from '../rich';
import { t } from '../../i18n';

declare global {
  interface Window {
    docx?: {
      Document: new (opts: unknown) => unknown;
      Packer: { toBlob: (doc: unknown) => Promise<Blob> };
      Paragraph: new (opts: unknown) => unknown;
      TextRun: new (opts: unknown) => unknown;
      HeadingLevel: Record<string, string>;
    };
  }
}

let docxLoad: Promise<void> | null = null;

function loadDocx(): Promise<void> {
  if (window.docx) return Promise.resolve();
  if (!docxLoad) {
    docxLoad = new Promise<void>((resolve, reject) => {
      const s = document.createElement('script');
      s.src = '/static/lib/docx.umd.min.js';
      s.onload = () => resolve();
      s.onerror = () => reject(new Error(t('Could not load the DOCX library')));
      document.head.appendChild(s);
    });
  }
  return docxLoad;
}

export function download(blob: Blob, name: string): void {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  window.setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}

export function baseName(title: string): string {
  return (title || 'document').replace(/\.[^.]+$/, '').replace(/[^\w\s.-]+/g, '').trim().replace(/\s+/g, '_') || 'document';
}

/** The document rendered the way the Studio reader renders it, as a standalone page. */
export function toHtml(title: string, text: string): string {
  const body = renderToStaticMarkup(createElement(Rich, { text }));
  return `<!doctype html>\n<html><head><meta charset="utf-8"><title>${escapeHtml(title)}</title><style>body{font:16px/1.6 system-ui,sans-serif;max-width:72ch;margin:3rem auto;padding:0 1.5rem;color:#1a1a1a}pre{background:#f4f4f4;padding:1rem;overflow:auto;border-radius:6px}code{font-family:ui-monospace,monospace}table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:.3rem .6rem}img{max-width:100%}</style></head><body>${body}</body></html>`;
}

function escapeHtml(s: string): string {
  return s.replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c] ?? c);
}

/** Headings, bold and italics survive; the rest goes in as plain paragraphs. */
export async function toDocx(text: string): Promise<Blob> {
  await loadDocx();
  const lib = window.docx;
  if (!lib) throw new Error(t('Could not load the DOCX library'));
  const { Document, Packer, Paragraph, TextRun, HeadingLevel } = lib;
  const paragraphs = text.split('\n').map((line) => {
    const h1 = /^# (.+)/.exec(line), h2 = /^## (.+)/.exec(line), h3 = /^### (.+)/.exec(line);
    if (h1) return new Paragraph({ text: h1[1], heading: HeadingLevel.HEADING_1 });
    if (h2) return new Paragraph({ text: h2[1], heading: HeadingLevel.HEADING_2 });
    if (h3) return new Paragraph({ text: h3[1], heading: HeadingLevel.HEADING_3 });
    const bullet = /^[-*]\s+/.test(line);
    const runs = (bullet ? line.replace(/^[-*]\s+/, '') : line).split(/(\*\*[^*]+\*\*|\*[^*]+\*)/).map((part) => {
      if (part.startsWith('**') && part.endsWith('**')) return new TextRun({ text: part.slice(2, -2), bold: true });
      if (part.startsWith('*') && part.endsWith('*')) return new TextRun({ text: part.slice(1, -1), italics: true });
      return new TextRun({ text: part });
    });
    return new Paragraph({ children: runs, bullet: bullet ? { level: 0 } : undefined });
  });
  return Packer.toBlob(new Document({ sections: [{ children: paragraphs }] }));
}
