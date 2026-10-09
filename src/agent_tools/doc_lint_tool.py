"""agent_tools/doc_lint_tool.py -- `doc_lint` tool executor.

Read-only dispatcher over `src.doc_lint.lint_paths`. Paths go through the
same workspace guard the file tools use (`_resolve_search_root`), so a path
outside the workspace / project roots is refused per file and never read.
The linter only reports; it never rewrites a document.
"""
import json
import logging
from typing import Any, Dict

from src import doc_lint

logger = logging.getLogger(__name__)

MAX_PATHS = 20


def _args(content: str) -> Dict[str, Any]:
    raw = (content or "").strip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    if raw:
        return {"paths": [p.strip() for p in raw.split(",") if p.strip()]}
    return {}


def _as_list(value: Any) -> list:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


class DocLintTool:
    """`doc_lint` {paths|path, rules?}: report-only Markdown lint."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        paths = _as_list(args.get("paths")) or _as_list(args.get("path"))
        if not paths:
            return {"error": "doc_lint: give `path` or `paths` (Markdown files)", "exit_code": 1}
        if len(paths) > MAX_PATHS:
            return {"error": f"doc_lint: at most {MAX_PATHS} paths per call", "exit_code": 1}
        rules = _as_list(args.get("rules")) or None
        try:
            from src.tool_execution import _resolve_search_root
            report = doc_lint.lint_paths(paths, rules, resolver=_resolve_search_root)
        except ValueError as exc:  # unknown rule name
            return {"error": f"doc_lint: {exc}", "exit_code": 1}
        except Exception as exc:  # noqa: BLE001 - a tool call never crashes the turn
            logger.warning("doc_lint failed: %s", exc)
            return {"error": f"doc_lint: {exc}", "exit_code": 1}
        return {"output": doc_lint.render_report(report), "exit_code": 0, "report": report}
