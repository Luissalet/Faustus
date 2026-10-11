"""A pinned document's llama-server slot is saved after its first turn and
restored on later ones (src/slot_cache.py, #254).

The server is `tests/fake_llama_server.py`: a real HTTP server that keeps
tokens per slot, answers /slots save/restore/erase like llama-server (501
without a slot save path) and reports `timings` with `cache_n`. The turns go
through `llm_core.stream_llm`, so what is checked is the request Faustus
really sends, in the order it sends it.
"""
import asyncio
import json
import os
import time

import pytest

from src import llama_slots, llm_core, slot_cache
from src.prompt_security import untrusted_context_message
from tests.fake_llama_server import FakeLlamaServer
from tests.test_side_threads import _mk_document, _mk_session, db, sm  # noqa: F401 - the DB harness

DOC = " ".join(f"clause{i} of the agreement binds the parties" for i in range(120))  # ~ 840 words
SYSTEM = "You are a careful assistant. Answer from the pinned document."


@pytest.fixture
def conf(monkeypatch, tmp_path):
    values = {
        "llamacpp_slot_cache": True,
        "llamacpp_slot_cache_max_tokens": 8000,
        "llamacpp_slot_cache_dir": str(tmp_path / "slots"),
        "llamacpp_slot_cache_max_mb": 4096,
        "llamacpp_slot_cache_max_age_days": 14,
        "llamacpp_pin_session_slot": True,
    }
    monkeypatch.setattr(slot_cache, "_setting", lambda key, default=None: values.get(key, default))
    monkeypatch.setattr(llama_slots, "_enabled", lambda: True)
    slot_cache.reset()
    yield values
    slot_cache.reset()


@pytest.fixture
def server(conf, tmp_path):
    with FakeLlamaServer(slot_save_path=conf["llamacpp_slot_cache_dir"], ms_per_prompt_token=0.2) as fake:
        llama_slots.note_slot_count(fake.url, fake.n_slots)
        yield fake


def _messages(question, doc=DOC, title="Agreement", history=()):
    msgs = [{"role": "system", "content": SYSTEM},
            untrusted_context_message(f"wired document context: {title}", doc)]
    msgs.extend(history)
    msgs.append({"role": "user", "content": question})
    return msgs


def _turn(fake, messages, session="chat-a", tools=None, model="fake-model"):
    async def run():
        usage, text = None, ""
        async for chunk in llm_core.stream_llm(fake.url + "/chat/completions", model, messages,
                                               session_id=session, tools=tools):
            if chunk.startswith("data: ") and "[DONE]" not in chunk:
                data = json.loads(chunk[6:])
                if data.get("type") == "usage":
                    usage = data["data"]
                elif "delta" in data and not data.get("thinking"):
                    text += data["delta"]
        await slot_cache.drain()
        return text, usage
    return asyncio.run(run())


def _slot_calls(fake):
    return [(r["action"], r["body"].get("filename")) for r in fake.calls()]


def _order(fake):
    """The requests in the order the server saw them, as short labels."""
    out = []
    with fake.lock:
        rows = list(fake.requests)
    for r in rows:
        if r["path"].startswith("/slots/"):
            out.append(r["action"])
        elif r["path"].endswith("/chat/completions"):
            out.append("chat")
    return out


# ── the main flow ──────────────────────────────────────────────────────────

def test_first_turn_saves_and_a_later_turn_restores_after_the_slot_was_taken(server, monkeypatch):
    # Two chats end up on the same slot (a server with fewer free slots).
    monkeypatch.setattr(llama_slots, "slot_for", lambda url, sid: 1)

    text, usage = _turn(server, _messages("What binds the parties?"), session="chat-a")
    assert text.startswith("The document says")
    pc = usage["prefix_cache"]
    assert pc["status"] == "saved" and pc["slot"] == 1 and pc["prefix_tokens"] > 256
    assert _order(server).count("save") == 1
    saved = os.path.join(server.slot_save_path, server.calls("save")[0]["body"]["filename"])
    assert os.path.getsize(saved) > 0

    # Another chat takes slot 1 with something else.
    _turn(server, [{"role": "system", "content": "other"}, {"role": "user", "content": "unrelated words here"}],
          session="chat-b")
    assert server.slots[1][:3] == ["<sys", "tem>", "othe"]

    saved_name = server.calls("save")[0]["body"]["filename"]
    server.requests.clear()
    _text, usage2 = _turn(server, _messages("And the second clause?"), session="chat-a")
    pc2 = usage2["prefix_cache"]
    assert pc2["status"] == "restored"
    assert _order(server) == ["restore", "chat"]  # restored BEFORE the completion
    assert server.calls("restore")[0]["body"]["filename"] == saved_name
    assert server.calls("restore")[0]["body"]["filename"] != ""
    assert usage2["engine_timings"]["cache_n"] > 500  # the document was not read again
    assert pc2["cache_n"] == usage2["engine_timings"]["cache_n"]
    assert pc2["prefill_saved_ms"] > 0
    assert pc2["restore_ms"] >= 0


