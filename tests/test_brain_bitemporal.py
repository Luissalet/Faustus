"""Bitemporal brain facts: contradiction closing, both time axes, and the
API / agent tool / MCP read surfaces (task 192)."""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes import brain_routes
from src.agent_tools.brain_tools import BrainTool
from src.brain import bitemporal, db as brain_db, entities, relation_history as history

OWNER = "alice"


@pytest.fixture
def clock(tmp_path, monkeypatch):
    brain_db.use_dir(str(tmp_path / "brain"))
    now = ["2026-01-01T00:00:00.000000Z"]
    monkeypatch.setattr(history, "recorded_now", lambda: now[0])
    yield now
    brain_db.use_dir(None)


def seed(clock):
    ada = entities.upsert_entity(OWNER, "Ada", type="person")
    lab = entities.upsert_entity(OWNER, "Lab", type="org")
    new_lab = entities.upsert_entity(OWNER, "New Lab", type="org")
    old = entities.add_relation(OWNER, ada["id"], "works_at", dst_id=lab["id"], valid_from="2024-01-01")
    clock[0] = "2026-02-01T00:00:00.000000Z"
    new = entities.add_relation(OWNER, ada["id"], "works_at", dst_id=new_lab["id"], valid_from="2025-01-01")
    return ada, lab, new_lab, old, new


def test_contradiction_closes_old_and_keeps_history(clock):
    ada, lab, new_lab, old, new = seed(clock)
    everything = bitemporal.facts(OWNER, entity_id=ada["id"], include_closed=True)["facts"]
    by_id = {r["id"]: r for r in everything}
    assert by_id[old["id"]]["status"] == "superseded"
    assert by_id[old["id"]]["valid_until"].startswith("2025-01-01")
    assert by_id[old["id"]]["superseded_by"] == new["id"]
    assert by_id[old["id"]]["superseded_at"] == "2026-02-01T00:00:00.000000Z"
    assert by_id[new["id"]]["status"] == "active"
    assert by_id[new["id"]]["superseded_at"] == ""
    assert by_id[old["id"]]["recorded_at"]  # the learned-at axis is explicit


def test_default_view_is_current_only_and_matches_list_relations(clock):
    ada, lab, new_lab, old, new = seed(clock)
    view = bitemporal.facts(OWNER, entity_id=ada["id"])
    assert view["mode"] == "current"
    assert [r["id"] for r in view["facts"]] == [new["id"]]
    plain = entities.list_relations(OWNER, entity_id=ada["id"], include_closed=False)
    assert [r["id"] for r in plain] == [new["id"]]
    assert "works_at" in view["functional_relations"]


def test_as_of_world_time(clock):
    ada, lab, new_lab, old, new = seed(clock)
    then = bitemporal.facts(OWNER, entity_id=ada["id"], as_of="2024-06-01")
    now = bitemporal.facts(OWNER, entity_id=ada["id"], as_of="2025-06-01")
    assert then["mode"] == "as_of"
    assert [r["dst"] for r in then["facts"]] == [lab["id"]]
    assert [r["dst"] for r in now["facts"]] == [new_lab["id"]]


def test_known_at_recorded_time_and_combined(clock):
    ada, lab, new_lab, old, new = seed(clock)
    before = bitemporal.facts(OWNER, entity_id=ada["id"], known_at="2026-01-15", include_closed=True)
    assert [r["dst"] for r in before["facts"]] == [lab["id"]]
    assert before["facts"][0]["status"] == "active"      # not yet retired in Faustus's mind
    assert before["facts"][0]["superseded_at"] == ""
    after = bitemporal.facts(OWNER, entity_id=ada["id"], known_at="2026-02-15", include_closed=True)
    assert {r["dst"] for r in after["facts"]} == {lab["id"], new_lab["id"]}
    both = bitemporal.facts(OWNER, entity_id=ada["id"], as_of="2025-06-01", known_at="2026-01-15")
    assert both["mode"] == "as_of+known_at"
    assert [r["dst"] for r in both["facts"]] == [lab["id"]]   # believed then
    both2 = bitemporal.facts(OWNER, entity_id=ada["id"], as_of="2025-06-01", known_at="2026-02-15")
    assert [r["dst"] for r in both2["facts"]] == [new_lab["id"]]


def test_migration_backfills_recorded_time_and_leaves_valid_open(clock):
    ada = entities.upsert_entity(OWNER, "Ada", type="person")
    lab = entities.upsert_entity(OWNER, "Lab", type="org")
    entities.add_relation(OWNER, ada["id"], "knows", dst_id=lab["id"])  # no valid_* given
    with brain_db.db() as conn:
        conn.execute("DELETE FROM relation_versions")           # as if pre-history legacy row
    clock[0] = "2026-03-01T00:00:00.000000Z"
    assert bitemporal.facts(OWNER, known_at="2026-02-01")["facts"] == []
    rows = bitemporal.facts(OWNER, known_at="2026-03-02")["facts"]
    assert len(rows) == 1 and rows[0]["recorded_at"]
    assert rows[0]["valid_until"] == ""


def test_bad_dates_and_rel_filter(clock):
    ada, *_ = seed(clock)
    with pytest.raises(ValueError):
        bitemporal.facts(OWNER, as_of="yesterday")
    with pytest.raises(ValueError):
        bitemporal.facts(OWNER, known_at="soon")
    assert bitemporal.facts(OWNER, rel="lives_in", include_closed=True)["count"] == 0
    assert bitemporal.facts(OWNER, rel="works_at", include_closed=True)["count"] == 2


