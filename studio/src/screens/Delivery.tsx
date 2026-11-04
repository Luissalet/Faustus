import { Download, FileText, PackageCheck, Plus, RefreshCw, ShieldCheck, Trash2 } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { useSearchParams } from 'react-router';
import { Button, EmptyState, Skeleton } from '../components';
import * as api from '../adapters/delivery';
import { t, tn } from '../i18n';
import { ScopePanel } from './DeliveryScope';
import { OfficePanel } from './DeliveryOffice';
import { ReviewPanel } from './DeliveryReview';
import { DeleteItem } from './DeliveryDelete';
import './delivery.css';

/**
 * Delivery: the editable files, exports, authorised data, instructions and
 * limitations of a job, packed into one zip that was opened twice before it
 * is called ready -- from the sources and again from the zip itself.
 *
 * Every status is a word before it is a colour; "No reader" and "Does not
 * open" are never drawn in the verified tone.
 */

function Pill({ status }: { status: string }) {
  return <span className="fs-dlv__pill" data-tone={api.statusTone(status)} data-status={status}>{api.statusLabel(status)}</span>;
}

function ReadyBadge({ pkg }: { pkg: Pick<api.DeliveryPackage, 'ready' | 'reimport'> }) {
  const ok = api.isReady(pkg);
  return <span className="fs-dlv__pill fs-dlv__ready" data-tone={ok ? 'good' : 'bad'} data-testid="delivery-ready">{ok ? t('Ready to deliver') : t('Not ready')}</span>;
}

