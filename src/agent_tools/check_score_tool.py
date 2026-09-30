"""agent_tools/check_score_tool.py — `check_score`: a coverage-aware score for a list of checks.

A pure calculation over `src.check_scoring`: no file, network or shell access.
A review agent lists what it checked (status and severity per check) and gets
back health, coverage, whether a score may be stated at all, the failed
blocking checks and what was not checked.
"""
from __future__ import annotations

import json
from typing import Any, Dict

from src import check_scoring


def _args(content: Any) -> Dict[str, Any]:
    raw = (content or "").strip() if isinstance(content, str) else content
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, list):
        return {"checks": raw}
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if isinstance(parsed, list):
        return {"checks": parsed}
    return parsed if isinstance(parsed, dict) else {}


class CheckScoreTool:
    """`check_score` {checks: [{name, status, severity, note?}]}."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        checks = args.get("checks")
        if not isinstance(checks, list) or not checks:
            return {"error": "check_score: `checks` must be a non-empty list of "
                             "{name, status, severity} objects", "exit_code": 1}
        result = check_scoring.score_checks(checks)
        payload = dict(result)
        payload["output"] = check_scoring.markdown_block(result)
        payload["exit_code"] = 0
        return payload
