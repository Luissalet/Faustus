import { AlertTriangle, ChevronDown, Crosshair, GitBranch, Info, Maximize2, RefreshCw, Search, Waypoints } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent, type WheelEvent as ReactWheelEvent } from 'react';
import { useSearchParams } from 'react-router';
import { Button, EmptyState, IconButton, Popover, Skeleton } from '../../components';
import {
  confidenceLabel,
  duplicateExcerpt,
  duplicateSpansText,
  explainNode,
  loadGraph,
  loadOrphans,
  META_KEYS,
  nodeNeighbors,
  percentLabel,
  stepWhere,
  terminus,
  type Explain,
  type GraphPayload,
  type Neighbors,
  type Orphans,
  type Scope,
  type Step,
} from '../../adapters/provenance';
import { countByKind, KIND_NOTE, layout, linesFrom, NODE_KINDS, nodeRadius, shorten, tooltip, viewModel, type Graph, type GraphNode, type ViewModel } from '../../lib/graph';
import { t, tn } from '../../i18n';
import '../provenance.css';

/**
 * Provenance (Memory → `?t=provenance`): the audit graph. Every edge was
 * read from a stored record — a declared dependency, an evidence span, a
 * checkpoint diff, a citation that resolves, a verified text overlap.
 * Nothing a model asserted.
 *
 * The previous interface drew it in a modal; here it is a tab: the legend
 * is the kind filter, the search pins its hits, a click explains the node
 * in the side panel ("why does the agent believe this"), and the
 * neighbourhood answers "what breaks if I touch this". `?node=<id>` deep
 * links a node; the scope (project, folder, budget) lives in the URL too.
 */

type Side = { kind: 'empty' } | { kind: 'loading' } | { kind: 'error'; message: string } | { kind: 'explain'; data: Explain } | { kind: 'neighbors'; data: Neighbors };

