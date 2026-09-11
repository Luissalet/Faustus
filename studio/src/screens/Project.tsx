import { Activity, AlertTriangle, Archive, ArchiveRestore, ArrowLeft, Brain, Check, Download, Eye, FileText, FolderOpen, FolderPlus, Image, Layers, Link2, Lock, MessageSquare, PencilLine, Pin, PinOff, Plus, RefreshCw, Send, Settings2, Target, Trash2, Unlink, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router';
import { ActivityDot, Button, Dialog, EmptyState, Menu, Skeleton, Toast } from '../components';
import { listModels, type ChatSession, type ModelRoute } from '../adapters/chat';
import { groupActivity, sessionActivity, useChatActivity } from '../shell/activity';
import { pickNative } from '../adapters/composer';
import { fitOf, fitSummary, useFitHints } from '../adapters/fit';
import { relativeTime } from '../adapters/home';
import {
  addContextRoot,
  AGENT_FLAGS,
  attachContextSource,
  chatsIn,
  countLinks,
  deleteProject,
  exportProjectUrl,
  flagOn,
  getContextPreview,
  getProject,
  groupLinksByRole,
  inspectContextLink,
  persistedLinkStatus,
  LINK_KINDS,
  LINK_ROLES,
  linkIsBehind,
  linkIsBroken,
  listContextLinks,
  patchContextLink,
  recentFolders,
  refreshContextLink,
  relocateProject,
  relocateResultMessage,
  removeChatFromProject,
  removeContextRoot,
  RETRIEVAL_POLICIES,
  shortRevision,
  startChatInProject,
  updateProject,
  type ContextLink,
  type ContextLinkPatch,
  type IndexStatus,
  type LinkKind,
  type LinkStatus,
  type Project,
  type RecentFolder,
  type RefreshReport,
  type RetrievalPolicy,
} from '../adapters/projects';
import { EXPORT_FORMATS } from '../adapters/sessions';
import { ProjectAudit } from './project/Audit';
import { ProjectMemoryFiles } from './project/Memory';
import { ProjectObjectives } from './project/Objectives';
import { ProjectSettings } from './project/Settings';
import './projects.css';
import './home.css';
import { t, tn } from '../i18n';

const TABS = [
  { id: 'brief', label: 'Brief', icon: FileText },
  { id: 'chats', label: 'Chats', icon: MessageSquare },
  { id: 'objetivos', label: 'Objectives', icon: Target },
  { id: 'memoria', label: 'Memory', icon: Brain },
  { id: 'actividad', label: 'Agent activity', icon: Activity },
  { id: 'contexto', label: 'Context', icon: Eye },
  { id: 'ajustes', label: 'Settings', icon: Settings2 },
] as const;

type TabId = (typeof TABS)[number]['id'];

const FORMAT_LABEL: Record<string, string> = { md: 'Markdown', txt: 'Plain text', json: 'JSON', html: 'HTML', pdf: 'PDF', docx: 'Word (.docx)' };

/* ── IDX-01: "The folder has moved…" ──
 *
 * Distinct from editing `workspace` in Settings (a plain field patch, which
 * never checks the path exists and never writes the destination's identity
 * marker): this calls the dedicated `POST /api/projects/{id}/relocate`
 * route, wired to `src.project_identity.relocate` — refuses an absent
 * path, and the folder itself corroborates the move afterwards. Identity,
 * memories and relations are keyed by `project_id`, untouched by this.
 *
 * `relocateProject`/`relocateResultMessage`/`recentFolders` live in
 * adapters/projects.ts now — Projects.tsx (the list) offers the same action
 * without opening a project first, and needs them too. Re-exported here so
 * `studio/checks/l43-project-relocate.check.mjs`, which bundles this file as
 * its entry point, still finds `relocateResultMessage` at this path.
 */
export { relocateResultMessage } from '../adapters/projects';

/* ── Context sources ──
 *
 * The list under a project stopped being "paths the agent may edit" and became
 * typed links: what the source is, what it is for, when it may enter a prompt,
 * which revision the project stands on, and — kept visibly apart from all of
 * that — whether the agent may write to it.
 *
 * `work_root` vs `read_only` is a permission, so it is not drawn as one more
 * grey badge among five. It gets its own icon and its own colour, because the
 * question a user needs answered at a glance is "can it change this?" and no
 * amount of correct text answers that if it looks like the other labels.
 */

const KIND_ICON: Record<LinkKind, typeof FileText> = {
  file: FileText,
  folder: FolderOpen,
  document: FileText,
  artifact: Layers,
  gallery_image: Image,
};

const KIND_LABEL: Record<LinkKind, string> = {
  file: 'File',
  folder: 'Folder',
  document: 'Document',
  artifact: 'Artifact',
  gallery_image: 'Image',
};

const POLICY_LABEL: Record<RetrievalPolicy, string> = {
  auto: 'Automatic',
  pinned_summary: 'Pinned summary',
  on_demand: 'On demand',
  disabled: 'Never',
};

const POLICY_HELP: Record<RetrievalPolicy, string> = {
  auto: 'Searched whenever the question calls for it.',
  pinned_summary: 'Its stored summary is searched; the full text is never injected.',
  on_demand: 'Listed to the agent, opened only when the agent decides to.',
  disabled: 'Never enters a prompt. It stays linked and stays readable by hand.',
};

const INDEX_LABEL: Record<IndexStatus, string> = {
  none: 'Not indexed',
  queued: 'Indexing queued',
  indexing: 'Indexing',
  ready: 'Indexed',
  stale: 'Index behind the source',
  failed: 'Indexing failed',
};

const ROLE_LABEL: Record<string, string> = {
  requirements: 'Requirements',
  reference: 'Reference',
  decision: 'Decision',
  style_reference: 'Style reference',
  example: 'Example',
  dataset: 'Dataset',
  specification: 'Specification',
  output: 'Output',
  // Not plain 'Archive': that key is already the verb on the project's own
  // archive button ("Archivar"), and a role is a noun.
  archive: 'Historical archive',
};

const roleName = (role: string) => (ROLE_LABEL[role] ? t(ROLE_LABEL[role]) : role);

/** What a refresh actually did, in one sentence the row can print. */
function describeRefresh(report: RefreshReport): string {
  if (!report.ok) return report.message || t('The source could not be read.');
  if (!report.changed) return t('Unchanged, still at {rev}.', { rev: shortRevision(report.revision) });
  return t('Changed: {from} became {to}. {index}.', {
    from: shortRevision(report.previousRevision),
    to: shortRevision(report.revision),
    index: t(INDEX_LABEL[report.indexStatus]),
  });
}

/** The state of one link, drawn once and read everywhere. */
function LinkBadges({ link, status }: { link: ContextLink; status?: LinkStatus }) {
  const effectiveStatus = status ?? persistedLinkStatus(link);
  const broken = linkIsBroken(effectiveStatus);
  const behind = linkIsBehind(link, effectiveStatus);
  return (
    <span className="fs-pj__badges">
      <span className="fs-pj__badge">{t(KIND_LABEL[link.kind])}</span>
      <span className="fs-pj__badge">{roleName(link.role)}</span>
      <span className="fs-pj__badge" title={t(POLICY_HELP[link.retrievalPolicy])}>
        {t(POLICY_LABEL[link.retrievalPolicy])}
      </span>
      <span
        className="fs-pj__badge"
        data-grant={link.accessMode === 'work_root' ? '' : undefined}
        title={link.accessMode === 'work_root' ? t('The agent can write to this source.') : t('The agent can read this source and cannot change it.')}
      >
        {link.accessMode === 'work_root' ? <PencilLine size={11} aria-hidden="true" /> : <Lock size={11} aria-hidden="true" />}
        {link.accessMode === 'work_root' ? t('Editable') : t('Read only')}
      </span>
      {!link.enabled && <span className="fs-pj__badge">{t('Off')}</span>}
      {broken && (
        <span className="fs-pj__badge" data-broken="">
          <AlertTriangle size={11} aria-hidden="true" />
          {effectiveStatus?.state === 'forbidden' ? t('Not available') : t('Source missing')}
        </span>
      )}
      {!broken && behind && <span className="fs-pj__badge" data-warn="">{t('Needs a refresh')}</span>}
      {!broken && !behind && <span className="fs-pj__badge">{t(INDEX_LABEL[link.indexStatus])}</span>}
    </span>
  );
}

interface SourcesProps {
  project: Project;
  links: ContextLink[] | null;
  statuses: Record<string, LinkStatus>;
  say: (message: string) => void;
  reload: () => Promise<void> | void;
}

/**
 * The manager: every link, grouped by what it is for, with the two knobs a
 * user actually turns (role and retrieval policy), a refresh that reports what
 * moved, and an unlink that says what it did not delete.
 */
function ProjectSources({ project, links, statuses, say, reload }: SourcesProps) {
  const [busy, setBusy] = useState<string | null>(null);
  const [reports, setReports] = useState<Record<string, string>>({});
  const [draft, setDraft] = useState<{ kind: LinkKind; locator: string; label: string; role: string; policy: RetrievalPolicy } | null>(null);

  const counts = useMemo(() => countLinks(links ?? [], statuses), [links, statuses]);
  const groups = useMemo(() => groupLinksByRole(links ?? []), [links]);

  const act = async (linkId: string, run: () => Promise<string>) => {
    setBusy(linkId);
    try {
      say(await run());
      await reload();
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const patch = (link: ContextLink, change: ContextLinkPatch, done: string) =>
    act(link.id, async () => {
      await patchContextLink(project.id, link.id, change);
      return done;
    });

  const refreshOne = (link: ContextLink) =>
    act(link.id, async () => {
      const report = await refreshContextLink(project.id, link.id);
      const sentence = describeRefresh(report);
      setReports((all) => ({ ...all, [link.id]: sentence }));
      return sentence;
    });

  const unlink = (link: ContextLink) =>
    act(link.id, async () => {
      await removeContextRoot(project.id, link.id);
      return t('Unlinked. The source itself was not deleted.');
    });

  const attach = async () => {
    if (!draft?.locator.trim()) return;
    const byPath = draft.kind === 'file' || draft.kind === 'folder';
    await act('new', async () => {
      const link = await attachContextSource(project.id, {
        kind: draft.kind,
        path: byPath ? draft.locator.trim() : '',
        id: byPath ? '' : draft.locator.trim(),
        label: draft.label.trim(),
        role: draft.role,
        retrievalPolicy: draft.policy,
      });
      setDraft(null);
      return t('Linked {label} as knowledge. It is read only.', { label: link.label });
    });
  };

  const pickPath = async () => {
    if (!draft) return;
    try {
      const pick = await pickNative(draft.kind === 'folder' ? 'folder' : 'file', project.workspace ?? '');
      if (pick.status === 'ok' && pick.path) setDraft({ ...draft, locator: pick.path });
    } catch (e) {
      say((e as Error).message);
    }
  };

  return (
    <div className="fs-pj__sources">
      <div className="fs-pj__card-head">
        <h3>{t('Context sources')}</h3>
        <Button
          variant="secondary"
          size="sm"
          icon={Plus}
          label={t('Link a source')}
          onClick={() => setDraft(draft ? null : { kind: 'file', locator: '', label: '', role: 'reference', policy: 'auto' })}
          testId="project-link-source"
        />
      </div>
      <p className="fs-prose">
        {t('Everything this project knows about, and how each piece is allowed to reach a chat. Linking a source is knowledge, not permission: only a work root can be written to.')}
      </p>
      {links !== null && (
        <p className="fs-pj__muted" data-testid="project-source-counts">
          {[
            tn(counts.total, '{n} source', '{n} sources'),
            counts.editable ? tn(counts.editable, '{n} work root', '{n} work roots') : '',
            counts.behind ? tn(counts.behind, '{n} needs a refresh', '{n} need a refresh') : '',
            counts.broken ? tn(counts.broken, '{n} broken link', '{n} broken links') : '',
            counts.off ? tn(counts.off, '{n} source switched off', '{n} sources switched off') : '',
          ]
            .filter(Boolean)
            .join(' · ')}
        </p>
      )}

      {draft && (
        <form
          className="fs-pj__attach"
          onSubmit={(e) => {
            e.preventDefault();
            void attach();
          }}
        >
          <select
            className="fs-field"
            value={draft.kind}
            onChange={(e) => setDraft({ ...draft, kind: e.target.value as LinkKind, locator: '' })}
            aria-label={t('Kind of source')}
          >
            {LINK_KINDS.map((kind) => (
              <option key={kind} value={kind}>
                {t(KIND_LABEL[kind])}
              </option>
            ))}
          </select>
          <input
            className="fs-field fs-pj__grow"
            value={draft.locator}
            onChange={(e) => setDraft({ ...draft, locator: e.target.value })}
            placeholder={draft.kind === 'file' || draft.kind === 'folder' ? t('Path of the file or folder') : t('Identifier of the document, artifact or image')}
            spellCheck={false}
            data-testid="project-source-locator"
          />
          {(draft.kind === 'file' || draft.kind === 'folder') && (
            <Button variant="ghost" size="sm" icon={FolderPlus} label={t('Browse')} onClick={() => void pickPath()} />
          )}
          <input
            className="fs-field"
            value={draft.label}
            onChange={(e) => setDraft({ ...draft, label: e.target.value })}
            placeholder={t('Label (optional)')}
            spellCheck={false}
          />
          <select className="fs-field" value={draft.role} onChange={(e) => setDraft({ ...draft, role: e.target.value })} aria-label={t('What it is for')}>
            {LINK_ROLES.map((role) => (
              <option key={role} value={role}>
                {roleName(role)}
              </option>
            ))}
          </select>
          <select
            className="fs-field"
            value={draft.policy}
            onChange={(e) => setDraft({ ...draft, policy: e.target.value as RetrievalPolicy })}
            aria-label={t('When it may be used')}
          >
            {RETRIEVAL_POLICIES.map((policy) => (
              <option key={policy} value={policy}>
                {t(POLICY_LABEL[policy])}
              </option>
            ))}
          </select>
          <Button type="submit" variant="secondary" size="sm" label={t('Link')} loading={busy === 'new'} disabled={!draft.locator.trim()} />
          <Button variant="ghost" size="sm" icon={X} label={t('Cancel')} onClick={() => setDraft(null)} />
        </form>
      )}

      {links === null ? (
        <Skeleton label={t('Loading the sources')} count={3} height="34px" />
      ) : links.length === 0 ? (
        <p className="fs-pj__muted">{t('Nothing linked yet. A linked document, folder or image becomes part of what the project knows, without being pasted into every chat.')}</p>
      ) : (
        groups.map((group) => (
          <section key={group.role} className="fs-pj__group">
            <h4 className="fs-panel__label">{roleName(group.role)}</h4>
            <ul className="fs-pj__links">
              {group.links.map((link) => {
                const Icon = KIND_ICON[link.kind];
                const status = statuses[link.id];
                return (
                  <li key={link.id} data-broken={linkIsBroken(status) || undefined} data-testid="project-source">
                    <div className="fs-pj__link-main">
                      <Icon size={13} aria-hidden="true" />
                      <span>
                        <strong>{link.label}</strong>
                        <small>
                          {link.path || link.refId}
                          {link.contentRevision ? ` · ${shortRevision(link.contentRevision)}` : ''}
                        </small>
                      </span>
                    </div>
                    <LinkBadges link={link} status={status} />
                    <div className="fs-pj__link-actions">
                      <select
                        className="fs-field"
                        value={link.role}
                        onChange={(e) => void patch(link, { role: e.target.value }, t('Role changed.'))}
                        aria-label={t('What it is for')}
                        disabled={busy === link.id}
                      >
                        {LINK_ROLES.map((role) => (
                          <option key={role} value={role}>
                            {roleName(role)}
                          </option>
                        ))}
                      </select>
                      <select
                        className="fs-field"
                        value={link.retrievalPolicy}
                        onChange={(e) => void patch(link, { retrievalPolicy: e.target.value as RetrievalPolicy }, t('Retrieval policy changed.'))}
                        aria-label={t('When it may be used')}
                        title={t(POLICY_HELP[link.retrievalPolicy])}
                        disabled={busy === link.id}
                      >
                        {RETRIEVAL_POLICIES.map((policy) => (
                          <option key={policy} value={policy}>
                            {t(POLICY_LABEL[policy])}
                          </option>
                        ))}
                      </select>
                      <Button
                        variant="ghost"
                        size="sm"
                        icon={RefreshCw}
                        label={t('Refresh')}
                        title={t('Read the source again and report what moved.')}
                        loading={busy === link.id}
                        onClick={() => void refreshOne(link)}
                      />
                      <Button
                        variant="ghost"
                        size="sm"
                        icon={Unlink}
                        label={t('Unlink')}
                        title={t('Remove the link. The source itself is never deleted.')}
                        onClick={() => void unlink(link)}
                      />
                    </div>
                    {(reports[link.id] || (linkIsBroken(status) && status?.message)) && (
                      <p className={linkIsBroken(status) ? 'fs-pj__error' : 'fs-pj__note'}>
                        {reports[link.id] || status?.message}
                      </p>
                    )}
                  </li>
                );
              })}
            </ul>
          </section>
        ))
      )}
    </div>
  );
}

/**
 * A project as a page (UI-040), and now the whole of it: start a chat
 * here, its chats, objectives, memory files, what the agent changed, the
 * context block the model receives, and the settings. `/projects/new`
 * is the same page with only the form.
 */
export function ProjectScreen() {
  const { projectId = '' } = useParams();
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const creating = projectId === 'new';
  const [project, setProject] = useState<Project | null>(null);
  const [failed, setFailed] = useState(false);
  const [chats, setChats] = useState<ChatSession[] | null>(null);
  const [context, setContext] = useState<string | null>(null);
  const [links, setLinks] = useState<ContextLink[] | null>(null);
  const [linkStates, setLinkStates] = useState<Record<string, LinkStatus>>({});
  const [routes, setRoutes] = useState<ModelRoute[]>([]);
  const [routeId, setRouteId] = useState('');
  const [prompt, setPrompt] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<'delete' | { chat: string } | null>(null);
  const [rootInput, setRootInput] = useState<string | null>(null);
  const [relocating, setRelocating] = useState(false);
  const [relocatePath, setRelocatePath] = useState('');
  const [relocateBusy, setRelocateBusy] = useState(false);
  const [relocateErr, setRelocateErr] = useState<string | null>(null);
  const [relocateRecent, setRelocateRecent] = useState<RecentFolder[] | null>(null);
  const [relocatePickUnavailable, setRelocatePickUnavailable] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const noticeTimer = useRef<number | null>(null);

  const say = useCallback((msg: string) => {
    setNotice(msg);
    if (noticeTimer.current) window.clearTimeout(noticeTimer.current);
    noticeTimer.current = window.setTimeout(() => setNotice(null), 2600);
  }, []);

  /**
   * The links, and then what each source says about itself.
   *
   * The second half is the reason this is not one request: `index_status`
   * is about the index, not about whether the file is still on disk, so a
   * link whose source has been deleted would render as a healthy row. It
   * has to LOOK broken rather than quietly keep working, so each link is
   * inspected — four at a time, because a project at the forty-link
   * ceiling should not open forty requests at once. A link that cannot be
   * inspected stays unchecked rather than being called broken.
   */
  const loadLinks = useCallback(async () => {
    let rows: ContextLink[] = [];
    try {
      rows = (await listContextLinks(projectId)).links;
    } catch {
      setLinks([]);
      return;
    }
    setLinks(rows);
    const queue = [...rows];
    const found: Record<string, LinkStatus> = {};
    const worker = async () => {
      for (let next = queue.shift(); next; next = queue.shift()) {
        try {
          found[next.id] = await inspectContextLink(projectId, next.id);
        } catch {
          /* unchecked is not broken */
        }
      }
    };
    await Promise.all([worker(), worker(), worker(), worker()]);
    setLinkStates(found);
  }, [projectId]);

  const rawTab = params.get('tab');
  const tab: TabId = creating ? 'ajustes' : TABS.some((x) => x.id === rawTab) ? (rawTab as TabId) : 'brief';

  /* A turn survives leaving the conversation, so this screen has to be able
     to say which of its chats is still working (or waiting for a permission
     nobody has given). One shared poll for the whole account. */
  const activity = useChatActivity();
  const liveChats = useMemo(
    () => (chats ?? []).filter((c) => sessionActivity(activity, c.id) !== null).length,
    [chats, activity],
  );
  /* One tone for the tab: a chat waiting for a permission outranks a chat
     that is merely busy — one of them needs a person. */
  const liveTone = useMemo(
    () => groupActivity(activity, (chats ?? []).map((c) => c.id)) ?? 'running',
    [chats, activity],
  );

  const reload = useCallback(async () => {
    if (creating) return;
    try {
      const p = await getProject(projectId);
      setProject(p);
      setFailed(false);
      void chatsIn(p).then(setChats).catch(() => setChats([]));
      void getContextPreview(projectId).then(setContext).catch(() => setContext(''));
      void loadLinks();
    } catch {
      setFailed(true);
    }
  }, [projectId, creating, loadLinks]);

  useEffect(() => {
    void reload();
  }, [reload]);

  useEffect(() => {
    void listModels()
      .then((list) => {
        setRoutes(list);
        let last = '';
        try {
          last = (JSON.parse(localStorage.getItem('faustus_studio_route') ?? '{}') as { id?: string }).id ?? '';
        } catch {
          /* private mode */
        }
        setRouteId((id) => id || (list.some((r) => r.id === last) ? last : (list[0]?.id ?? '')));
      })
      .catch(() => setRoutes([]));
  }, []);

  // Will the model for the new chat fit on the card? Read once, when the
  // Brief is on screen, from the same endpoint the picker uses.
  const fit = useFitHints(tab === 'brief');

  const setTab = (id: TabId) => {
    const next = new URLSearchParams(params);
    next.set('tab', id);
    setParams(next);
  };

  const flags = useMemo(() => (project ? AGENT_FLAGS.map((f) => ({ ...f, on: flagOn(project, f.key) })) : []), [project]);

  const doRelocate = async () => {
    if (!project || !relocatePath.trim()) return;
    setRelocateBusy(true);
    setRelocateErr(null);
    try {
      const result = await relocateProject(project.id, relocatePath.trim());
      setProject(result.project);
      say(relocateResultMessage(result));
      setRelocating(false);
      setRelocatePath('');
    } catch (e) {
      setRelocateErr((e as Error).message);
    } finally {
      setRelocateBusy(false);
    }
  };

  /** IDX-01: the real OS folder dialog when the browser runs on the same
   *  machine as the server; `unavailable` (a remote browser, no display)
   *  falls back to the recent-folders list and the plain text field, both
   *  already shown alongside it. */
  const browseRelocate = async () => {
    try {
      const pick = await pickNative('folder', relocatePath || project?.workspace || '');
      if (pick.status === 'ok' && pick.path) setRelocatePath(pick.path);
      else if (pick.status === 'unavailable') setRelocatePickUnavailable(true);
    } catch (e) {
      setRelocateErr((e as Error).message);
    }
  };

  const start = async () => {
    if (!project) return;
    setBusy('start');
    try {
      const sid = await startChatInProject(project, routes.find((r) => r.id === routeId) ?? null, prompt);
      const q = new URLSearchParams({ s: sid });
      if (prompt.trim()) {
        q.set('draft', prompt.trim());
        q.set('send', '1');
      }
      navigate(`/studio?${q}`);
    } catch (e) {
      say((e as Error).message);
      setBusy(null);
    }
  };

  const patch = async (key: string, input: Parameters<typeof updateProject>[1], done: string) => {
    if (!project) return;
    setBusy(key);
    try {
      setProject(await updateProject(project.id, input));
      say(done);
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const addRootPath = async (path: string) => {
    if (!project || !path.trim()) return;
    try {
      await addContextRoot(project.id, path.trim());
      setRootInput(null);
      await reload();
      say(t('Added'));
    } catch (e) {
      say((e as Error).message);
    }
  };

  const addRoot = async () => {
    if (!project) return;
    try {
      const pick = await pickNative('folder', project.workspace ?? '');
      if (pick.status === 'ok' && pick.path) await addRootPath(pick.path);
      // No system dialog (remote browser): type the path instead.
      else if (pick.status === 'unavailable') setRootInput('');
    } catch (e) {
      say((e as Error).message);
    }
  };

  if (creating) {
    return (
      <div className="fs-screen fs-pj" data-testid="project-new">
        <div>
          <Link to="/projects" className="fs-tab" style={{ paddingInline: 0 }}>
            <ArrowLeft size={14} aria-hidden="true" /> {t('Projects')}
          </Link>
        </div>
        <header className="fs-screen__head">
          <div>
            <h1 className="fs-screen__title">{t('New project')}</h1>
            <p className="fs-prose fs-pj__lede">{t('A project groups a folder, instructions, memory and conversations. The agent follows its manners inside that folder.')}</p>
          </div>
        </header>
        <div className="fs-panel">
          <ProjectSettings project={null} say={say} onCancel={() => navigate('/projects')} onSaved={(p) => navigate(`/projects/${p.id}`)} />
        </div>
        {notice && (
          <Toast>
            <Check size={12} aria-hidden="true" /> {notice}
          </Toast>
        )}
      </div>
    );
  }

  if (failed) {
    return (
      <EmptyState icon={FolderOpen} title={t('Could not find that project')} body={t('The identifier in the URL matches no project, or the API is not responding.')} primaryAction={{ label: t('See all projects'), onClick: () => navigate('/projects') }} />
    );
  }

  if (!project) {
    return (
      <div className="fs-screen">
        <Skeleton label={t('Loading the project')} width="40%" height="32px" />
        <Skeleton label={t('Loading the detail')} count={5} height="20px" />
      </div>
    );
  }

  // The primary working folder is the one root that is not a link: it comes
  // off the project record and is changed in Settings, not detached here.
  const workspaceName = project.workspace ? (project.workspace.split(/[\\/]/).filter(Boolean).pop() ?? project.workspace) : '';

  return (
    <div className="fs-screen fs-pj" data-testid="project" data-archived={project.archived || undefined}>
      <div>
        <Link to="/projects" className="fs-tab" style={{ paddingInline: 0 }}>
          <ArrowLeft size={14} aria-hidden="true" /> {t('Projects')}
        </Link>
      </div>

      <header className="fs-screen__head">
        <div className="fs-pj__title">
          <h1 className="fs-screen__title">{project.name}</h1>
          <p className="fs-screen__sub">{project.workspace ?? project.folder}</p>
        </div>
        <div className="fs-pj__head-actions">
          <Button variant="ghost" size="sm" icon={project.pinned ? PinOff : Pin} label={project.pinned ? t('Unpin') : t('Pin')} loading={busy === 'pin'} onClick={() => void patch('pin', { pinned: !project.pinned }, project.pinned ? t('Unpinned') : t('Pinned'))} testId="project-pin" />
          <Button
            variant="ghost"
            size="sm"
            icon={FolderOpen}
            label={t('The folder has moved…')}
            title={t('Point this project at a new folder — refuses one that does not exist, and keeps its memories and relations.')}
            onClick={() => {
              setRelocatePath(project.workspace ?? '');
              setRelocateErr(null);
              setRelocatePickUnavailable(false);
              setRelocating(true);
              setRelocateRecent(null);
              recentFolders().then(setRelocateRecent).catch(() => setRelocateRecent([]));
            }}
            testId="project-relocate"
          />
          <Menu
            trigger={<Button variant="ghost" size="sm" icon={Download} label={t('Export chats')} title={t('Every chat in this project as one .zip')} />}
            items={EXPORT_FORMATS.map((f) => ({
              label: FORMAT_LABEL[f] ?? f,
              onSelect: () => {
                const a = document.createElement('a');
                a.href = exportProjectUrl(project.id, f);
                a.download = '';
                document.body.appendChild(a);
                a.click();
                a.remove();
              },
            }))}
            align="end"
          />
          <Button variant="ghost" size="sm" icon={project.archived ? ArchiveRestore : Archive} label={project.archived ? t('Restore') : t('Archive')} loading={busy === 'archive'} onClick={() => void patch('archive', { archived: !project.archived }, project.archived ? t('Project restored') : t('Project archived'))} testId="project-archive" />
        </div>
      </header>

      {project.archived && (
        <p className="fs-notice" data-tone="warning">
          {t('This project is archived. Its existing chats still keep their context.')}
        </p>
      )}

      <div className="fs-tabs" role="tablist" aria-label={t('Project sections')}>
        {TABS.map((entry) => (
          <button key={entry.id} type="button" role="tab" aria-selected={tab === entry.id} className="fs-tab" data-testid={`project-tab-${entry.id}`} onClick={() => setTab(entry.id)}>
            {t(entry.label)}
            {/* The tab itself says it, so a working chat is visible without
                opening the section it lives in. */}
            {entry.id === 'chats' && liveChats > 0 && <ActivityDot state={liveTone} />}
          </button>
        ))}
      </div>

      {tab === 'brief' && (
        <div className="fs-pj__brief">
          <form
            className="fs-pj__start"
            onSubmit={(e) => {
              e.preventDefault();
              void start();
            }}
          >
            <label className="fs-pj__start-label" htmlFor="fs-pj-prompt">
              {t('Start a chat in {name}', { name: project.name })}
            </label>
            <textarea id="fs-pj-prompt" className="fs-field fs-pj__textarea" rows={2} value={prompt} onChange={(e) => setPrompt(e.target.value)} placeholder={t('What do you want to do here? (optional)')} disabled={project.archived} data-testid="project-prompt" />
            <div className="fs-pj__row">
              <select className="fs-field" value={routeId} onChange={(e) => setRouteId(e.target.value)} aria-label={t('Model for the new chat')} disabled={project.archived}>
                {routes.length === 0 && <option value="">{t('No model available')}</option>}
                {routes.map((r) => {
                  // The same reading the picker gives, in the only form a
                  // native <option> can carry: "16.4 GB · no room". Empty
                  // when the model is not served from this machine.
                  const summary = fitSummary(fitOf(r, fit));
                  return (
                    <option key={r.id} value={r.id}>
                      {summary ? `${r.model} · ${summary} · ${r.endpointName}` : `${r.model} · ${r.endpointName}`}
                    </option>
                  );
                })}
              </select>
              <Button type="submit" variant="primary" size="sm" icon={Send} label={t('Start chat')} loading={busy === 'start'} disabled={project.archived || !routes.length} testId="project-start" />
            </div>
          </form>

          <div className="fs-pj__cards">
            <section className="fs-panel fs-pj__card">
              <div className="fs-pj__card-head">
                <h3>{t('Instructions')}</h3>
                <Button variant="ghost" size="sm" label={t('Edit')} onClick={() => setTab('ajustes')} />
              </div>
              {project.instructions ? <p className="fs-prose fs-pj__instructions">{project.instructions}</p> : <p className="fs-pj__muted">{t('Add guidance that should apply to every chat in this project.')}</p>}
            </section>

            <section className="fs-panel fs-pj__card">
              <div className="fs-pj__card-head">
                <h3>{t('The agent here')}</h3>
                <Button variant="ghost" size="sm" label={t('Change')} onClick={() => setTab('ajustes')} />
              </div>
              <ul className="fs-pj__flags">
                {flags.map((f) => (
                  <li key={f.key} data-on={f.on || undefined} title={t(f.help)}>
                    <span className="fs-pj__flag-dot" aria-hidden="true" />
                    {t(f.label)}
                    <small>{f.on ? t('on') : t('off')}</small>
                  </li>
                ))}
                {project.test_command && (
                  <li data-on="">
                    <span className="fs-pj__flag-dot" aria-hidden="true" />
                    {t('Test command')}
                    <small>
                      <code>{project.test_command}</code>
                    </small>
                  </li>
                )}
                {project.review_model && (
                  <li data-on="">
                    <span className="fs-pj__flag-dot" aria-hidden="true" />
                    {t('Reviewer model')}
                    <small>{project.review_model}</small>
                  </li>
                )}
              </ul>
            </section>

            <section className="fs-panel fs-pj__card">
              <div className="fs-pj__card-head">
                <h3>{t('Context sources')}</h3>
                <Button variant="ghost" size="sm" icon={FolderPlus} label={t('Add a work root')} onClick={() => void addRoot()} testId="project-add-root" />
              </div>
              <p className="fs-pj__muted">{t('What this project knows. A work root is also editable; everything else is read only.')}</p>
              {rootInput !== null && (
                <form
                  className="fs-pj__row"
                  onSubmit={(e) => {
                    e.preventDefault();
                    void addRootPath(rootInput);
                  }}
                >
                  <input className="fs-field fs-pj__grow" value={rootInput} onChange={(e) => setRootInput(e.target.value)} placeholder={t('Path of the folder or file to add')} spellCheck={false} data-testid="project-root-path" />
                  <Button type="submit" variant="secondary" size="sm" label={t('Add')} disabled={!rootInput.trim()} />
                  <Button variant="ghost" size="sm" icon={X} label={t('Cancel')} onClick={() => setRootInput(null)} />
                </form>
              )}
              <ul className="fs-pj__roots">
                {project.workspace && (
                  <li key="primary">
                    <FolderOpen size={13} aria-hidden="true" />
                    <span>
                      <strong>{workspaceName}</strong>
                      <small>
                        {project.workspace} {'\u00b7'} {t('primary work folder')}
                      </small>
                    </span>
                    <Button variant="ghost" size="sm" label={t('Change')} onClick={() => setTab('ajustes')} />
                  </li>
                )}
                {(links ?? []).map((link) => {
                  const Icon = KIND_ICON[link.kind];
                  return (
                    <li key={link.id} data-broken={linkIsBroken(linkStates[link.id]) || undefined}>
                      <Icon size={13} aria-hidden="true" />
                      <span>
                        <strong>{link.label}</strong>
                        <LinkBadges link={link} status={linkStates[link.id]} />
                      </span>
                    </li>
                  );
                })}
              </ul>
              {links !== null && links.length === 0 && !project.workspace && (
                <p className="fs-pj__muted">{t('Add a primary folder or link a document to start working.')}</p>
              )}
              <Button variant="ghost" size="sm" icon={Link2} label={t('Manage sources')} onClick={() => setTab('contexto')} testId="project-manage-sources" />
            </section>
          </div>
          <p className="fs-file__meta">
            {t('Created {when}', { when: relativeTime(project.created_at) })} · {t('updated {when}', { when: relativeTime(project.updated_at) })}
          </p>
        </div>
      )}

      {tab === 'chats' && (
        <div className="fs-panel">
          <div className="fs-pj__card-head">
            <h3>{chats ? tn(chats.length, '{n} conversation in {folder}', '{n} conversations in {folder}', { folder: project.folder ?? '' }) : t('Conversations')}</h3>
            {!project.archived && <Button variant="secondary" size="sm" icon={Plus} label={t('New chat')} onClick={() => setTab('brief')} />}
          </div>
          {chats === null ? (
            <Skeleton label={t('Loading the conversations')} count={3} height="44px" />
          ) : chats.length === 0 ? (
            <p className="fs-pj__muted">{t('No chats yet. Start one from the brief and it will stay grouped here.')}</p>
          ) : (
            <div className="fs-list fs-list--rail">
              {chats.map((c) => {
                const live = sessionActivity(activity, c.id);
                return (
                  <div key={c.id} className="fs-pj__chat">
                    <Link to={`/studio?s=${encodeURIComponent(c.id)}`} className="fs-row" data-testid="project-chat">
                      <span className="fs-row__main">
                        <span className="fs-row__name">
                          {live && <ActivityDot state={live} position={activity.queued[c.id]} detail={activity.details[c.id]} withLabel />}
                          {c.name || t('Untitled')}
                        </span>
                        <span className="fs-row__meta">
                          {[c.model, tn(c.messageCount, '{n} message', '{n} messages'), relativeTime(c.lastMessageAt ?? c.createdAt)].filter(Boolean).join(' · ')}
                        </span>
                      </span>
                    </Link>
                    <Button variant="ghost" size="sm" icon={Trash2} label={t('Delete')} onClick={() => setConfirm({ chat: c.id })} />
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}

      {tab === 'objetivos' && (
        <div className="fs-panel">
          <ProjectObjectives projectId={project.id} say={say} />
        </div>
      )}

      {tab === 'memoria' && (
        <div className="fs-panel">
          <ProjectMemoryFiles project={project} say={say} />
        </div>
      )}

      {tab === 'actividad' && (
        <div className="fs-panel">
          <ProjectAudit projectId={project.id} say={say} />
        </div>
      )}

      {tab === 'contexto' && (
        <div className="fs-pj__brief">
          <div className="fs-panel">
            {/* `loadLinks`, not `reload`: toggling one policy has no reason to
                re-fetch the chats and the whole prepended block. */}
            <ProjectSources project={project} links={links} statuses={linkStates} say={say} reload={loadLinks} />
          </div>
          <p className="fs-panel__label">{t('What the model receives, literally')}</p>
          <p className="fs-prose" style={{ marginBlockEnd: 'var(--fs-space-3)' }}>
            {t('This block is what Faustus prepends to every conversation of this project. It was available in the API and no screen showed it: knowing what it knows before asking for anything is half of trusting it.')}
          </p>
          {context === null ? (
            <Skeleton label={t('Loading the context')} count={4} height="20px" />
          ) : context ? (
            <pre className="fs-context" data-testid="project-context">
              {context}
            </pre>
          ) : (
            <EmptyState icon={Eye} title={t('No context block')} body={t('This project prepends nothing yet. As soon as it has a folder, instructions or memory, it will appear here exactly as the model reads it.')} />
          )}
        </div>
      )}

      {tab === 'ajustes' && (
        <div className="fs-panel">
          <ProjectSettings
            project={project}
            say={say}
            onCancel={() => setTab('brief')}
            onSaved={(p) => {
              setProject(p);
              void reload();
              setTab('brief');
            }}
          />
          <div className="fs-pj__danger">
            <h3>{t('Danger zone')}</h3>
            <p className="fs-pj__muted">{t('Deleting the project keeps its chats and its folder; only the grouping, the instructions and the objectives go.')}</p>
            <Button variant="danger" size="sm" icon={Trash2} label={t('Delete project')} onClick={() => setConfirm('delete')} testId="project-delete" />
          </div>
        </div>
      )}

      {confirm === 'delete' && (
        <Dialog
          open
          onOpenChange={(o) => !o && setConfirm(null)}
          title={t('Delete “{name}”?', { name: project.name })}
          testId="project-confirm-delete"
          footer={
            <>
              <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setConfirm(null)} />
              <Button
                variant="danger-solid"
                size="sm"
                label={t('Delete')}
                loading={busy === 'delete'}
                onClick={() => {
                  setBusy('delete');
                  void deleteProject(project.id)
                    .then(() => navigate('/projects'))
                    .catch((e: Error) => {
                      say(e.message);
                      setBusy(null);
                    });
                }}
                testId="project-confirm-delete-ok"
              />
            </>
          }
        >
          <p className="fs-prose">{t('The chats stay, ungrouped. The folder on disk is not touched.')}</p>
        </Dialog>
      )}

      {confirm && typeof confirm === 'object' && (
        <Dialog
          open
          onOpenChange={(o) => !o && setConfirm(null)}
          title={t('Delete this chat?')}
          footer={
            <>
              <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setConfirm(null)} />
              <Button
                variant="danger-solid"
                size="sm"
                label={t('Delete')}
                onClick={() => {
                  const id = confirm.chat;
                  setConfirm(null);
                  void removeChatFromProject(project.id, id)
                    .then(() => chatsIn(project))
                    .then(setChats)
                    .then(() => say(t('Chat deleted')), (e: Error) => say(e.message));
                }}
              />
            </>
          }
        >
          <p className="fs-prose">{t('This cannot be undone.')}</p>
        </Dialog>
      )}

      {relocating && (
        <Dialog
          open
          onOpenChange={(o) => !o && setRelocating(false)}
          title={t('The folder has moved…')}
          testId="project-relocate-dialog"
          footer={
            <>
              <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setRelocating(false)} />
              <Button
                variant="primary"
                size="sm"
                label={t('Update folder')}
                loading={relocateBusy}
                disabled={!relocatePath.trim()}
                onClick={() => void doRelocate()}
                testId="project-relocate-confirm"
              />
            </>
          }
        >
          <p className="fs-prose">{t('The chats, memories and objectives stay linked to this project — only where its files live on disk changes. The new folder must already exist.')}</p>
          <label className="fs-field-label">
            {t('New folder path')}
            <div className="fs-inline">
              <input
                className="fs-field"
                value={relocatePath}
                onChange={(e) => setRelocatePath(e.target.value)}
                placeholder={project.workspace ?? ''}
                data-testid="project-relocate-path"
                autoFocus
              />
              <Button variant="ghost" size="sm" icon={FolderOpen} label={t('Browse…')} onClick={() => void browseRelocate()} testId="project-relocate-browse" />
            </div>
          </label>
          {relocatePickUnavailable && <p className="fs-set__help">{t('No system dialog is available for this browser — pick a recent folder below, or type the path.')}</p>}
          {relocateRecent && relocateRecent.length > 0 && (
            <div className="fs-relocate__recent" data-testid="project-relocate-recent">
              <span className="fs-set__help">{t('Recent folders')}</span>
              <ul>
                {relocateRecent.filter((f) => f.path !== project.workspace).map((f) => (
                  <li key={f.path}>
                    <button type="button" className="fs-chip" onClick={() => setRelocatePath(f.path)} title={f.path}>
                      {f.projectName ? t('{path} ({project})', { path: f.path, project: f.projectName }) : f.path}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {relocateErr && <p className="fs-set__help" data-tone="bad" role="alert">{relocateErr}</p>}
        </Dialog>
      )}

      {notice && (
        <Toast>
          <Check size={12} aria-hidden="true" /> {notice}
        </Toast>
      )}
    </div>
  );
}
