"""OBJ-56 (#327): the tools each turn really offered and what they weighed (src/turn_tool_offer.py)."""

import json

import pytest

from src import tool_footprint as tf
from src import turn_tool_offer as tto


def _tool(name, desc="Reads a file from the workspace and returns its text.", props=None):
    return {"type": "function", "function": {"name": name, "description": desc,
                                             "parameters": {"type": "object", "properties": props or {"path": {"type": "string"}}}}}


@pytest.fixture(autouse=True)
def _clean():
    tto._TURNS.clear()
    yield
    tto._TURNS.clear()


def test_record_and_take_sum_like_the_catalogue_footprint():
    tools = [_tool("read_file"), _tool("web_search", "Searches the web.", {"q": {"type": "string"}}),
             {"type": "function", "function": {"name": "mcp__links__media_download", "description": "Download a video.",
                                               "parameters": {"type": "object", "properties": {"url": {"type": "string"}}}},
              "server": "links"}]
    tto.record("s1", 1, tools, "")
    got = tto.take("s1")
    expected = tf.tool_costs(tf.rows_from_mappings(tools), count=tf.default_counter(""))
    assert got["count"] == 3
    assert got["tokens"] == sum(c["tokens"] for c in expected)            # same estimator and shape as the footprint
    assert got["sent_tokens"] == got["tokens"] and got["rounds"] == [{"round": 1, "count": 3, "tokens": got["tokens"]}]
    assert got["heaviest"][0]["tokens"] >= got["heaviest"][-1]["tokens"]
    assert set(got["by_source"]) == {"builtin", "links"}
    assert tto.take("s1") is None                                           # taken once


def test_rounds_union_and_new_turn_reset():
    tto.record("s2", 1, [_tool("read_file")], "")
    tto.record("s2", 2, [_tool("read_file"), _tool("write_file", "Writes a file.")], "")
    live = tto.peek("s2")
    assert live["running"] and live["count"] == 2 and len(live["rounds"]) == 2
    got = tto.take("s2")
    assert got["count"] == 2                                                # distinct tools over the turn
    assert got["sent_tokens"] == sum(r["tokens"] for r in got["rounds"]) > got["tokens"]
    tto.record("s2", 1, [_tool("read_file")], "")
    tto.record("s2", 1, [_tool("grep", "Searches text.")], "")             # a new round 1 is a new turn
    again = tto.take("s2")
    assert [h["name"] for h in again["heaviest"]] == ["grep"] and len(again["rounds"]) == 1


def test_no_tools_and_bad_input_never_raise():
    tto.record("", 1, [_tool("x")])
    tto.record("s3", "not-a-number", None)
    got = tto.take("s3")
    assert got["count"] == 0 and got["tokens"] == 0
    assert tto.take("nobody") is None


def test_stamp_on_saved_metadata():
    from routes.chat_helpers import _stamp_tools_offered
    tto.record("s4", 1, [_tool("read_file")], "")
    md = {}
    _stamp_tools_offered(md, "s4")
    assert md["tools_offered"]["count"] == 1
    kept = {"tools_offered": {"count": 9}}
    tto.record("s4", 1, [_tool("read_file")], "")
    _stamp_tools_offered(kept, "s4")
    assert kept["tools_offered"] == {"count": 9}                            # an existing record is not replaced