def test_the_next_turn_on_a_slot_that_still_holds_the_document_does_not_restore(server):
    _turn(server, _messages("first"))
    server.requests.clear()
    _text, usage = _turn(server, _messages("second"))
    assert usage["prefix_cache"]["status"] == "warm"
    assert _order(server) == ["chat"]
    assert usage["engine_timings"]["cache_n"] > 500


def test_the_save_names_the_file_from_a_hash_and_a_second_chat_of_the_same_prefix_reuses_it(server):
    _turn(server, _messages("first"), session="chat-a")
    first = server.calls("save")[0]["body"]["filename"]
    assert first.startswith("fsc-") and first.endswith(".bin")
    server.requests.clear()
    # A different chat with the same system + document: another slot, same state on disk.
    _text, usage = _turn(server, _messages("hello"), session="chat-b")
    assert usage["prefix_cache"]["status"] == "restored"
    assert server.calls("restore")[0]["body"]["filename"] == first
    assert not server.calls("save")


# ── invalidation ───────────────────────────────────────────────────────────

def test_an_edited_document_invalidates_and_saves_again(server):
    _turn(server, _messages("q1"))
    old = server.calls("save")[0]["body"]["filename"]
    server.requests.clear()
    _text, usage = _turn(server, _messages("q2", doc=DOC + " one more clause added later"))
    assert usage["prefix_cache"]["status"] == "saved"
    new = server.calls("save")[0]["body"]["filename"]
    assert new != old
    assert not server.calls("restore")
    # The chat no longer needs the old state: it is gone from disk and the index.
    assert not os.path.exists(os.path.join(server.slot_save_path, old))
    assert os.path.exists(os.path.join(server.slot_save_path, new))
    assert len(slot_cache.status()["entries"]) == 1


def test_another_model_or_template_or_tools_is_another_key(server):
    prefix, docs = slot_cache.pinned_prefix(_messages("x"))
    assert docs == 1 and len(prefix) == 2
    base = slot_cache.prefix_key("m1", "f|t", prefix, None)
    assert base == slot_cache.prefix_key("m1", "f|t", prefix, None)
    assert base != slot_cache.prefix_key("m2", "f|t", prefix, None)
    assert base != slot_cache.prefix_key("m1", "f|t2", prefix, None)
    assert base != slot_cache.prefix_key("m1", "f|t", prefix, [{"type": "function", "function": {"name": "x"}}])
    edited, _ = slot_cache.pinned_prefix(_messages("x", doc=DOC + " edit"))
    assert base != slot_cache.prefix_key("m1", "f|t", edited, None)


def test_the_question_and_history_are_not_part_of_the_key(server):
    a, _ = slot_cache.pinned_prefix(_messages("first question"))
    b, _ = slot_cache.pinned_prefix(_messages("another", history=[{"role": "user", "content": "h"},
                                                                  {"role": "assistant", "content": "r"}]))
    assert slot_cache.prefix_key("m", "", a, None) == slot_cache.prefix_key("m", "", b, None)


def test_a_server_template_change_is_seen_through_props(server, conf):
    _turn(server, _messages("q1"))
    first = server.calls("save")[0]["body"]["filename"]
    server.chat_template = "another-template"
    slot_cache._PROPS.clear()
    server.requests.clear()
    _turn(server, _messages("q2"))
    assert server.calls("save")[0]["body"]["filename"] != first


# ── failure: the turn always goes on ───────────────────────────────────────

