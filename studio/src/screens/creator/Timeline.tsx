import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Clapperboard, Redo2, Undo2, ZoomIn, ZoomOut } from 'lucide-react';
import { Button, EmptyState, Toast } from '../../components';
import { t } from '../../i18n';
import * as api from '../../adapters/creator';
import * as tlApi from '../../adapters/creator_timeline';
import { TimelineTrack, TRACK_GAP, TRACK_HEIGHT } from './TimelineTrack';
import { RenderPanel } from './RenderPanel';
import './timeline.css';

/**
 * WP13 — Timeline screen: a virtualized-enough (tracks are few, clips per
 * track windowed by the browser's own SVG clipping — no manual
 * windowing needed at Creator's real scale) editor over a `timeline`
 * CreatorDocument. Every mutation is a typed op through
 * `adapters/creator_timeline.ts`, applied with the document's OWN
 * `expected_revision` — a 409 (`RevisionConflictError`) reloads the
 * document rather than silently overwriting, exactly like
 * `CreatorScreen.tsx`'s command editor.
 *
 * Keyboard (WP13.md "Implementación" step 4 — a real NLE's minimum set):
 * ArrowLeft/ArrowRight nudge the playhead (or a selected, unlocked clip)
 * by one frame tick; J/K/L shuttle playback rate (no real decoder is
 * wired here — this drives the playhead advance only, at the document's
 * OWN clock, and is documented as a degraded/preview affordance, not a
 * real-time video player); I/O set a local in/out selection range (not
 * persisted — a future export-range feature reads it, this lot only
 * renders it).
 */

const MIN_PX_PER_TICK = 0.02;
const MAX_PX_PER_TICK = 4;

function useFrameTicks(doc: api.CreatorDocument | null): bigint {
  return useMemo(() => {
    const clock = (doc?.content as tlApi.TimelineDocContent | undefined)?.clock;
    if (!clock) return 1n;
    // This package treats one document-clock tick as one frame for a
    // video-authoritative timeline (see project_view.py's docstring) —
    // the frame step is always 1 tick, regardless of the clock's rate.
    return 1n;
  }, [doc]);
}

export interface TimelineProps {
  doc: api.CreatorDocument;
  onDocUpdated: (doc: api.CreatorDocument) => void;
  onRevisionConflict: () => void;
}

