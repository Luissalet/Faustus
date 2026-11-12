"""src/hoja.py -- Hoja, the spreadsheet the person and the agent edit together (the spreadsheet twin of Pizarra).

A workbook has tabs; a tab has cells (a value or a formula, a style), column widths, a frozen header, filters,
conditional formats and charts. It is a JSON working model on the server (``model.json``) and an .xlsx file on disk
(``sheet.xlsx``, rewritten shortly after each change: that file is the durable artifact, see ``src/hoja_xlsx.py``).

* formulas: ``src/hoja_formula.py`` (English and Spanish names/separators, >100 functions, errors as values);
  this module keeps the dependency graph (what each formula reads and who reads each cell) and recalculates only
  what a change can reach, in dependency order, flagging circular references as ``#CIRC!``;
* ops: ``edit(id, ops)`` is the one language the agent's ``sheet_write``, the REST route and the window all speak
  (set, fill, copy, clear, format, width, height, freeze, tabs, insert/delete rows and columns, sort, filter,
  conditional formats, charts, csv);
* reading: ``read_text`` (compact, for the model), ``window`` (cells of a viewport, for the grid), ``query``
  (aggregates without dumping cells), ``context_line`` (one line in the turn's context);
* undo / redo of the person's and the agent's changes alike (in memory).

Storage: ``DATA_DIR/surfaces/hoja/<id>/{meta.json, model.json, sheet.xlsx, history/}``. One process owns a workbook.
"""
from __future__ import annotations

import copy
import csv
import io
import json
import os
import re
import secrets
import threading
import time
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

from src import hoja_format as F
from src import hoja_formula as H
from src.hoja_formula import Err, addr, col_letters, col_number, parse_addr, parse_range

MAX_CELLS = 3_000_000
MAX_OPS = 400
MAX_TABS = 60
MAX_ROWS = 1_048_576
MAX_COLS = 16_384
MAX_TEXT = 32_767
DEFAULT_COL_W = 96
DEFAULT_ROW_H = 24
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
STYLE_KEYS = ("b", "i", "c", "bg", "al", "wrap", "nf", "bd")
DATE_FUNCS = {"TODAY", "NOW", "DATE", "EDATE", "EOMONTH", "DATEVALUE", "WORKDAY"}
COLORS = {"red": "#e5484d", "orange": "#f76b15", "yellow": "#f5d90a", "green": "#30a46c", "blue": "#0090ff", "purple": "#8e4ec6",
          "pink": "#d6409f", "gray": "#8b8d98", "grey": "#8b8d98", "black": "#000000", "white": "#ffffff", "lightgreen": "#c9f0d8",
          "lightred": "#fbd5d7", "lightyellow": "#fdf3b0", "lightblue": "#cfe8ff"}
_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


class SheetError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def new_id() -> str:
    return secrets.token_hex(8)


# ---------------------------------------------------------------------------
# model
# ---------------------------------------------------------------------------
class Cell:
    """Immutable entry except for the computed ``v`` / parsed ``ast`` / ``regs`` the workbook refreshes."""
    __slots__ = ("raw", "sid", "v", "ast", "regs")

    def __init__(self, raw: Any, sid: int = 0):
        self.raw = raw
        self.sid = sid
        self.v = raw
        self.ast = None
        self.regs: List[Tuple] = []

    @property
    def is_formula(self) -> bool:
        return self.ast is not None


class Tab:
    def __init__(self, tid: str, name: str):
        self.id = tid
        self.name = name
        self.cells: Dict[Tuple[int, int], Cell] = {}
        self.cols: Dict[int, int] = {}
        self.rows: Dict[int, int] = {}
        self.freeze = {"rows": 0, "cols": 0}
        self.filter: Optional[Dict[str, Any]] = None
        self.hidden: Set[int] = set()
        self.cf: List[Dict[str, Any]] = []
        self.charts: List[Dict[str, Any]] = []
        self.max_r = 0
        self.max_c = 0

    def used(self) -> Tuple[int, int]:
        return self.max_r, self.max_c

    def recompute_bounds(self) -> None:
        self.max_r = max((r for r, _ in self.cells), default=0)
        self.max_c = max((c for _, c in self.cells), default=0)

    def snapshot(self) -> Dict[str, Any]:
        return {"id": self.id, "name": self.name, "cells": dict(self.cells), "cols": dict(self.cols), "rows": dict(self.rows),
                "freeze": dict(self.freeze), "filter": copy.deepcopy(self.filter), "hidden": set(self.hidden),
                "cf": copy.deepcopy(self.cf), "charts": copy.deepcopy(self.charts)}

    @staticmethod
    def from_snapshot(s: Dict[str, Any]) -> "Tab":
        t = Tab(s["id"], s["name"])
        t.cells, t.cols, t.rows = dict(s["cells"]), dict(s["cols"]), dict(s["rows"])
        t.freeze, t.filter, t.hidden = dict(s["freeze"]), copy.deepcopy(s["filter"]), set(s["hidden"])
        t.cf, t.charts = copy.deepcopy(s["cf"]), copy.deepcopy(s["charts"])
        t.recompute_bounds()
        return t


class Changes:
    """What an edit touched: per tab a set of cells (or ``full``), plus structure and the agent's cursor trail."""

    def __init__(self) -> None:
        self.cells: Dict[str, Set[Tuple[int, int]]] = {}
        self.full: Set[str] = set()
        self.struct = False
        self.cursor: List[Dict[str, Any]] = []
        self.dirty: Set[Tuple[str, int, int]] = set()      # what to recalculate from (not capped)
        self.all = False                                   # structure changed: recalculate everything

    def touch(self, tab: str, r: int, c: int) -> None:
        self.dirty.add((tab, r, c))
        if tab in self.full:
            return
        s = self.cells.setdefault(tab, set())
        s.add((r, c))
        if len(s) > 4000:
            self.full.add(tab)
            self.cells.pop(tab, None)

    def touch_full(self, tab: str) -> None:
        self.full.add(tab)
        self.cells.pop(tab, None)

    def empty(self) -> bool:
        return not (self.cells or self.full or self.struct or self.dirty)

    def payload(self) -> Dict[str, Any]:
        return {"tabs": {**{t: {"cells": sorted(list(v))[:4000]} for t, v in self.cells.items()}, **{t: {"full": True} for t in self.full}},
                "struct": self.struct}


class Workbook:
    def __init__(self) -> None:
        self.tabs: List[Tab] = []
        self.styles: List[Dict[str, Any]] = [{}]
        self._style_ix: Dict[str, int] = {"{}": 0}
        self.dep_cells: Dict[Tuple[str, int, int], Set[Tuple[str, int, int]]] = {}
        self.dep_ranges: Dict[str, Dict[Tuple[int, int, int, int], Set[Tuple[str, int, int]]]] = {}
        self.volatile: Set[Tuple[str, int, int]] = set()
        self.seq = 0
        self.undo: List[Dict[str, Any]] = []
        self.redo: List[Dict[str, Any]] = []
        self._names: Dict[str, str] = {}
        self._now: Optional[Any] = None

    # -- tabs ----
    def tab(self, ref: Any) -> Optional[Tab]:
        if ref is None or ref == "":
            return None
        s = str(ref)
        for t in self.tabs:
            if t.id == s:
                return t
        low = s.lower()
        for t in self.tabs:
            if t.name.lower() == low:
                return t
        return None

    def reindex_names(self) -> None:
        self._names = {t.name.lower(): t.id for t in self.tabs}

    def new_tab(self, name: str, at: Optional[int] = None) -> Tab:
        self.seq += 1
        t = Tab(f"t{self.seq}", name)
        if at is None or at >= len(self.tabs):
            self.tabs.append(t)
        else:
            self.tabs.insert(max(0, at), t)
        self.reindex_names()
        return t

    def total_cells(self) -> int:
        return sum(len(t.cells) for t in self.tabs)

    # -- styles ----
    def style_id(self, style: Dict[str, Any]) -> int:
        clean = {k: v for k, v in style.items() if k in STYLE_KEYS and v not in (None, "", False, 0) or (k == "nf" and v)}
        key = json.dumps(clean, sort_keys=True, separators=(",", ":"))
        i = self._style_ix.get(key)
        if i is None:
            i = len(self.styles)
            self.styles.append(clean)
            self._style_ix[key] = i
        return i

    def merged_style(self, sid: int, patch: Dict[str, Any]) -> int:
        cur = dict(self.styles[sid]) if sid < len(self.styles) else {}
        for k, v in patch.items():
            if v is None or v == "" or v is False:
                cur.pop(k, None)
            else:
                cur[k] = v
        return self.style_id(cur)

    # -- dependency graph ----
    def unregister(self, key: Tuple[str, int, int], cell: Cell) -> None:
        for reg in cell.regs:
            if reg[0] == "c":
                s = self.dep_cells.get(reg[1])
                if s:
                    s.discard(key)
                    if not s:
                        self.dep_cells.pop(reg[1], None)
            else:
                d = self.dep_ranges.get(reg[1])
                if d:
                    s = d.get(reg[2])
                    if s:
                        s.discard(key)
                        if not s:
                            d.pop(reg[2], None)
        cell.regs = []
        self.volatile.discard(key)

    def register(self, key: Tuple[str, int, int], cell: Cell) -> None:
        if cell.ast is None:
            return
        regs: List[Tuple] = []
        for sheet, r0, c0, r1, c1 in H.refs(cell.ast):
            tid = self._names.get(sheet.lower()) if sheet else key[0]
            if tid is None:
                continue
            if (r0, c0) == (r1, c1):
                k = (tid, r0, c0)
                self.dep_cells.setdefault(k, set()).add(key)
                regs.append(("c", k))
            else:
                rk = (r0, c0, r1, c1)
                self.dep_ranges.setdefault(tid, {}).setdefault(rk, set()).add(key)
                regs.append(("r", tid, rk))
        cell.regs = regs
        if H.volatile(cell.ast):
            self.volatile.add(key)

    def rebuild_graph(self) -> None:
        self.dep_cells.clear()
        self.dep_ranges.clear()
        self.volatile.clear()
        for t in self.tabs:
            for (r, c), cell in t.cells.items():
                cell.regs = []
                if cell.ast is not None:
                    self.register((t.id, r, c), cell)

    def dependents(self, changed: Iterable[Tuple[str, int, int]]) -> Set[Tuple[str, int, int]]:
        out: Set[Tuple[str, int, int]] = set()
        frontier = list(changed)
        while frontier:
            per_tab: Dict[str, List[Tuple[int, int]]] = {}
            nxt: List[Tuple[str, int, int]] = []
            for key in frontier:
                per_tab.setdefault(key[0], []).append((key[1], key[2]))
                for fk in self.dep_cells.get(key, ()):
                    if fk not in out:
                        out.add(fk)
                        nxt.append(fk)
            for tid, pts in per_tab.items():
                ranges = self.dep_ranges.get(tid)
                if not ranges:
                    continue
                rmin, rmax = min(p[0] for p in pts), max(p[0] for p in pts)
                cmin, cmax = min(p[1] for p in pts), max(p[1] for p in pts)
                for (r0, c0, r1, c1), fks in ranges.items():
                    if r1 < rmin or r0 > rmax or c1 < cmin or c0 > cmax:
                        continue
                    if len(pts) > 1 and not any(r0 <= r <= r1 and c0 <= c <= c1 for r, c in pts):
                        continue
                    for fk in fks:
                        if fk not in out:
                            out.add(fk)
                            nxt.append(fk)
            frontier = nxt
        return out


