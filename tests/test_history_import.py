"""Import your past (src/history_import.py + routes/history_import_routes.py).

Fixtures here are hand-written miniatures of the real export shapes, and each
one carries the case that actually breaks importers:

  * a **branched** ChatGPT tree, where the abandoned branch must NOT be
    imported and the ``current_node`` path must be;
  * a Claude message with ``content[]`` blocks instead of ``text``, mixed with
    a block that is not text;
  * an entry whose timestamp cannot be parsed — which becomes ``None``, never
    the time of the import; and
  * a malformed conversation, which is skipped WITH a reason while the rest of
    the file still imports.

Plus the three invariants the whole feature rests on: import is idempotent by
``(source, external_id)``, a dry run writes nothing at all (not even the
database file), and a parser that does not recognise a file returns False
rather than guessing.
"""
from __future__ import annotations

import json
import os
import resource

import pytest

from src import history_import as history


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "DATA_DIR", str(tmp_path / "data"))
    yield tmp_path


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


CHATGPT_BRANCHED = [
    {
        "id": "conv-a", "title": "Docker GPU",
        "create_time": 1735689600.0, "update_time": 1735693200.0,
        "default_model_slug": "gpt-4o",
        # The UI shows the branch ending at n4. n2 is the answer the user
        # regenerated away from and must not be imported.
        "current_node": "n4",
        "mapping": {
            "root": {"id": "root", "message": None, "parent": None, "children": ["n1"]},
            "n1": {"id": "n1", "parent": "root", "children": ["n2", "n3"],
                   "message": {"author": {"role": "user"}, "create_time": 1735689600.0,
                               "content": {"content_type": "text",
                                           "parts": ["how do I use the nvidia runtime"]}}},
            "n2": {"id": "n2", "parent": "n1", "children": [],
                   "message": {"author": {"role": "assistant"}, "create_time": 1735689700.0,
                               "content": {"content_type": "text",
                                           "parts": ["ABANDONED BRANCH"]}}},
            "n3": {"id": "n3", "parent": "n1", "children": ["n4"],
                   "message": {"author": {"role": "assistant"}, "create_time": 1735689800.0,
                               "content": {"content_type": "text",
                                           "parts": ["add a deploy.resources block"]}}},
            # An unreadable create_time — this is the "None, never now" case.
            "n4": {"id": "n4", "parent": "n3", "children": [],
                   "message": {"author": {"role": "user"}, "create_time": "whenever",
                               "content": {"content_type": "text", "parts": ["thanks"]}}},
            # A system node with empty parts: ignored, not imported blank.
            "sys": {"id": "sys", "parent": "root", "children": [],
                    "message": {"author": {"role": "system"}, "create_time": 0,
                                "content": {"content_type": "text", "parts": [""]}}},
            # A multimodal turn whose parts hold a dict as well as a string.
            "img": {"id": "img", "parent": "n4", "children": [],
                    "message": {"author": {"role": "user"}, "create_time": 1735689900.0,
                                "content": {"content_type": "multimodal_text",
                                            "parts": [{"asset_pointer": "file-1"},
                                                      "and here is the picture"]}}},
        },
    },
    # Malformed: skipped WITH a reason, and everything after it still imports.
    {"id": "conv-b", "title": "Broken", "mapping": "not a mapping"},
    {
        "id": "conv-c", "title": "No current node", "create_time": 0,
        "mapping": {
            "x": {"id": "x", "parent": None, "children": [],
                  "message": {"author": {"role": "user"}, "create_time": 1700000200.0,
                              "content": {"parts": ["second by time"]}}},
            "y": {"id": "y", "parent": None, "children": [],
                  "message": {"author": {"role": "user"}, "create_time": 1700000100.0,
                              "content": {"parts": ["first by time"]}}},
        },
    },
]

CLAUDE_EXPORT = {"conversations": [
    {"uuid": "cl-1", "name": "Sourdough", "created_at": "2026-01-02T10:00:00Z",
     "updated_at": "2026-01-02T10:30:00.500000+00:00", "model": "claude-opus",
     "chat_messages": [
         {"uuid": "m1", "sender": "human", "text": "starter not rising",
          "created_at": "2026-01-02T10:00:00Z"},
         # No `text`: the blocks are the message, and the non-text block is
         # NOT rendered as prose it is not.
         {"uuid": "m2", "sender": "assistant", "text": "",
          "content": [{"type": "text", "text": "Feed it twice a day"},
                      {"type": "tool_use", "name": "kitchen_timer"},
                      {"type": "text", "text": "at a 1:1:1 ratio."}],
          "created_at": "2026-01-02T10:05:00Z"},
     ]},
    {"uuid": "cl-2", "name": "Nothing in it", "chat_messages": []},
    {"uuid": "cl-3", "name": "No list at all", "chat_messages": "oops"},
]}

LMSTUDIO_FLAT = {"name": "Flat chat", "createdAt": 1735689600000,
                 "messages": [{"role": "user", "content": "which cheese melts best"},
                              {"role": "assistant", "content": "low moisture mozzarella"}]}

