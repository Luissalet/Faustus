import { useCallback, useEffect, useRef, useState, type DragEvent } from 'react';
import { Download, Plus, RefreshCw, Upload } from 'lucide-react';
import { Button, EmptyState, Skeleton } from '../../components';
import { reviewActivateSkill, skillFiles, workspaceAction, type CatalogSkill, type ProjectSkill, type RepoSkill } from '../../adapters/projectWorkspace';
import { ProjectSkillImports } from './SkillImports';
import { t } from '../../i18n';

const STARTER = '---\nname: my-skill\ndescription: Describe when to use this skill\nversion: "1.0.0"\n---\n\n## Procedure\n\n1. Describe a concrete step.\n\n## Verification\n\n- Describe how to check the result.\n';

export async function droppedFiles(transfer: DataTransfer): Promise<File[]> {
  const result: File[] = [];
  const walk = async (entry: FileSystemEntry, prefix = ''): Promise<void> => {
    if (result.length >= 64) throw new Error(t('Choose at most 64 skill files.'));
    if (entry.isFile) {
      const file = await new Promise<File>((resolve, reject) => (entry as FileSystemFileEntry).file(resolve, reject));
      Object.defineProperty(file, 'webkitRelativePath', { value: prefix + file.name });
      result.push(file);
    } else if (entry.isDirectory) {
      const reader = (entry as FileSystemDirectoryEntry).createReader();
      for (;;) {
        const children = await new Promise<FileSystemEntry[]>((resolve, reject) => reader.readEntries(resolve, reject));
        if (!children.length) break;
        for (const child of children) await walk(child, prefix + entry.name + '/');
      }
    }
  };
  const entries = Array.from(transfer.items).map((item) => item.webkitGetAsEntry?.()).filter((entry): entry is FileSystemEntry => !!entry);
  if (!entries.length) return Array.from(transfer.files);
  for (const entry of entries) await walk(entry);
  return result;
}

