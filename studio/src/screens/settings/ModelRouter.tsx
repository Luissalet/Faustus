import { useCallback, useEffect, useMemo, useState } from 'react';
import { Button, EmptyState, Skeleton } from '../../components';
import {
  DEFAULT_MODEL_ROUTER_CONFIG,
  diffModelRouterConfig,
  getModelRouterConfig,
  getModelRouterLog,
  getModelRouterStats,
  MODEL_ROUTER_CAPABILITIES,
  modelListFromText,
  modelListToText,
  modelRouterConfigDirty,
  previewModelRouterDecision,
  updateModelRouterConfig,
  type ModelRouterApiError,
  type ModelRouterCapability,
  type ModelRouterConfig,
  type ModelRouterDecision,
  type ModelRouterLogEntry,
  type ModelRouterStatsEntry,
} from '../../adapters/modelRouter';
import { t } from '../../i18n';
import { Field, SaveBar, Text, Toggle } from './fields';
import '../settings.css';

/**
 * OBJ-8 / Lote B1 — MOD-05's admin surface (`docs/api/model_router.md`).
 * Registered as its own Settings section (`Settings.tsx`'s `model_router`
 * key). `docs/api/model_router.md`'s own "Integración pendiente" section
 * says this lote deliberately stops short of wiring `choose()` into a real
 * chat turn — the note below says exactly that, not a hidden limitation.
 */

const CAPABILITY_LABEL: Record<ModelRouterCapability, string> = {
  tool_call: 'Tool calling',
  json_mode: 'JSON mode',
  vision: 'Vision',
  reasoning: 'Reasoning',
};

interface ErrorState {
  message: string;
  errorClass: string | null;
}

function fromApiError(err: unknown, fallback: string): ErrorState {
  const e = err as Partial<ModelRouterApiError> & { message?: string };
  return { message: e?.message || fallback, errorClass: e?.errorClass ?? null };
}

type Tab = 'config' | 'preview' | 'log' | 'stats';

export function ModelRouterSection({ say }: { say: (t: string) => void }) {
  const [tab, setTab] = useState<Tab>('config');
  const [config, setConfig] = useState<ModelRouterConfig | null>(null);
  const [failedStatus, setFailedStatus] = useState<number | 'other' | null>(null);

  const load = useCallback(() => {
    setFailedStatus(null);
    getModelRouterConfig()
      .then(setConfig)
      .catch((e: unknown) => setFailedStatus((e as { status?: number })?.status ?? 'other'));
  }, []);
  useEffect(() => void load(), [load]);

  return (
    <section className="fs-set__section" aria-labelledby="fs-set-model-router">
      <header className="fs-set__section-head">
        <div>
          <h2 id="fs-set-model-router" className="fs-set__title">{t('Model router')}</h2>
          <p className="fs-prose">{t('Chooses which LOCAL model answers a turn from evidence this installation has actually recorded — off by default, and it never switches to a paid model on its own.')}</p>
        </div>
      </header>
      <p className="fs-set__help">{t('Chat turn integration: pending (MOD-05).')}</p>

      {failedStatus === 401 || failedStatus === 403 ? (
        <EmptyState tone="denied" title={t('Administrators only')} body={t('This account cannot see the model router.')} />
      ) : failedStatus === 426 ? (
        <EmptyState tone="incompatible" title={t('This client is out of date')} body={t('Update Faustus before changing the model router.')} />
      ) : failedStatus === 'other' ? (
        <EmptyState tone="error" title={t('Could not read the router configuration.')} body={t('GET /api/model-router/config failed.')} primaryAction={{ label: t('Try again'), onClick: () => void load() }} />
      ) : (
        <>
          <div className="fs-set__row-actions" role="tablist" aria-label={t('Model router views')}>
            <Button size="sm" variant={tab === 'config' ? 'primary' : 'secondary'} label={t('Configuration')} aria-pressed={tab === 'config'} onClick={() => setTab('config')} />
            <Button size="sm" variant={tab === 'preview' ? 'primary' : 'secondary'} label={t('Test a decision')} aria-pressed={tab === 'preview'} onClick={() => setTab('preview')} />
            <Button size="sm" variant={tab === 'log' ? 'primary' : 'secondary'} label={t('Log')} aria-pressed={tab === 'log'} onClick={() => setTab('log')} />
            <Button size="sm" variant={tab === 'stats' ? 'primary' : 'secondary'} label={t('Statistics')} aria-pressed={tab === 'stats'} onClick={() => setTab('stats')} />
          </div>

          {tab === 'config' &&
            (!config ? <Skeleton label={t('Loading')} count={4} height="44px" /> : <ModelRouterConfigForm config={config} onSaved={setConfig} say={say} />)}
          {tab === 'preview' && <ModelRouterPreviewPanel candidates={config?.candidates ?? []} />}
          {tab === 'log' && <ModelRouterLogPanel />}
          {tab === 'stats' && <ModelRouterStatsPanel />}
        </>
      )}
    </section>
  );
}