class WbCtx(H.Ctx):
    def __init__(self, wb: Workbook) -> None:
        self.wb = wb
        self._by_id = {t.id: t for t in wb.tabs}

    def resolve_tab(self, name: Optional[str]) -> Optional[str]:
        return self.wb._names.get(str(name).lower()) if name else self.cur_tab

    def cell(self, tab: str, r: int, c: int) -> Any:
        t = self._by_id.get(tab)
        if t is None:
            return H.REF
        cell = t.cells.get((r, c))
        return None if cell is None else cell.v

    def bounds(self, tab: str) -> Tuple[int, int]:
        t = self._by_id.get(tab)
        return t.used() if t else (0, 0)

    def iter_range(self, tab: str, r0: int, c0: int, r1: int, c1: int) -> Iterator[Tuple[int, int, Any]]:
        t = self._by_id.get(tab)
        if t is None:
            return
        r1, c1 = min(r1, max(t.max_r, r0)), min(c1, max(t.max_c, c0))
        area = (r1 - r0 + 1) * (c1 - c0 + 1)
        if area <= len(t.cells):
            for r in range(r0, r1 + 1):
                for c in range(c0, c1 + 1):
                    cell = t.cells.get((r, c))
                    if cell is not None and cell.v is not None:
                        yield r, c, cell.v
        else:
            for (r, c), cell in list(t.cells.items()):
                if r0 <= r <= r1 and c0 <= c <= c1 and cell.v is not None:
                    yield r, c, cell.v

    def today(self):
        return self.wb._now.date() if self.wb._now else super().today()

    def now(self):
        return self.wb._now or super().now()


# ---------------------------------------------------------------------------
# cell values
# ---------------------------------------------------------------------------
def _norm_color(v: Any) -> Optional[str]:
    if v is None or v == "":
        return None
    s = str(v).strip().lower()
    if s in COLORS:
        return COLORS[s]
    if _HEX_RE.match(s):
        return s if len(s) == 7 else "#" + "".join(ch * 2 for ch in s[1:])
    raise SheetError("bad_args", f"unknown colour {v!r}: use a name (red, green, lightblue ...) or #rrggbb")


def _clean_value(v: Any) -> Any:
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
            raise SheetError("bad_args", "numbers must be finite")
        return v
    if isinstance(v, str):
        return v[:MAX_TEXT]
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)[:MAX_TEXT]
    return str(v)[:MAX_TEXT]


def make_cell(wb: Workbook, tab: Tab, r: int, c: int, value: Any, *, parse: bool = True, sid: int = 0) -> Optional[Cell]:
    """A Cell from what someone typed (or the agent sent). Formulas are stored in canonical English form."""
    value = _clean_value(value)
    if value is None or value == "":
        return Cell(None, sid) if sid else None
    hint: Optional[str] = None
    if isinstance(value, str):
        if value.startswith("=") and len(value) > 1:
            try:
                canon = H.canonical(value)
                ast = H.parse(canon)
            except H.FormulaSyntax as exc:
                raise SheetError("bad_formula", f"{addr(r, c)}: {exc} in {value[:80]!r}") from None
            cell = Cell(canon, sid)
            cell.ast = ast
            return cell
        if parse:
            value, hint = F.parse_input(value)
            if value is None:
                return Cell(None, sid) if sid else None
        elif value.startswith("'"):
            value = value[1:]
    cell = Cell(value, sid)
    if hint and not (wb.styles[sid].get("nf") if sid < len(wb.styles) else None):
        cell.sid = wb.merged_style(sid, {"nf": hint})
    return cell


def put(wb: Workbook, tab: Tab, r: int, c: int, cell: Optional[Cell], ch: Optional[Changes] = None) -> None:
    """Replace the cell at (r, c), keeping the graph and the bounds right (values are recomputed by the caller)."""
    if not (1 <= r <= MAX_ROWS and 1 <= c <= MAX_COLS):
        raise SheetError("bad_args", f"{addr(r, c)} is outside the sheet")
    key = (tab.id, r, c)
    old = tab.cells.get((r, c))
    if old is not None:
        wb.unregister(key, old)
    if cell is None:
        tab.cells.pop((r, c), None)
        if r == tab.max_r or c == tab.max_c:
            tab.max_r = max(tab.max_r, 0)
            tab._dirty_bounds = True  # type: ignore[attr-defined]
    else:
        if len(tab.cells) >= MAX_CELLS and old is None:
            raise SheetError("too_big", f"a workbook holds at most {MAX_CELLS:,} cells")
        tab.cells[(r, c)] = cell
        tab.max_r = max(tab.max_r, r)
        tab.max_c = max(tab.max_c, c)
        if cell.ast is not None:
            wb.register(key, cell)
        else:
            cell.v = cell.raw
    if ch is not None:
        ch.touch(tab.id, r, c)


def fix_bounds(tab: Tab) -> None:
    if getattr(tab, "_dirty_bounds", False):
        tab.recompute_bounds()
        tab._dirty_bounds = False  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# recalculation
# ---------------------------------------------------------------------------
def _eval_cell(ctx: WbCtx, tid: str, r: int, c: int, cell: Cell) -> None:
    ctx.cur_tab, ctx.cur_pos = tid, (r, c)
    if cell.ast is None:
        cell.v = H.NAME
        return
    v = H.final_value(H.evaluate(cell.ast, ctx))
    cell.v = 0 if v is None else v


def recalc(wb: Workbook, changed: Iterable[Tuple[str, int, int]] = (), *, everything: bool = False, out: Optional[Changes] = None) -> int:
    """Recalculate the formulas a change reaches (or all of them), in dependency order. Returns how many ran."""
    ctx = WbCtx(wb)
    by_id = ctx._by_id
    if everything:
        dirty = {(t.id, r, c) for t in wb.tabs for (r, c), cell in t.cells.items() if cell.ast is not None}
    else:
        ch = list(changed)
        dirty = {k for k in ch if (cl := by_id[k[0]].cells.get((k[1], k[2]))) is not None and cl.ast is not None} if ch else set()
        dirty |= {k for k in wb.dependents(ch) if k[0] in by_id and by_id[k[0]].cells.get((k[1], k[2])) is not None}
        dirty |= {k for k in wb.volatile if k[0] in by_id and by_id[k[0]].cells.get((k[1], k[2])) is not None}
    if not dirty:
        return 0
    # precedent formula cells inside the dirty set
    indeg: Dict[Tuple[str, int, int], int] = {}
    rev: Dict[Tuple[str, int, int], List[Tuple[str, int, int]]] = {}
    for fk in dirty:
        cell = by_id[fk[0]].cells[(fk[1], fk[2])]
        pres: Set[Tuple[str, int, int]] = set()
        for sheet, r0, c0, r1, c1 in H.refs(cell.ast):
            tid = wb._names.get(sheet.lower()) if sheet else fk[0]
            if tid is None:
                continue
            t = by_id[tid]
            r1c, c1c = min(r1, max(t.max_r, r0)), min(c1, max(t.max_c, c0))
            area = (r1c - r0 + 1) * (c1c - c0 + 1)
            if area <= len(dirty):
                for r in range(r0, r1c + 1):
                    for c in range(c0, c1c + 1):
                        if (tid, r, c) in dirty:
                            pres.add((tid, r, c))
            else:
                for k in dirty:
                    if k[0] == tid and r0 <= k[1] <= r1c and c0 <= k[2] <= c1c:
                        pres.add(k)
        indeg[fk] = len(pres)
        for p in pres:
            rev.setdefault(p, []).append(fk)
    ready = [k for k, d in indeg.items() if d == 0]
    done: Set[Tuple[str, int, int]] = set()
    order: List[Tuple[str, int, int]] = []
    while ready:
        k = ready.pop()
        order.append(k)
        done.add(k)
        for d in rev.get(k, ()):
            indeg[d] -= 1
            if indeg[d] == 0:
                ready.append(d)
    for k in (k for k in indeg if k not in done):
        # left over: a circular reference, or something that reads one (the error propagates through the reads)
        by_id[k[0]].cells[(k[1], k[2])].v = H.CIRC
        if out is not None:
            out.touch(k[0], k[1], k[2])
    for k in order:
        cell = by_id[k[0]].cells[(k[1], k[2])]
        before = cell.v
        _eval_cell(ctx, k[0], k[1], k[2], cell)
        if out is not None and (cell.v != before or type(cell.v) != type(before)):
            out.touch(k[0], k[1], k[2])
    return len(order) + len([k for k in indeg if k not in done])


# ---------------------------------------------------------------------------
# display
# ---------------------------------------------------------------------------
def cell_format(wb: Workbook, cell: Cell) -> str:
    nf = wb.styles[cell.sid].get("nf") if cell.sid < len(wb.styles) else None
    if nf:
        return nf
    if cell.ast is not None and cell.ast[0] == "call" and cell.ast[1] in DATE_FUNCS:
        return "yyyy-mm-dd hh:mm" if cell.ast[1] == "NOW" else "yyyy-mm-dd"
    return "General"


def display(wb: Workbook, cell: Cell) -> str:
    try:
        return F.format_value(cell.v, cell_format(wb, cell))
    except Exception:  # noqa: BLE001
        return str(cell.v)


def _jsonable(v: Any) -> Any:
    if isinstance(v, Err):
        return v.code
    return v


def _vtype(v: Any) -> str:
    if isinstance(v, Err):
        return "e"
    if isinstance(v, bool):
        return "b"
    if isinstance(v, (int, float)):
        return "n"
    return "s"


# ---------------------------------------------------------------------------
# conditional formats
# ---------------------------------------------------------------------------
def _cf_overlay(wb: Workbook, tab: Tab, rules: List[Dict[str, Any]], r: int, c: int, cell: Cell, stats: Dict[int, Any]) -> Optional[Dict[str, Any]]:
    out: Optional[Dict[str, Any]] = None
    for i, rule in enumerate(rules):
        rg = rule.get("rg")
        if not rg or not (rg[0] <= r <= rg[2] and rg[1] <= c <= rg[3]):
            continue
        kind = rule.get("rule")
        v = cell.v
        hit = False
        num = isinstance(v, (int, float)) and not isinstance(v, bool)
        try:
            if kind == "scale":
                if num:
                    st = stats.get(i)
                    if st is None:
                        xs = [x.v for rr in range(rg[0], min(rg[2], tab.max_r) + 1) for cc in range(rg[1], min(rg[3], tab.max_c) + 1)
                              if (x := tab.cells.get((rr, cc))) is not None and isinstance(x.v, (int, float)) and not isinstance(x.v, bool)]
                        st = stats[i] = (min(xs), max(xs)) if xs else (0, 0)
                    lo, hi = st
                    t = 0.0 if hi == lo else (v - lo) / (hi - lo)
                    cols = rule.get("scale") or ["#f8696b", "#63be7b"]
                    ov = {"bg": _lerp_color(cols, t)}
                    out = {**(out or {}), **ov}
                continue
            if kind in ("gt", "lt", "gte", "lte", "eq", "neq"):
                t = rule.get("value")
                if num and isinstance(t, (int, float)):
                    hit = {"gt": v > t, "lt": v < t, "gte": v >= t, "lte": v <= t, "eq": v == t, "neq": v != t}[kind]
                elif isinstance(t, str) and kind in ("eq", "neq"):
                    hit = (str(v).lower() == t.lower()) == (kind == "eq")
            elif kind == "between":
                hit = num and rule.get("value") <= v <= rule.get("value2")
            elif kind == "contains":
                hit = str(rule.get("value", "")).lower() in str(v if v is not None else "").lower()
            elif kind == "blank":
                hit = v is None or v == ""
            elif kind == "notblank":
                hit = not (v is None or v == "")
            elif kind in ("top", "bottom"):
                st = stats.get(i)
                if st is None:
                    xs = sorted((x.v for rr in range(rg[0], min(rg[2], tab.max_r) + 1) for cc in range(rg[1], min(rg[3], tab.max_c) + 1)
                                 if (x := tab.cells.get((rr, cc))) is not None and isinstance(x.v, (int, float)) and not isinstance(x.v, bool)), reverse=(kind == "top"))
                    st = stats[i] = xs[: max(1, int(rule.get("value") or 1))]
                hit = num and v in st
            elif kind == "duplicate":
                st = stats.get(i)
                if st is None:
                    seen: Dict[Any, int] = {}
                    for rr in range(rg[0], min(rg[2], tab.max_r) + 1):
                        for cc in range(rg[1], min(rg[3], tab.max_c) + 1):
                            x = tab.cells.get((rr, cc))
                            if x is not None and x.v is not None:
                                k = x.v.lower() if isinstance(x.v, str) else x.v
                                seen[k] = seen.get(k, 0) + 1
                    st = stats[i] = {k for k, n in seen.items() if n > 1}
                hit = (v.lower() if isinstance(v, str) else v) in st
            elif kind == "formula":
                ast = H.shift_relative(H.parse(rule["value"]), r - rg[0], c - rg[1])
                ctx = WbCtx(wb)
                ctx.cur_tab, ctx.cur_pos = tab.id, (r, c)
                hit = bool(H.scalar(H.evaluate(ast, ctx))) is True
        except (TypeError, ValueError, KeyError, H.FormulaSyntax):
            hit = False
        if hit:
            ov = {k: rule[k] for k in ("bg", "c", "b") if rule.get(k)}
            out = {**(out or {}), **ov}
    return out


