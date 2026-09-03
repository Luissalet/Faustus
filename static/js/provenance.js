// static/js/provenance.js
// The provenance graph — a 2D AUDIT view, not a 3D nebula.
//
// The backend is authoritative (src/provenance_graph.py, routes/provenance_routes.py):
// every edge it hands us was READ from a stored record — a declared dependency,
// an evidence span, a checkpoint diff observed on disk, a citation that resolves
// to a page, a literally verified text overlap. Nothing a model asserted. This
// module is the human end of it, and it is built to answer, in this order:
//
//   1. "Why does the agent believe this?" — the explain panel: one node's
//      evidence chain as ordered steps, each printing the backend's own `why`
//      VERBATIM, ending at the chat, file and line that accounts for it.
//   2. "What is floating, and what is said twice?" — orphans by kind and the
//      verified near-duplicate pairs, with the shared span the detector proved.
//   3. "What breaks if I touch this?" — the neighbours subgraph and the
//      explicit impact list.
//
// And nothing else. A hairball past two hundred nodes is illegible and improves
// no query, so the canvas draws at most PV_MAX_DRAWN nodes — the best-connected
// ones — and SAYS SO ("showing N of M — narrow the filter") instead of painting
// a blob. There is no external graph library: the layout below is a small
// deterministic force pass, so the page stays self-contained.
//
// Two honesty rules run through the file:
//
//   * an edge's `why` and a node's `label`/`detail` are printed as the backend
//     wrote them — this view never re-derives a reason of its own; and
//   * `trust` is shown as it arrived. Today everything is "declared"; anything
//     else is marked, never quietly mixed into an audit view.
//
// The renderers, the layout and the filters are pure and live between the
// marked region below, so tests/test_provenance_page_js.py runs them in bare
// node.

const API = `${window.location.origin}/api/provenance`;

// ── Provenance: pure helpers (dependency-free; extracted and run under node by tests) ──
// Everything between these markers must stay free of DOM, module and window
// references so tests/test_provenance_page_js.py can execute it in bare node.

/** Local escape: same table as ui.js esc(), but import-free for tests. */
function pvEsc(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g, ch => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
  ));
}

/** Sanitize a server-provided word (a node or edge kind) for use in a class. */
function pvToken(value) {
  return String(value == null ? '' : value).toLowerCase().replace(/[^a-z0-9_-]/g, '');
}

