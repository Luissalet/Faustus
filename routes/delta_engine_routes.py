"""HTTP for the Universal Delta Engine: `/api/deltas`.

Thin on purpose. Everything that decides anything lives in
`src/delta_engine/service.py`; this file resolves the caller, checks the shape
of what arrived, and re-shapes the answer. Four conventions, all inherited
rather than invented, and each with the failure it prevents:

**A rejection is a 200 with `ok: false`** and an `error` of `{path, message,
code}` -- the shape `routes/contracts_routes.py` established and
`routes/state_mirror_routes.py` repeats. `4xx` is reserved for a malformed
request (no JSON body, not an object) and for the 404 below. `code` is a stable
token from `service.ERRORS`; `message` is prose that may be reworded any day,
and a client that had to regex the prose to find the field would break the day
it improved.

**Another owner's delta is a 404, never a 403.** A 403 confirms that an id
somebody guessed is real, which is half of what they were guessing at. The
service answers `not_found` for both cases and this file keeps them identical
at the status code too.

**The owner comes from the session, never from the body.** An `owner` in a
payload is discarded and reported back in `ignored_fields` rather than
honoured. The same rule the mirror's routes state, and the reason the 404 above
can be trusted.

**The flag gates comparing, not reading.** `agent_delta_engine` (default off)
stops `POST /` , `POST /{id}/run` and `POST /intent/compile` -- the three that
COST this machine a re-read and a re-parse of both revisions. Every GET keeps
answering, because a delta already stored was a conclusion recorded honestly
and switching the engine off is a decision about spending, never an instruction
to hide what was concluded.

**The gate.** Every endpoint is `require_admin`: a delta names paths, symbols,
configuration keys and state fields from this machine. `POST /{id}/reclassify`
is additionally `require_human`, and that one is not ceremony: reclassifying is
how a `regression` becomes a `required` change, and a model able to call the
endpoint its own page calls could reinterpret its way out of every finding
against it.

**Route order matters.** `/intent/compile`, `/profiles`, `/config`,
`/extractors/status`, `/events` and `/diagnostics` are all declared BEFORE
`/{delta_id}`, because FastAPI matches in declaration order and a path
parameter declared first would swallow every one of them and answer 404 for a
route that exists.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from core.middleware import require_admin, require_human
from src.auth_helpers import effective_user, get_current_user
from src.contracts.base import ContractError
from src.delta_engine import service as delta_service
from src.delta_engine.contracts import ASSESSMENTS, DOMAINS, DeltaError  # noqa: F401
from src.delta_engine.service import DeltaServiceError, enabled
from src.owner_identity import effective_storage_owner

logger = logging.getLogger(__name__)

#: The stable tokens THIS file invents, as opposed to the ones the service
#: owns. Deliberately short: everything else a caller can be told already has a
#: name in `service.ERRORS`.
ROUTE_ERRORS = ("delta_engine_disabled",)

#: Fields the server decides and a payload may not set. Sent back in
#: `ignored_fields` rather than refused, because a client that copies a delta
#: back as a request should get a working comparison and a note, not a 400.
SERVER_DECIDED = ("owner", "id", "created_at", "schema_version")

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
    """A `ContractError` (service or contract) as a 200 rejection.

    Both carry `path` and `message`; only the service's carries `code`. A
    contract failure that reaches here is a caller mistake about a field, so it
    defaults to `invalid_argument` rather than to something that sounds like a
    server fault.

    Every handler below catches `ContractError` and NOT `DeltaError`, and the
    difference is not cosmetic: `DeltaError` is a SUBCLASS of `ContractError`,
    so `except DeltaError` misses every rejection raised by the shared helpers
    in `src/contracts/base.py` -- `as_mapping`, `text`, `one_of`,
    `reject_unknown` -- which is to say it misses the most common caller
    mistake there is. `POST /api/deltas` with no `source` answered 500 until
    this line said the parent class.
    """
    return _refusal(
        str(getattr(exc, "path", "") or path or "<root>"),
        str(getattr(exc, "message", "") or str(exc)),
        code=str(getattr(exc, "code", "") or "invalid_argument"),
    )


def _switched_off(path: str) -> Optional[Dict[str, Any]]:
    if enabled():
        return None
    return _refusal(
        path,
        "the Delta Engine is switched off (Settings -> Agent & automation -> "
        "Delta Engine). No new comparison runs; every delta already stored "
        "keeps answering.",
        code="delta_engine_disabled", enabled=False)


def _owner(request: Request) -> str:
    try:
        who = effective_user(request) or get_current_user(request) or ""
    except Exception:  # noqa: BLE001 - attribution must not 500 a route
        logger.debug("delta routes: the caller could not be resolved", exc_info=True)
        who = ""
    try:
        return str(effective_storage_owner(who) or "").strip()
    except Exception:  # noqa: BLE001
        return str(who or "").strip()


def _require_owner(request: Request) -> str:
    """The owner, or a 403 -- never an empty string handed to the store.

    An empty owner is what the store reads as the UNSCOPED query, so a route
    that passed one would hand back every delta on the machine. This is the
    guard the rest of the file rests on.
    """
    owner = _owner(request)
    if not owner:
        raise HTTPException(403, "no signed-in owner")
    return owner


async def _payload(request: Request) -> Dict[str, Any]:
    """The JSON body as an object, or a 400. A missing body is `{}`.

    A 400 here and a 200 rejection everywhere else is not an inconsistency: a
    body that is not an object never reached the question, so there is no
    question to answer `ok: false` about.
    """
    try:
        raw = await request.json()
    except Exception:
        raw = None
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise HTTPException(400, "the body must be a JSON object")
    return dict(raw)


async def _offloaded(func: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a blocking service call in a worker thread.

    Not a nicety. A comparison walks both revisions and parses every file it
    finds, which on a repository this size is minutes of CPU -- and an
    `async def` handler that calls it inline holds the event loop for all of
    it. The symptom is not "deltas are slow": it is that every request in the
    entire application queues behind one comparison, the page shows skeletons
    forever, and the log stops. That is exactly what happened the first time
    this was pointed at two checkpoints of this repository from the browser,
    and no test could have seen it, because a test never has a second request.

    `src/council/orchestrator.py::_verify` already does this for the same
    reason: the work is synchronous by nature, so the loop must not be the one
    doing it.
    """
    return await asyncio.to_thread(lambda: func(*args, **kwargs))