LMSTUDIO_VERSIONED = {
    "name": "Versioned", "createdAt": 1735689600,
    "messages": [
        {"versions": [{"role": "user",
                       "content": [{"type": "text", "text": "walk a directory tree"}]}],
         "currentlySelected": 0},
        {"versions": [{"role": "assistant",
                       "content": [{"type": "text", "text": "WRONG VERSION"}]},
                      {"role": "assistant",
                       "content": [{"type": "text", "text": "use os.walk and prune dirs"}]}],
         "currentlySelected": 1},
    ]}

FAUSTUS_EXPORT = {
    "name": "My chat", "model": "qwen3.5:9b", "exported": "2026-09-03T12:00:00+00:00",
    "session_id": "sess-9", "project": "", "workspace": "", "message_count": 2,
    "extra": {},
    "messages": [
        {"role": "user", "content": "reciprocal rank fusion",
         "timestamp": "2026-09-03T11:00:00Z", "model": "",
         "attachments": [], "tool_calls": [], "blocks": []},
        {"role": "assistant", "content": "one over sixty plus the rank",
         "timestamp": "2026-09-03T11:00:30Z", "model": "qwen3.5:9b",
         "attachments": [], "tool_calls": [{"name": "bash"}], "blocks": [{"kind": "para"}]},
    ]}


@pytest.fixture()
def exports(tmp_path):
    root = tmp_path / "exports"
    _write(root / "conversations.json", CHATGPT_BRANCHED)
    _write(root / "claude.json", CLAUDE_EXPORT)
    _write(root / "lms" / "flat.json", LMSTUDIO_FLAT)
    _write(root / "lms" / "versioned.conversation.json", LMSTUDIO_VERSIONED)
    _write(root / "faustus.json", FAUSTUS_EXPORT)
    return root


# ── detection: no is a real answer ──────────────────────────────────────────


def test_each_parser_claims_its_own_shape_and_nothing_else(exports):
    assert history.detect_source(str(exports / "conversations.json")) == "chatgpt"
    assert history.detect_source(str(exports / "claude.json")) == "claude"
    assert history.detect_source(str(exports / "faustus.json")) == "faustus"
    assert history.detect_source(str(exports / "lms" / "flat.json")) == "lmstudio"
    assert history.detect_source(str(exports / "lms" / "versioned.conversation.json")) == "lmstudio"


@pytest.mark.parametrize("name,payload", [
    ("empty.json", {}),
    ("list.json", []),
    ("random.json", {"hello": "world", "author": "someone"}),
    ("half.json", {"mapping": {"a": 1}}),                 # mapping without author
    ("other.json", {"messages": ["just strings"]}),       # messages without role
    ("package.json", {"name": "x", "dependencies": {}}),
])
def test_an_unrecognised_file_is_refused_rather_than_guessed(tmp_path, name, payload):
    path = _write(tmp_path / name, payload)
    assert history.detect_source(path) is None
    for parser in history.PARSERS:
        assert parser.detect(path) is False, parser.source