/** A number, or `fallback` when the value is not one. Never NaN. */
function pvNum(value, fallback = 0) {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

/** An integer, or null when the value cannot be one (never a guess). */
function pvInt(value) {
  if (value == null || value === '' || typeof value === 'boolean') return null;
  const n = Number(value);
  return Number.isFinite(n) ? Math.trunc(n) : null;
}

/** Shorten for a label or a chip, with an ellipsis rather than a hard cut. */
function pvShort(value, limit = 40) {
  const text = String(value == null ? '' : value);
  return text.length <= limit ? text : `${text.slice(0, Math.max(1, limit - 1)).trimEnd()}…`;
}

/** A confidence as the backend recorded it, or an em dash when it recorded none. */
function pvConfidence(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n.toFixed(2) : '—';
}

/** A ratio as a whole percent; 0 when it is not a number. */
function pvPercent(value) {
  return `${Math.round(Math.max(0, Math.min(1, pvNum(value, 0))) * 100)}%`;
}

// The seven node kinds and the seven edge kinds the builder can emit. Kept in
// the module so the legend has a stable order even before the first response.
const PV_NODE_KINDS = ['objective', 'memory', 'chat', 'file', 'checkpoint', 'expert', 'corpus'];
const PV_EDGE_KINDS = ['depends_on', 'evidence_of', 'contradicts', 'changed', 'cites',
  'contains', 'duplicate_of'];

/** What each node kind IS, in one line — the legend's tooltip. */
const PV_KIND_NOTE = {
  objective: 'a goal declared in objectives.jsonl',
  memory: 'a standing rule or fact the agent remembers',
  chat: 'a chat session',
  file: 'a file in the bound workspace',
  checkpoint: 'a dispatched job and the diff it left on disk',
  expert: 'a specialist with its own corpus',
  corpus: 'a file inside an expert corpus',
};

/** How many nodes stay legible on one canvas. Past this the view says so. */
const PV_MAX_DRAWN = 200;
/** Past this many drawn nodes, only the nodes that matter keep a label. */
const PV_LABEL_ALL = 60;
/** How many of the busiest nodes keep a label on a crowded canvas. */
const PV_LABEL_TOP = 24;

const PV_LAYOUT = { width: 1000, height: 680, iterations: 140, pad: 46 };

// ── normalizers: accept the wrapped shape, a bare one, or junk ─────────────

/** One node: {id, kind, label, detail, meta}, every field defaulted. */
function normalizeNode(raw) {
  const row = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {};
  const id = String(row.id == null ? '' : row.id);
  return {
    id,
    kind: pvToken(row.kind) || 'node',
    label: String(row.label == null || row.label === '' ? id : row.label),
    detail: String(row.detail == null ? '' : row.detail),
    meta: row.meta && typeof row.meta === 'object' && !Array.isArray(row.meta) ? row.meta : {},
  };
}

/** One edge: {from, to, kind, confidence, trust, why, meta}. */
function normalizeEdge(raw) {
  const row = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {};
  return {
    from: String(row.from == null ? '' : row.from),
    to: String(row.to == null ? '' : row.to),
    kind: pvToken(row.kind) || 'edge',
    confidence: Number.isFinite(Number(row.confidence)) ? Number(row.confidence) : null,
    trust: String(row.trust || 'declared'),
    why: String(row.why == null ? '' : row.why),
    meta: row.meta && typeof row.meta === 'object' && !Array.isArray(row.meta) ? row.meta : {},
  };
}

/** Pull the graph body out of whatever wrapper it arrived in. */
function pvUnwrap(raw, keys) {
  let src = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {};
  for (const key of ['data', 'graph', 'result']) {
    const inner = src[key];
    if (inner && typeof inner === 'object' && !Array.isArray(inner)
        && keys.some(name => inner[name] !== undefined)) return inner;
  }
  return src;
}

/**
 * GET /api/provenance/graph. A bare `[node, …]`, the documented envelope, or a
 * `{data: …}` wrapper all normalize to the same shape; an edge whose endpoint
 * is not in `nodes` is dropped, exactly as the builder drops it, so the canvas
 * can never be asked to draw a line into nothing.
 */
function normalizeGraph(raw) {
  const src = Array.isArray(raw) ? { nodes: raw } : pvUnwrap(raw, ['nodes', 'edges', 'stats']);
  const nodes = (Array.isArray(src.nodes) ? src.nodes : []).map(normalizeNode).filter(n => n.id);
  const known = new Set(nodes.map(n => n.id));
  const edges = (Array.isArray(src.edges) ? src.edges : [])
    .map(normalizeEdge)
    .filter(e => e.from && e.to && known.has(e.from) && known.has(e.to));
  const sources = src.sources && typeof src.sources === 'object' && !Array.isArray(src.sources)
    ? src.sources : {};
  const stats = src.stats && typeof src.stats === 'object' && !Array.isArray(src.stats)
    ? src.stats : graphStats(nodes, edges);
  return {
    nodes,
    edges,
    sources,
    stats,
    truncated: Boolean(src.truncated),
    enabled: src.enabled === undefined ? true : Boolean(src.enabled),
    limit: pvNum(src.limit, 0),
    node_kinds: Array.isArray(src.node_kinds) && src.node_kinds.length
      ? src.node_kinds.map(pvToken).filter(Boolean) : PV_NODE_KINDS.slice(),
    edge_kinds: Array.isArray(src.edge_kinds) && src.edge_kinds.length
      ? src.edge_kinds.map(pvToken).filter(Boolean) : PV_EDGE_KINDS.slice(),
  };
}

/** One explain step, with its target node normalized too. */
function normalizeStep(raw, index = 0) {
  const row = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {};
  const order = pvInt(row.order);
  const hop = pvInt(row.hop);
  return {
    order: order == null ? index + 1 : order,
    hop: hop == null ? 1 : hop,
    from: String(row.from == null ? '' : row.from),
    to: String(row.to == null ? '' : row.to),
    kind: pvToken(row.kind) || 'edge',
    confidence: Number.isFinite(Number(row.confidence)) ? Number(row.confidence) : null,
    trust: String(row.trust || 'declared'),
    why: String(row.why == null ? '' : row.why),
    direction: row.direction === 'vouches_for' ? 'vouches_for' : 'rests_on',
    node: row.node && typeof row.node === 'object' ? normalizeNode(row.node) : null,
    meta: row.meta && typeof row.meta === 'object' && !Array.isArray(row.meta) ? row.meta : {},
  };
}

/** GET /api/provenance/node/{id}/explain. */
function normalizeExplain(raw) {
  const src = pvUnwrap(raw, ['steps', 'node', 'summary']);
  const node = src.node && typeof src.node === 'object' ? normalizeNode(src.node) : null;
  return {
    node: node && node.id ? node : null,
    steps: (Array.isArray(src.steps) ? src.steps : []).map(normalizeStep),
    summary: String(src.summary == null ? '' : src.summary),
    enabled: src.enabled === undefined ? true : Boolean(src.enabled),
  };
}

/** GET /api/provenance/node/{id}/neighbors. */
function normalizeNeighbors(raw) {
  const src = pvUnwrap(raw, ['nodes', 'impact', 'root']);
  const graph = normalizeGraph({ nodes: src.nodes, edges: src.edges });
  const impact = (Array.isArray(src.impact) ? src.impact : []).map(normalizeNode).filter(n => n.id);
  const ids = (Array.isArray(src.impact_ids) ? src.impact_ids : []).map(String).filter(Boolean);
  const known = new Set(impact.map(n => n.id));
  // An id without a node still counts as something that breaks: keep it, as a
  // bare id, rather than silently shrinking the blast radius.
  for (const id of ids) {
    if (!known.has(id)) { known.add(id); impact.push(normalizeNode({ id, kind: 'node', label: id })); }
  }
  const hops = pvInt(src.hops);
  return {
    root: String(src.root == null ? '' : src.root),
    hops: hops == null ? 2 : hops,
    nodes: graph.nodes,
    edges: graph.edges,
    impact,
    impact_ids: impact.map(n => n.id),
    enabled: src.enabled === undefined ? true : Boolean(src.enabled),
  };
}

/** One duplicate pair from GET /api/provenance/orphans. */
function normalizeDuplicate(raw) {
  const row = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {};
  const spans = [];
  for (const span of Array.isArray(row.spans) ? row.spans : []) {
    if (!Array.isArray(span) || span.length < 2) continue;
    const a = Array.isArray(span[0]) ? span[0] : [];
    const b = Array.isArray(span[1]) ? span[1] : [];
    const a0 = pvInt(a[0]); const a1 = pvInt(a[1]);
    const b0 = pvInt(b[0]); const b1 = pvInt(b[1]);
    if (a0 == null || a1 == null || b0 == null || b1 == null) continue;
    spans.push([[a0, a1], [b0, b1]]);
  }
  const a = String(row.a == null ? '' : row.a);
  const b = String(row.b == null ? '' : row.b);
  return {
    a,
    b,
    a_label: String(row.a_label == null || row.a_label === '' ? a : row.a_label),
    b_label: String(row.b_label == null || row.b_label === '' ? b : row.b_label),
    ratio: Number.isFinite(Number(row.ratio)) ? Number(row.ratio) : null,
    why: String(row.why == null ? '' : row.why),
    spans,
  };
}

/** GET /api/provenance/orphans. */
function normalizeOrphans(raw) {
  const src = pvUnwrap(raw, ['orphans', 'duplicates', 'orphan_ids']);
  const grouped = src.orphans && typeof src.orphans === 'object' && !Array.isArray(src.orphans)
    ? src.orphans : {};
  const byKind = {};
  for (const kind of Object.keys(grouped).sort()) {
    const rows = (Array.isArray(grouped[kind]) ? grouped[kind] : [])
      .map(normalizeNode).filter(n => n.id);
    if (rows.length) byKind[pvToken(kind) || 'node'] = rows;
  }
  const count = pvInt(src.count);
  const listed = Object.keys(byKind).reduce((total, kind) => total + byKind[kind].length, 0);
  return {
    orphans: byKind,
    orphan_ids: (Array.isArray(src.orphan_ids) ? src.orphan_ids : []).map(String).filter(Boolean),
    count: count == null ? listed : count,
    duplicates: (Array.isArray(src.duplicates) ? src.duplicates : [])
      .map(normalizeDuplicate).filter(pair => pair.a && pair.b),
    stats: src.stats && typeof src.stats === 'object' && !Array.isArray(src.stats) ? src.stats : {},
    enabled: src.enabled === undefined ? true : Boolean(src.enabled),
  };
}

// ── query building: node ids carry ":" and "/" ─────────────────────────────

/**
 * A node id as a URL path segment for `/node/{node_id:path}/…`.
 *
 * The route takes a `:path` converter, so a file id keeps its slashes as
 * separators and everything else in the id (the ":" after the kind, spaces, a
 * "?" in a filename) is percent-encoded segment by segment. Encoding the whole
 * id in one go would turn "/" into %2F, which some proxies refuse outright.
 */
function encodeNodeId(id) {
  return String(id == null ? '' : id).split('/').map(encodeURIComponent).join('/');
}

/** `?project=&workspace=` — only the parts that were actually set. */
function scopeQuery(state = {}) {
  const st = state && typeof state === 'object' ? state : {};
  const parts = [];
  const project = String(st.project == null ? '' : st.project).trim();
  const workspace = String(st.workspace == null ? '' : st.workspace).trim();
  if (project) parts.push(`project=${encodeURIComponent(project)}`);
  if (workspace) parts.push(`workspace=${encodeURIComponent(workspace)}`);
  return parts.length ? `?${parts.join('&')}` : '';
}

/** The graph query: the scope, plus the kind filter and the node budget. */
function graphQuery(state = {}) {
  const st = state && typeof state === 'object' ? state : {};
  const parts = [];
  const project = String(st.project == null ? '' : st.project).trim();
  const workspace = String(st.workspace == null ? '' : st.workspace).trim();
  if (project) parts.push(`project=${encodeURIComponent(project)}`);
  if (workspace) parts.push(`workspace=${encodeURIComponent(workspace)}`);
  const kinds = (Array.isArray(st.kinds) ? st.kinds : []).map(pvToken).filter(Boolean);
  if (kinds.length) parts.push(`kinds=${encodeURIComponent(kinds.join(','))}`);
  const limit = pvInt(st.limit);
  if (limit != null && limit > 0) parts.push(`limit=${limit}`);
  return parts.length ? `?${parts.join('&')}` : '';
}

// ── filtering, capping and the view model ─────────────────────────────────

/** Counts per node kind, in the canonical order, for the legend chips. */
function countByKind(nodes) {
  const counts = {};
  for (const node of Array.isArray(nodes) ? nodes : []) {
    const kind = pvToken(node && node.kind) || 'node';
    counts[kind] = (counts[kind] || 0) + 1;
  }
  return counts;
}

/** The same summary the backend computes, for a payload that arrived without one. */
function graphStats(nodes, edges) {
  const list = Array.isArray(nodes) ? nodes : [];
  const links = Array.isArray(edges) ? edges : [];
  const connected = new Set();
  const byEdge = {};
  for (const edge of links) {
    connected.add(edge.from); connected.add(edge.to);
    const kind = pvToken(edge.kind) || 'edge';
    byEdge[kind] = (byEdge[kind] || 0) + 1;
  }
  return {
    nodes: list.length,
    edges: links.length,
    node_kinds: countByKind(list),
    edge_kinds: byEdge,
    orphans: list.filter(n => !connected.has(n.id)).length,
  };
}

/** How many edges touch each node. */
function degreeMap(edges) {
  const degrees = {};
  for (const edge of Array.isArray(edges) ? edges : []) {
    degrees[edge.from] = (degrees[edge.from] || 0) + 1;
    degrees[edge.to] = (degrees[edge.to] || 0) + 1;
  }
  return degrees;
}

/** The lower-cased text a search runs against: label, detail, id and path. */
function searchHaystack(node) {
  const row = node && typeof node === 'object' ? node : {};
  const meta = row.meta && typeof row.meta === 'object' ? row.meta : {};
  return [row.label, row.detail, row.id, row.kind, meta.path, meta.title, meta.status,
    meta.session_id, meta.job_id, meta.slug, meta.source]
    .filter(part => part != null && part !== '')
    .join(' ')
    .toLowerCase();
}

/** Every whitespace-separated term must appear; an empty query matches nothing. */
function matchesHaystack(haystack, query) {
  const terms = String(query == null ? '' : query).toLowerCase().split(/\s+/).filter(Boolean);
  if (!terms.length) return false;
  const hay = String(haystack == null ? '' : haystack).toLowerCase();
  return terms.every(term => hay.includes(term));
}

function matchesQuery(node, query) {
  return matchesHaystack(searchHaystack(node), query);
}

/** The subgraph of the named kinds; an edge losing an endpoint goes with it. */
function filterGraphByKinds(graph, kinds) {
  const nodes = Array.isArray(graph && graph.nodes) ? graph.nodes : [];
  const edges = Array.isArray(graph && graph.edges) ? graph.edges : [];
  const wanted = new Set((Array.isArray(kinds) ? kinds : []).map(pvToken).filter(Boolean));
  if (!wanted.size) return { nodes: nodes.slice(), edges: edges.slice() };
  const keptNodes = nodes.filter(node => wanted.has(pvToken(node.kind)));
  const keep = new Set(keptNodes.map(node => node.id));
  return { nodes: keptNodes, edges: edges.filter(e => keep.has(e.from) && keep.has(e.to)) };
}

/**
 * Cut the drawing down to something legible.
 *
 * The nodes that survive are the best connected — the ones an audit view is
 * actually about — with ties broken on (kind, id) so two renders of the same
 * graph draw the same picture. Anything the user has pinned (the selection,
 * the search hits) is kept whatever its degree.
 */
function capGraph(nodes, edges, limit = PV_MAX_DRAWN, pinned = []) {
  const list = (Array.isArray(nodes) ? nodes : []).filter(node => node && node.id);
  const links = Array.isArray(edges) ? edges : [];
  const max = Math.max(1, pvNum(limit, PV_MAX_DRAWN));
  if (list.length <= max) {
    return { nodes: list.slice(), edges: links.slice(), shown: list.length, total: list.length, capped: false };
  }
  const degrees = degreeMap(links);
  const keepFirst = new Set((Array.isArray(pinned) ? pinned : []).map(String).filter(Boolean));
  const ranked = list.slice().sort((a, b) => {
    const pinA = keepFirst.has(a.id) ? 1 : 0;
    const pinB = keepFirst.has(b.id) ? 1 : 0;
    if (pinA !== pinB) return pinB - pinA;
    const degA = degrees[a.id] || 0;
    const degB = degrees[b.id] || 0;
    if (degA !== degB) return degB - degA;
    if (a.kind !== b.kind) return a.kind < b.kind ? -1 : 1;
    return a.id < b.id ? -1 : 1;
  });
  const kept = ranked.slice(0, max).sort((a, b) => {
    if (a.kind !== b.kind) return a.kind < b.kind ? -1 : 1;
    return a.id < b.id ? -1 : 1;
  });
  const keep = new Set(kept.map(node => node.id));
  return {
    nodes: kept,
    edges: links.filter(e => keep.has(e.from) && keep.has(e.to)),
    shown: kept.length,
    total: list.length,
    capped: true,
  };
}

/** Which nodes keep a text label: all of them when few, the ones that matter otherwise. */
function pickLabelIds(nodes, degrees, options = {}) {
  const list = Array.isArray(nodes) ? nodes : [];
  const opts = options && typeof options === 'object' ? options : {};
  if (list.length <= PV_LABEL_ALL) return list.map(node => node.id);
  const keep = new Set();
  if (opts.selected) keep.add(String(opts.selected));
  for (const id of Array.isArray(opts.matched) ? opts.matched : []) keep.add(String(id));
  const busiest = list.slice().sort((a, b) => {
    const degA = (degrees || {})[a.id] || 0;
    const degB = (degrees || {})[b.id] || 0;
    if (degA !== degB) return degB - degA;
    return a.id < b.id ? -1 : 1;
  }).slice(0, PV_LABEL_TOP);
  for (const node of busiest) keep.add(node.id);
  return list.filter(node => keep.has(node.id)).map(node => node.id);
}

/** Everything the canvas needs, decided once: what is drawn, and what is said about it. */
function graphViewModel(graph, state = {}) {
  const g = normalizeGraph(graph);
  const st = state && typeof state === 'object' ? state : {};
  const filtered = filterGraphByKinds(g, st.kinds);
  const query = String(st.query == null ? '' : st.query).trim();
  const hits = query ? filtered.nodes.filter(node => matchesQuery(node, query)).map(n => n.id) : [];
  const pinned = hits.slice();
  if (st.selected) pinned.push(String(st.selected));
  const drawLimit = pvNum(st.drawLimit, PV_MAX_DRAWN);
  const capped = capGraph(filtered.nodes, filtered.edges, drawLimit, pinned);
  const degrees = degreeMap(capped.edges);
  const drawn = new Set(capped.nodes.map(node => node.id));
  const matched = hits.filter(id => drawn.has(id));
  return {
    nodes: capped.nodes,
    edges: capped.edges,
    degrees,
    matched,
    labels: pickLabelIds(capped.nodes, degrees, { matched, selected: st.selected }),
    shown: capped.shown,
    total: g.nodes.length,
    filteredTotal: capped.total,
    capped: capped.capped,
    query,
    selected: String(st.selected == null ? '' : st.selected),
    kinds: (Array.isArray(st.kinds) ? st.kinds : []).map(pvToken).filter(Boolean),
    truncated: Boolean(g.truncated),
    limit: g.limit,
  };
}

// ── the deterministic force layout (no library, on purpose) ───────────────

/** FNV-1a, as a fraction in [0, 1): the layout's only source of "randomness". */
function pvHash(value) {
  const text = String(value == null ? '' : value);
  let hash = 2166136261;
  for (let i = 0; i < text.length; i += 1) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0) / 4294967296;
}

