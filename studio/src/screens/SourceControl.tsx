import { ArrowDownToLine, ArrowUpFromLine, Bot, Download, Github, GitBranch, GitBranchPlus, Plus, RefreshCw, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router';
import { Button, EmptyState, IconButton, Toast } from '../components';
import {
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
  listRepos,
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
} from '../adapters/git';
import { CommitDetail } from './source-control/CommitDetail';
import { CommitGraph } from './source-control/CommitGraph';
import { ChangesPane, type WorkingFileSelection } from './source-control/ChangesPane';
import { DiffPane } from './source-control/DiffPane';
import { RepoList, RepoListError, type RepoMenuAction } from './source-control/RepoList';
import { NewRepositoryDialog } from './source-control/NewRepositoryDialog';
import { CreateBranchDialog } from './source-control/CreateBranchDialog';
import { IdentityChip } from './source-control/IdentityChip';
import { RepoPolicyDialog } from './source-control/RepoPolicyDialog';
import { PublishToGithubDialog } from './source-control/PublishToGithubDialog';
import './source-control.css';
import { t } from '../i18n';

const LOG_PAGE = 50;
const POLL_MS = 15000;

/**
 * OBJ-4 — Source control: every git repo under the owner's linked project
 * folders (nested ones included), each with its branch, ahead/behind and
 * pending changes; select one to see CHANGES (stage/unstage + the commit
 * box) and the commit GRAPH, and pick a file or a commit to see its diff.
 * The VS Code Source Control view this mirrors, over `/api/git/*`
 * (`adapters/git.ts`, contract in the OBJ-4 lots' shared brief).
 */
export function SourceControlScreen() {
  const [params, setParams] = useSearchParams();
  const projectId = params.get('project') ?? undefined;

  const [repos, setRepos] = useState<GitRepo[] | null>(null);
  const [gitVersion, setGitVersion] = useState<string | null>(null);
  const [reposError, setReposError] = useState<string | null>(null);
  const [reposLoading, setReposLoading] = useState(true);

  const selectedRepoId = params.get('repo');
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

  // Lote 83: "New repository" and the per-repo "Agent & this repository"
  // policy dialog — both operate on `repos`/`selectedRepo` the same way
  // every other mutation on this screen does (mergeRepo).
  const [newRepoOpen, setNewRepoOpen] = useState(false);
  const [policyDialogOpen, setPolicyDialogOpen] = useState(false);

  // Lote 85: the center column's own "New branch" (its own branches fetch —
  // BranchPopover's is per-row and lazy the same way, so this mirrors it
  // rather than lifting that state up) and "Publish to GitHub".
  const [newBranchOpen, setNewBranchOpen] = useState(false);
  const [headerBranches, setHeaderBranches] = useState<GitBranchesResponse | null>(null);
  const [publishOpen, setPublishOpen] = useState(false);

  const say = useCallback((text: string) => {
    setToast(text);
    if (toastTimer.current) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), 3000);
  }, []);

  const loadRepos = useCallback(() => {
    setReposLoading(true);
    setReposError(null);
    listRepos(projectId)
      .then((res) => {
        setRepos(res.repos);
        setGitVersion(res.git_version);
      })
      .catch((e: unknown) => setReposError((e as Error).message))
      .finally(() => setReposLoading(false));
  }, [projectId]);

  useEffect(() => { loadRepos(); }, [loadRepos]);

  const selectRepo = useCallback(
    (repo: GitRepo) => {
      const next = new URLSearchParams(params);
      next.set('repo', repo.id);
      setParams(next, { replace: false });
      setSelectedFile(null);
      setSelectedCommitSha(null);
      setSelectedCommit(null);
      setSelectedCommitPath(null);
      setDiff(null);
      setMessage('');
      setCommitError(null);
    },
    [params, setParams],
  );

  const mergeRepo = useCallback((updated: GitRepo) => {
    setRepos((cur) => (cur ? cur.map((r) => (r.id === updated.id ? updated : r)) : cur));
  }, []);

  const mergeRepoPolicy = useCallback((repoId: string, policy: RepoPolicyResponse) => {
    setRepos((cur) => (cur ? cur.map((r) => (r.id === repoId ? { ...r, policy } : r)) : cur));
  }, []);

  const refreshStatus = useCallback(
    (repoId: string, quiet = false) => {
      if (!quiet) setStatusLoading(true);
      return getStatus(repoId)
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

  const loadLog = useCallback((repoId: string) => {
    setLogLoading(true);
    setLogError(null);
    getLog(repoId, { limit: LOG_PAGE })
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
  }, []);

  // A repo just got selected (or the URL named one on load): pull its
  // status and first page of history.
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

  // Lote 85 (CONTRATO_GIT_3.md, point 3): the whole REPOSITORIES rail
  // polls too, not just the selected repo — `?light=1` so up to a few
  // dozen repos cost one cheap call apiece instead of the full
  // remotes/user/identity/policy lookup, merged in-place
  // (`mergeLightRepos`) so `identity`/`policy`/`remotes` from the last
  // full load never flicker away between ticks.
  const reposLoaded = repos !== null;
  useEffect(() => {
    if (!reposLoaded) return;
    const tick = () => {
      if (document.visibilityState !== 'visible') return;
      listRepos(projectId, { light: true })
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
  }, [reposLoaded, projectId]);

  // The header's own "New branch" fetches its options only once opened —
  // same reason BranchPopover's per-row fetch is lazy (up to dozens of
  // repos on screen, nobody asked for their branches yet).
  useEffect(() => {
    if (!newBranchOpen || !selectedRepoId) return;
    getBranches(selectedRepoId).then(setHeaderBranches).catch(() => setHeaderBranches(null));
  }, [newBranchOpen, selectedRepoId]);

  // Whichever diff target is active — a working-tree file or a file inside
  // an open commit — fetch its diff. The two never overlap: picking one
  // clears the other (see selectFile/selectCommitFile below).
  useEffect(() => {
    if (!selectedRepoId) return;
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
  }, [selectedRepoId, selectedFile, selectedCommitSha, selectedCommitPath]);

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

  const commitDisabled = !status || !canCommit(message, status.staged.length) || committing;

  const onRepoCreated = (repo: GitRepo) => {
    setRepos((cur) => (cur ? [...cur, repo] : [repo]));
    selectRepo(repo);
    say(t('Repository created.'));
  };

  return (
    <div className="fs-screen fs-sc" data-testid="source-control">
      <header className="fs-screen__head">
        <div className="fs-sc__title">
          <h1 className="fs-screen__title">
            <GitBranch size={20} aria-hidden="true" /> {t('Source control')}
          </h1>
          <p className="fs-screen__sub">{t('Every git repository under your linked project folders.')}</p>
        </div>
        <IconButton icon={RefreshCw} label={t('Refresh repositories')} onClick={loadRepos} disabled={reposLoading} testId="source-control-refresh" />
      </header>

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

      <NewRepositoryDialog open={newRepoOpen} onOpenChange={setNewRepoOpen} onCreated={onRepoCreated} />

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
