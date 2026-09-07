"""Agent runners API — /api/agent-runners/* (src/agent_runners.py).

  GET  /api/agent-runners                the catalogue: every agent this
                                         machine's Ollama knows, merged with
                                         Faustus's own table — label, aliases,
                                         licence word, installed or not, the
                                         `ollama launch` command, and whether
                                         it can be a worker at all
                                         (`?versions=1` also probes --version,
                                         `?refresh=1` re-reads the live help)
  GET  /api/agent-runners/{key}          one of them, with its launch command,
                                         the argv that would run one task and
                                         the note that says what it is
  POST /api/agent-runners/{key}/launch   run `ollama launch <key> -y` (or
                                         `--config`) and stream its output

Admin-only, all three: the catalogue names software on the operator's machine,
and the launch INSTALLS some. The POST is additionally in the app_api
blocklist (src/tools/system.py `_APP_API_BLOCKLIST_METHOD_PATH`): `app_api`
loops back with the internal-tool token, which `require_admin` accepts with no
cookie and no approval card, and installing software must never be reachable
that way. The GETs stay open there — reading what is installed is exactly what
a model should be able to tell the user.

The honesty this page exists to print: **Faustus's command guard cannot see
inside another agent's own shell.** Every payload here carries that sentence
(`guard_note`), and a job that uses one of these runners carries it into its
proof (src/dispatch.py, `external_agent_unguarded`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
from typing import Any, AsyncIterator, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.middleware import require_admin, require_human

logger = logging.getLogger(__name__)

#: Bound on one `ollama launch` run started from the page.
LAUNCH_TIMEOUT_S = 900.0
#: Longest single output line forwarded to the browser.
LINE_CHARS = 2000


class LaunchBody(BaseModel):
    config_only: Optional[bool] = None
    model: Optional[str] = None


class ClientModelBody(BaseModel):
    billing_mode: str
    model: str = 'client-default'


def _payload(*, versions: bool = False, refresh: bool = False) -> Dict[str, Any]:
    from src import agent_runners as reg
    if refresh:
        reg.reset_cache()
        reg.help_text(refresh=True)
    return {"status": "success", **reg.summary(versions=versions)}


async def _launch_stream(argv: list, key: str) -> AsyncIterator[str]:
    """`ollama launch …` as server-sent events, in the dialect the rest of the
    app streams with (`data: {json}` per line, a final `event: end`) — the same
    one routes/dispatch_routes.py uses, so one page can read both."""
    yield "data: " + json.dumps({"event": "started", "runner": key,
                                 "command": " ".join(argv)}) + "\n\n"
    proc = None
    code: Optional[int] = None
    deadline = time.monotonic() + LAUNCH_TIMEOUT_S
    try:
        process_options = ({"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
                           if os.name == "nt" else {"start_new_session": True})
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            **process_options))
        try:
            proc = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            # Keep the process handle even if the HTTP stream disappears while
            # Windows is still starting it; finally owns its cleanup.
            proc = await spawn
            raise
        assert proc.stdout is not None
        while True:
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(),
                                            timeout=max(0.001, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                yield "data: " + json.dumps({"event": "error", "runner": key,
                                             "message": f"`{argv[0]} launch` exceeded its time limit of "
                                                        f"{int(LAUNCH_TIMEOUT_S)}s — stopped"}) + "\n\n"
                break
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip("\n")[:LINE_CHARS]
            yield "data: " + json.dumps({"event": "output", "runner": key, "line": line}) + "\n\n"
        if proc.returncode is None:
            try:
                code = await asyncio.wait_for(proc.wait(), timeout=max(0.001, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                pass
        else:
            code = proc.returncode
    except FileNotFoundError:
        yield "data: " + json.dumps({"event": "error", "runner": key,
                                     "message": "ollama is not installed on this machine"}) + "\n\n"
    except Exception as e:  # noqa: BLE001 - a stream reports, it does not 500 halfway
        logger.debug("agent runners: launch of %s failed: %s", key, e)
        yield "data: " + json.dumps({"event": "error", "runner": key,
                                     "message": f"{type(e).__name__}: {e}"[:300]}) + "\n\n"
    finally:
        if proc is not None and proc.returncode is None:
            try:
                # The installer can spawn helpers. Disconnect/timeout must not
                # leave them running, or await an unbounded proc.wait().
                from src.agent_tools.subprocess_tools import _kill_tree
                await asyncio.to_thread(_kill_tree, proc)
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:  # noqa: BLE001
                logger.warning("agent runner installer cleanup could not be confirmed")
    installed = False
    try:
        from src import agent_runners as reg
        runner = reg.get(key)
        installed = bool(runner and reg.to_row(runner)["installed"])
    except Exception:  # noqa: BLE001
        installed = False
    yield "event: end\ndata: " + json.dumps({"event": "end", "runner": key, "exit_code": code,
                                             "installed": installed}) + "\n\n"


def setup_agent_runner_routes() -> APIRouter:
    router = APIRouter(prefix="/api/agent-runners", tags=["agent-runners"])

    @router.get("")
    async def list_runners(request: Request,
                           versions: int = Query(default=0),
                           refresh: int = Query(default=0),
                           _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        """Every agent runner: the live `ollama launch --help` list merged with
        Faustus's own table, with `installed` resolved over this machine's
        PATH. Never fails: with no Ollama it answers the built-in table."""
        return await asyncio.to_thread(_payload, versions=bool(versions), refresh=bool(refresh))

    @router.get("/{key}")
    async def read_runner(key: str, versions: int = Query(default=0),
                          _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        from src import agent_runners as reg
        runner = await asyncio.to_thread(reg.get, key)
        if runner is None:
            raise HTTPException(status_code=404, detail="no such agent runner")
        row = await asyncio.to_thread(reg.to_row, runner, versions=bool(versions))
        row["launch_config_command"] = " ".join(reg.launch_argv(runner.key, config_only=True))
        return {"status": "success", "runner": row, "enabled": reg.enabled(),
                "timeout_s": reg.timeout_s(), "guard_note": reg.GUARD_NOTE}

    @router.get("/{key}/connection")
    async def connection(key: str, _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        """Explicit read-only check; listing runners never starts an auth probe."""
        from src.runner_connections import AUTH_COMMANDS, connection_status
        if key not in AUTH_COMMANDS:
            raise HTTPException(status_code=404, detail="No verified connection diagnostic for this runner")
        return {"status": "success", "connection": await connection_status(key)}

    @router.post("/{key}/model")
    async def connect_model(key: str, body: ClientModelBody, request: Request,
                            _human: None = Depends(require_human)) -> Dict[str, Any]:
        """Human-only opt-in to text inference; never enables all runners."""
        import secrets
        import uuid
        from core.database import ModelEndpoint, SessionLocal
        from routes.workspace_routes import _reject_cross_origin
        from src.auth_helpers import get_current_user
        from src import agent_runners as reg
        from src.runner_billing import prepare, verify
        _reject_cross_origin(request)
        if key != 'claude':
            raise HTTPException(400, 'This client does not yet have a verified text-only chat adapter')
        if not reg.enabled():
            raise HTTPException(409, 'Enable external agent runners explicitly in Settings first')
        model = body.model.strip()
        if not model or len(model) > 200 or model.startswith('-') or any(c in model for c in '\r\n\0'):
            raise HTTPException(400, 'Invalid client model identifier')
        owner = get_current_user(request)
        if not owner:
            # NULL-owner endpoints are shared; this authority must be private.
            raise HTTPException(403, 'Sign in to add a private official-client connection')
        try:
            _, env = prepare(key, body.billing_mode, [], reg.build_env(reg.get(key, help_source='')))
            facts = await asyncio.to_thread(verify, key, body.billing_mode, env)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(503, 'Official-client authentication could not be checked; no model added') from exc
        def persist():
            # The database primary key arbitrates concurrent/retried registrations.
            # Never rotate the capability or silently re-enable a revoked endpoint.
            from sqlalchemy.exc import IntegrityError
            identity = json.dumps(['faustus-official-cli-v1', owner, key, body.billing_mode, model])
            ident = uuid.uuid5(uuid.NAMESPACE_URL, identity).hex
            base_url = f'faustus-cli://claude/{body.billing_mode}/{ident}'
            reused = False
            with SessionLocal() as db:
                ep = db.get(ModelEndpoint, ident)
                if ep is None:
                    ep = ModelEndpoint(id=ident, name=f'Claude Code · {body.billing_mode}',
                        base_url=base_url, api_key=secrets.token_urlsafe(48), owner=owner, is_enabled=True,
                        endpoint_kind='official-cli', model_refresh_mode='manual', model_type='llm',
                        supports_tools=False, pinned_models=json.dumps([model]), cached_models=json.dumps([model]))
                    db.add(ep)
                    try:
                        db.commit()
                    except IntegrityError:
                        db.rollback()
                        ep = db.get(ModelEndpoint, ident)
                        if ep is None:
                            raise
                        reused = True
                else:
                    reused = True
                if reused:
                    from src.cli_model import authorize, ClientModelError
                    if ep.owner != owner or ep.base_url != base_url or ep.supports_tools:
                        raise HTTPException(409, 'This connection was modified; review it in Settings')
                    try:
                        authorize(base_url, model, {'Authorization': f'Bearer {ep.api_key}'})
                    except ClientModelError as exc:
                        raise HTTPException(409, 'This connection was disabled or modified; review it in Settings') from exc
            return {'status': 'success', 'endpoint_id': ident, 'model': model, 'billing': facts,
                    'text_only': True, 'tools': 'faustus', 'default_changed': False, 'reused': reused}
        # A SQLite lock must not stop all chats or prevent cancellation requests.
        return await asyncio.to_thread(persist)

    @router.post("/{key}/launch")
    async def launch(key: str, body: LaunchBody,
                     _admin: None = Depends(require_admin)) -> StreamingResponse:
        """Run `ollama launch <key> -y` (or `--config`) and stream its output.

        This INSTALLS software on the operator's machine, which is why it is a
        deliberate button and not something the catalogue does by itself — and
        why `app_api` is blocked from it.
        """
        from src import agent_runners as reg
        runner = reg.get(key)
        if runner is None:
            raise HTTPException(status_code=404, detail="no such agent runner")
        if not shutil.which("ollama"):
            raise HTTPException(status_code=400,
                                detail="ollama is not installed on this machine: `ollama launch` is "
                                       "what installs and configures these agents")
        argv = reg.launch_argv(runner.key, model=(body.model or None),
                               config_only=bool(body.config_only))
        if not argv:
            raise HTTPException(status_code=400, detail="no launch command for that runner")
        return StreamingResponse(
            _launch_stream(argv, runner.key), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    return router
