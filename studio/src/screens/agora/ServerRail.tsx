import { Compass, Home, Inbox, Plus } from 'lucide-react';
import { useState, type CSSProperties } from 'react';
import { iconUrl, type AgoraServer } from '../../adapters/agora';
import { BrandMark } from '../../shell/BrandMark';
import { t, tn } from '../../i18n';
import { agentInitials } from './util';

function serverHint(server: AgoraServer): string {
  const c = server.counts;
  const bits = [server.title];
  if (server.mentions) bits.push(tn(server.mentions, '{n} mention', '{n} mentions'));
  else if (server.unread) bits.push(tn(server.unread, '{n} unread', '{n} unread'));
  if (c.waiting) bits.push(tn(c.waiting, '{n} thread waits for you', '{n} threads wait for you'));
  if (c.active) bits.push(tn(c.active, '{n} task in progress', '{n} tasks in progress'));
  if (c.review) bits.push(tn(c.review, '{n} in review', '{n} in review'));
  if (server.online) bits.push(tn(server.online, '{n} agent around', '{n} agents around'));
  return bits.join(' · ');
}

/** The picture of a server: its own letter or emoji, the icon of its app, or its initials. */
export function ServerIcon({ server, size = 44 }: { server: Pick<AgoraServer, 'id' | 'title' | 'icon' | 'icon_app' | 'color'>; size?: number }) {
  const [broken, setBroken] = useState(false);
  const style = { '--agx-c': server.color || undefined, inlineSize: size, blockSize: size } as CSSProperties;
  const own = (server.icon || '').trim();
  if (own && own.length <= 4) return <span className="fs-agx-sicon" style={style} data-kind="glyph" aria-hidden="true">{own}</span>;
  const app = own || server.icon_app || '';
  if (app === 'faustus') return <span className="fs-agx-sicon" style={style} data-kind="brand" aria-hidden="true"><BrandMark size={Math.round(size * 0.6)} /></span>;
  if (app && !broken) {
    return <span className="fs-agx-sicon" style={style} data-kind="image" aria-hidden="true">
      <img src={iconUrl(app)} alt="" loading="lazy" onError={() => setBroken(true)} />
    </span>;
  }
  return <span className="fs-agx-sicon" style={style} data-kind="glyph" aria-hidden="true">{agentInitials(server.title)}</span>;
}

export function ServerRail({ servers, current, home, onSelect, onAdd, showHidden, onToggleHidden, onInbox, inboxOpen }: {
  servers: AgoraServer[]; current: string; home: string; onSelect: (id: string) => void; onAdd: () => void;
  showHidden: boolean; onToggleHidden: () => void; onInbox: () => void; inboxOpen: boolean;
}) {
  const waiting = servers.reduce((sum, s) => sum + (s.counts.waiting || 0) + s.mentions, 0);
  const visible = servers.filter((s) => s.id !== home && (showHidden || !s.hidden || s.id === current));
  const hiddenCount = servers.filter((s) => s.hidden).length;
  const homeServer = servers.find((s) => s.id === home);
  const row = (server: AgoraServer, glyph?: 'home') => {
    const attention = server.mentions + (server.counts.waiting || 0);
    return (
      <li key={server.id} className="fs-agx-rail__item" data-current={server.id === current || undefined}
        data-unread={server.unread > 0 || undefined} data-hidden={server.hidden || undefined}>
        <span className="fs-agx-rail__pill" aria-hidden="true" />
        <button type="button" className="fs-agx-rail__btn" title={serverHint(server)} aria-label={serverHint(server)}
          aria-current={server.id === current ? 'page' : undefined} onClick={() => onSelect(server.id)} data-testid={'agora-server-' + server.id}>
          {glyph === 'home'
            ? <span className="fs-agx-sicon" data-kind="home" aria-hidden="true"><Home size={22} /></span>
            : <ServerIcon server={server} />}
          {attention > 0 && <span className="fs-agx-badge" data-tone="alert" aria-hidden="true">{attention > 99 ? '99+' : attention}</span>}
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
