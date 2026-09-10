"""agent_tools/code_tools.py — find_symbol / callers / tests_for.

Thin executors over `src.code_index` (IDX-02/IDX-03, Lote 38): the agent's
read-only symbol-navigation tools, confined to the active workspace roots the
same way grep/glob/ls already are (`_resolve_search_root`), so "where is X
defined", "who calls X" and "what tests X" can be answered without reading a
repository one file at a time — the failure QA-02 names.

Each tool refreshes the index (incremental — see `code_index.refresh`) before
answering, so a result reflects the files as they are on disk right now
rather than whatever was last indexed.
"""

import json
import logging
from typing import Any, Dict

from src import code_index

logger = logging.getLogger(__name__)


def _args(content: str, *, first_key: str) -> Dict[str, Any]:
    """Accept a JSON object (the native function-call form) or, as a
    convenience for a text-fenced call, a bare string for `first_key` — the
    same permissive parsing `GrepTool`/`GlobTool` use."""
    raw = (content or "").strip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {first_key: raw} if raw else {}


def _root(raw_path: str) -> str:
    from src.tool_execution import _resolve_search_root
    return _resolve_search_root(raw_path or "")


class FindSymbolTool:
    """`find_symbol`: definition site(s) for a name, with evidence."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content, first_key="name")
        name = str(args.get("name") or "").strip()
        if not name:
            return {"error": "find_symbol: name is required", "exit_code": 1}
        try:
            root = _root(str(args.get("path") or args.get("workspace") or ""))
        except ValueError as exc:
            return {"error": f"find_symbol: {exc}", "exit_code": 1}
        project_id = str(args.get("project_id") or "")
        code_index.refresh(root, project_id=project_id)
        hits = code_index.find_definition(
            name, workspace=root, project_id=project_id, kind=str(args.get("kind") or ""))
        if not hits:
            return {"output": f"No definition found for {name!r} under {root}",
                    "exit_code": 0, "symbols": []}
        lines = [f"{h['path']}:{h['start_line']}-{h['end_line']}  {h['kind']} {h['qualname']}"
                 for h in hits]
        return {"output": "\n".join(lines), "exit_code": 0, "symbols": hits}


class CallersTool:
    """`callers`: lexical call sites for a name, with file and line."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content, first_key="name")
        name = str(args.get("name") or "").strip()
        if not name:
            return {"error": "callers: name is required", "exit_code": 1}
        try:
            root = _root(str(args.get("path") or args.get("workspace") or ""))
        except ValueError as exc:
            return {"error": f"callers: {exc}", "exit_code": 1}
        project_id = str(args.get("project_id") or "")
        code_index.refresh(root, project_id=project_id)
        try:
            limit = int(args.get("limit") or 200)
        except (TypeError, ValueError):
            limit = 200
        hits = code_index.find_callers(name, workspace=root, project_id=project_id, limit=limit)
        if not hits:
            return {"output": f"No callers found for {name!r} under {root}",
                    "exit_code": 0, "callers": []}
        lines = [f"{h['path']}:{h['line']}" for h in hits]
        return {"output": "\n".join(lines), "exit_code": 0, "callers": hits}


class TestsForTool:
    """`tests_for`: candidate test files for a symbol or path, by convention."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content, first_key="symbol_or_path")
        target = str(args.get("symbol_or_path") or args.get("name") or args.get("path") or "").strip()
        if not target:
            return {"error": "tests_for: symbol_or_path is required", "exit_code": 1}
        try:
            root = _root(str(args.get("path") or args.get("workspace") or ""))
        except ValueError as exc:
            return {"error": f"tests_for: {exc}", "exit_code": 1}
        project_id = str(args.get("project_id") or "")
        code_index.refresh(root, project_id=project_id)
        hits = code_index.tests_for(target, workspace=root, project_id=project_id)
        if not hits:
            return {"output": f"No test file found for {target!r} under {root}",
                    "exit_code": 0, "tests": []}
        lines = [f"{h['path']}:{h['line']} ({h['reason']})" for h in hits]
        return {"output": "\n".join(lines), "exit_code": 0, "tests": hits}
