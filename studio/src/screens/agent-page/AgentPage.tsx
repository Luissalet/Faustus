import { BrainCircuit, CalendarClock, GitCommitHorizontal, ListChecks, MessageSquare, RefreshCw, Send, X } from 'lucide-react';
import { useCallback, useEffect, useRef, useState, type CSSProperties } from 'react';
import {
  addMemory, decideProposal, getAgentProfile, removeMemory, setInstructions, tellAgent,
  type AgentMemoryItem, type AgentProfile, type AgentTimelineItem, type InstructionProposal,
} from '../../adapters/agoraAgent';
import { Button, IconButton } from '../../components';
import { t, tn } from '../../i18n';
import { ago, agentColor, agentInitials, PRESENCE_LABEL, RANK_LABEL } from '../agora/util';
import './agent-page.css';

type Tab = 'activity' | 'instructions' | 'memory' | 'routines' | 'usage';
const TABS: { id: Tab; label: string }[] = [
  { id: 'activity', label: 'Activity' }, { id: 'instructions', label: 'Instructions' }, { id: 'memory', label: 'Memory' },
  { id: 'routines', label: 'Routines' }, { id: 'usage', label: 'Usage' },
];
const MEMORY_LABEL: Record<AgentMemoryItem['kind'], string> = { learned: 'Learned', decision: 'Decision', correction: 'Correction', note: 'Note' };
const REFRESH_MS = 30_000;

export interface AgentPageProps {
  space: string; agentId: string; onClose: () => void;
  /** Open the direct conversation (a channel of the server) with this agent. */
  onOpenDm?: (slug: string) => void;
  onOpenTask?: (taskId: number) => void;
}

