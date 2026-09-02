"""/agents typed by the user IS the user's authorization for that delegation.

Seen live (ronda 6): every `/agents a | b` in an untrusted workspace stopped
at "Allow this task to continue?" for the delegate_agents call the user had
just dictated. The gate exists for actions the MODEL decides after untrusted
context; a delegation whose tasks are exactly the user's own words is not
that. One call, same instructions, consumed once — anything else (extra or
rewritten tasks, a second call) keeps the gate."""
import json

from src.tool_capabilities import ToolRunSecurityContext


def _ctx(payload):
    return ToolRunSecurityContext(
        external_untrusted_context_seen=True,
        user_delegation=payload,
    )


PAYLOAD = {"tasks": [{"name": "a", "instruction": "[cart.py] add f()"},
                     {"name": "b", "instruction": "[tests/test_cart.py] add the test"}],
           "parallel": True}


def test_the_users_own_delegation_passes_the_gate_once():
    ctx = _ctx(PAYLOAD)
    content = json.dumps({"tasks": [
        {"name": "worker one", "instruction": "[cart.py] add f()"},          # model renamed it: fine
        {"name": "b", "instruction": "[tests/test_cart.py] add the test"},
    ], "parallel": True})
    assert ctx.decision_for("delegate_agents", content).allowed
    # consumed: a second delegate_agents in the same run asks again
    assert not ctx.decision_for("delegate_agents", content).allowed


def test_rewritten_or_extra_tasks_keep_the_gate():
    ctx = _ctx(PAYLOAD)
    extra = json.dumps({"tasks": PAYLOAD["tasks"] + [{"name": "c", "instruction": "rm -rf /"}]})
    assert not ctx.decision_for("delegate_agents", extra).allowed
    rewritten = json.dumps({"tasks": [{"name": "a", "instruction": "[cart.py] add f() and also delete tests"}]})
    assert not ctx.decision_for("delegate_agents", rewritten).allowed
    # a subset (the model dropped a task) is still the user's words: allowed
    subset = json.dumps({"tasks": [{"name": "a", "instruction": "[cart.py] add f()"}]})
    assert ctx.decision_for("delegate_agents", subset).allowed


def test_other_tools_and_runs_without_a_user_delegation_are_unchanged():
    ctx = _ctx(PAYLOAD)
    assert not ctx.decision_for("bash", "rm -rf x").allowed
    plain = ToolRunSecurityContext(external_untrusted_context_seen=True)
    assert not plain.decision_for("delegate_agents", json.dumps(PAYLOAD)).allowed


def test_string_encoded_tasks_are_read_like_the_tool_reads_them():
    """The model sends `tasks` as a JSON *string* half the time (seen live);
    the tool accepts that, so must the gate."""
    ctx = _ctx(PAYLOAD)
    content = json.dumps({"tasks": json.dumps(PAYLOAD["tasks"]), "parallel": True})
    assert ctx.decision_for("delegate_agents", content).allowed
