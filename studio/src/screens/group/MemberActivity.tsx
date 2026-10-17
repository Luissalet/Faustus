import { useEffect, useState } from 'react';
import type { GroupSnapshot, GroupStep } from '../../adapters/group';
import { t } from '../../i18n';
import './member-activity.css';

type Activity = { started_at?: number; last_event_at?: number; finished_at?: number; phase?: string; tool?: string };

export function elapsed(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  return total < 60 ? `${total}s` : total < 3600 ? `${Math.floor(total / 60)}m ${total % 60}s` : `${Math.floor(total / 3600)}h ${Math.floor(total % 3600 / 60)}m`;
}

/** Current work outranks a newer queued instruction for that same member. */
export function memberActivityRows(snapshot: GroupSnapshot) {
  return snapshot.group.participants.map((member) => {
    const steps = snapshot.steps.filter((step) => step.member_id === member.sessionId);
    const step = [...steps].reverse().find((s) => s.status === 'running' || s.status === 'waiting_user')
      ?? [...steps].reverse().find((s) => s.status === 'queued') ?? steps[steps.length - 1];
    const activity = (step?.detail as (GroupStep['detail'] & { activity?: Activity }) | undefined)?.activity ?? {};
    const state = step?.status ?? 'idle';
    const phase = state === 'running' ? activity.phase ?? 'waiting' : state;
    const label = phase === 'thinking' ? t('Thinking') : phase === 'writing' ? t('Writing')
      : phase === 'tool' ? activity.tool ? t('Using {tool}', { tool: activity.tool }) : t('Running a tool')
        : state === 'running' ? t('Waiting for the model') : state === 'queued' ? t('Queued')
          : state === 'done' ? t('Finished') : state === 'error' ? t('Failed')
            : state === 'waiting_user' ? t('Waiting for your answer') : state === 'stopped' ? t('Stopped') : t('Listening');
    return { member, step, activity, state, label };
  });
}

export function MemberActivity({ snapshot, connected = true, onSelect }: {
  snapshot: GroupSnapshot; connected?: boolean; onSelect?: (memberId: string) => void;
}) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => window.clearInterval(timer);
  }, []);
  return <ul className="fs-member-activity" aria-label={t('Participants')} data-testid="member-activity">
    {memberActivityRows(snapshot).map(({ member, step, activity, state, label }) => {
      const active = state === 'running' || state === 'queued' || state === 'waiting_user';
      const stale = state === 'running' && activity.last_event_at != null && now - activity.last_event_at >= 60;
      const end = active && state !== 'waiting_user' ? now : activity.finished_at;
      const name = member.personaName || member.route.model;
      return <li key={member.sessionId} data-state={connected ? state : 'disconnected'} data-stale={stale || undefined}>
        <button type="button" onClick={() => onSelect?.(member.sessionId)} aria-label={t('Activity for {name}', { name })}>
          <span className="fs-member-activity__name">{name}</span>
          <span className="fs-member-activity__phase">{connected ? label : t('Connection lost; status may have changed')}</span>
          {activity.started_at != null && end != null && <span className="fs-member-activity__time">{t('Elapsed: {time}', { time: elapsed(end - activity.started_at) })}</span>}
          {activity.last_event_at != null && active && <span className="fs-member-activity__signal" title={new Date(activity.last_event_at * 1000).toLocaleString()}>
            {t('Last activity {time} ago', { time: elapsed(now - activity.last_event_at) })}
          </span>}
          {state === 'running' && activity.last_event_at == null && <span className="fs-member-activity__signal">{t('No activity timestamp available')}</span>}
          {stale && <span className="fs-member-activity__quiet">{t('No recent activity; check the run')}</span>}
          {step?.error && <span className="fs-member-activity__error">{step.error}</span>}
        </button>
      </li>;
    })}
  </ul>;
}