def _lerp_color(cols: List[str], t: float) -> str:
    t = max(0.0, min(1.0, t))
    pts = [tuple(int(x[i:i + 2], 16) for i in (1, 3, 5)) for x in cols]
    seg = t * (len(pts) - 1)
    i = min(int(seg), len(pts) - 2)
    f = seg - i
    a, b = pts[i], pts[i + 1]
    return "#%02x%02x%02x" % tuple(round(a[k] + (b[k] - a[k]) * f) for k in range(3))


# ---------------------------------------------------------------------------
# structure: inserting and deleting rows / columns keeps every reference right
# ---------------------------------------------------------------------------
def _shift_span(lo: int, hi: int, at: int, count: int, limit: int) -> Optional[Tuple[int, int]]:
    if count > 0:
        nlo = lo + count if lo >= at else lo
        nhi = hi if hi >= limit else (hi + count if hi >= at else hi)
        return nlo, min(nhi, limit)
    k = -count
    if hi < at:
        return lo, hi
    if lo >= at + k:
        return lo - k, (hi if hi >= limit else hi - k)
    nlo = lo if lo < at else at
    nhi = at - 1 if hi < at + k else (hi if hi >= limit else hi - k)
    return (nlo, nhi) if nhi >= nlo else None


def _rewrite_formulas(wb: Workbook, fn_for_tab) -> None:
    """Rebuild every formula cell with ``fn_for_tab(own_tab, ref_node) -> node | None``."""
    for t in wb.tabs:
        for pos, cell in list(t.cells.items()):
            if cell.ast is None:
                continue
            def fn(node, _t=t):
                sheet = node[1]
                target = wb.tab(sheet) if sheet else _t
                return fn_for_tab(target, node)
            ast = H.transform_refs(cell.ast, fn)
            new = Cell("=" + H.unparse(ast), cell.sid)
            new.ast = ast
            t.cells[pos] = new


def structure_shift(wb: Workbook, tab: Tab, axis: str, at: int, count: int, ch: Changes) -> None:
    limit = MAX_ROWS if axis == "r" else MAX_COLS

    def refn(target: Optional[Tab], node):
        if target is None or target.id != tab.id:
            return node
        _, sh, r0, c0, r1, c1, fl = node
        if axis == "r":
            sp = _shift_span(r0, r1, at, count, limit)
            return None if sp is None else ("ref", sh, sp[0], c0, sp[1], c1, fl)
        sp = _shift_span(c0, c1, at, count, limit)
        return None if sp is None else ("ref", sh, r0, sp[0], r1, sp[1], fl)

    _rewrite_formulas(wb, refn)
    moved: Dict[Tuple[int, int], Cell] = {}
    for (r, c), cell in tab.cells.items():
        v = r if axis == "r" else c
        sp = _shift_span(v, v, at, count, limit)
        if sp is None:
            continue
        moved[(sp[0], c) if axis == "r" else (r, sp[0])] = cell
    tab.cells = moved
    tab.recompute_bounds()
    # sizes, hidden rows, formats, charts, filter
    size = tab.rows if axis == "r" else tab.cols
    new_size = {}
    for k, v in size.items():
        sp = _shift_span(k, k, at, count, limit)
        if sp:
            new_size[sp[0]] = v
    if axis == "r":
        tab.rows = new_size
        tab.hidden = {sp[0] for k in tab.hidden if (sp := _shift_span(k, k, at, count, limit))}
    else:
        tab.cols = new_size

    def rg(t4):
        r0, c0, r1, c1 = t4
        if axis == "r":
            sp = _shift_span(r0, r1, at, count, limit)
            return None if sp is None else (sp[0], c0, sp[1], c1)
        sp = _shift_span(c0, c1, at, count, limit)
        return None if sp is None else (r0, sp[0], r1, sp[1])
    cf = []
    for rule in tab.cf:
        nr = rg(tuple(rule["rg"]))
        if nr:
            rule = {**rule, "rg": list(nr), "range": H.range_text(*nr)}
            cf.append(rule)
    tab.cf = cf
    if tab.filter and tab.filter.get("rg"):
        nr = rg(tuple(tab.filter["rg"]))
        tab.filter = None if nr is None else {**tab.filter, "rg": list(nr), "range": H.range_text(*nr)}
    for chart in tab.charts:
        def fix(text):
            if not text:
                return text
            sheet, body = (text.rsplit("!", 1) if "!" in text else (None, text))
            if sheet and sheet.strip("'").lower() != tab.name.lower() or (not sheet and chart.get("tab") not in (None, tab.name)):
                return text
            pr = parse_range(body)
            nr = rg(pr) if pr else None
            return H.range_text(*nr) if nr else text
        chart["x"] = fix(chart.get("x"))
        for s in chart.get("series", []):
            s["ref"] = fix(s.get("ref"))
        a = parse_addr(chart.get("at") or "A1") or (1, 1)
        nr = rg((a[0], a[1], a[0], a[1]))
        if nr:
            chart["at"] = addr(nr[0], nr[1])
    wb.rebuild_graph()
    ch.touch_full(tab.id)
    for t in wb.tabs:
        ch.touch_full(t.id)
    ch.struct = True
    ch.all = True


def rename_tab_refs(wb: Workbook, tab: Tab, old: str, new: str) -> None:
    def refn(target: Optional[Tab], node):
        if target is tab and node[1] and node[1].lower() == old.lower():
            return ("ref", new, *node[2:])
        return node
    _rewrite_formulas(wb, refn)
    for t in wb.tabs:
        for chart in t.charts:
            for key in ("x",):
                chart[key] = _rename_in_ref(chart.get(key), old, new)
            for s in chart.get("series", []):
                s["ref"] = _rename_in_ref(s.get("ref"), old, new)
            if chart.get("tab", "").lower() == old.lower():
                chart["tab"] = new


def _rename_in_ref(text: Optional[str], old: str, new: str) -> Optional[str]:
    if not text or "!" not in text:
        return text
    sheet, body = text.rsplit("!", 1)
    return H._sheet_text(new) + "!" + body if sheet.strip("'").replace("''", "'").lower() == old.lower() else text


def delete_tab_refs(wb: Workbook, tab: Tab) -> None:
    def refn(target: Optional[Tab], node):
        return None if target is tab and node[1] else node
    _rewrite_formulas(wb, refn)


# ---------------------------------------------------------------------------
# ops
# ---------------------------------------------------------------------------
def _pick_tab(wb: Workbook, op: Dict[str, Any], view: Optional[Dict[str, Any]], key: str = "tab") -> Tab:
    ref = op.get(key)
    if ref not in (None, ""):
        t = wb.tab(ref)
        if t is None:
            raise SheetError("not_found", f"no tab {ref!r}; tabs: " + ", ".join(repr(x.name) for x in wb.tabs))
        return t
    if view and view.get("tab"):
        t = wb.tab(view["tab"])
        if t:
            return t
    return wb.tabs[0]


def _parse_target(op: Dict[str, Any], *keys: str) -> Tuple[Tab, Tuple[int, int, int, int]]:
    raise NotImplementedError


def _rng(text: Any, what: str = "range") -> Tuple[int, int, int, int]:
    pr = parse_range(str(text)) if text not in (None, "") else None
    if not pr:
        raise SheetError("bad_args", f"{what} must look like A1 or A1:C10, got {text!r}")
    return pr


def _split_sheet(text: str) -> Tuple[Optional[str], str]:
    if "!" in text:
        sh, body = text.rsplit("!", 1)
        return H._unquote_sheet(sh), body
    return None, text


def _clamp(tab: Tab, rg: Tuple[int, int, int, int]) -> Tuple[int, int, int, int]:
    """Whole-column / whole-row ranges stop at what the tab uses."""
    r0, c0, r1, c1 = rg
    if r1 >= MAX_ROWS:
        r1 = max(tab.max_r, r0)
    if c1 >= MAX_COLS:
        c1 = max(tab.max_c, c0)
    return r0, c0, r1, c1


def _col_arg(v: Any) -> int:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return int(v)
    s = str(v).strip()
    if s.isdigit():
        return int(s)
    if re.fullmatch(r"[A-Za-z]{1,3}", s):
        return col_number(s)
    raise SheetError("bad_args", f"column must be a letter or number, got {v!r}")


def _grid(value: Any) -> List[List[Any]]:
    if isinstance(value, list):
        if value and all(isinstance(r, list) for r in value):
            return value
        return [value]
    return [[value]]


def _style_patch(op: Dict[str, Any]) -> Dict[str, Any]:
    p: Dict[str, Any] = {}
    for src, dst in (("bold", "b"), ("italic", "i")):
        if src in op:
            p[dst] = 1 if op[src] else None
    if "color" in op:
        p["c"] = _norm_color(op["color"])
    for k in ("fill", "background", "bg"):
        if k in op:
            p["bg"] = _norm_color(op[k])
    if "align" in op:
        a = str(op["align"] or "").lower()
        if a not in ("", "left", "center", "right", "l", "c", "r"):
            raise SheetError("bad_args", "align is left, center or right")
        p["al"] = a[:1] or None
    if "wrap" in op:
        p["wrap"] = 1 if op["wrap"] else None
    if "border" in op:
        p["bd"] = 1 if op["border"] else None
    if "format" in op or "decimals" in op or "number_format" in op:
        fmt = op.get("format", op.get("number_format"))
        p["nf"] = None if fmt in (None, "", "general", "General") and "decimals" not in op else F.format_code(fmt if fmt is not None else "number", op.get("decimals"), op.get("symbol") or op.get("currency"))
        if p["nf"] == "General":
            p["nf"] = None
    return p


