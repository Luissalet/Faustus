import { useContext, useEffect, useRef, useState } from 'react';
import { AgoraPersonContext } from '../agora/util';
import { AgentPage } from './AgentPage';
import './agent-page.css';

/** Ask the layer to open an agent's page: from a member card, an avatar, a mention… */
export const AGENT_PAGE_EVENT = 'agora:open-agent';
export function openAgentPage(agentId: string): void {
  window.dispatchEvent(new CustomEvent(AGENT_PAGE_EVENT, { detail: { agentId } }));
}

/** The id to put in `data-agent-id` for an author: agents have a page, the person and the system do not. */
export const pageOf = (author: string | null | undefined, personId?: string): string | undefined =>
  author && author !== personId && author !== 'system' && author !== 'hub' ? author : undefined;

export interface AgentPageLayerProps {
  space: string;
  onOpenDm?: (slug: string) => void;
  onOpenTask?: (taskId: number) => void;
}

/**
 * Shows the page of the agent that was asked for as a panel on the right, over whatever is open. It opens on
 * `openAgentPage(id)` and on a click on any element carrying `data-agent-id` (an avatar, a name, a mention), and
 * closes on Escape or a click outside, giving the focus back to what had it.
 */
export function AgentPageLayer({ space, onOpenDm, onOpenTask }: AgentPageLayerProps) {
  const person = useContext(AgoraPersonContext);
  const [agentId, setAgentId] = useState<string | null>(null);
  const origin = useRef<HTMLElement | null>(null);
  const holder = useRef<HTMLDivElement | null>(null);

  const close = () => { setAgentId(null); origin.current?.focus?.(); };

  useEffect(() => {
    const open = (id: string) => {
      if (!pageOf(id.toLowerCase(), person?.id)) return;
      if (!/^[a-z][a-z0-9_.-]{1,40}$/i.test(id)) return;
      origin.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      setAgentId(id.toLowerCase());
    };
    const onEvent = (e: Event) => open(String((e as CustomEvent).detail?.agentId ?? ''));
    const onClick = (e: MouseEvent) => {
      const target = (e.target as HTMLElement | null)?.closest?.('[data-agent-id]');
      if (target && !target.closest('.fs-agp')) open(target.getAttribute('data-agent-id') ?? '');
    };
    window.addEventListener(AGENT_PAGE_EVENT, onEvent);
    document.addEventListener('click', onClick);
    return () => { window.removeEventListener(AGENT_PAGE_EVENT, onEvent); document.removeEventListener('click', onClick); };
  }, [person?.id]);

  useEffect(() => {
    if (!agentId) return undefined;
    const page = holder.current?.querySelector<HTMLElement>('.fs-agp');
    page?.setAttribute('tabindex', '-1');
    page?.focus();
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') close(); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [agentId]);

  if (!agentId) return null;
  return (
    <div className="fs-agp-layer" ref={holder} onMouseDown={(e) => { if (e.target === e.currentTarget) close(); }} data-testid="agent-page-layer">
      <AgentPage space={space} agentId={agentId} onClose={close}
        onOpenDm={onOpenDm ? (slug) => { close(); onOpenDm(slug); } : undefined}
        onOpenTask={onOpenTask ? (id) => { close(); onOpenTask(id); } : undefined} />
    </div>
  );
}
