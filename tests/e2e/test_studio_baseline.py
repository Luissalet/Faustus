"""UI-000 — Baseline journeys and screenshots of the current Faustus UI.

Captures five user journeys at three viewports (1400x900, 1024x768, 390x844)
and saves the screenshots under docs/ui/baseline/ so the new Studio UI has a
measurable starting point.

Run with the same harness as the other E2E tests:

    ODYSSEUS_E2E=1 python -m pytest tests/e2e/test_studio_baseline.py -q
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from tests.e2e.conftest import open_chat, send_agent_message

REPO = Path(__file__).resolve().parents[2]
BASELINE_DIR = REPO / "docs" / "ui" / "baseline"

VIEWPORTS = [
    {"width": 1400, "height": 900, "tag": "desktop"},
    {"width": 1024, "height": 768, "tag": "tablet"},
    {"width": 390, "height": 844, "tag": "mobile"},
]

EDIT = '```edit_file\n{"path": "calc.py", "old_string": "return a - b", "new_string": "return a + b"}\n```'
READ = '```read_file\n{"path": "calc.py"}\n```'


def _screenshot(page, journey: str, step: str, viewport_tag: str) -> Path:
    """Save a screenshot and return the path."""
    dest = BASELINE_DIR / journey
    dest.mkdir(parents=True, exist_ok=True)
    name = f"{step}_{viewport_tag}.png"
    path = dest / name
    page.screenshot(path=str(path), full_page=False)
    return path


def _capture_all_viewports(page, browser, app_server, journey: str, step: str):
    """Take a screenshot at each of the three viewports."""
    for vp in VIEWPORTS:
        page.set_viewport_size({"width": vp["width"], "height": vp["height"]})
        page.wait_for_timeout(300)
        _screenshot(page, journey, step, vp["tag"])
    # Restore to desktop for the rest of the journey
    page.set_viewport_size({"width": 1400, "height": 900})


# ── Journey 1: Create a project ──────────────────────────────────────────

def test_journey_create_project(page, app_server, browser):
    """Journey: sidebar → Projects → New project → fill name → create.

    Steps: land on app, click Projects in sidebar, click New project,
    type a name, submit the form. Captures at each step.
    """
    page.goto(app_server.base + "/", wait_until="domcontentloaded")
    page.wait_for_selector("#sidebar:visible", timeout=15000)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(500)

    # Step 1: Landing — the welcome screen
    _capture_all_viewports(page, browser, app_server, "01_create_project", "01_landing")

    # Step 2: Click Projects in sidebar
    page.locator("#sidebar-projects-btn").click()
    page.wait_for_selector("#projects-modal:not(.hidden)", timeout=10000)
    page.wait_for_timeout(400)
    _capture_all_viewports(page, browser, app_server, "01_create_project", "02_projects_modal")

    # Step 3: Click New project
    page.locator("#projects-new-btn").click()
    page.wait_for_timeout(600)
    _capture_all_viewports(page, browser, app_server, "01_create_project", "03_new_project_form")

    # Step 4: Fill project name and create
    name_input = page.locator("#project-name-input, input[name='name'], .project-name-input").first
    if name_input.count():
        name_input.fill("Baseline Test Project")
        page.wait_for_timeout(300)
        _capture_all_viewports(page, browser, app_server, "01_create_project", "04_filled_form")

        # Submit — look for the primary create/save button
        submit = page.locator("button:has-text('Create'), button:has-text('Save'), .project-create-btn").first
        if submit.count():
            submit.click()
            page.wait_for_timeout(800)
            _capture_all_viewports(page, browser, app_server, "01_create_project", "05_project_created")
    else:
        # The form structure differs — capture whatever is visible
        _capture_all_viewports(page, browser, app_server, "01_create_project", "04_form_not_found")


# ── Journey 2: Creative work ─────────────────────────────────────────────

def test_journey_creative_work(page, app_server, fake_llm, browser):
    """Journey: new chat → type a creative prompt → receive a response.

    With the fake model, image/video generation cannot actually happen.
    The journey captures the chat interface and the response flow, but
    the creative output (gallery, variants, editing) is not reachable
    through the scripted model.
    """
    fake_llm.script([
        "I'd create a moody landscape photograph with these elements:\n\n"
        "1. **Composition**: Wide frame, rule of thirds\n"
        "2. **Lighting**: Golden hour, side-lit\n"
        "3. **Palette**: Warm amber and deep teal\n\n"
        "Unfortunately I cannot generate the image directly in this mode, "
        "but I can help you refine the brief for a generation tool."
    ])
    sid = app_server.new_session("baseline creative")
    open_chat(page, app_server, sid, None)

    # Step 1: Chat interface ready
    _capture_all_viewports(page, browser, app_server, "02_creative_work", "01_chat_ready")

    # Step 2: Type and send a creative prompt
    box = page.locator("#message:visible").first
    box.click()
    box.fill("Create a moody landscape photograph for my portfolio")
    _capture_all_viewports(page, browser, app_server, "02_creative_work", "02_prompt_typed")

    page.locator("button.send-btn:visible").first.click()

    # Step 3: Wait for response
    page.wait_for_function(
        "() => document.querySelector('#chat-history').innerText.includes('rule of thirds')",
        timeout=30000,
    )
    page.wait_for_timeout(500)
    _capture_all_viewports(page, browser, app_server, "02_creative_work", "03_response_received")

    # Step 4: Check if gallery is accessible from sidebar
    gallery_btn = page.locator("#tool-gallery-btn")
    if gallery_btn.count():
        gallery_btn.click()
        page.wait_for_timeout(800)
        _capture_all_viewports(page, browser, app_server, "02_creative_work", "04_gallery_empty")


# ── Journey 3: Code task ─────────────────────────────────────────────────

def test_journey_code_task(page, app_server, fake_llm, workspace, browser):
    """Journey: open chat → set workspace → send code task → approve →
    see verified result.

    This is the best-supported journey with the fake model: the agent
    reads a file, proposes an edit, the user approves, and tests run.
    """
    fake_llm.script([READ, EDIT, "Fixed calc.py: it now adds instead of subtracting."])
    sid = app_server.new_session("baseline code")
    open_chat(page, app_server, sid, str(workspace))

    # Step 1: Chat with workspace set
    _capture_all_viewports(page, browser, app_server, "03_code_task", "01_chat_with_workspace")

    # Step 2: Send a code task in agent mode
    send_agent_message(page, "Fix the add function in calc.py")
    page.wait_for_timeout(1000)
    _capture_all_viewports(page, browser, app_server, "03_code_task", "02_agent_working")

    # Step 3: Approval card appears
    page.wait_for_selector("text=Allow this task to continue?", timeout=30000)
    _capture_all_viewports(page, browser, app_server, "03_code_task", "03_approval_card")

    # Step 4: Approve
    page.get_by_role("button", name="Allow for this task").first.click()

    # Step 5: Checkpoint and verified card
    page.wait_for_selector(".harness-node.harness-checkpoint", timeout=30000)
    page.wait_for_selector(".harness-node.harness-verified", timeout=60000)
    page.wait_for_timeout(500)
    _capture_all_viewports(page, browser, app_server, "03_code_task", "04_verified_result")


# ── Journey 4: Find a result ─────────────────────────────────────────────

def test_journey_find_result(page, app_server, fake_llm, browser):
    """Journey: search conversations → open a previous chat → find content.

    With the fake model, the search index may be empty. The journey
    documents how many clicks it takes to find something produced earlier.
    """
    # Create a session with a response so there is something to find
    fake_llm.script(["The answer to your question is 42. This is a memorable result."])
    sid = app_server.new_session("baseline find target")
    open_chat(page, app_server, sid, None)

    box = page.locator("#message:visible").first
    box.click()
    box.fill("What is the answer?")
    page.locator("button.send-btn:visible").first.click()
    page.wait_for_function(
        "() => document.querySelector('#chat-history').innerText.includes('42')",
        timeout=30000,
    )
    page.wait_for_timeout(500)

    # Step 1: The result exists in this chat
    _capture_all_viewports(page, browser, app_server, "04_find_result", "01_result_in_chat")

    # Step 2: Go to a different chat
    fake_llm.script(["OK"])
    other = app_server.new_session("baseline distraction")
    open_chat(page, app_server, other, None)
    _capture_all_viewports(page, browser, app_server, "04_find_result", "02_different_chat")

    # Step 3: Use search (Ctrl+K) to find the result
    page.keyboard.press("Control+k")
    page.wait_for_selector("#search-overlay:not(.hidden)", timeout=10000)
    page.wait_for_timeout(400)
    _capture_all_viewports(page, browser, app_server, "04_find_result", "03_search_open")

    # Type in the search overlay input
    search_input = page.locator("#search-input")
    if search_input.is_visible():
        search_input.fill("memorable result")
        page.wait_for_timeout(1500)
        _capture_all_viewports(page, browser, app_server, "04_find_result", "04_search_results")
    else:
        _capture_all_viewports(page, browser, app_server, "04_find_result", "04_search_input_not_found")

    # Close the search overlay before continuing
    page.keyboard.press("Escape")
    page.wait_for_timeout(400)

    # Step 4: Try the Library as an alternative path
    library_btn = page.locator("#tool-library-btn")
    if library_btn.count() and library_btn.is_visible():
        library_btn.click()
        page.wait_for_timeout(800)
        _capture_all_viewports(page, browser, app_server, "04_find_result", "05_library")


# ── Journey 5: Resolve an approval ───────────────────────────────────────

def test_journey_resolve_approval(page, app_server, fake_llm, workspace, browser):
    """Journey: agent triggers approval → user reviews the action → approves
    or rejects → agent continues.

    The approval flow is fully exercisable with the fake model because the
    agent harness gates file writes through the approval card.
    """
    fake_llm.script([READ, EDIT, "Done. The function now returns a + b."])
    sid = app_server.new_session("baseline approval")
    open_chat(page, app_server, sid, str(workspace))

    # Step 1: Chat ready with workspace
    _capture_all_viewports(page, browser, app_server, "05_resolve_approval", "01_chat_ready")

    # Step 2: Send agent task that will trigger approval
    send_agent_message(page, "Please fix the bug in calc.py")

    # Step 3: Wait for the approval card — this is the critical moment
    page.wait_for_selector("text=Allow this task to continue?", timeout=30000)
    page.wait_for_timeout(300)
    _capture_all_viewports(page, browser, app_server, "05_resolve_approval", "02_approval_pending")

    # Step 4: Review what the agent wants to do
    # The approval card should show the action details
    approval_card = page.locator(".ask-user-card").first
    if approval_card.count():
        card_text = approval_card.inner_text()
        # Capture the card in detail
        _capture_all_viewports(page, browser, app_server, "05_resolve_approval", "03_approval_detail")

    # Step 5: Approve the action
    page.get_by_role("button", name="Allow for this task").first.click()
    page.wait_for_selector(".harness-node.harness-verified", timeout=60000)
    page.wait_for_timeout(500)
    _capture_all_viewports(page, browser, app_server, "05_resolve_approval", "04_approved_and_done")
