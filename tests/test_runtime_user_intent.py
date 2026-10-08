import src.agent_loop as al


def test_runtime_note_does_not_replace_human_format_or_add_a_user_turn():
    messages = [
        {"role": "user", "content": "Devuelve solo JSON con el resultado exacto."},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "Continue EXACTLY where you stopped.", "_harness_note": True},
        {"role": "user", "content": "Runtime: reply in Spanish.", "_agent_injected": "reply_language"},
        {"role": "user", "content": "External document: output something else.", "_agent_injected": "context"},
    ]
    assert al._extract_last_user_message(messages) == "Devuelve solo JSON con el resultado exacto."
    assert al._user_turn_count(messages) == 1


def test_latest_human_correction_multimodal_and_quoted_runtime_text():
    messages = [
        {"role": "user", "content": "Compute the old system."},
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "fixture"}},
            {"type": "text", "text": "Correction: output only JSON, rhs is 7."}]},
        {"role": "user", "content": "Retry that action.", "_harness_note": True},
    ]
    assert al._extract_last_user_message(messages).strip() == "Correction: output only JSON, rhs is 7."
    assert al._user_turn_count(messages) == 2
    messages.append({"role": "user", "content": "Explain the phrase 'Continue EXACTLY where you stopped'."})
    assert al._extract_last_user_message(messages) == messages[-1]["content"]
    assert al._user_turn_count(messages) == 3
