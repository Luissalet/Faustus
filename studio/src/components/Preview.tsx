import { useMemo } from 'react';
import { t } from '../i18n';
import { sandboxAttr, wrapPreviewHtml } from './previewSandbox';

/**
 * BENCH-06 — a sandboxed live preview for an HTML/SVG artifact or a small
 * prototype: an `<iframe sandbox>` that never grants the preview its own
 * origin (see `previewSandbox.ts`'s sandbox token list), served its own CSP
 * that blocks every network capability and every script unless
 * `allowScripts` opts in explicitly.
 *
 * QA-32 (tests/qa/test_qa_32_preview_malicioso.py) already holds this same
 * bar for documents/Editor.tsx, compare/Compare.tsx and email/Reader.tsx by
 * reading their source; `tests/test_p1_bench06_preview.py` extends that same
 * check to this file, and `studio/checks/p1-bench06-preview.check.mjs`
 * exercises the pure policy functions this component is built on.
 *
 * A broken or hostile preview fails inside its own iframe only — it never
 * reaches the chat, the app's origin, or (per SEC-07 / QA-32) Electron's
 * privileged process.
 */
export interface PreviewProps {
  /** Raw HTML/SVG body markup (no `<html>`/`<head>` wrapper needed). */
  html: string;
  /** Opt-in only: without it, `script-src 'none'` blocks every script,
   *  inline or not, regardless of what the artifact's own markup asks for. */
  allowScripts?: boolean;
  title?: string;
}

export default function Preview({ html, allowScripts = false, title }: PreviewProps) {
  const srcDoc = useMemo(() => wrapPreviewHtml(html, allowScripts), [html, allowScripts]);
  return (
    <iframe
      className="fs-preview__frame"
      data-testid="artifact-preview"
      sandbox={sandboxAttr(allowScripts)}
      srcDoc={srcDoc}
      title={title || t('Preview')}
      referrerPolicy="no-referrer"
      loading="lazy"
    />
  );
}
