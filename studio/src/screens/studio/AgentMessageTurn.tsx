import { Bot, Globe } from 'lucide-react';
import type { AgentMessageInfo } from '../../adapters/agentMailbox';
import { t } from '../../i18n';
import './agent-inbox.css';

const MAX_HOPS = 3;

/**
 * A message another agent (or an outside caller) left for this chat, as it shows
 * in the transcript: «Mensaje de <agente>», an urgent badge and the relay count.
 * It is not the person's own message and it is not an approval, so it has no
 * edit, regenerate or delete actions, and it sits apart from the person's bubble.
 */
export function AgentMessageTurn({ message, id, dbId, enter }: { message: AgentMessageInfo; id: string; dbId?: string; enter?: boolean }) {
  const Icon = message.senderKind === 'external' ? Globe : Bot;
  return (
    <article className="fs-turn fs-turn--agent" data-enter={enter || undefined} data-nav-id={id} data-db-id={dbId} data-testid="turn-agent-message">
      <div className="fs-agentmsg" data-urgent={message.urgent ? 'true' : undefined}>
        <div className="fs-agentmsg__head">
          <Icon size={14} aria-hidden="true" />
          <span className="fs-agentmsg__from">{t('Message from {name}', { name: message.senderLabel })}</span>
          {message.urgent && <span className="fs-agentmsg__badge" data-testid="agent-message-urgent">{t('Urgent')}</span>}
          <span className="fs-agentmsg__hop">{t('Relay {n} of {max}', { n: message.hops, max: MAX_HOPS })}</span>
        </div>
        <div className="fs-agentmsg__body" data-testid="agent-message-body">{message.body}</div>
      </div>
    </article>
  );
}
