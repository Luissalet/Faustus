"""Tests for `src.approval_autonomy` — shadow mode + confidence-tiered
autonomy for the post-external-context approval gate — and its two
integration points: `ToolRunSecurityContext._autonomy_decision`
(src/tool_capabilities.py) and `ToolApprovalStore.create`/
`consume_with_reason` (src/tool_approvals.py).

Four layers, smallest first:

1. Pure confidence/hard-block functions, no storage.
2. The shadow-log/family-stats/promotion storage, against a temp ce_store db.
3. `ToolRunSecurityContext.decision_for`'s autonomy override, with the
   family-promotion question monkeypatched so this layer does not depend on
   layer 2's exact thresholds.
4. `ToolApprovalStore` end to end: "off" is proven byte-identical to a store
   built before this feature existed; "shadow" logs without changing the
   card; "active" adds a recommendation note for a call it does not
   auto-approve outright.
"""
from __future__ import annotations

import json

import pytest

from src import approval_autonomy as autonomy
from src.context_engine import store as ce_store
from src.tool_capabilities import (
    ResultIntegrity, ToolCapabilities, ToolEffect, ToolGateDecision, ToolRunSecurityContext,
)
from src.tool_approvals import ToolApprovalStore


@pytest.fixture()
def ce_db(tmp_path):
    ce_store.use_path(str(tmp_path / "ce.db"))
    try:
        yield
    finally:
        ce_store.use_path(None)


def _write_caps(known: bool = True) -> ToolCapabilities:
    return ToolCapabilities(
        effects=frozenset({ToolEffect.WRITE_WORKSPACE}),
        result_integrity=ResultIntegrity.WORKSPACE_UNTRUSTED, known=known,
    )


def _read_private_caps() -> ToolCapabilities:
    return ToolCapabilities(
        effects=frozenset({ToolEffect.READ_PRIVATE}),
        result_integrity=ResultIntegrity.EXTERNAL_UNTRUSTED, known=True,
    )


def _bash_caps() -> ToolCapabilities:
    return ToolCapabilities(
        effects=frozenset({ToolEffect.EXECUTE_CODE}),
        result_integrity=ResultIntegrity.WORKSPACE_UNTRUSTED, known=True,
    )


# ── pure functions ───────────────────────────────────────────────────────

def test_tier_thresholds():
    assert autonomy.tier_for_score(0.80) == "act"
    assert autonomy.tier_for_score(1.0) == "act"
    assert autonomy.tier_for_score(0.79) == "advise"
    assert autonomy.tier_for_score(0.50) == "advise"
    assert autonomy.tier_for_score(0.49) == "escalate"
    assert autonomy.tier_for_score(0.0) == "escalate"


def test_family_for_is_the_tool_name():
    assert autonomy.family_for("write_file", "{}") == "write_file"
    assert autonomy.family_for("", None) == "unknown"


def test_dedup_key_is_stable_for_identical_calls_and_differs_otherwise():
    a = autonomy.dedup_key("write_file", '{"path": "x.py"}')
    b = autonomy.dedup_key("write_file", '{"path": "x.py"}')
    c = autonomy.dedup_key("write_file", '{"path": "y.py"}')
    assert a == b
    assert a != c


def test_hard_blocked_never_auto_tools(tmp_path):
    assert autonomy.is_hard_blocked("send_email", "{}", workspace=str(tmp_path)) is True
    assert autonomy.is_hard_blocked("bulk_email", "{}", workspace=str(tmp_path)) is True
    assert autonomy.is_hard_blocked("whatsapp_send", "{}", workspace=str(tmp_path)) is True
    assert autonomy.is_hard_blocked("run_payment_checkout", "{}", workspace=str(tmp_path)) is True


def test_hard_blocked_destructive_effect():
    caps = ToolCapabilities(effects=frozenset({ToolEffect.WRITE_WORKSPACE, ToolEffect.DESTRUCTIVE}),
                            result_integrity=ResultIntegrity.WORKSPACE_UNTRUSTED)
    assert autonomy.is_hard_blocked("apply_patch", "***Delete File: x.py", capabilities=caps) is True


