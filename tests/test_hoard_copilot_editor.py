import json

import pytest

from services.hoard_copilot import CopilotError, HoardCopilotStore, context_message, normalize_context
from services.projects import ProjectStore


def test_editor_reaches_stored_and_frozen_context_without_mutating_input(tmp_path):
    raw = {"origin": {"app_id": "links", "data_space_id": "qa-editor411"},
           "editor": {"revision": "r7", "fields": {"notes": "Unsent notes"}, "dirty": ["notes"]},
           "view": {"route": "bandeja"}}
    store = HoardCopilotStore(str(tmp_path / "copilot.db"), ProjectStore(str(tmp_path / "projects.json")))
    normalized = normalize_context(raw)
    assert normalized["editor"] == raw["editor"]
    normalized["editor"]["fields"]["notes"] = "Different"
    assert raw["editor"]["fields"]["notes"] == "Unsent notes"
    store.attach("sid", raw, "owner")
    reopened = HoardCopilotStore(store.path, store.projects)
    assert reopened.get("sid", "owner")["context"]["editor"] == raw["editor"]
    frozen = reopened.prepare_turn("sid", "owner", raw, "message1", "Use the draft")
    assert json.loads(context_message(frozen)["content"].split("\n", 1)[-1])["editor"] == raw["editor"]
    changed = {**raw, "editor": {"fields": {"notes": "Typed later"}}}
    with pytest.raises(CopilotError, match="different message or context"):
        reopened.prepare_turn("sid", "owner", changed, "message1", "Use the draft")
    continuation = reopened.prepare_turn("sid", "owner", None, "message2", "Continue")
    assert "editor" not in continuation
    assert "Unsent notes" not in context_message(continuation)["content"]
    assert reopened.prepare_turn("sid", "owner", raw, "message1", "Use the draft")["editor"] == raw["editor"]
