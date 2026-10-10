"""#268: the end-of-turn receipt `faustus.turn.completed` on the family Hub's bus."""
from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from src import turn_receipt

PROMPT = "ZZ-PROMPT-SECRET please summarise the contract"
REPLY = "ZZ-REPLY-SECRET the contract says 42"
THINKING = "ZZ-THINKING-SECRET let me think"


class _Session:
    def __init__(self, model="selected-model", owner="owner-1", name="ZZ-TITLE-SECRET"):
        self.model = model
        self.owner = owner
        self.name = name
        self.history = []

    def add_message(self, message):
        message.metadata = message.metadata if message.metadata is not None else {}
        message.metadata["_db_id"] = 4242
        self.history.append(message)

    def get_context_messages(self):
        return [{"role": "user", "content": PROMPT}]


def _metrics():
    return {
        "model": "fake-coder",
        "input_tokens": 120,
        "output_tokens": 30,
        "response_time": 1.234,
        "agent_rounds": 3,
        "tool_calls": 2,
        "thinking": THINKING,
        "tool_events": [{"name": "read_file", "output": "ZZ-FILE-CONTENT"}],
        "harness": {"project_id": "proj-7", "round_count": 3, "tool_calls": 2},
        "endpoint_id": "ep-1",
        "api_key": "ZZ-CREDENTIAL",
    }


@pytest.fixture
def sync_spawn(monkeypatch):
    """Run the background sender inline and record what reaches the wire."""
    sent = []
    monkeypatch.setattr(turn_receipt, "_spawn", lambda fn, *a: fn(*a))
    monkeypatch.setattr(turn_receipt, "_send", lambda payload, owner=None: sent.append((payload, owner)) or True)
    return sent


# ── payload ──────────────────────────────────────────────────────────────

def test_payload_has_only_the_allowed_keys_and_no_content():
    payload = turn_receipt.build_payload("sess-1", 4242, _metrics(), now=1_800_000_000.5)
    assert set(payload) <= set(turn_receipt.ALLOWED_KEYS)
    assert payload == {
        "id": "sess-1:4242",
        "session_id": "sess-1",
        "ts": 1_800_000_000.5,
        "status": "ok",
        "rounds": 3,
        "tool_calls": 2,
        "input_tokens": 120,
        "output_tokens": 30,
        "duration_s": 1.23,
        "model": "fake-coder",
        "project_id": "proj-7",
    }
    blob = json.dumps(payload)
    for secret in (PROMPT, REPLY, THINKING, "ZZ-FILE-CONTENT", "ZZ-CREDENTIAL", "ZZ-TITLE-SECRET"):
        assert secret not in blob


def test_unknown_counters_are_omitted_not_invented():
    payload = turn_receipt.build_payload("s", 1, {"model": "m"}, now=10.0)
    assert set(payload) == {"id", "session_id", "ts", "status", "model"}
    assert payload["status"] == "ok"


def test_rounds_and_tool_calls_fall_back_to_the_harness_summary_and_tool_events():
    payload = turn_receipt.build_payload(
        "s", 1, {"harness": {"round_count": 4}, "tool_events": [{}, {}, {}]}, now=1.0)
    assert payload["rounds"] == 4
    assert payload["tool_calls"] == 3


def test_junk_values_are_dropped():
    payload = turn_receipt.build_payload(
        "s", 1, {"input_tokens": "lots", "output_tokens": -4, "response_time": None,
                 "model": {"x": 1}, "agent_rounds": True}, now=1.0)
    assert set(payload) == {"id", "session_id", "ts", "status"}


@pytest.mark.parametrize("metadata, expected", [
    ({}, "ok"),
    (None, "ok"),
    ({"failed": True, "failure": {"status": 500}}, "error"),
    ({"failure": {"status": 502, "message": "boom"}}, "error"),
    ({"stopped": True}, "cancelled"),
    ({"stopped": True, "failed": True}, "cancelled"),
])
def test_status_mapping(metadata, expected):
    assert turn_receipt.status_of(metadata) == expected
    assert turn_receipt.build_payload("s", 1, metadata, now=1.0)["status"] == expected


def test_explicit_status_wins_and_bad_values_fall_back():
    assert turn_receipt.build_payload("s", 1, {}, status="cancelled", now=1.0)["status"] == "cancelled"
    assert turn_receipt.build_payload("s", 1, {"failed": True}, status="weird", now=1.0)["status"] == "error"


def test_id_falls_back_to_the_run_id_when_there_is_no_message_id():
    assert turn_receipt.build_payload("s", None, {"run_id": "run-9"}, now=1.0)["id"] == "s:run-9"


# ── the emit entry point ────────────────────────────────────────────────

def test_incognito_emits_nothing(sync_spawn):
    assert turn_receipt.emit_turn_receipt("s", 1, _metrics(), incognito=True) is False
    assert sync_spawn == []


def test_a_session_without_an_id_emits_nothing(sync_spawn):
    assert turn_receipt.emit_turn_receipt("", 1, _metrics()) is False
    assert sync_spawn == []


def test_emit_hands_the_payload_to_the_sender_with_the_owner(sync_spawn):
    assert turn_receipt.emit_turn_receipt("s", 7, _metrics(), owner="owner-1") is True
    assert len(sync_spawn) == 1
    payload, owner = sync_spawn[0]
    assert owner == "owner-1" and payload["id"] == "s:7"


