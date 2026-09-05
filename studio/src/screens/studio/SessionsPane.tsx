import { Archive, ArchiveRestore, ArrowUpDown, Bot, CheckSquare, Download, FolderOpen, MoreHorizontal, Plus, Search, Sparkles, Star, Trash2, Users, X } from 'lucide-react';
import { lazy, Suspense, useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router';
import { Button, IconButton, QuickMenu, Skeleton } from '../../components';
import type { ChatSession } from '../../adapters/chat';
import { relativeTime } from '../../adapters/home';
import { isGroupSessionName, stripGroupPrefix } from '../../adapters/group';
import {
  archiveSession,
  autoSortSessions,
  bulkDeleteSessions,
  downloadExportZip,
  listArchivedSessions,
  unarchiveSession,
  type ArchivedSession,
} from '../../adapters/sessions';
import { t, tn } from '../../i18n';

const SessionDialog = lazy(() => import('./SessionDialog'));

export type SortMode = 'active' | 'created' | 'name' | 'group';
const SORT_KEY = 'odysseus-session-sort'; // shared with the previous interface

export function readSortMode(): SortMode {
  try {
    const v = localStorage.getItem(SORT_KEY)?.replace(/^"|"$/g, '');
    return v === 'created' || v === 'name' || v === 'group' ? v : 'active';
  } catch {
    return 'active';
  }
}

const SORT_LABELS: Record<SortMode, string> = {
  active: 'Last activity',
  created: 'Creation date',
  name: 'Name',
  group: 'By folder',
};

export interface SessionsPaneProps {
  sessions: ChatSession[] | null;
  currentId: string | null;
  filter: string;
  setFilter: (value: string) => void;
  searchRef: React.RefObject<HTMLInputElement | null>;
  onOpen: (id: string | null) => void;
  /** After any change, so the list re-reads from the server. */
  onChanged: () => void;
  onNotice: (text: string, tone?: 'info' | 'warning' | 'danger') => void;
}

function sortSessions(list: ChatSession[], mode: SortMode): ChatSession[] {
  const out = list.slice();
  if (mode === 'name') out.sort((a, b) => a.name.localeCompare(b.name, 'es'));
  else if (mode === 'created') out.sort((a, b) => (b.createdAt ?? '').localeCompare(a.createdAt ?? ''));
  else out.sort((a, b) => (b.lastMessageAt ?? b.createdAt ?? '').localeCompare(a.lastMessageAt ?? a.createdAt ?? ''));
  // Favourites float, like the previous sidebar.
  return out.sort((a, b) => Number(b.isImportant) - Number(a.isImportant));
}

function Row({ s, i, currentId, selecting, selected, onToggle, onOpen, onMenu }: { s: ChatSession; i: number; currentId: string | null; selecting: boolean; selected: boolean; onToggle: () => void; onOpen: (id: string) => void; onMenu: () => void }) {
  return (
    <div className="fs-studio__session-row" role="listitem" data-selected={selected || undefined} data-selecting={selecting || undefined}>
      {selecting && (
        <input type="checkbox" className="fs-studio__session-check" checked={selected} onChange={onToggle} aria-label={t('Select {name}', { name: s.name })} />
      )}
      <Link
        to={`/studio?s=${encodeURIComponent(s.id)}`}
        className="fs-studio__session fs-enter"
        style={{ ['--i' as string]: Math.min(i, 8) }}
        aria-current={s.id === currentId ? 'page' : undefined}
        data-testid="studio-session"
        onClick={(event) => {
          event.preventDefault();
          if (selecting) onToggle();
          else onOpen(s.id);
        }}
      >
        <span className="fs-studio__session-name">
          {s.isImportant && <Star size={11} aria-label={t('Favourite')} className="fs-studio__star" />}
          {isGroupSessionName(s.name) && <Users size={11} aria-label={t('Group chat')} />}
          {isGroupSessionName(s.name) ? stripGroupPrefix(s.name) : s.name}
        </span>
        <span className="fs-studio__session-meta">
          {s.mode === 'agent' && <Bot size={11} aria-label={t('Agent')} />}
          {s.model ? s.model.split('/').pop() : t('no model')}
          {s.lastMessageAt && ` · ${relativeTime(s.lastMessageAt)}`}
        </span>
      </Link>
      {!selecting && <IconButton icon={MoreHorizontal} label={t('Actions for {name}', { name: s.name })} size="sm" onClick={onMenu} testId="session-menu" />}
    </div>
  );
}

function ArchivedList({ onOpen, onChanged, onNotice, onBack }: { onOpen: (id: string) => void; onChanged: () => void; onNotice: SessionsPaneProps['onNotice']; onBack: () => void }) {
  const [items, setItems] = useState<ArchivedSession[] | null>(null);
  const [q, setQ] = useState('');
  useEffect(() => {
    const timer = window.setTimeout(() => {
      listArchivedSessions(q)
        .then((r) => setItems(r.sessions))
        .catch(() => onNotice(t('Could not read the archived ones.'), 'danger'));
    }, 150);
    return () => window.clearTimeout(timer);
  }, [q, onNotice]);
  return (
    <>
      <div className="fs-studio__sessions-head">
        <span className="fs-panel__label" style={{ margin: 0 }}>
          Archivadas
        </span>
        <IconButton icon={X} label={t('Back to the conversations')} size="sm" onClick={onBack} />
      </div>
      <label className="fs-search fs-studio__search">
        <Search size={14} aria-hidden="true" />
        <input type="search" value={q} placeholder={t('Search archived…')} aria-label={t('Search archived')} onChange={(e) => setQ(e.target.value)} />
      </label>
      <div className="fs-studio__list" role="list">
        {!items && <Skeleton label={t('Loading archived')} count={5} height="44px" />}
        {items?.map((s) => (
          <div key={s.id} className="fs-studio__session-row" role="listitem">
            <button type="button" className="fs-studio__session" onClick={() => onOpen(s.id)}>
              <span className="fs-studio__session-name">{s.name}</span>
              <span className="fs-studio__session-meta">
                {s.model || t('no model')} · {s.messageCount} mensajes{s.lastMessageAt ? ` · ${relativeTime(s.lastMessageAt)}` : ''}
              </span>
            </button>
            <IconButton
              icon={ArchiveRestore}
              label={t('Recover {name}', { name: s.name })}
              size="sm"
              onClick={() => {
                unarchiveSession(s.id)
                  .then(() => {
                    setItems((list) => list?.filter((x) => x.id !== s.id) ?? null);
                    onChanged();
                    onNotice(t('Conversation recovered.'));
                  })
                  .catch(() => onNotice(t('Could not recover it.'), 'danger'));
              }}
            />
          </div>
        ))}
        {items && items.length === 0 && <p className="fs-studio__hint">{q ? t('No archived conversations with that name.') : t('No archived conversations.')}</p>}
      </div>
    </>
  );
}

/** The conversations column: search, sort, folders, bulk selection, the
 *  archive, and a "…" per row. */
export function SessionsPane({ sessions, currentId, filter, setFilter, searchRef, onOpen, onChanged, onNotice }: SessionsPaneProps) {
  const [target, setTarget] = useState<ChatSession | null>(null);
  const [sort, setSort] = useState<SortMode>(readSortMode);
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [archive, setArchive] = useState(false);
  const [openFolders, setOpenFolders] = useState<Record<string, boolean>>({});
  const [confirmBulk, setConfirmBulk] = useState(false);
  const [tidying, setTidying] = useState(false);

  useEffect(() => {
    try {
      localStorage.setItem(SORT_KEY, sort);
    } catch {
      /* private mode */
    }
  }, [sort]);

  useEffect(() => {
    if (!selecting) {
      setSelected(new Set());
      setConfirmBulk(false);
    }
  }, [selecting]);

  const needle = filter.trim().toLowerCase();
  const visible = useMemo(() => {
    const base = needle ? (sessions ?? []).filter((s) => `${s.name} ${s.model} ${s.folder ?? ''}`.toLowerCase().includes(needle)) : (sessions ?? []);
    return sortSessions(base, sort);
  }, [sessions, needle, sort]);

  const groups = useMemo(() => {
    if (sort !== 'group') return null;
    const map = new Map<string, ChatSession[]>();
    for (const s of visible) {
      const key = s.folder ?? '';
      if (!map.has(key)) map.set(key, []);
      map.get(key)?.push(s);
    }
    const names = Array.from(map.keys()).sort((a, b) => (a === '' ? 1 : b === '' ? -1 : a.localeCompare(b, 'es')));
    return names.map((name) => ({ name, items: map.get(name) ?? [] }));
  }, [visible, sort]);

  const toggle = (id: string) =>
    setSelected((set) => {
      const next = new Set(set);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const bulk = async (what: 'archive' | 'delete') => {
    const ids = Array.from(selected);
    if (!ids.length) return;
    try {
      if (what === 'archive') {
        await Promise.all(ids.map((id) => archiveSession(id)));
        onNotice(tn(ids.length, '{n} conversation archived.', '{n} conversations archived.'));
      } else {
        const n = await bulkDeleteSessions(ids);
        onNotice(`${n} conversaciones borradas.`);
        if (currentId && ids.includes(currentId)) onOpen(null);
      }
      setSelecting(false);
      onChanged();
    } catch (e) {
      onNotice(`No he podido: ${(e as Error).message}`, 'danger');
    }
  };

  const tidy = async (withAi: boolean) => {
    setTidying(true);
    try {
      const r = await autoSortSessions(!withAi);
      const bits = [
        r.deletedEmpty ? t('{n} empty deleted', { n: r.deletedEmpty }) : '',
        r.deletedThrowaway ? t('{n} throwaway deleted', { n: r.deletedThrowaway }) : '',
        r.updated ? t('{n} filed into {f} folders', { n: r.updated, f: r.folders.length }) : '',
      ].filter(Boolean);
      onNotice(r.status === 'skipped' ? (r.reason ?? t('There was nothing to tidy.')) : bits.length ? bits.join(' · ') : t('Nothing to tidy.'));
      if (withAi && r.updated) setSort('group');
      onChanged();
    } catch (e) {
      onNotice(`${t('Could not tidy')}: ${(e as Error).message}`, 'danger');
    } finally {
      setTidying(false);
    }
  };

  if (archive) {
    return (
      <aside className="fs-studio__sessions" aria-label={t('Archived conversations')}>
        <ArchivedList onOpen={(id) => onOpen(id)} onChanged={onChanged} onNotice={onNotice} onBack={() => setArchive(false)} />
      </aside>
    );
  }

  const renderRows = (list: ChatSession[], offset = 0) =>
    list.map((s, i) => (
      <Row key={s.id} s={s} i={i + offset} currentId={currentId} selecting={selecting} selected={selected.has(s.id)} onToggle={() => toggle(s.id)} onOpen={(id) => onOpen(id)} onMenu={() => setTarget(s)} />
    ));

  return (
    <aside className="fs-studio__sessions" aria-label={t('Conversations')}>
      <div className="fs-studio__sessions-head">
        <span className="fs-panel__label" style={{ margin: 0 }}>
          {t('Conversations')}
        </span>
        <span className="fs-studio__sessions-tools">
          <QuickMenu
            label={t('Sort and organise')}
            icon={ArrowUpDown}
            testId="sessions-menu"
            items={[
              ...(Object.keys(SORT_LABELS) as SortMode[]).map((m) => ({ label: `${m === sort ? '✓ ' : ''}${t(SORT_LABELS[m])}`, onSelect: () => setSort(m) })),
              null,
              { label: selecting ? t('Leave selection') : t('Select several'), icon: CheckSquare, onSelect: () => setSelecting((v) => !v) },
              { label: t('Archived…'), icon: Archive, onSelect: () => setArchive(true) },
              { label: t('Sort into folders with AI'), icon: Sparkles, onSelect: () => void tidy(true) },
              { label: t('Clean up empty ones (no AI)'), icon: Trash2, onSelect: () => void tidy(false) },
            ]}
          />
          <IconButton icon={Plus} label={t('New conversation')} size="sm" onClick={() => onOpen(null)} />
        </span>
      </div>
      <label className="fs-search fs-studio__search">
        <Search size={14} aria-hidden="true" />
        <input
          ref={searchRef}
          type="search"
          value={filter}
          placeholder={t('Search…')}
          aria-label={t('Search conversations')}
          onChange={(event) => setFilter(event.target.value)}
          data-testid="studio-search"
        />
      </label>

      {selecting && (
        <div className="fs-studio__bulk" data-testid="session-bulk">
          <span>{tn(selected.size, '{n} selected', '{n} selected#')}</span>
          <button type="button" className="fs-link" onClick={() => setSelected(new Set(visible.map((s) => s.id)))}>
            {t('all')}
          </button>
          <span className="fs-studio__bulk-actions">
            <Button
              size="sm"
              icon={Download}
              label="Zip"
              disabled={selected.size === 0}
              title={t('Export the selected ones as a zip (markdown)')}
              onClick={() => {
                // A link cannot see "too many at once": fetch it so the
                // server's refusal is a message and not a blank tab.
                void downloadExportZip('md', { ids: Array.from(selected) }).catch((e: Error) =>
                  onNotice(`${t('Could not export')}: ${e.message}`, 'danger'),
                );
              }}
            />
            <Button size="sm" icon={Archive} label={t('Archive')} disabled={selected.size === 0} onClick={() => void bulk('archive')} />
            {confirmBulk ? (
              <Button size="sm" variant="danger-solid" icon={Trash2} label={t('Delete {n}', { n: selected.size })} onClick={() => void bulk('delete')} />
            ) : (
              <Button size="sm" variant="danger" icon={Trash2} label={t('Delete')} disabled={selected.size === 0} onClick={() => setConfirmBulk(true)} />
            )}
            <IconButton icon={X} label={t('Leave selection')} size="sm" onClick={() => setSelecting(false)} />
          </span>
        </div>
      )}
      {tidying && <p className="fs-studio__hint">{t('Tidying…')}</p>}

      <div className="fs-studio__list" role="list">
        {!sessions && <Skeleton label={t('Loading conversations')} count={6} height="44px" />}
        {groups
          ? groups.map((g) => {
              const open = openFolders[g.name] ?? true;
              return (
                <section key={g.name || '__none'} className="fs-studio__folder" data-open={open || undefined}>
                  <button type="button" className="fs-studio__folder-head" onClick={() => setOpenFolders((o) => ({ ...o, [g.name]: !open }))} aria-expanded={open}>
                    <FolderOpen size={13} aria-hidden="true" />
                    <span>{g.name || t('No folder')}</span>
                    <span className="fs-studio__folder-count">{g.items.length}</span>
                  </button>
                  {open && renderRows(g.items)}
                </section>
              );
            })
          : renderRows(visible)}
        {sessions && sessions.length === 0 && <p className="fs-studio__hint">{t('No conversations yet. You start the first one below.')}</p>}
        {sessions && sessions.length > 0 && visible.length === 0 && <p className="fs-studio__hint">{t('No conversation has that name.')}</p>}
      </div>

      {target && (
        <Suspense fallback={null}>
          <SessionDialog
            target={target}
            currentId={currentId}
            folders={Array.from(new Set((sessions ?? []).map((s) => s.folder).filter((f): f is string => Boolean(f)))).sort()}
            onClose={() => setTarget(null)}
            onOpen={onOpen}
            onChanged={onChanged}
            onNotice={onNotice}
          />
        </Suspense>
      )}
    </aside>
  );
}
