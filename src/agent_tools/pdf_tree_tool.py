"""agent_tools/pdf_tree_tool.py — pdf_outline / pdf_read_section /
pdf_find_section: structural (table-of-contents) navigation of a long PDF,
over `src.pdf_tree`.

Same shape `code_graph_tools.py` uses for its own thin dispatchers: parse
args through the shared contract (JSON object; outline also accepts a bare path),
call straight into `src.pdf_tree`, return its result already shaped as
`{"output", "exit_code", ...}`. Workspace confinement, tree construction and
the char budget all live in `src.pdf_tree`, once.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from src import pdf_tree
from src.pdf_tool_contracts import PdfArgumentsError, parse_content

logger = logging.getLogger(__name__)


def _args(content: Any, tool: str):
    try:
        return parse_content(tool, content), None
    except PdfArgumentsError as exc:
        # Keep the existing failure envelope for malformed arguments.
        return None, {"error": f"{tool}: {exc}", "exit_code": 1, "error_class": "pdf_tree.error"}


def _catch(fn, *a, tool: str, **kw) -> Dict[str, Any]:
    try:
        return fn(*a, **kw)
    except pdf_tree.PdfTreeError as exc:
        return {"error": f"{tool}: {exc}", "exit_code": 1, "error_class": "pdf_tree.invalid"}
    except Exception as exc:  # noqa: BLE001 — a bad PDF is data, not a crash
        logger.warning("%s failed: %s", tool, exc)
        return {"error": f"{tool}: {exc}", "exit_code": 1, "error_class": "pdf_tree.error"}


class PdfOutlineTool:
    """`pdf_outline` {path, max_depth?}: build (or reuse the cached) tree
    and return it as compact indented text plus the structured nodes."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args, error = _args(content, "pdf_outline")
        if error:
            return error
        path = args["path"]
        max_depth = args.get("max_depth")

        def _run() -> Dict[str, Any]:
            tree = pdf_tree.build_tree(path)
            text = pdf_tree.format_outline_text(tree, max_depth=max_depth)
            return {
                "output": text,
                "exit_code": 0,
                "pages": tree["pages"],
                "source": tree["source"],
                "nodes": tree["nodes"],
            }

        return _catch(_run, tool="pdf_outline")


class PdfReadSectionTool:
    """`pdf_read_section` {path, node_id, max_chars?}: text of one node's
    page range, "[page N]" markers, page numbers taken from the tree — a
    node_id not in the current tree is refused rather than guessed at."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args, error = _args(content, "pdf_read_section")
        if error:
            return error
        path = args["path"]
        node_id = args["node_id"]
        def _run() -> Dict[str, Any]:
            section = pdf_tree.read_section(path, node_id, max_chars=args["max_chars"])
            header = f"[{section['id']}] {section['title']} (pp. {section['start_page']}-{section['end_page']})"
            body = header + "\n\n" + section["text"]
            return {
                "output": body,
                "exit_code": 0,
                "id": section["id"],
                "title": section["title"],
                "start_page": section["start_page"],
                "end_page": section["end_page"],
                "truncated": section["truncated"],
                **({"note": section["note"]} if section.get("note") else {}),
            }

        return _catch(_run, tool="pdf_read_section")


class PdfFindSectionTool:
    """`pdf_find_section` {path, query, limit?}: cheap title/keyword search
    over the tree's node titles (falls back to page text when the tree has
    no real structure) — use this to pick a node_id when the outline is
    long or the section name is only approximately known."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args, error = _args(content, "pdf_find_section")
        if error:
            return error
        path = args["path"]
        query = args["query"]
        def _run() -> Dict[str, Any]:
            limit = args["limit"]
            matches = pdf_tree.find_in_tree(path, query, limit=limit)
            if matches:
                lines = [
                    f"{m['id']}  {m['title']}  (pp. {m['start_page']}-{m['end_page']})" for m in matches
                ]
                text = "\n".join(lines)
            else:
                text = f"no section title matched '{query}'"
            return {"output": text, "exit_code": 0, "matches": matches}

        return _catch(_run, tool="pdf_find_section")
