"""
council/service.py — the one door into a council, and the only place that
decides whether the caller may open it.

The failure this file exists to prevent is the oldest one in any multi-tenant
surface, and the plan names it twice (§16, §20): a room's owner arriving in the
body of a request.  `POST /api/council {"owner": "alice", ...}` is a session
anybody can create as anybody; `GET /api/council/{id}` that answers 403 with a
title in the message is a room whose existence and subject leak to a stranger.
So every method here takes `owner` as an argument from the AUTHENTICATED
caller, never from a payload, and a room belonging to somebody else is answered
exactly as a room that does not exist: `None`, `[]`, `{}` — no exception, no
title, no revision, nothing that separates "not yours" from "not there".

The other four rules this module holds:

* **`post_message` answers with a `turn_id` and gets out of the way** (§13:
  "Debe responder rápido con `turn_id` y ejecutar en background. El streaming
  llega por eventos").  A request that waited for four models to finish would
  hold a connection for minutes and lose the whole turn when it dropped; the
  work runs as a task and the room is watched through `events.stream_for`.
* **A command fails with a token, not with a sentence** (§13: "Emplear errores
  de dominio estables, no depender del texto de una excepción").  `ERRORS` is a
  closed vocabulary; the sentence next to it is for a person.
* **A profile is computed on the server, every time** (§16, §25).  `create`
  and `assign_role` both run `participants.effective_profile`, which can only
  ever lower what a caller asked for.  A `tool_profile` in a request is a
  request, and nothing here can turn it into a grant.
* **Reads never raise.**  A council page that 500s because one row is corrupt
  is a council the user cannot even shut down.

What this module is NOT: it holds no state machine, no policy and no turn.
Those are `orchestrator.py` and `policies.py`, which are written alongside this
file; the interface assumed of them is declared in `_ORCHESTRATOR_CONTRACT`
below, and a build without them degrades to `engine_unavailable` rather than
growing a second coordinator here.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from src.council import contracts as _contracts
from src.council import events as council_events
from src.council import participants as council_participants
from src.council import persistence as council_persistence
from src.council import scheduler as council_scheduler
from src.council.contracts import (
    DEFAULT_BUDGETS,
    POLICIES,
    ROLES,
    SESSION_STATUSES,
    TERMINAL_TURN_STATES,
    TOOL_PROFILES,
    ROLE_TOOL_PROFILES,
    CouncilBudgets,
    CouncilError,
    CouncilSession,
)

logger = logging.getLogger(__name__)

__all__ = [
    "COMMANDS",
    "ERRORS",
    "CouncilService",
    "service",
    "reset_service",
    "use_orchestrator",
    "reset_orchestrator",
]

#: §13's command list, closed.  A command nothing routes on is a string in a
#: log, and a caller that mistypes one is told which ones exist.
COMMANDS: Tuple[str, ...] = (
    "pause", "resume", "cancel_turn", "stop_participant", "steer",
    "assign_role", "handoff_task", "request_synthesis",
)

#: The stable error tokens a command or a post may answer with.  A caller
#: branches on these; the `detail` beside them is prose for a human and may be
#: reworded any day without breaking anybody (§13).
ERRORS: Tuple[str, ...] = (
    "not_found",              # no such room, or not this owner's — same answer
    "unknown_command",
    "missing_argument",
    "invalid_argument",
    "revision_conflict",
    "turn_in_flight",         # §4: the policy may not change mid-turn
    "engine_unavailable",     # the orchestrator is not in this build
    "command_failed",         # the engine refused; `detail` says why
)

#: What this module assumes of `orchestrator.py`, written down because the two
#: files are being built at the same time and a shared assumption that lives
#: only in a conversation is the one that drifts.
_ORCHESTRATOR_CONTRACT: Tuple[str, ...] = (
    "await submit(*, author_id, content, mentions=(), idempotency_key='') -> turn_id",
    "await run_turn(turn_id) -> TurnOutcome",
    "await pause() / resume() / request_synthesis()",
    "await cancel_turn(turn_id, *, actor)",
    "await stop_participant(participant_id=..., actor=...)",
    "await steer(participant_id=..., message=..., actor=...)",
    "await handoff_task(task_id=..., to=..., actor=...)",
    "state() -> Dict",
)


# ── the orchestrator, injected rather than imported ────────────────────────

_ORCHESTRATOR_FACTORY: Optional[Callable[..., Any]] = None


def use_orchestrator(factory: Optional[Callable[..., Any]]) -> None:
    """Build turns with `factory` instead of `orchestrator.CouncilOrchestrator`.

    The same seam `context.use_compiler` and `persistence.use_path` provide.
    It exists for two callers: a test that must not start a real turn, and the
    period in which `orchestrator.py` is being written next to this file — a
    service that could not be tested until its coordinator landed would be a
    service tested only by the coordinator's bugs.
    """
    global _ORCHESTRATOR_FACTORY
    _ORCHESTRATOR_FACTORY = factory


def reset_orchestrator() -> None:
    """Go back to the real coordinator."""
    global _ORCHESTRATOR_FACTORY
    _ORCHESTRATOR_FACTORY = None


def _orchestrator_class() -> Optional[Callable[..., Any]]:
    if _ORCHESTRATOR_FACTORY is not None:
        return _ORCHESTRATOR_FACTORY
    try:
        from src.council.orchestrator import CouncilOrchestrator

        return CouncilOrchestrator
    except Exception as exc:  # noqa: BLE001 - a build without it degrades, it does not crash
        logger.warning("council service: no orchestrator in this build (%s); rooms can be "
                       "created and read, and no turn can run", exc)
        return None


# ── small shared readers ───────────────────────────────────────────────────

def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _rows(items: Any) -> List[Dict[str, Any]]:
    """Contract objects as plain dicts, for a route that will serialise them."""
    out: List[Dict[str, Any]] = []
    for item in items or ():
        to_dict = getattr(item, "to_dict", None)
        if callable(to_dict):
            try:
                out.append(dict(to_dict()))
                continue
            except Exception:  # noqa: BLE001 - one bad row is not the list
                logger.exception("council service: a row could not be serialised")
                continue
        if isinstance(item, Mapping):
            out.append(dict(item))
    return out


def _fail(code: str, detail: str, **extra: Any) -> Dict[str, Any]:
    """One refusal, in the one shape every caller branches on."""
    if code not in ERRORS:  # pragma: no cover - guarded by a test
        logger.error("council service: %r is not in ERRORS %s", code, list(ERRORS))
    answer = {"ok": False, "error": code, "detail": detail}
    answer.update(extra)
    return answer


class CouncilService:
    """Create, read, steer and close council rooms.

    One instance per process in normal use (`service()`), and any number in
    tests.  It owns three things and nothing else: the ownership check, the
    background task each `post_message` starts, and the orchestrator cache that
    makes `pause` reach the turn that is actually running.
    """

    def __init__(self, *, store: Any = None, invoker: Any = None,
                 executor: Any = None, clock: Optional[Callable[[], float]] = None) -> None:
        #: `None` means "the process-wide store, looked up per call".  Looked
        #: up per call rather than held, because `persistence.use_path` drops
        #: the shared instance and a service holding the old one would keep
        #: writing to the database a test just moved away from.
        self._store = store
        self._invoker = invoker
        self._executor = executor
        self._clock = clock or time.time
        self._guard = threading.RLock()
        self._orchestrators: Dict[str, Any] = {}
        #: Strong references to the turn tasks.  Without them asyncio may
        #: collect a running task mid-turn, which looks exactly like a room
        #: that stopped answering for no reason.
        self._turn_tasks: Dict[str, "asyncio.Task[Any]"] = {}

    # -- ownership: the check every method starts with ----------------------

    def _db(self) -> Any:
        return self._store if self._store is not None else council_persistence.store()

    def _visible(self, session_id: str, owner: str) -> Optional[CouncilSession]:
        """The room, or `None` — for absent and for somebody else's alike.

        This is the whole of §20's "sesiones de otros propietarios no son
        visibles".  `CouncilStore.get_session` already scopes by owner; this
        wrapper adds only the promise that a read never raises.
        """
        ident = _text(session_id)
        if not ident:
            return None
        try:
            return self._db().get_session(ident, owner=_text(owner))
        except Exception:  # noqa: BLE001 - a read path
            logger.exception("council service: reading session %s failed", ident)
            return None


    # -- lifecycle ---------------------------------------------------------

    def create(self, *, owner: str, title: str, policy: str, participants: Sequence[Any],
               workspace: str = "", project_id: str = "", budgets: Any = None,
               parent_session_id: str = "", preset_id: str = "") -> CouncilSession:
        """Open a room.  The owner is the caller's, and the seats are resolved.

        `participants` are REQUESTS (`participants.ParticipantSpec` or the
        mapping standing in for one).  Every one of them goes through
        `participants.resolve_participants` and then through
        `participants.effective_profile` a second time, and both of those can
        only lower a profile: the floor of what the spec asked for, what the
        seat's roles justify (§5) and what this policy consents to (§4.1).  A
        request for `full_with_gates` in a `chat` room is seated at
        `read_only`, and nothing in this method can produce the other answer.

        `preset_id` is accepted so the API shape of §13 is stable, and it is
        deliberately not stored: `CouncilSession` has no field for it, and
        widening a contract this file does not own — to record something a
        route has already expanded into `participants` — would be the worse of
        the two mistakes.  It is logged with the room it opened.
        """
        holder = _text(owner)
        if not holder:
            raise CouncilError(
                "session.owner",
                "a council is opened by an authenticated caller; the owner is not a field "
                "of the request body and there is no anonymous room")
        draft = CouncilSession.parse({
            "owner": holder,
            "title": _text(title),
            "policy": _text(policy) or "chat",
            "status": "draft",
            "workspace": _text(workspace),
            "project_id": _text(project_id),
            "parent_session_id": _text(parent_session_id),
            "budgets": _budgets(budgets),
        })
        seats = self._seats(draft, list(participants or ()), owner=holder)
        session = CouncilSession.parse({
            **draft.to_dict(),
            "participants": [seat.id for seat in seats],
        })
        store = self._db()
        stored = store.create_session(session)
        try:
            for seat in seats:
                store.add_participant(stored.id, seat)
        except Exception:
            # A room whose `participants` list names seats that were never
            # stored is worse than no room: every later read would show a
            # participant nobody can price permissions for.  Archive the half
            # built one (nothing is deleted, §13) and let the caller see why.
            logger.exception("council service: seating failed in %s; archiving it", stored.id)
            archive = getattr(store, "archive_session", None)
            if callable(archive):
                try:
                    archive(stored.id, owner=holder)
                except Exception:  # noqa: BLE001 - the original failure is the news
                    logger.exception("council service: could not archive %s", stored.id)
            raise
        logger.info("council service: opened %s (%s) for %s with %d seat(s)%s",
                    stored.id, stored.policy, holder, len(seats),
                    f" from preset {preset_id}" if _text(preset_id) else "")
        return stored

    def _seats(self, session: CouncilSession, specs: Sequence[Any],
               *, owner: str) -> List[Any]:
        """Resolve every requested seat and cap it at what the room allows."""
        resolved = council_participants.resolve_participants(
            specs, session=session, owner=owner, workspace=session.workspace)
        seats: List[Any] = []
        for row in resolved:
            seat = row.participant
            capped = council_participants.effective_profile(
                seat, session_policy=session.policy)
            if capped != seat.tool_profile:
                logger.info("council service: %s asked for %r and is seated at %r "
                            "(role and policy ceiling)",
                            seat.id, seat.tool_profile, capped)
                seat = _with_profile(seat, capped)
            for line in row.degraded:
                logger.info("council service: %s is degraded: %s", seat.id, line)
            seats.append(seat)
        return seats

    def get(self, session_id: str, *, owner: str = "") -> Optional[CouncilSession]:
        """The room, or `None` when it is absent or somebody else's."""
        return self._visible(session_id, owner)

    def list(self, *, owner: str = "", status: str = "", limit: int = 50) -> List[CouncilSession]:
        """This owner's rooms, newest first.  Never raises, never leaks."""
        try:
            return self._db().list_sessions(owner=_text(owner), status=_text(status),
                                            limit=int(limit or 50))
        except Exception:  # noqa: BLE001 - a read path
            logger.exception("council service: listing sessions for %r failed", owner)
            return []

    def update(self, session_id: str, patch: Mapping[str, Any], *, owner: str = "",
               expected_revision: Optional[int] = None) -> CouncilSession:
        """Change a room, against the revision the caller read (§8).

        Two things this refuses outright.  `owner` is not patchable: a room
        cannot be handed to somebody by editing a field, and the store would
        refuse it anyway — saying so here names the rule instead of the column.
        And the policy cannot change while a turn is in flight (§4: "El usuario
        puede cambiar de política entre actividades, pero no durante un turno
        activo"), because the turn was planned under the old one.

        A policy that DOES change re-caps every seat: a room that just became
        `chat` grants no mutating tools, and a driver seated in it is narrowed
        on the spot rather than at the next resolution.
        """
        changes = dict(patch or {})
        if "owner" in changes:
            raise CouncilError(
                "session.patch.owner",
                "the owner comes from the authenticated caller and is not a patchable "
                "field; a room is never handed over by editing a column")
        session = self._visible(session_id, owner)
        if session is None:
            raise council_persistence.NotFound("session", session_id)
        wanted = _text(changes.get("policy"))
        if wanted and wanted != session.policy and self._turn_in_flight(session.id):
            raise CouncilError(
                "session.policy",
                "a turn is still running in this room; a policy decides who speaks and "
                "cannot change under a turn that was planned with the old one")
        updated = self._db().update_session(
            session.id, changes, expected_revision=expected_revision, owner=_text(owner))
        if wanted and wanted != session.policy:
            self._recap_seats(updated)
        return updated

    def archive(self, session_id: str, *, owner: str = "") -> bool:
        """Hide a room from listings.  Nothing is deleted (§13)."""
        session = self._visible(session_id, owner)
        if session is None:
            return False
        try:
            archived = bool(self._db().archive_session(session.id, owner=_text(owner)))
        except Exception:  # noqa: BLE001 - archiving is not worth a 500
            logger.exception("council service: archiving %s failed", session_id)
            return False
        if archived:
            with self._guard:
                self._orchestrators.pop(session.id, None)
        return archived


    # -- the turn: opened here, run somewhere else (§13) --------------------

    async def post_message(self, session_id: str, *, author_id: str, content: str,
                           mentions: Sequence[str] = (), idempotency_key: str = "",
                           owner: str = "") -> Dict[str, Any]:
        """Take the user's message, open a turn, and answer with its id.

        The turn itself runs as a background task and reports through
        `events.stream_for(session_id)`.  §13 asks for exactly this shape, and
        the reason is not latency but correctness: a request that waited for
        four models would lose the whole turn when the connection dropped, and
        a client that reconnects has to be able to find the turn it started.

        `idempotency_key` is the anti-duplicate mechanism (§15.3) and is passed
        straight to `submit`: a retried POST lands on the turn the first one
        opened, and this method never starts a second background task for a
        turn that already has one.
        """
        session = self._visible(session_id, owner)
        if session is None:
            return _fail("not_found", "no such council room here")
        orchestrator = self._orchestrator_for(session)
        if orchestrator is None:
            return _fail("engine_unavailable",
                         "this build has no council orchestrator; the room is readable "
                         "and no turn can run in it")
        try:
            turn_id = _text(await orchestrator.submit(
                author_id=_text(author_id), content=str(content or ""),
                mentions=tuple(_text(m) for m in (mentions or ()) if _text(m)),
                idempotency_key=_text(idempotency_key)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a refused turn is an answer
            logger.exception("council service: submit failed in %s", session.id)
            return _fail("command_failed",
                         f"the turn could not be opened: {type(exc).__name__}: {exc}")
        if not turn_id:
            return _fail("command_failed", "the orchestrator opened no turn")
        started = self._start_turn(orchestrator, session.id, turn_id)
        return {"ok": True, "turn_id": turn_id, "session_id": session.id,
                "status": "running" if started else "already_running",
                "stream": "events"}

    def _start_turn(self, orchestrator: Any, session_id: str, turn_id: str) -> bool:
        """Run the turn in the background.  `False` when it already is.

        The task is kept in `_turn_tasks` for the whole of its life.  asyncio
        holds only a weak reference to a running task, and a turn collected
        mid-flight is a room that stops answering with nothing in the log.
        """
        with self._guard:
            existing = self._turn_tasks.get(turn_id)
            if existing is not None and not existing.done():
                return False

        async def _run() -> None:
            try:
                await orchestrator.run_turn(turn_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a turn dies in the room, not in a log
                logger.exception("council service: turn %s failed", turn_id)
                self._publish_error(session_id, turn_id, exc)

        task = asyncio.ensure_future(_run())
        with self._guard:
            self._turn_tasks[turn_id] = task
        task.add_done_callback(lambda _t, key=turn_id: self._forget_turn(key))
        return True

    def _forget_turn(self, turn_id: str) -> None:
        with self._guard:
            self._turn_tasks.pop(turn_id, None)

    def _publish_error(self, session_id: str, turn_id: str, exc: BaseException) -> None:
        """Tell the room its turn died.  A failure nobody can see is a hang."""
        try:
            council_events.stream_for(session_id).publish(
                "council_error", turn_id=turn_id, session_id=session_id,
                detail=f"{type(exc).__name__}: {exc}"[:500])
        except Exception:  # noqa: BLE001 - the stream is not the turn
            logger.exception("council service: could not publish the failure of %s", turn_id)

    def messages(self, session_id: str, *, viewer_id: str = "", since_id: str = "",
                 limit: int = 200, owner: str = "") -> List[Dict[str, Any]]:
        """The transcript as one reader may see it (§3.1, §4.2).

        The blind round lives in `persistence.list_messages`, not here.  The
        one decision this method makes is that the room's authenticated OWNER,
        asking without naming a viewer, gets the unfiltered transcript: §3.1
        gives the user authority over the room, and a room they cannot fully
        read is a room they cannot supervise.  Anyone naming a `viewer_id` is
        answered as that viewer, owner or not.
        """
        session = self._visible(session_id, owner)
        if session is None:
            return []
        watcher = _text(viewer_id)
        audit = bool(_text(owner)) and _text(owner) == session.owner and not watcher
        try:
            rows = self._db().list_messages(session.id, viewer_id=watcher,
                                            since_id=_text(since_id),
                                            limit=int(limit or 200), audit=audit)
        except Exception:  # noqa: BLE001 - a read path
            logger.exception("council service: reading the transcript of %s failed", session.id)
            return []
        return [self._with_identity_warning(row, message)
                for row, message in zip(_rows(rows), rows)]

    @staticmethod
    def _with_identity_warning(row: Dict[str, Any], message: Any) -> Dict[str, Any]:
        """Add `claims_identity` to one transcript row (§3.2).

        `CouncilMessage.claims_identity()` reads the text for an opening line
        that pretends to be somebody else, and the orchestrator already puts it
        on the `council_message` event.  A page that only ever fetches the
        transcript -- a reload, a deep link, a scroll back through history --
        never saw that event, so without this the warning existed exactly once
        and only for whoever was watching live.

        It is a computed field and not a stored one: the content is the record,
        and re-deriving the warning from it is what stops the two disagreeing
        after a message is read back from a different build.
        """
        checker = getattr(message, "claims_identity", None)
        if not callable(checker):
            row.setdefault("claims_identity", "")
            return row
        try:
            row["claims_identity"] = str(checker() or "")
        except Exception:  # noqa: BLE001 - a warning that fails is not a message that fails
            logger.debug("council service: claims_identity failed for a message", exc_info=True)
            row["claims_identity"] = ""
        return row


    # -- commands (§13) ----------------------------------------------------

    async def command(self, session_id: str, command: str, *, owner: str = "",
                      actor: str = "", **kw: Any) -> Dict[str, Any]:
        """One of `COMMANDS`, checked against owner, revision and permission.

        Every refusal is a token from `ERRORS` plus a sentence, never the text
        of an exception (§13).  The order of the checks is the order of the
        answers a caller can act on: a command that does not exist is named
        before a room is looked up, a room that is not the caller's is `not
        found`, and a revision that lost a race is told which one won so the
        caller can re-read and re-apply instead of guessing (§8).
        """
        name = _text(command)
        if name not in COMMANDS:
            return _fail("unknown_command",
                         f"{name or '(empty)'!r} is not a council command",
                         valid_commands=list(COMMANDS))
        session = self._visible(session_id, owner)
        if session is None:
            return _fail("not_found", "no such council room here", command=name)
        expected = kw.get("expected_revision")
        if expected is not None:
            if isinstance(expected, bool) or not isinstance(expected, int):
                return _fail("invalid_argument",
                             "expected_revision must be a whole number", command=name)
            if int(expected) != session.revision:
                return _fail("revision_conflict",
                             f"this room is at revision {session.revision}, not {expected}; "
                             "re-read it and re-apply your command",
                             command=name, revision=session.revision)
        if name == "assign_role":
            return self._assign_role(session, kw, actor=_text(actor))
        orchestrator = self._orchestrator_for(session)
        if orchestrator is None:
            return _fail("engine_unavailable",
                         "this build has no council orchestrator; nothing is running to "
                         "receive that command", command=name)
        try:
            outcome = await self._send(orchestrator, name, kw, actor=_text(actor))
        except _MissingArgument as exc:
            return _fail("missing_argument", str(exc), command=name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the engine refused; say so with a token
            logger.exception("council service: %s failed in %s", name, session.id)
            return _fail("command_failed",
                         f"{name} was refused: {type(exc).__name__}: {exc}", command=name)
        return {"ok": True, "command": name, "session_id": session.id,
                "revision": session.revision, "result": _outcome(outcome)}

    async def _send(self, orchestrator: Any, name: str, kw: Mapping[str, Any],
                    *, actor: str) -> Any:
        """The one place the orchestrator's command surface is named."""
        if name == "pause":
            return await _maybe_await(orchestrator.pause())
        if name == "resume":
            return await _maybe_await(orchestrator.resume())
        if name == "request_synthesis":
            return await _maybe_await(orchestrator.request_synthesis())
        if name == "cancel_turn":
            return await _maybe_await(
                orchestrator.cancel_turn(_need(kw, "turn_id", name), actor=actor))
        if name == "stop_participant":
            return await _maybe_await(orchestrator.stop_participant(
                participant_id=_need(kw, "participant_id", name), actor=actor))
        if name == "steer":
            return await _maybe_await(orchestrator.steer(
                participant_id=_need(kw, "participant_id", name),
                message=_need(kw, "message", name), actor=actor))
        if name == "handoff_task":
            return await _maybe_await(orchestrator.handoff_task(
                task_id=_need(kw, "task_id", name), to=_need(kw, "to", name), actor=actor))
        raise _MissingArgument(  # pragma: no cover - COMMANDS and this list agree
            f"{name} is in COMMANDS and has no handler")

    def _assign_role(self, session: CouncilSession, kw: Mapping[str, Any],
                     *, actor: str) -> Dict[str, Any]:
        """Give a seat another hat — and never more tools than before (§5, §11.1).

        This one command is the service's own rather than the orchestrator's:
        it changes a stored seat, not a running turn.  The profile is
        recomputed by `participants.effective_profile`, which takes the floor of
        what the seat already holds, what its roles justify and what the policy
        allows — so adding `integrator` to a seat the room narrowed to
        `read_only` records the hat and grants nothing.
        """
        participant_id = _text(kw.get("participant_id"))
        role = _text(kw.get("role"))
        if not participant_id or not role:
            return _fail("missing_argument",
                         "assign_role needs participant_id and role", command="assign_role")
        if role not in ROLES:
            return _fail("invalid_argument",
                         f"{role!r} is not a council role; the roles are {list(ROLES)}",
                         command="assign_role")
        store = self._db()
        try:
            seat = store.get_participant(session.id, participant_id)
        except Exception:  # noqa: BLE001 - a read path
            logger.exception("council service: reading seat %s failed", participant_id)
            seat = None
        if seat is None:
            return _fail("not_found", "no such participant in this room",
                         command="assign_role")
        roles = tuple(dict.fromkeys(tuple(seat.roles) + (role,)))
        candidate = _with_roles(seat, roles)
        profile = council_participants.effective_profile(
            candidate, session_policy=session.policy)
        try:
            updated = store.update_participant(
                session.id, participant_id,
                {"roles": list(roles), "tool_profile": profile})
        except Exception as exc:  # noqa: BLE001 - a refused write is a token
            logger.exception("council service: assign_role failed for %s", participant_id)
            return _fail("command_failed",
                         f"the seat could not be updated: {type(exc).__name__}: {exc}",
                         command="assign_role")
        logger.info("council service: %s now wears %s in %s (profile %s), assigned by %s",
                    participant_id, list(roles), session.id, profile, actor or "the owner")
        self._publish_seat(session.id, updated)
        return {"ok": True, "command": "assign_role", "session_id": session.id,
                "revision": session.revision, "result": updated.to_dict()}

    def _publish_seat(self, session_id: str, seat: Any) -> None:
        try:
            council_events.stream_for(session_id).publish(
                "council_participant_resolved", session_id=session_id,
                participant_id=seat.id, roles=list(seat.roles),
                tool_profile=seat.tool_profile)
        except Exception:  # noqa: BLE001 - the stream is not the seat
            logger.exception("council service: could not announce %s", getattr(seat, "id", "?"))


    # -- read views: none of these raise, ever -----------------------------

    def ledger(self, session_id: str, *, owner: str = "") -> Dict[str, Any]:
        """The ledger's own snapshot: tasks, claims, objections, decisions.

        Built from `CouncilLedger`, which reads the store — not re-derived
        here.  §12.2 makes the ledger the source of the close, and a second
        view assembled in a service would be the one the summary and the API
        disagree about.
        """
        session = self._visible(session_id, owner)
        if session is None:
            return {}
        try:
            from src.council.ledger import CouncilLedger

            # `publisher=None` on purpose: this ledger exists to answer a GET.
            # Left unset it would resolve the room's live event stream, and a
            # read view that can publish is one bad refactor away from
            # re-announcing hydrated rows to every page watching the room.
            book = CouncilLedger(session.id, store=self._db(),
                                 workspace=session.workspace, publisher=None)
            return dict(book.snapshot())
        except Exception:  # noqa: BLE001 - a read path
            logger.exception("council service: reading the ledger of %s failed", session.id)
            return {"session_id": session.id, "unreadable": True}

    def tasks(self, session_id: str, *, owner: str = "") -> List[Dict[str, Any]]:
        session = self._visible(session_id, owner)
        if session is None:
            return []
        try:
            return _rows(self._db().list_tasks(session.id))
        except Exception:  # noqa: BLE001 - a read path
            logger.exception("council service: reading tasks of %s failed", session.id)
            return []

    def decisions(self, session_id: str, *, owner: str = "") -> List[Dict[str, Any]]:
        session = self._visible(session_id, owner)
        if session is None:
            return []
        try:
            return _rows(self._db().list_decisions(session.id))
        except Exception:  # noqa: BLE001 - a read path
            logger.exception("council service: reading decisions of %s failed", session.id)
            return []

    def usage(self, session_id: str, *, owner: str = "") -> Dict[str, Any]:
        """What the room has spent, and the limit that would stop it (§3.6).

        The numbers are `scheduler.stats()`; nothing is recomputed here.  A
        second tally would be the one that disagrees with the reason a turn
        actually stopped.
        """
        session = self._visible(session_id, owner)
        if session is None:
            return {}
        out: Dict[str, Any] = {"session_id": session.id, "budgets": session.budgets.to_dict(),
                               "scheduler": {}, "stop_reason": "", "turns": 0}
        try:
            room = council_scheduler.scheduler_for(session)
            out["scheduler"] = dict(room.stats())
            out["stop_reason"] = room.stop_reason()
        except Exception:  # noqa: BLE001 - a read path
            logger.exception("council service: reading the scheduler of %s failed", session.id)
        try:
            out["turns"] = len(self._db().list_turns(session.id))
        except Exception:  # noqa: BLE001 - a read path
            logger.exception("council service: counting turns of %s failed", session.id)
        return out

    def events(self, session_id: str, *, owner: str = "") -> Any:
        """The room's `CouncilEventStream`, or `None` when it is not visible.

        A stream is created for a room the caller may see and never for one
        they may not: a stream handed out by id would be a side channel around
        the ownership check the rest of this file makes.
        """
        session = self._visible(session_id, owner)
        if session is None:
            return None
        try:
            return council_events.stream_for(session.id)
        except Exception:  # noqa: BLE001 - a read path
            logger.exception("council service: opening the stream of %s failed", session.id)
            return None

    def state(self, session_id: str, *, owner: str = "") -> Dict[str, Any]:
        """What the room's coordinator is doing right now, or `{}`.

        `CouncilOrchestrator.state()` has always been able to answer this —
        which turns were cancelled, which participants were stopped, which
        resources a tool is writing at this instant, what has been spent — and
        until this method existed nothing in the product could ask it.  A page
        that cannot see `mutating` cannot explain why a cancel did not release a
        file, which is the one question that state was written to answer.

        Live and process-local by construction: it reads the orchestrator this
        process holds and does NOT build one, because building a coordinator to
        answer a GET would start a room nobody asked to open.  A room with no
        turn in flight here answers `running: false` and the rest from the
        store.
        """
        session = self._visible(session_id, owner)
        if session is None:
            return {}
        with self._guard:
            orchestrator = self._orchestrators.get(session.id)
        base: Dict[str, Any] = {
            "session_id": session.id,
            "status": session.status,
            "policy": session.policy,
            "revision": session.revision,
            "running": self._turn_in_flight(session.id),
            "live": orchestrator is not None,
        }
        if orchestrator is None:
            return base
        try:
            return {**base, **dict(orchestrator.state() or {}), "live": True}
        except Exception:  # noqa: BLE001 - a read path
            logger.exception("council service: reading the state of %s failed", session.id)
            return {**base, "unreadable": True}

    def config(self) -> Dict[str, Any]:
        """Everything a form needs to open a room (§13's `/api/council/config`).

        Every list here is read from the module that owns it, so a role added
        to `contracts.ROLES` appears in the form without a second edit — and a
        ceiling shown to the user is the ceiling `effective_profile` will
        actually apply.

        The vocabularies below the fold are here for the same reason: a page
        that has to hard-code the list of task statuses, objection severities or
        event names is a page that goes quietly out of date the first time one
        of them changes, and the whole point of a closed vocabulary is that
        there is exactly one copy of it.
        """
        from src.council import adapters as council_adapters
        from src.council import events as council_event_names
        from src.council import policies as council_policies
        from src.council import synthesis as council_synthesis

        return {
            "policies": list(POLICIES),
            "roles": [{"id": role, "default_profile": ROLE_TOOL_PROFILES.get(role, "none")}
                      for role in ROLES],
            "tool_profiles": list(TOOL_PROFILES),
            "policy_tool_ceilings": {policy: council_participants.policy_ceiling(policy)
                                     for policy in POLICIES},
            "completion_modes": list(_completion_modes()),
            "session_statuses": list(SESSION_STATUSES),
            "default_budgets": dict(DEFAULT_BUDGETS),
            "commands": list(COMMANDS),
            "errors": list(ERRORS),
            "verdicts": list(council_adapters.VERDICTS),
            "orchestrator_available": _orchestrator_class() is not None,
            # The rest of the closed vocabularies, so a surface renders a
            # status, a severity or an event without inventing its own list.
            "turn_states": list(_contracts.TURN_STATES),
            "terminal_turn_states": list(_contracts.TERMINAL_TURN_STATES),
            "message_types": list(_contracts.MESSAGE_TYPES),
            "visibilities": list(_contracts.VISIBILITIES),
            "author_kinds": list(_contracts.AUTHOR_KINDS),
            "task_statuses": list(_contracts.TASK_STATUSES),
            "terminal_task_statuses": list(_contracts.TERMINAL_TASK_STATUSES),
            "claim_kinds": list(_contracts.CLAIM_KINDS),
            "claim_states": list(_contracts.CLAIM_STATES),
            "objection_targets": list(_contracts.OBJECTION_TARGETS),
            "objection_severities": list(_contracts.OBJECTION_SEVERITIES),
            "objection_statuses": list(_contracts.OBJECTION_STATUSES),
            "decision_statuses": list(_contracts.DECISION_STATUSES),
            "stop_reasons": list(_contracts.STOP_REASONS),
            "reserved_ids": list(_contracts.RESERVED_IDS),
            "events": list(council_event_names.COUNCIL_EVENTS),
            # Which profiles may WRITE. Without it, telling a read-only seat
            # from one that can change files means keeping a second copy of the
            # list in the front end — which `adapters/council.ts` was doing,
            # under a constant called `FALLBACK_WRITING_PROFILES`, with a note
            # apologising for it.
            "writing_profiles": list(_contracts.WRITING_PROFILES),
            # The five states a close may report (§3.4). The screen paints one
            # of them on every finished room.
            "close_statuses": list(council_synthesis.STATUSES),
            # How a round is run and what each participant is allowed to see.
            "round_modes": list(council_policies.MODES),
            "blindness": list(council_policies.BLINDNESS_VALUES),
            "debate_phases": list(council_policies.DEBATE_PHASES),
        }

    def recover(self) -> Dict[str, Any]:
        """Reconcile at start-up and say what was reconciled (§15.2).

        `persistence.recover()` does the three writes and repeats no effect;
        this adds the in-memory half — a process that just restarted holds no
        orchestrator and no turn task, and any it did hold belong to a loop
        that is gone.  Tasks that were mid-flight are REPORTED, never re-run:
        §25 forbids retrying an effect whose outcome is uncertain.
        """
        report: Dict[str, Any] = {"at": "", "counts": {}}
        try:
            report = dict(self._db().recover())
        except Exception as exc:  # noqa: BLE001 - start-up must not fail here
            logger.exception("council service: recovery failed")
            report = {"at": "", "counts": {}, "failed": f"{type(exc).__name__}: {exc}"}
        with self._guard:
            dropped = len(self._orchestrators)
            self._orchestrators.clear()
            self._turn_tasks.clear()
        report["orchestrators_dropped"] = dropped
        report["effects_repeated"] = list(report.get("effects_repeated") or ())
        return report

    # -- the engines this service hands to a turn --------------------------

    def _orchestrator_for(self, session: CouncilSession) -> Any:
        """One coordinator per room, kept, so `pause` reaches the live turn."""
        with self._guard:
            existing = self._orchestrators.get(session.id)
        if existing is not None:
            return existing
        factory = _orchestrator_class()
        if factory is None:
            return None
        try:
            made = factory(session, store=self._db(),
                           scheduler=council_scheduler.scheduler_for(session),
                           events=council_events.stream_for(session.id),
                           invoker=self._invoker_for(session),
                           executor=self._executor_for(session),
                           clock=self._clock)
        except Exception:  # noqa: BLE001 - a coordinator that cannot be built is absent
            logger.exception("council service: could not build an orchestrator for %s",
                             session.id)
            return None
        with self._guard:
            return self._orchestrators.setdefault(session.id, made)

    def _invoker_for(self, session: CouncilSession) -> Any:
        if self._invoker is not None:
            return self._invoker
        from src.council import adapters as council_adapters

        return council_adapters.default_invoker()

    def _executor_for(self, session: CouncilSession) -> Any:
        if self._executor is not None:
            return self._executor
        from src.council import adapters as council_adapters

        return council_adapters.default_executor(owner=session.owner,
                                                 workspace=session.workspace)

    # -- the two checks `update` needs -------------------------------------

    def _turn_in_flight(self, session_id: str) -> bool:
        try:
            turns = self._db().list_turns(session_id)
        except Exception:  # noqa: BLE001 - unreadable turns are not a licence
            logger.exception("council service: reading turns of %s failed", session_id)
            return True
        return any(_text(getattr(turn, "state", "")) not in TERMINAL_TURN_STATES
                   for turn in turns or ())

    def _recap_seats(self, session: CouncilSession) -> None:
        """Narrow every seat to what the room's new policy consents to."""
        store = self._db()
        try:
            seats = store.list_participants(session.id)
        except Exception:  # noqa: BLE001 - a read path
            logger.exception("council service: reading seats of %s failed", session.id)
            return
        for seat in seats or ():
            capped = council_participants.effective_profile(
                seat, session_policy=session.policy)
            if capped == seat.tool_profile:
                continue
            try:
                store.update_participant(session.id, seat.id, {"tool_profile": capped})
            except Exception:  # noqa: BLE001 - one seat is not the room
                logger.exception("council service: could not narrow %s", seat.id)
                continue
            logger.info("council service: %s narrowed to %r by the new policy %r",
                        seat.id, capped, session.policy)


# ── module-level helpers ───────────────────────────────────────────────────

class _MissingArgument(Exception):
    """A command arrived without something it cannot be executed without.

    Its own class so `command()` can answer `missing_argument` instead of
    `command_failed`: the caller's fix is different, and a token that cannot
    tell "you forgot the turn id" from "the engine refused" is a token nobody
    can branch on (§13).
    """


def _need(kw: Mapping[str, Any], key: str, command: str) -> str:
    value = _text(kw.get(key))
    if not value:
        raise _MissingArgument(f"{command} needs {key}")
    return value


async def _maybe_await(value: Any) -> Any:
    """Await a coroutine, pass anything else through.

    The orchestrator's commands are coroutines; a test double's are often not,
    and a service that only worked against `async def` would be a service only
    testable with a full coordinator.
    """
    if asyncio.iscoroutine(value) or isinstance(value, asyncio.Future):
        return await value
    return value


def _outcome(value: Any) -> Dict[str, Any]:
    """A command's answer as a mapping a route can serialise."""
    if value is None:
        return {}
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return dict(to_dict())
        except Exception:  # noqa: BLE001 - an unserialisable outcome is still an outcome
            logger.exception("council service: an outcome could not be serialised")
    if isinstance(value, Mapping):
        return dict(value)
    return {"value": value}


def _budgets(raw: Any) -> Dict[str, Any]:
    """Budgets as the contract's reader wants them, without inventing any.

    An empty mapping means "the defaults", which is what `CouncilBudgets`
    already does; a malformed one is left to `CouncilBudgets.parse` to refuse
    by name, because a budget quietly replaced by a generous default is a bill
    nobody agreed to.
    """
    if raw is None:
        return {}
    if isinstance(raw, CouncilBudgets):
        return raw.to_dict()
    if isinstance(raw, Mapping):
        return dict(raw)
    raise CouncilError("session.budgets", "expected a mapping of limits", got=raw)


def _with_profile(seat: Any, profile: str) -> Any:
    """The same seat at a narrower profile."""
    return type(seat).parse({**seat.to_dict(), "tool_profile": profile})


def _with_roles(seat: Any, roles: Sequence[str]) -> Any:
    """The same seat wearing another hat, before the profile is recomputed."""
    return type(seat).parse({**seat.to_dict(), "roles": list(roles)})


def _completion_modes() -> Tuple[str, ...]:
    """The completion modes the agent-profile layer declares, or `()`.

    Read from `agent_profiles.contracts` rather than restated: §7.1 says the
    council carries the name that layer chose and does not re-declare the list.
    An empty tuple is the honest answer when that layer is not in the build —
    a form that offers a mode nothing can resolve is worse than one that
    offers none.
    """
    try:
        from src.agent_profiles.contracts import COMPLETION_MODES

        return tuple(COMPLETION_MODES)
    except Exception as exc:  # noqa: BLE001 - a form is not worth an import error
        logger.debug("council service: completion modes are unavailable (%s)", exc)
        return ()


# ── the process-wide service ───────────────────────────────────────────────

_SERVICE: Optional[CouncilService] = None
_SERVICE_GUARD = threading.Lock()


def service() -> CouncilService:
    """The shared service.  Built on first use, dropped by `reset_service()`."""
    global _SERVICE
    with _SERVICE_GUARD:
        if _SERVICE is None:
            _SERVICE = CouncilService()
        return _SERVICE


def reset_service() -> None:
    """Forget the shared service.

    For tests and for a subsystem being shut down.  It cancels nothing: a turn
    still running belongs to the task holding it, and cancelling from here
    would stop work the user never asked to stop (§25: an effect whose outcome
    became uncertain is the thing this codebase refuses to create).
    """
    global _SERVICE
    with _SERVICE_GUARD:
        _SERVICE = None
