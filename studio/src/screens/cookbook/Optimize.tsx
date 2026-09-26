import { AlertTriangle, Award, GitCompare, Play, RefreshCw, StopCircle, Undo2 } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Button, EmptyState, ExecutionTimeline } from '../../components';
import { executionMetricsFrom } from '../../adapters/chat';
import { endpointsFor } from '../../adapters/cookbook';
import type { ModelEndpoint } from '../../adapters/settings';
import {
  activateProfile,
  activationScopeTone,
  BenchApiError,
  canActivate,
  canPromote,
  cancelRun,
  compareRuns,
  deactivateProfile,
  estimateLabel,
  formatDelta,
  getActiveProfile,
  getProfiles,
  getRollbackProposal,
  getRun,
  isRunInFlight,
  listRuns,
  listSuites,
  planBench,
  promoteProfile,
  runStateLabel,
  runStateTone,
  startBench,
  verdictTone,
  type ActivationResult,
  type BenchmarkRun,
  type Comparison,
  type InferenceProfile,
  type ProfileObjective,
  type ProfilesFor,
  type RollbackProposal,
  type RunSample,
  type Suite,
} from '../../adapters/bench';
import { t, tn } from '../../i18n';
import { Field } from './parts';

/**
 * Lote B (CONTRATO_INF04.md) — "Optimize for my machine", a new section
 * inside Cookbook (`?t=optimize`). Confirms a plan and a budget before
 * anything runs (§01/§09): step 1 picks a target and a suite, step 2 turns
 * that into a `BenchmarkRun` in state `"planned"` — real evidence (cases
 * planned, an honest estimate) a person can read before spending anything —
 * and only THEN offers "Start benchmark"; step 3 compares two finished runs
 * and, only for a comparable `verdict === "improvement"`, offers to mark
 * the candidate profile recommended.
 *
 * The one rule every other part of this lote answers to: mounting this
 * screen, or a remount when the Cookbook tab is reselected, NEVER starts a
 * benchmark on its own — `useEffect` here only ever calls `getRun` (a
 * read), never `startBench`. `tests/test_inf04_bench_js.py` greps this
 * file for exactly that. The last run this browser was watching survives a
 * remount via `localStorage` (a per-viewer convenience, not authoritative
 * state — the server's `GET /runs/{id}` is), so switching Cookbook tabs and
 * coming back reconnects to progress instead of losing it.
 */

const ACTIVE_RUN_KEY = 'fs-bench-active-run';

const OBJECTIVES: { value: ProfileObjective; label: string; hint: string }[] = [
  { value: 'interactive', label: 'Interactive chat', hint: 'Spanish conversation, instruction following' },
  { value: 'coding_agent', label: 'Coding agent', hint: 'Strict JSON tool-call formatting' },
  { value: 'long_documents', label: 'Long documents', hint: 'Retrieval from a long embedded document' },
];

function readActiveRunId(): string | null {
  try {
    return window.localStorage.getItem(ACTIVE_RUN_KEY);
  } catch {
    return null;
  }
}

function writeActiveRunId(id: string | null): void {
  try {
    if (id) window.localStorage.setItem(ACTIVE_RUN_KEY, id);
    else window.localStorage.removeItem(ACTIVE_RUN_KEY);
  } catch {
    /* private mode */
  }
}

function errorMessage(e: unknown): string {
  if (e instanceof BenchApiError) return e.message;
  if (e instanceof Error) return e.message;
  return String(e);
}

function msLabel(ms: number | null): string {
  if (ms === null || !Number.isFinite(ms)) return t('not observed');
  return ms >= 1000 ? t('{n} s', { n: (ms / 1000).toFixed(1) }) : t('{n} ms', { n: String(Math.round(ms)) });
}

/** `ActivationResult.applied`/`.deferred`'s own `options` sub-dict keys —
 *  the actual option NAMES a person would recognise (`num_ctx`,
 *  `keep_alive`…), not the wrapper object's own top-level keys
 *  (`endpoint_id`/`model`/`scope`/`note`). */
function optionKeys(obj: Record<string, unknown>): string[] {
  const options = obj.options;
  return options && typeof options === 'object' ? Object.keys(options as Record<string, unknown>) : [];
}

function pctLabel(v: number | null): string {
  if (v === null || !Number.isFinite(v)) return t('not evaluated yet');
  return `${Math.round(v * 100)}%`;
}

