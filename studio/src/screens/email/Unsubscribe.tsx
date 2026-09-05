import { ExternalLink, MailX, ShieldOff, Trash2 } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Button, Dialog, EmptyState, Skeleton } from '../../components';
import { cleanupUnsubscribed, executeUnsubscribe, folderLabel, scanUnsubscribe, type UnsubscribeCandidate } from '../../adapters/email';
import { displayName } from '../../lib/mail';
import { t } from '../../i18n';
import { Avatar } from './parts';

/**
 * Review of newsletters and ads in the folder: who they are, why they
 * look like that, and one button each to unsubscribe (mailto, done by the
 * server) or to open the web link. Afterwards, one cleanup for the ones
 * you dealt with.
 */
export function UnsubscribeDialog({ folder, accountId, onClose, say, onChanged }: { folder: string; accountId: string | null; onClose: () => void; say: (msg: string, tone?: 'ok' | 'warn') => void; onChanged: () => void }) {
  const [rows, setRows] = useState<UnsubscribeCandidate[] | null>(null);
  const [scanned, setScanned] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [done, setDone] = useState<Set<string>>(new Set());
  const [moveToSpam, setMoveToSpam] = useState(false);

  useEffect(() => {
    const c = new AbortController();
    scanUnsubscribe(folder, accountId, c.signal)
      .then((r) => {
        setRows(r.candidates);
        setScanned(r.scanned);
      })
      .catch((err: unknown) => {
        if ((err as { name?: string })?.name !== 'AbortError') {
          setError((err as Error).message || t('Could not scan the folder.'));
          setRows([]);
        }
      });
    return () => c.abort();
  }, [folder, accountId]);

  const unsubscribe = async (c: UnsubscribeCandidate) => {
    setBusy(c.uid);
    try {
      await executeUnsubscribe(c, accountId, moveToSpam);
      setDone((d) => new Set(d).add(c.uid));
      say(t('Unsubscribed from {who}.', { who: displayName(c.fromName, c.fromAddress) }));
      if (moveToSpam) onChanged();
    } catch (err) {
      say((err as Error).message || t('Could not unsubscribe.'), 'warn');
    } finally {
      setBusy(null);
    }
  };

  const openLink = (c: UnsubscribeCandidate) => {
    const url = c.methods.find((m) => m.kind === 'url')?.target;
    if (!url) return;
    window.open(url, '_blank', 'noopener');
    setDone((d) => new Set(d).add(c.uid));
  };

  const cleanup = async (action: 'junk' | 'delete') => {
    const uids = [...done];
    if (!uids.length) return;
    setBusy('cleanup');
    try {
      const r = await cleanupUnsubscribed(uids, folder, accountId, action);
      say(action === 'junk' ? t('{n} mails moved to spam.', { n: r.changed }) : t('{n} mails deleted.', { n: r.changed }));
      onChanged();
      onClose();
    } catch (err) {
      say((err as Error).message || t('Could not clean up.'), 'warn');
    } finally {
      setBusy(null);
    }
  };

  return (
    <Dialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={t('Unsubscribe review')}
      description={rows ? t('{n} candidates among {m} mails in {folder}.', { n: rows.length, m: scanned, folder: folderLabel(folder) }) : t('Scanning {folder}…', { folder: folderLabel(folder) })}
      testId="mail-unsubscribe"
      footer={
        done.size > 0 ? (
          <>
            <span className="fs-muted">{t('{n} dealt with', { n: done.size })}</span>
            <span className="fs-spacer" />
            <Button variant="secondary" size="sm" icon={ShieldOff} label={t('Move them to spam')} loading={busy === 'cleanup'} onClick={() => void cleanup('junk')} />
            <Button variant="danger" size="sm" icon={Trash2} label={t('Delete them')} disabled={busy === 'cleanup'} onClick={() => void cleanup('delete')} />
          </>
        ) : (
          <Button variant="ghost" size="sm" label={t('Close')} onClick={onClose} />
        )
      }
    >
      {!rows && <Skeleton label={t('Scanning')} count={4} height="52px" />}
      {error && <p className="fs-mail__error">{error}</p>}
      {rows && rows.length === 0 && !error && <EmptyState icon={MailX} title={t('Nothing to unsubscribe from')} body={t('No newsletters or ads with an unsubscribe option in this folder.')} />}
      {rows && rows.length > 0 && (
        <>
          <label className="fs-switch fs-mail__unsub-opt">
            <input type="checkbox" checked={moveToSpam} onChange={(e) => setMoveToSpam(e.target.checked)} />
            <span>{t('Also move the mail to spam when unsubscribing')}</span>
          </label>
          <ul className="fs-mail__unsub-list">
            {rows.map((c) => (
              <li key={`${c.folder}-${c.uid}`} className="fs-mail__unsub" data-done={done.has(c.uid) || undefined}>
                <Avatar name={c.fromName} email={c.fromAddress} size="sm" />
                <div className="fs-mail__unsub-text">
                  <b>{displayName(c.fromName, c.fromAddress)}</b>
                  {c.duplicateCount > 1 && <span className="fs-mail__unsub-n">×{c.duplicateCount}</span>}
                  <span className="fs-mail__unsub-subject">{c.subject}</span>
                  <span className="fs-mail__unsub-why">{c.reasons.join(' · ')}</span>
                </div>
                <span className="fs-mail__score" title={t('Score')} data-hot={c.score >= 75 || undefined}>
                  {c.score}
                </span>
                {done.has(c.uid) ? (
                  <span className="fs-muted">{t('Done')}</span>
                ) : c.canExecute ? (
                  <Button variant="secondary" size="sm" icon={MailX} label={t('Unsubscribe')} loading={busy === c.uid} onClick={() => void unsubscribe(c)} />
                ) : c.methods.some((m) => m.kind === 'url') ? (
                  <Button variant="ghost" size="sm" icon={ExternalLink} label={t('Open the link')} onClick={() => openLink(c)} />
                ) : null}
              </li>
            ))}
          </ul>
        </>
      )}
    </Dialog>
  );
}
