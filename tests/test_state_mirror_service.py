"""tests/test_state_mirror_service.py -- the door, the pipeline and the sweep.

The rows this file covers, one test each, and the failure each one is about:

* **a replayed observation is free.** The same look folded twice produces one
  accepted, one duplicate and ONE revision. Without it every retried webhook
  and every reconnecting subscription would tick the revision Delta Engine
  compares, and every comparison would be a false positive.
* **a refusal costs its own row.** One observation this build cannot read must
  not cost the other forty in the batch -- the whole point of a mirror over
  eleven sources is that it keeps working when one of them is broken.
* **`state_changed` carries a diff.** Not the state: a sweep that touched forty
  entities would otherwise push the whole mirror through an SSE connection.
* **a sweep retires what a WORKING adapter stopped reporting, and nothing
  when the adapter broke.** This is the dangerous half of `reconcile`: an
  unguarded sweep reads a failed import as "everything you knew is gone".
* **one owner cannot reach another's entity through any of the eleven public
  methods.** Written as a loop over the methods, because eleven copy-pasted
  tests is eleven places for the twelfth method to be forgotten.
* **the flag gates what costs the machine and nothing else.** Off means no
  sweep; it never means a blind user.

The adapters here are DOUBLES. The eleven real ones read this machine, and
none of the three states a sweep has to survive -- a source that is missing,
one that is broken, one that has stopped reporting something -- can be produced
on demand from a real GPU or a real dispatch queue.
"""

from __future__ import annotations

import json

import pytest

from src.contracts.event import EVENT_NAMES
from src.state_mirror import contracts as C
from src.state_mirror import events as events_mod
from src.state_mirror import ingest as ingest_mod
from src.state_mirror import persistence as P
from src.state_mirror import reconcile as reconcile_mod
from src.state_mirror import service as service_mod

OWNER = "alice"
OTHER = "mallory"
T0 = "2026-09-06T12:00:00Z"
T1 = "2026-09-06T13:00:00Z"

RUN = C.entity_id("run", OWNER, "dispatch:job1")
OTHER_RUN = C.entity_id("run", OTHER, "dispatch:job9")

#: A value only the other owner can see. Every answer in the ownership loop is
#: searched for it, so a leak through a field VALUE is caught as well as one
#: through an id.
SECRET_LABEL = "mallorys-private-build"


@pytest.fixture(autouse=True)
def _isolated(tmp_path):
    """Own database, own streams, no shared service."""
    P.use_path(str(tmp_path / "state_mirror.db"))
    events_mod.reset_streams()
    service_mod.reset_service()
    try:
        yield
    finally:
        service_mod.reset_service()
        events_mod.reset_streams()
        P.use_path(None)


# -- doubles ----------------------------------------------------------------

class Recorder:
    """A publisher that keeps what it was told, in order."""

    def __init__(self) -> None:
        self.seen = []

    def publish(self, name, **payload):
        self.seen.append((name, payload))
        return None

    def names(self):
        return [name for name, _ in self.seen]

    def payloads(self, name):
        return [payload for got, payload in self.seen if got == name]


class FakeAdapter:
    """One source of `service_state.v1`, with the three failures dialled in.

    `boom` makes `discover` raise, which is the state a real adapter reaches by
    having its registry renamed under it -- and the one the retirement guard
    exists for.
    """

    schemas = ("service_state.v1",)

    def __init__(self, identifiers, *, name="fake", boom=False,
                 health="available") -> None:
        self.name = name
        self.identifiers = list(identifiers)
        self.boom = boom
        self.health = health

    def available(self):
        return True

    def _id(self, scope, identifier):
        return C.entity_id("service", scope.owner, identifier,
                           namespace=scope.namespace)

    def discover(self, scope):
        if self.boom:
            raise RuntimeError("the registry this adapter reads is on fire")
        return [C.StateEntity.parse({"id": self._id(scope, i),
                                     "schema": "service_state.v1",
                                     "display_name": i})
                for i in self.identifiers]

    def observe(self, scope):
        if self.boom:
            return []
        return [C.StateObservation.parse({
            "entity_id": self._id(scope, i), "source": self.name,
            "schema": "service_state.v1", "epistemic": "observed",
            "observed_at": T0, "state": {"health": self.health}})
            for i in self.identifiers]

    def relations(self, scope):
        return []


