import { useCallback, useEffect, useState } from 'react';
import { Button, Skeleton } from '../../components';
import { t } from '../../i18n';
import {
  deleteEvalSet, getEvalSet, listEvalSets, listEvaluations, putEvalSet, runEvaluation,
  type EvalCase, type EvalReport, type EvalSetSummary, type Scorer, type ScorerType,
} from '../../adapters/workflows';
import { JsonField } from './NodeForms';

/**
 * Evaluate: does a saved workflow still do what it should? A set is a list of
 * cases (inputs, optional node mocks) and scorers that read a path out of what
 * the run produced. Simulate is the default and touches no model, skill or
 * sender; a real evaluation needs the mode chosen and a box ticked, and the
 * report says which one it was so a simulated pass is never read as a model
 * pass.
 */

export interface EvaluatePanelProps {
  /** The library name of the workflow on screen. Evaluation runs saved workflows. */
  savedName: string | null;
}

const SCORER_TYPES: ScorerType[] = ['exact', 'contains', 'regex', 'json_schema', 'numeric', 'judge'];

function scorerLabel(type: ScorerType): string {
  switch (type) {
    case 'exact': return t('Equals');
    case 'contains': return t('Contains');
    case 'regex': return t('Matches pattern');
    case 'json_schema': return t('Fits a JSON schema');
    case 'numeric': return t('Number within tolerance');
    default: return t('Model judge');
  }
}

function looseText(v: unknown): string {
  if (v === undefined) return '';
  return typeof v === 'string' ? v : JSON.stringify(v);
}

function looseParse(text: string): unknown {
  if (text.trim() === '') return undefined;
  try { return JSON.parse(text); } catch { return text; }
}

function ScorerRow({ scorer, onChange, onRemove, index }: { scorer: Scorer; onChange: (s: Scorer) => void; onRemove: () => void; index: number }) {
  const set = (patch: Partial<Scorer>) => onChange({ ...scorer, ...patch });
  return (
    <div className="fs-form__check" data-testid={`eval-scorer-${index}`}>
      <div className="fs-form__check-head">
        <select className="fs-form__control" value={scorer.type} aria-label={t('Scorer type')} data-testid={`eval-scorer-type-${index}`}
          onChange={(e) => onChange({ type: e.target.value as ScorerType, path: scorer.path })}>
          {SCORER_TYPES.map((x) => <option key={x} value={x}>{scorerLabel(x)}</option>)}
        </select>
        <Button variant="ghost" size="sm" label={t('Remove')} onClick={onRemove} testId={`eval-scorer-remove-${index}`} />
      </div>
      <div className="fs-form__field">
        <label className="fs-form__label">{t('Path into the outputs')}</label>
        <input className="fs-form__control" spellCheck={false} value={scorer.path ?? ''} placeholder="reply.data.total" data-testid={`eval-scorer-path-${index}`}
          onChange={(e) => set({ path: e.target.value || undefined })} />
      </div>
      {(scorer.type === 'exact' || scorer.type === 'contains') && (
        <div className="fs-form__field">
          <label className="fs-form__label">{t('Expected value')}</label>
          <input className="fs-form__control" spellCheck={false} value={looseText(scorer.expected)} data-testid={`eval-scorer-expected-${index}`}
            onChange={(e) => set({ expected: looseParse(e.target.value) })} />
        </div>
      )}
      {scorer.type === 'regex' && (
        <div className="fs-form__field">
          <label className="fs-form__label">{t('Pattern')}</label>
          <input className="fs-form__control" spellCheck={false} value={scorer.pattern ?? ''} data-testid={`eval-scorer-pattern-${index}`}
            onChange={(e) => set({ pattern: e.target.value })} />
        </div>
      )}
      {scorer.type === 'json_schema' && (
        <JsonField label={t('Schema (JSON)')} value={scorer.schema} rows={5} testId={`eval-scorer-schema-${index}`}
          onChange={(v) => set({ schema: v as Record<string, unknown> | undefined })} />
      )}
      {scorer.type === 'numeric' && (
        <div className="fs-form__row">
          <div className="fs-form__field">
            <label className="fs-form__label">{t('Expected number')}</label>
            <input className="fs-form__control" type="number" value={typeof scorer.expected === 'number' ? scorer.expected : ''} data-testid={`eval-scorer-number-${index}`}
              onChange={(e) => set({ expected: e.target.value === '' ? undefined : Number(e.target.value) })} />
          </div>
          <div className="fs-form__field">
            <label className="fs-form__label">{t('Tolerance')}</label>
            <input className="fs-form__control" type="number" min={0} step="any" value={scorer.tolerance ?? ''} data-testid={`eval-scorer-tolerance-${index}`}
              onChange={(e) => set({ tolerance: e.target.value === '' ? undefined : Number(e.target.value) })} />
          </div>
          <label className="fs-form__inline">
            <input type="checkbox" checked={scorer.relative === true} onChange={(e) => set({ relative: e.target.checked || undefined })} />
            {t('Relative')}
          </label>
        </div>
      )}
      {scorer.type === 'judge' && (
        <div className="fs-form__field">
          <label className="fs-form__label">{t('Criteria')}</label>
          <textarea className="fs-form__control" rows={3} value={scorer.criteria ?? ''} data-testid={`eval-scorer-criteria-${index}`}
            onChange={(e) => set({ criteria: e.target.value })} />
        </div>
      )}
    </div>
  );
}