export function Provenance() {
  const [params, setParams] = useSearchParams();
  const view = params.get('v') === 'orphans' ? 'orphans' : 'graph';
  const selected = params.get('node') ?? '';
  const scope = useMemo<Scope>(() => ({ project: params.get('project') ?? '', workspace: params.get('workspace') ?? '', limit: Number(params.get('limit')) || undefined }), [params]);
  const kinds = useMemo(() => (params.get('kinds') ?? '').split(',').filter(Boolean), [params]);
  const [query, setQuery] = useState(params.get('q') ?? '');

  const [graph, setGraph] = useState<GraphPayload | null>(null);
  const [orphans, setOrphans] = useState<Orphans | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [side, setSide] = useState<Side>({ kind: 'empty' });
  const [hops, setHops] = useState(2);
  const [focus, setFocus] = useState<string | null>(null);

  const setParam = useCallback(
    (key: string, value: string) => {
      const p = new URLSearchParams(params);
      if (value) p.set(key, value);
      else p.delete(key);
      setParams(p, { replace: true });
    },
    [params, setParams],
  );

  const load = useCallback(
    async (signal?: AbortSignal) => {
      setLoading(true);
      setError(null);
      try {
        const [g, o] = await Promise.all([loadGraph(scope, signal), loadOrphans(scope, signal).catch(() => null)]);
        if (signal?.aborted) return;
        setGraph(g);
        setOrphans(o);
      } catch (e) {
        if (!signal?.aborted) setError((e as Error).message);
      } finally {
        if (!signal?.aborted) setLoading(false);
      }
    },
    [scope],
  );

  useEffect(() => {
    const ac = new AbortController();
    void load(ac.signal);
    return () => ac.abort();
  }, [load]);

  const explain = useCallback(
    async (id: string) => {
      setSide({ kind: 'loading' });
      try {
        setSide({ kind: 'explain', data: await explainNode(id, scope) });
      } catch (e) {
        setSide({ kind: 'error', message: (e as Error).message });
      }
    },
    [scope],
  );

  const neighbors = useCallback(
    async (id: string, n: number) => {
      setSide({ kind: 'loading' });
      try {
        setSide({ kind: 'neighbors', data: await nodeNeighbors(id, n, scope) });
      } catch (e) {
        setSide({ kind: 'error', message: (e as Error).message });
      }
    },
    [scope],
  );

  useEffect(() => {
    if (selected) void explain(selected);
    else setSide({ kind: 'empty' });
  }, [selected, explain]);

  const pickNode = (id: string) => {
    // Picking from the orphans list lands on the graph, where the chain shows.
    const p = new URLSearchParams(params);
    p.set('node', id);
    p.delete('v');
    setParams(p, { replace: true });
  };
  const toggleKind = (kind: string) => setParam('kinds', (kinds.includes(kind) ? kinds.filter((k) => k !== kind) : [...kinds, kind]).join(','));

  const model = useMemo<ViewModel | null>(() => (graph ? viewModel(graph, { kinds, query, selected }) : null), [graph, kinds, query, selected]);
  const counts = useMemo(() => (graph ? countByKind(graph.nodes) : {}), [graph]);
  const edgeCounts = useMemo(() => {
    const out: Record<string, number> = {};
    for (const e of model?.edges ?? []) out[e.kind] = (out[e.kind] || 0) + 1;
    return out;
  }, [model]);

  const disabled = graph ? !graph.enabled : false;

  return (
    <div className="fs-pv" data-testid="provenance">
      <div className="fs-pv__toolbar">
        <div className="fs-seg" role="radiogroup" aria-label={t('View')}>
          <button type="button" role="radio" aria-checked={view === 'graph'} onClick={() => setParam('v', '')} data-testid="provenance-view-graph">
            {t('Graph')}
          </button>
          <button type="button" role="radio" aria-checked={view === 'orphans'} onClick={() => setParam('v', 'orphans')} data-testid="provenance-view-orphans">
            {t('Orphans & duplicates')}
            {orphans && orphans.count > 0 ? <span className="fs-pv__count">{orphans.count}</span> : null}
          </button>
        </div>
        <label className="fs-search fs-pv__search">
          <Search size={14} aria-hidden="true" />
          <input
            type="search"
            value={query}
            placeholder={t('A rule, a path, a session…')}
            aria-label={t('Search the graph')}
            onChange={(e) => {
              setQuery(e.target.value);
              setParam('q', e.target.value);
            }}
            data-testid="provenance-search"
          />
        </label>
        <ScopePopover scope={scope} onApply={(next) => setParams(withScope(params, next), { replace: true })} />
        <IconButton icon={RefreshCw} label={t('Reload')} onClick={() => void load()} testId="provenance-reload" />
      </div>

      {disabled && <EmptyState icon={GitBranch} title={t('The provenance graph is turned off')} body={t('Switch it on in Settings → Agent & automation; nothing is read from disk while it is off.')} />}

      {!disabled && error && !graph && (
        <p className="fs-notice" data-tone="danger" role="alert">
          {error}
        </p>
      )}

      {!disabled && view === 'orphans' && <OrphansPanel data={orphans} loading={loading} onOpen={pickNode} />}

      {!disabled && view === 'graph' && (
        <div className="fs-pv__body">
          <section className="fs-pv__main" aria-label={t('Graph')}>
            {graph && (
              <div className="fs-pv__legend" role="group" aria-label={t('Filter by node kind')}>
                {(graph.nodeKinds.length ? graph.nodeKinds : [...NODE_KINDS]).map((kind) => (
                  <button key={kind} type="button" className="fs-pv__kind" data-kind={kind} data-on={kinds.includes(kind) || undefined} data-empty={!counts[kind] || undefined} aria-pressed={kinds.includes(kind)} title={t(KIND_NOTE[kind] ?? 'a node in the provenance graph')} onClick={() => toggleKind(kind)}>
                    <span className="fs-pv__dot" aria-hidden="true" />
                    {kind} <b>{counts[kind] ?? 0}</b>
                  </button>
                ))}
                {kinds.length > 0 && <Button variant="ghost" size="sm" label={t('Show every kind')} onClick={() => setParam('kinds', '')} />}
              </div>
            )}
            {model && Object.keys(edgeCounts).length > 0 && (
              <p className="fs-pv__edgekey" title={t('An arrow points at the record that accounts for its source — follow it and you walk towards the proof.')}>
                <span className="fs-pv__edgekey-label">{t('edges')}</span>
                {Object.keys(edgeCounts).map((kind) => (
                  <span key={kind} className="fs-pv__edgekey-item">
                    <span className="fs-pv__swatch" data-kind={kind} aria-hidden="true" />
                    {kind} <b>{edgeCounts[kind]}</b>
                  </span>
                ))}
              </p>
            )}
            {loading && !graph && <Skeleton label={t('Reading the declared edges')} height="420px" radius="panel" />}
            {graph && model && (
              <>
                <p className="fs-pv__notice">
                  {model.capped ? (
                    <span className="fs-pv__warn">
                      <AlertTriangle size={12} aria-hidden="true" /> {t('Showing {shown} of {total} nodes — narrow the filter (a kind chip or the search) to see the rest.', { shown: model.shown, total: model.filteredTotal })}
                    </span>
                  ) : (
                    <span>{t('Showing all {n} nodes and {e} edges.', { n: model.shown, e: model.edges.length })}</span>
                  )}
                  {graph.truncated && (
                    <span className="fs-pv__warn">
                      <AlertTriangle size={12} aria-hidden="true" /> {t('The server stopped building at its node budget{limit} — this is a partial graph, not the whole workspace.', { limit: graph.limit ? ` (${graph.limit})` : '' })}
                    </span>
                  )}
                  {model.query && <span>{tn(model.matched.length, '{n} node matches “{q}”', '{n} nodes match “{q}”', { q: model.query })}</span>}
                  {error && <span className="fs-pv__warn">{error}</span>}
                </p>
                {graph.nodes.length === 0 ? (
                  <EmptyState icon={GitBranch} title={t('Nothing to draw yet')} body={t('The graph is built from stored records only — declare a dependency, store a memory with an evidence span, or bind a project folder and reload.')} headingLevel={3} />
                ) : model.nodes.length === 0 ? (
                  <EmptyState icon={Search} title={t('No node survives this filter')} body={t('Clear a kind chip or shorten the search.')} headingLevel={3} />
                ) : (
                  <Canvas model={model} focus={focus} onPick={pickNode} label={t('Provenance graph: {n} nodes, {e} edges', { n: model.nodes.length, e: model.edges.length })} />
                )}
                {graph.sources.length > 0 && (
                  <details className="fs-pv__sources">
                    <summary>
                      <ChevronDown size={12} aria-hidden="true" /> {t('Where this graph came from ({live} of {total} sources readable)', { live: graph.sources.filter((s) => s.available).length, total: graph.sources.length })}
                    </summary>
                    <ul>
                      {graph.sources.map((s) => (
                        <li key={s.name} data-off={!s.available || undefined}>
                          <b>{s.name}</b>
                          <span className="fs-pv__source-count">{s.available ? (s.count ?? '') : t('not read')}</span>
                          <span className="fs-muted">{s.note}</span>
                        </li>
                      ))}
                    </ul>
                  </details>
                )}
              </>
            )}
          </section>

          <aside className="fs-pv__side" aria-label={t('Details')}>
            {side.kind === 'empty' && (
              <div className="fs-pv__side-empty">
                <Info size={18} aria-hidden="true" />
                <p>{t('Click a node to see why the agent believes it — the ordered chain of stored records behind it.')}</p>
                <p className="fs-muted">{t('Drag to pan, scroll to zoom. A search pins its hits; a kind chip hides the rest.')}</p>
              </div>
            )}
            {side.kind === 'loading' && <Skeleton label={t('Reading the chain')} count={4} height="40px" />}
            {side.kind === 'error' && (
              <p className="fs-notice" data-tone="danger" role="alert">
                {side.message}
              </p>
            )}
            {side.kind === 'explain' && (
              <ExplainPanel
                data={side.data}
                onOpen={pickNode}
                onNeighbors={(id) => void neighbors(id, hops)}
                onFocus={(id) => {
                  setFocus(null);
                  window.setTimeout(() => setFocus(id), 0);
                }}
              />
            )}
            {side.kind === 'neighbors' && (
              <NeighborsPanel
                data={side.data}
                hops={hops}
                onHops={(n) => {
                  setHops(n);
                  void neighbors(side.data.root, n);
                }}
                onOpen={pickNode}
                onBack={() => void explain(side.data.root)}
              />
            )}
          </aside>
        </div>
      )}
    </div>
  );
}