def _observe(entity_id, body, *, source="runs", schema="run_state.v1",
             epistemic="observed", at=T0, owner=OWNER):
    return {"entity_id": entity_id, "owner": owner, "source": source,
            "schema": schema, "epistemic": epistemic, "observed_at": at,
            "state": dict(body)}


# -- the event vocabulary ---------------------------------------------------

def test_every_state_event_name_is_in_the_envelope_contract():
    """`STATE_EVENTS` must stay a subset of `EVENT_NAMES`.

    A name declared here and missing there would reach a page perfectly well
    and then be refused by `Event.parse` in the audit that replays it -- the
    failure only shows up in the one place nobody is watching.
    """
    missing = [name for name in events_mod.STATE_EVENTS if name not in EVENT_NAMES]
    assert missing == [], f"declared in state_mirror.events and not in the envelope: {missing}"
    assert len(events_mod.STATE_EVENTS) == len(set(events_mod.STATE_EVENTS))


def test_a_name_the_stream_does_not_declare_becomes_an_error_frame():
    """An unroutable name is published, not raised and not dropped.

    A sweep must not die of a typo in a log line, and a consumer must never be
    handed a name it has never heard of.
    """
    stream = events_mod.stream_for(OWNER)
    event = stream.publish("state.changed", entity_id=RUN)
    assert event.name == "state_error"
    assert event.payload["unknown_event"] == "state.changed"


def test_the_stream_is_keyed_by_owner_and_resumes_without_repeating():
    alice = events_mod.stream_for(OWNER)
    mallory = events_mod.stream_for(OTHER)
    assert alice is not mallory

    first = alice.publish("state_changed", entity_id=RUN, revision=1)
    second = alice.publish("state_changed", entity_id=RUN, revision=2)
    assert mallory.since(0) == [], (
        "state is about a machine, and one owner's machine is not another's")

    assert [e.seq for e in alice.since(0)] == [first.seq, second.seq]
    assert [e.seq for e in alice.since(first.seq)] == [second.seq], (
        "a consumer resuming from the last id it saw gets strictly what followed")
    assert alice.since(second.seq) == []


def test_a_frame_is_unnamed_and_carries_its_name_inside_the_json():
    """A named SSE frame never reaches `onmessage`; this stream speaks the
    dialect the rest of the application already speaks."""
    stream = events_mod.stream_for(OWNER)
    frame = stream.publish("state_changed", entity_id=RUN, revision=1).sse()
    assert frame.startswith("data: ") and frame.endswith("\n\n")
    assert "event:" not in frame
    assert json.loads(frame[len("data: "):])["name"] == "state_changed"


# -- ingest -----------------------------------------------------------------

def test_folding_the_same_observation_twice_moves_the_revision_once():
    """A replayed event is free: one accepted, one duplicate, one revision.

    `StateObservation.identity()` deliberately excludes the id and the arrival
    time, so the same look arriving twice is ONE observation however it got
    here -- and the second fold does no work at all rather than doing harmless
    work.
    """
    body = _observe(RUN, {"status": "running"})
    recorder = Recorder()

    first = ingest_mod.ingest([body], publisher=recorder, now=T0)
    second = ingest_mod.ingest([body], publisher=recorder, now=T0)

    assert (first.accepted, first.duplicates) == (1, 0)
    assert (second.accepted, second.duplicates) == (0, 1)
    assert first.changed_entities == (RUN,)
    assert second.changed_entities == ()

    state = P.store().get_state(RUN)
    assert state.revision == 1, "a replay must not tick the revision"
    assert recorder.names().count("state_changed") == 1
    assert recorder.names().count("state_observation_received") == 1, (
        "a duplicate is not reduced, not stored again and not announced")


def test_a_refused_observation_does_not_stop_the_batch():
    """One row this build cannot read costs that row and nothing else."""
    recorder = Recorder()
    report = ingest_mod.ingest([
        _observe(RUN, {"status": "running"}),
        {"entity_id": "not an entity id", "source": "runs",
         "schema": "run_state.v1", "state": {"status": "running"}},
        _observe(C.entity_id("run", OWNER, "dispatch:job2"), {"status": "queued"}),
    ], publisher=recorder, now=T0)

    assert report.accepted == 2
    assert len(report.refused) == 1
    assert "entity" in report.refused[0]
    assert report.errors == ()
    assert len(report.changed_entities) == 2


