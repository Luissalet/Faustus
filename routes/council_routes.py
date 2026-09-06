"""Council rooms over HTTP — /api/council/* (src/council/, plan §13, §14).

A council is a room where several models think and exactly one of them acts.
`src/council/service.py` is the whole of the decision-making; this file is the
transport, and it is deliberately thin: it resolves WHO is calling, hands that
owner to the service, and translates the service's answers into the shapes this
repo already uses. There is no ownership check here, no second state machine
and no re-derived view — those live in the service, and a route that recomputed
one would be the copy that drifts.

Five things this file is responsible for, and they are all about the edge:

* **The owner comes from the session, never from the body** (§16, §20). Every
  handler resolves the caller once through `_require_owner` and passes it down.
  An `owner` in a payload is not an error and not obeyed: it is dropped and
  named back in `ignored_fields`, so a client that thought it was choosing gets
  told it was not.
* **`""` is not an owner.** `persistence._owner_clause` treats an empty owner as
  the UNSCOPED read — right for recovery and the doctor, catastrophic for a
  route. So an unresolvable caller is refused here rather than handed an empty
  string that would read every room on the box.
* **A room that is not yours is 404, never 403** (§20). A 403 confirms the room
  exists, and its title is then one error message away. The service already
  answers `None`/`not_found` for absent and for foreign alike; this file keeps
  that indistinguishable at the status code too, and the 404 body carries no
  title, no owner and no revision.
* **A rejection is a 200 with `ok: false`** — the convention
  `routes/contracts_routes.py` set: the caller asked a question and got an
  answer. `4xx` is reserved for a malformed request (no JSON body, not an
  object) and for 404. The refusal carries `{"path", "message", "code"}`, where
  `code` is a STABLE token from `service.ERRORS` (§13: "emplear errores de
  dominio estables, no depender del texto de una excepción") and `message` is
  prose that may be reworded any day.
* **A turn is never awaited on the request.** `POST /messages` answers with a
  `turn_id` while the turn runs in the background (§13); the room is followed
  through `/events` (SSE) and `/wait` (long poll), both resumable by `seq`.

**Events, and the frame dialect.** `/events` speaks JSON by default and SSE
when the caller asks for it — `?stream=1`, or an `Accept: text/event-stream`,
which is what a browser's `EventSource` always sends. The frames follow the
dialect `src/contracts/event.py` and `routes/tournament_routes.py` already pay
for: progress frames are UNNAMED so they reach `onmessage`, and only the
terminal frame is named `end`. `src/council/events.py::CouncilEvent.sse()`
builds the unnamed frame with the event's name inside the JSON, so this file
does not format one itself.

**Resuming.** `since` (or `seq`, or a `Last-Event-ID` header) is the cursor and
means *strictly after*: `CouncilEventStream.since()` returns nothing already
seen, and inserts a gap marker when the ring buffer has dropped what was asked
for. Reconnecting therefore duplicates nothing and hides nothing (§20), and
this file adds no cursor arithmetic of its own that could reintroduce either.

**The flag.** `agent_council` (default off) gates EXECUTION only: `/messages`
and `/commands` refuse with `council_disabled` and say so. Every read — the
room, its transcript, its ledger, its tasks, decisions and usage — keeps
answering, because turning a subsystem off is a decision about what may run,
not an instruction to hide what already happened.

**The gate.** Every endpoint is `require_admin`, like Tournament: a council
spends every GPU on the box across several models, and the endpoints it names
are the owner's own. Ownership scoping is what separates two admins' rooms.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from core.middleware import require_admin
from src.auth_helpers import effective_user, get_current_user
from src.council import persistence as council_persistence
from src.council import service as council_service
from src.council.contracts import CouncilError
from src.owner_identity import effective_storage_owner

logger = logging.getLogger(__name__)

__all__ = ["ROUTE_ERRORS", "enabled", "setup_council_routes"]

#: The only error codes this FILE invents. Everything else a caller may branch
#: on comes from `service.ERRORS`, which is the closed vocabulary §13 asks for.
#: Keeping this tuple next to it — and short — is what stops a route from
#: quietly growing a second error language beside the service's.
ROUTE_ERRORS: Tuple[str, ...] = ("council_disabled",)

#: Long poll and stream limits. A council turn is minutes of several models
#: talking, so the ceilings are generous; they exist so a forgotten tab cannot
#: pin a worker for ever, not to cut a live room short.
DEFAULT_WAIT_S = 25.0
MAX_WAIT_S = 300.0
STREAM_MAX_S = 900.0
#: How long the SSE loop sleeps on the stream before emitting a heartbeat. A
#: comment frame keeps a proxy from closing an idle connection; it is a `:`
#: line, which every SSE client ignores by specification.
STREAM_HEARTBEAT_S = 15.0
MAX_EVENTS = 1000

#: Fields the SERVER decides. Present in a body they are dropped and named
#: back, never obeyed and never an error (§16: the owner arrives from the
#: authenticated caller, and a client that guessed otherwise deserves to be
#: told rather than silently overruled).
SERVER_DECIDED: Tuple[str, ...] = (
    "owner", "id", "revision", "status", "created_at", "updated_at", "archived",
)
#: The same, for a message: who wrote it is who is signed in.
MESSAGE_DECIDED: Tuple[str, ...] = ("owner", "author_id", "author_kind", "session_id")


# ── the feature flag ───────────────────────────────────────────────────────

def enabled() -> bool:
    """`agent_council`. Off = no turn may RUN; every read still answers.

    Read live on each call rather than captured at import, so flipping the
    switch in Settings takes effect on the next request instead of the next
    restart — the same shape `tournament.enabled()` uses.
    """
    try:
        from src.settings import get_setting

        return bool(get_setting("agent_council", False))
    except Exception:  # noqa: BLE001 - a read never fails over a settings lookup
        logger.debug("council routes: agent_council unreadable; treated as off", exc_info=True)
        return False


# ── the shapes every handler answers in ────────────────────────────────────

def _refusal(path: str, message: str, *, code: str = "", **extra: Any) -> Dict[str, Any]:
    """One rejection, in the shape `routes/contracts_routes.py` established.

    `path` names the field, `message` is for a person, and `code` is the stable
    token a client branches on. Splitting them is the point: a UI that had to
    regex one prose blob to find the field is a UI that breaks when the prose
    improves.
    """
    error: Dict[str, Any] = {"path": path, "message": message}
    if code:
        error["code"] = code
    body: Dict[str, Any] = {"ok": False, "error": error}
    body.update(extra)
    return body


def _relay(answer: Any, *, path: str) -> Dict[str, Any]:
    """A `service` answer as a route answer.

    The service already speaks in stable tokens; this only re-shapes them and
    makes ONE substantive decision — `not_found` becomes a 404 rather than a
    200 with `ok: false`. A room that is not this owner's and a room that never
    existed both arrive here as `not_found`, and both leave as the same bare
    404: §20's rule is that the two must be indistinguishable, and a status
    code is the first thing an attacker reads.
    """
    if not isinstance(answer, dict):  # pragma: no cover - the service always answers a dict
        logger.error("council routes: the service answered %r, not a mapping", type(answer))
        return _refusal(path, "the council service gave an unreadable answer")
    if answer.get("ok"):
        return dict(answer)
    code = str(answer.get("error") or "")
    if code == "not_found":
        raise HTTPException(404, "no such council")
    extra = {k: v for k, v in answer.items() if k not in ("ok", "error", "detail")}
    return _refusal(path, str(answer.get("detail") or code or "refused"),
                    code=code, **extra)


def _from_contract(exc: CouncilError, *, path: str = "") -> Dict[str, Any]:
    """A contract failure as a 200 rejection, with its own field name kept."""
    return _refusal(str(getattr(exc, "path", "") or path or "<root>"),
                    str(getattr(exc, "message", "") or str(exc)),
                    code="invalid_argument", detail=str(exc))


# ── who is calling ─────────────────────────────────────────────────────────

def _owner(request: Request) -> str:
    """The signed-in owner, resolved once, or `""`.

    `effective_user` first so a paired client's bearer token attributes to the
    human who minted it rather than to the `api` pseudo-user, then
    `effective_storage_owner`, which maps the explicit no-login mode onto the
    reserved local owner instead of onto nobody.
    """
    try:
        who = effective_user(request) or get_current_user(request) or ""
    except Exception:  # noqa: BLE001 - attribution must not 500 a route
        logger.debug("council routes: the caller could not be resolved", exc_info=True)
        who = ""
    try:
        return str(effective_storage_owner(who) or "").strip()
    except Exception:  # noqa: BLE001
        return str(who or "").strip()


def _require_owner(request: Request) -> str:
    """The owner, or a refusal — never an empty string handed to the store.

    This is the guard the rest of the file rests on. `persistence._owner_clause`
    reads an empty owner as the UNSCOPED query, which is correct for recovery
    and the doctor and would be a disclosure of every room on the machine if a
    route ever passed one. So an unresolvable caller stops here.
    """
    owner = _owner(request)
    if not owner:
        raise HTTPException(403, "a council room belongs to a signed-in owner")
    return owner


async def _body(request: Request) -> Dict[str, Any]:
    """The JSON object, or a 4xx. The one place a malformed request is a 4xx."""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "a JSON body is required")
    if not isinstance(payload, dict):
        raise HTTPException(400, "the body must be a JSON object")
    return payload


def _ignored(body: Dict[str, Any], names: Sequence[str]) -> List[str]:
    """Which server-decided fields the caller tried to set, in a stable order."""
    return [name for name in names if name in body]


def _int_arg(value: Any, default: int, *, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _float_arg(value: Any, default: float, *, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


# ── the flag, applied to the two endpoints that run models ────────────────

def _switched_off(path: str) -> Optional[Dict[str, Any]]:
    """The refusal an execution endpoint answers with when the flag is off.

    Creating and reading a room are not gated: a room that runs nothing costs
    nothing, and hiding evidence because a switch moved would make the flag a
    delete. §21 names `agent_council` as the flag for the feature; what it
    actually has to stop is a model being called.
    """
    if enabled():
        return None
    return _refusal(
        path,
        "the council is switched off (Settings → Agent & automation → Council). "
        "Existing rooms stay readable and no turn can run.",
        code="council_disabled", enabled=False)


# ── the routes ─────────────────────────────────────────────────────────────

def setup_council_routes() -> APIRouter:
    router = APIRouter(prefix="/api/council", tags=["council"])

    def _service() -> council_service.CouncilService:
        return council_service.service()

    def _visible(session_id: str, owner: str) -> Any:
        """The room, or a bare 404 that says nothing else about it."""
        session = _service().get(session_id, owner=owner)
        if session is None:
            raise HTTPException(404, "no such council")
        return session

    # -- lifecycle ---------------------------------------------------------

    @router.post("")
    async def create(request: Request):
        """Open a room. The owner is the caller's; an `owner` in the body is
        dropped and named back in `ignored_fields` (§16)."""
        require_admin(request)
        owner = _require_owner(request)
        body = await _body(request)
        ignored = _ignored(body, SERVER_DECIDED)
        try:
            session = _service().create(
                owner=owner,
                title=str(body.get("title") or ""),
                policy=str(body.get("policy") or "chat"),
                participants=list(body.get("participants") or ()),
                workspace=str(body.get("workspace") or ""),
                project_id=str(body.get("project_id") or ""),
                budgets=body.get("budgets"),
                parent_session_id=str(body.get("parent_session_id") or ""),
                preset_id=str(body.get("preset_id") or ""))
        except CouncilError as exc:
            return _from_contract(exc, path="session")
        except Exception as exc:  # noqa: BLE001 - a refused room is an answer
            logger.exception("council routes: the room could not be opened")
            return _refusal("session", f"the room could not be opened: {exc}",
                            code="command_failed")
        return {"ok": True, "session": session.to_dict(),
                "participants": [p.to_dict() for p in _seats(session.id)],
                "ignored_fields": ignored, "enabled": enabled()}

    def _seats(session_id: str) -> List[Any]:
        """The stored seats, resolved — the profile a request asked for is not
        the profile it got, and the caller should see which one it got."""
        try:
            return list(council_persistence.store().list_participants(session_id) or ())
        except Exception:  # noqa: BLE001 - a read path
            logger.exception("council routes: seats of %s unreadable", session_id)
            return []

    @router.get("")
    async def index(request: Request, status: str = "", limit: int = 50):
        """This owner's rooms, newest first. Archived rooms are not listed."""
        require_admin(request)
        owner = _require_owner(request)
        rooms = _service().list(owner=owner, status=str(status or ""),
                                limit=_int_arg(limit, 50, low=1, high=200))
        return {"ok": True, "sessions": [s.to_dict() for s in rooms],
                "count": len(rooms), "enabled": enabled()}

    @router.get("/config")
    async def config(request: Request):
        """Everything the Studio form needs to open a room (§13).

        Straight from `service().config()` — the policies, roles, ceilings,
        commands and error tokens are read from the modules that own them, so a
        role added to `contracts.ROLES` reaches the form without a second edit.
        """
        require_admin(request)
        payload = dict(_service().config())
        payload["enabled"] = enabled()
        payload["route_errors"] = list(ROUTE_ERRORS)
        return {"ok": True, "config": payload}

    @router.get("/{session_id}")
    async def show(request: Request, session_id: str):
        """One room, archived or not.

        There is deliberately no `archived` field here. The store returns an
        archived room from `get_session` and hides it from `list_sessions`, and
        `CouncilSession.to_dict()` does not carry the column — so the only way
        this route could report it would be to page a listing and infer it from
        an absence, which is exact only while the owner has fewer rooms than the
        page size. An approximate flag that is right in every test and wrong on
        a busy machine is worse than no flag: a room is archived when it stops
        appearing in `GET /api/council`, and that answer is always exact.
        """
        require_admin(request)
        owner = _require_owner(request)
        session = _visible(session_id, owner)
        return {"ok": True, "session": session.to_dict(),
                "participants": [p.to_dict() for p in _seats(session.id)],
                "enabled": enabled()}

    @router.patch("/{session_id}")
    async def patch(request: Request, session_id: str):
        """Change a room against the revision the caller read (§8).

        `owner` is refused by the service and dropped here first, so the answer
        is `ignored_fields` rather than an error: a client that sent the whole
        object back is not attacking anybody.
        """
        require_admin(request)
        owner = _require_owner(request)
        body = await _body(request)
        raw = body.get("patch")
        changes = dict(raw) if isinstance(raw, dict) else {
            k: v for k, v in body.items() if k not in ("patch", "expected_revision")}
        ignored = _ignored(changes, SERVER_DECIDED)
        for name in ignored:
            changes.pop(name, None)
        expected = body.get("expected_revision")
        if expected is not None and (isinstance(expected, bool) or not isinstance(expected, int)):
            return _refusal("expected_revision", "expected_revision must be a whole number",
                            code="invalid_argument")
        try:
            session = _service().update(session_id, changes, owner=owner,
                                        expected_revision=expected)
        except council_persistence.NotFound:
            raise HTTPException(404, "no such council")
        except council_persistence.RevisionConflict as exc:
            return _refusal("expected_revision", str(getattr(exc, "message", "") or exc),
                            code="revision_conflict", revision=exc.revision)
        except CouncilError as exc:
            return _from_contract(exc, path="session")
        except Exception as exc:  # noqa: BLE001
            logger.exception("council routes: %s could not be patched", session_id)
            return _refusal("session", f"the room could not be changed: {exc}",
                            code="command_failed")
        return {"ok": True, "session": session.to_dict(), "ignored_fields": ignored}

    @router.delete("/{session_id}")
    async def archive(request: Request, session_id: str):
        """Archive a room. Nothing is deleted (§13).

        The room drops out of `GET /api/council` and stays fully readable by id:
        its transcript, ledger, tasks, decisions and usage all keep answering.
        Evidence of what several models decided and did is the one thing a
        council must not be able to lose to a click.
        """
        require_admin(request)
        owner = _require_owner(request)
        _visible(session_id, owner)
        archived = _service().archive(session_id, owner=owner)
        if not archived:
            return _refusal("session", "the room could not be archived",
                            code="command_failed")
        return {"ok": True, "session_id": session_id, "archived": True, "deleted": False,
                "note": "archived, not deleted: the transcript, the ledger and the "
                        "evidence stay readable by id"}

    # -- messages (§13: answer with a turn id, run in the background) -------

    @router.post("/{session_id}/messages")
    async def post_message(request: Request, session_id: str):
        """Take the user's message, open a turn, answer with its id.

        The turn runs as a background task and reports through `/events`; this
        handler does not await it. A repeated POST carrying the same
        `idempotency_key` lands on the turn the first one opened rather than
        starting a second round of paid work (§15.3, §20).
        """
        require_admin(request)
        owner = _require_owner(request)
        body = await _body(request)
        off = _switched_off("command")
        if off is not None:
            _visible(session_id, owner)
            return off
        ignored = _ignored(body, MESSAGE_DECIDED)
        key = str(body.get("idempotency_key")
                  or request.headers.get("Idempotency-Key") or "").strip()
        mentions = [str(m) for m in (body.get("mentions") or ()) if str(m).strip()]
        answer = await _service().post_message(
            session_id, author_id=owner, content=str(body.get("content") or ""),
            mentions=mentions, idempotency_key=key, owner=owner)
        relayed = _relay(answer, path="content")
        if relayed.get("ok"):
            relayed["ignored_fields"] = ignored
            relayed["idempotency_key"] = key
        return relayed

    @router.get("/{session_id}/messages")
    async def messages(request: Request, session_id: str, viewer_id: str = "",
                       since_id: str = "", limit: int = 200):
        """The transcript. Naming a `viewer_id` reads it as that participant
        would — which is how the blind first round of `consult` and `debate`
        stays blind (§4.2); the owner asking for nobody gets the audit view."""
        require_admin(request)
        owner = _require_owner(request)
        _visible(session_id, owner)
        rows = _service().messages(session_id, viewer_id=str(viewer_id or ""),
                                   since_id=str(since_id or ""),
                                   limit=_int_arg(limit, 200, low=1, high=1000),
                                   owner=owner)
        return {"ok": True, "session_id": session_id, "messages": rows,
                "count": len(rows), "viewer_id": str(viewer_id or "")}

    # -- commands (§13) ----------------------------------------------------

    @router.post("/{session_id}/commands")
    async def command(request: Request, session_id: str):
        """One of `service.COMMANDS`, with its arguments.

        An unknown command is refused BY NAME and the valid ones are listed, so
        a caller that mistyped one is not left guessing. Every refusal carries a
        token from `service.ERRORS` — never the text of an exception (§13).
        """
        require_admin(request)
        owner = _require_owner(request)
        body = await _body(request)
        name = str(body.get("command") or "").strip()
        off = _switched_off("command")
        if off is not None:
            _visible(session_id, owner)
            off["valid_commands"] = list(council_service.COMMANDS)
            return off
        arguments = {k: v for k, v in body.items() if k != "command"}
        answer = await _service().command(session_id, name, owner=owner,
                                          actor=owner, **arguments)
        return _relay(answer, path="command")

    # -- events: SSE and long poll, both resumable by seq (§14, §20) -------

    @router.get("/{session_id}/events")
    async def events(request: Request, session_id: str, since: int = 0, seq: int = 0,
                     limit: int = 200, stream: int = 0, timeout: float = STREAM_MAX_S):
        """The room's events from `since`, as JSON or as SSE.

        SSE when `?stream=1` or when the caller sent `Accept: text/event-stream`
        — which a browser's `EventSource` always does, so a page needs no query
        parameter and a script needs no header.

        The cursor means STRICTLY AFTER, and it is the stream's own arithmetic,
        not this file's: reconnecting with the last `seq` seen repeats nothing,
        and a buffer that has already dropped what was asked for answers with a
        gap marker instead of a shorter list (§20).
        """
        require_admin(request)
        owner = _require_owner(request)
        _visible(session_id, owner)
        room = _service().events(session_id, owner=owner)
        if room is None:
            raise HTTPException(404, "no such council")
        cursor = _cursor(request, since, seq)
        if stream or _wants_sse(request):
            return StreamingResponse(
                _sse_stream(room, cursor, _float_arg(timeout, STREAM_MAX_S,
                                                     low=1.0, high=STREAM_MAX_S)),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                         "Connection": "keep-alive"})
        rows = room.since(cursor, limit=_int_arg(limit, 200, low=1, high=MAX_EVENTS))
        return {"ok": True, "session_id": session_id,
                "events": [e.to_dict() for e in rows], "count": len(rows),
                "since": cursor, "last_seq": room.last_seq(), "closed": room.closed}

    @router.get("/{session_id}/wait")
    async def wait(request: Request, session_id: str, since: int = 0, seq: int = 0,
                   timeout: float = DEFAULT_WAIT_S, limit: int = 200):
        """Long poll: answer as soon as anything follows `since`.

        Sleeps on the stream's own `asyncio.Event` — there is no poll tick in
        this path, the same design `src/dispatch.py` uses. A timeout is not an
        error: it answers `timed_out: true` with an empty list and the cursor
        the caller already had, which is exactly what it should send back.
        """
        require_admin(request)
        owner = _require_owner(request)
        _visible(session_id, owner)
        room = _service().events(session_id, owner=owner)
        if room is None:
            raise HTTPException(404, "no such council")
        cursor = _cursor(request, since, seq)
        rows = await room.wait(cursor,
                               timeout_s=_float_arg(timeout, DEFAULT_WAIT_S,
                                                    low=0.0, high=MAX_WAIT_S))
        rows = list(rows)[:_int_arg(limit, 200, low=1, high=MAX_EVENTS)]
        return {"ok": True, "session_id": session_id,
                "events": [e.to_dict() for e in rows], "count": len(rows),
                "since": cursor, "last_seq": room.last_seq(),
                "timed_out": not rows, "closed": room.closed}

    # -- read views (§13). None of these raise; none of them re-derive. ----

    @router.get("/{session_id}/ledger")
    async def ledger(request: Request, session_id: str):
        """Tasks, claims, objections and decisions, from `CouncilLedger` itself
        — §12.2 makes the ledger the source of the close, and a second view
        assembled here would be the one the summary disagrees with."""
        require_admin(request)
        owner = _require_owner(request)
        _visible(session_id, owner)
        return {"ok": True, "session_id": session_id,
                "ledger": _service().ledger(session_id, owner=owner)}

    @router.get("/{session_id}/tasks")
    async def tasks(request: Request, session_id: str):
        require_admin(request)
        owner = _require_owner(request)
        _visible(session_id, owner)
        rows = _service().tasks(session_id, owner=owner)
        return {"ok": True, "session_id": session_id, "tasks": rows, "count": len(rows)}

    @router.get("/{session_id}/decisions")
    async def decisions(request: Request, session_id: str):
        require_admin(request)
        owner = _require_owner(request)
        _visible(session_id, owner)
        rows = _service().decisions(session_id, owner=owner)
        return {"ok": True, "session_id": session_id, "decisions": rows, "count": len(rows)}

    @router.get("/{session_id}/usage")
    async def usage(request: Request, session_id: str):
        """What the room spent and the limit that would stop it (§3.6). The
        numbers are the scheduler's own — a second tally would be the one that
        disagrees with the reason a turn actually stopped."""
        require_admin(request)
        owner = _require_owner(request)
        _visible(session_id, owner)
        return {"ok": True, "session_id": session_id,
                "usage": _service().usage(session_id, owner=owner)}

    @router.get("/{session_id}/state")
    async def state(request: Request, session_id: str):
        """What the room's coordinator is doing at this instant.

        Live and process-local: `live` says whether this process is the one
        holding the room's coordinator, and everything under it — the cancelled
        turns, the stopped participants, the resources a tool is writing right
        now — is only knowable there.  A page asking "why did my cancel not
        release that file" is asking this route, and until it existed the answer
        was computed on every turn and shown to nobody.
        """
        require_admin(request)
        owner = _require_owner(request)
        _visible(session_id, owner)
        return {"ok": True, "session_id": session_id,
                "state": _service().state(session_id, owner=owner)}

    return router


