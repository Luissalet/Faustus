"""`src/delta_engine/service.py` -- the ORDER of the steps, and both halves of
the switch.

Every test here fixes a rule of the façade rather than a shape of its output.
The packages underneath have their own files; what this one is about is the
sequence, because the sequence is where the honesty of the subsystem lives:

* **the intent is frozen and stored before either end is read** (§1.9.1). Not
  asserted by reading the source of `_run` but by an adapter whose `snapshot`
  LOOKS: it counts the contracts in the store at the moment it is called, and
  a service that compiled the contract afterwards would hand it a zero;
* a contract that is not yours is `not_found`, and one frozen for another
  domain is refused saying both domains;
* a frozen contract and raw intent material together are refused, because
  picking one for the caller discards the other;
* a comparison with NO intent still runs -- "what changed between these two"
  is a real question -- and cannot come back `matched`;
* an end that could not be read is a STORED `inconclusive` delta naming the
  side that failed, never an exception;
* the second identical comparison is served from the store and the adapter is
  not called again; a bumped extractor version is a VISIBLE invalidation
  instead of a silent second row, which is the whole reason the fingerprint
  leaves the versions out;
* `agent_delta_engine` off refuses `create`, `run` and `compile_intent` and
  keeps `get`, `list`, `evidence` and `diagnostics` answering;
* `reclassify` insists on a reason and changes the interpretation only;
* every event a comparison emits is one `events.DELTA_EVENTS` declares, and a
  publisher that raises never costs the delta.

The doubles are deliberate. The adapter is registered with
`registry.register()` -- the one documented override -- and released in the
fixture, and its domain is `code` so the request is one the shipped vocabulary
accepts. Its findings are addressed WITHOUT the `file:` prefix, which keeps
`service._with_proof` a no-op: `integrations/changesets.file_changes` only
builds a ChangeSet out of `file:`-addressed assertions, and a test that built
one would be reaching into another subsystem's store to prove something about
this one.
"""
from __future__ import annotations

import pytest

from src.delta_engine import coverage as coverage_mod
from src.delta_engine import events as delta_events
from src.delta_engine import invariants as invariants_mod
from src.delta_engine import persistence as P
from src.delta_engine import registry, sources
from src.delta_engine import service as service_mod
from src.delta_engine.adapters.base import Element, Extraction, Finding, Snapshot
from src.delta_engine.adapters.code import CODE_TREE_MEDIA_TYPE
from src.delta_engine.contracts import (
    ASSESSMENTS,
    CLASSIFICATIONS,
    CONDITION_KINDS,
    CONFIDENCE,
    COVERAGE_DIMENSIONS,
    DOMAINS,
    EVIDENCE_KINDS,
    EXTRACTION_TIERS,
    INVARIANT_CLASSES,
    INVARIANT_STATUSES,
    OPERATIONS,
    SEVERITIES,
    DeltaError,
    InvariantResult,
)
from src.delta_engine.events import DELTA_EVENTS
from src.delta_engine.service import ERRORS, DeltaEngineService, DeltaServiceError

OWNER = "alice"
OTHER = "bob"
DOMAIN = "code"

#: The invariant the fake adapter reports violated. A real id from
#: `invariants.DOMAIN_DEFAULTS["code"]`, so the frozen contract carries it and
#: `classification._violations_for` can find the class it belongs to.
DEFAULT_INVARIANT = "code.public_symbols_kept"

#: Everything measured, on every axis. Not decoration: `verdict.assess` answers
#: `inconclusive` for an axis the intent needed and nobody measured, and a test
#: about "why is this not `matched`" has to remove that reason first or it
#: passes for the wrong one.
FULL_COVERAGE = {name: 1.0 for name in COVERAGE_DIMENSIONS}


# -- doubles ----------------------------------------------------------------


