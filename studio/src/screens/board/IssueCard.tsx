import { Bot, Bug, Lightbulb, Sparkles, User, Wrench, type LucideIcon } from 'lucide-react';
import { ListTodo } from 'lucide-react';
import type { DragEvent } from 'react';
import type { IssueCompact, IssuePriority, IssueType } from '../../adapters/board';
import { PRIORITY_LABEL, TYPE_LABEL } from '../../adapters/board';
import { t } from '../../i18n';

/**
 * Lote 93 — one issue, drawn identically wherever it appears (a kanban
 * card, a dense table row, the chat panel's compact list): id, type icon,
 * priority chip, title, assignee, labels. `IssueCard` is the kanban shape;
 * `IssueRow` is the same information as one dense line for the Table view
 * and the compact chat list.
 */

export const TYPE_ICON: Record<IssueType, LucideIcon> = {
  bug: Bug,
  idea: Lightbulb,
  feature: Sparkles,
  task: ListTodo,
  chore: Wrench,
};

export function typeLabel(type: IssueType): string {
  return t(TYPE_LABEL[type]);
}

export function priorityLabel(priority: IssuePriority): string {
  return t(PRIORITY_LABEL[priority]);
}

export function AssigneeBadge({ assignee }: { assignee: string | null }) {
  if (!assignee) return null;
  const Icon = assignee === 'agent' ? Bot : User;
  return (
    <span className="fs-issue-card__assignee" title={t('Assigned to {who}', { who: assignee })}>
      <Icon size={11} aria-hidden="true" /> {assignee}
    </span>
  );
}

export function IssueCard({
  issue,
  onOpen,
  draggable = true,
  onDragStart,
}: {
  issue: IssueCompact;
  onOpen: (issue: IssueCompact) => void;
  draggable?: boolean;
  onDragStart?: (issue: IssueCompact, e: DragEvent) => void;
}) {
  const Icon = TYPE_ICON[issue.type];
  return (
    <button
      type="button"
      className="fs-issue-card"
      data-testid="issue-card"
      data-priority={issue.priority}
      draggable={draggable}
      onDragStart={(e) => {
        e.dataTransfer.setData('text/plain', issue.id);
        e.dataTransfer.effectAllowed = 'move';
        onDragStart?.(issue, e);
      }}
      onClick={() => onOpen(issue)}
    >
      <span className="fs-issue-card__head" title={typeLabel(issue.type)}>
        <Icon size={13} aria-hidden="true" />
        <code className="fs-issue-card__id">{issue.id}</code>
        <span className="fs-issue-card__prio" data-prio={issue.priority}>{issue.priority}</span>
      </span>
      <span className="fs-issue-card__title">{issue.title}</span>
      {(issue.assignee || issue.labels.length > 0 || issue.blocked_by.length > 0) && (
        <span className="fs-issue-card__foot">
          <AssigneeBadge assignee={issue.assignee} />
          {issue.labels.slice(0, 3).map((l) => (
            <span key={l} className="fs-issue-card__label">{l}</span>
          ))}
          {issue.blocked_by.length > 0 && (
            <span className="fs-issue-card__blocked" title={t('Blocked by {ids}', { ids: issue.blocked_by.join(', ') })}>
              {t('blocked by {ids}', { ids: issue.blocked_by.join(', ') })}
            </span>
          )}
        </span>
      )}
    </button>
  );
}

/** The Table view's one line — same fields, denser, sortable by column. */
export function IssueRow({ issue, onOpen }: { issue: IssueCompact; onOpen: (issue: IssueCompact) => void }) {
  const Icon = TYPE_ICON[issue.type];
  return (
    <tr className="fs-issue-row" data-testid="issue-row" onClick={() => onOpen(issue)}>
      <td><code>{issue.id}</code></td>
      <td><Icon size={13} aria-hidden="true" /> {typeLabel(issue.type)}</td>
      <td className="fs-issue-row__title">{issue.title}</td>
      <td>{issue.priority}</td>
      <td>{issue.status}</td>
      <td>{issue.assignee || '—'}</td>
      <td className="fs-muted">{issue.labels.join(', ')}</td>
    </tr>
  );
}
