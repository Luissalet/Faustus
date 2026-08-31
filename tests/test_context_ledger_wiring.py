"""The context ledger is actually plugged in (FAUSTUS).

Same failure mode as the service-health endpoint: a perfectly good backend
signal that nothing forwards and nothing renders. Three links have to hold —
the loop emits, the chat route forwards the event type, the UI has a case for
it — and each is a one-line edit in a different file, so each gets a test.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOOP = (ROOT / "src" / "agent_loop.py").read_text(encoding="utf-8")
ROUTES = (ROOT / "routes" / "chat_routes.py").read_text(encoding="utf-8")
UI = (ROOT / "static" / "js" / "agentHarnessUI.js").read_text(encoding="utf-8")


def test_loop_builds_and_emits_the_ledger():
    assert "from src.context_ledger import build_ledger" in LOOP
    assert '"type": "context_ledger"' in LOOP


def test_loop_throttles_instead_of_emitting_every_round():
    assert "should_emit(_context_ledger_sent, _ledger_now)" in LOOP


def test_loop_slims_tool_schemas_before_measuring_them():
    """Order matters: measuring before slimming would report a fiction."""
    slim = LOOP.index("from src.tool_slimming import slim_tool_schemas")
    ledger = LOOP.index("from src.context_ledger import build_ledger")
    assert slim < ledger


def test_slimming_can_be_turned_off_from_settings():
    assert 'get_setting("agent_tool_schema_slim", True)' in LOOP


def test_chat_route_forwards_the_event_type():
    assert '"context_ledger",' in ROUTES


def test_ui_renders_it():
    assert "case 'context_ledger': renderContextLedger(json); return true;" in UI
    assert "export function renderContextLedger" in UI


def test_ledger_failure_can_never_break_a_round():
    """A measurement must not be able to kill a turn."""
    block = LOOP[LOOP.index("from src.context_ledger import build_ledger"):]
    assert "except Exception as _cl_err" in block[:1400]