def test_state_changed_carries_a_compact_diff_and_the_revision():
    """Never the whole state: a sweep over forty entities would otherwise put
    the entire mirror on the stream to say that one boolean moved."""
    recorder = Recorder()
    ingest_mod.ingest([_observe(RUN, {"status": "running"})],
                      publisher=recorder, now=T0)
    ingest_mod.ingest([_observe(RUN, {"status": "done"}, at=T1)],
                      publisher=recorder, now=T1)

    changed = recorder.payloads("state_changed")
    assert len(changed) == 2
    last = changed[-1]
    assert last["revision"] == 2
    assert last["revision_ref"] == f"state:{RUN}@2"
    assert last["changes"] == [
        {"field": "status", "before": "running", "after": "done",
         "reason": "same epistemology, newer observation"}]
    assert "fields" not in last and "state" not in last


def test_an_observation_mints_the_entity_row_it_needs():
    """A materialised state with no entity row answers no query at all: every
    listing joins `state_entities`."""
    recorder = Recorder()
    ingest_mod.ingest([_observe(RUN, {"status": "running"})],
                      publisher=recorder, now=T0)

    entity = P.store().get_entity(RUN)
    assert entity is not None and entity.kind == "run" and entity.owner == OWNER
    assert [p["entity_id"] for p in recorder.payloads("state_entity_discovered")] == [RUN]
    assert P.store().list_states(owner=OWNER) != []


# -- the sweep --------------------------------------------------------------

def _service(id_: str) -> str:
    return C.entity_id("service", OWNER, id_)


def test_a_sweep_retires_an_entity_that_stopped_being_discovered():
    recorder = Recorder()
    first = reconcile_mod.sweep(owner=OWNER, adapters=[FakeAdapter(["a", "b"])],
                                publisher=recorder, now=T0)
    assert first.entities_seen == 2
    assert first.retired == ()
    assert first.ingest.accepted == 2

    second = reconcile_mod.sweep(owner=OWNER, adapters=[FakeAdapter(["a"])],
                                 publisher=recorder, now=T0)
    assert second.retired == (_service("b"),)
    assert P.store().get_entity(_service("b")).retired() is True
    assert P.store().get_entity(_service("a")).retired() is False
    assert [p["entity_id"] for p in recorder.payloads("state_entity_retired")] \
        == [_service("b")]

    # Retirement is a timestamp, never a delete: "this used to exist" is an
    # answer a sweep has to be able to give.
    assert P.store().get_entity(_service("b")).retired_at != ""
    assert "state_reconcile_started" in recorder.names()
    assert "state_reconcile_completed" in recorder.names()


def test_a_sweep_never_retires_what_a_broken_adapter_owns():
    """The guard, stated as the case it exists for.

    Two sources. One works and stops reporting its entity -- which is a real
    retirement. The other raises out of `discover`, which looks identical from
    the outside (it reported nothing) and must retire nothing at all: an
    unguarded sweep would read one bad import as "everything you knew is gone"
    and break every one of those entities' history in half.
    """
    recorder = Recorder()
    reconcile_mod.sweep(
        owner=OWNER,
        adapters=[FakeAdapter(["kept"], name="steady"),
                  FakeAdapter(["orphan"], name="flaky")],
        publisher=recorder, now=T0)
    assert P.store().get_entity(_service("kept")) is not None
    assert P.store().get_entity(_service("orphan")) is not None

    report = reconcile_mod.sweep(
        owner=OWNER,
        adapters=[FakeAdapter([], name="steady"),
                  FakeAdapter(["orphan"], name="flaky", boom=True)],
        publisher=recorder, now=T0)

    assert report.adapters_failed == ("flaky",)
    assert report.retired == (_service("kept"),), (
        "the working adapter's missing entity is gone; the broken one's is not")
    assert P.store().get_entity(_service("orphan")).retired() is False
    assert any("flaky.discover" in line for line in report.errors)