function withScope(params: URLSearchParams, scope: Scope): URLSearchParams {
  const p = new URLSearchParams(params);
  for (const [k, v] of [
    ['project', scope.project ?? ''],
    ['workspace', scope.workspace ?? ''],
    ['limit', scope.limit ? String(scope.limit) : ''],
  ] as const) {
    if (v) p.set(k, v);
    else p.delete(k);
  }
  p.delete('node');
  return p;
}

function ScopePopover({ scope, onApply }: { scope: Scope; onApply: (s: Scope) => void }) {
  const [project, setProject] = useState(scope.project ?? '');
  const [workspace, setWorkspace] = useState(scope.workspace ?? '');
  const [limit, setLimit] = useState(scope.limit ? String(scope.limit) : '');
  useEffect(() => {
    setProject(scope.project ?? '');
    setWorkspace(scope.workspace ?? '');
    setLimit(scope.limit ? String(scope.limit) : '');
  }, [scope]);
  const active = Boolean(scope.project || scope.workspace || scope.limit);
  return (
    <Popover trigger={<Button variant={active ? 'secondary' : 'ghost'} size="sm" icon={Crosshair} label={active ? t('Scope: set') : t('Scope')} testId="provenance-scope" />} align="end">
      <form
        className="fs-pv__scope"
        onSubmit={(e) => {
          e.preventDefault();
          onApply({ project: project.trim(), workspace: workspace.trim(), limit: Number(limit) || undefined });
        }}
      >
        <label>
          <span className="fs-pv__label">{t('Project id')}</span>
          <input className="fs-field" value={project} onChange={(e) => setProject(e.target.value)} placeholder={t('optional')} />
        </label>
        <label>
          <span className="fs-pv__label">{t('Folder')}</span>
          <input className="fs-field" value={workspace} onChange={(e) => setWorkspace(e.target.value)} placeholder={t('workspace path')} />
        </label>
        <label>
          <span className="fs-pv__label">{t('Node budget')}</span>
          <input className="fs-field" type="number" min={1} value={limit} onChange={(e) => setLimit(e.target.value)} placeholder={t('server default')} />
        </label>
        <div className="fs-inline">
          <Button type="submit" variant="primary" size="sm" label={t('Apply')} />
          <Button variant="ghost" size="sm" label={t('Clear')} onClick={() => onApply({})} />
        </div>
      </form>
    </Popover>
  );
}

