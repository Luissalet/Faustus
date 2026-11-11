import { useEffect, useState } from 'react';
import { getJson } from '../../adapters/api';
import { Button } from '../../components';
import { t } from '../../i18n';

interface Flow { id: string; name: string; version: string; trigger: string; steps: string; outputs: string; status: string; skill_url: string }
interface AppFlows { id: string; name: string; flows: Flow[]; shelf_url: string }

function WrittenFlow({ flow }: { flow: Flow }) {
  const [source, setSource] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  async function read() {
    setBusy(true); setError('');
    try {
      const r = await getJson<{ text?: string; error?: string }>(flow.skill_url);
      if (!r.text) throw new Error(r.error || t('The skill could not be read.'));
      setSource(r.text);
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  return <details style={{ paddingBlock: 12, borderBottom: '1px solid var(--fs-border)' }}>
    <summary style={{ cursor: 'pointer', overflowWrap: 'anywhere' }}>{flow.name}</summary>
    <p>{flow.version} · {flow.status === 'written' ? t('Written procedure') : t('Incomplete procedure')}</p>
    {([['Trigger', flow.trigger], ['Steps', flow.steps], ['Expected outputs and checks', flow.outputs]] as const).map(([label, value]) =>
      <section key={label}><h3>{t(label)}</h3><p style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', lineHeight: 1.6 }}>{value || t('Not declared in the skill.')}</p></section>)}
    <Button variant="secondary" size="sm" label={t('Read full skill')} loading={busy} onClick={() => void read()} />
    {error && <p role="alert">{error}</p>}
    {source && <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', font: 'inherit' }}>{source}</pre>}
  </details>;
}

export function AgentFlowsPanel() {
  const [apps, setApps] = useState<AppFlows[] | null>(null);
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');
  async function load() {
    setError('');
    try {
      const r = await getJson<{ ok: boolean; apps?: AppFlows[]; error?: string }>('/api/skills/library/agent-flows');
      if (!r.ok) throw new Error(r.error || t('The flow catalogue is unavailable.'));
      setApps(r.apps || []);
    } catch (e) { setError((e as Error).message); }
  }
  useEffect(() => { void load(); }, []);
  const words = query.toLocaleLowerCase().split(/\s+/).filter(Boolean);
  let count = 0;
  return <div className="fs-prose" style={{ maxWidth: '75ch' }}>
    <h2>{t('Agent flows by app')}</h2>
    <p>{t('Written procedures from each app’s current skill shelf. Expected outputs come from Outputs or Verification in the skill. Reading a flow does not run it.')}</p>
    <label>{t('Find app or flow')} <input className="fs-field" type="search" value={query} onChange={e => setQuery(e.target.value)} /></label>
    <Button variant="ghost" size="sm" label={t('Refresh')} onClick={() => void load()} />
    {error && <p role="alert">{error}</p>}
    {!apps && !error && <p role="status">{t('Loading…')}</p>}
    {apps?.map(app => {
      const flows = app.flows.filter(flow => words.every(w => `${app.name} ${flow.name} ${flow.trigger} ${flow.steps}`.toLocaleLowerCase().includes(w)));
      if (words.length && !flows.length && !words.every(w => app.name.toLocaleLowerCase().includes(w))) return null;
      count++;
      return <section key={app.id} style={{ marginBlock: 28 }}>
        <h2>{app.name}</h2><a href={app.shelf_url} target="_blank" rel="noreferrer">{t('Choose skills in the Hub')}</a>
        {!flows.length && <p>{t('No written flows on this shelf.')}</p>}
        {flows.map(flow => <WrittenFlow key={flow.id} flow={flow} />)}
      </section>;
    })}
    {apps && !count && <p>{t('No matching flows.')}</p>}
  </div>;
}
