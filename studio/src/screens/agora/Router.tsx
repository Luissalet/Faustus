import { Route } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import {
  getRouteSettings, MAX_ROUTE_AGENTS, MIN_ROUTE_AGENTS, ROUTE_MODELS, setRouteSettings,
  type MessageRoute, type RouteModel, type RoutePick, type RouteSettings,
} from '../../adapters/agora';
import { Popover } from '../../components';
import { t } from '../../i18n';
import { pageOf } from '../agent-page/AgentPageLayer';

/** What the router did for a message that named nobody: who was told, who passed, and how it chose. */

const HOW: Record<string, string> = { model: 'chosen by the router (model)', rules: 'chosen by the router (rules)' };
const isPassed = (pick: RoutePick) => pick.status === 'passed';

/** The router's line under a message. Without a route (or without anything to say) it draws nothing. */
export function RouteLine({ route }: { route: MessageRoute | null | undefined }) {
  const [open, setOpen] = useState(false);
  if (!route || typeof route !== 'object') return null;
  const picks = Array.isArray(route.picks) ? route.picks.filter((p) => p && typeof p.agent === 'string' && p.agent) : [];
  const assigned = picks.filter((p) => !isPassed(p));
  const how = HOW[route.engine] ? t(HOW[route.engine]) : t('decided by the router');
  // The hub words its own label (Spanish); it stays as the line's title, while the line itself follows the interface language.
  const lead = !picks.length ? t('Nobody has to answer') : assigned.length ? t('Answered by') : t('Nobody left to answer');
  return (
    <div className="fs-agx-route" data-testid="agora-route" data-engine={route.engine} data-nobody={picks.length === 0 || undefined} title={route.label || undefined}>
      <Route size={13} aria-hidden="true" />
      <span className="fs-agx-route__lead">{lead}</span>
      {picks.map((pick) => (
        <button key={pick.agent} type="button" className="fs-agx-route__pick" data-testid="agora-route-pick" data-status={pick.status}
          data-agent-id={pageOf(pick.agent)} title={pick.why || undefined}
          aria-label={pick.agent + (isPassed(pick) ? ', ' + t('passed') : '') + (pick.why ? '. ' + pick.why : '')}>
          @{pick.agent}
        </button>
      ))}
      <Popover open={open} onOpenChange={setOpen} side="top" align="start" className="fs-agx-route-pop" testId="agora-route-pop"
        trigger={<button type="button" className="fs-agx-route__how" data-testid="agora-route-how" aria-label={t('Why the router chose this')}>· {how}</button>}>
        <div className="fs-agx-route-pop__body">
          <p className="fs-agx-route-pop__title">{t('Why the router chose this')}</p>
          {picks.length === 0 && <p className="fs-muted">{route.note || t('It found nobody who has to answer.')}</p>}
          <ul>
            {picks.map((pick) => (
              <li key={pick.agent} data-status={pick.status}>
                <strong>@{pick.agent}</strong>{isPassed(pick) && <span className="fs-agx-route-pop__passed"> · {t('passed')}</span>}
                <span>{pick.why || t('No reason given.')}</span>
              </li>
            ))}
          </ul>
          {picks.length > 0 && route.note && <p className="fs-muted">{route.note}</p>}
        </div>
      </Popover>
    </div>
  );
}

type Draft = RouteSettings;
export interface RouterSettingsState {
  /** `unavailable` when the hub answers no settings (an older hub): the section says so and the dialog still saves. */
  status: 'loading' | 'ready' | 'unavailable';
  draft: Draft | null; dirty: boolean; error: string;
  change: (changes: Partial<Draft>) => void;
  /** Writes what changed; resolves when the hub has it. */
  save: () => Promise<void>;
}

