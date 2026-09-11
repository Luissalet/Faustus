import { useMemo } from 'react';
import { Button, EmptyState, Skeleton } from '../../components';
import { relativeTime } from '../../adapters/home';
import { computeGraphLanes, parseRefs, type GitCommit, type GraphRow } from '../../adapters/git';
import { t } from '../../i18n';
import { GitCommit as GitCommitIcon } from 'lucide-react';

const LANE_W = 16;
const ROW_H = 40;
const DOT_R = 4;
const LANE_COLORS = 6;

function laneX(lane: number): number {
  return LANE_W / 2 + lane * LANE_W;
}

function laneColor(lane: number): string {
  return `var(--fs-sc-lane-${lane % LANE_COLORS})`;
}

/**
 * One row's lane graphic: a straight line for anything merely passing
 * through, the node's own line in and out, and a curve for any extra
 * parent (a merge) landing in a different lane — the mini "commit graph"
 * every VS Code-style Source Control view draws left of the message.
 */
function RowGraph({ row, width }: { row: GraphRow; width: number }) {
  const cy = ROW_H / 2;
  const x = laneX(row.lane);

  return (
    <svg width={width} height={ROW_H} viewBox={`0 0 ${width} ${ROW_H}`} aria-hidden="true" className="fs-sc__graph-svg" data-note="guard-ok: commit graph lanes are drawn data, not an icon">
      {row.passthrough.map((lane) => (
        <line key={`p-${lane}`} x1={laneX(lane)} y1={0} x2={laneX(lane)} y2={ROW_H} stroke={laneColor(lane)} strokeWidth={2} />
      ))}
      {row.continuesFromAbove && <line x1={x} y1={0} x2={x} y2={cy} stroke={laneColor(row.lane)} strokeWidth={2} />}
      {row.parentLanes.map((p) =>
        p.lane === row.lane ? (
          <line key={p.sha} x1={x} y1={cy} x2={x} y2={ROW_H} stroke={laneColor(row.lane)} strokeWidth={2} />
        ) : (
          <path
            key={p.sha}
            d={`M ${x} ${cy} C ${x} ${ROW_H}, ${laneX(p.lane)} ${cy}, ${laneX(p.lane)} ${ROW_H}`}
            fill="none"
            stroke={laneColor(p.lane)}
            strokeWidth={2}
          />
        ),
      )}
      <circle cx={x} cy={cy} r={DOT_R} fill={laneColor(row.lane)} />
    </svg>
  );
}

export function CommitGraph({
  commits,
  loading,
  error,
  selectedSha,
  onSelect,
  hasMore,
  loadingMore,
  onLoadMore,
}: {
  commits: GitCommit[];
  loading: boolean;
  error: string | null;
  selectedSha: string | null;
  onSelect: (commit: GitCommit) => void;
  hasMore: boolean;
  loadingMore: boolean;
  onLoadMore: () => void;
}) {
  const rows = useMemo(() => computeGraphLanes(commits), [commits]);
  const width = useMemo(() => Math.max(1, ...rows.map((r) => r.laneCount)) * LANE_W + LANE_W / 2, [rows]);
  const rowBySha = useMemo(() => new Map(rows.map((r) => [r.sha, r])), [rows]);

  if (loading) {
    return <Skeleton label={t('Loading commit graph')} count={6} height={`${ROW_H}px`} />;
  }

  if (error) {
    return (
      <p className="fs-notice" data-tone="danger" role="alert" data-testid="graph-error">
        {error}
      </p>
    );
  }

  if (commits.length === 0) {
    return <EmptyState headingLevel={3} icon={GitCommitIcon} title={t('No commits yet')} body={t('This branch has no history to show.')} />;
  }

  return (
    <div className="fs-sc__graph">
      <div className="fs-sc__graph-list" role="list" aria-label={t('Commit history')}>
        {commits.map((commit) => {
          const row = rowBySha.get(commit.sha);
          const refs = parseRefs(commit.refs);
          return (
            <button
              key={commit.sha}
              type="button"
              role="listitem"
              className="fs-sc__graph-row"
              aria-current={commit.sha === selectedSha ? 'true' : undefined}
              onClick={() => onSelect(commit)}
              data-testid="commit-row"
            >
              {row && <RowGraph row={row} width={width} />}
              <span className="fs-sc__graph-main">
                <span className="fs-sc__graph-message-row">
                  <span className="fs-sc__graph-message" title={commit.message}>
                    {commit.message}
                  </span>
                  {refs.length > 0 && (
                    <span className="fs-sc__graph-refs">
                      {refs.map((chip, i) => (
                        <span key={`${chip.kind}-${chip.label}-${i}`} className="fs-sc__ref-chip" data-kind={chip.kind}>
                          {chip.label}
                        </span>
                      ))}
                    </span>
                  )}
                </span>
                <span className="fs-sc__graph-meta">
                  <code>{commit.short}</code>
                  <span>{commit.author}</span>
                  <span title={commit.date}>{relativeTime(commit.date)}</span>
                </span>
              </span>
            </button>
          );
        })}
      </div>
      {hasMore && (
        <div className="fs-sc__graph-more">
          <Button variant="ghost" size="sm" label={loadingMore ? t('Loading…') : t('Load more commits')} loading={loadingMore} onClick={onLoadMore} testId="commit-load-more" />
        </div>
      )}
    </div>
  );
}
