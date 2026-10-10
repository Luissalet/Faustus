import { useEffect, useRef } from 'react';
import { audioLevel } from './audio';
import type { VoicePhase } from './engine';
import { MOUTHS, orbShape, stepMouth, visemeAt, type Mouth, type Playback } from './visemes';

/** A sampled spherical field. While Faustus speaks and the server sent the marks of its voice (`playback.marks`), the
 *  sphere takes the mouth shape of the sound that is playing: open for an "a", wide for an "i", pursed for an "u",
 *  pressed for "m, b, p". Without marks, and while it listens, it deforms with the real input/output RMS as before.
 *  With reduced motion it keeps its round shape either way.
 *  The canvas reports what it is doing in `data-source` (visemes | volume), `data-viseme` and `data-mouth`. */
export function VoiceOrb({ analyser, phase, playback }: { analyser: AnalyserNode | null; phase: VoicePhase; playback?: Playback | null }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const live = useRef({ analyser, phase, playback }); live.current = { analyser, phase, playback };
  useEffect(() => {
    const el = canvas.current!;
    const ctx = el.getContext('2d'); if (!ctx) return;
    const reduced = matchMedia('(prefers-reduced-motion: reduce)');
    let frame = 0, previous = 0, amplitude = 0, mouth: Mouth = MOUTHS.rest, shownViseme = '';
    const draw = (time: number) => {
      frame = requestAnimationFrame(draw);
      if (document.hidden || time - previous < (reduced.matches ? 200 : 33)) return;
      const dt = previous ? time - previous : 0;
      previous = time;
      const size = el.clientWidth, dpr = Math.min(devicePixelRatio || 1, 2);
      if (el.width !== Math.round(size * dpr)) { el.width = Math.round(size * dpr); el.height = el.width; }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, size, size);
      const state = live.current;
      const level = state.analyser ? Math.min(1, audioLevel(state.analyser) * 6) : 0;
      amplitude += (level - amplitude) * 0.24;
      // The mouth: the viseme of the sound playing now, eased; a closed mouth when there are no marks.
      const marks = state.playback?.marks ?? null;
      const viseme = marks && state.playback ? visemeAt(marks, state.playback.currentMs()) : null;
      mouth = viseme ? stepMouth(mouth, MOUTHS[viseme], dt) : stepMouth(mouth, MOUTHS.rest, dt, 120);
      const shape = orbShape(mouth, reduced.matches);
      const shown = viseme ?? 'volume';
      if (shown !== shownViseme) { shownViseme = shown; el.dataset.viseme = shown; el.dataset.source = viseme ? 'visemes' : 'volume'; }
      el.dataset.mouth = `${mouth.open.toFixed(2)} ${mouth.wide.toFixed(2)} ${mouth.round.toFixed(2)} ${mouth.press.toFixed(2)}`;
      const angle = reduced.matches ? 0.5 : time * 0.00013;
      const radius = size * (0.32 + amplitude * 0.035);
      const style = getComputedStyle(el);
      const color = style.getPropertyValue(state.phase === 'listening' ? '--fs-focus' : state.phase === 'error' ? '--fs-danger' : '--fs-brand').trim() || style.color;
      ctx.fillStyle = color; ctx.strokeStyle = color;
      const points: { x: number; y: number; z: number; r: number }[] = [];
      for (let i = 0; i < 850; i++) {
        const y = 1 - (i / 849) * 2, ring = Math.sqrt(1 - y * y);
        const theta = i * 2.399963 + angle;
        const wave = reduced.matches ? 0 : amplitude * 0.15 * Math.sin(theta * 4 + y * 8 + time * 0.003);
        const x = Math.cos(theta) * ring, z = Math.sin(theta) * ring;
        const tiltY = y * 0.94 - z * 0.342, tiltZ = y * 0.342 + z * 0.94;
        const perspective = 2.8 / (2.8 - tiltZ * 0.35);
        points.push({ x: size / 2 + x * radius * perspective * (1 + wave) * shape.sx, y: size / 2 + tiltY * radius * perspective * (1 + wave) * shape.sy, z: tiltZ, r: 0.6 + (tiltZ + 1) * 0.5 });
      }
      points.sort((a, b) => a.z - b.z);
      for (const p of points) {
        ctx.globalAlpha = 0.18 + (p.z + 1) * 0.37;
        ctx.beginPath(); ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2); ctx.fill();
      }
      ctx.globalAlpha = 0.25; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.ellipse(size / 2, size / 2, radius * shape.ringX, radius * shape.ringY, -0.32, 0, Math.PI * 2); ctx.stroke();
      ctx.globalAlpha = 1;
    };
    frame = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(frame);
  }, []);
  return <canvas ref={canvas} className="fs-voice__orb" aria-hidden="true" data-source="volume" />;
}
