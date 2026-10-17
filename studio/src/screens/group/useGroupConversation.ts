import { useEffect, useMemo, useRef, useState } from 'react';
import { loadHistory, type HistoryMessage } from '../../adapters/chat';
import { groupSnapshot, openGroup, questionFrom, resumeGroup, sendGroup, stopGroup, type GroupSnapshot } from '../../adapters/group';
import { useLiveSteps } from './group-live';
import { mentionsIn, roomView } from './room-model';
import { t } from '../../i18n';

/** Group execution behind Studio's regular transcript and composer. */
export function useGroupConversation(groupId: string | null) {
  const [snapshot, setSnapshot] = useState<GroupSnapshot | null>(null);
  const [history, setHistory] = useState<HistoryMessage[]>([]);
  const [error, setError] = useState('');
  const signature = useRef('');
  const visibleGroup = useRef(groupId);
  visibleGroup.current = groupId;
  useEffect(() => {
    setSnapshot(null); setHistory([]); setError(''); signature.current = '';
    if (!groupId) return;
    const controller = new AbortController();
    let timer: number | undefined;
    let first = true;
    const read = async () => {
      try {
        const current = await (first ? openGroup(groupId, controller.signal) : groupSnapshot(groupId, controller.signal));
        first = false;
        if (controller.signal.aborted) return;
        setSnapshot(current); setError('');
        const finished = current.steps.filter((step) => step.status !== 'queued' && step.status !== 'running');
        const mark = current.turns.map((turn) => turn.id + ':' + turn.status).join(',') + '|' + finished.map((step) => step.id + ':' + step.status).join(',');
        if (signature.current !== mark || !signature.current) {
          const record = await loadHistory(groupId, controller.signal);
          if (!controller.signal.aborted) {
            setHistory(record.history);
            const recorded = new Set(record.history.map((message) => String(message.metadata.group_step_id ?? '')));
            if (finished.every((step) => step.status === 'stopped' || recorded.has(step.id))) signature.current = mark;
          }
        }
      } catch (failure) {
        if (!controller.signal.aborted) setError((failure as Error).message);
      } finally {
        if (!controller.signal.aborted) timer = window.setTimeout(read, 1000);
      }
    };
    void read();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [groupId]);
  const current = snapshot?.group.parentId === groupId ? snapshot : null;
  const running = useMemo(() => (current?.steps ?? []).filter((step) => step.status === 'running' && step.detail.run_id)
    .map((step) => ({stepId: step.id, sessionId: step.member_id, runId: String(step.detail.run_id)})), [current]);
  const live = useLiveSteps(running);
  const view = roomView(current ? history : [], current, live);
  return { snapshot: current, group: current?.group ?? null, turns: view.turns, busy: current?.busy ?? false, error,
    ask: current?.steps.map(questionFrom).find(Boolean) ?? null,
    async send(message: string, executionOptions: Record<string, unknown>, mode: 'chat' | 'agent', workspace: string, requestId: string) {
      if (!groupId) return;
      if (!current) throw new Error(t('Loading the conversation'));
      const result = await sendGroup(groupId, message, requestId, mentionsIn(message, current.group.participants),
        {executionOptions, executionMode: mode, workspace});
      const updated = await groupSnapshot(groupId);
      if (visibleGroup.current === groupId) setSnapshot(updated);
      return result;
    },
    async stop() {
      if (groupId) { await stopGroup(groupId); const updated = await groupSnapshot(groupId); if (visibleGroup.current === groupId) setSnapshot(updated); }
    },
    async resume(message: string, answer: Record<string, unknown>) {
      if (groupId) { await resumeGroup(groupId, message, answer); const updated = await groupSnapshot(groupId); if (visibleGroup.current === groupId) setSnapshot(updated); }
    },
  };
}