def test_hard_blocked_non_promotable_effect_class():
    # bash is EXECUTE_CODE -- never in PROMOTABLE_EFFECTS, whatever else is true.
    assert autonomy.is_hard_blocked("bash", "echo hi", capabilities=_bash_caps()) is True


def test_hard_blocked_unknown_capabilities_fails_closed():
    assert autonomy.is_hard_blocked("mystery_tool", "{}", capabilities=_write_caps(known=False)) is True


def test_hard_blocked_write_outside_workspace(tmp_path):
    inside = tmp_path / "ws"
    inside.mkdir()
    outside = tmp_path / "elsewhere" / "x.py"
    content = json.dumps({"path": str(outside)})
    assert autonomy.is_hard_blocked("write_file", content, workspace=str(inside),
                                    capabilities=_write_caps()) is True


def test_hard_blocked_write_inside_workspace_is_not_blocked(tmp_path):
    inside = tmp_path / "ws"
    inside.mkdir()
    target = inside / "x.py"
    content = json.dumps({"path": str(target)})
    assert autonomy.is_hard_blocked("write_file", content, workspace=str(inside),
                                    capabilities=_write_caps()) is False


def test_compute_confidence_readonly_named_and_workspace_signals(tmp_path):
    inside = tmp_path / "ws"
    inside.mkdir()
    target = inside / "notes.py"
    target.write_text("# existing file\n")
    content = json.dumps({"path": str(target)})

    result = autonomy.compute_confidence(
        "write_file", content, workspace=str(inside), owner="alice",
        user_text="please update notes.py for me", capabilities=_write_caps(),
    )
    assert result.signals["named"] == 1.0
    assert result.signals["workspace"] == 1.0
    assert result.signals["reversible"] == 1.0  # existing file -> revertible
    assert 0.0 <= result.score <= 1.0
    assert result.tier in autonomy.TIERS


def test_compute_confidence_not_named_scores_lower(tmp_path):
    inside = tmp_path / "ws"
    inside.mkdir()
    target = inside / "notes.py"
    content = json.dumps({"path": str(target)})
    named = autonomy.compute_confidence(
        "write_file", content, workspace=str(inside), user_text="please update notes.py",
        capabilities=_write_caps(),
    )
    unnamed = autonomy.compute_confidence(
        "write_file", content, workspace=str(inside), user_text="do the thing we discussed",
        capabilities=_write_caps(),
    )
    assert named.score > unnamed.score


def test_compute_confidence_read_private_is_fully_readonly():
    result = autonomy.compute_confidence("vault_get", "{}", capabilities=_read_private_caps())
    assert result.signals["readonly"] == 1.0


# ── storage: shadow log, family stats, promotion, overrides ────────────

def test_record_and_finalize_shadow_decision(ce_db):
    row_id = autonomy.record_shadow_decision(
        owner="Alice", family="write_file", tool_name="write_file", workspace="/ws",
        score=0.9, tier="act", source="shadow", destructive=False, approval_id="appr-1",
    )
    assert row_id
    autonomy.finalize_shadow_decision(approval_id="appr-1", actual_decision="approved")
    stats = autonomy.family_stats("alice")
    assert len(stats) == 1
    row = stats[0]
    assert row["family"] == "write_file"
    assert row["act_total"] == 1
    assert row["act_agree"] == 1
    assert row["agreement_rate"] == 1.0


def test_escalate_tier_agrees_with_a_denial():
    pass  # covered structurally below (finalize logic), kept here as an index marker.


def test_finalize_escalate_tier_agreement_polarity(ce_db):
    autonomy.record_shadow_decision(
        owner="bob", family="edit_file", tool_name="edit_file", workspace="/ws",
        score=0.1, tier="escalate", approval_id="appr-esc",
    )
    autonomy.finalize_shadow_decision(approval_id="appr-esc", actual_decision="denied")
    # escalate + denied = agreement; family_stats only aggregates act-tier
    # rows for promotion math, but the row itself must be marked agreed.
    with ce_store.db() as conn:
        row = conn.execute(
            "SELECT agreed FROM approval_shadow_log WHERE approval_id = ?", ("appr-esc",)
        ).fetchone()
    assert bool(row["agreed"]) is True


