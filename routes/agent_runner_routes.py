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
import shutil
from typing import Any, AsyncIterator, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.middleware import require_admin

logger = logging.getLogger(__name__)

#: Bound on one `ollama launch` run started from the page.
LAUNCH_TIMEOUT_S = 900.0
#: Longest single output line forwarded to the browser.
LINE_CHARS = 2000


class LaunchBody(BaseModel):
    config_only: Optional[bool] = None
    model: Optional[str] = None


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
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        assert proc.stdout is not None
        while True:
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=LAUNCH_TIMEOUT_S)
            except asyncio.TimeoutError:
                yield "data: " + json.dumps({"event": "error", "runner": key,
                                             "message": f"`{argv[0]} launch` produced nothing for "
                                                        f"{int(LAUNCH_TIMEOUT_S)}s — stopped"}) + "\n\n"
                break
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip("\n")[:LINE_CHARS]
            yield "data: " + json.dumps({"event": "output", "runner": key, "line": line}) + "\n\n"
        code = await proc.wait()
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
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
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