def test_a_failing_restore_falls_back_to_the_normal_path(server, monkeypatch, caplog):
    monkeypatch.setattr(llama_slots, "slot_for", lambda url, sid: 1)
    _turn(server, _messages("q1"), session="chat-a")
    _turn(server, [{"role": "system", "content": "other"}, {"role": "user", "content": "unrelated"}], session="chat-b")
    server.fail_actions["restore"] = 500
    server.requests.clear()
    with caplog.at_level("WARNING"):
        text, usage = _turn(server, _messages("q2"), session="chat-a")
    assert text.startswith("The document says")  # the turn answered
    assert usage["engine_timings"]["cache_n"] < 50  # and read the document normally
    assert usage["prefix_cache"]["status"] in ("miss", "saved")
    assert any("answered HTTP 500 to a restore" in r.message for r in caplog.records)
    # Logged once, not on every turn.
    caplog.clear()
    with caplog.at_level("WARNING"):
        _turn(server, [{"role": "system", "content": "other"}, {"role": "user", "content": "unrelated"}], session="chat-b")
        _turn(server, _messages("q3"), session="chat-a")
    assert not [r for r in caplog.records if "to a restore" in r.message]


def test_a_failing_save_does_not_fail_the_turn_and_is_retried_later(server, caplog):
    server.fail_actions["save"] = 500
    with caplog.at_level("WARNING"):
        text, usage = _turn(server, _messages("q1"))
    assert text.startswith("The document says")
    assert usage["prefix_cache"]["status"] == "miss"
    assert slot_cache.status()["entries"] == []
    assert any("to a save" in r.message for r in caplog.records)
    # No retry on the very next turn (the failure is remembered for a while)...
    server.requests.clear()
    _turn(server, _messages("q2"))
    assert not server.calls("save")
    # ...but later, once the server works again, it saves.
    server.fail_actions.clear()
    slot_cache._SAVE_FAILED.clear()
    _text, usage3 = _turn(server, _messages("q3"))
    assert usage3["prefix_cache"]["status"] == "saved"


def test_a_server_without_a_slot_save_path_is_left_alone(conf):
    with FakeLlamaServer(slot_save_path=None) as plain:
        llama_slots.note_slot_count(plain.url, plain.n_slots)
        _text, usage = _turn(plain, _messages("q1"))
        assert usage["prefix_cache"]["status"] == "off" and usage["prefix_cache"]["reason"] == "unsupported"
        assert len(plain.calls()) == 1  # the one save that answered 501
        plain.requests.clear()
        text, usage2 = _turn(plain, _messages("q2"))
        assert text.startswith("The document says")
        assert usage2["prefix_cache"]["reason"] == "unsupported"
        assert plain.calls() == []  # not asked again
        assert "unsupported_servers" in slot_cache.status()
        assert slot_cache.status()["unsupported_servers"] == [plain.base]


def test_a_dead_server_for_the_slot_calls_never_blocks_the_turn(server, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(slot_cache, "_call", boom)
    text, usage = _turn(server, _messages("q1"))
    assert text.startswith("The document says")
    assert usage.get("prefix_cache", {}).get("status") in (None, "miss", "off")


def test_a_restore_that_the_server_did_not_honour_is_reported_as_a_miss(server, monkeypatch):
    monkeypatch.setattr(llama_slots, "slot_for", lambda url, sid: 1)
    _turn(server, _messages("q1"), session="chat-a")
    # The server forgets the slot but the restore "succeeds" without effect.
    _turn(server, [{"role": "system", "content": "other"}, {"role": "user", "content": "unrelated"}], session="chat-b")
    real = server._slot_action

    def lying(handler, slot, action, body):
        if action == "restore":
            return handler._send(200, {"id_slot": slot, "n_restored": 0})
        return real(handler, slot, action, body)
    server._slot_action = lying
    _text, usage = _turn(server, _messages("q2"), session="chat-a")
    pc = usage["prefix_cache"]
    assert pc["status"] in ("miss", "saved") and "did not reuse" in pc.get("reason", "")


# ── when it does nothing ───────────────────────────────────────────────────

def test_off_by_default_and_when_turned_off_nothing_is_sent(server, conf):
    conf["llamacpp_slot_cache"] = False
    _text, usage = _turn(server, _messages("q1"))
    assert "prefix_cache" not in usage
    assert server.calls() == []


def test_a_chat_without_a_pinned_document_is_untouched(server):
    msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "just a question " * 40}]
    _text, usage = _turn(server, msgs)
    assert "prefix_cache" not in usage and server.calls() == []


