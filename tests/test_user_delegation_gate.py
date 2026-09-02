"""/agents typed by the user IS the user's authorization for that delegation.

Seen live (ronda 6): every `/agents a | b` in an untrusted workspace stopped
at "Allow this task to continue?" for the delegate_agents call the user had
just dictated. The gate exists for actions the MODEL decides after untrusted
context; a delegation whose tasks are exactly the user's own words is not
that. Same instructions pass; anything else (extra or rewritten tasks)
keeps the gate."""
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


def test_the_users_own_delegation_passes_the_gate_every_time_it_is_checked():
    """The loop checks before the approval card and tool_execution checks
    again right before running: the same call must pass BOTH (a one-shot
    flag let the first through and blocked the second — seen live)."""
    ctx = _ctx(PAYLOAD)
    content = json.dumps({"tasks": [
        {"name": "worker one", "instruction": "[cart.py] add f()"},          # model renamed it: fine
        {"name": "b", "instruction": "[tests/test_cart.py] add the test"},
    ], "parallel": True})
    assert ctx.decision_for("delegate_agents", content).allowed
    assert ctx.decision_for("delegate_agents", content).allowed


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


def test_the_gate_reads_the_call_exactly_like_the_tool_does():
    """Seen live: qwen3.5 sends `tasks` as a JSON string with the rest of the
    object stuffed inside it — `{"tasks": "[{...}], \\"parallel\\": true}"}` —
    and the tool strips the `[files]` prefix from instructions. json.loads on
    that string fails, so the gate said 'not JSON' and asked for approval.
    Both sides now go through subagent_tools.parse_delegation_args."""
    ctx = _ctx(PAYLOAD)
    stuffed = '{"tasks": "[{\\"name\\": \\"a\\", \\"instruction\\": \\"[cart.py] add f()\\"}, ' \
              '{\\"name\\": \\"b\\", \\"instruction\\": \\"[tests/test_cart.py] add the test\\"}], \\"parallel\\": true}"}'
    assert ctx.decision_for("delegate_agents", stuffed).allowed
    # the model dropped the [file] prefix but kept the words: still the user's task
    no_prefix = json.dumps({"tasks": [{"name": "a", "instruction": "add f()", "files": ["cart.py"]}]})
    assert ctx.decision_for("delegate_agents", no_prefix).allowed
    # trailing period / whitespace / case differences are not rewrites
    loose = json.dumps({"tasks": [{"name": "a", "instruction": "  [cart.py]  Add F(). "}]})
    assert ctx.decision_for("delegate_agents", loose).allowed
    # a leading tool-name line around the JSON (fenced form)
    fenced = "delegate_agents\n" + json.dumps({"tasks": PAYLOAD["tasks"]})
    assert ctx.decision_for("delegate_agents", fenced).allowed
    # garbage never passes
    assert not ctx.decision_for("delegate_agents", "{not json").allowed
