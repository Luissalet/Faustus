"""The State Mirror over HTTP -- /api/state/* (src/state_mirror/, plan 17).

The mirror is the read model of what is true right now, when we last looked and
how we know it. `src/state_mirror/service.py` is the whole of the reading and
the reconciling; this file is the transport, and it is deliberately thin: it
resolves WHO is calling, hands that owner to the service, and translates the
service's answers into the shapes this repository already uses. There is no
freshness arithmetic here, no second view of an entity and no ownership check
of its own -- those live behind the service, and a route that re-derived one
would be the copy that disagrees with the panel it feeds.

Six things this file is responsible for, and they are all about the edge:

* **The owner comes from the session, never from the body** (plan 16, 20).
  Every handler resolves the caller once through `_require_owner` and passes it
  down. An `owner` in a payload is not an error and not obeyed: it is dropped
  and named back in `ignored_fields`, so a client that thought it was choosing
  gets told it was not.
* **`""` is not an owner.** `persistence._scope` treats an empty owner as the
  UNSCOPED read -- right for the sweep and the doctor, catastrophic for a
  route. So an unresolvable caller is refused here rather than handed an empty
  string that would read every entity on the box.
* **Another owner's entity is 404, never 403** (plan 20). A 403 confirms the
  row exists, and what it is about is then one error message away. The service
  answers `{}` for absent and for foreign alike; this file keeps the two
  indistinguishable at the status code too, and the 404 body carries no id, no
  kind and no owner.
* **A rejection is a 200 with `ok: false`** -- the convention
  `routes/contracts_routes.py` set and `routes/council_routes.py` repeats: the
  caller asked a question and got an answer. `4xx` is reserved for a malformed
  request (no JSON body, not an object) and for the 404 above. The refusal
  carries `{"path", "message", "code"}`, where `code` is a STABLE token and
  `message` is prose that may be reworded any day.
* **An entity id is a URL, not a slug.** `<kind>://<owner>/<namespace>/<ident>`
  carries a scheme, empty segments on a single-user install and slashes inside
  the identifier, so every path parameter here is declared `{entity_id:path}`
  and `_entity_id` unescapes once more when a client or a proxy escaped twice.
  Without that a request for `artifact:///real/exports/2026/report.pdf` would
  address a row nobody meant to name, or none at all.
* **Reads never raise.** Everything under this prefix is a read except two
  endpoints, and a page that shows what is stale is at its most useful exactly
  when something is broken. A read that 500s because a source is down would
  hide the one fact worth having.

**Events, and the frame dialect.** `/events` speaks JSON by default and SSE
when the caller asks for it -- `?stream=1`, or an `Accept: text/event-stream`,
which is what a browser's `EventSource` always sends. The frames follow the
dialect `src/contracts/event.py` and `routes/council_routes.py` already pay
for: progress frames are UNNAMED so they reach `onmessage`, and only the
terminal frame is named `end`. `StateEvent.sse()` builds the unnamed frame with
the event's name inside the JSON, so this file does not format one itself.

**Resuming.** `since` (or `seq`, or a `Last-Event-ID` header) is the cursor and
means *strictly after*: `StateEventStream.since()` returns nothing already seen
and inserts a gap marker when the ring buffer has dropped what was asked for.
Reconnecting therefore duplicates nothing and hides nothing (plan 20), and this
file adds no cursor arithmetic of its own that could reintroduce either.

**The flag.** `agent_state_mirror` (default off) gates the two endpoints that
COST this machine: `POST /refresh` and `POST /reconcile` refuse with
`state_mirror_disabled` and say so. Every read keeps answering, because turning
the mirror off is a decision about what may probe the box and never an
instruction to hide what was already observed -- a value shown with its age on
it is worth more than a blank panel.

**The gate.** Every endpoint is `require_admin`: the mirror names every
service, model, run and artifact on the machine, and what it holds is the
owner's own. `/reconcile` is additionally `require_human`, because it asks
every disagreeing source to look again and a model must not be able to spend
the machine on that by calling the endpoint its own page calls.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from core.middleware import require_admin, require_human
from src.auth_helpers import effective_user, get_current_user
from src.owner_identity import effective_storage_owner
from src.state_mirror import service as state_service
from src.state_mirror.contracts import (
    ENTITY_KINDS,
    MINIMUM_FRESHNESS_LEVELS,
    NAMESPACE_KINDS,
    REAL_NAMESPACE,
    StateError,
    is_entity_id,
)
from src.state_mirror.queries import SITUATIONS

logger = logging.getLogger(__name__)

__all__ = ["ROUTE_ERRORS", "enabled", "setup_state_mirror_routes"]

#: The only error codes this FILE invents. Everything else a caller may branch
#: on comes from the service. Keeping this tuple next to it -- and short -- is
#: what stops a route from quietly growing a second error language beside the
#: service's.
ROUTE_ERRORS: Tuple[str, ...] = ("state_mirror_disabled",)

#: Long poll and stream limits. A sweep publishes a few hundred events, so the
#: ceilings are generous; they exist so a forgotten tab cannot pin a worker for
#: ever, not to cut a live page short.
STREAM_MAX_S = 900.0
#: How long the SSE loop sleeps before emitting a heartbeat. A comment frame
#: keeps a proxy from closing an idle connection; it is a `:` line, which every
#: SSE client ignores by specification.
STREAM_HEARTBEAT_S = 15.0
MAX_EVENTS = 1000

#: Fields the SERVER decides. Present in a body they are dropped and named
#: back, never obeyed and never an error (plan 16: the owner arrives from the
#: authenticated caller, and a client that guessed otherwise deserves to be
#: told rather than silently overruled).
SERVER_DECIDED: Tuple[str, ...] = ("owner", "namespace", "observed_at", "revision")


# -- the feature flag -------------------------------------------------------

def enabled() -> bool:
    """`agent_state_mirror`. Off = nothing may PROBE; every read still answers.

    Read live on each call rather than captured at import, so flipping the
    switch in Settings takes effect on the next request instead of the next
    restart -- the same shape `council.enabled()` uses.
    """
    try:
        from src.settings import get_setting

        return bool(get_setting("agent_state_mirror", False))
    except Exception:  # noqa: BLE001 - a read never fails over a settings lookup
        logger.debug("state routes: agent_state_mirror unreadable; treated as off",
                     exc_info=True)
        return False


def _sweep_seconds() -> int:
    """The configured sweep interval, for the diagnostics answer.

    Reported even when the flag is off, because "off" and "on, every 30s" are
    both answers a person reading the page needs, and only one of them is
    visible from the data itself.
    """
    try:
        from src.settings import get_setting

        return int(get_setting("agent_state_mirror_sweep_seconds", 30) or 0)
    except Exception:  # noqa: BLE001
        return 0


# -- the shapes every handler answers in ------------------------------------

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


def _from_contract(exc: StateError, *, path: str = "") -> Dict[str, Any]:
    """A contract failure as a 200 rejection, with its own field name kept."""
    return _refusal(str(getattr(exc, "path", "") or path or "<root>"),
                    str(getattr(exc, "message", "") or str(exc)),
                    code="invalid_argument", detail=str(exc))


def _relay(answer: Any, *, path: str) -> Dict[str, Any]:
    """A `service` answer as a route answer.

    The service already speaks in stable tokens from `service.ERRORS`; this
    only re-shapes them. Note what it deliberately does NOT do: it does not
    turn `not_found` into a 404. Here that token means "no adapter by that
    name", which is a fact about this build and not about a row somebody may
    not see -- the 404 for an entity is raised by `_visible` before the service
    is ever reached, so mapping the token would answer 404 to a caller who
    merely mistyped a source.
    """
    if not isinstance(answer, dict):  # pragma: no cover - the service answers a dict
        logger.error("state routes: the service answered %r, not a mapping", type(answer))
        return _refusal(path, "the state mirror gave an unreadable answer",
                        code="invalid_argument")
    if answer.get("ok"):
        return dict(answer)
    code = str(answer.get("error") or "")
    extra = {k: v for k, v in answer.items() if k not in ("ok", "error", "detail")}
    return _refusal(path, str(answer.get("detail") or code or "refused"),
                    code=code or "invalid_argument", **extra)


# -- who is calling ---------------------------------------------------------

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
        logger.debug("state routes: the caller could not be resolved", exc_info=True)
        who = ""
    try:
        return str(effective_storage_owner(who) or "").strip()
    except Exception:  # noqa: BLE001
        return str(who or "").strip()


def _require_owner(request: Request) -> str:
    """The owner, or a refusal -- never an empty string handed to the store.

    This is the guard the rest of the file rests on. `persistence._scope` reads
    an empty owner as the UNSCOPED query, which is correct for the sweep and
    the doctor and would be a disclosure of every entity on the machine if a
    route ever passed one. So an unresolvable caller stops here.
    """
    owner = _owner(request)
    if not owner:
        raise HTTPException(403, "the state mirror belongs to a signed-in owner")
    return owner


async def _body(request: Request) -> Dict[str, Any]:
    """The JSON object, or a 4xx. The one place a malformed request is a 4xx.

    An empty body is an empty object rather than an error: `POST /refresh` with
    nothing in it means "everything that has aged out", which is the commonest
    thing a button sends.
    """
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except Exception:
        raise HTTPException(400, "a JSON body is required")
    if not isinstance(payload, dict):
        raise HTTPException(400, "the body must be a JSON object")
    return payload


def _ignored(body: Dict[str, Any], names: Sequence[str]) -> List[str]:
    """Which server-decided fields the caller tried to set, in a stable order."""
    return [name for name in names if name in body]


def _text_list(*values: Any) -> List[str]:
    """One argument that may arrive as a list or as a single string, flattened.

    A page refreshing one card sends one name; a script refreshing a batch
    sends a list. Making the caller choose the plural spelling would be one
    more thing to get wrong for no gain, and a body carrying both is answered
    with both rather than with an argument about which wins.
    """
    out: List[str] = []
    for value in values:
        items = value if isinstance(value, (list, tuple)) else (value,)
        for item in items:
            text = str(item or "").strip()
            if text and text not in out:
                out.append(text)
    return out


def _id_list(*values: Any) -> List[str]:
    """The same, for entity ids, each unescaped once more if it needs it."""
    out: List[str] = []
    for text in _text_list(*values):
        ident = _entity_id(text)
        if ident and ident not in out:
            out.append(ident)
    return out


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


def _entity_id(raw: Any) -> str:
    """The id a caller named, unescaped once more when it arrives escaped.

    An entity id is `<kind>://<owner>/<namespace>/<identifier>`: a client has
    to percent-escape the whole of it, and the server has already decoded it
    once by the time a path parameter reaches here. A client that escaped twice
    -- or a proxy that refuses to decode `%2F` in a path, which several do --
    would otherwise ask about an id that cannot exist and get a bare 404 that
    says nothing about why. Decoding a second time only when an escape is still
    visible is what keeps an identifier containing a literal `%` intact.
    """
    value = str(raw or "").strip()
    if "%2f" in value.lower() or "%3a" in value.lower():
        try:
            value = unquote(value)
        except Exception:  # noqa: BLE001 - a bad escape is the caller's typo
            pass
    return value.strip()


def _namespace_arg(value: Any) -> str:
    """A namespace argument, or `""` when it names no world this build knows.

    `""` rather than a raise: the caller is a query string, and the handler
    turns it into a 200 rejection that names the legal values. A namespace is
    the boundary between a branch simulation and the real machine, so a typo
    that silently widened it would be the worst possible failure here.
    """
    raw = str(value or "").strip() or REAL_NAMESPACE
    kind = raw.split(":", 1)[0]
    return raw if kind in NAMESPACE_KINDS else ""


# -- the flag, applied to the two endpoints that probe the machine ---------

def _switched_off(path: str) -> Optional[Dict[str, Any]]:
    """The refusal a probing endpoint answers with when the flag is off.

    Reading is not gated: what was already observed costs nothing to hand back,
    and hiding it because a switch moved would make the flag a delete. What the
    flag actually has to stop is this machine being probed for work nobody
    asked for at that moment.
    """
    if enabled():
        return None
    return _refusal(
        path,
        "the state mirror is switched off (Settings -> Agent & automation -> "
        "State Mirror). Nothing may probe this machine; everything already "
        "observed keeps answering, with its age on it.",
        code="state_mirror_disabled", enabled=False)


# -- the routes -------------------------------------------------------------

def setup_state_mirror_routes() -> APIRouter:
    router = APIRouter(prefix="/api/state", tags=["state"])

    def _service() -> Any:
        return state_service.service()

    def _visible(entity_id: str, owner: str) -> Dict[str, Any]:
        """The entity, or a bare 404 that says nothing else about it.

        The service answers `{}` both for a row that never existed and for one
        that belongs to somebody else, and this keeps the two identical at the
        status code as well: a 403 here would confirm that a `service://` id
        somebody guessed is real, which is half of what they were guessing at.
        """
        try:
            row = _service().entity(entity_id, owner=owner)
        except Exception:  # noqa: BLE001 - a read never raises out of a route
            logger.exception("state routes: %s could not be read", entity_id)
            row = {}
        if not row:
            raise HTTPException(404, "no such entity")
        return dict(row)

    # -- what the mirror holds --------------------------------------------

    @router.get("/entities")
    async def entities(request: Request, kind: str = "", project_id: str = "",
                       namespace: str = REAL_NAMESPACE, limit: int = 200):
        """This owner's entities, with the state currently believed about each.

        A `kind` outside `ENTITY_KINDS` is a 200 rejection naming the legal
        ones rather than an empty list: an empty list is indistinguishable from
        "you have none of those", and a typo that reads as good news is exactly
        what a closed vocabulary exists to prevent.
        """
        require_admin(request)
        owner = _require_owner(request)
        wanted = str(kind or "").strip()
        if wanted and wanted not in ENTITY_KINDS:
            return _refusal("kind", f"must be one of {', '.join(ENTITY_KINDS)}",
                            code="invalid_argument")
        world = _namespace_arg(namespace)
        if not world:
            return _refusal("namespace",
                            f"must be one of {', '.join(NAMESPACE_KINDS)}, "
                            "optionally followed by ':<id>'",
                            code="invalid_argument")
        rows = _service().entities(owner=owner, kind=wanted,
                                   project_id=str(project_id or ""),
                                   namespace=world,
                                   limit=_int_arg(limit, 200, low=1, high=500))
        return {"ok": True, "entities": list(rows), "count": len(rows),
                "namespace": world, "enabled": enabled()}

    @router.get("/changes")
    async def changes(request: Request, cursor: int = 0, limit: int = 200):
        """Everything that changed after `cursor`, with the next one to send.

        The cursor is the store's own monotonic revision counter and means
        STRICTLY AFTER, so a page that keeps the number it was last given
        re-reads nothing and skips nothing. It is deliberately not the event
        stream's `seq`: one counts changes to the state and the other counts
        frames on a wire, and a client that used one for the other would
        either replay changes or lose them.
        """
        require_admin(request)
        owner = _require_owner(request)
        answer = _service().changes(_int_arg(cursor, 0, low=0, high=2 ** 62),
                                    owner=owner,
                                    limit=_int_arg(limit, 200, low=1, high=500))
        body = dict(answer) if isinstance(answer, dict) else {"states": [], "cursor": 0}
        rows = list(body.get("states") or ())
        return {"ok": True, "states": rows, "count": len(rows),
                "cursor": int(body.get("cursor") or 0)}

    @router.get("/conflicts")
    async def conflicts(request: Request, entity_id: str = "", open_only: bool = True,
                        limit: int = 100):
        """What two sources disagree about, recorded rather than decided.

        A conflict is a first-class row precisely because the reducer did NOT
        choose: it keeps both claims and says what would settle them. Answering
        with the winner here would be inventing the decision the reducer
        refused to make.
        """
        require_admin(request)
        owner = _require_owner(request)
        rows = _service().conflicts(owner=owner, entity_id=_entity_id(entity_id),
                                    open_only=bool(open_only),
                                    limit=_int_arg(limit, 100, low=1, high=500))
        return {"ok": True, "conflicts": list(rows), "count": len(rows),
                "open_only": bool(open_only)}

    @router.get("/diagnostics")
    async def diagnostics(request: Request):
        """Which source is answering, how much is stored, and where the feed is.

        Reports the flag and the sweep interval too. "Off" and "on, every 30
        seconds" are both answers a person reading a stale panel needs, and
        neither is visible from the rows themselves.
        """
        require_admin(request)
        owner = _require_owner(request)
        try:
            payload = dict(_service().diagnostics(owner=owner) or {})
        except Exception:  # noqa: BLE001 - the page that shows what is broken
            logger.exception("state routes: diagnostics could not be read")
            payload = {}
        payload["enabled"] = enabled()
        payload["sweep_seconds"] = _sweep_seconds()
        payload["route_errors"] = list(ROUTE_ERRORS)
        return {"ok": True, "diagnostics": payload}

    @router.get("/project")
    async def project(request: Request, project_id: str = "",
                      entity_ref: Optional[List[str]] = Query(None),
                      field: Optional[List[str]] = Query(None),
                      minimum_freshness: str = "informational",
                      namespace: str = REAL_NAMESPACE,
                      token_budget: int = 0, reason: str = ""):
        """A bounded projection for Context Engine and other consumers.

        Query lists use repeated ``entity_ref`` and ``field`` parameters.  An
        empty list intentionally means "the useful fields in this project",
        as defined by the projection service, rather than a route-side guess.
        """
        require_admin(request)
        owner = _require_owner(request)
        world = _namespace_arg(namespace)
        if not world:
            return _refusal("namespace",
                            f"must be one of {', '.join(NAMESPACE_KINDS)}, "
                            "optionally followed by ':<id>'",
                            code="invalid_argument")
        freshness = str(minimum_freshness or "informational").strip()
        if freshness not in MINIMUM_FRESHNESS_LEVELS:
            return _refusal(
                "minimum_freshness",
                f"must be one of {', '.join(MINIMUM_FRESHNESS_LEVELS)}",
                code="invalid_argument",
            )
        refs = [_entity_id(value) for value in (entity_ref or ()) if str(value).strip()]
        for ref in refs:
            if not is_entity_id(ref):
                return _refusal("entity_ref", "must contain state entity ids",
                                code="invalid_argument", got=ref)
        try:
            payload = _service().project(
                owner=owner,
                project_id=str(project_id or ""),
                entity_refs=refs,
                fields=[str(value) for value in (field or ()) if str(value).strip()],
                minimum_freshness=freshness,
                namespace=world,
                token_budget=_int_arg(token_budget, 0, low=0, high=2 ** 31 - 1),
                reason=str(reason or "")[:500],
            )
        except Exception:  # noqa: BLE001 - projections are read paths
            logger.exception("state routes: project projection failed")
            payload = {}
        return {"ok": True, "projection": dict(payload or {}),
                "enabled": enabled()}

    @router.get("/situation/{name}")
    async def situation(request: Request, name: str, project_id: str = "",
                        namespace: str = REAL_NAMESPACE, limit: int = 200):
        """One of the named situation queries (plan 17), by name.

        An unknown name is refused BY NAME with the ones that exist listed. It
        is not an empty result: a situation query answering `[]` means "nothing
        is running", and a typo that produced the same answer would read as
        good news about a machine nobody looked at.
        """
        require_admin(request)
        owner = _require_owner(request)
        wanted = str(name or "").strip()
        if wanted not in SITUATIONS:
            return _refusal("name", f"{wanted!r} is not a situation query",
                            code="unknown_situation", situations=list(SITUATIONS))
        world = _namespace_arg(namespace)
        if not world:
            return _refusal("namespace",
                            f"must be one of {', '.join(NAMESPACE_KINDS)}, "
                            "optionally followed by ':<id>'",
                            code="invalid_argument")
        answer = _service().situation(wanted, owner=owner,
                                      project_id=str(project_id or ""),
                                      namespace=world,
                                      limit=_int_arg(limit, 200, low=1, high=500))
        return _relay(answer, path="name")

    # -- the two endpoints that cost this machine --------------------------

    @router.post("/refresh")
    async def refresh(request: Request):
        """Look again, now.

        The only read on this surface that spends the machine, which is why it
        is a POST and why the flag can refuse it. It names either the entities
        whose sources should be woken or the sources themselves, and `entity_id`
        / `source` are accepted beside `entity_ids` / `sources` because a page
        refreshing one card sends one id and should not have to wrap it.

        An `entity_id` that is not an id at all is a 200 rejection naming the
        field: the caller sent a bad VALUE, not a bad request. One that is an
        id but is not this owner's is the same 404 every other route gives,
        raised before the service is asked, so a probe is never spent finding
        out whether somebody else's machine has that service on it.
        """
        require_admin(request)
        owner = _require_owner(request)
        body = await _body(request)
        off = _switched_off("refresh")
        if off is not None:
            return off
        ignored = _ignored(body, SERVER_DECIDED)
        targets = _id_list(body.get("entity_ids"), body.get("entity_id"))
        for target in targets:
            if not is_entity_id(target):
                return _refusal("entity_id",
                                "must look like <kind>://<owner>/<namespace>/<identifier>",
                                code="invalid_argument", got=target)
            _visible(target, owner)
        sources = _text_list(body.get("sources"), body.get("source"))
        try:
            answer = _service().refresh(owner=owner, entity_ids=targets,
                                        sources=sources,
                                        project_id=str(body.get("project_id") or ""))
        except StateError as exc:
            return _from_contract(exc, path="refresh")
        except Exception as exc:  # noqa: BLE001 - a refused probe is an answer
            logger.exception("state routes: the refresh failed")
            return _refusal("refresh", f"nothing could be looked at again: {exc}",
                            code="invalid_argument")
        relayed = _relay(answer, path="refresh")
        if relayed.get("ok"):
            relayed["ignored_fields"] = ignored
            relayed["enabled"] = True
        return relayed

    @router.post("/reconcile")
    async def reconcile(request: Request):
        """Ask the disagreeing sources again, and settle what can be settled.

        `require_human` on top of `require_admin`: a reconciliation sweep asks
        every source that is in dispute to look again, which is the most
        expensive thing this subsystem can do. `require_admin` accepts the
        in-process internal token so the agent's own loopback calls reach admin
        routes, and here that would let a model spend the machine by calling
        the endpoint its own page calls.
        """
        require_admin(request)
        require_human(request)
        owner = _require_owner(request)
        off = _switched_off("reconcile")
        if off is not None:
            return off
        try:
            answer = _service().reconcile(owner=owner)
        except StateError as exc:
            return _from_contract(exc, path="reconcile")
        except Exception as exc:  # noqa: BLE001
            logger.exception("state routes: the reconciliation failed")
            return _refusal("reconcile", f"the reconciliation did not run: {exc}",
                            code="invalid_argument")
        relayed = _relay(answer, path="reconcile")
        if relayed.get("ok"):
            relayed["enabled"] = True
        return relayed

    # -- events: SSE and JSON, both resumable by seq (plan 20) -------------

    @router.get("/events")
    async def events(request: Request, since: int = 0, seq: int = 0,
                     limit: int = 200, stream: int = 0, timeout: float = STREAM_MAX_S):
        """This owner's state events from `since`, as JSON or as SSE.

        SSE when `?stream=1` or when the caller sent `Accept: text/event-stream`
        -- which a browser's `EventSource` always does, so a page needs no
        query parameter and a script needs no header.

        The cursor means STRICTLY AFTER, and it is the stream's own arithmetic,
        not this file's: reconnecting with the last `seq` seen repeats nothing,
        and a buffer that has already dropped what was asked for answers with a
        gap marker instead of a shorter list (plan 20).
        """
        require_admin(request)
        owner = _require_owner(request)
        feed = _service().events(owner=owner)
        if feed is None:
            raise HTTPException(404, "no state stream for this owner")
        cursor = _cursor(request, since, seq)
        if stream or _wants_sse(request):
            return StreamingResponse(
                _sse_stream(feed, cursor, _float_arg(timeout, STREAM_MAX_S,
                                                     low=1.0, high=STREAM_MAX_S)),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                         "Connection": "keep-alive"})
        rows = feed.since(cursor, limit=_int_arg(limit, 200, low=1, high=MAX_EVENTS))
        return {"ok": True, "events": [e.to_dict() for e in rows], "count": len(rows),
                "since": cursor, "last_seq": feed.last_seq(), "closed": feed.closed}

    # -- one entity ---------------------------------------------------------
    #
    # Declared LAST, and `/history` before the bare id, because `{entity_id:path}`
    # compiles to a greedy `.*`: with the bare route first, a request for
    # `/entities/<id>/history` would match it with `entity_id` ending in
    # `/history` and the history endpoint would never be reached.

    @router.get("/entities/{entity_id:path}/history")
    async def history(request: Request, entity_id: str, limit: int = 50):
        """Every observation behind this entity's state, newest first.

        The observation log is append-only and is the only thing that can
        answer "why does it say that": a state is a reduction, and a reduction
        nobody can trace back to what was seen is a number with an opinion.
        """
        require_admin(request)
        owner = _require_owner(request)
        target = _entity_id(entity_id)
        _visible(target, owner)
        rows = _service().history(target, owner=owner,
                                  limit=_int_arg(limit, 50, low=1, high=500))
        return {"ok": True, "entity_id": target, "observations": list(rows),
                "count": len(rows)}

    @router.get("/entities/{entity_id:path}")
    async def entity(request: Request, entity_id: str):
        """One entity, with every field's value, age, source and epistemology.

        `{entity_id:path}` and not `{entity_id}`: an id carries `://` and can
        carry slashes inside its identifier, and a parameter that stopped at
        the first `/` would answer 404 for every artifact under a directory.
        """
        require_admin(request)
        owner = _require_owner(request)
        target = _entity_id(entity_id)
        row = _visible(target, owner)
        return {"ok": True, "entity": row, "enabled": enabled()}

    return router


# -- the event cursor and the SSE frames ------------------------------------

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
    what makes `new EventSource('/api/state/events')` work with no query
    parameter -- while `?stream=1` keeps the explicit spelling Council,
    Tournament and Dispatch already use.
    """
    try:
        return "text/event-stream" in str(request.headers.get("accept") or "").lower()
    except Exception:  # noqa: BLE001
        return False


