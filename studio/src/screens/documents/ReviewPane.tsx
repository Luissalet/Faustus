import { AlertTriangle, Check, CheckCircle2, ChevronDown, ChevronRight, Link2, MessageSquarePlus, RefreshCw, Send, Trash2, Unlink } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { Button, EmptyState, IconButton, Skeleton, Toast } from '../../components';
import { ApiError } from '../../adapters/api';
import {
  acceptDocComment, createDocComment, deleteDocComment, listDocBacklinks, listDocComments, listDocLinks,
  updateDocComment, type DocComment, type DocLink,
} from '../../adapters/documents';
import { currentText, findOccurrences, sendComposerContext, useDocSession } from '../../lib/docSession';
import { t, tn } from '../../i18n';
import '../documents.css';

/**
 * The contextual review pane (CMP-01/02/03, W2-A1): everything about a
 * document's ANCHORED COMMENTS (`/api/documents/{id}/comments` —
 * `src/document_comments.py`, W1-E) that is not just "edit the text" —
 * open a thread, see the proposal it carries in context, accept it at its
 * (freshly relocated) anchor, resolve without applying, or hand a batch of
 * comments to the agent as a task the human still has to send. Plus a
 * compact view of this document's wiki-links (`src/document_links.py`).
 *
 * Used two ways: lazily imported by `Studio.tsx`'s `review` layout
 * (CMP-01-layout, W2-A2) with a live `docId`, and — until that lands, or
 * whenever no document is open — safe to render with no props at all.
 */
export interface ReviewPaneProps {
  docId?: string | null;
  docTitle?: string;
}

type Filter = 'open' | 'all';

function Diff({ find, replace }: { find: string; replace: string }) {
  return (
    <pre className="fs-panel__diff fs-review__diff">
      <span className="fs-diff-del">− {find}</span>
      <span className="fs-diff-add">+ {replace}</span>
    </pre>
  );
}

function StateBadge({ state }: { state: DocComment['state'] }) {
  if (state === 'resolved') return <span className="fs-review__badge" data-tone="ok">{t('resolved')}</span>;
  if (state === 'orphan') return <span className="fs-review__badge" data-tone="warn">{t('lost anchor — relocate by hand')}</span>;
  return <span className="fs-review__badge">{t('open')}</span>;
}

function CommentCard({ comment, checked, onCheck, onNotice, onChanged }: {
  comment: DocComment;
  checked: boolean;
  onCheck: (v: boolean) => void;
  onNotice: (text: string, tone?: 'info' | 'warning' | 'danger') => void;
  onChanged: (next: DocComment | 'deleted') => void;
}) {
  const [busy, setBusy] = useState(false);

  const resolve = async () => {
    setBusy(true);
    try {
      onChanged(await updateDocComment(comment.documentId, comment.id, { state: 'resolved' }));
    } catch (e) {
      onNotice((e as Error).message, 'danger');
    } finally {
      setBusy(false);
    }
  };

  const accept = async () => {
    setBusy(true);
    try {
      const { comment: next } = await acceptDocComment(comment.documentId, comment.id);
      onChanged(next);
      onNotice(t('Proposal applied.'));
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) onNotice(t('The document changed since this comment was anchored; it could not be applied automatically.'), 'warning');
      else onNotice((e as Error).message, 'danger');
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    setBusy(true);
    try {
      await deleteDocComment(comment.documentId, comment.id);
      onChanged('deleted');
    } catch (e) {
      onNotice((e as Error).message, 'danger');
      setBusy(false);
    }
  };

  return (
    <li className="fs-review__comment" data-state={comment.state} data-testid="review-comment">
      <div className="fs-review__comment-head">
        <input type="checkbox" checked={checked} onChange={(e) => onCheck(e.target.checked)} aria-label={t('Select this comment')} disabled={comment.state === 'resolved'} />
        <StateBadge state={comment.state} />
        <span className="fs-sa__muted">{comment.author === 'model' ? t('agent') : t('you')}</span>
      </div>
      <blockquote className="fs-review__quote">“{comment.quote.length > 220 ? `${comment.quote.slice(0, 220)}…` : comment.quote}”</blockquote>
      {comment.body && <p className="fs-review__body">{comment.body}</p>}
      {comment.proposal && <Diff find={comment.proposal.find} replace={comment.proposal.replace} />}
      {comment.state === 'orphan' && (
        <p className="fs-notice" data-tone="warning" role="status">
          {t('The quoted text no longer occurs unambiguously in the document — this comment was not silently reattached elsewhere. Find the passage and either resolve or delete it by hand.')}
        </p>
      )}
      <div className="fs-panel__row">
        {comment.proposal && comment.state !== 'resolved' && <Button size="sm" variant="primary" icon={Check} label={t('Accept proposal')} loading={busy} disabled={comment.state === 'orphan'} onClick={() => void accept()} testId="review-accept" />}
        {comment.state === 'open' && <Button size="sm" icon={CheckCircle2} label={t('Resolve')} loading={busy} onClick={() => void resolve()} />}
        <IconButton icon={Trash2} label={t('Delete')} size="sm" disabled={busy} onClick={() => void remove()} />
      </div>
    </li>
  );
}

