import { CornerLeftUp, Folder, FolderOpen, FolderPlus, HardDrive, Home, Plus, Trash2 } from 'lucide-react';
import { useEffect, useState } from 'react';
import {
  addTask, createChannel, createServerSpace, hubFolders, hubIsRemote, openThread, setSpaceMeta, updateSpace,
  type AgoraGuild, type AgoraServer, type AgoraThreadInfo, type HubFolders,
} from '../../adapters/agora';
import { pickNative } from '../../adapters/composer';
import { addContextRoot, createProject, listProjects, recentFolders, type Project, type RecentFolder } from '../../adapters/projects';
import { Button, Dialog, IconButton } from '../../components';
import WorkspaceDialog from '../studio/WorkspaceDialog';
import { t } from '../../i18n';
import { isOnline, ROLE_COLORS } from './util';

type Notify = (text: string, tone?: 'ok' | 'warn') => void;

/** True while the hub, and the agents with it, is on another machine than this Faustus (it runs on a Spark). */
function useHubRemote(open: boolean): boolean {
  const [remote, setRemote] = useState(false);
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    void hubIsRemote(controller.signal).then((value) => { if (!controller.signal.aborted) setRemote(value); });
    return () => controller.abort();
  }, [open]);
  return remote;
}

/** Choose a folder of the hub's machine, one level at a time: where the agents work when Faustus runs elsewhere. */
function HubFolderDialog({ open, initial, onClose, onPick }: { open: boolean; initial: string; onClose: () => void; onPick: (path: string) => void }) {
  const [listing, setListing] = useState<HubFolders | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const go = (path: string) => {
    setBusy(true); setError('');
    hubFolders(path).then(setListing).catch((e: Error) => setError(e.message)).finally(() => setBusy(false));
  };
  useEffect(() => {
    if (!open) return;
    setListing(null); setError(''); setBusy(true);
    hubFolders(initial).catch(() => hubFolders('')).then(setListing).catch((e: Error) => setError(e.message)).finally(() => setBusy(false));
  }, [open, initial]);
  const atDrives = !listing?.path;
  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) onClose(); }} title={t('Choose a folder on the PC')} testId="agora-hub-folders" className="fs-agx-dialog"
      description={t('The agents work on the PC, so the folder is chosen there.')}
      footer={<><Button label={t('Cancel')} onClick={onClose} />
        <Button variant="primary" icon={FolderOpen} label={t('Choose this folder')} disabled={atDrives || busy} onClick={() => { if (listing?.path) { onPick(listing.path); onClose(); } }} testId="agora-hub-folders-pick" /></>}>
      <div className="fs-agx-fb">
        <div className="fs-agx-fb__bar">
          <IconButton size="sm" icon={CornerLeftUp} label={t('Up one level')} disabled={!listing || listing.parent === null || busy} onClick={() => go(listing?.parent ?? '')} />
          <IconButton size="sm" icon={HardDrive} label={t('Drives')} disabled={busy} onClick={() => go('')} />
          <IconButton size="sm" icon={Home} label={t('Home folder')} disabled={!listing?.home || busy} onClick={() => go(listing?.home ?? '')} />
          <code className="fs-agx-fb__path" data-testid="agora-hub-folders-path">{listing?.path || t('Drives')}</code>
        </div>
        {error && <p className="fs-notice" data-tone="danger" role="alert">{error}</p>}
        <ul className="fs-agx-fb__list" aria-busy={busy}>
          {(listing?.folders ?? []).map((folder) => (
            <li key={folder.path}><button type="button" className="fs-agx-fb__row" onClick={() => go(folder.path)}>
              {atDrives ? <HardDrive size={15} aria-hidden="true" /> : <Folder size={15} aria-hidden="true" />}<span>{folder.name}</span></button></li>
          ))}
          {listing && !listing.folders.length && !error && <li className="fs-muted fs-agx-fb__empty">{t('No folders inside.')}</li>}
        </ul>
        {listing?.truncated && <p className="fs-muted">{t('Showing the first 2000 folders.')}</p>}
      </div>
    </Dialog>
  );
}