function RunStateBadge({ run }: { run: BenchmarkRun }) {
  return (
    <span className="fs-ck__badge" data-tone={runStateTone(run.state) === 'neutral' ? undefined : runStateTone(run.state)} data-testid="bench-run-state">
      {runStateLabel(run.state)}
    </span>
  );
}

/** One sample row: quality tag, the error a case failed with (if any), and
 *  its own compact `ExecutionTimeline` once the case actually produced
 *  `ExecutionMetrics` — a case that errored before any metrics were
 *  collected shows only the error, never a fabricated empty timeline. */
function SampleRow({ sample }: { sample: RunSample }) {
  const execution = sample.metrics ? executionMetricsFrom(sample.metrics) : undefined;
  const tone = sample.quality.passed === true ? 'ok' : sample.quality.passed === false ? 'danger' : undefined;
  return (
    <li className="fs-ck__item" data-testid={`bench-sample-${sample.case_id}-${sample.repeat}`}>
      <div className="fs-ck__item-row">
        <span className="fs-ck__item-main">
          <span className="fs-ck__item-name">
            {sample.case_id} <span className="fs-muted">#{sample.repeat}</span>
          </span>
        </span>
        <span className="fs-ck__badge" data-tone={tone}>
          {sample.quality.passed === true ? t('passed') : sample.quality.passed === false ? t('failed') : t('pending')}
        </span>
      </div>
      <div className="fs-ck__item-body">
        {sample.error ? (
          <p className="fs-muted" role="alert">
            {sample.error}
          </p>
        ) : null}
        {sample.quality.failed_checks.length > 0 && <p className="fs-muted">{t('Failed checks')}: {sample.quality.failed_checks.join(', ')}</p>}
        {typeof sample.output_excerpt === 'string' && sample.output_excerpt.length > 0 && (
          <blockquote className="fs-ck__excerpt" data-testid={`bench-sample-excerpt-${sample.case_id}-${sample.repeat}`}>
            {sample.output_excerpt}
            {sample.output_chars != null && sample.output_chars > sample.output_excerpt.length ? '…' : ''}
          </blockquote>
        )}
        {typeof sample.thinking_chars === 'number' && sample.thinking_chars > 0 && (
          <p className="fs-muted">{t('{n} characters of thinking were set aside before judging', { n: String(sample.thinking_chars) })}</p>
        )}
        {execution && <ExecutionTimeline execution={execution} variant="compact" testId={`bench-sample-timeline-${sample.case_id}-${sample.repeat}`} />}
      </div>
    </li>
  );
}

function RunResult({ run }: { run: BenchmarkRun }) {
  const { summary } = run;
  return (
    <div className="fs-ck__panel" data-testid="bench-result">
      <h3 className="fs-ck__h">{t('Result')}</h3>
      <dl className="fs-ck__opt-stats">
        <div>
          <dt>{t('Cases run')}</dt>
          <dd>
            {summary.cases_run} / {summary.cases_planned}
          </dd>
        </div>
        <div>
          <dt>{t('Quality pass rate')}</dt>
          <dd>{pctLabel(summary.quality_pass_rate)}</dd>
        </div>
        <div>
          <dt>{t('Generation, tok/s (median · p95 · n)')}</dt>
          <dd>
            {summary.gen_tps.median ?? '—'} · {summary.gen_tps.p95 ?? '—'} · {summary.gen_tps.n}
          </dd>
        </div>
        <div>
          <dt>{t('Time to first token (median · p95 · n)')}</dt>
          <dd>
            {msLabel(summary.ttft_ms.median)} · {msLabel(summary.ttft_ms.p95)} · {summary.ttft_ms.n}
          </dd>
        </div>
      </dl>
      {run.interruptions.length > 0 && (
        <p className="fs-ck__note" data-testid="bench-interruptions">
          <AlertTriangle size={13} aria-hidden="true" />{' '}
          {run.interruptions.map((x) => x.reason).join(', ')} —{' '}
          {t('the budget ran out before every case finished; this is a partial result, never presented as a completed optimisation.')}
        </p>
      )}
      {run.samples.length > 0 && (
        <ul className="fs-ck__list" data-testid="bench-samples">
          {run.samples.map((s) => (
            <SampleRow key={`${s.case_id}-${s.repeat}`} sample={s} />
          ))}
        </ul>
      )}
    </div>
  );
}

/** One saved profile's row: its evaluation state, an "Active" chip when
 *  `GET /api/bench/profiles/active` names it, and "Activate"/"Deactivate" —
 *  §13's own gate (`canActivate`) applied here, not re-derived: only
 *  `evaluated`/`recommended` ever get an enabled "Activate" button, never
 *  `regression` (that one gets the comparator's rollback proposal instead,
 *  see `bench-rollback` below). */
