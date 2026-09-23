"""The engine's own read-only context calls versus the external-context gate.

A delivered context packet is untrusted context, so it arms the gate; without
an exemption the packet's own follow-up (`context_recall` of an id its footer
listed) would ask for approval in "ask" mode. `src/context_tool_gate.py`
exempts exactly the ids offered by a packet delivered in THIS turn; unknown
ids, other tools and every non-read effect keep today's behaviour.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from src import context_tool_gate as gate
from src.context_tool_gate import ContextAwareSecurityContext, GateAllowRule
from src.tool_capabilities import ToolCapabilities, ToolEffect, ToolRunSecurityContext

OFFERED = "abcdef0123"
OTHER = "0123456789"


def _armed(**kw) -> ContextAwareSecurityContext:
    context = ContextAwareSecurityContext(**kw)
    context.external_untrusted_context_seen = True
    return context


@pytest.fixture(autouse=True)
def ask_mode(monkeypatch):
    import src.tool_capabilities as caps

    monkeypatch.setattr(caps, "tool_approval_mode", lambda: "ask")


# ── the table ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("content", [
    {"ids": [f"ctx:{OFFERED}"]},
    json.dumps({"ids": [OFFERED]}),
    f"[ctx:{OFFERED}] Bluehaven handbook (doc:handbook#chunk1)",
])
def test_offered_ids_pass(content):
    assert gate.allows("context_recall", content, offered_ids={OFFERED})


@pytest.mark.parametrize("content", [
    {"ids": [f"ctx:{OTHER}"]},
    {"ids": [OFFERED, OTHER]},          # one unknown id spoils the call
    {"ids": ["not-an-id"]},
    {"ids": []},
    "",
])
def test_unknown_or_malformed_ids_do_not_pass(content):
    assert not gate.allows("context_recall", content, offered_ids={OFFERED})


def test_nothing_offered_nothing_passes():
    assert not gate.allows("context_recall", {"ids": [OFFERED]}, offered_ids=())


def test_rules_never_lift_more_than_a_private_read():
    rules = (GateAllowRule("bash"), GateAllowRule("write_file"),
             GateAllowRule("manage_notes"), GateAllowRule("no_such_tool"))
    for name in ("bash", "write_file", "manage_notes", "no_such_tool"):
        assert not gate.allows(name, "{}", rules=rules), name


def test_an_action_rule_admits_only_its_actions(monkeypatch):
    """The shape a read-only multi-action tool gets: one line in the table."""
    monkeypatch.setattr(gate, "capabilities_for_action", lambda name, content: ToolCapabilities(
        frozenset({ToolEffect.READ_PRIVATE}), known=True))
    rules = (GateAllowRule("brain", actions=("search", "read", "entity", "timeline",
                                              "neighbors")),)
    assert gate.allows("brain", {"action": "search", "q": "Ada"}, rules=rules)
    assert gate.allows("brain", json.dumps({"action": "timeline"}), rules=rules)
    assert not gate.allows("brain", {"action": "write"}, rules=rules)
    assert not gate.allows("brain", {"q": "no action"}, rules=rules)


# ── the security context ───────────────────────────────────────────────────

def test_without_an_offer_it_decides_like_its_parent():
    ours, parent = _armed(), ToolRunSecurityContext(external_untrusted_context_seen=True)
    for name, content in (("context_recall", {"ids": [OFFERED]}), ("search_chats", "q"),
                          ("bash", "ls"), ("read_file", "a.txt")):
        assert ours.decision_for(name, content) == parent.decision_for(name, content), name


def test_recall_of_an_offered_id_passes_without_a_card():
    context = _armed()
    assert not context.decision_for("context_recall", {"ids": [OFFERED]}).allowed
    context.offer_context_ids([OFFERED, "garbage"])
    assert context.offered_context_ids == {OFFERED}
    assert context.decision_for("context_recall", {"ids": [f"ctx:{OFFERED}"]}).allowed
    denied = context.decision_for("context_recall", {"ids": [OTHER]})
    assert not denied.allowed and "context_recall" in denied.reason
    # the offer covers context_recall only, not other private reads
    assert not context.decision_for("search_chats", "Ada").allowed


def test_full_mode_and_bypass_are_untouched(monkeypatch):
    import src.tool_capabilities as caps

    monkeypatch.setattr(caps, "tool_approval_mode", lambda: "full")
    assert _armed().decision_for("context_recall", {"ids": [OTHER]}).allowed
    monkeypatch.setattr(caps, "tool_approval_mode", lambda: "ask")
    assert _armed(approval_gate_bypassed=True).decision_for(
        "context_recall", {"ids": [OTHER]}).allowed


# ── the agent loop: the packet arms the gate, its own ids do not ask ───────

def _turn(monkeypatch, recall_id):
    import src.agent_loop as al
    from src.context_engine import wiring
    from src import memory_engine

    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)
    monkeypatch.setattr(memory_engine, "injection_enabled", lambda: False)
    monkeypatch.setattr(wiring, "enabled", lambda: True)
    monkeypatch.setattr(wiring, "shadow_enabled", lambda: False)
    executed = []
    replies = iter([
        "```context_recall\n" + json.dumps({"ids": [f"ctx:{recall_id}"]}) + "\n```",
        "Hecho.",
    ])

    async def fake_stream(candidates, messages, **kwargs):
        # What llm_core does per candidate: ask the loop's request factory for
        # the exact prompt — that is where the run's gate observes the packet.
        factory = kwargs.get("candidate_request_factory")
        if factory is not None:
            url, model, headers = candidates[0]
            request = factory(0, url, model, headers)
            if hasattr(request, "__await__"):
                await request
        yield f"data: {json.dumps({'delta': next(replies, 'Hecho.')})}\n\n"
        yield "data: [DONE]\n\n"

    async def fake_execute(block, *args, **kwargs):
        executed.append(block.tool_type)
        return (block.tool_type, {"output": "recalled body", "exit_code": 0})

    async def fake_deliver(**kwargs):
        return {
            "message": {"role": "user", "content": "packet [ctx:%s] omitted" % OFFERED,
                        "_agent_injected": "context_engine",
                        "metadata": {"trusted": False, "tool_gate_untrusted": True,
                                     "provenance_origin": "context-packet:p1",
                                     "context_packet_id": "p1"}},
            "report": {"packet_id": "p1", "request_id": "r1", "delivered": True,
                       "sources": [], "recallable": [OFFERED]},
        }

    monkeypatch.setattr(al, "stream_llm_with_fallback", fake_stream)
    monkeypatch.setattr(al, "execute_tool_block", fake_execute)
    monkeypatch.setattr(wiring, "deliver_round", fake_deliver)

    async def collect():
        out = []
        async for chunk in al.stream_agent_loop(
                "http://local.test/v1", "small-local-model",
                [{"role": "user", "content": "¿qué dice el manual de Bluehaven?"}],
                max_rounds=2, relevant_tools={"context_recall"},
                session_id="s-ada", owner="ada"):
            if chunk.startswith("data: {"):
                out.append(json.loads(chunk[6:]))
        return out

    return asyncio.run(collect()), executed


def _asked(events):
    return [e for e in events if e.get("type") == "tool_output"
            and e.get("tool") == "context_recall"
            and (e.get("ask_user") or {}).get("kind") == "tool_approval"]


def test_agent_recall_of_an_offered_id_runs_without_a_card(monkeypatch):
    events, executed = _turn(monkeypatch, OFFERED)
    assert executed == ["context_recall"]
    assert _asked(events) == []


def test_agent_recall_of_an_unknown_id_still_asks(monkeypatch):
    events, executed = _turn(monkeypatch, OTHER)
    assert executed == []
    assert _asked(events), "today's behaviour: an approval card"


def test_brain_read_actions_are_exempt_but_writes_are_not():
    from src import context_tool_gate as gate
    rule = gate.rule_for("brain")
    assert rule is not None
    assert set(rule.actions) == {"search", "read", "entity", "timeline", "neighbors"}
    assert "write" not in rule.actions and "append" not in rule.actions
