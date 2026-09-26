"""Per-provider request extras: parallel tool calls on a self-hosted server,
a stable cache key on OpenAI."""
from src import llm_core


def test_self_hosted_server_gets_parallel_tool_calls(monkeypatch):
    monkeypatch.setattr("src.model_context.is_local_endpoint", lambda url: True)
    p = {"tools": [{"type": "function", "function": {"name": "read_file"}}]}
    llm_core._apply_parallel_tool_calls(p, "http://127.0.0.1:8081/v1/chat/completions")
    assert p["parallel_tool_calls"] is True


def test_hosted_and_ollama_are_left_alone(monkeypatch):
    p = {"tools": [{"type": "function", "function": {"name": "x"}}]}
    llm_core._apply_parallel_tool_calls(p, "https://api.openai.com/v1/chat/completions")
    llm_core._apply_parallel_tool_calls(p, "http://127.0.0.1:11434/v1/chat/completions")
    assert "parallel_tool_calls" not in p
    q = {}
    llm_core._apply_parallel_tool_calls(q, "http://127.0.0.1:8081/v1/chat/completions")
    assert "parallel_tool_calls" not in q


def test_openai_gets_a_hashed_stable_cache_key():
    a, b = {}, {}
    llm_core._apply_openai_cache_key(a, "https://api.openai.com/v1/chat/completions", "sess-1")
    llm_core._apply_openai_cache_key(b, "https://api.openai.com/v1/chat/completions", "sess-1")
    assert a["prompt_cache_key"] == b["prompt_cache_key"] and "sess-1" not in a["prompt_cache_key"]
    c = {}
    llm_core._apply_openai_cache_key(c, "http://127.0.0.1:8081/v1/chat/completions", "sess-1")
    assert c == {}
