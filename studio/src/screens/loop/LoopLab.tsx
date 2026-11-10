import { FlaskConical, Play, Plus, RefreshCw, Save, Square, Trash2, Wand2 } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Button } from '../../components';
import * as api from '../../adapters/loopCards';
import { loadMeta, type GoldfishMeta } from '../../adapters/goldfish';
import { t } from '../../i18n';

/**
 * The rest of a Loop Card's loop: the plan written down before trying anything (keep bar, minimum
 * paired runs, guard command), experiment arms against the card's own command as control, the
 * improvement loop (hypotheses, proposals, isolated alternatives, paired measurement, apply the
 * winner) and the ledger where every attempt -- the rejected ones too -- stays.
 */

type Tab = 'plan' | 'arms' | 'improve' | 'ledger';

function ArmPill({ verdict }: { verdict: string }) {
  return <span className="fs-loop__pill" data-tone={api.armTone(verdict)} data-verdict={verdict}>{api.armLabel(verdict)}</span>;
}

function num(v: string): number | null {
  const n = Number(v.replace(',', '.'));
  return v.trim() === '' || Number.isNaN(n) ? null : n;
}

function PlanEditor({ card, onSaved }: { card: api.FullCard; onSaved: () => void }) {
  const [pct, setPct] = useState(card.keep_bar?.pct ? String(card.keep_bar.pct) : '');
  const [abs, setAbs] = useState(card.keep_bar?.abs ? String(card.keep_bar.abs) : '');
  const [pairs, setPairs] = useState(card.min_pairs ? String(card.min_pairs) : '');
  const [guard, setGuard] = useState(card.guard_command ?? '');
  const [stop, setStop] = useState(card.stop_rule ?? '');
  const [timebox, setTimebox] = useState(card.timebox_min ? String(card.timebox_min) : '');
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ tone: string; text: string } | null>(null);
  const save = async () => {
    setBusy(true); setMsg(null);
    try {
      await api.savePlan(card.id, { keep_bar_pct: num(pct), keep_bar_abs: num(abs), min_pairs: num(pairs), guard_command: guard,
        stop_rule: stop, timebox_min: num(timebox), reason: reason || undefined });
      setMsg({ tone: 'good', text: t('Plan saved.') });
      onSaved();
    } catch (e) { setMsg({ tone: 'bad', text: (e as Error).message }); } finally { setBusy(false); }
  };
  return (
    <div className="fs-lab__pane" data-testid={`lab-plan-${card.id}`}>
      <p className="fs-loop__muted">{t('Write down what counts as a win before trying any change. Moving the bar later is recorded in the ledger with its reason.')}</p>
      <div className="fs-loop__row">
        <label className="fs-loop__field">{t('Keep bar (% of the baseline)')}
          <input className="fs-field fs-loop__num" inputMode="decimal" value={pct} onChange={(e) => setPct(e.target.value)} data-testid="lab-keep-pct" />
        </label>
        <label className="fs-loop__field">{t('Keep bar (metric units)')}
          <input className="fs-field fs-loop__num" inputMode="decimal" value={abs} onChange={(e) => setAbs(e.target.value)} />
        </label>
        <label className="fs-loop__field">{t('Paired runs before a verdict')}
          <input className="fs-field fs-loop__num" inputMode="numeric" value={pairs} onChange={(e) => setPairs(e.target.value)} />
        </label>
        <label className="fs-loop__field">{t('Timebox (minutes)')}
          <input className="fs-field fs-loop__num" inputMode="numeric" value={timebox} onChange={(e) => setTimebox(e.target.value)} />
        </label>
      </div>
      <label className="fs-loop__field">{t('Guard command (must still pass after a change, e.g. the tests)')}
        <input className="fs-field" value={guard} onChange={(e) => setGuard(e.target.value)} data-testid="lab-guard" />
      </label>
      <label className="fs-loop__field">{t('Stop rule')}
        <input className="fs-field" value={stop} onChange={(e) => setStop(e.target.value)} />
      </label>
      <label className="fs-loop__field">{t('Reason for the change (if results already exist)')}
        <input className="fs-field" value={reason} onChange={(e) => setReason(e.target.value)} />
      </label>
      {msg && <p className="fs-loop__why" data-tone={msg.tone} role="status">{msg.text}</p>}
      <div><Button variant="secondary" size="sm" icon={Save} label={t('Save plan')} loading={busy} onClick={() => { void save(); }} testId="lab-plan-save" /></div>
    </div>
  );
}