export function Timeline({ doc, onDocUpdated, onRevisionConflict }: TimelineProps) {
  const content = doc.content as tlApi.TimelineDocContent;
  const [pixelsPerTick, setPixelsPerTick] = useState(0.5);
  const [playheadTicks, setPlayheadTicks] = useState<bigint>(0n);
  const [selectedClipId, setSelectedClipId] = useState<string | null>(null);
  const [inOut, setInOut] = useState<{ in: bigint | null; out: bigint | null }>({ in: null, out: null });
  const [playRate, setPlayRate] = useState(0); // 0 = stopped; negative = reverse
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [validation, setValidation] = useState<{ ok: boolean; findings: tlApi.ValidationFinding[] } | null>(null);
  const [revisionHistory, setRevisionHistory] = useState<number[]>([doc.revision]);
  const [undoIndex, setUndoIndex] = useState(0); // index into revisionHistory, from the end
  const [renderPanelOpen, setRenderPanelOpen] = useState(false); // WP14
  const frameTicks = useFrameTicks(doc);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setRevisionHistory((prev) => (prev[prev.length - 1] === doc.revision ? prev : [...prev, doc.revision]));
    setUndoIndex(0);
  }, [doc.revision]);

  useEffect(() => {
    const controller = new AbortController();
    tlApi.getTimelineValidation(doc.id, controller.signal).then(setValidation).catch(() => setValidation(null));
    return () => controller.abort();
  }, [doc.id, doc.revision]);

  useEffect(() => {
    if (playRate === 0) return;
    const id = window.setInterval(() => {
      setPlayheadTicks((t0) => {
        const delta = BigInt(Math.round(playRate * 10)) * frameTicks;
        const next = t0 + delta;
        return next < 0n ? 0n : next;
      });
    }, 100);
    return () => window.clearInterval(id);
  }, [playRate, frameTicks]);

  const runOp = useCallback(async (fn: () => Promise<api.ApplyCommandResult>) => {
    setError(null);
    try {
      const result = await fn();
      onDocUpdated(result.document);
    } catch (err) {
      if (err instanceof api.RevisionConflictError) {
        onRevisionConflict();
      } else {
        setError(err instanceof Error ? err.message : String(err));
      }
    }
  }, [onDocUpdated, onRevisionConflict]);

  const onCommitMove = useCallback((trackId: string, clipId: string, nearTicks: bigint) => {
    void runOp(() => tlApi.snapClip(doc.id, doc.revision, trackId, clipId, nearTicks.toString(), '6'));
  }, [doc.id, doc.revision, runOp]);

  const undo = useCallback(() => {
    const idx = undoIndex + 1;
    if (idx >= revisionHistory.length) return;
    const target = revisionHistory[revisionHistory.length - 1 - idx];
    void tlApi.undoTo(doc.id, target, doc.revision)
      .then((result) => { setUndoIndex(idx); onDocUpdated(result.document); setToast(t('Undone.')); })
      .catch((err) => {
        if (err instanceof api.RevisionConflictError) onRevisionConflict();
        else setError(err instanceof Error ? err.message : String(err));
      });
  }, [undoIndex, revisionHistory, doc.id, doc.revision, onDocUpdated, onRevisionConflict]);

  const redo = useCallback(() => {
    const idx = undoIndex - 1;
    if (idx < 0) return;
    const target = revisionHistory[revisionHistory.length - 1 - idx];
    void tlApi.undoTo(doc.id, target, doc.revision)
      .then((result) => { setUndoIndex(idx); onDocUpdated(result.document); setToast(t('Redone.')); })
      .catch((err) => {
        if (err instanceof api.RevisionConflictError) onRevisionConflict();
        else setError(err instanceof Error ? err.message : String(err));
      });
  }, [undoIndex, revisionHistory, doc.id, doc.revision, onDocUpdated, onRevisionConflict]);

  const onKeyDown = useCallback((e: React.KeyboardEvent<HTMLDivElement>) => {
    const target = e.target as HTMLElement;
    if (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA') return;
    switch (e.key) {
      case 'ArrowLeft':
        e.preventDefault();
        setPlayheadTicks((t0) => (t0 > frameTicks ? t0 - frameTicks : 0n));
        break;
      case 'ArrowRight':
        e.preventDefault();
        setPlayheadTicks((t0) => t0 + frameTicks);
        break;
      case 'j': case 'J':
        setPlayRate((r) => (r >= 0 ? -1 : r * 2));
        break;
      case 'k': case 'K':
        setPlayRate(0);
        break;
      case 'l': case 'L':
        setPlayRate((r) => (r <= 0 ? 1 : r * 2));
        break;
      case 'i': case 'I':
        setInOut((prev) => ({ ...prev, in: playheadTicks }));
        break;
      case 'o': case 'O':
        setInOut((prev) => ({ ...prev, out: playheadTicks }));
        break;
      case 'Escape':
        setSelectedClipId(null);
        break;
      case 'z': case 'Z':
        if (e.ctrlKey || e.metaKey) { e.preventDefault(); if (e.shiftKey) redo(); else undo(); }
        break;
      default:
        break;
    }
  }, [frameTicks, playheadTicks, undo, redo]);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 2500);
    return () => window.clearTimeout(timer);
  }, [toast]);

  if (!content || !Array.isArray(content.tracks)) {
    return <EmptyState title={t('No timeline content')} body={t('This document has no tracks yet.')} />;
  }

  const totalTicks = BigInt(content.duration_ticks || '0');
  const width = Math.max(200, Number(totalTicks) * pixelsPerTick + 40);
  const errorCount = validation?.findings.filter((f) => f.severity === 'error').length ?? 0;

  return (
    <div
      className="fs-timeline"
      data-testid="timeline-editor"
      tabIndex={0}
      role="application"
      aria-label={t('Timeline editor')}
      onKeyDown={onKeyDown}
      ref={containerRef}
    >
      <div className="fs-timeline__toolbar" role="toolbar" aria-label={t('Timeline controls')}>
        <span className="fs-timeline__muted">{t('revision {n}', { n: doc.revision })}</span>
        <Button
          size="sm" variant="ghost" icon={Undo2} label={t('Undo')}
          onClick={undo} disabled={undoIndex + 1 >= revisionHistory.length} testId="timeline-undo"
        />
        <Button
          size="sm" variant="ghost" icon={Redo2} label={t('Redo')}
          onClick={redo} disabled={undoIndex <= 0} testId="timeline-redo"
        />
        <Button size="sm" variant="ghost" icon={ZoomOut} label={t('Zoom out')}
          onClick={() => setPixelsPerTick((p) => Math.max(MIN_PX_PER_TICK, p / 1.5))} testId="timeline-zoom-out" />
        <Button size="sm" variant="ghost" icon={ZoomIn} label={t('Zoom in')}
          onClick={() => setPixelsPerTick((p) => Math.min(MAX_PX_PER_TICK, p * 1.5))} testId="timeline-zoom-in" />
        <Button size="sm" variant="primary" icon={Clapperboard} label={t('Render')}
          onClick={() => setRenderPanelOpen(true)} testId="timeline-render-open" />
        <span className="fs-timeline__muted">{t('Playhead: {t}', { t: playheadTicks.toString() })}</span>
        {inOut.in !== null && <span className="fs-timeline__muted">{t('In: {t}', { t: inOut.in.toString() })}</span>}
        {inOut.out !== null && <span className="fs-timeline__muted">{t('Out: {t}', { t: inOut.out.toString() })}</span>}
        {validation && (
          <span className={errorCount > 0 ? 'fs-timeline__validation fs-timeline__validation--error' : 'fs-timeline__validation fs-timeline__validation--ok'} data-testid="timeline-validation">
            {errorCount > 0 ? t('{n} problem(s)', { n: errorCount }) : t('Valid')}
          </span>
        )}
      </div>

      {error && <p className="fs-timeline__error" role="alert">{error}</p>}

      <div className="fs-timeline__scroll">
        <div className="fs-timeline__tracks" style={{ width }}>
          <div
            className="fs-timeline__playhead"
            style={{ left: Number(playheadTicks) * pixelsPerTick }}
            data-testid="timeline-playhead"
            aria-hidden="true"
          />
          {content.tracks.map((track) => (
            <TimelineTrack
              key={track.id}
              track={track}
              content={content}
              pixelsPerTick={pixelsPerTick}
              selectedClipId={selectedClipId}
              onSelectClip={setSelectedClipId}
              onCommitMove={onCommitMove}
              frameTicks={frameTicks}
            />
          ))}
        </div>
      </div>

      {toast && <Toast>{toast}</Toast>}

      {renderPanelOpen && (
        <RenderPanel
          projectId={doc.project_id}
          docId={doc.id}
          onClose={() => setRenderPanelOpen(false)}
        />
      )}
    </div>
  );
}

export { TRACK_GAP, TRACK_HEIGHT };