/**
 * Start each kind in its own wedge of the circle.
 *
 * A force pass converges to a picture, but which picture depends on where it
 * started; seeding by kind means the file cluster lands in the same place every
 * time, which is what makes the canvas re-readable after a filter change.
 */
function seedPositions(nodes, options = {}) {
  const opts = { ...PV_LAYOUT, ...(options && typeof options === 'object' ? options : {}) };
  const list = Array.isArray(nodes) ? nodes : [];
  const kinds = [];
  for (const node of list) {
    const kind = pvToken(node.kind) || 'node';
    if (!kinds.includes(kind)) kinds.push(kind);
  }
  kinds.sort();
  const centreX = opts.width / 2;
  const centreY = opts.height / 2;
  const radius = Math.min(opts.width, opts.height) * 0.42;
  const positions = {};
  for (const node of list) {
    const kind = pvToken(node.kind) || 'node';
    const band = Math.max(1, kinds.length);
    const angle = 2 * Math.PI * ((kinds.indexOf(kind) + 0.15 + 0.7 * pvHash(node.id)) / band);
    const spread = 0.32 + 0.68 * pvHash(`r:${node.id}`);
    positions[node.id] = {
      x: centreX + Math.cos(angle) * radius * spread,
      y: centreY + Math.sin(angle) * radius * spread,
    };
  }
  return positions;
}

/**
 * A small Fruchterman–Reingold pass: repulsion between every pair, springs
 * along the edges, a little gravity so nothing drifts off the canvas, and a
 * cooling schedule. Pure and deterministic — no clock, no randomness — so the
 * same graph always lays out the same way and a test can pin it.
 */
function layoutGraph(nodes, edges, options = {}) {
  const opts = { ...PV_LAYOUT, ...(options && typeof options === 'object' ? options : {}) };
  const list = (Array.isArray(nodes) ? nodes : []).filter(node => node && node.id);
  const positions = seedPositions(list, opts);
  const ids = list.map(node => node.id);
  const known = new Set(ids);
  const links = (Array.isArray(edges) ? edges : [])
    .filter(edge => edge && known.has(edge.from) && known.has(edge.to) && edge.from !== edge.to);
  if (ids.length <= 1) {
    for (const id of ids) positions[id] = { x: opts.width / 2, y: opts.height / 2 };
    return { width: opts.width, height: opts.height, positions: roundPositions(positions) };
  }
  const area = opts.width * opts.height;
  const ideal = Math.sqrt(area / ids.length) * 0.72;
  const centreX = opts.width / 2;
  const centreY = opts.height / 2;
  let temperature = Math.min(opts.width, opts.height) * 0.12;
  // Repulsion is O(n²) per pass, so a big graph trades passes for a rendered
  // picture: the drawing stays interactive at the 200-node cap.
  const budget = options && options.iterations
    ? Math.trunc(options.iterations)
    : Math.max(60, Math.min(PV_LAYOUT.iterations, Math.round(12000 / ids.length)));
  const iterations = Math.max(1, budget);
  for (let step = 0; step < iterations; step += 1) {
    const dispX = {};
    const dispY = {};
    for (const id of ids) { dispX[id] = 0; dispY[id] = 0; }
    for (let i = 0; i < ids.length; i += 1) {
      for (let j = i + 1; j < ids.length; j += 1) {
        const a = positions[ids[i]];
        const b = positions[ids[j]];
        let dx = a.x - b.x;
        let dy = a.y - b.y;
        let dist = Math.sqrt(dx * dx + dy * dy);
        if (dist < 0.01) {
          // Two nodes exactly on top of each other have no direction to
          // separate along: nudge them apart deterministically by id.
          dx = (pvHash(ids[i]) - 0.5) * 0.1 || 0.05;
          dy = (pvHash(ids[j]) - 0.5) * 0.1 || 0.05;
          dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
        }
        const force = Math.min((ideal * ideal) / dist, ideal * 4);
        const fx = (dx / dist) * force;
        const fy = (dy / dist) * force;
        dispX[ids[i]] += fx; dispY[ids[i]] += fy;
        dispX[ids[j]] -= fx; dispY[ids[j]] -= fy;
      }
    }
    for (const edge of links) {
      const a = positions[edge.from];
      const b = positions[edge.to];
      const dx = a.x - b.x;
      const dy = a.y - b.y;
      const dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
      const force = (dist * dist) / ideal;
      const fx = (dx / dist) * force;
      const fy = (dy / dist) * force;
      dispX[edge.from] -= fx; dispY[edge.from] -= fy;
      dispX[edge.to] += fx; dispY[edge.to] += fy;
    }
    for (const id of ids) {
      const point = positions[id];
      dispX[id] += (centreX - point.x) * 0.012;
      dispY[id] += (centreY - point.y) * 0.012;
      const move = Math.sqrt(dispX[id] * dispX[id] + dispY[id] * dispY[id]) || 0.0001;
      const capped = Math.min(move, temperature);
      point.x += (dispX[id] / move) * capped;
      point.y += (dispY[id] / move) * capped;
    }
    temperature = Math.max(temperature * 0.94, 0.6);
  }
  return {
    width: opts.width,
    height: opts.height,
    positions: roundPositions(fitPositions(positions, ids, opts)),
  };
}

/** Scale the converged cloud into the viewBox, keeping its aspect ratio. */
function fitPositions(positions, ids, opts) {
  if (!ids.length) return positions;
  let minX = Infinity; let maxX = -Infinity; let minY = Infinity; let maxY = -Infinity;
  for (const id of ids) {
    const point = positions[id];
    minX = Math.min(minX, point.x); maxX = Math.max(maxX, point.x);
    minY = Math.min(minY, point.y); maxY = Math.max(maxY, point.y);
  }
  const spanX = Math.max(maxX - minX, 1);
  const spanY = Math.max(maxY - minY, 1);
  const usableX = Math.max(opts.width - opts.pad * 2, 1);
  const usableY = Math.max(opts.height - opts.pad * 2, 1);
  const scale = Math.min(usableX / spanX, usableY / spanY);
  const offsetX = opts.pad + (usableX - spanX * scale) / 2;
  const offsetY = opts.pad + (usableY - spanY * scale) / 2;
  const out = {};
  for (const id of ids) {
    out[id] = {
      x: offsetX + (positions[id].x - minX) * scale,
      y: offsetY + (positions[id].y - minY) * scale,
    };
  }
  return out;
}

function roundPositions(positions) {
  const out = {};
  for (const id of Object.keys(positions)) {
    out[id] = {
      x: Math.round(positions[id].x * 10) / 10,
      y: Math.round(positions[id].y * 10) / 10,
    };
  }
  return out;
}

/** Busier node, bigger dot — degree is the only size signal, and it is capped. */
function nodeRadius(degree) {
  return Math.round((6 + Math.min(6, Math.sqrt(Math.max(0, pvNum(degree, 0))) * 2.2)) * 10) / 10;
}

// ── rendering: the canvas ─────────────────────────────────────────────────

/** The kind chip that prefixes a node everywhere it appears. */
function nodeChipHtml(node) {
  const kind = pvToken(node && node.kind) || 'node';
  const note = PV_KIND_NOTE[kind] || 'a node in the provenance graph';
  return `<span class="pv-chip pv-kind-${pvEsc(kind)}" title="${pvEsc(note)}">${pvEsc(kind)}</span>`;
}

/** The hover card's text: what this node is, in the backend's own words. */
function nodeTooltipText(node) {
  const row = normalizeNode(node);
  const meta = row.meta || {};
  const parts = [`${row.kind} · ${row.label}`];
  if (row.detail && row.detail !== row.label) parts.push(row.detail);
  if (meta.path && meta.path !== row.label && meta.path !== row.detail) parts.push(String(meta.path));
  if (Array.isArray(meta.lines) && meta.lines.length) {
    parts.push(`line${meta.lines.length > 1 ? 's' : ''} ${meta.lines.join(', ')}`);
  }
  if (meta.session_id) parts.push(`session ${meta.session_id}`);
  if (meta.job_id) parts.push(`job ${meta.job_id}`);
  parts.push(row.id);
  return parts.join(' — ');
}