/* ── the canvas ── */

function Canvas({ model, focus, onPick, label, mini = false }: { model: ViewModel; focus: string | null; onPick: (id: string) => void; label: string; mini?: boolean }) {
  const size = mini ? { width: 480, height: 320, pad: 30 } : {};
  const drawn = useMemo(() => layout(model.nodes, model.edges, size), [model.nodes, model.edges, mini]); // eslint-disable-line react-hooks/exhaustive-deps
  const [zoom, setZoom] = useState({ x: 0, y: 0, k: 1 });
  const pan = useRef<{ sx: number; sy: number; x: number; y: number } | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const moved = useRef(false);

  useEffect(() => {
    setZoom({ x: 0, y: 0, k: 1 });
  }, [drawn]);

  useEffect(() => {
    if (!focus) return;
    const p = drawn.positions[focus];
    if (!p) return;
    const k = 2;
    setZoom({ k, x: drawn.width / 2 - p.x * k, y: drawn.height / 2 - p.y * k });
  }, [focus, drawn]);

  const local = (e: { clientX: number; clientY: number }) => {
    const svg = svgRef.current;
    if (!svg) return { x: 0, y: 0, scale: 1 };
    const rect = svg.getBoundingClientRect();
    const scale = Math.min(rect.width / drawn.width, rect.height / drawn.height) || 1;
    const offX = (rect.width - drawn.width * scale) / 2;
    const offY = (rect.height - drawn.height * scale) / 2;
    return { x: (e.clientX - rect.left - offX) / scale, y: (e.clientY - rect.top - offY) / scale, scale };
  };

  const onWheel = (e: ReactWheelEvent<SVGSVGElement>) => {
    e.preventDefault();
    const pt = local(e);
    const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15;
    setZoom((z) => {
      const k = Math.max(0.4, Math.min(6, z.k * factor));
      const ratio = k / z.k;
      return { k, x: pt.x - (pt.x - z.x) * ratio, y: pt.y - (pt.y - z.y) * ratio };
    });
  };
  const onDown = (e: ReactPointerEvent<SVGSVGElement>) => {
    if (e.button !== 0) return;
    const pt = local(e);
    pan.current = { sx: pt.x, sy: pt.y, x: zoom.x, y: zoom.y };
    moved.current = false;
  };
  const onMove = (e: ReactPointerEvent<SVGSVGElement>) => {
    if (!pan.current) return;
    const pt = local(e);
    const dx = pt.x - pan.current.sx;
    const dy = pt.y - pan.current.sy;
    if (!moved.current && Math.abs(dx) + Math.abs(dy) > 3) {
      // Capture only once a drag is real: capturing on pointerdown would
      // steal the click a node needs to be picked.
      moved.current = true;
      try {
        (e.currentTarget as Element).setPointerCapture(e.pointerId);
      } catch {
        /* the pointer went away */
      }
    }
    if (moved.current) setZoom((z) => ({ ...z, x: pan.current!.x + dx, y: pan.current!.y + dy }));
  };
  const onUp = () => {
    pan.current = null;
  };

  const dimming = Boolean(model.query) && model.matched.length > 0;
  const matched = new Set(model.matched);

  return (
    <div className="fs-pv__canvas" data-mini={mini || undefined}>
      <svg
        ref={svgRef}
        className="fs-pv__svg"
        viewBox={`0 0 ${drawn.width} ${drawn.height}`}
        preserveAspectRatio="xMidYMid meet"
        role="img"
        aria-label={label}
        onWheel={onWheel}
        onPointerDown={onDown}
        onPointerMove={onMove}
        onPointerUp={onUp}
        onPointerCancel={onUp}
        data-note="guard-ok: the provenance graph is drawn, not an icon"
      >
        <defs>
          <marker id={`${mini ? 'pvm' : 'pv'}-arrow`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
            <path className="fs-pv__arrow" d="M0 0 L10 5 L0 10 z" />
          </marker>
        </defs>
        <g transform={`translate(${zoom.x} ${zoom.y}) scale(${zoom.k})`}>
          <g>
            {model.edges.map((e, i) => {
              const a = drawn.positions[e.from];
              const b = drawn.positions[e.to];
              if (!a || !b) return null;
              const dx = b.x - a.x;
              const dy = b.y - a.y;
              const dist = Math.sqrt(dx * dx + dy * dy) || 1;
              const gap = nodeRadius(model.degrees[e.to]) + 5;
              const x2 = b.x - (dx / dist) * gap;
              const y2 = b.y - (dy / dist) * gap;
              const touches = model.selected && (e.from === model.selected || e.to === model.selected);
              return (
                <line key={i} className="fs-pv__edge" data-kind={e.kind} data-selected={touches || undefined} x1={a.x} y1={a.y} x2={x2} y2={y2} markerEnd={`url(#${mini ? 'pvm' : 'pv'}-arrow)`}>
                  <title>{`${e.from} ${e.kind} ${e.to}${e.why ? ` — ${e.why}` : ''}`}</title>
                </line>
              );
            })}
          </g>
          <g>
            {model.nodes.map((n) => {
              const p = drawn.positions[n.id];
              if (!p) return null;
              const r = nodeRadius(model.degrees[n.id]);
              const isSel = n.id === model.selected;
              const isMatch = matched.has(n.id);
              return (
                <g
                  key={n.id}
                  className="fs-pv__node"
                  data-kind={n.kind}
                  data-selected={isSel || undefined}
                  data-match={isMatch || undefined}
                  data-dim={(dimming && !isMatch && !isSel) || undefined}
                  transform={`translate(${p.x} ${p.y})`}
                  tabIndex={0}
                  role="button"
                  aria-label={tooltip(n)}
                  onClick={() => {
                    if (!moved.current) onPick(n.id);
                  }}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault();
                      onPick(n.id);
                    }
                  }}
                >
                  <title>{tooltip(n)}</title>
                  <circle className="fs-pv__dotc" r={r} />
                  {model.labels.has(n.id) && (
                    <text className="fs-pv__text" x={0} y={r + 15} textAnchor="middle">
                      {shorten(n.label, 22)}
                    </text>
                  )}
                </g>
              );
            })}
          </g>
        </g>
      </svg>
      <div className="fs-pv__canvas-tools">
        <IconButton icon={Maximize2} label={t('Reset view')} size="sm" onClick={() => setZoom({ x: 0, y: 0, k: 1 })} />
      </div>
    </div>
  );
}

