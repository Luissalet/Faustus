import { ExternalLink, Kanban, MessageSquare, PanelRight, Plus, Send, Settings2, Square, Trash2, Users, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router';
import { Button, IconButton, Menu, Popover, Skeleton } from '../../components';
import { acknowledgeCoordinationAttempt, coordinationAttempt, useCoordinationDraft } from '../../components/coordinationNavigation';
import { loadHistory, resumeTurn, type HistoryMessage } from '../../adapters/chat';
import {
  archiveGroup, DEFAULT_DISCUSSION_ROUNDS, groupSnapshot, openGroup, questionFrom, resumeGroup, sendGroup, speakerName,
  stopGroup, updateGroup, type GroupSnapshot, type GroupState,
} from '../../adapters/group';
import { apply, blankTurn, type Turn } from '../studio/model';
import { AskCard, Transcript } from '../studio/Transcript';
import { Vitals } from '../studio/Vitals';
import { SparkVitals } from '../studio/SparkVitals';
import { hueIndex, initials } from '../../lib/mail';
import { t, tn } from '../../i18n';
import { AgoraBoardView } from './AgoraBoard';
import { ROUND_CHOICES } from './RoomSetup';
import { mentionQuery, mentionsIn, roomView, shortHandle } from './room-model';
import { MemberActivity } from './MemberActivity';
import '../studio.css';
import '../group.css';

/**
 * A room of the Agora as a group chat: the person and several models in one
 * conversation, drawn by Studio's own Transcript (tool rail, thinking, live
 * progress), with who is speaking now, the project's board beside it and a
 * composer that addresses members with @name.
 */

type Running = { stepId: string; sessionId: string; runId: string };

/** Follows each member's live run, exactly as Studio rejoins a running chat. */
function useLiveSteps(running: Running[]): Record<string, Turn> {
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

function phaseText(turn: Turn | undefined): string {
  const live = turn?.live;
  if (!live) return t('Speaking');
  if (live.phase === 'tool') return live.label ? t('Using {tool}', { tool: live.label }) : t('Running a tool');
  if (live.phase === 'thinking') return t('Thinking');
  if (live.phase === 'writing') return t('Writing');
  return live.label || t('Waiting for the model');
}

export function RoomChat({ groupId, onNewRoom, onManage, onClosed, say, projectId, conversationsOpen, onToggleConversations }: {
  groupId: string; onNewRoom: () => void; onManage: (state: GroupState) => void; onClosed: () => void;
  say: (text: string, tone?: 'ok' | 'warn') => void; projectId?: string; conversationsOpen?: boolean; onToggleConversations?: () => void;
}) {
  const navigate = useNavigate();
  const [snapshot, setSnapshot] = useState<GroupSnapshot | null>(null);
  const [history, setHistory] = useState<HistoryMessage[]>([]);
  const [connectionError, setConnectionError] = useState('');
  const [draft, setDraft] = useCoordinationDraft('agora:draft:room:' + groupId, '');
  const [recipient, setRecipient] = useState('');
  const [sending, setSending] = useState(false);
  const [linkingProject, setLinkingProject] = useState(false);
  const [side, setSide] = useState<'board' | 'members' | null>(null);
  const [caret, setCaret] = useState(0);
  const [pick, setPick] = useState(0);
  const signature = useRef('');
  const scrollRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const pinned = useRef(true);

  useEffect(() => {
    const controller = new AbortController();
    let timer: number | undefined;
    let first = true;
    signature.current = '';
    setSnapshot(null);
    setHistory([]);
    setConnectionError('');
    const refresh = async () => {
      try {
        const current = first ? await openGroup(groupId, controller.signal) : await groupSnapshot(groupId, controller.signal);
        first = false;
        if (controller.signal.aborted) return;
        // Controls and observed member state do not wait for a large history.
        setSnapshot(current);
        setConnectionError('');
        const finished = current.steps.filter((s) => s.status !== 'queued' && s.status !== 'running');
        const next = current.turns.map((x) => x.id + ':' + x.status).join(',') + '|' + finished.map((s) => s.id + ':' + s.status).join(',');
        if (next !== signature.current) {
          const loaded = await loadHistory(groupId, controller.signal).catch(() => null);
          if (loaded && !controller.signal.aborted) {
            setHistory(loaded.history);
            // Re-read until every finished answer is in the room's record.
            const recorded = new Set(loaded.history.map((m) => String(m.metadata.group_step_id ?? '')));
            if (finished.every((s) => s.status === 'stopped' || recorded.has(s.id))) signature.current = next;
          }
        }
      } catch (error) {
        if (!controller.signal.aborted) setConnectionError((error as Error).message);
      } finally {
        if (!controller.signal.aborted) timer = window.setTimeout(() => void refresh(), 1000);
      }
    };
    void refresh();
    // Leaving only stops watching; the room keeps working on the server.
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [groupId]);

  const running = useMemo<Running[]>(() => (snapshot?.steps ?? [])
    .filter((s) => s.status === 'running' && s.detail.run_id)
    .map((s) => ({ stepId: s.id, sessionId: s.member_id, runId: String(s.detail.run_id) })), [snapshot]);
  const live = useLiveSteps(running);
  const view = roomView(history, snapshot, live);
  const state = snapshot?.group ?? null;
  const members = useMemo(() => state?.participants ?? [], [state]);
  const busy = snapshot?.busy ?? false;
  const pending = snapshot?.steps.find((s) => questionFrom(s));
  const ask = pending ? questionFrom(pending) : null;
  const rounds = state?.discussionRounds ?? DEFAULT_DISCUSSION_ROUNDS;
  const spaceId = state?.space_id || '';

  // Stay at the latest message while the reader is at the bottom.
  const mark = view.turns.map((x) => x.id + ':' + x.text.length + ':' + x.steps.length).join('|');
  useEffect(() => {
    const el = scrollRef.current;
    if (el && pinned.current) el.scrollTop = el.scrollHeight;
  }, [mark]);

  const speaking = members.filter((m) => view.activity[m.sessionId]?.state === 'speaking');
  const next = members.filter((m) => view.activity[m.sessionId]?.state === 'next');
  const status = ask ? t('Waiting for your answer')
    : speaking.length ? speaking.map((m) => `${speakerName(m)}: ${phaseText(live[view.activity[m.sessionId]?.stepId ?? ''])}`).join(' · ')
      + (next.length ? ' · ' + t('next: {names}', { names: next.map(speakerName).join(', ') }) : '')
      : busy ? t('Working. New messages join the queue; you can leave and return.') : '';

  const query = mentionQuery(draft, caret);
  const suggestions = query ? members.filter((m) => speakerName(m).toLowerCase().includes(query.query) || shortHandle(m, members).startsWith(query.query)) : [];
  const insertMention = (index: number) => {
    const member = suggestions[index];
    if (!member || !query) return;
    const handle = '@' + shortHandle(member, members) + ' ';
    const value = draft.slice(0, query.start) + handle + draft.slice(caret);
    setDraft(value);
    const at = query.start + handle.length;
    setCaret(at);
    requestAnimationFrame(() => textareaRef.current?.setSelectionRange(at, at));
  };

  const send = async () => {
    const message = draft.trim();
    if (!message || !state || sending) return;
    const mentions = recipient ? [recipient] : mentionsIn(message, members);
    const scope = 'groups:' + state.space_id + ':' + state.parentId;
    const attempt = coordinationAttempt(scope, message, mentions.join(','));
    setSending(true);
    pinned.current = true;
    try {
      const answer = await sendGroup(state.parentId, message, attempt.id, mentions);
      if (!answer?.turn_id) throw new Error(t('Message not confirmed. Retry the same send.'));
      acknowledgeCoordinationAttempt(scope, attempt.id);
      setDraft((current) => (current.trim() === message ? '' : current));
    } catch (error) {
      say((error as Error).message, 'warn');
    } finally {
      setSending(false);
    }
  };
  const stop = async () => {
    if (!state) return;
    try { await stopGroup(state.parentId); } catch (error) { say((error as Error).message, 'warn'); }
  };
  const answer = async (message: string, fields: Record<string, unknown>) => {
    if (!state || sending) return;
    setSending(true);
    try { await resumeGroup(state.parentId, message, fields); }
    catch (error) { say((error as Error).message, 'warn'); }
    finally { setSending(false); }
  };
  const close = async () => {
    if (!state) return;
    try {
      await archiveGroup(state.parentId);
      say(t('Group closed. Its transcript stays in Studio.'));
      onClosed();
    } catch (error) {
      say((error as Error).message, 'warn');
    }
  };
  const setRounds = useCallback(async (value: number) => {
    if (!state) return;
    try {
      const updated = await updateGroup(state, { discussionRounds: value });
      setSnapshot((current) => (current ? { ...current, group: updated } : current));
    } catch (error) {
      say((error as Error).message, 'warn');
    }
  }, [state, say]);

  const linkProject = async () => {
    if (!state || !projectId || linkingProject) return;
    setLinkingProject(true);
    try {
      const group = await updateGroup(state, { projectId });
      setSnapshot((current) => current ? { ...current, group } : current);
    } catch (error) { say((error as Error).message, 'warn'); }
    finally { setLinkingProject(false); }
  };

  if (!state) {
    return (
      <div className="fs-room fs-room--loading">
        {connectionError ? <p className="fs-notice" data-tone="danger" role="alert">{connectionError}</p> : <Skeleton label={t('Opening group')} count={3} height="56px" />}
      </div>
    );
  }

  return (
    <div className="fs-room" data-side={side || undefined} data-testid="room-chat">
      <section className="fs-room__main" aria-label={state.name}>
        <header className="fs-room__head">
          {onToggleConversations && <button type="button" className="fs-studio__chip" aria-expanded={conversationsOpen} onClick={onToggleConversations} title={t('Show or hide the conversations (Ctrl+B)')}><MessageSquare size={13} aria-hidden="true" /><span className="fs-room__conversations-label">{t('Conversations')}</span></button>}
          <div className="fs-room__title">
            <h2 title={state.name}>{state.name}</h2>
            <p>
              {state.mode === 'parallel' ? t('All at once') : t('Taking turns')} · {tn(members.length, '{n} participant', '{n} participants')}
              {state.executionMode === 'chat' ? ' · ' + t('Discuss without tools') : ''}
            </p>
          </div>
          <Popover trigger={<IconButton icon={Settings2} label={t('Room settings')} size="sm" />}>
          <p className="fs-muted">{state.mode === 'parallel' ? t('All at once') : t('Taking turns')} · {tn(members.length, '{n} participant', '{n} participants')}</p>
          <label className="fs-room__rounds" title={t('How many rounds the members may answer each other after your message')}>
            <span>{t('Between themselves')}</span>
            <select className="fs-field" value={rounds} onChange={(e) => void setRounds(Number(e.target.value))} data-testid="room-rounds">
              {[...new Set([...ROUND_CHOICES, rounds])].sort((a, b) => a - b).map((n) => <option key={n} value={n}>{n < 0 ? t('Until the discussion ends') : n === 0 ? t('Only answer me') : t('Up to {n} rounds', { n })}</option>)}
            </select>
          </label>
          </Popover>
          <Vitals busy={busy} />
          <SparkVitals />
          <IconButton icon={Kanban} label={side === 'board' ? t('Hide the board') : t('Show the board')} size="sm" onClick={() => setSide(side === 'board' ? null : 'board')} testId="room-board-toggle" />
          <IconButton icon={PanelRight} label={side === 'members' ? t('Hide the members') : t('Show the members')} size="sm" onClick={() => setSide(side === 'members' ? null : 'members')} />
          <Menu
            trigger={<IconButton size="sm" icon={Users} label={t('Room')} />}
            align="end"
            items={[
              { label: t('Manage members'), icon: Users, onSelect: () => onManage(state) },
              { label: t('Transcript in Studio'), icon: ExternalLink, onSelect: () => navigate(`/studio?s=${encodeURIComponent(state.parentId)}`) },
              { label: t('New room'), icon: Plus, onSelect: onNewRoom },
              { label: t('Close this group (keep the transcript)'), icon: Trash2, variant: 'danger', onSelect: () => void close() },
            ]}
          />
        </header>

        {projectId && !state.projectId && <div className="fs-notice fs-room__notice">
          <p>{t('This room is not linked to this project yet. Link it to use its instructions, folders and task board in future turns.')}</p>
          <Button size="sm" label={t('Work in this project')} loading={linkingProject} onClick={() => void linkProject()} testId="room-link-project" />
        </div>}

        {snapshot && <MemberActivity snapshot={snapshot} connected={!connectionError} onSelect={() => setSide('members')} />}

        {connectionError && <p className="fs-notice fs-room__notice" data-tone="danger" role="alert">{connectionError} {t('Reconnecting…')}</p>}

        <div className="fs-room__stage fs-studio__transcript-stage">
          <div className="fs-studio__scroll" ref={scrollRef} data-testid="room-transcript"
            onScroll={(e) => { const el = e.currentTarget; pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80; }}>
            {view.turns.length === 0 ? (
              <div className="fs-room__empty">
                <MessageSquare size={20} aria-hidden="true" />
                <p>{t('Say something to the table.')}</p>
                <p className="fs-muted">{t('Everyone answers; mention one with @name to talk to them. They answer each other the same way.')}</p>
              </div>
            ) : (
              <Transcript
                turns={view.turns}
                busy={busy}
                sessionId={null}
                projectId={projectId || state.projectId}
                onApproval={() => undefined}
                onAnswer={() => undefined}
                onEdit={() => undefined}
                onRegenerate={() => undefined}
                onDelete={() => undefined}
                onNotice={(text, tone) => say(text, tone === 'info' ? 'ok' : 'warn')}
                onQuote={(text) => setDraft((current) => (current ? current + '\n\n' : '') + '> ' + text.replace(/\n/g, '\n> ') + '\n\n')}
              />
            )}
          </div>
        </div>

        {ask && (
          <div className="fs-room__ask">
            <AskCard ask={ask} busy={sending}
              onAnswer={(text, optionIds) => void answer(text, { question_id: ask.questionId, question_revision: ask.revision, option_ids: optionIds })}
              onApproval={(decision) => void answer('', { tool_approval_id: ask.approvalId, tool_approval_decision: decision })} />
          </div>
        )}

        <footer className="fs-room__composer">
          {suggestions.length > 0 && (
            <div className="fs-room__mentions" role="listbox" aria-label={t('Mention a member')}>
              {suggestions.map((m, i) => (
                <button key={m.sessionId} type="button" role="option" aria-selected={i === pick} className="fs-room__mention"
                  onMouseDown={(e) => { e.preventDefault(); insertMention(i); }}>
                  <span className="fs-avatar" data-hue={hueIndex(speakerName(m))} data-size="sm" aria-hidden="true">{initials(speakerName(m))}</span>
                  {speakerName(m)} <small>@{shortHandle(m, members)}</small>
                </button>
              ))}
            </div>
          )}
          <textarea
            ref={textareaRef}
            className="fs-room__input"
            aria-label={t('Message to the group')}
            rows={2}
            value={draft}
            placeholder={t('Talk to the group… @name to address one')}
            onChange={(e) => { setDraft(e.target.value); setCaret(e.target.selectionStart ?? e.target.value.length); setPick(0); }}
            onSelect={(e) => setCaret(e.currentTarget.selectionStart ?? 0)}
            onKeyDown={(e) => {
              if (suggestions.length) {
                if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
                  e.preventDefault();
                  setPick((p) => (p + (e.key === 'ArrowDown' ? 1 : suggestions.length - 1)) % suggestions.length);
                  return;
                }
                if (e.key === 'Enter' || e.key === 'Tab') {
                  e.preventDefault();
                  insertMention(pick);
                  return;
                }
              }
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                void send();
              }
            }}
            data-testid="group-draft"
          />
          <div className="fs-room__composer-row">
            <p className="fs-room__status" role="status">{status}</p>
            <label className="fs-room__to">{t('To')}
              <select className="fs-field" value={recipient} onChange={(event) => setRecipient(event.target.value)} aria-label={t('Message recipients')}>
                <option value="">{t('Everyone (or whoever you @mention)')}</option>
                {members.map((p) => <option key={p.sessionId} value={p.sessionId}>{speakerName(p)}</option>)}
              </select>
            </label>
            {busy && <Button variant="danger" size="sm" icon={Square} label={t('Stop')} onClick={() => void stop()} />}
            <Button variant="primary" size="sm" icon={Send} label={busy ? t('Queue message') : t('Send')} loading={sending} disabled={!draft.trim() || sending} onClick={() => void send()} testId="group-send" />
          </div>
        </footer>
      </section>

      {side && (
        <aside className="fs-room__side" aria-label={side === 'board' ? t('Agora tasks') : t('Participants')}>
          <div className="fs-room__side-tabs">
            <div role="tablist" aria-label={t('Room')}>
            <button type="button" role="tab" aria-selected={side === 'board'} onClick={() => setSide('board')}><Kanban size={13} aria-hidden="true" /> {t('Board')}</button>
            <button type="button" role="tab" aria-selected={side === 'members'} onClick={() => setSide('members')}><Users size={13} aria-hidden="true" /> {t('Members')}</button>
            </div>
            <IconButton icon={X} label={t('Close')} size="sm" onClick={() => setSide(null)} />
          </div>
          {side === 'board' && (spaceId
            ? <AgoraBoardView spaceId={spaceId} variant="compact" />
            : <p className="fs-muted">{t('This room has no agora board.')}</p>)}
          {side === 'members' && (
            <ul className="fs-room__roster">
              {members.map((m) => {
                const act = view.activity[m.sessionId] ?? { state: 'idle' };
                return (
                  <li key={m.sessionId} className="fs-room__roster-row" data-state={act.state}>
                    <span className="fs-avatar" data-hue={hueIndex(speakerName(m))} aria-hidden="true">{initials(speakerName(m))}</span>
                    <div>
                      <b>{speakerName(m)}</b>
                      <small>{m.route.model}{m.route.endpointName ? ' · ' + m.route.endpointName : ''}</small>
                      <small>{m.autonomyPreset === 'read_only' ? t('Read only') : m.autonomyPreset === 'bounded_autonomous' ? t('Autonomous') : m.autonomyPreset === 'supervised' ? t('Supervised') : t('Project defaults')} · @{shortHandle(m, members)}</small>
                      <Link className="fs-link" to={`/studio?s=${encodeURIComponent(m.sessionId)}`}>{t('Open its own chat (every step)')}</Link>
                    </div>
                  </li>
                );
              })}
              <li><Button variant="ghost" size="sm" icon={Users} label={t('Manage members')} onClick={() => onManage(state)} /></li>
            </ul>
          )}
        </aside>
      )}
    </div>
  );
}
