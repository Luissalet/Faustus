import { History, RefreshCw, Undo2 } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { Link, useSearchParams } from 'react-router';
import { Button, EmptyState, IconButton, Skeleton } from '../../components';
import { listMemoryChanges, undoMemoryChange, type MemoryChange, type MemoryStore } from '../../adapters/memoryJournal';
import { t } from '../../i18n';

/**
 * Changes (Memory → `?t=changes`): every memory created, edited or deleted
 * in the facts store, the learned memory, the instincts and the notes, newest
 * first, with the chat that did it and an undo per change. `?session=<id>`
 * narrows it to one conversation (the turn summary links here).
 */

const STORES: { id: MemoryStore | ''; label: string }[] = [
  { id: '', label: 'All' },
  { id: 'facts', label: 'Facts' },
  { id: 'rules', label: 'Learned' },
  { id: 'instincts', label: 'Instincts' },
  { id: 'notes', label: 'Notes' },
];

const STORE_LABEL: Record<MemoryStore, string> = { facts: 'Facts', rules: 'Learned', instincts: 'Instincts', notes: 'Notes' };
const OP_LABEL: Record<MemoryChange['op'], string> = { create: 'Added', edit: 'Edited', delete: 'Deleted' };

function when(ts: number): string {
  const d = new Date(ts * 1000);
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'short' });
}

export function Changes() {
  const [params, setParams] = useSearchParams();
  const sessionId = params.get('session') ?? '';
  const store = (params.get('store') ?? '') as MemoryStore | '';
  const [changes, setChanges] = useState<MemoryChange[] | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const [notice, setNotice] = useState<{ id: string; text: string } | null>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      setError('');
      setChanges(await listMemoryChanges({ sessionId, store }, signal));
    } catch (exc) {
      if ((exc as Error).name !== 'AbortError') setError((exc as Error).message || t('Could not load the changes.'));
    }
  }, [sessionId, store]);

  useEffect(() => {
    const ctrl = new AbortController();
    setChanges(null);
    void load(ctrl.signal);
    return () => ctrl.abort();
  }, [load]);

  const setFilter = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value); else next.delete(key);
    setParams(next, { replace: true });
  };

  const undo = async (change: MemoryChange) => {
    setBusy(change.id);
    setNotice(null);
    try {
      await undoMemoryChange(change.id);
      await load();
    } catch (exc) {
      setNotice({ id: change.id, text: (exc as Error).message || t('Could not undo it.') });
    } finally {
      setBusy('');
    }
  };

  return (
    <section className="fs-memory-changes" data-testid="memory-changes">
      <div className="fs-memory-changes__bar">
        <div className="fs-memory-changes__stores" role="group" aria-label={t('Store')}>
          {STORES.map((s) => (
            <button key={s.id || 'all'} type="button" className="fs-chip" aria-pressed={store === s.id} data-on={store === s.id || undefined}
              onClick={() => setFilter('store', s.id)} data-testid={`memory-changes-store-${s.id || 'all'}`}>
              {t(s.label)}
            </button>
          ))}
        </div>
        {sessionId && (
          <span className="fs-memory-changes__session">
            {t('Only this conversation')} · <Link to={`/studio?s=${encodeURIComponent(sessionId)}`}>{t('Open it')}</Link>
            {' · '}<button type="button" className="fs-linkbutton" onClick={() => setFilter('session', '')}>{t('Show all')}</button>
          </span>
        )}
        <IconButton icon={RefreshCw} label={t('Reload')} onClick={() => void load()} />
      </div>

      {error && <p className="fs-memory-changes__error" role="alert">{error}</p>}
      {changes === null && !error && <Skeleton label={t('Loading changes')} count={4} height="3rem" />}
      {changes && changes.length === 0 && (
        <EmptyState icon={History} title={t('No memory changes yet')}
          body={t('When the assistant learns, edits or forgets something, it shows up here with an undo.')} />
      )}
      {changes && changes.length > 0 && (
        <ol className="fs-memory-changes__list">
          {changes.map((c) => (
            <li key={c.id} className="fs-memory-changes__row" data-undone={c.undone || undefined} data-testid="memory-change">
              <div className="fs-memory-changes__meta">
                <span className={`fs-memory-changes__op fs-memory-changes__op--${c.op}`}>{t(OP_LABEL[c.op])}</span>
                <span className="fs-memory-changes__store">{t(STORE_LABEL[c.store] ?? c.store)}</span>
                <time>{when(c.ts)}</time>
                {c.source && <span className="fs-memory-changes__source">{c.source}</span>}
                {c.sessionId && !sessionId && (
                  <button type="button" className="fs-linkbutton" onClick={() => setFilter('session', c.sessionId)}>{t('This conversation')}</button>
                )}
              </div>
              <p className="fs-memory-changes__text">
                {c.label || c.itemId}
                {c.op === 'edit' && c.beforeLabel && <span className="fs-memory-changes__before">{t('before')}: {c.beforeLabel}</span>}
              </p>
              <div className="fs-memory-changes__actions">
                {c.undone
                  ? <span className="fs-memory-changes__undone">{t('Undone')}</span>
                  : <Button size="sm" variant="ghost" icon={Undo2} label={t('Undo')} loading={busy === c.id}
                      disabled={!!busy} onClick={() => void undo(c)} />}
                {notice?.id === c.id && <span className="fs-memory-changes__error" role="alert">{notice.text}</span>}
              </div>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