def test_a_sweep_never_retires_what_another_working_source_still_reports():
    """Ownership is a tie-break about who wrote last, not a claim of exclusivity.

    Two adapters observe the same service and disagree, so the field ends up
    "owned" by whichever spoke most recently. When that one goes quiet the
    other is still reporting the service every sweep -- and retiring it on the
    strength of the tie-break would take away a thing that is demonstrably
    still there.
    """
    recorder = Recorder()
    reconcile_mod.sweep(
        owner=OWNER,
        adapters=[FakeAdapter(["shared"], name="first"),
                  FakeAdapter(["shared"], name="second", health="unavailable")],
        publisher=recorder, now=T0)

    report = reconcile_mod.sweep(
        owner=OWNER,
        adapters=[FakeAdapter(["shared"], name="first"),
                  FakeAdapter([], name="second")],
        publisher=recorder, now=T0)

    assert report.retired == ()
    assert P.store().get_entity(_service("shared")).retired() is False


def test_retiring_an_entity_abandons_the_argument_about_it():
    """`abandoned`, never `resolved`: nothing checked which claim was right.

    And it has to happen, because one live conflict per (entity, field) is a
    unique index: a conflict left open on a retired entity would block that
    pair for ever, so a service that came back would have its next genuine
    disagreement silently joined to the old row.
    """
    recorder = Recorder()
    reconcile_mod.sweep(
        owner=OWNER,
        adapters=[FakeAdapter(["a"], name="first"),
                  FakeAdapter(["a"], name="second", health="unavailable")],
        publisher=recorder, now=T0)

    live = P.store().conflicts(owner=OWNER, entity_id=_service("a"))
    assert [c.field for c in live] == ["health"]
    assert recorder.payloads("state_conflict_detected")

    report = reconcile_mod.sweep(
        owner=OWNER,
        adapters=[FakeAdapter([], name="first"), FakeAdapter([], name="second")],
        publisher=recorder, now=T0)

    assert report.retired == (_service("a"),)
    assert P.store().conflicts(owner=OWNER, entity_id=_service("a")) == []
    settled = P.store().conflicts(owner=OWNER, entity_id=_service("a"),
                                  open_only=False)
    assert [c.status for c in settled] == ["abandoned"]
    ended = recorder.payloads("state_conflict_resolved")
    assert ended and ended[-1]["status"] == "abandoned"


def test_a_sweep_reports_the_fields_that_have_just_aged_out():
    """`state_became_stale` fires on the CROSSING and only on the crossing.

    No source will ever send an event to say that a claim stopped being
    current, so if the sweep does not notice it, nobody does.
    """
    recorder = Recorder()
    reconcile_mod.sweep(owner=OWNER, adapters=[FakeAdapter(["a"])],
                        publisher=recorder, now=T0)
    assert recorder.payloads("state_became_stale") == []

    # An hour later, with the adapter gone quiet: nothing new is observed and
    # the claim ages out from under us.
    later = reconcile_mod.sweep(owner=OWNER, adapters=[FakeAdapter(["a"])],
                                publisher=recorder, now=T1)
    assert later.became_stale == (f"{_service('a')}#health",)
    aged = recorder.payloads("state_became_stale")
    assert aged and aged[-1]["fields"] == ["health"]

    # ...and ageing moved no revision: it is a change to what the state is
    # worth, not to the state.
    assert P.store().get_state(_service("a")).revision == 1

    again = reconcile_mod.sweep(owner=OWNER, adapters=[FakeAdapter(["a"])],
                                publisher=recorder, now=T1)
    assert again.became_stale == (), "the news is the transition, not the state"


def test_a_sweep_records_source_health_and_announces_the_change():
    recorder = Recorder()
    reconcile_mod.sweep(owner=OWNER, adapters=[FakeAdapter(["a"])],
                        publisher=recorder, now=T0)
    healthy = {row["source"]: row["health"] for row in P.store().sources()}
    assert healthy["fake"] == "ok"
    assert recorder.payloads("state_source_degraded") == []

    broken = reconcile_mod.sweep(owner=OWNER,
                                 adapters=[FakeAdapter(["a"], boom=True)],
                                 publisher=recorder, now=T0)
    assert broken.degraded == ("fake",)
    assert {row["source"]: row["health"]
            for row in P.store().sources()}["fake"] == "degraded"

    fixed = reconcile_mod.sweep(owner=OWNER, adapters=[FakeAdapter(["a"])],
                                publisher=recorder, now=T0)
    assert fixed.recovered == ("fake",)