/** One folder field or a list of them, chosen with the system dialog (the in-page browser when that cannot open). */
function FolderPicker({ label, hint, value, onChange, multiple = false, recent = [], remote = false }: {
  label: string; hint?: string; value: string[]; onChange: (next: string[]) => void; multiple?: boolean; recent?: RecentFolder[]; remote?: boolean;
}) {
  const [browse, setBrowse] = useState(false);
  const [hubBrowse, setHubBrowse] = useState(false);
  const [typed, setTyped] = useState('');
  const [error, setError] = useState('');
  const add = (path: string) => {
    const clean = path.trim();
    if (!clean) return;
    onChange(multiple ? [...value.filter((v) => v !== clean), clean] : [clean]);
    setTyped('');
  };
  const pick = async () => {
    setError('');
    if (remote) { setHubBrowse(true); return; }
    try {
      const result = await pickNative('folder', value[value.length - 1] || '');
      if (result.status === 'ok' && result.path) add(result.path);
      else if (result.status === 'unavailable') setBrowse(true);
      else if (result.detail) setError(result.detail);
    } catch (e) { setError((e as Error).message); }
  };
  const offers = remote ? [] : recent.filter((f) => !value.includes(f.path)).slice(0, 5);
  return (
    <div className="fs-agx-form__field">
      <span className="fs-agx-form__label">{label}</span>
      {hint && <span className="fs-muted">{hint}</span>}
      {value.length > 0 && (
        <ul className="fs-agx-folders">
          {value.map((path) => (
            <li key={path}><FolderOpen size={14} aria-hidden="true" /><code>{path}</code>
              <IconButton size="sm" icon={Trash2} label={t('Remove {path}', { path })} onClick={() => onChange(value.filter((v) => v !== path))} /></li>
          ))}
        </ul>
      )}
      {(multiple || !value.length) && (
        <div className="fs-agx-form__row">
          <input className="fs-field" value={typed} placeholder={t('Paste a path or choose a folder')} aria-label={label}
            onChange={(e) => setTyped(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); add(typed); } }} />
          {typed.trim() && <Button size="sm" variant="secondary" label={t('Add')} onClick={() => add(typed)} />}
          <Button size="sm" variant="secondary" icon={FolderPlus} label={t('Choose a folder…')} onClick={() => void pick()} testId="agora-pick-folder" />
        </div>
      )}
      {offers.length > 0 && (multiple || !value.length) && (
        <div className="fs-agx-form__chips">
          {offers.map((f) => <button key={f.path} type="button" className="fs-chip" title={f.path} onClick={() => add(f.path)}>{f.projectName || f.path.split(/[\\/]/).pop()}</button>)}
        </div>
      )}
      {error && <p className="fs-notice" data-tone="danger">{error}</p>}
      <WorkspaceDialog open={browse} initial={value[value.length - 1] || ''} onClose={() => setBrowse(false)} onPick={(path) => add(path)} />
      <HubFolderDialog open={hubBrowse} initial={value[value.length - 1] || ''} onClose={() => setHubBrowse(false)} onPick={(path) => add(path)} />
    </div>
  );
}

function Look({ icon, color, onIcon, onColor }: { icon: string; color: string; onIcon: (v: string) => void; onColor: (v: string) => void }) {
  return (
    <div className="fs-agx-form__row">
      <label className="fs-agx-form__field"><span className="fs-agx-form__label">{t('Letter or emoji')}</span>
        <input className="fs-field fs-agx-form__glyph" value={icon} maxLength={4} placeholder="A" onChange={(e) => onIcon(e.target.value)} /></label>
      <label className="fs-agx-form__field"><span className="fs-agx-form__label">{t('Colour')}</span>
        <input type="color" className="fs-agx-form__color" value={color || ROLE_COLORS.person} onChange={(e) => onColor(e.target.value)} /></label>
      {color && <Button size="sm" variant="ghost" label={t('No colour')} onClick={() => onColor('')} />}
    </div>
  );
}

