import { AlertTriangle, Crosshair, Search, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { EmptyState, IconButton, Skeleton } from '../../components';
import { loadGraph, type BrainGraph, type GraphScope } from '../../adapters/brain';
import { countByKind, layout, shorten, tooltip, viewModel, type GraphNode, type ViewModel } from '../../lib/graph';
import {
  declutterLabels,
  fitTransform,
  hitTest,
  isSettled,
  labelBox,
  neighborsOf,
  nodeRadius,
  panBy,
  reconcileSim,
  screenToWorld,
  simPositions,
  stepSimulation,
  zoomAround,
  type SimState,
  type Transform,
} from './graphSim';
import { t, tn } from '../../i18n';

/**
 * The vault's graph view (global by default; local when `center` is set): a
 * `<canvas>` that starts from `lib/graph.ts`'s deterministic `layout()` —
 * so the very first frame is a stable, fit-to-view picture, not a jump cut
 * — and from there runs a live `graphSim.ts` force simulation, panned and
 * zoomed by the viewer. `viewModel()`'s `capGraph` still keeps a big vault's
 * drawing legible; the simulation only ever sees the (already capped) nodes
 * currently on screen.
 */

const KIND_ORDER = ['memory', 'personal', 'entity', 'project', 'objective', 'concept', 'daily', 'home', 'note', 'unresolved'] as const;

/** Above this zoom, every node earns a label — below it, only the ones
 *  `pickLabelIds` already picked (high degree, matched, selected). */
const LABEL_ZOOM_THRESHOLD = 1.6;
const CANVAS_PAD = 48;
/** A pointer that has not moved more than this since press is a click, not
 *  a drag — the same slack a touch UI gives a tap. */
const CLICK_SLACK = 4;

function kindColor(style: CSSStyleDeclaration, kind: string): string {
  return (style.getPropertyValue(`--fs-brain-kind-${kind}`) || style.getPropertyValue('--fs-brain-kind-unknown')).trim();
}

function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

export function GraphView({ center, onOpenNote, onOpenEntity, compact = false }: { center?: string; onOpenNote: (path: string) => void; onOpenEntity: (id: string) => void; compact?: boolean }) {
  const [scope, setScope] = useState<GraphScope>('notes');
  const [local, setLocal] = useState(Boolean(center));
  const [depth, setDepth] = useState(2);
  const [graph, setGraph] = useState<BrainGraph | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [query, setQuery] = useState('');
  const [off, setOff] = useState<Set<string>>(new Set());
  const [hoverId, setHoverId] = useState<string | null>(null);
  const [hoverPos, setHoverPos] = useState<{ x: number; y: number } | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });

  useEffect(() => {
    setLocal(Boolean(center));
  }, [center]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    loadGraph({ scope, center: local ? center : undefined, depth: local ? depth : undefined }, controller.signal)
      .then((g) => setGraph(g))
      .catch((e) => setError(e))
      .finally(() => setLoading(false));
    return () => controller.abort();
  }, [scope, local, center, depth]);

  // Track both dimensions (not just width) — the canvas has to fill the
  // centre panel's actual height, which changes with the window and with
  // the mobile/desktop layout, not just when the pane gets narrower. The
  // wrap div this observes is always mounted (see the render below), even
  // while the graph is still loading, so the observer attaches on the
  // FIRST render rather than missing it because nothing was there yet.
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const observer = new ResizeObserver(() => setSize({ width: el.clientWidth, height: el.clientHeight }));
    observer.observe(el);
    setSize({ width: el.clientWidth, height: el.clientHeight });
    return () => observer.disconnect();
  }, []);

  const model = useMemo<ViewModel | null>(() => (graph ? viewModel(graph, { kinds: [...off].length ? [...KIND_ORDER].filter((k) => !off.has(k)) : [], query, selected: center }) : null), [graph, off, query, center]);
  const counts = useMemo(() => (graph ? countByKind(graph.nodes) : {}), [graph]);
  const degrees = model?.degrees ?? {};
  const radiusFor = useCallback((id: string) => nodeRadius(degrees[id]), [degrees]);
  const nodeIds = useMemo(() => model?.nodes.map((n) => n.id) ?? [], [model]);

  /* ── the simulation: seeded from the deterministic layout, then live ── */
  const simRef = useRef<SimState | null>(null);
  const transformRef = useRef<Transform>({ scale: 1, tx: 0, ty: 0 });
  const rafRef = useRef<number | null>(null);
  const hotRef = useRef(false);
  const interactedRef = useRef(false);
  const reducedMotionRef = useRef(prefersReducedMotion());
  const draggingRef = useRef<string | null>(null);
  const panningRef = useRef<{ x: number; y: number } | null>(null);
  const pointerDownRef = useRef<{ x: number; y: number; moved: boolean } | null>(null);
  const hoverRef = useRef<string | null>(null);
  const modelRef = useRef<ViewModel | null>(null);
  modelRef.current = model;

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    const m = modelRef.current;
    const sim = simRef.current;
    if (!canvas || !m || !sim) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    const dpr = window.devicePixelRatio || 1;
    const cssWidth = canvas.clientWidth || size.width || 1;
    const cssHeight = canvas.clientHeight || size.height || 1;
    const wantW = Math.max(1, Math.round(cssWidth * dpr));
    const wantH = Math.max(1, Math.round(cssHeight * dpr));
    if (canvas.width !== wantW || canvas.height !== wantH) {
      canvas.width = wantW;
      canvas.height = wantH;
    }
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const transform = transformRef.current;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.translate(transform.tx, transform.ty);
    ctx.scale(transform.scale, transform.scale);

    const style = getComputedStyle(canvas);
    const edgeColor = style.getPropertyValue('--fs-graph-edge').trim() || 'rgba(148,163,184,.35)';
    const labelColor = style.getPropertyValue('--fs-graph-label').trim() || '#e2e8f0';
    const positions = simPositions(sim);
    const hovered = hoverRef.current;
    const neighbors = hovered ? neighborsOf(hovered, m.edges) : null;
    // A hovered node's own neighbourhood is what stays fully visible; every
    // other node dims down to a fifth of its normal opacity so the
    // highlighted subgraph reads immediately against a busy canvas.
    const dim = (id: string) => Boolean(hovered) && id !== hovered && !neighbors?.has(id);
    const DIMMED_ALPHA = 0.2;
    const showAllLabels = transform.scale >= LABEL_ZOOM_THRESHOLD;

    ctx.lineWidth = 1 / transform.scale;
    for (const e of m.edges) {
      const a = positions[e.from];
      const b = positions[e.to];
      if (!a || !b) continue;
      const faded = dim(e.from) && dim(e.to);
      ctx.globalAlpha = faded ? 0.12 : hovered ? 0.75 : 1;
      ctx.strokeStyle = edgeColor;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
    }
    ctx.globalAlpha = 1;

    // Which labels actually get drawn is decided once per frame, not node
    // by node: a label that would land on top of an already-placed one is
    // dropped, unless it belongs to the hovered/selected node or one of its
    // neighbours — those always show, even at the cost of a little overlap
    // among themselves, because that is exactly the context a hover is for.
    const required = new Set<string>();
    if (center) required.add(center);
    if (hovered) {
      required.add(hovered);
      for (const nb of neighbors ?? []) required.add(nb);
    }
    const candidates = m.nodes
      .filter((n) => positions[n.id] && !dim(n.id) && (showAllLabels || m.labels.has(n.id) || required.has(n.id)))
      .sort((a, b) => (degrees[b.id] || 0) - (degrees[a.id] || 0));
    const boxes = candidates.map((n) => labelBox(n.id, positions[n.id], radiusFor(n.id), shorten(n.label, 26), transform));
    const keepLabels = declutterLabels(boxes, required);
    for (const id of required) keepLabels.add(id);

    for (const n of m.nodes) {
      const p = positions[n.id];
      if (!p) continue;
      const r = radiusFor(n.id);
      const faded = dim(n.id);
      const isNeighbor = Boolean(hovered) && neighbors?.has(n.id);
      ctx.globalAlpha = faded ? DIMMED_ALPHA : 1;
      ctx.beginPath();
      ctx.fillStyle = kindColor(style, n.kind);
      ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
      ctx.fill();
      if (n.id === center || n.id === hovered) {
        ctx.lineWidth = (n.id === hovered ? 2.5 : 2) / transform.scale;
        ctx.strokeStyle = labelColor;
        ctx.stroke();
      } else if (isNeighbor) {
        // A lighter ring than the hovered node's own, so a hover's
        // neighbourhood reads as emphasised without competing with it.
        ctx.lineWidth = 1.25 / transform.scale;
        ctx.strokeStyle = labelColor;
        ctx.globalAlpha = 0.85;
        ctx.stroke();
        ctx.globalAlpha = faded ? DIMMED_ALPHA : 1;
      }
      if (keepLabels.has(n.id) && !faded) {
        ctx.fillStyle = labelColor;
        ctx.font = `${11 / transform.scale}px sans-serif`;
        ctx.fillText(shorten(n.label, 26), p.x + r + 4 / transform.scale, p.y + 4 / transform.scale);
      }
    }
    ctx.globalAlpha = 1;
  }, [center, degrees, radiusFor, size.width, size.height]);

  const scheduleFrame = useCallback(() => {
    if (rafRef.current !== null) return;
    const loop = () => {
      rafRef.current = null;
      const sim = simRef.current;
      const m = modelRef.current;
      if (!sim || !m) return;
      const width = size.width || 480;
      const height = size.height || 320;
      let energy = 0;
      if (hotRef.current && !reducedMotionRef.current) {
        energy = stepSimulation(sim, m.edges, { width, height }, radiusFor);
        // Re-fit every frame while the layout is still warming up or
        // untangling a dense cluster — a settling simulation keeps pushing
        // nodes outward from where they were first seeded, and fitting only
        // once at the start (or only on a double-click) is what left the
        // very first frame's fit stale by the time it visibly stopped
        // moving. Stops the moment the viewer pans, zooms or drags a node.
        if (!interactedRef.current) {
          transformRef.current = fitTransform(simPositions(sim), m.nodes.map((n) => n.id), { width, height }, CANVAS_PAD);
        }
      }
      draw();
      if (hotRef.current && !reducedMotionRef.current && !isSettled(energy)) {
        rafRef.current = requestAnimationFrame(loop);
      } else {
        hotRef.current = false;
      }
    };
    rafRef.current = requestAnimationFrame(loop);
  }, [draw, radiusFor, size.width, size.height]);

  const reheat = useCallback(() => {
    if (reducedMotionRef.current) {
      draw();
      return;
    }
    hotRef.current = true;
    scheduleFrame();
  }, [draw, scheduleFrame]);

  // A changed graph (new data, a different filter/scope/local note/depth)
  // reseeds the simulation from the deterministic layout and fits the view
  // to it again — even past the point the viewer already panned or zoomed,
  // because a different scope or filter is effectively a different picture,
  // not a continuation of the one they were framing. From here the
  // continuous re-fit in `scheduleFrame`'s loop takes back over until the
  // next pan, zoom or drag.
  useEffect(() => {
    if (!model) {
      simRef.current = null;
      return;
    }
    interactedRef.current = false;
    const width = size.width || 480;
    const height = size.height || 320;
    const seeded = layout(model.nodes, model.edges, { width, height });
    const ids = model.nodes.map((n) => n.id);
    // A node already in the simulation keeps its live position and velocity
    // (a filter tweak or a background refresh should not visibly jump); a
    // brand new one seeds from the deterministic layout.
    simRef.current = reconcileSim(simRef.current ?? { nodes: new Map() }, seeded.positions, ids);
    transformRef.current = fitTransform(seeded.positions, ids, { width, height }, CANVAS_PAD);
    reheat();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [model?.nodes.length, model?.edges.length, scope, local, center, depth, query, [...off].join(',')]);

  // A resize alone (no data change) keeps the current pan/zoom once the
  // viewer has touched the canvas; before that, it stays fit-to-view so the
  // graph still fills whatever space the centre panel ends up with.
  useEffect(() => {
    if (!model || interactedRef.current) {
      draw();
      return;
    }
    const width = size.width || 480;
    const height = size.height || 320;
    const positions = simRef.current ? simPositions(simRef.current) : {};
    transformRef.current = fitTransform(positions, model.nodes.map((n) => n.id), { width, height }, CANVAS_PAD);
    draw();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [size.width, size.height]);

  useEffect(
    () => () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
    },
    [],
  );

  function pointFromEvent(e: { clientX: number; clientY: number }): { x: number; y: number } {
    const canvas = canvasRef.current!;
    const rect = canvas.getBoundingClientRect();
    return { x: e.clientX - rect.left, y: e.clientY - rect.top };
  }

  function openNode(node: GraphNode) {
    if (node.kind === 'unresolved') return;
    if (scope === 'entities') onOpenEntity(node.id.replace(/^ent:/, ''));
    else onOpenNote(node.id);
  }

  function fitToView() {
    const m = modelRef.current;
    const sim = simRef.current;
    if (!m || !sim) return;
    const positions = simPositions(sim);
    transformRef.current = fitTransform(positions, m.nodes.map((n) => n.id), { width: size.width || 480, height: size.height || 320 }, CANVAS_PAD);
    draw();
  }

  const toggleKind = (kind: string) => setOff((s) => { const next = new Set(s); if (next.has(kind)) next.delete(kind); else next.add(kind); return next; });

  return (
    <div className="fs-brain__graph" data-compact={compact || undefined}>
      <div className="fs-brain__graph-toolbar">
        {!compact && (
          <div className="fs-seg" role="radiogroup" aria-label={t('Graph scope')}>
            <button type="button" role="radio" aria-checked={scope === 'notes'} onClick={() => setScope('notes')}>
              {t('Notes')}
            </button>
            <button type="button" role="radio" aria-checked={scope === 'entities'} onClick={() => setScope('entities')}>
              {t('Entities')}
            </button>
          </div>
        )}
        <label className="fs-search fs-brain__graph-search">
          <Search size={13} aria-hidden="true" />
          <input type="search" value={query} placeholder={t('Filter the graph…')} onChange={(e) => setQuery(e.target.value)} aria-label={t('Filter the graph')} />
        </label>
        {center && (
          <label className="fs-brain__local-toggle">
            <input type="checkbox" checked={local} onChange={(e) => setLocal(e.target.checked)} />
            {t('Local to this note')}
          </label>
        )}
        {local && center && (
          <label className="fs-brain__depth">
            {t('Depth')}
            <input type="range" min={1} max={3} value={depth} onChange={(e) => setDepth(Number(e.target.value))} aria-label={t('Local graph depth')} />
            <span>{depth}</span>
          </label>
        )}
      </div>

      <div className="fs-brain__legend" role="group" aria-label={t('Filter by kind')}>
        {KIND_ORDER.filter((k) => counts[k]).map((kind) => (
          <button key={kind} type="button" className="fs-brain__legend-item" data-kind={kind} data-off={off.has(kind) || undefined} onClick={() => toggleKind(kind)}>
            <span className="fs-brain__legend-dot" data-kind={kind} aria-hidden="true" />
            {kind} <b>{counts[kind]}</b>
          </button>
        ))}
      </div>

      {graph && model && model.capped && (
        <p className="fs-notice" data-tone="warning">
          <AlertTriangle size={12} aria-hidden="true" /> {t('Showing {shown} of {total} nodes — narrow the filter to see the rest.', { shown: model.shown, total: model.filteredTotal })}
        </p>
      )}
      {/* Always mounted — even while loading or empty — so the ResizeObserver
          above attaches on the very first render instead of missing a div
          that would otherwise only appear once data arrives. */}
      <div ref={containerRef} className="fs-brain__canvas-wrap">
        {loading && !graph && <Skeleton label={t('Reading the note graph')} count={3} height="60px" />}
        {!loading && Boolean(error) && !graph && (
          <EmptyState icon={AlertTriangle} tone="error" title={t('The graph could not be read')} body={String((error as Error)?.message ?? error)} />
        )}
        {graph && graph.nodes.length === 0 && <EmptyState icon={Search} title={t('Nothing to draw yet')} body={t('Write a note and link it with [[another note]] to see it here.')} headingLevel={3} />}
        {graph && model && graph.nodes.length > 0 && (
          <>
            <canvas
              ref={canvasRef}
              className="fs-brain__canvas"
              role="img"
              aria-label={t('Note graph: {n} nodes, {e} edges', { n: model.nodes.length, e: model.edges.length })}
              onPointerDown={(e) => {
                const canvas = canvasRef.current;
                if (!canvas) return;
                const point = pointFromEvent(e);
                pointerDownRef.current = { x: point.x, y: point.y, moved: false };
                const hitId = simRef.current ? hitTest(nodeIds, simPositions(simRef.current), radiusFor, transformRef.current, point) : null;
                canvas.setPointerCapture(e.pointerId);
                if (hitId) {
                  draggingRef.current = hitId;
                  const node = simRef.current!.nodes.get(hitId)!;
                  node.fixed = true;
                  interactedRef.current = true;
                  reheat();
                } else {
                  panningRef.current = point;
                }
                canvas.style.cursor = 'grabbing';
              }}
              onPointerMove={(e) => {
                const point = pointFromEvent(e);
                const down = pointerDownRef.current;
                if (down && Math.hypot(point.x - down.x, point.y - down.y) > CLICK_SLACK) down.moved = true;
                if (draggingRef.current && simRef.current) {
                  const world = screenToWorld(transformRef.current, point);
                  const node = simRef.current.nodes.get(draggingRef.current);
                  if (node) {
                    node.x = world.x;
                    node.y = world.y;
                    node.vx = 0;
                    node.vy = 0;
                  }
                  reheat();
                  return;
                }
                if (panningRef.current) {
                  const last = panningRef.current;
                  transformRef.current = panBy(transformRef.current, point.x - last.x, point.y - last.y);
                  panningRef.current = point;
                  interactedRef.current = true;
                  draw();
                  return;
                }
                const hitId = simRef.current ? hitTest(nodeIds, simPositions(simRef.current), radiusFor, transformRef.current, point) : null;
                if (hitId !== hoverRef.current) {
                  hoverRef.current = hitId;
                  setHoverId(hitId);
                  draw();
                }
                if (hitId) setHoverPos({ x: e.clientX, y: e.clientY });
                const canvas = canvasRef.current;
                if (canvas) canvas.style.cursor = hitId ? 'pointer' : 'grab';
              }}
              onPointerUp={(e) => {
                const canvas = canvasRef.current;
                if (canvas?.hasPointerCapture(e.pointerId)) canvas.releasePointerCapture(e.pointerId);
                if (draggingRef.current && simRef.current) {
                  const node = simRef.current.nodes.get(draggingRef.current);
                  if (node) node.fixed = false;
                  draggingRef.current = null;
                  reheat();
                }
                panningRef.current = null;
                const down = pointerDownRef.current;
                pointerDownRef.current = null;
                let openedId: string | null = null;
                if (down && !down.moved) {
                  const point = pointFromEvent(e);
                  openedId = simRef.current ? hitTest(nodeIds, simPositions(simRef.current), radiusFor, transformRef.current, point) : null;
                  const node = openedId ? model.nodes.find((n) => n.id === openedId) : null;
                  if (node) openNode(node);
                }
                if (canvas) canvas.style.cursor = openedId ? 'pointer' : 'grab';
              }}
              onPointerLeave={() => {
                if (!draggingRef.current) {
                  hoverRef.current = null;
                  setHoverId(null);
                  setHoverPos(null);
                  draw();
                }
              }}
              onWheel={(e) => {
                e.preventDefault();
                const point = pointFromEvent(e);
                const factor = Math.exp(-e.deltaY * 0.0015);
                transformRef.current = zoomAround(transformRef.current, point, factor);
                interactedRef.current = true;
                draw();
              }}
              onDoubleClick={() => {
                interactedRef.current = false;
                fitToView();
              }}
            />
            {hoverId && hoverPos && (
              <div className="fs-brain__tooltip" style={{ left: hoverPos.x + 12, top: hoverPos.y + 12 }} role="status">
                {tooltip(model.nodes.find((n) => n.id === hoverId) ?? { id: hoverId, kind: '', label: hoverId, detail: '', meta: {} })}
              </div>
            )}
            <div className="fs-brain__recenter">
              <IconButton icon={Crosshair} label={t('Centre on the canvas')} size="sm" onClick={() => { interactedRef.current = false; fitToView(); }} />
            </div>
          </>
        )}
      </div>
      {graph && model && graph.nodes.length > 0 && (
        <p className="fs-muted">{model.query ? tn(model.matched.length, '{n} node matches', '{n} nodes match') : t('Showing all {n} nodes and {e} edges.', { n: model.shown, e: model.edges.length })}</p>
      )}
      {off.size > 0 && (
        <button type="button" className="fs-brain__clear-filter" onClick={() => setOff(new Set())}>
          <X size={12} aria-hidden="true" /> {t('Clear kind filters')}
        </button>
      )}
    </div>
  );
}