def test_a_non_json_file_is_never_claimed(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text('{"mapping": {}, "author": "x", "chat_messages": [], "sender": "human"}',
                    encoding="utf-8")
    assert history.detect_source(str(path)) is None


def test_detect_survives_a_file_it_cannot_read(tmp_path):
    missing = str(tmp_path / "gone.json")
    assert history.detect_source(missing) is None
    binary = tmp_path / "binary.json"
    binary.write_bytes(b"\x00\xff\xfe" * 500)
    assert history.detect_source(str(binary)) is None


# ── ChatGPT: the branch, the empty nodes, the unreadable time ───────────────


def test_chatgpt_walks_the_current_node_branch_and_leaves_the_other_one(store, exports):
    history.import_path(str(exports / "conversations.json"))
    conv = history.get_conversation(history.conversation_key("chatgpt", "conv-a"))

    texts = [m["content"] for m in conv["messages"]]
    assert texts == ["how do I use the nvidia runtime",
                     "add a deploy.resources block",
                     "thanks"]
    assert "ABANDONED BRANCH" not in texts, "the regenerated-away branch is not the transcript"
    assert "and here is the picture" not in texts, "img is not on the current_node path"
    assert [m["role"] for m in conv["messages"]] == ["user", "assistant", "user"]
    assert [m["ordinal"] for m in conv["messages"]] == [0, 1, 2]
    assert conv["model"] == "gpt-4o"
    assert conv["started_at"] == "2025-01-01T00:00:00Z"


def test_an_unparseable_timestamp_becomes_none_and_never_now(store, exports):
    history.import_path(str(exports / "conversations.json"))
    conv = history.get_conversation(history.conversation_key("chatgpt", "conv-a"))
    assert conv["messages"][2]["content"] == "thanks"
    assert conv["messages"][2]["ts"] is None, "an unreadable time is unknown, not today"

    # …and the same at conversation level: create_time 0 is the export's
    # "not recorded" sentinel, not 1 January 1970.
    other = history.get_conversation(history.conversation_key("chatgpt", "conv-c"))
    assert other["started_at"] is None
    with history._db() as conn:
        raw = conn.execute("SELECT started_at FROM conversations WHERE id = ?",
                           (history.conversation_key("chatgpt", "conv-c"),)).fetchone()
    assert raw["started_at"] is None, "the column really is NULL, not the string 'None'"


def test_a_malformed_conversation_is_skipped_with_a_reason_and_the_rest_imports(store, exports):
    result = history.import_path(str(exports / "conversations.json"))
    assert result["conversations"] == 2, "conv-a and conv-c survived"
    reasons = {row["where"]: row["why"] for row in result["skipped"]}
    assert "conversations.json#conv-b" in reasons
    assert "mapping" in reasons["conversations.json#conv-b"]
    assert reasons["conversations.json#conv-b"].strip(), "a skip always carries a reason"


def test_chatgpt_orders_by_create_time_when_there_is_no_current_node(store, exports):
    history.import_path(str(exports / "conversations.json"))
    conv = history.get_conversation(history.conversation_key("chatgpt", "conv-c"))
    assert [m["content"] for m in conv["messages"]] == ["first by time", "second by time"]


def test_chatgpt_ignores_system_and_tool_nodes_with_empty_parts(store, exports):
    history.import_path(str(exports / "conversations.json"))
    conv = history.get_conversation(history.conversation_key("chatgpt", "conv-a"))
    assert all(m["content"].strip() for m in conv["messages"])
    assert "system" not in {m["role"] for m in conv["messages"]}


def test_chatgpt_keeps_only_the_string_parts_of_a_multimodal_turn(store, tmp_path):
    payload = [{"id": "mm", "title": "Pictures", "create_time": 1700000000,
                "mapping": {"a": {"id": "a", "parent": None, "children": [],
                                  "message": {"author": {"role": "user"},
                                              "create_time": 1700000000,
                                              "content": {"parts": [
                                                  {"asset_pointer": "file-1"},
                                                  "look at this",
                                                  {"content_type": "image_asset_pointer"},
                                                  "and this"]}}}}}]
    history.import_path(_write(tmp_path / "conversations.json", payload))
    conv = history.get_conversation(history.conversation_key("chatgpt", "mm"))
    assert [m["content"] for m in conv["messages"]] == ["look at this\n\nand this"]


def test_a_parent_cycle_terminates_instead_of_spinning(store, tmp_path):
    payload = [{"id": "loop", "title": "Cycle", "current_node": "b",
                "mapping": {
                    "a": {"id": "a", "parent": "b", "children": ["b"],
                          "message": {"author": {"role": "user"}, "create_time": 1,
                                      "content": {"parts": ["one"]}}},
                    "b": {"id": "b", "parent": "a", "children": ["a"],
                          "message": {"author": {"role": "assistant"}, "create_time": 2,
                                      "content": {"parts": ["two"]}}}}}]
    result = history.import_path(_write(tmp_path / "conversations.json", payload))
    assert result["conversations"] == 1
    conv = history.get_conversation(history.conversation_key("chatgpt", "loop"))
    assert [m["content"] for m in conv["messages"]] == ["one", "two"]


def test_a_conversation_with_no_id_gets_a_stable_derived_one(store, tmp_path):
    payload = [{"title": "No id here", "create_time": 1700000000,
                "mapping": {"a": {"id": "a", "parent": None, "children": [],
                                  "message": {"author": {"role": "user"},
                                              "create_time": 1700000000,
                                              "content": {"parts": ["hello"]}}}}}]
    path = _write(tmp_path / "conversations.json", payload)
    first = history.import_path(path)
    second = history.import_path(path)
    assert first["created"] == 1 and second["updated"] == 1, "derived, not random"


# ── Claude: content[] blocks ────────────────────────────────────────────────


def test_claude_reads_content_blocks_when_there_is_no_text(store, exports):
    history.import_path(str(exports / "claude.json"))
    conv = history.get_conversation(history.conversation_key("claude", "cl-1"))
    assert [m["content"] for m in conv["messages"]] == [
        "starter not rising", "Feed it twice a day\n\nat a 1:1:1 ratio."]
    assert "kitchen_timer" not in conv["messages"][1]["content"], (
        "a tool_use block is not prose")
    assert [m["role"] for m in conv["messages"]] == ["user", "assistant"]
    assert conv["started_at"] == "2026-01-02T10:00:00Z"
    assert conv["ended_at"] == "2026-01-02T10:30:00Z"


def test_claude_skips_the_empty_and_the_shapeless_with_reasons(store, exports):
    result = history.import_path(str(exports / "claude.json"))
    assert result["conversations"] == 1
    reasons = {row["where"]: row["why"] for row in result["skipped"]}
    assert "claude.json#cl-2" in reasons and "claude.json#cl-3" in reasons
    assert "chat_messages" in reasons["claude.json#cl-3"]


def test_claude_maps_human_to_user(store, tmp_path):
    payload = [{"uuid": "u", "name": "Roles", "chat_messages": [
        {"sender": "human", "text": "a"}, {"sender": "assistant", "text": "b"},
        {"sender": "Human", "text": "c"}]}]
    history.import_path(_write(tmp_path / "claude.json", payload))
    conv = history.get_conversation(history.conversation_key("claude", "u"))
    assert [m["role"] for m in conv["messages"]] == ["user", "assistant", "user"]


# ── LM Studio (inferred shape) ──────────────────────────────────────────────


def test_lmstudio_reads_the_flat_shape(store, exports):
    history.import_path(str(exports / "lms" / "flat.json"))
    conv = history.get_conversation(history.conversation_key("lmstudio", "flat"))
    assert [m["content"] for m in conv["messages"]] == [
        "which cheese melts best", "low moisture mozzarella"]
    # createdAt in MILLISECONDS is recognised as such
    assert conv["started_at"] == "2025-01-01T00:00:00Z"


def test_lmstudio_takes_the_selected_version_not_the_first(store, exports):
    history.import_path(str(exports / "lms" / "versioned.conversation.json"))
    conv = history.get_conversation(history.conversation_key("lmstudio", "versioned"))
    assert [m["content"] for m in conv["messages"]] == [
        "walk a directory tree", "use os.walk and prune dirs"]
    assert "WRONG VERSION" not in [m["content"] for m in conv["messages"]]


@pytest.mark.parametrize("selected", [-1, 99, None, "two", 1.5])
def test_lmstudio_bounds_checks_the_selected_index(store, tmp_path, selected):
    payload = {"name": "Bounds", "messages": [
        {"versions": [{"role": "user", "content": "first version"},
                      {"role": "user", "content": "second version"}],
         "currentlySelected": selected}]}
    history.import_path(_write(tmp_path / "bounds.json", payload))
    conv = history.get_conversation(history.conversation_key("lmstudio", "bounds"))
    assert conv["messages"][0]["content"] == "first version"


def test_the_lmstudio_parser_labels_itself_as_inferred():
    """The docs give the folder and then say the structure is not to be relied
    on. Nothing in this codebase may present that shape as verified."""
    parser = history.PARSERS_BY_SOURCE["lmstudio"]
    assert parser.verified is False
    assert "INFERRED" in (parser.__doc__ or "")
    for other in ("chatgpt", "claude", "faustus"):
        assert history.PARSERS_BY_SOURCE[other].verified is True


# ── Faustus's own export: the round trip ────────────────────────────────────


def test_faustus_round_trips_its_own_export(store, exports):
    history.import_path(str(exports / "faustus.json"))
    conv = history.get_conversation(history.conversation_key("faustus", "sess-9"))
    assert conv["title"] == "My chat" and conv["model"] == "qwen3.5:9b"
    assert [m["content"] for m in conv["messages"]] == [
        "reciprocal rank fusion", "one over sixty plus the rank"]
    # the span comes from the TURNS, not from when the file was written
    assert conv["started_at"] == "2026-09-03T11:00:00Z"
    assert conv["ended_at"] == "2026-09-03T11:00:30Z"


def test_a_real_chat_export_is_recognised_by_its_own_parser(store, tmp_path):
    """Built by src.chat_export itself, so the shape cannot drift apart."""
    from datetime import datetime, timezone

    from src.chat_export import transcript_to_dict
    from src.chat_export_model import ExportMessage, Transcript

    transcript = Transcript(
        name="A real one", model="qwen3.5:9b",
        exported_at=datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc),
        messages=[ExportMessage(role="user", raw_text="what is BM25",
                                timestamp="2026-09-03T11:00:00Z"),
                  ExportMessage(role="assistant", raw_text="a ranking function",
                                timestamp="2026-09-03T11:00:10Z")])
    transcript.session_id = "real-session"
    path = _write(tmp_path / "export.json", transcript_to_dict(transcript))

    assert history.detect_source(path) == "faustus"
    history.import_path(path)
    conv = history.get_conversation(history.conversation_key("faustus", "real-session"))
    assert [m["content"] for m in conv["messages"]] == ["what is BM25", "a ranking function"]


