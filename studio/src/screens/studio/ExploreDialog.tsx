import { useEffect, useState } from 'react';
import { Button, Dialog } from '../../components';
import { createSideThread } from '../../adapters/sideThreads';
import { t } from '../../i18n';

/**
 * B3 (CONTRATO_EXCURSOS.md) — "Explorar aparte": the dialog that turns a
 * quoted passage (or a whole turn, no passage) into a new excurso. Confirms
 * once, asks the one question the backend actually needs (what to explore),
 * and is explicit that the CURRENT conversation is untouched — the same
 * "el humano dibuja el grafo" posture the contract opens with: nothing here
 * fires a message at any model, it only creates the wired-but-empty session
 * (`src/side_threads.py::create_side_thread`) and hands the question back
 * for the caller to seed the new session's composer with.
 */

export interface ExploreAnchor {
  historyIndex: number;
  passage?: string;
}

export interface ExploreDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  sessionId: string | null;
  anchor: ExploreAnchor | null;
  /** Called once the side thread exists — `question` is what the person
   *  typed, for the caller to seed the new session's composer with. */
  onCreated: (newSessionId: string, question: string) => void;
  onNotice: (text: string, tone?: 'info' | 'warning' | 'danger') => void;
}

export default function ExploreDialog({ open, onOpenChange, sessionId, anchor, onCreated, onNotice }: ExploreDialogProps) {
  const [question, setQuestion] = useState('');
  const [busy, setBusy] = useState(false);

  // A fresh box every time the dialog opens for a new anchor — never the
  // previous prompt left lying around for the next passage.
  useEffect(() => {
    if (open) setQuestion('');
  }, [open, anchor]);

  const canSubmit = Boolean(sessionId) && Boolean(anchor) && question.trim().length > 0 && !busy;

  const submit = async () => {
    if (!sessionId || !anchor || !question.trim() || busy) return;
    setBusy(true);
    try {
      const asked = question.trim();
      const result = await createSideThread(sessionId, {
        anchorIndex: anchor.historyIndex,
        passage: anchor.passage,
        question: asked,
      });
      onOpenChange(false);
      onCreated(result.session_id, result.question ?? asked);
    } catch (error) {
      onNotice(`${t('Could not open the side thread')}: ${(error as Error).message}`, 'danger');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!busy) onOpenChange(next);
      }}
      title={t('Explore separately')}
      testId="explore-dialog"
      footer={
        <>
          <Button variant="ghost" label={t('Cancel')} disabled={busy} onClick={() => onOpenChange(false)} />
          <Button
            variant="primary"
            label={t('Open the thread')}
            loading={busy}
            disabled={!canSubmit}
            onClick={() => void submit()}
            testId="explore-submit"
          />
        </>
      }
    >
      <div className="fs-explore">
        {anchor?.passage && (
          <blockquote className="fs-explore__passage" data-testid="explore-passage">
            “{anchor.passage}”
          </blockquote>
        )}
        <p className="fs-explore__hint">
          {t('A new thread opens that sees the conversation up to this turn. The current conversation does not change.')}
        </p>
        <label className="fs-field-label" htmlFor="explore-question">
          {t('What do you want to explore separately?')}
        </label>
        <textarea
          id="explore-question"
          className="fs-field fs-explore__textarea"
          value={question}
          disabled={busy}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => {
            if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
              e.preventDefault();
              void submit();
            }
          }}
          placeholder={t('What do you want to explore separately?')}
          autoFocus
          required
          data-testid="explore-question"
        />
      </div>
    </Dialog>
  );
}
