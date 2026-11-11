import {
  createContext, memo, useCallback, useContext, useEffect, useMemo, useRef, useState,
  type ClipboardEvent as ReactClipboardEvent, type DragEvent as ReactDragEvent, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent,
} from 'react';
import {
  AlignCenterHorizontal, AlignCenterVertical, AlignEndHorizontal, AlignEndVertical, AlignHorizontalSpaceBetween, AlignStartHorizontal, AlignStartVertical,
  AlignVerticalSpaceBetween, BringToFront, Circle, Copy, Diamond, Download, Frame as FrameIcon, Group, Hand, ImagePlus, Maximize, Maximize2, Minimize2,
  MousePointer2, Pencil, Redo2, SendToBack, Square, StickyNote, Trash2, Type, Undo2, Ungroup, Upload,
} from 'lucide-react';
import {
  Background, BackgroundVariant, BaseEdge, ConnectionMode, EdgeLabelRenderer, Handle, MarkerType, MiniMap, NodeResizer, Position, ReactFlow,
  ReactFlowProvider, SelectionMode, ViewportPortal, applyNodeChanges, useInternalNode, useReactFlow, useStore, useUpdateNodeInternals,
  type Connection, type Edge, type EdgeProps, type Node, type NodeChange, type NodeProps,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { Button, IconButton, Menu } from '../../components';
import {
  CLIENT_ID, boardAssetUrl, boardCanvasUrl, boardState, boardSvgUrl, getBoard, importCanvas, onSurfaceEvent, patchBoard, renameBoard, uploadBoardAsset,
  type SurfaceActor,
} from '../../adapters/surfaces';
import {
  PALETTE, alignRects, applyPatch, borderPoint, bounds, colorHex, diffDocs, duplicateItems, frameAround, framesHolding, kindOf, makeNode, mergePatches,
  newId, patchSize, snapBox, strokeNode, strokePath, textOf, withText,
  type AlignHow, type Doc, type Guide, type JcEdge, type JcNode, type Kind, type Patch,
} from '../../lib/pizarraModel';
import { t } from '../../i18n';
import './pizarra.css';

/**
 * Pizarra: a whiteboard in the side panel. The board is a JSON Canvas file on the server (src/pizarra.py); this
 * screen edits it by hand and shows what the agent does to it as it happens (items appear, a named cursor moves).
 * The canvas itself (pan, zoom, selection, resize, minimap) is @xyflow/react (MIT); everything else is here.
 */

type PzData = { jc: JcNode; fresh?: boolean };
type PzNode = Node<PzData, 'item'>;
type EdgeData = { jc: JcEdge };
type PzEdge = Edge<EdgeData, 'conn'>;
type Tool = 'select' | 'hand' | 'sticky' | 'text' | 'rect' | 'ellipse' | 'diamond' | 'frame' | 'pen';
const CREATE_TOOLS: Tool[] = ['sticky', 'text', 'rect', 'ellipse', 'diamond', 'frame'];
const SHORTCUT: Record<string, Tool> = { v: 'select', h: 'hand', s: 'sticky', t: 'text', r: 'rect', o: 'ellipse', d: 'diamond', f: 'frame', p: 'pen' };
const MAX_UNDO = 120;

interface Ctx {
  editing: string | null; editingEdge: string | null; boardId: string; interactive: boolean;
  stopEdit: (id: string, text?: string) => void; stopEdgeEdit: (id: string, label?: string) => void;
  resized: (id: string, r: { x: number; y: number; width: number; height: number }) => void;
}
const PzContext = createContext<Ctx | null>(null);

/* ── an item on the board ── */

/** The one handle every item always has: connectors draw themselves, but the canvas only renders an edge between nodes that have a handle. */
const Anchor = memo(function Anchor() {
  return <Handle id="c" type="source" position={Position.Top} isConnectable={false} className="fs-pz-anchor" />;
});

const ItemNode = memo(function ItemNode({ id, data, selected }: NodeProps<PzNode>) {
  const ctx = useContext(PzContext)!;
  const jc = data.jc;
  const kind = kindOf(jc);
  const editing = ctx.editing === id;
  const color = colorHex(jc.color);
  const text = textOf(jc);
  const style = color ? ({ ['--pz-c' as string]: color } as React.CSSProperties) : undefined;
  // Far away the items are plain coloured shapes: no text, handles or resizers, so hundreds still pan smoothly.
  const far = useStore((s) => s.transform[2] < 0.3);
  const connecting = useStore((s) => s.connection.inProgress);
  const [hot, setHot] = useState(false);
  const showHandles = kind !== 'stroke' && kind !== 'frame' && (hot || selected || connecting);
  // Handles that mount later are unknown to the canvas until it measures the node again.
  const updateInternals = useUpdateNodeInternals();
  useEffect(() => { updateInternals(id); }, [showHandles, connecting, id, updateInternals]);
  const edit = (
    <textarea
      className="fs-pz-edit nodrag nowheel nopan" autoFocus defaultValue={text} spellCheck={false} aria-label={t('Text of the item')}
      onFocus={(e) => e.currentTarget.select()}
      onBlur={(e) => ctx.stopEdit(id, e.currentTarget.value)}
      onKeyDown={(e) => {
        e.stopPropagation();
        if (e.key === 'Escape') ctx.stopEdit(id);
        else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) ctx.stopEdit(id, e.currentTarget.value);
      }}
    />
  );
  let body: React.ReactNode;
  if (far && !editing && !selected) {
    return <div className="fs-pz-item fs-pz-far" data-kind={kind} style={style}><Anchor /></div>;
  }
  if (kind === 'frame') {
    body = <div className="fs-pz-frame-title">{editing ? edit : <span>{text || t('Frame')}</span>}</div>;
  } else if (kind === 'stroke') {
    const pts = jc.fx?.pts ?? [];
    body = (
      <svg className="fs-pz-stroke" viewBox={`0 0 ${Math.max(1, jc.width)} ${Math.max(1, jc.height)}`} preserveAspectRatio="none" aria-hidden="true">
        <path d={strokePath(pts)} fill="none" strokeWidth={jc.fx?.sw ?? 3} strokeLinecap="round" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
      </svg>
    );
  } else if (kind === 'image') {
    body = <img className="fs-pz-image" src={boardAssetUrl(ctx.boardId, jc.file ?? '')} alt={t('Image on the board')} draggable={false} />;
  } else if (kind === 'link') {
    body = editing ? edit : <a className="fs-pz-link nodrag" href={jc.url} target="_blank" rel="noreferrer noopener">{jc.url}</a>;
  } else {
    body = (
      <>
        {kind === 'diamond' && (
          <svg className="fs-pz-bgshape" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true"><polygon points="50,1 99,50 50,99 1,50" vectorEffect="non-scaling-stroke" /></svg>
        )}
        <div className="fs-pz-text">{editing ? edit : text}</div>
      </>
    );
  }
  return (
    <div className="fs-pz-item" data-kind={kind} data-selected={selected || undefined} data-fresh={data.fresh || undefined} data-editing={editing || undefined} style={style} onPointerEnter={() => setHot(true)} onPointerLeave={() => setHot(false)}>
      {selected && ctx.interactive && !editing && (
        <NodeResizer
          isVisible minWidth={kind === 'stroke' ? 8 : 40} minHeight={kind === 'stroke' ? 8 : 24}
          keepAspectRatio={kind === 'image'} lineClassName="fs-pz-resize-line" handleClassName="fs-pz-resize-handle"
          onResizeEnd={(_, p) => ctx.resized(id, p)}
        />
      )}
      <Anchor />
      {showHandles && (
        <>
          <Handle id="t" type="source" position={Position.Top} className="fs-pz-handle" />
          <Handle id="r" type="source" position={Position.Right} className="fs-pz-handle" />
          <Handle id="b" type="source" position={Position.Bottom} className="fs-pz-handle" />
          <Handle id="l" type="source" position={Position.Left} className="fs-pz-handle" />
          {connecting && <Handle id="d" type="target" position={Position.Top} isConnectableStart={false} className="fs-pz-drop" />}
        </>
      )}
      {body}
    </div>
  );
}, (a, b) => a.data === b.data && a.selected === b.selected);