function pct(rate: number): string {
  return `${Math.round(rate * 100)}%`;
}

function seconds(ms: number): string {
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
}

export function ReportView({ report }: { report: EvalReport }) {
  const s = report.summary;
  return (
    <div className="fs-eval__report" data-testid="eval-report">
      <p className="fs-eval__summary" data-testid="eval-summary">
        <strong data-testid="eval-pass-rate">{pct(s.passRate)}</strong>{' '}
        {t('{p} passed, {f} failed, {e} errored of {n}', { p: s.passed, f: s.failed, e: s.error, n: s.total })}
        {' · '}
        <span className="fs-eval__mode" data-mode={s.mode} data-testid="eval-mode">{s.mode === 'real' ? t('real run') : t('simulated')}</span>
        {' · '}
        {t('{avg} average, {p95} slowest 5%', { avg: seconds(s.durationMsAvg), p95: seconds(s.durationMsP95) })}
      </p>
      {s.note && <p className="fs-form__hint">{s.note}</p>}
      {s.unscored > 0 && <p className="fs-form__hint">{t('{n} case(s) have no scorer: they only check that the run completes.', { n: s.unscored })}</p>}
      {Object.keys(s.byScorer).length > 0 && (
        <ul className="fs-eval__byscorer">
          {Object.entries(s.byScorer).map(([k, v]) => <li key={k}><code>{k}</code> {v.passed}/{v.total}</li>)}
        </ul>
      )}
      <table className="fs-plan__table" data-testid="eval-cases">
        <thead>
          <tr><th>{t('Case')}</th><th>{t('Result')}</th><th>{t('Scores')}</th><th>{t('Time')}</th></tr>
        </thead>
        <tbody>
          {report.cases.map((c) => (
            <tr key={c.id} data-testid={`eval-case-${c.id}`}>
              <td><code>{c.id}</code>{c.name && <div className="fs-form__hint">{c.name}</div>}</td>
              <td><span className="fs-eval__status" data-status={c.status}>{c.status === 'passed' ? t('passed') : c.status === 'failed' ? t('failed') : t('error')}</span></td>
              <td>
                {c.error && <div className="fs-form__error">{c.error}</div>}
                {c.note && <div className="fs-form__hint">{c.note}</div>}
                {c.scores.map((x, i) => (
                  <div key={i} className="fs-eval__score" data-passed={x.passed}>
                    <code>{x.type}{x.path ? ` ${x.path}` : ''}</code> {x.passed ? t('passed') : t('failed')}{x.detail ? `: ${x.detail}` : ''}
                  </div>
                ))}
                {c.output !== undefined && c.output !== null && (
                  <details><summary>{t('Output')}</summary><pre className="fs-library__pre">{JSON.stringify(c.output, null, 2)}</pre></details>
                )}
              </td>
              <td>{seconds(c.durationMs)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const newCase = (n: number): EvalCase => ({ id: `case-${n}`, inputs: {}, scorers: [] });

export function EvaluatePanel({ savedName }: EvaluatePanelProps) {
  const [sets, setSets] = useState<EvalSetSummary[] | null>(null);
  const [judgeOn, setJudgeOn] = useState(false);
  const [setName, setSetName] = useState('');
  const [scorers, setScorers] = useState<Scorer[]>([]);
  const [cases, setCases] = useState<EvalCase[]>([]);
  const [dirty, setDirty] = useState(false);
  const [mode, setMode] = useState<'simulate' | 'real'>('simulate');
  const [allowReal, setAllowReal] = useState(false);
  const [report, setReport] = useState<EvalReport | null>(null);
  const [history, setHistory] = useState<EvalReport[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const reload = useCallback(async (workflow: string) => {
    try {
      const [listing, past] = await Promise.all([listEvalSets(workflow), listEvaluations(workflow)]);
      setSets(listing.sets);
      setJudgeOn(listing.modelJudge);
      setHistory(past);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setSets((prev) => prev ?? []);
    }
  }, []);

  useEffect(() => {
    setSets(null); setReport(null); setSetName(''); setScorers([]); setCases([]); setDirty(false); setError(null); setNotice(null);
    if (savedName) void reload(savedName);
  }, [savedName, reload]);

  if (!savedName) {
    return (
      <section className="fs-eval" data-testid="eval-panel">
        <p className="fs-workflows__empty" data-testid="eval-needs-save">
          {t('Evaluation runs a saved workflow. Save this one to the library first, from the Design tab.')}
        </p>
      </section>
    );
  }
  const workflow = savedName;

  async function guarded(label: string, action: () => Promise<void>) {
    setBusy(label);
    setError(null);
    setNotice(null);
    try { await action(); } catch (e) { setError(e instanceof Error ? e.message : String(e)); } finally { setBusy(null); }
  }

  const openSet = (name: string) => guarded('open', async () => {
    const body = await getEvalSet(workflow, name);
    setSetName(name); setScorers(body.scorers); setCases(body.cases); setDirty(false); setReport(null);
  });

  const saveSet = () => guarded('save', async () => {
    await putEvalSet(workflow, setName.trim(), { scorers, cases });
    setDirty(false);
    setNotice(t('Set saved.'));
    await reload(workflow);
  });

  const removeSet = () => guarded('delete', async () => {
    await deleteEvalSet(workflow, setName.trim());
    setSetName(''); setScorers([]); setCases([]); setDirty(false);
    await reload(workflow);
  });

  const evaluate = () => guarded('run', async () => {
    const result = await runEvaluation(workflow, { set: setName.trim(), mode, allowReal: mode === 'real' && allowReal });
    setReport(result.report);
    if (!result.finished) setNotice(t('The evaluation is still running; open it from the history below when it finishes.'));
    await reload(workflow);
  });

  const edit = <T,>(setter: (v: T) => void) => (v: T) => { setter(v); setDirty(true); };
  const nameOk = /^[a-z0-9][a-z0-9_-]{0,47}$/.test(setName.trim());
  const canRun = nameOk && !dirty && cases.length > 0 && (mode === 'simulate' || allowReal);

  return (
    <section className="fs-eval" data-testid="eval-panel" aria-label={t('Evaluate')}>
      <p className="fs-form__hint">
        {t('Run cases through {name} and score what comes back. Simulation uses placeholders and proves the wiring; a real run reaches models and tools.', { name: workflow })}
      </p>
      {error && <p className="fs-workflows__error" role="alert" data-testid="eval-error">{error}</p>}
      {notice && <p className="fs-workflows__notice" data-testid="eval-notice">{notice}</p>}

      {sets === null && <Skeleton label={t('Loading evaluation sets')} count={2} height="32px" />}
      {sets && (
        <div className="fs-eval__sets" role="group" aria-label={t('Evaluation sets')}>
          {sets.map((x) => (
            <button key={x.name} type="button" className="fs-chip" data-on={x.name === setName || undefined} onClick={() => void openSet(x.name)} data-testid={`eval-set-${x.name}`}>
              {x.name} ({x.cases})
            </button>
          ))}
          <button type="button" className="fs-chip" onClick={() => { setSetName(`set-${sets.length + 1}`); setScorers([]); setCases([newCase(1)]); setDirty(true); setReport(null); }} data-testid="eval-new-set">
            {t('New set')}
          </button>
        </div>
      )}

      {(setName || cases.length > 0) && (
        <div className="fs-form" data-testid="eval-editor">
          <div className="fs-form__field">
            <label className="fs-form__label" htmlFor="eval-set-name">{t('Set name')}</label>
            <input id="eval-set-name" className="fs-form__control" value={setName} spellCheck={false} data-testid="eval-set-name"
              onChange={(e) => edit(setSetName)(e.target.value)} />
            {!nameOk && <p className="fs-form__error" role="alert">{t('Lowercase letters, digits, - and _, up to 48 characters.')}</p>}
          </div>

          <fieldset className="fs-form__group">
            <legend>{t('Scorers for every case')}</legend>
            {!judgeOn && scorers.some((s) => s.type === 'judge') && (
              <p className="fs-form__error" role="alert">{t('The model judge is off in settings, so a judge scorer will fail.')}</p>
            )}
            {scorers.map((s, i) => (
              <ScorerRow key={i} index={i} scorer={s}
                onChange={(next) => edit(setScorers)(scorers.map((x, at) => (at === i ? next : x)))}
                onRemove={() => edit(setScorers)(scorers.filter((_, at) => at !== i))} />
            ))}
            <Button variant="secondary" size="sm" label={t('Add scorer')} onClick={() => edit(setScorers)([...scorers, { type: 'exact' }])} testId="eval-add-scorer" />
          </fieldset>

          <fieldset className="fs-form__group">
            <legend>{t('Cases')}</legend>
            {cases.map((c, i) => (
              <div key={i} className="fs-form__check" data-testid={`eval-case-edit-${i}`}>
                <div className="fs-form__check-head">
                  <input className="fs-form__control" value={c.id} aria-label={t('Case id')} spellCheck={false} data-testid={`eval-case-id-${i}`}
                    onChange={(e) => edit(setCases)(cases.map((x, at) => (at === i ? { ...x, id: e.target.value } : x)))} />
                  <Button variant="ghost" size="sm" label={t('Remove')} onClick={() => edit(setCases)(cases.filter((_, at) => at !== i))} testId={`eval-case-remove-${i}`} />
                </div>
                <JsonField label={t('Inputs (JSON)')} value={c.inputs} rows={4} testId={`eval-case-inputs-${i}`}
                  onChange={(v) => edit(setCases)(cases.map((x, at) => (at === i ? { ...x, inputs: (v as Record<string, unknown>) ?? {} } : x)))} />
                <JsonField label={t('Mocked node results (JSON)')} value={c.mocks} rows={3} testId={`eval-case-mocks-${i}`}
                  hint={t('Optional. {"node_id": {...}} makes a simulated node answer this way, to drive one path.')}
                  onChange={(v) => edit(setCases)(cases.map((x, at) => (at === i ? { ...x, mocks: v as EvalCase['mocks'] } : x)))} />
                <JsonField label={t('Scorers for this case only (JSON list)')} value={c.scorers && c.scorers.length ? c.scorers : undefined} rows={3} asObjectOnly={false} testId={`eval-case-scorers-${i}`}
                  onChange={(v) => edit(setCases)(cases.map((x, at) => (at === i ? { ...x, scorers: Array.isArray(v) ? (v as Scorer[]) : [] } : x)))} />
              </div>
            ))}
            <Button variant="secondary" size="sm" label={t('Add case')} onClick={() => edit(setCases)([...cases, newCase(cases.length + 1)])} testId="eval-add-case" />
          </fieldset>

          <div className="fs-library__actions">
            <Button variant="primary" size="sm" label={t('Save set')} onClick={() => void saveSet()} loading={busy === 'save'} disabled={!nameOk || cases.length === 0} testId="eval-save-set" />
            <Button variant="danger" size="sm" label={t('Delete set')} onClick={() => void removeSet()} loading={busy === 'delete'} disabled={!sets?.some((x) => x.name === setName.trim())} testId="eval-delete-set" />
          </div>

          <fieldset className="fs-form__group" data-testid="eval-run-options">
            <legend>{t('Run')}</legend>
            <div className="fs-eval__modes" role="radiogroup" aria-label={t('Evaluation mode')}>
              <label className="fs-form__inline">
                <input type="radio" name="eval-mode" checked={mode === 'simulate'} onChange={() => { setMode('simulate'); setAllowReal(false); }} data-testid="eval-mode-simulate" />
                {t('Simulate: no model, skill or sender is touched')}
              </label>
              <label className="fs-form__inline">
                <input type="radio" name="eval-mode" checked={mode === 'real'} onChange={() => setMode('real')} data-testid="eval-mode-real" />
                {t('Real: start a real run for every case')}
              </label>
            </div>
            {mode === 'real' && (
              <label className="fs-form__inline fs-eval__confirm">
                <input type="checkbox" checked={allowReal} onChange={(e) => setAllowReal(e.target.checked)} data-testid="eval-allow-real" />
                {t('I understand this reaches models, skills and senders, and can have real effects.')}
              </label>
            )}
            <Button variant="primary" size="sm" label={mode === 'real' ? t('Run for real') : t('Run simulation')} onClick={() => void evaluate()} loading={busy === 'run'} disabled={!canRun} testId="eval-run" />
            {dirty && <p className="fs-form__hint">{t('Save the set before running it.')}</p>}
          </fieldset>
        </div>
      )}

      {report && <ReportView report={report} />}

      {history.length > 0 && (
        <div className="fs-eval__history" data-testid="eval-history">
          <h4>{t('Earlier evaluations')}</h4>
          <ul>
            {history.slice(0, 8).map((h) => (
              <li key={h.id}>
                <button type="button" className="fs-plan__list-btn" onClick={() => setReport(h)} data-testid={`eval-history-${h.id}`}>
                  {h.set} · {h.summary.mode === 'real' ? t('real run') : t('simulated')} · {pct(h.summary.passRate)} · {h.startedAt}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