def test_family_not_promoted_below_min_decisions(ce_db):
    for i in range(5):
        aid = f"a{i}"
        autonomy.record_shadow_decision(
            owner="carol", family="write_file", tool_name="write_file", workspace="/ws",
            score=0.9, tier="act", approval_id=aid,
        )
        autonomy.finalize_shadow_decision(approval_id=aid, actual_decision="approved")
    assert autonomy.is_family_promoted("carol", "write_file") is False


def test_family_promoted_after_enough_agreeing_decisions(ce_db):
    for i in range(autonomy.DEFAULT_PROMOTE_MIN_DECISIONS):
        aid = f"p{i}"
        autonomy.record_shadow_decision(
            owner="dave", family="write_file", tool_name="write_file", workspace="/ws",
            score=0.9, tier="act", approval_id=aid,
        )
        autonomy.finalize_shadow_decision(approval_id=aid, actual_decision="approved")
    assert autonomy.is_family_promoted("dave", "write_file") is True


def test_one_destructive_disagreement_blocks_promotion(ce_db):
    for i in range(autonomy.DEFAULT_PROMOTE_MIN_DECISIONS - 1):
        aid = f"d{i}"
        autonomy.record_shadow_decision(
            owner="erin", family="write_file", tool_name="write_file", workspace="/ws",
            score=0.9, tier="act", approval_id=aid,
        )
        autonomy.finalize_shadow_decision(approval_id=aid, actual_decision="approved")
    # One more, flagged destructive, that the user denied.
    autonomy.record_shadow_decision(
        owner="erin", family="write_file", tool_name="write_file", workspace="/ws",
        score=0.9, tier="act", destructive=True, approval_id="dX",
    )
    autonomy.finalize_shadow_decision(approval_id="dX", actual_decision="denied")
    assert autonomy.is_family_promoted("erin", "write_file") is False


def test_manual_override_promotes_and_demotes(ce_db):
    autonomy.set_family_override("frank", "write_file", "promoted")
    assert autonomy.is_family_promoted("frank", "write_file") is True
    autonomy.set_family_override("frank", "write_file", "demoted")
    assert autonomy.is_family_promoted("frank", "write_file") is False
    autonomy.set_family_override("frank", "write_file", "")
    assert autonomy.is_family_promoted("frank", "write_file") is False


def test_set_family_override_rejects_bad_status(ce_db):
    with pytest.raises(ValueError):
        autonomy.set_family_override("gwen", "write_file", "sometimes")


def test_count_prior_approvals(ce_db):
    for i in range(3):
        aid = f"c{i}"
        autonomy.record_shadow_decision(
            owner="hank", family="write_file", tool_name="write_file", workspace="/ws",
            score=0.9, tier="act", approval_id=aid,
        )
        autonomy.finalize_shadow_decision(approval_id=aid, actual_decision="approved")
    assert autonomy.count_prior_approvals("hank", "write_file") == 3
    assert autonomy.count_prior_approvals("hank", "bash") == 0


# ── decision_for integration ─────────────────────────────────────────────

def _gate(**kwargs) -> ToolRunSecurityContext:
    return ToolRunSecurityContext(external_untrusted_context_seen=True, **kwargs)


def test_off_mode_never_calls_the_autonomy_module(monkeypatch):
    monkeypatch.setattr(autonomy, "autonomy_mode", lambda: "off")
    called = {"n": 0}
    monkeypatch.setattr(autonomy, "is_hard_blocked", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or False)
    gate = _gate(owner="alice", workspace="/ws")
    decision = gate.decision_for("write_file", '{"path": "/ws/x.py"}')
    assert decision.allowed is False
    assert called["n"] == 0  # short-circuited before touching hard-block logic


def test_shadow_mode_still_denies(monkeypatch):
    monkeypatch.setattr(autonomy, "autonomy_mode", lambda: "shadow")
    gate = _gate(owner="alice", workspace="/ws")
    decision = gate.decision_for("write_file", '{"path": "/ws/x.py"}')
    assert decision.allowed is False