def test_a_prefix_over_the_token_limit_or_under_the_floor_is_not_saved(server, conf):
    conf["llamacpp_slot_cache_max_tokens"] = 300
    _text, usage = _turn(server, _messages("q"))
    assert usage["prefix_cache"]["status"] == "off" and usage["prefix_cache"]["reason"] == "too_large"
    conf["llamacpp_slot_cache_max_tokens"] = 8000
    _text, usage = _turn(server, _messages("q", doc="tiny"))
    assert usage["prefix_cache"]["reason"] == "too_small"
    assert server.calls() == []


def test_a_request_without_a_named_slot_is_not_touched(server, monkeypatch):
    monkeypatch.setattr(llama_slots, "slot_for", lambda url, sid: None)
    _text, usage = _turn(server, _messages("q"))
    assert "prefix_cache" not in usage and server.calls() == []


def test_the_section_stops_at_the_first_message_that_is_not_system_or_a_document():
    note = {"role": "user", "content": "[Note]\nremember this"}
    msgs = [{"role": "system", "content": "s"},
            untrusted_context_message("wired document context: A", "body a"), note,
            untrusted_context_message("wired document context: B", "body b")]
    prefix, docs = slot_cache.pinned_prefix(msgs)
    assert docs == 1 and len(prefix) == 2


def test_a_document_really_pinned_to_a_chat_is_found_by_the_prefix(sm):
    # The label is built in src/side_threads.py::material_block; if it ever
    # changes, the prefix would silently stop matching.
    from src.side_threads import add_material, inherited_context_with_snapshot
    _mk_session(sm, "main", n_messages=1)
    _mk_document("doc1", content=DOC)
    add_material("alice", "main", kind="document", document_id="doc1", depth="full")
    wired, _snapshot = inherited_context_with_snapshot(sm, "alice", "main")
    prefix, docs = slot_cache.pinned_prefix([{"role": "system", "content": SYSTEM}] + wired
                                            + [{"role": "user", "content": "hi"}])
    assert docs == 1 and len(prefix) == 2 and "clause7 of the agreement" in prefix[1]["content"]


# ── agent turns: several rounds ────────────────────────────────────────────

def test_rounds_of_one_turn_fold_into_one_record():
    miss = {"status": "miss", "slot": 1}
    saved = {"status": "saved", "slot": 1, "prefill_saved_ms": 0.0}
    warm = {"status": "warm", "slot": 1, "prefill_saved_ms": 120.5}
    restored = {"status": "restored", "slot": 1, "prefill_saved_ms": 300.0}
    folded = slot_cache.merge_reports(None, saved)
    folded = slot_cache.merge_reports(folded, warm)
    assert folded["status"] == "saved" and folded["rounds"] == 2 and folded["prefill_saved_ms"] == 120.5
    folded = slot_cache.merge_reports(slot_cache.merge_reports(None, restored), warm)
    assert folded["status"] == "restored" and folded["prefill_saved_ms"] == 420.5
    assert slot_cache.merge_reports(miss, None) == miss
    assert slot_cache.merge_reports(None, {"nothing": 1}) is None


# ── the folder: LRU by size and age, status, clear ─────────────────────────

def _seed(conf, entries):
    directory = conf["llamacpp_slot_cache_dir"]
    os.makedirs(directory, exist_ok=True)
    index = {"version": 1, "entries": {}}
    now = time.time()
    for key, size, age_days, sessions in entries:
        filename = slot_cache.filename_for(key)
        with open(os.path.join(directory, filename), "wb") as fh:
            fh.write(b"0" * size)
        index["entries"][key] = {"file": filename, "bytes": size, "tokens": 1000, "model": "m",
                                 "created": now - age_days * 86400, "last_used": now - age_days * 86400,
                                 "sessions": sessions, "servers": ["http://127.0.0.1:1"]}
    slot_cache._write_index(directory, index)
    return directory


