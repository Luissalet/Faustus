"""The workspace tool floor: a coding agent always keeps read/list/edit.

Live failure this pins (Windows, Ollama `/v1`, qwen3.5:9b, Agent mode, a
workspace bound to a project holding `cart.py` and `tests/test_cart.py`):

    user> "Anade a cart.py una funcion apply_tax(total, rate) que devuelva el
           total con el impuesto aplicado, y su test en tests/test_cart.py."

    [agent-debug] tools_sent=14 tool_names=['web_search', 'web_fetch', 'grep',
     'glob', 'ls', 'get_workspace', 'apply_patch', 'todowrite',
     'delegate_agents', 'project_context', 'ask_user', 'update_plan',
     'ask_teacher', 'manage_bg_jobs']

The tool index picked the right set — `read_file`, `edit_file` and `bash` were
all in `relevant_tools` — and none of the three reached the model. The agent
spent eight rounds discovering it could not read a file in its own folder.

Root cause is NOT a route/`route_state` mismatch. `_active_route_state`
["relevant_tools"] is the very same set object the debug line prints, so the
selection that reaches `_tool_schemas_for_route` is the selection that was
logged. The divergence happens one step later, in the `disabled_tools` filter
that `_tool_schemas_for_route` applies *after* the relevant-tools filter.

`disabled_tools` arrives already composed by the turn route
(`routes/chat_routes.py`), whose "direct web lookup" clamp fires on a plain
word-boundary regex over the user's text:

    r"\\b(search|look\\s*up|lookup|google|browse|web|online|latest|current|
       today|news|weather|forecast|rate|exchange\\s+rate)\\b"

`apply_tax(total, rate)` contains `rate`. So a request to write a function
was classified as a web lookup, and the clamp disabled
`{bash, python, read_file, write_file, edit_file, manage_skills, ...}` while
re-enabling `web_search`/`web_fetch` — which is exactly the 14-name list
above, in `FUNCTION_TOOL_SCHEMAS` order. The trigger is language-independent:
the English wording of the same request contains `rate` too.

The fix is not to chase that one regex. Any upstream heuristic that folds a
guess into `disabled_tools` can silently blind the agent by the same route, so
the loop keeps a floor of its own: with a workspace bound and a route that is
sending tools at all, `read_file`, `ls` and an edit path survive
`disabled_tools`. Authorization still wins — see the counter-tests below.
"""

import asyncio
import json
import re
import unittest.mock as mock

import pytest

import src.agent_tools  # noqa: F401  - resolves the circular schema imports first
import src.agent_loop as agent_loop


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

# The exact word-boundary regex `routes/chat_routes.py` uses to set
# `_explicit_web_intent`, and the exact set that clamp folds into
# `disabled_tools` for the turn. Copied here so the reproduction stands on the
# real production input rather than an invented one.
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


def route_disabled_tools(message: str) -> set:
    """The `disabled_tools` the turn route hands the loop for this message."""
    disabled = set()
    if WEB_INTENT_RE.search(message.lower()):
        disabled |= WEB_INTENT_CLAMP
        disabled -= {"web_search", "web_fetch"}  # search enabled for the turn
    return disabled


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


def tools_sent(
    message,
    workspace,
    *,
    disabled_tools=None,
    owner="admin",
    plan_mode=False,
    tool_policy=None,
    active_document=None,
    active_email=None,
    settings=None,
    supports_tools=True,
):
    """Drive the real `stream_agent_loop` and return the tool names the model got.

    Everything between the request and the provider call is the production
    path: intent classification, tool selection, route/prompt build,
    `_tool_schemas_for_route`, and schema slimming. Only the four true edges
    are stubbed — the provider stream, the tool executor, the MCP manager, and
    the endpoint's `supports_tools` row (the user's `/v1` endpoint is
    registered as tools-capable, which is what makes the route an API route).
    """
    captured = []
    turn_settings = dict(settings or {})

    async def fake_stream(candidates, messages, **kwargs):
        captured.append(kwargs.get("tools") or [])
        yield "data: " + json.dumps({"delta": "Listo."}) + "\n\n"
        yield "data: [DONE]\n\n"

    async def fake_execute(block, *args, **kwargs):
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
        # Single-user / admin box: no public denylist. Non-admin owners keep
        # the real `blocked_tools_for_owner` so the security tests below mean
        # something.
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
                max_rounds=1,
                context_length=32768,
                disabled_tools=set(disabled_tools) if disabled_tools else None,
                plan_mode=plan_mode,
                tool_policy=tool_policy,
                active_document=active_document,
                active_email=active_email,
                harness_options={
                    "checkpoints": False,
                    "run_tests": False,
                    "repo_map": False,
                },
            )
            return [chunk async for chunk in stream]

        asyncio.run(drive())
    finally:
        for patch in patches:
            patch.stop()

    assert captured, "the loop never reached the provider"
    return sorted(
        schema.get("function", {}).get("name")
        for schema in captured[0]
        if schema.get("function")
    )


