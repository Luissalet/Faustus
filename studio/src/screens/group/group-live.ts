import { useEffect, useRef, useState } from 'react';
import { resumeTurn } from '../../adapters/chat';
import { apply, blankTurn, type Turn } from '../studio/model';

export type Running = { stepId: string; sessionId: string; runId: string };

/** Follows each member's live run, exactly as Studio rejoins a running chat. */
export function useLiveSteps(running: Running[]): Record<string, Turn> {
  const [, setTick] = useState(0);
  const turns = useRef<Record<string, Turn>>({});
  const streams = useRef(new Map<string, AbortController>());
  const dirty = useRef(false);
  const key = running.map((r) => `${r.stepId}@${r.sessionId}#${r.runId}`).sort().join('|');
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (dirty.current) {
        dirty.current = false;
        setTick((n) => n + 1);
      }
    }, 150);
    const open = streams.current;
    return () => {
      window.clearInterval(timer);
      for (const controller of open.values()) controller.abort();
    };
  }, []);
  useEffect(() => {
    const wanted = new Map(running.map((r) => [r.stepId, r]));
    for (const [id, controller] of streams.current) {
      if (!wanted.has(id)) {
        controller.abort();
        streams.current.delete(id);
      }
    }
    for (const [stepId, run] of wanted) {
      if (streams.current.has(stepId)) continue;
      const controller = new AbortController();
      streams.current.set(stepId, controller);
      void (async () => {
        for (let attempt = 0; attempt < 80 && !controller.signal.aborted; attempt += 1) {
          let turn: Turn | null = null;
          const attemptController = new AbortController();
          const stop = () => attemptController.abort();
          controller.signal.addEventListener('abort', stop, { once: true });
          try {
            for await (const event of resumeTurn(run.sessionId, {
              signal: attemptController.signal,
              // Another run of that member's chat is not this step's answer.
              onRunId: (id) => { if (id && id !== run.runId) attemptController.abort(); },
            })) {
              turn = apply(turn ?? { ...blankTurn('assistant'), id: stepId }, event);
              // Questions are answered from the room, not inside the reply.
              turns.current[stepId] = { ...turn, ask: undefined };
              dirty.current = true;
            }
          } catch {
            /* the run ended or the stream was cut; the room record has the answer */
          } finally {
            controller.signal.removeEventListener('abort', stop);
          }
          if (turn) return;
          await new Promise((resolve) => window.setTimeout(resolve, 1500));
        }
      })();
    }
  }, [key]); // eslint-disable-line react-hooks/exhaustive-deps
  return turns.current;
}

