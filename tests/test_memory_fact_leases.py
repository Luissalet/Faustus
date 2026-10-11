"""Facts with a lease (src/memory_leases.py): the personal memory stops asserting
what nobody has confirmed for a while.

Covered, in the order that matters: the kind of a fact and the lease it gets,
an expired fact NOT reaching the prompt as if it were current, renew / forget,
new evidence of the same kind ending the old lease, and every fact written
before leases existed behaving exactly as before.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import mcp_servers.memory_server as memory_server
import routes.memory_routes as memory_routes
from src import ai_interaction, memory_leases
from src.chat_processor import ChatProcessor
from src.memory import MemoryManager

DAY = 86400
T0 = int(datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc).timestamp())


@pytest.fixture(autouse=True)
def _default_settings(monkeypatch):
    """Leases on, the shipped defaults, whatever the developer's settings say."""
    values = {"memory_leases_enabled": True, "memory_lease_days": {}, "memory_lease_review_window_days": 7}
    monkeypatch.setattr(memory_leases, "_setting", lambda key, default=None: values.get(key, default))
    monkeypatch.setattr("src.user_time.user_timezone", lambda: timezone.utc)   # the "as of" day is local; pin it
    return values


def _manager(tmp_path):
    return MemoryManager(str(tmp_path))


def _add(manager, text, *, owner="alice", category="fact", at=T0, **kwargs):
    """add_entry + save, with the clock pinned to ``at``."""
    entries = manager.load_all()
    entry = manager.add_entry(text, owner=owner, category=category, **kwargs)
    entry["timestamp"] = at
    if entry.get("lease_until"):
        entry["confirmed_at"] = at
        entry["lease_until"] = at + int(entry["lease_days"]) * DAY
    entries.append(entry)
    manager.save(entries)
    return next(e for e in manager.load_all() if e["id"] == entry["id"])


# ── the kind of a fact ────────────────────────────────────────────────────


@pytest.mark.parametrize("text,kind", [
    ("User lives in Madrid.", "location"),
    ("Vive en Barcelona", "location"),
    ("User is currently in Lisbon", "location"),
    ("User works at Acme Corp", "job"),
    ("User works as a teacher", "job"),
    ("Trabaja como ingeniero", "job"),
    ("User is working on a novel", "project"),
    ("Estoy trabajando en el proyecto Hoard", "project"),
    ("El usuario esta de vacaciones", "status"),
    ("User is on holiday", "status"),
    ("User prefers tea", "preference"),
    ("User prefers living in Madrid", "preference"),
    ("User lives in Madrid and likes tea", "location"),
    ("User wants to be called Sam", ""),
    ("User uses FastAPI", ""),
    ("Ana lives in Paris", ""),
    ("My brother lives in Berlin", ""),
    ("She works at a bank", ""),
])
def test_classify_kind(text, kind):
    assert memory_leases.classify_kind(text) == kind


def test_a_contact_is_never_given_a_kind():
    assert memory_leases.classify_kind("Lives in Paris", "contact") == ""


# ── lease assignment ──────────────────────────────────────────────────────


@pytest.mark.parametrize("text,kind,days", [
    ("User lives in Madrid", "location", 30),
    ("User is on holiday", "status", 30),
    ("User is working on a novel", "project", 90),
    ("User works at Acme Corp", "job", 365),
])
def test_each_kind_gets_its_lease(tmp_path, text, kind, days):
    entry = _manager(tmp_path).add_entry(text)
    assert entry["kind"] == kind
    assert entry["lease_days"] == days
    assert entry["lease_until"] - entry["confirmed_at"] == days * DAY


def test_a_preference_has_a_kind_but_no_lease(tmp_path):
    entry = _manager(tmp_path).add_entry("User prefers tea", category="preference")
    assert entry["kind"] == "preference"
    assert "lease_until" not in entry and "lease_days" not in entry


def test_an_unknown_kind_gets_no_lease_fields_at_all(tmp_path):
    """Backward compatible: the entry is byte-for-byte what it was before leases."""
    entry = _manager(tmp_path).add_entry("User uses FastAPI")
    assert set(entry) == {"id", "text", "timestamp", "source", "category", "uses"}


def test_settings_override_the_days_by_kind(tmp_path, _default_settings):
    _default_settings["memory_lease_days"] = {"location": 10, "job": 0}
    manager = _manager(tmp_path)
    assert manager.add_entry("User lives in Madrid")["lease_days"] == 10
    job = manager.add_entry("User works at Acme Corp")
    assert job["kind"] == "job" and "lease_until" not in job      # 0 = no lease


def test_the_switch_turns_the_whole_feature_off(tmp_path, _default_settings):
    _default_settings["memory_leases_enabled"] = False
    manager = _manager(tmp_path)
    entry = manager.add_entry("User lives in Madrid")
    assert set(entry) == {"id", "text", "timestamp", "source", "category", "uses"}
    stale = {"text": "x", "kind": "location", "lease_until": 1, "closed_at": 5}
    assert memory_leases.prompt_view([stale]) == [stale]            # nothing hidden, nothing marked


def test_explicit_kind_and_days_win_over_the_rules(tmp_path):
    manager = _manager(tmp_path)
    entry = manager.add_entry("User uses FastAPI", kind="project", lease_days=14)
    assert (entry["kind"], entry["lease_days"]) == ("project", 14)
    assert "lease_until" not in manager.add_entry("User lives in Madrid", lease_days=0)
    with pytest.raises(ValueError):
        manager.add_entry("whatever", kind="mood")
    with pytest.raises(ValueError):
        manager.add_entry("whatever", kind="job", lease_days=-1)


# ── lease state ───────────────────────────────────────────────────────────


def test_state_walks_from_active_to_expiring_to_expired(tmp_path):
    entry = _add(_manager(tmp_path), "User lives in Madrid")             # 30 days
    state = lambda days: memory_leases.state(entry, T0 + days * DAY)["state"]   # noqa: E731
    assert state(1) == "active"
    assert state(22) == "active"
    assert state(23.5) == "expiring"                                     # inside the 7-day window
    assert state(29.99) == "expiring"
    assert state(30) == "expired"
    assert memory_leases.state(entry, T0 + 35 * DAY)["days_left"] == pytest.approx(-5)


def test_a_fact_without_a_lease_is_state_none_forever(tmp_path):
    entry = _add(_manager(tmp_path), "User uses FastAPI")
    assert memory_leases.state(entry, T0 + 9999 * DAY)["state"] == "none"


# ── an expired fact is not injected as if it were current ─────────────────


class _Docs:
    rag_manager = None


class _Rows:
    def __init__(self, rows):
        self.rows = rows
        self.incremented = []

    def load(self, owner=None):
        return list(self.rows)

    def increment_uses(self, ids):
        self.incremented.extend(ids)


# Unrelated facts: the relevance scorer needs a corpus to tell a match from noise.
_FILLER = [
    {"id": "f1", "text": "User plays chess on weekends", "category": "fact", "timestamp": 1, "uses": 0},
    {"id": "f2", "text": "User keeps a blue bicycle in the garage", "category": "fact", "timestamp": 1, "uses": 0},
    {"id": "f3", "text": "User reads science fiction novels", "category": "fact", "timestamp": 1, "uses": 0},
]