function ProfileRow({
  profile, active, busy, onActivate, onDeactivate,
}: {
  profile: InferenceProfile;
  active: boolean;
  busy: boolean;
  onActivate: () => void;
  onDeactivate: () => void;
}) {
  return (
    <li className="fs-ck__item" data-testid={`bench-profile-${profile.id}`}>
      <div className="fs-ck__item-row">
        <span className="fs-ck__item-main">
          <span className="fs-ck__item-name">{profile.label}</span>
          <span className="fs-muted">{profile.evaluation}</span>
        </span>
        {active && (
          <span className="fs-ck__badge" data-tone="ok" data-testid="bench-active-chip">
            {t('Active')}
          </span>
        )}
        {active ? (
          <Button size="sm" variant="ghost" label={t('Deactivate')} loading={busy} onClick={onDeactivate} testId="bench-deactivate" />
        ) : (
          <Button
            size="sm"
            variant="secondary"
            label={t('Activate')}
            loading={busy}
            disabled={!canActivate(profile)}
            title={canActivate(profile) ? undefined : t('Only a measured (evaluated or recommended) profile can be activated')}
            onClick={onActivate}
            testId="bench-activate"
          />
        )}
      </div>
    </li>
  );
}

export interface OptimizeProps {
  say: (m: string) => void;
}

