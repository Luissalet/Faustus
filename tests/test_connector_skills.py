"""The skills a connector carries (src/connector_skills.py): collected when it connects or refreshes,
shown as a one-line catalogue for the apps in play, read on demand with the app's own `skill_read`,
and never listed twice when the library has the same slug."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from src import connector_skills
from src.builtin_mcp import _BUILTIN_SERVERS, is_native_twin
from src.mcp_manager import McpManager

SID = "ledger-1"

SHELF = {
    "app": "ledger", "skills": [
        {"slug": "recurring-payments", "name": "Recurring payments",
         "description": "Review subscriptions. Use when the user asks about recurring charges or subscriptions.",
         "version": "1.2.0", "category": "finance", "tags": ["ledger"], "files": ["references/a.md"]},
        {"slug": "api-design", "name": "API design", "description": "Design an HTTP API.", "version": "1.0.0"},
        {"slug": "month-close", "name": "Month close", "description": "Close the month. Use when closing the books.", "version": "0.3.0"},
    ],
}


def _result(payload, error=False):
    return SimpleNamespace(content=[SimpleNamespace(text=payload if isinstance(payload, str) else json.dumps(payload))],
                           isError=error, structuredContent=None)


class FakeSession:
    def __init__(self):
        self.calls = []

    async def call_tool(self, name, arguments, **_):
        self.calls.append((name, arguments))
        if name == "skills_list":
            return _result(SHELF)
        if name == "skill_read":
            return _result({"slug": arguments["slug"], "name": "Recurring payments", "version": "1.2.0",
                            "file": "SKILL.md", "files": [], "text": "# Procedure\n1. Open the ledger."})
        return _result("nope", error=True)


def _manager(session=None, tools=("skills_list", "skill_read", "list_payments")):
    mgr = McpManager()
    mgr._sessions[SID] = session or FakeSession()
    mgr._connections[SID] = {"status": "connected", "name": "Ledger's Hoard"}
    mgr._tools[SID] = [{"name": n, "description": "", "input_schema": {}} for n in tools]
    return mgr


@pytest.fixture(autouse=True)
def _clean():
    connector_skills.reset()
    yield
    connector_skills.reset()


async def test_collect_keeps_slug_name_when_version_and_marks_library(monkeypatch):
    monkeypatch.setattr(connector_skills, "_library_slugs", lambda: {"api-design"})
    entry = await connector_skills.collect(_manager(), SID)
    assert entry["source"] == "tool" and entry["error"] is None
    rows = {r["slug"]: r for r in entry["skills"]}
    assert set(rows) == {"recurring-payments", "api-design", "month-close"}
    assert rows["recurring-payments"]["version"] == "1.2.0"
    assert rows["recurring-payments"]["when_to_use"].startswith("Use when the user asks about recurring")
    assert rows["api-design"]["in_library"] is True and rows["month-close"]["in_library"] is False
    assert connector_skills.catalog(SID)["skills"][0]["slug"] == "recurring-payments"


async def test_collect_without_a_shelf_is_an_empty_catalogue_not_an_error():
    mgr = _manager(tools=("list_payments",))
    entry = await connector_skills.collect(mgr, SID)
    assert entry["skills"] == [] and entry["source"] == "none"


async def test_collect_survives_a_failing_shelf():
    class Broken(FakeSession):
        async def call_tool(self, name, arguments, **_):
            return _result("boom", error=True)
    entry = await connector_skills.collect(_manager(Broken()), SID)
    assert entry["skills"] == [] and entry["error"]


async def test_read_goes_to_the_connectors_own_skill_read():
    session = FakeSession()
    out = await connector_skills.read(_manager(session), SID, "Skill-Recurring-Payments")
    assert out["ok"] and "Open the ledger" in out["text"]
    assert session.calls == [("skill_read", {"slug": "recurring-payments"})]


async def test_read_of_a_disconnected_connector_says_so():
    out = await connector_skills.read(McpManager(), "gone", "x")
    assert out == {"ok": False, "error": "connector not connected"}


async def test_the_catalogue_is_refreshed_by_discovery_and_dropped_on_disconnect():
    session = FakeSession()
    mgr = _manager(session)
    connector_skills.schedule(mgr, SID)
    await asyncio.sleep(0.3)
    assert connector_skills.catalog(SID) and len(connector_skills.catalog(SID)["skills"]) == 3
    connector_skills.drop(SID)
    assert connector_skills.catalog(SID) is None


async def test_one_call_per_collection_not_per_turn():
    session = FakeSession()
    mgr = _manager(session)
    await connector_skills.collect(mgr, SID)
    for _ in range(5):
        connector_skills.plan_turn("show my recurring payments", ["mcp__ledger-1__list_payments"])
    assert [c[0] for c in session.calls] == ["skills_list"]


async def test_apps_in_play_come_from_retrieved_tools_or_the_name_never_from_shelf_tools():
    await connector_skills.collect(_manager(), SID)
    assert connector_skills.servers_in_play(["mcp__ledger-1__list_payments"], "algo") == [SID]
    assert connector_skills.servers_in_play(["mcp__ledger-1__skill_read", "mcp__ledger-1__skills_list"], "algo") == []
    assert connector_skills.servers_in_play([], "revisa mis pagos en Ledger por favor") == [SID]
    assert connector_skills.servers_in_play([], "una pregunta sin app") == []


async def test_plan_turn_is_a_short_catalogue_with_the_read_tool_and_no_bodies():
    await connector_skills.collect(_manager(), SID)
    plan = connector_skills.plan_turn("show my recurring payments", ["mcp__ledger-1__list_payments"])
    assert plan.servers == [SID]
    assert plan.skill_tools == {"mcp__ledger-1__skill_read"}
    assert "recurring-payments" in plan.text and "mcp__ledger-1__skill_read" in plan.text
    assert "Open the ledger" not in plan.text
    assert len(plan.entries) <= connector_skills.PER_CONNECTOR


async def test_a_library_slug_stays_one_entry_and_the_local_skill_is_ranked_up(monkeypatch):
    await connector_skills.collect(_manager(), SID)
    local = [{"name": "API design", "tags": ["library:api-design"]}]
    plan = connector_skills.plan_turn("ledger api", ["mcp__ledger-1__list_payments"], local_skills=local)
    assert "api-design" not in plan.text
    assert plan.boost == {"API design": 1.5}


async def test_nothing_in_play_costs_nothing():
    await connector_skills.collect(_manager(), SID)
    plan = connector_skills.plan_turn("hola", ["web_search"])
    assert not plan and plan.text == "" and plan.skill_tools == set()


async def test_served_by_and_listing_follow_the_catalogue(monkeypatch):
    monkeypatch.setattr(connector_skills, "_library_slugs", lambda: {"api-design"})
    mgr = _manager()
    await connector_skills.collect(mgr, SID)
    assert [c["server_id"] for c in connector_skills.served_by()["api-design"]] == [SID]
    rows = connector_skills.listing(mgr)
    assert rows[0]["count"] == 3 and rows[0]["server_id"] == SID
    mgr._sessions.pop(SID)
    assert connector_skills.listing(mgr) == []


def test_shelf_tools_are_kept_out_of_retrieval_but_other_tools_are_not():
    for tool in connector_skills.SKILL_SHELF_TOOLS:
        assert is_native_twin(f"mcp__ledger__{tool}") and connector_skills.is_shelf_tool(f"mcp__ledger__{tool}")
    assert not is_native_twin("mcp__ledger__list_payments")


def test_the_selector_ranks_up_a_boosted_skill():
    from src.skills_runtime import selector
    sm = SimpleNamespace(
        _filter_candidates=lambda skills, _c: skills,
        _load_usage=lambda: {},
        _usage_entry=lambda *_a, **_k: {},
    )
    skills = [{"id": n, "name": n, "description": "close month books", "status": "published", "tags": []} for n in ("a", "b")]
    selector.reset_selector_state()
    selector._embedder_failed = True
    plain = selector._select_hybrid(sm, None, "close month books", skills, 0.0, 5, 0.0)
    boosted = selector._select_hybrid(sm, None, "close month books", skills, 0.0, 5, 0.0, boost={"b": 1.5})
    assert [s["name"] for s in boosted][0] == "b"
    assert boosted[0]["_selector"]["score"] > plain[0]["_selector"]["score"]
    selector.reset_selector_state()


def test_the_faustus_mcp_server_is_registered_and_lists_both_tools():
    assert "connector_skills" in _BUILTIN_SERVERS
    from mcp_servers import connector_skills_server as srv
    names = asyncio.run(srv.list_tools())
    assert [t.name for t in names] == ["connector_skills_list", "connector_skill_read"]


async def test_a_closed_app_is_never_asked_for_its_shelf(monkeypatch):
    """A family bridge starts a closed app on the first call that reaches it; reading skills must not do that."""
    session = FakeSession()
    monkeypatch.setattr(connector_skills, "_connector_of", lambda sid: {"connector_id": "c1", "preset_id": "baird"})

    async def closed(meta):
        return False
    monkeypatch.setattr(connector_skills, "app_is_up", closed)
    entry = await connector_skills.collect(_manager(session), SID)
    assert session.calls == []
    assert entry["skills"] == [] and entry["error"] == "app not running" and entry["skipped"] is True


async def test_what_was_collected_while_the_app_was_up_survives_a_later_closed_check(monkeypatch):
    session = FakeSession()
    mgr = _manager(session)
    await connector_skills.collect(mgr, SID)

    async def closed(meta):
        return False
    monkeypatch.setattr(connector_skills, "_connector_of", lambda sid: {"connector_id": "c1", "preset_id": "ledger"})
    monkeypatch.setattr(connector_skills, "app_is_up", closed)
    entry = await connector_skills.collect(mgr, SID)
    assert len(entry["skills"]) == 3 and entry["skipped"] is True