def _injected(rows, message):
    processor = ChatProcessor(memory_manager=_Rows([*rows, *_FILLER]), personal_docs_manager=_Docs())
    preface, _, _ = processor.build_context_preface(
        message=message, session=SimpleNamespace(), use_rag=False, use_memory=True)
    return "\n".join(m.get("content", "") for m in preface)


def _row(id_, text, **extra):
    return {"id": id_, "text": text, "category": "fact", "timestamp": 1, "uses": 0, **extra}


def test_an_expired_fact_goes_into_the_prompt_marked_as_possibly_outdated():
    long_ago = int(datetime.now(timezone.utc).timestamp()) - 90 * DAY
    stale = _row("loc", "User lives in Madrid", kind="location", lease_days=30,
                 confirmed_at=long_ago, lease_until=long_ago + 30 * DAY)
    text = _injected([stale], "where does the user live in Madrid")
    as_of = datetime.fromtimestamp(long_ago, tz=timezone.utc).strftime("%Y-%m-%d")
    assert f"User lives in Madrid (as of {as_of}, may be outdated)" in text


def test_a_fact_inside_its_lease_is_injected_clean():
    now = int(datetime.now(timezone.utc).timestamp())
    fresh = _row("loc", "User lives in Madrid", kind="location", lease_days=30,
                 confirmed_at=now, lease_until=now + 30 * DAY)
    text = _injected([fresh], "where does the user live in Madrid")
    assert "User lives in Madrid" in text
    assert "may be outdated" not in text


def test_a_replaced_fact_is_not_injected_at_all():
    now = int(datetime.now(timezone.utc).timestamp())
    old = _row("old", "User lives in Madrid", kind="location", lease_days=30, confirmed_at=now,
               lease_until=now + 30 * DAY, closed_at=now, superseded_by="new")
    new = _row("new", "User lives in Berlin", kind="location", lease_days=30, confirmed_at=now,
               lease_until=now + 30 * DAY)
    text = _injected([old, new], "is it Berlin or Madrid")
    assert "Berlin" in text
    assert "Madrid" not in text


def test_a_fact_from_before_leases_is_injected_exactly_as_before():
    legacy = _row("old", "User lives in Madrid", timestamp=1)             # years ago, no kind
    text = _injected([legacy], "where does the user live in Madrid")
    assert "User lives in Madrid" in text
    assert "outdated" not in text


def test_prompt_view_returns_the_same_objects_for_facts_it_does_not_touch(tmp_path):
    plain = _add(_manager(tmp_path), "User uses FastAPI")
    assert memory_leases.prompt_view([plain])[0] is plain


def test_prompt_view_never_mutates_the_stored_entry(tmp_path):
    entry = _add(_manager(tmp_path), "User lives in Madrid")
    view = memory_leases.prompt_view([entry], now=T0 + 99 * DAY)
    assert view[0]["text"].endswith("may be outdated)") and view[0]["stale"] is True
    assert entry["text"] == "User lives in Madrid" and "stale" not in entry


def test_the_context_engine_source_marks_and_drops_the_same_way(tmp_path):
    from src.context_engine import candidates as C
    from src.context_engine.adapters import memory as ad_memory
    from tests.test_context_engine_sources import CountingMemoryManager, make_request

    now = int(datetime.now(timezone.utc).timestamp())
    store = CountingMemoryManager()
    store.entries = [
        {"id": "e1", "text": "the user lives in Madrid", "source": "user", "category": "fact", "owner": "alice",
         "timestamp": now - 90 * DAY, "kind": "location", "lease_days": 30,
         "confirmed_at": now - 90 * DAY, "lease_until": now - 60 * DAY},
        {"id": "e2", "text": "the user lived in Paris", "source": "user", "category": "fact", "owner": "alice",
         "timestamp": now - 90 * DAY, "closed_at": now - 80 * DAY, "superseded_by": "e1"},
    ]
    results = asyncio.run(C.gather([ad_memory.PersonalMemorySource(manager=store)],
                                   make_request(owner="alice", query="live")))
    bodies = [c.body for c in results[0].candidates]
    assert len(bodies) == 1 and "may be outdated" in bodies[0] and "Madrid" in bodies[0]


# ── renew ────────────────────────────────────────────────────────────────


def test_renew_restarts_the_lease_from_now(tmp_path):
    entry = _add(_manager(tmp_path), "User lives in Madrid")
    now = T0 + 60 * DAY                                                  # long expired
    assert memory_leases.state(entry, now)["state"] == "expired"
    memory_leases.renew(entry, now=now)
    assert memory_leases.state(entry, now)["state"] == "active"
    assert entry["lease_until"] == now + 30 * DAY and entry["confirmed_at"] == now


def test_renew_can_give_a_new_length(tmp_path):
    entry = _add(_manager(tmp_path), "User lives in Madrid")
    memory_leases.renew(entry, 90, now=T0)
    assert entry["lease_days"] == 90 and entry["lease_until"] == T0 + 90 * DAY
    memory_leases.renew(entry, now=T0 + DAY)                            # keeps the length it was given
    assert entry["lease_until"] == T0 + DAY + 90 * DAY


def test_renew_refuses_a_fact_that_has_no_lease(tmp_path):
    entry = _add(_manager(tmp_path), "User uses FastAPI")
    with pytest.raises(ValueError, match="no lease"):
        memory_leases.renew(entry)
    memory_leases.renew(entry, 10, now=T0)                              # unless it is given one
    assert entry["lease_until"] == T0 + 10 * DAY
    with pytest.raises(ValueError):
        memory_leases.renew(entry, 0)


def test_the_expired_review_list_shrinks_after_a_renew(tmp_path):
    manager = _manager(tmp_path)
    _add(manager, "User lives in Madrid")
    _add(manager, "User is on holiday")
    now = T0 + 40 * DAY
    report = memory_leases.review(manager.load_all(), now)
    assert report["counts"] == {"expired": 2, "expiring": 0, "closed": 0}
    entries = manager.load_all()
    memory_leases.renew(next(e for e in entries if e["kind"] == "location"), now=now)
    assert memory_leases.review(entries, now)["counts"]["expired"] == 1


# ── new evidence ends the old lease ──────────────────────────────────────


def test_a_newer_location_closes_the_older_one(tmp_path):
    manager = _manager(tmp_path)
    old = _add(manager, "User lives in Madrid", at=T0)
    new = _add(manager, "User lives in Berlin", at=T0 + 10 * DAY)
    stored = {e["id"]: e for e in manager.load_all()}
    assert stored[old["id"]]["closed_at"] == T0 + 10 * DAY
    assert stored[old["id"]]["superseded_by"] == new["id"]
    assert "closed_at" not in stored[new["id"]]
    assert memory_leases.state(stored[old["id"]], T0 + 11 * DAY)["state"] == "closed"
    assert [e["text"] for e in memory_leases.prompt_view(manager.load_all(), T0 + 11 * DAY)] == ["User lives in Berlin"]


def test_a_closed_fact_stays_in_the_store_as_history(tmp_path):
    manager = _manager(tmp_path)
    _add(manager, "User lives in Madrid", at=T0)
    _add(manager, "User lives in Berlin", at=T0 + DAY)
    assert {e["text"] for e in manager.load_all()} == {"User lives in Madrid", "User lives in Berlin"}


