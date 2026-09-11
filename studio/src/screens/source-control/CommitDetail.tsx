import { Copy, X } from 'lucide-react';
import { IconButton, Skeleton } from '../../components';
import { relativeTime } from '../../adapters/home';
import { fileStatusLabel, type GitCommitDetail, type GitDiff } from '../../adapters/git';
import { DiffPane } from './DiffPane';
import { t } from '../../i18n';

/**
 * One commit, picked from the graph: its message, author, date and the
 * files it touched — click a file to open its diff below, the same as
 * clicking a commit in VS Code's Graph opens its changed files.
 */
export function CommitDetail({
  commit,
  loading,
  selectedPath,
  onSelectFile,
  diff,
  diffLoading,
  diffError,
  onClose,
  onCopySha,
}: {
  commit: GitCommitDetail | null;
  loading: boolean;
  selectedPath: string | null;
  onSelectFile: (path: string) => void;
  diff: GitDiff | null;
  diffLoading: boolean;
  diffError: string | null;
  onClose: () => void;
  onCopySha: (sha: string) => void;
}) {
  if (loading) {
    return <Skeleton label={t('Loading commit')} count={5} height="16px" />;
  }
  if (!commit) return null;

  return (
    <div className="fs-sc__commit-detail" data-testid="commit-detail">
      <div className="fs-sc__commit-detail-head">
        <div className="fs-sc__commit-detail-title">
          <p className="fs-sc__commit-message">{commit.message}</p>
          {commit.body && <p className="fs-sc__commit-body">{commit.body}</p>}
          <p className="fs-sc__commit-meta">
            <code>{commit.short}</code>
            <span>{commit.author}</span>
            <span title={commit.date}>{relativeTime(commit.date)}</span>
          </p>
        </div>
        <div className="fs-inline">
          <IconButton icon={Copy} label={t('Copy commit hash')} size="sm" onClick={() => onCopySha(commit.sha)} testId="commit-copy-sha" />
          <IconButton icon={X} label={t('Close commit')} size="sm" onClick={onClose} testId="commit-close" />
        </div>
      </div>

      <div className="fs-sc__commit-files" role="list" aria-label={t('Files changed')}>
        {commit.files.map((f) => (
          <button
            key={f.path}
            type="button"
            role="listitem"
            className="fs-sc__file-row"
            aria-current={f.path === selectedPath ? 'true' : undefined}
            onClick={() => onSelectFile(f.path)}
            data-testid="commit-file-row"
          >
            <span className="fs-sc__file-status" data-status={f.status} title={t(fileStatusLabel(f.status))}>
              {f.status}
            </span>
            <span className="fs-sc__file-path">{f.path}</span>
            {(f.additions !== null || f.deletions !== null) && (
              <span className="fs-sc__file-stats">
                {f.additions !== null && <span className="fs-diff-add">+{f.additions}</span>}
                {f.deletions !== null && <span className="fs-diff-del">−{f.deletions}</span>}
              </span>
            )}
          </button>
        ))}
      </div>

      {selectedPath && (
        <div className="fs-sc__commit-diff">
          <p className="fs-sc__diff-title">{selectedPath}</p>
          <DiffPane path={selectedPath} diff={diff} loading={diffLoading} error={diffError} />
        </div>
      )}
    </div>
  );
}