export function ArmsTable({ stats }: { stats: api.ArmsStats }) {
  return (
    <div className="fs-lab__tablewrap">
      <p className="fs-loop__muted">
        {t('Control median {m}', { m: api.fmt(stats.control_median) })} · {t('keep bar {b}', { b: api.fmt(stats.bar) })}
        {stats.aa ? ` · ${t('A/A noise floor {n}', { n: api.fmt(stats.aa.noise_floor) })}` : ''}
      </p>
      <table className="fs-lab__table">
        <thead><tr><th>{t('Variant')}</th><th>{t('Verdict')}</th><th>{t('Median improvement')}</th><th>{t('Wins')}</th><th>{t('95 % interval')}</th><th>{t('Why')}</th></tr></thead>
        <tbody>{stats.arms.map((a) => (
          <tr key={a.arm} data-testid={`lab-arm-${a.arm}`}>
            <td><code>{a.arm}</code></td>
            <td><ArmPill verdict={a.verdict} /></td>
            <td>{api.fmt(a.median_delta)}{a.delta_pct !== null && a.delta_pct !== undefined ? ` (${a.delta_pct >= 0 ? '+' : ''}${api.fmt(a.delta_pct, 3)} %)` : ''}</td>
            <td>{a.wins ?? 0}/{a.pairs}</td>
            <td>{a.ci_low !== undefined && a.ci_low !== null ? `[${api.fmt(a.ci_low)}, ${api.fmt(a.ci_high)}]` : '–'}</td>
            <td className="fs-loop__muted">{a.why}</td>
          </tr>))}</tbody>
      </table>
      {stats.warnings.map((w, i) => <p key={i} className="fs-loop__why" data-tone="warn">{w}</p>)}
    </div>
  );
}

