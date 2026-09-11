import { useMemo, useRef, useState } from 'react';
import { t } from '../../i18n';

/**
 * W2-E (CMP-07, `docs/api/topology.md` §Simulate) — the shared graph both
 * `WorkflowsScreen` (design/simulate) and `RunOverlay` (execute) draw on:
 * one node per `WorkflowNode`, one edge per `needs` entry. No new
 * dependency (ADP-14's own limit, still true here): a hand-drawn, layered
 * SVG, same reasoning as `components/MermaidView.tsx` choosing not to pull
 * in a renderer for two call sites.
 *
 * W3-F (CONTRATO_W3.md): the layered positions below are the DEFAULT —
 * `layout` (node_id -> {x,y}) overrides them per node, and when the caller
 * passes `onNodeMove` a node can be dragged to a hand-adjusted spot (pointer
 * events converted through the SVG's own `getScreenCTM`, so it stays correct
 * under the `viewBox` scaling, no extra library). Dragging never changes
 * `needs`/edges — it is purely where a node is DRAWN, the same "visual only,
 * never part of what runs" contract `adapters/topology.ts::
 * exportWorkflowDefinition`'s docstring already describes for `layout`.
 * `RunOverlay` renders with no `onNodeMove`, so a run in progress stays
 * static — nothing to persist mid-execution.
 */

export interface PlanGraphNode {
  id: string;
  type: string;
  title: string;
  needs: string[];
}

/** What colours a node/edge. `undefined` is the neutral "not marked"
 *  state — Design mode draws every node the same until something (a
 *  simulation, a real run) says otherwise. */
export type NodeMark =
  | 'activated'
  | 'not_taken'
  | 'awaiting_choice'
  | 'human_wait'
  | 'run_completed'
  | 'run_failed'
  | 'run_running'
  | 'run_paused'
  | 'run_pending'
  | undefined;

export interface PlanGraphProps {
  nodes: PlanGraphNode[];
  marks?: Record<string, NodeMark>;
  selectedNodeId?: string | null;
  onSelectNode: (id: string) => void;
  emptyLabel?: string;
  /** Hand-adjusted positions, keyed by node id — overrides the computed
   *  layered position for the ids present. Unknown/missing ids fall back
   *  to the computed layout, so a stale entry (a node the definition no
   *  longer has) is silently ignored rather than breaking the draw. */
  layout?: Record<string, { x: number; y: number }>;
  /** Present only where dragging should be allowed (Design mode). Called
   *  once per drag, on release, with the node's final position — never on
   *  every pointer-move, so a caller that persists this (localStorage,
   *  W3-F) is not hammered mid-drag. */
  onNodeMove?: (id: string, pos: { x: number; y: number }) => void;
}

/** Client (pointer) coordinates -> the SVG's own user-unit coordinate
 *  space, via its screen CTM — correct regardless of how the `viewBox`
 *  scales the element on screen (responsive width), no extra library. */
function toSvgPoint(svg: SVGSVGElement, clientX: number, clientY: number): { x: number; y: number } {
  const pt = svg.createSVGPoint();
  pt.x = clientX;
  pt.y = clientY;
  const ctm = svg.getScreenCTM();
  if (!ctm) return { x: clientX, y: clientY };
  const transformed = pt.matrixTransform(ctm.inverse());
  return { x: transformed.x, y: transformed.y };
}

/** Depth = layers from a root. A dangling/cyclic reference (only possible
 *  on a hand-built or mid-edit definition; `WorkflowDefinition.parse()`
 *  already refuses both) never hangs the drawing: a node mid-cycle is
 *  simply pinned at depth 0 the moment it is revisited, same defensive
 *  posture `src/workflows/simulate.py` takes on the backend. */
function layerDepths(nodes: PlanGraphNode[]): Map<string, number> {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const depth = new Map<string, number>();
  const visiting = new Set<string>();
  function depthOf(id: string): number {
    if (depth.has(id)) return depth.get(id) as number;
    if (visiting.has(id)) return 0;
    visiting.add(id);
    const node = byId.get(id);
    const needs = (node?.needs ?? []).filter((d) => byId.has(d) && d !== id);
    const value = needs.length === 0 ? 0 : 1 + Math.max(...needs.map(depthOf));
    visiting.delete(id);
    depth.set(id, value);
    return value;
  }
  for (const n of nodes) depthOf(n.id);
  return depth;
}

