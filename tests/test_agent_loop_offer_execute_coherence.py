"""What is offered can be executed: one denylist, two surfaces.

Measured live (Windows, Ollama `/v1`, `qwen3.5:9b` at 31 tok/s, Agent mode, a
workspace bound to `D:\\LocalAI\\_claude_tmp\\demo_app`, chat NOT inside a
project, nothing else configured):

    user> "Anade a cart.py una funcion apply_tax(total, rate) ... y su test en
           tests/test_cart.py"

    [agent-debug] tools_sent=15 tool_names=['apply_patch', 'ask_teacher',
     'ask_user', 'delegate_agents', 'edit_file', 'get_workspace', 'glob',
     'grep', 'ls', 'manage_bg_jobs', 'read_file', 'todowrite', 'update_plan',
     'web_fetch', 'web_search']  pruned={project_context: ...}

    06:20:14 Tool blocked before approval by current_tool_policy: read_file
    06:20:14 Tool blocked before approval by current_tool_policy: read_file
    ... 8 blocks, 13 calls, 0 files changed.

`read_file` was on the list the model was shown AND on the list the runtime
refused. The model said "Veo que las herramientas están bloqueadas para leer
archivos", cast about with `ls`/`glob`, ran `web_search` for "demo_app
workspace status" — looking on the internet for the folder it was bound to —
and finished by dictating the patch as chat text.

WHY BOTH LISTS CAME FROM ONE DENYLIST AND STILL DISAGREED
---------------------------------------------------------
`routes/chat_routes.py` classifies the turn with a word-boundary regex that
includes `rate`; `apply_tax(total, rate)` matches, so the "direct web lookup"
clamp put `{bash, python, read_file, write_file, edit_file, ...}` into
`disabled_tools`, and `build_effective_tool_policy` then wrapped that same set
into the turn's `ToolPolicy`. Both gates therefore held `read_file`.

`WORKSPACE_TOOL_FLOOR` (the previous fix) restored `read_file` to
`_relevant_tools` and subtracted the floor from the denylist — but inside
`_tool_schemas_for_route`, into a local named `_effective_disabled` that only
the schema list could see. The prompt sections, the loop's execution gate and
`src/tool_execution.py`'s own gate all still read the unreconciled
`disabled_tools` and the unreconciled `tool_policy`. So the fix repaired the
list the model was SHOWN and left the list it could RUN exactly as broken,
which is strictly worse than the bug it replaced: an absent tool is a
limitation, an offered-then-refused tool is a trap.

Both predicates fired at once, and `src/agent_loop.py` labelled them with the
same word, so the log could not even say which. That is the second bug here
and it gets its own tests below.

THE INVARIANT
-------------
The floor is reconciled into `disabled_tools` AND `tool_policy` once, where the
floor is resolved, before either surface is built. Offered set == executable
set, by construction. `test_offered_set_equals_executable_set` asserts it for a
matrix of turns and names the trap tools when it fails.

Nothing is relaxed: `_resolve_workspace_floor` subtracts every authorization
denial before the floor exists, and section 4 re-asserts each one end to end.
"""

import asyncio
import json
import re
import unittest.mock as mock

import pytest

import src.agent_tools  # noqa: F401  - resolves the circular schema imports first
import src.agent_loop as agent_loop
from src.tool_policy import ToolPolicy, build_effective_tool_policy


OLLAMA_V1 = "http://127.0.0.1:11434/v1/chat/completions"
MODEL = "qwen3.5:9b"

SPANISH_REQUEST = (
    "Anade a cart.py una funcion apply_tax(total, rate) que devuelva el total "
    "con el impuesto aplicado, y su test en tests/test_cart.py."
)
ENGLISH_REQUEST = (
    "Add to cart.py a function apply_tax(total, rate) that returns the total "
    "with the tax applied, and its test in tests/test_cart.py."
)

# `routes/chat_routes.py`, verbatim: the regex that sets `_explicit_web_intent`
# and the set that clamp folds into `disabled_tools`. Copied so the
# reproduction runs on the real production input, not an invented one.
WEB_INTENT_RE = re.compile(
    r"\b(search|look\s*up|lookup|google|browse|web|online|latest|current|"
    r"today|news|weather|forecast|rate|exchange\s+rate)\b"
)
WEB_INTENT_CLAMP = {
    "bash", "python",
    "search_chats", "manage_skills", "manage_memory",
    "read_file", "write_file", "edit_file",
    "create_document", "edit_document", "update_document",
    "send_email", "reply_to_email",
    "manage_notes", "manage_calendar", "manage_tasks",
    "api_call",
}


