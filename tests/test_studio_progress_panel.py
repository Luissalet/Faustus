"""The agent's todowrite list belongs in the Studio side panel.

The backend already streams `progress_update` and persists the list at
GET /api/agent/progress/{session}; the chat UI must restore it into the
Progress tab and keep replacing it as statuses change — not freeze the
first snapshot at the top of the assistant message.
"""
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


def test_progress_is_a_side_panel_tab():
    panel = _read("studio/src/screens/studio/panel.ts")
    side = _read("studio/src/screens/studio/SidePanel.tsx")
    assert "'progress'" in panel, "PanelTab must include 'progress'"
    assert "revealProgress" in panel
    assert 'id:\'progress\'' in side or 'id: "progress"' in side or "id:'progress'" in side
    assert "ProgressList" in side
    assert "ProgressTab" in side
    assert "from './ProgressList'" in side or 'from "./ProgressList"' in side


def test_transcript_does_not_paint_the_todo_list_on_the_message():
    src = _read("studio/src/screens/studio/Transcript.tsx")
    assert "todos={turn.todos}" not in src


def test_studio_restores_progress_from_the_session_endpoint():
    src = _read("studio/src/screens/Studio.tsx")
    assert "loadAgentProgress" in src
    assert "type: 'progress'" in src or 'type: "progress"' in src


def test_chat_adapter_exposes_progress_restore():
    src = _read("studio/src/adapters/chat.ts")
    assert "export async function loadAgentProgress" in src
    assert "/api/agent/progress/" in src
    assert "export function todosFrom" in src
