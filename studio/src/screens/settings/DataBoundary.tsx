import { Download, RefreshCw } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { Button, Skeleton } from '../../components';
import { loadDataBoundaryReport, type DataBoundaryReport, type DataRoute, type DestinationScope } from '../../adapters/dataBoundary';
import { t } from '../../i18n';
import './data-boundary.css';

const SCOPES: Record<DestinationScope, string> = {
  machine: 'This machine', network: 'Another machine on the network', external: 'External service', unknown: 'Destination not fully known',
};
const CATEGORIES = [
  ['models', 'Models and conversations'], ['auxiliary', 'Voice and retrieval'],
  ['connectors', 'Connectors and APIs'], ['channels', 'Messaging and mail'],
  ['web', 'Web search and browsing'], ['sync', 'Synchronizations'],
] as const;
const PROFILES: Record<string, string> = { local_only: 'Local only', local_preferred: 'Local preferred', cloud_allowed: 'Cloud allowed' };
const ROLES: Record<string, string> = { default: 'Default AI', utility: 'Utility model', task: 'Task model', vision: 'Vision model', research: 'Research model', research_blind_review: 'Research review', rerank: 'Reranking' };

function RouteRow({ route }: { route: DataRoute }) {
  const uses = route.conversations ?? [];
  return <li className="fs-boundary__row" data-scope={route.scope} data-enabled={route.enabled}>
    <div className="fs-boundary__identity"><strong>{route.provider ? t(route.name, { provider: route.provider }) : t(route.name)}</strong><span className="fs-boundary__scope">{t(SCOPES[route.scope])}{!route.enabled && <> · {t('Disabled')}</>}</span></div>
    {route.address && <code className="fs-boundary__address">{route.address}</code>}
    <p className="fs-prose">{t(route.data)}</p>
    {!!route.models?.length && <p className="fs-boundary__metadata">{t('Models')}: {route.models.join(', ')}</p>}
    {!!route.roles?.length && <p className="fs-boundary__metadata">{t('Configured use')}: {route.roles.map(role => t(ROLES[role] ?? role)).join(', ')}</p>}
    {!!route.user_override_count && <p className="fs-boundary__metadata">{t('User overrides')}: {route.user_override_count}</p>}
    {uses.length > 0 && <details className="fs-boundary__uses"><summary>{t('Conversations using this destination')} ({uses.length})</summary>
      <ul>{uses.map(use => <li key={use.id}><span>{use.name}</span><span className="fs-boundary__metadata">{use.project_name || t('No project')}{use.archived ? ` · ${t('Archived')}` : ''} · {use.model}</span></li>)}</ul>
    </details>}
  </li>;
}

export function DataBoundarySection() {
  const [report, setReport] = useState<DataBoundaryReport | null>(null);
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(false);
  const [showDisabled, setShowDisabled] = useState(false);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(false);
    loadDataBoundaryReport(controller.signal).then(result => { if (!controller.signal.aborted) setReport(result); }).catch(() => { if (!controller.signal.aborted) setError(true); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [revision]);
  const download = useCallback(() => {
    if (!report) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' }));
    const anchor = document.createElement('a'); anchor.href = url; anchor.download = 'faustus-data-destinations.json'; anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }, [report]);
  const visible = report?.routes.filter(route => showDisabled || route.enabled) ?? [];
  return <div className="fs-boundary" data-testid="data-boundary-report">
    <section className="fs-set__card">
      <div className="fs-set__card-head"><h2>{t('Data destinations')}</h2></div>
      <p className="fs-prose">{t('See what can leave the machine running Faustus under its current configuration.')}</p>
      <div className="fs-boundary__actions"><Button size="sm" icon={RefreshCw} loading={loading} label={t('Refresh report')} onClick={() => setRevision(n => n + 1)} />
        <Button size="sm" icon={Download} disabled={!report || loading || error} label={t('Download report')} onClick={download} /></div>
      {loading && !report && <Skeleton height="100px" label={t('Loading…')} />}
      {error && <p role="alert" className="fs-prose">{t('The report could not be refreshed. Retry to read the current configuration.')}</p>}
      {report && <><p className="fs-boundary__metadata">{t(error ? 'Previous report' : 'Computed at')}: <time dateTime={report.computed_at}>{new Date(report.computed_at).toLocaleString()}</time> · {t('Active profile')}: {t(PROFILES[report.profile] ?? report.profile)}</p>
        <label className="fs-boundary__filter"><input type="checkbox" checked={showDisabled} onChange={event => setShowDisabled(event.target.checked)} />{t('Include disabled configuration')}</label>
        {report.missing_sources.length > 0 && <p role="status" className="fs-prose">{t('Some configuration could not be read. This report is incomplete.')}: {report.missing_sources.join(', ')}</p>}</>}
    </section>
    {report && CATEGORIES.map(([key, label]) => {
      const rows = visible.filter(route => route.category === key);
      return <section key={key} className="fs-set__card"><div className="fs-set__card-head"><h3>{t(label)}</h3></div>
        {rows.length ? <ul className="fs-boundary__routes">{rows.map(route => <RouteRow key={route.id} route={route} />)}</ul> : <p className="fs-prose">{t('No enabled destinations in this category.')}</p>}
      </section>;
    })}
    {report && <section className="fs-set__card"><div className="fs-set__card-head"><h3>{t('Local storage and report limits')}</h3></div>
      <ul className="fs-boundary__notes">{[...report.local, ...report.limits].map(note => <li key={note}>{t(note)}</li>)}</ul>
      {report.projects.some(project => project.privacy_profile) && <details><summary>{t('Project privacy profiles')}</summary><ul className="fs-boundary__notes">{report.projects.filter(project => project.privacy_profile).map(project => <li key={project.id}>{project.name}: {t(PROFILES[project.privacy_profile!] ?? project.privacy_profile!)}</li>)}</ul></details>}
    </section>}
  </div>;
}