function ModelRouterConfigForm({
  config,
  onSaved,
  say,
}: {
  config: ModelRouterConfig;
  onSaved: (c: ModelRouterConfig) => void;
  say: (t: string) => void;
}) {
  const [draft, setDraft] = useState<ModelRouterConfig>(config ?? DEFAULT_MODEL_ROUTER_CONFIG);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<ErrorState | null>(null);
  useEffect(() => setDraft(config), [config]);

  const dirty = modelRouterConfigDirty(config, draft);
  const set = <K extends keyof ModelRouterConfig>(key: K, value: ModelRouterConfig[K]) => setDraft((d) => ({ ...d, [key]: value }));
  const toggleCapability = (cap: ModelRouterCapability) => {
    const has = draft.min_capabilities.includes(cap);
    set('min_capabilities', has ? draft.min_capabilities.filter((c) => c !== cap) : [...draft.min_capabilities, cap]);
  };

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const patch = diffModelRouterConfig(config, draft);
      const next = await updateModelRouterConfig(patch);
      onSaved(next);
      say(t('Model router configuration saved.'));
    } catch (e) {
      setError(fromApiError(e, t('Could not save the configuration.')));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fs-set__card">
      {error && (
        <p className="fs-set__err" role="alert">
          {error.message}
          {error.errorClass ? ` (${error.errorClass})` : ''}
        </p>
      )}

      <Field label={t('Enable the router')} htmlFor="mr-enabled" help={t('Off by default. While off, nothing in the rest of the system changes.')}>
        <Toggle id="mr-enabled" checked={draft.enabled} onChange={(v) => set('enabled', v)} />
      </Field>

      <Field label={t('Prefer local models')} htmlFor="mr-prefer-local">
        <Toggle id="mr-prefer-local" checked={draft.prefer_local} onChange={(v) => set('prefer_local', v)} />
      </Field>

      <Field label={t('Max latency (seconds)')} htmlFor="mr-max-latency" help={t('Empty: no limit.')}>
        <input
          id="mr-max-latency"
          type="number"
          min="0"
          step="0.5"
          className="fs-field fs-field--short"
          value={draft.max_latency_s ?? ''}
          onChange={(e) => set('max_latency_s', e.target.value === '' ? null : Number(e.target.value))}
        />
      </Field>

      <Field label={t('Candidates')} htmlFor="mr-candidates" help={t('Comma-separated model names. Empty: every local model this installation has.')}>
        <Text id="mr-candidates" value={modelListToText(draft.candidates)} onChange={(v) => set('candidates', modelListFromText(v))} />
      </Field>

      <Field label={t('Required capabilities')} help={t('A model missing any of these is never chosen, whatever its score.')}>
        <div className="fs-inline">
          {MODEL_ROUTER_CAPABILITIES.map((cap) => (
            <label key={cap} className="fs-check">
              <input type="checkbox" checked={draft.min_capabilities.includes(cap)} onChange={() => toggleCapability(cap)} /> <span>{t(CAPABILITY_LABEL[cap])}</span>
            </label>
          ))}
        </div>
      </Field>

      <div className="fs-set__card fs-set__card--danger">
        <Field
          label={t('Allow paid escalation')}
          htmlFor="mr-escalation"
          help={t('It never switches to a paid model in silence: this only ever lets a decision report that escalation would be needed — a caller still has to read it and decide, every time.')}
        >
          <Toggle id="mr-escalation" checked={draft.allow_paid_escalation} onChange={(v) => set('allow_paid_escalation', v)} />
        </Field>
      </div>

      <SaveBar dirty={dirty} saving={saving} onSave={() => void save()} />
    </div>
  );
}