def test_cleanup_drops_the_least_recently_used_over_the_size_cap(conf):
    mb = 1024 * 1024
    conf["llamacpp_slot_cache_max_mb"] = 2
    directory = _seed(conf, [("a" * 64, mb, 3, ["s1"]), ("b" * 64, mb, 2, ["s2"]),
                             ("c" * 64, mb, 1, ["s3"]), ("d" * 64, mb, 0, ["s4"])])
    result = slot_cache.cleanup()
    assert result["removed"] == 2 and result["freed_bytes"] == 2 * mb
    left = sorted(slot_cache._load_index(directory)["entries"])
    assert left == ["c" * 64, "d" * 64]  # the two used most recently stay
    assert not os.path.exists(os.path.join(directory, slot_cache.filename_for("a" * 64)))


def test_cleanup_drops_entries_older_than_the_age_cap_and_orphans_and_missing_files(conf):
    directory = _seed(conf, [("a" * 64, 1000, 20, ["s1"]), ("b" * 64, 1000, 1, ["s2"])])
    orphan = os.path.join(directory, slot_cache.filename_for("e" * 64))
    with open(orphan, "wb") as fh:
        fh.write(b"x")
    os.utime(orphan, (time.time() - 7200, time.time() - 7200))
    fresh_orphan = os.path.join(directory, slot_cache.filename_for("f" * 64))  # may be mid-save
    with open(fresh_orphan, "wb") as fh:
        fh.write(b"x")
    os.remove(os.path.join(directory, slot_cache.filename_for("b" * 64)))  # file gone, entry stays
    slot_cache.cleanup()
    assert slot_cache._load_index(directory)["entries"] == {}
    assert not os.path.exists(orphan) and os.path.exists(fresh_orphan)


def test_status_lists_the_files_and_clear_removes_them(server):
    _turn(server, _messages("q1"), session="chat-a")
    st = slot_cache.status()
    assert st["enabled"] and len(st["entries"]) == 1 and st["total_bytes"] > 0
    assert st["entries"][0]["sessions"] == 1 and st["stats"]["saved"] == 1
    # Only this chat's states go when a chat is named; a shared state stays.
    _turn(server, _messages("hello"), session="chat-b")  # restores the same state
    assert slot_cache.status()["entries"][0]["sessions"] == 2
    assert slot_cache.clear("chat-a")["removed"] == 0
    assert slot_cache.status()["entries"][0]["sessions"] == 1
    assert slot_cache.clear("chat-b")["removed"] == 1
    assert slot_cache.status()["entries"] == []
    assert [n for n in os.listdir(server.slot_save_path) if n.startswith("fsc-")] == []


def test_clear_all_makes_the_next_turn_save_again(server):
    _turn(server, _messages("q1"))
    assert slot_cache.clear()["removed"] == 1
    server.requests.clear()
    _text, usage = _turn(server, _messages("q2"))
    assert usage["prefix_cache"]["status"] == "saved"
    assert _order(server).count("save") == 1


def test_the_size_cap_is_applied_when_a_state_is_saved(server, conf):
    conf["llamacpp_slot_cache_max_mb"] = 1
    _seed(conf, [("a" * 64, 900 * 1024, 5, ["old"])])
    _turn(server, _messages("q1"))  # the new state is ~600 KB in the fake
    keys = list(slot_cache._load_index(conf["llamacpp_slot_cache_dir"])["entries"])
    assert "a" * 64 not in keys and len(keys) == 1


# ── the routes ─────────────────────────────────────────────────────────────

