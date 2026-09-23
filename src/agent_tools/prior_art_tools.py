"""agent_tools/prior_art_tools.py — the `prior_art` tool executor.

One tool, one `action` discriminator, over `src.prior_art` — reuse, adapt,
or write, with every repository name checked live before it reaches the
user:

    prior_art  rubric  {idea, stack?, license?, constraints?}  -> checklist
    prior_art  verify  {slate, target_license?, stack?}        -> verified report
    prior_art  search  {query, language?, limit?}              -> GitHub search
    prior_art  report  {id?, limit?}                           -> saved report(s)

A bare string (no `{`) is shorthand for `{"action": "rubric", "idea": <string>}`
— the common case of "is there prior art for X" needs no JSON from the model.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

_ACTIONS = ("rubric", "verify", "search", "report")


def _parse_args(content: Any) -> Dict[str, Any]:
    """JSON object as given; a bare string becomes a `rubric` shorthand."""
    if isinstance(content, dict):
        return dict(content)
    raw = (content or "").strip() if isinstance(content, str) else ""
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return {"action": "rubric", "idea": raw}


class PriorArtTool:
    """`prior_art` {action, ...}: rubric, verify, search, report."""

    async def execute(self, content: Any, ctx: dict) -> Dict[str, Any]:
        args = _parse_args(content)
        action = str(args.get("action") or "rubric").strip().lower()
        if action not in _ACTIONS:
            return {"error": f"prior_art: unknown action {action!r}; expected "
                             f"one of {', '.join(_ACTIONS)}", "exit_code": 1}

        try:
            from src import prior_art
        except Exception as exc:  # noqa: BLE001
            return {"error": f"prior_art: unavailable: {exc}", "exit_code": 1}

        owner = str((ctx or {}).get("owner") or "")
        project_id = str((ctx or {}).get("project_id") or "")

        try:
            if action == "rubric":
                idea = str(args.get("idea") or args.get("query") or "").strip()
                if not idea:
                    return {"error": "prior_art rubric: `idea` is required", "exit_code": 1}
                result = prior_art.rubric(
                    idea, stack=str(args.get("stack") or ""),
                    license=str(args.get("license") or ""),
                    constraints=str(args.get("constraints") or ""),
                )
                return {"output": result["instructions"], **result, "exit_code": 0}

            if action == "verify":
                slate = args.get("slate")
                if slate is None:
                    # tolerate a model that hands the slate's own fields at
                    # the top level instead of nesting them under "slate"
                    slate = {"idea": args.get("idea"), "components": args.get("components")}
                result = prior_art.verify(
                    slate, target_license=str(args.get("target_license") or args.get("license") or ""),
                    stack=str(args.get("stack") or ""), owner=owner, project_id=project_id,
                )
                return {"output": result.get("table", ""), **result}

            if action == "search":
                query = str(args.get("query") or "").strip()
                if not query:
                    return {"error": "prior_art search: `query` is required", "exit_code": 1}
                result = prior_art.search(
                    query, language=str(args.get("language") or ""),
                    limit=int(args.get("limit") or 8),
                    include_stale=bool(args.get("include_stale")),
                )
                summary = f"{len(result.get('results') or [])} result(s) for {query!r}"
                return {"output": summary, **result}

            if action == "report":
                report_id = str(args.get("id") or args.get("report_id") or "").strip()
                if report_id:
                    found = prior_art.report(report_id)
                    if found is None:
                        return {"error": f"prior_art report: no such report {report_id!r}", "exit_code": 1}
                    return {"output": (found.get("results") or {}).get("table") or f"report {report_id}",
                            "report": found, "exit_code": 0}
                rows = prior_art.reports(int(args.get("limit") or 20), owner=owner)
                return {"output": f"{len(rows)} report(s)", "reports": rows, "exit_code": 0}

        except Exception as exc:  # noqa: BLE001 - a tool call must never raise
            logger.warning("prior_art tool failed (%s): %s", action, exc, exc_info=True)
            return {"error": f"prior_art {action}: {type(exc).__name__}: {exc}", "exit_code": 1}

        return {"error": f"prior_art: unhandled action {action!r}", "exit_code": 1}


__all__ = ["PriorArtTool"]