def _cells_in(tab: Tab, rg: Tuple[int, int, int, int]) -> List[Tuple[int, int]]:
    r0, c0, r1, c1 = rg
    r1, c1 = min(r1, max(tab.max_r, r0)), min(c1, max(tab.max_c, c0))
    area = (r1 - r0 + 1) * (c1 - c0 + 1)
    if area <= len(tab.cells):
        return [(r, c) for r in range(r0, r1 + 1) for c in range(c0, c1 + 1) if (r, c) in tab.cells]
    return [(r, c) for (r, c) in tab.cells if r0 <= r <= r1 and c0 <= c <= c1]


def _cursor(ch: Changes, tab: Tab, rg: Tuple[int, int, int, int]) -> None:
    ch.cursor.append({"tab": tab.id, "r": rg[0], "c": rg[1]})
    if (rg[2], rg[3]) != (rg[0], rg[1]) and rg[2] < MAX_ROWS and rg[3] < MAX_COLS:
        ch.cursor.append({"tab": tab.id, "r": rg[2], "c": rg[3]})


def _sort_key(v: Any):
    if v is None or v == "":
        return (4, 0)
    if isinstance(v, Err):
        return (3, v.code)
    if isinstance(v, bool):
        return (2, int(v))
    if isinstance(v, (int, float)):
        return (0, v)
    return (1, str(v).lower())


def _header_index(tab: Tab, rg: Tuple[int, int, int, int], by: Any) -> int:
    if isinstance(by, (int, float)) and not isinstance(by, bool):
        return int(by)
    s = str(by).strip()
    low = s.lower()
    for c in range(rg[1], rg[3] + 1):                 # a header text wins over a column letter ("v" may be a header)
        cell = tab.cells.get((rg[0], c))
        if cell is not None and isinstance(cell.v, str) and cell.v.strip().lower() == low:
            return c
    if re.fullmatch(r"[A-Za-z]{1,3}", s):
        return col_number(s)
    raise SheetError("bad_args", f"no column {by!r} (use a letter such as B or a header text from row {rg[0]})")


def _op_set(wb: Workbook, op: Dict[str, Any], view, ch: Changes) -> str:
    tab = _pick_tab(wb, op, view)
    parse = op.get("parse", True) is not False
    n = 0
    if isinstance(op.get("cells"), dict):
        for a, v in op["cells"].items():
            rc = parse_addr(str(a))
            if not rc:
                raise SheetError("bad_args", f"bad cell address {a!r}")
            old = tab.cells.get(rc)
            put(wb, tab, rc[0], rc[1], make_cell(wb, tab, rc[0], rc[1], v, parse=parse, sid=old.sid if old else 0), ch)
            n += 1
        if op["cells"]:
            first = parse_addr(str(next(iter(op["cells"]))))
            _cursor(ch, tab, (first[0], first[1], first[0], first[1]))
        return f"set {n} cells on {tab.name!r}"
    if "range" in op and "values" not in op:
        r0, c0, r1, c1 = _clamp(tab, _rng(op["range"]))
        if (r1 - r0 + 1) * (c1 - c0 + 1) > 500_000:
            raise SheetError("too_big", "that range is too large to fill in one op")
        for r in range(r0, r1 + 1):
            for c in range(c0, c1 + 1):
                old = tab.cells.get((r, c))
                put(wb, tab, r, c, make_cell(wb, tab, r, c, op.get("value"), parse=parse, sid=old.sid if old else 0), ch)
                n += 1
        _cursor(ch, tab, (r0, c0, r1, c1))
        return f"set {H.range_text(r0, c0, r1, c1)} on {tab.name!r}"
    at = op.get("at") or op.get("start") or (op.get("range") and str(op["range"]).split(":")[0]) or "A1"
    rc = parse_addr(str(at))
    if not rc:
        raise SheetError("bad_args", f"`at` must be a cell such as A1, got {at!r}")
    grid = _grid(op.get("values", op.get("value")))
    if op.get("transpose"):
        grid = [list(row) for row in zip(*[r + [None] * (max(map(len, grid)) - len(r)) for r in grid])]
    for i, row in enumerate(grid):
        if not isinstance(row, list):
            row = [row]
        for j, v in enumerate(row):
            r, c = rc[0] + i, rc[1] + j
            old = tab.cells.get((r, c))
            if v is None and old is None:
                continue
            put(wb, tab, r, c, make_cell(wb, tab, r, c, v, parse=parse, sid=old.sid if old else 0), ch)
            n += 1
    end = (rc[0] + max(len(grid) - 1, 0), rc[1] + max(max((len(r) if isinstance(r, list) else 1) for r in grid) - 1, 0))
    _cursor(ch, tab, (rc[0], rc[1], end[0], end[1]))
    return f"wrote {n} cells at {H.range_text(rc[0], rc[1], end[0], end[1])} on {tab.name!r}"


def _op_fill(wb: Workbook, op: Dict[str, Any], view, ch: Changes) -> str:
    tab = _pick_tab(wb, op, view)
    r0, c0, r1, c1 = _clamp(tab, _rng(op.get("range")))
    src = parse_addr(str(op["from"])) if op.get("from") else (r0, c0)
    if not src:
        raise SheetError("bad_args", "`from` must be a cell")
    base = tab.cells.get(src)
    if base is None:
        raise SheetError("bad_args", f"the source cell {addr(*src)} is empty")
    n = 0
    series = op.get("series") and isinstance(base.raw, (int, float)) and not isinstance(base.raw, bool)
    step = op.get("step", 1)
    for r in range(r0, r1 + 1):
        for c in range(c0, c1 + 1):
            if (r, c) == src:
                continue
            if base.ast is not None:
                ast = H.shift_relative(base.ast, r - src[0], c - src[1])
                if ast[0] == "err":
                    cell = Cell(None, base.sid)
                else:
                    cell = Cell("=" + H.unparse(ast), base.sid)
                    cell.ast = ast
            elif series:
                k = (r - src[0]) + (c - src[1])
                cell = Cell(base.raw + step * k, base.sid)
            else:
                cell = Cell(base.raw, base.sid)
            put(wb, tab, r, c, cell, ch)
            n += 1
    _cursor(ch, tab, (r0, c0, r1, c1))
    return f"filled {H.range_text(r0, c0, r1, c1)} from {addr(*src)} on {tab.name!r}"


def _op_copy(wb: Workbook, op: Dict[str, Any], view, ch: Changes) -> str:
    stab = _pick_tab(wb, op, view)
    r0, c0, r1, c1 = _clamp(stab, _rng(op.get("from"), "from"))
    dtab = _pick_tab(wb, {"tab": op.get("to_tab")}, view) if op.get("to_tab") else stab
    dest = parse_addr(str(op.get("to"))) if op.get("to") else None
    if not dest:
        raise SheetError("bad_args", "`to` must be a cell such as E1")
    block = {(r, c): stab.cells.get((r, c)) for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)}
    moved = bool(op.get("move"))
    for (r, c), cell in block.items():
        nr, nc = dest[0] + r - r0, dest[1] + c - c0
        if cell is None:
            put(wb, dtab, nr, nc, None, ch)
            continue
        if cell.ast is not None:
            ast = cell.ast if moved else H.shift_relative(cell.ast, nr - r, nc - c)
            new = Cell(None, cell.sid) if ast[0] == "err" else Cell("=" + H.unparse(ast), cell.sid)
            if ast[0] != "err":
                new.ast = ast
        else:
            new = Cell(cell.raw, cell.sid)
        put(wb, dtab, nr, nc, new, ch)
    if moved:
        for (r, c) in block:
            if not (dest[0] <= r < dest[0] + (r1 - r0 + 1) and dest[1] <= c < dest[1] + (c1 - c0 + 1) and dtab is stab):
                put(wb, stab, r, c, None, ch)
    _cursor(ch, dtab, (dest[0], dest[1], dest[0] + r1 - r0, dest[1] + c1 - c0))
    return f"{'moved' if moved else 'copied'} {H.range_text(r0, c0, r1, c1)} to {addr(*dest)} on {dtab.name!r}"


def _op_clear(wb: Workbook, op: Dict[str, Any], view, ch: Changes) -> str:
    tab = _pick_tab(wb, op, view)
    rg = _clamp(tab, _rng(op.get("range")))
    what = str(op.get("what") or "all").lower()
    for (r, c) in _cells_in(tab, rg):
        cell = tab.cells[(r, c)]
        if what == "formats":
            new = Cell(cell.raw, 0)
            new.ast = cell.ast
            put(wb, tab, r, c, new, ch)
        elif what == "values" and cell.sid:
            put(wb, tab, r, c, Cell(None, cell.sid), ch)
        else:
            put(wb, tab, r, c, None, ch)
    fix_bounds(tab)
    _cursor(ch, tab, rg)
    return f"cleared {what} in {H.range_text(*rg)} on {tab.name!r}"


def _op_format(wb: Workbook, op: Dict[str, Any], view, ch: Changes) -> str:
    tab = _pick_tab(wb, op, view)
    rg = _clamp(tab, _rng(op.get("range")))
    patch = _style_patch(op)
    if op.get("clear"):
        patch = {k: None for k in STYLE_KEYS}
    if not patch:
        raise SheetError("bad_args", "format needs bold, italic, color, fill, align, wrap, border, format or decimals")
    r0, c0, r1, c1 = rg
    if (r1 - r0 + 1) * (c1 - c0 + 1) > 1_000_000:
        raise SheetError("too_big", "that range is too large to format in one op")
    whole_col = rg[2] >= tab.max_r and rg[0] == 1 and (r1 - r0) > 200
    for r in range(r0, r1 + 1):
        for c in range(c0, c1 + 1):
            old = tab.cells.get((r, c))
            sid = wb.merged_style(old.sid if old else 0, patch)
            if old is None:
                if sid:
                    put(wb, tab, r, c, Cell(None, sid), ch)
                continue
            new = Cell(old.raw, sid)
            new.ast = old.ast
            put(wb, tab, r, c, new, ch)
    _cursor(ch, tab, rg)
    return f"formatted {H.range_text(*rg)} on {tab.name!r}"


def _op_size(wb: Workbook, op: Dict[str, Any], view, ch: Changes) -> str:
    tab = _pick_tab(wb, op, view)
    n = 0
    for k, v in (op.get("cols") or {}).items():
        c = _col_arg(k)
        if v in ("auto", "fit"):
            w = _auto_width(tab, c)
        else:
            w = DEFAULT_COL_W if v in (None, "", "default") else int(v)
        tab.cols[c] = max(24, min(800, w))
        n += 1
    for k, v in (op.get("rows") or {}).items():
        r = int(k)
        tab.rows[r] = max(14, min(600, int(v)))
        n += 1
    ch.struct = True
    ch.touch_full(tab.id)
    return f"resized {n} columns/rows on {tab.name!r}"


def _auto_width(tab: Tab, c: int) -> int:
    best = 6
    for (r, cc), cell in tab.cells.items():
        if cc == c and r <= 2000 and cell.v is not None:
            best = max(best, min(60, len(F.general(cell.v) if not isinstance(cell.v, str) else cell.v)))
    return int(best * 7.5 + 16)


def _op_freeze(wb: Workbook, op: Dict[str, Any], view, ch: Changes) -> str:
    tab = _pick_tab(wb, op, view)
    tab.freeze = {"rows": max(0, min(50, int(op.get("rows", 0) or 0))), "cols": max(0, min(20, int(op.get("cols", 0) or 0)))}
    ch.struct = True
    ch.touch_full(tab.id)
    return f"froze {tab.freeze['rows']} rows and {tab.freeze['cols']} columns on {tab.name!r}"


