import { useEffect, useState } from 'react';
import { Link } from 'react-router';
import { Check, Clock, ExternalLink, GitCommit, Link2, MessageSquare, Trash2, Unlink } from 'lucide-react';
import { Button, Dialog, IconButton, Skeleton } from '../../components';
import { Rich } from '../rich';
import {
  addComment,
  addLink,
  allowedNextStatuses,
  deleteIssue,
  getIssue,
  ISSUE_LINK_KINDS,
  ISSUE_PRIORITIES,
  ISSUE_TYPES,
  LINK_KIND_LABEL,
  removeLink,
  STATUS_LABEL,
  updateIssue,
  type Issue,
  type IssueLinkKind,
  type IssuePriority,
  type IssueStatus,
  type IssueType,
} from '../../adapters/board';
import { relativeTime } from '../../adapters/home';
import { t } from '../../i18n';
import { typeLabel, priorityLabel } from './IssueCard';

function statusLabel(status: IssueStatus): string {
  return t(STATUS_LABEL[status]);
}

function linkKindLabel(kind: IssueLinkKind): string {
  return t(LINK_KIND_LABEL[kind]);
}

const EVENT_LABEL: Record<string, string> = {
  status_change: 'Status changed',
  comment: 'Commented',
  link_added: 'Link added',
  commit_linked: 'Linked to a commit',
  session_linked: 'Linked to a session',
  claimed: 'Claimed',
  edited: 'Edited',
};

function eventLabel(kind: string): string {
  return EVENT_LABEL[kind] ? t(EVENT_LABEL[kind]) : kind;
}

/**
 * Lote 93 — an issue's own view: body (`<Rich>`), editable status/priority/
 * assignee, comments, links and refs (commit → Source control, session →
 * the chat), and a folded event history. Reused by the Board tab's card
 * click and the chat panel's compact list, both of which only need to hand
 * it `projectId` + `issueId`.
 */
