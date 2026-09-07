import { useEffect, useState } from 'react';
import { History } from 'lucide-react';
import { Button } from '../../components';
import { getJson } from '../../adapters/api';
import { t } from '../../i18n';
import type { HarnessSummary } from '../../adapters/chat';

/** A lazy read of the immutable receipt, never a fresh inference or live diff. */
export function SavedEvidence({ evidence }: { evidence: NonNullable<HarnessSummary['changeset']> }) {
  const [open, setOpen] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [text, setText] = useState('');
  const [error, setError] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!open || !evidence.id || !evidence.stored) return;
    const controller = new AbortController();
    let active = true;
    const timer = window.setTimeout(() => controller.abort(), 15_000);
    setText(''); setError(false); setBusy(true);
    void getJson<{ ok: boolean; rendered?: string }>(
      `/api/changesets/receipts/${encodeURIComponent(evidence.id)}`, controller.signal,
    ).then((receipt) => {
      if (!receipt.ok || typeof receipt.rendered !== 'string') throw new Error('Invalid receipt');
      if (active) setText(receipt.rendered);
    }).catch(() => { if (active) setError(true); })
      .finally(() => { window.clearTimeout(timer); if (active) setBusy(false); });
    return () => { active = false; window.clearTimeout(timer); controller.abort(); };
  }, [open, attempt, evidence.id, evidence.stored]);

  if (evidence.stored === false) return (
    <p className="fs-harness__line" data-testid="evidence-not-stored">
      {evidence.storageReason === 'incognito'
        ? t('Evidence is not saved in incognito mode.')
        : t('The turn finished, but its evidence could not be saved. The summary remains in this chat.')}
    </p>
  );
  if (!evidence.stored || !evidence.id) return null;
  return (
    <div className="fs-harness__line" data-testid="saved-evidence">
      <Button icon={History} variant="ghost" size="sm" aria-expanded={open}
        label={t(open ? 'Hide saved evidence' : 'Read saved evidence')}
        onClick={() => setOpen((current) => !current)} />
      {open && (
        <div aria-busy={busy}>
          <p>{t('This is the original evidence. File diffs compare with the current workspace.')}</p>
          {busy && <p role="status">{t('Loading saved evidence…')}</p>}
          {error && <div role="alert">
            <p>{t('Could not load the evidence. Check your connection and access, then retry.')}</p>
            <Button label={t('Retry')} size="sm" onClick={() => setAttempt((value) => value + 1)} />
          </div>}
          {text && <pre className="fs-studio__out fs-harness__diff">{text}</pre>}
        </div>
      )}
    </div>
  );
}
