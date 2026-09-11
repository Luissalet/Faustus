import { useSearchParams } from 'react-router';
import { useEffect, useState } from 'react';
import { AlertTriangle } from 'lucide-react';
import { Defs } from './agents/Defs';
import { Experts } from './agents/Experts';
import { Runners } from './agents/Runners';
import { Workers } from './agents/Workers';
import { Tournament } from './agents/Tournament';
import { loadLeases, type AgentLease, type LeasesSnapshot } from '../adapters/agents';
import { relativeTime } from '../adapters/home';
import './projects.css';
import './agents.css';
import { t, tn } from '../i18n';

/**
 * PLAN-03: "who edits what" — the process-wide resource-lease registry
 * (src/resource_ownership.py, `GET /api/agents/leases`), polled and shown as
 * a small card while any delegation holds a lease. Lives here rather than
 * inside one tab's panel because a lease outlives the tab you happen to be
 * on — a worker started from Workers can still hold a file while you are
 * reading Defs. Silent (renders nothing) when there is nothing to show, so
 * it never crowds a quiet install.
 */
const LEASES_POLL_MS = 4000;

function LeasesCard() {
  const [snapshot, setSnapshot] = useState<LeasesSnapshot | null>(null);

  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const next = await loadLeases();
        if (!cancelled) setSnapshot(next);
      } catch {
        // A transient fetch failure just skips this tick; the next poll
        // tries again. Nothing here is worth interrupting the screen for.
      }
    };
    void tick();
    const id = window.setInterval(() => void tick(), LEASES_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  if (!snapshot || (snapshot.leases.length === 0 && snapshot.conflicts.length === 0)) return null;

  const conflicted = new Set(snapshot.conflicts.map((c) => c.resource));
  const labelFor = (lease: AgentLease) => lease.taskId || lease.ownerAgent;

  return (
    <section className="fs-agents__leases" data-testid="agents-leases-card">
      <header className="fs-agents__leases-head">
        <strong>{t('Who edits what')}</strong>
        <span className="fs-agents__note">{tn(snapshot.leases.length, '{n} active lease', '{n} active leases', { n: snapshot.leases.length })}</span>
      </header>
      <ul className="fs-agents__leases-list">
        {snapshot.leases.map((lease) => {
          const isConflicted = conflicted.has(lease.resource);
          return (
            <li key={`${lease.kind}:${lease.resource}`} className="fs-agents__lease-row" data-conflict={isConflicted || undefined}>
              <code title={lease.resource}>{lease.resource.split(/[\\/]/).pop() || lease.resource}</code>
              <span title={lease.taskId ? `${t('agent')}: ${lease.ownerAgent}` : undefined}>{labelFor(lease)}</span>
              <span className="fs-agents__note">{t('since {time}', { time: relativeTime(lease.since) })}</span>
              {isConflicted && (
                <span className="fs-agents__lease-conflict" title={t('Another task asked for this resource while it was held')}>
                  <AlertTriangle size={13} aria-hidden="true" /> {t('Conflict')}
                </span>
              )}
            </li>
          );
        })}
      </ul>
      {snapshot.conflicts.length > 0 && (
        <p className="fs-agents__note" data-on>
          {tn(
            snapshot.conflicts.length,
            '{n} task waited on a resource another one already held.',
            '{n} tasks waited on a resource another one already held.',
            { n: snapshot.conflicts.length },
          )}
        </p>
      )}
    </section>
  );
}

/**
 * Agentes: the four panels the previous interface kept as separate modals
 * — Workers (the dispatch board), Agent runners, Agent definitions and
 * Expertos — as one screen with tabs, because they are one subject: who
 * does the mechanical work, under what rules, and with what knowledge.
 *
 * `?t=workers|runners|defs|experts|tournament` picks the tab; `?agent=<slug>` and
 * `?runner=<key>` prefill the Workers form (that is what "Usar en un
 * trabajo" on a definition or a runner does).
 */

type Tab = 'workers' | 'runners' | 'defs' | 'experts' | 'tournament';

const TABS: { key: Tab; label: string; hint: string }[] = [
  { key: 'workers', label: 'Workers', hint: 'The local models do the mechanical work; you read what changed' },
  { key: 'runners', label: 'Runners', hint: 'Claude Code, OpenCode, Qwen Code or any other terminal agent as a worker' },
  { key: 'defs', label: 'Definitions', hint: 'What each agent may use, touch and not do; its slug goes on a task' },
  { key: 'experts', label: 'Experts', hint: 'A specialist with its corpus, and corrections that cite the page' },
  { key: 'tournament', label: 'Tournament', hint: 'The same task to several models, rounds of hybrids, a judged table' },
];

export function AgentsScreen() {
  const [params, setParams] = useSearchParams();
  const raw = params.get('t');
  const tab: Tab = raw === 'runners' || raw === 'defs' || raw === 'experts' || raw === 'tournament' ? raw : 'workers';
  const agent = params.get('agent') ?? undefined;
  const runner = params.get('runner') ?? undefined;

  const go = (next: Tab, extra?: Record<string, string>) => {
    const p = new URLSearchParams();
    p.set('t', next);
    if (extra) for (const [k, v] of Object.entries(extra)) p.set(k, v);
    setParams(p);
  };

  return (
    <div className="fs-screen fs-agents" data-testid="agents">
      <header className="fs-screen__head">
        <div>
          <h1 className="fs-screen__title">{t('Agents')}</h1>
          <p className="fs-prose" style={{ marginBlockStart: 'var(--fs-space-2)' }}>
            {t('Who does the mechanical work, under what rules and with what knowledge.')}
          </p>
        </div>
      </header>
      <LeasesCard />
      <div className="fs-tabs" role="tablist" aria-label={t('Agents')}>
        {TABS.map((entry) => (
          <button key={entry.key} type="button" role="tab" className="fs-tab" aria-selected={tab === entry.key} title={t(entry.hint)} onClick={() => go(entry.key)} data-testid={`agents-tab-${entry.key}`}>
            {t(entry.label)}
          </button>
        ))}
      </div>
      <div className="fs-agents__panel" role="tabpanel">
        {tab === 'workers' && <Workers agent={agent} runner={runner} />}
        {tab === 'runners' && <Runners onUseRunner={(key) => go('workers', { runner: key })} />}
        {tab === 'defs' && <Defs onUseAgent={(slug) => go('workers', { agent: slug })} />}
        {tab === 'experts' && <Experts />}
        {tab === 'tournament' && <Tournament />}
      </div>
    </div>
  );
}
