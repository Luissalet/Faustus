"""The harness MCP server (mcp_servers/harness_server.py): read-only diagnostics
over the history projector, the resource claims and the paired bench reports.
Same discipline as the other built-in servers - in-process, no stdio transport."""
import asyncio
import json
from pathlib import Path

import pytest

pytest.importorskip("mcp")

import mcp_servers.harness_server as srv
from src import history_projection as hp


def _call(name, arguments):
    return asyncio.run(srv.call_tool(name, arguments))


def _payload(name, arguments):
    return json.loads(_call(name, arguments)[0].text)


def test_server_identity_and_tool_surface():
    assert srv.server.name == "harness"
    tools = asyncio.run(srv.list_tools())
    assert [t.name for t in tools] == ["history_projection", "resource_claims", "paired_bench_reports",
                                       "tool_footprint", "harness_proposals"]
    for t in tools:
        assert len(t.description) > 60
        for required in t.inputSchema.get("required", []):
            assert required in t.inputSchema["properties"]


def test_history_projection_keeps_an_unanswered_call_as_an_unknown_result():
    messages = [
        {"role": "user", "content": "run it"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]},
        {"role": "user", "content": "well?"},
    ]
    out = _payload("history_projection", {"messages": messages, "protocol": "openai_chat"})
    assert out["repair_count"] >= 1
    assert out["repairs"][0]["call_id"] == "c1"
    tool_rows = [m for m in out["projected"] if m["role"] == "tool"]
    assert len(tool_rows) == 1 and tool_rows[0]["tool_call_id"] == "c1"
    assert hp.UNKNOWN_RESULT_TEXT[:20] in tool_rows[0]["content"]


def test_history_projection_reports_nothing_for_a_clean_history_and_does_not_log_receipts():
    hp.reset_receipts_for_tests()
    out = _payload("history_projection", {"messages": [{"role": "user", "content": "hi"},
                                                        {"role": "assistant", "content": "hello"}]})
    assert out["repair_count"] == 0 and out["messages_out"] == 2
    # a diagnostic must not write into the running app's receipt log
    bad = [{"role": "assistant", "content": "", "tool_calls": [
        {"id": "x", "type": "function", "function": {"name": "t", "arguments": "{}"}}]}]
    _payload("history_projection", {"messages": bad})
    assert hp.recent_receipts() == []


def test_history_projection_every_protocol_and_bad_input():
    msgs = [{"role": "user", "content": "hi"}]
    for proto in hp.PROTOCOLS:
        assert _payload("history_projection", {"messages": msgs, "protocol": proto})["protocol"] == proto
    assert "unknown protocol" in _payload("history_projection", {"messages": msgs, "protocol": "x"})["error"]
    assert "must be a list" in _payload("history_projection", {"messages": "no"})["error"]
    assert "at most" in _payload("history_projection", {"messages": [{"role": "user", "content": "x"}] * 500})["error"]


def test_resource_claims_groups_reads_together_and_splits_on_a_write(tmp_path):
    ws = str(tmp_path)
    calls = [
        {"tool": "read_file", "content": json.dumps({"path": "a.py"})},
        {"tool": "read_file", "content": json.dumps({"path": "b.py"})},
        {"tool": "write_file", "content": json.dumps({"path": "a.py", "content": "x"})},
        {"tool": "read_file", "content": json.dumps({"path": "a.py"})},
    ]
    out = _payload("resource_claims", {"calls": calls, "workspace": ws})
    assert out["groups"][0] == [0, 1]
    assert 2 in out["groups"][1] or out["groups"][1] == [2]
    flat = [i for g in out["groups"] for i in g]
    assert flat == [0, 1, 2, 3]
    assert out["serialised"] is True and out["parallel"] is True
    assert any("write" in c for c in out["calls"][2]["claims"])


def test_resource_claims_scope_off_claims_nothing_and_shell_claims_nothing(tmp_path):
    w = {"tool": "write_file", "content": json.dumps({"path": "a.py", "content": "x"})}
    out = _payload("resource_claims", {"calls": [w, w], "workspace": str(tmp_path), "scope": "off"})
    assert out["groups"] == [[0, 1]] and all(c["claims_nothing"] for c in out["calls"])
    sh = _payload("resource_claims", {"calls": [{"tool": "bash", "content": "echo hi"}], "workspace": str(tmp_path)})
    assert sh["calls"][0]["claims_nothing"] is True


