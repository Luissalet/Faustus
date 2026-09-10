"""spreadsheet.py — ART-04: reliable spreadsheets.

Faustus had no dedicated spreadsheet path before this: CSV/XLSX only ever
passed through as opaque document text (`src/document_processor.py`) or as a
generic artifact byte blob (`src/artifact_store.py`). Neither preserves the
one property a spreadsheet actually needs: a cell's *type* survives a
round trip. Three failure modes this module exists to prevent, per ART-04's
acceptance criterion:

* **IDs turned into scientific notation.** A naive ``float(cell)`` on an
  18-digit account number silently mangles it (``1.23456789012345e+17``) and
  the original digits are gone. Anything that looks like an identifier
  (leading zero, or too many significant digits for exact float round-trip)
  is kept as text, never coerced to a number.
* **Ambiguous dates guessed instead of asked about.** ``01/02/03`` is a
  different date in en-US, en-GB and ISO conventions. Guessing one silently
  is a data-corruption bug wearing a feature's clothes — this module keeps it
  as text and flags it, so a human (or a caller that knows the source
  locale) resolves it instead of the parser assuming an answer nobody stated.
* **Formula injection on CSV export.** A cell whose value starts with
  ``=``/``+``/``-``/``@`` is executed as a formula by Excel/Sheets/LibreOffice
  the moment the CSV is opened (CWE-1236 / OWASP CSV injection). Exporting a
  value that did not arrive as an intentional formula (``allow_formulas``)
  neutralizes that leading character instead of writing it verbatim.

Reuses `openpyxl` (already a dependency — see `src/artifact_store.py` and
`src/artifact_identity.py`'s xlsx validator) rather than adding one. Nothing
here is a second document store: it is a set of pure, testable functions a
route or tool calls, the same shape as `src/document_actions.py`.
"""
from __future__ import annotations

import csv
import io
import logging
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: A run of digits long enough that a naive float() would start losing
#: precision (a double has ~15-17 significant decimal digits) or is likely an
#: identifier (leading zero) rather than a quantity.
_LEADING_ZERO_ID = re.compile(r"^0\d+$")
_LONG_DIGIT_RUN = re.compile(r"^\d{16,}$")
_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?\d*\.\d+$|^[+-]?\d+\.\d*$")
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
#: D/M/Y or M/D/Y with a 2-digit year, and D.M.Y — the classic ambiguous
#: forms. A 4-digit-year slash date (2024-style) is not ambiguous about which
#: field is the year, only D-vs-M order is still real ambiguity, so those are
#: ALSO flagged rather than guessed.
_SLASHED_DATE_RE = re.compile(r"^(\d{1,2})[/.](\d{1,2})[/.](\d{2,4})$")

#: Leading characters that make a spreadsheet application treat a CSV cell as
#: a formula/command rather than literal text (OWASP CSV injection).
_FORMULA_LEAD_CHARS = ("=", "+", "-", "@", "\t", "\r")


class SpreadsheetError(ValueError):
    pass


def looks_like_id(text: str) -> bool:
    """True for digit strings a numeric coercion would corrupt: a leading
    zero (a serial/account number, not a quantity with zero padding meant to
    disappear) or more digits than a float can round-trip exactly."""
    return bool(_LEADING_ZERO_ID.match(text) or _LONG_DIGIT_RUN.match(text))


def classify_cell_text(raw: str) -> Tuple[Any, str, Optional[str]]:
    """Decide what one CSV/TSV cell's text really is.

    Returns ``(value, type_name, warning)`` where ``type_name`` is one of
    ``"int"``, ``"float"``, ``"date"``, ``"text"``, ``"formula"``, ``"empty"``,
    and ``warning`` is a human-readable note when the safest choice was to
    leave something as text rather than guess (ID-shaped number, ambiguous
    date). Never raises — an unparsed cell just comes back as text.
    """
    text = raw if isinstance(raw, str) else ("" if raw is None else str(raw))
    stripped = text.strip()
    if stripped == "":
        return "", "empty", None
    if stripped.startswith("="):
        return stripped, "formula", None
    if looks_like_id(stripped):
        return stripped, "text", "kept as text — looks like an identifier, not a quantity"
    m = _ISO_DATE_RE.match(stripped)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))), "date", None
        except ValueError:
            pass  # falls through to text below
    if _SLASHED_DATE_RE.match(stripped):
        return stripped, "text", (
            f"kept as text — {stripped!r} is an ambiguous date "
            "(day/month order cannot be inferred); resolve it explicitly")
    if _INT_RE.match(stripped):
        try:
            value = int(stripped)
            # Even without a leading zero, a value this large already lost
            # exactness the moment anything upstream treated it as a float.
            if len(stripped.lstrip("+-")) >= 16:
                return stripped, "text", "kept as text — too many digits for exact numeric round-trip"
            return value, "int", None
        except ValueError:
            pass
    if _FLOAT_RE.match(stripped):
        try:
            return float(stripped), "float", None
        except ValueError:
            pass
    return stripped, "text", None


