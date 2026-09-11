import { Check, Copy, Download } from 'lucide-react';
import { useState } from 'react';
import { Button } from './Button';
import { t } from '../i18n';

export interface MermaidViewProps {
  code: string;
  /** Defaults to a generic name — callers with a natural id (a run id, a
   *  future id) should pass their own so the download is identifiable. */
  filename?: string;
}

/**
 * B2 (OBJ-8): renders a Mermaid `flowchart TD` string.
 *
 * Studio has no Mermaid renderer today — grepped `mermaid` across
 * `studio/src` and `package.json` before writing this, both come back
 * empty — and this lot does not add the dependency: "sin librerías nuevas"
 * (CONTRATO_OBJ8_B.md) rules out pulling one in for two call sites. So this
 * shows the diagram as its own source in a `<pre>`, with one-click copy and
 * a `.mmd` download — genuinely useful on its own (paste into any Mermaid
 * live editor, or a markdown file that already renders Mermaid) rather
 * than a placeholder waiting on a future dependency. If a renderer is ever
 * added, this is the one component that would need to change; every
 * caller already only hands it a `code: string`.
 */
export function MermaidView({ code, filename = 'diagram.mmd' }: MermaidViewProps) {
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState(false);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setCopyError(false);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopyError(true);
    }
  };

  const download = () => {
    const blob = new Blob([code], { type: 'text/vnd.mermaid' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="fs-mermaid" data-testid="mermaid-view">
      <p className="fs-mermaid__hint">
        {t('No diagram renderer is wired in yet — this is the Mermaid source. Paste it into a Mermaid live editor to see it drawn.')}
      </p>
      <div className="fs-mermaid__actions">
        <Button variant="secondary" size="sm" icon={copied ? Check : Copy} label={copied ? t('Copied') : t('Copy')} onClick={() => void copy()} testId="mermaid-copy" />
        <Button variant="ghost" size="sm" icon={Download} label={t('Download .mmd')} onClick={download} testId="mermaid-download" />
      </div>
      {copyError && <p className="fs-mermaid__hint" role="alert">{t('The browser refused the clipboard — select the code and copy it by hand.')}</p>}
      <pre className="fs-mermaid__code" data-testid="mermaid-code">{code}</pre>
    </div>
  );
}