def test_resource_claims_bad_input():
    assert "must be a list" in _payload("resource_claims", {"calls": 3})["error"]
    assert "needs a tool" in _payload("resource_claims", {"calls": [{"content": "x"}]})["error"]
    assert "unknown scope" in _payload("resource_claims", {"calls": [], "scope": "x"})["error"]
    assert "at most" in _payload("resource_claims", {"calls": [{"tool": "read_file"}] * 100})["error"]


def test_paired_bench_reports_list_read_and_reject_paths(tmp_path, monkeypatch):
    from src.bench import harness_pair as hpair
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    assert _payload("paired_bench_reports", {})["reports"] == []
    rec = lambda arm: hpair.RunRecord(arm=arm, case="c", repeat=0, success=True, finished=True, seconds=3.0)
    report = hpair.build_report({"mode": "scripted model", "model": "m", "same_model": True},
                                {"baseline": [rec("baseline")], "candidate": [rec("candidate")]},
                                baseline="baseline", candidate="candidate", min_repeats=1)
    d = Path(hpair.reports_dir())
    d.mkdir(parents=True)
    (d / "r1.json").write_text(json.dumps(report))
    listing = _payload("paired_bench_reports", {"limit": "5"})
    assert [r["file"] for r in listing["reports"]] == ["r1.json"]
    got = _payload("paired_bench_reports", {"file": "r1.json"})
    assert "equivalent" in got["summary"] and got["report"]["runs"][0]["case"] == "c"
    assert "file must be" in _payload("paired_bench_reports", {"file": "../x.json"})["error"]
    assert "cannot read" in _payload("paired_bench_reports", {"file": "missing.json"})["error"]


def test_errors_are_messages_not_exceptions():
    assert "Unknown tool" in _call("nope", {})[0].text
    assert json.loads(asyncio.run(srv.call_tool("resource_claims", None))[0].text)["error"]


def test_registered_as_a_builtin_server():
    from src import builtin_mcp

    script, name = builtin_mcp._BUILTIN_SERVERS["harness"]
    assert script == "mcp_servers/harness_server.py"
    assert (Path(__file__).parent.parent / script).is_file()
    assert "harness" not in builtin_mcp.NATIVE_TWIN_SERVERS  # there is no native twin of these tools


def test_tool_footprint_builtin_and_supplied_catalogues():
    builtin = _payload("tool_footprint", {"top": 3})
    assert builtin["catalogue"] == "builtin"
    assert builtin["count"] > 50 and len(builtin["heaviest"]) == 3
    assert builtin["total_tokens"] == builtin["offered_tokens"] + builtin["deferred_tokens"]
    supplied = _payload("tool_footprint", {"similarity": 0.5, "tools": [
        {"name": "search", "description": "Search saved articles by keyword, tag and date range.", "server": "links"},
        {"name": "search", "description": "Search indexed documents by keyword, tag and date range.", "server": "borges"},
    ]})
    assert supplied["catalogue"] == "supplied" and supplied["count"] == 2
    assert {b["source"] for b in supplied["by_source"]} == {"links", "borges"}
    assert supplied["near_duplicates"][0]["same_source"] is False
    assert supplied["name_collisions"][0]["tools"] == ["mcp__borges__search", "mcp__links__search"]
    assert "must be a list" in _payload("tool_footprint", {"tools": "nope"})["error"]


def test_harness_proposals_lists_and_reads_but_cannot_apply(tmp_path, monkeypatch):
    from src.harness_refinement import store

    monkeypatch.setattr(store, "DATA_DIR", str(tmp_path))
    rec = store.create_proposal(
        axis="memory", target="memory:new", op="create", before=None, after="Dates in ISO format.",
        rationale="corrected", evidence=[{"id": "e1", "kind": "correction", "quote": "ISO please"}],
        trigger="manual", session_id="s1")
    listing = _payload("harness_proposals", {})
    assert [p["id"] for p in listing["proposals"]] == [rec["id"]]
    assert "before" not in listing["proposals"][0] and listing["counts"]["pending"] == 1
    one = _payload("harness_proposals", {"id": rec["id"]})
    assert one["proposal"]["after"] == "Dates in ISO format." and one["proposal"]["status"] == "pending"
    assert _payload("harness_proposals", {"status": "applied"})["proposals"] == []
    assert "no proposal" in _payload("harness_proposals", {"id": "nope"})["error"]
    # there is no way to decide through this server
    tools = {t.name: t for t in asyncio.run(srv.list_tools())}
    assert set(tools["harness_proposals"].inputSchema["properties"]) == {"id", "status", "axis", "limit"}
    assert store.get(rec["id"])["status"] == "pending"