/* ── the side panel ── */

function KindChip({ node }: { node: GraphNode }) {
  return (
    <span className="fs-pv__chip" data-kind={node.kind}>
      <span className="fs-pv__dot" aria-hidden="true" />
      {node.kind}
    </span>
  );
}

function NodeMeta({ node }: { node: GraphNode }) {
  const rows: [string, string][] = [];
  for (const key of META_KEYS) {
    const v = node.meta[key];
    if (v === undefined || v === null || v === '') continue;
    rows.push([key, shorten(String(v), 120)]);
  }
  const lines = linesFrom(node.meta);
  if (lines.length) rows.push([lines.length > 1 ? 'lines' : 'line', lines.join(', ')]);
  if (!rows.length) return null;
  return (
    <dl className="fs-pv__meta">
      {rows.map(([k, v]) => (
        <div key={k}>
          <dt>{k}</dt>
          <dd>{v}</dd>
        </div>
      ))}
    </dl>
  );
}

function StepTarget({ step, onOpen }: { step: Step; onOpen: (id: string) => void }) {
  const other = step.direction === 'rests_on' ? step.to : step.from;
  if (!step.node) return <p className="fs-pv__target fs-muted">{other}</p>;
  const said = stepWhere(step);
  const where = said === step.node.label ? '' : said;
  return (
    <button type="button" className="fs-pv__target" onClick={() => onOpen(step.node!.id)} title={tooltip(step.node)}>
      <KindChip node={step.node} />
      <span className="fs-pv__target-label">{shorten(step.node.label, 70)}</span>
      {where && <span className="fs-pv__target-where">{where}</span>}
    </button>
  );
}