/** Add a server: a new project with its working folder, its context folders and its agora. */
export function AddServerDialog({ open, servers, onClose, onCreated, notify }: {
  open: boolean; servers: AgoraServer[]; onClose: () => void; onCreated: (spaceId: string) => void; notify: Notify;
}) {
  const [name, setName] = useState('');
  const [workspace, setWorkspace] = useState<string[]>([]);
  const [folders, setFolders] = useState<string[]>([]);
  const [about, setAbout] = useState('');
  const [instructions, setInstructions] = useState('');
  const [icon, setIcon] = useState('');
  const [color, setColor] = useState('');
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [projects, setProjects] = useState<Project[]>([]);
  const [recent, setRecent] = useState<RecentFolder[]>([]);
  const remote = useHubRemote(open);
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setName(''); setWorkspace([]); setFolders([]); setAbout(''); setInstructions(''); setIcon(''); setColor(''); setError(''); setBusy('');
    listProjects(controller.signal).then(setProjects).catch(() => undefined);
    recentFolders(controller.signal).then(setRecent).catch(() => undefined);
    return () => controller.abort();
  }, [open]);
  const bound = new Set(servers.map((s) => s.project_id).filter(Boolean));
  const orphans = projects.filter((p) => !p.archived && !bound.has(p.id)).slice(0, 12);

  const create = async () => {
    const title = name.trim();
    if (!title) { setError(t('Give the server a name.')); return; }
    setBusy('new'); setError('');
    try {
      // With the hub on another machine the folders are the agents' (on the PC): the server keeps them, and the project
      // here gets none, because those paths do not exist where Faustus runs.
      const project = await createProject({ name: title, folder: title, workspace: remote ? '' : workspace[0] ?? '', instructions: instructions.trim() });
      for (const folder of remote ? [] : folders) {
        try { await addContextRoot(project.id, folder); } catch (e) { notify(t('{path}: {reason}', { path: folder, reason: (e as Error).message }), 'warn'); }
      }
      const space = await createServerSpace(title, { project_id: project.id, workspace: workspace[0] ?? '',
        description: about.trim() || t('Work in {name}: rooms, tasks, reviews and decisions of this project.', { name: title }) });
      if (folders.length || icon.trim() || color) await setSpaceMeta(space.id, { folders, icon: icon.trim(), color });
      notify(t('Server {name} created', { name: title }));
      onCreated(space.id); onClose();
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(''); }
  };
  const adopt = async (project: Project) => {
    setBusy(project.id); setError('');
    try {
      const space = await createServerSpace(project.name, { project_id: project.id, workspace: remote ? '' : project.workspace || '',
        description: t('Work in {name}: rooms, tasks, reviews and decisions of this project.', { name: project.name }) });
      notify(t('Server {name} created', { name: project.name }));
      onCreated(space.id); onClose();
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(''); }
  };

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) onClose(); }} title={t('Add a server')} testId="agora-add-server-dialog" className="fs-agx-dialog"
      description={t('A server is a project with its agora: its channels, its task board, its rooms of models and the agents that work in it.')}
      footer={<><Button label={t('Cancel')} onClick={onClose} /><Button variant="primary" icon={Plus} label={t('Create the server')} loading={busy === 'new'} disabled={!name.trim()} onClick={() => void create()} testId="agora-create-server" /></>}>
      <div className="fs-agx-form">
        <label className="fs-agx-form__field"><span className="fs-agx-form__label">{t('Name')}</span>
          <input className="fs-field" autoFocus value={name} maxLength={120} onChange={(e) => setName(e.target.value)} placeholder={t('The project this server is about')} data-testid="agora-server-name" /></label>
        <FolderPicker label={t('Working folder')} hint={remote ? t('Where its agents read and write, on the PC.') : t('Where its agents read and write.')} value={workspace} onChange={setWorkspace} recent={recent} remote={remote} />
        <FolderPicker label={t('Context folders')} hint={t('Other folders its agents should know: documents, references, a sibling repository.')} value={folders} onChange={setFolders} multiple recent={recent} remote={remote} />
        <label className="fs-agx-form__field"><span className="fs-agx-form__label">{t('What it is about')}</span>
          <textarea className="fs-field fs-agx-form__area" value={about} maxLength={2000} onChange={(e) => setAbout(e.target.value)} /></label>
        <label className="fs-agx-form__field"><span className="fs-agx-form__label">{t('Instructions for its chats')}</span>
          <textarea className="fs-field fs-agx-form__area" value={instructions} maxLength={20000} onChange={(e) => setInstructions(e.target.value)} placeholder={t('Optional: how to work in this project')} /></label>
        <Look icon={icon} color={color} onIcon={setIcon} onColor={setColor} />
        {error && <p className="fs-notice" data-tone="danger" role="alert">{error}</p>}
        {orphans.length > 0 && (
          <section className="fs-agx-form__adopt">
            <h4>{t('Projects without a server yet')}</h4>
            <ul>
              {orphans.map((project) => (
                <li key={project.id}><span><strong>{project.name}</strong>{project.workspace ? <code>{project.workspace}</code> : null}</span>
                  <Button size="sm" variant="secondary" label={t('Add as a server')} loading={busy === project.id} onClick={() => void adopt(project)} /></li>
              ))}
            </ul>
          </section>
        )}
      </div>
    </Dialog>
  );
}

