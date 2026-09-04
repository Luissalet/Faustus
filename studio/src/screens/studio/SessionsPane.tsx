import { Bot, MoreHorizontal, Plus, Search, Star } from 'lucide-react';
import { lazy, Suspense, useState } from 'react';
import { Link } from 'react-router';
import { IconButton, Skeleton } from '../../components';
import type { ChatSession } from '../../adapters/chat';
import { relativeTime } from '../../adapters/home';

const SessionDialog = lazy(() => import('./SessionDialog'));

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

/** The conversations column: search, the list, and a "…" per row. */
export function SessionsPane({ sessions, currentId, filter, setFilter, searchRef, onOpen, onChanged, onNotice }: SessionsPaneProps) {
  const [target, setTarget] = useState<ChatSession | null>(null);

  const needle = filter.trim().toLowerCase();
  const visible = needle ? sessions?.filter((s) => `${s.name} ${s.model}`.toLowerCase().includes(needle)) : sessions;

  return (
    <aside className="fs-studio__sessions" aria-label="Conversaciones">
      <div className="fs-studio__sessions-head">
        <span className="fs-panel__label" style={{ margin: 0 }}>
          Conversaciones
        </span>
        <IconButton icon={Plus} label="Nueva conversación" size="sm" onClick={() => onOpen(null)} />
      </div>
      <label className="fs-search fs-studio__search">
        <Search size={14} aria-hidden="true" />
        <input
          ref={searchRef}
          type="search"
          value={filter}
          placeholder="Buscar…"
          aria-label="Buscar conversaciones"
          onChange={(event) => setFilter(event.target.value)}
          data-testid="studio-search"
        />
      </label>

      <div className="fs-studio__list" role="list">
        {!sessions && <Skeleton label="Cargando conversaciones" count={6} height="44px" />}
        {visible?.map((s, i) => (
          <div key={s.id} className="fs-studio__session-row" role="listitem">
            <Link
              to={`/studio?s=${encodeURIComponent(s.id)}`}
              className="fs-studio__session fs-enter"
              style={{ ['--i' as string]: Math.min(i, 8) }}
              aria-current={s.id === currentId ? 'page' : undefined}
              data-testid="studio-session"
              onClick={(event) => {
                event.preventDefault();
                onOpen(s.id);
              }}
            >
              <span className="fs-studio__session-name">
                {s.isImportant && <Star size={11} aria-label="Favorita" className="fs-studio__star" />}
                {s.name}
              </span>
              <span className="fs-studio__session-meta">
                {s.mode === 'agent' && <Bot size={11} aria-label="Agente" />}
                {s.model ? s.model.split('/').pop() : 'sin modelo'}
                {s.lastMessageAt && ` · ${relativeTime(s.lastMessageAt)}`}
              </span>
            </Link>
            <IconButton icon={MoreHorizontal} label={`Acciones de ${s.name}`} size="sm" onClick={() => setTarget(s)} testId="session-menu" />
          </div>
        ))}
        {sessions && sessions.length === 0 && <p className="fs-studio__hint">Todavía no hay conversaciones. La primera la empiezas abajo.</p>}
        {sessions && sessions.length > 0 && visible?.length === 0 && <p className="fs-studio__hint">Ninguna conversación se llama así.</p>}
      </div>

      {target && (
        <Suspense fallback={null}>
          <SessionDialog target={target} currentId={currentId} onClose={() => setTarget(null)} onOpen={onOpen} onChanged={onChanged} onNotice={onNotice} />
        </Suspense>
      )}
    </aside>
  );
}
