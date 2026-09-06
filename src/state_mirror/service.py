"""state_mirror/service.py -- the one door into the mirror, and the owner check.

Shaped after `src/council/service.py` and holding the same rule for the same
reason: **every method takes `owner` as an argument from the AUTHENTICATED
caller, never from a payload**, and an entity belonging to somebody else is
answered exactly as an entity that does not exist -- `None`, `[]`, `{}`, no
exception, no id, no revision, nothing that separates "not yours" from "not
there". A mirror is a description of a machine, and a refusal that leaked the
shape of somebody else's machine would be worse than no mirror at all.

`""` is a real owner. This install runs single-user by default and
`owner_identity.effective_storage_owner` answers `""` there, so nothing in this
file reads an empty owner as "every owner" -- `persistence.ANY_OWNER` is
`None`, it is what a maintenance sweep passes, and no method here passes it.

Three more rules:

* **Reads never raise.** Not one of `entities`, `entity`, `history`,
  `changes`, `conflicts`, `diagnostics`, `events`, `project` or `situation`
  can throw. A page that 500s because one row is corrupt is a page that cannot
  show the user what is wrong with their machine, which is the one job this
  subsystem has.

* **The feature flag gates what costs the machine, and nothing else.**
  `agent_state_mirror` off means no sweep and no refresh: no adapter runs, no
  subprocess is forked, no registry is walked. Everything already recorded
  stays readable. A flag that hid the reads would turn "stop spending cycles on
  this" into "lose the diagnostics", and the moment somebody reaches for the
  switch is the moment they most want to see what the machine was doing. The
  flag is read live inside the function, never captured at import, so flipping
  it in Settings takes effect on the next call rather than the next restart --
  the same shape `routes/council_routes.py::enabled()` uses.

* **A refusal is a value with a stable token.** `ERRORS` is closed; the
  sentence beside it is prose for a person and may be reworded any day. A
  caller branches on the token.

What this module is NOT: it holds no reducer, no freshness policy and no
adapter. `ingest`, `reconcile`, `queries` and `projection` do the work; this
decides who may ask.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.state_mirror import events as _events
from src.state_mirror import freshness as _freshness
from src.state_mirror import persistence as _persistence
from src.state_mirror import projection as _projection
from src.state_mirror import queries as _queries
from src.state_mirror import reconcile as _reconcile
from src.state_mirror.contracts import REAL_NAMESPACE, StateEntity

logger = logging.getLogger(__name__)

__all__ = [
    "ERRORS",
    "SETTING",
    "enabled",
    "StateMirrorService",
    "service",
    "reset_service",
]

#: The setting that gates the sweeps. Default OFF: this subsystem forks
#: nvidia-smi, walks a working tree and opens seven registries, and a feature
#: that starts doing that the moment it is merged is a feature that changes the
#: machine before anybody asked it to.
SETTING = "agent_state_mirror"

#: The stable tokens a write may answer with. A caller branches on these; the
#: `detail` beside them is for a human.
ERRORS: Tuple[str, ...] = (
    "not_found",            # no such entity, or not this owner's -- same answer
    "disabled",             # the feature flag is off; reads still work
    "invalid_argument",
    "unknown_situation",
    "sweep_failed",
)


def enabled() -> bool:
    """`agent_state_mirror`. Off = no sweep may RUN; every read still answers.

    Read live on each call rather than captured at import, so flipping the
    switch in Settings takes effect on the next request instead of the next
    restart. The import is inside the function for the same reason it is in
    `routes/council_routes.py`: a settings module is not on this package's
    import path and must not become so.
    """
    try:
        from src.settings import get_setting

        return bool(get_setting(SETTING, False))
    except Exception:  # noqa: BLE001 - a read never fails over a settings lookup
        logger.debug("state mirror: %s unreadable; treated as off", SETTING,
                     exc_info=True)
        return False


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
                logger.exception("state mirror: a row could not be serialised")
                continue
        if isinstance(item, Mapping):
            out.append(dict(item))
    return out


def _fail(code: str, detail: str, **extra: Any) -> Dict[str, Any]:
    """One refusal, in the one shape every caller branches on."""
    if code not in ERRORS:  # pragma: no cover - guarded by a test
        logger.error("state mirror: %r is not in ERRORS %s", code, list(ERRORS))
    answer = {"ok": False, "error": code, "detail": detail}
    answer.update(extra)
    return answer


class StateMirrorService:
    """Read the mirror, refresh it, and decide who may do either.

    One instance per process in normal use (`service()`), and any number in
    tests. It owns two things and nothing else: the ownership check, and the
    decision about whether a sweep is allowed to spend the machine's time.
    """

    def __init__(self, *, store: Any = None, publisher: Any = None,
                 adapters: Any = None) -> None:
        #: `None` means "the process-wide store, looked up per call". Looked up
        #: per call rather than held, because `persistence.use_path` drops the
        #: shared instance and a service holding the old one would keep reading
        #: the database a test just moved away from.
        self._store = store
        #: `None` means "each owner's own stream". A publisher passed in is how
        #: a test watches without a registry.
        self._publisher = publisher
        #: `None` means "every registered adapter". A list is how a test sweeps
        #: without building the real eleven against a real machine.
        self._adapters = adapters
        self._guard = threading.RLock()

    # -- ownership: the check every method starts with ---------------------

    def _db(self) -> Any:
        return self._store if self._store is not None else _persistence.store()

    def _visible(self, entity_id: str, owner: str) -> Optional[StateEntity]:
        """The entity, or `None` -- for absent and for somebody else's alike.

        `get_entity` is keyed by id and is not owner-scoped, so this is where
        the scoping happens for every method that starts from an id. A read
        never raises out of it.
        """
        ident = _text(entity_id)
        if not ident:
            return None
        try:
            row = self._db().get_entity(ident)
        except Exception:  # noqa: BLE001 - a read path
            logger.exception("state mirror: reading entity %s failed", ident)
            return None
        if row is None or row.owner != _text(owner):
            return None
        return row

    def _scope(self, owner: str, namespace: str, project_id: str,
               limit: int) -> _queries.Scope:
        return _queries.Scope(owner=_text(owner),
                              namespace=_text(namespace) or REAL_NAMESPACE,
                              project_id=_text(project_id),
                              limit=int(limit or 200))

    # -- reads: none of these raise, ever ----------------------------------

    def entities(self, *, owner: str, namespace: str = "", project_id: str = "",
                 kind: str = "", include_retired: bool = False,
                 limit: int = 200, now: Any = None) -> List[Dict[str, Any]]:
        """This owner's entities, each with a SUMMARY of what we believe.

        A summary and not the state: a listing of two hundred entities that
        carried every field with its four pieces of metadata would be a
        megabyte to render a sidebar. The revision, the combined freshness and
        the counts are what a list view shows, and `entity()` is one call away
        for the rest.
        """
        holder = _text(owner)
        rows = _queries.list_entities(
            owner=holder, namespace=_text(namespace),
            project_id=_text(project_id), kind=_text(kind),
            include_retired=bool(include_retired), limit=int(limit or 200),
            store=self._store)
        # One query for the states rather than one per entity. The store's own
        # listing excludes retired entities, so a listing that asked for them
        # falls back to a single read for each one it did not find -- which is
        # the rare case and the only one that needs it.
        known = {state.entity_id: state for state in _queries.list_states(
            owner=holder, namespace=_text(namespace),
            project_id=_text(project_id), kind=_text(kind),
            limit=max(int(limit or 200), len(rows)), store=self._store, now=now)}
        out: List[Dict[str, Any]] = []
        for entity in rows:
            body = entity.to_dict()
            state = known.get(entity.id)
            if state is None and entity.retired():
                state = _queries.get(entity.id, owner=holder,
                                     store=self._store, now=now)
            body["state"] = self._summary(state)
            out.append(body)
        return out

    @staticmethod
    def _summary(state: Any) -> Dict[str, Any]:
        """What a list view needs about one entity's state, and no values.

        `freshness` here is `freshness.combined`, which answers `mixed` when
        the fields disagree -- a word that means "open it and look" rather than
        an average. Averaging would let one fresh field make a row of stale
        ones look current.
        """
        if state is None:
            return {"known": False, "revision": 0, "freshness": "unknown",
                    "fields": 0, "unknown_fields": 0, "conflicts": 0,
                    "updated_at": ""}
        return {
            "known": True,
            "revision": state.revision,
            "freshness": _freshness.combined(
                [f.freshness for f in state.fields.values()]),
            "fields": len(state.fields),
            "unknown_fields": len(state.unknown_fields()),
            "conflicts": len(state.conflicts),
            "updated_at": state.updated_at,
            "revision_ref": state.revision_ref(),
        }

    def entity(self, entity_id: str, *, owner: str,
               now: Any = None) -> Dict[str, Any]:
        """Everything about one entity: the row, the state, its edges and its
        arguments. `{}` when it is absent or somebody else's."""
        row = self._visible(entity_id, owner)
        if row is None:
            return {}
        holder = _text(owner)
        state = _queries.get(row.id, owner=holder, store=self._store, now=now)
        return {
            "entity": row.to_dict(),
            "state": state.to_dict() if state is not None else {},
            "summary": self._summary(state),
            "relations": _rows(_queries.relations(row.id, owner=holder,
                                                  store=self._store)),
            "conflicts": _rows(_queries.conflicts(owner=holder,
                                                  entity_id=row.id,
                                                  store=self._store)),
        }

    def history(self, entity_id: str, *, owner: str, limit: int = 50,
                source: str = "") -> List[Dict[str, Any]]:
        """The observation log for one entity, newest first.

        `persistence.observations` is keyed by entity and takes no owner, so
        the visibility check happens here BEFORE the read -- which is the whole
        reason this method exists rather than callers reaching for the store.
        """
        row = self._visible(entity_id, owner)
        if row is None:
            return []
        try:
            return _rows(self._db().observations(row.id, limit=int(limit or 50),
                                                 source=_text(source)))
        except Exception:  # noqa: BLE001 - a read path
            logger.exception("state mirror: reading the history of %s failed", row.id)
            return []


    def changes(self, cursor: int = 0, *, owner: str, namespace: str = "",
                project_id: str = "", limit: int = 200,
                now: Any = None) -> Dict[str, Any]:
        """What changed after `cursor`, and the cursor to ask with next time.

        The cursor moves only on a material change, so a consumer polling this
        is woken for things that moved and never for things that were merely
        looked at again. That is the property Delta Engine compares revisions
        on, and it is enforced in `persistence.put_state`, not here.
        """
        states, head = _queries.changed_since(
            int(cursor or 0), owner=_text(owner), namespace=_text(namespace),
            project_id=_text(project_id), limit=int(limit or 200),
            store=self._store, now=now)
        return {"states": _rows(states), "cursor": head,
                "count": len(states)}

    def conflicts(self, *, owner: str, entity_id: str = "", namespace: str = "",
                  open_only: bool = True, limit: int = 100) -> List[Dict[str, Any]]:
        """Disagreements, recorded rather than decided (section 9.2).

        Scoped by owner in the store AND, when an entity is named, gated on
        that entity being visible first: a conflict names two sources and two
        values, and answering one for an entity the caller cannot see would
        leak both.
        """
        target = _text(entity_id)
        if target and self._visible(target, owner) is None:
            return []
        return _rows(_queries.conflicts(
            owner=_text(owner), entity_id=target, namespace=_text(namespace),
            open_only=bool(open_only), limit=int(limit or 100),
            store=self._store))

    def project(self, *, owner: str, entity_refs: Sequence[str] = (),
                fields: Sequence[str] = (),
                minimum_freshness: str = "informational",
                project_id: str = "", namespace: str = REAL_NAMESPACE,
                token_budget: int = 0, reason: str = "",
                now: Any = None) -> Dict[str, Any]:
        """A bounded, permission-checked projection for one consumer (section 1.3).

        The keys are `"<entity_id>#<field>"`; see `projection.py` for why that
        spelling and not `"<kind>.<field>"`. An entity the caller may not see
        is absent from the answer and from its `entity_refs`.
        """
        try:
            built = _projection.project(
                owner=_text(owner), entity_refs=list(entity_refs or ()),
                fields=list(fields or ()),
                minimum_freshness=_text(minimum_freshness) or "informational",
                project_id=_text(project_id),
                namespace=_text(namespace) or REAL_NAMESPACE,
                token_budget=int(token_budget or 0), reason=_text(reason),
                store=self._store, now=now)
        except Exception:  # noqa: BLE001 - a read path
            logger.exception("state mirror: projecting for %r failed", owner)
            return {}
        body = built.to_dict()
        body["sufficient"] = built.sufficient()
        return body

    def situation(self, name: str, *, owner: str, namespace: str = "",
                  project_id: str = "", limit: int = 200,
                  now: Any = None) -> Dict[str, Any]:
        """One of the six deterministic situation queries (section 10).

        A name outside `queries.SITUATIONS` answers a refusal listing what
        exists, rather than an empty list -- an empty list for a typo reads as
        "nothing is running", which is the most dangerous wrong answer this
        subsystem can give.
        """
        wanted = _text(name)
        if wanted not in _queries.SITUATIONS:
            return _fail("unknown_situation",
                         f"{wanted or '(empty)'!r} is not a situation query",
                         situations=list(_queries.SITUATIONS))
        scope = self._scope(owner, namespace, project_id, limit)
        rows = _queries.situation(wanted, scope, store=self._store, now=now)
        return {"ok": True, "situation": wanted, "rows": rows,
                "count": len(rows)}

    def events(self, *, owner: str) -> Any:
        """This owner's `StateEventStream`, or `None` if one cannot be made.

        Keyed by owner, so there is no id to hand in and therefore no side
        channel around the ownership check the rest of this file makes: a
        caller can only ever be given their own stream.
        """
        try:
            return _events.stream_for(_text(owner))
        except Exception:  # noqa: BLE001 - a read path
            logger.exception("state mirror: opening the stream for %r failed", owner)
            return None


    # -- the two things that cost the machine ------------------------------
    #
    # Both gated by `agent_state_mirror`, and only these two. A sweep forks
    # nvidia-smi, walks a working tree and opens seven registries; a read opens
    # one SQLite file that is already there.

    def refresh(self, *, owner: str, entity_ids: Sequence[str] = (),
                sources: Sequence[str] = (), project_id: str = "",
                namespace: str = REAL_NAMESPACE, workspace: str = "",
                limit: int = 100, now: Any = None) -> Dict[str, Any]:
        """Ask the sources behind these entities to look again.

        The adapters to wake are named directly (`sources`) or derived from the
        entities (`entity_ids`), because a field records the source that wrote
        it and that is the only thing that could refresh it. An entity the
        caller cannot see contributes nothing and is not named in the answer.

        Neither argument means neither a source nor an entity, and that is
        REFUSED rather than quietly promoted into a full sweep. "Refresh
        nothing in particular" and "sweep the whole machine" have very
        different costs, and a caller that meant the second has `reconcile`.
        """
        if not enabled():
            return _fail("disabled",
                         f"{SETTING} is off; the mirror stays readable and "
                         "nothing will go and look")
        holder = _text(owner)
        wanted = {s for s in (_text(x) for x in (sources or ())) if s}
        seen: List[str] = []
        for raw in (entity_ids or ()):
            row = self._visible(raw, holder)
            if row is None:
                continue
            seen.append(row.id)
            state = self._db().get_state(row.id)
            for held in (state.fields.values() if state is not None else ()):
                if held.source:
                    wanted.add(held.source)
        if not wanted:
            return _fail("invalid_argument",
                         "refresh needs at least one source, or at least one "
                         "entity this owner can see whose fields name one; to "
                         "ask every source, call reconcile")
        chosen = self._resolve_adapters(wanted)
        if not chosen:
            return _fail("not_found",
                         "none of those sources is an adapter in this build",
                         sources=sorted(wanted))
        report = self._sweep(holder, chosen, project_id=project_id,
                             namespace=namespace, workspace=workspace,
                             limit=limit, now=now)
        if isinstance(report, dict):
            return report
        return {"ok": True, "refreshed": sorted(wanted), "entities": seen,
                "report": report.to_dict()}

    def reconcile(self, *, owner: str, project_id: str = "",
                  namespace: str = REAL_NAMESPACE, workspace: str = "",
                  limit: int = 100, now: Any = None) -> Dict[str, Any]:
        """Sweep every source: section 8.4, gated by the flag.

        This is the expensive one and the one that can retire an entity, which
        is why it is a command with a token rather than a read that happens to
        write.
        """
        if not enabled():
            return _fail("disabled",
                         f"{SETTING} is off; the mirror stays readable and no "
                         "sweep will run")
        report = self._sweep(_text(owner), self._adapters,
                             project_id=project_id, namespace=namespace,
                             workspace=workspace, limit=limit, now=now)
        if isinstance(report, dict):
            return report
        return {"ok": True, "report": report.to_dict()}

    def _sweep(self, owner: str, adapters: Any, *, project_id: str,
               namespace: str, workspace: str, limit: int, now: Any) -> Any:
        """Run one sweep, or answer a refusal. Returns a `SweepReport` or a dict.

        `reconcile.sweep` does not raise, but building its scope imports the
        adapter package, and a build where that import is broken must answer a
        token rather than a traceback out of a route.
        """
        try:
            return _reconcile.sweep(
                owner=owner,
                scope={"project_id": _text(project_id),
                       "namespace": _text(namespace) or REAL_NAMESPACE,
                       "workspace": _text(workspace),
                       "limit": int(limit or 100)},
                adapters=adapters, store=self._store,
                publisher=self._publisher, now=now)
        except Exception as exc:  # noqa: BLE001 - a command answers with a token
            logger.exception("state mirror: the sweep for %r failed", owner)
            return _fail("sweep_failed",
                         f"the sweep could not run: {type(exc).__name__}: {exc}")

    def _resolve_adapters(self, names: Any) -> List[Any]:
        """The registered adapters whose names are in `names`.

        Function-local import: `state_mirror.adapters` builds eleven modules,
        and a process that only reads the mirror must not pay for that.
        """
        wanted = {str(n) for n in (names or ())}
        if self._adapters is not None:
            return [a for a in self._adapters
                    if str(getattr(a, "name", "")) in wanted]
        try:
            from src.state_mirror.adapters.base import all_adapters

            return [a for a in all_adapters()
                    if str(getattr(a, "name", "")) in wanted]
        except Exception:  # noqa: BLE001 - no adapters is not a crash
            logger.exception("state mirror: the adapter registry is unavailable")
            return []


    # -- diagnostics -------------------------------------------------------

    def diagnostics(self, *, owner: str) -> Dict[str, Any]:
        """What the mirror knows about ITSELF (section 18). Never raises.

        Four tables, and each answers a question the others cannot. The source
        health says which adapters are answering; the store stats say how much
        is held and how big the file is; `freshness.policy_table()` says how
        long every field is guaranteed for, which is the number a user has to
        see before they can argue with a `stale`; and the adapter list says
        what this build can observe at all.

        The three global tables carry no owner data -- a source is named
        `hardware`, a TTL is a number -- so they are the same for everybody.
        The event stream stats are this owner's, because the stream is.
        """
        holder = _text(owner)
        out: Dict[str, Any] = {
            "owner": holder,
            "enabled": enabled(),
            "setting": SETTING,
            "sources": [],
            "store": {},
            "freshness_policy": {},
            "adapters": [],
            "events": {},
            "situations": list(_queries.SITUATIONS),
            "event_names": list(_events.STATE_EVENTS),
        }
        db = self._db()
        try:
            out["sources"] = list(db.sources())
        except Exception:  # noqa: BLE001 - a read path
            logger.exception("state mirror: reading source health failed")
        try:
            out["store"] = dict(db.stats())
        except Exception:  # noqa: BLE001 - a read path
            logger.exception("state mirror: reading store stats failed")
        try:
            out["freshness_policy"] = _freshness.policy_table()
        except Exception:  # noqa: BLE001 - a read path
            logger.exception("state mirror: reading the freshness policy failed")
        out["adapters"] = self._adapter_table()
        stream = self.events(owner=holder)
        if stream is not None:
            try:
                out["events"] = dict(stream.stats())
            except Exception:  # noqa: BLE001 - a read path
                logger.exception("state mirror: reading stream stats failed")
        return out

    def _adapter_table(self) -> List[Dict[str, Any]]:
        """Every registered adapter, its schemas, and whether it can answer.

        `available()` is CALLED rather than assumed, because that is the whole
        value of the row: an adapter that is registered and cannot import its
        source is exactly the thing a diagnostics screen exists to show, and a
        list that only named what was registered would report a broken build as
        a healthy one. Each call is wrapped -- an adapter that raises out of
        `available` is reported as unavailable with the reason on it.
        """
        try:
            if self._adapters is not None:
                built = list(self._adapters)
            else:
                from src.state_mirror.adapters.base import all_adapters

                built = list(all_adapters())
        except Exception as exc:  # noqa: BLE001 - a read path
            logger.exception("state mirror: the adapter registry is unavailable")
            return [{"name": "", "available": False,
                     "detail": f"{type(exc).__name__}: {exc}"}]
        rows: List[Dict[str, Any]] = []
        for adapter in built:
            row: Dict[str, Any] = {
                "name": str(getattr(adapter, "name", "")
                            or type(adapter).__name__),
                "schemas": list(getattr(adapter, "schemas", ()) or ()),
                "available": False,
                "detail": "",
            }
            try:
                row["available"] = bool(adapter.available())
            except Exception as exc:  # noqa: BLE001 - one adapter, not the table
                row["detail"] = f"{type(exc).__name__}: {exc}"
            rows.append(row)
        return sorted(rows, key=lambda r: r["name"])


# -- the process-wide service -----------------------------------------------

_SERVICE: Optional[StateMirrorService] = None
_SERVICE_GUARD = threading.Lock()


def service() -> StateMirrorService:
    """The shared service. Built on first use, dropped by `reset_service()`."""
    global _SERVICE
    with _SERVICE_GUARD:
        if _SERVICE is None:
            _SERVICE = StateMirrorService()
        return _SERVICE


def reset_service() -> None:
    """Forget the shared service.

    For tests and for a reconfigured process (a new data directory, a new
    workspace). It cancels nothing and closes nothing: a sweep still running
    belongs to whatever started it, and the streams outlive this object on
    purpose -- see `events.reset_streams` for why forgetting one is a
    deliberate act rather than a side effect.
    """
    global _SERVICE
    with _SERVICE_GUARD:
        _SERVICE = None
