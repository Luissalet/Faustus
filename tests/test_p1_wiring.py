"""P1 integration wiring — applied by the integrator; these are plain regression checks.

`src/agent_loop.py` and `src/agent_harness.py::TurnLedger.check_completion`
are off limits to this lot (see `scratchpad/harness_wave/CONTRATO_P1.md`).
This test documents, as a failing (xfail-strict) expectation, the three
integration points an integrator wires on top of `src/plan_tracker.py`:

1. `_budget_last_user_attachment`'s call site (src/agent_loop.py, near line
   8240): before trimming an inlined attachment, if `agent_plan_tracker` is
   on and `find_plan_attachment(content)` finds one, `upsert_from_attachment`
   + `msg["content"] = replace_attachment(...)`, and an SSE `plan_tracker`
   event.
2. The same `_is_continue`/`_is_new_chat` block, when there is NO inlined
   attachment: `active(scope)` found -> inject `brief(...)` as a system
   message before the last user message.
3. `TurnLedger.check_completion`: a new `plan_without_action` reason when
   `self.plan_active` (set by the loop) and
   `looks_like_execute_request(user_text)` and `not self.events`.

The exact diff for each is in `scratchpad/harness_wave/P1_wiring.md`. This
test is xfail(strict=True): once an integrator applies that diff, the
assertions below start passing and the strict xfail itself starts failing
(a loud, deliberate signal to delete the marker and keep the test as a real
regression check).
"""
from __future__ import annotations

import inspect

from src import agent_harness as harness
from src import agent_loop as loop


def test_agent_loop_calls_plan_tracker_around_attachment_budgeting():
    source = inspect.getsource(loop)
    assert "plan_tracker" in source or "find_plan_attachment" in source
    assert "replace_attachment" in source
    assert "upsert_from_attachment" in source


def test_agent_loop_injects_brief_for_active_plan_without_attachment():
    source = inspect.getsource(loop)
    assert "plan_tracker.brief" in source or "pt.brief(" in source


def test_turn_ledger_has_plan_active_attribute_and_reason():
    ledger = harness.TurnLedger(workspace=None, user_text="Sigue implementando el plan")
    assert hasattr(ledger, "plan_active")
    ledger.plan_active = True
    result = ledger.check_completion("")
    assert "plan_without_action" in result["reasons"]
