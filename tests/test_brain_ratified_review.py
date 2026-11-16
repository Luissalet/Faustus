"""Ratified facts, contradictions and expired content in the brain."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.brain import db as brain_db  # noqa: E402
from src.brain import entities, ratified, review  # noqa: E402
from src import memory_engine as engine  # noqa: E402

OWNER = "alice"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    brain_db.use_dir(str(tmp_path / "brain"))
    monkeypatch.setattr(engine, "DATA_DIR", str(tmp_path / "engine"))
    engine.set_vector_store(None)
    yield tmp_path
    brain_db.use_dir(None)
    engine.reset_vector_store()


def _iso(days: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _two_employers(same_start: bool = True):
    ada = entities.upsert_entity(OWNER, "Ada", type="person")
    first = entities.add_relation(OWNER, ada["id"], "works_at", dst_value="Cordera Labs",
                                  valid_from="2024-01-01")
    second = entities.add_relation(OWNER, ada["id"], "works_at", dst_value="Bluehaven",
                                   valid_from="2024-01-01" if same_start else "2025-01-01")
    return ada, first, second


def test_ratify_roundtrip_and_validation(store):
    ada, first, _ = _two_employers()
    mark = ratified.ratify(OWNER, f"rel:{first['id']}", note="confirmed by me")
    assert mark["ref"] == f"rel:{first['id']}" and mark["note"] == "confirmed by me"
    assert ratified.is_ratified(OWNER, f"rel:{first['id']}")
    assert not ratified.is_ratified("bob", f"rel:{first['id']}")
    assert ratified.unratify(OWNER, f"rel:{first['id']}") is True
    assert ratified.unratify(OWNER, f"rel:{first['id']}") is False
    with pytest.raises(ratified.RatifyError):
        ratified.ratify(OWNER, "rel:does-not-exist")
    with pytest.raises(ratified.RatifyError):
        ratified.ratify(OWNER, "banana")


def test_equal_dates_leave_a_contradiction_that_ratify_settles(store):
    ada, first, second = _two_employers(same_start=True)
    found = review.contradictions(OWNER)
    assert len(found) == 1
    item = found[0]
    assert item["entity"]["name"] == "Ada" and item["rel"] == "works_at"
    assert {o["value"] for o in item["options"]} == {"Cordera Labs", "Bluehaven"}
    assert item["settled_by_ratification"] is False

    out = review.resolve(OWNER, "keep", f"rel:{first['id']}")
    assert f"rel:{second['id']}" in out["changed"]
    assert review.contradictions(OWNER) == []
    assert ratified.is_ratified(OWNER, f"rel:{first['id']}")
    active = entities.list_relations(OWNER, entity_id=ada["id"], include_closed=False)
    assert [r["dst_value"] for r in active] == ["Cordera Labs"]
    # the archived fact is kept for the record, not deleted
    kept = entities.list_relations(OWNER, entity_id=ada["id"], include_retracted=True)
    assert {r["status"] for r in kept} == {"active", "retracted"}


def test_ratified_fact_is_not_closed_by_a_newer_unratified_one(store):
    ada = entities.upsert_entity(OWNER, "Ada", type="person")
    old = entities.add_relation(OWNER, ada["id"], "works_at", dst_value="Cordera Labs",
                                valid_from="2023-01-01")
    ratified.ratify(OWNER, f"rel:{old['id']}")
    entities.add_relation(OWNER, ada["id"], "works_at", dst_value="Bluehaven",
                          valid_from="2025-01-01", method="llm")
    # without the ratification the newer value would have closed the old one
    found = review.contradictions(OWNER)
    assert len(found) == 1 and found[0]["settled_by_ratification"] is True
    assert found[0]["ratified_ref"] == f"rel:{old['id']}"
    assert found[0]["options"][0]["ratified"] is True


def test_unratified_newer_value_still_supersedes(store):
    ada = entities.upsert_entity(OWNER, "Ada", type="person")
    old = entities.add_relation(OWNER, ada["id"], "works_at", dst_value="Cordera Labs",
                                valid_from="2023-01-01")
    entities.add_relation(OWNER, ada["id"], "works_at", dst_value="Bluehaven", valid_from="2025-01-01")
    assert review.contradictions(OWNER) == []
    rows = {r["id"]: r for r in entities.list_relations(OWNER, entity_id=ada["id"])}
    assert rows[old["id"]]["status"] == "superseded"


def test_update_replaces_value_and_ratifies_the_new_one(store):
    ada, first, second = _two_employers()
    out = review.resolve(OWNER, "update", f"rel:{first['id']}", value="Northwind")
    new_ref = out["new_ref"]
    assert ratified.is_ratified(OWNER, new_ref)
    current = entities.list_relations(OWNER, entity_id=ada["id"], include_closed=False)
    values = sorted(r["dst_value"] for r in current)
    assert "Northwind" in values and "Cordera Labs" not in values


def test_expired_facts_and_notes_and_renew(store):
    ada = entities.upsert_entity(OWNER, "Ada", type="person")
    gone = entities.add_relation(OWNER, ada["id"], "uses", dst_value="Fax", valid_from="2020-01-01",
                                 valid_until=_iso(-3))
    soon = entities.add_relation(OWNER, ada["id"], "uses", dst_value="Pager", valid_from="2020-01-01",
                                 valid_until=_iso(5))
    entities.add_relation(OWNER, ada["id"], "uses", dst_value="Laptop", valid_from="2020-01-01",
                          valid_until=_iso(400))
    rows = {r["id"]: r for r in review.expired(OWNER)}
    assert rows[f"rel:{gone['id']}"]["state"] == "expired"
    assert rows[f"rel:{soon['id']}"]["state"] == "soon"
    assert len(rows) == 2

    review.resolve(OWNER, "renew", f"rel:{gone['id']}", until=_iso(200))
    assert f"rel:{gone['id']}" not in {r["id"] for r in review.expired(OWNER)}
    review.resolve(OWNER, "archive", f"rel:{soon['id']}")
    assert review.expired(OWNER) == []


def test_expired_note_from_frontmatter(store):
    from src.brain import notes
    notes.write_note(OWNER, "Notes/Promo.md", f"---\ntitle: Promo\ncaduca: {_iso(-2)[:10]}\n---\nBody\n")
    notes.write_note(OWNER, "Notes/Evergreen.md", "---\ntitle: Evergreen\n---\nBody\n")
    found = review.expired(OWNER)
    assert [r["path"] for r in found] == ["Notes/Promo.md"] and found[0]["state"] == "expired"
    ratified.ratify(OWNER, "note:Notes/Promo.md")
    review.resolve(OWNER, "renew", "note:Notes/Promo.md", until=_iso(90)[:10])
    assert review.expired(OWNER) == []
    review.resolve(OWNER, "archive", "note:Notes/Promo.md")
    assert not ratified.is_ratified(OWNER, "note:Notes/Promo.md")


def test_facts_and_profile_carry_the_ratified_flag(store):
    from src.brain import bitemporal
    ada, first, second = _two_employers()
    ratified.ratify(OWNER, f"rel:{first['id']}")
    rows = {r["id"]: r for r in bitemporal.facts(OWNER, entity_id=ada["id"])["facts"]}
    assert rows[first["id"]]["ratified"] is True and rows[second["id"]]["ratified"] is False
    profile = entities.profile(ada["id"])
    flags = {r["id"]: r["ratified"] for r in profile["relations"]}
    assert flags[first["id"]] is True and flags[second["id"]] is False
    ratified.ratify(OWNER, f"ent:{ada['id']}")
    assert entities.profile(ada["id"])["entity"]["ratified"] is True


def test_resolve_rejects_bad_requests(store):
    ada, first, _ = _two_employers()
    with pytest.raises(review.ReviewError):
        review.resolve(OWNER, "explode", f"rel:{first['id']}")
    with pytest.raises(review.ReviewError):
        review.resolve(OWNER, "update", f"rel:{first['id']}")
    with pytest.raises(review.ReviewError):
        review.resolve("bob", "archive", f"rel:{first['id']}")
    with pytest.raises(review.ReviewError):
        review.resolve(OWNER, "renew", f"rel:{first['id']}")


# ── context engine: ratified beats agent-filed ──────────────────────────────


def _ctx_search(query):
    import asyncio

    from src.context_engine import candidates as C
    from src.context_engine.adapters.brain import BrainSource
    from src.context_engine.contracts import (ContextExecution, ContextPolicy, ContextRequest,
                                              ContextTask)

    req = C.RetrievalRequest(
        request=ContextRequest(execution=ContextExecution(owner=OWNER), task=ContextTask(query=query),
                               policy=ContextPolicy()),
        query=query)
    return asyncio.run(BrainSource().search(req))


def test_entity_card_drops_the_unratified_value_and_marks_the_ratified_one(store):
    ada = entities.upsert_entity(OWNER, "Ada Lovelace", type="person")
    old = entities.add_relation(OWNER, ada["id"], "works_at", dst_value="Cordera Labs",
                                valid_from="2023-01-01")
    entities.add_relation(OWNER, ada["id"], "works_at", dst_value="Bluehaven", valid_from="2025-01-01",
                          method="llm")
    # not ratified: the newer claim closed the old one, the card shows Bluehaven
    assert "Bluehaven" in _ctx_search("Ada Lovelace")[0].body
    # ratify the old one, re-open the conflict: the card shows only the ratified value
    ratified.ratify(OWNER, f"rel:{old['id']}")
    entities.add_relation(OWNER, ada["id"], "works_at", dst_value="Quillfeather", valid_from="2026-01-01",
                          method="llm")
    body = _ctx_search("Ada Lovelace")[0].body
    assert "Cordera Labs (ratified)" in body and "Quillfeather" not in body


def test_ratified_entity_and_note_outrank_agent_filed_ones_in_a_conflict(store):
    from src.brain import notes
    from src.context_engine import conflicts

    ada = entities.upsert_entity(OWNER, "Ada Lovelace", type="person")
    card = _ctx_search("Ada Lovelace")[0]
    assert (card.trust_class, card.authority) == ("agent_assertion", "agent_claim")
    ratified.ratify(OWNER, f"ent:{ada['id']}")
    vouched = _ctx_search("Ada Lovelace")[0]
    assert (vouched.trust_class, vouched.authority) == ("human_explicit", "user_instruction")
    assert vouched.meta["ratified"] is True
    assert vouched.authority_rank() > card.authority_rank()

    notes.write_note(OWNER, "Notes/Launch.md", "---\ntitle: Launch\n---\nThe launch is safe to run.\n")
    plain = _ctx_search("launch safe")[0]
    ratified.ratify(OWNER, "note:Notes/Launch.md")
    firm = _ctx_search("launch safe")[0]
    assert plain.authority == "observed_state" and firm.authority == "user_instruction"
    winner, loser, resolution = conflicts._resolve_pair(conflicts.Conflict(a=plain, b=firm, kind="authority"))
    assert winner is firm and loser is plain and resolution.keep_both is False


# ── HTTP routes ─────────────────────────────────────────────────────────────


@pytest.fixture()
def client(store, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from routes import brain_routes

    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setattr(brain_routes, "effective_user", lambda request: request.headers.get("x-test-owner") or "")
    monkeypatch.setattr(brain_routes, "_mirror_entities_later", lambda owner, ids: None)
    app = FastAPI()
    app.include_router(brain_routes.setup_brain_routes())
    return TestClient(app)


H = {"x-test-owner": OWNER}


def test_review_ratify_resolve_over_http(client):
    ada, first, second = _two_employers()
    review_body = client.get("/api/brain/review", headers=H).json()
    assert review_body["counts"]["contradictions"] == 1 and review_body["counts"]["unsettled"] == 1

    r = client.post("/api/brain/ratify", headers=H, json={"ref": f"rel:{first['id']}", "note": "mine"})
    assert r.status_code == 200 and r.json()["ratified"] is True
    assert client.get("/api/brain/ratified", headers=H).json()["count"] == 1
    assert client.get("/api/brain/review", headers=H).json()["counts"]["unsettled"] == 0
    facts = client.get("/api/brain/facts", headers=H, params={"entity_id": ada["id"]}).json()["facts"]
    assert {f["id"]: f["ratified"] for f in facts} == {first["id"]: True, second["id"]: False}

    out = client.post("/api/brain/review/resolve", headers=H,
                      json={"action": "keep", "ref": f"rel:{first['id']}"}).json()
    assert f"rel:{second['id']}" in out["changed"]
    assert client.get("/api/brain/review", headers=H).json()["counts"]["contradictions"] == 0

    off = client.post("/api/brain/ratify", headers=H, json={"ref": f"rel:{first['id']}", "off": True}).json()
    assert off["ratified"] is False and off["removed"] is True


def test_http_errors_and_owner_isolation(client):
    ada, first, _ = _two_employers()
    assert client.post("/api/brain/ratify", headers=H, json={"ref": "nope"}).status_code == 400
    assert client.post("/api/brain/ratify", headers={"x-test-owner": "mallory"},
                       json={"ref": f"rel:{first['id']}"}).status_code == 400
    assert client.post("/api/brain/review/resolve", headers={"x-test-owner": "mallory"},
                       json={"action": "archive", "ref": f"rel:{first['id']}"}).status_code == 400
    assert client.post("/api/brain/review/resolve", headers=H,
                       json={"action": "renew", "ref": f"rel:{first['id']}"}).status_code == 400
    assert client.get("/api/brain/review", headers={"x-test-owner": "mallory"}).json()["counts"][
        "contradictions"] == 0


def test_galaxy_route(client, monkeypatch):
    from src.brain import galaxy

    monkeypatch.setattr(galaxy, "_projects", lambda owner: [{"id": "p1", "name": "Alpha"}])
    monkeypatch.setattr(galaxy, "_agents", lambda owner, limit: [])
    monkeypatch.setattr(galaxy, "_sessions", lambda owner, limit: [])
    monkeypatch.setattr(galaxy, "_outputs", lambda owner, limit: [])
    body = client.get("/api/brain/galaxy", headers=H, params={"kinds": "project,entity"}).json()
    assert body["counts"]["project"] == 1 and body["nodes"][0]["id"] == "project:p1"
