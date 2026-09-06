"""Four run registries, four shapes, one `run_state.v1`.

This application starts work in four unrelated places and nothing joins them
up. `src/dispatch.py` keeps in-memory `DispatchJob` objects mirrored to disk;
`src/agent_runs.py` keeps detached chat runs in a module dict behind a lane
queue; `src/media_runs.py` keeps rows in sqlite for renders on a GPU;
`src/bg_jobs.py` keeps a JSON file of detached OS processes. Each answers "what
is running" for its own corner and none of them can answer it for the machine,
so "is anything still going?" has four answers and a person has to know which
page to open to get each one.

That is what this module adds, and the only thing it adds. It writes nothing,
starts nothing and cancels nothing: it reads all four and states each run in
the same vocabulary, so a query over `run_state.v1` sees the whole machine.

`engine` is the field that keeps the union honest. It names the REGISTRY a run
came from and not the technology behind it -- a media run's `engine` here is
`media` even though its own row says `comfyui`, because the question this
field answers is "who do I ask to revalidate this", and the answer is
`src/media_runs.py`. The engine also prefixes the identifier, so two registries
that both mint `run_9f2c` cannot collide into one entity.

Three observations per run, never one. `reducers` stamps a single epistemology
on every field of an observation, and these runs carry three different kinds of
claim: a status read out of a registry that owns it is `observed`, a worker's
own account of itself is `reported` (section 2.1 says so in as many words), and
a fraction this module computed is `derived`. Merging them would mean either
promoting a worker's word to a measurement or demoting a registry's own row to
a rumour.

Entity ids: `run://<owner>/<namespace>/<engine>:<run_id>`, minted by
`contracts.entity_id`. `source_refs` carry `<engine>:<run_id>` for the same
reason `context_engine/adapters` documents its own schemes -- a projection has
to be able to name the system it would revalidate against.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from src.contracts.base import now_iso
from src.state_mirror.adapters.base import (
    Scope,
    ThreadedAdapter,
    entity,
    iso_or_blank,
    observation,
)
from src.state_mirror.contracts import StateEntity, StateObservation, entity_id

logger = logging.getLogger(__name__)

SCHEMA = "run_state.v1"

#: The four registries, as the closed vocabulary of the `engine` field. A run
#: whose engine is not one of these is a run nothing knows how to refresh.
ENGINES: Tuple[str, ...] = ("dispatch", "agent", "media", "bg")

#: Dispatch worker events that mean the worker stopped. Everything else is
#: still in flight, which is what makes the fraction below a floor rather than
#: a guess.
_FINISHED_EVENTS: Tuple[str, ...] = ("done", "error")

#: How much of a command or a title is worth keeping as a run's label. Long
#: enough to recognise the work, short enough that a state row stays a state
#: row and not a copy of the instruction.
_LABEL_CHARS = 200

__all__ = ["RunsAdapter", "RunRow", "ENGINES", "SCHEMA"]


def _word(value: Any, limit: int = 128) -> str:
    """A trimmed string, or `""`. Total: registries hold `None` in text
    columns and one of those reaching a contract is a lost observation."""
    if value is None or isinstance(value, bool):
        return ""
    return str(value).strip()[:limit]


def _progress(board: Any) -> Optional[float]:
    """How much of a dispatch job is finished, as 0..1, or `None`.

    Derived from the per-worker board `dispatch.compact` already builds, which
    seeds every task as `queued` before any of them start -- so a 4-task job at
    parallelism 2 is 0.0 and not 0.5. `None` when there is no board: a job with
    no workers has no progress, and reporting 0.0 for it would say the opposite
    of what is true.
    """
    if not isinstance(board, Mapping) or not board:
        return None
    total = 0
    done = 0
    for cell in board.values():
        if not isinstance(cell, Mapping):
            continue
        total += 1
        if _word(cell.get("last_event")) in _FINISHED_EVENTS:
            done += 1
    if total <= 0:
        return None
    return round(done / total, 4)


def _last_event_at(job: Any) -> str:
    """When dispatch last stamped an event on this job, as ISO, or `""`.

    Dispatch stamps `ts` on every event it appends, so the newest one is the
    closest thing this application has to a run heartbeat. The deque rotates at
    `EVENTS_KEPT`, which does not matter here: the newest is still the newest.
    """
    newest = 0.0
    for ev in list(getattr(job, "events", None) or ()):
        if not isinstance(ev, Mapping):
            continue
        stamp = ev.get("ts")
        if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
            newest = max(newest, float(stamp))
    return iso_or_blank(newest) if newest > 0 else ""


@dataclass(frozen=True)
class RunRow:
    """One run from one registry, already in `run_state.v1` vocabulary.

    Three field buckets rather than one because `reducers` stamps a single
    epistemology on every field of an observation: a status read out of
    dispatch's own job table and a fraction this module computed cannot
    honestly travel together.
    """

    engine: str
    run_id: str
    label: str = ""
    created_at: str = ""
    updated_at: str = ""
    observed: Mapping[str, Any] = field(default_factory=dict)
    reported: Mapping[str, Any] = field(default_factory=dict)
    derived: Mapping[str, Any] = field(default_factory=dict)

    def identifier(self) -> str:
        """The half of the entity id below the owner. Engine-prefixed, so two
        registries that both mint `run_9f2c` stay two runs."""
        return f"{self.engine}:{self.run_id}"

    def buckets(self) -> Tuple[Tuple[str, Mapping[str, Any]], ...]:
        """The three field sets, with `engine` stamped onto the observed one.

        Stamped here rather than written by each reader, so the engine in the
        entity id and the engine in the state cannot drift apart: they are the
        same attribute read twice.
        """
        return (("observed", {**dict(self.observed), "engine": self.engine}),
                ("reported", self.reported), ("derived", self.derived))


class RunsAdapter(ThreadedAdapter):
    """Every run this machine knows about, from all four registries."""

    name = "runs"
    schemas = (SCHEMA,)

    def available(self) -> bool:
        """True when at least one registry imports.

        Deliberately not "all four": a build without ComfyUI configured still
        has dispatch jobs, and an adapter that reported itself unavailable
        because one of its four sources is missing would hide the three that
        answer.
        """
        for module in ("dispatch", "agent_runs", "media_runs", "bg_jobs"):
            try:
                __import__(f"src.{module}")
                return True
            except Exception as exc:                           # noqa: BLE001
                logger.debug("runs adapter: src.%s unavailable: %s", module, exc)
        return False

    def discover(self, scope: Scope) -> List[StateEntity]:
        out: List[StateEntity] = []
        for row in self._collect(scope):
            made = entity("run", row.identifier(), scope=scope, schema=SCHEMA,
                          display_name=row.label, labels=(row.engine,),
                          source_refs=(row.identifier(),),
                          created_at=row.created_at, updated_at=row.updated_at)
            if made is not None:
                out.append(made)
        return out

    def observe(self, scope: Scope) -> List[StateObservation]:
        # One stamp for the whole sweep. `observed_at` is when WE looked, not
        # when the registry last changed: the second is what the fields say,
        # and conflating them would make a run that finished an hour ago read
        # as freshly observed only because it is still in the list.
        stamp = now_iso()
        out: List[StateObservation] = []
        for row in self._collect(scope):
            target = self._safe(entity_id, "run", scope.owner, row.identifier(),
                                namespace=scope.namespace, default="")
            if not target:
                continue
            for epistemic, body in row.buckets():
                made = observation(target, self.name, body, scope=scope,
                                   schema=SCHEMA, epistemic=epistemic,
                                   observed_at=stamp,
                                   source_revision=row.updated_at)
                if made is not None:
                    out.append(made)
        return out

    def _collect(self, scope: Scope) -> List[RunRow]:
        """Every run from every registry that answers.

        Each reader is wrapped separately, so a sqlite file that will not open
        costs the media runs and leaves the dispatch jobs standing. Read on
        every call rather than cached: a sweep that reused a list from earlier
        in the same sweep would report a run as live after it finished, which
        is the failure this whole subsystem exists to prevent.
        """
        rows: List[RunRow] = []
        for reader in (self._dispatch_rows, self._agent_rows,
                       self._media_rows, self._bg_rows):
            rows.extend(self._safe(reader, scope, default=[]) or [])
        # A row from an engine this build does not name is a row nothing knows
        # how to refresh. Dropping it here is what keeps `ENGINES` a contract
        # rather than a comment.
        return [row for row in rows if row.engine in ENGINES]

    # -- src/dispatch.py ---------------------------------------------------

    def _dispatch_rows(self, scope: Scope) -> List[RunRow]:
        """Multi-worker jobs. The richest of the four, and the only one with a
        heartbeat, a phase and a proof."""
        from src import dispatch

        out: List[RunRow] = []
        # `visible_to` reads a falsy owner as "single-user, show everything",
        # so `""` has to arrive as None rather than as an owner nobody matches.
        listed = dispatch.list_jobs(scope.owner or None, scope.capped(200))
        for raw in list(listed or []):
            if not isinstance(raw, Mapping):
                continue
            job_id = _word(raw.get("id"), 64)
            if not job_id:
                continue
            row = self._safe(self._dispatch_row, dispatch, raw, job_id)
            if row is not None:
                out.append(row)
        return out

    def _dispatch_row(self, dispatch: Any, raw: Mapping[str, Any],
                      job_id: str) -> RunRow:
        observed: Dict[str, Any] = {
            "status": _word(raw.get("status"), 64) or None,
            "label": _word(raw.get("title"), _LABEL_CHARS) or None,
            "started_at": iso_or_blank(raw.get("started")
                                       or raw.get("created")) or None,
        }
        reported: Dict[str, Any] = {}
        derived: Dict[str, Any] = {}

        # The list view is built with `include_result=False`, so the phase, the
        # board and the proof are only reachable through the job object. This
        # is a dict lookup: `list_jobs` has already loaded the mirrors.
        job = self._safe(dispatch.get, job_id)
        if job is not None:
            detail = self._safe(dispatch.compact, job, default={}) or {}
            if isinstance(detail, Mapping):
                observed["phase"] = _word(detail.get("phase")) or None
                derived["progress"] = _progress(detail.get("progress"))
                result = detail.get("result")
                proof = result.get("proof") if isinstance(result, Mapping) else None
                if isinstance(proof, Mapping):
                    # `prove.prove()` reconciles what was observed on disk with
                    # what the workers claimed. Its verdict is arithmetic over
                    # observations, which is what `derived` means.
                    derived["proof_status"] = _word(proof.get("verdict")) or None
            # A worker's own output said this about itself and nothing checked
            # it -- section 2.1's `reported`, exactly.
            states = self._safe(dispatch.worker_states, job, default={}) or {}
            if isinstance(states, Mapping) and states:
                reported["worker_states"] = dict(states)
            observed["last_heartbeat"] = _last_event_at(job) or None

        return RunRow(engine="dispatch", run_id=job_id,
                      label=_word(raw.get("title"), _LABEL_CHARS),
                      created_at=iso_or_blank(raw.get("created")),
                      updated_at=iso_or_blank(raw.get("finished")
                                              or raw.get("started")),
                      observed=observed, reported=reported, derived=derived)

    # -- src/agent_runs.py -------------------------------------------------

    def _agent_rows(self, scope: Scope) -> List[RunRow]:
        """Detached chat runs, plus whatever is waiting for a lane.

        Two passes because the module answers two different questions and
        neither covers the other: `active_session_ids()` knows which SESSIONS
        are busy, `queue_snapshot()` knows which RUNS are waiting, and only the
        second carries a label. A run appears in both while it holds a session
        and waits for its lane, so the session pass wins and the queue pass
        only fills in run ids it has not already seen.
        """
        from src import agent_runs

        seen: Dict[str, RunRow] = {}
        for raw in list(self._safe(agent_runs.active_session_ids, default=[]) or []):
            session_id = _word(raw, 128)
            if not session_id:
                continue
            # A session marked busy by `mark_busy` (a delegated worker chat)
            # has no detached run and therefore no run id. Naming it by its
            # session is not a second id for the same run -- there is no run
            # object to have one -- and dropping it would hide work that is
            # genuinely in flight.
            run_id = (_word(self._safe(agent_runs.get_run_id, session_id), 64)
                      or f"session:{session_id}")
            status = _word(self._safe(agent_runs.get_status, session_id), 64)
            seen[run_id] = RunRow(engine="agent", run_id=run_id,
                                  observed={"status": status or None})

        snapshot = self._safe(agent_runs.queue_snapshot, default={}) or {}
        if isinstance(snapshot, Mapping):
            for lane in snapshot.values():
                for row in self._queued(lane):
                    seen.setdefault(row.run_id, row)
        return list(seen.values())

    @staticmethod
    def _queued(lane: Any) -> List[RunRow]:
        """The runs waiting in one lane.

        `status` is `queued` because that is what waiting for a lane is. The
        lane's own position is dropped here: `run_state.v1` declares no field
        for it, and putting `2` into `phase` would be a number pretending to be
        a word. It reaches the mirror through `session_state.queued_position`
        instead, which is the field that exists for it.
        """
        if not isinstance(lane, Mapping):
            return []
        out: List[RunRow] = []
        for waiting in list(lane.get("waiting") or []):
            if not isinstance(waiting, Mapping):
                continue
            run_id = _word(waiting.get("run_id"), 64)
            if not run_id:
                continue
            label = _word(waiting.get("label"), _LABEL_CHARS)
            out.append(RunRow(engine="agent", run_id=run_id, label=label,
                              observed={"status": "queued",
                                        "label": label or None}))
        return out

    # -- src/media_runs.py -------------------------------------------------

    def _media_rows(self, scope: Scope) -> List[RunRow]:
        """Renders on a media engine.

        The only registry of the four with an owner column that means the same
        thing this subsystem means by owner, so the filter is passed straight
        through. `poll()` is NOT called: it asks the engine over the network,
        and a sweep that probed every unfinished render would turn a read of
        the mirror into a fan-out of HTTP calls. What the row says is what this
        adapter reports, and the projection's refresh action is where somebody
        decides that asking the engine is worth it.
        """
        from src import media_runs

        out: List[RunRow] = []
        for raw in list(media_runs.recent(owner=scope.owner,
                                          limit=scope.capped(200)) or []):
            if not isinstance(raw, Mapping):
                continue
            run_id = _word(raw.get("id"), 64)
            if not run_id:
                continue
            label = _word(" ".join(x for x in (_word(raw.get("workflow")),
                                               _word(raw.get("version"))) if x),
                          _LABEL_CHARS)
            out.append(RunRow(
                engine="media", run_id=run_id, label=label,
                created_at=iso_or_blank(raw.get("created_at")),
                updated_at=iso_or_blank(raw.get("ended_at")
                                        or raw.get("started_at")),
                observed={
                    "status": _word(raw.get("status"), 64) or None,
                    "label": label or None,
                    "started_at": iso_or_blank(raw.get("started_at")
                                               or raw.get("created_at")) or None,
                },
            ))
        return out

    # -- src/bg_jobs.py ----------------------------------------------------

    def _bg_rows(self, scope: Scope) -> List[RunRow]:
        """Detached OS processes started by the agent's `#!bg` shell.

        `refresh()` rather than `_load()` because the status on disk is only
        true after a reconcile: a record still marked `running` whose process
        died without writing an exit file would otherwise be reported as live
        forever. It is idempotent and is what every other reader of this module
        calls.

        These records carry a `session_id` and NO owner. Every one of them is
        therefore stamped with the sweep's owner, which is right on the
        single-user install this ships as and is a real gap on a multi-user
        one -- there is nothing in the record to filter on.
        """
        from src import bg_jobs

        jobs = bg_jobs.refresh()
        out: List[RunRow] = []
        for raw in list((jobs or {}).values() if isinstance(jobs, Mapping) else []):
            if not isinstance(raw, Mapping):
                continue
            job_id = _word(raw.get("id"), 64)
            if not job_id:
                continue
            label = _word(raw.get("command"), _LABEL_CHARS)
            out.append(RunRow(
                engine="bg", run_id=job_id, label=label,
                created_at=iso_or_blank(raw.get("started_at")),
                updated_at=iso_or_blank(raw.get("ended_at")
                                        or raw.get("started_at")),
                observed={
                    "status": _word(raw.get("status"), 64) or None,
                    "label": label or None,
                    "started_at": iso_or_blank(raw.get("started_at")) or None,
                },
            ))
        return out
