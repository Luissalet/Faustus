"""reasoning_effort against a chat template that restricts it. Seen live:
Qwen3.8's template accepts xhigh/medium/low only, and the "high" Faustus
sends for deep think mode / effort=high came back as HTTP 500."""
from __future__ import annotations

from src import chat_helpers, llm_core

QWEN38_TEMPLATE = """
{%- if enable_thinking is undefined or enable_thinking is true %}
    {%- set resolved_reasoning_effort = reasoning_effort|default('xhigh') %}
    {%- if resolved_reasoning_effort not in ('xhigh', 'medium', 'low') %}
        {{- raise_exception('Unexpected reasoning effort ' ~ reasoning_effort ~ '. Supported types are xhigh (default), medium, and low.') }}
    {%- endif %}
{%- endif %}
"""


def test_the_template_lists_its_efforts_default_first():
    assert chat_helpers.parse_reasoning_efforts(QWEN38_TEMPLATE) == ("xhigh", "medium", "low")
    assert chat_helpers.parse_reasoning_efforts("{{ messages }}") is None
    assert chat_helpers.parse_reasoning_efforts("") is None


def test_faustus_levels_map_onto_the_template():
    accepted = ("xhigh", "medium", "low")
    assert llm_core.fit_reasoning_effort("high", accepted) == "xhigh"
    assert llm_core.fit_reasoning_effort("medium", accepted) == "medium"
    assert llm_core.fit_reasoning_effort("low", accepted) == "low"
    assert llm_core.fit_reasoning_effort("xhigh", accepted) == "xhigh"
    assert llm_core.fit_reasoning_effort("minimal", accepted) == "low"
    assert llm_core.fit_reasoning_effort("none", accepted) == "low"
    assert llm_core.fit_reasoning_effort("bogus", accepted) is None
    # an unrestricted template gets the value as it is
    assert llm_core.fit_reasoning_effort("high", None) == "high"
    # a template that does list "high" keeps it
    assert llm_core.fit_reasoning_effort("high", ("low", "medium", "high")) == "high"


def test_the_payload_is_fitted_only_for_a_local_llama_server(monkeypatch):
    monkeypatch.setattr(chat_helpers, "llamacpp_reasoning_efforts", lambda url: ("xhigh", "medium", "low"))
    payload = {"reasoning_effort": "high"}
    llm_core._fit_reasoning_effort_to_template(payload, "http://127.0.0.1:8081/v1")
    assert payload["reasoning_effort"] == "xhigh"
    bogus = {"reasoning_effort": "bogus"}
    llm_core._fit_reasoning_effort_to_template(bogus, "http://127.0.0.1:8081/v1")
    assert "reasoning_effort" not in bogus
    remote = {"reasoning_effort": "high"}
    llm_core._fit_reasoning_effort_to_template(remote, "https://api.openai.com/v1")
    assert remote["reasoning_effort"] == "high"
