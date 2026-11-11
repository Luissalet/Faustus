"""src/hoja_xlsx.py -- Hoja <-> .xlsx. The workbook is written with XlsxWriter (BSD) so every formula carries its computed value
(readers that do not recalculate, such as pandas or a preview, still see numbers) and read with openpyxl (MIT).

Written: values, formulas (English names; newer functions get their ``_xlfn.`` prefix), bold / italic / colour / fill /
alignment / wrap / border / number formats, column widths, row heights, hidden rows, frozen panes, the filter range,
conditional formats and charts. Read: values, formulas, the same styles, widths, heights, frozen panes. Not read: charts,
merged cells, comments, images (they are left out, never an error).
"""
from __future__ import annotations

import datetime as _dt
import io
import re
from typing import Any, Dict, List, Optional, Tuple

from src import hoja
from src import hoja_format as F
from src import hoja_formula as H


def _sheet_ref(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"


def _abs(ref: str, tabname: str) -> str:
    sheet, body = hoja._split_sheet(ref)
    pr = H.parse_range(body)
    if not pr:
        return "=" + _sheet_ref(sheet or tabname) + "!A1"
    r0, c0, r1, c1 = pr
    a = f"${H.col_letters(c0)}${r0}"
    return "=" + _sheet_ref(sheet or tabname) + "!" + (a if (r0, c0) == (r1, c1) else f"{a}:${H.col_letters(c1)}${r1}")


def export_bytes(wb: hoja.Workbook, meta: Optional[Dict[str, Any]] = None) -> bytes:
    import xlsxwriter
    buf = io.BytesIO()
    book = xlsxwriter.Workbook(buf, {"in_memory": True, "strings_to_numbers": False, "strings_to_formulas": False,
                                     "strings_to_urls": False, "nan_inf_to_errors": True, "use_future_functions": True})
    if meta:
        book.set_properties({"title": str(meta.get("title") or ""), "comments": "Made with Faustus Hoja"})
    fmts: Dict[Tuple[int, str], Any] = {}

    def fmt(sid: int, nf_extra: str = ""):
        key = (sid, nf_extra)
        if key in fmts:
            return fmts[key]
        st = hoja.style_of(wb, sid)
        props: Dict[str, Any] = {}
        if st.get("b"):
            props["bold"] = True
        if st.get("i"):
            props["italic"] = True
        if st.get("c"):
            props["font_color"] = st["c"]
        if st.get("bg"):
            props["bg_color"] = st["bg"]
        if st.get("al"):
            props["align"] = {"l": "left", "c": "center", "r": "right"}[st["al"]]
        if st.get("wrap"):
            props["text_wrap"] = True
        if st.get("bd"):
            props["border"] = 1
        nf = st.get("nf") or nf_extra
        if nf and nf != "General":
            props["num_format"] = nf
        fmts[key] = book.add_format(props) if props else None
        return fmts[key]

    for tab in wb.tabs:
        ws = book.add_worksheet(tab.name[:31])
        for c, w in tab.cols.items():
            ws.set_column(c - 1, c - 1, max(2.0, (w - 5) / 7.0))
        for r, h in tab.rows.items():
            ws.set_row(r - 1, h * 0.75, None, {"hidden": r in tab.hidden})
        for r in tab.hidden:
            if r not in tab.rows:
                ws.set_row(r - 1, None, None, {"hidden": True})
        if tab.freeze["rows"] or tab.freeze["cols"]:
            ws.freeze_panes(tab.freeze["rows"], tab.freeze["cols"])
        for (r, c) in sorted(tab.cells):
            cell = tab.cells[(r, c)]
            extra = hoja.cell_format(wb, cell) if not hoja.style_of(wb, cell.sid).get("nf") else ""
            f = fmt(cell.sid, extra)
            if cell.ast is not None:
                v = cell.v
                val = v.code if isinstance(v, H.Err) else (v if v is not None else 0)
                ws.write_formula(r - 1, c - 1, cell.raw, f, val)
            elif cell.raw is None:
                ws.write_blank(r - 1, c - 1, None, f)
            elif isinstance(cell.raw, bool):
                ws.write_boolean(r - 1, c - 1, cell.raw, f)
            elif isinstance(cell.raw, (int, float)):
                ws.write_number(r - 1, c - 1, cell.raw, f)
            else:
                ws.write_string(r - 1, c - 1, str(cell.raw), f)
        if tab.filter and tab.filter.get("rg"):
            r0, c0, r1, c1 = tab.filter["rg"]
            ws.autofilter(r0 - 1, c0 - 1, r1 - 1, c1 - 1)
        for rule in tab.cf:
            r0, c0, r1, c1 = rule["rg"]
            k = rule["rule"]
            props = {k2: v for k2, v in (("bg_color", rule.get("bg")), ("font_color", rule.get("c")), ("bold", bool(rule.get("b")) or None)) if v}
            f = book.add_format(props) if props else None
            opt: Dict[str, Any] = {}
            crit = {"gt": ">", "lt": "<", "gte": ">=", "lte": "<=", "eq": "==", "neq": "!="}
            if k in crit:
                opt = {"type": "cell", "criteria": crit[k], "value": rule.get("value"), "format": f}
            elif k == "between":
                opt = {"type": "cell", "criteria": "between", "minimum": rule.get("value"), "maximum": rule.get("value2"), "format": f}
            elif k == "contains":
                opt = {"type": "text", "criteria": "containing", "value": str(rule.get("value", "")), "format": f}
            elif k in ("blank", "notblank"):
                opt = {"type": "blanks" if k == "blank" else "no_blanks", "format": f}
            elif k in ("top", "bottom"):
                opt = {"type": k, "value": int(rule.get("value") or 1), "format": f}
            elif k == "duplicate":
                opt = {"type": "duplicate", "format": f}
            elif k == "scale":
                cols = rule.get("scale") or ["#f8696b", "#63be7b"]
                opt = ({"type": "3_color_scale", "min_color": cols[0], "mid_color": cols[1], "max_color": cols[2]} if len(cols) == 3
                       else {"type": "2_color_scale", "min_color": cols[0], "max_color": cols[1]})
            elif k == "formula":
                opt = {"type": "formula", "criteria": str(rule.get("value")), "format": f}
            if opt:
                try:
                    ws.conditional_format(r0 - 1, c0 - 1, min(r1, max(tab.max_r, r0)) - 1, min(c1, max(tab.max_c, c0)) - 1, opt)
                except Exception:  # noqa: BLE001
                    pass
        for chart in tab.charts:
            kind = {"bar": "column", "line": "line", "pie": "pie", "scatter": "scatter", "area": "area"}[chart["type"]]
            ch = book.add_chart({"type": kind})
            src_name = chart.get("tab") or tab.name
            for s in chart.get("series", []):
                spec = {"name": s.get("name"), "values": _abs(s["ref"], src_name)}
                if chart.get("x"):
                    spec["categories"] = _abs(chart["x"], src_name)
                ch.add_series(spec)
            if chart.get("title"):
                ch.set_title({"name": chart["title"]})
            ch.set_size({"width": int(chart.get("w", 520)), "height": int(chart.get("h", 320))})
            a = H.parse_addr(chart.get("at") or "A1") or (1, 1)
            ws.insert_chart(a[0] - 1, a[1] - 1, ch)
    book.close()
    return buf.getvalue()


def _rgb(color: Any) -> Optional[str]:
    try:
        if color is None or getattr(color, "type", "rgb") != "rgb":
            return None
        v = str(color.rgb)
        if re.fullmatch(r"[0-9A-Fa-f]{8}", v):
            v = v[2:]
        return "#" + v.lower() if re.fullmatch(r"[0-9A-Fa-f]{6}", v) else None
    except Exception:  # noqa: BLE001
        return None


def import_bytes(data: bytes) -> hoja.Workbook:
    import openpyxl
    try:
        book = openpyxl.load_workbook(io.BytesIO(data), data_only=False)
    except Exception as exc:  # noqa: BLE001
        raise hoja.SheetError("bad_args", f"that is not a readable .xlsx file ({type(exc).__name__})") from None
    wb = hoja.Workbook()
    budget = hoja.MAX_CELLS
    for ws in book.worksheets[: hoja.MAX_TABS]:
        tab = wb.new_tab(hoja._unique_name(wb, ws.title))
        for row in ws.iter_rows():
            for cell in row:
                if cell.value is None and not (cell.has_style and cell.fill is not None and cell.fill.fill_type):
                    continue
                if budget <= 0:
                    break
                v = cell.value
                nf = cell.number_format if cell.number_format and cell.number_format != "General" else None
                if isinstance(v, _dt.datetime):
                    v = F.serial_from_datetime(v)
                    nf = nf or "yyyy-mm-dd hh:mm"
                    v = int(v) if float(v).is_integer() else v
                elif isinstance(v, _dt.date):
                    v = F.serial_from_date(v)
                    nf = nf or "yyyy-mm-dd"
                elif isinstance(v, _dt.time):
                    v = (v.hour * 3600 + v.minute * 60 + v.second) / 86400.0
                    nf = nf or "hh:mm:ss"
                style: Dict[str, Any] = {}
                if cell.font is not None:
                    if cell.font.b:
                        style["b"] = 1
                    if cell.font.i:
                        style["i"] = 1
                    col = _rgb(cell.font.color)
                    if col and col not in ("#000000",):
                        style["c"] = col
                if cell.fill is not None and cell.fill.fill_type == "solid":
                    bg = _rgb(cell.fill.fgColor)
                    if bg and bg != "#000000":
                        style["bg"] = bg
                if cell.alignment is not None:
                    if cell.alignment.horizontal in ("left", "center", "right"):
                        style["al"] = cell.alignment.horizontal[0]
                    if cell.alignment.wrap_text:
                        style["wrap"] = 1
                if nf:
                    style["nf"] = nf
                sid = wb.style_id(style) if style else 0
                if not isinstance(v, (str, int, float, bool, type(None))) and hasattr(v, "text"):
                    v = v.text if str(v.text).startswith("=") else "=" + str(v.text)       # array / dynamic formulas
                elif not isinstance(v, (str, int, float, bool, type(None))):
                    v = str(v)
                if isinstance(v, str) and v.startswith("="):
                    v = re.sub(r"_xlfn\.|_xlws\.", "", v)
                    try:
                        c = hoja.make_cell(wb, tab, cell.row, cell.column, v, parse=False, sid=sid)
                    except hoja.SheetError:
                        c = hoja.Cell("'" + v, sid)
                        c.raw = c.v = v
                elif v is None:
                    c = hoja.Cell(None, sid)
                else:
                    if isinstance(v, str) and v.startswith("'"):
                        v = "'" + v
                    c = hoja.Cell(v, sid)
                hoja.put(wb, tab, cell.row, cell.column, c)
                budget -= 1
        for letter, dim in ws.column_dimensions.items():
            if dim.width and not dim.hidden:
                lo, hi = (dim.min or H.col_number(letter)), (dim.max or H.col_number(letter))
                for c in range(lo, min(hi, lo + 200) + 1):
                    tab.cols[c] = int(dim.width * 7 + 5)
        for r, dim in ws.row_dimensions.items():
            if dim.height:
                tab.rows[int(r)] = int(dim.height / 0.75)
            if dim.hidden:
                tab.hidden.add(int(r))
        fp = ws.freeze_panes
        if fp:
            rc = H.parse_addr(str(fp))
            if rc:
                tab.freeze = {"rows": rc[0] - 1, "cols": rc[1] - 1}
        hoja.fix_bounds(tab)
        tab.recompute_bounds()
    if not wb.tabs:
        wb.new_tab("Sheet1")
    wb.reindex_names()
    wb.rebuild_graph()
    hoja.recalc(wb, everything=True)
    return wb
