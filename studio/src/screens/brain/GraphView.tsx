import { AlertTriangle, Crosshair, Search, X } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { EmptyState, IconButton, Skeleton } from '../../components';
import { loadGraph, type BrainGraph, type GraphScope } from '../../adapters/brain';
import { countByKind, layout, nodeRadius, shorten, tooltip, viewModel, type GraphNode, type ViewModel } from '../../lib/graph';
import { t, tn } from '../../i18n';

/**
 * The vault's graph view (global by default; local when `center` is set) —
 * a `<canvas>` force layout built on `lib/graph.ts`'s deterministic
 * `layout()`/`viewModel()`, the same pure functions `screens/memory/
 * Provenance.tsx` draws its SVG from. A canvas here instead, because the
 * contract asks for one and because a vault can hold thousands of notes —
 * `capGraph` (inside `viewModel`) already keeps the drawing legible, and a
 * canvas repaints that many circles cheaper than that many DOM nodes.
 */

const KIND_ORDER = ['memory', 'personal', 'entity', 'project', 'objective', 'concept', 'daily', 'home', 'note', 'unresolved'] as const;

function kindColor(style: CSSStyleDeclaration, kind: string): string {
  return (style.getPropertyValue(`--fs-brain-kind-${kind}`) || style.getPropertyValue('--fs-brain-kind-unknown')).trim();
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
  const [hover, setHover] = useState<{ node: GraphNode; x: number; y: number } | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [size, setSize] = useState({ width: 0, height: compact ? 260 : 480 });

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

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const observer = new ResizeObserver(() => setSize((s) => ({ ...s, width: el.clientWidth })));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const model = useMemo<ViewModel | null>(() => (graph ? viewModel(graph, { kinds: [...off].length ? [...KIND_ORDER].filter((k) => !off.has(k)) : [], query, selected: center }) : null), [graph, off, query, center]);
  const counts = useMemo(() => (graph ? countByKind(graph.nodes) : {}), [graph]);
  const drawn = useMemo(() => (model ? layout(model.nodes, model.edges, { width: size.width || 480, height: size.height }) : null), [model, size]);
  const byId = useMemo(() => new Map((model?.nodes ?? []).map((n) => [n.id, n])), [model]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !drawn || !model) return;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = drawn.width * dpr;
    canvas.height = drawn.height * dpr;
    canvas.style.width = `${drawn.width}px`;
    canvas.style.height = `${drawn.height}px`;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, drawn.width, drawn.height);
    const style = getComputedStyle(canvas);
    const edgeColor = style.getPropertyValue('--fs-graph-edge').trim() || 'rgba(148,163,184,.35)';
    const labelColor = style.getPropertyValue('--fs-graph-label').trim() || '#e2e8f0';

    ctx.strokeStyle = edgeColor;
    ctx.lineWidth = 1;
    for (const e of model.edges) {
      const a = drawn.positions[e.from];
      const b = drawn.positions[e.to];
      if (!a || !b) continue;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
    }
    for (const n of model.nodes) {
      const p = drawn.positions[n.id];
      if (!p) continue;
      const r = nodeRadius(model.degrees[n.id]);
      ctx.beginPath();
      ctx.fillStyle = kindColor(style, n.kind);
      ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
      ctx.fill();
      if (n.id === center) {
        ctx.lineWidth = 2;
        ctx.strokeStyle = labelColor;
        ctx.stroke();
      }
      if (model.labels.has(n.id)) {
        ctx.fillStyle = labelColor;
        ctx.font = '11px sans-serif';
        ctx.fillText(shorten(n.label, 26), p.x + r + 4, p.y + 4);
      }
    }
  }, [drawn, model, center]);

  function hit(clientX: number, clientY: number): GraphNode | null {
    const canvas = canvasRef.current;
    if (!canvas || !drawn || !model) return null;
    const rect = canvas.getBoundingClientRect();
    const x = clientX - rect.left;
    const y = clientY - rect.top;
    let best: GraphNode | null = null;
    let bestD = Infinity;
    for (const n of model.nodes) {
      const p = drawn.positions[n.id];
      if (!p) continue;
      const r = nodeRadius(model.degrees[n.id]) + 3;
      const d = Math.hypot(p.x - x, p.y - y);
      if (d <= r && d < bestD) {
        bestD = d;
        best = n;
      }
    }
    return best;
  }

  function openNode(node: GraphNode) {
    if (node.kind === 'unresolved') return;
    if (scope === 'entities') onOpenEntity(node.id.replace(/^ent:/, ''));
    else onOpenNote(node.id);
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

      {loading && !graph && <Skeleton label={t('Reading the note graph')} count={3} height="60px" />}
      {!loading && Boolean(error) && !graph && (
        <EmptyState icon={AlertTriangle} tone="error" title={t('The graph could not be read')} body={String((error as Error)?.message ?? error)} />
      )}
      {graph && graph.nodes.length === 0 && <EmptyState icon={Search} title={t('Nothing to draw yet')} body={t('Write a note and link it with [[another note]] to see it here.')} headingLevel={3} />}
      {graph && model && graph.nodes.length > 0 && (
        <>
          {model.capped && (
            <p className="fs-notice" data-tone="warning">
              <AlertTriangle size={12} aria-hidden="true" /> {t('Showing {shown} of {total} nodes — narrow the filter to see the rest.', { shown: model.shown, total: model.filteredTotal })}
            </p>
          )}
          <div ref={containerRef} className="fs-brain__canvas-wrap">
            <canvas
              ref={canvasRef}
              className="fs-brain__canvas"
              role="img"
              aria-label={t('Note graph: {n} nodes, {e} edges', { n: model.nodes.length, e: model.edges.length })}
              onClick={(e) => {
                const node = hit(e.clientX, e.clientY);
                if (node) openNode(node);
              }}
              onMouseMove={(e) => {
                const node = hit(e.clientX, e.clientY);
                setHover(node ? { node, x: e.clientX, y: e.clientY } : null);
              }}
              onMouseLeave={() => setHover(null)}
            />
            {hover && (
              <div className="fs-brain__tooltip" style={{ left: hover.x + 12, top: hover.y + 12 }} role="status">
                {tooltip(hover.node)}
              </div>
            )}
            {center && local && (
              <div className="fs-brain__recenter">
                <IconButton icon={Crosshair} label={t('Centre on the canvas')} size="sm" onClick={() => setLocal(true)} />
              </div>
            )}
          </div>
          <p className="fs-muted">{model.query ? tn(model.matched.length, '{n} node matches', '{n} nodes match') : t('Showing all {n} nodes and {e} edges.', { n: model.shown, e: model.edges.length })}</p>
        </>
      )}
      {off.size > 0 && (
        <button type="button" className="fs-brain__clear-filter" onClick={() => setOff(new Set())}>
          <X size={12} aria-hidden="true" /> {t('Clear kind filters')}
        </button>
      )}
    </div>
  );
}