/**
 * The graph as one SVG string.
 *
 * Everything is a class, so the theme paints it: node colour by kind, edge
 * dash by kind. `prefix` namespaces the arrow marker id, because the neighbours
 * panel draws a second, smaller graph on the same page.
 */
function graphSvgHtml(view, layout, options = {}) {
  const opts = options && typeof options === 'object' ? options : {};
  const prefix = pvToken(opts.prefix) || 'pv';
  const model = view && typeof view === 'object' ? view : {};
  const nodes = Array.isArray(model.nodes) ? model.nodes : [];
  const edges = Array.isArray(model.edges) ? model.edges : [];
  const positions = (layout && layout.positions) || {};
  const width = pvNum(layout && layout.width, PV_LAYOUT.width);
  const height = pvNum(layout && layout.height, PV_LAYOUT.height);
  const degrees = model.degrees || {};
  const labels = new Set(Array.isArray(model.labels) ? model.labels : nodes.map(n => n.id));
  const matched = new Set(Array.isArray(model.matched) ? model.matched : []);
  const selected = String(model.selected == null ? '' : model.selected);
  const dimming = Boolean(model.query) && matched.size > 0;

  const edgeMarkup = edges.map(edge => {
    const a = positions[edge.from];
    const b = positions[edge.to];
    if (!a || !b) return '';
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const dist = Math.sqrt(dx * dx + dy * dy) || 1;
    // Stop the line short of the target dot so the arrow head stays visible.
    const gap = nodeRadius(degrees[edge.to]) + 5;
    const x2 = Math.round((b.x - (dx / dist) * gap) * 10) / 10;
    const y2 = Math.round((b.y - (dy / dist) * gap) * 10) / 10;
    const touches = selected && (edge.from === selected || edge.to === selected);
    const cls = `pv-edge pv-edge-${pvEsc(pvToken(edge.kind))}${touches ? ' is-selected' : ''}`;
    const why = edge.why ? ` — ${edge.why}` : '';
    return `<line class="${cls}" x1="${a.x}" y1="${a.y}" x2="${x2}" y2="${y2}" `
      + `marker-end="url(#${prefix}-arrow)"><title>${pvEsc(`${edge.from} ${edge.kind} ${edge.to}${why}`)}</title></line>`;
  }).join('');

  const nodeMarkup = nodes.map(node => {
    const point = positions[node.id];
    if (!point) return '';
    const radius = nodeRadius(degrees[node.id]);
    const isSelected = node.id === selected;
    const isMatch = matched.has(node.id);
    const classes = ['pv-node', `pv-kind-${pvEsc(pvToken(node.kind))}`];
    if (isSelected) classes.push('is-selected');
    if (isMatch) classes.push('is-match');
    if (dimming && !isMatch && !isSelected) classes.push('is-dim');
    const tooltip = nodeTooltipText(node);
    const label = labels.has(node.id)
      ? `<text class="pv-node-label" x="0" y="${radius + 12}" text-anchor="middle">${pvEsc(pvShort(node.label, 22))}</text>`
      : '';
    return `<g class="${classes.join(' ')}" data-pv-node="${pvEsc(node.id)}" `
      + `data-pv-hay="${pvEsc(searchHaystack(node))}" data-pv-title="${pvEsc(tooltip)}" `
      + `transform="translate(${point.x} ${point.y})" tabindex="0" role="button" `
      + `aria-label="${pvEsc(tooltip)}">`
      + `<circle class="pv-dot" r="${radius}"></circle>${label}</g>`;
  }).join('');

  const caption = `Provenance graph: ${nodes.length} node(s), ${edges.length} edge(s)`;
  return `<svg class="pv-svg" data-pv-svg viewBox="0 0 ${width} ${height}" `
    + `preserveAspectRatio="xMidYMid meet" role="img" aria-label="${pvEsc(caption)}">`
    + `<defs><marker id="${prefix}-arrow" viewBox="0 0 10 10" refX="9" refY="5" `
    + 'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
    + '<path class="pv-arrow-head" d="M0 0 L10 5 L0 10 z"></path></marker></defs>'
    + `<g class="pv-zoom" data-pv-zoom transform="translate(0 0) scale(1)">`
    + `<g class="pv-edge-layer">${edgeMarkup}</g>`
    + `<g class="pv-node-layer">${nodeMarkup}</g>`
    + '</g></svg>';
}

/** The legend, which is also the kind filter: one chip per kind, with counts. */
function graphLegendHtml(graph, state = {}) {
  const g = normalizeGraph(graph);
  const st = state && typeof state === 'object' ? state : {};
  const active = new Set((Array.isArray(st.kinds) ? st.kinds : []).map(pvToken).filter(Boolean));
  const counts = countByKind(g.nodes);
  const order = g.node_kinds.length ? g.node_kinds : PV_NODE_KINDS;
  const chips = order.map(kind => {
    const token = pvToken(kind);
    const count = counts[token] || 0;
    const on = active.has(token);
    const note = PV_KIND_NOTE[token] || 'a node in the provenance graph';
    return `<button type="button" class="pv-legend-chip pv-kind-${pvEsc(token)}${on ? ' is-on' : ''}`
      + `${count ? '' : ' is-empty'}" data-pv-kind="${pvEsc(token)}" `
      + `aria-pressed="${on ? 'true' : 'false'}" title="${pvEsc(note)}">`
      + `<span class="pv-legend-dot"></span>${pvEsc(token)} <b>${count}</b></button>`;
  }).join('');
  const clear = active.size
    ? '<button type="button" class="pv-legend-clear" data-pv-kind-clear>show every kind</button>'
    : '';
  return `<div class="pv-legend" role="group" aria-label="Filter by node kind">${chips}${clear}</div>`
    + edgeKeyHtml(g);
}

/** The line style of every edge kind actually present, drawn the way it draws. */
function edgeKeyHtml(graph) {
  const g = normalizeGraph(graph);
  const counts = {};
  for (const edge of g.edges) {
    const kind = pvToken(edge.kind) || 'edge';
    counts[kind] = (counts[kind] || 0) + 1;
  }
  const order = (g.edge_kinds.length ? g.edge_kinds : PV_EDGE_KINDS).filter(kind => counts[kind]);
  if (!order.length) return '';
  const items = order.map(kind => '<span class="pv-edge-key-item">'
    + '<svg class="pv-edge-key-swatch" viewBox="0 0 30 6" aria-hidden="true">'
    + `<line class="pv-edge pv-edge-${pvEsc(kind)}" x1="1" y1="3" x2="29" y2="3"></line></svg>`
    + `${pvEsc(kind)} <b>${counts[kind]}</b></span>`).join('');
  return '<div class="pv-edge-key">'
    + '<span class="pv-edge-key-label" title="an arrow points at the record that accounts for '
    + 'its source — follow it and you walk towards the proof">edges</span>'
    + `${items}</div>`;
}

/**
 * What the canvas is NOT showing, said plainly.
 *
 * A drawing that quietly leaves half the graph out is worse than no drawing:
 * this line is the difference between an audit view and a decoration.
 */
function graphNoticeHtml(view, graph) {
  const model = view && typeof view === 'object' ? view : {};
  const g = normalizeGraph(graph);
  const lines = [];
  if (model.capped) {
    lines.push(`<span class="pv-notice-warn">Showing ${pvNum(model.shown, 0)} of `
      + `${pvNum(model.filteredTotal, 0)} nodes — narrow the filter (a kind chip or the search) `
      + 'to see the rest.</span>');
  } else {
    lines.push(`<span class="pv-notice-ok">Showing all ${pvNum(model.shown, 0)} node(s) and `
      + `${(Array.isArray(model.edges) ? model.edges : []).length} edge(s).</span>`);
  }
  if (g.truncated) {
    lines.push('<span class="pv-notice-warn">The server stopped building at its node budget'
      + `${g.limit ? ` (${g.limit})` : ''} — this is a partial graph, not the whole workspace.</span>`);
  }
  const matches = Array.isArray(model.matched) ? model.matched.length : 0;
  const matchText = model.query
    ? `${matches} node(s) match “${pvEsc(model.query)}”`
    : '';
  return `<p class="pv-notice">${lines.join(' ')}`
    + `<span class="pv-notice-matches" data-pv-matches>${matchText}</span></p>`;
}

/** Where the graph came from, and which sources were missing. */
function sourcesHtml(sources) {
  const rows = sources && typeof sources === 'object' && !Array.isArray(sources) ? sources : {};
  const names = Object.keys(rows).sort();
  if (!names.length) return '';
  const items = names.map(name => {
    const row = rows[name] && typeof rows[name] === 'object' ? rows[name] : {};
    const available = Boolean(row.available);
    const count = pvInt(row.count);
    return `<li class="pv-source${available ? '' : ' is-off'}">`
      + `<b>${pvEsc(name)}</b>`
      + `<span class="pv-source-count">${available ? (count == null ? '' : count) : 'not read'}</span>`
      + `<span class="pv-source-note">${pvEsc(row.note || '')}</span></li>`;
  }).join('');
  const live = names.filter(name => rows[name] && rows[name].available).length;
  return `<details class="pv-sources"><summary>Where this graph came from `
    + `(${live} of ${names.length} source(s) readable)</summary><ul>${items}</ul></details>`;
}

