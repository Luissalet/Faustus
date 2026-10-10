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