# ── the event cursor and the SSE frames ────────────────────────────────────

def _cursor(request: Request, since: Any, seq: Any) -> int:
    """The resume point: `since`, or `seq`, or `Last-Event-ID`, or 0.

    Three spellings because three clients exist: a page written against this
    API sends `since`, a script that kept the last event's `seq` sends that,
    and a browser `EventSource` reconnecting sends `Last-Event-ID` on its own
    without being asked. They mean the same thing, and a client that had to
    know which one this endpoint wanted would be a client that loses events on
    the reconnect it did not plan.
    """
    for candidate in (since, seq, request.headers.get("Last-Event-ID")):
        try:
            value = int(candidate)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0


def _wants_sse(request: Request) -> bool:
    """True when the caller asked for a stream in its `Accept` header.

    `EventSource` always sends `Accept: text/event-stream`, so honouring it is
    what makes `new EventSource('/api/council/{id}/events')` work with no query
    parameter — while `?stream=1` keeps the explicit spelling Tournament and
    Dispatch already use.
    """
    try:
        return "text/event-stream" in str(request.headers.get("accept") or "").lower()
    except Exception:  # noqa: BLE001
        return False


async def _sse_stream(room: Any, cursor: int, deadline_s: float):
    """Replay from `cursor`, then follow the room until it closes.

    Frame dialect, and it is not a style choice: an SSE frame carrying an
    `event: <name>` line does NOT reach `EventSource.onmessage`, only a
    listener registered for that exact name — `src/contracts/event.py` says it
    cost a debugging session, and `routes/tournament_routes.py` repeats it. So
    every council event goes out UNNAMED with its name inside the JSON (that is
    what `CouncilEvent.sse()` builds), and only the terminal frame is named
    `end`, which a client opts into.

    The loop never re-reads what it already sent: `since()` is strictly after
    the cursor, and the cursor only ever moves forward to the highest `seq`
    actually written to the wire. A heartbeat is a `:` comment, which every SSE
    client ignores and every proxy counts as traffic.
    """
    started = time.monotonic()
    try:
        while True:
            try:
                batch = room.since(cursor, limit=MAX_EVENTS)
            except Exception as exc:  # noqa: BLE001 - one bad read, not a dead page
                logger.warning("council routes: reading the stream failed: %s", exc)
                batch = []
            for event in batch:
                yield event.sse()
                if event.seq > cursor:
                    cursor = event.seq
            if room.closed:
                yield _named("end", {"session_id": room.session_id, "last_seq": room.last_seq(),
                                     "closed": True})
                return
            if time.monotonic() - started > deadline_s:
                yield _named("end", {"session_id": room.session_id, "last_seq": cursor,
                                     "closed": False, "timeout": True,
                                     "note": "reconnect with since=" + str(cursor)})
                return
            remaining = deadline_s - (time.monotonic() - started)
            try:
                await room.wait(cursor, timeout_s=min(STREAM_HEARTBEAT_S, max(0.0, remaining)))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.debug("council routes: waiting on the stream failed: %s", exc)
                await asyncio.sleep(0.1)
            else:
                # Nothing new within the heartbeat window: say so on the wire so
                # a proxy counting idle seconds does not close a live room.
                if not room.since(cursor, limit=1):
                    yield ": keep-alive\n\n"
    except asyncio.CancelledError:  # the client went away
        raise
    except Exception as exc:  # noqa: BLE001 - a stream never 500s a live room
        logger.debug("council routes: the event stream ended: %s", exc)
        yield _named("end", {"session_id": getattr(room, "session_id", ""),
                             "last_seq": cursor, "closed": False, "error": True})


def _named(name: str, data: Dict[str, Any]) -> str:
    """The ONE named frame this endpoint emits, and only at the end."""
    try:
        body = json.dumps(data, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        body = "{}"
    return f"event: {name}\ndata: {body}\n\n"
