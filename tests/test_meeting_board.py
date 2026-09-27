"""A saved meeting can become sourced board work without duplicate tasks."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from src import constants, meetings, project_board
from src.agent_tools.board_tools import MeetingActionsToBoardTool
from src.agent_loop import _is_meeting_board_transfer


def test_meeting_board_intent_on_followup_without_meeting_id():
    assert _is_meeting_board_transfer("Repite la transferencia de esa reunión al tablero sin duplicar")
    assert _is_meeting_board_transfer("Pasa los compromisos del acta al board")
    assert not _is_meeting_board_transfer("Lista las tareas del tablero")


def test_meeting_actions_preview_commit_and_repeat(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(meetings, "MEETINGS_DIR", str(tmp_path / "meetings"))
    folder = Path(meetings.MEETINGS_DIR)
    folder.mkdir()
    meeting_id = "2026-09-27-atlas"
    (folder / f"{meeting_id}.json").write_text(json.dumps({
        "id": meeting_id, "title": "Atlas planning", "owner": "luis",
        "project_id": "p1", "model_ok": True,
    }), encoding="utf-8")
    (folder / f"{meeting_id}.md").write_text(
        "# Atlas planning\n\n## Summary\nWe discussed DuckDB.\n\n"
        "## Action items\n- [00:12] Ana prepara la migración antes del viernes (responsable: Ana)\n"
        "- Luis revisa el endpoint de salud\n"
        "- ¿Migramos todo a DuckDB?\n\n"
        "## Open questions\n- ¿Migramos todo a DuckDB?\n\n"
        "## Transcript\n[00:12] Ana: preparo la migración.\n", encoding="utf-8")

    tool = MeetingActionsToBoardTool()
    ctx = {"owner": "luis", "project_id": "p1"}
    monkeypatch.setattr("src.agent_tools.board_tools._key_for", lambda *_: "ATL")
    preview = asyncio.run(tool.execute(json.dumps({"meeting_id": meeting_id}), ctx))
    assert preview["exit_code"] == 0 and preview["created"] == 0
    assert len(preview["actions"]) == 2
    assert preview["actions"][0]["source_time"] == "00:12"
    assert project_board.list_issues("p1")[0] == []

    created = asyncio.run(tool.execute(json.dumps({"meeting_id": meeting_id, "commit": True}), ctx))
    assert created["created"] == 2
    issue = project_board.get(created["actions"][0]["issue_id"])
    assert issue and "Ana" in issue["title"] and "viernes" in issue["title"]
    assert issue["assignee"] == "Ana" and not issue["title"].startswith("[00:12]")
    assert issue["refs"][0]["value"] == str(folder / f"{meeting_id}.md")
    assert "Transcript time: 00:12" in issue["body_md"]

    # A crash after issue creation but before source-ref insertion still
    # leaves the marker in the issue body, so repeating does not duplicate it.
    with project_board.Store()._db(write=True) as db:
        db.execute("DELETE FROM refs WHERE issue_id=?", (issue["id"],))

    again = asyncio.run(tool.execute(json.dumps({"meeting_id": meeting_id, "commit": True}), ctx))
    assert again["created"] == 0
    assert all(a["status"] == "already_on_board" for a in again["actions"])
    assert len(project_board.list_issues("p1")[0]) == 2

    wrong_owner = asyncio.run(tool.execute(json.dumps({"meeting_id": meeting_id}),
                                           {"owner": "other", "project_id": "p1"}))
    assert wrong_owner["exit_code"] == 1
    wrong_project = asyncio.run(tool.execute(json.dumps({"meeting_id": meeting_id}),
                                             {"owner": "luis", "project_id": "p2"}))
    assert wrong_project["exit_code"] == 1
