import { Mail } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { fetchMailbox, markMailboxRead, type MailboxView } from '../../adapters/agentMailbox';
import { Popover } from '../../components';
import { t, useLang } from '../../i18n';
import './agent-inbox.css';

const POLL_MS = 8000;

function stateLabel(state: string): string {
  if (state === 'queued') return t('Waiting for the next turn');
  if (state === 'delivered') return t('Delivered');
  if (state === 'read') return t('Marked as read');
  return state;
}

/**
 * The chat's mailbox in the Studio header: how many messages other agents (or
 * outside callers) left for this conversation, which of them still wait for its
 * next turn, and who sent them.
 *
 * Messages that were delivered show in the transcript as «Mensaje de <agente>»;
 * this chip is only the inbox view. It polls lightly while the chat is open and,
 * when a message arrived while nothing was running (an urgent one wakes the chat
 * and the turn happens without this tab), asks the screen to reload the history.
 * Nothing here can answer a tool approval or change a setting.
 */
export function AgentInbox({ sessionId, busy, onNew }: { sessionId: string | null; busy: boolean; onNew: () => void }) {
  useLang();
  const [view, setView] = useState<MailboxView | null>(null);
  const known = useRef<number | null>(null);
  const busyRef = useRef(busy);
  busyRef.current = busy;
  const onNewRef = useRef(onNew);
  onNewRef.current = onNew;

  const load = useCallback(async (signal?: AbortSignal) => {
    if (!sessionId) return;
    try {
      const next = await fetchMailbox(sessionId, signal);
      if (signal?.aborted) return;
      setView(next);
      if (known.current === null) {
        known.current = next.total;
      } else if (next.total > known.current && !busyRef.current) {
        known.current = next.total;
        onNewRef.current();
      }
    } catch {
      /* the chip is optional: a failed read leaves it as it was */
    }
  }, [sessionId]);

  useEffect(() => {
    known.current = null;
    setView(null);
    if (!sessionId) return undefined;
    const controller = new AbortController();
    void load(controller.signal);
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void load(controller.signal);
    }, POLL_MS);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [sessionId, load]);

  if (!sessionId || !view || view.total === 0) return null;
  const title = t('Messages from other agents: {unread} unread, {waiting} waiting', { unread: view.unread, waiting: view.waiting });
  return (
    <Popover
      testId="agent-inbox-popover"
      align="end"
      className="fs-agentbox__popover"
      trigger={(
        <button type="button" className="fs-agentbox" data-unread={view.unread > 0 ? 'true' : undefined}
          title={title} aria-label={title} data-testid="agent-inbox-chip">
          <Mail size={13} aria-hidden="true" />
          <span className="fs-agentbox__count">{view.unread > 0 ? view.unread : view.total}</span>
        </button>
      )}
    >
      <div className="fs-agentbox__panel" data-testid="agent-inbox-panel">
        <p className="fs-agentbox__heading">{t('Agent messages')}</p>
        <p className="fs-agentbox__note">{t('Not an approval: other agents cannot answer approvals or change settings.')}</p>
        <ul className="fs-agentbox__list">
          {view.messages.map((m) => (
            <li key={m.id} className="fs-agentbox__item" data-urgent={m.urgent ? 'true' : undefined}>
              <div className="fs-agentbox__meta">
                <b>{m.senderLabel}</b>
                {m.urgent && <span className="fs-agentbox__badge">{t('Urgent')}</span>}
                <span className="fs-agentbox__state">{stateLabel(m.state)}</span>
              </div>
              <p className="fs-agentbox__body">{m.body}</p>
            </li>
          ))}
        </ul>
        {view.unread > 0 && (
          <button type="button" className="fs-agentbox__mark" data-testid="agent-inbox-mark-read"
            onClick={() => { void markMailboxRead(sessionId).then(() => load()); }}>
            {t('Mark all as read')}
          </button>
        )}
      </div>
    </Popover>
  );
}