const COL_WIDTH = 216;
const ROW_HEIGHT = 72;
const NODE_W = 176;
const NODE_H = 48;

export function PlanGraph({ nodes, marks, selectedNodeId, onSelectNode, emptyLabel, layout, onNodeMove }: PlanGraphProps) {
  const nodeRefs = useRef(new Map<string, SVGGElement>());
  const svgRef = useRef<SVGSVGElement>(null);
  const dragOffset = useRef({ x: 0, y: 0 });
  const dragMoved = useRef(false);
  const [dragState, setDragState] = useState<{ id: string; x: number; y: number } | null>(null);

  const depths = useMemo(() => layerDepths(nodes), [nodes]);
  const layers = useMemo(() => {
    const byDepth = new Map<number, PlanGraphNode[]>();
    for (const n of nodes) {
      const d = depths.get(n.id) ?? 0;
      const list = byDepth.get(d) ?? [];
      list.push(n);
      byDepth.set(d, list);
    }
    return [...byDepth.entries()].sort(([a], [b]) => a - b).map(([, list]) => list);
  }, [nodes, depths]);

  const layerIndexOf = useMemo(() => {
    const m = new Map<string, { col: number; row: number }>();
    layers.forEach((layer, col) => layer.forEach((n, row) => m.set(n.id, { col, row })));
    return m;
  }, [layers]);

  const basePositions = useMemo(() => {
    const pos = new Map<string, { x: number; y: number }>();
    layers.forEach((layer, col) => {
      layer.forEach((n, row) => pos.set(n.id, { x: col * COL_WIDTH + 20, y: row * ROW_HEIGHT + 20 }));
    });
    return pos;
  }, [layers]);

  const positions = useMemo(() => {
    const pos = new Map(basePositions);
    if (layout) {
      for (const [id, p] of Object.entries(layout)) {
        if (pos.has(id) && Number.isFinite(p?.x) && Number.isFinite(p?.y)) pos.set(id, { x: p.x, y: p.y });
      }
    }
    if (dragState && pos.has(dragState.id)) pos.set(dragState.id, { x: dragState.x, y: dragState.y });
    return pos;
  }, [basePositions, layout, dragState]);

  if (nodes.length === 0) {
    return <p className="fs-plan__empty">{emptyLabel ?? t('No nodes to draw yet.')}</p>;
  }

  const byId = new Map(nodes.map((n) => [n.id, n]));
  const maxRows = Math.max(1, ...layers.map((l) => l.length));
  const allX = [...positions.values()].map((p) => p.x);
  const allY = [...positions.values()].map((p) => p.y);
  const width = Math.max(COL_WIDTH * Math.max(layers.length, 1) + 20, 280, Math.max(0, ...allX) + NODE_W + 20);
  const height = Math.max(ROW_HEIGHT * maxRows + 20, 120, Math.max(0, ...allY) + NODE_H + 20);

  function focusNode(id?: string) {
    if (id) nodeRefs.current.get(id)?.focus();
  }

  function onNodePointerDown(e: React.PointerEvent<SVGGElement>, node: PlanGraphNode) {
    if (!onNodeMove || !svgRef.current) return;
    const p = positions.get(node.id);
    if (!p) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    const start = toSvgPoint(svgRef.current, e.clientX, e.clientY);
    dragOffset.current = { x: start.x - p.x, y: start.y - p.y };
    dragMoved.current = false;
    setDragState({ id: node.id, x: p.x, y: p.y });
  }

  function onNodePointerMove(e: React.PointerEvent<SVGGElement>) {
    if (!dragState || !svgRef.current) return;
    const cur = toSvgPoint(svgRef.current, e.clientX, e.clientY);
    const nx = cur.x - dragOffset.current.x;
    const ny = cur.y - dragOffset.current.y;
    if (Math.abs(nx - dragState.x) > 1 || Math.abs(ny - dragState.y) > 1) dragMoved.current = true;
    setDragState({ id: dragState.id, x: nx, y: ny });
  }

  function onNodePointerUp() {
    if (!dragState) return;
    onNodeMove?.(dragState.id, { x: dragState.x, y: dragState.y });
    setDragState(null);
  }

  function onNodeClick(id: string) {
    if (dragMoved.current) {
      dragMoved.current = false;
      return;
    }
    onSelectNode(id);
  }

  function onNodeKeyDown(e: React.KeyboardEvent<SVGGElement>, node: PlanGraphNode) {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onSelectNode(node.id);
      return;
    }
    const at = layerIndexOf.get(node.id);
    if (!at) return;
    if (e.key === 'ArrowRight') { e.preventDefault(); focusNode((layers[at.col + 1]?.[at.row] ?? layers[at.col + 1]?.[0])?.id); }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); focusNode((layers[at.col - 1]?.[at.row] ?? layers[at.col - 1]?.[0])?.id); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); focusNode(layers[at.col]?.[at.row + 1]?.id); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); focusNode(layers[at.col]?.[at.row - 1]?.id); }
  }

  return (
    <div className="fs-plan" data-testid="plan-graph">
      <svg
        ref={svgRef}
        className="fs-plan__svg"
        data-note="guard-ok: hand-drawn layered workflow graph — no charting/graph library, same reasoning as MermaidView"
        viewBox={`0 0 ${width} ${height}`}
        role="group"
        aria-label={t('Workflow plan, {n} nodes', { n: nodes.length })}
      >
        <defs>
          <marker id="fs-plan-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0,0 L8,4 L0,8 Z" className="fs-plan__arrowhead" />
          </marker>
        </defs>
        {nodes.flatMap((n) =>
          n.needs.filter((dep) => byId.has(dep)).map((dep) => {
            const from = positions.get(dep);
            const to = positions.get(n.id);
            if (!from || !to) return null;
            const x1 = from.x + NODE_W;
            const y1 = from.y + NODE_H / 2;
            const x2 = to.x;
            const y2 = to.y + NODE_H / 2;
            const mid = (x1 + x2) / 2;
            return (
              <path
                key={`${dep}->${n.id}`}
                d={`M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}`}
                className="fs-plan__edge"
                data-mark={marks?.[n.id]}
                markerEnd="url(#fs-plan-arrow)"
              />
            );
          }),
        )}
        {nodes.map((n) => {
          const p = positions.get(n.id);
          if (!p) return null;
          return (
            <g
              key={n.id}
              ref={(el) => {
                if (el) nodeRefs.current.set(n.id, el);
                else nodeRefs.current.delete(n.id);
              }}
              transform={`translate(${p.x}, ${p.y})`}
              className="fs-plan__node"
              data-type={n.type}
              data-mark={marks?.[n.id]}
              data-selected={selectedNodeId === n.id || undefined}
              data-draggable={onNodeMove ? true : undefined}
              tabIndex={0}
              role="button"
              aria-label={`${n.title || n.id} (${n.type})`}
              data-testid={`plan-node-${n.id}`}
              onClick={() => onNodeClick(n.id)}
              onKeyDown={(e) => onNodeKeyDown(e, n)}
              onPointerDown={onNodeMove ? (e) => onNodePointerDown(e, n) : undefined}
              onPointerMove={onNodeMove ? onNodePointerMove : undefined}
              onPointerUp={onNodeMove ? onNodePointerUp : undefined}
              onPointerCancel={onNodeMove ? onNodePointerUp : undefined}
            >
              <rect width={NODE_W} height={NODE_H} rx={8} className="fs-plan__rect" />
              <text x={10} y={19} className="fs-plan__type">{n.type}</text>
              <text x={10} y={36} className="fs-plan__title">{(n.title || n.id).slice(0, 28)}</text>
            </g>
          );
        })}
      </svg>

      <details className="fs-plan__list">
        <summary>{t('View as a list (keyboard/screen-reader alternative)')}</summary>
        <table className="fs-plan__table">
          <thead>
            <tr>
              <th>{t('Node')}</th>
              <th>{t('Type')}</th>
              <th>{t('Needs')}</th>
              <th>{t('State')}</th>
            </tr>
          </thead>
          <tbody>
            {nodes.map((n) => (
              <tr key={n.id} data-selected={selectedNodeId === n.id || undefined}>
                <td>
                  <button
                    type="button"
                    className="fs-plan__list-btn"
                    onClick={() => onSelectNode(n.id)}
                    data-testid={`plan-list-${n.id}`}
                  >
                    {n.title || n.id}
                  </button>
                </td>
                <td>{n.type}</td>
                <td>{n.needs.join(', ') || t('none — a root node')}</td>
                <td>{marks?.[n.id] ?? '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </div>
  );
}
