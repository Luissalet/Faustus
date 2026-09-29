"""Round-trip a real Faustus export into the passive history archive."""
import json
from datetime import datetime, timezone
from pathlib import Path

from src import chat_export, history_import as history
from src.chat_export_model import ExportMessage, ToolCall, Transcript


def test_export_preview_import_and_search_preserve_text_but_not_actions(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "DATA_DIR", str(tmp_path / "archive"))
    transcript = Transcript(
        name="Portability pilot", model="fixture", session_id="portability-pilot",
        exported_at=datetime(2026, 9, 29, tzinfo=timezone.utc),
        messages=[
            ExportMessage(role="user", raw_text="Remember this quoted phrase: violet lantern.",
                          timestamp="2026-09-29T10:00:00Z", attachments=["portrait.png"]),
            ExportMessage(role="assistant", raw_text="The violet lantern is in the archive.",
                          timestamp="2026-09-29T10:00:01Z",
                          tool_calls=[ToolCall(name="bash", arguments="DO_NOT_EXECUTE",
                                               result="archived output", status="success")]),
        ],
    )
    path = tmp_path / "session.json"
    path.write_text(json.dumps(chat_export.transcript_to_dict(transcript)), encoding="utf-8")
    preview = history.import_path(str(path), dry_run=True)
    assert preview["messages"] == 2
    assert not Path(history.DATA_DIR).exists()

    result = history.import_path(str(path))
    assert result["created"] == 1
    archived = history.get_conversation(history.list_conversations()[0]["id"])
    messages = archived["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert [m["content"] for m in messages] == [m.raw_text for m in transcript.messages]
    assert all("tool_calls" not in m and "attachments" not in m for m in messages)
    assert history.search("violet lantern")["hits"]
    assert history.import_path(str(path))["updated"] == 1
    # The imperative stays text in history.db; no memory or live session created.
    assert not (Path(history.DATA_DIR) / "memory.json").exists()
    assert not (Path(history.DATA_DIR) / "memory_engine.db").exists()
