import { AtSign, ChevronDown, ChevronRight, Lock, MoreHorizontal, Plus, SlidersHorizontal, Square, UserMinus, UserPlus } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import {
  getConsole, getMembers, setMemberRank, stopWorker,
  type AgoraGuild, type AgoraMember, type ConsoleLeader, type ConsoleWorker, type QuotaProvider,
} from '../../adapters/agora';
import { Button, IconButton, Menu, Popover } from '../../components';
import { t, tn } from '../../i18n';
import { AddAgentDialog, AgentModelsDialog, effortLabel, failureWhy, modelLine, PruneAgentsDialog, RankOptions, RemoveAgentDialog } from './MemberDialogs';
import {
  ago, agentColor, agentInitials, composerBus, DEFAULT_RANK, findTask, isOnline, PRESENCE_LABEL, RANK_LABEL, ROLE_COLORS, ROLE_LABEL, ROLE_ORDER,
  STATUS_LABEL, type View,
} from './util';

function quotaOf(providers: QuotaProvider[], role: string): { free: number | null; reset: string } | null {
  const provider = providers.find((p) => p.provider_id === role);
  const windows = (provider?.pools ?? []).flatMap((pool) => (pool.stale ? [] : pool.windows ?? [])).filter((w) => !w.stale && w.remaining_percent !== null);
  if (!provider) return null;
  if (!windows.length) return { free: null, reset: '' };
  const tight = windows.reduce((a, b) => ((a.remaining_percent ?? 100) <= (b.remaining_percent ?? 100) ? a : b));
  const when = tight.reset_at ? new Date(typeof tight.reset_at === 'number' ? tight.reset_at * 1000 : tight.reset_at) : null;
  return { free: Math.round(tight.remaining_percent ?? 0), reset: when && !Number.isNaN(when.getTime()) ? when.toLocaleString(undefined, { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }) : '' };
}

const OBSERVED_LABEL: Record<string, string> = { working: 'At work', tool: 'Using a tool', waiting: 'Waiting', idle: 'Idle', stale: 'Gone quiet' };

/** A session the hub started from here, with the model it runs on as the person reads it. */
interface Started { worker: ConsoleWorker; name: string }
/** Keep each source together: a declaration must not fill gaps in a verified session. */
function memberModel(member: AgoraMember, started?: Started) {
  const verified = !!(started?.worker.model || started?.worker.effort);
  const model = verified ? started?.worker.model : member.declared_model;
  const effort = verified ? started?.worker.effort : member.declared_effort;
  const name = verified && model ? started?.name || model : modelLine(undefined, model, undefined);
  return { name, line: model || effort ? (name || t('not declared')) + ' · ' + (effort ? effortLabel(effort) : t('not declared')) : t('not declared'),
    declared: !verified && !!(model || effort) };
}
interface CardProps {
  member: AgoraMember; guild: AgoraGuild; onOpen: (view: View) => void; onRemove: (member: AgoraMember) => void;
  started?: Started; onStop: (worker: ConsoleWorker) => void; onRank: (member: AgoraMember, rank: string) => void;
}

/** The rank of a member next to its name, when it is not the one everybody has. */
function RankMark({ member }: { member: AgoraMember }) {
  const rank = member.rank ?? DEFAULT_RANK;
  if (member.role === 'person' || rank === DEFAULT_RANK || !RANK_LABEL[rank]) return null;
  return <span className="fs-agx-rank" data-rank={rank}>{t(RANK_LABEL[rank])}</span>;
}

