import { useCallback, useEffect, useMemo, useState } from 'react';
import { Button, EmptyState, Skeleton } from '../../components';
import {
  DEFAULT_OPENROUTER_PREFS,
  deleteOpenRouterEndpointPrefs,
  diffOpenRouterPrefs,
  getOpenRouterEndpointPrefs,
  isOpenRouterEndpoint,
  openRouterPrefsDirty,
  providerListFromText,
  providerListToText,
  putOpenRouterEndpointPrefs,
  type OpenRouterApiError,
  type OpenRouterMaxPrice,
  type OpenRouterPrefs as OpenRouterPrefsShape,
} from '../../adapters/openrouter';
import type { ModelEndpoint } from '../../adapters/settings';
import { t } from '../../i18n';
import { Field, SaveBar, Select, Text, Toggle } from './fields';
import '../settings.css';

/**
 * OBJ-8 / Lote B1 — per-endpoint OpenRouter preferences (`docs/api/
 * openrouter.md`'s "Opciones por endpoint", backend Lote A2). Registered as
 * its own Settings section (`Settings.tsx`'s `openrouter` key) and reachable
 * from Models: an endpoint whose base URL is `openrouter.ai` gets a
 * "OpenRouter preferences" button that jumps here with it pre-selected.
 *
 * One endpoint's prefs at a time, its own draft/dirty/save loop — the same
 * shape `RepositoriesSection` (`Settings.tsx`) already uses for a
 * small, fully-typed config object instead of the generic `Settings`
 * dictionary `useDraft` wraps.
 */

interface ErrorState {
  message: string;
  errorClass: string | null;
}

function fromApiError(err: unknown, fallback: string): ErrorState {
  const e = err as Partial<OpenRouterApiError> & { message?: string };
  return { message: e?.message || fallback, errorClass: e?.errorClass ?? null };
}

export function OpenRouterPrefsSection({
  endpoints,
  focusEndpointId,
  say,
}: {
  endpoints: ModelEndpoint[] | null;
  focusEndpointId?: string | null;
  say: (t: string) => void;
}) {
  const orEndpoints = useMemo(() => (endpoints ?? []).filter((e) => isOpenRouterEndpoint(e.baseUrl)), [endpoints]);
  const [selected, setSelected] = useState('');

  useEffect(() => {
    if (orEndpoints.length === 0) {
      setSelected('');
      return;
    }
    if (focusEndpointId && orEndpoints.some((e) => e.id === focusEndpointId)) {
      setSelected(focusEndpointId);
      return;
    }
    setSelected((cur) => (orEndpoints.some((e) => e.id === cur) ? cur : orEndpoints[0].id));
  }, [orEndpoints, focusEndpointId]);

  return (
    <section className="fs-set__section" aria-labelledby="fs-set-openrouter">
      <header className="fs-set__section-head">
        <div>
          <h2 id="fs-set-openrouter" className="fs-set__title">{t('OpenRouter')}</h2>
          <p className="fs-prose">{t('Routing, privacy and cost preferences OpenRouter applies to every request on one endpoint. Nothing here changes any other provider.')}</p>
        </div>
      </header>

      {endpoints === null ? (
        <Skeleton label={t('Loading endpoints')} count={2} height="64px" />
      ) : orEndpoints.length === 0 ? (
        <EmptyState
          title={t('No OpenRouter endpoint yet')}
          body={t('Add an endpoint whose base URL is openrouter.ai under Models, then come back here.')}
        />
      ) : (
        <>
          {orEndpoints.length > 1 && (
            <div className="fs-set__row-actions" role="tablist" aria-label={t('OpenRouter endpoint')}>
              {orEndpoints.map((ep) => (
                <Button
                  key={ep.id}
                  size="sm"
                  variant={selected === ep.id ? 'primary' : 'secondary'}
                  label={ep.name || ep.baseUrl}
                  aria-pressed={selected === ep.id}
                  onClick={() => setSelected(ep.id)}
                />
              ))}
            </div>
          )}
          {selected && <OpenRouterEndpointForm key={selected} endpointId={selected} say={say} />}
        </>
      )}
    </section>
  );
}

