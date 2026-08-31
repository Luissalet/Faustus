"""Browser flows for the workspace slash commands added by the fork:
/agentsmd write (drafts AGENTS.md from what the runtime detects) and
/checkpoints (lists the shadow checkpoints, shows what differs, restores)."""
from __future__ import annotations

from tests.e2e.conftest import open_chat, send_agent_message
from tests.e2e.test_agent_flows import EDIT, READ


def test_agentsmd_write_creates_the_instructions_file(page, app_server, fake_llm, workspace):
    fake_llm.script(["unused"])
    sid = app_server.new_session("e2e agentsmd")
    open_chat(page, app_server, sid, str(workspace))
    send_agent_message(page, "/agentsmd write")
    page.wait_for_function("() => document.querySelector('#chat-history').innerText.includes('AGENTS.md written')", timeout=20000)
    text = (workspace / "AGENTS.md").read_text(encoding="utf-8")
    assert text.startswith("# ws") and "pytest" in text and "## Do not touch" in text
    # A second run never overwrites: the reply says a file already exists.
    send_agent_message(page, "/agentsmd write")
    page.wait_for_function("() => document.querySelector('#chat-history').innerText.includes('already has an instructions file')", timeout=20000)
    assert (workspace / "AGENTS.md").read_text(encoding="utf-8") == text


def test_checkpoints_list_diff_and_restore(page, app_server, fake_llm, workspace):
    # No turn yet → no checkpoint.
    sid = app_server.new_session("e2e checkpoints")
    open_chat(page, app_server, sid, str(workspace))
    send_agent_message(page, "/checkpoints")
    page.wait_for_function("() => document.querySelector('#chat-history').innerText.includes('No checkpoints yet')", timeout=20000)

    # One agent turn that edits calc.py (approval gate → verified) creates one.
    fake_llm.script([READ, EDIT, "He corregido calc.py: ahora suma."])
    send_agent_message(page, "Arregla la función add en calc.py")
    page.wait_for_selector("text=Allow this task to continue?", timeout=30000)
    page.get_by_role("button", name="Allow for this task").first.click()
    page.wait_for_selector(".harness-node.harness-verified", timeout=60000)
    assert (workspace / "calc.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"

    send_agent_message(page, "/checkpoints")
    page.wait_for_selector(".checkpoint-list button[data-cp-diff]", timeout=20000)
    page.locator(".checkpoint-list button[data-cp-diff]").first.click()
    page.wait_for_function("() => document.querySelector('.checkpoint-list .cp-note') && document.querySelector('.checkpoint-list .cp-note').textContent.includes('M calc.py')", timeout=20000)

    page.locator(".checkpoint-list button[data-cp-restore]").first.click()
    page.wait_for_selector("#styled-confirm-ok", state="visible")
    page.locator("#styled-confirm-ok").click()
    page.wait_for_function("() => [...document.querySelectorAll('.checkpoint-list button[data-cp-restore]')].some(b => b.textContent.includes('Restored'))", timeout=20000)
    assert (workspace / "calc.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a - b\n"
