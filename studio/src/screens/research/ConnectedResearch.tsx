import { useEffect, useState, type MouseEvent, type ReactNode } from 'react';
import { useNavigate } from 'react-router';
import { ChevronRight, Folder, Plug } from 'lucide-react';
import { iconUrl } from '../../adapters/appDock';
import { safeExternal } from '../../lib/markdown';
import { connectedTitle, groupEntries, sourceGroup, sourceGroups, type SourceGroup } from './connectedSources';
import { Button } from '../../components';
import { connectedPlan, connectedSources, openResearchSource, type ConnectedSource, type ResearchProgress, type ResearchSettings, type ResearchSource } from '../../adapters/research';
import { t } from '../../i18n';
import { Rich } from '../rich';

const FILTER_LABELS: Record<string, string> = { after: 'After', before: 'Before', account_id: 'Account', folder: 'Folder',
  project_id: 'Project', kind: 'Type', issuer: 'Issuer', year: 'Year', state: 'State', tag: 'Tag', order: 'Order', offset: 'Offset',
  pinned: 'Pinned', radar_from: 'From radar', radar_to: 'To radar', saved: 'Saved filter', channel: 'Channel', tools: 'Include tools',
  deep: 'Read deeply', start: 'Start', end: 'End', top: 'Results', id: 'Item', url: 'URL', path: 'File path', with_data: 'Include data',
  doc: 'Document', doc_ids: 'Documents', reveal: 'Show original identifiers' };

