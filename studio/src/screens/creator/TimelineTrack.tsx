import { useCallback, useRef, useState } from 'react';
import { t } from '../../i18n';
import { previewSnap, type TimelineDocContent, type TimelineTrack as TimelineTrackData } from '../../adapters/creator_timeline';

/**
 * WP13 — One track's lane: a lightweight SVG row (no canvas needed at this
 * scale — a track rarely has more than a few hundred clips, and SVG gives
 * free hit-testing/accessibility hooks a raw canvas would have to
 * hand-roll). Clips are `<rect>` + `<text>`, dragged via Pointer Events
 * with a live snap preview computed client-side
 * (`adapters/creator_timeline.ts::previewSnap`) — the ACTUAL move is only
 * ever committed server-side through `onCommitMove` (`timeline.snap`),
 * never applied optimistically to local state, so a rejected/late server
 * response can't leave the UI showing a position the document never had.
 */

export const TRACK_HEIGHT = 44;
export const TRACK_GAP = 6;

export interface TimelineTrackProps {
  track: TimelineTrackData;
  content: TimelineDocContent;
  pixelsPerTick: number;
  selectedClipId: string | null;
  onSelectClip: (clipId: string | null) => void;
  /** Commits a drag: server-validated via `timeline.snap` regardless of
   *  whether a snap candidate was found (see that op's docstring). */
  onCommitMove: (trackId: string, clipId: string, nearTicks: bigint) => void;
  frameTicks?: bigint;
  snapWindowTicks?: bigint;
  disabled?: boolean;
}

function tickToX(ticks: string | bigint, pixelsPerTick: number): number {
  return Number(BigInt(ticks)) * pixelsPerTick;
}

export function TimelineTrack({
  track, content, pixelsPerTick, selectedClipId, onSelectClip, onCommitMove,
  frameTicks, snapWindowTicks = 6n, disabled = false,
}: TimelineTrackProps) {
  const [dragState, setDragState] = useState<{ clipId: string; startX: number; originTicks: bigint; previewTicks: bigint } | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);

  const beginDrag = useCallback((clipId: string, originTicks: bigint, clientX: number) => {
    if (disabled || track.locked) return;
    setDragState({ clipId, startX: clientX, originTicks, previewTicks: originTicks });
    onSelectClip(clipId);
  }, [disabled, track.locked, onSelectClip]);

  const onPointerMove = useCallback((e: React.PointerEvent<SVGRectElement>) => {
    if (!dragState) return;
    const deltaPx = e.clientX - dragState.startX;
    const deltaTicks = BigInt(Math.round(deltaPx / pixelsPerTick));
    let candidate = dragState.originTicks + deltaTicks;
    if (candidate < 0n) candidate = 0n;
    const snap = previewSnap(content, candidate, snapWindowTicks, { frameTicks, excludeTrackId: undefined });
    setDragState((prev) => (prev ? { ...prev, previewTicks: snap ? snap.ticks : candidate } : prev));
  }, [dragState, pixelsPerTick, content, snapWindowTicks, frameTicks]);

  const onPointerUp = useCallback((e: React.PointerEvent<SVGRectElement>) => {
    if (!dragState) return;
    try { e.currentTarget.releasePointerCapture(e.pointerId); } catch { /* not captured */ }
    if (dragState.previewTicks !== dragState.originTicks) {
      onCommitMove(track.id, dragState.clipId, dragState.previewTicks);
    }
    setDragState(null);
  }, [dragState, onCommitMove, track.id]);

  const onKeyDownClip = useCallback((e: React.KeyboardEvent<SVGRectElement>, clip: TimelineTrackData['clips'][number]) => {
    if (disabled || track.locked) return;
    const step = frameTicks ?? 1n;
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      e.preventDefault();
      const start = BigInt(clip.timeline_start_ticks);
      const next = e.key === 'ArrowLeft' ? (start > step ? start - step : 0n) : start + step;
      onCommitMove(track.id, clip.id, next);
    }
  }, [disabled, track.locked, frameTicks, onCommitMove, track.id]);

  return (
    <div className="fs-timeline__track" data-testid={`timeline-track-${track.id}`}>
      <div className="fs-timeline__track-label">
        <span className="fs-timeline__track-kind">{track.kind}</span>
        <span className="fs-timeline__track-id">{track.id}</span>
        {track.locked && <span className="fs-timeline__track-locked" title={t('Locked')}>🔒</span>}
      </div>
      <svg
        ref={svgRef}
        className="fs-timeline__track-lane"
        height={TRACK_HEIGHT}
        role="list"
        aria-label={t('Track {id} clips', { id: track.id })}
      >
        {track.clips.map((clip) => {
          const isDragging = dragState?.clipId === clip.id;
          const startTicks = isDragging ? dragState.previewTicks : BigInt(clip.timeline_start_ticks);
          const x = tickToX(startTicks, pixelsPerTick);
          const width = Math.max(2, tickToX(clip.timeline_duration_ticks, pixelsPerTick));
          const selected = selectedClipId === clip.id;
          return (
            <g key={clip.id} role="listitem">
              <rect
                data-testid={`timeline-clip-${clip.id}`}
                data-clip-id={clip.id}
                tabIndex={disabled || track.locked ? -1 : 0}
                role="button"
                aria-label={t('Clip {id}, starts at tick {t}', { id: clip.id, t: startTicks.toString() })}
                aria-selected={selected}
                x={x}
                y={2}
                width={width}
                height={TRACK_HEIGHT - 4}
                rx={4}
                className={`fs-timeline__clip${selected ? ' fs-timeline__clip--selected' : ''}${isDragging ? ' fs-timeline__clip--dragging' : ''}`}
                onPointerDown={(e) => {
                  try { e.currentTarget.setPointerCapture(e.pointerId); } catch { /* not supported (e.g. jsdom/happy-dom in tests) */ }
                  beginDrag(clip.id, BigInt(clip.timeline_start_ticks), e.clientX);
                }}
                onPointerMove={onPointerMove}
                onPointerUp={onPointerUp}
                onClick={() => onSelectClip(clip.id)}
                onKeyDown={(e) => onKeyDownClip(e, clip)}
              />
              <text x={x + 4} y={TRACK_HEIGHT / 2 + 4} className="fs-timeline__clip-label" pointerEvents="none">
                {clip.id}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