def test_forgetting_the_newer_fact_reopens_the_older_one(tmp_path):
    manager = _manager(tmp_path)
    old = _add(manager, "User lives in Madrid", at=T0)
    new = _add(manager, "User lives in Berlin", at=T0 + DAY)
    manager.save([e for e in manager.load_all() if e["id"] != new["id"]])
    reopened = next(e for e in manager.load_all() if e["id"] == old["id"])
    assert "closed_at" not in reopened and "superseded_by" not in reopened


def test_a_preference_is_not_single_valued(tmp_path):
    manager = _manager(tmp_path)
    _add(manager, "User prefers tea", category="preference")
    _add(manager, "User prefers dark roast coffee", category="preference", at=T0 + DAY)
    assert not any(e.get("closed_at") for e in manager.load_all())


def test_a_project_is_not_single_valued(tmp_path):
    manager = _manager(tmp_path)
    _add(manager, "User is working on a novel")
    _add(manager, "User is working on a game", at=T0 + DAY)
    assert not any(e.get("closed_at") for e in manager.load_all())


def test_employer_and_role_are_two_slots(tmp_path):
    manager = _manager(tmp_path)
    _add(manager, "User works at Acme Corp")
    _add(manager, "User works as a teacher", at=T0 + DAY)
    assert not any(e.get("closed_at") for e in manager.load_all())
    _add(manager, "User works at Globex", at=T0 + 2 * DAY)
    closed = [e["text"] for e in manager.load_all() if e.get("closed_at")]
    assert closed == ["User works at Acme Corp"]


def test_two_owners_never_close_each_others_facts(tmp_path):
    manager = _manager(tmp_path)
    _add(manager, "User lives in Madrid", owner="alice")
    _add(manager, "User lives in Berlin", owner="bob", at=T0 + DAY)
    assert not any(e.get("closed_at") for e in manager.load_all())


def test_facts_without_a_kind_are_never_closed(tmp_path):
    manager = _manager(tmp_path)
    _add(manager, "User uses FastAPI")
    _add(manager, "User uses Flask", at=T0 + DAY)
    assert not any(e.get("closed_at") for e in manager.load_all())


def test_renewing_the_older_fact_makes_it_the_current_one(tmp_path):
    manager = _manager(tmp_path)
    old = _add(manager, "User lives in Madrid", at=T0)
    new = _add(manager, "User lives in Berlin", at=T0 + DAY)
    entries = manager.load_all()
    memory_leases.renew(next(e for e in entries if e["id"] == old["id"]), now=T0 + 5 * DAY)
    manager.save(entries)
    stored = {e["id"]: e for e in manager.load_all()}
    assert "closed_at" not in stored[old["id"]]
    assert stored[new["id"]]["superseded_by"] == old["id"]


def test_an_edit_is_new_evidence_and_restarts_the_lease(tmp_path):
    manager = _manager(tmp_path)
    entry = _add(manager, "User lives in Madrid")
    entry["text"] = "User works at Acme Corp"
    memory_leases.reassign(entry, now=T0 + 3 * DAY)
    assert (entry["kind"], entry["lease_days"]) == ("job", 365)
    assert entry["lease_until"] == T0 + 3 * DAY + 365 * DAY


# ── the API ──────────────────────────────────────────────────────────────


def _routes(monkeypatch, manager, user="alice"):
    monkeypatch.setattr(memory_routes, "get_current_user", lambda request: user)
    router = memory_routes.setup_memory_routes(manager, MagicMock())

    def find(path, method):
        return next(r.endpoint for r in router.routes if r.path == path and method in r.methods)
    return find


def test_the_review_endpoint_lists_expired_expiring_and_replaced(monkeypatch, tmp_path):
    manager = _manager(tmp_path)
    now = int(datetime.now(timezone.utc).timestamp())
    _add(manager, "User is on holiday", at=now - 40 * DAY)               # expired
    _add(manager, "User is working on a novel", at=now - 85 * DAY)       # 90-day lease: expiring
    _add(manager, "User lives in Madrid", at=now - 20 * DAY)
    _add(manager, "User lives in Berlin", at=now - 10 * DAY)             # closes Madrid
    _add(manager, "User uses FastAPI", at=now - 1000 * DAY)              # no lease: not listed
    find = _routes(monkeypatch, manager)
    report = find("/api/memory/leases", "GET")(request=None, window_days=None)
    assert report["counts"] == {"expired": 1, "expiring": 1, "closed": 1}
    assert report["expired"][0]["text"] == "User is on holiday"
    assert report["expiring"][0]["text"] == "User is working on a novel"
    assert report["closed"][0]["text"] == "User lives in Madrid"
    assert report["settings"]["lease_days"]["location"] == 30


def test_the_review_endpoint_is_owner_scoped(monkeypatch, tmp_path):
    manager = _manager(tmp_path)
    now = int(datetime.now(timezone.utc).timestamp())
    _add(manager, "User is on holiday", owner="bob", at=now - 40 * DAY)
    find = _routes(monkeypatch, manager, user="alice")
    assert find("/api/memory/leases", "GET")(request=None, window_days=None)["counts"]["expired"] == 0


def test_renew_endpoint_and_forget_endpoint(monkeypatch, tmp_path):
    manager = _manager(tmp_path)
    now = int(datetime.now(timezone.utc).timestamp())
    stale = _add(manager, "User is on holiday", at=now - 40 * DAY)
    gone = _add(manager, "User lives in Madrid", at=now - 50 * DAY)
    find = _routes(monkeypatch, manager)

    renewed = find("/api/memory/leases/{memory_id}/renew", "POST")(request=None, memory_id=stale["id"], days=None)
    assert renewed["ok"] and renewed["lease"]["state"] == "active"
    forgot = find("/api/memory/leases/{memory_id}/forget", "POST")(request=None, memory_id=gone["id"])
    assert forgot["ok"]

    report = find("/api/memory/leases", "GET")(request=None, window_days=None)
    assert report["counts"] == {"expired": 0, "expiring": 0, "closed": 0}
    assert [e["id"] for e in manager.load_all()] == [stale["id"]]


def test_renew_endpoint_errors(monkeypatch, tmp_path):
    from fastapi import HTTPException
    manager = _manager(tmp_path)
    plain = _add(manager, "User uses FastAPI")
    other = _add(manager, "User is on holiday", owner="bob")
    renew = _routes(monkeypatch, manager)("/api/memory/leases/{memory_id}/renew", "POST")
    with pytest.raises(HTTPException) as no_lease:
        renew(request=None, memory_id=plain["id"], days=None)
    assert no_lease.value.status_code == 400
    with pytest.raises(HTTPException) as missing:
        renew(request=None, memory_id="nope", days=None)
    assert missing.value.status_code == 404
    with pytest.raises(HTTPException) as foreign:
        renew(request=None, memory_id=other["id"], days=None)
    assert foreign.value.status_code == 404


def test_the_memory_list_carries_the_lease_only_for_facts_that_have_one(monkeypatch, tmp_path):
    manager = _manager(tmp_path)
    _add(manager, "User lives in Madrid")
    _add(manager, "User uses FastAPI")
    rows = _routes(monkeypatch, manager)("/api/memory", "GET")(request=None)["memory"]
    by_text = {r["text"]: r for r in rows}
    assert by_text["User lives in Madrid"]["lease"]["kind"] == "location"
    assert "lease" not in by_text["User uses FastAPI"]