def test_owner_isolation(clock):
    seed(clock)
    assert bitemporal.facts("mallory", include_closed=True)["facts"] == []


def test_api_route(clock, monkeypatch):
    ada, lab, new_lab, old, new = seed(clock)
    monkeypatch.setattr(brain_routes, "effective_user", lambda r: r.headers.get("x-test-owner") or "")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    app = FastAPI()
    app.include_router(brain_routes.setup_brain_routes())
    c = TestClient(app)
    h = {"x-test-owner": OWNER}
    cur = c.get("/api/brain/facts", params={"entity_id": ada["id"]}, headers=h).json()
    assert [r["id"] for r in cur["facts"]] == [new["id"]]
    then = c.get("/api/brain/facts", params={"entity_id": ada["id"], "as_of": "2024-06-01"}, headers=h).json()
    assert [r["dst"] for r in then["facts"]] == [lab["id"]]
    kn = c.get("/api/brain/facts", params={"known_at": "2026-01-15", "include_closed": "true"}, headers=h).json()
    assert [r["dst"] for r in kn["facts"]] == [lab["id"]]
    assert c.get("/api/brain/facts", params={"as_of": "nope"}, headers=h).status_code == 400
    assert c.get("/api/brain/facts", headers={"x-test-owner": "mallory"}).json()["facts"] == []


def test_agent_tool_facts(clock):
    ada, lab, new_lab, old, new = seed(clock)
    run = lambda c: asyncio.run(BrainTool().execute(c, {"owner": OWNER}))
    out = run({"action": "facts", "name": "Ada", "as_of": "2024-06-01"})
    assert out["exit_code"] == 0 and [r["dst"] for r in out["facts"]] == [lab["id"]]
    cur = run({"action": "facts", "entity_id": ada["id"]})
    assert [r["id"] for r in cur["facts"]] == [new["id"]]
    assert run({"action": "facts", "known_at": "garbage"})["exit_code"] == 1
    assert run({"action": "facts", "name": "Nobody"})["exit_code"] == 1


def test_as_of_boundary_is_half_open(clock):
    ada, lab, new_lab, old, new = seed(clock)
    before = bitemporal.facts(OWNER, entity_id=ada["id"], as_of="2024-12-31")["facts"]
    on = bitemporal.facts(OWNER, entity_id=ada["id"], as_of="2025-01-01")["facts"]
    after = bitemporal.facts(OWNER, entity_id=ada["id"], as_of="2025-01-02")["facts"]
    assert [r["dst"] for r in before] == [lab["id"]]
    assert [r["dst"] for r in on] == [new_lab["id"]]      # old closed exactly at T is over
    assert [r["dst"] for r in after] == [new_lab["id"]]


def test_restored_relation_does_not_keep_old_retirement(clock):
    ada = entities.upsert_entity(OWNER, "Ada", type="person")
    lab = entities.upsert_entity(OWNER, "Lab", type="org")
    rel = entities.add_relation(OWNER, ada["id"], "invented_rel", dst_id=lab["id"], method="llm")
    clock[0] = "2026-02-01T00:00:00.000000Z"
    assert entities.revalidate(OWNER)["retracted_count"] == 1
    during = bitemporal.facts(OWNER, entity_id=ada["id"], include_closed=True)
    assert [r["id"] for r in during["facts"]] == []        # withdrawn claim is not a fact
    clock[0] = "2026-03-01T00:00:00.000000Z"
    assert entities.undo_revalidate(OWNER)["restored_count"] == 1
    for kw in ({}, {"known_at": "2026-03-02"}, {"as_of": "2099-01-01"}):
        row = [r for r in bitemporal.facts(OWNER, entity_id=ada["id"], **kw)["facts"] if r["id"] == rel["id"]]
        assert row and row[0]["status"] == "active" and row[0]["superseded_at"] == "", kw
    before = bitemporal.facts(OWNER, entity_id=ada["id"], known_at="2026-01-15")["facts"]
    assert before and before[0]["superseded_at"] == ""
    mid = bitemporal.facts(OWNER, entity_id=ada["id"], known_at="2026-02-15", include_closed=True)["facts"]
    assert mid == []


def test_native_schema_accepts_facts_arguments():
    from src.tool_schemas import validate_tool_arguments
    args = {"action": "facts", "name": "QA Ada", "as_of": "2025-01-01",
            "known_at": "2026-01-01", "rel": "works_at", "include_closed": True}
    assert validate_tool_arguments("brain", args) == []
    assert any(e.kind == "enum" for e in validate_tool_arguments("brain", {"action": "nope"}))


def test_facts_is_classified_as_private_read():
    from src import context_tool_gate as gate
    from src.tool_capabilities import ToolEffect, capabilities_for_action
    caps = capabilities_for_action("brain", {"action": "facts"})
    assert ToolEffect.READ_PRIVATE in caps.effects and ToolEffect.WRITE_PRIVATE not in caps.effects
    assert gate.allows("brain", {"action": "facts"})
    for act in ("write", "append", "daily"):                # writes stay gated
        assert not gate.allows("brain", {"action": act})
        assert ToolEffect.WRITE_PRIVATE in capabilities_for_action("brain", {"action": act}).effects
