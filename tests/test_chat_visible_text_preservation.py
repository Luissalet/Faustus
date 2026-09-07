import pytest
from routes.chat_helpers import clean_thinking_for_save


@pytest.mark.parametrize("text", [
    "I will check the saved objective.\n\nThe objective is confirmed:\n\n- Title: Keep this\n- Priority: 1\n- Status: open",
    "I can help with this.\n\nHere is the complete answer.",
    "The user interface has two panels.\n\nThe left one contains projects.",
    "I need the filename to continue.\n\nPlease attach it.",
])
def test_ordinary_assistant_prose_is_not_reclassified_as_hidden_reasoning(text):
    saved, metadata = clean_thinking_for_save(text)
    assert saved == text
    assert not metadata.get("thinking")


def test_explicit_thinking_channel_still_separates_from_visible_answer():
    saved, metadata = clean_thinking_for_save("<think>Private reasoning.</think>\n\nVisible answer.")
    assert saved == "Visible answer."
    assert metadata["thinking"] == "Private reasoning."
