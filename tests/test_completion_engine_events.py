"""The completion engine's stream: what reaches a consumer, and under what name.

The durable half lives in `test_completion_engine_persistence.py`. This file is
about the live commentary -- and about the one naming rule the whole subsystem
turns on.

The guarantees, one test each:

* **no `stop_reason` can produce both `completion_converged` and
  `completion_budget_exhausted`.** They are two names on purpose: "there was
  nothing left worth doing" and "we ran out" lead to opposite next actions, and
  an engine that could report both for one stop makes the second one worthless
  -- which is exactly how a budget that is too small stays too small forever;
* `COMPLETION_EVENTS` is exactly the `completion_*` block of `EVENT_NAMES`,
  checked by READING `src/contracts/event.py` rather than by repeating the list
  here -- a copy of a list in a test is a second place to forget a name;
* a name this module never declared is published as `completion_error` with the
  requested name inside the payload: never dropped, never raised;
* the cursor resumes exactly where it stopped, and says so when the capacity
  dropped what the caller asked for;
* the frame is an UNNAMED SSE frame with the name inside the JSON;
* a publisher cannot put an event in somebody else's stream;
* a secret does not leave, and the count says how many went.
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

from src.completion_engine import events as E
from src.completion_engine.contracts import HONEST_STOPS, STOP_REASONS
from src.contracts.event import EVENT_NAMES

OWNER = "alice"


@pytest.fixture(autouse=True)
def clean_streams():
    """No test inherits another's streams."""
    E.reset_streams()
    try:
        yield
    finally:
        E.reset_streams()


def stream(owner: str = OWNER, **kwargs) -> E.CompletionEventStream:
    return E.CompletionEventStream(owner, **kwargs)


# ===========================================================================
# the rule: convergence and exhaustion are never the same stop
# ===========================================================================


def test_no_stop_reason_produces_both_converged_and_budget_exhausted():
    """The reason there are two names where another subsystem would have put
    one. `stop_event` returns a single name, so the interesting claim is not
    that one call cannot return two -- it is that the two names carve the stop
    reasons into disjoint sets, and that nothing widens one of them into the
    other later.
    """
    converged, exhausted = set(), set()
    for reason in STOP_REASONS:
        name = E.stop_event(reason)
        if name == "completion_converged":
            converged.add(reason)
        elif name == "completion_budget_exhausted":
            exhausted.add(reason)

    assert converged and exhausted, "both names have to be reachable at all"
    assert not (converged & exhausted), (
        f"{sorted(converged & exhausted)} would be reported as convergence AND "
        f"as exhaustion, which is the one confusion this module exists to "
        f"prevent")
    assert exhausted == {"budget"}, (
        "only running out is running out; anything else reported as exhaustion "
        "would send a reader to raise a budget that was never the problem")
    assert converged == set(HONEST_STOPS), (
        "the frozen contract's own answer to 'was there nothing left worth "
        "doing' is `HONEST_STOPS`, and this module must not invent a second one")
    assert "budget" not in HONEST_STOPS, (
        "the disjointness above is only structural while this holds -- it is "
        "also what `CompletionDecision.parse` leans on when it refuses a "
        "`converged` beside an exhausted line")


def test_every_stop_reason_maps_to_a_name_this_module_declares():
    """A stop that had no event name would be a run that ended and said
    nothing, which is worse than either of the two sentences being wrong."""
    for reason in STOP_REASONS:
        assert E.stop_event(reason) in E.COMPLETION_EVENTS, reason
    assert E.stop_event("converged") == "completion_converged"
    assert E.stop_event("budget") == "completion_budget_exhausted"
    assert E.stop_event("risk") == "completion_decision_recorded", (
        "an interruption is reported as one, not as either of the two")


def test_an_unknown_stop_reason_is_an_error_rather_than_a_guess():
    """The two names lead to opposite actions, so guessing between them for a
    word nobody declared has the worst possible payoff: an unrecognised stop
    reported as convergence is a run nobody ever raises the budget for."""
    for bad in ("", None, "ran_out", "converged_ish"):
        assert E.stop_event(bad) == "completion_error"


def test_publishing_a_stop_emits_exactly_one_of_the_two_names():
    """The rule held where a caller can actually break it."""
    events = stream()
    events.publish_stop("budget", run_id="run_1", decision_id="decision_1")
    events.publish_stop("converged", run_id="run_2", decision_id="decision_2")

    published, _, _ = events.since(0)
    names = [event.name for event in published]
    assert names == ["completion_budget_exhausted", "completion_converged"]
    assert published[0].payload["stop_reason"] == "budget", (
        "the reason travels in the payload, so a consumer never has to infer "
        "it back out of the name")
    # And the same claim walked over every stop reason there is, on the path a
    # caller actually takes: one stop, one frame, never both of the two names.
    for reason in STOP_REASONS:
        probe = stream(f"probe_{reason}")
        probe.publish_stop(reason, run_id="run_3")
        emitted = {event.name for event in probe.since(0)[0]}
        assert len(emitted) == 1, f"{reason} published {sorted(emitted)}"
        assert not {"completion_converged",
                    "completion_budget_exhausted"} <= emitted, (
            f"{reason} reported convergence AND exhaustion for one stop")


