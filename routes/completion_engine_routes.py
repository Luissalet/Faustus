"""HTTP for the Greedy Completion Engine: `/api/completion`.

Thin. Everything that decides anything is in `src/completion_engine/service.py`;
this resolves the caller, checks shapes, and re-shapes answers, following the
conventions the newer routers in this repository already keep:

* **A rejection is a 200 with `ok: false`** and an `error` of `{path, message,
  code}`. `4xx` is for a malformed body and for the 404 below.
* **Another owner's decision is a 404, never a 403.**
* **The owner comes from the session**, never from a body.
* **Literal paths are declared before `/{decision_id}`**, because FastAPI
  matches in declaration order and a path parameter first would swallow them.

What is different here, and it is the interesting part of this router: there is
no endpoint that RUNS anything. The engine decides inside a turn, at the moment
the model stops calling tools; everything here is a read of what it decided,
plus the two switches. A `POST /run` would be a second way to reach the
decision -- one with no turn behind it, no ledger, no proof and no budget --
and the answer it produced would look exactly like the real one.

The one write is `POST /{decision_id}/reject-improvement`, and it is
`require_human`. §12: a person can say no to an extra. A model that could
reject its own improvements could also quietly delete the record of having been
told to do them.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from core.middleware import require_admin, require_human
from src.auth_helpers import effective_user, get_current_user
from src.completion_engine import service as completion_service
from src.completion_engine.contracts import COMPLETION_MODES, STOP_REASONS, CompletionError
from src.completion_engine.service import CompletionServiceError, enabled, shadow_enabled
from src.contracts.base import ContractError
from src.owner_identity import effective_storage_owner

logger = logging.getLogger(__name__)

ROUTE_ERRORS = ("completion_engine_disabled", "not_stored")

STREAM_MAX_S = 900.0
STREAM_HEARTBEAT_S = 15.0
MAX_STREAM_EVENTS = 1000


def _refusal(path: str, message: str, *, code: str = "", **extra: Any) -> Dict[str, Any]:
    error: Dict[str, Any] = {"path": path, "message": message}
    if code:
        error["code"] = code
    body: Dict[str, Any] = {"ok": False, "error": error}
    body.update(extra)
    return body


def _from_error(exc: Exception, *, path: str = "") -> Dict[str, Any]:
    """A `ContractError` as a 200 rejection.

    `ContractError` and not `CompletionError`: the second is a SUBCLASS of the
    first, and the shared helpers in `src/contracts/base.py` -- which parse
    every field of every payload -- raise the parent. Catching only the child
    answered 500 to the commonest caller mistake there is, in the delta routes,
    a few hours before this file was written.
    """
    return _refusal(
        str(getattr(exc, "path", "") or path or "<root>"),
        str(getattr(exc, "message", "") or str(exc)),
        code=str(getattr(exc, "code", "") or "invalid_argument"),
    )


def _owner(request: Request) -> str:
    try:
        who = effective_user(request) or get_current_user(request) or ""
    except Exception:  # noqa: BLE001
        logger.debug("completion routes: the caller could not be resolved", exc_info=True)
        who = ""
    try:
        return str(effective_storage_owner(who) or "").strip()
    except Exception:  # noqa: BLE001
        return str(who or "").strip()


def _require_owner(request: Request) -> str:
    owner = _owner(request)
    if not owner:
        raise HTTPException(403, "no signed-in owner")
    return owner


async def _payload(request: Request) -> Dict[str, Any]:
    try:
        raw = await request.json()
    except Exception:
        raw = None
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise HTTPException(400, "the body must be a JSON object")
    return dict(raw)


def _int_arg(value: Any, default: int, *, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def setup_completion_engine_routes() -> APIRouter:
    router = APIRouter(prefix="/api/completion", tags=["completion"])

    def _service() -> Any:
        return completion_service.service()

    # -- the literal paths, before `/{decision_id}` ------------------------

    @router.get("/modes")
    async def modes(request: Request):
        """The four modes and what each one's policy actually says.

        Served from `agent_profiles/completion.py` rather than restated here.
        That module has owned the vocabulary since plan 3, and a second copy in
        a route would be the first place the two drift.
        """
        require_admin(request)
        answer = _service().config()
        return {"ok": True, "enabled": answer["enabled"],
                "shadow_enabled": answer["shadow_enabled"],
                "modes": answer["modes"], "policies": answer["policies"],
                "layers": answer["layers"]}

    @router.get("/config")
    async def config(request: Request):
        require_admin(request)
        return _service().config()

    @router.get("/settings")
    async def settings(request: Request):
        """The two switches and the two numbers, read live.

        A read and not a mirror of `DEFAULT_SETTINGS`: what matters to a caller
        is what this machine is doing now, and the defaults are what it would
        do if nobody had chosen.
        """
        require_admin(request)
        from src.settings import get_setting

        return {
            "ok": True,
            "enabled": enabled(),
            "shadow_enabled": shadow_enabled(),
            "verification_reserve": get_setting("agent_completion_verification_reserve", 0.15),
            "max_bonus_rounds": get_setting("agent_completion_max_bonus_rounds", 3),
        }

    @router.get("/diagnostics")
    async def diagnostics(request: Request):
        require_admin(request)
        owner = _require_owner(request)
        try:
            return _service().diagnostics(owner=owner)
        except ContractError as exc:
            return _from_error(exc, path="diagnostics")

    @router.get("/events")
    async def events(request: Request, since: int = 0, limit: int = 200,
                     stream: int = 0):
        """What the engine decided, resumable by cursor.

        Polling by default, SSE when asked. Frames are UNNAMED with the name
        inside the JSON, which is the dialect the rest of this repository's
        streams speak and the one a browser's `EventSource` can actually read.
        """
        require_admin(request)
        owner = _require_owner(request)
        cursor = max(0, int(since or 0))
        wanted = _int_arg(limit, 200, low=1, high=MAX_STREAM_EVENTS)
        last_id = str(request.headers.get("last-event-id") or "")
        if last_id.isdigit():
            cursor = max(cursor, int(last_id))
        stream_obj = _service().events(owner=owner)
        accept = str(request.headers.get("accept") or "").lower()
        if not (stream or "text/event-stream" in accept):
            rows, next_cursor, gap = stream_obj.since(cursor, limit=wanted)
            return {"ok": True, "events": [row.to_dict() for row in rows],
                    "cursor": next_cursor, "gap": bool(gap),
                    "enabled": enabled(), "shadow_enabled": shadow_enabled()}

        async def frames():
            position = cursor
            started = time.monotonic()
            last_beat = started
            while time.monotonic() - started < STREAM_MAX_S:
                if await request.is_disconnected():
                    return
                rows, position, _gap = stream_obj.since(position, limit=wanted)
                for row in rows:
                    yield row.sse()
                if rows:
                    last_beat = time.monotonic()
                    continue
                try:
                    await stream_obj.wait(position, timeout=STREAM_HEARTBEAT_S)
                except Exception:  # noqa: BLE001
                    await asyncio.sleep(1.0)
                if time.monotonic() - last_beat >= STREAM_HEARTBEAT_S:
                    last_beat = time.monotonic()
                    yield ": keep-alive\n\n"
            yield "event: end\ndata: " + json.dumps({"cursor": position}) + "\n\n"

        return StreamingResponse(frames(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    # -- the collection ---------------------------------------------------

    @router.get("")
    async def list_decisions(request: Request, shadow: str = "false", mode: str = "",
                             project_id: str = "", stop_reason: str = "",
                             limit: int = 50, cursor: str = ""):
        """This owner's decisions. Shadow and real are never mixed by default.

        `shadow` is a three-state string -- `true`, `false`, `all` -- and not a
        boolean, because the third state has to be reachable and a missing
        boolean would default to one of the other two silently. Mixing the two
        would ruin the measurement the shadow mode exists to produce: counting
        what the engine WOULD have done alongside what it did.
        """
        require_admin(request)
        owner = _require_owner(request)
        wanted = str(shadow or "false").strip().lower()
        if wanted not in ("true", "false", "all"):
            return _refusal("shadow", "must be true, false or all",
                            code="invalid_argument")
        if mode and mode not in COMPLETION_MODES:
            return _refusal("mode", f"must be one of {', '.join(COMPLETION_MODES)}",
                            code="unknown_mode")
        if stop_reason and stop_reason not in STOP_REASONS:
            return _refusal("stop_reason", f"must be one of {', '.join(STOP_REASONS)}",
                            code="invalid_argument")
        try:
            return _service().list(
                owner=owner,
                shadow=None if wanted == "all" else (wanted == "true"),
                mode=mode, project_id=project_id, stop_reason=stop_reason,
                limit=_int_arg(limit, 50, low=1, high=200), cursor=str(cursor or ""))
        except ContractError as exc:
            return _from_error(exc, path="decisions")

    # -- one decision -----------------------------------------------------

    @router.get("/{decision_id}")
    async def get_decision(request: Request, decision_id: str):
        require_admin(request)
        owner = _require_owner(request)
        try:
            return _service().get(str(decision_id or ""), owner=owner)
        except CompletionServiceError as exc:
            if getattr(exc, "code", "") == "not_found":
                raise HTTPException(404, "no such decision") from exc
            return _from_error(exc, path="decision_id")
        except ContractError as exc:
            return _from_error(exc, path="decision_id")

    @router.post("/{decision_id}/reject-improvement")
    async def reject_improvement(request: Request, decision_id: str):
        """A person says no to one improvement, with a reason. §12.

        `require_human`, and the reason is mandatory: §1.8's rule is that a
        rejected opportunity must not reappear without new evidence, and a
        refusal nobody justified is indistinguishable next month from one
        nobody meant.

        Recorded and never destructive: the candidate keeps its evidence and
        its score, and what changes is its status. The record of the engine
        having proposed it survives, because the interesting question later is
        not what ran -- it is what was offered and turned down.
        """
        require_admin(request)
        require_human(request)
        owner = _require_owner(request)
        body = await _payload(request)
        candidate_id = str(body.get("candidate_id") or "")
        reason = str(body.get("reason") or "").strip()
        if not candidate_id:
            return _refusal("candidate_id", "is required", code="invalid_argument")
        if not reason:
            return _refusal(
                "reason",
                "is empty; a refusal nobody justified cannot be told from one "
                "nobody meant once everyone has forgotten",
                code="invalid_argument")
        try:
            row = _service().store().get_decision(str(decision_id or ""), owner=owner)
        except Exception as exc:  # noqa: BLE001 - a read never 500s a route
            logger.warning("completion routes: reading %s failed: %s", decision_id, exc)
            row = None
        if row is None:
            raise HTTPException(404, "no such decision")
        known = {c.id: c for c in (row.executed + row.rejected + row.deferred)}
        candidate = known.get(candidate_id)
        if candidate is None:
            return _refusal("candidate_id", "names no improvement in this decision",
                            code="not_found")

        # Stored against `candidate.key` and not the id it was named by. The id
        # belongs to this decision; the improvement outlives it, and tomorrow's
        # turn rediscovers the same missing test under a new id. Keying on the
        # id would enforce the refusal exactly once -- against the row the
        # person was looking at, and never against the thing they refused.
        stored = _service().store().record_refusal(
            owner=owner, candidate_key=candidate.key, reason=reason,
            decision_id=row.id, candidate_id=candidate_id,
            project_id=str(getattr(row, "project_id", "") or ""), actor=owner)
        if not stored:
            return _refusal(
                "candidate_id",
                "the refusal could not be stored; it would be offered again next "
                "round and saying otherwise here would be the lie",
                code="not_stored")
        return {"ok": True, "decision_id": row.id, "candidate_id": candidate_id,
                "candidate_key": candidate.key, "recorded": reason,
                "actor": owner, "stored": True}

    return router
