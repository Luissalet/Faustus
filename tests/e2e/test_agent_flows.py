"""Browser flows: approval gate → checkpoint / tests / verified card → file
viewer with the checkpoint diff → restore; and a run that keeps going in the
background while the user switches chats, then re-attaches."""
from __future__ import annotations

import time

from tests.e2e.conftest import open_chat, send_agent_message

EDIT = '```edit_file\n{"path": "calc.py", "old_string": "return a - b", "new_string": "return a + b"}\n```'
READ = '```read_file\n{"path": "calc.py"}\n```'


def test_approval_then_verified_card_viewer_and_restore(page, app_server, fake_llm, workspace):
    fake_llm.script([READ, EDIT, "He corregido calc.py: ahora suma."])
    sid = app_server.new_session("e2e approval")
    open_chat(page, app_server, sid, str(workspace))
    send_agent_message(page, "Arregla la función add en calc.py")

    # 1. The first write after reading workspace content is gated: the exact
    #    approval card shows up and "Allow for this task" resumes the turn.
    page.wait_for_selector("text=Allow this task to continue?", timeout=30000)
    page.get_by_role("button", name="Allow for this task").first.click()

    # 2. Checkpoint before the first change, project tests, verified card.
    page.wait_for_selector(".harness-node.harness-checkpoint", timeout=30000)
    page.wait_for_selector(".harness-node.harness-verified", timeout=60000)
    verified = page.locator(".harness-node.harness-verified").first
    assert "tests passed" in verified.inner_text()
    assert (workspace / "calc.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"

    # 3. Turn summary → file chip → viewer shows the diff vs. the checkpoint.
    summary = page.locator(".harness-node", has_text="Turn summary").last
    summary.wait_for(timeout=30000)
    chip = summary.locator("a.harness-file[data-open-checkpoint]").first
    assert chip.get_attribute("data-open-checkpoint")
    chip.click()
    page.wait_for_selector("#file-viewer-panel:not([hidden])")
    page.wait_for_function("() => document.querySelector('#file-viewer-panel .fv-code').innerText.includes('return a + b')")
    code = page.locator("#file-viewer-panel .fv-code").inner_text()
    assert "-    return a - b" in code and "+    return a + b" in code
    assert "before this turn" in page.locator("#file-viewer-panel .fv-meta").inner_text()
    page.locator('#file-viewer-panel [data-fv="close"]').click()

    # 4. Restore to before this turn (checkpoint, no git needed).
    summary.locator("button[data-restore-turn]").first.click()
    page.wait_for_selector("#styled-confirm-ok", state="visible")
    page.locator("#styled-confirm-ok").click()
    page.wait_for_function("() => [...document.querySelectorAll('button[data-restore-turn]')].some(b => b.textContent.includes('Restored'))")
    assert (workspace / "calc.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a - b\n"

    # The turn was recorded for the scorecard and the audit trail.
    calls = fake_llm.calls()
    assert calls["count"] >= 3


def test_background_run_survives_switching_chats(page, app_server, fake_llm, workspace):
    """The answer keeps streaming server-side while the user opens another
    chat; the sidebar shows the working dot and, back in the chat, the
    finished answer is there (re-attached or reloaded)."""
    # Not a repeated phrase: llm_core's degenerate-stream detector would
    # (rightly) abort a model that loops. Distinct sentences keep it honest.
    long_text = "".join(f"Paso {i}: reviso la línea {i * 3} de calc.py y anoto lo que hace. " for i in range(1, 41)) + "\n\nConclusión: la función add resta en vez de sumar."
    fake_llm.script([long_text], delay=0.3)   # ~25 chunks → ~8 s of streaming
    sid = app_server.new_session("e2e background")
    open_chat(page, app_server, sid, str(workspace))
    send_agent_message(page, "¿Qué hace calc.py?")
    page.wait_for_function("() => document.querySelector('#chat-history').innerText.includes('Paso 1:')", timeout=30000)

    # Leave for another chat while the run is in flight (hash navigation is
    # what the sidebar rows do).
    other = app_server.new_session("e2e other")
    page.goto(app_server.base + "/#" + other, wait_until="domcontentloaded")
    page.wait_for_timeout(800)
    act = app_server.activity()
    assert sid in act["running"], act
    assert "la función add resta" not in page.locator("#chat-history").inner_text()
    # The sidebar dot: working (blinking) while it runs, "finished — unread"
    # once it is done. The list polls /api/chat/activity every few seconds.
    page.wait_for_function(
        "sid => { const s = document.querySelector(`.session-star[data-session-id='${sid}']`); return !!s && (s.classList.contains('processing') || s.classList.contains('notify')); }",
        arg=sid, timeout=15000,
    )

    # Wait for the server to finish, then come back: the full answer is there.
    t0 = time.time()
    while sid in app_server.activity()["running"] and time.time() - t0 < 60:
        time.sleep(0.5)
    page.goto(app_server.base + "/#" + sid, wait_until="domcontentloaded")
    page.wait_for_function("() => document.querySelector('#chat-history').innerText.includes('la función add resta en vez de sumar')", timeout=30000)


def test_second_chat_waits_in_the_gpu_queue(page, app_server, fake_llm, workspace):
    """Two chats on the same local endpoint: the second one is queued behind
    the first (one generation at a time) and shows its position, then runs
    on its own once the lane frees."""
    slow = "".join(f"Bloque {i}: sigo trabajando en la respuesta, todavía no he terminado el análisis. " for i in range(1, 31)) + "\n\nListo A."
    fake_llm.script([slow, "Respuesta B: terminado."], delay=0.25)
    a = app_server.new_session("e2e queue A")
    b = app_server.new_session("e2e queue B")
    open_chat(page, app_server, a, str(workspace))
    send_agent_message(page, "Tarea larga A")
    page.wait_for_function("() => document.querySelector('#chat-history').innerText.includes('Bloque 1:')", timeout=30000)
    page.goto(app_server.base + "/#" + b, wait_until="domcontentloaded")
    page.wait_for_selector("#message:visible")
    page.wait_for_timeout(500)
    send_agent_message(page, "Tarea corta B")
    page.wait_for_selector(".harness-node.harness-queued", timeout=20000)
    assert "position 1" in page.locator(".harness-node.harness-queued").first.inner_text()
    act = app_server.activity()
    assert act["queued"].get(b) == 1, act
    # A finishes → B starts and completes without any further click.
    page.wait_for_function("() => document.querySelector('#chat-history').innerText.includes('Respuesta B: terminado')", timeout=60000)
    assert "Started" in page.locator(".harness-node", has_text="Started (the queue reached this chat)").first.inner_text()
