import { useState } from 'react';
import { getLang } from '../i18n';
import { Button } from '../components';
import { catalog, exportBundle, loadHooks, previewBundle, proposeHooks, saveHook, type Bundle, type Catalog, type MarkdownHook } from '../adapters/instructionPortability';
import './instruction-portability.css';

export function InstructionPortability() {
  const es = getLang() === 'es';
  const copy = (en: string, spanish: string) => es ? spanish : en;
  const [workspace, setWorkspace] = useState('');
  const [destination, setDestination] = useState('');
  const [target, setTarget] = useState('codex');
  const [sources, setSources] = useState<Catalog | null>(null);
  const [include, setInclude] = useState({ skills: true, rules: true, agents: true });
  const [bundle, setBundle] = useState<Bundle | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const [overwrite, setOverwrite] = useState(false);
  const [correction, setCorrection] = useState('');
  const [session, setSession] = useState('');
  const [proposals, setProposals] = useState<MarkdownHook[]>([]);
  const [hooks, setHooks] = useState<MarkdownHook[]>([]);
  const [draft, setDraft] = useState<MarkdownHook>({ name: '', event: 'bash', pattern: '', message: '', enabled: true });
  const options = () => ({ target, workspace, destination, overwrite, ...Object.fromEntries(Object.entries(include).filter(([, yes]) => !yes).map(([key]) => [key, []])) });
  const run = async (action: () => Promise<void>) => {
    setBusy(true); setNotice('');
    try { await action(); } catch (error) { setNotice(error instanceof Error ? error.message : String(error)); } finally { setBusy(false); }
  };
  const refresh = async () => {
    setSources(await catalog(workspace));
    if (workspace) {
      const loaded = await loadHooks(workspace); setHooks(loaded.rules);
      if (loaded.warnings.length) setNotice(loaded.warnings.map(row => row.message).join(' · '));
    }
  };
  const invalidate = () => setBundle(null);
  return <section className="fs-portability" data-testid="instruction-portability">
    <h2>{copy('Export instructions', 'Exportar instrucciones')}</h2>
    <p>{copy('Move skills, project rules and specialist instructions to another coding workspace. Preview the files before exporting.', 'Lleva skills, reglas y agentes a otro espacio de trabajo. Revisa los archivos antes de exportarlos.')}</p>
    <div className="fs-portability__fields">
      <label>{copy('Source project folder (optional)', 'Carpeta del proyecto de origen (opcional)')}<input className="fs-field" value={workspace} onChange={e => { setWorkspace(e.target.value); invalidate(); }} data-testid="portability-workspace" /></label>
      <label>{copy('Export destination', 'Carpeta de destino')}<input className="fs-field" value={destination} onChange={e => { setDestination(e.target.value); invalidate(); }} data-testid="portability-destination" /></label>
      <label>{copy('Destination app', 'Aplicación de destino')}<select className="fs-field" value={target} onChange={e => { setTarget(e.target.value); invalidate(); }} data-testid="portability-target"><option value="claude">Claude Code</option><option value="codex">Codex</option><option value="cursor">Cursor</option></select></label>
    </div>
    <div className="fs-portability__actions">
      {(['skills', 'rules', 'agents'] as const).map(key => <label key={key}><input type="checkbox" checked={include[key]} onChange={e => { setInclude({ ...include, [key]: e.target.checked }); invalidate(); }} />{key === 'skills' ? 'Skills' : key === 'rules' ? copy('Rules', 'Reglas') : copy('Agents', 'Agentes')}{sources ? ` (${sources[key].length})` : ''}</label>)}
      <Button label={copy('Read sources', 'Leer origen')} variant="ghost" disabled={busy} onClick={() => void run(refresh)} />
      <Button label={copy('Preview', 'Vista previa')} disabled={busy} onClick={() => void run(async () => setBundle(await previewBundle(options())))} testId="portability-preview" />
      <Button label={copy('Export files', 'Exportar archivos')} disabled={busy || !bundle || !(destination || workspace)} onClick={() => void run(async () => { const receipt = await exportBundle(options()); setNotice(copy(`Created ${receipt.created}, updated ${receipt.updated}, unchanged ${receipt.unchanged}, conflicts ${receipt.conflicts.length}.`, `Creados ${receipt.created}, actualizados ${receipt.updated}, sin cambios ${receipt.unchanged}, conflictos ${receipt.conflicts.length}.`)); })} testId="portability-export" />
    </div>
    <label className="fs-portability__overwrite"><input type="checkbox" checked={overwrite} onChange={e => setOverwrite(e.target.checked)} />{copy('Replace existing destination files with different content', 'Sustituir archivos del destino que tengan otro contenido')}</label>
    {bundle && <div className="fs-portability__preview" data-testid="portability-files"><strong>{bundle.files.length} {copy('files', 'archivos')}</strong>{bundle.warnings.map((warning, i) => <p key={i} role="status">{warning}</p>)}{bundle.files.map(file => <details key={file.path}><summary>{file.path}</summary><pre>{file.encoding === 'base64' ? copy('Binary supporting resource included.', 'Recurso binario incluido.') : file.content}</pre></details>)}</div>}
    <hr />
    <h2>{copy('Project checks', 'Comprobaciones del proyecto')}</h2>
    <p>{copy('Markdown checks show a message when their pattern matches a command, file, request or final answer. They accompany the agent’s work.', 'Las comprobaciones Markdown muestran un aviso cuando un patrón coincide con un comando, archivo, petición o respuesta final. Acompañan el trabajo del agente.')}</p>
    <div className="fs-portability__fields">
      <label>{copy('Correction or instruction', 'Corrección o indicación')}<textarea className="fs-field" value={correction} onChange={e => setCorrection(e.target.value)} placeholder={copy('Do not use `git add -A`; stage only the changed paths.', 'No uses `git add -A`; indexa solo las rutas modificadas.')} data-testid="hook-correction" /></label>
      <label>{copy('Or conversation id', 'O identificador de conversación')}<input className="fs-field" value={session} onChange={e => setSession(e.target.value)} data-testid="hook-session" /></label>
    </div>
    <Button label={copy('Propose checks', 'Proponer comprobaciones')} disabled={busy || (!correction && !session)} onClick={() => void run(async () => { const result = await proposeHooks(correction ? [{ role: 'user', content: correction }] : [], session); setProposals(result.proposals); setNotice(result.notes.map(row => row.message).join(' · ')); })} testId="hook-propose" />
    {proposals.map(rule => <article key={rule.name} className="fs-portability__proposal"><p>{rule.evidence}</p><pre>{rule.markdown}</pre><Button label={copy('Edit proposal', 'Editar propuesta')} variant="ghost" onClick={() => setDraft(rule)} /><Button label={copy('Save check', 'Guardar comprobación')} disabled={busy || !workspace} onClick={() => void run(async () => { const receipt = await saveHook(workspace, rule); setNotice([`${receipt.status}: ${receipt.path}`, ...(receipt.warnings ?? [])].join(' · ')); setHooks((await loadHooks(workspace)).rules); })} testId="hook-save-proposal" /></article>)}
    <div className="fs-portability__fields">
      <label>{copy('Name', 'Nombre')}<input className="fs-field" value={draft.name} onChange={e => setDraft({ ...draft, name: e.target.value })} /></label>
      <label>{copy('Event', 'Evento')}<select className="fs-field" value={draft.event} onChange={e => setDraft({ ...draft, event: e.target.value })}>{[['bash', copy('Command', 'Comando')], ['file', copy('File', 'Archivo')], ['prompt', copy('Request', 'Petición')], ['stop', copy('Final answer', 'Respuesta final')]].map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      <label>{copy('Regex pattern', 'Patrón de búsqueda')}<input className="fs-field" value={draft.pattern ?? ''} onChange={e => setDraft({ ...draft, pattern: e.target.value, conditions: [] })} /></label>
      <label>{copy('Message', 'Aviso')}<textarea className="fs-field" value={draft.message} onChange={e => setDraft({ ...draft, message: e.target.value })} /></label>
    </div>
    <label><input type="checkbox" checked={draft.enabled !== false} onChange={e => setDraft({ ...draft, enabled: e.target.checked })} />{copy('Enabled', 'Activada')}</label>
    <Button label={draft.path ? copy('Save changes', 'Guardar cambios') : copy('Save check', 'Guardar comprobación')} disabled={busy || !workspace || !draft.name || !draft.message || !(draft.pattern || draft.conditions?.length)} onClick={() => void run(async () => { const receipt = await saveHook(workspace, draft, Boolean(draft.path)); setDraft({ ...draft, path: receipt.path }); setNotice([`${receipt.status}: ${receipt.path}`, ...(receipt.warnings ?? [])].join(' · ')); setHooks((await loadHooks(workspace)).rules); })} testId="hook-save-draft" />
    {hooks.length > 0 && <ul>{hooks.map(rule => <li key={rule.path ?? rule.name}><button type="button" onClick={() => setDraft(rule)}>{rule.name}</button> · {rule.event} · {rule.enabled === false ? copy('off', 'desactivada') : copy('on', 'activada')}</li>)}</ul>}
    {notice && <p role="status" className="fs-portability__notice">{notice}</p>}
  </section>;
}