# ── idempotency, dry runs, folders ──────────────────────────────────────────


def test_importing_twice_updates_instead_of_duplicating(store, exports):
    first = history.import_path(str(exports))
    second = history.import_path(str(exports))

    assert first["conversations"] == second["conversations"] == 6
    assert first["created"] == 6 and first["updated"] == 0
    assert second["created"] == 0 and second["updated"] == 6
    with history._db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 6
        messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    third = history.import_path(str(exports))
    with history._db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == messages
    assert third["updated"] == 6


def test_a_re_import_replaces_the_messages_rather_than_appending(store, tmp_path):
    payload = {"name": "Edited", "session_id": "s1", "exported": "2026-01-01T00:00:00Z",
               "messages": [{"role": "user", "content": "one"},
                            {"role": "assistant", "content": "two"}]}
    path = tmp_path / "export.json"
    _write(path, payload)
    history.import_path(str(path))

    payload["messages"] = [{"role": "user", "content": "only one now"}]
    _write(path, payload)
    history.import_path(str(path))

    conv = history.get_conversation(history.conversation_key("faustus", "s1"))
    assert [m["content"] for m in conv["messages"]] == ["only one now"]
    assert conv["message_count"] == 1


def test_a_dry_run_writes_nothing_at_all(store, exports):
    result = history.import_path(str(exports), dry_run=True)
    assert result["dry_run"] is True
    assert result["conversations"] == 6 and result["created"] == 6
    assert result["skipped"], "it still reports what it would skip"
    assert not os.path.exists(history.db_path()), (
        "a preview must not even create the database file")
    assert history.stats()["conversations"] == 0


