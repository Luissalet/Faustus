"""agent_tools/spreadsheet_tools.py — ART-04 tool wrapper (Lote 54).

`src/spreadsheet.py` (Lote 47) already has typed CSV/XLSX handling with 14
passing tests — import/export with formula-injection protection, an
XLSX preview that tells a live formula from a stale cached one, and range
writes — but no HTTP route and no tool the model could call reached it
(MAPA_P1's own Lote 47 row: "la primitiva está lista y probada; falta el
endpoint... y, si se quiere exponer al modelo, la tool correspondiente").
This is that tool: one action-dispatched wrapper, paths confined the same
way `read_file`/`write_file` already are.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict

from src import spreadsheet as ss

logger = logging.getLogger(__name__)

_MANAGE_SPREADSHEET_ACTIONS = frozenset(
    {"import_csv", "export_csv", "read_workbook", "write_range"}
)


def _args(content: Any) -> Dict[str, Any]:
    raw = (content or "").strip() if isinstance(content, str) else content
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _path(raw_path: str) -> str:
    from src.tool_execution import _resolve_tool_path

    return _resolve_tool_path(raw_path or "")


class ManageSpreadsheetTool:
    """`manage_spreadsheet` (ART-04): import/export CSV, preview and edit an
    XLSX range — every path confined to the allowed roots the same way
    `read_file`/`write_file` already are (`_resolve_tool_path`)."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        action = str(args.get("action") or "").strip().lower()
        if action not in _MANAGE_SPREADSHEET_ACTIONS:
            return {"error": f"manage_spreadsheet: `action` must be one of "
                             f"{sorted(_MANAGE_SPREADSHEET_ACTIONS)}, got {action!r}", "exit_code": 1}
        try:
            if action == "import_csv":
                text = args.get("text")
                if not isinstance(text, str) or not text:
                    return {"error": "manage_spreadsheet: `text` (CSV/TSV content) is required", "exit_code": 1}
                result = ss.import_csv_rows(text, dialect=str(args.get("dialect") or "excel"))
                n = len(result["rows"])
                out = f"Parsed {n} row(s)."
                if result["warnings"]:
                    out += f" {len(result['warnings'])} warning(s): " + "; ".join(result["warnings"][:5])
                return {"output": out, "exit_code": 0, **result}
            if action == "export_csv":
                rows = args.get("rows")
                if not isinstance(rows, list):
                    return {"error": "manage_spreadsheet: `rows` (a list of row lists) is required", "exit_code": 1}
                csv_text = ss.export_csv_rows(rows, allow_formulas=bool(args.get("allow_formulas")))
                return {"output": f"Exported {len(rows)} row(s) as CSV "
                                  f"({'formulas allowed' if args.get('allow_formulas') else 'formula injection blocked'}).",
                        "exit_code": 0, "csv": csv_text}
            if action == "read_workbook":
                path = _path(str(args.get("path") or ""))
                result = ss.read_workbook_preview(
                    path, max_rows=int(args.get("max_rows") or 200), sheet_name=str(args.get("sheet_name") or ""),
                )
                stale_total = sum(len(s["stale_formula_cells"]) for s in result["sheets"])
                out = f"{len(result['sheets'])} sheet(s) previewed."
                if stale_total:
                    out += f" {stale_total} formula cell(s) have no cached value — recalculate before trusting them."
                return {"output": out, "exit_code": 0, **result}
            # action == "write_range"
            path = _path(str(args.get("path") or ""))
            sheet_name = str(args.get("sheet_name") or "")
            start_cell = str(args.get("start_cell") or "")
            values = args.get("values")
            if not (sheet_name and start_cell and isinstance(values, list)):
                return {"error": "manage_spreadsheet: write_range needs `sheet_name`, `start_cell` "
                                 "and `values` (a 2D list)", "exit_code": 1}
            save_as = args.get("save_as")
            save_as_path = _path(str(save_as)) if save_as else None
            result = ss.write_range(path, sheet_name, start_cell, values, save_as=save_as_path)
            return {"output": f"Wrote {len(values)} row(s) into {sheet_name}!{start_cell}"
                              + (f", saved as {save_as_path}" if save_as_path else " (in place)."),
                    "exit_code": 0, **result}
        except ss.SpreadsheetError as exc:
            return {"error": f"manage_spreadsheet: {exc}", "exit_code": 1}
        except ValueError as exc:
            return {"error": f"manage_spreadsheet: {exc}", "exit_code": 1}
        except Exception as exc:  # noqa: BLE001 - tools never raise
            logger.warning("manage_spreadsheet failed: %s", exc, exc_info=True)
            return {"error": f"manage_spreadsheet: {type(exc).__name__}: {exc}", "exit_code": 1}