function ExplainPanel({ data, onOpen, onNeighbors, onFocus }: { data: Explain; onOpen: (id: string) => void; onNeighbors: (id: string) => void; onFocus: (id: string) => void }) {
  const node = data.node;
  if (!node) {
    return (
      <div className="fs-pv__side-empty">
        <p>{t('This node is not in the graph any more.')}</p>
      </div>
    );
  }
  const end = terminus(data.steps);
  return (
    <section className="fs-pv__explain" data-testid="provenance-explain">
      <header className="fs-pv__explain-head">
        <KindChip node={node} />
        <h3 className="fs-pv__explain-title">{node.label}</h3>
        {node.detail && node.detail !== node.label && <p className="fs-pv__explain-detail">{node.detail}</p>}
        <code className="fs-pv__id">{node.id}</code>
      </header>
      <NodeMeta node={node} />
      {data.summary && <p className="fs-pv__summary">{data.summary}</p>}
      <h4 className="fs-pv__label">{t('Why does the agent believe this?')}</h4>
      {end ? (
        <p className="fs-pv__terminus">
          <span className="fs-pv__label">{t('Traced to')}</span>
          <KindChip node={end.node} />
          <button type="button" className="fs-pv__link" onClick={() => onOpen(end.node.id)}>
            {end.text || end.node.label}
          </button>
        </p>
      ) : (
        <p className="fs-pv__terminus fs-muted">{t('No chat, file or job record ends this chain — nothing stored says where this came from.')}</p>
      )}
      {data.steps.length === 0 && <p className="fs-muted">{t('No edge touches this node: it stands on nothing recorded, and nothing recorded rests on it.')}</p>}
      <ol className="fs-pv__steps">
        {data.steps.map((step, i) => (
          <li key={i} className="fs-pv__step" data-kind={step.kind}>
            <div className="fs-pv__step-head">
              <span className="fs-pv__step-order">{step.order}</span>
              <span title={t('edges away from the node you picked')}>{t('hop {n}', { n: step.hop })}</span>
              <span className="fs-pv__step-kind">{step.kind}</span>
              <span title={step.direction === 'vouches_for' ? t('an edge pointing AT this node: a record made about it') : t('an edge out of this node: what it rests on')}>{step.direction === 'vouches_for' ? t('vouches for it') : t('rests on')}</span>
              <span title={t('the confidence stored on this edge')}>{t('conf {n}', { n: confidenceLabel(step.confidence) })}</span>
              <span className="fs-pv__trust" data-declared={step.trust === 'declared' || undefined} title={step.trust === 'declared' ? t('read from a stored record, not asserted by a model') : t('NOT a declared record — treat with care')}>
                {step.trust}
              </span>
            </div>
            <p className="fs-pv__step-why">{step.why}</p>
            <StepTarget step={step} onOpen={onOpen} />
          </li>
        ))}
      </ol>
      <div className="fs-inline fs-pv__side-actions">
        <Button variant="secondary" size="sm" icon={Waypoints} label={t('What breaks if I touch this')} onClick={() => onNeighbors(node.id)} testId="provenance-neighbors" />
        <Button variant="ghost" size="sm" icon={Crosshair} label={t('Centre on the canvas')} onClick={() => onFocus(node.id)} />
      </div>
    </section>
  );
}