class _Publisher:
    """The stream the service was HANDED, so a test sees exactly what it said.

    Injected rather than read out of `events.stream_for(owner)` for the reason
    `service._stream` states in its own docstring: a ledger that published to
    the module registry instead of the stream it was given recorded everything
    perfectly and showed nothing.
    """

    def __init__(self) -> None:
        self.published = []

    def publish(self, name, **payload):
        self.published.append((name, payload))
        return None

    def names(self):
        return [name for name, _ in self.published]

    def payloads(self, name):
        return [payload for published, payload in self.published if published == name]


class _Explodes(_Publisher):
    """A page that went away mid-comparison. Publishing must not lose a delta."""

    def publish(self, name, **payload):
        self.published.append((name, payload))
        raise RuntimeError("the page this was announced to is gone")


class _Adapter:
    """A `code` adapter that reads nothing and answers exactly what it is told.

    `snapshots` and `comparisons` are the counters the cache tests read: a hit
    that still called the adapter is not a hit, and counting is the only way to
    tell the two apart from the outside.
    """

    domain = DOMAIN

    def __init__(self, *, findings=(), invariant_results=(), version="1",
                 dimensions=None, explode_on="", on_snapshot=None) -> None:
        self.version = version
        self.findings = tuple(findings)
        self.invariant_results = tuple(invariant_results)
        self.dimensions = dict(FULL_COVERAGE if dimensions is None else dimensions)
        self.explode_on = explode_on
        self.on_snapshot = on_snapshot
        self.snapshots = 0
        self.comparisons = 0

    def available(self) -> bool:
        return True

    def snapshot(self, revision, *, scope):
        self.snapshots += 1
        if self.on_snapshot is not None:
            self.on_snapshot(revision, scope)
        if self.explode_on and revision.hash == self.explode_on:
            raise RuntimeError("this end was gone by the time we looked")
        return Snapshot(revision=revision, tier="parser",
                        elements=(Element(key="src/auth.py#consume_state",
                                          kind="symbol", hash=revision.hash),))

    def compare(self, source, target, *, scope):
        self.comparisons += 1
        return Extraction(
            findings=self.findings,
            coverage=coverage_mod.build(source_readable=True, target_readable=True,
                                        dimensions=self.dimensions),
            extractor_versions={"probe": self.version},
        )

    def check_invariants(self, intent, source, target, *, scope):
        return tuple(self.invariant_results)


# -- fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_engine(tmp_path):
    """Its own database, its own registry, its own streams -- and back again.

    `use_path` before `reset_store` and both again at the end: a test that
    wrote into the real `data/` directory would leave rows on the machine of
    whoever ran the suite, and a registry left holding a fake `code` adapter
    would answer for every test that came after this file.
    """
    P.use_path(str(tmp_path / "delta_engine.db"))
    P.reset_store()
    service_mod.reset_service()
    delta_events.reset_streams()
    try:
        yield
    finally:
        registry.reset()
        service_mod.reset_service()
        delta_events.reset_streams()
        P.reset_store()
        P.use_path(None)


@pytest.fixture(autouse=True)
def engine_on(monkeypatch):
    """The switch, on. Read live inside every method that costs something, so
    patching the module attribute is what turns it on for all of them."""
    monkeypatch.setattr(service_mod, "enabled", lambda: True)


# -- helpers ----------------------------------------------------------------


def _tree(files):
    """An immutable revision holding `{path: text}`. No disk, real sha256."""
    return sources.stash(dict(files), media_type=CODE_TREE_MEDIA_TYPE)


SOURCE = {"src/auth.py": "def consume_state(token):\n    return token\n"}
TARGET = {"src/auth.py": "def consume_state(token):\n    return token * 2\n"}


def _request(**over):
    body = {"domain": DOMAIN,
            "source": _tree(SOURCE).to_dict(),
            "target": _tree(TARGET).to_dict()}
    body.update(over)
    return body


def _finding(**over):
    payload = {"path": "src/auth.py#consume_state", "operation": "modified",
               "before": "token", "after": "token * 2", "method": "python_ast",
               "tier": "parser", "confidence": "high"}
    payload.update(over)
    return Finding(**payload)