def route_turn_policy(message, extra_disabled=()):
    """Exactly what `routes/chat_routes.py` hands `stream_agent_loop`.

    The route composes a denylist, wraps it in a `ToolPolicy`, and then
    replaces the denylist with `policy.all_disabled_names()` — so the loop
    receives the same names through two independent channels
    (`routes/chat_routes.py`, `disabled_tools = tool_policy.all_disabled_names()`).
    Reproducing that pairing is the whole point: a test that passes only
    `disabled_tools` misses the `tool_policy` gate entirely, which is how this
    bug survived the workspace-floor fix.
    """
    disabled = set(extra_disabled)
    if WEB_INTENT_RE.search(str(message).lower()):
        disabled |= WEB_INTENT_CLAMP
        disabled -= {"web_search", "web_fetch"}  # search enabled for the turn
    policy = build_effective_tool_policy(
        disabled_tools=disabled, last_user_message=message
    )
    return policy.all_disabled_names(), policy


@pytest.fixture()
def workspace(tmp_path):
    """The user's linked project: cart.py plus its test."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "cart.py").write_text(
        "def total(items):\n    return sum(i['price'] for i in items)\n"
    )
    (tmp_path / "tests" / "test_cart.py").write_text(
        "from cart import total\n\n\ndef test_total():\n    assert total([]) == 0\n"
    )
    return str(tmp_path)


class Turn:
    """What one real `stream_agent_loop` run offered, ran and refused."""

    def __init__(self, offered, executed, refusals, events, exec_kwargs, prompt):
        self.offered = offered            # tool names the model was shown
        self.executed = executed          # tool names that reached the executor
        self.refusals = refusals          # {tool: result dict} for blocked calls
        self.events = events              # decoded SSE payloads
        self.exec_kwargs = exec_kwargs    # gate inputs handed to the dispatcher
        self.prompt = prompt              # round 1's messages, joined

    @property
    def traps(self):
        """Tools offered this turn that the same turn refused to run."""
        return sorted(set(self.offered) & set(self.refusals))


def run_turn(
    message,
    workspace=None,
    *,
    disabled_tools=None,
    tool_policy=None,
    calls=(),
    owner="admin",
    plan_mode=False,
    settings=None,
    supports_tools=True,
    active_document=None,
    active_email=None,
    relevant_tools=None,
    max_rounds=None,
):
    """Drive the real loop, then make it call the tools it just offered.

    Round 1 goes out with whatever the production path selected; the fake
    provider answers with native `tool_calls` for `calls` (or, when `calls` is
    the string "all", for every tool round 1 actually offered — which is how
    the coherence test asks the turn about itself). Only the four true edges
    are stubbed: the provider stream, the tool executor, the MCP manager, and
    the endpoint's `supports_tools` row. Everything between the request and the
    gate is production code.
    """
    captured_tools = []
    captured_messages = []
    executed = []
    exec_kwargs = []
    turn_settings = dict(settings or {})
    state = {"pending": None}

    async def fake_stream(candidates, messages, **kwargs):
        schemas = kwargs.get("tools") or []
        captured_tools.append(schemas)
        captured_messages.append(messages)
        if state["pending"] is None:
            offered = [
                s.get("function", {}).get("name")
                for s in schemas if s.get("function")
            ]
            if calls == "all":
                state["pending"] = list(offered)
            else:
                state["pending"] = list(calls)
        batch, state["pending"] = state["pending"], []
        if not batch:
            yield "data: " + json.dumps({"delta": "Listo."}) + "\n\n"
        elif supports_tools:
            yield "data: " + json.dumps({"delta": "Voy."}) + "\n\n"
            yield "data: " + json.dumps({
                "type": "tool_calls",
                "calls": [
                    {"name": name, "arguments": json.dumps(_ARGS.get(name, {}))}
                    for name in batch
                ],
            }) + "\n\n"
        else:
            # A route with no function-calling channel has exactly one way to
            # call a tool: write the fence the prompt taught it.
            fenced = "Voy.\n\n" + "\n\n".join(
                "```%s\n%s\n```" % (name, json.dumps(_ARGS.get(name, {})))
                for name in batch
            )
            yield "data: " + json.dumps({"delta": fenced}) + "\n\n"
        yield "data: [DONE]\n\n"

    async def fake_execute(block, *args, **kwargs):
        executed.append(block.tool_type)
        exec_kwargs.append(kwargs)
        return (block.tool_type, {"output": "ok", "exit_code": 0})

    def fake_get_setting(key, default=None):
        return turn_settings.get(key, default)

    patches = [
        mock.patch.object(agent_loop, "stream_llm_with_fallback", fake_stream),
        mock.patch.object(agent_loop, "execute_tool_block", fake_execute),
        mock.patch.object(agent_loop, "get_mcp_manager", lambda: None),
        mock.patch.object(agent_loop, "estimate_tokens", lambda *a, **k: 10),
        mock.patch.object(agent_loop, "get_setting", fake_get_setting),
        mock.patch.object(
            agent_loop,
            "_agent_route_tool_mode",
            lambda *a, **k: (bool(supports_tools), False, True),
        ),
    ]
    if owner == "admin":
        patches.append(
            mock.patch.object(agent_loop, "blocked_tools_for_owner", lambda o: set())
        )

    for patch in patches:
        patch.start()
    try:
        async def drive():
            stream = agent_loop.stream_agent_loop(
                endpoint_url=OLLAMA_V1,
                model=MODEL,
                messages=[{"role": "user", "content": message}],
                headers={},
                workspace=workspace,
                owner=owner,
                session_id="s-cart",
                max_rounds=max_rounds if max_rounds is not None else 2,
                context_length=32768,
                disabled_tools=set(disabled_tools) if disabled_tools else None,
                tool_policy=tool_policy,
                plan_mode=plan_mode,
                active_document=active_document,
                active_email=active_email,
                relevant_tools=set(relevant_tools) if relevant_tools else None,
                harness_options={
                    "checkpoints": False,
                    "run_tests": False,
                    "repo_map": False,
                },
            )
            return [chunk async for chunk in stream]

        chunks = asyncio.run(drive())
    finally:
        for patch in patches:
            patch.stop()

    assert captured_tools, "the loop never reached the provider"
    offered = sorted(
        s.get("function", {}).get("name")
        for s in captured_tools[0] if s.get("function")
    )
    events = []
    for chunk in chunks:
        if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
            try:
                events.append(json.loads(chunk[6:]))
            except json.JSONDecodeError:
                pass
    refusals = {
        e["tool"]: e
        for e in events
        if e.get("type") == "tool_output" and e.get("blocked")
    }
    prompt = "\n".join(
        str(m.get("content") or "")
        for m in captured_messages[0] if isinstance(m, dict)
    )
    return Turn(offered, executed, refusals, events, exec_kwargs, prompt)


# Arguments that make each tool a well-formed call. Only the shape matters —
# the executor is stubbed; the gate under test runs before dispatch.
_ARGS = {
    "read_file": {"path": "cart.py"},
    "edit_file": {"path": "cart.py", "old_string": "a", "new_string": "b"},
    "apply_patch": {"patch": "*** Begin Patch\n*** End Patch\n"},
    "write_file": {"path": "cart.py", "content": "x"},
    "ls": {"path": "."},
    "glob": {"pattern": "*.py"},
    "grep": {"pattern": "def"},
    "bash": {"command": "ls"},
    "python": {"code": "print(1)"},
    "web_search": {"query": "x"},
    "web_fetch": {"url": "https://example.com"},
}


def blocked_log_lines(caplog):
    return [
        r.getMessage() for r in caplog.records
        if r.getMessage().startswith("Tool blocked before approval")
    ]


# --------------------------------------------------------------------------
# 1. The reproduction: offered and blocked in the same turn
# --------------------------------------------------------------------------

def test_the_route_hands_the_loop_the_same_names_through_both_channels():
    """The precondition the workspace-floor tests did not model.

    `disabled_tools` and `tool_policy` are not alternatives. The route builds
    one from the other, so `read_file` reaches the loop twice, and a fix that
    only reconciles one of them fixes nothing.
    """
    disabled, policy = route_turn_policy(SPANISH_REQUEST)
    assert "read_file" in disabled
    assert policy.blocks("read_file")
    assert policy.mode == "normal" and not policy.block_all_tool_calls


def test_the_live_failure_read_file_is_offered_and_runs(workspace):
    """The measured turn, end to end: 15 tools sent, `read_file` executes."""
    disabled, policy = route_turn_policy(SPANISH_REQUEST)
    turn = run_turn(
        SPANISH_REQUEST, workspace,
        disabled_tools=disabled, tool_policy=policy,
        calls=["read_file"],
    )
    assert "read_file" in turn.offered, turn.offered
    assert not turn.traps, (
        f"offered then refused: {turn.traps} "
        f"({ {t: turn.refusals[t].get('output') for t in turn.traps} })"
    )
    assert turn.executed == ["read_file"], turn.executed


@pytest.mark.parametrize(
    "tool", ["read_file", "ls", "edit_file", "apply_patch"]
)
@pytest.mark.parametrize(
    "message", [
        pytest.param(SPANISH_REQUEST, id="es"),
        pytest.param(ENGLISH_REQUEST, id="en"),
    ],
)
def test_every_floor_tool_the_turn_offers_also_runs(tool, message, workspace):
    """All four floor tools, both languages, through the real route policy."""
    disabled, policy = route_turn_policy(message)
    turn = run_turn(
        message, workspace, disabled_tools=disabled, tool_policy=policy,
        calls=[tool],
    )
    assert tool in turn.offered, f"{tool} was not offered: {turn.offered}"
    assert tool in turn.executed, (
        f"{tool} was offered and then refused: "
        f"{turn.refusals.get(tool, {}).get('output')}"
    )


def test_the_dispatcher_gate_downstream_agrees_with_the_loop(workspace):
    """The loop is not the last gate; `src/tool_execution.py` gates again.

    It re-checks `disabled_tools` and `tool_policy` on the values the loop
    passes it, so reconciling only inside the loop would move the refusal one
    frame down the stack instead of removing it. Assert on what the loop
    actually handed over.
    """
    disabled, policy = route_turn_policy(SPANISH_REQUEST)
    turn = run_turn(
        SPANISH_REQUEST, workspace, disabled_tools=disabled, tool_policy=policy,
        calls=["read_file"],
    )
    assert turn.exec_kwargs, "no tool reached the dispatcher"
    handed = turn.exec_kwargs[0]
    downstream_denylist = set(handed.get("disabled_tools") or ())
    downstream_policy = handed.get("tool_policy")
    for name in ("read_file", "ls", "edit_file", "apply_patch"):
        assert name not in downstream_denylist, (
            f"loop passed a floor tool down as disabled: {name}"
        )
        assert not (downstream_policy and downstream_policy.blocks(name)), (
            f"loop passed a policy down that still blocks {name}"
        )


def test_a_blocked_call_is_reported_to_the_model_and_the_ui(workspace):
    """`bash` is NOT floored, so this turn legitimately refuses it.

    The refusal must be visible as such: a blocked result, a reason the model
    can read in the next round, and no pretence that it ran.
    """
    disabled, policy = route_turn_policy(SPANISH_REQUEST)
    assert "bash" in disabled, "fixture no longer withholds bash"
    turn = run_turn(
        SPANISH_REQUEST, workspace, disabled_tools=disabled, tool_policy=policy,
        calls=["bash"],
    )
    assert "bash" not in turn.offered, "bash must not be offered here"
    assert "bash" not in turn.executed
    assert "bash" in turn.refusals
    assert not turn.traps, turn.traps


# --------------------------------------------------------------------------
# 2. The general invariant: offered == executable, for any turn
# --------------------------------------------------------------------------

COHERENCE_TURNS = [
    pytest.param(SPANISH_REQUEST, dict(), id="es-live-failure"),
    pytest.param(ENGLISH_REQUEST, dict(), id="en-live-failure"),
    pytest.param("arregla el bug del carrito", dict(), id="es-plain"),
    pytest.param("fix the cart bug in cart.py", dict(), id="en-plain"),
    pytest.param("mira el proyecto", dict(), id="es-vague"),
    pytest.param("look at this", dict(), id="en-vague"),
    pytest.param("refactor the rate limiter in cart.py", dict(), id="rate"),
    pytest.param("bump the package to the latest version", dict(), id="latest"),
    pytest.param("add a search() method to the index", dict(), id="search"),
    pytest.param("busca en la web el tipo de cambio de hoy", dict(), id="real-web"),
    pytest.param("lee cart.py y explicame que hace", dict(), id="read-explain"),
    pytest.param(SPANISH_REQUEST, dict(plan_mode=True), id="plan-mode"),
    pytest.param(SPANISH_REQUEST, dict(no_workspace=True), id="no-workspace"),
    pytest.param(ENGLISH_REQUEST, dict(no_workspace=True), id="no-workspace-en"),
    pytest.param(
        SPANISH_REQUEST,
        dict(settings={"disabled_tools": ["read_file", "edit_file"]},
             extra_disabled=["read_file", "edit_file"]),
        id="operator-off",
    ),
    pytest.param(
        SPANISH_REQUEST, dict(extra_disabled=["glob", "grep", "todowrite"]),
        id="extra-denials",
    ),
    pytest.param(
        SPANISH_REQUEST + " Do not use any tools, just tell me how.",
        dict(), id="guide-only",
    ),
]


def probe_every_offered_tool(message, workspace_path, opts):
    """Ask one turn what it offers, then ask it to run each of those, one per turn.

    One call per turn, not one batch: `ask_user` (and friends) legitimately end
    a round, so a 15-call batch stops after the second tool and a batched
    version of this test passes while the invariant is broken — it did, on the
    first draft, which is why it is spelled out here.

    Returns `(offered, refusals, alarm_lines)`.
    """
    disabled, policy = route_turn_policy(
        message, extra_disabled=opts.get("extra_disabled", ())
    )
    common = dict(
        disabled_tools=disabled,
        tool_policy=policy,
        plan_mode=opts.get("plan_mode", False),
        settings=opts.get("settings"),
    )
    target = None if opts.get("no_workspace") else workspace_path
    offered = run_turn(message, target, calls=(), **common).offered
    refusals, alarms = {}, []
    for tool in offered:
        with mock.patch.object(agent_loop.logger, "error") as spy:
            turn = run_turn(message, target, calls=[tool], **common)
        refusals.update(turn.refusals)
        alarms.extend(
            str(call.args[0]) % call.args[1:] if len(call.args) > 1 else str(call.args[0])
            for call in spy.call_args_list
            if "[tool-coherence]" in str(call.args[0])
        )
    return offered, refusals, alarms


@pytest.mark.parametrize("message,opts", COHERENCE_TURNS)
def test_offered_set_equals_executable_set(message, opts, workspace):
    """For ANY turn: every tool the round offered, that same round can run.

    Not a rule about `read_file`, and not a rule about the workspace floor — a
    rule about the two lists. Each tool the turn advertised is called back, and
    any that comes back refused is a trap the runtime built and then put a door
    sign on. Failure names them, and the loop's own `[tool-coherence]` alarm
    has to have stayed silent throughout.
    """
    offered, refusals, alarms = probe_every_offered_tool(message, workspace, opts)
    traps = sorted(set(offered) & set(refusals))
    assert not traps, (
        "tools offered to the model and then refused by the same turn: "
        + ", ".join(f"{t} ({refusals[t].get('output')})" for t in traps)
    )
    assert not alarms, alarms


class _DriftingPolicy(ToolPolicy):
    """A policy whose `blocks()` disagrees with `all_disabled_names()`.

    The schema filter subtracts names; the execution gate asks a predicate.
    When those two stop describing the same set, a tool ships and is then
    refused — the live bug exactly, though there the drift came from a local
    subtraction inside `_tool_schemas_for_route` rather than from the policy
    object. The alarm cannot tell the two apart and should not: it watches the
    consequence, so it keeps working for divergences nobody has invented yet.
    """

    def blocks(self, tool_name):  # noqa: D102 - see class docstring
        return tool_name == "glob" or super().blocks(tool_name)


def test_the_coherence_alarm_actually_fires_when_the_invariant_breaks(workspace, caplog):
    """A silent alarm proves nothing. Break the invariant on purpose."""
    with caplog.at_level("ERROR", logger="src.agent_loop"):
        turn = run_turn(
            SPANISH_REQUEST, workspace, tool_policy=_DriftingPolicy(), calls=["glob"]
        )
    assert "glob" in turn.offered, "fixture no longer offers glob"
    assert turn.traps == ["glob"], turn.traps
    tripped = [r.getMessage() for r in caplog.records if "[tool-coherence]" in r.getMessage()]
    assert tripped and "glob" in tripped[0], tripped


def test_a_policy_that_hides_a_tool_also_keeps_it_off_the_list(workspace):
    """The healthy coupling, stated: a real `ToolPolicy` denial removes the tool.

    `stream_agent_loop` folds `all_disabled_names()` into `disabled_tools`, so
    an ordinary policy denial reaches the schema filter too and no trap can
    form. `_DriftingPolicy` above has to override `blocks` precisely because
    this is true.
    """
    turn = run_turn(
        SPANISH_REQUEST, workspace,
        tool_policy=ToolPolicy(disabled_tools=frozenset({"glob"})), calls=["glob"],
    )
    assert "glob" not in turn.offered, turn.offered
    assert "glob" not in turn.executed, turn.executed
    assert not turn.traps, turn.traps


def test_a_fenced_route_gets_the_floor_in_its_prompt_and_can_run_it(workspace):
    """The surface the schema-local fix never reached.

    A local backend with no function-calling channel has no schema list: its
    tools are prose sections in the system prompt, built from
    `relevant_tools` MINUS `disabled_tools`. The old subtraction lived inside
    `_tool_schemas_for_route`, so this route saw the unreconciled denylist —
    the floor did not exist for it at all, and a fenced `read_file` was both
    absent from the prompt and refused at the gate. Reconciling once, upstream,
    fixes this route as a side effect of fixing the other one.
    """
    disabled, policy = route_turn_policy(SPANISH_REQUEST)
    turn = run_turn(
        SPANISH_REQUEST, workspace, disabled_tools=disabled, tool_policy=policy,
        supports_tools=False, calls=["read_file"],
    )
    assert turn.offered == [], "a fenced route must be sent no schemas"
    assert "read_file" in turn.prompt, "read_file is not in the prompt's tool sections"
    assert "read_file" in turn.executed, (
        f"fenced read_file refused: {turn.refusals.get('read_file', {}).get('output')}"
    )


def test_a_fenced_route_still_refuses_what_it_never_advertised(workspace):
    """The floor is four tools wide on this route too, not a general amnesty."""
    disabled, policy = route_turn_policy(SPANISH_REQUEST)
    turn = run_turn(
        SPANISH_REQUEST, workspace, disabled_tools=disabled, tool_policy=policy,
        supports_tools=False, calls=["bash"],
    )
    assert "bash" not in turn.executed, turn.executed
    assert "bash" in turn.refusals


# --------------------------------------------------------------------------
# 3. The log tells the two causes apart
# --------------------------------------------------------------------------

def test_the_block_log_names_the_predicate_the_policy_and_the_reason(workspace, caplog):
    """`current_tool_policy` for two different gates cost twenty minutes.

    A block line now carries which gate closed (`source`), which named policy
    inside it (`policy`), where the name entered the denylist (`origin`), the
    spelling that matched, and the sentence the model was handed.
    """
    disabled, policy = route_turn_policy(SPANISH_REQUEST)
    with caplog.at_level("INFO", logger="src.agent_loop"):
        run_turn(
            SPANISH_REQUEST, workspace, disabled_tools=disabled,
            tool_policy=policy, calls=["bash"],
        )
    lines = blocked_log_lines(caplog)
    assert lines, "no block line emitted"
    line = lines[0]
    assert "current_tool_policy" not in line, f"still one word for two gates: {line}"
    for field in ("tool=bash", "source=", "policy=", "origin=", "matched=", "reason="):
        assert field in line, f"{field!r} missing from {line}"
    assert "source=tool_policy" in line, line


def test_a_denylist_only_block_says_disabled_tools_not_tool_policy(workspace, caplog):
    """The other gate, named as itself.

    With no `ToolPolicy` at all, the same refusal comes from `disabled_tools`,
    and the line has to say so — that difference is the whole diagnosis.
    """
    with caplog.at_level("INFO", logger="src.agent_loop"):
        run_turn(
            SPANISH_REQUEST, workspace,
            disabled_tools={"bash"}, tool_policy=None, calls=["bash"],
        )
    lines = blocked_log_lines(caplog)
    assert lines, "no block line emitted"
    assert "source=disabled_tools" in lines[0], lines[0]
    assert "source=tool_policy" not in lines[0], lines[0]


def test_the_two_gates_produce_different_lines_for_the_same_tool(workspace, caplog):
    """Same tool, same round number, two causes — two distinguishable lines."""
    with caplog.at_level("INFO", logger="src.agent_loop"):
        run_turn(SPANISH_REQUEST, workspace, disabled_tools={"bash"}, calls=["bash"])
        by_denylist = blocked_log_lines(caplog)[-1]
        caplog.clear()
        run_turn(
            SPANISH_REQUEST, workspace,
            tool_policy=ToolPolicy(disabled_tools=frozenset({"bash"})),
            calls=["bash"],
        )
        by_policy = blocked_log_lines(caplog)[-1]
    assert by_denylist != by_policy, by_denylist
    assert "source=disabled_tools" in by_denylist
    assert "source=tool_policy" in by_policy


def test_a_guide_only_block_names_the_guide_only_policy(workspace, caplog):
    """The policy's own name and mode reach the log, not a generic label."""
    message = SPANISH_REQUEST + " Do not use any tools, just tell me how."
    disabled, policy = route_turn_policy(message)
    assert policy.mode == "guide_only", "fixture no longer builds a guide-only turn"
    with caplog.at_level("INFO", logger="src.agent_loop"):
        run_turn(
            message, workspace, disabled_tools=disabled, tool_policy=policy,
            calls=["read_file"],
        )
    lines = blocked_log_lines(caplog)
    assert lines, "guide-only call was not blocked"
    assert "mode=guide_only" in lines[0] and "block_all" in lines[0], lines[0]


