import { Archive, ArchiveRestore, ChevronDown, ChevronUp, Copy, ExternalLink, MessageSquare, MoreHorizontal, Sparkles, Trash2 } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router';
import { Button, Dialog, EmptyState, IconButton, Menu, Skeleton } from '../../components';
import { listSessions, loadHistory, type ChatSession, type HistoryMessage } from '../../adapters/chat';
import { relativeTime } from '../../adapters/home';
import { archiveSession, autoSortSessions, bulkDeleteSessions, deleteSession, listArchivedSessions, unarchiveSession, type ArchivedSession } from '../../adapters/sessions';
import { t, tn } from '../../i18n';
import { BulkBar, Highlight, SelectToggle, useSelection } from './parts';

type Sort = 'recent' | 'oldest' | 'most-messages' | 'alpha';

interface Row {
  id: string;
  name: string;
  model: string;
  messageCount: number;
  at: string | null;
  folder: string | null;
}

/**
 * Every chat as a library row: search, folder chips, sort, a peek at the
 * last exchanges, open / copy / archive / delete, and the tidy pass that
 * files chats into folders. `archived` lists the archived ones with restore.
 */
export function ChatsLibrary({ query, say, archived = false }: { query: string; say: (m: string) => void; archived?: boolean }) {
  const navigate = useNavigate();
  const [rows, setRows] = useState<Row[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sort, setSort] = useState<Sort>('recent');
  const [folder, setFolder] = useState('');
  const [expanded, setExpanded] = useState<string | null>(null);
  const [peek, setPeek] = useState<Record<string, HistoryMessage[] | string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<string[] | null>(null);
  const [limit, setLimit] = useState(40);
  const sel = useSelection<Row>();

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      if (archived) {
        const out = await listArchivedSessions('', 0, 200);
        if (signal?.aborted) return;
        setRows(out.sessions.map((s: ArchivedSession) => ({ id: s.id, name: s.name, model: s.model, messageCount: s.messageCount, at: s.archivedAt ?? s.lastMessageAt, folder: null })));
      } else {
        const list = await listSessions(signal);
        if (signal?.aborted) return;
        setRows(list.map((s: ChatSession) => ({ id: s.id, name: s.name, model: s.model, messageCount: s.messageCount, at: s.lastMessageAt ?? s.createdAt, folder: s.folder })));
      }
      setError(null);
    } catch (e) {
      if (!signal?.aborted) setError((e as Error).message);
    }
  }, [archived]);

  useEffect(() => {
    const ac = new AbortController();
    void load(ac.signal);
    return () => ac.abort();
  }, [load]);

  const folders = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const r of rows ?? []) if (r.folder) counts[r.folder] = (counts[r.folder] ?? 0) + 1;
    return Object.entries(counts).sort((a, b) => a[0].localeCompare(b[0]));
  }, [rows]);

  const visible = useMemo(() => {
    if (!rows) return [];
    const q = query.trim().toLowerCase();
    let list = rows.filter((r) => (!q || r.name.toLowerCase().includes(q) || r.model.toLowerCase().includes(q)) && (!folder || r.folder === folder));
    if (sort === 'oldest') list = [...list].sort((a, b) => (a.at ?? '').localeCompare(b.at ?? ''));
    else if (sort === 'most-messages') list = [...list].sort((a, b) => b.messageCount - a.messageCount);
    else if (sort === 'alpha') list = [...list].sort((a, b) => a.name.localeCompare(b.name));
    else list = [...list].sort((a, b) => (b.at ?? '').localeCompare(a.at ?? ''));
    return list;
  }, [rows, query, folder, sort]);

  const act = async (what: string, work: () => Promise<void>, done?: string) => {
    setBusy(what);
    try {
      await work();
      if (done) say(done);
      await load();
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const expand = async (row: Row) => {
    if (expanded === row.id) {
      setExpanded(null);
      return;
    }
    setExpanded(row.id);
    if (!(row.id in peek)) {
      try {
        const h = await loadHistory(row.id);
        setPeek((cur) => ({ ...cur, [row.id]: h.history.slice(-6) }));
      } catch (e) {
        setPeek((cur) => ({ ...cur, [row.id]: (e as Error).message }));
      }
    }
  };

  const copyChat = async (row: Row) => {
    try {
      const h = await loadHistory(row.id);
      const text = h.history.map((m) => `${m.role === 'user' ? t('You') : h.model || t('Assistant')}: ${m.content}`).join('\n\n');
      await navigator.clipboard.writeText(text);
      say(t('Chat copied'));
    } catch (e) {
      say((e as Error).message);
    }
  };

  const shown = visible.slice(0, limit);

  return (
    <div className="fs-gal fs-lib" data-testid="chats-library">
      <div className="fs-gal__toolbar">
        {folders.length > 0 && (
          <div className="fs-gal__chips" role="group" aria-label={t('Folders')}>
            <button type="button" className="fs-chip" data-on={!folder || undefined} onClick={() => setFolder('')}>
              {t('All')} <span className="fs-gal__n">{rows?.length ?? 0}</span>
            </button>
            {folders.map(([f, n]) => (
              <button key={f} type="button" className="fs-chip" data-on={folder === f || undefined} onClick={() => setFolder(folder === f ? '' : f)}>
                {f} <span className="fs-gal__n">{n}</span>
              </button>
            ))}
          </div>
        )}
        <span className="fs-gal__spacer" />
        <select className="fs-field" value={sort} onChange={(e) => setSort(e.target.value as Sort)} aria-label={t('Sort')}>
          <option value="recent">{t('Newest first')}</option>
          <option value="oldest">{t('Oldest first')}</option>
          <option value="most-messages">{t('Most messages')}</option>
          <option value="alpha">{t('A to Z')}</option>
        </select>
        {!archived && (
          <Button
            variant="ghost"
            size="sm"
            icon={Sparkles}
            label={t('Tidy')}
            title={t('Delete empty and throwaway chats and file the rest into folders with a model')}
            loading={busy === 'tidy'}
            onClick={() =>
              void act('tidy', async () => {
                const r = await autoSortSessions(false);
                say(r.updated || r.deletedEmpty || r.deletedThrowaway ? t('Tidy: {sorted} filed into {folders} folders, {removed} removed', { sorted: r.updated, folders: r.folders.length, removed: r.deletedEmpty + r.deletedThrowaway }) : r.reason || t('Nothing to tidy'));
              })
            }
          />
        )}
        <SelectToggle selecting={sel.selecting} onToggle={() => (sel.selecting ? sel.leave() : sel.enter())} testId="chats-select" />
      </div>

      <p className="fs-gal__stats">{rows ? tn(visible.length, '{n} chat', '{n} chats') : ''}</p>

      {sel.selecting && (
        <BulkBar items={shown} selected={sel.selected} onAll={(on) => sel.all(shown, on)} label={t('Selection')}>
          {archived ? (
            <Button variant="ghost" size="sm" icon={ArchiveRestore} label={t('Restore')} disabled={!sel.selected.size} onClick={() => void act('archive', async () => { for (const id of sel.selected) await unarchiveSession(id); sel.leave(); }, t('Restored'))} />
          ) : (
            <Button variant="ghost" size="sm" icon={Archive} label={t('Archive')} disabled={!sel.selected.size} onClick={() => void act('archive', async () => { for (const id of sel.selected) await archiveSession(id); sel.leave(); }, t('Archived'))} />
          )}
          <Button variant="danger" size="sm" icon={Trash2} label={t('Delete')} disabled={!sel.selected.size} onClick={() => setConfirm([...sel.selected])} />
        </BulkBar>
      )}

      {error && (
        <p className="fs-notice" data-tone="warning">
          {error}
        </p>
      )}
      {!rows && !error && <Skeleton label={t('Loading chats')} count={4} height="56px" radius="panel" />}
      {rows && !visible.length && <EmptyState icon={MessageSquare} title={archived ? t('No archived chats') : t('No chats')} body={archived ? t('Archived chats wait here until you restore or delete them.') : t('Start one in Studio; every chat shows up here with its model and folder.')} />}

      {shown.length > 0 && (
        <ul className="fs-lib__list">
          {shown.map((row) => {
            const open = expanded === row.id;
            const p = peek[row.id];
            return (
              <li key={row.id} className="fs-lib__item" data-open={open || undefined} data-selected={sel.selected.has(row.id) || undefined}>
                <div className="fs-lib__row">
                  {sel.selecting && <input type="checkbox" className="fs-lib__cb" checked={sel.selected.has(row.id)} onChange={() => sel.toggle(row.id)} aria-label={t('Select {name}', { name: row.name })} />}
                  <button type="button" className="fs-lib__main" onClick={() => void expand(row)} aria-expanded={open}>
                    <MessageSquare size={16} aria-hidden="true" className="fs-lib__icon" />
                    <span className="fs-lib__text">
                      <span className="fs-lib__title">
                        <Highlight text={row.name} needle={query} />
                        {row.messageCount > 0 && <span className="fs-lib__badge">{tn(row.messageCount, '{n} msg', '{n} msgs')}</span>}
                      </span>
                      <span className="fs-lib__meta">{[row.model.split('/').pop(), row.folder, relativeTime(row.at)].filter(Boolean).join(' · ')}</span>
                    </span>
                    {open ? <ChevronUp size={14} aria-hidden="true" /> : <ChevronDown size={14} aria-hidden="true" />}
                  </button>
                  <Button size="sm" variant="ghost" icon={ExternalLink} label={t('Open')} onClick={() => navigate(`/studio?s=${encodeURIComponent(row.id)}`)} />
                  <Menu
                    align="end"
                    trigger={<IconButton icon={MoreHorizontal} label={t('Chat actions')} size="sm" />}
                    items={[
                      { label: t('Open'), icon: ExternalLink, onSelect: () => navigate(`/studio?s=${encodeURIComponent(row.id)}`) },
                      { label: t('Copy the conversation'), icon: Copy, onSelect: () => void copyChat(row) },
                      null,
                      archived ? { label: t('Restore'), icon: ArchiveRestore, onSelect: () => void act('archive', () => unarchiveSession(row.id), t('Restored')) } : { label: t('Archive'), icon: Archive, onSelect: () => void act('archive', () => archiveSession(row.id), t('Archived')) },
                      { label: t('Delete'), icon: Trash2, variant: 'danger', onSelect: () => setConfirm([row.id]) },
                    ]}
                  />
                </div>
                {open && (
                  <div className="fs-lib__peek">
                    {typeof p === 'string' && <p className="fs-gal__muted">{p}</p>}
                    {p === undefined && <p className="fs-gal__muted">{t('Loading…')}</p>}
                    {Array.isArray(p) && !p.length && <p className="fs-gal__muted">{t('Empty chat')}</p>}
                    {Array.isArray(p) &&
                      p.map((m, i) => (
                        <p key={i} className="fs-lib__msg" data-role={m.role}>
                          <strong>{m.role === 'user' ? t('You') : row.model.split('/').pop() || t('Assistant')}</strong> {m.content.slice(0, 400)}
                          {m.content.length > 400 ? '…' : ''}
                        </p>
                      ))}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}

      {visible.length > shown.length && (
        <div className="fs-gal__more">
          <Button variant="secondary" label={t('Show more ({n} left)', { n: visible.length - shown.length })} onClick={() => setLimit((n) => n + 40)} />
        </div>
      )}

      <Dialog
        open={!!confirm}
        onOpenChange={(o) => !o && setConfirm(null)}
        title={confirm && confirm.length > 1 ? t('Delete {n} chats?', { n: confirm.length }) : t('Delete this chat?')}
        description={t('Messages, documents and images made in it stay in the library; the conversation itself is gone for good.')}
        testId="chats-delete"
        footer={
          <>
            <Button variant="ghost" label={t('Cancel')} onClick={() => setConfirm(null)} />
            <Button
              variant="danger-solid"
              label={t('Delete')}
              onClick={() => {
                const ids = confirm ?? [];
                setConfirm(null);
                void act('delete', async () => { if (ids.length > 1) await bulkDeleteSessions(ids); else for (const id of ids) await deleteSession(id); sel.leave(); }, tn(ids.length, '{n} chat deleted', '{n} chats deleted'));
              }}
            />
          </>
        }
      />
    </div>
  );
}
