import { Hash, Kanban, MessagesSquare, Search, SquarePen, type LucideIcon } from 'lucide-react';
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react';
import type { AgoraGuild, AgoraServer } from '../../adapters/agora';
import { Dialog } from '../../components';
import { t } from '../../i18n';
import { AgentAvatar, roleLine } from './Identity';
import { composerBus, STATUS_LABEL, THREAD_KIND_LABEL, type View } from './util';

interface Hit { key: string; label: string; hint: string; icon?: LucideIcon; agent?: string; go: () => void }

const fold = (text: string) => text.toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '');

/** «Ir a…»: channels, threads, tasks, agents and servers by name, from the keyboard (Ctrl/⌘ + K). */
export function QuickJump({ open, onClose, guild, servers, onOpen, onServer }: {
  open: boolean; onClose: () => void; guild: AgoraGuild; servers: AgoraServer[];
  onOpen: (view: View) => void; onServer: (id: string) => void;
}) {
  const [query, setQuery] = useState('');
  const [pick, setPick] = useState(0);
  const list = useRef<HTMLUListElement>(null);
  useEffect(() => { if (open) { setQuery(''); setPick(0); } }, [open]);

  const all = useMemo<Hit[]>(() => {
    const tasks = Object.values(guild.tasks).flat();
    return [
      ...guild.channels.filter((c) => !c.archived).map((c) => ({ key: 'c' + c.slug, label: '#' + c.title, hint: c.topic || t('Channel'), icon: Hash,
        go: () => onOpen({ kind: 'channel', id: c.slug }) })),
      ...[...guild.waiting, ...guild.threads].map((th) => ({ key: 'h' + th.id, label: th.title, hint: t(THREAD_KIND_LABEL[th.kind] ?? th.kind) + ' · ' + t('Thread'), icon: MessagesSquare,
        go: () => onOpen({ kind: 'thread', id: String(th.id) }) })),
      ...tasks.filter((task) => task.thread_id).map((task) => ({ key: 't' + task.id, label: '#' + task.id + ' ' + task.title,
        hint: t(STATUS_LABEL[task.status] ?? task.status) + (task.owner ? ' · ' + task.owner : ''), icon: SquarePen,
        go: () => onOpen({ kind: 'thread', id: String(task.thread_id) }) })),
      ...guild.members.filter((m) => m.role !== 'person').map((m) => ({ key: 'a' + m.id, label: '@' + m.id, hint: roleLine(m.id, m) + ' · ' + t('mention in the message'), agent: m.id,
        go: () => composerBus.insert('@' + m.id + ' ') })),
      { key: 'v-board', label: t('board'), hint: t('Every task of this server by state'), icon: Kanban, go: () => onOpen({ kind: 'view', id: 'board' }) },
      ...servers.filter((s) => s.id !== guild.space.id).map((s) => ({ key: 's' + s.id, label: s.title, hint: t('Server'), icon: Hash, go: () => onServer(s.id) })),
    ];
  }, [guild, servers, onOpen, onServer]);

  const hits = useMemo(() => {
    const words = fold(query).split(/\s+/).filter(Boolean);
    const found = words.length ? all.filter((hit) => { const text = fold(hit.label + ' ' + hit.hint); return words.every((w) => text.includes(w)); }) : all;
    return found.slice(0, 40);
  }, [all, query]);
  useEffect(() => { setPick(0); }, [query]);
  useEffect(() => { list.current?.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' }); }, [pick]);

  const choose = (hit: Hit | undefined) => { if (!hit) return; onClose(); hit.go(); };
  const onKey = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'ArrowDown') { event.preventDefault(); setPick((p) => Math.min(hits.length - 1, p + 1)); }
    else if (event.key === 'ArrowUp') { event.preventDefault(); setPick((p) => Math.max(0, p - 1)); }
    else if (event.key === 'Enter') { event.preventDefault(); choose(hits[pick]); }
  };

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) onClose(); }} title={t('Go to…')} className="fs-agx-jump" testId="agora-jump">
      <label className="fs-agx-jump__field">
        <Search size={16} aria-hidden="true" />
        <input autoFocus value={query} onChange={(e) => setQuery(e.target.value)} onKeyDown={onKey} placeholder={t('Channels, threads, tasks or agents')}
          aria-label={t('Go to…')} role="combobox" aria-expanded="true" aria-controls="agora-jump-list" aria-activedescendant={hits[pick] ? 'agora-jump-' + hits[pick].key : undefined}
          data-testid="agora-jump-input" />
      </label>
      <ul className="fs-agx-jump__list" id="agora-jump-list" role="listbox" ref={list}>
        {hits.map((hit, index) => {
          const Icon = hit.icon;
          return (
            <li key={hit.key} id={'agora-jump-' + hit.key} role="option" aria-selected={index === pick}>
              <button type="button" tabIndex={-1} onMouseEnter={() => setPick(index)} onClick={() => choose(hit)} data-testid="agora-jump-hit">
                {hit.agent ? <AgentAvatar id={hit.agent} size="sm" /> : Icon && <Icon size={16} aria-hidden="true" />}
                <strong>{hit.label}</strong><span>{hit.hint}</span>
              </button>
            </li>
          );
        })}
        {!hits.length && <li className="fs-agx-jump__empty">{t('Nothing by that name.')}</li>}
      </ul>
    </Dialog>
  );
}
