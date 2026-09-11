import { ArrowDownToLine, ArrowUpFromLine, Bot, Download, Github, GitBranch, GitBranchPlus, GitMerge, Plus, RefreshCw, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router';
import { Button, EmptyState, IconButton, Skeleton, Toast } from '../../components';
import { relativeTime } from '../../adapters/home';
import {
  aheadBehindLabel,
  canCommit,
  commit as commitRepo,
  discard as discardFiles,
  fetchRemote,
  getBranches,
  getCommit,
  getCommitDiff,
  getLog,
  getRepo,
  getStatus,
  getWorkingDiff,
  GIT_REFRESH_EVENT,
  listRepos,
  mergeAbort,
  mergeLightRepos,
  pull,
  push as pushRepo,
  stage as stageFiles,
  sync as syncRepo,
  unstage as unstageFiles,
  type GitBranchesResponse,
  type GitCommit,
  type GitCommitDetail,
  type GitDiff,
  type GitRepo,
  type GitStatus,
  type RepoPolicyResponse,
} from '../../adapters/git';
import { CommitDetail } from './CommitDetail';
import { CommitGraph } from './CommitGraph';
import { ChangesPane, type WorkingFileSelection } from './ChangesPane';
import { DiffPane } from './DiffPane';
import { RepoList, RepoListError, type RepoMenuAction } from './RepoList';
import { NewRepositoryDialog } from './NewRepositoryDialog';
import { CreateBranchDialog } from './CreateBranchDialog';
import { MergeDialog } from './MergeDialog';
import { IdentityChip } from './IdentityChip';
import { RepoPolicyDialog } from './RepoPolicyDialog';
import { PublishToGithubDialog } from './PublishToGithubDialog';
import { BranchPopover } from './BranchPopover';
import '../source-control.css';
import { t } from '../../i18n';

const LOG_PAGE = 50;
const COMPACT_LOG = 8;
const POLL_MS = 15000;

export interface SourceControlPanelProps {
  /** Scope the repo list to this project's linked folders (`?project_id=`
   *  server-side). Standalone `/source-control` reads the same idea off
   *  its own URL instead — see `SourceControl.tsx`. */
  projectId?: string;
  /** Pin the initial selection to this repo (an uncontrolled default: the
   *  person picking a different one in the full list still wins). */
  repoId?: string;
  /** Lote 86: the condensed view for the chat's side panel — no repo rail,
   *  no New repository/branch/GitHub/policy flows, just what the contract
   *  asks for: branch (clickable), ↓↑, changes with stage/commit/push, the
   *  last {@link COMPACT_LOG} commits, and Fetch/Pull/Push/Sync. */
  compact?: boolean;
  /** Compact mode only: the repo whose `path` matches this working folder
   *  is shown; absent a match (or with none given), the first repo of
   *  `projectId`'s list is used instead. */
  workspace?: string;
}

/**
 * OBJ-4 — Source control, reusable. Extracted from the standalone
 * `/source-control` screen (Lote 86, `CONTRATO_GIT_4.md`) so the same
 * repos-under-a-project logic and the same live git-management UI can be
 * mounted three ways: the full screen (`SourceControlScreen`, unchanged
 * behaviour), a project's own "Repositories" section (`Project.tsx`,
 * `projectId` scoped), and — the point of this lot — a compact panel
 * inside the project's chat (`SidePanel.tsx`), so what the agent does to
 * the repository is visible without leaving the conversation.
 */
export function SourceControlPanel({ projectId, repoId, compact = false, workspace }: SourceControlPanelProps) {
  const [params, setParams] = useSearchParams();
  // Compact mode never touches the URL — it lives inside a chat session's
  // own `?s=`/panel state, not a shareable "?project=&repo=" link.
  const effectiveProjectId = projectId ?? (!compact ? params.get('project') ?? undefined : undefined);

  const [repos, setRepos] = useState<GitRepo[] | null>(null);
  const [gitVersion, setGitVersion] = useState<string | null>(null);
  const [reposError, setReposError] = useState<string | null>(null);
  const [reposLoading, setReposLoading] = useState(true);

  // Uncontrolled selection: `repoId` (if given) or, in compact mode, an
  // auto-pick are only the *default* — clicking another repo in the full
  // rail (or the URL, outside compact) always wins from then on.
  const [manualRepoId, setManualRepoId] = useState<string | null>(null);
  const autoRepoId = useMemo(() => {
    if (!compact || !repos || repos.length === 0) return null;
    if (workspace) {
      const norm = (p: string) => p.replace(/[\\/]+$/, '');
      const match = repos.find((r) => norm(r.path) === norm(workspace));
      if (match) return match.id;
    }
    return repos[0].id;
  }, [compact, repos, workspace]);
  const selectedRepoId = manualRepoId ?? repoId ?? (compact ? autoRepoId : params.get('repo'));
  const selectedRepo = useMemo(() => repos?.find((r) => r.id === selectedRepoId) ?? null, [repos, selectedRepoId]);

  const [status, setStatus] = useState<GitStatus | null>(null);
  const [statusLoading, setStatusLoading] = useState(false);
  const [statusError, setStatusError] = useState<string | null>(null);

  const [commits, setCommits] = useState<GitCommit[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [logLoading, setLogLoading] = useState(false);
  const [logLoadingMore, setLogLoadingMore] = useState(false);
  const [logError, setLogError] = useState<string | null>(null);

  const [selectedFile, setSelectedFile] = useState<WorkingFileSelection | null>(null);
  const [selectedCommitSha, setSelectedCommitSha] = useState<string | null>(null);
  const [selectedCommit, setSelectedCommit] = useState<GitCommitDetail | null>(null);
  const [commitLoading, setCommitLoading] = useState(false);
  const [selectedCommitPath, setSelectedCommitPath] = useState<string | null>(null);

  const [diff, setDiff] = useState<GitDiff | null>(null);
  const [diffLoading, setDiffLoading] = useState(false);
  const [diffError, setDiffError] = useState<string | null>(null);

  const [message, setMessage] = useState('');
  const [committing, setCommitting] = useState(false);
  const [commitError, setCommitError] = useState<string | null>(null);
  const [actionBusy, setActionBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const [repoBusyId, setRepoBusyId] = useState<string | null>(null);
  const [repoActionError, setRepoActionError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const toastTimer = useRef<number | undefined>(undefined);

  // Full mode only (never rendered in compact): "New repository", the
  // per-repo policy dialog, "New branch" and "Publish to GitHub".
  const [newRepoOpen, setNewRepoOpen] = useState(false);
  const [policyDialogOpen, setPolicyDialogOpen] = useState(false);
  const [newBranchOpen, setNewBranchOpen] = useState(false);
  const [headerBranches, setHeaderBranches] = useState<GitBranchesResponse | null>(null);
  const [publishOpen, setPublishOpen] = useState(false);
  const [mergeOpen, setMergeOpen] = useState(false);
  const [abortingMerge, setAbortingMerge] = useState(false);

  const say = useCallback((text: string) => {
    setToast(text);
    if (toastTimer.current) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), 3000);
  }, []);

  const loadRepos = useCallback(() => {
    setReposLoading(true);
    setReposError(null);
    listRepos(effectiveProjectId)
      .then((res) => {
        setRepos(res.repos);
        setGitVersion(res.git_version);
      })
      .catch((e: unknown) => setReposError((e as Error).message))
      .finally(() => setReposLoading(false));
  }, [effectiveProjectId]);

  useEffect(() => { loadRepos(); }, [loadRepos]);

  const selectRepo = useCallback(
    (repo: GitRepo) => {
      setManualRepoId(repo.id);
      if (!compact) {
        const next = new URLSearchParams(params);
        next.set('repo', repo.id);
        setParams(next, { replace: false });
      }
      setSelectedFile(null);
      setSelectedCommitSha(null);
      setSelectedCommit(null);
      setSelectedCommitPath(null);
      setDiff(null);
      setMessage('');
      setCommitError(null);
    },
    [compact, params, setParams],
  );

  const mergeRepo = useCallback((updated: GitRepo) => {
    setRepos((cur) => (cur ? cur.map((r) => (r.id === updated.id ? updated : r)) : cur));
  }, []);

  const mergeRepoPolicy = useCallback((repoId2: string, policy: RepoPolicyResponse) => {
    setRepos((cur) => (cur ? cur.map((r) => (r.id === repoId2 ? { ...r, policy } : r)) : cur));
  }, []);

  const refreshStatus = useCallback(
    (repoId2: string, quiet = false) => {
      if (!quiet) setStatusLoading(true);
      return getStatus(repoId2)
        .then((s) => {
          setStatus(s);
          setStatusError(null);
        })
        .catch((e: unknown) => {
          if (!quiet) setStatusError((e as Error).message);
        })
        .finally(() => { if (!quiet) setStatusLoading(false); });
    },
    [],
  );

  const loadLog = useCallback((repoId2: string) => {
    setLogLoading(true);
    setLogError(null);
    getLog(repoId2, { limit: compact ? COMPACT_LOG : LOG_PAGE })
      .then((res) => {
        setCommits(res.commits);
        setNextCursor(res.next_cursor);
      })
      .catch((e: unknown) => {
        setCommits([]);
        setNextCursor(null);
        setLogError((e as Error).message);
      })
      .finally(() => setLogLoading(false));
  }, [compact]);

  // A repo just got selected (or the URL/auto-pick named one on load): pull
  // its status and first page of history.
  useEffect(() => {
    if (!selectedRepoId) { setStatus(null); setCommits([]); return; }
    void refreshStatus(selectedRepoId);
    loadLog(selectedRepoId);
  }, [selectedRepoId, refreshStatus, loadLog]);

  // UI-light polling (contract: every 15s while the tab is visible) —
  // status only, so a repo whose pane is open reflects what changed on
  // disk without the person hitting Refresh, without re-fetching the
  // whole graph every tick.
  useEffect(() => {
    if (!selectedRepoId) return;
    const tick = () => {
      if (document.visibilityState !== 'visible') return;
      void refreshStatus(selectedRepoId, true);
      getRepo(selectedRepoId).then(mergeRepo).catch(() => {});
    };
    const id = window.setInterval(tick, POLL_MS);
    const onVisible = () => { if (document.visibilityState === 'visible') tick(); };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      window.clearInterval(id);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [selectedRepoId, refreshStatus, mergeRepo]);

  // Lote 86: the chat's own turn events ping `GIT_REFRESH_EVENT` (a
  // `git_policy` event, or the turn ending — see `panel.ts::panelReducer`)
  // so what the agent just did shows up here without waiting for the next
  // 15s poll. Harmless (and just as correct) outside a chat too.
  useEffect(() => {
    if (!selectedRepoId) return;
    const onPing = () => {
      void refreshStatus(selectedRepoId, true);
      loadLog(selectedRepoId);
      getRepo(selectedRepoId).then(mergeRepo).catch(() => {});
    };
    window.addEventListener(GIT_REFRESH_EVENT, onPing);
    return () => window.removeEventListener(GIT_REFRESH_EVENT, onPing);
  }, [selectedRepoId, refreshStatus, loadLog, mergeRepo]);

  // Lote 85 (CONTRATO_GIT_3.md, point 3): the whole REPOSITORIES rail
  // polls too, not just the selected repo — `?light=1` so up to a few
  // dozen repos cost one cheap call apiece instead of the full
  // remotes/user/identity/policy lookup, merged in-place
  // (`mergeLightRepos`) so `identity`/`policy`/`remotes` from the last
  // full load never flicker away between ticks. Full mode only: compact
  // shows one repo, already covered by the ping/poll above.
  const reposLoaded = repos !== null;
  useEffect(() => {
    if (compact || !reposLoaded) return;
    const tick = () => {
      if (document.visibilityState !== 'visible') return;
      listRepos(effectiveProjectId, { light: true })
        .then((res) => setRepos((cur) => (cur ? mergeLightRepos(cur, res.repos) : cur)))
        .catch(() => {});
    };
    const id = window.setInterval(tick, POLL_MS);
    const onVisible = () => { if (document.visibilityState === 'visible') tick(); };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      window.clearInterval(id);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [compact, reposLoaded, effectiveProjectId]);

  // The header's own "New branch"/"Merge…" fetch their options only once
  // opened — same reason BranchPopover's per-row fetch is lazy (up to
  // dozens of repos on screen, nobody asked for their branches yet). Full
  // mode only.
  useEffect(() => {
    if (compact || !(newBranchOpen || mergeOpen) || !selectedRepoId) return;
    getBranches(selectedRepoId).then(setHeaderBranches).catch(() => setHeaderBranches(null));
  }, [compact, newBranchOpen, mergeOpen, selectedRepoId]);

  // Whichever diff target is active — a working-tree file or a file inside
  // an open commit — fetch its diff. The two never overlap: picking one
  // clears the other (see selectFile/selectCommit below). Full mode only:
  // compact has no room for a diff pane (see the panel's props doc).
  useEffect(() => {
    if (compact || !selectedRepoId) return;
    if (selectedFile) {
      setDiffLoading(true);
      setDiffError(null);
      getWorkingDiff(selectedRepoId, selectedFile.path, selectedFile.staged)
        .then(setDiff)
        .catch((e: unknown) => setDiffError((e as Error).message))
        .finally(() => setDiffLoading(false));
      return;
    }
    if (selectedCommitSha && selectedCommitPath) {
      setDiffLoading(true);
      setDiffError(null);
      getCommitDiff(selectedRepoId, selectedCommitSha, selectedCommitPath)
        .then(setDiff)
        .catch((e: unknown) => setDiffError((e as Error).message))
        .finally(() => setDiffLoading(false));
      return;
    }
    setDiff(null);
  }, [compact, selectedRepoId, selectedFile, selectedCommitSha, selectedCommitPath]);

  const selectFile = (file: WorkingFileSelection) => {
    setSelectedCommitSha(null);
    setSelectedCommit(null);
    setSelectedCommitPath(null);
    setSelectedFile(file);
  };

  const selectCommit = (repoCommit: GitCommit) => {
    if (!selectedRepoId) return;
    setSelectedFile(null);
    setSelectedCommitSha(repoCommit.sha);
    setSelectedCommitPath(null);
    setCommitLoading(true);
    getCommit(selectedRepoId, repoCommit.sha)
      .then(setSelectedCommit)
      .catch(() => setSelectedCommit(null))
      .finally(() => setCommitLoading(false));
  };

  const closeCommit = () => {
    setSelectedCommitSha(null);
    setSelectedCommit(null);
    setSelectedCommitPath(null);
  };

  const loadMoreCommits = () => {
    if (!selectedRepoId || !nextCursor) return;
    setLogLoadingMore(true);
    getLog(selectedRepoId, { limit: LOG_PAGE, cursor: nextCursor })
      .then((res) => {
        setCommits((cur) => [...cur, ...res.commits]);
        setNextCursor(res.next_cursor);
      })
      .finally(() => setLogLoadingMore(false));
  };

  const runAction = async (fn: () => Promise<{ repo: GitRepo }>): Promise<boolean> => {
    if (!selectedRepoId) return false;
    setActionBusy(true);
    setActionError(null);
    try {
      const result = await fn();
      mergeRepo(result.repo);
      await refreshStatus(selectedRepoId, true);
      return true;
    } catch (e) {
      setActionError((e as Error).message);
      return false;
    } finally {
      setActionBusy(false);
    }
  };

  const onStage = (paths: string[] | 'all') => void runAction(() => stageFiles(selectedRepoId as string, paths));
  const onUnstage = (paths: string[] | 'all') => void runAction(() => unstageFiles(selectedRepoId as string, paths));
  const onDiscard = (paths: string[]) => {
    void runAction(() => discardFiles(selectedRepoId as string, paths)).then((ok) => {
      if (ok && selectedFile && paths.includes(selectedFile.path)) setSelectedFile(null);
    });
  };

  const onCommit = () => {
    if (!selectedRepoId || !status) return;
    if (!canCommit(message, status.staged.length)) return;
    setCommitting(true);
    setCommitError(null);
    commitRepo(selectedRepoId, message.trim())
      .then((result) => {
        mergeRepo(result.repo);
        setMessage('');
        setSelectedFile(null);
        say(t('Committed {sha}', { sha: result.short }));
        return Promise.all([refreshStatus(selectedRepoId, true), loadLog(selectedRepoId)]);
      })
      .catch((e: unknown) => setCommitError((e as Error).message))
      .finally(() => setCommitting(false));
  };

  const handleRepoMenuAction = (repo: GitRepo, action: RepoMenuAction) => {
    setRepoBusyId(repo.id);
    setRepoActionError(null);
    const after = (updated: GitRepo, label: string) => {
      mergeRepo(updated);
      say(label);
      if (repo.id === selectedRepoId) void refreshStatus(repo.id, true);
    };
    const call =
      action === 'fetch' ? fetchRemote(repo.id).then((r) => after(r.repo, t('Fetched')))
      : action === 'pull' ? pull(repo.id).then((r) => after(r.repo, t('Pulled')))
      : action === 'push' ? pushRepo(repo.id).then((r) => after(r.repo, t('Pushed')))
      : getRepo(repo.id).then((r) => after(r, t('Refreshed')));
    call
      .catch((e: unknown) => setRepoActionError((e as Error).message))
      .finally(() => setRepoBusyId(null));
  };

  const handleSync = (repo: GitRepo) => {
    setRepoBusyId(repo.id);
    setRepoActionError(null);
    syncRepo(repo.id)
      .then((result) => {
        mergeRepo(result.repo);
        say(t('Synced'));
        if (repo.id === selectedRepoId) {
          void refreshStatus(repo.id, true);
          loadLog(repo.id);
        }
      })
      .catch((e: unknown) => setRepoActionError((e as Error).message))
      .finally(() => setRepoBusyId(null));
  };

  const onAbortMerge = () => {
    if (!selectedRepoId) return;
    setAbortingMerge(true);
    setActionError(null);
    mergeAbort(selectedRepoId)
      .then((result) => {
        mergeRepo(result.repo);
        say(t('Merge aborted.'));
        return refreshStatus(selectedRepoId, true);
      })
      .catch((e: unknown) => setActionError((e as Error).message))
      .finally(() => setAbortingMerge(false));
  };

  const onMerged = (updated: GitRepo, sha: string, mergedBranch: string) => {
    if (!selectedRepoId) return;
    mergeRepo(updated);
    say(t('Merged {branch} into {into} ({sha})', { branch: mergedBranch, into: updated.branch ?? '?', sha: sha.slice(0, 12) }));
    void refreshStatus(selectedRepoId, true);
    loadLog(selectedRepoId);
  };

  const onConflictKept = () => {
    if (!selectedRepoId) return;
    void refreshStatus(selectedRepoId, true);
    getRepo(selectedRepoId).then(mergeRepo).catch(() => {});
  };

  const commitDisabled = !status || !canCommit(message, status.staged.length) || committing;

  const onRepoCreated = (repo: GitRepo) => {
    setRepos((cur) => (cur ? [...cur, repo] : [repo]));
    selectRepo(repo);
    say(t('Repository created.'));
  };

  const ab = selectedRepo ? aheadBehindLabel(selectedRepo.ahead, selectedRepo.behind) : '';

  if (compact) {
    return (
      <div className="fs-sc fs-sc--compact" data-testid="source-control-panel-compact">
        {reposError ? (
          <RepoListError message={reposError} onRetry={loadRepos} />
        ) : reposLoading && !repos ? (
          <Skeleton label={t('Loading repositories')} count={3} height="24px" />
        ) : !selectedRepo ? (
          <EmptyState
            headingLevel={3}
            icon={GitBranch}
            title={t('No git repository here')}
            body={t('Link a project folder that contains a git repository to manage it from this chat.')}
          />
        ) : (
          <>
            <div className="fs-sc__repo-header" data-testid="source-control-compact-header">
              <div className="fs-sc__repo-header-title">
                <span className="fs-muted" title={selectedRepo.path}>{selectedRepo.name}</span>
                <BranchPopover repoId={selectedRepo.id} currentBranch={selectedRepo.branch} detached={selectedRepo.detached} onCheckedOut={mergeRepo} />
                {ab && <span className="fs-sc__repo-ab" data-testid="repo-ahead-behind">{ab}</span>}
              </div>
              <div className="fs-sc__repo-header-actions">
                <IconButton icon={Download} label={t('Fetch')} size="sm" disabled={repoBusyId === selectedRepo.id} onClick={() => handleRepoMenuAction(selectedRepo, 'fetch')} testId="repo-header-fetch" />
                <IconButton icon={ArrowDownToLine} label={t('Pull')} size="sm" disabled={repoBusyId === selectedRepo.id} onClick={() => handleRepoMenuAction(selectedRepo, 'pull')} testId="repo-header-pull" />
                <IconButton icon={ArrowUpFromLine} label={t('Push')} size="sm" disabled={repoBusyId === selectedRepo.id} onClick={() => handleRepoMenuAction(selectedRepo, 'push')} testId="repo-header-push" />
                <IconButton icon={RefreshCw} label={t('Sync')} size="sm" disabled={repoBusyId === selectedRepo.id} onClick={() => handleSync(selectedRepo)} testId="repo-header-sync" />
              </div>
            </div>
            {repoActionError && (
              <p className="fs-notice" data-tone="danger" role="alert" data-testid="repo-action-error">{repoActionError}</p>
            )}
            <ChangesPane
              status={status}
              loadingStatus={statusLoading}
              statusError={statusError}
              message={message}
              onMessageChange={setMessage}
              onCommit={onCommit}
              committing={committing}
              commitDisabled={commitDisabled}
              commitError={commitError}
              onStage={onStage}
              onUnstage={onUnstage}
              onDiscard={onDiscard}
              selectedFile={selectedFile}
              onSelectFile={selectFile}
              actionBusy={actionBusy}
              actionError={actionError}
              user={selectedRepo.user}
              onAbortMerge={onAbortMerge}
              abortingMerge={abortingMerge}
            />
            <div className="fs-sc__compact-log">
              <h3 className="fs-panel__label">{t('Recent commits')}</h3>
              {logLoading ? (
                <Skeleton label={t('Loading the commit history')} count={3} height="20px" />
              ) : logError ? (
                <p className="fs-notice" data-tone="danger" role="alert">{logError}</p>
              ) : commits.length === 0 ? (
                <p className="fs-muted">{t('No commits yet')}</p>
              ) : (
                <ul className="fs-sc__compact-commits" data-testid="compact-commit-list">
                  {commits.slice(0, COMPACT_LOG).map((c) => (
                    <li key={c.sha} title={c.message}>
                      <code>{c.short}</code>
                      <span className="fs-sc__compact-commit-message">{c.message}</span>
                      <span className="fs-muted">{relativeTime(c.date)}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </>
        )}
        {toast && <Toast>{toast}</Toast>}
      </div>
    );
  }

  return (
    <div className="fs-sc" data-testid="source-control-panel">
      {repoActionError && (
        <p className="fs-notice" data-tone="danger" role="alert" data-testid="repo-action-error">
          {repoActionError}
        </p>
      )}

      <div
        className="fs-sc__layout"
        data-detail={selectedRepoId ? '' : undefined}
        data-diff-open={selectedRepo && (selectedCommitSha || selectedFile) ? '' : undefined}
      >
        <aside className="fs-sc__repos" aria-label={t('Repositories')}>
          <div className="fs-sc__repos-head">
            <Button variant="ghost" size="sm" icon={Plus} label={t('New repository')} onClick={() => setNewRepoOpen(true)} testId="new-repo-open" />
            <IconButton icon={RefreshCw} label={t('Refresh repositories')} size="sm" onClick={loadRepos} disabled={reposLoading} testId="source-control-refresh" />
          </div>
          {reposError ? (
            <RepoListError message={reposError} onRetry={loadRepos} />
          ) : (
            <RepoList
              repos={repos}
              gitVersion={gitVersion}
              selectedId={selectedRepoId}
              onSelect={selectRepo}
              onRepoUpdate={mergeRepo}
              busyId={repoBusyId}
              onSync={handleSync}
              onMenuAction={handleRepoMenuAction}
            />
          )}
        </aside>

        {selectedRepo ? (
          <>
            <div className="fs-sc__middle">
              <div className="fs-sc__repo-header">
                <div className="fs-sc__repo-header-title">
                  <h2 title={selectedRepo.name}>{selectedRepo.name}</h2>
                  <p className="fs-muted" title={selectedRepo.path}>{selectedRepo.path}</p>
                </div>
                <div className="fs-sc__repo-header-actions">
                  <IdentityChip repo={selectedRepo} onRepoUpdate={mergeRepo} />
                  <Button variant="ghost" size="sm" icon={Bot} label={t('Agent & this repository')} onClick={() => setPolicyDialogOpen(true)} testId="repo-policy-open" />
                </div>
              </div>
              <div className="fs-sc__repo-ops">
                <Button
                  variant="ghost"
                  size="sm"
                  icon={Download}
                  label={t('Fetch')}
                  loading={repoBusyId === selectedRepo.id}
                  onClick={() => handleRepoMenuAction(selectedRepo, 'fetch')}
                  testId="repo-header-fetch"
                />
                <Button
                  variant="ghost"
                  size="sm"
                  icon={ArrowDownToLine}
                  label={t('Pull')}
                  loading={repoBusyId === selectedRepo.id}
                  onClick={() => handleRepoMenuAction(selectedRepo, 'pull')}
                  testId="repo-header-pull"
                />
                <Button
                  variant="ghost"
                  size="sm"
                  icon={ArrowUpFromLine}
                  label={t('Push')}
                  loading={repoBusyId === selectedRepo.id}
                  onClick={() => handleRepoMenuAction(selectedRepo, 'push')}
                  testId="repo-header-push"
                />
                <Button
                  variant="ghost"
                  size="sm"
                  icon={RefreshCw}
                  label={t('Sync')}
                  loading={repoBusyId === selectedRepo.id}
                  onClick={() => handleSync(selectedRepo)}
                  testId="repo-header-sync"
                />
                <Button variant="ghost" size="sm" icon={GitBranchPlus} label={t('New branch')} onClick={() => setNewBranchOpen(true)} testId="repo-header-new-branch" />
                <Button variant="ghost" size="sm" icon={GitMerge} label={t('Merge…')} onClick={() => setMergeOpen(true)} testId="repo-header-merge" />
                {!selectedRepo.remotes.some((r) => r.name === 'origin') && (
                  <Button variant="ghost" size="sm" icon={Github} label={t('Publish to GitHub')} onClick={() => setPublishOpen(true)} testId="repo-header-publish" />
                )}
              </div>
              <ChangesPane
                status={status}
                loadingStatus={statusLoading}
                statusError={statusError}
                message={message}
                onMessageChange={setMessage}
                onCommit={onCommit}
                committing={committing}
                commitDisabled={commitDisabled}
                commitError={commitError}
                onStage={onStage}
                onUnstage={onUnstage}
                onDiscard={onDiscard}
                selectedFile={selectedFile}
                onSelectFile={selectFile}
                actionBusy={actionBusy}
                actionError={actionError}
                user={selectedRepo.user}
                onAbortMerge={onAbortMerge}
                abortingMerge={abortingMerge}
              />
              <CommitGraph
                commits={commits}
                loading={logLoading}
                error={logError}
                selectedSha={selectedCommitSha}
                onSelect={selectCommit}
                hasMore={nextCursor !== null}
                loadingMore={logLoadingMore}
                onLoadMore={loadMoreCommits}
              />
            </div>
            {(selectedCommitSha || selectedFile) && (
              <div className="fs-sc__right">
                {selectedCommitSha ? (
                  <CommitDetail
                    commit={selectedCommit}
                    loading={commitLoading}
                    selectedPath={selectedCommitPath}
                    onSelectFile={setSelectedCommitPath}
                    diff={diff}
                    diffLoading={diffLoading}
                    diffError={diffError}
                    onClose={closeCommit}
                    onCopySha={(sha) => { void navigator.clipboard?.writeText(sha).then(() => say(t('Copied'))); }}
                  />
                ) : selectedFile ? (
                  <div className="fs-sc__working-diff">
                    <div className="fs-sc__diff-head">
                      <p className="fs-sc__diff-title">
                        {selectedFile.path} <span className="fs-muted">{selectedFile.staged ? t('(staged)') : t('(working tree)')}</span>
                      </p>
                      <IconButton icon={X} label={t('Close diff')} size="sm" onClick={() => setSelectedFile(null)} testId="working-diff-close" />
                    </div>
                    <DiffPane path={selectedFile.path} diff={diff} loading={diffLoading} error={diffError} />
                  </div>
                ) : null}
              </div>
            )}
          </>
        ) : (
          <div className="fs-sc__blank">
            <EmptyState
              icon={GitBranch}
              title={t('Select a repository')}
              body={t('Pick one from the list to see its changes and history.')}
            />
          </div>
        )}
      </div>

      {toast && <Toast>{toast}</Toast>}

      <NewRepositoryDialog open={newRepoOpen} onOpenChange={setNewRepoOpen} onCreated={onRepoCreated} projectId={effectiveProjectId} />

      {selectedRepo && (
        <RepoPolicyDialog
          open={policyDialogOpen}
          onOpenChange={setPolicyDialogOpen}
          repo={selectedRepo}
          onPolicyChange={(policy) => mergeRepoPolicy(selectedRepo.id, policy)}
        />
      )}

      {selectedRepo && (
        <CreateBranchDialog
          open={newBranchOpen}
          onOpenChange={setNewBranchOpen}
          repoId={selectedRepo.id}
          branches={headerBranches}
          onCreated={(updated) => {
            mergeRepo(updated);
            void refreshStatus(updated.id, true);
            loadLog(updated.id);
            say(t('Branch created.'));
          }}
        />
      )}

      {selectedRepo && (
        <MergeDialog
          open={mergeOpen}
          onOpenChange={setMergeOpen}
          repoId={selectedRepo.id}
          currentBranch={selectedRepo.branch}
          branches={headerBranches}
          onMerged={onMerged}
          onConflictKept={onConflictKept}
        />
      )}

      {selectedRepo && (
        <PublishToGithubDialog
          open={publishOpen}
          onOpenChange={setPublishOpen}
          repo={selectedRepo}
          onPublished={(updated) => {
            mergeRepo(updated);
            say(t('Published to GitHub.'));
          }}
        />
      )}
    </div>
  );
}
