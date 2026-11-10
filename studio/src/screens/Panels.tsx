import { ArrowDown, ArrowUp, ExternalLink, History, PanelsTopLeft, Pencil, Play, Plus, RefreshCw, RotateCcw, Trash2 } from 'lucide-react';
import { useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router';
import { Button, EmptyState, Skeleton } from '../components';
import * as api from '../adapters/panels';
import { t, tn } from '../i18n';
import './panels.css';

/**
 * Panels: the user's own pages. The manager lists them (rename, icon, order, versions,
 * restore, delete, a headless check); the viewer shows one in a frame on this origin.
 */

function editDraftHref(name: string): string {
  return `/studio?draft=${encodeURIComponent(t('I want to change the panel "{name}" so that ', { name }))}`;
}

function CheckResult({ id, result, stamp }: { id: string; result: api.PanelCheck; stamp: number }) {
  const problems = result.console_errors.length + result.page_errors.length + result.failed_requests.length;
  return (
    <div className="fs-pnl__check" data-testid={`panel-check-${id}`} data-ok={result.ok}>
      <p className="fs-pnl__verdict" data-tone={result.ok ? 'good' : 'bad'}>
        {result.ok ? t('Renders without errors') : tn(problems || 1, '{n} problem found', '{n} problems found')}
      </p>
      <ul className="fs-pnl__problems">
        {result.console_errors.map((e, i) => <li key={`c${i}`}>{t('Console')}: {e.text}</li>)}
        {result.page_errors.map((e, i) => <li key={`p${i}`}>{t('Page error')}: {e}</li>)}
        {result.failed_requests.map((f, i) => <li key={`f${i}`}>{t('Failed request')}: {f.status ?? f.error} {f.url}</li>)}
        {result.notes.map((n, i) => <li key={`n${i}`}>{n}</li>)}
      </ul>
      {result.screenshot && <img className="fs-pnl__shot" alt={t('Screenshot of the panel')} src={api.checkImageHref(id, stamp)} />}
    </div>
  );
}

function Versions({ panel, onRestored }: { panel: api.Panel; onRestored: () => void }) {
  const [data, setData] = useState<{ current: number; versions: api.PanelVersion[] } | null>(null);
  const [error, setError] = useState('');
  const load = () => api.panelVersions(panel.id).then(setData).catch((e) => setError((e as Error).message));
  if (!data && !error) void load();
  return (
    <div className="fs-pnl__versions" data-testid={`panel-versions-${panel.id}`}>
      {error && <p className="fs-pnl__why" data-tone="bad" role="alert">{error}</p>}
      {!data && !error && <Skeleton label={t('Reading the versions')} count={2} height="24px" />}
      {data && (
        <ul>
          {data.versions.map((v) => (
            <li key={v.version}>
              <span>{t('Version {n}', { n: v.version })}{v.version === data.current ? ` · ${t('current')}` : ''}</span>
              <span className="fs-pnl__muted">{v.saved.replace('T', ' ')} · {Math.max(1, Math.round(v.bytes / 1024))} KB</span>
              {v.version !== data.current && (
                <Button variant="secondary" size="sm" icon={RotateCcw} label={t('Restore')} testId={`panel-restore-${panel.id}-${v.version}`}
                  onClick={async () => {
                    try { await api.restorePanel(panel.id, v.version); await api.refreshPanels(); onRestored(); await load(); } catch (e) { setError((e as Error).message); }
                  }} />
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Row({ panel, index, total }: { panel: api.Panel; index: number; total: number }) {
  const navigate = useNavigate();
  const panels = api.usePanels() ?? [];
  const [name, setName] = useState<string | null>(null);
  const [showVersions, setShowVersions] = useState(false);
  const [check, setCheck] = useState<{ result: api.PanelCheck; stamp: number } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const Icon = api.panelIcon(panel.icon);
  const guard = async (fn: () => Promise<unknown>) => {
    setBusy(true); setError('');
    try { await fn(); await api.refreshPanels(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  const move = (delta: number) => {
    const ids = panels.map((p) => p.id);
    const target = index + delta;
    if (target < 0 || target >= ids.length) return;
    [ids[index], ids[target]] = [ids[target], ids[index]];
    void guard(() => api.reorderPanels(ids));
  };
  return (
    <li className="fs-pnl__row" data-testid={`panel-row-${panel.id}`}>
      <div className="fs-pnl__main">
        <Icon size={18} aria-hidden="true" />
        <div className="fs-pnl__titles">
          {name === null ? (
            <button type="button" className="fs-pnl__name" onClick={() => navigate(`/panels/${panel.id}`)}>{panel.name}</button>
          ) : (
            <form className="fs-pnl__rename" onSubmit={(e) => { e.preventDefault(); void guard(async () => { await api.updatePanel(panel.id, { name }); setName(null); }); }}>
              <input className="fs-field" value={name} onChange={(e) => setName(e.target.value)} aria-label={t('Panel name')} data-testid={`panel-name-${panel.id}`} autoFocus />
              <Button type="submit" variant="primary" size="sm" label={t('Save')} disabled={!name.trim()} />
              <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setName(null)} />
            </form>
          )}
          <span className="fs-pnl__muted">{panel.description || t('No description')} · {t('Version {n}', { n: panel.version })}</span>
        </div>
        <select className="fs-field fs-pnl__icon" value={api.PANEL_ICONS[panel.icon] ? panel.icon : 'layout-dashboard'} aria-label={t('Icon')}
          data-testid={`panel-icon-${panel.id}`} disabled={busy} onChange={(e) => { void guard(() => api.updatePanel(panel.id, { icon: e.target.value })); }}>
          {Object.keys(api.PANEL_ICONS).map((k) => <option key={k} value={k}>{k}</option>)}
        </select>
      </div>
      <div className="fs-pnl__actions">
        <Button variant="secondary" size="sm" icon={Play} label={t('Open')} testId={`panel-open-${panel.id}`} onClick={() => navigate(`/panels/${panel.id}`)} />
        <Button variant="secondary" size="sm" icon={ExternalLink} label={t('Open in a window')} onClick={() => window.open(api.pageHref(panel.id), `panel-${panel.id}`, 'popup,width=1100,height=780')} />
        <Button variant="secondary" size="sm" icon={Pencil} label={t('Rename')} onClick={() => setName(panel.name)} />
        <Button variant="secondary" size="sm" icon={ArrowUp} label={t('Up')} disabled={busy || index === 0} onClick={() => move(-1)} testId={`panel-up-${panel.id}`} />
        <Button variant="secondary" size="sm" icon={ArrowDown} label={t('Down')} disabled={busy || index === total - 1} onClick={() => move(1)} testId={`panel-down-${panel.id}`} />
        <Button variant="secondary" size="sm" icon={History} label={t('Versions')} testId={`panel-versions-btn-${panel.id}`} onClick={() => setShowVersions((v) => !v)} />
        <Button variant="secondary" size="sm" icon={RefreshCw} label={t('Check')} loading={busy} testId={`panel-check-btn-${panel.id}`}
          onClick={() => { void guard(async () => setCheck({ result: await api.checkPanel(panel.id), stamp: Date.now() })); }} />
        <Link className="fs-pnl__link" to={editDraftHref(panel.name)}>{t('Change it in a chat')}</Link>
        <button type="button" className="fs-pnl__del" aria-label={t('Delete {name}', { name: panel.name })} title={t('Delete')} data-testid={`panel-delete-${panel.id}`}
          onClick={() => {
            if (window.confirm(t('Delete the panel "{name}" with all its versions and stored data?', { name: panel.name }))) void guard(() => api.deletePanel(panel.id));
          }}><Trash2 size={14} aria-hidden="true" /></button>
      </div>
      {error && <p className="fs-pnl__why" data-tone="bad" role="alert">{error}</p>}
      {check && <CheckResult id={panel.id} result={check.result} stamp={check.stamp} />}
      {showVersions && <Versions panel={panel} onRestored={() => undefined} />}
    </li>
  );
}

export function PanelsScreen() {
  const panels = api.usePanels();
  const navigate = useNavigate();
  return (
    <div className="fs-screen fs-pnl" data-testid="panels">
      <header className="fs-screen__head">
        <div>
          <h1 className="fs-screen__title">{t('Panels')}</h1>
          <p className="fs-prose fs-pnl__lede">{t('Your own pages. Describe one in a chat and Faustus builds it, tests it and keeps it in the sidebar; every change is saved as a version you can go back to.')}</p>
        </div>
        <Button variant="primary" icon={Plus} label={t('New panel')} testId="panels-new" onClick={() => navigate(api.newPanelDraftHref())} />
      </header>
      {panels === null && <Skeleton label={t('Reading the panels')} count={3} height="56px" />}
      {panels && panels.length === 0 && (
        <EmptyState icon={PanelsTopLeft} title={t('No panels yet')} body={t('Start a chat with "New panel" and say what the page should show or do.')} />
      )}
      {panels && panels.length > 0 && (
        <ul className="fs-pnl__list" aria-label={t('Panels')}>
          {panels.map((p, i) => <Row key={p.id} panel={p} index={i} total={panels.length} />)}
        </ul>
      )}
    </div>
  );
}

export function PanelViewScreen() {
  const { panelId } = useParams();
  const { panel } = api.usePanel(panelId);
  const [stamp, setStamp] = useState(0);
  if (panel === undefined) return <Skeleton label={t('Reading the panel')} count={2} height="56px" />;
  if (panel === null || !panelId) {
    return (
      <div className="fs-screen fs-pnl">
        <EmptyState icon={PanelsTopLeft} title={t('This panel does not exist')} body={t('It may have been deleted.')} />
        <Link className="fs-pnl__link" to="/panels">{t('Manage panels')}</Link>
      </div>
    );
  }
  return (
    <div className="fs-pnl fs-pnl--view" data-testid="panel-view">
      <header className="fs-pnl__bar">
        <h1 className="fs-pnl__bar-title">{panel.name}</h1>
        <span className="fs-pnl__muted">{t('Version {n}', { n: panel.version })}</span>
        <span className="fs-pnl__grow" />
        <Button variant="ghost" size="sm" icon={RefreshCw} label={t('Reload')} onClick={() => setStamp((s) => s + 1)} />
        <Button variant="ghost" size="sm" icon={ExternalLink} label={t('Open in a window')} onClick={() => window.open(api.pageHref(panel.id), `panel-${panel.id}`, 'popup,width=1100,height=780')} />
        <Link className="fs-pnl__link" to={editDraftHref(panel.name)}>{t('Change it in a chat')}</Link>
        <Link className="fs-pnl__link" to="/panels">{t('Manage panels')}</Link>
      </header>
      <iframe key={`${panel.id}-${panel.version}-${stamp}`} className="fs-pnl__frame" title={panel.name} data-testid="panel-frame"
        src={api.pageHref(panel.id, `${panel.version}-${stamp}`)} />
    </div>
  );
}