function ModelRouterPreviewPanel({ candidates }: { candidates: string[] }) {
  const [caps, setCaps] = useState<Set<ModelRouterCapability>>(new Set());
  const [maxLatency, setMaxLatency] = useState('');
  const [installed, setInstalled] = useState(modelListToText(candidates));
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ModelRouterDecision | null>(null);
  const [explain, setExplain] = useState('');
  const [error, setError] = useState<string | null>(null);

  const toggleCap = (cap: ModelRouterCapability) => {
    setCaps((cur) => {
      const next = new Set(cur);
      if (next.has(cap)) next.delete(cap);
      else next.add(cap);
      return next;
    });
  };

  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      const { decision, explain: text } = await previewModelRouterDecision(
        { capabilities: [...caps], max_latency_s: maxLatency === '' ? null : Number(maxLatency) },
        modelListFromText(installed),
      );
      setResult(decision);
      setExplain(text);
    } catch (e) {
      setError((e as Error).message || t('Could not run the preview.'));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fs-set__card" data-testid="model-router-preview">
      <h3 className="fs-set__card-title">{t('Test a decision')}</h3>
      <p className="fs-set__help">{t('Runs the router without recording anything in the log — see what it would choose before turning it on.')}</p>

      <Field label={t('Required capabilities')}>
        <div className="fs-inline">
          {MODEL_ROUTER_CAPABILITIES.map((cap) => (
            <label key={cap} className="fs-check">
              <input type="checkbox" checked={caps.has(cap)} onChange={() => toggleCap(cap)} /> <span>{t(CAPABILITY_LABEL[cap])}</span>
            </label>
          ))}
        </div>
      </Field>

      <Field label={t('Max latency (seconds)')} htmlFor="mr-preview-latency">
        <input id="mr-preview-latency" type="number" min="0" step="0.5" className="fs-field fs-field--short" value={maxLatency} onChange={(e) => setMaxLatency(e.target.value)} />
      </Field>

      <Field label={t('Installed models')} htmlFor="mr-preview-installed" help={t('Comma-separated — what this device actually has right now.')}>
        <Text id="mr-preview-installed" value={installed} onChange={setInstalled} />
      </Field>

      <Button variant="primary" size="sm" label={t('Test a decision')} loading={busy} onClick={() => void run()} testId="model-router-preview-run" />

      {error && <p className="fs-set__err" role="alert">{error}</p>}

      {result && (
        <div className="fs-set__card" data-testid="model-router-decision">
          <p>
            <strong>{result.model ?? t('No local model chosen')}</strong>
            {result.escalated && (
              <span className="fs-notice" data-tone="danger" style={{ display: 'inline-block', marginInlineStart: 'var(--fs-space-2)' }}>
                {t('Escalated — a paid model would be needed; nothing was chosen automatically.')}
              </span>
            )}
          </p>
          <p className="fs-set__help">{explain || result.reason}</p>
          {result.alternatives.length > 0 && (
            <div className="fs-mr__table-wrap">
              <table className="fs-mr__table">
                <thead>
                  <tr>
                    <th>{t('Model')}</th>
                    <th>{t('Score')}</th>
                    <th>{t('Why')}</th>
                    <th>{t('Meets requirements')}</th>
                  </tr>
                </thead>
                <tbody>
                  {result.alternatives.map((alt) => (
                    <tr key={alt.model}>
                      <td>{alt.model}</td>
                      <td>{alt.score.toFixed(2)}</td>
                      <td>{alt.why.join(' · ')}</td>
                      <td>{alt.meets_requirements ? t('Yes') : t('No')}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ModelRouterLogPanel() {
  const [entries, setEntries] = useState<ModelRouterLogEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => {
    setError(null);
    getModelRouterLog(50)
      .then((r) => setEntries(r.entries))
      .catch((e: unknown) => setError((e as Error).message));
  }, []);
  useEffect(() => void load(), [load]);

  return (
    <div className="fs-set__card">
      <div className="fs-set__row-actions">
        <h3 className="fs-set__card-title">{t('Log')}</h3>
        <span className="fs-set__spacer" />
        <Button size="sm" variant="ghost" label={t('Refresh')} onClick={() => void load()} />
      </div>
      {error && <p className="fs-set__err" role="alert">{error}</p>}
      {!entries ? (
        <Skeleton label={t('Loading')} count={3} height="32px" />
      ) : entries.length === 0 ? (
        <p className="fs-set__help">{t('No decisions logged yet.')}</p>
      ) : (
        <div className="fs-mr__table-wrap">
          <table className="fs-mr__table">
            <thead>
              <tr>
                <th>{t('When')}</th>
                <th>{t('Requested')}</th>
                <th>{t('Chosen')}</th>
                <th>{t('Reason')}</th>
                <th>{t('Escalated')}</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((e, i) => (
                <tr key={i}>
                  <td>{e.ts ?? ''}</td>
                  <td>{e.requested ?? ''}</td>
                  <td>{e.chosen ?? t('None')}</td>
                  <td>{e.reason ?? ''}</td>
                  <td>{e.escalated ? t('Yes') : t('No')}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function ModelRouterStatsPanel() {
  const [stats, setStats] = useState<Record<string, ModelRouterStatsEntry> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => {
    setError(null);
    getModelRouterStats()
      .then((r) => setStats(r.stats))
      .catch((e: unknown) => setError((e as Error).message));
  }, []);
  useEffect(() => void load(), [load]);
  const rows = useMemo(() => Object.entries(stats ?? {}), [stats]);

  return (
    <div className="fs-set__card">
      <div className="fs-set__row-actions">
        <h3 className="fs-set__card-title">{t('Statistics')}</h3>
        <span className="fs-set__spacer" />
        <Button size="sm" variant="ghost" label={t('Refresh')} onClick={() => void load()} />
      </div>
      {error && <p className="fs-set__err" role="alert">{error}</p>}
      {!stats ? (
        <Skeleton label={t('Loading')} count={3} height="32px" />
      ) : rows.length === 0 ? (
        <p className="fs-set__help">{t('No history yet.')}</p>
      ) : (
        <div className="fs-mr__table-wrap">
          <table className="fs-mr__table">
            <thead>
              <tr>
                <th>{t('Model')}</th>
                <th>{t('OK')}</th>
                <th>{t('Failures')}</th>
                <th>{t('Avg. latency (s)')}</th>
                <th>{t('Last error')}</th>
                <th>{t('Updated')}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(([model, s]) => (
                <tr key={model}>
                  <td>{model}</td>
                  <td>{s.ok}</td>
                  <td>{s.fail}</td>
                  <td>{s.ewma_latency_s == null ? '—' : s.ewma_latency_s.toFixed(2)}</td>
                  <td>{s.last_error_class ?? '—'}</td>
                  <td>{s.updated_at}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
