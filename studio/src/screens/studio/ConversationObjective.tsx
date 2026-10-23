import { useEffect, useState } from 'react';
import { Link } from 'react-router';
import { Button, Dialog } from '../../components';
import { ApiError } from '../../adapters/api';
import { getObjective, getObjectiveTemplates, writeObjective, type ConversationObjective as Objective, type ObjectiveRef, type ObjectiveTemplate } from '../../adapters/conversation-objective';
import { getRepo, getWorkingDiff, type GitDiff } from '../../adapters/git';
import { DiffPane } from '../source-control/DiffPane';
import { TurnActivity } from './TurnActivity';
import type { Turn } from './model';
import type { ModelRoute } from '../../adapters/chat';
import { t, useLang } from '../../i18n';
import './conversation-objective.css';

type Props = {
  sessionId: string; projectId?: string; workspace: string; turns: Turn[]; route?: ModelRoute | null;
  onContinue: (runId?: string | null) => void; onFile: (workspace: string, path: string) => void;
  onAgents: () => void; onTasks: () => void;
};
const blank = {statement: '', context: '', deliverable: '', refs: [] as ObjectiveRef[]};

export function ConversationObjective({sessionId, projectId, workspace, turns, route, onContinue, onFile, onAgents, onTasks}: Props) {
  const [objective, setObjective] = useState<Objective | null>(null);
  const [editing, setEditing] = useState(false);
  const [brief, setBrief] = useState(blank);
  const [revision, setRevision] = useState(0);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [generation, setGeneration] = useState<{active: boolean} | null>(null);
  const [conflict, setConflict] = useState(false);
  const [result, setResult] = useState('');
  const [completing, setCompleting] = useState(false);
  const [selectedRun, setSelectedRun] = useState<string | null>(null);
  const [diffRef, setDiffRef] = useState<ObjectiveRef | null>(null);
  const [diff, setDiff] = useState<GitDiff | null>(null);
  const [diffError, setDiffError] = useState('');
  const [ref, setRef] = useState<ObjectiveRef>({kind: 'resource', workspace, path: ''});
  const language = useLang();
  const [templates, setTemplates] = useState<ObjectiveTemplate[]>([]);
  const [templateId, setTemplateId] = useState('');
  const [templateError, setTemplateError] = useState('');

  useEffect(() => {
    if (!editing) return;
    const controller = new AbortController();
    setTemplates([]); setTemplateId(''); setTemplateError('');
    void getObjectiveTemplates(sessionId, language, controller.signal).then(data => setTemplates(data.templates)).catch(exc => {
      if (!controller.signal.aborted) setTemplateError((exc as Error).message);
    });
    return () => controller.abort();
  }, [editing, sessionId, language]);

  useEffect(() => {
    const controller = new AbortController();
    setLoaded(false); setObjective(null); setEditing(false); setError('');
    const load = () => getObjective(sessionId, controller.signal).then(data => { setObjective(data.objective); setGeneration(data.generation ?? null); setLoaded(true); }).catch(exc => {
      if (!controller.signal.aborted) { setError((exc as Error).message); setLoaded(true); }
    });
    void load();
    const timer = window.setInterval(() => { if (document.visibilityState === 'visible') void load(); }, 5000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [sessionId]);

  useEffect(() => {
    if (!diffRef) return;
    let live = true; setDiff(null); setDiffError('');
    void getRepo(diffRef.id!).then(repo => {
      const normal = (value: string) => {
        const normalized = value.replace(/\\/g, '/').replace(/\/$/, '');
        return /^[a-z]:\//i.test(normalized) || value.startsWith('\\\\') ? normalized.toLowerCase() : normalized;
      };
      if (normal(repo.path) !== normal(diffRef.workspace!)) throw new Error(t('The saved repository and workspace no longer match.'));
      return getWorkingDiff(repo.id, diffRef.path!, diffRef.scope === 'staged');
    }).then(value => { if (live) setDiff(value); }).catch(exc => { if (live) setDiffError((exc as Error).message); });
    return () => { live = false; };
  }, [diffRef]);

  async function mutate(action: 'save' | 'state' | 'continue', body: unknown) {
    setBusy(true); setError('');
    try {
      const response = await writeObjective(sessionId, action, body);
      setObjective(response.objective);
      if (response.generation !== undefined) setGeneration(response.generation);
      if (action === 'continue') {
        if (response.run_id) onContinue(response.run_id);
        else setError(t('The continuation is being prepared. Its run is not available yet; use Continue again to rejoin it.'));
      }
      setEditing(false); setCompleting(false);
    } catch (exc) {
      if (exc instanceof ApiError && exc.status === 409 && action === 'save') {
        setConflict(true);
        try { setObjective((await getObjective(sessionId)).objective); } catch { /* keep the current view and draft */ }
        setError(t('This objective changed elsewhere. Your draft is kept. Review the saved version before saving again.'));
      } else setError((exc as Error).message);
    } finally { setBusy(false); }
  }
  function edit() {
    setBrief(objective ? {statement: objective.statement, context: objective.context, deliverable: objective.deliverable, refs: objective.refs} : blank);
    setRevision(objective?.revision ?? 0); setConflict(false); setError(''); setEditing(true);
  }
  function openRef(value: ObjectiveRef) {
    if (value.kind === 'resource') onFile(value.workspace!, value.path!);
    if (value.kind === 'change') setDiffRef(value);
  }
  const runTurn = turns.find(turn => turn.metrics?.runId === selectedRun || turn.traceId === selectedRun || turn.terminalRun?.runId === selectedRun);
  const binding = objective?.runs.find(run => run.run_id === selectedRun);
  const template = templates.find(item => item.id === templateId);

  return <section className="fs-conversation-objective" aria-label={t('Conversation objective')} data-testid="conversation-objective">
    <div className="fs-conversation-objective__bar">
      <button type="button" className="fs-studio__chip" title={objective?.statement} onClick={edit} disabled={!loaded} data-testid="objective-edit">{objective ? objective.statement : t('Set conversation objective')}</button>
      {objective && <><span>{t(objective.status === 'active' ? 'Active' : objective.status === 'paused' ? 'Paused' : 'Completed')}</span>
        {objective.status === 'active' && <Button size="sm" label={t('Pause objective')} disabled={busy} onClick={() => void mutate('state', {expected_revision: objective.revision, status: 'paused'})} />}
        <Button size="sm" label={t('Continue objective')} disabled={busy || (objective.status === 'paused' && generation?.active === true)} onClick={() => void mutate('continue', {expected_revision: objective.revision,
          workspace, ...(route ? {selected_model: route.model, selected_endpoint_id: route.endpointId, selected_endpoint_url: route.endpointUrl} : {})})} />
        <Button size="sm" label={t('Complete objective')} disabled={busy} onClick={() => { setRevision(objective.revision); setCompleting(true); }} /></>}
    </div>
    {error && <p role="alert" className="fs-notice" data-tone="danger">{error}</p>}
    {objective?.status === 'paused' && generation?.active && <p role="status">{t('Pausing at the next safe point…')}</p>}
    {objective && <details><summary>{t('Brief, linked work and results')}</summary>
      <p>{objective.context}</p><p><strong>{t('Expected deliverable')}</strong> {objective.deliverable || '—'}</p>
      <p className="fs-muted">{t('Time with objective active')}: {Math.floor(objective.active_seconds / 60)} min · {t('Revision')} {objective.revision}</p>
      <p className="fs-muted">{t('Pausing requests a generation safe point. Schedules keep their own state. Completion is explicit.')}</p>
      <div className="fs-conversation-objective__links"><Button size="sm" label={t('Agents')} onClick={onAgents} /><Button size="sm" label={t('Tasks')} onClick={onTasks} /><Link to={`/activity?session=${encodeURIComponent(sessionId)}`}>{t('Conversation activity')}</Link></div>
      <ul>{objective.refs.map((value, i) => <li key={i}>
        {value.kind === 'task' ? <Link to={`/projects/${encodeURIComponent(value.project_id!)}?tab=objetivos&task=${encodeURIComponent(value.id!)}`}>{value.label || value.id}</Link> :
          value.kind === 'agent' ? <Link to={`/studio?s=${encodeURIComponent(value.id!)}`}>{value.label || value.id}</Link> :
          value.kind === 'schedule' ? <Link to={`/automations?task=${encodeURIComponent(value.id!)}`}>{value.label || value.id}</Link> :
          <button type="button" className="fs-studio__chip" onClick={() => openRef(value)}>{value.label || value.path}{value.kind === 'change' ? ` · ${t(value.scope === 'staged' ? 'Staged' : 'Unstaged')}` : ''}</button>}
      </li>)}</ul>
      <ul>{objective.runs.map(run => <li key={run.run_id}><button className="fs-studio__chip" type="button" onClick={() => setSelectedRun(run.run_id)}>{new Date(run.linked_at * 1000).toLocaleString()} · {t('Revision')} {run.revision} · {run.run_id.slice(0, 8)}</button></li>)}</ul>
      {objective.result && <p>{objective.result}</p>}
    </details>}
    <Dialog open={editing} onOpenChange={setEditing} title={t('Conversation objective')} testId="objective-dialog" footer={<Button label={t('Save')} disabled={busy || !brief.statement.trim()} onClick={() => void mutate('save', {expected_revision: revision, brief})} />}>
      <div className="fs-conversation-objective__form">
        <details><summary>{t('Start from a brief template')}</summary>
          <label>{t('Brief template')}<select value={templateId} onChange={e => setTemplateId(e.target.value)} data-testid="objective-template">
            <option value="">{t('Choose a template')}</option>
            {templates.map(item => <option key={item.id} value={item.id}>{item.title}</option>)}
          </select></label>
          {template && <><p>{template.brief.statement}</p><p>{template.brief.context}</p><p>{template.brief.deliverable}</p>
            <p className="fs-muted">{t('Apply replaces the draft text and keeps linked work. Save to keep your edits.')}</p>
            <Button size="sm" label={t('Apply template to draft')} onClick={() => { setBrief({...template.brief, refs: brief.refs}); setTemplateId(''); }} /></>}
          {templateError && <p role="alert">{templateError}</p>}
        </details>
        {conflict && objective && <section className="fs-notice" data-tone="warning"><strong>{t('Saved version')} · {t('Revision')} {objective.revision}</strong>
          <p>{objective.statement}</p><p>{objective.context}</p><p>{objective.deliverable}</p>
          <ul>{objective.refs.map((value, index) => <li key={index}>{value.label || value.path || value.id} · {value.kind} {value.scope || ''}</li>)}</ul>
          <Button label={t('Keep my draft on this version')} onClick={() => { setRevision(objective.revision); setConflict(false); setError(''); }} /></section>}
        <label>{t('Objective')}<textarea data-testid="objective-statement" value={brief.statement} onChange={e => setBrief({...brief, statement: e.target.value})} /></label>
        <label>{t('Context and sources')}<textarea value={brief.context} onChange={e => setBrief({...brief, context: e.target.value})} /></label>
        <label>{t('Expected deliverable')}<textarea value={brief.deliverable} onChange={e => setBrief({...brief, deliverable: e.target.value})} /></label>
        <fieldset><legend>{t('Link existing work')}</legend>
          <label>{t('Type')}<select value={ref.kind} onChange={e => setRef({kind: e.target.value as ObjectiveRef['kind'], workspace, project_id: projectId, scope: 'unstaged'})}><option value="resource">{t('File')}</option><option value="change">{t('Changes')}</option><option value="task">{t('Task')}</option><option value="agent">{t('Child conversation')}</option><option value="schedule">{t('Automation')}</option></select></label>
          <label>{t('Label')}<input value={ref.label ?? ''} onChange={e => setRef({...ref, label: e.target.value})} /></label>
          {ref.kind !== 'resource' && <label>{t(ref.kind === 'change' ? 'Repository ID' : 'Existing item ID')}<input value={ref.id ?? ''} onChange={e => setRef({...ref, id: e.target.value})} /></label>}
          {ref.kind === 'task' && <label>{t('Project ID')}<input value={ref.project_id ?? ''} onChange={e => setRef({...ref, project_id: e.target.value})} /></label>}
          {(ref.kind === 'resource' || ref.kind === 'change') && <><label>{t('Working folder')}<input value={ref.workspace ?? ''} onChange={e => setRef({...ref, workspace: e.target.value})} /></label><label>{t('File path')}<input value={ref.path ?? ''} onChange={e => setRef({...ref, path: e.target.value})} /></label></>}
          {ref.kind === 'change' && <label>{t('Change scope')}<select value={ref.scope} onChange={e => setRef({...ref, scope: e.target.value as 'staged' | 'unstaged'})}><option value="unstaged">{t('Unstaged')}</option><option value="staged">{t('Staged')}</option></select></label>}
          <Button size="sm" label={t('Add reference')} onClick={() => { setBrief({...brief, refs: [...brief.refs, ref]}); setRef({kind: 'resource', workspace, path: ''}); }} />
          <ul>{brief.refs.map((value, i) => <li key={i}>{value.label || value.path || value.id} <Button size="sm" label={t('Remove')} onClick={() => setBrief({...brief, refs: brief.refs.filter((_, n) => n !== i)})} /></li>)}</ul>
        </fieldset>
        {error && <p className="fs-notice" role="alert" data-tone="danger">{error}</p>}
      </div>
    </Dialog>
    <Dialog open={completing} onOpenChange={setCompleting} title={t('Complete objective')} footer={<Button label={t('Complete objective')} disabled={busy || !result.trim()} onClick={() => { if (objective) void mutate('state', {expected_revision: revision, status: 'complete', result}); }} />}>
      <label>{t('Delivered result')}<textarea value={result} onChange={e => setResult(e.target.value)} /></label>
    </Dialog>
    <Dialog open={Boolean(selectedRun)} onOpenChange={open => { if (!open) setSelectedRun(null); }} title={t('Linked run activity')}>
      <p><code>{selectedRun}</code> · {binding?.generation?.state ?? t('not reported')}</p>
      {binding?.brief && <p>{binding.brief.statement} · {binding.brief.deliverable}</p>}
      {runTurn ? <TurnActivity turn={runTurn} /> : <p className="fs-muted">{t('Detailed activity is not in the loaded history. The saved run identity is retained.')}</p>}
    </Dialog>
    <Dialog open={Boolean(diffRef)} onOpenChange={open => { if (!open) setDiffRef(null); }} title={`${t('Changes')} · ${t(diffRef?.scope === 'staged' ? 'Staged' : 'Unstaged')}`}>
      <DiffPane path={diffRef?.path ?? ''} diff={diff} loading={!diff && !diffError} error={diffError || null} />
    </Dialog>
  </section>;
}
