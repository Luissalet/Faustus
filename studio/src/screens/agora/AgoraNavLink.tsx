import { MessagesSquare } from 'lucide-react';
import { useEffect, useState } from 'react';
import { NavLink } from 'react-router';
import { getServers } from '../../adapters/agora';
import { t, tn } from '../../i18n';

/**
 * The Agora in the sidebar, next to the other things used every day, with
 * how many things wait for the person across all servers: mentions and the
 * threads that ask for a decision. Looked at once a minute while the window
 * is in view; a hub that is not answering simply shows no count.
 *
 * On a phone it is one of the tabs at the bottom (`tab`), with the count on
 * the icon's corner.
 */
export function AgoraNavLink({ tab = false }: { tab?: boolean }) {
  const [waiting, setWaiting] = useState(0);
  useEffect(() => {
    let alive = true;
    const controller = new AbortController();
    const load = () => {
      if (document.hidden) return;
      getServers(controller.signal)
        .then((listing) => { if (alive) setWaiting(listing.servers.filter((s) => !s.hidden).reduce((n, s) => n + s.mentions + (s.counts.waiting || 0), 0)); })
        .catch(() => { if (alive) setWaiting(0); });
    };
    load();
    const timer = window.setInterval(load, 60000);
    window.addEventListener('focus', load);
    return () => { alive = false; controller.abort(); window.clearInterval(timer); window.removeEventListener('focus', load); };
  }, []);
  const label = t('Agora');
  const title = waiting > 0 ? label + ' · ' + tn(waiting, '{n} thing waits for you', '{n} things wait for you') : label;
  const badge = waiting > 0 && <span className="fs-nav__badge" aria-hidden="true">{waiting > 99 ? '99+' : waiting}</span>;
  if (tab) {
    return (
      <NavLink to="/agora" className="fs-nav__item" title={title} aria-label={title} data-testid="nav-agora">
        <span className="fs-nav__node"><MessagesSquare size={14} aria-hidden="true" />{badge}</span>
        <span className="fs-nav__label">{label}</span>
      </NavLink>
    );
  }
  return (
    <NavLink to="/agora" className="fs-nav__tool" title={title} data-testid="nav-tool-agora">
      <MessagesSquare size={13} aria-hidden="true" />
      <span>{label}</span>
      {badge}
    </NavLink>
  );
}
