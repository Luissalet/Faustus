"""Tool preflight: an impossible tool is never put on the model's list.

Live failure this pins (Agent mode, a linked folder, a chat that is NOT inside
a project, a local 9B model at ~1.3 tok/s):

    round 1  project_context -> {"error": "This chat is not attached to a
                                 project", "exit_code": 1}
    round 2  project_context -> {"error": "This chat is not attached to a
                                 project", "exit_code": 1}

The tool could not have worked at any point in that turn: the chat has no
project, and `project_context` opens by resolving one. Two rounds and two
transcript failures were spent on a fact the runtime knew before the first
token. The same log carried "SMTP not configured — add an Email Account in
Settings" and "IMAP not configured", so the email tools were being offered on a
box with no mailbox either — a pattern, not one tool.

A large model reads the description, works out it does not apply, and moves on;
a 9B model tries it, fails, and tries again. So the runtime removes what it can
prove cannot succeed (`src/tool_preflight.py`), rather than asking the model to
be smarter.

Everything below drives the real `stream_agent_loop`. Only the true edges are
stubbed — the provider stream, the tool executor, the MCP manager, the
endpoint's `supports_tools` row — plus the three facts a rule reads
(`project_for_session`, `_get_email_config`, `load_integrations`), which are
the environment, not the code under test.
"""

import asyncio
import json
import re
import unittest.mock as mock

import pytest

import src.agent_tools  # noqa: F401  - resolves the circular schema imports first
import src.agent_loop as agent_loop
import src.tool_preflight as preflight


OLLAMA_V1 = "http://127.0.0.1:11434/v1/chat/completions"
MODEL = "qwen3.5:9b"

# The measured turn: a linked folder, agent mode, an ordinary code request.
CODE_REQUEST = (
    "Anade a cart.py una funcion apply_tax(total, rate) que devuelva el total "
    "con el impuesto aplicado, y su test en tests/test_cart.py."
)
EMAIL_REQUEST = "read my last 5 emails and tell me which ones need a reply"

A_PROJECT = {"id": "p1", "name": "Cart", "folder": "cart", "enabled": True}

# The legacy branch of `_get_email_config`: no account row resolved, no flat
# keys in settings.json, no SMTP_*/IMAP_* env vars. This is the dict that made
# the observed run log "SMTP not configured — add an Email Account in Settings".
NO_MAILBOX = {
    "account_id": None,
    "account_name": "legacy",
    "smtp_host": "", "smtp_port": 465, "smtp_user": "", "smtp_password": "",
    "imap_host": "", "imap_port": 993, "imap_user": "", "imap_password": "",
    "from_address": "",
}
AN_ACCOUNT_ROW = dict(NO_MAILBOX, account_id="acct-1", account_name="Gmail")
LEGACY_ENV_MAILBOX = dict(
    NO_MAILBOX,
    imap_host="imap.example.com", imap_user="me@example.com", imap_password="s3cret",
)

FLOOR = {"read_file", "ls", "edit_file", "apply_patch"}


@pytest.fixture(autouse=True)
def _desktop_is_available(monkeypatch):
    """Rule 4 (desktop) prunes all seven desktop tools on a headless box —
    which is what a CI runner is. The contracts below describe a box where
    "everything is configured", so give it a desktop and the default mode;
    tests/test_desktop_tools.py covers the rule itself."""
    from src.agent_tools import desktop_tools as dt

    class _Desk(dt.DesktopBackend):
        def available(self):
            return True, ""

    monkeypatch.setattr(dt, "get_backend", lambda: _Desk())
    monkeypatch.setattr("src.tool_capabilities.desktop_control_mode", lambda: "ask_each")


