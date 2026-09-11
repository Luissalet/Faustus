import { GitBranch, RefreshCw } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router';
import { EmptyState, IconButton, Toast } from '../components';
import {
  canCommit,
  commit as commitRepo,
  discard as discardFiles,
  fetchRemote,
  getCommit,
  getCommitDiff,
  getLog,
  getRepo,
  getStatus,
  getWorkingDiff,
  listRepos,
  pull,
  push as pushRepo,
  stage as stageFiles,
  sync as syncRepo,
  unstage as unstageFiles,
  type GitCommit,
  type GitCommitDetail,
  type GitDiff,
  type GitRepo,
  type GitStatus,
} from '../adapters/git';
import { CommitDetail } from './source-control/CommitDetail';
import { CommitGraph } from './source-control/CommitGraph';
import { ChangesPane, type WorkingFileSelection } from './source-control/ChangesPane';
import { DiffPane } from './source-control/DiffPane';
import { RepoList, RepoListError, type RepoMenuAction } from './source-control/RepoList';
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

      <div className="fs-sc__layout" data-detail={selectedRepoId ? '' : undefined}>
        <aside className="fs-sc__repos" aria-label={t('Repositories')}>
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
                  <p className="fs-sc__diff-title">
                    {selectedFile.path} <span className="fs-muted">{selectedFile.staged ? t('(staged)') : t('(working tree)')}</span>
                  </p>
                  <DiffPane path={selectedFile.path} diff={diff} loading={diffLoading} error={diffError} />
                </div>
              ) : (
                <EmptyState headingLevel={3} title={t('Nothing selected')} body={t('Pick a changed file or a commit to see its diff.')} />
              )}
            </div>
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
    </div>
  );
}