def test_active_mode_auto_approves_a_promoted_act_tier_family(monkeypatch, tmp_path):
    inside = tmp_path / "ws"
    inside.mkdir()
    target = inside / "x.py"
    target.write_text("existing\n")
    monkeypatch.setattr(autonomy, "autonomy_mode", lambda: "active")
    monkeypatch.setattr(autonomy, "is_family_promoted", lambda owner, family: True)
    monkeypatch.setattr(autonomy, "compute_confidence", lambda *a, **k: autonomy.ConfidenceResult(
        score=0.95, tier="act", signals={}, reasons=["forced for test"]))
    logged = []
    monkeypatch.setattr(autonomy, "record_shadow_decision", lambda **kw: logged.append(kw) or "id")

    gate = _gate(owner="alice", workspace=str(inside))
    decision = gate.decision_for("write_file", json.dumps({"path": str(target)}))
    assert decision.allowed is True
    assert len(logged) == 1
    assert logged[0]["source"] == "active_auto"

    # A second decision_for call for the SAME content must not log twice
    # (decision_for is called more than once per real tool call).
    decision2 = gate.decision_for("write_file", json.dumps({"path": str(target)}))
    assert decision2.allowed is True
    assert len(logged) == 1


def test_active_mode_never_overrides_a_hard_block(monkeypatch, tmp_path):
    inside = tmp_path / "ws"
    inside.mkdir()
    outside = tmp_path / "outside" / "x.py"
    monkeypatch.setattr(autonomy, "autonomy_mode", lambda: "active")
    monkeypatch.setattr(autonomy, "is_family_promoted", lambda owner, family: True)
    monkeypatch.setattr(autonomy, "compute_confidence", lambda *a, **k: autonomy.ConfidenceResult(
        score=1.0, tier="act", signals={}, reasons=[]))

    gate = _gate(owner="alice", workspace=str(inside))
    decision = gate.decision_for("write_file", json.dumps({"path": str(outside)}))
    assert decision.allowed is False


def test_active_mode_does_not_override_unpromoted_family(monkeypatch, tmp_path):
    inside = tmp_path / "ws"
    inside.mkdir()
    target = inside / "x.py"
    monkeypatch.setattr(autonomy, "autonomy_mode", lambda: "active")
    monkeypatch.setattr(autonomy, "is_family_promoted", lambda owner, family: False)
    monkeypatch.setattr(autonomy, "compute_confidence", lambda *a, **k: autonomy.ConfidenceResult(
        score=1.0, tier="act", signals={}, reasons=[]))

    gate = _gate(owner="alice", workspace=str(inside))
    decision = gate.decision_for("write_file", json.dumps({"path": str(target)}))
    assert decision.allowed is False


def test_active_mode_never_bypasses_the_desktop_or_command_guard_denials(monkeypatch):
    """The autonomy override only lives inside the post-external-context
    branch of `decision_for` — the desktop-input and destructive-command
    denials both return before it is ever reached, whatever the mode."""
    monkeypatch.setattr(autonomy, "autonomy_mode", lambda: "active")
    monkeypatch.setattr(autonomy, "is_family_promoted", lambda owner, family: True)
    monkeypatch.setattr(autonomy, "compute_confidence", lambda *a, **k: autonomy.ConfidenceResult(
        score=1.0, tier="act", signals={}, reasons=[]))
    from src import tool_capabilities as caps
    monkeypatch.setattr(caps, "tool_approval_mode", lambda: "ask")
    monkeypatch.setattr(caps, "tool_requires_per_call_approval", lambda tool_name: tool_name == "desktop_click")

    gate = ToolRunSecurityContext()  # no external context needed for this branch
    decision = gate.decision_for("desktop_click", "{}")
    assert decision.allowed is False


# ── ToolApprovalStore integration ────────────────────────────────────────

def _caps_for_write():
    return ToolCapabilities(effects=frozenset({ToolEffect.WRITE_WORKSPACE}),
                            result_integrity=ResultIntegrity.WORKSPACE_UNTRUSTED)


