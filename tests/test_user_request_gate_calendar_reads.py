"""«Mira mi calendario de esta semana y dime qué día tengo más libre.» reads
the user's own calendar without a card.

Seen live in the daily battery (01-10-2026): `list_calendars` ran, its result
armed the external-context gate, and `list_events` stopped at «Allow this task
to continue?». Only the two read actions pass, and only when the message is
about the calendar; update and delete keep the card.
"""
import json

from src.user_request_gate import allows

ASK = "Mira mi calendario de esta semana y dime qué día tengo más libre."


def _call(action, **extra):
    return json.dumps({"action": action, **extra})


def test_reads_pass_when_the_message_is_about_the_calendar():
    assert allows("manage_calendar", _call("list_calendars"), ASK) is True
    assert allows("manage_calendar", _call("list_events", start="2026-09-28T00:00:00",
                                           end="2026-10-05T00:00:00"), ASK) is True
    assert allows("manage_calendar", _call("list_events"), "What meetings do I have tomorrow?") is True


def test_writes_and_unrelated_messages_keep_the_card():
    assert allows("manage_calendar", _call("delete_event", event_id="x"), ASK) is False
    assert allows("manage_calendar", _call("update_event", event_id="x", summary="y"), ASK) is False
    assert allows("manage_calendar", _call("list_events"), "Resume este artículo, por favor.") is False
