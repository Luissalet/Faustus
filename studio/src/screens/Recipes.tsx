import { BookOpenCheck, FlaskConical, Play, RefreshCw, ShieldCheck } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { useSearchParams } from 'react-router';
import { Button, EmptyState, Skeleton } from '../components';
import * as api from '../adapters/recipes';
import { t, tn } from '../i18n';
import './recipes.css';

/**
 * Recipes: what Faustus learned to do, and what happened each time it did it for real.
 *
 * Two rules of product shape the page.
 *
 * 1. A dry run ("Dry run") only renders the steps on a new case: nothing is
 *    executed and nothing is checked, so it is drawn in its own neutral frame
 *    with the word "structural" and never with the verified tone. Only a real
 *    execution that ended `verified` is drawn as verified.
 * 2. Every status is a word before it is a colour, and a verification that
 *    went stale says WHY (the recipe, its criteria, an input or a delivered
 *    file changed) instead of fading quietly.
 */

const fmt = (seconds: number): string => (seconds ? new Date(seconds * 1000).toLocaleString() : '—');
const argsText = (args: unknown): string => {
  try {
    const s = typeof args === 'string' ? args : JSON.stringify(args);
    return s.length > 160 ? `${s.slice(0, 160)}…` : s;
  } catch {
    return '';
  }
};

function StatusPill({ status, empty }: { status: string; empty?: string }) {
  const info = status ? api.statusInfo(status) : { label: empty ?? t('Never executed'), tone: 'unknown' as const };
  return <span className="fs-rcp__pill" data-tone={info.tone} data-status={status}>{info.label}</span>;
}

function RecipeList({ recipes, openId, onOpen }: { recipes: api.Recipe[]; openId: string; onOpen: (id: string) => void }) {
  return (
    <ul className="fs-rcp__list" aria-label={t('Learned recipes')}>
      {recipes.map((r) => {
        const last = r.executions[r.executions.length - 1];
        return (
          <li key={r.id}>
            <button type="button" className="fs-rcp__item" data-active={r.id === openId} onClick={() => onOpen(r.id)}>
              <span className="fs-rcp__item-title">{r.title}</span>
              <span className="fs-rcp__muted">
                {tn(r.stepTemplates.length, '{n} recipe step', '{n} recipe steps')} · {tn(r.params.length, '{n} recipe parameter', '{n} recipe parameters')} · {tn(r.exitCriteria.length, '{n} exit criterion', '{n} exit criteria')}
              </span>
              <StatusPill status={last ? last.status : ''} />
            </button>
          </li>
        );
      })}
    </ul>
  );
}