def test_add_endpoint_gives_a_lease_and_saying_it_again_renews_it(monkeypatch, tmp_path):
    from src.request_models import MemoryAddRequest
    manager = _manager(tmp_path)
    find = _routes(monkeypatch, manager)
    add = find("/api/memory/add", "POST")
    monkeypatch.setattr("src.auth_helpers.require_privilege", lambda *a, **k: None)

    first = asyncio.run(add(request=None, memory_data=MemoryAddRequest(text="User lives in Madrid")))
    assert first["lease"]["kind"] == "location"
    stored = manager.load_all()[0]
    stored["lease_until"] -= 25 * DAY                                    # time passes
    stored["confirmed_at"] -= 25 * DAY
    manager.save([stored])
    before = manager.load_all()[0]["lease_until"]

    again = asyncio.run(add(request=None, memory_data=MemoryAddRequest(text="User lives in Madrid")))
    assert again["message"] == "Memory already exists" and again["renewed"] is True
    assert manager.load_all()[0]["lease_until"] > before

    explicit = asyncio.run(add(request=None, memory_data=MemoryAddRequest(text="Bakes on Sundays", kind="status", lease_days=3)))
    assert explicit["lease"]["kind"] == "status"


def test_add_endpoint_rejects_an_unknown_kind(monkeypatch, tmp_path):
    from fastapi import HTTPException
    from src.request_models import MemoryAddRequest
    add = _routes(monkeypatch, _manager(tmp_path))("/api/memory/add", "POST")
    monkeypatch.setattr("src.auth_helpers.require_privilege", lambda *a, **k: None)
    with pytest.raises(HTTPException) as err:
        asyncio.run(add(request=None, memory_data=MemoryAddRequest(text="x", kind="mood")))
    assert err.value.status_code == 400


def test_update_endpoint_restarts_the_lease(monkeypatch, tmp_path):
    manager = _manager(tmp_path)
    entry = _add(manager, "User lives in Madrid")
    update = _routes(monkeypatch, manager)("/api/memory/{memory_id}", "PUT")
    update(request=None, memory_id=entry["id"], text="User works at Acme Corp", category=None)
    stored = manager.load_all()[0]
    assert stored["kind"] == "job" and stored["lease_days"] == 365


# ── the agent tool and the MCP server ────────────────────────────────────


def test_the_agent_tool_renews_when_the_same_fact_is_said_again(monkeypatch, tmp_path):
    manager = _manager(tmp_path)
    stale = _add(manager, "User lives in Madrid", owner="alice")
    stale["lease_until"] = 1_000_000                                     # long expired
    stale["confirmed_at"] = 1_000
    manager.save([stale])
    monkeypatch.setattr(ai_interaction, "_memory_manager", manager)
    result = asyncio.run(ai_interaction.do_manage_memory("add\nUser lives in Madrid", owner="alice"))
    assert result["duplicate"] is True and result["renewed"] is True
    assert memory_leases.state(manager.load_all()[0])["state"] == "active"


def _server(monkeypatch, manager):
    monkeypatch.setattr(memory_server, "_memory_manager", manager)
    monkeypatch.setattr(memory_server, "_memory_vector", None)
    monkeypatch.setattr(memory_server, "_initialized", True)
    for key in memory_server._OWNER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ODYSSEUS_MCP_MEMORY_OWNER", "alice")


def _call(name, arguments):
    return asyncio.run(memory_server.call_tool(name, arguments))[0].text


def test_mcp_exposes_the_lease_tool():
    names = [t.name for t in asyncio.run(memory_server.list_tools())]
    assert names == ["manage_memory", "memory_leases"]


def test_mcp_review_renew_forget(monkeypatch, tmp_path):
    manager = _manager(tmp_path)
    now = int(datetime.now(timezone.utc).timestamp())
    a = _add(manager, "User is on holiday", at=now - 40 * DAY)
    b = _add(manager, "User lives in Madrid", at=now - 50 * DAY)
    _server(monkeypatch, manager)

    review = _call("memory_leases", {"action": "review"})
    assert "2 expired" in review and a["id"][:8] in review and b["id"][:8] in review

    assert "Lease renewed" in _call("memory_leases", {"action": "renew", "memory_id": a["id"][:8]})
    assert "Memory forgotten" in _call("memory_leases", {"action": "forget", "memory_id": b["id"][:8]})
    assert _call("memory_leases", {"action": "review"}) == "No memory leases to review."
    assert [e["id"] for e in manager.load_all()] == [a["id"]]
    assert "Error" in _call("memory_leases", {"action": "renew", "memory_id": "zzzz"})
    assert "Error" in _call("memory_leases", {"action": "dance"})


def test_mcp_list_marks_an_expired_fact_and_add_reports_the_lease(monkeypatch, tmp_path):
    manager = _manager(tmp_path)
    now = int(datetime.now(timezone.utc).timestamp())
    _add(manager, "User lives in Madrid", at=now - 90 * DAY)
    _server(monkeypatch, manager)
    assert "may be outdated" in _call("manage_memory", {"action": "list"})
    assert "may be outdated" in _call("manage_memory", {"action": "search", "text": "Madrid"})
    added = _call("manage_memory", {"action": "add", "text": "User works at Acme Corp"})
    assert "[job, lease 365 days]" in added
    assert "Error" in _call("manage_memory", {"action": "add", "text": "x", "kind": "mood"})


# ══════════════════════════════════════════════════════════════════════════
# Review round: third parties and vague sentences never close the user's facts
# ══════════════════════════════════════════════════════════════════════════

THIRD_PARTY_PLACE = [
    "User's sister lives in Berlin.",
    "Luis's sister lives in Berlin",
    "The user's brother lives in Paris",
    "El hermano del usuario vive en Berlin",
    "Su hermana vive en Berlin",
    "La hermana de Luis vive en Berlin",
    "User's mother lives in Lima",
]
THIRD_PARTY_JOB = [
    "User's wife works at Google",
    "Su hermana trabaja en Google",
    "The user's boss works at Initech",
    "El marido del usuario trabaja en Google",
]
VAGUE_PLACE = [
    "User lives in the moment",
    "User lives in constant fear of deadlines",
    "User stays at hotels when travelling",
    "User dreams of moving to Berlin",
]
VAGUE_JOB = [
    "User works at night",
    "User works at home on Fridays",
    "El usuario trabaja en Faustus por las noches",
    "User works at Globex on weekends",
]


@pytest.mark.parametrize("text", THIRD_PARTY_PLACE + THIRD_PARTY_JOB + VAGUE_PLACE + VAGUE_JOB)
def test_these_sentences_get_no_single_valued_kind_and_no_slot(text):
    kind = memory_leases.classify_kind(text)
    assert kind not in memory_leases.SINGLE_VALUED
    # Even if somebody forces the kind by hand, the slot refuses to compete.
    assert memory_leases.slot_of("location", text) == ""
    assert memory_leases.slot_of("job", text) == ""


@pytest.mark.parametrize("text,kind,slot", [
    ("User lives in Madrid", "location", "location"),
    ("I live in Berlin", "location", "location"),
    ("Lives in Madrid", "location", "location"),
    ("Vivo en Madrid", "location", "location"),
    ("El usuario vive en Madrid", "location", "location"),
    ("User moved to Lisbon last year", "location", "location"),
    ("User works at Acme Corp", "job", "job:employer"),
    ("El usuario trabaja en Acme", "job", "job:employer"),
    ("User works as a teacher", "job", "job:role"),
    ("User's job title is Engineer", "job", "job:role"),
])
def test_real_statements_about_the_user_keep_their_kind_and_slot(text, kind, slot):
    assert memory_leases.classify_kind(text) == kind
    assert memory_leases.slot_of(kind, text) == slot