@pytest.fixture()
def workspace(tmp_path):
    """The user's linked folder: a tiny project with a test."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "cart.py").write_text(
        "def total(items):\n    return sum(i['price'] for i in items)\n"
    )
    (tmp_path / "tests" / "test_cart.py").write_text(
        "from cart import total\n\n\ndef test_total():\n    assert total([]) == 0\n"
    )
    return str(tmp_path)


def run_turn(
    message,
    workspace=None,
    *,
    project=None,
    email_config=NO_MAILBOX,
    integrations=(),
    settings=None,
    supports_tools=True,
    responses=("Listo.",),
    owner="admin",
    session_id="s-cart",
    project_for_session=None,
    relevant_tools=None,
):
    """Drive `stream_agent_loop` and report what the model was actually given.

    Returns (tool_names, prompt_text, events):
      * tool_names — the function schemas of the first round (API routes).
      * prompt_text — every message of the LAST round, which is where a
        non-API route lists its tools, and where a tool result from the
        previous round comes back to the model.
      * events — the decoded SSE payloads, for the tool_output of a call the
        model made anyway.

    `supports_tools=False` models the other real route shape: a local backend
    with no function-calling channel at all, whose tools live in the prompt as
    fenced sections and whose only way to call one is to write that fence.
    """
    captured_tools = []
    captured_messages = []
    turn_settings = dict(settings or {})
    replies = list(responses)

    async def fake_stream(candidates, messages, **kwargs):
        captured_tools.append(kwargs.get("tools") or [])
        captured_messages.append(messages)
        body = replies.pop(0) if replies else "Listo."
        yield "data: " + json.dumps({"delta": body}) + "\n\n"
        yield "data: [DONE]\n\n"

    async def fake_execute(block, *args, **kwargs):
        return (block.tool_type, {"output": "ok", "exit_code": 0})

    def fake_get_setting(key, default=None):
        return turn_settings.get(key, default)

    resolver = project_for_session or (lambda session_id, owner=None: project)

    patches = [
        mock.patch.object(agent_loop, "stream_llm_with_fallback", fake_stream),
        mock.patch.object(agent_loop, "execute_tool_block", fake_execute),
        mock.patch.object(agent_loop, "get_mcp_manager", lambda: None),
        mock.patch.object(agent_loop, "estimate_tokens", lambda *a, **k: 10),
        mock.patch.object(agent_loop, "get_setting", fake_get_setting),
        mock.patch.object(agent_loop, "blocked_tools_for_owner", lambda o: set()),
        mock.patch.object(
            agent_loop,
            "_agent_route_tool_mode",
            lambda *a, **k: (bool(supports_tools), False, bool(supports_tools)),
        ),
        mock.patch("services.projects.project_for_session", resolver),
        mock.patch(
            "routes.email_helpers._get_email_config",
            lambda *a, **k: dict(email_config),
        ),
        mock.patch("src.integrations.load_integrations", lambda: list(integrations)),
    ]

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
                session_id=session_id,
                max_rounds=2,
                context_length=32768,
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
    names = sorted(
        schema.get("function", {}).get("name")
        for schema in captured_tools[0]
        if schema.get("function")
    )
    prompt = "\n".join(
        str(m.get("content") or "") for m in captured_messages[-1]
        if isinstance(m, dict)
    )
    events = []
    for chunk in chunks:
        if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
            try:
                events.append(json.loads(chunk[6:]))
            except json.JSONDecodeError:
                pass
    return names, prompt, events


def tools_sent(*args, **kwargs):
    return set(run_turn(*args, **kwargs)[0])


# --------------------------------------------------------------------------
# 1. The reproduction: project tools in a chat with no project
# --------------------------------------------------------------------------

def test_chat_without_a_project_is_not_offered_project_context(workspace):
    """The measured failure: the trap is gone before the model can step in it."""
    names = tools_sent(CODE_REQUEST, workspace, project=None)
    assert "project_context" not in names, sorted(names)


def test_chat_inside_a_project_still_gets_project_context(workspace):
    """The other half. The rule reads the chat, not the request."""
    names = tools_sent(CODE_REQUEST, workspace, project=A_PROJECT)
    assert "project_context" in names, sorted(names)


def test_search_project_chats_follows_the_same_condition():
    """It opens with the identical two lines in `src/tool_execution.py`.

    Pinned tool set (the shape the scheduler and an approval replay use), so
    the assertion is about the preflight and not about what retrieval felt
    like selecting for this wording.
    """
    pinned = {"search_project_chats", "search_chats", "ask_user"}
    without = tools_sent("what did we decide about the cart last week?",
                         project=None, relevant_tools=pinned)
    within = tools_sent("what did we decide about the cart last week?",
                        project=A_PROJECT, relevant_tools=pinned)
    assert "search_project_chats" not in without, sorted(without)
    assert "search_project_chats" in within, sorted(within)
    # `search_chats` searches every transcript and needs no project: untouched.
    assert "search_chats" in without and "search_chats" in within


def test_the_rule_asks_the_same_resolver_the_tool_asks(workspace):
    """`project_context` calls `project_for_session`; so does the rule.

    Not a re-derivation of "is this chat in a project" from session rows — the
    same function, so the prediction and the runtime failure cannot drift.
    """
    calls = []

    def spy(session_id, owner=None):
        calls.append((session_id, owner))
        return None

    tools_sent(CODE_REQUEST, workspace, project_for_session=spy,
               session_id="s-live", owner=None)
    assert calls, "the rule never consulted the resolver"
    assert calls[0] == ("s-live", None), calls
    # owner=None matters: `services.projects._owned` reads it as "any owner",
    # while "" is a user id that matches nothing. The rule must hand the
    # resolver the value the executor would, or it answers a different
    # question than the tool will.


def test_the_route_and_the_preflight_cannot_disagree(workspace):
    """`routes/chat_routes.py` force-includes these two tools when
    `project_for_session` finds a project; this rule removes them when it does
    not. Both ask that one function, so "forced in, then pruned out" — the
    contradiction that would make the turn nonsense — cannot happen.
    """
    forced = {"project_context", "search_project_chats", "ask_user"}
    within = tools_sent("what is in this project?", workspace,
                        project=A_PROJECT, relevant_tools=forced)
    assert {"project_context", "search_project_chats"} <= within, sorted(within)


# --------------------------------------------------------------------------
# 2. Both surfaces: function schemas AND the prompt's tool sections
# --------------------------------------------------------------------------

def test_a_non_api_route_loses_the_tool_section_too(workspace):
    """A textual model reads tools from the prompt, not from schemas.

    The measured model is a small local one; if the pruning only reached the
    function schemas it would do nothing for exactly the model that needs it.
    """
    _, without, _ = run_turn(CODE_REQUEST, workspace, project=None,
                             supports_tools=False)
    _, within, _ = run_turn(CODE_REQUEST, workspace, project=A_PROJECT,
                            supports_tools=False)
    assert "```project_context```" in within, "fixture no longer lists the tool"
    assert "```project_context```" not in without


# --------------------------------------------------------------------------
# 3. The [agent-debug] line carries what was removed and why
# --------------------------------------------------------------------------

def debug_line(caplog):
    lines = [r.getMessage() for r in caplog.records if "[agent-debug]" in r.getMessage()]
    assert lines, "no [agent-debug] line emitted"
    return lines[-1]


def test_debug_line_names_the_pruned_tools_and_the_reason(workspace, caplog):
    """The line that makes this diagnosable: what left, and why it left."""
    with caplog.at_level("INFO", logger="src.agent_loop"):
        tools_sent(CODE_REQUEST, workspace, project=None)
    line = debug_line(caplog)
    pruned = re.search(r"pruned=\{(.*?)\}", line)
    assert pruned, f"debug line carries no pruned= section: {line}"
    assert "project_context: this chat is not attached to a project" in pruned.group(1)
    # The pre-existing halves of the line are untouched.
    assert "relevant_not_sent=" in line and "sent_not_relevant=" in line, line
    assert "..." not in line and "…" not in line, f"debug line truncated: {line}"


def test_debug_line_keeps_its_shape_when_nothing_is_pruned(workspace, caplog):
    """`pruned={}` is emitted too — a fixed shape is what makes it greppable."""
    with caplog.at_level("INFO", logger="src.agent_loop"):
        tools_sent(CODE_REQUEST, workspace, project=A_PROJECT,
                   email_config=AN_ACCOUNT_ROW, integrations=[{"id": "ha", "name": "HA"}])
    assert "pruned={}" in debug_line(caplog)


# --------------------------------------------------------------------------
# 4. If the model calls it anyway, it gets the reason — not "unknown tool"
# --------------------------------------------------------------------------

FENCED_CALL = 'Voy a mirar el proyecto.\n\n```project_context\n{"action": "list"}\n```'


def tool_outputs(events, tool):
    return [
        str(e.get("output") or "")
        for e in events
        if e.get("type") == "tool_output" and e.get("tool") == tool
    ]


def test_a_call_to_a_pruned_tool_answers_with_the_reason(workspace):
    """A fence needs no schema, so the model can still ask. Answer usefully.

    "this chat is not attached to a project" closes the loop in one round.
    "unknown tool" / "disabled by the current request policy" invites the retry
    that this whole mechanism exists to prevent.
    """
    _, next_round_prompt, events = run_turn(
        CODE_REQUEST, workspace, project=None, supports_tools=False,
        responses=(FENCED_CALL, "Vale, sin proyecto."),
    )
    outputs = tool_outputs(events, "project_context")
    assert outputs, f"the call was not intercepted: {[e.get('type') for e in events]}"
    error = outputs[0]
    assert "this chat is not attached to a project" in error, error
    assert "unknown tool" not in error.lower(), error
    assert "disabled by the current request policy" not in error, error
    # And the model itself is told, not just the UI: the reason comes back in
    # the next round's context, which is where a retry would be decided.
    assert "this chat is not attached to a project" in next_round_prompt


def test_an_ordinary_denial_keeps_its_own_message(workspace):
    """The preflight reason replaces nothing but its own case.

    With a project attached, `project_context` is possible; an operator switch
    is what removes it, and that denial keeps the generic policy wording.
    """
    _, _, events = run_turn(
        CODE_REQUEST, workspace, project=A_PROJECT, supports_tools=False,
        settings={"disabled_tools": ["project_context"]},
        responses=(FENCED_CALL, "Vale."),
    )
    for error in tool_outputs(events, "project_context"):
        assert "this chat is not attached to a project" not in error, error


# --------------------------------------------------------------------------
# 5. Counter-tests: what the preflight must never do
# --------------------------------------------------------------------------

WORKSPACE_REQUESTS = [
    pytest.param(CODE_REQUEST, id="es-live-failure"),
    pytest.param("fix the cart bug in cart.py", id="en-plain"),
    pytest.param("read cart.py and explain what it does", id="read-explain"),
    pytest.param("refactor tests/test_cart.py", id="refactor"),
]


@pytest.mark.parametrize("message", WORKSPACE_REQUESTS)
def test_preflight_never_touches_the_workspace_floor(message, workspace):
    """With a folder bound, read/list/edit survive every rule, always."""
    names = tools_sent(message, workspace, project=None)
    assert "read_file" in names and "ls" in names, sorted(names)
    assert names & {"edit_file", "apply_patch"}, sorted(names)


def test_the_floor_wins_when_a_rule_contradicts_it():
    """A rule reaching for a floor tool is a bug; the floor is an invariant."""
    rogue = preflight.PreflightRule(
        "rogue",
        lambda: frozenset({"read_file"}),
        lambda ctx: {"read_file": "nonsense"},
    )
    with mock.patch.object(preflight, "RULES", (rogue,)):
        assert preflight.unusable_tools({}) == {"read_file": "nonsense"}
        assert preflight.prune_for_turn({}, protected=FLOOR) == {}


def test_setting_off_prunes_nothing(workspace):
    """One switch turns the whole mechanism off."""
    names = tools_sent(CODE_REQUEST, workspace, project=None,
                       settings={"agent_tool_preflight": False})
    assert "project_context" in names, sorted(names)


def test_setting_defaults_to_on():
    from src.settings import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["agent_tool_preflight"] is True


def test_a_rule_that_raises_costs_the_turn_nothing(workspace, caplog):
    """Degrade to "remove nothing", never to a turn with tools missing."""
    def boom(session_id, owner=None):
        raise RuntimeError("projects.json is a banana")

    with caplog.at_level("INFO", logger="src.agent_loop"):
        names, _, events = run_turn(CODE_REQUEST, workspace,
                                    project_for_session=boom)
    assert "project_context" in names, sorted(names)
    assert any(e.get("type") == "metrics" for e in events), "the turn did not finish"
    assert "pruned={}" in debug_line(caplog)


def test_a_rule_that_raises_does_not_silence_the_others(workspace):
    """Rules are independent: one broken rule keeps the rest working."""
    def boom(session_id, owner=None):
        raise RuntimeError("projects.json is a banana")

    names = tools_sent(EMAIL_REQUEST, None, project_for_session=boom,
                       email_config=NO_MAILBOX)
    assert "list_emails" not in names, sorted(names)


# --------------------------------------------------------------------------
# 6. Email: no mailbox anywhere on the box
# --------------------------------------------------------------------------

EMAIL_TOOLS = {"list_emails", "read_email", "send_email", "reply_to_email",
               "list_email_accounts"}


def test_email_tools_are_not_offered_with_no_mailbox_configured():
    """The other half of the observed log: SMTP/IMAP not configured."""
    names = tools_sent(EMAIL_REQUEST, None, email_config=NO_MAILBOX)
    assert not (names & EMAIL_TOOLS), sorted(names & EMAIL_TOOLS)


def test_email_tools_survive_an_existing_account_row():
    """A resolved account row ends the question — nothing is pruned."""
    names = tools_sent(EMAIL_REQUEST, None, email_config=AN_ACCOUNT_ROW)
    assert EMAIL_TOOLS & names, sorted(names)


def test_email_tools_survive_legacy_env_credentials():
    """No account row, but IMAP_* in the environment: still a real mailbox."""
    names = tools_sent(EMAIL_REQUEST, None, email_config=LEGACY_ENV_MAILBOX)
    assert EMAIL_TOOLS & names, sorted(names)


def test_contact_tools_are_not_collateral_damage():
    """`resolve_contact`/`manage_contact` ride in the email DOMAIN, not IMAP.

    They read the address book and work perfectly with no mailbox at all; a
    rule that swept the domain instead of the tools would delete them.
    """
    pruned = preflight.unusable_tools({"session_id": "s", "owner": "admin"})
    assert "resolve_contact" not in pruned
    assert "manage_contact" not in pruned


def test_both_spellings_of_an_email_tool_are_pruned_together():
    """A denylist written bare and a call made qualified is a known bypass."""
    with mock.patch("routes.email_helpers._get_email_config", lambda *a, **k: dict(NO_MAILBOX)):
        pruned = preflight.unusable_tools({"session_id": "s", "owner": "admin"})
    assert "send_email" in pruned and "mcp__email__send_email" in pruned


# --------------------------------------------------------------------------
# 7. api_call with nothing registered to call
# --------------------------------------------------------------------------

def test_api_call_is_dropped_with_no_integrations_registered():
    """`do_api_call` matches its argument against an empty list — always fails."""
    with mock.patch("src.integrations.load_integrations", lambda: []):
        assert "api_call" in preflight.unusable_tools({})


def test_api_call_survives_a_registered_integration():
    with mock.patch("src.integrations.load_integrations",
                    lambda: [{"id": "ha", "name": "Home Assistant", "enabled": True}]):
        assert "api_call" not in preflight.unusable_tools({})


def test_a_disabled_integration_is_not_called_impossible():
    """`do_api_call` matches by id/name without checking `enabled`.

    So "every integration is disabled" is NOT a proven dead end, and the rule
    does not claim it is. Only the empty list is.
    """
    with mock.patch("src.integrations.load_integrations",
                    lambda: [{"id": "ha", "name": "Home Assistant", "enabled": False}]):
        assert "api_call" not in preflight.unusable_tools({})


# --------------------------------------------------------------------------
# 8. Rules investigated and deliberately NOT written
# --------------------------------------------------------------------------

def test_web_tools_are_never_pruned():
    """There is no reachable "no search provider" state, so there is no rule.

    `search_provider` defaults to searxng, `SEARXNG_INSTANCE` defaults to
    localhost:8080, and the default `search_fallback_chain` is DuckDuckGo,
    which needs no API key. Pruning web tools would take away a tool that
    works.
    """
    from src.settings import DEFAULT_SETTINGS
    from services.search.core import _build_provider_chain

    assert DEFAULT_SETTINGS["search_provider"] == "searxng"
    assert "duckduckgo" in DEFAULT_SETTINGS["search_fallback_chain"]
    assert _build_provider_chain("searxng"), "no provider chain at all"

    with mock.patch("routes.email_helpers._get_email_config", lambda *a, **k: dict(NO_MAILBOX)), \
         mock.patch("src.integrations.load_integrations", lambda: []), \
         mock.patch("services.projects.project_for_session", lambda *a, **k: None):
        pruned = preflight.unusable_tools({"session_id": "s", "owner": "admin"})
    assert "web_search" not in pruned and "web_fetch" not in pruned


def test_file_and_shell_tools_are_never_pruned():
    """No bound workspace is not an impossibility — they use the default cwd."""
    with mock.patch("routes.email_helpers._get_email_config", lambda *a, **k: dict(NO_MAILBOX)), \
         mock.patch("src.integrations.load_integrations", lambda: []), \
         mock.patch("services.projects.project_for_session", lambda *a, **k: None):
        pruned = preflight.unusable_tools({"session_id": "", "owner": ""})
    assert not (set(pruned) & {"bash", "python", "read_file", "write_file",
                               "edit_file", "ls", "grep", "glob", "get_workspace",
                               "edit_image", "trigger_research"}), sorted(pruned)


# --------------------------------------------------------------------------
# 9. `unusable_tools` contract
# --------------------------------------------------------------------------

def test_nothing_is_unusable_when_everything_is_configured():
    with mock.patch("services.projects.project_for_session", lambda *a, **k: A_PROJECT), \
         mock.patch("routes.email_helpers._get_email_config", lambda *a, **k: dict(AN_ACCOUNT_ROW)), \
         mock.patch("src.integrations.load_integrations", lambda: [{"id": "x", "name": "X"}]):
        assert preflight.unusable_tools({"session_id": "s", "owner": "admin"}) == {}


def test_a_rule_whose_tools_are_off_the_table_never_runs():
    """Scope limit and optimisation in one: no email tools, no email lookup."""
    def explode(*a, **k):
        raise AssertionError("the email rule ran for a turn with no email tools")

    ctx = preflight.PreflightContext(
        session_id="s", owner="admin", tools=frozenset({"read_file", "ls"})
    )
    with mock.patch("routes.email_helpers._get_email_config", explode), \
         mock.patch("src.integrations.load_integrations", explode), \
         mock.patch("services.projects.project_for_session", explode):
        assert preflight.unusable_tools(ctx) == {}


def test_the_result_is_scoped_to_the_tools_on_the_table():
    ctx = preflight.PreflightContext(
        session_id="s", owner="admin",
        tools=frozenset({"project_context", "read_file"}),
    )
    with mock.patch("services.projects.project_for_session", lambda *a, **k: None), \
         mock.patch("routes.email_helpers._get_email_config", lambda *a, **k: dict(NO_MAILBOX)), \
         mock.patch("src.integrations.load_integrations", lambda: []):
        pruned = preflight.unusable_tools(ctx)
    assert set(pruned) == {"project_context"}


def test_a_context_can_be_a_plain_mapping_or_an_object():
    with mock.patch("services.projects.project_for_session", lambda *a, **k: None), \
         mock.patch("routes.email_helpers._get_email_config", lambda *a, **k: dict(AN_ACCOUNT_ROW)), \
         mock.patch("src.integrations.load_integrations", lambda: [{"id": "x", "name": "X"}]):
        as_dict = preflight.unusable_tools({"session_id": "s", "owner": "admin"})
        as_ctx = preflight.unusable_tools(
            preflight.PreflightContext(session_id="s", owner="admin")
        )
    assert as_dict == as_ctx == {
        "project_context": preflight.PROJECT_REASON,
        "search_project_chats": preflight.PROJECT_REASON,
    }


def test_every_reason_reads_as_a_sentence_the_model_can_act_on():
    """The reason is shown to the model verbatim; it has to say what to do."""
    for reason in (preflight.PROJECT_REASON, preflight.EMAIL_REASON,
                   preflight.INTEGRATION_REASON):
        assert reason == reason.strip() and reason
        assert reason[0].islower(), reason      # reads inside a sentence
        assert not reason.endswith("."), reason


# ── Rule 5: legacy document tools with nothing to seal ────────────────────
# Found live: a saved skill pulled `suggest_document` into a turn with no
# document open, and that same round refused it with "Open the exact document
# to edit…". The loop's own [tool-coherence] alarm calls that a trap.

def _doc_ctx(**over):
    from src.tool_preflight import DOCUMENT_SEAL_TOOLS, PreflightContext
    base = dict(session_id="s1", owner="luis", tools=frozenset(DOCUMENT_SEAL_TOOLS),
                approval_gate_armed=True, sealable_document=False)
    base.update(over)
    return PreflightContext(**base)


def test_document_tools_are_pruned_when_the_gate_is_armed_and_nothing_can_be_sealed():
    from src.tool_preflight import DOCUMENT_SEAL_TOOLS, unusable_tools
    pruned = unusable_tools(_doc_ctx())
    assert set(pruned) == set(DOCUMENT_SEAL_TOOLS)
    assert all("open the exact document" in r.lower() for r in pruned.values())


def test_a_clean_run_keeps_the_document_tools():
    """No armed gate means the runtime would have run them: pruning would cost
    the user a tool for nothing."""
    from src.tool_preflight import unusable_tools
    assert unusable_tools(_doc_ctx(approval_gate_armed=False)) == {}


def test_an_open_document_keeps_them_even_with_the_gate_armed():
    from src.tool_preflight import unusable_tools
    assert unusable_tools(_doc_ctx(sealable_document=True)) == {}


def test_the_rule_says_nothing_about_tools_that_are_not_on_the_table():
    from src.tool_preflight import unusable_tools
    assert unusable_tools(_doc_ctx(tools=frozenset({"read_file"}))) == {}


def test_a_caller_that_sets_neither_flag_prunes_nothing():
    """Callers other than the loop (and older ones) must not lose tools to a
    field they never heard of."""
    from src.tool_preflight import unusable_tools
    assert unusable_tools({"session_id": "s1", "owner": "luis",
                           "tools": ["suggest_document"]}) == {}