function OpenRouterEndpointForm({ endpointId, say }: { endpointId: string; say: (t: string) => void }) {
  const [base, setBase] = useState<OpenRouterPrefsShape | null>(null);
  const [draft, setDraft] = useState<OpenRouterPrefsShape>(DEFAULT_OPENROUTER_PREFS);
  const [loading, setLoading] = useState(true);
  const [failedStatus, setFailedStatus] = useState<number | 'other' | null>(null);
  const [saving, setSaving] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [confirmReset, setConfirmReset] = useState(false);
  const [error, setError] = useState<ErrorState | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    setFailedStatus(null);
    setError(null);
    getOpenRouterEndpointPrefs(endpointId)
      .then((p) => {
        setBase(p);
        setDraft(p);
      })
      .catch((e: unknown) => setFailedStatus((e as { status?: number })?.status ?? 'other'))
      .finally(() => setLoading(false));
  }, [endpointId]);
  useEffect(() => void load(), [load]);

  const dirty = base ? openRouterPrefsDirty(base, draft) : false;
  const set = <K extends keyof OpenRouterPrefsShape>(key: K, value: OpenRouterPrefsShape[K]) =>
    setDraft((d) => ({ ...d, [key]: value }));
  const setMaxPrice = (patch: Partial<OpenRouterMaxPrice>) => {
    const merged: OpenRouterMaxPrice = { ...(draft.max_price ?? {}), ...patch };
    set('max_price', merged.prompt === undefined && merged.completion === undefined ? null : merged);
  };

  const save = async () => {
    if (!base) return;
    setSaving(true);
    setError(null);
    try {
      const patch = diffOpenRouterPrefs(base, draft);
      const next = await putOpenRouterEndpointPrefs(endpointId, patch);
      setBase(next);
      setDraft(next);
      say(t('OpenRouter preferences saved.'));
    } catch (e) {
      setError(fromApiError(e, t('Could not save these preferences.')));
    } finally {
      setSaving(false);
    }
  };

  const reset = async () => {
    setResetting(true);
    setError(null);
    try {
      await deleteOpenRouterEndpointPrefs(endpointId);
      setConfirmReset(false);
      say(t('Restored to defaults.'));
      await load();
    } catch (e) {
      setError(fromApiError(e, t('Could not restore the defaults.')));
    } finally {
      setResetting(false);
    }
  };

  if (failedStatus === 401 || failedStatus === 403) {
    return <EmptyState tone="denied" title={t('Administrators only')} body={t('This account cannot change OpenRouter preferences.')} />;
  }
  if (failedStatus === 426) {
    return <EmptyState tone="incompatible" title={t('This client is out of date')} body={t('Update Faustus before changing these preferences.')} />;
  }
  if (failedStatus === 'other') {
    return (
      <EmptyState
        tone="error"
        title={t('Could not read these preferences.')}
        body={t('GET /api/openrouter/endpoints/{id}/prefs failed.', { id: endpointId })}
        primaryAction={{ label: t('Try again'), onClick: () => void load() }}
      />
    );
  }
  if (loading || !base) return <Skeleton label={t('Loading OpenRouter preferences')} count={4} height="44px" />;

  return (
    <div className="fs-set__card" data-testid="openrouter-prefs-form">
      {error && (
        <p className="fs-set__err" role="alert">
          {error.message}
          {error.errorClass ? ` (${error.errorClass})` : ''}
        </p>
      )}

      <Field label={t('Sort')} htmlFor="or-sort" help={t('How OpenRouter should rank providers before falling back to its own default.')}>
        <Select
          id="or-sort"
          value={draft.sort}
          onChange={(v) => set('sort', v as OpenRouterPrefsShape['sort'])}
          options={[
            { value: '', label: t('Let OpenRouter decide') },
            { value: 'price', label: t('Price') },
            { value: 'throughput', label: t('Throughput') },
            { value: 'latency', label: t('Latency') },
          ]}
        />
      </Field>

      <Field label={t('Allow fallback providers')} htmlFor="or-fallbacks">
        <Toggle id="or-fallbacks" checked={draft.allow_fallbacks} onChange={(v) => set('allow_fallbacks', v)} />
      </Field>

      <Field label={t('Require every parameter to be honored')} htmlFor="or-require-params" help={t('Reject a provider that would silently ignore a parameter this request sent.')}>
        <Toggle id="or-require-params" checked={draft.require_parameters} onChange={(v) => set('require_parameters', v)} />
      </Field>

      <Field label={t('Zero Data Retention (ZDR)')} htmlFor="or-zdr" help={t('Only route to providers contractually committed to not retaining your data.')}>
        <Toggle id="or-zdr" checked={draft.zdr} onChange={(v) => set('zdr', v)} />
      </Field>

      <Field
        label={t('Data collection')}
        htmlFor="or-data-collection"
        help={draft.data_collection === 'auto' ? t("auto = the project's privacy policy decides.") : undefined}
      >
        <Select
          id="or-data-collection"
          value={draft.data_collection}
          onChange={(v) => set('data_collection', v as OpenRouterPrefsShape['data_collection'])}
          options={[
            { value: 'auto', label: t('Auto') },
            { value: 'allow', label: t('Allow') },
            { value: 'deny', label: t('Deny') },
          ]}
        />
      </Field>

      <div className="fs-set__grid2">
        <Field label={t('Max prompt price ($/1M tokens)')} htmlFor="or-max-prompt">
          <input
            id="or-max-prompt"
            type="number"
            min="0"
            step="0.01"
            className="fs-field"
            value={draft.max_price?.prompt ?? ''}
            onChange={(e) => setMaxPrice({ prompt: e.target.value === '' ? undefined : Number(e.target.value) })}
          />
        </Field>
        <Field label={t('Max completion price ($/1M tokens)')} htmlFor="or-max-completion">
          <input
            id="or-max-completion"
            type="number"
            min="0"
            step="0.01"
            className="fs-field"
            value={draft.max_price?.completion ?? ''}
            onChange={(e) => setMaxPrice({ completion: e.target.value === '' ? undefined : Number(e.target.value) })}
          />
        </Field>
      </div>

      <Field label={t('Preferred providers (order)')} htmlFor="or-order" help={t('Comma-separated provider ids, tried in this order first.')}>
        <Text id="or-order" value={providerListToText(draft.order)} onChange={(v) => set('order', providerListFromText(v))} placeholder="anthropic, openai" />
      </Field>

      <Field label={t('Excluded providers')} htmlFor="or-ignore" help={t('Comma-separated provider ids OpenRouter must never route to.')}>
        <Text id="or-ignore" value={providerListToText(draft.ignore)} onChange={(v) => set('ignore', providerListFromText(v))} />
      </Field>

      <Field label={t('Native fallback models')} htmlFor="or-native-fallback" help={t("Let OpenRouter itself retry on a fallback model list before Faustus's own retry logic ever sees the failure.")}>
        <Toggle id="or-native-fallback" checked={draft.native_fallback} onChange={(v) => set('native_fallback', v)} />
      </Field>

      <div className="fs-set__card fs-set__card--danger">
        <h3 className="fs-set__card-title">{t('Web search')}</h3>
        <Field label={t('Enable web search')} htmlFor="or-web-enabled">
          <Toggle id="or-web-enabled" checked={draft.web_search.enabled} onChange={(v) => set('web_search', { ...draft.web_search, enabled: v })} />
        </Field>
        {draft.web_search.enabled && (
          <Field label={t('Results per search')} htmlFor="or-web-results">
            <input
              id="or-web-results"
              type="number"
              min={1}
              max={10}
              className="fs-field fs-field--short"
              value={draft.web_search.max_results}
              onChange={(e) => set('web_search', { ...draft.web_search, max_results: Math.max(1, Math.min(10, Number(e.target.value) || 1)) })}
            />
          </Field>
        )}
        <p className="fs-set__help">{t('~$0.02 per request with 5 results; this never turns itself on — only this toggle, or a one-off request, enables it.')}</p>
      </div>

      <div className="fs-set__row-actions">
        {confirmReset ? (
          <>
            <span className="fs-set__help">{t('Discard the saved preferences for this endpoint?')}</span>
            <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setConfirmReset(false)} />
            <Button variant="danger-solid" size="sm" label={t('Reset')} loading={resetting} onClick={() => void reset()} testId="openrouter-reset-confirm" />
          </>
        ) : (
          <Button variant="ghost" size="sm" label={t('Reset to defaults')} onClick={() => setConfirmReset(true)} testId="openrouter-reset" />
        )}
      </div>
      <SaveBar dirty={dirty} saving={saving} onSave={() => void save()} />
    </div>
  );
}