def test_the_blocked_result_carries_the_same_three_fields(workspace):
    """What the log says, the result says too — for the UI and the model."""
    turn = run_turn(SPANISH_REQUEST, workspace, disabled_tools={"bash"}, calls=["bash"])
    blocked = [
        e for e in turn.events
        if e.get("type") == "tool_output" and e.get("tool") == "bash"
    ]
    assert blocked, "no tool_output for the blocked call"
    assert blocked[0].get("blocked") is True
    assert blocked[0].get("policy") == "disabled_tools"


def test_the_blocked_result_also_carries_the_spelling_that_matched(workspace):
    """The one field a reader downstream can test against a policy set.

    A denylist written `bash` and a call made in any of its policy-equivalent
    spellings are the same denial; a reader given only the spelling the model
    typed can conclude "no rule denies this" about a call a rule just denied.
    src/agent_tools/subagent_tools.py resolves a worker's refusal cause from
    this field, so it has to reach the stream.
    """
    turn = run_turn(SPANISH_REQUEST, workspace, disabled_tools={"bash"}, calls=["bash"])
    blocked = [e for e in turn.events if e.get("type") == "tool_output" and e.get("tool") == "bash"]
    assert blocked and blocked[0].get("policy_matched") == "bash"


@pytest.mark.parametrize(
    "policy_kwargs,expected_source,expected_mode",
    [
        (dict(disabled_tools=frozenset({"bash"})), "tool_policy", "mode=normal"),
        (
            dict(disabled_tools=frozenset({"bash"}), mode="guide_only",
                 block_all_tool_calls=True),
            "tool_policy",
            "mode=guide_only,block_all",
        ),
    ],
)
def test_denial_records_name_their_own_policy(policy_kwargs, expected_source, expected_mode):
    """The decision function, asserted directly on its own output."""
    denial = agent_loop._denial_for_tool(
        "bash", tool_policy=ToolPolicy(**policy_kwargs), disabled_tools={"bash"}
    )
    assert denial is not None
    assert denial.source == expected_source
    assert expected_mode in denial.policy


