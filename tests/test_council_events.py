"""Tests for src/council/events.py — resuming, and what a frame may carry.

The plan's section 20 asks for one thing of this module by name: reconnecting
must not duplicate events. The rest of what is tested here is the same
sentence read carefully: a hole must be announced rather than hidden, a
sequence must survive a reconnection, the common payload must be present even
when nobody filled it, no secret may travel, and a straggler that arrives after
the room closed is kept and marked instead of acted on.

No sleeps longer than a scheduling tick: `wait()` is woken by a publish, which
is the point of it.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from src.council.events import (
    COMMON_PAYLOAD_KEYS,
    COUNCIL_EVENTS,
    CouncilEvent,
    CouncilEventStream,
    close_stream,
    reset_streams,
    stream_for,
)

DEADLINE = 5.0
SECRET = "sk-live-9f2c4d8e7a1b6c5d"


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_streams()
    yield
    reset_streams()


@pytest.fixture
def stream():
    return CouncilEventStream("council_test", capacity=100)


# --- rule 1: reconnecting duplicates nothing and skips nothing -------------

def test_since_returns_strictly_what_follows_the_cursor(stream):
    for i in range(5):
        stream.publish("council_message", index=i)

    assert [e.seq for e in stream.since(0)] == [1, 2, 3, 4, 5]
    assert [e.seq for e in stream.since(2)] == [3, 4, 5]
    assert [e.seq for e in stream.since(5)] == []
    # A consumer somehow ahead of the stream is not rewound.
    assert [e.seq for e in stream.since(99)] == []


def test_a_reconnect_repeats_nothing_and_loses_nothing(stream):
    for i in range(5):
        stream.publish("council_message", index=i)

    first_leg = stream.since(0, limit=2)
    cursor = first_leg[-1].seq
    second_leg = stream.since(cursor)          # the client comes back

    seen = [e.seq for e in first_leg] + [e.seq for e in second_leg]
    assert seen == [1, 2, 3, 4, 5]
    assert len(seen) == len(set(seen)), "no event was delivered twice"
    ids = [e.id for e in first_leg + second_leg]
    assert len(ids) == len(set(ids))


def test_seq_is_monotonic_per_session_and_survives_a_reconnect():
    live = stream_for("council_abc")
    live.publish("council_activity_started")
    live.publish("council_message", text="one")
    assert live.last_seq() == 2

    again = stream_for("council_abc")           # the client reconnects
    assert again is live
    again.publish("council_message", text="two")
    assert again.last_seq() == 3
    assert [e.seq for e in again.since(2)] == [3]


def test_an_overflowed_buffer_says_so_instead_of_leaving_a_hole():
    small = CouncilEventStream("council_small", capacity=3)
    for i in range(5):
        small.publish("council_message", index=i)

    batch = small.since(0)
    gap, rest = batch[0], batch[1:]
    assert gap.name == "council_error"
    assert gap.payload["gap"] is True
    assert gap.payload["missed"] == 2
    assert (gap.payload["from_seq"], gap.payload["to_seq"]) == (1, 2)
    assert [e.seq for e in rest] == [3, 4, 5]
    # The marker is built, not published: it burns no sequence number.
    assert small.last_seq() == 5
    # And a consumer already inside the surviving range is told nothing.
    assert [e.seq for e in small.since(2)] == [3, 4, 5]
    assert all(not e.payload.get("gap") for e in small.since(2))


def test_since_clamps_its_limit(stream):
    for i in range(10):
        stream.publish("council_message", index=i)
    assert len(stream.since(0, limit=3)) == 3
    assert len(stream.since(0, limit=0)) == 1
    assert len(stream.since(0, limit=10_000)) == 10


# --- rule 3: the common payload is present even when nobody filled it ------

def test_the_common_payload_is_present_as_empty_never_absent(stream):
    event = stream.publish("council_message")

    for key in COMMON_PAYLOAD_KEYS:
        assert key in event.payload, f"{key} must be present, not absent"

    assert event.payload["owner"] == ""
    assert event.payload["project_id"] == ""
    assert event.payload["council_id"] == ""
    assert event.payload["activity_id"] == ""
    assert event.payload["run_id"] == ""
    assert event.payload["actor_id"] == ""
    assert event.payload["causation_id"] == ""
    assert event.payload["correlation_id"] == ""
    # The three the stream owns are filled in, not blank.
    assert event.payload["event_id"] == event.id
    assert event.payload["session_id"] == "council_test"
    assert event.payload["at"] == event.created_at


def test_a_supplied_common_payload_travels_on_the_event(stream):
    event = stream.publish(
        "council_decision_recorded",
        owner="luis", project_id="proj_1", council_id="council_test",
        activity_id="act_7", run_id="run_3", actor_id="p_claude",
        causation_id="cev_before", correlation_id="turn_9",
    )
    assert event.activity_id == "act_7"
    assert event.causation_id == "cev_before"
    assert event.correlation_id == "turn_9"
    assert event.payload["owner"] == "luis"
    assert event.payload["run_id"] == "run_3"
    # The session is the stream's, never the publisher's claim about it.
    assert event.payload["session_id"] == "council_test"


def test_the_publisher_may_not_rename_the_session(stream):
    event = stream.publish("council_message", session_id="somebody_elses_room")
    assert event.session_id == "council_test"
    assert event.payload["session_id"] == "council_test"


# --- rule 4: no secret travels in an event ---------------------------------

def test_a_token_in_a_payload_never_leaves(stream):
    event = stream.publish(
        "council_message",
        token=SECRET,
        headers={"authorization": f"Bearer {SECRET}"},
        text="the plain words survive",
    )
    assert event.payload["token"] == "<redacted>"
    assert event.payload["headers"]["authorization"] == "<redacted>"
    assert event.payload["text"] == "the plain words survive"
    assert event.payload["redactions"] == 2, "the count says how many went"
    assert SECRET not in event.sse()
    assert SECRET not in json.dumps(event.to_dict())


# --- rule 5: a late event is kept, marked, and moves nothing ---------------

def test_an_event_after_close_is_kept_and_marked_late(stream):
    stream.publish("council_message", text="during")
    stream.publish("council_activity_completed")
    stream.close()

    late = stream.publish("council_message", text="the straggler")
    assert late.payload["late"] is True
    assert late.seq == 3, "it is still numbered; the audit stays honest"
    assert stream.closed is True, "a straggler reopens nothing"
    assert [e.seq for e in stream.since(0)] == [1, 2, 3]
    assert stream.since(0)[0].payload["late"] is False
    assert stream.stats()["late"] == 1


async def test_a_late_event_wakes_nobody():
    live = CouncilEventStream("council_closing")
    live.publish("council_message", text="during")
    live.close()
    # A closed stream answers at once instead of holding the page for 25s.
    started = time.monotonic()
    tail = await live.wait(1, timeout_s=DEADLINE)
    assert tail == []
    assert time.monotonic() - started < 1.0
    live.publish("council_message", text="after")
    assert live.since(1)[0].payload["late"] is True


# --- rule 6: an unnamed frame with the name inside -------------------------

def test_sse_is_an_unnamed_frame_carrying_the_name_in_the_json(stream):
    event = stream.publish("council_turn_state", state="running")
    frame = event.sse()

    assert frame.startswith("data: ")
    assert frame.endswith("\n\n")
    assert not frame.startswith("event:")
    assert "\nevent:" not in frame

    body = json.loads(frame[len("data: "):].strip())
    assert body["name"] == "council_turn_state"
    assert body["seq"] == event.seq
    assert body["session_id"] == "council_test"
    assert body["payload"]["state"] == "running"


def test_to_dict_carries_every_field(stream):
    event = stream.publish("council_usage", tokens=120)
    assert set(event.to_dict()) == {
        "id", "seq", "name", "session_id", "activity_id", "payload",
        "created_at", "causation_id", "correlation_id",
    }
    assert isinstance(event, CouncilEvent)


# --- rule 7: wait() is a long poll, not a spin -----------------------------

async def test_wait_returns_as_soon_as_something_is_published():
    live = CouncilEventStream("council_live")
    waiting = asyncio.create_task(live.wait(0, timeout_s=DEADLINE))
    await asyncio.sleep(0.05)
    assert not waiting.done(), "nothing published yet, so nothing to answer"

    started = time.monotonic()
    live.publish("council_message", text="here")
    got = await asyncio.wait_for(waiting, timeout=DEADLINE)
    assert [e.seq for e in got] == [1]
    assert time.monotonic() - started < 1.0, "woken by the publish, not by a tick"
    assert live.stats()["waiters"] == 0, "the waiter was removed on the way out"


async def test_wait_answers_immediately_when_there_is_already_something(stream):
    stream.publish("council_message", text="already here")
    got = await stream.wait(0, timeout_s=DEADLINE)
    assert [e.seq for e in got] == [1]


async def test_wait_times_out_with_an_empty_list_not_an_error():
    live = CouncilEventStream("council_quiet")
    assert await live.wait(0, timeout_s=0.05) == []
    assert live.stats()["waiters"] == 0


async def test_two_waiters_are_both_woken():
    live = CouncilEventStream("council_two")
    first = asyncio.create_task(live.wait(0, timeout_s=DEADLINE))
    second = asyncio.create_task(live.wait(0, timeout_s=DEADLINE))
    await asyncio.sleep(0.05)
    live.publish("council_message", text="broadcast")
    both = await asyncio.wait_for(asyncio.gather(first, second), timeout=DEADLINE)
    assert [[e.seq for e in leg] for leg in both] == [[1], [1]]


# --- names, and the registry -----------------------------------------------

def test_an_undeclared_name_becomes_council_error_instead_of_raising(stream):
    event = stream.publish("council_invented_name", detail="whatever")
    assert event.name == "council_error"
    assert event.payload["unknown_event"] == "council_invented_name"
    assert event.payload["detail"] == "whatever"
    assert event.name in COUNCIL_EVENTS


def test_every_declared_name_publishes_under_its_own_name(stream):
    for name in COUNCIL_EVENTS:
        assert stream.publish(name).name == name


def test_stream_for_is_one_per_session_and_close_keeps_the_numbering():
    live = stream_for("council_abc")
    live.publish("council_activity_started")
    close_stream("council_abc")
    assert live.closed is True
    # Still the same stream, so a client reconnecting after the room ended
    # resumes from a number that still means what it meant.
    assert stream_for("council_abc") is live
    assert stream_for("council_abc").last_seq() == 1


def test_reset_streams_forgets_and_closes_them():
    live = stream_for("council_abc")
    reset_streams()
    assert live.closed is True
    assert stream_for("council_abc") is not live
