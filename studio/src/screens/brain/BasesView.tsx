import { Database, Pencil, Plus, RefreshCw, Trash2 } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { Button, Dialog, EmptyState } from '../../components';
import * as api from '../../adapters/brainBases';
import { ensureOverlayRoot } from '../../shell/overlayRoot';
import { t } from '../../i18n';
import './bases.css';

/**
 * Obsidian Bases inside the Cerebro: every `.base` file of the vault, evaluated by the server
 * against the notes (filters, formulas, sort, groups, summaries) and shown as its table, cards or
 * list. A new base is made from tags, a folder, conditions and columns; the YAML can be edited
 * and is validated before it is saved, so the same file keeps opening in Obsidian.
 */
export function BasesView({ onOpenNote }: { onOpenNote: (path: string) => void }) {
  const [bases, setBases] = useState<api.BaseFile[] | null>(null);
  const [path, setPath] = useState('');
  const [view, setView] = useState('');
  const [result, setResult] = useState<api.BaseResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [newOpen, setNewOpen] = useState(false);
  const [yamlOpen, setYamlOpen] = useState(false);

  const reloadList = useCallback(async (select?: string) => {
    try {
      const list = await api.listBases();
      setBases(list);
      const next = select ?? (path && list.some((b) => b.path === path) ? path : list[0]?.path ?? '');
      setPath(next);
    } catch (e) { setError((e as Error).message); setBases((b) => b ?? []); }
  }, [path]);
  useEffect(() => { void reloadList(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const run = useCallback(async () => {
    if (!path) { setResult(null); return; }
    setBusy(true); setError('');
    try {
      const r = await api.queryBase(path);
      setResult(r);
      setView((v) => (r.views.some((x) => x.name === v) ? v : r.views[0]?.name ?? ''));
    } catch (e) { setError((e as Error).message); setResult(null); } finally { setBusy(false); }
  }, [path]);
  useEffect(() => { void run(); }, [run]);

  const current = result?.views.find((v) => v.name === view) ?? result?.views[0];
  return (
    <div className="fs-bases" data-testid="brain-bases">
      <div className="fs-bases__bar">
        <Database size={14} aria-hidden="true" />
        <select className="fs-field" value={path} onChange={(e) => setPath(e.target.value)} aria-label={t('Base')} data-testid="bases-select">
          {(bases ?? []).map((b) => <option key={b.path} value={b.path}>{b.name}{b.ok ? '' : ` (${t('invalid')})`}</option>)}
        </select>
        {result && result.views.length > 1 && (
          <div className="fs-seg" role="radiogroup" aria-label={t('View')}>
            {result.views.map((v) => <button key={v.name} type="button" role="radio" aria-checked={current?.name === v.name} onClick={() => setView(v.name)} data-testid={`bases-view-${v.name}`}>{v.name}</button>)}
          </div>)}
        <span className="fs-bases__grow" />
        <Button variant="ghost" size="sm" icon={RefreshCw} label={t('Refresh')} loading={busy} onClick={() => { void run(); }} />
        <Button variant="ghost" size="sm" icon={Plus} label={t('New base')} onClick={() => setNewOpen(true)} testId="bases-new" />
        <Button variant="ghost" size="sm" icon={Pencil} label={t('Edit YAML')} disabled={!path} onClick={() => setYamlOpen(true)} testId="bases-edit" />
        <Button variant="ghost" size="sm" icon={Trash2} label={t('Delete')} disabled={!path}
          onClick={() => { if (window.confirm(t('Delete the base {name}? The notes are not touched.', { name: path }))) void api.deleteBase(path).then(() => reloadList('')); }} testId="bases-delete" />
      </div>
      {error && <p className="fs-bases__msg" data-tone="bad" role="alert">{error}</p>}
      {bases && bases.length === 0 && <EmptyState icon={Database} title={t('No bases yet')} body={t('A base is a saved view of your notes: which notes (by tag, folder or property), which columns, how to sort and group. Obsidian opens the same .base files.')} />}
      {result && current && (
        <>
          <p className="fs-bases__muted" data-testid="bases-summary">
            {t('{m} of {n} notes match; this view shows {s} of {total}.', { m: result.matched, n: result.notes_scanned, s: current.shown, total: current.total })}
            {current.note ? ` ${t('The map view is shown as a table.')}` : ''}
          </p>
          {current.groups
            ? current.groups.map((g) => <section key={g.key} className="fs-bases__group"><h4 className="fs-bases__gh">{g.label} <span className="fs-bases__muted">({g.rows.length})</span></h4><Rows view={current} rows={g.rows} onOpenNote={onOpenNote} /></section>)
            : <Rows view={current} rows={current.rows} onOpenNote={onOpenNote} />}
          {Object.keys(current.summaries).length > 0 && (
            <p className="fs-bases__muted" data-testid="bases-summaries">{Object.entries(current.summaries).map(([prop, s]) => `${t(s.name)} ${prop}: ${s.value}`).join(' · ')}</p>)}
          {result.errors.length > 0 && <p className="fs-bases__msg" data-tone="warn">{t('Some values could not be computed')}: {result.errors.slice(0, 3).join('; ')}</p>}
        </>)}
      {createPortal(<>
        <NewBaseDialog open={newOpen} onOpenChange={setNewOpen} onSaved={(p) => { setNewOpen(false); void reloadList(p); }} />
        {yamlOpen && path && <YamlDialog path={path} onClose={() => setYamlOpen(false)} onSaved={() => { setYamlOpen(false); void reloadList(path).then(run); }} />}
      </>, ensureOverlayRoot())}
    </div>
  );
}

function Rows({ view, rows, onOpenNote }: { view: api.BaseView; rows: api.BaseRow[]; onOpenNote: (path: string) => void }) {
  if (!rows.length) return <p className="fs-bases__muted">{t('No notes in this view.')}</p>;
  if (view.type === 'cards') {
    return <ul className="fs-bases__cards" data-testid="bases-cards">{rows.map((r) => (
      <li key={r.path}><button type="button" className="fs-bases__card" onClick={() => onOpenNote(r.path)}>
        <strong>{r.title}</strong>
        {view.columns.filter((c) => c.id !== 'file.name').map((c) => r.cells[c.id] ? <span key={c.id} className="fs-bases__muted">{c.label}: {r.cells[c.id]}</span> : null)}
      </button></li>))}</ul>;
  }
  if (view.type === 'list') {
    return <ul className="fs-bases__list" data-testid="bases-list">{rows.map((r) => (
      <li key={r.path}><button type="button" className="fs-bases__link" onClick={() => onOpenNote(r.path)}>{r.title}</button>
        {view.columns.filter((c) => c.id !== 'file.name').map((c) => r.cells[c.id] ? <span key={c.id} className="fs-bases__muted"> · {r.cells[c.id]}</span> : null)}</li>))}</ul>;
  }
  return (
    <div className="fs-bases__tablewrap">
      <table className="fs-bases__table" data-testid="bases-table">
        <thead><tr>{view.columns.map((c) => <th key={c.id}>{c.label}</th>)}</tr></thead>
        <tbody>{rows.map((r) => <tr key={r.path}>{view.columns.map((c, i) => (
          <td key={c.id}>{i === 0 ? <button type="button" className="fs-bases__link" onClick={() => onOpenNote(r.path)}>{r.cells[c.id] || r.title}</button> : r.cells[c.id]}</td>))}</tr>)}</tbody>
      </table>
    </div>
  );
}

function NewBaseDialog({ open, onOpenChange, onSaved }: { open: boolean; onOpenChange: (o: boolean) => void; onSaved: (path: string) => void }) {
  const [name, setName] = useState('');
  const [tags, setTags] = useState('');
  const [folder, setFolder] = useState('');
  const [where, setWhere] = useState('');
  const [columns, setColumns] = useState('file.name, file.mtime');
  const [type, setType] = useState('table');
  const [group, setGroup] = useState('');
  const [sort, setSort] = useState('');
  const [error, setError] = useState('');
  const split = (s: string) => s.split(',').map((x) => x.trim()).filter(Boolean);
  const save = async () => {
    setError('');
    try {
      const r = await api.saveBase(name.trim(), { spec: { tags: split(tags), folder: folder.trim() || undefined, where: where.trim() ? [where.trim()] : [],
        columns: split(columns), view_type: type, view_name: t('Table'), group_by: group.trim() || undefined, sort_by: sort.trim() || undefined } });
      onSaved(r.path);
    } catch (e) { setError((e as Error).message); }
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange} title={t('New base')} testId="bases-new-dialog"
      footer={<><Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => onOpenChange(false)} />
        <Button variant="primary" size="sm" label={t('Create')} disabled={!name.trim()} onClick={() => { void save(); }} testId="bases-new-save" /></>}>
      <div className="fs-bases__form">
        <label>{t('Name')}<input className="fs-field" value={name} onChange={(e) => setName(e.target.value)} data-testid="bases-new-name" /></label>
        <label>{t('Notes with any of these tags (comma-separated)')}<input className="fs-field" value={tags} onChange={(e) => setTags(e.target.value)} data-testid="bases-new-tags" /></label>
        <label>{t('Only in this folder')}<input className="fs-field" value={folder} onChange={(e) => setFolder(e.target.value)} /></label>
        <label>{t('Extra condition (e.g. status != "done")')}<input className="fs-field" value={where} onChange={(e) => setWhere(e.target.value)} /></label>
        <label>{t('Columns (file.name, a property, file.mtime…)')}<input className="fs-field" value={columns} onChange={(e) => setColumns(e.target.value)} data-testid="bases-new-columns" /></label>
        <div className="fs-bases__formrow">
          <label>{t('View')}<select className="fs-field" value={type} onChange={(e) => setType(e.target.value)}>
            <option value="table">{t('Table')}</option><option value="cards">{t('Cards')}</option><option value="list">{t('List')}</option></select></label>
          <label>{t('Group by')}<input className="fs-field" value={group} onChange={(e) => setGroup(e.target.value)} /></label>
          <label>{t('Sort by')}<input className="fs-field" value={sort} onChange={(e) => setSort(e.target.value)} /></label>
        </div>
        {error && <p className="fs-bases__msg" data-tone="bad" role="alert">{error}</p>}
      </div>
    </Dialog>
  );
}

function YamlDialog({ path, onClose, onSaved }: { path: string; onClose: () => void; onSaved: () => void }) {
  const [text, setText] = useState('');
  const [check, setCheck] = useState<{ ok: boolean; errors: string[]; warnings: string[] } | null>(null);
  const [error, setError] = useState('');
  useEffect(() => { void api.readBase(path).then((r) => { setText(r.yaml); setCheck(r.validation); }).catch((e) => setError((e as Error).message)); }, [path]);
  const validate = async () => { try { setCheck(await api.validateBase(text)); } catch (e) { setError((e as Error).message); } };
  const save = async () => { setError(''); try { await api.saveBase(path, { yaml: text }); onSaved(); } catch (e) { setError((e as Error).message); } };
  return (
    <Dialog open onOpenChange={(o) => { if (!o) onClose(); }} title={path} testId="bases-yaml-dialog"
      footer={<><Button variant="ghost" size="sm" label={t('Check')} onClick={() => { void validate(); }} />
        <Button variant="ghost" size="sm" label={t('Cancel')} onClick={onClose} />
        <Button variant="primary" size="sm" label={t('Save')} onClick={() => { void save(); }} testId="bases-yaml-save" /></>}>
      <textarea className="fs-field fs-bases__yaml" rows={18} value={text} onChange={(e) => { setText(e.target.value); setCheck(null); }} spellCheck={false} data-testid="bases-yaml" />
      {check && (check.ok ? <p className="fs-bases__msg" data-tone="good">{t('Valid.')}{check.warnings.length ? ` ${check.warnings.join('; ')}` : ''}</p>
        : <p className="fs-bases__msg" data-tone="bad">{check.errors.join('; ')}</p>)}
      {error && <p className="fs-bases__msg" data-tone="bad" role="alert">{error}</p>}
    </Dialog>
  );
}