@pytest.mark.parametrize("text", THIRD_PARTY_PLACE + VAGUE_PLACE + ["User is currently in Berlin"])
def test_a_third_party_vague_or_temporary_place_never_closes_where_the_user_lives(tmp_path, text):
    manager = _manager(tmp_path)
    home = _add(manager, "User lives in Madrid", at=T0)
    _add(manager, text, at=T0 + DAY)
    stored = {e["id"]: e for e in manager.load_all()}
    assert "closed_at" not in stored[home["id"]]
    shown = [e["text"] for e in memory_leases.prompt_view(manager.load_all(), T0 + 2 * DAY)]
    assert "User lives in Madrid" in shown


@pytest.mark.parametrize("text", THIRD_PARTY_JOB + VAGUE_JOB)
def test_a_third_party_or_side_activity_never_closes_the_users_job(tmp_path, text):
    manager = _manager(tmp_path)
    job = _add(manager, "User works at Acme Corp", at=T0)
    _add(manager, text, at=T0 + DAY)
    stored = {e["id"]: e for e in manager.load_all()}
    assert "closed_at" not in stored[job["id"]]
    assert "User works at Acme Corp" in [e["text"] for e in memory_leases.prompt_view(manager.load_all(), T0 + 2 * DAY)]


@pytest.mark.parametrize("old,new", [
    ("User lives in Madrid", "User lives in Berlin"),
    ("I live in Madrid", "I live in Berlin"),
    ("Vivo en Madrid", "Vivo en Berlin"),
    ("El usuario vive en Madrid", "El usuario vive en Berlin"),
    ("Lives in Madrid", "User lives in Berlin"),
    ("User works at Acme Corp", "User works at Globex"),
    ("El usuario trabaja en Acme", "El usuario trabaja en Globex"),
    ("User works as a teacher", "User works as a nurse"),
])
def test_real_new_evidence_about_the_user_still_closes_the_old_fact(tmp_path, old, new):
    manager = _manager(tmp_path)
    first = _add(manager, old, at=T0)
    second = _add(manager, new, at=T0 + DAY)
    stored = {e["id"]: e for e in manager.load_all()}
    assert stored[first["id"]]["superseded_by"] == second["id"]
    assert "closed_at" not in stored[second["id"]]


def test_a_third_party_fact_through_the_add_endpoint_leaves_madrid_in_the_prompt(monkeypatch, tmp_path):
    from src.request_models import MemoryAddRequest
    manager = _manager(tmp_path)
    find = _routes(monkeypatch, manager)
    add = find("/api/memory/add", "POST")
    monkeypatch.setattr("src.auth_helpers.require_privilege", lambda *a, **k: None)
    asyncio.run(add(request=None, memory_data=MemoryAddRequest(text="User lives in Madrid")))
    for text in ("User's sister lives in Berlin.", "Su hermana vive en Berlin", "User's wife works at Google"):
        asyncio.run(add(request=None, memory_data=MemoryAddRequest(text=text)))
    rows = manager.load(owner="alice")
    assert all("closed_at" not in e for e in rows)
    assert "User lives in Madrid" in _injected(rows, "where does the user live in Madrid")


# ── status is not a single slot ───────────────────────────────────────────


@pytest.mark.parametrize("a,b", [
    ("User currently uses VS Code", "User currently drinks tea"),
    ("User is on vacation", "User is currently learning Rust"),
    ("User is on holiday", "User is sick"),
])
def test_a_status_never_closes_another_status(tmp_path, a, b):
    assert "status" not in memory_leases.SINGLE_VALUED
    manager = _manager(tmp_path)
    first = _add(manager, a, at=T0)
    second = _add(manager, b, at=T0 + DAY)
    assert first["kind"] == second["kind"] == "status"
    assert all("closed_at" not in e for e in manager.load_all())
    assert len(memory_leases.prompt_view(manager.load_all(), T0 + 2 * DAY)) == 2


def test_a_status_still_expires_like_before(tmp_path):
    manager = _manager(tmp_path)
    _add(manager, "User is on holiday", at=T0)
    view = memory_leases.prompt_view(manager.load_all(), T0 + 40 * DAY)
    assert view[0]["text"].startswith("User is on holiday (as of ")


@pytest.mark.parametrize("text", ["User is sick of Python", "User stays at hotels when travelling",
                                  "User dreams of moving to Berlin", "User lives in the moment"])
def test_the_vague_sentences_get_no_kind_at_all(text):
    assert memory_leases.classify_kind(text) == ""


# ── lease length cap, local dates, switch ─────────────────────────────────


def test_a_lease_longer_than_ten_years_is_refused_everywhere(tmp_path, _default_settings):
    from pydantic import ValidationError
    from src.request_models import MemoryAddRequest
    with pytest.raises(ValueError):
        _manager(tmp_path).add_entry("User lives in Madrid", lease_days=3651)
    assert _manager(tmp_path).add_entry("User lives in Madrid", lease_days=3650)["lease_days"] == 3650
    with pytest.raises(ValidationError):
        MemoryAddRequest(text="x", lease_days=3651)
    entry = _add(_manager(tmp_path), "User works at Acme Corp")
    with pytest.raises(ValueError):
        memory_leases.renew(entry, 3651)
    _default_settings["memory_lease_days"] = {"job": 99999}
    assert memory_leases.lease_days_for("job") == 3650


def test_the_as_of_date_is_the_users_local_day(monkeypatch):
    from datetime import timedelta
    noon_utc = int(datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc).timestamp())
    entry = {"text": "User lives in Madrid", "confirmed_at": noon_utc}
    monkeypatch.setattr("src.user_time.user_timezone", lambda: timezone(timedelta(hours=14)))
    assert memory_leases.marked_text(entry) == "User lives in Madrid (as of 2026-06-02, may be outdated)"
    monkeypatch.setattr("src.user_time.user_timezone", lambda: timezone.utc)
    assert memory_leases.marked_text(entry) == "User lives in Madrid (as of 2026-06-01, may be outdated)"


def test_switching_leases_off_puts_the_closed_facts_back_in_the_prompt(tmp_path, _default_settings):
    manager = _manager(tmp_path)
    _add(manager, "User lives in Madrid", at=T0)
    _add(manager, "User lives in Berlin", at=T0 + DAY)
    assert [e["text"] for e in memory_leases.prompt_view(manager.load_all(), T0 + 2 * DAY)] == ["User lives in Berlin"]
    _default_settings["memory_leases_enabled"] = False
    assert sorted(e["text"] for e in memory_leases.prompt_view(manager.load_all(), T0 + 2 * DAY)) == [
        "User lives in Berlin", "User lives in Madrid"]


# ── the form-mode add endpoint takes the lease fields ────────────────────


def test_form_mode_add_accepts_kind_and_lease_days(monkeypatch, tmp_path):
    from fastapi import HTTPException
    manager = _manager(tmp_path)
    find = _routes(monkeypatch, manager)
    add = find("/api/memory/add", "POST")
    monkeypatch.setattr("src.auth_helpers.require_privilege", lambda *a, **k: None)

    class _Form:
        def __init__(self, data):
            self.data = data

        async def form(self):
            return self.data

    done = asyncio.run(add(request=_Form({"text": "Bakes on Sundays", "kind": "status", "lease_days": "3"}), memory_data=None))
    assert done["lease"]["kind"] == "status" and done["lease"]["lease_days"] == 3
    with pytest.raises(HTTPException) as bad:
        asyncio.run(add(request=_Form({"text": "x y z", "lease_days": "soon"}), memory_data=None))
    assert bad.value.status_code == 400
    with pytest.raises(HTTPException) as bad_kind:
        asyncio.run(add(request=_Form({"text": "x y z", "kind": "mood"}), memory_data=None))
    assert bad_kind.value.status_code == 400


