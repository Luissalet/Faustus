"""`read_artifact`: the model's way back into an offloaded tool result.

`src/tool_result_offload.py` (A12) stores an oversized tool result whole in
the artifact store and leaves the model a bounded summary plus an
`artifact_id`. This tool lets the model open that artifact by character range
or by substring query — scoped to the owner exactly like the HTTP routes, so
another tenant's id reads as "not found" (A13).
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

TOOL_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "read_artifact",
        "description": (
            "Read back the full text of an artifact by id — a character range "
            "(start/end, 0-based, end exclusive) or a `query` substring with "
            "surrounding context. Use it to read past a "
            "'[... chars omitted; open the artifact ...]' truncation left in an "
            "earlier tool result."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string"},
                "start": {"type": "integer", "description": "0-based start offset (range mode)."},
                "end": {"type": "integer", "description": "Exclusive end offset (range mode)."},
                "query": {"type": "string", "description": "Case-insensitive substring to locate instead of a range."},
            },
            "required": ["artifact_id"],
        },
    },
}


class ReadArtifactTool:
    async def execute(self, content: Any, ctx: Optional[dict] = None) -> dict:
        from src.owner_identity import effective_storage_owner
        from src.tool_result_offload import ArtifactAccessDenied, read_artifact_range

        raw = content if isinstance(content, str) else json.dumps(content or {})
        try:
            args = json.loads(raw) if raw.strip() else {}
        except (TypeError, ValueError):
            args = {"artifact_id": raw.strip()}
        if not isinstance(args, dict):
            args = {"artifact_id": str(args)}
        artifact_id = str(args.get("artifact_id") or "").strip()
        if not artifact_id:
            return {"error": "read_artifact requires `artifact_id`.", "exit_code": 1}
        ctx = ctx or {}
        owner = effective_storage_owner(ctx.get("owner"), auth_is_disabled=None) or ""
        if not owner:
            return {"error": "artifact not found", "exit_code": 1}
        query = args.get("query")
        try:
            start = int(args.get("start") or 0)
        except (TypeError, ValueError):
            start = 0
        end = args.get("end")
        try:
            end = int(end) if end not in (None, "") else None
        except (TypeError, ValueError):
            end = None
        try:
            return read_artifact_range(
                artifact_id, owner=owner,
                start=start if query is None else 0,
                end=end if query is None else None,
                query=str(query) if query is not None else None,
            )
        except ArtifactAccessDenied:
            # Same words for "not yours" and "does not exist" (A13).
            return {"error": "artifact not found", "exit_code": 1}