def _unique_name(wb: Workbook, name: str, skip: Optional[Tab] = None) -> str:
    base = re.sub(r"[\[\]:*?/\\]", " ", str(name or "Sheet")).strip()[:31] or "Sheet"
    cand, i = base, 2
    while any(t.name.lower() == cand.lower() and t is not skip for t in wb.tabs):
        cand = f"{base[:28]} {i}"
        i += 1
    return cand


def _op_add_tab(wb: Workbook, op: Dict[str, Any], view, ch: Changes) -> str:
    if len(wb.tabs) >= MAX_TABS:
        raise SheetError("too_big", f"at most {MAX_TABS} tabs")
    t = wb.new_tab(_unique_name(wb, op.get("name") or f"Sheet{len(wb.tabs) + 1}"), op.get("at"))
    ch.struct = True
    ch.touch_full(t.id)
    ch.cursor.append({"tab": t.id, "r": 1, "c": 1})
    return f"added tab {t.name!r}"


def _op_rename_tab(wb: Workbook, op: Dict[str, Any], view, ch: Changes) -> str:
    t = _pick_tab(wb, op, view)
    new = _unique_name(wb, op.get("name"), skip=t)
    old = t.name
    rename_tab_refs(wb, t, old, new)
    t.name = new
    wb.reindex_names()
    wb.rebuild_graph()
    ch.struct = True
    ch.all = True
    return f"renamed tab {old!r} to {new!r}"


def _op_delete_tab(wb: Workbook, op: Dict[str, Any], view, ch: Changes) -> str:
    t = _pick_tab(wb, op, view)
    if len(wb.tabs) == 1:
        raise SheetError("bad_args", "a workbook needs at least one tab")
    delete_tab_refs(wb, t)
    wb.tabs.remove(t)
    wb.reindex_names()
    wb.rebuild_graph()
    for x in wb.tabs:
        ch.touch_full(x.id)
    ch.struct = True
    ch.all = True
    return f"deleted tab {t.name!r}"


def _op_move_tab(wb: Workbook, op: Dict[str, Any], view, ch: Changes) -> str:
    t = _pick_tab(wb, op, view)
    wb.tabs.remove(t)
    wb.tabs.insert(max(0, min(len(wb.tabs), int(op.get("to", 0)))), t)
    ch.struct = True
    return f"moved tab {t.name!r}"


def _op_insdel(kind: str):
    def run(wb: Workbook, op: Dict[str, Any], view, ch: Changes) -> str:
        tab = _pick_tab(wb, op, view)
        axis = "r" if kind.endswith("rows") else "c"
        at = int(op["at"]) if axis == "r" and str(op.get("at", "")).isdigit() else (_col_arg(op.get("at")) if op.get("at") not in (None, "") else 0)
        if at < 1:
            raise SheetError("bad_args", "`at` is the row number or column letter where it happens")
        count = max(1, min(10_000, int(op.get("count", 1) or 1)))
        structure_shift(wb, tab, axis, at, count if kind.startswith("insert") else -count, ch)
        ch.cursor.append({"tab": tab.id, "r": at if axis == "r" else 1, "c": at if axis == "c" else 1})
        what = "row" if axis == "r" else "column"
        return f"{'inserted' if kind.startswith('insert') else 'deleted'} {count} {what}(s) at {at if axis == 'r' else col_letters(at)} on {tab.name!r}"
    return run


def _op_sort(wb: Workbook, op: Dict[str, Any], view, ch: Changes) -> str:
    tab = _pick_tab(wb, op, view)
    rg = _clamp(tab, _rng(op.get("range")))
    r0, c0, r1, c1 = rg
    header = bool(op.get("header"))
    keys = op.get("by") if isinstance(op.get("by"), list) else [op.get("by", c0)]
    desc = op.get("desc") if isinstance(op.get("desc"), list) else [bool(op.get("desc"))] * len(keys)
    cols = [_header_index(tab, rg, k) for k in keys]
    start = r0 + 1 if header else r0
    rows = list(range(start, r1 + 1))

    def val(r, c):
        cell = tab.cells.get((r, c))
        return None if cell is None else cell.v
    order = rows[:]
    for col, d in reversed(list(zip(cols, desc))):
        present = [r for r in order if val(r, col) not in (None, "")]
        blanks = [r for r in order if val(r, col) in (None, "")]
        present.sort(key=lambda r: _sort_key(val(r, col)), reverse=bool(d))
        order = present + blanks
    new_cells: Dict[Tuple[int, int], Optional[Cell]] = {}
    for dst, src in zip(rows, order):
        for c in range(c0, c1 + 1):
            cell = tab.cells.get((src, c))
            if cell is not None and cell.ast is not None and src != dst:
                ast = H.shift_relative(cell.ast, dst - src, 0)
                new = Cell(None, cell.sid) if ast[0] == "err" else Cell("=" + H.unparse(ast), cell.sid)
                if ast[0] != "err":
                    new.ast = ast
                cell = new
            new_cells[(dst, c)] = cell
    for (r, c), cell in new_cells.items():
        put(wb, tab, r, c, cell, ch)
    _cursor(ch, tab, rg)
    return f"sorted {H.range_text(*rg)} by {', '.join(col_letters(c) for c in cols)} on {tab.name!r}"


def apply_filter(tab: Tab) -> None:
    tab.hidden = set()
    f = tab.filter
    if not f:
        return
    r0, c0, r1, c1 = f["rg"]
    col = f["col"]
    start = r0 + 1
    test = None
    if f.get("values") is not None:
        allowed = {str(v).lower() for v in f["values"]}
        test = lambda v: ("" if v is None else str(H.F.general(v) if not isinstance(v, str) else v)).lower() in allowed  # noqa: E731
    elif f.get("where") is not None:
        test = H.make_criterion(f["where"])
    if test is None:
        return
    for r in range(start, min(r1, max(tab.max_r, r0)) + 1):
        cell = tab.cells.get((r, col))
        if not test(None if cell is None else cell.v):
            tab.hidden.add(r)


def _op_filter(wb: Workbook, op: Dict[str, Any], view, ch: Changes) -> str:
    tab = _pick_tab(wb, op, view)
    if op.get("clear"):
        tab.filter = None
        apply_filter(tab)
        ch.struct = True
        ch.touch_full(tab.id)
        return f"cleared the filter on {tab.name!r}"
    rg = _clamp(tab, _rng(op.get("range") or (tab.filter or {}).get("range")))
    col = _header_index(tab, rg, op.get("column", rg[1]))
    if "values" not in op and "where" not in op:
        raise SheetError("bad_args", "filter needs `where` (such as '>10' or 'Madrid') or `values` [..]")
    tab.filter = {"range": H.range_text(*rg), "rg": list(rg), "col": col, "values": op.get("values"), "where": op.get("where")}
    apply_filter(tab)
    ch.struct = True
    ch.touch_full(tab.id)
    return f"filtered {H.range_text(*rg)} by column {col_letters(col)} on {tab.name!r}: {len(tab.hidden)} rows hidden"


def _op_cond(wb: Workbook, op: Dict[str, Any], view, ch: Changes) -> str:
    tab = _pick_tab(wb, op, view)
    if op.get("clear"):
        rg = _clamp(tab, _rng(op["range"])) if op.get("range") else None
        tab.cf = [r for r in tab.cf if rg is None or tuple(r["rg"]) == rg]
        ch.touch_full(tab.id)
        ch.struct = True
        return f"cleared conditional formats on {tab.name!r}"
    rg = _clamp(tab, _rng(op.get("range")))
    rule = str(op.get("rule") or "").lower()
    if rule not in ("gt", "lt", "gte", "lte", "eq", "neq", "between", "contains", "blank", "notblank", "top", "bottom", "duplicate", "scale", "formula"):
        raise SheetError("bad_args", "rule is gt, lt, gte, lte, eq, neq, between, contains, blank, notblank, top, bottom, duplicate, scale or formula")
    entry: Dict[str, Any] = {"id": op.get("id") or new_id()[:8], "range": H.range_text(*rg), "rg": list(rg), "rule": rule}
    for k in ("value", "value2"):
        if k in op:
            entry[k] = op[k]
    if rule == "formula":
        try:
            H.parse(str(entry.get("value", "")))
        except H.FormulaSyntax as exc:
            raise SheetError("bad_formula", f"conditional formula: {exc}") from None
    fill = op.get("fill", op.get("bg"))
    if fill:
        entry["bg"] = _norm_color(fill)
    if op.get("color"):
        entry["c"] = _norm_color(op["color"])
    if op.get("bold"):
        entry["b"] = 1
    if rule == "scale":
        cols = [_norm_color(c) for c in (op.get("scale") or ["red", "green"])]
        entry["scale"] = cols[:3] if len(cols) >= 2 else ["#f8696b", "#63be7b"]
    elif not (entry.get("bg") or entry.get("c") or entry.get("b")):
        entry["bg"] = COLORS["lightred"]
    tab.cf = [r for r in tab.cf if r["id"] != entry["id"]] + [entry]
    ch.touch_full(tab.id)
    ch.struct = True
    _cursor(ch, tab, rg)
    return f"conditional format {rule} on {H.range_text(*rg)} on {tab.name!r}"


