"""Handoff lanes API -- /api/handoff-lanes/* (src/handoff_lanes.py).

  GET  /api/handoff-lanes         the current lanes + mode
  PUT  /api/handoff-lanes         replace the lanes and/or the mode (validated;
                                  a malformed lane is rejected wholesale, none
                                  of it is saved)
  GET  /api/handoff-lanes/graph   {"nodes","edges","mode"} plus the same data
                                  as a Mermaid diagram source, for a Studio panel
  POST /api/handoff-lanes/test    {"from","to","tools"?,"depth"?} -> a dry-run
                                  Decision, without saving or logging anything

Admin-only: a lane is a machine-wide policy over every agent's delegation,
the same trust class as an agent definition itself (routes/agent_def_routes.py).
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from src import handoff_lanes
from src.auth_helpers import require_user

logger = logging.getLogger(__name__)


def _is_admin(owner: str) -> bool:
    try:
        from src import tool_security as ts
        return bool(ts.owner_is_admin_or_single_user(owner or None))
    except Exception:  # pragma: no cover
        return False


def _owner(request: Request) -> str:
    owner = require_user(request)
    if not _is_admin(owner):
        raise HTTPException(403, "handoff lanes govern every agent's delegation: admins only")
    return owner


async def _body(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "a JSON body is required")
    if not isinstance(body, dict):
        raise HTTPException(400, "the body must be a JSON object")
    return body


def setup_handoff_lanes_routes() -> APIRouter:
    router = APIRouter(prefix="/api/handoff-lanes", tags=["handoff-lanes"])

    @router.get("")
    async def get_lanes(request: Request):
        _owner(request)
        from src.settings import get_setting
        raw = get_setting("agent_handoff_lanes", [])
        try:
            lanes = handoff_lanes.validate(raw)
        except ValueError:
            lanes = []
        return {
            "lanes": lanes,
            "mode": handoff_lanes.normalize_mode(get_setting("agent_handoff_lanes_mode", "off")),
        }

    @router.put("")
    async def put_lanes(request: Request):
        _owner(request)
        body = await _body(request)
        try:
            lanes = handoff_lanes.validate(body.get("lanes"))
        except ValueError as e:
            raise HTTPException(400, str(e))
        mode = handoff_lanes.normalize_mode(body.get("mode", "off")) if "mode" in body else None
        from src.settings import save_settings, load_settings
        current = load_settings()
        current["agent_handoff_lanes"] = lanes
        if mode is not None:
            current["agent_handoff_lanes_mode"] = mode
        save_settings(current)
        return {
            "lanes": lanes,
            "mode": mode if mode is not None else
                    handoff_lanes.normalize_mode(current.get("agent_handoff_lanes_mode", "off")),
        }

    @router.get("/graph")
    async def graph(request: Request):
        _owner(request)
        data = handoff_lanes.graph_json()
        data["mermaid"] = handoff_lanes.mermaid(data["edges"])
        return data

    @router.post("/test")
    async def test(request: Request):
        _owner(request)
        body = await _body(request)
        frm = str(body.get("from") or "main")
        to = str(body.get("to") or "*")
        tools = body.get("tools") or []
        if not isinstance(tools, (list, tuple)):
            raise HTTPException(400, "'tools' must be a list of tool names")
        try:
            depth = max(1, int(body.get("depth") or 1))
        except (TypeError, ValueError):
            depth = 1
        decision = handoff_lanes.evaluate(frm, to, tools, depth)
        return decision.to_dict()

    return router
