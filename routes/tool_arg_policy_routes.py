"""routes/tool_arg_policy_routes.py — admin API for argument-level tool
policy rules (src/tool_arg_policy.py).

    GET  /api/tool-arg-rules        -> {"rules": [...]}
    PUT  /api/tool-arg-rules        -> {"rules": [...]}   (replaces the whole list)
    POST /api/tool-arg-rules/test   -> {"tool": str, "args": {...}} -> decision

Admin-only, same gate as the other admin settings routes
(routes/agent_settings_routes.py, routes/command_guard_routes.py):
`core.middleware.require_admin`.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.middleware import require_admin
from src.settings import SettingsError, get_setting, update_settings
from src.tool_arg_policy import RuleError, evaluate, extract_tool_args, validate_rules

logger = logging.getLogger(__name__)


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


class SaveRulesRequest(BaseModel):
    rules: List[Dict[str, Any]]


class TestRuleRequest(BaseModel):
    tool: str
    args: Dict[str, Any] = {}


def setup_tool_arg_policy_routes() -> APIRouter:
    router = APIRouter(tags=["tool-arg-policy"])

    @router.get("/api/tool-arg-rules")
    async def get_tool_arg_rules(request: Request) -> Dict[str, Any]:
        require_admin(request)
        return {"rules": get_setting("tool_arg_rules", []) or []}

    @router.put("/api/tool-arg-rules")
    async def put_tool_arg_rules(request: Request, body: SaveRulesRequest):
        require_admin(request)
        try:
            checked = validate_rules(body.rules)
        except RuleError as exc:
            return _error(400, str(exc))
        try:
            update_settings({"tool_arg_rules": checked})
        except SettingsError as exc:
            return _error(400, str(exc))
        return {"rules": checked}

    @router.post("/api/tool-arg-rules/test")
    async def test_tool_arg_rule(request: Request, body: TestRuleRequest) -> Dict[str, Any]:
        require_admin(request)
        tool = (body.tool or "").strip()
        if not tool:
            return _error(400, "tool is required")
        args = body.args if isinstance(body.args, dict) else extract_tool_args(tool, body.args)
        decision = evaluate(tool, args)
        if decision is None:
            return {"allowed": True}
        return {
            "allowed": False,
            "action": decision.action,
            "rule_id": decision.rule_id,
            "arg": decision.arg,
            "op": decision.op,
            "value": decision.value,
            "message": decision.message(),
        }

    return router