export function IssueDetail({
  open,
  onOpenChange,
  projectId,
  issueId,
  onChanged,
  onDeleted,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  projectId: string;
  issueId: string | null;
  /** Fired after any successful mutation (status, comment, link, ref…) so
   *  the caller's own list (kanban column, compact panel) can refresh. */
  onChanged: (issue: Issue) => void;
  onDeleted: (issueId: string) => void;
}) {
  const [issue, setIssue] = useState<Issue | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [comment, setComment] = useState('');
  const [linkKind, setLinkKind] = useState<IssueLinkKind>('blocks');
  const [linkTarget, setLinkTarget] = useState('');
  const [eventsOpen, setEventsOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  useEffect(() => {
    if (!open || !issueId) return;
    setLoading(true);
    setError(null);
    getIssue(projectId, issueId)
      .then((res) => setIssue(res.issue))
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, [open, projectId, issueId]);

  const patch = async (input: Parameters<typeof updateIssue>[2]) => {
    if (!issue) return;
    setBusy(true);
    setError(null);
    try {
      const res = await updateIssue(projectId, issue.id, input);
      setIssue(res.issue);
      onChanged(res.issue);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const submitComment = async () => {
    if (!issue || !comment.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await addComment(projectId, issue.id, comment.trim());
      const res = await getIssue(projectId, issue.id);
      setIssue(res.issue);
      setComment('');
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const submitLink = async () => {
    if (!issue || !linkTarget.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await addLink(projectId, issue.id, linkKind, linkTarget.trim());
      const res = await getIssue(projectId, issue.id);
      setIssue(res.issue);
      onChanged(res.issue);
      setLinkTarget('');
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const removeLinkRow = async (linkId: string) => {
    if (!issue) return;
    setBusy(true);
    setError(null);
    try {
      await removeLink(projectId, issue.id, linkId);
      const res = await getIssue(projectId, issue.id);
      setIssue(res.issue);
      onChanged(res.issue);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const doDelete = async () => {
    if (!issue) return;
    setBusy(true);
    try {
      await deleteIssue(projectId, issue.id);
      onDeleted(issue.id);
      onOpenChange(false);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={issue ? `${issue.id} — ${issue.title}` : issueId ?? t('Issue')}
      testId="board-issue-detail"
      className="fs-issue-detail-dialog"
      footer={
        confirmDelete ? (
          <>
            <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setConfirmDelete(false)} />
            <Button variant="danger-solid" size="sm" icon={Trash2} label={t('Delete issue')} loading={busy} onClick={() => void doDelete()} testId="board-issue-delete-confirm" />
          </>
        ) : (
          <Button variant="ghost" size="sm" icon={Trash2} label={t('Delete')} onClick={() => setConfirmDelete(true)} testId="board-issue-delete" />
        )
      }
    >
      {loading && <Skeleton label={t('Loading the issue')} count={5} height="18px" />}
      {!loading && error && !issue && <p className="fs-notice" data-tone="danger" role="alert">{error}</p>}
      {!loading && issue && (
        <div className="fs-issue-detail">
          <div className="fs-issue-detail__fields">
            <label className="fs-field-label">
              {t('Status')}
              <select
                className="fs-field"
                value={issue.status}
                disabled={busy}
                onChange={(e) => void patch({ status: e.target.value as IssueStatus })}
                data-testid="board-issue-status"
              >
                {allowedNextStatuses(issue.status).map((s) => (
                  <option key={s} value={s}>{statusLabel(s)}</option>
                ))}
              </select>
            </label>
            <label className="fs-field-label">
              {t('Priority')}
              <select className="fs-field" value={issue.priority} disabled={busy} onChange={(e) => void patch({ priority: e.target.value as IssuePriority })}>
                {ISSUE_PRIORITIES.map((p) => (
                  <option key={p} value={p}>{priorityLabel(p)}</option>
                ))}
              </select>
            </label>
            <label className="fs-field-label">
              {t('Type')}
              <select className="fs-field" value={issue.type} disabled={busy} onChange={(e) => void patch({ type: e.target.value as IssueType })}>
                {ISSUE_TYPES.map((ty) => (
                  <option key={ty} value={ty}>{typeLabel(ty)}</option>
                ))}
              </select>
            </label>
            <label className="fs-field-label">
              {t('Assignee')}
              <input
                className="fs-field"
                value={issue.assignee ?? ''}
                disabled={busy}
                placeholder={t('user, agent, or a name')}
                onBlur={(e) => {
                  const v = e.target.value.trim();
                  if (v !== (issue.assignee ?? '')) void patch({ assignee: v || null });
                }}
                onChange={(e) => setIssue({ ...issue, assignee: e.target.value })}
              />
            </label>
          </div>

          {issue.body_md ? (
            <div className="fs-issue-detail__body"><Rich text={issue.body_md} /></div>
          ) : (
            <p className="fs-muted">{t('No description.')}</p>
          )}

          {issue.labels.length > 0 && (
            <p className="fs-issue-detail__labels">
              {issue.labels.map((l) => <span key={l} className="fs-issue-card__label">{l}</span>)}
            </p>
          )}

          <section aria-label={t('Links')}>
            <h3 className="fs-panel__label"><Link2 size={12} aria-hidden="true" /> {t('Links')}</h3>
            {issue.links.length === 0 && <p className="fs-muted">{t('No links yet.')}</p>}
            <ul className="fs-issue-detail__list">
              {issue.links.map((l) => (
                <li key={l.id}>
                  <span>{linkKindLabel(l.kind)} <code>{l.target_issue_id}</code></span>
                  <IconButton icon={Unlink} label={t('Remove link')} size="sm" onClick={() => void removeLinkRow(l.id)} />
                </li>
              ))}
            </ul>
            <form className="fs-inline" onSubmit={(e) => { e.preventDefault(); void submitLink(); }}>
              <select className="fs-field" value={linkKind} onChange={(e) => setLinkKind(e.target.value as IssueLinkKind)} aria-label={t('Link kind')}>
                {ISSUE_LINK_KINDS.map((k) => (
                  <option key={k} value={k}>{linkKindLabel(k)}</option>
                ))}
              </select>
              <input className="fs-field" value={linkTarget} onChange={(e) => setLinkTarget(e.target.value)} placeholder="FAU-9" aria-label={t('Target issue id')} />
              <Button type="submit" size="sm" label={t('Link#issue_link_verb')} disabled={!linkTarget.trim() || busy} />
            </form>
          </section>

          {issue.refs.length > 0 && (
            <section aria-label={t('References')}>
              <h3 className="fs-panel__label"><GitCommit size={12} aria-hidden="true" /> {t('References')}</h3>
              <ul className="fs-issue-detail__list">
                {issue.refs.map((r) => (
                  <li key={r.id}>
                    {r.kind === 'session' ? (
                      <Link className="fs-link" to={`/studio?s=${encodeURIComponent(r.value)}`}>
                        <ExternalLink size={12} aria-hidden="true" /> {r.label || t('Session {id}', { id: r.value.slice(0, 8) })}
                      </Link>
                    ) : r.kind === 'commit' ? (
                      <Link className="fs-link" to={`/source-control?project=${encodeURIComponent(projectId)}`} title={r.value}>
                        <GitCommit size={12} aria-hidden="true" /> {r.label || r.value.slice(0, 7)}
                      </Link>
                    ) : (
                      <span>{r.label || r.value}</span>
                    )}
                  </li>
                ))}
              </ul>
            </section>
          )}

          <section aria-label={t('Comments')}>
            <h3 className="fs-panel__label"><MessageSquare size={12} aria-hidden="true" /> {t('Comments')}</h3>
            <ul className="fs-issue-detail__comments">
              {issue.comments.map((c) => (
                <li key={c.id}>
                  <span className="fs-issue-detail__comment-head">
                    <strong>{c.author}</strong> <span className="fs-muted">{relativeTime(c.created_at)}</span>
                  </span>
                  <Rich text={c.body_md} />
                </li>
              ))}
              {issue.comments.length === 0 && <li className="fs-muted">{t('No comments yet.')}</li>}
            </ul>
            <form className="fs-inline" onSubmit={(e) => { e.preventDefault(); void submitComment(); }}>
              <input className="fs-field fs-pj__grow" value={comment} onChange={(e) => setComment(e.target.value)} placeholder={t('Write a comment…')} data-testid="board-issue-comment-input" />
              <Button type="submit" size="sm" icon={Check} label={t('Comment')} disabled={!comment.trim() || busy} testId="board-issue-comment-submit" />
            </form>
          </section>

          {issue.events.length > 0 && (
            <details className="fs-issue-detail__events" open={eventsOpen} onToggle={(e) => setEventsOpen(e.currentTarget.open)}>
              <summary><Clock size={12} aria-hidden="true" /> {t('History')} ({issue.events.length})</summary>
              <ul className="fs-issue-detail__list">
                {issue.events.map((ev) => (
                  <li key={ev.id}>
                    <span>{eventLabel(ev.kind)}</span>
                    <span className="fs-muted">{ev.actor} · {relativeTime(ev.created_at)}</span>
                  </li>
                ))}
              </ul>
            </details>
          )}

          {error && <p className="fs-notice" data-tone="danger" role="alert">{error}</p>}
        </div>
      )}
    </Dialog>
  );
}