# ── facts from before leases: an opt-in backfill ─────────────────────────


def _legacy_file(manager):
    rows = [
        {"id": "l1", "text": "User lives in Madrid", "category": "fact", "timestamp": T0 - 900 * DAY, "owner": "alice"},
        {"id": "l2", "text": "User lives in Berlin", "category": "fact", "timestamp": T0 - 300 * DAY, "owner": "alice"},
        {"id": "l3", "text": "User works at Acme Corp", "category": "fact", "timestamp": T0 - 100 * DAY, "owner": "alice"},
        {"id": "l4", "text": "User uses FastAPI", "category": "fact", "timestamp": T0 - 100 * DAY, "owner": "alice"},
        {"id": "l5", "text": "Ana lives in Paris", "category": "contact", "timestamp": T0 - 100 * DAY, "owner": "alice"},
        {"id": "l6", "text": "User lives in Rome", "category": "fact", "timestamp": T0, "owner": "bob"},
    ]
    manager.save(rows)
    return rows


def test_loading_and_reviewing_never_rewrites_old_facts(tmp_path):
    manager = _manager(tmp_path)
    _legacy_file(manager)
    path = tmp_path / "memory.json"
    before = path.read_bytes()
    manager.load_all()
    manager.load(owner="alice")
    memory_leases.review(manager.load(owner="alice"), now=T0)
    assert path.read_bytes() == before


def test_the_review_counts_the_untyped_facts(tmp_path):
    manager = _manager(tmp_path)
    _legacy_file(manager)
    report = memory_leases.review(manager.load(owner="alice"), now=T0)
    assert report["untyped"] == {"total": 4, "typable": 3}   # contact excluded; FastAPI has no kind


def test_backfill_is_a_dry_run_by_default_and_says_what_it_would_close(tmp_path):
    manager = _manager(tmp_path)
    entries = _legacy_file(manager)
    snapshot = [dict(e) for e in entries]
    result = memory_leases.backfill(entries, now=T0, only_ids={"l1", "l2", "l3", "l4", "l5"})
    assert entries == snapshot                                           # nothing written
    assert result["applied"] is False and result["typed"] == 3 and result["left_untyped"] == 1
    rows = {r["id"]: r for r in result["changes"]}
    assert rows["l1"]["kind"] == "location" and rows["l1"]["state"] == "closed"
    assert rows["l2"]["state"] == "expired" and rows["l2"]["closes"] == ["l1"]
    assert rows["l3"]["kind"] == "job" and rows["l3"]["lease_days"] == 365 and rows["l3"]["state"] == "active"
    assert "l4" not in rows and "l5" not in rows and "l6" not in rows


def test_backfill_apply_writes_kind_and_lease_and_save_closes_the_replaced(tmp_path):
    manager = _manager(tmp_path)
    entries = _legacy_file(manager)
    result = memory_leases.backfill(entries, apply=True, now=T0, only_ids={"l1", "l2", "l3", "l4", "l5"})
    assert result["applied"] is True
    manager.save(entries)
    stored = {e["id"]: e for e in manager.load_all()}
    assert stored["l2"]["kind"] == "location" and stored["l2"]["lease_until"] == (T0 - 300 * DAY) + 30 * DAY
    assert stored["l1"]["closed_at"] and stored["l1"]["superseded_by"] == "l2"
    assert "kind" not in stored["l4"] and "kind" not in stored["l5"] and "kind" not in stored["l6"]
    # counted from when it was saved: long expired, so it waits in the review list
    assert memory_leases.state(stored["l2"], T0)["state"] == "expired"
    assert memory_leases.backfill(manager.load_all(), apply=True, now=T0)["typed"] == 1   # bob's, not scoped here


def test_backfill_from_now_starts_every_lease_today(tmp_path):
    manager = _manager(tmp_path)
    entries = _legacy_file(manager)
    memory_leases.backfill(entries, apply=True, basis="now", now=T0, only_ids={"l2"})
    assert entries[1]["lease_until"] == T0 + 30 * DAY and memory_leases.state(entries[1], T0)["state"] == "active"
    with pytest.raises(ValueError):
        memory_leases.backfill(entries, basis="yesterday")


def test_backfill_endpoint_dry_run_then_apply(monkeypatch, tmp_path):
    from fastapi import HTTPException
    manager = _manager(tmp_path)
    _legacy_file(manager)
    find = _routes(monkeypatch, manager)
    backfill = find("/api/memory/leases/backfill", "POST")
    monkeypatch.setattr("src.auth_helpers.require_privilege", lambda *a, **k: None)
    path = tmp_path / "memory.json"
    before = path.read_bytes()
    dry = backfill(request=None, apply=False, basis="recorded")
    assert dry["typed"] == 3 and dry["applied"] is False and path.read_bytes() == before
    done = backfill(request=None, apply=True, basis="recorded")
    assert done["applied"] is True
    stored = {e["id"]: e for e in manager.load_all()}
    assert stored["l3"]["kind"] == "job" and "kind" not in stored["l6"]       # bob's fact untouched
    with pytest.raises(HTTPException):
        backfill(request=None, apply=False, basis="yesterday")
    review = find("/api/memory/leases", "GET")(request=None, window_days=None)
    assert review["untyped"]["typable"] == 0


def test_mcp_backfill_and_review_hint(monkeypatch, tmp_path):
    manager = _manager(tmp_path)
    _legacy_file(manager)
    _server(monkeypatch, manager)
    assert "older fact(s) have no kind" in _call("memory_leases", {"action": "review"})
    dry = _call("memory_leases", {"action": "backfill"})
    assert dry.startswith("Would type (dry run") and "User works at Acme Corp" in dry
    assert all("kind" not in e for e in manager.load_all())
    done = _call("memory_leases", {"action": "backfill", "apply": True})
    assert done.startswith("Typed 3 fact(s)")
    assert next(e for e in manager.load_all() if e["id"] == "l3")["kind"] == "job"
    assert "Error" in _call("memory_leases", {"action": "backfill", "basis": "yesterday"})


def test_mcp_review_rejects_a_bad_window_instead_of_crashing(monkeypatch, tmp_path):
    manager = _manager(tmp_path)
    _add(manager, "User lives in Madrid")
    _server(monkeypatch, manager)
    assert _call("memory_leases", {"action": "review", "window_days": "soon"}).startswith("Error")
    assert _call("memory_leases", {"action": "review", "window_days": -3}).startswith("Error")
    assert _call("memory_leases", {"action": "renew", "memory_id": "x", "days": "many"}).startswith("Error")


# ── the real extractor (stubbed model) confirms an expired fact the user repeats ──


class _ExtractSession:
    session_id = "s-ext"
    owner = "alice"

    def get_context_messages(self):
        return [
            {"role": "user", "content": "just so you know, I still live in Madrid"},
            {"role": "assistant", "content": "Noted."},
        ]


