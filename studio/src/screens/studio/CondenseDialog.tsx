import { useEffect, useRef, useState } from 'react';
import { Button, Dialog } from '../../components';
import { condense, previewCondense, type CondensePreview } from '../../adapters/condense';
import { formatTokenCount } from '../../adapters/sideThreads';
import { t, tn } from '../../i18n';

/**
 * F3 (CONTRATO_CABLES2) — "Condense up to here": pick an explicit range of
 * already-settled turns and fold them into one summary row, with a live,
 * free preview (`GET .../condense/preview`, no LLM call) on every edit, and
 * one real model call only once the user confirms (`POST .../condense`).
 *
 * Turn numbers shown here are 1-based ("turn 3"), matching how a person
 * counts; the adapter/backend index (`start`/`end`) is 0-based, converted
 * at the two edges of this component only.
 */

export interface CondenseDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  sessionId: string | null;
  /** 0-based history index of the assistant turn whose "Condense up to
   *  here" was clicked — the range's default END. `null` closes the dialog
   *  (nothing to condense up to). */
  toHistoryIndex: number | null;
  /** 0-based default START: the row right after the last condensed summary,
   *  or 0 when there is none yet — computed by the caller (`Studio.tsx`,
   *  which owns the turn list this dialog does not see). */
  defaultStart: number;
  /** The model the summary will actually run on, for the busy label —
   *  omitted (a generic label shows instead) rather than required, since
   *  naming it needs no round trip of its own. */
  modelLabel?: string;
  /** Condense succeeded — the caller reloads history (the range collapsed
   *  to one row server-side). */
  onDone: () => void;
  onNotice: (text: string, tone?: 'info' | 'warning' | 'danger') => void;
}

const PREVIEW_DEBOUNCE_MS = 250;

export default function CondenseDialog({ open, onOpenChange, sessionId, toHistoryIndex, defaultStart, modelLabel, onDone, onNotice }: CondenseDialogProps) {
  // 1-based turn numbers, as typed/shown — never the 0-based wire index.
  const [start, setStart] = useState(1);
  const [end, setEnd] = useState(1);
  const [preview, setPreview] = useState<CondensePreview | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [busy, setBusy] = useState(false);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const requestRef = useRef(0);

  // A fresh range every time the dialog opens for a new anchor.
  useEffect(() => {
    if (!open || toHistoryIndex === null) return;
    setStart(Math.max(0, defaultStart) + 1);
    setEnd(toHistoryIndex + 1);
    setPreview(null);
    setPreviewError(null);
  }, [open, toHistoryIndex, defaultStart]);

  useEffect(() => {
    if (!open || !sessionId) return;
    if (debounceRef.current) clearTimeout(debounceRef.current);
    const startIdx = start - 1;
    const endIdx = end - 1;
    if (!(startIdx >= 0 && endIdx > startIdx)) {
      setPreview(null);
      setPreviewError(t('Choose a start before the end.'));
      return;
    }
    debounceRef.current = setTimeout(() => {
      const requestId = ++requestRef.current;
      setPreviewing(true);
      previewCondense(sessionId, startIdx, endIdx)
        .then((result) => {
          if (requestRef.current !== requestId) return;
          setPreview(result);
          setPreviewError(null);
        })
        .catch((error: Error) => {
          if (requestRef.current !== requestId) return;
          setPreview(null);
          setPreviewError(error.message);
        })
        .finally(() => {
          if (requestRef.current === requestId) setPreviewing(false);
        });
    }, PREVIEW_DEBOUNCE_MS);
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, [open, sessionId, start, end]);

  const canSubmit = Boolean(sessionId) && Boolean(preview) && !previewError && !previewing && !busy;

  const submit = async () => {
    if (!sessionId || !preview || busy) return;
    setBusy(true);
    try {
      await condense(sessionId, start - 1, end - 1);
      onOpenChange(false);
      onNotice(t('Condensed {n} turns.', { n: preview.rows }));
      onDone();
    } catch (error) {
      onNotice(`${t('Could not condense that range')}: ${(error as Error).message}`, 'danger');
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
      title={t('Condense up to here')}
      testId="condense-dialog"
      footer={
        <>
          <Button variant="ghost" label={t('Cancel')} disabled={busy} onClick={() => onOpenChange(false)} />
          <Button
            variant="primary"
            label={busy ? (modelLabel ? t('Summarizing with {model}…', { model: modelLabel }) : t('Summarizing…')) : t('Condense')}
            loading={busy}
            disabled={!canSubmit}
            onClick={() => void submit()}
            testId="condense-submit"
          />
        </>
      }
    >
      <div className="fs-condense-dialog">
        <p className="fs-condense-dialog__hint">
          {t('Turns already settled fold into one summary; every future turn stops paying to resend them. The last turn is never eligible — it is still in progress.')}
        </p>
        <div className="fs-condense-dialog__range">
          <label className="fs-field-label" htmlFor="condense-start">
            {t('Start turn')}
          </label>
          <input
            id="condense-start"
            className="fs-field"
            type="number"
            min={1}
            value={start}
            disabled={busy}
            onChange={(e) => setStart(Math.max(1, Number(e.target.value) || 1))}
            data-testid="condense-start"
          />
          <label className="fs-field-label" htmlFor="condense-end">
            {t('End turn')}
          </label>
          <input
            id="condense-end"
            className="fs-field"
            type="number"
            min={1}
            value={end}
            disabled={busy}
            onChange={(e) => setEnd(Math.max(1, Number(e.target.value) || 1))}
            data-testid="condense-end"
          />
        </div>
        {previewError && (
          <p className="fs-notice" data-tone="warning" data-testid="condense-preview-error">
            {previewError}
          </p>
        )}
        {!previewError && preview && (
          <p className="fs-condense-dialog__summary" data-testid="condense-preview">
            {t('{rows} · ~{before} tok → ~{after} tok', {
              rows: tn(preview.rows, '{n} row', '{n} rows', { n: preview.rows }),
              before: formatTokenCount(preview.tokens_before),
              after: formatTokenCount(preview.tokens_after_estimate),
            })}
          </p>
        )}
        {previewing && !preview && !previewError && <p className="fs-condense-dialog__summary">{t('Checking…')}</p>}
      </div>
    </Dialog>
  );
}
