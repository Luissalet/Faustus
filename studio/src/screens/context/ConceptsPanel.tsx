import { AlertTriangle, Boxes, Search, Trash2 } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { Button, EmptyState, Skeleton } from '../../components';
import {
  CONCEPT_KINDS,
  loadConcept,
  loadGraph,
  loadHistory,
  loadStale,
  removeConcept,
  understand,
  type Concept,
  type ConceptDetail,
  type ConceptGraph,
  type HistoryEntry,
  type StaleResult,
} from '../../adapters/projectConcepts';
import { t } from '../../i18n';

/**
 * Project Concepts (Context → Concepts).
 *
 * The agent's own persistent, per-project graph of architecture concepts
 * (src/project_concepts.py) — a node per feature/module/pattern/config/
 * decision/component the agent decided was worth remembering, edges typed
 * connects_to/depends_on/implements/calls/configured_by, drawn as a small
 * force-directed graph on a plain <canvas> (no charting library: this is a
 * few dozen nodes, not a data-viz problem) plus a search box that runs the
 * same semantic `understand` the agent's own `concepts_understand` tool
 * uses, so a person can sanity-check what the agent would retrieve.
 */

const KIND_COLORS: Record<string, string> = {
  feature: '#6c8cff',
  module: '#3ecf8e',
  pattern: '#f5a623',
  config: '#b980f0',
  decision: '#ff6b6b',
  component: '#4dd0e1',
};

function kindColor(kind: string): string {
  return KIND_COLORS[kind] ?? '#9aa3b2';
}

interface LaidOutNode extends Concept {
  x: number;
  y: number;
  vx: number;
  vy: number;
}

function useForceLayout(graph: ConceptGraph | undefined, width: number, height: number) {
  const [nodes, setNodes] = useState<LaidOutNode[]>([]);
  const frameRef = useRef<number>(0);

  useEffect(() => {
    if (!graph || width <= 0 || height <= 0) return;
    const g = graph;
    const degree = new Map<string, number>();
    for (const e of g.edges) {
      degree.set(e.src, (degree.get(e.src) ?? 0) + 1);
      degree.set(e.dst, (degree.get(e.dst) ?? 0) + 1);
    }
    let live: LaidOutNode[] = g.nodes.map((n, i) => {
      const angle = (i / Math.max(1, g.nodes.length)) * Math.PI * 2;
      return {
        ...n,
        degree: degree.get(n.id) ?? 0,
        x: width / 2 + Math.cos(angle) * Math.min(width, height) * 0.3,
        y: height / 2 + Math.sin(angle) * Math.min(width, height) * 0.3,
        vx: 0,
        vy: 0,
      };
    });
    const byId = new Map(live.map((n) => [n.id, n]));
    let ticks = 0;
    const REPEL = 2400;
    const SPRING = 0.02;
    const SPRING_LEN = 110;
    const CENTER = 0.004;
    const DAMPING = 0.82;

    function step() {
      for (let i = 0; i < live.length; i++) {
        for (let j = i + 1; j < live.length; j++) {
          const a = live[i];
          const b = live[j];
          let dx = a.x - b.x;
          let dy = a.y - b.y;
          let d2 = dx * dx + dy * dy;
          if (d2 < 1) d2 = 1;
          const force = REPEL / d2;
          const d = Math.sqrt(d2);
          const fx = (dx / d) * force;
          const fy = (dy / d) * force;
          a.vx += fx;
          a.vy += fy;
          b.vx -= fx;
          b.vy -= fy;
        }
      }
      for (const e of g.edges) {
        const a = byId.get(e.src);
        const b = byId.get(e.dst);
        if (!a || !b) continue;
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const d = Math.max(1, Math.sqrt(dx * dx + dy * dy));
        const stretch = d - SPRING_LEN;
        const fx = (dx / d) * stretch * SPRING;
        const fy = (dy / d) * stretch * SPRING;
        a.vx += fx;
        a.vy += fy;
        b.vx -= fx;
        b.vy -= fy;
      }
      for (const n of live) {
        n.vx += (width / 2 - n.x) * CENTER;
        n.vy += (height / 2 - n.y) * CENTER;
        n.vx *= DAMPING;
        n.vy *= DAMPING;
        n.x = Math.min(width - 20, Math.max(20, n.x + n.vx));
        n.y = Math.min(height - 20, Math.max(20, n.y + n.vy));
      }
      ticks += 1;
      setNodes([...live]);
      if (ticks < 220) frameRef.current = requestAnimationFrame(step);
    }
    frameRef.current = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frameRef.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graph, width, height]);

  return nodes;
}

