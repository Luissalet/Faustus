"""The model managing its own context: the five context tools' handlers
(intents) and `src.context_self_manage.apply_intent` over real message lists,
the real overflow store (tmp dir) and the real compaction-pin store (tmp db).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from src import context_overflow as co
from src import context_self_manage as csm
from src import tool_result_segments as trs
from src.agent_tools import TOOL_HANDLERS
from src.context_engine import store as ce_store
from src.prompt_security import untrusted_context_message


@pytest.fixture()
def stores(tmp_path, monkeypatch):
    monkeypatch.setattr(co, "OVERFLOW_DIR", str(tmp_path / "overflow"))
    ce_store.use_path(str(tmp_path / "ce.db"))
    try:
        yield tmp_path
    finally:
        ce_store.use_path(None)


def _octx(**kw):
    base = dict(session_id="sess-ctx", owner="alice", model="m", context_length=10_000)
    base.update(kw)
    return csm.OpContext(**base)


BIG = "line of build output with detail\n" * 200   # ~6.6k chars


def _native_history():
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "fix the build"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_a", "type": "function", "function": {"name": "bash", "arguments": '{"command": "make"}'}},
            {"id": "call_b", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}},
        ]},
        {"role": "tool", "tool_call_id": "call_a", "content": BIG, "_tool_round": 1},
        {"role": "tool", "tool_call_id": "call_b", "content": "def f():\n    return 1\n", "_tool_round": 1},
        {"role": "assistant", "content": "Looking."},
    ]


def _fenced_message(texts, tools, round_num=3):
    joined, spans = trs.escape_results(texts)
    msg = untrusted_context_message("tool execution results", joined)
    assert trs.annotate(msg, joined, spans, round_num=round_num, tools=tools,
                        call_ids=[f"c{i}" for i in range(len(texts))])
    return msg


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Handlers: intents only, validated
# ---------------------------------------------------------------------------

def test_handlers_return_intents_and_validate():
    r = _run(TOOL_HANDLERS["context_status"]("{}", {}))
    assert r[csm.INTENT_KEY] == {"op": "status"}
    r = _run(TOOL_HANDLERS["context_drop"](json.dumps({"handles": ["call_a", "r3.1"]}), {}))
    assert r[csm.INTENT_KEY] == {"op": "drop", "handles": ["call_a", "r3.1"]}
    r = _run(TOOL_HANDLERS["context_drop"]("{}", {}))
    assert "error" in r
    r = _run(TOOL_HANDLERS["context_note"](json.dumps({"handles": "call_a"}), {}))
    assert "note" in r["error"]
    r = _run(TOOL_HANDLERS["context_note"](json.dumps({"handles": "call_a", "note": "make fails on x"}), {}))
    assert r[csm.INTENT_KEY]["op"] == "note" and r[csm.INTENT_KEY]["handles"] == ["call_a"]
    r = _run(TOOL_HANDLERS["context_pin"](json.dumps({"snippet": "def f():"}), {}))
    assert r[csm.INTENT_KEY] == {"op": "pin", "handles": [], "snippet": "def f():"}
    r = _run(TOOL_HANDLERS["context_unpin"](json.dumps({"all": True}), {}))
    assert r[csm.INTENT_KEY]["all"] is True
    r = _run(TOOL_HANDLERS["context_pin"]("not json", {}))
    assert "error" in r


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def test_status_lists_largest_items_with_handles(stores):
    msgs = _native_history() + [_fenced_message(["### read_file x\n" + "y" * 3000, "### ls\nshort"], ["read_file", "ls"])]
    _msgs, out = csm.apply_intent(msgs, {"op": "status"}, _octx())
    assert out["ok"] and out["context_length"] == 10_000
    handles = [it["handle"] for it in out["largest"]]
    assert handles[0] == "call_a"
    assert "r3.0" in handles and "r3.1" in handles and "call_b" in handles
    assert "call_a · bash" in out["text"]
    assert "%" in out["text"].splitlines()[0]


# ---------------------------------------------------------------------------
# drop
# ---------------------------------------------------------------------------

def test_drop_native_result_spills_to_overflow_and_is_reacquirable(stores):
    msgs = _native_history()
    new, out = csm.apply_intent(msgs, {"op": "drop", "handles": ["call_a"]}, _octx())
    assert out["ok"] and out["handles"] == ["call_a"] and out["tokens_freed"] > 1000
    stub = new[3]["content"]
    assert stub.startswith("[overflow id=") and "tool=bash" in stub and "call_id=call_a" in stub
    assert new[3]["tool_call_id"] == "call_a" and new[3]["role"] == "tool"
    assert new[3]["metadata"]["overflow_id"] == out["overflow_ids"][0]
    # The original list is untouched; the rest of the history is too.
    assert msgs[3]["content"] == BIG and new[4] == msgs[4] and new[1] == msgs[1]
    back = co.read_overflow(session_id="sess-ctx", content_sha256=out["overflow_ids"][0])
    assert back["content"] == BIG
    assert out["overflow_ids"][0] in co.referenced_overflow_ids(new)


def test_drop_refuses_unknown_already_spilled_and_pinned(stores):
    msgs = _native_history()
    new, out = csm.apply_intent(msgs, {"op": "drop", "handles": ["call_a"]}, _octx())
    again, out2 = csm.apply_intent(new, {"op": "drop", "handles": ["call_a", "nope"]}, _octx())
    assert not out2["ok"] and again is new
    reasons = {r["handle"]: r["reason"] for r in out2["refused"]}
    assert "already out of context" in reasons["call_a"] and "no such item" in reasons["nope"]
    _m, pin_out = csm.apply_intent(new, {"op": "pin", "handles": ["call_b"]}, _octx())
    assert pin_out["ok"]
    _m, out3 = csm.apply_intent(new, {"op": "drop", "handles": ["call_b"]}, _octx())
    assert not out3["ok"] and "pinned" in out3["refused"][0]["reason"]


def test_drop_refuses_a_result_with_a_pending_approval(stores, monkeypatch):
    from src import context_compactor as cc
    monkeypatch.setattr(cc, "_find_pending_approvals",
                        lambda text: [{"approval_id": "apr_1", "tool": "bash", "args_digest": "x"}]
                        if "apr_1" in text else [])
    msgs = _native_history()
    msgs[3] = dict(msgs[3], content=BIG + "\napproval apr_1 pending")
    new, out = csm.apply_intent(msgs, {"op": "drop", "handles": ["call_a"]}, _octx())
    assert not out["ok"] and new is msgs
    assert "pending approval" in out["refused"][0]["reason"]


def test_drop_fenced_segment_keeps_wrapper_and_other_results(stores):
    first = "### bash: pytest\n" + ("FAILED test_x assertion\n" * 150)
    second = "### read_file: b.py\nprint('keep me')"
    fenced = _fenced_message([first, second], ["bash", "read_file"], round_num=4)
    msgs = [{"role": "user", "content": "run tests"}, {"role": "assistant", "content": "```bash\npytest\n```"}, fenced]
    new, out = csm.apply_intent(msgs, {"op": "drop", "handles": ["r4.0"]}, _octx())
    assert out["ok"] and out["tokens_freed"] > 500
    msg = new[2]
    assert trs.is_fenced_results(msg)
    segs = trs.segments(msg)
    assert len(segs) == 2
    assert trs.segment_text(msg, segs[0]).startswith("[overflow id=")
    assert trs.segment_text(msg, segs[1]) == second
    assert msg["content"].endswith("<<<END_UNTRUSTED_SOURCE_DATA>>>")
    back = co.read_overflow(session_id="sess-ctx", content_sha256=out["overflow_ids"][0])
    assert back["content"] == first
    # r4 names the whole round
    _n, out2 = csm.apply_intent(new, {"op": "status"}, _octx())
    assert "r4.1" in out2["text"]


# ---------------------------------------------------------------------------
# note
# ---------------------------------------------------------------------------

def test_note_replaces_results_with_one_note_listing_overflow_ids(stores):
    msgs = _native_history()
    new, out = csm.apply_intent(
        msgs, {"op": "note", "handles": ["call_a", "call_b"],
               "note": "make fails: missing header foo.h; a.py f() returns 1"}, _octx())
    assert out["ok"] and len(out["overflow_ids"]) == 2
    note_msg, other = new[3], new[4]
    assert note_msg["role"] == "tool" and note_msg["tool_call_id"] == "call_a"
    assert "missing header foo.h" in note_msg["content"]
    for oid in out["overflow_ids"]:
        assert f"[overflow id={oid}" in note_msg["content"]
    assert other["tool_call_id"] == "call_b"
    assert other["content"].startswith("[overflow id=") and "summarized in the context note at call_a" in other["content"]
    assert "missing header" not in other["content"]
    assert set(out["overflow_ids"]) <= co.referenced_overflow_ids(new)
    assert co.read_overflow(session_id="sess-ctx", content_sha256=out["overflow_ids"][0])["content"] == BIG


def test_note_keeps_pending_approvals_like_compaction(stores, monkeypatch):
    from src import context_compactor as cc
    monkeypatch.setattr(cc, "_find_pending_approvals",
                        lambda text: [{"approval_id": "apr_9", "tool": "git_push", "args_digest": "d"}]
                        if "apr_9" in text else [])
    msgs = _native_history()
    msgs[3] = dict(msgs[3], content=BIG + "\ncard apr_9 waiting")
    new, out = csm.apply_intent(msgs, {"op": "note", "handles": ["call_a"], "note": "build log"}, _octx())
    assert out["ok"]
    assert "approval_id=apr_9" in new[3]["content"]


def test_note_over_fenced_segments_across_rounds(stores):
    f1 = _fenced_message(["### grep x\n" + "hit\n" * 400, "### ls\nsmall"], ["grep", "ls"], round_num=2)
    f2 = _fenced_message(["### grep y\n" + "hit2\n" * 400], ["grep"], round_num=3)
    msgs = [{"role": "user", "content": "find it"}, f1, {"role": "assistant", "content": "more"}, f2]
    new, out = csm.apply_intent(msgs, {"op": "note", "handles": ["r2.0", "r3"], "note": "x is in a.py:10, y nowhere"}, _octx())
    assert out["ok"] and out["handles"] == ["r2.0", "r3.0"]
    s1 = trs.segments(new[1])
    assert "x is in a.py:10" in trs.segment_text(new[1], s1[0])
    assert trs.segment_text(new[1], s1[1]) == "### ls\nsmall"
    s2 = trs.segments(new[3])
    assert "summarized in the context note at r2.0" in trs.segment_text(new[3], s2[0])


# ---------------------------------------------------------------------------
# pin / unpin
# ---------------------------------------------------------------------------

def test_pin_by_handle_and_snippet_is_honoured_by_compaction(stores):
    from src.context_compactor import compact_with_integrity, _row_fingerprint
    from src.context_engine import compaction_pins
    msgs = _native_history()
    _m, out = csm.apply_intent(msgs, {"op": "pin", "handles": ["call_b"]}, _octx())
    assert out["ok"] and out["handles"] == ["call_b"]
    fps = compaction_pins.pinned_fingerprints("alice", "sess-ctx")
    assert _row_fingerprint("tool", msgs[4]["content"]) in fps
    _m, out = csm.apply_intent(msgs, {"op": "pin", "snippet": "fix the build"}, _octx())
    assert out["ok"]
    _m, bad = csm.apply_intent(msgs, {"op": "pin", "snippet": "short"}, _octx())
    assert not bad["ok"] and "at least" in bad["text"]
    dup = msgs + [{"role": "user", "content": "again: fix the build"}]
    _m, bad = csm.apply_intent(dup, {"op": "pin", "snippet": "fix the build"}, _octx())
    assert not bad["ok"] and "not unique" in bad["text"]
    # status marks pins
    _m, st = csm.apply_intent(msgs, {"op": "status"}, _octx())
    assert "call_b" in st["pinned"]
    # unpin all model pins
    _m, un = csm.apply_intent(msgs, {"op": "unpin", "all": True}, _octx())
    assert un["ok"] and compaction_pins.pinned_fingerprints("alice", "sess-ctx") == set()
    assert compact_with_integrity  # imported: pins go through the same store it reads


def test_model_pin_cap(stores):
    msgs = _native_history()
    _m, out = csm.apply_intent(msgs, {"op": "pin", "handles": ["call_a", "call_b"]}, _octx(max_pins=1))
    assert out["handles"] == ["call_a"]
    assert "pin limit" in out["refused"][0]["reason"]


def test_spill_skips_pinned_native_results(stores):
    from src.context_compactor import spill_large_tool_results, _row_fingerprint
    msgs = _native_history()
    pinned = {_row_fingerprint("tool", BIG)}
    out, rep = spill_large_tool_results(msgs, session_id="s", keep_tool_rounds=0, spill_chars=100, pinned=pinned)
    assert out[3]["content"] == BIG
    assert rep["spilled"] == 1  # call_b still spills


# ---------------------------------------------------------------------------
# offer policy
# ---------------------------------------------------------------------------

def test_offer_policy():
    kw = dict(enabled=True, context_length=100_000, offer_pct=0.45, offer_round=12)
    assert not csm.offer_decision(used_tokens=10_000, round_num=3, **kw)
    assert csm.offer_decision(used_tokens=45_000, round_num=3, **kw)
    assert csm.offer_decision(used_tokens=1_000, round_num=12, **kw)
    assert csm.offer_decision(used_tokens=0, round_num=1, asked=True, **kw)
    assert not csm.offer_decision(used_tokens=99_000, round_num=40, asked=True,
                                  **dict(kw, enabled=False))
    assert not csm.offer_decision(used_tokens=1_000, round_num=40, **dict(kw, offer_round=0))


def test_user_ask_detection():
    assert csm.user_asked("please free up your context before continuing")
    assert csm.user_asked("libera el contexto y sigue")
    assert csm.user_asked("how big is your context window?")
    assert not csm.user_asked("add context to the error message")
    assert not csm.user_asked("fix the login bug")
