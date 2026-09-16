import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { Button, EmptyState, Skeleton, Toast } from '../../components';
import { t } from '../../i18n';
import * as api from '../../adapters/creator';
import * as subApi from '../../adapters/creator_subtitles';
import { undoTo } from '../../adapters/creator_timeline';
import './creator.css';

/**
 * WP16 — Subtitle editor tab. A top-level `CreatorScreen.tsx` tab (like
 * "Models"), over `routes/creator_subtitle_routes.py` (from-transcript /
 * qa / export) and the `subtitles.*` typed ops
 * (`src/creator/ops/subtitle_ops.py`) applied through the SAME
 * `POST .../commands` route every other Creator document uses.
 *
 * Layout: pick a `transcript` document to regenerate from (left), a
 * waveform-less but time-synced cue list (center) — inline-editable text,
 * per-cue QA warnings, keyboard Up/Down to move the selection, Enter to
 * focus the text — and export buttons (right). Undo goes through the same
 * revision-history-as-a-new-revision endpoint WP13 wired
 * (`adapters/creator_timeline.ts::undoTo`), since it is document-kind
 * agnostic.
 */

function ticksToClockSeconds(ticks: string, clock: { ticks_per_second_numerator: string; ticks_per_second_denominator: string }): number {
  const num = BigInt(clock.ticks_per_second_numerator);
  const den = BigInt(clock.ticks_per_second_denominator);
  // seconds = ticks * den / num, done in a float at the end only — good
  // enough for display; the server keeps the exact rational.
  return (Number(BigInt(ticks) * den) / Number(num));
}

function fmtTime(seconds: number): string {
  const s = Math.max(0, seconds);
  const m = Math.floor(s / 60);
  const rem = (s - m * 60).toFixed(2);
  return `${m}:${rem.padStart(5, '0')}`;
}

export interface SubtitlesProps {
  projectId: string;
}