/** The toolbar: the two tabs, the scope, the search and the reload. */
function toolbarHtml(state = {}) {
  const st = state && typeof state === 'object' ? state : {};
  const tab = st.tab === 'orphans' ? 'orphans' : 'graph';
  return '<div class="pv-toolbar-row">'
    + '<div class="pv-tabs" role="tablist">'
    + `<button type="button" class="pv-tab${tab === 'graph' ? ' is-on' : ''}" data-pv-tab="graph" `
    + `role="tab" aria-selected="${tab === 'graph' ? 'true' : 'false'}">Graph</button>`
    + `<button type="button" class="pv-tab${tab === 'orphans' ? ' is-on' : ''}" data-pv-tab="orphans" `
    + `role="tab" aria-selected="${tab === 'orphans' ? 'true' : 'false'}">Orphans &amp; duplicates</button>`
    + '</div>'
    + '<label class="pv-field"><span>search</span>'
    + `<input type="search" class="pv-input" data-pv-search value="${pvEsc(st.query || '')}" `
    + 'placeholder="a rule, a path, a session…"></label>'
    + '<label class="pv-field"><span>project</span>'
    + `<input type="text" class="pv-input pv-input-narrow" data-pv-project value="${pvEsc(st.project || '')}" `
    + 'placeholder="project id"></label>'
    + '<label class="pv-field"><span>folder</span>'
    + `<input type="text" class="pv-input" data-pv-workspace value="${pvEsc(st.workspace || '')}" `
    + 'placeholder="workspace path"></label>'
    + '<button type="button" class="pv-btn" data-pv-reload>Reload</button>'
    + '<button type="button" class="pv-btn" data-pv-zoom-reset>Reset view</button>'
    + `<span class="pv-error" data-pv-error${st.error ? '' : ' hidden'}>${pvEsc(st.error || '')}</span>`
    + '</div>';
}

/** The whole graph tab: legend, notice, canvas, sources. */
function graphPanelHtml(graph, state = {}) {
  const g = normalizeGraph(graph);
  const st = state && typeof state === 'object' ? state : {};
  if (g.enabled === false) return disabledHtml();
  if (st.loading) return '<div class="pv-placeholder">Reading the declared edges…</div>';
  // A failed read must not read as an empty workspace.
  if (st.error && !g.nodes.length) return `<p class="pv-error-block">${pvEsc(st.error)}</p>`;
  if (!g.nodes.length) {
    return '<div class="pv-placeholder">Nothing to draw yet. The graph is built from stored '
      + 'records only — declare a dependency, store a memory with an evidence span, or bind a '
      + 'project folder above and reload.'
      + `${sourcesHtml(g.sources)}</div>`;
  }
  const view = graphViewModel(g, st);
  const layout = layoutGraph(view.nodes, view.edges, {});
  const empty = view.nodes.length
    ? ''
    : '<p class="pv-placeholder">No node survives this filter.</p>';
  return '<div class="pv-graph">'
    + graphLegendHtml(g, st)
    + graphNoticeHtml(view, g)
    + `<div class="pv-canvas" data-pv-canvas>${empty}${graphSvgHtml(view, layout, { prefix: 'pv' })}`
    + '<div class="pv-tip" data-pv-tip hidden></div></div>'
    + sourcesHtml(g.sources)
    + '</div>';
}

function disabledHtml() {
  return '<div class="pv-placeholder">The provenance graph is turned off in '
    + 'Settings → Agent &amp; automation, so it has no nodes.</div>';
}

// ── rendering: the explain panel (the feature's whole point) ───────────────

/** The lines a step names: the edge's own first, the file node's as a fallback. */
function linesFrom(meta) {
  const out = [];
  const row = meta && typeof meta === 'object' && !Array.isArray(meta) ? meta : {};
  const push = (value) => {
    const line = pvInt(value);
    if (line != null && line > 0 && !out.includes(line)) out.push(line);
  };
  push(row.line);
  for (const value of Array.isArray(row.lines) ? row.lines : []) push(value);
  return out.sort((a, b) => a - b);
}

/**
 * The line THIS step names, read out of its own `why`.
 *
 * `explain` hands back the edge's `why` but not the edge's `meta`, and the why
 * of a file step is written as "…points at src/app.py:120…" by the builder. So
 * the line is recovered only when the sentence names this exact file node's
 * path — never guessed from a stray number.
 */
function linesFromWhy(step) {
  const row = step && typeof step === 'object' ? step : {};
  const node = row.node && typeof row.node === 'object' ? row.node : null;
  if (!node || pvToken(node.kind) !== 'file') return [];
  const meta = node.meta && typeof node.meta === 'object' ? node.meta : {};
  const path = String(meta.path || node.detail || '');
  if (!path) return [];
  const why = String(row.why == null ? '' : row.why);
  const needle = `${path}:`;
  const out = [];
  let at = why.indexOf(needle);
  while (at !== -1) {
    const digits = /^(\d+)/.exec(why.slice(at + needle.length));
    if (digits) {
      const line = pvInt(digits[1]);
      if (line != null && !out.includes(line)) out.push(line);
    }
    at = why.indexOf(needle, at + 1);
  }
  return out.sort((a, b) => a - b);
}

/** The lines and where they came from: this edge, this sentence, or the node. */
function stepLineInfo(step) {
  const row = step && typeof step === 'object' ? step : {};
  const fromEdge = linesFrom(row.meta);
  if (fromEdge.length) return { lines: fromEdge, source: 'edge' };
  const fromWhy = linesFromWhy(row);
  if (fromWhy.length) return { lines: fromWhy, source: 'why' };
  return { lines: linesFrom(row.node && row.node.meta), source: 'node' };
}

function stepLines(step) {
  return stepLineInfo(step).lines;
}

/** "src/app.py line 120", "session a1b2c3…", "job 7" — where the step lands. */
function stepWhereText(step) {
  const row = step && typeof step === 'object' ? step : {};
  const node = row.node ? normalizeNode(row.node) : null;
  if (!node) return '';
  const meta = node.meta || {};
  const info = stepLineInfo(row);
  const lines = info.lines;
  if (node.kind === 'file') {
    const path = String(meta.path || node.detail || node.label || '');
    if (!lines.length) return path;
    // Only a line THIS step names is presented as this step's line; the file
    // node's own list is every line anything recorded against it, and says so.
    if (info.source === 'node' && lines.length > 1) {
      return `${path} — lines recorded here: ${lines.join(', ')}`;
    }
    return `${path} line${lines.length > 1 ? 's' : ''} ${lines.join(', ')}`;
  }
  // Below: say what the label does NOT already say, and nothing more. A "where"
  // that repeats the label is noise in a panel meant to be read.
  if (node.kind === 'chat') {
    const session = String(meta.session_id || '');
    return session && !node.label.includes(session) ? `chat session ${session}` : '';
  }
  if (node.kind === 'checkpoint') {
    const job = String(meta.job_id || '');
    return job && !node.label.includes(job) ? `dispatch job ${job}` : '';
  }
  if (node.kind === 'corpus') {
    const slug = String(meta.slug || '');
    if (!slug) return '';
    const source = String(meta.source || '');
    return source && source !== node.label ? `${source} · ${slug} corpus` : `${slug} corpus`;
  }
  return '';
}

const PV_TERMINUS_RANK = { file: 0, chat: 1, corpus: 2, checkpoint: 3 };

/**
 * The record the chain ends at: the nearest step that lands on something a
 * human can open — a file (with its line), a chat session, a corpus page, a
 * job. Nearest, not deepest: hop 1 IS the answer to "where did this come from",
 * and the deeper hops are context around it.
 */
function explainTerminus(steps) {
  let best = null;
  let bestScore = Infinity;
  const list = Array.isArray(steps) ? steps : [];
  for (let i = 0; i < list.length; i += 1) {
    const step = normalizeStep(list[i], i);
    const kind = step.node ? step.node.kind : '';
    if (!(kind in PV_TERMINUS_RANK)) continue;
    const score = step.hop * 100 + PV_TERMINUS_RANK[kind] * 10 + Math.min(9, step.order);
    if (score < bestScore) { bestScore = score; best = step; }
  }
  if (!best) return null;
  return { step: best, node: best.node, lines: stepLines(best), text: stepWhereText(best) };
}

function explainTerminusHtml(steps) {
  const end = explainTerminus(steps);
  if (!end) {
    return '<p class="pv-terminus is-none">No chat, file or job record ends this chain — '
      + 'nothing stored says where this came from.</p>';
  }
  return '<p class="pv-terminus"><span class="pv-terminus-label">Traced to</span>'
    + nodeChipHtml(end.node)
    + `<button type="button" class="pv-link" data-pv-node-open="${pvEsc(end.node.id)}">`
    + `${pvEsc(end.text || end.node.label)}</button></p>`;
}

/** The node record it lands on, as a button that walks the chain onwards. */
function stepTargetHtml(step) {
  const row = normalizeStep(step);
  const other = row.direction === 'rests_on' ? row.to : row.from;
  if (!row.node) {
    return `<p class="pv-step-target is-missing">${pvEsc(other)}</p>`;
  }
  const said = stepWhereText(row);
  const where = said === row.node.label ? '' : said;
  const label = pvEsc(pvShort(row.node.label, 70));
  return `<button type="button" class="pv-step-target" data-pv-node-open="${pvEsc(row.node.id)}" `
    + `title="${pvEsc(nodeTooltipText(row.node))}">`
    + `${nodeChipHtml(row.node)}<span class="pv-step-target-label">${label}</span>`
    + `${where ? `<span class="pv-step-where">${pvEsc(where)}</span>` : ''}</button>`;
}

/** One step of the evidence chain. The `why` is printed exactly as it arrived. */
function explainStepHtml(step, index = 0) {
  const row = normalizeStep(step, index);
  const declared = row.trust === 'declared';
  const direction = row.direction === 'vouches_for'
    ? '<span class="pv-step-dir" title="an edge pointing AT this node: a record made about it">vouches for it</span>'
    : '<span class="pv-step-dir" title="an edge out of this node: what it rests on">rests on</span>';
  return `<li class="pv-step pv-step-${pvEsc(pvToken(row.kind))}" data-pv-step="${row.order}">`
    + '<div class="pv-step-head">'
    + `<span class="pv-step-order">${row.order}</span>`
    + `<span class="pv-step-hop" title="edges away from the node you picked">hop ${row.hop}</span>`
    + `<span class="pv-step-kind">${pvEsc(row.kind)}</span>`
    + direction
    + `<span class="pv-step-conf" title="the confidence stored on this edge">conf ${pvEsc(pvConfidence(row.confidence))}</span>`
    + `<span class="pv-step-trust${declared ? ' is-declared' : ' is-inferred'}" `
    + `title="${declared ? 'read from a stored record, not asserted by a model' : 'NOT a declared record — treat with care'}">`
    + `${pvEsc(row.trust)}</span>`
    + '</div>'
    + `<p class="pv-step-why">${pvEsc(row.why)}</p>`
    + stepTargetHtml(row)
    + '</li>';
}