/** Rename a server, say what it is about, change its picture and the folders it reads. */
export function EditServerDialog({ open, guild, focus, onClose, onSaved, notify }: {
  open: boolean; guild: AgoraGuild; focus?: 'folders'; onClose: () => void; onSaved: () => void; notify: Notify;
}) {
  const space = guild.space;
  const [title, setTitle] = useState(space.title);
  const [about, setAbout] = useState(space.description);
  const [workspace, setWorkspace] = useState<string[]>(space.workspace ? [space.workspace] : []);
  const [folders, setFolders] = useState<string[]>(space.meta.folders);
  const [icon, setIcon] = useState(space.meta.icon);
  const [color, setColor] = useState(space.meta.color);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const remote = useHubRemote(open);
  useEffect(() => {
    if (!open) return;
    setTitle(space.title); setAbout(space.description); setWorkspace(space.workspace ? [space.workspace] : []);
    setFolders(space.meta.folders); setIcon(space.meta.icon); setColor(space.meta.color); setError('');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, space.id]);
  const save = async () => {
    setBusy(true); setError('');
    try {
      const changes: Record<string, unknown> = {};
      if (title.trim() && title.trim() !== space.title) changes.title = title.trim();
      if (about.trim() !== (space.description || '')) changes.description = about.trim();
      if ((workspace[0] ?? '') !== (space.workspace || '')) changes.workspace = workspace[0] ?? '';
      if (Object.keys(changes).length) await updateSpace(space.id, space.revision, changes);
      await setSpaceMeta(space.id, { folders, icon: icon.trim(), color });
      if (space.project_id && !remote) {
        for (const folder of folders.filter((f) => !space.meta.folders.includes(f))) {
          try { await addContextRoot(space.project_id, folder); } catch { /* the project may already read it */ }
        }
      }
      notify(t('Server saved')); onSaved(); onClose();
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };
  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) onClose(); }} title={focus === 'folders' ? t('Context folders') : t('Edit server')} testId="agora-edit-server-dialog" className="fs-agx-dialog"
      footer={<><Button label={t('Cancel')} onClick={onClose} /><Button variant="primary" label={t('Save')} loading={busy} onClick={() => void save()} testId="agora-save-server" /></>}>
      <div className="fs-agx-form">
        {focus !== 'folders' && (
          <>
            <label className="fs-agx-form__field"><span className="fs-agx-form__label">{t('Name')}</span>
              <input className="fs-field" value={title} maxLength={120} onChange={(e) => setTitle(e.target.value)} /></label>
            <label className="fs-agx-form__field"><span className="fs-agx-form__label">{t('What it is about')}</span>
              <textarea className="fs-field fs-agx-form__area" value={about} maxLength={4000} onChange={(e) => setAbout(e.target.value)} /></label>
            <Look icon={icon} color={color} onIcon={setIcon} onColor={setColor} />
          </>
        )}
        <FolderPicker label={t('Working folder')} hint={remote ? t('Where its agents read and write, on the PC.') : t('Where its agents read and write.')} value={workspace} onChange={setWorkspace} remote={remote} />
        <FolderPicker label={t('Context folders')} hint={t('Other folders its agents should know: documents, references, a sibling repository.')} value={folders} onChange={setFolders} multiple remote={remote} />
        {error && <p className="fs-notice" data-tone="danger" role="alert">{error}</p>}
      </div>
    </Dialog>
  );
}

