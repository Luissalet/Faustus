import { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router';
import { RefreshCw, Save } from 'lucide-react';
import { Button } from '../../components';
import { ApiError } from '../../adapters/api';
import { connectorKey, connectorName, hasConnectorServer, listConnectors, stateLabel, type Connector } from '../../adapters/connectors';
import { choiceKey, choiceSelected, coordinationDraft, coordinationState, getCoordinationChoices,
  getProjectCoordination, saveProjectCoordination, spaces, type CoordinationSpace, type CoordinationChoice, type CoordinationView } from '../../adapters/coordination';
import type { Project } from '../../adapters/projects';
import { t } from '../../i18n';
import './coordination.css';
import '../../components/coordinationFields.css';

const STATE_TEXT = {
  not_configured: 'Not configured', disabled: 'Coordination disabled', unavailable: 'Coordination unavailable',
  prepared: 'Project configuration saved', available: 'Coordination available',
} as const;

export function CoordinationPanel({ project, onNotice }: { project: Project; onNotice: (text: string) => void }) {
  const [view, setView] = useState<CoordinationView | null>(null);
  const [connections, setConnections] = useState<Connector[]>([]);
  const [connector, setConnector] = useState('');
  const [agoras, setAgoras] = useState<CoordinationSpace[]>([]);
  const [spaceId, setSpaceId] = useState('ecosystem');
  const [choices, setChoices] = useState<CoordinationChoice[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [enabled, setEnabled] = useState(true);
  const [editing, setEditing] = useState(false);
  const [loading, setLoading] = useState(true);
  const [scopeLoading, setScopeLoading] = useState(false);
  const [scopeFailure, setScopeFailure] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const dirty = useRef(false);
  const loadedProject = useRef(project.id);

  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;
    if (loadedProject.current !== project.id) { dirty.current = false; loadedProject.current = project.id; }
    setLoading(true); setFailure(null);
    Promise.allSettled([getProjectCoordination(project.id, controller.signal), listConnectors(), spaces(controller.signal)]).then(results => {
      if (cancelled) return;
      const state = results[0], catalogue = results[1];
      if (state.status === 'fulfilled') {
        setView(state.value);
        if (!dirty.current) {
          setConnector(state.value.template?.connector_id ?? '');
          setSpaceId(state.value.template?.space_id || 'ecosystem');
          setEnabled(state.value.template?.active ?? true);
          setEditing(!state.value.template);
        }
      } else setFailure(t('Could not read coordination settings. Try again.'));
      if (catalogue.status === 'fulfilled') setConnections(catalogue.value);
      else { setConnections([]); setFailure(t('Could not load saved connections. Try again.')); }
      if (results[2].status === 'fulfilled') setAgoras(results[2].value);
      setLoading(false);
    });
    return () => { cancelled = true; controller.abort(); };
  }, [project.id, refresh]);

  useEffect(() => {
    const controller = new AbortController();
    setChoices([]); setScopeFailure(false);
    if (!connector || !view) { setScopeLoading(false); return () => controller.abort(); }
    setScopeLoading(true);
    getCoordinationChoices(project.id, connector, controller.signal).then(result => {
      if (controller.signal.aborted) return;
      setChoices(result.choices);
      setScopeFailure(Boolean(result.unavailable));
      const saved = view.template?.connector_id === connector ? view.template : null;
      setSelected(previous => dirty.current ? new Set([...previous].filter(key => result.choices.some(choice => choiceKey(choice) === key)))
        : new Set(result.choices.filter(choice => choiceSelected(saved, choice)).map(choiceKey)));
      setScopeLoading(false);
    }).catch(() => {
      if (controller.signal.aborted) return;
      setScopeFailure(true); setScopeLoading(false);
    });
    return () => controller.abort();
  }, [project.id, connector, view]);

  const save = useCallback(async (active = enabled) => {
    if (!view || !connector || busy) return;
    setBusy(true); setFailure(null);
    try {
      // Revocation keeps the saved scope intact and works without the connection.
      const draft = !active && view.template
        ? { ...coordinationDraft(view.template, view.template.connector_id, [], new Set(), false),
            resources: view.template.resources, repos: view.template.repos }
        : coordinationDraft(view.template, connector, choices, selected, active);
      const previousSpace = view.template?.space_id || 'ecosystem';
      await saveProjectCoordination(project.id, {...draft, space_id: spaceId,
        thread_ids: spaceId === previousSpace ? draft.thread_ids : spaceId === 'ecosystem' ? [32] : []});
      dirty.current = false;
      setEditing(false); setRefresh(value => value + 1);
      onNotice(t(active ? 'Project coordination saved.' : 'Project coordination disabled.'));
    } catch (error) {
      setFailure(error instanceof ApiError && error.status === 409
        ? t('The coordination settings changed. Reload them before saving again.')
        : t('Could not save coordination settings. Check the connection and try again.'));
    } finally { setBusy(false); }
  }, [view, connector, spaceId, busy, enabled, choices, selected, project.id, onNotice]);

  const permitted = connections.filter(connection => !hasConnectorServer(connection) || !project.connector_ids || project.connector_ids.includes(connection.server.id));
  const savedConnection = connections.find(connection => hasConnectorServer(connection) && connection.server.id === view?.template?.connector_id);
  const currentConnection = permitted.find(connection => hasConnectorServer(connection) && connection.server.id === connector);
  const state = view ? coordinationState(view) : null;
  const canSave = !loading && !scopeLoading && !busy && Boolean(connector)
    && (!enabled || (!scopeFailure && Boolean(currentConnection?.server?.is_enabled) && selected.size > 0));
  return <section className="fs-coordination" data-testid="project-coordination">
    <div className="fs-coordination__head"><h4>{t('Work coordination in Agora')}</h4>
      {view?.template && !editing && <Button variant="ghost" size="sm" label={t('Change')} onClick={() => setEditing(true)} />}
    </div>
    {loading ? <p className="fs-coordination__secondary" role="status">{t('Reading coordination settings…')}</p> : <>
      {state && <p className="fs-coordination__state" data-state={state} role="status">{t(STATE_TEXT[state])}</p>}
      <p>{t('Faustus uses this project configuration in its conversations. Write the assignment in the usual chat.')}</p>
      {view?.template && !editing && <>
        <p className="fs-coordination__secondary">{t('Connection')}: {savedConnection ? connectorName(savedConnection) : t('Saved connection unavailable')}</p>
        <p className="fs-coordination__secondary">{t('Agora')}: {agoras.find(space => space.id === spaceId)?.title || spaceId}</p>
        {choices.filter(choice => choiceSelected(view.template, choice)).length > 0 && <ul className="fs-coordination__folders">
          {choices.filter(choice => choiceSelected(view.template, choice)).map(choice => <li key={choiceKey(choice)}>{choice.label}<small>{choice.path}</small></li>)}
        </ul>}
        {view.template.active && <Button variant="ghost" size="sm" label={t('Disable coordination')} onClick={() => void save(false)} disabled={busy} />}
      </>}
      {editing && view && <form className="fs-coordination__form" onSubmit={event => { event.preventDefault(); void save(); }}>
        <label>{t('Agora')}<select className="fs-field fs-coordination-agora-select" aria-label={t('Agora')} value={spaceId} disabled={busy} onChange={event => {dirty.current = true; setSpaceId(event.target.value);}}>
          {!agoras.some(space => space.id === spaceId) && <option value={spaceId}>{spaceId}</option>}
          {agoras.map(space => <option key={space.id} value={space.id}>{space.title}</option>)}
        </select></label>
        <label>{t('Saved connection')}<select className="fs-field" value={connector} onChange={event => { dirty.current = true; setSelected(new Set()); setConnector(event.target.value); }} disabled={busy} data-testid="coordination-connector">
          <option value="">{t('Choose a saved connection')}</option>
          {connector && !permitted.some(connection => connection.server?.id === connector) && <option value={connector}>{t('Saved connection unavailable')}</option>}
          {permitted.map(connection => <option key={connectorKey(connection)} value={connection.server?.id ?? connectorKey(connection)} disabled={!connection.server?.is_enabled}>
            {connectorName(connection)}{!hasConnectorServer(connection) ? ` · ${t('Not set up')}` : !connection.server.is_enabled ? ` · ${stateLabel('disabled')}` : ''}
          </option>)}
        </select></label>
        <p className="fs-coordination__secondary"><Link to="/connectors">{t('Open connections')}</Link></p>
        <fieldset disabled={busy || scopeLoading} className="fs-coordination__scope"><legend>{t('Work folders in this project')}</legend>
          {scopeLoading ? <p role="status">{t('Reading project folders…')}</p> : choices.map(choice => <label className="fs-coordination__choice" key={choiceKey(choice)}>
            <input type="checkbox" checked={selected.has(choiceKey(choice))} onChange={event => setSelected(previous => {
              dirty.current = true;
              const next = new Set(previous); if (event.target.checked) next.add(choiceKey(choice)); else next.delete(choiceKey(choice)); return next;
            })} />
            <span>{choice.label}<small>{choice.path}</small></span>
          </label>)}
          {connector && !scopeLoading && !choices.length && <p className="fs-coordination__secondary">{t(scopeFailure
            ? 'Project folders could not be checked. Reconnect the saved connection and retry.'
            : 'No project work folders are available through this connection.')}</p>}
        </fieldset>
        <label className="fs-coordination__choice"><input type="checkbox" checked={enabled} disabled={busy} onChange={event => { dirty.current = true; setEnabled(event.target.checked); }} />
          <span>{t('Enable coordination for this project')}</span></label>
        <div className="fs-coordination__actions">
          <Button type="submit" variant="primary" size="sm" icon={Save} label={t('Save')} disabled={!canSave} loading={busy} testId="coordination-save" />
          {view.template && <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => { dirty.current = false; setRefresh(value => value + 1); }} disabled={busy} />}
        </div>
      </form>}
    </>}
    {failure && <p className="fs-coordination__error" role="alert">{failure}</p>}
    {(failure || scopeFailure || state === 'unavailable') && <Button variant="ghost" size="sm" icon={RefreshCw} label={t('Retry')} onClick={() => setRefresh(value => value + 1)} disabled={busy} />}
  </section>;
}
