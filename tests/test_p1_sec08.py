"""SEC-08 — proportional, auditable policy profiles.

Acceptance: a plugin update with more permissions invalidates prior consent
without blocking tools that were not affected.
"""
from __future__ import annotations

from src import security_policy as sp
from src.tool_capabilities import ToolEffect


# ── profiles are proportional: reach grows, admin/destructive always asks ──

def test_read_profile_refuses_a_write_effect():
    decision = sp.evaluate([ToolEffect.WRITE_WORKSPACE], profile="read")
    assert decision.allowed is False
    assert "WRITE_WORKSPACE".lower() in " ".join(decision.effects) or "write_workspace" in decision.reason


def test_local_edit_profile_permits_a_workspace_write_without_approval():
    decision = sp.evaluate([ToolEffect.WRITE_WORKSPACE], profile="local_edit")
    assert decision.allowed is True
    assert decision.requires_approval is False


def test_local_edit_profile_refuses_network_egress():
    decision = sp.evaluate([ToolEffect.NETWORK_EGRESS], profile="local_edit")
    assert decision.allowed is False


def test_supervised_agent_profile_permits_network_egress():
    decision = sp.evaluate([ToolEffect.NETWORK_EGRESS], profile="supervised_agent")
    assert decision.allowed is True


def test_destructive_effect_always_needs_approval_even_when_permitted():
    decision = sp.evaluate([ToolEffect.DESTRUCTIVE], profile="bounded_automation")
    assert decision.allowed is True
    assert decision.requires_approval is True


def test_already_authorised_reads_never_require_approval_read_only():
    """Confirmation fatigue: an effect the profile already grants outright
    never needs a per-call approval."""
    decision = sp.evaluate([ToolEffect.READ_WORKSPACE], profile="bounded_automation")
    assert decision.allowed is True
    assert decision.requires_approval is False


def test_unknown_profile_falls_back_to_the_default_rather_than_crashing():
    decision = sp.evaluate([ToolEffect.READ_WORKSPACE], profile="not-a-real-profile")
    assert decision.profile == sp.DEFAULT_PROFILE


# ── every decision is logged with a reason, and exportable ─────────────────

def test_evaluate_appends_a_reasoned_record_to_the_audit_log():
    sp.clear_audit_log()
    sp.evaluate([ToolEffect.ADMIN_CHANGE], profile="supervised_agent",
               plugin_id="p1", tool_name="danger_tool")
    rows = sp.audit_log(limit=10)
    assert rows
    assert rows[0]["plugin_id"] == "p1"
    assert rows[0]["reason"]


def test_export_audit_is_unsigned_without_a_key():
    sp.clear_audit_log()
    sp.evaluate([ToolEffect.READ_WORKSPACE], profile="read")
    export = sp.export_audit(signing_key=None)
    assert export["signed"] is False
    assert export["records"][0].get("signature") is None or "signature" not in export["records"][0]


def test_export_audit_is_signed_and_verifiable_with_a_key():
    sp.clear_audit_log()
    sp.evaluate([ToolEffect.READ_WORKSPACE], profile="read")
    key = b"a-test-signing-key"
    export = sp.export_audit(signing_key=key)
    assert export["signed"] is True
    record = export["records"][0]
    assert record["signature"]
    # Tampering with any field changes the digest.
    tampered = dict(record)
    tampered["allowed"] = not tampered["allowed"]
    tampered.pop("signature")
    assert sp._sign(tampered, key) != record["signature"]


# ── consent is per (plugin, tool): an upgrade never blocks a sibling ───────

def test_a_tool_with_no_prior_consent_is_not_consented():
    store = sp.ConsentStore()
    check = store.check("acme-plugin", "read_docs", [ToolEffect.READ_WORKSPACE])
    assert check["consented"] is False


def test_added_permission_invalidates_consent_for_that_tool():
    store = sp.ConsentStore()
    store.grant("acme-plugin", "sync_tool", [ToolEffect.READ_WORKSPACE])
    check = store.check("acme-plugin", "sync_tool",
                        [ToolEffect.READ_WORKSPACE, ToolEffect.NETWORK_EGRESS])
    assert check["consented"] is False
    assert "network_egress" in check["new_effects"]


def test_removed_permission_keeps_consent_valid():
    store = sp.ConsentStore()
    store.grant("acme-plugin", "sync_tool", [ToolEffect.READ_WORKSPACE, ToolEffect.NETWORK_EGRESS])
    check = store.check("acme-plugin", "sync_tool", [ToolEffect.READ_WORKSPACE])
    assert check["consented"] is True


def test_upgrading_one_tool_does_not_touch_a_sibling_tools_consent():
    """The exact SEC-08 acceptance case: a plugin update that adds a
    permission to ONE tool must not invalidate — or even look at — another
    tool's own consent record under the same plugin."""
    store = sp.ConsentStore()
    store.grant("acme-plugin", "read_docs", [ToolEffect.READ_WORKSPACE])
    store.grant("acme-plugin", "sync_tool", [ToolEffect.READ_WORKSPACE])

    # sync_tool's update adds a permission — only sync_tool is affected.
    sync_check = store.check("acme-plugin", "sync_tool",
                             [ToolEffect.READ_WORKSPACE, ToolEffect.EXTERNAL_SIDE_EFFECT])
    assert sync_check["consented"] is False

    # read_docs was never touched and is still fully consented.
    docs_check = store.check("acme-plugin", "read_docs", [ToolEffect.READ_WORKSPACE])
    assert docs_check["consented"] is True


def test_evaluate_refuses_when_consent_is_missing_and_records_why():
    store = sp.ConsentStore()
    decision = sp.evaluate([ToolEffect.WRITE_WORKSPACE], profile="local_edit",
                           plugin_id="acme-plugin", tool_name="new_tool", consents=store)
    assert decision.allowed is False
    assert decision.requires_approval is True
    assert "consent" in decision.reason.lower() or "no consent" in decision.reason.lower()


def test_evaluate_allows_once_consent_is_granted():
    store = sp.ConsentStore()
    store.grant("acme-plugin", "new_tool", [ToolEffect.WRITE_WORKSPACE])
    decision = sp.evaluate([ToolEffect.WRITE_WORKSPACE], profile="local_edit",
                           plugin_id="acme-plugin", tool_name="new_tool", consents=store)
    assert decision.allowed is True


def test_revoke_one_tool_does_not_affect_siblings():
    store = sp.ConsentStore()
    store.grant("acme-plugin", "a", [ToolEffect.READ_WORKSPACE])
    store.grant("acme-plugin", "b", [ToolEffect.READ_WORKSPACE])
    removed = store.revoke("acme-plugin", "a")
    assert removed == 1
    assert store.get("acme-plugin", "a") is None
    assert store.get("acme-plugin", "b") is not None
