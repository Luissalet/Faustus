import { BellRing, MessagesSquare } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { listThreads, type AgoraSubthread, type ThreadState } from '../../adapters/agoraThreads';
import { EmptyState, Skeleton } from '../../components';
import { t, tn } from '../../i18n';
import { ago } from '../agora/util';
import { THREAD_STATE_LABEL } from './ThreadParts';
import './agora-threads.css';

/** The threads started from messages: the threads a reader follows (or every thread of the server), with what has not been seen. */
export function FollowedThreads({ space, version, onOpen }: { space: string; version: string; onOpen: (threadId: number) => void }) {
  const [scope, setScope] = useState<'following' | 'all'>('following');
  const [state, setState] = useState<ThreadState | 'all'>('open');
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [rows, setRows] = useState<AgoraSubthread[] | null>(null);
  const [error, setError] = useState('');
  const load = useCallback((signal?: AbortSignal) => {
    listThreads(space, { scope, state, unread: unreadOnly }, signal).then((out) => { setRows(out.threads); setError(''); })
      .catch((e: Error) => { if (!signal?.aborted) setError(e.message); });
  }, [space, scope, state, unreadOnly]);
  useEffect(() => { const controller = new AbortController(); setRows(null); load(controller.signal); return () => controller.abort(); }, [load]);
  useEffect(() => { if (version) load(); }, [version]);   // eslint-disable-line react-hooks/exhaustive-deps
  const unread = (rows ?? []).reduce((n, r) => n + r.unread, 0);
  return (
    <section className="fs-agt-view" data-testid="agora-threads-view">
      <header className="fs-agt-view__head">
        <h2><MessagesSquare size={18} aria-hidden="true" /> {t('Threads from messages')}</h2>
        <div className="fs-seg" role="radiogroup" aria-label={t('Which threads')}>
          <button type="button" role="radio" aria-checked={scope === 'following'} onClick={() => setScope('following')}>{t('Following')}</button>
          <button type="button" role="radio" aria-checked={scope === 'all'} onClick={() => setScope('all')}>{t('All')}</button>
        </div>
        <select value={state} onChange={(e) => setState(e.target.value as ThreadState | 'all')} aria-label={t('State')}>
          {(['open', 'closed', 'archived'] as ThreadState[]).map((s) => <option key={s} value={s}>{t(THREAD_STATE_LABEL[s])}</option>)}
          <option value="all">{t('Any state')}</option>
        </select>
        <label className="fs-agt-check"><input type="checkbox" checked={unreadOnly} onChange={(e) => setUnreadOnly(e.target.checked)} /> {t('Only with something new')}</label>
        {unread > 0 && <span className="fs-agt-new">{unread}</span>}
      </header>
      {error && <p className="fs-notice" data-tone="danger" role="alert">{error}</p>}
      {rows === null && !error && <Skeleton label={t('Loading the threads')} count={4} height="52px" />}
      {rows !== null && rows.length === 0 && (
        <EmptyState title={t('No threads here')} body={scope === 'following'
          ? t('Start one from any message with «Start a thread»: you follow the ones you start, write in or are named in.')
          : t('Nobody has started a thread on this server yet.')} />
      )}
      <ul className="fs-agt-list">
        {(rows ?? []).map((row) => (
          <li key={row.thread_id}>
            <button type="button" className="fs-agt-row" data-unread={row.unread > 0 || undefined} data-state={row.state} onClick={() => onOpen(row.thread_id)} data-testid="agora-threads-row">
              <span className="fs-agt-row__title">
                {row.following && <BellRing size={13} aria-label={t('Following')} />}
                <strong>{row.title}</strong>
                {row.state !== 'open' && <span className="fs-agt-state">{t(THREAD_STATE_LABEL[row.state])}</span>}
                {row.unread > 0 && <span className="fs-agt-new">{row.unread}</span>}
              </span>
              <span className="fs-muted fs-agt-row__origin">{row.channel ? '#' + row.channel + ' · ' : ''}{row.origin.author}: {row.origin.excerpt}</span>
              <span className="fs-muted">{tn(row.replies, '{n} reply', '{n} replies')}{row.last_at ? ' · ' + t('last {when}', { when: ago(row.last_at) }) : ''}{row.last_author ? ' · ' + row.last_author : ''}</span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
