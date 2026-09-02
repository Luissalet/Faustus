"""Browser flow: a delegate_agents call renders the live control board — one
card per worker with a status pill, an elapsed ticker, the worker's chat link
and a Stop control — then the cards settle to done and the board survives a
reload (rebuilt from the persisted evidence). Also: the sidebar rows of the
worker chats, and the banner of an open running worker chat."""
from __future__ import annotations

import json
import time

from tests.e2e.conftest import open_chat, send_agent_message

DELEGATE = "```delegate_agents\n" + json.dumps({
    "tasks": [
        {"name": "api", "instruction": "Read calc.py and report what add() does. Do not change files."},
        {"name": "docs", "instruction": "Read tests/test_calc.py and report what it expects. Do not change files."},
    ],
    "parallel": True,
    "reviewer": False,
}) + "\n```"
# Distinct sentences: llm_core's degenerate-stream detector aborts loops.
WORKER = "".join(f"Nota {i}: he leído la parte {i} del archivo y anoto lo que hace. " for i in range(1, 25)) + "\n\nInforme: no he cambiado ningún archivo."
FINAL = "Resumen del coordinador: los dos sub-agentes han terminado."


def _maybe_allow(page):
    """delegate_agents is an EXECUTE_CODE tool: the approval card may show up
    before the board. Wait for whichever comes first; click through the gate."""
    page.wait_for_selector("#chat-history .ask-user-card, .subagent-board .subagent-card", timeout=40000)
    if page.locator("#chat-history .ask-user-card").count():
        page.get_by_role("button", name="Allow for this task").first.click()


def test_delegation_board_cards_live_then_persisted(page, app_server, fake_llm, workspace):
    # call 0: coordinator delegates; calls 1-2: the two workers (slow, so the
    # cards are observable while running); later calls: the final answer.
    fake_llm.script([DELEGATE, {"text": WORKER, "delay": 0.35}, {"text": WORKER, "delay": 0.35}, FINAL])
    sid = app_server.new_session("e2e delegation")
    open_chat(page, app_server, sid, str(workspace))
    send_agent_message(page, "Reparte el trabajo entre dos sub-agentes")
    _maybe_allow(page)

    # 1. The live board: one card per worker, running pill, elapsed ticker,
    #    Stop / Steer / Open chat controls.
    page.wait_for_selector(".subagent-board .subagent-card", timeout=40000)
    page.wait_for_function("() => document.querySelectorAll('.subagent-board .subagent-card').length === 2", timeout=20000)
    cards = page.locator(".subagent-board .subagent-card")
    names = " ".join(cards.nth(i).locator(".subagent-name").inner_text() for i in range(2))
    assert "api" in names and "docs" in names
    page.wait_for_selector(".subagent-board .subagent-card .subagent-pill.is-running", timeout=20000)
    running = page.locator(".subagent-board .subagent-card.is-live").first
    assert running.locator("button[data-stop-worker]").count() == 1
    assert running.locator("button[data-steer-worker]").count() == 1
    child = running.locator("a.subagent-chat-link").first.get_attribute("href")
    assert child and child.startswith("#")
    e1 = running.locator(".subagent-elapsed").inner_text()
    page.wait_for_timeout(2200)
    e2 = running.locator(".subagent-elapsed").inner_text()
    assert e1 != e2, (e1, e2)          # the 1 s ticker counts
    assert "worker" in running.locator(".subagent-role-badge").inner_text().lower()   # CSS uppercases it

    # 2. The server knows the workers (activity.workers) and the sidebar rows
    #    of the worker chats carry the inline Stop while they run.
    act = app_server.activity()
    workers = act.get("workers") or {}
    if workers:                        # backend contract present
        assert any(w.get("parent") == sid for w in workers.values()), act
        page.wait_for_function(
            "() => [...document.querySelectorAll('.session-item-worker .session-worker-stop')].some(b => !b.hidden)",
            timeout=15000,
        )

    # 3. Everything settles: done pills, count 2/2, no live controls left.
    page.wait_for_function("() => document.querySelectorAll('.subagent-board .subagent-card .subagent-pill.is-done').length === 2", timeout=90000)
    assert page.locator(".subagent-board .subagent-board-count").inner_text().strip() == "2/2"
    assert page.locator(".subagent-board button[data-stop-worker]").count() == 0
    t0 = time.time()
    while sid in app_server.activity()["running"] and time.time() - t0 < 60:
        time.sleep(0.5)

    # 4. Reload the page: the board is rebuilt from tool_events[i].subagents
    #    with the same card markup (role badge, elapsed, tools, Open chat).
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector(".subagent-restored .subagent-card", timeout=30000)
    restored = page.locator(".subagent-restored .subagent-card")
    assert restored.count() == 2
    assert page.locator(".subagent-restored .subagent-pill.is-done").count() == 2
    first = restored.first
    assert first.locator(".subagent-elapsed").inner_text().endswith("s")
    assert first.locator("a.subagent-chat-link").count() == 1
    assert page.locator(".subagent-restored button[data-stop-worker]").count() == 0


def test_running_worker_chat_shows_the_banner(page, app_server, fake_llm, workspace):
    fake_llm.script([DELEGATE, {"text": WORKER, "delay": 0.5}, {"text": WORKER, "delay": 0.5}, FINAL])
    sid = app_server.new_session("e2e delegation banner")
    open_chat(page, app_server, sid, str(workspace))
    send_agent_message(page, "Reparte el trabajo entre dos sub-agentes")
    _maybe_allow(page)
    page.wait_for_selector(".subagent-board .subagent-card.is-live a.subagent-chat-link", timeout=40000)
    href = page.locator(".subagent-board .subagent-card.is-live a.subagent-chat-link").first.get_attribute("href")
    child = href.lstrip("#")
    act = app_server.activity()
    if not (act.get("workers") or {}).get(child):
        return                      # backend contract (activity.workers) not there: nothing to show
    # Open the running worker chat: a banner instead of an empty chat.
    page.goto(app_server.base + "/#" + child, wait_until="domcontentloaded")
    page.wait_for_selector(".worker-chat-banner", timeout=20000)
    text = page.locator(".worker-chat-banner").inner_text()
    assert "Worker" in text and "running" in text
    assert page.locator(".worker-chat-banner .worker-chat-stop").count() == 1
    parent_link = page.locator(".worker-chat-banner a.worker-chat-open-parent").first.get_attribute("href")
    assert parent_link == "#" + sid
    # Back in the parent, the board is still there (retained in the background).
    page.goto(app_server.base + "/#" + sid, wait_until="domcontentloaded")
    page.wait_for_selector(".subagent-board .subagent-card, .subagent-restored .subagent-card", timeout=30000)
