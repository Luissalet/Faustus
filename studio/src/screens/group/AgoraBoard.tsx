import { ChevronDown, ExternalLink, RefreshCw } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { IconButton, Skeleton } from '../../components';
import { agoraBoard, type AgoraBoard, type BoardColumn, type BoardTask } from '../../adapters/group';
import { hueIndex, initials } from '../../lib/mail';
import { t } from '../../i18n';

/**
 * The Agora task board of one space, drawn like the Hub's: what is open,
 * what is moving, what waits for a reviewer and what is finished. The same
 * board appears in the project's Objectives tab (full) and beside a room's
 * chat (compact), so whatever the members put on it is visible in both.
 */

const COLUMNS: { id: BoardColumn; label: string }[] = [
  { id: 'open', label: 'Open' },
  { id: 'in_progress', label: 'In progress' },
  { id: 'review', label: 'In review' },
  { id: 'done', label: 'Done' },
];

const STATUS_LABEL: Record<string, string> = {
  open: 'Open', claimed: 'Claimed', in_progress: 'In progress', blocked: 'Blocked', changes: 'Changes requested',
  review: 'In review', approved: 'Approved', done: 'Done',
};

function Card({ task }: { task: BoardTask }) {
  const [open, setOpen] = useState(false);
  const who = task.owner || task.created_by || '';
  return (
    <li className="fs-board-agora__card" data-status={task.status}>
      <button type="button" className="fs-board-agora__card-head" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        <code>#{task.id}</code>
        <span className="fs-board-agora__title">{task.title}</span>
      </button>
      <div className="fs-board-agora__meta">
        {who && (
          <span className="fs-board-agora__who" title={task.owner ? t('Owner') : t('Proposed by')}>
            <span className="fs-avatar" data-hue={hueIndex(who)} data-size="sm" aria-hidden="true">{initials(who)}</span>
            {who}
          </span>
        )}
        <span className="fs-board-agora__chip" data-status={task.status}>{t(STATUS_LABEL[task.status] ?? task.status)}</span>
        {typeof task.priority === 'number' && <span className="fs-board-agora__chip">P{task.priority}</span>}
        {task.kind && <span className="fs-board-agora__chip">{task.kind}</span>}
        {task.reviewer && <span className="fs-board-agora__chip">{t('Reviewer: {name}', { name: task.reviewer })}</span>}
      </div>
      {open && task.body && <p className="fs-board-agora__body">{task.body}</p>}
    </li>
  );
}

export function AgoraBoardView({ spaceId, variant = 'full', title }: { spaceId: string; variant?: 'full' | 'compact'; title?: string }) {
  const [board, setBoard] = useState<AgoraBoard | null>(null);
  const [error, setError] = useState('');
  const [folded, setFolded] = useState<Record<string, boolean>>({ done: variant === 'compact' });
  const [tick, setTick] = useState(0);
  const refresh = useCallback(() => setTick((n) => n + 1), []);

  useEffect(() => {
    const controller = new AbortController();
    const load = () => agoraBoard(spaceId, controller.signal).then((next) => {
      if (!controller.signal.aborted) { setBoard(next); setError(''); }
    }).catch((failure: Error) => { if (!controller.signal.aborted) setError(failure.message); });
    void load();
    // Agents move cards while the person watches: keep it live.
    const timer = window.setInterval(load, variant === 'compact' ? 6000 : 8000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [spaceId, variant, tick]);

  const total = (column: BoardColumn) => (column === 'done' ? board?.done_total ?? 0 : board?.columns[column].length ?? 0);
  return (
    <section className="fs-board-agora" data-variant={variant} data-testid="agora-board" aria-label={t('Agora tasks')}>
      <header className="fs-board-agora__head">
        <h3>{title ?? t('Agora tasks')}</h3>
        <span className="fs-board-agora__actions">
          <IconButton icon={RefreshCw} label={t('Refresh')} size="sm" onClick={refresh} />
          <a className="fs-board-agora__hub" href={'/api/hoard/agora/open?space_id=' + encodeURIComponent(spaceId)} target="_blank" rel="noopener" title={t('Open this board in the Hub')}>
            <ExternalLink size={13} aria-hidden="true" /> {variant === 'full' ? t('Open in the Hub') : null}
          </a>
        </span>
      </header>
      {error && !board && <p className="fs-notice" data-tone="warning" role="alert">{error}</p>}
      {!board && !error && <Skeleton label={t('Loading the board')} count={3} height="56px" />}
      {board && (
        <div className="fs-board-agora__columns">
          {COLUMNS.map((column) => {
            const rows = board.columns[column.id];
            const hidden = Boolean(folded[column.id]);
            return (
              <div key={column.id} className="fs-board-agora__column" data-column={column.id}>
                <button type="button" className="fs-board-agora__column-head" aria-expanded={!hidden}
                  onClick={() => setFolded((prev) => ({ ...prev, [column.id]: !prev[column.id] }))}>
                  <span>{t(column.label)}</span>
                  <b>{total(column.id)}</b>
                  {variant === 'compact' && <ChevronDown size={13} aria-hidden="true" data-open={!hidden || undefined} />}
                </button>
                {!(variant === 'compact' && hidden) && (
                  rows.length ? (
                    <ul className="fs-board-agora__cards">{rows.map((task) => <Card key={task.id} task={task} />)}</ul>
                  ) : <p className="fs-board-agora__empty">{t('Nothing here.')}</p>
                )}
                {column.id === 'done' && board.done_total > rows.length && !(variant === 'compact' && hidden) && (
                  <p className="fs-board-agora__empty">{t('Showing the {n} most recent of {total}.', { n: rows.length, total: board.done_total })}</p>
                )}
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}
