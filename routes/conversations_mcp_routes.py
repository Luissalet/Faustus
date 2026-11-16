"""GET /api/conversations-mcp/config — the client config for the conversations
MCP server (mcp_servers/conversations_server.py), ready to paste into Claude
Code, Codex or Cursor. The token is left as a placeholder: an API token from
Settings › API tokens goes there."""
from __future__ import annotations

import os
import sys
from typing import Any, Dict

from fastapi import APIRouter, Request

from core.middleware import require_admin
from src.runtime_paths import get_app_root


def client_config(base_url: str) -> Dict[str, Any]:
    script = os.path.join(get_app_root(), "mcp_servers", "conversations_server.py").replace("\\", "/")
    python = sys.executable.replace("\\", "/")
    env = {"FAUSTUS_URL": base_url.rstrip("/"), "FAUSTUS_TOKEN": "<API token de Ajustes › Tokens de API>"}
    entry = {"command": python, "args": [script], "env": env}
    return {
        "server": "faustus-conversations",
        "json": {"mcpServers": {"faustus-conversations": entry}},
        "claude_code": f'claude mcp add faustus-conversations -e FAUSTUS_URL={env["FAUSTUS_URL"]} '
                       f'-e FAUSTUS_TOKEN=<token> -- "{python}" "{script}"',
        "codex_toml": ("[mcp_servers.faustus-conversations]\n"
                       f'command = "{python}"\nargs = ["{script}"]\n'
                       f'env = {{ FAUSTUS_URL = "{env["FAUSTUS_URL"]}", FAUSTUS_TOKEN = "<token>" }}\n'),
        "tools": ["conversations_list", "conversation_get", "messages_read", "events_poll", "events_wait",
                  "messages_send"],
    }


def setup_conversations_mcp_routes() -> APIRouter:
    router = APIRouter(prefix="/api/conversations-mcp", tags=["conversations-mcp"])

    @router.get("/config")
    async def get_config(request: Request) -> Dict[str, Any]:
        require_admin(request)
        return client_config(str(request.base_url))

    return router
