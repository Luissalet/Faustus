import { CornerDownRight, MoreHorizontal, RefreshCw } from 'lucide-react';
import { EmptyState, IconButton, Menu, Skeleton } from '../../components';
import { aheadBehindLabel, repoProjectsLabel, type GitRepo } from '../../adapters/git';
import { BranchPopover } from './BranchPopover';
import { t } from '../../i18n';

export type RepoMenuAction = 'fetch' | 'pull' | 'push' | 'refresh';

/**
 * The REPOSITORIES rail: every git repo found under the owner's linked
 * project folders (nested ones indented under their parent), each with its
 * branch chip, ahead/behind, a dirty-count badge and the sync/⋯ controls
 * the contract asks for — the same left column VS Code's Source Control
 * view opens with.
 */
export function RepoList({
  repos,
  gitVersion,
  selectedId,
  onSelect,
  onRepoUpdate,
  busyId,
  onSync,
  onMenuAction,
}: {
  repos: GitRepo[] | null;
  gitVersion: string | null;
  selectedId: string | null;
  onSelect: (repo: GitRepo) => void;
  onRepoUpdate: (repo: GitRepo) => void;
  busyId: string | null;
  onSync: (repo: GitRepo) => void;
  onMenuAction: (repo: GitRepo, action: RepoMenuAction) => void;
}) {
  if (repos === null) {
    return <Skeleton label={t('Loading repositories')} count={4} height="52px" />;
  }

  if (repos.length === 0) {
    return (
      <EmptyState
        headingLevel={3}
        title={t('No git repositories found')}
        body={
          gitVersion === null
            ? t('git is not available in this environment, so folders cannot be scanned for repositories.')
            : t('Link a project folder that contains a git repository (a .git directory or worktree file) to see it here.')
        }
      />
    );
  }

  return (
    <div className="fs-sc__repo-list" role="list" aria-label={t('Repositories')}>
      {repos.map((repo) => {
        const dirtyTotal = repo.dirty.staged + repo.dirty.unstaged + repo.dirty.untracked;
        const ab = aheadBehindLabel(repo.ahead, repo.behind);
        const projectsLabel = repoProjectsLabel(repo);
        return (
          <div
            key={repo.id}
            role="listitem"
            className="fs-sc__repo-row"
            data-nested={repo.parent_repo_id ? '' : undefined}
            data-current={repo.id === selectedId || undefined}
          >
            {repo.parent_repo_id && <CornerDownRight size={12} aria-hidden="true" className="fs-sc__repo-nest" />}
            <button
              type="button"
              className="fs-sc__repo-main"
              onClick={() => onSelect(repo)}
              aria-current={repo.id === selectedId ? 'true' : undefined}
              data-testid="repo-row"
            >
              <span className="fs-sc__repo-name">
                <span className="fs-sc__repo-name-text" title={repo.name}>{repo.name}</span>
                {dirtyTotal > 0 && (
                  <span className="fs-sc__repo-dirty" data-testid="repo-dirty-count" aria-label={t('{n} pending changes', { n: dirtyTotal })}>
                    {dirtyTotal}
                  </span>
                )}
              </span>
              {projectsLabel && <span className="fs-sc__repo-project" title={projectsLabel}>{projectsLabel}</span>}
            </button>
            <div className="fs-sc__repo-actions">
              <BranchPopover repoId={repo.id} currentBranch={repo.branch} detached={repo.detached} onCheckedOut={onRepoUpdate} />
              {ab && <span className="fs-sc__repo-ab" data-testid="repo-ahead-behind">{ab}</span>}
              <IconButton
                icon={RefreshCw}
                label={t('Sync (pull, then push)')}
                size="sm"
                disabled={busyId === repo.id}
                onClick={() => onSync(repo)}
                testId="repo-sync"
              />
              <Menu
                trigger={<IconButton icon={MoreHorizontal} label={t('More actions for {name}', { name: repo.name })} size="sm" testId="repo-menu" />}
                align="end"
                items={[
                  { label: t('Fetch'), onSelect: () => onMenuAction(repo, 'fetch') },
                  { label: t('Pull'), onSelect: () => onMenuAction(repo, 'pull') },
                  { label: t('Push'), onSelect: () => onMenuAction(repo, 'push') },
                  null,
                  { label: t('Refresh'), onSelect: () => onMenuAction(repo, 'refresh') },
                ]}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}

/** Kept here so `SourceControl.tsx` does not need its own "retry" button
 *  wired by hand for the top-level load failure. */
export function RepoListError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <EmptyState
      tone="error"
      headingLevel={3}
      title={t('Could not load repositories')}
      body={message}
      primaryAction={{ label: t('Retry'), onClick: onRetry }}
    />
  );
}
