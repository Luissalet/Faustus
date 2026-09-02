"""LOCALHOST_BYPASS=true: the loopback caller acts as the first admin.

Seen live on the 7001 test instance: with the bypass on, the middleware let
every loopback request through with NO user on it, so every route with its
own `get_current_user` check (email unread/urgency state, research, projects,
cookbook, diagnostics…) answered 401/403; the SPA's global 401 handler sent
the browser to /login, which the bypass sent straight back to / — a reload
loop, the app unusable in the very mode meant for local development.

The probe boots the real AuthMiddleware from app.py in a subprocess (app.py
has import-time side effects), like tests/test_auth_root_path.py.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_bypass_puts_the_first_admin_on_the_request_for_loopback_only(tmp_path):
    env = os.environ.copy()
    env.update({
        "AUTH_ENABLED": "true",
        "LOCALHOST_BYPASS": "true",
        "CHROMADB_CONNECT_TIMEOUT": "0.01",
        "CHROMADB_HOST": "127.0.0.1",
        "CHROMADB_PORT": "9",
        "DATABASE_URL": f"sqlite:///{tmp_path / 'app.db'}",
        "ODYSSEUS_DATA_DIR": str(tmp_path),
        "ODYSSEUS_DISABLE_MCP": "1",
        "OPENAI_API_KEY": "",
        "PYTHONPATH": str(ROOT),
        "PYTHON_DOTENV_DISABLED": "1",
    })
    probe = textwrap.dedent(
        """
        import asyncio
        import json

        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        import app as app_module


        class _AuthManager:
            is_configured = True

            def __init__(self, users):
                self.users = users

            @staticmethod
            def validate_token(_token):
                return False

            @staticmethod
            def get_username_for_token(_token):
                return None


        async def _case(users, client_host, headers=()):
            manager = _AuthManager(users)
            app_module.auth_manager = manager
            seen = []

            async def endpoint(request):
                seen.append(getattr(request.state, "current_user", "<unset>"))
                return JSONResponse({"reached": True})

            downstream = Starlette(routes=[Route("/api/email/unread-state", endpoint)])
            downstream.state.auth_manager = manager
            middleware = app_module.AuthMiddleware(downstream)
            scope = {
                "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
                "path": "/api/email/unread-state", "raw_path": b"/api/email/unread-state",
                "root_path": "", "query_string": b"",
                "headers": [(k.encode(), v.encode()) for k, v in headers],
                "client": (client_host, 4321), "server": ("testserver", 80), "app": downstream,
            }
            sent = []
            request_sent = False

            async def receive():
                nonlocal request_sent
                if request_sent:
                    return {"type": "http.disconnect"}
                request_sent = True
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                sent.append(message)

            await middleware(scope, receive, send)
            start = next(m for m in sent if m["type"] == "http.response.start")
            return {"status": start["status"], "user": seen[0] if seen else None}


        async def main():
            users = {"viewer": {"is_admin": False}, "luis": {"is_admin": True}, "other": {"is_admin": True}}
            print("RESULT=" + json.dumps({
                "loopback": await _case(users, "127.0.0.1"),
                "loopback_v6": await _case(users, "::1"),
                "first_user_when_no_admin": await _case({"solo": {}}, "127.0.0.1"),
                "no_users": await _case({}, "127.0.0.1"),
                "remote": await _case(users, "192.0.2.10"),
                "proxied_loopback": await _case(users, "127.0.0.1", [("x-forwarded-for", "203.0.113.5")]),
            }, sort_keys=True))


        asyncio.run(main())
        """
    )
    result = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    line = next((ln for ln in result.stdout.splitlines() if ln.startswith("RESULT=")), None)
    assert line is not None, result.stdout
    got = json.loads(line[len("RESULT="):])
    # the first ADMIN in creation order, not the first user
    assert got["loopback"] == {"status": 200, "user": "luis"}
    assert got["loopback_v6"] == {"status": 200, "user": "luis"}
    # no admin yet: the first user; no users at all: nothing to act as
    assert got["first_user_when_no_admin"] == {"status": 200, "user": "solo"}
    assert got["no_users"]["user"] in (None, "<unset>")
    # the bypass never widens beyond a direct loopback connection
    assert got["remote"]["status"] == 401 and got["remote"]["user"] is None
    assert got["proxied_loopback"]["status"] == 401 and got["proxied_loopback"]["user"] is None