# --------------------------------------------------------------------------
# 1. The reproduction
# --------------------------------------------------------------------------

def test_web_intent_clamp_is_triggered_by_the_parameter_name():
    """`apply_tax(total, rate)` reads as a web lookup — in both languages."""
    assert WEB_INTENT_RE.search(SPANISH_REQUEST.lower()).group(0) == "rate"
    assert WEB_INTENT_RE.search(ENGLISH_REQUEST.lower()).group(0) == "rate"


def test_spanish_code_request_reaches_the_model_with_its_file_tools(workspace):
    """The live failure: read_file/edit_file/ls must reach the model."""
    names = tools_sent(
        SPANISH_REQUEST,
        workspace,
        disabled_tools=route_disabled_tools(SPANISH_REQUEST),
    )
    missing = sorted({"read_file", "edit_file", "ls"} - set(names))
    assert not missing, (
        f"agent cannot touch its own workspace: missing {missing}; got {names}"
    )


def test_debug_line_reports_the_difference_untruncated(workspace, caplog):
    """The evidence the live incident needed and did not have.

    Both name lists used to be clipped to `[:15]`, so the drop that mattered
    (`read_file`) sat past the cut of an alphabetically sorted list and the
    reader had to diff two truncated lists by eye. The line now carries the
    full sent list and both differences, and no floor tool may appear on the
    "selected but not sent" side.
    """
    with caplog.at_level("INFO", logger="src.agent_loop"):
        tools_sent(
            SPANISH_REQUEST,
            workspace,
            disabled_tools=route_disabled_tools(SPANISH_REQUEST),
        )
    debug_lines = [r.getMessage() for r in caplog.records if "[agent-debug]" in r.getMessage()]
    assert debug_lines, "no [agent-debug] line emitted"
    line = debug_lines[-1]

    assert "relevant_not_sent=" in line and "sent_not_relevant=" in line, (
        f"debug line reports no difference: {line}"
    )
    # No ellipsis and no clipped list: every name is accounted for.
    assert "..." not in line and "…" not in line, f"debug line truncated: {line}"
    dropped = re.search(r"relevant_not_sent=\[(.*?)\]", line).group(1)
    assert not (set(re.findall(r"'([^']+)'", dropped)) & {"read_file", "edit_file", "ls", "apply_patch"}), (
        f"a floor tool was dropped on the way to the model: {line}"
    )
    # The tools the clamp may legitimately withhold are still reported, so the
    # line explains the gap rather than hiding it.
    assert "bash" in dropped and "write_file" in dropped, (
        f"debug line hides the withheld tools: {line}"
    )


# --------------------------------------------------------------------------
# 2. The invariant: it does not depend on the text of the request
# --------------------------------------------------------------------------

CODE_REQUESTS = [
    pytest.param(SPANISH_REQUEST, id="es-live-failure"),
    pytest.param(ENGLISH_REQUEST, id="en-live-failure"),
    pytest.param("arregla el bug del carrito", id="es-plain"),
    pytest.param("fix the cart bug", id="en-plain"),
    # Every other word the web-intent regex reacts to, in ordinary code work.
    pytest.param("refactor the rate limiter in cart.py", id="rate"),
    pytest.param("update the current user cache", id="current"),
    pytest.param("fix the news feed parser", id="news"),
    pytest.param("rename the browse() helper in nav.js", id="browse"),
    pytest.param("bump the package to the latest version", id="latest"),
    pytest.param("add a search() method to the index", id="search"),
]
VAGUE_REQUESTS = [
    pytest.param("mira el proyecto", id="es-vague"),
    pytest.param("look at this", id="en-vague"),
]

# Each request is run against three denylists: none, the one the route
# actually composes for that wording, and the full web-intent clamp. The floor
# must survive all three.
DENYLISTS = ("none", "route", "full-clamp")


def denylist(kind, message):
    if kind == "none":
        return set()
    if kind == "route":
        return route_disabled_tools(message)
    return set(WEB_INTENT_CLAMP)