async def _sse_stream(feed: Any, cursor: int, deadline_s: float):
    """Replay from `cursor`, then follow the mirror until it closes.

    Frame dialect, and it is not a style choice: an SSE frame carrying an
    `event: <name>` line does NOT reach `EventSource.onmessage`, only a
    listener registered for that exact name -- `src/contracts/event.py` says it
    cost a debugging session, and `routes/council_routes.py` repeats it. So
    every state event goes out UNNAMED with its name inside the JSON (that is
    what `StateEvent.sse()` builds), and only the terminal frame is named
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
                batch = feed.since(cursor, limit=MAX_EVENTS)
            except Exception as exc:  # noqa: BLE001 - one bad read, not a dead page
                logger.warning("state routes: reading the stream failed: %s", exc)
                batch = []
            for event in batch:
                yield event.sse()
                if event.seq > cursor:
                    cursor = event.seq
            if feed.closed:
                yield _named("end", {"last_seq": feed.last_seq(), "closed": True})
                return
            if time.monotonic() - started > deadline_s:
                yield _named("end", {"last_seq": cursor, "closed": False, "timeout": True,
                                     "note": "reconnect with since=" + str(cursor)})
                return
            remaining = deadline_s - (time.monotonic() - started)
            try:
                await feed.wait(cursor, timeout_s=min(STREAM_HEARTBEAT_S,
                                                      max(0.0, remaining)))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.debug("state routes: waiting on the stream failed: %s", exc)
                await asyncio.sleep(0.1)
            else:
                # Nothing new within the heartbeat window: say so on the wire so
                # a proxy counting idle seconds does not close a live page.
                if not feed.since(cursor, limit=1):
                    yield ": keep-alive\n\n"
    except asyncio.CancelledError:  # the client went away
        raise
    except Exception as exc:  # noqa: BLE001 - a stream never 500s a live page
        logger.debug("state routes: the event stream ended: %s", exc)
        yield _named("end", {"last_seq": cursor, "closed": False, "error": True})


def _named(name: str, data: Dict[str, Any]) -> str:
    """The ONE named frame this endpoint emits, and only at the end."""
    try:
        body = json.dumps(data, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        body = "{}"
    return f"event: {name}\ndata: {body}\n\n"