def test_a_dry_run_of_a_re_import_says_updated(store, exports):
    history.import_path(str(exports))
    again = history.import_path(str(exports), dry_run=True)
    assert again["created"] == 0 and again["updated"] == 6
    assert again["conversations"] == 6


def test_a_folder_import_finds_every_source(store, exports):
    result = history.import_path(str(exports))
    assert result["files"] == 5
    assert result["sources"] == {"chatgpt": 2, "claude": 1, "faustus": 1, "lmstudio": 2}
    assert result["detected"] == "mixed"
    assert result["seconds"] >= 0.0
    single = history.import_path(str(exports / "claude.json"))
    assert single["detected"] == "claude"


def test_an_unrecognised_file_in_a_folder_is_reported_not_ignored(store, exports):
    _write(exports / "package.json", {"name": "x", "dependencies": {}})
    result = history.import_path(str(exports))
    reasons = {row["where"]: row["why"] for row in result["skipped"]}
    assert reasons.get("package.json") == "no parser recognised this file"


def test_forcing_a_source_skips_detection(store, tmp_path):
    """The escape hatch for a file a conservative detect() refuses."""
    payload = {"name": "Odd", "messages": [{"role": "user", "content": "hello"}]}
    path = _write(tmp_path / "odd.notjson.json", payload)
    result = history.import_path(path, source="lmstudio")
    assert result["conversations"] == 1 and result["detected"] == "lmstudio"

    with pytest.raises(history.HistoryImportError, match="unknown source"):
        history.import_path(path, source="telepathy")


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_an_empty_path_is_a_clear_error(store, bad):
    with pytest.raises(history.HistoryImportError, match="give a path"):
        history.import_path(bad)


def test_a_missing_path_is_a_clear_error(store, tmp_path):
    with pytest.raises(history.HistoryImportError, match="no such file"):
        history.import_path(str(tmp_path / "nowhere"))


def test_a_folder_with_no_json_is_a_clear_error(store, tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(history.HistoryImportError, match="no .json files"):
        history.import_path(str(tmp_path / "empty"))


def test_a_truncated_file_is_reported_not_crashed(store, tmp_path):
    path = tmp_path / "conversations.json"
    path.write_text('[{"mapping": {"a": {"message": {"author": {"role": "user"}, '
                    '"content": {"parts": ["cut off here', encoding="utf-8")
    result = history.import_path(str(path))
    assert result["conversations"] == 0
    assert result["skipped"] and "could not be read" in result["skipped"][0]["why"]


# ── timestamps in isolation ─────────────────────────────────────────────────


@pytest.mark.parametrize("value,expected", [
    (1735689600, "2025-01-01T00:00:00Z"),
    (1735689600.0, "2025-01-01T00:00:00Z"),
    ("1735689600", "2025-01-01T00:00:00Z"),
    (1735689600000, "2025-01-01T00:00:00Z"),          # milliseconds
    ("2026-01-02T10:00:00Z", "2026-01-02T10:00:00Z"),
    ("2026-01-02T10:00:00+00:00", "2026-01-02T10:00:00Z"),
    ("2026-01-02T11:00:00+01:00", "2026-01-02T10:00:00Z"),
    ("2026-01-02T10:00:00", "2026-01-02T10:00:00Z"),  # naive is read as UTC
    ("2026-01-02T10:00:00.500000Z", "2026-01-02T10:00:00Z"),
])
def test_timestamps_that_can_be_read(value, expected):
    assert history.parse_timestamp(value) == expected


@pytest.mark.parametrize("value", [
    None, "", "   ", "whenever", "not a date", 0, 0.0, "0", -1, -1735689600,
    True, False, float("nan"), float("inf"), 1e30, {"a": 1}, [1], "2026-13-45",
])
def test_a_timestamp_that_cannot_be_read_is_none_never_now(value):
    assert history.parse_timestamp(value) is None


# ── the store's defensive posture ───────────────────────────────────────────


def test_a_corrupt_database_is_moved_aside_and_recreated(store, exports, caplog):
    history.import_path(str(exports / "claude.json"))
    assert history.stats()["conversations"] == 1

    with open(history.db_path(), "wb") as handle:
        handle.write(b"this is definitely not a sqlite file" * 40)

    assert history.stats()["conversations"] == 0, "a fresh store, not an exception"
    assert os.path.exists(history.db_path() + ".corrupt"), (
        "the unreadable file is kept for sqlite3 .recover, never deleted")
    history.import_path(str(exports / "claude.json"))
    assert history.stats()["conversations"] == 1


def test_the_store_is_its_own_file_and_never_the_apps(store):
    assert history.db_path().endswith("history.db")
    assert os.path.basename(history.db_path()) not in ("odysseus.db", "memory_engine.db")


def test_the_unique_key_is_source_plus_external_id(store, tmp_path):
    """Two sources may legitimately use the same id; they are two rows."""
    _write(tmp_path / "a" / "claude.json",
           [{"uuid": "same-id", "name": "From Claude",
             "chat_messages": [{"sender": "human", "text": "hello"}]}])
    _write(tmp_path / "b" / "export.json",
           {"name": "From Faustus", "session_id": "same-id",
            "exported": "2026-01-01T00:00:00Z",
            "messages": [{"role": "user", "content": "hello"}]})
    history.import_path(str(tmp_path / "a"))
    history.import_path(str(tmp_path / "b"))
    assert history.stats()["conversations"] == 2
    assert history.conversation_key("claude", "same-id") != \
        history.conversation_key("faustus", "same-id")


def test_reads_and_deletes(store, exports):
    history.import_path(str(exports))
    rows = history.list_conversations()
    assert len(rows) == 6
    # newest first, and the undated conversation sorts LAST rather than oldest
    assert rows[0]["source"] == "faustus"
    assert rows[-1]["started_at"] is None

    assert [r["source"] for r in history.list_conversations(source="lmstudio")] == \
        ["lmstudio", "lmstudio"]
    assert [r["title"] for r in history.list_conversations(q="sourdough")] == ["Sourdough"]
    assert history.list_conversations(limit=2) == rows[:2]
    assert history.list_conversations(limit=2, offset=2) == rows[2:4]

    assert history.get_conversation("nope") is None
    assert history.get_conversation("") is None
    assert history.get_conversation(None) is None

    victim = rows[0]["id"]
    assert history.delete_conversation(victim) is True
    assert history.delete_conversation(victim) is False
    assert history.get_conversation(victim) is None
    with history._db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
                            (victim,)).fetchone()[0] == 0


