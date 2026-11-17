import { useRef, useState } from 'react';
import type { Turn } from '../../screens/studio/model';
import { AgentLoopPanel, useLoopClock } from './AgentLoopPanel';
import { loopOfTurn } from './loop';

/** The loop panel of an assistant turn; nothing for a turn that neither runs nor used a tool. `prompt` is
 *  the message it answers, which the practices of research, files and operations read. */
export function TurnLoop({ turn, prompt = '' }: { turn: Turn; prompt?: string }) {
  const live = turn.streaming;
  const [startedAt] = useState(() => (live ? Date.now() : undefined));
  const now = useLoopClock(live);
  // The model is observing a result until it writes something that is not a separator.
  const mark = useRef({ done: -1, size: 0 });
  const done = turn.steps.filter((s) => s.state !== 'running' && s.state !== 'waiting').length;
  const size = (turn.text + turn.thinking).replace(/\s+/g, '').length;
  if (done !== mark.current.done) mark.current = { done, size };
  if (turn.role !== 'assistant' || (!live && turn.steps.length === 0)) return null;
  return <AgentLoopPanel view={loopOfTurn(turn, startedAt, now, size > mark.current.size, prompt)} live={live} />;
}