def _violation(**over):
    payload = {"invariant_id": DEFAULT_INVARIANT, "status": "violated",
               "severity": "material", "confidence": "high", "tier": "parser",
               "method": "python_ast"}
    payload.update(over)
    return InvariantResult.parse(payload)


def _service(adapter, publisher=None):
    """Install the adapter and hand back a service that publishes where we look.

    `register` takes a zero-argument factory and the registry rebuilds from it,
    so the lambda has to answer the SAME instance every time or the counters
    would be spread over several adapters.
    """
    registry.register(lambda: adapter)
    return DeltaEngineService(publisher=publisher)


def _delta_of(answer):
    assert answer["ok"] is True, answer
    return answer["delta"]


# -- §1.9.1: the intent is frozen before anything is read -------------------


def test_the_intent_is_frozen_and_stored_before_either_end_is_read():
    """The central guarantee of the subsystem, proved from inside the read.

    An intent compiled AFTER the extraction can be compiled to fit it, and
    every evaluation built that way scores full marks. So the adapter counts
    the frozen contracts in the store at the moment it is asked for a snapshot:
    a service that froze the contract afterwards hands it a zero.
    """
    seen = {}

    def _look(revision, scope):
        seen.setdefault("intents", P.store().counts(owner=OWNER)["intents"])

    adapter = _Adapter(findings=(_finding(),), on_snapshot=_look)
    svc = _service(adapter)
    assert P.store().counts(owner=OWNER)["intents"] == 0

    answer = svc.create(_request(intent_text="src/auth.py#consume_state changes"),
                        owner=OWNER)

    assert adapter.snapshots == 2, "both ends were read"
    assert seen["intents"] == 1, (
        "the contract was not in the store when the first end was read")
    delta = _delta_of(answer)
    assert delta["intent_contract_id"] == answer["intent_contract_id"]
    frozen = P.store().get_intent(delta["intent_contract_id"], owner=OWNER)
    assert frozen is not None and frozen.frozen_at


def test_an_intent_contract_that_is_not_yours_is_not_found():
    """Read back from the store rather than trusted from the payload, and
    scoped -- so somebody else's contract is `not_found` and not a comparison
    judged against a request they never made."""
    adapter = _Adapter(findings=(_finding(),))
    svc = _service(adapter)
    frozen = svc.compile_intent(owner=OWNER, domain=DOMAIN,
                                text="src/auth.py#consume_state changes")

    with pytest.raises(DeltaServiceError) as raised:
        svc.create(_request(intent_contract_id=frozen["intent"]["id"]), owner=OTHER)

    assert raised.value.code == "not_found"
    assert raised.value.path == "intent_contract_id"
    assert adapter.snapshots == 0, "nothing was read for a request that was refused"


def test_an_intent_frozen_for_another_domain_is_refused_and_names_both():
    """An intent about one kind of thing cannot judge another, and the refusal
    says which two it was asked to reconcile -- otherwise the caller's only
    move is to guess which of its ids was wrong."""
    adapter = _Adapter(findings=(_finding(),))
    svc = _service(adapter)
    frozen = svc.compile_intent(owner=OWNER, domain="document",
                                text="the citations are preserved")

    with pytest.raises(DeltaServiceError) as raised:
        svc.create(_request(intent_contract_id=frozen["intent"]["id"]), owner=OWNER)

    assert raised.value.code == "invalid_argument"
    assert "document" in raised.value.message and DOMAIN in raised.value.message
    assert adapter.snapshots == 0