/** A node's own stored fields, shown as they are — never re-derived. */
function nodeMetaHtml(node) {
  const row = normalizeNode(node);
  const meta = row.meta || {};
  const keys = ['status', 'priority', 'level', 'maturity', 'trust_class', 'effective_score',
    'harmful_ratio', 'path', 'session_id', 'job_id', 'slug', 'source', 'project', 'owner',
    'updated_at', 'workspace', 'verdict'];
  const rows = [];
  for (const key of keys) {
    const value = meta[key];
    if (value === undefined || value === null || value === '') continue;
    rows.push(`<div class="pv-meta-row"><span class="pv-meta-key">${pvEsc(key)}</span>`
      + `<span class="pv-meta-value">${pvEsc(pvShort(String(value), 120))}</span></div>`);
  }
  const lines = linesFrom(meta);
  if (lines.length) {
    rows.push('<div class="pv-meta-row"><span class="pv-meta-key">'
      + `line${lines.length > 1 ? 's' : ''}</span>`
      + `<span class="pv-meta-value">${pvEsc(lines.join(', '))}</span></div>`);
  }
  return rows.length ? `<div class="pv-meta">${rows.join('')}</div>` : '';
}

/** The side panel with nothing picked yet. */
function sideEmptyHtml(state = {}) {
  const st = state && typeof state === 'object' ? state : {};
  if (st.error) return `<div class="pv-side-card"><p class="pv-error-block">${pvEsc(st.error)}</p></div>`;
  return '<div class="pv-side-card pv-side-hint">'
    + '<h3>Why does the agent believe this?</h3>'
    + '<p>Pick a node — on the canvas, in the orphan list, or in a duplicate pair — and its '
    + 'evidence chain appears here: every declared record that accounts for it, in order, '
    + 'ending at the chat, file and line it came from.</p>'
    + '<p class="pv-muted">Every edge in this graph was read from something already stored. '
    + 'Nothing here was asserted by a model.</p></div>';
}

/** The evidence chain for one node — value #1 of the whole feature. */
function explainPanelHtml(payload, state = {}) {
  const st = state && typeof state === 'object' ? state : {};
  if (st.loading) return '<div class="pv-side-card"><p class="pv-muted">Following the evidence…</p></div>';
  const data = normalizeExplain(payload);
  if (!data.node) return sideEmptyHtml(st);
  const node = data.node;
  const steps = data.steps;
  return '<section class="pv-explain" data-pv-explain>'
    + '<header class="pv-explain-head">'
    + `<div class="pv-explain-title">${nodeChipHtml(node)}`
    + `<h3 class="pv-explain-label">${pvEsc(node.label)}</h3></div>`
    + `${node.detail ? `<p class="pv-explain-detail">${pvEsc(node.detail)}</p>` : ''}`
    + `<p class="pv-explain-id"><code>${pvEsc(node.id)}</code></p>`
    + nodeMetaHtml(node)
    + '</header>'
    + `<p class="pv-explain-summary">${pvEsc(data.summary)}</p>`
    + explainTerminusHtml(steps)
    + (steps.length
      ? `<ol class="pv-steps">${steps.map((step, i) => explainStepHtml(step, i)).join('')}</ol>`
      : '<p class="pv-placeholder">Nothing stored points at this node — in this graph it is an '
        + 'orphan, and no record explains it.</p>')
    + '<div class="pv-side-actions">'
    + `<button type="button" class="pv-btn" data-pv-neighbors="${pvEsc(node.id)}">`
    + 'What breaks if I touch this</button>'
    + `<button type="button" class="pv-btn" data-pv-focus="${pvEsc(node.id)}">Centre on the canvas</button>`
    + '</div>'
    + `<p class="pv-error" data-pv-side-error${st.error ? '' : ' hidden'}>${pvEsc(st.error || '')}</p>`
    + '</section>';
}

// ── rendering: neighbours and impact ──────────────────────────────────────

/** "What breaks if I touch this": the reversed depends_on / changed closure. */
function impactListHtml(nodes) {
  const list = (Array.isArray(nodes) ? nodes : []).map(normalizeNode).filter(n => n.id);
  if (!list.length) {
    return '<p class="pv-impact-none">Nothing declared depends on this node. Touching it breaks '
      + 'nothing the graph knows about — which is not the same as nothing at all.</p>';
  }
  const rows = list.map(node => '<li class="pv-impact-row">'
    + `${nodeChipHtml(node)}<button type="button" class="pv-link" data-pv-node-open="${pvEsc(node.id)}">`
    + `${pvEsc(pvShort(node.label, 70))}</button></li>`).join('');
  return `<ul class="pv-impact-list">${rows}</ul>`;
}

/** The subgraph around one node, at 1–3 hops, plus the impact set. */
function neighborsPanelHtml(payload, state = {}) {
  const st = state && typeof state === 'object' ? state : {};
  if (st.loading) return '<div class="pv-side-card"><p class="pv-muted">Walking the neighbourhood…</p></div>';
  const data = normalizeNeighbors(payload);
  if (!data.root) return sideEmptyHtml(st);
  const root = data.nodes.find(node => node.id === data.root) || normalizeNode({ id: data.root });
  const view = graphViewModel({ nodes: data.nodes, edges: data.edges },
    { selected: data.root, drawLimit: PV_MAX_DRAWN });
  const layout = layoutGraph(view.nodes, view.edges, { width: 520, height: 380, pad: 34 });
  const hops = [1, 2, 3].map(hop => `<button type="button" class="pv-hop`
    + `${hop === data.hops ? ' is-on' : ''}" data-pv-hops="${hop}" `
    + `aria-pressed="${hop === data.hops ? 'true' : 'false'}">${hop} hop${hop > 1 ? 's' : ''}</button>`).join('');
  return '<section class="pv-neighbors" data-pv-neighbors-panel>'
    + '<header class="pv-explain-head">'
    + `<div class="pv-explain-title">${nodeChipHtml(root)}`
    + `<h3 class="pv-explain-label">${pvEsc(root.label)}</h3></div>`
    + `<p class="pv-explain-id"><code>${pvEsc(data.root)}</code></p>`
    + `<div class="pv-hops" role="group" aria-label="Hops">${hops}</div>`
    + '</header>'
    + '<h4 class="pv-side-heading">What breaks if I touch this</h4>'
    + `<p class="pv-muted">${data.impact.length} node(s) reachable by reversed `
    + '<code>depends_on</code> / <code>changed</code> edges.</p>'
    + impactListHtml(data.impact)
    + '<h4 class="pv-side-heading">The neighbourhood</h4>'
    + `<p class="pv-muted">${data.nodes.length} node(s), ${data.edges.length} edge(s) within `
    + `${data.hops} hop(s).</p>`
    + `<div class="pv-canvas pv-canvas-mini" data-pv-canvas>`
    + `${graphSvgHtml(view, layout, { prefix: 'pvmini' })}`
    + '<div class="pv-tip" data-pv-tip hidden></div></div>'
    + '<div class="pv-side-actions">'
    + `<button type="button" class="pv-btn" data-pv-node-open="${pvEsc(data.root)}">`
    + 'Back to the evidence chain</button></div>'
    + `<p class="pv-error" data-pv-side-error${st.error ? '' : ' hidden'}>${pvEsc(st.error || '')}</p>`
    + '</section>';
}

// ── rendering: orphans and duplicates ─────────────────────────────────────

/** The excerpt the detector quoted inside its own `why`, if it quoted one. */
function duplicateExcerpt(why) {
  const text = String(why == null ? '' : why);
  const match = /[“"]([^“”"]+)[”"]\s*$/.exec(text.trim());
  return match ? match[1] : '';
}

/** "2 verified span(s): chars 0–37 ↔ 4–41" — offsets into the normalized text. */
function duplicateSpansText(spans) {
  const list = (Array.isArray(spans) ? spans : []).filter(span => Array.isArray(span) && span.length >= 2);
  if (!list.length) return '';
  const shown = list.slice(0, 4).map(span => {
    const [a0, a1] = span[0];
    const [b0, b1] = span[1];
    return `${a0}–${a1} ↔ ${b0}–${b1}`;
  }).join(', ');
  const rest = list.length > 4 ? `, +${list.length - 4} more` : '';
  return `${list.length} verified span(s), in normalized characters: ${shown}${rest}`;
}

/** One near-duplicate pair: both labels, the measured ratio, the why, the span. */
function duplicateRowHtml(pair) {
  const row = normalizeDuplicate(pair);
  const excerpt = duplicateExcerpt(row.why);
  const spans = duplicateSpansText(row.spans);
  return `<li class="pv-dup" data-pv-dup="${pvEsc(`${row.a}|${row.b}`)}">`
    + '<div class="pv-dup-pair">'
    + `<button type="button" class="pv-dup-side" data-pv-node-open="${pvEsc(row.a)}">`
    + `${pvEsc(pvShort(row.a_label, 90))}</button>`
    + '<span class="pv-dup-vs" aria-label="near-duplicate of">≈</span>'
    + `<button type="button" class="pv-dup-side" data-pv-node-open="${pvEsc(row.b)}">`
    + `${pvEsc(pvShort(row.b_label, 90))}</button>`
    + `<span class="pv-dup-ratio" title="measured overlap, not an estimate">${pvEsc(pvPercent(row.ratio))}</span>`
    + '</div>'
    + `<p class="pv-dup-why">${pvEsc(row.why)}</p>`
    + (excerpt
      ? '<p class="pv-dup-span"><span class="pv-dup-span-label">verified shared text</span>'
        + `<q>${pvEsc(excerpt)}</q></p>`
      : '')
    + (spans ? `<p class="pv-dup-offsets">${pvEsc(spans)}</p>` : '')
    + '</li>';
}