function MemberCard({ member, guild, onOpen, onRemove, started, onStop, onRank }: CardProps) {
  const openTask = (id: number) => {
    const thread = findTask(guild, id)?.thread_id;
    onOpen(thread ? { kind: 'thread', id: String(thread) } : { kind: 'view', id: 'board' });
  };
  const seen = member.observed;
  const seenTool = seen?.tool && typeof seen.tool === 'object' ? seen.tool.name : '';
  const model = memberModel(member, started);
  const declaredAt = member.declared_at ? new Date(member.declared_at) : null;
  return (
    <div className="fs-agx-profile" style={{ '--agx-c': agentColor(member.id) } as CSSProperties} data-testid="agora-profile">
      <header>
        <span className="fs-agx-avatar" data-size="lg" data-presence={member.presence} aria-hidden="true">{agentInitials(member.id)}</span>
        <div><strong>{member.id}</strong><span>{t(PRESENCE_LABEL[member.presence])}{member.presence === 'invited' ? '' : ' · ' + t('seen {when}', { when: ago(member.last_seen) })}</span></div>
      </header>
      {member.role !== 'person' && (
        <label className="fs-agx-profile__rank" title={t('A higher role decides: its word closes disagreements, its review stands, it hands out the work and the questions of those below reach it before you.')}>
          <span>{t('Role')}</span>
          <select className="fs-field" value={member.rank ?? DEFAULT_RANK} onChange={(e) => onRank(member, e.target.value)} data-testid="agora-member-rank"><RankOptions /></select>
        </label>
      )}
      {member.role !== 'person' && <p className="fs-agx-profile__model" data-testid="agora-profile-model">
        <strong>{t('Model · effort')}</strong><span>{model.line}</span>
        {model.declared && <small className="fs-muted">{t('declared by the agent')}
          {declaredAt && !Number.isNaN(declaredAt.getTime()) && <> · <time dateTime={member.declared_at!}>{declaredAt.toLocaleString()}</time></>}
        </small>}
      </p>}
      {member.doing && <p className="fs-agx-profile__doing">{member.doing}</p>}
      {!member.doing && member.note && <p className="fs-agx-profile__doing">{member.note}</p>}
      {seen?.state && <p className="fs-muted">{t('Its session: {state}', { state: t(OBSERVED_LABEL[seen.state] ?? seen.state) })}{seenTool ? ' · ' + seenTool : ''}{seen.title ? ' · ' + seen.title : ''}</p>}
      {started && <p className="fs-muted">{t('Started from here')}</p>}
      {started && (started.worker.state === 'failed' || started.worker.state === 'unavailable') && (
        <p className="fs-agx-profile__failed" data-testid="agora-profile-failed">{t('Its session failed: {why}', { why: failureWhy(started.worker) || t('no reason given') })}</p>
      )}
      {started?.worker.state === 'completed' && <p className="fs-muted">{t('Its session finished')}</p>}
      {member.quota.note && <p className="fs-agx-profile__quota" data-state={member.quota.state}>{member.quota.note}</p>}
      {member.quota.state === 'ok' && member.quota.remaining_percent !== null && <p className="fs-muted">{t('Quota: {n}% free', { n: Math.round(member.quota.remaining_percent) })}</p>}
      {member.tasks.length > 0 && (
        <>
          <h4>{t('On its plate')}</h4>
          <ul className="fs-agx-profile__tasks">
            {member.tasks.map((task) => (
              <li key={task.id}><span className="fs-agx-status" data-status={task.status}>{t(STATUS_LABEL[task.status] ?? task.status)}</span>
                <button type="button" className="fs-agx-link" onClick={() => openTask(task.id)}>#{task.id} {task.title}</button></li>
            ))}
          </ul>
        </>
      )}
      {member.locks > 0 && <p className="fs-muted"><Lock size={12} aria-hidden="true" /> {tn(member.locks, '{n} lease held', '{n} leases held')}</p>}
      {member.role !== 'person' && (
        <div className="fs-agx-profile__actions">
          <Button size="sm" variant="secondary" icon={AtSign} label={t('Mention')} onClick={() => composerBus.insert('@' + member.id + ' ')} testId="agora-mention" />
          {started?.worker.state === 'running' && <Button size="sm" variant="ghost" icon={Square} label={t('Stop its session')} onClick={() => onStop(started.worker)} testId="agora-stop-session" />}
          <Button size="sm" variant="ghost" icon={UserMinus} label={t('Take off the server…')} onClick={() => onRemove(member)} testId="agora-remove-agent" />
        </div>
      )}
    </div>
  );
}

function MemberRow({ member, guild, onOpen, onRemove, started, onStop, onRank }: CardProps) {
  const line = member.doing || member.tasks[0]?.title || (member.presence === 'offline' ? t('seen {when}', { when: ago(member.last_seen) }) : t(PRESENCE_LABEL[member.presence]));
  const model = memberModel(member, started);
  return (
    <li>
      <Popover side="left" align="start" className="fs-agx-profile-pop" testId="agora-member-pop" trigger={
        <button type="button" className="fs-agx-member" data-presence={member.presence} style={{ '--agx-c': agentColor(member.id) } as CSSProperties}
          title={member.id + ' · ' + t(PRESENCE_LABEL[member.presence])} data-testid={'agora-member-' + member.id}>
          <span className="fs-agx-avatar" data-presence={member.presence} aria-hidden="true">{agentInitials(member.id)}</span>
          {/* The role goes under the name, so a long name is not cut short to make room for it. */}
          <span className="fs-agx-member__text"><strong>{member.id}</strong>
            {member.role !== 'person' && model.name && <span title={model.line} data-testid="agora-member-model">{model.name}</span>}
            <span className="fs-agx-member__line"><RankMark member={member} /><span>{line}</span></span></span>
          {member.tasks.length > 0 && <span className="fs-agx-row__count">{member.tasks.length}</span>}
        </button>}>
        <MemberCard member={member} guild={guild} onOpen={onOpen} onRemove={onRemove} started={started} onStop={onStop} onRank={onRank} />
      </Popover>
    </li>
  );
}

