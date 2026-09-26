"""Agent chats keep their rounds on one llama-server slot (src/llama_slots.py)."""
import pytest

from src import llama_slots
from src.llm_core import _apply_llamacpp_slot

URL = "http://127.0.0.1:8081/v1/chat/completions"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    llama_slots.reset()
    monkeypatch.setattr(llama_slots, "_enabled", lambda: True)
    yield
    llama_slots.reset()


def test_unknown_server_is_never_pinned():
    assert llama_slots.slot_for(URL, "chat-a") is None


def test_two_slots_are_not_enough():
    llama_slots.note_slot_count("http://127.0.0.1:8081/v1", 2)
    assert llama_slots.slot_for(URL, "chat-a") is None


def test_each_chat_keeps_its_own_slot_and_slot_zero_stays_free():
    llama_slots.note_slot_count("http://127.0.0.1:8081/v1", 4)
    a = llama_slots.slot_for(URL, "chat-a")
    b = llama_slots.slot_for(URL, "chat-b")
    assert a != b and 0 not in (a, b)
    assert llama_slots.slot_for(URL, "chat-a") == a  # same chat, same slot


def test_a_new_chat_avoids_a_slot_the_server_is_working_on():
    slots = [{"id": i, "is_processing": i != 3} for i in range(4)]
    llama_slots.note_slots(URL, slots)
    assert llama_slots.slot_for(URL, "chat-a") == 3


def test_full_server_gives_the_oldest_chats_slot_to_a_new_one():
    llama_slots.note_slot_count(URL, 3)  # slots 1 and 2 for chats
    a = llama_slots.slot_for(URL, "a")
    b = llama_slots.slot_for(URL, "b")
    llama_slots.slot_for(URL, "a")  # a used again: b is now the oldest
    c = llama_slots.slot_for(URL, "c")
    assert c == b and llama_slots.slot_for(URL, "a") == a


def test_fewer_slots_after_a_restart_drops_slots_it_no_longer_has():
    llama_slots.note_slot_count(URL, 6)
    for sid in "abcde":
        llama_slots.slot_for(URL, sid)
    llama_slots.note_slot_count(URL, 3)
    assert all(llama_slots.slot_for(URL, sid) in (1, 2) for sid in "abcde")


def test_payload_gets_id_slot_only_for_a_local_llamacpp_chat():
    llama_slots.note_slot_count(URL, 4)
    payload = {}
    _apply_llamacpp_slot(payload, URL, "chat-a")
    assert payload.get("id_slot") in (1, 2, 3)
    cloud = {}
    llama_slots.note_slot_count("https://api.openai.com/v1", 4)
    _apply_llamacpp_slot(cloud, "https://api.openai.com/v1/chat/completions", "chat-a")
    assert "id_slot" not in cloud
    helper = {}
    _apply_llamacpp_slot(helper, URL, None)
    assert helper.get("id_slot") not in (None, payload["id_slot"])


def test_setting_off_pins_nothing(monkeypatch):
    llama_slots.note_slot_count(URL, 4)
    monkeypatch.setattr(llama_slots, "_enabled", lambda: False)
    assert llama_slots.slot_for(URL, "chat-a") is None


def test_helper_requests_share_one_slot_no_chat_holds():
    llama_slots.note_slots(URL, [{"id": i, "is_processing": i == 0} for i in range(4)])
    chat = llama_slots.slot_for(URL, "chat-a")
    got = {llama_slots.helper_slot(URL) for _ in range(5)}
    assert len(got) == 1 and chat not in got and 0 not in got  # 0 is busy right now


def test_a_stale_slot_count_is_not_trusted(monkeypatch):
    llama_slots.note_slot_count(URL, 4)
    now = llama_slots.time.monotonic()
    monkeypatch.setattr(llama_slots.time, "monotonic", lambda: now + llama_slots.COUNT_TTL + 1)
    assert llama_slots.slot_for(URL, "chat-a") is None
    assert llama_slots.helper_slot(URL) is None
