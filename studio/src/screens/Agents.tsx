import { useSearchParams } from 'react-router';
import { Defs } from './agents/Defs';
import { Experts } from './agents/Experts';
import { Runners } from './agents/Runners';
import { Workers } from './agents/Workers';
import './projects.css';
import './agents.css';
import { t } from '../i18n';

/**
 * Agentes: the four panels the previous interface kept as separate modals
 * — Workers (the dispatch board), Agent runners, Agent definitions and
 * Expertos — as one screen with tabs, because they are one subject: who
 * does the mechanical work, under what rules, and with what knowledge.
 *
 * `?t=workers|runners|defs|experts` picks the tab; `?agent=<slug>` and
 * `?runner=<key>` prefill the Workers form (that is what "Usar en un
 * trabajo" on a definition or a runner does).
 */

type Tab = 'workers' | 'runners' | 'defs' | 'experts';

const TABS: { key: Tab; label: string; hint: string }[] = [
  { key: 'workers', label: 'Workers', hint: 'The local models do the mechanical work; you read what changed' },
  { key: 'runners', label: 'Runners', hint: 'Claude Code, OpenCode, Qwen Code or any other terminal agent as a worker' },
  { key: 'defs', label: 'Definitions', hint: 'What each agent may use, touch and not do; its slug goes on a task' },
  { key: 'experts', label: 'Experts', hint: 'A specialist with its corpus, and corrections that cite the page' },
];

export function AgentsScreen() {
  const [params, setParams] = useSearchParams();
  const raw = params.get('t');
  const tab: Tab = raw === 'runners' || raw === 'defs' || raw === 'experts' ? raw : 'workers';
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
      </div>
    </div>
  );
}