function Summary({ recipe }: { recipe: api.Recipe }) {
  return (
    <section className="fs-rcp__block" aria-label={t('What the recipe does')}>
      <h2 className="fs-rcp__h">{t('Steps')}</h2>
      <ol className="fs-rcp__steps">
        {recipe.stepTemplates.map((s) => (
          <li key={s.index}>
            <span className="fs-rcp__mono fs-rcp__tool">{s.tool}</span>
            <code className="fs-rcp__args">{argsText(s.args)}</code>
          </li>
        ))}
      </ol>
      <h2 className="fs-rcp__h">{t('Parameters')}</h2>
      {recipe.params.length === 0 ? <p className="fs-rcp__muted">{t('This recipe has no parameters.')}</p> : (
        <table className="fs-rcp__table">
          <thead><tr><th>{t('Name')}</th><th>{t('Kind')}</th><th>{t('First case')}</th></tr></thead>
          <tbody>
            {recipe.params.map((p) => (
              <tr key={p.name}>
                <td className="fs-rcp__mono">{p.name}</td><td>{p.kind}</td>
                <td className="fs-rcp__mono">{typeof p.example === 'string' ? p.example : JSON.stringify(p.example)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <h2 className="fs-rcp__h">{t('Exit criteria')}</h2>
      {recipe.exitCriteria.length === 0
        ? <p className="fs-rcp__muted">{t('No exit criteria declared: a real run will execute the steps but cannot verify anything.')}</p>
        : <ul className="fs-rcp__crit">{recipe.exitCriteria.map((c, i) => <li key={i} className="fs-rcp__mono">{argsText(c)}</li>)}</ul>}
    </section>
  );
}

function History({ recipe, cases }: { recipe: api.Recipe; cases: api.VerificationCase[] | null }) {
  const runs = [...recipe.executions].reverse().slice(0, 8);
  return (
    <section className="fs-rcp__block" aria-label={t('Real executions')}>
      <h2 className="fs-rcp__h">{t('Real executions')}</h2>
      {cases === null && <p className="fs-rcp__muted">{t('Reading the verifications…')}</p>}
      {cases && cases.length === 0 && <p className="fs-rcp__muted">{t('This recipe was never executed for real.')}</p>}
      {cases && cases.map((c) => (
        <div key={c.caseKey} className="fs-rcp__case" data-testid="recipe-case">
          <div className="fs-rcp__row">
            <StatusPill status={c.status} />
            {c.verified && (
              <span className="fs-rcp__pill" data-tone={c.fresh ? 'good' : 'warn'}>
                {c.fresh ? t('Verification current') : t('Verification stale')}
              </span>
            )}
            <span className="fs-rcp__muted">{fmt(c.at)}</span>
          </div>
          <code className="fs-rcp__args">{argsText(c.params)}</code>
          {c.stale && c.staleReasons.length > 0 && (
            <ul className="fs-rcp__why" data-tone="warn">{c.staleReasons.map((r) => <li key={r}>{r}</li>)}</ul>
          )}
        </div>
      ))}
      {runs.length > 1 && <p className="fs-rcp__muted">{t('{n} executions stored.', { n: recipe.executions.length })}</p>}
    </section>
  );
}

function RunResult({ report }: { report: api.RunReport }) {
  const verified = api.canClaimVerified(report);
  return (
    <section
      className="fs-rcp__result" data-testid="recipe-result"
      data-kind={report.structural ? 'structural' : 'real'}
      data-tone={report.structural ? 'unknown' : api.statusInfo(report.status).tone}
      aria-live="polite"
    >
      {report.structural ? (
        <>
          <h2 className="fs-rcp__h"><FlaskConical size={14} aria-hidden /> {t('Dry run: structural check only')}</h2>
          <p className="fs-rcp__muted">
            {t('Nothing was executed and nothing was checked. This only shows whether each step renders with these parameters. It is not a verification.')}
          </p>
        </>
      ) : (
        <h2 className="fs-rcp__h">
          <ShieldCheck size={14} aria-hidden /> {t('Real execution')} <StatusPill status={report.status} />
          {verified && <span className="fs-rcp__muted">{t('every step finished and every criterion passed')}</span>}
        </h2>
      )}
      {!report.structural && (
        <p className="fs-rcp__muted">
          {t('{ran} executed now · {reused} reused · {skipped} not run', { ran: report.ranNow, reused: report.reused, skipped: report.skipped })}
        </p>
      )}
      {report.problems.length > 0 && <ul className="fs-rcp__why" data-tone="bad">{report.problems.map((p) => <li key={p}>{p}</li>)}</ul>}
      {report.why.length > 0 && <ul className="fs-rcp__why" data-tone="warn">{report.why.map((p) => <li key={p}>{p}</li>)}</ul>}

      <h3 className="fs-rcp__h3">{t('Steps')}</h3>
      <ol className="fs-rcp__run-steps">
        {report.steps.map((s) => {
          const info = api.stepStatusInfo(s.status);
          return (
            <li key={`${s.index}-${s.tool}`} className="fs-rcp__run-step" data-testid="recipe-step">
              <div className="fs-rcp__row">
                <span className="fs-rcp__mono fs-rcp__tool">{s.tool}</span>
                <span className="fs-rcp__pill" data-tone={info.tone}>{info.label}</span>
                {s.waitedFor && <span className="fs-rcp__muted">{t('waited for')} {s.waitedFor}</span>}
              </div>
              {s.detail && <p className="fs-rcp__muted">{s.detail}</p>}
              {Object.keys(s.ids).length > 0 && (
                <p className="fs-rcp__mono fs-rcp__muted">{Object.entries(s.ids).map(([k, v]) => `${k}=${v}`).join(' · ')}</p>
              )}
              {s.whyRun.length > 0 && <p className="fs-rcp__muted">{t('Why it ran')}: {s.whyRun.join('; ')}</p>}
              {s.output && <pre className="fs-rcp__out">{s.output}</pre>}
            </li>
          );
        })}
      </ol>

      {report.criteria.length > 0 && (
        <>
          <h3 className="fs-rcp__h3">{t('Criteria')}</h3>
          <ul className="fs-rcp__crit-res">
            {report.criteria.map((c, i) => (
              <li key={i} data-testid="recipe-criterion">
                <span className="fs-rcp__pill" data-tone={c.passed ? 'good' : 'bad'}>{c.passed ? t('Passed') : t('Criterion failed')}</span>
                <span className="fs-rcp__mono">{c.kind} {c.path}</span>
                {!c.passed && (c.error || c.expected || c.observed) && (
                  <span className="fs-rcp__muted">
                    {c.error ? c.error : `${t('expected')} ${c.expected} · ${t('observed')} ${c.observed}`}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

function RunForm({ recipe, onDone }: { recipe: api.Recipe; onDone: () => void }) {
  const [values, setValues] = useState<Record<string, string>>(() => api.initialParams(recipe));
  const [asJson, setAsJson] = useState(false);
  const [jsonText, setJsonText] = useState('');
  const [workspace, setWorkspace] = useState(api.activeWorkspace());
  const [criteriaText, setCriteriaText] = useState(() => api.criteriaText(recipe.exitCriteria));
  const [timeout, setTimeoutS] = useState('120');
  const [rerun, setRerun] = useState(false);
  const [busy, setBusy] = useState<'' | 'dry' | 'real'>('');
  const [error, setError] = useState('');
  const [report, setReport] = useState<api.RunReport | null>(null);

  function params(): Record<string, unknown> | null {
    if (asJson) {
      const parsed = api.parseParamsJson(jsonText);
      if (!parsed.ok) { setError(parsed.error); return null; }
      return parsed.value;
    }
    return api.typedParams(recipe, values);
  }

  function switchMode(json: boolean) {
    if (json) setJsonText(JSON.stringify(api.typedParams(recipe, values), null, 2));
    else {
      const parsed = api.parseParamsJson(jsonText);
      if (parsed.ok) {
        const next: Record<string, string> = {};
        for (const p of recipe.params) {
          const v = parsed.value[p.name];
          next[p.name] = v == null ? '' : typeof v === 'string' ? v : JSON.stringify(v);
        }
        setValues(next);
      }
    }
    setAsJson(json);
    setError('');
  }

  async function dry() {
    setError('');
    const p = params();
    if (!p) return;
    setBusy('dry');
    try {
      setReport(await api.dryRun(recipe.id, p));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy('');
    }
  }

  async function real() {
    setError('');
    const p = params();
    if (!p) return;
    const criteria = api.parseCriteriaJson(criteriaText);
    if (!criteria.ok) { setError(criteria.error); return; }
    if (!workspace.trim()) { setError(t('A real execution needs a workspace folder.')); return; }
    setBusy('real');
    try {
      setReport(await api.executeRecipe(recipe.id, {
        params: p, workspace: workspace.trim(), criteria: criteria.value,
        jobTimeout: Number(timeout) > 0 ? Number(timeout) : undefined, rerun,
      }));
      onDone();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy('');
    }
  }

  return (
    <section className="fs-rcp__block" aria-label={t('Run the recipe on a new case')}>
      <h2 className="fs-rcp__h">{t('Run on a new case')}</h2>
      <div className="fs-rcp__row">
        <Button variant={asJson ? 'ghost' : 'secondary'} size="sm" label={t('Fields')} onClick={() => switchMode(false)} />
        <Button variant={asJson ? 'secondary' : 'ghost'} size="sm" label="JSON" onClick={() => switchMode(true)} />
      </div>
      {asJson ? (
        <textarea
          className="fs-field fs-rcp__json" rows={6} spellCheck={false} aria-label={t('Parameters as JSON')}
          value={jsonText} onChange={(e) => setJsonText(e.target.value)}
        />
      ) : (
        <div className="fs-rcp__fields">
          {recipe.params.length === 0 && <p className="fs-rcp__muted">{t('This recipe has no parameters.')}</p>}
          {recipe.params.map((p) => (
            <label key={p.name} className="fs-rcp__field">
              <span>{p.name} <span className="fs-rcp__muted">({p.kind})</span></span>
              <input
                className="fs-field" value={values[p.name] ?? ''} data-testid={`recipe-param-${p.name}`}
                onChange={(e) => setValues({ ...values, [p.name]: e.target.value })}
              />
            </label>
          ))}
        </div>
      )}
      <p className="fs-rcp__muted">{t('Change at least the parameters that make this a different case from the first one.')}</p>

      <label className="fs-rcp__field">
        <span>{t('Workspace folder (real execution)')}</span>
        <input className="fs-field" value={workspace} placeholder={t('Absolute path of the active workspace')} onChange={(e) => setWorkspace(e.target.value)} />
      </label>
      <label className="fs-rcp__field">
        <span>{t('Exit criteria (JSON list)')}</span>
        <textarea
          className="fs-field fs-rcp__json" rows={5} spellCheck={false} value={criteriaText}
          placeholder={api.criteriaExample(recipe)} onChange={(e) => setCriteriaText(e.target.value)}
        />
      </label>
      <div className="fs-rcp__row">
        <label className="fs-rcp__inline">
          <span>{t('Wait for jobs (s)')}</span>
          <input className="fs-field fs-rcp__num" type="number" min={1} max={1800} value={timeout} onChange={(e) => setTimeoutS(e.target.value)} />
        </label>
        <label className="fs-rcp__inline">
          <input type="checkbox" checked={rerun} onChange={(e) => setRerun(e.target.checked)} />
          <span>{t('Run every step again, ignoring earlier runs of this case')}</span>
        </label>
      </div>

      <div className="fs-rcp__row">
        <Button
          variant="secondary" size="sm" icon={FlaskConical} label={t('Structural dry run')} loading={busy === 'dry'} disabled={busy !== ''}
          title={t('Structural check only: nothing is executed or verified.')} onClick={() => void dry()}
        />
        <Button
          variant="primary" size="sm" icon={Play} label={t('Run for real')} loading={busy === 'real'} disabled={busy !== ''}
          title={t('Executes the steps through the normal tools, with the usual permissions, and checks the exit criteria on the delivered files.')}
          onClick={() => void real()}
        />
      </div>
      {error && <p className="fs-rcp__why" data-tone="bad" role="alert">{error}</p>}
      {report && <RunResult report={report} />}
    </section>
  );
}

export function RecipesScreen() {
  const [params, setParams] = useSearchParams();
  const openId = params.get('r') ?? '';
  const [recipes, setRecipes] = useState<api.Recipe[] | null>(null);
  const [loadError, setLoadError] = useState('');
  const [recipe, setRecipe] = useState<api.Recipe | null>(null);
  const [cases, setCases] = useState<api.VerificationCase[] | null>(null);

  const reloadList = useCallback(async () => {
    try {
      setRecipes(await api.listRecipes());
      setLoadError('');
    } catch (e) {
      setLoadError((e as Error).message);
      setRecipes((cur) => cur ?? []);
    }
  }, []);

  const reloadOne = useCallback(async (id: string) => {
    if (!id) return;
    try {
      const [r, v] = await Promise.all([api.loadRecipe(id), api.loadVerification(id).catch(() => [] as api.VerificationCase[])]);
      setRecipe(r);
      setCases(v);
    } catch (e) {
      setLoadError((e as Error).message);
    }
  }, []);

  useEffect(() => { void reloadList(); }, [reloadList]);
  useEffect(() => {
    setRecipe(null);
    setCases(null);
    void reloadOne(openId);
  }, [openId, reloadOne]);

  const open = (id: string) => {
    const next = new URLSearchParams(params);
    next.set('r', id);
    setParams(next, { replace: false });
  };

  const afterRun = () => { void reloadOne(openId); void reloadList(); };

  return (
    <div className="fs-screen fs-rcp" data-testid="recipes">
      <header className="fs-screen__head">
        <div>
          <h1 className="fs-screen__title">{t('Recipes')}</h1>
          <p className="fs-prose fs-rcp__lede">
            {t('Procedures Faustus learned from work that went well. A dry run only checks that the steps render for a new case. A real run executes them through the normal tools and checks what was delivered against the exit criteria; only that can end as verified.')}
          </p>
        </div>
        <Button variant="secondary" size="sm" icon={RefreshCw} label={t('Reload')} onClick={() => { void reloadList(); void reloadOne(openId); }} />
      </header>

      {loadError && <p className="fs-rcp__why" data-tone="bad" role="alert">{loadError}</p>}
      {recipes === null && <Skeleton label={t('Reading the recipes')} count={3} height="56px" />}
      {recipes && recipes.length === 0 && !loadError && (
        <EmptyState
          icon={BookOpenCheck} title={t('No learned recipes yet')}
          body={t('When a task goes well, ask Faustus to learn it as a recipe; it will show up here with its parameters and exit criteria.')}
        />
      )}
      {recipes && recipes.length > 0 && (
        <div className="fs-rcp__layout">
          <RecipeList recipes={recipes} openId={openId} onOpen={open} />
          <div className="fs-rcp__detail">
            {!openId && <p className="fs-rcp__muted">{t('Choose a recipe to see its steps and run it.')}</p>}
            {openId && !recipe && !loadError && <Skeleton label={t('Reading the recipe')} count={3} height="56px" />}
            {recipe && (
              <>
                <h2 className="fs-rcp__title">{recipe.title}</h2>
                <Summary recipe={recipe} />
                <History recipe={recipe} cases={cases} />
                <RunForm key={`${recipe.id}:${recipe.version}`} recipe={recipe} onDone={afterRun} />
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
