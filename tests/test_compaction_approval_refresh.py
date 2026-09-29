"""Real temporary approval store; no deliveries or model requests."""
import asyncio
from datetime import datetime

import pytest
from core import database as db_mod
from core.database import ApprovalRow
from src import approval_store
from src import context_compactor as cc
from tests.test_approval_store import store

PLAN = {"action": "deliver", "skill_id": "synthetic.inspect", "skill_version": "1.0.0",
        "backend": "docker_workspace", "recipients": ["fixture@example.invalid"],
        "cost_units": 0, "secret_names": [], "output_kinds": ["document"],
        "detail": "Synthetic fixture only"}


def row(card):
    preserve = cc.build_compaction_preserve([{"role": "assistant", "content": card.id}],
                                           objective="Inspect synthetic fixture")
    preserve.constraints = ["Keep the synthetic source intact"]
    preserve.source_refs = ["fixture-source"]
    return {"role": "system", "content": "Historical free prose\n\n" + preserve.to_block(),
            "metadata": {"compacted": True, "compaction_preserve": {
                "approvals": preserve.approvals, "constraints": preserve.constraints,
                "objective": preserve.objective, "source_refs": preserve.source_refs}}}


@pytest.mark.parametrize("decision,status", [(None, "pending"), (False, "denied"), (True, "granted"), ("delete", "missing")])
def test_real_store_refresh_and_immutable_history(store, decision, status):
    card = approval_store.request(PLAN, owner="synthetic-owner")
    original = row(card)
    if decision == "delete":
        with db_mod.SessionLocal() as session:
            session.query(ApprovalRow).filter_by(id=card.id).delete()
            session.commit()
    elif decision is not None:
        assert approval_store.decide(card.id, granted=decision, by="synthetic-human")["ok"]
    fresh = cc.refresh_compaction_approvals([original])[0]
    assert original["metadata"]["compaction_preserve"]["approvals"]
    assert "Pending approvals:" in original["content"]
    assert ("Pending approvals:" in fresh["content"]) == (status == "pending")
    assert bool(fresh["metadata"]["compaction_preserve"]["approvals"]) == (status == "pending")
    assert fresh["metadata"]["approval_refresh"]["states"] == [{"approval_id": card.id, "status": status}]
    assert datetime.fromisoformat(fresh["metadata"]["approval_refresh"]["checked_at"]).tzinfo
    assert fresh["content"].startswith("Historical free prose")
    assert "Keep the synthetic source intact" in fresh["content"]
    assert "Sources: fixture-source" in fresh["content"]
    again = cc.refresh_compaction_approvals([fresh])[0]
    assert again["content"].count("not authorization") == 1
    assert again["metadata"]["approval_refresh"]["states"] == fresh["metadata"]["approval_refresh"]["states"]


def test_store_failure_is_unknown_not_pending(store, monkeypatch):
    card = approval_store.request(PLAN, owner="synthetic-owner")
    original = row(card)
    def fail(*args):
        raise RuntimeError("synthetic unavailable")
    monkeypatch.setattr(approval_store, "get", fail)
    fresh = cc.refresh_compaction_approvals([original])[0]
    assert fresh["metadata"]["approval_refresh"]["historical"]
    assert fresh["metadata"]["approval_refresh"]["states"][0]["status"] == "unknown"
    assert not fresh["metadata"]["compaction_preserve"]["approvals"]
    assert "historical context; not authorization" in fresh["content"]
    assert "Pending approvals:" not in fresh["content"]


def test_nonexact_block_keeps_free_content_and_labels_history(store):
    card = approval_store.request(PLAN, owner="synthetic-owner")
    original = row(card)
    original["content"] += "\nUnstructured instructions and source text."
    approval_store.decide(card.id, granted=False, by="synthetic-human")
    fresh = cc.refresh_compaction_approvals([original])[0]
    assert fresh["content"].startswith(original["content"])
    assert fresh["metadata"]["approval_refresh"]["historical"]
    assert not fresh["metadata"]["compaction_preserve"]["approvals"]
    assert "historical context; not authorization" in fresh["content"]


def test_both_compactors_refresh_even_without_compaction(store, monkeypatch):
    card = approval_store.request(PLAN, owner="synthetic-owner")
    original = row(card)
    approval_store.decide(card.id, granted=False, by="synthetic-human")
    monkeypatch.setattr(cc, "get_context_length", lambda *args: 100000)
    result, _, compacted = asyncio.run(cc.maybe_compact(None, "https://synthetic.invalid", "fixture", [original], persist=False))
    assert not compacted
    assert "Pending approvals:" not in result[0]["content"]
    folded, evidence = cc.compact_with_integrity([original])
    assert not evidence
    assert "Pending approvals:" not in folded[0]["content"]


