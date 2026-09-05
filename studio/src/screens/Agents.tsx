import { useSearchParams } from 'react-router';
import { Defs } from './agents/Defs';
import { Experts } from './agents/Experts';
import { Runners } from './agents/Runners';
import { Workers } from './agents/Workers';
import './projects.css';
import './agents.css';

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
  { key: 'workers', label: 'Workers', hint: 'Los modelos locales hacen el trabajo mecánico; tú lees qué cambió' },
  { key: 'runners', label: 'Runners', hint: 'Claude Code, OpenCode, Qwen Code o cualquier otro agente de terminal como worker' },
  { key: 'defs', label: 'Definiciones', hint: 'Qué puede usar, tocar y no hacer cada agente; su slug va en una tarea' },
  { key: 'experts', label: 'Expertos', hint: 'Un especialista con su corpus, y correcciones que citan la página' },
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
          <h1 className="fs-screen__title">Agentes</h1>
          <p className="fs-prose" style={{ marginBlockStart: 'var(--fs-space-2)' }}>
            Quién hace el trabajo mecánico, bajo qué reglas y con qué conocimiento.
          </p>
        </div>
      </header>
      <div className="fs-tabs" role="tablist" aria-label="Agentes">
        {TABS.map((t) => (
          <button key={t.key} type="button" role="tab" className="fs-tab" aria-selected={tab === t.key} title={t.hint} onClick={() => go(t.key)} data-testid={`agents-tab-${t.key}`}>
            {t.label}
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
