"""Pending approval cards survive a server restart inside their TTL (live,
24-09-2026: a restart turned a paused exam turn's card into a 409)."""
import json
import time

from src.tool_approvals import ToolApprovalStore
from src.tool_capabilities import capabilities_for_action


def _create(store, session="sess-1"):
    return store.create(
        owner="Admin", session_id=session, origin_run_id="run-1", tool_name="python",
        content="print(1)", workspace="/tmp/ws", external_untrusted_context_seen=True,
        capabilities=capabilities_for_action("python", "print(1)"), continuation_query="solve it",
    )


def test_a_card_created_before_a_restart_can_be_consumed_after_it(tmp_path):
    path = str(tmp_path / "pending.json")
    first = ToolApprovalStore()
    first.enable_persistence(path)
    card = _create(first)
    assert json.load(open(path))[0]["approval_id"] == card.approval_id

    second = ToolApprovalStore()            # the restarted server
    assert second.enable_persistence(path) == 1
    reason, approval = second.consume_with_reason(card.approval_id, decision="approve_task",
                                                  owner="admin", session_id="sess-1")
    assert reason == "consumed" and approval is not None
    assert json.load(open(path)) == []      # consumed: gone from the file too

    third = ToolApprovalStore()             # and it cannot come back
    assert third.enable_persistence(path) == 0


def test_expired_cards_are_not_restored_and_the_ttl_is_not_extended(tmp_path):
    path = str(tmp_path / "pending.json")
    first = ToolApprovalStore(ttl_seconds=60)
    first.enable_persistence(path)
    card = _create(first)
    rows = json.load(open(path))
    rows[0]["expires_at"] = time.time() - 1
    json.dump(rows, open(path, "w"))
    second = ToolApprovalStore()
    assert second.enable_persistence(path) == 0
    assert second.peek(card.approval_id) is None


def test_a_corrupt_file_starts_empty(tmp_path):
    path = tmp_path / "pending.json"
    path.write_text("{not json", encoding="utf-8")
    store = ToolApprovalStore()
    assert store.enable_persistence(str(path)) == 0
    _create(store)
    assert len(json.load(open(path))) == 1


def test_without_persistence_nothing_is_written(tmp_path):
    store = ToolApprovalStore()
    _create(store)
    assert list(tmp_path.iterdir()) == []
