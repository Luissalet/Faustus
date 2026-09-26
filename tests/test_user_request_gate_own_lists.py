"""«¿Qué tareas tengo pendientes?» lists the user's own tasks without a card.

Seen live on the 27B: the question got `board_ready` and the turn stopped at
«Allow this task to continue?» — the skills index and the tool list in the
prompt arm the external-context gate before anything runs, and a read of the
user's own board is READ_PRIVATE.
"""
import json

import pytest

from src.prompt_security import untrusted_context_message
from src.tool_capabilities import ToolRunSecurityContext
from src.user_request_gate import allows


@pytest.fixture(autouse=True)
def _own_context_arms_the_gate():
    """These tests exercise a gate armed by the owner's own prompt context
    (skills, saved memory), which is the behaviour with
    `tool_gate_own_context_trusted` off; the default no longer arms it
    (see test_own_prompt_context_does_not_arm_the_gate_by_default)."""
    from src.settings import update_settings
    update_settings({"tool_gate_own_context_trusted": False})
    yield



def _armed(text):
    context = ToolRunSecurityContext(user_request=text)
    context.observe_messages([untrusted_context_message("available skills index", "- hoard-daily-digest: …")])
    assert context.external_untrusted_context_seen is True
    return context


@pytest.mark.parametrize("tool,content,text", [
    ("board_ready", "{}", "¿Qué tareas tengo pendientes en Faustus? Solo lístalas."),
    ("board_list", "{}", "Enséñame el tablero"),
    ("board_get", '{"id": 3}', "¿Qué dice la issue 3?"),
    ("manage_tasks", '{"action": "list"}', "¿Qué tareas programadas tengo?"),
    ("manage_tasks", "{}", "list my scheduled tasks"),
])
def test_listing_own_items_passes(tool, content, text):
    assert allows(tool, content, text) is True
    assert _armed(text).decision_for(tool, content).allowed is True


@pytest.mark.parametrize("tool,content,text", [
    ("manage_tasks", '{"action": "run", "task_id": "x"}', "¿Qué tareas programadas tengo?"),
    ("manage_tasks", '{"action": "delete", "task_id": "x"}', "borra las tareas pendientes"),
    ("board_ready", "{}", "¿Qué tiempo hace mañana?"),
])
def test_anything_else_keeps_the_card(tool, content, text):
    assert allows(tool, content, text) is False