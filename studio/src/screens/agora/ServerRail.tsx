import { Compass, Home, Inbox, Plus, Scale } from 'lucide-react';
import { useState, type CSSProperties } from 'react';
import { iconUrl, spaceIconUrl, type AgoraServer } from '../../adapters/agora';
import { BrandMark } from '../../shell/BrandMark';
import { t, tn } from '../../i18n';
import { agentInitials, serverColor } from './util';

function serverHint(server: AgoraServer, pending: number): string {
  const c = server.counts;
  const bits = [server.title];
  if (pending) bits.push(t('Pending decisions: {count}', { count: pending }));
  if (server.mentions) bits.push(tn(server.mentions, '{n} mention', '{n} mentions'));
  else if (server.unread) bits.push(tn(server.unread, '{n} unread', '{n} unread'));
  if (c.waiting) bits.push(tn(c.waiting, '{n} thread waits for you', '{n} threads wait for you'));
  if (c.active) bits.push(tn(c.active, '{n} task in progress', '{n} tasks in progress'));
  if (c.review) bits.push(tn(c.review, '{n} in review', '{n} in review'));
  if (server.online) bits.push(tn(server.online, '{n} agent around', '{n} agents around'));
  if (server.muted) bits.push(t('muted'));
  return bits.join(' · ');
}


/**
 * The picture of a server: its own picture, letter or emoji, the icon of its app (by the app it names or, failing
 * that, by its own id) or its initials. The initials are always underneath: while a picture loads, or when it
 * cannot be had, the tile shows the server's letters in its colour instead of an empty circle.
 */
export function ServerIcon({ server, size = 44 }: { server: Pick<AgoraServer, 'id' | 'title' | 'icon' | 'icon_app' | 'color'>; size?: number }) {
  const [broken, setBroken] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const style = {
    '--agx-c': server.color || serverColor(server.id), inlineSize: size, blockSize: size,
    fontSize: Math.round(size * 0.34),
  } as CSSProperties;
  const own = (server.icon || '').trim();
  const letters = <span className="fs-agx-sicon__letters">{agentInitials(server.title || server.id)}</span>;
  const picture = (src: string) => (
    <span className="fs-agx-sicon" style={style} data-kind={loaded ? 'image' : 'glyph'} aria-hidden="true">
      {letters}
      <img src={src} alt="" loading="lazy" data-loaded={loaded || undefined} onLoad={() => setLoaded(true)} onError={() => setBroken(true)} />
    </span>
  );
  if (own.startsWith('img:') && !broken) return picture(spaceIconUrl(server.id, own));
  if (own && !own.startsWith('img:') && own.length <= 4) return <span className="fs-agx-sicon" style={style} data-kind="glyph" aria-hidden="true">{own}</span>;
  const app = (own.startsWith('img:') ? '' : own) || server.icon_app || server.id;
  if (app === 'faustus') return <span className="fs-agx-sicon" style={style} data-kind="brand" aria-hidden="true"><BrandMark size={Math.round(size * 0.6)} /></span>;
  if (app && !broken) return picture(iconUrl(app, true));
  return <span className="fs-agx-sicon" style={style} data-kind="glyph" aria-hidden="true">{letters}</span>;
}

export function ServerRail({ servers, current, home, onSelect, onAdd, showHidden, onToggleHidden, onInbox, inboxOpen, pendingCounts }: {
  servers: AgoraServer[]; current: string; home: string; onSelect: (id: string) => void; onAdd: () => void;
  showHidden: boolean; onToggleHidden: () => void; onInbox: () => void; inboxOpen: boolean; pendingCounts?: Record<string, number>;
}) {
  const waiting = servers.reduce((sum, s) => sum + (s.muted ? 0 : (s.counts.waiting || 0) + s.mentions), 0);
  const visible = servers.filter((s) => s.id !== home && (showHidden || !s.hidden || s.id === current));
  const hiddenCount = servers.filter((s) => s.hidden).length;
  const homeServer = servers.find((s) => s.id === home);
  const row = (server: AgoraServer, glyph?: 'home') => {
    const attention = server.mentions + (server.counts.waiting || 0);
    const pending = pendingCounts?.[server.id] ?? 0;
    const hint = serverHint(server, pending);
    return (
      <li key={server.id} className="fs-agx-rail__item" data-current={server.id === current || undefined}
        data-unread={server.unread > 0 || undefined} data-hidden={server.hidden || undefined} data-muted={server.muted || undefined}>
        <span className="fs-agx-rail__pill" aria-hidden="true" />
        <button type="button" className="fs-agx-rail__btn" title={hint} aria-label={hint}
          aria-current={server.id === current ? 'page' : undefined} onClick={() => onSelect(server.id)} data-testid={'agora-server-' + server.id}>
          {glyph === 'home'
            ? <span className="fs-agx-sicon" data-kind="home" aria-hidden="true"><Home size={22} /></span>
            : <ServerIcon server={server} />}
          {pending > 0 && <span className="fs-agx-rail__decisions" data-testid="agora-server-pending-decisions" aria-hidden="true"><Scale size={12} /></span>}
          {attention > 0 && <span className="fs-agx-badge" data-tone={server.muted ? 'quiet' : 'alert'} aria-hidden="true">{attention > 99 ? '99+' : attention}</span>}
          {attention === 0 && server.online > 0 && <span className="fs-agx-rail__live" aria-hidden="true" />}
        </button>
      </li>
    );
  };
  return (
    <nav className="fs-agx-rail" aria-label={t('Servers')} data-testid="agora-rail">
      <ul>
        {homeServer && row(homeServer, 'home')}
        <li className="fs-agx-rail__item" data-current={inboxOpen || undefined}>
          <span className="fs-agx-rail__pill" aria-hidden="true" />
          <button type="button" className="fs-agx-rail__btn" title={t('What waits for you in every server')}
            aria-label={waiting > 0 ? t('Your inbox') + ' · ' + tn(waiting, '{n} thing waits for you', '{n} things wait for you') : t('Your inbox')} onClick={onInbox} data-testid="agora-inbox-button">
            <span className="fs-agx-sicon" data-kind="home" aria-hidden="true"><Inbox size={20} /></span>
            {waiting > 0 && <span className="fs-agx-badge" data-tone="alert" aria-hidden="true">{waiting > 99 ? '99+' : waiting}</span>}
          </button>
        </li>
        <li className="fs-agx-rail__sep" aria-hidden="true" />
        {visible.map((server) => row(server))}
        <li className="fs-agx-rail__item">
          <button type="button" className="fs-agx-rail__btn fs-agx-rail__btn--action" title={t('Add a server: a new project with its agora')}
            aria-label={t('Add a server')} onClick={onAdd} data-testid="agora-add-server">
            <span className="fs-agx-sicon" data-kind="action" aria-hidden="true"><Plus size={22} /></span>
          </button>
        </li>
        {hiddenCount > 0 && (
          <li className="fs-agx-rail__item">
            <button type="button" className="fs-agx-rail__btn fs-agx-rail__btn--action" aria-pressed={showHidden}
              title={showHidden ? t('Show only the servers you keep on view') : tn(hiddenCount, 'Show {n} hidden server', 'Show {n} hidden servers')}
              aria-label={t('Hidden servers')} onClick={onToggleHidden}>
              <span className="fs-agx-sicon" data-kind="action" aria-hidden="true"><Compass size={20} /></span>
            </button>
          </li>
        )}
      </ul>
    </nav>
  );
}
