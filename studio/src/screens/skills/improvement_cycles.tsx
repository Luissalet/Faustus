import { useEffect, useState } from 'react';
import { Play, RefreshCw, RotateCcw } from 'lucide-react';
import { Button } from '../../components';
import { getJson, responseReason } from '../../adapters/api';
import { getLang } from '../../i18n';
import './improvement_cycles.css';

const text = (en: string, es: string) => getLang() === 'es' ? es : en;
type Case = { id: string; group: string; prompt: string; mode?: string; checks: object };
type Comparison = { reason: string; groups: Record<string, { before: number; after: number; delta: number; cases: number }> };
type Candidate = { patch_id?: string; status: string; error?: string; high_risk?: boolean; proposal: { edit: { kind: string; key: string; value: unknown }; reason: string }; comparison?: Comparison };
type Cycle = { id: string; name: string; status: string; phase: string; created_at: string; error?: string; spec: { target_group: string; regression_threshold: number; repeats: number }; candidates: Candidate[] };
type Patch = { patch_id: string; status: string; evaluation: { before: { id: string; group: string; answer?: string; score: number | null; error: string }[]; after: { id: string; group: string; answer?: string; score: number | null; error: string }[] } };
type Benchmark = { id: string; name: string; digest: string; cases: Case[]; limitations: string; coverage: { examples: number; outcomes: Record<string, number>; splits: Record<string, number> }; measurements: { id: string; model: string; status: string; groups: Record<string, { score: number | null; cases: number; errors: number }> }[]; galton_exports: { suite: { name?: string; id?: string } }[] };
const base = '/api/harness/cycles';
async function post<T>(url: string, body: unknown): Promise<T> {
  const response = await fetch(url, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (!response.ok) throw new Error(await responseReason(response, url));
  return response.json() as Promise<T>;
}
const pct = (n: number) => `${(n * 100).toLocaleString(undefined, { maximumFractionDigits: 2 })}%`;
function statusLabel(status: string) {
  const labels: Record<string, string> = {
    queued: text('Queued', 'En cola'), running: text('Running', 'En marcha'), done: text('Completed', 'Completado'),
    accepted: text('Applied', 'Aplicado'), rejected: text('Rejected', 'Rechazado'), awaiting_person: text('Waiting for you', 'Pendiente de la persona'),
    stale: text('Configuration changed', 'Configuración modificada'), error: text('Error', 'Error'), reverted: text('Reverted', 'Revertido'),
    promoted: text('Applied', 'Aplicado'), evaluated_failed: text('Rejected', 'Rechazado'), evaluated: text('Evaluated', 'Evaluado'),
  };
  return labels[status] ?? status;
}
function reasonLabel(reason: string) {
  return ({ improved: text('Target improved; other groups within the threshold.', 'Mejora el objetivo; los demás grupos quedan dentro del umbral.'),
    regression: text('Another group regressed beyond the threshold.', 'Otro grupo empeora más de lo permitido.'),
    target_not_improved: text('The target did not improve.', 'El objetivo no mejora.'),
    insufficient_data: text('The comparison is incomplete.', 'La comparación está incompleta.') } as Record<string, string>)[reason] ?? reason;
}

export function ImprovementCyclesPanel() {
  const [cycles, setCycles] = useState<Cycle[]>([]);
  const [selected, setSelected] = useState('');
  const [detail, setDetail] = useState<{ cycle: Cycle; patches: Patch[] } | null>(null);
  const [name, setName] = useState(text('Configuration improvement', 'Mejora de configuración'));
  const [target, setTarget] = useState('');
  const [project, setProject] = useState('');
  const [threshold, setThreshold] = useState('1');
  const [casesText, setCasesText] = useState('[]');
  const [candidateCount, setCandidateCount] = useState(2);
  const [repeats, setRepeats] = useState(1);
  const [scheduleTime, setScheduleTime] = useState('06:00');
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [optionsText, setOptionsText] = useState('{}');
  const [galtonUrl, setGaltonUrl] = useState('');
  const [galtonSuites, setGaltonSuites] = useState('');
  const [benchmarks, setBenchmarks] = useState<Benchmark[]>([]);
  const [benchmarkId, setBenchmarkId] = useState('');
  const [sessions, setSessions] = useState('');
  const [redactions, setRedactions] = useState('');
  const [textKind, setTextKind] = useState('');
  const [textKey, setTextKey] = useState('response');
  const benchmark = benchmarks.find(b => b.id === benchmarkId);
  async function reloadBenchmarks() {
    const result = await post<{ benchmarks: Benchmark[] }>('/api/harness/personal', { action: 'list' });
    setBenchmarks(result.benchmarks);
  }
  useEffect(() => { void reloadBenchmarks().catch(e => setError((e as Error).message)); }, []);
  function loadBenchmark(b: Benchmark) {
    setBenchmarkId(b.id); setCasesText(JSON.stringify(b.cases, null, 2)); setTarget(b.cases[0]?.group ?? '');
  }
  function modelOptions() {
    const options = JSON.parse(optionsText) as { endpoint_url?: string; model?: string };
    return { endpoint_url: options.endpoint_url ?? '', model: options.model ?? '' };
  }
  async function reload() {
    const result = await getJson<{ cycles: Cycle[] }>(base);
    setCycles(result.cycles);
  }
  useEffect(() => {
    let live = true;
    const load = async () => {
      try { const result = await getJson<{ cycles: Cycle[] }>(base); if (live) setCycles(result.cycles); }
      catch (e) { if (live) setError((e as Error).message); }
    };
    void load();
    const timer = window.setInterval(() => void load(), 4000);
    return () => { live = false; window.clearInterval(timer); };
  }, []);
  useEffect(() => {
    let live = true;
    setDetail(null);
    if (!selected) return;
    const load = async () => {
      try { const result = await getJson<{ cycle: Cycle; patches: Patch[] }>(`${base}/${selected}`); if (live) setDetail(result); }
      catch (e) { if (live) setError((e as Error).message); }
    };
    void load();
    const timer = window.setInterval(() => void load(), 3000);
    return () => { live = false; window.clearInterval(timer); };
  }, [selected]);
  async function action(key: string, fn: () => Promise<void>) {
    setBusy(key); setError(''); setNotice('');
    try { await fn(); } catch (e) { setError((e as Error).message); } finally { setBusy(''); }
  }
  function spec() {
    const cases = JSON.parse(casesText) as Case[];
    const thresholdNumber = Number(threshold);
    if (!target || !Array.isArray(cases) || !Number.isFinite(thresholdNumber) || thresholdNumber < 0 || thresholdNumber > 100) throw new Error(text('Check the cases, target and threshold.', 'Revisa los casos, el objetivo y el umbral.'));
    return { ...JSON.parse(optionsText) as object, name, target_group: target, project_id: project, cases, regression_threshold: thresholdNumber / 100, candidates: candidateCount, repeats,
      benchmark_id: benchmarkId, text_target: textKind ? { kind: textKind, key: textKey } : null };
  }
  return <section className="fs-cycles" aria-label={text('Improvement cycles', 'Ciclos de mejora')}>
    <div className="fs-cycles__intro"><h2>{text('Configuration improvement', 'Mejora de configuración')}</h2><p>{text('Each candidate is measured against the current configuration. Only a target improvement within every regression bound is applied. You can undo each edit.', 'Cada candidato se mide frente a la configuración actual. Se aplica si mejora el objetivo y respeta el umbral de cada grupo de regresión. Puedes deshacer cada edición.')}</p></div>
    {error && <p className="fs-cycles__message" role="alert">{error}</p>}
    {notice && <p className="fs-cycles__message" role="status">{notice}</p>}
    <div className="fs-cycles__layout">
      <form className="fs-cycles__form" onSubmit={e => { e.preventDefault(); void action('start', async () => { const result = await post<{ cycle: Cycle }>(base, spec()); setSelected(result.cycle.id); await reload(); }); }}>
        <h3>{text('New cycle', 'Nuevo ciclo')}</h3>
        <label>{text('Name', 'Nombre')}<input className="fs-field" value={name} onChange={e => setName(e.target.value)} required /></label>
        <details><summary>{text('Personal benchmark from traces', 'Benchmark personal desde las trazas')}</summary>
          <p>{text('Build text tasks from rated answers in your conversations. Local masks remove recognized identifiers, secrets, URLs and paths before synthesis. Add names and other private entities below; masking is heuristic. Model-generated references need domain validation.', 'Crea tareas de texto a partir de respuestas valoradas de tus conversaciones. Antes de sintetizar, se ocultan en local identificadores reconocidos, secretos, URL y rutas. Añade nombres y otras entidades privadas debajo; el enmascarado es heurístico. Las referencias del modelo necesitan validación de dominio.')}</p>
          <label>{text('Conversation IDs (comma separated; blank uses recent own chats)', 'IDs de conversaciones (separados por comas; vacío usa tus chats recientes)')}<input className="fs-field" value={sessions} onChange={e => setSessions(e.target.value)} /></label>
          <label>{text('Names and entities to mask (one per line)', 'Nombres y entidades que ocultar (uno por línea)')}<textarea className="fs-field" value={redactions} onChange={e => setRedactions(e.target.value)} /></label>
          <Button type="button" variant="secondary" size="sm" label={text('Synthesize personal benchmark', 'Sintetizar benchmark personal')} disabled={!!busy} loading={busy === 'personal-build'} onClick={() => void action('personal-build', async () => {
            const result = await post<{ benchmark: Benchmark }>('/api/harness/personal', { action: 'build', spec: { name, session_ids: sessions.split(',').map(s => s.trim()).filter(Boolean), redact_terms: redactions.split('\n').map(s => s.trim()).filter(Boolean), ...modelOptions() } });
            await reloadBenchmarks(); loadBenchmark(result.benchmark); setNotice(text('Frozen battery loaded. Reserved cases are excluded from optimization proposals.', 'Batería fija cargada. Los casos reservados se excluyen de las propuestas de optimización.'));
          })} />
          <label>{text('Saved benchmark', 'Benchmark guardado')}<select className="fs-field" value={benchmarkId} onChange={e => { const b = benchmarks.find(item => item.id === e.target.value); if (b) loadBenchmark(b); else setBenchmarkId(''); }}><option value="">{text('No personal battery selected', 'Sin batería personal seleccionada')}</option>{benchmarks.map(b => <option key={b.id} value={b.id}>{b.name} · {b.cases.length}</option>)}</select></label>
          {benchmark && <><p>{benchmark.coverage.examples} {text('rated examples', 'ejemplos valorados')} · {benchmark.coverage.outcomes.positive ?? 0} {text('positive', 'positivos')} · {benchmark.coverage.outcomes.negative ?? 0} {text('negative', 'negativos')} · {benchmark.coverage.splits.heldout ?? 0} {text('reserved', 'reservados')}</p>
            <p>{text('Text tasks only. The same frozen cases measure each model or edit; they do not certify tool actions or statistical representativeness.', 'Solo tareas textuales. Los mismos casos fijos miden cada modelo o edición; no acreditan acciones con herramientas ni representatividad estadística.')}</p>
            <Button type="button" variant="secondary" size="sm" label={text('Measure current model', 'Medir modelo actual')} loading={busy === 'personal-measure'} disabled={!!busy} onClick={() => void action('personal-measure', async () => { await post('/api/harness/personal', { action: 'measure', id: benchmarkId, spec: { ...modelOptions(), repeats, project_id: project } }); await reloadBenchmarks(); })} />
            <label>{text('Enabled Galton evaluation URL', 'URL de evaluación de Galton activada')}<input className="fs-field" type="url" value={galtonUrl} onChange={e => setGaltonUrl(e.target.value)} /></label>
            <Button type="button" variant="secondary" size="sm" label={text('Create suite in Galton', 'Crear suite en Galton')} disabled={!!busy || !galtonUrl} onClick={() => void action('personal-export', async () => { await post('/api/harness/personal', { action: 'export', id: benchmarkId, url: galtonUrl }); await reloadBenchmarks(); setNotice(text('Suite created. Open Galton evaluation → Suites to inspect and measure models.', 'Suite creada. Abre Evaluación de Galton → Suites para revisarla y medir modelos.')); })} />
            {benchmark.galton_exports.map((item, i) => <p key={i}>{text('Galton suite', 'Suite de Galton')}: {item.suite.name ?? item.suite.id}</p>)}
            {!!benchmark.measurements.length && <div className="fs-cycles__table"><table><caption>{text('Measurements on this fixed battery', 'Medidas sobre esta batería fija')}</caption><thead><tr><th>{text('Model', 'Modelo')}</th><th>{text('Group', 'Grupo')}</th><th>{text('Score', 'Puntuación')}</th><th>{text('Cases / errors', 'Casos / errores')}</th></tr></thead><tbody>{benchmark.measurements.flatMap(m => Object.entries(m.groups).map(([group, score]) => <tr key={`${m.id}-${group}`}><th>{m.model}</th><td>{group}</td><td>{score.score === null ? text('Unavailable', 'No disponible') : pct(score.score)}</td><td>{score.cases} / {score.errors}</td></tr>))}</tbody></table></div>}
          </>}
        </details>
        <label>{text('Optimization target', 'Texto que optimizar')}<select className="fs-field" value={textKind} onChange={e => { setTextKind(e.target.value); setTextKey(e.target.value === 'skill_text' ? '' : 'response'); }}><option value="">{text('Any configuration edit', 'Cualquier ajuste de configuración')}</option><option value="system_section">{text('Prompt section', 'Sección de prompt')}</option><option value="skill_text">{text('Skill text', 'Texto de skill')}</option></select></label>
        {textKind === 'system_section' && <label>{text('Section', 'Sección')}<select className="fs-field" value={textKey} onChange={e => setTextKey(e.target.value)}>{['response', 'reasoning', 'workflow', 'verification'].map(k => <option key={k} value={k}>{({ response: text('Response', 'Respuesta'), reasoning: text('Reasoning', 'Razonamiento'), workflow: text('Workflow', 'Flujo de trabajo'), verification: text('Verification', 'Verificación') } as Record<string, string>)[k]}</option>)}</select></label>}
        {textKind === 'skill_text' && <label>{text('Existing skill name', 'Nombre de la skill existente')}<input className="fs-field" value={textKey} onChange={e => setTextKey(e.target.value)} required /></label>}
        <label>{text('Target group', 'Grupo objetivo')}<input className="fs-field" value={target} onChange={e => setTarget(e.target.value)} placeholder={text('A group present in the cases', 'Un grupo presente en los casos')} required /></label>
        <label>{text('Project ID (optional)', 'ID del proyecto (opcional)')}<input className="fs-field" value={project} onChange={e => setProject(e.target.value)} /></label>
        <div className="fs-cycles__numbers"><label>{text('Regression threshold (%)', 'Umbral de regresión (%)')}<input className="fs-field" type="number" min="0" max="100" step="0.1" value={threshold} onChange={e => setThreshold(e.target.value)} required /></label>
          <label>{text('Candidates', 'Candidatos')}<input className="fs-field" type="number" min="1" max="8" value={candidateCount} onChange={e => setCandidateCount(Number(e.target.value))} required /></label>
          <label>{text('Repeats per case', 'Repeticiones por caso')}<input className="fs-field" type="number" min="1" max="5" value={repeats} onChange={e => setRepeats(Number(e.target.value))} required /></label></div>
        <label>{text('Evaluation cases (JSON)', 'Casos de evaluación (JSON)')}<textarea className="fs-field fs-cycles__editor" value={casesText} spellCheck={false} onChange={e => setCasesText(e.target.value)} /></label>
        <Button type="button" variant="secondary" size="sm" label={text('Load daily battery', 'Cargar batería diaria')} loading={busy === 'cases'} disabled={!!busy} onClick={() => void action('cases', async () => { const result = await getJson<{ cases: Case[] }>(`${base}/cases`); setBenchmarkId(''); setCasesText(JSON.stringify(result.cases, null, 2)); setTarget(result.cases[0]?.group ?? ''); })} />
        <details><summary>{text('Model, traces and Galton', 'Modelo, trazas y Galton')}</summary><p>{text('Uses the configured global model. Optional JSON: session_ids, endpoint_url + model, base_system, max_tokens, galton_url, galton_run_ids. Galton must already be enabled.', 'Usa el modelo global configurado. JSON opcional: session_ids, endpoint_url + model, base_system, max_tokens, galton_url, galton_run_ids. Galton debe estar ya activado.')}</p><label>{text('Options (JSON)', 'Opciones (JSON)')}<textarea className="fs-field" value={optionsText} onChange={e => setOptionsText(e.target.value)} spellCheck={false} /></label>
          <label>{text('Enabled Galton evaluation URL', 'URL de evaluación de Galton activada')}<input className="fs-field" type="url" value={galtonUrl} onChange={e => setGaltonUrl(e.target.value)} /></label>
          <label>{text('Suite IDs, separated by commas', 'IDs de suites, separados por comas')}<input className="fs-field" value={galtonSuites} onChange={e => setGaltonSuites(e.target.value)} /></label>
          <Button type="button" variant="secondary" size="sm" label={text('Import Galton suites', 'Importar suites de Galton')} disabled={!!busy || !galtonUrl || !galtonSuites} onClick={() => void action('galton', async () => { const result = await post<{ cases: Case[] }>(`${base}/galton/cases`, { url: galtonUrl, suites: galtonSuites.split(',').map(s => s.trim()).filter(Boolean) }); setCasesText(JSON.stringify(result.cases, null, 2)); setTarget(result.cases[0]?.group ?? ''); setOptionsText(JSON.stringify({ ...JSON.parse(optionsText) as object, galton_url: galtonUrl }, null, 2)); })} />
        </details>
        <p>{text('The target and at least one other group need explicit checks. A 1% threshold permits one percentage point per group. High-risk edits wait for you.', 'El objetivo y al menos otro grupo necesitan comprobaciones explícitas. Un umbral del 1 % permite un punto porcentual por grupo. Las ediciones de riesgo alto esperan a la persona.')}</p>
        <Button type="submit" variant="primary" icon={Play} label={text('Run cycle', 'Lanzar ciclo')} loading={busy === 'start'} disabled={!!busy} />
        <details><summary>{text('Schedule this cycle', 'Programar este ciclo')}</summary><label>{text('Daily at', 'Cada día a las')}<input className="fs-field" type="time" value={scheduleTime} onChange={e => setScheduleTime(e.target.value)} /></label><Button type="button" variant="secondary" size="sm" label={text('Create daily automation', 'Crear automatización diaria')} disabled={!!busy} onClick={() => void action('schedule', async () => { await post('/api/tasks', { name, task_type: 'action', action: 'improvement_cycle', prompt: JSON.stringify(spec()), schedule: 'daily', scheduled_time: scheduleTime, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone, output_target: 'session' }); setNotice(text('Daily automation created. Manage it in Automations.', 'Automatización diaria creada. Puedes gestionarla en Automatizaciones.')); })} /></details>
      </form>
      <div className="fs-cycles__history"><div className="fs-cycles__head"><h3>{text('Cycle history', 'Historial de ciclos')}</h3><Button variant="ghost" size="sm" icon={RefreshCw} label={text('Refresh', 'Actualizar')} onClick={() => void action('refresh', reload)} disabled={!!busy} /></div>
        {!cycles.length && <p>{text('No cycles yet. Load or enter a battery to begin.', 'Todavía no hay ciclos. Carga o introduce una batería para empezar.')}</p>}
        <ul className="fs-cycles__list">{cycles.map(c => <li key={c.id}><button type="button" onClick={() => setSelected(c.id)} aria-pressed={selected === c.id}><span>{c.name}</span><small>{new Date(c.created_at).toLocaleString()} · {statusLabel(c.status)}</small></button></li>)}</ul>
        {selected && !detail && <p role="status">{text('Loading results…', 'Cargando resultados…')}</p>}
        {detail && <div aria-live="polite"><h3>{detail.cycle.name}</h3><p>{statusLabel(detail.cycle.status)} · {text('Target', 'Objetivo')}: {detail.cycle.spec.target_group} · {text('Threshold', 'Umbral')}: {pct(detail.cycle.spec.regression_threshold)}</p>{detail.cycle.error && <p role="alert">{detail.cycle.error}</p>}
          {detail.cycle.candidates.map((c, i) => { const patch = detail.patches.find(p => p.patch_id === c.patch_id); const status = patch?.status === 'promoted' || patch?.status === 'reverted' ? patch.status : c.status;
            return <article className="fs-cycles__candidate" key={c.patch_id ?? i}><div className="fs-cycles__head"><h4>{text('Candidate', 'Candidato')} {i + 1}</h4><strong data-status={status}>{statusLabel(status)}</strong></div><p>{c.proposal.reason}</p><code>{c.proposal.edit.kind} / {c.proposal.edit.key}</code><pre>{typeof c.proposal.edit.value === 'string' ? c.proposal.edit.value : JSON.stringify(c.proposal.edit.value)}</pre>
              {c.comparison && <><p>{reasonLabel(c.comparison.reason)}</p><div className="fs-cycles__table"><table><caption>{text('Scores by group', 'Puntuaciones por grupo')}</caption><thead><tr><th>{text('Group', 'Grupo')}</th><th>{text('Before', 'Antes')}</th><th>{text('After', 'Después')}</th><th>Δ</th><th>{text('Cases', 'Casos')}</th></tr></thead><tbody>{Object.entries(c.comparison.groups).map(([group, score]) => <tr key={group}><th>{group}</th><td>{pct(score.before)}</td><td>{pct(score.after)}</td><td>{pct(score.delta)}</td><td>{score.cases}</td></tr>)}</tbody></table></div></>}
              {c.error && <p role="alert">{c.error}</p>}
              {patch && <details><summary>{text('Read before/after cases', 'Ver casos antes y después')}</summary>{patch.evaluation.before.map((before, index) => { const after = patch.evaluation.after[index]; return <div className="fs-cycles__case" key={before.id}><h5>{before.id} · {before.group}</h5><p>{text('Before', 'Antes')} ({before.score ?? '—'})</p><pre>{before.error || before.answer}</pre><p>{text('After', 'Después')} ({after?.score ?? '—'})</p><pre>{after?.error || after?.answer}</pre></div>; })}</details>}
              {(status === 'promoted' || status === 'awaiting_person') && <Button size="sm" variant="secondary" icon={RotateCcw} label={status === 'promoted' ? text('Revert this edit', 'Revertir esta edición') : text('Apply evaluated edit', 'Aplicar edición evaluada')} disabled={!!busy} onClick={() => void action(c.patch_id ?? '', async () => { await post(`${base}/patches/${c.patch_id}/${status === 'promoted' ? 'revert' : 'accept'}`, {}); setDetail(await getJson(`${base}/${selected}`)); await reload(); })} />}
            </article>; })}</div>}
      </div>
    </div>
  </section>;
}