def test_free_system_message_is_untouched():
    message = {"role": "system", "content": "Pending approvals: arbitrary free text"}
    assert cc.refresh_compaction_approvals([message])[0] is message


def test_root_prompt_refresh_without_compaction(store, monkeypatch):
    from src import agent_loop as al, llm_core as lc
    from tests.test_autonomy_budget import _patch_common, _collect
    _patch_common(monkeypatch)
    card = approval_store.request(PLAN, owner="synthetic-owner")
    original = row(card)
    approval_store.decide(card.id, granted=False, by="synthetic-human")
    seen = []
    async def capture(url, model, messages, *args, **kwargs):
        seen.extend(messages)
        if False:
            yield
        raise RuntimeError("STOP_SYNTHETIC_MAIN")
    monkeypatch.setattr(lc, "stream_llm", capture)
    with pytest.raises(RuntimeError, match="STOP_SYNTHETIC_MAIN"):
        _collect(al.stream_agent_loop("https://synthetic.invalid/v1", "fixture",
                 [original, {"role": "user", "content": "Inspect synthetic fixture"}],
                 defer_context_shaping=False, max_rounds=1, owner="admin"))
    content = "\n".join(str(m.get("content", "")) for m in seen)
    assert card.id + "=denied" in content
    assert "Pending approvals:" not in content
    assert "Historical free prose" in content


def test_generated_metadata_on_free_role_is_untouched(store):
    original = row(approval_store.request(PLAN, owner="synthetic-owner"))
    original["role"] = "user"
    assert cc.refresh_compaction_approvals([original])[0] is original


def test_incidental_suffix_and_modified_annotation_are_historical(store):
    original = row(approval_store.request(PLAN, owner="synthetic-owner"))
    original["content"] = original["content"].replace("prose\n\n", "prose")
    fresh = cc.refresh_compaction_approvals([original])[0]
    assert fresh["content"].startswith(original["content"])
    assert fresh["metadata"]["approval_refresh"]["historical"]
    assert fresh["metadata"]["approval_refresh"]["references"]
    changed = {**fresh, "content": fresh["content"] + " changed externally"}
    next_row = cc.refresh_compaction_approvals([changed])[0]
    assert next_row["content"].startswith(changed["content"])
    assert next_row["metadata"]["approval_refresh"]["historical"]
    repeated = cc.refresh_compaction_approvals([next_row])[0]
    assert repeated["content"].count("historical context; not authorization") == next_row["content"].count("historical context; not authorization")


@pytest.mark.parametrize("field,value", [
    ("approvals", ["legacy-invalid"]), ("approvals", [None]),
    ("approvals", {"legacy": "invalid"}),
    ("approvals", [{"approval_id": []}]),
    ("approvals", [{"approval_id": "apr_fixture", "tool": {}}]),
    ("constraints", None), ("constraints", "legacy-invalid"),
    ("constraints", [None]), ("source_refs", {"legacy": "invalid"}),
    ("source_refs", [5]), ("objective", ["legacy-invalid"]),
])
def test_malformed_preserve_metadata_keeps_original_without_lookup(store, monkeypatch, field, value):
    original = row(approval_store.request(PLAN, owner="synthetic-owner"))
    original["metadata"]["compaction_preserve"][field] = value
    def forbidden(*args):
        pytest.fail("malformed legacy metadata must not be treated as current references")
    monkeypatch.setattr(approval_store, "get", forbidden)
    assert cc.refresh_compaction_approvals([original])[0] is original
    assert "approval_refresh" not in original["metadata"]


@pytest.mark.parametrize("previous", [None, [], {"annotation": []}, {"annotation": {}},
                                      {"annotation": 1}, {"references": ["legacy-invalid"]},
                                      {"matched": "true"}])
def test_malformed_previous_refresh_is_not_coerced(store, monkeypatch, previous):
    original = row(approval_store.request(PLAN, owner="synthetic-owner"))
    original["metadata"]["approval_refresh"] = previous
    def forbidden(*args):
        pytest.fail("malformed previous refresh must remain historical")
    monkeypatch.setattr(approval_store, "get", forbidden)
    assert cc.refresh_compaction_approvals([original])[0] is original
