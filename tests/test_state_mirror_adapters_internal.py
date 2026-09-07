"""tests/test_state_mirror_adapters_internal.py -- the internal sources.

Everything here runs against DOUBLES of the registries, not against the real
ones, and that is the point rather than a convenience. The adapters exist to
survive a source that is missing, broken or lying, and none of those three
states can be produced on demand from a real `dispatch` job or a real ComfyUI
render. Doubles are also the only way to hold four run registries in one
assertion: the whole claim of `runs.py` is that it unifies them, and a test
that could only see whichever ones happened to be running would never check it.

The four things worth proving, and the failure each one is about:

* **an observation this build can make is an observation this build can read
  back.** Every one is re-parsed through its own contract, so a misspelled
  schema field fails here rather than in production, where it would show up as
  an adapter that silently reports nothing.
* **a broken source costs itself.** Raising, absent and answering nonsense, for
  every adapter: `observe()` answers `[]` and never raises, because one
  registry on fire must not take down what the mirror knows about the rest of
  the machine.
* **nobody invents an owner.** The registries behind these adapters keep
  `owner` fields of their own that mean other things -- an objective's is the
  word `agent`, a media row's is whatever was stored -- and one of those
  reaching an entity id would file one person's state under another's. The
  doubles below deliberately carry the WRONG owner so the assertion has
  something to catch.
* **`blocked_by` is derived, and a finished dependency blocks nothing.** That
  is the one field in this package computed rather than read, and getting it
  backwards would mark the whole project blocked forever.

The artifacts adapter is the exception to the doubles: it runs against a real
temporary SQLite file and real bytes on disk, because both fields worth having
from it (`exists`, and the free integrity comparison against the content-
addressed name) are statements about a filesystem, and a fake filesystem would
prove nothing about either.
"""
from __future__ import annotations

import hashlib
import importlib
import sys
import types

import pytest

from src.state_mirror import contracts
from src.state_mirror.adapters import ADAPTER_FACTORIES
from src.state_mirror.adapters import runs as runs_module
from src.state_mirror.adapters.approvals import ApprovalsAdapter
from src.state_mirror.adapters.artifacts import ArtifactsAdapter
from src.state_mirror.adapters.base import Scope
from src.state_mirror.adapters.council import CouncilAdapter
from src.state_mirror.adapters.objectives import ObjectivesAdapter, objective_identifier
from src.state_mirror.adapters.runs import RunsAdapter
from src.state_mirror.adapters.sessions import SessionsAdapter

OWNER = "alice"
#: The owner every double stores on its own rows. Never `OWNER`, so a test
#: that passes cannot be passing because the two happened to agree.
FOREIGN_OWNER = "someone-else"

SCOPE = Scope(owner=OWNER, project_id="proj-1", workspace="/tmp/ws", limit=50)

ADAPTERS = (RunsAdapter, ApprovalsAdapter, ArtifactsAdapter,
            ObjectivesAdapter, CouncilAdapter, SessionsAdapter)


# -- installing a double where both import spellings will find it -----------
#
# `from src import dispatch` resolves the ATTRIBUTE on the `src` package first
# and only falls back to `sys.modules` when there is none, so patching
# `sys.modules` alone leaves the real module in place for any package already
# imported. Both are patched here, and monkeypatch puts both back.