function GraphCanvas({
  graph,
  selectedId,
  onSelect,
}: {
  graph: ConceptGraph;
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const observer = new ResizeObserver(() => {
      setSize({ width: el.clientWidth, height: Math.max(320, el.clientHeight) });
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const nodes = useForceLayout(graph, size.width, size.height);
  const byId = useMemo(() => new Map(nodes.map((n) => [n.id, n])), [nodes]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || size.width === 0) return;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = size.width * dpr;
    canvas.height = size.height * dpr;
    canvas.style.width = `${size.width}px`;
    canvas.style.height = `${size.height}px`;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, size.width, size.height);

    ctx.strokeStyle = 'rgba(148, 163, 184, 0.35)';
    ctx.lineWidth = 1;
    for (const e of graph.edges) {
      const a = byId.get(e.src);
      const b = byId.get(e.dst);
      if (!a || !b) continue;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
    }

    for (const n of nodes) {
      const r = 6 + Math.min(14, (n.degree ?? 0) * 2.5);
      ctx.beginPath();
      ctx.fillStyle = kindColor(n.kind);
      ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
      ctx.fill();
      if (n.id === selectedId) {
        ctx.lineWidth = 2;
        ctx.strokeStyle = '#ffffff';
        ctx.stroke();
      }
      ctx.fillStyle = 'rgba(226, 232, 240, 0.92)';
      ctx.font = '11px sans-serif';
      ctx.fillText(n.name, n.x + r + 4, n.y + 4);
    }
  }, [nodes, graph.edges, size, selectedId, byId]);

  function handleClick(event: React.MouseEvent<HTMLCanvasElement>) {
    const rect = canvasRef.current?.getBoundingClientRect();
    if (!rect) return;
    const x = event.clientX - rect.left;
    const y = event.clientY - rect.top;
    let hit: LaidOutNode | null = null;
    let best = Infinity;
    for (const n of nodes) {
      const r = 6 + Math.min(14, (n.degree ?? 0) * 2.5);
      const d = Math.hypot(n.x - x, n.y - y);
      if (d <= r + 4 && d < best) {
        best = d;
        hit = n;
      }
    }
    if (hit) onSelect(hit.id);
  }

  return (
    <div ref={containerRef} className="fs-ctx__concepts-canvas-wrap">
      <canvas
        ref={canvasRef}
        className="fs-ctx__concepts-canvas"
        onClick={handleClick}
        data-testid="concepts-graph-canvas"
      />
    </div>
  );
}

function StaleBadge({ stale }: { stale: StaleResult | null }) {
  if (!stale || !stale.checked) return null;
  if (!stale.stale) return <span className="fs-badge fs-badge--ok">{t('Refs resolve')}</span>;
  return (
    <span className="fs-badge fs-badge--warn">
      <AlertTriangle size={12} aria-hidden="true" /> {t('Stale')} ({stale.issues.length})
    </span>
  );
}

function ConceptDetailPanel({
  id,
  scope,
  onRemoved,
}: {
  id: string;
  scope: { workspace?: string; projectId?: string };
  onRemoved: () => void;
}) {
  const [detail, setDetail] = useState<ConceptDetail | null>(null);
  const [stale, setStale] = useState<StaleResult | null>(null);
  const [history, setHistory] = useState<HistoryEntry[]>([]);
  const [showHistory, setShowHistory] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setDetail(null);
    setStale(null);
    Promise.all([loadConcept(id, scope), loadStale(id, scope).catch(() => null)]).then(([d, s]) => {
      if (cancelled) return;
      setDetail(d);
      setStale(s);
    });
    return () => {
      cancelled = true;
    };
  }, [id, scope.workspace, scope.projectId]);

  if (!detail) return <Skeleton label={t('Reading the concept')} count={2} height="48px" />;

  return (
    <aside className="fs-ctx__concepts-side" data-testid="concept-detail">
      <header>
        <h4>{detail.name}</h4>
        <div className="fs-ctx__concepts-meta">
          <span className="fs-badge" style={{ background: kindColor(detail.kind) }}>{detail.kind}</span>
          <StaleBadge stale={stale} />
        </div>
      </header>
      <p className="fs-muted">{detail.summary || t('(no summary)')}</p>
      {detail.details && <p className="fs-ctx__concepts-details">{detail.details}</p>}
      {detail.refs.length > 0 && (
        <div>
          <h5>{t('Refs')}</h5>
          <ul className="fs-ctx__concepts-refs">
            {detail.refs.map((r) => {
              const broken = stale?.issues.some((i) => i.ref === r);
              return (
                <li key={r} className={broken ? 'fs-ctx__concepts-ref--broken' : ''}>
                  {r}
                </li>
              );
            })}
          </ul>
        </div>
      )}
      {(detail.outgoing.length > 0 || detail.incoming.length > 0) && (
        <div>
          <h5>{t('Relations')}</h5>
          <ul className="fs-ctx__concepts-edges">
            {detail.outgoing.map((e) => (
              <li key={e.id}>→ {e.rel} → {e.dst}</li>
            ))}
            {detail.incoming.map((e) => (
              <li key={e.id}>{e.src} → {e.rel} →</li>
            ))}
          </ul>
        </div>
      )}
      {detail.children.length > 0 && (
        <div>
          <h5>{t('Children')}</h5>
          <ul className="fs-ctx__concepts-edges">
            {detail.children.map((c) => (
              <li key={c.id}>{c.name}</li>
            ))}
          </ul>
        </div>
      )}
      <div className="fs-ctx__toolbar">
        <Button
          variant="ghost"
          size="sm"
          label={showHistory ? t('Hide history') : t('Show history')}
          onClick={() => {
            const next = !showHistory;
            setShowHistory(next);
            if (next && history.length === 0) loadHistory(id, scope).then(setHistory);
          }}
        />
        <Button
          variant="danger"
          size="sm"
          icon={Trash2}
          label={t('Remove')}
          onClick={() => void removeConcept(id, scope).then(onRemoved)}
        />
      </div>
      {showHistory && (
        <ul className="fs-ctx__concepts-history">
          {history.map((h) => (
            <li key={h.id}>
              <span className="fs-muted">{h.ts}</span> — {h.action}
            </li>
          ))}
        </ul>
      )}
    </aside>
  );
}