export function NewChannelDialog({ open, guild, category, onClose, onCreated, notify }: {
  open: boolean; guild: AgoraGuild; category: string; onClose: () => void; onCreated: (slug: string) => void; notify: Notify;
}) {
  const [title, setTitle] = useState('');
  const [topic, setTopic] = useState('');
  const [group, setGroup] = useState(category || 'chat');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => { if (open) { setTitle(''); setTopic(''); setGroup(category || 'chat'); setError(''); } }, [open, category]);
  const create = async () => {
    if (!title.trim()) return;
    setBusy(true); setError('');
    try {
      const made = await createChannel(guild.space.id, { title: title.trim(), topic: topic.trim(), category: group.trim() || 'chat' });
      notify(t('Channel #{name} created', { name: made.channel.slug })); onCreated(made.channel.slug); onClose();
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };
  const known = [...new Set(guild.channels.map((c) => c.category || 'chat'))];
  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) onClose(); }} title={t('New channel')} testId="agora-new-channel-dialog" className="fs-agx-dialog"
      description={t('A named conversation of this server. Agents write in it with `agora say <channel> "…"` and are mentioned with @id.')}
      footer={<><Button label={t('Cancel')} onClick={onClose} /><Button variant="primary" label={t('Create the channel')} loading={busy} disabled={!title.trim()} onClick={() => void create()} testId="agora-create-channel" /></>}>
      <form className="fs-agx-form" onSubmit={(e) => { e.preventDefault(); void create(); }}>
        <label className="fs-agx-form__field"><span className="fs-agx-form__label">{t('Name')}</span>
          <input className="fs-field" autoFocus value={title} maxLength={80} onChange={(e) => setTitle(e.target.value)} placeholder={t('ideas, design, bugs…')} data-testid="agora-channel-name" /></label>
        <label className="fs-agx-form__field"><span className="fs-agx-form__label">{t('Topic')}</span>
          <input className="fs-field" value={topic} maxLength={300} onChange={(e) => setTopic(e.target.value)} placeholder={t('What is talked about here')} /></label>
        <label className="fs-agx-form__field"><span className="fs-agx-form__label">{t('Category')}</span>
          <input className="fs-field" value={group} maxLength={40} list="agora-categories" onChange={(e) => setGroup(e.target.value)} />
          <datalist id="agora-categories">{known.map((c) => <option key={c} value={c} />)}</datalist></label>
        {error && <p className="fs-notice" data-tone="danger" role="alert">{error}</p>}
      </form>
    </Dialog>
  );
}

const THREAD_KINDS = [['debate', 'Debate'], ['question', 'Question'], ['decision', 'Decision'], ['note', 'Note'], ['handoff', 'Handoff']] as const;

export function NewThreadDialog({ open, guild, onClose, onCreated, notify }: {
  open: boolean; guild: AgoraGuild; onClose: () => void; onCreated: (thread: AgoraThreadInfo) => void; notify: Notify;
}) {
  const [title, setTitle] = useState('');
  const [body, setBody] = useState('');
  const [kind, setKind] = useState<string>('debate');
  const [mentions, setMentions] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => { if (open) { setTitle(''); setBody(''); setKind('debate'); setMentions([]); setError(''); } }, [open]);
  const around = guild.members.filter((m) => m.role !== 'person' && isOnline(m)).slice(0, 16);
  const create = async () => {
    if (!title.trim() || !body.trim()) return;
    setBusy(true); setError('');
    try {
      const made = await openThread(guild.space.id, { title: title.trim(), body: body.trim(), kind, mentions });
      notify(t('Thread opened')); onCreated(made.thread); onClose();
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };
  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) onClose(); }} title={t('New thread')} testId="agora-new-thread-dialog" className="fs-agx-dialog"
      description={t('A debate, a question or a note with its own conversation. Closing it with a decision puts it in the decision log.')}
      footer={<><Button label={t('Cancel')} onClick={onClose} /><Button variant="primary" label={t('Open the thread')} loading={busy} disabled={!title.trim() || !body.trim()} onClick={() => void create()} testId="agora-create-thread" /></>}>
      <div className="fs-agx-form">
        <label className="fs-agx-form__field"><span className="fs-agx-form__label">{t('Title')}</span>
          <input className="fs-field" autoFocus value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} data-testid="agora-thread-title" /></label>
        <div className="fs-seg" role="radiogroup" aria-label={t('Kind of thread')}>
          {THREAD_KINDS.map(([id, label]) => <button key={id} type="button" role="radio" aria-checked={kind === id} onClick={() => setKind(id)}>{t(label)}</button>)}
        </div>
        <label className="fs-agx-form__field"><span className="fs-agx-form__label">{t('First message')}</span>
          <textarea className="fs-field fs-agx-form__area" value={body} maxLength={20000} onChange={(e) => setBody(e.target.value)} data-testid="agora-thread-body" /></label>
        {around.length > 0 && (
          <div className="fs-agx-form__field"><span className="fs-agx-form__label">{t('Addressed to')}</span>
            <div className="fs-agx-form__chips">
              {around.map((m) => <button key={m.id} type="button" className="fs-chip" data-on={mentions.includes(m.id) || undefined}
                onClick={() => setMentions((old) => old.includes(m.id) ? old.filter((x) => x !== m.id) : [...old, m.id])}>@{m.id}</button>)}
            </div>
          </div>
        )}
        {error && <p className="fs-notice" data-tone="danger" role="alert">{error}</p>}
      </div>
    </Dialog>
  );
}

