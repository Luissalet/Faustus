import { AlertTriangle, BadgeCheck, Crosshair, Search } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Button, EmptyState, Skeleton } from '../../components';
import { GALAXY_KINDS, loadGalaxy, type Galaxy, type GalaxyKind, type GalaxyNode } from '../../adapters/brainReview';
import { clampCamera, fitCamera, pick, viewProjection, type Camera } from './galaxyMath';
import { t } from '../../i18n';

/**
 * The brain as a galaxy: projects, sessions, agents, outputs and entities as
 * five navigable point clouds drawn with plain WebGL (one point program, one
 * line program, no engine). Drag to orbit, wheel to zoom, click a point to
 * see what it is, double-click to fly to it; the legend chips show or hide a
 * cloud and the search box lights up the points whose name matches.
 */

const KIND_COLOR: Record<GalaxyKind, [number, number, number]> = {
  project: [0.93, 0.66, 0.29],
  agent: [0.48, 0.64, 0.97],
  session: [0.55, 0.84, 0.79],
  output: [0.78, 0.63, 0.96],
  entity: [0.95, 0.56, 0.68],
};

function kindLabel(kind: GalaxyKind): string {
  switch (kind) {
    case 'project': return t('Projects');
    case 'agent': return t('Agents');
    case 'session': return t('Sessions');
    case 'output': return t('Outputs');
    default: return t('Entities');
  }
}

function hex(c: [number, number, number]): string {
  return `rgb(${Math.round(c[0] * 255)}, ${Math.round(c[1] * 255)}, ${Math.round(c[2] * 255)})`;
}

const POINT_VS = `
attribute vec3 aPos;
attribute float aSize;
attribute vec4 aColor;
uniform mat4 uVP;
uniform float uHeight;
uniform float uDpr;
varying vec4 vColor;
void main() {
  vec4 clip = uVP * vec4(aPos, 1.0);
  gl_Position = clip;
  float px = aSize * uHeight * 0.02 / max(clip.w, 0.05);
  gl_PointSize = clamp(px, 2.5, 34.0) * uDpr;
  vColor = aColor;
}`;
const POINT_FS = `
precision mediump float;
varying vec4 vColor;
void main() {
  float d = length(gl_PointCoord - vec2(0.5));
  if (d > 0.5) discard;
  float a = vColor.a * smoothstep(0.5, 0.32, d);
  gl_FragColor = vec4(vColor.rgb, a);
}`;
const LINE_VS = `
attribute vec3 aPos;
uniform mat4 uVP;
void main() { gl_Position = uVP * vec4(aPos, 1.0); }`;
const LINE_FS = `
precision mediump float;
uniform vec4 uColor;
void main() { gl_FragColor = uColor; }`;

function compile(gl: WebGLRenderingContext, vs: string, fs: string): WebGLProgram | null {
  const make = (type: number, src: string) => {
    const shader = gl.createShader(type);
    if (!shader) return null;
    gl.shaderSource(shader, src);
    gl.compileShader(shader);
    return gl.getShaderParameter(shader, gl.COMPILE_STATUS) ? shader : null;
  };
  const v = make(gl.VERTEX_SHADER, vs);
  const f = make(gl.FRAGMENT_SHADER, fs);
  const program = gl.createProgram();
  if (!v || !f || !program) return null;
  gl.attachShader(program, v);
  gl.attachShader(program, f);
  gl.linkProgram(program);
  return gl.getProgramParameter(program, gl.LINK_STATUS) ? program : null;
}

interface GlState {
  gl: WebGLRenderingContext;
  point: WebGLProgram;
  line: WebGLProgram;
  posBuf: WebGLBuffer;
  sizeBuf: WebGLBuffer;
  colorBuf: WebGLBuffer;
  linkBuf: WebGLBuffer;
  linkCount: number;
}

