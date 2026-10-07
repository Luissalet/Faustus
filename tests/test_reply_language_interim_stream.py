from pathlib import Path
import asyncio
import json

import src.agent_loop as al
from src.reply_language import drop_mismatched_interim_progress, reply_language_mismatch


def test_english_progress_is_removed_before_spanish_followup():
    english = ("The search returned native tools instead of MCP tools. "
               "Let me try listing the MCP category and searching by exact name.")
    final = "Se guardó la revisión y ahora inspecciono el PDF final."
    assert reply_language_mismatch("es", english) == "en"
    streamed = english
    cleaned, dropped = drop_mismatched_interim_progress(
        streamed, english, english, language_mismatch=True, has_tool_calls=True
    )
    # A later round appends its final Spanish answer to the now-clean stream.
    final_stream = cleaned + "\n\n" + final
    assert dropped is True
    assert final_stream.strip() == final
    assert "The search returned" not in final_stream


def test_correct_language_and_non_tool_turn_are_kept():
    text = "Se guardó la revisión."
    assert drop_mismatched_interim_progress(
        text, text, text, language_mismatch=False, has_tool_calls=True
    ) == (text, False)
    assert drop_mismatched_interim_progress(
        text, text, text, language_mismatch=True, has_tool_calls=False
    ) == (text, False)


def test_quotes_code_json_and_tool_data_are_not_removed():
    samples = [
        'Status: the tool returned `{"ok": true}`.',
        'The source says "approved" and I will verify it.',
        '{"status": "ready"}',
        'See https://example.test/item.',
    ]
    for text in samples:
        assert drop_mismatched_interim_progress(
            text, text, text, language_mismatch=True, has_tool_calls=True
        ) == (text, False)


def test_only_the_current_round_suffix_can_be_removed():
    earlier = "Previous valid response."
    round_text = "The search returned native tools and I will continue."
    full = earlier + "\n\n" + round_text
    assert drop_mismatched_interim_progress(
        full, round_text, round_text, language_mismatch=True, has_tool_calls=True
    ) == (earlier, True)
    assert drop_mismatched_interim_progress(
        full + " trailing", round_text, round_text,
        language_mismatch=True, has_tool_calls=True
    ) == (full + " trailing", False)


def _collect(gen):
    async def _run():
        return [chunk async for chunk in gen]
    return asyncio.run(_run())


def _events(chunks):
    result = []
    for chunk in chunks:
        if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
            try:
                result.append(json.loads(chunk[6:]))
            except Exception:
                pass
    return result


def test_live_loop_replaces_transient_progress_and_keeps_raw_audit(tmp_path, monkeypatch):
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)
    monkeypatch.setattr(al, "_agent_route_tool_mode",
                        lambda *a, **k: (True, False, True), raising=False)
    (tmp_path / "note.txt").write_text("El documento está listo.", encoding="utf-8")
    progress = ("The search returned native tools instead of MCP tools. "
                "Let me try listing the MCP category and searching by exact name.")
    final = "El texto es: el documento está listo."
    provider_rounds = []
    calls = {"n": 0}

    async def _fake_exec(block, *a, **k):
        return (block.tool_type, {"output": "El documento está listo.", "exit_code": 0})

    async def _fake_stream(_candidates, messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            provider_rounds.append(progress)
            yield "data: " + json.dumps({"delta": progress}) + "\n\n"
            yield "data: " + json.dumps({"type": "tool_calls", "calls": [
                {"name": "read_file", "arguments": json.dumps({"path": "note.txt"})}
            ]}) + "\n\n"
            yield "data: " + json.dumps({"type": "finish", "finish_reason": "tool_calls"}) + "\n\n"
        else:
            provider_rounds.append(final)
            yield "data: " + json.dumps({"delta": final}) + "\n\n"
            yield "data: " + json.dumps({"type": "finish", "finish_reason": "stop"}) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    events = _events(_collect(al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "stub-qwen",
        [{"role": "user", "content": "¿Qué texto aparece en note.txt? Contesta en español."}],
        max_rounds=4, relevant_tools={"read_file"},
        session_id="language-stream-test",
    )))

    replacements = [event for event in events if event.get("type") == "response_replace"]
    assert replacements, events
    assert "The search returned" not in replacements[0].get("text", "")
    english_delta_index = next(i for i, event in enumerate(events)
                               if event.get("delta") == progress)
    replacement_index = events.index(replacements[0])
    spanish_delta_index = next(i for i, event in enumerate(events)
                               if event.get("delta") == final)
    assert english_delta_index < replacement_index < spanish_delta_index
    assert "El texto es" in "".join(
        event.get("delta", "") for event in events if "delta" in event and not event.get("thinking")
    )
    visible = ""
    for event in events:
        if event.get("type") == "response_replace":
            visible = event.get("text", "")
        elif "delta" in event and not event.get("thinking"):
            visible += event.get("delta", "")
    assert visible.strip() == final
    assert "The search returned" not in visible
    assert calls["n"] == 2
    round_infos = [event for event in events if event.get("type") == "round_info"]
    assert len(round_infos) == 2
    metrics = [event["data"] for event in events if event.get("type") == "metrics"][-1]
    assert metrics["round_texts"][0] == ""
    assert metrics["round_texts"][1] == final
    assert metrics["provider_round_texts"] == provider_rounds
    assert "The search returned" not in " ".join(metrics["round_texts"])



def test_provider_text_metrics_are_omitted_when_nothing_was_retracted():
    metrics = al._compute_final_metrics(
        [], "Respuesta normal.", 1.0, 0.2, 8192, 0, 0, False, [], [],
        provider_round_texts=None,
    )
    assert "provider_round_texts" not in metrics