export function Subtitles({ projectId }: SubtitlesProps) {
  const [transcripts, setTranscripts] = useState<api.CreatorDocument[]>([]);
  const [subtitleDocs, setSubtitleDocs] = useState<api.CreatorDocument[]>([]);
  const [selectedTranscriptId, setSelectedTranscriptId] = useState('');
  const [doc, setDoc] = useState<api.CreatorDocument | null>(null);
  const [qa, setQa] = useState<subApi.QaReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [selectedCueIndex, setSelectedCueIndex] = useState(0);
  const [revisionHistory, setRevisionHistory] = useState<number[]>([]);
  const [undoIndex, setUndoIndex] = useState(0);
  const listRef = useRef<HTMLUListElement>(null);

  const content = doc?.content as subApi.SubtitlesDocContent | undefined;

  const reloadDocuments = useCallback(() => {
    if (!projectId) return;
    const controller = new AbortController();
    api.listDocuments(projectId, controller.signal)
      .then((r) => {
        setTranscripts(r.documents.filter((d) => d.kind === 'transcript'));
        setSubtitleDocs(r.documents.filter((d) => d.kind === 'subtitles'));
      })
      .catch(() => { setTranscripts([]); setSubtitleDocs([]); });
    return () => controller.abort();
  }, [projectId]);

  useEffect(() => { reloadDocuments(); }, [reloadDocuments]);

  useEffect(() => {
    if (!doc) return;
    setRevisionHistory((prev) => (prev[prev.length - 1] === doc.revision ? prev : [...prev, doc.revision]));
    setUndoIndex(0);
  }, [doc]);

  useEffect(() => {
    if (!doc) { setQa(null); return; }
    const controller = new AbortController();
    subApi.getQa(doc.id, controller.signal).then((r) => setQa(r.report)).catch(() => setQa(null));
    return () => controller.abort();
  }, [doc]);

  const openDoc = useCallback((d: api.CreatorDocument) => {
    setDoc(d);
    setSelectedCueIndex(0);
    setRevisionHistory([d.revision]);
    setUndoIndex(0);
  }, []);

  const regenerate = useCallback(async () => {
    if (!selectedTranscriptId) return;
    setLoading(true);
    setError(null);
    try {
      if (doc && content?.source_transcript_id === selectedTranscriptId) {
        // Same track exists already: regenerate IN PLACE, preserving edits.
        const transcriptDoc = transcripts.find((d) => d.id === selectedTranscriptId);
        const result = await subApi.regenerateFromTranscript(
          doc.id, doc.revision, transcriptDoc?.content,
        );
        setDoc(result.document);
        setToast(t('Regenerated from transcript (edited cues kept).'));
      } else {
        const created = await subApi.createFromTranscript(selectedTranscriptId);
        setSubtitleDocs((prev) => [created, ...prev]);
        openDoc(created);
        setToast(t('Subtitle track created from transcript.'));
      }
    } catch (err) {
      if (err instanceof api.RevisionConflictError) {
        setError(t('Someone else changed this document — reload it.'));
      } else {
        setError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setLoading(false);
    }
  }, [selectedTranscriptId, doc, content, transcripts, openDoc]);

  const runOp = useCallback(async (fn: () => Promise<api.ApplyCommandResult>) => {
    setError(null);
    try {
      const result = await fn();
      setDoc(result.document);
    } catch (err) {
      if (err instanceof api.RevisionConflictError) {
        setError(t('Someone else changed this document — reload it.'));
      } else {
        setError(err instanceof Error ? err.message : String(err));
      }
    }
  }, []);

  const editCueLines = useCallback((cueId: string, text: string) => {
    if (!doc) return;
    const lines = text.split('\n').filter((l) => l.length > 0);
    if (lines.length === 0) return;
    void runOp(() => subApi.editCue(doc.id, doc.revision, cueId, { lines }));
  }, [doc, runOp]);

  const undo = useCallback(() => {
    if (!doc) return;
    const idx = undoIndex + 1;
    if (idx >= revisionHistory.length) return;
    const target = revisionHistory[revisionHistory.length - 1 - idx];
    undoTo(doc.id, target, doc.revision)
      .then((result) => { setUndoIndex(idx); setDoc(result.document); setToast(t('Undone.')); })
      .catch((err) => {
        if (err instanceof api.RevisionConflictError) setError(t('Someone else changed this document — reload it.'));
        else setError(err instanceof Error ? err.message : String(err));
      });
  }, [doc, undoIndex, revisionHistory]);

  const redo = useCallback(() => {
    if (!doc) return;
    const idx = undoIndex - 1;
    if (idx < 0) return;
    const target = revisionHistory[revisionHistory.length - 1 - idx];
    undoTo(doc.id, target, doc.revision)
      .then((result) => { setUndoIndex(idx); setDoc(result.document); setToast(t('Redone.')); })
      .catch((err) => {
        if (err instanceof api.RevisionConflictError) setError(t('Someone else changed this document — reload it.'));
        else setError(err instanceof Error ? err.message : String(err));
      });
  }, [doc, undoIndex, revisionHistory]);

  const download = useCallback(async (format: 'srt' | 'vtt' | 'ass') => {
    if (!doc) return;
    try {
      const text = await subApi.fetchExportText(doc.id, format);
      const blob = new Blob([text], { type: 'text/plain' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `subtitles.${format}`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [doc]);

  const qaByCueId = useMemo(() => {
    const map = new Map<string, subApi.QaCueIssues>();
    for (const entry of qa?.issues ?? []) map.set(entry.cue_id, entry);
    return map;
  }, [qa]);

  const onListKeyDown = useCallback((e: React.KeyboardEvent<HTMLUListElement>) => {
    const target = e.target as HTMLElement;
    if (target.tagName === 'TEXTAREA') return;
    if (!content) return;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setSelectedCueIndex((i) => Math.min(i + 1, content.cues.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setSelectedCueIndex((i) => Math.max(i - 1, 0));
    }
  }, [content]);

  useEffect(() => {
    const el = listRef.current?.querySelector<HTMLElement>(`[data-cue-index="${selectedCueIndex}"]`);
    el?.focus({ preventScroll: false });
  }, [selectedCueIndex, doc]);

  return (
    <div className="fs-creator__grid" role="group" aria-label={t('Subtitle editor')} data-testid="subtitles-screen">
      <aside className="fs-creator__left" aria-label={t('Subtitle tracks')}>
        <h2>{t('Subtitle tracks')}</h2>
        <div className="fs-subtitles__regen">
          <label htmlFor="subtitles-transcript-select">{t('From transcript')}</label>
          <select
            id="subtitles-transcript-select"
            value={selectedTranscriptId}
            onChange={(e) => setSelectedTranscriptId(e.target.value)}
          >
            <option value="">{t('Choose a transcript…')}</option>
            {transcripts.map((td) => (
              <option key={td.id} value={td.id}>{td.id}</option>
            ))}
          </select>
          <Button
            size="sm" variant="primary" icon={RefreshCw}
            label={t('Regenerate from transcript')}
            onClick={() => void regenerate()}
            disabled={!selectedTranscriptId || loading}
            loading={loading}
            testId="subtitles-regenerate"
          />
        </div>
        <ul className="fs-subtitles__track-list" aria-label={t('Subtitle documents')}>
          {subtitleDocs.map((d) => (
            <li key={d.id}>
              <button
                type="button"
                className={doc?.id === d.id ? 'fs-subtitles__track fs-subtitles__track--active' : 'fs-subtitles__track'}
                onClick={() => openDoc(d)}
                data-testid={`subtitles-track-${d.id}`}
              >
                {(d.content as subApi.SubtitlesDocContent).language} · {d.id}
              </button>
            </li>
          ))}
          {subtitleDocs.length === 0 && (
            <EmptyState title={t('No subtitle tracks yet')} body={t('Pick a transcript and regenerate.')} />
          )}
        </ul>
      </aside>

      <div className="fs-creator__center">
        {!doc && <EmptyState title={t('No subtitle track selected')} body={t('Choose one on the left, or create one from a transcript.')} />}

        {doc && content && (
          <>
            <div className="fs-subtitles__toolbar" role="toolbar" aria-label={t('Subtitle controls')}>
              <span>{t('{n} cue(s)', { n: content.cues.length })}</span>
              <span>{t('{n} problem(s)', { n: qa?.cues_with_issues ?? 0 })}</span>
              <Button size="sm" label={t('Undo')} onClick={undo} disabled={undoIndex + 1 >= revisionHistory.length} testId="subtitles-undo" />
              <Button size="sm" label={t('Redo')} onClick={redo} disabled={undoIndex <= 0} testId="subtitles-redo" />
              <Button size="sm" label={t('Export SRT')} onClick={() => void download('srt')} testId="subtitles-export-srt" />
              <Button size="sm" label={t('Export VTT')} onClick={() => void download('vtt')} testId="subtitles-export-vtt" />
              <Button size="sm" label={t('Export ASS')} onClick={() => void download('ass')} testId="subtitles-export-ass" />
            </div>

            {error && <p className="fs-creator__error" role="alert">{error}</p>}

            <ul
              className="fs-subtitles__cues"
              role="application"
              aria-label={t('Subtitle cues')}
              ref={listRef}
              onKeyDown={onListKeyDown}
            >
              {content.cues.map((cue, i) => {
                const issues = qaByCueId.get(cue.id);
                const start = ticksToClockSeconds(cue.start_ticks, content.clock);
                const end = ticksToClockSeconds(
                  (BigInt(cue.start_ticks) + BigInt(cue.duration_ticks)).toString(), content.clock,
                );
                return (
                  <li
                    key={cue.id}
                    data-testid={`subtitles-cue-${cue.id}`}
                    data-cue-index={i}
                    tabIndex={selectedCueIndex === i ? 0 : -1}
                    className={selectedCueIndex === i ? 'fs-subtitles__cue fs-subtitles__cue--selected' : 'fs-subtitles__cue'}
                    onFocus={() => setSelectedCueIndex(i)}
                  >
                    <div className="fs-subtitles__cue-meta">
                      <span className="fs-subtitles__cue-time">{fmtTime(start)} → {fmtTime(end)}</span>
                      {cue.speaker_id && <span className="fs-subtitles__cue-speaker">{cue.speaker_id}</span>}
                      {cue.unmapped && <span className="fs-subtitles__cue-flag fs-subtitles__cue-flag--unmapped">{t('unmapped')}</span>}
                      {cue.edited && <span className="fs-subtitles__cue-flag fs-subtitles__cue-flag--edited">{t('edited')}</span>}
                    </div>
                    <textarea
                      className="fs-subtitles__cue-text"
                      defaultValue={cue.lines.join('\n')}
                      aria-label={t('Cue text')}
                      onBlur={(e) => editCueLines(cue.id, e.target.value)}
                      data-testid={`subtitles-cue-text-${cue.id}`}
                    />
                    {issues && issues.issues.length > 0 && (
                      <ul className="fs-subtitles__cue-warnings" aria-label={t('QA warnings')}>
                        {issues.issues.map((iss, j) => (
                          <li key={j} className="fs-subtitles__cue-warning">
                            {t('{rule}: {actual} (target {target})', {
                              rule: iss.rule, actual: String(iss.actual), target: String(iss.target),
                            })}
                          </li>
                        ))}
                      </ul>
                    )}
                  </li>
                );
              })}
            </ul>
          </>
        )}
        {loading && !doc && <Skeleton label={t('Loading')} count={3} height="20px" />}
      </div>

      {toast && <Toast>{toast}</Toast>}
    </div>
  );
}
