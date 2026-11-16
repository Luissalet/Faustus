import { AlertTriangle, Archive, BadgeCheck, CalendarClock, Check, Pencil, RefreshCw } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { Button, EmptyState, Skeleton } from '../../components';
import {
  loadReview,
  resolveReview,
  setRatified,
  type Contradiction,
  type ExpiryItem,
  type Review,
  type ReviewAction,
  type ReviewOption,
} from '../../adapters/brainReview';
import { locale, t } from '../../i18n';

/**
 * What needs the owner's eye: facts that contradict each other (a relation
 * that can only have one value, open with two) and content that has expired
 * or is about to. Every row carries its quick actions — keep one side and
 * archive the rest, archive, replace the value, renew the expiry — and a
 * ratified mark that makes a fact win over what agents filed.
 */

function fmt(iso: string): string {
  if (!iso) return '—';
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return iso;
  return new Date(at).toLocaleDateString(locale(), { dateStyle: 'medium' });
}

function inDays(days: number): string {
  const d = new Date(Date.now() + days * 86400000);
  return d.toISOString().slice(0, 10);
}

export function ReviewPanel({ onOpenNote, onOpenEntity, onChanged }: { onOpenNote: (path: string) => void; onOpenEntity: (entityId: string) => void; onChanged?: () => void }) {
  const [review, setReview] = useState<Review | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState('');
  const [editing, setEditing] = useState<{ ref: string; value: string } | null>(null);

  const reload = useCallback(async (signal?: AbortSignal) => {
    try {
      setReview(await loadReview(signal));
      setError(null);
    } catch (e) {
      if ((e as Error)?.name !== 'AbortError') setError(e);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void reload(controller.signal);
    return () => controller.abort();
  }, [reload]);

  async function act(key: string, run: () => Promise<unknown>) {
    setBusy(key);
    try {
      await run();
      await reload();
      onChanged?.();
    } catch (e) {
      setError(e);
    } finally {
      setBusy('');
    }
  }

  const resolve = (ref: string, action: ReviewAction, extra: { value?: string; until?: string } = {}) =>
    act(`${action}:${ref}`, () => resolveReview({ action, ref, ...extra }));

  if (!review && !error) return <Skeleton label={t('Reading the review')} count={4} height="48px" />;
  if (!review) return <EmptyState tone="error" title={t('The review could not be read')} body={String((error as Error)?.message ?? error)} primaryAction={{ label: t('Retry'), onClick: () => void reload() }} />;

  const { contradictions, expired, counts } = review;

  function renderOption(c: Contradiction, o: ReviewOption) {
    const editingThis = editing?.ref === o.ref;
    return (
      <li key={o.ref} className="fs-brain__review-option" data-ratified={o.ratified || undefined} data-testid="brain-review-option">
        <div className="fs-brain__review-value">
          <strong>{o.value}</strong>
          {o.ratified && (
            <span className="fs-badge fs-brain__ratified" data-testid="brain-ratified-badge">
              <BadgeCheck size={12} aria-hidden="true" /> {t('Ratified')}
            </span>
          )}
          <span className="fs-muted">
            {' '}· {fmt(o.validFrom)} — {o.validUntil ? fmt(o.validUntil) : t('now')} · {o.method === 'llm' ? t('filed by an agent') : t('read from the text')}
          </span>
        </div>
        {editingThis ? (
          <form
            className="fs-brain__review-edit"
            onSubmit={(e) => {
              e.preventDefault();
              if (editing.value.trim()) void resolve(o.ref, 'update', { value: editing.value.trim() }).then(() => setEditing(null));
            }}
          >
            <input value={editing.value} onChange={(e) => setEditing({ ref: o.ref, value: e.target.value })} aria-label={t('New value')} data-testid="brain-review-update-input" autoFocus />
            <Button variant="primary" size="sm" type="submit" label={t('Save')} icon={Check} />
            <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setEditing(null)} />
          </form>
        ) : (
          <div className="fs-brain__review-actions">
            <Button variant="secondary" size="sm" icon={BadgeCheck} label={t('Ratify this and archive the other')} loading={busy === `keep:${o.ref}`} onClick={() => void resolve(o.ref, 'keep')} testId="brain-review-keep" />
            <Button variant="ghost" size="sm" icon={Archive} label={t('Archive')} loading={busy === `archive:${o.ref}`} onClick={() => void resolve(o.ref, 'archive')} testId="brain-review-archive" />
            <Button variant="ghost" size="sm" icon={Pencil} label={t('Update')} onClick={() => setEditing({ ref: o.ref, value: o.value })} testId="brain-review-update" />
          </div>
        )}
      </li>
    );
  }

  function renderExpiry(item: ExpiryItem) {
    return (
      <li key={item.id} className="fs-brain__review-row" data-state={item.state} data-testid="brain-review-expiry">
        <div className="fs-brain__review-value">
          {item.kind === 'note' ? (
            <button type="button" className="fs-brain__link-item" onClick={() => onOpenNote(item.path)}>{item.title}</button>
          ) : (
            <strong>{item.title}</strong>
          )}
          <span className="fs-badge" data-tone={item.state === 'expired' ? 'danger' : 'warning'}>
            <CalendarClock size={12} aria-hidden="true" /> {item.state === 'expired' ? t('Expired {date}', { date: fmt(item.expiresAt) }) : t('Expires {date}', { date: fmt(item.expiresAt) })}
          </span>
          {item.ratified && <span className="fs-badge fs-brain__ratified"><BadgeCheck size={12} aria-hidden="true" /> {t('Ratified')}</span>}
          <span className="fs-muted"> · {item.kind === 'note' ? t('note') : t('fact')}</span>
        </div>
        <div className="fs-brain__review-actions">
          <Button variant="secondary" size="sm" icon={RefreshCw} label={t('Renew 90 days')} loading={busy === `renew:${item.ref}`} onClick={() => void resolve(item.ref, 'renew', { until: inDays(90) })} testId="brain-review-renew" />
          <Button variant="ghost" size="sm" label={t('No expiry')} onClick={() => void resolve(item.ref, 'renew', { until: '' })} testId="brain-review-clear" />
          <Button variant="ghost" size="sm" icon={Archive} label={t('Archive')} loading={busy === `archive:${item.ref}`} onClick={() => void resolve(item.ref, 'archive')} testId="brain-review-expiry-archive" />
          {item.kind === 'fact' && item.entityId && <Button variant="ghost" size="sm" label={t('Open entity')} onClick={() => onOpenEntity(item.entityId)} />}
        </div>
      </li>
    );
  }

  return (
    <section className="fs-brain__review" data-testid="brain-review" aria-label={t('Review')}>
      <header className="fs-brain__review-head">
        <h3>{t('Review')}</h3>
        <span className="fs-muted" data-testid="brain-review-counts">
          {t('{a} contradictions · {b} expired · {c} expiring soon · {d} ratified', { a: counts.contradictions, b: counts.expired, c: counts.soon, d: counts.ratified })}
        </span>
        <Button variant="ghost" size="sm" icon={RefreshCw} label={t('Refresh')} onClick={() => void reload()} />
      </header>
      {error != null && (
        <p className="fs-notice" data-tone="warning"><AlertTriangle size={12} aria-hidden="true" /> {String((error as Error)?.message ?? error)}</p>
      )}

      <h4>{t('Contradictions')}</h4>
      {contradictions.length === 0 && <p className="fs-muted" data-testid="brain-review-no-contradictions">{t('No fact contradicts another.')}</p>}
      {contradictions.map((c) => (
        <article key={c.id} className="fs-brain__review-card" data-settled={c.settledByRatification || undefined} data-testid="brain-review-contradiction">
          <header>
            <button type="button" className="fs-brain__link-item" onClick={() => onOpenEntity(c.entity.id)}>{c.entity.name}</button>
            <span className="fs-brain__rel-verb"> {c.rel.replace(/_/g, ' ')}</span>
            {c.settledByRatification && <span className="fs-badge fs-brain__ratified"><BadgeCheck size={12} aria-hidden="true" /> {t('Settled by a ratified fact')}</span>}
          </header>
          <ul className="fs-brain__review-options">{c.options.map((o) => renderOption(c, o))}</ul>
        </article>
      ))}

      <h4>{t('Expired and expiring content')}</h4>
      {expired.length === 0 && <p className="fs-muted" data-testid="brain-review-no-expired">{t('Nothing has expired. Put caduca: 2027-01-31 in a note, or give a fact an end date, and it shows up here.')}</p>}
      <ul className="fs-brain__review-list">{expired.map(renderExpiry)}</ul>
    </section>
  );
}

/** A small toggle: ratify (or un-ratify) one ref and report the new state. */
export function RatifyButton({ refId, ratified, onChange, label }: { refId: string; ratified: boolean; onChange: (next: boolean) => void; label?: string }) {
  const [busy, setBusy] = useState(false);
  async function toggle() {
    setBusy(true);
    try {
      await setRatified(refId, !ratified);
      onChange(!ratified);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Button
      variant={ratified ? 'secondary' : 'ghost'}
      size="sm"
      icon={BadgeCheck}
      label={ratified ? t('Ratified') : (label ?? t('Ratify'))}
      loading={busy}
      title={ratified ? t('Ratified by you: it wins over what agents filed. Click to remove.') : t('Mark as confirmed by you: it will win over what agents filed.')}
      onClick={() => void toggle()}
      testId="brain-ratify-toggle"
    />
  );
}