# -- the door ---------------------------------------------------------------

def _public_methods():
    """Every method a caller can reach, read off the class.

    Read rather than listed, so that a twelfth method added without an
    ownership check fails the loop below instead of quietly not being tested.
    """
    return sorted(name for name, value in vars(service_mod.StateMirrorService).items()
                  if not name.startswith("_") and callable(value))


#: One call per public method, all of them aimed at the OTHER owner's entity.
#: A dict rather than eleven tests: eleven copies is eleven places for the
#: twelfth method to be forgotten, and the assertion below checks that this
#: table and the class agree about what exists.
CALLS = {
    "entities": lambda svc: svc.entities(owner=OWNER),
    "entity": lambda svc: svc.entity(OTHER_RUN, owner=OWNER),
    "history": lambda svc: svc.history(OTHER_RUN, owner=OWNER),
    "changes": lambda svc: svc.changes(0, owner=OWNER),
    "conflicts": lambda svc: svc.conflicts(owner=OWNER, entity_id=OTHER_RUN),
    "refresh": lambda svc: svc.refresh(owner=OWNER, entity_ids=[OTHER_RUN]),
    "reconcile": lambda svc: svc.reconcile(owner=OWNER, now=T0),
    "diagnostics": lambda svc: svc.diagnostics(owner=OWNER),
    "events": lambda svc: svc.events(owner=OWNER).stats(),
    "project": lambda svc: svc.project(owner=OWNER, entity_refs=[OTHER_RUN],
                                       fields=["status", "label"],
                                       minimum_freshness="informational",
                                       now=T0),
    "situation": lambda svc: svc.situation("running_work", owner=OWNER, now=T0),
}


def test_the_ownership_loop_covers_every_public_method():
    assert sorted(CALLS) == _public_methods(), (
        "a public method is missing from the ownership loop below")


def test_one_owner_cannot_reach_another_through_any_public_method(monkeypatch):
    """The rule the whole package holds, checked eleven ways at once.

    Not-yours and not-there are answered identically, so the assertion is not
    "an error was raised" -- it is that nothing in the answer names the other
    owner's entity or repeats a value only they can see. The flag is turned ON
    so that `refresh` and `reconcile` are checked on their real path rather
    than being answered `disabled` before the ownership check is reached.
    """
    monkeypatch.setattr(service_mod, "enabled", lambda: True)
    ingest_mod.ingest([_observe(OTHER_RUN,
                                {"status": "running", "label": SECRET_LABEL},
                                owner=OTHER)],
                      publisher=Recorder(), now=T0)
    svc = service_mod.StateMirrorService(publisher=Recorder(), adapters=[])

    for name in sorted(CALLS):
        answer = CALLS[name](svc)
        blob = json.dumps(answer, default=str, sort_keys=True)
        assert OTHER_RUN not in blob, f"{name} named another owner's entity"
        assert SECRET_LABEL not in blob, f"{name} leaked another owner's value"

    # The positive control: the row really is there, so the eleven empty
    # answers above are refusals and not an empty database.
    theirs = svc.entity(OTHER_RUN, owner=OTHER)
    assert theirs["state"]["fields"]["label"]["value"] == SECRET_LABEL


def test_the_flag_gates_sweeps_and_refreshes_and_never_a_read(monkeypatch):
    """Off means "stop spending cycles", never "lose the diagnostics".

    The moment somebody reaches for that switch is the moment they most want to
    see what the machine was doing.
    """
    ingest_mod.ingest([_observe(RUN, {"status": "running"})],
                      publisher=Recorder(), now=T0)
    svc = service_mod.StateMirrorService(publisher=Recorder(),
                                         adapters=[FakeAdapter(["a"])])

    monkeypatch.setattr(service_mod, "enabled", lambda: False)
    assert svc.reconcile(owner=OWNER, now=T0)["error"] == "disabled"
    assert svc.refresh(owner=OWNER, sources=["fake"])["error"] == "disabled"
    assert P.store().get_entity(_service("a")) is None, (
        "a disabled sweep must not have gone and looked")

    assert len(svc.entities(owner=OWNER)) == 1
    assert svc.entity(RUN, owner=OWNER)["state"]["revision"] == 1
    assert svc.history(RUN, owner=OWNER) != []
    assert svc.diagnostics(owner=OWNER)["enabled"] is False

    monkeypatch.setattr(service_mod, "enabled", lambda: True)
    assert svc.reconcile(owner=OWNER, now=T0)["ok"] is True
    assert P.store().get_entity(_service("a")) is not None


