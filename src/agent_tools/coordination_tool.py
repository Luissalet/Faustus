"""Product-native coordination on the existing project and chat context."""
from __future__ import annotations

import asyncio
import json

from src.coordination.bindings import CoordinationError, UnknownOutcome
from src.coordination.runtime import client, resolve_grant
from src.coordination.tools import parse


class CoordinationTool:
    def __init__(self, name):
        self.name = name

    async def execute(self, content, ctx):
        try:
            ctx = ctx or {}
            action, args = parse(self.name, content)
            grant = getattr(ctx.get("security_context"), "coordination_grant", None)
            if grant is None:
                grant = await asyncio.to_thread(resolve_grant, ctx.get("owner"), ctx.get("session_id"),
                                                project_id=ctx.get("project_id") or None,
                                                read_only=bool(getattr(ctx.get("tool_policy"), "read_only", False)))
            if grant.owner != ctx.get("owner") or grant.session_id != ctx.get("session_id"):
                raise CoordinationError("coordination grant belongs to another run")
            native_client = client(grant, disabled_tools=ctx.get("disabled_tools", ()),
                                   read_only=bool(getattr(ctx.get("tool_policy"), "read_only", False)))
            native_client.tool_policy = ctx.get("tool_policy")
            data = await native_client.execute(action, args)
            public = {k: v for k, v in data.items() if not k.startswith("_coordination_")}
            return {**data, "output": json.dumps(public, ensure_ascii=False), "exit_code": 0,
                    "untrusted_content": True}
        except (CoordinationError, ValueError, TypeError, KeyError) as exc:
            return {"error": str(exc), "exit_code": 1, "blocked": not isinstance(exc, UnknownOutcome),
                    **({"outcome_unknown": True, "effect_certainty": "unknown", "non_retryable": True}
                       if isinstance(exc, UnknownOutcome) else {})}