def import_csv_rows(text: str, *, dialect: str = "excel") -> Dict[str, Any]:
    """Parse CSV/TSV text into typed rows without guessing away ambiguity.

    Returns ``{"rows": [[cell, ...], ...], "types": [[type_name, ...], ...],
    "warnings": [str, ...]}``. Delimiter is sniffed between comma and tab; a
    caller that already knows the dialect can pass one of csv's registered
    names instead of relying on the sniff.
    """
    if not text:
        return {"rows": [], "types": [], "warnings": []}
    sample = text[:4096]
    try:
        sniffed = csv.Sniffer().sniff(sample, delimiters=",\t;")
        reader = csv.reader(io.StringIO(text), dialect=sniffed)
    except csv.Error:
        reader = csv.reader(io.StringIO(text), dialect=dialect)

    rows: List[List[Any]] = []
    types: List[List[str]] = []
    warnings: List[str] = []
    for r_idx, raw_row in enumerate(reader):
        row_values: List[Any] = []
        row_types: List[str] = []
        for c_idx, cell in enumerate(raw_row):
            value, kind, warning = classify_cell_text(cell)
            row_values.append(value)
            row_types.append(kind)
            if warning:
                warnings.append(f"row {r_idx + 1}, col {c_idx + 1}: {warning}")
        rows.append(row_values)
        types.append(row_types)
    return {"rows": rows, "types": types, "warnings": warnings}


def safe_csv_value(value: Any, *, allow_formulas: bool = False) -> str:
    """Neutralize a value that would otherwise be interpreted as a formula by
    a spreadsheet application opening the exported CSV (ART-04 acceptance:
    "bloquear inyección de fórmulas al exportar texto no confiable").

    ``allow_formulas=True`` is for the one legitimate case — the value really
    is a formula the caller means to keep live (e.g. re-exporting a sheet
    that already had formulas) — and skips neutralization entirely.
    """
    text = value if isinstance(value, str) else ("" if value is None else str(value))
    if allow_formulas or not text:
        return text
    if text[0] in _FORMULA_LEAD_CHARS:
        # A leading apostrophe is the standard "treat as literal text" escape
        # every major spreadsheet application recognizes on CSV import.
        return "'" + text
    return text


