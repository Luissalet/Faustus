"""What waits for the person in the Agora, told to their phone (src/agora_watch.py): each mention and each question
once, nothing on the first look, nothing old, and a tap that opens the conversation."""
import json

import pytest

from src import agora_watch, push


NOW = 1_800_000_000.0


def inbox(*, pending=(), escalated=()):
    return {"ok": True, "pending": list(pending), "escalated": list(escalated), "reviews": []}


def mention(mid, *, space="atlas", thread=7, age=60, body="@luis mira esto", author="codex-docs", title="general"):
    return {"id": mid, "space_id": space, "thread_id": thread, "author": author, "body": body, "thread_title": title,
            "created": NOW - age}


@pytest.fixture
def watch(monkeypatch, tmp_path):
    state = {"inbox": inbox(), "told": []}
    monkeypatch.setattr(agora_watch, "STATE_FILE", str(tmp_path / "agora_watch.json"))
    monkeypatch.setattr(agora_watch, "_ask", lambda: state["inbox"])
    monkeypatch.setattr(agora_watch, "_owners", lambda: ["admin"])
    from src import notifications
    monkeypatch.setattr(notifications, "emit", lambda kind, **kw: state["told"].append({"kind": kind, **kw}))
    return state


def test_the_first_look_takes_note_and_tells_nothing(watch):
    watch["inbox"] = inbox(pending=[mention(1), mention(2)])
    assert agora_watch.look(NOW) == (0, 2)
    assert watch["told"] == []
    assert json.loads(open(agora_watch.STATE_FILE, encoding="utf-8").read())["seen"] == ["agora:m:atlas:1", "agora:m:atlas:2"]


def test_a_new_mention_and_a_new_question_are_told_once_with_their_place(watch):
    agora_watch.look(NOW)                                      # nothing there: from now on, what comes is news
    watch["inbox"] = inbox(pending=[mention(5, body="@luis   **mira**\n[esto](http://x), token `sk_live_ABCDEFGHIJKLMNOPQRSTUVWX`")],
                           escalated=[{"id": 9, "space_id": "vulcan", "title": "¿Publicamos ya?", "question": "¿Sí o no?",
                                       "escalated_by": "claude", "updated": NOW - 30}])
    assert agora_watch.look(NOW) == (2, 2)
    said, question = watch["told"]                             # as they happened: the mention came first
    assert question["kind"] == "agora_question" and question["owner"] == "admin"
    assert question["title"] == "Ágora · claude · ¿Publicamos ya?" and question["body"] == "¿Sí o no?"
    assert question["data"]["url"] == "/agora?space_id=vulcan&c=t%3A9" and question["data"]["thread_id"] == 9
    assert said["kind"] == "agora_mention" and said["title"] == "Ágora · codex-docs · general"
    assert said["body"] == "@luis mira esto, token [redacted]"                 # one line, no marks, nothing shaped like a secret
    assert said["data"] == {"space_id": "atlas", "thread_id": 7, "message_id": 5, "url": "/agora?space_id=atlas&c=t%3A7"}

    assert agora_watch.look(NOW + 60) == (0, 2)                # still waiting, already told
    assert len(watch["told"]) == 2

    # the message that carries a question also names the person: one notification, the question
    watch["inbox"] = inbox(pending=[mention(11, space="vulcan", thread=20, body="@luis ¿sí o no?")],
                           escalated=[{"id": 20, "space_id": "vulcan", "title": "Otra", "question": "¿sí o no?", "updated": NOW}])
    assert agora_watch.look(NOW + 120) == (1, 1)
    assert watch["told"][-1]["kind"] == "agora_question" and watch["told"][-1]["title"] == "Ágora · Otra"


def test_after_a_long_silence_the_latest_are_told_and_the_rest_in_one_line(watch):
    agora_watch.look(NOW)
    watch["inbox"] = inbox(pending=[mention(100 + n, age=600 - n, author="codex-%d" % (n % 2)) for n in range(8)])
    assert agora_watch.look(NOW) == (8, 8)
    told = watch["told"]
    assert [t["data"].get("message_id") for t in told[:3]] == [105, 106, 107]          # the latest three, as they happened
    assert told[3]["title"] == "Ágora · +5" and told[3]["body"] == "codex-0, codex-1"
    assert told[3]["data"] == {"url": "/agora?c=v%3Ainbox", "more": 5} and len(told) == 4
    assert agora_watch.look(NOW + 60) == (0, 8)                                         # none of them again