export function GalaxyView({ onOpenEntity }: { onOpenEntity: (entityId: string) => void }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const glRef = useRef<GlState | null>(null);
  const cameraRef = useRef<Camera>(fitCamera(2));
  const dirty = useRef(true);
  const spin = useRef(true);

  const [galaxy, setGalaxy] = useState<Galaxy | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [glFailed, setGlFailed] = useState(false);
  const [hidden, setHidden] = useState<Set<GalaxyKind>>(new Set());
  const [search, setSearch] = useState('');
  const [hover, setHover] = useState<{ index: number; x: number; y: number } | null>(null);
  const [selected, setSelected] = useState<number>(-1);

  useEffect(() => {
    const controller = new AbortController();
    loadGalaxy(400, controller.signal)
      .then((g) => {
        setGalaxy(g);
        cameraRef.current = fitCamera(g.radius);
        dirty.current = true;
      })
      .catch((e) => {
        if ((e as Error)?.name !== 'AbortError') setError(e);
      });
    return () => controller.abort();
  }, []);

  const nodes = galaxy?.nodes ?? [];
  const positions = useMemo(() => nodes.map((n) => n.pos), [nodes]);
  const sizes = useMemo(() => nodes.map((n) => n.size), [nodes]);
  const needle = search.trim().toLowerCase();
  const visible = useMemo(() => nodes.map((n) => !hidden.has(n.kind)), [nodes, hidden]);
  const matches = useMemo(() => nodes.map((n) => needle !== '' && n.label.toLowerCase().includes(needle)), [nodes, needle]);
  const matchCount = matches.filter(Boolean).length;

  // Per-point colour: kind colour, dimmed when searching and not matching,
  // brightened when selected or hovered, transparent when its cloud is hidden.
  const colors = useMemo(() => {
    const out = new Float32Array(nodes.length * 4);
    nodes.forEach((n, i) => {
      const base = KIND_COLOR[n.kind];
      let alpha = visible[i] ? 0.85 : 0;
      if (visible[i] && needle !== '') alpha = matches[i] ? 1 : 0.12;
      let boost = 0;
      if (i === selected) { alpha = 1; boost = 0.45; }
      if (hover && hover.index === i) { alpha = 1; boost = Math.max(boost, 0.3); }
      out.set([Math.min(1, base[0] + boost), Math.min(1, base[1] + boost), Math.min(1, base[2] + boost), alpha], i * 4);
    });
    return out;
  }, [nodes, visible, matches, needle, selected, hover]);

  // Upload geometry whenever the galaxy changes.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !galaxy) return undefined;
    if (!glRef.current) {
      const gl = (canvas.getContext('webgl', { antialias: true, alpha: true }) || canvas.getContext('experimental-webgl')) as WebGLRenderingContext | null;
      const point = gl && compile(gl, POINT_VS, POINT_FS);
      const line = gl && compile(gl, LINE_VS, LINE_FS);
      const posBuf = gl?.createBuffer();
      const sizeBuf = gl?.createBuffer();
      const colorBuf = gl?.createBuffer();
      const linkBuf = gl?.createBuffer();
      if (!gl || !point || !line || !posBuf || !sizeBuf || !colorBuf || !linkBuf) {
        setGlFailed(true);
        return undefined;
      }
      gl.enable(gl.BLEND);
      gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
      glRef.current = { gl, point, line, posBuf, sizeBuf, colorBuf, linkBuf, linkCount: 0 };
    }
    const g = glRef.current;
    const pos = new Float32Array(galaxy.nodes.length * 3);
    galaxy.nodes.forEach((n, i) => pos.set(n.pos, i * 3));
    g.gl.bindBuffer(g.gl.ARRAY_BUFFER, g.posBuf);
    g.gl.bufferData(g.gl.ARRAY_BUFFER, pos, g.gl.STATIC_DRAW);
    g.gl.bindBuffer(g.gl.ARRAY_BUFFER, g.sizeBuf);
    g.gl.bufferData(g.gl.ARRAY_BUFFER, new Float32Array(galaxy.nodes.map((n) => n.size)), g.gl.STATIC_DRAW);
    const lines = new Float32Array(galaxy.links.length * 6);
    galaxy.links.forEach(([a, b], i) => {
      lines.set(galaxy.nodes[a].pos, i * 6);
      lines.set(galaxy.nodes[b].pos, i * 6 + 3);
    });
    g.gl.bindBuffer(g.gl.ARRAY_BUFFER, g.linkBuf);
    g.gl.bufferData(g.gl.ARRAY_BUFFER, lines, g.gl.STATIC_DRAW);
    g.linkCount = galaxy.links.length * 2;
    dirty.current = true;
    return undefined;
  }, [galaxy]);

  useEffect(() => {
    const g = glRef.current;
    if (!g) return;
    g.gl.bindBuffer(g.gl.ARRAY_BUFFER, g.colorBuf);
    g.gl.bufferData(g.gl.ARRAY_BUFFER, colors, g.gl.DYNAMIC_DRAW);
    dirty.current = true;
  }, [colors]);

  const size = useCallback(() => {
    const canvas = canvasRef.current;
    const wrap = wrapRef.current;
    if (!canvas || !wrap) return { width: 1, height: 1, dpr: 1 };
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const width = Math.max(1, wrap.clientWidth);
    const height = Math.max(1, wrap.clientHeight);
    if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
      dirty.current = true;
    }
    return { width, height, dpr };
  }, []);

  // Render loop: draws only when something changed or the camera drifts.
  useEffect(() => {
    const reduced = typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduced) spin.current = false;
    let frame = 0;
    const draw = () => {
      frame = requestAnimationFrame(draw);
      const g = glRef.current;
      if (!g || !galaxy) return;
      if (spin.current) {
        cameraRef.current = { ...cameraRef.current, yaw: cameraRef.current.yaw + 0.0016 };
        dirty.current = true;
      }
      const { width, height, dpr } = size();
      if (!dirty.current) return;
      dirty.current = false;
      const { gl } = g;
      gl.viewport(0, 0, Math.round(width * dpr), Math.round(height * dpr));
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      const vp = viewProjection(cameraRef.current, width / height);

      if (g.linkCount > 0 && g.linkCount <= 8000) {
        gl.useProgram(g.line);
        gl.uniformMatrix4fv(gl.getUniformLocation(g.line, 'uVP'), false, vp);
        gl.uniform4f(gl.getUniformLocation(g.line, 'uColor'), 0.6, 0.65, 0.8, 0.13);
        const loc = gl.getAttribLocation(g.line, 'aPos');
        gl.bindBuffer(gl.ARRAY_BUFFER, g.linkBuf);
        gl.enableVertexAttribArray(loc);
        gl.vertexAttribPointer(loc, 3, gl.FLOAT, false, 0, 0);
        gl.drawArrays(gl.LINES, 0, g.linkCount);
        gl.disableVertexAttribArray(loc);
      }

      gl.useProgram(g.point);
      gl.uniformMatrix4fv(gl.getUniformLocation(g.point, 'uVP'), false, vp);
      gl.uniform1f(gl.getUniformLocation(g.point, 'uHeight'), height);
      gl.uniform1f(gl.getUniformLocation(g.point, 'uDpr'), dpr);
      const bind = (name: string, buf: WebGLBuffer, n: number) => {
        const loc = gl.getAttribLocation(g.point, name);
        gl.bindBuffer(gl.ARRAY_BUFFER, buf);
        gl.enableVertexAttribArray(loc);
        gl.vertexAttribPointer(loc, n, gl.FLOAT, false, 0, 0);
        return loc;
      };
      const locs = [bind('aPos', g.posBuf, 3), bind('aSize', g.sizeBuf, 1), bind('aColor', g.colorBuf, 4)];
      gl.drawArrays(gl.POINTS, 0, galaxy.nodes.length);
      locs.forEach((l) => gl.disableVertexAttribArray(l));
    };
    frame = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(frame);
  }, [galaxy, size]);

  const hit = useCallback((clientX: number, clientY: number) => {
    const canvas = canvasRef.current;
    if (!canvas || !galaxy) return { index: -1, x: 0, y: 0 };
    const box = canvas.getBoundingClientRect();
    const x = clientX - box.left;
    const y = clientY - box.top;
    const vp = viewProjection(cameraRef.current, box.width / box.height);
    return { index: pick(positions, sizes, visible, vp, box.width, box.height, x, y), x, y };
  }, [galaxy, positions, sizes, visible]);

  const drag = useRef<{ x: number; y: number; moved: number } | null>(null);

  function onPointerDown(e: React.PointerEvent) {
    (e.target as Element).setPointerCapture?.(e.pointerId);
    drag.current = { x: e.clientX, y: e.clientY, moved: 0 };
    spin.current = false;
  }
  function onPointerMove(e: React.PointerEvent) {
    const d = drag.current;
    if (d) {
      const dx = e.clientX - d.x;
      const dy = e.clientY - d.y;
      d.moved += Math.abs(dx) + Math.abs(dy);
      d.x = e.clientX;
      d.y = e.clientY;
      const c = cameraRef.current;
      cameraRef.current = clampCamera({ ...c, yaw: c.yaw - dx * 0.006, pitch: c.pitch + dy * 0.006 });
      dirty.current = true;
      return;
    }
    const h = hit(e.clientX, e.clientY);
    setHover(h.index < 0 ? null : { index: h.index, x: h.x, y: h.y });
  }
  function onPointerUp(e: React.PointerEvent) {
    const d = drag.current;
    drag.current = null;
    if (d && d.moved < 5) {
      const h = hit(e.clientX, e.clientY);
      setSelected(h.index);
    }
  }
  function onWheel(e: React.WheelEvent) {
    const c = cameraRef.current;
    cameraRef.current = clampCamera({ ...c, distance: c.distance * (e.deltaY > 0 ? 1.1 : 0.9) });
    spin.current = false;
    dirty.current = true;
  }
  function onDoubleClick(e: React.MouseEvent) {
    const h = hit(e.clientX, e.clientY);
    if (h.index >= 0) {
      cameraRef.current = clampCamera({ ...cameraRef.current, target: positions[h.index], distance: 1.2 });
      dirty.current = true;
    }
  }
  function onKeyDown(e: React.KeyboardEvent) {
    const c = cameraRef.current;
    const step = 0.12;
    const next = { ...c };
    if (e.key === 'ArrowLeft') next.yaw -= step;
    else if (e.key === 'ArrowRight') next.yaw += step;
    else if (e.key === 'ArrowUp') next.pitch += step;
    else if (e.key === 'ArrowDown') next.pitch -= step;
    else if (e.key === '+' || e.key === '=') next.distance *= 0.9;
    else if (e.key === '-') next.distance /= 0.9;
    else return;
    e.preventDefault();
    spin.current = false;
    cameraRef.current = clampCamera(next);
    dirty.current = true;
  }

  function resetView() {
    cameraRef.current = fitCamera(galaxy?.radius ?? 2);
    spin.current = true;
    setSelected(-1);
    dirty.current = true;
  }

  function toggleKind(kind: GalaxyKind) {
    setHidden((prev) => {
      const next = new Set(prev);
      if (next.has(kind)) next.delete(kind);
      else next.add(kind);
      return next;
    });
  }

  if (!galaxy && !error) return <Skeleton label={t('Reading the galaxy')} count={3} height="80px" />;
  if (!galaxy) return <EmptyState tone="error" title={t('The galaxy could not be read')} body={String((error as Error)?.message ?? error)} />;

  const picked: GalaxyNode | null = selected >= 0 ? nodes[selected] ?? null : null;
  const hovered: GalaxyNode | null = hover ? nodes[hover.index] ?? null : null;
  const total = nodes.length;

  return (
    <section className="fs-brain__galaxy" data-testid="brain-galaxy" aria-label={t('Galaxy')}>
      <div className="fs-brain__galaxy-toolbar">
        <div className="fs-brain__legend" role="group" aria-label={t('Clouds')}>
          {GALAXY_KINDS.map((kind) => (
            <button
              key={kind}
              type="button"
              className="fs-brain__galaxy-chip"
              aria-pressed={!hidden.has(kind)}
              onClick={() => toggleKind(kind)}
              data-testid={`brain-galaxy-kind-${kind}`}
            >
              <span className="fs-brain__galaxy-dot" style={{ background: hex(KIND_COLOR[kind]) }} aria-hidden="true" />
              {kindLabel(kind)} <span className="fs-muted">{galaxy.counts[kind]}</span>
            </button>
          ))}
        </div>
        <label className="fs-brain__graph-search">
          <Search size={13} aria-hidden="true" />
          <input value={search} onChange={(e) => { setSearch(e.target.value); dirty.current = true; }} placeholder={t('Light up by name')} aria-label={t('Light up by name')} data-testid="brain-galaxy-search" />
        </label>
        {needle !== '' && <span className="fs-muted" data-testid="brain-galaxy-matches">{t('{n} match', { n: matchCount })}</span>}
        <Button variant="ghost" size="sm" icon={Crosshair} label={t('Reset view')} onClick={resetView} testId="brain-galaxy-reset" />
      </div>

      {galaxy.warnings.length > 0 && (
        <p className="fs-notice" data-tone="warning"><AlertTriangle size={12} aria-hidden="true" /> {galaxy.warnings.join(' · ')}</p>
      )}

      {total === 0 ? (
        <EmptyState title={t('Nothing to show yet')} body={t('Projects, chats, agents, outputs and entities appear here as soon as they exist.')} />
      ) : (
        <div className="fs-brain__galaxy-stage" ref={wrapRef}>
          <canvas
            ref={canvasRef}
            className="fs-brain__galaxy-canvas"
            tabIndex={0}
            role="img"
            aria-label={t('Galaxy of {n} points. Drag to orbit, scroll to zoom, arrow keys to rotate.', { n: total })}
            data-testid="brain-galaxy-canvas"
            data-points={total}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            onPointerLeave={() => setHover(null)}
            onWheel={onWheel}
            onDoubleClick={onDoubleClick}
            onKeyDown={onKeyDown}
          />
          {glFailed && <p className="fs-notice fs-brain__galaxy-fallback" data-tone="warning">{t('WebGL is not available here, so the points cannot be drawn.')}</p>}
          {hovered && hover && (
            <div className="fs-brain__galaxy-tip" style={{ left: hover.x + 12, top: hover.y + 12 }} data-testid="brain-galaxy-tip">
              <strong>{hovered.label}</strong> <span className="fs-muted">{kindLabel(hovered.kind)}</span>
            </div>
          )}
          {picked && (
            <aside className="fs-brain__galaxy-card" data-testid="brain-galaxy-card">
              <h4>{picked.label}</h4>
              <p className="fs-muted">{kindLabel(picked.kind)}</p>
              {picked.ratified && <p><span className="fs-badge fs-brain__ratified"><BadgeCheck size={12} aria-hidden="true" /> {t('Ratified')}</span></p>}
              <p className="fs-muted">{t('{n} links', { n: galaxy.links.filter(([a, b]) => a === selected || b === selected).length })}</p>
              {picked.kind === 'entity' && picked.ref.startsWith('ent:') && (
                <Button variant="secondary" size="sm" label={t('Open entity')} onClick={() => onOpenEntity(picked.ref.slice(4))} testId="brain-galaxy-open" />
              )}
            </aside>
          )}
        </div>
      )}
    </section>
  );
}