def test_a_frozen_contract_and_raw_intent_together_are_refused():
    """Two different asks. Picking one for the caller would silently discard
    the other, and the caller would never learn which half was ignored."""
    adapter = _Adapter(findings=(_finding(),))
    svc = _service(adapter)
    frozen = svc.compile_intent(owner=OWNER, domain=DOMAIN,
                                text="src/auth.py#consume_state changes")

    with pytest.raises(DeltaError) as raised:
        svc.create(_request(intent_contract_id=frozen["intent"]["id"],
                            intent_text="actually, keep it as it was"),
                   owner=OWNER)

    assert raised.value.path.endswith("intent_contract_id")
    assert adapter.snapshots == 0


# This began as an xfail against a real bug: `verdict.assess` had no rule about
# an intent that asked for nothing, so its final `return "matched"` was
# reachable with an EMPTY contract -- and `service._intent_for` states the
# opposite in as many words. FIXED in `verdict.assess`, which now returns
# `partial` when nothing was requested, on the grounds that the comparison
# succeeded and it is the verdict about the intent that is empty. The test
# stays as the regression guard.
def test_a_comparison_with_no_intent_at_all_runs_and_never_answers_matched():
    """"Compare these two and tell me what changed" is a legitimate question.

    It produces a delta -- with an EMPTY contract frozen and stored like any
    other -- and what it cannot produce is `matched`: nothing was requested for
    the result to match, and a `matched` here would be the engine marking an
    errand nobody set as done. Every other reason to withhold the word is
    removed on purpose: both ends readable, every coverage axis measured, one
    observation, no violated invariant.
    """
    adapter = _Adapter(findings=(_finding(),))
    svc = _service(adapter)

    answer = svc.create(_request(), owner=OWNER)
    delta = _delta_of(answer)

    assert delta["assessment"] in ASSESSMENTS
    assert delta["intent_contract_id"], "the empty contract is frozen and stored too"
    assert delta["assertions"], "the comparison still observed what changed"
    assert delta["assessment"] != "matched", (
        "nothing was requested, so there is nothing the result could match")


def test_an_unreadable_end_is_a_stored_inconclusive_delta_that_names_the_side():
    """The failure is the finding. An exception here would lose the other end's
    work and tell the caller nothing about WHICH end failed."""
    source = _tree(SOURCE)
    adapter = _Adapter(findings=(_finding(),), explode_on=source.hash)
    publisher = _Publisher()
    svc = _service(adapter, publisher)

    delta = _delta_of(svc.create(_request(source=source.to_dict()), owner=OWNER))

    assert delta["assessment"] == "inconclusive"
    notes = " ".join(delta["coverage"].get("notes") or [])
    assert "source:" in notes, f"the side that failed is not named: {notes!r}"
    assert "target:" not in notes, "the readable end was blamed too"
    assert delta["coverage"]["source_readable"] is False
    assert delta["coverage"]["target_readable"] is True
    assert adapter.comparisons == 0, "nothing was compared across an unreadable end"
    assert P.store().get_delta(delta["id"], owner=OWNER) is not None, (
        "'we tried and could not see the source' is exactly the record a "
        "caller needs before acting, so it is stored")
    assert "delta_inconclusive" in publisher.names()


# -- §22: the store is the cache, and invalidation is visible ---------------


def test_the_same_comparison_twice_is_served_without_waking_the_adapter():
    """A hit that still ran the extraction is not a hit. The counters are the
    only way to tell the two apart from outside."""
    adapter = _Adapter(findings=(_finding(),))
    svc = _service(adapter)
    body = _request()

    first = svc.create(dict(body), owner=OWNER)
    second = svc.create(dict(body), owner=OWNER)

    assert first["cached"] is False
    assert second["cached"] is True
    assert second["delta"]["id"] == first["delta"]["id"]
    assert adapter.comparisons == 1, "the second comparison re-extracted"
    assert adapter.snapshots == 2, "the second comparison re-read the two ends"


