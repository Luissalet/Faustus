/**
 * BENCH-06 — the sandbox/CSP policy behind `Preview.tsx`'s iframe, as pure
 * functions so they can be reasoned about (and tested with plain node,
 * `studio/checks/p1-bench06-preview.check.mjs`) without rendering React.
 *
 * QA-32 already proves the *rest* of the app's HTML preview surfaces
 * (documents/Editor.tsx, compare/Compare.tsx, email/Reader.tsx) render
 * inside a sandboxed iframe that never combines `allow-same-origin` with
 * `allow-scripts` (tests/qa/test_qa_32_preview_malicioso.py). This module is
 * that same rule made real for the workbench's artifact/canvas preview,
 * which had no sandboxed surface at all before this lot: `allow-same-origin`
 * is never granted here, at all — a script running inside, even one running
 * because the caller explicitly opted into `allowScripts`, still cannot
 * reach the app's own origin, cookies or localStorage.
 */

/** Sandbox tokens for the preview iframe. Popups are allowed (a prototype
 *  opening `target=_blank` should not silently fail) but escape the sandbox
 *  themselves when they do, so a popup is a normal, separately-sandboxed
 *  browsing context rather than inheriting this one's privileges. Scripts
 *  are opt-in only, and `allow-same-origin` is never included — the one
 *  combination QA-32 forbids, and the one that would let injected content
 *  reach Faustus's own origin. */
export function sandboxAttr(allowScripts: boolean): string {
  const tokens = ['allow-popups', 'allow-popups-to-escape-sandbox'];
  if (allowScripts) tokens.push('allow-scripts');
  return tokens.join(' ');
}

/** The CSP the preview document itself declares, on top of the sandbox.
 *  Belt and braces: the sandbox already blocks navigation/top-level framing
 *  the browser enforces regardless of CSP, but `connect-src`/`frame-src`
 *  are the preview's *own* declared network capability — explicit per
 *  BENCH-06 ("capacidades explícitas de red/ejecución") — and default to
 *  none so a preview cannot phone out even if `allowScripts` is later
 *  extended to also permit `connect-src` on a specific opt-in. */
export function previewCsp(allowScripts: boolean): string {
  return [
    "default-src 'none'",
    "img-src data: blob:",
    "style-src 'unsafe-inline'",
    "font-src data:",
    allowScripts ? "script-src 'unsafe-inline'" : "script-src 'none'",
    "connect-src 'none'",
    "frame-src 'none'",
    "form-action 'none'",
  ].join('; ');
}

/** Wraps an artifact's raw HTML/SVG body into the full document the iframe
 *  gets as `srcDoc` — the CSP meta tag is the first thing in `<head>` so it
 *  governs every subsequent tag the artifact's own markup adds. */
export function wrapPreviewHtml(body: string, allowScripts: boolean): string {
  return `<!doctype html><html><head><meta charset="utf-8">`
    + `<meta http-equiv="Content-Security-Policy" content="${previewCsp(allowScripts)}">`
    + `<meta name="referrer" content="no-referrer"></head><body>${body}</body></html>`;
}
