import { Check, FileText, Fish, Play, RefreshCw, Square, Trash2 } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router';
import { Button, EmptyState, Skeleton } from '../components';
import * as api from '../adapters/goldfish';
import { t } from '../i18n';
import './goldfish.css';

/**
 * Goldfish reviews: the side that holds the context (the elephant) writes and judges; fresh
 * workers that never saw the conversation (the goldfish) review. One form starts any of the six
 * flows; the run list and the run view show reviewers, findings, the elephant's decision on each
 * one and the reports, and wait for your decision when automatic fixing is off.
 */

const FLOWS: api.Flow[] = ['review', 'loop', 'draft', 'brainstorm', 'panel', 'drift'];

function Sev({ s }: { s: api.Severity }) {
  return <span className="fs-gf__sev" data-sev={s}>{api.severityLabel(s)}</span>;
}

function Counts({ c }: { c: api.Run['counts'] }) {
  if (!c) return null;
  return <span className="fs-gf__muted">{t('{b} blockers, {m} major, {n} nits', { b: c.blocker ?? 0, m: c.major ?? 0, n: c.nit ?? 0 })}</span>;
}

function NewRunForm({ meta, onStarted }: { meta: api.GoldfishMeta | null; onStarted: (id: string) => void }) {
  const [flow, setFlow] = useState<api.Flow>('loop');
  const [path, setPath] = useState('');
  const [text, setText] = useState('');
  const [brief, setBrief] = useState('');
  const [context, setContext] = useState('');
  const [files, setFiles] = useState('');
  const [reviewers, setReviewers] = useState<string[]>(['comprehension', 'design_critic', 'zoom_out']);
  const [preset, setPreset] = useState('full');
  const [rounds, setRounds] = useState(5);
  const [auto, setAuto] = useState(true);
  const [writeBack, setWriteBack] = useState(true);
  const [models, setModels] = useState<string[]>([]);
  const [panelMode, setPanelMode] = useState('defects');
  const [saveTo, setSaveTo] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const needsBrief = flow === 'draft' || flow === 'brainstorm';
  const toggle = (list: string[], v: string) => (list.includes(v) ? list.filter((x) => x !== v) : [...list, v]);
  const submit = async () => {
    setBusy(true); setError('');
    try {
      const run = await api.startRun({
        flow, path: path.trim() || undefined, text: text.trim() || undefined, brief: brief.trim() || undefined,
        context: context.trim() || undefined, context_files: files.split('\n').map((f) => f.trim()).filter(Boolean),
        reviewers: flow === 'loop' ? reviewers : undefined, preset: flow === 'review' ? preset : undefined, max_rounds: rounds,
        auto_apply: auto, write_back: writeBack, models: flow === 'panel' ? models : undefined, panel_mode: flow === 'panel' ? panelMode : undefined,
        save_to: flow === 'draft' && saveTo.trim() ? saveTo.trim() : undefined,
      });
      onStarted(run.id);
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  return (
    <section className="fs-gf__form" aria-label={t('New review')} data-testid="gf-form">
      <div className="fs-seg fs-gf__flows" role="radiogroup" aria-label={t('Flow')}>
        {FLOWS.map((f) => <button key={f} type="button" role="radio" aria-checked={flow === f} onClick={() => setFlow(f)} data-testid={`gf-flow-${f}`}>{api.flowLabel(f)}</button>)}
      </div>
      <p className="fs-gf__muted">{{
        review: t('Five stages, each with fresh reviewers: zoom out, comprehension, critic, zoom out again, implementation readiness. One consolidated report.'),
        loop: t('Fresh reviewers review, the elephant classifies each finding (fix, contest, escalate, decline) and fixes; new reviewers check again until two rounds in a row are clean.'),
        draft: t('The elephant writes the document section by section from your brief and context, checks it against the brief, and then runs the review loop on it.'),
        brainstorm: t('Seven fresh lenses look at a raw idea; the elephant writes a concepts brief with goals, alternatives and the decisions left to you.'),
        panel: t('Several different models review the same material blind. Only findings two models agree on, or that survive an attempt to disprove them, are reported.'),
        drift: t('A fresh reviewer compares the document with the source files it describes and lists what the code contradicts or the document leaves out.'),
      }[flow]}</p>
      {needsBrief
        ? <label className="fs-gf__field">{flow === 'draft' ? t('Brief or notes to write from') : t('The idea')}
            <textarea className="fs-field fs-gf__area" rows={4} value={brief} onChange={(e) => setBrief(e.target.value)} data-testid="gf-brief" /></label>
        : <>
            <label className="fs-gf__field">{t('Document (file path, or brain:Notes/x.md)')}
              <input className="fs-field" value={path} onChange={(e) => setPath(e.target.value)} data-testid="gf-path" /></label>
            {!path.trim() && <label className="fs-gf__field">{t('…or paste the text')}
              <textarea className="fs-field fs-gf__area" rows={4} value={text} onChange={(e) => setText(e.target.value)} data-testid="gf-text" /></label>}
          </>}
      <label className="fs-gf__field">{t('Context for the elephant only (decisions, constraints); the reviewers never see it')}
        <textarea className="fs-field fs-gf__area" rows={2} value={context} onChange={(e) => setContext(e.target.value)} data-testid="gf-context" /></label>
      <label className="fs-gf__field">{t('Reading list for the reviewers (files the document relies on, one per line)')}
        <textarea className="fs-field fs-gf__area" rows={2} value={files} onChange={(e) => setFiles(e.target.value)} /></label>
      {flow === 'loop' && meta && (
        <fieldset className="fs-gf__set"><legend>{t('Reviewers')}</legend>
          {Object.entries(meta.reviewers).map(([k, label]) => (
            <label key={k} className="fs-gf__check"><input type="checkbox" checked={reviewers.includes(k)} onChange={() => setReviewers((r) => toggle(r, k))} /> {t(label)}</label>))}
        </fieldset>)}
      {flow === 'panel' && meta && (
        <fieldset className="fs-gf__set"><legend>{t('Models (at least two)')}</legend>
          {meta.models.map((m) => (
            <label key={`${m.endpoint_id}/${m.model}`} className="fs-gf__check"><input type="checkbox" checked={models.includes(m.model)} onChange={() => setModels((r) => toggle(r, m.model))} /> {m.model}</label>))}
          <select className="fs-field" value={panelMode} onChange={(e) => setPanelMode(e.target.value)}>
            <option value="defects">{t('Look for defects')}</option>
            <option value="plan">{t('Critique a plan')}</option>
          </select>
        </fieldset>)}
      <div className="fs-gf__row">
        {flow === 'review' && <label className="fs-gf__field">{t('Stages')}
          <select className="fs-field" value={preset} onChange={(e) => setPreset(e.target.value)}>
            <option value="full">{t('All five')}</option>
            <option value="feature_spec">{t('Feature specification only')}</option>
            <option value="implementation">{t('Implementation plan only')}</option>
          </select></label>}
        {(flow === 'loop' || flow === 'draft') && <label className="fs-gf__field">{t('Maximum rounds')}
          <input className="fs-field fs-gf__num" type="number" min={1} max={5} value={rounds} onChange={(e) => setRounds(Number(e.target.value) || 5)} /></label>}
        {(flow === 'loop' || flow === 'draft') && <label className="fs-gf__check"><input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} data-testid="gf-auto" /> {t('Apply the elephant\'s fixes without asking')}</label>}
        {!needsBrief && <label className="fs-gf__check"><input type="checkbox" checked={writeBack} onChange={(e) => setWriteBack(e.target.checked)} /> {t('Write the fixes back to the file')}</label>}
        {flow === 'draft' && <label className="fs-gf__field fs-gf__grow">{t('Save the draft to (optional file path)')}
          <input className="fs-field" value={saveTo} onChange={(e) => setSaveTo(e.target.value)} /></label>}
      </div>
      {error && <p className="fs-gf__msg" data-tone="bad" role="alert">{error}</p>}
      <div><Button variant="primary" icon={Play} label={t('Start')} loading={busy} disabled={needsBrief ? !brief.trim() : !(path.trim() || text.trim())}
        onClick={() => { void submit(); }} testId="gf-start" /></div>
    </section>
  );
}

function FindingItem({ f, decision, choice, onChoice }: { f: api.Finding; decision?: api.Decision; choice?: string; onChoice?: (v: string) => void }) {
  const key = f.cid ?? f.id;
  return (
    <li className="fs-gf__finding" data-testid={`gf-finding-${key}`}>
      <div className="fs-gf__row">
        <Sev s={f.severity} />
        <strong>{key}. {f.title}</strong>
        <span className="fs-gf__muted">{f.lines ? `${t('lines')} ${f.lines} · ` : ''}{(f.reviewers ?? [f.reviewer]).join(', ')}{f.state && f.state !== 'new' ? ` · ${t(f.state)}` : ''}</span>
      </div>
      <p>{f.description}</p>
      {f.rationale && <p className="fs-gf__muted">{f.rationale}</p>}
      {decision && (
        <div className="fs-gf__decision">
          <span className="fs-gf__cat" data-cat={decision.category}>{t(decision.category)}</span> <span className="fs-gf__muted">{decision.justification}</span>
          {decision.edits.map((e, i) => <div key={i} className="fs-gf__edit"><del>{e.find}</del><ins>{e.replace}</ins>{e.status ? <span className="fs-gf__muted"> ({t(e.status)})</span> : null}</div>)}
          {onChoice && (
            <select className="fs-field fs-gf__choice" value={choice ?? ''} onChange={(e) => onChoice(e.target.value)} data-testid={`gf-choice-${key}`}>
              <option value="">{t('Follow the elephant')}</option>
              <option value="accept">{t('Apply the fix')}</option>
              <option value="skip">{t('Skip')}</option>
              <option value="overrule">{t('Overrule and fix')}</option>
            </select>)}
        </div>)}
    </li>
  );
}

function RunView({ id, onChanged, onDeleted }: { id: string; onChanged: () => void; onDeleted: () => void }) {
  const [run, setRun] = useState<api.Run | null>(null);
  const [error, setError] = useState('');
  const [artifact, setArtifact] = useState<{ name: string; text: string } | null>(null);
  const [choices, setChoices] = useState<Record<string, string>>({});
  const timer = useRef<number | null>(null);
  const load = useCallback(async () => {
    try { setRun(await api.getRun(id)); setError(''); } catch (e) { setError((e as Error).message); }
  }, [id]);
  useEffect(() => { setRun(null); setArtifact(null); setChoices({}); void load(); }, [load]);
  useEffect(() => {
    const live = run && (run.status === 'running' || run.status === 'queued');
    if (live && timer.current === null) timer.current = window.setInterval(() => { void load(); }, 3000);
    if (!live && timer.current !== null) { window.clearInterval(timer.current); timer.current = null; onChanged(); }
  }, [run, load, onChanged]);
  useEffect(() => () => { if (timer.current !== null) window.clearInterval(timer.current); }, []);
  const openArtifact = async (name: string) => {
    try { const r = await api.getRun(id, name); setArtifact(r.artifact ?? null); } catch (e) { setError((e as Error).message); }
  };
  const lastRound = run?.rounds?.length ? run.rounds[run.rounds.length - 1] : null;
  const waiting = run?.status === 'awaiting_decision';
  const files = useMemo(() => {
    if (!run) return [] as string[];
    const out = [run.consolidated_file, run.final_file, run.reconciliation_file, ...(run.rounds ?? []).map((r) => r.file), ...(run.stages_done ?? []).map((s) => s.file)];
    return out.filter((x): x is string => Boolean(x)).filter((x, i, a) => a.indexOf(x) === i);
  }, [run]);
  if (error && !run) return <p className="fs-gf__msg" data-tone="bad">{error}</p>;
  if (!run) return <Skeleton label={t('Reading the run')} count={2} height="80px" />;
  const resolve = async (mode: 'auto_accept' | 'continue' | 'decisions') => {
    const decisions = Object.fromEntries(Object.entries(choices).filter(([, v]) => v));
    await api.resolveRun(id, mode === 'decisions' ? { mode, decisions } : { mode });
    setChoices({});
    window.setTimeout(() => { void load(); }, 800);
  };
  return (
    <article className="fs-gf__run" data-testid="gf-run">
      <header className="fs-gf__row">
        <h2 className="fs-gf__h">{run.title}</h2>
        <span className="fs-gf__pill" data-status={run.status} data-testid="gf-status">{t(run.status)}</span>
        <span className="fs-gf__muted">{api.flowLabel(run.flow)} · {api.phaseLabel(run.phase)}{run.outcome ? ` · ${t(run.outcome)}` : ''}</span>
        <Counts c={run.counts} />
        <span className="fs-gf__grow" />
        <Button variant="ghost" size="sm" icon={RefreshCw} label={t('Refresh')} onClick={() => { void load(); }} />
        {api.isLive(run.status)
          ? <Button variant="ghost" size="sm" icon={Square} label={t('Cancel')} onClick={() => { void api.cancelRun(id).then(load); }} />
          : <Button variant="ghost" size="sm" icon={Trash2} label={t('Delete')} onClick={() => { if (window.confirm(t('Delete this run and its reports?'))) void api.deleteRun(id).then(onDeleted); }} testId="gf-delete" />}
      </header>
      <p className="fs-gf__muted">{run.target_label}{run.written_back ? ` · ${t('fixes written back')}` : ''}{run.models?.length ? ` · ${run.models.join(', ')}` : ''}</p>
      {run.error && <p className="fs-gf__msg" data-tone="bad">{run.error}</p>}
      {run.log.length > 0 && <pre className="fs-gf__log" data-testid="gf-log">{run.log.slice(-6).map((l) => `${l.at}  ${l.msg}`).join('\n')}</pre>}
      {run.stages_done && run.stages_done.length > 0 && (
        <table className="fs-gf__table"><thead><tr><th>{t('Stage')}</th><th>{t('Reviewers')}</th><th>{t('Findings')}</th></tr></thead>
          <tbody>{run.stages_done.map((s) => <tr key={s.stage}><td>{s.stage}. {t(s.name)}</td>
            <td className="fs-gf__muted">{s.skipped ? t('skipped') : Object.entries(s.statuses ?? {}).map(([k, v]) => `${k}: ${t(v)}`).join(' · ')}</td>
            <td>{s.findings?.length ?? 0}</td></tr>)}</tbody></table>)}
      {run.rounds && run.rounds.length > 0 && (
        <table className="fs-gf__table"><thead><tr><th>{t('Round')}</th><th>{t('Reviewers')}</th><th>{t('Clean')}</th><th>{t('Fixes applied')}</th></tr></thead>
          <tbody>{run.rounds.map((r) => <tr key={r.round}><td>{r.round}</td>
            <td className="fs-gf__muted">{Object.entries(r.statuses).map(([k, v]) => `${k}: ${t(v)}`).join(' · ')}</td>
            <td>{r.clean ? t('yes') : t('no')}</td><td>{r.applied?.length ?? 0}</td></tr>)}</tbody></table>)}
      {waiting && lastRound && (
        <section className="fs-gf__waiting" data-testid="gf-waiting">
          <p>{t('The elephant has classified the findings of round {n}. Choose what to apply.', { n: lastRound.round })}</p>
          <ul className="fs-gf__findings">{lastRound.findings.filter((f) => f.state !== 'settled').map((f) => (
            <FindingItem key={f.cid ?? f.id} f={f} decision={lastRound.decisions?.[f.cid ?? f.id]} choice={choices[f.cid ?? f.id]}
              onChoice={(v) => setChoices((c) => ({ ...c, [f.cid ?? f.id]: v }))} />))}</ul>
          <div className="fs-gf__row">
            <Button variant="primary" size="sm" icon={Check} label={t('Apply what I chose')} onClick={() => { void resolve('decisions'); }} testId="gf-apply-chosen" />
            <Button variant="secondary" size="sm" label={t('Apply every proposed fix')} onClick={() => { void resolve('auto_accept'); }} testId="gf-accept-all" />
            <Button variant="ghost" size="sm" label={t('Continue without changes')} onClick={() => { void resolve('continue'); }} />
          </div>
        </section>)}
      {!waiting && (
        <>
          <h3 className="fs-gf__h3">{run.flow === 'loop' || run.flow === 'draft' ? t('Open after the last round') : t('Findings')}</h3>
          {run.findings.length === 0 ? <p className="fs-gf__muted">{api.isLive(run.status) ? t('Working…') : t('No findings.')}</p>
            : <ul className="fs-gf__findings">{run.findings.map((f) => <FindingItem key={f.cid ?? f.id} f={f} decision={run.decisions?.[f.cid ?? f.id]} />)}</ul>}
        </>)}
      {files.length > 0 && (
        <div className="fs-gf__row" data-testid="gf-files">
          {files.map((f) => <Button key={f} variant="ghost" size="sm" icon={FileText} label={f} onClick={() => { void openArtifact(f); }} />)}
        </div>)}
      {artifact && <pre className="fs-gf__doc" data-testid="gf-artifact">{artifact.text}</pre>}
    </article>
  );
}

export function GoldfishScreen() {
  const [params, setParams] = useSearchParams();
  const selected = params.get('run');
  const [rows, setRows] = useState<api.RunRow[] | null>(null);
  const [meta, setMeta] = useState<api.GoldfishMeta | null>(null);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState('');
  const reload = useCallback(async () => {
    try { setRows(await api.listRuns()); setError(''); } catch (e) { setError((e as Error).message); setRows((r) => r ?? []); }
  }, []);
  useEffect(() => { void reload(); void api.loadMeta().then(setMeta).catch(() => undefined); }, [reload]);
  const select = (id: string | null) => setParams((p) => { const n = new URLSearchParams(p); if (id) n.set('run', id); else n.delete('run'); return n; });
  return (
    <div className="fs-screen fs-gf" data-testid="goldfish">
      <header className="fs-screen__head">
        <div>
          <h1 className="fs-screen__title">{t('Goldfish reviews')}</h1>
          <p className="fs-prose fs-gf__lede">{t('Write with the side that holds the context, review with workers that have never seen the conversation. A fresh reviewer trips exactly where a future reader will: undefined names, decisions taken in chat and never written down, steps nobody could follow.')}</p>
        </div>
        <Button variant="primary" icon={Fish} label={t('New review')} onClick={() => setCreating((v) => !v)} testId="gf-new" />
      </header>
      {creating && <NewRunForm meta={meta} onStarted={(id) => { setCreating(false); void reload(); select(id); }} />}
      {error && <p className="fs-gf__msg" data-tone="bad" role="alert">{error}</p>}
      <div className="fs-gf__body">
        <aside className="fs-gf__list" aria-label={t('Runs')}>
          {rows === null && <Skeleton label={t('Reading the runs')} count={3} height="48px" />}
          {rows && rows.length === 0 && <EmptyState icon={Fish} title={t('No reviews yet')} body={t('Start one with a document, a brief or an idea.')} />}
          {rows && rows.map((r) => (
            <button key={r.id} type="button" className="fs-gf__item" aria-current={selected === r.id} onClick={() => select(r.id)} data-testid={`gf-item-${r.id}`}>
              <span className="fs-gf__item-title">{r.title}</span>
              <span className="fs-gf__muted">{api.flowLabel(r.flow)} · {t(r.status)} · {r.created.replace('T', ' ')}</span>
              <Counts c={r.counts} />
            </button>))}
        </aside>
        <main className="fs-gf__main">
          {selected ? <RunView id={selected} onChanged={() => { void reload(); }} onDeleted={() => { select(null); void reload(); }} /> : <p className="fs-gf__muted">{t('Choose a run, or start a new review.')}</p>}
        </main>
      </div>
    </div>
  );
}
