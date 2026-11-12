import {
  useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState,
  type ClipboardEvent as RClipboardEvent, type KeyboardEvent as RKeyboardEvent, type PointerEvent as RPointerEvent,
} from 'react';
import {
  AlignCenter, AlignLeft, AlignRight, ArrowDownAZ, ArrowUpZA, Baseline, Bold, ChartColumn, ChartLine, ChartPie, ChartScatter, Download, Highlighter, Italic,
  ListFilter, Maximize2, Minimize2, PaintBucket, Percent, Plus, Redo2, Snowflake, Sigma, Undo2, Upload, WrapText, X,
} from 'lucide-react';
import { IconButton, Menu } from '../../components';
import {
  CLIENT_ID, editSheet, getSheet, importSheet, onSurfaceEvent, redoSheet, renameSheet, sheetCsvUrl, sheetState, sheetWindow, sheetXlsxUrl, undoSheet,
  type HojaChanges, type HojaTab, type SurfaceActor,
} from '../../adapters/surfaces';
import {
  DEFAULT_COL_W, DEFAULT_ROW_H, MAX_COLS, MAX_ROWS, addr, clamp, colLetters, inSel, makeAxis, normSel, parseAddr, parseTsv, planFill, rangeText, summarise,
  tableFromHtml, toHtmlTable, toTsv,
  type Cell, type Sel, type Style,
} from '../../lib/hojaModel';
import HojaChart from './HojaChart';
import { t } from '../../i18n';
import './hoja.css';

/**
 * Hoja: a spreadsheet in the side panel. The workbook is an .xlsx on the server with a JSON working model
 * (src/hoja.py); this screen shows it through a virtual grid (only the cells in view are asked for and drawn, so a
 * hundred thousand rows scroll like ten) and shows what the agent does to it as it happens: cells fill in and a
 * named cursor moves. Formulas, formats and recalculation are the server's; the grid never computes a value.
 */

const ROW_HEAD_W = 48;
const COL_HEAD_H = 24;
const BR = 100;            // cells are fetched in blocks of 100 rows x 20 columns
const BC = 20;

interface Props {
  sheetId: string; title: string; sessionId: string | null; hidden?: boolean; maximized?: boolean; onToggleMaximize?: () => void;
  onNotice: (text: string, tone?: 'info' | 'warning' | 'danger') => void; onTitle: (title: string) => void;
}
type Whole = 'row' | 'col' | null;
interface Editing { r: number; c: number; text: string; src: 'cell' | 'bar' }
interface Cursor { actor: SurfaceActor; tab: string; r: number; c: number; at: number; state: 'working' | 'idle' }
interface Ctx { x: number; y: number; kind: 'row' | 'col' | 'tab'; index: number }
interface Prompt { title: string; label: string; initial: string; onSubmit: (value: string) => void }

const key = (r: number, c: number) => r * 20000 + c;

function Tb({ pressed, ...rest }: { pressed?: boolean } & React.ComponentProps<typeof IconButton>) {
  return <span className="fs-hj-tb" data-on={pressed || undefined}><IconButton {...rest} /></span>;
}

function cssFor(st: Style | undefined, ov: Record<string, unknown> | null): React.CSSProperties {
  const o: React.CSSProperties = {};
  if (st?.b || ov?.b) o.fontWeight = 700;
  if (st?.i) o.fontStyle = 'italic';
  const color = (ov?.c as string | undefined) ?? st?.c;
  if (color) o.color = color;
  const bg = (ov?.bg as string | undefined) ?? st?.bg;
  if (bg) o.background = bg;
  if (st?.wrap) o.whiteSpace = 'normal';
  return o;
}

function decimalsOf(nf: string | undefined): number {
  const m = /\.(0+)/.exec((nf ?? '').split(';')[0]);
  return m ? m[1].length : 0;
}
function withDecimals(nf: string | undefined, delta: number): string {
  const base = (nf ?? '').split(';')[0] || '0';
  const d = clamp(decimalsOf(base) + delta, 0, 10);
  const head = base.replace(/\.0+/, '');
  const [intPart, suffix] = /(%|\s?\[.*\])$/.test(head) ? [head.replace(/(%|\s?\[.*\])$/, ''), (/(%|\s?\[.*\])$/.exec(head) as RegExpExecArray)[0]] : [head, ''];
  return intPart + (d ? '.' + '0'.repeat(d) : '') + suffix;
}

