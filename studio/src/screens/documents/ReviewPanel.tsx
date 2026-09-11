import { AlertTriangle, Check, CheckCircle2, Circle, X, XCircle } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { Button, IconButton, Skeleton } from '../../components';
import { ApiError } from '../../adapters/api';
import { decideReview, getReviewState, readCheckpointFile, readWorkspaceFile, type ReviewState } from '../../adapters/review';
import { t } from '../../i18n';
import { DiffView } from './DiffView';

/**
 * VER-06: accept/reject a turn's file changes, one file at a time, against
 * `GET/POST /api/workspace/review/{message_id}`. Two facts are shown side
 * by side and never merged into one verdict: a human's accept/reject
 * (`approvals`/`pending`) and whatever automatic verification ran
 * (`testsStatus`) — accepting a file never turns a failed test green, and a
 * green test run never counts as a human's approval. An approval whose file
 * changed since (`stale`) is shown as invalidated, not silently kept.
 */
export function ReviewPanel({ messageId, onClose }: { messageId: string; onClose: () => void }) {
  const [state, setState] = useState<ReviewState | null | 'missing'>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [diffFor, setDiffFor] = useState<string | null>(null);
  const [diff, setDiff] = useState<{ before: string; after: string } | 'loading' | null>(null);

  const load = useCallback(() => {
    setError(null);
    getReviewState(messageId)
      .then(setState)
      .catch((e: unknown) => {
        if (e instanceof ApiError && e.status === 404) setState('missing');
        else setError((e as Error).message);
      });
  }, [messageId]);

  useEffect(() => {
    setState(null);
    load();
  }, [load]);

  const decide = async (path: string, decision: 'accept' | 'reject') => {
    setBusy(path);
    try {
      const next = await decideReview(messageId, path, decision);
      setState(next);
      if (diffFor === path) setDiffFor(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const openDiff = async (path: string) => {
    if (diffFor === path) {
      setDiffFor(null);
      return;
    }
    setDiffFor(path);
    setDiff('loading');
    if (state === null || state === 'missing') return;
    try {
      const [before, after] = await Promise.all([
        state.checkpoint ? readCheckpointFile(state.workspace, state.checkpoint, path) : Promise.resolve({ text: '', binary: false, exists: false }),
        readWorkspaceFile(state.workspace, path),
      ]);
      setDiff({ before: before.binary ? '' : before.text, after: after.binary ? '' : after.text });
    } catch (e) {
      setError((e as Error).message);
      setDiff(null);
    }
  };

  if (error) {
    return (
      <div className="fs-docs__review" role="alert" data-testid="review-panel">
        <p className="fs-notice" data-tone="warning">
          <AlertTriangle size={14} aria-hidden="true" /> {error}
        </p>
        <Button variant="ghost" label={t('Close')} onClick={onClose} />
      </div>
    );
  }
  if (state === 'missing') {
    return (
      <div className="fs-docs__review" data-testid="review-panel" data-state="missing">
        <p className="fs-notice">{t('There is nothing to review for this message — it changed no files, or review mode was off.')}</p>
        <Button variant="ghost" label={t('Close')} onClick={onClose} />
      </div>
    );
  }
  if (state === null) {
    return (
      <div className="fs-docs__review" data-testid="review-panel">
        <Skeleton label={t('Loading the review')} count={3} height="24px" />
      </div>
    );
  }

  const approvalOf = (path: string) => state.approvals.find((a) => a.path === path) ?? null;
  const tests = state.testsStatus;

  return (
    <div className="fs-docs__review" data-testid="review-panel">
      <header className="fs-docs__review-head">
        <h3>{t('Review this turn’s changes')}</h3>
        <span className="fs-docs__review-tests" data-testid="review-tests-status" data-state={tests ? (tests.ran ? (tests.inconclusive ? 'inconclusive' : tests.ok ? 'pass' : 'fail') : 'not-run') : 'unknown'}>
          {!tests || !tests.ran
            ? t('Automatic tests: not run for this turn')
            : tests.inconclusive
              ? t('Automatic tests: inconclusive')
              : tests.ok
                ? t('Automatic tests: passed')
                : t('Automatic tests: failed')}
        </span>
        <span className="fs-spacer" />
        <IconButton icon={X} label={t('Close')} size="sm" onClick={onClose} />
      </header>

      {state.pending.length > 0 && (
        <section className="fs-docs__review-section">
          <h4>{t('Pending ({n})', { n: state.pending.length })}</h4>
          <ul>
            {state.pending.map((path) => (
              <li key={path} className="fs-docs__review-row">
                <Circle size={14} aria-hidden="true" className="fs-docs__review-icon" />
                <span className="fs-docs__review-path">{path}</span>
                <span className="fs-spacer" />
                <Button size="sm" variant="ghost" label={t('View diff')} onClick={() => void openDiff(path)} />
                <Button size="sm" variant="primary" icon={Check} label={t('Accept')} loading={busy === path} onClick={() => void decide(path, 'accept')} testId={`review-accept-${path}`} />
                <Button size="sm" variant="danger" icon={X} label={t('Reject')} loading={busy === path} onClick={() => void decide(path, 'reject')} testId={`review-reject-${path}`} />
              </li>
            ))}
          </ul>
        </section>
      )}

      {state.accepted.length > 0 && (
        <section className="fs-docs__review-section">
          <h4>{t('Accepted ({n})', { n: state.accepted.length })}</h4>
          <ul>
            {state.accepted.map((path) => {
              const approval = approvalOf(path);
              return (
                <li key={path} className="fs-docs__review-row" data-stale={approval?.stale || undefined}>
                  <CheckCircle2 size={14} aria-hidden="true" className="fs-docs__review-icon fs-docs__review-icon--ok" />
                  <span className="fs-docs__review-path">{path}</span>
                  {approval?.stale && (
                    <span className="fs-docs__review-stale" data-testid={`review-stale-${path}`}>
                      <AlertTriangle size={12} aria-hidden="true" /> {t('Changed since approval — approval no longer covers this content')}
                    </span>
                  )}
                  <span className="fs-spacer" />
                  <Button size="sm" variant="ghost" label={t('View diff')} onClick={() => void openDiff(path)} />
                  {approval?.stale && <Button size="sm" variant="primary" icon={Check} label={t('Re-accept')} loading={busy === path} onClick={() => void decide(path, 'accept')} />}
                </li>
              );
            })}
          </ul>
        </section>
      )}

      {state.rejected.length > 0 && (
        <section className="fs-docs__review-section">
          <h4>{t('Rejected ({n})', { n: state.rejected.length })}</h4>
          <ul>
            {state.rejected.map((path) => (
              <li key={path} className="fs-docs__review-row">
                <XCircle size={14} aria-hidden="true" className="fs-docs__review-icon fs-docs__review-icon--no" />
                <span className="fs-docs__review-path">{path}</span>
                <span className="fs-docs__review-note">{t('reverted')}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {!state.pending.length && !state.accepted.length && !state.rejected.length && <p className="fs-docs__muted">{t('No files in this turn.')}</p>}

      {diffFor && (
        <div className="fs-docs__review-diff">
          {diff === 'loading' ? (
            <Skeleton label={t('Loading the difference')} count={4} height="18px" />
          ) : diff ? (
            <DiffView oldText={diff.before} newText={diff.after} oldLabel={t('Before this turn')} newLabel={t('Now')} onCancel={() => setDiffFor(null)} onApply={() => setDiffFor(null)} readOnly />
          ) : null}
        </div>
      )}
    </div>
  );
}