# ===========================================================================
# the vocabulary is closed, and it is closed in one place
# ===========================================================================


def test_completion_events_is_exactly_the_completion_block_of_the_envelope():
    """Read from `EVENT_NAMES`, not copied into this file.

    Both directions matter and they fail differently. A name here and not there
    reaches a page and is then refused by the envelope an audit replays it
    through. A name there and not here is a name this module can never publish,
    which is a stop that happens and is never reported.
    """
    declared = set(E.COMPLETION_EVENTS)
    in_envelope = {name for name in EVENT_NAMES if name.startswith("completion_")}

    assert declared <= set(EVENT_NAMES), (
        f"{sorted(declared - set(EVENT_NAMES))} would reach a page and then be "
        f"refused by the envelope")
    assert declared == in_envelope, (
        f"the two lists have drifted: {sorted(declared ^ in_envelope)}")
    assert declared < set(EVENT_NAMES), "a strict subset, not the whole vocabulary"
    assert len(E.COMPLETION_EVENTS) == len(declared), "no name is declared twice"
    assert len(declared) == 14, (
        "the envelope declares fourteen completion names; a fifteenth that "
        "arrived without this module noticing is a stop nothing can report")


def test_an_unknown_name_is_published_as_completion_error_and_is_never_lost():
    """A run must not die of a typo in a progress line, and the typo must still
    be findable afterwards."""
    events = stream()
    published = events.publish("completion_finished", run_id="run_1")

    assert published.name == "completion_error"
    assert published.payload["unknown_event"] == "completion_finished"
    assert published.payload["run_id"] == "run_1"
    assert published.seq == 1, "it consumed a sequence number like any other"
    assert events.since(0)[0][0].id == published.id


def test_publishing_never_raises_whatever_the_name_is():
    events = stream()
    for name in ("", "   ", None, "completion.converged", "delta_completed"):
        assert events.publish(name, run_id="r").name == "completion_error"
    assert events.stats()["last_seq"] == 5, "nothing was dropped on the way"


# ===========================================================================
# resuming, and admitting a hole
# ===========================================================================


def test_the_cursor_resumes_exactly_where_it_stopped():
    events = stream()
    for index in range(3):
        events.publish("completion_candidate_discovered", run_id="r",
                       candidate_id=f"improvement_{index}")

    first, cursor, gap = events.since(0, limit=2)
    assert [e.seq for e in first] == [1, 2]
    assert (cursor, gap) == (2, False), (
        "the cursor is the last event of THIS page, not the head of the stream")

    second, cursor, gap = events.since(cursor)
    assert [e.seq for e in second] == [3]
    assert (cursor, gap) == (3, False)

    tail, cursor, gap = events.since(cursor)
    assert (tail, cursor, gap) == ([], 3, False), (
        "an empty page returns the cursor unchanged; resetting it is how a "
        "poller silently starts replaying history")


def test_the_cursor_announces_the_hole_the_capacity_made():
    """A silent hole costs a rejected candidate nobody knows was refused, which
    on this stream is the same as a budget nobody knows to raise."""
    events = stream(capacity=3)
    for index in range(5):
        events.publish("completion_candidate_rejected", run_id="r",
                       candidate_id=f"improvement_{index}")

    resumed, cursor, gap = events.since(0)
    assert [e.seq for e in resumed] == [3, 4, 5]
    assert gap is True, "events 1 and 2 are gone and the caller has to be told"
    assert cursor == 5
    assert events.stats()["dropped"] == 2

    kept, _, gap = events.since(2)
    assert [e.seq for e in kept] == [3, 4, 5]
    assert gap is False, "nothing the caller asked for was missing this time"


def test_a_consumer_ahead_of_the_stream_gets_nothing_rather_than_a_rewind():
    events = stream()
    events.publish("completion_contract_created", run_id="r")
    assert events.since(99) == ([], 99, False)


# ===========================================================================
# the envelope
# ===========================================================================


def test_every_event_carries_the_common_payload_even_when_nobody_supplied_it():
    """Absent and empty are two different facts, and a consumer should only ever
    have to handle one of them."""
    published = stream().publish("completion_layer_opened", run_id="run_1",
                                 layer="bonus")
    for key in E.COMMON_PAYLOAD_KEYS:
        assert key in published.payload, f"{key} is missing from the payload"
    assert published.payload["mode"] == ""
    assert published.payload["candidate_id"] == ""
    assert published.payload["at"] == published.created_at
    assert published.payload["layer"] == "bonus"


