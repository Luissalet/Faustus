"""context_overflow_tool.py — A15: the model's own way to re-acquire a body
`apply_midturn_pressure`/`spill_large_tool_results` spilled to disk.

A spilled tool result leaves an in-prompt stub of the shape
``[overflow id=<sha256> tool=... bytes=...]`` (see
`src.context_compactor._overflow_stub`). When the model needs the detail the
stub omitted, this tool re-reads the ORIGINAL body by that id — not a second
guess, not a re-run of the tool — and the read is charged against a
consultable per-session/run reacquisition log
(`src.context_overflow.reacquisitions_for`/`reacquisition_summary`) so a
report can show what compaction saved and what got paid back.

NOT YET REGISTERED in `src.tool_schemas.FUNCTION_TOOL_SCHEMAS` or
`src.agent_tools.TOOL_HANDLERS` — both are outside T8's owned files for this
lot (docs/spec/paridad/CONTRATO.md rule 6). The exact two-line diff each
needs is in `T8_wiring.md`; `tests/test_t8_wiring.py` proves it with an
`xfail(strict=True)` until the integrator applies it.
"""
from __future__ import annotations

import json
from typing import Any, Dict

from src import context_overflow
from src.owner_identity import effective_storage_owner

#: Registration entry for `src.tool_schemas.FUNCTION_TOOL_SCHEMAS` (see
#: T8_wiring.md) — kept here so the schema travels with its implementation.
TOOL_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "read_overflow",
        "description": (
            "Re-read the ORIGINAL body of a tool result that context compaction "
            "spilled to disk, by the id in its in-prompt stub "
            "(\"[overflow id=<sha256> ...]\"). Use this when you need a detail "
            "the stub's short preview omitted. The re-read is recorded with its "
            "cost (characters/estimated tokens) against this session's run."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content_sha256": {
                    "type": "string",
                    "description": "The overflow id from the stub, e.g. the "
                                    "64-hex-char value after \"overflow id=\".",
                }
            },
            "required": ["content_sha256"],
        },
    },
}


class ReadOverflowTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        raw = (content or "").strip()
        content_sha256 = raw
        if raw.startswith("{"):
            try:
                content_sha256 = str(json.loads(raw).get("content_sha256", "")).strip()
            except (json.JSONDecodeError, TypeError, ValueError):
                content_sha256 = ""
        if not content_sha256:
            return {"error": "read_overflow: content_sha256 is required", "exit_code": 1}

        session_id = str((ctx or {}).get("session_id") or "")
        run_id = str((ctx or {}).get("run_id") or "")
        # Rule 7: resolve the real owner, fail-closed empty never silently
        # substituted — LOCALHOST_BYPASS runs without a login and an empty
        # owner here would otherwise read as "nobody's data", not "everybody's".
        owner = effective_storage_owner((ctx or {}).get("owner"))
        if not session_id:
            return {"error": "read_overflow: no session in context", "exit_code": 1}

        import asyncio
        result = await asyncio.to_thread(
            context_overflow.read_overflow,
            session_id=session_id, content_sha256=content_sha256, run_id=run_id,
        )
        if result is None:
            return {
                "error": f"read_overflow: nothing stored under {content_sha256} "
                         f"for this session (expired, pruned, or wrong id)",
                "exit_code": 1,
            }
        return {
            "content": result["content"],
            "content_sha256": result["content_sha256"],
            "chars": result["chars"],
            "owner": owner,
        }
