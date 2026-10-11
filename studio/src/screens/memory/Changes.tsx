import { History, RefreshCw, Trash2, Undo2 } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useSearchParams } from 'react-router';
import { Button, Dialog, EmptyState, IconButton, Skeleton } from '../../components';
import {
  clearMemoryChanges, listMemoryChanges, undoMemoryChange,
  type MemoryChange, type MemoryStore,
} from '../../adapters/memoryJournal';
import { groupByTurn, OP_LABEL, sourceLabel, STORE_LABEL, undoMessage, when } from './changes-model';
import { t, tn } from '../../i18n';

/**
 * Changes (Memory → `?t=changes`): every memory created, edited or deleted
 * in the facts store, the learned memory, the instincts and the notes, newest
 * first, with the chat that did it and an undo per change. Changes made in
 * the same turn are grouped. `?session=<id>` narrows it to one conversation
 * and `?turn=<id>` to one turn (the turn summary links here). «Clear history»
 * deletes the record (not the memories).
 */

const STORES: { id: MemoryStore | ''; label: string }[] = [
  { id: '', label: 'All' },
  { id: 'facts', label: 'Facts' },
  { id: 'rules', label: 'Learned' },
  { id: 'instincts', label: 'Instincts' },
  { id: 'notes', label: 'Notes' },
];

export function Changes() {
  const [params, setParams] = useSearchParams();
  const sessionId = params.get('session') ?? '';
  const turnId = params.get('turn') ?? '';
  const store = (params.get('store') ?? '') as MemoryStore | '';
  const [changes, setChanges] = useState<MemoryChange[] | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const [notice, setNotice] = useState<{ id: string; text: string } | null>(null);
  const [confirmClear, setConfirmClear] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [cleared, setCleared] = useState<number | null>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      setError('');
      setChanges(await listMemoryChanges({ sessionId, turnId, store }, signal));
    } catch (exc) {
      if ((exc as Error).name !== 'AbortError') setError((exc as Error).message || t('Could not load the changes.'));
    }
  }, [sessionId, turnId, store]);

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

  const showAll = () => {
    const next = new URLSearchParams(params);
    next.delete('session');
    next.delete('turn');
    setParams(next, { replace: true });
  };

  const undo = async (change: MemoryChange) => {
    setBusy(change.id);
    setNotice(null);
    setCleared(null);
    try {
      await undoMemoryChange(change.id);
      await load();
    } catch (exc) {
      setNotice({ id: change.id, text: undoMessage(exc) });
    } finally {
      setBusy('');
    }
  };

  const clearHistory = async () => {
    setClearing(true);
    try {
      setCleared(await clearMemoryChanges());
      setConfirmClear(false);
      setNotice(null);
      await load();
    } catch (exc) {
      setError((exc as Error).message || t('Could not clear the history.'));
      setConfirmClear(false);
    } finally {
      setClearing(false);
    }
  };

  const blocks = useMemo(() => groupByTurn(changes ?? []), [changes]);

  const row = (c: MemoryChange) => (
    <li key={c.id} className="fs-memory-changes__row" data-undone={c.undone || undefined} data-testid="memory-change">
      <div className="fs-memory-changes__meta">
        <span className={`fs-memory-changes__op fs-memory-changes__op--${c.op}`}>{t(OP_LABEL[c.op])}</span>
        <span className="fs-memory-changes__store">{t(STORE_LABEL[c.store] ?? c.store)}</span>
        <time dateTime={new Date(c.ts * 1000).toISOString()}>{when(c.ts)}</time>
        {c.source && <span className="fs-memory-changes__source">{sourceLabel(c.source)}</span>}
        {c.sessionId && !sessionId && (
          <button type="button" className="fs-linkbutton" onClick={() => setFilter('session', c.sessionId)}>{t('This conversation')}</button>
        )}
      </div>
      <p className="fs-memory-changes__text">
        {c.purged ? <em>{t('The text was erased when it was forgotten.')}</em> : (c.label || c.itemId)}
        {!c.purged && c.op === 'edit' && c.beforeLabel && <span className="fs-memory-changes__before">{t('before')}: {c.beforeLabel}</span>}
      </p>
      <div className="fs-memory-changes__actions">
        {c.undone
          ? <span className="fs-memory-changes__undone">{t('Rolled back')}</span>
          : !c.purged && (
            <Button size="sm" variant="ghost" icon={Undo2} label={t('Undo')} loading={busy === c.id}
              disabled={!!busy} onClick={() => void undo(c)} />
          )}
        {notice?.id === c.id && <span className="fs-memory-changes__error" role="alert">{notice.text}</span>}
      </div>
    </li>
  );

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
        {(sessionId || turnId) && (
          <span className="fs-memory-changes__session">
            {turnId ? t('Only this turn') : t('Only this conversation')}
            {sessionId && <> · <Link to={`/studio?s=${encodeURIComponent(sessionId)}`}>{t('Open it')}</Link></>}
            {' · '}<button type="button" className="fs-linkbutton" onClick={showAll}>{t('Show all')}</button>
          </span>
        )}
        <span className="fs-memory-changes__tools">
          <Button size="sm" variant="ghost" icon={Trash2} label={t('Clear history')} testId="memory-changes-clear"
            disabled={clearing || !changes || changes.length === 0} onClick={() => setConfirmClear(true)} />
          <IconButton icon={RefreshCw} label={t('Reload')} onClick={() => void load()} />
        </span>
      </div>

      {cleared !== null && (
        <p className="fs-memory-changes__undone" role="status" data-testid="memory-changes-cleared">
          {tn(cleared, 'History cleared: {n} change', 'History cleared: {n} changes')}
        </p>
      )}
      {error && <p className="fs-memory-changes__error" role="alert">{error}</p>}
      {changes === null && !error && <Skeleton label={t('Loading changes')} count={4} height="3rem" />}
      {changes && changes.length === 0 && (
        <EmptyState icon={History} title={t('No memory changes yet')}
          body={t('When the assistant learns, edits or forgets something, it shows up here with an undo.')} />
      )}
      {changes && changes.length > 0 && (
        <ol className="fs-memory-changes__list">
          {blocks.map((block) => block.rows.length === 1
            ? row(block.rows[0])
            : (
              <li key={`turn-${block.turnId}`} className="fs-memory-changes__group" data-testid="memory-change-group">
                <div className="fs-memory-changes__grouphead">
                  <time dateTime={new Date(block.rows[0].ts * 1000).toISOString()}>{when(block.rows[0].ts)}</time>
                  <span>{tn(block.rows.length, '{n} change in this turn', '{n} changes in this turn')}</span>
                  {!turnId && (
                    <button type="button" className="fs-linkbutton" onClick={() => setFilter('turn', block.turnId)}>{t('Only this turn')}</button>
                  )}
                </div>
                <ol className="fs-memory-changes__list">{block.rows.map(row)}</ol>
              </li>
            ))}
        </ol>
      )}

      <Dialog open={confirmClear} onOpenChange={(o) => { if (!o && !clearing) setConfirmClear(false); }}
        title={t('Clear the change history?')}
        description={t('This deletes the record of what was learned, edited or forgotten, including the text it kept. Your memories are not touched, but these changes can no longer be undone.')}
        testId="memory-changes-clear-dialog"
        footer={(
          <>
            <Button variant="ghost" size="sm" label={t('Cancel')} disabled={clearing} onClick={() => setConfirmClear(false)} />
            <Button variant="danger-solid" size="sm" icon={Trash2} label={t('Clear history')} loading={clearing}
              testId="memory-changes-clear-confirm" onClick={() => void clearHistory()} />
          </>
        )} />
    </section>
  );
}
