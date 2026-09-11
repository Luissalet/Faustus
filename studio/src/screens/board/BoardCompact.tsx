import { useCallback, useEffect, useState } from 'react';
import { LayoutGrid, Plus } from 'lucide-react';
import { Button, EmptyState, Skeleton } from '../../components';
import { BOARD_REFRESH_EVENT, getSummary, type BoardSummary, type Issue } from '../../adapters/board';
import { IssueCard } from './IssueCard';
import { NewIssueDialog } from './NewIssueDialog';
import { IssueDetail } from './IssueDetail';
import { t } from '../../i18n';
import '../board.css';

/**
 * Lote 93 — the chat's own Board tab (`SidePanel.tsx`): `ready` +
 * `in_progress`, compact, plus a quick "New issue" (title + type only) —
 * the equivalent of `SourceControlPanel`'s `compact` mode, but the board
 * has no working-tree state to poll, so a `summary` fetch plus the same
 * `BOARD_REFRESH_EVENT` ping (`turn-end`, mirroring `GIT_REFRESH_EVENT`)
 * is enough to stay live.
 */
export function BoardCompact({
  projectId,
  /** Set from outside (an issue-id chip elsewhere in the chat, via
   *  `panel.ts`'s `board-issue` action) to open straight to that issue's
   *  detail. Every distinct value re-opens the dialog, including the same
   *  id clicked twice — the panel's own row clicks otherwise own this. */
  openIssueId,
}: {
  projectId: string;
  openIssueId?: string | null;
}) {
  const [summary, setSummary] = useState<BoardSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [newOpen, setNewOpen] = useState(false);
  const [selectedIssueId, setSelectedIssueId] = useState<string | null>(null);

  useEffect(() => {
    if (openIssueId) setSelectedIssueId(openIssueId);
  }, [openIssueId]);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    getSummary(projectId)
      .then(setSummary)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, [projectId]);

  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    const onPing = () => load();
    window.addEventListener(BOARD_REFRESH_EVENT, onPing);
    return () => window.removeEventListener(BOARD_REFRESH_EVENT, onPing);
  }, [load]);

  const onCreated = (issue: Issue) => {
    void load();
    setSelectedIssueId(issue.id);
  };

  if (loading && !summary) return <Skeleton label={t('Loading the board')} count={3} height="24px" />;
  if (error && !summary) return <p className="fs-notice" data-tone="danger" role="alert">{error}</p>;
  if (!summary) return null;

  const rows = [...summary.in_progress, ...summary.ready];

  return (
    <div className="fs-board fs-board--compact" data-testid="board-compact">
      <div className="fs-board__toolbar">
        <span className="fs-board__key-chip" title={t('This project\'s board key')}>{t('Key: {key}', { key: summary.key })}</span>
        <span className="fs-board__toolbar-spacer" />
        <Button variant="ghost" size="sm" icon={Plus} label={t('New issue')} onClick={() => setNewOpen(true)} testId="board-compact-new-issue" />
      </div>
      {rows.length === 0 ? (
        <EmptyState
          headingLevel={3}
          icon={LayoutGrid}
          title={t('Nothing pending')}
          body={t('No ready or in-progress issue for this project yet.')}
          primaryAction={{ label: t('New issue'), onClick: () => setNewOpen(true) }}
        />
      ) : (
        <div className="fs-board__compact-list">
          {rows.map((issue) => (
            <IssueCard key={issue.id} issue={issue} draggable={false} onOpen={(i) => setSelectedIssueId(i.id)} />
          ))}
        </div>
      )}
      {summary.recent_done.length > 0 && (
        <details className="fs-board__compact-done">
          <summary>{t('Done recently')} ({summary.recent_done.length})</summary>
          <div className="fs-board__compact-list">
            {summary.recent_done.map((issue) => (
              <IssueCard key={issue.id} issue={issue} draggable={false} onOpen={(i) => setSelectedIssueId(i.id)} />
            ))}
          </div>
        </details>
      )}
      <NewIssueDialog open={newOpen} onOpenChange={setNewOpen} projectId={projectId} quick onCreated={onCreated} />
      <IssueDetail
        open={selectedIssueId !== null}
        onOpenChange={(o) => !o && setSelectedIssueId(null)}
        projectId={projectId}
        issueId={selectedIssueId}
        onChanged={() => load()}
        onDeleted={() => { load(); setSelectedIssueId(null); }}
      />
    </div>
  );
}
