"""agent_tools/code_graph_tools.py — code_graph_* tool executors.

Thin dispatchers over `src.code_graph.query`/`semantic`: each `execute`
parses the tool's args (JSON object, or a bare string for the tool's single
required field — same convention `code_tools.py` uses), calls straight into
`src.code_graph`, and returns its dict unchanged (already the
`{"output", "exit_code", ...}` shape every tool returns). Workspace
confinement, index refresh and the char budget all live in
`src.code_graph.query`, once.
"""
import json
import logging
from typing import Any, Dict

from src import code_graph

logger = logging.getLogger(__name__)


def _args(content: str, *, first_key: str) -> Dict[str, Any]:
    raw = (content or "").strip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {first_key: raw} if raw else {}


def _catch(fn, *a, tool: str, **kw) -> Dict[str, Any]:
    try:
        return fn(*a, **kw)
    except ValueError as exc:
        return {"error": f"{tool}: {exc}", "exit_code": 1}
    except Exception as exc:  # noqa: BLE001 - a tool call never crashes the turn
        logger.warning("%s failed: %s", tool, exc)
        return {"error": f"{tool}: {exc}", "exit_code": 1}


class CodeGraphIndexTool:
    """`code_graph_index` {root?, force?}: (re)build the graph for a workspace."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content, first_key="root")
        return _catch(
            code_graph.index, str(args.get("root") or ""),
            force=bool(args.get("force")), project_id=str(args.get("project_id") or ""),
            tool="code_graph_index",
        )


class CodeGraphSearchTool:
    """`code_graph_search` {pattern|query, kinds?, limit?}: symbol search."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content, first_key="pattern")
        pattern = str(args.get("pattern") or args.get("query") or "").strip()
        if not pattern:
            return {"error": "code_graph_search: pattern is required", "exit_code": 1}
        kinds = args.get("kinds") or ()
        if isinstance(kinds, str):
            kinds = [k.strip() for k in kinds.split(",") if k.strip()]
        semantic = bool(args.get("semantic"))
        fn = code_graph.semantic_query if semantic else code_graph.search_graph
        arg0 = pattern
        kw: Dict[str, Any] = dict(
            workspace=str(args.get("root") or args.get("workspace") or ""),
            project_id=str(args.get("project_id") or ""),
            limit=int(args.get("limit") or 40),
        )
        if not semantic:
            kw["kinds"] = kinds
        return _catch(fn, arg0, tool="code_graph_search", **kw)


class CodeGraphTraceTool:
    """`code_graph_trace` {from, to, max_depth?}: BFS call/import path."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content, first_key="from")
        src = str(args.get("from") or args.get("src") or "").strip()
        dst = str(args.get("to") or args.get("dst") or "").strip()
        if not src or not dst:
            return {"error": "code_graph_trace: `from` and `to` are required", "exit_code": 1}
        return _catch(
            code_graph.trace_path, src, dst,
            workspace=str(args.get("root") or args.get("workspace") or ""),
            project_id=str(args.get("project_id") or ""),
            max_depth=int(args.get("max_depth") or 5),
            tool="code_graph_trace",
        )


class CodeGraphChangesTool:
    """`code_graph_changes` {base_ref?}: git diff -> affected symbols + callers."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content, first_key="base_ref")
        return _catch(
            code_graph.detect_changes, str(args.get("root") or args.get("workspace") or ""),
            base_ref=str(args.get("base_ref") or "HEAD"),
            project_id=str(args.get("project_id") or ""),
            tool="code_graph_changes",
        )


class CodeGraphImpactTool:
    """`code_graph_impact` {symbol?, base_ref?, depth?, include_history?}:
    what else can break and which tests to run, from a symbol or from the
    current git diff — plus files that historically change alongside the
    seed file(s) even without a call/import edge, unless include_history
    is explicitly false."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content, first_key="symbol")
        include_history = args.get("include_history")
        return _catch(
            code_graph.impact, str(args.get("symbol") or "").strip(),
            workspace=str(args.get("root") or args.get("workspace") or ""),
            project_id=str(args.get("project_id") or ""),
            base_ref=str(args.get("base_ref") or "HEAD"),
            depth=int(args.get("depth") or 3),
            include_history=True if include_history is None else bool(include_history),
            tool="code_graph_impact",
        )


class CodeGraphCochangesTool:
    """`code_graph_cochanges` {path, limit?}: files that historically change
    together with `path` in git history — a correlation signal the static
    call graph cannot see (config, templates, tests, i18n)."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content, first_key="path")
        path = str(args.get("path") or "").strip()
        if not path:
            return {"error": "code_graph_cochanges: path is required", "exit_code": 1}
        return _catch(
            code_graph.cochanges, path,
            workspace=str(args.get("root") or args.get("workspace") or ""),
            max_commits=int(args.get("max_commits") or 500),
            limit=int(args.get("limit") or 15),
            min_support=int(args.get("min_support") or 2),
            tool="code_graph_cochanges",
        )


class CodeGraphArchitectureTool:
    """`code_graph_architecture` {}: languages, routes, fan-in/out, hotspots."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content, first_key="root")
        return _catch(
            code_graph.get_architecture, str(args.get("root") or ""),
            project_id=str(args.get("project_id") or ""),
            tool="code_graph_architecture",
        )


class CodeGraphSnippetTool:
    """`code_graph_snippet` {symbol}: exactly one symbol's source lines."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content, first_key="symbol")
        symbol = str(args.get("symbol") or "").strip()
        if not symbol:
            return {"error": "code_graph_snippet: symbol is required", "exit_code": 1}
        return _catch(
            code_graph.snippet, symbol,
            workspace=str(args.get("root") or args.get("workspace") or ""),
            project_id=str(args.get("project_id") or ""),
            tool="code_graph_snippet",
        )