/** Orphans grouped by kind, then the duplicate pairs — value #2 of the report. */
function orphansPanelHtml(payload, state = {}) {
  const st = state && typeof state === 'object' ? state : {};
  if (st.loading) return '<div class="pv-placeholder">Looking for what is floating…</div>';
  const data = normalizeOrphans(payload);
  if (data.enabled === false) return disabledHtml();
  const kinds = Object.keys(data.orphans);
  // A failed read must not read as a tidy workspace.
  if (st.error && !kinds.length && !data.duplicates.length) {
    return `<p class="pv-error-block">${pvEsc(st.error)}</p>`;
  }
  const groups = kinds.map(kind => {
    const rows = data.orphans[kind].map(node => '<li class="pv-orphan-row">'
      + `${nodeChipHtml(node)}<button type="button" class="pv-link" data-pv-node-open="${pvEsc(node.id)}">`
      + `${pvEsc(pvShort(node.label, 90))}</button>`
      + `${node.detail ? `<span class="pv-orphan-detail">${pvEsc(pvShort(node.detail, 70))}</span>` : ''}`
      + '</li>').join('');
    return `<details class="pv-orphan-group" open><summary>${pvEsc(kind)} `
      + `<b>${data.orphans[kind].length}</b></summary><ul>${rows}</ul></details>`;
  }).join('');
  const duplicates = data.duplicates.map(duplicateRowHtml).join('');
  const stats = data.stats && typeof data.stats === 'object' ? data.stats : {};
  return '<div class="pv-orphans">'
    + '<section class="pv-orphan-section">'
    + `<h3>Orphans <b>${data.count}</b></h3>`
    + '<p class="pv-muted">Nodes no declared edge touches. Nothing points at them, and they point '
    + 'at nothing — so nothing in the workspace accounts for them.'
    + `${stats.nodes ? ` (${stats.nodes} node(s), ${stats.edges || 0} edge(s) in scope.)` : ''}</p>`
    + (groups || '<p class="pv-placeholder">No orphans: every node is connected to something.</p>')
    + '</section>'
    + '<section class="pv-orphan-section">'
    + `<h3>Near-duplicates <b>${data.duplicates.length}</b></h3>`
    + '<p class="pv-muted">Found positionally and then verified with an exact substring compare — '
    + 'a ratio here is measured, never a model’s opinion.</p>'
    + (duplicates
      ? `<ul class="pv-dups">${duplicates}</ul>`
      : '<p class="pv-placeholder">Nothing is said twice above the detector’s threshold.</p>')
    + '</section>'
    + `<p class="pv-error" data-pv-error${st.error ? '' : ' hidden'}>${pvEsc(st.error || '')}</p>`
    + '</div>';
}

// ── Provenance: end pure helpers ──

export {
  explainPanelHtml, explainStepHtml, explainTerminus, graphPanelHtml, graphSvgHtml,
  graphViewModel, layoutGraph, neighborsPanelHtml, orphansPanelHtml, duplicateRowHtml,
  encodeNodeId, graphQuery, normalizeGraph, normalizeExplain, normalizeOrphans,
};

const $ = (id) => document.getElementById(id);

const MODAL_ID = 'provenance-modal';
const TOOLBAR_ID = 'provenance-toolbar';
const MAIN_ID = 'provenance-main';
const SIDE_ID = 'provenance-side';

const EMPTY_GRAPH = {
  nodes: [], edges: [], sources: {}, stats: {}, truncated: false, enabled: true,
  limit: 0, node_kinds: PV_NODE_KINDS.slice(), edge_kinds: PV_EDGE_KINDS.slice(),
};

let _graph = EMPTY_GRAPH;
let _orphans = null;
let _explain = null;
let _neighbors = null;
let _state = {
  tab: 'graph', kinds: [], query: '', project: '', workspace: '', selected: '',
  hops: 2, error: '', loading: false, side: 'explain', sideLoading: false, sideError: '',
};
let _wired = false;
let _loaded = false;
let _returnFocus = null;
let _zoom = { k: 1, x: 0, y: 0 };
let _pan = null;

/** fetch wrapper for /api/provenance/*: a non-2xx becomes an Error with {detail}. */
async function req(path) {
  const res = await fetch(`${API}${path}`, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* non-JSON body */ }
  if (!res.ok) {
    const detail = data && data.detail != null
      ? (typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail))
      : '';
    throw new Error(detail || `HTTP ${res.status}`);
  }
  return data;
}

export const fetchGraph = (state) => req(`/graph${graphQuery(state)}`);
export const fetchExplain = (nodeId, state) =>
  req(`/node/${encodeNodeId(nodeId)}/explain${scopeQuery(state)}`);
export const fetchNeighbors = (nodeId, hops, state) => {
  const scope = scopeQuery(state);
  const join = scope ? '&' : '?';
  return req(`/node/${encodeNodeId(nodeId)}/neighbors${scope}${join}hops=${encodeURIComponent(hops)}`);
};
export const fetchOrphans = (state) => req(`/orphans${scopeQuery(state)}`);

/** The folder the app is bound to, so the graph opens on something real. */
function currentWorkspace() {
  try {
    if (window.workspaceModule && window.workspaceModule.getWorkspace) {
      const bound = window.workspaceModule.getWorkspace();
      if (bound) return String(bound);
    }
  } catch (_) { /* module not loaded yet */ }
  try {
    const raw = localStorage.getItem('odysseus-workspace');
    if (!raw) return '';
    try {
      const value = JSON.parse(raw);
      return typeof value === 'string' ? value : (value && value.path) || '';
    } catch (_) { return raw; }
  } catch (_) { return ''; }
}

// ── rendering ─────────────────────────────────────────────────────────────

function renderToolbar() {
  const host = $(TOOLBAR_ID);
  if (!host) return;
  const active = document.activeElement;
  const focused = active && active.dataset
    ? ['pvSearch', 'pvProject', 'pvWorkspace'].find(name => name in active.dataset)
    : null;
  const caret = focused && typeof active.selectionStart === 'number' ? active.selectionStart : null;
  host.innerHTML = toolbarHtml(_state);
  if (!focused) return;
  const attr = { pvSearch: 'data-pv-search', pvProject: 'data-pv-project', pvWorkspace: 'data-pv-workspace' }[focused];
  const next = host.querySelector(`[${attr}]`);
  if (!next) return;
  next.focus();
  if (caret != null) { try { next.setSelectionRange(caret, caret); } catch (_) { /* not a text input */ } }
}

function renderMain() {
  const host = $(MAIN_ID);
  if (!host) return;
  if (_state.tab === 'orphans') {
    host.innerHTML = orphansPanelHtml(_orphans, { loading: _state.loading, error: _state.error });
    return;
  }
  host.innerHTML = graphPanelHtml(_graph, {
    kinds: _state.kinds,
    query: _state.query,
    selected: _state.selected,
    loading: _state.loading,
    error: _state.error,
  });
  applyZoom();
}

function renderSide() {
  const host = $(SIDE_ID);
  if (!host) return;
  if (_state.side === 'neighbors') {
    host.innerHTML = neighborsPanelHtml(_neighbors,
      { loading: _state.sideLoading, error: _state.sideError });
    return;
  }
  host.innerHTML = explainPanelHtml(_explain,
    { loading: _state.sideLoading, error: _state.sideError });
}

function renderAll() {
  renderToolbar();
  renderMain();
  renderSide();
}

/** Errors land inline, next to what failed — never in a native dialog. */
function inlineError(message, where = 'main') {
  _state[where === 'side' ? 'sideError' : 'error'] = message || '';
  const modal = $(MODAL_ID);
  if (!modal) return;
  const attr = where === 'side' ? '[data-pv-side-error]' : '[data-pv-error]';
  const slots = modal.querySelectorAll(attr);
  slots.forEach(slot => {
    slot.textContent = message || '';
    slot.hidden = !message;
  });
}

// ── loading ───────────────────────────────────────────────────────────────

export async function loadGraph(force = false) {
  if (_loaded && !force) return _graph;
  _state.loading = true;
  inlineError('');
  renderMain();
  try {
    _graph = normalizeGraph(await fetchGraph(_state));
    _loaded = true;
  } catch (error) {
    _graph = EMPTY_GRAPH;
    inlineError(`Could not read the provenance graph: ${error.message || error}`);
  } finally {
    _state.loading = false;
    renderToolbar();
    renderMain();
  }
  return _graph;
}

export async function loadOrphans(force = false) {
  if (_orphans && !force) return _orphans;
  _state.loading = true;
  inlineError('');
  renderMain();
  try {
    _orphans = normalizeOrphans(await fetchOrphans(_state));
  } catch (error) {
    _orphans = null;
    inlineError(`Could not read the orphans: ${error.message || error}`);
  } finally {
    _state.loading = false;
    renderToolbar();
    renderMain();
  }
  return _orphans;
}

/** Pick a node: its evidence chain, which is what this page is for. */
export async function openNode(nodeId) {
  const id = String(nodeId || '');
  if (!id) return;
  _state.selected = id;
  _state.side = 'explain';
  _state.sideLoading = true;
  _state.sideError = '';
  renderSide();
  try {
    _explain = normalizeExplain(await fetchExplain(id, _state));
  } catch (error) {
    _explain = null;
    _state.sideError = `Could not explain ${id}: ${error.message || error}`;
  } finally {
    _state.sideLoading = false;
    renderSide();
    markSelected();
  }
}

export async function openNeighbors(nodeId, hops) {
  const id = String(nodeId || _state.selected || '');
  if (!id) return;
  const walk = Math.max(1, Math.min(3, pvNum(hops, _state.hops) || 2));
  _state.selected = id;
  _state.hops = walk;
  _state.side = 'neighbors';
  _state.sideLoading = true;
  _state.sideError = '';
  renderSide();
  try {
    _neighbors = normalizeNeighbors(await fetchNeighbors(id, walk, _state));
  } catch (error) {
    _neighbors = null;
    _state.sideError = `Could not walk from ${id}: ${error.message || error}`;
  } finally {
    _state.sideLoading = false;
    renderSide();
  }
}

/** Move the selection ring without redrawing (and so without losing the view). */
function markSelected() {
  const host = $(MAIN_ID);
  if (!host) return;
  host.querySelectorAll('[data-pv-node]').forEach(group => {
    group.classList.toggle('is-selected', group.dataset.pvNode === _state.selected);
  });
}