function stamp(ts: number | null | undefined): string {
  if (!ts) return '';
  return new Date(ts * 1000).toLocaleString(undefined, { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
}

function Proposal({ proposal, busy, onDecide }: { proposal: InstructionProposal; busy: boolean; onDecide: (id: number, decision: 'accept' | 'discard', body?: string) => void }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(proposal.body);
  return (
    <article className="fs-agp-proposal" data-testid="agent-proposal">
      <header>
        <strong>{t('Proposed change to its instructions')}</strong>
        <time dateTime={new Date(proposal.created * 1000).toISOString()}>{stamp(proposal.created)}</time>
      </header>
      <p>{proposal.reason}</p>
      {proposal.source && <p className="fs-agp-muted">{t('Because of: {source}', { source: proposal.source })}</p>}
      {proposal.stale && <p className="fs-agp-muted" role="status">{t('The instructions changed after this was proposed; the difference below is against the current text.')}</p>}
      {editing
        ? <textarea className="fs-field fs-agp-text" value={draft} onChange={(e) => setDraft(e.target.value)} rows={10} aria-label={t('Instructions to accept')} />
        : <pre className="fs-agp-diff" tabIndex={0}>{proposal.diff || proposal.body}</pre>}
      <footer>
        <Button label={editing ? t('Accept with my edit') : t('Accept')} variant="primary" size="sm" disabled={busy} testId="agent-proposal-accept"
          onClick={() => onDecide(proposal.id, 'accept', editing ? draft : undefined)} />
        {!editing && <Button label={t('Edit and accept')} variant="secondary" size="sm" disabled={busy} onClick={() => setEditing(true)} />}
        <Button label={t('Discard')} variant="ghost" size="sm" disabled={busy} testId="agent-proposal-discard" onClick={() => onDecide(proposal.id, 'discard')} />
      </footer>
    </article>
  );
}

function Timeline({ items, onOpenTask }: { items: AgentTimelineItem[]; onOpenTask?: (id: number) => void }) {
  if (!items.length) return <p className="fs-agp-empty">{t('No activity of this agent on this server yet.')}</p>;
  return (
    <ol className="fs-agp-timeline" data-testid="agent-activity">
      {items.map((item, i) => (
        <li key={item.type + String(item.message_id ?? item.task_id ?? i) + i} data-type={item.type}>
          <span className="fs-agp-timeline__icon" aria-hidden="true">
            {item.type === 'message' ? <MessageSquare size={14} /> : item.type === 'delivery' ? <GitCommitHorizontal size={14} /> : <ListChecks size={14} />}
          </span>
          <div>
            <p>
              {item.type === 'delivery' && <strong>{t('Delivery')} · </strong>}
              {item.type === 'task' && <strong>{item.role === 'owner' ? t('Task') : t('Task it opened')} · </strong>}
              {item.task_id && onOpenTask && item.type !== 'message'
                ? <button type="button" className="fs-agp-link" onClick={() => onOpenTask(item.task_id!)}>#{item.task_id} {item.text}</button>
                : item.text}
            </p>
            <small>
              {item.where ? item.where + ' · ' : ''}{item.status ? item.status + ' · ' : ''}
              <time>{stamp(item.at)}</time>
            </small>
            {!!item.commits?.length && <div className="fs-agp-commits">{item.commits.slice(0, 6).map((c) => <code key={c}>{c.slice(0, 8)}</code>)}</div>}
          </div>
        </li>
      ))}
    </ol>
  );
}

function Usage({ data }: { data: AgentProfile['usage'] }) {
  const top = Math.max(1, ...data.days.map((d) => d.messages), ...data.days.map((d) => d.tasks_closed));
  const topRun = Math.max(1, ...data.days.map((d) => d.run_minutes));
  const hasRun = data.days.some((d) => d.run_minutes > 0);
  return (
    <div data-testid="agent-usage">
      <div className="fs-agp-bars" role="img" aria-label={t('Messages per day in the last {n} days', { n: data.days.length })}>
        {data.days.map((d) => (
          <div key={d.day} className="fs-agp-bars__day" title={t('{day}: {m} messages, {c} tasks closed, {r} min running', { day: d.day, m: d.messages, c: d.tasks_closed, r: Math.round(d.run_minutes) })}>
            <span className="fs-agp-bars__bar" style={{ blockSize: `${Math.max(2, Math.round(d.messages / top * 100))}%` } as CSSProperties} />
            {hasRun && <span className="fs-agp-bars__run" style={{ blockSize: `${Math.round(d.run_minutes / topRun * 100)}%` } as CSSProperties} />}
            <small>{d.day.slice(8)}</small>
          </div>
        ))}
      </div>
      <p className="fs-agp-muted">{hasRun ? t('Bars: messages per day; the second tone is the minutes its sessions ran.') : t('Bars: messages per day.')}</p>
      <p className="fs-agp-muted">{data.tokens === null ? t('Tokens are not recorded.') : t('{n} tokens', { n: data.tokens })}</p>
    </div>
  );
}

export function AgentPage({ space, agentId, onClose, onOpenDm, onOpenTask }: AgentPageProps) {
  const [data, setData] = useState<AgentProfile | null>(null);
  const [error, setError] = useState('');
  const [tab, setTab] = useState<Tab>('activity');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const [message, setMessage] = useState('');
  const [correction, setCorrection] = useState(false);
  const [draft, setDraft] = useState<string | null>(null);
  const [noteText, setNoteText] = useState('');
  const [noteKind, setNoteKind] = useState<AgentMemoryItem['kind']>('note');
  const loaded = useRef('');

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const next = await getAgentProfile(space, agentId, signal);
      setData(next);
      setError('');
      loaded.current = agentId;
    } catch (e) {
      if (!signal?.aborted) setError(e instanceof Error ? e.message : String(e));
    }
  }, [space, agentId]);

  useEffect(() => {
    setData(null); setDraft(null); setNotice(''); setTab('activity');
    const controller = new AbortController();
    void load(controller.signal);
    const timer = window.setInterval(() => void load(), REFRESH_MS);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [load]);

  const act = useCallback(async (job: () => Promise<unknown>, done?: string) => {
    setBusy(true);
    try {
      await job();
      if (done) setNotice(done);
      await load();
    } catch (e) {
      setNotice(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [load]);

  const send = () => {
    const text = message.trim();
    if (!text) return;
    void act(async () => {
      const out = await tellAgent(space, agentId, text, correction);
      setMessage(''); setCorrection(false);
      const wake = out.delivery.wake?.status === 'queued';
      setNotice(wake ? t('Sent. It is in its inbox and a turn was queued for it.') : t('Sent. It is in its inbox and in the mentions of its next heartbeat.'));
    });
  };

  const decide = (id: number, decision: 'accept' | 'discard', body?: string) =>
    void act(() => decideProposal(space, id, decision, { body }), decision === 'accept' ? t('Instructions updated.') : t('Proposal discarded.'));

  if (error && !data) {
    return (
      <section className="fs-agp" role="region" aria-label={t('Agent page')}>
        <div className="fs-agp-bar"><IconButton icon={X} label={t('Close')} onClick={onClose} /></div>
        <p className="fs-agp-empty" role="alert" data-testid="agent-page-error">{error}</p>
      </section>
    );
  }
  if (!data) return <section className="fs-agp" aria-busy="true" role="region" aria-label={t('Agent page')}><p className="fs-agp-empty">{t('Loading…')}</p></section>;

  const h = data.profile;
  const isPerson = h.presence === 'person' || h.rank === 'person';
  const color = h.color || agentColor(h.id, isPerson ? h.id : undefined);
  const pending = data.proposals.filter((p) => p.status === 'pending');
  const instructionsText = draft ?? data.instructions.body;
  const engineLine = [h.engine, h.model, h.effort].filter(Boolean).join(' · ');

  return (
    <section className="fs-agp" role="region" aria-label={t('Agent page')} style={{ '--agp-c': color } as CSSProperties} data-testid="agent-page">
      <header className="fs-agp-head">
        <span className="fs-agp-avatar" data-presence={h.presence} aria-hidden="true">{h.initials || agentInitials(h.id)}</span>
        <div className="fs-agp-head__text">
          <h2>{isPerson ? h.name || h.id : h.id}{RANK_LABEL[h.rank] && h.rank !== 'agent' && <span className="fs-agp-rank">{t(RANK_LABEL[h.rank])}</span>}</h2>
          <p>
            <span className="fs-agp-dot" data-presence={h.presence} aria-hidden="true" />
            {t(PRESENCE_LABEL[h.presence as keyof typeof PRESENCE_LABEL] ?? h.presence)}
            {h.since ? ' · ' + (h.since_source === 'session' ? t('session started {when}', { when: ago(h.since) }) : t('seen {when}', { when: ago(h.since) })) : ''}
          </p>
          <p className="fs-agp-muted" data-testid="agent-engine">
            {engineLine || t('Engine not declared')}{h.model_source === 'declared' ? ' · ' + t('declared by the agent') : ''}
          </p>
          {h.doing && <p className="fs-agp-doing">{h.doing}</p>}
        </div>
        <div className="fs-agp-head__actions">
          {data.dm && onOpenDm && <Button label={t('Direct messages')} icon={MessageSquare} variant="secondary" size="sm" onClick={() => onOpenDm(data.dm!.channel)} />}
          <IconButton icon={RefreshCw} label={t('Refresh')} onClick={() => void load()} />
          <IconButton icon={X} label={t('Close')} onClick={onClose} />
        </div>
      </header>

      {!isPerson && (
        <form className="fs-agp-tell" onSubmit={(e) => { e.preventDefault(); send(); }} data-testid="agent-tell">
          <label>
            <span>{t('Tell {agent} something', { agent: h.id })}</span>
            <textarea className="fs-field" rows={2} value={message} onChange={(e) => setMessage(e.target.value)} data-testid="agent-tell-text"
              placeholder={t('It reaches it even while it works: through its inbox and its next heartbeat')}
              onKeyDown={(e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send(); } }} />
          </label>
          <div className="fs-agp-tell__row">
            <label className="fs-agp-check"><input type="checkbox" checked={correction} onChange={(e) => setCorrection(e.target.checked)} />
              <span>{t('It is a correction: ask it to propose a change to its instructions')}</span></label>
            <Button label={t('Send')} icon={Send} variant="primary" size="sm" type="submit" disabled={busy || !message.trim()} testId="agent-tell-send" />
          </div>
        </form>
      )}
      {notice && <p className="fs-agp-notice" role="status" data-testid="agent-notice">{notice}</p>}

      {pending.length > 0 && tab !== 'instructions' && (
        <button type="button" className="fs-agp-banner" onClick={() => setTab('instructions')} data-testid="agent-proposals-banner">
          {tn(pending.length, '{n} proposed change to its instructions is waiting for you', '{n} proposed changes to its instructions are waiting for you')}
        </button>
      )}

      <div className="fs-agp-tabs" role="tablist" aria-label={t('Agent page')}>
        {TABS.map((item) => (
          <button key={item.id} type="button" role="tab" id={`agp-tab-${item.id}`} aria-selected={tab === item.id} aria-controls="agp-panel"
            tabIndex={tab === item.id ? 0 : -1} data-testid={`agent-tab-${item.id}`} onClick={() => setTab(item.id)}
            onKeyDown={(e) => {
              if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
              const at = TABS.findIndex((x) => x.id === tab);
              setTab(TABS[(at + (e.key === 'ArrowRight' ? 1 : TABS.length - 1)) % TABS.length].id);
            }}>
            {t(item.label)}{item.id === 'instructions' && pending.length > 0 && <span className="fs-agp-count">{pending.length}</span>}
          </button>
        ))}
      </div>

      <div id="agp-panel" role="tabpanel" aria-labelledby={`agp-tab-${tab}`} className="fs-agp-panel">
        {tab === 'activity' && <Timeline items={data.activity.timeline} onOpenTask={onOpenTask} />}

        {tab === 'instructions' && (
          <div data-testid="agent-instructions">
            {pending.map((p) => <Proposal key={p.id} proposal={p} busy={busy} onDecide={decide} />)}
            <label className="fs-agp-label" htmlFor="agp-instr">{t('Its instructions')}</label>
            <textarea id="agp-instr" className="fs-field fs-agp-text" rows={12} value={instructionsText} data-testid="agent-instructions-text"
              onChange={(e) => setDraft(e.target.value)} placeholder={t('No instructions of its own yet. What you write here is what this agent works by.')} />
            <div className="fs-agp-tell__row">
              <small className="fs-agp-muted">{data.instructions.updated ? t('Last change {when} by {who}', { when: ago(data.instructions.updated), who: data.instructions.updated_by ?? '' }) : ''}</small>
              <Button label={t('Save instructions')} variant="primary" size="sm" disabled={busy || draft === null || draft === data.instructions.body} testId="agent-instructions-save"
                onClick={() => void act(async () => { await setInstructions(space, agentId, draft ?? ''); setDraft(null); }, t('Instructions saved.'))} />
            </div>
            {data.proposals.some((p) => p.status !== 'pending') && (
              <details className="fs-agp-history">
                <summary>{t('Earlier proposals')}</summary>
                <ul>{data.proposals.filter((p) => p.status !== 'pending').map((p) => (
                  <li key={p.id}><strong>{p.status === 'accepted' ? t('Accepted') : t('Discarded')}</strong> · {p.reason} <time>{stamp(p.decided_at)}</time></li>
                ))}</ul>
              </details>
            )}
          </div>
        )}

        {tab === 'memory' && (
          <div data-testid="agent-memory">
            {!data.memory.length && <p className="fs-agp-empty">{t('Nothing kept yet: decisions it closed, corrections it received and notes appear here.')}</p>}
            <ul className="fs-agp-memory">
              {data.memory.map((m, i) => (
                <li key={(m.id ?? 'd') + '-' + i} data-kind={m.kind}>
                  <span className="fs-agp-tag">{t(MEMORY_LABEL[m.kind])}</span>
                  <p>{m.text}</p>
                  <small>{m.source ? m.source + ' · ' : ''}{m.by} · <time>{stamp(m.at)}</time></small>
                  {m.id !== null && <IconButton icon={X} label={t('Remove note')} onClick={() => void act(() => removeMemory(space, m.id!))} />}
                </li>
              ))}
            </ul>
            <form className="fs-agp-tell__row" onSubmit={(e) => { e.preventDefault(); const text = noteText.trim(); if (text) void act(async () => { await addMemory(space, agentId, text, noteKind); setNoteText(''); }); }}>
              <select className="fs-field" value={noteKind} onChange={(e) => setNoteKind(e.target.value as AgentMemoryItem['kind'])} aria-label={t('Kind of note')}>
                {(Object.keys(MEMORY_LABEL) as AgentMemoryItem['kind'][]).map((k) => <option key={k} value={k}>{t(MEMORY_LABEL[k])}</option>)}
              </select>
              <input className="fs-field" value={noteText} onChange={(e) => setNoteText(e.target.value)} placeholder={t('Add a note to its memory')} aria-label={t('Add a note to its memory')} data-testid="agent-memory-text" />
              <Button label={t('Add')} icon={BrainCircuit} variant="secondary" size="sm" type="submit" disabled={busy || !noteText.trim()} />
            </form>
          </div>
        )}

        {tab === 'routines' && (
          <div data-testid="agent-routines">
            {!data.routines.length && <p className="fs-agp-empty">{t('No routines declared. The agent records its recurring jobs here (hub_agora_agent_routine_set).')}</p>}
            <ul className="fs-agp-routines">
              {data.routines.map((r) => (
                <li key={r.name} data-enabled={r.enabled}>
                  <CalendarClock size={16} aria-hidden="true" />
                  <div><strong>{r.name}</strong><span>{r.schedule || t('no schedule given')}</span>{r.note && <p>{r.note}</p>}</div>
                  <span className="fs-agp-tag">{r.enabled ? t('On') : t('Off')}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {tab === 'usage' && <Usage data={data.usage} />}
      </div>
      {data.usage.note && tab === 'usage' && <p className="fs-agp-muted">{data.usage.note}</p>}
    </section>
  );
}