class _FakeVector:
    healthy = True

    def __init__(self, hit_id=None):
        self.hit_id = hit_id
        self.added = []

    def search(self, text, k=8):
        return [{"memory_id": self.hit_id, "score": 0.9}] if self.hit_id else []

    def add(self, memory_id, text):
        self.added.append(memory_id)


def _extract(monkeypatch, manager, answer, vector=None):
    import json as _json
    import services.memory.memory_extractor as extractor

    async def _fake_llm(*args, **kwargs):
        return _json.dumps([{"text": t, "category": "fact"} for t in answer])

    monkeypatch.setattr("src.llm_core.llm_call_async", _fake_llm)
    asyncio.run(extractor.extract_and_store(_ExtractSession(), manager, vector, endpoint_url="http://127.0.0.1:1/v1", model="stub"))
    return {e["text"]: e for e in manager.load_all()}


def _now():
    return int(datetime.now(timezone.utc).timestamp())


@pytest.mark.parametrize("age_days", [40, 25])          # expired, then inside the review window
def test_extractor_renews_a_leased_fact_the_user_says_again(monkeypatch, tmp_path, age_days):
    manager = _manager(tmp_path)
    old = _add(manager, "User lives in Madrid", at=_now() - age_days * DAY)
    stored = _extract(monkeypatch, manager, ["User lives in Madrid"])
    assert len(stored) == 1
    renewed = stored["User lives in Madrid"]
    assert renewed["id"] == old["id"] and renewed["lease_until"] > _now() + 28 * DAY
    assert memory_leases.state(renewed)["state"] == "active"
    assert memory_leases.prompt_view(manager.load_all())[0]["text"] == "User lives in Madrid"      # no longer marked


def test_extractor_leaves_a_fresh_lease_alone(monkeypatch, tmp_path):
    manager = _manager(tmp_path)
    old = _add(manager, "User lives in Madrid", at=_now() - 2 * DAY)
    stored = _extract(monkeypatch, manager, ["User lives in Madrid"])
    assert stored["User lives in Madrid"]["lease_until"] == old["lease_until"]


def test_extractor_renews_through_the_vector_duplicate_path_too(monkeypatch, tmp_path):
    manager = _manager(tmp_path)
    old = _add(manager, "User lives in Madrid", at=_now() - 40 * DAY)
    stored = _extract(monkeypatch, manager, ["User lives in Madrid."], vector=_FakeVector(old["id"]))
    assert len(stored) == 1 and stored["User lives in Madrid"]["lease_until"] > _now() + 28 * DAY


def test_extractor_adds_a_different_place_instead_of_renewing_the_old_one(monkeypatch, tmp_path):
    """"lives in Paris" is 0.6 alike to "lives in Madrid" by words, but it is another claim."""
    manager = _manager(tmp_path)
    old = _add(manager, "User lives in Madrid", at=_now() - 40 * DAY)
    stored = _extract(monkeypatch, manager, ["User lives in Paris"])
    assert set(stored) == {"User lives in Madrid", "User lives in Paris"}
    assert stored["User lives in Madrid"]["superseded_by"] == stored["User lives in Paris"]["id"]
    assert memory_leases.state(stored["User lives in Madrid"])["state"] == "closed"
    assert stored["User lives in Madrid"]["id"] == old["id"]


def test_extractor_reopens_a_replaced_fact_only_for_the_very_same_text(monkeypatch, tmp_path):
    manager = _manager(tmp_path)
    now = _now()
    _add(manager, "User lives in Madrid", at=now - 20 * DAY)
    _add(manager, "User lives in Berlin", at=now - 10 * DAY)
    assert "closed_at" in next(e for e in manager.load_all() if e["text"] == "User lives in Madrid")
    # a looser wording of the old place is NOT a reason to reopen it
    stored = _extract(monkeypatch, manager, ["User lives in Madrid, Spain."])
    assert "closed_at" in stored["User lives in Madrid"] and "User lives in Madrid, Spain." in stored
    # the very same words are the user saying it now: it comes back and Berlin is the one replaced
    (tmp_path / "second").mkdir()
    manager2 = _manager(tmp_path / "second")
    _add(manager2, "User lives in Madrid", at=now - 20 * DAY)
    _add(manager2, "User lives in Berlin", at=now - 10 * DAY)
    stored2 = _extract(monkeypatch, manager2, ["User lives in Madrid"])
    assert "closed_at" not in stored2["User lives in Madrid"] and "closed_at" in stored2["User lives in Berlin"]
    assert len(stored2) == 2


def test_extractor_drops_a_repeat_of_a_fact_that_has_no_kind_exactly_as_before(monkeypatch, tmp_path):
    manager = _manager(tmp_path)
    manager.save([{"id": "old", "text": "User studied physics in Valencia", "category": "fact", "timestamp": 1, "owner": "alice"}])
    stored = _extract(monkeypatch, manager, ["User studied physics in Valencia"])
    assert list(stored) == ["User studied physics in Valencia"] and "kind" not in stored["User studied physics in Valencia"]


def test_extractor_does_not_lose_a_move_to_a_look_alike_third_party_or_legacy_fact(monkeypatch, tmp_path):
    """"User lives in Berlin" is a near-duplicate (words and embedding) of the sister's
    place and of an untyped "lives in Paris"; neither may swallow the user's own move."""
    manager = _manager(tmp_path)
    manager.save([
        {"id": "sis", "text": "User's sister lives in Berlin.", "category": "fact", "timestamp": 1, "owner": "alice"},
        {"id": "old", "text": "User lives in Paris", "category": "fact", "timestamp": 2, "owner": "alice"},
    ])
    stored = _extract(monkeypatch, manager, ["User lives in Berlin"], vector=_FakeVector("sis"))
    assert stored["User lives in Berlin"]["kind"] == "location"
    assert set(stored) == {"User's sister lives in Berlin.", "User lives in Paris", "User lives in Berlin"}
    shown = [e["text"] for e in memory_leases.prompt_view(manager.load_all())]
    assert "User lives in Berlin" in shown and "User's sister lives in Berlin." in shown


# ══════════════════════════════════════════════════════════════════════════
# Second review round: wishes, plans, the past and relative clauses
# ══════════════════════════════════════════════════════════════════════════

NOT_CURRENT_PLACE = [
    "User wants to live in Tokyo",
    "User used to live in Paris",
    "User will live in Berlin next year",
    "User is thinking of living in Tokyo",
    "User dreams of living in Rome",
    "User met Ana, who lives in Berlin",
    "User would like to live in Lisbon",
    "User hopes to live in Oslo",
    "User is planning to live in Vienna",
    "User is going to live in Porto",
    "User is considering living in Prague",
    "User applied to live in Canada",
    "User has a friend that lives in Lima",
    "El usuario quiere vivir en Tokio",
    "Al usuario le gustaría vivir en Roma",
    "El usuario piensa vivir en Lisboa",
    "El usuario va a vivir en Berlín",
    "El usuario vivirá en Berlín",
    "El usuario solía vivir en París",
    "El usuario antes vivía en París",
    "El usuario ya no vive en Madrid",
    "El usuario sueña con vivir en Roma",
    "El usuario conoció a Ana, que vive en Berlín",
]
NOT_CURRENT_JOB = [
    "User wants to work at Google",
    "User applied to work at Google",
    "User used to work at Google",
    "User is looking for a job working at Google",
    "User would like to work for Initech",
    "User plans to work at Globex",
    "User will work at Google next year",
    "User dreams of working at Google",
    "User has a friend who works at Google",
    "User no longer works at Acme",
    "El usuario quiere trabajar en Google",
    "El usuario ha solicitado trabajar en Google",
    "El usuario busca trabajo en Google",
    "El usuario solía trabajar en Google",
    "El usuario antes trabajaba en Google",
    "El usuario trabajará en Google el año que viene",
    "El usuario ya no trabaja en Acme",
    "El usuario tiene una amiga que trabaja en Google",
]


