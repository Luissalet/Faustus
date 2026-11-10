import { AtSign, ChevronDown, ChevronRight, Lock } from 'lucide-react';
import { useMemo, useState, type CSSProperties } from 'react';
import type { AgoraGuild, AgoraMember, QuotaProvider } from '../../adapters/agora';
import { Button, Popover } from '../../components';
import { t, tn } from '../../i18n';
import {
  ago, agentColor, agentInitials, composerBus, findTask, isOnline, PRESENCE_LABEL, ROLE_COLORS, ROLE_LABEL, ROLE_ORDER, STATUS_LABEL, type View,
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

function MemberCard({ member, guild, onOpen }: { member: AgoraMember; guild: AgoraGuild; onOpen: (view: View) => void }) {
  const openTask = (id: number) => {
    const thread = findTask(guild, id)?.thread_id;
    onOpen(thread ? { kind: 'thread', id: String(thread) } : { kind: 'view', id: 'board' });
  };
  const seen = member.observed;
  const seenTool = seen?.tool && typeof seen.tool === 'object' ? seen.tool.name : '';
  return (
    <div className="fs-agx-profile" style={{ '--agx-c': agentColor(member.id) } as CSSProperties} data-testid="agora-profile">
      <header>
        <span className="fs-agx-avatar" data-size="lg" data-presence={member.presence} aria-hidden="true">{agentInitials(member.id)}</span>
        <div><strong>{member.id}</strong><span>{t(PRESENCE_LABEL[member.presence])} · {t('seen {when}', { when: ago(member.last_seen) })}</span></div>
      </header>
      {member.doing && <p className="fs-agx-profile__doing">{member.doing}</p>}
      {seen?.state && <p className="fs-muted">{t('Its session: {state}', { state: t(OBSERVED_LABEL[seen.state] ?? seen.state) })}{seenTool ? ' · ' + seenTool : ''}{seen.title ? ' · ' + seen.title : ''}</p>}
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
      {member.role !== 'person' && <Button size="sm" variant="secondary" icon={AtSign} label={t('Mention')} onClick={() => composerBus.insert('@' + member.id + ' ')} testId="agora-mention" />}
    </div>
  );
}

function MemberRow({ member, guild, onOpen }: { member: AgoraMember; guild: AgoraGuild; onOpen: (view: View) => void }) {
  const line = member.doing || member.tasks[0]?.title || (member.presence === 'offline' ? t('seen {when}', { when: ago(member.last_seen) }) : t(PRESENCE_LABEL[member.presence]));
  return (
    <li>
      <Popover side="left" align="start" className="fs-agx-profile-pop" testId="agora-member-pop" trigger={
        <button type="button" className="fs-agx-member" data-presence={member.presence} style={{ '--agx-c': agentColor(member.id) } as CSSProperties}
          title={member.id + ' · ' + t(PRESENCE_LABEL[member.presence])} data-testid={'agora-member-' + member.id}>
          <span className="fs-agx-avatar" data-presence={member.presence} aria-hidden="true">{agentInitials(member.id)}</span>
          <span className="fs-agx-member__text"><strong>{member.id}</strong><span>{line}</span></span>
          {member.tasks.length > 0 && <span className="fs-agx-row__count">{member.tasks.length}</span>}
        </button>}>
        <MemberCard member={member} guild={guild} onOpen={onOpen} />
      </Popover>
    </li>
  );
}

export function MembersPane({ guild, onOpen }: { guild: AgoraGuild; onOpen: (view: View) => void }) {
  const [showOffline, setShowOffline] = useState(false);
  const groups = useMemo(() => {
    const online = guild.members.filter(isOnline);
    return ROLE_ORDER.map((role) => ({ role, members: online.filter((m) => m.role === role || (role === 'other' && !ROLE_ORDER.includes(m.role))) }))
      .filter((group) => group.members.length);
  }, [guild.members]);
  const offline = useMemo(() => guild.members.filter((m) => !isOnline(m)), [guild.members]);
  return (
    <aside className="fs-agx-members" aria-label={t('Members')} data-testid="agora-members">
      {groups.map(({ role, members }) => {
        const quota = role === 'person' ? null : quotaOf(guild.providers, role);
        return (
          <section key={role} className="fs-agx-role" style={{ '--agx-c': ROLE_COLORS[role] } as CSSProperties}>
            <h3><span>{t(ROLE_LABEL[role] ?? role)}</span><span className="fs-agx-cat__count">{members.length}</span></h3>
            {quota && (
              <div className="fs-agx-quota" title={quota.reset ? t('Resets {when}', { when: quota.reset }) : undefined} data-low={quota.free !== null && quota.free <= 10 || undefined}>
                <span className="fs-agx-quota__bar"><span style={{ inlineSize: (quota.free ?? 0) + '%' }} /></span>
                <span>{quota.free === null ? t('quota unknown') : t('{n}% free', { n: quota.free })}</span>
              </div>
            )}
            <ul>{members.map((member) => <MemberRow key={member.id} member={member} guild={guild} onOpen={onOpen} />)}</ul>
          </section>
        );
      })}
      {offline.length > 0 && (
        <section className="fs-agx-role" data-offline>
          <h3>
            <button type="button" className="fs-agx-cat__toggle" aria-expanded={showOffline} onClick={() => setShowOffline(!showOffline)}>
              {showOffline ? <ChevronDown size={12} aria-hidden="true" /> : <ChevronRight size={12} aria-hidden="true" />}
              <span>{t('Not around')}</span><span className="fs-agx-cat__count">{offline.length + (guild.members_older ?? 0)}</span>
            </button>
          </h3>
          {showOffline && <ul>{offline.slice(0, 120).map((member) => <MemberRow key={member.id} member={member} guild={guild} onOpen={onOpen} />)}</ul>}
          {showOffline && (guild.members_older ?? 0) > 0 && <p className="fs-muted fs-agx-role__more">{tn(guild.members_older ?? 0, 'and {n} more from long ago', 'and {n} more from long ago')}</p>}
        </section>
      )}
    </aside>
  );
}
