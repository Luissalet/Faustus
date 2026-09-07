from dataclasses import replace

import pytest

from src.context_engine.contracts import ContextPacket, ContextSection, ContextItem, ContextOmission
from src.context_engine.manifest import summarize
from src.memory_view import from_context_packet


def packet():
    return ContextPacket(request_id="request", sections=(ContextSection(kind="retrieved_memory", items=(
        ContextItem(source_type="memory", source_ref="mem:one", body="Selected", source_revision="1"),
        ContextItem(source_type="document", source_ref="doc:two", body="Not memory"),
    )),), omissions=(ContextOmission(source_ref="mem:three", reason="budget"),))


def test_summary_uses_final_packet_without_fetching_or_reselecting():
    p = packet()
    summary = summarize(p)
    view = summary["memory_view"]
    assert len(view["entry_ids"]) == 1
    assert len(view["dropped"]) == 1
    assert view["dropped"][0]["reason"] == "budget"
    assert view["used_chars"] == len("Selected")
    assert view["budget_chars"] is None
    assert "budget: 1" in summary["memory_explanation"]
    assert p == packet()


def test_same_source_changed_content_changes_memory_fingerprint():
    p = packet()
    item = replace(p.items()[0], body="Changed")
    changed = replace(p, sections=(replace(p.sections[0], items=(item,)),))
    assert from_context_packet(p).fingerprint() != from_context_packet(changed).fingerprint()


@pytest.mark.parametrize("reason,expected", [("unauthorised", "scope"), ("unavailable", "unavailable"),
    ("policy", "policy"), ("quarantined", "quarantined"), ("contradicted", "conflict")])
def test_drop_reasons_are_preserved_without_rejected_content(reason, expected):
    p = replace(packet(), omissions=(ContextOmission(source_ref="secret-owner-label", reason=reason,
                                                   detail="private-rejected-content"),))
    view = from_context_packet(p).to_dict()
    assert view["dropped"][0]["reason"] == expected
    assert "private-rejected-content" not in str(view)
    assert "secret-owner-label" not in str(view)


def test_degraded_and_empty_packets_do_not_invent_memories():
    view = from_context_packet(ContextPacket(degraded=True))
    assert not view.entry_ids
    assert view.used_chars == 0
    assert view.degraded and view.degraded_reason


def test_large_receipt_is_explicitly_bounded_without_breaking_context_summary():
    items = tuple(ContextItem(source_type="memory", source_ref=f"mem:{i}", body="x") for i in range(2001))
    p = ContextPacket(sections=(ContextSection(kind="retrieved_memory", items=items),))
    view = from_context_packet(p)
    assert len(view.entry_ids) == 2000 and view.used_chars == 2001
    assert view.degraded and '2000 of 2001' in view.degraded_reason
    changed = replace(p, sections=(replace(p.sections[0], items=items[:-1] + (replace(items[-1], body='y'),)),))
    assert view.fingerprint() != from_context_packet(changed).fingerprint()
    assert summarize(p)['memory_view']['degraded']
