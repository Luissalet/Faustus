"""Regression test for #3993 — live chat leaves executed tool fences visible.

The backend strips every fenced tool block (``src/tool_parsing.py`` builds its
regex from the full ``TOOL_TAGS`` set), so a reloaded session renders cleanly.
The live path has its own regex, built in ``studio/src/lib/fences.ts``.

Originally that regex came from a hand-maintained subset, so any executable tool
not in it — and every *future* tool added to ``TOOL_TAGS`` — left its executed
fence lingering as a raw code block in the live bubble until reload. The fix
makes ``TOOL_TAGS`` the single source: the interface hard-codes no tool list
at all. It fetches the backend's authoritative set once from
``GET /api/tools`` (which serves ``sorted(TOOL_TAGS)``) and builds
``EXEC_FENCE_RE`` from it at load, minus ``bash``/``python`` (legitimate code
examples a user may have asked the model to show). There is no second list to
drift.

The behavioural tests exercise an equivalent Python regex built straight from
the backend ``TOOL_TAGS`` — the same source the live regex derives from — and a
source-level guard asserts the interface keeps no hard-coded list.
"""
import json
import re
from pathlib import Path

_SRC = Path("studio/src/lib/fences.ts")
_ROUTES_SRC = Path("routes/model_routes.py")

# Deliberately NOT stripped: legitimate code-example languages, not tool
# invocations. Must match the carve-out in studio/src/lib/fences.ts.
_NON_STRIPPED = {"bash", "python"}


def _tool_tags() -> set[str]:
    """The backend TOOL_TAGS set — the same authoritative set GET /api/tools
    serves (sorted) and the live EXEC_FENCE_RE derives from. Imported rather
    than source-scraped so it reflects the real set however it is composed: the
    literal plus the ``| BUILTIN_EMAIL_TOOLS`` union (email tool names live in
    that single source, not inline in the literal)."""
    from src.agent_tools import TOOL_TAGS
    return set(TOOL_TAGS)


def _exec_fence_regex() -> re.Pattern:
    """Rebuild EXEC_FENCE_RE's behavior from the same source the live regex now
    derives from: the backend TOOL_TAGS (served via /api/tools) minus bash/python."""
    tags = _tool_tags() - _NON_STRIPPED
    assert tags, "TOOL_TAGS is empty"
    return re.compile(
        r"```(" + "|".join(re.escape(tag) for tag in sorted(tags)) + r")(?![\w-])"
        r"[ \t]*([{\[][^\n]*?)?[ \t]*(?=\r?\n|```)\r?\n?([\s\S]*?)```",
        re.IGNORECASE,
    )


def _strip_live_exec_fences(text: str) -> str:
    rx = _exec_fence_regex()

    def repl(match: re.Match) -> str:
        inline = (match.group(2) or "").strip()
        if not inline:
            return ""
        body = (match.group(3) or "").strip()
        content = f"{inline}\n{body}" if body else inline
        try:
            json.loads(content)
        except (TypeError, ValueError):
            return match.group(0)
        return ""

    return rx.sub(repl, text)


def test_strips_executed_email_tool_fences():
    # The exact shape the reporter observed lingering in the live bubble.
    text = 'Here are emails\n\n```list_emails\n{"max_results":10}\n```'
    assert _strip_live_exec_fences(text).strip() == "Here are emails"


def test_strips_executed_inline_email_tool_fences():
    text = 'Here are accounts\n\n```list_email_accounts {}\n```'
    assert _strip_live_exec_fences(text).strip() == "Here are accounts"


def test_strips_multiline_inline_json_email_fences():
    text = 'Here are emails\n\n```list_emails {"folder": "INBOX",\n"max_results": 2}\n```'
    assert _strip_live_exec_fences(text).strip() == "Here are emails"


def test_strips_every_named_email_tool_fence():
    email_tools = [
        "list_email_accounts", "send_email", "list_emails", "read_email",
        "reply_to_email", "bulk_email", "archive_email", "delete_email",
        "mark_email_read", "scan_email_unsubscribes", "unsubscribe_email",
    ]
    for tool in email_tools:
        fence = f"```{tool}\n{{}}\n```"
        assert _strip_live_exec_fences(fence).strip() == "", f"{tool} fence not stripped"


def test_preserves_existing_web_search_stripping():
    fence = '```web_search\n{"q":"x"}\n```'
    assert _strip_live_exec_fences(fence).strip() == ""


def test_does_not_strip_bash_or_python_code_examples():
    """bash/python fences are deliberately excluded — they are legitimate code
    examples a user may have asked the model to show, not tool invocations."""
    for lang in sorted(_NON_STRIPPED):
        example = f"```{lang}\nls -la\n```"
        assert _strip_live_exec_fences(example) == example, f"{lang} example wrongly stripped"


def test_does_not_strip_invalid_inline_json_metadata():
    for example in (
        '```list_email_accounts {title="setup"}\n```',
        '```web_search {query="odysseus"}\n```',
    ):
        assert _strip_live_exec_fences(example) == example


def test_the_interface_keeps_no_hardcoded_tool_list():
    """Root-cause guard for #3993: the live stripper must NOT carry a
    hand-maintained mirror of TOOL_TAGS. A copy drifts the day a tool is
    added, and the symptom — one tool's fences linger while the rest are
    fine — is almost impossible to trace back here. The tags come from
    `GET /api/tools` at runtime; the only literals allowed are the
    `bash`/`python` carve-out, which are languages, not tools.
    """
    source = _SRC.read_text(encoding="utf-8")
    assert "/api/tools" in source, "the tag list must come from the endpoint"
    assert "JSON.parse" in source, (
        "content has to parse as JSON before a fence is removed, or a markdown "
        "block labelled with a tool's name disappears"
    )
    m = re.search(r"NOT_A_TOOL\s*=\s*new Set\(\[(?P<body>.*?)\]\)", source, re.DOTALL)
    assert m, "the bash/python carve-out (NOT_A_TOOL) is not there"
    carve_out = set(re.findall(r"['\"]([a-z_]+)['\"]", m.group("body")))
    assert carve_out == _NON_STRIPPED, (
        f"NOT_A_TOOL must carve out exactly {sorted(_NON_STRIPPED)}, got {sorted(carve_out)}"
    )
    # Any other bare tool name in the CODE would be the mirror coming back.
    # Comments are stripped first: they explain the feature and naturally name
    # a tool or two as an example.
    code = re.sub(r"/\*.*?\*/|//[^\n]*", "", source, flags=re.DOTALL)
    for tag in ("read_file", "web_search", "list_email_accounts", "edit_file"):
        assert tag not in code, f"{tag} is hard-coded; the list must come from the server"


def test_api_tools_endpoint_serves_full_tool_tags():
    """The frontend's single source is GET /api/tools. Guard that the endpoint
    serves the complete TOOL_TAGS set (sorted) — if it ever served a subset, the
    live-strip list would silently shrink with no second list to catch it."""
    source = _ROUTES_SRC.read_text(encoding="utf-8")
    assert re.search(r"for\s+tag\s+in\s+sorted\(\s*TOOL_TAGS\s*\)", source), (
        "GET /api/tools must iterate sorted(TOOL_TAGS) so the frontend's "
        "EXEC_FENCE_RE covers every executable tool (#3993)."
    )
