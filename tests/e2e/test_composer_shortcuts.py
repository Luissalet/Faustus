"""Browser flows for the composer shortcuts: "@" file mentions, "#" remember,
and /versions restoring what an edit deleted."""
from __future__ import annotations

from tests.e2e.conftest import open_chat, send_agent_message


def _type_into_composer(page, text: str) -> None:
    box = page.locator("#message:visible").first
    box.click()
    box.fill("")
    box.type(text, delay=20)


def test_at_opens_the_file_picker_and_inserts_the_path(page, app_server, fake_llm, workspace):
    sid = app_server.new_session("e2e mentions")
    open_chat(page, app_server, sid, str(workspace))

    _type_into_composer(page, "arregla @calc")
    page.wait_for_selector("#file-mention-autocomplete .slash-ac-row", timeout=20000)
    rows = page.locator("#file-mention-autocomplete .slash-ac-row")
    assert rows.first.inner_text().startswith("calc.py")

    page.keyboard.press("Enter")
    page.wait_for_function(
        "() => document.querySelector('#message').value === 'arregla @calc.py '", timeout=10000)
    # Enter picked the file — it must not have sent the message.
    assert "@calc.py" not in page.locator("#chat-history").inner_text()


def test_at_finds_a_nested_file_and_escape_closes_the_picker(page, app_server, fake_llm, workspace):
    sid = app_server.new_session("e2e mentions 2")
    open_chat(page, app_server, sid, str(workspace))

    _type_into_composer(page, "mira @test_calc")
    page.wait_for_selector("#file-mention-autocomplete .slash-ac-row", timeout=20000)
    assert "tests" in page.locator("#file-mention-autocomplete").inner_text()

    page.keyboard.press("Escape")
    page.wait_for_function(
        "() => document.querySelector('#file-mention-autocomplete').style.display === 'none'",
        timeout=10000)


def test_hash_remembers_a_rule_in_agents_md(page, app_server, fake_llm, workspace):
    sid = app_server.new_session("e2e remember")
    open_chat(page, app_server, sid, str(workspace))

    send_agent_message(page, "# los tests se lanzan con pytest -q")
    page.wait_for_function(
        "() => document.querySelector('#chat-history').innerText.includes('Remembered in')",
        timeout=20000)
    body = (workspace / "AGENTS.md").read_text(encoding="utf-8")
    assert "## Notes added from chat" in body
    assert "- los tests se lanzan con pytest -q" in body

    # The same rule again is reported, not duplicated.
    send_agent_message(page, "# los tests se lanzan con pytest -q")
    page.wait_for_function(
        "() => document.querySelector('#chat-history').innerText.includes('Already in')",
        timeout=20000)
    assert (workspace / "AGENTS.md").read_text(encoding="utf-8").count(
        "- los tests se lanzan con pytest -q") == 1


def test_versions_restores_what_an_edit_deleted(page, app_server, fake_llm, workspace):
    fake_llm.script(["La primera respuesta.", "La segunda respuesta."])
    sid = app_server.new_session("e2e versions")
    open_chat(page, app_server, sid, str(workspace))

    send_agent_message(page, "dime algo")
    page.wait_for_function(
        "() => document.querySelector('#chat-history').innerText.includes('La primera respuesta')",
        timeout=60000)

    # Edit the user message: the first answer is truncated away…
    page.locator(".msg.msg-user").last.hover()
    page.locator(".msg.msg-user").last.locator("button[title='Edit message']").first.click()
    editor = page.locator(".msg.msg-user .edit-textarea").last
    editor.fill("dime otra cosa")
    page.locator(".msg.msg-user .edit-save-btn").last.click()
    page.wait_for_function(
        "() => document.querySelector('#chat-history').innerText.includes('La segunda respuesta')",
        timeout=60000)
    assert "La primera respuesta" not in page.locator("#chat-history").inner_text()

    # …but /versions has it, and Restore puts it back.
    send_agent_message(page, "/versions")
    page.wait_for_selector(".chat-version-list button[data-cv-restore]", timeout=20000)
    assert "La primera respuesta" in page.locator(".chat-version-list").inner_text()
    page.locator(".chat-version-list button[data-cv-restore]").first.click()
    page.wait_for_selector("#styled-confirm-ok", state="visible")
    page.locator("#styled-confirm-ok").click()
    page.wait_for_function(
        "() => document.querySelector('#chat-history').innerText.includes('La primera respuesta')",
        timeout=30000)


def test_a_sent_mention_becomes_a_chip_that_opens_the_file(page, app_server, fake_llm, workspace):
    fake_llm.script(["Vale."])
    sid = app_server.new_session("e2e chips")
    open_chat(page, app_server, sid, str(workspace))

    send_agent_message(page, "mira @calc.py y dime que hace")
    page.wait_for_selector(".msg-user .mention-chip", timeout=30000)
    chip = page.locator(".msg-user .mention-chip").first
    assert chip.inner_text() == "@calc.py"
    # The rest of the sentence survives the decoration.
    assert "mira @calc.py y dime que hace" in page.locator(".msg-user").last.inner_text()

    chip.click()
    page.wait_for_selector("#file-viewer-panel", state="visible", timeout=20000)
    page.wait_for_function(
        "() => document.querySelector('#file-viewer-panel').innerText.includes('return a - b')",
        timeout=20000)