export function ConnectedSources({ query, settings, onChange }: { query: string; settings: ResearchSettings; onChange: (s: ResearchSettings) => void }) {
  const [sources, setSources] = useState<ConnectedSource[]>([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [brief, setBrief] = useState<{ query: string; plan: string[]; clarification: string } | null>(null);
  const refresh = () => {
    setError('');
    void connectedSources().then(setSources).catch(e => setError(String(e.message ?? e)));
  };
  useEffect(refresh, []);
  const selected = settings.sourceIds ?? sources.map(s => s.id);
  const prepare = async () => {
    setLoading(true); setError('');
    try { setBrief({ ...await connectedPlan(query, selected, settings), query }); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setLoading(false); }
  };
  return <div className="fs-rs__connected" data-testid="connected-research-settings">
    <p className="fs-muted">{t('Search the sources you already have. Each app keeps its own data.')}</p>
    <fieldset className="fs-rs__source-picker">
      <legend>{t('Sources to consult')}</legend>
      {sources.map(source => <label key={source.id}>
        <input type="checkbox" checked={selected.includes(source.id)} onChange={e => onChange({ ...settings, sourceIds: e.target.checked ? [...selected, source.id] : selected.filter(id => id !== source.id) })} />
        <span>{t(source.name)}<small>{source.lanes.includes('semantic') ? t('Text + semantic search') : t('Text search · semantic search unavailable')}</small></span>
      </label>)}
      {!sources.length && !error && <p role="status">{t('Loading sources…')}</p>}
    </fieldset>
    <Button size="sm" variant="ghost" label={t('Refresh sources')} onClick={refresh} />
    {sources.filter(s => selected.includes(s.id) && s.filters.length > 0).map(source => <details key={source.id} className="fs-rs__source-filters">
      <summary>{t('Filters')} · {t(source.name)}</summary>
      <div className="fs-rs__settings">{source.filters.map(key => <label key={key} className="fs-rs__setting">
        <span>{t(FILTER_LABELS[key] ?? key.replaceAll('_', ' '))}</span>
        <input className="fs-field" type={['after', 'before'].includes(key) ? 'date' : 'text'} value={settings.sourceFilters?.[source.id]?.[key] ?? ''} onChange={e => {
          const filters = { ...settings.sourceFilters?.[source.id] };
          if (e.target.value) filters[key] = e.target.value; else delete filters[key];
          onChange({ ...settings, sourceFilters: { ...settings.sourceFilters, [source.id]: filters } });
        }} />
      </label>)}</div>
    </details>)}
    <div className="fs-inline"><Button size="sm" variant="secondary" label={t('Prepare research plan')} loading={loading} disabled={!query.trim() || !selected.length} onClick={() => void prepare()} /><span className="fs-muted">{t('Optional: clarify the scope before starting.')}</span></div>
    {brief && brief.query === query && <div className="fs-rs__brief"><h3>{t('Research plan')}</h3><ol>{brief.plan.map((step, i) => <li key={i}>{step}</li>)}</ol>{brief.clarification && <p>{brief.clarification}</p>}</div>}
    <label className="fs-rs__setting"><span>{t('Clarification or context (optional)')}</span><textarea className="fs-field" rows={2} value={settings.clarification ?? ''} onChange={e => onChange({ ...settings, clarification: e.target.value })} /></label>
    {selected.length === 0 && <p role="alert">{t('Select at least one source.')}</p>}
    {error && <p role="alert">{error}</p>}
  </div>;
}

export function ConnectedSourceLabel({ group }: { group: SourceGroup }) {
  const [broken, setBroken] = useState(false);
  const Icon = group.key === 'project_files' ? Folder : Plug;
  return <span className="fs-rs__source-label">
    {group.appId && !broken
      ? <img src={iconUrl(group.appId)} width={16} height={16} alt="" onError={() => setBroken(true)} />
      : <Icon size={16} aria-hidden="true" />}
    {t(group.name)}
  </span>;
}

function TimelineTime({ at }: { at: number }) {
  const date = new Date(at * 1000);
  return <time dateTime={date.toISOString()}>{date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</time>;
}

export function ConnectedTimeline({ progress, sources = [] }: { progress?: ResearchProgress | null; sources?: ResearchSource[] }) {
  if (!progress?.plan && !progress?.timeline?.length) return null;
  const events = progress.timeline ?? [];
  const groups = sourceGroups(events, sources);
  const buckets = groupEntries(events, event => groups.get(event.source) ?? sourceGroup(event.source));
  return <div className="fs-rs__connected-trace" data-testid="connected-research-timeline">
    {progress.plan && <details open><summary>{t('Research plan')}</summary><div className="fs-prose"><Rich text={progress.plan} /></div></details>}
    {!!buckets.length && <details open><summary>{t('Source timeline')}</summary><div className="fs-rs__timeline">
      {buckets.map(group => <details key={group.key} className="fs-rs__timeline-group">
        <summary><ChevronRight size={14} aria-hidden="true" /><ConnectedSourceLabel group={group} /> <TimelineTime at={group.entries[group.entries.length - 1].at} /></summary>
        <ol aria-label={t(group.name)}>
          {group.entries.map(event => <li key={event.id} data-status={event.status}>
            <TimelineTime at={event.at} />
            <div><strong>{t(event.lane === 'read' ? 'Reading original' : event.lane === 'ranking' ? 'Hybrid ranking' : event.lane === 'semantic' ? 'Semantic search' : 'Text search')}</strong> · {t(event.status === 'done' ? 'Completed' : event.status === 'error' ? 'Unavailable' : 'Searching…')}
              <p>{event.query}</p>{event.status === 'done' && <small>{t('{n} citable results', { n: event.count })}</small>}{event.message && <p className="fs-muted">{event.message}</p>}
            </div>
          </li>)}
        </ol>
      </details>)}
    </div></details>}
  </div>;
}

/** Cited app originals open in the existing App Dock with the report's chat. */
export function ConnectedReport({ id, sources, children }: { id: string; sources: ResearchSource[]; children: ReactNode }) {
  const navigate = useNavigate();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const click = async (event: MouseEvent<HTMLDivElement>) => {
    const link = (event.target as HTMLElement).closest('a');
    if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
    const source = sources.find(s => s.appId && (s.url === link.href || s.url === link.getAttribute('href')));
    if (!source) return;
    event.preventDefault();
    if (busy) return;
    setBusy(true); setError('');
    try { navigate(`/studio?s=${encodeURIComponent(await openResearchSource(id, source.url))}&dock=pick`); }
    catch { setError(t('This source could not open beside the chat. Open the original in its own window.')); }
    finally { setBusy(false); }
  };
  return <div onClick={e => void click(e)} aria-busy={busy || undefined}>{children}{error && <div role="alert"><p>{error}</p>{sources.filter(s => s.appId && safeExternal(s.url)).map(s => <a key={s.url} href={s.url} target="_blank" rel="noopener noreferrer" onClick={e => e.stopPropagation()}>{connectedTitle(s)}</a>)}</div>}</div>;
}