def _op_chart(wb: Workbook, op: Dict[str, Any], view, ch: Changes) -> str:
    tab = _pick_tab(wb, op, view)
    if op.get("delete") or op.get("op") == "chart_delete":
        before = len(tab.charts)
        tab.charts = [c for c in tab.charts if c["id"] != op.get("id")]
        ch.struct = True
        return f"deleted chart ({before - len(tab.charts)})"
    kind = str(op.get("type") or "bar").lower()
    if kind not in ("line", "bar", "column", "pie", "scatter", "area"):
        raise SheetError("bad_args", "chart type is line, bar, pie, scatter or area")
    kind = "bar" if kind == "column" else kind
    src_tab = tab
    x = op.get("x")
    series: List[Dict[str, Any]] = []
    if op.get("range"):
        sheet, body = _split_sheet(str(op["range"]))
        if sheet:
            src_tab = _pick_tab(wb, {"tab": sheet}, view)
        r0, c0, r1, c1 = _clamp(src_tab, _rng(body))
        head = [src_tab.cells.get((r0, c)) for c in range(c0, c1 + 1)]
        has_header = op.get("header", all(h is not None and isinstance(h.v, str) for h in head[: max(1, len(head))]))
        first_data = r0 + 1 if has_header else r0
        first = src_tab.cells.get((first_data, c0))
        cat_col = isinstance(first.v, str) if first is not None else False
        if c1 > c0 or kind == "pie":
            if cat_col or c1 > c0 and kind != "scatter":
                x = H.range_text(first_data, c0, r1, c0)
                vcols = range(c0 + 1, c1 + 1)
            else:
                vcols = range(c0, c1 + 1)
            if kind == "scatter" and not x:
                x = H.range_text(first_data, c0, r1, c0)
                vcols = range(c0 + 1, c1 + 1)
        else:
            vcols = range(c0, c1 + 1)
        for c in vcols:
            h = src_tab.cells.get((r0, c)) if has_header else None
            series.append({"name": str(h.v) if h is not None and h.v is not None else f"Series {len(series) + 1}",
                           "ref": H.range_text(first_data, c, r1, c)})
    for s in op.get("series") or []:
        if isinstance(s, str):
            sheet, body = _split_sheet(s)
            pr = _rng(body)
            nm = None
            hdr = (sheet and wb.tab(sheet) or src_tab).cells.get((pr[0] - 1, pr[1])) if pr[0] > 1 else None
            if hdr is not None and isinstance(hdr.v, str):
                nm = hdr.v
            series.append({"name": nm or f"Series {len(series) + 1}", "ref": s})
        elif isinstance(s, dict) and s.get("ref") or isinstance(s, dict) and s.get("values"):
            series.append({"name": str(s.get("name") or f"Series {len(series) + 1}"), "ref": s.get("ref") or s.get("values")})
    if not series:
        raise SheetError("bad_args", "chart needs `range` (a block with a header row) or `series`")
    shifted = False
    for s in series:                         # a series given as B1:B7 with its header in the first cell: the header is the name, not data
        if not isinstance(s.get("ref"), str):
            continue
        sheet, body = _split_sheet(s["ref"])
        pr = _rng(body, "series ref")
        t_src = (wb.tab(sheet) if sheet else None) or src_tab
        top = t_src.cells.get((pr[0], pr[1]))
        below = t_src.cells.get((pr[0] + 1, pr[1])) if pr[2] > pr[0] else None
        if top is not None and isinstance(top.v, str) and top.v != "" and below is not None and isinstance(below.v, (int, float)) and not isinstance(below.v, bool):
            if s["name"].startswith("Series ") or not s["name"]:
                s["name"] = top.v
            s["ref"] = (sheet + "!" if sheet else "") + H.range_text(pr[0] + 1, pr[1], pr[2], pr[3])
            shifted = True
    if shifted and isinstance(x, str):
        xs, xb = _split_sheet(x)
        xr = _rng(xb, "x range")
        xt = (wb.tab(xs) if xs else None) or src_tab
        xtop = xt.cells.get((xr[0], xr[1]))
        if xtop is not None and isinstance(xtop.v, str) and xr[2] > xr[0]:
            x = (xs + "!" if xs else "") + H.range_text(xr[0] + 1, xr[1], xr[2], xr[3])
    for s in series:
        sheet, body = _split_sheet(str(s["ref"]))
        _rng(body, "series ref")
    entry = {"id": op.get("id") or new_id()[:8], "type": kind, "title": str(op.get("title") or "")[:120],
             "x": x, "series": series[:12], "at": str(op.get("at") or _free_anchor(tab)), "w": int(op.get("w", 520)), "h": int(op.get("h", 320)),
             "tab": src_tab.name if src_tab is not tab else None}
    tab.charts = [c for c in tab.charts if c["id"] != entry["id"]] + [entry]
    ch.struct = True
    ch.touch_full(tab.id)
    a = parse_addr(entry["at"]) or (1, 1)
    ch.cursor.append({"tab": tab.id, "r": a[0], "c": a[1]})
    return f"chart {kind} {entry['title']!r} added on {tab.name!r} at {entry['at']}"


def _free_anchor(tab: Tab) -> str:
    return addr(2, max(tab.max_c, 1) + 2)


def _op_csv(wb: Workbook, op: Dict[str, Any], view, ch: Changes) -> str:
    tab = _pick_tab(wb, op, view)
    text = str(op.get("text") or "")
    delim = str(op.get("delimiter") or (";" if text.count(";") > text.count(",") else "\t" if text.count("\t") > text.count(",") else ","))
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    return _op_set(wb, {**op, "values": rows, "tab": tab.id}, view, ch).replace("wrote", "pasted CSV:")


OPS = {"set": _op_set, "fill": _op_fill, "copy": _op_copy, "move": lambda wb, op, v, ch: _op_copy(wb, {**op, "move": True}, v, ch),
       "clear": _op_clear, "format": _op_format, "width": _op_size, "height": _op_size, "size": _op_size, "freeze": _op_freeze,
       "add_tab": _op_add_tab, "rename_tab": _op_rename_tab, "delete_tab": _op_delete_tab, "move_tab": _op_move_tab,
       "insert_rows": _op_insdel("insert_rows"), "delete_rows": _op_insdel("delete_rows"),
       "insert_cols": _op_insdel("insert_cols"), "delete_cols": _op_insdel("delete_cols"),
       "sort": _op_sort, "filter": _op_filter, "cond": _op_cond, "chart": _op_chart, "chart_delete": lambda wb, op, v, ch: _op_chart(wb, {**op, "delete": True}, v, ch),
       "csv": _op_csv}
OPS["insert_columns"], OPS["delete_columns"] = OPS["insert_cols"], OPS["delete_cols"]


def apply_ops(wb: Workbook, ops: Any, view: Optional[Dict[str, Any]] = None) -> Tuple[List[str], Changes]:
    if not isinstance(ops, list) or not ops:
        raise SheetError("bad_args", "`ops` must be a non-empty list")
    if len(ops) > MAX_OPS:
        raise SheetError("too_big", f"at most {MAX_OPS} ops per call")
    ch = Changes()
    results: List[str] = []
    for i, op in enumerate(ops):
        if not isinstance(op, dict):
            err = SheetError("bad_args", f"op {i + 1} is not an object")
            err.changes, err.results = ch, results  # type: ignore[attr-defined]
            raise err
        name = str(op.get("op") or "")
        fnc = OPS.get(name)
        try:
            if fnc is None:
                raise SheetError("bad_args", f"unknown op {name!r}; ops: " + ", ".join(sorted(OPS)))
            results.append(fnc(wb, op, view, ch))
        except SheetError as exc:
            exc.message = f"op {i + 1} ({name}): {exc.message}"
            exc.args = (exc.message,)
            fix_all(wb)
            _settle(wb, ch)
            exc.changes, exc.results = ch, results  # type: ignore[attr-defined]
            raise
    fix_all(wb)
    _settle(wb, ch)
    for t in wb.tabs:
        if t.filter:
            apply_filter(t)
            if t.id not in ch.full:
                ch.touch_full(t.id)
    return results, ch


def fix_all(wb: Workbook) -> None:
    for t in wb.tabs:
        fix_bounds(t)


def _settle(wb: Workbook, ch: Changes) -> None:
    """Recalculate what the ops reached (everything after a structure change)."""
    if ch.all:
        recalc(wb, everything=True, out=ch)
    else:
        recalc(wb, list(ch.dirty), out=ch)


# ---------------------------------------------------------------------------
# what the window and the model read
# ---------------------------------------------------------------------------
def style_of(wb: Workbook, sid: int) -> Dict[str, Any]:
    return wb.styles[sid] if sid < len(wb.styles) else {}


def window(wb: Workbook, tab: Tab, r0: int, c0: int, r1: int, c1: int) -> Dict[str, Any]:
    """Cells of a viewport for the grid: ``[r, c, value, shown, formula|None, styleId, cfOverlay|None]`` per existing cell."""
    r1, c1 = min(r1, r0 + 600), min(c1, c0 + 80)
    stats: Dict[int, Any] = {}
    out: List[List[Any]] = []
    used: Set[int] = set()
    area = (r1 - r0 + 1) * (c1 - c0 + 1)
    keys = ([(r, c) for r in range(r0, r1 + 1) for c in range(c0, c1 + 1) if (r, c) in tab.cells] if area <= len(tab.cells)
            else [(r, c) for (r, c) in tab.cells if r0 <= r <= r1 and c0 <= c <= c1])
    for (r, c) in keys:
        cell = tab.cells[(r, c)]
        ov = _cf_overlay(wb, tab, tab.cf, r, c, cell, stats) if tab.cf else None
        out.append([r, c, _jsonable(cell.v), display(wb, cell), cell.raw if cell.ast is not None else None, cell.sid, ov])
        used.add(cell.sid)
    return {"cells": out, "styles": {str(i): style_of(wb, i) for i in used if i}}


def chart_data(wb: Workbook, tab: Tab, chart: Dict[str, Any]) -> Dict[str, Any]:
    def values(ref: Optional[str], numeric: bool) -> List[Any]:
        if not ref:
            return []
        sheet, body = _split_sheet(str(ref))
        t = wb.tab(sheet or chart.get("tab")) or tab
        pr = parse_range(body)
        if not pr:
            return []
        r0, c0, r1, c1 = _clamp(t, pr)
        out = []
        for r in range(r0, min(r1, r0 + 2000) + 1):
            for c in range(c0, c1 + 1):
                cell = t.cells.get((r, c))
                v = None if cell is None else cell.v
                out.append((v if isinstance(v, (int, float)) and not isinstance(v, bool) else None) if numeric else ("" if v is None else display(wb, cell)))
        return out
    cats = values(chart.get("x"), chart.get("type") == "scatter")
    series = [{"name": s.get("name"), "values": values(s.get("ref"), True)} for s in chart.get("series", [])]
    if not cats and series:
        cats = [str(i + 1) for i in range(len(series[0]["values"]))]
    return {"id": chart["id"], "type": chart["type"], "title": chart.get("title", ""), "at": chart.get("at"), "w": chart.get("w", 520), "h": chart.get("h", 320),
            "categories": cats, "series": series}


def tab_meta(wb: Workbook, tab: Tab) -> Dict[str, Any]:
    return {"id": tab.id, "name": tab.name, "used": [tab.max_r, tab.max_c], "cols": {str(k): v for k, v in tab.cols.items()},
            "rows": {str(k): v for k, v in tab.rows.items()}, "freeze": tab.freeze, "filter": tab.filter, "hidden": sorted(tab.hidden),
            "cf": tab.cf, "charts": [chart_data(wb, tab, c) for c in tab.charts]}


def structure(wb: Workbook) -> Dict[str, Any]:
    return {"tabs": [{"id": t.id, "name": t.name, "used": [t.max_r, t.max_c]} for t in wb.tabs]}


def _display_text(wb: Workbook, tab: Tab, r: int, c: int) -> str:
    cell = tab.cells.get((r, c))
    return "" if cell is None or cell.v is None else display(wb, cell)


def _header_row(wb: Workbook, tab: Tab) -> Optional[int]:
    """Row 1 is a header when it is text and something sits under it."""
    if tab.max_r < 2:
        return None
    cells = [tab.cells.get((1, c)) for c in range(1, tab.max_c + 1)]
    filled = [x for x in cells if x is not None and x.v is not None and x.ast is None]
    if filled and all(isinstance(x.v, str) for x in filled):
        return 1
    return None


def _col_summary(wb: Workbook, tab: Tab, c: int, first: int) -> str:
    vals = [tab.cells[(r, c)].v for r in range(first, min(tab.max_r, first + 500) + 1) if (r, c) in tab.cells and tab.cells[(r, c)].v is not None]
    if not vals:
        return "empty"
    nums = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if len(nums) >= 0.8 * len(vals):
        sample = next((tab.cells[(r, c)] for r in range(first, tab.max_r + 1) if (r, c) in tab.cells and tab.cells[(r, c)].v is not None), None)
        kind = "date" if sample is not None and F.is_date_format(cell_format(wb, sample)) else "number"
        return f"{kind} {F.general(min(nums))}..{F.general(max(nums))}" if kind == "number" else f"date x{len(nums)}"
    d = sorted({str(v) for v in vals})
    return f"text, {len(d)} distinct" + (": " + ", ".join(repr(x[:18]) for x in d[:4]) if len(d) <= 12 else "")


