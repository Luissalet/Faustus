"""agent_tools/bug_hunt_tools.py — `bug_hunt`: an autonomous bug hunter.

Thin executor over `src.bug_hunt`. Effect: it writes generated test files
under `<workspace>/.faustus/bughunt/` and, when `keep_tests` is true, under
`<workspace>/tests/` too — the same class of side effect as `write_file`
(ToolEffect.WRITE_WORKSPACE), so the existing approval policy for a write
applies unchanged; it is never treated as read-only.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


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


class BugHuntTool:
    """`bug_hunt` {target, max_cases?, keep_tests?, run_only?}: understand a
    module/function, generate edge-case pytest tests for it, run them
    isolated, triage failures as real bug / wrong test / unclear, and return
    a structured report. With `run_only`, tests are generated and run but
    never triaged or kept (a quick "does it already break" pass)."""

    async def execute(self, content: str, ctx: dict) -> dict:
        from src import bug_hunt
        from src.tool_execution import _resolve_search_root

        args = _args(content)
        target = str(args.get("target") or "").strip()
        if not target:
            return {"error": "bug_hunt: 'target' is required (a file, dir, or path::symbol)", "exit_code": 1}
        try:
            workspace = _resolve_search_root(str(args.get("workspace") or ""))
        except ValueError as e:
            return {"error": f"bug_hunt: {e}", "exit_code": 1}
        owner = str((ctx or {}).get("owner") or "")
        keep_tests = bool(args.get("keep_tests", False))
        run_only = bool(args.get("run_only", False))
        max_cases = args.get("max_cases")
        try:
            max_cases = int(max_cases) if max_cases is not None else None
        except (TypeError, ValueError):
            max_cases = None

        try:
            report = await bug_hunt.hunt(
                workspace, target, owner=owner,
                keep_tests=keep_tests and not run_only,
                max_cases=max_cases,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[bug_hunt] hunt failed for %r: %s", target, e, exc_info=True)
            return {"error": f"bug_hunt: {e}", "exit_code": 1}

        payload = report.to_dict()
        if run_only:
            payload.pop("kept_tests_path", None)
        payload["output"] = report.to_markdown()
        payload["exit_code"] = 0
        return payload