export default function Hoja({ sheetId, title, sessionId, hidden, maximized, onToggleMaximize, onNotice, onTitle }: Props) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const [load, setLoad] = useState<'loading' | 'ready' | 'error'>('loading');
  const [loadError, setLoadError] = useState('');
  const [tabs, setTabs] = useState<HojaTab[]>([]);
  const [tabId, setTabId] = useState('');
  const tabIdRef = useRef('');
  tabIdRef.current = tabId;
  const tab = useMemo(() => tabs.find((x) => x.id === tabId) ?? null, [tabs, tabId]);

  /* the cells in view live in a ref (they change on every scroll); `tick` redraws */
  const cells = useRef<Map<string, Map<number, Cell>>>(new Map());
  const styles = useRef<Map<number, Style>>(new Map());
  const blocks = useRef<Map<string, 'loading' | 'done'>>(new Map());
  const fresh = useRef<Map<number, number>>(new Map());
  const [tick, setTick] = useState(0);
  const [fetchTick, setFetchTick] = useState(0);
  const bump = () => setTick((n) => n + 1);

  const [anchor, setAnchor] = useState({ r: 1, c: 1 });
  const [focus, setFocus] = useState({ r: 1, c: 1 });
  const [whole, setWhole] = useState<Whole>(null);
  const sel = useMemo<Sel>(() => normSel({ r0: anchor.r, c0: anchor.c, r1: focus.r, c1: focus.c }), [anchor, focus]);
  const selRef = useRef(sel);
  selRef.current = sel;
  const [editing, setEditing] = useState<Editing | null>(null);
  const editingRef = useRef<Editing | null>(null);
  editingRef.current = editing;
  const [view, setView] = useState({ left: 0, top: 0, w: 800, h: 500 });
  const [extent, setExtent] = useState({ rows: 100, cols: 26 });
  const [liveSize, setLiveSize] = useState<{ axis: 'col' | 'row'; index: number; size: number } | null>(null);
  const [fillTo, setFillTo] = useState<Sel | null>(null);
  const [cursors, setCursors] = useState<Record<string, Cursor>>({});
  const [pulse, setPulse] = useState<Record<string, number>>({});
  const [save, setSave] = useState<'saved' | 'saving' | 'error'>('saved');
  const [titleText, setTitleText] = useState(title);
  const [ctx, setCtx] = useState<Ctx | null>(null);
  const [prompt, setPrompt] = useState<Prompt | null>(null);
  const [renamingTab, setRenamingTab] = useState<string | null>(null);
  const clip = useRef<{ tab: string; sel: Sel; cut: boolean; text: string } | null>(null);
  const queue = useRef<Promise<unknown>>(Promise.resolve());
  const drag = useRef<{ mode: 'select' | 'fill' | 'col' | 'row'; start?: { r: number; c: number }; index?: number; origin?: number; size?: number } | null>(null);

  useEffect(() => { setTitleText(title); }, [title]);

  /* ── geometry ── */
  const rowAxis = useMemo(() => makeAxis(extent.rows, DEFAULT_ROW_H, {
    ...(tab?.rows ?? {}), ...(liveSize?.axis === 'row' ? { [liveSize.index]: liveSize.size } : {}),
  }, tab?.hidden ?? []), [extent.rows, tab, liveSize]);
  const colAxis = useMemo(() => makeAxis(extent.cols, DEFAULT_COL_W, {
    ...(tab?.cols ?? {}), ...(liveSize?.axis === 'col' ? { [liveSize.index]: liveSize.size } : {}),
  }), [extent.cols, tab, liveSize]);
  const fr = tab?.freeze.rows ?? 0;
  const fc = tab?.freeze.cols ?? 0;
  const frozenH = fr ? rowAxis.start(fr + 1) : 0;
  const frozenW = fc ? colAxis.start(fc + 1) : 0;

  useEffect(() => {      // the used range sets the minimum extent; moving or scrolling past the end grows it
    if (!tab) return;
    setExtent((e) => ({ rows: Math.max(e.rows, tab.used[0] + 60, 100), cols: Math.max(e.cols, tab.used[1] + 8, 26) }));
  }, [tab]);

  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const measure = () => setView((v) => (v.w === el.clientWidth && v.h === el.clientHeight ? v : { ...v, w: el.clientWidth, h: el.clientHeight }));
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [load, hidden]);

  const r0v = clamp(rowAxis.at(view.top + frozenH) - 2, 1, extent.rows), r1v = clamp(rowAxis.at(view.top + view.h) + 2, 1, extent.rows);
  const c0v = clamp(colAxis.at(view.left + frozenW) - 1, 1, extent.cols), c1v = clamp(colAxis.at(view.left + view.w) + 1, 1, extent.cols);

  useEffect(() => {
    if (r1v > extent.rows - 20 && extent.rows < MAX_ROWS) setExtent((e) => ({ ...e, rows: Math.min(MAX_ROWS, e.rows + 100) }));
    if (c1v > extent.cols - 4 && extent.cols < MAX_COLS) setExtent((e) => ({ ...e, cols: Math.min(MAX_COLS, e.cols + 10) }));
  }, [r1v, c1v, extent.rows, extent.cols]);

  /* ── loading ── */
  const reloadTabs = useCallback(async () => {
    const r = await getSheet(sheetId);
    setTabs(r.tabs);
    setTabId((cur) => (r.tabs.some((x) => x.id === cur) ? cur : (r.tabs[0]?.id ?? '')));
    return r;
  }, [sheetId]);
  useEffect(() => {
    let live = true;
    setLoad('loading');
    getSheet(sheetId).then((r) => {
      if (!live) return;
      setTabs(r.tabs); setTabId(r.tabs[0]?.id ?? ''); onTitle(r.meta.title); setTitleText(r.meta.title); setLoad('ready');
    }).catch((e: Error) => { if (live) { setLoadError(e.message); setLoad('error'); } });
    return () => { live = false; };
  }, [sheetId]); // eslint-disable-line react-hooks/exhaustive-deps

  const tabCells = (id: string) => { let m = cells.current.get(id); if (!m) { m = new Map(); cells.current.set(id, m); } return m; };
  useEffect(() => {
    if (load !== 'ready' || !tabId || hidden) return;
    const need = new Set<string>();
    const add = (ra: number, rb: number, ca: number, cb: number) => {
      for (let br = Math.floor((ra - 1) / BR); br <= Math.floor((rb - 1) / BR); br += 1) for (let bc = Math.floor((ca - 1) / BC); bc <= Math.floor((cb - 1) / BC); bc += 1) need.add(`${br}:${bc}`);
    };
    add(r0v, r1v, c0v, c1v);
    if (fr) add(1, fr, c0v, c1v);
    if (fc) add(r0v, r1v, 1, fc);
    if (fr && fc) add(1, fr, 1, fc);
    for (const bk of need) {
      const full = `${tabId}:${bk}`;
      if (blocks.current.has(full)) continue;
      blocks.current.set(full, 'loading');
      const [br, bc] = bk.split(':').map(Number);
      const ra = br * BR + 1, ca = bc * BC + 1;
      sheetWindow(sheetId, tabId, ra, ca, ra + BR - 1, ca + BC - 1).then((w) => {
        const m = tabCells(w.tab);
        for (let r = ra; r < ra + BR; r += 1) for (let c = ca; c < ca + BC; c += 1) m.delete(key(r, c));
        for (const row of w.cells) m.set(key(row[0], row[1]), { r: row[0], c: row[1], v: row[2], shown: row[3], formula: row[4], sid: row[5], ov: row[6] });
        for (const [id, st] of Object.entries(w.styles)) styles.current.set(Number(id), st);
        blocks.current.set(`${w.tab}:${bk}`, 'done');
        bump();
      }).catch(() => { blocks.current.delete(full); });
    }
  }, [load, tabId, hidden, r0v, r1v, c0v, c1v, fr, fc, fetchTick, sheetId]);

  const stale = useCallback((ch: HojaChanges, origin: string) => {
    for (const [tid, info] of Object.entries(ch.tabs ?? {})) {
      if (info.full || ch.struct) {
        for (const k of [...blocks.current.keys()]) if (k.startsWith(tid + ':')) blocks.current.delete(k);
      } else {
        for (const [r, c] of info.cells ?? []) {
          blocks.current.delete(`${tid}:${Math.floor((r - 1) / BR)}:${Math.floor((c - 1) / BC)}`);
          if (origin === 'agent') fresh.current.set(key(r, c), Date.now());
        }
      }
      if (origin === 'agent' && tid !== tabIdRef.current) {
        setPulse((p) => ({ ...p, [tid]: Date.now() }));
      }
    }
    if (ch.struct) void reloadTabs().catch(() => undefined);
    setFetchTick((n) => n + 1);
    bump();
    if (origin === 'agent') window.setTimeout(bump, 1000);
  }, [reloadTabs]);

  /* what the agent and other windows do */
  useEffect(() => onSurfaceEvent((ev) => {
    const d = ev.data;
    if (d.kind !== 'hoja' || d.id !== sheetId) return;
    if (ev.event === 'cells') {
      if (d.origin === CLIENT_ID) return;
      stale(d as unknown as HojaChanges, String(d.origin ?? ''));
    } else if (ev.event === 'cursor' && d.actor) {
      const actor = d.actor as SurfaceActor;
      setCursors((cur) => {
        if (d.state === 'idle' || d.state === 'gone') { const n = { ...cur }; if (n[actor.id]) n[actor.id] = { ...n[actor.id], state: 'idle', at: Date.now() }; return n; }
        return { ...cur, [actor.id]: { actor, tab: String(d.tab ?? ''), r: Number(d.r) || 1, c: Number(d.c) || 1, at: Date.now(), state: 'working' } };
      });
      // keep the agent's work in sight: bring the view to a cell outside it (the person's own scroll stays theirs)
      if (d.tab === tabIdRef.current) {
        const el = scrollRef.current;
        if (el) {
          const r = Number(d.r) || 1, c = Number(d.c) || 1;
          const top = rowAxis.start(r), left = colAxis.start(c);
          if (top < el.scrollTop + frozenH || top + 24 > el.scrollTop + el.clientHeight) el.scrollTo({ top: Math.max(0, top - el.clientHeight / 3), behavior: 'smooth' });
          if (left < el.scrollLeft + frozenW || left + 96 > el.scrollLeft + el.clientWidth) el.scrollTo({ left: Math.max(0, left - el.clientWidth / 3), behavior: 'smooth' });
        }
      }
    } else if (ev.event === 'meta' && d.title) {
      setTitleText(String(d.title)); onTitle(String(d.title));
    }
  }), [sheetId, stale, rowAxis, colAxis, frozenH, frozenW, onTitle]);
  useEffect(() => {
    const timer = window.setInterval(() => {
      setCursors((cur) => {
        const now = Date.now();
        const keep = Object.fromEntries(Object.entries(cur).filter(([, v]) => now - v.at < (v.state === 'idle' ? 2200 : 8000)));
        return Object.keys(keep).length === Object.keys(cur).length ? cur : keep;
      });
      setPulse((p) => { const now = Date.now(); const keep = Object.fromEntries(Object.entries(p).filter(([, v]) => now - v < 4000)); return Object.keys(keep).length === Object.keys(p).length ? p : keep; });
    }, 1000);
    return () => window.clearInterval(timer);
  }, []);

  /* what the person has on screen, for the agent */
  const reportState = useCallback(() => {
    if (!sessionId || hidden || !tabIdRef.current) return;
    void sheetState(sheetId, { session: sessionId, tab: tabIdRef.current, sel: selRef.current });
  }, [sheetId, sessionId, hidden]);
  useEffect(() => { const timer = window.setTimeout(reportState, 400); return () => window.clearTimeout(timer); }, [sel, tabId, reportState, load]);
  useEffect(() => {
    if (!sessionId) return;
    if (hidden) { void sheetState(sheetId, { session: sessionId, open: false }); return; }
    reportState();
    const timer = window.setInterval(reportState, 30000);
    return () => { window.clearInterval(timer); void sheetState(sheetId, { session: sessionId, open: false }); };
  }, [sheetId, sessionId, hidden, reportState]);

  /* ── reading the cache ── */
  void tick;
  const cellAt = (r: number, c: number): Cell | undefined => cells.current.get(tabId)?.get(key(r, c));
  const styleOf = (cell: Cell | undefined): Style | undefined => (cell && cell.sid ? styles.current.get(cell.sid) : undefined);
  const rawOf = (cell: Cell | undefined): string => {
    if (!cell) return '';
    if (cell.formula) return cell.formula;
    const nf = styleOf(cell)?.nf ?? '';
    if (/[ymd]/i.test(nf) && !/General/i.test(nf)) return cell.shown;
    return cell.v == null ? '' : String(cell.v);
  };

  /* ── sending edits ── */
  const run = useCallback((ops: unknown[], after?: () => void): Promise<void> => {
    setSave('saving');
    const job = queue.current.then(async () => {
      try {
        const res = await editSheet(sheetId, ops, tabIdRef.current);
        stale(res, CLIENT_ID);
        if (res.error) onNotice(res.error.message, 'danger');
        setSave(res.error ? 'error' : 'saved');
        after?.();
      } catch (e) {
        setSave('error');
        onNotice((e as Error).message, 'danger');
        setFetchTick((n) => n + 1);
      }
    });
    queue.current = job;
    return job;
  }, [sheetId, stale, onNotice]);

  const rangeArg = (s: Sel = sel, w: Whole = whole): string => {
    if (w === 'col') return `${colLetters(s.c0)}:${colLetters(s.c1)}`;
    if (w === 'row') return `${s.r0}:${s.r1}`;
    return rangeText(s);
  };

  const putLocal = (r: number, c: number, text: string) => {
    const m = tabCells(tabId);
    const old = m.get(key(r, c));
    if (text === '') { m.delete(key(r, c)); } else m.set(key(r, c), { r, c, v: text, shown: text, formula: text.startsWith('=') ? text : null, sid: old?.sid ?? 0, ov: null });
    bump();
  };

  const moveTo = useCallback((r: number, c: number, extend = false) => {
    const nr = clamp(r, 1, MAX_ROWS), nc = clamp(c, 1, MAX_COLS);
    setFocus({ r: nr, c: nc });
    if (!extend) setAnchor({ r: nr, c: nc });
    setWhole(null);
    setExtent((e) => ({ rows: Math.max(e.rows, nr + 30), cols: Math.max(e.cols, nc + 6) }));
    const el = scrollRef.current;
    if (el) {      // keep the cell on screen
      const top = rowAxis.start(nr), bottom = top + rowAxis.size(nr), left = colAxis.start(nc), right = left + colAxis.size(nc);
      if (nr > fr && top < el.scrollTop + frozenH) el.scrollTop = top - frozenH;
      else if (nr > fr && bottom > el.scrollTop + el.clientHeight) el.scrollTop = bottom - el.clientHeight + 16;
      if (nc > fc && left < el.scrollLeft + frozenW) el.scrollLeft = left - frozenW;
      else if (nc > fc && right > el.scrollLeft + el.clientWidth) el.scrollLeft = right - el.clientWidth + 16;
    }
  }, [rowAxis, colAxis, fr, fc, frozenH, frozenW]);

  const startEdit = (r: number, c: number, text?: string, src: 'cell' | 'bar' = 'cell') => {
    setEditing({ r, c, text: text ?? rawOf(cellAt(r, c)), src });
  };
  const commit = (move: 'down' | 'right' | 'up' | 'left' | 'none') => {
    const ed = editingRef.current;
    if (!ed) return;
    editingRef.current = null;
    setEditing(null);
    const before = rawOf(cellAt(ed.r, ed.c));
    if (ed.text !== before) { putLocal(ed.r, ed.c, ed.text); void run([{ op: 'set', at: addr(ed.r, ed.c), values: [[ed.text]] }]); }
    const d = { down: [1, 0], up: [-1, 0], right: [0, 1], left: [0, -1], none: [0, 0] }[move];
    if (move !== 'none') moveTo(Math.max(1, ed.r + d[0]), Math.max(1, ed.c + d[1]));
    window.setTimeout(() => scrollRef.current?.focus(), 0);
  };
  const cancelEdit = () => { editingRef.current = null; setEditing(null); window.setTimeout(() => scrollRef.current?.focus(), 0); };

  /* ── pointer: select, resize, fill ── */
  const hit = (e: { clientX: number; clientY: number }): { r: number; c: number } => {
    const el = scrollRef.current as HTMLDivElement;
    const box = el.getBoundingClientRect();
    const vx = e.clientX - box.left, vy = e.clientY - box.top;
    const x = fc && vx < frozenW ? vx : vx + el.scrollLeft;
    const y = fr && vy < frozenH ? vy : vy + el.scrollTop;
    return { r: rowAxis.at(y), c: colAxis.at(x) };
  };
  const onPointerDown = (e: RPointerEvent<HTMLDivElement>) => {
    const target = e.target as HTMLElement;
    if (target.closest('.fs-hj-edit, .fs-hj-chart, .fs-hj-menu')) return;
    if (e.button === 2) { const p = hit(e); if (!inSel(sel, p.r, p.c)) moveTo(p.r, p.c); return; }
    if (e.button !== 0) return;
    const el = e.currentTarget;
    if (target.closest('[data-handle]')) {
      drag.current = { mode: 'fill' };
      el.setPointerCapture(e.pointerId);
      e.preventDefault();
      return;
    }
    const p = hit(e);
    // while a formula is being typed, a click on a cell inserts its address
    const ed = editingRef.current;
    if (ed && ed.text.startsWith('=') && /[=(,;+\-*/^&<>:]$/.test(ed.text)) {
      setEditing({ ...ed, text: ed.text + addr(p.r, p.c) });
      e.preventDefault();
      return;
    }
    if (ed) commit('none');
    drag.current = { mode: 'select', start: p };
    if (e.shiftKey) moveTo(p.r, p.c, true); else moveTo(p.r, p.c);
    el.setPointerCapture(e.pointerId);
    el.focus({ preventScroll: true });
  };
  const onPointerMove = (e: RPointerEvent<HTMLDivElement>) => {
    const d = drag.current;
    if (!d) return;
    const el = e.currentTarget;
    const box = el.getBoundingClientRect();
    if (e.clientX > box.right - 24) el.scrollLeft += 24; else if (e.clientX < box.left + 24) el.scrollLeft -= 24;
    if (e.clientY > box.bottom - 24) el.scrollTop += 24; else if (e.clientY < box.top + 24) el.scrollTop -= 24;
    const p = hit(e);
    if (d.mode === 'select') { setFocus((f) => (f.r === p.r && f.c === p.c ? f : p)); }
    else if (d.mode === 'fill') {
      const s = selRef.current;
      const down = Math.max(p.r - s.r1, 0) >= Math.max(p.c - s.c1, 0);
      setFillTo(down ? { ...s, r1: Math.max(s.r1, p.r) } : { ...s, c1: Math.max(s.c1, p.c) });
    }
  };
  const onPointerUp = (e: RPointerEvent<HTMLDivElement>) => {
    const d = drag.current;
    drag.current = null;
    try { e.currentTarget.releasePointerCapture(e.pointerId); } catch { /* not captured */ }
    if (d?.mode === 'fill' && fillTo) {
      const to = fillTo;
      setFillTo(null);
      const plan = planFill(sel, to, (r, c) => cellAt(r, c)?.v);
      if (plan.length) {
        void run(plan.map((p) => ({ op: 'fill', range: p.range, from: p.from, ...(p.series ? { series: true, step: p.step } : {}) })));
        setAnchor({ r: sel.r0, c: sel.c0 }); setFocus({ r: to.r1, c: to.c1 });
      }
    }
  };

  /* column / row headers */
  const startResize = (e: RPointerEvent<HTMLElement>, axis: 'col' | 'row', index: number) => {
    e.stopPropagation(); e.preventDefault();
    const size = axis === 'col' ? colAxis.size(index) : rowAxis.size(index);
    const x0 = axis === 'col' ? e.clientX : e.clientY;
    const target = e.currentTarget;
    target.setPointerCapture(e.pointerId);
    drag.current = { mode: axis, index, origin: x0, size };
    const move = (ev: PointerEvent) => {
      const dx = (axis === 'col' ? ev.clientX : ev.clientY) - x0;
      setLiveSize({ axis, index, size: Math.max(axis === 'col' ? 24 : 14, size + dx) });
    };
    const up = (ev: PointerEvent) => {
      target.removeEventListener('pointermove', move); target.removeEventListener('pointerup', up);
      drag.current = null;
      const next = Math.max(axis === 'col' ? 24 : 14, size + ((axis === 'col' ? ev.clientX : ev.clientY) - x0));
      setLiveSize(null);
      if (Math.abs(next - size) >= 2) void run([axis === 'col' ? { op: 'width', cols: { [colLetters(index)]: Math.round(next) } } : { op: 'height', rows: { [index]: Math.round(next) } }]);
    };
    target.addEventListener('pointermove', move); target.addEventListener('pointerup', up);
  };
  const pickHeader = (axis: 'col' | 'row', index: number, extend: boolean) => {
    if (axis === 'col') { setAnchor((a) => (extend ? { r: 1, c: a.c } : { r: 1, c: index })); setFocus({ r: extent.rows, c: index }); setWhole('col'); }
    else { setAnchor((a) => (extend ? { r: a.r, c: 1 } : { r: index, c: 1 })); setFocus({ r: index, c: extent.cols }); setWhole('row'); }
    scrollRef.current?.focus({ preventScroll: true });
  };

  /* ── formatting helpers ── */
  const active = cellAt(anchor.r, anchor.c);
  const activeStyle = styleOf(active);
  const fmt = (patch: Record<string, unknown>) => void run([{ op: 'format', range: rangeArg(), ...patch }]);
  const toggle = (k: 'b' | 'i' | 'wrap', name: string) => fmt({ [name]: !activeStyle?.[k] });
  const usedRange = (): string => (tab ? `A1:${addr(Math.max(tab.used[0], 1), Math.max(tab.used[1], 1))}` : 'A1');
  const hasHeader = (): boolean => {
    const m = cells.current.get(tabId);
    if (!m || !tab) return false;
    let any = false;
    for (let c = 1; c <= tab.used[1]; c += 1) { const v = m.get(key(1, c))?.v; if (v != null) { any = true; if (typeof v !== 'string') return false; } }
    return any;
  };
  const sortBy = (desc: boolean) => {
    const single = sel.r0 === sel.r1 && sel.c0 === sel.c1 || whole === 'col';
    const range = single ? usedRange() : rangeText(sel);
    void run([{ op: 'sort', range, by: colLetters(anchor.c), desc, header: single ? hasHeader() : false }]);
  };
  const chartOf = (type: string) => {
    const single = sel.r0 === sel.r1 && sel.c0 === sel.c1;
    void run([{ op: 'chart', type, range: single ? usedRange() : rangeText(sel), at: addr(1, (tab?.used[1] ?? 1) + 2) }]);
  };

  /* ── clipboard ── */
  const cellsText = (s: Sel) => {
    const rows: string[][] = [], sty: (Style | undefined)[][] = [];
    for (let r = s.r0; r <= s.r1; r += 1) {
      const row: string[] = [], srow: (Style | undefined)[] = [];
      for (let c = s.c0; c <= s.c1; c += 1) { const cell = cellAt(r, c); row.push(cell ? cell.shown : ''); srow.push(styleOf(cell)); }
      rows.push(row); sty.push(srow);
    }
    return { rows, sty };
  };
  const onCopy = (e: RClipboardEvent, cut = false) => {
    if (editingRef.current || (e.target as HTMLElement).closest('input')) return;
    const s = whole ? { ...sel, r1: Math.min(sel.r1, tab?.used[0] ?? 1), c1: Math.min(sel.c1, tab?.used[1] ?? 1) } : sel;
    const { rows, sty } = cellsText(s);
    const text = toTsv(rows);
    e.clipboardData.setData('text/plain', text);
    e.clipboardData.setData('text/html', toHtmlTable(rows, sty));
    e.preventDefault();
    clip.current = { tab: tabId, sel: s, cut, text };
  };
  const onPaste = (e: RClipboardEvent) => {
    if (editingRef.current || (e.target as HTMLElement).closest('input')) return;
    e.preventDefault();
    const text = e.clipboardData.getData('text/plain');
    const html = e.clipboardData.getData('text/html');
    const c = clip.current;
    if (c && text === c.text && text !== '') {       // from this sheet: formulas move with their references
      const at = addr(sel.r0, sel.c0);
      const ops: unknown[] = [{ op: c.cut ? 'move' : 'copy', from: rangeText(c.sel), to: at, ...(c.tab !== tabId ? { tab: c.tab, to_tab: tabId } : {}) }];
      void run(ops);
      if (c.cut) clip.current = null;
      return;
    }
    let rows = text ? parseTsv(text.replace(/\n$/, '')) : [];
    if ((!rows.length || (rows.length === 1 && rows[0].length === 1)) && html) rows = tableFromHtml(html) ?? rows;
    if (!rows.length) return;
    void run([{ op: 'set', at: addr(sel.r0, sel.c0), values: rows }]);
    setAnchor({ r: sel.r0, c: sel.c0 }); setFocus({ r: sel.r0 + rows.length - 1, c: sel.c0 + Math.max(...rows.map((r) => r.length)) - 1 });
  };
  const clipH = useRef({ onCopy, onPaste });
  clipH.current = { onCopy, onPaste };
  useEffect(() => {          // the native selection can end up outside the grid (shift-click, drags): listen at the document, act only while the grid has focus
    const live = (e: Event) => { const g = scrollRef.current; return !!g && document.activeElement === g && !(e.target as HTMLElement | null)?.closest?.('input,textarea'); };
    const cp = (e: ClipboardEvent) => { if (live(e)) clipH.current.onCopy(e as unknown as RClipboardEvent, e.type === 'cut'); };
    const ps = (e: ClipboardEvent) => { if (live(e)) clipH.current.onPaste(e as unknown as RClipboardEvent); };
    document.addEventListener('copy', cp); document.addEventListener('cut', cp); document.addEventListener('paste', ps);
    return () => { document.removeEventListener('copy', cp); document.removeEventListener('cut', cp); document.removeEventListener('paste', ps); };
  }, []);
  /* ── keyboard ── */
  const onKeyDown = (e: RKeyboardEvent<HTMLDivElement>) => {
    if (editingRef.current || (e.target as HTMLElement).closest('input, textarea')) return;
    const mod = e.ctrlKey || e.metaKey;
    const k = e.key;
    const step = (dr: number, dc: number) => { e.preventDefault(); const f = e.shiftKey ? focus : anchor; moveTo(f.r + dr, f.c + dc, e.shiftKey); };
    if (mod) {
      const low = k.toLowerCase();
      if (low === 'z') { e.preventDefault(); void history(e.shiftKey ? 'redo' : 'undo'); return; }
      if (low === 'y') { e.preventDefault(); void history('redo'); return; }
      if (low === 'b') { e.preventDefault(); toggle('b', 'bold'); return; }
      if (low === 'i') { e.preventDefault(); toggle('i', 'italic'); return; }
      if (low === 'a') { e.preventDefault(); setAnchor({ r: 1, c: 1 }); setFocus({ r: Math.max(tab?.used[0] ?? 1, 1), c: Math.max(tab?.used[1] ?? 1, 1) }); setWhole(null); return; }
      if (low === 'd') { e.preventDefault(); if (sel.r1 > sel.r0) void run(Array.from({ length: sel.c1 - sel.c0 + 1 }, (_, i) => ({ op: 'fill', range: `${addr(sel.r0, sel.c0 + i)}:${addr(sel.r1, sel.c0 + i)}`, from: addr(sel.r0, sel.c0 + i) }))); return; }
      if (low === 'r') { e.preventDefault(); if (sel.c1 > sel.c0) void run(Array.from({ length: sel.r1 - sel.r0 + 1 }, (_, i) => ({ op: 'fill', range: `${addr(sel.r0 + i, sel.c0)}:${addr(sel.r0 + i, sel.c1)}`, from: addr(sel.r0 + i, sel.c0) }))); return; }
      if (k === 'ArrowDown') { e.preventDefault(); moveTo(tab?.used[0] ?? 1, anchor.c, e.shiftKey); return; }
      if (k === 'ArrowUp') { e.preventDefault(); moveTo(1, anchor.c, e.shiftKey); return; }
      if (k === 'ArrowRight') { e.preventDefault(); moveTo(anchor.r, tab?.used[1] ?? 1, e.shiftKey); return; }
      if (k === 'ArrowLeft') { e.preventDefault(); moveTo(anchor.r, 1, e.shiftKey); return; }
      if (k === 'Home') { e.preventDefault(); moveTo(1, 1); if (scrollRef.current) scrollRef.current.scrollTo(0, 0); return; }
      return;
    }
    switch (k) {
      case 'ArrowDown': return step(1, 0);
      case 'ArrowUp': return step(-1, 0);
      case 'ArrowRight': return step(0, 1);
      case 'ArrowLeft': return step(0, -1);
      case 'PageDown': return step(Math.max(1, Math.floor(view.h / DEFAULT_ROW_H) - 2), 0);
      case 'PageUp': return step(-Math.max(1, Math.floor(view.h / DEFAULT_ROW_H) - 2), 0);
      case 'Home': e.preventDefault(); return moveTo(anchor.r, 1, e.shiftKey);
      case 'End': e.preventDefault(); return moveTo(anchor.r, Math.max(tab?.used[1] ?? 1, 1), e.shiftKey);
      case 'Tab': e.preventDefault(); return moveTo(anchor.r, anchor.c + (e.shiftKey ? -1 : 1));
      case 'Enter': e.preventDefault(); return startEdit(anchor.r, anchor.c);
      case 'F2': e.preventDefault(); return startEdit(anchor.r, anchor.c);
      case 'Delete': case 'Backspace':
        e.preventDefault(); void run([{ op: 'clear', range: rangeArg(), what: 'values' }]); return;
      case 'Escape': clip.current = null; return;
      default:
        if (k.length === 1 && !e.altKey) { e.preventDefault(); startEdit(anchor.r, anchor.c, k); }
    }
  };

  const history = async (which: 'undo' | 'redo') => {
    setSave('saving');
    try {
      const res = await (which === 'undo' ? undoSheet(sheetId) : redoSheet(sheetId));
      if (res.ok) stale(res, CLIENT_ID); else onNotice(which === 'undo' ? t('Nothing to undo.') : t('Nothing to redo.'), 'info');
      setSave('saved');
    } catch (e) { setSave('error'); onNotice((e as Error).message, 'danger'); }
  };

  /* ── tabs ── */
  const addTab = () => void run([{ op: 'add_tab', name: t('Sheet') + ' ' + (tabs.length + 1) }]).then(() => reloadTabs().then((r) => setTabId(r.tabs[r.tabs.length - 1]?.id ?? '')));
  const renameTab = (id: string, name: string) => {
    setRenamingTab(null);
    const cur = tabs.find((x) => x.id === id);
    if (name.trim() && cur && name.trim() !== cur.name) void run([{ op: 'rename_tab', tab: id, name: name.trim() }]);
  };

  /* ── export / import ── */
  const download = (url: string, name: string) => { const a = document.createElement('a'); a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove(); };
  const doImport = async (file: File) => {
    try { await importSheet(sheetId, file, tabId); await reloadTabs(); blocks.current.clear(); cells.current.clear(); setFetchTick((n) => n + 1); onNotice(t('File imported.'), 'info'); }
    catch (e) { onNotice((e as Error).message, 'danger'); }
  };

  /* ── drawing ── */
  const filterCol = tab?.filter?.col ?? 0;
  const now = Date.now();
  const drawCell = (r: number, c: number, layer: string) => {
    const h = rowAxis.size(r);
    if (h === 0) return null;
    const cell = cellAt(r, c);
    const st = styleOf(cell);
    const isSel = inSel(sel, r, c);
    const isActive = r === anchor.r && c === anchor.c;
    const isFill = fillTo && inSel(fillTo, r, c) && !isSel;
    const v = cell?.v;
    const align = st?.al ? (st.al === 'l' ? 'left' : st.al === 'c' ? 'center' : 'right') : typeof v === 'number' ? 'right' : typeof v === 'boolean' || (typeof v === 'string' && v.startsWith('#') && v === cell?.shown) ? 'center' : 'left';
    const wasFresh = fresh.current.get(key(r, c));
    const isLastSel = isSel && !whole && !editing && r === sel.r1 && c === sel.c1;
    return (
      <div
        key={`${layer}${r}:${c}`} className="fs-hj-c" data-cell={addr(r, c)} data-sel={isSel || undefined} data-active={isActive || undefined}
        data-top={(isSel && r === sel.r0) || undefined} data-bottom={(isSel && r === sel.r1) || undefined} data-left={(isSel && c === sel.c0) || undefined} data-right={(isSel && c === sel.c1) || undefined}
        data-fill={isFill || undefined} data-err={(typeof v === 'string' && v.startsWith('#') && v === cell?.shown) || undefined}
        data-fresh={(wasFresh && now - wasFresh < 900) || undefined} data-fcol={c <= fc || undefined} data-frow={r <= fr || undefined}
        style={{ left: colAxis.start(c), top: rowAxis.start(r), width: colAxis.size(c), height: h, textAlign: align, ...cssFor(st, cell?.ov ?? null) }}
      >
        <span className="fs-hj-t">{cell?.shown ?? ''}</span>
        {r === 1 && filterCol === c && <span className="fs-hj-filter" aria-hidden="true"><ListFilter size={11} /></span>}
        {isLastSel && <span className="fs-hj-fh" data-handle="1" />}
      </div>
    );
  };
  const drawRange = (ra: number, rb: number, ca: number, cb: number, layer: string) => {
    const out = [];
    for (let r = ra; r <= rb; r += 1) for (let c = ca; c <= cb; c += 1) out.push(drawCell(r, c, layer));
    return out;
  };

  const chartsOf = (tb: HojaTab) => tb.charts.map((ch) => {
    const p = ch.at ? parseAddr(ch.at) : null;
    const x = p ? colAxis.start(p.c) : 40, y = p ? rowAxis.start(p.r) : 40;
    return (
      <div key={ch.id} className="fs-hj-chart" style={{ left: x, top: y, width: ch.w, height: ch.h }} data-testid="hoja-chart">
        <HojaChart chart={ch} />
        <button type="button" className="fs-hj-chart-x" aria-label={t('Remove the chart')} title={t('Remove the chart')} onClick={() => void run([{ op: 'chart_delete', id: ch.id }])}><X size={12} aria-hidden="true" /></button>
      </div>
    );
  });

  const status = useMemo(() => {
    const vals: unknown[] = [];
    const m = cells.current.get(tabId);
    const n = (sel.r1 - sel.r0 + 1) * (sel.c1 - sel.c0 + 1);
    if (m && n > 1 && n <= 50000) for (let r = sel.r0; r <= sel.r1; r += 1) for (let c = sel.c0; c <= sel.c1; c += 1) vals.push(m.get(key(r, c))?.v);
    return n > 1 && n <= 50000 ? summarise(vals) : null;
  }, [sel, tabId, tick]);

  const barText = editing ? editing.text : rawOf(active);
  const nameBox = whole === 'col' ? `${colLetters(sel.c0)}:${colLetters(sel.c1)}` : whole === 'row' ? `${sel.r0}:${sel.r1}` : rangeText(sel);
  const colHeads = [];
  const colList: number[] = [];
  for (let c = 1; c <= fc; c += 1) colList.push(c);
  for (let c = Math.max(fc + 1, clamp(colAxis.at(view.left + frozenW) - 1, 1, extent.cols)); c <= c1v; c += 1) colList.push(c);
  for (const c of colList) {
    const w = colAxis.size(c);
    colHeads.push(
      <div key={c} className="fs-hj-ch" data-on={(sel.c0 <= c && c <= sel.c1) || undefined} data-pinned={c <= fc || undefined} data-col={colLetters(c)} style={{ left: c <= fc ? colAxis.start(c) : colAxis.start(c) - view.left, width: w }}
        onPointerDown={(e) => { if ((e.target as HTMLElement).dataset.resize) return; pickHeader('col', c, e.shiftKey); }}
        onContextMenu={(e) => { e.preventDefault(); if (!(whole === 'col' && c >= sel.c0 && c <= sel.c1)) pickHeader('col', c, false); setCtx({ x: e.clientX, y: e.clientY, kind: 'col', index: c }); }}>
        {colLetters(c)}
        <span className="fs-hj-rz" data-resize="1" onPointerDown={(e) => startResize(e, 'col', c)} onDoubleClick={() => void run([{ op: 'width', cols: { [colLetters(c)]: 'auto' } }])} />
      </div>,
    );
  }
  const rowHeads = [];
  const rowList: number[] = [];
  for (let r = 1; r <= fr; r += 1) rowList.push(r);
  for (let r = Math.max(fr + 1, clamp(rowAxis.at(view.top + frozenH) - 1, 1, extent.rows)); r <= r1v; r += 1) rowList.push(r);
  for (const r of rowList) {
    const h = rowAxis.size(r);
    if (!h) continue;
    rowHeads.push(
      <div key={r} className="fs-hj-rh" data-on={(sel.r0 <= r && r <= sel.r1) || undefined} data-pinned={r <= fr || undefined} style={{ top: r <= fr ? rowAxis.start(r) : rowAxis.start(r) - view.top, height: h }}
        onPointerDown={(e) => { if ((e.target as HTMLElement).dataset.resize) return; pickHeader('row', r, e.shiftKey); }}
        onContextMenu={(e) => { e.preventDefault(); if (!(whole === 'row' && r >= sel.r0 && r <= sel.r1)) pickHeader('row', r, false); setCtx({ x: e.clientX, y: e.clientY, kind: 'row', index: r }); }}>
        {r}
        <span className="fs-hj-rz" data-resize="1" onPointerDown={(e) => startResize(e, 'row', r)} />
      </div>,
    );
  }

  const ctxItems = (): { label: string; run: () => void; danger?: boolean }[] => {
    if (!ctx) return [];
    const n = ctx.kind === 'col' ? sel.c1 - sel.c0 + 1 : sel.r1 - sel.r0 + 1;
    if (ctx.kind === 'col') {
      return [
        { label: t('Insert column left'), run: () => void run([{ op: 'insert_cols', at: sel.c0, count: n }]) },
        { label: t('Insert column right'), run: () => void run([{ op: 'insert_cols', at: sel.c1 + 1, count: n }]) },
        { label: t('Delete column'), danger: true, run: () => void run([{ op: 'delete_cols', at: sel.c0, count: n }]) },
        { label: t('Sort A to Z'), run: () => sortBy(false) },
        { label: t('Sort Z to A'), run: () => sortBy(true) },
        { label: t('Fit column to its content'), run: () => void run([{ op: 'width', cols: Object.fromEntries(Array.from({ length: n }, (_, i) => [colLetters(sel.c0 + i), 'auto'])) }]) },
        { label: t('Freeze up to this column'), run: () => void run([{ op: 'freeze', rows: fr, cols: sel.c1 }]) },
      ];
    }
    if (ctx.kind === 'row') {
      return [
        { label: t('Insert row above'), run: () => void run([{ op: 'insert_rows', at: sel.r0, count: n }]) },
        { label: t('Insert row below'), run: () => void run([{ op: 'insert_rows', at: sel.r1 + 1, count: n }]) },
        { label: t('Delete row'), danger: true, run: () => void run([{ op: 'delete_rows', at: sel.r0, count: n }]) },
        { label: t('Freeze up to this row'), run: () => void run([{ op: 'freeze', rows: sel.r1, cols: fc }]) },
      ];
    }
    const tb = tabs[ctx.index];
    return tb ? [
      { label: t('Rename'), run: () => setRenamingTab(tb.id) },
      { label: t('Move left'), run: () => void run([{ op: 'move_tab', tab: tb.id, to: Math.max(0, ctx.index - 1) }]) },
      { label: t('Move right'), run: () => void run([{ op: 'move_tab', tab: tb.id, to: ctx.index + 1 }]) },
      { label: t('Delete tab'), danger: true, run: () => (tabs.length > 1 ? void run([{ op: 'delete_tab', tab: tb.id }]) : onNotice(t('A workbook needs at least one tab.'), 'warning')) },
    ] : [];
  };

  const nfItems = [
    { label: t('General'), nf: 'General' }, { label: t('Number'), nf: '#,##0.00' }, { label: t('Integer'), nf: '0' }, { label: t('Currency (€)'), cur: 'eur' },
    { label: t('Currency ($)'), cur: 'usd' }, { label: t('Percent'), nf: '0.00%' }, { label: t('Date'), nf: 'yyyy-mm-dd' }, { label: t('Text'), nf: '@' },
  ];

  if (load === 'loading') return <div className="fs-hj" hidden={hidden} data-testid="hoja"><div className="fs-hj-state" role="status"><span className="fs-hj-spin" aria-hidden="true" />{t('Loading the spreadsheet')}</div></div>;
  if (load === 'error') return <div className="fs-hj" hidden={hidden} data-testid="hoja"><div className="fs-hj-state" role="alert">{t('Could not open the spreadsheet.')} {loadError}</div></div>;

  const editBox = editing && editing.src === 'cell' ? (
    <input
      className="fs-hj-edit" autoFocus value={editing.text} spellCheck={false} aria-label={t('Cell')} data-testid="hoja-edit"
      style={{ left: colAxis.start(editing.c), top: rowAxis.start(editing.r), minWidth: colAxis.size(editing.c), height: rowAxis.size(editing.r) }}
      onChange={(e) => setEditing({ ...editing, text: e.target.value })}
      onKeyDown={(e) => {
        e.stopPropagation();
        if (e.key === 'Enter') { e.preventDefault(); commit(e.shiftKey ? 'up' : 'down'); }
        else if (e.key === 'Tab') { e.preventDefault(); commit(e.shiftKey ? 'left' : 'right'); }
        else if (e.key === 'Escape') { e.preventDefault(); cancelEdit(); }
      }}
      onBlur={() => { if (editingRef.current?.src === 'cell') commit('none'); }}
      onFocus={(e) => { const v = e.target.value; e.target.setSelectionRange(v.length, v.length); }}
    />
  ) : null;

  return (
    <div className="fs-hj" hidden={hidden} data-testid="hoja" data-load={load} onClick={() => setCtx(null)}>
      <div className="fs-hj-head">
        <input
          className="fs-hj-title" value={titleText} aria-label={t('Spreadsheet title')} onChange={(e) => setTitleText(e.target.value)}
          onBlur={() => { const v = titleText.trim(); if (v && v !== title) void renameSheet(sheetId, v).then((m) => onTitle(m.title)).catch(() => onNotice(t('Could not rename.'), 'danger')); }}
          onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); }}
        />
        <span className="fs-hj-status" data-state={save} role="status" data-testid="hoja-status">{save === 'saved' ? t('Saved') : save === 'saving' ? t('Saving…') : t('Not saved yet')}</span>
        <span className="fs-hj-grow" />
        <IconButton icon={Undo2} label={t('Undo')} size="sm" onClick={() => void history('undo')} testId="hoja-undo" />
        <IconButton icon={Redo2} label={t('Redo')} size="sm" onClick={() => void history('redo')} testId="hoja-redo" />
        <Menu align="end" testId="hoja-export" trigger={<IconButton icon={Download} label={t('Export')} size="sm" />} items={[
          { label: t('Excel workbook (.xlsx)'), onSelect: () => download(sheetXlsxUrl(sheetId), `${titleText || 'sheet'}.xlsx`) },
          { label: t('CSV of this tab'), onSelect: () => download(sheetCsvUrl(sheetId, tabId), `${titleText || 'sheet'}.csv`) },
        ]} />
        <IconButton icon={Upload} label={t('Import an .xlsx or .csv file')} size="sm" onClick={() => fileRef.current?.click()} testId="hoja-import" />
        <input ref={fileRef} type="file" accept=".xlsx,.csv,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" hidden onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ''; if (f) void doImport(f); }} />
        {onToggleMaximize && (
          <IconButton icon={maximized ? Minimize2 : Maximize2} label={maximized ? t('Bring the chat back (Esc)') : t('Make the spreadsheet fill the window')} size="sm" onClick={onToggleMaximize} testId="hoja-maximize" />
        )}
      </div>

      <div className="fs-hj-bar" role="toolbar" aria-label={t('Spreadsheet tools')}>
        <Tb icon={Bold} label={t('Bold (Ctrl+B)')} size="sm" pressed={!!activeStyle?.b} onClick={() => toggle('b', 'bold')} testId="hoja-bold" />
        <Tb icon={Italic} label={t('Italic (Ctrl+I)')} size="sm" pressed={!!activeStyle?.i} onClick={() => toggle('i', 'italic')} testId="hoja-italic" />
        <Tb icon={WrapText} label={t('Wrap text')} size="sm" pressed={!!activeStyle?.wrap} onClick={() => toggle('wrap', 'wrap')} />
        <span className="fs-hj-sep" />
        <Tb icon={AlignLeft} label={t('Align left')} size="sm" pressed={activeStyle?.al === 'l'} onClick={() => fmt({ align: 'left' })} />
        <Tb icon={AlignCenter} label={t('Align center')} size="sm" pressed={activeStyle?.al === 'c'} onClick={() => fmt({ align: 'center' })} />
        <Tb icon={AlignRight} label={t('Align right')} size="sm" pressed={activeStyle?.al === 'r'} onClick={() => fmt({ align: 'right' })} />
        <span className="fs-hj-sep" />
        <label className="fs-hj-color" title={t('Text colour')}><Baseline size={15} aria-hidden="true" /><input type="color" aria-label={t('Text colour')} value={activeStyle?.c ?? '#000000'} onChange={(e) => fmt({ color: e.target.value })} data-testid="hoja-color" /></label>
        <label className="fs-hj-color" title={t('Fill colour')}><PaintBucket size={15} aria-hidden="true" /><input type="color" aria-label={t('Fill colour')} value={activeStyle?.bg ?? '#ffffff'} onChange={(e) => fmt({ fill: e.target.value })} data-testid="hoja-fill" /></label>
        <span className="fs-hj-sep" />
        <Menu testId="hoja-numfmt" trigger={<button type="button" className="fs-hj-fmtbtn" aria-label={t('Number format')} data-testid="hoja-numfmt-trigger">{t('Format')}</button>} items={nfItems.map((i) => ({
          label: i.label, onSelect: () => (i.cur ? fmt({ format: 'currency', symbol: i.cur, decimals: 2 }) : fmt({ format: i.nf })),
        }))} />
        <IconButton icon={Percent} label={t('Percent')} size="sm" onClick={() => fmt({ format: 'percent', decimals: 0 })} testId="hoja-percent" />
        <button type="button" className="fs-hj-fmtbtn" title={t('Fewer decimals')} aria-label={t('Fewer decimals')} onClick={() => fmt({ format: withDecimals(activeStyle?.nf, -1) })}>.0</button>
        <button type="button" className="fs-hj-fmtbtn" title={t('More decimals')} aria-label={t('More decimals')} onClick={() => fmt({ format: withDecimals(activeStyle?.nf, 1) })}>.00</button>
        <span className="fs-hj-sep" />
        <IconButton icon={ArrowDownAZ} label={t('Sort A to Z')} size="sm" onClick={() => sortBy(false)} testId="hoja-sort-asc" />
        <IconButton icon={ArrowUpZA} label={t('Sort Z to A')} size="sm" onClick={() => sortBy(true)} />
        <Tb icon={ListFilter} label={tab?.filter ? t('Clear the filter') : t('Filter this column')} size="sm" pressed={!!tab?.filter} testId="hoja-filter"
          onClick={() => (tab?.filter ? void run([{ op: 'filter', clear: true }]) : setPrompt({
            title: t('Filter'), label: t('Show only rows of column {col} where it is (for example >10, Madrid)', { col: colLetters(anchor.c) }), initial: '',
            onSubmit: (v) => void run([{ op: 'filter', range: usedRange(), column: colLetters(anchor.c), where: v }]),
          }))} />
        <Tb icon={Snowflake} label={fr || fc ? t('Unfreeze panes') : t('Freeze the top row')} size="sm" pressed={!!(fr || fc)} onClick={() => void run([{ op: 'freeze', rows: fr || fc ? 0 : 1, cols: 0 }])} testId="hoja-freeze" />
        <Menu testId="hoja-cond" trigger={<IconButton icon={Highlighter} label={t('Conditional formatting')} size="sm" />} items={[
          { label: t('Colour scale'), onSelect: () => void run([{ op: 'cond', range: rangeArg(), rule: 'scale', scale: ['white', 'green'] }]) },
          { label: t('Highlight duplicates'), onSelect: () => void run([{ op: 'cond', range: rangeArg(), rule: 'duplicate', fill: 'yellow' }]) },
          { label: t('Highlight values greater than…'), onSelect: () => setPrompt({ title: t('Highlight'), label: t('Greater than'), initial: '0', onSubmit: (v) => void run([{ op: 'cond', range: rangeArg(), rule: 'gt', value: Number(v) || 0, fill: 'red' }]) }) },
          null,
          { label: t('Clear conditional formats'), onSelect: () => void run([{ op: 'cond', clear: true }]) },
        ]} />
        <Menu testId="hoja-chart-menu" trigger={<IconButton icon={ChartColumn} label={t('Insert a chart')} size="sm" testId="hoja-chart-trigger" />} items={[
          { label: t('Bar chart'), icon: ChartColumn, onSelect: () => chartOf('bar') },
          { label: t('Line chart'), icon: ChartLine, onSelect: () => chartOf('line') },
          { label: t('Pie chart'), icon: ChartPie, onSelect: () => chartOf('pie') },
          { label: t('Scatter chart'), icon: ChartScatter, onSelect: () => chartOf('scatter') },
        ]} />
        <span className="fs-hj-grow" />
        <IconButton icon={Sigma} label={t('Sum the selection below it')} size="sm" onClick={() => {
          const at = addr(sel.r1 + 1, sel.c0);
          void run([{ op: 'set', at, values: [[`=SUM(${rangeText(sel)})`]] }]);
        }} testId="hoja-sum" />
      </div>

      <div className="fs-hj-fx">
        <span className="fs-hj-name" data-testid="hoja-name" aria-label={t('Selected cells')}>{nameBox}</span>
        <span className="fs-hj-fxl" aria-hidden="true">fx</span>
        <input
          className="fs-hj-formula" data-testid="hoja-formula" aria-label={t('Formula bar')} spellCheck={false} value={barText}
          onFocus={() => { if (!editingRef.current) startEdit(anchor.r, anchor.c, undefined, 'bar'); }}
          onChange={(e) => setEditing({ r: editing?.r ?? anchor.r, c: editing?.c ?? anchor.c, text: e.target.value, src: 'bar' })}
          onKeyDown={(e) => {
            if (e.key === 'Enter') { e.preventDefault(); commit('down'); }
            else if (e.key === 'Escape') { e.preventDefault(); cancelEdit(); }
          }}
          onBlur={() => { if (editingRef.current?.src === 'bar') commit('none'); }}
        />
      </div>

      <div className="fs-hj-grid" style={{ ['--hj-rh' as string]: ROW_HEAD_W + 'px', ['--hj-ch' as string]: COL_HEAD_H + 'px' } as React.CSSProperties}>
        <div className="fs-hj-corner" onPointerDown={() => { setAnchor({ r: 1, c: 1 }); setFocus({ r: Math.max(tab?.used[0] ?? 1, 1), c: Math.max(tab?.used[1] ?? 1, 1) }); setWhole(null); }} />
        <div className="fs-hj-colheads" aria-hidden="true">{colHeads}</div>
        <div className="fs-hj-rowheads" aria-hidden="true">{rowHeads}</div>
        <div
          ref={scrollRef} className="fs-hj-scroll" tabIndex={0} role="grid" aria-label={t('Spreadsheet')} aria-rowcount={extent.rows} aria-colcount={extent.cols} data-testid="hoja-grid"
          onScroll={(e) => { const el = e.currentTarget; setView((v) => (v.left === el.scrollLeft && v.top === el.scrollTop ? v : { ...v, left: el.scrollLeft, top: el.scrollTop })); }}
          onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp} onKeyDown={onKeyDown}
          
          onDoubleClick={(e) => { if ((e.target as HTMLElement).closest('.fs-hj-edit, .fs-hj-chart')) return; const p = hit(e); startEdit(p.r, p.c); }}
          onContextMenu={(e) => e.preventDefault()}
        >
          <div className="fs-hj-sizer" style={{ width: colAxis.total, height: rowAxis.total }}>
            <div className="fs-hj-layer">
              {drawRange(Math.max(r0v, fr + 1), r1v, Math.max(c0v, fc + 1), c1v, 'm')}
              {tab && chartsOf(tab)}
              {Object.values(cursors).filter((cu) => cu.tab === tabId).map((cu) => (
                <div key={cu.actor.id} className="fs-hj-agent" data-state={cu.state} data-testid="hoja-agent-cursor"
                  style={{ left: colAxis.start(cu.c), top: rowAxis.start(cu.r), width: colAxis.size(cu.c), height: rowAxis.size(cu.r), ['--ag' as string]: cu.actor.color } as React.CSSProperties}>
                  <span className="fs-hj-agent-name">{cu.actor.name}</span>
                </div>
              ))}
              {editBox}
            </div>
            {fr > 0 && <div className="fs-hj-layer fs-hj-frozen-rows" style={{ height: frozenH }}>{drawRange(1, fr, Math.max(c0v, fc + 1), c1v, 'r')}</div>}
            {fc > 0 && <div className="fs-hj-layer fs-hj-frozen-cols" style={{ width: frozenW }}>{drawRange(Math.max(r0v, fr + 1), r1v, 1, fc, 'c')}</div>}
            {fr > 0 && fc > 0 && <div className="fs-hj-layer fs-hj-frozen-corner" style={{ width: frozenW, height: frozenH }}>{drawRange(1, fr, 1, fc, 'x')}</div>}
          </div>
        </div>
      </div>

      <div className="fs-hj-foot">
        <button type="button" className="fs-hj-addtab" aria-label={t('Add a tab')} title={t('Add a tab')} onClick={addTab} data-testid="hoja-add-tab"><Plus size={14} aria-hidden="true" /></button>
        <div className="fs-hj-tabs" role="tablist" aria-label={t('Tabs')}>
          {tabs.map((tb, i) => (
            renamingTab === tb.id ? (
              <input key={tb.id} className="fs-hj-tabedit" autoFocus defaultValue={tb.name} aria-label={t('Tab name')}
                onBlur={(e) => renameTab(tb.id, e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); else if (e.key === 'Escape') setRenamingTab(null); }} />
            ) : (
              <button key={tb.id} type="button" role="tab" className="fs-hj-tab" aria-selected={tb.id === tabId} data-pulse={pulse[tb.id] ? true : undefined} data-testid={'hoja-tab-' + i}
                onClick={() => { if (tb.id !== tabId) { setTabId(tb.id); setAnchor({ r: 1, c: 1 }); setFocus({ r: 1, c: 1 }); setWhole(null); setEditing(null); setExtent({ rows: 100, cols: 26 }); scrollRef.current?.scrollTo(0, 0); } }}
                onDoubleClick={() => setRenamingTab(tb.id)}
                onContextMenu={(e) => { e.preventDefault(); setCtx({ x: e.clientX, y: e.clientY, kind: 'tab', index: i }); }}>
                {tb.name}
              </button>
            )
          ))}
        </div>
        <span className="fs-hj-grow" />
        {status && status.count > 0 && (
          <span className="fs-hj-sum" data-testid="hoja-summary">{t('Sum')} {+status.sum.toFixed(4)} · {t('Average')} {+status.avg.toFixed(4)} · {t('Count')} {status.count}</span>
        )}
        <span className="fs-hj-rows">{tab ? t('{n} rows', { n: tab.used[0] }) : ''}</span>
      </div>

      {ctx && (
        <div className="fs-hj-menu" role="menu" style={{ left: ctx.x, top: ctx.y }} data-testid="hoja-context-menu" onClick={(e) => e.stopPropagation()}>
          {ctxItems().map((it) => (
            <button key={it.label} type="button" role="menuitem" data-danger={it.danger || undefined} onClick={() => { setCtx(null); it.run(); }}>{it.label}</button>
          ))}
        </div>
      )}
      {prompt && (
        <form className="fs-hj-prompt" role="dialog" aria-label={prompt.title} onSubmit={(e) => { e.preventDefault(); const v = (new FormData(e.currentTarget).get('v') as string) ?? ''; const p = prompt; setPrompt(null); if (v.trim()) p.onSubmit(v.trim()); }}>
          <label>{prompt.label}<input name="v" autoFocus defaultValue={prompt.initial} spellCheck={false} data-testid="hoja-prompt-input" /></label>
          <div className="fs-hj-prompt-actions">
            <button type="button" onClick={() => setPrompt(null)}>{t('Cancel')}</button>
            <button type="submit" data-primary="1" data-testid="hoja-prompt-ok">{t('Apply')}</button>
          </div>
        </form>
      )}
    </div>
  );
}