function NeighborsPanel({ data, hops, onHops, onOpen, onBack }: { data: Neighbors; hops: number; onHops: (n: number) => void; onOpen: (id: string) => void; onBack: () => void }) {
  const root = data.nodes.find((n) => n.id === data.root) ?? null;
  const sub: Graph = { nodes: data.nodes, edges: data.edges };
  const model = useMemo(() => viewModel(sub, { selected: data.root, drawLimit: 120 }), [data]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <section className="fs-pv__explain" data-testid="provenance-neighbors">
      <header className="fs-pv__explain-head">
        {root && <KindChip node={root} />}
        <h3 className="fs-pv__explain-title">{root?.label ?? data.root}</h3>
        <code className="fs-pv__id">{data.root}</code>
      </header>
      <div className="fs-seg" role="radiogroup" aria-label={t('Hops')}>
        {[1, 2, 3].map((n) => (
          <button key={n} type="button" role="radio" aria-checked={hops === n} onClick={() => onHops(n)}>
            {tn(n, '{n} hop', '{n} hops')}
          </button>
        ))}
      </div>
      <h4 className="fs-pv__label">{t('What breaks if I touch this')}</h4>
      {data.impact.length === 0 ? (
        <p className="fs-muted">{t('Nothing recorded rests on this node.')}</p>
      ) : (
        <ul className="fs-pv__impact">
          {data.impact.map((n) => (
            <li key={n.id}>
              <KindChip node={n} />
              <button type="button" className="fs-pv__link" onClick={() => onOpen(n.id)}>
                {shorten(n.label, 70)}
              </button>
            </li>
          ))}
        </ul>
      )}
      <h4 className="fs-pv__label">{t('The neighbourhood')}</h4>
      <Canvas model={model} focus={null} onPick={onOpen} label={t('Neighbourhood of {id}', { id: data.root })} mini />
      <div className="fs-inline fs-pv__side-actions">
        <Button variant="ghost" size="sm" label={t('Back to the chain')} onClick={onBack} />
      </div>
    </section>
  );
}

