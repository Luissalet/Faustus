import { Bot, Check, Clock, RefreshCw, Trash2, X } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { Button, EmptyState, IconButton, Skeleton } from '../../components';
import { approvePendingDraft, cancelScheduled, discardPendingDraft, listPendingDrafts, listScheduled, type PendingDraft, type ScheduledMail } from '../../adapters/email';
import { t } from '../../i18n';
import { fmtFull } from './parts';

/**
 * What is leaving later: agent drafts waiting for a person, and mails
 * scheduled for a time. Both are lists the previous interface hid in
 * the "scheduled" pseudo-folder and a pill.
 */
export function Outbox({ say, onCounts }: { say: (msg: string, tone?: 'ok' | 'warn') => void; onCounts: (pending: number, scheduled: number) => void }) {
  const [pending, setPending] = useState<PendingDraft[] | null>(null);
  const [scheduled, setScheduled] = useState<ScheduledMail[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  const load = useCallback((signal?: AbortSignal) => {
    Promise.all([listPendingDrafts(signal), listScheduled(signal)])
      .then(([p, s]) => {
        setPending(p);
        setScheduled(s);
        onCounts(p.length, s.filter((x) => x.status === 'pending').length);
      })
      .catch(() => {
        setPending((c) => c ?? []);
        setScheduled((c) => c ?? []);
      });
  }, [onCounts]);

  useEffect(() => {
    const c = new AbortController();
    load(c.signal);
    return () => c.abort();
  }, [load]);

  const approve = async (d: PendingDraft) => {
    setBusy(d.id);
    try {
      await approvePendingDraft(d.id);
      say(t('Sent.'));
      load();
    } catch (err) {
      say((err as Error).message || t('Could not approve.'), 'warn');
    } finally {
      setBusy(null);
    }
  };

  const discard = async (d: PendingDraft) => {
    setBusy(d.id);
    try {
      await discardPendingDraft(d.id);
      say(t('Discarded.'));
      load();
    } catch (err) {
      say((err as Error).message || t('Could not discard.'), 'warn');
    } finally {
      setBusy(null);
    }
  };

  const cancel = async (s: ScheduledMail) => {
    setBusy(s.id);
    try {
      await cancelScheduled(s.id);
      say(t('Cancelled.'));
      load();
    } catch (err) {
      say((err as Error).message || t('Could not cancel.'), 'warn');
    } finally {
      setBusy(null);
    }
  };

  if (!pending || !scheduled) return <Skeleton label={t('Loading the outbox')} count={4} height="56px" />;
  if (!pending.length && !scheduled.length) return <EmptyState icon={Clock} title={t('Nothing waiting')} body={t('Mails you schedule with "Send later", and drafts an agent writes for your approval, show up here.')} />;

  return (
    <div className="fs-mail__outbox" data-testid="mail-outbox">
      <div className="fs-mail__outbox-head">
        <span className="fs-spacer" />
        <IconButton icon={RefreshCw} label={t('Refresh')} size="sm" onClick={() => load()} />
      </div>
      {pending.length > 0 && (
        <section aria-label={t('Waiting for your approval')}>
          <h3 className="fs-mail__group">
            <Bot size={12} aria-hidden="true" /> {t('Waiting for your approval')} · {pending.length}
          </h3>
          <ul className="fs-mail__outbox-list">
            {pending.map((d) => (
              <li key={d.id} className="fs-mail__outbox-item" data-testid="mail-pending">
                <button type="button" className="fs-mail__outbox-main" aria-expanded={open === d.id} onClick={() => setOpen((o) => (o === d.id ? null : d.id))}>
                  <span className="fs-mail__row-from">{t('To {who}', { who: d.to })}</span>
                  <span className="fs-mail__row-when">{fmtFull(d.createdAt)}</span>
                  <span className="fs-mail__row-subject">{d.subject || t('(no subject)')}</span>
                  {open !== d.id && <span className="fs-mail__row-snippet">{d.body.slice(0, 140)}</span>}
                </button>
                {open === d.id && <pre className="fs-mail__outbox-body">{d.body}</pre>}
                <div className="fs-mail__outbox-actions">
                  <Button variant="primary" size="sm" icon={Check} label={t('Approve and send')} loading={busy === d.id} onClick={() => void approve(d)} />
                  <Button variant="danger" size="sm" icon={X} label={t('Discard')} disabled={busy === d.id} onClick={() => void discard(d)} />
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}
      {scheduled.length > 0 && (
        <section aria-label={t('Scheduled')}>
          <h3 className="fs-mail__group">
            <Clock size={12} aria-hidden="true" /> {t('Scheduled')} · {scheduled.length}
          </h3>
          <ul className="fs-mail__outbox-list">
            {scheduled.map((s) => (
              <li key={s.id} className="fs-mail__outbox-item" data-status={s.status} data-testid="mail-scheduled">
                <div className="fs-mail__outbox-main">
                  <span className="fs-mail__row-from">{t('To {who}', { who: s.to })}</span>
                  <span className="fs-mail__row-when">{s.status === 'pending' ? fmtFull(s.sendAt) : s.status}</span>
                  <span className="fs-mail__row-subject">{s.subject || t('(no subject)')}</span>
                  {s.error && <span className="fs-mail__error">{s.error}</span>}
                </div>
                {s.status === 'pending' && (
                  <div className="fs-mail__outbox-actions">
                    <Button variant="ghost" size="sm" icon={Trash2} label={t('Cancel')} loading={busy === s.id} onClick={() => void cancel(s)} />
                  </div>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