def _mod(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def _install(monkeypatch, dotted: str, module: types.ModuleType) -> None:
    monkeypatch.setitem(sys.modules, dotted, module)
    parent_name, _, leaf = dotted.rpartition(".")
    if parent_name:
        monkeypatch.setattr(importlib.import_module(parent_name), leaf,
                            module, raising=False)


def _absent(monkeypatch, dotted: str) -> None:
    """What a build without this module looks like from inside an adapter.

    `None` in `sys.modules` is how the import system spells "this was tried and
    is not here"; the attribute has to go too, or the parent package would keep
    handing out the real one.
    """
    monkeypatch.setitem(sys.modules, dotted, None)
    parent_name, _, leaf = dotted.rpartition(".")
    if parent_name:
        monkeypatch.delattr(importlib.import_module(parent_name), leaf,
                            raising=False)


def _raising(dotted: str, names) -> types.ModuleType:
    def boom(*_a, **_kw):
        raise RuntimeError(f"{dotted} is on fire")

    return _mod(dotted, **{name: boom for name in names})


def _nonsense(dotted: str, names) -> types.ModuleType:
    """Every entry point answers something of the wrong shape.

    An object rather than `None`: a source that answers `None` is easy to
    survive by accident, and the shapes that actually break an adapter are the
    ones that are truthy and unindexable.
    """
    return _mod(dotted, **{name: (lambda *_a, **_kw: object()) for name in names})


# -- the doubles ------------------------------------------------------------

class _Job:
    """Just enough of `dispatch.DispatchJob` for the adapter to read."""

    def __init__(self) -> None:
        self.id = "job-1"
        self.status = "running"
        self.events = [{"ts": 1_700_000_000.0, "name": "job", "message": "verifying"},
                       {"ts": 1_700_000_060.0, "name": "w1", "event": "done"}]


def _dispatch() -> types.ModuleType:
    job = _Job()
    listed = [{"id": "job-1", "owner": FOREIGN_OWNER, "title": "wire the adapter",
               "status": "running", "created": 1_699_999_000.0,
               "started": 1_699_999_100.0, "finished": None}]
    return _mod(
        "src.dispatch",
        list_jobs=lambda owner, limit=50: list(listed),
        get=lambda job_id: job if job_id == "job-1" else None,
        compact=lambda _job: {
            "phase": "verifying",
            "progress": {"w1": {"last_event": "done"}, "w2": {"last_event": "tick"}},
            "result": {"proof": {"verdict": "proved"}},
        },
        worker_states=lambda _job: {"w1": {"state": "finished", "why": "it said so"}},
    )


def _agent_runs() -> types.ModuleType:
    return _mod(
        "src.agent_runs",
        active_session_ids=lambda: ["sess-a"],
        get_status=lambda _sid: "running",
        get_run_id=lambda _sid: "run-a",
        queued_positions=lambda: {"sess-b": 2},
        queue_snapshot=lambda: {"default": {"active": 1, "limit": 2, "waiting": [
            {"run_id": "run-b", "label": "second in line", "position": 1}]}},
    )


def _media_runs() -> types.ModuleType:
    return _mod("src.media_runs", recent=lambda *, owner="", limit=20: [{
        "id": "med-1", "workflow": "image.product", "version": "1.0.0",
        # The row's own `engine` is the technology; the adapter must report the
        # REGISTRY instead, or nobody can tell it what to ask to revalidate.
        "engine": "comfyui", "status": "running", "owner": FOREIGN_OWNER,
        "created_at": "2026-01-01T00:00:00Z", "started_at": "2026-01-01T00:00:05Z",
        "ended_at": None}])


def _bg_jobs() -> types.ModuleType:
    return _mod("src.bg_jobs", refresh=lambda: {"bg-1": {
        "id": "bg-1", "session_id": "sess-a", "command": "pip install ruff",
        "status": "running", "started_at": 1_699_999_500.0, "ended_at": None,
        "exit_code": None}})


def _approval_store() -> types.ModuleType:
    card = types.SimpleNamespace(
        id="apr-1", plan=types.SimpleNamespace(action="network"), status="pending",
        owner=FOREIGN_OWNER, requested_at="2026-01-01T00:00:00Z",
        expires_at="2026-01-01T00:30:00Z", uses_left=1)
    return _mod("src.approval_store",
                pending=lambda *, owner="", limit=50: [card],
                get=lambda _approval_id: card)


def _objectives() -> types.ModuleType:
    """Three objectives: a done dep, an open dep, and the work behind both.

    `owner` on every record is the literal word an objective stores -- who last
    touched it -- which is exactly the value that must never reach an entity id.
    """
    payload = {
        "objectives": [
            {"id": "OBJ-1", "title": "the dependency that landed", "status": "done",
             "priority": 2, "owner": "agent", "last_actor": "agent",
             "updated_at": "2026-01-01T00:00:00Z", "deps": []},
            {"id": "OBJ-2", "title": "the dependency that did not", "status": "open",
             "priority": 1, "owner": "user", "last_actor": "user",
             "updated_at": "2026-01-02T00:00:00Z", "deps": []},
            {"id": "OBJ-3", "title": "the work", "status": "blocked",
             "priority": 1, "owner": "agent", "last_actor": "agent",
             "updated_at": "2026-01-03T00:00:00Z", "deps": ["OBJ-1", "OBJ-2"]},
        ],
        "edges": [{"from": "OBJ-3", "to": "OBJ-1"}, {"from": "OBJ-3", "to": "OBJ-2"}],
        "scores": {"OBJ-3": {"score": 0.8125, "components": {}, "hint": None}},
        "log": [{"kind": "evidence", "id": "OBJ-3"},
                {"kind": "evidence", "id": "OBJ-3"},
                {"kind": "ADD", "id": "OBJ-1"}],
    }
    return _mod("services.objectives",
                dashboard_payload=lambda _project, log_limit=50, **kwargs: payload)


def _projects() -> types.ModuleType:
    """An explicit scoped project without reading the real registry."""
    return _mod("services.projects",
                get_store=lambda: types.SimpleNamespace(
                    get=lambda _project_id, _owner=None: {'id': _project_id, 'owner': _owner, 'workspace': '/tmp/ws'}))


def _council_service() -> types.ModuleType:
    room = types.SimpleNamespace(
        id="room-1", title="the design review", policy="debate", status="active",
        owner=FOREIGN_OWNER, participants=("p1", "p2"), revision=3,
        created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:10:00Z")
    svc = types.SimpleNamespace(
        list=lambda *, owner="", status="", limit=50: [room],
        state=lambda session_id, *, owner="": {"session_id": session_id,
                                               "running": True},
        ledger=lambda session_id, *, owner="": {
            "session_id": session_id,
            "tasks": [{"id": "t1", "status": "running"},
                      {"id": "t2", "status": "done"}],
            "open_objections": [{"id": "o1"}],
            "held_claims": [{"id": "c1"}, {"id": "c2"}],
        },
    )
    return _mod("src.council.service", service=lambda: svc)


def _session_manager():
    rows = {
        "sess-a": types.SimpleNamespace(id="sess-a", name="build", mode="agent",
                                        project_id="proj-1", owner=FOREIGN_OWNER),
        "sess-b": types.SimpleNamespace(id="sess-b", name="chat", mode=None,
                                        project_id=None, owner=FOREIGN_OWNER),
    }
    return types.SimpleNamespace(get_sessions_for_user=lambda username=None: rows)


#: Adapter name -> the modules it reads and the entry points it calls on each.
#: `_break` walks this, so an adapter that grows a source has to add it here
#: before the degradation test can pass over it in silence.
SOURCES = {
    "runs": {
        "src.dispatch": ("list_jobs", "get", "compact", "worker_states"),
        "src.agent_runs": ("active_session_ids", "get_status", "get_run_id",
                           "queued_positions", "queue_snapshot"),
        "src.media_runs": ("recent",),
        "src.bg_jobs": ("refresh",),
    },
    "approvals": {"src.approval_store": ("pending", "get")},
    "artifacts": {"core.database": ("SessionLocal", "ArtifactRow")},
    "objectives": {"services.objectives": ("dashboard_payload",)},
    "council": {"src.council.service": ("service",)},
    "sessions": {
        "core.models": ("get_session_manager_instance",),
        "src.agent_runs": ("active_session_ids", "get_status", "queued_positions"),
    },
}


@pytest.fixture()
def world(tmp_path, monkeypatch):
    """Every source answering, so an empty result means a real failure."""
    for dotted, module in (("src.dispatch", _dispatch()),
                           ("src.agent_runs", _agent_runs()),
                           ("src.media_runs", _media_runs()),
                           ("src.bg_jobs", _bg_jobs()),
                           ("src.approval_store", _approval_store()),
                           ("services.objectives", _objectives()),
                           ("services.projects", _projects()),
                           ("src.council.service", _council_service())):
        _install(monkeypatch, dotted, module)

    import core.models as core_models
    monkeypatch.setattr(core_models, "get_session_manager_instance",
                        lambda: _session_manager(), raising=False)

    digest, engine = _artifact_world(tmp_path, monkeypatch)
    try:
        yield digest
    finally:
        # Windows will not delete tmp_path while sqlite still holds the file.
        engine.dispose()


def _artifact_world(tmp_path, monkeypatch):
    """A real store directory and a real row, and one artifact that lies.

    `art-1` is intact: its bytes are on disk under the content-addressed name
    the store mints. `art-2` has a name that is a well-formed digest and a
    `sha256` column that disagrees with it, which is the corruption the free
    comparison exists to find -- and its file is not there at all.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from core import database as db_mod
    from core.database import ArtifactRow, Base
    from src import artifact_store

    url = "sqlite:///" + (tmp_path / "artifacts.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    Base.metadata.create_all(bind=engine)

    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setattr(artifact_store, "ARTIFACT_STORE_DIR", str(store))

    payload = b"a small deliverable"
    digest = hashlib.sha256(payload).hexdigest()
    (store / f"{digest}.txt").write_bytes(payload)

    db = db_mod.SessionLocal()
    try:
        db.add(ArtifactRow(id="art-1", kind="text", filename=f"{digest}.txt",
                           sha256=digest, byte_size=len(payload),
                           owner=OWNER, project_id="proj-1"))
        db.add(ArtifactRow(id="art-2", kind="text", filename=f"{'a' * 64}.txt",
                           sha256=digest, byte_size=len(payload),
                           owner=OWNER, project_id="proj-1"))
        db.commit()
    finally:
        db.close()
    return digest, engine


def _break(monkeypatch, adapter_name: str, mode: str) -> None:
    for dotted, names in SOURCES[adapter_name].items():
        if mode == "absent":
            _absent(monkeypatch, dotted)
        elif mode == "raising":
            _install(monkeypatch, dotted, _raising(dotted, names))
        else:
            _install(monkeypatch, dotted, _nonsense(dotted, names))


def _by_field(observations, field: str):
    """entity id -> the value of `field`, for the observation that carried it."""
    return {obs.entity_id: obs.state[field]
            for obs in observations if field in obs.state}


# -- the contract holds -----------------------------------------------------

@pytest.mark.parametrize("factory", ADAPTERS, ids=lambda f: f.name)
def test_every_observation_survives_its_own_contract(world, factory):
    """Build one and parse it back. A schema typo has to fail here.

    An adapter that names a field its schema does not declare produces nothing
    at all in production -- `observation()` logs and drops it -- so without
    this the symptom is an adapter that looks like a source with no data.
    """
    adapter = factory()
    seen = adapter.observe(SCOPE)
    assert seen, f"{adapter.name} observed nothing in a world built for it"
    for obs in seen:
        again = contracts.StateObservation.parse(obs.to_dict())
        assert again.entity_id == obs.entity_id
        assert again.schema == obs.schema
        assert again.state == obs.state
        assert obs.source == adapter.name
        assert obs.schema in adapter.schemas
        assert obs.epistemic in contracts.EPISTEMICS
        assert set(obs.state) <= set(contracts.schema_fields(obs.schema))


def test_every_adapter_this_file_covers_is_one_the_package_actually_uses():
    """The six internal adapters must all be in `ADAPTER_FACTORIES`.

    A SUBSET check and not an equality one, deliberately. This file owns the
    six sources that read another subsystem's registry; the five that read the
    machine (workspace, services, models, hardware, connections) are covered by
    `test_state_mirror_adapters_local.py` and are equally real. An equality
    assertion here would fail every time the package grew a source it does not
    own, which is exactly what it did -- and the reflex fix for that failure is
    to shrink `ADAPTER_FACTORIES`, which would silently unwire five working
    adapters to make a test in the wrong file go green.

    The complementary half -- that no adapter in the package is missing from
    `ADAPTER_FACTORIES`, and that none is left uncovered by any test file --
    lives in `tests/test_state_mirror_wiring.py`, which can see all of them.
    """
    missing = sorted(f.__name__ for f in ADAPTERS if f not in ADAPTER_FACTORIES)
    assert not missing, (
        f"{missing} are proven here and not listed in ADAPTER_FACTORIES; an "
        "adapter absent from that tuple is invisible to every sweep, query and "
        "screen no matter how green its own tests are")


# -- a broken source costs itself -------------------------------------------

@pytest.mark.parametrize("mode", ("raising", "absent", "nonsense"))
@pytest.mark.parametrize("factory", ADAPTERS, ids=lambda f: f.name)
def test_an_adapter_answers_nothing_rather_than_raising(world, monkeypatch,
                                                        factory, mode):
    _break(monkeypatch, factory.name, mode)
    adapter = factory()
    assert adapter.discover(SCOPE) == []
    assert adapter.observe(SCOPE) == []
    assert adapter.relations(SCOPE) == []


@pytest.mark.parametrize("mode", ("raising", "absent", "nonsense"))
def test_one_broken_run_registry_does_not_cost_the_other_three(world, monkeypatch,
                                                               mode):
    """The claim `runs.py` exists to make, tested one registry at a time.

    A union of four sources is only worth building if it degrades to a union
    of three. Breaking them one by one is the only way to catch a reader that
    was accidentally wired into the same guard as its neighbours.
    """
    for dotted, names in SOURCES["runs"].items():
        with monkeypatch.context() as patched:
            if mode == "absent":
                _absent(patched, dotted)
            elif mode == "raising":
                _install(patched, dotted, _raising(dotted, names))
            else:
                _install(patched, dotted, _nonsense(dotted, names))
            engines = {obs.state["engine"] for obs in RunsAdapter().observe(SCOPE)
                       if "engine" in obs.state}
        broken = dotted.rsplit(".", 1)[-1].replace("_runs", "").replace("_jobs", "")
        assert len(engines) == 3, f"breaking {dotted} cost more than {broken}"


# -- nobody invents an owner ------------------------------------------------

@pytest.mark.parametrize("factory", ADAPTERS, ids=lambda f: f.name)
def test_an_adapter_never_invents_an_owner(world, factory):
    """Every row in the doubles is stored under `FOREIGN_OWNER`.

    So an entity carrying the scope's owner is proof the adapter took it from
    the scope, and one carrying anything else is the leak this test is for.
    """
    adapter = factory()
    found = adapter.discover(SCOPE)
    assert found, f"{adapter.name} discovered nothing in a world built for it"
    for item in found:
        assert item.owner == OWNER
        assert contracts.parse_entity_id(item.id)["owner"] == OWNER
        assert FOREIGN_OWNER not in item.id
    for obs in adapter.observe(SCOPE):
        assert obs.owner == OWNER
        assert contracts.parse_entity_id(obs.entity_id)["owner"] == OWNER
    for edge in adapter.relations(SCOPE):
        assert edge.owner == OWNER


# -- runs.py: four registries, four engines ---------------------------------

def test_runs_maps_all_four_registries_with_a_distinct_engine(world):
    seen = RunsAdapter().observe(SCOPE)
    engines = {}
    for obs in seen:
        engine = obs.state.get("engine")
        if engine:
            engines.setdefault(engine, set()).add(obs.entity_id)

    assert set(engines) == set(runs_module.ENGINES)
    for engine, ids in engines.items():
        for ident in ids:
            parts = contracts.parse_entity_id(ident)
            assert parts["kind"] == "run"
            # Engine-prefixed, so two registries minting the same run id stay
            # two runs instead of silently merging into one entity.
            assert parts["identifier"].startswith(f"{engine}:")

    # The media row calls its own engine `comfyui`. The field names the
    # registry to ask, which is `media`, or nothing can revalidate it.
    assert "comfyui" not in engines
    assert contracts.entity_id("run", OWNER, "media:med-1") in engines["media"]


def test_runs_keeps_a_workers_word_apart_from_what_it_measured(world):
    """Three epistemologies, three observations, never averaged.

    `reducers` stamps one epistemology on every field it folds, so a worker's
    own account of itself travelling in the same observation as a registry's
    status would promote a rumour to a measurement.
    """
    by_epistemic = {}
    for obs in RunsAdapter().observe(SCOPE):
        by_epistemic.setdefault(obs.epistemic, set()).update(obs.state)

    assert "status" in by_epistemic["observed"]
    assert "worker_states" in by_epistemic["reported"]
    assert "progress" in by_epistemic["derived"]
    assert "proof_status" in by_epistemic["derived"]
    assert "worker_states" not in by_epistemic["observed"]


# -- objectives.py: blocked_by is derived -----------------------------------

def test_objectives_derives_blocked_by_and_a_done_dep_blocks_nothing(world):
    blocked = _by_field(ObjectivesAdapter().observe(SCOPE), "blocked_by")
    assert blocked[contracts.entity_id("objective", OWNER, objective_identifier(SCOPE, "OBJ-3"))] == ["OBJ-2"]
    # OBJ-1 is done, so it appears in nobody's blockers and has none of its own.
    assert blocked[contracts.entity_id("objective", OWNER, objective_identifier(SCOPE, "OBJ-1"))] == []


def test_objectives_emit_the_dependency_edge_and_only_the_live_block(world):
    edges = {(e.from_id, e.kind, e.to_id, e.origin)
             for e in ObjectivesAdapter().relations(SCOPE)}
    work = contracts.entity_id("objective", OWNER, objective_identifier(SCOPE, "OBJ-3"))
    landed = contracts.entity_id("objective", OWNER, objective_identifier(SCOPE, "OBJ-1"))
    pending = contracts.entity_id("objective", OWNER, objective_identifier(SCOPE, "OBJ-2"))

    assert (work, "depends_on", landed, "declared") in edges
    assert (work, "depends_on", pending, "declared") in edges
    assert (work, "blocked_by", pending, "observed") in edges
    assert not any(e[:3] == (work, "blocked_by", landed) for e in edges)


def test_objectives_carry_the_impact_score_and_the_evidence_they_have(world):
    seen = ObjectivesAdapter().observe(SCOPE)
    work = contracts.entity_id("objective", OWNER, objective_identifier(SCOPE, "OBJ-3"))
    assert _by_field(seen, "impact")[work] == 0.8125
    assert _by_field(seen, "evidence_count")[work] == 2


# -- artifacts.py: the filesystem, and the comparison that is free ----------

def test_artifacts_report_the_disk_and_compare_the_recorded_digest(world):
    seen = ArtifactsAdapter().observe(SCOPE)
    intact = contracts.entity_id("artifact", OWNER, "art-1")
    corrupt = contracts.entity_id("artifact", OWNER, "art-2")

    exists = _by_field(seen, "exists")
    assert exists[intact] is True
    assert exists[corrupt] is False
    assert _by_field(seen, "byte_size")[intact] == len(b"a small deliverable")
    # Absent, not zero: an empty file is a real size and a missing one is not.
    assert corrupt not in _by_field(seen, "byte_size")

    integrity = _by_field(seen, "integrity")
    assert integrity[intact] == "ok"
    assert integrity[corrupt] == "mismatch"
    assert all(obs.epistemic == "derived" for obs in seen
               if "integrity" in obs.state)


def test_artifacts_leave_integrity_unsampled_when_nothing_recorded_it(world):
    """A row nobody hashed is not a row that passed.

    The comparison is free only because the store is content-addressed. A
    legacy name that is not a digest has nothing to compare against, and the
    field has to be absent -- which the reducer reads as "not sampled" --
    rather than `ok`, which is the most expensive way to be wrong here.
    """
    from core import database as db_mod
    from core.database import ArtifactRow

    db = db_mod.SessionLocal()
    try:
        db.add(ArtifactRow(id="art-3", kind="text", filename="legacy-import.txt",
                           sha256=None, owner=OWNER, project_id="proj-1"))
        db.commit()
    finally:
        db.close()

    legacy = contracts.entity_id("artifact", OWNER, "art-3")
    seen = ArtifactsAdapter().observe(SCOPE)
    assert legacy in _by_field(seen, "exists")
    assert legacy not in _by_field(seen, "integrity")


# -- sessions.py: "we did not look" is not "nothing is running" -------------

def test_sessions_report_the_run_and_where_it_sits_in_the_queue(world):
    seen = SessionsAdapter().observe(SCOPE)
    busy = contracts.entity_id("session", OWNER, "sess-a")
    waiting = contracts.entity_id("session", OWNER, "sess-b")

    assert _by_field(seen, "active")[busy] is True
    assert _by_field(seen, "active")[waiting] is False
    # `run_state.v1` declares no queue position; this is the field that does.
    assert _by_field(seen, "queued_position")[waiting] == 2
    assert busy not in _by_field(seen, "queued_position")


def test_sessions_leave_active_unsampled_when_the_runs_cannot_be_read(world,
                                                                      monkeypatch):
    """With `agent_runs` gone, `active` is absent -- never `False`.

    A sidebar that says "idle" about a chat whose worker is still writing is
    how a second instruction lands in a running turn, and `False` is exactly
    what an adapter that returned empty collections on failure would produce.
    """
    _absent(monkeypatch, "src.agent_runs")
    seen = SessionsAdapter().observe(SCOPE)
    assert seen, "the sessions themselves are still readable"
    assert _by_field(seen, "active") == {}
    assert _by_field(seen, "queued_position") == {}


# -- council.py: rooms live in `real`, and the counts come off the ledger ----

def test_council_rooms_are_real_namespace_entities_with_ledger_counts(world):
    adapter = CouncilAdapter()
    room = contracts.entity_id("council", OWNER, "room-1")
    found = adapter.discover(SCOPE)
    assert [item.id for item in found] == [room]
    # A room is an entity of kind `council` in `real`. The `council:` NAMESPACE
    # is a room's own scratch world, and putting the rooms there would hide
    # every one of them from any query about the machine.
    assert contracts.parse_entity_id(room)["namespace"] == contracts.REAL_NAMESPACE

    seen = adapter.observe(SCOPE)
    assert _by_field(seen, "open_objections")[room] == 1
    assert _by_field(seen, "held_claims")[room] == 2
    assert _by_field(seen, "running_tasks")[room] == 1
    assert _by_field(seen, "turn_state")[room] == "running"
    assert _by_field(seen, "policy")[room] == "debate"


# -- approvals.py -----------------------------------------------------------

def test_approvals_report_the_card_and_derive_that_it_needs_a_person(world):
    seen = ApprovalsAdapter().observe(SCOPE)
    card = contracts.entity_id("approval", OWNER, "apr-1")
    assert _by_field(seen, "status")[card] == "pending"
    assert _by_field(seen, "action")[card] == "network"
    assert _by_field(seen, "uses_left")[card] == 1
    assert _by_field(seen, "requires_human")[card] is True
    assert all(obs.epistemic == "derived" for obs in seen
               if "requires_human" in obs.state)