export function ConceptsPanel({ workspace, projectId }: { workspace: string; projectId: string }) {
  const scope = useMemo(() => ({ workspace: workspace || undefined, projectId: projectId || undefined }), [workspace, projectId]);
  const [graph, setGraph] = useState<ConceptGraph | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [searchResult, setSearchResult] = useState<Concept[] | null>(null);

  function reload() {
    setLoading(true);
    setError(null);
    loadGraph(scope)
      .then((g) => setGraph(g))
      .catch((err) => setError(err))
      .finally(() => setLoading(false));
  }

  useEffect(reload, [scope.workspace, scope.projectId]);

  async function runSearch(text: string) {
    setQuery(text);
    if (!text.trim()) {
      setSearchResult(null);
      return;
    }
    try {
      const result = await understand(text, scope, 8);
      setSearchResult(result.concepts);
    } catch (err) {
      setError(err);
    }
  }

  if (!scope.workspace && !scope.projectId) {
    return (
      <EmptyState
        icon={Boxes}
        title={t('No project bound')}
        body={t('Open a project or workspace to see its concept graph.')}
      />
    );
  }

  return (
    <div className="fs-ctx__concepts" data-testid="context-concepts">
      <header className="fs-ctx__half-head">
        <h3>{t('Project concepts')}</h3>
        <p className="fs-muted">
          {t('The graph the agent itself writes as it learns this project — features, modules, patterns, config, decisions and components, with typed relations between them.')}
        </p>
      </header>
      <form
        className="fs-ctx__toolbar"
        onSubmit={(event) => {
          event.preventDefault();
          void runSearch(query);
        }}
      >
        <label className="fs-search fs-ctx__grow">
          <Search size={13} aria-hidden="true" />
          <input
            type="search"
            value={query}
            placeholder={t('What are you trying to understand…')}
            aria-label={t('Search the concept graph')}
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
        <Button variant="secondary" size="sm" label={t('Understand')} type="submit" />
      </form>
      {searchResult && (
        <ul className="fs-ctx__concepts-search-results">
          {searchResult.length === 0 && <li className="fs-muted">{t('Nothing matches yet')}</li>}
          {searchResult.map((c) => (
            <li key={c.id}>
              <button type="button" onClick={() => setSelectedId(c.id)}>
                <span className="fs-badge" style={{ background: kindColor(c.kind) }}>{c.kind}</span>
                {c.name} — {c.summary}
              </button>
            </li>
          ))}
        </ul>
      )}
      {loading && !graph && <Skeleton label={t('Reading the concept graph')} count={3} height="72px" />}
      {error != null && (
        <EmptyState
          icon={AlertTriangle}
          tone="error"
          title={t('The concept graph could not be read')}
          body={String((error as Error)?.message ?? error)}
          primaryAction={{ label: t('Retry'), onClick: reload }}
        />
      )}
      {graph && graph.nodes.length === 0 && (
        <EmptyState
          icon={Boxes}
          title={t('No concepts recorded yet')}
          body={t('The agent records a concept whenever it works out what a subsystem is or why it exists (concept_upsert). Nothing has been recorded for this project yet.')}
        />
      )}
      {graph && graph.nodes.length > 0 && (
        <div className="fs-ctx__concepts-body">
          <GraphCanvas graph={graph} selectedId={selectedId} onSelect={setSelectedId} />
          {selectedId && (
            <ConceptDetailPanel
              id={selectedId}
              scope={scope}
              onRemoved={() => {
                setSelectedId(null);
                reload();
              }}
            />
          )}
        </div>
      )}
      <p className="fs-muted fs-ctx__concepts-kinds">
        {CONCEPT_KINDS.map((k) => (
          <span key={k} className="fs-ctx__concepts-kind-swatch">
            <span style={{ background: kindColor(k) }} /> {k}
          </span>
        ))}
      </p>
    </div>
  );
}