@pytest.mark.parametrize("kind", DENYLISTS)
@pytest.mark.parametrize("message", CODE_REQUESTS + VAGUE_REQUESTS)
def test_read_floor_holds_for_any_request_text(message, kind, workspace):
    """Workspace bound + tools-capable route => read_file and ls always sent.

    This half of the floor has no wording condition at all: a workspace agent
    that cannot read or list is not an agent, whatever it was asked.
    """
    disabled = denylist(kind, message)
    names = set(tools_sent(message, workspace, disabled_tools=disabled))
    assert "read_file" in names, f"{message!r} disabled={sorted(disabled)}: {sorted(names)}"
    assert "ls" in names, f"{message!r} disabled={sorted(disabled)}: {sorted(names)}"


@pytest.mark.parametrize("kind", DENYLISTS)
@pytest.mark.parametrize("message", CODE_REQUESTS)
def test_edit_floor_holds_for_any_code_request(message, kind, workspace):
    """A request to change the folder always keeps an edit path."""
    disabled = denylist(kind, message)
    names = set(tools_sent(message, workspace, disabled_tools=disabled))
    assert names & {"edit_file", "apply_patch"}, (
        f"{message!r} disabled={sorted(disabled)}: no edit path in {sorted(names)}"
    )


@pytest.mark.parametrize("message", VAGUE_REQUESTS)
def test_vague_turn_keeps_its_read_only_shape(message, workspace):
    """The edit half waits for a request that actually asks for work.

    Pre-existing behaviour (`tests/test_workspace_confine.py`): a vague turn
    with a workspace bound gets tools to investigate with, not to write with.
    The floor extends the read half to that turn and leaves the rest as it was.
    """
    names = set(tools_sent(message, workspace))
    assert "read_file" in names and "ls" in names, sorted(names)
    assert not names & {"edit_file", "apply_patch", "write_file", "bash", "python"}, (
        f"vague turn was handed write/shell tools: {sorted(names)}"
    )


TRANSLATION_PAIRS = [
    pytest.param(SPANISH_REQUEST, ENGLISH_REQUEST, id="live-failure"),
    pytest.param("arregla el bug del carrito en cart.py",
                 "fix the cart bug in cart.py", id="fix-bug"),
    pytest.param("lee cart.py y explicame que hace",
                 "read cart.py and explain what it does", id="read-explain"),
    pytest.param("refactoriza tests/test_cart.py",
                 "refactor tests/test_cart.py", id="refactor"),
    pytest.param("anade una funcion apply_tax a cart.py",
                 "add an apply_tax function to cart.py", id="add-function"),
]


@pytest.mark.parametrize("spanish,english", TRANSLATION_PAIRS)
def test_same_request_in_either_language_yields_the_same_toolset(spanish, english, workspace):
    """Translation must not change what the agent can do.

    The loop's own intent classifier carries Spanish action/target vocabulary,
    so the selection is bilingual. The route's web-intent clamp is English-word
    matching, but it reads the raw text either way — `rate` fires in both — so
    the language is not what decided the live failure. This pins both halves.
    """
    es = tools_sent(spanish, workspace, disabled_tools=route_disabled_tools(spanish))
    en = tools_sent(english, workspace, disabled_tools=route_disabled_tools(english))
    assert es == en, (
        f"language changed the toolset: es-only={sorted(set(es) - set(en))}, "
        f"en-only={sorted(set(en) - set(es))}"
    )


@pytest.mark.parametrize("spanish,english", TRANSLATION_PAIRS)
def test_intent_classification_is_language_neutral(spanish, english):
    """The bilingual half, asserted directly: same domains, same signal level."""
    es = agent_loop._classify_agent_request([{"role": "user", "content": spanish}], spanish)
    en = agent_loop._classify_agent_request([{"role": "user", "content": english}], english)
    assert es["low_signal"] == en["low_signal"], (
        f"{spanish!r} low_signal={es['low_signal']} vs {english!r} low_signal={en['low_signal']}"
    )
    assert es["domains"] == en["domains"], (
        f"{spanish!r} -> {sorted(es['domains'])} vs {english!r} -> {sorted(en['domains'])}"
    )
    assert (
        agent_loop._looks_like_workspace_coding_request(spanish)
        == agent_loop._looks_like_workspace_coding_request(english)
    )


def test_accentless_spanish_is_still_a_coding_request():
    """The incident opened with "Anade" — Spanish typed without the tilde."""
    assert agent_loop._looks_like_workspace_coding_request("Anade una funcion a cart.py")
    assert agent_loop._looks_like_workspace_coding_request("Añade una función a cart.py")