def test_a_denial_reports_the_origin_the_loop_recorded(workspace, caplog):
    """`origin` attributes the name to the composition point that added it."""
    with caplog.at_level("INFO", logger="src.agent_loop"):
        run_turn(SPANISH_REQUEST, workspace, plan_mode=True, calls=["bash"])
    lines = blocked_log_lines(caplog)
    assert lines, "plan mode did not block bash"
    assert "origin=plan_mode_readonly" in lines[0], lines[0]


def test_the_preflight_keeps_its_own_reason_and_its_own_source(workspace, caplog):
    """The third cause stays distinguishable from the other two.

    A chat with no project cannot run `project_context`; the model is told
    that, not "disabled", and the log says `tool_preflight`, not either gate.
    """
    with mock.patch("services.projects.project_for_session", lambda *a, **k: None), \
            caplog.at_level("INFO", logger="src.agent_loop"):
        turn = run_turn(
            SPANISH_REQUEST, workspace, calls=["project_context"],
            relevant_tools={"project_context", "read_file", "ls"},
        )
    lines = blocked_log_lines(caplog)
    assert lines, "the pruned tool was not blocked"
    assert "source=tool_preflight" in lines[0], lines[0]
    assert "this chat is not attached to a project" in lines[0], lines[0]
    refusal = turn.refusals.get("project_context", {})
    assert "this chat is not attached to a project" in str(refusal.get("output")), refusal