def test_report_reads_saved_turns():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from core.database import Base, ChatMessage, Session

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Session.__table__, ChatMessage.__table__])
    db = sessionmaker(bind=engine)()
    db.add(Session(id="sa", name="a", owner="admin", endpoint_url="http://x", model="m"))
    db.add(Session(id="sb", name="b", owner="otro", endpoint_url="http://x", model="m"))
    tto.record("sa", 1, [_tool("read_file")], "")
    offered = tto.take("sa")
    db.add(ChatMessage(id="m1", session_id="sa", role="assistant", content="ok",
                       meta_data=json.dumps({"tools_offered": offered})))
    db.add(ChatMessage(id="m2", session_id="sa", role="assistant", content="sin", meta_data=json.dumps({"x": 1})))
    db.add(ChatMessage(id="m3", session_id="sb", role="assistant", content="ok",
                       meta_data=json.dumps({"tools_offered": {"count": 2, "tokens": 50}})))
    db.commit()
    by_session = tto.report("sa", db=db)
    assert [t["message_id"] for t in by_session["turns"]] == ["m1"] and by_session["turns"][0]["count"] == 1
    assert tto.report(message_id="m3", db=db)["turns"][0]["tokens"] == 50
    assert {t["message_id"] for t in tto.report(owner="admin", db=db)["turns"]} == {"m1"}
    assert len(tto.report(db=db)["turns"]) == 2


def test_every_tool_is_kept_not_only_the_heaviest():
    from routes.chat_helpers import _stamp_tools_offered
    names = [f"fixture_{i:02d}" for i in range(11)]
    tto.record("s5", 1, [_tool(n, "Fixture tool " + "x" * i) for i, n in enumerate(names)], "")
    md = {}
    _stamp_tools_offered(md, "s5")
    offered = md["tools_offered"]
    assert offered["count"] == 11 and len(offered["heaviest"]) == tto.TOP
    assert sorted(t["name"] for t in offered["tools"]) == names              # fixture_10 is not lost
    assert sum(t["tokens"] for t in offered["tools"]) == offered["tokens"]
    assert all(t["source"] == "builtin" for t in offered["tools"])


def test_report_returns_the_full_list(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from core.database import Base, ChatMessage, Session

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Session.__table__, ChatMessage.__table__])
    db = sessionmaker(bind=engine)()
    db.add(Session(id="sf", name="f", owner="admin", endpoint_url="http://x", model="m"))
    tto.record("sf", 1, [_tool(f"fixture_{i:02d}") for i in range(12)], "")
    db.add(ChatMessage(id="mf", session_id="sf", role="assistant", content="ok",
                       meta_data=json.dumps({"tools_offered": tto.take("sf")})))
    db.commit()
    turn = tto.report("sf", db=db)["turns"][0]
    assert len(turn["tools"]) == 12 and len(turn["heaviest"]) == tto.TOP


def test_mcp_schemas_from_the_manager_keep_their_server():
    # The exact shape MCPManager.get_all_openai_schemas hands the loop: only the qualified name says the server.
    tools = [
        {"type": "function", "function": {"name": "mcp__fixture_server__read", "description": "Read a record.",
                                          "parameters": {"type": "object", "properties": {"id": {"type": "string"}}}}},
        {"type": "function", "function": {"name": "mcp__2b27c03c__search", "description": "Search the archive.",
                                          "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}}},
        _tool("read_file"),
    ]
    tto.record("s6", 1, tools, "")
    got = tto.take("s6")
    assert set(got["by_source"]) == {"fixture_server", "2b27c03c", "builtin"}
    assert {t["name"]: t["source"] for t in got["tools"]} == {
        "mcp__fixture_server__read": "fixture_server", "mcp__2b27c03c__search": "2b27c03c", "read_file": "builtin"}


def test_a_running_turn_outlives_newer_abandoned_ones(monkeypatch):
    monkeypatch.setattr(tto, "_MAX_SESSIONS", 4)
    tto.record("active", 1, [_tool("read_file")], "")
    for i in range(3):
        tto.record(f"abandoned{i}", 1, [_tool("read_file")], "")
    tto.record("active", 2, [_tool("read_file")], "")                     # its next round refreshes it
    for i in range(3, 6):
        tto.record(f"abandoned{i}", 1, [_tool("read_file")], "")
    assert tto.peek("active") is not None and len(tto.peek("active")["rounds"]) == 2
    assert tto.peek("abandoned0") is None