def test_a_new_extractor_version_is_a_visible_invalidation_not_a_silent_miss():
    """Why the fingerprint leaves the extractor versions OUT.

    A key containing them could only be computed after the extraction -- the
    expensive half -- and a parser upgrade would produce a silent second live
    row. Identifying the delta by the QUESTION means the second answer collides
    with the first, and the service supersedes it on purpose, with a reason and
    an event a reader can see.
    """
    adapter = _Adapter(findings=(_finding(),))
    publisher = _Publisher()
    svc = _service(adapter, publisher)
    body = _request()
    first = _delta_of(svc.create(dict(body), owner=OWNER))

    adapter.version = "2"
    second = svc.create(dict(body), owner=OWNER)

    assert second["cached"] is False, "an upgraded parser served yesterday's answer"
    assert second["delta"]["id"] != first["id"]
    assert adapter.comparisons == 2
    announced = publisher.payloads("delta_invalidated")
    assert announced, "the invalidation happened silently"
    assert announced[0]["delta_id"] == first["id"]
    assert "extractor version changed" in announced[0]["reason"]
    assert P.store().counts(owner=OWNER)["superseded_deltas"] == 1
    assert [row["id"] for row in svc.list(owner=OWNER)["deltas"]] == [
        second["delta"]["id"]], "the retired answer is still listed as live"


# -- the switch: comparing costs, reading does not -------------------------


def test_the_switch_off_refuses_every_comparison(monkeypatch):
    """`create`, `run` and `compile_intent` are the three that cost this
    machine a re-read and a re-parse of both revisions."""
    adapter = _Adapter(findings=(_finding(),))
    svc = _service(adapter)
    request_id = svc.create(_request(), owner=OWNER, run=False)["request_id"]
    monkeypatch.setattr(service_mod, "enabled", lambda: False)

    for name, call in (
            ("create", lambda: svc.create(_request(), owner=OWNER)),
            ("run", lambda: svc.run(request_id, owner=OWNER)),
            ("compile_intent", lambda: svc.compile_intent(
                owner=OWNER, domain=DOMAIN, text="src/auth.py changes")),
    ):
        with pytest.raises(DeltaServiceError) as raised:
            call()
        assert raised.value.code == "disabled", f"{name} answered {raised.value.code}"
        assert raised.value.path == "settings.agent_delta_engine"


# This began as an xfail against a real bug: `create` froze and stored the
# contract and the request BEFORE `_run` read the switch, so a refused
# comparison left both rows behind -- and `create(..., run=False)` succeeded
# outright with the engine off, because nothing on that path read the switch at
# all. FIXED: `create` and `run` both read `enabled()` before anything is
# frozen. The test stays as the regression guard, and it is worth keeping
# because the failure it describes is invisible from the HTTP surface: the
# route cuts in first, so only a non-HTTP caller ever saw it.
def test_the_switch_off_leaves_no_contract_and_no_request_behind(monkeypatch):
    """A contract nobody will ever answer is not a cheap row: it is a frozen
    request that reads, six months later, like an errand somebody set."""
    svc = _service(_Adapter(findings=(_finding(),)))
    monkeypatch.setattr(service_mod, "enabled", lambda: False)

    with pytest.raises(DeltaServiceError) as raised:
        svc.create(_request(), owner=OWNER)

    assert raised.value.code == "disabled"
    counts = P.store().counts(owner=OWNER)
    assert counts["intents"] == 0, "a contract was frozen for a comparison that never ran"
    assert counts["requests"] == 0, "a request was stored for a comparison that never ran"


def test_the_switch_off_keeps_every_read_answering(monkeypatch):
    """A delta already stored was a conclusion recorded honestly. Turning the
    engine off is a decision about what the machine may SPEND, never an
    instruction to hide what was concluded."""
    adapter = _Adapter(findings=(_finding(),))
    svc = _service(adapter)
    delta = _delta_of(svc.create(_request(), owner=OWNER))
    monkeypatch.setattr(service_mod, "enabled", lambda: False)

    assert svc.get(delta["id"], owner=OWNER)["delta"]["id"] == delta["id"]
    listed = svc.list(owner=OWNER)
    assert listed["ok"] is True and listed["enabled"] is False
    assert [row["id"] for row in listed["deltas"]] == [delta["id"]]
    assert svc.evidence(delta["id"], owner=OWNER)["ok"] is True
    diagnostics = svc.diagnostics(owner=OWNER)
    assert diagnostics["ok"] is True and diagnostics["enabled"] is False
    assert diagnostics["counts"]["live_deltas"] == 1


