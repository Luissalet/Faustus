import { AlertTriangle } from 'lucide-react';
import { useMemo } from 'react';
import { Skeleton } from '../../components';
import { parseUnifiedDiff, type GitDiff } from '../../adapters/git';
import { t } from '../../i18n';

/**
 * A read-only render of a unified diff `git diff`/`git show` already
 * produced — `+`/`−` lines coloured with the same `fs-diff-add`/
 * `fs-diff-del` tokens `documents/DiffView.tsx` uses, so a diff reads the
 * same whether it came from a document comparison or a repo. Not built on
 * `DiffView` itself: that component re-computes its own line diff from two
 * whole texts, and all this ever has is the text git already decided was
 * the difference (`diff`, possibly `truncated`) — recomputing it from
 * scratch could disagree with what git reported.
 */
export function DiffPane({ path, diff, loading, error }: { path: string; diff: GitDiff | null; loading: boolean; error: string | null }) {
  const lines = useMemo(() => (diff ? parseUnifiedDiff(diff.diff) : []), [diff]);

  if (loading) {
    return <Skeleton label={t('Loading diff for {path}', { path })} count={6} height="14px" />;
  }

  if (error) {
    return (
      <p className="fs-notice" data-tone="danger" role="alert">
        {error}
      </p>
    );
  }

  if (!diff) return null;

  if (diff.binary) {
    return (
      <p className="fs-notice" data-tone="warning">
        <AlertTriangle size={14} aria-hidden="true" /> {t('{path} is binary — no line-by-line diff to show.', { path })}
      </p>
    );
  }

  if (!diff.diff.trim()) {
    return <p className="fs-muted">{t('No differences.')}</p>;
  }

  return (
    <div className="fs-sc__diff" data-testid="source-control-diff">
      {diff.truncated && (
        <p className="fs-notice" data-tone="warning">
          <AlertTriangle size={14} aria-hidden="true" /> {t('This diff was too large and was truncated.')}
        </p>
      )}
      <pre className="fs-sc__diff-body">
        {lines.map((line, i) => (
          <div key={i} className="fs-sc__diff-line" data-kind={line.type}>
            <span aria-hidden="true" className="fs-sc__diff-gutter">
              {line.type === 'add' ? '+' : line.type === 'del' ? '−' : ''}
            </span>
            <span className={line.type === 'add' ? 'fs-diff-add' : line.type === 'del' ? 'fs-diff-del' : undefined}>{line.text || ' '}</span>
          </div>
        ))}
      </pre>
    </div>
  );
}
