"""Conversation export as normalized JSONL events (radar #333)."""
import json

from src.chat_export import build_transcript, render, transcript_events
from src.chat_export_model import SUPPORTED_FORMATS


class Msg:
    def __init__(self, role, content, metadata=None):
        self.role, self.content, self.metadata = role, content, metadata


class Sess:
    def __init__(self, history):
        self.id, self.name, self.model, self.history = "s-9", "Chat", "m-1", history


def _session():
    return Sess([
        Msg("user", "Arregla el test", {"timestamp": "2026-09-26T20:00:00Z"}),
        Msg("assistant", "Hecho.", {
            "timestamp": "2026-09-26T20:00:09Z", "model": "m-1",
            "tool_events": [
                {"round": 1, "tool": "read_file", "command": "tests/test_x.py", "output": "x" * 3000, "exit_code": 0},
                {"round": 2, "tool": "edit_file", "command": "src/x.py", "output": "ok", "exit_code": 0},
            ],
        }),
    ])


def test_the_format_is_supported_and_one_event_per_line():
    assert "jsonl" in SUPPORTED_FORMATS
    out = render(build_transcript(_session()), "ndjson")
    assert out.media_type == "application/x-ndjson"
    assert out.filename.endswith(".jsonl")
    lines = out.content.decode("utf-8").strip().split("\n")
    events = [json.loads(line) for line in lines]
    assert events[0]["type"] == "session" and events[0]["session_id"] == "s-9"
    assert all(e["v"] == 1 for e in events)


def test_events_cover_prompt_response_tools_and_file_changes():
    events = transcript_events(build_transcript(_session()))
    types = [e["type"] for e in events]
    assert types == ["session", "prompt", "response", "tool_call", "tool_call", "file_change"]
    read = events[3]
    assert read["tool"] == "read_file" and read["result_truncated"] is True and len(read["result"]) == 2000
    change = events[5]
    assert change["path"] == "src/x.py" and change["tool"] == "edit_file"
    assert events[2]["model"] == "m-1"


def test_the_workers_mcp_filters_events():
    from mcp_servers.workers_server import TOOLS, filter_session_events
    assert any(t.name == "session_events" for t in TOOLS)
    raw = render(build_transcript(_session()), "jsonl").content.decode("utf-8")
    out = filter_session_events(raw, ["file_change"], 10).splitlines()
    assert json.loads(out[0])["type"] == "session"
    assert [json.loads(l)["type"] for l in out[1:]] == ["file_change"]
    assert len(filter_session_events(raw, None, 1).splitlines()) == 2
