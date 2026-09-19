"""agent_tools/doc_claims_tool.py — `doc_claims_check` tool executor.

Thin dispatcher over `src.doc_claims.report`, mirroring the code_graph_*
executors in `code_graph_tools.py`: parse args (JSON object, or a bare
string for the tool's one commonly-used field), call straight into
`src.doc_claims`, and return its dict wrapped in the usual
`{"output", "exit_code", ...}` tool-result shape.
"""
import json
import logging
from typing import Any, Dict

from src import doc_claims

logger = logging.getLogger(__name__)


def _args(content: str) -> Dict[str, Any]:
    raw = (content or "").strip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    if raw:
        return {"docs": [d.strip() for d in raw.split(",") if d.strip()]}
    return {}


class DocClaimsCheckTool:
    """`doc_claims_check` {docs?, root?}: doc-claim drift report for a
    workspace's Markdown docs (default FAUSTUS.md, README.md)."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        root = str(args.get("root") or args.get("workspace") or "")
        docs = args.get("docs") or None
        try:
            data = doc_claims.report(root or ".", docs)
        except Exception as exc:  # noqa: BLE001 - a tool call never crashes the turn
            logger.warning("doc_claims_check failed: %s", exc)
            return {"error": f"doc_claims_check: {exc}", "exit_code": 1}
        text = doc_claims.render_report(data)
        return {"output": text, "exit_code": 0, "report": data}
