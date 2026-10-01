"""Pending approval cards: dated per chat, and gone with a deleted chat."""
from src.tool_approvals import ToolApprovalStore
from src.tool_capabilities import capabilities_for_tool


def _card(store, sid, owner="alice", content="echo hi"):
    return store.create(owner=owner, session_id=sid, origin_run_id="r", tool_name="bash", content=content,
                        workspace=None, external_untrusted_context_seen=False,
                        capabilities=capabilities_for_tool("bash"))


def test_since_is_the_oldest_card_of_each_chat():
    store = ToolApprovalStore()
    first = _card(store, "s1")
    _card(store, "s1", content="echo two")
    _card(store, "s2")
    since = store.pending_session_since(owner="alice")
    assert set(since) == {"s1", "s2"} and since["s1"] >= first.created_at
    assert store.pending_session_since(owner="bob") == {}


def test_forget_session_drops_its_cards_only():
    store = ToolApprovalStore()
    _card(store, "s1")
    _card(store, "s1", owner="bob", content="echo b")
    _card(store, "s2")
    assert store.forget_session("s1") == 2
    assert store.pending_session_ids(owner="alice") == ["s2"]
    assert store.forget_session("") == 0 and store.forget_session("nope") == 0