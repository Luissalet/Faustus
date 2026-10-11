"""Change trace of a fact: update chain, retrieval presentation, isolation."""
import asyncio
import json

import pytest

from src.brain import db as brain_db, entities, fact_history

OWNER = "ana-owner"


@pytest.fixture(autouse=True)
def store(tmp_path):
    brain_db.use_dir(str(tmp_path / "brain"))
    yield
    brain_db.use_dir(None)


def _seed():
    ana = entities.upsert_entity(OWNER, "Ana", type="person")
    x = entities.upsert_entity(OWNER, "Cordera Labs", type="organization")
    y = entities.upsert_entity(OWNER, "Bluehaven", type="organization")
    z = entities.upsert_entity(OWNER, "Northwind", type="organization")
    return ana, x, y, z


def _chain():
    ana, x, y, z = _seed()
    r1 = entities.add_relation(OWNER, ana["id"], "works_at", dst_id=x["id"],
                               valid_from="2024-01-01", evidence=["mem:a"])
    r2 = entities.add_relation(OWNER, ana["id"], "works_at", dst_id=y["id"],
                               valid_from="2025-03-01", evidence=["mem:b"])
    r3 = entities.add_relation(OWNER, ana["id"], "works_at", dst_id=z["id"],
                               valid_from="2026-09-15", evidence=["mem:c"])
    return ana, (r1, r2, r3)


def test_update_chain_links_both_ways():
    ana, (r1, r2, r3) = _chain()
    view = fact_history.fact_history(OWNER, "Ana", "trabaja en")
    assert view["attribute"] == "works_at"
    assert [e["value"] for e in view["events"]] == ["Cordera Labs", "Bluehaven", "Northwind"]
    assert [e["value"] for e in view["current"]] == ["Northwind"]
    e1, e2, e3 = view["events"]
    assert e1["superseded_by"] == r2["id"] and e1["supersedes"] == ""
    assert e2["supersedes"] == r1["id"] and e2["superseded_by"] == r3["id"]
    assert e3["supersedes"] == r2["id"] and e3["superseded_by"] == ""
    assert e1["valid_until"].startswith("2025-03-01")
    assert e2["valid_until"].startswith("2026-09-15")
    assert e1["source"] == "mem:a" and e3["source"] == "mem:c"
    assert view["changes"] == 3
    assert view["history_line"] == "antes: Bluehaven, hasta 2026-09; Cordera Labs, hasta 2025-03"
    # old events are kept as rows, not overwritten
    assert len(entities.list_relations(OWNER, entity_id=ana["id"], include_closed=True)) == 3


def test_unchanged_fact_has_no_history_line():
    ana, x, _, _ = _seed()
    entities.add_relation(OWNER, ana["id"], "works_at", dst_id=x["id"], evidence=["mem:a"])
    view = fact_history.fact_history(OWNER, ana["id"], "works at")
    assert [e["value"] for e in view["current"]] == ["Cordera Labs"]
    assert view["history_line"] == "" and view["changes"] == 0


def test_unrelated_facts_untouched():
    ana, (r1, r2, r3) = _chain()
    tool = entities.upsert_entity(OWNER, "Vim", type="tool")
    other = entities.upsert_entity(OWNER, "Ben", type="person")
    bx = entities.upsert_entity(OWNER, "Initech", type="organization")
    uses = entities.add_relation(OWNER, ana["id"], "uses", dst_id=tool["id"])
    ben = entities.add_relation(OWNER, other["id"], "works_at", dst_id=bx["id"])
    assert fact_history.fact_history(OWNER, "Ana", "uses")["history_line"] == ""
    assert [e["value"] for e in fact_history.fact_history(OWNER, "Ben", "works_at")["current"]] == ["Initech"]
    rows = {r["id"]: r for r in entities.list_relations(OWNER, include_closed=True)}
    assert rows[uses["id"]]["status"] == "active" and rows[uses["id"]]["valid_until"] == ""
    assert rows[ben["id"]]["status"] == "active" and rows[ben["id"]]["superseded_by"] == ""


def test_owner_isolation_and_errors():
    _chain()
    with pytest.raises(ValueError):
        fact_history.fact_history("someone-else", "Ana", "works_at")
    with pytest.raises(ValueError):
        fact_history.fact_history(OWNER, "Ana", "")
    with pytest.raises(ValueError):
        fact_history.fact_history(OWNER, "", "works_at")


def test_attribute_detection():
    assert "works_at" in fact_history.attributes_in_text("¿Dónde trabaja Ana?")
    assert "works_at" in fact_history.attributes_in_text("Where does Ana work now?")
    assert "lives_in" in fact_history.attributes_in_text("donde vive Ana")
    assert fact_history.attributes_in_text("Recuérdame comprar pan") == []


def test_injected_card_shows_history_only_when_asked():
    from src.context_engine.adapters.brain import BrainSource, _entity_body
    ana, _ = _chain()
    lines = fact_history.history_lines_for(OWNER, ana["id"], "¿Dónde trabaja Ana?")
    assert lines == ["works at: Northwind (antes: Bluehaven, hasta 2026-09; Cordera Labs, hasta 2025-03)"]
    assert fact_history.history_lines_for(OWNER, ana["id"], "¿Cómo está Ana?") == []
    profile = entities.profile(ana["id"])
    body = _entity_body(ana, profile, lines)
    assert "antes: Bluehaven, hasta 2026-09" in body
    assert "antes:" not in _entity_body(ana, profile)
    assert BrainSource  # adapter imports cleanly


def test_mcp_tool_and_route_shape():
    pytest.importorskip("mcp")
    import mcp_servers.brain_server as bs
    _chain()
    was_initialized, was_engine = bs._initialized, dict(bs._engine)
    names = {t.name for t in asyncio.run(bs.list_tools())}
    assert "brain_fact_history" in names
    bs._initialized = False
    import os
    os.environ["ODYSSEUS_MCP_BRAIN_OWNER"] = OWNER
    try:
        out = asyncio.run(bs.call_tool("brain_fact_history",
                                       {"subject": "Ana", "attribute": "works at"}))
        data = json.loads(out[0].text)
        assert data["ok"] and data["current"][0]["value"] == "Northwind"
        assert data["history_line"].startswith("antes: Bluehaven")
        bad = asyncio.run(bs.call_tool("brain_fact_history", {"subject": "Nobody", "attribute": "works at"}))
        assert "brain_fact_history:" in bad[0].text
    finally:
        os.environ.pop("ODYSSEUS_MCP_BRAIN_OWNER", None)
        bs._initialized = was_initialized
        bs._engine.clear()
        bs._engine.update(was_engine)
