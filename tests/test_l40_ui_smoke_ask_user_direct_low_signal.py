"""Lot 40, point 1 — reproduces `scripts/ui_smoke.py`'s own step 3 script
without a browser, the same way `docs/spec/v2/EVAL_ESTADO.md`'s "Hallazgo
NUEVO" (lote 36) diagnosed it: `tests/eval/harness.py::EvalApp` calling
`/api/chat_stream` directly with the EXACT script and message
`scripts/ui_smoke.py::run_live`'s step 3 sends.

Before this lot: the turn reached `direct_low_signal` (0 rounds, no tools)
and the ` ```ask_user``` ` fence streamed as literal text — no `ask_user`
SSE event, so `page.wait_for_selector('[data-testid="studio-question"]')`
timed out (the literal bug EVAL_ESTADO.md's "Hallazgo NUEVO" section
describes). After it: the fence is parsed in that same fast path and the
`ask_user` event fires, which is what actually drives the Studio card the
browser step waits for (studio/src/adapters/chat.ts's `case 'ask_user'`).

This does not replace a real Chromium run (EVAL-04) — Playwright's Chromium
is not installable in this sandbox (no network egress for
`playwright install`) — but it proves the SERVER-side half of the fix
end-to-end, through the real route and the real agent loop, which is what
was actually broken.
"""
from __future__ import annotations

from tests.eval.harness import EvalApp

# Verbatim from scripts/ui_smoke.py::run_live, step 3.
_SMOKE_SCRIPT = [
    '```ask_user\n{"question": "Which greeting do you want?", '
    '"options": [{"label": "Hello"}, {"label": "Hi"}]}\n```',
    "Understood — used the greeting you picked. Done.",
]
_SMOKE_MESSAGE = "Say hi to the user, asking which greeting they want first."


def test_ui_smoke_step_3_script_now_surfaces_an_ask_user_card():
    app = EvalApp()
    app.start()
    try:
        app.script(_SMOKE_SCRIPT)
        sid = app.new_session("ui-smoke-repro")
        result = app.send_turn(sid, _SMOKE_MESSAGE)
    finally:
        app.stop()

    ask_user_events = [
        ev for ev in result.events
        if ev.get("type") == "ask_user" and isinstance(ev.get("data"), dict)
        and ev["data"].get("kind") != "tool_approval"
    ]
    assert len(ask_user_events) == 1, (
        "the exact ui_smoke.py step-3 script must produce one ask_user card "
        "event, the one studio-question renders from"
    )
    payload = ask_user_events[0]["data"]
    assert payload["question"] == "Which greeting do you want?"
    assert [o["label"] for o in payload["options"]] == ["Hello", "Hi"]

    # The direct_low_signal metrics shape ui_smoke's own diagnosis quoted
    # (EVAL_ESTADO.md: `"direct_low_signal": true, "agent_rounds": 0,
    # "tool_calls": 0`) is still 0 rounds/0 tool_calls by DESIGN (no round
    # loop ran) — what changed is that the fence no longer streams as inert
    # text: metrics now carry the executed ask_user tool_event too.
    assert result.metrics.get("direct_low_signal") is True
    assert result.metrics.get("agent_rounds") == 0
    assert result.metrics.get("tool_calls") == 1
    assert result.metrics.get("tool_events", [{}])[0].get("tool") == "ask_user"
