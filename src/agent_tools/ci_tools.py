"""agent_tools/ci_tools.py -- `ci_failures`: what really broke, from the
workflow run's own logs, not just "the build failed" (lot C).

Thin executor over `src.ci_failures`: resolves the GitHub repo from the
workspace's `git remote`, reads the latest failed run (or one named by
`run_id`), extracts pytest/jest/tsc/eslint/cargo/go/npm/generic failure
blocks from the failed jobs' logs, and maps each one back onto the actual
file in the workspace (who last touched it, and -- best effort -- which
code-graph area it belongs to). Read-only and network, same class as
`web_fetch`/`reach_read`.
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


class CiFailuresTool:
    """`ci_failures`: which files broke the last (or a given) GitHub Actions
    run, with the concrete error, who last touched that file, and -- when
    `propose` is set and a utility model is configured -- a ranked guess at
    cause and fix. Read-only, network."""

    async def execute(self, content: str, ctx: dict) -> dict:
        from src import ci_failures

        args = _args(content)
        ctx = ctx or {}
        workspace = str(ctx.get("workspace") or args.get("workspace") or "").strip()
        if not workspace:
            return {"error": "ci_failures: no workspace to resolve the GitHub repo from", "exit_code": 1}
        owner = str(ctx.get("owner") or "")
        branch = str(args.get("branch") or "").strip() or None
        run_id_raw = args.get("run_id")
        try:
            run_id = int(run_id_raw) if run_id_raw not in (None, "") else None
        except (TypeError, ValueError):
            run_id = None
        try:
            limit = max(1, min(int(args.get("limit") or 20), 100))
        except (TypeError, ValueError):
            limit = 20

        try:
            analysis = await ci_failures.analyze(workspace, run_id=run_id, branch=branch)
        except ci_failures.CiFailuresError as exc:
            return {"error": f"ci_failures: {exc}", "exit_code": 1}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"ci_failures: {exc}", "exit_code": 1}

        fixes = None
        if args.get("propose"):
            try:
                fixes = await ci_failures.propose_fixes(analysis, owner=owner)
            except Exception as exc:  # noqa: BLE001
                logger.debug("ci_failures: propose_fixes failed: %s", exc)
                fixes = []

        shown = analysis.blocks[:limit]
        result: Dict[str, Any] = {
            "output": analysis.summary_md(),
            "exit_code": 0,
            "owner": analysis.owner,
            "repo": analysis.repo,
            "run": {k: analysis.run.get(k) for k in
                    ("id", "head_branch", "conclusion", "status", "html_url", "created_at")},
            "failure_count": len(analysis.blocks),
            "failures": shown,
            "from_cache": analysis.from_cache,
        }
        if fixes is not None:
            result["proposed_fixes"] = fixes
        return result