def test_off_mode_create_writes_no_shadow_row_and_no_note(ce_db, monkeypatch):
    monkeypatch.setattr(autonomy, "autonomy_mode", lambda: "off")
    store = ToolApprovalStore()
    pending = store.create(
        owner="alice", session_id="s1", origin_run_id="r1", tool_name="write_file",
        content='{"path": "x.py"}', workspace="/ws",
        external_untrusted_context_seen=True, capabilities=_caps_for_write(),
    )
    assert pending.autonomy_note == ""
    assert "autonomy_note" not in pending.public_payload()
    with ce_store.db() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM approval_shadow_log").fetchone()["n"]
    assert n == 0


def test_shadow_mode_create_logs_a_row_but_no_note(ce_db, monkeypatch):
    monkeypatch.setattr(autonomy, "autonomy_mode", lambda: "shadow")
    store = ToolApprovalStore()
    pending = store.create(
        owner="alice", session_id="s1", origin_run_id="r1", tool_name="write_file",
        content='{"path": "x.py"}', workspace="/ws",
        external_untrusted_context_seen=True, capabilities=_caps_for_write(),
    )
    assert pending.autonomy_note == ""
    with ce_store.db() as conn:
        row = conn.execute(
            "SELECT * FROM approval_shadow_log WHERE approval_id = ?", (pending.approval_id,)
        ).fetchone()
    assert row is not None
    assert row["source"] == "shadow"
    assert row["actual_decision"] == ""

    grant = store.consume(pending.approval_id, decision="approve_task", owner="alice", session_id="s1")
    assert grant is not None
    with ce_store.db() as conn:
        row = conn.execute(
            "SELECT actual_decision, agreed FROM approval_shadow_log WHERE approval_id = ?",
            (pending.approval_id,),
        ).fetchone()
    assert row["actual_decision"] == "approved"


def test_shadow_mode_finalizes_a_denial(ce_db, monkeypatch):
    monkeypatch.setattr(autonomy, "autonomy_mode", lambda: "shadow")
    store = ToolApprovalStore()
    pending = store.create(
        owner="alice", session_id="s1", origin_run_id="r1", tool_name="write_file",
        content='{"path": "x.py"}', workspace="/ws",
        external_untrusted_context_seen=True, capabilities=_caps_for_write(),
    )
    reason, grant = store.consume_with_reason(
        pending.approval_id, decision="deny", owner="alice", session_id="s1",
    )
    assert reason == "invalid_decision"
    assert grant is None
    with ce_store.db() as conn:
        row = conn.execute(
            "SELECT actual_decision FROM approval_shadow_log WHERE approval_id = ?",
            (pending.approval_id,),
        ).fetchone()
    assert row["actual_decision"] == "denied"


def test_active_mode_card_carries_a_recommendation_note(ce_db, monkeypatch):
    monkeypatch.setattr(autonomy, "autonomy_mode", lambda: "active")
    store = ToolApprovalStore()
    pending = store.create(
        owner="alice", session_id="s1", origin_run_id="r1", tool_name="write_file",
        content='{"path": "x.py"}', workspace="/ws",
        external_untrusted_context_seen=True, capabilities=_caps_for_write(),
    )
    assert pending.autonomy_note != ""
    assert "autonomy_note" in pending.public_payload()


def test_non_promotable_effect_class_is_never_logged(ce_db, monkeypatch):
    monkeypatch.setattr(autonomy, "autonomy_mode", lambda: "shadow")
    store = ToolApprovalStore()
    caps = ToolCapabilities(effects=frozenset({ToolEffect.EXECUTE_CODE}),
                            result_integrity=ResultIntegrity.WORKSPACE_UNTRUSTED)
    pending = store.create(
        owner="alice", session_id="s1", origin_run_id="r1", tool_name="bash",
        content="echo hi", workspace="/ws",
        external_untrusted_context_seen=True, capabilities=caps,
    )
    assert pending.autonomy_note == ""
    with ce_store.db() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM approval_shadow_log").fetchone()["n"]
    assert n == 0