def read_text(wb: Workbook, meta: Dict[str, Any], *, tab: Optional[str] = None, rng: Optional[str] = None,
              state: Optional[Dict[str, Any]] = None, max_cells: int = 400) -> str:
    lines = [f"Sheet {meta['title']!r} (id {meta['id']}, rev {meta['rev']}). Tabs: " +
             ", ".join(f"{t.name!r} {('A1:' + addr(max(t.max_r, 1), max(t.max_c, 1))) if t.cells else '(empty)'}" for t in wb.tabs)]
    sel_text = ""
    if state:
        st = wb.tab(state.get("tab"))
        s = state.get("sel")
        if st and isinstance(s, dict):
            try:
                sel_text = f"The person is on {st.name!r} with {H.range_text(s['r0'], s['c0'], s['r1'], s['c1'])} selected" \
                           f" (active cell {addr(*state.get('active', [s['r0'], s['c0']]))})."
            except (KeyError, TypeError, ValueError):
                sel_text = ""
        elif st:
            sel_text = f"The person is looking at {st.name!r}."
    if sel_text:
        lines.append(sel_text)
    target = wb.tab(tab) if tab else None
    if tab and target is None:
        raise SheetError("not_found", f"no tab {tab!r}; tabs: " + ", ".join(repr(t.name) for t in wb.tabs))
    show = [target] if target else wb.tabs
    for t in show:
        if not t.cells:
            lines.append(f"[{t.name}] empty")
            continue
        hr = _header_row(wb, t)
        first = 2 if hr else 1
        head = " | ".join(f"{col_letters(c)}={_display_text(wb, t, 1, c) or '-'}" for c in range(1, min(t.max_c, 16) + 1)) if hr else \
            f"no header row; columns A..{col_letters(t.max_c)}"
        lines.append(f"[{t.name}] used A1:{addr(t.max_r, t.max_c)}; " + (f"header (row 1): {head}" if hr else head))
        if len(show) <= 3 or target:
            for c in range(1, min(t.max_c, 12) + 1):
                lines.append(f"  col {col_letters(c)} {_display_text(wb, t, 1, c)[:24]!r}: {_col_summary(wb, t, c, first)}")
        extras = []
        if t.freeze["rows"] or t.freeze["cols"]:
            extras.append(f"frozen {t.freeze['rows']}r/{t.freeze['cols']}c")
        if t.filter:
            extras.append(f"filter {t.filter['range']} ({len(t.hidden)} rows hidden)")
        if t.cf:
            extras.append(f"{len(t.cf)} conditional formats")
        if t.charts:
            extras.append("charts: " + ", ".join(f"{c['type']} {c['title']!r} at {c['at']}" for c in t.charts))
        if extras:
            lines.append("  " + "; ".join(extras))
    # a block of cells
    block_tab = target or (wb.tab((state or {}).get("tab")) if state else None) or wb.tabs[0]
    if rng or target:
        pr = _clamp(block_tab, _rng(rng)) if rng else (1, 1, min(block_tab.max_r, 25), min(block_tab.max_c, 10))
    else:
        pr = (1, 1, min(block_tab.max_r, 12), min(block_tab.max_c, 8)) if block_tab.cells else None
    if pr:
        r0, c0, r1, c1 = pr
        if (r1 - r0 + 1) * (c1 - c0 + 1) > max_cells:
            r1 = min(r1, r0 + max(1, max_cells // max(1, c1 - c0 + 1)) - 1)
            lines.append(f"(range cut to {H.range_text(r0, c0, r1, c1)}: {max_cells} cells at most per read)")
        lines.append(f"{block_tab.name!r}!{H.range_text(r0, c0, r1, c1)} (value, or value {{=formula}}):")
        lines.append("row | " + " | ".join(col_letters(c) for c in range(c0, c1 + 1)))
        for r in range(r0, r1 + 1):
            cols = []
            for c in range(c0, c1 + 1):
                cell = block_tab.cells.get((r, c))
                if cell is None or cell.v is None:
                    cols.append("")
                    continue
                s = display(wb, cell)
                if cell.ast is not None:
                    s += " {" + cell.raw + "}"
                cols.append(s[:60])
            lines.append(f"{r} | " + " | ".join(cols))
        if r1 < block_tab.max_r and not rng:
            lines.append(f"... {block_tab.max_r - r1} more rows (read with range)")
    return "\n".join(lines)


def context_line(st: Dict[str, Any]) -> str:
    """One line for the turn's context block about the sheet the person has open."""
    try:
        meta, wb = _snapshot_view(st["id"])
    except SheetError:
        return ""
    tab = wb.tab(st.get("tab")) or wb.tabs[0]
    line = (f"Spreadsheet open beside the chat: {meta['title']!r} (id {meta['id']}), tabs "
            + ", ".join(f"{t.name!r}" for t in wb.tabs) + f"; the person is on {tab.name!r}")
    s = st.get("sel")
    if isinstance(s, dict):
        try:
            rg = H.range_text(s["r0"], s["c0"], s["r1"], s["c1"])
            line += f", selected {rg}"
            vals = []
            for (r, c) in sorted(_cells_in(tab, (s["r0"], s["c0"], min(s["r1"], s["r0"] + 5), min(s["c1"], s["c0"] + 5))))[:8]:
                vals.append(f"{addr(r, c)}={_display_text(wb, tab, r, c)[:24]!r}")
            if vals:
                line += " (" + ", ".join(vals) + ")"
        except (KeyError, TypeError):
            pass
    return line + ". sheet_read shows it, sheet_write changes it in one batch, sheet_query aggregates without dumping cells."


# ---------------------------------------------------------------------------
# queries without dumping cells
# ---------------------------------------------------------------------------
def _col_of(wb: Workbook, tab: Tab, ref: Any) -> int:
    hr = _header_row(wb, tab)
    return _header_index(tab, (1, 1, max(tab.max_r, 1), max(tab.max_c, 1)), ref) if ref not in (None, "") else 1


def _agg(vals: List[Any], how: str) -> Any:
    nums = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if how in ("count",):
        return len(nums)
    if how == "counta":
        return len([v for v in vals if v not in (None, "")])
    if how == "distinct":
        return len({(v.lower() if isinstance(v, str) else v) for v in vals if v not in (None, "")})
    if how in ("sum", "total"):
        return H._tidy(float(sum(nums)))
    if not nums:
        return None
    if how in ("avg", "average", "mean"):
        return H._tidy(sum(nums) / len(nums))
    if how == "min":
        return min(nums)
    if how == "max":
        return max(nums)
    if how == "median":
        import statistics
        return H._tidy(statistics.median(nums))
    if how in ("stdev", "std"):
        import statistics
        return H._tidy(statistics.stdev(nums)) if len(nums) > 1 else None
    raise SheetError("bad_args", "agg is count, counta, sum, avg, min, max, median, stdev or distinct")


def query(wb: Workbook, tab: Optional[str], spec: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    t = wb.tab(tab) if tab else wb.tabs[0]
    if t is None:
        raise SheetError("not_found", f"no tab {tab!r}")
    hr = _header_row(wb, t) or 0
    first = hr + 1
    kind = str(spec.get("kind") or "agg").lower()
    rows = range(first, t.max_r + 1)
    for w in spec.get("where") or []:
        col = _col_of(wb, t, w.get("column", w.get("col")))
        crit = w.get("is")
        if crit is None and "op" in w:
            crit = (w["op"] if w["op"] != "==" else "=") + str(w.get("value"))
        if crit is None:
            crit = w.get("equals")
        test = H.make_criterion(crit)
        rows = [r for r in rows if test(None if (r, col) not in t.cells else t.cells[(r, col)].v)]
    rows = list(rows)
    name = lambda c: (_display_text(wb, t, 1, c) if hr else col_letters(c)) or col_letters(c)  # noqa: E731
    if kind == "head":
        cols = range(1, min(t.max_c, 12) + 1)
        lines = [" | ".join(name(c) for c in cols)]
        for r in rows[: int(spec.get("limit", 10))]:
            lines.append(" | ".join(_display_text(wb, t, r, c)[:30] for c in cols))
        return f"{len(rows)} rows match\n" + "\n".join(lines), {"rows": len(rows)}
    col = _col_of(wb, t, spec.get("column")) if spec.get("column") not in (None, "") else 1
    vals = [t.cells[(r, col)].v if (r, col) in t.cells else None for r in rows]
    if kind == "distinct":
        seen: Dict[str, int] = {}
        for v in vals:
            if v not in (None, ""):
                k = display_value(wb, t, col, v)
                seen[k] = seen.get(k, 0) + 1
        top = sorted(seen.items(), key=lambda kv: -kv[1])[: int(spec.get("limit", 30))]
        return f"{len(seen)} distinct values in {name(col)!r}: " + ", ".join(f"{k} ({n})" for k, n in top), {"distinct": len(seen)}
    if kind == "find":
        needle = str(spec.get("text") or spec.get("value") or "").lower()
        hits = [addr(r, c) for (r, c), cell in t.cells.items() if needle and needle in display(wb, cell).lower()][: int(spec.get("limit", 30))]
        return f"{len(hits)} cells contain {needle!r}: " + ", ".join(hits), {"cells": hits}
    how = str(spec.get("agg") or "sum").lower()
    group = spec.get("group_by")
    if group not in (None, ""):
        gcol = _col_of(wb, t, group)
        buckets: Dict[str, List[Any]] = {}
        for r in rows:
            g = _display_text(wb, t, r, gcol)
            buckets.setdefault(g, []).append(t.cells[(r, col)].v if (r, col) in t.cells else None)
        res = {g: _agg(v, how) for g, v in buckets.items()}
        ordered = sorted(res.items(), key=lambda kv: (kv[1] is None, -(kv[1] or 0) if isinstance(kv[1], (int, float)) else 0))[: int(spec.get("limit", 30))]
        return (f"{how} of {name(col)!r} by {name(gcol)!r} on {t.name!r} ({len(rows)} rows): " +
                ", ".join(f"{g or '(blank)'}={F.general(v) if v is not None else 'n/a'}" for g, v in ordered)), {"groups": dict(ordered)}
    val = _agg(vals, how)
    return f"{how} of {name(col)!r} on {t.name!r} over {len(rows)} rows = {F.general(val) if val is not None else 'n/a'}", {"value": val, "rows": len(rows)}


def display_value(wb: Workbook, t: Tab, col: int, v: Any) -> str:
    for r in range(1, min(t.max_r, 50) + 1):
        cell = t.cells.get((r, col))
        if cell is not None and cell.v == v:
            return display(wb, cell)
    return F.general(v) if not isinstance(v, str) else v


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------
_lock = threading.RLock()
_cache: Dict[str, Dict[str, Any]] = {}
_root_override: Optional[str] = None
_timers: Dict[str, threading.Timer] = {}
FLUSH_S = 1.2


def root() -> str:
    if _root_override:
        return _root_override
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "surfaces", "hoja")


def set_root_for_tests(path: Optional[str]) -> None:
    global _root_override
    with _lock:
        for t in _timers.values():
            t.cancel()
        _timers.clear()
        _root_override = path
        _cache.clear()


def _dir(sheet_id: str) -> str:
    if not _ID_RE.match(str(sheet_id or "")):
        raise SheetError("bad_args", "bad sheet id")
    return os.path.join(root(), sheet_id)


def _atomic_write(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    if isinstance(data, bytes):
        with open(tmp, "wb") as fh:
            fh.write(data)
    else:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(data)
    os.replace(tmp, path)


def dump_model(wb: Workbook) -> str:
    tabs = []
    for t in wb.tabs:
        cells = {}
        for (r, c), cell in t.cells.items():
            raw = cell.raw
            if isinstance(raw, str) and raw.startswith("=") and cell.ast is None:
                raw = "'" + raw
            cells[addr(r, c)] = [raw, cell.sid] if cell.sid else [raw]
        tabs.append({"id": t.id, "name": t.name, "cells": cells, "cols": {str(k): v for k, v in t.cols.items()},
                     "rows": {str(k): v for k, v in t.rows.items()}, "freeze": t.freeze, "filter": t.filter, "cf": t.cf, "charts": t.charts})
    return json.dumps({"v": 1, "seq": wb.seq, "styles": wb.styles, "tabs": tabs}, ensure_ascii=False, separators=(",", ":"))


def load_model(text: str) -> Workbook:
    try:
        data = json.loads(text)
    except ValueError:
        raise SheetError("bad_args", "the sheet model is not valid JSON") from None
    wb = Workbook()
    wb.styles = [s if isinstance(s, dict) else {} for s in data.get("styles") or [{}]] or [{}]
    wb._style_ix = {json.dumps(s, sort_keys=True, separators=(",", ":")): i for i, s in enumerate(wb.styles)}
    wb.seq = int(data.get("seq") or 0)
    for td in data.get("tabs") or []:
        t = Tab(str(td["id"]), str(td["name"]))
        for a, entry in (td.get("cells") or {}).items():
            rc = parse_addr(a)
            if not rc or not entry:
                continue
            raw = entry[0]
            sid = int(entry[1]) if len(entry) > 1 else 0
            cell = Cell(raw, sid if sid < len(wb.styles) else 0)
            if isinstance(raw, str) and raw.startswith("'="):
                cell.raw = cell.v = raw[1:]
            elif isinstance(raw, str) and raw.startswith("=") and len(raw) > 1:
                try:
                    cell.ast = H.parse(raw)
                except H.FormulaSyntax:
                    cell.ast = None
            t.cells[rc] = cell
        t.cols = {int(k): int(v) for k, v in (td.get("cols") or {}).items()}
        t.rows = {int(k): int(v) for k, v in (td.get("rows") or {}).items()}
        t.freeze = td.get("freeze") or {"rows": 0, "cols": 0}
        t.filter, t.cf, t.charts = td.get("filter"), td.get("cf") or [], td.get("charts") or []
        t.recompute_bounds()
        wb.tabs.append(t)
    if not wb.tabs:
        wb.new_tab("Sheet1")
    wb.reindex_names()
    wb.rebuild_graph()
    recalc(wb, everything=True)
    for t in wb.tabs:
        apply_filter(t)
    return wb


def _write(sheet_id: str, entry: Dict[str, Any]) -> None:
    d = _dir(sheet_id)
    _atomic_write(os.path.join(d, "meta.json"), json.dumps(entry["meta"], ensure_ascii=False, indent=1))
    _atomic_write(os.path.join(d, "model.json"), dump_model(entry["wb"]))
    entry["xlsx_dirty"] = True
    _schedule_flush(sheet_id)


def _schedule_flush(sheet_id: str) -> None:
    old = _timers.pop(sheet_id, None)
    if old:
        old.cancel()
    t = threading.Timer(FLUSH_S, lambda: flush(sheet_id))
    t.daemon = True
    _timers[sheet_id] = t
    t.start()


def flush(sheet_id: str) -> Optional[str]:
    """Write ``sheet.xlsx`` now when it is behind the model; returns its path."""
    with _lock:
        try:
            e = _load(sheet_id)
        except SheetError:
            return None
        path = os.path.join(_dir(sheet_id), "sheet.xlsx")
        if e.get("xlsx_dirty") or not os.path.exists(path):
            try:
                from src import hoja_xlsx
                _atomic_write(path, hoja_xlsx.export_bytes(e["wb"], e["meta"]))
                e["xlsx_dirty"] = False
            except ImportError:
                return None
            except Exception:  # noqa: BLE001
                return None
        _timers.pop(sheet_id, None)
        return path


def xlsx_path(sheet_id: str) -> Optional[str]:
    return flush(sheet_id)


def _load(sheet_id: str) -> Dict[str, Any]:
    with _lock:
        if sheet_id in _cache:
            return _cache[sheet_id]
        d = _dir(sheet_id)
        try:
            with open(os.path.join(d, "meta.json"), encoding="utf-8") as fh:
                meta = json.load(fh)
            with open(os.path.join(d, "model.json"), encoding="utf-8") as fh:
                wb = load_model(fh.read())
        except (OSError, ValueError):
            raise SheetError("not_found", f"no sheet {sheet_id!r}") from None
        if meta.get("deleted"):
            raise SheetError("not_found", f"no sheet {sheet_id!r}")
        entry = {"meta": meta, "wb": wb, "xlsx_dirty": False}
        _cache[sheet_id] = entry
        return entry


def _snapshot_view(sheet_id: str) -> Tuple[Dict[str, Any], Workbook]:
    with _lock:
        e = _load(sheet_id)
        return dict(e["meta"]), e["wb"]


def create(title: str = "", *, session_id: str = "", project_id: str = "", owner: str = "", csv_text: Optional[str] = None,
           xlsx_bytes: Optional[bytes] = None, wb: Optional[Workbook] = None, tab_name: str = "Sheet1") -> Dict[str, Any]:
    with _lock:
        sheet_id = new_id()
        now = time.time()
        if xlsx_bytes is not None:
            from src import hoja_xlsx
            wb = hoja_xlsx.import_bytes(xlsx_bytes)
        elif wb is None:
            wb = Workbook()
            wb.new_tab(tab_name)
            if csv_text:
                ch = Changes()
                _op_csv(wb, {"text": csv_text, "at": "A1", "tab": wb.tabs[0].id}, None, ch)
                fix_all(wb)
                recalc(wb, everything=True)
        meta = {"id": sheet_id, "kind": "hoja", "title": (title or "").strip()[:200] or "Untitled sheet", "session_id": session_id or "",
                "project_id": project_id or "", "owner": owner or "", "created": now, "updated": now, "rev": 1}
        entry = {"meta": meta, "wb": wb, "xlsx_dirty": True}
        _cache[sheet_id] = entry
        _write(sheet_id, entry)
        return dict(meta)


def _row(meta: Dict[str, Any], wb: Workbook) -> Dict[str, Any]:
    row = dict(meta)
    row["tabs"] = len(wb.tabs)
    row["cells"] = wb.total_cells()
    return row


def get(sheet_id: str) -> Tuple[Dict[str, Any], Workbook]:
    """``(meta, workbook)``: the live workbook (callers read; edits go through ``edit``)."""
    return _snapshot_view(sheet_id)


def list_sheets(*, session_id: Optional[str] = None, project_id: Optional[str] = None, owner: Optional[str] = None) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    base = root()
    if not os.path.isdir(base):
        return out
    for name in os.listdir(base):
        try:
            e = _load(name)
        except SheetError:
            continue
        m = e["meta"]
        if session_id is not None and m.get("session_id") != session_id:
            continue
        if project_id is not None and m.get("project_id") != project_id:
            continue
        if owner and m.get("owner") and m["owner"] != owner:
            continue
        out.append(_row(m, e["wb"]))
    out.sort(key=lambda r: r.get("updated", 0), reverse=True)
    return out


def _commit(sheet_id: str, entry: Dict[str, Any]) -> None:
    entry["meta"]["rev"] = int(entry["meta"].get("rev", 0)) + 1
    entry["meta"]["updated"] = time.time()
    _write(sheet_id, entry)


def rename(sheet_id: str, title: str) -> Dict[str, Any]:
    with _lock:
        e = _load(sheet_id)
        e["meta"]["title"] = (title or "").strip()[:200] or e["meta"]["title"]
        _commit(sheet_id, e)
        return dict(e["meta"])


def delete(sheet_id: str) -> bool:
    with _lock:
        try:
            e = _load(sheet_id)
        except SheetError:
            return False
        e["meta"]["deleted"] = True
        _atomic_write(os.path.join(_dir(sheet_id), "meta.json"), json.dumps(e["meta"], ensure_ascii=False, indent=1))
        _cache.pop(sheet_id, None)
        return True


def _depth(wb: Workbook) -> int:
    n = wb.total_cells()
    return 40 if n < 100_000 else 12 if n < 500_000 else 4


def _snap(wb: Workbook) -> Dict[str, Any]:
    return {"tabs": [t.snapshot() for t in wb.tabs], "seq": wb.seq}


def _restore(wb: Workbook, snap: Dict[str, Any]) -> Changes:
    wb.tabs = [Tab.from_snapshot(s) for s in snap["tabs"]]
    wb.seq = max(wb.seq, snap["seq"])
    wb.reindex_names()
    wb.rebuild_graph()
    recalc(wb, everything=True)
    for t in wb.tabs:
        apply_filter(t)
    ch = Changes()
    ch.struct = True
    for t in wb.tabs:
        ch.touch_full(t.id)
    return ch


def edit(sheet_id: str, ops: Any, *, view: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], List[str], Changes, Optional[SheetError]]:
    """Run ``ops``. A failing op keeps what ran before it (and says which op failed)."""
    with _lock:
        e = _load(sheet_id)
        wb = e["wb"]
        snap = _snap(wb)
        err: Optional[SheetError] = None
        try:
            results, ch = apply_ops(wb, ops, view=view)
        except SheetError as exc:
            if not hasattr(exc, "changes"):
                raise
            results, ch, err = list(getattr(exc, "results", [])), getattr(exc, "changes"), exc
        if not ch.empty():
            wb.undo.append(snap)
            del wb.undo[:-_depth(wb)]
            wb.redo.clear()
            _commit(sheet_id, e)
        return dict(e["meta"]), results, ch, err


def undo(sheet_id: str) -> Tuple[Dict[str, Any], Changes, bool]:
    with _lock:
        e = _load(sheet_id)
        wb = e["wb"]
        if not wb.undo:
            return dict(e["meta"]), Changes(), False
        wb.redo.append(_snap(wb))
        ch = _restore(wb, wb.undo.pop())
        _commit(sheet_id, e)
        return dict(e["meta"]), ch, True


def redo(sheet_id: str) -> Tuple[Dict[str, Any], Changes, bool]:
    with _lock:
        e = _load(sheet_id)
        wb = e["wb"]
        if not wb.redo:
            return dict(e["meta"]), Changes(), False
        wb.undo.append(_snap(wb))
        ch = _restore(wb, wb.redo.pop())
        _commit(sheet_id, e)
        return dict(e["meta"]), ch, True


def replace_from_xlsx(sheet_id: str, data: bytes) -> Tuple[Dict[str, Any], Changes]:
    from src import hoja_xlsx
    with _lock:
        e = _load(sheet_id)
        new = hoja_xlsx.import_bytes(data)
        e["wb"].undo.append(_snap(e["wb"]))
        new.undo, new.redo = e["wb"].undo, []
        e["wb"] = new
        ch = Changes()
        ch.struct = True
        for t in new.tabs:
            ch.touch_full(t.id)
        _commit(sheet_id, e)
        return dict(e["meta"]), ch


def export_csv(sheet_id: str, tab: Optional[str] = None) -> str:
    meta, wb = get(sheet_id)
    t = wb.tab(tab) if tab else wb.tabs[0]
    if t is None:
        raise SheetError("not_found", f"no tab {tab!r}")
    buf = io.StringIO()
    w = csv.writer(buf)
    for r in range(1, t.max_r + 1):
        w.writerow([_display_text(wb, t, r, c) if (r, c) in t.cells else "" for c in range(1, t.max_c + 1)])
    return buf.getvalue()


def reset_for_tests() -> None:
    with _lock:
        for t in _timers.values():
            t.cancel()
        _timers.clear()
        _cache.clear()