export function ProjectSkills({ projectId, hasWorkspace, say }: { projectId: string; hasWorkspace: boolean; say: (message: string) => void }) {
  const [skills, setSkills] = useState<ProjectSkill[] | null>(null);
  const [catalog, setCatalog] = useState<CatalogSkill[] | null>(null);
  const [query, setQuery] = useState('');
  const [url, setUrl] = useState('');
  const [repository, setRepository] = useState<RepoSkill[] | null>(null);
  const [mode, setMode] = useState<'installed' | 'library' | 'import' | 'manual'>('installed');
  const [source, setSource] = useState(STARTER);
  const [selected, setSelected] = useState<ProjectSkill | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [override, setOverride] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const fileInput = useRef<HTMLInputElement>(null);
  const folderInput = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    if (!hasWorkspace) return;
    const data = await workspaceAction<{ skills: ProjectSkill[] }>(projectId, 'skills_list');
    setSkills(data.skills);
  }, [projectId, hasWorkspace]);
  useEffect(() => { void load().catch((e: Error) => setError(e.message)); }, [load]);
  useEffect(() => {
    if (mode === 'library' && !catalog) void workspaceAction<{ skills: CatalogSkill[] }>(projectId, 'skills_catalog').then((d) => setCatalog(d.skills)).catch((e: Error) => setError(e.message));
  }, [mode, catalog, projectId]);

  const act = async (run: () => Promise<unknown>, message?: string) => {
    if (busy) return;
    setBusy(true); setError('');
    try { await run(); await load(); if (message) say(message); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };
  const installFiles = async (files: File[]) => {
    await act(async () => {
      const input = await skillFiles(files);
      await workspaceAction(projectId, 'skill_import', { files: input });
      setMode('installed');
    }, t('Skill installed. Review its content before activation.'));
  };
  const drop = (event: DragEvent) => {
    event.preventDefault();
    const transfer = event.dataTransfer;
    void droppedFiles(transfer).then(installFiles).catch((e: Error) => setError(e.message));
  };
  const matches = (name: string, description = '') => `${name} ${description}`.toLocaleLowerCase().includes(query.toLocaleLowerCase());

  if (!hasWorkspace) return <EmptyState title={t('Choose a working folder')} body={t('Project skills are installed inside its working folder. Choose it in Settings, then add skills here.')} />;

  return <div className="fs-project-workspace" data-testid="project-skills">
    <div className="fs-project-workspace__toolbar" aria-label={t('Skill actions')}>
      <Button size="sm" variant={mode === 'installed' ? 'secondary' : 'ghost'} label={t('Installed in this project')} onClick={() => setMode('installed')} />
      <Button size="sm" variant={mode === 'library' ? 'secondary' : 'ghost'} icon={Download} label={t('Browse library')} onClick={() => setMode('library')} testId="project-skills-library" />
      <Button size="sm" variant={mode === 'import' ? 'secondary' : 'ghost'} icon={Upload} label={t('Import skill')} onClick={() => setMode('import')} testId="project-skills-import" />
      <Button size="sm" variant={mode === 'manual' ? 'secondary' : 'ghost'} icon={Plus} label={t('Create skill')} onClick={() => setMode('manual')} testId="project-skills-create" />
      <Button size="sm" variant="ghost" icon={RefreshCw} label={t('Refresh')} disabled={busy} onClick={() => void act(load)} />
    </div>
    {error && <p className="fs-notice" data-tone="danger" role="alert">{error}</p>}
    {(mode === 'installed' || mode === 'library') && <label className="fs-field-label">{t('Search skills…')}<input className="fs-field" type="search" value={query} onChange={(e) => setQuery(e.target.value)} data-testid="project-skills-search" /></label>}
    {mode === 'installed' && <section className="fs-panel">
      <h2>{t('Installed in this project')}</h2>
      <p className="fs-prose">{t('Installed files stay in the project. Activation adds the reviewed procedure to its chats; changed content needs review again.')}</p>
      {!skills ? <Skeleton label={t('Loading skills')} count={3} height="50px" /> : !skills.length ? <EmptyState title={t('No project skills yet')} body={t('Browse the library, import a skill folder or create a procedure.')} /> : <ul className="fs-project-workspace__list">
        {skills.filter((s) => matches(s.name, s.description)).map((skill) => <li key={skill.key}>
          <div><strong>{skill.name}</strong><p>{skill.description || skill.error}</p><small>{skill.active ? t('Active') : skill.changed ? t('Changed — review required') : t('Inactive')}</small></div>
          <Button size="sm" variant="secondary" label={t('Review content')} disabled={busy || !!skill.error} onClick={() => void act(async () => { const data = await workspaceAction<{skill: ProjectSkill}>(projectId, 'skill_read', {key: skill.key}); setSelected(data.skill); setEditing(null); setOverride(false); })} testId={`project-skill-review-${skill.name}`} />
          {skill.active && <Button size="sm" variant="ghost" label={t('Deactivate')} disabled={busy} onClick={() => void act(() => workspaceAction(projectId, 'skill_deactivate', { key: skill.key }))} />}
        </li>)}
        {!!skills.length && !skills.some((s) => matches(s.name, s.description)) && <li>{t('Nothing matches')}</li>}
      </ul>}
    </section>}
    {mode === 'library' && <section className="fs-panel">
      <h2>{t('Skill library')}</h2>
      {!catalog ? <Skeleton label={t('Loading skills')} count={3} height="48px" /> : <ul className="fs-project-workspace__list">
        {catalog.filter((s) => matches(s.name, s.description)).map((skill) => <li key={`${skill.kind}:${skill.id}`}>
          <div><strong>{skill.name}</strong><p>{skill.description}</p><small>{skill.kind === 'bundled' ? t('Bundled library') : t('Your saved skills')}</small></div>
          <Button size="sm" variant="secondary" label={t('Install in project')} disabled={busy} onClick={() => void act(async () => { await workspaceAction(projectId, 'skill_install', { kind: skill.kind, name: skill.id }); setMode('installed'); }, t('Skill installed. Review its content before activation.'))} />
        </li>)}
        {!catalog.some((s) => matches(s.name, s.description)) && <li>{t('Nothing matches')}</li>}
      </ul>}
    </section>}
    {mode === 'import' && <section className="fs-panel" onDragOver={(e) => e.preventDefault()} onDrop={drop} data-testid="project-skills-drop">
      <h2>{t('Import skill')}</h2>
      <p className="fs-prose">{t('Drop a skill folder or SKILL.md here. Supporting text files stay with the procedure. Importing does not execute scripts.')}</p>
      <div className="fs-project-workspace__toolbar">
        <Button size="sm" variant="secondary" label={t('Choose files')} disabled={busy} onClick={() => fileInput.current?.click()} />
        <Button size="sm" variant="secondary" label={t('Choose folder…')} disabled={busy} onClick={() => folderInput.current?.click()} />
        <input ref={fileInput} hidden type="file" multiple onChange={(e) => { if (e.target.files) void installFiles(Array.from(e.target.files)); e.target.value = ''; }} data-testid="project-skills-files" />
        <input ref={folderInput} hidden type="file" multiple {...{ webkitdirectory: '' }} onChange={(e) => { if (e.target.files) void installFiles(Array.from(e.target.files)); e.target.value = ''; }} />
      </div>
      <form className="fs-project-workspace__form" onSubmit={(e) => { e.preventDefault(); void act(async () => { const data = await workspaceAction<{ skills: RepoSkill[] }>(projectId, 'skills_repository', { url }); setRepository(data.skills); }); }}>
        <label className="fs-field-label">{t('GitHub repository or skill URL')}<input className="fs-field" type="url" required value={url} onChange={(e) => { setUrl(e.target.value); setRepository(null); }} placeholder="https://github.com/owner/repository" data-testid="project-skills-url" /></label>
        <div className="fs-project-workspace__toolbar">
          <Button type="submit" size="sm" variant="secondary" label={t('Find skills in repository')} loading={busy} disabled={!url.trim()} />
          <Button size="sm" label={t('Import this skill URL')} disabled={busy || !url.trim()} onClick={() => void act(async () => { await workspaceAction(projectId, 'skill_import', { url }); setMode('installed'); }, t('Skill installed. Review its content before activation.'))} />
        </div>
      </form>
      {repository && <ul className="fs-project-workspace__list">{!repository.length && <li>{t('No SKILL.md found in this repository.')}</li>}{repository.map((skill) => <li key={skill.path}><div><strong>{skill.name}</strong><p>{skill.path}</p><small>{skill.revision.slice(0, 12)}</small></div><Button size="sm" label={t('Install')} disabled={busy} onClick={() => void act(async () => { await workspaceAction(projectId, 'skill_import', { url: skill.url }); setMode('installed'); })} /></li>)}</ul>}
    </section>}
    {mode === 'manual' && <form className="fs-panel fs-project-workspace__form" onSubmit={(e) => { e.preventDefault(); void act(async () => { await workspaceAction(projectId, 'skill_import', { content: source }); setMode('installed'); }, t('Skill created. Review its content before activation.')); }}>
      <h2>{t('Create skill')}</h2><label className="fs-field-label">SKILL.md<textarea className="fs-field fs-project-workspace__source" rows={16} value={source} onChange={(e) => setSource(e.target.value)} spellCheck={false} data-testid="project-skills-source" /></label>
      <Button type="submit" size="sm" label={t('Create skill')} loading={busy} disabled={!source.trim()} testId="project-skills-save" />
    </form>}
    {selected && <section className="fs-panel fs-project-workspace__form" aria-label={t('Review content')} data-testid="project-skill-review">
      <div className="fs-pj__card-head"><h2>{selected.name}</h2><Button size="sm" variant="ghost" label={t('Close')} onClick={() => setSelected(null)} /></div>
      <p>{selected.description}</p>
      {editing === null ? <><pre className="fs-context fs-project-workspace__source">{selected.markdown}</pre><Button size="sm" variant="secondary" label={t('Edit')} onClick={() => setEditing(selected.markdown || '')} /></> : <><textarea className="fs-field fs-project-workspace__source" rows={12} value={editing} onChange={(e) => setEditing(e.target.value)} aria-label="SKILL.md" /><div className="fs-project-workspace__toolbar"><Button size="sm" label={t('Save changes')} loading={busy} onClick={() => void act(async () => { const data = await workspaceAction<{skill: ProjectSkill}>(projectId, 'skill_write', {key: selected.key, digest: selected.digest, content: editing}); setSelected(data.skill); setEditing(null); })} /><Button size="sm" variant="ghost" label={t('Cancel')} onClick={() => setEditing(null)} /></div></>}
      {Object.entries(selected.files ?? {}).filter(([name]) => name !== 'SKILL.md').map(([name, text]) => <details key={name}><summary>{name}</summary><pre className="fs-context fs-project-workspace__source">{text}</pre></details>)}
      <p>{t('Required tools')}: {selected.tools?.join(', ') || t('None declared')}</p>
      {selected.review?.permissions && <details><summary>{t('Permissions')}</summary><pre className="fs-context fs-project-workspace__source">{JSON.stringify(selected.review.permissions, null, 2)}</pre></details>}
      {!!selected.review?.privilege_request.length && <p role="alert">{t('This skill requests changes to approval controls and cannot be activated as written.')}</p>}
      {!!selected.review?.security_scan?.findings.length && <ul>{selected.review.security_scan.findings.map((finding, i) => <li key={i}>{finding.severity} · {finding.file}:{finding.line} — {finding.description}</li>)}</ul>}
      {selected.review?.security_scan?.risk_level === 'critical' && <label><input type="checkbox" checked={override} onChange={(e) => setOverride(e.target.checked)} /> {t('I have reviewed the findings and want to approve anyway')}</label>}
      <Button size="sm" label={t('Review and activate')} loading={busy} disabled={editing !== null || !!selected.review?.privilege_request.length || (selected.review?.security_scan?.risk_level === 'critical' && !override)} onClick={() => void act(async () => { await reviewActivateSkill(projectId, selected, override); setSelected(null); }, t('Skill active in this project.'))} testId="project-skill-activate" />
    </section>}
    <details className="fs-panel fs-project-workspace__section"><summary>{t('Existing folder import reviews')}</summary><ProjectSkillImports projectId={projectId} say={say} /></details>
  </div>;
}