# --------------------------------------------------------------------------
# 4. Counter-tests: the reconciliation never relaxes an authorization
# --------------------------------------------------------------------------

def test_guide_only_still_sends_and_runs_nothing(workspace):
    """"Do not use any tools" is the user's instruction. Both lists stay empty."""
    message = SPANISH_REQUEST + " Do not use any tools, just tell me how."
    disabled, policy = route_turn_policy(message)
    assert policy.mode == "guide_only"
    turn = run_turn(
        message, workspace, disabled_tools=disabled, tool_policy=policy,
        calls=["read_file", "ls", "edit_file", "apply_patch", "bash"],
    )
    assert turn.offered == [], f"guide-only turn was handed tools: {turn.offered}"
    assert turn.executed == [], f"guide-only turn ran tools: {turn.executed}"


def test_block_all_tool_calls_is_not_exemptible():
    """A blanket block has no exemptions, floor or otherwise."""
    policy = ToolPolicy(block_all_tool_calls=True, mode="guide_only")
    assert policy.exempting(agent_loop.WORKSPACE_TOOL_FLOOR) is policy
    for name in agent_loop.WORKSPACE_TOOL_FLOOR:
        assert policy.exempting(agent_loop.WORKSPACE_TOOL_FLOOR).blocks(name)


def test_a_block_all_policy_refuses_a_floor_tool_end_to_end(workspace):
    """The same thing through the loop, not just the dataclass."""
    policy = ToolPolicy(block_all_tool_calls=True, mode="guide_only")
    turn = run_turn(
        SPANISH_REQUEST, workspace, tool_policy=policy,
        calls=["read_file", "edit_file"],
    )
    assert turn.executed == [], turn.executed