function OrphansPanel({ data, loading, onOpen }: { data: Orphans | null; loading: boolean; onOpen: (id: string) => void }) {
  if (loading && !data) return <Skeleton label={t('Reading the orphans')} count={3} height="60px" radius="panel" />;
  if (!data) return <EmptyState icon={GitBranch} title={t('Could not read the orphans')} body={t('The orphans endpoint did not answer; the graph tab may still work.')} headingLevel={3} />;
  const kinds = Object.keys(data.byKind);
  return (
    <div className="fs-pv__orphans" data-testid="provenance-orphans">
      <section className="fs-pv__panel">
        <h3 className="fs-pv__h">
          {t('Orphans')} <b>{data.count}</b>
        </h3>
        <p className="fs-muted">{t('Nodes nothing points at: no stored record vouches for them.')}</p>
        {kinds.length === 0 && <p className="fs-muted">{t('Every node has at least one edge.')}</p>}
        {kinds.map((kind) => (
          <details key={kind} className="fs-pv__group" open>
            <summary>
              <span className="fs-pv__chip" data-kind={kind}>
                <span className="fs-pv__dot" aria-hidden="true" />
                {kind}
              </span>
              <b>{data.byKind[kind].length}</b>
            </summary>
            <ul>
              {data.byKind[kind].map((n) => (
                <li key={n.id}>
                  <button type="button" className="fs-pv__link" onClick={() => onOpen(n.id)}>
                    {shorten(n.label, 90)}
                  </button>
                  {n.detail && <span className="fs-muted"> {shorten(n.detail, 70)}</span>}
                </li>
              ))}
            </ul>
          </details>
        ))}
      </section>
      <section className="fs-pv__panel">
        <h3 className="fs-pv__h">
          {t('Near-duplicates')} <b>{data.duplicates.length}</b>
        </h3>
        <p className="fs-muted">{t('Pairs whose shared text was literally verified — the ratio is measured, not estimated.')}</p>
        {data.duplicates.length === 0 && <p className="fs-muted">{t('No verified duplicate pair.')}</p>}
        <ul className="fs-pv__dups">
          {data.duplicates.map((d) => {
            const excerpt = duplicateExcerpt(d.why);
            const spans = duplicateSpansText(d.spans);
            return (
              <li key={`${d.a}|${d.b}`} className="fs-pv__dup">
                <div className="fs-pv__dup-row">
                  <button type="button" className="fs-pv__link" onClick={() => onOpen(d.a)}>
                    {shorten(d.aLabel, 90)}
                  </button>
                  <span className="fs-pv__dup-vs" aria-label={t('near-duplicate of')}>
                    ≈
                  </span>
                  <button type="button" className="fs-pv__link" onClick={() => onOpen(d.b)}>
                    {shorten(d.bLabel, 90)}
                  </button>
                  <span className="fs-pv__dup-ratio">{percentLabel(d.ratio)}</span>
                </div>
                {d.why && <p className="fs-muted">{d.why}</p>}
                {excerpt && (
                  <p className="fs-pv__dup-quote">
                    <span className="fs-pv__label">{t('verified shared text')}</span> <q>{excerpt}</q>
                  </p>
                )}
                {spans && <p className="fs-muted">{spans}</p>}
              </li>
            );
          })}
        </ul>
      </section>
    </div>
  );
}