def test_the_caller_returns_at_once_even_when_the_hub_is_slow(monkeypatch):
    """The real sender on a daemon thread, a hub that takes 1.5 s to answer."""
    from src import family_services
    from src.hoard_link import family

    started, finished = threading.Event(), threading.Event()

    def slow_emit(*args, **kwargs):
        started.set()
        time.sleep(1.5)
        finished.set()
        return False

    monkeypatch.setattr(turn_receipt, "_send", turn_receipt._send_to_hub)
    monkeypatch.setattr(family_services, "configure", lambda: {"enabled": True})
    monkeypatch.setattr(family, "status", lambda: {"enabled": True})
    monkeypatch.setattr(family, "emit", slow_emit)

    t0 = time.monotonic()
    assert turn_receipt.emit_turn_receipt("s", 1, _metrics()) is True
    elapsed = time.monotonic() - t0
    assert elapsed < 0.3, f"the turn path waited {elapsed:.2f}s"
    assert started.wait(2.0), "the sender never ran"
    assert not finished.is_set(), "the sender ran inline"
    assert finished.wait(3.0)


def test_a_failing_hub_client_never_raises(monkeypatch):
    from src import family_services
    from src.hoard_link import family

    monkeypatch.setattr(family_services, "configure", lambda: (_ for _ in ()).throw(RuntimeError("no hub")))
    monkeypatch.setattr(turn_receipt, "_spawn", lambda fn, *a: fn(*a))
    monkeypatch.setattr(turn_receipt, "_send", turn_receipt._send_to_hub)
    assert turn_receipt.emit_turn_receipt("s", 1, _metrics()) is True  # queued; the failure is swallowed

    monkeypatch.setattr(family_services, "configure", lambda: {})
    monkeypatch.setattr(family, "status", lambda: {"enabled": True})
    monkeypatch.setattr(family, "emit", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert turn_receipt._send_to_hub({"session_id": "s", "id": "s:1"}) is False


def test_the_sender_uses_the_family_client_and_the_event_contract(monkeypatch):
    from src import family_services
    from src.hoard_link import family

    seen = []
    monkeypatch.setattr(family_services, "configure", lambda: seen.append("configure") or {})
    monkeypatch.setattr(family, "status", lambda: {"enabled": True})
    monkeypatch.setattr(family, "emit", lambda type, data=None, **kw: seen.append((type, data, kw)) or True)
    payload = turn_receipt.build_payload("s", 5, {"model": "m"}, now=3.0)
    payload["leak"] = "not allowed"
    assert turn_receipt._send_to_hub(payload) is True
    assert seen[0] == "configure"
    etype, data, kw = seen[1]
    assert etype == "faustus.turn.completed"
    assert "leak" not in data and set(data) <= set(turn_receipt.ALLOWED_KEYS)
    assert kw["block"] is True and kw["timeout"] <= 3.0


def test_the_family_off_switch_is_respected(monkeypatch):
    from src import family_services
    from src.hoard_link import family

    monkeypatch.setattr(family_services, "configure", lambda: {})
    monkeypatch.setattr(family, "status", lambda: {"enabled": False})
    called = []
    monkeypatch.setattr(family, "emit", lambda *a, **k: called.append(1) or True)
    assert turn_receipt._send_to_hub({"session_id": "s", "id": "s:1"}) is False
    assert called == []


# ── the hook in save_assistant_response ─────────────────────────────────

def test_save_assistant_response_emits_one_receipt_per_saved_turn(sync_spawn):
    from routes.chat_helpers import save_assistant_response

    sess = _Session()
    sm = SimpleNamespace(save_sessions=lambda: None)
    save_assistant_response(sess, sm, "sess-1", REPLY, _metrics())
    assert len(sync_spawn) == 1
    payload, owner = sync_spawn[0]
    assert owner == "owner-1"
    assert payload["id"] == "sess-1:4242" and payload["status"] == "ok"
    assert payload["model"] == "fake-coder" and payload["project_id"] == "proj-7"
    blob = json.dumps(payload)
    for secret in (PROMPT, REPLY, THINKING, "ZZ-TITLE-SECRET", "ZZ-FILE-CONTENT", "ZZ-CREDENTIAL"):
        assert secret not in blob

    save_assistant_response(sess, sm, "sess-1", REPLY, _metrics())
    assert len(sync_spawn) == 2, "one receipt per saved reply"


def test_a_failed_turn_is_an_error_receipt(sync_spawn):
    from routes.chat_helpers import save_assistant_response

    sess = _Session()
    sm = SimpleNamespace(save_sessions=lambda: None)
    save_assistant_response(sess, sm, "s", "[Agent stopped]", {"failed": True, "failure": {"status": 500}})
    assert sync_spawn[0][0]["status"] == "error"


def test_save_assistant_response_incognito_emits_no_receipt(sync_spawn):
    from routes.chat_helpers import save_assistant_response

    sess = _Session()
    assert save_assistant_response(sess, None, "s", REPLY, _metrics(), incognito=True) is None
    assert sync_spawn == []


def test_the_save_path_survives_a_broken_receipt(monkeypatch):
    from routes.chat_helpers import save_assistant_response

    monkeypatch.setattr(turn_receipt, "emit_turn_receipt",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    sess = _Session()
    sm = SimpleNamespace(save_sessions=lambda: None)
    assert save_assistant_response(sess, sm, "s", REPLY, _metrics()) == 4242
    assert len(sess.history) == 1