def test_plan_mode_runs_reads_and_refuses_writes(workspace):
    """Plan mode investigates; the reconciliation must not hand it an editor."""
    disabled, policy = route_turn_policy(SPANISH_REQUEST)
    turn = run_turn(
        SPANISH_REQUEST, workspace, disabled_tools=disabled, tool_policy=policy,
        plan_mode=True, calls=["read_file", "ls", "edit_file", "apply_patch", "bash"],
    )
    assert "read_file" in turn.executed and "ls" in turn.executed, turn.executed
    assert not set(turn.executed) & {"edit_file", "apply_patch", "bash"}, turn.executed
    assert not turn.traps, turn.traps


def test_non_admin_denylist_outranks_the_reconciliation(workspace):
    """`blocked_tools_for_owner` is authorization; the floor never touches it."""
    from src.tool_security import NON_ADMIN_BLOCKED_TOOLS

    disabled, policy = route_turn_policy(SPANISH_REQUEST)
    with mock.patch.object(
        agent_loop, "blocked_tools_for_owner", lambda o: set(NON_ADMIN_BLOCKED_TOOLS)
    ):
        turn = run_turn(
            SPANISH_REQUEST, workspace, owner="publicuser",
            disabled_tools=disabled, tool_policy=policy,
            calls=["read_file", "edit_file", "ls", "apply_patch", "bash"],
        )
    assert not set(turn.executed) & set(NON_ADMIN_BLOCKED_TOOLS), turn.executed
    assert not set(turn.offered) & set(NON_ADMIN_BLOCKED_TOOLS), turn.offered


