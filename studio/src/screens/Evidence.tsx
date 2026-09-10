import { useEffect, useState } from 'react';
import { resolveEvidence, type EvidenceRef, type EvidenceResolution } from '../adapters/evidence';
import { t } from '../i18n';

/**
 * BENCH-03 — the evidence inspector: every `EvidenceRef` the system attaches
 * to a read, a citation, a fetch or an artifact (`src/contracts/tool.py`)
 * opens here, showing the exact range/hash it was captured with and whether
 * that source still says the same thing today.
 *
 * The one rule this component is built around, verbatim from the
 * requirement: a source that changed since capture is never shown as if the
 * new version were the original proof. `capturedContent` (when the caller
 * has it — the text the agent actually saw, from wherever the turn that
 * produced this evidence displayed it) and the resolver's `currentContent`
 * are always two visually distinct sections; nothing here ever substitutes
 * one for the other silently.
 */
export interface EvidenceInspectorProps {
  evidence: EvidenceRef;
  /** The text the agent actually saw, if the caller already has it (e.g.
   *  from the tool result that produced this evidence). Optional: the
   *  inspector still tells the truth about staleness without it. */
  capturedContent?: string;
  /** Needed to re-read `source_type === 'file'` evidence; omit otherwise. */
  workspace?: string;
}

function locatorLabel(evidence: EvidenceRef): string {
  const { kind, value } = evidence.locator;
  if (kind === 'lines') return t('lines {v}', { v: value });
  if (kind === 'whole') return t('whole file');
  if (kind === 'page') return t('page {v}', { v: value });
  if (kind === 'time') return t('at {v}', { v: value });
  if (kind === 'byte_range') return t('bytes {v}', { v: value });
  if (kind === 'cells') return t('cells {v}', { v: value });
  return `${kind}: ${value}`;
}

export default function EvidenceInspector({ evidence, capturedContent, workspace }: EvidenceInspectorProps) {
  const [resolution, setResolution] = useState<EvidenceResolution | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const abort = new AbortController();
    setLoading(true);
    setError(null);
    setResolution(null);
    resolveEvidence(evidence, workspace, abort.signal)
      .then((r) => { if (!abort.signal.aborted) setResolution(r); })
      .catch((e: Error) => { if (!abort.signal.aborted) setError(e.message); })
      .finally(() => { if (!abort.signal.aborted) setLoading(false); });
    return () => abort.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [evidence.evidence_id, workspace]);

  return (
    <div className="fs-evidence" data-testid="evidence-inspector">
      <dl className="fs-act__facts">
        <div className="fs-act__fact"><dt>{t('Source')}</dt><dd title={evidence.source_ref}>{evidence.source_ref}</dd></div>
        <div className="fs-act__fact"><dt>{t('Kind')}</dt><dd>{evidence.source_type}</dd></div>
        <div className="fs-act__fact"><dt>{t('Range')}</dt><dd>{locatorLabel(evidence)}</dd></div>
        <div className="fs-act__fact"><dt>{t('Captured')}</dt><dd>{evidence.captured_at}</dd></div>
        {evidence.content_sha256 && <div className="fs-act__fact"><dt>SHA-256</dt><dd>{evidence.content_sha256.slice(0, 16)}…</dd></div>}
      </dl>

      {capturedContent !== undefined && (
        <section className="fs-evidence__section" aria-label={t('What was captured')}>
          <h4>{t('What was captured')}</h4>
          <pre className="fs-panel__code">{capturedContent}</pre>
        </section>
      )}

      {loading && <p role="status">{t('Checking whether this is still current…')}</p>}
      {error && <p className="fs-notice" data-tone="danger" role="alert">{error}</p>}

      {resolution && (
        <section className="fs-evidence__section" data-testid="evidence-status">
          {resolution.stillValid === true && (
            <p className="fs-notice" data-tone="success" role="status">{t('Still matches the source today.')}</p>
          )}
          {resolution.stillValid === false && (
            <p className="fs-notice" data-tone="warning" role="alert">
              {resolution.reason || t('This no longer matches the source.')}
            </p>
          )}
          {resolution.stillValid === null && resolution.reason && (
            <p className="fs-notice" data-tone="info" role="status">{resolution.reason}</p>
          )}
          {resolution.currentAvailable && resolution.currentContent !== null && (
            <>
              <h4>{t('Source today (not the captured evidence)')}</h4>
              <pre className="fs-panel__code" data-testid="evidence-current-content">{resolution.currentContent}</pre>
            </>
          )}
        </section>
      )}
    </div>
  );
}