def test_bare_filename_is_a_code_target():
    """A filename with no directory part is still a "work in this folder" signal."""
    assert agent_loop._looks_like_workspace_coding_request("refactor the rate limiter in cart.py")
    assert agent_loop._looks_like_workspace_coding_request("fix main.go")
    # ...without turning ordinary prose into code work.
    assert not agent_loop._looks_like_workspace_coding_request("check example.com for news")
    assert not agent_loop._looks_like_workspace_coding_request("look at the local thing")


def test_floor_does_not_widen_the_rest_of_the_toolset(workspace):
    """The floor exempts the floor tools, nothing else.

    `bash`, `python` and `write_file` are the privileged trio a route may
    legitimately withhold; the floor must never hand them back.
    """
    names = set(tools_sent(
        SPANISH_REQUEST, workspace, disabled_tools=set(WEB_INTENT_CLAMP)
    ))
    assert not names & {"bash", "python", "write_file"}, (
        f"floor resurrected privileged tools: {sorted(names & {'bash', 'python', 'write_file'})}"
    )
    assert not names & {"manage_memory", "send_email", "manage_calendar"}, (
        "floor leaked unrelated clamped tools"
    )


# --------------------------------------------------------------------------
# 3. Authorization still wins over the floor
# --------------------------------------------------------------------------

def test_guide_only_turn_still_sends_no_tools(workspace):
    """"Do not use any tools" is the user's own instruction; the floor obeys."""
    from src.tool_policy import build_effective_tool_policy

    message = SPANISH_REQUEST + " Do not use any tools, just tell me how."
    policy = build_effective_tool_policy(last_user_message=message)
    assert policy.mode == "guide_only", "test fixture no longer builds a guide-only turn"

    names = tools_sent(
        message, workspace, tool_policy=policy, disabled_tools=policy.all_disabled_names()
    )
    assert names == [], f"guide-only turn was handed tools: {names}"


def test_plan_mode_floors_reading_but_not_writing(workspace):
    """Plan mode investigates read-only; the floor must respect that."""
    names = set(tools_sent(SPANISH_REQUEST, workspace, plan_mode=True))
    assert "read_file" in names and "ls" in names, f"plan mode lost its read tools: {names}"
    assert not names & {"edit_file", "apply_patch", "write_file", "bash"}, (
        f"plan mode was handed mutating tools: {sorted(names)}"
    )


def test_non_admin_owner_keeps_the_public_denylist(workspace):
    """`blocked_tools_for_owner` outranks the floor."""
    from src.tool_security import NON_ADMIN_BLOCKED_TOOLS

    with mock.patch.object(
        agent_loop, "blocked_tools_for_owner", lambda o: set(NON_ADMIN_BLOCKED_TOOLS)
    ):
        names = set(tools_sent(SPANISH_REQUEST, workspace, owner="publicuser"))
    assert not names & {"read_file", "edit_file", "ls", "apply_patch", "bash"}, (
        f"public user was handed file tools: {sorted(names)}"
    )


def test_operator_disabled_tools_setting_outranks_the_floor(workspace):
    """An explicit `disabled_tools` setting is a choice, not a heuristic.

    The route folds that setting into the turn's `disabled_tools`; the loop
    reads the same setting so it knows those two denials are deliberate and
    leaves them out of the floor.
    """
    operator_off = ["read_file", "edit_file"]
    names = set(tools_sent(
        SPANISH_REQUEST,
        workspace,
        disabled_tools=route_disabled_tools(SPANISH_REQUEST) | set(operator_off),
        settings={"disabled_tools": operator_off},
    ))
    assert "read_file" not in names and "edit_file" not in names, (
        f"floor overrode the operator's own setting: {sorted(names)}"
    )
    # The rest of the floor is untouched by that setting.
    assert "ls" in names and "apply_patch" in names


def test_no_workspace_means_no_floor(tmp_path):
    """Without a bound workspace there is nothing to floor."""
    names = set(tools_sent(
        SPANISH_REQUEST, None, disabled_tools=set(WEB_INTENT_CLAMP)
    ))
    assert "read_file" not in names, f"floor fired without a workspace: {sorted(names)}"


def test_model_without_tool_support_still_gets_no_schemas(workspace):
    """A non-API route sends no function schemas; the floor does not force any."""
    names = tools_sent(SPANISH_REQUEST, workspace, supports_tools=False)
    assert names == [], f"non-tool route was handed schemas: {names}"
