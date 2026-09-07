import { useEffect, useRef } from 'react';
import { audioLevel } from './audio';
import type { VoicePhase } from './engine';

/** A sampled spherical field. Deformation comes from real input/output RMS. */
export function VoiceOrb({ analyser, phase }: { analyser: AnalyserNode | null; phase: VoicePhase }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const live = useRef({ analyser, phase }); live.current = { analyser, phase };
  useEffect(() => {
    const el = canvas.current!;
    const ctx = el.getContext('2d'); if (!ctx) return;
    const reduced = matchMedia('(prefers-reduced-motion: reduce)');
    let frame = 0, previous = 0, amplitude = 0;
    const draw = (time: number) => {
      frame = requestAnimationFrame(draw);
      if (document.hidden || time - previous < (reduced.matches ? 200 : 33)) return;
      previous = time;
      const size = el.clientWidth, dpr = Math.min(devicePixelRatio || 1, 2);
      if (el.width !== Math.round(size * dpr)) { el.width = Math.round(size * dpr); el.height = el.width; }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, size, size);
      const state = live.current;
      const level = state.analyser ? Math.min(1, audioLevel(state.analyser) * 6) : 0;
      amplitude += (level - amplitude) * 0.24;
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
        points.push({ x: size / 2 + x * radius * perspective * (1 + wave), y: size / 2 + tiltY * radius * perspective * (1 + wave), z: tiltZ, r: 0.6 + (tiltZ + 1) * 0.5 });
      }
      points.sort((a, b) => a.z - b.z);
      for (const p of points) {
        ctx.globalAlpha = 0.18 + (p.z + 1) * 0.37;
        ctx.beginPath(); ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2); ctx.fill();
      }
      ctx.globalAlpha = 0.25; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.ellipse(size / 2, size / 2, radius * 1.28, radius * 0.3, -0.32, 0, Math.PI * 2); ctx.stroke();
      ctx.globalAlpha = 1;
    };
    frame = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(frame);
  }, []);
  return <canvas ref={canvas} className="fs-voice__orb" aria-hidden="true" />;
}
