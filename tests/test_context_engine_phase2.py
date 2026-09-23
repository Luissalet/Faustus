"""Context Engine phase 2: the live path is safe to switch on.

Four promises are tested here, each the answer to a specific way the live
packet could have made a turn worse than the legacy prompt it replaces:

* **fail-safe** — with `agent_context_engine` on, the legacy learned-memory
  block is built as a standby. A round that gets no packet (None, timeout,
  crash) keeps it, at the legacy position; a round that gets one drops it.
  Never both, never neither, credited once.
* **scope from the runtime** — `_build_system_prompt(project_id=...)` uses the
  project the route already resolved instead of re-resolving it.
* **history** — the persisted, owner-checked session history serves compiles
  that have no live transcript, and a live `history_scope` always wins.
* **receipts** — every delivered packet is receipted exactly once, including
  when the stream is stopped mid-turn.

The recoverable-omission tests (OBJ-29) live in
`tests/test_context_engine_recall.py`.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from src import agent_loop as al
from src import memory_engine
from src.context_engine import wiring
from src.context_engine.adapters import sessions as session_adapter
from src.context_engine.adapters import session_store

MEMORY_TEXT = "Always run the project tests before claiming a fix."


# ── helpers ────────────────────────────────────────────────────────────────

@pytest.fixture()
def learned_memory(monkeypatch):
    """A memory store with one rule, recording every `note_injected`."""
    noted = []
    monkeypatch.setattr(memory_engine, "injection_enabled", lambda: True)
    monkeypatch.setattr(memory_engine, "injection_budget", lambda: 1000)
    monkeypatch.setattr(memory_engine, "pack_for_session",
                        lambda *a, **k: {"text": MEMORY_TEXT, "ids": ["m-ada-1"]})
    monkeypatch.setattr(memory_engine, "note_injected",
                        lambda key, ids, **k: noted.append((key, list(ids))))
    return noted


def _build(messages=None, **kw):
    al._cached_base_prompt = None
    al._cached_base_prompt_key = None
    built, _ = al._build_system_prompt(
        list(messages or [{"role": "user", "content": "please run the tests"}]),
        "test-model", None, None, set(),
        relevant_tools={"read_file"}, suppress_skills=True, **kw)
    return built


def _memory_messages(messages):
    return [m for m in messages if MEMORY_TEXT in str(m.get("content") or "")]


def _packet_messages(messages):
    return [m for m in messages if m.get("_agent_injected") == "context_engine"]


# ── 1. fail-safe: the standby block ────────────────────────────────────────

def test_engine_off_injects_and_credits_the_legacy_block(monkeypatch, learned_memory):
    monkeypatch.setattr(wiring, "enabled", lambda: False)
    built = _build(owner="ada", session_id="s-1")
    memory = _memory_messages(built)
    assert len(memory) == 1
    assert not al._is_legacy_memory_standby(memory[0])
    assert learned_memory == [("s-1", ["m-ada-1"])]


def test_engine_on_builds_the_block_as_an_uncredited_standby(monkeypatch, learned_memory):
    monkeypatch.setattr(wiring, "enabled", lambda: True)
    built = _build(owner="ada", session_id="s-1")
    memory = _memory_messages(built)
    assert len(memory) == 1, "the standby is in its legacy slot"
    assert al._is_legacy_memory_standby(memory[0])
    assert memory[0][al._LEGACY_MEMORY_IDS_KEY] == ["m-ada-1"]
    assert memory[0]["_agent_injected"] == "context"          # same tag
    assert memory[0]["metadata"]["trusted"] is False           # same lane
    assert learned_memory == [], "credited only when it is really sent"


def test_incognito_still_gets_no_standby(monkeypatch, learned_memory):
    monkeypatch.setattr(wiring, "enabled", lambda: True)
    built = _build(owner="ada", session_id="s-1", suppress_personal_memory=True)
    assert _memory_messages(built) == []


def test_restore_puts_the_block_back_in_the_legacy_slot():
    memory = {"role": "user", "content": MEMORY_TEXT, "_agent_injected": "context",
              al._LEGACY_MEMORY_STANDBY_KEY: "learned_memory"}
    when = {"role": "user", "_agent_injected": "context",
            "content": "[Context — current date/time, refreshed each turn; x]"}
    lang = {"role": "user", "_agent_injected": "context", "_reply_language": "es",
            "content": "[Runtime requirement — reply language] Spanish"}
    messages = [
        {"role": "system", "content": "prompt", "_agent_injected": "prompt"},
        {"role": "user", "content": "skills", "_agent_injected": "context"},
        when, lang,
        {"role": "user", "content": "the question"},
        {"role": "assistant", "content": "calling a tool"},
        {"role": "tool", "content": "tool output"},
    ]
    restored, inserted = al._restore_legacy_memory_fallback(messages, memory)
    assert inserted
    assert [m.get("content") for m in restored][1:5] == [
        "skills", MEMORY_TEXT, when["content"], lang["content"]]
    again, inserted_again = al._restore_legacy_memory_fallback(restored, memory)
    assert not inserted_again and again == restored, "never duplicated"
    unchanged, none = al._restore_legacy_memory_fallback(messages, None)
    assert not none and unchanged == messages


def _run_turn(monkeypatch, *, deliveries, rounds=(("Hecho.", "stop"),),
              stop_after_packet=False):
    """Run one agent turn with the live engine on and `deliver_round` scripted.

    Returns (messages sent per provider call, events)."""
    from tests.test_agent_harness_loop import _patch_common

    _patch_common(monkeypatch)
    monkeypatch.setattr(wiring, "enabled", lambda: True)
    monkeypatch.setattr(wiring, "shadow_enabled", lambda: False)
    sent = []
    calls = {"n": 0}

    async def fake_stream(_candidates, messages, **kwargs):
        sent.append([dict(m) for m in messages])
        index = min(calls["n"], len(rounds) - 1)
        calls["n"] += 1
        text, finish = rounds[index]
        yield f'data: {json.dumps({"delta": text})}\n\n'
        yield f'data: {json.dumps({"type": "finish", "finish_reason": finish})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", fake_stream, raising=False)
    script = list(deliveries)

    async def fake_deliver(**kwargs):
        outcome = script.pop(0) if script else None
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(wiring, "deliver_round", fake_deliver)
    gen = al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3.5:9b",
        [{"role": "user", "content": "Explica qué hace el fichero server.py"}],
        max_rounds=3, relevant_tools={"read_file"}, session_id="s-ada",
        owner="ada")

    async def consume():
        out = []
        async for chunk in gen:
            out.append(chunk)
            if stop_after_packet and '"type": "context_packet"' in chunk:
                break
        await gen.aclose()
        return out

    chunks = asyncio.run(consume())
    return sent, chunks


def _packet(packet_id="ctxpkt_live1"):
    return {
        "message": {"role": "user", "content": "compiled packet body",
                    "_agent_injected": "context_engine",
                    "metadata": {"trusted": False, "context_packet_id": packet_id}},
        "report": {"packet_id": packet_id, "request_id": "ctxreq_1", "delivered": True,
                   "sources": [], "recallable": []},
    }


def test_a_round_without_a_packet_keeps_the_legacy_memory(monkeypatch, learned_memory):
    sent, _ = _run_turn(monkeypatch, deliveries=[None])
    assert len(_memory_messages(sent[0])) == 1
    assert _packet_messages(sent[0]) == []
    assert learned_memory == [("s-ada", ["m-ada-1"])]


def test_a_crashing_delivery_keeps_the_legacy_memory(monkeypatch, learned_memory):
    sent, _ = _run_turn(monkeypatch, deliveries=[RuntimeError("store down")])
    assert len(_memory_messages(sent[0])) == 1
    assert learned_memory == [("s-ada", ["m-ada-1"])]


def test_a_delivered_packet_replaces_the_legacy_memory(monkeypatch, learned_memory):
    sent, _ = _run_turn(monkeypatch, deliveries=[_packet()])
    assert _memory_messages(sent[0]) == [], "never both"
    assert len(_packet_messages(sent[0])) == 1
    assert learned_memory == [], "the legacy ids were never sent"


def test_packet_then_no_packet_restores_the_memory_once(monkeypatch, learned_memory):
    """Round one gets a packet, round two (an auto-continue) gets none: the
    second provider call must carry the legacy block again, exactly once."""
    sent, _ = _run_turn(
        monkeypatch, deliveries=[_packet(), None, None],
        rounds=(("Esta es la primera mitad de la explicación del fichero", "length"),
                ("y este es el resto de la explicación.", "stop")))
    assert len(sent) >= 2
    assert _memory_messages(sent[0]) == [] and len(_packet_messages(sent[0])) == 1
    assert len(_memory_messages(sent[1])) == 1 and _packet_messages(sent[1]) == []
    assert learned_memory == [("s-ada", ["m-ada-1"])], "credited once per turn"


# ── 2. project_id comes from the runtime ───────────────────────────────────

@pytest.fixture()
def project_blocks(monkeypatch):
    seen = {"repos": [], "board": [], "resolved": []}
    monkeypatch.setattr(al, "_project_repos_block",
                        lambda owner, pid: seen["repos"].append(pid) or f"\n[repos:{pid}]")
    monkeypatch.setattr(al, "_project_board_block",
                        lambda owner, pid: seen["board"].append(pid) or f"\n[board:{pid}]")
    import services.projects as projects

    def resolve(session_id, owner):
        seen["resolved"].append(session_id)
        return {"id": "p-from-session"}

    monkeypatch.setattr(projects, "project_for_session", resolve)
    monkeypatch.setattr(wiring, "enabled", lambda: False)
    monkeypatch.setattr(memory_engine, "injection_enabled", lambda: False)
    return seen


def test_given_project_id_is_used_without_re_resolving(project_blocks):
    built = _build(owner="ada", session_id="s-1", project_id="p-cordera")
    prompt = next(m["content"] for m in built if m.get("role") == "system")
    assert "[repos:p-cordera]" in prompt and "[board:p-cordera]" in prompt
    assert project_blocks["resolved"] == []


def test_project_id_works_without_a_session(project_blocks):
    built = _build(owner="ada", project_id="p-cordera")
    prompt = next(m["content"] for m in built if m.get("role") == "system")
    assert "[repos:p-cordera]" in prompt and "[board:p-cordera]" in prompt


def test_without_project_id_the_session_is_resolved_once(project_blocks):
    built = _build(owner="ada", session_id="s-1")
    prompt = next(m["content"] for m in built if m.get("role") == "system")
    assert "[repos:p-from-session]" in prompt and "[board:p-from-session]" in prompt
    assert project_blocks["resolved"] == ["s-1"], "the board reuses the repos lookup"


def test_the_route_passes_the_harness_project_id():
    source = open(al.__file__, encoding="utf-8").read()
    call = source[source.index("route_messages, route_mcp_schemas = _build_system_prompt("):]
    assert 'project_id=str(_hopts.get("project_id") or "").strip() or None' in call[:1600]


# ── 3. history: persisted provider, scope wins ─────────────────────────────

@pytest.fixture()
def clean_provider():
    session_adapter.reset_history_provider()
    yield
    session_adapter.reset_history_provider()


def test_a_live_scope_wins_over_the_global_provider(clean_provider):
    session_adapter.set_history_provider(lambda sid, owner: [{"role": "user",
                                                              "content": "persisted"}])
    live = [{"role": "user", "content": "live"}]
    with session_adapter.history_scope("s-1", "ada", live):
        provider = session_adapter.history_provider()
        assert list(provider("s-1", "ada")) == live
        assert list(provider("s-1", "bruno")) == [], "scope mismatch is empty"
    provider = session_adapter.history_provider()
    assert list(provider("s-1", "ada")) == [{"role": "user", "content": "persisted"}]


def test_install_default_history_provider(clean_provider):
    assert session_adapter.history_provider() is None
    assert session_store.install_default_history_provider() is True
    assert session_adapter.history_provider() is session_store.persisted_history


@pytest.fixture()
def chat_db(monkeypatch):
    """A throwaway SQLite with the real `sessions`/`chat_messages` tables."""
    from datetime import datetime, timedelta

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import core.database as database

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    database.Base.metadata.create_all(
        engine, tables=[database.Session.__table__, database.ChatMessage.__table__])
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(database, "SessionLocal", factory)
    db = factory()
    start = datetime(2026, 1, 5, 9, 0, 0)
    for sid, owner in (("s-ada", "ada"), ("s-legacy", None)):
        db.add(database.Session(id=sid, name=sid, endpoint_url="http://x", model="m",
                                owner=owner))
    rows = [("user", "How do I deploy Bluehaven?"),
            ("assistant", "Run the release script."),
            ("tool", "tool noise"),
            ("user", "And roll back?")]
    for index, (role, content) in enumerate(rows):
        db.add(database.ChatMessage(id=f"m{index}", session_id="s-ada", role=role,
                                    content=content,
                                    timestamp=start + timedelta(minutes=index)))
    db.add(database.ChatMessage(id="legacy0", session_id="s-legacy", role="user",
                                content="shared note", timestamp=start))
    db.commit()
    db.close()
    return factory


def test_persisted_history_reads_the_owners_conversation_oldest_first(chat_db):
    rows = session_store.persisted_history("s-ada", "ada")
    assert [r["content"] for r in rows] == [
        "How do I deploy Bluehaven?", "Run the release script.", "And roll back?"]
    assert all(r["role"] in ("user", "assistant") for r in rows)
    assert rows[0]["timestamp"].startswith("2026-01-05T09:00")


def test_persisted_history_refuses_another_owner(chat_db):
    assert session_store.persisted_history("s-ada", "bruno") == ()
    assert session_store.persisted_history("s-missing", "ada") == ()
    assert session_store.persisted_history("", "ada") == ()


def test_persisted_history_follows_the_packet_owner_rule(chat_db):
    # empty caller = single-user mode; owner-less session = install-wide
    assert len(session_store.persisted_history("s-ada", "")) == 3
    assert [r["content"] for r in session_store.persisted_history("s-legacy", "bruno")] == [
        "shared note"]


def test_persisted_history_respects_the_limit(chat_db):
    rows = session_store.persisted_history("s-ada", "ada", limit=2)
    assert [r["content"] for r in rows] == ["Run the release script.", "And roll back?"]


def test_the_session_source_uses_the_persisted_history(chat_db, clean_provider):
    from src.context_engine.adapters.sessions import SessionSource
    from src.context_engine.candidates import RetrievalRequest

    session_store.install_default_history_provider()
    request = wiring.build_request(owner="ada", session_id="s-ada", model="m",
                                   messages=[{"role": "user", "content": "roll back?"}])
    source = SessionSource()
    assert source.available()
    found = source._search(RetrievalRequest(request=request, limit=10))
    assert [c.body for c in found][-1] == "And roll back?"
    assert all(c.source_ref.startswith("session:s-ada#") for c in found)


def test_the_app_installs_the_provider_at_startup():
    source = open("app.py", encoding="utf-8").read()
    assert "install_default_history_provider()" in source


# ── 4. receipts ────────────────────────────────────────────────────────────

@pytest.fixture()
def receipts(monkeypatch):
    seen = []
    monkeypatch.setattr(wiring, "observe_receipt", lambda **kw: seen.append(kw))
    return seen


def test_every_delivered_packet_is_receipted_once_at_turn_end(monkeypatch, receipts,
                                                              learned_memory):
    sent, _ = _run_turn(
        monkeypatch, deliveries=[_packet("ctxpkt_a"), _packet("ctxpkt_b")],
        rounds=(("Esta es la primera mitad de la explicación del fichero", "length"),
                ("y este es el resto de la explicación.", "stop")))
    assert [r["packet_id"] for r in receipts] == ["ctxpkt_a", "ctxpkt_b"]
    assert all(r["messages"] for r in receipts), "the turn's messages ride along"
    assert all(r["verdict"] for r in receipts)


def test_a_stopped_stream_still_receipts_what_it_delivered(monkeypatch, receipts,
                                                           learned_memory):
    _run_turn(monkeypatch, deliveries=[_packet("ctxpkt_stop")], stop_after_packet=True)
    assert [r["packet_id"] for r in receipts] == ["ctxpkt_stop"]
    assert receipts[0]["verdict"] == "interrupted"


def test_no_packet_no_receipt(monkeypatch, receipts, learned_memory):
    _run_turn(monkeypatch, deliveries=[None])
    assert receipts == []


def test_terminal_error_returns_flush_receipts_first():
    source = open(al.__file__, encoding="utf-8").read()
    assert source.count('_flush_context_receipts(verdict="error")') == 2
    assert "_flush_context_receipts(hsum=_hsum)" in source
