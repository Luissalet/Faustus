import { useEffect, useRef, useState } from 'react';
import type { Turn } from '../../screens/studio/model';
import { AgentLoopPanel, useLoopClock } from './AgentLoopPanel';
import { loopOfTurn } from './loop';

let authorRequest: Promise<string | undefined> | undefined;

function loadGitAuthor() {
  authorRequest ??= fetch('/api/threads/agents/commit-author', { credentials: 'same-origin' })
    .then(async response => {
      if (!response.ok) throw new Error('Commit author unavailable');
      const result = await response.json() as { author?: string };
      return result.author;
    }).catch(() => { authorRequest = undefined; return undefined; });
  return authorRequest;
}

/** The loop panel of an assistant turn; nothing for a turn that neither runs nor used a tool. `prompt` is
 *  the message it answers, which the practices of research, files and operations read. */
export function TurnLoop({ turn, prompt = '' }: { turn: Turn; prompt?: string }) {
  const live = turn.streaming;
  const [author, setAuthor] = useState<string>();
  const hasCommit = turn.steps.some(step => /\bgit\b.*\bcommit\b/i.test(step.command || step.label || ''));
  useEffect(() => {
    if (!hasCommit) return;
    let active = true;
    void loadGitAuthor().then(value => { if (active) setAuthor(value); });
    return () => { active = false; };
  }, [hasCommit]);
  const [startedAt] = useState(() => (live ? Date.now() : undefined));
  const now = useLoopClock(live);
  // The model is observing a result until it writes something that is not a separator.
  const mark = useRef({ done: -1, size: 0 });
  const done = turn.steps.filter((s) => s.state !== 'running' && s.state !== 'waiting').length;
  const size = (turn.text + turn.thinking).replace(/\s+/g, '').length;
  if (done !== mark.current.done) mark.current = { done, size };
  if (turn.role !== 'assistant' || (!live && turn.steps.length === 0)) return null;
  return <AgentLoopPanel view={loopOfTurn(turn, startedAt, now, size > mark.current.size, prompt, author)} live={live} />;
}