/* ── a connector: it leaves the border of each end towards the other, so it stays attached and reroutes ── */

function ConnEdge({ id, source, target, data, selected, markerEnd, markerStart }: EdgeProps<PzEdge>) {
  const ctx = useContext(PzContext)!;
  const s = useInternalNode(source);
  const tg = useInternalNode(target);
  if (!s || !tg || !data) return null;
  const rs = { x: s.internals.positionAbsolute.x, y: s.internals.positionAbsolute.y, width: s.measured.width ?? 0, height: s.measured.height ?? 0 };
  const rt = { x: tg.internals.positionAbsolute.x, y: tg.internals.positionAbsolute.y, width: tg.measured.width ?? 0, height: tg.measured.height ?? 0 };
  const p1 = borderPoint(rs, rt.x + rt.width / 2, rt.y + rt.height / 2, kindOf((s.data as PzData).jc));
  const p2 = borderPoint(rt, rs.x + rs.width / 2, rs.y + rs.height / 2, kindOf((tg.data as PzData).jc));
  const jc = data.jc;
  const color = colorHex(jc.color);
  const mx = (p1.x + p2.x) / 2, my = (p1.y + p2.y) / 2;
  const editing = ctx.editingEdge === id;
  return (
    <>
      <BaseEdge id={id} path={`M ${p1.x} ${p1.y} L ${p2.x} ${p2.y}`} markerEnd={markerEnd} markerStart={markerStart} interactionWidth={22}
        style={{ stroke: color ?? 'var(--pz-ink)', strokeWidth: selected ? 3 : 2 }} />
      {(jc.label || editing) && (
        <EdgeLabelRenderer>
          <div className="fs-pz-elabel nodrag nopan" style={{ transform: `translate(-50%, -50%) translate(${mx}px, ${my}px)` }} data-selected={selected || undefined}>
            {editing ? (
              <input
                autoFocus defaultValue={jc.label ?? ''} aria-label={t('Connector label')} className="fs-pz-elabel-input"
                onBlur={(e) => ctx.stopEdgeEdit(id, e.currentTarget.value)}
                onKeyDown={(e) => { e.stopPropagation(); if (e.key === 'Enter') ctx.stopEdgeEdit(id, e.currentTarget.value); else if (e.key === 'Escape') ctx.stopEdgeEdit(id); }}
              />
            ) : jc.label}
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  );
}

const nodeTypes = { item: ItemNode };
const edgeTypes = { conn: ConnEdge };

/* ── doc <-> React Flow ── */

const rfNode = (jc: JcNode, selected: boolean, fresh: boolean, index: number): PzNode => ({
  id: jc.id, type: 'item', position: { x: jc.x, y: jc.y }, data: { jc, fresh }, width: jc.width, height: jc.height, selected,
  zIndex: kindOf(jc) === 'frame' ? 0 : 1 + (index % 1000),
});
function rfEdge(jc: JcEdge, selected: boolean): PzEdge {
  const color = colorHex(jc.color);
  const end = (kind?: string) => (kind === 'none' ? undefined : { type: MarkerType.ArrowClosed, color: color ?? 'var(--pz-ink)', width: 16, height: 16 });
  return { id: jc.id, source: jc.fromNode, target: jc.toNode, type: 'conn', data: { jc }, selected, markerEnd: end(jc.toEnd ?? 'arrow'), markerStart: jc.fromEnd === 'arrow' ? end('arrow') : undefined };
}

function themeNow(): 'light' | 'dark' {
  const explicit = document.documentElement.getAttribute('data-theme');
  if (explicit === 'light' || explicit === 'dark') return explicit;
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

interface Cursor { actor: SurfaceActor; x: number; y: number; at: number; state: string }

/** The named cursors, kept apart so a pan or zoom redraws only these and not the whole board. */
function Cursors({ cursors }: { cursors: Record<string, Cursor> }) {
  const zoom = useStore((s) => s.transform[2]);
  return (
    <>
      {Object.values(cursors).map((c) => (
        <div key={c.actor.id} className="fs-pz-cursor" data-state={c.state} data-testid="pizarra-agent-cursor" style={{ transform: `translate(${c.x}px, ${c.y}px) scale(${1 / Math.max(0.25, zoom)})`, ['--c' as string]: c.actor.color } as React.CSSProperties}>
          <svg width="18" height="20" viewBox="0 0 18 20" aria-hidden="true"><path d="M1 1l14 7-6 2-2 7z" fill="var(--c)" stroke="#fff" strokeWidth="1.2" strokeLinejoin="round" /></svg>
          <span>{c.actor.name}</span>
        </div>
      ))}
    </>
  );
}
export interface PizarraProps {
  boardId: string; title: string; sessionId: string | null; hidden?: boolean; maximized?: boolean; onToggleMaximize?: () => void;
  onNotice: (text: string, tone?: 'info' | 'warning' | 'danger') => void; onTitle: (title: string) => void;
}

const TOOL_DEFS: { id: Tool; icon: typeof Square; label: string; key: string }[] = [
  { id: 'select', icon: MousePointer2, label: 'Select', key: 'V' }, { id: 'hand', icon: Hand, label: 'Move the view', key: 'H' },
  { id: 'sticky', icon: StickyNote, label: 'Sticky note', key: 'S' }, { id: 'text', icon: Type, label: 'Text', key: 'T' },
  { id: 'rect', icon: Square, label: 'Rectangle', key: 'R' }, { id: 'ellipse', icon: Circle, label: 'Ellipse', key: 'O' },
  { id: 'diamond', icon: Diamond, label: 'Diamond', key: 'D' }, { id: 'frame', icon: FrameIcon, label: 'Frame', key: 'F' },
  { id: 'pen', icon: Pencil, label: 'Pen', key: 'P' },
];

function Board({ boardId, title, sessionId, hidden, maximized, onToggleMaximize, onNotice, onTitle }: PizarraProps) {
  const rf = useReactFlow<PzNode, PzEdge>();
  const wrapRef = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const importRef = useRef<HTMLInputElement>(null);
  const docRef = useRef<Doc>({ nodes: [], edges: [] });
  const [load, setLoad] = useState<'loading' | 'ready' | 'error'>('loading');
  const [loadError, setLoadError] = useState('');
  const [nodes, setNodes] = useState<PzNode[]>([]);
  const [edges, setEdges] = useState<PzEdge[]>([]);
  const nodesRef = useRef<PzNode[]>([]);
  nodesRef.current = nodes;
  const [tool, setTool] = useState<Tool>('select');
  const [editing, setEditing] = useState<string | null>(null);
  const [editingEdge, setEditingEdge] = useState<string | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const selectedRef = useRef<string[]>([]);
  selectedRef.current = selected;
  const [guides, setGuides] = useState<Guide[]>([]);
  const [draft, setDraft] = useState<{ kind: Tool; a: { x: number; y: number }; b: { x: number; y: number }; pts?: number[][] } | null>(null);
  const [cursors, setCursors] = useState<Record<string, Cursor>>({});
  const [save, setSave] = useState<'saved' | 'saving' | 'error'>('saved');
  const [tick, setTick] = useState(0);
  const [titleText, setTitleText] = useState(title);
  const [theme, setTheme] = useState<'light' | 'dark'>(themeNow);
  const [penColor, setPenColor] = useState<string>('');
  const [showMap, setShowMap] = useState(true);
  const undoRef = useRef<{ before: Patch; after: Patch }[]>([]);
  const redoRef = useRef<{ before: Patch; after: Patch }[]>([]);
  const pending = useRef<Patch | null>(null);
  const sendTimer = useRef<number | undefined>(undefined);
  const busyIds = useRef<Set<string>>(new Set());
  const fresh = useRef<Set<string>>(new Set());
  const frameDrag = useRef<{ id: string; start: { x: number; y: number }; members: Map<string, { x: number; y: number }> } | null>(null);
  const dragStart = useRef<Map<string, { x: number; y: number }>>(new Map());
  const clipboard = useRef<{ nodes: JcNode[]; edges: JcEdge[] } | null>(null);
  const bump = () => setTick((n) => n + 1);

  useEffect(() => { setTitleText(title); }, [title]);
  useEffect(() => {
    const observer = new MutationObserver(() => setTheme(themeNow()));
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'class'] });
    return () => observer.disconnect();
  }, []);

  /* the React Flow items for a doc, reusing every object that did not change */
  const rebuild = useCallback((doc: Doc, select?: Set<string>) => {
    setNodes((prev) => {
      const sel = select ?? new Set(prev.filter((n) => n.selected).map((n) => n.id));
      const old = new Map(prev.map((n) => [n.id, n]));
      return doc.nodes.map((jc, i) => {
        const was = old.get(jc.id);
        const isSel = sel.has(jc.id);
        const isFresh = fresh.current.has(jc.id);
        if (was && was.data.jc === jc && was.selected === isSel && was.data.fresh === isFresh && was.position.x === jc.x && was.position.y === jc.y && was.width === jc.width && was.height === jc.height) return was;
        return rfNode(jc, isSel, isFresh, i);
      });
    });
    setEdges((prev) => {
      const old = new Map(prev.map((e) => [e.id, e]));
      const ids = select ? null : new Set(prev.filter((e) => e.selected).map((e) => e.id));
      return doc.edges.map((jc) => {
        const was = old.get(jc.id);
        const sel = ids ? ids.has(jc.id) : false;
        if (was && was.data?.jc === jc && was.selected === sel) return was;
        return rfEdge(jc, sel);
      });
    });
  }, []);

  /* sending: the person's changes go out merged, a moment after the last one */
  const flush = useCallback(async () => {
    const body = pending.current;
    pending.current = null;
    window.clearTimeout(sendTimer.current);
    if (!body || !patchSize(body)) return;
    setSave('saving');
    try {
      await patchBoard(boardId, CLIENT_ID, body);
      if (!pending.current) setSave('saved');
    } catch {
      pending.current = pending.current ? mergePatches(body, pending.current) : body;
      setSave('error');
      sendTimer.current = window.setTimeout(() => void flush(), 4000);
    }
  }, [boardId]);
  const queue = useCallback((p: Patch) => {
    pending.current = pending.current ? mergePatches(pending.current, p) : p;
    setSave('saving');
    window.clearTimeout(sendTimer.current);
    sendTimer.current = window.setTimeout(() => void flush(), 160);
  }, [flush]);
  useEffect(() => () => { void flush(); }, [flush]);

  /* the one way the person changes the board */
  const commit = useCallback((next: Doc, o: { select?: string[]; record?: boolean; send?: boolean } = {}) => {
    const prev = docRef.current;
    const d = diffDocs(prev, next);
    docRef.current = next;
    for (const id of Object.keys(d?.after.nodes ?? {})) if (!prev.nodes.some((n) => n.id === id)) fresh.current.add(id);
    rebuild(next, o.select ? new Set(o.select) : undefined);
    if (o.select) setSelected(o.select);
    if (fresh.current.size) window.setTimeout(() => { fresh.current.clear(); rebuild(docRef.current); }, 700);
    if (!d) return;
    if (o.record !== false) { undoRef.current.push(d); if (undoRef.current.length > MAX_UNDO) undoRef.current.shift(); redoRef.current = []; }
    if (o.send !== false) queue(d.after);
    bump();
  }, [queue, rebuild]);

  /* first load */
  useEffect(() => {
    let live = true;
    setLoad('loading');
    getBoard(boardId)
      .then((r) => {
        if (!live) return;
        docRef.current = r.doc;
        undoRef.current = []; redoRef.current = [];
        rebuild(r.doc, new Set());
        setLoad('ready');
        window.setTimeout(() => { if (r.doc.nodes.length) void rf.fitView({ padding: 0.2, duration: 0, maxZoom: 1 }); }, 60);
      })
      .catch((e: Error) => { if (live) { setLoadError(e.message); setLoad('error'); } });
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [boardId]);

  /* what the server says: the agent's edits, its cursor, a new title */
  useEffect(() => onSurfaceEvent((ev) => {
    const d = ev.data;
    if (d.kind !== 'pizarra' || d.id !== boardId) return;
    if (ev.event === 'patch') {
      if (d.origin === CLIENT_ID) return;
      const patch = d.patch as Patch;
      const prev = docRef.current;
      const next = applyPatch(prev, patch, busyIds.current);
      const diff = diffDocs(prev, next);
      if (!diff) return;
      for (const id of Object.keys(diff.after.nodes ?? {})) if (!prev.nodes.some((n) => n.id === id)) fresh.current.add(id);
      docRef.current = next;
      rebuild(next);
      if (d.origin === 'agent' || d.origin === 'import' || d.origin === 'restore') {
        undoRef.current.push(diff); if (undoRef.current.length > MAX_UNDO) undoRef.current.shift(); redoRef.current = [];
      }
      window.setTimeout(() => { fresh.current.clear(); rebuild(docRef.current); }, 900);
      // What the agent adds outside the view: bring the view to it (the person's own pan stays theirs).
      if (d.origin === 'agent') {
        const added = next.nodes.filter((n) => Object.prototype.hasOwnProperty.call(diff.after.nodes ?? {}, n.id) && !prev.nodes.some((p) => p.id === n.id));
        const box = bounds(added);
        const el = wrapRef.current;
        if (box && el && el.clientWidth > 0) {
          const vp = rf.getViewport();
          const vx = -vp.x / vp.zoom, vy = -vp.y / vp.zoom, vw = el.clientWidth / vp.zoom, vh = el.clientHeight / vp.zoom;
          const inside = box.x >= vx && box.y >= vy && box.x + box.width <= vx + vw && box.y + box.height <= vy + vh;
          if (!inside) window.setTimeout(() => void rf.fitView({ padding: 0.15, duration: 450, maxZoom: 1 }), 80);
        }
      }
      bump();
    } else if (ev.event === 'cursor' && d.actor) {
      const actor = d.actor as SurfaceActor;
      setCursors((c) => {
        if (d.state === 'idle' || d.state === 'gone') { const n = { ...c }; if (n[actor.id]) n[actor.id] = { ...n[actor.id], state: 'idle', at: Date.now() }; return n; }
        return { ...c, [actor.id]: { actor, x: Number(d.x) || 0, y: Number(d.y) || 0, at: Date.now(), state: 'working' } };
      });
    } else if (ev.event === 'meta' && d.title) {
      setTitleText(String(d.title)); onTitle(String(d.title));
    }
  }), [boardId, rebuild, onTitle]);
  useEffect(() => {
    const timer = window.setInterval(() => setCursors((c) => {
      const now = Date.now();
      const keep = Object.fromEntries(Object.entries(c).filter(([, v]) => now - v.at < (v.state === 'idle' ? 2200 : 8000)));
      return Object.keys(keep).length === Object.keys(c).length ? c : keep;
    }), 1000);
    return () => window.clearInterval(timer);
  }, []);

  /* what the person has on screen, for the agent */
  const reportState = useCallback(() => {
    if (!sessionId || hidden) return;
    const el = wrapRef.current;
    const vp = rf.getViewport();
    void boardState(boardId, { session: sessionId, selection: selectedRef.current, viewport: { x: vp.x, y: vp.y, zoom: vp.zoom, w: el?.clientWidth ?? 0, h: el?.clientHeight ?? 0 } });
  }, [boardId, sessionId, hidden, rf]);
  useEffect(() => { const timer = window.setTimeout(reportState, 400); return () => window.clearTimeout(timer); }, [selected, reportState, load]);
  useEffect(() => {
    if (!sessionId) return;
    if (hidden) { void boardState(boardId, { session: sessionId, open: false }); return; }
    reportState();
    const timer = window.setInterval(reportState, 30000);
    return () => { window.clearInterval(timer); void boardState(boardId, { session: sessionId, open: false }); };
  }, [boardId, sessionId, hidden, reportState]);

  /* ── editing helpers ── */
  const selectedNodes = useMemo(() => docRef.current.nodes.filter((n) => selected.includes(n.id)), [selected, tick]); // eslint-disable-line react-hooks/exhaustive-deps
  const selectedEdges = edges.filter((e) => e.selected);
  const withNodes = (fn: (n: JcNode) => JcNode, ids = selectedRef.current): Doc => ({ ...docRef.current, nodes: docRef.current.nodes.map((n) => (ids.includes(n.id) ? fn(n) : n)) });

  const syncPositions = useCallback((): Doc => {
    const pos = new Map(nodesRef.current.map((n) => [n.id, n]));
    let changed = false;
    const list = docRef.current.nodes.map((n) => {
      const r = pos.get(n.id);
      if (!r) return n;
      const x = Math.round(r.position.x), y = Math.round(r.position.y);
      if (x === n.x && y === n.y) return n;
      changed = true;
      return { ...n, x, y };
    });
    return changed ? { ...docRef.current, nodes: list } : docRef.current;
  }, []);

  const place = useCallback((jc: JcNode, edit = false) => {
    const doc = docRef.current;
    const nextNodes = kindOf(jc) === 'frame' ? [jc, ...doc.nodes] : [...doc.nodes, jc];
    commit({ ...doc, nodes: nextNodes }, { select: [jc.id] });
    if (edit) window.setTimeout(() => setEditing(jc.id), 30);
  }, [commit]);

  const center = useCallback((w: number, h: number) => {
    const el = wrapRef.current;
    const p = rf.screenToFlowPosition({ x: (el?.getBoundingClientRect().left ?? 0) + (el?.clientWidth ?? 600) / 2, y: (el?.getBoundingClientRect().top ?? 0) + (el?.clientHeight ?? 400) / 2 });
    return { x: p.x - w / 2, y: p.y - h / 2 };
  }, [rf]);

  const stopEdit = useCallback((id: string, text?: string) => {
    setEditing(null);
    const n = docRef.current.nodes.find((x) => x.id === id);
    if (!n || text === undefined || text === textOf(n)) return;
    commit({ ...docRef.current, nodes: docRef.current.nodes.map((x) => (x.id === id ? withText(x, text) : x)) });
    wrapRef.current?.focus();
  }, [commit]);
  const stopEdgeEdit = useCallback((id: string, label?: string) => {
    setEditingEdge(null);
    if (label === undefined) return;
    commit({ ...docRef.current, edges: docRef.current.edges.map((e) => (e.id === id ? (label ? { ...e, label } : (({ label: _l, ...rest }) => rest)(e)) : e)) });
  }, [commit]);
  const resized = useCallback((id: string, r: { x: number; y: number; width: number; height: number }) => {
    commit({ ...docRef.current, nodes: docRef.current.nodes.map((n) => (n.id === id ? { ...n, x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height) } : n)) });
  }, [commit]);

  const removeSelected = useCallback(() => {
    const ids = new Set(selectedRef.current);
    const eids = new Set(edges.filter((e) => e.selected).map((e) => e.id));
    if (!ids.size && !eids.size) return;
    const doc = docRef.current;
    commit({ nodes: doc.nodes.filter((n) => !ids.has(n.id)), edges: doc.edges.filter((e) => !eids.has(e.id) && !ids.has(e.fromNode) && !ids.has(e.toNode)) }, { select: [] });
  }, [commit, edges]);

  const duplicate = useCallback(() => {
    const ids = new Set(selectedRef.current);
    if (!ids.size) return;
    const c = duplicateItems(docRef.current, ids);
    commit({ nodes: [...docRef.current.nodes, ...c.nodes], edges: [...docRef.current.edges, ...c.edges] }, { select: c.ids });
  }, [commit]);

  const groupSelected = useCallback(() => {
    const items = docRef.current.nodes.filter((n) => selectedRef.current.includes(n.id) && kindOf(n) !== 'frame');
    const frame = frameAround(items);
    if (!frame) return;
    commit({ ...docRef.current, nodes: [frame, ...docRef.current.nodes] }, { select: [frame.id] });
    window.setTimeout(() => setEditing(frame.id), 40);
  }, [commit]);
  const ungroupSelected = useCallback(() => {
    const frames = new Set(docRef.current.nodes.filter((n) => selectedRef.current.includes(n.id) && kindOf(n) === 'frame').map((n) => n.id));
    if (!frames.size) return;
    const held = docRef.current.nodes.filter((n) => frames.has(n.id)).flatMap((f) => framesHolding(docRef.current, f));
    commit({ nodes: docRef.current.nodes.filter((n) => !frames.has(n.id)), edges: docRef.current.edges }, { select: held });
  }, [commit]);

  const align = useCallback((how: AlignHow) => {
    const items = docRef.current.nodes.filter((n) => selectedRef.current.includes(n.id)).map((n) => ({ id: n.id, x: n.x, y: n.y, width: n.width, height: n.height }));
    const moves = alignRects(items, how);
    if (!Object.keys(moves).length) return;
    commit({ ...docRef.current, nodes: docRef.current.nodes.map((n) => (moves[n.id] ? { ...n, ...moves[n.id] } : n)) });
  }, [commit]);

  const reorder = useCallback((to: 'front' | 'back') => {
    const ids = new Set(selectedRef.current);
    const doc = docRef.current;
    const mine = doc.nodes.filter((n) => ids.has(n.id)), rest = doc.nodes.filter((n) => !ids.has(n.id));
    commit({ ...doc, nodes: to === 'front' ? [...rest, ...mine] : [...mine, ...rest] });
  }, [commit]);

  const recolor = useCallback((color: string) => {
    const ids = selectedRef.current;
    const eids = edges.filter((e) => e.selected).map((e) => e.id);
    const paint = <T extends { color?: string }>(x: T): T => (color ? { ...x, color } : (({ color: _c, ...rest }) => rest as unknown as T)(x));
    commit({ nodes: docRef.current.nodes.map((n) => (ids.includes(n.id) ? paint(n) : n)), edges: docRef.current.edges.map((e) => (eids.includes(e.id) ? paint(e) : e)) });
    setPenColor(color);
  }, [commit, edges]);

  const nudge = useCallback((dx: number, dy: number) => {
    if (!selectedRef.current.length) return;
    commit(withNodes((n) => ({ ...n, x: n.x + dx, y: n.y + dy })));
  }, [commit]); // eslint-disable-line react-hooks/exhaustive-deps

  const history = useCallback((dir: 'undo' | 'redo') => {
    const from = dir === 'undo' ? undoRef.current : redoRef.current;
    const entry = from.pop();
    if (!entry) return;
    const patch = dir === 'undo' ? entry.before : entry.after;
    const next = applyPatch(docRef.current, patch);
    (dir === 'undo' ? redoRef.current : undoRef.current).push(entry);
    const ids = Object.keys(patch.nodes ?? {}).filter((id) => next.nodes.some((n) => n.id === id));
    commit(next, { record: false, select: ids });
  }, [commit]);

  /* ── images ── */
  const addImage = useCallback(async (blob: Blob, at?: { x: number; y: number }) => {
    try {
      const file = await uploadBoardAsset(boardId, blob);
      const dims = await new Promise<{ w: number; h: number }>((resolve) => {
        const img = new Image();
        const url = URL.createObjectURL(blob);
        img.onload = () => { resolve({ w: img.naturalWidth || 320, h: img.naturalHeight || 240 }); URL.revokeObjectURL(url); };
        img.onerror = () => { resolve({ w: 320, h: 240 }); URL.revokeObjectURL(url); };
        img.src = url;
      });
      const scale = Math.min(1, 480 / Math.max(dims.w, dims.h));
      const w = Math.round(dims.w * scale), h = Math.round(dims.h * scale);
      const pos = at ?? center(w, h);
      place(makeNode('image', pos.x, pos.y, { w, h, file }));
    } catch (e) {
      onNotice(`${t('Could not add the image.')} ${(e as Error).message}`, 'danger');
    }
  }, [boardId, center, onNotice, place]);

  /* ── the canvas ── */
  const onNodesChange = useCallback((changes: NodeChange<PzNode>[]) => {
    setNodes((prev) => applyNodeChanges(changes, prev));
    const sel = changes.filter((c) => c.type === 'select');
    if (sel.length) {
      setSelected((cur) => {
        const set = new Set(cur);
        for (const c of sel) if (c.type === 'select') { if (c.selected) set.add(c.id); else set.delete(c.id); }
        return [...set].filter((id) => docRef.current.nodes.some((n) => n.id === id));
      });
    }
  }, []);

  const onConnect = useCallback((c: Connection) => {
    if (!c.source || !c.target || c.source === c.target) return;
    if (docRef.current.edges.some((e) => e.fromNode === c.source && e.toNode === c.target)) return;
    const edge: JcEdge = { id: newId(), fromNode: c.source, toNode: c.target, toEnd: 'arrow' };
    commit({ ...docRef.current, edges: [...docRef.current.edges, edge] });
  }, [commit]);

  const onDragStart = useCallback((_: unknown, node: PzNode) => {
    busyIds.current.add(node.id);
    dragStart.current = new Map(nodesRef.current.map((n) => [n.id, n.position]));
    const jc = node.data.jc;
    if (kindOf(jc) === 'frame') {
      const members = framesHolding(docRef.current, jc).filter((id) => !selectedRef.current.includes(id) || id === node.id);
      frameDrag.current = { id: node.id, start: { ...node.position }, members: new Map(members.map((id) => [id, { ...(dragStart.current.get(id) ?? { x: 0, y: 0 }) }])) };
      members.forEach((id) => busyIds.current.add(id));
    }
  }, []);
  const onDrag = useCallback((_: unknown, node: PzNode, dragged: PzNode[]) => {
    const fd = frameDrag.current;
    if (fd && fd.id === node.id) {
      const dx = node.position.x - fd.start.x, dy = node.position.y - fd.start.y;
      setNodes((prev) => prev.map((n) => { const m = fd.members.get(n.id); return m && n.id !== node.id ? { ...n, position: { x: m.x + dx, y: m.y + dy } } : n; }));
    }
    const moving = bounds(dragged.map((n) => ({ x: n.position.x, y: n.position.y, width: n.width ?? 0, height: n.height ?? 0 })));
    if (!moving) return;
    const skip = new Set(dragged.map((n) => n.id));
    const others = nodesRef.current.filter((n) => !skip.has(n.id) && kindOf(n.data.jc) !== 'frame' && kindOf(n.data.jc) !== 'stroke').map((n) => ({ x: n.position.x, y: n.position.y, width: n.width ?? 0, height: n.height ?? 0 }));
    setGuides(snapBox(moving, others, 6 / Math.max(0.2, rf.getZoom())).guides);
  }, [rf]);
  const onDragStop = useCallback((_: unknown, node: PzNode, dragged: PzNode[]) => {
    setGuides([]);
    const skip = new Set(dragged.map((n) => n.id));
    const moving = bounds(dragged.map((n) => ({ x: n.position.x, y: n.position.y, width: n.width ?? 0, height: n.height ?? 0 })));
    let sx = 0, sy = 0;
    if (moving && !frameDrag.current) {
      const others = nodesRef.current.filter((n) => !skip.has(n.id) && kindOf(n.data.jc) !== 'frame' && kindOf(n.data.jc) !== 'stroke').map((n) => ({ x: n.position.x, y: n.position.y, width: n.width ?? 0, height: n.height ?? 0 }));
      const s = snapBox(moving, others, 6 / Math.max(0.2, rf.getZoom()));
      sx = s.dx; sy = s.dy;
    }
    const pos = new Map(nodesRef.current.map((n) => [n.id, n.position]));
    const doc = docRef.current;
    const list = doc.nodes.map((n) => {
      const p = pos.get(n.id);
      if (!p) return n;
      const moved = dragged.some((d) => d.id === n.id);
      const x = Math.round(p.x + (moved ? sx : 0)), y = Math.round(p.y + (moved ? sy : 0));
      return x === n.x && y === n.y ? n : { ...n, x, y };
    });
    busyIds.current.clear();
    frameDrag.current = null;
    commit({ ...doc, nodes: list });
    void node;
  }, [commit, rf]);

  /* ── creating: a click places the default size, a drag sizes it, the pen draws ── */
  const startDraw = useCallback((e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0 || load !== 'ready') return;
    if (tool !== 'pen' && !CREATE_TOOLS.includes(tool)) return;
    if ((e.target as HTMLElement).closest('.fs-pz-bar, .fs-pz-sel, .react-flow__minimap, .react-flow__controls')) return;
    e.preventDefault();
    e.stopPropagation();
    wrapRef.current?.setPointerCapture(e.pointerId);
    const p = rf.screenToFlowPosition({ x: e.clientX, y: e.clientY });
    setDraft({ kind: tool, a: p, b: p, pts: tool === 'pen' ? [[p.x, p.y]] : undefined });
  }, [load, rf, tool]);
  const moveDraw = useCallback((e: ReactPointerEvent<HTMLDivElement>) => {
    if (!draft) return;
    const p = rf.screenToFlowPosition({ x: e.clientX, y: e.clientY });
    setDraft({ ...draft, b: p, pts: draft.pts ? [...draft.pts, [p.x, p.y]] : undefined });
  }, [draft, rf]);
  const endDraw = useCallback((e: ReactPointerEvent<HTMLDivElement>) => {
    if (!draft) return;
    wrapRef.current?.releasePointerCapture(e.pointerId);
    const d = draft;
    setDraft(null);
    if (d.kind === 'pen') {
      const node = strokeNode(d.pts ?? [], penColor || undefined);
      if (node) place(node);
      return;
    }
    const kind = d.kind as Kind;
    const dx = Math.abs(d.b.x - d.a.x), dy = Math.abs(d.b.y - d.a.y);
    const click = dx < 6 && dy < 6;
    const node = click
      ? makeNode(kind, d.a.x - 100, d.a.y - 40, { color: kind === 'sticky' && penColor ? penColor : undefined })
      : makeNode(kind, Math.min(d.a.x, d.b.x), Math.min(d.a.y, d.b.y), { w: dx, h: dy });
    setTool('select');
    place(node, kind !== 'frame' || true);
  }, [draft, penColor, place]);

  const onDoubleClick = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    if (tool !== 'select' || load !== 'ready') return;
    const target = e.target as HTMLElement;
    if (!target.classList.contains('react-flow__pane')) return;
    const p = rf.screenToFlowPosition({ x: e.clientX, y: e.clientY });
    place(makeNode('sticky', p.x - 100, p.y - 70), true);
  }, [load, place, rf, tool]);

  /* ── clipboard, drop, keys ── */
  const onCopy = useCallback((e: ReactClipboardEvent, cut = false) => {
    const target = e.target as HTMLElement;
    if (target.tagName === 'TEXTAREA' || target.tagName === 'INPUT') return;
    const ids = new Set(selectedRef.current);
    if (!ids.size) return;
    const doc = docRef.current;
    clipboard.current = { nodes: doc.nodes.filter((n) => ids.has(n.id)), edges: doc.edges.filter((x) => ids.has(x.fromNode) && ids.has(x.toNode)) };
    e.clipboardData.setData('text/plain', clipboard.current.nodes.map(textOf).filter(Boolean).join('\n'));
    e.clipboardData.setData('application/x-faustus-pizarra', JSON.stringify(clipboard.current));
    e.preventDefault();
    if (cut) removeSelected();
  }, [removeSelected]);
  const onPaste = useCallback((e: ReactClipboardEvent) => {
    const target = e.target as HTMLElement;
    if (target.tagName === 'TEXTAREA' || target.tagName === 'INPUT') return;
    const image = [...e.clipboardData.files].find((f) => f.type.startsWith('image/'));
    if (image) { e.preventDefault(); void addImage(image); return; }
    let items = clipboard.current;
    const raw = e.clipboardData.getData('application/x-faustus-pizarra');
    if (raw) { try { items = JSON.parse(raw) as { nodes: JcNode[]; edges: JcEdge[] }; } catch { /* use the in-memory copy */ } }
    if (items?.nodes.length) {
      e.preventDefault();
      const c = duplicateItems({ nodes: items.nodes, edges: items.edges }, new Set(items.nodes.map((n) => n.id)), 28, 28);
      clipboard.current = { nodes: c.nodes, edges: c.edges };
      commit({ nodes: [...docRef.current.nodes, ...c.nodes], edges: [...docRef.current.edges, ...c.edges] }, { select: c.ids });
      return;
    }
    const text = e.clipboardData.getData('text/plain').trim();
    if (text) {
      e.preventDefault();
      const kind: Kind = /^https?:\/\/\S+$/.test(text) ? 'link' : 'sticky';
      const pos = center(200, 140);
      place(makeNode(kind, pos.x, pos.y, { text: text.slice(0, 4000) }));
    }
  }, [addImage, center, commit, place]);
  const onDrop = useCallback((e: ReactDragEvent) => {
    const files = [...e.dataTransfer.files];
    const image = files.find((f) => f.type.startsWith('image/'));
    if (image) { e.preventDefault(); void addImage(image, rf.screenToFlowPosition({ x: e.clientX, y: e.clientY })); return; }
    const canvas = files.find((f) => /\.canvas$/i.test(f.name));
    if (canvas) { e.preventDefault(); void doImport(canvas); }
  }, [addImage, rf]); // eslint-disable-line react-hooks/exhaustive-deps

  const onKeyDown = useCallback((e: ReactKeyboardEvent<HTMLDivElement>) => {
    const el = e.target as HTMLElement;
    if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT' || el.isContentEditable) return;
    const mod = e.ctrlKey || e.metaKey;
    const k = e.key.toLowerCase();
    if (mod && k === 'z') { e.preventDefault(); history(e.shiftKey ? 'redo' : 'undo'); return; }
    if (mod && k === 'y') { e.preventDefault(); history('redo'); return; }
    if (mod && k === 'd') { e.preventDefault(); duplicate(); return; }
    if (mod && k === 'g') { e.preventDefault(); if (e.shiftKey) ungroupSelected(); else groupSelected(); return; }
    if (mod && k === 'a') { e.preventDefault(); const all = docRef.current.nodes.map((n) => n.id); setSelected(all); rebuild(docRef.current, new Set(all)); return; }
    if (mod) return;
    if (e.key === 'Delete' || e.key === 'Backspace') { e.preventDefault(); removeSelected(); return; }
    if (e.key === 'Escape') { setTool('select'); setSelected([]); rebuild(docRef.current, new Set()); return; }
    if (e.key === 'Enter' && selectedRef.current.length === 1) { e.preventDefault(); setEditing(selectedRef.current[0]); return; }
    if (e.key.startsWith('Arrow')) {
      e.preventDefault();
      const s = e.shiftKey ? 10 : 1;
      nudge(e.key === 'ArrowLeft' ? -s : e.key === 'ArrowRight' ? s : 0, e.key === 'ArrowUp' ? -s : e.key === 'ArrowDown' ? s : 0);
      return;
    }
    if (e.key === '[') { reorder('back'); return; }
    if (e.key === ']') { reorder('front'); return; }
    if (e.key === '0' || (e.key === '1' && e.shiftKey)) { void rf.fitView({ padding: 0.2, duration: 200 }); return; }
    if (e.key === '+' || e.key === '=') { void rf.zoomIn({ duration: 120 }); return; }
    if (e.key === '-') { void rf.zoomOut({ duration: 120 }); return; }
    if (SHORTCUT[k]) setTool(SHORTCUT[k]);
  }, [duplicate, groupSelected, history, nudge, removeSelected, rebuild, reorder, rf, ungroupSelected]);

  /* ── export / import ── */
  const exportPng = useCallback(async () => {
    try {
      const svg = await (await fetch(boardSvgUrl(boardId, theme === 'dark'), { credentials: 'same-origin' })).text();
      const img = new Image();
      const url = URL.createObjectURL(new Blob([svg], { type: 'image/svg+xml' }));
      await new Promise<void>((resolve, reject) => { img.onload = () => resolve(); img.onerror = () => reject(new Error('svg')); img.src = url; });
      const scale = Math.min(2, 8000 / Math.max(img.width, img.height, 1));
      const canvas = document.createElement('canvas');
      canvas.width = Math.max(1, Math.round(img.width * scale)); canvas.height = Math.max(1, Math.round(img.height * scale));
      canvas.getContext('2d')!.drawImage(img, 0, 0, canvas.width, canvas.height);
      URL.revokeObjectURL(url);
      canvas.toBlob((blob) => {
        if (!blob) return;
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob); a.download = `${titleText || 'board'}.png`; a.click();
        window.setTimeout(() => URL.revokeObjectURL(a.href), 2000);
      }, 'image/png');
    } catch { onNotice(t('Could not export the image.'), 'danger'); }
  }, [boardId, onNotice, theme, titleText]);
  const doImport = async (file: File) => {
    try {
      await flush();
      await importCanvas(boardId, await file.text());
      const r = await getBoard(boardId);
      docRef.current = r.doc; undoRef.current = []; redoRef.current = [];
      rebuild(r.doc, new Set()); bump();
      window.setTimeout(() => void rf.fitView({ padding: 0.2, duration: 200, maxZoom: 1 }), 60);
      onNotice(t('Imported {n} items.', { n: r.doc.nodes.length }));
    } catch (e) { onNotice(`${t('Could not import the file.')} ${(e as Error).message}`, 'danger'); }
  };
  const download = (href: string, name: string) => { const a = document.createElement('a'); a.href = href; a.download = name; a.click(); };

  const empty = load === 'ready' && nodes.length === 0;
  const canUndo = undoRef.current.length > 0, canRedo = redoRef.current.length > 0;
  const multi = selected.length > 1;
  const hasFrame = selectedNodes.some((n) => kindOf(n) === 'frame');
  const ctxValue: Ctx = useMemo(() => ({ editing, editingEdge, boardId, interactive: tool === 'select', stopEdit, stopEdgeEdit, resized }), [editing, editingEdge, boardId, tool, stopEdit, stopEdgeEdit, resized]);
  const sel0 = selectedNodes[0];
  const selBox = useMemo(() => {
    const b = bounds(selectedNodes);
    return b;
  }, [selectedNodes]);

  return (
    <PzContext.Provider value={ctxValue}>
      <div className="fs-pz" hidden={hidden} data-testid="pizarra" data-tool={tool} data-theme-mode={theme} data-load={load}>
        <div className="fs-pz-head">
          <input
            className="fs-pz-title" value={titleText} aria-label={t('Whiteboard title')} onChange={(e) => setTitleText(e.target.value)}
            onBlur={() => { const v = titleText.trim(); if (v && v !== title) void renameBoard(boardId, v).then((m) => onTitle(m.title)).catch(() => onNotice(t('Could not rename.'), 'danger')); }}
            onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); }}
          />
          <span className="fs-pz-status" data-state={save} role="status" data-testid="pizarra-status">
            {save === 'saved' ? t('Saved') : save === 'saving' ? t('Saving…') : t('Not saved yet')}
          </span>
          <span className="fs-pz-grow" />
          <IconButton icon={Undo2} label={t('Undo')} size="sm" disabled={!canUndo} onClick={() => history('undo')} testId="pizarra-undo" />
          <IconButton icon={Redo2} label={t('Redo')} size="sm" disabled={!canRedo} onClick={() => history('redo')} testId="pizarra-redo" />
          <IconButton icon={Maximize} label={t('Fit everything in view (0)')} size="sm" onClick={() => void rf.fitView({ padding: 0.2, duration: 200 })} testId="pizarra-fit" />
          <Menu align="end" testId="pizarra-export" trigger={<IconButton icon={Download} label={t('Export')} size="sm" />} items={[
            { label: t('PNG image'), onSelect: () => void exportPng() },
            { label: t('SVG image'), onSelect: () => download(boardSvgUrl(boardId, theme === 'dark'), `${titleText || 'board'}.svg`) },
            { label: t('JSON Canvas file (.canvas)'), onSelect: () => download(boardCanvasUrl(boardId), `${titleText || 'board'}.canvas`) },
          ]} />
          <IconButton icon={Upload} label={t('Import a .canvas file')} size="sm" onClick={() => importRef.current?.click()} testId="pizarra-import" />
          <input ref={importRef} type="file" accept=".canvas,application/json" hidden onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ''; if (f) void doImport(f); }} />
          {onToggleMaximize && (
            <span className="fs-pz-max">
              <IconButton icon={maximized ? Minimize2 : Maximize2} label={maximized ? t('Bring the chat back (Esc)') : t('Make the whiteboard fill the window')} size="sm" onClick={onToggleMaximize} testId="pizarra-maximize" />
            </span>
          )}
        </div>
        <div
          ref={wrapRef} className="fs-pz-stage" tabIndex={0} onKeyDown={onKeyDown} onCopy={onCopy} onCut={(e) => onCopy(e, true)} onPaste={onPaste}
          onPointerDownCapture={startDraw} onPointerMove={moveDraw} onPointerUp={endDraw} onDoubleClick={onDoubleClick}
          onDragOver={(e) => { if (e.dataTransfer.types.includes('Files')) e.preventDefault(); }} onDrop={onDrop}
        >
          {load === 'loading' && <div className="fs-pz-state" role="status"><span className="fs-pz-spin" aria-hidden="true" />{t('Loading the whiteboard')}</div>}
          {load === 'error' && <div className="fs-pz-state" role="alert">{t('Could not open the whiteboard.')} {loadError}</div>}
          {load === 'ready' && (
            <ReactFlow<PzNode, PzEdge>
              nodes={nodes} edges={edges} nodeTypes={nodeTypes} edgeTypes={edgeTypes} colorMode={theme}
              onNodesChange={onNodesChange} onConnect={onConnect} connectionMode={ConnectionMode.Loose}
              onNodeDragStart={onDragStart} onNodeDrag={onDrag} onNodeDragStop={onDragStop}
              onNodeDoubleClick={(_, n) => { if (tool === 'select') setEditing(n.id); }}
              onEdgeDoubleClick={(_, e) => setEditingEdge(e.id)}
              onEdgeClick={(_, e) => setEdges((prev) => prev.map((x) => ({ ...x, selected: x.id === e.id })))}
              onPaneClick={() => { setEdges((prev) => (prev.some((x) => x.selected) ? prev.map((x) => ({ ...x, selected: false })) : prev)); wrapRef.current?.focus(); }}
              onMoveEnd={reportState}
              minZoom={0.05} maxZoom={4} onlyRenderVisibleElements
              panOnScroll panOnDrag={tool === 'hand' ? true : [1, 2]} panActivationKeyCode="Space" zoomOnScroll={false} zoomOnPinch zoomOnDoubleClick={false}
              selectionOnDrag={tool === 'select'} selectionMode={SelectionMode.Partial} multiSelectionKeyCode="Shift" deleteKeyCode={null}
              nodesDraggable={tool === 'select'} nodesConnectable={tool === 'select'} elementsSelectable={tool === 'select'} nodesFocusable={false} edgesFocusable={false}
              proOptions={{ hideAttribution: true }} defaultViewport={{ x: 0, y: 0, zoom: 1 }}
            >
              <Background variant={BackgroundVariant.Dots} gap={24} size={1.4} />
              {showMap && nodes.length > 0 && (
                <MiniMap pannable zoomable ariaLabel={t('Minimap')} nodeStrokeWidth={2} maskColor="color-mix(in srgb, var(--fs-text-3, #888) 22%, transparent)"
                  nodeColor={(n) => colorHex((n.data as PzData).jc.color) ?? 'var(--fs-text-3, #888)'} />
              )}
              <ViewportPortal>
                {guides.map((g, i) => (
                  <div key={i} className="fs-pz-guide" style={g.axis === 'x' ? { left: g.at, top: g.from, width: 0, height: g.to - g.from } : { left: g.from, top: g.at, width: g.to - g.from, height: 0 }} />
                ))}
                {draft && draft.kind !== 'pen' && (
                  <div className="fs-pz-draft" data-kind={draft.kind} style={{ left: Math.min(draft.a.x, draft.b.x), top: Math.min(draft.a.y, draft.b.y), width: Math.abs(draft.b.x - draft.a.x), height: Math.abs(draft.b.y - draft.a.y) }} />
                )}
                {draft?.pts && (
                  <svg className="fs-pz-penlive" style={{ position: 'absolute', left: 0, top: 0, overflow: 'visible', pointerEvents: 'none' }} width="1" height="1">
                    <path d={strokePath(draft.pts)} fill="none" stroke={colorHex(penColor) ?? 'var(--pz-ink)'} strokeWidth={3} strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                )}
                <Cursors cursors={cursors} />
              </ViewportPortal>
            </ReactFlow>
          )}
          {empty && !draft && (
            <div className="fs-pz-empty" data-testid="pizarra-empty">
              <p><strong>{t('The board is empty.')}</strong></p>
              <p>{t('Double-click to add a note, pick a tool below, drop an image, or ask Faustus to build something here.')}</p>
              <Button size="sm" variant="primary" icon={StickyNote} label={t('Add a note')} onClick={() => { const p = center(200, 140); place(makeNode('sticky', p.x, p.y), true); }} testId="pizarra-first-note" />
            </div>
          )}
          {load === 'ready' && (
            <>
              <div className="fs-pz-bar" role="toolbar" aria-label={t('Whiteboard tools')}>
                {TOOL_DEFS.map((d) => (
                  <button key={d.id} type="button" className="fs-pz-tool" aria-pressed={tool === d.id} data-testid={'pizarra-tool-' + d.id} title={`${t(d.label)} (${d.key})`} aria-label={t(d.label)} onClick={() => setTool(d.id)}>
                    <d.icon size={17} aria-hidden="true" />
                  </button>
                ))}
                <span className="fs-pz-sep" />
                <button type="button" className="fs-pz-tool" title={t('Add an image')} aria-label={t('Add an image')} onClick={() => fileRef.current?.click()}><ImagePlus size={17} aria-hidden="true" /></button>
                <input ref={fileRef} type="file" accept="image/*" hidden onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ''; if (f) void addImage(f); }} />
              </div>
              {(selected.length > 0 || selectedEdges.length > 0 || tool === 'pen') && (
                <div className="fs-pz-sel" role="toolbar" aria-label={t('Selection')} data-testid="pizarra-selection-bar">
                  <span className="fs-pz-swatches">
                    {PALETTE.map((c) => (
                      <button key={c.id || 'none'} type="button" className="fs-pz-swatch" aria-label={t(c.label)} title={t(c.label)} style={{ ['--sw' as string]: colorHex(c.id) ?? 'transparent' } as React.CSSProperties} data-none={!c.id || undefined} data-on={(sel0?.color ?? '') === c.id && selected.length > 0 || undefined} onClick={() => recolor(c.id)} />
                    ))}
                  </span>
                  {selected.length > 0 && (
                    <>
                      <span className="fs-pz-sep" />
                      {multi && (
                        <>
                          <IconButton icon={AlignStartVertical} label={t('Align left')} size="sm" onClick={() => align('left')} />
                          <IconButton icon={AlignCenterVertical} label={t('Align centres')} size="sm" onClick={() => align('center')} />
                          <IconButton icon={AlignEndVertical} label={t('Align right')} size="sm" onClick={() => align('right')} />
                          <IconButton icon={AlignStartHorizontal} label={t('Align top')} size="sm" onClick={() => align('top')} />
                          <IconButton icon={AlignCenterHorizontal} label={t('Align middles')} size="sm" onClick={() => align('middle')} />
                          <IconButton icon={AlignEndHorizontal} label={t('Align bottom')} size="sm" onClick={() => align('bottom')} />
                          {selected.length > 2 && (
                            <>
                              <IconButton icon={AlignHorizontalSpaceBetween} label={t('Distribute horizontally')} size="sm" onClick={() => align('distribute-h')} />
                              <IconButton icon={AlignVerticalSpaceBetween} label={t('Distribute vertically')} size="sm" onClick={() => align('distribute-v')} />
                            </>
                          )}
                          <span className="fs-pz-sep" />
                        </>
                      )}
                      <IconButton icon={hasFrame ? Ungroup : Group} label={hasFrame ? t('Remove the frame (keeps its content)') : t('Group in a frame')} size="sm" onClick={hasFrame ? ungroupSelected : groupSelected} testId="pizarra-group" />
                      <IconButton icon={Copy} label={t('Duplicate')} size="sm" onClick={duplicate} testId="pizarra-duplicate" />
                      <IconButton icon={BringToFront} label={t('Bring to front (])')} size="sm" onClick={() => reorder('front')} />
                      <IconButton icon={SendToBack} label={t('Send to back ([)')} size="sm" onClick={() => reorder('back')} />
                    </>
                  )}
                  {(selected.length > 0 || selectedEdges.length > 0) && <IconButton icon={Trash2} label={t('Delete')} size="sm" onClick={removeSelected} testId="pizarra-delete" />}
                </div>
              )}
              {selBox && selected.length === 1 && false && <span className="fs-pz-hidden" />}
            </>
          )}
        </div>
      </div>
    </PzContext.Provider>
  );
}

export default function Pizarra(props: PizarraProps) {
  return (
    <ReactFlowProvider>
      <Board {...props} />
    </ReactFlowProvider>
  );
}