/** Search highlights in place: no re-layout, so the picture stays where it was. */
function highlightMatches() {
  const host = $(MAIN_ID);
  if (!host) return;
  const query = String(_state.query || '').trim();
  const groups = host.querySelectorAll('[data-pv-node]');
  let matches = 0;
  groups.forEach(group => {
    const hit = query ? matchesHaystack(group.dataset.pvHay || '', query) : false;
    if (hit) matches += 1;
    group.classList.toggle('is-match', hit);
    group.classList.toggle('is-dim', Boolean(query) && !hit && group.dataset.pvNode !== _state.selected);
  });
  const label = host.querySelector('[data-pv-matches]');
  if (label) label.textContent = query ? `${matches} node(s) match “${query}”` : '';
}

// ── pan / zoom ────────────────────────────────────────────────────────────

function zoomGroup() {
  const host = $(MAIN_ID);
  return host ? host.querySelector('[data-pv-zoom]') : null;
}

function applyZoom() {
  const group = zoomGroup();
  if (group) group.setAttribute('transform', `translate(${_zoom.x} ${_zoom.y}) scale(${_zoom.k})`);
}

function resetZoom() {
  _zoom = { k: 1, x: 0, y: 0 };
  applyZoom();
}

/** Pointer position in the SVG's own user units, before the zoom transform. */
function svgPoint(svg, clientX, clientY) {
  try {
    const matrix = svg.getScreenCTM();
    if (!matrix) return null;
    const point = svg.createSVGPoint();
    point.x = clientX;
    point.y = clientY;
    const local = point.matrixTransform(matrix.inverse());
    return { x: local.x, y: local.y, scale: matrix.a || 1 };
  } catch (_) { return null; }
}

function onWheel(event) {
  const canvas = event.target.closest ? event.target.closest('[data-pv-canvas]') : null;
  if (!canvas || canvas.classList.contains('pv-canvas-mini')) return;
  const svg = canvas.querySelector('[data-pv-svg]');
  if (!svg) return;
  event.preventDefault();
  const point = svgPoint(svg, event.clientX, event.clientY);
  if (!point) return;
  const next = Math.max(0.3, Math.min(6, _zoom.k * (event.deltaY < 0 ? 1.12 : 1 / 1.12)));
  // Keep whatever is under the cursor under the cursor.
  _zoom.x = point.x - ((point.x - _zoom.x) / _zoom.k) * next;
  _zoom.y = point.y - ((point.y - _zoom.y) / _zoom.k) * next;
  _zoom.k = next;
  applyZoom();
}

function onPointerDown(event) {
  const canvas = event.target.closest ? event.target.closest('[data-pv-canvas]') : null;
  if (!canvas || canvas.classList.contains('pv-canvas-mini')) return;
  if (event.target.closest('[data-pv-node]')) return;
  const svg = canvas.querySelector('[data-pv-svg]');
  if (!svg) return;
  const point = svgPoint(svg, event.clientX, event.clientY);
  if (!point) return;
  _pan = { clientX: event.clientX, clientY: event.clientY, x: _zoom.x, y: _zoom.y, scale: point.scale };
  canvas.classList.add('is-panning');
}

function onPointerMove(event) {
  if (!_pan) return;
  const scale = _pan.scale || 1;
  _zoom.x = _pan.x + (event.clientX - _pan.clientX) / scale;
  _zoom.y = _pan.y + (event.clientY - _pan.clientY) / scale;
  applyZoom();
}

function onPointerUp() {
  _pan = null;
  const host = $(MAIN_ID);
  if (host) host.querySelectorAll('[data-pv-canvas]').forEach(c => c.classList.remove('is-panning'));
}

/** Bring one node to the middle of the canvas at a readable zoom. */
function focusNode(nodeId) {
  const host = $(MAIN_ID);
  if (!host) return;
  // A node id carries ":" and "/", so it is matched by value rather than
  // spliced into a selector.
  const wanted = String(nodeId);
  let group = null;
  host.querySelectorAll('[data-pv-node]').forEach(candidate => {
    if (!group && candidate.dataset.pvNode === wanted) group = candidate;
  });
  const svg = host.querySelector('[data-pv-svg]');
  if (!group || !svg) return;
  const match = /translate\(([-\d.]+)[ ,]+([-\d.]+)\)/.exec(group.getAttribute('transform') || '');
  const box = (svg.getAttribute('viewBox') || '').split(/\s+/).map(Number);
  if (!match || box.length !== 4) return;
  _zoom.k = Math.max(_zoom.k, 1.6);
  _zoom.x = box[2] / 2 - Number(match[1]) * _zoom.k;
  _zoom.y = box[3] / 2 - Number(match[2]) * _zoom.k;
  applyZoom();
}

// ── hover tooltip ─────────────────────────────────────────────────────────

function showTip(event) {
  const group = event.target.closest ? event.target.closest('[data-pv-node]') : null;
  const canvas = event.target.closest ? event.target.closest('[data-pv-canvas]') : null;
  if (!canvas) return;
  const tip = canvas.querySelector('[data-pv-tip]');
  if (!tip) return;
  if (!group) { tip.hidden = true; return; }
  const rect = canvas.getBoundingClientRect();
  tip.textContent = group.dataset.pvTitle || '';
  tip.hidden = false;
  const left = Math.min(Math.max(8, event.clientX - rect.left + 14), Math.max(8, rect.width - 260));
  const top = Math.min(Math.max(8, event.clientY - rect.top + 14), Math.max(8, rect.height - 60));
  tip.style.left = `${left}px`;
  tip.style.top = `${top}px`;
}

function hideTips() {
  const modal = $(MODAL_ID);
  if (modal) modal.querySelectorAll('[data-pv-tip]').forEach(tip => { tip.hidden = true; });
}

// ── wiring: delegated listeners on the modal, data attributes on the markup ──

function wire() {
  if (_wired) return;
  const modal = $(MODAL_ID);
  if (!modal) return;
  _wired = true;

  modal.addEventListener('click', (event) => {
    const target = event.target;
    if (target.closest('#close-provenance-modal')) { closeProvenancePanel(); return; }
    const tab = target.closest('[data-pv-tab]');
    if (tab) { switchTab(tab.dataset.pvTab); return; }
    const kind = target.closest('[data-pv-kind]');
    if (kind) { toggleKind(kind.dataset.pvKind); return; }
    if (target.closest('[data-pv-kind-clear]')) { _state.kinds = []; renderMain(); renderToolbar(); return; }
    if (target.closest('[data-pv-reload]')) { reload(); return; }
    if (target.closest('[data-pv-zoom-reset]')) { resetZoom(); return; }
    const open = target.closest('[data-pv-node-open]');
    if (open) { openNode(open.dataset.pvNodeOpen); return; }
    const neighbours = target.closest('[data-pv-neighbors]');
    if (neighbours) { openNeighbors(neighbours.dataset.pvNeighbors, _state.hops); return; }
    const hops = target.closest('[data-pv-hops]');
    if (hops) { openNeighbors(_state.selected, Number(hops.dataset.pvHops)); return; }
    const focus = target.closest('[data-pv-focus]');
    if (focus) { focusNode(focus.dataset.pvFocus); return; }
    const node = target.closest('[data-pv-node]');
    if (node) openNode(node.dataset.pvNode);
  });

  modal.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    const node = event.target.closest ? event.target.closest('[data-pv-node]') : null;
    if (!node) return;
    event.preventDefault();
    openNode(node.dataset.pvNode);
  });

  modal.addEventListener('input', (event) => {
    if (event.target.matches('[data-pv-search]')) {
      _state.query = event.target.value;
      highlightMatches();
      return;
    }
    if (event.target.matches('[data-pv-project]')) { _state.project = event.target.value; return; }
    if (event.target.matches('[data-pv-workspace]')) _state.workspace = event.target.value;
  });

  modal.addEventListener('wheel', onWheel, { passive: false });
  modal.addEventListener('pointerdown', onPointerDown);
  modal.addEventListener('pointermove', (event) => { onPointerMove(event); showTip(event); });
  modal.addEventListener('pointerup', onPointerUp);
  modal.addEventListener('pointerleave', () => { onPointerUp(); hideTips(); });
  // A drag that ends outside the modal must still end the pan.
  document.addEventListener('pointerup', onPointerUp);
  document.addEventListener('pointercancel', onPointerUp);

  $('tool-provenance-btn')?.addEventListener('click', () => openProvenancePanel());
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    const open = $(MODAL_ID);
    if (open && !open.classList.contains('hidden')) closeProvenancePanel();
  });
}

function switchTab(tab) {
  _state.tab = tab === 'orphans' ? 'orphans' : 'graph';
  renderToolbar();
  renderMain();
  if (_state.tab === 'orphans' && !_orphans) loadOrphans(true);
}

function toggleKind(kind) {
  const token = pvToken(kind);
  if (!token) return;
  _state.kinds = _state.kinds.includes(token)
    ? _state.kinds.filter(name => name !== token)
    : _state.kinds.concat([token]);
  renderMain();
  renderToolbar();
}

async function reload() {
  _loaded = false;
  _orphans = null;
  resetZoom();
  if (_state.tab === 'orphans') { await loadOrphans(true); return; }
  await loadGraph(true);
}

export async function openProvenancePanel(options = {}) {
  const modal = $(MODAL_ID);
  if (!modal) return;
  wire();
  _returnFocus = document.activeElement;
  modal.classList.remove('hidden');
  if (!_state.workspace) _state.workspace = currentWorkspace();
  if (options.project) _state.project = String(options.project);
  renderAll();
  await loadGraph(true);
  if (options.node) await openNode(options.node);
}

export function closeProvenancePanel() {
  $(MODAL_ID)?.classList.add('hidden');
  hideTips();
  try { _returnFocus?.focus?.(); } catch (_) { /* the trigger went away */ }
}

export function initProvenance() {
  wire();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initProvenance);
} else {
  initProvenance();
}

const provenanceModule = {
  initProvenance,
  openProvenancePanel,
  closeProvenancePanel,
  loadGraph,
  loadOrphans,
  openNode,
  openNeighbors,
  fetchGraph,
  fetchExplain,
  fetchNeighbors,
  fetchOrphans,
};

if (typeof window !== 'undefined') window.provenanceModule = provenanceModule;

export default provenanceModule;
