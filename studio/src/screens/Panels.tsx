import {
  ArrowDown, ArrowUp, ChevronLeft, ChevronRight, Database, ExternalLink, History, LayoutTemplate, PanelsTopLeft, Pause, Pencil,
  Play, Plus, RefreshCw, RotateCcw, Timer, Trash2,
} from 'lucide-react';
import { useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router';
import { Button, EmptyState, Skeleton } from '../components';
import * as api from '../adapters/panels';
import { t, tn } from '../i18n';
import './panels.css';

/**
 * Panels: the user's own pages. The manager lists them (rename, icon, order, versions,
 * restore, delete, a headless check, the tables and rows of the panel's database and its
 * server actions with their last run) and offers the template gallery; the viewer shows one
 * in a frame on this origin.
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

/** A cron line in words when it is one of the usual intervals. */
function scheduleText(cron: string | null | undefined, everyMinutes?: number): string {
  if (everyMinutes) return everyMinutes % 60 === 0 && everyMinutes >= 60
    ? tn(everyMinutes / 60, 'every hour', 'every {n} hours') : tn(everyMinutes, 'every minute', 'every {n} minutes');
  if (!cron) return t('only on demand');
  const m = cron.match(/^\*\/(\d+) \* \* \* \*$/);
  if (m) return tn(Number(m[1]), 'every minute', 'every {n} minutes');
  const h = cron.match(/^0 \*\/(\d+) \* \* \*$/);
  if (h) return tn(Number(h[1]), 'every hour', 'every {n} hours');
  if (cron === '0 * * * *') return tn(1, 'every hour', 'every {n} hours');
  if (cron === '* * * * *') return tn(1, 'every minute', 'every {n} minutes');
  if (cron === '0 0 * * *') return t('every day');
  return cron;
}

const when = (s: string | null | undefined) => {
  if (!s) return '';
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? s : d.toLocaleString();
};

function cellText(v: unknown): string {
  if (v === null || v === undefined) return '';
  const s = typeof v === 'object' ? JSON.stringify(v) : String(v);
  return s.length > 90 ? `${s.slice(0, 87)}…` : s;
}

const PAGE = 50;

function DataView({ panel }: { panel: api.Panel }) {
  const [tables, setTables] = useState<{ tables: api.PanelTable[]; bytes: number } | null>(null);
  const [table, setTable] = useState<api.PanelTable | null>(null);
  const [rows, setRows] = useState<api.PanelRows | null>(null);
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState('');
  const load = () => api.panelTables(panel.id).then(setTables).catch((e) => setError((e as Error).message));
  if (!tables && !error) void load();
  const open = async (tb: api.PanelTable, off: number) => {
    setTable(tb); setOffset(off); setError('');
    const q = `"${tb.name.replace(/"/g, '""')}"`;
    try {
      setRows(await api.panelQuery(panel.id, tb.type === 'table' ? `SELECT * FROM ${q} ORDER BY rowid DESC` : `SELECT * FROM ${q}`, [], PAGE, off));
    } catch (e) { setError((e as Error).message); }
  };
  return (
    <div className="fs-pnl__data" data-testid={`panel-data-${panel.id}`}>
      {error && <p className="fs-pnl__why" data-tone="bad" role="alert">{error}</p>}
      {!tables && !error && <Skeleton label={t('Reading the database')} count={2} height="24px" />}
      {tables && tables.tables.length === 0 && <p className="fs-pnl__muted">{t('This panel has no tables yet.')}</p>}
      {tables && tables.tables.length > 0 && (
        <>
          <p className="fs-pnl__muted">{tn(tables.tables.length, '{n} table', '{n} tables')} · {Math.max(1, Math.round(tables.bytes / 1024))} KB</p>
          <div className="fs-pnl__tables">
            {tables.tables.map((tb) => (
              <button key={tb.name} type="button" className="fs-pnl__table" data-on={table?.name === tb.name} data-testid={`panel-table-${panel.id}-${tb.name}`}
                onClick={() => { void open(tb, 0); }}>
                <span>{tb.name}</span>
                <span className="fs-pnl__muted">{tb.type === 'view' ? t('view') : tn(tb.rows ?? 0, '{n} row', '{n} rows')}</span>
              </button>
            ))}
          </div>
        </>
      )}
      {table && rows && (
        <div className="fs-pnl__grid-wrap">
          <p className="fs-pnl__muted">
            {table.type === 'table' ? t('Newest first') : t('View')} · {t('rows {from}–{to}', { from: offset + 1, to: offset + rows.row_count })}
            {' · '}{table.columns.map((c) => `${c.name}${c.type ? ` ${c.type}` : ''}`).join(', ')}
          </p>
          {rows.row_count === 0 ? <p className="fs-pnl__muted">{t('The table is empty.')}</p> : (
            <div className="fs-pnl__grid-scroll">
              <table className="fs-pnl__grid" data-testid={`panel-rows-${panel.id}`}>
                <thead><tr>{rows.columns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
                <tbody>{rows.rows.map((r, i) => <tr key={i}>{rows.columns.map((c) => <td key={c} title={cellText(r[c]).length > 86 ? JSON.stringify(r[c]) : undefined}>{cellText(r[c])}</td>)}</tr>)}</tbody>
              </table>
            </div>
          )}
          <div className="fs-pnl__actions">
            <Button variant="secondary" size="sm" icon={ChevronLeft} label={t('Previous')} disabled={offset === 0} onClick={() => { void open(table, Math.max(0, offset - PAGE)); }} />
            <Button variant="secondary" size="sm" icon={ChevronRight} label={t('Next')} disabled={!rows.more} onClick={() => { void open(table, offset + PAGE); }} />
            <Button variant="ghost" size="sm" icon={RefreshCw} label={t('Reload')} onClick={() => { void load(); void open(table, offset); }} />
          </div>
        </div>
      )}
    </div>
  );
}

function ActionsView({ panel }: { panel: api.Panel }) {
  const [actions, setActions] = useState<api.PanelAction[] | null>(null);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [note, setNote] = useState('');
  const load = () => api.panelActions(panel.id).then((r) => setActions(r.actions)).catch((e) => setError((e as Error).message));
  if (!actions && !error) void load();
  const act = async (name: string, fn: () => Promise<unknown>) => {
    setBusy(name); setError(''); setNote('');
    try { await fn(); } catch (e) { setError((e as Error).message); } finally { setBusy(''); await load(); }
  };
  return (
    <div className="fs-pnl__acts" data-testid={`panel-actions-${panel.id}`}>
      {error && <p className="fs-pnl__why" data-tone="bad" role="alert">{error}</p>}
      {note && <p className="fs-pnl__verdict" data-tone="good">{note}</p>}
      {!actions && !error && <Skeleton label={t('Reading the actions')} count={2} height="24px" />}
      {actions && actions.length === 0 && <p className="fs-pnl__muted">{t('This panel has no server actions. Ask for one in a chat, for example a sample every 5 minutes.')}</p>}
      {actions && actions.length > 0 && (
        <ul>
          {actions.map((a) => {
            const source = a.tool ? `${a.app ? `${a.app} · ` : ''}${a.tool}` : `${a.method ?? 'GET'} ${a.url ?? ''}`;
            const state = !a.schedule ? t('only on demand') : a.enabled === false ? t('paused') : scheduleText(a.schedule, a.every_minutes);
            return (
              <li key={a.name} className="fs-pnl__act" data-testid={`panel-action-${panel.id}-${a.name}`}>
                <div className="fs-pnl__act-head">
                  <Timer size={15} aria-hidden="true" />
                  <strong>{a.label || a.name}</strong>
                  <span className="fs-pnl__muted">{source}</span>
                  <span className="fs-pnl__chip" data-tone={a.enabled === false ? 'off' : 'on'}>{state}</span>
                </div>
                <p className="fs-pnl__muted">
                  {a.last_run
                    ? <>{t('Last run {when}', { when: when(a.last_run) })}: <span className="fs-pnl__verdict-inline" data-tone={a.last_ok ? 'good' : 'bad'}>{a.last_ok ? tn(a.last_saved ?? 0, '{n} value saved', '{n} values saved') : `${t('failed')}: ${a.last_error ?? ''}`}</span></>
                    : t('Never run')}
                  {' · '}{tn(a.runs, '{n} run', '{n} runs')}{a.failures ? ` · ${tn(a.failures, '{n} failure', '{n} failures')}` : ''}
                  {a.next_run && a.enabled !== false ? ` · ${t('next {when}', { when: when(a.next_run) })}` : ''}
                </p>
                {a.history.length > 1 && (
                  <p className="fs-pnl__muted fs-pnl__hist">
                    {a.history.slice(-6).reverse().map((h, i) => (
                      <span key={i} data-tone={h.ok ? 'good' : 'bad'} title={h.error ?? undefined}>{when(h.at)} {h.ok ? '✓' : '✗'}</span>
                    ))}
                  </p>
                )}
                <div className="fs-pnl__actions">
                  <Button variant="secondary" size="sm" icon={Play} label={t('Run now')} loading={busy === a.name} testId={`panel-action-run-${panel.id}-${a.name}`}
                    onClick={() => { void act(a.name, async () => { const r = await api.runPanelAction(panel.id, a.name); setNote(tn(r.saved, '{n} value saved', '{n} values saved')); }); }} />
                  {a.schedule && (
                    <Button variant="secondary" size="sm" icon={a.enabled === false ? Play : Pause} label={a.enabled === false ? t('Resume') : t('Pause')}
                      disabled={busy === a.name} onClick={() => { void act(a.name, () => api.setPanelActionEnabled(panel.id, a, a.enabled === false)); }} />
                  )}
                  <Button variant="ghost" size="sm" icon={Trash2} label={t('Remove')} disabled={busy === a.name}
                    onClick={() => {
                      if (window.confirm(t('Remove the action "{name}"? The data it already saved stays.', { name: a.label || a.name }))) void act(a.name, () => api.removePanelAction(panel.id, a.name));
                    }} />
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

function TemplateGallery({ onClose }: { onClose: () => void }) {
  const navigate = useNavigate();
  const [templates, setTemplates] = useState<api.PanelTemplate[] | null>(null);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  if (!templates && !error) void api.listTemplates().then(setTemplates).catch((e) => setError((e as Error).message));
  const create = async (tpl: api.PanelTemplate) => {
    setBusy(tpl.id); setError('');
    try {
      const made = await api.createFromTemplate(tpl.id);
      await api.refreshPanels();
      const failed = made.first_runs.filter((r) => !r.ok);
      if (failed.length) setError(t('Created, but the first run of {name} failed: {why}', { name: failed[0].action, why: failed[0].error ?? '' }));
      else { onClose(); navigate(`/panels/${made.id}`); }
    } catch (e) { setError((e as Error).message); } finally { setBusy(''); }
  };
  return (
    <section className="fs-pnl__gallery" aria-label={t('Templates')} data-testid="panel-templates">
      <div className="fs-pnl__bar">
        <h2 className="fs-pnl__bar-title">{t('Templates')}</h2>
        <span className="fs-pnl__muted">{t('Ready-made panels with real data; each one is an ordinary panel afterwards that you can change in a chat.')}</span>
        <span className="fs-pnl__grow" />
        <Button variant="ghost" size="sm" label={t('Close')} onClick={onClose} />
      </div>
      {error && <p className="fs-pnl__why" data-tone="bad" role="alert">{error}</p>}
      {!templates && !error && <Skeleton label={t('Reading the templates')} count={2} height="72px" />}
      {templates && (
        <ul className="fs-pnl__tpls">
          {templates.map((tpl) => {
            const Icon = api.panelIcon(tpl.icon);
            return (
              <li key={tpl.id} className="fs-pnl__tpl" data-testid={`panel-template-${tpl.id}`}>
                <div className="fs-pnl__main"><Icon size={18} aria-hidden="true" /><strong>{tpl.name}</strong></div>
                <p className="fs-pnl__tpl-text">{tpl.description}</p>
                <p className="fs-pnl__muted">{t('Data: {source}', { source: tpl.source })}</p>
                {tpl.actions.map((a) => (
                  <p key={a.name} className="fs-pnl__muted">{t('Saves on its own: {label}, {when}', { label: a.label ?? a.name, when: scheduleText(a.schedule) })}</p>
                ))}
                <Button variant="primary" size="sm" icon={Plus} label={t('Create')} loading={busy === tpl.id} disabled={!!busy}
                  testId={`panel-template-create-${tpl.id}`} onClick={() => { void create(tpl); }} />
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function Row({ panel, index, total }: { panel: api.Panel; index: number; total: number }) {
  const navigate = useNavigate();
  const panels = api.usePanels() ?? [];
  const [name, setName] = useState<string | null>(null);
  const [showVersions, setShowVersions] = useState(false);
  const [showData, setShowData] = useState(false);
  const [showActions, setShowActions] = useState(false);
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
        <Button variant="secondary" size="sm" icon={Database} label={t('Data')} testId={`panel-data-btn-${panel.id}`} onClick={() => setShowData((v) => !v)} />
        <Button variant="secondary" size="sm" icon={Timer} label={t('Actions')} testId={`panel-actions-btn-${panel.id}`} onClick={() => setShowActions((v) => !v)} />
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
      {showData && <DataView panel={panel} />}
      {showActions && <ActionsView panel={panel} />}
    </li>
  );
}

export function PanelsScreen() {
  const panels = api.usePanels();
  const navigate = useNavigate();
  const [gallery, setGallery] = useState(false);
  return (
    <div className="fs-screen fs-pnl" data-testid="panels">
      <header className="fs-screen__head">
        <div>
          <h1 className="fs-screen__title">{t('Panels')}</h1>
          <p className="fs-prose fs-pnl__lede">{t('Your own pages. Describe one in a chat and Faustus builds it, tests it and keeps it in the sidebar; every change is saved as a version you can go back to.')}</p>
        </div>
        <div className="fs-pnl__actions">
          <Button variant="secondary" icon={LayoutTemplate} label={t('From a template')} testId="panels-templates" onClick={() => setGallery((g) => !g)} />
          <Button variant="primary" icon={Plus} label={t('New panel')} testId="panels-new" onClick={() => navigate(api.newPanelDraftHref())} />
        </div>
      </header>
      {gallery && <TemplateGallery onClose={() => setGallery(false)} />}
      {panels === null && <Skeleton label={t('Reading the panels')} count={3} height="56px" />}
      {panels && panels.length === 0 && (
        <EmptyState icon={PanelsTopLeft} title={t('No panels yet')} body={t('Start a chat with "New panel" and say what the page should show or do, or create one from a template.')} />
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