def test_operator_disabled_tools_setting_outranks_the_reconciliation(workspace):
    """An operator who switched a tool off in Settings meant it — both lists."""
    operator_off = ["read_file", "edit_file"]
    disabled, policy = route_turn_policy(SPANISH_REQUEST, extra_disabled=operator_off)
    turn = run_turn(
        SPANISH_REQUEST, workspace, disabled_tools=disabled, tool_policy=policy,
        settings={"disabled_tools": operator_off},
        calls=["read_file", "edit_file", "ls", "apply_patch"],
    )
    assert "read_file" not in turn.offered and "edit_file" not in turn.offered
    assert "read_file" not in turn.executed and "edit_file" not in turn.executed
    # The rest of the floor is untouched by that setting: `ls` runs, and
    # `apply_patch` is offered and reaches the approval gate — which is a
    # different thing from a refusal, and the point of the distinction. A
    # policy block ends the call; an approval card hands it to the user.
    assert "ls" in turn.executed, turn.executed
    assert "apply_patch" in turn.offered, turn.offered
    assert "apply_patch" not in turn.refusals, turn.refusals
    assert any(
        e.get("type") == "tool_output"
        and e.get("tool") == "apply_patch"
        and "approval" in str(e.get("output", "")).lower()
        for e in turn.events
    ), "apply_patch neither ran nor asked for approval"
    assert not turn.traps, turn.traps