def test_a_question_asked_again_in_other_words_is_told_again(watch):
    agora_watch.look(NOW)
    ask = lambda text: inbox(escalated=[{"id": 9, "space_id": "vulcan", "title": "Publicar", "question": text, "updated": NOW}])
    watch["inbox"] = ask("¿Publicamos ya?")
    assert agora_watch.look(NOW)[0] == 1 and agora_watch.look(NOW)[0] == 0
    watch["inbox"] = ask("Con la importación hecha, ¿publicamos ahora?")
    assert agora_watch.look(NOW)[0] == 1
    assert [t["body"] for t in watch["told"]] == ["¿Publicamos ya?", "Con la importación hecha, ¿publicamos ahora?"]


def test_what_is_older_than_a_day_is_history(watch):
    agora_watch.look(NOW)
    watch["inbox"] = inbox(pending=[mention(3, age=90_000), mention(4, age=10)])
    assert agora_watch.look(NOW) == (1, 2)
    assert [t["data"]["message_id"] for t in watch["told"]] == [4]
    assert agora_watch.look(NOW) == (0, 2)                     # the old one is not reconsidered


def test_a_hub_that_is_off_is_asked_again_later_and_every_device_owner_is_told(watch, monkeypatch):
    agora_watch.look(NOW)
    watch["inbox"] = None
    assert agora_watch.look(NOW) == (0, -1) and watch["told"] == []
    monkeypatch.setattr(agora_watch, "_owners", lambda: ["admin", "ana"])
    watch["inbox"] = inbox(pending=[mention(8)])
    assert agora_watch.look(NOW) == (1, 1)
    assert [t["owner"] for t in watch["told"]] == ["admin", "ana"]
    monkeypatch.setattr(agora_watch, "_owners", lambda: [""])
    watch["inbox"] = inbox(pending=[mention(8), mention(10)])
    agora_watch.look(NOW)
    assert watch["told"][-1]["owner"] is None                  # nobody subscribed: the bus keeps it, ownerless


def test_owners_are_those_whose_devices_asked_for_notifications(monkeypatch):
    monkeypatch.setattr(push, "_load_subscriptions", lambda: [{"owner": "admin"}, {"owner": "admin"}, {"owner": "ana"}, "junk"])
    assert agora_watch._owners() == ["admin", "ana"]
    monkeypatch.setattr(push, "_load_subscriptions", lambda: [])
    assert agora_watch._owners() == [""]


def test_a_tap_on_the_push_opens_the_conversation():
    event = {"id": 4, "kind": "agora_mention", "title": "Ágora · codex-docs · general", "body": "mira",
             "data": {"url": "/agora?space_id=atlas&c=t%3A7"}}
    assert push.event_to_push_payload(event)["url"] == "/agora?space_id=atlas&c=t%3A7"
    assert push.event_to_push_payload({"kind": "agora_question", "data": {"url": "https://elsewhere.example/x"}})["url"] == "/agora"
    assert push.event_to_push_payload({"kind": "agora_mention"})["url"] == "/agora"


def test_the_question_asked_of_the_hub_is_the_persons_inbox_as_a_peek(monkeypatch):
    asked = {}

    def fake(method, path, *, query=None, timeout=30.0, **_):
        asked.update(method=method, path=path, query=query)
        return 200, {"ok": True, "pending": [], "escalated": []}, "application/json"

    from routes import agora_routes
    monkeypatch.setattr(agora_routes, "hub_request", fake)
    assert agora_watch._ask() == {"ok": True, "pending": [], "escalated": []}
    assert asked["path"] == "/api/agora/inbox" and asked["query"]["agent"] == "luis" and asked["query"]["peek"] == "1"
    monkeypatch.setattr(agora_routes, "hub_request", lambda *a, **k: (None, None, ""))
    assert agora_watch._ask() is None