function ArmsPanel({ card, onDone }: { card: api.FullCard; onDone: () => void }) {
  const [arms, setArms] = useState<api.ArmSpec[]>([{ name: 'a1', command: card.command, cwd: '', label: '' }]);
  const [rounds, setRounds] = useState(6);
  const [aa, setAa] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<api.ArmsResult | null>(null);
  const set = (i: number, patch: Partial<api.ArmSpec>) => setArms((cur) => cur.map((a, j) => (j === i ? { ...a, ...patch } : a)));
  const run = async () => {
    setBusy(true); setError(''); setResult(null);
    try {
      setResult(await api.runArms(card.id, { arms: arms.map((a) => ({ ...a, cwd: a.cwd?.trim() || undefined })), rounds, aa }));
      onDone();
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  return (
    <div className="fs-lab__pane" data-testid={`lab-arms-${card.id}`}>
      <p className="fs-loop__muted">{t('Each variant runs once per round next to the control (the card\'s own command), in a shuffled order. A variant is kept only if it wins every pair, reaches the keep bar and its interval excludes zero.')}</p>
      {arms.map((a, i) => (
        <div className="fs-loop__row" key={i}>
          <label className="fs-loop__field">{t('Name')}
            <input className="fs-field fs-lab__name" value={a.name} onChange={(e) => set(i, { name: e.target.value })} />
          </label>
          <label className="fs-loop__field fs-loop__grow">{t('Command')}
            <input className="fs-field" value={a.command ?? ''} onChange={(e) => set(i, { command: e.target.value })} data-testid={`lab-arm-cmd-${i}`} />
          </label>
          <label className="fs-loop__field">{t('Folder (optional)')}
            <input className="fs-field" value={a.cwd ?? ''} onChange={(e) => set(i, { cwd: e.target.value })} />
          </label>
          <label className="fs-loop__field fs-loop__grow">{t('Idea it tests')}
            <input className="fs-field" value={a.label ?? ''} onChange={(e) => set(i, { label: e.target.value })} />
          </label>
          <button type="button" className="fs-loop__del" aria-label={t('Remove')} title={t('Remove')} disabled={arms.length < 2}
            onClick={() => setArms((cur) => cur.filter((_, j) => j !== i))}><Trash2 size={14} aria-hidden="true" /></button>
        </div>
      ))}
      <div className="fs-loop__row">
        <Button variant="ghost" size="sm" icon={Plus} label={t('Add variant')} onClick={() => setArms((cur) => [...cur, { name: `a${cur.length + 1}`, command: card.command }])} />
        <label className="fs-loop__field">{t('Paired rounds')}
          <input className="fs-field fs-loop__num" type="number" min={2} max={30} value={rounds} onChange={(e) => setRounds(Number(e.target.value) || 6)} data-testid="lab-rounds" />
        </label>
        <label className="fs-lab__check"><input type="checkbox" checked={aa} onChange={(e) => setAa(e.target.checked)} /> {t('Measure the noise floor (A/A)')}</label>
      </div>
      {error && <p className="fs-loop__why" data-tone="bad" role="alert">{error}</p>}
      <div><Button variant="secondary" size="sm" icon={FlaskConical} label={busy ? t('Measuring…') : t('Compare with the control')} loading={busy} onClick={() => { void run(); }} testId="lab-arms-run" /></div>
      {result && <ArmsTable stats={result.stats} />}
    </div>
  );
}

function JobView({ job, onChanged }: { job: api.ImproveJob; onChanged: () => void }) {
  const live = job.status === 'running' || job.status === 'queued';
  return (
    <div className="fs-lab__job" data-testid={`lab-job-${job.id}`}>
      <div className="fs-loop__row">
        <strong>{job.id}</strong>
        <span className="fs-loop__pill" data-tone={job.status === 'done' ? (job.applied ? 'good' : 'unknown') : job.status === 'failed' ? 'bad' : 'unknown'} data-testid="lab-job-status">{t(job.status)}</span>
        <span className="fs-loop__muted">{t(job.phase)} · {t('iteration {i} of {n}', { i: job.iteration, n: job.iterations })}</span>
        {live
          ? <Button variant="ghost" size="sm" icon={Square} label={t('Cancel')} onClick={() => { void api.cancelImprove(job.id).then(onChanged); }} />
          : <Button variant="ghost" size="sm" icon={Trash2} label={t('Delete')} onClick={() => { void api.deleteImprove(job.id).then(onChanged); }} />}
      </div>
      {job.error && <p className="fs-loop__why" data-tone="bad">{job.error}</p>}
      {job.winner && <p className="fs-loop__why" data-tone="good">{t('Winner: {h}', { h: job.winner.hypothesis })}{job.applied ? ` — ${t('applied to the folder')}` : ''}</p>}
      {job.history.map((it) => (
        <div key={it.iteration} className="fs-lab__iter">
          <span className="fs-loop__muted">{t('Iteration {i}', { i: it.iteration })}</span>
          <ol className="fs-lab__hyps">{it.arms.map((a) => <li key={a.name}><code>{a.name}</code> {a.hypothesis}{a.error ? <span className="fs-loop__muted"> — {a.error}</span> : null}</li>)}</ol>
          {it.stats && it.stats.arms?.length ? <ArmsTable stats={it.stats} /> : null}
          {it.verify && <p className="fs-loop__muted">{t('Measured again in the folder: {v} ({p} %)', { v: api.verdictLabel(it.verify.verdict), p: api.fmt(it.verify.delta_pct ?? 0, 3) })}</p>}
        </div>
      ))}
      {job.log.length > 0 && <pre className="fs-lab__log">{job.log.slice(-8).map((l) => `${l.at}  ${l.msg}`).join('\n')}</pre>}
    </div>
  );
}

function ImprovePanel({ card }: { card: api.FullCard }) {
  const [files, setFiles] = useState('');
  const [proposals, setProposals] = useState(3);
  const [iterations, setIterations] = useState(1);
  const [rounds, setRounds] = useState(6);
  const [guard, setGuard] = useState(card.guard_command ?? '');
  const [apply, setApply] = useState(true);
  const [model, setModel] = useState('');
  const [hints, setHints] = useState('');
  const [meta, setMeta] = useState<GoldfishMeta | null>(null);
  const [jobs, setJobs] = useState<api.ImproveJob[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const timer = useRef<number | null>(null);
  const reload = useCallback(async () => {
    try {
      const rows = await api.listImprove(card.id);
      const full = await Promise.all(rows.slice(0, 5).map((r) => api.getImprove(r.id)));
      setJobs(full);
    } catch (e) { setError((e as Error).message); }
  }, [card.id]);
  useEffect(() => { void reload(); void loadMeta().then(setMeta).catch(() => undefined); }, [reload]);
  useEffect(() => {
    const live = jobs.some((j) => j.status === 'running' || j.status === 'queued');
    if (live && timer.current === null) timer.current = window.setInterval(() => { void reload(); }, 3000);
    if (!live && timer.current !== null) { window.clearInterval(timer.current); timer.current = null; }
    return () => { if (timer.current !== null && !live) { window.clearInterval(timer.current); timer.current = null; } };
  }, [jobs, reload]);
  useEffect(() => () => { if (timer.current !== null) window.clearInterval(timer.current); }, []);
  const start = async () => {
    setBusy(true); setError('');
    try {
      await api.startImprove({ id: card.id, files: files.split(/[\n,]/).map((f) => f.trim()).filter(Boolean), proposals, iterations, rounds,
        guard_command: guard || undefined, apply, model: model || undefined, hints: hints || undefined });
      await reload();
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  return (
    <div className="fs-lab__pane" data-testid={`lab-improve-${card.id}`}>
      <p className="fs-loop__muted">{t('Faustus writes several hypotheses, a fresh worker turns each into a change of the files you name, every change is measured against an untouched copy, and only a change that clears the keep bar is merged into the folder and measured again. Rejected ideas stay in the ledger.')}</p>
      {!card.cwd && <p className="fs-loop__why" data-tone="warn">{t('This card has no folder: the improvement loop needs one.')}</p>}
      <label className="fs-loop__field">{t('Files it may change (relative to {cwd}, one per line)', { cwd: card.cwd ?? '–' })}
        <textarea className="fs-field fs-lab__area" rows={2} value={files} onChange={(e) => setFiles(e.target.value)} data-testid="lab-files" />
      </label>
      <div className="fs-loop__row">
        <label className="fs-loop__field">{t('Proposals')}
          <input className="fs-field fs-loop__num" type="number" min={1} max={6} value={proposals} onChange={(e) => setProposals(Number(e.target.value) || 3)} />
        </label>
        <label className="fs-loop__field">{t('Iterations')}
          <input className="fs-field fs-loop__num" type="number" min={1} max={5} value={iterations} onChange={(e) => setIterations(Number(e.target.value) || 1)} />
        </label>
        <label className="fs-loop__field">{t('Paired rounds')}
          <input className="fs-field fs-loop__num" type="number" min={2} max={30} value={rounds} onChange={(e) => setRounds(Number(e.target.value) || 6)} />
        </label>
        <label className="fs-loop__field">{t('Model')}
          <select className="fs-field" value={model} onChange={(e) => setModel(e.target.value)}>
            <option value="">{t('Background model')}</option>
            {(meta?.models ?? []).map((m) => <option key={`${m.endpoint_id}/${m.model}`} value={m.model}>{m.model}</option>)}
          </select>
        </label>
        <label className="fs-lab__check"><input type="checkbox" checked={apply} onChange={(e) => setApply(e.target.checked)} /> {t('Apply the winner')}</label>
      </div>
      <label className="fs-loop__field">{t('Guard command')}
        <input className="fs-field" value={guard} onChange={(e) => setGuard(e.target.value)} />
      </label>
      <label className="fs-loop__field">{t('Hints for the proposals (optional)')}
        <input className="fs-field" value={hints} onChange={(e) => setHints(e.target.value)} />
      </label>
      {error && <p className="fs-loop__why" data-tone="bad" role="alert">{error}</p>}
      <div className="fs-loop__row">
        <Button variant="primary" size="sm" icon={Wand2} label={t('Start the improvement loop')} loading={busy} disabled={!files.trim() || !card.cwd}
          onClick={() => { void start(); }} testId="lab-improve-start" />
        <Button variant="ghost" size="sm" icon={RefreshCw} label={t('Refresh')} onClick={() => { void reload(); }} />
      </div>
      {jobs.map((j) => <JobView key={j.id} job={j} onChanged={() => { void reload(); }} />)}
    </div>
  );
}

function Ledger({ rows }: { rows: api.LedgerRow[] }) {
  if (!rows.length) return <p className="fs-loop__muted">{t('Nothing recorded yet.')}</p>;
  return (
    <div className="fs-lab__tablewrap" data-testid="lab-ledger">
      <table className="fs-lab__table">
        <thead><tr><th>{t('When')}</th><th>{t('Source')}</th><th>{t('Change')}</th><th>{t('Verdict')}</th><th>{t('Difference')}</th><th>{t('Runs')}</th><th>{t('Why')}</th></tr></thead>
        <tbody>{[...rows].reverse().map((r, i) => (
          <tr key={i}>
            <td className="fs-loop__muted">{r.at.replace('T', ' ')}</td>
            <td className="fs-loop__muted">{r.source}</td>
            <td>{r.change}</td>
            <td><ArmPill verdict={r.verdict} /></td>
            <td>{r.delta_pct !== null && r.delta_pct !== undefined ? `${r.delta_pct >= 0 ? '+' : ''}${api.fmt(r.delta_pct, 3)} %` : '–'}</td>
            <td>{r.pairs ?? '–'}</td>
            <td className="fs-loop__muted">{r.why}</td>
          </tr>))}</tbody>
      </table>
    </div>
  );
}

export function CardLab({ id, onChanged }: { id: string; onChanged: () => void }) {
  const [tab, setTab] = useState<Tab>('plan');
  const [card, setCard] = useState<api.FullCard | null>(null);
  const [error, setError] = useState('');
  const load = useCallback(async () => {
    try { setCard(await api.getCard(id)); } catch (e) { setError((e as Error).message); }
  }, [id]);
  useEffect(() => { void load(); }, [load]);
  if (error) return <p className="fs-loop__why" data-tone="bad">{error}</p>;
  if (!card) return <p className="fs-loop__muted">{t('Loading…')}</p>;
  const refreshed = () => { void load(); onChanged(); };
  return (
    <div className="fs-lab" data-testid={`lab-${id}`}>
      <div className="fs-seg" role="radiogroup" aria-label={t('Loop')}>
        {(['plan', 'arms', 'improve', 'ledger'] as Tab[]).map((k) => (
          <button key={k} type="button" role="radio" aria-checked={tab === k} onClick={() => setTab(k)} data-testid={`lab-tab-${k}`}>
            {{ plan: t('Plan'), arms: t('Variants'), improve: t('Improvement loop'), ledger: t('Ledger ({n})', { n: card.ledger?.length ?? 0 }) }[k]}
          </button>
        ))}
      </div>
      {tab === 'plan' && <PlanEditor card={card} onSaved={refreshed} />}
      {tab === 'arms' && <ArmsPanel card={card} onDone={refreshed} />}
      {tab === 'improve' && <ImprovePanel card={card} />}
      {tab === 'ledger' && <Ledger rows={card.ledger ?? []} />}
    </div>
  );
}

export function MirrorSection() {
  const [days, setDays] = useState(30);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [stats, setStats] = useState<api.MirrorStats | null>(null);
  const run = async () => {
    setBusy(true); setError('');
    try { setStats(await api.runMirror(days)); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  const r = stats?.rates_pct_of_typed;
  return (
    <section className="fs-loop__form" aria-label={t('How you ask')} data-testid="mirror">
      <h2 className="fs-loop__h">{t('How you ask')}</h2>
      <p className="fs-loop__muted">{t('A scorecard of how you direct Faustus, read from your own chats (scheduled and delegated traffic is left out): status asks, corrections you repeat, kickoffs without an end, chats stuck without a measurement. Nothing leaves this machine and nothing is changed.')}</p>
      <div className="fs-loop__row">
        <label className="fs-loop__field">{t('Days')}
          <input className="fs-field fs-loop__num" type="number" min={1} max={365} value={days} onChange={(e) => setDays(Number(e.target.value) || 30)} />
        </label>
        <Button variant="secondary" size="sm" icon={Play} label={t('Analyse my chats')} loading={busy} onClick={() => { void run(); }} testId="mirror-run" />
      </div>
      {error && <p className="fs-loop__why" data-tone="bad" role="alert">{error}</p>}
      {stats && r && (
        <div className="fs-lab__mirror" data-testid="mirror-result">
          <dl className="fs-loop__stats">
            <div><dt>{t('Typed messages')}</dt><dd>{stats.typed}</dd></div>
            <div><dt>{t('Chats')}</dt><dd>{stats.chats}</dd></div>
            <div><dt>{t('Machine messages per typed')}</dt><dd>{stats.machine_per_typed}</dd></div>
            <div><dt>{t('Status asks')}</dt><dd>{r.status_ask} %</dd></div>
            <div><dt>{t('Kickoffs with a done-when')}</dt><dd>{stats.kickoffs.has_done_when_pct} %</dd></div>
            <div><dt>{t('Chats stuck without a measurement')}</dt><dd>{stats.loop_smells.length}</dd></div>
            <div><dt>{t('Concessions per 100 replies')}</dt><dd>{stats.assistant.concessions_per_100}</dd></div>
          </dl>
          {stats.suggestions.length === 0 && <p className="fs-loop__muted">{t('Nothing recurs often enough to suggest a change.')}</p>}
          <ol className="fs-lab__sugs">{stats.suggestions.map((s) => (
            <li key={s.key} data-testid={`mirror-sug-${s.key}`}>
              <strong>{s.title}</strong> <span className="fs-loop__muted">— {s.signal}</span>
              {s.before && <p className="fs-loop__muted">{t('Before')}: «{s.before}»</p>}
              <p>{s.fix}</p>
              <p className="fs-loop__muted">{t('Next time')}: {s.after}</p>
            </li>))}</ol>
          {stats.trend.length > 0 && <ul className="fs-lab__sugs">{stats.trend.map((x, i) => <li key={i} className="fs-loop__muted">{x.suggestion}: {x.before} % → {x.after} % ({x.verdict})</li>)}</ul>}
        </div>
      )}
    </section>
  );
}
