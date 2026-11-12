import { AppWindow, ChevronRight, ExternalLink, FileText, RefreshCw, Search } from 'lucide-react';
import { useEffect, useState } from 'react';
import { getJson } from '../../adapters/api';
import { iconUrl } from '../../adapters/appDock';
import { Button } from '../../components';
import { t, tn } from '../../i18n';
import { Rich } from '../rich';
import './AgentFlows.css';

interface Flow { id: string; name: string; description?: string; version: string; trigger: string; steps: string; outputs: string; status: string; skill_url: string }
interface AppFlows { id: string; name: string; flows: Flow[]; shelf_url: string }

function AppIcon({ id }: { id: string }) {
  const [broken, setBroken] = useState(false);
  return <span className="fs-flows__icon" aria-hidden="true">
    {broken ? <AppWindow size={24} /> : <img src={iconUrl(id)} alt="" loading="lazy" onError={() => setBroken(true)} />}
  </span>;
}

function WrittenFlow({ flow }: { flow: Flow }) {
  const [source, setSource] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const title = flow.name.replace(/[-_]+/g, ' ').replace(/^\w/, c => c.toUpperCase());
  async function read() {
    setBusy(true); setError('');
    try {
      const r = await getJson<{ text?: string; error?: string }>(flow.skill_url);
      if (r.text) setSource(r.text);
      else setError(t(r.error || 'The skill could not be read.'));
    } catch { setError(t('The skill could not be read. Check the Hub connection and retry.')); }
    finally { setBusy(false); }
  }
  return <details className="fs-flows__flow">
    <summary className="fs-flows__summary">
      <ChevronRight className="fs-flows__chevron" size={16} aria-hidden="true" />
      <span className="fs-flows__flow-heading">
        <strong>{title}</strong>
        {flow.description && <span className="fs-flows__description" title={flow.description}>{flow.description}</span>}
      </span>
      {flow.status !== 'written' && <span className="fs-flows__incomplete">{t('Incomplete procedure')}</span>}
    </summary>
    <div className="fs-flows__reader">
      <p className="fs-flows__meta">{[flow.version, flow.status === 'written' ? t('Written procedure') : t('Incomplete procedure')].filter(Boolean).join(' · ')}</p>
      {flow.description && <p className="fs-flows__intro">{flow.description}</p>}
      {([['Trigger', flow.trigger], ['Steps', flow.steps], ['Expected outputs and checks', flow.outputs]] as const).map(([label, value]) =>
        <section className="fs-flows__section" key={label}><h4>{t(label)}</h4><Rich text={value || t('Not declared in the skill.')} /></section>)}
      <Button variant="secondary" size="sm" icon={FileText} label={t('Read full skill')} loading={busy} onClick={() => void read()} />
      {error && <p className="fs-sk__error" role="alert">{error}</p>}
      {source && <pre className="fs-sk__pre fs-flows__source">{source}</pre>}
    </div>
  </details>;
}

export function AgentFlowsPanel() {
  const [apps, setApps] = useState<AppFlows[] | null>(null);
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');
  const [busy, setBusy] = useState(false);
  async function load() {
    setError(''); setBusy(true);
    try {
      const r = await getJson<{ ok: boolean; apps?: AppFlows[]; error?: string }>('/api/skills/library/agent-flows');
      if (r.ok) setApps(r.apps || []);
      else setError(t(r.error || 'The flow catalogue is unavailable.'));
    } catch { setError(t('The Hub flow catalogue is unavailable. Check the Hub connection and retry.')); }
    finally { setBusy(false); }
  }
  useEffect(() => { void load(); }, []);
  const words = query.toLocaleLowerCase().split(/\s+/).filter(Boolean);
  const populated = apps?.filter(app => app.flows.length).map(app => ({ ...app,
    flows: app.flows.filter(flow => words.every(w => `${app.name} ${flow.name} ${flow.description || ''} ${flow.trigger} ${flow.steps}`.toLocaleLowerCase().includes(w))),
  })).filter(app => app.flows.length) || [];
  const empty = apps?.filter(app => !app.flows.length && words.every(w => app.name.toLocaleLowerCase().includes(w))) || [];
  return <div className="fs-flows">
    <header className="fs-flows__head">
      <h2>{t('Agent flows by app')}</h2>
      <p>{t('Explore when to use each app’s skills, the steps to follow and the results to expect.')}</p>
    </header>
    <div className="fs-sk__toolbar fs-flows__toolbar">
      <label className="fs-sk__search">
        <Search size={13} aria-hidden="true" />
        <input type="search" placeholder={t('Find app or flow')} aria-label={t('Find app or flow')} value={query} onChange={e => setQuery(e.target.value)} />
      </label>
      <Button variant="secondary" size="sm" icon={RefreshCw} label={t('Refresh')} loading={busy} onClick={() => void load()} />
    </div>
    {error && <p className="fs-sk__error" role="alert">{error}</p>}
    {!apps && busy && <p className="fs-sk__hint" role="status">{t('Loading…')}</p>}
    {populated.map(app => <section className="fs-flows__app" key={app.id} aria-label={app.name}>
      <header className="fs-flows__app-head">
        <AppIcon id={app.id} />
        <div className="fs-flows__app-title"><h3>{app.name}</h3><span>{tn(app.flows.length, '{n} flow', '{n} flows')}</span></div>
        <a className="fs-flows__shelf" href={app.shelf_url} target="_blank" rel="noreferrer">{t('Choose skills in the Hub')}<ExternalLink size={13} aria-hidden="true" /></a>
      </header>
      {app.flows.map(flow => <WrittenFlow key={flow.id} flow={flow} />)}
    </section>)}
    {!!empty.length && <p className="fs-flows__empty"><span>{t('No written flows:')}</span>{' '}{empty.map((app, i) => <span key={app.id}>{i > 0 && ' · '}<a href={app.shelf_url} target="_blank" rel="noreferrer">{app.name}</a></span>)}</p>}
    {apps && !populated.length && !empty.length && <p className="fs-sk__hint" role="status">{t('No matching flows.')}</p>}
  </div>;
}
