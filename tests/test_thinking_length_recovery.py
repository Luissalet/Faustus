import json

import pytest

import src.agent_loop as al
from tests.test_agent_harness_loop import _collect, _events, _patch_common


@pytest.mark.parametrize("overrides,mode,partial,expected", [
    (None, None, False, [True, True, False]),
    ({"think": True}, None, False, [True, True, True]),
    ({"think": False}, None, False, [False, False, False]),
    (None, {"source": "explicit", "mode": "deep"}, False, [True, True, True]),
    (None, None, True, [True, True, True]),
])
def test_thinking_only_token_stops_recover_without_changing_user_choices(
    tmp_path, monkeypatch, overrides, mode, partial, expected,
):
    _patch_common(monkeypatch)
    seen = []
    requests = []

    async def stream(_candidates, messages, **kwargs):
        seen.append(dict(kwargs.get("gen_overrides") or {}))
        requests.append(al._extract_last_user_message(messages))
        if len(seen) <= 2:
            yield 'data: ' + json.dumps({"delta": "Still considering the same data. " * 20, "thinking": True}) + '\n\n'
            if partial:
                yield 'data: ' + json.dumps({"delta": "Una sesión registra "}) + '\n\n'
            finish = "length"
        else:
            yield 'data: ' + json.dumps({"delta": "Una sesión registra cuánto tiempo se jugó en una fecha concreta."}) + '\n\n'
            finish = "stop"
        yield 'data: ' + json.dumps({"type": "finish", "finish_reason": finish}) + '\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr(al, "stream_llm_with_fallback", stream)
    events = _events(_collect(al.stream_agent_loop(
        "http://127.0.0.1:8081/v1", "qwen3.8-27b-q8-llamacpp",
        [{"role": "user", "content": "¿Qué significa una sesión de juego?"}],
        workspace=str(tmp_path), max_rounds=5, max_tokens=1200,
        gen_overrides=overrides, think_mode=mode, relevant_tools={"read_file"},
        harness_options={"no_memory": True, "no_skills": True, "repo_map": False, "review_model": "off"},
    )))
    assert [s.get("think") for s in seen[:3]] == expected
    assert set(requests) == {"¿Qué significa una sesión de juego?"}
    # The workspace's independent no-action nudge can request another reply;
    # it must retain the recovered mode rather than enabling thinking again.
    assert all(s.get("think") is expected[-1] for s in seen[3:])
    recovery = [e for e in events if e.get("status") == "thinking_length_recovery"]
    if expected[-1] is False and overrides is None:
        assert len(recovery) == 2
        assert seen[1]["reasoning_effort"] == "low" and seen[1]["reasoning_budget"] == 600
        assert "reasoning_budget" not in seen[2]
    else:
        assert not recovery
    assert any("cuánto tiempo" in e.get("delta", "") and not e.get("thinking") for e in events)
