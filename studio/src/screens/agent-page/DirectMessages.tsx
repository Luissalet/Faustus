import { useCallback, useEffect, useState, type CSSProperties } from 'react';
import { listDms, type DmRow } from '../../adapters/agoraAgent';
import { t } from '../../i18n';
import { agentColor, agentInitials } from '../agora/util';
import './agent-page.css';

export interface DirectMessagesProps {
  space: string;
  /** The slug of the direct conversation that is open, if one is. */
  active?: string | null;
  onOpen: (slug: string) => void;
  /** Change this when something happened in the Agora, so the list reads again. */
  refreshKey?: string | number;
}

/** The direct conversations of the person on this server, for the side bar: who it is with, the last line, what is new. */
export function DirectMessages({ space, active, onOpen, refreshKey }: DirectMessagesProps) {
  const [rows, setRows] = useState<DmRow[]>([]);
  const [failed, setFailed] = useState(false);
  const load = useCallback(async (signal?: AbortSignal) => {
    try { setRows((await listDms(space, signal)).dms); setFailed(false); } catch { if (!signal?.aborted) setFailed(true); }
  }, [space]);
  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load, refreshKey]);
  if (failed && !rows.length) return null;
  return (
    <ul className="fs-agp-dms" aria-label={t('Direct messages')} data-testid="agora-dms">
      {rows.map((row) => (
        <li key={row.slug}>
          <button type="button" data-active={active === row.slug || undefined} onClick={() => onOpen(row.slug)} data-testid="agora-dm"
            style={{ '--agp-c': agentColor(row.with_agent) } as CSSProperties}>
            <span className="fs-agp-dms__avatar" aria-hidden="true">{agentInitials(row.with_agent)}</span>
            <span className="fs-agp-dms__text"><strong>{row.with_agent}</strong>{row.last_text && <small>{row.last_text}</small>}</span>
            {row.unread > 0 && <span className="fs-agp-count" aria-label={t('{n} new', { n: row.unread })}>{row.unread}</span>}
          </button>
        </li>
      ))}
      {!rows.length && <li className="fs-agp-muted">{t('No direct conversations yet. Open an agent and tell it something.')}</li>}
    </ul>
  );
}