def test_routes_report_and_clear(server, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routes.engine_routes as er
    monkeypatch.setattr(er, "require_admin", lambda request: None)
    _turn(server, _messages("q1"))
    app = FastAPI()
    app.include_router(er.setup_engine_routes())
    with TestClient(app) as client:
        body = client.get("/api/engines/slot-cache").json()
        assert body["enabled"] is True and len(body["entries"]) == 1
        done = client.delete("/api/engines/slot-cache").json()
        assert done["removed"] == 1
        assert client.get("/api/engines/slot-cache").json()["entries"] == []


def test_routes_need_an_admin(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import core.middleware as mw
    import routes.engine_routes as er
    monkeypatch.setattr(mw, "auth_disabled", lambda: False)
    app = FastAPI()
    app.include_router(er.setup_engine_routes())
    with TestClient(app) as client:
        assert client.get("/api/engines/slot-cache").status_code in (401, 403)
        assert client.delete("/api/engines/slot-cache").status_code in (401, 403)


# ── the launcher ───────────────────────────────────────────────────────────

def test_an_engine_started_from_faustus_gets_the_slot_save_path_only_when_enabled(conf, monkeypatch):
    from src import engines, launch_profiles
    profile = {"id": "e1", "name": "eng", "argv": ["-m", "x.gguf", "--port", "8081"]}
    updates = []
    monkeypatch.setattr(launch_profiles, "update_profile", lambda pid, **f: updates.append((pid, f)))
    conf["llamacpp_slot_cache"] = False
    assert engines._ensure_slot_save_path(profile) is None and updates == []
    conf["llamacpp_slot_cache"] = True
    added = engines._ensure_slot_save_path(profile)
    assert added == conf["llamacpp_slot_cache_dir"] and os.path.isdir(added)
    assert updates == [("e1", {"argv": profile["argv"] + ["--slot-save-path", added]})]
    # A profile that already names a folder keeps it.
    updates.clear()
    own = {"id": "e2", "name": "eng2", "argv": ["--slot-save-path", "D:/mine"]}
    assert engines._ensure_slot_save_path(own) is None and updates == []
    assert engines._parse_fields(own)["slot_save_path"] == "D:/mine"


def test_a_server_restart_costs_one_slow_turn_and_keeps_the_saved_file(server):
    _turn(server, _messages("q1"))
    name = server.calls("save")[0]["body"]["filename"]
    server.slots = [[] for _ in range(server.n_slots)]  # the server restarted: every slot is empty
    server.requests.clear()
    _text, usage = _turn(server, _messages("q2"))
    pc = usage["prefix_cache"]
    assert pc["status"] == "miss" and "did not reuse" in pc["reason"]  # said so, from the server's own timings
    assert not server.calls("save")  # the file was fine: no new one
    assert os.path.exists(os.path.join(server.slot_save_path, name))
    _text, usage3 = _turn(server, _messages("q3"))
    assert usage3["prefix_cache"]["status"] == "warm"


# ── review round 1: clears, unfinished saves and the server's identity ─────

TIMINGS = {"prompt_n": 2000, "cache_n": 0, "prompt_ms": 400.0}


async def _plan_and_finish(fake, session="chat-a", slot=1, question="q", doc=DOC):
    plan = await slot_cache.prepare(fake.url, "fake-model", {"id_slot": slot}, _messages(question, doc=doc),
                                    None, session)
    assert plan is not None
    return plan, await slot_cache.finish(plan, dict(TIMINGS))


def _files(fake):
    return [n for n in os.listdir(fake.slot_save_path) if n.startswith("fsc-")] \
        if os.path.isdir(fake.slot_save_path) else []


def test_a_save_still_running_is_not_certified_as_saved(server, monkeypatch):
    monkeypatch.setattr(slot_cache, "SAVE_BUDGET_S", 0.1)
    server.action_delay["save"] = 0.6

    async def go():
        plan, report = await _plan_and_finish(server)
        mid = (report["status"], slot_cache.status()["stats"]["saved"], list(slot_cache.status()["entries"]))
        await slot_cache.drain()
        return plan, report, mid

    plan, report, mid = asyncio.run(go())
    assert mid == ("saving", 0, [])  # nothing certified while the server is still writing
    stats = slot_cache.status()["stats"]
    assert stats["saved"] == 1 and stats["errors"] == 0  # counted when it landed
    assert report["status"] == "saved"  # the turn's record, still held, follows it
    assert len(slot_cache.status()["entries"]) == 1


def test_a_save_that_fails_after_the_turn_stopped_waiting_is_counted_and_reported_next_turn(server, monkeypatch):
    monkeypatch.setattr(slot_cache, "SAVE_BUDGET_S", 0.1)
    server.action_delay["save"] = 0.4
    server.fail_actions["save"] = 500

    async def go():
        plan, report = await _plan_and_finish(server)
        assert report["status"] == "saving"
        await slot_cache.drain()
        server.fail_actions.clear()
        server.action_delay.clear()
        nxt_plan, nxt = await _plan_and_finish(server, question="q2")
        return report, nxt

    report, nxt = asyncio.run(go())
    stats = slot_cache.status()["stats"]
    assert stats["errors"] == 1 and stats["saved"] == 0
    assert slot_cache.status()["entries"] == [] and _files(server) == []
    assert report["status"] == "miss" and report["reason"] == "the save failed"
    assert nxt["earlier_save"] == "failed"  # the chat's next turn says what happened


def test_a_save_that_fails_inside_the_wait_budget_counts_as_an_error_too(server):
    server.fail_actions["save"] = 500

    async def go():
        return await _plan_and_finish(server)

    _plan, report = asyncio.run(go())
    stats = slot_cache.status()["stats"]
    assert report["status"] == "miss" and stats["errors"] == 1 and stats["saved"] == 0


def test_a_save_started_before_clear_all_records_nothing_when_it_lands(server, monkeypatch):
    monkeypatch.setattr(slot_cache, "SAVE_BUDGET_S", 0.1)
    server.action_delay["save"] = 0.6

    async def go():
        plan, report = await _plan_and_finish(server)
        assert report["status"] == "saving"
        cleared = slot_cache.clear()
        assert cleared["removed"] == 0 and cleared["pending_saves"] == 1
        await slot_cache.drain()
        return plan, report

    _plan, report = asyncio.run(go())
    assert slot_cache.status()["entries"] == []
    assert _files(server) == []  # the file the server wrote late was removed again
    stats = slot_cache.status()["stats"]
    assert stats["saved"] == 0 and stats["errors"] == 0
    assert report["status"] == "miss" and "cleared" in report["reason"]
    # A clear is not a failure: the chat's next turn saves again.
    server.action_delay.clear()
    monkeypatch.setattr(slot_cache, "SAVE_BUDGET_S", 5.0)
    _text, usage = _turn(server, _messages("q2"), session="chat-a")
    assert usage["prefix_cache"]["status"] == "saved"
    assert len(slot_cache.status()["entries"]) == 1


def test_a_save_started_before_a_chat_clear_records_nothing_but_a_shared_state_is_respected(server, monkeypatch):
    monkeypatch.setattr(slot_cache, "SAVE_BUDGET_S", 0.1)
    server.action_delay["save"] = 3.0

    async def go():
        plan_a, report_a = await _plan_and_finish(server, session="chat-a", slot=1)
        assert report_a["status"] == "saving"
        while not server.calls("save"):  # the late save has reached the server (and is sleeping there)
            await asyncio.sleep(0.02)
        # Meanwhile another chat saves the very same prefix.
        server.action_delay.clear()
        monkeypatch.setattr(slot_cache, "SAVE_BUDGET_S", 5.0)
        _plan_b, report_b = await _plan_and_finish(server, session="chat-b", slot=2)
        assert report_b["status"] == "saved"
        assert slot_cache.clear("chat-a")["pending_saves"] == 1
        await slot_cache.drain()
        return report_a

    report_a = asyncio.run(go())
    entries = slot_cache.status()["entries"]
    assert len(entries) == 1 and entries[0]["sessions"] == 1  # only chat-b, not chat-a
    assert len(_files(server)) == 1  # the state chat-b uses was not taken away
    assert report_a["status"] == "miss"
    # And a chat clear with nothing shared removes the late file.
    slot_cache.clear()
    slot_cache.reset()
    monkeypatch.setattr(slot_cache, "SAVE_BUDGET_S", 0.1)
    server.action_delay["save"] = 0.8

    async def alone():
        _p, rep = await _plan_and_finish(server, session="chat-c", slot=1)
        assert rep["status"] == "saving"
        slot_cache.clear("chat-c")
        await slot_cache.drain()

    asyncio.run(alone())
    assert slot_cache.status()["entries"] == [] and _files(server) == []


def test_a_clear_of_another_chat_does_not_cancel_this_chats_late_save(server, monkeypatch):
    monkeypatch.setattr(slot_cache, "SAVE_BUDGET_S", 0.1)
    server.action_delay["save"] = 0.4

    async def go():
        _plan, report = await _plan_and_finish(server, session="chat-a")
        assert report["status"] == "saving"
        assert slot_cache.clear("chat-z")["pending_saves"] == 0
        await slot_cache.drain()

    asyncio.run(go())
    assert len(slot_cache.status()["entries"]) == 1 and slot_cache.status()["stats"]["saved"] == 1


def test_the_clear_route_says_how_many_saves_it_dropped(server, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routes.engine_routes as er
    monkeypatch.setattr(er, "require_admin", lambda request: None)
    monkeypatch.setattr(slot_cache, "SAVE_BUDGET_S", 0.1)
    server.action_delay["save"] = 0.5
    app = FastAPI()
    app.include_router(er.setup_engine_routes())

    async def go(client):
        _plan, report = await _plan_and_finish(server)
        assert report["status"] == "saving"
        done = await asyncio.to_thread(client.delete, "/api/engines/slot-cache")
        await slot_cache.drain()
        return done.json()

    with TestClient(app) as client:
        body = asyncio.run(go(client))
    assert body["removed"] == 0 and body["pending_saves"] == 1
    assert slot_cache.status()["entries"] == []


def _id(**changes):
    props = {"model_path": "D:/m/a.gguf", "chat_template": "t", "total_slots": 4, "build_info": "b1",
             "default_generation_settings": {"n_ctx": 8192, "params": {}}}
    for key, value in changes.items():
        if key in ("n_ctx", "n_batch", "n_ubatch", "cache_type_k", "cache_type_v"):
            props["default_generation_settings"][key] = value
        else:
            props[key] = value
    return slot_cache.identity_from_props(props)


def test_the_server_identity_covers_what_a_saved_state_depends_on():
    base = _id()
    assert base == _id()
    for changed in (_id(model_path="D:/other/a.gguf"), _id(model_path="a.gguf"), _id(chat_template="t2"),
                    _id(n_ctx=4096), _id(n_batch=512), _id(n_ubatch=128), _id(cache_type_k="q8_0"),
                    _id(cache_type_v="q4_0"), _id(build_info="b2"), _id(total_slots=8)):
        assert changed != base
    # Without the context size in /props, the slot list gives it.
    bare = {"model_path": "D:/m/a.gguf", "chat_template": "t", "total_slots": 2}
    assert slot_cache.identity_from_props(bare, [{"id": 0, "n_ctx": 4096}]) != \
        slot_cache.identity_from_props(bare, [{"id": 0, "n_ctx": 8192}])


def test_a_model_file_replaced_in_place_is_another_identity(tmp_path):
    model = tmp_path / "m.gguf"
    model.write_bytes(b"x" * 100)
    props = {"model_path": str(model), "chat_template": "t"}
    first = slot_cache.identity_from_props(props)
    model.write_bytes(b"y" * 250)
    assert slot_cache.identity_from_props(props) != first
    # A file this machine cannot see falls back to what /v1/models reports.
    remote = {"model_path": "/srv/models/m.gguf", "chat_template": "t"}
    assert slot_cache.identity_from_props(remote, None, {"size": 1, "n_params": 5}) != \
        slot_cache.identity_from_props(remote, None, {"size": 2, "n_params": 5})


def test_two_servers_that_differ_only_in_context_or_model_directory_have_other_identities(conf):
    async def ident(fake):
        return await slot_cache._server_fingerprint(fake.base, None)

    async def go():
        with FakeLlamaServer(n_ctx=8192) as a, FakeLlamaServer(n_ctx=16384) as b, \
                FakeLlamaServer(n_ctx=8192) as c, \
                FakeLlamaServer(n_ctx=8192, model_path="E:/elsewhere/fake-model.gguf") as d:
            return [await ident(s) for s in (a, b, c, d)]

    a, b, c, d = asyncio.run(go())
    assert a != b  # same file name and template, another context size
    assert a != d  # same file name, another directory
    assert a == c  # the same configuration is the same identity


def test_a_context_change_on_the_same_server_makes_another_state(server):
    _turn(server, _messages("q1"))
    first = server.calls("save")[0]["body"]["filename"]
    server.n_ctx = 4096  # restarted with a smaller context on the same port
    server.requests.clear()
    _text, usage = _turn(server, _messages("q2"))
    assert usage["prefix_cache"]["status"] == "saved"
    assert server.calls("save")[0]["body"]["filename"] != first


def test_props_is_read_every_turn_so_a_change_is_seen_at_once(server):
    _turn(server, _messages("q1"))
    _turn(server, _messages("q2"))
    props_reads = [r for r in server.requests if r["path"] == "/props"]
    assert len(props_reads) == 2