/** The router's settings of one server, loaded when the dialog opens and written with its Save. */
export function useRouterSettings(space: string, open: boolean): RouterSettingsState {
  const [status, setStatus] = useState<RouterSettingsState['status']>('loading');
  const [base, setBase] = useState<Draft | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setStatus('loading'); setError('');
    getRouteSettings(space, controller.signal).then((answer) => {
      const s = answer.settings;
      const value: Draft = {
        enabled: s.enabled !== false, max_agents: Math.min(MAX_ROUTE_AGENTS, Math.max(MIN_ROUTE_AGENTS, Number(s.max_agents) || 3)),
        model: (ROUTE_MODELS as readonly string[]).includes(s.model) ? s.model : 'auto',
      };
      setBase(value); setDraft(value); setStatus('ready');
    }).catch((e: Error) => {
      if (controller.signal.aborted) return;
      setStatus('unavailable'); setBase(null); setDraft(null); setError(e.message);
    });
    return () => controller.abort();
  }, [space, open]);
  const dirty = Boolean(base && draft && (base.enabled !== draft.enabled || base.max_agents !== draft.max_agents || base.model !== draft.model));
  const change = useCallback((changes: Partial<Draft>) => setDraft((d) => (d ? { ...d, ...changes } : d)), []);
  const save = useCallback(async () => {
    if (!base || !draft || !dirty) return;
    const changes: Partial<Draft> = {};
    if (draft.enabled !== base.enabled) changes.enabled = draft.enabled;
    if (draft.max_agents !== base.max_agents) changes.max_agents = draft.max_agents;
    if (draft.model !== base.model) changes.model = draft.model;
    const answer = await setRouteSettings(space, changes);
    const kept: Draft = answer.settings ? { enabled: answer.settings.enabled, max_agents: answer.settings.max_agents, model: answer.settings.model } : draft;
    setBase(kept); setDraft(kept);
  }, [base, draft, dirty, space]);
  return { status, draft, dirty, error, change, save };
}

const MODEL_LABEL: Record<RouteModel, string> = { auto: 'The Sparks model', rules: 'Rules only' };

/** The section of the server's settings that says who answers what nobody was named for. */
export function RouterSettingsSection({ state }: { state: RouterSettingsState }) {
  const { draft } = state;
  return (
    <section className="fs-agx-form__group" data-testid="agora-router-settings" aria-labelledby="agora-router-title">
      <h3 id="agora-router-title" className="fs-agx-form__label">{t('Who answers messages that name nobody')}</h3>
      <p className="fs-muted fs-agx-form__hint">{t('The router picks the agents of this server that should answer what you write without an @ and tells them. It never answers agent messages.')}</p>
      {state.status === 'loading' && <p className="fs-muted" aria-live="polite">{t('Loading…')}</p>}
      {state.status === 'unavailable' && <p className="fs-muted" data-testid="agora-router-unavailable">{t('This Hub does not offer the router yet.')}</p>}
      {state.status === 'ready' && draft && (
        <div className="fs-agx-router">
          <label className="fs-switch"><input type="checkbox" checked={draft.enabled} data-testid="agora-router-enabled"
            onChange={(e) => state.change({ enabled: e.target.checked })} /> {t('Choose who answers')}</label>
          <div className="fs-agx-form__row">
            <label className="fs-agx-form__field"><span className="fs-agx-form__label">{t('At most')}</span>
              <select className="fs-field" value={draft.max_agents} disabled={!draft.enabled} data-testid="agora-router-max"
                onChange={(e) => state.change({ max_agents: Number(e.target.value) })}>
                {Array.from({ length: MAX_ROUTE_AGENTS - MIN_ROUTE_AGENTS + 1 }, (_, i) => MIN_ROUTE_AGENTS + i).map((n) => (
                  <option key={n} value={n}>{n === 1 ? t('1 agent') : t('{n} agents', { n })}</option>
                ))}
              </select></label>
            <label className="fs-agx-form__field"><span className="fs-agx-form__label">{t('It decides with')}</span>
              <select className="fs-field" value={draft.model} disabled={!draft.enabled} data-testid="agora-router-model"
                onChange={(e) => state.change({ model: e.target.value as RouteModel })}>
                {ROUTE_MODELS.map((m) => <option key={m} value={m}>{t(MODEL_LABEL[m])}</option>)}
              </select></label>
          </div>
        </div>
      )}
    </section>
  );
}