# -- one owner cannot read another's comparison ----------------------------


def test_another_owners_delta_is_not_found_and_is_not_listed():
    """404-shaped for both cases: telling somebody that an id exists but is not
    theirs is itself the disclosure."""
    adapter = _Adapter(findings=(_finding(),))
    svc = _service(adapter)
    delta = _delta_of(svc.create(_request(), owner=OWNER))

    with pytest.raises(DeltaServiceError) as raised:
        svc.get(delta["id"], owner=OTHER)
    assert raised.value.code == "not_found"

    with pytest.raises(DeltaServiceError):
        svc.evidence(delta["id"], owner=OTHER)
    assert svc.list(owner=OTHER)["deltas"] == []
    assert [row["id"] for row in svc.list(owner=OWNER)["deltas"]] == [delta["id"]]


def test_an_empty_owner_is_refused_rather_than_read_across_everybody():
    """`persistence._owner_clause` reads an empty owner as the UNSCOPED query,
    which is right for the doctor and a disclosure of every delta on the
    machine if a caller ever passed one."""
    svc = _service(_Adapter(findings=(_finding(),)))
    _delta_of(svc.create(_request(), owner=OWNER))

    with pytest.raises(DeltaServiceError) as raised:
        svc.list(owner="")

    assert raised.value.code == "invalid_argument"
    assert raised.value.path == "owner"


# -- §23: reclassifying changes the meaning, never the observation ---------


def test_a_reclassification_without_a_reason_is_refused():
    """A reinterpretation nobody justified cannot be told from a mistake once
    everyone has forgotten, so the reason is not optional."""
    svc = _service(_Adapter(findings=(_finding(),)))
    delta = _delta_of(svc.create(_request(), owner=OWNER))
    observed = delta["assertions"][0]

    with pytest.raises(DeltaServiceError) as raised:
        svc.reclassify(delta["id"], observed["id"], owner=OWNER,
                       classification_name="regression", actor=OWNER, reason="   ")

    assert raised.value.code == "invalid_argument"
    assert raised.value.path == "reason"
    still = svc.get(delta["id"], owner=OWNER)["delta"]
    assert still["assertions"][0]["classification"] == observed["classification"]


def test_a_reclassification_changes_the_meaning_and_nothing_that_was_observed():
    """A human saying "that was a bug" is a statement about MEANING. If it
    could also move `before` and `after`, the record would stop saying what the
    machine saw, and the next reader could not tell the two apart."""
    svc = _service(_Adapter(findings=(_finding(),)))
    delta = _delta_of(svc.create(_request(), owner=OWNER))
    observed = delta["assertions"][0]
    assert observed["classification"] != "regression"

    answer = svc.reclassify(delta["id"], observed["id"], owner=OWNER,
                            classification_name="regression", actor=OWNER,
                            reason="the doubled token is a bug we shipped")

    revised = next(a for a in answer["delta"]["assertions"]
                   if a["id"] == observed["id"])
    assert revised["classification"] == "regression"
    for field in ("path", "operation", "before", "after", "method", "tier"):
        assert revised[field] == observed[field], f"{field} was rewritten"
    trail = svc.get(answer["delta"]["id"], owner=OWNER)["reclassifications"]
    assert any("doubled token" in str(row.get("reason", "")) for row in trail)
    assert any(str(row.get("actor", "")) == OWNER for row in trail)


# -- what a comparison says out loud ---------------------------------------