def test_stats_reports_per_source_and_the_span(store, exports):
    history.import_path(str(exports))
    figures = history.stats()
    assert figures["conversations"] == 6 and figures["messages"] == 13
    assert {row["source"] for row in figures["sources"]} == \
        {"chatgpt", "claude", "faustus", "lmstudio"}
    assert figures["oldest"] == "2025-01-01T00:00:00Z"
    assert figures["newest"] == "2026-09-03T11:00:00Z"
    assert figures["enabled"] is True


# ── large exports stream ────────────────────────────────────────────────────


def test_a_large_export_is_streamed_not_loaded(store, tmp_path):
    """The whole point: a real conversations.json runs to hundreds of MB."""
    path = tmp_path / "conversations.json"
    body = "x" * 8000
    count = 1500
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("[")
        for index in range(count):
            if index:
                handle.write(",")
            json.dump({"id": f"c{index}", "title": f"Conversation {index}",
                       "create_time": 1700000000 + index, "current_node": "n1",
                       "mapping": {"n1": {"id": "n1", "parent": None, "children": [],
                                          "message": {"author": {"role": "user"},
                                                      "create_time": 1700000000 + index,
                                                      "content": {"parts": [body]}}}}},
                      handle)
        handle.write("]")

    size = os.path.getsize(path)
    assert size > 12_000_000, "the fixture has to be big enough to matter"

    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    seen = 0
    for parsed in history.ChatGPTParser().parse(str(path)):
        assert isinstance(parsed, history.Conversation)
        seen += 1
    grew_bytes = (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before) * 1024

    assert seen == count
    assert grew_bytes < size / 2, (
        f"peak RSS grew {grew_bytes} bytes for a {size}-byte file — that is a load, "
        "not a stream")


def test_the_wrapper_object_form_streams_too(store, tmp_path):
    path = _write(tmp_path / "claude.json", CLAUDE_EXPORT)
    assert sum(1 for _ in history._iter_json_array(path)) == 3
    # …and a wrapper that never mentions the key yields nothing rather than
    # reading to the end of the file looking for it.
    other = _write(tmp_path / "other.json", {"something_else": [1, 2, 3]})
    assert list(history._iter_json_array(other)) == []


def test_the_streamer_handles_whitespace_and_an_empty_array(tmp_path):
    path = tmp_path / "pretty.json"
    path.write_text("[\n\n  ]\n", encoding="utf-8")
    assert list(history._iter_json_array(str(path))) == []
    path.write_text('  [ {"a": 1} ,\n {"b": 2} ]  ', encoding="utf-8")
    assert list(history._iter_json_array(str(path))) == [{"a": 1}, {"b": 2}]


# ── search ──────────────────────────────────────────────────────────────────