function LinkRow({ link }: { link: DocLink }) {
  const label = link.alias || link.targetValue;
  return (
    <li className="fs-review__link" data-status={link.status}>
      <Link2 size={12} aria-hidden="true" />
      <span title={link.raw}>{label}</span>
      <span className="fs-review__badge" data-tone={link.status === 'resolved' ? 'ok' : link.status === 'broken' || link.status === 'rejected' ? 'warn' : undefined}>
        {link.status === 'resolved' ? t('resolved') : link.status === 'ambiguous' ? t('ambiguous — {n} matches', { n: link.candidates.length }) : link.status === 'rejected' ? t('rejected') : t('broken')}
      </span>
    </li>
  );
}

export function ReviewPane({ docId, docTitle }: ReviewPaneProps) {
  const session = useDocSession(docId ?? null);
  const [comments, setComments] = useState<DocComment[] | null>(null);
  const [links, setLinks] = useState<DocLink[] | null>(null);
  const [backlinks, setBacklinks] = useState<DocLink[] | null>(null);
  const [linksOpen, setLinksOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>('open');
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [notice, setNotice] = useState<{ text: string; tone: 'ok' | 'warn' } | null>(null);
  const [addingFromSelection, setAddingFromSelection] = useState(false);

  const say = useCallback((text: string, tone: 'info' | 'warning' | 'danger' = 'info') => {
    setNotice({ text, tone: tone === 'danger' ? 'warn' : tone === 'warning' ? 'warn' : 'ok' });
    window.setTimeout(() => setNotice(null), tone === 'info' ? 3500 : 6000);
  }, []);

  const load = useCallback(() => {
    if (!docId) return;
    setError(null);
    listDocComments(docId).then(setComments).catch((e: Error) => setError(e.message));
  }, [docId]);

  useEffect(() => {
    setComments(null);
    setLinks(null);
    setBacklinks(null);
    setSelected(new Set());
    setLinksOpen(false);
    load();
  }, [docId, load]);

  const loadLinks = () => {
    if (!docId || links) { setLinksOpen((v) => !v); return; }
    setLinksOpen(true);
    Promise.all([listDocLinks(docId), listDocBacklinks(docId)])
      .then(([l, b]) => { setLinks(l); setBacklinks(b); })
      .catch((e: Error) => say(e.message, 'danger'));
  };

  if (!docId) {
    return (
      <div className="fs-review fs-review--empty">
        <EmptyState icon={MessageSquarePlus} title={t('No document to review')} body={t('Open a document to see comments anchored on it, and to review what it links to.')} />
      </div>
    );
  }
  if (error) return <p className="fs-notice" data-tone="danger" role="alert">{error}</p>;
  if (!comments) return <div className="fs-review"><Skeleton label={t('Loading comments')} count={4} height="18px" /></div>;

  const visible = filter === 'open' ? comments.filter((c) => c.state !== 'resolved') : comments;
  const openCount = comments.filter((c) => c.state === 'open').length;
  const orphanCount = comments.filter((c) => c.state === 'orphan').length;

  const applyChange = (id: string, next: DocComment | 'deleted') => {
    setComments((cur) => {
      if (!cur) return cur;
      if (next === 'deleted') return cur.filter((c) => c.id !== id);
      return cur.map((c) => (c.id === id ? next : c));
    });
    setSelected((cur) => { const n = new Set(cur); n.delete(id); return n; });
  };

  const selectedComments = comments.filter((c) => selected.has(c.id));

  const sendSelectedAsTask = () => {
    if (!selectedComments.length) return;
    // A drafted context for the NEXT message, never an executed change —
    // the human still reviews and sends it (CMP-03, §3.2).
    sendComposerContext({
      doc: { id: docId, title: docTitle || t('Document') },
      ranges: [],
      action: 'comments',
      items: selectedComments.map((c) => ({ quote: c.quote, note: c.body || undefined })),
    });
    say(tn(selectedComments.length, '{n} comment added as context for your next message.', '{n} comments added as context for your next message.'));
    setSelected(new Set());
  };

  // Seen live: the button created an EMPTY comment straight away. A comment
  // is a note on a passage — ask for the note first, one inline field.
  const [draftBody, setDraftBody] = useState<string | null>(null);

  const addFromSelection = async () => {
    if (!session || !session.selection.length) return;
    const range = session.selection[0];
    const text = currentText(session);
    const quote = text.slice(range.start, range.end);
    if (!quote.trim()) return;
    setAddingFromSelection(true);
    try {
      // Pass explicit context (same window `document_comments.py` itself
      // uses) so a quote that repeats elsewhere still anchors to THIS
      // occurrence instead of failing `quote_ambiguous`.
      const occ = findOccurrences(text, quote).find((o) => o.start === range.start) ?? findOccurrences(text, quote)[0];
      const created = await createDocComment(docId, { quote, body: (draftBody ?? '').trim(), beforeCtx: occ?.before, afterCtx: occ?.after });
      setComments((cur) => (cur ? [...cur, created] : [created]));
      setDraftBody(null);
      say(t('Comment added.'));
    } catch (e) {
      say((e as Error).message, 'danger');
    } finally {
      setAddingFromSelection(false);
    }
  };

  return (
    <div className="fs-review" data-testid="review-pane">
      <header className="fs-review__head">
        <h3>{t('Review')}{docTitle ? ` — ${docTitle}` : ''}</h3>
        <IconButton icon={RefreshCw} label={t('Refresh')} size="sm" onClick={load} />
      </header>

      <div className="fs-review__summary">
        <span>{tn(openCount, '{n} open comment', '{n} open comments')}</span>
        {orphanCount > 0 && <span className="fs-review__badge" data-tone="warn">{tn(orphanCount, '{n} lost anchor', '{n} lost anchors')}</span>}
      </div>

      {session && session.selection.length > 0 && draftBody === null && (
        <div className="fs-panel__row">
          <Button size="sm" icon={MessageSquarePlus} label={t('Comment on the current selection')} onClick={() => setDraftBody('')} data-testid="review-comment-start" />
        </div>
      )}
      {session && session.selection.length > 0 && draftBody !== null && (
        <div className="fs-review__draft" data-testid="review-comment-draft">
          <textarea
            className="fs-panel__editor"
            rows={3}
            value={draftBody}
            placeholder={t('What about this passage?')}
            aria-label={t('Comment text')}
            onChange={(e) => setDraftBody(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) void addFromSelection(); if (e.key === 'Escape') setDraftBody(null); }}
          />
          <div className="fs-panel__row">
            <Button size="sm" label={t('Cancel')} onClick={() => setDraftBody(null)} />
            <Button size="sm" variant="primary" icon={MessageSquarePlus} label={t('Add comment')} loading={addingFromSelection} disabled={!draftBody.trim()} onClick={() => void addFromSelection()} />
          </div>
        </div>
      )}

      <div className="fs-seg" role="radiogroup" aria-label={t('Filter')}>
        <button type="button" role="radio" aria-checked={filter === 'open'} onClick={() => setFilter('open')}>{t('Open')}</button>
        <button type="button" role="radio" aria-checked={filter === 'all'} onClick={() => setFilter('all')}>{t('All')}</button>
      </div>

      {visible.length === 0 ? (
        <p className="fs-studio__hint">{t('No comments to show.')}</p>
      ) : (
        <ul className="fs-review__list">
          {visible.map((c) => (
            <CommentCard key={c.id} comment={c} checked={selected.has(c.id)} onNotice={say} onChanged={(next) => applyChange(c.id, next)} onCheck={(v) => setSelected((cur) => { const n = new Set(cur); if (v) n.add(c.id); else n.delete(c.id); return n; })} />
          ))}
        </ul>
      )}

      {selectedComments.length > 0 && (
        <div className="fs-panel__row fs-review__send">
          <Button size="sm" variant="primary" icon={Send} label={tn(selectedComments.length, 'Send {n} comment to the agent as a task', 'Send {n} comments to the agent as a task')} onClick={sendSelectedAsTask} testId="review-send-task" />
        </div>
      )}

      <section className="fs-review__links">
        <button type="button" className="fs-review__links-toggle" onClick={loadLinks} aria-expanded={linksOpen}>
          {linksOpen ? <ChevronDown size={14} aria-hidden="true" /> : <ChevronRight size={14} aria-hidden="true" />}
          {t('Links')}
        </button>
        {linksOpen && (
          <>
            {!links ? (
              <Skeleton label={t('Loading links')} count={2} height="16px" />
            ) : (
              <>
                <h4>{t('This document links to')}</h4>
                {links.length ? <ul className="fs-review__link-list">{links.map((l, i) => <LinkRow key={i} link={l} />)}</ul> : <p className="fs-studio__hint fs-review__no-links"><Unlink size={12} aria-hidden="true" /> {t('No links.')}</p>}
                <h4>{t('Documents that link here')}</h4>
                {backlinks && backlinks.length ? <ul className="fs-review__link-list">{backlinks.map((l, i) => <LinkRow key={i} link={l} />)}</ul> : <p className="fs-studio__hint fs-review__no-links"><Unlink size={12} aria-hidden="true" /> {t('Nothing references this document yet.')}</p>}
              </>
            )}
          </>
        )}
      </section>

      {notice && (
        <Toast>
          {notice.tone === 'warn' ? <AlertTriangle size={12} aria-hidden="true" /> : <Check size={12} aria-hidden="true" />} {notice.text}
        </Toast>
      )}
    </div>
  );
}

export default ReviewPane;