def _int_arg(value: Any, default: int, *, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def setup_delta_engine_routes() -> APIRouter:
    router = APIRouter(prefix="/api/deltas", tags=["deltas"])

    def _service() -> Any:
        return delta_service.service()

    # -- the literal paths, declared before `/{delta_id}` -------------------

    @router.get("/config")
    async def config(request: Request):
        """Every closed vocabulary this subsystem uses.

        Served rather than duplicated in the front end, because a page with its
        own copy of a vocabulary drifts on the day someone adds a word, and the
        symptom is a row that renders blank instead of an error anyone notices.
        """
        require_admin(request)
        return _service().config()

    @router.get("/profiles")
    async def profiles(request: Request):
        require_admin(request)
        return _service().profiles()

    @router.get("/extractors/status")
    async def extractors(request: Request):
        """Which domains can actually be compared here, and why not the rest."""
        require_admin(request)
        return _service().extractors()

    @router.get("/diagnostics")
    async def diagnostics(request: Request):
        require_admin(request)
        owner = _require_owner(request)
        try:
            return _service().diagnostics(owner=owner)
        except ContractError as exc:
            return _from_error(exc, path="diagnostics")

    @router.post("/intent/compile")
    async def compile_intent(request: Request):
        """Freeze a request into a contract, before anything has been read.

        Gated by the flag even though it reads no revision: a frozen contract
        is the first half of a comparison, and minting one while the engine is
        off leaves contracts in the store that no delta will ever answer.
        """
        require_admin(request)
        owner = _require_owner(request)
        off = _switched_off("intent")
        if off is not None:
            return off
        body = await _payload(request)
        ignored = [key for key in SERVER_DECIDED if key in body]
        try:
            answer = _service().compile_intent(
                owner=owner,
                domain=str(body.get("domain") or ""),
                text=str(body.get("text") or body.get("intent_text") or ""),
                requested=body.get("requested") or (),
                invariants=body.get("invariants") or (),
                scope=body.get("scope"),
                tolerances=body.get("tolerances"),
                acceptance=body.get("acceptance") or (),
                evaluation_profile=str(body.get("evaluation_profile") or "default"),
                project_id=str(body.get("project_id") or ""),
                supersedes=str(body.get("supersedes") or ""),
            )
        except ContractError as exc:
            return _from_error(exc, path="intent")
        answer["ignored_fields"] = ignored
        return answer

    # -- the collection ----------------------------------------------------

    @router.get("")
    async def list_deltas(request: Request, domain: str = "", project_id: str = "",
                          assessment: str = "", limit: int = 50, cursor: str = ""):
        """This owner's live deltas, newest first.

        A `domain` or `assessment` outside the vocabulary is a 200 rejection
        naming the legal values, not an empty list: an empty list is
        indistinguishable from "you have none of those", and a typo that reads
        as good news is what a closed vocabulary exists to prevent.
        """
        require_admin(request)
        owner = _require_owner(request)
        if domain and domain not in DOMAINS:
            return _refusal("domain", f"must be one of {', '.join(DOMAINS)}",
                            code="unknown_domain")
        if assessment and assessment not in ASSESSMENTS:
            return _refusal("assessment", f"must be one of {', '.join(ASSESSMENTS)}",
                            code="invalid_argument")
        try:
            return _service().list(owner=owner, domain=domain, project_id=project_id,
                                   assessment=assessment,
                                   limit=_int_arg(limit, 50, low=1, high=200),
                                   cursor=str(cursor or ""))
        except ContractError as exc:
            return _from_error(exc, path="deltas")

    @router.post("")
    async def create_delta(request: Request):
        """Create a comparison and, unless told otherwise, run it now."""
        require_admin(request)
        owner = _require_owner(request)
        off = _switched_off("delta_request")
        if off is not None:
            return off
        body = await _payload(request)
        workspace = str(body.get("workspace") or "")
        run = body.get("run", True)
        try:
            # Off the loop: this is the call that reads and parses both
            # revisions. See `_offloaded`.
            return await _offloaded(_service().create, body, owner=owner,
                                    workspace=workspace, run=bool(run))
        except ContractError as exc:
            return _from_error(exc, path="delta_request")

    # -- the live stream ---------------------------------------------------

    @router.get("/events")
    async def events(request: Request, since: int = 0, limit: int = 200,
                     stream: int = 0):
        """What this owner's comparisons are doing, resumable by cursor.

        Polling by default and SSE only when the caller asks -- `?stream=1`, or
        an `Accept: text/event-stream`, which is what a browser's `EventSource`
        always sends. Progress frames are UNNAMED so they reach `onmessage` and
        only the terminal frame is named `end`; `DeltaEvent.sse()` builds the
        unnamed frame with the name inside the JSON, so this file formats none
        of it. `since` means STRICTLY AFTER, so reconnecting duplicates nothing
        and the stream announces a gap rather than hiding one.
        """
        require_admin(request)
        owner = _require_owner(request)
        cursor = max(0, int(since or 0))
        wanted = _int_arg(limit, 200, low=1, high=MAX_STREAM_EVENTS)
        accept = str(request.headers.get("accept") or "")
        last_id = str(request.headers.get("last-event-id") or "")
        if last_id.isdigit():
            cursor = max(cursor, int(last_id))
        stream_obj = _service().events(owner=owner)
        if not (stream or "text/event-stream" in accept.lower()):
            rows, next_cursor, gap = stream_obj.since(cursor, limit=wanted)
            return {"ok": True, "events": [row.to_dict() for row in rows],
                    "cursor": next_cursor, "gap": bool(gap),
                    "enabled": enabled()}

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
                except Exception:  # noqa: BLE001 - a dead wait is a heartbeat
                    await asyncio.sleep(1.0)
                if time.monotonic() - last_beat >= STREAM_HEARTBEAT_S:
                    last_beat = time.monotonic()
                    yield ": keep-alive\n\n"
            yield "event: end\ndata: " + json.dumps({"cursor": position}) + "\n\n"

        return StreamingResponse(frames(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    # -- one delta ---------------------------------------------------------

    def _not_found() -> HTTPException:
        return HTTPException(404, "no such delta")

    @router.get("/{delta_id}")
    async def get_delta(request: Request, delta_id: str):
        require_admin(request)
        owner = _require_owner(request)
        try:
            return _service().get(str(delta_id or ""), owner=owner)
        except DeltaServiceError as exc:
            if getattr(exc, "code", "") == "not_found":
                raise _not_found() from exc
            return _from_error(exc, path="delta_id")
        except ContractError as exc:
            return _from_error(exc, path="delta_id")

    @router.get("/{delta_id}/evidence")
    async def evidence(request: Request, delta_id: str):
        """The pointers, never the bytes. §18: evidence lives in its own store."""
        require_admin(request)
        owner = _require_owner(request)
        try:
            return _service().evidence(str(delta_id or ""), owner=owner)
        except DeltaServiceError as exc:
            if getattr(exc, "code", "") == "not_found":
                raise _not_found() from exc
            return _from_error(exc, path="delta_id")

    @router.post("/{delta_id}/run")
    async def run_delta(request: Request, delta_id: str):
        """Run a comparison whose request was created earlier.

        `delta_id` here is the REQUEST id, and the parameter keeps the shared
        name because the path is `/api/deltas/...`; the docstring is the only
        honest place to say so, since renaming the parameter would change the
        route's shape for no gain.
        """
        require_admin(request)
        owner = _require_owner(request)
        off = _switched_off("delta_id")
        if off is not None:
            return off
        body = await _payload(request)
        try:
            return await _offloaded(_service().run, str(delta_id or ""), owner=owner,
                                    workspace=str(body.get("workspace") or ""))
        except DeltaServiceError as exc:
            if getattr(exc, "code", "") == "not_found":
                raise _not_found() from exc
            return _from_error(exc, path="delta_id")
        except ContractError as exc:
            return _from_error(exc, path="delta_id")

    @router.post("/{delta_id}/reclassify")
    async def reclassify(request: Request, delta_id: str):
        """Change what one observation MEANS. Never what it was.

        `require_human` and not merely `require_admin`, and this is the one
        endpoint in the file where the difference has teeth: reclassifying is
        how a `regression` becomes a `required` change, so a model that could
        call it would be able to reinterpret its way out of every finding
        against it. The observation is untouched either way -- the store
        refuses a revision that would alter it -- and the reason is mandatory
        because a reinterpretation nobody justified cannot be told from a
        mistake once everyone has forgotten.

        NOT gated by the flag: reinterpreting costs no re-read, and a person
        correcting the record should not be stopped by a switch about spending.
        """
        require_admin(request)
        require_human(request)
        owner = _require_owner(request)
        body = await _payload(request)
        try:
            return _service().reclassify(
                str(delta_id or ""), str(body.get("assertion_id") or ""),
                owner=owner,
                classification_name=str(body.get("classification") or ""),
                actor=owner,
                reason=str(body.get("reason") or ""),
            )
        except DeltaServiceError as exc:
            if getattr(exc, "code", "") == "not_found":
                raise _not_found() from exc
            return _from_error(exc, path="reclassify")
        except ContractError as exc:
            return _from_error(exc, path="reclassify")

    @router.post("/{delta_id}/invalidate")
    async def invalidate(request: Request, delta_id: str):
        """Retire a delta whose revisions turned out not to be immutable.

        Not gated by the flag either: retiring a conclusion that is no longer
        about anything is a correction, and a switch about what the machine may
        SPEND should never keep a wrong answer alive.
        """
        require_admin(request)
        require_human(request)
        owner = _require_owner(request)
        body = await _payload(request)
        try:
            return _service().invalidate(str(delta_id or ""), owner=owner,
                                         reason=str(body.get("reason") or ""))
        except DeltaServiceError as exc:
            if getattr(exc, "code", "") == "not_found":
                raise _not_found() from exc
            return _from_error(exc, path="delta_id")
        except ContractError as exc:
            return _from_error(exc, path="delta_id")

    return router
