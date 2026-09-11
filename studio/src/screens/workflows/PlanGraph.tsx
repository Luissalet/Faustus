import { useMemo, useRef } from 'react';
import { t } from '../../i18n';

/**
 * W2-E (CMP-07, `docs/api/topology.md` §Simulate) — the shared graph both
 * `WorkflowsScreen` (design/simulate) and `RunOverlay` (execute) draw on:
 * one node per `WorkflowNode`, one edge per `needs` entry. No new
 * dependency (ADP-14's own limit, still true here): a hand-drawn, layered
 * SVG, same reasoning as `components/MermaidView.tsx` choosing not to pull
 * in a renderer for two call sites.
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

export function PlanGraph({ nodes, marks, selectedNodeId, onSelectNode, emptyLabel }: PlanGraphProps) {
  const nodeRefs = useRef(new Map<string, SVGGElement>());

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

  const positions = useMemo(() => {
    const pos = new Map<string, { x: number; y: number }>();
    layers.forEach((layer, col) => {
      layer.forEach((n, row) => pos.set(n.id, { x: col * COL_WIDTH + 20, y: row * ROW_HEIGHT + 20 }));
    });
    return pos;
  }, [layers]);

  if (nodes.length === 0) {
    return <p className="fs-plan__empty">{emptyLabel ?? t('No nodes to draw yet.')}</p>;
  }

  const byId = new Map(nodes.map((n) => [n.id, n]));
  const maxRows = Math.max(1, ...layers.map((l) => l.length));
  const width = Math.max(COL_WIDTH * Math.max(layers.length, 1) + 20, 280);
  const height = Math.max(ROW_HEIGHT * maxRows + 20, 120);

  function focusNode(id?: string) {
    if (id) nodeRefs.current.get(id)?.focus();
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
              tabIndex={0}
              role="button"
              aria-label={`${n.title || n.id} (${n.type})`}
              data-testid={`plan-node-${n.id}`}
              onClick={() => onSelectNode(n.id)}
              onKeyDown={(e) => onNodeKeyDown(e, n)}
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