@pytest.mark.parametrize("text", NOT_CURRENT_PLACE + NOT_CURRENT_JOB + ["User doesn't live in Madrid anymore"])
def test_wishes_plans_the_past_and_relative_clauses_get_no_single_valued_kind(text):
    assert memory_leases.classify_kind(text) not in memory_leases.SINGLE_VALUED
    assert memory_leases.slot_of("location", text) == ""
    assert memory_leases.slot_of("job", text) == ""


@pytest.mark.parametrize("text", NOT_CURRENT_PLACE)
def test_a_wish_a_plan_or_the_past_never_takes_the_place_of_where_the_user_lives(tmp_path, text):
    manager = _manager(tmp_path)
    home = _add(manager, "User lives in Madrid", at=T0)
    _add(manager, text, at=T0 + DAY)
    stored = {e["id"]: e for e in manager.load_all()}
    assert "closed_at" not in stored[home["id"]]
    assert "User lives in Madrid" in [e["text"] for e in memory_leases.prompt_view(manager.load_all(), T0 + 2 * DAY)]


@pytest.mark.parametrize("text", NOT_CURRENT_JOB)
def test_a_wish_a_plan_or_the_past_never_takes_the_place_of_the_users_employer(tmp_path, text):
    manager = _manager(tmp_path)
    job = _add(manager, "User works at Acme", at=T0)
    _add(manager, text, at=T0 + DAY)
    stored = {e["id"]: e for e in manager.load_all()}
    assert "closed_at" not in stored[job["id"]]
    assert "User works at Acme" in [e["text"] for e in memory_leases.prompt_view(manager.load_all(), T0 + 2 * DAY)]


def test_leaving_a_place_does_not_state_a_new_one_and_the_old_fact_stays(tmp_path):
    text = "User doesn't live in Madrid anymore"
    assert memory_leases.classify_kind(text) not in memory_leases.SINGLE_VALUED
    manager = _manager(tmp_path)
    home = _add(manager, "User lives in Madrid", at=T0)
    _add(manager, text, at=T0 + DAY)
    # It cannot take over the residence slot: whatever happens to Madrid, no new place appears.
    assert not [e for e in manager.load_all() if memory_leases.slot_of(e.get("kind", ""), e["text"]) == "location"
                and e["text"] != "User lives in Madrid"]


@pytest.mark.parametrize("old,new,slot", [
    ("User lives in Madrid", "User moved to Berlin", "location"),
    ("User lives in Madrid", "Vivo en Valencia", "location"),
    ("User lives in Madrid", "User lives in Berlin", "location"),
    ("User lives in Madrid", "User used to live in Paris and now lives in Berlin", "location"),
    ("User lives in Madrid", "User lives in Berlin and wants to live in Tokyo", "location"),
    ("User lives in Madrid", "Yo antes vivía en París pero ahora vivo en Berlín", "location"),
    ("User works at Acme", "Trabajo en Globex", "job:employer"),
    ("User works at Acme", "User works at Google", "job:employer"),
    ("User works at Acme", "User used to work at Hooli but works at Google", "job:employer"),
    ("User works at Acme", "User works at Google and would like to work at Initech", "job:employer"),
])
def test_real_current_statements_still_supersede_in_the_same_sentences_family(tmp_path, old, new, slot):
    kind = memory_leases.classify_kind(new)
    assert memory_leases.slot_of(kind, new) == slot
    manager = _manager(tmp_path)
    first = _add(manager, old, at=T0)
    second = _add(manager, new, at=T0 + DAY)
    stored = {e["id"]: e for e in manager.load_all()}
    assert stored[first["id"]]["superseded_by"] == second["id"]
    assert "closed_at" not in stored[second["id"]]


def test_wishes_and_relative_clauses_through_the_add_endpoint_leave_the_prompt_alone(monkeypatch, tmp_path):
    from src.request_models import MemoryAddRequest
    manager = _manager(tmp_path)
    find = _routes(monkeypatch, manager)
    add = find("/api/memory/add", "POST")
    monkeypatch.setattr("src.auth_helpers.require_privilege", lambda *a, **k: None)
    asyncio.run(add(request=None, memory_data=MemoryAddRequest(text="User lives in Madrid")))
    asyncio.run(add(request=None, memory_data=MemoryAddRequest(text="User works at Acme")))
    for text in ("User wants to live in Tokyo", "User used to live in Paris", "User met Ana, who lives in Berlin",
                 "User wants to work at Google", "User applied to work at Google", "User used to work at Google",
                 "El usuario ya no vive en Madrid"):
        asyncio.run(add(request=None, memory_data=MemoryAddRequest(text=text)))
    rows = manager.load(owner="alice")
    assert all("closed_at" not in e for e in rows)
    assert "User lives in Madrid" in _injected(rows, "where does the user live in Madrid")
    assert "User works at Acme" in _injected(rows, "where does the user work at Acme")


def test_the_review_endpoint_rejects_a_negative_window_like_the_mcp_tool(monkeypatch, tmp_path):
    from fastapi import HTTPException
    manager = _manager(tmp_path)
    review = _routes(monkeypatch, manager)("/api/memory/leases", "GET")
    with pytest.raises(HTTPException) as err:
        review(request=None, window_days=-3)
    assert err.value.status_code == 400
    assert review(request=None, window_days=0)["counts"]["expired"] == 0


# ── two candidates joined by "or", and questions, are not statements ──────

ALTERNATIVES = [
    "User lives in Madrid or Berlin, and do I work at Acme Corp or Globex",
    "User lives in Madrid or Berlin",
    "User works at Acme or Globex",
    "Vivo en Madrid o Berlin",
]


@pytest.mark.parametrize("text", ALTERNATIVES)
def test_two_candidates_joined_by_or_are_not_a_residence_or_an_employer(tmp_path, text):
    assert memory_leases.classify_kind(text) not in memory_leases.SINGLE_VALUED
    assert memory_leases.slot_of("location", text) == "" and memory_leases.slot_of("job", text) == ""
    manager = _manager(tmp_path)
    home = _add(manager, "User lives in Madrid", at=T0)
    job = _add(manager, "User works at Acme Corp", at=T0)
    _add(manager, text, at=T0 + DAY)
    stored = {e["id"]: e for e in manager.load_all()}
    assert "closed_at" not in stored[home["id"]] and "closed_at" not in stored[job["id"]]


def test_the_fallback_extractor_does_not_turn_a_question_into_a_place():
    from services.memory.memory_extractor import _fallback_memory_candidates as fallback
    for question in ("Do I live in Madrid or Berlin?", "Where do I live in Spain?", "Do I live in Madrid",
                     "I live in Madrid or Berlin, I do not remember"):
        assert fallback([{"role": "user", "content": question}]) == [], question
    assert [c["text"] for c in fallback([{"role": "user", "content": "I live in Berlin."}])] == ["User lives in Berlin"]
