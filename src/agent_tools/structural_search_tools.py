"""agent_tools/structural_search_tools.py — structural_search /
structural_rewrite tool executors.

Thin dispatchers over `src.structural_search` (an `ast-grep` wrapper): parse
the tool's args (JSON object, or a bare string for the single required
field — same convention `code_tools.py`/`code_graph_tools.py` use), call
straight into `src.structural_search`, and return its dict unchanged
(already the `{"output", "exit_code", ...}` shape every tool returns).
Path confinement, the ast-grep subprocess call and output caps all live in
`src.structural_search`, once.
"""
import json
import logging
from typing import Any, Dict

from src import structural_search

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


class StructuralSearchTool:
    """`structural_search` {pattern, lang, path?, max_results?, context?}:
    AST-pattern search via ast-grep."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content, first_key="pattern")
        pattern = str(args.get("pattern") or "").strip()
        lang = str(args.get("lang") or "").strip()
        if not pattern:
            return {"error": "structural_search: pattern is required", "exit_code": 1}
        return _catch(
            structural_search.search, pattern, lang, str(args.get("path") or ""),
            max_results=int(args.get("max_results") or 200),
            context=int(args.get("context") or 0),
            tool="structural_search",
        )


class StructuralRewriteTool:
    """`structural_rewrite` {pattern, rewrite, lang, path?, apply?}:
    AST-pattern rewrite via ast-grep -- apply=false (default) returns a
    diff preview without touching disk; apply=true writes it."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content, first_key="pattern")
        pattern = str(args.get("pattern") or "").strip()
        rewrite = args.get("rewrite")
        lang = str(args.get("lang") or "").strip()
        if not pattern:
            return {"error": "structural_rewrite: pattern is required", "exit_code": 1}
        if rewrite is None:
            return {"error": "structural_rewrite: rewrite is required", "exit_code": 1}
        apply = bool(args.get("apply"))
        fn = structural_search.rewrite_apply if apply else structural_search.rewrite_preview
        return _catch(
            fn, pattern, str(rewrite), lang, str(args.get("path") or ""),
            tool="structural_rewrite",
        )