export function Optimize({ say }: OptimizeProps) {
  // ── catalogue (endpoints with installed models, and the suites) ──────────
  const [endpoints, setEndpoints] = useState<ModelEndpoint[]>([]);
  const [suites, setSuites] = useState<Suite[]>([]);
  const [catalogLoaded, setCatalogLoaded] = useState(false);

  useEffect(() => {
    let alive = true;
    Promise.all([endpointsFor(), listSuites().catch(() => [] as Suite[])]).then(([eps, sts]) => {
      if (!alive) return;
      setEndpoints(eps.filter((e) => e.enabled && e.modelType === 'llm' && e.models.length > 0));
      setSuites(sts);
      setCatalogLoaded(true);
    });
    return () => {
      alive = false;
    };
  }, []);

  // ── step 1: target ────────────────────────────────────────────────────────
  const [endpointId, setEndpointId] = useState('');
  const [model, setModel] = useState('');
  const [objective, setObjective] = useState<ProfileObjective>('interactive');
  const [suiteId, setSuiteId] = useState('');

  const endpoint = useMemo(() => endpoints.find((e) => e.id === endpointId) ?? null, [endpoints, endpointId]);
  const suitesForObjective = useMemo(() => suites.filter((s) => s.objective === objective), [suites, objective]);

  const chooseObjective = useCallback(
    (next: ProfileObjective) => {
      setObjective(next);
      const stillValid = suites.some((s) => s.id === suiteId && s.objective === next);
      if (!stillValid) setSuiteId(suites.find((s) => s.objective === next)?.id ?? '');
    },
    [suites, suiteId],
  );

  // ── activation (INF-05 B3, §13) ──────────────────────────────────────────
  // `refreshProfiles` is a plain read (GET `/api/bench/profiles`+`/active`)
  // and may run from the effect below like every other reconnect/refresh in
  // this file; `handleActivate`/`handleDeactivate` are the only calls to
  // `activateProfile`/`deactivateProfile` anywhere in this file, and both
  // are wired only to an explicit button `onClick` — never to a `useEffect`
  // (tests/test_inf05_hardware_js.py greps this file for exactly that, the
  // same T19 discipline `startBench` above already follows).
  const [profilesFor, setProfilesFor] = useState<ProfilesFor | null>(null);
  const [activeProfileId, setActiveProfileId] = useState<string | null>(null);
  const [activatingId, setActivatingId] = useState<string | null>(null);
  const [lastActivation, setLastActivation] = useState<ActivationResult | null>(null);

  const refreshProfiles = useCallback(() => {
    if (!endpoint || !model) {
      setProfilesFor(null);
      setActiveProfileId(null);
      return;
    }
    getProfiles(endpoint.baseUrl, model)
      .then(setProfilesFor)
      .catch(() => setProfilesFor(null));
    getActiveProfile(endpoint.baseUrl, model)
      .then(setActiveProfileId)
      .catch(() => setActiveProfileId(null));
  }, [endpoint, model]);

  useEffect(() => {
    refreshProfiles();
  }, [refreshProfiles]);

  const handleActivate = useCallback(
    async (profileId: string) => {
      setActivatingId(profileId);
      try {
        const result = await activateProfile(profileId);
        setLastActivation(result);
        say(result.note || t('Activated.'));
        refreshProfiles();
      } catch (e) {
        say(errorMessage(e));
      } finally {
        setActivatingId(null);
      }
    },
    [say, refreshProfiles],
  );

  // "Relaunch with this profile": the same `activateProfile` call
  // (§13 never launches a process itself — it only ever returns a plan),
  // wired to its own explicit button so a person can re-run it after they
  // have relaunched the server by hand with the deferred plan's options,
  // and get back an unambiguous "restart required" vs "relaunched" result
  // rather than re-reading the Activate button's generic note.
  const [relaunching, setRelaunching] = useState(false);
  const handleRelaunch = useCallback(
    async (profileId: string) => {
      setRelaunching(true);
      try {
        const result = await activateProfile(profileId);
        setLastActivation(result);
        const stillNeedsRestart = optionKeys(result.deferred).length > 0;
        say(stillNeedsRestart
          ? t('Restart required — {note}', { note: result.note })
          : t('Relaunched — {note}', { note: result.note }));
        refreshProfiles();
      } catch (e) {
        say(errorMessage(e));
      } finally {
        setRelaunching(false);
      }
    },
    [say, refreshProfiles],
  );

  const handleDeactivate = useCallback(
    async (profileId: string) => {
      setActivatingId(profileId);
      try {
        await deactivateProfile(profileId);
        say(t('Deactivated — the previous options are restored.'));
        refreshProfiles();
      } catch (e) {
        say(errorMessage(e));
      } finally {
        setActivatingId(null);
      }
    },
    [say, refreshProfiles],
  );

  const savedForModel = useMemo(
    () => (model ? (profilesFor?.saved ?? []).filter((p) => p.model.artifact_id === model) : []),
    [profilesFor, model],
  );

  // ── step 2: budget, plan, run ────────────────────────────────────────────
  const [maxCases, setMaxCases] = useState('');
  const [maxSeconds, setMaxSeconds] = useState('120');
  const [maxGeneratedTokens, setMaxGeneratedTokens] = useState('');
  const [repeats, setRepeats] = useState('3');

  const [run, setRun] = useState<BenchmarkRun | null>(null);
  const [planning, setPlanning] = useState(false);
  const [starting, setStarting] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [planError, setPlanError] = useState<string | null>(null);

  const hasBudget = Boolean(maxCases || maxSeconds || maxGeneratedTokens);
  const canPlan = Boolean(endpoint && model && suiteId && hasBudget && !planning);

  // Reconnect (T19): a run this browser was watching before the screen
  // unmounted (a Cookbook tab switch, a reload) is re-read here — GET only,
  // never started. Runs exactly once per mount.
  useEffect(() => {
    let alive = true;
    const id = readActiveRunId();
    if (!id) return;
    getRun(id)
      .then((r) => {
        if (alive) setRun(r);
      })
      .catch(() => writeActiveRunId(null));
    return () => {
      alive = false;
    };
  }, []);

  // Poll while (and only while) the run is actually moving. A run that just
  // came back from `planBench`/`startBench`/`cancelRun` already carries its
  // freshest known state, so this effect keys off `run.state` itself rather
  // than a separate flag — one interval per in-flight run, cleared on every
  // state change or unmount, never left to duplicate on a remount (the
  // reconnect effect above only ever calls `setRun` once).
  useEffect(() => {
    if (!run || !isRunInFlight(run.state)) return;
    const id = run.id;
    const timer = window.setInterval(() => {
      getRun(id)
        .then((r) => setRun(r))
        .catch(() => {
          /* a transient network hiccup; the next tick tries again */
        });
    }, 2000);
    return () => window.clearInterval(timer);
  }, [run?.id, run?.state]);

  const handlePlan = useCallback(async () => {
    if (!endpoint || !model || !suiteId) return;
    setPlanning(true);
    setPlanError(null);
    try {
      const planned = await planBench({
        endpointUrl: endpoint.baseUrl,
        model,
        suiteId,
        objective,
        budget: {
          maxCases: maxCases ? Number(maxCases) : undefined,
          maxSeconds: maxSeconds ? Number(maxSeconds) : undefined,
          maxGeneratedTokens: maxGeneratedTokens ? Number(maxGeneratedTokens) : undefined,
          repeats: repeats ? Number(repeats) : 1,
        },
      });
      setRun(planned);
      writeActiveRunId(planned.id);
    } catch (e) {
      setPlanError(errorMessage(e));
    } finally {
      setPlanning(false);
    }
  }, [endpoint, model, suiteId, objective, maxCases, maxSeconds, maxGeneratedTokens, repeats]);

  // The only call to `startBench` in this file — a direct response to the
  // person clicking the "Start benchmark" button, never from a useEffect.
  const handleStart = useCallback(async () => {
    if (!run) return;
    setStarting(true);
    setPlanError(null);
    try {
      await startBench(run.id);
      // The 202 response still carries `state: "planned"` (the background
      // task has only just been handed to the event loop) — a short,
      // bounded re-read loop picks up whatever state it has actually
      // reached (almost always within one or two ticks), so the polling
      // effect above has an in-flight state to key off instead of sitting
      // on a stale "planned" until its own next unrelated re-render.
      let fresh = await getRun(run.id);
      for (let attempt = 0; attempt < 5 && fresh.state === 'planned'; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 300));
        fresh = await getRun(run.id);
      }
      setRun(fresh);
    } catch (e) {
      setPlanError(errorMessage(e));
    } finally {
      setStarting(false);
    }
  }, [run]);

  const handleCancel = useCallback(async () => {
    if (!run) return;
    setCancelling(true);
    try {
      const cancelled = await cancelRun(run.id);
      setRun(cancelled);
    } catch (e) {
      say(errorMessage(e));
    } finally {
      setCancelling(false);
    }
  }, [run, say]);

  const startNew = useCallback(() => {
    setRun(null);
    setPlanError(null);
    writeActiveRunId(null);
  }, []);

  // ── step 3: comparator ───────────────────────────────────────────────────
  const [runHistory, setRunHistory] = useState<BenchmarkRun[]>([]);
  const [baselineId, setBaselineId] = useState('');
  const [candidateId, setCandidateId] = useState('');
  const [comparison, setComparison] = useState<Comparison | null>(null);
  const [comparing, setComparing] = useState(false);
  const [compareError, setCompareError] = useState<string | null>(null);
  const [promoting, setPromoting] = useState(false);

  const refreshHistory = useCallback(() => {
    listRuns(50)
      .then(setRunHistory)
      .catch(() => setRunHistory([]));
  }, []);

  useEffect(() => {
    refreshHistory();
  }, [refreshHistory]);

  // Once a watched run reaches a terminal state, the history list (and the
  // comparator's own dropdowns) refreshes so the run just finished is
  // pickable without a manual reload.
  useEffect(() => {
    if (run && !isRunInFlight(run.state) && run.state !== 'planned') refreshHistory();
  }, [run?.state, refreshHistory, run]);

  const candidateRun = useMemo(() => runHistory.find((r) => r.id === candidateId) ?? null, [runHistory, candidateId]);

  // §13: a measured `regression` may PROPOSE returning to the previous
  // profile — a read, refreshed automatically whenever the comparator
  // finds one; `bench-rollback` below is the only thing that ever acts on
  // it, and only on an explicit click.
  const [rollback, setRollback] = useState<RollbackProposal | null>(null);
  useEffect(() => {
    if (!comparison || comparison.verdict !== 'regression' || !candidateRun) {
      setRollback(null);
      return;
    }
    let alive = true;
    getRollbackProposal(candidateRun.profile.id)
      .then((r) => {
        if (alive) setRollback(r);
      })
      .catch(() => setRollback(null));
    return () => {
      alive = false;
    };
  }, [comparison, candidateRun]);

  const rollbackLabel = useMemo(() => {
    if (!rollback?.previous_profile_id) return null;
    const prev = savedForModel.find((p) => p.id === rollback.previous_profile_id);
    return prev?.label ?? rollback.previous_profile_id;
  }, [rollback, savedForModel]);

  const handleCompare = useCallback(async () => {
    if (!baselineId || !candidateId) return;
    setComparing(true);
    setCompareError(null);
    try {
      const c = await compareRuns(baselineId, candidateId);
      setComparison(c);
    } catch (e) {
      setCompareError(errorMessage(e));
    } finally {
      setComparing(false);
    }
  }, [baselineId, candidateId]);

  const handlePromote = useCallback(async () => {
    if (!comparison || !candidateRun) return;
    setPromoting(true);
    try {
      const profile = await promoteProfile(candidateRun.profile.id, comparison.candidate_run_id, comparison.baseline_run_id);
      say(t('"{label}" marked as recommended', { label: profile.label }));
    } catch (e) {
      say(errorMessage(e));
    } finally {
      setPromoting(false);
    }
  }, [comparison, candidateRun, say]);

  const handleKeepBaseline = useCallback(() => {
    // §13: any verdict other than "improvement" is recorded as what it is
    // (regression/inconclusive/no_change) and proposes a return to the
    // baseline, but changes nothing by itself. There is no server call to
    // make here — this button is the acknowledgement, not an action.
    say(t('Baseline kept — nothing was changed.'));
  }, [say]);

  return (
    <div className="fs-ck__opt" data-testid="bench-optimize">
      <p className="fs-prose">
        {t(
          'Measure a configuration that is already running, on the suites below, before deciding anything. Opening this screen never starts a benchmark by itself — only "Start benchmark" does, after you have seen the plan.',
        )}
      </p>

      <section className="fs-ck__group" aria-labelledby="bench-h1">
        <h2 className="fs-ck__h" id="bench-h1">
          {t('1. What to measure')}
        </h2>
        {catalogLoaded && endpoints.length === 0 ? (
          <EmptyState
            title={t('No installed models to benchmark')}
            body={t('Serve a model from Models or Fit first — this bench only measures configurations that are already installed and running, never a download.')}
          />
        ) : (
          <div className="fs-ck__grid">
            <Field label={t('Endpoint')}>
              <select
                className="fs-field"
                value={endpointId}
                onChange={(e) => {
                  setEndpointId(e.target.value);
                  setModel('');
                }}
                data-testid="bench-endpoint"
              >
                <option value="">{t('Choose…')}</option>
                {endpoints.map((e) => (
                  <option key={e.id} value={e.id}>
                    {e.name || e.baseUrl}
                  </option>
                ))}
              </select>
            </Field>
            <Field label={t('Model')}>
              <select className="fs-field" value={model} onChange={(e) => setModel(e.target.value)} disabled={!endpoint} data-testid="bench-model">
                <option value="">{t('Choose…')}</option>
                {(endpoint?.models ?? []).map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
            </Field>
            <Field label={t('Objective')}>
              <select className="fs-field" value={objective} onChange={(e) => chooseObjective(e.target.value as ProfileObjective)} data-testid="bench-objective">
                {OBJECTIVES.map((o) => (
                  <option key={o.value} value={o.value} title={t(o.hint)}>
                    {t(o.label)}
                  </option>
                ))}
              </select>
            </Field>
            <Field label={t('Suite')}>
              <select className="fs-field" value={suiteId} onChange={(e) => setSuiteId(e.target.value)} data-testid="bench-suite">
                <option value="">{t('Choose…')}</option>
                {suitesForObjective.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.id} · v{s.version} · {tn(s.cases.length, '{n} case', '{n} cases', { n: s.cases.length })}
                  </option>
                ))}
              </select>
            </Field>
          </div>
        )}
      </section>

      {savedForModel.length > 0 && (
        <section className="fs-ck__group" aria-labelledby="bench-h-profiles">
          <h2 className="fs-ck__h" id="bench-h-profiles">
            {t('Saved profiles for this target')}
          </h2>
          <ul className="fs-ck__list" data-testid="bench-profiles">
            {savedForModel.map((p) => (
              <ProfileRow
                key={p.id}
                profile={p}
                active={activeProfileId === p.id}
                busy={activatingId === p.id}
                onActivate={() => void handleActivate(p.id)}
                onDeactivate={() => void handleDeactivate(p.id)}
              />
            ))}
          </ul>
          {lastActivation && (
            <p className="fs-ck__note" data-testid="bench-activation-result">
              <span className="fs-ck__badge" data-tone={activationScopeTone(lastActivation.scope)}>
                {lastActivation.scope === 'next_request' ? t('Applies now') : t('Needs a relaunch')}
              </span>{' '}
              {lastActivation.note}
              {optionKeys(lastActivation.applied).length > 0 && ` · ${t('Applied: {keys}', { keys: optionKeys(lastActivation.applied).join(', ') })}`}
              {optionKeys(lastActivation.deferred).length > 0 && ` · ${t('Deferred: {keys}', { keys: optionKeys(lastActivation.deferred).join(', ') })}`}
              {optionKeys(lastActivation.deferred).length > 0 && (
                <>
                  {' '}
                  <Button
                    size="sm"
                    variant="secondary"
                    label={t('Relaunch with this profile')}
                    loading={relaunching}
                    onClick={() => void handleRelaunch(lastActivation.profile_id)}
                    testId="bench-relaunch"
                  />
                </>
              )}
            </p>
          )}
        </section>
      )}

      <section className="fs-ck__group" aria-labelledby="bench-h2">
        <h2 className="fs-ck__h" id="bench-h2">
          {t('2. Budget and run')}
        </h2>
        <div className="fs-ck__grid">
          <Field label={t('Max cases')} hint={t('Leave blank for every case in the suite (times repeats)')}>
            <input className="fs-field" inputMode="numeric" value={maxCases} onChange={(e) => setMaxCases(e.target.value.replace(/[^0-9]/g, ''))} data-testid="bench-max-cases" />
          </Field>
          <Field label={t('Max seconds')}>
            <input className="fs-field" inputMode="numeric" value={maxSeconds} onChange={(e) => setMaxSeconds(e.target.value.replace(/[^0-9]/g, ''))} data-testid="bench-max-seconds" />
          </Field>
          <Field label={t('Max generated tokens')}>
            <input className="fs-field" inputMode="numeric" value={maxGeneratedTokens} onChange={(e) => setMaxGeneratedTokens(e.target.value.replace(/[^0-9]/g, ''))} data-testid="bench-max-tokens" />
          </Field>
          <Field label={t('Repeats per case')} hint={t('3 is the exploratory starting point this bench is built around')}>
            <input className="fs-field" inputMode="numeric" value={repeats} onChange={(e) => setRepeats(e.target.value.replace(/[^0-9]/g, ''))} data-testid="bench-repeats" />
          </Field>
        </div>
        {!hasBudget && <p className="fs-ck__note">{t('At least one of max cases, max seconds or max generated tokens is required.')}</p>}
        <div className="fs-ck__serve-actions">
          <Button label={t('Get plan')} icon={RefreshCw} onClick={() => void handlePlan()} loading={planning} disabled={!canPlan} testId="bench-plan" />
          {run && (
            <Button label={t('Start over')} variant="ghost" onClick={startNew} testId="bench-reset" />
          )}
        </div>
        {planError && (
          <p className="fs-ck__note" role="alert" data-testid="bench-plan-error">
            <AlertTriangle size={13} aria-hidden="true" /> {planError}
          </p>
        )}

        {run && (
          <div className="fs-ck__panel" data-testid="bench-plan-result">
            <div className="fs-ck__item-row">
              <span className="fs-ck__item-main">
                <span className="fs-ck__item-name">
                  {run.suite_id} · v{run.suite_version} — {run.profile.model.artifact_id}
                </span>
              </span>
              <RunStateBadge run={run} />
            </div>
            <dl className="fs-ck__opt-stats">
              <div>
                <dt>{t('Cases planned')}</dt>
                <dd data-testid="bench-cases-planned">{run.summary.cases_planned}</dd>
              </div>
              <div>
                <dt>{t('Estimated duration')}</dt>
                <dd data-testid="bench-estimate">{estimateLabel(run.summary.estimate_seconds)}</dd>
              </div>
              <div>
                <dt>{t('Processes affected')}</dt>
                <dd>{t('none: measures the running configuration')}</dd>
              </div>
            </dl>

            {run.state === 'planned' && (
              <div className="fs-ck__serve-actions">
                <Button label={t('Start benchmark')} icon={Play} variant="primary" onClick={() => void handleStart()} loading={starting} testId="bench-start" />
              </div>
            )}

            {isRunInFlight(run.state) && (
              <div className="fs-ck__serve-actions">
                <p className="fs-muted" data-testid="bench-progress">
                  {t('{done} of {total} cases so far', { done: Math.max(run.summary.cases_run, run.samples.length), total: run.summary.cases_planned })}
                </p>
                <Button label={t('Cancel')} icon={StopCircle} variant="danger" onClick={() => void handleCancel()} loading={cancelling} testId="bench-cancel" />
              </div>
            )}

            {!isRunInFlight(run.state) && run.state !== 'planned' && <RunResult run={run} />}
          </div>
        )}
      </section>

      <section className="fs-ck__group" aria-labelledby="bench-h3">
        <h2 className="fs-ck__h" id="bench-h3">
          {t('3. Compare against a baseline')}
        </h2>
        <div className="fs-ck__grid">
          <Field label={t('Baseline run')}>
            <select className="fs-field" value={baselineId} onChange={(e) => setBaselineId(e.target.value)} data-testid="bench-baseline">
              <option value="">{t('Choose…')}</option>
              {runHistory.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.id} · {r.profile.model.artifact_id} · {r.suite_id} · {runStateLabel(r.state)}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t('Candidate run')}>
            <select className="fs-field" value={candidateId} onChange={(e) => setCandidateId(e.target.value)} data-testid="bench-candidate">
              <option value="">{t('Choose…')}</option>
              {runHistory.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.id} · {r.profile.model.artifact_id} · {r.suite_id} · {runStateLabel(r.state)}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <div className="fs-ck__serve-actions">
          <Button label={t('Compare')} icon={GitCompare} onClick={() => void handleCompare()} loading={comparing} disabled={!baselineId || !candidateId} testId="bench-compare" />
          <Button label={t('Refresh runs')} variant="ghost" icon={RefreshCw} onClick={refreshHistory} testId="bench-refresh-runs" />
        </div>
        {compareError && (
          <p className="fs-ck__note" role="alert">
            <AlertTriangle size={13} aria-hidden="true" /> {compareError}
          </p>
        )}

        {comparison && (
          <div className="fs-ck__panel" data-testid="bench-comparison">
            <div className="fs-ck__item-row">
              <span className="fs-ck__item-main">
                <span className="fs-ck__item-name">{t('Verdict')}</span>
              </span>
              <span className="fs-ck__badge" data-tone={verdictTone(comparison.verdict)} data-testid="bench-verdict">
                {t(
                  { improvement: 'Improvement', regression: 'Regression', no_change: 'No change', inconclusive: 'Inconclusive' }[
                    comparison.verdict
                  ],
                )}
              </span>
            </div>
            {!comparison.comparable && (
              <p className="fs-ck__note" role="alert" data-testid="bench-not-comparable">
                <AlertTriangle size={13} aria-hidden="true" /> {t('These two runs are not comparable')}
                {comparison.reasons.length > 0 ? `: ${comparison.reasons.join(', ')}` : '.'}
              </p>
            )}
            {comparison.comparable && comparison.reasons.length > 0 && <p className="fs-ck__note">{comparison.reasons.join(', ')}</p>}
            <dl className="fs-ck__opt-stats">
              <div>
                <dt>{t('Generation tok/s, median change')}</dt>
                <dd>{formatDelta(comparison.deltas.gen_tps_median_pct)}</dd>
              </div>
              <div>
                <dt>{t('Time to first token, median change')}</dt>
                <dd>{formatDelta(comparison.deltas.ttft_median_pct)}</dd>
              </div>
              <div>
                <dt>{t('Quality pass rate change')}</dt>
                <dd>{comparison.deltas.quality_pass_rate_delta === null ? t('n/a') : `${(comparison.deltas.quality_pass_rate_delta * 100).toFixed(1)} pp`}</dd>
              </div>
              <div>
                <dt>{t('Sample sizes (baseline · candidate)')}</dt>
                <dd data-testid="bench-sample-sizes">
                  {comparison.sample_sizes.baseline} · {comparison.sample_sizes.candidate}
                </dd>
              </div>
            </dl>
            <div className="fs-ck__serve-actions">
              <Button
                label={t('Mark as recommended')}
                icon={Award}
                variant="primary"
                onClick={() => void handlePromote()}
                loading={promoting}
                disabled={!canPromote(comparison)}
                title={canPromote(comparison) ? undefined : t('Only available when the candidate is a measured improvement')}
                testId="bench-promote"
              />
              <Button label={t('Keep baseline')} icon={Undo2} variant="ghost" onClick={handleKeepBaseline} testId="bench-keep-baseline" />
              {candidateRun && activeProfileId !== candidateRun.profile.id && (
                <Button
                  label={t('Activate candidate')}
                  variant="secondary"
                  loading={activatingId === candidateRun.profile.id}
                  disabled={!canActivate(candidateRun.profile)}
                  title={canActivate(candidateRun.profile) ? undefined : t('Only a measured (evaluated or recommended) profile can be activated')}
                  onClick={() => void handleActivate(candidateRun.profile.id)}
                  testId="bench-activate"
                />
              )}
            </div>
            {comparison.verdict === 'regression' && rollback?.previous_profile_id && (
              <p className="fs-ck__note" data-testid="bench-rollback-note">
                {t('Proposal: return to {label}', { label: rollbackLabel ?? rollback.previous_profile_id })}{' '}
                <Button
                  size="sm"
                  variant="secondary"
                  label={t('Roll back')}
                  loading={activatingId === rollback.previous_profile_id}
                  onClick={() => rollback.previous_profile_id && void handleActivate(rollback.previous_profile_id)}
                  testId="bench-rollback"
                />
              </p>
            )}
          </div>
        )}
      </section>
    </div>
  );
}
