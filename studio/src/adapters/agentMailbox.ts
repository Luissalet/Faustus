/**
 * Adapter for the mailbox between agents (`routes/agent_mailbox_routes.py`,
 * `src/agent_mailbox.py`): messages other agents, sub-agents, workers or outside
 * callers left for this chat.
 *
 * A message from another agent is never the person's own words and never an
 * approval. The transcript shows it apart (sender, urgent badge), and nothing in
 * this file can answer a tool approval or change a setting.
 */
import { asArray, getJson } from './api';

export interface AgentMessageInfo {
  id: string;
  senderKind: 'session' | 'external';
  senderId: string;
  /** Who it came from, as the receiver reads it (chat title or `external:<caller>`). */
  senderLabel: string;
  urgent: boolean;
  hops: number;
  body: string;
}

const text = (value: unknown): string => (typeof value === 'string' ? value : '');

/** One `agent_message` object off the wire (a `steer` event or a saved message's metadata). */
export function agentMessageFrom(raw: unknown): AgentMessageInfo | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const r = raw as Record<string, unknown>;
  const body = text(r.body);
  const senderId = text(r.sender_id);
  if (!body && !senderId) return null;
  const hops = typeof r.hops === 'number' && Number.isFinite(r.hops) ? r.hops : 1;
  return {
    id: text(r.id),
    senderKind: r.sender_kind === 'external' ? 'external' : 'session',
    senderId,
    senderLabel: text(r.sender_label) || senderId || 'agent',
    urgent: r.urgent === true,
    hops,
    body,
  };
}

/** A saved user message that is really another agent's (`metadata.agent_message`). */
export function agentMessageFromMetadata(meta: Record<string, unknown> | undefined): AgentMessageInfo | null {
  if (!meta || meta.source !== 'agent_message') return null;
  return agentMessageFrom(meta.agent_message);
}

export interface MailboxMessage {
  id: string;
  ts: number;
  senderLabel: string;
  senderId: string;
  recipient: string;
  body: string;
  urgent: boolean;
  hops: number;
  state: string;
  reason: string;
}

export interface MailboxView {
  /** Queued: not delivered to a turn yet. */
  waiting: number;
  /** Not marked read (waiting or delivered). */
  unread: number;
  /** Every message the chat was sent that was not refused. */
  total: number;
  messages: MailboxMessage[];
}

export function mailboxViewFrom(raw: unknown): MailboxView {
  const r = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>;
  const num = (v: unknown): number => (typeof v === 'number' && Number.isFinite(v) ? v : 0);
  const messages = asArray<Record<string, unknown>>(r.messages).map((m) => {
    const sender = (m.sender && typeof m.sender === 'object' ? m.sender : {}) as Record<string, unknown>;
    return {
      id: text(m.id),
      ts: num(m.ts),
      senderLabel: text(sender.label) || text(sender.id),
      senderId: text(sender.id),
      recipient: text(m.recipient),
      body: text(m.body),
      urgent: m.urgent === true,
      hops: num(m.hops) || 1,
      state: text(m.state),
      reason: text(m.reason),
    };
  });
  return { waiting: num(r.waiting), unread: num(r.unread), total: num(r.total), messages };
}

export async function fetchMailbox(sessionId: string, signal?: AbortSignal): Promise<MailboxView> {
  const raw = await getJson<unknown>(`/api/agent-mailbox/${encodeURIComponent(sessionId)}/inbox?limit=20`, signal);
  return mailboxViewFrom(raw);
}

export async function markMailboxRead(sessionId: string): Promise<boolean> {
  try {
    const response = await fetch(`/api/agent-mailbox/${encodeURIComponent(sessionId)}/read`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    });
    return response.ok;
  } catch {
    return false;
  }
}