function BuildForm({ onBuilt }: { onBuilt: (p: api.DeliveryPackage) => void }) {
  const [name, setName] = useState('');
  const [items, setItems] = useState<api.DraftItem[]>([{ role: 'editable', path: '', authorized: false }, { role: 'export', path: '', authorized: false }]);
  const [instructions, setInstructions] = useState('');
  const [limitations, setLimitations] = useState('');
  const [softwareRoot, setSoftwareRoot] = useState('');
  const [lang, setLang] = useState('es');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const setItem = (i: number, patch: Partial<api.DraftItem>) => setItems((cur) => cur.map((it, k) => (k === i ? { ...it, ...patch } : it)));
  const submit = async () => {
    setBusy(true);
    setError('');
    try {
      onBuilt(await api.buildPackage({ name, items, instructions, limitations: api.lines(limitations), software_root: softwareRoot.trim() || undefined, lang }));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="fs-dlv__block" aria-label={t('New delivery package')} data-testid="delivery-form">
      <h2 className="fs-dlv__h">{t('New delivery package')}</h2>
      <label className="fs-dlv__field">{t('Name')}
        <input className="fs-field" value={name} onChange={(e) => setName(e.target.value)} data-testid="delivery-name" />
      </label>
      <div className="fs-dlv__items">
        {items.map((it, i) => (
          <div className="fs-dlv__item-row" key={i}>
            <select className="fs-field fs-dlv__role" value={it.role} aria-label={t('Role')} onChange={(e) => setItem(i, { role: e.target.value as api.Role })}>
              {api.ROLES.map((r) => <option key={r} value={r}>{api.roleLabel(r)}</option>)}
            </select>
            <input className="fs-field fs-dlv__path" placeholder={t('Full path of the file')} value={it.path} aria-label={t('File path')}
              onChange={(e) => setItem(i, { path: e.target.value })} data-testid={`delivery-path-${i}`} />
            {it.role === 'data' && (
              <label className="fs-dlv__auth"><input type="checkbox" checked={it.authorized} onChange={(e) => setItem(i, { authorized: e.target.checked })} />{t('May be shared')}</label>
            )}
            <button type="button" className="fs-dlv__icon" aria-label={t('Remove file')} onClick={() => setItems((cur) => cur.filter((_, k) => k !== i))}><Trash2 size={14} /></button>
          </div>
        ))}
        <Button variant="secondary" size="sm" icon={Plus} label={t('Add file')} onClick={() => setItems((cur) => [...cur, { role: 'other', path: '', authorized: false }])} />
      </div>
      <label className="fs-dlv__field">{t('Instructions')}
        <textarea className="fs-field" rows={3} value={instructions} onChange={(e) => setInstructions(e.target.value)} data-testid="delivery-instructions" />
      </label>
      <label className="fs-dlv__field">{t('Limitations (one per line)')}
        <textarea className="fs-field" rows={3} value={limitations} onChange={(e) => setLimitations(e.target.value)} data-testid="delivery-limitations" />
      </label>
      <div className="fs-dlv__row">
        <label className="fs-dlv__field fs-dlv__grow">{t('Software repository with a verified handoff (optional)')}
          <input className="fs-field" value={softwareRoot} onChange={(e) => setSoftwareRoot(e.target.value)} />
        </label>
        <label className="fs-dlv__field">{t('Document language')}
          <select className="fs-field" value={lang} onChange={(e) => setLang(e.target.value)}>
            <option value="es">Español</option><option value="en">English</option>
          </select>
        </label>
      </div>
      {error && <p className="fs-dlv__why" data-tone="bad" role="alert">{error}</p>}
      <div><Button icon={PackageCheck} label={busy ? t('Building and reopening…') : t('Build and verify')} onClick={() => { void submit(); }}
        variant="primary" disabled={busy || !name.trim()} testId="delivery-build" /></div>
    </section>
  );
}

function PackageDetail({ pkg, onReverified }: { pkg: api.DeliveryPackage; onReverified: (ok: boolean) => void }) {
  const [checking, setChecking] = useState(false);
  const [reverify, setReverify] = useState<{ ok: boolean; zip_same_hash: boolean } | null>(null);
  const reimport = new Map((pkg.reimport?.items ?? []).map((r) => [r.package_path, r]));
  const recheck = async () => {
    setChecking(true);
    try {
      const r = await api.reverifyPackage(pkg.id);
      setReverify(r.reverify);
      onReverified(r.reverify.ok);
    } finally {
      setChecking(false);
    }
  };
  return (
    <div className="fs-dlv__detail" data-testid="delivery-detail">
      <div className="fs-dlv__title-row">
        <h2 className="fs-dlv__title">{pkg.name}</h2>
        <ReadyBadge pkg={pkg} />
      </div>
      <p className="fs-dlv__muted">{pkg.created} · {pkg.id}{pkg.zip ? ` · ${(pkg.zip.size / 1024).toFixed(1)} KB · SHA-256 ${pkg.zip.sha256.slice(0, 12)}…` : ''}</p>
      <div className="fs-dlv__row">
        <a className="fs-btn fs-dlv__link" data-variant="secondary" data-size="sm" href={api.downloadHref(pkg.id)} download><Download size={14} /> {t('Download zip')}</a>
        <a className="fs-btn fs-dlv__link" data-variant="secondary" data-size="sm" href={api.documentHref(pkg.id)} target="_blank" rel="noreferrer"><FileText size={14} /> ENTREGA.md</a>
        <Button variant="secondary" size="sm" icon={ShieldCheck} label={checking ? t('Reopening…') : t('Reopen the zip again')} onClick={() => { void recheck(); }} disabled={checking} testId="delivery-reverify" />
        {reverify && <span className="fs-dlv__pill" data-tone={reverify.ok ? 'good' : 'bad'}>{reverify.ok ? t('Zip unchanged and every file opens') : t('The zip changed or a file no longer opens')}</span>}
      </div>
      {pkg.gaps.length > 0 && (
        <section className="fs-dlv__block" data-tone="bad" aria-label={t('What is missing')}>
          <h3 className="fs-dlv__h">{t('What is missing before it is ready')}</h3>
          <ul className="fs-dlv__plain">{pkg.gaps.map((g) => <li key={g}>{g}</li>)}</ul>
        </section>
      )}
      <section className="fs-dlv__block" aria-label={t('Contents')}>
        <h3 className="fs-dlv__h">{t('Contents')}</h3>
        <table className="fs-dlv__table">
          <thead><tr><th>{t('Role')}</th><th>{t('File')}</th><th>{t('From the source')}</th><th>{t('From the zip')}</th><th>{t('What the reader saw')}</th></tr></thead>
          <tbody>
            {pkg.items.map((it) => {
              const back = reimport.get(it.package_path);
              const facts = it.verification.facts ? Object.entries(it.verification.facts).filter(([k, v]) => k !== 'format' && (typeof v !== 'object' || Array.isArray(v))).slice(0, 4).map(([k, v]) => `${k}: ${typeof v === 'object' ? JSON.stringify(v) : String(v)}`).join(' · ') : '';
              return (
                <tr key={it.package_path}>
                  <td>{api.roleLabel(it.role)}</td>
                  <td className="fs-dlv__mono">{it.package_path}</td>
                  <td><Pill status={it.verification.status} /></td>
                  <td>{back ? <Pill status={back.status} /> : '—'}</td>
                  <td className="fs-dlv__muted">{it.verification.reason || facts || it.verification.level || ''}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>
      <section className="fs-dlv__block" aria-label={t('Instructions and limitations')}>
        <h3 className="fs-dlv__h">{t('Instructions')}</h3>
        <p className="fs-prose">{pkg.instructions || t('None written; an instructions file may be in the package.')}</p>
        <h3 className="fs-dlv__h">{t('Limitations')}</h3>
        {pkg.limitations.length ? <ul className="fs-dlv__plain">{pkg.limitations.map((l) => <li key={l}>{l}</li>)}</ul> : <p className="fs-dlv__muted">{t('None stated.')}</p>}
        {pkg.software && (
          <p className="fs-dlv__muted">{t('Software handoff')}: {pkg.software.counts ? `${pkg.software.counts.verified ?? 0} ${t('verified')}, ${pkg.software.counts.failed ?? 0} ${t('failed')}, ${pkg.software.counts['not run'] ?? 0} ${t('not run')}` : t('not verified')}</p>
        )}
        {pkg.excluded.length > 0 && (
          <>
            <h3 className="fs-dlv__h">{t('Left out of the package')}</h3>
            <ul className="fs-dlv__plain">{pkg.excluded.map((x) => <li key={x.path}><span className="fs-dlv__mono">{x.path}</span> — {x.reason}</li>)}</ul>
          </>
        )}
      </section>
    </div>
  );
}

export function DeliveryScreen() {
  const [params, setParams] = useSearchParams();
  const openId = params.get('p') ?? '';
  const raw = params.get('view') ?? '';
  const view = raw === 'scope' || raw === 'office' || raw === 'review' ? raw : 'packages';
  const setView = (v: string) => { const next = new URLSearchParams(params); if (v === 'packages') next.delete('view'); else next.set('view', v); setParams(next, { replace: true }); };
  const [rows, setRows] = useState<api.PackageRow[] | null>(null);
  const [pkg, setPkg] = useState<api.DeliveryPackage | null>(null);
  const [error, setError] = useState('');
  const [creating, setCreating] = useState(false);

  const reloadList = useCallback(async () => {
    try {
      setRows(await api.listPackages());
      setError('');
    } catch (e) {
      setError((e as Error).message);
      setRows((cur) => cur ?? []);
    }
  }, []);
  useEffect(() => { void reloadList(); }, [reloadList]);
  useEffect(() => {
    setPkg(null);
    if (!openId) return;
    api.loadPackage(openId).then(setPkg).catch((e) => setError((e as Error).message));
  }, [openId]);

  const open = (id: string) => {
    const next = new URLSearchParams(params);
    next.set('p', id);
    setParams(next, { replace: false });
    setCreating(false);
  };

  return (
    <div className="fs-screen fs-dlv" data-testid="delivery">
      <header className="fs-screen__head">
        <div>
          <h1 className="fs-screen__title">{t('Delivery')}</h1>
          <p className="fs-prose fs-dlv__lede">
            {t('Pack the editable files, exports, data you may share, instructions and limitations of a job. Each editable and export is opened with a reader for its format before copying and again from the finished zip; the package is ready only when both pass.')}
          </p>
        </div>
        <div className="fs-dlv__row">
          <Button variant="primary" icon={Plus} label={t('New package')} onClick={() => { setCreating(true); setView('packages'); }} testId="delivery-new" />
          <Button variant="secondary" size="sm" icon={RefreshCw} label={t('Reload')} onClick={() => { void reloadList(); }} />
        </div>
      </header>
      <div className="fs-tabs" role="tablist" aria-label={t('Delivery sections')}>
        <button type="button" role="tab" className="fs-tab" aria-selected={view === 'packages'} data-testid="delivery-tab-packages" onClick={() => setView('packages')}>{t('Packages')}</button>
        <button type="button" role="tab" className="fs-tab" aria-selected={view === 'scope'} data-testid="delivery-tab-scope" onClick={() => setView('scope')}>{t('Scope changes')}</button>
        <button type="button" role="tab" className="fs-tab" aria-selected={view === 'office'} data-testid="delivery-tab-office" onClick={() => setView('office')}>{t('Commissions')}</button>
        <button type="button" role="tab" className="fs-tab" aria-selected={view === 'review'} data-testid="delivery-tab-review" onClick={() => setView('review')}>{t('Reviews')}</button>
      </div>
      {view === 'scope' && <ScopePanel />}
      {view === 'office' && <OfficePanel />}
      {view === 'review' && <ReviewPanel />}
      {view === 'packages' && error && <p className="fs-dlv__why" data-tone="bad" role="alert">{error}</p>}
      {view === 'packages' && rows === null && <Skeleton label={t('Reading the packages')} count={3} height="56px" />}
      {view === 'packages' && <div className="fs-dlv__layout" data-single={!rows || rows.length === 0 || undefined}>
        <div>
          {rows && rows.length === 0 && !creating && (
            <EmptyState icon={PackageCheck} title={t('No delivery packages yet')} body={t('Create one with the files of a finished job.')} />
          )}
          {rows && rows.length > 0 && (
            <ul className="fs-dlv__list" aria-label={t('Delivery packages')}>
              {rows.map((r) => (
                <li className="fs-dlv__li" key={r.id}>
                  <button type="button" className="fs-dlv__item" data-active={r.id === openId} onClick={() => open(r.id)}>
                    <span className="fs-dlv__item-title">{r.name}</span>
                    <span className="fs-dlv__muted">{r.created} · {tn(r.items, '{n} file', '{n} files')}{r.gaps ? ` · ${tn(r.gaps, '{n} gap', '{n} gaps')}` : ''}</span>
                    <span className="fs-dlv__pill" data-tone={r.ready ? 'good' : 'bad'}>{r.ready ? t('Ready to deliver') : t('Not ready')}</span>
                  </button>
                  <DeleteItem label={r.name} confirm={t('Delete the package "{name}"? The copy kept by Faustus (folder and zip) is removed; the files it was built from are not touched.', { name: r.name })} onDelete={async () => { await api.deletePackage(r.id); if (r.id === openId) { const next = new URLSearchParams(params); next.delete('p'); setParams(next, { replace: true }); setPkg(null); } await reloadList(); }} testId={`delivery-delete-${r.id}`} />
                </li>
              ))}
            </ul>
          )}
        </div>
        <div>
          {creating && <BuildForm onBuilt={(p) => { void reloadList(); setPkg(p); open(p.id); }} />}
          {!creating && pkg && <PackageDetail key={pkg.id} pkg={pkg} onReverified={() => { void reloadList(); }} />}
          {!creating && !pkg && rows && rows.length > 0 && <p className="fs-dlv__muted">{t('Choose a package to see what was checked.')}</p>}
        </div>
      </div>}
    </div>
  );
}