def test_a_publisher_cannot_put_an_event_in_somebody_elses_stream():
    """The owner comes from the stream, never from the caller: a publisher that
    could name someone else's owner could file an event under it."""
    events = stream("alice")
    published = events.publish("completion_decision_recorded", run_id="r",
                               owner="bob")

    assert published.owner == "alice"
    assert published.payload["owner"] == "alice"


def test_the_frame_is_an_unnamed_sse_frame_with_the_name_inside_the_json():
    """A named frame never reaches `onmessage`, and a page written against the
    unnamed dispatch stream goes deaf on a named one without erroring."""
    frame = stream().publish("completion_converged", run_id="run_1").sse()

    assert frame.startswith("data: ") and frame.endswith("\n\n")
    assert "event:" not in frame
    body = json.loads(frame[len("data: "):].strip())
    assert body["name"] == "completion_converged"
    assert body["run_id"] == "run_1"


def test_a_payload_that_will_not_serialise_costs_its_own_frame_only():
    class Opaque:
        def __repr__(self):
            return "<opaque>"

    events = stream()
    published = events.publish("completion_frontier_recomputed", run_id="r",
                               estimator=Opaque())
    frame = published.sse()

    assert "opaque" in frame, "`default=str` keeps the frame instead of losing it"
    assert events.publish("completion_converged", run_id="r").seq == 2


def test_a_secret_in_a_payload_does_not_leave_and_the_count_says_so():
    """A completion payload quotes a candidate's `detail` and names the paths it
    would touch, so this is not theoretical here."""
    published = stream().publish("completion_candidate_discovered", run_id="r",
                                 api_key="sk-live-01234567890abcdef")

    assert "sk-live-01234567890abcdef" not in json.dumps(
        published.to_dict(), default=str)
    assert published.payload["redactions"] >= 1


# ===========================================================================
# waiting
# ===========================================================================


def test_wait_wakes_on_a_publish_from_another_thread():
    """Discovery and verification run on workers while a route holds the long
    poll, so the cross-thread wake is the normal path here. A lost wake would
    look exactly like a run that has hung."""
    events = stream()

    async def scenario() -> bool:
        threading.Timer(0.02, lambda: events.publish("completion_converged",
                                                     run_id="r")).start()
        return await events.wait(0, timeout=5.0)

    assert asyncio.run(scenario()) is True
    assert events.since(0)[0][0].name == "completion_converged"


def test_wait_answers_false_on_the_deadline_and_that_is_not_an_error():
    events = stream()

    async def scenario() -> bool:
        return await events.wait(0, timeout=0.05)

    assert asyncio.run(scenario()) is False


def test_a_closed_stream_answers_immediately_instead_of_holding_the_poll():
    events = stream()
    events.close()

    async def scenario() -> bool:
        return await events.wait(0, timeout=30.0)

    assert asyncio.run(scenario()) is True


def test_an_event_after_close_is_kept_numbered_and_marked_late():
    """What arrived after a shutdown is part of the record of what happened, and
    is acted on by nothing."""
    events = stream()
    events.publish("completion_contract_created", run_id="r")
    events.close()
    late = events.publish("completion_decision_recorded", run_id="r")

    assert late.seq == 2
    assert late.payload["late"] is True
    assert events.stats()["late"] == 1


# ===========================================================================
# one stream per owner
# ===========================================================================


def test_a_stream_is_made_once_and_kept_so_its_numbers_keep_meaning_something():
    first = E.stream_for(OWNER)
    first.publish("completion_contract_created", run_id="r")
    assert E.stream_for(OWNER) is first
    assert E.stream_for("bob") is not first
    assert set(E.stream_names()) == {OWNER, "bob"}

    E.close_stream(OWNER)
    assert E.stream_for(OWNER) is first, (
        "a closed stream stays in the registry; recreating it would restart the "
        "sequence at 1 and hand a reconnecting client numbers it had already used")
    assert E.stream_for(OWNER).last_seq() == 1

    E.reset_streams()
    assert E.stream_names() == ()
    assert E.stream_for(OWNER) is not first


def test_an_empty_owner_is_a_real_key_and_not_a_missing_one():
    """This install runs single-user by default and answers `""` there, so it
    gets its own stream rather than a placeholder shared with every owner that
    failed to resolve."""
    anonymous = E.stream_for("")
    assert E.stream_for(None) is anonymous
    assert E.stream_for(OWNER) is not anonymous
    assert "" in E.stream_names()