def test_the_flag_is_read_live_and_is_off_by_default(monkeypatch):
    """Read inside the call, so Settings takes effect on the next request
    rather than the next restart."""
    import src.settings as settings

    asked = []

    def _absent(key, default=None):
        asked.append(key)
        return default

    monkeypatch.setattr(settings, "get_setting", _absent)
    assert service_mod.enabled() is False
    assert asked == [service_mod.SETTING]

    monkeypatch.setattr(settings, "get_setting", lambda key, default=None: True)
    assert service_mod.enabled() is True, "the flag is not captured at import"


def test_a_refusal_carries_a_token_from_the_closed_vocabulary(monkeypatch):
    monkeypatch.setattr(service_mod, "enabled", lambda: True)
    svc = service_mod.StateMirrorService(adapters=[])

    empty = svc.refresh(owner=OWNER)
    assert empty["error"] == "invalid_argument"
    assert "reconcile" in empty["detail"], (
        "a caller that meant the whole machine must be told which call that is")

    unknown = svc.situation("what_is_it_doing", owner=OWNER)
    assert unknown["error"] == "unknown_situation"
    assert set(unknown["situations"]) == set(
        service_mod._queries.SITUATIONS)

    missing = svc.refresh(owner=OWNER, sources=["not-an-adapter"])
    assert missing["error"] == "not_found"

    for answer in (empty, unknown, missing):
        assert answer["error"] in service_mod.ERRORS


def test_no_read_raises_when_the_store_is_on_fire(monkeypatch):
    """A page that 500s because one row is corrupt is a page that cannot show
    the user what is wrong with their machine."""
    svc = service_mod.StateMirrorService(adapters=[])

    class _Broken:
        def __getattr__(self, name):
            def boom(*args, **kwargs):
                raise RuntimeError("the database is gone")
            return boom

    monkeypatch.setattr(P, "store", lambda: _Broken())

    assert svc.entities(owner=OWNER) == []
    assert svc.entity(RUN, owner=OWNER) == {}
    assert svc.history(RUN, owner=OWNER) == []
    assert svc.changes(0, owner=OWNER) == {"states": [], "cursor": 0, "count": 0}
    assert svc.conflicts(owner=OWNER) == []
    assert svc.project(owner=OWNER, entity_refs=[RUN], fields=["status"],
                       minimum_freshness="informational")["fields"] == {}
    assert svc.situation("running_work", owner=OWNER)["rows"] == []
    assert svc.diagnostics(owner=OWNER)["store"] == {}
    assert svc.events(owner=OWNER) is not None


def test_the_service_is_shared_and_forgettable():
    first = service_mod.service()
    assert service_mod.service() is first
    service_mod.reset_service()
    assert service_mod.service() is not first


def test_a_projection_through_the_service_says_whether_it_is_sufficient():
    """The one number a caller may branch on before an effect."""
    ingest_mod.ingest([_observe(RUN, {"status": "running"})],
                      publisher=Recorder(), now=T0)
    svc = service_mod.StateMirrorService(adapters=[])

    now = svc.project(owner=OWNER, entity_refs=[RUN], fields=["status"],
                      minimum_freshness="action_safe", now=T0)
    assert now["sufficient"] is True
    assert now["fields"][f"{RUN}#status"]["value"] == "running"

    later = svc.project(owner=OWNER, entity_refs=[RUN], fields=["status"],
                        minimum_freshness="action_safe", now=T1)
    assert later["sufficient"] is False
    assert later["refresh_actions"], (
        "a projection that cannot be acted on must say what would fix it")
