"""A round whose narration is in the wrong language and that stops at a
card never gets the next round, which is where the language nudge acts:
the narration is dropped instead of being left above the card."""
import json

import src.agent_loop as al
from tests.test_research_streak import _collect, _events, _patch_common

ENGLISH = "I will now ask you which of the two folders you want me to use for the new report."


def test_english_narration_before_a_question_card_is_dropped(tmp_path, monkeypatch):
    _patch_common(monkeypatch, {})

    async def _fake_exec(block, *a, **k):
        return (block.tool_type, {"ask_user": {"question": "¿Qué carpeta uso?", "options": [
            {"label": "A"}, {"label": "B"}]}, "output": "asked", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)

    async def _fake_stream(_candidates, messages, **kwargs):
        yield "data: " + json.dumps({"delta": ENGLISH}) + "\n\n"
        yield "data: " + json.dumps({"type": "tool_calls", "calls": [{"name": "ask_user", "arguments": json.dumps({
            "question": "¿Qué carpeta uso?", "options": [{"label": "A"}, {"label": "B"}]})}]}) + "\n\n"
        yield "data: " + json.dumps({"type": "finish", "finish_reason": "tool_calls"}) + "\n\n"
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    events = _events(_collect(al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        [{"role": "user", "content": "prepara el informe nuevo en la carpeta que te diga, por favor"}],
        max_rounds=4, relevant_tools={"ask_user", "read_file"}, workspace=str(tmp_path), session_id="s-lang",
    )))
    replaced = [e for e in events if e.get("type") == "response_replace"]
    assert replaced, [e.get("type") for e in events]
    assert ENGLISH not in replaced[-1]["text"]
    # Since 7c4a96f9 the narration is retracted as soon as the round ends,
    # before the card's question is streamed; what the client shows is the
    # last replacement plus the text streamed after it.
    visible = ""
    for e in events:
        if e.get("type") == "response_replace":
            visible = e.get("text") or ""
        elif e.get("delta") and not e.get("thinking"):
            visible += e["delta"]
    assert ENGLISH not in visible
    assert "¿Qué carpeta uso?" in visible