const TASK_KINDS = ['feature', 'bug', 'research', 'chore', 'docs', 'idea'];

export function NewTaskDialog({ open, guild, onClose, onCreated, notify }: {
  open: boolean; guild: AgoraGuild; onClose: () => void; onCreated: (threadId: number) => void; notify: Notify;
}) {
  const [title, setTitle] = useState('');
  const [body, setBody] = useState('');
  const [kind, setKind] = useState('feature');
  const [priority, setPriority] = useState(2);
  const [owner, setOwner] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => { if (open) { setTitle(''); setBody(''); setKind('feature'); setPriority(2); setOwner(''); setError(''); } }, [open]);
  const around = guild.members.filter((m) => m.role !== 'person' && isOnline(m)).slice(0, 30);
  const create = async () => {
    if (!title.trim()) return;
    setBusy(true); setError('');
    try {
      const made = await addTask(guild.space.id, { title: title.trim(), body: body.trim(), kind, priority, ...(owner ? { owner } : {}) });
      notify(t('Task #{id} created', { id: made.task.id })); onCreated(made.task.thread_id); onClose();
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };
  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) onClose(); }} title={t('New task')} testId="agora-new-task-dialog" className="fs-agx-dialog"
      description={t('It lands in To do with its own thread; an agent claims it, or you assign it now.')}
      footer={<><Button label={t('Cancel')} onClick={onClose} /><Button variant="primary" label={t('Create the task')} loading={busy} disabled={!title.trim()} onClick={() => void create()} testId="agora-create-task" /></>}>
      <div className="fs-agx-form">
        <label className="fs-agx-form__field"><span className="fs-agx-form__label">{t('Title')}</span>
          <input className="fs-field" autoFocus value={title} maxLength={300} onChange={(e) => setTitle(e.target.value)} data-testid="agora-task-title" /></label>
        <label className="fs-agx-form__field"><span className="fs-agx-form__label">{t('Details')}</span>
          <textarea className="fs-field fs-agx-form__area" value={body} maxLength={200000} onChange={(e) => setBody(e.target.value)} /></label>
        <div className="fs-agx-form__row">
          <label className="fs-agx-form__field"><span className="fs-agx-form__label">{t('Kind')}</span>
            <select className="fs-field" value={kind} onChange={(e) => setKind(e.target.value)}>{TASK_KINDS.map((k) => <option key={k} value={k}>{k}</option>)}</select></label>
          <label className="fs-agx-form__field"><span className="fs-agx-form__label">{t('Priority')}</span>
            <select className="fs-field" value={priority} onChange={(e) => setPriority(Number(e.target.value))}>
              <option value={0}>{t('P0 · urgent')}</option><option value={1}>{t('P1 · high')}</option><option value={2}>{t('P2 · normal')}</option><option value={3}>{t('P3 · someday')}</option></select></label>
          <label className="fs-agx-form__field"><span className="fs-agx-form__label">{t('Assign to')}</span>
            <select className="fs-field" value={owner} onChange={(e) => setOwner(e.target.value)}>
              <option value="">{t('Whoever claims it')}</option>{around.map((m) => <option key={m.id} value={m.id}>{m.id}</option>)}</select></label>
        </div>
        {error && <p className="fs-notice" data-tone="danger" role="alert">{error}</p>}
      </div>
    </Dialog>
  );
}