def export_csv_rows(rows: Sequence[Sequence[Any]], *, allow_formulas: bool = False) -> str:
    """Rows -> CSV text, with every cell run through `safe_csv_value` unless
    the caller explicitly opts into live formulas."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    for row in rows:
        writer.writerow([safe_csv_value(v, allow_formulas=allow_formulas) for v in row])
    return buf.getvalue()


def _require_openpyxl():
    try:
        import openpyxl
    except ImportError as exc:
        raise SpreadsheetError("openpyxl is not installed") from exc
    return openpyxl


def read_workbook_preview(path: str, *, max_rows: int = 200, sheet_name: str = "") -> Dict[str, Any]:
    """A type-aware preview of an XLSX: per sheet, header guess, rows with
    each cell's value/type, which cells are live formulas, and which formula
    cells have NO cached value — i.e. would show stale/blank data until the
    application recalculates them (ART-04: "aviso cuando resultados
    cacheados no se han recalculado").

    Reads the workbook twice (``data_only=False`` for formula text,
    ``data_only=True`` for the last-cached value) — the two are not
    reconcilable from a single load; openpyxl exposes only one or the other
    per open.
    """
    openpyxl = _require_openpyxl()
    try:
        wb_formulas = openpyxl.load_workbook(path, data_only=False, read_only=True)
        wb_values = openpyxl.load_workbook(path, data_only=True, read_only=True)
    except Exception as exc:
        raise SpreadsheetError(f"could not open workbook: {exc}") from exc

    names = [sheet_name] if sheet_name else wb_formulas.sheetnames
    sheets: List[Dict[str, Any]] = []
    for name in names:
        if name not in wb_formulas.sheetnames:
            raise SpreadsheetError(f"no such sheet: {name!r}")
        ws_f = wb_formulas[name]
        ws_v = wb_values[name]
        rows_out: List[List[Dict[str, Any]]] = []
        stale_cells: List[str] = []
        for r_idx, (row_f, row_v) in enumerate(zip(ws_f.iter_rows(), ws_v.iter_rows())):
            if r_idx >= max_rows:
                break
            row_out = []
            for cell_f, cell_v in zip(row_f, row_v):
                is_formula = cell_f.data_type == "f"
                cached = cell_v.value
                cell_type = "date" if isinstance(cell_f.value, (datetime, date)) else (
                    "formula" if is_formula else cell_f.data_type)
                entry = {
                    "ref": cell_f.coordinate,
                    "value": _jsonable(cached if not is_formula else cached),
                    "formula": cell_f.value if is_formula else None,
                    "type": cell_type,
                }
                if is_formula and cached is None:
                    stale_cells.append(cell_f.coordinate)
                    entry["stale"] = True
                row_out.append(entry)
            rows_out.append(row_out)
        sheets.append({
            "name": name,
            "rows": rows_out,
            "stale_formula_cells": stale_cells,
            "needs_recalculation": bool(stale_cells),
        })
    wb_formulas.close()
    wb_values.close()
    return {"sheets": sheets}


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _coerce_for_cell(value: Any) -> Any:
    """String input from a range-write op -> the type openpyxl should store,
    reusing `classify_cell_text` so writes get the same ID/date protection as
    CSV import. Non-string input (already-typed values from a caller that
    parsed its own JSON) passes through unchanged."""
    if not isinstance(value, str):
        return value
    parsed, kind, _warning = classify_cell_text(value)
    if kind == "formula":
        return parsed  # a leading '=' — openpyxl stores this as a live formula
    return parsed


def write_range(path: str, sheet_name: str, start_cell: str,
                values: Sequence[Sequence[Any]], *, save_as: Optional[str] = None) -> Dict[str, Any]:
    """Write a 2D block of values into `sheet_name` starting at `start_cell`,
    preserving formulas (a string starting with '=') and applying the same
    ID/ambiguous-date protection `import_csv_rows` applies, then mark the
    workbook so a viewer recalculates on open when any formula is present
    (ART-04's "recálculo o aviso"). Writes in place unless `save_as` is given.
    """
    openpyxl = _require_openpyxl()
    from openpyxl.utils.cell import coordinate_from_string, column_index_from_string
    from openpyxl.workbook.properties import CalcProperties

    try:
        col_letter, row0 = coordinate_from_string(start_cell)
    except ValueError as exc:
        raise SpreadsheetError(f"invalid start_cell {start_cell!r}") from exc
    col0 = column_index_from_string(col_letter)

    try:
        wb = openpyxl.load_workbook(path, data_only=False)
    except Exception as exc:
        raise SpreadsheetError(f"could not open workbook: {exc}") from exc
    if sheet_name not in wb.sheetnames:
        raise SpreadsheetError(f"no such sheet: {sheet_name!r}")
    ws = wb[sheet_name]

    wrote_formula = False
    id_like_cells: List[str] = []
    for r_off, row in enumerate(values):
        for c_off, raw in enumerate(row):
            coerced = _coerce_for_cell(raw)
            cell = ws.cell(row=row0 + r_off, column=col0 + c_off)
            cell.value = coerced
            if isinstance(coerced, str) and coerced.startswith("="):
                wrote_formula = True
            elif isinstance(raw, str) and looks_like_id(raw.strip()):
                cell.number_format = "@"  # force text so Excel never re-guesses a number
                id_like_cells.append(cell.coordinate)

    if wrote_formula:
        wb.calculation = CalcProperties(fullCalcOnLoad=True)

    target = save_as or path
    try:
        wb.save(target)
    except Exception as exc:
        raise SpreadsheetError(f"could not save workbook: {exc}") from exc
    finally:
        wb.close()
    return {"path": target, "marked_recalculate": wrote_formula, "id_cells_forced_text": id_like_cells}