export function MembersPane({ guild, onOpen, notify, onChanged }: {
  guild: AgoraGuild; onOpen: (view: View) => void; notify: (text: string, tone?: 'ok' | 'warn') => void; onChanged: () => void;
}) {
  const [showOffline, setShowOffline] = useState(false);
  /** The dialog to add an agent: closed, open, or open on the provider whose + was pressed. */
  const [adding, setAdding] = useState<false | true | string>(false);
  const [pruning, setPruning] = useState(false);
  const [leaving, setLeaving] = useState<AgoraMember | null>(null);
  const [tuning, setTuning] = useState(false);
  // What the hub's console knows: the model and effort chosen for each provider on this server and the sessions
  // started from here. Asked apart from the server view (it looks at the programs installed on the PC) and never in its way.
  const [consoleData, setConsoleData] = useState<{ leaders: ConsoleLeader[]; workers: ConsoleWorker[] } | null>(null);
  const shown = useRef(guild.space.id);
  shown.current = guild.space.id;
  const loadConsole = useCallback((signal?: AbortSignal) => {
    const space = guild.space.id;
    getConsole(space, signal).then((data) => {
      if (shown.current === space) setConsoleData({ leaders: data.leaders ?? [], workers: data.workers ?? [] });   // not an answer about the server left behind
    }).catch(() => { /* a hub without a console: the list simply shows no models */ });
  }, [guild.space.id]);
  useEffect(() => {
    setConsoleData(null);
    const controller = new AbortController();
    loadConsole(controller.signal);
    return () => controller.abort();
  }, [loadConsole]);
  // While a session started here runs it is looked at again every few seconds: when it ends, its card says how and
  // stops offering to stop it.
  const running = (consoleData?.workers ?? []).some((w) => w.state === 'running' || w.state === 'stopping');
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => { if (!document.hidden) loadConsole(); }, 8000);
    return () => window.clearInterval(timer);
  }, [running, loadConsole]);
  const startedBy = useMemo(() => {
    const map = new Map<string, Started>();
    for (const worker of consoleData?.workers ?? []) {
      if (map.has(worker.agent_id)) continue;      // newest first: the first row of an agent is its latest session
      map.set(worker.agent_id, { worker, name: modelLine(consoleData?.leaders.find((l) => l.id === worker.parent_id)?.catalog, worker.model, undefined) });
    }
    return map;
  }, [consoleData]);
  const stopSession = (worker: ConsoleWorker) => {
    void stopWorker(guild.space.id, worker.id).then(() => {
      notify(t('Stopping {name}', { name: worker.agent_id }));
      setConsoleData((old) => (old ? { ...old, workers: old.workers.map((w) => (w.id === worker.id ? { ...w, state: 'stopping' } : w)) } : old));
    }).catch((e: Error) => { notify(e.message, 'warn'); loadConsole(); });
  };
  // Everyone, also those the server view leaves out for being long gone: asked for, not carried on every refresh.
  const [everyone, setEveryone] = useState<AgoraMember[] | null>(null);
  useEffect(() => { setEveryone(null); }, [guild.space.id]);
  const loadEveryone = () => {
    void getMembers(guild.space.id).then((out) => setEveryone(out.members.filter((m) => !isOnline(m)))).catch((e: Error) => notify(e.message, 'warn'));
  };
  const changed = () => { setEveryone(null); loadConsole(); onChanged(); };
  const groups = useMemo(() => {
    // Within a provider, whoever holds the higher rank goes first; among equals, the order they came in.
    const online = guild.members.filter(isOnline).map((m, at) => ({ m, at }))
      .sort((a, b) => (b.m.rank_level ?? 2) - (a.m.rank_level ?? 2) || a.at - b.at).map((x) => x.m);
    return ROLE_ORDER.map((role) => ({ role, members: online.filter((m) => m.role === role || (role === 'other' && !ROLE_ORDER.includes(m.role))) }))
      .filter((group) => group.members.length);
  }, [guild.members]);
  const giveRank = (member: AgoraMember, rank: string) => {
    void setMemberRank(guild.space.id, member.id, rank)
      .then(() => { notify(t('{name} is now {role}', { name: member.id, role: t(RANK_LABEL[rank] ?? rank) })); onChanged(); })
      .catch((e: Error) => notify(e.message, 'warn'));
  };
  const offline = useMemo(() => everyone ?? guild.members.filter((m) => !isOnline(m)), [everyone, guild.members]);
  const older = everyone ? 0 : guild.members_older ?? 0;
  return (
    <aside className="fs-agx-members" aria-label={t('Members')} data-testid="agora-members">
      <header className="fs-agx-members__head">
        <span>{t('Members')}</span>
        <Menu align="end" testId="agora-members-menu" trigger={<IconButton size="sm" icon={MoreHorizontal} label={t('More about members')} />} items={[
          { label: t('Add an agent…'), icon: UserPlus, onSelect: () => setAdding(true) },
          { label: t('Model and effort of the agents…'), icon: SlidersHorizontal, onSelect: () => setTuning(true) },
          { label: t('Take off those who no longer come…'), icon: UserMinus, onSelect: () => setPruning(true) },
        ]} />
      </header>
      <div className="fs-agx-members__add">
        <Button size="sm" variant="secondary" icon={UserPlus} label={t('Add an agent')} onClick={() => setAdding(true)} testId="agora-add-agent" />
      </div>
      {groups.map(({ role, members }) => {
        const quota = role === 'person' ? null : quotaOf(guild.providers, role);
        const leader = consoleData?.leaders.find((l) => l.id === role && (l.catalog ?? []).length > 0);
        const chosen = leader ? modelLine(leader.catalog, leader.model, leader.effort) : '';
        return (
          <section key={role} className="fs-agx-role" style={{ '--agx-c': ROLE_COLORS[role] } as CSSProperties}>
            <h3>
              <span>{t(ROLE_LABEL[role] ?? role)}</span><span className="fs-agx-cat__count">{members.length}</span>
              {role !== 'person' && role !== 'other' && (
                <IconButton size="sm" icon={Plus} label={t('Add a {provider} agent', { provider: t(ROLE_LABEL[role] ?? role) })} onClick={() => setAdding(role)} testId={'agora-add-agent-' + role} />
              )}
            </h3>
            {quota && (
              <div className="fs-agx-quota" title={quota.reset ? t('Resets {when}', { when: quota.reset }) : undefined} data-low={quota.free !== null && quota.free <= 10 || undefined}>
                <span className="fs-agx-quota__bar"><span style={{ inlineSize: (quota.free ?? 0) + '%' }} /></span>
                <span>{quota.free === null ? t('quota unknown') : t('{n}% free', { n: quota.free })}</span>
              </div>
            )}
            {chosen && (
              <button type="button" className="fs-agx-role__model" title={t('Model and effort of the agents')} onClick={() => setTuning(true)} data-testid={'agora-role-model-' + role}>
                <SlidersHorizontal size={11} aria-hidden="true" /><span>{chosen}</span>
              </button>
            )}
            <ul>{members.map((member) => <MemberRow key={member.id} member={member} guild={guild} onOpen={onOpen} onRemove={setLeaving} started={startedBy.get(member.id)} onStop={stopSession} onRank={giveRank} />)}</ul>
          </section>
        );
      })}
      {offline.length > 0 && (
        <section className="fs-agx-role" data-offline>
          <h3>
            <button type="button" className="fs-agx-cat__toggle" aria-expanded={showOffline} onClick={() => setShowOffline(!showOffline)}>
              {showOffline ? <ChevronDown size={12} aria-hidden="true" /> : <ChevronRight size={12} aria-hidden="true" />}
              <span>{t('Not around')}</span><span className="fs-agx-cat__count">{offline.length + older}</span>
            </button>
          </h3>
          {showOffline && <ul>{offline.slice(0, 400).map((member) => <MemberRow key={member.id} member={member} guild={guild} onOpen={onOpen} onRemove={setLeaving} started={startedBy.get(member.id)} onStop={stopSession} onRank={giveRank} />)}</ul>}
          {showOffline && older > 0 && (
            <p className="fs-muted fs-agx-role__more">
              {tn(older, 'and {n} more from long ago', 'and {n} more from long ago')}
              {' · '}<button type="button" className="fs-agx-link" onClick={loadEveryone} data-testid="agora-members-all">{t('See them all')}</button>
            </p>
          )}
        </section>
      )}
      <AddAgentDialog open={adding !== false} guild={guild} preset={typeof adding === 'string' ? adding : undefined} onClose={() => setAdding(false)} onDone={changed} notify={notify} />
      <RemoveAgentDialog member={leaving} guild={guild} onClose={() => setLeaving(null)} onDone={changed} notify={notify} />
      <PruneAgentsDialog open={pruning} guild={guild} onClose={() => setPruning(false)} onDone={changed} notify={notify} />
      <AgentModelsDialog open={tuning} guild={guild} onClose={() => setTuning(false)} onSaved={() => loadConsole()} notify={notify} />
    </aside>
  );
}
