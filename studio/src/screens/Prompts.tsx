import { Copy, Download, Heart, Pencil, Plus, Search, Star, Trash2, Upload } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router';
import { Button, Dialog, EmptyState, IconButton, Skeleton, Toast } from '../components';
import {
  createPrompt,
  deletePrompt,
  exportPrompts,
  importPrompts,
  listPrompts,
  previewRender,
  renderPrompt,
  toggleFavoritePrompt,
  updatePrompt,
  type PromptDraft,
  type PromptTemplate,
  type PromptVariable,
} from '../adapters/prompts';
import { ApiError } from '../adapters/api';
import { t } from '../i18n';
import './library.css';

/**
 * UX-10: the prompt library.
 *
 * A saved template pastes into the composer draft — it is never sent from
 * here. "Use" always ends on /studio with the rendered text pre-filled and
 * the person still holding Send.
 */

const EMPTY_VAR: PromptVariable = { name: '', type: 'string', required: true, default: undefined, choices: undefined, help: '' };

function blankDraft(): PromptDraft {
  return { name: '', description: '', body: '', variables: [], scope: 'personal', tags: [] };
}

export function PromptsScreen() {
  const navigate = useNavigate();
  const [templates, setTemplates] = useState<PromptTemplate[] | null>(null);
  const [query, setQuery] = useState('');
  const [failed, setFailed] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [editing, setEditing] = useState<PromptTemplate | null>(null);
  const [editorOpen, setEditorOpen] = useState(false);
  const [draft, setDraft] = useState<PromptDraft>(blankDraft());
  const [saving, setSaving] = useState(false);
  const [useTarget, setUseTarget] = useState<PromptTemplate | null>(null);
  const [useValues, setUseValues] = useState<Record<string, string>>({});
  const [importOpen, setImportOpen] = useState(false);
  const [importText, setImportText] = useState('');
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const say = (msg: string) => setNotice(msg);

  const reload = async () => {
    try {
      const rows = await listPrompts({ q: query });
      setTemplates(rows);
      setFailed(false);
    } catch {
      setFailed(true);
    }
  };

  useEffect(() => {
    void reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query]);

  const visible = useMemo(() => templates ?? [], [templates]);

  const openCreate = () => {
    setEditing(null);
    setDraft(blankDraft());
    setEditorOpen(true);
  };

  const openEdit = (tpl: PromptTemplate) => {
    setEditing(tpl);
    setDraft({ name: tpl.name, description: tpl.description, body: tpl.body, variables: tpl.variables, scope: tpl.scope, projectId: tpl.projectId, tags: tpl.tags, examples: tpl.examples });
    setEditorOpen(true);
  };

  const save = async () => {
    setSaving(true);
    try {
      if (editing) await updatePrompt(editing.id, draft);
      else await createPrompt(draft);
      setEditorOpen(false);
      await reload();
      say(t('Saved'));
    } catch (e) {
      say(e instanceof ApiError ? e.message : t('Could not save the template'));
    } finally {
      setSaving(false);
    }
  };

  const remove = async (tpl: PromptTemplate) => {
    try {
      await deletePrompt(tpl.id);
      await reload();
      say(t('Deleted'));
    } catch (e) {
      say(e instanceof ApiError ? e.message : t('Could not delete the template'));
    }
  };

  const favorite = async (tpl: PromptTemplate) => {
    try {
      await toggleFavoritePrompt(tpl.id);
      await reload();
    } catch (e) {
      say(e instanceof ApiError ? e.message : t('Could not update favorites'));
    }
  };

  const openUse = (tpl: PromptTemplate) => {
    const values: Record<string, string> = {};
    for (const v of tpl.variables) if (v.default !== undefined && v.default !== null) values[v.name] = String(v.default);
    setUseValues(values);
    setUseTarget(tpl);
  };

  const confirmUse = async () => {
    if (!useTarget) return;
    try {
      const text = await renderPrompt(useTarget.id, useValues);
      setUseTarget(null);
      navigate(`/studio?draft=${encodeURIComponent(text)}`);
    } catch (e) {
      say(e instanceof ApiError ? e.message : t('Could not render the template'));
    }
  };

  const doExport = async () => {
    const ids = selected.size ? [...selected] : visible.map((v) => v.id);
    if (!ids.length) return;
    try {
      const rows = await exportPrompts(ids);
      const blob = new Blob([JSON.stringify({ templates: rows }, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'prompts-export.json';
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      say(e instanceof ApiError ? e.message : t('Could not export'));
    }
  };

  const doImport = async () => {
    try {
      const parsed = JSON.parse(importText) as { templates?: Record<string, unknown>[] };
      const result = await importPrompts(parsed.templates ?? []);
      setImportOpen(false);
      setImportText('');
      await reload();
      if (result.needsResolution.length) {
        say(t('Imported {n}. {m} need a project to attach to before they can be saved.', { n: result.imported.length, m: result.needsResolution.length }));
      } else {
        say(t('Imported {n} template(s)', { n: result.imported.length }));
      }
    } catch (e) {
      say(e instanceof Error ? e.message : t('The import file is not valid'));
    }
  };

  if (failed && !templates) {
    return (
      <div className="fs-screen" data-testid="prompts">
        <EmptyState tone="error" icon={Star} title={t('Could not read the prompt library')} body={t('Refresh to try again.')} primaryAction={{ label: t('Retry'), onClick: () => void reload() }} />
      </div>
    );
  }

  return (
    <div className="fs-screen" data-testid="prompts">
      <header className="fs-screen__head">
        <div>
          <h1 className="fs-screen__title">{t('Prompts')}</h1>
          <p className="fs-prose">{t('Saved prompts and templates. Using one pastes it into a chat — nothing here sends by itself.')}</p>
        </div>
        <div className="fs-screen__actions">
          <Button variant="secondary" size="sm" icon={Upload} label={t('Import')} onClick={() => setImportOpen(true)} />
          <Button variant="secondary" size="sm" icon={Download} label={t('Export')} onClick={() => void doExport()} />
          <Button variant="primary" size="sm" icon={Plus} label={t('New template')} onClick={openCreate} testId="prompts-new" />
        </div>
      </header>

      <label className="fs-act__search">
        <Search size={13} aria-hidden="true" />
        <input type="search" placeholder={t('Search by name, tag or intent…')} value={query} onChange={(e) => setQuery(e.target.value)} aria-label={t('Search')} data-testid="prompts-search" />
      </label>

      {!templates && <Skeleton label={t('Loading templates')} count={4} height="64px" />}

      {templates && visible.length === 0 && (
        <EmptyState icon={Star} title={t('No templates yet')} body={t('Save a message from Studio as a template, or create one here.')} primaryAction={{ label: t('New template'), onClick: openCreate }} />
      )}

      {templates && visible.length > 0 && (
        <ul className="fs-list" data-testid="prompts-list">
          {visible.map((tpl) => (
            <li key={tpl.id} className="fs-run" data-testid="prompts-row">
              <label className="fs-studio__context-chip" style={{ marginInlineEnd: 8 }}>
                <input type="checkbox" checked={selected.has(tpl.id)} onChange={(e) => {
                  const next = new Set(selected);
                  if (e.target.checked) next.add(tpl.id); else next.delete(tpl.id);
                  setSelected(next);
                }} aria-label={t('Select {name} for export', { name: tpl.name })} />
              </label>
              <span className="fs-run__main">
                <span className="fs-row__name">{tpl.name} <span className="fs-act__hint">v{tpl.version}</span></span>
                {tpl.description && <span className="fs-run__detail">{tpl.description}</span>}
                <span className="fs-row__meta">{tpl.scope === 'project' ? t('Project') : t('Personal')} · {tpl.variables.length ? tn(tpl.variables.length) : t('No variables')}</span>
              </span>
              <IconButton icon={favIcon(tpl.favorite)} label={tpl.favorite ? t('Unfavorite') : t('Favorite')} onClick={() => void favorite(tpl)} testId="prompts-favorite" />
              <Button size="sm" label={t('Use')} onClick={() => openUse(tpl)} testId="prompts-use" />
              <IconButton icon={Pencil} label={t('Edit')} onClick={() => openEdit(tpl)} />
              <IconButton icon={Trash2} label={t('Delete')} onClick={() => void remove(tpl)} testId="prompts-delete" />
            </li>
          ))}
        </ul>
      )}

      <Dialog open={editorOpen} onOpenChange={setEditorOpen} title={editing ? t('Edit template') : t('New template')} testId="prompts-editor"
        footer={<><Button variant="secondary" label={t('Cancel')} onClick={() => setEditorOpen(false)} /><Button variant="primary" label={t('Save')} loading={saving} disabled={!draft.name.trim() || !draft.body.trim()} onClick={() => void save()} testId="prompts-save" /></>}>
        <label className="fs-act__field"><span>{t('Name')}</span><input className="fs-field" value={draft.name} onChange={(e) => setDraft((d) => ({ ...d, name: e.target.value }))} /></label>
        <label className="fs-act__field"><span>{t('Description')}</span><input className="fs-field" value={draft.description ?? ''} onChange={(e) => setDraft((d) => ({ ...d, description: e.target.value }))} /></label>
        <label className="fs-act__field"><span>{t('Body — use {{variable}} for each placeholder')}</span>
          <textarea className="fs-field" rows={6} value={draft.body} onChange={(e) => setDraft((d) => ({ ...d, body: e.target.value }))} data-testid="prompts-body" /></label>
        <div>
          <p className="fs-act__hint">{t('Variables')}</p>
          {(draft.variables ?? []).map((v, i) => (
            <div key={i} className="fs-act__actions">
              <input className="fs-field" style={{ maxWidth: 160 }} value={v.name} placeholder={t('name')}
                onChange={(e) => setDraft((d) => ({ ...d, variables: (d.variables ?? []).map((x, xi) => xi === i ? { ...x, name: e.target.value } : x) }))} />
              <select value={v.type} onChange={(e) => setDraft((d) => ({ ...d, variables: (d.variables ?? []).map((x, xi) => xi === i ? { ...x, type: e.target.value as PromptVariable['type'] } : x) }))}>
                <option value="string">{t('Text')}</option>
                <option value="number">{t('Number')}</option>
                <option value="boolean">{t('Yes/No')}</option>
                <option value="choice">{t('Choice')}</option>
              </select>
              <label><input type="checkbox" checked={v.required} onChange={(e) => setDraft((d) => ({ ...d, variables: (d.variables ?? []).map((x, xi) => xi === i ? { ...x, required: e.target.checked } : x) }))} /> {t('Required')}</label>
              <IconButton icon={Trash2} label={t('Remove variable')} size="sm" onClick={() => setDraft((d) => ({ ...d, variables: (d.variables ?? []).filter((_, xi) => xi !== i) }))} />
            </div>
          ))}
          <Button size="sm" variant="ghost" icon={Plus} label={t('Add variable')} onClick={() => setDraft((d) => ({ ...d, variables: [...(d.variables ?? []), { ...EMPTY_VAR }] }))} testId="prompts-add-variable" />
        </div>
      </Dialog>

      <Dialog open={!!useTarget} onOpenChange={(open) => !open && setUseTarget(null)} title={useTarget ? t('Use "{name}"', { name: useTarget.name }) : ''} testId="prompts-use-dialog"
        footer={<><Button variant="secondary" label={t('Cancel')} onClick={() => setUseTarget(null)} /><Button variant="primary" label={t('Paste into Studio')} onClick={() => void confirmUse()} testId="prompts-use-confirm" /></>}>
        {useTarget?.variables.map((v) => (
          <label key={v.name} className="fs-act__field"><span>{v.name}{v.required && ' *'}</span>
            <input className="fs-field" value={useValues[v.name] ?? ''} onChange={(e) => setUseValues((cur) => ({ ...cur, [v.name]: e.target.value }))} /></label>
        ))}
        {useTarget && <pre className="fs-act__pre">{previewRender(useTarget.body, useValues)}</pre>}
      </Dialog>

      <Dialog open={importOpen} onOpenChange={setImportOpen} title={t('Import templates')} testId="prompts-import-dialog"
        footer={<><Button variant="secondary" label={t('Cancel')} onClick={() => setImportOpen(false)} /><Button variant="primary" icon={Copy} label={t('Import')} onClick={() => void doImport()} testId="prompts-import-confirm" /></>}>
        <p className="fs-prose">{t('Paste an exported JSON file. A template tied to a project on another machine will ask you to pick the matching project here rather than write to the old one.')}</p>
        <textarea className="fs-field" rows={8} value={importText} onChange={(e) => setImportText(e.target.value)} data-testid="prompts-import-text" />
      </Dialog>

      {notice && <Toast>{notice}</Toast>}
    </div>
  );
}

function favIcon(on: boolean) {
  return on ? Heart : Star;
}

function tn(n: number): string {
  return n === 1 ? t('1 variable') : t('{n} variables', { n });
}
