"""EVAL-04 (lote 37): re-verifies the lote-36 fix `scripts/ui_smoke.py`'s own
docstring claims — "Step 3 below now sends `client_message_id` unmodified" —
is still true, and that the script's step 3 drives the message through the
REAL Studio composer (a real browser click) rather than a hand-built POST
that could quietly drop the field again.

This lote's own re-run of `python3 scripts/ui_smoke.py` (see
`docs/spec/v2/EVAL_ESTADO.md`, "Re-verificado en el lote 37") produced the
SAME result as lote 36: login/new_conversation/Settings green,
`send_message_and_question_card` red for the unrelated agent_loop routing
finding already documented there — not a regression of this fix.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "ui_smoke.py"


def test_no_code_strips_client_message_id_from_the_request():
    source = SCRIPT.read_text(encoding="utf-8")
    for bad in ("pop(\"client_message_id\"", "pop('client_message_id'",
                "del client_message_id", "client_message_id\"] = None",
                "client_message_id'] = None"):
        assert bad not in source, f"found a pattern that would strip client_message_id again: {bad!r}"


def test_step_3_sends_the_message_through_the_real_composer_click():
    """The bug this pins against: a hand-built `page.request.post(.../api/chat_stream, ...)`
    that a developer could construct WITHOUT `client_message_id`, silently
    reintroducing the exact gap the browser always closes on its own."""
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'page.click(\'[data-testid="studio-send"]\')' in source
    assert 'page.request.post(app.base + "/api/chat_stream"' not in source, (
        "step 3 must never construct a direct POST to /api/chat_stream — "
        "it has to go through the real composer so the browser fills in "
        "client_message_id itself, the same way the module docstring documents"
    )


def test_eval_estado_records_a_lote_37_re_verification():
    doc = (REPO / "docs" / "spec" / "v2" / "EVAL_ESTADO.md").read_text(encoding="utf-8")
    assert "lote 37" in doc.lower()
    assert "Re-verificado en el lote 37" in doc