def test_the_preflight_denials_are_not_relaxed(workspace):
    """A preflighted tool stays refused; `prune_for_turn` already spared the floor."""
    with mock.patch("services.projects.project_for_session", lambda *a, **k: None):
        turn = run_turn(
            SPANISH_REQUEST, workspace, calls=["project_context", "read_file"],
            relevant_tools={"project_context", "read_file", "ls"},
        )
    assert "project_context" not in turn.offered, turn.offered
    assert "project_context" not in turn.executed, turn.executed
    assert "read_file" in turn.executed, turn.executed


def test_the_privileged_trio_is_never_resurrected(workspace):
    """`bash`, `python` and `write_file` are withholdable, and stay withheld."""
    disabled, policy = route_turn_policy(SPANISH_REQUEST)
    turn = run_turn(
        SPANISH_REQUEST, workspace, disabled_tools=disabled, tool_policy=policy,
        calls=["bash", "python", "write_file", "manage_memory", "send_email"],
    )
    assert turn.executed == [], turn.executed
    assert not set(turn.offered) & {"bash", "python", "write_file", "manage_memory"}


def test_no_workspace_no_reconciliation(workspace):
    """Without a bound folder there is no floor, so nothing is exempted."""
    disabled, policy = route_turn_policy(SPANISH_REQUEST)
    turn = run_turn(
        SPANISH_REQUEST, None, disabled_tools=disabled, tool_policy=policy,
        calls=["read_file", "edit_file"],
    )
    assert turn.executed == [], turn.executed
    assert "read_file" not in turn.offered, turn.offered
    assert not turn.traps, turn.traps


# --------------------------------------------------------------------------
# 5. `ToolPolicy.exempting` on its own terms
# --------------------------------------------------------------------------

def test_exempting_removes_only_what_it_was_given():
    policy = ToolPolicy(
        disabled_tools=frozenset({"read_file", "bash"}),
        hidden_tools=frozenset({"read_file"}),
        reasons={"read_file": "clamped", "bash": "clamped"},
    )
    out = policy.exempting({"read_file"})
    assert not out.blocks("read_file")
    assert out.blocks("bash")
    assert "read_file" not in out.reasons and out.reasons["bash"] == "clamped"
    assert out.mode == policy.mode and out.disable_mcp == policy.disable_mcp


def test_exempting_is_a_noop_when_nothing_matches():
    policy = ToolPolicy(disabled_tools=frozenset({"bash"}))
    assert policy.exempting({"read_file"}) is policy
    assert policy.exempting(()) is policy
    assert policy.exempting(None) is policy


def test_exempting_does_not_mutate_the_original():
    policy = ToolPolicy(disabled_tools=frozenset({"read_file"}),
                        reasons={"read_file": "clamped"})
    policy.exempting({"read_file"})
    assert policy.blocks("read_file")
    assert policy.reasons["read_file"] == "clamped"


def test_exempting_clears_hidden_tools_too():
    """`hidden_tools` feeds `all_disabled_names`, which feeds the denylist."""
    policy = ToolPolicy(hidden_tools=frozenset({"read_file"}))
    assert "read_file" in policy.all_disabled_names()
    assert "read_file" not in policy.exempting({"read_file"}).all_disabled_names()