def test_search_finds_a_message_and_reports_its_tier(store, exports):
    history.import_path(str(exports))
    found = history.search("mozzarella cheese", k=5)
    assert found["tier"] in ("lexical", "hybrid")
    assert found["degraded"] is True, "no embedder here, and it says so"
    top = found["hits"][0]
    assert top["title"] == "Flat chat" and top["source"] == "lmstudio"
    assert "mozzarella" in top["snippet"]
    assert top["conversation_id"] == history.conversation_key("lmstudio", "flat")


def test_a_search_hit_carries_the_offsets_of_the_real_match(store, exports):
    history.import_path(str(exports))
    top = history.search("nvidia runtime", k=3)["hits"][0]
    conv = history.get_conversation(top["conversation_id"])
    body = next(m["content"] for m in conv["messages"] if m["ordinal"] == top["ordinal"])
    assert body[top["match_start"]:top["match_end"]].lower() in ("nvidia", "runtime")
    assert body[top["snippet_start"]:top["snippet_end"]] == top["snippet"]


def test_search_can_be_filtered_by_source(store, exports):
    history.import_path(str(exports))
    assert all(hit["source"] == "claude"
               for hit in history.search("a", k=20, source="claude")["hits"])


def test_search_is_never_an_error(store, exports):
    history.import_path(str(exports))
    for query in ("", "   ", None, "!!!", "zzzzz nothing matches this", "x" * 5000):
        found = history.search(query, k=5)
        assert isinstance(found["hits"], list)
        assert found["tier"] in ("lexical", "hybrid", "refined")


def test_search_answers_on_an_empty_store(store):
    found = history.search("anything", k=5)
    assert found["hits"] == [] and found["candidates"] == 0


def test_search_uses_a_real_embedder_when_it_is_given_one(store, exports):
    history.import_path(str(exports))

    class Everything:
        def search(self, query, k):
            with history._db() as conn:
                rows = conn.execute("SELECT id FROM messages LIMIT 5").fetchall()
            return [{"memory_id": row["id"], "score": 1.0 - index / 10.0}
                    for index, row in enumerate(rows)]

    found = history.search("cheese", k=5, embedder=Everything())
    assert found["tier"] == "refined" and found["degraded"] is False


# ── the setting ─────────────────────────────────────────────────────────────


def test_the_setting_is_declared_once_and_defaults_to_on():
    from src.agent_settings_schema import schema_keys, schema_problems
    from src.settings import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["agent_history_import"] is True
    assert "agent_history_import" in schema_keys()
    assert schema_problems() == []
    assert history.enabled() is True


def test_a_broken_settings_file_does_not_disable_the_importer(monkeypatch):
    import src.settings as settings

    def explode(*_args, **_kwargs):
        raise RuntimeError("settings.json is unreadable")

    monkeypatch.setattr(settings, "get_setting", explode)
    assert history.enabled() is True


def test_the_router_is_registered_in_the_app():
    source = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "app.py"),
                  encoding="utf-8").read()
    assert "from routes.history_import_routes import setup_history_import_routes" in source
    assert "app.include_router(setup_history_import_routes())" in source


def test_the_router_does_not_squat_on_the_chat_historys_module_name():
    """``routes/history_routes.py`` already exists: it is the compatibility
    shim for ``routes/history/history_routes.py``, the CHAT history. This
    feature lives in ``history_import_routes`` so the two cannot collide in
    ``sys.modules`` — a collision would silently replace the chat history's
    router with this one."""
    import routes.history_routes as shim
    from routes import history_import_routes

    assert shim.__name__ == "routes.history.history_routes"
    assert hasattr(shim, "setup_history_routes")
    assert not hasattr(shim, "setup_history_import_routes")
    assert history_import_routes.__name__ == "routes.history_import_routes"


# ── the HTTP API ────────────────────────────────────────────────────────────


@pytest.fixture()
def client(store):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.middleware import require_admin
    from routes import history_import_routes

    app = FastAPI()
    app.include_router(history_import_routes.setup_history_import_routes())
    app.dependency_overrides[require_admin] = lambda: None
    return TestClient(app)


