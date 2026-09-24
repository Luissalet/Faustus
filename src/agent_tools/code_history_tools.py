"""agent_tools/code_history_tools.py — `code_history` tool executor.

Thin dispatcher over `src.code_history`: parses the tool's args (JSON object,
or a bare string taken as `path` — same convention `code_graph_tools.py`
uses), confines `workspace` to the turn's active workspace the same way
`grep`/`glob`/`find_symbol` do, and calls straight into `src.code_history`.
Read-only; never raises past `execute`.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict

from src import code_history

logger = logging.getLogger(__name__)

_MODES = {"explain", "file", "symbol", "blame", "co_change", "risk"}


def _args(content: Any) -> Dict[str, Any]:
    raw = (content or "").strip() if isinstance(content, str) else content
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    if isinstance(raw, str) and raw.startswith("{"):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {"path": raw}
    return {"path": raw} if isinstance(raw, str) else {}


def _workspace(args: Dict[str, Any]) -> str:
    from src.tool_execution import _resolve_search_root
    return _resolve_search_root(str(args.get("workspace") or ""))


class CodeHistoryTool:
    """`code_history`: who changed a file/symbol, how often, with which
    commits, what else usually changes with it, and the risk that implies."""

    async def execute(self, content: Any, ctx: dict = None) -> dict:
        args = _args(content)
        try:
            workspace = _workspace(args)
        except ValueError as exc:
            return {"error": f"code_history: {exc}", "exit_code": 1}

        path = str(args.get("path") or "").strip()
        if not path:
            return {"error": "code_history: `path` is required", "exit_code": 1}
        symbol = str(args.get("symbol") or "").strip() or None
        mode = str(args.get("mode") or ("symbol" if symbol else "explain")).strip().lower()
        if mode not in _MODES:
            mode = "explain"
        try:
            limit = int(args.get("limit")) if args.get("limit") is not None else None
        except (TypeError, ValueError):
            limit = None

        try:
            if mode == "file":
                result = code_history.file_history(workspace, path, limit=limit or 30)
            elif mode == "symbol":
                if not symbol:
                    return {"error": "code_history: mode=symbol requires `symbol`", "exit_code": 1}
                result = code_history.symbol_history(workspace, path, symbol, limit=limit or 20)
            elif mode == "blame":
                result = code_history.blame_summary(
                    workspace, path,
                    start=args.get("start"), end=args.get("end"),
                )
            elif mode == "co_change":
                result = code_history.co_change(workspace, path, limit=limit or 200)
            elif mode == "risk":
                result = code_history.risk(workspace, path)
            else:
                result = code_history.explain(workspace, path, symbol=symbol)
        except Exception as exc:  # noqa: BLE001 - a tool call never crashes the turn
            logger.warning("code_history tool failed: %s", exc)
            return {"error": f"code_history: {exc}", "exit_code": 1}

        if "error" not in result:
            result.setdefault("exit_code", 0)
            result.setdefault("output", result.get("summary_md") or f"code_history[{mode}] for {path}")
        else:
            result.setdefault("exit_code", 1)
        return json.loads(json.dumps(result, default=str))