def test_every_event_a_comparison_emits_is_one_this_module_declares():
    """A name outside `DELTA_EVENTS` reaches a page and is then refused by the
    envelope an audit replays it through."""
    publisher = _Publisher()
    svc = _service(_Adapter(findings=(_finding(),)), publisher)

    svc.create(_request(intent_text="src/auth.py#consume_state changes"), owner=OWNER)

    emitted = publisher.names()
    assert set(emitted) <= set(DELTA_EVENTS), sorted(set(emitted) - set(DELTA_EVENTS))
    for expected in ("delta_requested", "delta_intent_compiled",
                     "delta_source_resolved", "delta_extraction_completed",
                     "delta_coverage_computed", "delta_completed"):
        assert expected in emitted, f"{expected} was never announced"


def test_a_comparison_with_a_regression_says_so_on_the_stream():
    """The event a consumer waits for. A regression found and never announced
    is a regression nobody acts on."""
    finding = _finding(invariant_refs=(DEFAULT_INVARIANT,))
    publisher = _Publisher()
    svc = _service(_Adapter(findings=(finding,), invariant_results=(_violation(),)),
                   publisher)

    delta = _delta_of(svc.create(_request(), owner=OWNER))

    assert delta["assessment"] == "regressed"
    assert "regression" in {a["classification"] for a in delta["assertions"]}
    detected = publisher.payloads("delta_regression_detected")
    assert detected, "a regression was concluded and the stream never said so"
    assert any(row.get("path") == finding.path for row in detected)
    assert "delta_invariant_checked" in publisher.names()


def test_a_publisher_that_raises_does_not_cost_the_delta():
    """A comparison that succeeded and could not be announced is still a
    comparison that succeeded; raising would mean a page that went away takes
    the result with it."""
    publisher = _Explodes()
    svc = _service(_Adapter(findings=(_finding(),)), publisher)

    delta = _delta_of(svc.create(_request(), owner=OWNER))

    assert P.store().get_delta(delta["id"], owner=OWNER) is not None
    assert "delta_completed" in publisher.names()


# -- the vocabularies the page is served rather than copying ---------------


def test_config_has_no_empty_vocabulary_and_every_word_comes_from_contracts():
    """Compared against the MODULES, never against a list retyped here: a copy
    in the test drifts exactly the way the copy in a front end does."""
    svc = _service(_Adapter())

    config = svc.config()

    expected = {
        "domains": DOMAINS, "assessments": ASSESSMENTS,
        "classifications": CLASSIFICATIONS, "operations": OPERATIONS,
        "severities": SEVERITIES, "confidence": CONFIDENCE,
        "tiers": EXTRACTION_TIERS, "invariant_classes": INVARIANT_CLASSES,
        "invariant_statuses": INVARIANT_STATUSES,
        "coverage_dimensions": COVERAGE_DIMENSIONS,
        "evidence_kinds": EVIDENCE_KINDS, "condition_kinds": CONDITION_KINDS,
        "events": DELTA_EVENTS, "errors": ERRORS,
    }
    for key, vocabulary in expected.items():
        assert config[key] == list(vocabulary), key
    empty = sorted(key for key, value in config.items()
                   if isinstance(value, list) and not value)
    assert not empty, f"a vocabulary served empty is one a page renders blank: {empty}"
    assert config["ok"] is True and config["enabled"] is True


def test_the_profiles_widen_coverage_and_never_lower_the_security_floor():
    """Everything a profile buys is about COVERAGE and budget.
    `invariants.MANDATORY_CLASSES` is what enforces that; `profiles()` only
    describes it, and it has to describe it out of the same tuple."""
    svc = _service(_Adapter())

    profiles = svc.profiles()

    assert [row["id"] for row in profiles["profiles"]] == [
        "literal", "default", "greedy", "maximalist"]
    assert profiles["mandatory_classes"] == list(invariants_mod.MANDATORY_CLASSES)
    assert profiles["mandatory_classes"], (
        "a profile list with no mandatory class is one a cheap profile can trim")