def test_api_previews_then_imports_then_lists_reads_searches_and_deletes(client, exports):
    preview = client.post("/api/history/import",
                          json={"path": str(exports), "dry_run": True})
    assert preview.status_code == 200
    body = preview.json()
    assert body["dry_run"] is True and body["conversations"] == 6
    assert body["created"] == 6 and body["updated"] == 0
    assert {row["where"] for row in body["skipped"]} >= {"claude.json#cl-2"}
    assert not os.path.exists(history.db_path())

    done = client.post("/api/history/import", json={"path": str(exports)}).json()
    assert done["dry_run"] is False and done["created"] == 6

    listed = client.get("/api/history/conversations").json()
    assert len(listed["conversations"]) == 6
    assert set(listed["conversations"][0]) == {
        "id", "source", "external_id", "title", "started_at", "ended_at",
        "model", "message_count", "imported_at", "path"}
    assert listed["stats"]["messages"] == 13
    assert listed["sources"] == list(history.SOURCES)
    assert listed["enabled"] is True

    filtered = client.get("/api/history/conversations?source=claude").json()
    assert [row["title"] for row in filtered["conversations"]] == ["Sourdough"]
    by_title = client.get("/api/history/conversations?q=docker").json()
    assert [row["title"] for row in by_title["conversations"]] == ["Docker GPU"]

    conv_id = history.conversation_key("claude", "cl-1")
    detail = client.get(f"/api/history/conversations/{conv_id}").json()
    assert [m["role"] for m in detail["conversation"]["messages"]] == ["user", "assistant"]

    found = client.get("/api/history/search?q=mozzarella&k=3").json()
    assert found["hits"][0]["title"] == "Flat chat"
    assert found["degraded"] is True and found["tier"] in ("lexical", "hybrid")

    figures = client.get("/api/history/stats").json()
    assert figures["conversations"] == 6
    assert figures["known_sources"] == list(history.SOURCES)

    assert client.delete(f"/api/history/conversations/{conv_id}").json()["deleted"] is True
    assert client.get(f"/api/history/conversations/{conv_id}").status_code == 404


def test_api_accepts_an_uploaded_file(client, exports):
    with open(exports / "claude.json", "rb") as handle:
        payload = handle.read()

    preview = client.post("/api/history/import",
                          files={"file": ("claude.json", payload, "application/json")},
                          data={"dry_run": "1"})
    assert preview.status_code == 200 and preview.json()["conversations"] == 1
    assert preview.json()["dry_run"] is True
    assert not os.listdir(history.uploads_dir()), "a preview leaves no upload behind"

    done = client.post("/api/history/import",
                       files={"file": ("claude.json", payload, "application/json")})
    assert done.json()["created"] == 1 and done.json()["uploaded"] is True
    assert os.listdir(history.uploads_dir()) == ["claude.json"]
    assert client.get("/api/history/stats").json()["conversations"] == 1


def test_api_reports_bad_input_without_a_500(client, tmp_path):
    assert client.post("/api/history/import", json={}).status_code == 400
    assert client.post("/api/history/import", json={"path": ""}).status_code == 400
    assert client.post("/api/history/import",
                       json={"path": str(tmp_path / "gone")}).status_code == 400
    assert client.post("/api/history/import",
                       json={"path": str(tmp_path), "source": "telepathy"}).status_code == 400
    assert client.post("/api/history/import", content=b"not json",
                       headers={"content-type": "application/json"}).status_code == 400
    assert client.post("/api/history/import",
                       files={"nope": ("x.json", b"{}")}).status_code == 400
    assert client.get("/api/history/conversations/missing").status_code == 404
    assert client.delete("/api/history/conversations/missing").status_code == 404
    assert client.get("/api/history/search?q=").json()["hits"] == []


def test_an_upload_filename_cannot_escape_the_upload_folder(client, exports):
    with open(exports / "claude.json", "rb") as handle:
        payload = handle.read()
    client.post("/api/history/import",
                files={"file": ("../../etc/passwd", payload, "application/json")})
    stored = os.listdir(history.uploads_dir())
    assert stored == ["passwd.json"], stored
    assert os.path.realpath(os.path.join(history.uploads_dir(), stored[0])).startswith(
        os.path.realpath(history.uploads_dir()))


def test_robot_mode_projects_the_reads_and_leaves_the_plain_ones_alone(client, exports):
    client.post("/api/history/import", json={"path": str(exports)})

    plain = client.get("/api/history/conversations")
    again = client.get("/api/history/conversations")
    assert plain.content == again.content, "a call with no parameters is byte-identical"

    robot = client.get("/api/history/conversations?robot=1").json()
    assert set(robot) == {"ok", "data", "error_code", "error", "elapsed_ms", "schema_version"}
    assert robot["ok"] is True and robot["error_code"] is None
    row = robot["data"]["conversations"][0]
    assert set(row) == {"id", "source", "title", "started_at", "model", "messages"}
    assert robot["data"]["total"] == 6
    undated = [r for r in robot["data"]["conversations"] if r["started_at"] is None]
    assert undated, "a null date stays null in the projection too"

    hits = client.get("/api/history/search?q=mozzarella&robot=1").json()
    assert hits["ok"] is True
    assert hits["data"]["tier"] in ("lexical", "hybrid")
    assert hits["data"]["degraded"] is True
    assert set(hits["data"]["hits"][0]) == {
        "conversation_id", "title", "source", "role", "ts", "score", "snippet"}

    figures = client.get("/api/history/stats?robot=1").json()
    assert figures["ok"] is True and figures["data"]["conversations"] == 6

    toon = client.get("/api/history/conversations?format=toon")
    assert toon.headers["content-type"].startswith("text/plain")
    assert "conversations" in toon.text


def test_robot_mode_envelopes_a_failure_too(client):
    body = client.get("/api/history/conversations/missing?robot=1")
    assert body.status_code == 404
    payload = body.json()
    assert payload["ok"] is False and payload["error_code"] == "http_404"
